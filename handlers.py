"""Core Telegram command & message handlers for the proxy checker."""
from __future__ import annotations

import io
import time

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ContextTypes

import admin
import config
from checker import check_all, group_and_rank
from formatting import (
    build_category_messages,
    build_forward_message,
    build_summary,
    build_txt_export,
)
from parser import parse_proxies
from storage import store

WELCOME = (
    "🛰 <b>ADVANCED PROXY CHECKER</b>\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "Just paste proxies in <i>any</i> format — messy text, JSON, .txt files, "
    "with or without auth or scheme. I'll extract, validate and rank them.\n\n"
    "<b>What I detect</b>\n"
    "• Protocol: HTTP / SOCKS4 / SOCKS5\n"
    "• Type: residential / datacenter / rotating\n"
    "• Ping (latency), country & ISP\n\n"
    "<b>How to use</b>\n"
    "• Paste proxies or send a .txt / .json file\n"
    "• Results come grouped, fastest first\n"
    "• Live proxies get forwarded automatically\n\n"
    "No limits. No cooldown. ⚡"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    store.touch_user(user.id, user.full_name)
    await update.effective_message.reply_text(WELCOME, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(WELCOME, parse_mode=ParseMode.HTML)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("awaiting_broadcast", None)
    await update.effective_message.reply_text("Cancelled.")


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not admin.is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /ban <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Invalid user id.")
        return
    store.ban(uid)
    await update.effective_message.reply_text(f"🚫 Banned <code>{uid}</code>.", parse_mode=ParseMode.HTML)


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not admin.is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /unban <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Invalid user id.")
        return
    store.unban(uid)
    await update.effective_message.reply_text(f"✅ Unbanned <code>{uid}</code>.", parse_mode=ParseMode.HTML)


async def _extract_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Pull raw text from a message and/or an attached document."""
    msg = update.effective_message
    text = msg.text or msg.caption or ""
    if msg.document:
        doc = msg.document
        # only try to read reasonably sized text-ish files
        if doc.file_size and doc.file_size <= 5 * 1024 * 1024:
            try:
                f = await doc.get_file()
                data = await f.download_as_bytearray()
                text += "\n" + data.decode("utf-8", errors="ignore")
            except Exception:
                pass
    return text


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if store.is_banned(user.id):
        return

    # Admin broadcast composing takes priority.
    if await admin.handle_broadcast_message(update, context):
        return

    store.touch_user(user.id, user.full_name)
    text = await _extract_text(update, context)
    proxies = parse_proxies(text)

    if not proxies:
        # Don't spam group chats with "no proxies" — only reply in private.
        if update.effective_chat.type == "private":
            await update.effective_message.reply_text(
                "🤔 I couldn't find any proxies in that. Paste them in any format "
                "(ip:port, user:pass@ip:port, scheme://…, JSON, or a .txt file)."
            )
        return

    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)

    status_msg = await update.effective_message.reply_text(
        f"🔍 Extracted <b>{len(proxies)}</b> proxies. Checking…",
        parse_mode=ParseMode.HTML,
    )

    # throttle progress edits
    last_edit = {"t": 0.0}

    async def progress(done: int, total: int):
        now = time.time()
        if now - last_edit["t"] < 1.5 and done != total:
            return
        last_edit["t"] = now
        bar_len = 12
        filled = int(bar_len * done / total) if total else bar_len
        bar = "█" * filled + "░" * (bar_len - filled)
        try:
            await status_msg.edit_text(
                f"🔍 Checking proxies…\n<code>{bar}</code> {done}/{total}",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    started = time.perf_counter()
    results = await check_all(proxies, progress)
    elapsed = time.perf_counter() - started

    groups, working = group_and_rank(results)
    store.record_check(user.id, len(proxies), len(working))

    # Summary
    try:
        await status_msg.edit_text(
            build_summary(len(proxies), working, elapsed), parse_mode=ParseMode.HTML
        )
    except Exception:
        await update.effective_message.reply_text(
            build_summary(len(proxies), working, elapsed), parse_mode=ParseMode.HTML
        )

    if not working:
        await update.effective_message.reply_text("😔 No live proxies found in that batch.")
        return

    # Category breakdowns (SOCKS5 / SOCKS4 / HTTP / residential / datacenter / rotating)
    for m in build_category_messages(groups):
        await update.effective_message.reply_text(m, parse_mode=ParseMode.HTML)

    # Full export as a .txt (best ping first)
    export = build_txt_export(working)
    bio = io.BytesIO(export.encode("utf-8"))
    bio.name = "live_proxies.txt"
    await update.effective_message.reply_document(
        document=bio,
        filename="live_proxies.txt",
        caption=f"✅ {len(working)} live proxies (sorted by ping).",
    )

    # Forward live proxies to group + admin
    await _forward_live(context, working, user.full_name, origin_chat_id=chat_id)


async def _forward_live(context, working, source_name: str, origin_chat_id: int) -> None:
    settings = store.settings
    to_send = working
    if settings.get("only_forward_residential"):
        to_send = [r for r in working if r.is_residential] or working
    min_lat = settings.get("min_latency_forward", 0)
    if min_lat:
        to_send = [r for r in to_send if (r.latency_ms or 0) <= min_lat] or to_send

    msg = build_forward_message(to_send, source_name)
    if not msg:
        return

    # to group (skip if the check already happened in that same group)
    if settings.get("forward_to_group") and config.GROUP_ID and origin_chat_id != config.GROUP_ID:
        try:
            await context.bot.send_message(config.GROUP_ID, msg, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    # to admin (skip if admin is the origin)
    if settings.get("forward_to_admin") and config.ADMIN_ID and origin_chat_id != config.ADMIN_ID:
        try:
            await context.bot.send_message(config.ADMIN_ID, msg, parse_mode=ParseMode.HTML)
        except Exception:
            pass
