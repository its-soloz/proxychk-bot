"""Core Telegram command & message handlers for the proxy checker."""
from __future__ import annotations

from dataclasses import dataclass
import io
import secrets
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ContextTypes

import admin
import config
from checker import CheckResult, check_all, group_and_rank
from formatting import (
    build_category_message,
    build_forward_summary,
    build_summary,
    build_txt_export,
)
from parser import parse_proxies
from storage import store

_RESULT_TTL_SECONDS = 30 * 60
_RESULT_CACHE_MAX = 100
_RESULT_CATEGORIES = (
    "residential",
    "http",
    "socks5",
    "rotating",
    "socks4",
    "datacenter",
)


@dataclass
class _ResultSession:
    owner_id: int | None
    allowed_chat_ids: frozenset[int]
    summary: str
    groups: dict[str, list[CheckResult]]
    working: list[CheckResult]
    expires_at: float


_result_sessions: dict[str, _ResultSession] = {}


def _prune_result_sessions(now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    expired = [
        session_id
        for session_id, session in _result_sessions.items()
        if session.expires_at <= now
    ]
    for session_id in expired:
        _result_sessions.pop(session_id, None)


def _store_result_session(
    owner_id: int | None,
    summary: str,
    groups: dict[str, list[CheckResult]],
    working: list[CheckResult],
    allowed_chat_ids: set[int] | frozenset[int] | None = None,
) -> str:
    _prune_result_sessions()
    while len(_result_sessions) >= _RESULT_CACHE_MAX:
        oldest_id = min(
            _result_sessions,
            key=lambda key: _result_sessions[key].expires_at,
        )
        _result_sessions.pop(oldest_id, None)
    session_id = secrets.token_hex(5)
    _result_sessions[session_id] = _ResultSession(
        owner_id=owner_id,
        allowed_chat_ids=frozenset(allowed_chat_ids or ()),
        summary=summary,
        groups=groups,
        working=working,
        expires_at=time.monotonic() + _RESULT_TTL_SECONDS,
    )
    return session_id


def _get_result_session(session_id: str) -> _ResultSession | None:
    _prune_result_sessions()
    return _result_sessions.get(session_id)


def _result_menu_text(session: _ResultSession) -> str:
    return (
        f"{session.summary}\n\n"
        "<b>Choose a category below.</b>\n"
        "Each category shows its fastest 10 live proxies."
    )


def _result_menu_markup(
    session_id: str, groups: dict[str, list[CheckResult]]
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"🏠 Residential ({len(groups.get('residential', []))})",
                    callback_data=f"res:{session_id}:residential",
                ),
                InlineKeyboardButton(
                    f"🌐 HTTP ({len(groups.get('http', []))})",
                    callback_data=f"res:{session_id}:http",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"🧦 SOCKS5 ({len(groups.get('socks5', []))})",
                    callback_data=f"res:{session_id}:socks5",
                ),
                InlineKeyboardButton(
                    f"🔄 Rotating ({len(groups.get('rotating', []))})",
                    callback_data=f"res:{session_id}:rotating",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🗂 More: SOCKS4 / Datacenter",
                    callback_data=f"res:{session_id}:more",
                )
            ],
            [
                InlineKeyboardButton(
                    "📄 Get all live proxies (.txt)",
                    callback_data=f"res:{session_id}:all",
                )
            ],
        ]
    )


def _more_categories_markup(
    session_id: str, groups: dict[str, list[CheckResult]]
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"🧦 SOCKS4 ({len(groups.get('socks4', []))})",
                    callback_data=f"res:{session_id}:socks4",
                ),
                InlineKeyboardButton(
                    f"🏢 Datacenter ({len(groups.get('datacenter', []))})",
                    callback_data=f"res:{session_id}:datacenter",
                ),
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Back to results",
                    callback_data=f"res:{session_id}:menu",
                )
            ],
            [
                InlineKeyboardButton(
                    "📄 Get all live proxies (.txt)",
                    callback_data=f"res:{session_id}:all",
                )
            ],
        ]
    )


def _category_markup(session_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⬅️ Back to results",
                    callback_data=f"res:{session_id}:menu",
                )
            ],
            [
                InlineKeyboardButton(
                    "📄 Get all live proxies (.txt)",
                    callback_data=f"res:{session_id}:all",
                )
            ],
        ]
    )


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
    "• Browse category buttons, fastest first\n"
    "• Live proxies get forwarded automatically\n\n"
    "No limits. No cooldown. ⚡"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    store.touch_user(user.id, user.full_name)
    await update.effective_message.reply_text(WELCOME, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(WELCOME, parse_mode=ParseMode.HTML)


async def cmd_addproxy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Accept /addproxy followed by any supported proxy text format."""
    await handle_message(update, context)


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

    summary = build_summary(len(proxies), working, elapsed)

    # Summary
    if working:
        result_session_id = _store_result_session(
            owner_id=user.id,
            summary=summary,
            groups=groups,
            working=working,
        )
        result_session = _result_sessions[result_session_id]
        result_text = _result_menu_text(result_session)
        result_markup = _result_menu_markup(result_session_id, groups)
    else:
        result_text = f"{summary}\n\n😔 No live proxies found in that batch."
        result_markup = None

    try:
        await status_msg.edit_text(
            result_text,
            parse_mode=ParseMode.HTML,
            reply_markup=result_markup,
        )
    except Exception:
        await update.effective_message.reply_text(
            result_text,
            parse_mode=ParseMode.HTML,
            reply_markup=result_markup,
        )

    if not working:
        return

    # Forward live proxies to group + admin
    await _forward_live(context, working, user.full_name, origin_chat_id=chat_id)


async def handle_result_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return

    parts = query.data.split(":", 2)
    if len(parts) != 3:
        await query.answer("Invalid result action.", show_alert=True)
        return

    _, session_id, action = parts
    session = _get_result_session(session_id)
    if session is None:
        await query.answer(
            "These results expired. Send the proxies again to create a new result.",
            show_alert=True,
        )
        return
    callback_chat_id = getattr(query.message, "chat_id", None)
    owner_allowed = (
        session.owner_id is not None and query.from_user.id == session.owner_id
    )
    chat_allowed = callback_chat_id in session.allowed_chat_ids
    if not owner_allowed and not chat_allowed:
        await query.answer(
            "This result menu is only available to its owner or inside the chat "
            "where it was forwarded.",
            show_alert=True,
        )
        return

    if action == "all":
        await query.answer("Preparing your text file…")
        export = build_txt_export(session.working)
        bio = io.BytesIO(export.encode("utf-8"))
        bio.name = "live_proxies.txt"
        await context.bot.send_document(
            chat_id=query.message.chat_id,
            document=bio,
            filename="live_proxies.txt",
            caption=(
                f"✅ {len(session.working)} live proxies "
                "(all categories, sorted by ping)."
            ),
        )
        return

    if action == "menu":
        await query.answer()
        await query.edit_message_text(
            _result_menu_text(session),
            parse_mode=ParseMode.HTML,
            reply_markup=_result_menu_markup(session_id, session.groups),
        )
        return

    if action == "more":
        await query.answer()
        await query.edit_message_text(
            "🗂 <b>MORE PROXY CATEGORIES</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Choose SOCKS4 or datacenter proxies below. "
            "Each category shows its fastest 10 live proxies.",
            parse_mode=ParseMode.HTML,
            reply_markup=_more_categories_markup(session_id, session.groups),
        )
        return

    if action not in _RESULT_CATEGORIES:
        await query.answer("Unknown proxy category.", show_alert=True)
        return

    await query.answer()
    await query.edit_message_text(
        build_category_message(action, session.groups.get(action, []), top_n=10),
        parse_mode=ParseMode.HTML,
        reply_markup=_category_markup(session_id),
    )


async def _forward_live(context, working, source_name: str, origin_chat_id: int) -> None:
    settings = store.settings
    to_send = working
    if settings.get("only_forward_residential"):
        to_send = [r for r in working if r.is_residential] or working
    min_lat = settings.get("min_latency_forward", 0)
    if min_lat:
        to_send = [r for r in to_send if (r.latency_ms or 0) <= min_lat] or to_send

    groups, ranked = group_and_rank(to_send)
    summary = build_forward_summary(ranked, source_name)
    if not summary:
        return

    # to group (skip if the check already happened in that same group)
    if settings.get("forward_to_group") and config.GROUP_ID and origin_chat_id != config.GROUP_ID:
        group_session_id = _store_result_session(
            owner_id=None,
            allowed_chat_ids={config.GROUP_ID},
            summary=summary,
            groups=groups,
            working=ranked,
        )
        group_session = _result_sessions[group_session_id]
        try:
            await context.bot.send_message(
                config.GROUP_ID,
                _result_menu_text(group_session),
                parse_mode=ParseMode.HTML,
                reply_markup=_result_menu_markup(group_session_id, groups),
            )
        except Exception:
            _result_sessions.pop(group_session_id, None)

    # to admin (skip if admin is the origin)
    if settings.get("forward_to_admin") and config.ADMIN_ID and origin_chat_id != config.ADMIN_ID:
        admin_session_id = _store_result_session(
            owner_id=config.ADMIN_ID,
            allowed_chat_ids={config.ADMIN_ID},
            summary=summary,
            groups=groups,
            working=ranked,
        )
        admin_session = _result_sessions[admin_session_id]
        try:
            await context.bot.send_message(
                config.ADMIN_ID,
                _result_menu_text(admin_session),
                parse_mode=ParseMode.HTML,
                reply_markup=_result_menu_markup(admin_session_id, groups),
            )
        except Exception:
            _result_sessions.pop(admin_session_id, None)
