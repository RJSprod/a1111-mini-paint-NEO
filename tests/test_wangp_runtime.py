"""The WanGP child process, checked without a WanGP, a GPU or a spawn.

Everything section 11 promises about the runtime is a promise about things
that never happen: no second child, no other GPU, no LAN address, no process
we did not start. Those are hard to see in a running system and easy to see
here, because every place the module touches the operating system is a seam -
``spawn``, ``probe``, ``gpus``, ``handoff_root`` - and the two decisions that
matter most, the command line and the environment, are pure functions.

So the fakes are the point of this file. The spawn seam records and returns a
process that never existed; a missing-GPU start has to leave that recorder
empty, because a start that quietly moved to another card is the failure
section 41 is written to catch. The one real resource is a loopback listening
socket, which exists so the health gate has something to connect to when a
start is meant to succeed.
"""

from harness import Results, setup_path

setup_path()

import contextlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import pathlib  # noqa: E402
import socket  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

from minipaint_neo.wangp import bridge, lock, vram  # noqa: E402
from minipaint_neo.wangp import config as wangp_config  # noqa: E402
from minipaint_neo.wangp import discovery, errors, runtime  # noqa: E402
from minipaint_neo.wangp.errors import IntegrationError  # noqa: E402

GPU_UUID = "GPU-11112222-3333-4444-5555-666677778888"
FORGE_UUID = "GPU-99998888-7777-6666-5555-444433332222"


def gpu(uuid=GPU_UUID, index=0, name="NVIDIA GeForce RTX 5090"):
    return discovery.Gpu(index=index, name=name, uuid=uuid, total_mb=32768, bus_id="0000:01:00.0")


def config_for(root="", prefix="", strategy=discovery.DIRECT_PYTHON, uuid=GPU_UUID):
    """A config as a plain dict - the shape ``runtime`` documents it accepts."""
    return {
        "wangp_root": str(root),
        "runtime": {"type": discovery.VENV, "prefix": str(prefix), "display_name": "wan", "launch_strategy": strategy},
        "gpu": {"uuid": uuid},
        "integration": {"proxy_path": "/wan2gp", "auto_start": "lazy"},
    }


def fake_root(base):
    """A directory that passes ``discovery.validate_root`` and holds nothing."""
    root = pathlib.Path(base) / "WanGP"
    (root / "shared").mkdir(parents=True)
    (root / "wgp.py").write_text("# not the real thing\n", encoding="utf-8")
    return root


def fake_prefix(base):
    """An environment whose only claim to being one is a file called python."""
    prefix = pathlib.Path(base) / "env"
    folder, name = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    (prefix / folder).mkdir(parents=True)
    (prefix / folder / name).write_text("", encoding="utf-8")
    return prefix


def unused_pid():
    """A pid the system does not know, so a fake child has no process group.

    ``_Child`` asks the operating system for the group of whatever pid it is
    handed. A pid that resolves would give ``stop`` a real group to signal -
    in the worst case this test runner's own - so a fake process is only safe
    to hand over once ``getpgid`` has been seen to refuse it.
    """
    getpgid = getattr(os, "getpgid", None)
    if getpgid is None:  # Windows: the job object path, and no group to find
        return 4194303
    for candidate in range(4194303, 4193303, -1):
        try:
            getpgid(candidate)
        except Exception:
            return candidate
    raise RuntimeError("every candidate pid was in use")


class FakeStderr:
    """The child's stderr as a stream that reports when it has been drained.

    The runtime only reads the crash tail after the process is seen to have
    ended, so a fake whose exit becomes visible exactly when the drain reaches
    EOF makes "it died saying the address was in use" deterministic instead of
    a race with a background thread.
    """

    def __init__(self, lines, drained):
        self._lines = list(lines)
        self._drained = drained

    def readline(self, limit=-1):
        if self._lines:
            return self._lines.pop(0)
        self._drained.set()
        return b""

    def close(self):
        self._drained.set()


class FakeProcess:
    """A child that never existed, recording exactly what was done to it."""

    def __init__(self, pid, stderr_lines=None):
        self.pid = pid
        self.calls = []
        self.stderr = None
        self._exit = None
        self._ended = threading.Event()
        self._drained = threading.Event()
        if stderr_lines is None:
            self._drained.set()
        else:
            # It died on the way up; the exit code only becomes visible once
            # the tail that explains it has been read.
            self.stderr = FakeStderr(stderr_lines, self._drained)
            self._exit = 1
            self._ended.set()

    def poll(self):
        return self._exit if self._drained.is_set() else None

    def wait(self, timeout=None):
        self._ended.wait(30.0 if timeout is None else timeout)
        return self.poll()

    def terminate(self):
        self.calls.append("terminate")
        self.end(-15)

    def kill(self):
        self.calls.append("kill")
        self.end(-9)

    def end(self, code=0):
        if self._exit is None:
            self._exit = code
        self._drained.set()
        self._ended.set()


class Spawns:
    """The spawn seam. It records the launch and starts nothing at all."""

    def __init__(self, stderr_lines=None):
        self.calls = []
        self.processes = []
        self.delay = 0.0
        self._stderr_lines = stderr_lines
        self._lock = threading.Lock()

    def __call__(self, command, cwd, env):
        with self._lock:
            self.calls.append({"command": list(command), "cwd": str(cwd), "env": dict(env)})
        if self.delay:
            time.sleep(self.delay)
        process = FakeProcess(unused_pid(), self._stderr_lines)
        self.processes.append(process)
        return process

    def ports(self):
        return [call["command"][call["command"].index("--server-port") + 1] for call in self.calls]


class Listener:
    """A real loopback socket, so a start that should succeed can."""

    def __init__(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(8)
        self.port = int(self.socket.getsockname()[1])

    def close(self):
        with contextlib.suppress(Exception):
            self.socket.close()


@contextlib.contextmanager
def port_from(listener):
    """Make ``start`` pick the port something is actually listening on."""
    original = runtime.free_port
    runtime.free_port = lambda: listener.port
    try:
        yield
    finally:
        runtime.free_port = original


def healthy(port):
    return True, f"base page 200 on {port}"


def rebindable(port):
    """Did ``free_port`` really let the number go again?"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def failed(call):
    """Run something that must raise an IntegrationError; give back its code."""
    try:
        call()
    except IntegrationError as error:
        return error.code
    except Exception as error:  # a different exception is still a failure to report
        return f"{type(error).__name__}: {error}"
    return ""


def health_is_never_a_wait(r: Results) -> None:
    """The tab has to be able to draw STARTING while a start is happening.

    ``start`` holds the runtime lock for the whole launch, and loading a video
    model is measured in minutes. ``health`` is what every repaint of the tab
    reads, so if it waited for that lock the one state a user most needs to
    see would be the one state the tab could never show.
    """
    current = runtime.current()
    launched = threading.Event()
    release = threading.Event()

    def hold_it_like_start():
        with current._lock:
            launched.set()
            release.wait(5.0)

    holder = threading.Thread(target=hold_it_like_start, daemon=True)
    holder.start()
    try:
        r.check("the stand-in launch took the lock", launched.wait(2.0))
        began = time.monotonic()
        report = current.health()
        waited = time.monotonic() - began
        r.check("health answers while a launch holds the lock", waited < 0.5, f"{waited:.3f}s")
        r.check("it still reports a state", bool(report.get("state")))
        r.check("and says it could not reconcile", report.get("reconciled") is False)
    finally:
        release.set()
        holder.join(5.0)

    r.check("once the launch is over it reconciles again", current.health().get("reconciled") is True)


def ownership_and_failure_checks(r: Results) -> None:
    """Two ways a launch or a stop can go wrong without anyone noticing.

    A group is only ever signalled while its leader is still ours, and an
    unexpected error on the way up reports itself instead of leaving the tab
    saying "starting" for the rest of the session.
    """
    current = runtime.current()

    # A child the watcher already reaped: its pid is free, and a process-group
    # id is only the leader's pid, so the number may by now name somebody
    # else's work. Section 11.7 rules that out alongside killing by name.
    reaped = FakeProcess(pid=424242)
    reaped.end(0)
    child = runtime._Child(reaped, "instance", "/nowhere", 7999)
    child.pgid = 424242
    signalled = []
    original_killpg = getattr(runtime.os, "killpg", None)
    if original_killpg is not None:
        runtime.os.killpg = lambda pgid, sig: signalled.append((pgid, sig))
    try:
        current._terminate(child, 0.5)
    finally:
        if original_killpg is not None:
            runtime.os.killpg = original_killpg
    r.check("a reaped child's process group is never signalled", signalled == [], repr(signalled))
    r.check("the reaped child is still tidied up through its own handle", "terminate" in reaped.calls)

    # A live one is signalled exactly as before.
    alive = FakeProcess(pid=424243)
    living = runtime._Child(alive, "instance", "/nowhere", 7999)
    living.pgid = 424243
    signalled = []
    if original_killpg is not None:
        runtime.os.killpg = lambda pgid, sig: (signalled.append((pgid, sig)), alive.end(-15))[0]
        try:
            current._terminate(living, 0.5)
        finally:
            runtime.os.killpg = original_killpg
        r.check("a living child's own group still is", [pgid for pgid, _ in signalled] == [424243], repr(signalled))

    # An unexpected error mid-launch must not strand the state machine.
    runtime.reset_for_tests()
    stranded = runtime.current()

    def explode(*args, **keywords):
        raise MemoryError("no room to make a thread")

    failed = None
    # The launch resolves a handoff root, and resolving the real one would make
    # a folder in whatever checkout this suite runs in.
    with tempfile.TemporaryDirectory(prefix="minipaint-wangp-launch-") as scratch:
        wangp_config.use_config_dir(pathlib.Path(scratch) / "data")
        try:
            stranded.start(CONFIG_FOR_FAILURE, spawn=explode, probe=lambda *a, **k: (True, ""), gpus=GPUS_FOR_FAILURE)
        except Exception as error:
            failed = error
        finally:
            wangp_config.use_config_dir(None)
    r.check("an unexpected launch error is reported, not raised raw",
            isinstance(failed, IntegrationError) and failed.code == errors.PROCESS_START_FAILED, repr(failed))
    r.check("and the runtime does not sit in STARTING",
            stranded.state != runtime.STARTING, stranded.state)
    r.check("the failure has a code the tab can act on", bool(stranded.error_code))
    runtime.reset_for_tests()


def lock_checks(r: Results, base: str) -> None:
    """One managed WanGP per machine: the lock names the Forge, and only the Forge."""
    data = pathlib.Path(base) / "lock-data"
    wangp_config.use_config_dir(data)
    try:
        r.check("no lock before anyone claims it", lock.read() is None and lock.holder() is None and lock.status()["state"] == "none")
        record = lock.claim()
        written = json.loads(lock.lock_path().read_text(encoding="utf-8"))
        r.check("a claim writes the Forge pid and nothing that is never persisted",
                set(written) <= lock.ALLOWED_KEYS and written["forge_pid"] == os.getpid()
                and not any(key in written for key in ("pid", "port", "instance_id", "secret", "child_pid")), str(sorted(written)))
        r.check("the lock is ours", lock.status()["state"] == "ours" and lock.holder() is None and record["forge_pid"] == os.getpid())
        r.check("claiming again from the same Forge is fine", lock.claim()["forge_pid"] == os.getpid())

        # Another live Forge: the test runner's parent process stands in for it.
        other = os.getppid()
        foreign = dict(written, forge_pid=other, forge_start=lock.process_start(other))
        lock.lock_path().write_text(json.dumps(foreign), encoding="utf-8")
        held = lock.holder()
        code = failed(lock.claim)
        r.check("a lock held by another live Forge is seen as held", held is not None and held["forge_pid"] == other, str(held))
        r.check("and a claim is refused with WANGP_ALREADY_MANAGED", code == errors.WANGP_ALREADY_MANAGED, code)
        r.check("the status says another Forge holds it", lock.status()["state"] == "other")
        r.check("releasing somebody else's lock does nothing", lock.release() is False and lock.lock_path().is_file())

        # A dead Forge: its pid is nobody's.
        stale = dict(written, forge_pid=unused_pid(), forge_start="")
        lock.lock_path().write_text(json.dumps(stale), encoding="utf-8")
        r.check("a lock left by a dead Forge is stale", lock.status()["state"] == "stale")
        r.check("and is removed by the next claim, which succeeds", lock.claim()["forge_pid"] == os.getpid() and lock.status()["state"] == "ours")
        r.check("release removes ours", lock.release() is True and not lock.lock_path().exists())

        # A recycled pid: alive, but not the process that took the lock.
        recycled = dict(written, forge_pid=other, forge_start="1" if lock.process_start(other) != "1" else "2")
        if lock.process_start(other):
            lock.lock_path().write_text(json.dumps(recycled), encoding="utf-8")
            r.check("a live pid with another start time is a recycled pid, so the lock is stale", lock.status()["state"] == "stale")
        lock.lock_path().write_text("not json", encoding="utf-8")
        r.check("an unreadable lock is dropped", lock.holder() is None and not lock.lock_path().exists())
        lock.lock_path().write_text(json.dumps(stale), encoding="utf-8")
        r.check("sweep removes a stale lock", lock.sweep() is True and not lock.lock_path().exists() and lock.sweep() is False)

        # The runtime refuses to start a second managed WanGP on the machine.
        lock.lock_path().write_text(json.dumps(foreign), encoding="utf-8")
        blocked = runtime.Runtime()
        spawns = Spawns()
        code = failed(lambda: blocked.start(CONFIG_FOR_FAILURE, spawn=spawns, probe=healthy, gpus=GPUS_FOR_FAILURE, handoff_root=str(pathlib.Path(base) / "handoff"), timeout=1.0))
        r.check("a start while another Forge holds the lock is WANGP_ALREADY_MANAGED before any spawn",
                code == errors.WANGP_ALREADY_MANAGED and spawns.calls == [], f"{code} {spawns.calls}")
        r.check("and the tab may offer Restart, not Reinitialize",
                blocked.state == runtime.STOPPED and blocked.snapshot()["can_restart"] and not blocked.snapshot()["needs_setup"])
        r.check("the other Forge's lock is left alone", json.loads(lock.lock_path().read_text(encoding="utf-8"))["forge_pid"] == other)
        lock.lock_path().unlink()
    finally:
        with contextlib.suppress(Exception):
            lock.release()
        wangp_config.use_config_dir(None)


def vram_checks(r: Results) -> None:
    """The GPU report reads nvidia-smi's answers and never a path."""
    r.check("memory used and total are read", vram.parse_memory("1234, 24564\n") == {"used_mb": 1234, "total_mb": 24564})
    r.check("a missing answer is None", vram.parse_memory("") is None and vram.parse_memory("N/A, N/A") is None and vram.parse_memory(None) is None)
    apps = vram.parse_compute_apps("4242, 11264, /home/someone/WanGP/venv/bin/python3\n4243, [N/A], C:\\Users\\me\\wan\\python.exe\n, 5, x\n")
    r.check("compute apps carry pid, memory and the program's name only",
            apps == [{"pid": 4242, "used_mb": 11264, "name": "python3"}, {"pid": 4243, "used_mb": None, "name": "python.exe"}], str(apps))
    r.check("no path survives", "home" not in json.dumps(apps) and "Users" not in json.dumps(apps))
    r.check("amounts render for a screen", vram.mib(512) == "512 MiB" and vram.mib(11264) == "11.0 GiB" and vram.mib(None) == "unknown")

    class Completed:
        def __init__(self, stdout, returncode=0):
            self.stdout, self.returncode = stdout, returncode

    calls = []

    def runner(command, **keywords):
        calls.append(list(command))
        if "--query-gpu=memory.used,memory.total" in command:
            return Completed("2048, 24564\n")
        return Completed("7, 1536, /opt/wan/python\n")

    saved = vram.shutil.which
    vram.shutil.which = lambda name: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None
    try:
        picture = vram.snapshot(GPU_UUID, runner)
        r.check("a snapshot asks nvidia-smi for the chosen device only",
                picture["available"] and picture["memory"] == {"used_mb": 2048, "total_mb": 24564} and picture["processes"] == [{"pid": 7, "used_mb": 1536, "name": "python"}]
                and all(GPU_UUID in command for command in calls), str(calls))
        r.check("a device that is not configured is not asked about", vram.snapshot("", runner)["available"] is False)
    finally:
        vram.shutil.which = saved
    without = vram.snapshot(GPU_UUID, runner)
    r.check("without nvidia-smi the answer is honest", without["available"] is False and "nvidia-smi" in without["detail"])


def emergency_restart_checks(r: Results, config, handoff) -> None:
    """The emergency option: the tree ends, the card is read before and after, a fresh start follows."""
    class Completed:
        def __init__(self, stdout, returncode=0):
            self.stdout, self.returncode = stdout, returncode

    stage = {"phase": "before"}
    asked = []

    def runner(command, **keywords):
        asked.append(stage["phase"])
        if "--query-gpu=memory.used,memory.total" in command:
            return Completed("11776, 24564\n" if stage["phase"] == "before" else "512, 24564\n")
        if stage["phase"] == "before":
            return Completed(f"{stage['pid']}, 11264, /opt/wan/venv/bin/python\n9999, 512, /usr/bin/other\n")
        return Completed("9999, 512, /usr/bin/other\n")

    listener = Listener()
    current = runtime.Runtime()
    spawns = Spawns()
    saved_which = vram.shutil.which
    vram.shutil.which = lambda name: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None
    try:
        with port_from(listener):
            current.start(config, spawn=spawns, probe=healthy, gpus=[gpu()], handoff_root=handoff, timeout=20.0)
        r.check("(a WanGP of ours is running)", current.state == runtime.READY and len(spawns.processes) == 1)
        stage["pid"] = spawns.processes[0].pid
        first_instance = current.instance_id
        bridge.registry().hello("a" * 32, first_instance)
        r.check("(a browser session is bound to it)", bridge.registry().snapshot()["sessions"] == 1)

        def start_again(candidate):
            stage["phase"] = "after"
            with port_from(listener):
                current.start(candidate, spawn=spawns, probe=healthy, gpus=[gpu()], handoff_root=handoff, timeout=20.0)

        # The phase flips to "after" when the stop has happened: the stop is
        # observed through the tracked process being terminated.
        original_stop = current.stop

        def stop_then_flip(timeout=runtime.STOP_TIMEOUT):
            original_stop(timeout)
            stage["phase"] = "after"

        current.stop = stop_then_flip
        report = current.emergency_restart(config, gpu_uuid=GPU_UUID, runner=runner, start_async=start_again)
        current.stop = original_stop

        r.check("the restart reports success", report["ok"] is True and report["verified"] is True, json.dumps(report["steps"]))
        r.check("the old child was terminated", "terminate" in spawns.processes[0].calls)
        r.check("the tree was named by pid and nothing of it remains", report["pids"] == [stage["pid"]] and report["remaining"] == [])
        r.check("the GPU was read before and after", report["before"]["available"] and report["after"]["available"] and asked[0] == "before" and asked[-1] == "after")
        r.check("the memory that came back is counted", report["freed_mb"] == 11776 - 512, str(report["freed_mb"]))
        r.check("the report names our process on the card before the stop and not after",
                [e["pid"] for e in report["before"]["processes"]] == [stage["pid"], 9999] and [e["pid"] for e in report["after"]["processes"]] == [9999])
        r.check("the other process on the card is reported as not ours", any("does not own" in step for step in report["steps"]), json.dumps(report["steps"]))
        r.check("a fresh WanGP was started", report["started"] == "requested" and len(spawns.processes) == 2 and current.state == runtime.READY
                and current.instance_id not in ("", first_instance))
        r.check("the browser sessions of the old run were invalidated", bridge.registry().snapshot()["sessions"] == 0)
        r.check("no step names a path", not any("/opt" in step or "/usr" in step for step in report["steps"]), json.dumps(report["steps"]))

        # Without nvidia-smi the tree is still verified and the report says what it could not see.
        vram.shutil.which = lambda name: None
        stage["phase"] = "before"
        report = current.emergency_restart(config, gpu_uuid=GPU_UUID, runner=runner, start_async=start_again)
        r.check("without nvidia-smi the restart still completes and says VRAM is unverified",
                report["ok"] is True and report["freed_mb"] is None and any("unverified" in step for step in report["steps"]), json.dumps(report["steps"]))

        # Nothing running: the report says so and still starts one.
        current.stop()
        stage["phase"] = "before"
        report = current.emergency_restart(config, gpu_uuid=GPU_UUID, runner=runner, start_async=start_again)
        r.check("with no WanGP running the restart says so and starts one", any("no WanGP of ours was running" in step for step in report["steps"]) and report["started"] == "requested")
    finally:
        vram.shutil.which = saved_which
        with contextlib.suppress(Exception):
            current.stop(timeout=2.0)
        with contextlib.suppress(Exception):
            bridge.registry().invalidate_instance("")
        listener.close()


def run() -> Results:
    r = Results("wangp runtime")

    base = tempfile.TemporaryDirectory()
    root = fake_root(base.name)
    prefix = fake_prefix(base.name)
    handoff = str(pathlib.Path(base.name) / "handoff")
    config = config_for(root, prefix)

    try:
        # ---- the command line: loopback, a port, and nothing that undoes either (42)
        interpreter = discovery.interpreter_for({"prefix": str(prefix)})
        direct = runtime.command_line(config, 7999, interpreter=interpreter)
        r.check("the direct command runs wgp.py with the chosen interpreter",
                "wgp.py" in direct and direct[0] == str(interpreter))
        r.check("the direct command binds loopback",
                direct[direct.index("--server-name") + 1] == "127.0.0.1")
        r.check("the direct command carries the port",
                direct[direct.index("--server-port") + 1] == "7999")
        r.check("--listen is absent from the direct command", "--listen" not in direct)
        r.check("--share is absent from the direct command", "--share" not in direct)
        r.check("--open-browser is absent from the direct command", "--open-browser" not in direct)
        r.check("no argument of the direct command mentions 0.0.0.0",
                not any("0.0.0.0" in part for part in direct), " ".join(direct))

        conda = runtime.command_line(
            config_for(root, "/opt/conda/envs/wan", discovery.CONDA_RUN), 7999, conda="/opt/conda/bin/conda"
        )
        r.check("conda_run runs conda with the prefix",
                conda[:6] == ["/opt/conda/bin/conda", "run", "--no-capture-output", "-p", "/opt/conda/envs/wan", "python"])
        r.check("the conda command binds loopback",
                conda[conda.index("--server-name") + 1] == "127.0.0.1")
        r.check("the conda command carries the port", conda[conda.index("--server-port") + 1] == "7999")
        r.check("--listen is absent from the conda command", "--listen" not in conda)
        r.check("--share is absent from the conda command", "--share" not in conda)
        r.check("--open-browser is absent from the conda command", "--open-browser" not in conda)
        r.check("no argument of the conda command mentions 0.0.0.0",
                not any("0.0.0.0" in part for part in conda), " ".join(conda))
        r.check("the forbidden list still names all three flags",
                {"--listen", "--share", "--open-browser"} <= set(runtime.FORBIDDEN_ARGUMENTS))

        no_prefix = failed(lambda: runtime.command_line(config_for(root, "", discovery.CONDA_RUN), 7999))
        r.check("conda_run without a prefix is a runtime failure", no_prefix == errors.RUNTIME_MISSING, no_prefix)
        no_python = failed(lambda: runtime.command_line(config_for(root, pathlib.Path(base.name) / "gone"), 7999))
        r.check("a prefix with no interpreter is a runtime failure", no_python == errors.RUNTIME_MISSING, no_python)

        # ---- the environment: the chosen card, whatever the parent thought (41)
        parent = {
            "PATH": "/usr/bin",
            "CUDA_VISIBLE_DEVICES": FORGE_UUID,
            "GRADIO_ROOT_PATH": "/somewhere-else",
            "GRADIO_SHARE": "1",
            "PYTHONPATH": "/forge/lib",
            "PYTHONHOME": "/forge",
            # What Forge's --cuda-malloc and --expandable-segments export.
            "PYTORCH_CUDA_ALLOC_CONF": "backend:cudaMallocAsync",
            "PYTORCH_ALLOC_CONF": "expandable_segments:True",
        }
        child = runtime.build_environment(config, 7999, "0123456789abcdef", "the-secret", environ=parent, handoff_root=handoff)
        r.check("the chosen GPU replaces the one Forge exported",
                child["CUDA_VISIBLE_DEVICES"] == GPU_UUID, child["CUDA_VISIBLE_DEVICES"])
        r.check("Forge's card is nowhere in the child environment",
                FORGE_UUID not in json.dumps(child))
        r.check("the root path is the proxy mount", child["GRADIO_ROOT_PATH"] == "/wan2gp")
        r.check("the child is told its instance id", child[runtime.ENV_INSTANCE_ID] == "0123456789abcdef")
        r.check("the child is told the handoff root", child[runtime.ENV_HANDOFF_ROOT] == handoff)
        r.check("the child is told the bridge secret", child[runtime.ENV_BRIDGE_SECRET] == "the-secret")
        r.check("the gradio server variables agree with the command line",
                child["GRADIO_SERVER_NAME"] == "127.0.0.1" and child["GRADIO_SERVER_PORT"] == "7999")
        r.check("an inherited share permission is dropped", "GRADIO_SHARE" not in child)
        r.check("Forge's interpreter variables are dropped",
                "PYTHONPATH" not in child and "PYTHONHOME" not in child)
        r.check("Forge's allocator variables are dropped",
                "PYTORCH_CUDA_ALLOC_CONF" not in child and "PYTORCH_ALLOC_CONF" not in child)
        r.check("cudaMallocAsync reaches the child through no other name",
                "cudaMallocAsync" not in json.dumps(child))
        r.check("the allocator list names the variable --cuda-malloc exports",
                "PYTORCH_CUDA_ALLOC_CONF" in runtime.FORGE_ALLOCATOR_VARIABLES)
        r.check("everything else is inherited", child["PATH"] == "/usr/bin")
        r.check("the parent environment is left alone", parent["CUDA_VISIBLE_DEVICES"] == FORGE_UUID)

        # ---- a missing GPU starts nothing at all (41)
        absent = runtime.Runtime()
        spawns = Spawns()
        code = failed(lambda: absent.start(config, spawn=spawns, probe=healthy,
                                           gpus=[gpu(FORGE_UUID)], handoff_root=handoff))
        r.check("a GPU that is not present fails", code == errors.GPU_UUID_MISSING, code)
        r.check("nothing was spawned on another card", spawns.calls == [], str(spawns.calls))
        r.check("a missing GPU asks for setup, not a restart",
                absent.state == runtime.REINIT_REQUIRED and absent.snapshot()["needs_setup"]
                and not absent.snapshot()["can_restart"])
        empty = runtime.Runtime()
        no_driver = Spawns()
        no_devices = failed(lambda: empty.start(config, spawn=no_driver, probe=healthy, gpus=[], handoff_root=handoff))
        r.check("no devices at all fails the same way",
                no_devices == errors.GPU_UUID_MISSING and no_driver.calls == [], no_devices)
        no_uuid = runtime.Runtime()
        unconfigured = Spawns()
        unset = failed(lambda: no_uuid.start(config_for(root, prefix, uuid=""), spawn=unconfigured,
                                             probe=healthy, gpus=[gpu()], handoff_root=handoff))
        r.check("an unconfigured GPU fails before any launch",
                unset == errors.GPU_UUID_MISSING and unconfigured.calls == [], unset)

        # ---- two clicks, one child (11.6)
        listener = Listener()
        concurrent = runtime.Runtime()
        racing = Spawns()
        racing.delay = 0.15
        answers = []
        try:
            with port_from(listener):
                def click():
                    try:
                        answers.append(concurrent.start(config, spawn=racing, probe=healthy, gpus=[gpu()],
                                                        handoff_root=handoff, timeout=20.0))
                    except Exception as error:  # recorded, so a failure is visible as one
                        answers.append(error)

                threads = [threading.Thread(target=click, name=f"click-{index}") for index in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=30.0)

            r.check("two simultaneous starts spawn exactly one child", len(racing.calls) == 1, str(len(racing.calls)))
            r.check("both callers get the one runtime", answers == [concurrent, concurrent], str(answers))
            r.check("the runtime is ready", concurrent.state == runtime.READY)
            r.check("the run has an instance id", len(concurrent.instance_id) == 32)
            r.check("the runtime knows the port it asked for", concurrent.backend_port == listener.port)
            r.check("a third start reuses the same child", concurrent.start(config, spawn=racing, probe=healthy,
                                                                           gpus=[gpu()], handoff_root=handoff) is concurrent
                    and len(racing.calls) == 1)

            # What a screen may see: enough to explain itself, never the port,
            # never the secret, never a pid.
            snapshot = json.dumps(concurrent.snapshot())
            r.check("the snapshot never carries the backend port", str(listener.port) not in snapshot, snapshot)
            r.check("the snapshot never carries the bridge secret",
                    bool(concurrent.bridge_secret()) and concurrent.bridge_secret() not in snapshot)
            r.check("the snapshot never carries a pid", "pid" not in concurrent.snapshot())
            r.check("the snapshot says a port is bound without saying which",
                    concurrent.snapshot()["port_bound"] is True and concurrent.snapshot()["running"] is True)

            # ---- a child that ends on its own is CRASHED, and the run is over
            first_id = concurrent.instance_id
            racing.processes[0].end(3)
            report = concurrent.health()
            r.check("an exited child is CRASHED", report["state"] == runtime.CRASHED, report["state"])
            r.check("the instance id does not survive the child", concurrent.instance_id != first_id)
            r.check("a crash is not running", report["running"] is False and report["error_code"] == errors.PROCESS_EXITED)
            r.check("the crash tail is available to a screen", isinstance(report["stderr_tail"], list))

            with port_from(listener):
                concurrent.start(config, spawn=racing, probe=healthy, gpus=[gpu()], handoff_root=handoff, timeout=20.0)
            r.check("a restarted child is a different run",
                    len(racing.calls) == 2 and concurrent.instance_id not in ("", first_id))
            r.check("the crashed child was terminated before the new one started",
                    "terminate" in racing.processes[0].calls)

            # ---- stop touches the tracked handle and nothing else (11.7)
            tracked = racing.processes[-1]
            bystander = FakeProcess(unused_pid())
            concurrent.stop(timeout=2.0)
            r.check("stop terminates the tracked child", "terminate" in tracked.calls, str(tracked.calls))
            r.check("stop leaves every other process alone", bystander.calls == [], str(bystander.calls))
            r.check("stop clears the run", concurrent.state == runtime.STOPPED and concurrent.instance_id == ""
                    and concurrent.bridge_secret() == "" and concurrent.error_code == "")
            before = list(tracked.calls)
            concurrent.stop(timeout=2.0)
            r.check("stopping twice touches nothing", tracked.calls == before and bystander.calls == [])
        finally:
            with contextlib.suppress(Exception):
                concurrent.stop(timeout=2.0)
            listener.close()

        # ---- a port lost to somebody else: bounded retries, then PORT_IN_USE
        echo = runtime.ECHO_CHILD_STDERR
        runtime.ECHO_CHILD_STDERR = False  # the fake traceback below is not news
        try:
            losing = runtime.Runtime()
            taken = Spawns([b"OSError: [Errno 98] Address already in use\n"])
            code = failed(lambda: losing.start(config, spawn=taken, probe=healthy, gpus=[gpu()],
                                               handoff_root=handoff, timeout=5.0))
        finally:
            runtime.ECHO_CHILD_STDERR = echo
        r.check("a port race gives up as PORT_IN_USE", code == errors.PORT_IN_USE, code)
        r.check("the retries are bounded", len(taken.calls) == runtime.PORT_ATTEMPTS, str(len(taken.calls)))
        r.check("every child it started was terminated",
                all("terminate" in process.calls for process in taken.processes))
        r.check("each attempt asked for a real port",
                all(0 < int(port) < 65536 for port in taken.ports()), str(taken.ports()))
        r.check("a failed start is not ready", losing.state != runtime.READY and losing.instance_id == ""
                and losing.snapshot()["error_code"] == errors.PORT_IN_USE)

        # ---- a spawn that raises is a start failure, not a half-started runtime
        def refuse(command, cwd, env):
            raise OSError("no such file or directory")

        broken = runtime.Runtime()
        refused = failed(lambda: broken.start(config, spawn=refuse, probe=healthy, gpus=[gpu()],
                                              handoff_root=handoff, timeout=1.0))
        r.check("a spawn that fails is PROCESS_START_FAILED",
                refused == errors.PROCESS_START_FAILED and broken.state != runtime.READY, refused)

        # ---- a root that is not a WanGP checkout, and an environment that is gone
        missing_root = runtime.Runtime()
        never = Spawns()
        gone = failed(lambda: missing_root.start(config_for(pathlib.Path(base.name) / "nowhere", prefix),
                                                 spawn=never, probe=healthy, gpus=[gpu()], handoff_root=handoff))
        r.check("a root that is not WanGP fails before the launch",
                gone == errors.WANGP_ROOT_MISSING and never.calls == [], gone)

        # ---- the state machine refuses what it is not allowed to do (11.6)
        marked = runtime.Runtime()
        r.check("a fresh runtime is stopped and holds nothing",
                marked.state == runtime.STOPPED and marked.instance_id == "" and marked.backend_port == 0)
        r.check("stopping a runtime with no child is a no-op", marked.stop() is None and marked.state == runtime.STOPPED)
        marked.mark(errors.BRIDGE_MISSING)
        r.check("a bridge failure is INCOMPATIBLE", marked.state == runtime.INCOMPATIBLE)
        marked.stop()
        r.check("stopping does not clear INCOMPATIBLE",
                marked.state == runtime.INCOMPATIBLE and marked.error_code == errors.BRIDGE_MISSING)
        marked.mark(errors.WANGP_ROOT_MISSING)
        r.check("a moved root is REINIT_REQUIRED", marked.state == runtime.REINIT_REQUIRED)
        marked.stop()
        r.check("stopping does not clear REINIT_REQUIRED", marked.state == runtime.REINIT_REQUIRED)
        r.check("a reinit state offers setup rather than restart",
                marked.snapshot()["needs_setup"] and not marked.snapshot()["can_restart"])
        marked.mark(errors.PROCESS_EXITED)
        r.check("an exit is CRASHED and may be restarted",
                marked.state == runtime.CRASHED and marked.snapshot()["can_restart"])
        marked.stop()
        # The crash reason outlives the stop when there was no child left to
        # terminate, which is what the tab wants: STOPPED with "WanGP stopped
        # running" still on the screen, and Restart still offered.
        r.check("stopping a crashed runtime settles it as stopped",
                marked.state == runtime.STOPPED and marked.snapshot()["can_restart"])
        r.check("every state reached is a declared one",
                all(state in runtime.STATES for state in
                    (absent.state, losing.state, broken.state, marked.state, missing_root.state)))
        r.check("the declared states are section 11.6's",
                runtime.STATES == ("STOPPED", "STARTING", "READY", "STOPPING", "CRASHED", "INCOMPATIBLE", "REINIT_REQUIRED"))

        # ---- free_port asks the kernel and lets go again
        port = runtime.free_port()
        r.check("a free port is a real port", isinstance(port, int) and 1024 <= port <= 65535, str(port))
        r.check("the port was let go again", rebindable(port), str(port))

        # ---- the module keeps one runtime, and lets a test drop it
        r.check("current() is the one runtime", runtime.current() is runtime.current())
        runtime.reset_for_tests()
        r.check("resetting gives a fresh one", runtime.current().state == runtime.STOPPED)
        runtime.reset_for_tests()

        health_is_never_a_wait(r)

        global CONFIG_FOR_FAILURE, GPUS_FOR_FAILURE
        CONFIG_FOR_FAILURE, GPUS_FOR_FAILURE = config, [gpu(uuid=GPU_UUID)]
        ownership_and_failure_checks(r)
        lock_checks(r, base.name)
        vram_checks(r)
        emergency_restart_checks(r, config, handoff)
    finally:
        base.cleanup()

    return r


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
