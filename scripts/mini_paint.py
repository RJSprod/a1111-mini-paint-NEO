import datetime
import html
import os.path
import pathlib
import threading
import typing
import urllib.parse

import gradio as gr

from modules import script_callbacks
from modules.shared import opts

try:
    root_path = pathlib.Path(__file__).resolve().parents[1]
except NameError:
    import inspect

    root_path = pathlib.Path(inspect.getfile(lambda: None)).resolve().parents[1]


def get_asset_url(
    file_path: pathlib.Path, append: typing.Optional[dict[str, str]] = None
) -> str:
    if append is None:
        append = {"v": str(os.path.getmtime(file_path))}
    else:
        append = append.copy()
        append["v"] = str(os.path.getmtime(file_path))
    return f"/file={file_path.absolute()}?{urllib.parse.urlencode(append)}"


def write_config_file() -> pathlib.Path:
    config_dir = root_path / "downloads"
    config_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    config_path = config_dir / "config.json"
    if not config_path.exists():
        # get_asset_url() stats this file, so it has to exist.
        config_path.write_text("{}", encoding="utf-8")
    return config_path


SEND_LOG_ROUTE = "/minipaint/log"
SEND_LOG_PATH = root_path / "logs" / "send-log.txt"
SEND_LOG_MAX_BYTES = 1_000_000
SEND_LOG_MAX_STEPS = 200
SEND_LOG_MAX_LINE = 500
_send_log_lock = threading.Lock()


def _clean(value: typing.Any, limit: int = SEND_LOG_MAX_LINE) -> str:
    """One printable line. The browser is the only writer, but it is still input."""
    text = value if isinstance(value, str) else repr(value)
    text = "".join(character if character.isprintable() else " " for character in text)
    return text[:limit]


def format_send_entry(record: typing.Any) -> str:
    """Render one transfer the way it will appear in the log file."""
    if not isinstance(record, dict):
        record = {}

    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    destination = _clean(record.get("destination", "unknown destination"), 120)
    outcome = _clean(record.get("outcome", "unknown outcome"), 300)

    lines = [f"[{stamp}] {destination} -> {outcome}"]

    steps = record.get("steps")
    if isinstance(steps, list):
        for step in steps[:SEND_LOG_MAX_STEPS]:
            lines.append(f"    {_clean(step)}")

    return "\n".join(lines) + "\n\n"


def append_send_log(record: typing.Any) -> pathlib.Path:
    """Append one transfer to logs/send-log.txt, rotating it when it grows."""
    entry = format_send_entry(record)

    with _send_log_lock:
        SEND_LOG_PATH.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        if SEND_LOG_PATH.exists() and SEND_LOG_PATH.stat().st_size > SEND_LOG_MAX_BYTES:
            SEND_LOG_PATH.replace(SEND_LOG_PATH.with_name("send-log.previous.txt"))
        with SEND_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(entry)

    return SEND_LOG_PATH


def announce_send_log() -> None:
    """Create logs/send-log.txt as soon as the extension loads.

    An empty folder is a useless answer to "where is the log?": if the file is
    missing after a restart, the extension being loaded is what to doubt, so
    the file says when it was loaded and where transfers will appear.
    """
    try:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        append_send_log(
            {
                "destination": "extension loaded",
                "outcome": f"ready - transfers from miniPaint will be logged here from {stamp}",
                "steps": [f"log route: POST {SEND_LOG_ROUTE}"],
            }
        )
    except OSError as error:
        print(f"MiniPaint: could not write {SEND_LOG_PATH}: {error}")


def on_app_started(_demo, app) -> None:
    """Let the editor write its transfer log next to the extension.

    A plain route rather than a Gradio event on purpose: this has to keep
    working when what failed *is* the Gradio round trip.
    """

    # The parameter has to be annotated: without the type, FastAPI reads
    # "request" as a required query parameter and answers every POST with 422,
    # which is what happened - the log route was there and refused everything.
    from fastapi import Request

    @app.post(SEND_LOG_ROUTE)
    async def minipaint_log(request: Request):
        try:
            record = await request.json()
        except Exception:
            return {"ok": False, "error": "expected a JSON body"}
        try:
            path = append_send_log(record)
        except OSError as error:
            return {"ok": False, "error": str(error)}
        return {"ok": True, "path": str(path)}

    print(f"MiniPaint: transfer log route ready at {SEND_LOG_ROUTE}, writing to {SEND_LOG_PATH}")


def get_controlnet_unit_count() -> int:
    """Number of ControlNet units the host exposes.

    Forge Neo reads the same option name for its rewritten ControlNet, so this
    stays correct without pinning a Forge version.
    """
    try:
        return int(opts.data.get("control_net_unit_count", 3))
    except (AttributeError, TypeError, ValueError):
        return 3


def on_ui_tabs():
    with gr.Blocks(analytics_enabled=False) as blocks:
        create_ui()
    return [(blocks, "Mini Paint", "minipaint")]


def get_bundle_stamp() -> str:
    """Build stamp of the miniPaint bundle.

    index.html keeps the same mtime when only dist/bundle.js is rebuilt, so
    without this the browser can go on running a cached build of the editor
    after the extension is updated.
    """
    bundle = root_path / "miniPaint" / "dist" / "bundle.js"
    try:
        return str(os.path.getmtime(bundle))
    except OSError:
        return ""


def create_ui():
    announce_send_log()
    cn_max = get_controlnet_unit_count()
    config = {
        "config": get_asset_url(write_config_file()) or "",
        "bundle": get_bundle_stamp(),
    }
    html_url = get_asset_url(root_path / "miniPaint" / "index.html", config)
    with gr.Tabs(elem_id="a1111minipaint_main"):
        gr.HTML(
            f"""
            <iframe id="a1111minipaint_iframe" src="{html.escape(html_url)}" onload = "a1111minipaint.onload()"></iframe>
            """
        )
        gr.Markdown("Original: [miniPaint](https://github.com/viliusle/miniPaint)")
        gr.Textbox(
            str(cn_max), visible=False, elem_id="a1111minipaint_controlnet_max"
        )


script_callbacks.on_ui_tabs(on_ui_tabs)
script_callbacks.on_app_started(on_app_started)
