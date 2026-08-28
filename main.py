"""Entry point — wires up the Telegram bot, admin panel and Render keep-alive."""
from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

import admin
import config
import handlers
from daily_scheduler import launch_daily_scheduler
from keepalive import launch_keepalive_tasks, start_web_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("main")


async def _post_init(app: Application) -> None:
    """Runs once after the bot starts: launch web server + keep-alive."""
    await start_web_server()
    launch_keepalive_tasks()
    launch_daily_scheduler(app.bot)
    try:
        await app.bot.send_message(
            config.ADMIN_ID,
            "✅ <b>Proxy Checker Bot is online.</b>\nSend /admin to open the control panel.",
            parse_mode="HTML",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not notify admin on startup: %s", exc)


def build_application() -> Application:
    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .post_init(_post_init)
        .concurrent_updates(True)
        .build()
    )

    app.add_handler(CommandHandler("start", handlers.cmd_start))
    app.add_handler(CommandHandler("help", handlers.cmd_help))
    app.add_handler(CommandHandler("addproxy", handlers.cmd_addproxy))
    app.add_handler(CommandHandler("cancel", handlers.cmd_cancel))
    app.add_handler(CommandHandler("admin", admin.open_panel))
    app.add_handler(CommandHandler("panel", admin.open_panel))
    app.add_handler(CommandHandler("ban", handlers.cmd_ban))
    app.add_handler(CommandHandler("unban", handlers.cmd_unban))
    app.add_handler(CallbackQueryHandler(admin.handle_callback, pattern=r"^adm:"))
    app.add_handler(
        CallbackQueryHandler(handlers.handle_result_callback, pattern=r"^res:")
    )
    # Text or document messages → proxy checker
    app.add_handler(
        MessageHandler(
            (filters.TEXT & ~filters.COMMAND) | filters.Document.ALL,
            handlers.handle_message,
        )
    )
    return app


def main() -> None:
    config.validate()
    app = build_application()
    log.info("Starting bot (polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
