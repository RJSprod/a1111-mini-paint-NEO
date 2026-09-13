"""The queue outbox: every Add to Queue press as a job the server owns.

A request owns the live WanGP form from the moment the bridge writes into
it until the admission is settled and the overrides are put back, so two
requests cannot overlap and the waiting between them cannot be removed. It
can be moved: off the button, off the user, and off the browser. This module
is where it goes. A press appends a job here and returns at once; the jobs
are run one at a time, in the order they were pressed, across every browser
page on this Forge, by the page that composed each one - the WanGP form a
job overlays is Gradio session state belonging to the iframe in *that*
document, and "uses WanGP's current settings" means the settings that user
is looking at, not another browser's.

The server is the authority: it holds the list (``clipboard-outbox.json``,
beside the draft and the history, with the same atomic write and the same
quarantine of a broken file), hands out one lease at a time, decides whose
turn it is, and is told the outcome. A page is a pump that asks "is it my
turn, and what do I run" and reports back; it holds no queue of its own, so
a refresh, a closed tab or a second browser cannot lose or duplicate work.

Three rules a reader should not have to infer:

* An **unconfirmed** job is never retried by the machine. "Unconfirmed"
  means the bridge could not prove either way whether WanGP took the task,
  and a retry could queue it twice. It is shown, and a person decides.
* A lease that expires puts a job back to **pending** only when the page
  never reported the bridge's admission; once the overlay was written, an
  expired lease means unconfirmed, for the same reason.
* Nothing here is asked of WanGP while WanGP is not running: a press is
  refused with ``WANGP_NOT_RUNNING`` rather than stored for a process that
  may never come.

The prompt is held in the job because the job is a snapshot of the composer
at press time and must survive the composer changing afterwards. It is
shown in the tab, and it is never written to a log.
"""

from __future__ import annotations

import datetime
import re
import secrets
import threading
import time
import typing

from ..wangp import errors, protocol
from ..wangp.errors import IntegrationError
from . import config

OUTBOX_NAME = "clipboard-outbox.json"
SCHEMA = 1

PENDING = "pending"
SENDING = "sending"
QUEUED = "queued"
STARTED = "started"
FAILED = "failed"
UNCONFIRMED = "unconfirmed"
CANCELLED = "cancelled"
STATES = (PENDING, SENDING, QUEUED, STARTED, FAILED, UNCONFIRMED, CANCELLED)
TERMINAL = (QUEUED, STARTED, FAILED, UNCONFIRMED, CANCELLED)
POSITIVE = (QUEUED, STARTED)

ORIGIN_CLIPBOARD = "clipboard"
ORIGIN_API = "api"
ORIGINS = (ORIGIN_CLIPBOARD, ORIGIN_API)

#: A claimed job must be reported within this; a whole enqueue is bounded at
#: about forty seconds, so twice that is a page that went away.
LEASE_SECONDS = 90.0
#: A page that claimed within this long is pumping; the head job of a page
#: that has not is skipped in favour of a page that is asking.
PAGE_ACTIVE_SECONDS = 15.0
#: How long a page is told to wait before asking again: while another page's
#: job is being sent, and while it is another page's turn.
WAIT_BUSY_MS = 400
WAIT_TURN_MS = 250
#: Bounds. Terminal jobs are kept a week for the list; pending ones are
#: capped so a runaway caller cannot fill the disk with requests.
MAX_JOBS = 500
MAX_PENDING = 200
KEEP_TERMINAL_SECONDS = 7 * 24 * 3600.0

PAGE_RE = re.compile(r"\A[0-9a-f]{8,32}\Z")
PHASE_SENT = "sent"
PHASE_DONE = "done"
PHASES = (PHASE_SENT, PHASE_DONE)

_LOG_PREFIX = "MiniPaint Clipboard:"

_lock = threading.RLock()
_seams: typing.Dict[str, typing.Any] = {"clock": time.time, "running": None}
#: When each page last claimed. In memory on purpose: after a Forge restart
#: every page is "not asking" until it asks again, which is the truth.
_pages: typing.Dict[str, float] = {}


# ---------------------------------------------------------------- seams --


def use_clock(clock: typing.Optional[typing.Callable[[], float]]) -> None:
    """Test seam: the wall clock the leases and the ages are measured on."""
    _seams["clock"] = clock or time.time


def use_running(running: typing.Optional[typing.Callable[[], bool]]) -> None:
    """Test seam: whether the managed WanGP is running. None restores the runtime's answer."""
    _seams["running"] = running


def wangp_running() -> bool:
    """Whether the WanGP this extension manages is serving right now.

    The runtime's own answer, contained: a WanGP integration that cannot be
    imported is a WanGP that is not running, and a press is refused rather
    than stored for it.
    """
    override = _seams.get("running")
    if override is not None:
        try:
            return bool(override())
        except Exception:
            return False
    try:
        from ..wangp import runtime

        return bool(runtime.current().snapshot().get("running"))
    except Exception:
        return False


def reset_for_tests() -> None:
    _seams["clock"] = time.time
    _seams["running"] = None
    _pages.clear()


def _now() -> float:
    return float(_seams["clock"]())


def _iso(stamp: float) -> str:
    return datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).replace(microsecond=0).isoformat()


def _journal(message: str) -> None:
    try:
        from ..wangp import process_log

        process_log.note("outbox", message)
    except Exception:
        pass


# ------------------------------------------------------------ the document --


def _code(value: typing.Any) -> str:
    return value if isinstance(value, str) and protocol.CODE_RE.match(value) else ""


def _whole(value: typing.Any, floor: int = 0) -> typing.Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value >= floor else None


def sanitize_result(raw: typing.Any) -> dict:
    """What a page may say about how a job ended: statuses, counts, codes,
    the model - never a prompt, never a path, never a sentence it made up."""
    raw = raw if isinstance(raw, dict) else {}
    status = raw.get("status") if raw.get("status") in (QUEUED, STARTED, "refused", UNCONFIRMED, PENDING) else "refused"
    ok = raw.get("ok") is True and status in POSITIVE
    summary = protocol.queue_summary(raw.get("applied"), raw.get("inherited"), raw.get("ignored"))
    model = raw.get("model") if isinstance(raw.get("model"), dict) else {}
    message = raw.get("message") if isinstance(raw.get("message"), str) else ""
    return {
        "ok": ok,
        "status": status if ok or status in (UNCONFIRMED, "refused") else "refused",
        "tasks_added": _whole(raw.get("tasks_added")) or 0,
        "queue_depth": _whole(raw.get("queue_depth")),
        "route": raw.get("route") if raw.get("route") in protocol.ROUTES else "",
        "start": raw.get("start") if raw.get("start") in protocol.START_ANSWERS else "",
        "applied": summary["applied"],
        "inherited": summary["inherited"],
        "ignored": summary["ignored"],
        "model": {"type": str(model.get("type") or "")[:120], "label": str(model.get("label") or "")[:120]},
        "code": _code(raw.get("code")) if not ok else "",
        "message": message[:200] if not ok else "",
    }


def summary_of(request: typing.Mapping[str, typing.Any]) -> dict:
    """Which fields a request supplies, for a list that must not show the prompt to a log."""
    images = request.get("images") if isinstance(request.get("images"), dict) else {}
    return {
        "prompt": request.get("prompt") is not None,
        "start": bool(images.get(protocol.QUEUE_FIELD_START)),
        "end": bool(images.get(protocol.QUEUE_FIELD_END)),
        "references": len(images.get(protocol.QUEUE_FIELD_REFERENCES) or []),
        "start_mode": request.get("start") or protocol.START_AUTO,
    }


def _normalize(raw: typing.Any) -> typing.Optional[dict]:
    """One job as stored, or None for a record that is not one."""
    if not isinstance(raw, dict):
        return None
    job_id = raw.get("job_id")
    if not isinstance(job_id, str) or not re.match(r"\A[0-9a-f]{16}\Z", job_id):
        return None
    request = raw.get("request")
    if not isinstance(request, dict) or not protocol.valid_request_id(request.get("request_id")):
        return None
    state = raw.get("state") if raw.get("state") in STATES else FAILED
    lease = raw.get("lease") if isinstance(raw.get("lease"), dict) else None
    if lease is not None and not (isinstance(lease.get("token"), str) and isinstance(lease.get("expires_at"), (int, float))):
        lease = None
    return {
        "job_id": job_id,
        "request": request,
        "page": str(raw.get("page") or "")[:32],
        "origin": raw.get("origin") if raw.get("origin") in ORIGINS else ORIGIN_API,
        "created_at": float(raw.get("created_at") or 0.0),
        "updated_at": float(raw.get("updated_at") or 0.0),
        "state": state,
        "attempts": int(raw.get("attempts") or 0),
        "sent": bool(raw.get("sent")),
        "lease": lease,
        "result": raw.get("result") if isinstance(raw.get("result"), dict) else None,
        "error": raw.get("error") if isinstance(raw.get("error"), dict) else None,
        "retry_of": str(raw.get("retry_of") or "")[:16],
        "history_recorded": bool(raw.get("history_recorded")),
    }


def _load() -> typing.List[dict]:
    document = config.read_document(OUTBOX_NAME, {})
    jobs = document.get("jobs") if isinstance(document, dict) else None
    if not isinstance(jobs, list):
        return []
    kept = [job for job in (_normalize(item) for item in jobs) if job is not None]
    kept.sort(key=lambda job: (job["created_at"], job["job_id"]))
    return kept


def _save(jobs: typing.List[dict]) -> None:
    config.write_document(OUTBOX_NAME, {"schema": SCHEMA, "jobs": jobs})


def public(job: typing.Mapping[str, typing.Any]) -> dict:
    """A job as a page or a screen may see it. The lease token stays here."""
    lease = job.get("lease") or {}
    return {
        "job_id": job["job_id"],
        "request": dict(job["request"]),
        "page": job.get("page", ""),
        "origin": job.get("origin", ORIGIN_API),
        "state": job["state"],
        "created_at": _iso(job["created_at"]) if job.get("created_at") else "",
        "updated_at": _iso(job["updated_at"]) if job.get("updated_at") else "",
        # The same two instants as numbers, for ordering: several jobs can
        # settle within one second and the text above cannot tell them apart.
        "created": float(job.get("created_at") or 0.0),
        "updated": float(job.get("updated_at") or 0.0),
        "attempts": job.get("attempts", 0),
        "sent": bool(job.get("sent")),
        "leased_until": _iso(lease["expires_at"]) if lease else "",
        "result": dict(job["result"]) if job.get("result") else None,
        "error": dict(job["error"]) if job.get("error") else None,
        "retry_of": job.get("retry_of", ""),
        "summary": summary_of(job["request"]),
        "history_recorded": bool(job.get("history_recorded")),
    }


# ----------------------------------------------------------------- rules --


def _sweep(jobs: typing.List[dict], now: float) -> bool:
    """Expired leases and old records. Returns whether anything changed."""
    changed = False
    for job in jobs:
        lease = job.get("lease")
        if job["state"] == SENDING and lease is not None and now > float(lease.get("expires_at", 0.0)):
            job["lease"] = None
            job["updated_at"] = now
            changed = True
            if job.get("sent"):
                job["state"] = UNCONFIRMED
                job["error"] = {"code": errors.ADMISSION_UNCONFIRMED, "message": "the page that was sending it went away before WanGP confirmed"}
                _journal(f"job {job['job_id'][:8]}: lease expired after the overlay was written; unconfirmed")
            else:
                job["state"] = PENDING
                _journal(f"job {job['job_id'][:8]}: lease expired before anything was written; pending again")
    kept = [job for job in jobs if not (job["state"] in TERMINAL and now - job["updated_at"] > KEEP_TERMINAL_SECONDS)]
    while len(kept) > MAX_JOBS:
        victim = next((job for job in kept if job["state"] in TERMINAL), None)
        if victim is None:
            break
        kept.remove(victim)
    if len(kept) != len(jobs):
        jobs[:] = kept
        changed = True
    return changed


def _find(jobs: typing.List[dict], job_id: typing.Any) -> dict:
    for job in jobs:
        if job["job_id"] == job_id:
            return job
    raise IntegrationError(errors.QUEUE_JOB_UNKNOWN, f"no job {str(job_id)[:8]}")


def _page(page: typing.Any) -> str:
    text = str(page or "").strip()
    if not PAGE_RE.match(text):
        raise IntegrationError(errors.REQUEST_INVALID, "a page id is 8 to 32 lowercase hex characters")
    return text


# -------------------------------------------------------------------- api --


def jobs() -> typing.List[dict]:
    """Every job, oldest first, after the expired leases have been settled."""
    with _lock:
        listed = _load()
        if _sweep(listed, _now()):
            _save(listed)
        return [public(job) for job in listed]


def get(job_id: typing.Any) -> typing.Optional[dict]:
    with _lock:
        for job in _load():
            if job["job_id"] == job_id:
                return public(job)
    return None


def pending_count(page: typing.Optional[str] = None) -> int:
    with _lock:
        return sum(1 for job in _load() if job["state"] == PENDING and (page is None or job["page"] == page))


def submit(request: typing.Any, page: typing.Any, origin: str = ORIGIN_CLIPBOARD, require_running: bool = True) -> dict:
    """Append a job. The request is normalised here, so a bad one is refused
    before it is stored; a WanGP that is not running refuses it too."""
    from .. import interop

    normalised = interop.normalize_public_request(request)
    page_id = _page(page)
    if origin not in ORIGINS:
        origin = ORIGIN_API
    if require_running and not wangp_running():
        raise IntegrationError(errors.WANGP_NOT_RUNNING, "the managed WanGP is not serving")
    with _lock:
        listed = _load()
        now = _now()
        _sweep(listed, now)
        if sum(1 for job in listed if job["state"] in (PENDING, SENDING)) >= MAX_PENDING:
            raise IntegrationError(errors.QUEUE_BUSY, f"{MAX_PENDING} requests are already waiting")
        job = {
            "job_id": secrets.token_hex(8),
            "request": normalised,
            "page": page_id,
            "origin": origin,
            "created_at": now,
            "updated_at": now,
            "state": PENDING,
            "attempts": 0,
            "sent": False,
            "lease": None,
            "result": None,
            "error": None,
            "retry_of": "",
            "history_recorded": False,
        }
        listed.append(job)
        _save(listed)
    summary = summary_of(normalised)
    supplied = [name for name in ("prompt", "start", "end") if summary[name]] + (["references"] if summary["references"] else [])
    _journal(f"job {job['job_id'][:8]}: submitted from {origin} (overrides {', '.join(supplied) or 'none'}; start {summary['start_mode']}); "
             f"{sum(1 for item in listed if item['state'] == PENDING)} pending")
    return public(job)


def claim(page: typing.Any) -> dict:
    """The next job for this page, with a lease, or why not yet.

    One lease at a time across every page. The head of the line is the
    oldest pending job of a page that is asking; a page that has stopped
    asking does not hold the others up, and its jobs wait for it.
    """
    page_id = _page(page)
    with _lock:
        listed = _load()
        now = _now()
        changed = _sweep(listed, now)
        _pages[page_id] = now
        mine = [job for job in listed if job["state"] == PENDING and job["page"] == page_id]
        if any(job["state"] == SENDING for job in listed):
            if changed:
                _save(listed)
            return {"wait": WAIT_BUSY_MS, "reason": "busy", "pending": len(mine)}
        head = next((job for job in listed if job["state"] == PENDING), None)
        if head is None:
            if changed:
                _save(listed)
            return {"empty": True, "pending": 0}
        job: typing.Optional[dict]
        if head["page"] == page_id:
            job = head
        else:
            owner_active = now - _pages.get(head["page"], 0.0) <= PAGE_ACTIVE_SECONDS
            if owner_active:
                if changed:
                    _save(listed)
                return {"wait": WAIT_TURN_MS, "reason": "turn", "pending": len(mine)}
            job = mine[0] if mine else None
        if job is None:
            if changed:
                _save(listed)
            elsewhere = sum(1 for item in listed if item["state"] == PENDING)
            return {"empty": True, "pending": 0, "waiting_elsewhere": elsewhere}
        token = secrets.token_hex(16)
        job["state"] = SENDING
        job["lease"] = {"token": token, "page": page_id, "expires_at": now + LEASE_SECONDS}
        job["attempts"] = int(job.get("attempts", 0)) + 1
        job["sent"] = False
        job["updated_at"] = now
        _save(listed)
        _journal(f"job {job['job_id'][:8]}: claimed by a page (attempt {job['attempts']}); {len(mine) - 1} of its own pending behind it")
        return {"job": public(job), "lease": token, "pending": max(0, len(mine) - 1)}


def report(job_id: typing.Any, lease: typing.Any, phase: typing.Any, payload: typing.Any = None) -> dict:
    """What a page says about a leased job: the bridge admitted it (``sent``),
    or how it ended (``done``). A report without the lease is nobody's."""
    if phase not in PHASES:
        raise IntegrationError(errors.REQUEST_INVALID, "a report is sent or done")
    with _lock:
        listed = _load()
        now = _now()
        _sweep(listed, now)
        job = _find(listed, job_id)
        current = job.get("lease") or {}
        if job["state"] != SENDING or not lease or current.get("token") != lease:
            raise IntegrationError(errors.REQUEST_INVALID, f"job {job['job_id'][:8]} is not leased to this page")
        if phase == PHASE_SENT:
            job["sent"] = True
            job["lease"] = dict(current, expires_at=now + LEASE_SECONDS)
            job["updated_at"] = now
            _save(listed)
            _journal(f"job {job['job_id'][:8]}: the bridge admitted it; awaiting confirmation")
            return public(job)
        result = sanitize_result(payload)
        if result["ok"]:
            job["state"] = STARTED if result["status"] == STARTED else QUEUED
            job["error"] = None
        elif result["status"] == UNCONFIRMED:
            job["state"] = UNCONFIRMED
            job["error"] = {"code": result["code"] or errors.ADMISSION_UNCONFIRMED, "message": result["message"] or errors.message(errors.ADMISSION_UNCONFIRMED)}
        else:
            job["state"] = FAILED
            job["error"] = {"code": result["code"] or errors.QUEUE_REQUEST_REFUSED, "message": result["message"] or errors.message(result["code"] or errors.QUEUE_REQUEST_REFUSED)}
        job["result"] = result
        job["lease"] = None
        job["updated_at"] = now
        _save(listed)
        depth = f", {result['queue_depth']} ahead" if result.get("queue_depth") is not None else ""
        _journal(f"job {job['job_id'][:8]}: {job['state']}" + (f" ({job['error']['code']})" if job.get("error") else f" via {result.get('route') or 'queue'}{depth}"))
        return public(job)


def cancel(job_id: typing.Any) -> dict:
    """A pending job is cancelled; one being sent is not ours to stop."""
    with _lock:
        listed = _load()
        now = _now()
        _sweep(listed, now)
        job = _find(listed, job_id)
        if job["state"] == SENDING:
            raise IntegrationError(errors.QUEUE_BUSY, f"job {job['job_id'][:8]} is being sent")
        if job["state"] != PENDING:
            return public(job)
        job["state"] = CANCELLED
        job["updated_at"] = now
        _save(listed)
        _journal(f"job {job['job_id'][:8]}: cancelled while pending")
        return public(job)


def retry(job_id: typing.Any, page: typing.Any) -> dict:
    """A failed or unconfirmed job, tried again as a *new* job for this page.

    New because the bridge answers a reused request id from its record, and
    for an unconfirmed job that record may say "queued" for a task nobody
    saw. A person pressed this; the machine never does.
    """
    page_id = _page(page)
    with _lock:
        listed = _load()
        now = _now()
        _sweep(listed, now)
        old = _find(listed, job_id)
        if old["state"] not in (FAILED, UNCONFIRMED, CANCELLED):
            raise IntegrationError(errors.REQUEST_INVALID, f"job {old['job_id'][:8]} is {old['state']}, not something to retry")
    request = dict(old["request"])
    request["request_id"] = secrets.token_hex(16)
    job = submit(request, page_id, old["origin"])
    with _lock:
        listed = _load()
        for item in listed:
            if item["job_id"] == job["job_id"]:
                item["retry_of"] = old["job_id"]
        _save(listed)
    job["retry_of"] = old["job_id"]
    return job


def adopt(job_id: typing.Any, page: typing.Any) -> dict:
    """A pending job composed on a page that is gone, run from this one instead.

    A choice a person makes, because the job then inherits *this* page's
    WanGP settings rather than the ones it was composed beside.
    """
    page_id = _page(page)
    with _lock:
        listed = _load()
        now = _now()
        _sweep(listed, now)
        job = _find(listed, job_id)
        if job["state"] != PENDING:
            raise IntegrationError(errors.REQUEST_INVALID, f"job {job['job_id'][:8]} is {job['state']}, not pending")
        job["page"] = page_id
        job["updated_at"] = now
        _save(listed)
        _journal(f"job {job['job_id'][:8]}: adopted by another page")
        return public(job)


def on_wangp_restart() -> int:
    """WanGP is going away: whatever was being sent can no longer be proved."""
    with _lock:
        listed = _load()
        now = _now()
        count = 0
        for job in listed:
            if job["state"] == SENDING:
                job["state"] = UNCONFIRMED
                job["lease"] = None
                job["error"] = {"code": errors.WANGP_RESTARTED, "message": errors.message(errors.WANGP_RESTARTED)}
                job["updated_at"] = now
                count += 1
        if count:
            _save(listed)
            _journal(f"WanGP restarted: {count} job(s) in flight are unconfirmed")
        return count


def unrecorded(origin: str = ORIGIN_CLIPBOARD) -> typing.List[dict]:
    """Positive jobs the tab's history has not been told about yet."""
    with _lock:
        return [public(job) for job in _load() if job["state"] in POSITIVE and job["origin"] == origin and not job.get("history_recorded")]


def mark_recorded(job_ids: typing.Iterable[str]) -> None:
    wanted = set(job_ids)
    if not wanted:
        return
    with _lock:
        listed = _load()
        for job in listed:
            if job["job_id"] in wanted:
                job["history_recorded"] = True
        _save(listed)


def counts() -> dict:
    """How many jobs are in each state, for a status line."""
    with _lock:
        listed = _load()
    out = {state: 0 for state in STATES}
    for job in listed:
        out[job["state"]] += 1
    return out


__all__ = [
    "CANCELLED", "FAILED", "LEASE_SECONDS", "MAX_JOBS", "MAX_PENDING", "ORIGIN_API", "ORIGIN_CLIPBOARD", "OUTBOX_NAME",
    "PAGE_ACTIVE_SECONDS", "PENDING", "PHASE_DONE", "PHASE_SENT", "POSITIVE", "QUEUED", "SENDING", "STARTED", "STATES", "TERMINAL",
    "UNCONFIRMED", "adopt", "cancel", "claim", "counts", "get", "jobs", "mark_recorded", "on_wangp_restart", "pending_count", "public",
    "report", "reset_for_tests", "retry", "sanitize_result", "submit", "summary_of", "unrecorded", "use_clock", "use_running", "wangp_running",
]
