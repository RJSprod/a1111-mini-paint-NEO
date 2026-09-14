"""Press Add to Queue and walk away: the server executor, with no browser.

Every check here runs with no page, no Gradio session and no browser timer.
That is not a convenience of the test harness - it is the property under
test. The old queue could only be advanced by the page that pressed it, so
closing a tab stopped it and a phone that locked itself stopped it too; this
suite asserts that the same press now reaches a generated file with nothing
on the browser side participating at all.

The WanGP child is stood in for by a fake control plane - the same shapes
``wangp/control.py`` normalises, driven by the test - so a cold start, a
busy card, a crash mid-generation and a restart can each be made to happen
at the exact moment that matters rather than waited for.

What is asserted, in the order it matters:

*   a press with WanGP stopped is *admitted*, not refused. The old rule
    refused it, correctly, because there was nothing for a page to drive;
    keeping that rule would make the cold case - the one the feature exists
    for - the one case that does not work.
*   the pictures a job owns survive a restart, a thirty-minute wait and a
    four-hour one, because a pin is read by the sweeper rather than a timer.
*   a job waits for the card rather than starting on top of somebody else's
    generation, and says why while it waits.
*   a stage that takes minutes does not clobber a cancel that landed during
    it, because a write checks the revision it was prepared against.
*   nothing is ever resubmitted across an ambiguity: a restart reconciles by
    execution id, and where that cannot answer the job says so and stops.
*   no prompt, no path and no enhanced text reaches a log, an event or a
    job's public record.
"""

from harness import Results, setup_path

setup_path()

import io  # noqa: E402
import json  # noqa: E402
import pathlib  # noqa: E402
import tempfile  # noqa: E402
import types  # noqa: E402

from PIL import Image  # noqa: E402

from minipaint_neo import events, interop  # noqa: E402
from minipaint_neo.clipboard import config, enhance, executor, job_inputs, outbox  # noqa: E402
from minipaint_neo.clipboard import store as clipboard_store  # noqa: E402
from minipaint_neo.wangp import config as wangp_config  # noqa: E402
from minipaint_neo.wangp import control, errors, handoff, process_log  # noqa: E402
from minipaint_neo.wangp import protocol as wire  # noqa: E402
from minipaint_neo.wangp.errors import IntegrationError  # noqa: E402

PAGE = "a" * 16
PROMPT = "a lighthouse in a storm"


class _Clock:
    def __init__(self):
        self.now = 1_700_000_000.0

    def __call__(self):
        return self.now

    def tick(self, seconds):
        self.now += seconds
        return self.now


class FakeRuntime:
    """The managed WanGP child, as far as the executor can see it."""

    STOPPED = "STOPPED"
    STARTING = "STARTING"
    READY = "READY"
    STOPPING = "STOPPING"
    CRASHED = "CRASHED"
    INCOMPATIBLE = "INCOMPATIBLE"
    REINIT_REQUIRED = "REINIT_REQUIRED"

    def __init__(self):
        self.state = self.STOPPED
        self.instance_id = ""
        self.error_code = ""
        self.starts = 0

    def current(self):
        return self

    def start(self):
        self.starts += 1
        self.state = self.READY
        self.instance_id = "child-one"
        return self

    def bridge_secret(self):
        return "s3cr3t"


class FakeChild:
    """The control plane, answering the way the real child's ledger does."""

    def __init__(self, clock):
        self.clock = clock
        self.records = {}
        self.submissions = []
        self.composes = []
        self.cancels = []
        self.forgotten = []
        self.busy = False
        self.available = True
        self.can_compose = True
        #: Whether this build could EVER run an unattended job. False is a
        #: permanent fact about the install, not a stage of its startup.
        self.can_execute_ever = True
        self.instance = "child-one"
        self.form = {"steps": 30, "image_prompt_type": "", "video_prompt_type": "", "image_start": None, "image_end": None, "image_refs": []}
        self.source = wire.BASE_RECORDED
        self.refuse = ""

    def __call__(self, operation, payload):
        if operation == wire.CONTROL_HELLO:
            if not self.available:
                return 409, {"ok": False, "code": wire.SERVICE_UNAVAILABLE, "message": "no service"}
            return 200, {"ok": True, "control_version": wire.CONTROL_VERSION, "protocol": wire.PROTOCOL,
                         "can_execute": self.can_execute_ever, "can_compose": self.can_compose,
                         "service": self.can_execute_ever, "service_possible": self.can_execute_ever,
                         "code": "" if self.can_execute_ever else wire.SERVICE_UNAVAILABLE,
                         "generation_running": self.busy, "queue_depth": 2 if self.busy else 0,
                         "instance": self.instance}
        if operation == wire.CONTROL_COMPOSE:
            self.composes.append(dict(payload))
            if not self.can_compose:
                return 409, {"ok": False, "code": wire.COMPOSE_UNAVAILABLE, "message": "no settings"}
            return 200, {"ok": True, "settings": dict(self.form), "source": self.source,
                         "model_type": payload.get("model_type") or "t2v", "wan2gp_version": "8.4",
                         "residency_key": "t2v||", "model": {"type": payload.get("model_type") or "t2v", "label": "T2V"}}
        if operation == wire.CONTROL_SUBMIT:
            if self.refuse:
                return 409, {"ok": False, "code": self.refuse, "message": "refused"}
            self.submissions.append(dict(payload))
            record = self.records.get(payload["execution_id"])
            if record is None:
                record = {"execution_id": payload["execution_id"], "state": wire.EXEC_ACCEPTED,
                          "submitted_at": self.clock(), "instance": self.instance, "stage": "handed to WanGP's queue"}
                self.records[payload["execution_id"]] = record
            return 200, {"ok": True, "record": record}
        if operation == wire.CONTROL_STATUS:
            return 200, {"ok": True, "instance": self.instance,
                         "records": {key: self.records[key] for key in payload["execution_ids"] if key in self.records}}
        if operation == wire.CONTROL_CANCEL:
            self.cancels.append(payload["execution_id"])
            record = self.records.setdefault(payload["execution_id"], {"execution_id": payload["execution_id"]})
            record.update({"state": wire.EXEC_CANCELLED, "finished_at": self.clock(), "instance": self.instance})
            return 200, {"ok": True, "record": record}
        if operation == wire.CONTROL_FORGET:
            self.forgotten.extend(payload["execution_ids"])
            for key in payload["execution_ids"]:
                self.records.pop(key, None)
            return 200, {"ok": True, "forgotten": len(payload["execution_ids"])}
        return 404, {"ok": False, "code": "REQUEST_INVALID"}

    # -- what a test makes happen
    def finish(self, execution_id, files=("/wangp/outputs/one.mp4",)):
        self.records[execution_id] = {"execution_id": execution_id, "state": wire.EXEC_DONE,
                                      "generated_files": list(files), "finished_at": self.clock(), "instance": self.instance}

    def generating(self, execution_id):
        self.records[execution_id] = {"execution_id": execution_id, "state": wire.EXEC_RUNNING,
                                      "stage": "generating", "instance": self.instance}

    def fail(self, execution_id, code=wire.EXECUTION_REFUSED):
        self.records[execution_id] = {"execution_id": execution_id, "state": wire.EXEC_FAILED, "code": code,
                                      "message": "WanGP declined the task", "instance": self.instance}


def _png(colour=(20, 40, 80, 255), size=(6, 4)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def _setup(clock):
    """A scratch Forge: its own data root, its own handoff root, no threads."""
    scratch = tempfile.mkdtemp(prefix="minipaint-executor-")
    config.use_config_dir(scratch)
    wangp_config.use_runtime_dir(scratch) if hasattr(wangp_config, "use_runtime_dir") else None
    clipboard_store.reset_for_tests()
    outbox.reset_for_tests()
    outbox.use_clock(clock)
    outbox.use_executor(outbox.EXECUTOR_SERVER)
    outbox.use_running(lambda: False)
    job_inputs.use_clock(clock)
    executor.reset_for_tests()
    executor.use_clock(clock)
    executor.use_sleep(lambda _seconds: None)
    executor.use_thread(False)
    events.reset_for_tests()
    return scratch


def _request(prompt=PROMPT, **images):
    request = {"request_id": "0" * 32, "prompt": prompt}
    if images:
        request["images"] = images
    return request


def _asset(colour=(10, 20, 30, 255)):
    """One picture in the Clipboard library, so a press has something to own."""
    library = clipboard_store.store()
    if library.root() is None:
        library.set_root(str(pathlib.Path(config.config_dir()) / "library"), create=True)
    return library.import_bytes(_png(colour), "one.png", "upload").asset_id


def _run(child, runtime, steps=40):
    """Drive the executor to a standstill, the way its own thread would."""
    control.use_transport(child)
    original = control._endpoint
    try:
        for _ in range(steps):
            if not executor.step():
                return True
        return False
    finally:
        control.use_transport(None)
        control._endpoint = original


def _install(monkey, runtime):
    """Point the executor's runtime lookups at the fake child process."""
    from minipaint_neo.wangp import runtime as real

    monkey.append((real, "current", real.current))
    real.current = lambda: runtime
    monkey.append((real, "start", real.start))
    real.start = lambda *args, **keywords: runtime.start()


def _restore(monkey):
    for module, name, value in monkey:
        setattr(module, name, value)


# --------------------------------------------------------------- the tests --


def admission_checks(r: Results, clock) -> None:
    """A press with WanGP stopped is admitted. The whole cold case."""
    _setup(clock)
    job = outbox.submit(_request(), PAGE)
    r.check("a press while WanGP is stopped is admitted, not refused",
            job["state"] == outbox.ADMITTED and job["executor"] == outbox.EXECUTOR_SERVER, job["state"])
    r.check("and it has an execution id before anything external exists",
            len(job["execution_id"]) == 32, job["execution_id"])
    r.check("the acknowledgement is durable at once: the document holds it",
            len(outbox.jobs()) == 1 and outbox.jobs()[0]["job_id"] == job["job_id"])
    r.check("no page claims it, whichever page asks",
            outbox.claim(PAGE).get("empty") is True, str(outbox.claim(PAGE)))
    r.check("and a press is still refused for the browser path, which has nothing to drive",
            _refused(lambda: outbox.submit(_request(), PAGE, executor=outbox.EXECUTOR_BROWSER)) == errors.WANGP_NOT_RUNNING)


def _refused(call) -> str:
    try:
        call()
    except IntegrationError as error:
        return error.code
    return ""


def walkaway_checks(r: Results, clock) -> None:
    """The product test: press, close the browser, find a generated file."""
    _setup(clock)
    asset = _asset()
    job = outbox.submit(_request(start={"kind": "clipboard_asset", "id": asset}), PAGE)
    execution_id = job["execution_id"]
    r.check("the press owns its picture from admission",
            len(outbox.get(job["job_id"])["inputs"]) == 1, str(outbox.get(job["job_id"])["inputs"]))

    runtime = FakeRuntime()
    child = FakeChild(clock)
    monkey = []
    _install(monkey, runtime)
    try:
        control.use_transport(child)
        executor.step()  # admitted -> ensuring wangp
        r.check("the executor takes the job on with no browser involved",
                outbox.get(job["job_id"])["state"] == outbox.ENSURING_WANGP)
        executor.step()  # start the cold child
        r.check("a cold WanGP is started by the server", runtime.starts == 1 and runtime.state == FakeRuntime.READY)
        executor.step()  # ready -> composing
        r.check("and only then are the settings read", outbox.get(job["job_id"])["state"] == outbox.COMPOSING)
        executor.step()  # compose
        stored = outbox.get(job["job_id"])
        r.check("the snapshot is frozen with where it came from",
                stored["snapshot"]["source"] == wire.BASE_RECORDED and stored["snapshot"]["settings_count"] > 0, str(stored["snapshot"]))
        r.check("and the settings themselves never cross to a screen", "settings" not in stored["snapshot"])
        executor.step()  # card free -> submitting
        executor.step()  # submit
        r.check("the task reaches WanGP once", len(child.submissions) == 1, str(len(child.submissions)))
        r.check("carrying the job's own picture as an id, never a path",
                child.submissions[0]["media"]["start"] == stored["inputs"]["start"], str(child.submissions[0]["media"]))
        r.check("and the prompt that was typed", child.submissions[0]["prompt"] == PROMPT)
        r.check("the job says it is with WanGP now",
                outbox.get(job["job_id"])["state"] in (outbox.SUBMITTING_WANGP, outbox.GENERATION_WAITING))

        child.generating(execution_id)
        executor.step()
        r.check("generation is followed from the server", outbox.get(job["job_id"])["state"] == outbox.GENERATION_RUNNING)

        child.finish(execution_id, ["/wangp/outputs/storm.mp4"])
        executor.step()
        done = outbox.get(job["job_id"])
        r.check("it completes", done["state"] == outbox.COMPLETED, done["state"])
        r.check("with the generated file persisted on the job", done["generated_count"] == 1, str(done))
        r.check("and the child's record let go once ours is durable", child.forgotten == [execution_id], str(child.forgotten))
        r.check("no browser was ever scheduled: nothing claimed, nothing leased",
                done["attempts"] == 0 and not done["sent"] and not done["leased_until"], str(done))
    finally:
        control.use_transport(None)
        _restore(monkey)


def card_checks(r: Results, clock) -> None:
    """The wait that is the feature: never on top of the user's own run."""
    _setup(clock)
    job = outbox.submit(_request(), PAGE)
    runtime = FakeRuntime()
    runtime.state = FakeRuntime.READY
    runtime.instance_id = "child-one"
    child = FakeChild(clock)
    child.busy = True
    monkey = []
    _install(monkey, runtime)
    try:
        control.use_transport(child)
        for _ in range(4):
            executor.step()
        waiting = outbox.get(job["job_id"])
        r.check("a job arriving while WanGP is generating waits", waiting["state"] == outbox.WAITING_FOR_CARD, waiting["state"])
        r.check("and says why, rather than stalling silently",
                "busy" in waiting["stage"].lower(), waiting["stage"])
        r.check("nothing was submitted on top of the user's run", not child.submissions, str(child.submissions))
        for _ in range(3):
            executor.step()
        r.check("it keeps waiting for as long as the user's work takes",
                outbox.get(job["job_id"])["state"] == outbox.WAITING_FOR_CARD and not child.submissions)

        child.busy = False
        executor.step()
        executor.step()
        r.check("and starts once the card is free", len(child.submissions) == 1, str(child.submissions))
    finally:
        control.use_transport(None)
        _restore(monkey)


def stuck_card_checks(r: Results, clock) -> None:
    """A WanGP that never stops reporting itself busy does not stop the queue.

    THIS HAPPENED, AND IT STOPPED EVERYTHING.

    ``generation_running`` is the service's live worker handle - the best
    signal available, and still not a promise. After a generation was
    cancelled mid-run it stayed true, and every job pressed afterwards sat in
    ``waiting_for_card`` for ever: composed, ready, one step from submission,
    and never taken.

    The gate is a courtesy. Submitting while WanGP is generating appends to
    its queue and pre-empts nothing - it is what pressing Add to Queue in the
    WanGP tab does during a run - so a wait on it may be polite and must not
    be load-bearing.
    """
    _setup(clock)
    runtime = FakeRuntime()
    runtime.state = FakeRuntime.READY
    runtime.instance_id = "child-one"
    child = FakeChild(clock)
    child.busy = True  # and never stops being busy
    monkey = []
    _install(monkey, runtime)
    try:
        control.use_transport(child)
        job = outbox.submit(_request(), PAGE)
        for _ in range(6):
            executor.step()
        r.check("while the wait is young the job waits, and says why",
                outbox.get(job["job_id"])["state"] == outbox.WAITING_FOR_CARD
                and "busy" in outbox.get(job["job_id"])["stage"].lower(), outbox.get(job["job_id"])["stage"])
        r.check("and nothing has been submitted on top of what the user can see", not child.submissions)

        # Past the bound, with the card still claiming to be busy, the job
        # goes into WanGP's queue anyway rather than waiting for ever.
        clock.tick(executor.CARD_WAIT_MAX_SECONDS + 1.0)
        for _ in range(4):
            executor.step()
        settled = outbox.get(job["job_id"])
        r.check("past the bound it queues behind WanGP instead of stopping for good",
                len(child.submissions) == 1 and settled["state"] in outbox.SERVER_SUBMITTED,
                f"{settled['state']} {len(child.submissions)}")
        r.check("and the card never stopped reporting itself busy, which is the whole point",
                child.busy is True)
    finally:
        control.use_transport(None)
        _restore(monkey)


def input_lifetime_checks(r: Results, clock) -> None:
    """A waiting job's picture outlives every sweep there is."""
    _setup(clock)
    asset = _asset()
    job = outbox.submit(_request(start={"kind": "clipboard_asset", "id": asset}), PAGE)
    held = outbox.get(job["job_id"])["inputs"]["start"]
    r.check("the input is a durable object of the job's own, not the asset",
            held != asset and handoff.resolve(held).exists())

    # The sweeps that run at boot, with a job that has been waiting far
    # longer than either of their ages. This is the restart case, which is
    # the one that could actually happen: nothing deletes an input while
    # Forge keeps running.
    clock.tick(8 * 3600)
    removed = handoff.sweep(max_age_seconds=6 * 3600, now=clock())
    r.check("the boot handoff sweep does not take a waiting job's picture",
            handoff.resolve(held).exists(), f"{removed} removed")
    removed = interop.sweep_staging(max_age_seconds=30 * 60, now=clock())
    r.check("nor does the staging sweep", handoff.resolve(held).exists())

    # And the wait that is now legitimate and can far exceed both: the user
    # is generating in the WanGP tab.
    clock.tick(12 * 3600)
    outbox.transition(job["job_id"], outbox.WAITING_FOR_CARD)
    handoff.sweep(max_age_seconds=6 * 3600, now=clock())
    r.check("a job held for the card past every sweep age keeps its picture",
            handoff.resolve(held).exists())
    r.check("and the pin says so while it is not terminal",
            held in job_inputs.pinned_ids(), str(job_inputs.counts()))

    outbox.transition(job["job_id"], outbox.COMPLETED)
    r.check("a terminal job starts its retention clock rather than deleting at once",
            handoff.resolve(held).exists() and held not in job_inputs.pinned_ids())
    r.check("so a retry or a diagnosis still has what it ran with",
            held in job_inputs.pinned_ids(include_released=True))
    clock.tick(job_inputs.RETENTION_SECONDS + 60)
    dropped = job_inputs.sweep(now=clock())
    r.check("and it is released when the grace runs out", dropped == 1 and held not in job_inputs.pinned_ids(include_released=True))

    job_inputs.use_clock(clock)


def write_discipline_checks(r: Results, clock) -> None:
    """A long stage cannot undo what landed during it."""
    _setup(clock)
    job = outbox.submit(_request(), PAGE)
    stale = outbox.get(job["job_id"])["revision"]

    # Somebody presses Cancel while a stage is four minutes into a model load.
    outbox.cancel(job["job_id"])
    r.check("the cancel lands", outbox.get(job["job_id"])["state"] == outbox.CANCELLED)

    # The stage now finishes and writes what it prepared, against the version
    # it read. A whole-document write would silently un-cancel the job.
    written = outbox.transition(job["job_id"], outbox.ENSURING_WANGP, expect_revision=stale)
    r.check("a write prepared against a version somebody moved past does nothing",
            written is None and outbox.get(job["job_id"])["state"] == outbox.CANCELLED)
    r.check("and the revision counts every durable change",
            outbox.get(job["job_id"])["revision"] > stale)


def recovery_checks(r: Results, clock) -> None:
    """A restart starts empty, and never generates anything twice.

    THIS USED TO RECONCILE, AND RECONCILING WAS THE WRONG PROMISE.

    The queue was durable across restarts, on the reasoning that a job
    somebody walked away from should survive one. In use that was the
    complaint: a restart brought back prompts from hours earlier, ran them
    again, and held up the ones that had just been pressed. A queue that
    resurrects work nobody asked for again is worse than one that forgets
    work they did.

    So what is asserted now is that a session starts empty - every job, every
    state, every pin - and that nothing is resubmitted on the way there,
    which is the one property the old reconciliation existed to protect and
    the one this must not lose.
    """
    _setup(clock)
    runtime = FakeRuntime()
    runtime.state = FakeRuntime.READY
    runtime.instance_id = "child-one"
    child = FakeChild(clock)
    monkey = []
    _install(monkey, runtime)
    try:
        control.use_transport(child)
        asset = _asset()
        job = outbox.submit(_request(start={"kind": "clipboard_asset", "id": asset}), PAGE)
        for _ in range(6):
            executor.step()
        r.check("the job is submitted before the restart", len(child.submissions) == 1, str(len(child.submissions)))
        held = outbox.get(job["job_id"])["inputs"]["start"]
        r.check("and holds a pinned picture of its own", bool(held), str(held))

        # A second job that never reached WanGP, and a finished one, so the
        # clear is asserted across every kind of record the queue can hold.
        waiting = outbox.submit(_request("never submitted"), PAGE)
        outbox.transition(waiting["job_id"], outbox.COMPOSING)

        counted = executor.recover()
        r.check("a restart starts with an empty queue, whatever was in it",
                outbox.jobs() == [] and counted["cleared"] == 2, f"{len(outbox.jobs())} {counted}")
        r.check("nothing is resubmitted on the way there - that is the whole point of clearing rather than resuming",
                len(child.submissions) == 1, str(len(child.submissions)))
        r.check("and the pictures those jobs owned are let go rather than left as orphans",
                counted["inputs_released"] >= 1, str(counted))

        from minipaint_neo.clipboard import job_inputs

        r.check("the pinned input really is let go, not left held for a job that no longer exists",
                job_inputs.counts()["held"] == 0, str(job_inputs.counts()))

        # An empty queue is an ordinary thing to start with, not a special case.
        counted = executor.recover()
        r.check("a second start finds nothing to clear and says so",
                counted["cleared"] == 0 and outbox.jobs() == [], str(counted))

        # And the queue still works afterwards.
        fresh = outbox.submit(_request("after the restart"), PAGE)
        for _ in range(6):
            executor.step()
        r.check("a job pressed after the restart runs normally",
                outbox.get(fresh["job_id"])["state"] in outbox.SERVER_SUBMITTED
                and len(child.submissions) == 2, outbox.get(fresh["job_id"])["state"])
    finally:
        control.use_transport(None)
        _restore(monkey)


def inheritance_toggle_checks(r: Results, clock) -> None:
    """The choice is the job's, frozen at the press."""
    _setup(clock)
    runtime = FakeRuntime()
    runtime.state = FakeRuntime.READY
    runtime.instance_id = "child-one"
    child = FakeChild(clock)
    monkey = []
    _install(monkey, runtime)
    try:
        control.use_transport(child)

        outbox.use_inherit(False)
        job = outbox.submit(_request(), PAGE)
        r.check("a press with inheritance off records that it was off",
                outbox.get(job["job_id"])["inherit_settings"] is False)
        _run(child, runtime)
        r.check("and the child was asked not to read the user's form",
                child.composes and child.composes[-1].get("inherit") is False, str(child.composes[-1:]))
        child.finish(outbox.get(job["job_id"])["execution_id"])
        _run(child, runtime)

        outbox.use_inherit(True)
        job = outbox.submit(_request("with settings"), PAGE)
        r.check("a press with it on records that too",
                outbox.get(job["job_id"])["inherit_settings"] is True)
        _run(child, runtime)
        r.check("and the child was asked to",
                child.composes[-1].get("inherit") is True, str(child.composes[-1:]))

        # The setting moving does not reach back into a job already pressed.
        outbox.use_inherit(False)
        r.check("a job already pressed keeps the choice it was pressed with",
                outbox.get(job["job_id"])["inherit_settings"] is True)
    finally:
        outbox.use_inherit(None)
        control.use_transport(None)
        _restore(monkey)


def incapable_build_checks(r: Results, clock) -> None:
    """A WanGP that cannot run unattended jobs still runs them.

    Not every Wan2GP carries the queue worker the unattended path submits
    into, and on one that does not there is nothing to wait for. The design
    refuses to invent a second execution path beside the arbiter - that is
    two generations on one card - so what is left is the path that was always
    there: the page drives the live form and presses WanGP's own button.

    The press must reach that path *by itself*. A job that is admitted as
    unattended, waits, and is handed over a moment later works, but a job
    that is never admitted as unattended in the first place is the one that
    does not show the user a queue changing its mind.
    """
    _setup(clock)
    runtime = FakeRuntime()
    runtime.state = FakeRuntime.READY
    runtime.instance_id = "child-one"
    child = FakeChild(clock)
    child.can_execute_ever = False
    monkey = []
    _install(monkey, runtime)
    outbox.use_executor(None)
    outbox.use_running(lambda: True)
    try:
        control.use_transport(child)

        # Before anything has asked the child, a press is still unattended:
        # cold start is the case the unattended path exists for, and refusing
        # it on silence would give that case away.
        control.reset_for_tests()
        control.use_transport(child)
        r.check("with nothing known about the child a press is still admitted as unattended",
                outbox.chosen_executor() == outbox.EXECUTOR_SERVER)

        # Once the child has said it never can, the press goes straight to the
        # page rather than round the houses.
        control.hello()
        r.check("a child that says it never can sends the next press straight to the page",
                outbox.chosen_executor() == outbox.EXECUTOR_BROWSER)
        job = outbox.submit(_request(), PAGE)
        r.check("and that press is a browser job from the start, not one that changes its mind",
                job["executor"] == outbox.EXECUTOR_BROWSER and job["state"] == outbox.PENDING,
                f"{job['executor']} {job['state']}")
        r.check("which the page can claim at once",
                outbox.claim(PAGE).get("job", {}).get("job_id") == job["job_id"], str(outbox.claim(PAGE))[:120])

        # And a job already in flight when the answer arrives is handed over
        # rather than left waiting for something that will never come.
        outbox.reset_for_tests()
        outbox.use_clock(clock)
        outbox.use_executor(outbox.EXECUTOR_SERVER)
        outbox.use_running(lambda: True)
        stranded = outbox.submit(_request("already admitted"), PAGE)
        executor.step()
        executor.step()
        settled = outbox.get(stranded["job_id"])
        r.check("a job already admitted is handed to the page rather than waiting for what will never come",
                settled["executor"] == outbox.EXECUTOR_BROWSER and settled["state"] == outbox.PENDING,
                f"{settled['executor']} {settled['state']}")
        r.check("nothing was submitted to a child that cannot take it", not child.submissions, str(child.submissions))
    finally:
        control.use_transport(None)
        control.reset_for_tests()
        outbox.use_executor(outbox.EXECUTOR_SERVER)
        _restore(monkey)


def session_switch_checks(r: Results, clock) -> None:
    """The enhancement switch is this session's too, and off is the default.

    "Off by default" is what the panel says, and persisting the switch made
    that true only of a fresh install - after that it was "off until you ever
    turn it on, then on in every session forever". A user who turned it on
    days ago, collapsed the panel, and pressed Add to Queue today got an
    enhanced prompt from a switch they could not see and did not set.
    """
    _setup(clock)
    enhance.set_enabled(True)
    r.check("the switch can be on at the end of a session", enhance.enabled() is True)
    executor.recover()
    r.check("and a new session starts with it off, because that is what the panel promises",
            enhance.enabled() is False)
    r.check("a session that starts with it already off leaves it alone rather than writing again",
            executor.recover().get("enhance_reset") is None)


def retention_checks(r: Results, clock) -> None:
    """A finished job leaves the queue. The queue is a queue, not a record."""
    _setup(clock)
    runtime = FakeRuntime()
    runtime.state = FakeRuntime.READY
    runtime.instance_id = "child-one"
    child = FakeChild(clock)
    monkey = []
    _install(monkey, runtime)
    try:
        control.use_transport(child)
        job = outbox.submit(_request(), PAGE)
        for _ in range(6):
            executor.step()
        child.finish(outbox.get(job["job_id"])["execution_id"])
        executor.step()
        r.check("a job that finished is still there a moment later, so the page that was waiting is told",
                outbox.get(job["job_id"])["state"] == outbox.COMPLETED)

        # The grace exists for the waiter and for nothing else. Past it, the
        # job has left the queue - history is where finished work lives.
        clock.tick(outbox.KEEP_TERMINAL_SECONDS + 1.0)
        outbox.submit(_request("something to sweep on"), PAGE)
        r.check("and gone once the grace is over, rather than sitting in the list for a week",
                all(item["job_id"] != job["job_id"] for item in outbox.jobs()), str([i["state"] for i in outbox.jobs()]))
        r.check("the grace is short enough to be a queue rather than a log",
                outbox.KEEP_TERMINAL_SECONDS <= 600.0, str(outbox.KEEP_TERMINAL_SECONDS))
    finally:
        control.use_transport(None)
        _restore(monkey)


def failure_checks(r: Results, clock) -> None:
    """Every dependency failure is its own sentence, not a timeout."""
    _setup(clock)
    runtime = FakeRuntime()
    child = FakeChild(clock)
    monkey = []
    _install(monkey, runtime)
    try:
        control.use_transport(child)

        runtime.state = FakeRuntime.INCOMPATIBLE
        runtime.error_code = errors.BRIDGE_VERSION_MISMATCH
        job = outbox.submit(_request(), PAGE)
        executor.step()
        executor.step()
        settled = outbox.get(job["job_id"])
        r.check("a WanGP whose setup needs attention fails the job with that code",
                settled["state"] == outbox.FAILED and settled["error"]["code"] == errors.BRIDGE_VERSION_MISMATCH, str(settled["error"]))

        runtime.state = FakeRuntime.CRASHED
        job = outbox.submit(_request(), PAGE)
        executor.step()
        executor.step()
        r.check("a crashed WanGP is an honest failure rather than a retry loop",
                outbox.get(job["job_id"])["error"]["code"] == errors.PROCESS_EXITED)

        runtime.state = FakeRuntime.READY
        child.can_compose = False
        job = outbox.submit(_request(), PAGE)
        for _ in range(4):
            executor.step()
        settled = outbox.get(job["job_id"])
        r.check("settings that cannot be read refuse the job rather than running it at defaults nobody chose",
                settled["state"] == outbox.FAILED and settled["error"]["code"] == wire.COMPOSE_UNAVAILABLE, str(settled["error"]))

        child.can_compose = True
        child.refuse = wire.MODEL_UNAVAILABLE
        job = outbox.submit(_request(), PAGE)
        for _ in range(6):
            executor.step()
        settled = outbox.get(job["job_id"])
        r.check("a model WanGP no longer has fails with its own code, never a silent substitution",
                settled["error"]["code"] == wire.MODEL_UNAVAILABLE, str(settled["error"]))

        # A WanGP that is up but cannot take a job yet is WAITED for, not
        # failed. READY means a process is alive and answered HTTP, and
        # Wan2GP builds its UI - and creates the generation service - after
        # that. Failing on the first ask threw away jobs that would have run
        # seconds later, which is the opposite of what walking away is for.
        child.refuse = ""
        child.available = False
        job = outbox.submit(_request(), PAGE)
        for _ in range(4):
            executor.step()
        waiting = outbox.get(job["job_id"])
        r.check("a WanGP that is up but not ready yet holds the job rather than throwing it away",
                waiting["state"] == outbox.ENSURING_WANGP and waiting["error"] is None, str(waiting["state"]))
        r.check("and says so while it waits, rather than looking stalled",
                "not ready" in waiting["stage"].lower() and "again in" in waiting["stage"].lower(), waiting["stage"])
        r.check("nothing was submitted to a child that said it could not execute", not child.submissions, str(child.submissions))

        # Bounded, though - and what it gives up to is the page, not a
        # failure. Running with the tab open is not walking away, but it is
        # generating, and a job thrown away is neither.
        for _ in range(executor.MAX_RETRYABLE_ATTEMPTS + 4):
            executor.step()
        settled = outbox.get(job["job_id"])
        r.check("a WanGP that never becomes able to run one hands the job to the page rather than binning it",
                settled["executor"] == outbox.EXECUTOR_BROWSER and settled["state"] == outbox.PENDING,
                f"{settled['executor']} {settled['state']}")
        r.check("and the page can claim it, which is the whole point of handing it over",
                outbox.claim(PAGE).get("job", {}).get("job_id") == job["job_id"], str(outbox.claim(PAGE))[:120])

        # And the moment it can, the job it was holding goes on.
        child.available = True
        job = outbox.submit(_request(prompt="held then run"), PAGE)
        for _ in range(3):
            executor.step()
        held = outbox.get(job["job_id"])
        r.check("a job held through a slow start runs as soon as the child can take it",
                held["state"] not in (outbox.FAILED, outbox.ENSURING_WANGP), held["state"])
    finally:
        control.use_transport(None)
        _restore(monkey)


def cancellation_checks(r: Results, clock) -> None:
    """Cancel is authoritative for our jobs, at every stage."""
    _setup(clock)
    runtime = FakeRuntime()
    runtime.state = FakeRuntime.READY
    child = FakeChild(clock)
    monkey = []
    _install(monkey, runtime)
    try:
        control.use_transport(child)
        asset = _asset()
        job = outbox.submit(_request(start={"kind": "clipboard_asset", "id": asset}), PAGE)
        held = outbox.get(job["job_id"])["inputs"]["start"]
        settled = outbox.cancel(job["job_id"])
        r.check("a job still waiting is cancelled", settled["state"] == outbox.CANCELLED)
        r.check("its picture starts its retention rather than vanishing",
                held in job_inputs.pinned_ids(include_released=True) and held not in job_inputs.pinned_ids())
        r.check("and nothing was asked of WanGP for a job that never reached it", not child.cancels)

        job = outbox.submit(_request("second"), PAGE)
        for _ in range(6):
            executor.step()
        execution_id = outbox.get(job["job_id"])["execution_id"]
        settled = outbox.cancel(job["job_id"])
        r.check("a job WanGP holds is cancelled there too",
                settled["state"] == outbox.CANCELLED and child.cancels == [execution_id], str(child.cancels))

        job = outbox.submit(_request("third"), PAGE)
        outbox.transition(job["job_id"], outbox.WAITING_FOR_CARD)
        child.busy = True
        settled = outbox.cancel(job["job_id"])
        r.check("cancelling a job that was waiting for the card never disturbs what holds it",
                settled["state"] == outbox.CANCELLED and len(child.cancels) == 1, str(child.cancels))

        first = outbox.submit(_request("fourth"), PAGE)
        second = outbox.submit(_request("fifth"), PAGE)
        answer = outbox.cancel_all()
        r.check("cancel everything reaches the server-executed jobs too",
                answer["cancelled"] >= 2
                and outbox.get(first["job_id"])["state"] == outbox.CANCELLED
                and outbox.get(second["job_id"])["state"] == outbox.CANCELLED, str(answer))
    finally:
        control.use_transport(None)
        _restore(monkey)


def event_checks(r: Results, clock) -> None:
    """Every durable transition is published, and nothing private is on it."""
    _setup(clock)
    before = events.revision()
    job = outbox.submit(_request(), PAGE)
    r.check("an admission advances the revision", events.revision() > before)
    outbox.transition(job["job_id"], outbox.ENSURING_WANGP)
    records, reset = events.replay(events.cursor(before))
    r.check("and a page holding an old cursor is given exactly what it missed",
            reset is False and len(records) == 2, f"{len(records)} {reset}")
    payloads = [record["payload"] for record in records]
    r.check("the frames carry the state and the stage",
            payloads[-1]["state"] == outbox.ENSURING_WANGP and payloads[-1]["stage"], str(payloads[-1]))
    blob = json.dumps(payloads)
    r.check("and never the prompt", PROMPT not in blob, blob[:120])
    r.check("nor any path", "/" not in blob.replace("\\/", ""), blob[:120])

    snapshot = interop.snapshot(PAGE)
    r.check("a sync is authoritative: the epoch, the revision and every job",
            snapshot["ok"] and snapshot["server_epoch"] == events.epoch() and len(snapshot["jobs"]) == 1, str(snapshot)[:160])
    r.check("it says whether the server runs the queue at all", snapshot["unattended"] is True)
    blob = json.dumps(snapshot["jobs"])
    r.check("a job on the wire carries the prompt it was composed with and no generated path",
            "generated_files" not in blob and "generated_count" in blob, blob[:160])


def event_loop_checks(r: Results, clock) -> None:
    """The snapshot route asks nobody: it reads what the executor left.

    Every call in ``wangp/control.py`` is a blocking socket read, and the
    sync route is an async handler on Forge's own event loop. One such call
    from there stalls every page on this Forge - every tab, every other
    extension - for as long as the child takes to answer, which is exactly
    the shape B19 forbids on this side of the wire. The rule is therefore
    not "keep it quick", it is "do not ask at all", and that is what is
    asserted: a snapshot makes zero control calls, reports what the last
    hello said, and reports "not known" once that answer is too old rather
    than presenting a stale card state as current.
    """
    _setup(clock)
    control.reset_for_tests()
    runtime = FakeRuntime()
    runtime.state = FakeRuntime.READY
    runtime.instance_id = "child-one"
    child = FakeChild(clock)
    asked = []
    monkey = []
    _install(monkey, runtime)

    def counted(operation, payload):
        asked.append(operation)
        return child(operation, payload)

    try:
        control.use_transport(counted)
        before = len(asked)
        summary = interop.snapshot(PAGE)["runtime"]
        r.check("a snapshot with nothing cached asks the child nothing at all",
                len(asked) == before, str(asked))
        r.check("and says it does not know what the card is doing, rather than guessing",
                summary["card_busy"] is None and summary["queue_depth"] is None, str(summary))
        r.check("while still answering what this process itself knows",
                summary["running"] is True and summary["state"] == FakeRuntime.READY, str(summary))

        # The executor thread is what keeps it fresh. One pass, then the
        # route reads the answer that pass left behind.
        child.busy = True
        control.hello()
        before = len(asked)
        summary = interop.snapshot(PAGE)["runtime"]
        r.check("once the executor has asked, the route reports the answer it left",
                summary["card_busy"] is True and summary["queue_depth"] == 2, str(summary))
        r.check("and still asked nothing itself", len(asked) == before, str(asked[before:]))

        # Age the cached answer rather than the clock: what makes it stale is
        # that the executor has not asked lately, and that is a fact about
        # this module's own state.
        control._last["at"] = control._last["at"] - control.HELLO_TTL - 1.0
        before = len(asked)
        summary = interop.snapshot(PAGE)["runtime"]
        r.check("an answer too old to mean anything reads as not known, not as the old one",
                summary["card_busy"] is None and summary["queue_depth"] is None, str(summary))
        r.check("and even then the route does not go and ask", len(asked) == before, str(asked[before:]))
    finally:
        control.use_transport(None)
        control.reset_for_tests()
        _restore(monkey)


def flush_attribution_checks(r: Results, clock) -> None:
    """"What you were looking at" and "what WanGP had recorded" are different.

    Both come out of ``load_model_form``, so nothing downstream can tell them
    apart by inspection - the difference is whether the page committed its
    live form on purpose before it pressed. The job carries what the page
    managed, and compose turns that into the source it records, so a reader
    of the queue can tell a job that ran at the settings on screen from one
    that ran at whatever WanGP happened to have kept.
    """
    _setup(clock)
    runtime = FakeRuntime()
    runtime.state = FakeRuntime.READY
    runtime.instance_id = "child-one"
    child = FakeChild(clock)
    monkey = []
    _install(monkey, runtime)
    def source_for(prompt, flush):
        """One press, driven to a generated file. Returns its recorded base."""
        job = outbox.submit(_request(prompt=prompt), PAGE, settings_flush=flush)
        _run(child, runtime)
        child.finish(job["execution_id"])
        _run(child, runtime)
        return outbox.get(job["job_id"])["snapshot"]["source"]

    try:
        control.use_transport(child)
        r.check("a job whose page committed its live form composes from a flushed base",
                source_for(PROMPT, wire.FLUSH_COMMITTED) == wire.BASE_FLUSHED)
        r.check("one whose page never got to still composes, from the recorded form, saying so",
                source_for("another", "") == wire.BASE_RECORDED)
        r.check("a flush WanGP refused is not dressed up as one that happened",
                source_for("a third", wire.FLUSH_SUPPRESSED) == wire.BASE_RECORDED)
        r.check("nor is one that ran out of time without the record moving",
                source_for("a fourth", wire.FLUSH_UNAVAILABLE) == wire.BASE_RECORDED)
        r.check("every one of them reached WanGP regardless - a flush is an optimisation, never a gate",
                len(child.submissions) == 4, str(len(child.submissions)))

        # Factory is still factory. A press that flushed against a WanGP with
        # nothing recorded has carried nothing, and must not claim otherwise.
        child.source = wire.BASE_FACTORY
        r.check("a flush against a WanGP with nothing recorded is still factory, not a flushed base",
                source_for("a fifth", wire.FLUSH_COMMITTED) == wire.BASE_FACTORY)
    finally:
        control.use_transport(None)
        _restore(monkey)


def privacy_checks(r: Results, clock) -> None:
    """Nothing this path writes down carries a prompt or a path."""
    scratch = _setup(clock)
    process_log.begin("i" * 32, "executor privacy check")
    runtime = FakeRuntime()
    runtime.state = FakeRuntime.READY
    child = FakeChild(clock)
    monkey = []
    _install(monkey, runtime)
    try:
        control.use_transport(child)
        job = outbox.submit(_request("a secret prompt about a lighthouse"), PAGE)
        for _ in range(8):
            executor.step()
        child.finish(outbox.get(job["job_id"])["execution_id"], ["/home/someone/wangp/outputs/secret.mp4"])
        executor.step()
    finally:
        control.use_transport(None)
        _restore(monkey)
    log = pathlib.Path(str(process_log.path()))
    text = log.read_text(encoding="utf-8") if log.exists() else ""
    r.check("the journal has no prompt", "secret prompt" not in text, text[-200:])
    r.check("and no generated path", "secret.mp4" not in text and "/home/someone" not in text, text[-200:])
    r.check("but does say the job finished and with how many files",
            "completed" in text.lower() or "file(s)" in text, text[-200:])
    stored = json.loads((config.path_of(outbox.OUTBOX_NAME)).read_text(encoding="utf-8"))
    r.check("the document keeps the generated path for a later viewer",
            any(item.get("generated_files") for item in stored["jobs"]), str(stored)[:200])
    r.check("at the current schema", stored["schema"] == outbox.SCHEMA)


def migration_checks(r: Results, clock) -> None:
    """A document written before any of this reads as exactly what it was."""
    _setup(clock)
    old = {
        "schema": 2,
        "jobs": [{
            "job_id": "1" * 16,
            "request": {"request_id": "2" * 32, "prompt": "old", "images": {}, "start": "auto"},
            "page": PAGE, "origin": "clipboard", "created_at": clock(), "updated_at": clock(),
            "state": "pending", "attempts": 1, "sent": False, "lease": None,
            "result": {"ok": True, "status": "queued"}, "error": None, "retry_of": "",
            "history_recorded": False, "model": {}, "enhance_requested": False, "enhance": None,
            "wangp": {"state": "waiting", "position": 1, "queue_depth": 1, "seen_at": clock()},
        }],
    }
    config.write_document(outbox.OUTBOX_NAME, old)
    listed = outbox.jobs()
    r.check("an old job keeps its state", len(listed) == 1 and listed[0]["state"] == outbox.PENDING, str(listed)[:160])
    r.check("and is read as browser-executed, because that is all there was",
            listed[0]["executor"] == outbox.EXECUTOR_BROWSER)
    r.check("its WanGP tracking still reads", listed[0]["wangp"]["state"] == outbox.WANGP_WAITING)
    r.check("and a page can still claim it, because its rules did not change",
            outbox.claim(PAGE).get("job", {}).get("job_id") == "1" * 16, str(outbox.claim(PAGE))[:120])
    r.check("and the new fields are present and empty rather than missing",
            listed[0]["execution_id"] == "" and listed[0]["snapshot"] is None and listed[0]["generated_count"] == 0)
    r.check("and the executor does not pick it up: its rules are the old ones",
            outbox.next_executable() is None)
    # Read across a restart, an old job goes the same way a new one does. Its
    # page is gone with the browser that held it, so keeping the record would
    # leave a job in the list that nothing on earth was going to advance.
    r.check("and a new session clears it with everything else",
            executor.recover()["cleared"] == 1 and outbox.jobs() == [], str(outbox.jobs()))


def enhanced_checks(r: Results, clock) -> None:
    """An enhanced job reaches the WanGP stage carrying the written prompt."""
    _setup(clock)

    written = {"state": enhance.LLM_QUEUED, "prompt": "", "stage": "waiting for the GPU"}

    class FakeApi:
        API_VERSION = 1

        class Rejected(Exception):
            pass

        def capabilities(self):
            return {"api_version": 1, "enabled": True, "configured": True, "vision": True, "model": "/m/q.gguf", "reason": ""}

        def submit_minimax(self, prompt, **keywords):
            return "b" * 16

        def status(self, llm_id):
            return dict(written, position=0, elapsed=12.0, image_used="", image_ignored=[], system_override=False, cancelling=False)

        def subscribe(self, llm_id):
            return iter([{"event": "status", "stage": written["stage"]},
                         {"event": "chunk", "text": "the whole enhanced prompt, replayed"},
                         {"event": "done"}])

    enhance.use_api(FakeApi())
    enhance.set_enabled(True)
    try:
        job = outbox.submit(_request(), PAGE, model={"type": "minimax_h3_fl2va", "architecture": "minimax_h3_fl2va"})
        r.check("an enhanced press waits for its prompt", job["state"] == outbox.ENHANCING, job["state"])

        runtime = FakeRuntime()
        runtime.state = FakeRuntime.READY
        child = FakeChild(clock)
        monkey = []
        _install(monkey, runtime)
        try:
            control.use_transport(child)
            executor.step()
            r.check("the feed's stage text reaches the job, in the runtime's own words",
                    "GPU" in outbox.get(job["job_id"])["stage"], outbox.get(job["job_id"])["stage"])
            r.check("and the content-bearing events on it are never read into anything",
                    "enhanced prompt, replayed" not in json.dumps(outbox.get(job["job_id"])))

            written.update({"state": enhance.LLM_DONE, "prompt": "a rewritten prompt for the model"})
            executor.step()
            settled = outbox.get(job["job_id"])
            r.check("a finished enhancement moves the job past the enhancer",
                    settled["state"] in (outbox.ENHANCED, outbox.ENSURING_WANGP), settled["state"])
            r.check("with the written prompt persisted before the WanGP stage begins",
                    settled["request"]["prompt"] == "a rewritten prompt for the model", settled["request"]["prompt"])
            r.check("and the typed prompt is kept beside it", settled["enhance"]["prompt_original"] == PROMPT)

            for _ in range(8):
                executor.step()
            r.check("the WanGP stage runs with the written prompt",
                    child.submissions and child.submissions[0]["prompt"] == "a rewritten prompt for the model",
                    str(child.submissions)[:160])
        finally:
            control.use_transport(None)
            _restore(monkey)
    finally:
        enhance.use_api(None)
        enhance.set_enabled(False)


def preflight_checks(r: Results, clock) -> None:
    """Four refusals, told apart, none of them a cold runtime."""
    _setup(clock)

    class FakeApi:
        API_VERSION = 1

        def __init__(self):
            self.enabled = True
            self.configured = True
            self.vision = True

        def capabilities(self):
            return {"api_version": 1, "enabled": self.enabled, "configured": self.configured,
                    "vision": self.vision, "model": "/m/q.gguf", "reason": "because"}

    fake = FakeApi()
    enhance.use_api(fake)
    try:
        plan = {"slots": {}}
        r.check("a working install is startable", enhance.preflight(plan)[0] == "")
        fake.enabled = False
        r.check("LLM Studio switched off is a policy state with its own code",
                enhance.preflight(plan)[0] == errors.ENHANCE_SWITCHED_OFF)
        fake.enabled = True
        fake.configured = False
        r.check("no configured model is its own code, and nothing is downloaded",
                enhance.preflight(plan)[0] == errors.ENHANCE_NOT_CONFIGURED)
        fake.configured = True
        fake.vision = False
        r.check("a text-only model refuses a plan with a picture",
                enhance.preflight({"slots": {"first_frame": {}}})[0] == errors.ENHANCE_NO_VISION)
        r.check("and takes one without", enhance.preflight(plan)[0] == "")
        enhance.use_api(None)
        r.check("a missing extension is its own code, never 'switched off'",
                enhance.preflight(plan)[0] == errors.ENHANCE_EXTENSION_MISSING)
    finally:
        enhance.use_api(None)


def run() -> Results:
    r = Results("clipboard executor")
    clock = _Clock()
    admission_checks(r, clock)
    walkaway_checks(r, clock)
    card_checks(r, clock)
    stuck_card_checks(r, clock)
    input_lifetime_checks(r, clock)
    write_discipline_checks(r, clock)
    recovery_checks(r, clock)
    retention_checks(r, clock)
    session_switch_checks(r, clock)
    incapable_build_checks(r, clock)
    inheritance_toggle_checks(r, clock)
    failure_checks(r, clock)
    cancellation_checks(r, clock)
    event_checks(r, clock)
    event_loop_checks(r, clock)
    flush_attribution_checks(r, clock)
    privacy_checks(r, clock)
    migration_checks(r, clock)
    enhanced_checks(r, clock)
    preflight_checks(r, clock)
    outbox.reset_for_tests()
    executor.reset_for_tests()
    return r


if __name__ == "__main__":
    raise SystemExit(0 if run().report() else 1)
