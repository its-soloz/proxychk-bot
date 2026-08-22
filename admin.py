"""Advanced inline-keyboard admin panel."""
from __future__ import annotations

import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import config
from storage import store


def is_admin(user_id: int) -> bool:
    return user_id == config.ADMIN_ID


def _on(v: bool) -> str:
    return "🟢 ON" if v else "🔴 OFF"


def _fmt_uptime(seconds: float) -> str:
    seconds = int(seconds)
    d, seconds = divmod(seconds, 86400)
    h, seconds = divmod(seconds, 3600)
    m, seconds = divmod(seconds, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def panel_markup() -> InlineKeyboardMarkup:
    s = store.settings
    rows = [
        [
            InlineKeyboardButton("📊 Statistics", callback_data="adm:stats"),
            InlineKeyboardButton("👥 Users", callback_data="adm:users"),
        ],
        [
            InlineKeyboardButton(f"➡️ Group fwd: {_on(s['forward_to_group'])}", callback_data="adm:t:forward_to_group"),
        ],
        [
            InlineKeyboardButton(f"👤 Admin fwd: {_on(s['forward_to_admin'])}", callback_data="adm:t:forward_to_admin"),
        ],
        [
            InlineKeyboardButton(
                f"🏠 Residential-only fwd: {_on(s['only_forward_residential'])}",
                callback_data="adm:t:only_forward_residential",
            ),
        ],
        [
            InlineKeyboardButton("📢 Broadcast", callback_data="adm:broadcast"),
            InlineKeyboardButton("🚫 Ban / Unban", callback_data="adm:bans"),
        ],
        [
            InlineKeyboardButton("💓 Health", callback_data="adm:health"),
            InlineKeyboardButton("🔄 Refresh", callback_data="adm:home"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def _back_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="adm:home")]])


def panel_text() -> str:
    s = store.stats
    up = _fmt_uptime(time.time() - s.get("started_at", time.time()))
    return (
        "⚙️ <b>ADMIN CONTROL PANEL</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🤖 Uptime: <b>{up}</b>\n"
        f"🔎 Total checks: <b>{s['total_checks']}</b>\n"
        f"📦 Proxies checked: <b>{s['total_proxies_checked']}</b>\n"
        f"✅ Live found: <b>{s['total_working']}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Use the controls below to manage the bot."
    )


async def open_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.effective_message.reply_text("⛔ You are not authorised to use the admin panel.")
        return
    await update.effective_message.reply_text(
        panel_text(), parse_mode=ParseMode.HTML, reply_markup=panel_markup()
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    await query.answer()
    if not is_admin(user.id):
        await query.answer("⛔ Not authorised", show_alert=True)
        return

    data = query.data.split(":", 2)
    action = data[1] if len(data) > 1 else "home"

    if action == "home":
        await query.edit_message_text(panel_text(), parse_mode=ParseMode.HTML, reply_markup=panel_markup())

    elif action == "stats":
        s = store.stats
        up = _fmt_uptime(time.time() - s.get("started_at", time.time()))
        rate = (s["total_working"] / s["total_proxies_checked"] * 100) if s["total_proxies_checked"] else 0
        text = (
            "📊 <b>STATISTICS</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"⏱ Uptime: <b>{up}</b>\n"
            f"🔎 Checks run: <b>{s['total_checks']}</b>\n"
            f"📦 Proxies checked: <b>{s['total_proxies_checked']}</b>\n"
            f"✅ Live found: <b>{s['total_working']}</b>\n"
            f"📈 Global success rate: <b>{rate:.1f}%</b>\n"
            f"👥 Known users: <b>{len(store.users)}</b>\n"
            f"🚫 Banned: <b>{len(store.banned)}</b>"
        )
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=_back_markup())

    elif action == "users":
        users = store.users
        top = sorted(users.items(), key=lambda kv: kv[1].get("proxies_checked", 0), reverse=True)[:15]
        if not top:
            body = "No users yet."
        else:
            body = "\n".join(
                f"• <code>{uid}</code> {u.get('name','?')} — {u.get('checks',0)} checks, "
                f"{u.get('proxies_checked',0)} proxies"
                for uid, u in top
            )
        await query.edit_message_text(
            f"👥 <b>TOP USERS</b>\n━━━━━━━━━━━━━━━━━━\n{body}",
            parse_mode=ParseMode.HTML,
            reply_markup=_back_markup(),
        )

    elif action == "t":
        key = data[2]
        new_val = store.toggle(key)
        await query.answer(f"{key} → {'ON' if new_val else 'OFF'}")
        await query.edit_message_text(panel_text(), parse_mode=ParseMode.HTML, reply_markup=panel_markup())

    elif action == "broadcast":
        context.user_data["awaiting_broadcast"] = True
        await query.edit_message_text(
            "📢 <b>BROADCAST</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Send the message you want to broadcast to all known users.\n"
            "Send /cancel to abort.",
            parse_mode=ParseMode.HTML,
            reply_markup=_back_markup(),
        )

    elif action == "bans":
        banned = store.banned
        body = "\n".join(f"• <code>{b}</code>" for b in banned) if banned else "No banned users."
        await query.edit_message_text(
            "🚫 <b>BAN MANAGEMENT</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"{body}\n\n"
            "Use <code>/ban &lt;user_id&gt;</code> or <code>/unban &lt;user_id&gt;</code>.",
            parse_mode=ParseMode.HTML,
            reply_markup=_back_markup(),
        )

    elif action == "health":
        s = store.stats
        up = _fmt_uptime(time.time() - s.get("started_at", time.time()))
        ka = "enabled" if config.RENDER_EXTERNAL_URL else "DISABLED (set RENDER_EXTERNAL_URL)"
        text = (
            "💓 <b>HEALTH</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"🤖 Bot uptime: <b>{up}</b>\n"
            f"🌐 Health server port: <b>{config.PORT}</b>\n"
            f"🔁 Keep-alive: <b>{ka}</b>\n"
            f"⏲ Ping interval: <b>{config.KEEPALIVE_INTERVAL}s</b>\n"
            f"⚙️ Concurrency: <b>{config.MAX_CONCURRENCY}</b>\n"
            f"⌛ Timeout: <b>{config.CHECK_TIMEOUT}s</b>"
        )
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=_back_markup())


async def handle_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """If admin is composing a broadcast, send it. Returns True if consumed."""
    if not context.user_data.get("awaiting_broadcast"):
        return False
    context.user_data["awaiting_broadcast"] = False
    text = update.effective_message.text or ""
    sent = 0
    failed = 0
    for uid in list(store.users.keys()):
        try:
            await context.bot.send_message(chat_id=int(uid), text=f"📢 <b>Announcement</b>\n\n{text}",
                                            parse_mode=ParseMode.HTML)
            sent += 1
        except Exception:
            failed += 1
    await update.effective_message.reply_text(f"✅ Broadcast sent to {sent} users ({failed} failed).")
    return True
