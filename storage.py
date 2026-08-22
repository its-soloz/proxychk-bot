"""Tiny JSON-file persistence for users, settings and stats.

Deliberately dependency-free (no DB) so it runs anywhere, including Render's
free tier. State is small and write-rarely, so a JSON file is plenty.
NOTE: Render's free filesystem is ephemeral — this survives while the instance
is warm and resets on redeploy/cold start. That is acceptable for stats/settings.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

_PATH = os.getenv("STATE_PATH", os.path.join(os.path.dirname(__file__), "state.json"))
_LOCK = threading.Lock()

_DEFAULT: dict[str, Any] = {
    "settings": {
        "forward_to_group": True,
        "forward_to_admin": True,
        "only_forward_residential": False,
        "min_latency_forward": 0,  # forward all by default
    },
    "users": {},          # str(user_id) -> {name, checks, proxies_checked, last_seen}
    "stats": {
        "total_checks": 0,
        "total_proxies_checked": 0,
        "total_working": 0,
        "started_at": time.time(),
    },
    "banned": [],         # list of user ids
}


def _load() -> dict:
    if not os.path.exists(_PATH):
        return json.loads(json.dumps(_DEFAULT))
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return json.loads(json.dumps(_DEFAULT))
    # merge defaults for forward-compat
    for k, v in _DEFAULT.items():
        data.setdefault(k, json.loads(json.dumps(v)))
    for k, v in _DEFAULT["settings"].items():
        data["settings"].setdefault(k, v)
    return data


def _save(data: dict) -> None:
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, _PATH)


class Store:
    def __init__(self) -> None:
        self._data = _load()

    # ── settings ──
    def get_setting(self, key: str):
        return self._data["settings"].get(key)

    def set_setting(self, key: str, value) -> None:
        with _LOCK:
            self._data["settings"][key] = value
            _save(self._data)

    def toggle(self, key: str) -> bool:
        with _LOCK:
            cur = bool(self._data["settings"].get(key, False))
            self._data["settings"][key] = not cur
            _save(self._data)
            return not cur

    @property
    def settings(self) -> dict:
        return dict(self._data["settings"])

    # ── users ──
    def touch_user(self, user_id: int, name: str) -> None:
        with _LOCK:
            u = self._data["users"].setdefault(
                str(user_id), {"name": name, "checks": 0, "proxies_checked": 0, "last_seen": 0}
            )
            u["name"] = name
            u["last_seen"] = time.time()
            _save(self._data)

    def record_check(self, user_id: int, n_proxies: int, n_working: int) -> None:
        with _LOCK:
            u = self._data["users"].setdefault(
                str(user_id), {"name": "?", "checks": 0, "proxies_checked": 0, "last_seen": 0}
            )
            u["checks"] += 1
            u["proxies_checked"] += n_proxies
            s = self._data["stats"]
            s["total_checks"] += 1
            s["total_proxies_checked"] += n_proxies
            s["total_working"] += n_working
            _save(self._data)

    @property
    def users(self) -> dict:
        return dict(self._data["users"])

    @property
    def stats(self) -> dict:
        return dict(self._data["stats"])

    # ── bans ──
    def is_banned(self, user_id: int) -> bool:
        return user_id in self._data["banned"]

    def ban(self, user_id: int) -> None:
        with _LOCK:
            if user_id not in self._data["banned"]:
                self._data["banned"].append(user_id)
                _save(self._data)

    def unban(self, user_id: int) -> None:
        with _LOCK:
            if user_id in self._data["banned"]:
                self._data["banned"].remove(user_id)
                _save(self._data)

    @property
    def banned(self) -> list:
        return list(self._data["banned"])


store = Store()
