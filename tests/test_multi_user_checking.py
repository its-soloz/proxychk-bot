from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import checker
import handlers
from checker import CheckResult
from parser import ParsedProxy, parse_proxies


class MultiUserCheckingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        checker._global_check_semaphore = None
        checker._global_check_loop = None

    def test_addproxy_style_colon_credentials_are_parsed(self):
        text = (
            "/addproxy\n\n"
            "74.122.59.184:42418:pOBJOO0hIXndyTS:y7eysZ1TFpg0WiX\n"
            "74.122.57.172:44087:Tn28qJZfUNQh4Sm:MttoFGLMv0XEidb\n"
            "45.45.197.155:44049:60eE6jOPMQqzznh:sMkgRlvZLdnkpQL\n"
        )
        proxies = parse_proxies(text)
        self.assertEqual(len(proxies), 3)
        self.assertEqual(proxies[0].host, "74.122.59.184")
        self.assertEqual(proxies[0].port, 42418)
        self.assertEqual(proxies[0].username, "pOBJOO0hIXndyTS")
        self.assertEqual(proxies[0].password, "y7eysZ1TFpg0WiX")
        self.assertEqual(
            proxies[0].input_line,
            "74.122.59.184:42418:pOBJOO0hIXndyTS:y7eysZ1TFpg0WiX",
        )

    async def test_addproxy_command_uses_the_normal_check_flow(self):
        update = SimpleNamespace()
        context = SimpleNamespace()
        with patch.object(handlers, "handle_message", AsyncMock()) as handle:
            await handlers.cmd_addproxy(update, context)
        handle.assert_awaited_once_with(update, context)

    async def test_second_user_starts_while_first_batch_is_running(self):
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        release = asyncio.Event()
        active_first = 0

        async def fake_check_one(session, proxy):
            nonlocal active_first
            if proxy.host.startswith("10.0.0."):
                active_first += 1
                if active_first == 2:
                    first_started.set()
            else:
                second_started.set()
            await release.wait()
            return CheckResult(proxy=proxy)

        first_batch = [ParsedProxy(f"10.0.0.{index + 1}", 8000) for index in range(4)]
        second_batch = [ParsedProxy("10.1.0.1", 9000)]
        loop = asyncio.get_running_loop()
        with (
            patch.object(checker.config, "MAX_CONCURRENCY", 4),
            patch.object(checker.config, "BATCH_CONCURRENCY", 2),
            patch.object(checker, "check_one", fake_check_one),
            patch.object(checker, "_global_check_semaphore", asyncio.Semaphore(4)),
            patch.object(checker, "_global_check_loop", loop),
        ):
            first_task = asyncio.create_task(checker.check_all(first_batch))
            await asyncio.wait_for(first_started.wait(), timeout=1)
            second_task = asyncio.create_task(checker.check_all(second_batch))
            await asyncio.wait_for(second_started.wait(), timeout=1)
            release.set()
            first_results, second_results = await asyncio.gather(first_task, second_task)

        self.assertEqual(len(first_results), 4)
        self.assertEqual(len(second_results), 1)


if __name__ == "__main__":
    unittest.main()