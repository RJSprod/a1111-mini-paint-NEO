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
from functools import wraps

import gradio as gr

from . import imaging


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


_image_class = None


def canvas_image_class():
    """The host's hidden image textbox, with ``numpy=False`` as its default.

    Gradio keeps a per-session copy of any component an event answered with
    an update (``gr.skip()`` included), rebuilt from the arguments the
    component was constructed with. Forge switches that recording off before
    its own ``LogicalImage`` is defined, so the copy is built from the
    Textbox-level arguments alone and comes back with ``numpy=True``: from
    then on every read of that textbox is an array, not an image. A subclass
    whose default is the value we want survives the rebuild.
    """
    global _image_class
    canvas = host_canvas()
    if canvas is None:
        raise RuntimeError(missing())
    if _image_class is None or not issubclass(_image_class, canvas.LogicalImage):

        class CanvasImage(canvas.LogicalImage):
            webui_do_not_create_gradio_pyi_thank_you = True

            # @wraps matters: Gradio builds a component's config from its
            # __init__ signature, so without it elem_id and friends vanish
            # and the browser side cannot find the textbox.
            @wraps(canvas.LogicalImage.__init__)
            def __init__(self, *args, numpy=False, **kwargs):
                super().__init__(*args, numpy=numpy, **kwargs)

            def postprocess(self, value):
                # A data URL made on our side (a display copy of the
                # composite) goes through as it is; a picture is encoded
                # the host's way.
                if isinstance(value, str):
                    return value
                return super().postprocess(value)

            def preprocess(self, payload):
                # The host reads PNG only; the canvas may hold one of our
                # display copies (JPEG or WebP) when a step reads it back.
                if isinstance(payload, str) and payload.startswith("data:image/") and not payload.startswith("data:image/png;"):
                    try:
                        return imaging.from_data_url(payload)
                    except Exception:
                        return None
                # A display object's URL, if the page was given one. Resolved
                # through the store's own id grammar and never as a path -
                # this is a value arriving from a browser, and the fact that
                # this extension is what put it there a moment ago is not a
                # reason to treat the string as trusted.
                #
                # Not gated behind the setting, deliberately: a page loaded
                # while URLs were on can still send one after they are turned
                # off, and a read-back that could not understand it would
                # lose the picture rather than the optimisation.
                resolved = _display_object(payload)
                if resolved is not None:
                    return resolved
                return super().preprocess(payload)

            def get_block_name(self):
                return "textbox"

        _image_class = CanvasImage
    return _image_class


def _display_object(payload: typing.Any) -> typing.Any:
    """The picture behind one of our display URLs, or None for anything else.

    "Anything else" includes a URL shaped like ours that does not resolve:
    the store answers with its own refusals and none of them is a reason to
    go looking somewhere else.
    """
    if not isinstance(payload, str) or not payload:
        return None
    from . import display

    found = display.id_in_url(payload)
    if not found:
        return None
    try:
        path, _content_type = display.resolve(found)
        return imaging.open_file(str(path))
    except Exception:
        return None


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
        fit_window: bool,
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
        image_class = canvas_image_class()
        self.foreground = image_class(
            visible=False,
            label="foreground",
            elem_id=self.uuid,
            elem_classes=["logical_image_foreground"],
        )
        self.background = image_class(
            visible=False,
            label="background",
            elem_id=self.uuid,
            elem_classes=["logical_image_background"],
        )

        self.mask_style = host_mask_style()
        self.options: typing.Dict[str, typing.Any] = {
            "heightPercent": int(height_percent),
            "fit": bool(fit_window),
            "brushWidth": int(brush_width),
            **self.mask_style,
        }

        from gradio.context import Context

        # The same kind of load event the host registers for each of its own
        # canvases; ours fetches the adapter and then calls it.
        #
        # WHY THIS ONE IS ON PAGE LOAD RATHER THAN ON TAB ACTIVATION.
        #
        # The Canvas adapter is reached from outside its own tab: "Send to
        # Mini Paint" on the txt2img output row runs ``pickGalleryImage`` in
        # the browser, before the Canvas tab has ever been selected, and the
        # same chain ends by switching to it. So the bundle has to be there
        # for a user who has not opened the tab. Making it genuinely lazy
        # means teaching those entry points to await it first, which is a
        # change to the receive chain and wants a browser to prove - see
        # ``assets.py``. What is already won is that it is fetched once,
        # cached immutably, and parsed after the app has mounted rather than
        # during hydration with everything else.
        from .. import assets

        bundles = assets.loader_js(["canvas", "wangp"])
        Context.root_block.load(
            None,
            js=(
                "async () => { "
                f"await ({bundles})(); "
                f"if (window.minipaintCanvas) {{ {attach_js}({json.dumps(self.uuid)}, {json.dumps(self.options)}); }} "
                "}"
            ),
        )
