"""Reusable Telegram document-album delivery for proxy logs."""
from __future__ import annotations

from html import escape
import io

from telegram import InputMediaDocument
from telegram.constants import ParseMode

from checker import CheckResult
from formatting import build_txt_export


EXPORT_CATEGORIES = (
    ("residential", "residential.txt", "Residential"),
    ("http", "http.txt", "HTTP"),
    ("socks4", "socks4.txt", "SOCKS4"),
    ("socks5", "socks5.txt", "SOCKS5"),
    ("datacenter", "datacenter.txt", "Datacenter"),
    ("rotating", "rotating.txt", "Rotating"),
)


def build_logs_caption(
    source_name: str,
    total_checked: int,
    working: list[CheckResult],
    groups: dict[str, list[CheckResult]],
    *,
    daily: bool = False,
) -> str:
    """Build the detailed caption attached to the first album document."""
    heading = "DAILY LIFETIME PROXY RECHECK" if daily else "PROXY CHECK LOGS"
    dead = max(0, total_checked - len(working))
    counts = {category: len(groups.get(category, [])) for category, _, _ in EXPORT_CATEGORIES}
    return (
        f"📦 <b>{heading}</b>\n"
        f"👤 Checked by: <b>{escape(source_name)}</b>\n"
        f"📊 Total checked: <b>{total_checked}</b>\n"
        f"✅ Working: <b>{len(working)}</b>   ❌ Dead: <b>{dead}</b>\n"
        f"🏠 Residential: <b>{counts['residential']}</b>   "
        f"🏢 Datacenter: <b>{counts['datacenter']}</b>\n"
        f"🌐 HTTP: <b>{counts['http']}</b>   "
        f"🧦 SOCKS4: <b>{counts['socks4']}</b>   "
        f"🧦 SOCKS5: <b>{counts['socks5']}</b>\n"
        f"🔄 Rotating: <b>{counts['rotating']}</b>"
    )


def build_logs_album(
    source_name: str,
    total_checked: int,
    working: list[CheckResult],
    groups: dict[str, list[CheckResult]],
    *,
    daily: bool = False,
) -> list[InputMediaDocument]:
    """Build one Telegram album containing category files and a combined file."""
    if not working:
        return []

    files: list[tuple[str, list[CheckResult]]] = [
        (filename, groups.get(category, []))
        for category, filename, _ in EXPORT_CATEGORIES
        if groups.get(category)
    ]
    files.append(("all_working_proxies.txt", working))

    caption = build_logs_caption(
        source_name,
        total_checked,
        working,
        groups,
        daily=daily,
    )
    album: list[InputMediaDocument] = []
    for index, (filename, results) in enumerate(files):
        stream = io.BytesIO(build_txt_export(results).encode("utf-8"))
        stream.name = filename
        album.append(
            InputMediaDocument(
                media=stream,
                filename=filename,
                caption=caption if index == 0 else None,
                parse_mode=ParseMode.HTML if index == 0 else None,
            )
        )
    return album


async def send_logs_album(
    bot,
    chat_id: int,
    source_name: str,
    total_checked: int,
    working: list[CheckResult],
    groups: dict[str, list[CheckResult]],
    *,
    daily: bool = False,
) -> bool:
    """Send all result files in one sendMediaGroup request."""
    album = build_logs_album(
        source_name,
        total_checked,
        working,
        groups,
        daily=daily,
    )
    if not album:
        return False
    await bot.send_media_group(chat_id=chat_id, media=album)
    return True