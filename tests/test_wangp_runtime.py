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
    finally:
        base.cleanup()

    return r


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
