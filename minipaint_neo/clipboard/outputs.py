"""What WanGP made for this tab's requests, kept where a restart can find it.

A job is a queue entry: it is swept minutes after it finishes, because a
queue is a queue. A video is not - it outlives the session that asked for
it, and "show me what I made" is a question asked days later. So the two
are kept apart, and this is the durable half: one entry per Clipboard
request that reached WanGP, holding the files that request produced.

HOW A FILE IS TIED TO A REQUEST. Two ways, and the difference is recorded
rather than smoothed over, because one of them is a match and the other is
not:

* **Exactly**, for a job the server ran. WanGP's own control surface hands
  back the paths it wrote, ``record_execution`` persists them on the job,
  and ``remember`` copies them here while they are fresh. Nothing is
  guessed; a file is in this entry because WanGP said it was.

* **By window**, for a job the browser ran. That path has no execution
  record - a page can see that WanGP took its task and that the task later
  left the queue, and nothing in between - so the entry takes the files
  that appeared in WanGP's output folder between those two moments and
  were not already claimed. It is a match, and ``exact`` is false on it.

Claiming is exclusive: a path in one entry is never taken by another, so
two requests running back to back cannot both claim the same video.

WHY IT IS A PULL AND NOT ONLY A PUSH. ``sync`` reconciles this document
against the outbox rather than waiting to be told: it opens a claim for a
request WanGP has taken, closes one whose job has finished or has already
been swept, and finishes the job of any claim left open by a Forge that was
shut down mid-generation. That last case is the whole reason it reads the
world instead of trusting a callback - the callback was never made, and a
video the user is looking at exists whether or not this extension was
running when it was written.

WHAT CROSSES TO THE BROWSER. Ids, names, sizes and times, over this tab's
own route, the way the picture library already works. A path never does,
and neither does a name on the shared event stream - the stream is every
page's, and only counts and states belong on it.
"""

from __future__ import annotations

import os
import pathlib
import secrets
import threading
import time
import typing

from .. import scrub
from . import config

#: What counts as something WanGP made. Videos first, because that is what
#: it makes; images because some models write a frame or a grid beside it,
#: and a gallery that hid them would be lying about what the run produced.
#: A GIF is an image here and not a video, because that is what a browser
#: makes of it: put one in a <video> and it renders nothing at all.
VIDEO_SUFFIXES = (".mp4", ".webm", ".mkv", ".mov", ".m4v")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")
MEDIA_SUFFIXES = VIDEO_SUFFIXES + IMAGE_SUFFIXES

#: How many entries are kept. Generous, because an entry is a few hundred
#: bytes and the thing it points at is hundreds of megabytes: the document
#: is never what fills a disk, and forgetting a video the user still has is
#: worse than keeping a line about one they deleted.
MAX_ENTRIES = 2000

#: How long after a job leaves WanGP's queue a file may still turn up and
#: be counted as that job's. WanGP writes the file as it finishes, but the
#: page learns the task has gone on its next poll, so the two are not the
#: same instant.
CLOSE_GRACE_SECONDS = 180.0

#: How long a claim may stay open before it is closed with whatever it has.
#: A job that vanished without ever being seen finished - Forge killed,
#: WanGP killed, the page closed - would otherwise hold its window open
#: forever and go on claiming files that have nothing to do with it.
CLAIM_MAX_SECONDS = 24 * 60 * 60.0

#: How many files one directory listing will look at. A folder somebody has
#: pointed at their whole media library should make this slow, not fatal.
MAX_SCANNED = 5000

SCHEMA = 1

_LOG_PREFIX = "MiniPaint Clipboard:"
_lock = threading.RLock()
_seams: typing.Dict[str, typing.Any] = {"clock": time.time}


def use_clock(clock: typing.Optional[typing.Callable[[], float]]) -> None:
    """Test seam: the wall clock a window is measured on."""
    _seams["clock"] = clock or time.time


def _now() -> float:
    return float(_seams["clock"]())


def _id() -> str:
    return secrets.token_hex(8)


# ------------------------------------------------------------------ folder --


def folder() -> typing.Optional[pathlib.Path]:
    """Where WanGP writes, or ``None`` when there is nothing to read.

    The setting first, so an install that keeps its outputs somewhere else
    has a way to say so; otherwise ``<wangp root>/outputs``, which is where
    every install that has not been repointed puts them. A folder that is
    not there is not an error - WanGP may simply never have run.
    """
    chosen = str(config.load().outputs_folder or "").strip()
    if chosen:
        path = pathlib.Path(chosen).expanduser()
        return path if path.is_dir() else None
    try:
        from ..wangp import config as wangp_config

        root = str(wangp_config.load().wangp_root or "").strip()
    except Exception:
        return None
    if not root:
        return None
    path = pathlib.Path(root).expanduser() / "outputs"
    return path if path.is_dir() else None


def _media_in(where: pathlib.Path) -> typing.List[typing.Tuple[pathlib.Path, float, int]]:
    """Every media file under ``where``, with when it was written and how big.

    Recursive, because WanGP sorts its outputs into per-model folders on
    some versions and not others, and a gallery that only saw the top level
    would be empty on half of them.
    """
    found: typing.List[typing.Tuple[pathlib.Path, float, int]] = []
    try:
        walker = os.walk(str(where))
    except OSError:
        return found
    for parent, _dirs, names in walker:
        for name in names:
            if not name.lower().endswith(MEDIA_SUFFIXES):
                continue
            path = pathlib.Path(parent) / name
            try:
                stat = path.stat()
            except OSError:
                continue
            found.append((path, float(stat.st_mtime), int(stat.st_size)))
            if len(found) >= MAX_SCANNED:
                return found
    return found


# ---------------------------------------------------------------- document --


def _normalize_file(raw: typing.Any) -> typing.Optional[dict]:
    if not isinstance(raw, dict):
        return None
    path = raw.get("path")
    if not isinstance(path, str) or not path:
        return None
    file_id = raw.get("file_id")
    return {
        "file_id": file_id if isinstance(file_id, str) and len(file_id) == 16 else _id(),
        "path": path[:1000],
        "name": str(raw.get("name") or pathlib.Path(path).name)[:200],
        "size": max(0, int(raw.get("size") or 0)),
        "at": float(raw.get("at") or 0.0),
    }


def _normalize_entry(raw: typing.Any) -> typing.Optional[dict]:
    if not isinstance(raw, dict):
        return None
    job_id = raw.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        return None
    files = raw.get("files")
    kept = [one for one in (_normalize_file(item) for item in files) if one] if isinstance(files, (list, tuple)) else []
    entry_id = raw.get("entry_id")
    return {
        "entry_id": entry_id if isinstance(entry_id, str) and len(entry_id) == 16 else _id(),
        "job_id": job_id[:32],
        "request_id": str(raw.get("request_id") or "")[:64],
        "opened_at": float(raw.get("opened_at") or 0.0),
        "closed_at": float(raw.get("closed_at") or 0.0),
        #: The newest file already in the output folder when this claim
        #: opened. See ``_floor``.
        "floor": float(raw.get("floor") or 0.0),
        "exact": bool(raw.get("exact")),
        "model": str(raw.get("model") or "")[:120],
        "files": kept,
    }


def _load() -> typing.List[dict]:
    raw = config.read_document(config.OUTPUTS_NAME, {})
    entries = raw.get("entries") if isinstance(raw, dict) else None
    listed = [one for one in (_normalize_entry(item) for item in entries) if one] if isinstance(entries, (list, tuple)) else []
    listed.sort(key=lambda entry: (entry["opened_at"], entry["entry_id"]))
    return listed[-MAX_ENTRIES:]


def _save(entries: typing.List[dict]) -> None:
    config.write_document(config.OUTPUTS_NAME, {"schema": SCHEMA, "entries": entries[-MAX_ENTRIES:]})


def _claimed_paths(entries: typing.Sequence[dict]) -> typing.Set[str]:
    return {one["path"] for entry in entries for one in entry["files"]}


def _entry_for_job(entries: typing.Sequence[dict], job_id: str) -> typing.Optional[dict]:
    for entry in entries:
        if entry["job_id"] == job_id:
            return entry
    return None


def _file_record(path: pathlib.Path, at: float = 0.0, size: int = 0) -> dict:
    if not at or not size:
        try:
            stat = path.stat()
            at = at or float(stat.st_mtime)
            size = size or int(stat.st_size)
        except OSError:
            pass
    return {"file_id": _id(), "path": str(path), "name": path.name, "size": int(size), "at": float(at)}


# -------------------------------------------------------------- the writing --


def remember(job_id: typing.Any, paths: typing.Sequence[typing.Any], request_id: str = "",
             model: str = "") -> typing.Optional[dict]:
    """The exact half: WanGP said it wrote these, for this job.

    Called while the paths are fresh - the job they live on is swept from
    the queue minutes later - and it closes the entry outright, because
    there is nothing left to find out. A path already claimed by another
    entry is not taken twice.
    """
    job_id = str(job_id or "")
    wanted = [str(one) for one in paths if isinstance(one, str) and one]
    if not job_id:
        return None
    with _lock:
        entries = _load()
        entry = _entry_for_job(entries, job_id)
        now = _now()
        if entry is None:
            entry = {"entry_id": _id(), "job_id": job_id, "request_id": str(request_id or "")[:64],
                     "opened_at": now, "closed_at": 0.0, "exact": True, "floor": 0.0,
                     "model": str(model or "")[:120], "files": []}
            entries.append(entry)
        if request_id:
            entry["request_id"] = str(request_id)[:64]
        if model:
            entry["model"] = str(model)[:120]
        taken = _claimed_paths(entries)
        for one in wanted:
            path = pathlib.Path(one)
            if str(path) in taken:
                continue
            entry["files"].append(_file_record(path))
            taken.add(str(path))
        entry["exact"] = True
        entry["closed_at"] = now
        _save(entries)
        if wanted:
            # The count, never a name: this line can reach a console.
            scrub.console(f"job {job_id[:8]}: {len(wanted)} output file(s) recorded.", _LOG_PREFIX)
        return dict(entry)


def _floor(where: typing.Optional[pathlib.Path]) -> float:
    """The newest file already in the output folder.

    What a window is measured from, instead of the moment the request was
    made. The two are nearly the same and the difference is the whole
    point: a folder full of videos from last year has files whose times
    nobody can vouch for - a copy, a restore, a touched file, a clock that
    moved - and "newer than everything that was already here" cannot claim
    any of them, where "after the request" can.
    """
    if where is None:
        return 0.0
    times = [at for _path, at, _size in _media_in(where)]
    return max(times) if times else 0.0


def _close(entry: dict, entries: typing.Sequence[dict], now: float) -> None:
    """Take the files that appeared while this entry's job was running."""
    entry["closed_at"] = now
    where = folder()
    if where is None:
        return
    taken = _claimed_paths(entries)
    # Strictly after the floor: a file whose time equals it was already
    # there when the claim opened, and a claim never takes what it found.
    start = max(entry["opened_at"], entry["floor"])
    end = now + CLOSE_GRACE_SECONDS
    for path, at, size in sorted(_media_in(where), key=lambda item: item[1]):
        if str(path) in taken or not (start < at <= end):
            continue
        entry["files"].append(_file_record(path, at, size))
        taken.add(str(path))


def sync() -> None:
    """Reconcile this document against the queue. Safe to call at any time.

    Opens a claim for every request WanGP has taken and does not have one,
    closes the claims whose jobs have finished or have already been swept,
    and closes any claim a shutdown left open. Called at start-up and
    whenever the gallery is asked for a page, which between them covers
    every way a session can end.
    """
    from . import outbox

    with _lock:
        entries = _load()
        now = _now()
        changed = False
        where = folder()
        jobs = {job["job_id"]: job for job in outbox.jobs()}
        for job in jobs.values():
            if job["state"] not in outbox.POSITIVE and job["state"] not in outbox.SERVER_SUBMITTED:
                continue
            if _entry_for_job(entries, job["job_id"]) is not None:
                continue
            entries.append({
                "entry_id": _id(), "job_id": job["job_id"],
                "request_id": str((job.get("request") or {}).get("request_id") or "")[:64],
                # The window opens when the request was made, not when this
                # noticed it: a video written before the first sync of a
                # session still belongs to the job that asked for it.
                "opened_at": float(job.get("created") or now),
                "closed_at": 0.0, "exact": False, "floor": _floor(where),
                "model": str((job.get("model") or {}).get("label") or (job.get("model") or {}).get("type") or "")[:120], "files": [],
            })
            changed = True
        for entry in entries:
            if entry["closed_at"]:
                continue
            job = jobs.get(entry["job_id"])
            over = job is None or _job_is_done(job, outbox)
            if over or now - entry["opened_at"] > CLAIM_MAX_SECONDS:
                _close(entry, entries, now)
                changed = True
        if changed:
            _save(entries)


def _job_is_done(job: typing.Mapping[str, typing.Any], outbox: typing.Any) -> bool:
    """Whether WanGP has finished with this job, as far as anything can see."""
    state = job.get("state")
    if state == outbox.COMPLETED or outbox.failed_state(state):
        return True
    if state in (outbox.QUEUED, outbox.STARTED):
        return (job.get("wangp") or {}).get("state") == outbox.WANGP_FINISHED
    return False


# -------------------------------------------------------------- the reading --


def _kind(name: str) -> str:
    return "video" if name.lower().endswith(VIDEO_SUFFIXES) else "image"


def files(refresh: bool = True) -> typing.List[dict]:
    """Every output still on disk, newest first, as facts for the browser.

    A file the user has deleted is dropped rather than shown as a broken
    tile, and dropping it is written back - the document is a record of
    what exists, not of what once did.
    """
    if refresh:
        sync()
    with _lock:
        entries = _load()
        listed: typing.List[dict] = []
        changed = False
        for entry in entries:
            kept = []
            for one in entry["files"]:
                path = pathlib.Path(one["path"])
                try:
                    stat = path.stat()
                except OSError:
                    changed = True
                    continue
                kept.append(one)
                listed.append({
                    "id": one["file_id"],
                    "name": one["name"],
                    "kind": _kind(one["name"]),
                    "size": int(stat.st_size),
                    "at": one["at"] or float(stat.st_mtime),
                    "job_id": entry["job_id"],
                    "request_id": entry["request_id"],
                    "exact": entry["exact"],
                })
            if len(kept) != len(entry["files"]):
                entry["files"] = kept
        if changed:
            _save(entries)
    listed.sort(key=lambda item: (item["at"], item["id"]), reverse=True)
    return listed


def path_of(file_id: typing.Any) -> typing.Optional[pathlib.Path]:
    """The file behind an opaque id, or ``None``. The only place a path is resolved."""
    wanted = str(file_id or "")
    if not wanted:
        return None
    with _lock:
        for entry in _load():
            for one in entry["files"]:
                if one["file_id"] == wanted:
                    path = pathlib.Path(one["path"])
                    return path if path.is_file() else None
    return None


def forget_all() -> int:
    """Empty the document. The files themselves are never touched."""
    with _lock:
        count = sum(len(entry["files"]) for entry in _load())
        _save([])
        return count


def reset_for_tests() -> None:
    with _lock:
        _save([])


__all__ = [
    "CLAIM_MAX_SECONDS",
    "CLOSE_GRACE_SECONDS",
    "IMAGE_SUFFIXES",
    "MAX_ENTRIES",
    "MEDIA_SUFFIXES",
    "SCHEMA",
    "VIDEO_SUFFIXES",
    "files",
    "folder",
    "forget_all",
    "path_of",
    "remember",
    "reset_for_tests",
    "use_clock",
    "sync",
]
