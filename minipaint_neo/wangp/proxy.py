"""``/wan2gp/*`` on the Forge origin, and one loopback destination behind it.

WanGP needs an HTTP service; the security goal was never "no socket", it was
"no externally reachable WanGP socket". So the child binds 127.0.0.1 on a port
nobody wrote down, and the browser only ever talks to the Forge origin. This
module is the join between those two facts, and almost everything in it exists
to keep that join from becoming a hole.

The destination is not a parameter. It is read from ``runtime.current()`` and
composed here from a fixed scheme, a fixed host and that runtime's port; there
is deliberately no branch anywhere in this file where a query string, a header,
a cookie or a path segment can influence where a request goes. An open proxy is
one ``?url=`` away from any design that forgets that, so the rule is written
down rather than merely followed.

The rest of the module is about being a real reverse proxy instead of a GET
forwarder. Gradio uploads multi-gigabyte videos, streams its queue as
server-sent events, serves range requests for media, redirects, sets cookies
and may or may not use a WebSocket depending on the release that happens to be
installed. Nothing here reads a body into memory: the request body is streamed
out of the browser's connection and into the upstream one, and the response is
streamed back with the bytes untouched. Timeouts are three separate classes for
the same reason - a connection that never opens is a fault after ten seconds,
while a generation that takes forty minutes to produce its next queue event is
not a fault at all, and one timeout constant cannot mean both.

What the browser is told when something is wrong is deliberately thin: a code,
a sentence and a status. Not a traceback, and never the backend port.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import typing
import urllib.parse

import httpx

from .errors import (
    AUTH_BOUNDARY_FAILED,
    PROXY_NOT_READY,
    PROXY_ROOT_PATH_FAILED,
    PROXY_STREAM_FAILED,
    message,
)

#: The public mount point. It matches ``GRADIO_ROOT_PATH`` in the child's
#: environment exactly; the two are one decision written in two places.
PROXY_PATH = "/wan2gp"
PROXY_PREFIX = PROXY_PATH + "/"

#: Everything Gradio has been observed to use, plus the ones a future release
#: may. A method that is not here never reaches the backend.
METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")

_LOG_PREFIX = "MiniPaint WanGP:"

# ------------------------------------------------------------- timeouts ----
# Three classes, because one number cannot mean three things.

#: Opening the loopback connection. Short: 127.0.0.1 either answers at once or
#: the child is not listening, and waiting longer only delays the error page.
CONNECT_TIMEOUT = 10.0

#: Waiting for an ordinary response - a page, an asset, a control change. Long
#: enough for a model switch that blocks the event loop, short enough that a
#: wedged backend does not hold a browser connection forever.
RESPONSE_TIMEOUT = 120.0

#: Idle time inside a queue/event stream. None on purpose: a generation may
#: emit nothing for as long as it takes, and a proxy that cuts it turns a slow
#: success into a failure the user cannot distinguish from a crash.
STREAM_IDLE_TIMEOUT: typing.Optional[float] = None

#: Writing a request body upstream. Uploads are large and the browser may be on
#: a slow link; this is per write, not for the whole upload.
WRITE_TIMEOUT = 300.0

#: Waiting for a free connection in the pool.
POOL_TIMEOUT = 30.0

#: Path shapes that mean "this is a stream, do not apply the ordinary read
#: timeout". Gradio's SSE queue lives under /queue/, its heartbeat is a
#: long-lived GET, and other releases have used /stream and /sse.
STREAM_PATH_MARKERS = ("/queue/", "/stream", "/sse", "/heartbeat")

#: Hop-by-hop headers are properties of one connection and are meaningless on
#: the next one. Stripped in both directions; names listed in an incoming
#: ``Connection`` header are stripped as well.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

#: Where the backend could leak: an exception's text, a Location header, a log
#: line. Every string that can leave this module goes through ``redact``.
_BACKEND_RE = re.compile(r"(?:127\.0\.0\.1|localhost|\[::1\]):\d+")

#: First asset the probe can ask for, discovered from the served page.
_ASSET_RE = re.compile(
    r"""(?:src|href)\s*=\s*["']([^"'<>\s]+?\.(?:js|css|png|svg|ico|woff2?))(?:\?[^"']*)?["']""",
    re.IGNORECASE,
)

#: Module state: the shared client, the loop it belongs to, and the keys of
#: one-shot log lines. None of it is persisted, and none of it is a secret.
_state: typing.Dict[str, typing.Any] = {"client": None, "loop": None, "injected": None, "logged": set()}


def _log(text: str) -> None:
    print(f"{_LOG_PREFIX} {text}")


def _log_once(key: str, text: str) -> None:
    """Say it the first time. A proxy that repeats itself every frame is noise."""
    logged = _state["logged"]
    if key in logged:
        return
    logged.add(key)
    _log(text)


def redact(text: typing.Any) -> str:
    """A message with no backend port in it, whoever ends up reading it."""
    return _BACKEND_RE.sub("127.0.0.1:<backend>", str(text))


# --------------------------------------------------------------- upstream --


def upstream() -> typing.Optional[str]:
    """``http://127.0.0.1:<port>`` from the runtime, or None when it is not up.

    THE ONLY SOURCE OF A DESTINATION IN THIS MODULE. The scheme and host are
    literals here and the port comes from the runtime object; no caller, no
    request and no configuration file can substitute a different one. Any
    future change that lets a value from outside this function reach the
    returned string is the open-proxy bug section 12.5 forbids.
    """
    try:
        from . import runtime
    except Exception as error:  # pragma: no cover - runtime is optional at import time
        _log_once("no-runtime", f"the WanGP runtime module could not be imported ({error}); /wan2gp/ stays closed.")
        return None

    try:
        current = runtime.current()
        if current.state != getattr(runtime, "READY", "READY"):
            return None
        port = int(current.backend_port or 0)
    except Exception as error:
        _log_once("runtime-unreadable", f"the WanGP runtime state could not be read ({error}).")
        return None

    if port <= 0:
        return None
    return f"http://127.0.0.1:{port}"


def upstream_path(path: typing.Any) -> str:
    """The public path as the backend sees it: our prefix removed.

    The child runs with ``GRADIO_ROOT_PATH=/wan2gp`` and still serves from its
    own root, which is the ordinary reverse-proxy arrangement: the prefix is
    stripped on the way in and re-attached by Gradio when it builds URLs from
    the forwarded headers.
    """
    text = str(path or "")
    if text.startswith(PROXY_PREFIX):
        return text[len(PROXY_PATH) :] or "/"
    if text == PROXY_PATH:
        return "/"
    if not text.startswith("/"):
        text = "/" + text
    return text or "/"


def stream_shaped(path: typing.Any, accept: typing.Any = "") -> bool:
    """Does this request look like an event/queue stream before it is sent?

    Pure, and decided from the path shape because the timeout has to be chosen
    before the response exists. ``_relax_read_timeout`` refines the answer once
    the content type is known.
    """
    text = str(path or "").lower()
    if any(marker in text for marker in STREAM_PATH_MARKERS):
        return True
    return "text/event-stream" in str(accept or "").lower()


def timeout_for(path: typing.Any, accept: typing.Any = "") -> httpx.Timeout:
    read = STREAM_IDLE_TIMEOUT if stream_shaped(path, accept) else RESPONSE_TIMEOUT
    return httpx.Timeout(connect=CONNECT_TIMEOUT, read=read, write=WRITE_TIMEOUT, pool=POOL_TIMEOUT)


# ---------------------------------------------------------------- headers --


def _header_items(request: typing.Any) -> typing.List[typing.Tuple[str, str]]:
    """Incoming headers as lowercase name/value pairs, from anything request-ish."""
    headers = getattr(request, "headers", None)
    if headers is None:
        return []
    try:
        items = list(headers.items())
    except Exception:
        return []
    pairs = []
    for name, value in items:
        if isinstance(name, (bytes, bytearray)):
            name = bytes(name).decode("latin-1")
        if isinstance(value, (bytes, bytearray)):
            value = bytes(value).decode("latin-1")
        pairs.append((str(name).lower(), str(value)))
    return pairs


def hop_by_hop_names(pairs: typing.Iterable[typing.Tuple[str, str]]) -> typing.Set[str]:
    """The fixed list plus whatever this connection's ``Connection`` header names."""
    names = set(HOP_BY_HOP)
    for name, value in pairs:
        if name == "connection":
            names.update(token.strip().lower() for token in value.split(",") if token.strip())
    return names


def forwarded_headers(request: typing.Any) -> dict:
    """What the backend is told about the public request.

    Pure enough to test with a stand-in object: it reads ``headers``, ``url``
    and ``client`` and nothing else. Existing ``X-Forwarded-*`` values win over
    our own view of the connection, because a Forge behind an external reverse
    proxy already has the true public host and scheme in them and our view is
    only of the hop from that proxy. The forwarded-for chain is appended to
    rather than replaced, for the same reason.
    """
    pairs = _header_items(request)
    drop = hop_by_hop_names(pairs)

    headers: typing.Dict[str, str] = {}
    for name, value in pairs:
        if name in drop:
            continue
        if name in headers:
            # Duplicates are legal and rare; the comma form is what a receiver
            # would have reconstructed from them anyway.
            headers[name] = f"{headers[name]}, {value}"
            continue
        headers[name] = value

    url = getattr(request, "url", None)
    scheme = str(getattr(url, "scheme", "") or "http")
    # A WebSocket's scheme is ws/wss, but X-Forwarded-Proto describes the
    # public *transport* the page was loaded over, and that is what Gradio
    # builds its URLs from.
    scheme = {"ws": "http", "wss": "https"}.get(scheme, scheme)
    host = headers.get("host") or str(getattr(url, "netloc", "") or getattr(url, "hostname", "") or "")
    port = getattr(url, "port", None)
    if not port:
        port = 443 if scheme == "https" else 80

    # Host is forwarded verbatim so Gradio builds its URLs for the origin the
    # browser is actually on. httpx keeps a Host we set rather than deriving
    # one from the loopback URL it dials.
    if host:
        headers["host"] = host
        headers.setdefault("x-forwarded-host", host)
    headers.setdefault("x-forwarded-proto", scheme)
    headers.setdefault("x-forwarded-port", str(port))

    client = getattr(request, "client", None)
    peer = str(getattr(client, "host", "") or "")
    if peer:
        chain = headers.get("x-forwarded-for", "")
        headers["x-forwarded-for"] = f"{chain}, {peer}" if chain else peer

    return headers


def rewrite_location(value: str, target: typing.Optional[str] = None) -> str:
    """Keep a redirect on the Forge origin, under our prefix.

    A backend that answers ``Location: /queue/join`` would send the browser out
    of ``/wan2gp/`` and into Forge's own routes; one that answers with its own
    ``http://127.0.0.1:<port>/...`` would hand the port to the browser. Both
    become a path under the prefix. Anything else - an absolute URL to a real
    external host, which Gradio does emit for documentation links - is left
    exactly as it is.
    """
    text = str(value or "")
    if not text:
        return text

    if target and text.startswith(target):
        text = text[len(target) :] or "/"
    elif "://" in text:
        parsed = urllib.parse.urlsplit(text)
        if parsed.hostname in ("127.0.0.1", "localhost", "::1"):
            text = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, parsed.fragment))
        else:
            return text

    if not text.startswith("/"):
        return text
    if text == PROXY_PATH or text.startswith(PROXY_PREFIX):
        return text
    return PROXY_PATH + text


def rewrite_set_cookie(value: str) -> str:
    """Scope a backend cookie to ``/wan2gp/`` and to this origin.

    WanGP's cookies are WanGP's business. Left at ``Path=/`` they would ride
    along on every Forge request, and a ``Domain`` chosen for 127.0.0.1 is
    simply wrong on the public origin, so it is dropped and the browser
    defaults to the host it is talking to.
    """
    parts = [part.strip() for part in str(value or "").split(";") if part.strip()]
    if not parts:
        return str(value or "")

    kept = [parts[0]]
    saw_path = False
    for part in parts[1:]:
        name = part.split("=", 1)[0].strip().lower()
        if name == "domain":
            continue
        if name == "path":
            saw_path = True
            path = part.split("=", 1)[1].strip() if "=" in part else "/"
            if not (path == PROXY_PATH or path.startswith(PROXY_PREFIX)):
                path = PROXY_PATH + (path if path.startswith("/") else "/" + path)
            kept.append(f"Path={path}")
            continue
        kept.append(part)
    if not saw_path:
        kept.append(f"Path={PROXY_PREFIX}")
    return "; ".join(kept)


def response_headers(pairs: typing.Iterable[typing.Tuple[str, str]], target: typing.Optional[str] = None) -> typing.List[typing.Tuple[str, str]]:
    """The upstream's headers, cleaned for the connection they are going out on.

    Hop-by-hop headers go, and so does ``content-length``: the body leaves here
    as a stream and the server that frames it decides how to say how long it
    is. ``content-encoding`` stays, because the bytes are passed through raw
    and are still in whatever encoding the backend chose.
    """
    pairs = [(str(name).lower(), str(value)) for name, value in pairs]
    drop = hop_by_hop_names(pairs) | {"content-length"}

    cleaned: typing.List[typing.Tuple[str, str]] = []
    for name, value in pairs:
        if name in drop:
            continue
        if name == "location":
            value = rewrite_location(value, target)
        elif name == "set-cookie":
            value = rewrite_set_cookie(value)
        cleaned.append((name, value))
    return cleaned


# ----------------------------------------------------------------- client --


def use_client(client: typing.Any) -> None:
    """Test seam: hand the module a client, None restores the real one.

    Nothing in the extension calls this; it exists so the forwarding logic can
    be exercised against a transport that never opens a socket.
    """
    _state["injected"] = client


def _client() -> httpx.AsyncClient:
    injected = _state.get("injected")
    if injected is not None:
        return injected

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - forward() always has a loop
        loop = None

    client = _state.get("client")
    if client is None or client.is_closed or _state.get("loop") is not loop:
        # A client belongs to the loop its connections were opened on. A Forge
        # restart-in-place builds a new loop, and the old client's pool goes
        # with the old one - dropped rather than closed, because closing it
        # would need the loop that is already gone.
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=CONNECT_TIMEOUT, read=RESPONSE_TIMEOUT, write=WRITE_TIMEOUT, pool=POOL_TIMEOUT),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
        _state["client"] = client
        _state["loop"] = loop
    return client


async def _aclose_client() -> None:
    client = _state.get("client")
    _state["client"] = None
    _state["loop"] = None
    if client is not None:
        with contextlib.suppress(Exception):
            await client.aclose()


# ---------------------------------------------------------------- errors ---


def wants_json(request: typing.Any) -> bool:
    """Would this caller rather have JSON than a page?

    Gradio's own traffic is XHR and fetch; a browser navigating to the tab is
    not. Getting this wrong only changes which shape of the same three facts
    the caller receives.
    """
    accept = ""
    for name, value in _header_items(request):
        if name == "accept":
            accept = value.lower()
            break
    if "application/json" in accept:
        return True
    if "text/html" in accept:
        return False
    path = str(getattr(getattr(request, "url", None), "path", "") or "")
    return stream_shaped(path) or "/api" in path or "/queue" in path


_ERROR_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>WanGP</title>
<style>
 body {{ margin:0; display:flex; align-items:center; justify-content:center; min-height:100vh;
        font-family: system-ui, -apple-system, "Segoe UI", sans-serif; background:#1f2129; color:#e8e8ec; }}
 main {{ max-width:34rem; padding:2rem; text-align:center; }}
 h1 {{ font-size:1.1rem; font-weight:600; margin:0 0 .6rem; }}
 p {{ margin:0; color:#b6b8c2; line-height:1.5; }}
 code {{ color:#8a8d99; font-size:.85em; }}
 @media (prefers-color-scheme: light) {{
   body {{ background:#f2f2f5; color:#1f2129; }} p {{ color:#55586a; }}
 }}
</style></head>
<body><main><h1>{heading}</h1><p>{detail}</p><p><code>{code}</code></p></main></body></html>
"""


def error_payload(code: str, detail: str = "") -> dict:
    """The three facts an error may carry. Never a path, never a port."""
    return {"ok": False, "code": code, "message": message(code), "detail": redact(detail) if detail else ""}


def error_response(request: typing.Any, code: str, status: int, detail: str = "") -> typing.Any:
    """A controlled failure: a small page or a small object, and a status.

    Section 12.9: no stack trace reaches the browser. The detail that would
    have explained it is logged on this side instead.
    """
    from starlette.responses import HTMLResponse, JSONResponse

    if detail:
        _log_once(f"error-{code}", f"/wan2gp/ returned {code}: {redact(detail)}")

    if wants_json(request):
        return JSONResponse(error_payload(code, detail), status_code=status)

    page = _ERROR_PAGE.format(
        heading="WanGP is not available",
        detail=message(code),
        code=code,
    )
    return HTMLResponse(page, status_code=status)


# --------------------------------------------------------------- forward ---


def _raw_target(request: typing.Any) -> bytes:
    """The upstream path, still percent-encoded exactly as it arrived.

    ASGI hands us a decoded ``path`` and, from every server that can, the
    original bytes in ``raw_path``. Re-encoding a decoded path guesses; using
    the raw one does not, which matters for the file names Gradio puts in URLs.
    """
    scope = getattr(request, "scope", None) or {}
    raw = scope.get("raw_path") if isinstance(scope, dict) else None
    if isinstance(raw, (bytes, bytearray)):
        text = bytes(raw).split(b"?", 1)[0].decode("latin-1", "ignore")
        if text:
            return upstream_path(text).encode("latin-1")
    path = str(getattr(getattr(request, "url", None), "path", "") or "/")
    return urllib.parse.quote(upstream_path(path), safe="/").encode("ascii")


def _raw_query(request: typing.Any) -> bytes:
    scope = getattr(request, "scope", None) or {}
    query = scope.get("query_string") if isinstance(scope, dict) else None
    if isinstance(query, (bytes, bytearray)):
        return bytes(query)
    return str(getattr(getattr(request, "url", None), "query", "") or "").encode("latin-1")


def should_send_body(method: str, headers: typing.Mapping[str, str]) -> bool:
    """Only stream a body when the request actually has one.

    Attaching an empty async iterator to a GET would make httpx frame it as a
    chunked request, which some servers answer with 400 for no reason anyone
    enjoys debugging.
    """
    if method.upper() in ("GET", "HEAD", "OPTIONS", "DELETE"):
        return bool(headers.get("content-length") or headers.get("transfer-encoding"))
    return True


def _relax_read_timeout(request_object: typing.Any) -> None:
    """Best effort: let a stream that only announced itself in its headers run.

    The timeout class is chosen from the path before the request is sent, which
    is the mechanism that has to be right. This is the refinement for the case
    where a release streams from a path shape we did not predict: httpx reads
    the read timeout out of the request's extensions each time it pulls from
    the body, so relaxing it after the headers arrive usually takes effect. It
    is written as "usually" on purpose - nothing depends on it.
    """
    with contextlib.suppress(Exception):
        extensions = getattr(request_object, "extensions", None)
        if isinstance(extensions, dict) and isinstance(extensions.get("timeout"), dict):
            extensions["timeout"]["read"] = STREAM_IDLE_TIMEOUT


async def _stream_response(response: httpx.Response) -> typing.AsyncIterator[bytes]:
    """The body, raw and unbuffered. Nothing here holds more than a chunk."""
    if getattr(response, "is_stream_consumed", False):
        # A transport that handed back a body it had already materialised -
        # a test double, in practice. Yielding it is the same bytes; raising
        # on the second read would only make the module untestable offline.
        yield response.content
        return

    try:
        async for chunk in response.aiter_raw():
            yield chunk
    except httpx.HTTPError as error:
        # The status line left long ago; there is no error page to send now.
        # Ending the body is the only honest move, and the log carries the why.
        _log(f"{PROXY_STREAM_FAILED}: the WanGP response ended early ({redact(error)}).")


async def forward(request: typing.Any) -> typing.Any:
    """One browser request, streamed to the backend and streamed back."""
    from starlette.background import BackgroundTask
    from starlette.responses import StreamingResponse

    if not serving_allowed():
        return error_response(request, AUTH_BOUNDARY_FAILED, 503)

    target = upstream()
    if not target:
        return error_response(request, PROXY_NOT_READY, 503)

    # The destination, assembled from the runtime's port and the path this
    # route matched. Note what is absent: no header, query parameter or body
    # value is consulted here, and none may be added later.
    raw_query = _raw_query(request)
    raw_path = _raw_target(request)
    url = httpx.URL(target).copy_with(raw_path=raw_path + (b"?" + raw_query if raw_query else b""))

    method = str(getattr(request, "method", "GET") or "GET").upper()
    headers = forwarded_headers(request)
    content = request.stream() if should_send_body(method, headers) else None
    timeout = timeout_for(raw_path.decode("latin-1", "ignore"), headers.get("accept", ""))

    client = _client()
    upstream_request = client.build_request(method, url, headers=headers, content=content, timeout=timeout)

    try:
        response = await client.send(upstream_request, stream=True, follow_redirects=False)
    except httpx.TimeoutException as error:
        return error_response(request, PROXY_NOT_READY, 504, f"upstream timeout: {error}")
    except httpx.HTTPError as error:
        return error_response(request, PROXY_NOT_READY, 502, f"upstream unreachable: {error}")

    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type.lower():
        _relax_read_timeout(upstream_request)

    proxied = StreamingResponse(
        _stream_response(response),
        status_code=response.status_code,
        background=BackgroundTask(response.aclose),
    )
    # Set after construction so duplicate headers survive - Set-Cookie arrives
    # more than once and a dict would keep only the last one.
    proxied.raw_headers = [
        (name.encode("latin-1"), value.encode("latin-1"))
        for name, value in response_headers(response.headers.multi_items(), target)
    ]
    return proxied


async def _redirect_to_prefix(request: typing.Any) -> typing.Any:
    """``/wan2gp`` -> ``/wan2gp/``, saying nothing about what is behind it."""
    from starlette.responses import RedirectResponse

    query = _raw_query(request).decode("latin-1", "ignore")
    destination = PROXY_PREFIX + (f"?{query}" if query else "")
    # Temporary and method-preserving: the prefix is a constant, but a cached
    # permanent redirect is a thing users cannot clear when it is ever wrong.
    return RedirectResponse(destination, status_code=307)


# ------------------------------------------------------------- websocket ---


class _UpstreamSocket:
    """One shape for the two libraries that might be installed, or neither."""

    def __init__(self, session: typing.Any, kind: str, subprotocol: typing.Optional[str]) -> None:
        self._session = session
        self._kind = kind
        self.subprotocol = subprotocol

    async def send(self, data: typing.Union[str, bytes]) -> None:
        if self._kind == "websockets":
            await self._session.send(data)
            return
        if isinstance(data, str):
            await self._session.send_text(data)
        else:
            await self._session.send_bytes(data)

    async def receive(self) -> typing.Optional[typing.Union[str, bytes]]:
        """The next frame, or None once the upstream is done."""
        try:
            if self._kind == "websockets":
                return await self._session.recv()
            event = await self._session.receive()
            return getattr(event, "data", None)
        except Exception:
            return None


@contextlib.asynccontextmanager
async def _connect_upstream(url: str, headers: typing.Mapping[str, str], subprotocols: typing.Optional[typing.List[str]] = None):
    """Open the backend side of a WebSocket with whatever is importable.

    Neither library is guaranteed: ``websockets`` usually rides along with
    Gradio and ``httpx-ws`` usually does not. When both are absent the caller
    closes the browser side cleanly - current Gradio may not use a WebSocket at
    all, and a missing optional transport is not a reason to break the tab.
    """
    connect = None
    kind = ""
    try:
        from websockets.asyncio.client import connect as ws_connect  # type: ignore

        connect, kind = ws_connect, "websockets"
    except Exception:
        try:  # pragma: no cover - older websockets releases
            from websockets.legacy.client import connect as ws_connect  # type: ignore

            connect, kind = ws_connect, "websockets-legacy"
        except Exception:
            connect = None

    if connect is not None:
        keyword = "additional_headers" if kind == "websockets" else "extra_headers"
        options: typing.Dict[str, typing.Any] = {
            keyword: list(headers.items()),
            "subprotocols": subprotocols or None,
            "max_size": None,
            "open_timeout": CONNECT_TIMEOUT,
            "ping_interval": None,
        }
        try:
            session = connect(url, **options)
        except TypeError:  # a release that spells one of those differently
            session = connect(url)
        async with session as socket:
            yield _UpstreamSocket(socket, "websockets", getattr(socket, "subprotocol", None))
        return

    try:
        from httpx_ws import aconnect_ws  # type: ignore
    except Exception:
        _log_once(
            "no-websocket",
            "no WebSocket client library is installed, so /wan2gp/ carries HTTP only. "
            "This matters only if the installed Gradio uses a WebSocket transport.",
        )
        yield None
        return

    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=CONNECT_TIMEOUT, read=STREAM_IDLE_TIMEOUT, write=WRITE_TIMEOUT, pool=POOL_TIMEOUT)) as client:
        async with aconnect_ws(url, client, headers=dict(headers)) as session:
            yield _UpstreamSocket(session, "httpx_ws", getattr(session, "subprotocol", None))


async def _pump_to_upstream(websocket: typing.Any, socket: _UpstreamSocket) -> None:
    while True:
        packet = await websocket.receive()
        kind = packet.get("type", "")
        if kind == "websocket.disconnect":
            return
        if packet.get("text") is not None:
            await socket.send(packet["text"])
        elif packet.get("bytes") is not None:
            await socket.send(packet["bytes"])


async def _pump_to_browser(websocket: typing.Any, socket: _UpstreamSocket) -> None:
    while True:
        frame = await socket.receive()
        if frame is None:
            return
        if isinstance(frame, str):
            await websocket.send_text(frame)
        else:
            await websocket.send_bytes(bytes(frame))


async def _websocket_endpoint(websocket: typing.Any) -> None:
    """Bridge frames both ways for as long as either side keeps talking."""
    # The same gate the HTTP side has. A socket is the one route a browser can
    # open without ever fetching the page, so refusing only in ``forward``
    # would leave the door it was meant to close.
    if not serving_allowed():
        await websocket.close(code=1011)
        return

    target = upstream()
    if not target:
        await websocket.close(code=1011)
        return

    path = upstream_path(str(getattr(getattr(websocket, "url", None), "path", "") or "/"))
    query = _raw_query(websocket).decode("latin-1", "ignore")
    url = "ws" + target[len("http") :] + path + (f"?{query}" if query else "")

    headers = forwarded_headers(websocket)
    # The upgrade itself is negotiated by the client library, so the headers
    # that describe this connection must not be replayed onto the next one.
    requested = [value.strip() for value in headers.pop("sec-websocket-protocol", "").split(",") if value.strip()]
    for name in ("sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions", "host"):
        headers.pop(name, None)

    try:
        async with _connect_upstream(url, headers, requested) as socket:
            if socket is None:
                await websocket.close(code=1011)
                return
            await websocket.accept(subprotocol=socket.subprotocol)
            tasks = [
                asyncio.ensure_future(_pump_to_upstream(websocket, socket)),
                asyncio.ensure_future(_pump_to_browser(websocket, socket)),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                with contextlib.suppress(Exception):
                    task.result()
    except Exception as error:
        _log_once("ws-failed", f"a /wan2gp/ WebSocket could not be bridged ({redact(error)}).")
    finally:
        with contextlib.suppress(Exception):
            await websocket.close()


# ----------------------------------------------------------------- probe ---

#: The readiness gate of 12.7, in the order it is attempted. A 200 on the base
#: page is not readiness: an unmounted Gradio, a captive portal or a stale
#: process would all pass that alone.
PROBE_STEPS = ("upstream", "base_page", "asset", "root_path")


def find_asset_url(html: typing.Any) -> typing.Optional[str]:
    """The first same-origin asset the served page refers to."""
    for match in _ASSET_RE.finditer(str(html or "")):
        candidate = match.group(1)
        if "://" in candidate or candidate.startswith("//"):
            continue
        return candidate
    return None


def _looks_like_our_root(value: typing.Any) -> bool:
    text = str(value or "")
    if not text:
        return False
    if "://" in text:
        text = urllib.parse.urlsplit(text).path
    text = text.rstrip("/")
    return text.endswith(PROXY_PATH)


def config_root_ok(payload: typing.Any) -> bool:
    """Does the served Gradio config say it lives under ``/wan2gp``?

    This is the check that proves ``GRADIO_ROOT_PATH`` actually took effect in
    the child. Without it the page loads and every asset and event URL it then
    builds points at Forge's own root, which fails later and further away.
    """
    if isinstance(payload, dict):
        return any(_looks_like_our_root(payload.get(key)) for key in ("root", "root_url", "root_path"))
    text = str(payload or "")
    pattern = r'"root(?:_url|_path)?"\s*:\s*"[^"]*' + re.escape(PROXY_PATH) + r'(?:/?")'
    return bool(re.search(pattern, text))


async def probe(client: typing.Any = None) -> dict:
    """Is ``/wan2gp/`` actually usable? Step by step, so the tab can say which.

    The requests go to the backend rather than back through Forge, because a
    self-request would need Forge's own port and this integration deliberately
    does not learn it. That leaves one thing unproven here - that the route is
    reachable from the browser - and it is proven where it can be, by the tab
    loading the iframe. The bridge round trip 12.7 also asks for belongs to the
    bridge; this is the transport half.
    """
    steps: typing.Dict[str, dict] = {}

    def record(name: str, ok: bool, detail: str = "") -> None:
        steps[name] = {"ok": bool(ok), "detail": redact(detail)}

    def finish(code: str = "") -> dict:
        for name in PROBE_STEPS:
            steps.setdefault(name, {"ok": False, "detail": "not attempted"})
        ok = all(steps[name]["ok"] for name in PROBE_STEPS)
        return {"ok": ok, "code": "" if ok else (code or PROXY_NOT_READY), "steps": steps, "order": list(PROBE_STEPS)}

    target = upstream()
    if not target:
        record("upstream", False, "the WanGP runtime is not ready")
        return finish(PROXY_NOT_READY)
    record("upstream", True, "loopback runtime is ready")

    owned = client is None
    if owned:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=CONNECT_TIMEOUT, read=RESPONSE_TIMEOUT, write=WRITE_TIMEOUT, pool=POOL_TIMEOUT),
            follow_redirects=True,
        )

    try:
        try:
            response = await client.get(target + "/", headers={"accept": "text/html"})
            html = response.text
            record("base_page", response.status_code == 200, f"HTTP {response.status_code}")
        except Exception as error:
            record("base_page", False, f"the base page could not be fetched ({error})")
            return finish(PROXY_NOT_READY)

        if not steps["base_page"]["ok"]:
            return finish(PROXY_NOT_READY)

        asset = find_asset_url(html)
        # The page's own asset URLs already carry the public prefix when the
        # root path is in effect, so they are mapped back the same way a real
        # request is - which is itself a small proof that the mapping matches.
        asset_path = upstream_path(urllib.parse.urlsplit(asset).path) if asset else "/theme.css"
        try:
            response = await client.get(target + asset_path)
            record("asset", response.status_code == 200 and bool(response.content), f"HTTP {response.status_code}")
        except Exception as error:
            record("asset", False, f"an asset could not be fetched ({error})")

        if config_root_ok(html):
            record("root_path", True, f"the served page reports {PROXY_PATH}")
        else:
            try:
                response = await client.get(target + "/config", headers={"accept": "application/json"})
                payload = response.json() if response.status_code == 200 else {}
            except Exception as error:
                payload = {}
                record("root_path", False, f"the Gradio config could not be read ({error})")
            if "root_path" not in steps:
                ok = config_root_ok(payload)
                record("root_path", ok, "" if ok else f"the Gradio config does not report {PROXY_PATH}")
    finally:
        if owned:
            with contextlib.suppress(Exception):
                await client.aclose()

    return finish(PROXY_ROOT_PATH_FAILED if not steps.get("root_path", {}).get("ok") else PROXY_NOT_READY)


# ------------------------------------------------------- the auth boundary --

#: Middleware whose name suggests it decides who may talk to this app. A guess
#: by name, which is why a match is reported as evidence and a miss is reported
#: as "unknown" rather than "not protected".
AUTH_HINTS = ("auth", "login", "credential", "basic", "signin", "session")


def _middleware_names(app: typing.Any) -> typing.List[str]:
    names: typing.List[str] = []
    for entry in getattr(app, "user_middleware", None) or []:
        cls = getattr(entry, "cls", None)
        name = getattr(cls, "__name__", None) or str(cls)
        options = getattr(entry, "kwargs", None) or {}
        dispatch = options.get("dispatch") if isinstance(options, dict) else None
        if dispatch is not None:
            name = f"{name}({getattr(dispatch, '__name__', 'dispatch')})"
        names.append(name)
    return names


def _host_auth_configured() -> typing.Optional[bool]:
    """Did this Forge start with authentication asked for? None if unknowable."""
    try:
        from modules import shared

        options = getattr(shared, "cmd_opts", None)
        if options is None:
            return None
        return bool(getattr(options, "gradio_auth", None) or getattr(options, "gradio_auth_path", None) or getattr(options, "api_auth", None))
    except Exception:
        return None


def auth_boundary_report(app: typing.Any) -> dict:
    """What can actually be seen about who is allowed to reach ``/wan2gp/``.

    Section 12.6 refuses to let route registration stand in for protection, and
    49.4 makes proving it a release blocker, so this reports evidence rather
    than a verdict it cannot support. ASGI middleware is the one mechanism that
    can be trusted from here: it wraps the whole application, so a route added
    to the router is inside it by construction. Authentication enforced per
    route - a FastAPI dependency, Gradio's own login check - cannot be seen to
    cover a route it was never attached to, and that case is reported as
    ``unknown``, not as covered and not as broken. ``ok`` false means "not
    proven"; the caller fails closed with ``failure_code``.
    """
    routes = getattr(getattr(app, "router", None), "routes", None) or []
    registered = any(str(getattr(route, "path", "")).startswith(PROXY_PATH) for route in routes)

    middleware = _middleware_names(app)
    auth_middleware = [name for name in middleware if any(hint in name.lower() for hint in AUTH_HINTS)]

    mechanisms: typing.List[str] = []
    if auth_middleware:
        mechanisms.append("asgi_middleware")
    if getattr(app, "auth", None) or getattr(getattr(app, "blocks", None), "auth", None):
        mechanisms.append("gradio_auth")
    if getattr(getattr(app, "router", None), "dependencies", None):
        mechanisms.append("router_dependencies")
    host_auth = _host_auth_configured()
    if host_auth:
        mechanisms.append("command_line_auth")

    report = {
        "route_registered": registered,
        "middleware": middleware,
        "auth_middleware": auth_middleware,
        "mechanisms": mechanisms,
        "host_auth_configured": host_auth,
        "failure_code": AUTH_BOUNDARY_FAILED,
    }

    if not registered:
        report.update(ok=False, coverage="unknown", detail="the /wan2gp route is not registered on this app, so nothing can cover it.")
    elif auth_middleware:
        report.update(
            ok=True,
            coverage="covered",
            detail="authentication runs as ASGI middleware, which wraps every route including /wan2gp/.",
        )
    elif not mechanisms:
        report.update(
            ok=True,
            coverage="no_auth_configured",
            detail="this Forge has no authentication of its own; /wan2gp/ is exactly as reachable as the rest of it.",
        )
    else:
        report.update(
            ok=False,
            coverage="unknown",
            detail=(
                "authentication appears to be enforced per route or outside this application "
                f"({', '.join(mechanisms)}), which cannot be seen to cover a dynamically added route. "
                "It has to be checked with an unauthenticated request to /wan2gp/."
            ),
        )
    return report


# --------------------------------------------------------------- install ---

_INSTALLED_FLAG = "_minipaint_wangp_proxy_installed"

#: The verdict ``install`` reached about who can reach ``/wan2gp/``. None until
#: the routes exist, which is itself a refusal: nothing is served before the
#: question has been asked.
_boundary: typing.Optional[dict] = None

#: The one way past an unproven boundary. Section 12.6 makes an unauthenticated
#: ``/wan2gp/`` a release blocker and the check here cannot see per-route
#: authentication, so a Forge whose sign-in genuinely does cover the route
#: still reports "unknown". Rather than leave those installs with a feature
#: that can never run, the answer they get from the PHASE0 curl check can be
#: recorded here - deliberately as an environment variable and not a setting,
#: so it is a decision someone made about a deployment rather than a checkbox
#: a browser can tick.
AUTH_OVERRIDE_ENV = "MINIPAINT_WANGP_ALLOW_UNPROVEN_AUTH"
_TRUE = {"1", "true", "yes", "on"}


def auth_override() -> bool:
    return os.environ.get(AUTH_OVERRIDE_ENV, "").strip().lower() in _TRUE


def boundary_report() -> typing.Optional[dict]:
    """What ``install`` concluded, for the tab and the diagnostics report."""
    return dict(_boundary) if _boundary is not None else None


def serving_allowed() -> bool:
    """Whether this proxy may answer at all.

    The gate rather than the log entry. ``auth_boundary_report`` says ``ok``
    false for "not proven", and a proxy that forwards anyway would hand an
    unauthenticated caller the whole of WanGP - which has no sign-in of its
    own, because it was only ever meant to be reachable on loopback. So an
    unproven boundary refuses, here, at the point of service: that covers a
    child started by the wizard's own checks and a config initialised before
    authentication was switched on, neither of which any startup-time decision
    would have caught.
    """
    if _boundary is None:
        return False
    return bool(_boundary.get("ok")) or auth_override()


def install(app: typing.Any) -> None:
    """Put the routes on Forge's FastAPI app. Called from ``on_app_started``.

    Everything is contained: if Starlette moves under us, or the app is not the
    shape we expect, WanGP loses its route and Mini Paint keeps working. The
    routes go at the front of the table so a host catch-all cannot shadow them,
    and they are exact paths, so nothing else is shadowed by them.
    """
    if getattr(app, _INSTALLED_FLAG, False):
        return

    try:
        from starlette.routing import Route, WebSocketRoute

        routes = [
            Route(PROXY_PATH, endpoint=_redirect_to_prefix, methods=["GET", "HEAD"]),
            Route(PROXY_PREFIX + "{path:path}", endpoint=forward, methods=list(METHODS)),
            WebSocketRoute(PROXY_PREFIX + "{path:path}", endpoint=_websocket_endpoint),
        ]
        app.router.routes[0:0] = routes
        setattr(app, _INSTALLED_FLAG, True)
    except Exception as error:
        _log(f"the /wan2gp/ route could not be registered ({error}); the WanGP tab will stay unavailable.")
        return

    with contextlib.suppress(Exception):
        app.add_event_handler("shutdown", _aclose_client)

    global _boundary
    _boundary = auth_boundary_report(app)
    _log(f"reverse proxy ready at {PROXY_PREFIX} (one loopback upstream, chosen by the runtime only).")
    if not _boundary["ok"]:
        _log(f"authentication coverage for {PROXY_PREFIX} is {_boundary['coverage']}: {_boundary['detail']}")
        if auth_override():
            _log(f"{AUTH_OVERRIDE_ENV} is set, so {PROXY_PREFIX} will be served anyway.")
        else:
            _log(
                f"{PROXY_PREFIX} will answer {AUTH_BOUNDARY_FAILED} until that is proven. Check it with an "
                f"unauthenticated request (docs/wangp/PHASE0.md); if it is protected, set {AUTH_OVERRIDE_ENV}=1."
            )
