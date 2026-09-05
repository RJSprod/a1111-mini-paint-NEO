"""A stand-in for ``modules_forge.forge_canvas.canvas`` (Forge Neo's canvas).

The extension takes three things from that module: ``canvas_html`` (the
markup one canvas is made of, with ``forge_mixin`` where its id goes),
``LogicalImage`` (a hidden Textbox that carries a PIL image as a PNG data
URL) and, in the Forge-like host, ``ForgeCanvas`` itself for the img2img and
Inpaint inputs. All three are reproduced here with the same shapes.

Set FORGE_ROOT to a Forge Neo checkout and the real ``canvas.html`` is used
(and the browser test loads the real ``canvas.js`` and ``canvas.css``);
otherwise a minimal page with the same element ids stands in.
"""

from __future__ import annotations

import base64
import os
import pathlib
import uuid
from functools import wraps
from io import BytesIO

import gradio as gr
from gradio.context import Context
from PIL import Image

from modules import shared

DEBUG_MODE = False

_MINIMAL_HTML = """<div class="forge-container" id="container_forge_mixin">
    <input type="file" id="imageInput_forge_mixin" class="forge-file-upload">
    <div id="imageContainer_forge_mixin" class="forge-image-container">
        <div id="uploadHint_forge_mixin"></div>
        <img id="image_forge_mixin" draggable="false" class="forge-image">
        <canvas id="drawingCanvas_forge_mixin" class="forge-drawing-canvas" style="position:absolute;top:0;left:0;" width="1" height="1"></canvas>
        <div class="forge-toolbar" id="toolbar_forge_mixin">
            <div class="forge-toolbar-box-a">
                <button id="maxButton_forge_mixin" class="forge-btn">⛶</button>
                <button id="minButton_forge_mixin" class="forge-btn">➖</button>
                <button id="uploadButton_forge_mixin" class="forge-btn">📂</button>
                <button id="removeButton_forge_mixin" class="forge-btn">🗑️</button>
                <button id="centerButton_forge_mixin" class="forge-btn">✠</button>
                <button id="resetButton_forge_mixin" class="forge-btn">🔄</button>
                <button id="undoButton_forge_mixin" class="forge-btn">↩️</button>
                <button id="redoButton_forge_mixin" class="forge-btn">↪️</button>
            </div>
            <div class="forge-toolbar-box-b">
                <div class="forge-color-picker-block" id="scribbleColorBlock_forge_mixin"><input type="color" id="scribbleColor_forge_mixin" value="#000000"></div>
                <div class="forge-range-row" id="scribbleWidthBlock_forge_mixin"><div id="widthLabel_forge_mixin"></div><input type="range" id="scribbleWidth_forge_mixin" min="1" max="100" value="25"></div>
                <div class="forge-range-row" id="scribbleAlphaBlock_forge_mixin"><div id="alphaLabel_forge_mixin"></div><input type="range" id="scribbleAlpha_forge_mixin" min="0" max="100" value="100"></div>
                <div class="forge-range-row" id="scribbleSoftnessBlock_forge_mixin"><div id="softnessLabel_forge_mixin"></div><input type="range" id="scribbleSoftness_forge_mixin" min="0" max="100" value="0"></div>
            </div>
        </div>
        <div id="scribbleIndicator_forge_mixin" class="forge-scribble-indicator"></div>
    </div>
</div>"""


def forge_root() -> pathlib.Path | None:
    root = os.environ.get("FORGE_ROOT")
    if root and (pathlib.Path(root) / "modules_forge" / "forge_canvas" / "canvas.html").exists():
        return pathlib.Path(root)
    return None


def _load_html() -> str:
    root = forge_root()
    if root is not None:
        return (root / "modules_forge" / "forge_canvas" / "canvas.html").read_text(encoding="utf-8")
    return _MINIMAL_HTML


canvas_html = _load_html()


def image_to_base64(image, numpy=True):
    if numpy:  # pragma: no cover - the extension never asks for arrays
        image = Image.fromarray(image)
    image = image.convert("RGBA")
    buffered = BytesIO()
    image.save(buffered, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffered.getvalue()).decode("utf-8")


def base64_to_image(base64_str, numpy=True):
    if base64_str.startswith("data:image/png;base64,"):
        base64_str = base64_str.replace("data:image/png;base64,", "")
    image = Image.open(BytesIO(base64.b64decode(base64_str))).convert("RGBA")
    if numpy:  # pragma: no cover
        import numpy as np

        return np.array(image)
    return image


class LogicalImage(gr.Textbox):
    """Forge's hidden textbox: a PIL image in, a PNG data URL out.

    @wraps matters: Gradio builds a component's config from its __init__
    signature, so without it elem_id and friends would vanish."""

    @wraps(gr.Textbox.__init__)
    def __init__(self, *args, numpy=True, **kwargs):
        self.numpy = numpy
        self.infotext = {}
        if "value" in kwargs:
            initial = kwargs["value"]
            if initial is not None:
                kwargs["value"] = image_to_base64(initial, numpy=numpy)
            else:
                del kwargs["value"]
        super().__init__(*args, **kwargs)

    def preprocess(self, payload):
        if not isinstance(payload, str) or not payload.startswith("data:image/png;base64,"):
            return None
        image = base64_to_image(payload, numpy=self.numpy)
        if hasattr(image, "info"):
            image.info = self.infotext
        return image

    def postprocess(self, value):
        if value is None:
            return None
        if hasattr(value, "info"):
            self.infotext = value.info
        return image_to_base64(value, numpy=self.numpy)

    def get_block_name(self):
        return "textbox"


def _opt(name, default):
    try:
        return getattr(shared.opts, name)
    except AttributeError:
        return default


class ForgeCanvas:
    """The host's own canvas, built the way ``modules/ui.py`` builds it."""

    def __init__(self, no_upload=False, no_scribbles=False, contrast_scribbles=False, height=None,
                 scribble_color="#000000", scribble_color_fixed=False, scribble_width=25,
                 scribble_width_fixed=False, scribble_alpha=100, scribble_alpha_fixed=False,
                 scribble_softness=0, scribble_softness_fixed=False, visible=True, numpy=False,
                 initial_image=None, elem_id=None, elem_classes=None):
        self.uuid = "uuid_" + uuid.uuid4().hex
        html = canvas_html.replace("forge_mixin", self.uuid)
        if _opt("forge_canvas_toolbar_always", False):
            html = html.replace('class="forge-toolbar"', 'class="forge-toolbar-static"')
        self.block = gr.HTML(html, visible=visible, elem_id=elem_id, elem_classes=elem_classes)
        self.foreground = LogicalImage(visible=DEBUG_MODE, label="foreground", numpy=numpy, elem_id=self.uuid, elem_classes=["logical_image_foreground"])
        self.background = LogicalImage(visible=DEBUG_MODE, label="background", numpy=numpy, value=initial_image, elem_id=self.uuid, elem_classes=["logical_image_background"])
        js = (
            f'async ()=>{{new ForgeCanvas("{self.uuid}", {str(no_upload).lower()}, {str(no_scribbles).lower()}, '
            f"{str(contrast_scribbles).lower()}, {height or _opt('forge_canvas_height', 512)}, '{scribble_color}', "
            f"{str(scribble_color_fixed).lower()}, {scribble_width}, {str(scribble_width_fixed).lower()}, "
            f"{str(_opt('forge_canvas_consistent_brush', False)).lower()}, {scribble_alpha}, "
            f"{str(scribble_alpha_fixed).lower()}, {scribble_softness}, {str(scribble_softness_fixed).lower()});}}"
        )
        Context.root_block.load(None, js=js)
