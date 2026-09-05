"""A startup report, written to a file, for when the UI cannot report anything.

The one thing missing whenever this extension misbehaves is what the host
actually is. A browser console is out of reach on a tablet, a WebUI console
scrolls, and a page that will not respond cannot describe itself - so this is
written to disk every time the tab is built, before anything can go wrong with
it, and it is the file to attach to a bug report.
"""

from __future__ import annotations

import datetime
import platform
import sys
import traceback
import typing

from .paths import root_path

REPORT_PATH = root_path / "logs" / "startup.txt"


def _version(module_name: str) -> str:
    try:
        module = __import__(module_name)
        return str(getattr(module, "__version__", "unknown"))
    except Exception as error:
        return f"not importable ({error})"


def _sibling_extensions() -> typing.List[str]:
    """What else is installed. A conflict is usually one of these."""
    try:
        return sorted(
            entry.name
            for entry in root_path.parent.iterdir()
            if entry.is_dir() and not entry.name.startswith(".")
        )
    except OSError:
        return []


def _gradio_components() -> str:
    try:
        import gradio as gr
    except Exception as error:  # pragma: no cover - gradio is always there
        return f"gradio not importable: {error}"
    present = [
        name
        for name in ("ImageEditor", "Brush", "Eraser", "DownloadButton", "UploadButton")
        if getattr(gr, name, None) is not None
    ]
    missing = [
        name
        for name in ("ImageEditor", "Brush", "Eraser", "DownloadButton", "UploadButton")
        if getattr(gr, name, None) is None
    ]
    return f"present: {', '.join(present) or 'none'}; missing: {', '.join(missing) or 'none'}"


def write(frontend: str, reason: str = "", error: BaseException = None) -> None:
    """Overwrite logs/startup.txt with what this launch looks like."""
    lines = [
        f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] miniPaint / Canvas",
        "",
        f"frontend mounted : {frontend}",
        f"reason           : {reason or 'the saved setting'}",
        f"python           : {platform.python_version()} on {platform.system()} {platform.release()}",
        f"gradio           : {_version('gradio')}",
        f"pillow           : {_version('PIL')}",
        f"components       : {_gradio_components()}",
        f"extension path   : {root_path}",
        "",
        "other extensions installed:",
    ]
    lines += [f"  {name}" for name in _sibling_extensions()] or ["  (none found)"]

    if error is not None:
        lines += ["", "the touch Canvas failed to build:", ""]
        lines += traceback.format_exception(type(error), error, error.__traceback__)

    try:
        REPORT_PATH.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as failure:  # pragma: no cover - read-only install
        print(f"MiniPaint: could not write {REPORT_PATH}: {failure}", file=sys.stderr)
