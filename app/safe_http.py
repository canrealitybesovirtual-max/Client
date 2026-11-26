"""Outbound HTTP for integrations and webhooks, with SSRF protection.

Every request a connector or webhook makes goes through SafeHTTP (never a bare httpx.Client), because the URLs come
from tenants (webhook URLs) or from vendor responses (redirects, pagination links). Rules:

- https only. Plain http is allowed only for localhost/127.0.0.1/::1 and only when allow_localhost=True (tests).
- No credentials in the URL (user:pass@host), ports limited to `allowed_ports` (443 and 8443 by default).
- The host is resolved and refused if ANY address is not public: private (RFC 1918), loopback, link-local
  (169.254.0.0/16, which includes the cloud metadata address 169.254.169.254), carrier-grade NAT, multicast,
  reserved, unspecified, IPv6 ULA (fc00::/7), IPv6 link-local, and IPv6 forms that embed an IPv4 address
  (IPv4-mapped ::ffff:a.b.c.d, 6to4 2002::/16, Teredo, NAT64 64:ff9b::/96) are checked by the embedded address.
  Names like "localhost", "*.localhost", "*.internal", "*.local" and "metadata.google.internal" are refused outright.
- The check runs again at connect time (GuardedBackend), on the addresses actually connected to, so a DNS answer that
  changes between the check and the connection (DNS rebinding) can't reach an internal address. TLS still verifies
  the certificate against the hostname.
- Redirects are followed by hand (at most max_redirects), each target checked the same way; Authorization and Cookie
  headers are dropped when a redirect leaves the original origin. follow_redirects=False returns the 3xx as is.
- Environment proxies (HTTPS_PROXY etc.) are ignored: a proxy would resolve the host itself, past these checks.
- Timeouts (connect, read, and a total deadline per attempt) and a response size cap (Content-Length and the
  decoded bytes actually read).
- Retries with exponential backoff and jitter for 429 and 5xx (500, 502, 503, 504), honoring Retry-After (seconds
  or an HTTP date) up to max_backoff; a longer Retry-After returns the response so the caller (e.g. the event queue)
  can reschedule. Non-idempotent methods (POST, PATCH) are retried only on 429 or when the connection could not be
  opened at all, unless idempotent=True is passed.
- Logs show method, host and status only: no paths (a Slack webhook path IS its secret), queries or headers.

Tests plug in `transport=httpx.MockTransport(handler)` and a fake `resolver` (see tests/support.py); the SSRF rules
still apply to the URL and the resolver's answers.
"""
from __future__ import annotations

import email.utils
import ipaddress
import logging
import random
import socket
import time
from typing import Callable, Iterable

import httpcore
import httpx

log = logging.getLogger("tradecomply.http")
# httpx itself logs every request's full URL at INFO ("HTTP Request: POST https://hooks.slack.com/services/..."), and
# the app logs at INFO, so Slack/Teams webhook URLs (whose path or ?sig= is the secret) would reach the server log.
# Keep httpx to warnings; this module logs method, host and status instead.
if logging.getLogger("httpx").level < logging.WARNING:
    logging.getLogger("httpx").setLevel(logging.WARNING)

Resolver = Callable[[str, int], list[str]]
DEFAULT_PORTS = (443, 8443)
REDIRECT_CODES = (301, 302, 303, 307, 308)
RETRY_STATUSES = (429, 500, 502, 503, 504)
IDEMPOTENT = {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}
LOCALHOST_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]"}
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".home.arpa")
BLOCKED_NAMES = {"localhost", "metadata.google.internal", "metadata", "instance-data"}
NAT64 = ipaddress.ip_network("64:ff9b::/96")
USER_AGENT = "TradeComply-CA/1.0 (integrations)"


class SafeHTTPError(Exception):
    """Base class. Messages are safe to show a customer: host names, never paths, queries, headers or bodies."""


class UnsafeURLError(SafeHTTPError, ValueError):
    """The URL (or a redirect target, or the address it resolves to) is not allowed."""


class ResponseTooLarge(SafeHTTPError):
    pass


class UpstreamError(SafeHTTPError):
    """The request could not be completed (connection failure, timeout, too many redirects)."""


class BlockedAddress(httpcore.ConnectError):
    """Raised by GuardedBackend at connect time; surfaces as UnsafeURLError."""


# ---------------------------------------------------------------------------- address and URL checks
def system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


def _embedded_v4(ip: ipaddress.IPv6Address):
    if ip.ipv4_mapped:
        return ip.ipv4_mapped
    if ip.sixtofour:
        return ip.sixtofour
    if ip.teredo:
        return ip.teredo[1]
    if ip in NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def is_public_ip(value: str) -> bool:
    """True only for a globally routable unicast address (and, for IPv6 forms embedding IPv4, that IPv4 too)."""
    try:
        ip = ipaddress.ip_address(str(value).split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        inner = _embedded_v4(ip)
        if inner is not None:
            if ip in NAT64:            # NAT64 addresses are reserved; refuse them whatever they embed
                return False
            return is_public_ip(str(inner))
    return bool(ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local
                                      or ip.is_private or ip.is_unspecified))


def _is_loopback(value: str) -> bool:
    try:
        return ipaddress.ip_address(str(value).split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _literal_ip(host: str):
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def check_url(url, *, allow_localhost: bool = False, allowed_ports: Iterable[int] | None = DEFAULT_PORTS,
              resolver: Resolver | None = system_resolver) -> httpx.URL:
    """Validate an outbound URL (and resolve its host) or raise UnsafeURLError. Returns the parsed URL.
    resolver=None skips DNS (literal IPs are still checked), for validating a URL without network access."""
    try:
        u = url if isinstance(url, httpx.URL) else httpx.URL(str(url))
    except (httpx.InvalidURL, TypeError, ValueError):
        raise UnsafeURLError("That is not a valid URL") from None
    host = (u.host or "").lower().rstrip(".")
    if not host:
        raise UnsafeURLError("The URL has no host")
    local = allow_localhost and host in LOCALHOST_NAMES
    if u.scheme != "https" and not (u.scheme == "http" and local):
        raise UnsafeURLError("Only https:// URLs are allowed")
    if u.userinfo:
        raise UnsafeURLError("URLs with a user name or password in them are not allowed")
    port = u.port or (443 if u.scheme == "https" else 80)
    if not local and allowed_ports is not None and port not in set(allowed_ports):
        raise UnsafeURLError(f"Port {port} is not allowed (allowed: {', '.join(map(str, allowed_ports))})")
    if local:
        return u
    if host in BLOCKED_NAMES or host.endswith(BLOCKED_SUFFIXES) or "." not in host and _literal_ip(host) is None:
        raise UnsafeURLError(f"{host} is not a public internet host")
    literal = _literal_ip(host)
    if literal is not None:
        if not is_public_ip(str(literal)):
            raise UnsafeURLError(f"{host} is not a public internet address")
        return u
    if resolver is None:
        return u
    try:
        addresses = resolver(host, port)
    except (OSError, UnicodeError):
        raise UnsafeURLError(f"Could not find the server {host} (DNS lookup failed)") from None
    if not addresses:
        raise UnsafeURLError(f"Could not find the server {host} (DNS lookup failed)")
    bad = [a for a in addresses if not is_public_ip(a)]
    if bad:
        raise UnsafeURLError(f"{host} resolves to a private or reserved address, which is not allowed")
    return u


# ---------------------------------------------------------------------------- connect-time guard
class GuardedBackend(httpcore.SyncBackend):
    """Resolves the host itself, refuses non-public addresses, and connects to a checked address. httpcore then
    starts TLS with the original host name, so certificate verification and SNI are unchanged."""

    def __init__(self, resolver: Resolver = system_resolver, allow_localhost: bool = False):
        super().__init__()
        self.resolver, self.allow_localhost = resolver, allow_localhost

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        name = host.lower().rstrip(".")
        if self.allow_localhost and name in LOCALHOST_NAMES:
            addresses = ["127.0.0.1" if name in ("localhost", "127.0.0.1") else "::1"]
        else:
            literal = _literal_ip(name)
            try:
                addresses = [str(literal)] if literal is not None else self.resolver(name, port)
            except (OSError, UnicodeError) as e:
                raise httpcore.ConnectError(f"DNS lookup failed for {name}") from e
            if not addresses or not all(is_public_ip(a) for a in addresses):
                raise BlockedAddress(f"refused to connect to {name}: not a public address")
        last: Exception | None = None
        for addr in addresses:
            try:
                return super().connect_tcp(addr, port, timeout=timeout, local_address=local_address,
                                           socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
                last = e
        raise last or httpcore.ConnectError(f"could not connect to {name}")


class GuardedTransport(httpx.HTTPTransport):
    """httpx's own transport, with its connection pool rebuilt on GuardedBackend. No proxy, no env proxies."""

    def __init__(self, resolver: Resolver = system_resolver, allow_localhost: bool = False, verify=True):
        super().__init__(verify=verify, trust_env=False, retries=0)
        limits = httpx.Limits(max_connections=20, max_keepalive_connections=5, keepalive_expiry=5.0)
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=verify),
            max_connections=limits.max_connections, max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry, http1=True, http2=False, retries=0,
            network_backend=GuardedBackend(resolver, allow_localhost))


# ---------------------------------------------------------------------------- client
def retry_after_seconds(resp: httpx.Response) -> float | None:
    """Retry-After as seconds (it may be a number or an HTTP date), or None."""
    value = (resp.headers.get("retry-after") or "").strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - time.time())


class SafeHTTP:
    """Synchronous; meant for worker threads (FastAPI `def` handlers, the event delivery thread). Use it as a
    context manager, or call close(). Every option can be overridden per request."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None, resolver: Resolver | None = None,
                 allow_localhost: bool = False, timeout: float = 10.0, connect_timeout: float = 5.0,
                 total_timeout: float = 30.0, max_bytes: int = 5 * 1024 * 1024, retries: int = 2,
                 backoff: float = 0.5, max_backoff: float = 30.0, max_redirects: int = 3,
                 follow_redirects: bool = True, allowed_ports: Iterable[int] | None = DEFAULT_PORTS,
                 sleep: Callable[[float], None] = time.sleep, user_agent: str = USER_AGENT):
        self.resolver = resolver or system_resolver
        self.allow_localhost = allow_localhost
        self.timeout, self.connect_timeout, self.total_timeout = timeout, connect_timeout, total_timeout
        self.max_bytes, self.retries, self.backoff, self.max_backoff = max_bytes, retries, backoff, max_backoff
        self.max_redirects, self.follow_redirects = max_redirects, follow_redirects
        self.allowed_ports = tuple(allowed_ports) if allowed_ports is not None else None
        self.sleep = sleep
        transport = transport or GuardedTransport(self.resolver, allow_localhost)
        self._client = httpx.Client(transport=transport, trust_env=False, follow_redirects=False,
                                    headers={"user-agent": user_agent},
                                    timeout=httpx.Timeout(timeout, connect=connect_timeout))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        self._client.close()

    def check(self, url) -> httpx.URL:
        return check_url(url, allow_localhost=self.allow_localhost, allowed_ports=self.allowed_ports,
                         resolver=self.resolver)

    # convenience verbs
    def get(self, url, **kw) -> httpx.Response:
        return self.request("GET", url, **kw)

    def post(self, url, **kw) -> httpx.Response:
        return self.request("POST", url, **kw)

    def put(self, url, **kw) -> httpx.Response:
        return self.request("PUT", url, **kw)

    def patch(self, url, **kw) -> httpx.Response:
        return self.request("PATCH", url, **kw)

    def delete(self, url, **kw) -> httpx.Response:
        return self.request("DELETE", url, **kw)

    def request(self, method: str, url, *, headers: dict | None = None, params=None, json=None, data=None,
                content=None, auth=None, timeout: float | None = None, max_bytes: int | None = None,
                retries: int | None = None, follow_redirects: bool | None = None,
                idempotent: bool | None = None) -> httpx.Response:
        """Send a request and return the response with its body read (at most max_bytes). Raises UnsafeURLError,
        ResponseTooLarge or UpstreamError; an HTTP error status is returned, not raised."""
        method = method.upper()
        retries = self.retries if retries is None else max(0, int(retries))
        idempotent = (method in IDEMPOTENT) if idempotent is None else idempotent
        follow = self.follow_redirects if follow_redirects is None else follow_redirects
        limit = self.max_bytes if max_bytes is None else int(max_bytes)
        tmo = httpx.Timeout(timeout if timeout is not None else self.timeout, connect=self.connect_timeout)
        first = self.check(url)
        request = self._client.build_request(method, first, headers=headers, params=params, json=json, data=data,
                                             content=content, timeout=tmo)
        attempt = 0
        while True:
            try:
                resp = self._send(request, auth, follow, limit)
            except UpstreamError as e:
                connect_failed = isinstance(e.__cause__, (httpx.ConnectError, httpx.ConnectTimeout))
                if attempt < retries and (idempotent or connect_failed):
                    self.sleep(self._delay(attempt))
                    attempt += 1
                    continue
                raise
            if resp.status_code in RETRY_STATUSES and attempt < retries and (idempotent or resp.status_code == 429):
                wait = retry_after_seconds(resp)
                if wait is not None and wait > self.max_backoff:
                    return resp            # the caller reschedules (e.g. the event queue) instead of blocking
                self.sleep(min(self.max_backoff, wait if wait is not None else self._delay(attempt)))
                attempt += 1
                continue
            return resp

    def _delay(self, attempt: int) -> float:
        base = min(self.max_backoff, self.backoff * (2 ** attempt))
        return base * (0.75 + random.random() * 0.5)

    def _send(self, request: httpx.Request, auth, follow: bool, limit: int) -> httpx.Response:
        origin = (request.url.scheme, request.url.host, request.url.port)
        for _ in range(self.max_redirects + 1):
            resp = self._send_one(request, auth, limit)
            if not (follow and resp.status_code in REDIRECT_CODES and resp.headers.get("location")):
                return resp
            try:
                target = self.check(request.url.join(resp.headers["location"]))
            except UnsafeURLError as e:
                raise UnsafeURLError(f"Redirect refused: {e}") from None
            method, body = request.method, request.content
            if resp.status_code == 303 or (resp.status_code in (301, 302) and method == "POST"):
                method, body = ("GET" if method != "HEAD" else "HEAD"), b""
            headers = httpx.Headers(request.headers)
            for h in ("content-length", "transfer-encoding", "host"):
                headers.pop(h, None)
            if not body:
                headers.pop("content-type", None)
            if (target.scheme, target.host, target.port) != origin:
                for h in ("authorization", "cookie", "proxy-authorization"):
                    headers.pop(h, None)
                auth = None
            request = self._client.build_request(method, target, headers=headers, content=body or None,
                                                 timeout=request.extensions.get("timeout"))
        raise UpstreamError(f"Too many redirects from {origin[1]}")

    def _send_one(self, request: httpx.Request, auth, limit: int) -> httpx.Response:
        host = request.url.host
        deadline = time.monotonic() + self.total_timeout
        try:
            resp = self._client.send(request, auth=auth, stream=True, follow_redirects=False)
        except httpx.ConnectError as e:
            if isinstance(e.__cause__, BlockedAddress):
                raise UnsafeURLError(f"{host} resolves to a private or reserved address, which is not allowed") \
                    from None
            raise UpstreamError(f"Could not connect to {host}") from e
        except httpx.TimeoutException as e:
            raise UpstreamError(f"{host} did not answer in time") from e
        except httpx.HTTPError as e:
            raise UpstreamError(f"Request to {host} failed ({type(e).__name__})") from e
        try:
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > limit:
                raise ResponseTooLarge(f"The response from {host} is larger than {limit} bytes")
            body = bytearray()
            for chunk in resp.iter_bytes():
                body.extend(chunk)
                if len(body) > limit:
                    raise ResponseTooLarge(f"The response from {host} is larger than {limit} bytes")
                if time.monotonic() > deadline:
                    raise UpstreamError(f"{host} took too long to send its response")
        except httpx.TimeoutException as e:
            raise UpstreamError(f"{host} did not answer in time") from e
        except httpx.HTTPError as e:
            raise UpstreamError(f"Reading the response from {host} failed ({type(e).__name__})") from e
        finally:
            resp.close()
        log.info("outbound %s %s -> %s", request.method, host, resp.status_code)
        headers = [(k, v) for k, v in resp.headers.multi_items()
                   if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")]
        out = httpx.Response(resp.status_code, headers=headers, content=bytes(body), request=request,
                             extensions={"http_version": resp.extensions.get("http_version", b"HTTP/1.1")})
        return out
