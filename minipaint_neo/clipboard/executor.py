"""The thing that advances a queued job when nobody is watching.

This is the whole of the authority inversion. Before it, a job in the outbox
was a note to a browser: the page that pressed it claimed it, drove the live
WanGP form, and reported back, so closing the tab stopped the queue and a
phone that locked itself stopped it too. After it, the outbox is work Forge
owns, and the browser's only remaining job is to describe what the user
wants and get a durable acknowledgement before it disappears.

The shape is a single coordinator thread per Forge process, woken by a
condition rather than a timer. It sleeps when the queue is empty and costs
nothing; it does not re-read the outbox every 250 ms to find out whether
anything happened, because the things that make something happen all know
how to say so.

Four rules it does not bend:

*   **The lock is never held for anything slow.** A stage acquires the outbox
    lock to persist a transition and releases it before starting a process,
    loading a model, waiting for a card or running a generation. What that
    costs is that a stage's own view of a job goes stale while it works, and
    what pays for it is ``transition``'s revision check: a write that was
    prepared against a version somebody else has moved past does nothing.

*   **It never blocks the event loop.** Every route in this extension
    enqueues, controls or queries; none of them runs a stage. The coordinator
    is an ordinary worker thread and every call it makes into the WanGP child
    has a finite timeout.

*   **One job at a time.** Not a throughput decision - a correctness one.
    Two jobs advancing at once would be two things competing for one card,
    and the card is the resource this whole design is arranged around. Press
    order holds among our own jobs; against work queued in the WanGP tab it
    interleaves by whoever reaches the shared queue first, and we always
    yield to a generation already running.

*   **It never resubmits across an ambiguity.** A job whose submission exists
    and whose outcome does not is reconciled against the child's ledger by
    execution id, and where that cannot answer, the job becomes
    EXECUTION_UNKNOWN and waits for a person. The cost of being wrong is a
    second generation on somebody's card, and no amount of probability makes
    that an acceptable guess.

The stages, in order, and what each waits for:

    ADMITTED         -> the queue's turn
    ENHANCING        -> ModelSwitchRefiner's own feed (readiness happens
                        inside the run; there is nothing to pre-warm)
    ENHANCED         -> the VRAM handover, if the install needs one
    ENSURING_WANGP   -> the managed child reaching READY, which on a cold
                        start is minutes and is not a browser timeout
    COMPOSING        -> the settings this job runs at, read from the child
    WAITING_FOR_CARD -> whatever is generating now, ours or the user's
    SUBMITTING_WANGP -> the child recording the task
    WANGP_*          -> the generation
"""

from __future__ import annotations

import threading
import time
import typing

from .. import scrub
from ..wangp import errors
from ..wangp import protocol as wire
from ..wangp.errors import IntegrationError
from . import outbox

_LOG_PREFIX = "MiniPaint Clipboard:"

#: How long the coordinator sleeps when it has nothing to do and nobody has
#: woken it. Not a poll: every path that creates work calls ``wake``, and
#: this is the backstop for the one that does not exist yet.
IDLE_SECONDS = 30.0

#: How long a stage waits before looking again at something outside its
#: control. Seconds, never sub-second: these are a process starting, a model
#: loading and somebody else's generation finishing.
POLL_SECONDS = 2.0
#: How often a job waiting for the card looks again. The wait is honest and
#: can be long - as long as the user's own work takes - so it is measured in
#: seconds and is never a retry storm.
CARD_POLL_SECONDS = 3.0
#: How long the managed child is given to reach READY from cold. A model
#: load is minutes; this is the point at which something is actually wrong.
WANGP_READY_TIMEOUT = 20 * 60.0
#: How long a submission may sit un-acknowledged before the job is called
#: unknown rather than waited on.
SUBMIT_TIMEOUT = 120.0

#: A refusal that is the state of the world rather than a mistake. The job
#: waits and tries again instead of failing.
RETRYABLE = frozenset({errors.ENHANCE_QUEUE_FULL, errors.QUEUE_BUSY})
#: How long a retryable refusal backs off for, and how far it grows. Bounded
#: because a queue that is full forever is a configuration problem, not a
#: transient one, and a job should say so rather than retry all night.
BACKOFF_START = 5.0
BACKOFF_MAX = 120.0
MAX_RETRYABLE_ATTEMPTS = 40
#: How long a job is willing to wait for WanGP to stop being busy before it
#: queues behind it anyway.
#:
#: Long enough that the ordinary case - the job before this one still
#: finishing - still serialises neatly and the wait is reported as a wait.
#: Bounded because the signal it waits on is somebody else's live worker
#: handle, and a cancelled generation was observed leaving it set: without a
#: bound the queue stopped for good. Submitting anyway is safe by the same
#: reasoning the whole path rests on - the task is appended to WanGP's own
#: queue, drained by the one worker, and pre-empts nothing.
CARD_WAIT_MAX_SECONDS = 60.0

_lock = threading.RLock()
_wake = threading.Event()
_state: typing.Dict[str, typing.Any] = {"thread": None, "stopping": False, "started": False, "last_error": ""}
_seams: typing.Dict[str, typing.Any] = {"clock": time.time, "sleep": time.sleep, "enabled": True}

#: What a job says when this WanGP can never run it unattended. The next
#: press is routed to the page-driven path, so pressing again is the fix.
NO_SERVICE_MESSAGE = "This WanGP cannot run queued jobs on its own. Press Add to Queue again: it will be sent from this page."


# ----------------------------------------------------------------- seams --


def use_clock(clock: typing.Optional[typing.Callable[[], float]]) -> None:
    _seams["clock"] = clock or time.time


def use_sleep(sleeper: typing.Optional[typing.Callable[[float], None]]) -> None:
    """Test seam: how a stage waits. Tests step the clock instead."""
    _seams["sleep"] = sleeper or time.sleep


def use_thread(enabled: bool) -> None:
    """Test seam: whether ``wake`` starts a coordinator. Tests call ``step``."""
    _seams["enabled"] = bool(enabled)


def reset_for_tests() -> None:
    stop()
    _seams["clock"] = time.time
    _seams["sleep"] = time.sleep
    _seams["enabled"] = False
    _state["last_error"] = ""


def _now() -> float:
    return float(_seams["clock"]())


def _pause(seconds: float) -> None:
    try:
        _seams["sleep"](max(0.0, float(seconds)))
    except Exception:
        pass


def _journal(message: str) -> None:
    try:
        from ..wangp import process_log

        process_log.note("executor", message)
    except Exception:
        pass


# --------------------------------------------------------------- the loop --


def wake() -> None:
    """Something changed; look again. Starts the coordinator if it is asleep.

    Called from every path that creates work - a press, a cancel, a retry, a
    runtime state change - which is what makes the loop a condition wait
    rather than a poll.
    """
    _wake.set()
    ensure_running()


def ensure_running() -> None:
    """One coordinator per Forge process, started on demand."""
    if not _seams.get("enabled", True):
        return
    with _lock:
        thread = _state.get("thread")
        if thread is not None and thread.is_alive():
            return
        _state["stopping"] = False
        thread = threading.Thread(target=_loop, name="minipaint-clipboard-executor", daemon=True)
        _state["thread"] = thread
        _state["started"] = True
    thread.start()


def stop() -> None:
    """Let the coordinator end. In-flight generations are not disturbed."""
    with _lock:
        _state["stopping"] = True
    _wake.set()


def running() -> bool:
    with _lock:
        thread = _state.get("thread")
        return bool(thread is not None and thread.is_alive())


def _loop() -> None:
    try:
        while True:
            with _lock:
                if _state.get("stopping"):
                    return
            worked = False
            try:
                worked = step()
            except Exception as error:  # pragma: no cover - a coordinator that dies stops the queue
                _state["last_error"] = f"{type(error).__name__}: {error}"
                _journal(f"the coordinator stumbled ({type(error).__name__}); it keeps going")
                scrub.console(f"the queue coordinator hit {type(error).__name__}; it is still running.", _LOG_PREFIX)
                _wake.wait(POLL_SECONDS)
                _wake.clear()
                continue
            if worked:
                continue
            # Nothing to do. Sleep until somebody says otherwise.
            _wake.wait(IDLE_SECONDS)
            _wake.clear()
            with _lock:
                if _state.get("stopping"):
                    return
                if outbox.next_executable() is None:
                    _state["thread"] = None
                    return
    finally:
        with _lock:
            if _state.get("thread") is threading.current_thread():
                _state["thread"] = None


def step() -> bool:
    """Advance the queue by one stage. Returns whether anything was done.

    Public because it is also how a test drives the executor: everything the
    coordinator does is in here, and the thread around it only decides when
    to call it.
    """
    job = outbox.next_executable()
    if job is None:
        return False
    state = job["state"]
    handler = _STAGES.get(state)
    if handler is None:
        return False
    return bool(handler(job))


# -------------------------------------------------------------- the stages --


def _stage_admitted(job: dict) -> bool:
    """A job's turn has come. Where it goes next depends on what it asked for.

    An enhanced job whose prompt is still being written stays where it is:
    the enhancement was submitted at press and its own watcher is following
    it, so this stage does nothing but wait. Everything else goes straight to
    the WanGP stage, and a cold WanGP is part of that stage rather than a
    precondition for it.
    """
    if job.get("enhance_requested") and job.get("enhance") and job["enhance"]["state"] not in ("done", "failed", "cancelled", "lost"):
        outbox.transition(job["job_id"], outbox.ENHANCING, expect_revision=job["revision"],
                          stage=_enhance_stage(job))
        _pause(POLL_SECONDS)
        return True
    if job.get("enhance_requested") and not job.get("enhance"):
        # Enhance was asked for and nothing was submitted: the preflight
        # refused before the press stored anything, or the submission was
        # lost. Either way it is not run silently without the rewrite.
        return bool(
            outbox.fail(job["job_id"], errors.ENHANCE_LOST,
                        "The prompt was never handed to the enhancer.", expect_revision=job["revision"])
        )
    return bool(outbox.transition(job["job_id"], outbox.ENSURING_WANGP, expect_revision=job["revision"]))


def _stage_waiting_turn(job: dict) -> bool:
    return bool(outbox.transition(job["job_id"], outbox.ADMITTED, expect_revision=job["revision"]))


def _enhance_stage(job: typing.Mapping[str, typing.Any]) -> str:
    """The enhancer's own words about where its run is, for a screen.

    Its stage text, not ours: the distinction between "the language model is
    cold" and "the language model is writing" is real, it is something the
    owning extension already reports, and inventing a state for it here would
    be a state MiniPaint can neither enter nor leave observably - which is a
    state that lies in diagnostics.
    """
    record = job.get("enhance") or {}
    text = str(record.get("stage") or "").strip()
    if text:
        return text[:200]
    position = record.get("position") or 0
    if record.get("state") == "queued" and position:
        return f"Waiting for the enhancer: {position} ahead."
    return outbox.STAGE_TEXT[outbox.ENHANCING]


def _stage_enhancing(job: dict) -> bool:
    """Follow the enhancement to a terminal state, server-side.

    The feed does the work; this stage reconciles what it recorded and moves
    the job on. A feed that expires or raises is not a job failure - it is a
    subscription with a TTL - so it is re-read and reconciled against the
    API's own status before anything terminal is concluded.
    """
    from . import enhance

    record = job.get("enhance") or {}
    llm_id = record.get("llm_id") or ""
    if not llm_id:
        return bool(outbox.fail(job["job_id"], errors.ENHANCE_LOST, "no enhancement to wait for",
                                expect_revision=job["revision"]))
    try:
        enhance.follow(llm_id)
    except Exception:
        pass
    # ``refresh`` is what carries a finished prompt into the job and fails it
    # honestly when the enhancement did not finish. It is the same path the
    # legacy watcher uses, so there is one place where an enhancement becomes
    # a prompt and one place where it becomes a failure.
    outbox.refresh()
    moved = outbox.get(job["job_id"])
    if moved is None or moved["state"] != outbox.ENHANCING:
        return True
    stage = _enhance_stage(moved)
    if stage != moved.get("stage"):
        outbox.transition(job["job_id"], outbox.ENHANCING, expect_revision=moved["revision"], stage=stage)
    _pause(POLL_SECONDS)
    return True


def _stage_enhanced(job: dict) -> bool:
    """The prompt exists. Hand the card over before the video model wants it.

    An enhanced cold job has just loaded a language model onto the configured
    card and is about to want a video model on the same one. Nothing
    arbitrates between them: the enhancer's broker reasons about workloads it
    knows of and the managed WanGP is a separate process it does not know
    about, while WanGP sizes itself against whatever the card reports free at
    load time and has no ladder to climb down.

    The asymmetry matters. WanGP-then-enhancer is protected, because the
    enhancer sees a reduced card and degrades gracefully. Enhancer-then-WanGP
    is not, and that is exactly this boundary - so if the enhancement side
    publishes a way to give the memory back, it is asked here. Where it does
    not, ordering is all there is, and B4's ordering already guarantees the
    enhancement is terminal first.
    """
    from . import enhance

    released = enhance.release_runtime()
    stage = "Freed the language model's memory." if released else None
    return bool(outbox.transition(job["job_id"], outbox.ENSURING_WANGP, expect_revision=job["revision"], stage=stage))


def _stage_ensuring_wangp(job: dict) -> bool:
    """Bring the managed child to READY, and prove it can take a job.

    Cold start is minutes and is a server-side wait, not a browser timeout.
    The ownership rules are the runtime's own and are not duplicated here:
    one managed WanGP per machine, the exact configured GPU, loopback only,
    no attaching to an arbitrary process and no killing by name.

    READY is not the end of it. Runtime state is a liveness and transport
    fact; whether the bridge is on the page, the generation service resolved
    and a settings base can be read is the child's own answer, and it is
    asked for rather than assumed.
    """
    from ..wangp import control, runtime

    current = runtime.current()
    state = current.state
    if state == runtime.STOPPED:
        outbox.transition(job["job_id"], outbox.ENSURING_WANGP, expect_revision=job["revision"],
                          stage="Starting WanGP. This can take a few minutes from cold.")
        try:
            runtime.start()
        except IntegrationError as error:
            return bool(outbox.fail(job["job_id"], error.code, error.detail))
        except Exception as error:
            return bool(outbox.fail(job["job_id"], errors.PROCESS_START_FAILED, f"{type(error).__name__}: {error}"))
        return True
    if state == runtime.STARTING:
        # Joined, never duplicated: a second start is a second child, and
        # there is exactly one per machine by design.
        _pause(POLL_SECONDS)
        return True
    if state == runtime.STOPPING:
        _pause(POLL_SECONDS)
        return True
    if state == runtime.CRASHED:
        return bool(outbox.fail(job["job_id"], errors.PROCESS_EXITED,
                                "WanGP stopped running before this job reached it."))
    if state in (runtime.INCOMPATIBLE, runtime.REINIT_REQUIRED):
        return bool(outbox.fail(job["job_id"], current.error_code or errors.SETUP_REQUIRED,
                                "WanGP's setup needs attention before queued jobs can run."))
    if state != runtime.READY:
        _pause(POLL_SECONDS)
        return True

    ok, code = control.available()
    if not ok:
        if code in (errors.CONTROL_UNAVAILABLE, errors.CONTROL_UNAUTHORISED):
            # The child is running but is not one this Forge can drive - an
            # older bridge, or a child from a previous run. Neither fixes
            # itself, so it is said plainly rather than retried into a wall.
            return bool(outbox.fail(job["job_id"], code))
        # Everything else here is "not yet" at least as often as it is
        # "never", and READY is the reason: it means a process is alive and
        # answered HTTP, which happens before Wan2GP has finished building
        # its UI - and the generation service does not exist until it does.
        # Failing the job on the first ask threw away work that would have
        # run seconds later, which is the opposite of what pressing and
        # walking away is for. Bounded, so a build that genuinely cannot
        # execute still ends up saying so.
        return _wait_for_service(job, code or errors.SERVICE_UNAVAILABLE)
    return bool(outbox.transition(job["job_id"], outbox.COMPOSING, expect_revision=job["revision"]))


def _wait_for_service(job: dict, code: str) -> bool:
    """Hold a job while WanGP finishes coming up - unless it never will.

    "Not yet" and "never" look identical from here and need opposite
    answers. A build that carries the generation service has it moments after
    READY, so waiting costs seconds; a build without it never will, and a job
    that waits for it waits forever while holding the queue open behind it.
    The child answers which, and a "never" is not waited on.
    """
    from ..wangp import control

    if code == errors.SERVICE_UNAVAILABLE and not control.executable_ever():
        # This Wan2GP has no queue worker to submit into, and inventing a
        # second execution path beside its arbiter is the one thing this
        # design refuses - that is two generations on one card. The job used
        # to be handed back to the page, but nothing tells a page that any
        # more - no connection is held open to tell it over - so it ends
        # here, saying so. The next press already goes straight to the path
        # the page drives itself (see outbox.chosen_executor), because the
        # child has now said it never can.
        _journal(
            f"job {job['job_id'][:8]}: this WanGP cannot run unattended jobs "
            f"({control.why_not()[:200]}); the job is ended and the next press runs from the page"
        )
        return bool(outbox.fail(job["job_id"], code, NO_SERVICE_MESSAGE))

    counted = outbox.attempt(job["job_id"])
    attempts = int((counted or job).get("attempts") or 0)
    if attempts == 1:
        # Once per job, not once per attempt: the child's own account of what
        # it looked at. A log that says only SERVICE_UNAVAILABLE cannot tell a
        # WanGP that is still building its UI from one that will never be able
        # to run a job, and those need opposite responses from whoever reads it.
        why = control.why_not()
        if why:
            _journal(f"job {job['job_id'][:8]}: waiting for WanGP's generation service - {why[:300]}")
    if attempts > MAX_RETRYABLE_ATTEMPTS:
        # Waited long enough. Ended with a sentence somebody can act on,
        # rather than handed to a page that nothing would tell.
        _journal(f"job {job['job_id'][:8]}: gave up waiting for unattended execution after {attempts} attempts")
        return bool(outbox.fail(job["job_id"], code, f"WanGP was still not ready after {attempts} tries. Press Add to Queue again."))
    delay = min(BACKOFF_MAX, BACKOFF_START * (2 ** min(6, attempts - 1)))
    outbox.transition(job["job_id"], outbox.ENSURING_WANGP,
                      stage=f"WanGP is up but not ready to take a job yet. Trying again in {int(delay)}s.")
    _pause(delay)
    return True


def _stage_composing(job: dict) -> bool:
    """Read the settings this job runs at, once, and freeze them.

    Composed now rather than at press because at press there may have been no
    process to ask - cold start is a stage that happens after admission. The
    job records when its base was captured and where it came from, and the
    queue says so, because "the settings as of when it ran" and "the settings
    as of when you pressed" are different promises.
    """
    from ..wangp import control

    if job.get("snapshot") and (job["snapshot"] or {}).get("settings_count"):
        return bool(outbox.transition(job["job_id"], outbox.WAITING_FOR_CARD, expect_revision=job["revision"]))
    model_type = (job.get("model") or {}).get("type") or ""
    # The job's own choice, frozen at the press, not the setting as it stands
    # now: a job composed while inheriting was on is not re-composed because
    # somebody has since turned it off, and the record says which it was.
    inherit = bool(job.get("inherit_settings"))
    try:
        answer = control.compose(model_type, inherit=inherit)
    except IntegrationError as error:
        return bool(outbox.fail(job["job_id"], error.code, error.detail))
    if not answer["ok"]:
        return bool(outbox.fail(job["job_id"], answer["code"] or errors.COMPOSE_UNAVAILABLE, answer["message"]))
    source = answer["source"]
    if not inherit:
        _journal(f"job {job['job_id'][:8]}: settings not inherited by choice; WanGP fills them in from its own defaults")
    if source == wire.BASE_RECORDED and job.get("settings_flush") in wire.FLUSH_FRESH:
        # Same seam, different promise. The page committed its live form on
        # purpose before it pressed, so this base is what was on screen -
        # not whatever WanGP happened to have recorded last. The job says
        # which, because those are answers to different questions and a
        # reader of the queue is entitled to tell them apart.
        source = wire.BASE_FLUSHED
    snapshot = {
        "schema": 1,
        "model_type": answer["model_type"],
        "settings": answer["settings"],
        "source": source,
        "captured_at": _now(),
        "wan2gp_version": answer["wan2gp_version"],
        "residency_key": answer["residency_key"],
    }
    moved = outbox.record_snapshot(job["job_id"], snapshot, expect_revision=job["revision"])
    if moved is None:
        return True
    if answer["source"] == wire.BASE_FACTORY:
        _journal(
            f"job {job['job_id'][:8]}: nothing committed in WanGP for {answer['model_type'][:40]}; "
            "composed from the settings WanGP itself loads for that model, and the job says so"
        )
    return bool(outbox.transition(job["job_id"], outbox.WAITING_FOR_CARD, expect_revision=moved["revision"],
                                 model=answer["model"]))


def _stage_waiting_for_card(job: dict) -> bool:
    """Submit, unless WanGP is generating - in which case wait, honestly.

    THE WAIT IS THE FEATURE, NOT A WORKAROUND.

    One card, one generation, whoever started it. A submission arriving while
    the user is generating in the WanGP tab is not refused and not raced: the
    job waits, visibly, with stage text saying why, and starts when that run
    finishes. The user's work is never pre-empted, never aborted and never
    made to queue behind an unattended job that has not started.

    There is no exclusion to acquire here. The task goes into WanGP's own
    queue and its one worker drains it, so both arrival orders are handled by
    the gate WanGP already has - this stage only avoids adding to a queue
    whose worker is busy with something the user can see, so that the wait is
    reported rather than hidden inside a generation time.

    AND BECAUSE IT IS A COURTESY, IT IS BOUNDED.

    That paragraph says the quiet part: this gate is about *reporting*, not
    about safety. Submitting while WanGP is generating appends to its queue
    and pre-empts nothing - it is what pressing Add to Queue in the WanGP tab
    does during a run. So waiting here can be polite, and must not be
    load-bearing.

    It was. ``generation_running`` is the service's live worker handle, which
    is the best signal available and still not a promise: after a generation
    was cancelled mid-run it stayed true, and every job pressed afterwards
    waited on it for ever - composed, ready, and stuck one step from being
    submitted. A queue that stops for good because a flag somewhere else did
    not clear is worse than one that occasionally queues a job behind a run
    the user can see.
    """
    from ..wangp import control

    try:
        answer = control.hello()
    except IntegrationError as error:
        return bool(outbox.fail(job["job_id"], error.code, error.detail))
    if answer["generation_running"]:
        waited = max(0.0, _now() - float(job.get("updated") or 0.0))
        if waited < CARD_WAIT_MAX_SECONDS:
            depth = answer["queue_depth"]
            stage = "Waiting: WanGP is busy with its own work." + (f" {depth} in its queue." if depth else "")
            if stage != job.get("stage"):
                outbox.transition(job["job_id"], outbox.WAITING_FOR_CARD, expect_revision=job["revision"], stage=stage)
            _pause(CARD_POLL_SECONDS)
            return True
        _journal(
            f"job {job['job_id'][:8]}: WanGP has reported itself busy for {int(waited)}s; "
            "queuing behind it rather than waiting longer - a task appended to its queue pre-empts nothing"
        )
    return bool(outbox.transition(job["job_id"], outbox.SUBMITTING_WANGP, expect_revision=job["revision"]))


def _stage_submitting(job: dict) -> bool:
    """Hand the task over, once, and never twice.

    The child's ledger is what makes a repeated call safe: it is keyed by
    this job's execution id, written before the submission is made, and a
    second call with the same id is answered from the record rather than by
    asking WanGP again. So a submission whose answer never arrived is
    resolved by asking the child what it knows - which is the one question
    that has a correct answer - rather than by guessing.
    """
    from ..wangp import control

    execution_id = job.get("execution_id") or ""
    if not execution_id:
        return bool(outbox.fail(job["job_id"], errors.INTERNAL_ERROR, "the job has no execution id"))

    snapshot = _snapshot_settings(job)
    if snapshot is None:
        return bool(outbox.fail(job["job_id"], errors.COMPOSE_UNAVAILABLE,
                                "the settings this job was composed with are gone"))
    media, missing = _media_for(job)
    if missing:
        return bool(outbox.fail(job["job_id"], errors.JOB_INPUT_MISSING,
                                f"{missing} is no longer on disk"))
    prompt = (job.get("request") or {}).get("prompt")
    try:
        record = control.submit(
            execution_id,
            snapshot["settings"],
            prompt=prompt,
            media=media,
            model_type=snapshot.get("model_type", ""),
        )
    except IntegrationError as error:
        if error.code in RETRYABLE:
            return _back_off(job, error.code)
        return bool(outbox.fail(job["job_id"], error.code, error.detail))
    return bool(outbox.record_execution(job["job_id"], record, expect_revision=job["revision"],
                                        child_instance=record.get("instance", "")))


def _stage_tracking(job: dict) -> bool:
    """Follow a submitted generation to a result, from the child's ledger.

    Read rather than waited on: the child's own tracker is what watches the
    shared queue, and asking it is one short request. Nothing here blocks on
    a generation, holds a socket open for one, or calls a result() that would
    take WanGP's generation lock.
    """
    from ..wangp import control, runtime

    execution_id = job.get("execution_id") or ""
    current = runtime.current()
    if current.state != runtime.READY:
        # The child went away with a submission outstanding. Whether that
        # generation finished is not knowable from here, and a resubmission
        # would be a second one, so the job says so and waits for a person.
        return bool(outbox.fail(job["job_id"], errors.EXECUTION_UNKNOWN,
                                "WanGP stopped while this generation was in flight.",
                                state=outbox.EXECUTION_UNKNOWN))
    if job.get("child_instance") and current.instance_id and job["child_instance"] != current.instance_id:
        return bool(outbox.fail(job["job_id"], errors.EXECUTION_UNKNOWN,
                                "WanGP restarted while this generation was in flight.",
                                state=outbox.EXECUTION_UNKNOWN))
    try:
        found = control.status([execution_id])
    except IntegrationError as error:
        if error.code in (errors.WANGP_NOT_RUNNING, errors.CONTROL_UNAVAILABLE):
            _pause(POLL_SECONDS)
            return True
        return bool(outbox.fail(job["job_id"], error.code, error.detail))
    record = found.get(execution_id)
    if record is None:
        _pause(POLL_SECONDS)
        return True
    moved = outbox.record_execution(job["job_id"], record, expect_revision=job["revision"],
                                    child_instance=record.get("instance", ""))
    settled = moved or outbox.get(job["job_id"])
    if settled is not None and settled["state"] in outbox.SERVER_TERMINAL:
        # Ours now, durably. The child may let its record go.
        control.forget([execution_id])
        if settled["state"] == outbox.COMPLETED:
            _journal(f"job {job['job_id'][:8]}: completed with {settled.get('generated_count', 0)} file(s)")
            # Copy the paths somewhere durable while they are still here.
            # They live on the job, and the job is swept out of the queue
            # two minutes after this - so a viewer opened any later would
            # find nothing, and these are the only exact ones there are.
            _remember_outputs(job, record)
        return True
    _pause(POLL_SECONDS)
    return True


# ----------------------------------------------------------------- helpers --


def _remember_outputs(job: typing.Mapping[str, typing.Any], record: typing.Mapping[str, typing.Any]) -> None:
    """Hand this job's output paths to the durable ledger. Never fatal.

    A generation that succeeded must not be turned into a failure by a
    document that would not write, so every way this can go wrong ends in a
    line rather than an exception.
    """
    try:
        from . import outputs

        outputs.remember(
            job["job_id"],
            (record or {}).get("generated_files") or [],
            request_id=str((job.get("request") or {}).get("request_id") or ""),
            model=str((job.get("model") or {}).get("label") or ""),
        )
    except Exception as error:  # noqa: BLE001 - a ledger is never worth a failed job
        _journal(f"job {job['job_id'][:8]}: the output ledger could not be written ({type(error).__name__})")


def _snapshot_settings(job: typing.Mapping[str, typing.Any]) -> typing.Optional[dict]:
    """The frozen settings, read from the stored document rather than the
    public one - the public view deliberately does not carry them."""
    with outbox._lock:  # noqa: SLF001 - one reader, and the alternative is a second copy of _load
        for stored in outbox._load():  # noqa: SLF001
            if stored["job_id"] == job["job_id"]:
                snapshot = stored.get("snapshot")
                return dict(snapshot) if isinstance(snapshot, dict) and snapshot.get("settings") else None
    return None


def _media_for(job: typing.Mapping[str, typing.Any]) -> typing.Tuple[dict, str]:
    """The job's own inputs as ids the child can resolve, or the first missing one.

    Proved to exist here rather than discovered missing inside the child,
    because a job that waited four hours for the card and then found its
    picture gone should say which picture, not report a refusal.
    """
    from . import job_inputs

    inputs = job.get("inputs") or {}
    media: typing.Dict[str, typing.Any] = {}
    for slot in (wire.EXEC_SLOT_START, wire.EXEC_SLOT_END):
        value = inputs.get(slot)
        if not value:
            continue
        try:
            job_inputs.resolve(value)
        except IntegrationError:
            return {}, f"the {slot.replace('_', ' ')} image"
        media[slot] = value
    references = inputs.get(wire.EXEC_SLOT_REFERENCES) or []
    kept = []
    for value in references:
        try:
            job_inputs.resolve(value)
        except IntegrationError:
            return {}, "a reference image"
        kept.append(value)
    if kept:
        media[wire.EXEC_SLOT_REFERENCES] = kept
    return media, ""


def _back_off(job: dict, code: str) -> bool:
    """A refusal that is the state of the world. Wait and try again, bounded."""
    counted = outbox.attempt(job["job_id"])
    attempts = int((counted or job).get("attempts") or 0)
    if attempts > MAX_RETRYABLE_ATTEMPTS:
        outbox.fail(job["job_id"], code, f"still refused after {attempts} attempts")
        return True
    delay = min(BACKOFF_MAX, BACKOFF_START * (2 ** min(6, attempts - 1)))
    outbox.transition(job["job_id"], outbox.WAITING_FOR_CARD,
                      stage=f"{errors.message(code)} Trying again in {int(delay)}s.")
    _pause(delay)
    return True


_STAGES: typing.Mapping[str, typing.Callable[[dict], bool]] = {
    outbox.ADMITTED: _stage_admitted,
    outbox.WAITING_TURN: _stage_waiting_turn,
    outbox.ENHANCING: _stage_enhancing,
    outbox.ENHANCED: _stage_enhanced,
    outbox.ENSURING_WANGP: _stage_ensuring_wangp,
    outbox.COMPOSING: _stage_composing,
    outbox.WAITING_FOR_CARD: _stage_waiting_for_card,
    outbox.SUBMITTING_WANGP: _stage_submitting,
    outbox.GENERATION_WAITING: _stage_tracking,
    outbox.GENERATION_RUNNING: _stage_tracking,
}


# -------------------------------------------------------------- recovery --


def recover() -> dict:
    """What a starting Forge does before anything sweeps. Returns the counts.

    Called from the app-started hook, and deliberately ahead of the handoff
    and staging sweeps: a sweep that ran first would already have deleted the
    pictures the pins were about to protect.
    """
    # The queue is this session's. Nothing carries in, so there is nothing to
    # recover and nothing to reconcile: a restart that resurrected old prompts
    # and ran them again was the behaviour this replaces.
    counted = outbox.start_session()
    # The switch spends money and minutes on every press, and "off by
    # default" is what the panel says. Persisting it made that true only of a
    # fresh install; a session that starts with it on, inherited from one the
    # user does not remember, is the surprise this removes.
    try:
        from . import enhance

        if enhance.start_session():
            counted["enhance_reset"] = True
    except Exception:
        pass
    return counted


def reconcile() -> dict:
    """Settle every job whose submission exists and whose outcome does not.

    The ledger inside the child is the only thing that can answer, and what
    it answers decides the outcome:

        terminal, and this child wrote it  -> adopt that outcome
        still running in this same child   -> re-attach and keep tracking
        this child never saw it            -> EXECUTION_UNKNOWN

    The last one is the expected answer on Windows, where the child is
    deliberately killed with Forge, and it is a pass rather than a failure:
    proving a generation was lost is the correct result, and resubmitting it
    would be a second generation.
    """
    from ..wangp import control, runtime

    pending = outbox.submitted_jobs()
    if not pending:
        return {"adopted": 0, "reattached": 0, "unknown": 0}
    counted = {"adopted": 0, "reattached": 0, "unknown": 0}
    current = runtime.current()
    if current.state != runtime.READY:
        for job in pending:
            outbox.fail(job["job_id"], errors.EXECUTION_UNKNOWN,
                        "WanGP was not running when Forge came back, so this could not be checked.",
                        state=outbox.EXECUTION_UNKNOWN)
            counted["unknown"] += 1
        return counted
    ids = [job["execution_id"] for job in pending if job.get("execution_id")]
    try:
        records = control.status(ids)
    except IntegrationError:
        records = {}
    for job in pending:
        record = records.get(job.get("execution_id") or "")
        if record is None or record["state"] == wire.EXEC_UNKNOWN:
            outbox.fail(job["job_id"], errors.EXECUTION_UNKNOWN,
                        "Whether WanGP ran this could not be proved after the restart.",
                        state=outbox.EXECUTION_UNKNOWN)
            counted["unknown"] += 1
            continue
        outbox.record_execution(job["job_id"], record, child_instance=record.get("instance", ""))
        if record["state"] in wire.EXEC_TERMINAL:
            counted["adopted"] += 1
        else:
            counted["reattached"] += 1
    _journal(
        f"reconciled {len(pending)} submitted job(s): {counted['adopted']} adopted, "
        f"{counted['reattached']} re-attached, {counted['unknown']} unknown"
    )
    return counted


def snapshot() -> dict:
    """What a status line may say about the executor itself."""
    listed = outbox.server_jobs()
    active = [job for job in listed if job["state"] in outbox.SERVER_ACTIVE]
    return {
        "running": running(),
        "started": bool(_state.get("started")),
        "active": len(active),
        "state": active[0]["state"] if active else "",
        "stage": active[0]["stage"] if active else "",
        "last_error": _state.get("last_error", ""),
    }


__all__ = [
    "BACKOFF_MAX", "BACKOFF_START", "CARD_POLL_SECONDS", "IDLE_SECONDS", "MAX_RETRYABLE_ATTEMPTS",
    "CARD_WAIT_MAX_SECONDS", "POLL_SECONDS", "RETRYABLE", "SUBMIT_TIMEOUT", "WANGP_READY_TIMEOUT",
    "ensure_running", "reconcile", "recover", "reset_for_tests", "running", "snapshot", "step",
    "stop", "use_clock", "use_sleep", "use_thread", "wake",
]
