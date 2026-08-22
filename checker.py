"""Advanced asynchronous proxy checker.

For each parsed proxy it:
  - tries the relevant protocol(s) (http, socks4, socks5) and keeps the first
    that works, so the *real* protocol is detected regardless of what the user
    labelled it;
  - measures latency (ping) in milliseconds;
  - reads the exit IP + geo/ISP metadata via a judge endpoint;
  - classifies datacenter vs residential using the ISP "hosting" flag;
  - detects rotating proxies by comparing the exit IP across two requests.

No cooldown and no per-user limit — only MAX_CONCURRENCY bounds open sockets.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from enum import Enum

import aiohttp
from aiohttp_socks import ProxyConnector, ProxyType

import config
from parser import ParsedProxy

_global_check_semaphore: asyncio.Semaphore | None = None
_global_check_loop: asyncio.AbstractEventLoop | None = None


class Protocol(str, Enum):
    HTTP = "http"
    SOCKS4 = "socks4"
    SOCKS5 = "socks5"


@dataclass
class CheckResult:
    proxy: ParsedProxy
    working: bool = False
    protocol: Protocol | None = None
    latency_ms: int | None = None
    exit_ip: str | None = None
    country: str | None = None
    country_code: str | None = None
    city: str | None = None
    isp: str | None = None
    org: str | None = None
    is_datacenter: bool = False
    is_residential: bool = False
    is_rotating: bool = False
    error: str | None = None

    # ── formatting helpers ──
    @property
    def type_label(self) -> str:
        if self.is_rotating:
            return "rotating"
        if self.is_residential:
            return "residential"
        return "datacenter"

    def as_line(self) -> str:
        """The proxy string, prefixed with its detected scheme."""
        p = self.proxy
        scheme = self.protocol.value if self.protocol else "http"
        if p.username and p.password:
            return f"{scheme}://{p.username}:{p.password}@{p.host}:{p.port}"
        return f"{scheme}://{p.host}:{p.port}"


def _connector_for(proto: Protocol, p: ParsedProxy) -> aiohttp.BaseConnector:
    if proto is Protocol.HTTP:
        # http proxy handled via session request(proxy=...), not a connector
        raise ValueError("http uses request-level proxy")
    ptype = ProxyType.SOCKS5 if proto is Protocol.SOCKS5 else ProxyType.SOCKS4
    return ProxyConnector(
        proxy_type=ptype,
        host=p.host,
        port=p.port,
        username=p.username,
        password=p.password,
        rdns=True,
    )


def _protocols_to_try(p: ParsedProxy) -> list[Protocol]:
    """Order attempts by the user's hint, then fall back to the rest."""
    order = [Protocol.HTTP, Protocol.SOCKS5, Protocol.SOCKS4]
    if p.scheme_hint == "socks5":
        order = [Protocol.SOCKS5, Protocol.SOCKS4, Protocol.HTTP]
    elif p.scheme_hint == "socks4":
        order = [Protocol.SOCKS4, Protocol.SOCKS5, Protocol.HTTP]
    elif p.scheme_hint == "http":
        order = [Protocol.HTTP, Protocol.SOCKS5, Protocol.SOCKS4]
    return order


def _global_check_limit() -> asyncio.Semaphore:
    """Return the one process-wide socket budget shared by every user batch."""
    global _global_check_loop, _global_check_semaphore
    loop = asyncio.get_running_loop()
    if _global_check_semaphore is None or _global_check_loop is not loop:
        _global_check_loop = loop
        _global_check_semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY)
    return _global_check_semaphore


def _request_timeout(total: int | None = None) -> aiohttp.ClientTimeout:
    """Build a bounded timeout that fails stalled handshakes and reads quickly."""
    total = max(1, total or config.CHECK_TIMEOUT)
    connect = min(config.CONNECT_TIMEOUT, total)
    read = min(config.READ_TIMEOUT, total)
    return aiohttp.ClientTimeout(
        total=total,
        connect=connect,
        sock_connect=connect,
        sock_read=read,
    )


async def _fetch_judge(
    session: aiohttp.ClientSession,
    url: str,
    proto: Protocol,
    p: ParsedProxy,
    *,
    timeout_total: int | None = None,
):
    """Perform one request through the proxy; return (status, json_or_text)."""
    timeout = _request_timeout(timeout_total)
    if proto is Protocol.HTTP:
        proxy_url = f"http://{p.host}:{p.port}"
        auth = None
        if p.username and p.password:
            auth = aiohttp.BasicAuth(p.username, p.password)
        async with session.get(url, proxy=proxy_url, proxy_auth=auth, timeout=timeout) as r:
            body = await r.text()
            return r.status, body
    else:
        connector = _connector_for(proto, p)
        async with aiohttp.ClientSession(connector=connector) as s:
            async with s.get(url, timeout=timeout) as r:
                body = await r.text()
                return r.status, body


def _parse_ipapi(body: str) -> dict:
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return {}
    if isinstance(data, dict) and data.get("status") == "success":
        return data
    # httpbin style {"origin": "1.2.3.4"}
    if isinstance(data, dict) and data.get("origin"):
        return {"query": data["origin"].split(",")[0].strip()}
    if isinstance(data, dict) and data.get("ip"):
        return {"query": data["ip"]}
    return {}


async def _detect_rotating(session: aiohttp.ClientSession, proto: Protocol, p: ParsedProxy, first_ip: str | None) -> bool:
    """Hit a fast IP-echo endpoint again; different exit IP => rotating."""
    if not first_ip:
        return False
    try:
        status, body = await _fetch_judge(
            session,
            config.ROTATE_ECHO_URL,
            proto,
            p,
            timeout_total=config.ROTATION_TIMEOUT,
        )
        if status != 200:
            return False
        info = _parse_ipapi(body)
        second_ip = info.get("query")
        return bool(second_ip and second_ip != first_ip)
    except Exception:
        return False


async def check_one(session: aiohttp.ClientSession, p: ParsedProxy) -> CheckResult:
    result = CheckResult(proxy=p)
    for proto in _protocols_to_try(p):
        start = time.perf_counter()
        try:
            status, body = await _fetch_judge(session, config.JUDGE_URLS[0], proto, p)
        except Exception as exc:  # noqa: BLE001 - proxy failures are expected
            result.error = type(exc).__name__
            continue
        if status != 200:
            result.error = f"HTTP {status}"
            continue

        latency = int((time.perf_counter() - start) * 1000)
        info = _parse_ipapi(body)
        result.working = True
        result.protocol = proto
        result.latency_ms = latency
        result.exit_ip = info.get("query")
        result.country = info.get("country")
        result.country_code = info.get("countryCode")
        result.city = info.get("city")
        result.isp = info.get("isp")
        result.org = info.get("org")
        hosting = info.get("hosting")
        proxy_flag = info.get("proxy")
        if hosting is True:
            result.is_datacenter = True
        elif hosting is False:
            result.is_residential = True
        else:
            # No hosting info (e.g. httpbin fallback) — infer from ISP keywords.
            blob = f"{info.get('isp','')} {info.get('org','')} {info.get('as','')}".lower()
            dc_kw = ("hosting", "cloud", "server", "data center", "datacenter", "vps",
                     "digitalocean", "amazon", "google", "ovh", "hetzner", "linode",
                     "leaseweb", "colo", "m247", "choopa", "vultr")
            result.is_datacenter = any(k in blob for k in dc_kw)
            result.is_residential = not result.is_datacenter and bool(result.exit_ip)

        result.is_rotating = await _detect_rotating(session, proto, p, result.exit_ip)
        return result

    return result  # never worked


async def check_all(proxies: list[ParsedProxy], progress_cb=None) -> list[CheckResult]:
    """Check a batch without letting it monopolise the shared socket budget."""
    batch_limit = min(config.BATCH_CONCURRENCY, config.MAX_CONCURRENCY)
    batch_sem = asyncio.Semaphore(batch_limit)
    global_sem = _global_check_limit()
    total = len(proxies)
    done = 0
    results: list[CheckResult] = []
    lock = asyncio.Lock()

    connector = aiohttp.TCPConnector(
        limit=batch_limit,
        ssl=False,
        ttl_dns_cache=300,
        enable_cleanup_closed=True,
    )
    async with aiohttp.ClientSession(connector=connector) as session:

        async def worker(px: ParsedProxy):
            nonlocal done
            async with batch_sem:
                async with global_sem:
                    res = await check_one(session, px)
            async with lock:
                done += 1
                results.append(res)
                if progress_cb:
                    await progress_cb(done, total)

        await asyncio.gather(*(worker(p) for p in proxies))

    return results


# ── result grouping / ranking ──

def group_and_rank(results: list[CheckResult], top_n: int | None = None):
    """Return dict of category -> latency-sorted working proxies."""
    top_n = top_n or config.TOP_N
    working = [r for r in results if r.working]
    working.sort(key=lambda r: r.latency_ms if r.latency_ms is not None else 10**9)

    groups = {
        "socks5": [r for r in working if r.protocol is Protocol.SOCKS5],
        "socks4": [r for r in working if r.protocol is Protocol.SOCKS4],
        "http": [r for r in working if r.protocol is Protocol.HTTP],
        "residential": [r for r in working if r.is_residential],
        "datacenter": [r for r in working if r.is_datacenter and not r.is_residential],
        "rotating": [r for r in working if r.is_rotating],
    }
    return groups, working
