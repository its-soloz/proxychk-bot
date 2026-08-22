from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp

import checker
from checker import CheckResult, Protocol
from parser import ParsedProxy


class CheckerTimeoutTests(unittest.IsolatedAsyncioTestCase):
    def test_request_timeout_uses_targeted_budgets(self):
        with (
            patch.object(checker.config, "CHECK_TIMEOUT", 7),
            patch.object(checker.config, "CONNECT_TIMEOUT", 3),
            patch.object(checker.config, "READ_TIMEOUT", 4),
        ):
            timeout = checker._request_timeout()
        self.assertEqual(timeout.total, 7)
        self.assertEqual(timeout.connect, 3)
        self.assertEqual(timeout.sock_connect, 3)
        self.assertEqual(timeout.sock_read, 4)

    def test_short_request_caps_connect_and_read_budgets(self):
        with (
            patch.object(checker.config, "CONNECT_TIMEOUT", 5),
            patch.object(checker.config, "READ_TIMEOUT", 6),
        ):
            timeout = checker._request_timeout(2)
        self.assertEqual(timeout.total, 2)
        self.assertEqual(timeout.connect, 2)
        self.assertEqual(timeout.sock_connect, 2)
        self.assertEqual(timeout.sock_read, 2)

    async def test_rotation_probe_uses_its_short_timeout(self):
        proxy = ParsedProxy("192.0.2.1", 8080)
        fetch = AsyncMock(return_value=(200, '{"ip":"198.51.100.2"}'))
        with (
            patch.object(checker, "_fetch_judge", fetch),
            patch.object(checker.config, "ROTATION_TIMEOUT", 3),
        ):
            rotating = await checker._detect_rotating(
                AsyncMock(spec=aiohttp.ClientSession),
                Protocol.HTTP,
                proxy,
                "198.51.100.1",
            )
        self.assertTrue(rotating)
        self.assertEqual(fetch.await_args.kwargs["timeout_total"], 3)

    async def test_protocol_fallback_and_classification_are_preserved(self):
        proxy = ParsedProxy("192.0.2.1", 8080)
        responses = [
            asyncio.TimeoutError(),
            (
                200,
                '{"status":"success","query":"198.51.100.1",'
                '"country":"Testland","countryCode":"TS","city":"Fast City",'
                '"isp":"Home ISP","org":"Home ISP","hosting":false}',
            ),
            (200, '{"ip":"198.51.100.2"}'),
        ]
        fetch = AsyncMock(side_effect=responses)
        with patch.object(checker, "_fetch_judge", fetch):
            result = await checker.check_one(
                AsyncMock(spec=aiohttp.ClientSession), proxy
            )
        self.assertTrue(result.working)
        self.assertIs(result.protocol, Protocol.SOCKS5)
        self.assertTrue(result.is_residential)
        self.assertTrue(result.is_rotating)

    async def test_batch_concurrency_remains_bounded(self):
        active = 0
        peak = 0

        async def fake_check_one(session, proxy):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.005)
            active -= 1
            return CheckResult(proxy=proxy)

        proxies = [ParsedProxy(f"192.0.2.{index + 1}", 8000) for index in range(20)]
        with (
            patch.object(checker, "check_one", fake_check_one),
            patch.object(checker.config, "MAX_CONCURRENCY", 4),
        ):
            results = await checker.check_all(proxies)
        self.assertEqual(len(results), len(proxies))
        self.assertEqual(peak, 4)


if __name__ == "__main__":
    unittest.main()