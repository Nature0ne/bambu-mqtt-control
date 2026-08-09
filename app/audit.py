from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Mapping
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SENSITIVE_KEYS = frozenset({"access_code", "password", "token", "secret"})


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[redacted]" if str(key).lower() in SENSITIVE_KEYS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


class AuditLog:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS command_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    printer_id TEXT NOT NULL,
                    command TEXT NOT NULL,
                    sequence_id TEXT,
                    params_json TEXT NOT NULL,
                    result TEXT NOT NULL,
                    detail TEXT
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def record(
        self,
        *,
        actor: str,
        printer_id: str,
        command: str,
        params: Mapping[str, Any],
        result: str,
        sequence_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        payload = json.dumps(_redact(dict(params)), sort_keys=True, separators=(",", ":"))
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO command_audit
                    (created_at, actor, printer_id, command, sequence_id, params_json, result, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    actor[:128],
                    printer_id[:64],
                    command[:64],
                    sequence_id,
                    payload,
                    result[:32],
                    detail[:500] if detail else None,
                ),
            )

    def counts(self) -> dict[str, int]:
        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT result, COUNT(*) FROM command_audit GROUP BY result"
            ).fetchall()
        return {str(result): int(count) for result, count in rows}
