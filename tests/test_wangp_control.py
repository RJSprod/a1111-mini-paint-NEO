"""Server-owned execution inside WanGP: the control surface, and the one worker.

THE PROPERTY THIS FILE EXISTS TO ASSERT IS IDENTITY, NOT TRUTHFULNESS.

The hazard server execution introduces is two generations against one card.
The obvious way to test for it - read a flag, check it says the right thing -
cannot observe the failure at all: the flag has four unconditional writers,
no owner and no nesting count, and every assertion about what it says is
fully compatible with two generations running. So what is asserted here is
that there is exactly **one worker object** and exactly **one entry into the
queue loop**, across both submission paths, in both arrival orders.

Wan2GP is not vendored into this repository and cannot be imported here - it
reaches torch transitively - so the arbiter is stood in for by a stub with
the same structure and the same guarantees: one mutation lock, one worker
thread started only when none exists, ``start_generation`` returning the
existing worker rather than a second one, a queue every state shares by
reference, and a finally block that finalises, syncs and unloads *before* it
clears the worker handle. That last detail is not decoration - it is the
stranded-task window, and one of the tests below drives a submission into it
deliberately.

The rest is the control plane: that it refuses without the credential before
it reads a body, that a repeated submission is answered from the ledger
rather than by asking WanGP twice, that a ledger record is written before the
submission and not after, that compose reads the process-wide committed form
rather than a session's, that media flags are recomputed from the job's own
pictures rather than inherited from whoever composed the base, and that the
execution id reaches WanGP as the task's own client id so the artifacts come
back identifiable.
"""

from harness import Results, ROOT, setup_path

setup_path()

import importlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import pathlib  # noqa: E402
import shutil  # noqa: E402
import socket  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import types  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

BRIDGE_DIR = ROOT / "wan2gp_bridge" / "wan2gp-minipaint-bridge"


def _modules():
    added = str(BRIDGE_DIR) not in sys.path
    if added:
        sys.path.insert(0, str(BRIDGE_DIR))
    try:
        import compatibility
        import compose
        import control
        import execution
        import ledger
        import protocol
    finally:
        if added and str(BRIDGE_DIR) in sys.path:
            sys.path.remove(str(BRIDGE_DIR))
    return compatibility, compose, control, execution, ledger, protocol


def _bridge_class():
    """``plugin.MiniPaintBridge``, imported the way ``_modules`` imports the rest.

    Separate because ``plugin`` reaches the receiver modules on import and
    only the flush checks need it; everything else here works against the
    control surface and the executor directly.
    """
    added = str(BRIDGE_DIR) not in sys.path
    if added:
        sys.path.insert(0, str(BRIDGE_DIR))
    try:
        import plugin

        return plugin.MiniPaintBridge
    finally:
        if added and str(BRIDGE_DIR) in sys.path:
            sys.path.remove(str(BRIDGE_DIR))


# --------------------------------------------------------------- the stub --


class FakeService:
    """Wan2GP's one generation service, with the parts that matter kept.

    The parts that matter are: a mutation lock; a worker started only when
    none is running; ``start_generation`` handing back the existing worker
    instead of starting a second; one queue every caller shares by reference;
    and a finally that does its unload *before* it clears the worker handle.
    """

    def __init__(self, gen):
        self._state = {"gen": gen}
        self._mutation_lock = threading.RLock()
        self._queue_worker = None
        #: Every distinct worker object ever started, and every entry into
        #: the drain loop. The two counts are what the identity tests read.
        self.workers = []
        self.loop_entries = 0
        self.commands = []
        #: Tasks Wan2GP's unpacker would have thrown away. Never empty by
        #: accident: a skipped task is a job that will wait forever.
        self.skipped = []
        #: Tasks that reached the worker and were refused by Python before
        #: a frame was generated. Same rule: never empty by accident.
        self.dispatch_errors = []
        self.gate = threading.Event()
        self.gate.set()
        self.unload_gate = threading.Event()
        self.unload_gate.set()
        self.aborted = 0
        self.forms = {}

    # -- the arbiter
    def start_generation(self):
        with self._mutation_lock:
            if self._queue_worker is None and self._state["gen"]["queue"]:
                worker = threading.Thread(target=self._run_generation, name="WanGP generation service", daemon=True)
                self._queue_worker = worker
                self.workers.append(worker)
                worker.start()
            return self._queue_worker

    @property
    def generation_running(self):
        return self._queue_worker is not None

    def command(self, name, payload=None):
        """``load_queue_trigger``, unpacked the way Wan2GP actually unpacks it.

        THIS STUB USED TO BE TOO KIND, AND IT COST A RELEASE.

        It read ``inline["params"]`` straight off the slot, which accepted a
        shape Wan2GP does not. Wan2GP wraps a *dict* itself -
        ``if isinstance(newly_loaded_queue, dict): newly_loaded_queue =
        [{"id": 0, "params": newly_loaded_queue}]`` - so a slot that was
        already ``{"id": …, "params": …}`` got wrapped twice and the
        manifest's params became ``{"id": 0, "params": {…}}``, one level
        above the settings and carrying no ``model_type``. Wan2GP said so and
        skipped the task; the job sat in the queue having never been one.

        So this mirrors the real rule, including the requirement that makes
        the failure visible: a task whose params have no ``model_type`` is
        skipped rather than quietly queued, and ``plugin_data`` is read off
        the *entry* rather than out of params, because that is the only
        place ``_parse_task_manifest`` looks for it.
        """
        self.commands.append((name, dict(payload or {})))
        gen = self._state["gen"]
        inline = gen.pop("inline_queue", None)
        if inline is not None:
            manifest = [{"id": 0, "params": inline}] if isinstance(inline, dict) else list(inline)
            for entry in manifest:
                params = dict((entry or {}).get("params") or {})
                if not params.get("model_type"):
                    # Wan2GP: "Settings must contain 'model_type'. Skipping."
                    self.skipped.append(params)
                    continue
                plugin_data = (entry or {}).get("plugin_data") or {}
                gen["queue"].append({"id": len(gen["queue"]) + 1, "params": params, "plugin_data": plugin_data})
        return self.start_generation()

    def abort(self):
        self.aborted += 1

    # -- what the worker does
    @staticmethod
    def _generate_media(task, send_cmd, plugin_data=None, **settings):
        """Wan2GP's generation entry point, in the one respect that matters.

        It takes ``plugin_data`` as a named parameter and the settings as
        the rest. That is the whole reason params and plugin_data cannot be
        the same dict, and it is a property of the *call*, so modelling the
        signature is enough to make Python enforce it here exactly as it
        does there.
        """
        return True

    def _dispatch(self, task):
        """The two lines of ``queue_worker_func`` that decide it runs.

        Wan2GP filters params down to the arguments ``generate_media`` names
        and splats them beside an explicit ``plugin_data=`` popped off the
        task. ``plugin_data`` is one of those names, so a copy of it left
        inside params is not filtered out and not ignored - it arrives
        twice, and Python refuses the call before anything is generated.
        """
        params = dict(task.get("params") or {})
        plugin_data = dict(task).pop("plugin_data", {})
        return self._generate_media(task, None, plugin_data=plugin_data, **params)

    def _run_generation(self):
        self.loop_entries += 1
        gen = self._state["gen"]
        try:
            gen["in_progress"] = True
            while gen["queue"]:
                self.gate.wait()
                task = gen["queue"][0]
                try:
                    self._dispatch(task)
                except TypeError as error:
                    # Wan2GP reports this one and abandons the task. Kept
                    # here instead so the rest of the queue still drains and
                    # the refusal is something a check can read.
                    self.dispatch_errors.append(str(error))
                else:
                    gen.setdefault("file_list", []).append(f"/out/{task['params'].get('client_id', 'x')}.mp4")
                gen["queue"].pop(0)
        finally:
            gen["in_progress"] = False
            # The window: finalise, sync, unload - all before the handle is
            # cleared. A task landing here is merged into a queue whose
            # worker has already left its loop.
            self.unload_gate.wait()
            with self._mutation_lock:
                self._queue_worker = None

    # -- the compose seam
    def load_model_form(self, model_type):
        return self.forms.get(model_type)

    def record_model_form(self, model_type, values):
        self.forms[model_type] = dict(values)


class _Component:
    """As much of a Gradio component as ``Host.is_component`` looks at.

    It checks for ``_id`` and ``get_config`` and nothing else, on purpose: a
    value that is merely *named* like a component takes WanGP's whole UI
    build into safe mode when it reaches an event.
    """

    _id = 4242

    def get_config(self):
        return {}


class FakeHost:
    """The plugin API surface ``compatibility.Host`` reads through."""

    def __init__(self, globals_map=None):
        self._globals = dict(globals_map or {})
        self.requested = []

    def request_component(self, elem_id):
        return None

    def request_global(self, name):
        self.requested.append(name)
        return self._globals.get(name)

    def get_global(self, name):
        return self._globals.get(name)


def _compat(globals_map=None, environ=None):
    compatibility, _compose, _control, _execution, _ledger, _protocol = _modules()
    found = compatibility.Compatibility(host=compatibility.Host(FakeHost(globals_map)), environ=environ or {})
    found.declare_globals()
    return found


def _environ(root, port=0, secret="s3cr3t"):
    compatibility = _modules()[0]
    return {
        compatibility.ENV_INSTANCE_ID: "i" * 32,
        compatibility.ENV_HANDOFF_ROOT: root,
        compatibility.ENV_BRIDGE_SECRET: secret,
        compatibility.ENV_CONTROL_PORT: str(port),
        compatibility.ENV_LEDGER_ROOT: root,
    }


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _executor(gen=None, root=None, forms=None):
    """An executor wired to a fake service, with a real durable ledger."""
    _compatibility, _compose, _control, execution, ledger, _protocol = _modules()
    gen = gen if gen is not None else {"queue": [], "in_progress": False}
    service = FakeService(gen)
    service.forms.update(forms or {})
    scratch = root or tempfile.mkdtemp(prefix="minipaint-control-")
    compat = _compat({"service_for": lambda *_a: service, "get_gen_info": lambda state: state["gen"]})
    book = ledger.Ledger(root=scratch, instance="i" * 32)
    return execution.Executor(compat, book, environ={}), service, gen, book


def _settings(**extra):
    # ``model_type`` is in here because a real compose always puts it there,
    # and because Wan2GP's queue unpacker skips a task without it. A fixture
    # that left it out was submitting tasks the real WanGP would throw away.
    base = {"image_start": None, "image_end": None, "image_refs": [], "image_prompt_type": "",
            "video_prompt_type": "", "steps": 30, "model_type": "t2v"}
    base.update(extra)
    return base


def _submit(runner, execution_id, **extra):
    request = {"execution_id": execution_id, "settings": _settings(), "media": {}, "model_type": "", "priority": False}
    request.update(extra)
    return runner.submit(request)


def _drain(service, gen, timeout=3.0):
    """Let the worker finish and settle, the way a real generation would."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not gen["queue"] and not service.generation_running:
            return True
        time.sleep(0.01)
    return False


# ------------------------------------------------------ the identity tests --


def identity_checks(r: Results) -> None:
    """Section 7.7's six, which are the release gate for the whole design."""
    _compatibility, _compose, _control, _execution, _ledger, protocol = _modules()

    # (1) Worker identity, the user first. A generation is already running
    #     when MiniPaint submits: no second worker starts, and the running
    #     one drains our task from the same list.
    runner, service, gen, _book = _executor()
    service.gate.clear()
    gen["queue"].append({"id": 0, "params": {"client_id": "user"}})
    first = service.start_generation()
    _submit(runner, "a" * 32)
    second = service.start_generation()
    r.check("user first: no second worker is started", first is second and len(service.workers) == 1,
            f"{len(service.workers)} worker(s)")
    r.check("user first: the queue loop is entered exactly once", service.loop_entries == 1, str(service.loop_entries))
    r.check("user first: our task is in the one shared queue",
            any((task.get("params") or {}).get("client_id") == "a" * 32 for task in gen["queue"]), str(gen["queue"]))
    service.gate.set()
    _drain(service, gen)

    # (2) Worker identity, MiniPaint first. THE DIRECTION A FLAG CHECK
    #     STRUCTURALLY CANNOT COVER: nothing between a user's Generate press
    #     and generate_media consults anything a plugin can own, so this one
    #     is only safe because both paths funnel through one gate.
    runner, service, gen, _book = _executor()
    service.gate.clear()
    _submit(runner, "b" * 32)
    ours = service._queue_worker
    gen["queue"].append({"id": 9, "params": {"client_id": "user"}})
    theirs = service.start_generation()
    r.check("MiniPaint first: the user's press observes our worker rather than starting one",
            ours is theirs and len(service.workers) == 1, f"{len(service.workers)} worker(s)")
    r.check("MiniPaint first: the queue loop is still entered exactly once", service.loop_entries == 1, str(service.loop_entries))
    service.gate.set()
    _drain(service, gen)

    # (3) No flag on the execution path. The submission never reads
    #     is_generation_in_progress, and never writes it.
    reads = {"count": 0}

    def counting_flag():
        reads["count"] += 1
        return False

    gen = {"queue": [], "in_progress": False}
    service = FakeService(gen)
    compat = _compat({"service_for": lambda *_a: service, "get_gen_info": lambda state: state["gen"],
                      "is_generation_in_progress": counting_flag})
    _c, _co, _ct, execution_module, ledger_module, _p = _modules()
    book = ledger_module.Ledger(root=tempfile.mkdtemp(prefix="minipaint-flag-"), instance="i" * 32)
    runner = execution_module.Executor(compat, book, environ={})
    service.gate.clear()
    _submit(runner, "c" * 32)
    r.check("the submission path never reads WanGP's generation flag", reads["count"] == 0, str(reads["count"]))
    r.check("the execution id is in the shared queue as the task's client id",
            any((task.get("params") or {}).get("client_id") == "c" * 32 for task in gen["queue"]), str(gen["queue"]))
    service.gate.set()
    _drain(service, gen)
    source = (BRIDGE_DIR.parent.parent / "wan2gp_bridge").rglob("*.py")
    assigns = [path.name for path in source if "gen_in_progress =" in path.read_text(encoding="utf-8")]
    r.check("no code in the bridge assigns wgp.gen_in_progress", not assigns, ", ".join(assigns))

    # (4) The stranded-task window, deterministically. The worker is held
    #     inside its finally - after the drain loop has exited and before the
    #     handle is cleared - and a submission lands there.
    runner, service, gen, _book = _executor()
    service.unload_gate.clear()
    gen["queue"].append({"id": 0, "params": {"client_id": "user"}})
    service.start_generation()
    deadline = time.monotonic() + 2.0
    while gen["queue"] and time.monotonic() < deadline:
        time.sleep(0.01)
    _submit(runner, "d" * 32)  # lands in the window
    service.unload_gate.set()
    runner._tick()
    time.sleep(0.05)
    for _ in range(40):
        runner._watched.get("d" * 32, {})["idle_since"] = 1.0
        runner._tick()
        if not gen["queue"]:
            break
        time.sleep(0.02)
    r.check("a task stranded in the unload window is re-triggered and runs",
            _drain(service, gen) and service.loop_entries >= 2, f"entries {service.loop_entries}, queue {gen['queue']}")

    # (5) Stuck-flag immunity. ``main_process_running`` left True by an
    #     exception two branches earlier must not wedge the queue forever.
    runner, service, gen, _book = _executor()
    gen["main_process_running"] = True
    gen["process_status"] = "process:main"
    record = _submit(runner, "e" * 32)
    r.check("a stuck main_process_running does not stop a submission",
            record["state"] in (_modules()[5].EXEC_ACCEPTED, _modules()[5].EXEC_QUEUED, _modules()[5].EXEC_RUNNING), record["state"])
    r.check("and the job completes", _drain(service, gen), str(gen["queue"]))

    # (6) Ordering. Unattended work goes on the end of whatever the user has
    #     queued, and stays there: nothing this path submits reorders the
    #     queue in front of somebody.
    runner, service, gen, _book = _executor()
    service.gate.clear()
    gen["queue"].extend([{"id": 1, "params": {"client_id": "user1"}}, {"id": 2, "params": {"client_id": "user2"}}])
    service.start_generation()
    _submit(runner, "f" * 32)
    r.check("an unattended job is appended behind the user's own tasks",
            (gen["queue"][-1].get("params") or {}).get("client_id") == "f" * 32,
            str([(task.get("params") or {}).get("client_id") for task in gen["queue"]]))
    # Even asked for, priority does not splice. Wan2GP's queue manifest has
    # no such key - an entry is {id, params, plugin_data} and nothing else -
    # so a priority flag written into it was read by nobody, and the only way
    # to honour one would be to reorder the queue a user is looking at. This
    # used to pass because the stub invented a priority the real loader has
    # not, which is how a stub stops being a model of anything.
    _submit(runner, "0" * 32, priority=True)
    r.check("even a job somebody is waiting for goes on the end: the user's queue is never reordered",
            [(task.get("params") or {}).get("client_id") for task in gen["queue"]]
            == ["user1", "user2", "f" * 32, "0" * 32],
            str([(task.get("params") or {}).get("client_id") for task in gen["queue"]]))
    service.gate.set()
    _drain(service, gen)


# ------------------------------------------------------------- the ledger --


def inheritance_checks(r: Results) -> None:
    """Off does not read the user's form. It is not read-then-discard.

    "Build the job from what the WanGP page is set to" and "let WanGP fill it
    in from that model's saved defaults" are two answers somebody chooses
    between, not a preference and a fallback. So with it off the recorded
    form is never asked for - a base that was read and then thrown away is
    still a base that was read, and the job would say ``recorded_form`` about
    settings it did not use.
    """
    compatibility, compose, _control, _execution, _ledger, protocol = _modules()
    gen = {"queue": [], "in_progress": False, "model_type": "t2v"}
    service = FakeService(gen)
    service.forms["t2v"] = {"steps": 42, "loras_multipliers": "0.9"}
    asked = []

    class Watched(type(service)):  # type: ignore[misc]
        pass

    host = compatibility.Host(FakeHost({
        "service_for": lambda *_a: service,
        "get_gen_info": lambda state: state["gen"],
        "get_default_settings": lambda model_type: {"steps": 30, "from": "defaults"},
        "get_model_def": lambda model_type: {"name": "T2V"},
    }))
    compat = compatibility.Compatibility(host=host, environ={})
    compat.declare_globals()
    original = compat.recorded_form

    def watched(service_object, model_type):
        asked.append(model_type)
        return original(service_object, model_type)

    compat.recorded_form = watched  # type: ignore[assignment]
    composer = compose.Composer(compat, note=lambda _text: None)

    answer = composer.compose("t2v", "", True)
    r.check("with inheritance on the user's committed form is what the job runs at",
            answer["source"] == protocol.BASE_RECORDED and answer["settings"]["steps"] == 42, str(answer["source"]))
    r.check("and reading it is what was done", asked == ["t2v"], str(asked))

    asked.clear()
    answer = composer.compose("t2v", "", False)
    r.check("with it off the job runs at the model's own defaults",
            answer["source"] == protocol.BASE_FACTORY and answer["settings"].get("from") == "defaults", str(answer["source"]))
    r.check("and the user's form was never read at all, not read and discarded", asked == [], str(asked))
    r.check("the model still travels, because a task without one is a task WanGP skips",
            answer["settings"]["model_type"] == "t2v", str(answer["settings"].get("model_type")))


def manifest_shape_checks(r: Results) -> None:
    """The task reaches WanGP in the shape WanGP's own unpacker reads.

    THE FAILURE THIS CATCHES WAS SILENT AND COMPLETE.

    Wan2GP's loader wraps the inline slot when it finds a dict::

        if isinstance(newly_loaded_queue, dict):
            newly_loaded_queue = [{"id": 0, "params": newly_loaded_queue}]

    Handing it a dict that was already ``{"id": …, "params": …}`` wrapped it
    twice, so the manifest's ``params`` was ``{"id": 0, "params": {…}}`` - a
    mapping one level above the settings with no ``model_type`` in it.
    Wan2GP printed "Settings must contain 'model_type'", skipped the task and
    carried on; the bridge saw a command that returned cleanly and reported
    the job handed over. It then waited forever for a generation that had
    never been queued.

    Nothing in the round trip said no. That is why the shape is asserted
    here, and why the stub now applies Wan2GP's rule rather than being kind.
    """
    _compatibility, _compose, _control, _execution, _ledger, protocol = _modules()
    runner, service, gen, _book = _executor()
    service.gate.clear()
    _submit(runner, "a" * 32, model_type="t2v")

    r.check("the slot is a list, so Wan2GP reads it as the manifest instead of wrapping it again",
            gen["queue"] and not service.skipped, f"queued {len(gen['queue'])}, skipped {len(service.skipped)}")
    params = (gen["queue"][-1].get("params") or {}) if gen["queue"] else {}
    r.check("the task's params ARE the settings, not a box with the settings inside",
            params.get("model_type") == "t2v" and "params" not in params, str(sorted(params)[:6]))
    r.check("and they carry the job's own id as WanGP's client id, so the artifacts come back identifiable",
            params.get("client_id") == "a" * 32, str(params.get("client_id")))
    service.gate.set()
    _drain(service, gen)

    # And a snapshot that names no model is refused rather than handed over
    # to be skipped: a visible code beats a job that hangs.
    runner, service, gen, _book = _executor()
    refused = ""
    try:
        _submit(runner, "b" * 32, settings={"steps": 30}, model_type="")
    except _execution.ExecutionError as error:
        refused = error.code
    r.check("settings that name no model are refused here, not skipped silently there",
            refused == protocol.MODEL_UNAVAILABLE, refused)
    r.check("and nothing was left in WanGP's queue for it", not gen["queue"], str(gen["queue"]))

    # ``plugin_data`` is a sibling of params, and a base that carries it
    # inside them is not hypothetical: the composed base is a snapshot of
    # the user's committed form, Wan2GP's own recorder pops the key before
    # storing one, but another plugin that captures the form for itself may
    # record it with the key still on. Every bridge job on that install then
    # died in the worker - "got multiple values for keyword argument
    # 'plugin_data'" - after the bridge had reported the job handed over,
    # which is the same silent-complete failure this function opens with.
    runner, service, gen, _book = _executor()
    service.gate.clear()
    _submit(runner, "c" * 32, settings=_settings(plugin_data={"api": {"return_audio": True}}), model_type="t2v")
    entry = gen["queue"][-1] if gen["queue"] else {}
    r.check("a base that carries plugin_data still queues the task",
            gen["queue"] and not service.skipped, f"queued {len(gen['queue'])}, skipped {len(service.skipped)}")
    r.check("plugin_data is lifted out of params, where Wan2GP would pass it to generate_media twice",
            "plugin_data" not in (entry.get("params") or {}), str(sorted(entry.get("params") or {})[:8]))
    r.check("and put beside them, where Wan2GP's unpacker reads it, so the plugin's data is carried not dropped",
            (entry.get("plugin_data") or {}) == {"api": {"return_audio": True}}, str(entry.get("plugin_data")))
    service.gate.set()
    _drain(service, gen)
    r.check("so the worker generates instead of refusing the call", not service.dispatch_errors,
            "; ".join(service.dispatch_errors))


def ledger_checks(r: Results) -> None:
    _compatibility, _compose, _control, execution, ledger, protocol = _modules()
    scratch = tempfile.mkdtemp(prefix="minipaint-ledger-")

    book = ledger.Ledger(root=scratch, instance="one")
    record = book.reserve("a" * 32, model_type="t2v", residency_key="t2v||")
    r.check("a reservation is accepted and written before anything is submitted",
            record["state"] == protocol.EXEC_ACCEPTED and (book.path).exists())
    r.check("and it names the run that made it", record["instance"] == "one")

    stored = json.loads(book.path.read_text(encoding="utf-8"))
    r.check("the reservation is on disk, not only in memory",
            any(item.get("execution_id") == "a" * 32 for item in stored["records"]), str(stored)[:120])

    book.update("a" * 32, state=protocol.EXEC_DONE, generated_files=["/out/a.mp4"])
    r.check("a terminal record carries its files", book.get("a" * 32)["generated_files"] == ["/out/a.mp4"])
    book.update("a" * 32, state=protocol.EXEC_RUNNING)
    r.check("and never moves again: a stale reading cannot un-finish a job",
            book.get("a" * 32)["state"] == protocol.EXEC_DONE)

    r.check("a terminal record can be let go once Forge has it", book.forget("a" * 32) is True and book.get("a" * 32) is None)
    book.reserve("b" * 32)
    r.check("a live one cannot", book.forget("b" * 32) is False and book.get("b" * 32) is not None)

    # A second run of the child reads what the first left, and every
    # submission it cannot account for becomes unknown rather than absent.
    reopened = ledger.Ledger(root=scratch, instance="two")
    reopened.load()
    r.check("a new run reads the records the old one wrote", reopened.get("b" * 32) is not None)
    changed = reopened.orphan_open_records()
    r.check("and an open submission from a process that is gone becomes unknown, never a resubmission",
            changed == 1 and reopened.get("b" * 32)["state"] == protocol.EXEC_UNKNOWN)
    r.check("with the code that stops an automatic retry upstream",
            reopened.get("b" * 32)["code"] == protocol.EXECUTION_UNKNOWN)


def idempotency_checks(r: Results) -> None:
    """The one guarantee WanGP itself cannot make: submit twice, generate once."""
    _compatibility, _compose, _control, _execution, _ledger, protocol = _modules()
    runner, service, gen, book = _executor()
    service.gate.clear()

    first = _submit(runner, "a" * 32)
    before = len(gen["queue"])
    second = _submit(runner, "a" * 32)
    r.check("a repeated submission is answered from the ledger",
            second["execution_id"] == first["execution_id"] and len(gen["queue"]) == before, str(gen["queue"]))
    r.check("and never asks WanGP a second time", len(service.commands) == 1, str(service.commands))

    # The order that matters: the record exists before the submission does.
    order = []
    original = book.reserve

    def watched(execution_id, **keywords):
        order.append("reserve")
        return original(execution_id, **keywords)

    book.reserve = watched
    original_command = service.command

    def watched_command(name, payload=None):
        order.append("submit")
        return original_command(name, payload)

    service.command = watched_command
    _submit(runner, "b" * 32)
    r.check("the ledger record is written before the submission, not after", order == ["reserve", "submit"], str(order))
    service.gate.set()
    _drain(service, gen)


# ------------------------------------------------------------- the surface --


def surface_checks(r: Results) -> None:
    """The transport: authenticated, refusing before it reads, and honest."""
    compatibility, _compose, control, _execution, _ledger, protocol = _modules()
    scratch = tempfile.mkdtemp(prefix="minipaint-surface-")
    port = _free_port()
    environ = _environ(scratch, port)
    gen = {"queue": [], "in_progress": False}
    service = FakeService(gen)
    compat = compatibility.Compatibility(
        host=compatibility.Host(FakeHost({"service_for": lambda *_a: service, "get_gen_info": lambda state: state["gen"]})),
        environ=environ,
    )
    compat.declare_globals()
    surface = control.ControlSurface(compat, environ=environ, note=lambda _text: None)
    r.check("the surface will not open without everything it needs",
            control.ControlSurface(compat, environ={}, note=lambda _t: None).usable()[0] is False)
    r.check("nor without a credential",
            control.ControlSurface(compat, environ=dict(environ, **{compatibility.ENV_BRIDGE_SECRET: ""}), note=lambda _t: None).usable()[0] is False)
    r.check("it opens when they are all there", surface.start() is True and surface.listening)

    def call(operation, payload=None, secret="s3cr3t", method="POST"):
        url = f"http://127.0.0.1:{port}{protocol.CONTROL_PREFIX}/{operation}"
        data = json.dumps(payload or {}).encode() if method == "POST" else None
        request = urllib.request.Request(url, data=data, method=method)
        if secret:
            request.add_header(protocol.CONTROL_SECRET_HEADER, secret)
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    try:
        status, body = call(protocol.CONTROL_HELLO, method="GET")
        r.check("hello answers what this child is and whether it can take a job",
                status == 200 and body["service"] is True and body["control_version"] == protocol.CONTROL_VERSION, str(body)[:160])
        r.check("and names the Wan2GP revision it was proven against",
                body["wan2gp_revision_pinned"] == compatibility.WAN2GP_EXECUTION_REVISION)

        status, body = call(protocol.CONTROL_HELLO, secret="", method="GET")
        r.check("no credential is refused", status == 401 and body["code"] == protocol.CONTROL_UNAUTHORISED, str(body))
        status, body = call(protocol.CONTROL_HELLO, secret="wrong", method="GET")
        r.check("a wrong credential is refused", status == 401 and body["code"] == protocol.CONTROL_UNAUTHORISED, str(body))
        status, body = call("generate")
        r.check("an operation that is not one is not found, so a probe learns nothing",
                status == 404, f"{status} {body}")

        # The credential is compared before the body is read: an unauthorised
        # caller never gets to hand this process a megabyte to parse.
        url = f"http://127.0.0.1:{port}{protocol.CONTROL_PREFIX}/{protocol.CONTROL_SUBMIT}"
        request = urllib.request.Request(url, data=b'{"x":' + b"1" * 4096 + b"}", method="POST")
        request.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(request, timeout=5)
            refused = False
        except urllib.error.HTTPError as error:
            refused = error.code == 401
        r.check("an unauthorised body is refused rather than parsed", refused)

        service.forms["t2v"] = {"steps": 42, "image_prompt_type": "S", "client_id": "somebody-elses-page"}
        status, body = call(protocol.CONTROL_COMPOSE, {"model_type": "t2v"})
        r.check("compose returns the form the user last committed for that model",
                status == 200 and body["settings"]["steps"] == 42 and body["source"] == protocol.BASE_RECORDED, str(body)[:200])
        r.check("and strips the composing page's client id, which is never this job's",
                "client_id" not in body["settings"])
        r.check("and never carries a session handle across the wire", "state" not in body["settings"])

        service.forms.pop("t2v")
        status, body = call(protocol.CONTROL_COMPOSE, {"model_type": "t2v"})
        r.check("a model with no committed form falls back to factory defaults and says which",
                status == 409 or body.get("source") in (protocol.BASE_FACTORY, ""), str(body)[:160])

        status, body = call(protocol.CONTROL_SUBMIT, {"execution_id": "a" * 32, "settings": _settings(), "prompt": "go"})
        r.check("a submission is recorded and answered at once",
                status == 200 and body["record"]["execution_id"] == "a" * 32, str(body)[:160])
        r.check("and the handler did not wait for the generation",
                body["record"]["state"] in (protocol.EXEC_ACCEPTED, protocol.EXEC_QUEUED, protocol.EXEC_RUNNING, protocol.EXEC_DONE))

        status, body = call(protocol.CONTROL_STATUS, {"execution_ids": ["a" * 32, "9" * 32]})
        r.check("status answers for every id asked about", status == 200 and len(body["records"]) == 2, str(body)[:160])
        r.check("and a submission it has no record of is unknown, never 'did not happen'",
                body["records"]["9" * 32]["state"] == protocol.EXEC_UNKNOWN)

        status, body = call(protocol.CONTROL_SUBMIT, {"execution_id": "nope", "settings": _settings()})
        r.check("a malformed submission is refused", status == 400, f"{status} {body}")
        status, body = call(protocol.CONTROL_SUBMIT, {"execution_id": "b" * 32, "settings": {}})
        r.check("and one with no settings too - a job never runs at whatever generate_media defaults to",
                status == 400, f"{status} {body}")
    finally:
        surface.stop()
    r.check("the surface closes cleanly", surface.listening is False)


# -------------------------------------------------------------- the media --


def media_checks(r: Results) -> None:
    """The job's own pictures, and the flags that describe them."""
    compatibility, _compose, _control, _execution, _ledger, _protocol = _modules()
    compat = _compat()

    # A base composed while the user had a start and an end frame selected,
    # and a job that supplies only a start frame. The flags must describe the
    # job's pictures, not the base's: WanGP's own back-fill only ADDS flags
    # implied by media that is present and never removes one whose media is
    # gone, so an inherited "E" would reach generation claiming a last frame
    # this job does not have.
    settings = _settings(image_prompt_type="SE", image_start="/theirs/a.png", image_end="/theirs/b.png")
    switched = compat.apply_media_to_settings(settings, {"start": ["/ours/one.png"]})
    r.check("the job's own picture replaces the base's", settings["image_start"] == "/ours/one.png")
    r.check("a slot this job does not fill is cleared, not inherited", settings["image_end"] is None)
    r.check("and the flags are recomputed from what is actually there",
            settings["image_prompt_type"] == "S", settings["image_prompt_type"])
    r.check("nothing is reported as switched when the base already had that slot on",
            switched == [], str(switched))

    settings = _settings(image_prompt_type="")
    switched = compat.apply_media_to_settings(settings, {"start": ["/ours/one.png"], "end": ["/ours/two.png"]})
    r.check("a slot the base did not have on is switched on, and said so",
            switched == ["start_frame", "end_frame"], str(switched))
    r.check("both frames switch their own flags on",
            set(settings["image_prompt_type"]) == {"S", "E"}, settings["image_prompt_type"])

    settings = _settings()
    compat.apply_media_to_settings(settings, {"references": ["/ours/r1.png", "/ours/r2.png"]})
    r.check("references are a list", settings["image_refs"] == ["/ours/r1.png", "/ours/r2.png"])

    # Only the letter strings belong in a settings dict; the radio and the
    # checkbox are controls a person operates to produce them.
    r.check("no page-only control leaks into the settings",
            not any(key.endswith("_radio") or key.endswith("_endcheckbox") for key in settings), str(sorted(settings)))


def client_id_checks(r: Results) -> None:
    """The execution id reaches WanGP as the task's own client id.

    Not a nicety. A composed base carries the *composing page's* client id,
    WanGP routes a generation's returned artifacts by that key, and an
    adapter that did not overwrite it would hand this job's files to somebody
    else's request.
    """
    runner, service, gen, _book = _executor()
    service.gate.clear()
    request = {"execution_id": "a" * 32, "settings": _settings(client_id="somebody-elses-page"), "media": {}, "model_type": "", "priority": False}
    runner.submit(request)
    task = gen["queue"][0]
    r.check("the task carries this job's execution id as its client id",
            (task.get("params") or {}).get("client_id") == "a" * 32, str(task))
    service.gate.set()
    _drain(service, gen)


def model_checks(r: Results) -> None:
    """A job runs against the model it was composed for, or none."""
    _compatibility, _compose, _control, execution, ledger, protocol = _modules()
    gen = {"queue": [], "in_progress": False}
    service = FakeService(gen)
    compat = _compat({
        "service_for": lambda *_a: service,
        "get_gen_info": lambda state: state["gen"],
        "get_model_def": lambda model_type: {"name": "T2V"} if model_type == "t2v" else None,
    })
    runner = execution.Executor(compat, ledger.Ledger(root=tempfile.mkdtemp(prefix="minipaint-model-"), instance="i"), environ={})
    service.gate.clear()
    runner.submit({"execution_id": "a" * 32, "settings": _settings(), "media": {}, "model_type": "t2v", "priority": False})
    r.check("a model this WanGP has is submitted", len(gen["queue"]) == 1, str(gen["queue"]))
    refused = ""
    try:
        runner.submit({"execution_id": "b" * 32, "settings": _settings(), "media": {}, "model_type": "gone", "priority": False})
    except execution.ExecutionError as error:
        refused = error.code
    r.check("a model it no longer has is refused, never quietly swapped for the current one",
            refused == protocol.MODEL_UNAVAILABLE, refused)
    service.gate.set()
    _drain(service, gen)


def cancellation_checks(r: Results) -> None:
    """Ours stops; whatever is not ours is never disturbed."""
    _compatibility, _compose, _control, _execution, _ledger, protocol = _modules()
    runner, service, gen, _book = _executor()
    service.gate.clear()
    gen["queue"].append({"id": 0, "params": {"client_id": "user"}})
    service.start_generation()
    _submit(runner, "a" * 32)
    record = runner.cancel("a" * 32)
    r.check("a waiting task of ours is removed from the shared queue",
            record["state"] == protocol.EXEC_CANCELLED
            and not any((task.get("params") or {}).get("client_id") == "a" * 32 for task in gen["queue"]), str(gen["queue"]))
    r.check("and the user's own running task is untouched",
            (gen["queue"][0].get("params") or {}).get("client_id") == "user" and service.aborted == 0, str(gen["queue"]))
    service.gate.set()
    _drain(service, gen)

    # A cancel of a task at the head, while it is generating, is ours to
    # abort - and only because it is ours.
    runner, service, gen, _book = _executor()
    service.gate.clear()
    _submit(runner, "b" * 32)
    time.sleep(0.05)
    gen["in_progress"] = True
    runner.cancel("b" * 32)
    r.check("a generating task of ours is aborted through WanGP's own control", service.aborted == 1, str(service.aborted))
    service.gate.set()
    _drain(service, gen)


def unavailable_checks(r: Results) -> None:
    """A build without the arbiter refuses rather than running beside it."""
    _compatibility, _compose, _control, execution, ledger, protocol = _modules()
    compat = _compat({})  # no service_for, no importable hybrid module here
    runner = execution.Executor(compat, ledger.Ledger(root=tempfile.mkdtemp(prefix="minipaint-none-"), instance="i"), environ={})
    ok, code = runner.available()
    r.check("a build whose generation service cannot be reached says so",
            ok is False and code == protocol.SERVICE_UNAVAILABLE, code)
    refused = ""
    try:
        _submit(runner, "a" * 32)
    except execution.ExecutionError as error:
        refused = error.code
    r.check("and refuses the submission rather than falling back to a path beside the arbiter",
            refused == protocol.SERVICE_UNAVAILABLE, refused)


def flush_checks(r: Results) -> None:
    """Committing the page's live form: a write, and never a silent one.

    A job composes from the form Wan2GP recorded process-wide, and that is
    only written when the user commits one - Generate, Add to Queue in WanGP
    itself, a LoRA set applied, a model switched. A weight dragged and then
    left alone is in the browser and nowhere else, so the flush exists to let
    a page hand those values over before it walks away.

    What is asserted is the two things that make it safe rather than the fact
    that it writes: that a probe does not write (or every poll would restart
    the wait it exists to end), and that a page mid settings-load is refused
    rather than flushed - ``ignore_save_form`` is a one-shot suppression that
    a settings load sets so the model switch behind it cannot clobber what was
    just loaded, and a flush that popped it would hand that clobber to the
    user as their own press.
    """
    compatibility, _compose, _control, _execution, _ledger, protocol = _modules()
    scratch = tempfile.mkdtemp(prefix="minipaint-flush-")
    environ = _environ(scratch)
    gen = {"queue": [], "in_progress": False}
    service = FakeService(gen)
    service.forms["t2v"] = {"steps": 30, "loras_multipliers": "0.5"}
    gen["model_type"] = "t2v"
    host = compatibility.Host(FakeHost({"service_for": lambda *_a: service, "get_gen_info": lambda state: state["gen"]}))
    compat = compatibility.Compatibility(host=host, environ=environ)
    compat.declare_globals()
    # The bridge object itself, with its components resolved the way a real
    # page resolves them: ``save_form_trigger`` is handed over by variable
    # name like every other one, which is the whole reason it needs no elem_id.
    bridge = _bridge_class()(host=host, environ=environ)
    bridge.declare()
    bridge.compat.host.accept_components({"save_form_trigger": _Component()})
    bridge.resolve()

    before = compat.recorded_fingerprint(service, "t2v")
    r.check("a recorded form has a fingerprint, so a flush can be observed rather than assumed", len(before) == 32, before)
    service.forms["t2v"] = {"steps": 30, "loras_multipliers": "0.9"}
    r.check("and the fingerprint moves when the recorded form does",
            compat.recorded_fingerprint(service, "t2v") != before)
    r.check("a model with nothing recorded has no fingerprint rather than a made-up one",
            compat.recorded_fingerprint(service, "nothing-recorded") == "")

    answer, writes = bridge.flush({}, {compatibility.SESSION_STATE: {"gen": gen}})
    r.check("a press asks WanGP to commit its own form",
            answer["flush"] == protocol.FLUSH_REQUESTED and compatibility.SAVE_FORM_TRIGGER in (writes or {}), str(answer))
    r.check("by writing the trigger WanGP already wires to save_inputs, not by reading ninety components",
            list((writes or {}).keys()) == [compatibility.SAVE_FORM_TRIGGER], str(writes))

    answer, writes = bridge.flush({"probe": True}, {compatibility.SESSION_STATE: {"gen": gen}})
    r.check("a probe writes nothing, so polling cannot restart the wait it is ending",
            writes is None, str(writes))

    live = {compatibility.SESSION_STATE: {"gen": gen, "ignore_save_form": True}}
    suppressed, writes = bridge.flush({}, live)
    r.check("a page mid settings-load is refused rather than flushed",
            suppressed["flush"] == protocol.FLUSH_SUPPRESSED and writes is None, str(suppressed))
    r.check("and the suppression is left for WanGP's own save to consume, not eaten by the refusal",
            live[compatibility.SESSION_STATE].get("ignore_save_form") is True,
            str(live[compatibility.SESSION_STATE]))


def session_checks(r: Results) -> None:
    """Bridge 1.8.0: the page's Gradio session, reported and guarded.

    WHAT THIS EXISTS TO CATCH.

    Gradio 5 marks a page's session closed the moment its heartbeat stream
    ends - for any reason - and nothing marks it open when the browser's
    EventSource reconnects a second later; a background task then deletes
    the closed session's state as soon as it is more than an hour old. An
    embedded WanGP page's heartbeat rides Forge's connection through the
    proxy, so a blip that standalone WanGP never sees turns, an hour into a
    session, into a page that answers everything and does nothing. The guard
    wraps two of Gradio's own objects: the heartbeat route, so a reconnect
    reopens the session and every beat is remembered; and the expiry, so a
    session heard within the grace is never expired. And every hello marks
    the page's state, so a state that lost its marker after a drop is
    reported as reset - once on the console, and on every answer.
    """
    import asyncio

    compatibility, _compose, _control, _execution, _ledger, protocol = _modules()
    added = str(BRIDGE_DIR) not in sys.path
    if added:
        sys.path.insert(0, str(BRIDGE_DIR))
    try:
        import session_guard
    finally:
        if added and str(BRIDGE_DIR) in sys.path:
            sys.path.remove(str(BRIDGE_DIR))

    notes = []
    clock = {"now": 1000.0}
    guard = session_guard.SessionGuard(notes.append, clock=lambda: clock["now"])

    class Session:
        def __init__(self):
            self.is_closed = False

    class Holder:
        def __init__(self):
            self.session_data = {"abc": Session()}
            self.deleted = []

        def delete_state(self, session_id, expired_only=False):
            self.deleted.append((session_id, expired_only))

    sent = []

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"data: ALIVE\n\n", "more_body": True})
        await send({"type": "http.response.body", "body": b"data: ALIVE\n\n", "more_body": True})

    class Route:
        path = "/gradio_api/heartbeat/{session_hash}"

    class Other:
        path = "/gradio_api/queue/data"

    route = Route()
    route.app = inner
    other = Other()
    other.app = inner

    class Router:
        routes = [other, route]

    class App:
        state_holder = Holder()
        router = Router()

    app = App()
    r.check("the guard installs on a Gradio-shaped app, wrapping the heartbeat route's app",
            guard.install(app) is True and guard.installed and route.app is not inner and other.app is inner, guard.install_note)
    r.check("and says so once, naming the grace", sum("session guard: Gradio's heartbeat route is wrapped" in n for n in notes) == 1, str(notes))

    scope = {"type": "http", "path_params": {"session_hash": "abc"}}

    async def receive():
        return {"type": "http.request"}

    async def send(message):
        sent.append(message)

    asyncio.run(route.app(scope, receive, send))
    r.check("beats pass through untouched",
            [m.get("type") for m in sent] == ["http.response.start", "http.response.body", "http.response.body"], str(sent))
    r.check("and each beat is remembered as the moment the page was last heard", guard.recently_heard("abc") is True)
    r.check("and the stream's end is remembered too", "abc" in guard._dropped)

    # Gradio marks the session closed when the stream ends, and asks for the
    # expiry every second.
    app.state_holder.session_data["abc"].is_closed = True
    app.state_holder.delete_state("abc", expired_only=True)
    r.check("an expiry of a session heard a moment ago is skipped",
            app.state_holder.deleted == [] and guard.skipped_expiries == 1, str(app.state_holder.deleted))
    app.state_holder.delete_state("abc", expired_only=False)
    r.check("a deliberate deletion is not", app.state_holder.deleted == [("abc", False)], str(app.state_holder.deleted))

    clock["now"] += 3.0
    asyncio.run(route.app(scope, receive, send))
    r.check("a heartbeat reconnecting reopens the session Gradio had closed",
            app.state_holder.session_data["abc"].is_closed is False and guard.reopened == 1)
    r.check("and says so, with how long the stream had been gone, and no hash",
            any("heartbeat reconnected after 3s" in n and "reopened" in n and "abc" not in n for n in notes), str(notes[-1:]))

    clock["now"] += session_guard.GRACE_SECONDS + 1
    app.state_holder.deleted.clear()
    app.state_holder.delete_state("abc", expired_only=True)
    r.check("a session not heard within the grace is left to Gradio's own expiry",
            app.state_holder.deleted == [("abc", True)], str(app.state_holder.deleted))
    r.check("installing twice does nothing", guard.install(app) is True and guard.reopened == 1)

    class Empty:
        pass

    bare = session_guard.SessionGuard(notes.append)
    r.check("an app with no heartbeat route gets no guard and a reason", bare.install(Empty()) is False and bare.install_note != "")

    # -- the marker and the report ------------------------------------------
    holder = app.state_holder
    state = {"gen": {}}
    first = guard.observe("abc", state, "hello", "bsess-1", holder=holder)
    r.check("a hello marks the page's state with the bridge session",
            state.get(session_guard.MARKER_KEY) == "bsess-1" and first["reset"] is False, str(first))
    later = guard.observe("abc", state, "flush", "bsess-1", holder=holder)
    r.check("a later request from the same state is not a reset, and says how long since the page was heard",
            later["reset"] is False and later["closed"] is False and isinstance(later["silent_s"], int), str(later))
    fresh_guard = session_guard.SessionGuard(notes.append, clock=lambda: clock["now"])
    first_state = {}
    fresh_guard.observe("fresh", first_state, "hello", "b2", holder=holder)
    replaced = {}
    verdict = fresh_guard.observe("fresh", replaced, "flush", "b2", holder=holder)
    r.check("a state without the marker whose heartbeat never dropped is re-marked, not called reset",
            verdict["reset"] is False and replaced.get(session_guard.MARKER_KEY) == "b2", str(verdict))
    deleted = {"gen": {}}
    reset = guard.observe("abc", deleted, "flush", "bsess-1", holder=holder)
    r.check("a state without the marker after the heartbeat dropped is a reset", reset["reset"] is True, str(reset))
    r.check("said once, with no hash in it",
            sum("session state was reset by Gradio" in n for n in notes) == 1
            and all("abc" not in n for n in notes if "reset by Gradio" in n), str(notes))
    again = guard.observe("abc", deleted, "flush", "bsess-1", holder=holder)
    hello_again = guard.observe("abc", deleted, "hello", "bsess-1", holder=holder)
    r.check("and reported on every answer after, hellos included, until the page is a new session",
            again["reset"] is True and hello_again["reset"] is True
            and sum("session state was reset by Gradio" in n for n in notes) == 1, str(hello_again))
    holder.session_data["abc"].is_closed = True
    r.check("Gradio's own closed flag is read and reported, never written by the report",
            guard.observe("abc", deleted, "flush", "bsess-1", holder=holder)["closed"] is True
            and holder.session_data["abc"].is_closed is True)
    r.check("a request with no state and no holder is reported as nothing found",
            guard.observe("", None, "flush", "b", holder=None) == {"closed": False, "reset": False, "silent_s": None})

    # -- through the bridge itself --------------------------------------------
    scratch = tempfile.mkdtemp(prefix="minipaint-session-")
    environ = _environ(scratch)
    gen = {"queue": [], "in_progress": False}
    service = FakeService(gen)
    host = compatibility.Host(FakeHost({"service_for": lambda *_a: service, "get_gen_info": lambda s_: s_["gen"]}))
    bridge = _bridge_class()(host=host, environ=environ)
    bridge.declare()
    bridge.compat.host.accept_components({"save_form_trigger": _Component()})
    bridge.resolve()
    bridge.state_keys = (compatibility.SESSION_STATE,) + tuple(k for k in bridge.state_keys if k != compatibility.SESSION_STATE)
    r.check("the handshake offers the session capability, and the bridge is 1.8.0",
            compatibility.BRIDGE_VERSION == "1.8.0"
            and bridge.compat.handshake(bridge_session="s", environ=environ)["capabilities"].get("session") is True)
    info = json.loads((BRIDGE_DIR / "plugin_info.json").read_text(encoding="utf-8"))
    r.check("and plugin_info.json says the same version twice",
            info.get("version") == "1.8.0" and info.get("bridge_version") == "1.8.0", str(info))

    class GradioRequest:
        session_hash = "abc"
        request = types.SimpleNamespace(app=App())

    page_state = {"gen": gen}
    flush = json.dumps({"op": "flush", "request_id": "r1", "channel_id": "c" * 32, "probe": True})
    ack, _result = bridge.handle(flush, [page_state], "abc", GradioRequest())
    r.check("every acknowledgement carries the session report, and the first request marks the state",
            isinstance(ack.get("session"), dict) and ack["session"]["reset"] is False
            and page_state.get(session_guard.MARKER_KEY) == ack["bridge_session"], str(ack.get("session")))
    r.check("and the guard was installed from that request's app, once",
            bridge.sessions.installed is True and GradioRequest.request.app.router.routes[1].app is not inner)
    bridge.sessions.dropped("abc")
    ack2, _result = bridge.handle(flush, [{"gen": gen}], "abc", GradioRequest())
    r.check("a fresh state after a drop is answered as reset", ack2["session"]["reset"] is True, str(ack2.get("session")))
    ack3, _result = bridge.handle(flush, [page_state], "xyz", None)
    r.check("a request with no gr.Request still answers, with nothing found",
            ack3["session"] == {"closed": False, "reset": False, "silent_s": None}, str(ack3.get("session")))
    ack4, _result = bridge.handle(json.dumps({"op": "nonsense", "request_id": "r2", "channel_id": "c" * 32}), [page_state], "abc", None)
    r.check("a refused request carries it too", isinstance(ack4.get("session"), dict) and ack4.get("ok") is False, str(ack4))


def service_lookup_checks(r: Results) -> None:
    """Finding the one service from a thread with no browser session.

    THE REGRESSION THIS EXISTS FOR SHIPPED, AND FAILED EXACTLY HERE.

    Every unattended job went ``enhanced -> ensuring_wangp -> failed``, and the
    child said SERVICE_UNAVAILABLE, because the lookup could not have worked:

    *   ``service_for`` is ``state.service if isinstance(state, SharedState)
        else None``. A control-plane thread has no state by definition, so
        calling it with None - or with nothing - is answered None every time.
        It is the right answer to the question it was asked.
    *   the global cannot be requested either: Wan2GP *copies* each requested
        global onto the plugin object while the plugins load, which is before
        the generator tab is built and therefore before the service exists.
        The snapshot would be None for the life of the process.
    *   and the singleton was looked for under the wrong names in the wrong
        module. It is ``_deepy_hybrid`` in ``wgp`` - and ``wgp`` is
        ``__main__``, because Wan2GP is launched as ``python wgp.py``.

    So what is asserted is the shape of the real thing: a factory that only
    answers for a SharedState, a service that exists only as a module global,
    and a module reachable solely as ``__main__``. And that nothing here
    imports: an ``import wgp`` would not find that module object, it would run
    the whole application again inside its own process.
    """
    compatibility, _compose, _control, _execution, _ledger, protocol = _modules()
    gen = {"queue": [], "in_progress": False}
    service = FakeService(gen)

    class SharedState(dict):
        """Wan2GP's own: the service hangs off the state, defaulting to None."""

        service = None

    def service_for(state):
        return state.service if isinstance(state, SharedState) else None

    holder = types.ModuleType("pretend-wgp")
    compat = _compat({"service_for": service_for})
    r.check("with no session state the factory cannot answer, and that is not a bug",
            service_for(None) is None and compat.service() is None)

    imported = []
    real_import = importlib.import_module

    def watched(name, *args, **keywords):
        imported.append(name)
        return real_import(name, *args, **keywords)

    saved = sys.modules.get("__main__")
    try:
        holder._deepy_hybrid = service
        sys.modules["__main__"] = holder
        importlib.import_module = watched
        found = compat.service()
    finally:
        importlib.import_module = real_import
        if saved is not None:
            sys.modules["__main__"] = saved
    r.check("the process-wide service is found where Wan2GP actually keeps it",
            found is service, str(found))
    r.check("and nothing imported wgp to get it, which would run the application twice",
            not any(name in ("wgp", "__main__") for name in imported), str(imported))

    # Read live, not once: the global is assigned while the generator tab is
    # built, so anything that cached an answer from plugin-load time caches
    # None forever.
    holder2 = types.ModuleType("pretend-wgp-2")
    saved = sys.modules.get("__main__")
    try:
        sys.modules["__main__"] = holder2
        empty = compat.service()
        holder2._deepy_hybrid = service
        late = compat.service()
    finally:
        if saved is not None:
            sys.modules["__main__"] = saved
    r.check("a service that does not exist yet reads as absent, and as present once it does",
            empty is None and late is service)

    # A state is still the better answer when a caller has one: that is the
    # browser path, and it must not have been broken by fixing the other.
    state = SharedState()
    state.service = service
    r.check("a caller holding a real session state still gets the service through the factory",
            _compat({"service_for": service_for}).service(state) is service)

    r.check("something merely named like the service is refused",
            compatibility.Compatibility.is_service(types.SimpleNamespace(start_generation=1, command=2)) is False)


def diagnosis_checks(r: Results) -> None:
    """When there is no service, the log says what was looked at.

    SERVICE_UNAVAILABLE is one code for several different worlds - a factory
    that answered None, a module that is not loaded, a global not assigned
    yet, a build that has none of this - and a line carrying only the code
    sends whoever reads it guessing. It sent me guessing twice.
    """
    compatibility, _compose, _control, _execution, _ledger, _protocol = _modules()
    compat = _compat({})
    text = compat.service_diagnosis()
    r.check("with no service the diagnosis names what was looked for rather than repeating the code",
            text and "SERVICE_UNAVAILABLE" not in text and compatibility.SERVICE_FACTORY in text, text[:200])
    r.check("and says which modules were not even loaded, which is the usual answer",
            "not loaded" in text, text[:200])

    gen = {"queue": [], "in_progress": False}
    service = FakeService(gen)
    holder = types.ModuleType("pretend-wgp")
    holder._deepy_hybrid = service
    saved = sys.modules.get("__main__")
    try:
        sys.modules["__main__"] = holder
        found = compat.service_diagnosis()
    finally:
        if saved is not None:
            sys.modules["__main__"] = saved
    r.check("and once there is one it says so in a word rather than listing everything again",
            found == "resolved", found)

    # Found by shape, not by name: a rename upstream must not read as "this
    # build has no generation service", which is indistinguishable from the
    # real thing and would send the next reader down the same hole.
    renamed = types.ModuleType("pretend-wgp-renamed")
    renamed._deepy_generation_arbiter = service
    saved = sys.modules.get("__main__")
    try:
        sys.modules["__main__"] = renamed
        by_shape = compat.service()
    finally:
        if saved is not None:
            sys.modules["__main__"] = saved
    r.check("a service under a name this bridge has never heard of is still found",
            by_shape is service, str(by_shape))

    # But the class is not the instance, and the class is importable from the
    # same namespaces - so a sweep by shape has to tell them apart.
    only_class = types.ModuleType("pretend-wgp-class-only")
    only_class.HybridService = FakeService
    saved = sys.modules.get("__main__")
    try:
        sys.modules["__main__"] = only_class
        nothing = compat.service()
    finally:
        if saved is not None:
            sys.modules["__main__"] = saved
    r.check("a class that merely looks like the service is not mistaken for one",
            nothing is None, str(nothing))


def learned_service_checks(r: Results) -> None:
    """The service is taken from a page, because that is where it can be had.

    THIS IS THE ONE THAT MATTERS, AND IT IS THE DOCUMENTED SEAM.

    ``service_for(state)`` is how Wan2GP itself reaches the service, and it
    needs a session state. A control-plane thread has none and never will, so
    every other route this class knows is archaeology around that fact: a
    global injected as a snapshot before the service exists, a module
    attribute whose name and module move between revisions. Two rounds of
    field reports were spent on that archaeology being wrong.

    Every request from a page carries a state. The first one resolves the
    service and the process keeps it, which is safe because it is a
    process-wide singleton - the thing the pages share - rather than anything
    belonging to the tab that happened to hand it over.
    """
    compatibility, _compose, _control, _execution, _ledger, _protocol = _modules()
    gen = {"queue": [], "in_progress": False}
    service = FakeService(gen)

    class SharedState(dict):
        service = None

    def service_for(state):
        return state.service if isinstance(state, SharedState) else None

    state = SharedState()
    state["gen"] = gen
    state.service = service

    compat = _compat({"service_for": service_for, "get_gen_info": lambda s: s["gen"]})
    r.check("a control-plane thread cannot find it on its own, which is the whole problem",
            compat.service() is None)
    compat.remember_service(state)
    r.check("a page hands it over, and the control plane has it from then on",
            compat.service() is service, str(compat.service()))
    r.check("and can reach the shared queue through it, with no state of its own",
            compat.shared_gen() is gen, str(compat.shared_gen()))

    # A build that hands over the state but not the factory: the attribute is
    # the attribute, and service_for is one line that reads it.
    bare = _compat({"get_gen_info": lambda s: s["gen"]})
    bare.remember_service(state)
    r.check("a build that did not hand over service_for still yields the service from the state",
            bare.service() is service)

    # And nothing is learned from a state that has none, rather than a stale
    # or wrong object being kept.
    empty = _compat({"service_for": service_for})
    empty.remember_service(SharedState())
    r.check("a page whose state carries no service teaches nothing", empty.service() is None)


def manifest_checks(r: Results) -> None:
    """The version the operator is shown is the version that is running.

    ``plugin_info.json`` is what the setup panel reports as "bridge status:
    ok - version X", and it is the only thing an operator has to tell them
    whether an update actually landed in the WanGP install. It had drifted a
    release behind the code, so a install carrying the new control surface
    still announced itself as the old one - which is worse than no version at
    all, because it answers the question wrongly.
    """
    compatibility, _compose, _control, _execution, _ledger, protocol = _modules()
    manifest = json.loads((BRIDGE_DIR / "plugin_info.json").read_text(encoding="utf-8"))
    r.check("the manifest's version is the code's version",
            manifest["version"] == compatibility.BRIDGE_VERSION, f"{manifest['version']} vs {compatibility.BRIDGE_VERSION}")
    r.check("and so is the bridge version beside it",
            manifest["bridge_version"] == compatibility.BRIDGE_VERSION, str(manifest["bridge_version"]))
    r.check("the manifest names the postMessage protocol this build actually speaks",
            manifest["bridge_protocol"] == protocol.PROTOCOL, f"{manifest['bridge_protocol']} vs {protocol.PROTOCOL}")
    r.check("and declares the two things this release added",
            {"unattended_execution", "settings_flush"} <= set(manifest["capabilities"]), str(manifest["capabilities"]))


def output_path_checks(r: Results) -> None:
    """What a finished job reports, and the one property it must have.

    Wan2GP's ``save_path`` is a setting. An install that has not repointed
    it holds the relative string ``outputs``, so the paths in the shared
    record are relative to the directory Wan2GP runs in - which is this
    process's directory and nothing else's. Forge reads them from its own
    process, stats them against its own working directory, finds nothing,
    and drops the file from the gallery as one the user deleted. The job
    succeeded, the video is on the disk, and the tab says "Nothing yet".

    So the shape asserted here is not "some paths": it is that every path
    that leaves this process is absolute.
    """
    _compatibility, _compose, _control, execution, ledger, _protocol = _modules()
    runner, _service, _gen, _book = _executor()
    scratch = pathlib.Path(tempfile.mkdtemp(prefix="minipaint-outputs-"))
    (scratch / "outputs").mkdir()
    fresh = scratch / "outputs" / "fresh.mp4"
    fresh.write_bytes(b"video")
    stale = scratch / "outputs" / "stale.mp4"
    stale.write_bytes(b"older")
    os.utime(str(stale), (1_000.0, 1_000.0))
    since = 2_000.0
    os.utime(str(fresh), (since + 10, since + 10))

    was = os.getcwd()
    try:
        # The bridge lives inside Wan2GP, so its working directory IS the
        # one a relative save_path resolves against. That is the whole
        # reason it can settle this and Forge cannot.
        os.chdir(str(scratch))
        found = runner._outputs({"file_list": [os.path.join("outputs", "fresh.mp4")]}, since)
        r.check("a path Wan2GP recorded relative to its own directory comes back absolute",
                found == [str(fresh.resolve())], str(found))
        absolute = runner._outputs({"file_list": [str(fresh)]}, since)
        r.check("one that was already absolute is unchanged", absolute == [str(fresh.resolve())], str(absolute))
        both = runner._outputs({"file_list": [os.path.join("outputs", "fresh.mp4"), str(fresh)]}, since)
        r.check("and two spellings of one file are one file, not two",
                both == [str(fresh.resolve())], str(both))
        old_file = runner._outputs({"file_list": [os.path.join("outputs", "stale.mp4")]}, since)
        r.check("a file that was already there before the job is still not the job's", old_file == [], str(old_file))
    finally:
        os.chdir(was)
        shutil.rmtree(str(scratch), ignore_errors=True)


def run() -> Results:
    r = Results("wangp control")
    identity_checks(r)
    inheritance_checks(r)
    manifest_shape_checks(r)
    output_path_checks(r)
    ledger_checks(r)
    idempotency_checks(r)
    surface_checks(r)
    media_checks(r)
    client_id_checks(r)
    model_checks(r)
    cancellation_checks(r)
    unavailable_checks(r)
    flush_checks(r)
    session_checks(r)
    service_lookup_checks(r)
    manifest_checks(r)
    diagnosis_checks(r)
    learned_service_checks(r)
    return r


if __name__ == "__main__":
    raise SystemExit(0 if run().report() else 1)
