"""Batched SQLite persistence for every checked proxy and its credentials."""
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


def proxy_line(proxy: ParsedProxy) -> str:
    """Return a saved proxy in the representation supplied by the user."""
    return proxy.input_line


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
                protocol TEXT NOT NULL DEFAULT '',
                scheme_hint TEXT,
                proxy_text TEXT,
                working INTEGER NOT NULL DEFAULT 1,
                last_error TEXT,
                first_seen REAL NOT NULL,
                last_checked REAL NOT NULL
            )
            """
        )
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(lifetime_proxies)")
        }
        migrations = {
            "scheme_hint": "ALTER TABLE lifetime_proxies ADD COLUMN scheme_hint TEXT",
            "proxy_text": "ALTER TABLE lifetime_proxies ADD COLUMN proxy_text TEXT",
            "working": (
                "ALTER TABLE lifetime_proxies "
                "ADD COLUMN working INTEGER NOT NULL DEFAULT 1"
            ),
            "last_error": "ALTER TABLE lifetime_proxies ADD COLUMN last_error TEXT",
        }
        for name, statement in migrations.items():
            if name not in columns:
                connection.execute(statement)
        return connection

    def add_working(
        self, results: list[CheckResult], *, timestamp: float | None = None
    ) -> int:
        """Upsert every completed check while keeping one row per proxy identity."""
        now = time.time() if timestamp is None else timestamp
        rows_by_key = {
            proxy_key(result.proxy): (
                proxy_key(result.proxy),
                result.proxy.host,
                result.proxy.port,
                result.proxy.username,
                result.proxy.password,
                result.protocol.value if result.protocol is not None else "",
                result.proxy.scheme_hint,
                result.proxy.input_line,
                int(result.working),
                result.error,
                now,
                now,
            )
            for result in results
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
                    scheme_hint, proxy_text, working, last_error,
                    first_seen, last_checked
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(proxy_key) DO UPDATE SET
                    scheme_hint = excluded.scheme_hint,
                    proxy_text = excluded.proxy_text,
                    protocol = excluded.protocol,
                    working = excluded.working,
                    last_error = excluded.last_error,
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
                SELECT host, port, username, password, protocol, scheme_hint,
                       proxy_text
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
                scheme_hint=scheme_hint or protocol or None,
                raw=proxy_text,
            )
            for host, port, username, password, protocol, scheme_hint, proxy_text in rows
        ]

    def export_lines(self) -> list[str]:
        """Return the current lifetime set in downloadable proxy-list format."""
        return [proxy_line(proxy) for proxy in self.load_all()]

    def reconcile(
        self,
        checked: list[ParsedProxy],
        results: list[CheckResult],
        *,
        timestamp: float | None = None,
        snapshot_started_at: float | None = None,
    ) -> tuple[int, int]:
        """Refresh every row from this snapshot without deleting failed proxies."""
        now = time.time() if timestamp is None else timestamp
        snapshot_started_at = (
            now if snapshot_started_at is None else snapshot_started_at
        )
        result_rows = [
            (
                result.protocol.value if result.protocol is not None else "",
                int(result.working),
                result.error,
                result.proxy.scheme_hint,
                result.proxy.input_line,
                now,
                proxy_key(result.proxy),
            )
            for result in results
        ]
        with self._lock, closing(self._connect()) as connection, connection:
            if result_rows:
                connection.executemany(
                    """
                    UPDATE lifetime_proxies
                    SET protocol = ?, working = ?, last_error = ?,
                        scheme_hint = ?, proxy_text = ?, last_checked = ?
                    WHERE proxy_key = ? AND last_checked <= ?
                    """,
                    (row + (snapshot_started_at,) for row in result_rows),
                )
        return sum(1 for result in results if result.working), 0

    def count(self) -> int:
        with self._lock, closing(self._connect()) as connection, connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM lifetime_proxies"
                ).fetchone()[0]
            )


lifetime_store = LifetimeProxyStore(config.LIFETIME_DB_PATH)