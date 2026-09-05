"""Chooses the Canvas frontend. Exactly one of them is ever mounted.

The choice is made here, once, while the UI is being constructed - not at
runtime - because the two editors are different component trees. Switching
without rebuilding the UI would leave two canvases, two sets of listeners and
two copies of the image in the page, which is why the setting asks for a
Reload UI.
"""

from __future__ import annotations

import traceback

import gradio as gr

from . import settings
from .legacy import legacy_ui

TAB_LABEL = "Mini Paint"
TAB_ID = "minipaint"


def _legacy_tab(warning: str = ""):
    with gr.Blocks(analytics_enabled=False) as blocks:
        if warning:
            gr.Markdown(warning, elem_id="forge_canvas_fallback_warning")
        legacy_ui.create_ui()
    return blocks


def _touch_tab():
    # Imported here so a missing dependency costs the redesign rather than the
    # tab: the legacy editor needs nothing but Gradio.
    from .touch import ui as touch_ui

    with gr.Blocks(analytics_enabled=False) as blocks:
        touch_ui.create_ui()
    return blocks


def on_ui_tabs():
    """Build the one Canvas tab. Its label and id never change."""
    if settings.use_old_ui():
        print("MiniPaint: mounting the legacy miniPaint editor (Old UI is on).")
        return [(_legacy_tab(), TAB_LABEL, TAB_ID)]

    try:
        return [(_touch_tab(), TAB_LABEL, TAB_ID)]
    except Exception:
        traceback.print_exc()
        print(
            "MiniPaint: the touch Canvas failed to initialise; "
            "loading the legacy miniPaint editor instead."
        )
        return [
            (
                _legacy_tab(
                    "**Canvas redesign failed to initialize; legacy miniPaint was "
                    "loaded.** The traceback is in the WebUI console. Settings -> "
                    "miniPaint / Canvas -> *Use Old UI (legacy miniPaint)* makes "
                    "this the deliberate choice."
                ),
                TAB_LABEL,
                TAB_ID,
            )
        ]
