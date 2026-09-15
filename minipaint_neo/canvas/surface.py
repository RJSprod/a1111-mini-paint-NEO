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


def canvas_markup(uuid: str) -> str:
    """The markup one canvas is made of, with this surface's id in it.

    Split out of ``Surface`` for the same reason as ``bootstrap_js``: the
    browser test builds a page out of this, and a copy of it in the test
    would drift. The toolbar rewrite in particular is load-bearing - the
    stylesheet, the crop grip's collision test and the geometry check all
    address the toolbar by the class this produces, not the one Forge ships.
    """
    canvas = host_canvas()
    if canvas is None:
        raise RuntimeError(missing())
    html = canvas.canvas_html.replace("forge_mixin", uuid)
    if _host_option("forge_canvas_plain", False):
        colour = str(_host_option("forge_canvas_plain_color", "#808080"))
        html = html.replace(
            'class="forge-image-container"',
            f'class="forge-image-container plain" style="background-color: {colour}"',
        ).replace('stroke="white"', "stroke=#444")
    # Touch has no hover, so the toolbar is always visible.
    return html.replace('class="forge-toolbar"', 'class="forge-toolbar-static"')


def bootstrap_js(uuid: str, options: typing.Mapping[str, typing.Any], status_elem_id: str = "") -> str:
    """The page-load JavaScript that brings one Canvas up.

    WHY THIS IS ON PAGE LOAD RATHER THAN ON TAB ACTIVATION.

    The Canvas adapter is reached from outside its own tab: "Send to Mini
    Paint" on the txt2img output row runs ``pickGalleryImage`` in the browser
    before the Canvas tab has ever been selected, and the same chain ends by
    switching to it. Those entry points now await readiness rather than
    hoping for it, so this could become lazy - but changing the readiness
    contract and the moment of the fetch in one patch is two experiments at
    once and only one of them is the repair. What is already won is that the
    bundle is fetched once, cached immutably, and parsed after the app has
    mounted rather than during hydration with everything else.

    CANVAS IS REQUIRED. WANGP IS NOT.

    They are separate products from the page's point of view and they are
    loaded separately because of it: the Canvas load is awaited and decides
    whether the editor is ready, and the WanGP load is started afterwards
    and its answer is never consulted. A WanGP bundle that 404s, times out
    or throws leaves Open, paste, drop, crop, mask and layers exactly as
    they were. Asking for both in one ``load`` call would manufacture one
    required failure out of one optional one.

    Split out of ``Surface`` so the browser test can drive the very string
    the extension ships. A test that reimplemented this would keep passing
    while this drifted, which is exactly how a dropped promise survived a
    green suite once already.
    """
    from .. import assets

    config = {
        "url": assets.url_for("canvas"),
        "uuid": uuid,
        "options": dict(options),
        "status": status_elem_id,
    }
    wangp = json.dumps([assets.url_for("wangp")])
    # The transfer library, named for the page and started early. A send is
    # the first thing that needs it and a send is the worst moment to be
    # fetching it, so it is asked for here and awaited only where it is used.
    host = json.dumps(assets.url_for("host"))
    return (
        "async () => { "
        "const r = window.minipaintCanvasReady; "
        "if (!r) { return false; } "
        f"r.configure({json.dumps(config)}); "
        "const ok = await r.ensure(); "
        "const a = window.minipaintAssets; "
        f"window.minipaintHostUrl = {host}; "
        f"if (a && a.module) {{ a.module({host}); }} "
        f"if (a && a.load) {{ a.load({wangp}); }} "
        "return ok; "
        "}"
    )


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
        status_elem_id: str = "",
    ) -> None:
        canvas = host_canvas()
        if canvas is None:
            raise RuntimeError(missing())

        self.uuid = "uuid_" + uuid.uuid4().hex

        html = canvas_markup(self.uuid)
        # The host's own presentation choices, applied the way the host does.
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

        Context.root_block.load(None, js=bootstrap_js(self.uuid, self.options, status_elem_id))
