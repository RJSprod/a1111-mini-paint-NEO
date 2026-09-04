"""Legacy miniPaint UI: the editor in an iframe, plus its Forge bridge hooks.

Behaviour here is deliberately unchanged from the version this redesign was
built on. The parent-frame half of the bridge lives in ``javascript/main.js``
and only ever runs when this frontend has mounted, because it waits for the
iframe to call ``a1111minipaint.onload()``.
"""

from __future__ import annotations

import html
import os.path

import gradio as gr

from modules.shared import opts

from ..paths import get_asset_url, root_path, write_config_file
from ..transfer_log import announce_send_log


def get_controlnet_unit_count() -> int:
    """Number of ControlNet units the host exposes.

    Forge Neo reads the same option name for its rewritten ControlNet, so this
    stays correct without pinning a Forge version.
    """
    try:
        return int(opts.data.get("control_net_unit_count", 3))
    except (AttributeError, TypeError, ValueError):
        return 3


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


def create_ui() -> None:
    announce_send_log("legacy miniPaint")
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
