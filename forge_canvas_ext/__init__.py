"""Forge Neo Canvas extension.

Two frontends live side by side in here:

    forge_canvas_ext.legacy  - the original miniPaint editor in an iframe
    forge_canvas_ext.touch   - the touch-first Gradio ImageEditor workspace

Which one is mounted is decided once, at UI construction time, by
``forge_canvas_ext.ui_router``. Exactly one of them is ever built.
"""
