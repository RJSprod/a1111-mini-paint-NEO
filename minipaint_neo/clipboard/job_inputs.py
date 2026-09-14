"""Images a queued job owns, and the pin that keeps them until it is done.

THE FAILURE THIS PREVENTS, STATED ACCURATELY.

Before the queue was server-owned, a job could not outlive the page that
pressed it: the browser claimed it, prepared its images seconds before use,
and released them in the same function's finally. Nothing had to keep a
picture for longer than a request, so nothing did.

Server-owned execution changes that completely. A job now waits for a cold
model to load, for a language model to write a prompt, and - because the card
is shared with whatever the user is doing in the WanGP tab - for somebody
else's generation to finish, which can take as long as it takes. It waits
across a Forge restart. The picture it was composed with has to still be
there at the end of all that.

The hazard is not the six-hour handoff sweep, which already exempts anything
live. It is narrower and it is real: an *admitted* job holds image handles -
a staging token, or a Clipboard asset id - and the staging area is swept by
age at start-up. A Forge that restarts with a queued job whose staged image
is older than half an hour comes back to a job whose input is gone. That is a
restart, not a wait, and it is what this file is written against.

So at admission every handle is resolved once and copied into a durable,
job-owned object under the handoff root, and that object is *pinned*: a
registry on disk, read by the sweeper, that survives the restart the hazard
needs. A pinned object is never swept by age, however long the job waits. It
is released when the job is terminal and a retention grace has passed, so a
retry or a diagnosis still has the picture it ran with.

Two consequences worth stating because they are easy to get backwards:

*   A pin is recorded **before** the job document is written, and released
    **after** the job is terminal. An input pinned for a job that was never
    stored is a leak swept by the orphan rule below; an input released for a
    job still waiting is the failure this whole file exists to prevent, and
    the asymmetry is deliberate.

*   Recovery registers pins **before** any sweeper runs. The start-up order
    is the whole of the protection: a sweep that runs first has already
    deleted the thing the pin was going to protect.
"""

from __future__ import annotations

import datetime
import threading
import time
import typing

from ..wangp import errors, handoff, protocol
from ..wangp.errors import IntegrationError
from . import config

PINS_NAME = "clipboard-job-inputs.json"
SCHEMA = 1

#: How long a terminal job's inputs are kept. Long enough for a retry that
#: somebody decides on after looking at the result, and for a diagnosis of
#: what a job actually ran with; short enough that a week of queue does not
#: become a week of pictures.
RETENTION_SECONDS = 24 * 3600.0

#: A pin whose job never appeared. Written before the job document, so a
#: crash in between leaves one behind; swept on this grace rather than kept
#: forever, and never on the same clock as a live job.
ORPHAN_SECONDS = 3600.0

_lock = threading.RLock()
_seams: typing.Dict[str, typing.Any] = {"clock": time.time}


def use_clock(clock: typing.Optional[typing.Callable[[], float]]) -> None:
    """Test seam: the wall clock retention is measured on."""
    _seams["clock"] = clock or time.time


def reset_for_tests() -> None:
    _seams["clock"] = time.time


def _now() -> float:
    return float(_seams["clock"]())


def _iso(stamp: float) -> str:
    return datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).replace(microsecond=0).isoformat()


# ------------------------------------------------------------- the registry --


def _normalize(raw: typing.Any) -> typing.Optional[dict]:
    if not isinstance(raw, dict):
        return None
    input_id = raw.get("input_id")
    if not protocol.valid_handoff_id(input_id):
        return None
    return {
        "input_id": input_id,
        "job_id": str(raw.get("job_id") or "")[:32],
        "slot": str(raw.get("slot") or "")[:40],
        "created_at": float(raw.get("created_at") or 0.0),
        "released_at": float(raw.get("released_at") or 0.0),
        "width": int(raw.get("width") or 0),
        "height": int(raw.get("height") or 0),
        "digest": str(raw.get("digest") or "")[:64],
        # What the input was made from, for diagnostics only. A kind and an
        # opaque id; never a path, because there is no field one could go in.
        "source_kind": str(raw.get("source_kind") or "")[:40],
        "source_id": str(raw.get("source_id") or "")[:64],
    }


def _load() -> typing.List[dict]:
    document = config.read_document(PINS_NAME, {})
    listed = document.get("pins") if isinstance(document, dict) else None
    if not isinstance(listed, list):
        return []
    return [item for item in (_normalize(entry) for entry in listed) if item is not None]


def _save(pins: typing.List[dict]) -> None:
    config.write_document(PINS_NAME, {"schema": SCHEMA, "pins": pins})


# ------------------------------------------------------------------ pinning --


def adopt(handle: typing.Mapping[str, typing.Any], job_id: str = "", slot: str = "") -> dict:
    """One image handle, resolved now and kept as a durable job-owned input.

    Resolved *now*, at admission, because a handle is a reference to
    somebody else's lifetime - a staging token that expires, an asset a user
    may rename or delete - and a queued job cannot depend on either. What it
    gets instead is its own copy under the handoff root, pinned, named by an
    id of the same fixed grammar as every other id this extension hands out.

    The pin is written before the caller stores the job. A pin with no job is
    swept as an orphan; a job with no pin is the failure.
    """
    from .. import interop

    image = interop.open_handle(handle)
    written = handoff.write(image)
    now = _now()
    record = _normalize(
        {
            "input_id": written.id,
            "job_id": str(job_id or ""),
            "slot": slot,
            "created_at": now,
            "released_at": 0.0,
            "width": written.manifest.get("width"),
            "height": written.manifest.get("height"),
            "digest": written.manifest.get("sha256"),
            "source_kind": handle.get("kind"),
            "source_id": handle.get("id"),
        }
    )
    if record is None:  # pragma: no cover - handoff.write always mints a valid id
        raise IntegrationError(errors.INTERNAL_ERROR, "a job input could not be recorded")
    with _lock:
        pins = _load()
        pins.append(record)
        _save(pins)
    return dict(record)


def assign(input_ids: typing.Iterable[str], job_id: str) -> int:
    """Attach already-adopted inputs to the job that was just stored.

    The second half of the ordering rule: the pins exist before the job does,
    and this is what stops them looking like orphans the moment it appears.
    """
    wanted = {item for item in input_ids if protocol.valid_handoff_id(item)}
    if not wanted or not job_id:
        return 0
    with _lock:
        pins = _load()
        changed = 0
        for pin in pins:
            if pin["input_id"] in wanted and pin["job_id"] != job_id:
                pin["job_id"] = str(job_id)[:32]
                changed += 1
        if changed:
            _save(pins)
        return changed


def release(input_ids: typing.Iterable[str], now: typing.Optional[float] = None) -> int:
    """Start a pinned input's retention clock. It is not deleted here.

    Deliberately not deleted: a job that has just failed is a job somebody is
    about to look at, and quite possibly retry. The picture it ran with is
    part of the answer to "why did that come out like that", so it stays for
    the grace period and the sweeper takes it after.
    """
    wanted = {item for item in input_ids if protocol.valid_handoff_id(item)}
    if not wanted:
        return 0
    moment = _now() if now is None else float(now)
    with _lock:
        pins = _load()
        changed = 0
        for pin in pins:
            if pin["input_id"] in wanted and not pin["released_at"]:
                pin["released_at"] = moment
                changed += 1
        if changed:
            _save(pins)
        return changed


def resolve(input_id: typing.Any) -> typing.Any:
    """The file a job input names, proved to be under the handoff root.

    The same three questions every other resolver here asks, with this one's
    own code for the answer that matters: an input that is gone is not a
    generic missing file, it is the thing the pin was supposed to prevent,
    and it is named so a recurrence is visible rather than read as an
    ordinary refusal.
    """
    if not protocol.valid_handoff_id(input_id):
        raise IntegrationError(errors.HANDOFF_INVALID_ID, "a job input id is 32 lowercase hex characters")
    try:
        return handoff.resolve(str(input_id))
    except IntegrationError as error:
        if error.code == errors.HANDOFF_NOT_FOUND:
            raise IntegrationError(errors.JOB_INPUT_MISSING, f"job input {str(input_id)[:8]} is no longer on disk")
        raise


def pinned_ids(include_released: bool = False) -> typing.Set[str]:
    """Every input a job still owns. What the sweeper must not touch.

    ``include_released`` adds the ones inside their retention grace, which is
    what the sweeper wants: a released input is still protected from the age
    rule until its own clock runs out.
    """
    now = _now()
    with _lock:
        pins = _load()
    out: typing.Set[str] = set()
    for pin in pins:
        if not pin["released_at"]:
            out.add(pin["input_id"])
        elif include_released and now - pin["released_at"] <= RETENTION_SECONDS:
            out.add(pin["input_id"])
    return out


def for_job(job_id: str) -> typing.List[dict]:
    with _lock:
        return [dict(pin) for pin in _load() if pin["job_id"] == job_id]


def sweep(now: typing.Optional[float] = None) -> int:
    """Drop pins whose grace ran out, and the files with them. Returns the count.

    Only ever a pin that has been explicitly released and waited out its
    retention, or one that was written for a job that never appeared. A pin
    with no release stamp is a live job's input and is untouchable however
    old it is - that is the whole promise, and the sweep is where it would be
    broken if it were going to be.
    """
    moment = _now() if now is None else float(now)
    with _lock:
        pins = _load()
        kept: typing.List[dict] = []
        dropped: typing.List[str] = []
        for pin in pins:
            if pin["released_at"]:
                if moment - pin["released_at"] > RETENTION_SECONDS:
                    dropped.append(pin["input_id"])
                    continue
            elif not pin["job_id"] and moment - pin["created_at"] > ORPHAN_SECONDS:
                # Written before a job that was never stored. An hour is far
                # longer than the window between the two writes.
                dropped.append(pin["input_id"])
                continue
            kept.append(pin)
        if dropped:
            _save(kept)
    for input_id in dropped:
        handoff.discard(input_id)
    return len(dropped)


def counts() -> dict:
    """How many inputs are held, released and total. For a status line."""
    now = _now()
    with _lock:
        pins = _load()
    held = sum(1 for pin in pins if not pin["released_at"])
    waiting = sum(1 for pin in pins if pin["released_at"] and now - pin["released_at"] <= RETENTION_SECONDS)
    return {"held": held, "retained": waiting, "total": len(pins)}


def describe(input_id: typing.Any) -> typing.Optional[dict]:
    """One input's record, for diagnostics. Ids and sizes, never a path."""
    with _lock:
        for pin in _load():
            if pin["input_id"] == input_id:
                return dict(pin, created=_iso(pin["created_at"]) if pin["created_at"] else "")
    return None


__all__ = [
    "ORPHAN_SECONDS", "PINS_NAME", "RETENTION_SECONDS", "SCHEMA",
    "adopt", "assign", "counts", "describe", "for_job", "pinned_ids", "release",
    "reset_for_tests", "resolve", "sweep", "use_clock",
]
