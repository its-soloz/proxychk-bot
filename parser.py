"""Extract proxies from arbitrarily messy user input.

Supports (mixed together, in any order, with any surrounding junk):
  - ip:port
  - ip:port:user:pass
  - user:pass@ip:port
  - scheme://[user:pass@]ip:port           (http, https, socks4, socks5)
  - JSON objects / arrays with host+port fields
  - lines with commas, pipes, spaces, labels ("Proxy - 1.2.3.4:8080")
Anything unparseable is silently ignored.
"""
from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field
from typing import Iterable

_SCHEMES = ("socks5h", "socks5", "socks4a", "socks4", "https", "http")

# host = IPv4 or a domain name
_HOST = r"(?:(?:\d{1,3}\.){3}\d{1,3}|(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,})"
_PORT = r"\d{1,5}"

# scheme://user:pass@host:port  or  scheme://host:port
_RE_URL = re.compile(
    r"(?P<scheme>" + "|".join(_SCHEMES) + r")://"
    r"(?:(?P<user>[^:@/\s]+):(?P<pw>[^@/\s]+)@)?"
    r"(?P<host>" + _HOST + r"):(?P<port>" + _PORT + r")",
    re.IGNORECASE,
)

# user:pass@host:port  (no scheme)
_RE_AUTH_AT = re.compile(
    r"(?<![\w@])(?P<user>[^\s:@/]+):(?P<pw>[^\s:@/]+)@(?P<host>" + _HOST + r"):(?P<port>" + _PORT + r")"
)

# host:port:user:pass
_RE_COLON4 = re.compile(
    r"(?<![\w.:])(?P<host>" + _HOST + r"):(?P<port>" + _PORT + r"):(?P<user>[^\s:@/]+):(?P<pw>[^\s:@/]+)"
)

# bare host:port
_RE_HOSTPORT = re.compile(r"(?<![\w.:@])(?P<host>" + _HOST + r"):(?P<port>" + _PORT + r")(?![\w.:])")


@dataclass(frozen=True)
class ParsedProxy:
    host: str
    port: int
    username: str | None = None
    password: str | None = None
    scheme_hint: str | None = None  # normalised protocol family if user provided one
    raw: str | None = None  # original proxy token when it came from text input

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def key(self) -> str:
        """Dedup key — proxy is the same regardless of the scheme the user typed."""
        return f"{self.host}:{self.port}:{self.username or ''}:{self.password or ''}"

    @property
    def input_line(self) -> str:
        """Return the proxy in its original form, including authentication."""
        if self.raw:
            return self.raw.strip()
        scheme = f"{self.scheme_hint}://" if self.scheme_hint else ""
        credentials = ""
        if self.username is not None or self.password is not None:
            credentials = f"{self.username or ''}:{self.password or ''}@"
        return f"{scheme}{credentials}{self.host}:{self.port}"


def _valid_port(port: str) -> bool:
    try:
        p = int(port)
    except ValueError:
        return False
    return 0 < p <= 65535


def _valid_host(host: str) -> bool:
    # If it looks like an IPv4, it must be a real one.
    if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", host):
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            return False
    return True  # domain names accepted as-is


def _norm_scheme(scheme: str | None) -> str | None:
    if not scheme:
        return None
    s = scheme.lower()
    if s.startswith("socks5"):
        return "socks5"
    if s.startswith("socks4"):
        return "socks4"
    if s.startswith("https"):
        return "http"  # https proxy is just an http proxy over TLS transport
    if s.startswith("http"):
        return "http"
    return None


def _extract_from_json(obj, out: list[ParsedProxy]) -> None:
    """Walk arbitrary JSON and pull out proxy-shaped dicts."""
    if isinstance(obj, dict):
        host = obj.get("host") or obj.get("ip") or obj.get("address") or obj.get("server")
        port = obj.get("port")
        if host and port:
            if _valid_host(str(host)) and _valid_port(str(port)):
                out.append(
                    ParsedProxy(
                        host=str(host).strip(),
                        port=int(port),
                        username=(obj.get("username") or obj.get("user") or obj.get("login") or None),
                        password=(obj.get("password") or obj.get("pass") or obj.get("pwd") or None),
                        scheme_hint=_norm_scheme(obj.get("protocol") or obj.get("scheme") or obj.get("type")),
                    )
                )
        for v in obj.values():
            _extract_from_json(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _extract_from_json(v, out)


def parse_proxies(text: str) -> list[ParsedProxy]:
    """Return a de-duplicated, order-preserving list of proxies found in text."""
    if not text:
        return []

    found: list[ParsedProxy] = []
    consumed: list[tuple[int, int]] = []

    def overlaps(start: int, end: int) -> bool:
        return any(s < end and start < e for s, e in consumed)

    # 1) Try JSON anywhere in the blob (whole thing, or embedded objects).
    for candidate in _json_candidates(text):
        try:
            _extract_from_json(json.loads(candidate), found)
        except (json.JSONDecodeError, ValueError):
            continue

    # 2) Regex passes, most specific first, tracking consumed spans so we
    #    don't double-count the same substring.
    for rx in (_RE_URL, _RE_COLON4, _RE_AUTH_AT, _RE_HOSTPORT):
        for m in rx.finditer(text):
            if overlaps(m.start(), m.end()):
                continue
            host = m.group("host")
            port = m.group("port")
            if not (_valid_host(host) and _valid_port(port)):
                continue
            gd = m.groupdict()
            found.append(
                ParsedProxy(
                    host=host.strip(),
                    port=int(port),
                    username=gd.get("user"),
                    password=gd.get("pw"),
                    scheme_hint=_norm_scheme(gd.get("scheme")),
                    raw=m.group(0),
                )
            )
            consumed.append((m.start(), m.end()))

    # De-dup preserving order.
    seen: set[str] = set()
    unique: list[ParsedProxy] = []
    for p in found:
        if p.key in seen:
            continue
        seen.add(p.key)
        unique.append(p)
    return unique


def _json_candidates(text: str) -> Iterable[str]:
    """Yield substrings that might be valid JSON (whole text + balanced braces/brackets)."""
    stripped = text.strip()
    if stripped and stripped[0] in "[{":
        yield stripped
    # Find balanced {...} and [...] blocks.
    for opener, closer in (("{", "}"), ("[", "]")):
        depth = 0
        start = -1
        for i, ch in enumerate(text):
            if ch == opener:
                if depth == 0:
                    start = i
                depth += 1
            elif ch == closer and depth:
                depth -= 1
                if depth == 0 and start >= 0:
                    yield text[start : i + 1]
                    start = -1
