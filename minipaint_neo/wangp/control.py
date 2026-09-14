"""Forge's half of the control plane: how the server reaches WanGP directly.

Everything else this extension does to WanGP goes through a browser. A send
writes a value into a live Gradio session, a queue request writes an overlay
and presses a trigger, and both require a page to be open because the form
they act on is session state belonging to an iframe in one document. That is
correct for what a person does while looking at a screen.

It is not how a job somebody walked away from gets run. So this module is the
other way in: an ordinary authenticated JSON POST from this process to the
managed child's own loopback surface, with no page involved and frequently
with no page in existence.

Four rules, and they are the reason this is a module rather than three lines
in the executor:

*   **The destination comes from the runtime object and nowhere else.** The
    host and scheme are literals here, the port is the one this process kept
    before it launched that child, and nothing a caller passes can reach the
    URL. It is the same discipline the proxy's ``upstream`` is written under,
    for the same reason.

*   **The credential is this run's.** It was minted per launch, exported into
    the child, and never persisted. A request that gets 401 is not retried
    with anything else; it means the child is from another run, and the
    caller's answer to that is to reconcile, not to guess.

*   **Nothing blocks for a generation.** Every call here is a short request
    with a finite timeout. Submission returns as soon as the child has
    recorded the task; progress is read by asking again. Forge never holds a
    socket open for the length of a generation, which is the shape B19
    requires on both sides of this wire.

*   **It is never called from an ASGI handler.** Every caller is the executor
    thread. A route that needs to know something asks the durable job record,
    which the executor keeps up to date.
"""

from __future__ import annotations

import json
import threading
import time
import typing
import urllib.error
import urllib.request

from . import errors, protocol
from .errors import IntegrationError

#: The only host this module will speak to, written here as a literal for the
#: reason ``proxy.upstream`` gives: a destination that can come from outside
#: the function is an open proxy waiting to be found.
LOOPBACK = "127.0.0.1"

#: How long a control call may take. Generous for a child that is loading a
#: model and answering on a busy thread; finite, because the executor is a
#: single thread and a call that never returns is a queue that never moves.
CONNECT_TIMEOUT = 5.0
CALL_TIMEOUT = 30.0
#: The compose call reads settings off a live process and can be slower.
COMPOSE_TIMEOUT = 60.0

_LOG_PREFIX = "MiniPaint WanGP:"
_lock = threading.RLock()
#: Test seam: a callable taking (operation, payload) and returning (status,
#: body), standing in for the socket. Nothing in the extension sets it.
_seams: typing.Dict[str, typing.Any] = {"transport": None}
#: The last answer to ``hello``, and when. Read by anything that wants to
#: know what the child is doing *without* asking it - which is every async
#: route, because every call in this module is a blocking socket read and an
#: async route that made one would stall the whole of Forge for its timeout.
_last: typing.Dict[str, typing.Any] = {"at": 0.0, "hello": None}
#: How old a cached answer may be before it is reported as not known. Longer
#: than the executor's own poll, so a queue that is moving keeps it fresh;
#: short enough that a stale card state is never presented as current.
HELLO_TTL = 15.0


def use_transport(transport: typing.Optional[typing.Callable[..., typing.Tuple[int, dict]]]) -> None:
    """Test seam: answer control calls without a socket. None restores it."""
    with _lock:
        _seams["transport"] = transport


def reset_for_tests() -> None:
    use_transport(None)
    with _lock:
        _last["at"] = 0.0
        _last["hello"] = None


# ---------------------------------------------------------------- the wire --


def _endpoint() -> typing.Tuple[str, str]:
    """``(base url, secret)`` for the child this Forge is running, or a refusal.

    Both come off the runtime object together, deliberately: a port from one
    run and a secret from another would produce a 401 that looked like a
    configuration problem rather than the restart it is.
    """
    from . import runtime

    current = runtime.current()
    if current.state != runtime.READY:
        raise IntegrationError(errors.WANGP_NOT_RUNNING, f"the managed WanGP is {current.state}")
    port = int(getattr(current, "control_port", 0) or 0)
    secret = current.bridge_secret()
    if port <= 0 or not secret:
        raise IntegrationError(
            errors.CONTROL_UNAVAILABLE,
            "this WanGP was launched without a control surface; restart it to enable unattended jobs",
        )
    return f"http://{LOOPBACK}:{port}{protocol.CONTROL_PREFIX}", secret


def call(operation: str, payload: typing.Optional[dict] = None, timeout: float = CALL_TIMEOUT) -> dict:
    """One control request. Returns the body; raises IntegrationError instead
    of returning a refusal, so a caller cannot forget to look.

    ``urllib`` rather than the proxy's httpx client because this is not an
    async path and must never touch the event loop's client: the executor is
    an ordinary worker thread, and borrowing a client that belongs to
    another loop is the bug the proxy's own ``_client`` exists to avoid.
    """
    if operation not in protocol.CONTROL_OPERATIONS:
        raise IntegrationError(errors.REQUEST_INVALID, f"no control operation {str(operation)[:40]!r}")
    transport = _seams.get("transport")
    if transport is not None:
        status, body = transport(operation, dict(payload or {}))
        return _settle(operation, status, body if isinstance(body, dict) else {})

    base, secret = _endpoint()
    data = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(f"{base}/{operation}", data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header(protocol.CONTROL_SECRET_HEADER, secret)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            body = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read().decode("utf-8") or "{}")
        except Exception:
            body = {}
        status = int(error.code)
    except Exception as error:
        raise IntegrationError(errors.CONTROL_UNAVAILABLE, f"the control surface did not answer: {type(error).__name__}")
    return _settle(operation, status, body if isinstance(body, dict) else {})


def _settle(operation: str, status: int, body: dict) -> dict:
    """Turn one answer into a body or a coded refusal."""
    if status == 200 and body.get("ok") is True:
        return body
    code = body.get("code")
    code = code if isinstance(code, str) and protocol.CODE_RE.match(code) else ""
    if status == 401:
        code = code or errors.CONTROL_UNAUTHORISED
    elif not code:
        code = errors.CONTROL_UNAVAILABLE
    raise IntegrationError(code, f"{operation}: {str(body.get('message') or '')[:200]}")


# ------------------------------------------------------------ operations --


def hello(timeout: float = CALL_TIMEOUT) -> dict:
    """What the child is, and whether it can take a job right now.

    READY is not an admission fact. It says a process is alive and answered
    an HTTP request; it does not say the bridge is on the page, that the
    generation service resolved, or that a settings base can be read. Those
    are the child's to answer and this is where it does.
    """
    answer = protocol.normalize_control_hello(call(protocol.CONTROL_HELLO, {}, timeout))
    with _lock:
        _last["at"] = time.time()
        _last["hello"] = answer
    return answer


def last_hello(max_age: float = HELLO_TTL) -> typing.Optional[dict]:
    """The most recent ``hello``, if it is recent enough to mean anything.

    THE ONLY THING AN ASYNC ROUTE MAY ASK. Every call in this module is a
    blocking socket read with a finite but real timeout; one made from a
    route would stall Forge's event loop - every page, every tab, every
    other extension - for as long as the child took to answer. The executor
    thread is what keeps this fresh, and a value older than ``max_age`` is
    reported as "not known" rather than presented as current.
    """
    with _lock:
        answer = _last.get("hello")
        at = float(_last.get("at") or 0.0)
    if answer is None or (time.time() - at) > max(0.0, float(max_age)):
        return None
    return dict(answer)


def compose(
    model_type: str = "",
    session_hash: str = "",
    inherit: bool = True,
    timeout: float = COMPOSE_TIMEOUT,
) -> dict:
    """The settings base a job will run at, and where it came from.

    ``inherit`` False asks the child not to read the user's committed form at
    all, so WanGP fills the job in from that model's own saved defaults.
    """
    answer = call(
        protocol.CONTROL_COMPOSE,
        {"model_type": model_type, "session_hash": session_hash, "inherit": bool(inherit)},
        timeout,
    )
    return protocol.normalize_compose_answer(answer)


def submit(
    execution_id: str,
    settings: typing.Mapping[str, typing.Any],
    prompt: typing.Optional[str] = None,
    media: typing.Optional[typing.Mapping[str, typing.Any]] = None,
    model_type: str = "",
    priority: bool = False,
    timeout: float = CALL_TIMEOUT,
) -> dict:
    """Hand one generation to WanGP's own queue. Returns the ledger record.

    Idempotent by execution id on the child's side, which is what makes it
    safe to call again after an answer that never arrived: a repeated
    submission is answered from the ledger rather than by asking WanGP twice.
    """
    payload: typing.Dict[str, typing.Any] = {
        "execution_id": execution_id,
        "settings": dict(settings),
        "media": dict(media or {}),
        "model_type": model_type,
        "priority": bool(priority),
    }
    if prompt is not None:
        payload["prompt"] = prompt
    answer = call(protocol.CONTROL_SUBMIT, payload, timeout)
    return protocol.normalize_execution_record(answer.get("record"))


def status(execution_ids: typing.Sequence[str], timeout: float = CALL_TIMEOUT) -> typing.Dict[str, dict]:
    """Where each of these is, as the child's own ledger says."""
    wanted = [item for item in dict.fromkeys(execution_ids) if protocol.valid_execution_id(item)]
    if not wanted:
        return {}
    answer = call(protocol.CONTROL_STATUS, {"execution_ids": wanted}, timeout)
    records = answer.get("records")
    out: typing.Dict[str, dict] = {}
    for execution_id in wanted:
        found = records.get(execution_id) if isinstance(records, dict) else None
        out[execution_id] = protocol.normalize_execution_record(found)
    return out


def cancel(execution_id: str, timeout: float = CALL_TIMEOUT) -> dict:
    """Stop one of ours. Never disturbs a generation that is not ours."""
    answer = call(protocol.CONTROL_CANCEL, {"execution_id": execution_id}, timeout)
    return protocol.normalize_execution_record(answer.get("record"))


def forget(execution_ids: typing.Sequence[str], timeout: float = CALL_TIMEOUT) -> int:
    """Release terminal ledger records Forge has finished with.

    Only terminal ones; the child refuses the rest. Forge asks for this after
    it has durably recorded an outcome, so the two ledgers converge rather
    than growing forever on the assumption that somebody else is sweeping.
    """
    wanted = [item for item in dict.fromkeys(execution_ids) if protocol.valid_execution_id(item)]
    if not wanted:
        return 0
    try:
        answer = call(protocol.CONTROL_FORGET, {"execution_ids": wanted}, timeout)
    except IntegrationError:
        return 0
    count = answer.get("forgotten")
    return int(count) if isinstance(count, int) else 0


def available() -> typing.Tuple[bool, str]:
    """Whether unattended execution can run right now, and the code if not.

    A cheap question with a real answer, for a status line and for the
    executor's own gate. Never raises.
    """
    try:
        answer = hello(timeout=CONNECT_TIMEOUT + 5.0)
    except IntegrationError as error:
        return False, error.code
    if answer["control_version"] != protocol.CONTROL_VERSION:
        return False, errors.CONTROL_VERSION_MISMATCH
    if not answer["can_execute"]:
        return False, answer["code"] or errors.SERVICE_UNAVAILABLE
    return True, ""


def executable_ever() -> bool:
    """Whether the child could ever run a server-side job, as it last said.

    False is a permanent fact about the installed Wan2GP - it does not carry
    the generation service - and is the difference between a job worth
    waiting for and one that would wait forever.
    """
    answer = last_hello()
    return bool((answer or {}).get("service_possible", True))


def why_not() -> str:
    """The child's own account of why it cannot execute, or "".

    Read from the cached hello rather than asked for, because the caller is
    the executor and it has just asked. Diagnostic only.
    """
    answer = last_hello()
    return str((answer or {}).get("diagnosis") or "")


__all__ = [
    "CALL_TIMEOUT", "COMPOSE_TIMEOUT", "CONNECT_TIMEOUT", "LOOPBACK",
    "HELLO_TTL", "available", "call", "cancel", "compose", "forget", "hello", "last_hello", "reset_for_tests",
    "executable_ever", "status", "submit", "use_transport", "why_not",
]
