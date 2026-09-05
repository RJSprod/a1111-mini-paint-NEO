"""Chooses the frontend. Exactly one of them is ever mounted.

The choice is made once, while the UI is being constructed, because the two
editors are different component trees: switching without a Reload UI would
leave two canvases, two sets of listeners and two copies of the image in the
page. The tab keeps the same label and id whichever editor is inside it, so
tab order, hidden-tab settings and themes see one stable tab.
"""

from __future__ import annotations

import contextlib
import traceback

import gradio as gr

from . import legacy_ui, settings

TAB_LABEL = "Mini Paint"
TAB_ID = "minipaint"

# What the touch Canvas is built out of. A host without these is not a broken
# install, it is an older Gradio - so it gets the legacy editor and a reason.
REQUIRED_COMPONENTS = ("ImageEditor", "Brush", "Eraser")


def missing_components() -> str:
    missing = [name for name in REQUIRED_COMPONENTS if getattr(gr, name, None) is None]
    if not missing:
        return ""
    version = getattr(gr, "__version__", "unknown version")
    names = ", ".join(f"gr.{name}" for name in missing)
    return f"this WebUI's Gradio ({version}) has no {names}"


@contextlib.contextmanager
def _keep_build_context():
    """Build a tab without letting a failure take the rest of the WebUI with it.

    Gradio's ``Blocks.__exit__`` does not restore the parent context when the
    body raises; it sets the render context and ``Context.root_block`` to
    None. Forge Neo calls ``on_ui_tabs`` outside any Blocks, where that is
    harmless, but a host that calls it inside its own Blocks would then build
    every later component with no root. Putting the context back costs
    nothing and keeps the fallback safe everywhere.
    """
    try:
        from gradio.context import Context, get_render_context, set_render_context

        saved_block = get_render_context()
    except ImportError:  # pragma: no cover - Gradio 3.x
        from gradio.context import Context

        get_render_context = set_render_context = None
        saved_block = getattr(Context, "block", None)

    saved_root = getattr(Context, "root_block", None)
    try:
        yield
    finally:
        if set_render_context is not None:
            set_render_context(saved_block)
        else:  # pragma: no cover - Gradio 3.x
            Context.block = saved_block
        Context.root_block = saved_root


def _legacy_tab(warning: str = ""):
    with gr.Blocks(analytics_enabled=False) as blocks:
        if warning:
            gr.Markdown(warning, elem_id="minipaint_fallback_warning")
        legacy_ui.create_ui()
    return blocks


def _canvas_tab():
    # Imported here so that a missing dependency is an ordinary ImportError
    # caught below, and never a half-built tab.
    from .canvas import ui as canvas_ui

    with gr.Blocks(analytics_enabled=False) as blocks:
        canvas_ui.create_ui()
    return blocks


def on_ui_tabs():
    """Build the one tab. Its label and id never change."""
    if settings.use_old_ui():
        print("MiniPaint: mounting the legacy miniPaint editor (Old UI is on).")
        return [(_legacy_tab(), TAB_LABEL, TAB_ID)]

    missing = missing_components()
    if missing:
        print(f"MiniPaint: {missing}; mounting the legacy miniPaint editor.")
        return [
            (
                _legacy_tab(
                    f"**The touch Canvas needs a newer Gradio:** {missing}. "
                    "The legacy miniPaint editor was loaded instead."
                ),
                TAB_LABEL,
                TAB_ID,
            )
        ]

    try:
        with _keep_build_context():
            blocks = _canvas_tab()
        return [(blocks, TAB_LABEL, TAB_ID)]
    except Exception as error:
        traceback.print_exc()
        print("MiniPaint: the touch Canvas failed to build; loading the legacy miniPaint editor instead.")
        reason = f"{type(error).__name__}: {error}".strip()
        return [
            (
                _legacy_tab(
                    "**Canvas redesign failed to initialize; legacy miniPaint was loaded.**  \n"
                    f"`{reason}`  \nThe full traceback is in the WebUI console. "
                    "Settings -> miniPaint / Canvas -> *Use Old UI (legacy miniPaint)* "
                    "makes this the deliberate choice."
                ),
                TAB_LABEL,
                TAB_ID,
            )
        ]
