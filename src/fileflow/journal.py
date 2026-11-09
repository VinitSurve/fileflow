"""Write-ahead SQLite operation journal (journal.db) for fileflow."""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import secrets
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fileflow.common.exceptions import DataError, NotFoundError
from fileflow.common.paths import ToolPaths

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS operations (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    root TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    meta TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operation_items (
    operation_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    destination TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    sha256 TEXT,
    is_symlink INTEGER NOT NULL,
    created_dirs TEXT NOT NULL,
    PRIMARY KEY (operation_id, seq),
    FOREIGN KEY (operation_id) REFERENCES operations(id)
);

CREATE INDEX IF NOT EXISTS idx_operations_created_at ON operations(created_at DESC, id DESC);
"""


@dataclass
class JournalItem:
    """Single file operation item recorded in the journal."""

    seq: int
    kind: str  # "move", "rename", "mkdir"
    source: str
    destination: str
    status: str  # "planned", "done", "failed", "skipped", "undone"
    size: int
    mtime_ns: int
    is_symlink: bool = False
    sha256: str | None = None
    error: str | None = None
    created_dirs: list[str] = field(default_factory=list)


@dataclass
class OperationSummary:
    """Summary of a journaled operation for 'fileflow history'."""

    id: str
    type: str
    root: str
    created_at: str
    status: str
    total_items: int
    done_count: int
    failed_count: int
    skipped_count: int
    undone_count: int
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class OperationRecord:
    """Full journal record including all items."""

    id: str
    type: str
    root: str
    created_at: str
    status: str
    meta: dict[str, Any]
    items: list[JournalItem]

    @property
    def done_count(self) -> int:
        return sum(1 for i in self.items if i.status == "done")

    @property
    def failed_count(self) -> int:
        return sum(1 for i in self.items if i.status == "failed")

    @property
    def skipped_count(self) -> int:
        return sum(1 for i in self.items if i.status == "skipped")

    @property
    def undone_count(self) -> int:
        return sum(1 for i in self.items if i.status == "undone")


def generate_operation_id() -> str:
    """Generate a short, chronologically sortable operation ID."""
    now = dt.datetime.now(tz=dt.UTC)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    suffix = secrets.token_hex(2)
    return f"{stamp}-{suffix}"


class Journal:
    """Transactional SQLite operation journal stored in fileflow's data directory."""

    def __init__(self, paths: ToolPaths) -> None:
        self.paths = paths

    @property
    def db_path(self) -> Path:
        return self.paths.data_dir / "journal.db"

    @contextlib.contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        db_file = self.db_path
        if write:
            ToolPaths.ensure(self.paths.data_dir)
        elif not db_file.exists():
            raise FileNotFoundError(str(db_file))

        try:
            conn = sqlite3.connect(str(db_file), timeout=10.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.executescript(_SCHEMA_SQL)
            if write and db_file.exists():
                with contextlib.suppress(OSError):
                    os.chmod(db_file, 0o600)
        except sqlite3.DatabaseError as exc:
            raise DataError(
                f"Operation journal is corrupt or unreadable ({db_file}): {exc}",
                hint="Inspect or back up and remove the corrupt journal.db file.",
            ) from exc
        except OSError as exc:
            raise DataError(
                f"Cannot access operation journal ({db_file}): {exc}",
                hint="Check directory permissions for fileflow data directory.",
            ) from exc

        try:
            yield conn
            conn.commit()
        except sqlite3.DatabaseError as exc:
            with contextlib.suppress(Exception):
                conn.rollback()
            raise DataError(
                f"Operation journal database error ({db_file}): {exc}",
                hint="Inspect or repair journal.db.",
            ) from exc
        finally:
            with contextlib.suppress(Exception):
                conn.close()

    def recover_stale_operations(self) -> int:
        """Mark any leftover 'planned' operations from a prior crash as 'interrupted'."""
        if not self.db_path.exists():
            return 0
        with self._connect(write=True) as conn:
            cur = conn.execute(
                "UPDATE operations SET status = 'interrupted' WHERE status = 'planned'"
            )
            return cur.rowcount

    def begin_operation(
        self,
        op_type: str,
        root: Path | str,
        items: Sequence[JournalItem],
        *,
        meta: Mapping[str, Any] | None = None,
        op_id: str | None = None,
    ) -> str:
        """Write-ahead commit of a planned operation and all its items before moving files."""
        self.recover_stale_operations()
        operation_id = op_id or generate_operation_id()
        now_iso = dt.datetime.now(tz=dt.UTC).isoformat(timespec="seconds")
        meta_json = json.dumps(dict(meta or {}), sort_keys=True)

        with self._connect(write=True) as conn:
            # Ensure unique ID even within the same second
            while (
                conn.execute("SELECT 1 FROM operations WHERE id = ?", (operation_id,)).fetchone()
                is not None
            ):
                operation_id = generate_operation_id()

            conn.execute(
                """
                INSERT INTO operations (id, type, root, created_at, status, meta)
                VALUES (?, ?, ?, ?, 'planned', ?)
                """,
                (operation_id, op_type, str(root), now_iso, meta_json),
            )
            for item in items:
                conn.execute(
                    """
                    INSERT INTO operation_items (
                        operation_id, seq, kind, source, destination, status, error,
                        size, mtime_ns, sha256, is_symlink, created_dirs
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        operation_id,
                        item.seq,
                        item.kind,
                        item.source,
                        item.destination,
                        item.status,
                        item.error,
                        item.size,
                        item.mtime_ns,
                        item.sha256,
                        1 if item.is_symlink else 0,
                        json.dumps(item.created_dirs),
                    ),
                )
        return operation_id

    def record_item_done(
        self,
        op_id: str,
        seq: int,
        *,
        destination: str,
        size: int,
        mtime_ns: int,
        sha256: str | None,
        created_dirs: Sequence[str],
    ) -> None:
        """Mark one item as done immediately after its filesystem move completes."""
        with self._connect(write=True) as conn:
            conn.execute(
                """
                UPDATE operation_items
                SET status = 'done',
                    destination = ?,
                    size = ?,
                    mtime_ns = ?,
                    sha256 = ?,
                    created_dirs = ?,
                    error = NULL
                WHERE operation_id = ? AND seq = ?
                """,
                (
                    destination,
                    size,
                    mtime_ns,
                    sha256,
                    json.dumps(list(created_dirs)),
                    op_id,
                    seq,
                ),
            )

    def record_item_failed(self, op_id: str, seq: int, error: str) -> None:
        """Mark one item as failed with its error message."""
        with self._connect(write=True) as conn:
            conn.execute(
                """
                UPDATE operation_items
                SET status = 'failed', error = ?
                WHERE operation_id = ? AND seq = ?
                """,
                (error, op_id, seq),
            )

    def record_item_skipped(self, op_id: str, seq: int, reason: str) -> None:
        """Mark one item as skipped."""
        with self._connect(write=True) as conn:
            conn.execute(
                """
                UPDATE operation_items
                SET status = 'skipped', error = ?
                WHERE operation_id = ? AND seq = ?
                """,
                (reason, op_id, seq),
            )

    def record_item_undone(self, op_id: str, seq: int) -> None:
        """Mark one item as undone after safe reversal."""
        with self._connect(write=True) as conn:
            conn.execute(
                """
                UPDATE operation_items
                SET status = 'undone'
                WHERE operation_id = ? AND seq = ?
                """,
                (op_id, seq),
            )

    def finish_operation(self, op_id: str, status: str) -> None:
        """Update the overall status of an operation."""
        with self._connect(write=True) as conn:
            conn.execute(
                "UPDATE operations SET status = ? WHERE id = ?",
                (status, op_id),
            )

    def list_operations(self, *, limit: int = 20) -> list[OperationSummary]:
        """Return recent operations ordered newest first."""
        if not self.db_path.exists():
            return []
        self.recover_stale_operations()
        with self._connect(write=False) as conn:
            rows = conn.execute(
                """
                SELECT
                    o.id,
                    o.type,
                    o.root,
                    o.created_at,
                    o.status,
                    o.meta,
                    COUNT(i.seq) AS total_items,
                    SUM(CASE WHEN i.status = 'done' THEN 1 ELSE 0 END) AS done_count,
                    SUM(CASE WHEN i.status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                    SUM(CASE WHEN i.status = 'skipped' THEN 1 ELSE 0 END) AS skipped_count,
                    SUM(CASE WHEN i.status = 'undone' THEN 1 ELSE 0 END) AS undone_count
                FROM operations o
                LEFT JOIN operation_items i ON o.id = i.operation_id
                GROUP BY o.id
                ORDER BY o.created_at DESC, o.id DESC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()

        summaries: list[OperationSummary] = []
        for row in rows:
            meta = _parse_json_dict(row["meta"])
            summaries.append(
                OperationSummary(
                    id=str(row["id"]),
                    type=str(row["type"]),
                    root=str(row["root"]),
                    created_at=str(row["created_at"]),
                    status=str(row["status"]),
                    total_items=int(row["total_items"] or 0),
                    done_count=int(row["done_count"] or 0),
                    failed_count=int(row["failed_count"] or 0),
                    skipped_count=int(row["skipped_count"] or 0),
                    undone_count=int(row["undone_count"] or 0),
                    meta=meta,
                )
            )
        return summaries

    def get_operation(self, op_id: str) -> OperationRecord:
        """Fetch an operation and all its items by ID (or 'last'/'latest')."""
        if not self.db_path.exists():
            raise NotFoundError(
                f"Operation not found: {op_id!r} (journal is empty).",
                hint="Run 'fileflow history' to view recorded operations.",
            )
        self.recover_stale_operations()
        with self._connect(write=False) as conn:
            if op_id.lower() in ("last", "latest"):
                op_row = conn.execute(
                    "SELECT * FROM operations ORDER BY created_at DESC, id DESC LIMIT 1"
                ).fetchone()
            else:
                op_row = conn.execute("SELECT * FROM operations WHERE id = ?", (op_id,)).fetchone()
            if op_row is None:
                raise NotFoundError(
                    f"Operation not found: {op_id!r}",
                    hint="Run 'fileflow history' to view recorded operation IDs.",
                )
            actual_id = str(op_row["id"])
            item_rows = conn.execute(
                """
                SELECT * FROM operation_items
                WHERE operation_id = ?
                ORDER BY seq ASC
                """,
                (actual_id,),
            ).fetchall()

        items: list[JournalItem] = []
        for r in item_rows:
            created_dirs = _parse_json_list(r["created_dirs"])
            items.append(
                JournalItem(
                    seq=int(r["seq"]),
                    kind=str(r["kind"]),
                    source=str(r["source"]),
                    destination=str(r["destination"]),
                    status=str(r["status"]),
                    error=str(r["error"]) if r["error"] is not None else None,
                    size=int(r["size"]),
                    mtime_ns=int(r["mtime_ns"]),
                    sha256=str(r["sha256"]) if r["sha256"] is not None else None,
                    is_symlink=bool(r["is_symlink"]),
                    created_dirs=created_dirs,
                )
            )

        return OperationRecord(
            id=actual_id,
            type=str(op_row["type"]),
            root=str(op_row["root"]),
            created_at=str(op_row["created_at"]),
            status=str(op_row["status"]),
            meta=_parse_json_dict(op_row["meta"]),
            items=items,
        )


def _parse_json_dict(raw: Any) -> dict[str, Any]:
    if not raw or not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError as exc:
        raise DataError(f"Corrupt metadata JSON in journal: {exc}") from exc


def _parse_json_list(raw: Any) -> list[str]:
    if not raw or not isinstance(raw, str):
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
        return []
    except json.JSONDecodeError as exc:
        raise DataError(f"Corrupt created_dirs JSON in journal: {exc}") from exc
