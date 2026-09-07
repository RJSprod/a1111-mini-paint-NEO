"""Which browser page is talking to which live WanGP session, and what it said.

WanGP's visible state is not one thing. Gradio keeps a session per browser
page, so the answer to "what image inputs can WanGP accept right now" is only
ever true of *one* page. Two Forge tabs can sit on two different models with
two different reference lists, and a process-global "current receivers" would
quietly let the second one decide where the first one's picture goes. So there
is no such variable here. Everything a session knows lives in a ``Session``
keyed by the channel id of the page that established it, the sessions live in
one dictionary owned by one ``Registry``, and the only way to reach a session
is to name the channel it belongs to. That is the whole reason this module
exists, and it is why ``Session`` is frozen: an update replaces the record
under the lock instead of mutating a structure another page might be reading.

Sessions are bound to a runtime instance id as well as to a page. When the
WanGP child restarts, everything the old child told us stops being true - the
model, the receiver list, the revision, and above all the identity of the slot
an in-flight send is about to fill. ``invalidate_instance`` drops those
sessions, and a send that was already past the gate when it happened fails with
``WANGP_RESTARTED`` rather than landing in a process that never agreed to
receive it.

``check_send`` is the gate, and it is deliberately joyless: it answers only
"may this exact image go to this exact receiver at this exact revision", and
every answer that is not yes is a refusal with a code. It never picks a
different receiver, never switches model or mode, never rounds a stale
revision up to the current one. A destination that cannot be proven correct is
not sent to.

On the channel id, plainly, because section 13.3 insists on it: a
browser-visible channel id is for message correlation and confusion
prevention. It is **not** an authentication boundary against JavaScript that is
already running on the Forge origin - such code is already inside the browser's
trust boundary and can read the id like anything else on the page. What it
does buy is that a message from the wrong frame, the wrong page or a previous
iframe load is recognised and dropped instead of acted on. Where a real secret
is needed - a server-to-server control path - that is a separate, server-only
value that never reaches a page; ``new_server_secret`` mints those and the
runtime holds one per launch.

Nothing in here is written to disk. A channel id, a bridge session, a runtime
instance id, a handoff id and a receiver list are all facts about one run of
one page, and the log-record builder below drops them on the floor precisely
because the send log is a file that outlives the run.
"""

from __future__ import annotations

import collections
import contextlib
import dataclasses
import re
import secrets
import threading
import time
import typing

from . import protocol
from .errors import (
    BRIDGE_SESSION_MISMATCH,
    BRIDGE_VERSION_MISMATCH,
    HANDOFF_DIGEST_MISMATCH,
    IFRAME_NOT_READY,
    INTERNAL_ERROR,
    RECEIVER_APPLY_FAILED,
    RECEIVER_DISABLED,
    RECEIVER_LIMIT_REACHED,
    RECEIVER_VERIFY_FAILED,
    STALE_RECEIVER_STATE,
    UNKNOWN_RECEIVER,
    WANGP_RESTARTED,
    IntegrationError,
    message,
)

PROTOCOL = protocol.PROTOCOL

#: A channel id is 32 lowercase hex characters, the same shape as a handoff
#: id, and for the same reason: a fixed shape cannot be talked into meaning
#: something else. Anything that is not exactly this is rejected, not repaired.
CHANNEL_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")

#: The bridge session and the runtime instance id are opaque to us - one is
#: minted inside WanGP, the other by ``runtime`` - so their shape is not ours
#: to dictate. Bounded length and a conservative character set is all we ask,
#: and both are still required rather than sanitised.
OPAQUE_ID_RE = re.compile(r"\A[A-Za-z0-9._:-]{1,128}\Z")

#: How long a page may say nothing before its session is forgotten. Long
#: enough that a user can leave a tab open over lunch and come back to a menu
#: that still works, short enough that a closed laptop does not keep a stale
#: picture of a model forever. Expiry is checked when a session is looked at:
#: there is no sweeper thread and nothing polls.
SESSION_IDLE_SECONDS = 30 * 60

#: A ceiling on remembered pages. Browsers do not always get to tell us a tab
#: closed, so the oldest idle session is evicted rather than allowed to
#: accumulate. Far more than anyone opens on purpose.
MAX_SESSIONS = 32

#: How many just-retired instance ids are remembered, so that a send caught by
#: a restart can say "WanGP restarted" instead of "that page is unknown". In
#: memory, bounded, never written down.
RETIRED_INSTANCES = 8

#: How long a send waits for the receiver it wants to stop being busy. An
#: append is a Gradio round trip, not a generation, so this is generous rather
#: than long.
SEND_LOCK_TIMEOUT = 60.0

# ------------------------------------------------------------ verification --

#: Section 23.3's vocabulary, strongest first. A file digest survives transport
#: but not necessarily Gradio's decode/re-encode, so "the bytes match" is the
#: best answer available rather than the only acceptable one.
BYTE_IDENTICAL = "byte-identical"
PIXEL_EQUIVALENT = "pixel-equivalent"
STRUCTURALLY_VERIFIED = "structurally verified"
#: Not a level: the absence of one. An acknowledgement that proves nothing
#: lands here, and here is a failure.
UNVERIFIED = "unverified"

VERIFICATION_LEVELS = (BYTE_IDENTICAL, PIXEL_EQUIVALENT, STRUCTURALLY_VERIFIED)

_VERIFICATION_RANK = {
    BYTE_IDENTICAL: 3,
    PIXEL_EQUIVALENT: 2,
    STRUCTURALLY_VERIFIED: 1,
    UNVERIFIED: 0,
}


def verification_rank(level: typing.Any) -> int:
    """How strong a verification level is. An unknown level is worth nothing."""
    return _VERIFICATION_RANK.get(level, 0)


def accepted(level: typing.Any, minimum: str = STRUCTURALLY_VERIFIED) -> bool:
    """Is this level good enough to call the send a success.

    The default floor is the weakest real level, because that is what the
    caller is entitled to assume by default: something was checked. A caller
    that wants more - a test, or a future "strict send" - raises the floor
    rather than reimplementing the comparison.
    """
    return verification_rank(level) >= verification_rank(minimum) > 0


# ----------------------------------------------------------------- helpers --


def _text(value: typing.Any, limit: int = 200) -> str:
    """One printable, bounded string. Everything crossing the bridge is input."""
    if not isinstance(value, str):
        return ""
    cleaned = "".join(character if character.isprintable() else " " for character in value).strip()
    return cleaned[:limit]


def _valid_channel_id(value: typing.Any) -> bool:
    return isinstance(value, str) and bool(CHANNEL_ID_RE.match(value))


def _valid_opaque_id(value: typing.Any) -> bool:
    return isinstance(value, str) and bool(OPAQUE_ID_RE.match(value))


def _digest(value: typing.Any) -> str:
    """A sha256 hex digest, or "" for anything that is not one."""
    if not isinstance(value, str):
        return ""
    candidate = value.strip().lower()
    return candidate if re.match(r"\A[0-9a-f]{64}\Z", candidate) else ""


def _dimension(value: typing.Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    number = int(value)
    return number if number > 0 else 0


def _count(value: typing.Any) -> typing.Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def normalize_model(raw: typing.Any) -> dict:
    """The model descriptor as the menu and the log will use it.

    Three strings and nothing else. The bridge is free to send more; whatever
    it sends is not carried into a session, because a field nobody reads is a
    field that turns up in a log one day.
    """
    raw = raw if isinstance(raw, dict) else {}
    return {
        "type": _text(raw.get("type"), 120),
        "label": _text(raw.get("label"), 120),
        "family": _text(raw.get("family"), 120),
    }


def new_channel_id() -> str:
    """A fresh channel id for one iframe load.

    ``secrets`` rather than ``random`` even though this is not a secret: the
    cost is nothing and the alternative is a value another page could predict
    and therefore address. See the module docstring for what this id is and,
    more importantly, what it is not.
    """
    return secrets.token_hex(16)


def new_server_secret() -> str:
    """A server-only value, for a control path no browser participates in.

    Kept next to ``new_channel_id`` on purpose, so that the difference between
    the two is visible in one place: this one never appears in a page, in a
    postMessage, in a log line or in a diagnostics report, and it is minted
    per run and never written down.
    """
    return secrets.token_urlsafe(32)


# ----------------------------------------------------------------- session --


@dataclasses.dataclass(frozen=True)
class Session:
    """What one browser page's WanGP iframe last told us.

    Frozen, and the receiver list is a tuple, because two threads read this
    while a third replaces it. An update is a new record swapped in under the
    registry's lock, never a field assignment: a half-updated session is a
    session that could send an image into a slot from a different state.
    """

    channel_id: str
    bridge_session: str = ""
    instance_id: str = ""
    state_revision: str = ""
    receivers: typing.Tuple[dict, ...] = ()
    model: typing.Optional[dict] = None
    updated: float = 0.0
    created: float = 0.0
    ready: bool = False
    view: str = ""
    bridge_version: str = ""
    bridge_protocol: int = PROTOCOL

    @property
    def usable(self) -> bool:
        """Ready, introduced, and bound to a run - all three or none."""
        return bool(self.ready and self.bridge_session and self.instance_id)

    def receiver(self, receiver_id: typing.Any) -> typing.Optional[dict]:
        """One receiver descriptor by id, copied, or None."""
        for descriptor in self.receivers:
            if descriptor.get("id") == receiver_id:
                return dict(descriptor)
        return None

    def receiver_list(self) -> typing.List[dict]:
        """The receivers as a fresh list of fresh dicts, safe to hand out."""
        return [dict(descriptor) for descriptor in self.receivers]

    def age(self, now: float) -> float:
        return max(0.0, now - self.updated)

    def expired(self, now: float, idle: float = SESSION_IDLE_SECONDS) -> bool:
        return self.age(now) >= idle

    def snapshot(self, now: typing.Optional[float] = None) -> dict:
        """Diagnostics. Ids are shortened; none of this is load-bearing.

        The channel id is browser-visible and the bridge session is not a
        credential, but neither belongs in a report someone pastes into an
        issue at full length, so both are cut to a recognisable prefix.
        """
        now = time.monotonic() if now is None else now
        model = self.model or {}
        return {
            "channel": self.channel_id[:8],
            "bridge_session": self.bridge_session[:8],
            "instance": self.instance_id[:8],
            "ready": self.ready,
            "view": self.view,
            "model_label": model.get("label", ""),
            "model_type": model.get("type", ""),
            "state_revision": self.state_revision[:8],
            "receivers": [descriptor.get("id", "") for descriptor in self.receivers],
            "enabled_receivers": [d.get("id", "") for d in self.receivers if d.get("enabled")],
            "bridge_protocol": self.bridge_protocol,
            "bridge_version": self.bridge_version,
            "idle_seconds": round(self.age(now), 1),
        }


# ---------------------------------------------------------------- registry --


class Registry:
    """Every live page's session, and nothing that outlives them.

    One instance is the process's, via ``registry()``; the class takes a clock
    so a test can age a session out without waiting half an hour, and so that
    nothing here has to reach for the wall clock at all. Monotonic time is used
    deliberately: a session must not expire because the machine woke up and the
    system clock jumped.
    """

    def __init__(self, clock: typing.Callable[[], float] = time.monotonic, idle: float = SESSION_IDLE_SECONDS) -> None:
        self._clock = clock
        self._idle = float(idle)
        self._lock = threading.RLock()
        self._sessions: typing.Dict[str, Session] = {}
        self._send_locks: typing.Dict[typing.Tuple[str, str], threading.Lock] = {}
        self._retired: typing.Deque[str] = collections.deque(maxlen=RETIRED_INSTANCES)

    # -- handshake ----------------------------------------------------------

    def hello(self, channel_id: typing.Any, instance_id: typing.Any) -> dict:
        """Start a channel, and forget whatever the last load of it said.

        A HELLO means an iframe just loaded. Whatever the previous load of the
        same channel believed about models and receivers is now the state of a
        page that no longer exists, so it goes rather than being merged: the
        one thing worse than not knowing the receiver list is knowing the
        previous one.

        The returned dict is the payload the parent puts in its HELLO envelope.
        """
        if not _valid_channel_id(channel_id):
            raise IntegrationError(IFRAME_NOT_READY, "channel id is not 32 hex characters")
        if not _valid_opaque_id(instance_id):
            raise IntegrationError(IFRAME_NOT_READY, "no runtime instance to bind this page to")

        now = self._clock()
        with self._lock:
            self._expire(now)
            self._forget_locks(channel_id)
            self._sessions[channel_id] = Session(
                channel_id=channel_id,
                instance_id=instance_id,
                updated=now,
                created=now,
            )
            self._evict(now)

        return {
            "protocol": PROTOCOL,
            "channel_id": channel_id,
            "instance_id": instance_id,
            "receiver_query_timeout_ms": protocol.RECEIVER_QUERY_TIMEOUT_MS,
            "receive_timeout_ms": protocol.RECEIVE_TIMEOUT_MS,
        }

    def ready(self, channel_id: typing.Any, payload: typing.Any) -> Session:
        """The iframe has introduced itself: record the session it named."""
        return self._record(channel_id, payload, introducing=True)

    def receivers(self, channel_id: typing.Any, payload: typing.Any) -> Session:
        """A receiver answer for a page we already know."""
        return self._record(channel_id, payload, introducing=False)

    def _record(self, channel_id: typing.Any, payload: typing.Any, introducing: bool) -> Session:
        if not _valid_channel_id(channel_id):
            raise IntegrationError(IFRAME_NOT_READY, "channel id is not 32 hex characters")
        payload = payload if isinstance(payload, dict) else {}

        # The version check is here rather than in the browser because a
        # mismatched plugin must fail as a *named* failure with a sentence
        # about updating it, not as a receiver list that happens to be empty.
        if payload.get("protocol") != PROTOCOL:
            raise IntegrationError(
                BRIDGE_VERSION_MISMATCH,
                f"bridge speaks protocol {payload.get('protocol')!r}, this build speaks {PROTOCOL}",
            )

        bridge_session = payload.get("bridge_session")
        if not _valid_opaque_id(bridge_session):
            raise IntegrationError(BRIDGE_SESSION_MISMATCH, "the bridge did not name its session")
        instance_id = payload.get("instance_id")
        if not _valid_opaque_id(instance_id):
            raise IntegrationError(WANGP_RESTARTED, "the bridge did not name a runtime instance")

        now = self._clock()
        with self._lock:
            self._expire(now)
            existing = self._sessions.get(channel_id)

            if existing is not None and existing.instance_id and existing.instance_id != instance_id:
                # The page is answering for a different child process than the
                # one it was introduced to. Nothing it says is about the run
                # this Forge is driving, so it is refused rather than adopted.
                raise IntegrationError(WANGP_RESTARTED, "this page belongs to a WanGP run that has ended")

            if not introducing:
                if existing is None or not existing.usable:
                    raise IntegrationError(IFRAME_NOT_READY, "no established session for this page")
                if existing.bridge_session != bridge_session:
                    raise IntegrationError(BRIDGE_SESSION_MISMATCH, "the WanGP page reloaded since the handshake")

            session = Session(
                channel_id=channel_id,
                bridge_session=bridge_session,
                instance_id=instance_id,
                state_revision=_text(payload.get("state_revision"), 128),
                receivers=tuple(protocol.normalize_receivers(payload.get("receivers"))),
                model=normalize_model(payload.get("model")),
                updated=now,
                created=existing.created if existing is not None else now,
                ready=bool(payload.get("ready", True)),
                view=_text(payload.get("view"), 60),
                bridge_version=_text(payload.get("bridge_version"), 40),
                bridge_protocol=PROTOCOL,
            )
            if introducing:
                # A second READY on the same channel is a reload of the same
                # iframe; the per-receiver locks belonged to the old load.
                self._forget_locks(channel_id)
            self._sessions[channel_id] = session
            self._evict(now)
            return session

    # -- lookup -------------------------------------------------------------

    def get(self, channel_id: typing.Any) -> typing.Optional[Session]:
        """The session for a page, or None. Idle sessions die when looked at."""
        if not _valid_channel_id(channel_id):
            return None
        now = self._clock()
        with self._lock:
            self._expire(now)
            return self._sessions.get(channel_id)

    def drop(self, channel_id: typing.Any) -> None:
        """Forget one page: it navigated away, or its iframe is being rebuilt."""
        if not isinstance(channel_id, str):
            return
        with self._lock:
            self._sessions.pop(channel_id, None)
            self._forget_locks(channel_id)

    def invalidate_instance(self, instance_id: str = "") -> None:
        """Drop every session bound to a run that is over.

        Called when the child starts, stops or crashes. An empty id means "all
        of them", which is what a stop with nothing running should still do:
        the cheapest way to be sure no page is holding a picture of a process
        is to have no pages at all.
        """
        with self._lock:
            if instance_id:
                gone = [key for key, session in self._sessions.items() if session.instance_id == instance_id]
                self._remember_retired(instance_id)
            else:
                gone = list(self._sessions)
                for session in self._sessions.values():
                    self._remember_retired(session.instance_id)
            for key in gone:
                self._sessions.pop(key, None)
                self._forget_locks(key)

    def retired(self, instance_id: typing.Any) -> bool:
        """Was this run invalidated recently, rather than simply unknown.

        The difference matters exactly once: a send that was in flight when the
        child restarted deserves "WanGP restarted", not "that page is unknown".
        """
        with self._lock:
            return isinstance(instance_id, str) and instance_id in self._retired

    def snapshot(self) -> dict:
        """What a diagnostics report may say about the live sessions."""
        now = self._clock()
        with self._lock:
            self._expire(now)
            sessions = [session.snapshot(now) for session in self._sessions.values()]
            held = sorted(f"{channel[:8]}/{receiver}" for (channel, receiver), lock in self._send_locks.items() if lock.locked())
        return {
            "protocol": PROTOCOL,
            "sessions": len(sessions),
            "idle_timeout_seconds": self._idle,
            "detail": sessions,
            "sends_in_flight": held,
        }

    # -- the gate -----------------------------------------------------------

    def check_send(self, channel_id: typing.Any, receiver_id: typing.Any, state_revision: typing.Any) -> dict:
        """May this image go to this receiver, at this revision. Nothing else.

        Every branch that is not "yes" raises, and no branch chooses a
        different destination. Refusing is the only behaviour available here:
        a menu entry that has gone stale is a question the user has to answer
        again, not a question this function is allowed to answer for them.
        """
        session = self.get(channel_id)
        if session is None or not session.usable:
            raise IntegrationError(IFRAME_NOT_READY, "no live WanGP session for this page")

        if receiver_id not in protocol.RECEIVER_IDS:
            # A receiver nobody has ever defined is wrong at every revision, so
            # it is named as such before the revision is even considered.
            raise IntegrationError(UNKNOWN_RECEIVER, f"no such logical receiver: {_text(receiver_id, 40)!r}")

        if not isinstance(state_revision, str) or not state_revision or state_revision != session.state_revision:
            raise IntegrationError(STALE_RECEIVER_STATE, "the receiver list this send was prepared from has moved on")

        descriptor = session.receiver(receiver_id)
        if descriptor is None:
            raise IntegrationError(UNKNOWN_RECEIVER, "this WanGP state does not offer that input")

        # Capacity before enablement, and not the other way round: a full
        # receiver is reported as disabled by the normaliser, so asking about
        # "enabled" first would tell a user their reference slot is inactive
        # when what it actually is, is full.
        if descriptor.get("full"):
            raise IntegrationError(RECEIVER_LIMIT_REACHED, "the receiver is at its maximum count")
        if not descriptor.get("enabled"):
            raise IntegrationError(RECEIVER_DISABLED, _text(descriptor.get("reason_code"), 60))

        return {
            "ok": True,
            "channel_id": session.channel_id,
            "bridge_session": session.bridge_session,
            "instance_id": session.instance_id,
            "state_revision": session.state_revision,
            "receiver": descriptor,
            "receiver_id": descriptor["id"],
            "role": descriptor.get("role", ""),
            "operation": descriptor.get("operation", ""),
            "model": dict(session.model or {}),
        }

    # -- after a send -------------------------------------------------------

    def settle(self, channel_id: typing.Any, ack: typing.Any) -> typing.Optional[Session]:
        """Take the state the bridge reports *after* it applied an image.

        The bridge is the authority on its own state, and it has just told us
        the new fingerprint and the new count. Recording them is not a cache of
        anything: it is the same authority speaking, one message later. It also
        has the effect that a second click on the still-open menu carries the
        old revision and is therefore refused, which is what section 17 wants.
        """
        ack = ack if isinstance(ack, dict) else {}
        revision = _text(ack.get("state_revision_after"), 128)
        receiver_id = ack.get("receiver_id")
        new_count = _count(ack.get("new_count"))

        with self._lock:
            session = self._sessions.get(channel_id) if isinstance(channel_id, str) else None
            if session is None:
                return None
            if ack.get("bridge_session") and ack.get("bridge_session") != session.bridge_session:
                return session

            receivers = list(session.receivers)
            if receiver_id in protocol.RECEIVER_IDS and new_count is not None:
                for index, descriptor in enumerate(receivers):
                    if descriptor.get("id") != receiver_id:
                        continue
                    updated = dict(descriptor)
                    updated["count"] = new_count
                    maximum = updated.get("max_count")
                    updated["full"] = isinstance(maximum, int) and new_count >= maximum
                    updated["enabled"] = bool(updated.get("enabled")) and not updated["full"]
                    receivers[index] = updated
                    break

            session = dataclasses.replace(
                session,
                state_revision=revision or session.state_revision,
                receivers=tuple(receivers),
                updated=self._clock(),
            )
            self._sessions[channel_id] = session
            return session

    # -- serialisation ------------------------------------------------------

    def send_lock(self, channel_id: typing.Any, receiver_id: typing.Any) -> threading.Lock:
        """The lock for one page's one receiver.

        Per (page, receiver) rather than global, because a start frame in one
        tab has nothing to serialise against a reference list in another, and a
        single global lock would make one slow append hold up every send in the
        browser.
        """
        key = (str(channel_id), str(receiver_id))
        with self._lock:
            lock = self._send_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._send_locks[key] = lock
            return lock

    # -- internals ----------------------------------------------------------

    def _expire(self, now: float) -> None:
        for key in [key for key, session in self._sessions.items() if session.expired(now, self._idle)]:
            self._sessions.pop(key, None)
            self._forget_locks(key)

    def _evict(self, now: float) -> None:
        while len(self._sessions) > MAX_SESSIONS:
            oldest = min(self._sessions.values(), key=lambda session: session.updated)
            self._sessions.pop(oldest.channel_id, None)
            self._forget_locks(oldest.channel_id)

    def _forget_locks(self, channel_id: str) -> None:
        # A held lock is left alone: the send holding it is still running, and
        # dropping the object would let the next send run beside it.
        for key in [key for key in self._send_locks if key[0] == channel_id and not self._send_locks[key].locked()]:
            self._send_locks.pop(key, None)

    def _remember_retired(self, instance_id: str) -> None:
        if instance_id and instance_id not in self._retired:
            self._retired.append(instance_id)


_registry: typing.Optional[Registry] = None
_registry_lock = threading.Lock()


def registry() -> Registry:
    """The process's registry. One object, many sessions - never one session."""
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = Registry()
        return _registry


def reset_for_tests() -> None:
    """Drop every session. Named for what it is; nothing else may call it."""
    global _registry
    with _registry_lock:
        _registry = None


def new_channel(instance_id: typing.Any, book: typing.Optional[Registry] = None) -> dict:
    """Mint a channel id and open it in one step, the way the tab wants it."""
    channel_id = new_channel_id()
    return (book or registry()).hello(channel_id, instance_id)


def check_send(channel_id: typing.Any, receiver_id: typing.Any, state_revision: typing.Any) -> dict:
    """Module-level gate; see ``Registry.check_send``."""
    return registry().check_send(channel_id, receiver_id, state_revision)


@contextlib.contextmanager
def serialised_send(
    channel_id: typing.Any,
    receiver_id: typing.Any,
    timeout: float = SEND_LOCK_TIMEOUT,
    book: typing.Optional[Registry] = None,
) -> typing.Iterator[None]:
    """Hold one receiver still for the whole of apply *and* verify.

    Section 35.2's race, concretely: two appends both read "2 references",
    both write "3", and one image is gone with an acknowledgement that says it
    arrived. The fix is not a bigger revision check - both sends have a valid
    revision at the moment they check - but exclusion: the second send must not
    begin until the first has applied its image *and* seen the result, because
    only then is the count it will read the truth.
    """
    lock = (book or registry()).send_lock(channel_id, receiver_id)
    if not lock.acquire(timeout=max(0.0, float(timeout))):
        raise IntegrationError(RECEIVER_APPLY_FAILED, "another send to this input is still running")
    try:
        yield
    finally:
        lock.release()


# ------------------------------------------------------------ verification --


def expectation(manifest: typing.Any, decision: typing.Any) -> dict:
    """What the acknowledgement has to agree with, in one flat dict.

    Pure, so the verifier can be tested without a registry: everything the
    verdict depends on is assembled here and nowhere else.
    """
    manifest = manifest if isinstance(manifest, dict) else {}
    decision = decision if isinstance(decision, dict) else {}
    return {
        "width": _dimension(manifest.get("width")),
        "height": _dimension(manifest.get("height")),
        "sha256": _digest(manifest.get("sha256")),
        "pixel_digest": _digest(manifest.get("pixel_digest")),
        "receiver_id": decision.get("receiver_id") or decision.get("receiver", {}).get("id", ""),
        "operation": decision.get("operation", ""),
        "bridge_session": decision.get("bridge_session", ""),
        "instance_id": decision.get("instance_id", ""),
    }


def verify_result(expected_manifest: typing.Any, ack: typing.Any) -> typing.Tuple[bool, str, str]:
    """Turn one bridge acknowledgement into a verdict.

    Returns ``(ok, level, code)``. The rule that decides every branch: an
    acknowledgement is a claim, and a claim that carries no evidence is not a
    pass. "ok: true" on its own proves that a message came back, which is the
    thing section 23 opens by saying is not enough, so it ends as
    ``RECEIVER_VERIFY_FAILED`` - the image may well have arrived, and the user
    is told exactly that rather than told it succeeded.
    """
    expected = expected_manifest if isinstance(expected_manifest, dict) else {}
    ack = ack if isinstance(ack, dict) else {}

    if not isinstance(expected_manifest, dict) or not isinstance(ack, dict):
        return False, UNVERIFIED, INTERNAL_ERROR

    # The bridge's own refusal wins: it knows why it would not apply the image,
    # and its code is more specific than anything derivable from the payload.
    if not ack.get("ok"):
        code = ack.get("code")
        return False, UNVERIFIED, code if isinstance(code, str) and code else RECEIVER_APPLY_FAILED

    # Identity before content. An acknowledgement from a different run or a
    # different page describes a slot this send never chose.
    if expected.get("instance_id") and ack.get("instance_id") and ack["instance_id"] != expected["instance_id"]:
        return False, UNVERIFIED, WANGP_RESTARTED
    if expected.get("bridge_session") and ack.get("bridge_session") and ack["bridge_session"] != expected["bridge_session"]:
        return False, UNVERIFIED, BRIDGE_SESSION_MISMATCH
    if expected.get("receiver_id") and ack.get("receiver_id") != expected["receiver_id"]:
        return False, UNVERIFIED, UNKNOWN_RECEIVER
    if expected.get("operation") and ack.get("operation") and ack["operation"] != expected["operation"]:
        # A replace where an append was agreed would have destroyed the user's
        # other references; it is a failure even though an image arrived.
        return False, UNVERIFIED, RECEIVER_VERIFY_FAILED

    # The file that reached WanGP must be the file that left, if the bridge
    # says which one it read. This is transport integrity, not verification.
    source_digest = _digest(ack.get("source_digest"))
    if expected.get("sha256") and source_digest and source_digest != expected["sha256"]:
        return False, UNVERIFIED, HANDOFF_DIGEST_MISMATCH

    width, height = _dimension(ack.get("width")), _dimension(ack.get("height"))
    if not width or not height:
        return False, UNVERIFIED, RECEIVER_VERIFY_FAILED
    if expected.get("width") and expected.get("height"):
        if (width, height) != (expected["width"], expected["height"]):
            return False, UNVERIFIED, RECEIVER_VERIFY_FAILED

    receiver_digest = _digest(ack.get("receiver_digest"))
    if receiver_digest and expected.get("sha256") and receiver_digest == expected["sha256"]:
        return True, BYTE_IDENTICAL, ""

    source_pixels = _digest(ack.get("source_pixel_digest")) or expected.get("pixel_digest", "")
    receiver_pixels = _digest(ack.get("receiver_pixel_digest"))
    if source_pixels and receiver_pixels:
        if source_pixels == receiver_pixels:
            return True, PIXEL_EQUIVALENT, ""
        # Both sides decoded an image and the pixels differ. That is a
        # destination holding something else, which is worse than no evidence.
        return False, UNVERIFIED, RECEIVER_VERIFY_FAILED

    # No digest survived. The remaining evidence is that the receiver's own
    # contents changed in the way the agreed operation says they should.
    new_count = _count(ack.get("new_count"))
    previous_count = _count(ack.get("previous_count"))
    if expected.get("operation") == protocol.APPEND:
        if new_count is not None and previous_count is not None and new_count == previous_count + 1:
            return True, STRUCTURALLY_VERIFIED, ""
    elif new_count is None or new_count >= 1:
        if ack.get("receiver_present") is True:
            return True, STRUCTURALLY_VERIFIED, ""

    return False, UNVERIFIED, RECEIVER_VERIFY_FAILED


def send_verified(
    channel_id: typing.Any,
    receiver_id: typing.Any,
    state_revision: typing.Any,
    manifest: typing.Any,
    apply: typing.Callable[[dict], typing.Any],
    timeout: float = SEND_LOCK_TIMEOUT,
    book: typing.Optional[Registry] = None,
    clock: typing.Callable[[], float] = time.monotonic,
) -> dict:
    """Gate, apply and verify one image, with the receiver held throughout.

    ``apply`` is the only thing here that touches the outside world - it is
    handed the decision and returns the bridge's acknowledgement - which is
    what makes this whole path testable with a function that returns a dict.

    The session is re-read after the acknowledgement on purpose. Section 35.4:
    if the child restarted while the image was in flight, the send is refused
    after the fact rather than reported as a success, because the slot it
    describes belongs to a process that is gone.
    """
    book = book or registry()
    started = clock()
    with serialised_send(channel_id, receiver_id, timeout=timeout, book=book):
        waited = clock() - started
        decision = book.check_send(channel_id, receiver_id, state_revision)
        expected = expectation(manifest, decision)

        applied_at = clock()
        ack = apply(decision)
        apply_seconds = clock() - applied_at

        verified_at = clock()
        ok, level, code = verify_result(expected, ack)
        verify_seconds = clock() - verified_at

        if ok:
            live = book.get(channel_id)
            if live is None or live.instance_id != decision["instance_id"]:
                ok, level, code = False, UNVERIFIED, WANGP_RESTARTED
            elif live.bridge_session != decision["bridge_session"]:
                ok, level, code = False, UNVERIFIED, BRIDGE_SESSION_MISMATCH
            else:
                book.settle(channel_id, ack if isinstance(ack, dict) else {})

    return {
        "ok": ok,
        "verification": level,
        "code": code,
        "decision": decision,
        "expected": expected,
        "ack": ack if isinstance(ack, dict) else {},
        "latency": {
            "send_wait": round(waited * 1000.0),
            "bridge_receive": round(apply_seconds * 1000.0),
            "receiver_verify": round(verify_seconds * 1000.0),
        },
    }


# ----------------------------------------------------------------- logging --

#: The only keys that may reach the send log, in the order section 24 lists
#: them. A whitelist rather than a blacklist, because the caller assembles this
#: dict from a decision, a manifest and an acknowledgement, and any of those
#: could grow a field later that nobody meant to write to a file.
LOGGED_FIELDS = (
    "destination",
    "runtime_state",
    "bridge_protocol",
    "bridge_version",
    "model_label",
    "model_type",
    "receiver_id",
    "receiver_role",
    "operation",
    "state_revision",
    "width",
    "height",
    "verification",
    "latency",
    "error_code",
    "error_detail",
)

#: Named so the reason is not folklore. Two kinds of thing are in here: what
#: section 24 forbids outright (the bridge secret, cookies, arbitrary headers,
#: the picture itself), and what section 9 says never survives a run - a pid, a
#: port, a channel id, a bridge session, a runtime instance, a handoff id.
#: The send log is a file that outlives the run, so those belong in neither.
FORBIDDEN_FIELDS = frozenset(
    {
        "secret",
        "bridge_secret",
        "token",
        "cookie",
        "cookies",
        "headers",
        "authorization",
        "password",
        "session_hash",
        "channel_id",
        "bridge_session",
        "instance_id",
        "handoff_id",
        "handoff_path",
        "path",
        "port",
        "pid",
        "image",
        "image_data",
        "pixels",
        "contents",
        "file_contents",
    }
)

#: The latency steps section 24 shows, with the labels it uses, in the order a
#: send performs them. A step the caller did not measure is simply absent.
LATENCY_STEPS = (
    ("receiver_query", "receiver query"),
    ("export", "flatten/export"),
    ("handoff_write", "handoff write"),
    ("send_wait", "send wait"),
    ("bridge_receive", "bridge receive"),
    ("receiver_verify", "receiver verify"),
)


def redact(record: typing.Any) -> dict:
    """Keep the fields section 24 asks for, drop every other one.

    Both directions are applied: the whitelist decides what stays, and the
    forbidden set is checked as well so that adding a name to ``LOGGED_FIELDS``
    can never quietly re-admit a secret. A test can hand this a dict full of
    cookies and assert the result is empty of them.
    """
    record = record if isinstance(record, dict) else {}
    kept: dict = {}
    for field in LOGGED_FIELDS:
        if field in FORBIDDEN_FIELDS or field not in record:
            continue
        value = record[field]
        if value is None or value == "":
            continue
        kept[field] = value
    return kept


def format_latency(latency: typing.Any) -> typing.List[str]:
    """The optional detailed steps, as lines. Milliseconds, integers, ordered."""
    latency = latency if isinstance(latency, dict) else {}
    lines = []
    for key, label in LATENCY_STEPS:
        value = latency.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        lines.append(f"{label}: {int(round(value))} ms")
    return lines


def build_log_record(event: typing.Any) -> dict:
    """One transfer, shaped for ``minipaint_neo.send_log.log_quietly``.

    ``send_log`` renders ``destination``, ``outcome`` and ``steps`` and nothing
    else, so those three are what this builds; ``fields`` rides along redacted
    for whoever reads the record programmatically, and is invisible in the
    file. The failure sentence comes from the code's entry in ``errors``, never
    from an exception's text, so the log and the screen say the same thing.
    """
    fields = redact(event)

    destination = _text(fields.get("destination"), 120) or "WanGP"
    verification = _text(fields.get("verification"), 40)
    error_code = _text(fields.get("error_code"), 60)
    width, height = _dimension(fields.get("width")), _dimension(fields.get("height"))
    receiver = _text(fields.get("receiver_id"), 40)
    operation = _text(fields.get("operation"), 20)

    if error_code:
        outcome = f"failed: {error_code} - {message(error_code)}"
    else:
        size = f"{width}x{height}" if width and height else "image"
        into = f" into {receiver}" if receiver else ""
        how = f" ({operation})" if operation else ""
        outcome = f"{verification or UNVERIFIED} - {size}{into}{how}"

    steps: typing.List[str] = []
    runtime_state = _text(fields.get("runtime_state"), 40)
    if runtime_state:
        steps.append(f"runtime: {runtime_state}")
    bridge_version = _text(fields.get("bridge_version"), 40)
    steps.append(f"bridge: protocol {fields.get('bridge_protocol', PROTOCOL)}" + (f", plugin {bridge_version}" if bridge_version else ""))
    model_label = _text(fields.get("model_label"), 120)
    model_type = _text(fields.get("model_type"), 120)
    if model_label or model_type:
        steps.append(f"model: {model_label or model_type}" + (f" ({model_type})" if model_label and model_type else ""))
    role = _text(fields.get("receiver_role"), 40)
    if receiver:
        steps.append(f"receiver: {receiver}" + (f" / {role}" if role else "") + (f", {operation}" if operation else ""))
    revision = _text(fields.get("state_revision"), 128)
    if revision:
        steps.append(f"state revision: {revision}")
    steps.extend(format_latency(fields.get("latency")))
    detail = _text(fields.get("error_detail"), 200)
    if detail:
        steps.append(f"detail: {detail}")
    if not error_code and verification:
        steps.append(f"final: {verification}")

    return {"destination": destination, "outcome": outcome, "steps": steps, "fields": fields}


def log_transfer(event: typing.Any, logger: typing.Optional[typing.Callable[[dict], None]] = None) -> dict:
    """Write one transfer, and never let writing it be why a send failed.

    ``logger`` is the seam: the default is the extension's own quiet appender,
    a test passes a list's ``append``.
    """
    record = build_log_record(event)
    if logger is None:
        try:
            from ..send_log import log_quietly as logger  # type: ignore[assignment]
        except Exception:
            return record
    try:
        logger(record)
    except Exception:
        pass
    return record
