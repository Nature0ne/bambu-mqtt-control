from __future__ import annotations

import itertools
import math
import secrets
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


class CommandError(ValueError):
    """A rejected or malformed printer command."""


class FrozenDict(dict[str, Any]):
    """A JSON-serialisable dict that cannot be mutated after construction."""

    @staticmethod
    def _immutable(*_args: Any, **_kwargs: Any) -> None:
        raise TypeError("command payload is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __ior__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


def _freeze_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return FrozenDict({key: _freeze_payload(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_payload(item) for item in value)
    return value


@dataclass(frozen=True)
class BuiltCommand:
    name: str
    sequence_id: str
    payload: FrozenDict


class CommandBuilder:
    def __init__(self, start: int | None = None):
        # Keep identifiers decimal and within a signed 32-bit range, while
        # avoiding collisions with delayed replies after a service restart.
        initial = start if start is not None else secrets.randbelow(2_000_000_000) + 1
        self._counter = itertools.count(initial)
        self._lock = threading.Lock()

    def _sequence(self) -> str:
        with self._lock:
            return str(next(self._counter))

    @staticmethod
    def _integer(params: Mapping[str, Any], name: str, minimum: int, maximum: int) -> int:
        value = params.get(name)
        if isinstance(value, bool):
            raise CommandError(f"{name} must be an integer")
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise CommandError(f"{name} must be an integer") from exc
        if not math.isfinite(numeric) or not numeric.is_integer():
            raise CommandError(f"{name} must be an integer")
        result = int(numeric)
        if not minimum <= result <= maximum:
            raise CommandError(f"{name} must be between {minimum} and {maximum}")
        return result

    @staticmethod
    def _filament(params: Mapping[str, Any]) -> str:
        value = params.get("filament", "")
        if not isinstance(value, str):
            raise CommandError("filament must be a string")
        if len(value) > 80 or any(character in value for character in ("\x00", "\n", "\r")):
            raise CommandError("filament is malformed")
        return value

    def build(self, command: str, params: Mapping[str, Any] | None = None) -> BuiltCommand:
        if params is None:
            params = {}
        if not isinstance(params, Mapping):
            raise CommandError("params must be an object")
        allowed_params = {
            "pause": frozenset(),
            "resume": frozenset(),
            "stop": frozenset(),
            "speed": frozenset({"level"}),
            "light": frozenset({"on"}),
            "refresh_rfid": frozenset({"ams_id", "slot_id"}),
            "start_drying": frozenset(
                {"ams_id", "temp", "duration", "rotate_tray", "filament"}
            ),
            "stop_drying": frozenset({"ams_id"}),
            "camera_recording": frozenset({"on"}),
            "camera_timelapse": frozenset({"on"}),
            "camera_resolution": frozenset({"resolution"}),
        }
        if command not in allowed_params:
            raise CommandError(f"unsupported command: {command}")
        unexpected = set(params) - allowed_params[command]
        if unexpected:
            raise CommandError(
                f"unexpected parameters for {command}: {sorted(map(str, unexpected))}"
            )
        sequence = self._sequence()

        if command in {"pause", "resume", "stop"}:
            body: dict[str, Any] = {
                "print": {
                    "sequence_id": sequence,
                    "command": command,
                    "param": "",
                }
            }
        elif command == "speed":
            speed_names = {"silent": 1, "standard": 2, "sport": 3, "ludicrous": 4}
            level_value = params.get("level")
            if isinstance(level_value, str) and level_value.lower() in speed_names:
                level = speed_names[level_value.lower()]
            else:
                level = self._integer(params, "level", 1, 4)
            body = {
                "print": {
                    "sequence_id": sequence,
                    "command": "print_speed",
                    "param": str(level),
                }
            }
        elif command == "light":
            on_value = params.get("on")
            if not isinstance(on_value, bool):
                raise CommandError("on must be true or false")
            body = {
                "system": {
                    "sequence_id": sequence,
                    "command": "ledctrl",
                    "led_node": "chamber_light",
                    "led_mode": "on" if on_value else "off",
                    "led_on_time": 500,
                    "led_off_time": 500,
                    "loop_times": 0,
                    "interval_time": 0,
                }
            }
        elif command == "refresh_rfid":
            ams_id = self._integer(params, "ams_id", 0, 255)
            slot_id = self._integer(params, "slot_id", 0, 255)
            body = {
                "print": {
                    "sequence_id": sequence,
                    "command": "ams_get_rfid",
                    "ams_id": ams_id,
                    "slot_id": slot_id,
                }
            }
        elif command == "start_drying":
            ams_id = self._integer(params, "ams_id", 0, 255)
            temperature = self._integer(params, "temp", 45, 85)
            duration = self._integer(params, "duration", 1, 24)
            rotate_tray = params.get("rotate_tray", False)
            if not isinstance(rotate_tray, bool):
                raise CommandError("rotate_tray must be true or false")
            body = {
                "print": {
                    "sequence_id": sequence,
                    "command": "ams_filament_drying",
                    "ams_id": ams_id,
                    "mode": 1,
                    "filament": self._filament(params),
                    "temp": temperature,
                    "duration": duration,
                    "humidity": 0,
                    "rotate_tray": rotate_tray,
                    "cooling_temp": 45,
                    "close_power_conflict": False,
                }
            }
        elif command == "stop_drying":
            ams_id = self._integer(params, "ams_id", 0, 255)
            body = {
                "print": {
                    "sequence_id": sequence,
                    "command": "ams_filament_drying",
                    "ams_id": ams_id,
                    "mode": 0,
                    "filament": "",
                    "temp": 0,
                    "duration": 0,
                    "humidity": 0,
                    "rotate_tray": False,
                    "cooling_temp": 0,
                    "close_power_conflict": False,
                }
            }
        elif command in {"camera_recording", "camera_timelapse"}:
            on_value = params.get("on")
            if not isinstance(on_value, bool):
                raise CommandError("on must be true or false")
            body = {
                "camera": {
                    "sequence_id": sequence,
                    "command": (
                        "ipcam_record_set"
                        if command == "camera_recording"
                        else "ipcam_timelapse"
                    ),
                    "control": "enable" if on_value else "disable",
                }
            }
        elif command == "camera_resolution":
            resolution = params.get("resolution")
            if (
                not isinstance(resolution, str)
                or not 1 <= len(resolution) <= 32
                or not resolution.replace("-", "").replace("_", "").replace(".", "").isalnum()
            ):
                raise CommandError("resolution is malformed")
            body = {
                "camera": {
                    "sequence_id": sequence,
                    "command": "ipcam_resolution_set",
                    "resolution": resolution,
                }
            }
        return BuiltCommand(
            name=command,
            sequence_id=sequence,
            payload=_freeze_payload(body),
        )


def push_all_payload(sequence_id: str = "0") -> dict[str, Any]:
    return {
        "pushing": {
            "sequence_id": sequence_id,
            "command": "pushall",
            "version": 1,
            "push_target": 1,
        }
    }


def get_version_payload(sequence_id: str = "0") -> dict[str, Any]:
    return {
        "info": {
            "sequence_id": sequence_id,
            "command": "get_version",
        }
    }
