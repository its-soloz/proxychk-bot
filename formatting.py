"""Build the Telegram result messages from check results."""
from __future__ import annotations

from html import escape

from checker import CheckResult

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
    line = f"{prefix}<code>{escape(r.as_line())}</code>\n     {_ping_badge(r.latency_ms)}"
    if loc:
        line += f"  •  {escape(loc)}"
    if r.isp:
        line += f"\n     🏷 {escape(r.isp)}"
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


def build_category_message(
    category: str, items: list[CheckResult], top_n: int = 10
) -> str:
    """Build one latency-ranked category view for an inline result button."""
    title = _CATEGORY_TITLES.get(category, category.upper())
    shown = items[:top_n]
    header = f"{title}  —  <b>{len(items)}</b> live"
    if not shown:
        return (
            f"{header}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "No live proxies were found in this category."
        )
    body = "\n".join(_fmt_result(r, i + 1) for i, r in enumerate(shown))
    return (
        f"{header}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>Top {len(shown)} by ping</b>\n\n"
        f"{body}"
    )


def build_forward_summary(working: list[CheckResult], source: str) -> str | None:
    """Build the summary shown above forwarded result-menu buttons."""
    if not working:
        return None
    lines = [
        f"🚀 <b>{len(working)} LIVE PROXIES</b> — via {escape(source)}",
        "━━━━━━━━━━━━━━━━━━",
        "Forwarded proxy check results",
    ]
    fastest = working[0].latency_ms
    if fastest is not None:
        lines.append(f"⚡ Fastest: <b>{fastest}ms</b>")
    return "\n".join(lines)


def build_txt_export(working: list[CheckResult]) -> str:
    """Plain text file body — one proxy per line, best ping first."""
    return "\n".join(r.as_line() for r in working)
