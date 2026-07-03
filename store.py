#!/usr/bin/env python3
"""
store.py — SQLite-backed persistent store for probes.

Presents a dict-like interface (get / __getitem__ / __setitem__ /
__contains__) so it drops straight into TestRoom in place of the in-memory
dict the tests use. Probes survive process restarts; a GC sweep drops
old terminal/expired probes so the table stays small.

One row per probe_id; the Probe dataclass is stored as JSON.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

from testroom import Probe


_SCHEMA = """
CREATE TABLE IF NOT EXISTS probes (
    probe_id   TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    state      TEXT NOT NULL,
    data       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_probes_created ON probes(created_at);
"""


class SqliteProbeStore:
    """dict-like {probe_id: Probe} backed by SQLite."""

    def __init__(self, path: str | Path):
        self._path = str(path)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def __contains__(self, probe_id: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM probes WHERE probe_id=?", (probe_id,))
        return cur.fetchone() is not None

    def get(self, probe_id: str, default=None):
        cur = self._conn.execute(
            "SELECT data FROM probes WHERE probe_id=?", (probe_id,))
        row = cur.fetchone()
        if not row:
            return default
        return Probe(**json.loads(row[0]))

    def __getitem__(self, probe_id: str) -> Probe:
        p = self.get(probe_id)
        if p is None:
            raise KeyError(probe_id)
        return p

    def __setitem__(self, probe_id: str, probe: Probe) -> None:
        self._conn.execute(
            "INSERT INTO probes(probe_id, created_at, state, data) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(probe_id) DO UPDATE SET state=excluded.state, "
            "data=excluded.data",
            (probe_id, probe.created_at, probe.state,
             json.dumps(asdict(probe))),
        )
        self._conn.commit()

    def gc(self, older_than_hours: int = 48) -> int:
        """Delete terminal/expired probes older than the cutoff. Returns count."""
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(hours=older_than_hours)).isoformat(timespec="seconds")
        cur = self._conn.execute(
            "DELETE FROM probes WHERE created_at < ? "
            "AND state IN ('passed','failed','expired')", (cutoff,))
        self._conn.commit()
        return cur.rowcount
