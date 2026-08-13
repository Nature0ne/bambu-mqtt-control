from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Mapping
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

SENSITIVE_KEY_MARKERS = frozenset(
    {
        "access_code",
        "api_key",
        "authorization",
        "cookie",
        "credential",
        "password",
        "secret",
        "token",
    }
)
DEFAULT_RETENTION_DAYS = 90
DEFAULT_MAX_ROWS = 100_000


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): (
                "[redacted]"
                if any(
                    marker in str(key).strip().lower().replace("-", "_")
                    for marker in SENSITIVE_KEY_MARKERS
                )
                else _redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


class AuditLog:
    def __init__(
        self,
        path: str,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        max_rows: int = DEFAULT_MAX_ROWS,
        clock: Callable[[], datetime] | None = None,
    ):
        if not 1 <= retention_days <= 3650:
            raise ValueError("invalid audit retention")
        if not 100 <= max_rows <= 1_000_000:
            raise ValueError("invalid audit row limit")
        self.path = path
        self.retention_days = retention_days
        self.max_rows = max_rows
        self._clock = clock or (lambda: datetime.now(timezone.utc))
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
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_command_audit_created_id "
                "ON command_audit(created_at, id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_command_audit_printer_id "
                "ON command_audit(printer_id, id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_command_audit_command_id "
                "ON command_audit(command, id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_command_audit_result_id "
                "ON command_audit(result, id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_command_audit_actor_id "
                "ON command_audit(actor, id)"
            )
            self._prune(connection, include_age=True)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("audit clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def _prune(self, connection: sqlite3.Connection, *, include_age: bool) -> None:
        if include_age:
            cutoff = self._now() - timedelta(days=self.retention_days)
            connection.execute(
                "DELETE FROM command_audit WHERE created_at < ?",
                (cutoff.isoformat(),),
            )
        boundary = connection.execute(
            "SELECT id FROM command_audit ORDER BY id DESC LIMIT 1 OFFSET ?",
            (self.max_rows,),
        ).fetchone()
        if boundary is not None:
            connection.execute(
                "DELETE FROM command_audit WHERE id <= ?",
                (int(boundary[0]),),
            )

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
                    self._now().isoformat(),
                    actor[:128],
                    printer_id[:64],
                    command[:64],
                    sequence_id,
                    payload,
                    result[:32],
                    detail[:500] if detail else None,
                ),
            )
            # The row cap is enforced on every write. Age pruning also happens
            # here so a quiet installation cannot retain records indefinitely
            # after it becomes active again.
            self._prune(connection, include_age=True)

    def counts(self) -> dict[str, int]:
        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT result, COUNT(*) FROM command_audit GROUP BY result"
            ).fetchall()
        return {str(result): int(count) for result, count in rows}

    def history(
        self,
        *,
        limit: int,
        cursor: int | None = None,
        printer_id: str | None = None,
        command: str | None = None,
        result: str | None = None,
        actor: str | None = None,
        from_time: str | None = None,
        to_time: str | None = None,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise ValueError("invalid audit page size")
        clauses: list[str] = []
        parameters: list[Any] = []
        for column, value in (
            ("printer_id", printer_id),
            ("command", command),
            ("result", result),
            ("actor", actor),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        if cursor is not None:
            clauses.append("id < ?")
            parameters.append(cursor)
        if from_time is not None:
            clauses.append("created_at >= ?")
            parameters.append(from_time)
        if to_time is not None:
            clauses.append("created_at <= ?")
            parameters.append(to_time)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        query = (
            "SELECT id, created_at, actor, printer_id, command, result "
            f"FROM command_audit{where} ORDER BY id DESC LIMIT ?"
        )
        parameters.append(limit + 1)
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(query, parameters).fetchall()
        has_more = len(rows) > limit
        page = rows[:limit]
        items = [
            {
                "id": int(row[0]),
                "created_at": str(row[1]),
                "actor": str(row[2]),
                "printer_id": str(row[3]),
                "command": str(row[4]),
                "result": str(row[5]),
            }
            for row in page
        ]
        return {
            "items": items,
            "next_cursor": items[-1]["id"] if has_more and items else None,
        }

    def retention(self) -> dict[str, int]:
        return {"days": self.retention_days, "max_rows": self.max_rows}
