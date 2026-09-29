"""The card lease: another extension borrowing WanGP's card, as Forge keeps it.

``minipaint_neo.wangp.turns`` is read by SD-Neo-ModelSwitchRefiner, which puts
a text-to-speech model of eighteen to twenty gigabytes on the card WanGP runs
on, between WanGP's jobs. What is held here, in the order it matters:

*   the record's shape is closed - the keys its owner checks, and no others;
*   the Clipboard's gate closes the moment a lease is live: no job starts
    WanGP, none is composed, none is submitted, and the 60-second "submit
    anyway" courtesy does not reach a job held for a lease - while the job
    WanGP is already running is followed to its end and never aborted;
*   the phases mean what the contract says: ``pending`` while a task of
    ours is on the card, ``holding`` until the bridge says the card is clear,
    ``held`` when it does (or WanGP is not running at all), ``released`` and
    ``expired`` open the gate and tell the bridge to resume, ``refused`` says
    why - and a lease already ``held`` is never refused afterwards;
*   ``wanted`` is a Clipboard job waiting for the card, or WanGP work waiting
    on the hold;
*   one lease at a time, the same owner gets its own back, and one that is
    not renewed for twenty seconds expires;
*   none of the functions raises, and none of them touches the control
    plane - the executor thread is the one that does.

The WanGP child is a fake control plane answering the way bridge 1.12.0
does (and, where it matters, the way 1.11.0 does), driven by the test, so a
task can be made to finish, a hold to be confirmed and a flush to be refused
at exactly the moment a check needs.
"""

from harness import Results, setup_path

setup_path()

import json  # noqa: E402
import os  # noqa: E402
import pathlib  # noqa: E402
import tempfile  # noqa: E402

from minipaint_neo import events  # noqa: E402
from minipaint_neo.clipboard import config, executor, outbox  # noqa: E402
from minipaint_neo.wangp import config as wangp_config  # noqa: E402
from minipaint_neo.wangp import control, errors, lock, presence, process_log, runtime, turns  # noqa: E402
from minipaint_neo.wangp import protocol as wire  # noqa: E402
from minipaint_neo.wangp.errors import IntegrationError  # noqa: E402

PAGE = "a" * 16
OWNER = "Voice Box"
GPU_UUID = "GPU-11112222-3333-4444-5555-666677778888"
GB = 1024 ** 3


class Clock:
    def __init__(self):
        self.now = 1_700_000_000.0

    def __call__(self):
        return self.now

    def tick(self, seconds):
        self.now += seconds


class FakeRuntime:
    """The managed child, as far as a lease and the executor can see it."""

    def __init__(self):
        self.state = runtime.STOPPED
        self.instance_id = ""
        self.error_code = ""
        self.starts = 0

    def is_running(self):
        return self.state in (runtime.READY, runtime.STARTING, runtime.STOPPING, runtime.INCOMPATIBLE)

    def start(self, *args, **keywords):
        self.starts += 1
        self.state = runtime.READY
        self.instance_id = "child-one"
        return self

    def bridge_secret(self):
        return "s3cr3t"

    def snapshot(self):
        return {"state": self.state, "instance_id": self.instance_id, "running": self.state == runtime.READY}

    def ready(self, instance="child-one"):
        self.state = runtime.READY
        self.instance_id = instance


class Child:
    """The control plane, answering the way bridge 1.12.0 does - or 1.11.0."""

    def __init__(self, clock):
        self.clock = clock
        self.can_hold = True
        self.word = wire.HOLD_HELD
        self.task_running = False
        self.active = ""
        self.waiter = False
        self.queue_length = 0
        self.claim = wire.CLAIM_TAKEN
        self.busy_with = ""
        self.vram_free = 30 * GB
        self.vram_total = 32 * GB
        self.holds, self.resumes, self.flushes = [], [], []
        self.flush_refusal = ""
        self.hold_refusal = 0
        self.garble = False
        self.busy = False
        self.submissions, self.composes, self.cancels, self.forgotten = [], [], [], []
        self.records = {}

    def activity(self):
        return {"worker": self.task_running or self.waiter, "task_running": self.task_running,
                "active_client_id": self.active, "queue_length": self.queue_length, "waiter": self.waiter,
                "vram_free": self.vram_free, "vram_total": self.vram_total}

    def __call__(self, operation, payload):
        if operation == wire.CONTROL_HELLO:
            answer = {"ok": True, "control_version": wire.CONTROL_VERSION, "protocol": wire.PROTOCOL,
                      "bridge_version": "1.12.0" if self.can_hold else "1.11.0",
                      "can_execute": True, "can_compose": True, "service": True, "service_possible": True,
                      "generation_running": self.busy, "queue_depth": 2 if self.busy else 0, "instance": "child-one"}
            if self.can_hold:
                answer.update({"capabilities": {"hold": True}, "hold": wire.HOLD_NONE, **self.activity()})
            return 200, answer
        if operation in (wire.CONTROL_HOLD, wire.CONTROL_RESUME, wire.CONTROL_FLUSH) and not self.can_hold:
            return 404, {"ok": False, "code": "REQUEST_INVALID", "message": "no such operation"}
        if operation == wire.CONTROL_HOLD:
            self.holds.append(dict(payload))
            if self.hold_refusal:
                self.hold_refusal -= 1
                return 503, {"ok": False, "code": wire.CONTROL_UNAVAILABLE, "message": "busy"}
            return 200, {"ok": True, "lease": payload["lease"], "hold": "mostly" if self.garble else self.word,
                         **self.activity(), "claim": self.claim, "busy_with": self.busy_with,
                         "expires_in_s": payload["ttl_s"]}
        if operation == wire.CONTROL_RESUME:
            self.resumes.append(dict(payload))
            return 200, {"ok": True, "lease": payload["lease"], "hold": wire.HOLD_NONE, "resumed": True, **self.activity()}
        if operation == wire.CONTROL_FLUSH:
            self.flushes.append(dict(payload))
            if self.flush_refusal:
                return 409, {"ok": False, "code": self.flush_refusal, "message": "refused"}
            self.vram_free = 31 * GB
            return 200, {"ok": True, "lease": payload["lease"], "hold": wire.HOLD_HELD, **self.activity(),
                         "flushed": payload["level"], "parts": ["model", "cache"]}
        if operation == wire.CONTROL_COMPOSE:
            self.composes.append(dict(payload))
            return 200, {"ok": True, "settings": {"steps": 30, "model_type": "t2v"}, "source": wire.BASE_RECORDED,
                         "model_type": "t2v", "wan2gp_version": "13.14", "residency_key": "t2v||",
                         "model": {"type": "t2v", "label": "T2V"}}
        if operation == wire.CONTROL_SUBMIT:
            self.submissions.append(dict(payload))
            record = self.records.setdefault(payload["execution_id"], {
                "execution_id": payload["execution_id"], "state": wire.EXEC_ACCEPTED,
                "submitted_at": self.clock(), "instance": "child-one"})
            return 200, {"ok": True, "record": record}
        if operation == wire.CONTROL_STATUS:
            return 200, {"ok": True, "instance": "child-one",
                         "records": {key: self.records[key] for key in payload["execution_ids"] if key in self.records}}
        if operation == wire.CONTROL_CANCEL:
            self.cancels.append(payload["execution_id"])
            return 200, {"ok": True, "record": {"execution_id": payload["execution_id"], "state": wire.EXEC_CANCELLED}}
        if operation == wire.CONTROL_FORGET:
            self.forgotten.extend(payload["execution_ids"])
            return 200, {"ok": True, "forgotten": len(payload["execution_ids"])}
        return 404, {"ok": False, "code": "REQUEST_INVALID"}

    def generating(self, execution_id):
        self.records[execution_id] = {"execution_id": execution_id, "state": wire.EXEC_RUNNING,
                                      "stage": "generating", "instance": "child-one"}

    def finish(self, execution_id):
        self.records[execution_id] = {"execution_id": execution_id, "state": wire.EXEC_DONE,
                                      "generated_files": ["/wangp/outputs/one.mp4"], "instance": "child-one"}


class World:
    """One scratch Forge: its data roots, a WanGP folder, the fakes, no threads."""

    def __init__(self, bridge_version="1.12.0", configured=True):
        self.clock = Clock()
        self.base = pathlib.Path(tempfile.mkdtemp(prefix="minipaint-turns-"))
        config.use_config_dir(str(self.base))
        wangp_config.use_config_dir(str(self.base))
        process_log.use_log_dir(self.base / "logs")
        outbox.reset_for_tests()
        outbox.use_clock(self.clock)
        outbox.use_executor(outbox.EXECUTOR_SERVER)
        executor.reset_for_tests()
        executor.use_clock(self.clock)
        executor.use_sleep(lambda _seconds: None)
        executor.use_thread(False)
        events.reset_for_tests()
        control.reset_for_tests()
        presence.forget()
        turns.reset_for_tests()
        turns.use_clock(self.clock)
        self.runtime = FakeRuntime()
        self.child = Child(self.clock)
        control.use_transport(self.child)
        self._saved = [(runtime, "current", runtime.current), (runtime, "start", runtime.start)]
        runtime.current = lambda: self.runtime
        runtime.start = lambda *args, **keywords: self.runtime.start()
        outbox.use_running(lambda: self.runtime.state == runtime.READY)
        self.root = self.base / "WanGP"
        self.install_bridge(bridge_version)
        if configured:
            self.write_config()

    def install_bridge(self, version):
        folder = self.root / "plugins" / "wan2gp-minipaint-bridge"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "plugin_info.json").write_text(json.dumps({"name": "wan2gp-minipaint-bridge", "version": version,
                                                             "bridge_version": version}), encoding="utf-8")
        (self.root / "wgp_config.json").write_text(json.dumps({"enabled_plugins": ["wan2gp-minipaint-bridge"]}),
                                                   encoding="utf-8")
        (self.root / "wgp.py").write_text("# stand-in\n", encoding="utf-8")

    def write_config(self):
        wangp_config.atomic_write(wangp_config.config_path(), json.dumps({
            "schema_version": wangp_config.SCHEMA_VERSION, "initialized": True, "wangp_root": str(self.root),
            "runtime": {"type": "venv", "prefix": str(self.base / "env"), "display_name": "wan",
                        "launch_strategy": "direct_python"},
            "gpu": {"uuid": GPU_UUID},
            "integration": {"proxy_path": "/wan2gp", "auto_start": "lazy", "auth_checked": False},
        }))
        presence.forget()

    def close(self):
        for module, name, value in self._saved:
            setattr(module, name, value)
        control.use_transport(None)
        control.reset_for_tests()
        turns.reset_for_tests()
        outbox.reset_for_tests()
        executor.reset_for_tests()
        process_log.use_log_dir(None)
        wangp_config.use_config_dir(None)
        presence.forget()

    def steps(self, count=1):
        for _ in range(count):
            executor.step()

    def job(self, prompt="a lighthouse in a storm"):
        return outbox.submit({"request_id": os.urandom(16).hex(), "prompt": prompt}, PAGE)

    def journal(self):
        path = pathlib.Path(str(process_log.path()))
        return path.read_text(encoding="utf-8") if path.exists() else ""


def _fresh(world):
    """Let the next public call read the outbox rather than the executor's count."""
    world.clock.tick(turns.DEMAND_FRESH_SECONDS + 0.5)


# ----------------------------------------------------------------- shape --


def shape_checks(r: Results) -> None:
    world = World(configured=False)
    try:
        records = [turns.request(OWNER), turns.state("f" * 32), turns.flush("f" * 32, "soft"), turns.release("f" * 32)]
        r.check("every function answers a record with exactly the documented keys",
                all(tuple(record) == turns.LEASE_KEYS for record in records), str([tuple(item) for item in records]))
        r.check("and a wangp block with exactly its six", all(tuple(record["wangp"]) == turns.WANGP_KEYS for record in records))
        r.check("the keys are the contract's, word for word",
                turns.LEASE_KEYS == ("version", "lease", "owner", "phase", "reason", "card_uuid", "wanted", "bridge_hold",
                                     "expires_in_s", "wangp")
                and turns.WANGP_KEYS == ("running", "task_running", "queue_length", "jobs_waiting", "vram_free_bytes",
                                         "vram_total_bytes"))
        found = turns.report()
        r.check("report answers exactly its own keys, with or without a lease", tuple(found) == turns.REPORT_KEYS
                and tuple(found["wangp"]) == turns.WANGP_KEYS, str(tuple(found)))
        r.check("the version is 1", all(record["version"] == 1 for record in records) and found["version"] == 1)
        for forbidden in ("port", "pid", "secret", "path", "root"):
            r.check(f"no key names a {forbidden}", not any(forbidden in key for key in turns.LEASE_KEYS + turns.REPORT_KEYS))
    finally:
        world.close()


def refusal_checks(r: Results) -> None:
    """Refused, and why: not set up, not this Forge's, or not holdable."""
    world = World(configured=False)
    try:
        record = turns.request(OWNER)
        r.check("a lease with no WanGP set up is refused, and says so",
                record["phase"] == turns.REFUSED and record["reason"] == turns.TEXT_NOT_SET_UP, str(record))
        r.check("and closes no gate", turns.gate_closed() is False)
        r.check("an owner with no name is refused too", turns.request("   ")["phase"] == turns.REFUSED)
    finally:
        world.close()

    world = World()
    try:
        path = lock.lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": 1, "forge_pid": os.getppid(), "forge_start": lock.process_start(os.getppid()),
                                    "created_at": "now", "host": "here"}), encoding="utf-8")
        record = turns.request(OWNER)
        r.check("a WanGP another Forge on this machine runs is not this one's to hold",
                record["phase"] == turns.REFUSED and record["reason"] == turns.TEXT_OTHER_FORGE, str(record))
        path.unlink()
    finally:
        world.close()

    # An older bridge, already known from a recent hello: refused on the spot.
    world = World()
    try:
        world.runtime.ready()
        world.child.can_hold = False
        control.hello()
        record = turns.request(OWNER)
        r.check("WanGP running with a bridge that cannot hold is refused, naming the bridge it needs",
                record["phase"] == turns.REFUSED and "1.12.0" in record["reason"], str(record))
        r.check("and no hold was ever sent to it", world.child.holds == [])
    finally:
        world.close()

    # ...and not yet known: found out by the executor, and refused then.
    world = World()
    try:
        world.runtime.ready()
        world.child.can_hold = False
        record = turns.request(OWNER)
        r.check("with nothing heard from the bridge yet the lease is asking, gate closed",
                record["phase"] == turns.HOLDING and record["reason"] == turns.TEXT_ASKING and turns.gate_closed(), str(record))
        world.steps()
        record = turns.state(record["lease"])
        r.check("the executor learns it cannot hold and the lease is refused, naming 1.12.0",
                record["phase"] == turns.REFUSED and "1.12.0" in record["reason"], str(record))
        r.check("the gate opens again, and nothing was sent that it would not understand",
                turns.gate_closed() is False and world.child.holds == [])
    finally:
        world.close()

    world = World()
    try:
        first = turns.request(OWNER)
        other = turns.request("Somebody Else")
        r.check("one lease at a time: another owner is refused, and told whose the card is",
                other["phase"] == turns.REFUSED and OWNER in other["reason"] and other["lease"] != first["lease"], str(other))
        r.check("and the lease that is live is untouched", turns.state(first["lease"])["phase"] == turns.HELD)
        again = turns.request(OWNER)
        r.check("a second request by the same owner returns the live lease", again["lease"] == first["lease"], str(again))
    finally:
        world.close()


def stopped_checks(r: Results) -> None:
    """WanGP not running: held at once, and no Clipboard job starts it."""
    world = World()
    try:
        record = turns.request(OWNER, purpose="Speech engine", need_bytes=19 * GB)
        r.check("with WanGP stopped a lease is held at once: nothing of WanGP's is on the card",
                record["phase"] == turns.HELD and record["bridge_hold"] == wire.HOLD_NONE and record["reason"] == "", str(record))
        r.check("it names WanGP's card", record["card_uuid"] == GPU_UUID, record["card_uuid"])
        r.check("and a WanGP that is not running has no task running",
                record["wangp"]["running"] is False and record["wangp"]["task_running"] is False, str(record["wangp"]))
        job = world.job()
        world.steps(6)
        held = outbox.get(job["job_id"])
        r.check("a Clipboard job pressed now does not start WanGP", world.runtime.starts == 0, str(world.runtime.starts))
        r.check("it is held before it can, and says who has the card",
                held["state"] == outbox.ENSURING_WANGP and held["stage"].startswith(executor.LENT_STAGE_PREFIX + OWNER),
                f"{held['state']} {held['stage']}")
        _fresh(world)
        record = turns.state(record["lease"])
        r.check("and the lease says a Clipboard job wants the card", record["wanted"] is True
                and record["wangp"]["jobs_waiting"] == 1, str(record))
        flushed = turns.flush(record["lease"], "soft")
        r.check("a flush with WanGP stopped asks nobody and says there was nothing to move",
                flushed["phase"] == turns.HELD and "none of it was on the card" in flushed["reason"]
                and world.child.flushes == [], str(flushed))
        released = turns.release(record["lease"], reason="done")
        r.check("release opens the gate at once", released["phase"] == turns.RELEASED and turns.gate_closed() is False)
        world.steps(3)
        r.check("and the job goes on to start WanGP as it would have", world.runtime.starts == 1, str(world.runtime.starts))
        r.check("no bridge was told anything: there was nothing to resume", world.child.resumes == [])
    finally:
        world.close()

    world = World(bridge_version="1.11.0")
    try:
        record = turns.request(OWNER)
        r.check("with WanGP stopped and an older bridge installed, held - and bridge_hold says a WanGP started now could not be",
                record["phase"] == turns.HELD and record["bridge_hold"] == wire.HOLD_UNSUPPORTED
                and "1.12.0" in record["reason"], str(record))
    finally:
        world.close()


def ready_checks(r: Results) -> None:
    """WanGP idle: asked, held, renewed, resumed."""
    world = World()
    try:
        world.runtime.ready()
        record = turns.request(OWNER, purpose="Speech engine")
        r.check("on a running WanGP the lease starts by asking the bridge", record["phase"] == turns.HOLDING
                and record["reason"] == turns.TEXT_ASKING and record["bridge_hold"] == wire.HOLD_NONE, str(record))
        r.check("and the gate is closed from that moment", turns.gate_closed() is True)
        r.check("the function itself asked nobody: the control plane is the executor's",
                world.child.holds == [])
        world.steps()
        r.check("the executor sends the hold, with the lease, the bridge's timer and the name",
                len(world.child.holds) == 1 and world.child.holds[0] == {
                    "lease": record["lease"], "ttl_s": turns.BRIDGE_TTL_SECONDS, "label": "Speech engine"},
                str(world.child.holds))
        record = turns.state(record["lease"])
        r.check("and the lease is held once the bridge says so",
                record["phase"] == turns.HELD and record["bridge_hold"] == wire.HOLD_HELD, str(record))
        r.check("with WanGP's activity from the bridge's own answer",
                record["wangp"] == {"running": True, "task_running": False, "queue_length": 0, "jobs_waiting": 0,
                                    "vram_free_bytes": 30 * GB, "vram_total_bytes": 32 * GB}, str(record["wangp"]))
        r.check("renewed, it says it has the whole twenty seconds again", record["expires_in_s"] == turns.LEASE_TTL_SECONDS,
                str(record["expires_in_s"]))
        world.steps(2)
        r.check("every pass renews the bridge's timer", len(world.child.holds) == 3, str(len(world.child.holds)))
        released = turns.release(record["lease"])
        r.check("released, the bridge is not yet told - that is the executor's next pass",
                released["phase"] == turns.RELEASED and world.child.resumes == [], str(released))
        world.steps()
        r.check("which tells it, once", world.child.resumes == [{"lease": record["lease"]}], str(world.child.resumes))
        world.steps(2)
        record = turns.state(record["lease"])
        r.check("and then the lease says the bridge has let go", record["bridge_hold"] == wire.HOLD_NONE
                and len(world.child.resumes) == 1 and len(world.child.holds) == 3, str(record))
        r.check("and the executor has nothing more to do for it", turns.needs_driver() is False)
    finally:
        world.close()


def pending_checks(r: Results) -> None:
    """A job of ours on the card: pending, never aborted, then held."""
    world = World()
    try:
        world.runtime.ready()
        job = world.job()
        world.steps(6)
        execution_id = outbox.get(job["job_id"])["execution_id"]
        world.child.generating(execution_id)
        world.steps()
        r.check("the job is generating on WanGP", outbox.get(job["job_id"])["state"] == outbox.GENERATION_RUNNING)
        record = turns.request(OWNER)
        r.check("a lease asked for while it runs says pending: the task on the card is Mini Paint's",
                record["phase"] == turns.PENDING and record["reason"] == turns.TEXT_PENDING, str(record))
        world.child.word, world.child.task_running, world.child.active = wire.HOLD_HOLDING, True, execution_id
        world.steps()
        r.check("the hold is asked for at once, so WanGP stops after it rather than taking its next task",
                len(world.child.holds) == 1)
        record = turns.state(record["lease"])
        r.check("and while the bridge names our job as the task on the card, pending",
                record["phase"] == turns.PENDING and record["bridge_hold"] == wire.HOLD_HOLDING, str(record))
        world.child.finish(execution_id)
        world.child.word, world.child.task_running, world.child.active = wire.HOLD_HELD, False, ""
        world.steps(2)
        r.check("the job runs to its end and completes", outbox.get(job["job_id"])["state"] == outbox.COMPLETED,
                outbox.get(job["job_id"])["state"])
        r.check("it was never cancelled or aborted", world.child.cancels == [], str(world.child.cancels))
        r.check("and then the lease is held", turns.state(record["lease"])["phase"] == turns.HELD)
    finally:
        world.close()


def busy_checks(r: Results) -> None:
    """Somebody else's task on the card: holding, and why."""
    world = World()
    try:
        world.runtime.ready()
        world.child.word, world.child.task_running, world.child.active = wire.HOLD_HOLDING, True, ""
        record = turns.request(OWNER)
        world.steps()
        record = turns.state(record["lease"])
        r.check("a task that is not ours is waited for as holding, not pending",
                record["phase"] == turns.HOLDING and record["reason"] == turns.TEXT_FINISHING, str(record))
        world.child.task_running = False
        world.child.claim, world.child.busy_with = wire.CLAIM_BUSY, "Deepy"
        world.steps()
        record = turns.state(record["lease"])
        r.check("another of WanGP's own GPU processes on the card is named", record["phase"] == turns.HOLDING
                and "Deepy" in record["reason"], str(record))
        world.child.hold_refusal = 1000
        lease = record["lease"]
        for _ in range(3):
            # The owner renews every fifteen seconds; the bridge stops answering.
            world.clock.tick(15.0)
            turns.state(lease)
            world.steps()
        record = turns.state(lease)
        r.check("a bridge that did not answer is asked again, never taken as held",
                record["phase"] == turns.HOLDING and wire.CONTROL_UNAVAILABLE in record["reason"]
                and len(world.child.holds) >= 5, str(record))
        world.child.hold_refusal = 0
        world.child.garble = True
        world.child.word = wire.HOLD_HELD
        world.steps()
        record = turns.state(lease)
        r.check("an answer that names no hold state is no answer: holding, with bridge_hold in its own vocabulary",
                record["phase"] == turns.HOLDING and record["bridge_hold"] in wire.HOLD_STATES, str(record))
    finally:
        world.close()


def gate_checks(r: Results) -> None:
    """The executor's gate: nothing started, composed or submitted; no 60 s courtesy."""
    world = World()
    try:
        world.runtime.ready()
        world.child.busy = True
        job = world.job()
        world.steps(4)
        r.check("a job reaches the wait for the card", outbox.get(job["job_id"])["state"] == outbox.WAITING_FOR_CARD)
        lease = turns.request(OWNER)["lease"]
        for _ in range(24):
            # Six minutes, the owner renewing every fifteen seconds.
            world.clock.tick(15.0)
            turns.state(lease)
            world.steps()
        held = outbox.get(job["job_id"])
        r.check("held for a lease, it is never submitted - not after a minute, not after six",
                world.child.submissions == [] and held["state"] == outbox.WAITING_FOR_CARD, str(world.child.submissions))
        r.check("and says so", held["stage"].startswith(executor.LENT_STAGE_PREFIX), held["stage"])
        turns.release(lease)
        world.steps()
        back = outbox.get(job["job_id"])
        r.check("given back, it is its own stage again", not back["stage"].startswith(executor.LENT_STAGE_PREFIX)
                and back["state"] == outbox.WAITING_FOR_CARD, back["stage"])
        world.steps(2)
        r.check("with a fresh courtesy clock: WanGP busy with its own work is waited for, not queued behind at once",
                world.child.submissions == [] and "busy" in outbox.get(job["job_id"])["stage"].lower(),
                outbox.get(job["job_id"])["stage"])
        world.child.busy = False
        world.steps(3)
        r.check("and it is submitted once the card is free", len(world.child.submissions) == 1, str(world.child.submissions))
    finally:
        world.close()

    world = World()
    try:
        world.runtime.ready()
        job = world.job()
        world.steps(2)
        r.check("a job about to compose", outbox.get(job["job_id"])["state"] == outbox.COMPOSING)
        lease = turns.request(OWNER)["lease"]
        world.steps(3)
        r.check("is not composed while the card is lent - no settings frozen during somebody else's turn",
                world.child.composes == [] and outbox.get(job["job_id"])["state"] == outbox.COMPOSING)
        turns.release(lease)
        world.steps(4)
        r.check("and composes and submits once it is given back",
                len(world.child.composes) == 1 and len(world.child.submissions) == 1)
    finally:
        world.close()

    world = World()
    try:
        world.runtime.ready()
        job = world.job()
        world.steps(3)
        outbox.transition(job["job_id"], outbox.SUBMITTING_WANGP)
        lease = turns.request(OWNER)["lease"]
        world.steps(3)
        r.check("a job a step from submission is not submitted while the card is lent",
                world.child.submissions == [] and outbox.get(job["job_id"])["state"] == outbox.SUBMITTING_WANGP)
        turns.release(lease)
        world.steps(2)
        r.check("and is, once given back", len(world.child.submissions) == 1)
    finally:
        world.close()


def expiry_checks(r: Results) -> None:
    """Not renewed for twenty seconds: expired, gate open, bridge told."""
    world = World()
    try:
        world.runtime.ready()
        lease = turns.request(OWNER)["lease"]
        world.steps()
        # The contract's number, written as a number: a check that ticked by
        # the module's own constant would move with it and hold nothing.
        r.check("the contract's twenty seconds", turns.LEASE_TTL_SECONDS == 20.0)
        world.clock.tick(19.0)
        r.check("renewed within twenty seconds the lease lives on", turns.state(lease)["phase"] == turns.HELD)
        world.clock.tick(19.0)
        world.steps()
        r.check("and a renewal gives it twenty more: live thirty-eight seconds after it was asked for",
                turns.gate_closed() is True and turns.state(lease)["phase"] == turns.HELD)
        world.clock.tick(20.5)
        r.check("unrenewed past twenty seconds the gate is open", turns.gate_closed() is False)
        record = turns.state(lease)
        r.check("the lease has expired and says why", record["phase"] == turns.EXPIRED
                and record["reason"] == turns.TEXT_EXPIRED, str(record))
        r.check("a state call on an expired lease does not bring it back", turns.state(lease)["phase"] == turns.EXPIRED)
        world.steps()
        r.check("and the executor tells the bridge to resume", world.child.resumes == [{"lease": lease}], str(world.child.resumes))
        again = turns.request(OWNER)
        r.check("the owner can ask again, for a new lease", again["lease"] != lease and again["phase"] in turns.LIVE,
                str(again))
    finally:
        world.close()


def flush_checks(r: Results) -> None:
    """Only while held; holding until done; held again with what happened."""
    world = World()
    try:
        world.runtime.ready()
        world.child.word = wire.HOLD_HOLDING
        world.child.task_running = True
        lease = turns.request(OWNER)["lease"]
        world.steps()
        record = turns.flush(lease, "soft")
        r.check("a flush before the card is held is refused in the reason, and nothing is asked",
                record["phase"] == turns.HOLDING and "not held yet" in record["reason"], str(record))
        world.steps()
        r.check("and the executor did not flush either", world.child.flushes == [])
        world.child.word, world.child.task_running = wire.HOLD_HELD, False
        world.steps()
        record = turns.flush(lease, "soft")
        r.check("a flush on a held card puts the lease back to holding until it is done",
                record["phase"] == turns.HOLDING and "soft" in record["reason"], str(record))
        world.steps()
        r.check("the executor asks the bridge for it", world.child.flushes == [{"lease": lease, "level": "soft"}],
                str(world.child.flushes))
        record = turns.state(lease)
        r.check("and held again means it is done, with what the card has free now",
                record["phase"] == turns.HELD and record["reason"].startswith("Soft flush done")
                and "31.0 GB of 32.0 GB" in record["reason"] and record["wangp"]["vram_free_bytes"] == 31 * GB, str(record))
        world.child.flush_refusal = errors.FLUSH_HARD_REFUSED
        turns.flush(lease, "hard")
        world.steps()
        record = turns.state(lease)
        r.check("a flush the bridge refuses is held again, saying why in words",
                record["phase"] == turns.HELD and record["reason"] == "Hard flush refused: " + errors.message(errors.FLUSH_HARD_REFUSED),
                str(record))
        record = turns.flush(lease, "medium")
        r.check("a flush that is neither soft nor hard asks nothing", "soft or hard" in record["reason"]
                and len(world.child.flushes) == 2, str(record))
    finally:
        world.close()


def wanted_checks(r: Results) -> None:
    """Wanted: a Clipboard job waiting for the card, or WanGP work waiting on the hold."""
    world = World()
    try:
        world.runtime.ready()
        lease = turns.request(OWNER)["lease"]
        world.steps()
        r.check("with nothing waiting, not wanted - the owner may stay warm", turns.state(lease)["wanted"] is False)
        world.child.waiter, world.child.queue_length = True, 1
        world.steps()
        record = turns.state(lease)
        r.check("WanGP work waiting on the hold makes it wanted", record["wanted"] is True
                and record["wangp"]["jobs_waiting"] == 0, str(record))
        world.child.waiter, world.child.queue_length = False, 0
        world.steps()
        world.job()
        _fresh(world)
        record = turns.state(lease)
        r.check("so does a Clipboard job waiting at the gate", record["wanted"] is True
                and record["wangp"]["jobs_waiting"] == 1, str(record))
    finally:
        world.close()

    world = World()
    try:
        demand_before = outbox.card_demand()
        outbox.submit({"request_id": "3" * 32, "prompt": "p"}, PAGE)
        listed = outbox._load()  # noqa: SLF001 - a job whose prompt is still to be written
        listed[-1]["enhance_requested"] = True
        listed[-1]["enhance"] = {"llm_id": "x" * 16, "state": "queued"}
        outbox._save(listed)  # noqa: SLF001
        r.check("a job whose prompt is still to be written does not want the card yet",
                outbox.card_demand()["waiting"] == demand_before["waiting"], str(outbox.card_demand()))
    finally:
        world.close()


def never_raises_checks(r: Results) -> None:
    world = World()
    try:
        answers = [turns.request(None), turns.request(12), turns.request(OWNER, purpose=object(), need_bytes="lots"),
                   turns.state(None), turns.state("zz"), turns.flush(None, None), turns.flush("g" * 32, 3),
                   turns.release(object()), turns.release("h" * 32, reason=None)]
        r.check("nothing a caller passes makes any of them raise, and every answer is a record",
                all(isinstance(item, dict) and tuple(item) == turns.LEASE_KEYS for item in answers))
        r.check("an id nobody knows is refused, not invented", turns.state("9" * 32)["phase"] == turns.REFUSED
                and turns.state("9" * 32)["reason"] == turns.TEXT_NO_LEASE)
    finally:
        world.close()


def page_path_checks(r: Results) -> None:
    """The path a page drives waits at the same gate."""
    world = World()
    try:
        world.runtime.ready()
        outbox.use_executor(outbox.EXECUTOR_BROWSER)
        job = outbox.submit({"request_id": "4" * 32, "prompt": "p"}, PAGE)
        lease = turns.request(OWNER)["lease"]
        answer = outbox.claim(PAGE)
        r.check("a page asking for its next job while the card is lent is told to wait",
                answer.get("wait") == outbox.WAIT_LENT_MS and answer.get("reason") == "lent" and "job" not in answer, str(answer))
        turns.release(lease)
        answer = outbox.claim(PAGE)
        r.check("and handed it once the card is given back", (answer.get("job") or {}).get("job_id") == job["job_id"],
                str(answer)[:160])
    finally:
        world.close()


def unholdable_checks(r: Results) -> None:
    """Held with WanGP stopped; WanGP then starts unholdable: not refused, not held."""
    world = World()
    try:
        lease = turns.request(OWNER)["lease"]
        r.check("held with WanGP stopped", turns.state(lease)["phase"] == turns.HELD)
        world.runtime.state = runtime.STARTING
        record = turns.state(lease)
        r.check("WanGP starting from its tab takes the lease back to holding, saying so",
                record["phase"] == turns.HOLDING and record["reason"] == turns.TEXT_STARTING, str(record))
        world.runtime.ready()
        world.child.can_hold = False
        world.steps()
        record = turns.state(lease)
        r.check("up with a bridge that cannot hold, a lease already held is not refused - the owner may be mid-render",
                record["phase"] == turns.HOLDING and record["bridge_hold"] == wire.HOLD_UNSUPPORTED
                and record["reason"] == turns.TEXT_OLD_BRIDGE_LATE, str(record))
        r.check("and the gate stays closed until it is given back", turns.gate_closed() is True)
    finally:
        world.close()

    world = World()
    try:
        world.runtime.ready()
        lease = turns.request(OWNER)["lease"]
        world.steps()
        r.check("held on one WanGP run", turns.state(lease)["phase"] == turns.HELD)
        world.runtime.ready(instance="child-two")
        r.check("a restart is a WanGP nobody asked: holding until the hold is sent again",
                turns.state(lease)["phase"] == turns.HOLDING)
        world.steps()
        r.check("and held once it has been", turns.state(lease)["phase"] == turns.HELD and len(world.child.holds) == 2)
    finally:
        world.close()


def client_checks(r: Results) -> None:
    """Forge's control client for the three operations."""
    world = World()
    try:
        answer = control.hold("5" * 32, 45.0, "Voice")
        r.check("hold sends the lease, the timer and the name and reads the answer",
                world.child.holds[-1] == {"lease": "5" * 32, "ttl_s": 45.0, "label": "Voice"} and answer["hold"] == wire.HOLD_HELD)
        r.check("resume sends the lease", control.resume("5" * 32)["resumed"] is True and world.child.resumes[-1] == {"lease": "5" * 32})
        world.child.flush_refusal = errors.HOLD_TASK_RUNNING
        refused = ""
        try:
            control.flush("5" * 32, "soft")
        except IntegrationError as error:
            refused = error.code
        r.check("a refused flush is an IntegrationError with the bridge's code", refused == errors.HOLD_TASK_RUNNING, refused)
        r.check("with nothing heard from WanGP, whether it can hold is not known", control.can_hold() is None)
        world.child.can_hold = False
        r.check("an older bridge's hello says it cannot", control.can_hold(control.hello()) is False)
        r.check("and every code the bridge can refuse with has a sentence",
                all(code in errors.MESSAGES for code in (errors.HOLD_NOT_HELD, errors.HOLD_TASK_RUNNING,
                                                         errors.HOLD_UNSUPPORTED, errors.FLUSH_HARD_REFUSED)))
        r.check("spelled the same in the shared vocabulary",
                (wire.HOLD_CODE_NOT_HELD, wire.HOLD_CODE_TASK_RUNNING, wire.HOLD_CODE_UNSUPPORTED, wire.HOLD_CODE_FLUSH_REFUSED)
                == (errors.HOLD_NOT_HELD, errors.HOLD_TASK_RUNNING, errors.HOLD_UNSUPPORTED, errors.FLUSH_HARD_REFUSED))
    finally:
        world.close()


def report_checks(r: Results) -> None:
    world = World()
    try:
        found = turns.report()
        r.check("without a lease, report says so, with the card and WanGP's state",
                found["lease"] == "" and found["phase"] == "" and found["card_uuid"] == GPU_UUID
                and found["running"] is False, str(found))
        r.check("and whether the installed bridge could hold a WanGP started now", found["can_hold"] is True, str(found))
        world.runtime.ready()
        control.hello()
        lease = turns.request(OWNER)["lease"]
        world.steps()
        found = turns.report()
        r.check("with one, it names the lease, its owner and its phase",
                found["lease"] == lease and found["owner"] == OWNER and found["phase"] == turns.HELD
                and found["bridge_hold"] == wire.HOLD_HELD and found["can_hold"] is True, str(found))
    finally:
        world.close()


def loop_checks(r: Results) -> None:
    """The executor thread stays up while a lease is owed anything, and not after."""
    world = World()
    try:
        world.runtime.ready()
        r.check("with no lease the executor has nothing of the lease's to do", executor._lease_owes() is False)  # noqa: SLF001
        lease = turns.request(OWNER)["lease"]
        r.check("with one it does, even with no job", executor._lease_owes() is True  # noqa: SLF001
                and executor._lease_interval() == turns.DRIVE_SECONDS)  # noqa: SLF001
        r.check("and a pass with only a lease to drive does no job work, so the loop paces itself",
                executor.step() is False and len(world.child.holds) == 1)
        turns.release(lease)
        r.check("released, it still owes the resume", executor._lease_owes() is True)  # noqa: SLF001
        world.steps()
        r.check("and owes nothing once that is sent", executor._lease_owes() is False and len(world.child.resumes) == 1)  # noqa: SLF001
    finally:
        world.close()


def journal_checks(r: Results) -> None:
    world = World()
    try:
        world.runtime.ready()
        lease = turns.request(OWNER, purpose="Speech engine", need_bytes=19 * GB)["lease"]
        world.steps()
        turns.state(lease)
        turns.release(lease)
        world.steps()
        text = world.journal()
        r.check("the journal says who asked, and how much",
                f"asked for by {OWNER} (Speech engine, asks for 19.0 GB)" in text, text[-400:])
        r.check("that it was held and given back, and that the bridge was told",
                "): held" in text and "released" in text and "told to resume" in text, text[-400:])
        r.check("naming the lease by its first eight characters only", lease not in text and lease[:8] in text)
    finally:
        world.close()


def run() -> Results:
    r = Results("wangp turns")
    shape_checks(r)
    refusal_checks(r)
    stopped_checks(r)
    ready_checks(r)
    pending_checks(r)
    busy_checks(r)
    gate_checks(r)
    expiry_checks(r)
    flush_checks(r)
    wanted_checks(r)
    never_raises_checks(r)
    page_path_checks(r)
    unholdable_checks(r)
    client_checks(r)
    report_checks(r)
    loop_checks(r)
    journal_checks(r)
    return r


if __name__ == "__main__":
    raise SystemExit(0 if run().report() else 1)
