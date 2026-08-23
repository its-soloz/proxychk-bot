"""Core Telegram command & message handlers for the proxy checker."""
from __future__ import annotations

from dataclasses import dataclass
import io
import json
import logging
import math
import os
import secrets
import time
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ContextTypes

import admin
import config
from checker import CheckResult, Protocol, check_all, group_and_rank
from formatting import (
    build_category_message,
    build_forward_summary,
    build_summary,
    build_txt_export,
)
from parser import ParsedProxy, parse_proxies
from storage import store

_RESULT_TTL_SECONDS = 30 * 60
_RESULT_CACHE_MAX = 100
_RESULT_SESSION_PATH = os.getenv(
    "RESULT_SESSION_PATH",
    os.path.join(os.path.dirname(__file__), "result_sessions.json"),
)
_RESULT_DATABASE_URL = os.getenv("RESULT_DATABASE_URL", "").strip()
_RESULT_TABLE = "proxy_result_sessions"
_RESULT_CATEGORIES = (
    "residential",
    "http",
    "socks5",
    "rotating",
    "socks4",
    "datacenter",
)

logger = logging.getLogger(__name__)


@dataclass
class _ResultSession:
    owner_id: int | None
    allowed_chat_ids: frozenset[int]
    summary: str
    groups: dict[str, list[CheckResult]]
    working: list[CheckResult]
    expires_at: float


def _serialize_check_result(result: CheckResult) -> dict[str, Any]:
    """Convert a checked proxy to the safe JSON shape used for result menus."""
    return {
        "proxy": {
            "host": result.proxy.host,
            "port": result.proxy.port,
            "username": result.proxy.username,
            "password": result.proxy.password,
            "scheme_hint": result.proxy.scheme_hint,
        },
        "working": result.working,
        "protocol": result.protocol.value if result.protocol else None,
        "latency_ms": result.latency_ms,
        "exit_ip": result.exit_ip,
        "country": result.country,
        "country_code": result.country_code,
        "city": result.city,
        "isp": result.isp,
        "org": result.org,
        "is_datacenter": result.is_datacenter,
        "is_residential": result.is_residential,
        "is_rotating": result.is_rotating,
        "error": result.error,
    }


def _deserialize_check_result(data: object) -> CheckResult:
    """Restore a checked proxy from a persisted result-menu record."""
    if not isinstance(data, dict):
        raise ValueError("Result is not an object")
    proxy_data = data.get("proxy")
    if not isinstance(proxy_data, dict):
        raise ValueError("Result is missing proxy data")

    host = proxy_data.get("host")
    port = proxy_data.get("port")
    if not isinstance(host, str) or not isinstance(port, int) or not 0 < port <= 65535:
        raise ValueError("Result has an invalid proxy address")

    protocol_value = data.get("protocol")
    if protocol_value is not None and protocol_value not in {p.value for p in Protocol}:
        raise ValueError("Result has an invalid protocol")

    def optional_string(value: object) -> str | None:
        return value if isinstance(value, str) else None

    return CheckResult(
        proxy=ParsedProxy(
            host=host,
            port=port,
            username=optional_string(proxy_data.get("username")),
            password=optional_string(proxy_data.get("password")),
            scheme_hint=optional_string(proxy_data.get("scheme_hint")),
        ),
        working=bool(data.get("working")),
        protocol=Protocol(protocol_value) if protocol_value else None,
        latency_ms=data.get("latency_ms")
        if isinstance(data.get("latency_ms"), int)
        else None,
        exit_ip=optional_string(data.get("exit_ip")),
        country=optional_string(data.get("country")),
        country_code=optional_string(data.get("country_code")),
        city=optional_string(data.get("city")),
        isp=optional_string(data.get("isp")),
        org=optional_string(data.get("org")),
        is_datacenter=bool(data.get("is_datacenter")),
        is_residential=bool(data.get("is_residential")),
        is_rotating=bool(data.get("is_rotating")),
        error=optional_string(data.get("error")),
    )


def _serialize_result_session(session: _ResultSession) -> dict[str, Any]:
    return {
        "owner_id": session.owner_id,
        "allowed_chat_ids": sorted(session.allowed_chat_ids),
        "summary": session.summary,
        "working": [_serialize_check_result(result) for result in session.working],
        "expires_at": session.expires_at,
    }


def _deserialize_result_session(data: object) -> _ResultSession:
    if not isinstance(data, dict):
        raise ValueError("Session is not an object")

    owner_id = data.get("owner_id")
    if owner_id is not None and (not isinstance(owner_id, int) or isinstance(owner_id, bool)):
        raise ValueError("Session has an invalid owner")
    allowed_chat_ids = data.get("allowed_chat_ids")
    if not isinstance(allowed_chat_ids, list) or not all(
        isinstance(chat_id, int) and not isinstance(chat_id, bool)
        for chat_id in allowed_chat_ids
    ):
        raise ValueError("Session has invalid allowed chats")
    summary = data.get("summary")
    working_data = data.get("working")
    expires_at = data.get("expires_at")
    if (
        not isinstance(summary, str)
        or not isinstance(working_data, list)
        or not isinstance(expires_at, (int, float))
    ):
        raise ValueError("Session has invalid content")
    expires_at = float(expires_at)
    if (
        not math.isfinite(expires_at)
        or expires_at > time.time() + _RESULT_TTL_SECONDS
    ):
        raise ValueError("Session has an invalid expiry")

    working = [_deserialize_check_result(result) for result in working_data]
    groups, working = group_and_rank(working)
    return _ResultSession(
        owner_id=owner_id,
        allowed_chat_ids=frozenset(allowed_chat_ids),
        summary=summary,
        groups=groups,
        working=working,
        expires_at=expires_at,
    )


def _load_file_result_sessions() -> dict[str, _ResultSession]:
    """Load unexpired sessions from the local fallback file."""
    try:
        with open(_RESULT_SESSION_PATH, "r", encoding="utf-8") as session_file:
            raw_sessions = json.load(session_file)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("Could not load persisted result sessions: %s", error)
        return {}

    if not isinstance(raw_sessions, dict):
        logger.warning("Ignoring result session file with an invalid top-level shape")
        return {}

    now = time.time()
    sessions: dict[str, _ResultSession] = {}
    for session_id, raw_session in raw_sessions.items():
        if not isinstance(session_id, str):
            continue
        try:
            session = _deserialize_result_session(raw_session)
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid persisted result session %r", session_id)
            continue
        if session.expires_at > now:
            sessions[session_id] = session
    return sessions


def _write_result_sessions(sessions: dict[str, _ResultSession]) -> None:
    """Atomically write a given set of short-lived result-menu sessions to disk."""
    directory = os.path.dirname(_RESULT_SESSION_PATH)
    temporary_path = f"{_RESULT_SESSION_PATH}.tmp"
    try:
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(temporary_path, "w", encoding="utf-8") as session_file:
            os.chmod(temporary_path, 0o600)
            json.dump(
                {
                    session_id: _serialize_result_session(session)
                    for session_id, session in sessions.items()
                },
                session_file,
                separators=(",", ":"),
            )
        os.replace(temporary_path, _RESULT_SESSION_PATH)
    except OSError as error:
        logger.warning("Could not persist result sessions: %s", error)
        try:
            os.unlink(temporary_path)
        except OSError:
            pass


def _open_result_database():
    import psycopg

    return psycopg.connect(_RESULT_DATABASE_URL, connect_timeout=5)


def _ensure_result_table(connection) -> None:
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_RESULT_TABLE} (
            session_id TEXT PRIMARY KEY,
            expires_at DOUBLE PRECISION NOT NULL,
            payload JSONB NOT NULL
        )
        """
    )


def _load_database_result_sessions() -> tuple[bool, dict[str, _ResultSession]]:
    """Load the durable Render/Postgres cache; report whether it was reachable."""
    if not _RESULT_DATABASE_URL:
        return False, {}

    now = time.time()
    invalid_ids: list[str] = []
    sessions: dict[str, _ResultSession] = {}
    try:
        with _open_result_database() as connection:
            _ensure_result_table(connection)
            connection.execute(
                f"""
                DELETE FROM {_RESULT_TABLE}
                WHERE expires_at <= %s OR expires_at > %s
                """,
                (now, now + _RESULT_TTL_SECONDS),
            )
            rows = connection.execute(
                f"SELECT session_id, payload FROM {_RESULT_TABLE}"
            ).fetchall()
            for session_id, payload in rows:
                try:
                    session = _deserialize_result_session(payload)
                except (TypeError, ValueError):
                    logger.warning(
                        "Ignoring invalid database result session %r", session_id
                    )
                    invalid_ids.append(session_id)
                    continue
                if session.expires_at > now:
                    sessions[session_id] = session
            if invalid_ids:
                connection.execute(
                    f"DELETE FROM {_RESULT_TABLE} WHERE session_id = ANY(%s)",
                    (invalid_ids,),
                )
    except Exception as error:
        logger.warning("Could not load database result sessions: %s", error)
        return False, {}

    if len(sessions) > _RESULT_CACHE_MAX:
        keep_ids = {
            session_id
            for session_id, _ in sorted(
                sessions.items(),
                key=lambda item: item[1].expires_at,
                reverse=True,
            )[:_RESULT_CACHE_MAX]
        }
        discarded_ids = set(sessions) - keep_ids
        sessions = {
            session_id: session
            for session_id, session in sessions.items()
            if session_id in keep_ids
        }
        _delete_database_result_sessions(discarded_ids)
    return True, sessions


def _save_database_result_sessions(session_ids: set[str]) -> None:
    if not _RESULT_DATABASE_URL or not session_ids:
        return
    rows = [
        (
            session_id,
            _result_sessions[session_id].expires_at,
            json.dumps(
                _serialize_result_session(_result_sessions[session_id]),
                separators=(",", ":"),
            ),
        )
        for session_id in session_ids
        if session_id in _result_sessions
    ]
    if not rows:
        return
    try:
        with _open_result_database() as connection:
            _ensure_result_table(connection)
            with connection.cursor() as cursor:
                cursor.executemany(
                    f"""
                    INSERT INTO {_RESULT_TABLE} (session_id, expires_at, payload)
                    VALUES (%s, %s, %s::jsonb)
                    ON CONFLICT (session_id) DO UPDATE SET
                        expires_at = EXCLUDED.expires_at,
                        payload = EXCLUDED.payload
                    """,
                    rows,
                )
    except Exception as error:
        logger.warning("Could not persist database result sessions: %s", error)


def _delete_database_result_sessions(session_ids: set[str]) -> None:
    if not _RESULT_DATABASE_URL or not session_ids:
        return
    try:
        with _open_result_database() as connection:
            _ensure_result_table(connection)
            connection.execute(
                f"DELETE FROM {_RESULT_TABLE} WHERE session_id = ANY(%s)",
                (list(session_ids),),
            )
    except Exception as error:
        logger.warning("Could not delete database result sessions: %s", error)


def _load_result_sessions() -> dict[str, _ResultSession]:
    """Load durable sessions, falling back to the local file outside Render."""
    database_available, database_sessions = _load_database_result_sessions()
    if database_available:
        _write_result_sessions(database_sessions)
        return database_sessions

    file_sessions = _load_file_result_sessions()
    _write_result_sessions(file_sessions)
    return file_sessions


_result_sessions: dict[str, _ResultSession] = _load_result_sessions()


def _persist_result_sessions(
    saved_ids: set[str] | None = None,
    deleted_ids: set[str] | None = None,
) -> None:
    """Save the local fallback and apply the same changes to durable storage."""
    _write_result_sessions(_result_sessions)
    if saved_ids is None:
        saved_ids = set(_result_sessions)
    _save_database_result_sessions(saved_ids)
    _delete_database_result_sessions(deleted_ids or set())


def _delete_result_sessions(session_ids: set[str]) -> None:
    for session_id in session_ids:
        _result_sessions.pop(session_id, None)
    if session_ids:
        _persist_result_sessions(saved_ids=set(), deleted_ids=session_ids)


def _prune_result_sessions(now: float | None = None) -> None:
    now = time.time() if now is None else now
    expired = [
        session_id
        for session_id, session in _result_sessions.items()
        if session.expires_at <= now
    ]
    _delete_result_sessions(set(expired))


def _store_result_session(
    owner_id: int | None,
    summary: str,
    groups: dict[str, list[CheckResult]],
    working: list[CheckResult],
    allowed_chat_ids: set[int] | frozenset[int] | None = None,
) -> str:
    _prune_result_sessions()
    evicted_ids: set[str] = set()
    while len(_result_sessions) >= _RESULT_CACHE_MAX:
        oldest_id = min(
            _result_sessions,
            key=lambda key: _result_sessions[key].expires_at,
        )
        _result_sessions.pop(oldest_id, None)
        evicted_ids.add(oldest_id)
    session_id = secrets.token_hex(5)
    _result_sessions[session_id] = _ResultSession(
        owner_id=owner_id,
        allowed_chat_ids=frozenset(allowed_chat_ids or ()),
        summary=summary,
        groups=groups,
        working=working,
        expires_at=time.time() + _RESULT_TTL_SECONDS,
    )
    _persist_result_sessions(saved_ids={session_id}, deleted_ids=evicted_ids)
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


# Order + human labels for the per-category .txt files sent to the admin.
_EXPORT_CATEGORIES = (
    ("residential", "residential"),
    ("http", "http"),
    ("socks5", "socks5"),
    ("socks4", "socks4"),
    ("datacenter", "datacenter"),
    ("rotating", "rotating"),
)


async def _send_category_files(
    context, chat_id: int, groups: dict[str, list[CheckResult]], source_name: str
) -> None:
    """Send one .txt per non-empty category, named residential/http/socks5/…"""
    for category, label in _EXPORT_CATEGORIES:
        items = groups.get(category) or []
        if not items:
            continue
        export = build_txt_export(items)
        filename = f"{label}_{len(items)}.txt"
        bio = io.BytesIO(export.encode("utf-8"))
        bio.name = filename
        try:
            await context.bot.send_document(
                chat_id=chat_id,
                document=bio,
                filename=filename,
                caption=f"{label.upper()} — {len(items)} live · via {source_name}",
            )
        except Exception:
            pass


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
            _delete_result_sessions({group_session_id})

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
            _delete_result_sessions({admin_session_id})
        # Also deliver the live proxies as per-category .txt files.
        await _send_category_files(context, config.ADMIN_ID, groups, source_name)
