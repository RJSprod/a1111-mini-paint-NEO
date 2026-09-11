"""The WanGP child process: one at a time, ours, and only ever on loopback.

Everything hard about this module is ownership. Forge is a long-lived process
that gets clicked at from several browser tabs at once, and WanGP is a large
GPU application that takes minutes to load a model and does not enjoy being
started twice against the same install. So the rules here are narrow on
purpose:

*one* child, because every transition goes through a single lock and the whole
start - port choice, launch, health - happens inside it, so a second click
cannot interleave and produce a second WanGP; it waits, finds a runtime that
is already READY and gets that one back.

*ours*, because we keep the handle, the pid, the process group (POSIX) or the
job object (Windows), the root we launched from and the port we asked for, and
we only ever terminate that. There is no name-based kill anywhere in this file
and there must never be one: a user may legitimately be running their own
standalone WanGP or any other Python, and `pkill python` would take it with
us. A pid on its own is not ownership either - pids get recycled - which is
why the Popen handle is held unreaped until we are done with it.

*loopback*, because the backend binds 127.0.0.1 and the browser never learns
the port. The command line is built here and checked here for the flags that
would undo that; the proxy is the only thing that talks to the child, and it
reads the port from this object rather than from anything a request carried.

The GPU is decided before anything is launched. The config stores a physical
UUID, and if that exact card is not present the answer is a failure, not
another card: a start that silently moved to the wrong GPU would be worse than
no start at all.

State that belongs to one run - the instance id, the port, the pid, the bridge
secret - lives on this object and nowhere else. Nothing here is persisted, and
the instance id is thrown away the moment the child stops, so an image that
was prepared for the old WanGP cannot be applied to the new one.
"""

from __future__ import annotations

import collections
import ctypes
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import typing

from .. import scrub
from . import discovery, errors, journal, process_log
from .config import DEFAULT_PROXY_PATH, runtime_dir
from .errors import IntegrationError

# -- the state machine of section 11.6 --------------------------------------
STOPPED = "STOPPED"
STARTING = "STARTING"
READY = "READY"
STOPPING = "STOPPING"
CRASHED = "CRASHED"
INCOMPATIBLE = "INCOMPATIBLE"
REINIT_REQUIRED = "REINIT_REQUIRED"

STATES = (STOPPED, STARTING, READY, STOPPING, CRASHED, INCOMPATIBLE, REINIT_REQUIRED)

#: The environment variables the child is told about itself with. Named here
#: so the bridge plugin and the diagnostics report can agree on the spelling.
ENV_INSTANCE_ID = "MINIPAINT_WANGP_INSTANCE_ID"
ENV_HANDOFF_ROOT = "MINIPAINT_WANGP_HANDOFF_ROOT"
ENV_BRIDGE_SECRET = "MINIPAINT_WANGP_BRIDGE_SECRET"

#: The address the backend is allowed to bind. Not a default, not a setting.
LOOPBACK = "127.0.0.1"

#: Flags that would take the backend off loopback or open a tunnel to it.
#: ``command_line`` refuses to return a command containing any of them, which
#: is cheap insurance against a later edit adding one by habit.
FORBIDDEN_ARGUMENTS = ("--listen", "--share", "--open-browser", "--server-name=0.0.0.0")

#: Allocator settings Forge exports into its own process - ``--cuda-malloc``
#: writes ``PYTORCH_CUDA_ALLOC_CONF=backend:cudaMallocAsync`` and
#: ``--expandable-segments`` writes ``PYTORCH_ALLOC_CONF`` (Forge Neo,
#: ``modules_forge/cuda_malloc.py``). WanGP started on its own never sees
#: either; a child that inherits them runs WanGP on Forge's torch tuning. The
#: first one is a crash, not a slowdown: under cudaMallocAsync an allocation
#: made while a CUDA graph is being captured is a graph node with no memory
#: behind it yet, Triton refuses the address ("cannot be accessed from Triton
#: (cpu tensor?)"), and the abort that follows takes the whole process. WanGP's
#: prompt enhancer captures a graph on first use, which is exactly where it
#: died. Dropped for the reason PYTHONPATH is: Forge's torch is not the
#: child's torch.
FORGE_ALLOCATOR_VARIABLES = ("PYTORCH_CUDA_ALLOC_CONF", "PYTORCH_ALLOC_CONF")

#: How many times a start may lose the port race before it gives up. The
#: window between "this port was free" and "the child bound it" is small but
#: real, and the only honest fix is to try again on a different number.
PORT_ATTEMPTS = 3

#: WanGP imports torch and builds its Gradio app before it listens, which on a
#: cold filesystem is minutes rather than seconds. The generous ceiling is the
#: point: a start that is merely slow must not be reported as a failure.
LISTENER_TIMEOUT = 300.0
#: Once the socket accepts, the HTTP answer is close behind.
PROBE_TIMEOUT = 30.0
POLL_INTERVAL = 0.25

#: How long a terminate is given before the group is killed outright.
STOP_TIMEOUT = 20.0

#: The child's stderr, kept for the crash screen. Bounded twice - lines and
#: line length - because the reader has to keep draining the pipe no matter
#: what the child writes into it.
STDERR_TAIL_LINES = 200
STDERR_MAX_LINE = 2000

#: WanGP's own console output stays visible; swallowing a traceback because it
#: happened to arrive on the pipe we read would make every support request
#: harder than it needs to be. It is echoed *scrubbed*, which is a change from
#: relaying it verbatim and is the whole point: WanGP prints the prompt and the
#: output filename on the way through, and the WebUI console is the surface
#: most likely to be photographed, streamed or pasted.
ECHO_CHILD_STDERR = True

_LOG_PREFIX = "MiniPaint WanGP:"

#: What an operating system says when something else already has the port.
_ADDRESS_IN_USE_MARKERS = (
    "address already in use",
    "address in use",
    "only one usage of each socket address",
    "errno 98",
    "errno 48",
    "10048",
    "cannot find empty port",
)


def _text(value: typing.Any) -> str:
    return str(value).strip() if value is not None else ""


def _section(config: typing.Any, name: str) -> dict:
    """One block of a Config, tolerating a plain dict for the same shape.

    Tests and the setup wizard both hold half-built configs; asking for
    attributes that may not be there is not worth an exception.
    """
    if isinstance(config, dict):
        value = config.get(name)
    else:
        value = getattr(config, name, None)
    return value if isinstance(value, dict) else {}


def _root_of(config: typing.Any) -> str:
    if isinstance(config, dict):
        return _text(config.get("wangp_root"))
    return _text(getattr(config, "wangp_root", ""))


def _gpu_uuid_of(config: typing.Any) -> str:
    return _text(_section(config, "gpu").get("uuid"))


# ------------------------------------------------------------ pure inputs --


def build_environment(
    config: typing.Any,
    port: int,
    instance_id: str,
    secret: str,
    environ: typing.Optional[typing.Mapping[str, str]] = None,
    handoff_root: typing.Any = "",
) -> typing.Dict[str, str]:
    """The exact environment the child is started in.

    Deliberately a pure function of its arguments: the interesting part of a
    launch is which GPU the child can see, and that has to be assertable in a
    test without a driver, a WanGP or a process.

    ``CUDA_VISIBLE_DEVICES`` is *set*, never inherited and never appended to.
    Forge has its own device selection and may well have exported one already;
    inheriting it would hand WanGP Forge's card, and appending would leave two
    devices visible where the user chose one. The value is the physical UUID
    rather than an index, so a driver reshuffle or a reboot cannot repoint it.

    Forge's allocator variables are dropped for the same reason its
    interpreter variables are: they describe Forge's torch, and WanGP launched
    by hand never has them. Everything else in Forge's environment is
    inherited, so the child sees the same PATH and drivers a terminal
    would give it.

    ``GRADIO_ROOT_PATH`` is the mount point the proxy serves, and the two
    Gradio server variables are pinned to the same loopback address and port
    as the command line - not because the arguments are insufficient, but so
    that an inherited ``GRADIO_SERVER_NAME=0.0.0.0`` in Forge's own
    environment cannot contradict them.
    """
    source = os.environ if environ is None else environ
    child: typing.Dict[str, str] = {str(key): str(value) for key, value in source.items()}

    # Forge's interpreter is not the child's interpreter. Carrying these over
    # points the WanGP environment at Forge's libraries, which is either an
    # import error or, worse, a working import of the wrong torch.
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONEXECUTABLE"):
        child.pop(key, None)
    # A share tunnel is not something the child may inherit permission for.
    for key in ("GRADIO_SHARE", "GRADIO_ROOT_PATH"):
        child.pop(key, None)
    # Forge's torch is not the child's torch either: its allocator flags are
    # exported as environment variables and would otherwise arrive here as if
    # they were WanGP's own. See FORGE_ALLOCATOR_VARIABLES for the crash.
    for key in FORGE_ALLOCATOR_VARIABLES:
        child.pop(key, None)

    child["CUDA_VISIBLE_DEVICES"] = _gpu_uuid_of(config)
    child["GRADIO_ROOT_PATH"] = DEFAULT_PROXY_PATH
    child["GRADIO_SERVER_NAME"] = LOOPBACK
    child["GRADIO_SERVER_PORT"] = str(int(port))
    child["PYTHONNOUSERSITE"] = "1"
    child["PYTHONIOENCODING"] = "utf-8"
    # Line-buffered stderr, so the crash tail holds the traceback rather than
    # whatever was flushed before the interpreter died.
    child["PYTHONUNBUFFERED"] = "1"

    child[ENV_INSTANCE_ID] = _text(instance_id)
    child[ENV_HANDOFF_ROOT] = _text(handoff_root)
    child[ENV_BRIDGE_SECRET] = _text(secret)
    return child


def _forbidden_arguments(command: typing.Sequence[str]) -> typing.List[str]:
    lowered = [str(item).strip().lower() for item in command]
    found = [item for item in lowered if item in FORBIDDEN_ARGUMENTS]
    if LOOPBACK not in lowered and "--server-name" in lowered:
        found.append("--server-name without 127.0.0.1")
    return found


def command_line(
    config: typing.Any,
    port: int,
    interpreter: typing.Any = None,
    conda: typing.Any = None,
) -> typing.List[str]:
    """The argv that starts WanGP, and nothing else.

    Two shapes, decided by the strategy the setup wizard *proved* rather than
    guessed: the environment's own interpreter, or ``conda run -p <prefix>``
    for the installs that genuinely need activation scripts. ``wgp.py`` is
    relative because the child is started with ``cwd=<wangp_root>``, which is
    where WanGP looks for its own configuration, models and outputs.

    ``--server-name 127.0.0.1`` and ``--server-port`` are the entire network
    surface. ``--listen`` and ``--share`` are never added - they would put the
    backend on a LAN address or behind a public tunnel with no authentication
    in front of it - and neither is ``--open-browser``, which would fling a
    second, unproxied window at the user. The check below is not decoration:
    it fails the launch rather than trusting a later reader of this function
    to remember the rule.
    """
    strategy = _text(_section(config, "runtime").get("launch_strategy"))
    prefix = _text(_section(config, "runtime").get("prefix"))

    tail = [
        "wgp.py",
        "--server-name",
        LOOPBACK,
        "--server-port",
        str(int(port)),
    ]

    if strategy == discovery.CONDA_RUN:
        if not prefix:
            raise IntegrationError(errors.RUNTIME_MISSING, "conda_run was persisted without a prefix")
        # --no-capture-output because the default buffers the child's output
        # until it exits, which for a server means never - no console output,
        # and a full pipe waiting to happen.
        binary = _text(conda) or discovery.conda_binary() or "conda"
        command = [binary, "run", "--no-capture-output", "-p", prefix, "python", "-u"] + tail
    else:
        found = interpreter if interpreter else discovery.interpreter_for(_section(config, "runtime"))
        if not found:
            raise IntegrationError(errors.RUNTIME_MISSING, f"no interpreter under {prefix or '(no prefix)'}")
        command = [str(found), "-u"] + tail

    forbidden = _forbidden_arguments(command)
    if forbidden:
        raise IntegrationError(errors.PROCESS_START_FAILED, f"refusing to launch with {', '.join(forbidden)}")
    return command


def free_port() -> int:
    """A loopback port nobody held a moment ago.

    Binding to port 0 and reading back what the kernel chose is the only way
    to ask the question, and it leaves the familiar gap between the answer and
    the child's own bind. ``start`` closes that gap with retries rather than
    pretending it is not there.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((LOOPBACK, 0))
            return int(probe.getsockname()[1])
    except OSError as error:
        raise IntegrationError(errors.LOOPBACK_BIND_FAILED, f"could not bind {LOOPBACK}:0 ({error})")


# ------------------------------------------------------------- the launch --


def _default_probe(port: int, timeout: float = 5.0) -> typing.Tuple[bool, str]:
    """One HTTP request to the child, on loopback, from this process only.

    Section 11.2 step 17: a listening socket is not a working application.
    This is the base-page half of the health gate; the fuller probe - an
    asset, the root path, a bridge round trip - belongs to ``proxy.probe``,
    which can only run once the route is mounted.
    """
    try:
        import httpx
    except Exception as error:  # pragma: no cover - httpx ships with Forge
        return False, f"httpx is unavailable ({error})"

    url = f"http://{LOOPBACK}:{int(port)}/"
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=False)
    except Exception as error:
        return False, str(error)[:200]
    if response.status_code >= 400:
        return False, f"the base page answered {response.status_code}"
    return True, f"base page {response.status_code}"


def _windows_creation_flags(breakaway: bool) -> int:
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    if breakaway:
        flags |= 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
    return flags


def _default_spawn(command: typing.Sequence[str], cwd: str, env: typing.Mapping[str, str]) -> typing.Any:
    """Start the child in its own group (POSIX) or process group (Windows).

    The group is the whole point. WanGP starts workers of its own, and a
    terminate that reaches only the process we can see leaves them holding the
    GPU. On POSIX ``start_new_session`` gives us a session whose pgid is the
    child's pid, and that pgid is the only thing ``stop`` ever signals. On
    Windows the group flag is the equivalent, and ``_attach_job_object`` adds
    the part that survives Forge being killed outright.
    """
    keywords: typing.Dict[str, typing.Any] = {
        "cwd": cwd,
        "env": dict(env),
        "stdin": subprocess.DEVNULL,
        # Both streams, into one pipe. stdout used to stay attached to Forge's
        # console on the grounds that WanGP's progress belongs where the user
        # is already looking - but it is also where WanGP says which plugins
        # it loaded, and that line is the answer to the commonest failure
        # there is: a bridge that is installed, enabled, and silent. The
        # reader echoes what it reads, so the console still gets it. A pipe
        # nobody reads fills up and stops the child dead, so it is drained on
        # a thread.
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "close_fds": True,
    }

    if os.name == "nt":
        # Breakaway first: if Forge itself was started inside somebody's job
        # object, the child cannot join ours until it leaves theirs. Some jobs
        # forbid that, and the launch fails with a plain OSError, so the
        # second attempt is the same launch without it.
        try:
            return subprocess.Popen(list(command), creationflags=_windows_creation_flags(True), **keywords)
        except OSError:
            return subprocess.Popen(list(command), creationflags=_windows_creation_flags(False), **keywords)

    return subprocess.Popen(list(command), start_new_session=True, **keywords)


# -- Windows job object, through ctypes ---------------------------------------
# Inert on POSIX; defined at module level so the shape is readable rather than
# rebuilt inside a function. pywin32 is not a dependency of the WebUI, so the
# handful of calls needed are made against kernel32 directly, and every one of
# them is allowed to fail: a machine where the job cannot be created still
# gets the tracked-handle termination, just not the kill-on-Forge-exit part.

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobBasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JobExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _attach_job_object(pid: int) -> typing.Optional[int]:
    """Put the child in a job that dies when Forge does, or return None.

    Section 49.5: a Popen handle alone does not promise that grandchildren go
    away when the host exits. A job object with KILL_ON_JOB_CLOSE does, and
    the handle is held for the life of the runtime precisely so that closing
    it - deliberately, or because the process ended - is what collects the
    tree.
    """
    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        limits = _JobExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job, _JOB_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            kernel32.CloseHandle(job)
            return None

        handle = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, int(pid))
        if not handle:
            kernel32.CloseHandle(job)
            return None
        try:
            if not kernel32.AssignProcessToJobObject(job, handle):
                kernel32.CloseHandle(job)
                return None
        finally:
            kernel32.CloseHandle(handle)
        return int(job)
    except Exception:
        return None


def _close_job_object(job: typing.Optional[int]) -> None:
    """Kill the job's remaining processes, then let the handle go."""
    if not job or os.name != "nt":
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.TerminateJobObject(job, 1)
        kernel32.CloseHandle(job)
    except Exception:
        pass


class _Child:
    """Everything that makes one running process *ours*.

    Section 11.7's list, in one object, so that ``stop`` has no way to reach a
    process this runtime did not start. Nothing in here is ever written to
    disk: a persisted pid is a loaded gun pointed at whatever inherits the
    number after a reboot.
    """

    def __init__(self, process: typing.Any, instance_id: str, root: str, port: int) -> None:
        self.process = process
        self.pid = int(getattr(process, "pid", 0) or 0)
        self.instance_id = instance_id
        self.root = root
        self.port = int(port)
        self.started_at = time.time()
        self.stopping = False
        self.pgid: typing.Optional[int] = None
        self.job: typing.Optional[int] = None
        self.stderr_tail: typing.Deque[str] = collections.deque(maxlen=STDERR_TAIL_LINES)
        self.exit_code: typing.Optional[int] = None

        if os.name != "nt":
            try:
                self.pgid = os.getpgid(self.pid)
            except Exception:
                self.pgid = None
        else:
            self.job = _attach_job_object(self.pid)

    def alive(self) -> bool:
        poll = getattr(self.process, "poll", None)
        if poll is None:
            return False
        try:
            return poll() is None
        except Exception:
            return False

    def tail(self) -> typing.List[str]:
        return list(self.stderr_tail)


def _drain(child: _Child) -> None:
    """Read the child's stderr forever, so the pipe cannot fill.

    A pipe with nobody on the reading end holds about 64 KB and then blocks
    the writer - which here means WanGP freezing mid-generation for a reason
    nobody would ever guess. The reads are capped so a progress bar that never
    emits a newline becomes several bounded chunks instead of one unbounded
    string.
    """
    # Both streams arrive on stdout now; stderr is kept as a fallback for a
    # spawn seam (a test's fake process) that still hands one over.
    stream = getattr(child.process, "stdout", None) or getattr(child.process, "stderr", None)
    if stream is None:
        return
    try:
        while True:
            raw = stream.readline(STDERR_MAX_LINE)
            if not raw:
                break
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            # Scrubbed once, here, at the only place the child's words enter
            # this process. Everything downstream - the crash tail, the tab's
            # console, the log file, the WebUI console, the sentence the error
            # screen quotes - reads what this produces, so there is no path by
            # which a raw line reaches a screen and no second writer to
            # remember to fix. WanGP is a third-party application printing a
            # user's prompts and output filenames; relaying that verbatim is
            # what put them in the console in the first place.
            body = line.rstrip("\r\n")
            stripped = scrub.line(body, limit=STDERR_MAX_LINE)
            child.stderr_tail.append(stripped)
            # The child's own words, in the one place a user can read them.
            # This is where WanGP says which plugins it loaded, and it is the
            # only place it says so.
            if stripped.strip():
                journal.note("wangp", stripped)
            if ECHO_CHILD_STDERR:
                try:
                    # The child's own line ending, kept: a chunk that arrived
                    # without one is a progress bar mid-update, and adding a
                    # newline to it turns one line into hundreds.
                    sys.stderr.write(stripped + line[len(body) :])
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _looks_like_address_in_use(lines: typing.Sequence[str]) -> bool:
    haystack = "\n".join(lines[-40:]).lower()
    return any(marker in haystack for marker in _ADDRESS_IN_USE_MARKERS)


def _state_for_code(code: str) -> str:
    """Which state a failure leaves the runtime in.

    Three outcomes worth telling apart, because the tab offers a different
    button for each: the setup on disk no longer describes reality
    (Reinitialize), the WanGP we can see is not one we can drive
    (Incompatible, and intelligent send stays off), or the process is simply
    not running (Restart).
    """
    if code in (
        errors.BRIDGE_MISSING,
        errors.BRIDGE_DISABLED,
        errors.BRIDGE_VERSION_MISMATCH,
        errors.BRIDGE_COMPONENT_INCOMPATIBLE,
    ):
        return INCOMPATIBLE
    if code in errors.REINIT_CODES:
        return REINIT_REQUIRED
    if code == errors.PROCESS_EXITED:
        return CRASHED
    return STOPPED


class Runtime:
    """The one WanGP child, and the lock that keeps it one.

    Every state transition happens with ``_lock`` held, and so does the whole
    of ``start`` - not just the bookkeeping around it. That is what makes "two
    simultaneous browser events produce one child" structural rather than
    likely: the second caller cannot begin until the first has finished and
    published its result, at which point it sees READY and takes it.

    The readable fields are deliberately plain attributes and ``snapshot`` does
    not take the lock, because a start can legitimately hold it for minutes
    while torch loads, and a health panel that blocked behind that would be
    worse than one that is a quarter of a second stale.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.state = STOPPED
        self.instance_id = ""
        self.backend_port = 0
        self.error_code = ""
        self.error_detail = ""
        self._child: typing.Optional[_Child] = None
        self._secret = ""
        self._last_instance_id = ""

    # -- reading ------------------------------------------------------------

    def is_running(self) -> bool:
        child = self._child
        return bool(child is not None and child.alive())

    def bridge_secret(self) -> str:
        """The current run's secret. Never persisted, never in a snapshot."""
        return self._secret

    def uptime(self) -> float:
        child = self._child
        return max(0.0, time.time() - child.started_at) if child is not None else 0.0

    def snapshot(self) -> dict:
        """What a screen may show. No secret, no path, no port, no pid.

        The port is left out on purpose rather than by omission: the browser
        must not learn it, and the surest way to keep that true is for the
        number never to be in anything that gets rendered.
        """
        code = self.error_code
        return {
            "state": self.state,
            "instance_id": self.instance_id,
            "running": self.state == READY,
            "port_bound": bool(self.backend_port),
            "error_code": code,
            "message": errors.message(code) if code else "",
            "needs_setup": self.state == REINIT_REQUIRED or code in errors.REINIT_CODES,
            "can_restart": self.state in (STOPPED, CRASHED, INCOMPATIBLE) and code not in errors.REINIT_CODES,
            "uptime": round(self.uptime(), 1),
        }

    def health(self) -> dict:
        """A snapshot, reconciled with the operating system, plus the tail.

        Reconciled because a child can die between two clicks and nothing
        would have asked the watcher thread about it yet; a health panel that
        reports READY for a process that is gone is exactly the "pretend the
        iframe is usable" that section 11.8 forbids.

        The lock is taken without waiting, though, because ``start`` holds it
        for the whole launch - a model-loading WanGP can take minutes - and
        this is what the tab repaints from. Blocking here would mean the one
        state a user most needs to see, STARTING, is the one state the tab
        cannot draw. When the lock is busy there is also nothing to reconcile:
        a start in progress is not a READY child that quietly died, so the
        plain snapshot is both what we can get and what is true.
        """
        held = self._lock.acquire(blocking=False)
        try:
            child = self._child
            if held and self.state == READY and child is not None and not child.alive():
                self._note_exit(child)
            report = self.snapshot()
            report["pid_tracked"] = bool(child is not None and child.pid)
            report["stderr_tail"] = child.tail() if child is not None else []
            report["job_object"] = bool(child is not None and child.job)
            report["reconciled"] = held
            return report
        finally:
            if held:
                self._lock.release()

    # -- transitions --------------------------------------------------------

    def _fail(self, code: str, detail: str = "") -> IntegrationError:
        self.error_code = code
        self.error_detail = _text(detail)
        self.state = _state_for_code(code)
        journal.note("runtime", f"{code}: {_text(detail)} (state {self.state})")
        return IntegrationError(code, detail)

    def mark(self, code: str, detail: str = "") -> None:
        """Record a failure another module proved - a bridge handshake, say.

        The bridge and the setup wizard find out things this module cannot
        (that the plugin is the wrong version, that the root moved) and the
        tab reads one state, not three. Nothing is terminated here; the caller
        stops the child if it wants it stopped.
        """
        with self._lock:
            self._fail(code, detail)

    def _clear_instance(self) -> None:
        """Forget the id in-flight sends were prepared against.

        A send carries the instance id it was prepared for. Dropping the id
        the moment the process is gone is what turns "applied to the wrong
        WanGP" into an ordinary rejected send.
        """
        previous = self.instance_id
        if previous:
            self._last_instance_id = previous
        self.instance_id = ""
        self.backend_port = 0
        if previous:
            _invalidate_bridge(previous)

    def _note_exit(self, child: _Child) -> None:
        """The child ended without being asked to."""
        try:
            child.exit_code = child.process.poll()
        except Exception:
            child.exit_code = None
        detail = f"WanGP exited with code {child.exit_code}"
        self._clear_instance()
        self._fail(errors.PROCESS_EXITED, detail)
        self.state = CRASHED
        process_log.end(child.instance_id, detail)
        scrub.console(detail, _LOG_PREFIX)

    # -- start --------------------------------------------------------------

    def start(
        self,
        config: typing.Any,
        spawn: typing.Optional[typing.Callable[..., typing.Any]] = None,
        probe: typing.Optional[typing.Callable[..., typing.Tuple[bool, str]]] = None,
        gpus: typing.Optional[typing.List[typing.Any]] = None,
        handoff_root: typing.Any = None,
        timeout: typing.Optional[float] = None,
    ) -> "Runtime":
        spawn = spawn or _default_spawn
        probe = probe or _default_probe
        deadline_span = LISTENER_TIMEOUT if timeout is None else float(timeout)

        with self._lock:
            if self.is_running() and self.state == READY:
                # The second of two simultaneous clicks lands here. One child.
                return self

            # A half-started or crashed child from a previous attempt is ours
            # to clean up before another one is launched.
            if self._child is not None:
                self._terminate(self._child, STOP_TIMEOUT)
                self._child = None

            root = self._validate(config, gpus)

            self.state = STARTING
            self.error_code = ""
            self.error_detail = ""

            # Everything from here to the end of the launch reports through
            # _fail. Without this, an unexpected error - the kernel refusing a
            # port, a thread that cannot be created - would escape with the
            # state left at STARTING and no code to explain it, and the tab
            # would sit on "starting WanGP" until somebody pressed again.
            try:
                return self._launch(config, spawn, probe, root, handoff_root, deadline_span)
            except IntegrationError:
                raise
            except Exception as error:
                raise self._fail(errors.PROCESS_START_FAILED, f"{type(error).__name__}: {error}")

    def _launch(self, config, spawn, probe, root, handoff_root, deadline_span) -> "Runtime":
        """The launch itself, with the lock already held. See ``start``."""
        if True:
            instance_id = secrets.token_hex(16)
            secret = secrets.token_urlsafe(32)
            root_text = str(root)
            handoff = _text(handoff_root) or _handoff_root()

            # Tell the scrubber what these two directories are before anything
            # is launched. Almost every path WanGP prints is under one of them,
            # and a labelled path - <wangp>/outputs/*.mp4 - is the difference
            # between a log that says where a file went and one that says
            # nothing at all. Registered per launch rather than at import,
            # because the setup can be repointed at another install.
            scrub.register_root("wangp", root_text)
            scrub.register_root("runtime", _text(_section(config, "runtime").get("prefix")))
            process_log.begin(instance_id, f"root {scrub.line(root_text)}")

            last_detail = ""
            for attempt in range(PORT_ATTEMPTS):
                port = free_port()
                command = command_line(config, port)
                environment = build_environment(
                    config, port, instance_id, secret, handoff_root=handoff
                )

                journal.note("runtime", f"launch attempt {attempt + 1}: {' '.join(command)}")
                try:
                    process = spawn(command, root_text, environment)
                except Exception as error:
                    journal.note("runtime", f"spawn failed: {type(error).__name__}: {error}")
                    raise self._fail(errors.PROCESS_START_FAILED, f"{command[0]}: {error}")

                child = _Child(process, instance_id, root_text, port)
                threading.Thread(
                    target=_drain, args=(child,), name="minipaint-wangp-stderr", daemon=True
                ).start()

                ok, retryable, detail = self._await_health(child, probe, deadline_span)
                if ok:
                    self._child = child
                    self.instance_id = instance_id
                    self.backend_port = port
                    self._secret = secret
                    self.state = READY
                    self.error_code = ""
                    self.error_detail = ""
                    threading.Thread(
                        target=self._watch, args=(child,), name="minipaint-wangp-watch", daemon=True
                    ).start()
                    return self

                # Only ever the child this loop started, never a search for
                # something that looks like WanGP.
                self._terminate(child, STOP_TIMEOUT)
                last_detail = detail
                if not retryable:
                    raise self._fail(errors.PROCESS_START_FAILED, detail)
                scrub.console(f"port {port} did not work out ({detail}); trying another", _LOG_PREFIX)

            raise self._fail(errors.PORT_IN_USE, f"{PORT_ATTEMPTS} ports tried; last: {last_detail}")

    def _validate(self, config: typing.Any, gpus: typing.Optional[typing.List[typing.Any]]) -> str:
        """Everything that must be true before a process is created.

        The GPU is checked first and hardest. A missing card is not a
        degraded start on another one - the whole reason the config holds a
        physical UUID is that "some other GPU" is never the right answer - so
        nothing has been launched by the time this fails.
        """
        uuid = _gpu_uuid_of(config)
        if not uuid or discovery.find_gpu(uuid, devices=gpus) is None:
            raise self._fail(errors.GPU_UUID_MISSING, f"no device with uuid {uuid or '(unset)'}")

        root = _root_of(config)
        if not root:
            raise self._fail(errors.WANGP_ROOT_MISSING, "no WanGP root is configured")
        problems = discovery.validate_root(root)
        if problems:
            raise self._fail(problems[0], f"{root} is not a usable WanGP checkout")

        strategy = _text(_section(config, "runtime").get("launch_strategy"))
        if strategy != discovery.CONDA_RUN and discovery.interpreter_for(_section(config, "runtime")) is None:
            raise self._fail(errors.RUNTIME_MISSING, "the configured environment has no interpreter")
        return root

    def _await_health(
        self,
        child: _Child,
        probe: typing.Callable[..., typing.Tuple[bool, str]],
        span: float,
    ) -> typing.Tuple[bool, bool, str]:
        """Section 11.2, steps 15-17. Returns (ok, retryable, detail).

        Alive, then listening, then answering - in that order, because each
        one makes the next question meaningful. A child that died holding an
        "address already in use" is the port race, and the caller may try
        again on another number; a child that died for any other reason will
        die the same way on the next port, so it is reported instead.
        """
        deadline = time.time() + span

        while True:
            if not child.alive():
                lines = child.tail()
                detail = lines[-1] if lines else f"WanGP exited with code {child.exit_code}"
                return False, _looks_like_address_in_use(lines), f"the process exited during startup: {detail}"
            try:
                with socket.create_connection((LOOPBACK, child.port), timeout=1.0):
                    break
            except OSError:
                pass
            if time.time() >= deadline:
                return False, True, f"nothing accepted on {LOOPBACK}:{child.port} within {span:.0f}s"
            time.sleep(POLL_INTERVAL)

        probe_deadline = time.time() + PROBE_TIMEOUT
        detail = "the base page was never answered"
        while True:
            if not child.alive():
                return False, _looks_like_address_in_use(child.tail()), "the process exited before answering"
            try:
                ok, detail = probe(child.port)
            except Exception as error:
                ok, detail = False, str(error)[:200]
            if ok:
                return True, False, detail
            if time.time() >= probe_deadline:
                # Something is on the port and it is not the application we
                # started. Another number is worth one more try.
                return False, True, f"health mismatch on {LOOPBACK}:{child.port}: {detail}"
            time.sleep(POLL_INTERVAL)

    def _watch(self, child: _Child) -> None:
        """Notice a child that ends on its own, and say so exactly once."""
        try:
            child.process.wait()
        except Exception:
            return
        with self._lock:
            if self._child is not child or child.stopping:
                return  # we stopped it, or it has already been replaced
            self._note_exit(child)

    # -- stop ---------------------------------------------------------------

    def stop(self, timeout: float = STOP_TIMEOUT) -> None:
        with self._lock:
            child = self._child
            self._child = None
            self._clear_instance()
            self._secret = ""
            # REINIT_REQUIRED and INCOMPATIBLE describe the setup on disk, not
            # the process, so stopping does not clear them - the tab would
            # otherwise offer Restart for a config that cannot start anything.
            settled = self.state if self.state in (REINIT_REQUIRED, INCOMPATIBLE) else STOPPED
            if child is None:
                self.state = settled
                return
            self.state = STOPPING
            self._terminate(child, timeout)
            self.state = settled
            if settled == STOPPED:
                self.error_code = ""
                self.error_detail = ""

    def _terminate(self, child: _Child, timeout: float) -> None:
        """End this child's tree, and nothing else on the machine.

        There is no ``taskkill /IM python.exe`` and no ``pkill python`` here,
        and there must never be: the user may be running their own standalone
        WanGP (section 49.6 says explicitly not to touch it), a Jupyter
        kernel, or the training run of their afternoon. What gets signalled is
        the process group we created at spawn time or the job object we
        assigned the child to - both of which we can only have a handle on
        because we started it.
        """
        child.stopping = True
        process = child.process
        deadline = time.time() + max(0.5, float(timeout))

        # Whether this child is still ours to signal. Once the watcher thread
        # has waited on it the pid is free, and a process-group id is only the
        # leader's pid - so a remembered pgid can by then name a group that
        # belongs to somebody else's work. Popen.terminate() checks the same
        # thing for itself and does nothing to a reaped child, but os.killpg
        # does not, and signalling a group on the strength of a stale number is
        # the mistake section 11.7 rules out alongside killing by name.
        try:
            reaped = process.poll() is not None
        except Exception:
            reaped = True

        try:
            if os.name == "nt":
                if child.job:
                    _close_job_object(child.job)
                    child.job = None
                else:
                    process.terminate()
            elif child.pgid and not reaped:
                os.killpg(child.pgid, signal.SIGTERM)
            else:
                process.terminate()
        except (ProcessLookupError, PermissionError, OSError):
            pass
        except Exception:
            pass

        while time.time() < deadline:
            try:
                if process.poll() is not None:
                    break
            except Exception:
                break
            time.sleep(POLL_INTERVAL)

        try:
            if process.poll() is None:
                if os.name != "nt" and child.pgid:
                    os.killpg(child.pgid, signal.SIGKILL)
                else:
                    process.kill()
        except Exception:
            pass

        try:
            # Reap it. Until this returns, the pid is still ours and cannot be
            # handed to somebody else's process.
            process.wait(timeout=5.0)
        except Exception:
            pass
        try:
            child.exit_code = process.poll()
        except Exception:
            child.exit_code = None

    def restart(self, config: typing.Any, **keywords: typing.Any) -> "Runtime":
        """Stop and start as one operation, with the lock never released.

        Held across both halves so that a send arriving in the middle cannot
        find a READY runtime that is about to become a different one.
        """
        with self._lock:
            self.stop()
            return self.start(config, **keywords)


# ------------------------------------------------------------- the module --

_singleton: typing.Optional[Runtime] = None
_singleton_lock = threading.Lock()


def current() -> Runtime:
    """The one runtime. Created on first use, never replaced behind a caller."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = Runtime()
        return _singleton


def reset_for_tests() -> None:
    """Drop the singleton, stopping anything it owns first."""
    global _singleton
    with _singleton_lock:
        existing = _singleton
        _singleton = None
    if existing is not None:
        try:
            existing.stop(timeout=5.0)
        except Exception:
            pass


def _handoff_root() -> str:
    """Where the child is told to read prepared images from.

    ``handoff`` owns this directory; the fallback exists so that a failure to
    import one module of the integration cannot become a failure to start
    WanGP at all, and it computes the same path the contract defines.
    """
    try:
        from . import handoff

        return str(handoff.handoff_root())
    except Exception:
        pass
    try:
        directory = runtime_dir() / "handoff"
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        return str(directory)
    except Exception:
        # No handoff root means no intelligent send, which the bridge reports
        # for itself. It is not a reason to refuse to start WanGP.
        return ""


def _invalidate_bridge(instance_id: str) -> None:
    """Tell the session registry that everything bound to this run is stale.

    Contained on purpose: the bridge is a later phase, and a runtime that
    cannot import it must still be able to stop a process.
    """
    try:
        from . import bridge

        bridge.registry().invalidate_instance(instance_id)
    except Exception:
        pass


def build_environment_for(runtime: Runtime, config: typing.Any, port: int) -> typing.Dict[str, str]:
    """The environment for an already-identified run. Diagnostics only."""
    return build_environment(config, port, runtime.instance_id, runtime.bridge_secret(), handoff_root=_handoff_root())


def start(config: typing.Any, **keywords: typing.Any) -> Runtime:
    return current().start(config, **keywords)


def stop(timeout: float = STOP_TIMEOUT) -> None:
    current().stop(timeout)


def restart(config: typing.Any, **keywords: typing.Any) -> Runtime:
    return current().restart(config, **keywords)


def health() -> dict:
    return current().health()


def snapshot() -> dict:
    return current().snapshot()


def mark(code: str, detail: str = "") -> None:
    current().mark(code, detail)
