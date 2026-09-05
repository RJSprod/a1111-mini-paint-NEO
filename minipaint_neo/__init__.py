"""miniPaint for Forge Neo: two frontends, one tab.

``legacy_ui`` is the original miniPaint editor in an iframe, kept intact.
``canvas`` is the touch-first Crop / Mask / Expand workspace built from Gradio
components around one ``gr.ImageEditor``. ``router`` mounts exactly one of them,
chosen by the "Use Old UI (legacy miniPaint)" setting.
"""
