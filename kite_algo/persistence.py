"""Sqlite audit trail (scaffold)."""

from __future__ import annotations


class SqliteStore:
    """Persistent audit store for orders + fills. Scaffold only."""

    def __init__(self, path: str):
        self.path = path

    def write_event(self, table: str, row: dict) -> None:
        raise NotImplementedError
