"""Holding WanGP between tasks: the bridge's hold, resume and flush (bridge 1.12.0).

WHAT THIS HOLDS THE LINE ON IS WHAT WANGP DOES, NOT WHAT THE BRIDGE SAYS.

A hold that answers ``held`` while WanGP goes on to start its next task is the
failure this whole feature exists to prevent - another extension would put
eighteen gigabytes on the card under it. So the stand-in here is a working
model of the parts of WanGP a hold leans on, not a record of calls: a worker
thread that checks the pause flag at the top of every iteration and takes the
next task only when it is clear, a run that waits in
``acquire_main_GPU_ressources`` while somebody else holds WanGP's GPU lock, a
``try_acquire_GPU_ressources`` that refuses exactly when WanGP's does, and an
edit that clears the flag behind the hold's back. The checks then read what
the model did - which tasks started, and when - beside what the hold said.

The unload side is stood in for the same way: a video model's offload object
whose ``unload_all`` keeps its weights and whose release drops them, a prompt
enhancer, a GPU resident with a release callback, an extension registry, the
model unload guard (and whether ``release_model`` really ran inside it), and
the preload policy that makes a hard flush a hang.

Wan2GP is not vendored and cannot be imported here; these are installed into
``sys.modules`` under WanGP's own module names for the length of each check,
because that is where the bridge looks for them and the only place it looks.
"""

from harness import Results, ROOT, setup_path

setup_path()

import contextlib  # noqa: E402
import json  # noqa: E402
import socket  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import types  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

BRIDGE_DIR = ROOT / "wan2gp_bridge" / "wan2gp-minipaint-bridge"
LOCKS = "shared.utils.process_locks"
UNLOAD = "shared.utils.model_unload"
REGISTRY = "shared.utils.offload_registry"
OURS = "e" * 32
LEASE = "1" * 32
OTHER_LEASE = "2" * 32
GB = 1024 ** 3


def _modules():
    added = str(BRIDGE_DIR) not in sys.path
    if added:
        sys.path.insert(0, str(BRIDGE_DIR))
    try:
        import compatibility
        import control
        import hold
        import protocol
    finally:
        if added and str(BRIDGE_DIR) in sys.path:
            sys.path.remove(str(BRIDGE_DIR))
    return compatibility, control, hold, protocol


# ------------------------------------------------------- WanGP's GPU lock --


def make_locks():
    """``shared/utils/process_locks.py``, the four functions a hold meets.

    Their decisions are WanGP's to the letter - the idle claim refuses while
    anything but a stale ``process:main`` holds the lock, the release puts
    back what the hierarchy remembered, a resident is released through its
    own callback - and the lock itself is real, because the worker below
    waits on it for real.
    """
    module = types.ModuleType(LOCKS)
    module.gen_lock = threading.Lock()
    module.calls = []

    def _gen(state):
        return state.get("gen")

    def try_acquire_GPU_ressources(state, process_id, process_name):
        gen = _gen(state)
        module.calls.append(("try_acquire", process_id, process_name))
        actions = []
        with module.gen_lock:
            status = gen.get("process_status")
            if status is not None and not (status == "process:main" and not gen.get("main_process_running", False)):
                return False
            residents = gen.setdefault("gpu_residents", {})
            for resident, info in list(residents.items()):
                if resident == process_id:
                    residents.pop(resident)
                    continue
                if info.get("force_release_on_acquire") and callable(info.get("release_vram_callback")):
                    actions.append(info["release_vram_callback"])
                    residents.pop(resident)
            gen.setdefault("process_hierarchy", {})[process_id] = None
            gen.setdefault("process_names", {})[process_id] = process_name
            gen["process_status"] = "process:" + process_id
        for action in actions:
            action()
        return True

    def release_GPU_ressources(state, process_id, keep_resident=False, process_name=None,
                               release_vram_callback=None, force_release_on_acquire=True):
        gen = _gen(state)
        module.calls.append(("release", process_id))
        with module.gen_lock:
            restore = gen.get("process_hierarchy", {}).pop(process_id, None)
            if restore == "process:main" and not gen.get("main_process_running", False):
                restore = None
            gen["process_status"] = restore
            gen.get("process_names", {}).pop(process_id, None)

    def force_release_GPU_resident(state, process_id):
        gen = _gen(state)
        callback = None
        with module.gen_lock:
            info = gen.setdefault("gpu_residents", {}).pop(process_id, None)
            if info is not None:
                callback = info.get("release_vram_callback")
        if callable(callback):
            callback()

    def acquire_main_GPU_ressources(state):
        gen = _gen(state)
        while True:
            with module.gen_lock:
                status = gen.get("process_status")
                if status is None or status == "process:main":
                    gen["process_status"] = "process:main"
                    return
                holder = status.split(":", 1)[1]
                name = gen.get("process_names", {}).get(holder, holder)
                gen["status"] = f"Media generation is waiting for {name} to release GPU resources..."
            time.sleep(0.005)

    module.try_acquire_GPU_ressources = try_acquire_GPU_ressources
    module.release_GPU_ressources = release_GPU_ressources
    module.force_release_GPU_resident = force_release_GPU_resident
    module.acquire_main_GPU_ressources = acquire_main_GPU_ressources
    return module


# ------------------------------------------------------ WanGP's generation --


class WanGP:
    """The one generation service, with ``_process_tasks`` inside it.

    ``start_generation`` starts a worker only when none exists and the queue
    has something in it, and hands back the running one otherwise; the run
    marks itself (``main_process_running``), waits for WanGP's GPU lock,
    raises ``in_progress``, and then loops: the pause flag first - sleeping,
    and saying so in the status line, while it is set - then the head of the
    queue, marked as the running task while it runs. ``marks`` False is the
    pinned revision, which has no marker.
    """

    def __init__(self, locks, marks=True):
        self.gen = {"queue": [], "process_status": None}
        self._state = {"gen": self.gen}
        self._queue_worker = None
        self._mutation_lock = threading.RLock()
        self.locks = locks
        self.marks = marks
        self.started = []
        self.gates = {}
        self.poll = 0.005

    @property
    def generation_running(self):
        return self._queue_worker is not None

    def start_generation(self):
        with self._mutation_lock:
            if self._queue_worker is None and self.gen["queue"]:
                self._queue_worker = threading.Thread(target=self._run, daemon=True)
                self._queue_worker.start()
            return self._queue_worker

    def command(self, name, payload=None):
        return self.start_generation()

    def add(self, client, gate=False):
        """A task in WanGP's queue; ``gate`` keeps it running until let go."""
        self.gen["queue"].append({"id": len(self.started) + len(self.gen["queue"]) + 1,
                                  "params": {"client_id": client, "model_type": "t2v"}})
        if gate:
            self.gates[client] = threading.Event()

    def finish(self, client):
        self.gates[client].set()

    def _run(self):
        gen = self.gen
        try:
            gen["main_process_running"] = True
            self.locks.acquire_main_GPU_ressources({"gen": gen})
            gen["in_progress"] = True
            while gen["queue"]:
                paused = False
                while gen.get("queue_paused_for_edit", False):
                    if not paused:
                        gen["status"] = "Queue paused for editing..."
                        paused = True
                    time.sleep(self.poll)
                if paused:
                    gen["status"] = "Resuming queue processing..."
                task = gen["queue"][0] if gen["queue"] else None
                if task is None:
                    break
                client = task["params"]["client_id"]
                if self.marks:
                    gen["api_active_queue_task"] = task
                self.started.append(client)
                gen["status"] = f"Generating {client[:8]}"
                gate = self.gates.get(client)
                if gate is not None:
                    gate.wait(10)
                gen.pop("api_active_queue_task", None)
                gen["queue"].pop(0)
        finally:
            gen.pop("in_progress", None)
            gen.pop("main_process_running", None)
            with self.locks.gen_lock:
                if gen.get("process_status") == "process:main":
                    gen["process_status"] = None
            with self._mutation_lock:
                self._queue_worker = None


def wait_for(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return bool(condition())


# ---------------------------------------------------------- the unloaders --


class Offload:
    """An mmgp offload object: ``unload_all`` moves weights to RAM, ``release`` drops them."""

    def __init__(self):
        self.unloads = 0
        self.released = 0

    def unload_all(self):
        self.unloads += 1

    def release(self):
        self.released += 1


def make_wgp(policy=()):
    guard_module = types.ModuleType(UNLOAD)
    guard_module.inside = False
    guard_module.entered = 0

    @contextlib.contextmanager
    def model_unload_guard():
        guard_module.entered += 1
        guard_module.inside = True
        try:
            yield
        finally:
            guard_module.inside = False

    guard_module.model_unload_guard = model_unload_guard

    wgp = types.ModuleType("wgp")
    wgp.offloadobj = Offload()
    wgp.enhancer_offloadobj = Offload()
    wgp.preload_model_policy = list(policy)
    wgp.releases = []

    def release_model():
        wgp.releases.append(guard_module.inside)
        if wgp.offloadobj is not None:
            wgp.offloadobj.release()
        wgp.offloadobj = None

    wgp.release_model = release_model

    registry = types.ModuleType(REGISTRY)
    registry.unloaded = 0

    def unload_vram(names=None):
        registry.unloaded += 1
        return ["MMAudio"]

    registry.unload_vram = unload_vram
    return wgp, guard_module, registry


@contextlib.contextmanager
def installed(**modules):
    """Put stand-ins where the bridge looks - ``sys.modules`` - and put back what was there."""
    names = {"locks": LOCKS, "unload": UNLOAD, "registry": REGISTRY, "wgp": "wgp", "torch": "torch"}
    saved = {}
    try:
        for key, module in modules.items():
            name = names[key]
            saved[name] = sys.modules.get(name)
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        yield
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class FakeHost:
    def __init__(self, globals_map):
        self._globals = dict(globals_map)

    def request_component(self, elem_id):
        return None

    def request_global(self, name):
        return self._globals.get(name)

    def get_global(self, name):
        return self._globals.get(name)


def setup(marks=True, service=True):
    compatibility, _control, hold, protocol = _modules()
    locks = make_locks()
    wangp = WanGP(locks, marks=marks)
    globals_map = {"get_gen_info": lambda state: state["gen"]}
    if service:
        globals_map["service_for"] = lambda *_a: wangp
    compat = compatibility.Compatibility(host=compatibility.Host(FakeHost(globals_map)), environ={})
    compat.declare_globals()
    clock = {"now": 1000.0}
    notes = []
    holder = hold.Holder(compat, note=notes.append, clock=lambda: clock["now"], threaded=False)
    return compat, holder, wangp, locks, clock, notes, protocol


# --------------------------------------------------------------- the checks --


def idle_checks(r: Results) -> None:
    """No worker: the flag and the claim, and a Generate that waits for both."""
    compat, holder, wangp, locks, _clock, notes, protocol = setup()
    compatibility = _modules()[0]
    with installed(locks=locks):
        answer = holder.hold(LEASE, 45.0, "Speech engine")
        r.check("a hold on an idle WanGP is held at once", answer["hold"] == protocol.HOLD_HELD, str(answer))
        r.check("it set WanGP's own between-task pause flag", wangp.gen.get("queue_paused_for_edit") is True)
        r.check("and took the idle claim on WanGP's GPU lock, under the name Forge sent",
                answer["claim"] == protocol.CLAIM_TAKEN
                and wangp.gen["process_status"] == "process:" + compatibility.HOLD_PROCESS_ID
                and wangp.gen["process_names"][compatibility.HOLD_PROCESS_ID] == "Speech engine", str(wangp.gen))
        r.check("nothing runs, nothing waits", answer["task_running"] is False and answer["waiter"] is False
                and answer["worker"] is False, str(answer))
        r.check("and the answer says how long the hold lasts unrenewed", answer["expires_in_s"] == 45.0, str(answer))

        # Somebody presses Generate in the WanGP tab.
        wangp.add("page-client-1")
        wangp.start_generation()
        time.sleep(0.15)
        r.check("a run started while held waits for the claim, and starts no task",
                wangp.started == [] and wangp.generation_running, str(wangp.started))
        r.check("and WanGP tells its user who it is waiting for",
                "waiting for Speech engine to release GPU resources" in str(wangp.gen.get("status")), str(wangp.gen.get("status")))
        holder.tick()
        answer = holder.status()
        r.check("the hold is still held, with that run reported as waiting on it",
                answer["hold"] == protocol.HOLD_HELD and answer["waiter"] is True and answer["task_running"] is False,
                str(answer))

        # Resume: the claim first, then the flag - the reverse of the hold.
        order = []
        real_release = compat.release_gpu
        real_flag = compat.set_pause_flag

        def watched_release(gen):
            order.append("claim")
            return real_release(gen)

        def watched_flag(gen, value):
            if not value:
                order.append("flag")
            return real_flag(gen, value)

        compat.release_gpu = watched_release
        compat.set_pause_flag = watched_flag
        answer = holder.resume(LEASE)
        r.check("resume undoes the hold in reverse order: the claim, then the flag", order == ["claim", "flag"], str(order))
        r.check("and says so", answer["resumed"] is True and answer["hold"] == protocol.HOLD_NONE, str(answer))
        r.check("the run that was waiting now generates", wait_for(lambda: wangp.started == ["page-client-1"]), str(wangp.started))
        r.check("and WanGP's GPU lock no longer names the hold",
                wait_for(lambda: not wangp.generation_running) and wangp.gen.get("process_status") is None,
                str(wangp.gen.get("process_status")))
        again = holder.resume(LEASE)
        r.check("resuming twice is resuming once", again["resumed"] is False and again["hold"] == protocol.HOLD_NONE, str(again))


def busy_checks(r: Results) -> None:
    """A task running: holding until it finishes, then held - and the next never starts."""
    _compat, holder, wangp, locks, _clock, _notes, protocol = setup()
    with installed(locks=locks):
        wangp.add("page-client-1", gate=True)
        wangp.add("page-client-2")
        wangp.start_generation()
        wait_for(lambda: wangp.started == ["page-client-1"])
        answer = holder.hold(LEASE, 45.0, "")
        r.check("a hold while a task runs says holding", answer["hold"] == protocol.HOLD_HOLDING, str(answer))
        r.check("and does not cut the task off: it is still the one running",
                answer["task_running"] is True and wangp.started == ["page-client-1"], str(answer))
        r.check("and never asks for the claim while a worker exists - it could only suspend the run",
                not any(call[0] == "try_acquire" for call in locks.calls)
                and wangp.gen.get("process_status") == "process:main", str(locks.calls))
        r.check("a page's own client id is not reported, only Forge's own ids are",
                answer["active_client_id"] == "", str(answer))
        wangp.finish("page-client-1")
        r.check("the task finishes and the worker stops at the flag instead of taking the next",
                wait_for(lambda: wangp.gen.get("status") == "Queue paused for editing...")
                and wangp.started == ["page-client-1"], str(wangp.started))
        time.sleep(0.05)
        holder.tick()
        answer = holder.status()
        r.check("then the hold is held", answer["hold"] == protocol.HOLD_HELD and answer["task_running"] is False, str(answer))
        r.check("with WanGP's own queued task reported as waiting", answer["waiter"] is True and answer["queue_length"] == 1,
                str(answer))
        r.check("and the next task has still not started", wangp.started == ["page-client-1"], str(wangp.started))
        holder.resume(LEASE)
        r.check("resume lets the next task run", wait_for(lambda: wangp.started == ["page-client-1", "page-client-2"]),
                str(wangp.started))


def ours_checks(r: Results) -> None:
    """The running task's client id, when it is one of Forge's own execution ids."""
    _compat, holder, wangp, locks, _clock, _notes, protocol = setup()
    with installed(locks=locks):
        wangp.add(OURS, gate=True)
        wangp.start_generation()
        wait_for(lambda: wangp.started == [OURS])
        answer = holder.hold(LEASE, 45.0, "")
        r.check("a task Forge submitted is named by its execution id",
                answer["task_running"] is True and answer["active_client_id"] == OURS, str(answer))
        wangp.finish(OURS)
        wait_for(lambda: not wangp.generation_running)
        holder.tick()
        answer = holder.status()
        r.check("and when it ends with nothing queued behind it, the worker goes, the claim is taken and it is held",
                answer["hold"] == protocol.HOLD_HELD and answer["claim"] == protocol.CLAIM_TAKEN
                and answer["waiter"] is False, str(answer))
        # A worker still finishing its run - unloading, its queue empty - is
        # not work waiting for the card, and must not make the lease wanted.
        wangp._queue_worker = threading.Thread(target=lambda: None)
        answer = holder.status()
        r.check("a worker ending its run with an empty queue is not a waiter",
                answer["worker"] is True and answer["task_running"] is False and answer["waiter"] is False, str(answer))
        wangp._queue_worker = None
        holder.resume(LEASE)


def reassert_checks(r: Results) -> None:
    """WanGP's edit clears the flag; the hold sets it again, and knows whose it is."""
    compat, holder, wangp, locks, _clock, notes, protocol = setup()
    with installed(locks=locks):
        holder.hold(LEASE, 45.0, "")
        wangp.gen["queue_paused_for_edit"] = False  # an edit was saved
        holder.tick()
        r.check("a flag WanGP's edit cleared is set again on the next look",
                wangp.gen.get("queue_paused_for_edit") is True)
        r.check("and the log says so", any("cleared its pause flag" in note for note in notes), str(notes[-3:]))
        holder.resume(LEASE)
        r.check("a flag the hold set is cleared on resume", wangp.gen.get("queue_paused_for_edit") is False)

    # An edit already in progress when the hold began keeps its flag.
    compat, holder, wangp, locks, _clock, _notes, protocol = setup()
    with installed(locks=locks):
        wangp.gen["queue_paused_for_edit"] = True  # somebody is editing a queued task
        holder.hold(LEASE, 45.0, "")
        holder.resume(LEASE)
        r.check("a flag an edit had set before the hold is left set - the edit still owns it",
                wangp.gen.get("queue_paused_for_edit") is True)

    # ...unless the edit ended during the hold, in which case the flag is the hold's.
    compat, holder, wangp, locks, _clock, _notes, protocol = setup()
    with installed(locks=locks):
        wangp.gen["queue_paused_for_edit"] = True
        holder.hold(LEASE, 45.0, "")
        wangp.gen["queue_paused_for_edit"] = False  # the edit ended
        holder.tick()
        holder.resume(LEASE)
        r.check("once the edit has ended under the hold, resume clears the flag it set again",
                wangp.gen.get("queue_paused_for_edit") is False)


def ttl_checks(r: Results) -> None:
    """Not renewed within ttl_s: WanGP resumes by itself."""
    compatibility = _modules()[0]
    _compat, holder, wangp, locks, clock, notes, protocol = setup()
    with installed(locks=locks):
        holder.hold(LEASE, 5.0, "")
        clock["now"] += 4.0
        holder.hold(LEASE, 5.0, "")  # renewed
        clock["now"] += 4.0
        holder.tick()
        r.check("a renewed hold outlives its first timer", holder.status()["hold"] == protocol.HOLD_HELD)
        clock["now"] += 1.5
        holder.tick()
        r.check("an unrenewed one lets go by itself: the claim and the flag both",
                holder.status()["hold"] == protocol.HOLD_NONE and wangp.gen.get("queue_paused_for_edit") is False
                and wangp.gen.get("process_status") is None, str(wangp.gen))
        r.check("and says why", any("not renewed within 5s" in note for note in notes), str(notes[-2:]))
        r.check("the timer is bounded below, so a zero cannot make a hold that never holds",
                protocol.normalize_hold_request({"lease": LEASE, "ttl_s": 0})[0]["ttl_s"] == protocol.HOLD_TTL_MIN_SECONDS)
        r.check("the claim is WanGP's lock under this bridge's own id",
                compatibility.HOLD_PROCESS_ID == "minipaint_card_lease")


def rekey_checks(r: Results) -> None:
    """A new lease takes the hold over without letting WanGP go in between."""
    _compat, holder, wangp, locks, _clock, notes, protocol = setup()
    with installed(locks=locks):
        holder.hold(LEASE, 45.0, "")
        releases = sum(1 for call in locks.calls if call[0] == "release")
        answer = holder.hold(OTHER_LEASE, 45.0, "")
        r.check("a hold for another lease takes over", answer["lease"] == OTHER_LEASE and answer["hold"] == protocol.HOLD_HELD,
                str(answer))
        r.check("without releasing the claim or the flag in between",
                sum(1 for call in locks.calls if call[0] == "release") == releases
                and wangp.gen.get("queue_paused_for_edit") is True)
        r.check("and the old lease can no longer resume it", holder.resume(LEASE)["resumed"] is False
                and holder.status()["hold"] == protocol.HOLD_HELD)
        holder.resume(OTHER_LEASE)


def stale_checks(r: Results) -> None:
    """A run flag a crash left set refuses the claim for ever: the pause flag holds alone."""
    _compat, holder, wangp, locks, _clock, notes, protocol = setup()
    with installed(locks=locks):
        wangp.gen["process_status"] = "process:main"
        wangp.gen["main_process_running"] = True  # left behind by an exception, no worker
        answer = holder.hold(LEASE, 45.0, "")
        r.check("the claim gives way to the flag alone, and says why",
                answer["claim"] == protocol.CLAIM_FLAG_ONLY and any("a crash left set" in note for note in notes),
                str(answer))
        r.check("and the hold is held, not stuck asking", answer["hold"] == protocol.HOLD_HELD, str(answer))
        wangp.add("page-client-1")
        wangp.start_generation()
        r.check("a run started now passes WanGP's lock and stops at the flag, starting nothing",
                wait_for(lambda: wangp.gen.get("status") == "Queue paused for editing...") and wangp.started == [],
                str(wangp.started))
        holder.tick()
        r.check("and is reported as waiting", holder.status()["waiter"] is True, str(holder.status()))
        holder.resume(LEASE)
        r.check("resume lets it run", wait_for(lambda: wangp.started == ["page-client-1"]), str(wangp.started))


def busy_other_checks(r: Results) -> None:
    """Another of WanGP's GPU processes has the card: holding, and who, until it is free."""
    _compat, holder, wangp, locks, clock, _notes, protocol = setup()
    with installed(locks=locks):
        wangp.gen["process_status"] = "process:deepy"
        wangp.gen["process_names"] = {"deepy": "Deepy"}
        answer = holder.hold(LEASE, 45.0, "")
        r.check("the hold says holding while another of WanGP's processes has the GPU",
                answer["hold"] == protocol.HOLD_HOLDING and answer["claim"] == protocol.CLAIM_BUSY, str(answer))
        r.check("and names it", answer["busy_with"] == "Deepy", str(answer))
        wangp.gen["process_status"] = None  # it let go
        clock["now"] += 0.1
        holder.tick()
        r.check("a refused claim is not asked again before its retry", holder.status()["claim"] == protocol.CLAIM_BUSY)
        clock["now"] += 1.0
        holder.tick()
        r.check("and once the GPU is free the claim is taken and the hold is held",
                holder.status()["hold"] == protocol.HOLD_HELD and holder.status()["claim"] == protocol.CLAIM_TAKEN,
                str(holder.status()))
        holder.resume(LEASE)


def flush_checks(r: Results) -> None:
    """Soft keeps the weights in RAM; hard releases the model inside WanGP's guard."""
    compatibility = _modules()[0]
    _compat, holder, wangp, locks, _clock, notes, protocol = setup()
    wgp, guard, registry = make_wgp()
    main_model, enhancer = wgp.offloadobj, wgp.enhancer_offloadobj
    released = []
    wangp.gen["gpu_residents"] = {"deepy": {"process_name": "Deepy", "force_release_on_acquire": False,
                                            "release_vram_callback": lambda: released.append("deepy")}}
    with installed(locks=locks, wgp=wgp, unload=guard, registry=registry):
        holder.hold(LEASE, 45.0, "")
        answer = holder.flush(LEASE, protocol.FLUSH_SOFT)
        r.check("a soft flush moves the video model and the enhancer off the card",
                main_model.unloads == 1 and enhancer.unloads == 1, f"{main_model.unloads} {enhancer.unloads}")
        r.check("releases every GPU resident through its own callback, and unloads the extensions",
                released == ["deepy"] and registry.unloaded == 1, f"{released} {registry.unloaded}")
        r.check("and keeps the weights: nothing is released, nothing reloads from disk",
                main_model.released == 0 and wgp.offloadobj is main_model and wgp.releases == [], str(wgp.releases))
        r.check("the answer says what moved, and the hold is still held",
                answer["flushed"] == protocol.FLUSH_SOFT and "model" in answer["parts"] and "residents" in answer["parts"]
                and answer["hold"] == protocol.HOLD_HELD, str(answer))
        answer = holder.flush(LEASE, protocol.FLUSH_HARD)
        r.check("a hard flush releases the model, so the next task reloads it",
                wgp.releases and wgp.offloadobj is None and main_model.released == 1, str(wgp.releases))
        r.check("and releases it inside WanGP's model unload guard, as its own Unload does",
                wgp.releases == [True] and guard.entered == 1, str(wgp.releases))
        r.check("and says so", answer["flushed"] == protocol.FLUSH_HARD and "released" in answer["parts"], str(answer))

        refused = ""
        try:
            holder.flush(OTHER_LEASE, protocol.FLUSH_SOFT)
        except _modules()[2].HoldError as error:
            refused = error.code
        r.check("a flush for a lease WanGP is not held for is refused", refused == compatibility.HOLD_NOT_HELD, refused)
        holder.resume(LEASE)
        refused = ""
        try:
            holder.flush(LEASE, protocol.FLUSH_SOFT)
        except _modules()[2].HoldError as error:
            refused = error.code
        r.check("and so is one after the hold was let go", refused == compatibility.HOLD_NOT_HELD, refused)


def flush_refusal_checks(r: Results) -> None:
    """Refused while a task runs; hard refused under a paused run, and where WanGP would hang."""
    compatibility, _control, hold, protocol = _modules()
    _compat, holder, wangp, locks, _clock, _notes, _protocol = setup()
    wgp, guard, registry = make_wgp()
    with installed(locks=locks, wgp=wgp, unload=guard, registry=registry):
        wangp.add("page-client-1", gate=True)
        wangp.add("page-client-2")
        wangp.start_generation()
        wait_for(lambda: wangp.started == ["page-client-1"])
        holder.hold(LEASE, 45.0, "")
        codes = []
        for level in protocol.FLUSH_LEVELS:
            try:
                holder.flush(LEASE, level)
                codes.append("")
            except hold.HoldError as error:
                codes.append(error.code)
        r.check("both flushes are refused while a task runs, and nothing was touched",
                codes == [compatibility.HOLD_TASK_RUNNING] * 2 and wgp.offloadobj.unloads == 0, str(codes))
        wangp.finish("page-client-1")
        wait_for(lambda: wangp.gen.get("status") == "Queue paused for editing...")
        refused = ""
        try:
            holder.flush(LEASE, protocol.FLUSH_HARD)
        except hold.HoldError as error:
            refused = error.code
        r.check("a hard flush is refused while a run is paused between tasks, as WanGP's own Unload is",
                refused == compatibility.HOLD_TASK_RUNNING and wgp.releases == [], refused)
        answer = holder.flush(LEASE, protocol.FLUSH_SOFT)
        r.check("while a soft one is done, the run left paused", answer["flushed"] == protocol.FLUSH_SOFT
                and wangp.started == ["page-client-1"], str(answer))
        holder.resume(LEASE)
        wait_for(lambda: not wangp.generation_running)

    # WanGP set to load its model at start: generate_media would wait for ever
    # for a model a hard flush released.
    _compat, holder, wangp, locks, _clock, _notes, _protocol = setup()
    wgp, guard, registry = make_wgp(policy=["P"])
    with installed(locks=locks, wgp=wgp, unload=guard, registry=registry):
        holder.hold(LEASE, 45.0, "")
        refused = ""
        try:
            holder.flush(LEASE, protocol.FLUSH_HARD)
        except hold.HoldError as error:
            refused = error.code
        r.check("a hard flush is refused where WanGP preloads its model at start and would never reload it",
                refused == compatibility.FLUSH_HARD_REFUSED and wgp.releases == [], refused)
        holder.resume(LEASE)
    _compat, holder, wangp, locks, _clock, _notes, _protocol = setup()
    wgp, guard, registry = make_wgp(policy=["P", "U"])
    with installed(locks=locks, wgp=wgp, unload=guard, registry=registry):
        holder.hold(LEASE, 45.0, "")
        answer = holder.flush(LEASE, protocol.FLUSH_HARD)
        r.check("but allowed where it unloads after every run as well, which reloads on demand",
                answer["flushed"] == protocol.FLUSH_HARD and wgp.releases == [True], str(answer))
        holder.resume(LEASE)
    _compat, holder, wangp, locks, _clock, _notes, _protocol = setup()
    wgp, guard, registry = make_wgp()
    with installed(locks=locks, wgp=wgp, unload=None, registry=registry):
        holder.hold(LEASE, 45.0, "")
        refused = ""
        try:
            holder.flush(LEASE, protocol.FLUSH_HARD)
        except hold.HoldError as error:
            refused = error.code
        r.check("and refused as unsupported on a build without the guard a release must run inside",
                refused == compatibility.HOLD_UNSUPPORTED and wgp.releases == [], refused)
        holder.resume(LEASE)


def unsupported_checks(r: Results) -> None:
    """A build without an internal: said, never guessed around."""
    _compat, holder, wangp, locks, _clock, notes, protocol = setup(service=False)
    with installed(locks=locks):
        answer = holder.hold(LEASE, 45.0, "")
        r.check("with WanGP's shared record out of reach the hold is unsupported, not held",
                answer["hold"] == protocol.HOLD_UNSUPPORTED and answer["detail"], str(answer))
        r.check("and nothing was written anywhere", "queue_paused_for_edit" not in wangp.gen, str(wangp.gen))
        holder.resume(LEASE)

    _compat, holder, wangp, _locks, _clock, notes, protocol = setup()
    with installed(locks=None):
        answer = holder.hold(LEASE, 45.0, "")
        r.check("a build with no GPU lock holds with the flag alone, and says it has no claim",
                answer["hold"] == protocol.HOLD_HELD and answer["claim"] == protocol.CLAIM_ABSENT
                and wangp.gen.get("queue_paused_for_edit") is True, str(answer))
        holder.resume(LEASE)


def pinned_revision_checks(r: Results) -> None:
    """The pinned execution revision has no marker of the running task."""
    _compat, holder, wangp, locks, _clock, _notes, protocol = setup(marks=False)
    with installed(locks=locks):
        wangp.add("page-client-1", gate=True)
        wangp.add("page-client-2")
        wangp.start_generation()
        wait_for(lambda: wangp.started == ["page-client-1"])
        answer = holder.hold(LEASE, 45.0, "")
        r.check("without the marker a running task is read from the running loop, and holds",
                answer["hold"] == protocol.HOLD_HOLDING and answer["task_running"] is True, str(answer))
        wangp.finish("page-client-1")
        wait_for(lambda: wangp.gen.get("status") == "Queue paused for editing...")
        holder.tick()
        answer = holder.status()
        r.check("and the worker's own word that it paused is what makes it held",
                answer["hold"] == protocol.HOLD_HELD and wangp.started == ["page-client-1"], str(answer))
        holder.resume(LEASE)
        wait_for(lambda: not wangp.generation_running)

    # A build that has shown the marker once is trusted without it after.
    compat, holder, wangp, locks, _clock, _notes, protocol = setup(marks=True)
    wangp.gen["api_active_queue_task"] = {"params": {"client_id": OURS}}
    r.check("the marker is read as a running task", compat.task_on_card(wangp.gen, True) == (True, OURS))
    wangp.gen.pop("api_active_queue_task")
    wangp.gen["in_progress"] = True
    wangp.gen["queue"] = [{"params": {"client_id": "x"}}]
    r.check("and once seen, its absence under a live worker means between tasks",
            compat.task_on_card(wangp.gen, True) == (False, ""))


def vram_checks(r: Results) -> None:
    """The card's memory, only once CUDA is up in WanGP's process."""
    compatibility = _modules()[0]
    cuda = types.SimpleNamespace(initialized=False, emptied=0)
    cuda.is_available = lambda: True
    cuda.is_initialized = lambda: cuda.initialized
    cuda.mem_get_info = lambda: (7 * GB, 32 * GB)

    def empty_cache():
        cuda.emptied += 1

    cuda.empty_cache = empty_cache
    torch = types.ModuleType("torch")
    torch.cuda = cuda
    with installed(torch=torch):
        r.check("before CUDA is initialised nothing is asked, so no context is made on a lent card",
                compatibility.Compatibility.vram() == (None, None))
        cuda.initialized = True
        r.check("after, the driver's free and total bytes", compatibility.Compatibility.vram() == (7 * GB, 32 * GB))
        _compat, holder, wangp, locks, _clock, _notes, protocol = setup()
        with installed(locks=locks):
            answer = holder.hold(LEASE, 45.0, "")
            r.check("and every hold answer carries them", answer["vram_free"] == 7 * GB and answer["vram_total"] == 32 * GB,
                    str(answer))
            wgp, guard, registry = make_wgp()
            with installed(wgp=wgp, unload=guard, registry=registry):
                holder.flush(LEASE, protocol.FLUSH_SOFT)
            r.check("a soft flush empties the allocator's cache", cuda.emptied == 1)
            holder.resume(LEASE)


def hello_checks(r: Results) -> None:
    """hello: the capability flag and WanGP's activity, over the real transport."""
    compatibility, control, _hold, protocol = _modules()
    locks = make_locks()
    wangp = WanGP(locks)
    scratch = tempfile.mkdtemp(prefix="minipaint-hold-")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    environ = {
        compatibility.ENV_INSTANCE_ID: "i" * 32, compatibility.ENV_HANDOFF_ROOT: scratch,
        compatibility.ENV_BRIDGE_SECRET: "s3cr3t", compatibility.ENV_CONTROL_PORT: str(port),
        compatibility.ENV_LEDGER_ROOT: scratch,
    }
    compat = compatibility.Compatibility(
        host=compatibility.Host(FakeHost({"service_for": lambda *_a: wangp, "get_gen_info": lambda state: state["gen"]})),
        environ=environ,
    )
    compat.declare_globals()
    surface = control.ControlSurface(compat, environ=environ, note=lambda _text: None)
    surface.holder._threaded = False

    def call(operation, payload=None, secret="s3cr3t"):
        request = urllib.request.Request(f"http://127.0.0.1:{port}{protocol.CONTROL_PREFIX}/{operation}",
                                         data=json.dumps(payload or {}).encode(), method="POST")
        request.add_header(protocol.CONTROL_SECRET_HEADER, secret)
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    with installed(locks=locks):
        r.check("the surface opens", surface.start() is True)
        try:
            status, body = call(protocol.CONTROL_HELLO)
            hello = protocol.normalize_control_hello(body)
            r.check("hello says this bridge speaks hold, resume and flush",
                    status == 200 and body.get("capabilities") == {"hold": True} and hello["capabilities"]["hold"] is True,
                    str(body.get("capabilities")))
            r.check("and carries every field the lease reads, with no hold yet",
                    all(key in body for key in ("worker", "task_running", "active_client_id", "queue_length", "hold",
                                                "waiter", "vram_free", "vram_total"))
                    and body["hold"] == protocol.HOLD_NONE and body["worker"] is False and body["task_running"] is False,
                    str({key: body.get(key) for key in ("hold", "worker", "task_running", "waiter")}))
            r.check("and the bridge is 1.12.0", body["bridge_version"] == "1.12.0" == compatibility.BRIDGE_VERSION,
                    str(body.get("bridge_version")))

            status, body = call(protocol.CONTROL_HOLD, {"lease": LEASE, "ttl_s": 45, "label": "Speech engine"})
            answer = protocol.normalize_hold_answer(body)
            r.check("hold over the wire is held on an idle WanGP", status == 200 and answer["hold"] == protocol.HOLD_HELD,
                    f"{status} {body}")
            status, body = call(protocol.CONTROL_HELLO)
            r.check("and hello says so", body["hold"] == protocol.HOLD_HELD, str(body.get("hold")))
            status, body = call(protocol.CONTROL_HOLD, {"lease": "not-a-lease", "ttl_s": 45})
            r.check("a hold without a proper lease id is refused as invalid", status == 400
                    and body["code"] == protocol.QUEUE_CODE_REQUEST_INVALID, f"{status} {body}")
            status, body = call(protocol.CONTROL_FLUSH, {"lease": OTHER_LEASE, "level": "soft"})
            r.check("a flush for another lease is a coded refusal, not a crash",
                    status == 409 and body["code"] == compatibility.HOLD_NOT_HELD, f"{status} {body}")
            status, body = call(protocol.CONTROL_FLUSH, {"lease": LEASE, "level": "medium"})
            r.check("and a flush that is neither soft nor hard is invalid", status == 400, f"{status} {body}")
            status, body = call(protocol.CONTROL_RESUME, {"lease": LEASE}, secret="wrong")
            r.check("resume needs the credential like everything else", status == 401, f"{status} {body}")
            status, body = call(protocol.CONTROL_RESUME, {"lease": LEASE})
            r.check("resume over the wire lets go", status == 200 and body["resumed"] is True
                    and wangp.gen.get("queue_paused_for_edit") is False, f"{status} {body}")
            status, body = call("pause")
            r.check("an operation that is not one is still not found", status == 404, f"{status} {body}")

            surface.holder.hold(LEASE, 45.0, "")
        finally:
            surface.stop()
        r.check("closing the surface lets a hold go rather than leaving it with nobody to renew it",
                surface.holder.status()["hold"] == protocol.HOLD_NONE and wangp.gen.get("queue_paused_for_edit") is False)


def older_bridge_checks(r: Results) -> None:
    """An older bridge is recognised by what its hello leaves out."""
    _compatibility, _control, _hold, protocol = _modules()
    old = protocol.normalize_control_hello({"ok": True, "control_version": 1, "bridge_version": "1.11.0"})
    r.check("a hello without the capability reads as a bridge that cannot hold",
            old["capabilities"] == {"hold": False}, str(old["capabilities"]))
    r.check("and its hold word is empty - it did not say - never 'none'", old["hold"] == "", repr(old["hold"]))
    r.check("the control version did not move, so everything else an older bridge does still works",
            protocol.CONTROL_VERSION == 1)
    r.check("the three operations are on the one list the surface and the client both check",
            {protocol.CONTROL_HOLD, protocol.CONTROL_RESUME, protocol.CONTROL_FLUSH} <= set(protocol.CONTROL_OPERATIONS))
    garbled = protocol.normalize_hold_answer({"ok": True, "hold": "mostly"})
    r.check("an answer that names no hold state is not held", garbled["ok"] is False and garbled["hold"] == "", str(garbled))
    info = json.loads((BRIDGE_DIR / "plugin_info.json").read_text(encoding="utf-8"))
    r.check("plugin_info.json lists the hold among its capabilities", "hold" in info.get("capabilities", []),
            str(info.get("capabilities")))


def run() -> Results:
    r = Results("wangp hold")
    idle_checks(r)
    busy_checks(r)
    ours_checks(r)
    reassert_checks(r)
    ttl_checks(r)
    rekey_checks(r)
    stale_checks(r)
    busy_other_checks(r)
    flush_checks(r)
    flush_refusal_checks(r)
    unsupported_checks(r)
    pinned_revision_checks(r)
    vram_checks(r)
    hello_checks(r)
    older_bridge_checks(r)
    return r


if __name__ == "__main__":
    raise SystemExit(0 if run().report() else 1)
