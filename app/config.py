from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
SAFE_SERIAL = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
KNOWN_COMMANDS = frozenset(
    {
        "pause",
        "resume",
        "stop",
        "speed",
        "light",
        "refresh_rfid",
        "start_drying",
        "stop_drying",
        "camera_recording",
        "camera_timelapse",
        "camera_resolution",
    }
)
# Drying is deliberately opt-in.  Existing configurations keep their current
# permissions until an administrator explicitly adds the two commands.
DEFAULT_COMMANDS = frozenset({"pause", "resume", "stop", "speed", "light", "refresh_rfid"})


class ConfigError(ValueError):
    """Raised when the local runtime configuration is unsafe or incomplete."""


@dataclass(frozen=True)
class PrinterConfig:
    id: str
    name: str
    host: str
    serial: str
    access_code: str = field(repr=False)
    model: str = "unknown"
    port: int = 8883
    writable: bool = False
    camera_enabled: bool = False
    allowed_commands: frozenset[str] = DEFAULT_COMMANDS
    allow_self_signed_tls: bool = False
    tls_ca_file: str | None = None
    tls_fingerprint_sha256: str | None = None
    stale_after_seconds: int = 90
    full_refresh_seconds: int = 300


@dataclass(frozen=True)
class WebConfig:
    username: str
    password: str = field(repr=False)
    allowed_origins: tuple[str, ...] = ()


@dataclass(frozen=True)
class AppConfig:
    printers: tuple[PrinterConfig, ...]
    web: WebConfig
    audit_db: str = "/var/lib/bambu-control/audit.sqlite3"


def _read_secret(path_value: Any, field_name: str) -> str:
    if not isinstance(path_value, str) or not path_value.startswith("/"):
        raise ConfigError(f"{field_name} must be an absolute secret-file path")
    path = Path(path_value)
    try:
        if path.stat().st_size > 4096:
            raise ConfigError(f"{field_name} is unexpectedly large")
        value = path.read_text(encoding="utf-8").strip()
    except ConfigError:
        raise
    except OSError as exc:
        raise ConfigError(f"cannot read {field_name}: {exc}") from exc
    if not value or "\x00" in value or "\n" in value:
        raise ConfigError(f"{field_name} is empty or malformed")
    return value


def _as_mapping(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{field_name} must be a mapping")
    return value


def _boolean(raw: Mapping[str, Any], name: str, default: bool = False) -> bool:
    value = raw.get(name, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be true or false")
    return value


def _integer_setting(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{field_name} must be an integer")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field_name} must be an integer") from exc
    if not math.isfinite(numeric) or not numeric.is_integer():
        raise ConfigError(f"{field_name} must be an integer")
    return int(numeric)


def _parse_printer(raw_value: Any, seen_ids: set[str], seen_serials: set[str]) -> PrinterConfig:
    raw = _as_mapping(raw_value, "printer")
    printer_id = str(raw.get("id", "")).strip().lower()
    if not SAFE_ID.fullmatch(printer_id):
        raise ConfigError(f"invalid printer id: {printer_id!r}")
    if printer_id in seen_ids:
        raise ConfigError(f"duplicate printer id: {printer_id}")

    serial = str(raw.get("serial", "")).strip()
    if not SAFE_SERIAL.fullmatch(serial):
        raise ConfigError(f"invalid serial for printer {printer_id}")
    if serial in seen_serials:
        raise ConfigError(f"duplicate printer serial: {serial}")

    host = str(raw.get("host", "")).strip()
    if not host or any(char.isspace() for char in host) or len(host) > 253:
        raise ConfigError(f"invalid host for printer {printer_id}")

    port = _integer_setting(raw.get("port", 8883), f"{printer_id}.port")
    stale_after = _integer_setting(
        raw.get("stale_after_seconds", 90),
        f"{printer_id}.stale_after_seconds",
    )
    full_refresh = _integer_setting(
        raw.get("full_refresh_seconds", 300),
        f"{printer_id}.full_refresh_seconds",
    )
    if not 1 <= port <= 65535:
        raise ConfigError(f"invalid MQTT port for printer {printer_id}")
    if not 15 <= stale_after <= 3600:
        raise ConfigError(f"stale_after_seconds must be 15..3600 for {printer_id}")
    if full_refresh != 0 and not 300 <= full_refresh <= 3600:
        raise ConfigError(f"full_refresh_seconds must be 0 or 300..3600 for {printer_id}")

    writable = _boolean(raw, "writable")
    camera_enabled = _boolean(raw, "camera_enabled")
    command_values = raw.get("allowed_commands", sorted(DEFAULT_COMMANDS))
    if not isinstance(command_values, list) or not all(isinstance(item, str) for item in command_values):
        raise ConfigError(f"allowed_commands must be a string list for {printer_id}")
    commands = frozenset(command_values)
    unknown_commands = commands - KNOWN_COMMANDS
    if unknown_commands:
        raise ConfigError(f"unknown commands for {printer_id}: {sorted(unknown_commands)}")
    if "start_drying" in commands and "stop_drying" not in commands:
        raise ConfigError(
            f"start_drying requires stop_drying for printer {printer_id}"
        )
    if not writable and commands != DEFAULT_COMMANDS:
        raise ConfigError(f"custom allowed_commands require writable: true for {printer_id}")

    allow_self_signed = _boolean(raw, "allow_self_signed_tls")
    ca_file = str(raw["tls_ca_file"]) if raw.get("tls_ca_file") else None
    if ca_file and not Path(ca_file).is_absolute():
        raise ConfigError(f"tls_ca_file must be absolute for {printer_id}")
    fingerprint = str(raw["tls_fingerprint_sha256"]) if raw.get("tls_fingerprint_sha256") else None
    if fingerprint:
        fingerprint = fingerprint.replace(":", "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ConfigError(f"invalid SHA-256 TLS fingerprint for {printer_id}")
    if allow_self_signed:
        if ca_file:
            raise ConfigError(
                f"{printer_id} self-signed TLS must not contain tls_ca_file"
            )
    elif not ca_file or fingerprint:
        raise ConfigError(
            f"{printer_id} verified TLS needs only tls_ca_file"
        )

    seen_ids.add(printer_id)
    seen_serials.add(serial)
    return PrinterConfig(
        id=printer_id,
        name=str(raw.get("name") or printer_id).strip()[:80],
        host=host,
        serial=serial,
        access_code=_read_secret(raw.get("access_code_file"), f"{printer_id}.access_code_file"),
        model=str(raw.get("model") or "unknown").strip()[:80],
        port=port,
        writable=writable,
        camera_enabled=camera_enabled,
        allowed_commands=commands,
        allow_self_signed_tls=allow_self_signed,
        tls_ca_file=ca_file,
        tls_fingerprint_sha256=fingerprint,
        stale_after_seconds=stale_after,
        full_refresh_seconds=full_refresh,
    )


def load_config(path: str | os.PathLike[str]) -> AppConfig:
    config_path = Path(path)
    try:
        if config_path.stat().st_size > 1024 * 1024:
            raise ConfigError("configuration file is unexpectedly large")
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except ConfigError:
        raise
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot load configuration: {exc}") from exc

    root = _as_mapping(raw, "configuration")
    printer_values = root.get("printers")
    if not isinstance(printer_values, list) or not printer_values:
        raise ConfigError("at least one printer must be configured")

    seen_ids: set[str] = set()
    seen_serials: set[str] = set()
    printers = tuple(_parse_printer(item, seen_ids, seen_serials) for item in printer_values)

    web_raw = _as_mapping(root.get("web"), "web")
    username = str(web_raw.get("username", "")).strip()
    if not username or len(username) > 128 or ":" in username:
        raise ConfigError("web.username is empty or malformed")
    origins_value = web_raw.get("allowed_origins", [])
    if not isinstance(origins_value, list) or not all(isinstance(item, str) for item in origins_value):
        raise ConfigError("web.allowed_origins must be a string list")
    origins = tuple(item.rstrip("/") for item in origins_value if item)
    if not origins:
        raise ConfigError("web.allowed_origins must contain at least one HTTPS origin")
    for origin in origins:
        parsed = urlsplit(origin)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
            raise ConfigError(f"invalid HTTPS origin: {origin}")

    audit_db = str(root.get("audit_db") or os.environ.get("BAMBU_AUDIT_DB") or "/var/lib/bambu-control/audit.sqlite3")
    password = _read_secret(web_raw.get("password_file"), "web.password_file")
    if len(password) < 12:
        raise ConfigError("web password must contain at least 12 characters")

    return AppConfig(
        printers=printers,
        web=WebConfig(
            username=username,
            password=password,
            allowed_origins=origins,
        ),
        audit_db=audit_db,
    )
