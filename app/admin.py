from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from .config import (
    DEFAULT_COMMANDS,
    KNOWN_COMMANDS,
    SAFE_ID,
    SAFE_SERIAL,
    AppConfig,
    ConfigError,
    load_config,
)
from .setup import DEFAULT_TLS_CA_FILE, _fsync_directory, _stage_private_file

MAX_ADMIN_BODY_BYTES = 64 * 1024
MAX_ADMIN_PRINTERS = 16
MAX_CONFIG_BYTES = 1024 * 1024
MANAGED_SECRET_NAME = re.compile(
    r"^bambu-(?:web-password|[a-z0-9][a-z0-9_-]{0,31}-access-code)"
    r"(?:-v1-[0-9a-f]{16})?$"
)


class AdminConfigError(RuntimeError):
    """Base class for deliberately non-secret administrative errors."""


class AdminConfigValidationError(AdminConfigError):
    pass


class AdminConfigConflict(AdminConfigError):
    pass


class AdminConfigPersistenceError(AdminConfigError):
    pass


def _safe_secret(value: Any, *, minimum: int, maximum: int) -> Any:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid secret")
    if (
        value != value.strip()
        or not minimum <= len(value) <= maximum
        or "\x00" in value
        or "\n" in value
        or "\r" in value
    ):
        raise ValueError("invalid secret")
    return value


def _https_origin(value: str) -> str:
    # Keep validation in one place with the runtime loader as the final gate.
    canonical = value.rstrip("/")
    if not canonical or len(canonical) > 512:
        raise ValueError("invalid HTTPS origin")
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(canonical)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("invalid HTTPS origin") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid HTTPS origin")
    return canonical


class AdminWebInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    username: str = Field(min_length=1, max_length=128)
    allowed_origins: list[str] = Field(min_length=1, max_length=16)
    password: SecretStr | None = None

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        canonical = value.strip()
        if (
            not canonical
            or ":" in canonical
            or "\x00" in canonical
            or "\n" in canonical
            or "\r" in canonical
        ):
            raise ValueError("invalid username")
        return canonical

    @field_validator("allowed_origins")
    @classmethod
    def validate_origins(cls, values: list[str]) -> list[str]:
        origins = [_https_origin(value) for value in values]
        if len(origins) != len(set(origins)):
            raise ValueError("duplicate origin")
        return origins

    @field_validator("password", mode="before")
    @classmethod
    def validate_password(cls, value: Any) -> Any:
        return _safe_secret(value, minimum=12, maximum=512)


class AdminPrinterInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    name: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=80)
    host: str
    port: int = Field(ge=1, le=65535)
    serial: str
    writable: bool
    camera_enabled: bool
    allowed_commands: list[str] = Field(max_length=len(KNOWN_COMMANDS))
    allow_self_signed_tls: bool
    tls_ca_file: str | None = None
    tls_fingerprint_sha256: str | None = None
    stale_after_seconds: int = Field(ge=15, le=3600)
    full_refresh_seconds: int
    access_code: SecretStr | None = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        canonical = value.strip().lower()
        if not SAFE_ID.fullmatch(canonical):
            raise ValueError("invalid printer id")
        return canonical

    @field_validator("name", "model", mode="before")
    @classmethod
    def strip_label(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        canonical = value.strip()
        if not canonical or len(canonical) > 253 or any(char.isspace() for char in canonical):
            raise ValueError("invalid host")
        return canonical

    @field_validator("serial")
    @classmethod
    def validate_serial(cls, value: str) -> str:
        canonical = value.strip()
        if not SAFE_SERIAL.fullmatch(canonical):
            raise ValueError("invalid serial")
        return canonical

    @field_validator("allowed_commands")
    @classmethod
    def validate_commands(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)) or set(values) - KNOWN_COMMANDS:
            raise ValueError("invalid command allowlist")
        if "start_drying" in values and "stop_drying" not in values:
            raise ValueError("unsafe drying command allowlist")
        return sorted(values)

    @field_validator("tls_ca_file")
    @classmethod
    def validate_ca_file(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if (
            not value
            or len(value) > 512
            or not Path(value).is_absolute()
            or "\x00" in value
            or "\n" in value
            or "\r" in value
        ):
            raise ValueError("invalid CA file")
        return value

    @field_validator("tls_fingerprint_sha256")
    @classmethod
    def validate_fingerprint(cls, value: str | None) -> str | None:
        if value is None or not value:
            return None
        canonical = value.strip().replace(":", "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", canonical):
            raise ValueError("invalid certificate fingerprint")
        return canonical

    @field_validator("full_refresh_seconds")
    @classmethod
    def validate_refresh(cls, value: int) -> int:
        if value != 0 and not 300 <= value <= 3600:
            raise ValueError("invalid refresh interval")
        return value

    @field_validator("access_code", mode="before")
    @classmethod
    def validate_access_code(cls, value: Any) -> Any:
        return _safe_secret(value, minimum=1, maximum=256)

    @model_validator(mode="after")
    def validate_write_permissions(self) -> AdminPrinterInput:
        if not self.writable and self.allowed_commands not in (
            [],
            sorted(DEFAULT_COMMANDS),
        ):
            raise ValueError("commands require writable printer")
        if self.allow_self_signed_tls and not self.tls_fingerprint_sha256:
            raise ValueError("self-signed TLS requires a certificate fingerprint")
        if self.allow_self_signed_tls and self.tls_ca_file is not None:
            raise ValueError("self-signed TLS must not contain a CA file")
        if not self.allow_self_signed_tls and self.tls_fingerprint_sha256:
            raise ValueError("verified TLS must not contain a leaf fingerprint")
        return self


class AdminConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    current_password: SecretStr
    web: AdminWebInput
    printers: list[AdminPrinterInput] = Field(
        min_length=1,
        max_length=MAX_ADMIN_PRINTERS,
    )

    @field_validator("current_password", mode="before")
    @classmethod
    def validate_current_password(cls, value: Any) -> Any:
        return _safe_secret(value, minimum=1, maximum=1024)

    @model_validator(mode="after")
    def validate_unique_printers(self) -> AdminConfigUpdate:
        ids = [printer.id for printer in self.printers]
        serials = [printer.serial for printer in self.printers]
        if len(ids) != len(set(ids)) or len(serials) != len(set(serials)):
            raise ValueError("duplicate printer")
        return self


def parse_admin_config_update(raw: Any) -> AdminConfigUpdate:
    try:
        return AdminConfigUpdate.model_validate(raw)
    except ValidationError as exc:
        # Pydantic errors can contain the rejected input, including secrets.
        raise AdminConfigValidationError("Ungültige Konfigurationsdaten") from exc


def public_config(config: AppConfig) -> dict[str, Any]:
    return {
        "version": 1,
        "restart_required": False,
        "web": {
            "username": config.web.username,
            "allowed_origins": list(config.web.allowed_origins),
            "password_configured": True,
        },
        "printers": [
            {
                "id": printer.id,
                "name": printer.name,
                "model": printer.model,
                "host": printer.host,
                "port": printer.port,
                "serial": printer.serial,
                "writable": printer.writable,
                "camera_enabled": printer.camera_enabled,
                "allowed_commands": (
                    sorted(printer.allowed_commands) if printer.writable else []
                ),
                "allow_self_signed_tls": printer.allow_self_signed_tls,
                "tls_ca_file": printer.tls_ca_file,
                "tls_fingerprint_sha256": printer.tls_fingerprint_sha256,
                "stale_after_seconds": printer.stale_after_seconds,
                "full_refresh_seconds": printer.full_refresh_seconds,
                "access_code_configured": True,
            }
            for printer in config.printers
        ],
    }


def _private_file_exclusive(directory: Path, prefix: str, value: str) -> Path:
    for _attempt in range(16):
        path = directory / f"{prefix}-{secrets.token_hex(8)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(value + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_directory(directory)
            return path
        except BaseException:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    raise OSError("could not allocate a private secret file")


def _regular_private_config(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AdminConfigPersistenceError(
            "Konfiguration kann nicht verwaltet werden"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_CONFIG_BYTES:
            raise AdminConfigPersistenceError("Konfiguration kann nicht verwaltet werden")
        content = bytearray()
        while len(content) <= MAX_CONFIG_BYTES:
            chunk = os.read(descriptor, min(64 * 1024, MAX_CONFIG_BYTES + 1 - len(content)))
            if not chunk:
                return bytes(content)
            content.extend(chunk)
        raise AdminConfigPersistenceError("Konfiguration kann nicht verwaltet werden")
    except AdminConfigPersistenceError:
        raise
    except OSError as exc:
        raise AdminConfigPersistenceError(
            "Konfiguration kann nicht verwaltet werden"
        ) from exc
    finally:
        os.close(descriptor)


def _managed_secret(path_value: Any, directory: Path) -> Path | None:
    if not isinstance(path_value, str):
        return None
    path = Path(path_value)
    if path.parent != directory or not MANAGED_SECRET_NAME.fullmatch(path.name):
        return None
    return path


def _unlink_managed_secrets(directory: Path, paths: tuple[Path, ...]) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        directory_fd = os.open(directory, flags)
    except OSError:
        return
    try:
        for path in paths:
            if path.parent != directory or not MANAGED_SECRET_NAME.fullmatch(path.name):
                continue
            try:
                metadata = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode):
                    continue
                os.unlink(path.name, dir_fd=directory_fd)
            except OSError:
                continue
        try:
            _fsync_directory(directory)
        except OSError:
            pass
    finally:
        os.close(directory_fd)


@dataclass
class PreparedConfigUpdate:
    store: AdminConfigStore
    config: AppConfig
    staged_config: Path
    baseline: bytes = field(repr=False)
    baseline_digest: bytes = field(repr=False)
    created_secrets: tuple[Path, ...] = field(repr=False)
    obsolete_secrets: tuple[Path, ...] = field(repr=False)
    _backup: Path | None = field(default=None, init=False, repr=False)
    _committed: bool = field(default=False, init=False, repr=False)

    def commit(self) -> None:
        with self.store._lock:
            current = _regular_private_config(self.store.config_path)
            if not secrets.compare_digest(
                hashlib.sha256(current).digest(),
                self.baseline_digest,
            ):
                raise AdminConfigConflict("Konfiguration wurde zwischenzeitlich geändert")
            try:
                self._backup = _stage_private_file(
                    self.store.config_path,
                    self.baseline.decode("utf-8"),
                )
                os.replace(self.staged_config, self.store.config_path)
                self._committed = True
                # _stage_private_file already created the replacement as 0600.
                # chmod is defense in depth and must not turn a successful
                # atomic commit into an ambiguous partial failure.
                try:
                    os.chmod(self.store.config_path, 0o600)
                except OSError:
                    pass
                _fsync_directory(self.store.directory)
            except (OSError, UnicodeError) as exc:
                if self._committed and self._backup is not None:
                    try:
                        os.replace(self._backup, self.store.config_path)
                        self._backup = None
                        self._committed = False
                        try:
                            _fsync_directory(self.store.directory)
                        except OSError:
                            pass
                    except OSError:
                        # Leave the committed flag set: abort() must not remove
                        # secret files referenced by the on-disk replacement.
                        pass
                raise AdminConfigPersistenceError(
                    "Konfiguration konnte nicht gespeichert werden"
                ) from exc

    def rollback(self) -> None:
        with self.store._lock:
            if self._committed and self._backup is not None:
                try:
                    os.replace(self._backup, self.store.config_path)
                except OSError as exc:
                    raise AdminConfigPersistenceError(
                        "Konfigurations-Rollback fehlgeschlagen"
                    ) from exc
                self._backup = None
                self._committed = False
                try:
                    os.chmod(self.store.config_path, 0o600)
                    _fsync_directory(self.store.directory)
                except OSError:
                    pass
        self.abort()

    def finalize(self) -> None:
        if self._backup is not None:
            try:
                self._backup.unlink(missing_ok=True)
            except OSError:
                pass
            self._backup = None
        _unlink_managed_secrets(self.store.directory, self.obsolete_secrets)

    def abort(self) -> None:
        if not self._committed:
            try:
                self.staged_config.unlink(missing_ok=True)
            except OSError:
                pass
            for path in self.created_secrets:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        if self._backup is not None and not self._committed:
            try:
                self._backup.unlink(missing_ok=True)
            except OSError:
                pass
            self._backup = None


class AdminConfigStore:
    def __init__(self, config_path: str | os.PathLike[str]) -> None:
        self.config_path = Path(config_path)
        if not self.config_path.is_absolute():
            raise ConfigError("BAMBU_CONFIG_FILE must be an absolute path")
        self.directory = self.config_path.parent
        self._lock = threading.Lock()

    def prepare(
        self,
        update: AdminConfigUpdate,
        current: AppConfig,
    ) -> PreparedConfigUpdate:
        baseline = _regular_private_config(self.config_path)
        baseline_copy: Path | None = None
        try:
            baseline_copy = _stage_private_file(
                self.config_path,
                baseline.decode("utf-8"),
            )
            disk_config = load_config(baseline_copy)
            raw = yaml.safe_load(baseline)
        except (ConfigError, UnicodeError, yaml.YAMLError) as exc:
            raise AdminConfigPersistenceError(
                "Konfiguration kann nicht verwaltet werden"
            ) from exc
        except OSError as exc:
            raise AdminConfigPersistenceError(
                "Konfiguration kann nicht verwaltet werden"
            ) from exc
        finally:
            if baseline_copy is not None:
                try:
                    baseline_copy.unlink(missing_ok=True)
                except OSError:
                    pass
        if disk_config != current or not isinstance(raw, dict):
            raise AdminConfigConflict("Konfiguration wurde zwischenzeitlich geändert")

        web_raw = raw.get("web")
        printer_values = raw.get("printers")
        if not isinstance(web_raw, dict) or not isinstance(printer_values, list):
            raise AdminConfigPersistenceError("Konfiguration kann nicht verwaltet werden")
        current_raw_printers = {
            str(item.get("id", "")).strip().lower(): item
            for item in printer_values
            if isinstance(item, dict)
        }
        password_file = web_raw.get("password_file")
        if not isinstance(password_file, str) or not Path(password_file).is_absolute():
            raise AdminConfigPersistenceError("Konfiguration kann nicht verwaltet werden")

        created: list[Path] = []
        staged: Path | None = None
        try:
            if update.web.password is not None:
                password_path = _private_file_exclusive(
                    self.directory,
                    "bambu-web-password-v1",
                    update.web.password.get_secret_value(),
                )
                created.append(password_path)
                password_file = str(password_path)

            document_printers: list[dict[str, Any]] = []
            current_by_id = {printer.id: printer for printer in current.printers}
            for printer in update.printers:
                existing = current_by_id.get(printer.id)
                existing_raw = current_raw_printers.get(printer.id)
                same_identity = (
                    existing is not None
                    and existing.serial == printer.serial
                    and isinstance(existing_raw, dict)
                )
                if printer.access_code is None:
                    if not same_identity:
                        raise AdminConfigValidationError(
                            "Für neue Drucker ist ein Zugangscode erforderlich"
                        )
                    access_file = existing_raw.get("access_code_file")
                    if not isinstance(access_file, str) or not Path(access_file).is_absolute():
                        raise AdminConfigPersistenceError(
                            "Konfiguration kann nicht verwaltet werden"
                        )
                else:
                    access_path = _private_file_exclusive(
                        self.directory,
                        f"bambu-{printer.id}-access-code-v1",
                        printer.access_code.get_secret_value(),
                    )
                    created.append(access_path)
                    access_file = str(access_path)

                if (
                    existing is not None
                    and printer.tls_ca_file != existing.tls_ca_file
                    and not (
                        printer.allow_self_signed_tls
                        and printer.tls_ca_file is None
                    )
                ):
                    raise AdminConfigValidationError("tls_ca_file ist schreibgeschützt")
                ca_file = existing.tls_ca_file if existing is not None else printer.tls_ca_file
                if printer.allow_self_signed_tls:
                    # The explicit opt-in selects the fingerprint-backed insecure
                    # transport path. Keeping the previous CA would make the
                    # switch ineffective while still disabling camera TLS.
                    ca_file = None
                elif ca_file is None:
                    ca_file = DEFAULT_TLS_CA_FILE
                if existing is None and ca_file not in {None, DEFAULT_TLS_CA_FILE}:
                    raise AdminConfigValidationError("tls_ca_file ist schreibgeschützt")

                item: dict[str, Any] = {
                    "id": printer.id,
                    "name": printer.name,
                    "model": printer.model,
                    "host": printer.host,
                    "port": printer.port,
                    "serial": printer.serial,
                    "access_code_file": access_file,
                    "writable": printer.writable,
                    "camera_enabled": printer.camera_enabled,
                    "allowed_commands": (
                        printer.allowed_commands
                        if printer.writable
                        else sorted(DEFAULT_COMMANDS)
                    ),
                    "allow_self_signed_tls": printer.allow_self_signed_tls,
                    "stale_after_seconds": printer.stale_after_seconds,
                    "full_refresh_seconds": printer.full_refresh_seconds,
                }
                if ca_file is not None:
                    item["tls_ca_file"] = ca_file
                if (
                    printer.allow_self_signed_tls
                    and printer.tls_fingerprint_sha256 is not None
                ):
                    item["tls_fingerprint_sha256"] = printer.tls_fingerprint_sha256
                document_printers.append(item)

            document = {
                "audit_db": current.audit_db,
                "web": {
                    "username": update.web.username,
                    "password_file": password_file,
                    "allowed_origins": update.web.allowed_origins,
                },
                "printers": document_printers,
            }
            old_secret_paths = {
                path
                for value in (
                    web_raw.get("password_file"),
                    *(
                        item.get("access_code_file")
                        for item in printer_values
                        if isinstance(item, dict)
                    ),
                )
                if (path := _managed_secret(value, self.directory)) is not None
            }
            new_secret_paths = {
                Path(document["web"]["password_file"]),
                *(Path(item["access_code_file"]) for item in document_printers),
            }
            staged = _stage_private_file(
                self.config_path,
                yaml.safe_dump(document, sort_keys=False, allow_unicode=True),
            )
            candidate = load_config(staged)
            return PreparedConfigUpdate(
                store=self,
                config=candidate,
                staged_config=staged,
                baseline=baseline,
                baseline_digest=hashlib.sha256(baseline).digest(),
                created_secrets=tuple(created),
                obsolete_secrets=tuple(sorted(old_secret_paths - new_secret_paths)),
            )
        except AdminConfigError:
            raise
        except ConfigError as exc:
            raise AdminConfigValidationError("Ungültige Konfigurationsdaten") from exc
        except OSError as exc:
            raise AdminConfigPersistenceError(
                "Konfiguration konnte nicht vorbereitet werden"
            ) from exc
        finally:
            # Ownership transfers to PreparedConfigUpdate only on return.
            if staged is not None and "candidate" not in locals():
                try:
                    staged.unlink(missing_ok=True)
                except OSError:
                    pass
            if "candidate" not in locals():
                for path in created:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
