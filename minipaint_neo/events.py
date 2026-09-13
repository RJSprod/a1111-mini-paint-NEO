"""The event spine: what changed, said once, to the pages that care.

Every integration in this extension used to find out that the server's state
had moved by asking it again a few times a second. That is slower than being
told - a poll's mean latency is half its interval - and it costs a request per
tick per page whether or not anything happened. The server is the thing that
changes the state, so the server is in a position to say so, and this is where
it says it.

Three rules shape everything here.

**Events are advisory; the snapshot is authoritative.** An event says
"something about job X changed" and carries enough to update a screen quickly.
It is never a second copy of the truth. A page that has any reason to doubt
what it holds asks for a snapshot instead, and that is a normal thing to do
rather than a failure - see ``sync`` in the interop routes.

**A cursor is namespaced by the process that issued it.** A bare counter would
reset when Forge restarts, and a browser that had been asleep would come back
holding a number larger than the one the new process is issuing - and would be
handed events it had already seen, or none at all, with no way to tell. So a
cursor is ``<epoch>:<revision>``: the epoch is minted once per process, and a
cursor from another epoch is not stale, it is meaningless, and says so.

**Replay is bounded.** The ring holds the recent past so a page that blinked
can catch up without a snapshot. A page that has been gone longer than the
ring is told to resync rather than handed an unbounded replay.

Targeted advisory events are the one thing that does not follow the durable
path. ``claim_ready`` tells one page it may now have work to claim; it grants
nothing, it is recomputed from current state whenever a page connects or
syncs, and replaying a stale opportunity would only produce a wasted request.
So it goes to its page and nowhere else: not into the ring, not through the
revision, and with no id for a browser to resume from.

Nothing here writes to a socket, and nothing here blocks. ``publish`` is
called from whatever thread changed the state - usually one holding another
module's lock - so it does the least possible: takes the next revision, puts a
record in the ring, and wakes each subscriber through its own event loop. The
waking is what the loop does next, not what the caller waits for.
"""

from __future__ import annotations

import asyncio
import collections
import itertools
import secrets
import threading
import time
import typing

#: How much of the recent past a page can be replayed from. A page that has
#: been away longer resyncs, which is one request and always correct.
RING_LIMIT = 512

#: What one subscriber may fall behind by before it is given up on. A browser
#: this far behind is not reading; it is told to resync when it comes back.
QUEUE_LIMIT = 256

#: A page that has gone quiet is still that page's for this long. A remote
#: browser loses its connection for a second at a time - a Wi-Fi roam, a VPN
#: renegotiation - and its work must not be reordered because of it.
PAGE_ACTIVE_SECONDS = 15.0

#: Durable broadcasts. Each advances the revision and enters the ring.
JOB = "job"
ENHANCE = "enhance"
HANDOFF = "handoff"
RUNTIME = "runtime"
WANGP = "wangp"
#: Continuity, not state: told to one page, never replayed.
RESET = "reset"
HEARTBEAT = "heartbeat"
#: Advisory and targeted. Carries no lease and no revision.
CLAIM_READY = "claim_ready"

DURABLE = (JOB, ENHANCE, HANDOFF, RUNTIME, WANGP)
ADVISORY = (CLAIM_READY,)

_lock = threading.RLock()
#: Not a secret and not an identifier of anything: a label that tells one run
#: of this process from the next, so a cursor cannot be read across a restart.
_epoch = secrets.token_hex(8)
_revision = 0
_ring: typing.Deque[dict] = collections.deque(maxlen=RING_LIMIT)
_subscribers: typing.Dict[int, dict] = {}
_ids = itertools.count(1)
#: When each page was last seen holding a transport open, and when it let go.
_pages: typing.Dict[str, dict] = {}
_clock: typing.Callable[[], float] = time.time


def _now() -> float:
    return float(_clock())


def use_clock(clock: typing.Optional[typing.Callable[[], float]]) -> None:
    """Test seam. None restores the real clock."""
    global _clock
    _clock = clock if clock is not None else time.time


def reset_for_tests(epoch: typing.Optional[str] = None) -> None:
    """Forget everything, optionally as a new process would. Tests only."""
    global _epoch, _revision
    with _lock:
        if epoch is not None:
            _epoch = str(epoch)
        _revision = 0
        _ring.clear()
        _subscribers.clear()
        _pages.clear()


# ------------------------------------------------------------- the cursor --


def epoch() -> str:
    return _epoch


def revision() -> int:
    with _lock:
        return _revision


def cursor(rev: typing.Optional[int] = None) -> str:
    """``<epoch>:<revision>`` - what an SSE ``id:`` carries."""
    with _lock:
        return f"{_epoch}:{_revision if rev is None else int(rev)}"


def parse_cursor(text: typing.Any) -> typing.Optional[typing.Tuple[str, int]]:
    """A cursor as (epoch, revision), or None when it is not one.

    Forgiving on purpose: this reads a query string and a ``Last-Event-ID``
    header, both of which a browser, a proxy or a person may have mangled. An
    unreadable cursor is not an error, it is a page that needs a snapshot.
    """
    if not isinstance(text, str) or ":" not in text:
        return None
    head, _, tail = text.partition(":")
    if not head or not tail.isdigit():
        return None
    return head, int(tail)


# ---------------------------------------------------------------- publish --


def _record(kind: str, payload: typing.Optional[dict], rev: typing.Optional[int]) -> dict:
    return {
        "kind": str(kind),
        "revision": rev,
        "payload": dict(payload) if isinstance(payload, dict) else {},
        "at": _now(),
    }


def _wake(entry: dict, record: dict) -> None:
    """Hand one record to one subscriber, on the loop that owns its queue.

    Never blocks and never raises into the caller: the caller is whatever
    thread just changed the state, frequently holding another module's lock,
    and a subscriber that has gone away or fallen behind is not that thread's
    problem. A queue that is full has a reader that is not reading, so it is
    marked for a reset and told once rather than waited for.
    """
    loop = entry.get("loop")
    queue = entry.get("queue")
    if loop is None or queue is None or entry.get("closed"):
        return

    def deliver() -> None:
        try:
            if entry.get("closed"):
                return
            if queue.qsize() >= QUEUE_LIMIT:
                entry["overflowed"] = True
                return
            queue.put_nowait(record)
        except Exception:
            entry["overflowed"] = True

    try:
        loop.call_soon_threadsafe(deliver)
    except RuntimeError:
        # The loop has closed; the subscription's own cleanup removes it.
        entry["closed"] = True


def publish(kind: str, payload: typing.Optional[dict] = None) -> int:
    """A durable change, to every page. Returns the revision it was given.

    Called from whatever thread made the change, including one holding the
    outbox lock, so it must stay cheap: a counter, a deque append, and one
    ``call_soon_threadsafe`` per subscriber.

    The payload is a description, not a record: ids and states only. No
    prompt, no filename, no path, no session hash and no secret ever goes into
    one - the rule the rest of this extension's writers follow, and the one
    ``test_logging_privacy`` holds these to as well.
    """
    global _revision
    with _lock:
        _revision += 1
        record = _record(kind, payload, _revision)
        _ring.append(record)
        listeners = list(_subscribers.values())
        rev = _revision
    for entry in listeners:
        _wake(entry, record)
    return rev


def notify(page: typing.Any, kind: str, payload: typing.Optional[dict] = None) -> None:
    """An advisory event, to one page only.

    No revision, no ring, no id. ``claim_ready`` is the whole of this today:
    it says an opportunity may exist, the page asks the claim route, and the
    server decides under its own lock. Replaying one would mean re-offering an
    opportunity that has certainly been taken, so it is never replayed - a
    page that reconnects recomputes eligibility instead.
    """
    name = str(page or "")
    if not name:
        return
    record = _record(kind, payload, None)
    with _lock:
        listeners = [entry for entry in _subscribers.values() if entry.get("page") == name]
    for entry in listeners:
        _wake(entry, record)


# ----------------------------------------------------------------- replay --


def replay(text: typing.Any) -> typing.Tuple[typing.List[dict], bool]:
    """What a page holding ``text`` has missed, and whether it must resync.

    (records, reset). ``reset`` true means the cursor cannot be honoured and
    the page is to take a snapshot: a different epoch, a revision this process
    has not issued, or one older than the ring still holds.
    """
    parsed = parse_cursor(text)
    with _lock:
        if parsed is None:
            return [], True
        seen_epoch, seen = parsed
        if seen_epoch != _epoch or seen > _revision:
            return [], True
        if seen == _revision:
            return [], False
        oldest = _ring[0]["revision"] if _ring else None
        if oldest is None or seen + 1 < oldest:
            return [], True
        return [record for record in _ring if (record["revision"] or 0) > seen], False


# --------------------------------------------------------------- presence --


def connected(page: typing.Any) -> None:
    """This page holds a transport open, so it is present."""
    name = str(page or "")
    if not name:
        return
    with _lock:
        _pages[name] = {"since": _now(), "left": None}


def disconnected(page: typing.Any) -> None:
    """This page's transport went away. It stays present for the grace period."""
    name = str(page or "")
    if not name:
        return
    with _lock:
        entry = _pages.get(name)
        if entry is not None:
            entry["left"] = _now()


def active(page: typing.Any) -> bool:
    """Whether this page counts as present for queue-order purposes.

    A page inside the grace period is still active. That grace is what stops a
    one-second network hiccup on a remote browser from handing its turn to
    somebody else and reordering the line.
    """
    name = str(page or "")
    with _lock:
        entry = _pages.get(name)
        if entry is None:
            return False
        left = entry.get("left")
        if left is None:
            return True
        return (_now() - float(left)) <= PAGE_ACTIVE_SECONDS


def active_pages() -> typing.List[str]:
    with _lock:
        names = list(_pages)
    return [name for name in names if active(name)]


def forget(page: typing.Any) -> None:
    """Drop a page's presence entirely. For tests and for a page that said goodbye."""
    with _lock:
        _pages.pop(str(page or ""), None)


# ------------------------------------------------------------ subscribing --


class Subscription:
    """One page's live feed. Async-iterable, and cleans up after itself.

    The queue belongs to the loop that created it, which is why the loop is
    captured here rather than looked up at publication time: ``publish`` runs
    on a worker thread and must not go looking for a running loop it is not on.
    """

    def __init__(self, page: str) -> None:
        self.page = page
        self.id = next(_ids)
        self.queue: "asyncio.Queue[dict]" = asyncio.Queue()
        self._entry = {
            "page": page,
            "queue": self.queue,
            "loop": asyncio.get_running_loop(),
            "closed": False,
            "overflowed": False,
        }

    def __enter__(self) -> "Subscription":
        with _lock:
            _subscribers[self.id] = self._entry
        connected(self.page)
        return self

    def __exit__(self, *_exc: typing.Any) -> None:
        self.close()

    @property
    def overflowed(self) -> bool:
        """True once this subscriber fell far enough behind to need a resync."""
        return bool(self._entry.get("overflowed"))

    def close(self) -> None:
        self._entry["closed"] = True
        with _lock:
            _subscribers.pop(self.id, None)
        disconnected(self.page)

    async def next(self, timeout: typing.Optional[float] = None) -> typing.Optional[dict]:
        """The next record, or None when ``timeout`` passed with nothing.

        None is how a caller knows to send a heartbeat: the point of the
        heartbeat is that a silent connection and a dead one look identical to
        a remote browser until something arrives on it.
        """
        if timeout is None:
            return await self.queue.get()
        try:
            return await asyncio.wait_for(self.queue.get(), timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return None


def subscribe(page: typing.Any) -> Subscription:
    """A feed for one page. Use it as a context manager so it always unregisters."""
    return Subscription(str(page or ""))


def subscriber_count() -> int:
    with _lock:
        return len(_subscribers)


__all__ = [
    "ADVISORY", "CLAIM_READY", "DURABLE", "ENHANCE", "HANDOFF", "HEARTBEAT", "JOB",
    "PAGE_ACTIVE_SECONDS", "QUEUE_LIMIT", "RESET", "RING_LIMIT", "RUNTIME", "WANGP",
    "Subscription", "active", "active_pages", "connected", "cursor", "disconnected",
    "epoch", "forget", "notify", "parse_cursor", "publish", "replay", "reset_for_tests",
    "revision", "subscribe", "subscriber_count", "use_clock",
]
