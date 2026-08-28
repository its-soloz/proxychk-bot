"""Batched SQLite persistence for unique lifetime working proxies."""
from __future__ import annotations

from contextlib import closing
import json
import os
import sqlite3
import threading
import time

import config
from checker import CheckResult
from parser import ParsedProxy


def proxy_key(proxy: ParsedProxy) -> str:
    """Stable identity independent of the protocol detected on a later check."""
    return json.dumps(
        [proxy.host, proxy.port, proxy.username, proxy.password],
        ensure_ascii=False,
        separators=(",", ":"),
    )


class LifetimeProxyStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS lifetime_proxies (
                proxy_key TEXT PRIMARY KEY,
                host TEXT NOT NULL,
                port INTEGER NOT NULL,
                username TEXT,
                password TEXT,
                protocol TEXT NOT NULL,
                first_seen REAL NOT NULL,
                last_checked REAL NOT NULL
            )
            """
        )
        return connection

    def add_working(
        self, results: list[CheckResult], *, timestamp: float | None = None
    ) -> int:
        """Upsert a completed batch once while keeping one row per proxy."""
        now = time.time() if timestamp is None else timestamp
        rows_by_key = {
            proxy_key(result.proxy): (
                proxy_key(result.proxy),
                result.proxy.host,
                result.proxy.port,
                result.proxy.username,
                result.proxy.password,
                result.protocol.value,
                now,
                now,
            )
            for result in results
            if result.working and result.protocol is not None
        }
        rows = list(rows_by_key.values())
        if not rows:
            return 0
        with self._lock, closing(self._connect()) as connection, connection:
            count_before = int(
                connection.execute(
                    "SELECT COUNT(*) FROM lifetime_proxies"
                ).fetchone()[0]
            )
            connection.executemany(
                """
                INSERT INTO lifetime_proxies (
                    proxy_key, host, port, username, password, protocol,
                    first_seen, last_checked
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(proxy_key) DO UPDATE SET
                    protocol = excluded.protocol,
                    last_checked = excluded.last_checked
                """,
                rows,
            )
            count_after = int(
                connection.execute(
                    "SELECT COUNT(*) FROM lifetime_proxies"
                ).fetchone()[0]
            )
            return count_after - count_before

    def load_all(self) -> list[ParsedProxy]:
        """Load every unique saved proxy with its latest detected protocol hint."""
        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT host, port, username, password, protocol
                FROM lifetime_proxies
                ORDER BY first_seen, proxy_key
                """
            ).fetchall()
        return [
            ParsedProxy(
                host=host,
                port=port,
                username=username,
                password=password,
                scheme_hint=protocol,
            )
            for host, port, username, password, protocol in rows
        ]

    def reconcile(
        self,
        checked: list[ParsedProxy],
        results: list[CheckResult],
        *,
        timestamp: float | None = None,
        snapshot_started_at: float | None = None,
    ) -> tuple[int, int]:
        """Refresh survivors and safely delete failures from this snapshot only."""
        now = time.time() if timestamp is None else timestamp
        snapshot_started_at = (
            now if snapshot_started_at is None else snapshot_started_at
        )
        survivor_rows = [
            (result.protocol.value, now, proxy_key(result.proxy))
            for result in results
            if result.working and result.protocol is not None
        ]
        survivor_keys = {row[2] for row in survivor_rows}
        failed_keys = [
            proxy_key(proxy)
            for proxy in checked
            if proxy_key(proxy) not in survivor_keys
        ]
        with self._lock, closing(self._connect()) as connection, connection:
            if survivor_rows:
                connection.executemany(
                    """
                    UPDATE lifetime_proxies
                    SET protocol = ?, last_checked = ?
                    WHERE proxy_key = ?
                    """,
                    survivor_rows,
                )
            if failed_keys:
                delete_cursor = connection.executemany(
                    """
                    DELETE FROM lifetime_proxies
                    WHERE proxy_key = ? AND last_checked <= ?
                    """,
                    ((key, snapshot_started_at) for key in failed_keys),
                )
                removed = max(0, delete_cursor.rowcount)
            else:
                removed = 0
        return len(survivor_rows), removed

    def count(self) -> int:
        with self._lock, closing(self._connect()) as connection, connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM lifetime_proxies"
                ).fetchone()[0]
            )


lifetime_store = LifetimeProxyStore(config.LIFETIME_DB_PATH)