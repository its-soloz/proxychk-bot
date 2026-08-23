from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace

from checker import CheckResult, Protocol
import handlers
from parser import ParsedProxy


class FakeQuery:
    def __init__(self, data: str, user_id: int, chat_id: int):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = SimpleNamespace(chat_id=chat_id)
        self.answers: list[tuple[str | None, bool]] = []
        self.edits: list[tuple[str, dict]] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class FakeBot:
    def __init__(self):
        self.documents: list[dict] = []

    async def send_document(self, **kwargs):
        self.documents.append(kwargs)


class FakeDatabaseResult:
    def __init__(self, rows=None):
        self._rows = rows or []

    def fetchall(self):
        return self._rows


class FakeDatabaseCursor:
    def __init__(self, rows: dict[str, dict]):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def executemany(self, _query, values):
        for session_id, expires_at, payload in values:
            self._rows[session_id] = {
                "expires_at": expires_at,
                "payload": json.loads(payload),
            }


class FakeDatabaseConnection:
    def __init__(self, rows: dict[str, dict]):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def cursor(self):
        return FakeDatabaseCursor(self._rows)

    def execute(self, query, params=None):
        normalized = " ".join(query.split())
        if normalized.startswith("CREATE TABLE"):
            return FakeDatabaseResult()
        if normalized.startswith("SELECT session_id"):
            return FakeDatabaseResult(
                [
                    (session_id, record["payload"])
                    for session_id, record in self._rows.items()
                ]
            )
        if normalized.startswith("DELETE") and "ANY(%s)" in normalized:
            for session_id in params[0]:
                self._rows.pop(session_id, None)
            return FakeDatabaseResult()
        if normalized.startswith("DELETE"):
            earliest, latest = params
            for session_id, record in list(self._rows.items()):
                if not earliest < record["expires_at"] <= latest:
                    self._rows.pop(session_id, None)
            return FakeDatabaseResult()
        raise AssertionError(f"Unexpected SQL in test: {normalized}")


def make_results(count: int = 12) -> list[CheckResult]:
    return [
        CheckResult(
            proxy=ParsedProxy(f"192.0.2.{index + 1}", 9000 + index),
            working=True,
            protocol=Protocol.HTTP,
            latency_ms=50 + index,
            is_residential=True,
            is_datacenter=True,
            is_rotating=True,
        )
        for index in range(count)
    ]


class ResultMenuTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._session_directory = tempfile.TemporaryDirectory()
        self._original_session_path = handlers._RESULT_SESSION_PATH
        handlers._RESULT_SESSION_PATH = str(
            Path(self._session_directory.name) / "result_sessions.json"
        )
        self._original_database_url = handlers._RESULT_DATABASE_URL
        self._original_open_result_database = handlers._open_result_database
        handlers._RESULT_DATABASE_URL = ""
        handlers._result_sessions.clear()
        self.results = make_results()
        self.groups = {
            category: list(self.results)
            for category in handlers._RESULT_CATEGORIES
        }
        self.context = SimpleNamespace(bot=FakeBot())

    def tearDown(self):
        handlers._result_sessions.clear()
        handlers._RESULT_SESSION_PATH = self._original_session_path
        handlers._RESULT_DATABASE_URL = self._original_database_url
        handlers._open_result_database = self._original_open_result_database
        self._session_directory.cleanup()

    @staticmethod
    def _simulate_restart() -> None:
        handlers._result_sessions.clear()
        handlers._result_sessions.update(handlers._load_result_sessions())

    def test_primary_and_more_menus_expose_every_category(self):
        session_id = handlers._store_result_session(
            owner_id=42,
            summary="summary",
            groups=self.groups,
            working=self.results,
        )
        primary = handlers._result_menu_markup(session_id, self.groups)
        primary_actions = {
            button.callback_data.rsplit(":", 1)[-1]
            for row in primary.inline_keyboard
            for button in row
        }
        self.assertTrue(
            {"residential", "http", "socks5", "rotating", "more", "all"}
            <= primary_actions
        )

        secondary = handlers._more_categories_markup(session_id, self.groups)
        secondary_actions = {
            button.callback_data.rsplit(":", 1)[-1]
            for row in secondary.inline_keyboard
            for button in row
        }
        self.assertTrue({"socks4", "datacenter", "menu", "all"} <= secondary_actions)

    async def test_admin_receives_named_category_txt_files(self):
        groups = {
            "residential": self.results[:3],
            "http": self.results[:2],
            "socks5": [],  # empty categories must be skipped
            "socks4": [],
            "datacenter": self.results[:1],
            "rotating": [],
        }
        await handlers._send_category_files(
            self.context, 777, groups, "Tester"
        )
        by_name = {
            doc["filename"]: doc for doc in self.context.bot.documents
        }
        # One file per non-empty category, named by category + count.
        self.assertEqual(
            set(by_name),
            {"residential_3.txt", "http_2.txt", "datacenter_1.txt"},
        )
        for doc in self.context.bot.documents:
            self.assertEqual(doc["chat_id"], 777)
            self.assertIn("via Tester", doc["caption"])
        # File body carries one proxy per line.
        residential_body = by_name["residential_3.txt"]["document"].getvalue()
        self.assertEqual(len(residential_body.decode().splitlines()), 3)

    async def test_every_category_callback_shows_top_ten(self):
        session_id = handlers._store_result_session(
            owner_id=42,
            summary="summary",
            groups=self.groups,
            working=self.results,
        )
        expected_titles = {
            "residential": "RESIDENTIAL",
            "http": "HTTP/HTTPS",
            "socks5": "SOCKS5",
            "rotating": "ROTATING",
            "socks4": "SOCKS4",
            "datacenter": "DATACENTER",
        }
        for category, title in expected_titles.items():
            with self.subTest(category=category):
                query = FakeQuery(f"res:{session_id}:{category}", 42, 500)
                update = SimpleNamespace(callback_query=query)
                await handlers.handle_result_callback(update, self.context)
                self.assertEqual(len(query.edits), 1)
                text = query.edits[0][0]
                self.assertIn(title, text)
                self.assertIn("Top 10 by ping", text)
                self.assertIn("192.0.2.10", text)
                self.assertNotIn("192.0.2.11", text)

    async def test_owner_group_and_admin_authorization(self):
        owner_session = handlers._store_result_session(
            owner_id=42,
            summary="summary",
            groups=self.groups,
            working=self.results,
        )
        owner_query = FakeQuery(f"res:{owner_session}:http", 42, 500)
        await handlers.handle_result_callback(
            SimpleNamespace(callback_query=owner_query), self.context
        )
        self.assertTrue(owner_query.edits)

        other_query = FakeQuery(f"res:{owner_session}:http", 7, 500)
        await handlers.handle_result_callback(
            SimpleNamespace(callback_query=other_query), self.context
        )
        self.assertTrue(other_query.answers[-1][1])
        self.assertFalse(other_query.edits)

        group_session = handlers._store_result_session(
            owner_id=None,
            allowed_chat_ids={-100123},
            summary="summary",
            groups=self.groups,
            working=self.results,
        )
        group_query = FakeQuery(f"res:{group_session}:socks5", 999, -100123)
        await handlers.handle_result_callback(
            SimpleNamespace(callback_query=group_query), self.context
        )
        self.assertTrue(group_query.edits)

        outside_query = FakeQuery(f"res:{group_session}:socks5", 999, -200000)
        await handlers.handle_result_callback(
            SimpleNamespace(callback_query=outside_query), self.context
        )
        self.assertTrue(outside_query.answers[-1][1])
        self.assertFalse(outside_query.edits)

        admin_session = handlers._store_result_session(
            owner_id=77,
            allowed_chat_ids={77},
            summary="summary",
            groups=self.groups,
            working=self.results,
        )
        admin_query = FakeQuery(f"res:{admin_session}:rotating", 77, 77)
        await handlers.handle_result_callback(
            SimpleNamespace(callback_query=admin_query), self.context
        )
        self.assertTrue(admin_query.edits)

    async def test_export_sends_every_live_proxy_to_current_chat(self):
        session_id = handlers._store_result_session(
            owner_id=None,
            allowed_chat_ids={-100123},
            summary="summary",
            groups=self.groups,
            working=self.results,
        )
        query = FakeQuery(f"res:{session_id}:all", 999, -100123)
        await handlers.handle_result_callback(
            SimpleNamespace(callback_query=query), self.context
        )
        self.assertEqual(len(self.context.bot.documents), 1)
        document_call = self.context.bot.documents[0]
        self.assertEqual(document_call["chat_id"], -100123)
        exported = document_call["document"].getvalue().decode("utf-8")
        self.assertEqual(len(exported.splitlines()), len(self.results))

    async def test_persisted_sessions_work_after_restart_for_each_destination(self):
        sender_session = handlers._store_result_session(
            owner_id=42,
            summary="sender summary",
            groups=self.groups,
            working=self.results,
        )
        group_session = handlers._store_result_session(
            owner_id=None,
            allowed_chat_ids={-100123},
            summary="group summary",
            groups=self.groups,
            working=self.results,
        )
        admin_session = handlers._store_result_session(
            owner_id=77,
            allowed_chat_ids={77},
            summary="admin summary",
            groups=self.groups,
            working=self.results,
        )

        self._simulate_restart()

        for session_id, user_id, chat_id in (
            (sender_session, 42, 500),
            (group_session, 999, -100123),
            (admin_session, 77, 77),
        ):
            with self.subTest(session_id=session_id):
                query = FakeQuery(f"res:{session_id}:http", user_id, chat_id)
                await handlers.handle_result_callback(
                    SimpleNamespace(callback_query=query), self.context
                )
                self.assertTrue(query.edits)

        unauthorized = FakeQuery(f"res:{group_session}:http", 999, -200000)
        await handlers.handle_result_callback(
            SimpleNamespace(callback_query=unauthorized), self.context
        )
        self.assertTrue(unauthorized.answers[-1][1])
        self.assertFalse(unauthorized.edits)

    def test_expired_sessions_are_discarded_on_restart(self):
        session_id = handlers._store_result_session(
            owner_id=42,
            summary="summary",
            groups=self.groups,
            working=self.results,
        )
        handlers._result_sessions[session_id].expires_at = time.time() - 1
        handlers._persist_result_sessions()

        self._simulate_restart()

        self.assertIsNone(handlers._get_result_session(session_id))
        self.assertFalse(
            Path(handlers._RESULT_SESSION_PATH).read_text(encoding="utf-8").strip("{}")
        )

    async def test_database_sessions_survive_restart_and_expire(self):
        database_rows: dict[str, dict] = {}
        handlers._RESULT_DATABASE_URL = "postgresql://test-only"
        handlers._open_result_database = lambda: FakeDatabaseConnection(database_rows)

        valid_session = handlers._store_result_session(
            owner_id=None,
            allowed_chat_ids={-100123},
            summary="database summary",
            groups=self.groups,
            working=self.results,
        )
        expired_session = handlers._store_result_session(
            owner_id=42,
            summary="expired summary",
            groups=self.groups,
            working=self.results,
        )
        handlers._result_sessions[expired_session].expires_at = time.time() - 1
        handlers._persist_result_sessions(saved_ids={expired_session})
        Path(handlers._RESULT_SESSION_PATH).unlink()

        self._simulate_restart()

        self.assertIn(valid_session, handlers._result_sessions)
        self.assertNotIn(expired_session, handlers._result_sessions)
        self.assertNotIn(expired_session, database_rows)
        query = FakeQuery(f"res:{valid_session}:http", 999, -100123)
        await handlers.handle_result_callback(
            SimpleNamespace(callback_query=query), self.context
        )
        self.assertTrue(query.edits)

    def test_non_finite_expiry_is_rejected(self):
        session_id = handlers._store_result_session(
            owner_id=42,
            summary="summary",
            groups=self.groups,
            working=self.results,
        )
        persisted = json.loads(
            Path(handlers._RESULT_SESSION_PATH).read_text(encoding="utf-8")
        )
        persisted[session_id]["expires_at"] = float("inf")
        Path(handlers._RESULT_SESSION_PATH).write_text(
            json.dumps(persisted),
            encoding="utf-8",
        )

        self._simulate_restart()

        self.assertNotIn(session_id, handlers._result_sessions)


if __name__ == "__main__":
    unittest.main()