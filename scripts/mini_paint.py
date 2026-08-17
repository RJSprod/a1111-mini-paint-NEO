import html
import os.path
import pathlib
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
