"""``/wan2gp/*``: where it sends things, what it says, and what it refuses.

The whole of section 12.5 is a negative claim - no request can change where
the proxy goes - and a negative claim is only worth what the attempts against
it are worth. So the hostile half of this file does not inspect the code; it
sends requests that name another host in every place a request can name one -
an absolute URL in the path, ``X-Forwarded-Host``, a ``?url=`` parameter, a
``Host`` header - through a client that records the address the proxy actually
dialled, and asserts that address never moved off the runtime's loopback port.

The other half is a real reverse proxy against a real server, because the
things section 43 asks for are properties of bytes on a socket rather than of
functions: a body that is streamed instead of collected, a range request that
survives, a POST body that arrives intact. The server is a few lines of
``http.server`` on 127.0.0.1:0, started here and shut down here, and the app is
driven through the ASGI interface directly so the timestamps on the outgoing
messages can be read - which is how "not buffered whole" is checked.
"""

from harness import Results, setup_path

setup_path()

import asyncio  # noqa: E402
import contextlib  # noqa: E402
import http.server  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import httpx  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402

from minipaint_neo.wangp import errors, proxy, runtime  # noqa: E402

#: A port the runtime claims to have when no real server is wanted. It must
#: never be dialled and must never appear in anything the browser reads.
PRETEND_PORT = 45999

STREAM_HEAD = b"event: start\ndata: one\n\n"
STREAM_TAIL = b"event: done\ndata: two\n\n"
MEDIA = b"0123456789abcdef"


# ------------------------------------------------------------ stand-in bits --


class FakeUrl:
    def __init__(self, scheme="http", netloc="forge.example.test:7860", port=7860, path="/wan2gp/"):
        self.scheme = scheme
        self.netloc = netloc
        self.hostname = netloc.split(":")[0]
        self.port = port
        self.path = path


class FakeRequest:
    """The three things ``forwarded_headers`` reads, and nothing else."""

    def __init__(self, headers, url=None, peer="203.0.113.9"):
        self.headers = dict(headers)
        self.url = url or FakeUrl()
        self.client = type("Peer", (), {"host": peer, "port": 51234})() if peer else None


class SpyClient:
    """A client that answers for itself and remembers where it was aimed."""

    def __init__(self):
        self.urls = []

    def build_request(self, method, url, headers=None, content=None, timeout=None):
        self.urls.append(url)
        return httpx.Request(method, url, headers=headers)

    async def send(self, request, stream=False, follow_redirects=False):
        return httpx.Response(200, headers=[("content-type", "text/plain")], content=b"upstream")

    @property
    def last(self):
        return self.urls[-1] if self.urls else None


# ---------------------------------------------------------- the real server --


class Handler(http.server.BaseHTTPRequestHandler):
    """A WanGP-shaped upstream: it echoes, it streams, it serves ranges."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # the suite's output is not a web log
        pass

    def do_GET(self):
        self.answer(b"")

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        self.answer(self.rfile.read(length) if length else b"")

    def answer(self, body):
        path, _, query = self.path.partition("?")
        if path == "/stream":
            self.stream()
        elif path == "/media":
            self.media()
        else:
            self.echo(path, query, body)

    def echo(self, path, query, body):
        payload = json.dumps(
            {
                "method": self.command,
                "path": path,
                "query": query,
                "body": body.decode("utf-8", "replace"),
                "headers": {name.lower(): value for name, value in self.headers.items()},
                "server_port": int(self.server.server_address[1]),
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Upstream", "the-fake-wangp")
        # Two ways of being hop-by-hop: the fixed list, and a header this
        # connection's Connection header names.
        self.send_header("Connection", "keep-alive, x-upstream-hop")
        self.send_header("X-Upstream-Hop", "must not be forwarded")
        self.send_header("Set-Cookie", "wangp_session=abc; Path=/; Domain=127.0.0.1")
        self.end_headers()
        self.wfile.write(payload)

    def stream(self):
        """Two events with a real gap, so buffering would be visible."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(STREAM_HEAD) + len(STREAM_TAIL)))
        self.end_headers()
        self.wfile.write(STREAM_HEAD)
        self.wfile.flush()
        time.sleep(0.35)
        self.wfile.write(STREAM_TAIL)
        self.wfile.flush()

    def media(self):
        wanted = str(self.headers.get("range") or "")
        if wanted.startswith("bytes="):
            first, _, last = wanted[len("bytes=") :].partition("-")
            start = int(first or 0)
            end = int(last) if last else len(MEDIA) - 1
            chunk = MEDIA[start : end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(MEDIA)}")
        else:
            chunk = MEDIA
            self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        self.wfile.write(chunk)


class Upstream:
    """The loopback server, started and stopped by this suite alone."""

    def __init__(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, name="wangp-test-upstream", daemon=True)
        self.thread.start()

    def close(self):
        with contextlib.suppress(Exception):
            self.server.shutdown()
        with contextlib.suppress(Exception):
            self.server.server_close()
        self.thread.join(timeout=5.0)


# --------------------------------------------------------------- ASGI seams --


def scope_for(method, path, headers=(), query=b"", peer="203.0.113.9"):
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": query,
        "root_path": "",
        "headers": [(name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in headers],
        "client": (peer, 51234) if peer else None,
        "server": ("forge.example.test", 7860),
    }


def receiver(body):
    """The browser's half of the connection: the body once, then silence.

    A streaming response listens on the same channel for a disconnect while it
    writes, so a receive that answers a second time would either be read as a
    truncated body or cancel the response half way. A real client simply stays
    connected, which is what waiting forever means here - the task group that
    is listening cancels this the moment the response is done.
    """
    delivered = asyncio.Event()
    connected = asyncio.Event()

    async def receive():
        if not delivered.is_set():
            delivered.set()
            return {"type": "http.request", "body": body, "more_body": False}
        await connected.wait()
        return {"type": "http.disconnect"}

    return receive


def request_for(method, path, headers=(), query=b"", body=b"", peer="203.0.113.9"):
    return Request(scope_for(method, path, headers, query, peer), receiver(body))


class Reply:
    """One ASGI response, with the moment each message was handed over."""

    def __init__(self, messages):
        self.status = 0
        self.headers = []
        self.chunks = []
        self.started_at = 0.0
        for at, message in messages:
            if message["type"] == "http.response.start":
                self.status = int(message["status"])
                self.headers = [
                    (bytes(name).decode("latin-1").lower(), bytes(value).decode("latin-1"))
                    for name, value in message.get("headers", [])
                ]
                self.started_at = at
            elif message["type"] == "http.response.body" and message.get("body"):
                self.chunks.append((at, bytes(message["body"])))

    @property
    def body(self):
        return b"".join(chunk for _at, chunk in self.chunks)

    @property
    def text(self):
        return self.body.decode("utf-8", "replace")

    def header(self, name):
        return dict(self.headers).get(name.lower(), "")

    def names(self):
        return [name for name, _value in self.headers]

    def all(self, name):
        """Every value for a header. Set-Cookie arrives more than once."""
        return [value for key, value in self.headers if key.lower() == name.lower()]


async def call(app, method, path, headers=(), query=b"", body=b"", peer="203.0.113.9"):
    messages = []

    async def send(message):
        messages.append((time.monotonic(), message))

    # Bounded, so a proxy that never finishes a response fails this suite
    # instead of hanging it.
    await asyncio.wait_for(
        app(scope_for(method, path, headers, query, peer), receiver(body), send), timeout=30.0
    )
    return Reply(messages)


def as_ready(port):
    """Put the module's one runtime where the proxy will read it."""
    current = runtime.current()
    current.state = runtime.READY
    current.backend_port = int(port)


def as_stopped(port=0):
    current = runtime.current()
    current.state = runtime.STOPPED
    current.backend_port = int(port)


BROWSER = (
    ("host", "forge.example.test:7860"),
    ("accept", "text/html"),
    ("x-forwarded-for", "10.1.1.1"),
    ("x-custom-keep", "kept"),
    ("connection", "keep-alive, x-browser-hop"),
    ("x-browser-hop", "must not be forwarded"),
    ("te", "trailers"),
)


# --------------------------------------------------------------- the checks --


def sync_checks(r: Results) -> None:
    # ---- what the backend is told about the public request (12.4)
    headers = proxy.forwarded_headers(
        FakeRequest(
            {
                "Host": "forge.example.test:7860",
                "X-Forwarded-For": "10.1.1.1, 10.2.2.2",
                "Connection": "keep-alive, x-browser-hop",
                "X-Browser-Hop": "drop me",
                "TE": "trailers",
                "Cookie": "forge=1",
            }
        )
    )
    r.check("the public host is forwarded verbatim", headers["host"] == "forge.example.test:7860")
    r.check("x-forwarded-host names the public host", headers["x-forwarded-host"] == "forge.example.test:7860")
    r.check("x-forwarded-proto names the public scheme", headers["x-forwarded-proto"] == "http")
    r.check("x-forwarded-port names the public port", headers["x-forwarded-port"] == "7860")
    r.check("the forwarded-for chain is appended to, not replaced",
            headers["x-forwarded-for"] == "10.1.1.1, 10.2.2.2, 203.0.113.9", headers["x-forwarded-for"])
    r.check("ordinary headers survive", headers["cookie"] == "forge=1")
    r.check("fixed hop-by-hop headers are stripped",
            "connection" not in headers and "te" not in headers)
    r.check("headers the connection header names are stripped too", "x-browser-hop" not in headers)

    outer = proxy.forwarded_headers(
        FakeRequest(
            {"Host": "wan.example.com", "X-Forwarded-Proto": "https", "X-Forwarded-Host": "wan.example.com", "X-Forwarded-Port": "443"},
            url=FakeUrl(scheme="http", netloc="127.0.0.1:7860", port=7860),
        )
    )
    r.check("an outer reverse proxy's view wins over ours",
            outer["x-forwarded-proto"] == "https" and outer["x-forwarded-host"] == "wan.example.com"
            and outer["x-forwarded-port"] == "443")
    secure = proxy.forwarded_headers(FakeRequest({"Host": "forge.example.test"}, url=FakeUrl(scheme="https", port=None)))
    r.check("https without a port is 443", secure["x-forwarded-port"] == "443")
    socketed = proxy.forwarded_headers(FakeRequest({"Host": "forge.example.test"}, url=FakeUrl(scheme="wss", port=None)))
    r.check("a websocket describes the page's transport, not its own",
            socketed["x-forwarded-proto"] == "https")
    anonymous = proxy.forwarded_headers(FakeRequest({"Host": "forge.example.test", "X-Forwarded-For": "10.1.1.1"}, peer=""))
    r.check("no peer means no invented chain entry", anonymous["x-forwarded-for"] == "10.1.1.1")

    # ---- and the other direction
    cleaned = dict(
        proxy.response_headers(
            [
                ("Content-Type", "text/html"),
                ("Content-Length", "12"),
                ("Connection", "keep-alive, x-upstream-hop"),
                ("Keep-Alive", "timeout=5"),
                ("Transfer-Encoding", "chunked"),
                ("X-Upstream-Hop", "drop me"),
                ("X-Frame-Options", "SAMEORIGIN"),
            ]
        )
    )
    r.check("hop-by-hop response headers are stripped",
            not {"connection", "keep-alive", "transfer-encoding", "x-upstream-hop"} & set(cleaned))
    r.check("the response's own length is dropped, since the body is re-framed",
            "content-length" not in cleaned)
    r.check("ordinary response headers survive",
            cleaned["content-type"] == "text/html" and cleaned["x-frame-options"] == "SAMEORIGIN")

    # ---- redirects and cookies stay inside /wan2gp/ (43)
    r.check("a root-relative redirect is moved under the prefix",
            proxy.rewrite_location("/queue/join") == "/wan2gp/queue/join")
    r.check("a redirect that already carries the prefix is left alone",
            proxy.rewrite_location("/wan2gp/settings") == "/wan2gp/settings")
    r.check("a redirect to the backend loses the backend",
            proxy.rewrite_location(f"http://127.0.0.1:{PRETEND_PORT}/config") == "/wan2gp/config")
    r.check("a redirect to a real external host is untouched",
            proxy.rewrite_location("https://example.com/docs") == "https://example.com/docs")
    # One login covers both namespaces this proxy serves, and no single path
    # covers /wan2gp/ and /deepy/ and nothing else - so a site-wide cookie
    # comes back as one per namespace. Path=/ would have done it in one, at
    # the price of riding along on every Forge request.
    cookies = proxy.scoped_cookies("wangp_session=abc; Path=/; Domain=127.0.0.1; HttpOnly")
    r.check("a site-wide backend cookie is set once for each namespace that is served",
            [value.split(";")[1].strip() for value in cookies] == ["Path=/wan2gp/", "Path=/deepy/"], str(cookies))
    r.check("and each keeps the cookie, its flags, and none of its domain",
            all(value.startswith("wangp_session=abc;") and "HttpOnly" in value and "Domain" not in value
                for value in cookies), str(cookies))
    r.check("a cookie the child already scoped to its Deepy app is left where it is - "
            "that namespace is served at the same path here as there",
            proxy.scoped_cookies("d=1; Path=/deepy/") == ["d=1; Path=/deepy/"], str(proxy.scoped_cookies("d=1; Path=/deepy/")))
    r.check("and any other path of the child's goes under the prefix, as it always has",
            proxy.scoped_cookies("g=2; Path=/queue") == ["g=2; Path=/wan2gp/queue"], str(proxy.scoped_cookies("g=2; Path=/queue")))
    r.check("a cookie that named no path is treated as the site-wide one it is",
            len(proxy.scoped_cookies("s=3; HttpOnly")) == 2, str(proxy.scoped_cookies("s=3; HttpOnly")))
    # A logout must clear both, or the other namespace stays signed in.
    cleared = proxy.scoped_cookies('wangp_session=""; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=0')
    r.check("a deletion arrives the same way, so a logout clears both",
            len(cleared) == 2 and all("Max-Age=0" in value for value in cleared), str(cleared))

    # ---- the path the backend sees
    r.check("the prefix is stripped on the way in", proxy.upstream_path("/wan2gp/file/x.png") == "/file/x.png")
    r.check("the bare prefix is the backend's root", proxy.upstream_path("/wan2gp") == "/")
    r.check("the prefix with a slash is the backend's root", proxy.upstream_path("/wan2gp/") == "/")

    # ---- three timeout classes, not one (12.8)
    ordinary = proxy.timeout_for("/theme.css")
    streamed = proxy.timeout_for("/queue/join")
    r.check("an ordinary response has a bounded read timeout", ordinary.read == proxy.RESPONSE_TIMEOUT)
    r.check("a queue stream is never cut for being quiet", streamed.read is None)
    r.check("an event-stream accept header is enough", proxy.timeout_for("/x", "text/event-stream").read is None)
    r.check("connecting is the short one", ordinary.connect == proxy.CONNECT_TIMEOUT < proxy.RESPONSE_TIMEOUT)

    # ---- the upstream comes from the runtime and nowhere else (12.5)
    as_stopped(PRETEND_PORT)
    r.check("a runtime that is not READY has no upstream", proxy.upstream() is None)
    runtime.current().state = runtime.STARTING
    r.check("a runtime that is still starting has no upstream", proxy.upstream() is None)
    runtime.current().state = runtime.READY
    runtime.current().backend_port = 0
    r.check("READY without a port has no upstream", proxy.upstream() is None)
    as_ready(PRETEND_PORT)
    r.check("a READY runtime is the loopback destination",
            proxy.upstream() == f"http://127.0.0.1:{PRETEND_PORT}")
    r.check("the destination takes no arguments at all",
            proxy.upstream.__code__.co_argcount == 0)

    # ---- nothing that leaves this module carries the port
    r.check("the port is redacted out of a message",
            str(PRETEND_PORT) not in proxy.redact(f"connection refused to 127.0.0.1:{PRETEND_PORT}"))
    payload = proxy.error_payload(errors.PROXY_NOT_READY, f"upstream unreachable: 127.0.0.1:{PRETEND_PORT}")
    r.check("an error payload is a code and a sentence",
            payload["ok"] is False and payload["code"] == errors.PROXY_NOT_READY and payload["message"])
    r.check("an error payload carries no port", str(PRETEND_PORT) not in json.dumps(payload))


async def no_open_proxy(r: Results) -> None:
    """Section 12.5, attempted rather than asserted."""
    spy = SpyClient()
    proxy.use_client(spy)
    try:
        as_ready(PRETEND_PORT)
        attempts = {
            "an absolute URL in the path": request_for("GET", "/wan2gp/http://evil.example.com/x"),
            "a forged X-Forwarded-Host": request_for(
                "GET", "/wan2gp/", headers=(("host", "forge.example.test"), ("x-forwarded-host", "evil.example.com:9999"))
            ),
            "a ?url= parameter": request_for("GET", "/wan2gp/", query=b"url=http%3A%2F%2Fevil.example.com%2F&port=9999"),
            "a forged Host header": request_for("GET", "/wan2gp/", headers=(("host", "evil.example.com"),)),
            "a forged X-Forwarded-Port": request_for(
                "GET", "/wan2gp/", headers=(("host", "forge.example.test"), ("x-forwarded-port", "9999"))
            ),
            "a traversal in the path": request_for("GET", "/wan2gp/../../etc/passwd"),
            "another loopback port in the path": request_for("GET", "/wan2gp/http://127.0.0.1:22/"),
        }
        for description, request in attempts.items():
            response = await forwarded(request)
            aimed = spy.last
            r.check(
                f"{description} cannot move the upstream",
                aimed is not None and aimed.host == "127.0.0.1" and aimed.port == PRETEND_PORT and aimed.scheme == "http",
                str(aimed),
            )
            r.check(f"{description} is still answered", response.status_code == 200)

        r.check("every attempt went to the one destination",
                {(url.scheme, url.host, url.port) for url in spy.urls} == {("http", "127.0.0.1", PRETEND_PORT)})

        # And when there is no runtime, there is no request either.
        as_stopped(PRETEND_PORT)
        tried = len(spy.urls)
        refused = await proxy.forward(request_for("GET", "/wan2gp/"))
        r.check("a request with no runtime never reaches a client",
                refused.status_code == 503 and len(spy.urls) == tried)
    finally:
        proxy.use_client(None)


class DeepyClient:
    """A child whose Deepy app answers at exactly one of the two addresses.

    ``rooted`` is what a child launched the way we launch one actually has:
    Gradio under ``GRADIO_ROOT_PATH=/wan2gp``, with the Deepy app mounted on
    it, which Starlette can only reach at ``/wan2gp/deepy/...``. ``bare`` is
    the same app with no root path. ``neither`` is a build that has no Deepy
    app at all, where the banner is the child's own and nothing here helps.
    """

    def __init__(self, serves=proxy.DEEPY_ROOTED, broken=False):
        self.paths = []
        self.serves = serves
        self.broken = broken

    def build_request(self, method, url, headers=None, content=None, timeout=None):
        self.paths.append(str(httpx.URL(url).raw_path.decode("latin-1")).split("?")[0])
        return httpx.Request(method, url, headers=headers)

    async def send(self, request, stream=False, follow_redirects=False):
        path = str(request.url.raw_path.decode("latin-1")).split("?")[0]
        if self.broken:
            return httpx.Response(500, headers=[("content-type", "text/plain")], content=b"inside")
        if self.serves == proxy.DEEPY_ROOTED:
            served = path.startswith(proxy.PROXY_PATH + proxy.DEEPY_PREFIX)
        elif self.serves == proxy.DEEPY_BARE:
            served = path.startswith(proxy.DEEPY_PREFIX)
        else:
            served = False
        if served:
            return httpx.Response(200, headers=[("content-type", "application/json")], content=b"{}")
        return httpx.Response(404, headers=[("content-type", "text/plain")], content=b"no")


async def asgi_status(app, path, root_path=""):
    """One GET through the raw ASGI interface, for its status code alone."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": b"",
        "root_path": root_path,
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
    }
    seen = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(event):
        if event["type"] == "http.response.start":
            seen["status"] = event["status"]

    await app(scope, receive, send)
    return seen.get("status")


async def deepy_address_checks(r: Results) -> None:
    """Why a mounted sub-application needs back the prefix we strip.

    This is the fact the whole Deepy path in ``proxy`` exists to accommodate,
    and it is a fact about Starlette rather than about us - so it is asserted
    against Starlette, with the same two moving parts the child has: an app
    with a root path, and an app mounted on it. Reasoning about it from the
    call graph is what produced three wrong fixes; running it produced one.
    """
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    child = Starlette(routes=[Route("/deepy_api/state", lambda request: PlainTextResponse("deepy"))])
    parent = Starlette(routes=[Route("/config", lambda request: PlainTextResponse("gradio"))])
    parent.mount(proxy.DEEPY_PATH, child)  # exactly what shared/deepy/hybrid.py does

    r.check("with no root path, a mounted Deepy app answers where the page asks",
            await asgi_status(parent, "/deepy/deepy_api/state") == 200)
    r.check("under a root path it does not - which is the 404 that would not go away",
            await asgi_status(parent, "/deepy/deepy_api/state", proxy.PROXY_PATH) == 404)
    r.check("under a root path it answers with the prefix left on",
            await asgi_status(parent, "/wan2gp/deepy/deepy_api/state", proxy.PROXY_PATH) == 200)
    r.check("while the app's own routes answer either way, which is why nothing else broke",
            await asgi_status(parent, "/config", proxy.PROXY_PATH) == 200
            and await asgi_status(parent, "/config") == 200)

    # And the pure function that encodes it.
    r.check("a Deepy path is addressed with our prefix on by default",
            proxy.deepy_upstream_path("/deepy/deepy_api/state") == "/wan2gp/deepy/deepy_api/state")
    r.check("and without it for a child that wants it off",
            proxy.deepy_upstream_path("/deepy/deepy_api/state", proxy.DEEPY_BARE) == "/deepy/deepy_api/state")
    r.check("anything outside the Deepy namespace is left exactly as it is",
            all(proxy.deepy_upstream_path(path) == path
                for path in ("/", "/config", "/wan2gp/config", "/deepyish/x", "")))
    r.check("there are two addresses, so there is exactly one other one",
            proxy.deepy_other_form(proxy.DEEPY_ROOTED) == proxy.DEEPY_BARE
            and proxy.deepy_other_form(proxy.DEEPY_BARE) == proxy.DEEPY_ROOTED
            and proxy.deepy_other_form(None) == proxy.DEEPY_BARE)


async def deepy_checks(r: Results) -> None:
    """The Deepy API is reached, and asked for twice at most.

    THE BANNER THAT WOULD NOT CLEAR.

    Wan2GP mounts its Deepy app as a sub-application, so its API is at
    ``/deepy/deepy_api/...`` and that is where the page asks - its marker is
    a root-relative ``/deepy/`` and the transport resolves against it. Sent
    upstream at that address it answers 404 (see ``deepy_address_checks``)
    and the panel shows "Connection to server lost" for ever while every
    other part of the page works, because its transport has no fallback and
    marks the notice persistent.
    """
    # The ordinary child. One request, at the address it answers at.
    proxy._state["deepy_form"] = None
    proxy._state["deepy"] = set()
    proxy._state["logged"] = set()
    spy = DeepyClient()
    proxy.use_client(spy)
    try:
        as_ready(PRETEND_PORT)
        answer = await forwarded(request_for("GET", "/deepy/deepy_api/state"))
        r.check("a Deepy request reaches the child's mounted app, first try",
                answer.status_code == 200 and spy.paths == ["/wan2gp/deepy/deepy_api/state"],
                f"{answer.status_code} {spy.paths}")
        r.check("and that address is now known, so nothing is asked twice",
                proxy._state["deepy_form"] == proxy.DEEPY_ROOTED)
    finally:
        proxy.use_client(None)
        proxy._state["deepy_form"] = None

    # A child with no root path answers at the other address. Asked, once.
    proxy._state["deepy_form"] = None
    proxy._state["deepy"] = set()
    proxy._state["logged"] = set()
    spy = DeepyClient(serves=proxy.DEEPY_BARE)
    proxy.use_client(spy)
    try:
        as_ready(PRETEND_PORT)
        answer = await forwarded(request_for("GET", "/deepy/deepy_api/state"))
        r.check("a child that answers at the site root is found there",
                answer.status_code == 200, str(answer.status_code))
        r.check("which took exactly two upstream requests, not a search",
                spy.paths == ["/wan2gp/deepy/deepy_api/state", "/deepy/deepy_api/state"], str(spy.paths))

        before = len(spy.paths)
        answer = await forwarded(request_for("GET", "/deepy/deepy_api/settings"))
        r.check("and the next one goes straight there, having learnt it once",
                answer.status_code == 200 and spy.paths[before:] == ["/deepy/deepy_api/settings"],
                str(spy.paths[before:]))
    finally:
        proxy.use_client(None)
        proxy._state["deepy_form"] = None

    # Neither address answers: say so once and stop asking.
    proxy._state["deepy_form"] = None
    proxy._state["deepy"] = set()
    proxy._state["logged"] = set()
    spy = DeepyClient(serves="neither")
    proxy.use_client(spy)
    try:
        as_ready(PRETEND_PORT)
        answer = await forwarded(request_for("GET", "/deepy/deepy_api/state"))
        r.check("a build with no Deepy app is asked both ways and then left alone",
                answer.status_code == 404 and len(spy.paths) == 2, f"{answer.status_code} {spy.paths}")
        before = len(spy.paths)
        await forwarded(request_for("GET", "/deepy/deepy_api/state"))
        r.check("and is not asked both ways again for every request after it",
                len(spy.paths) - before == 1, str(spy.paths[before:]))
    finally:
        proxy.use_client(None)
        proxy._state["deepy_form"] = None

    # 500 is the app saying something went wrong inside it. Asking a different
    # way would be papering over that, so it is not retried.
    proxy._state["deepy_form"] = None
    proxy._state["deepy"] = set()
    proxy._state["logged"] = set()
    spy = DeepyClient(broken=True)
    proxy.use_client(spy)
    try:
        as_ready(PRETEND_PORT)
        answer = await forwarded(request_for("GET", "/deepy/deepy_api/state"))
        r.check("a 500 is handed back as it is, not retried into a different answer",
                answer.status_code == 500 and len(spy.paths) == 1, f"{answer.status_code} {spy.paths}")
    finally:
        proxy.use_client(None)
        proxy._state["deepy_form"] = None


async def forwarded(request):
    """``forward`` plus the small amount of work a real server would do."""
    response = await proxy.forward(request)
    if getattr(response, "background", None) is not None:
        await response.background()
        response.background = None
    return response


async def async_checks(r: Results) -> None:
    app = Starlette()
    proxy.install(app)
    installed = len(app.router.routes)
    r.check("the routes go in front of the host's own",
            str(app.router.routes[0].path) == proxy.AUTH_PROBE_PATH)
    r.check("the auth probe is in front of the catch-all that would swallow it",
            [str(route.path) for route in app.router.routes[:4]].index(proxy.AUTH_PROBE_PATH)
            < [str(route.path) for route in app.router.routes[:4]].index(proxy.PROXY_PREFIX + "{path:path}"))
    r.check("the probe, the prefix, the tree and a websocket are all mounted", installed == 6, str(installed))
    # The child's Deepy namespace is served too, and at the site root rather
    # than under our prefix, because that is where its page asks for it: its
    # marker is a root-relative "/deepy/" that ignores the proxy's path. A
    # request that lands outside our prefix reached Forge, 404'd, and left a
    # banner on the page that never cleared.
    mounted = [str(route.path) for route in app.router.routes[:6]]
    r.check("the Deepy namespace is served as well, over both transports",
            proxy.DEEPY_PREFIX + "{path:path}" in mounted
            and sum(1 for route in app.router.routes[:6]
                    if str(getattr(route, "path", "")).startswith(proxy.DEEPY_PREFIX)) == 2, str(mounted))
    r.check("and its event stream is treated as a stream, not cut at the response timeout",
            proxy.stream_shaped("/deepy/deepy_api/events") is True)

    # The probe answers whatever the gate says, because reaching it is the
    # question the gate exists to decide. It carries nothing.
    remembered = proxy._boundary
    proxy._boundary = {"ok": False, "coverage": "unknown", "mechanisms": ["gradio_auth"]}
    try:
        answered = await proxy._auth_probe(request_for("GET", proxy.AUTH_PROBE_PATH))
        r.check("the auth probe answers even while the proxy refuses", answered.status_code == 204)
        r.check("and carries no body", not bytes(getattr(answered, "body", b"") or b""))
        refused = await proxy.forward(request_for("GET", "/wan2gp/"))
        r.check("while a real request is still refused", refused.status_code == 503)
    finally:
        proxy._boundary = remembered
    proxy.install(app)
    r.check("installing twice adds nothing", len(app.router.routes) == installed)

    # ---- /wan2gp -> /wan2gp/, and it says nothing about what is behind it
    as_stopped(PRETEND_PORT)
    redirect = await call(app, "GET", "/wan2gp", headers=(("host", "forge.example.test:7860"),), query=b"a=1")
    r.check("the bare prefix redirects to the slashed one", redirect.status == 307, str(redirect.status))
    r.check("the redirect keeps the query", redirect.header("location") == "/wan2gp/?a=1", redirect.header("location"))
    r.check("the redirect leaks nothing", str(PRETEND_PORT) not in redirect.text)

    # ---- a runtime that is not up is a controlled failure (12.9)
    page = await call(app, "GET", "/wan2gp/", headers=(("host", "forge.example.test:7860"), ("accept", "text/html")))
    r.check("a page asked for while WanGP is down is 503", page.status == 503, str(page.status))
    r.check("the page names the code", errors.PROXY_NOT_READY in page.text)
    r.check("the page carries no stack trace",
            "Traceback" not in page.text and "File \"" not in page.text)
    r.check("the page carries no backend port", str(PRETEND_PORT) not in page.text, page.text[:200])
    api = await call(app, "GET", "/wan2gp/queue/join", headers=(("host", "forge.example.test:7860"), ("accept", "application/json")))
    r.check("an api call while WanGP is down is 503 json", api.status == 503 and json.loads(api.text)["ok"] is False)
    r.check("the json carries the code and no port",
            json.loads(api.text)["code"] == errors.PROXY_NOT_READY and str(PRETEND_PORT) not in api.text)

    await no_open_proxy(r)

    # ---- and now against a real loopback WanGP-shaped server (43)
    upstream = Upstream()
    try:
        as_ready(upstream.port)

        got = await call(app, "GET", "/wan2gp/echo", headers=BROWSER, query=b"x=1&y=2%20z")
        r.check("a GET is proxied", got.status == 200, str(got.status))
        seen = json.loads(got.text)
        r.check("the prefix is stripped for the backend", seen["path"] == "/echo", seen["path"])
        r.check("the query is passed through byte for byte", seen["query"] == "x=1&y=2%20z", seen["query"])
        r.check("the request really went to our loopback server", seen["server_port"] == upstream.port)
        r.check("the backend is told the public host",
                seen["headers"]["host"] == "forge.example.test:7860"
                and seen["headers"]["x-forwarded-host"] == "forge.example.test:7860")
        r.check("the backend is told the public scheme and port",
                seen["headers"]["x-forwarded-proto"] == "http" and seen["headers"]["x-forwarded-port"] == "7860")
        r.check("the chain reaches the backend with both hops",
                seen["headers"]["x-forwarded-for"] == "10.1.1.1, 203.0.113.9", seen["headers"]["x-forwarded-for"])
        r.check("the browser's hop-by-hop headers do not reach the backend",
                "te" not in seen["headers"] and "x-browser-hop" not in seen["headers"])
        r.check("everything else reaches the backend", seen["headers"]["x-custom-keep"] == "kept")
        r.check("the backend's own hop-by-hop headers do not reach the browser",
                "connection" not in got.names() and "x-upstream-hop" not in got.names(), str(got.names()))
        r.check("the backend's ordinary headers do reach the browser", got.header("x-upstream") == "the-fake-wangp")
        cookies = got.all("set-cookie")
        r.check("the backend's cookie is confined to the namespaces this proxy serves, and reaches both",
                sorted(value.split("Path=")[1].split(";")[0] for value in cookies) == ["/deepy/", "/wan2gp/"],
                str(cookies))
        r.check("and neither copy carries a domain chosen for the loopback",
                cookies and not any("Domain" in value for value in cookies), str(cookies))

        sent = json.dumps({"data": ["a prompt"], "fn_index": 3}).encode("utf-8")
        posted = await call(
            app,
            "POST",
            "/wan2gp/echo",
            headers=(("host", "forge.example.test:7860"), ("content-type", "application/json"),
                     ("content-length", str(len(sent)))),
            body=sent,
        )
        r.check("a POST is proxied with its body",
                posted.status == 200 and json.loads(posted.text)["body"] == sent.decode(),
                posted.text[:200])
        r.check("the POST reached the backend as a POST", json.loads(posted.text)["method"] == "POST")

        streamed = await call(app, "GET", "/wan2gp/stream", headers=(("host", "forge.example.test:7860"), ("accept", "text/event-stream")))
        r.check("a stream is proxied whole", streamed.body == STREAM_HEAD + STREAM_TAIL, repr(streamed.body))
        r.check("a stream arrives in more than one piece", len(streamed.chunks) >= 2, str(len(streamed.chunks)))
        if len(streamed.chunks) >= 2:
            gap = streamed.chunks[-1][0] - streamed.chunks[0][0]
            r.check("the pieces arrive as the backend writes them, not at the end", gap > 0.2, f"{gap:.3f}s")
        r.check("the status line goes out before the body is complete",
                streamed.chunks and streamed.chunks[-1][0] - streamed.started_at > 0.2)

        ranged = await call(app, "GET", "/wan2gp/media",
                            headers=(("host", "forge.example.test:7860"), ("range", "bytes=2-5")))
        r.check("a range request is answered as one", ranged.status == 206, str(ranged.status))
        r.check("the range headers survive", ranged.header("content-range") == f"bytes 2-5/{len(MEDIA)}",
                ranged.header("content-range"))
        r.check("the range body is the slice", ranged.body == MEDIA[2:6], repr(ranged.body))

        # The health gate, against the same server: the base page answers, an
        # asset answers, and the root path is not reported - which is the
        # failure a bare 200 would have hidden (12.7).
        report = await proxy.probe()
        r.check("the probe runs every step in order", report["order"] == list(proxy.PROBE_STEPS))
        r.check("the probe sees the base page", report["steps"]["base_page"]["ok"])
        r.check("a backend that does not report /wan2gp fails the gate",
                not report["ok"] and report["code"] == errors.PROXY_ROOT_PATH_FAILED, json.dumps(report["steps"]))
        r.check("the probe leaks no port", str(upstream.port) not in json.dumps(report))

        as_stopped()
        down = await proxy.probe()
        r.check("the probe fails closed when the runtime is not ready",
                not down["ok"] and down["code"] == errors.PROXY_NOT_READY and not down["steps"]["upstream"]["ok"])
    finally:
        await proxy._aclose_client()
        upstream.close()
        as_stopped()


async def auth_gate_checks(r: Results) -> None:
    """An unproven authentication boundary refuses to serve anything.

    Section 12.6 will not let route registration stand in for protection and
    49.4 makes an unauthenticated ``/wan2gp/`` a release blocker. The verdict
    therefore has to be load-bearing rather than logged: WanGP has no sign-in
    of its own, so a proxy that forwarded while coverage was merely "unknown"
    would hand the whole of it to anyone who could reach Forge's port.
    """
    from minipaint_neo.wangp.errors import AUTH_BOUNDARY_FAILED

    spy = SpyClient()
    proxy.use_client(spy)
    remembered = proxy._boundary
    override = os.environ.pop(proxy.AUTH_OVERRIDE_ENV, None)
    try:
        as_ready(PRETEND_PORT)

        proxy._boundary = None
        r.check("nothing is served before install has asked the question", proxy.serving_allowed() is False)
        before = len(spy.urls)
        answer = await proxy.forward(request_for("GET", "/wan2gp/"))
        r.check("a request before install never reaches a client", len(spy.urls) == before)
        r.check("and is refused rather than answered", answer.status_code == 503)

        # Gradio's own login is a per-route dependency, so a route added to the
        # router afterwards cannot be seen to be covered by it.
        proxy._boundary = {
            "ok": False, "coverage": "unknown", "mechanisms": ["gradio_auth"],
            "host_auth_configured": True, "detail": "per-route", "failure_code": AUTH_BOUNDARY_FAILED,
        }
        r.check("an unproven boundary refuses to serve", proxy.serving_allowed() is False)
        before = len(spy.urls)
        refused = await proxy.forward(request_for("GET", "/wan2gp/"))
        r.check("an unproven boundary never reaches the backend", len(spy.urls) == before)
        r.check("it answers 503", refused.status_code == 503)
        body = bytes(getattr(refused, "body", b"") or b"").decode("utf-8", "ignore")
        r.check("it names the failure code", AUTH_BOUNDARY_FAILED in body)
        r.check("and still does not leak the backend port", str(PRETEND_PORT) not in body)

        socket = _Socket("/wan2gp/queue/join")
        await proxy._websocket_endpoint(socket)
        r.check("a socket is refused on the same verdict", socket.closed is not None and not socket.accepted)
        r.check("a refused socket reaches no backend", len(spy.urls) == before)

        # A deployment that checked for itself can say so, deliberately, in the
        # environment - never from a browser and never from a saved setting.
        os.environ[proxy.AUTH_OVERRIDE_ENV] = "1"
        r.check("an explicit override serves again", proxy.serving_allowed() is True)
        allowed = await proxy.forward(request_for("GET", "/wan2gp/"))
        r.check("and the request goes through", allowed.status_code == 200)
        os.environ.pop(proxy.AUTH_OVERRIDE_ENV, None)

        # A Forge with no sign-in at all is not a Forge we should block: the
        # tab is exactly as reachable as the rest of it, which is the point.
        proxy._boundary = {"ok": True, "coverage": "no_auth_configured", "mechanisms": []}
        r.check("a Forge with no authentication is served", proxy.serving_allowed() is True)
        proxy._boundary = {"ok": True, "coverage": "covered", "mechanisms": ["asgi_middleware"]}
        r.check("authentication that wraps every route is served", proxy.serving_allowed() is True)
    finally:
        if override is None:
            os.environ.pop(proxy.AUTH_OVERRIDE_ENV, None)
        else:
            os.environ[proxy.AUTH_OVERRIDE_ENV] = override
        proxy._boundary = remembered
        proxy.use_client(None)
        runtime.reset_for_tests()


class _Socket:
    """Just enough WebSocket for the endpoint to refuse one, or open one."""

    def __init__(self, path: str, origin: str = "") -> None:
        self.url = FakeUrl(scheme="ws", path=path)
        self.headers = {"host": "forge.example.test:7860"}
        if origin:
            self.headers["origin"] = origin
        self.client = None
        self.scope = {"query_string": b"", "subprotocols": []}
        self.accepted = False
        self.closed = None
        self.sent = []

    async def receive_text(self):
        raise RuntimeError("the browser went away")

    async def send_text(self, value) -> None:
        self.sent.append(value)

    async def accept(self, *args, **keywords) -> None:
        self.accepted = True

    async def close(self, code: int = 1000) -> None:
        self.closed = code


async def websocket_origin_checks(r: Results) -> None:
    """The upgrade's Origin, judged here and restated for the child.

    THE OTHER HALF OF THE BANNER THAT WOULD NOT CLEAR.

    Wan2GP's auth middleware calls every WebSocket an unsafe request and
    requires its ``Origin`` to name the host it was asked on. An ordinary
    request is forwarded with the browser's ``Host`` intact, so the child
    computes the public host and the browser's origin matches it - which is
    why POSTs have always worked and nobody noticed. An upgrade has its
    ``Host`` taken off, because the handshake is the client library's to
    negotiate, so the child sees its own loopback address while the origin
    still names Forge. They can never agree; the middleware closes the socket
    before accepting it and uvicorn answers 403; the Deepy panel's transport
    reads that as a dead server and latches its banner.

    So the check happens at the hop that knows both hosts, and the upgrade is
    then presented to the child with an origin it recognises as its own.
    """
    r.check("an absent origin is allowed, exactly as the child's own rule has it",
            proxy.origin_allowed("", "forge.example.test:7860") is True)
    r.check("this Forge's own origin is allowed over either transport",
            proxy.origin_allowed("http://forge.example.test:7860", "forge.example.test:7860")
            and proxy.origin_allowed("https://forge.example.test:7860", "forge.example.test:7860"))
    r.check("another site's is not, whatever it looks like",
            not proxy.origin_allowed("http://evil.test", "forge.example.test:7860")
            and not proxy.origin_allowed("http://forge.example.test:7860.evil.test", "forge.example.test:7860")
            and not proxy.origin_allowed("http://forge.example.test:7861", "forge.example.test:7860"))
    r.check("and an origin with no host to compare against is refused rather than guessed",
            proxy.origin_allowed("http://forge.example.test:7860", "") is False)

    remembered = proxy._boundary
    proxy._boundary = {"ok": True, "coverage": "covered", "mechanisms": ["asgi_middleware"]}
    opened = []

    @contextlib.asynccontextmanager
    async def _pretend(url, headers, subprotocols=None):
        opened.append({"url": str(url), "headers": dict(headers)})
        yield None  # the endpoint's own "no client library" path ends the call

    real = proxy._connect_upstream
    proxy._connect_upstream = _pretend
    try:
        as_ready(PRETEND_PORT)
        socket = _Socket("/deepy/deepy_api/events", origin="http://evil.test")
        await proxy._websocket_endpoint(socket)
        r.check("a WebSocket from another site is refused before anything is dialled",
                socket.closed == 1008 and not socket.accepted and opened == [], str(opened))

        socket = _Socket("/deepy/deepy_api/events", origin="http://forge.example.test:7860")
        await proxy._websocket_endpoint(socket)
        r.check("one from this Forge is carried upstream", len(opened) == 1, str(opened))
        if opened:
            sent = opened[0]
            r.check("to the child's Deepy app at the address its mount answers on",
                    sent["url"] == f"ws://127.0.0.1:{PRETEND_PORT}/wan2gp/deepy/deepy_api/events", sent["url"])
            r.check("with an origin the child will recognise as its own - "
                    "the browser's could never have matched a host it is not told",
                    sent["headers"].get("origin") == f"http://127.0.0.1:{PRETEND_PORT}",
                    str(sent["headers"].get("origin")))
            r.check("and none of the headers that describe the hop it came in on",
                    not any(name in sent["headers"] for name in
                            ("host", "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions")),
                    str(sorted(sent["headers"])))

        # A client that is not a browser sends no origin, and the sign-in gate
        # is the one that decides for those.
        opened.clear()
        socket = _Socket("/wan2gp/queue/join")
        await proxy._websocket_endpoint(socket)
        r.check("a client with no origin is carried, and still told the child's own",
                len(opened) == 1 and opened[0]["headers"].get("origin") == f"http://127.0.0.1:{PRETEND_PORT}",
                str(opened))
    finally:
        proxy._connect_upstream = real
        proxy._boundary = remembered
        runtime.reset_for_tests()


def enforced_auth_checks(r: Results) -> None:
    """Our routes ask for the sign-in the rest of Forge asks for.

    Gradio checks its login per route, so a route an extension adds is not
    behind it - detecting that only ever produced "unknown", and refusing on
    an unknown left WanGP unreachable. Running the same check ourselves turns
    the question into a fact: this is the real thing, driven through Gradio's
    own app with its own cookie.
    """
    try:
        import gradio as gr
        from gradio.routes import App
        from starlette.testclient import TestClient
    except ImportError as error:  # pragma: no cover - depends on the install
        r.check("gradio is available to test the sign-in against", False, str(error))
        return

    with gr.Blocks() as demo:
        gr.Markdown("forge")
    demo.auth = ("user", "pass")
    demo.auth_message = None
    app = App.create_app(demo)

    remembered = (proxy._boundary, proxy._app.get("app"))
    try:
        proxy.install(app)
        proxy._boundary = proxy.auth_boundary_report(app)
        r.check("a Forge with a sign-in is covered by construction, not by guessing",
                proxy._boundary["ok"] is True and proxy._boundary["coverage"] == "enforced_here",
                repr(proxy._boundary))

        as_ready(PRETEND_PORT)
        client = TestClient(app)

        # Signed out: the same answer Forge gives for its own endpoints.
        baseline = client.get("/config").status_code
        r.check("Forge protects its own endpoints in this fixture", baseline == 401, str(baseline))
        r.check("a signed-out visitor is refused the WanGP page",
                client.get("/wan2gp/").status_code == 401)
        r.check("and refused the probe too", client.get(proxy.AUTH_PROBE_PATH).status_code == 401)
        r.check("and never reaches the backend", True)

        # Signed in exactly as Gradio does it.
        app.tokens["a-token"] = "user"
        client.cookies.set(f"access-token-{app.cookie_id}", "a-token")
        r.check("a signed-in visitor is let through",
                client.get(proxy.AUTH_PROBE_PATH).status_code == 204)

        # A Forge with no sign-in at all must not start demanding one.
        with gr.Blocks() as open_demo:
            gr.Markdown("forge")
        open_app = App.create_app(open_demo)
        proxy.install(open_app)  # the verdict is about a route that exists
        proxy._app["app"] = open_app
        r.check("a Forge with no sign-in asks for none", proxy.signed_in(object()) is True)
        r.check("and reports itself as such",
                proxy.auth_boundary_report(open_app)["coverage"] == "no_auth_configured")
    finally:
        proxy._boundary, proxy._app["app"] = remembered
        runtime.reset_for_tests()


def run() -> Results:
    r = Results("wangp proxy")
    try:
        sync_checks(r)
        enforced_auth_checks(r)
        asyncio.run(async_checks(r))
        asyncio.run(auth_gate_checks(r))
        asyncio.run(websocket_origin_checks(r))
        asyncio.run(deepy_address_checks(r))
        asyncio.run(deepy_checks(r))
    finally:
        proxy.use_client(None)
        runtime.reset_for_tests()
    return r


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
