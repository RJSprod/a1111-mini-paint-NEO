"""The card lease: another extension borrowing WanGP's card between WanGP's jobs.

Written for SD-Neo-ModelSwitchRefiner, which runs a text-to-speech model of
eighteen to twenty gigabytes and lets the user place it on the card the
managed WanGP renders on. Its user's rules, which this module exists to keep:

*   a speech request goes NEXT on that card - ahead of every Clipboard job that
    has not started, and ahead of the next task in WanGP's own queue;
*   the WanGP job that is running is never cut off;
*   the Clipboard's next job waits until the guest is done with the card;
*   the guest may stay warm on the card while WanGP is idle, and must give it
    back when WanGP work wants it.

Nothing about that can be decided from outside this package: which jobs are
Mini Paint's, whether one is on the card, how to stop handing WanGP the next
one and how to make WanGP itself stop between tasks are all things only this
integration knows or can do. So the other extension asks here, in-process, by
name - ``importlib.import_module("minipaint_neo.wangp.turns")`` once the host
has imported this package, the way it reads ``presence`` - and gets a plain
dict back. ``docs/wangp/CONTRACTS.md`` (the card lease) is the contract, and
SD-Neo-ModelSwitchRefiner's ``docs/23-voice-box.md`` section 6 is its other
half.

THE SHAPE, AND WHY IT IS THIS ONE.

The public functions - ``request``, ``state``, ``flush``, ``release`` and
``report`` - only record what was asked and read what is known. They never
raise, they return at once, and they never touch the control plane: every
call in ``control.py`` is a blocking socket read, the executor thread is the
one thread allowed to make them, and a caller that is sometimes an ASGI
handler must not be handed a function that sometimes waits ten seconds. The
executor drives the lease instead (``drive``, at the top of every step, and
every second while a lease is live and there is no job): it asks the bridge to
hold WanGP, renews the hold, performs a flush that was asked for, and tells the
bridge to resume when the lease ends. The executor's own gate
(``gate_closed``) is what keeps a Clipboard job from starting WanGP or being
submitted while the card is lent, and ``outbox.claim`` asks the same gate for
the path a page drives.

THE PHASES, AS THE OWNER SEES THEM.

*   ``pending`` - a WanGP task of Mini Paint's is still running on the card.
    The hold has already been asked for, so WanGP stops after it rather than
    picking up whatever it has queued next; the phase only says whose task it
    is.
*   ``holding`` - the bridge has been asked to hold WanGP and has not said the
    card is clear: somebody else's task is finishing, WanGP is starting or
    stopping, another of WanGP's own GPU processes has the card, or a flush is
    being done. The reason says which.
*   ``held`` - nothing is running on WanGP's card for WanGP and nothing will
    start; the owner may use the card. Either the bridge says so, or WanGP is
    not running at all - in which case no Clipboard job will start it either.
*   ``released`` / ``expired`` - the gate is open again, and the bridge is told
    to resume on the executor's next pass; ``bridge_hold`` says ``none`` once
    it has been. A lease not renewed by ``state`` for ``LEASE_TTL_SECONDS``
    expires, so an owner that died cannot keep the Clipboard waiting.
*   ``refused`` - the integration is not set up, another Forge runs WanGP, or
    WanGP is running and cannot be held (a bridge older than 1.12.0, or a
    WanGP build without what a hold needs). The reason says which. A lease the
    owner was already told was ``held`` is never refused afterwards: if WanGP
    comes up unholdable in the middle of it, the lease says ``holding`` with
    ``bridge_hold: unsupported``, and the gate stays closed until it is
    released - opening it under a render in progress would be the one wrong
    answer.

Phases are not monotonic. A ``held`` lease goes back to ``holding`` when a
flush is asked for (until it is done, so "held again" is how an owner knows the
flush settled), when WanGP is started from its tab while the card is lent, and
if a task slips past the hold (see the bridge's ``hold.py`` for the one window
where that can happen). An owner renews with ``state`` and reads the phase
every time.

WHAT IT NEVER DOES.

It never aborts a generation, never cancels a Clipboard job, never presses
Generate and never starts WanGP. A lease only ever makes things wait.
"""

from __future__ import annotations

import collections
import re
import secrets
import threading
import time
import typing

from . import protocol

#: Bumped when a key is added or its meaning changes.
TURNS_VERSION = 1

LEASE_KEYS = ("version", "lease", "owner", "phase", "reason", "card_uuid", "wanted", "bridge_hold", "expires_in_s", "wangp")
"""Every key a lease record carries, and the only ones. Held closed by tests/test_wangp_turns.py."""

WANGP_KEYS = ("running", "task_running", "queue_length", "jobs_waiting", "vram_free_bytes", "vram_total_bytes")
"""The ``wangp`` block of a lease record and of ``report``; any of them None when unknown."""

REPORT_KEYS = ("version", "available", "configured", "card_uuid", "running", "lease", "owner", "phase", "wanted",
               "can_hold", "bridge_hold", "wangp")
"""Every key ``report()`` carries, and the only ones."""

PENDING = "pending"
HOLDING = "holding"
HELD = "held"
RELEASED = "released"
EXPIRED = "expired"
REFUSED = "refused"
PHASES = (PENDING, HOLDING, HELD, RELEASED, EXPIRED, REFUSED)
LIVE = (PENDING, HOLDING, HELD)
TERMINAL = (RELEASED, EXPIRED, REFUSED)

#: A lease not renewed by ``state`` (or a repeated ``request``) for this long
#: expires: the gate opens and the bridge is told to resume. The contract's
#: number, and short on purpose - the owner is in the same process and polls.
LEASE_TTL_SECONDS = 20.0
#: The timer handed to the bridge with every hold. When it runs out without a
#: renewal the bridge resumes WanGP by itself, which is what covers a Forge
#: that died. Longer than the lease's own, because the executor that renews it
#: is one thread whose longest single call (a status read, ``CALL_TIMEOUT``)
#: can take thirty seconds when WanGP is busy; a hold that lapsed while Forge
#: was merely slow would let WanGP start under a render.
BRIDGE_TTL_SECONDS = 45.0
#: How often the executor looks at a live lease when it has no job to run.
DRIVE_SECONDS = 1.0
#: How long a bridge answer is believed. The bridge keeps holding for as long
#: as its timer runs, so an answer is good for about that long - after which
#: the lease says it does not know rather than repeating an old ``held``.
ANSWER_FRESH_SECONDS = BRIDGE_TTL_SECONDS - 5.0
#: How old the executor's count of waiting jobs may be before a public call
#: reads the outbox itself.
DEMAND_FRESH_SECONDS = 5.0
#: How many times a resume is tried before it is left to the bridge's own
#: timer, which lets go within ``BRIDGE_TTL_SECONDS`` anyway.
RESUME_ATTEMPTS = 5
#: The bridge release that can hold WanGP. An older one is recognised, not
#: refused for everything else: it answers every other operation as before.
HOLD_BRIDGE_VERSION = "1.12.0"
#: How many ended leases are remembered, so ``state`` on one says how it
#: ended rather than "unknown".
MAX_RECENT = 16
OWNER_MAX = 60
_GB = 1024.0 ** 3

#: The sentences the reasons are built from, in one place.
TEXT_NOT_SET_UP = "WanGP is not set up in Mini Paint, so there is no WanGP card to lend."
TEXT_OTHER_FORGE = "Another Forge on this machine runs WanGP, so it is not this one's to hold."
TEXT_OLD_BRIDGE = ("WanGP is running with a bridge older than 1.12.0, which cannot hold it between tasks; install "
                   "the bridge again from the WanGP tab's setup (step 4) and restart WanGP.")
TEXT_OLD_BRIDGE_LATE = ("WanGP started with a bridge older than 1.12.0, which cannot hold it between tasks; the card "
                        "is shared until this lease is released.")
TEXT_UNSUPPORTED = "This WanGP build does not expose what holding it between tasks needs, so it cannot be held."
TEXT_UNSUPPORTED_LATE = ("WanGP is running on a build that cannot be held between tasks; the card is shared until "
                         "this lease is released.")
TEXT_PENDING = "A Mini Paint job is still generating on WanGP's card; WanGP stops after it."
TEXT_FINISHING = "WanGP is finishing the task it is running; it stops after it."
TEXT_ASKING = "Asking WanGP to hold between tasks."
TEXT_STARTING = "WanGP is starting; it is held as soon as it answers."
TEXT_STOPPING = "WanGP is stopping."
TEXT_EXPIRED = f"Not renewed for {LEASE_TTL_SECONDS:.0f} s, so the card was given back to WanGP and the Clipboard."
TEXT_NO_LEASE = "No lease by that id is known here; ask for a new one."
TEXT_OFF_WHEN_STOPPED = "WanGP is not running; if it starts, its bridge is older than 1.12.0 and cannot hold it."

_lock = threading.RLock()
_seams: typing.Dict[str, typing.Any] = {"clock": time.monotonic}
#: The current lease - live, or the last one while its resume is still owed.
_lease: typing.Optional[dict] = None
#: Ended leases by id, newest last, so ``state`` can say how one ended.
_recent: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
#: The executor's last count of what wants the card.
_facts: typing.Dict[str, typing.Any] = {"demand": None, "demand_at": -1e18}


# ----------------------------------------------------------------- seams --


def use_clock(clock: typing.Optional[typing.Callable[[], float]]) -> None:
    """Test seam: the monotonic clock expiry is measured on. None restores it."""
    _seams["clock"] = clock or time.monotonic


def reset_for_tests() -> None:
    """Forget every lease and every fact. Nothing is told to the bridge."""
    global _lease
    with _lock:
        _lease = None
        _recent.clear()
        _facts["demand"], _facts["demand_at"] = None, -1e18
    _seams["clock"] = time.monotonic


def _now() -> float:
    return float(_seams["clock"]())


def _journal(message: str) -> None:
    try:
        from . import process_log

        process_log.note("turns", message)
    except Exception:
        pass


def _nudge() -> None:
    """Tell the executor the lease changed, so it acts now rather than after a pause."""
    try:
        from ..clipboard import executor

        executor.nudge()
    except Exception:
        pass


# ------------------------------------------------------------- the facts --


def _clean(value: typing.Any, limit: int) -> str:
    """A caller's words as a record may carry them: printable, one line, bounded."""
    if not isinstance(value, str):
        return ""
    text = "".join(character for character in value if character.isprintable())
    return re.sub(r"\s+", " ", text).strip()[:limit].strip()


def _setup() -> typing.Tuple[bool, str]:
    """``(configured, gpu_uuid)``, from ``presence`` - which caches the file."""
    try:
        from . import presence

        found = presence.report()
        return bool(found.get("configured")), str(found.get("gpu_uuid") or "")
    except Exception:
        return False, ""


def _runtime() -> dict:
    """The managed child as far as a lease needs it, read without taking a lock.

    ``alive`` is the process, not the state: a runtime marked incompatible can
    still be serving pages, and a WanGP that serves pages can generate.
    """
    try:
        from . import runtime

        current = runtime.current()
        state = str(getattr(current, "state", "") or "")
        probe = getattr(current, "is_running", None)
        alive = bool(probe()) if callable(probe) else state in (runtime.READY, runtime.STARTING, runtime.STOPPING)
        return {"alive": alive, "state": state, "ready": alive and state == runtime.READY,
                "starting": state == runtime.STARTING, "stopping": state == runtime.STOPPING,
                "instance": str(getattr(current, "instance_id", "") or ""),
                "code": str(getattr(current, "error_code", "") or "")}
    except Exception:
        return {"alive": False, "state": "", "ready": False, "starting": False, "stopping": False,
                "instance": "", "code": ""}


def _hello() -> typing.Optional[dict]:
    """The last recent hello, which the executor keeps fresh. Never asks."""
    try:
        from . import control

        return control.last_hello()
    except Exception:
        return None


def _demand(fresh: bool = False) -> dict:
    """What wants the card: the executor's count when it is recent, else read now.

    ``fresh`` is the executor's own pass, which always reads. Read OUTSIDE
    this module's lock, always: the outbox takes its own, and ``outbox.claim``
    asks ``gate_closed`` with that lock held, so the order between the two
    must only ever be outbox first.
    """
    with _lock:
        cached, at = _facts["demand"], float(_facts["demand_at"])
    if not fresh and cached is not None and _now() - at <= DEMAND_FRESH_SECONDS:
        return cached
    try:
        from ..clipboard import outbox

        found = outbox.card_demand()
        found = {"waiting": int(found.get("waiting") or 0), "running": list(found.get("running") or []),
                 "submitted": list(found.get("submitted") or [])}
    except Exception:
        found = {"waiting": None, "running": [], "submitted": []}
    with _lock:
        _facts["demand"], _facts["demand_at"] = found, _now()
    return found


def _version(value: typing.Any) -> typing.Tuple[int, ...]:
    parts: typing.List[int] = []
    for chunk in str(value or "").strip().split("."):
        digits = re.match(r"\d+", chunk)
        if not digits:
            break
        parts.append(int(digits.group(0)))
    return tuple(parts)


def _installed_can_hold() -> typing.Optional[bool]:
    """Whether the bridge installed in WanGP could hold it, read off the disk.

    Only asked when WanGP is not running, because a running one answers for
    itself in its hello. A bridge that is missing or switched off can hold
    nothing - WanGP starts without it - and a version before 1.12.0 does not
    know the operation. None when the setup cannot be read at all.
    """
    try:
        from . import config, discovery

        saved = config.load()
        root = str(getattr(saved, "wangp_root", "") or "") if saved is not None else ""
        if not root:
            return None
        code, _detail = discovery.bridge_status(root, "")
        if code:
            return False
        found = _version(discovery.bridge_version(discovery.read_bridge_info(root)))
        return bool(found) and found >= _version(HOLD_BRIDGE_VERSION)
    except Exception:
        return None


def _other_forge() -> bool:
    """Whether another live Forge on this machine holds the WanGP lock."""
    try:
        from . import lock

        return lock.status().get("state") == "other"
    except Exception:
        return False


# ------------------------------------------------------------ the leases --


def _blank(owner: str, purpose: str, need: int, now: float) -> dict:
    return {
        "id": secrets.token_hex(16), "owner": owner, "purpose": purpose, "need_bytes": need,
        "created": now, "renewed": now, "phase": "", "reason": "",
        #: Whether the owner has ever been told ``held``. After that the lease
        #: is never refused, only reported as no longer held.
        "ever_held": False,
        #: Whether a hold was sent to the bridge, and which WanGP run answered.
        "asked": False, "instance": "", "answer": None, "answer_at": -1e18,
        #: WanGP is running and cannot be held, found out mid-lease.
        "unholdable": "",
        "flush": "", "flushing": False, "flushed": "",
        "resume_due": False, "resumed": False, "resume_attempts": 0,
        "error": "", "said": "", "installed_can_hold": None,
    }


def _live(lease: typing.Optional[dict]) -> bool:
    return lease is not None and lease["phase"] not in TERMINAL


def _end(lease: dict, phase: str, reason: str) -> None:
    """End a lease. Lock held. The resume is owed only if a hold was asked for."""
    lease["phase"] = phase
    lease["reason"] = reason
    lease["flush"], lease["flushing"] = "", False
    lease["resume_due"] = True
    lease["resumed"] = not lease["asked"]
    _remember(lease)
    _journal(f"lease {lease['id'][:8]} ({lease['owner']}): {phase} - {reason}")


def _remember(lease: dict) -> None:
    _recent[lease["id"]] = lease
    _recent.move_to_end(lease["id"])
    while len(_recent) > MAX_RECENT:
        _recent.popitem(last=False)


def _expire_due(lease: typing.Optional[dict], now: float) -> bool:
    """End a live lease whose owner stopped renewing it. Lock held. Wakes nobody."""
    if not _live(lease) or now - float(lease["renewed"]) <= LEASE_TTL_SECONDS:
        return False
    _end(lease, EXPIRED, TEXT_EXPIRED)
    return True


def _find(lease_id: str) -> typing.Optional[dict]:
    if _lease is not None and _lease["id"] == lease_id:
        return _lease
    return _recent.get(lease_id)


def _fresh_answer(lease: dict, now: float, rt: dict) -> typing.Optional[dict]:
    """The bridge's last word for this lease, if it is recent and from this run."""
    answer = lease.get("answer")
    if not isinstance(answer, dict) or not rt["ready"]:
        return None
    if lease["instance"] and rt["instance"] and lease["instance"] != rt["instance"]:
        return None
    return answer if now - float(lease["answer_at"]) <= ANSWER_FRESH_SECONDS else None


def _held_reason(lease: dict, rt: dict, answer: typing.Optional[dict]) -> str:
    """Nothing, usually. A flush's outcome, or a hint that one would help."""
    if lease["flushed"]:
        return lease["flushed"]
    if not rt["alive"]:
        return TEXT_OFF_WHEN_STOPPED if lease["installed_can_hold"] is False else ""
    free = (answer or {}).get("vram_free")
    need = int(lease["need_bytes"] or 0)
    if need and isinstance(free, int) and free < need:
        return (f"{free / _GB:.1f} GB is free on WanGP's card and {need / _GB:.1f} GB was asked for; "
                "flush() can move WanGP's weights off it.")
    return ""


def _assess(lease: dict, now: float, rt: dict, demand: dict) -> typing.Tuple[str, str, str]:
    """``(phase, reason, bridge_hold)`` for a lease, from what is known now."""
    if lease["phase"] in TERMINAL:
        told = lease["resumed"] or not lease["asked"]
        return lease["phase"], lease["reason"], protocol.HOLD_NONE if told else _bridge_word(lease, now, rt)
    if not rt["alive"]:
        # Nothing of WanGP's is on the card, and no Clipboard job will start
        # it while the gate is closed. Whether a WanGP started from its tab
        # could be held is said in ``bridge_hold``, so an owner can decide
        # not to stay warm on a card that could be taken back unasked.
        word = protocol.HOLD_UNSUPPORTED if lease["installed_can_hold"] is False else protocol.HOLD_NONE
        return HELD, _held_reason(lease, rt, None), word
    if rt["starting"]:
        return HOLDING, TEXT_STARTING, protocol.HOLD_NONE
    if rt["stopping"]:
        return HOLDING, TEXT_STOPPING, protocol.HOLD_NONE
    if not rt["ready"]:
        why = lease["unholdable"] or f"WanGP is running, but Mini Paint cannot drive it now ({rt['code'] or rt['state']})."
        return HOLDING, why, protocol.HOLD_UNSUPPORTED
    if lease["unholdable"]:
        return HOLDING, lease["unholdable"], protocol.HOLD_UNSUPPORTED
    answer = _fresh_answer(lease, now, rt)
    word = answer["hold"] if answer is not None else protocol.HOLD_NONE
    if lease["flush"] or lease["flushing"]:
        level = lease["flush"] or "a"
        return HOLDING, f"WanGP's weights are being moved off its card ({level} flush).", word
    if answer is None:
        if demand.get("running"):
            return PENDING, TEXT_PENDING, protocol.HOLD_NONE
        if lease["error"]:
            return HOLDING, f"WanGP did not answer the hold ({lease['error']}); asking again.", protocol.HOLD_NONE
        return HOLDING, TEXT_ASKING, protocol.HOLD_NONE
    if word == protocol.HOLD_HELD:
        return HELD, _held_reason(lease, rt, answer), word
    if word == protocol.HOLD_UNSUPPORTED:
        return HOLDING, TEXT_UNSUPPORTED_LATE, word
    ours = answer.get("active_client_id")
    if answer.get("task_running") and ours and ours in (demand.get("submitted") or []):
        return PENDING, TEXT_PENDING, word
    if answer.get("claim") == protocol.CLAIM_BUSY:
        who = answer.get("busy_with") or "another of its own processes"
        return HOLDING, f"WanGP's GPU is in use by {who}; the hold waits for it to finish.", word
    return HOLDING, TEXT_FINISHING, word


def _bridge_word(lease: dict, now: float, rt: dict) -> str:
    answer = _fresh_answer(lease, now, rt)
    return answer["hold"] if answer is not None else protocol.HOLD_NONE


def _wangp(rt: dict, source: typing.Optional[dict], demand: dict) -> dict:
    """The ``wangp`` block: what the freshest answer says, None where nothing does."""
    block: typing.Dict[str, typing.Any] = {key: None for key in WANGP_KEYS}
    block["running"] = bool(rt["ready"])
    block["jobs_waiting"] = demand.get("waiting")
    if not rt["alive"]:
        # No process, no task - that much is known. Its queue and the card's
        # numbers are not this module's to guess at.
        block["task_running"] = False
        return block
    if isinstance(source, dict) and rt["ready"]:
        block["task_running"] = source.get("task_running")
        block["queue_length"] = source.get("queue_length")
        block["vram_free_bytes"] = source.get("vram_free")
        block["vram_total_bytes"] = source.get("vram_total")
    return block


def _wanted(demand: dict, source: typing.Optional[dict]) -> bool:
    """A Clipboard job waits for the card, or WanGP work waits on the hold."""
    return bool(demand.get("waiting")) or bool(isinstance(source, dict) and source.get("waiter") is True)


def _record(lease: dict, now: float, rt: dict, demand: dict, uuid: str,
            hello: typing.Optional[dict]) -> dict:
    """The lease as its owner reads it. Lock held."""
    phase, reason, word = _assess(lease, now, rt, demand)
    live = lease["phase"] not in TERMINAL
    if live and phase == HELD:
        lease["ever_held"] = True
    answer = _fresh_answer(lease, now, rt) if live else None
    source = answer if answer is not None else hello
    remaining = max(0.0, LEASE_TTL_SECONDS - (now - float(lease["renewed"]))) if live else 0.0
    record = {
        "version": TURNS_VERSION,
        "lease": lease["id"],
        "owner": lease["owner"],
        "phase": phase,
        "reason": reason,
        "card_uuid": uuid,
        "wanted": _wanted(demand, source),
        "bridge_hold": word,
        "expires_in_s": round(remaining, 1),
        "wangp": _wangp(rt, source, demand),
    }
    if live and phase != lease["said"]:
        lease["said"] = phase
        _journal(f"lease {lease['id'][:8]} ({lease['owner']}): {phase}" + (f" - {reason}" if reason else ""))
    return record


def _refusal(owner: str, reason: str, uuid: str = "", lease_id: str = "") -> dict:
    """A refused record that belongs to no live lease."""
    return {
        "version": TURNS_VERSION, "lease": lease_id or secrets.token_hex(16), "owner": owner, "phase": REFUSED,
        "reason": reason, "card_uuid": uuid, "wanted": False, "bridge_hold": protocol.HOLD_NONE,
        "expires_in_s": 0.0, "wangp": {key: None for key in WANGP_KEYS},
    }


def _why_not(configured: bool, rt: dict, hello: typing.Optional[dict]) -> str:
    """Why a lease cannot be granted at all, or ""."""
    if not configured:
        return TEXT_NOT_SET_UP
    if _other_forge():
        return TEXT_OTHER_FORGE
    if rt["alive"] and not (rt["ready"] or rt["starting"] or rt["stopping"]):
        return (f"WanGP is running, but Mini Paint cannot drive it right now ({rt['code'] or rt['state']}), "
                "so it cannot be held.")
    if rt["ready"] and isinstance(hello, dict):
        capabilities = hello.get("capabilities") if isinstance(hello.get("capabilities"), dict) else {}
        if not capabilities.get("hold"):
            return TEXT_OLD_BRIDGE
        if hello.get("hold") == protocol.HOLD_UNSUPPORTED:
            return TEXT_UNSUPPORTED
    return ""


def _lease_id(value: typing.Any) -> str:
    return value if protocol.valid_lease_id(value) else ""


def _need(value: typing.Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        found = int(value)
    except (TypeError, ValueError):
        return 0
    return found if found > 0 else 0


# -------------------------------------------------------------- the api --


def request(owner: typing.Any, *, purpose: typing.Any = "", need_bytes: typing.Any = 0) -> dict:
    """Ask for WanGP's card. Returns the lease record at once; never raises.

    The gate closes the moment this returns a live lease: no Clipboard job is
    started or submitted from then on. The executor asks the bridge to hold
    WanGP on its next pass - within a second or two - and the phase says what
    happened; poll ``state``, which also renews the lease. A second request
    by the same owner returns (and renews) the live lease; a request by
    anybody else while one is live is refused, one lease at a time.

    ``purpose`` is a short name for what the card is for - it becomes the name
    WanGP's own status line gives the holder - and ``need_bytes`` how much of
    the card the owner expects to need, which only ever turns into a hint in
    ``reason``: whether to flush is the owner's decision.
    """
    try:
        return _request(owner, purpose, need_bytes)
    except Exception as error:  # noqa: BLE001 - a lease never raises into its owner
        return _refusal(_clean(owner, OWNER_MAX), f"The card lease failed inside Mini Paint ({type(error).__name__}).")


def _request(owner: typing.Any, purpose: typing.Any, need_bytes: typing.Any) -> dict:
    global _lease
    name = _clean(owner, OWNER_MAX)
    if not name:
        return _refusal("", "A lease needs an owner: the name of whoever asks for the card.")
    configured, uuid = _setup()
    rt = _runtime()
    hello = _hello()
    demand = _demand()
    installed = None if rt["alive"] else _installed_can_hold()
    now = _now()
    granted = False
    with _lock:
        current = _lease
        _expire_due(current, now)
        if _live(current):
            if current["owner"] == name:
                current["renewed"] = now
                return _record(current, now, rt, demand, uuid, hello)
            return _refusal(name, f"WanGP's card is already lent to {current['owner']}; one lease at a time.", uuid)
        why = _why_not(configured, rt, hello)
        lease = _blank(name, _clean(purpose, 80), _need(need_bytes), now)
        lease["installed_can_hold"] = installed
        if why:
            _end(lease, REFUSED, why)
        else:
            if current is not None:
                _remember(current)
            _lease = lease
            granted = True
            need = f", asks for {lease['need_bytes'] / _GB:.1f} GB" if lease["need_bytes"] else ""
            _journal(f"lease {lease['id'][:8]}: WanGP's card asked for by {name}"
                     + (f" ({lease['purpose']}{need})" if lease["purpose"] or need else "")
                     + "; the Clipboard's gate is closed")
        record = _record(lease, now, rt, demand, uuid, hello)
    if granted:
        _nudge()
    return record


def state(lease: typing.Any) -> dict:
    """The lease as it stands, and a renewal of it. Never raises.

    Call it at least every ``LEASE_TTL_SECONDS`` for as long as the card is
    needed - through a render too - or the lease expires and WanGP resumes.
    An ended lease answers how it ended; an id nobody here knows is refused.
    """
    try:
        return _state(lease)
    except Exception as error:  # noqa: BLE001
        return _refusal("", f"The card lease failed inside Mini Paint ({type(error).__name__}).", lease_id=_lease_id(lease))


def _state(lease: typing.Any) -> dict:
    key = _lease_id(lease)
    _configured, uuid = _setup()
    rt = _runtime()
    hello = _hello()
    demand = _demand()
    now = _now()
    with _lock:
        found = _find(key) if key else None
        if found is None:
            return _refusal("", TEXT_NO_LEASE, uuid, key)
        _expire_due(found, now)
        if _live(found):
            found["renewed"] = now
        return _record(found, now, rt, demand, uuid, hello)


def flush(lease: typing.Any, level: typing.Any) -> dict:
    """Ask for WanGP's weights to be moved off its card: ``soft`` or ``hard``.

    Only while the lease is ``held``. It returns at once, like everything
    here: the executor does the flush on its next pass, and until it has, the
    lease says ``holding`` ("WanGP's weights are being moved off its card").
    ``held`` again means it is done - or was refused, and ``reason`` then
    says why (a WanGP run paused between tasks refuses a hard flush; a WanGP
    set to load its model at start refuses it always). ``soft`` keeps
    WanGP's weights in RAM, so its next task starts without a disk read;
    ``hard`` releases them, so its next task reloads them from disk.

    With WanGP not running there is nothing of it on the card, and the lease
    says so without asking anybody. Also renews the lease.
    """
    try:
        return _flush(lease, level)
    except Exception as error:  # noqa: BLE001
        return _refusal("", f"The card lease failed inside Mini Paint ({type(error).__name__}).", lease_id=_lease_id(lease))


def _flush(lease: typing.Any, level: typing.Any) -> dict:
    key = _lease_id(lease)
    _configured, uuid = _setup()
    rt = _runtime()
    hello = _hello()
    demand = _demand()
    now = _now()
    asked = False
    with _lock:
        found = _find(key) if key else None
        if found is None:
            return _refusal("", TEXT_NO_LEASE, uuid, key)
        _expire_due(found, now)
        if not _live(found):
            return _record(found, now, rt, demand, uuid, hello)
        found["renewed"] = now
        if level not in protocol.FLUSH_LEVELS:
            record = _record(found, now, rt, demand, uuid, hello)
            record["reason"] = "A flush is soft or hard, so nothing was asked of WanGP."
            return record
        phase, _reason, _word = _assess(found, now, rt, demand)
        if found["flush"] or found["flushing"]:
            return _record(found, now, rt, demand, uuid, hello)
        if phase != HELD:
            record = _record(found, now, rt, demand, uuid, hello)
            record["reason"] = f"The card is not held yet ({phase}), so nothing was flushed."
            return record
        if not rt["alive"]:
            found["flushed"] = "WanGP is not running, so none of it was on the card to flush."
        else:
            found["flush"], found["flushed"] = level, ""
            asked = True
            _journal(f"lease {found['id'][:8]}: a {level} flush asked for by {found['owner']}")
        record = _record(found, now, rt, demand, uuid, hello)
    if asked:
        _nudge()
    return record


def release(lease: typing.Any, *, reason: typing.Any = "") -> dict:
    """Give WanGP's card back. Never raises; releasing twice is releasing once.

    The gate opens at once - the next Clipboard job may go - and the executor
    tells the bridge to resume on its next pass; ``bridge_hold`` reads
    ``none`` from then on. Before releasing, an owner that put memory on the
    card should have taken it off again: WanGP sizes itself against what the
    card reports free and has nothing to fall back on.
    """
    try:
        return _release(lease, reason)
    except Exception as error:  # noqa: BLE001
        return _refusal("", f"The card lease failed inside Mini Paint ({type(error).__name__}).", lease_id=_lease_id(lease))


def _release(lease: typing.Any, reason: typing.Any) -> dict:
    key = _lease_id(lease)
    _configured, uuid = _setup()
    rt = _runtime()
    hello = _hello()
    demand = _demand()
    now = _now()
    ended = False
    with _lock:
        found = _find(key) if key else None
        if found is None:
            return _refusal("", TEXT_NO_LEASE, uuid, key)
        _expire_due(found, now)
        if _live(found):
            _end(found, RELEASED, _clean(reason, 200) or f"Given back by {found['owner']}.")
            ended = True
        record = _record(found, now, rt, demand, uuid, hello)
    if ended:
        _nudge()
    return record


def report() -> dict:
    """WanGP's activity and the lease, with or without one. Never raises.

    ``can_hold`` is whether the bridge speaks hold at all - from WanGP's
    recent hello when it is running, from the installed bridge's version when
    it is not, None when neither can be read. ``bridge_hold`` is the bridge's
    own word about a hold now, or "" when nothing recent has said.
    """
    found: typing.Dict[str, typing.Any] = {
        "version": TURNS_VERSION, "available": True, "configured": False, "card_uuid": "", "running": False,
        "lease": "", "owner": "", "phase": "", "wanted": False, "can_hold": None, "bridge_hold": "",
        "wangp": {key: None for key in WANGP_KEYS},
    }
    try:
        found["configured"], found["card_uuid"] = _setup()
        rt = _runtime()
        hello = _hello()
        demand = _demand()
        now = _now()
        found["running"] = bool(rt["ready"])
        with _lock:
            current = _lease
            _expire_due(current, now)
            answer = None
            if _live(current):
                found["lease"], found["owner"] = current["id"], current["owner"]
                found["phase"] = _assess(current, now, rt, demand)[0]
                answer = _fresh_answer(current, now, rt)
        source = answer if answer is not None else hello
        found["wanted"] = _wanted(demand, source)
        found["wangp"] = _wangp(rt, source, demand)
        if rt["ready"] and isinstance(hello, dict):
            found["can_hold"] = bool((hello.get("capabilities") or {}).get("hold"))
        elif not rt["alive"]:
            found["can_hold"] = _installed_can_hold()
        if isinstance(source, dict) and source.get("hold") in protocol.HOLD_STATES:
            found["bridge_hold"] = source["hold"]
    except Exception:  # noqa: BLE001 - each part already read on its own; the defaults stand
        pass
    return found


# ------------------------------------------------- the executor's side --


def gate_closed() -> bool:
    """Whether the card is lent: no Clipboard job may start WanGP or reach it.

    Asked by the executor before every stage that touches WanGP's card and by
    ``outbox.claim`` with the outbox's lock held - so this takes nothing but
    this module's own lock and wakes nobody. A lease past its renewal is
    ended here too; the executor, which looks every second while a lease is
    owed anything, tells the bridge.
    """
    with _lock:
        current = _lease
        _expire_due(current, _now())
        return _live(current)


def gate_owner() -> typing.Optional[str]:
    """Who the card is lent to, or None while it is not."""
    with _lock:
        current = _lease
        _expire_due(current, _now())
        return current["owner"] if _live(current) else None


def needs_driver() -> bool:
    """Whether the executor has lease work to do even with no job: a live
    lease to hold and renew, or a resume still owed."""
    with _lock:
        current = _lease
        if current is None:
            return False
        _expire_due(current, _now())
        return _live(current) or (current["resume_due"] and not current["resumed"])


def driver_interval() -> float:
    return DRIVE_SECONDS


def drive() -> None:
    """One pass of the lease's own work. The executor thread's, and nobody else's.

    Every control call is made here, with this module's lock released: hello
    (whether the bridge can hold at all, and a fresh cache for everybody who
    reads ``last_hello``), hold (asked, or renewed, and where it stands),
    flush when one is owed and WanGP is held, and resume once a lease has
    ended. A call that fails is noted on the lease and tried again on the
    next pass; nothing here raises into the executor.
    """
    now = _now()
    with _lock:
        lease = _lease
        if lease is None:
            return
        _expire_due(lease, now)
        live = _live(lease)
        owed = (not live) and lease["resume_due"] and not lease["resumed"]
        if not live and not owed:
            return
    demand = _demand(fresh=True)
    rt = _runtime()
    if owed:
        _drive_resume(lease, rt)
        return
    if not rt["alive"]:
        with _lock:
            # A hold lives in the process that took it. Gone with it.
            lease["answer"], lease["instance"] = None, ""
        return
    if rt["starting"] or rt["stopping"]:
        return
    if not rt["ready"]:
        _unholdable(lease, f"WanGP is running, but Mini Paint cannot drive it now ({rt['code'] or rt['state']}).",
                    f"WanGP is running, but Mini Paint cannot drive it now ({rt['code'] or rt['state']}), "
                    "so it cannot be held.")
        return
    try:
        from . import control
        from .errors import IntegrationError

        try:
            hello = control.hello(timeout=control.HOLD_TIMEOUT)
        except IntegrationError as error:
            _note_error(lease, error.code)
            return
        if not control.can_hold(hello):
            _unholdable(lease, TEXT_OLD_BRIDGE_LATE, TEXT_OLD_BRIDGE)
            return
        label = protocol.clean_hold_label(lease["purpose"]) or protocol.clean_hold_label(lease["owner"])
        try:
            answer = control.hold(lease["id"], BRIDGE_TTL_SECONDS, label)
        except IntegrationError as error:
            _note_error(lease, error.code)
            return
        if answer.get("hold") not in protocol.HOLD_STATES:
            # An answer that names no hold state is not an answer about a
            # hold. Asked again next pass; never read as held.
            _note_error(lease, answer.get("code") or "INTERNAL_ERROR")
            return
    except Exception as error:  # noqa: BLE001 - the executor keeps going
        _note_error(lease, type(error).__name__)
        return
    level = ""
    with _lock:
        lease["asked"] = True
        lease["answer"], lease["answer_at"], lease["instance"] = answer, _now(), rt["instance"]
        lease["error"] = ""
        if not _live(lease):
            # Released while the hold was on its way: the resume it now owes
            # goes out on the next pass.
            lease["resumed"] = False
            return
        if answer["hold"] == protocol.HOLD_HELD:
            lease["unholdable"] = ""
            if lease["flush"]:
                level, lease["flush"], lease["flushing"] = lease["flush"], "", True
        elif answer["hold"] == protocol.HOLD_UNSUPPORTED:
            _unholdable_locked(lease, TEXT_UNSUPPORTED_LATE, TEXT_UNSUPPORTED)
    if level:
        _drive_flush(lease, level)


def _note_error(lease: dict, code: str) -> None:
    with _lock:
        if lease["error"] != code:
            _journal(f"lease {lease['id'][:8]}: WanGP's control surface did not answer ({code}); asking again")
        lease["error"] = code or "INTERNAL_ERROR"


def _unholdable(lease: dict, late: str, never: str) -> None:
    with _lock:
        _unholdable_locked(lease, late, never)


def _unholdable_locked(lease: dict, late: str, never: str) -> None:
    """WanGP is running and cannot be held. Refused, unless the owner was
    already told ``held`` - then the lease stays, not held, gate closed."""
    if not _live(lease):
        return
    if not lease["ever_held"]:
        _end(lease, REFUSED, never)
        return
    if lease["unholdable"] != late:
        _journal(f"lease {lease['id'][:8]}: {late}")
    lease["unholdable"] = late


def _drive_flush(lease: dict, level: str) -> None:
    """Do the flush that was asked for, and say how it went."""
    started = time.monotonic()
    answer: typing.Optional[dict] = None
    try:
        from . import control, errors
        from .errors import IntegrationError

        try:
            answer = control.flush(lease["id"], level)
            took = time.monotonic() - started
            free, total = answer.get("vram_free"), answer.get("vram_total")
            room = (f"; {free / _GB:.1f} GB of {total / _GB:.1f} GB is free on WanGP's card"
                    if isinstance(free, int) and isinstance(total, int) and total else "")
            outcome = f"{level.capitalize()} flush done in {took:.1f} s{room}."
            detail = ", ".join(answer.get("parts") or []) or "nothing to move"
            _journal(f"lease {lease['id'][:8]}: {level} flush done in {took:.1f} s ({detail}){room}")
        except IntegrationError as error:
            outcome = f"{level.capitalize()} flush refused: {errors.message(error.code)}"
            _journal(f"lease {lease['id'][:8]}: {level} flush refused ({error.code})")
    except Exception as error:  # noqa: BLE001
        outcome = f"{level.capitalize()} flush failed ({type(error).__name__})."
        _journal(f"lease {lease['id'][:8]}: {level} flush failed ({type(error).__name__})")
    with _lock:
        lease["flushing"] = False
        lease["flushed"] = outcome
        if isinstance(answer, dict) and answer.get("hold") in protocol.HOLD_STATES:
            lease["answer"], lease["answer_at"] = answer, _now()


def _drive_resume(lease: dict, rt: dict) -> None:
    """Tell the bridge to let WanGP go, if the WanGP that was held is still there."""
    same = lease["asked"] and rt["ready"] and (not lease["instance"] or lease["instance"] == rt["instance"])
    gave_up = False
    if same:
        try:
            from . import control
            from .errors import IntegrationError

            try:
                control.resume(lease["id"])
            except IntegrationError as error:
                with _lock:
                    lease["resume_attempts"] += 1
                    if lease["resume_attempts"] < RESUME_ATTEMPTS:
                        return
                gave_up = True
                _journal(f"lease {lease['id'][:8]}: the bridge did not take the resume ({error.code}); "
                         f"its own timer lets WanGP go within {BRIDGE_TTL_SECONDS:.0f} s")
        except Exception:  # noqa: BLE001
            gave_up = True
    with _lock:
        lease["resumed"] = True
        lease["answer"] = None
    if same and not gave_up:
        _journal(f"lease {lease['id'][:8]}: the bridge was told to resume WanGP")


__all__ = [
    "ANSWER_FRESH_SECONDS", "BRIDGE_TTL_SECONDS", "DRIVE_SECONDS", "EXPIRED", "HELD", "HOLDING",
    "HOLD_BRIDGE_VERSION", "LEASE_KEYS", "LEASE_TTL_SECONDS", "LIVE", "PENDING", "PHASES", "REFUSED",
    "RELEASED", "REPORT_KEYS", "TERMINAL", "TURNS_VERSION", "WANGP_KEYS",
    "drive", "driver_interval", "flush", "gate_closed", "gate_owner", "needs_driver", "release", "report",
    "request", "reset_for_tests", "state", "use_clock",
]
