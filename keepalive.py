"""Render anti-sleep keep-alive.

Render's free web services spin down after ~15 minutes without inbound HTTP
traffic on the bound PORT. Two defences run together:

1. A tiny aiohttp web server binds to $PORT and answers GET / and /health.
   This satisfies Render's port-scan and gives an endpoint to ping.

2. An internal self-pinger hits RENDER_EXTERNAL_URL every KEEPALIVE_INTERVAL
   seconds (default 8 min < 15 min idle window). Because the request goes out
   to the public URL and comes back through Render's router, it counts as real
   inbound traffic and resets the idle timer.

IMPORTANT (told to the user in the README): a pure internal self-ping can
still miss if the instance is already asleep when the timer fires. The robust
belt-and-braces fix is an EXTERNAL uptime monitor (UptimeRobot / cron-job.org)
hitting <RENDER_EXTERNAL_URL>/health every 5 minutes. The health server below
exists precisely so that monitor has something to hit.
"""
from __future__ import annotations

import asyncio
import logging
import time

import aiohttp
from aiohttp import web

import config

log = logging.getLogger("keepalive")
_START = time.time()


async def _health(request: web.Request) -> web.Response:
    uptime = int(time.time() - _START)
    return web.json_response(
        {"status": "alive", "uptime_seconds": uptime, "service": "proxy-checker-bot"}
    )


async def _root(request: web.Request) -> web.Response:
    return web.Response(text="Proxy Checker Bot is running. ✅", content_type="text/plain")


async def start_web_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", _root)
    app.router.add_get("/health", _health)
    app.router.add_get("/ping", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.PORT)
    await site.start()
    log.info("Health server listening on 0.0.0.0:%s", config.PORT)
    return runner


async def _self_ping_loop() -> None:
    if not config.RENDER_EXTERNAL_URL:
        log.warning(
            "RENDER_EXTERNAL_URL not set — internal self-ping disabled. "
            "Set it (and ideally add an external UptimeRobot monitor) to stay 24/7."
        )
        return
    url = f"{config.RENDER_EXTERNAL_URL}/health"
    interval = max(60, config.KEEPALIVE_INTERVAL)
    # small initial delay so the web server is definitely up
    await asyncio.sleep(15)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    log.info("Keep-alive self-ping %s -> %s", url, r.status)
            except Exception as exc:  # noqa: BLE001
                log.warning("Keep-alive self-ping failed: %s", exc)
            await asyncio.sleep(interval)


def launch_keepalive_tasks() -> asyncio.Task:
    """Start the background self-ping loop (call after the event loop is running)."""
    return asyncio.create_task(_self_ping_loop())
