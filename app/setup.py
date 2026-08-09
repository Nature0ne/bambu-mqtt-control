from __future__ import annotations

import os
import re
import secrets
import stat
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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

DEFAULT_TLS_CA_FILE = "/app/certs/bambu-lab-ca.pem"
MAX_SETUP_BODY_BYTES = 64 * 1024


class SetupError(RuntimeError):
    """Base class for safe, non-secret setup failures."""


class SetupAlreadyConfigured(SetupError):
    pass


class SetupUnauthorized(SetupError):
    pass


class SetupValidationError(SetupError):
    pass


class SetupPersistenceError(SetupError):
    pass


class SetupRateLimited(SetupError):
    pass


class SetupWebInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    username: str = Field(min_length=1, max_length=128)
    password: SecretStr

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        canonical = value.strip()
        if (
            not canonical
            or len(canonical) > 128
            or ":" in canonical
            or "\x00" in canonical
            or "\n" in canonical
            or "\r" in canonical
        ):
            raise ValueError("invalid username")
        return canonical

    @field_validator("password", mode="before")
    @classmethod
    def validate_password(cls, value: Any) -> Any:
        if not isinstance(value, str):
            raise ValueError("invalid password")  # noqa: TRY004
        if (
            value != value.strip()
            or not 12 <= len(value) <= 512
            or "\x00" in value
            or "\n" in value
            or "\r" in value
        ):
            raise ValueError("invalid password")
        return value


class SetupPrinterInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    name: str = Field(min_length=1, max_length=80)
    model: str = Field(default="unknown", min_length=1, max_length=80)
    host: str
    port: int = Field(default=8883, ge=1, le=65535)
    serial: str
    access_code: SecretStr
    writable: bool = False
    camera_enabled: bool = False
    allowed_commands: list[str] = Field(
        default_factory=lambda: sorted(DEFAULT_COMMANDS),
        min_length=0,
        max_length=len(KNOWN_COMMANDS),
    )
    allow_self_signed_tls: bool = False
    tls_fingerprint_sha256: str | None = None
    stale_after_seconds: int = Field(default=90, ge=15, le=3600)
    full_refresh_seconds: int = Field(default=300)

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

    @field_validator("serial")
    @classmethod
    def validate_serial(cls, value: str) -> str:
        canonical = value.strip()
        if not SAFE_SERIAL.fullmatch(canonical):
            raise ValueError("invalid printer serial")
        return canonical

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        canonical = value.strip()
        if not canonical or len(canonical) > 253 or any(char.isspace() for char in canonical):
            raise ValueError("invalid printer host")
        return canonical

    @field_validator("access_code", mode="before")
    @classmethod
    def validate_access_code(cls, value: Any) -> Any:
        if not isinstance(value, str):
            raise ValueError("invalid access code")  # noqa: TRY004
        if (
            value != value.strip()
            or not value
            or len(value) > 256
            or "\x00" in value
            or "\n" in value
            or "\r" in value
        ):
            raise ValueError("invalid access code")
        return value

    @field_validator("allowed_commands")
    @classmethod
    def validate_allowed_commands(cls, value: list[str]) -> list[str]:
        commands = set(value)
        if len(commands) != len(value) or commands - KNOWN_COMMANDS:
            raise ValueError("invalid command allowlist")
        if "start_drying" in commands and "stop_drying" not in commands:
            raise ValueError("unsafe drying command allowlist")
        return sorted(commands)

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
    def validate_full_refresh(cls, value: int) -> int:
        if value != 0 and not 300 <= value <= 3600:
            raise ValueError("invalid full refresh interval")
        return value

class SetupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    bootstrap_token: SecretStr | None = None
    web: SetupWebInput
    printers: list[SetupPrinterInput] = Field(min_length=1, max_length=16)

    @field_validator("bootstrap_token", mode="before")
    @classmethod
    def validate_bootstrap_token(cls, value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > 1024 or "\x00" in value:
            raise ValueError("invalid bootstrap token")
        return value

    @model_validator(mode="after")
    def validate_unique_printers(self) -> SetupRequest:
        ids = [printer.id for printer in self.printers]
        serials = [printer.serial for printer in self.printers]
        if len(ids) != len(set(ids)) or len(serials) != len(set(serials)):
            raise ValueError("duplicate printer")
        return self


def parse_setup_request(raw: Any) -> SetupRequest:
    """Validate a setup body without ever returning Pydantic's input values."""
    try:
        return SetupRequest.model_validate(raw)
    except ValidationError as exc:
        raise SetupValidationError("Ungültige Setup-Daten") from exc


def validate_https_origin(value: str | None) -> str:
    if not value or len(value) > 512:
        raise SetupValidationError("Ungültige Setup-Daten")
    origin = value.rstrip("/")
    try:
        parsed = urlsplit(origin)
        _ = parsed.port
    except ValueError as exc:
        raise SetupValidationError("Ungültige Setup-Daten") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise SetupValidationError("Ungültige Setup-Daten")
    return origin


class SetupRateLimiter:
    def __init__(
        self,
        max_attempts: int = 5,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._clock = clock
        self._attempts: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def consume(self, key: str) -> None:
        now = self._clock()
        with self._lock:
            attempts = self._attempts.setdefault(key[:256], deque())
            while attempts and now - attempts[0] >= self.window_seconds:
                attempts.popleft()
            if len(attempts) >= self.max_attempts:
                raise SetupRateLimited("Zu viele Setup-Versuche")
            attempts.append(now)


class SetupStore:
    def __init__(
        self,
        config_path: str | os.PathLike[str],
        *,
        token_path: str | os.PathLike[str] | None = None,
        audit_db: str | None = None,
        bootstrap_token: str | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self.config_path = Path(config_path)
        if not self.config_path.is_absolute():
            raise ConfigError("BAMBU_CONFIG_FILE must be an absolute path")
        self.directory = self.config_path.parent
        self.token_path = Path(token_path) if token_path else self.directory / "bootstrap-token"
        if not self.token_path.is_absolute():
            raise ConfigError("BAMBU_BOOTSTRAP_TOKEN_FILE must be an absolute path")
        if self.token_path == self.config_path:
            raise ConfigError("bootstrap token and configuration paths must differ")
        self.audit_db = audit_db or os.environ.get("BAMBU_AUDIT_DB") or "/var/lib/bambu-control/audit.sqlite3"
        self._bootstrap_token = bootstrap_token
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self._lock = threading.Lock()

    def configuration_present(self) -> bool:
        """Treat every directory entry, including a broken symlink, as configured."""
        try:
            self.config_path.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return True

    def prepare(self) -> None:
        with self._lock:
            if self.configuration_present():
                return
            try:
                self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError as exc:
                raise SetupPersistenceError("Setup konnte nicht vorbereitet werden") from exc
            try:
                token_stat = self.token_path.lstat()
            except FileNotFoundError:
                token = self._bootstrap_token or self._token_factory()
                if not _valid_generated_token(token):
                    raise SetupPersistenceError("Setup konnte nicht vorbereitet werden")
                self._create_token_file(token)
                return
            except OSError as exc:
                raise SetupPersistenceError("Setup konnte nicht vorbereitet werden") from exc
            if not stat.S_ISREG(token_stat.st_mode) or token_stat.st_size > 4096:
                raise SetupPersistenceError("Setup konnte nicht vorbereitet werden")
            self._read_token()

    def _create_token_file(self, token: str) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.token_path, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(token + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_directory(self.token_path.parent)
        except OSError as exc:
            raise SetupPersistenceError("Setup konnte nicht vorbereitet werden") from exc

    def _read_token(self) -> str:
        try:
            token_stat = self.token_path.lstat()
            if not stat.S_ISREG(token_stat.st_mode) or token_stat.st_size > 4096:
                raise SetupPersistenceError("Setup konnte nicht vorbereitet werden")
            token = self.token_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise SetupPersistenceError("Setup konnte nicht vorbereitet werden") from exc
        if not _valid_generated_token(token):
            raise SetupPersistenceError("Setup konnte nicht vorbereitet werden")
        return token

    def verify_token(self, presented: str) -> bool:
        expected = self._read_token()
        return secrets.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))

    def complete(
        self,
        setup: SetupRequest,
        *,
        origin_header: str | None,
        header_token: str | None = None,
    ) -> AppConfig:
        with self._lock:
            if self.configuration_present():
                raise SetupAlreadyConfigured("Setup ist bereits abgeschlossen")

            body_token = setup.bootstrap_token.get_secret_value() if setup.bootstrap_token else ""
            presented = body_token or header_token or ""
            if body_token and header_token and not secrets.compare_digest(body_token, header_token):
                # Still compare a value against the stored token before rejecting the request.
                self.verify_token(body_token)
                raise SetupUnauthorized("Ungültiger Setup-Token")
            if not self.verify_token(presented):
                raise SetupUnauthorized("Ungültiger Setup-Token")

            origin = validate_https_origin(origin_header)
            config_document, secret_values = self._build_document(setup, origin)
            return self._commit(config_document, secret_values)

    def _build_document(
        self, setup: SetupRequest, origin: str
    ) -> tuple[dict[str, Any], dict[Path, str]]:
        web_secret_path = self.directory / "bambu-web-password"
        secret_values = {web_secret_path: setup.web.password.get_secret_value()}
        printers: list[dict[str, Any]] = []
        for printer in setup.printers:
            access_path = self.directory / f"bambu-{printer.id}-access-code"
            secret_values[access_path] = printer.access_code.get_secret_value()
            item: dict[str, Any] = {
                "id": printer.id,
                "name": printer.name,
                "model": printer.model,
                "host": printer.host,
                "port": printer.port,
                "serial": printer.serial,
                "access_code_file": str(access_path),
                "allow_self_signed_tls": printer.allow_self_signed_tls,
                "writable": printer.writable,
                "camera_enabled": printer.camera_enabled,
                "allowed_commands": printer.allowed_commands
                if printer.writable
                else sorted(DEFAULT_COMMANDS),
                "stale_after_seconds": printer.stale_after_seconds,
                "full_refresh_seconds": printer.full_refresh_seconds,
            }
            if not printer.allow_self_signed_tls:
                item["tls_ca_file"] = DEFAULT_TLS_CA_FILE
            if printer.tls_fingerprint_sha256:
                item["tls_fingerprint_sha256"] = printer.tls_fingerprint_sha256
            printers.append(item)
        return (
            {
                "audit_db": self.audit_db,
                "web": {
                    "username": setup.web.username,
                    "password_file": str(web_secret_path),
                    "allowed_origins": [origin],
                },
                "printers": printers,
            },
            secret_values,
        )

    def _commit(self, document: dict[str, Any], secret_values: dict[Path, str]) -> AppConfig:
        staged_config: Path | None = None
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            for path, value in secret_values.items():
                _atomic_write(path, value + "\n")

            yaml_text = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
            staged_config = _stage_private_file(self.config_path, yaml_text)
            config = load_config(staged_config)
            try:
                # A hard-link commit is atomic and, unlike replace(), can never
                # overwrite a configuration created by another local process.
                os.link(staged_config, self.config_path, follow_symlinks=False)
            except FileExistsError:
                raise SetupAlreadyConfigured("Setup ist bereits abgeschlossen")
            try:
                staged_config.unlink()
            except OSError:
                pass
            staged_config = None
            os.chmod(self.config_path, 0o600)
            _fsync_directory(self.directory)
        except SetupAlreadyConfigured:
            raise
        except ConfigError as exc:
            raise SetupValidationError("Ungültige Setup-Daten") from exc
        except OSError as exc:
            raise SetupPersistenceError("Setup konnte nicht gespeichert werden") from exc
        finally:
            if staged_config is not None:
                try:
                    staged_config.unlink(missing_ok=True)
                except OSError:
                    pass

        try:
            self.token_path.unlink(missing_ok=True)
            _fsync_directory(self.token_path.parent)
        except OSError:
            # The committed configuration is the authoritative one-time lock.
            pass
        return config


def _valid_generated_token(value: str) -> bool:
    return 24 <= len(value) <= 1024 and "\x00" not in value and "\n" not in value and "\r" not in value


def _stage_private_file(destination: Path, content: str) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return temporary


def _atomic_write(destination: Path, content: str) -> None:
    temporary = _stage_private_file(destination, content)
    try:
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
        _fsync_directory(destination.parent)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
