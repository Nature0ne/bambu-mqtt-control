from __future__ import annotations

import argparse
import io
import json
import os
import re
import sqlite3
import stat
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

import yaml

from app.config import load_config
from app.version import build_version

BACKUP_FORMAT = 1
MAX_CONFIG_FILE_BYTES = 1024 * 1024
MAX_CONFIG_TOTAL_BYTES = 16 * 1024 * 1024
MAX_AUDIT_BYTES = 2 * 1024 * 1024 * 1024
SAFE_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MANAGED_SECRET_NAME = re.compile(
    r"^bambu-(?:web-password|[a-z0-9][a-z0-9_-]{0,31}-access-code)"
    r"(?:-v1-[0-9a-f]{16})?$"
)


class BackupError(RuntimeError):
    """Raised when a backup or restore cannot be completed safely."""


class BackupRollbackIncomplete(BackupError):
    """Raised when active files could not all be restored after a failed commit."""


def _paths() -> tuple[Path, Path, Path]:
    config_file = Path(os.environ.get("BAMBU_CONFIG_FILE", "/config/printers.yml"))
    audit_file = Path(
        os.environ.get("BAMBU_AUDIT_DB", "/var/lib/bambu-control/audit.sqlite3")
    )
    if not config_file.is_absolute() or not audit_file.is_absolute():
        raise BackupError("backup paths must be absolute")
    return config_file, config_file.parent, audit_file


def _read_regular_file(path: Path, *, maximum: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BackupError(f"cannot safely read {path.name}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise BackupError(f"unsafe backup input: {path.name}")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            content = source.read(maximum + 1)
        if len(content) > maximum:
            raise BackupError(f"backup input is too large: {path.name}")
        return content
    finally:
        os.close(descriptor)


def _referenced_secret_names(config_content: bytes, config_directory: Path) -> set[str]:
    try:
        document = yaml.safe_load(config_content.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise BackupError("configuration contains invalid secret references") from exc
    if not isinstance(document, dict):
        raise BackupError("configuration contains invalid secret references")
    web = document.get("web")
    printers = document.get("printers")
    if not isinstance(web, dict) or not isinstance(printers, list):
        raise BackupError("configuration contains invalid secret references")
    values: list[Any] = [web.get("password_file")]
    values.extend(
        printer.get("access_code_file")
        for printer in printers
        if isinstance(printer, dict)
    )
    names: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise BackupError("configuration contains an invalid secret reference")
        path = Path(value)
        if path.parent != config_directory or not SAFE_FILE_NAME.fullmatch(path.name):
            raise BackupError("configuration refers to a secret outside its directory")
        names.add(path.name)
    return names


def _tar_bytes(archive: tarfile.TarFile, name: str, content: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    info.mode = 0o600
    info.mtime = 0
    archive.addfile(info, io.BytesIO(content))


def _snapshot_database(source_path: Path) -> Path:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_descriptor = os.open(source_path, flags)
    except OSError as exc:
        raise BackupError("cannot safely open the audit database") from exc
    try:
        source_metadata = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_metadata.st_mode):
            raise BackupError("unsafe audit database input")
        try:
            path_metadata = source_path.stat(follow_symlinks=False)
        except OSError as exc:
            raise BackupError("cannot safely inspect the audit database") from exc
        if (
            path_metadata.st_dev != source_metadata.st_dev
            or path_metadata.st_ino != source_metadata.st_ino
        ):
            raise BackupError("audit database changed while it was opened")

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".bambu-audit-",
            suffix=".sqlite3",
            dir=source_path.parent,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            # SQLite needs the original pathname so its WAL/rollback journal is
            # included. The held no-follow descriptor and inode checks prevent
            # a symlink or path substitution from being accepted silently.
            source: sqlite3.Connection | None = None
            destination: sqlite3.Connection | None = None
            try:
                source = sqlite3.connect(
                    f"file:{source_path}?mode=ro", uri=True, timeout=5
                )
                destination = sqlite3.connect(temporary, timeout=5)
                current_metadata = source_path.stat(follow_symlinks=False)
                if (
                    current_metadata.st_dev != source_metadata.st_dev
                    or current_metadata.st_ino != source_metadata.st_ino
                ):
                    raise BackupError("audit database changed while it was opened")
                source.backup(destination)
                result = destination.execute("PRAGMA quick_check").fetchone()
                if not result or result[0] != "ok":
                    raise BackupError(
                        "audit database snapshot failed its integrity check"
                    )
                current_metadata = source_path.stat(follow_symlinks=False)
                if (
                    current_metadata.st_dev != source_metadata.st_dev
                    or current_metadata.st_ino != source_metadata.st_ino
                ):
                    raise BackupError("audit database changed during its snapshot")
                if temporary.stat().st_size > MAX_AUDIT_BYTES:
                    raise BackupError("audit database snapshot exceeds the safety limit")
            finally:
                if destination is not None:
                    destination.close()
                if source is not None:
                    source.close()
        except (OSError, sqlite3.Error, BackupError) as exc:
            temporary.unlink(missing_ok=True)
            if isinstance(exc, BackupError):
                raise
            raise BackupError("cannot snapshot the audit database") from exc
        return temporary
    finally:
        os.close(source_descriptor)


def create_backup(output: BinaryIO) -> None:
    config_file, config_directory, audit_file = _paths()
    if not config_file.is_file():
        raise BackupError("configuration is not complete")

    config_content = _read_regular_file(
        config_file,
        maximum=MAX_CONFIG_FILE_BYTES,
    )
    referenced_secrets = _referenced_secret_names(config_content, config_directory)
    config_files: dict[str, bytes] = {config_file.name: config_content}
    total = len(config_content)
    if total > MAX_CONFIG_TOTAL_BYTES:
        raise BackupError("configuration backup exceeds the safety limit")
    try:
        entries = sorted(config_directory.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise BackupError("cannot inspect the configuration directory") from exc
    for path in entries:
        if not SAFE_FILE_NAME.fullmatch(path.name):
            continue
        if path.name == config_file.name:
            # Archive exactly the byte snapshot used to resolve secret
            # references. Reading the main file twice could mix two atomic
            # Manage revisions into one non-restorable backup.
            continue
        if (
            MANAGED_SECRET_NAME.fullmatch(path.name)
            and path.name not in referenced_secrets
        ):
            continue
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise BackupError(f"cannot inspect {path.name}") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise BackupError(f"refusing non-regular configuration entry: {path.name}")
        content = _read_regular_file(path, maximum=MAX_CONFIG_FILE_BYTES)
        total += len(content)
        if total > MAX_CONFIG_TOTAL_BYTES:
            raise BackupError("configuration backup exceeds the safety limit")
        config_files[path.name] = content
    if config_file.name not in config_files:
        raise BackupError("main configuration file is missing from the backup set")

    snapshot = _snapshot_database(audit_file)
    try:
        manifest = {
            "format": BACKUP_FORMAT,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "build_version": build_version(),
            "config_file": config_file.name,
            "config_files": sorted(config_files),
        }
        with tarfile.open(fileobj=output, mode="w|gz", compresslevel=6) as archive:
            _tar_bytes(
                archive,
                "manifest.json",
                json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
            )
            for name, content in config_files.items():
                _tar_bytes(archive, f"config/{name}", content)
            info = archive.gettarinfo(str(snapshot), arcname="data/audit.sqlite3")
            info.mode = 0o600
            info.mtime = 0
            with snapshot.open("rb") as database:
                archive.addfile(info, database)
    finally:
        snapshot.unlink(missing_ok=True)


def _copy_limited(source: BinaryIO, destination: BinaryIO, maximum: int) -> None:
    remaining = maximum + 1
    while remaining:
        chunk = source.read(min(1024 * 1024, remaining))
        if not chunk:
            return
        destination.write(chunk)
        remaining -= len(chunk)
    raise BackupError("backup entry exceeds its safety limit")


def _validate_candidate(
    config_content: bytes,
    config_files: dict[str, bytes],
    config_directory: Path,
    expected_audit_path: Path,
    audit_candidate: Path,
) -> None:
    try:
        document = yaml.safe_load(config_content.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise BackupError("backup contains invalid configuration") from exc
    if not isinstance(document, dict):
        raise BackupError("backup contains invalid configuration")
    configured_audit = document.get(
        "audit_db", "/var/lib/bambu-control/audit.sqlite3"
    )
    if (
        not isinstance(configured_audit, str)
        or not Path(configured_audit).is_absolute()
        or os.path.abspath(configured_audit) != os.path.abspath(expected_audit_path)
    ):
        raise BackupError("backup audit database target does not match this installation")

    expected_columns = [
        "id",
        "created_at",
        "actor",
        "printer_id",
        "command",
        "sequence_id",
        "params_json",
        "result",
        "detail",
    ]
    try:
        connection = sqlite3.connect(
            f"file:{audit_candidate}?mode=ro", uri=True, timeout=5
        )
        try:
            columns = [
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(command_audit)"
                ).fetchall()
            ]
            connection.execute(
                "SELECT id, created_at, actor, printer_id, command, "
                "sequence_id, params_json, result, detail "
                "FROM command_audit LIMIT 1"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise BackupError("backup audit database has an incompatible schema") from exc
    if columns != expected_columns:
        raise BackupError("backup audit database has an incompatible schema")

    with tempfile.TemporaryDirectory(prefix="bambu-config-check-") as temporary_name:
        temporary = Path(temporary_name)
        for name, content in config_files.items():
            (temporary / name).write_bytes(content)

        web = document.get("web")
        printers = document.get("printers")
        if not isinstance(web, dict) or not isinstance(printers, list):
            raise BackupError("backup contains invalid configuration")
        secret_fields: list[tuple[dict[str, Any], str]] = [(web, "password_file")]
        secret_fields.extend(
            (printer, "access_code_file")
            for printer in printers
            if isinstance(printer, dict)
        )
        for section, field in secret_fields:
            raw_path = section.get(field)
            if not isinstance(raw_path, str):
                raise BackupError("backup contains an invalid secret reference")
            path = Path(raw_path)
            if path.parent != config_directory or path.name not in config_files:
                raise BackupError("backup refers to a secret outside its configuration set")
            section[field] = str(temporary / path.name)
        validation_path = temporary / "validation.yml"
        validation_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        try:
            load_config(validation_path)
        except Exception as exc:  # ConfigError contains no submitted secret values.
            raise BackupError("backup configuration failed validation") from exc


def _stage_bytes(destination: Path, content: bytes) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".restore-stage",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        temporary.chmod(0o600)
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _snapshot_existing(destination: Path, *, maximum: int) -> Path | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_descriptor = os.open(destination, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BackupError(f"cannot safely snapshot {destination.name}") from exc

    rollback: Path | None = None
    try:
        metadata = os.fstat(source_descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise BackupError(f"unsafe restore target: {destination.name}")
        rollback_descriptor, rollback_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".restore-rollback",
            dir=destination.parent,
        )
        rollback = Path(rollback_name)
        try:
            source_file = os.fdopen(source_descriptor, "rb")
            source_descriptor = -1
            try:
                rollback_file = os.fdopen(rollback_descriptor, "wb")
                rollback_descriptor = -1
            except BaseException:
                source_file.close()
                raise
            with source_file, rollback_file:
                _copy_limited(source_file, rollback_file, maximum)
                rollback_file.flush()
                os.fsync(rollback_file.fileno())
            rollback.chmod(0o600)
            return rollback
        except BaseException:
            rollback.unlink(missing_ok=True)
            raise
        finally:
            if rollback_descriptor >= 0:
                os.close(rollback_descriptor)
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise BackupError(f"unsafe restore directory: {path.name}")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_safe_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=False)
        except FileExistsError:
            metadata = path.lstat()
        else:
            metadata = path.lstat()
    except OSError as exc:
        raise BackupError(f"cannot inspect restore directory: {path.name}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise BackupError(f"unsafe restore directory: {path.name}")
    try:
        _fsync_directory(path)
    except OSError as exc:
        raise BackupError(f"cannot access restore directory: {path.name}") from exc


@dataclass
class _RestoreEntry:
    destination: Path
    staged: Path
    rollback: Path | None
    installed: bool = False


def _cleanup_restore_entries(
    entries: list[_RestoreEntry], *, preserve_rollbacks: bool = False
) -> None:
    for entry in entries:
        try:
            entry.staged.unlink(missing_ok=True)
        except OSError:
            pass
        if entry.rollback is not None and not preserve_rollbacks:
            try:
                entry.rollback.unlink(missing_ok=True)
            except OSError:
                pass
            else:
                entry.rollback = None


def _rollback_restore(entries: list[_RestoreEntry]) -> None:
    failed = False
    touched_parents: set[Path] = set()
    for entry in reversed(entries):
        if not entry.installed:
            continue
        touched_parents.add(entry.destination.parent)
        try:
            if entry.rollback is not None:
                os.replace(entry.rollback, entry.destination)
                entry.rollback = None
            else:
                metadata = entry.destination.lstat()
                if not stat.S_ISREG(metadata.st_mode):
                    raise OSError("restore target changed type during rollback")
                entry.destination.unlink()
            entry.installed = False
        except OSError:
            failed = True
    for parent in touched_parents:
        try:
            _fsync_directory(parent)
        except (OSError, BackupError):
            failed = True
    if failed:
        raise BackupRollbackIncomplete(
            "backup restore failed and rollback was incomplete"
        )


def _prune_unreferenced_managed_secrets(
    directory: Path, referenced_names: set[str]
) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_descriptor = os.open(directory, flags)
    except OSError:
        return
    changed = False
    try:
        try:
            names = os.listdir(directory_descriptor)
        except OSError:
            return
        for name in names:
            if name in referenced_names or not MANAGED_SECRET_NAME.fullmatch(name):
                continue
            try:
                metadata = os.stat(
                    name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if not stat.S_ISREG(metadata.st_mode):
                    continue
                os.unlink(name, dir_fd=directory_descriptor)
                changed = True
            except OSError:
                continue
        if changed:
            try:
                os.fsync(directory_descriptor)
            except OSError:
                pass
    finally:
        os.close(directory_descriptor)


def restore_backup(source: BinaryIO) -> None:
    config_file, config_directory, audit_file = _paths()
    _ensure_safe_directory(config_directory)
    _ensure_safe_directory(audit_file.parent)

    manifest: dict[str, Any] | None = None
    config_files: dict[str, bytes] = {}
    config_total = 0
    audit_temporary: Path | None = None
    restore_entries: list[_RestoreEntry] = []
    preserve_rollbacks = False
    seen: set[str] = set()
    try:
        try:
            archive = tarfile.open(fileobj=source, mode="r|gz")
        except (OSError, tarfile.TarError) as exc:
            raise BackupError("cannot read backup archive") from exc
        with archive:
            for member in archive:
                if member.name in seen or not member.isfile():
                    raise BackupError("backup contains a duplicate or unsafe entry")
                seen.add(member.name)
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise BackupError("backup entry cannot be read")
                if member.name == "manifest.json":
                    if member.size > 64 * 1024:
                        raise BackupError("backup manifest is too large")
                    try:
                        manifest = json.loads(extracted.read(64 * 1024 + 1))
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        raise BackupError("backup manifest is invalid") from exc
                    continue
                if member.name.startswith("config/"):
                    name = member.name.removeprefix("config/")
                    if "/" in name or not SAFE_FILE_NAME.fullmatch(name):
                        raise BackupError("backup contains an unsafe configuration path")
                    if member.size > MAX_CONFIG_FILE_BYTES:
                        raise BackupError("backup configuration entry is too large")
                    content = extracted.read(MAX_CONFIG_FILE_BYTES + 1)
                    config_total += len(content)
                    if len(content) > MAX_CONFIG_FILE_BYTES or config_total > MAX_CONFIG_TOTAL_BYTES:
                        raise BackupError("backup configuration exceeds the safety limit")
                    config_files[name] = content
                    continue
                if member.name == "data/audit.sqlite3":
                    if member.size > MAX_AUDIT_BYTES:
                        raise BackupError("audit backup is too large")
                    descriptor, temporary_name = tempfile.mkstemp(
                        prefix=".audit.", suffix=".restore", dir=audit_file.parent
                    )
                    audit_temporary = Path(temporary_name)
                    with os.fdopen(descriptor, "wb") as target:
                        _copy_limited(extracted, target, MAX_AUDIT_BYTES)
                        target.flush()
                        os.fsync(target.fileno())
                    audit_temporary.chmod(0o600)
                    continue
                raise BackupError("backup contains an unexpected entry")

        if not isinstance(manifest, dict) or manifest.get("format") != BACKUP_FORMAT:
            raise BackupError("unsupported backup format")
        configured_name = manifest.get("config_file")
        listed_files = manifest.get("config_files")
        if configured_name != config_file.name or listed_files != sorted(config_files):
            raise BackupError("backup manifest does not match its contents")
        if config_file.name not in config_files or audit_temporary is None:
            raise BackupError("backup is incomplete")

        try:
            connection = sqlite3.connect(
                f"file:{audit_temporary}?mode=ro", uri=True, timeout=5
            )
            try:
                result = connection.execute("PRAGMA quick_check").fetchone()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise BackupError("audit backup is invalid") from exc
        if not result or result[0] != "ok":
            raise BackupError("audit backup failed its integrity check")

        _validate_candidate(
            config_files[config_file.name],
            config_files,
            config_directory,
            audit_file,
            audit_temporary,
        )

        referenced_secrets = _referenced_secret_names(
            config_files[config_file.name],
            config_directory,
        )
        non_main_names = [
            name for name in sorted(config_files) if name != config_file.name
        ]
        destinations = [
            *(config_directory / name for name in non_main_names),
            audit_file,
            config_file,
        ]
        canonical_destinations = [os.path.abspath(path) for path in destinations]
        if len(canonical_destinations) != len(set(canonical_destinations)):
            raise BackupError("backup restore targets overlap")

        for name in non_main_names:
            destination = config_directory / name
            restore_entries.append(
                _RestoreEntry(
                    destination=destination,
                    staged=_stage_bytes(destination, config_files[name]),
                    rollback=None,
                )
            )
        restore_entries.append(
            _RestoreEntry(
                destination=audit_file,
                staged=audit_temporary,
                rollback=None,
            )
        )
        audit_temporary = None
        restore_entries.append(
            _RestoreEntry(
                destination=config_file,
                staged=_stage_bytes(config_file, config_files[config_file.name]),
                rollback=None,
            )
        )

        # Snapshot every active target before the first replacement. This is
        # intentionally done after every archive and candidate check, while the
        # service is stopped by scripts/restore.
        for entry in restore_entries:
            entry.rollback = _snapshot_existing(
                entry.destination,
                maximum=(
                    MAX_AUDIT_BYTES
                    if entry.destination == audit_file
                    else MAX_CONFIG_FILE_BYTES
                ),
            )

        try:
            # Secrets and the audit database are installed first. The validated
            # printers.yml is the final commit point, so the application can
            # never observe it before every referenced file is present.
            for entry in restore_entries:
                os.replace(entry.staged, entry.destination)
                entry.installed = True
                _fsync_directory(entry.destination.parent)
        except BaseException as exc:
            try:
                _rollback_restore(restore_entries)
            except BackupError as rollback_error:
                preserve_rollbacks = True
                raise rollback_error from exc
            raise BackupError("backup restore failed") from exc

        # The committed state is durable. Old snapshots can now be discarded;
        # a hidden file is retained if best-effort cleanup itself fails.
        for entry in restore_entries:
            if entry.rollback is None:
                continue
            try:
                entry.rollback.unlink(missing_ok=True)
            except OSError:
                continue
            entry.rollback = None
        for parent in {entry.destination.parent for entry in restore_entries}:
            try:
                _fsync_directory(parent)
            except (OSError, BackupError):
                # Every replacement and the final printers.yml commit were
                # already synced before rollback snapshots were removed.
                # A cleanup sync failure must not turn a successful restore
                # into an ambiguous reported failure.
                pass
        _prune_unreferenced_managed_secrets(
            config_directory,
            referenced_secrets,
        )
    except (OSError, tarfile.TarError) as exc:
        if isinstance(exc, BackupError):
            raise
        raise BackupError("backup restore failed") from exc
    finally:
        _cleanup_restore_entries(
            restore_entries,
            preserve_rollbacks=preserve_rollbacks,
        )
        if audit_temporary is not None:
            audit_temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or restore a Bambu Control backup")
    parser.add_argument("operation", choices=("create", "restore"))
    arguments = parser.parse_args()
    try:
        if arguments.operation == "create":
            create_backup(sys.stdout.buffer)
        else:
            restore_backup(sys.stdin.buffer)
    except BackupRollbackIncomplete as exc:
        print(f"Backup error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except BackupError as exc:
        print(f"Backup error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
