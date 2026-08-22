from __future__ import annotations

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
        handlers._result_sessions.clear()
        self.results = make_results()
        self.groups = {
            category: list(self.results)
            for category in handlers._RESULT_CATEGORIES
        }
        self.context = SimpleNamespace(bot=FakeBot())

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


if __name__ == "__main__":
    unittest.main()