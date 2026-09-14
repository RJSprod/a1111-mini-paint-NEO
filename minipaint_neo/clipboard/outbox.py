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

With enhanced prompts on, a job has one more stage before it is pending:
**enhancing**. The press hands the typed prompt (and the pictures the H3
model reads) to ModelSwitchRefiner's LLM Studio at once, so the language
model - which works one request at a time - is never left idle while
requests wait; the job then waits here, still in press order, and becomes
pending the moment its prompt is written. The line is strict: a job whose
prompt is still being written holds every job behind it, whichever page
pressed it and whether or not that job is enhanced, because "in the order
requested" is the promise. Each job's record says where its enhancement is
(waiting at position n, a stage the run is at, done in so many seconds,
failed and why) and, once WanGP has it, where it is in WanGP's own queue
(waiting, generating, gone) as the page that queued it reports back.

Three rules a reader should not have to infer:

* An **unconfirmed** job is never retried by the machine. "Unconfirmed"
  means the bridge could not prove either way whether WanGP took the task,
  and a retry could queue it twice. It is shown, and a person decides.
* A lease that expires puts a job back to **pending** only when the page
  never reported the bridge's admission; once the overlay was written, an
  expired lease means unconfirmed, for the same reason.
* Nothing here is asked of WanGP while WanGP is not running: a press is
  refused with ``WANGP_NOT_RUNNING`` rather than stored for a process that
  may never come. An enhanced press is refused - before anything is stored
  or asked - when the LLM side is not available or the page is not on a
  MiniMax H3 model.

The prompt is held in the job because the job is a snapshot of the composer
at press time and must survive the composer changing afterwards. It is
shown in the tab, and it is never written to a log. The enhanced prompt
replaces it when it arrives, and the typed one is kept beside it.
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
#: 3 adds the server-execution half: the stages of B3, the durable snapshot,
#: the job-owned inputs and the execution id. A document written by schema 2
#: still reads correctly - every state it can carry is still a state - which
#: is the read-side migration this change needs and the reason the legacy
#: vocabulary below is kept rather than replaced.
SCHEMA = 3

# -- the browser-executed vocabulary (legacy) -------------------------------
# Kept so a page that was already loaded when this version arrived does not
# break, and so a job admitted under the old scheme is read as what it was.
# No new job uses a lease as its execution truth.
ENHANCING = "enhancing"
PENDING = "pending"
SENDING = "sending"
QUEUED = "queued"
STARTED = "started"
FAILED = "failed"
UNCONFIRMED = "unconfirmed"
CANCELLED = "cancelled"

# -- the server-executed vocabulary ------------------------------------------
# What a job is actually waiting for, said out loud. The old states described
# where a job was in a handshake with a browser; these describe where it is
# in the work, which is the only thing a person who walked away can be told.
#: Durably accepted; inputs pinned; no browser required from here on.
ADMITTED = "admitted"
#: Behind an earlier job of ours. Press order, and nothing else.
WAITING_TURN = "waiting_turn"
#: The enhanced prompt exists and is persisted; ready for the WanGP stage.
ENHANCED = "enhanced"
#: The managed WanGP child is being brought to READY, which on a cold start
#: is minutes and is not a browser timeout.
ENSURING_WANGP = "ensuring_wangp"
#: Reading the settings this job will run at, from the child, with no page.
COMPOSING = "composing"
#: WanGP is ready and something else is generating on the card - the user's
#: own work, or another of ours. An honest, published wait rather than a
#: silent stall, and never a pre-emption.
WAITING_FOR_CARD = "waiting_for_card"
#: Handing the immutable task to WanGP's own queue.
SUBMITTING_WANGP = "submitting_wangp"
#: Accepted, waiting ahead of generation.
GENERATION_WAITING = "wangp_waiting"
#: Generating.
GENERATION_RUNNING = "wangp_generating"
#: Terminal success; generated file paths persisted.
COMPLETED = "completed"
#: The rare safety state. A submission was recorded and its outcome was not,
#: so nothing here may decide it did not happen. Never auto-duplicated.
EXECUTION_UNKNOWN = "execution_unknown"

STATES = (
    ENHANCING, PENDING, SENDING, QUEUED, STARTED, FAILED, UNCONFIRMED, CANCELLED,
    ADMITTED, WAITING_TURN, ENHANCED, ENSURING_WANGP, COMPOSING, WAITING_FOR_CARD,
    SUBMITTING_WANGP, GENERATION_WAITING, GENERATION_RUNNING, COMPLETED, EXECUTION_UNKNOWN,
)
#: A browser-executed job ends when WanGP has taken it; that is as far as a
#: page could ever see. A server-executed one ends when the generation does.
LEGACY_TERMINAL = (QUEUED, STARTED, FAILED, UNCONFIRMED, CANCELLED)
TERMINAL = LEGACY_TERMINAL + (COMPLETED, EXECUTION_UNKNOWN)
POSITIVE = (QUEUED, STARTED, COMPLETED)
#: Not yet handed to a page: the part of the line that keeps its order.
WAITING = (ENHANCING, PENDING)
#: Every stage a server-executed job passes through before it is terminal.
#: The executor's whole working set, and the states restart recovery reads.
SERVER_ACTIVE = (
    ADMITTED, WAITING_TURN, ENHANCING, ENHANCED, ENSURING_WANGP, COMPOSING,
    WAITING_FOR_CARD, SUBMITTING_WANGP, GENERATION_WAITING, GENERATION_RUNNING,
)
SERVER_TERMINAL = (COMPLETED, FAILED, CANCELLED, EXECUTION_UNKNOWN)
#: The stages after which an external submission exists and a retry would be
#: a second generation. Recovery reconciles these against the child's ledger
#: and never resubmits one on its own.
SERVER_SUBMITTED = (SUBMITTING_WANGP, GENERATION_WAITING, GENERATION_RUNNING)

#: Who advances a job. A document with neither is a legacy one, because that
#: is all there used to be.
EXECUTOR_BROWSER = "browser"
EXECUTOR_SERVER = "server"
EXECUTORS = (EXECUTOR_BROWSER, EXECUTOR_SERVER)

#: What a stage says to a screen when the job has nothing more specific.
STAGE_TEXT = {
    ADMITTED: "Queued on the server. You can close this page.",
    WAITING_TURN: "Waiting its turn.",
    ENHANCING: "Writing the enhanced prompt.",
    ENHANCED: "The enhanced prompt is ready.",
    ENSURING_WANGP: "Starting WanGP.",
    COMPOSING: "Reading WanGP's settings for this model.",
    WAITING_FOR_CARD: "Waiting: WanGP is busy with its own work.",
    SUBMITTING_WANGP: "Handing it to WanGP.",
    GENERATION_WAITING: "In WanGP's queue.",
    GENERATION_RUNNING: "Generating.",
    COMPLETED: "Done.",
    EXECUTION_UNKNOWN: "Whether WanGP ran this could not be proved.",
}

ORIGIN_CLIPBOARD = "clipboard"
ORIGIN_API = "api"
ORIGINS = (ORIGIN_CLIPBOARD, ORIGIN_API)

#: Where a job's task is inside WanGP once WanGP has it, as the page that
#: queued it reports (protocol 5's track). "accepted" is the confirmation
#: alone; "finished" and "unknown" stick.
WANGP_ACCEPTED = "accepted"
WANGP_WAITING = "waiting"
WANGP_GENERATING = "generating"
WANGP_FINISHED = "finished"
WANGP_UNKNOWN = "unknown"
WANGP_STATES = (WANGP_ACCEPTED, WANGP_WAITING, WANGP_GENERATING, WANGP_FINISHED, WANGP_UNKNOWN)

#: A claimed job must be reported within this; a whole enqueue is bounded at
#: about forty seconds, so twice that is a page that went away.
LEASE_SECONDS = 90.0
#: A page that claimed within this long is pumping; the head job of a page
#: that has not is skipped in favour of a page that is asking.
PAGE_ACTIVE_SECONDS = 15.0
#: How long a page is told to wait before asking again: while another page's
#: job is being sent, while it is another page's turn, and while the head of
#: the line is still having its prompt written.
WAIT_BUSY_MS = 400
WAIT_TURN_MS = 250
WAIT_ENHANCE_MS = 1000
#: How often the server looks at the LLM side on its own while a
#: browser-executed job is enhancing, so a finished prompt is collected even
#: when no page is asking.
#:
#: THIS IS THE LEGACY PATH'S ONLY BACKGROUND DRIVER, and it is not the thing
#: the performance work set out to delete. A server-executed job never enters
#: it - its enhancement is followed on the owning extension's own event feed,
#: by the executor, with no full-document read at all - and what is left here
#: drives expired leases and the enhancement of jobs a page still owns. It
#: goes when the browser-executed path does.
WATCH_SECONDS = 2.0
#: Bounds. Terminal jobs are kept a week for the list; waiting ones are
#: capped so a runaway caller cannot fill the disk with requests.
MAX_JOBS = 500
MAX_PENDING = 200
#: How long a finished job stays in the queue before it is dropped.
#:
#: Short, because the queue is a queue and not a record: what is finished has
#: left it, and the history list is where finished work lives. It is not zero
#: because a page that was waiting on the job still has to be told what
#: happened, and history has to be written from the job before it goes.
KEEP_TERMINAL_SECONDS = 120.0

PAGE_RE = re.compile(r"\A[0-9a-f]{8,32}\Z")
PHASE_SENT = "sent"
PHASE_DONE = "done"
PHASES = (PHASE_SENT, PHASE_DONE)

_LOG_PREFIX = "MiniPaint Clipboard:"

_lock = threading.RLock()
_seams: typing.Dict[str, typing.Any] = {"clock": time.time, "running": None, "watcher": True, "executor": None}
#: When each page last claimed. In memory on purpose: after a Forge restart
#: every page is "not asking" until it asks again, which is the truth.
_pages: typing.Dict[str, float] = {}
_watch: typing.Dict[str, typing.Any] = {"thread": None}


# ---------------------------------------------------------------- seams --


def use_clock(clock: typing.Optional[typing.Callable[[], float]]) -> None:
    """Test seam: the wall clock the leases and the ages are measured on."""
    _seams["clock"] = clock or time.time


def use_running(running: typing.Optional[typing.Callable[[], bool]]) -> None:
    """Test seam: whether the managed WanGP is running. None restores the runtime's answer."""
    _seams["running"] = running


def use_watcher(enabled: bool) -> None:
    """Test seam: whether a background thread follows enhancing jobs. Tests
    drive ``refresh`` themselves rather than racing a thread."""
    _seams["watcher"] = bool(enabled)


def use_executor(executor: typing.Optional[str]) -> None:
    """Which executor new jobs get. None restores the setting's own answer.

    A seam rather than a constant because the two paths coexist for a
    compatibility window (B13): an already-loaded page still claims and
    reports browser-executed jobs while every new one is run by the server.
    Tests of the legacy path say so explicitly rather than depending on which
    way the default happens to point this week.
    """
    _seams["executor"] = executor if executor in EXECUTORS else None


def unattended_enabled() -> bool:
    """Whether this Forge runs queued jobs itself. The product setting.

    Off means the browser executes, exactly as it did before: a press is
    refused while WanGP is not running, a page claims its own jobs, and
    walking away stops the queue. On is the whole point of the feature, and
    it is what a fresh install gets.
    """
    try:
        from .. import settings

        return bool(settings.unattended_queue())
    except Exception:
        return True


def chosen_executor() -> str:
    """Which executor a new job gets, seam first and setting second."""
    forced = _seams.get("executor")
    if forced in EXECUTORS:
        return forced
    return EXECUTOR_SERVER if unattended_enabled() else EXECUTOR_BROWSER


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
    _seams["watcher"] = False
    # The legacy path, explicitly, so a suite written for it is not reading
    # whichever way the product default points. A suite for the server path
    # calls ``use_executor(EXECUTOR_SERVER)`` and says so.
    _seams["executor"] = EXECUTOR_BROWSER
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


def _enhancer():
    from . import enhance

    return enhance


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


def sanitize_track(raw: typing.Any) -> dict:
    """What a page may say about where a queued job's task is in WanGP: one
    of the track states and two counts. Anything else reads as unknown."""
    raw = raw if isinstance(raw, dict) else {}
    state = raw.get("state")
    if state == protocol.TRACK_WAITING:
        state = WANGP_WAITING
    elif state == protocol.TRACK_GENERATING:
        state = WANGP_GENERATING
    elif state == protocol.TRACK_FINISHED:
        state = WANGP_FINISHED
    else:
        state = WANGP_UNKNOWN
    return {"state": state, "position": _whole(raw.get("position")), "queue_depth": _whole(raw.get("queue_depth"))}


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


def _model(raw: typing.Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {key: str(raw.get(key) or "")[:120] for key in ("type", "label", "family", "architecture")}


def _normalize_enhance(raw: typing.Any) -> typing.Optional[dict]:
    """The enhancement half of a job as stored, or None for a plain job."""
    if not isinstance(raw, dict) or not isinstance(raw.get("llm_id"), str) or not raw.get("llm_id"):
        return None
    enhancer = _enhancer()
    state = raw.get("state") if raw.get("state") in enhancer.LLM_STATES + ("lost",) else enhancer.LLM_QUEUED
    slots = enhancer.SLOTS
    return {
        "variant": raw.get("variant") if raw.get("variant") in enhancer.VARIANTS else enhancer.FL2VA,
        "llm_id": str(raw["llm_id"])[:64],
        "state": state,
        "stage": str(raw.get("stage") or "")[:160],
        "position": _whole(raw.get("position")) or 0,
        "elapsed": float(raw.get("elapsed") or 0.0),
        "image_used": raw.get("image_used") if raw.get("image_used") in slots else "",
        "image_ignored": [item for item in (raw.get("image_ignored") or []) if item in slots],
        "dropped": [item for item in (raw.get("dropped") or []) if item in protocol.QUEUE_FIELDS],
        "extra_references": _whole(raw.get("extra_references")) or 0,
        "system_override": bool(raw.get("system_override")),
        "prompt_original": raw.get("prompt_original") if isinstance(raw.get("prompt_original"), str) else "",
        "error": str(raw.get("error") or "")[:200],
        "submitted_at": float(raw.get("submitted_at") or 0.0),
        "finished_at": float(raw.get("finished_at") or 0.0),
        "reused_from": str(raw.get("reused_from") or "")[:16],
    }


def _normalize_wangp(raw: typing.Any) -> typing.Optional[dict]:
    if not isinstance(raw, dict) or raw.get("state") not in WANGP_STATES:
        return None
    return {
        "state": raw["state"],
        "position": _whole(raw.get("position")),
        "queue_depth": _whole(raw.get("queue_depth")),
        "seen_at": float(raw.get("seen_at") or 0.0),
    }


def _normalize_inputs(raw: typing.Any) -> dict:
    """The job's own durable images, by slot. Ids only, never a path.

    A job owns its pictures from admission: whatever handle composed it -
    a staging token that expires, an asset the user may rename - is resolved
    once and copied into a pinned object of the job's own. This records which
    object is in which slot; ``job_inputs`` records the pin.
    """
    raw = raw if isinstance(raw, dict) else {}
    out: typing.Dict[str, typing.Any] = {}
    for slot in (protocol.EXEC_SLOT_START, protocol.EXEC_SLOT_END):
        value = raw.get(slot)
        if protocol.valid_handoff_id(value):
            out[slot] = value
    references = raw.get(protocol.EXEC_SLOT_REFERENCES)
    if isinstance(references, (list, tuple)):
        kept = [item for item in references if protocol.valid_handoff_id(item)][: protocol.MAX_QUEUE_REFERENCES]
        if kept:
            out[protocol.EXEC_SLOT_REFERENCES] = kept
    return out


def input_ids(job: typing.Mapping[str, typing.Any]) -> typing.List[str]:
    """Every durable input a job holds, flattened. What its pin covers."""
    inputs = job.get("inputs") if isinstance(job.get("inputs"), dict) else {}
    found: typing.List[str] = []
    for slot in (protocol.EXEC_SLOT_START, protocol.EXEC_SLOT_END):
        if inputs.get(slot):
            found.append(inputs[slot])
    found.extend(inputs.get(protocol.EXEC_SLOT_REFERENCES) or [])
    return found


def _normalize_snapshot(raw: typing.Any) -> typing.Optional[dict]:
    """The immutable execution snapshot: what this job runs as, and from when.

    The settings base is not captured at press. It cannot be: a job may be
    admitted with the WanGP child stopped, and cold start is a server-side
    stage that happens after admission - so at press there may be no process,
    no session and no settings to read. It is composed at the WanGP stage
    instead, from the process-wide record of what the user last committed for
    that model, and *then* frozen. ``captured_at`` is when that happened and
    the queue says so, because "the settings as of when it ran" and "the
    settings as of when you pressed" are different promises and only one of
    them is being made.
    """
    if not isinstance(raw, dict):
        return None
    settings = raw.get("settings")
    source = raw.get("source") if raw.get("source") in protocol.BASE_SOURCES else ""
    return {
        "schema": int(raw.get("schema") or 1),
        "model_type": str(raw.get("model_type") or "")[:200],
        "settings": dict(settings) if isinstance(settings, dict) else {},
        "source": source,
        "captured_at": float(raw.get("captured_at") or 0.0),
        "wan2gp_version": str(raw.get("wan2gp_version") or "")[:40],
        "residency_key": str(raw.get("residency_key") or "")[:200],
        # Protocol 4's generate-versus-queue choice is about WanGP's WebUI
        # queue and has no meaning for a server-executed job: every one of
        # them goes into that same queue and is drained by the one worker.
        # Kept in the schema so an old document still reads; never surfaced.
        "start_mode_legacy": str(raw.get("start_mode_legacy") or "")[:16],
    }


def _normalize_execution(raw: typing.Any) -> typing.Optional[dict]:
    """The child's own last word about this job's generation."""
    if not isinstance(raw, dict) or not protocol.valid_execution_id(raw.get("execution_id")):
        return None
    return protocol.normalize_execution_record(raw)


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
    enhance = _normalize_enhance(raw.get("enhance"))
    if state == ENHANCING and enhance is None:
        # A job that says it is enhancing without a request to wait for
        # would wait forever; it is failed, honestly, rather than kept.
        state = FAILED
    # A document written before server execution existed carries no executor
    # and no server states, so it reads as exactly what it was. This is the
    # read-side migration: the old vocabulary was not replaced, so an old
    # document cannot be misread by the new scheme.
    executor = raw.get("executor") if raw.get("executor") in EXECUTORS else EXECUTOR_BROWSER
    if state in SERVER_ACTIVE and state not in (ENHANCING,):
        executor = EXECUTOR_SERVER
    files = raw.get("generated_files")
    generated = [str(item)[:400] for item in files if isinstance(item, str) and item][:64] if isinstance(files, (list, tuple)) else []
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
        "model": _model(raw.get("model")),
        "enhance_requested": bool(raw.get("enhance_requested")),
        "enhance": enhance,
        "wangp": _normalize_wangp(raw.get("wangp")),
        # -- the server-executed half ---------------------------------------
        "executor": executor,
        #: Bumped by every durable transition. A stage writes only if the job
        #: is still at the revision it read, so a cancel or a retry that
        #: landed in between is never clobbered by a whole-document write.
        "revision": int(raw.get("revision") or 0),
        "stage": str(raw.get("stage") or "")[:200],
        "execution_id": raw["execution_id"] if protocol.valid_execution_id(raw.get("execution_id")) else "",
        "execution": _normalize_execution(raw.get("execution")),
        "snapshot": _normalize_snapshot(raw.get("snapshot")),
        "inputs": _normalize_inputs(raw.get("inputs")),
        "generated_files": generated,
        #: Which run of the WanGP child holds this job's generation. A
        #: mismatch after a restart is what makes the difference between
        #: re-attaching and admitting that nobody can say.
        "child_instance": str(raw.get("child_instance") or "")[:64],
        "inputs_released": bool(raw.get("inputs_released")),
        #: What the pressing page managed to do about WanGP's live form. Not
        #: an instruction and not a state - only the provenance of the base
        #: this job will compose at, kept so the queue can tell "the settings
        #: you were looking at" from "the settings WanGP had recorded".
        "settings_flush": (str(raw.get("settings_flush") or "")
                           if raw.get("settings_flush") in protocol.FLUSH_OUTCOMES else ""),
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
        "model": dict(job.get("model") or {}),
        "enhance_requested": bool(job.get("enhance_requested")),
        "enhance": dict(job["enhance"]) if job.get("enhance") else None,
        "wangp": dict(job["wangp"]) if job.get("wangp") else None,
        # -- the server-executed half ---------------------------------------
        # The snapshot's settings do NOT cross: they are the user's whole
        # WanGP configuration, they are several kilobytes, and no screen
        # needs them. What a screen needs is which model, from where, and
        # when - so a job that ran at factory defaults says so rather than
        # looking like one that ran at the settings its owner had chosen.
        "executor": job.get("executor", EXECUTOR_BROWSER),
        "revision": int(job.get("revision") or 0),
        "stage": job.get("stage") or STAGE_TEXT.get(job["state"], ""),
        "execution_id": job.get("execution_id", ""),
        "execution": dict(job["execution"]) if job.get("execution") else None,
        "snapshot": _public_snapshot(job.get("snapshot")),
        "inputs": {slot: value for slot, value in (job.get("inputs") or {}).items()},
        # The count crosses; the paths do not. Section 6.4's rule holds for a
        # job document read over a route exactly as it does for an event.
        "generated_count": len(job.get("generated_files") or []),
        # One of five fixed words, so it carries nothing of the user's and
        # crosses freely. It is the other half of the snapshot's ``source``:
        # together they say whether the base this job ran at is the one its
        # owner was looking at when they pressed.
        "settings_flush": job.get("settings_flush", ""),
    }


def _public_snapshot(raw: typing.Any) -> typing.Optional[dict]:
    """What a screen may know about a job's snapshot. Never its settings."""
    if not isinstance(raw, dict):
        return None
    return {
        "model_type": raw.get("model_type", ""),
        "source": raw.get("source", ""),
        "captured_at": _iso(raw["captured_at"]) if raw.get("captured_at") else "",
        "wan2gp_version": raw.get("wan2gp_version", ""),
        "residency_key": raw.get("residency_key", ""),
        "settings_count": len(raw.get("settings") or {}),
    }


# ----------------------------------------------------------------- rules --


def _blank(job_id: str, now: float) -> dict:
    """One job document with every field present. The shape ``_normalize``
    expects, written once so a new field is added in one place."""
    return {
        "job_id": job_id,
        "request": {},
        "page": "",
        "origin": ORIGIN_API,
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
        "model": _model(None),
        "enhance_requested": False,
        "enhance": None,
        "wangp": None,
        "executor": EXECUTOR_BROWSER,
        "revision": 0,
        "stage": "",
        "execution_id": "",
        "execution": None,
        "snapshot": None,
        "inputs": {},
        "generated_files": [],
        "child_instance": "",
        "inputs_released": False,
        "settings_flush": "",
    }


def _fail(job: dict, code: str, message: str, now: float, state: str = FAILED) -> None:
    job["state"] = state
    job["error"] = {"code": code, "message": str(message or errors.message(code))[:200]}
    job["lease"] = None
    job["updated_at"] = now
    job["revision"] = int(job.get("revision") or 0) + 1
    job["stage"] = ""


def _advance(jobs: typing.List[dict], now: float) -> bool:
    """Every enhancing job, brought up to date with the LLM side.

    A prompt that is done makes the job pending, carrying it; a run that
    failed or was cancelled (in LLM Studio's own panel, too) ends the job
    with the reason; a record the API has forgotten - retention ran out, or
    Forge restarted and the API's memory with it - is a lost enhancement,
    said so, never a silent retry. Returns whether anything changed.
    """
    changed = False
    enhancer = None
    for job in jobs:
        if job["state"] != ENHANCING or not job.get("enhance"):
            continue
        if enhancer is None:
            enhancer = _enhancer()
        record = job["enhance"]
        found = enhancer.status(record["llm_id"])
        if found is None:
            record["state"] = "lost"
            record["finished_at"] = now
            _fail(job, errors.ENHANCE_LOST, errors.message(errors.ENHANCE_LOST), now)
            _journal(f"job {job['job_id'][:8]}: the enhancement record is gone; failed (ENHANCE_LOST)")
            changed = True
            continue
        progress = {
            "stage": found["stage"], "position": found["position"], "elapsed": float(int(found["elapsed"])),
            "image_used": found["image_used"], "image_ignored": list(found["image_ignored"]), "system_override": found["system_override"],
        }
        moved = any(record.get(key) != value for key, value in progress.items()) or record.get("state") != found["state"]
        if moved:
            record.update(progress)
            changed = True
        if found["state"] in (enhancer.LLM_QUEUED, enhancer.LLM_RUNNING):
            if record.get("state") != found["state"]:
                record["state"] = found["state"]
                job["updated_at"] = now
            continue
        record["state"] = found["state"]
        record["finished_at"] = now
        record["elapsed"] = found["elapsed"]
        changed = True
        if found["state"] == enhancer.LLM_DONE:
            prompt = protocol.clean_prompt(found["prompt"])
            if prompt is None:
                record["error"] = "the writer returned an empty prompt"
                _fail(job, errors.ENHANCE_FAILED, "The enhancer returned an empty prompt.", now)
                _journal(f"job {job['job_id'][:8]}: enhancement returned nothing; failed")
            elif len(prompt) > protocol.PROMPT_MAX_CHARS:
                record["error"] = f"{len(prompt)} characters"
                _fail(job, errors.PROMPT_TOO_LONG, errors.message(errors.PROMPT_TOO_LONG), now)
                _journal(f"job {job['job_id'][:8]}: the enhanced prompt is {len(prompt)} characters, over the ceiling; failed")
            else:
                job["request"]["prompt"] = prompt
                # The enhanced prompt is persisted *before* the WanGP stage
                # begins, which is B4 STEP 5: a job that reached WanGP with
                # the typed prompt because the write had not landed yet
                # would generate the wrong thing and look like it worked.
                job["state"] = ENHANCED if job.get("executor") == EXECUTOR_SERVER else PENDING
                job["stage"] = STAGE_TEXT.get(job["state"], "")
                job["revision"] = int(job.get("revision") or 0) + 1
                job["error"] = None
                job["updated_at"] = now
                described = f"described {record['image_used'].replace('_', ' ')}" if record.get("image_used") else "no picture"
                _journal(f"job {job['job_id'][:8]}: enhanced ({record['variant']}, {described}) in {found['elapsed']:.0f}s; pending")
        elif found["state"] == enhancer.LLM_FAILED:
            record["error"] = found["error"]
            _fail(job, errors.ENHANCE_FAILED, found["error"] or errors.message(errors.ENHANCE_FAILED), now)
            _journal(f"job {job['job_id'][:8]}: enhancement failed")
        else:
            record["error"] = found["reason"]
            _fail(job, errors.ENHANCE_CANCELLED, found["reason"] or errors.message(errors.ENHANCE_CANCELLED), now, state=CANCELLED)
            _journal(f"job {job['job_id'][:8]}: enhancement cancelled on the LLM side")
    return changed


def _sweep(jobs: typing.List[dict], now: float) -> bool:
    """Expired leases, the LLM side, and old records. Returns whether anything changed."""
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
    if _advance(jobs, now):
        changed = True
    kept = [job for job in jobs if not _finished_with(job, now)]
    while len(kept) > MAX_JOBS:
        victim = next((job for job in kept if job["state"] in TERMINAL), None)
        if victim is None:
            break
        kept.remove(victim)
    if len(kept) != len(jobs):
        jobs[:] = kept
        changed = True
    return changed


def _finished_with(job: dict, now: float) -> bool:
    """Whether a finished job has been in the list long enough to go.

    The grace is not politeness: a page waiting on this job still has to be
    told what happened, and a caller of the public API is holding a promise
    that resolves from this record. Dropping it the instant it goes terminal
    turns "your job finished" into QUEUE_JOB_UNKNOWN.

    ``history_recorded`` deliberately does NOT shorten this, tempting as it
    looks. It is set when a job is handed to WanGP, not when it comes back -
    so a job would leave the queue at the moment it started running.
    """
    return job["state"] in TERMINAL and now - job["updated_at"] > KEEP_TERMINAL_SECONDS


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


# ------------------------------------------------------------- the watcher --


def _ensure_watcher() -> None:
    """A daemon thread that keeps looking at enhancing jobs until none is
    left, so a prompt finished while every page is idle is still collected
    before the API forgets it. One at a time; none in tests."""
    if not _seams.get("watcher", True):
        return
    with _lock:
        thread = _watch.get("thread")
        if thread is not None and thread.is_alive():
            return
        thread = threading.Thread(target=_watch_loop, name="minipaint-clipboard-enhance", daemon=True)
        _watch["thread"] = thread
        thread.start()


def _watch_loop() -> None:
    try:
        while True:
            time.sleep(WATCH_SECONDS)
            with _lock:
                listed = _load()
                if _sweep(listed, _now()):
                    _save(listed)
                if not any(job["state"] == ENHANCING for job in listed):
                    return
    except Exception as error:  # pragma: no cover - a watcher dying is a note, and the next press starts another
        _journal(f"the enhancement watcher stopped ({type(error).__name__}); the next press or refresh resumes")
    finally:
        with _lock:
            if _watch.get("thread") is threading.current_thread():
                _watch["thread"] = None


# -------------------------------------------------------------------- api --


def refresh() -> dict:
    """Bring the document up to date with the LLM side and the clock, and
    say how many jobs are in each state. What a page's refresh calls."""
    with _lock:
        listed = _load()
        if _sweep(listed, _now()):
            _save(listed)
    out = {state: 0 for state in STATES}
    for job in listed:
        out[job["state"]] += 1
    return out


def jobs() -> typing.List[dict]:
    """Every job, oldest first, after the expired leases have been settled."""
    with _lock:
        listed = _load()
        if _sweep(listed, _now()):
            _save(listed)
        return [public(job) for job in listed]


def get(job_id: typing.Any) -> typing.Optional[dict]:
    with _lock:
        listed = _load()
        if _sweep(listed, _now()):
            _save(listed)
        for job in listed:
            if job["job_id"] == job_id:
                return public(job)
    return None


def pending_count(page: typing.Optional[str] = None) -> int:
    """How many jobs are still waiting to be handed out - enhancing or
    pending - for one page or for all."""
    with _lock:
        return sum(1 for job in _load() if job["state"] in WAITING and (page is None or job["page"] == page))


def submit(
    request: typing.Any,
    page: typing.Any,
    origin: str = ORIGIN_CLIPBOARD,
    require_running: bool = True,
    enhance: typing.Optional[bool] = None,
    model: typing.Any = None,
    executor: typing.Optional[str] = None,
    settings_flush: str = "",
) -> dict:
    """Append a job. The request is normalised here, so a bad one is refused
    before it is stored.

    ``enhance`` None means the tab's switch decides; True or False is a
    caller's own choice. ``model`` is the WanGP model the pressing page is
    on, as the bridge described it - what the H3 variant is chosen from.
    An enhanced press asks the LLM side *before* the job is stored, inside
    the lock, so that a refusal there stores nothing and a stored job always
    has a request to wait for.

    ``executor`` decides who advances it, and the difference is the whole
    feature:

    *   ``browser`` is what this used to be. A page claims the job, overlays
        the live WanGP form and reports back, so a press while WanGP is not
        running is refused rather than stored - there is nothing for a page
        to drive.

    *   ``server`` is unattended. Forge advances the job itself, and starting
        a cold WanGP is one of the stages it does: a press while WanGP is
        stopped is therefore **admitted**, not refused, which is exactly the
        case the old rule existed to prevent and the new design exists to
        serve. Its images are copied into durable objects of its own and
        pinned at admission, because the handles it was composed from - a
        staging token, an asset the user may rename - belong to somebody
        else's lifetime and this job may outlive both.

    ``settings_flush`` is what the pressing page managed to do about WanGP's
    live form before it got here - see ``protocol.FLUSH_*``. It changes
    nothing about how the job runs; it is recorded so that the base the job
    composes at can be attributed honestly. "The settings you were looking
    at" and "the settings WanGP had recorded" are different promises, and a
    queue that cannot tell them apart cannot keep either.
    """
    from .. import interop

    normalised = interop.normalize_public_request(request)
    page_id = _page(page)
    if origin not in ORIGINS:
        origin = ORIGIN_API
    who = executor if executor in EXECUTORS else chosen_executor()
    server = who == EXECUTOR_SERVER
    if require_running and not server and not wangp_running():
        raise IntegrationError(errors.WANGP_NOT_RUNNING, "the managed WanGP is not serving")
    enhancer = _enhancer()
    wanted = enhancer.enabled() if enhance is None else bool(enhance)
    block = _model(model)
    planned = enhancer.plan(normalised, block) if wanted else None
    # The pictures, before the lock and before the document: resolving a
    # handle can read a file and decode an image, and the outbox lock is not
    # something to hold while that happens. A pin written for a job that then
    # fails to store is an orphan the sweeper takes in an hour; the other
    # order - a stored job whose pictures were never pinned - is the failure.
    owned: typing.Dict[str, typing.Any] = {}
    if server:
        owned = _adopt_inputs(normalised)
    try:
        return _store(
            normalised, page_id, origin, who, block, wanted, planned, owned, enhancer,
            settings_flush if settings_flush in protocol.FLUSH_OUTCOMES else "",
        )
    except Exception:
        if owned:
            _release_inputs(_flatten_inputs(owned))
        raise


def _flatten_inputs(inputs: typing.Mapping[str, typing.Any]) -> typing.List[str]:
    found = [inputs[slot] for slot in (protocol.EXEC_SLOT_START, protocol.EXEC_SLOT_END) if inputs.get(slot)]
    found.extend(inputs.get(protocol.EXEC_SLOT_REFERENCES) or [])
    return found


def _adopt_inputs(request: typing.Mapping[str, typing.Any]) -> typing.Dict[str, typing.Any]:
    """Every handle in a request, as durable objects this job owns."""
    from . import job_inputs

    images = request.get("images") if isinstance(request.get("images"), dict) else {}
    owned: typing.Dict[str, typing.Any] = {}
    for field, slot in (
        (protocol.QUEUE_FIELD_START, protocol.EXEC_SLOT_START),
        (protocol.QUEUE_FIELD_END, protocol.EXEC_SLOT_END),
    ):
        handle = images.get(field)
        if handle:
            owned[slot] = job_inputs.adopt(handle, slot=slot)["input_id"]
    references = images.get(protocol.QUEUE_FIELD_REFERENCES) or []
    if references:
        owned[protocol.EXEC_SLOT_REFERENCES] = [
            job_inputs.adopt(handle, slot=protocol.EXEC_SLOT_REFERENCES)["input_id"] for handle in references
        ]
    return owned


def _release_inputs(ids: typing.Iterable[str]) -> int:
    try:
        from . import job_inputs

        return job_inputs.release(ids)
    except Exception:
        return 0


def _store(
    normalised: dict,
    page_id: str,
    origin: str,
    who: str,
    block: dict,
    wanted: bool,
    planned: typing.Optional[dict],
    owned: typing.Dict[str, typing.Any],
    enhancer: typing.Any,
    settings_flush: str = "",
) -> dict:
    """The stored half of ``submit``: the document, under the lock."""
    server = who == EXECUTOR_SERVER
    with _lock:
        listed = _load()
        now = _now()
        _sweep(listed, now)
        if sum(1 for job in listed if job["state"] in WAITING or job["state"] == SENDING) >= MAX_PENDING:
            raise IntegrationError(errors.QUEUE_BUSY, f"{MAX_PENDING} requests are already waiting")
        job = _blank(secrets.token_hex(8), now)
        job.update({
            "request": normalised,
            "page": page_id,
            "origin": origin,
            "state": ADMITTED if server else PENDING,
            "model": block,
            "enhance_requested": wanted,
            "executor": who,
            "execution_id": secrets.token_hex(16) if server else "",
            "inputs": _normalize_inputs(owned),
            "stage": STAGE_TEXT[ADMITTED] if server else "",
            "settings_flush": settings_flush,
        })
        if planned is not None:
            asked = enhancer.submit(normalised["prompt"], planned)
            job["state"] = ENHANCING
            job["enhance"] = {
                "variant": asked["variant"],
                "llm_id": asked["llm_id"],
                "state": enhancer.LLM_QUEUED,
                "stage": "",
                "position": 0,
                "elapsed": 0.0,
                "image_used": "",
                "image_ignored": [],
                "dropped": list(planned["dropped"]),
                "extra_references": int(planned.get("extra_references") or 0),
                "system_override": bool(asked["system_override"]),
                "prompt_original": normalised["prompt"],
                "error": "",
                "submitted_at": now,
                "finished_at": 0.0,
                "reused_from": "",
            }
        listed.append(job)
        try:
            _save(listed)
        except Exception:
            if planned is not None:
                enhancer.cancel(job["enhance"]["llm_id"], "the outbox could not store the job")
            raise
    if owned:
        # The pins exist already; this is what stops them looking like
        # orphans now that the job they belong to is on disk.
        try:
            from . import job_inputs

            job_inputs.assign(_flatten_inputs(owned), job["job_id"])
        except Exception:
            pass
    if planned is not None and not server:
        _ensure_watcher()
    if server:
        _publish(job)
        _wake_executor()
    summary = summary_of(normalised)
    supplied = [name for name in ("prompt", "start", "end") if summary[name]] + (["references"] if summary["references"] else [])
    waiting = sum(1 for item in listed if item["state"] in WAITING)
    if planned is not None:
        dropped = f"; left out of the enhancement: {', '.join(planned['dropped'])}" if planned["dropped"] else ""
        _journal(f"job {job['job_id'][:8]}: submitted from {origin} (overrides {', '.join(supplied) or 'none'}; start {summary['start_mode']}); "
                 f"enhancing as {planned['variant']} (llm {job['enhance']['llm_id'][:8]}){dropped}; {waiting} waiting")
    else:
        _journal(f"job {job['job_id'][:8]}: submitted from {origin} (overrides {', '.join(supplied) or 'none'}; start {summary['start_mode']}); "
                 f"{waiting} waiting")
    return public(job)


def claim(page: typing.Any) -> dict:
    """The next job for this page, with a lease, or why not yet.

    One lease at a time across every page. The line keeps press order: its
    head is the oldest job not yet handed out, and while that head is still
    having its prompt written nobody is served, whichever page asks. A
    pending head goes to its own page; a page that has stopped asking does
    not hold the others up, and its jobs wait for it - but a page never
    skips one of its own that is still enhancing.
    """
    page_id = _page(page)
    with _lock:
        listed = _load()
        now = _now()
        changed = _sweep(listed, now)
        _pages[page_id] = now

        def answer(payload: dict) -> dict:
            if changed:
                _save(listed)
            return payload

        # Browser-executed jobs only, and in a *separate* list: ``listed`` is
        # what gets written back, so filtering it in place would save a
        # document with every server-executed job deleted. A server job is
        # not a page's to run, is not in this line at all, and must not be
        # able to hold the head of it - a legacy page waiting behind an
        # unattended job would wait for something it can never be handed.
        theirs = [job for job in listed if job.get("executor") != EXECUTOR_SERVER]
        mine = [job for job in theirs if job["state"] in WAITING and job["page"] == page_id]
        if any(job["state"] == SENDING for job in theirs):
            return answer({"wait": WAIT_BUSY_MS, "reason": "busy", "pending": len(mine)})
        head = next((job for job in theirs if job["state"] in WAITING), None)
        if head is None:
            return answer({"empty": True, "pending": 0})
        if head["state"] == ENHANCING:
            return answer({"wait": WAIT_ENHANCE_MS, "reason": "enhancing", "pending": len(mine), "job_id": head["job_id"]})
        job: typing.Optional[dict]
        if head["page"] == page_id:
            job = head
        else:
            owner_active = now - _pages.get(head["page"], 0.0) <= PAGE_ACTIVE_SECONDS
            if owner_active:
                return answer({"wait": WAIT_TURN_MS, "reason": "turn", "pending": len(mine)})
            if mine and mine[0]["state"] == ENHANCING:
                return answer({"wait": WAIT_ENHANCE_MS, "reason": "enhancing", "pending": len(mine), "job_id": mine[0]["job_id"]})
            job = mine[0] if mine else None
        if job is None:
            elsewhere = sum(1 for item in theirs if item["state"] in WAITING)
            return answer({"empty": True, "pending": 0, "waiting_elsewhere": elsewhere})
        token = secrets.token_hex(16)
        job["state"] = SENDING
        job["lease"] = {"token": token, "page": page_id, "expires_at": now + LEASE_SECONDS}
        job["attempts"] = int(job.get("attempts", 0)) + 1
        job["sent"] = False
        job["updated_at"] = now
        _save(listed)
        _journal(f"job {job['job_id'][:8]}: claimed by a page (attempt {job['attempts']}); {len(mine) - 1} of its own waiting behind it")
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
            depth = result.get("queue_depth")
            if job["state"] == STARTED:
                job["wangp"] = {"state": WANGP_GENERATING, "position": 0, "queue_depth": 0, "seen_at": now}
            else:
                job["wangp"] = {"state": WANGP_WAITING if depth is not None else WANGP_ACCEPTED, "position": depth, "queue_depth": depth, "seen_at": now}
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
        depth_text = f", {result['queue_depth']} ahead" if result.get("queue_depth") is not None else ""
        _journal(f"job {job['job_id'][:8]}: {job['state']}" + (f" ({job['error']['code']})" if job.get("error") else f" via {result.get('route') or 'queue'}{depth_text}"))
        return public(job)


def track(job_id: typing.Any, page: typing.Any, payload: typing.Any = None) -> dict:
    """Where a queued job's task is in WanGP now, as the page that queued it
    saw through the bridge. Only that page may say; only a job WanGP took
    is tracked; "finished" and "unknown" are final."""
    page_id = _page(page)
    with _lock:
        listed = _load()
        now = _now()
        changed = _sweep(listed, now)
        job = _find(listed, job_id)
        if job["page"] != page_id:
            raise IntegrationError(errors.REQUEST_INVALID, f"job {job['job_id'][:8]} was not queued from this page")
        if job["state"] not in POSITIVE:
            if changed:
                _save(listed)
            return public(job)
        current = job.get("wangp") or {}
        if current.get("state") in (WANGP_FINISHED, WANGP_UNKNOWN):
            if changed:
                _save(listed)
            return public(job)
        seen = sanitize_track(payload)
        moved = seen["state"] != current.get("state") or seen["position"] != current.get("position") or seen["queue_depth"] != current.get("queue_depth")
        if moved:
            job["wangp"] = {"state": seen["state"], "position": seen["position"], "queue_depth": seen["queue_depth"], "seen_at": now}
            job["updated_at"] = now
            changed = True
            if seen["state"] != current.get("state"):
                ahead = f" ({seen['position']} ahead)" if seen["state"] == WANGP_WAITING and seen["position"] else ""
                _journal(f"job {job['job_id'][:8]}: WanGP {seen['state']}{ahead}")
        if changed:
            _save(listed)
        return public(job)


def _cancel_server(listed: typing.List[dict], job: dict, now: float) -> dict:
    """Cancel one server-executed job, at whatever stage it is. Lock held.

    Cancellation is explicit and it is authoritative for our jobs, which is
    what makes it safe to do at every stage:

    *   enhancing - the LLM request is cancelled too, cooperatively. A cold
        runtime is not killed to cancel a prompt; that is somebody else's
        process and somebody else's next request.
    *   ensuring WanGP - the child is *not* stopped. It is shared, other jobs
        want it, and one cancelled request is not a reason to take it away.
    *   waiting for the card - the job simply stops waiting. Whatever holds
        the card is untouched; it was never ours to interrupt.
    *   submitted or generating - the child is asked to stop that one task,
        by execution id. It removes ours from the shared queue, or aborts it
        if it is the one running, and never disturbs anything that is not
        ours.

    A cancel that cannot reach the child is still a cancel *here*: the job
    stops, and what may still be running there is recorded honestly rather
    than reported as stopped.
    """
    if job["state"] in TERMINAL:
        return public(job)
    stage = job["state"]
    detail = "Cancelled before it started."
    if stage == ENHANCING and job.get("enhance"):
        try:
            _enhancer().cancel(job["enhance"]["llm_id"])
        except Exception:
            pass
        job["enhance"]["state"] = "cancelled"
        job["enhance"]["finished_at"] = now
        detail = "Cancelled while the prompt was being written."
    elif stage in SERVER_SUBMITTED and job.get("execution_id"):
        detail = _cancel_in_child(job)
    job["state"] = CANCELLED
    job["error"] = {"code": errors.ENHANCE_CANCELLED if stage == ENHANCING else errors.QUEUE_JOB_UNKNOWN, "message": detail}
    job["stage"] = ""
    job["lease"] = None
    job["updated_at"] = now
    job["revision"] = int(job.get("revision") or 0) + 1
    if not job.get("inputs_released"):
        _release_inputs(input_ids(job))
        job["inputs_released"] = True
    _save(listed)
    _journal(f"job {job['job_id'][:8]}: cancelled while {stage}")
    _publish(job)
    return public(job)


def _cancel_in_child(job: typing.Mapping[str, typing.Any]) -> str:
    """Ask the WanGP child to stop this one task. Returns what to record."""
    try:
        from ..wangp import control

        record = control.cancel(job["execution_id"])
    except Exception as error:
        return f"Cancelled here; WanGP could not be told ({type(error).__name__})."
    if record["state"] == protocol.EXEC_CANCELLED:
        return "Cancelled; WanGP stopped it."
    if record["state"] == protocol.EXEC_DONE:
        return "Cancelled after WanGP had already finished it."
    return "Cancelled here; WanGP reported " + (record["state"] or "nothing") + "."


def cancel(job_id: typing.Any) -> dict:
    """A waiting job is cancelled - its enhancement too; one being sent is not ours to stop."""
    with _lock:
        listed = _load()
        now = _now()
        _sweep(listed, now)
        job = _find(listed, job_id)
        if job.get("executor") == EXECUTOR_SERVER:
            return _cancel_server(listed, job, now)
        if job["state"] == SENDING:
            raise IntegrationError(errors.QUEUE_BUSY, f"job {job['job_id'][:8]} is being sent")
        if job["state"] not in WAITING:
            return public(job)
        if job["state"] == ENHANCING and job.get("enhance"):
            _enhancer().cancel(job["enhance"]["llm_id"])
            job["enhance"]["state"] = "cancelled"
            job["enhance"]["finished_at"] = now
            _fail(job, errors.ENHANCE_CANCELLED, "Cancelled before the prompt was enhanced.", now, state=CANCELLED)
            _journal(f"job {job['job_id'][:8]}: cancelled while enhancing")
        else:
            job["state"] = CANCELLED
            job["updated_at"] = now
            _journal(f"job {job['job_id'][:8]}: cancelled while pending")
        _save(listed)
        return public(job)


def cancel_all() -> dict:
    """Everything still waiting is cancelled at once: every enhancement this
    extension asked for, and every pending job. A job being sent is left to
    finish - the bridge has its overlay and will settle it within seconds -
    and is counted so the tab can say so."""
    enhancer = _enhancer()
    with _lock:
        listed = _load()
        now = _now()
        _sweep(listed, now)
        cancelled: typing.List[dict] = []
        in_flight = 0
        enhancing = [job for job in listed if job["state"] == ENHANCING]
        if enhancing:
            enhancer.cancel_all()
        for job in listed:
            if job.get("executor") == EXECUTOR_SERVER:
                if job["state"] in TERMINAL:
                    continue
                _cancel_server(listed, job, now)
                cancelled.append(job)
            elif job["state"] == ENHANCING:
                if job.get("enhance"):
                    job["enhance"]["state"] = "cancelled"
                    job["enhance"]["finished_at"] = now
                _fail(job, errors.ENHANCE_CANCELLED, "Cancelled with the whole queue.", now, state=CANCELLED)
                cancelled.append(job)
            elif job["state"] == PENDING:
                job["state"] = CANCELLED
                job["updated_at"] = now
                cancelled.append(job)
            elif job["state"] == SENDING:
                in_flight += 1
        if cancelled:
            _save(listed)
    _journal(f"cancel all: {len(cancelled)} job(s) cancelled ({len(enhancing)} enhancing); {in_flight} in flight left to finish")
    return {"cancelled": len(cancelled), "enhancing": len(enhancing), "in_flight": in_flight, "jobs": [public(job) for job in cancelled]}


def retry(job_id: typing.Any, page: typing.Any) -> dict:
    """A failed or unconfirmed job, tried again as a *new* job for this page.

    New because the bridge answers a reused request id from its record, and
    for an unconfirmed job that record may say "queued" for a task nobody
    saw. A person pressed this; the machine never does. An enhanced job is
    retried from the prompt that was typed, and enhanced again - except that
    a prompt already written is carried over rather than asked for twice,
    unless the failure was that the page's model had changed, in which case
    it is written again for the model the page is on now.
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
    record = old.get("enhance")
    failed_code = (old.get("error") or {}).get("code")
    reuse = bool(record and record.get("state") == "done" and request.get("prompt") and failed_code != errors.MODEL_CHANGED)
    if record and record.get("prompt_original") and not reuse:
        request["prompt"] = record["prompt_original"]
    job = submit(request, page_id, old["origin"], enhance=(bool(old.get("enhance_requested")) and not reuse), model=old.get("model"))
    with _lock:
        listed = _load()
        for item in listed:
            if item["job_id"] == job["job_id"]:
                item["retry_of"] = old["job_id"]
                if reuse:
                    item["enhance_requested"] = True
                    item["enhance"] = dict(record, reused_from=old["job_id"])
        _save(listed)
    job["retry_of"] = old["job_id"]
    if reuse:
        job["enhance_requested"] = True
        job["enhance"] = dict(record, reused_from=old["job_id"])
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


# ------------------------------------------------- the server executor's api --
#
# Everything below is what ``executor.py`` calls, and it is separated because
# the discipline is different: the executor holds no lock while it does
# anything slow - a process start, a model load, a generation - so every one
# of these is a short, self-contained durable transition rather than a phase
# of a long one.


def _publish(job: typing.Mapping[str, typing.Any]) -> None:
    """Say a job moved, on the shared spine, after it is on disk.

    Never with the outbox lock held for anything that writes a socket, which
    is why the event hub's publish is a counter, a deque append and one wake
    per subscriber and nothing else. Losing this makes a screen stale; it
    cannot stop a job, which is the whole of principle 2.9.
    """
    try:
        from .. import events

        events.publish(
            events.JOB,
            {
                "job_id": job.get("job_id", ""),
                "state": job.get("state", ""),
                "stage": job.get("stage") or STAGE_TEXT.get(job.get("state", ""), ""),
                "executor": job.get("executor", EXECUTOR_BROWSER),
                "attempts": int(job.get("attempts") or 0),
                "revision": int(job.get("revision") or 0),
                "error_code": (job.get("error") or {}).get("code", ""),
                "generated_count": len(job.get("generated_files") or []),
            },
        )
    except Exception:
        pass


def _wake_executor() -> None:
    """Tell the coordinator there is something to do. Never starts one in tests."""
    try:
        from . import executor

        executor.wake()
    except Exception:
        pass


def transition(
    job_id: typing.Any,
    state: str,
    expect_revision: typing.Optional[int] = None,
    stage: typing.Optional[str] = None,
    **fields: typing.Any,
) -> typing.Optional[dict]:
    """Move one job to one state, writing only that job's own fields.

    THE WRITE DISCIPLINE, AND WHY IT IS NOT A DETAIL.

    The only persistence primitive under this module is a whole-document
    read-modify-overwrite. A stage that loaded a job, released the lock, spent
    four minutes loading a model and then wrote the document back would
    silently undo a cancel, a retry or another job's transition that landed
    in between - not rarely, but exactly whenever somebody pressed Cancel
    during a model load, which is precisely when they would.

    So a transition re-reads under the lock and writes only the fields of its
    own job, and ``expect_revision`` lets a caller say which version of that
    job it was working from. A stale write does nothing and answers None, and
    the executor treats that as "somebody else moved this job", which is
    true.

    Returns the job as it now is, or None when the expectation failed or the
    job is gone.
    """
    with _lock:
        listed = _load()
        now = _now()
        job = next((item for item in listed if item["job_id"] == job_id), None)
        if job is None:
            return None
        if expect_revision is not None and int(job.get("revision") or 0) != int(expect_revision):
            return None
        if state not in STATES:
            return None
        was = job["state"]
        job["state"] = state
        job["updated_at"] = now
        job["revision"] = int(job.get("revision") or 0) + 1
        job["stage"] = STAGE_TEXT.get(state, "") if stage is None else str(stage)[:200]
        for key, value in fields.items():
            job[key] = value
        if state in SERVER_TERMINAL and not job.get("inputs_released"):
            # Terminal: the pictures start their retention clock. They are
            # not deleted - a job somebody is about to look at and possibly
            # retry keeps what it ran with - and a job still waiting can
            # never reach here, which is the promise B12 makes.
            _release_inputs(input_ids(job))
            job["inputs_released"] = True
        _save(listed)
        settled = public(job)
    if was != state:
        _journal(f"job {str(job_id)[:8]}: {was} -> {state}" + (f" ({stage})" if stage else ""))
    _publish(job)
    return settled


def next_executable(now: typing.Optional[float] = None) -> typing.Optional[dict]:
    """The one server-executed job to work on next, or None.

    Press order among our own jobs, which is the queue-order contract - and
    it says nothing about what the user has queued in the WanGP tab, which
    interleaves by whoever reaches the shared queue first. One job at a time:
    a second would be a second thing competing for the same card, and the
    card is the resource the whole design is arranged around.

    A job already past submission is returned too, because its stage is
    "follow this to the end" rather than "start it".
    """
    moment = _now() if now is None else float(now)
    with _lock:
        listed = _load()
        if _sweep(listed, moment):
            _save(listed)
        server = [job for job in listed if job.get("executor") == EXECUTOR_SERVER and job["state"] in SERVER_ACTIVE]
        if not server:
            return None
        # Anything already in flight first: it owns the card, or is waiting
        # for it, and starting a second job behind its back is the one thing
        # this queue must never do.
        active = next((job for job in server if job["state"] not in (ADMITTED, WAITING_TURN)), None)
        return public(active if active is not None else server[0])


def server_jobs(states: typing.Optional[typing.Sequence[str]] = None) -> typing.List[dict]:
    """Every server-executed job, oldest first, optionally filtered by state."""
    with _lock:
        listed = _load()
        wanted = tuple(states) if states else None
        return [
            public(job)
            for job in listed
            if job.get("executor") == EXECUTOR_SERVER and (wanted is None or job["state"] in wanted)
        ]


def record_snapshot(job_id: typing.Any, snapshot: typing.Mapping[str, typing.Any], expect_revision: typing.Optional[int] = None) -> typing.Optional[dict]:
    """Freeze the settings this job will run at. Once; never re-composed.

    After this the job runs with these settings whatever a WanGP page holds
    later, which is principle 2.3 and is the only semantics that can survive
    the composer disappearing.
    """
    frozen = _normalize_snapshot(dict(snapshot, captured_at=snapshot.get("captured_at") or _now()))
    return transition(job_id, COMPOSING, expect_revision=expect_revision, snapshot=frozen,
                      stage=f"settings read from WanGP ({(frozen or {}).get('source') or 'unknown'})")


def record_execution(
    job_id: typing.Any,
    record: typing.Mapping[str, typing.Any],
    state: typing.Optional[str] = None,
    expect_revision: typing.Optional[int] = None,
    child_instance: str = "",
) -> typing.Optional[dict]:
    """Persist the child's last word about this job's generation.

    ``generated_files`` lands here and stays here: it is persisted on the job
    for a later viewer to read, and it never crosses to a browser on the
    shared event stream, where only the count and the state go.
    """
    normalised = protocol.normalize_execution_record(record)
    fields: typing.Dict[str, typing.Any] = {"execution": normalised}
    if child_instance:
        fields["child_instance"] = child_instance
    if normalised["generated_files"]:
        fields["generated_files"] = normalised["generated_files"]
    chosen = state or _state_for_record(normalised)
    if normalised["state"] in (protocol.EXEC_FAILED, protocol.EXEC_UNKNOWN) and chosen in (FAILED, EXECUTION_UNKNOWN):
        fields["error"] = {
            "code": normalised["code"] or (errors.EXECUTION_UNKNOWN if chosen == EXECUTION_UNKNOWN else errors.EXECUTION_REFUSED),
            "message": normalised["message"] or errors.message(normalised["code"] or errors.EXECUTION_REFUSED),
        }
    stage = normalised["stage"] or None
    if normalised["state"] == protocol.EXEC_QUEUED and normalised["position"]:
        stage = f"In WanGP's queue, {normalised['position']} ahead."
    return transition(job_id, chosen, expect_revision=expect_revision, stage=stage, **fields)


def _state_for_record(record: typing.Mapping[str, typing.Any]) -> str:
    """The job state one child-side execution state means."""
    return {
        # "accepted" means the child has recorded it and it is in WanGP's own
        # queue - so the job is *waiting*, not still being submitted. Mapping
        # it back to SUBMITTING_WANGP would put the job in the state whose
        # stage submits, and the next pass would hand it over again: harmless
        # only because the child's ledger answers a repeat from the record,
        # and pointless work either way.
        protocol.EXEC_ACCEPTED: GENERATION_WAITING,
        protocol.EXEC_QUEUED: GENERATION_WAITING,
        protocol.EXEC_RUNNING: GENERATION_RUNNING,
        protocol.EXEC_DONE: COMPLETED,
        protocol.EXEC_FAILED: FAILED,
        protocol.EXEC_CANCELLED: CANCELLED,
        protocol.EXEC_UNKNOWN: EXECUTION_UNKNOWN,
    }.get(record.get("state", ""), EXECUTION_UNKNOWN)


def fail(job_id: typing.Any, code: str, message: str = "", expect_revision: typing.Optional[int] = None, state: str = FAILED) -> typing.Optional[dict]:
    """End a server job honestly, with a code somebody can act on."""
    return transition(
        job_id, state, expect_revision=expect_revision, stage="",
        error={"code": code, "message": str(message or errors.message(code))[:200]},
    )


def attempt(job_id: typing.Any) -> typing.Optional[dict]:
    """Count one try at a stage that may be retried. Never a resubmission."""
    with _lock:
        listed = _load()
        job = next((item for item in listed if item["job_id"] == job_id), None)
        if job is None:
            return None
        job["attempts"] = int(job.get("attempts") or 0) + 1
        job["updated_at"] = _now()
        _save(listed)
        return public(job)


def start_session() -> dict:
    """Begin a Forge run with an empty queue. Returns what was let go.

    THE QUEUE IS THIS SESSION'S, AND NOTHING CARRIES INTO THE NEXT ONE.

    It used to be durable across restarts, on the reasoning that a job
    somebody walked away from should survive one. In use that was wrong, and
    wrong in the way that costs trust: a restart brought back prompts from
    hours earlier, re-ran them, and held up the ones that had just been
    pressed. A queue that resurrects work nobody asked for again is worse
    than one that forgets work they did.

    So a run starts empty. What that gives up is real and is written down
    here rather than discovered: a generation that was in flight when Forge
    stopped is no longer reconciled against the child's ledger, and its
    record is gone. It is not *cancelled* - on POSIX the child outlives Forge
    and will finish what it was given, and its output lands in WanGP's own
    gallery as it always did - it is simply no longer tracked here. A person
    who wants it queued again presses it again, which is the thing they can
    do and the thing they would have had to do anyway once the old record
    turned into EXECUTION_UNKNOWN.

    Every pin is released, because a pinned input whose job no longer exists
    is an orphan the sweeper would carry for an hour for no reason.
    """
    with _lock:
        listed = _load()
        count = len(listed)
        if not count:
            return {"cleared": 0, "inputs_released": 0}
        pins: typing.List[str] = []
        for job in listed:
            pins.extend(input_ids(job))
        _save([])
    released = _release_inputs(pins) if pins else 0
    if count:
        _journal(f"a new Forge session starts with an empty queue; {count} job(s) from the last one let go")
    return {"cleared": count, "inputs_released": released}


def recover(child_instance: str = "") -> dict:
    """What a restarting Forge does to the jobs it finds. Returns the counts.

    THE ORDER MATTERS AND IS THE POINT.

    This runs before any sweeper, because a sweep that ran first would
    already have deleted the pictures the pins were about to protect.

    Then each non-terminal server job is put somewhere honest:

    *   Anything before submission simply resumes. Nothing external exists
        yet, so there is nothing ambiguous about starting it again.

    *   Anything after submission is **not** resumed, and this is the rule
        that matters most in the whole module: a resubmission would be a
        second generation on somebody's card. It is reconciled against the
        child's own ledger by execution id, and where that cannot prove an
        outcome the job becomes EXECUTION_UNKNOWN and waits for a person.
        On POSIX the child outlives Forge and the ledger usually can prove
        it; on Windows the child is killed with Forge by deliberate design,
        and EXECUTION_UNKNOWN is the expected, correct answer rather than a
        failure of this code.

    *   A legacy browser-executed job is left exactly as it was. Its page
        may still be open, and its rules are the old ones.
    """
    counted = {"resumed": 0, "reconciled": 0, "unknown": 0, "pinned": 0, "legacy": 0, "legacy_enhancing": 0}
    with _lock:
        listed = _load()
        now = _now()
        pins: typing.List[str] = []
        for job in listed:
            if job.get("executor") != EXECUTOR_SERVER or job["state"] in TERMINAL:
                if job.get("executor") != EXECUTOR_SERVER and job["state"] not in TERMINAL:
                    counted["legacy"] += 1
                continue
            pins.extend(input_ids(job))
            if job["state"] in SERVER_SUBMITTED:
                counted["reconciled"] += 1
                continue
            # Before submission: nothing external exists. A stage that was
            # halfway through starting a process or reading settings simply
            # starts again, and its inputs are still pinned.
            if job["state"] in (ENSURING_WANGP, COMPOSING, WAITING_FOR_CARD, ENHANCED):
                job["state"] = ADMITTED
                job["stage"] = STAGE_TEXT[ADMITTED]
                job["revision"] = int(job.get("revision") or 0) + 1
                job["updated_at"] = now
            counted["resumed"] += 1
        counted["pinned"] = len(pins)
        counted["legacy_enhancing"] = sum(
            1 for job in listed if job.get("executor") != EXECUTOR_SERVER and job["state"] == ENHANCING
        )
        _save(listed)
    if counted["legacy_enhancing"]:
        # The legacy watcher is what drives a browser-executed job's
        # enhancement to a finished prompt and settles an expired lease, and
        # nothing has ever started it at boot - it was only ever started by a
        # press. A Forge that restarts with one of those mid-enhancement used
        # to leave it there until somebody pressed something else.
        _ensure_watcher()
    if counted["resumed"] or counted["reconciled"]:
        _journal(
            f"recovery: {counted['resumed']} job(s) resumed, {counted['reconciled']} awaiting reconciliation, "
            f"{counted['pinned']} pinned input(s), {counted['legacy']} legacy job(s) left alone"
        )
    return counted


def submitted_jobs() -> typing.List[dict]:
    """Server jobs whose submission exists and whose outcome does not.

    What recovery reconciles against the child's ledger, and the only set a
    restart may not simply resume.
    """
    return server_jobs(SERVER_SUBMITTED)


def counts() -> dict:
    """How many jobs are in each state, for a status line."""
    with _lock:
        listed = _load()
    out = {state: 0 for state in STATES}
    for job in listed:
        out[job["state"]] += 1
    return out


__all__ = [
    "ADMITTED", "CANCELLED", "COMPLETED", "COMPOSING", "ENHANCED", "ENHANCING", "ENSURING_WANGP",
    "EXECUTION_UNKNOWN", "EXECUTORS", "EXECUTOR_BROWSER", "EXECUTOR_SERVER", "FAILED",
    "GENERATION_RUNNING", "GENERATION_WAITING", "LEASE_SECONDS", "LEGACY_TERMINAL", "MAX_JOBS",
    "MAX_PENDING", "ORIGIN_API", "ORIGIN_CLIPBOARD", "OUTBOX_NAME", "PAGE_ACTIVE_SECONDS", "PENDING",
    "PHASE_DONE", "PHASE_SENT", "POSITIVE", "QUEUED", "SCHEMA", "SENDING", "SERVER_ACTIVE",
    "SERVER_SUBMITTED", "SERVER_TERMINAL", "STAGE_TEXT", "STARTED", "STATES", "SUBMITTING_WANGP",
    "TERMINAL", "UNCONFIRMED", "WAITING", "WAITING_FOR_CARD", "WAITING_TURN", "WAIT_BUSY_MS",
    "WAIT_ENHANCE_MS", "WAIT_TURN_MS", "WANGP_ACCEPTED", "WANGP_FINISHED", "WANGP_GENERATING",
    "WANGP_STATES", "WANGP_UNKNOWN", "WANGP_WAITING", "adopt", "attempt", "cancel", "cancel_all",
    "chosen_executor", "claim", "counts", "fail", "get", "input_ids", "jobs", "mark_recorded",
    "next_executable", "on_wangp_restart", "pending_count", "public", "record_execution",
    "record_snapshot", "recover", "refresh", "report", "reset_for_tests", "retry", "sanitize_result",
    "sanitize_track", "server_jobs", "submit", "submitted_jobs", "summary_of", "track",
    "transition", "unattended_enabled", "unrecorded", "use_clock", "use_executor", "use_running",
    "use_watcher", "wangp_running",
]
