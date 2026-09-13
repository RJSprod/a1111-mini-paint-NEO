"""One managed WanGP per machine: the lock that says which Forge owns it.

Within one Forge server the runtime is a singleton behind a lock, so two
browsers, or two clicks, produce one child. Two Forge *servers* on one
machine are a different case: each has its own singleton, and until now each
would have started its own WanGP on the same GPU. This module is the one
piece of shared state between them: a small file under the runtime
directory that names the Forge process holding a managed WanGP, so the
second server refuses to start another and says why
(``WANGP_ALREADY_MANAGED``).

What the file holds, and deliberately nothing else: the **Forge server's**
pid, that process's start time where the platform exposes one, and when the
lock was taken. Never the child's pid, never a port, never an instance id or
a secret - section 9's list is unchanged. The pid is used for exactly one
thing, asking the operating system whether that process still exists
(signal 0 on POSIX, a query handle on Windows); nothing is ever signalled,
terminated or connected to on the strength of it. A lock left behind by a
Forge that crashed is therefore harmless: its pid is dead, or belongs to an
unrelated process whose start time does not match, and the next claim
removes it.

Refusing rather than attaching is a choice. Attaching a second Forge to the
first one's WanGP would need the bridge secret shared through a file, and
that secret is the one thing this integration promises never to persist.
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import time
import typing

from . import errors, journal
from .config import runtime_dir
from .errors import IntegrationError

LOCK_NAME = "wangp.lock.json"
SCHEMA = 1

#: The only keys the file may carry. A test holds the writer to this list.
ALLOWED_KEYS = frozenset({"schema", "forge_pid", "forge_start", "created_at", "host"})

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def lock_path() -> pathlib.Path:
    return runtime_dir() / LOCK_NAME


def process_start(pid: int) -> str:
    """When a process started, as an opaque token, or "" where unknown.

    Linux exposes it in ``/proc/<pid>/stat`` (field 22, clock ticks since
    boot). It is what tells a pid that was recycled after a crash from the
    process that took the lock; elsewhere the pid alone has to do.
    """
    try:
        text = pathlib.Path(f"/proc/{int(pid)}/stat").read_text(encoding="ascii", errors="replace")
    except Exception:
        return ""
    # The command name is in parentheses and may itself contain spaces or
    # parentheses; everything after the last ')' is the fixed-order rest.
    tail = text.rsplit(")", 1)[-1].split()
    # tail[0] is field 3 (state); field 22 is tail[19].
    return tail[19] if len(tail) > 19 else ""


def pid_alive(pid: typing.Any) -> bool:
    """Whether the operating system knows this pid. Never a signal that does anything."""
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return False
    if number <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, number)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == _STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(number, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists; it is somebody else's
    except OSError:
        return False
    return True


def read() -> typing.Optional[dict]:
    """The lock as written, or None when there is none or it is unreadable."""
    try:
        raw = json.loads(lock_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    try:
        pid = int(raw.get("forge_pid"))
    except (TypeError, ValueError):
        return None
    return {
        "schema": raw.get("schema"),
        "forge_pid": pid,
        "forge_start": str(raw.get("forge_start") or ""),
        "created_at": str(raw.get("created_at") or ""),
        "host": str(raw.get("host") or ""),
    }


def _is_ours(record: typing.Mapping[str, typing.Any]) -> bool:
    return int(record.get("forge_pid") or 0) == os.getpid()


def _is_live(record: typing.Mapping[str, typing.Any]) -> bool:
    """Whether the Forge the record names is still that Forge."""
    pid = int(record.get("forge_pid") or 0)
    if not pid_alive(pid):
        return False
    recorded = str(record.get("forge_start") or "")
    current = process_start(pid)
    if recorded and current and recorded != current:
        return False  # the pid was recycled by something else
    return True


def _remove() -> None:
    try:
        lock_path().unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def holder() -> typing.Optional[dict]:
    """The *other* live Forge holding the lock, or None.

    None also when the lock is ours, absent, or stale - and a stale one is
    removed on the way, so nothing accumulates.
    """
    record = read()
    if record is None:
        if lock_path().exists():
            _remove()  # unreadable: nobody can be relying on it
        return None
    if _is_ours(record):
        return None
    if not _is_live(record):
        journal.note("runtime", "a stale WanGP lock was left by a Forge that is gone; removed")
        _remove()
        return None
    return record


def claim() -> dict:
    """Take the lock for this Forge, or refuse with ``WANGP_ALREADY_MANAGED``."""
    other = holder()
    if other is not None:
        raise IntegrationError(
            errors.WANGP_ALREADY_MANAGED,
            f"another Forge (pid {other['forge_pid']}, since {other.get('created_at') or 'unknown'}) holds the WanGP lock",
        )
    record = {
        "schema": SCHEMA,
        "forge_pid": os.getpid(),
        "forge_start": process_start(os.getpid()),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "host": socket.gethostname()[:64],
    }
    path = lock_path()
    temporary = path.with_suffix(path.suffix + ".part")
    try:
        temporary.write_text(json.dumps(record, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    except Exception as error:
        # A lock that cannot be written is not a reason to refuse to run
        # WanGP: the single-server case, which is nearly every install, does
        # not need it. Said in the journal, once.
        journal.note("runtime", f"the WanGP lock could not be written ({type(error).__name__}); continuing without it")
        try:
            temporary.unlink()
        except Exception:
            pass
    return record


def release() -> bool:
    """Drop the lock, but only when it is this Forge's. Returns whether it was."""
    record = read()
    if record is None or not _is_ours(record):
        return False
    _remove()
    return True


def sweep() -> bool:
    """At startup: remove a lock nobody live is holding. Returns whether one went."""
    record = read()
    if record is None:
        if lock_path().exists():
            _remove()
            return True
        return False
    if _is_ours(record) or not _is_live(record):
        _remove()
        return True
    return False


def status() -> dict:
    """What a diagnostics report may say: who holds it, never more."""
    record = read()
    if record is None:
        return {"state": "none", "summary": "none"}
    if _is_ours(record):
        return {"state": "ours", "summary": "held by this Forge"}
    if _is_live(record):
        return {"state": "other", "summary": f"held by another Forge on {record.get('host') or 'this machine'} since {record.get('created_at') or 'unknown'}"}
    return {"state": "stale", "summary": "stale (left by a Forge that is gone)"}


__all__ = ["ALLOWED_KEYS", "LOCK_NAME", "claim", "holder", "lock_path", "pid_alive", "process_start", "read", "release", "status", "sweep"]
