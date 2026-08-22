"""Build the Telegram result messages from check results."""
from __future__ import annotations

from checker import CheckResult
import config

_FLAG_OFFSET = 127397  # regional indicator offset


def _flag(cc: str | None) -> str:
    if not cc or len(cc) != 2 or not cc.isalpha():
        return ""
    return "".join(chr(ord(c.upper()) + _FLAG_OFFSET) for c in cc)


def _ping_badge(ms: int | None) -> str:
    if ms is None:
        return "—"
    if ms < 500:
        return f"🟢 {ms}ms"
    if ms < 1500:
        return f"🟡 {ms}ms"
    return f"🔴 {ms}ms"


def _fmt_result(r: CheckResult, rank: int | None = None) -> str:
    prefix = f"{rank}. " if rank else ""
    flag = _flag(r.country_code)
    loc = " ".join(x for x in [flag, r.country, r.city] if x)
    line = f"{prefix}<code>{r.as_line()}</code>\n     {_ping_badge(r.latency_ms)}"
    if loc:
        line += f"  •  {loc}"
    if r.isp:
        line += f"\n     🏷 {r.isp}"
    return line


_CATEGORY_TITLES = {
    "socks5": "🧦 SOCKS5",
    "socks4": "🧦 SOCKS4",
    "http": "🌐 HTTP/HTTPS",
    "residential": "🏠 RESIDENTIAL",
    "datacenter": "🏢 DATACENTER",
    "rotating": "🔄 ROTATING",
}


def build_summary(total: int, working: list[CheckResult], elapsed: float) -> str:
    dead = total - len(working)
    rate = (len(working) / total * 100) if total else 0
    fastest = working[0].latency_ms if working else None
    lines = [
        "✨ <b>PROXY CHECK COMPLETE</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"📦 Extracted: <b>{total}</b>",
        f"✅ Live: <b>{len(working)}</b>   ❌ Dead: <b>{dead}</b>",
        f"📊 Success rate: <b>{rate:.1f}%</b>",
    ]
    if fastest is not None:
        lines.append(f"⚡ Fastest: <b>{fastest}ms</b>")
    lines.append(f"⏱ Time: <b>{elapsed:.1f}s</b>")
    return "\n".join(lines)


def build_category_messages(groups: dict[str, list[CheckResult]]) -> list[str]:
    """One message per non-empty category, top-N each, sorted by ping."""
    msgs: list[str] = []
    top_n = config.TOP_N
    for key, title in _CATEGORY_TITLES.items():
        items = groups.get(key, [])
        if not items:
            continue
        shown = items[:top_n]
        header = f"{title}  —  <b>{len(items)}</b> live (top {len(shown)} by ping)"
        body = "\n".join(_fmt_result(r, i + 1) for i, r in enumerate(shown))
        msgs.append(f"{header}\n━━━━━━━━━━━━━━━━━━\n{body}")
    return msgs


def build_forward_message(working: list[CheckResult], source: str) -> str | None:
    """Compact 'live proxies' broadcast for the group / admin."""
    if not working:
        return None
    top = working[: config.TOP_N]
    lines = [f"🚀 <b>{len(working)} LIVE PROXIES</b> — via {source}", "━━━━━━━━━━━━━━━━━━"]
    for r in top:
        badge = _ping_badge(r.latency_ms)
        tag = r.type_label
        lines.append(f"<code>{r.as_line()}</code>\n   {badge} • {r.protocol.value if r.protocol else '?'} • {tag}")
    if len(working) > len(top):
        lines.append(f"\n…and {len(working) - len(top)} more")
    return "\n".join(lines)


def build_txt_export(working: list[CheckResult]) -> str:
    """Plain text file body — one proxy per line, best ping first."""
    return "\n".join(r.as_line() for r in working)
