"""What the bridge wrote into the live form for a queue request, what was
there before, and what has been proved about it since.

A queue request is an *overlay*: the caller's prompt and images go into the
page for exactly one queued task and then the page goes back to what it was.
That last half is where the honesty lives. Between the write and the restore
WanGP's own queue chain reads the form, and the user may touch it too, so the
record below keeps three things per changed component: the value that was
there, the value the bridge wrote, and - for a gallery - the pixel digests of
what it wrote. Restoration compares the live value with the written one and
puts the original back only when they still agree. A component the user
edited in between is left alone, and the journal says so.

The ledger is per bridge session, bounded and expiring, and holds no prompt
text once a record is terminal: the original and the written prompt exist
only while a restore may still need them, and ``forget_private`` drops them
the moment it has run. The idempotency key is a digest of the payload, never
the payload.

Pure data and pure functions: nothing here reads a component or touches
Gradio, so all of it runs under a test with a dictionary of live values.
"""

from __future__ import annotations

import collections
import dataclasses
import time
import typing

try:
    from . import compatibility, handoff, protocol, receiver_adapters, receiver_state
except ImportError:  # pragma: no cover - depends on how WanGP imports plugins
    import compatibility  # type: ignore[no-redef]
    import handoff  # type: ignore[no-redef]
    import protocol  # type: ignore[no-redef]
    import receiver_adapters  # type: ignore[no-redef]
    import receiver_state  # type: ignore[no-redef]


#: The component keys whose values are letter flags, compared as sets of
#: letters: WanGP's own handlers may rewrite "SE" as "ES" after a switch,
#: and that is the same selection.
LETTER_KEYS = (compatibility.IMAGE_PROMPT_TYPE, compatibility.VIDEO_PROMPT_TYPE)

#: The component keys that hold pictures, compared by what they show.
GALLERY_KEYS = (compatibility.START_IMAGE, compatibility.END_IMAGE, compatibility.REFERENCE_GALLERY)

#: Components restored together or not at all: a selector and the letter
#: string generation reads from it are one choice, and putting one back
#: without the other would leave the page saying two different things.
RESTORE_GROUPS: typing.Tuple[typing.Tuple[str, ...], ...] = (
    (compatibility.IMAGE_PROMPT_RADIO, compatibility.END_IMAGES_CHECKBOX, compatibility.IMAGE_PROMPT_TYPE),
    (compatibility.REFERENCE_SELECTOR, compatibility.VIDEO_PROMPT_TYPE),
)

#: How many gallery entries are decoded for a comparison. A request supplies
#: at most MAX_QUEUE_REFERENCES, so this is the same bound.
MAX_COMPARED_ENTRIES = protocol.MAX_QUEUE_REFERENCES


@dataclasses.dataclass
class PendingAdmission:
    """One queue request the bridge has written into the live form."""

    request_id: str
    bridge_session: str
    payload_hash: str
    created_at: float
    status: str = protocol.QUEUE_PENDING
    #: Once a confirmation has seen a matching task, this never goes back:
    #: a fast task may leave the queue before the next confirmation.
    seen_queued: bool = False
    tasks_added_max: int = 0
    code: str = ""
    #: The live value of each component this request changed, before it did.
    originals: typing.Dict[str, typing.Any] = dataclasses.field(default_factory=dict)
    #: What the bridge wrote into each of them.
    written: typing.Dict[str, typing.Any] = dataclasses.field(default_factory=dict)
    #: For the gallery components: the pixel digests of what was written.
    written_digests: typing.Dict[str, typing.List[str]] = dataclasses.field(default_factory=dict)
    #: applied / inherited / ignored, as the answer reports them.
    summary: typing.Dict[str, typing.Any] = dataclasses.field(default_factory=dict)
    model: typing.Dict[str, str] = dataclasses.field(default_factory=dict)
    terminal_at: float = 0.0
    #: Whether compare-before-restore has run for this record.
    restored: bool = False
    #: Which components were put back, and which were left because the page
    #: had moved on. Names only, never values.
    restored_keys: typing.List[str] = dataclasses.field(default_factory=list)
    skipped_keys: typing.List[str] = dataclasses.field(default_factory=list)

    @property
    def terminal(self) -> bool:
        return self.status != protocol.QUEUE_PENDING

    def expired(self, now: float) -> bool:
        return not self.terminal and (now - self.created_at) > protocol.PENDING_ADMISSION_SECONDS

    def settle(self, status: str, code: str, now: float) -> None:
        """Make the record terminal. A queued record stays queued."""
        if self.terminal:
            return
        if self.seen_queued:
            status, code = protocol.QUEUE_QUEUED, ""
        self.status = status
        self.code = code
        self.terminal_at = now

    def forget_private(self) -> None:
        """Drop the values. Only the names of what was changed survive."""
        self.originals = {}
        self.written = {}
        self.written_digests = {}

    def answer(self) -> dict:
        """The fields a result or a status answer carries about this record."""
        out = {
            "queue_request_id": self.request_id,
            "status": self.status,
            "tasks_added": self.tasks_added_max,
            "model": dict(self.model),
        }
        out.update(self.summary)
        if self.code:
            out["code"] = self.code
        if self.restored:
            out["restored"] = list(self.restored_keys)
            out["restore_skipped"] = list(self.skipped_keys)
        return out


class Ledger:
    """Every page's pending and recent admissions, bounded, expiring.

    Keyed by bridge session so that two browser pages cannot see, own or
    restore each other's overlays. Takes its clock so a test can age a record
    without waiting; monotonic so a system clock jump cannot expire one.
    """

    def __init__(self, clock: typing.Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._sessions: typing.Dict[str, "collections.OrderedDict[str, PendingAdmission]"] = {}

    def now(self) -> float:
        return self._clock()

    def get(self, bridge_session: str, request_id: str) -> typing.Optional[PendingAdmission]:
        records = self._sessions.get(bridge_session)
        if not records:
            return None
        self._prune(bridge_session, self._clock())
        return records.get(request_id)

    def owner(self, bridge_session: str) -> typing.Optional[PendingAdmission]:
        """The record that currently owns the live form, if any.

        At most one per session is ever pending: that invariant is what the
        per-session serialisation of section 14.1 rests on, and ``add``
        refuses to break it.
        """
        for record in (self._sessions.get(bridge_session) or {}).values():
            if not record.terminal:
                return record
        return None

    def add(self, record: PendingAdmission) -> None:
        current = self.owner(record.bridge_session)
        if current is not None and current.request_id != record.request_id:
            raise compatibility.BridgeError(
                compatibility.QUEUE_BUSY, f"{current.request_id[:8]} still owns the live form"
            )
        records = self._sessions.setdefault(record.bridge_session, collections.OrderedDict())
        records[record.request_id] = record
        self._prune(record.bridge_session, self._clock())

    def pending(self, bridge_session: str) -> typing.List[PendingAdmission]:
        return [record for record in (self._sessions.get(bridge_session) or {}).values() if not record.terminal]

    def _prune(self, bridge_session: str, now: float) -> None:
        records = self._sessions.get(bridge_session)
        if not records:
            return
        for key in [key for key, record in records.items()
                    if record.terminal and now - record.terminal_at > protocol.ADMISSION_RECORD_SECONDS]:
            records.pop(key, None)
        while len(records) > protocol.MAX_ADMISSION_RECORDS:
            # The oldest terminal record goes first; a pending one is only
            # evicted when nothing else is left to evict.
            victim = next((key for key, record in records.items() if record.terminal), None)
            if victim is None:
                victim = next(iter(records))
            records.pop(victim, None)
        if not records:
            self._sessions.pop(bridge_session, None)


# ----------------------------------------------------------- comparisons --


def letters(value: typing.Any) -> str:
    """A letter-flag string as a comparable value: its letters, sorted."""
    return "".join(sorted(set(receiver_state.selection_text(value))))


def gallery_digests(value: typing.Any) -> typing.List[str]:
    """What a gallery shows, as pixel digests, entry by entry.

    A gallery value comes back from Gradio as file paths in its own cache,
    never as the objects the bridge wrote, so identity and equality say
    nothing; the pixels do. Bounded, and an entry that will not decode is an
    empty digest, which matches nothing.
    """
    entries = receiver_adapters.gallery_entries(value)[:MAX_COMPARED_ENTRIES]
    return [handoff.pixel_digest(receiver_adapters.as_image(entry)) for entry in entries]


def unchanged(record: PendingAdmission, key: str, live: typing.Mapping[str, typing.Any]) -> bool:
    """Whether the live value of ``key`` is still what the bridge wrote."""
    current = live.get(key)
    written = record.written.get(key)
    if key in GALLERY_KEYS:
        return gallery_digests(current) == list(record.written_digests.get(key) or [])
    if key in LETTER_KEYS:
        return letters(current) == letters(written)
    return receiver_state.selection_text(current) == receiver_state.selection_text(written)


def restore_plan(
    record: PendingAdmission, live: typing.Mapping[str, typing.Any]
) -> typing.Tuple[typing.Dict[str, typing.Any], typing.List[str], typing.List[str]]:
    """``(updates, restored, skipped)``: what to put back, and what not to.

    Compare before restore, per component or per group: a value the bridge
    wrote and that is still there goes back to its original; a value the
    user changed since is theirs now and is left as it is. A group - a
    selector with its letter string - is restored whole or not at all.
    """
    updates: typing.Dict[str, typing.Any] = {}
    restored: typing.List[str] = []
    skipped: typing.List[str] = []
    changed = set(record.written)
    grouped: typing.Set[str] = set()

    for group in RESTORE_GROUPS:
        members = [key for key in group if key in changed]
        if not members:
            continue
        grouped.update(members)
        if all(unchanged(record, key, live) for key in members):
            for key in members:
                updates[key] = record.originals.get(key)
                restored.append(key)
        else:
            skipped.extend(members)

    for key in sorted(changed - grouped):
        if unchanged(record, key, live):
            updates[key] = record.originals.get(key)
            restored.append(key)
        else:
            skipped.append(key)
    return updates, restored, skipped


# --------------------------------------------------------------- the queue --


def matching_tasks(gen: typing.Any, request_id: str) -> int:
    """How many tasks in WanGP's queue carry this request as their client id.

    ``save_inputs`` copies ``client_id`` into every task's params, which is
    what makes a positive observation possible at all. A task naming another
    client, or none, is somebody else's.
    """
    if not isinstance(gen, dict):
        return 0
    queue = gen.get("queue")
    if not isinstance(queue, (list, tuple)):
        return 0
    count = 0
    for task in queue:
        if not isinstance(task, dict):
            continue
        params = task.get("params")
        client = params.get("client_id") if isinstance(params, dict) else None
        if client is None:
            client = task.get("client_id")
        if client == request_id:
            count += 1
    return count


def refusal_evidence(gen: typing.Any, request_id: str) -> bool:
    """Whether WanGP recorded a rejection for exactly this request.

    Only evidence tied to the client id counts: a ``queue_errors`` entry
    keyed by it, or one that names it. The absence of a task is never
    refusal - a busy WanGP may simply not have run its chain yet.

    VERIFY ON A REAL INSTALL: the shape of ``queue_errors`` in the target
    Wan2GP build. Both shapes seen in discussion are read; a build with
    neither leaves every failed validation "unconfirmed", which is the honest
    answer rather than the wrong one.
    """
    if not isinstance(gen, dict):
        return False
    errors = gen.get("queue_errors")
    if isinstance(errors, dict):
        return bool(errors.get(request_id))
    if isinstance(errors, (list, tuple)):
        for item in errors:
            if isinstance(item, dict) and item.get("client_id") == request_id:
                return True
    return False


__all__ = [
    "GALLERY_KEYS",
    "LETTER_KEYS",
    "Ledger",
    "PendingAdmission",
    "RESTORE_GROUPS",
    "gallery_digests",
    "letters",
    "matching_tasks",
    "refusal_evidence",
    "restore_plan",
    "unchanged",
]
