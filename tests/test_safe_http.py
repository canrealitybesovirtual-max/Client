"""app/safe_http.py: SSRF protection (scheme, ports, private/reserved addresses, DNS answers, redirects, connect-time
re-check against DNS rebinding), size limits, retries with backoff, Retry-After, and quiet logs."""
import gzip
import http.server
import logging
import threading

import httpx
import pytest

from app.safe_http import (BlockedAddress, GuardedBackend, GuardedTransport, ResponseTooLarge, SafeHTTP,
                           UnsafeURLError, UpstreamError, check_url, is_public_ip)
from tests.support import PUBLIC_TEST_IP, fake_resolver


def client(handler, hosts=None, **kw) -> tuple[SafeHTTP, list, list]:
    seen, sleeps = [], []

    def wrapped(request):
        seen.append(request)
        return handler(request)
    kw.setdefault("sleep", sleeps.append)
    return SafeHTTP(transport=httpx.MockTransport(wrapped), resolver=fake_resolver(hosts), **kw), seen, sleeps


@pytest.mark.parametrize("ip,public", [
    ("8.8.8.8", True), ("2606:4700::1111", True), ("::ffff:8.8.8.8", True),
    ("10.1.2.3", False), ("172.16.0.1", False), ("192.168.1.1", False), ("127.0.0.1", False),
    ("169.254.169.254", False), ("100.64.0.1", False), ("0.0.0.0", False), ("255.255.255.255", False),
    ("224.0.0.1", False), ("240.0.0.1", False), ("198.18.0.1", False), ("192.0.2.1", False),
    ("::1", False), ("::", False), ("fe80::1", False), ("fd00:ec2::254", False), ("fc00::1", False),
    ("::ffff:127.0.0.1", False), ("::ffff:169.254.169.254", False), ("2002:7f00:1::", False),
    ("64:ff9b::7f00:1", False), ("2001::1", False), ("not an ip", False),
])
def test_address_classification(ip, public):
    assert is_public_ip(ip) is public


@pytest.mark.parametrize("url", [
    "http://api.example.com/x",                 # not https
    "ftp://api.example.com/",
    "https://user:pw@api.example.com/",         # credentials in the URL
    "https://api.example.com:8080/",            # port not allowed
    "https://localhost/", "https://foo.localhost/", "https://metadata.google.internal/computeMetadata/v1/",
    "https://db.internal/", "https://printer.local/", "https://intranet/",
    "https://169.254.169.254/latest/meta-data/", "https://10.0.0.5/", "https://127.0.0.1/", "https://0.0.0.0/",
    "https://[::1]/", "https://[fd00::1]/", "https://[::ffff:127.0.0.1]/", "https://2130706433/",
    "https://[fe80::1]/", "not a url", "https:///nohost",
])
def test_refused_urls(url):
    with pytest.raises(UnsafeURLError):
        check_url(url, resolver=fake_resolver())


def test_dns_answers_are_checked():
    with pytest.raises(UnsafeURLError, match="private or reserved"):
        check_url("https://evil.example.com/", resolver=fake_resolver({"evil.example.com": "10.0.0.7"}))
    with pytest.raises(UnsafeURLError):          # one bad address among good ones is enough to refuse
        check_url("https://mixed.example.com/", resolver=fake_resolver({"mixed.example.com": [PUBLIC_TEST_IP,
                                                                                             "169.254.169.254"]}))
    with pytest.raises(UnsafeURLError, match="DNS"):
        check_url("https://nxdomain.example.com/", resolver=fake_resolver(default=None))
    assert check_url("https://api.example.com/v1?x=1", resolver=fake_resolver()).host == "api.example.com"
    assert check_url("https://api.example.com:8443/", resolver=fake_resolver()).port == 8443


def test_localhost_only_in_test_mode():
    with pytest.raises(UnsafeURLError):
        check_url("http://localhost:8000/hook", resolver=fake_resolver())
    assert check_url("http://localhost:8000/hook", allow_localhost=True, resolver=fake_resolver())
    assert check_url("http://127.0.0.1:9/hook", allow_localhost=True, resolver=fake_resolver())
    with pytest.raises(UnsafeURLError):          # other private addresses stay refused
        check_url("http://10.0.0.1/", allow_localhost=True, resolver=fake_resolver())


def test_plain_request_reads_the_body_and_sends_no_proxy_auth():
    http, seen, _ = client(lambda r: httpx.Response(200, json={"ok": True}))
    r = http.get("https://api.example.com/v1/me", headers={"authorization": "Bearer t"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert seen[0].headers["authorization"] == "Bearer t" and seen[0].headers["user-agent"].startswith("TradeComply")


def test_gzip_body_is_decoded_and_counted_decoded():
    raw = b"x" * 5000
    http, _, _ = client(lambda r: httpx.Response(200, content=gzip.compress(raw),
                                                 headers={"content-encoding": "gzip"}))
    assert http.get("https://api.example.com/").content == raw
    with pytest.raises(ResponseTooLarge):        # a decompression bomb is capped by decoded size
        http.get("https://api.example.com/", max_bytes=1000)


def test_size_limits():
    http, _, _ = client(lambda r: httpx.Response(200, content=b"y" * 2000), max_bytes=1000)
    with pytest.raises(ResponseTooLarge):
        http.get("https://api.example.com/")

    def streamed(r):
        return httpx.Response(200, stream=httpx.ByteStream(b"z" * 5000))   # no Content-Length
    http2, _, _ = client(streamed, max_bytes=1000)
    with pytest.raises(ResponseTooLarge):
        http2.get("https://api.example.com/")


def test_redirects_are_checked_and_strip_credentials_across_origins():
    def handler(r):
        if r.url.path == "/start":
            return httpx.Response(302, headers={"location": "https://other.example.com/next"})
        if r.url.path == "/same":
            return httpx.Response(307, headers={"location": "/final"})
        return httpx.Response(200, json={"path": r.url.path})
    http, seen, _ = client(handler)
    assert http.get("https://api.example.com/start", headers={"authorization": "Bearer t"}).json() == {"path": "/next"}
    assert "authorization" not in seen[-1].headers and seen[-1].url.host == "other.example.com"
    assert http.get("https://api.example.com/same", headers={"authorization": "Bearer t"}).json() == {"path": "/final"}
    assert seen[-1].headers["authorization"] == "Bearer t"
    assert http.get("https://api.example.com/start", follow_redirects=False).status_code == 302


@pytest.mark.parametrize("location", ["https://169.254.169.254/latest/meta-data/", "http://api.example.com/x",
                                      "https://internal.example.com/", "https://127.0.0.1/admin"])
def test_redirect_to_a_forbidden_target_is_refused(location):
    http, seen, _ = client(lambda r: httpx.Response(301, headers={"location": location}),
                           hosts={"internal.example.com": "192.168.0.10"})
    with pytest.raises(UnsafeURLError, match="Redirect refused"):
        http.get("https://api.example.com/")
    assert len(seen) == 1                        # the forbidden target was never contacted


def test_303_turns_a_post_into_a_get_and_redirect_loops_end():
    def handler(r):
        if r.url.path == "/form":
            return httpx.Response(303, headers={"location": "/done"})
        if r.url.path == "/loop":
            return httpx.Response(302, headers={"location": "/loop"})
        return httpx.Response(200, json={"method": r.method, "body": r.content.decode()})
    http, _, _ = client(handler)
    assert http.post("https://api.example.com/form", json={"a": 1}).json() == {"method": "GET", "body": ""}
    with pytest.raises(UpstreamError, match="Too many redirects"):
        http.get("https://api.example.com/loop")


def test_retries_5xx_with_backoff_for_idempotent_requests():
    answers = iter([503, 502, 200])
    http, seen, sleeps = client(lambda r: httpx.Response(next(answers)), retries=2, backoff=0.5)
    assert http.get("https://api.example.com/").status_code == 200
    assert len(seen) == 3 and len(sleeps) == 2 and 0.3 <= sleeps[0] <= 0.7 and 0.7 <= sleeps[1] <= 1.3


def test_retry_after_is_honored_and_long_waits_are_handed_back():
    answers = iter([httpx.Response(429, headers={"retry-after": "2"}), httpx.Response(200)])
    http, seen, sleeps = client(lambda r: next(answers))
    assert http.post("https://api.example.com/", json={}).status_code == 200      # 429: safe to retry a POST
    assert sleeps == [2.0]
    http2, seen2, sleeps2 = client(lambda r: httpx.Response(429, headers={"retry-after": "3600"}), max_backoff=30)
    assert http2.get("https://api.example.com/").status_code == 429 and sleeps2 == [] and len(seen2) == 1
    date = "Wed, 21 Oct 2015 07:28:00 GMT"       # an HTTP date in the past = retry now
    answers3 = iter([httpx.Response(503, headers={"retry-after": date}), httpx.Response(200)])
    http3, _, sleeps3 = client(lambda r: next(answers3))
    assert http3.get("https://api.example.com/").status_code == 200 and sleeps3 == [0.0]


def test_post_is_not_retried_on_5xx_unless_idempotent():
    http, seen, _ = client(lambda r: httpx.Response(503))
    assert http.post("https://api.example.com/", json={}).status_code == 503 and len(seen) == 1
    http2, seen2, _ = client(lambda r: httpx.Response(503), retries=2)
    assert http2.post("https://api.example.com/", json={}, idempotent=True).status_code == 503 and len(seen2) == 3


def test_transport_errors():
    def refuse(r):
        raise httpx.ConnectError("connection refused", request=r)
    http, seen, _ = client(refuse, retries=2)
    with pytest.raises(UpstreamError, match="Could not connect to api.example.com"):
        http.post("https://api.example.com/secret/path?token=abc", json={})
    assert len(seen) == 3                        # nothing was sent, so even a POST is retried

    def slow(r):
        raise httpx.ReadTimeout("timed out", request=r)
    http2, seen2, _ = client(slow, retries=2)
    with pytest.raises(UpstreamError, match="did not answer in time") as e:
        http2.post("https://api.example.com/", json={})
    assert len(seen2) == 1 and "token" not in str(e.value)       # a POST that may have arrived is not repeated
    http3, seen3, _ = client(slow, retries=2)
    with pytest.raises(UpstreamError):
        http3.get("https://api.example.com/")
    assert len(seen3) == 3


def test_logs_name_the_host_never_the_path_or_query(caplog):
    http, _, _ = client(lambda r: httpx.Response(200, text="ok"))
    with caplog.at_level(logging.DEBUG, logger="tradecomply.http"):
        http.post("https://hooks.slack.com/services/T000/B000/XXXXSECRETXXXX?sig=abc", json={"text": "hi"})
    assert "hooks.slack.com" in caplog.text and "XXXXSECRETXXXX" not in caplog.text and "sig=abc" not in caplog.text


def test_connect_time_guard_blocks_dns_rebinding():
    """The pre-check sees a public address; the second lookup, when connecting, answers 127.0.0.1. The guarded
    transport re-checks at connect time and never opens the socket."""
    answers = iter([[PUBLIC_TEST_IP], ["127.0.0.1"]])

    def rebinding(host, port):
        return next(answers)
    http = SafeHTTP(transport=GuardedTransport(rebinding), resolver=rebinding, retries=0)
    with pytest.raises(UnsafeURLError, match="private or reserved"):
        http.get("https://rebind.example.com/")


def test_guarded_backend_refuses_before_connecting():
    backend = GuardedBackend(fake_resolver({"meta.example.com": "169.254.169.254"}))
    with pytest.raises(BlockedAddress):
        backend.connect_tcp("meta.example.com", 443)
    with pytest.raises(BlockedAddress):
        backend.connect_tcp("10.0.0.1", 443)


def test_guarded_transport_really_connects_to_an_allowed_host():
    """End to end over a real socket: a local server is reachable only in test mode (allow_localhost)."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"hello": "world"}'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        with SafeHTTP(allow_localhost=True, retries=0) as http_:
            assert http_.get(url).json() == {"hello": "world"}
        with SafeHTTP(retries=0) as strict, pytest.raises(UnsafeURLError):
            strict.get(url)
    finally:
        server.shutdown()
        server.server_close()


def test_a_307_never_re_sends_a_body_to_another_site():
    """Security review: a 307/308 keeps the method and body (an OAuth form with a token or client secret). Within
    the same origin that is fine; to another origin the request is refused and the other site is never contacted."""
    def handler(r):
        if r.url.path == "/token":
            return httpx.Response(307, headers={"location": "https://collector.example.net/steal"})
        if r.url.path == "/moved":
            return httpx.Response(308, headers={"location": "/token-v2"})
        return httpx.Response(200, json={"path": r.url.path, "body": r.content.decode()})
    http, seen, _ = client(handler)
    with pytest.raises(UnsafeURLError, match="Redirect refused.*with a body"):
        http.post("https://login.example.com/token", data={"client_secret": "csecret", "token": "rt"})
    assert [r.url.host for r in seen] == ["login.example.com"]
    out = http.post("https://login.example.com/moved", data={"token": "rt"}).json()
    assert out == {"path": "/token-v2", "body": "token=rt"}                  # same origin: kept
    assert http.get("https://login.example.com/token").json()["path"] == "/steal"   # no body: followed
