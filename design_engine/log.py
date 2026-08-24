"""FRACAS-style structured action log — the source of truth.

Every tool action writes a row here BEFORE doing its work (result='pending'),
then finalizes it to pass/fail. Reports and images are generated FROM this
log, never authored independently of it.

Schema = the minimum columns locked in the build plan (Phase 2), plus
`details_json` for structured numeric payloads (volumes, margins, digests).
This module is the Phase 2 seed; Phase 2 extends it (queries by failure mode,
sign-off tokens) without changing these columns.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from pathlib import Path

RESULTS = ("pass", "fail", "pending")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    phase TEXT NOT NULL,
    action TEXT NOT NULL,
    geometry_version TEXT,
    reason TEXT,
    result TEXT NOT NULL CHECK (result IN ('pass','fail','pending')),
    failure_mode TEXT,
    linked_parent_id INTEGER REFERENCES actions(id),
    signed_off_by TEXT,
    details_json TEXT
)
"""


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


class ActionLog:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def open_action(
        self,
        phase: str,
        action: str,
        *,
        geometry_version: str | None = None,
        reason: str | None = None,
        linked_parent_id: int | None = None,
    ) -> int:
        """Insert a 'pending' row for an action about to run. Returns row id."""
        cur = self._conn.execute(
            "INSERT INTO actions"
            " (timestamp, phase, action, geometry_version, reason, result,"
            "  linked_parent_id)"
            " VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (_utcnow(), phase, action, geometry_version, reason, linked_parent_id),
        )
        self._conn.commit()
        return cur.lastrowid

    def close_action(
        self,
        action_id: int,
        result: str,
        *,
        failure_mode: str | None = None,
        geometry_version: str | None = None,
        details: dict | None = None,
        signed_off_by: str | None = None,
    ) -> None:
        """Finalize a pending row to pass/fail."""
        if result not in ("pass", "fail"):
            raise ValueError(f"result must be pass|fail, got {result!r}")
        self._conn.execute(
            "UPDATE actions SET result = ?, failure_mode = ?,"
            " geometry_version = COALESCE(?, geometry_version),"
            " details_json = COALESCE(?, details_json),"
            " signed_off_by = COALESCE(?, signed_off_by)"
            " WHERE id = ?",
            (
                result,
                failure_mode,
                geometry_version,
                json.dumps(details, sort_keys=True) if details is not None else None,
                signed_off_by,
                action_id,
            ),
        )
        self._conn.commit()

    def rows(
        self,
        *,
        action: str | None = None,
        result: str | None = None,
        geometry_version: str | None = None,
    ) -> list[sqlite3.Row]:
        clauses, params = [], []
        for col, val in (
            ("action", action),
            ("result", result),
            ("geometry_version", geometry_version),
        ):
            if val is not None:
                clauses.append(f"{col} = ?")
                params.append(val)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return list(
            self._conn.execute(f"SELECT * FROM actions{where} ORDER BY id", params)
        )

    def latest_pass_for(self, geometry_version: str) -> sqlite3.Row | None:
        """Most recent passed row that produced/touched this geometry version."""
        rows = self.rows(geometry_version=geometry_version, result="pass")
        return rows[-1] if rows else None

    # ---------- Phase 2: FRACAS queries ----------

    def failures(self, failure_mode_like: str | None = None) -> list[sqlite3.Row]:
        """All fail rows, optionally filtered by failure-mode substring."""
        if failure_mode_like is None:
            return self.rows(result="fail")
        return list(self._conn.execute(
            "SELECT * FROM actions WHERE result = 'fail'"
            " AND failure_mode LIKE ? ORDER BY id",
            (f"%{failure_mode_like}%",)))

    def failure_mode_counts(self) -> list[tuple[str, int]]:
        """Failure modes ranked by frequency — the FRACAS 'what keeps breaking' view."""
        return [
            (r["failure_mode"], r["n"])
            for r in self._conn.execute(
                "SELECT failure_mode, COUNT(*) AS n FROM actions"
                " WHERE result = 'fail' GROUP BY failure_mode"
                " ORDER BY n DESC, failure_mode")
        ]

    def version_history(self, part_number: str) -> list[sqlite3.Row]:
        """Passed create/edit rows for every version of a part, oldest first."""
        return list(self._conn.execute(
            "SELECT * FROM actions WHERE result = 'pass'"
            " AND action IN ('create_part', 'edit_part')"
            " AND geometry_version LIKE ? ORDER BY id",
            (f"{part_number}@v%",)))

    def lineage(self, geometry_version: str) -> list[sqlite3.Row]:
        """Rows from this version back to its root, walking linked_parent_id."""
        chain: list[sqlite3.Row] = []
        row = self.latest_pass_for(geometry_version)
        seen: set[int] = set()
        while row is not None and row["id"] not in seen:
            chain.append(row)
            seen.add(row["id"])
            parent_id = row["linked_parent_id"]
            row = None if parent_id is None else self._conn.execute(
                "SELECT * FROM actions WHERE id = ?", (parent_id,)).fetchone()
        return chain

    def pending_actions(self) -> list[sqlite3.Row]:
        """Rows never finalized — evidence of a crashed or interrupted run."""
        return self.rows(result="pending")

    def close(self) -> None:
        self._conn.close()
