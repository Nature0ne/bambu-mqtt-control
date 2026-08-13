from __future__ import annotations

import copy
import math
import re
import threading
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .commands import LIGHT_NODES
from .config import PrinterConfig

AMS_ENV_PATTERN = re.compile(
    r"\[AMS\]\[TASK\]ams(?P<id>\d+)\s+temp:(?P<temp>-?\d+(?:\.\d+)?);"
    r"humidity:(?P<humidity>\d+(?:\.\d+)?)%;humidity_idx:(?P<index>\d+)",
    re.IGNORECASE,
)
FULL_STATUS_FIELD_GROUPS = (
    frozenset({"gcode_state"}),
    frozenset({"nozzle_temper"}),
    frozenset({"bed_temper"}),
    frozenset({"mc_percent", "percent"}),
    frozenset({"mc_remaining_time", "remain_time"}),
    frozenset({"layer_num"}),
    frozenset({"total_layer_num"}),
    frozenset({"spd_lvl"}),
    frozenset({"ams"}),
    frozenset({"lights_report"}),
    frozenset({"hms"}),
    frozenset({"print_error"}),
    frozenset({"cooling_fan_speed"}),
    frozenset({"nozzle_target_temper"}),
    frozenset({"bed_target_temper"}),
)
SIGNATURE_REQUIRED_BIT = 0x20000000
HOME_FLAG_SD_CARD_PRESENT = 0x00000100
HOME_FLAG_SD_CARD_ABNORMAL = 0x00000200
DOOR_OPEN_BIT = 0x00800000
HMS_SEVERITIES = {
    1: "fatal",
    2: "serious",
    3: "common",
    4: "info",
}
HMS_MODULES = {
    0x03: "motion_controller",
    0x05: "mainboard",
    0x07: "ams",
    0x08: "toolhead",
    0x0C: "camera",
}
VERSION_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/+:-]*")
LIGHT_NODE_LABELS = {
    "chamber_light": "Bauraumlicht",
    "chamber_light2": "Bauraumlicht 2",
    "work_light": "Arbeitslicht",
    "heatbed_light": "Druckbettlicht",
}
DRY_CAPABLE_AMS_MODELS = frozenset({"AMS 2 Pro", "AMS HT"})
AMS_MODULE_PREFIXES = (
    ("ams_f1/", "AMS Lite"),
    ("ams/", "AMS"),
    ("n3f/", "AMS 2 Pro"),
    ("n3s/", "AMS HT"),
)


def deep_merge(target: dict[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    """Merge partial MQTT reports while replacing arrays as atomic values."""
    for key, value in update.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            deep_merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)
    return target


def _is_full_status(print_section: Mapping[str, Any]) -> bool:
    message_kind = _integer(print_section.get("msg"))
    if "msg" in print_section and message_kind != 0:
        return False
    keys = print_section.keys()
    return all(group.intersection(keys) for group in FULL_STATUS_FIELD_GROUPS)


def _number(value: Any) -> float | int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else round(number, 2)


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _strict_integer(value: Any) -> int | None:
    number = _number(value)
    if number is None or not float(number).is_integer():
        return None
    return int(number)


def _feature_bits(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        canonical = value.strip()
        if not canonical or not re.fullmatch(r"[0-9a-fA-F]+", canonical):
            return None
        try:
            return int(canonical, 16)
        except ValueError:
            return None
    return _integer(value)


def _uint32(value: Any) -> int | None:
    number = _strict_integer(value)
    if number is None or not 0 <= number <= 0xFFFFFFFF:
        return None
    return number


def _safe_version_token(value: Any, *, max_length: int = 64) -> str | None:
    """Keep version metadata useful without reflecting arbitrary report strings."""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not 1 <= len(text) <= max_length or not VERSION_TOKEN_PATTERN.fullmatch(text):
        return None
    return text


def _normalise_version_modules(modules: Any) -> list[dict[str, str | None]]:
    if not isinstance(modules, list):
        return []
    result: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for module in modules[:64]:
        if not isinstance(module, Mapping):
            continue
        name = _safe_version_token(module.get("name"), max_length=48)
        if name is None or name in seen:
            continue
        software = _safe_version_token(
            module.get("sw_ver", module.get("software"))
        )
        hardware = _safe_version_token(
            module.get("hw_ver", module.get("hardware"))
        )
        if software is None and hardware is None:
            continue
        seen.add(name)
        result.append(
            {
                "name": name,
                "software": software,
                "hardware": hardware,
            }
        )
    return result


def _normalise_wifi_signal(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(-?\d{1,3})\s*(?:dBm)?\s*", value, re.IGNORECASE)
        if not match:
            return None
        signal = int(match.group(1))
    else:
        signal = _strict_integer(value)
    if signal is None or not -127 <= signal <= 0:
        return None
    return signal


def _normalise_stage_id(value: Any) -> int | None:
    stage_id = _strict_integer(value)
    if stage_id is None or stage_id in {-1, 255} or not 0 <= stage_id <= 65534:
        return None
    return stage_id


def _normalise_sd_card(print_state: Mapping[str, Any]) -> dict[str, Any]:
    home_flag = _integer(print_state.get("home_flag"))
    if home_flag is not None and home_flag & HOME_FLAG_SD_CARD_ABNORMAL:
        return {"present": True, "status": "abnormal"}

    direct = _enabled_flag(print_state.get("sdcard")) if "sdcard" in print_state else None
    if direct is not None:
        return {"present": direct, "status": "normal" if direct else "missing"}

    if home_flag is None:
        return {"present": None, "status": "unknown"}
    present = bool(home_flag & HOME_FLAG_SD_CARD_PRESENT)
    return {"present": present, "status": "normal" if present else "missing"}


def _normalise_door_open(
    print_state: Mapping[str, Any], printer_model: Any
) -> bool | None:
    if "door_open" in print_state:
        return _enabled_flag(print_state.get("door_open"))

    canonical_model = re.sub(r"[^a-z0-9]", "", str(printer_model or "").lower())
    if canonical_model.startswith("x1"):
        home_flag = _integer(print_state.get("home_flag"))
        return None if home_flag is None else bool(home_flag & DOOR_OPEN_BIT)
    if canonical_model.startswith(("h2", "p2s", "x2d")):
        stat = _feature_bits(print_state.get("stat"))
        return None if stat is None else bool(stat & DOOR_OPEN_BIT)
    return None


def _normalise_hms(print_state: Mapping[str, Any]) -> dict[str, Any]:
    result: list[dict[str, str]] = []
    seen: set[tuple[int, int]] = set()
    hms_values = print_state.get("hms")
    if not isinstance(hms_values, list):
        hms_values = []
    for item in hms_values[:64]:
        if not isinstance(item, Mapping):
            continue
        attribute = _uint32(item.get("attr"))
        code = _uint32(item.get("code"))
        if not attribute or not code or (attribute, code) in seen:
            continue
        seen.add((attribute, code))
        identifier = (
            f"HMS_{attribute >> 16:04X}_{attribute & 0xFFFF:04X}_"
            f"{code >> 16:04X}_{code & 0xFFFF:04X}"
        )
        result.append(
            {
                "code": identifier,
                "module": HMS_MODULES.get((attribute >> 24) & 0xFF, "unknown"),
                "severity": HMS_SEVERITIES.get(code >> 16, "unknown"),
            }
        )
    return {"count": len(result), "items": result}


def _normalise_diagnostics(
    print_state: Mapping[str, Any],
    modules: Any,
    printer_model: Any,
) -> dict[str, Any]:
    version_modules = _normalise_version_modules(modules)
    printer_module = next(
        (module for module in version_modules if module["name"].lower() == "ota"),
        None,
    )
    return {
        "wifi_signal_dbm": _normalise_wifi_signal(print_state.get("wifi_signal")),
        "door_open": _normalise_door_open(print_state, printer_model),
        "sd_card": _normalise_sd_card(print_state),
        "print_stage": {
            "phase_id": _normalise_stage_id(print_state.get("stg_cur")),
            "stage_id": _normalise_stage_id(print_state.get("mc_print_stage")),
            "substage_id": _normalise_stage_id(
                print_state.get("mc_print_sub_stage")
            ),
        },
        "firmware": {
            "printer": (
                {
                    "software": printer_module["software"],
                    "hardware": printer_module["hardware"],
                }
                if printer_module is not None
                else {"software": None, "hardware": None}
            ),
            "modules": version_modules,
        },
        "hms": _normalise_hms(print_state),
    }


def drying_printer_policy(model: str) -> str:
    """Return the conservative support level for AMS drying on a printer."""
    canonical = re.sub(r"[^a-z0-9]", "", str(model or "").lower())
    if canonical.startswith(("p1", "a1")):
        return "blocked"
    if canonical.startswith(("x1", "h2c", "h2s", "x2d")):
        return "experimental"
    if canonical.startswith(("p2s", "h2d")):
        return "supported"
    return "unsupported"


def _ams_models_from_modules(modules: Any) -> dict[str, dict[str, str]]:
    if not isinstance(modules, list):
        return {}
    result: dict[str, dict[str, str]] = {}
    for module in modules:
        if not isinstance(module, Mapping):
            continue
        name = str(module.get("name") or "").strip().lower()
        for prefix, model in AMS_MODULE_PREFIXES:
            if not name.startswith(prefix):
                continue
            suffix = name[len(prefix):]
            if suffix.isdecimal() and 0 <= int(suffix) <= 255:
                instance = int(suffix)
                # AMS HT instances live in the printer's 128..135 AMS-ID
                # range even though get_version commonly reports n3s/0..
                # n3s/7.  Firmware that already reports 128+ is retained.
                ams_id = instance + 128 if prefix == "n3s/" and instance < 128 else instance
                if ams_id > 255:
                    break
                result[str(ams_id)] = {
                    "model": model,
                    "source": "get_version",
                }
            break
    return result


def _color(value: Any) -> str | None:
    text = str(value or "").strip().lstrip("#")
    if len(text) == 8:
        text = text[:6]
    if len(text) != 6 or any(char not in "0123456789abcdefABCDEF" for char in text):
        return None
    return f"#{text.upper()}"


def _fan_percent(value: Any) -> int | None:
    number = _number(value)
    if number is None:
        return None
    number = float(number)
    if 0 <= number <= 15:
        number = number / 15 * 100
    elif 15 < number <= 255:
        number = number / 255 * 100
    return max(0, min(100, round(number)))


def _temperature(current: Any, target: Any = None) -> dict[str, float | int | None]:
    return {"current": _number(current), "target": _number(target)}


def _normalise_lights(print_state: Mapping[str, Any]) -> dict[str, Any]:
    reported: dict[str, str] = {}
    reports = print_state.get("lights_report") or []
    if not isinstance(reports, list):
        reports = []
    for item in reports:
        if not isinstance(item, Mapping):
            continue
        node_value = item.get("node")
        if not isinstance(node_value, str):
            node_value = item.get("led_node")
        if not isinstance(node_value, str):
            continue
        node = node_value.strip()
        if node not in LIGHT_NODES:
            continue
        mode = str(item.get("mode") or item.get("led_mode") or "unknown").lower()
        if mode not in {"on", "off", "flashing"}:
            mode = "unknown"
        reported[node] = mode
    return {
        # Retain the original two fields for existing API/UI clients.
        "chamber": reported.get("chamber_light", "unknown"),
        "work": reported.get("work_light", "unknown"),
        "nodes": [
            {
                "node": node,
                "label": label,
                "mode": reported[node],
            }
            for node, label in LIGHT_NODE_LABELS.items()
            if node in reported
        ],
    }


def _enabled_flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    canonical = str(value or "").strip().lower()
    if canonical in {"1", "on", "enable", "enabled", "true"}:
        return True
    if canonical in {"0", "off", "disable", "disabled", "false"}:
        return False
    return None


def _normalise_camera(print_state: Mapping[str, Any]) -> dict[str, Any]:
    ipcam = print_state.get("ipcam")
    if not isinstance(ipcam, Mapping):
        ipcam = {}

    supported_values = ipcam.get("resolution_supported")
    if not isinstance(supported_values, list):
        supported_values = []
    supported: list[str] = []
    for value in supported_values:
        if not isinstance(value, str):
            continue
        resolution = value.strip()
        if 1 <= len(resolution) <= 32 and resolution not in supported:
            supported.append(resolution)

    liveview = ipcam.get("liveview")
    local_protocol: str | None = None
    if isinstance(liveview, Mapping):
        candidate = str(liveview.get("local") or "").strip().lower()
        if candidate in {"local", "rtsp", "rtsps", "disabled", "none"}:
            local_protocol = candidate
    rtsp_url = str(ipcam.get("rtsp_url") or "").strip().lower()
    if rtsp_url.startswith("rtsps"):
        local_protocol = "rtsps"
    elif rtsp_url.startswith("rtsp"):
        local_protocol = "rtsp"
    elif rtsp_url == "disable":
        local_protocol = "disabled"

    available = _enabled_flag(ipcam.get("ipcam_dev"))
    if available is None and (
        local_protocol in {"local", "rtsp", "rtsps"}
        or supported
        or "ipcam_record" in ipcam
        or "timelapse" in ipcam
    ):
        available = True

    resolution_value = ipcam.get("resolution")
    resolution = (
        resolution_value.strip()
        if isinstance(resolution_value, str) and resolution_value.strip()
        else None
    )
    return {
        "available": available,
        "recording": _enabled_flag(ipcam.get("ipcam_record")),
        "timelapse": _enabled_flag(ipcam.get("timelapse")),
        "resolution": resolution,
        "resolution_supported": supported,
        "local_protocol": local_protocol,
    }


def _normalise_ams(
    print_state: Mapping[str, Any],
    environment: Mapping[str, Mapping[str, Any]] | None = None,
    models: Mapping[str, Mapping[str, str]] | None = None,
) -> tuple[dict[str, Any], int | None]:
    ams_state = print_state.get("ams")
    if not isinstance(ams_state, Mapping):
        ams_state = {}

    active_tray = _integer(ams_state.get("tray_now"))
    if active_tray == 255:
        active_tray = None
    unit_values = ams_state.get("ams") or []
    if not isinstance(unit_values, list):
        unit_values = []
    units: list[dict[str, Any]] = []

    for unit_index, unit_value in enumerate(unit_values):
        if not isinstance(unit_value, Mapping):
            continue
        unit_id = _integer(unit_value.get("id", unit_value.get("ams_id")))
        if unit_id is None:
            unit_id = unit_index
        environment_value = (environment or {}).get(str(unit_id), {})
        model_value = (models or {}).get(str(unit_id), {})
        tray_values = unit_value.get("tray") or unit_value.get("trays") or []
        if not isinstance(tray_values, list):
            tray_values = []
        slots: list[dict[str, Any]] = []
        for slot_index, tray in enumerate(tray_values):
            if not isinstance(tray, Mapping):
                continue
            slot_id = _integer(tray.get("id", tray.get("tray_id")))
            if slot_id is None:
                slot_id = slot_index
            global_id = _integer(
                tray.get("global_id", tray.get("tray_global_id"))
            )
            if global_id is None and 0 <= unit_id <= 3:
                global_id = unit_id * 4 + slot_id
            material = str(tray.get("tray_type") or tray.get("tray_info_idx") or "").strip() or None
            tag_uid = str(tray.get("tag_uid") or "").strip()
            has_rfid_tag = bool(tag_uid and set(tag_uid) != {"0"})
            remain = _number(tray.get("remain"))
            if remain is not None and not 0 <= float(remain) <= 100:
                remain = None
            if not has_rfid_tag:
                # Third-party/AMS-Lite values are commonly fixed at 0 or 100
                # rather than a measured quantity. Avoid false precision.
                remain = None
            slots.append(
                {
                    "id": slot_id,
                    "global_id": global_id,
                    "material": material,
                    "sub_brand": str(tray.get("tray_sub_brands") or tray.get("tray_id_name") or "").strip() or None,
                    "color": _color(tray.get("tray_color")),
                    "remaining_percent": remain,
                    "active": bool(tray.get("active"))
                    or (global_id is not None and global_id == active_tray),
                    "empty": material is None and not has_rfid_tag,
                    "rfid_state": "read" if has_rfid_tag else "unknown",
                }
            )
        humidity_index = _integer(
            environment_value.get("humidity_index", unit_value.get("humidity"))
        )
        if humidity_index is not None and not 1 <= humidity_index <= 5:
            humidity_index = None
        humidity_percent = _number(
            environment_value.get("humidity_percent", unit_value.get("humidity_raw"))
        )
        if humidity_percent is not None and not 0 <= float(humidity_percent) <= 100:
            humidity_percent = None
        dry_time = _integer(unit_value.get("dry_time"))
        if dry_time is not None and dry_time < 0:
            dry_time = None
        dry_setting = unit_value.get("dry_setting")
        if not isinstance(dry_setting, Mapping):
            dry_setting = {}
        dry_temperature = _number(dry_setting.get("dry_temperature"))
        dry_duration = _integer(dry_setting.get("dry_duration"))
        if dry_duration is not None and dry_duration < 0:
            dry_duration = None
        dry_filament_value = dry_setting.get("dry_filament")
        dry_filament = (
            str(dry_filament_value).strip()
            if isinstance(dry_filament_value, str) and dry_filament_value.strip()
            else None
        )
        ams_model = str(model_value.get("model") or "unknown")
        units.append(
            {
                "id": unit_id,
                "model": ams_model,
                "model_source": model_value.get("source"),
                "dry_capable": ams_model in DRY_CAPABLE_AMS_MODELS,
                "experimental": False,
                "drying": {
                    "active": dry_time is not None and dry_time > 0,
                    "remaining_minutes": dry_time,
                    "temperature": dry_temperature,
                    "duration_hours": dry_duration,
                    "filament": dry_filament,
                },
                "humidity_index": humidity_index,
                "humidity_percent": humidity_percent,
                "temperature": _number(
                    environment_value.get("temperature", unit_value.get("temp"))
                ),
                "slots": slots,
            }
        )
    external_value = print_state.get("vt_tray")
    external_spool: dict[str, Any] | None = None
    if isinstance(external_value, Mapping) and external_value:
        material = str(
            external_value.get("tray_type")
            or external_value.get("tray_info_idx")
            or ""
        ).strip() or None
        tag_uid = str(external_value.get("tag_uid") or "").strip()
        has_rfid_tag = bool(tag_uid and set(tag_uid) != {"0"})
        remain = _number(external_value.get("remain"))
        if remain is not None and not 0 <= float(remain) <= 100:
            remain = None
        if not has_rfid_tag:
            remain = None
        external_spool = {
            "id": 254,
            "global_id": 254,
            "material": material,
            "sub_brand": str(
                external_value.get("tray_sub_brands")
                or external_value.get("tray_id_name")
                or ""
            ).strip()
            or None,
            "color": _color(external_value.get("tray_color")),
            "remaining_percent": remain,
            "active": active_tray == 254,
            "empty": material is None and not has_rfid_tag,
            "rfid_state": "read" if has_rfid_tag else "unknown",
        }
    return {"units": units, "external_spool": external_spool}, active_tray


def _normalise_errors(print_state: Mapping[str, Any]) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    severity_map = {
        "fatal": "error",
        "serious": "error",
        "common": "warning",
        "info": "info",
        "unknown": "warning",
    }
    for item in _normalise_hms(print_state)["items"]:
        code = item["code"]
        errors.append(
            {
                "code": code,
                "message": f"Der Drucker meldet {code}.",
                "severity": severity_map[item["severity"]],
            }
        )
    print_error = _integer(print_state.get("print_error"))
    if print_error:
        errors.append(
            {
                "code": str(print_error),
                "message": f"Der Drucker meldet Fehlercode {print_error}.",
                "severity": "error",
            }
        )
    return errors


def normalise_report(raw: Mapping[str, Any]) -> dict[str, Any]:
    print_state = raw.get("print") if isinstance(raw.get("print"), Mapping) else raw
    environment = raw.get("_bambu_control_ams_environment")
    if not isinstance(environment, Mapping):
        environment = {}
    ams_models = raw.get("_bambu_control_ams_models")
    if not isinstance(ams_models, Mapping):
        ams_models = {}
    modules = raw.get("_bambu_control_version_modules")
    printer_model = raw.get("_bambu_control_printer_model")
    ams, active_tray = _normalise_ams(print_state, environment, ams_models)
    progress = _number(print_state.get("mc_percent", print_state.get("percent")))
    if progress is not None and not 0 <= float(progress) <= 100:
        progress = None
    remaining_minutes = _integer(
        print_state.get("mc_remaining_time", print_state.get("remain_time"))
    )
    if remaining_minutes is not None and remaining_minutes < 0:
        remaining_minutes = None
    feature_bits = _feature_bits(print_state.get("fun"))
    return {
        "status": str(print_state.get("gcode_state") or "unknown").lower(),
        "progress": progress,
        "remaining_minutes": remaining_minutes,
        "current_layer": _integer(print_state.get("layer_num")),
        "total_layers": _integer(print_state.get("total_layer_num")),
        "task_name": str(print_state.get("subtask_name") or print_state.get("task_name") or "").strip() or None,
        "gcode_file": str(print_state.get("gcode_file") or "").strip() or None,
        "speed_level": _integer(print_state.get("spd_lvl")),
        "temperatures": {
            "nozzle": _temperature(print_state.get("nozzle_temper"), print_state.get("nozzle_target_temper")),
            "bed": _temperature(print_state.get("bed_temper"), print_state.get("bed_target_temper")),
            "chamber": _temperature(print_state.get("chamber_temper")),
        },
        "fans": {
            "part": _fan_percent(print_state.get("cooling_fan_speed")),
            "heatbreak": _fan_percent(print_state.get("heatbreak_fan_speed")),
            "aux": _fan_percent(print_state.get("big_fan1_speed")),
            "chamber": _fan_percent(print_state.get("big_fan2_speed")),
        },
        "camera": _normalise_camera(print_state),
        "lights": _normalise_lights(print_state),
        "active_tray": active_tray,
        "developer_lan_mode": (
            None
            if feature_bits is None
            else not bool(feature_bits & SIGNATURE_REQUIRED_BIT)
        ),
        "ams": ams,
        "errors": _normalise_errors(print_state),
        "diagnostics": _normalise_diagnostics(
            print_state,
            modules,
            printer_model,
        ),
    }


class StateStore:
    def __init__(self, configs: tuple[PrinterConfig, ...], clock=time.time):
        self._configs = {config.id: config for config in configs}
        self._clock = clock
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._raw = {printer_id: {} for printer_id in self._configs}
        self._last_seen: dict[str, float | None] = {printer_id: None for printer_id in self._configs}
        self._connection = {printer_id: "starting" for printer_id in self._configs}
        self._connection_error: dict[str, str | None] = {printer_id: None for printer_id in self._configs}
        self._complete = {printer_id: False for printer_id in self._configs}
        self._ams_environment: dict[str, dict[str, dict[str, Any]]] = {
            printer_id: {} for printer_id in self._configs
        }
        self._ams_models: dict[str, dict[str, dict[str, str]]] = {
            printer_id: {} for printer_id in self._configs
        }
        self._version_modules: dict[str, list[dict[str, str | None]]] = {
            printer_id: [] for printer_id in self._configs
        }
        self._ams_dry_generation: dict[str, dict[int, int]] = {
            printer_id: {} for printer_id in self._configs
        }

    def mark_connection(self, printer_id: str, state: str, error: str | None = None) -> None:
        if state not in {"starting", "connecting", "online", "offline", "error", "stopped"}:
            raise ValueError(f"invalid connection state: {state}")
        with self._lock:
            self._connection[printer_id] = state
            self._connection_error[printer_id] = error
            if state in {"starting", "connecting", "offline", "error", "stopped"}:
                self._complete[printer_id] = False

    def apply_report(self, printer_id: str, payload: Mapping[str, Any]) -> bool:
        if not isinstance(payload, Mapping):
            raise TypeError("MQTT report must be an object")
        with self._lock:
            changed = False
            status_changed = False
            print_section = payload.get("print")
            if isinstance(print_section, Mapping):
                command = str(print_section.get("command") or "")
                if command == "push_status":
                    reported_ams = print_section.get("ams")
                    reported_units = (
                        reported_ams.get("ams")
                        if isinstance(reported_ams, Mapping)
                        else None
                    )
                    if isinstance(reported_units, list):
                        for unit_index, unit in enumerate(reported_units):
                            if not isinstance(unit, Mapping) or "dry_time" not in unit:
                                continue
                            unit_id = _integer(unit.get("id", unit.get("ams_id")))
                            if unit_id is None:
                                unit_id = unit_index
                            generations = self._ams_dry_generation[printer_id]
                            generations[unit_id] = generations.get(unit_id, 0) + 1
                    deep_merge(self._raw[printer_id], {"print": print_section})
                    changed = True
                    status_changed = True
                    if _is_full_status(print_section):
                        self._complete[printer_id] = True
            mc_print = payload.get("mc_print")
            if isinstance(mc_print, Mapping):
                match = AMS_ENV_PATTERN.search(str(mc_print.get("param") or ""))
                if match:
                    self._ams_environment[printer_id][match.group("id")] = {
                        "temperature": _number(match.group("temp")),
                        "humidity_percent": _number(match.group("humidity")),
                        "humidity_index": _integer(match.group("index")),
                    }
                    changed = True
            info_section = payload.get("info")
            if (
                isinstance(info_section, Mapping)
                and str(info_section.get("command") or "") == "get_version"
            ):
                reported_modules_value = info_section.get("module")
                reported_models = _ams_models_from_modules(reported_modules_value)
                for ams_id, model in reported_models.items():
                    if self._ams_models[printer_id].get(ams_id) != model:
                        self._ams_models[printer_id][ams_id] = model
                        changed = True
                reported_versions = _normalise_version_modules(
                    reported_modules_value
                )
                if (
                    reported_versions
                    and self._version_modules[printer_id] != reported_versions
                ):
                    self._version_modules[printer_id] = reported_versions
                    changed = True
            if status_changed:
                self._last_seen[printer_id] = self._clock()
                self._connection[printer_id] = "online"
                self._connection_error[printer_id] = None
            if changed:
                self._condition.notify_all()
            return changed

    def raw_report(self, printer_id: str) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._raw[printer_id])

    def ams_operation_risk(self, printer_id: str) -> bool:
        """Report filament movement/loading states that make tray rotation unsafe."""
        with self._lock:
            raw = self._raw[printer_id]
            print_state = raw.get("print")
            if not isinstance(print_state, Mapping):
                return True
            ams_state = print_state.get("ams")
            if not isinstance(ams_state, Mapping):
                return True
            for field_name in ("tray_now", "tray_tar"):
                value = _integer(ams_state.get(field_name))
                if value is not None and value != 255:
                    return True
            ams_status = _integer(
                print_state.get("ams_status", ams_state.get("ams_status"))
            )
            if ams_status != 0:
                return True
            unit_values = ams_state.get("ams")
            if not isinstance(unit_values, list):
                return True
            for unit in unit_values:
                if not isinstance(unit, Mapping):
                    continue
                trays = unit.get("tray", unit.get("trays"))
                if not isinstance(trays, list):
                    continue
                for tray in trays:
                    if isinstance(tray, Mapping) and tray.get("active") is True:
                        return True
            return False

    def wait_for_ams_drying_state(
        self,
        printer_id: str,
        ams_id: int,
        *,
        active: bool,
        after_generation: int,
        timeout: float,
    ) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while True:
                generation = self._ams_dry_generation[printer_id].get(ams_id, 0)
                print_state = self._raw[printer_id].get("print")
                ams_state = (
                    print_state.get("ams")
                    if isinstance(print_state, Mapping)
                    else None
                )
                units = ams_state.get("ams") if isinstance(ams_state, Mapping) else None
                if isinstance(units, list):
                    for unit in units:
                        if not isinstance(unit, Mapping):
                            continue
                        unit_id = _integer(unit.get("id", unit.get("ams_id")))
                        if unit_id != ams_id:
                            continue
                        dry_time = _integer(unit.get("dry_time"))
                        if generation > after_generation and (
                            (active and dry_time is not None and dry_time > 0)
                            or (not active and dry_time == 0)
                        ):
                            return True
                        break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)

    def ams_dry_generation(self, printer_id: str, ams_id: int) -> int:
        with self._lock:
            return self._ams_dry_generation[printer_id].get(ams_id, 0)

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        now = self._clock()
        with self._lock:
            printers: list[dict[str, Any]] = []
            for config in self._configs.values():
                last_seen = self._last_seen[config.id]
                stale = last_seen is None or now - last_seen > config.stale_after_seconds
                connection = self._connection[config.id]
                online = connection == "online" and not stale
                public_connection = (
                    "partial" if connection == "online" and not self._complete[config.id] else connection
                )
                raw = copy.deepcopy(self._raw[config.id])
                raw["_bambu_control_ams_environment"] = copy.deepcopy(
                    self._ams_environment[config.id]
                )
                raw["_bambu_control_ams_models"] = copy.deepcopy(
                    self._ams_models[config.id]
                )
                raw["_bambu_control_version_modules"] = copy.deepcopy(
                    self._version_modules[config.id]
                )
                raw["_bambu_control_printer_model"] = config.model
                state = normalise_report(raw)
                drying_policy = drying_printer_policy(config.model)
                for unit in state["ams"]["units"]:
                    hardware_capable = bool(unit["dry_capable"])
                    unit["dry_capable"] = hardware_capable and drying_policy in {
                        "supported",
                        "experimental",
                    }
                    unit["experimental"] = (
                        hardware_capable and drying_policy == "experimental"
                    )
                if self._connection_error[config.id]:
                    state["errors"] = [
                        {
                            "code": "connection",
                            "message": self._connection_error[config.id],
                            "severity": "error",
                        },
                        *state["errors"],
                    ]
                printers.append(
                    {
                        "id": config.id,
                        "name": config.name,
                        "model": config.model,
                        "online": online,
                        "stale": stale,
                        "connection_state": public_connection,
                        "last_seen": datetime.fromtimestamp(last_seen, timezone.utc).isoformat() if last_seen else None,
                        "writable": config.writable,
                        "capabilities": sorted(config.allowed_commands) if config.writable else [],
                        "state": state,
                    }
                )
            return {"printers": printers}
