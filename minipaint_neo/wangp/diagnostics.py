"""One block of text a user can paste into a bug report without reading it first.

A WanGP failure has four possible homes - the setup on disk, the child
process, the proxy, the browser session - and the questions that tell them
apart are always the same ones. Asking a user to find each answer by hand
gets a report that is missing the one field that mattered, so this module
collects all of them at once, in a fixed order, and formats them as plain
text.

Everything here is subtractive. The report is going to end up in a public
issue tracker, so it is built out of a whitelist of fields rather than a dump
with a few things removed: nothing reaches it unless a line below asks for it
by name. Paths shrink to a basename because a home directory carries a real
name and the interesting part of "D:\\AI\\Wan2GP" is "Wan2GP". The bridge
secret, the server secret, cookies and authorization headers have no line at
all - not a redacted one, because a redacted line is a line someone can be
talked into un-redacting.

Two fields section 48 asks for are deliberately reported as facts rather than
values. The child's pid and the backend loopback port appear as "tracked" and
"bound", never as numbers, because this report is rendered inside the tab and
the browser must never learn the port. The number is in the WebUI console for
whoever is standing at the machine; it is not in the thing that gets pasted.

Nothing in here raises. A diagnostic report that fails to build is the worst
possible moment to lose the report, so every field is collected inside its own
guard and an unavailable one says so.
"""

from __future__ import annotations

import datetime
import json
import re
import typing

from .. import paths
from ..send_log import SEND_LOG_PATH
from . import bridge, config, discovery, errors, protocol, runtime

#: How much of the transfer log to carry. Enough for one failed send and the
#: successful one before it, short enough to stay pasteable.
LOG_TAIL_LINES = 40

#: What a field says when it could not be collected. One phrase, so a reader
#: can tell "we did not look" from "we looked and there is nothing".
UNAVAILABLE = "unavailable"
NOT_RUN = "not run for this report"

#: Where the bridge we ship lives in this repository.
BRIDGE_SOURCE_DIR = paths.root_path / "wan2gp_bridge"

_SEPARATOR = re.compile(r"[\\/]+")


def redact_path(path: typing.Any, full: bool = False) -> str:
    """A path reduced to its last component, which is the part that means
    something to a reader and the part that identifies nobody.

    Windows and POSIX separators are both split on regardless of the machine
    the report is written on, because the report is frequently written on a
    different one than the path came from.
    """
    text = str(path or "").strip()
    if not text or full:
        return text
    parts = [part for part in _SEPARATOR.split(text) if part]
    return f".../{parts[-1]}" if parts else text


def _guard(collect: typing.Callable[[], typing.Any], fallback: str = UNAVAILABLE) -> str:
    try:
        value = collect()
    except Exception:
        return fallback
    if value is None or value == "":
        return fallback
    return str(value)


def _yes(value: typing.Any) -> str:
    return "yes" if value else "no"


# ------------------------------------------------------------- the host ----


def _forge_version() -> str:
    """Whatever this WebUI calls itself. Every family names it differently."""
    from modules import shared

    version = getattr(shared, "version", None)
    if callable(version):
        version = version()
    if not version:
        for name in ("versions_html", "git_tag", "commit_hash"):
            candidate = getattr(shared, name, None)
            if callable(candidate):
                candidate = candidate()
            if candidate:
                version = candidate
                break
    return " ".join(str(version or "").split())[:200]


def _gradio_version() -> str:
    import gradio

    return getattr(gradio, "__version__", "")


def _extension_version() -> str:
    """The checked-out commit, read from .git rather than run out of it.

    Shelling out to git from a diagnostics call is a subprocess in the UI
    thread for a string; reading the two files git would have read is the same
    answer without one.
    """
    head = (paths.root_path / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    if head.startswith("ref:"):
        reference = head.split(":", 1)[1].strip()
        head = (paths.root_path / ".git" / reference).read_text(encoding="utf-8").strip()
    return head[:12]


def shipped_bridge_version() -> str:
    """The bridge version this copy of the extension would install."""
    info_path = BRIDGE_SOURCE_DIR / discovery.BRIDGE_FOLDER_NAME / discovery.BRIDGE_INFO_NAME
    return discovery.bridge_version(json.loads(info_path.read_text(encoding="utf-8")))


# ------------------------------------------------------------ the setup ----


def _setup_fields(active: typing.Any, full_paths: bool) -> typing.List[typing.Tuple[str, str]]:
    if active is None:
        return [("setup", "never completed")]

    runtime_block = active.runtime if isinstance(active.runtime, dict) else {}
    gpu_block = active.gpu if isinstance(active.gpu, dict) else {}
    uuid = str(gpu_block.get("uuid") or "")

    # The friendly name is worth having and can only come from the machine, so
    # a missing card is reported as missing rather than left blank - that is
    # the GPU_UUID_MISSING case, and it is the first thing to check.
    try:
        device = discovery.find_gpu(uuid) if uuid else None
    except Exception:
        device = None
    gpu_name = device.label if device is not None else "not present on this machine"

    return [
        ("setup", "initialized" if active.initialized else "incomplete"),
        ("schema version", str(active.schema_version)),
        ("wangp root", redact_path(active.wangp_root, full_paths)),
        ("runtime type", str(runtime_block.get("type") or UNAVAILABLE)),
        ("runtime name", str(runtime_block.get("display_name") or UNAVAILABLE)),
        ("runtime prefix", redact_path(runtime_block.get("prefix"), full_paths)),
        ("launch strategy", str(runtime_block.get("launch_strategy") or "not probed")),
        ("gpu uuid", uuid or UNAVAILABLE),
        ("gpu present", gpu_name),
    ]


# ----------------------------------------------------------- the report ----


def fields(
    full_paths: bool = False,
    probe_result: typing.Optional[dict] = None,
) -> typing.List[typing.Tuple[str, str]]:
    """Every line of the report, as label/value pairs.

    Split from :func:`report` so that a test can assert a field is present, or
    absent, without matching against formatted text - and so the one rule that
    matters ("no secret has a line here") is checkable by listing labels.

    ``probe_result`` is the proxy's own step-by-step health, passed in rather
    than fetched: the probe is an async network call and a report must stay a
    cheap, synchronous thing that works when nothing is running.
    """
    collected: typing.List[typing.Tuple[str, str]] = [
        ("generated", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("forge", _guard(_forge_version)),
        ("gradio", _guard(_gradio_version)),
        ("extension", _guard(_extension_version)),
        ("integration protocol", str(protocol.PROTOCOL)),
        ("bridge shipped", _guard(shipped_bridge_version)),
    ]

    active = None
    try:
        active = config.load()
    except errors.IntegrationError as error:
        collected.append(("config", f"{error.code}"))
    except Exception:
        collected.append(("config", UNAVAILABLE))
    collected.extend(_setup_fields(active, full_paths))

    # The bridge answer is a code plus a detail, so it is collected on its own
    # rather than through the string guard above.
    try:
        code, detail = discovery.bridge_status(
            active.wangp_root if active is not None else "", shipped_bridge_version()
        )
        collected.append(("bridge installed", f"{code} - {detail}" if code else detail))
    except Exception:
        collected.append(("bridge installed", UNAVAILABLE))

    try:
        health = runtime.current().health()
    except Exception:
        health = {}

    collected.extend(
        [
            ("runtime state", str(health.get("state", UNAVAILABLE))),
            ("runtime instance", str(health.get("instance_id", ""))[:8] or "none"),
            ("uptime seconds", str(health.get("uptime", 0))),
            # Facts, not values: see the module docstring.
            ("child process", "tracked" if health.get("pid_tracked") else "none"),
            ("job object", _yes(health.get("job_object"))),
            ("backend port", "bound to loopback" if health.get("port_bound") else "not bound"),
            ("failure code", str(health.get("error_code") or "none")),
            ("failure message", str(health.get("message") or "")),
        ]
    )

    if probe_result:
        steps = probe_result.get("steps") or {}
        for name in probe_result.get("order") or sorted(steps):
            step = steps.get(name) or {}
            collected.append((f"proxy {name}", f"{'ok' if step.get('ok') else 'failed'} - {step.get('detail', '')}".strip(" -")))
    else:
        collected.append(("proxy health", NOT_RUN))

    try:
        sessions = bridge.registry().snapshot()
    except Exception:
        sessions = {}
    collected.append(("bridge sessions", str(sessions.get("sessions", 0))))
    for session in sessions.get("detail") or []:
        collected.append(
            (
                f"session {session.get('channel', '')}",
                "model {label} ({kind}); revision {revision}; receivers {receivers}; ready {ready}".format(
                    label=session.get("model_label") or "unknown",
                    kind=session.get("model_type") or "unknown",
                    revision=session.get("state_revision") or "none",
                    receivers=", ".join(session.get("receivers") or []) or "none",
                    ready=_yes(session.get("ready")),
                ),
            )
        )

    collected.append(("sends in flight", ", ".join(sessions.get("sends_in_flight") or []) or "none"))
    return collected


def log_tail(limit: int = LOG_TAIL_LINES) -> typing.List[str]:
    """The last few transfer-log lines, which is where a send's story is.

    Read whole and sliced rather than seeked, because the file is capped at a
    megabyte by ``send_log`` and a seek that lands mid-character in a UTF-8
    log is a bug that only shows up in someone else's language.
    """
    try:
        text = SEND_LOG_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = [line.rstrip() for line in text.splitlines()]
    return [line for line in lines if line][-max(0, int(limit)) :]


def report(full_paths: bool = False, probe_result: typing.Optional[dict] = None) -> str:
    """The whole report as text. Never raises; an empty report is a bug report
    with nothing in it, which is worse than one with holes."""
    lines = ["MiniPaint - WanGP integration diagnostics", ""]
    try:
        collected = fields(full_paths, probe_result)
        width = max((len(label) for label, _ in collected), default=20)
        lines.extend(f"{label.ljust(width)}  {value}" for label, value in collected)
    except Exception as error:  # pragma: no cover - the guards above make this rare
        lines.append(f"the report could not be completed ({type(error).__name__})")

    lines.append("")
    lines.append("recent transfer log")
    tail = log_tail()
    lines.extend(f"  {line}" for line in tail)
    if not tail:
        lines.append("  (empty)")

    lines.append("")
    lines.append(
        "Paths are shown as their last component, and no secret, cookie or "
        "authorization header appears above. The backend port and the child pid "
        "are reported as present or absent on purpose: the browser must never "
        "learn the port."
    )
    return "\n".join(lines) + "\n"
