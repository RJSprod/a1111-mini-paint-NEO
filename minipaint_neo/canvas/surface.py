"""The editing surface: the WebUI's own canvas, built from the host's pieces.

Forge Neo draws its img2img and Inpaint inputs with ForgeCanvas: an HTML
block, a JavaScript class loaded on every page, and two hidden textboxes that
carry the image and the scribbles as PNG data URLs. The same pieces are used
here, so the Canvas tab's editor is the box users already know from img2img,
needs no WebGL, and follows the theme exactly as img2img does. The one
difference is that this extension creates the JavaScript instance itself, so
it can put a crop frame and touch gestures on top of it.
"""

from __future__ import annotations

import json
import typing
import uuid

import gradio as gr


def host_canvas():
    """The host's canvas module, or None when this WebUI does not have it."""
    try:
        from modules_forge.forge_canvas import canvas
    except Exception:
        return None
    if not hasattr(canvas, "canvas_html") or not hasattr(canvas, "LogicalImage"):
        return None
    return canvas


def missing() -> str:
    """Why the surface cannot be built here, or an empty string."""
    if host_canvas() is None:
        return "this WebUI has no Forge Canvas (modules_forge.forge_canvas), which the touch Canvas draws with"
    return ""


def _host_option(name: str, default):
    try:
        from modules.shared import opts

        value = getattr(opts, name)
    except Exception:
        return default
    return default if value is None else value


def host_mask_style() -> typing.Dict[str, typing.Any]:
    """How the Inpaint tab draws its mask: colour, opacity, high-contrast
    checkerboard, brush scaling. The Canvas draws its mask the same way,
    because that is where the mask is going."""
    try:
        alpha = int(_host_option("img2img_inpaint_mask_scribble_alpha", 75))
    except (TypeError, ValueError):
        alpha = 75
    return {
        "color": str(_host_option("img2img_inpaint_mask_brush_color", "#808080")),
        "alpha": max(0, min(100, alpha)),
        "contrast": bool(_host_option("img2img_inpaint_mask_high_contrast", False)),
        "consistent": bool(_host_option("forge_canvas_consistent_brush", False)),
    }


class Surface:
    """One canvas: its HTML block, its two hidden image textboxes, and the
    load event that hands it to the browser-side adapter."""

    def __init__(
        self,
        elem_id: str,
        *,
        height_percent: int,
        brush_width: int,
        attach_js: str,
    ) -> None:
        canvas = host_canvas()
        if canvas is None:
            raise RuntimeError(missing())

        self.uuid = "uuid_" + uuid.uuid4().hex

        html = canvas.canvas_html.replace("forge_mixin", self.uuid)
        # The host's own presentation choices, applied the way the host does.
        if _host_option("forge_canvas_plain", False):
            colour = str(_host_option("forge_canvas_plain_color", "#808080"))
            html = html.replace(
                'class="forge-image-container"',
                f'class="forge-image-container plain" style="background-color: {colour}"',
            ).replace('stroke="white"', "stroke=#444")
        # Touch has no hover, so the toolbar is always visible.
        html = html.replace('class="forge-toolbar"', 'class="forge-toolbar-static"')

        self.block = gr.HTML(html, elem_id=elem_id, elem_classes=["minipaint-surface"])
        self.foreground = canvas.LogicalImage(
            visible=False,
            label="foreground",
            numpy=False,
            elem_id=self.uuid,
            elem_classes=["logical_image_foreground"],
        )
        self.background = canvas.LogicalImage(
            visible=False,
            label="background",
            numpy=False,
            elem_id=self.uuid,
            elem_classes=["logical_image_background"],
        )

        self.mask_style = host_mask_style()
        self.options: typing.Dict[str, typing.Any] = {
            "heightPercent": int(height_percent),
            "brushWidth": int(brush_width),
            **self.mask_style,
        }

        from gradio.context import Context

        # The same kind of load event the host registers for each of its own
        # canvases; ours calls the adapter, which keeps the instance.
        Context.root_block.load(
            None,
            js=f"() => {attach_js}({json.dumps(self.uuid)}, {json.dumps(self.options)})",
        )
