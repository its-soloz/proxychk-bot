from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from checker import CheckResult, Protocol, group_and_rank
import daily_scheduler
from delivery import build_logs_album, send_logs_album
from lifetime_store import LifetimeProxyStore
from parser import ParsedProxy


def working_result(
    host: str,
    protocol: Protocol,
    latency: int,
    *,
    residential: bool = False,
    datacenter: bool = False,
    rotating: bool = False,
) -> CheckResult:
    return CheckResult(
        proxy=ParsedProxy(host, 8080),
        working=True,
        protocol=protocol,
        latency_ms=latency,
        is_residential=residential,
        is_datacenter=datacenter,
        is_rotating=rotating,
    )


class FakeAlbumBot:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send_media_group(self, **kwargs):
        self.calls.append(kwargs)


class LifetimeLogsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "lifetime.sqlite3")
        self.store = LifetimeProxyStore(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    async def test_logs_are_one_album_with_named_sorted_files_and_caption(self):
        results = [
            working_result(
                "192.0.2.2",
                Protocol.HTTP,
                200,
                residential=True,
            ),
            working_result(
                "192.0.2.1",
                Protocol.HTTP,
                50,
                residential=True,
                rotating=True,
            ),
            working_result(
                "192.0.2.3",
                Protocol.SOCKS5,
                100,
                datacenter=True,
            ),
        ]
        groups, ranked = group_and_rank(results)
        album = build_logs_album("Alice <admin>", 5, ranked, groups)

        filenames = [item.media.filename for item in album]
        self.assertEqual(
            filenames,
            [
                "residential.txt",
                "http.txt",
                "socks5.txt",
                "datacenter.txt",
                "rotating.txt",
                "all_working_proxies.txt",
            ],
        )
        self.assertIn("Alice &lt;admin&gt;", album[0].caption)
        self.assertIn("Total checked: <b>5</b>", album[0].caption)
        self.assertIn("Working: <b>3</b>", album[0].caption)
        self.assertIn("Dead: <b>2</b>", album[0].caption)

        all_body = album[-1].media.input_file_content.decode("utf-8").splitlines()
        self.assertIn("192.0.2.1", all_body[0])
        self.assertIn("192.0.2.3", all_body[1])
        self.assertIn("192.0.2.2", all_body[2])

        bot = FakeAlbumBot()
        sent = await send_logs_album(bot, -100123, "Alice", 5, ranked, groups)
        self.assertTrue(sent)
        self.assertEqual(len(bot.calls), 1)
        self.assertEqual(bot.calls[0]["chat_id"], -100123)
        self.assertEqual(len(bot.calls[0]["media"]), len(album))

    def test_sqlite_bulk_insert_deduplicates_and_reconcile_trims_dead(self):
        first = working_result("192.0.2.1", Protocol.HTTP, 50)
        second = working_result("192.0.2.2", Protocol.SOCKS5, 60)

        self.assertEqual(
            self.store.add_working([first, first, second], timestamp=1000),
            2,
        )
        self.assertEqual(self.store.add_working([first], timestamp=2000), 0)
        self.assertEqual(self.store.count(), 2)

        checked = self.store.load_all()
        survivor = working_result("192.0.2.2", Protocol.SOCKS4, 40)
        kept, removed = self.store.reconcile(
            checked,
            [CheckResult(proxy=checked[0]), survivor],
            timestamp=3000,
        )
        self.assertEqual((kept, removed), (1, 1))
        loaded = self.store.load_all()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].host, "192.0.2.2")
        self.assertEqual(loaded[0].scheme_hint, "socks4")

    def test_daily_snapshot_cannot_delete_a_newer_user_confirmation(self):
        live = working_result("192.0.2.10", Protocol.HTTP, 50)
        self.store.add_working([live], timestamp=1000)
        checked_snapshot = self.store.load_all()

        # A user confirms the same proxy after the daily snapshot was taken.
        self.store.add_working([live], timestamp=2000)
        kept, removed = self.store.reconcile(
            checked_snapshot,
            [CheckResult(proxy=checked_snapshot[0])],
            timestamp=3000,
            snapshot_started_at=1500,
        )

        self.assertEqual((kept, removed), (0, 0))
        self.assertEqual(self.store.count(), 1)

    async def test_daily_job_rechecks_reconciles_and_sends_same_album(self):
        proxies = [
            ParsedProxy("192.0.2.1", 8080, scheme_hint="http"),
            ParsedProxy("192.0.2.2", 8080, scheme_hint="socks5"),
        ]
        results = [
            working_result("192.0.2.1", Protocol.HTTP, 50),
            CheckResult(proxy=proxies[1]),
        ]
        fake_store = unittest.mock.Mock()
        fake_store.load_all.return_value = proxies
        fake_store.reconcile.return_value = (1, 1)
        send = AsyncMock(return_value=True)

        with (
            patch.object(daily_scheduler, "lifetime_store", fake_store),
            patch.object(
                daily_scheduler,
                "check_all",
                AsyncMock(return_value=results),
            ),
            patch.object(daily_scheduler, "send_logs_album", send),
            patch.object(daily_scheduler.config, "GROUP_ID", -100123),
        ):
            outcome = await daily_scheduler.run_daily_lifetime_recheck(
                FakeAlbumBot()
            )

        self.assertEqual(outcome, (1, 1))
        fake_store.reconcile.assert_called_once()
        self.assertEqual(fake_store.reconcile.call_args.args, (proxies, results))
        self.assertIn(
            "snapshot_started_at",
            fake_store.reconcile.call_args.kwargs,
        )
        send.assert_awaited_once()
        self.assertEqual(send.await_args.args[1], -100123)
        self.assertEqual(send.await_args.args[3], 2)
        self.assertTrue(send.await_args.kwargs["daily"])

    def test_next_run_is_9_pm_delhi(self):
        now = datetime(
            2026,
            8,
            28,
            20,
            30,
            tzinfo=ZoneInfo("Asia/Kolkata"),
        )
        with (
            patch.object(daily_scheduler.config, "DAILY_LOG_HOUR", 21),
            patch.object(daily_scheduler.config, "DAILY_LOG_MINUTE", 0),
            patch.object(
                daily_scheduler.config,
                "DAILY_LOG_TIMEZONE",
                "Asia/Kolkata",
            ),
        ):
            self.assertEqual(
                daily_scheduler.seconds_until_next_run(now),
                30 * 60,
            )


if __name__ == "__main__":
    unittest.main()