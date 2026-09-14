"""The control surface: how Forge reaches this plugin with no browser.

Until this file existed the bridge had exactly one way in - a Gradio event a
browser triggers - and said so in its own first paragraph: "after that it
does nothing at all until a browser asks it something." That is a correct
design for an image send, which is a thing a person does while looking at a
page. It is the wrong design for a job somebody walked away from, because
there is nobody left to ask.

So this is the first thing in the plugin that listens, and the first thread
it has ever had. Four decisions shape it, and each of them is the
conservative one:

*   **Its own loopback port, not WanGP's app.** Wan2GP's plugin API offers no
    hook at which the FastAPI application exists, and reaching for one means
    depending on Gradio internals and on the moment ``launch`` happens to
    run. Forge instead keeps a free loopback port before it starts the child
    and exports it, exactly as it already does for the Gradio port: the child
    never picks a number, never announces one, and there is no discovery step
    to get wrong or stale file for a dead port to sit in.

*   **The secret is compared on every request, before the body is read.**
    Forge has minted a per-launch credential for this child since the
    integration was built and has never used it; its own docstring calls it
    "a server-only value, for a control path no browser participates in".
    This is that path. Loopback alone was a defensible boundary while
    everything behind it either required a Gradio session hash or was a
    readiness GET - the moment a route on that socket can start a generation
    with no session, it stops being one.

*   **Nothing here blocks on a generation.** ``submit`` records the ledger
    entry, leaves the task in WanGP's own queue, asks the service to look,
    and returns. The submission handler never waits on WanGP's generation
    lock, never calls a result() and never touches a session close - all
    three of which take a lock held for the whole of a generation.

*   **It refuses to open at all when it cannot be authenticated.** No
    secret, no port, no ledger root: no listener. A control plane that
    listened without a check because the credential was missing would be
    worse than the absence it is standing in for.

Nothing here may raise into WanGP. A control surface that breaks is a Forge
that cannot run unattended jobs and says so; it is not an exception in
somebody else's process.
"""

from __future__ import annotations

import hmac
import json
import threading
import typing

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from . import compatibility, compose, execution, ledger, protocol
except ImportError:  # pragma: no cover - depends on how WanGP imports plugins
    import compatibility  # type: ignore[no-redef]
    import compose  # type: ignore[no-redef]
    import execution  # type: ignore[no-redef]
    import ledger  # type: ignore[no-redef]
    import protocol  # type: ignore[no-redef]


#: A control request this size or larger is refused unread. A composed
#: settings dict is the big one and is a few kilobytes of JSON; a megabyte is
#: three orders of magnitude of headroom and still finite.
MAX_BODY_BYTES = 1024 * 1024

#: How long a handler may spend reading a request body.
READ_TIMEOUT_SECONDS = 30.0

#: How many execution ids one status request may name.
MAX_STATUS_IDS = 256


class ControlSurface:
    """The listener, and the four things it can be asked to do.

    Constructed once per child from the plugin, started when everything it
    needs is present, and silent otherwise.
    """

    def __init__(
        self,
        compat: "compatibility.Compatibility",
        environ: typing.Optional[typing.Mapping[str, str]] = None,
        note: typing.Optional[typing.Callable[[str], None]] = None,
    ) -> None:
        self.compat = compat
        self.environ = environ
        self._note = note or (lambda _text: None)
        self.secret = compatibility.bridge_secret(environ)
        self.port = compatibility.control_port(environ)
        self.instance = compatibility.instance_id(environ)
        self.ledger: typing.Optional[ledger.Ledger] = None
        self.executor: typing.Optional[execution.Executor] = None
        self.composer: typing.Optional[compose.Composer] = None
        self._server: typing.Optional[ThreadingHTTPServer] = None
        self._thread: typing.Optional[threading.Thread] = None
        self._lock = threading.RLock()

    # -- lifecycle -----------------------------------------------------------

    def usable(self) -> typing.Tuple[bool, str]:
        """Whether this run was launched with everything the surface needs."""
        if not compatibility.managed(self.environ):
            return False, "WanGP was started by hand"
        if not self.secret:
            return False, "no control credential was exported for this run"
        if self.port <= 0:
            return False, "no control port was kept for this run"
        if not compatibility.ledger_root(self.environ):
            return False, "no ledger root was exported for this run"
        return True, ""

    def start(self) -> bool:
        """Open the surface. Returns whether it is listening.

        Safe to call more than once; a surface already listening stays as it
        is. Every failure is a note and a False, never an exception.
        """
        with self._lock:
            if self._server is not None:
                return True
            ok, why = self.usable()
            if not ok:
                self._note(f"control surface off: {why}")
                return False
            book = ledger.for_child(self.environ)
            if book is None:
                self._note("control surface off: the execution ledger could not be opened")
                return False
            self.ledger = book
            self.executor = execution.Executor(self.compat, book, environ=self.environ, note=self._note)
            self.composer = compose.Composer(self.compat, note=self._note)
            try:
                server = _build_server(self.port, self)
            except Exception as error:
                self._note(f"control surface off: {type(error).__name__}: {error}")
                return False
            self._server = server
            thread = threading.Thread(target=server.serve_forever, name="minipaint-bridge-control", daemon=True)
            self._thread = thread
        thread.start()
        open_records = book.open_count()
        self._note(
            f"control surface listening on loopback ({len(book.records())} ledger record(s)"
            + (f", {open_records} still open" if open_records else "")
            + ")"
        )
        return True

    def stop(self) -> None:
        """Close the surface. The child is going away; nothing is aborted."""
        with self._lock:
            server, self._server = self._server, None
            executor, self.executor = self.executor, None
        if executor is not None:
            executor.stop()
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass

    @property
    def listening(self) -> bool:
        with self._lock:
            return self._server is not None

    # -- authentication ------------------------------------------------------

    def authorised(self, offered: typing.Any) -> bool:
        """A constant-time comparison, and no other way in.

        ``compare_digest`` rather than ``==`` because the comparison is
        against a value an attacker supplies and the timing of a byte-by-byte
        mismatch is information. It is a small thing that costs nothing and
        the alternative has no upside.
        """
        if not self.secret or not isinstance(offered, str) or not offered:
            return False
        return hmac.compare_digest(offered, self.secret)

    # -- the operations ------------------------------------------------------

    def dispatch(self, operation: str, payload: typing.Any) -> typing.Tuple[int, dict]:
        """One control request, as (status, body). Never raises."""
        try:
            if operation == protocol.CONTROL_HELLO:
                return 200, self.hello()
            if operation == protocol.CONTROL_COMPOSE:
                return self._compose(payload)
            if operation == protocol.CONTROL_SUBMIT:
                return self._submit(payload)
            if operation == protocol.CONTROL_STATUS:
                return self._status(payload)
            if operation == protocol.CONTROL_CANCEL:
                return self._cancel(payload)
            if operation == protocol.CONTROL_FORGET:
                return self._forget(payload)
        except execution.ExecutionError as error:
            return 409, {"ok": False, "code": error.code, "message": error.detail[:200]}
        except compose.ComposeError as error:
            return 409, {"ok": False, "code": error.code, "message": error.detail[:200]}
        except Exception as error:  # pragma: no cover - a handler that throws is a refusal
            self._note(f"control {operation}: failed ({type(error).__name__})")
            return 500, {"ok": False, "code": compatibility.INTERNAL_ERROR, "message": type(error).__name__}
        return 404, {"ok": False, "code": compatibility.REQUEST_INVALID, "message": "no such operation"}

    def hello(self) -> dict:
        """What this child is and whether it can take a job right now.

        Answers the question a running process cannot: READY means the
        process is alive and answering HTTP, and says nothing about whether
        the bridge is on the page, the arbiter resolved, or a settings base
        can be read. The executor answers those itself so that nothing
        upstream mistakes liveness for admission.
        """
        executor = self.executor
        service = self.compat.service()
        can_execute, code = executor.available() if executor is not None else (False, protocol.CONTROL_UNAVAILABLE)
        gen = self.compat.shared_gen(service)
        depth = None
        if isinstance(gen, dict) and isinstance(gen.get(compatibility.GEN_QUEUE_KEY), (list, tuple)):
            depth = len(gen[compatibility.GEN_QUEUE_KEY])
        return {
            "ok": True,
            "control_version": protocol.CONTROL_VERSION,
            "protocol": protocol.PROTOCOL,
            "bridge_version": compatibility.BRIDGE_VERSION,
            "wan2gp_version": self.compat.wan2gp_version,
            "wan2gp_revision_pinned": compatibility.WAN2GP_EXECUTION_REVISION,
            "instance": self.instance,
            "can_execute": bool(can_execute),
            # What was looked at, when there is nothing to execute with. The
            # Forge side puts it in the job's stage line and the journal, so
            # the reason reaches whoever is reading a log rather than staying
            # inside this process as a three-word code.
            "diagnosis": "" if can_execute else self.compat.service_diagnosis(),
            "can_compose": bool(self.composer is not None and self.composer.available()),
            "service": service is not None,
            "generation_running": self.compat.service_generation_running(service),
            "queue_depth": depth,
            "model_type": self.compat.current_model_type(service),
            "ledger_open": self.ledger.open_count() if self.ledger is not None else 0,
            "code": code,
            "message": "",
        }

    def _compose(self, payload: typing.Any) -> typing.Tuple[int, dict]:
        request, code = protocol.normalize_compose_request(payload)
        if code:
            return 400, {"ok": False, "code": code, "message": "the compose request did not normalise"}
        if self.composer is None:
            return 409, {"ok": False, "code": protocol.COMPOSE_UNAVAILABLE, "message": "the control surface is not started"}
        return 200, self.composer.compose(request["model_type"], request["session_hash"])

    def _submit(self, payload: typing.Any) -> typing.Tuple[int, dict]:
        request, code = protocol.normalize_execution_request(payload)
        if code:
            return 400, {"ok": False, "code": code, "message": "the execution request did not normalise"}
        if self.executor is None:
            return 409, {"ok": False, "code": protocol.CONTROL_UNAVAILABLE, "message": "the control surface is not started"}
        record = self.executor.submit(request)
        return 200, {"ok": True, "record": record}

    def _status(self, payload: typing.Any) -> typing.Tuple[int, dict]:
        raw = payload if isinstance(payload, dict) else {}
        ids = raw.get("execution_ids")
        if not isinstance(ids, (list, tuple)) or len(ids) > MAX_STATUS_IDS:
            return 400, {"ok": False, "code": compatibility.REQUEST_INVALID, "message": "a status names a list of execution ids"}
        wanted = [item for item in dict.fromkeys(ids) if protocol.valid_execution_id(item)]
        if self.executor is None:
            return 409, {"ok": False, "code": protocol.CONTROL_UNAVAILABLE, "message": "the control surface is not started"}
        records = self.executor.status(wanted)
        service = self.compat.service()
        return 200, {
            "ok": True,
            "records": records,
            "generation_running": self.compat.service_generation_running(service),
            "instance": self.instance,
        }

    def _cancel(self, payload: typing.Any) -> typing.Tuple[int, dict]:
        raw = payload if isinstance(payload, dict) else {}
        execution_id = raw.get("execution_id")
        if not protocol.valid_execution_id(execution_id):
            return 400, {"ok": False, "code": compatibility.REQUEST_INVALID, "message": "a cancel names one execution id"}
        if self.executor is None:
            return 409, {"ok": False, "code": protocol.CONTROL_UNAVAILABLE, "message": "the control surface is not started"}
        return 200, {"ok": True, "record": self.executor.cancel(execution_id)}

    def _forget(self, payload: typing.Any) -> typing.Tuple[int, dict]:
        raw = payload if isinstance(payload, dict) else {}
        ids = raw.get("execution_ids")
        listed = [item for item in (ids if isinstance(ids, (list, tuple)) else []) if protocol.valid_execution_id(item)]
        if self.ledger is None:
            return 409, {"ok": False, "code": protocol.CONTROL_UNAVAILABLE, "message": "the control surface is not started"}
        dropped = [item for item in listed if self.ledger.forget(item)]
        return 200, {"ok": True, "forgotten": len(dropped)}


# ------------------------------------------------------------ the listener --


def _build_server(port: int, surface: ControlSurface) -> ThreadingHTTPServer:
    """A loopback-only HTTP server for one child. Bound before it is returned."""

    class Handler(BaseHTTPRequestHandler):
        # The default writes to stderr, which is the child's process log, for
        # every request. A control plane ticking a status request every few
        # seconds would drown the log the crash tail is read from.
        def log_message(self, *_args: typing.Any) -> None:  # noqa: N802 - stdlib name
            return

        protocol_version = "HTTP/1.1"
        server_version = "minipaint-bridge"
        sys_version = ""

        def _answer(self, status: int, body: dict) -> None:
            try:
                data = json.dumps(body).encode("utf-8")
            except Exception:
                status, data = 500, b'{"ok":false,"code":"INTERNAL_ERROR"}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(data)
            except Exception:
                pass

        def do_POST(self) -> None:  # noqa: N802 - stdlib name
            operation = self.path.strip("/").split("/")[-1]
            if not self.path.startswith(protocol.CONTROL_PREFIX + "/") or operation not in protocol.CONTROL_OPERATIONS:
                self._answer(404, {"ok": False, "code": compatibility.REQUEST_INVALID, "message": "no such operation"})
                return
            # The credential first, before the body is read: an unauthorised
            # caller never gets to hand this process a megabyte to parse.
            if not surface.authorised(self.headers.get(protocol.CONTROL_SECRET_HEADER)):
                self._answer(401, {"ok": False, "code": protocol.CONTROL_UNAUTHORISED, "message": "not authorised"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY_BYTES:
                self._answer(413, {"ok": False, "code": compatibility.REQUEST_INVALID, "message": "body too large"})
                return
            try:
                raw = self.rfile.read(length) if length else b"{}"
                payload = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                self._answer(400, {"ok": False, "code": compatibility.REQUEST_INVALID, "message": "unreadable body"})
                return
            status, body = surface.dispatch(operation, payload)
            self._answer(status, body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib name
            # Only hello, and only authenticated. A readiness probe that
            # answered without the credential would be a way to learn that a
            # control surface exists on this port at all.
            if self.path.rstrip("/") != protocol.CONTROL_PREFIX + "/" + protocol.CONTROL_HELLO:
                self._answer(404, {"ok": False, "code": compatibility.REQUEST_INVALID, "message": "no such operation"})
                return
            if not surface.authorised(self.headers.get(protocol.CONTROL_SECRET_HEADER)):
                self._answer(401, {"ok": False, "code": protocol.CONTROL_UNAUTHORISED, "message": "not authorised"})
                return
            status, body = surface.dispatch(protocol.CONTROL_HELLO, {})
            self._answer(status, body)

    server = ThreadingHTTPServer(("127.0.0.1", int(port)), Handler)
    server.daemon_threads = True
    server.timeout = READ_TIMEOUT_SECONDS
    return server


__all__ = ["MAX_BODY_BYTES", "MAX_STATUS_IDS", "READ_TIMEOUT_SECONDS", "ControlSurface"]
