"""What the chosen GPU holds, as nvidia-smi reports it.

The emergency restart promises something the ordinary stop never had to:
that after it, every process of the WanGP we started is gone *and the memory
they held on the card is back*. The first half is answered by the operating
system (see ``runtime.Runtime.tree_pids``); the second can only be answered
by the driver, and nvidia-smi is the one way to ask it that every machine
with an NVIDIA card has. Read before and after, the two answers become the
report's before-and-after lines.

Best effort, never raising, and honest about not knowing: a machine without
nvidia-smi, a driver that reports per-process memory as ``N/A`` (WSL and
some Windows driver modes do), or a query that times out all come back as
"could not be verified" rather than as a number.

A process name from nvidia-smi is a full path; only its last component is
kept, because the report is rendered on a screen and pasted into bug
reports, and a path there is the account name of whoever installed WanGP.
"""

from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
import typing

QUERY_TIMEOUT = 20.0


def _int(value: typing.Any) -> typing.Optional[int]:
    text = str(value if value is not None else "").strip().strip("[]")
    if not text or text.upper() in ("N/A", "NOT SUPPORTED", "INSUFFICIENT PERMISSIONS"):
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def parse_memory(text: typing.Any) -> typing.Optional[dict]:
    """``memory.used, memory.total`` (MiB, no units) for one device, or None."""
    if not isinstance(text, str) or not text.strip():
        return None
    for row in csv.reader(io.StringIO(text), skipinitialspace=True):
        fields = [str(cell).strip() for cell in row]
        if len(fields) < 2:
            continue
        used, total = _int(fields[0]), _int(fields[1])
        if used is None or total is None:
            continue
        return {"used_mb": used, "total_mb": total}
    return None


def parse_compute_apps(text: typing.Any) -> typing.List[dict]:
    """``pid, used_memory, process_name`` rows into ``{pid, used_mb, name}``.

    ``used_mb`` is None where the driver would not say; ``name`` is the last
    path component only. A row without a readable pid is dropped.
    """
    found: typing.List[dict] = []
    if not isinstance(text, str) or not text.strip():
        return found
    for row in csv.reader(io.StringIO(text), skipinitialspace=True):
        fields = [str(cell).strip() for cell in row]
        if len(fields) < 2:
            continue
        pid = _int(fields[0])
        if pid is None or pid <= 0:
            continue
        name = fields[2] if len(fields) > 2 else ""
        name = name.replace("\\", "/").rsplit("/", 1)[-1][:80]
        found.append({"pid": pid, "used_mb": _int(fields[1]), "name": name})
    return found


def _run(arguments: typing.Sequence[str], runner: typing.Callable[..., typing.Any]) -> typing.Optional[str]:
    binary = shutil.which("nvidia-smi")
    if not binary:
        return None
    try:
        completed = runner([binary, *arguments], capture_output=True, text=True, timeout=QUERY_TIMEOUT)
    except Exception:
        return None
    if getattr(completed, "returncode", 1) != 0:
        return None
    return getattr(completed, "stdout", "") or ""


def query_memory(uuid: str, runner: typing.Callable[..., typing.Any] = subprocess.run) -> typing.Optional[dict]:
    if not str(uuid or "").strip():
        return None
    text = _run(["--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits", "-i", str(uuid).strip()], runner)
    return parse_memory(text) if text is not None else None


def query_compute_apps(uuid: str, runner: typing.Callable[..., typing.Any] = subprocess.run) -> typing.Optional[typing.List[dict]]:
    """The processes holding memory on the device, or None when unknowable."""
    if not str(uuid or "").strip():
        return None
    text = _run(["--query-compute-apps=pid,used_memory,process_name", "--format=csv,noheader,nounits", "-i", str(uuid).strip()], runner)
    return parse_compute_apps(text) if text is not None else None


def snapshot(uuid: str, runner: typing.Callable[..., typing.Any] = subprocess.run) -> dict:
    """Both questions at once, with one flag saying whether either was answered."""
    memory = query_memory(uuid, runner)
    processes = query_compute_apps(uuid, runner)
    available = memory is not None or processes is not None
    detail = ""
    if not str(uuid or "").strip():
        detail = "no GPU is configured"
    elif not shutil.which("nvidia-smi"):
        detail = "nvidia-smi is not available on this machine"
    elif not available:
        detail = "nvidia-smi did not answer"
    return {"available": available, "memory": memory, "processes": processes if processes is not None else [], "detail": detail}


def mib(value: typing.Any) -> str:
    """A memory amount for a screen: MiB below a gibibyte, GiB above."""
    number = _int(value)
    if number is None:
        return "unknown"
    if number >= 1024:
        return f"{number / 1024:.1f} GiB"
    return f"{number} MiB"


__all__ = ["QUERY_TIMEOUT", "mib", "parse_compute_apps", "parse_memory", "query_compute_apps", "query_memory", "snapshot"]
