"""Centralised configuration loaded from environment variables."""
from __future__ import annotations

import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # dotenv is optional in production
    pass


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
GROUP_ID: int = _int("GROUP_ID", -1004358364327)
ADMIN_ID: int = _int("ADMIN_ID", 5010778910)

# Keep-alive / web server
RENDER_EXTERNAL_URL: str = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
PORT: int = _int("PORT", 10000)
KEEPALIVE_INTERVAL: int = _int("KEEPALIVE_INTERVAL", 480)

# Checker tuning
CHECK_TIMEOUT: int = max(1, _int("CHECK_TIMEOUT", 7))
CONNECT_TIMEOUT: int = max(1, _int("CONNECT_TIMEOUT", 3))
READ_TIMEOUT: int = max(1, _int("READ_TIMEOUT", 4))
ROTATION_TIMEOUT: int = max(1, _int("ROTATION_TIMEOUT", 3))
MAX_CONCURRENCY: int = max(1, _int("MAX_CONCURRENCY", 300))
TOP_N: int = _int("TOP_N", 10)

# Judge endpoints used to validate proxies and read the exit IP.
JUDGE_URLS = [
    "http://ip-api.com/json/?fields=status,country,countryCode,region,city,isp,org,as,proxy,hosting,query",
    "http://httpbin.org/ip",
]
# Lightweight IP-echo endpoint used for rotating detection (fast, tiny body).
ROTATE_ECHO_URL = "http://api.ipify.org/?format=json"


def validate() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN is not set. Add it to your .env file or Render environment."
        )
