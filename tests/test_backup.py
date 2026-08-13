from __future__ import annotations

import io
import json
import os
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from app import backup
from app.audit import AuditLog


class _Input:
    def __init__(self, content: bytes = b"") -> None:
        self.buffer = io.BytesIO(content)


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="bambu-backup-test-")
        self.root = Path(self.temporary.name)
        self.config_directory = self.root / "config"
        self.data_directory = self.root / "data"
        self.config_directory.mkdir()
        self.data_directory.mkdir()
        self.config_file = self.config_directory / "printers.yml"
        self.audit_file = self.data_directory / "audit.sqlite3"
        self.password_name = "bambu-web-password-v1-0123456789abcdef"
        self.access_name = "bambu-main-access-code-v1-fedcba9876543210"
        self.environment = mock.patch.dict(
            os.environ,
            {
                "BAMBU_CONFIG_FILE": str(self.config_file),
                "BAMBU_AUDIT_DB": str(self.audit_file),
                "BAMBU_CONTROL_VERSION": "0.2.0-test",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.temporary.cleanup)

    def _write_database(self, marker: str) -> None:
        self.audit_file.unlink(missing_ok=True)
        connection = sqlite3.connect(self.audit_file)
        try:
            connection.execute("CREATE TABLE restore_marker (value TEXT NOT NULL)")
            connection.execute("INSERT INTO restore_marker VALUES (?)", (marker,))
            connection.commit()
        finally:
            connection.close()
        AuditLog(str(self.audit_file))

    def _write_state(self, marker: str) -> None:
        (self.config_directory / self.password_name).write_text(
            f"web-password-{marker}-long-enough\n",
            encoding="utf-8",
        )
        (self.config_directory / self.access_name).write_text(
            f"access-{marker}\n",
            encoding="utf-8",
        )
        document = {
            "audit_db": str(self.audit_file),
            "web": {
                "username": f"admin-{marker}",
                "password_file": str(self.config_directory / self.password_name),
                "allowed_origins": ["https://localhost:9444"],
            },
            "printers": [
                {
                    "id": "main",
                    "name": f"Printer {marker}",
                    "host": "192.0.2.10",
                    "serial": "01P00TESTSERIAL",
                    "access_code_file": str(
                        self.config_directory / self.access_name
                    ),
                    "allow_self_signed_tls": True,
                    "tls_fingerprint_sha256": "a" * 64,
                    "writable": False,
                }
            ],
        }
        self.config_file.write_text(
            yaml.safe_dump(document, sort_keys=False),
            encoding="utf-8",
        )
        self._write_database(marker)

    def _create(self) -> bytes:
        output = io.BytesIO()
        backup.create_backup(output)
        return output.getvalue()

    @staticmethod
    def _members(content: bytes) -> list[tuple[str, bytes]]:
        result: list[tuple[str, bytes]] = []
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
            for member in archive.getmembers():
                extracted = archive.extractfile(member)
                result.append((member.name, extracted.read() if extracted else b""))
        return result

    @staticmethod
    def _archive(entries: list[tuple[str, bytes]], *, directory: str | None = None) -> bytes:
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            if directory is not None:
                info = tarfile.TarInfo(directory)
                info.type = tarfile.DIRTYPE
                info.mode = 0o700
                archive.addfile(info)
            for name, content in entries:
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.mode = 0o600
                archive.addfile(info, io.BytesIO(content))
        return output.getvalue()

    def _active_bytes(self) -> dict[Path, bytes]:
        return {
            self.config_file: self.config_file.read_bytes(),
            self.config_directory / self.password_name: (
                self.config_directory / self.password_name
            ).read_bytes(),
            self.config_directory / self.access_name: (
                self.config_directory / self.access_name
            ).read_bytes(),
            self.audit_file: self.audit_file.read_bytes(),
        }

    def _assert_no_restore_files(self) -> None:
        leftovers = [
            *self.config_directory.glob(".*.restore-*"),
            *self.data_directory.glob(".*.restore-*"),
        ]
        self.assertEqual(leftovers, [])

    def _new_archive_over_old_state(self) -> tuple[bytes, dict[Path, bytes]]:
        self._write_state("new")
        archive = self._create()
        self._write_state("old")
        return archive, self._active_bytes()

    def test_create_streams_manifest_config_secrets_and_consistent_sqlite(self) -> None:
        self._write_state("new")
        notes = self.config_directory / "operator-notes.txt"
        notes.write_text("local configuration attachment", encoding="utf-8")
        orphan = (
            self.config_directory
            / "bambu-unused-access-code-v1-1111111111111111"
        )
        orphan.write_text("must-not-leak", encoding="utf-8")

        content = self._create()
        members = dict(self._members(content))

        manifest = json.loads(members["manifest.json"])
        self.assertEqual(manifest["format"], backup.BACKUP_FORMAT)
        self.assertEqual(manifest["build_version"], "0.2.0-test")
        self.assertEqual(manifest["config_file"], "printers.yml")
        self.assertIn(f"config/{self.password_name}", members)
        self.assertIn(f"config/{self.access_name}", members)
        self.assertIn("config/operator-notes.txt", members)
        self.assertNotIn(f"config/{orphan.name}", members)

        database_copy = self.root / "snapshot.sqlite3"
        database_copy.write_bytes(members["data/audit.sqlite3"])
        connection = sqlite3.connect(database_copy)
        try:
            marker = connection.execute(
                "SELECT value FROM restore_marker"
            ).fetchone()
            integrity = connection.execute("PRAGMA quick_check").fetchone()
        finally:
            connection.close()
        self.assertEqual(marker, ("new",))
        self.assertEqual(integrity, ("ok",))

    def test_create_uses_one_config_revision_during_concurrent_rotation(self) -> None:
        self._write_state("old")
        old_config = self.config_file.read_bytes()
        new_password = "bambu-web-password-v1-1111111111111111"
        new_access = "bambu-main-access-code-v1-2222222222222222"
        real_read = backup._read_regular_file
        rotated = False

        def racing_read(path, *, maximum):
            nonlocal rotated
            content = real_read(path, maximum=maximum)
            if Path(path) == self.config_file and not rotated:
                rotated = True
                (self.config_directory / new_password).write_text(
                    "new-web-password-long-enough\n", encoding="utf-8"
                )
                (self.config_directory / new_access).write_text(
                    "new-access\n", encoding="utf-8"
                )
                document = yaml.safe_load(old_config)
                document["web"]["password_file"] = str(
                    self.config_directory / new_password
                )
                document["printers"][0]["access_code_file"] = str(
                    self.config_directory / new_access
                )
                self.config_file.write_text(
                    yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
                )
            return content

        with mock.patch.object(
            backup, "_read_regular_file", side_effect=racing_read
        ):
            members = dict(self._members(self._create()))

        self.assertEqual(members["config/printers.yml"], old_config)
        self.assertIn(f"config/{self.password_name}", members)
        self.assertIn(f"config/{self.access_name}", members)
        self.assertNotIn(f"config/{new_password}", members)
        self.assertNotIn(f"config/{new_access}", members)

    def test_database_snapshot_is_staged_in_the_persistent_data_volume(self) -> None:
        self._write_state("new")
        real_mkstemp = tempfile.mkstemp
        locations = []

        def recording_mkstemp(*args, **kwargs):
            locations.append(Path(kwargs["dir"]))
            return real_mkstemp(*args, **kwargs)

        with mock.patch.object(
            backup.tempfile, "mkstemp", side_effect=recording_mkstemp
        ):
            snapshot = backup._snapshot_database(self.audit_file)
        try:
            self.assertEqual(locations, [self.audit_file.parent])
            self.assertEqual(snapshot.parent, self.audit_file.parent)
        finally:
            snapshot.unlink(missing_ok=True)

    def test_create_refuses_referenced_secret_and_database_symlinks(self) -> None:
        self._write_state("new")
        external_secret = self.root / "outside-secret"
        external_secret.write_text("outside-secret", encoding="utf-8")
        access = self.config_directory / self.access_name
        access.unlink()
        access.symlink_to(external_secret)
        with self.assertRaises(backup.BackupError):
            self._create()

        access.unlink()
        access.write_text("access-new", encoding="utf-8")
        external_database = self.root / "outside.sqlite3"
        self.audit_file.replace(external_database)
        self.audit_file.symlink_to(external_database)
        with self.assertRaises(backup.BackupError):
            self._create()

    def test_restore_round_trip_commits_main_config_last(self) -> None:
        archive, _ = self._new_archive_over_old_state()
        real_replace = os.replace
        destinations: list[Path] = []

        def recording_replace(source: os.PathLike[str], destination: os.PathLike[str]) -> None:
            destinations.append(Path(destination))
            real_replace(source, destination)

        with mock.patch.object(backup.os, "replace", side_effect=recording_replace):
            backup.restore_backup(io.BytesIO(archive))

        self.assertEqual(destinations[-1], self.config_file)
        self.assertIn(b"admin-new", self.config_file.read_bytes())
        self.assertEqual(
            (self.config_directory / self.password_name).read_text().strip(),
            "web-password-new-long-enough",
        )
        connection = sqlite3.connect(self.audit_file)
        try:
            marker = connection.execute(
                "SELECT value FROM restore_marker"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(marker, ("new",))
        self._assert_no_restore_files()

    def test_restore_rejects_traversal_duplicate_and_non_regular_entries(self) -> None:
        self._write_state("new")
        valid_entries = self._members(self._create())
        malformed = {
            "traversal": self._archive(
                [
                    ("config/../escape", content)
                    if name == f"config/{self.access_name}"
                    else (name, content)
                    for name, content in valid_entries
                ]
            ),
            "duplicate": self._archive(
                [valid_entries[0], *valid_entries]
            ),
            "non-regular": self._archive(valid_entries, directory="config/unsafe"),
        }
        before = self._active_bytes()
        for label, content in malformed.items():
            with self.subTest(label=label):
                with self.assertRaises(backup.BackupError):
                    backup.restore_backup(io.BytesIO(content))
                self.assertEqual(self._active_bytes(), before)
                self._assert_no_restore_files()

    def test_invalid_database_is_rejected_before_any_active_file_changes(self) -> None:
        archive, before = self._new_archive_over_old_state()
        entries = [
            (name, b"not a sqlite database" if name == "data/audit.sqlite3" else data)
            for name, data in self._members(archive)
        ]
        with self.assertRaises(backup.BackupError):
            backup.restore_backup(io.BytesIO(self._archive(entries)))
        self.assertEqual(self._active_bytes(), before)
        self._assert_no_restore_files()

    def test_invalid_configuration_is_rejected_before_any_active_file_changes(self) -> None:
        archive, before = self._new_archive_over_old_state()
        entries = []
        for name, data in self._members(archive):
            if name == "config/printers.yml":
                document = yaml.safe_load(data)
                document["printers"][0]["host"] = "invalid host with spaces"
                data = yaml.safe_dump(document).encode()
            entries.append((name, data))
        with self.assertRaises(backup.BackupError):
            backup.restore_backup(io.BytesIO(self._archive(entries)))
        self.assertEqual(self._active_bytes(), before)
        self._assert_no_restore_files()

    def test_restore_rejects_a_different_configured_audit_target(self) -> None:
        archive, before = self._new_archive_over_old_state()
        entries = []
        for name, data in self._members(archive):
            if name == "config/printers.yml":
                document = yaml.safe_load(data)
                document["audit_db"] = "/config/printers.yml"
                data = yaml.safe_dump(document).encode()
            entries.append((name, data))
        with self.assertRaises(backup.BackupError):
            backup.restore_backup(io.BytesIO(self._archive(entries)))
        self.assertEqual(self._active_bytes(), before)
        self._assert_no_restore_files()

    def test_restore_rejects_an_incompatible_audit_schema(self) -> None:
        archive, before = self._new_archive_over_old_state()
        malformed = self.root / "malformed.sqlite3"
        connection = sqlite3.connect(malformed)
        try:
            connection.execute("CREATE TABLE command_audit (id INTEGER PRIMARY KEY)")
            connection.commit()
        finally:
            connection.close()
        entries = [
            (name, malformed.read_bytes() if name == "data/audit.sqlite3" else data)
            for name, data in self._members(archive)
        ]
        with self.assertRaises(backup.BackupError):
            backup.restore_backup(io.BytesIO(self._archive(entries)))
        self.assertEqual(self._active_bytes(), before)
        self._assert_no_restore_files()

    def test_staging_failure_leaves_every_active_file_untouched(self) -> None:
        archive, before = self._new_archive_over_old_state()
        real_stage = backup._stage_bytes
        calls = 0

        def failing_stage(destination: Path, content: bytes) -> Path:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected staging failure")
            return real_stage(destination, content)

        with mock.patch.object(backup, "_stage_bytes", side_effect=failing_stage):
            with self.assertRaises(backup.BackupError):
                backup.restore_backup(io.BytesIO(archive))
        self.assertEqual(self._active_bytes(), before)
        self._assert_no_restore_files()

    def test_snapshot_fdopen_failure_does_not_double_close_or_leak(self) -> None:
        destination = self.config_directory / "existing-secret"
        destination.write_bytes(b"existing")
        real_fdopen = os.fdopen
        calls = 0

        def failing_fdopen(descriptor: int, mode: str):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected second fdopen failure")
            return real_fdopen(descriptor, mode)

        with mock.patch.object(backup.os, "fdopen", side_effect=failing_fdopen):
            with self.assertRaisesRegex(RuntimeError, "second fdopen"):
                backup._snapshot_existing(destination, maximum=1024)
        self.assertEqual(list(self.config_directory.glob(".*.restore-rollback")), [])

    def test_secret_install_failure_rolls_back_byte_exactly(self) -> None:
        archive, before = self._new_archive_over_old_state()
        real_replace = os.replace
        failed = False

        def failing_replace(source: os.PathLike[str], destination: os.PathLike[str]) -> None:
            nonlocal failed
            source_path = Path(source)
            if not failed and source_path.name.endswith(".restore-stage"):
                failed = True
                raise OSError("injected secret replacement failure")
            real_replace(source, destination)

        with mock.patch.object(backup.os, "replace", side_effect=failing_replace):
            with self.assertRaises(backup.BackupError):
                backup.restore_backup(io.BytesIO(archive))
        self.assertEqual(self._active_bytes(), before)
        self._assert_no_restore_files()

    def test_database_install_failure_rolls_back_all_installed_secrets(self) -> None:
        archive, before = self._new_archive_over_old_state()
        real_replace = os.replace

        def failing_replace(source: os.PathLike[str], destination: os.PathLike[str]) -> None:
            if Path(destination) == self.audit_file and not Path(source).name.endswith(
                ".restore-rollback"
            ):
                raise OSError("injected database replacement failure")
            real_replace(source, destination)

        with mock.patch.object(backup.os, "replace", side_effect=failing_replace):
            with self.assertRaises(backup.BackupError):
                backup.restore_backup(io.BytesIO(archive))
        self.assertEqual(self._active_bytes(), before)
        self._assert_no_restore_files()

    def test_final_config_failure_rolls_back_database_and_secrets(self) -> None:
        archive, before = self._new_archive_over_old_state()
        real_replace = os.replace

        def failing_replace(source: os.PathLike[str], destination: os.PathLike[str]) -> None:
            if Path(destination) == self.config_file and Path(source).name.endswith(
                ".restore-stage"
            ):
                raise OSError("injected commit-point failure")
            real_replace(source, destination)

        with mock.patch.object(backup.os, "replace", side_effect=failing_replace):
            with self.assertRaises(backup.BackupError):
                backup.restore_backup(io.BytesIO(archive))
        self.assertEqual(self._active_bytes(), before)
        self._assert_no_restore_files()

    def test_post_replace_fsync_failure_rolls_back_byte_exactly(self) -> None:
        archive, before = self._new_archive_over_old_state()
        real_replace = os.replace
        real_fsync = backup._fsync_directory
        installed = False
        failed = False

        def recording_replace(source: os.PathLike[str], destination: os.PathLike[str]) -> None:
            nonlocal installed
            real_replace(source, destination)
            if not Path(source).name.endswith(".restore-rollback"):
                installed = True

        def failing_fsync(path: Path) -> None:
            nonlocal failed
            if installed and not failed:
                failed = True
                raise OSError("injected directory fsync failure")
            real_fsync(path)

        with (
            mock.patch.object(backup.os, "replace", side_effect=recording_replace),
            mock.patch.object(backup, "_fsync_directory", side_effect=failing_fsync),
        ):
            with self.assertRaises(backup.BackupError):
                backup.restore_backup(io.BytesIO(archive))
        self.assertEqual(self._active_bytes(), before)
        self._assert_no_restore_files()

    def test_incomplete_rollback_raises_special_error_and_preserves_snapshots(self) -> None:
        archive, before = self._new_archive_over_old_state()
        real_replace = os.replace

        def failing_replace(source: os.PathLike[str], destination: os.PathLike[str]) -> None:
            source_path = Path(source)
            destination_path = Path(destination)
            if destination_path == self.audit_file and not source_path.name.endswith(
                ".restore-rollback"
            ):
                raise OSError("injected install failure")
            if (
                destination_path == self.config_directory / self.access_name
                and source_path.name.endswith(".restore-rollback")
            ):
                raise OSError("injected rollback failure")
            real_replace(source, destination)

        with mock.patch.object(backup.os, "replace", side_effect=failing_replace):
            with self.assertRaises(backup.BackupRollbackIncomplete):
                backup.restore_backup(io.BytesIO(archive))

        self.assertEqual(self.config_file.read_bytes(), before[self.config_file])
        self.assertEqual(self.audit_file.read_bytes(), before[self.audit_file])
        self.assertEqual(
            (self.config_directory / self.access_name).read_text().strip(),
            "access-new",
        )
        preserved = [
            *self.config_directory.glob(".*.restore-rollback"),
            *self.data_directory.glob(".*.restore-rollback"),
        ]
        self.assertTrue(preserved)

    def test_success_prunes_only_unreferenced_regular_managed_secrets(self) -> None:
        archive, _ = self._new_archive_over_old_state()
        orphan = (
            self.config_directory
            / "bambu-orphan-access-code-v1-2222222222222222"
        )
        orphan.write_text("obsolete", encoding="utf-8")
        unrelated = self.config_directory / "keep-me.txt"
        unrelated.write_text("unrelated", encoding="utf-8")
        external = self.root / "outside"
        external.write_text("outside", encoding="utf-8")
        orphan_symlink = (
            self.config_directory
            / "bambu-symlink-access-code-v1-3333333333333333"
        )
        orphan_symlink.symlink_to(external)

        backup.restore_backup(io.BytesIO(archive))

        self.assertFalse(orphan.exists())
        self.assertEqual(unrelated.read_text(), "unrelated")
        self.assertTrue(orphan_symlink.is_symlink())
        self.assertEqual(external.read_text(), "outside")

    def test_cli_maps_incomplete_rollback_to_exit_code_two(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch.object(backup.sys, "argv", ["backup", "restore"]),
            mock.patch.object(backup.sys, "stdin", _Input()),
            mock.patch.object(backup.sys, "stderr", stderr),
            mock.patch.object(
                backup,
                "restore_backup",
                side_effect=backup.BackupRollbackIncomplete(
                    "rollback was incomplete"
                ),
            ),
        ):
            with self.assertRaises(SystemExit) as raised:
                backup.main()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("rollback", stderr.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
