"""What the WanGP process said, on disk, next to the extension.

``logs/wangp-log.txt`` beside ``logs/send-log.txt``, so the two questions a
WanGP problem splits into - "what did the transfer do?" and "what did the
process say?" - have an answer in the same folder, and neither of them is the
WebUI console, which belongs to Forge and is gone the moment it scrolls.

This file exists because of where WanGP's output used to go. A child process
writes to a pipe; the pipe was drained onto Forge's console and into a ring
buffer in memory, which meant the only durable copy of a failed generation was
whatever the user had managed to select with a mouse before restarting. A
model that fails to load at 3am, a plugin that silently does not, a CUDA error
forty minutes into a run - all of them were, in practice, unrecoverable.

Everything written here is scrubbed first, by the same pass the console and
the tab use, so this file is safe to attach to an issue without reading it
line by line beforehand. That is the point of it: a log a user has to audit
before sharing is a log a user will not share. What that costs is the exact
text of a prompt and the exact name of an output, and neither has ever been
the reason a generation failed.

Bounded the way ``send_log`` is bounded, and for the same reason: WanGP is
chatty, a progress bar is a line, and nobody's disk should fill up because a
log had no ceiling. At the cap the file becomes ``wangp-log.previous.txt`` and
a new one starts, so there is always at least one full run of history and
never more than two files.

Nothing here raises. The writer is the thread draining the child's pipe, and
that thread stopping is WanGP freezing mid-generation the next time the pipe
fills - so a failure to log is swallowed, deliberately and completely.
"""

from __future__ import annotations

import datetime
import pathlib
import threading
import typing

from .. import scrub
from ..paths import root_path

#: Beside the transfer log, in the folder the .gitignore already excludes.
LOG_DIR = root_path / "logs"
LOG_PATH = LOG_DIR / "wangp-log.txt"
PREVIOUS_PATH = LOG_DIR / "wangp-log.previous.txt"

#: Two megabytes holds a long start plus a generation's worth of progress
#: lines. Twice that on disk once the previous file is counted.
MAX_BYTES = 2_000_000

#: Longer than the journal's line, because this one is not rendered inside a
#: tab and a truncated traceback frame is a frame nobody can use.
MAX_LINE = 1000

_lock = threading.Lock()
#: Set once the first line is written, so the header is not repeated and a
#: filesystem that refuses us is not asked again on every line of output.
_state: typing.Dict[str, typing.Any] = {"opened": False, "broken": False}


def use_log_dir(directory: typing.Optional[typing.Any]) -> None:
    """Point the log at ``directory``; None restores the extension's own.

    The seam exists so the rotation, the bounds and the scrubbing can be
    exercised without writing into the folder a real install logs to. Nothing
    in the extension calls it.
    """
    global LOG_DIR, LOG_PATH, PREVIOUS_PATH
    LOG_DIR = pathlib.Path(str(directory)) if directory is not None else root_path / "logs"
    LOG_PATH = LOG_DIR / "wangp-log.txt"
    PREVIOUS_PATH = LOG_DIR / "wangp-log.previous.txt"
    with _lock:
        _state["opened"] = False
        _state["broken"] = False


def _stamp() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _write(text: str) -> None:
    """Append, rotating at the cap. Caller holds the lock."""
    LOG_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    try:
        if LOG_PATH.stat().st_size > MAX_BYTES:
            LOG_PATH.replace(PREVIOUS_PATH)
    except OSError:
        pass  # not there yet, which is the common case on the first line
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(text)


def note(source: typing.Any, message: typing.Any) -> None:
    """One line, scrubbed, timestamped and tagged with who said it.

    ``source`` is the half of the integration the line came from - ``wangp``
    for the child's own words, ``runtime``, ``proxy``, ``browser`` - and it is
    the first thing worth knowing when a log is read back, because the four
    halves fail in completely different ways.
    """
    try:
        line = scrub.private(message, limit=MAX_LINE)
        if not line.strip():
            return
        entry = f"[{_stamp()}] {str(source)[:16]:<16} {line}\n"
        with _lock:
            if _state["broken"]:
                return
            try:
                if not _state["opened"]:
                    _state["opened"] = True
                    _write(
                        f"\n[{_stamp()}] {'session':<16} "
                        "--- Forge started; WanGP output below is scrubbed of prompts, "
                        "filenames and paths ---\n"
                    )
                _write(entry)
            except Exception:
                # Every failure, not just OSError: a path the platform refuses
                # outright raises ValueError, and that is the same permanent
                # condition as a full disk. One complaint on the console and
                # then silence - whatever will not take this line will not take
                # the next thousand either, and the drain thread cannot afford
                # to find out one line at a time.
                _state["broken"] = True
                scrub.console(f"the WanGP log could not be written to {LOG_PATH.name}", "MiniPaint WanGP:")
    except Exception:  # pragma: no cover - a log is never worth an exception
        pass


def begin(instance_id: typing.Any = "", detail: typing.Any = "") -> None:
    """Mark the start of one WanGP run, so the file reads as a series of them.

    Only the first eight characters of the instance id, which is enough to
    tell two runs apart in a file and not enough to be the id - that one is
    never written down anywhere, and a log is not the place to start.
    """
    short = str(instance_id or "")[:8] or "unknown"
    note("session", f"--- WanGP run {short} starting --- {scrub.line(detail)}".rstrip())


def end(instance_id: typing.Any = "", detail: typing.Any = "") -> None:
    short = str(instance_id or "")[:8] or "unknown"
    note("session", f"--- WanGP run {short} ended --- {scrub.line(detail)}".rstrip())


def tail(limit: int = 40) -> typing.List[str]:
    """The last few lines, for the diagnostics report. Never raises."""
    try:
        text = LOG_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = [line.rstrip() for line in text.splitlines()]
    return [line for line in lines if line][-max(0, int(limit)) :]


def path() -> str:
    """Where the log is, for a screen that has to tell somebody."""
    return str(LOG_PATH)
