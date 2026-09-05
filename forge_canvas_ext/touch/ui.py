"""The touch-first Canvas: one large ImageEditor with ordinary Gradio controls.

Layout, top to bottom: an action bar, the editor, a status line, the three
tool modes, and one panel of options per mode. Which panel is showing is a
frontend concern - switching tools must never cost a server round trip, and
must never rebuild the editor, because rebuilding it re-uploads the image and
loses the strokes.

What *does* go to Python is the low-frequency work: opening a file, committing
a crop, expanding the canvas, clearing or inverting the mask, undo, and
preparing a handoff. Painting never does.
"""

from __future__ import annotations

import html
import json
import os.path
import typing

import gradio as gr
from PIL import Image

from .. import settings
from ..paths import get_asset_url, root_path
from ..transfer_log import announce_send_log
from . import bridge, document, imaging, outpaint
from .gradio_compat import build, has

TOOLS = ["Crop", "Mask", "Expand"]
SIDES = ["Left", "Right", "Top", "Bottom"]

ASSETS = root_path / "forge_canvas_ext" / "touch" / "assets"

# A 1x1 transparent GIF, purely so an element with an onload attribute exists.
_PIXEL = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"


def _adapter_tag() -> str:
    """Load the Canvas adapter, and only when the Canvas is on screen.

    The WebUI loads every file under an extension's javascript/ folder on every
    page, whichever editor is mounted. That is the wrong shape for a frontend
    you can turn off: the legacy editor must be able to run with none of the
    redesign's browser code present at all. A script tag written through
    innerHTML does not execute, but an inline handler does - the same trick the
    legacy editor already uses to know its iframe has loaded.
    """
    try:
        source = get_asset_url(ASSETS / "canvas.js")
    except OSError:
        return ""
    loader = (
        "if(!window.forgeTouchCanvasLoading){window.forgeTouchCanvasLoading=1;"
        "var s=document.createElement('script');s.src=" + json.dumps(source) + ";"
        "s.async=false;document.head.appendChild(s);}"
    )
    attribute = html.escape(loader, quote=True)
    return (
        f'<img alt="" src="{_PIXEL}" style="display:none" '
        f'onload="{attribute}" onerror="{attribute}">'
    )


def _tool_class(name: str) -> str:
    return f"forge-touch-tool-{name.lower()}"


def _status(message: str, notes: typing.Sequence[str] = ()) -> str:
    lines = [message] if message else []
    lines.extend(f"_{note}_" for note in notes if note)
    return "  \n".join(lines)


class TouchCanvas:
    """Builds the tab and owns the callbacks. One instance per mounted UI."""

    def __init__(self) -> None:
        self.mask_colour = imaging.parse_colour(
            settings.get(settings.MASK_COLOUR, "#ff2f2f")
        )
        self.canvas_height = int(settings.get(settings.CANVAS_HEIGHT, 70) or 70)
        self.default_tool = settings.get(settings.DEFAULT_TOOL, "Crop")
        if self.default_tool not in TOOLS:
            self.default_tool = "Crop"
        self.warn_megapixels = float(settings.get(settings.WARN_MEGAPIXELS, 16) or 16)

    # -- helpers -----------------------------------------------------------

    def _value(self, doc: document.Document):
        return imaging.editor_value(doc.image, doc.mask, self.mask_colour)

    def _size_note(self, doc: document.Document) -> typing.Optional[str]:
        if doc.image is None:
            return None
        size = imaging.megapixels(doc.image.size)
        if size <= self.warn_megapixels:
            return None
        return (
            f"{size} megapixels is above the {self.warn_megapixels:g} this install "
            "warns at - expect the editor to feel heavy, especially on a tablet"
        )

    def _reply(
        self,
        doc: document.Document,
        message: str,
        notes: typing.Sequence[str] = (),
        touch_editor: bool = True,
    ):
        """The outputs every structural callback returns."""
        notes = [note for note in notes if note]
        size_note = self._size_note(doc)
        if size_note:
            notes.append(size_note)

        label = bridge.send_label(doc.has_mask, doc.has_expansion)
        destination = bridge.resolve_destination("Auto", doc.has_mask, doc.has_expansion)
        summary = f"**{doc.describe()}** - Auto sends to {destination}"

        return (
            self._value(doc) if touch_editor else gr.update(),
            doc,
            _status(f"{summary}  \n{message}" if message else summary, notes),
            gr.update(value=label),
        )

    # -- callbacks: document -----------------------------------------------

    def open_file(self, file, state):
        doc = document.ensure(state)
        path = getattr(file, "name", file)
        if not path:
            return self._reply(doc, "No file was chosen.", touch_editor=False)

        try:
            with Image.open(path) as opened:
                opened.load()
                # convert() builds a new image, but for a file that is already
                # RGBA it would hand back the one this block is about to close.
                image = (
                    opened.copy() if opened.mode == "RGBA" else opened.convert("RGBA")
                )
        except Exception as error:  # Pillow raises a family of these
            return self._reply(
                doc,
                f"Could not open this image format. ({error})",
                touch_editor=False,
            )

        notes = []
        if doc.has_image:
            doc.checkpoint("open")
            notes.append("the previous image is one Undo away")

        doc.load(image, "local", os.path.basename(str(path)))
        return self._reply(doc, "Opened.", notes)

    def receive(self, image, state):
        """An image arriving from a txt2img/img2img/Extras gallery."""
        doc = document.ensure(state)
        if image is None:
            return self._reply(doc, "", touch_editor=False)

        notes = []
        if doc.has_image:
            doc.checkpoint("receive")
            notes.append("the previous image is one Undo away")

        doc.load(imaging.to_rgba(image), "webui", None)
        return self._reply(doc, "Received from the WebUI.", notes)

    def undo(self, state):
        doc = document.ensure(state)
        label = doc.undo()
        if label is None:
            return self._reply(doc, "Nothing left to undo.", touch_editor=False)
        return self._reply(doc, f"Undid {label}.")

    def redo(self, state):
        doc = document.ensure(state)
        label = doc.redo()
        if label is None:
            return self._reply(doc, "Nothing to redo.", touch_editor=False)
        return self._reply(doc, f"Redid {label}.")

    def reset(self, state):
        doc = document.ensure(state)
        if doc.original is None:
            return self._reply(doc, "There is nothing to reset to.", touch_editor=False)
        doc.checkpoint("reset")
        doc.load(doc.original, doc.origin, doc.filename)
        return self._reply(doc, "Back to the image as it arrived.")

    def revert_editor(self, state):
        """Cancel: put the committed document back, dropping uncommitted edits."""
        doc = document.ensure(state)
        if not doc.has_image:
            return self._reply(doc, "There is no image yet.", touch_editor=False)
        return self._reply(doc, "Cancelled - back to the last applied state.")

    # -- callbacks: crop ---------------------------------------------------

    def apply_crop(self, editor, state, preset, custom_width, custom_height):
        doc = document.ensure(state)
        image, mask, notes = imaging.read_editor(editor)
        if image is None:
            return self._reply(doc, "There is no image to crop.", touch_editor=False)

        # A crop that pads with transparency is a component bug, not the user's
        # picture - but only when the picture going in had no transparency of
        # its own, which is the one case where trimming cannot destroy pixels.
        if doc.image is not None and not imaging.has_alpha_content(doc.image):
            image, mask, note = imaging.trim_transparent_frame(image, mask)
            if note:
                notes.append(note)

        box = None
        if preset == "Original" and doc.original is not None:
            box = imaging.crop_box_for_ratio(image.size, doc.original.size)
        elif preset == "Custom":
            wanted = (
                int(custom_width or 0) or image.width,
                int(custom_height or 0) or image.height,
            )
            box = imaging.crop_box_for_size(image.size, *wanted)
            if (box[2] - box[0], box[3] - box[1]) != wanted:
                notes.append(
                    "the custom size was clamped to the image - a crop never stretches"
                )
        else:
            ratio = imaging.ASPECT_RATIOS.get(preset)
            if isinstance(ratio, tuple):
                box = imaging.crop_box_for_ratio(image.size, ratio)

        unchanged = (
            doc.image is not None
            and image.size == doc.image.size
            and (box is None or box == (0, 0, image.width, image.height))
        )
        if unchanged:
            return self._reply(
                doc,
                f"{preset} is already the whole image - nothing to crop."
                if box is not None
                else "Nothing to crop yet - drag the crop handles, or pick an aspect.",
                notes,
                touch_editor=False,
            )

        doc.checkpoint("crop")
        if box is not None:
            image, mask = imaging.apply_box(image, mask, box)
        doc.commit(image, mask)
        return self._reply(
            doc, f"Cropped to {doc.image.width} x {doc.image.height}.", notes
        )

    # -- callbacks: mask ---------------------------------------------------

    def clear_mask(self, editor, state):
        doc = document.ensure(state)
        image, mask, notes = imaging.read_editor(editor)
        if image is None:
            return self._reply(doc, "There is no image yet.", touch_editor=False)
        if imaging.mask_is_empty(mask):
            doc.commit(image, None)
            return self._reply(doc, "There was no mask to clear.", notes)

        doc.checkpoint("clear mask")
        doc.commit(image, None)
        return self._reply(doc, "Mask cleared. The image is untouched.", notes)

    def invert_mask(self, editor, state):
        doc = document.ensure(state)
        image, mask, notes = imaging.read_editor(editor)
        if image is None:
            return self._reply(doc, "There is no image yet.", touch_editor=False)

        doc.checkpoint("invert mask")
        if imaging.mask_is_empty(mask):
            inverted = Image.new("L", image.size, imaging.MASK_ON)
        else:
            inverted = mask.point(lambda v: imaging.MASK_ON - v)
        doc.commit(image, inverted)
        return self._reply(doc, "Mask inverted.", notes)

    # -- callbacks: expand -------------------------------------------------

    def expand_preview(self, state, left, right, top, bottom, snap_choice):
        doc = document.ensure(state)
        step = outpaint.snap_from_choice(snap_choice)
        sides = tuple(
            outpaint.snap_value(value, step) for value in (left, right, top, bottom)
        )
        return outpaint.describe(doc.size, sides)

    def add_side(self, side, amount, left, right, top, bottom, snap_choice):
        """One tap adds the chosen amount to one side."""
        step = outpaint.snap_from_choice(snap_choice)
        values = {"Left": left, "Right": right, "Top": top, "Bottom": bottom}
        try:
            addition = int(amount)
        except (TypeError, ValueError):
            addition = 0
        values[side] = outpaint.snap_value((values.get(side) or 0) + addition, step)
        return (
            values["Left"],
            values["Right"],
            values["Top"],
            values["Bottom"],
        )

    def clear_sides(self):
        return 0, 0, 0, 0

    def apply_expand(
        self, editor, state, left, right, top, bottom, overlap, fill, snap_choice
    ):
        doc = document.ensure(state)
        image, mask, notes = imaging.read_editor(editor)
        if image is None:
            return self._reply(doc, "There is no image to expand.", touch_editor=False)

        step = outpaint.snap_from_choice(snap_choice)
        sides = tuple(
            outpaint.snap_value(value, step) for value in (left, right, top, bottom)
        )

        try:
            expanded, new_mask, info = outpaint.expand(
                image, mask, sides, int(overlap or 0), fill
            )
        except ValueError as error:
            return self._reply(doc, str(error), notes, touch_editor=False)

        doc.checkpoint("expand")
        doc.commit(expanded, new_mask)
        doc.has_expansion = True
        doc.last_expansion = info

        if info["megapixels"] > self.warn_megapixels:
            notes.append(
                f"the new canvas is {info['megapixels']} megapixels - large "
                "documents are slow in the browser and can run a tablet out of memory"
            )
        if info["overlap"]:
            notes.append(
                f"the mask reaches {info['overlap']}px back into the original so the "
                "model has room to blend"
            )

        return self._reply(
            doc,
            f"Expanded to {info['to'][0]} x {info['to'][1]}. The new area is masked - "
            "switch to Mask to refine it.",
            notes,
        )

    # -- callbacks: send ---------------------------------------------------

    def prepare_send(
        self, editor, state, destination, smoothing, controlnet_unit, controlnet_tab
    ):
        doc = document.ensure(state)
        image, mask, notes = imaging.read_editor(editor)
        if image is None:
            return ("", *self._reply(doc, "There is no image to send.", touch_editor=False))

        doc.commit(image, mask)

        outgoing = imaging.smooth_mask(doc.mask, smoothing)
        if smoothing != "Off" and doc.has_mask:
            notes.append(f"mask smoothing: {smoothing}")

        target = bridge.resolve_destination(
            destination, doc.has_mask, doc.has_expansion, doc.origin
        )
        fill_name = settings.get(settings.SEND_FILL, "Neutral gray")

        try:
            payload, send_notes = bridge.prepare(
                doc.image,
                outgoing,
                target,
                fill_name,
                controlnet_unit,
                controlnet_tab,
            )
        except (ValueError, OSError) as error:
            return ("", *self._reply(doc, f"Could not prepare the send: {error}", notes))

        notes.extend(send_notes)
        doc.last_send = payload["label"]
        return (
            bridge.payload_json(payload),
            *self._reply(
                doc, f"Sending to {payload['label']}...", notes, touch_editor=False
            ),
        )

    def transfer_result(self, raw, state):
        doc = document.ensure(state)
        result = bridge.read_result(raw)
        if not result:
            return gr.update()

        if result.get("ok"):
            return _status(
                f"**Sent to {result.get('label', doc.last_send or 'img2img')}.**",
                [result.get("detail", "")],
            )
        return _status(
            f"**Transfer failed** - {result.get('message', 'the browser did not say why')}",
            [
                result.get("detail", ""),
                "The image here is untouched. Fix the destination and press Send again.",
            ],
        )

    def download(self, editor, state):
        doc = document.ensure(state)
        image, mask, _notes = imaging.read_editor(editor)
        if image is None:
            return gr.update()
        doc.commit(image, mask)
        return gr.update(value=bridge.save_for_download(doc.image))

    # -- layout ------------------------------------------------------------

    def build(self) -> None:
        announce_send_log("touch Canvas")
        colour_hex = "#%02x%02x%02x" % self.mask_colour

        gr.HTML(
            "<style>#forge_touch_editor_root{--forge-touch-canvas-height:"
            f"{self.canvas_height}vh;}}</style>" + _adapter_tag(),
            elem_id="forge_touch_style",
        )

        with gr.Column(
            elem_id="forge_touch_editor_root",
            elem_classes=["forge-touch-root", _tool_class(self.default_tool)],
        ):
            state = gr.State(None)

            with gr.Row(elem_classes=["forge-touch-topbar"]):
                open_btn = build(
                    gr.UploadButton,
                    label="Open",
                    file_types=["image"],
                    type="filepath",
                    elem_id="forge_touch_open",
                    elem_classes=["forge-touch-action"],
                    size="lg",
                )
                undo_btn = gr.Button(
                    "Undo", elem_id="forge_touch_undo", elem_classes=["forge-touch-action"]
                )
                redo_btn = gr.Button(
                    "Redo", elem_id="forge_touch_redo", elem_classes=["forge-touch-action"]
                )
                fit_btn = gr.Button(
                    "Fit", elem_id="forge_touch_fit", elem_classes=["forge-touch-action"]
                )
                focus_btn = gr.Button(
                    "Focus", elem_id="forge_touch_focus", elem_classes=["forge-touch-action"]
                )
                send_btn = gr.Button(
                    "Send to img2img",
                    variant="primary",
                    elem_id="forge_touch_send",
                    elem_classes=["forge-touch-action", "forge-touch-send"],
                )

            with gr.Row(elem_classes=["forge-touch-canvas-row"]):
                editor = build(
                    gr.ImageEditor,
                    label="Canvas",
                    show_label=False,
                    type="pil",
                    image_mode="RGBA",
                    sources=["upload", "clipboard"],
                    transforms=["crop"],
                    brush=build(
                        gr.Brush,
                        default_size="auto",
                        colors=[colour_hex],
                        default_color=colour_hex,
                        color_mode="fixed",
                    ),
                    eraser=build(gr.Eraser, default_size="auto"),
                    layers=True,
                    # PNG on purpose: the frontend re-encodes on every commit,
                    # and the default (webp) is lossy - a few round trips of an
                    # image on its way to img2img is not the place for that.
                    format="png",
                    show_download_button=False,
                    show_fullscreen_button=True,
                    container=False,
                    elem_id="forge_touch_canvas",
                    elem_classes=["forge-touch-canvas"],
                )

            status = gr.Markdown(
                "**No image** - open one, or send one here from txt2img.",
                elem_id="forge_touch_status",
                elem_classes=["forge-touch-status"],
            )

            with gr.Row(elem_classes=["forge-touch-modebar"]):
                crop_mode = gr.Button(
                    "Crop", elem_id="forge_touch_tool_crop",
                    elem_classes=["forge-touch-mode"],
                    variant="primary" if self.default_tool == "Crop" else "secondary",
                )
                mask_mode = gr.Button(
                    "Mask", elem_id="forge_touch_tool_mask",
                    elem_classes=["forge-touch-mode"],
                    variant="primary" if self.default_tool == "Mask" else "secondary",
                )
                expand_mode = gr.Button(
                    "Expand", elem_id="forge_touch_tool_expand",
                    elem_classes=["forge-touch-mode"],
                    variant="primary" if self.default_tool == "Expand" else "secondary",
                )

            with gr.Group(elem_classes=["forge-touch-options"]):
                crop_panel, crop_controls = self._crop_panel()
                mask_panel, mask_controls = self._mask_panel()
                expand_panel, expand_controls = self._expand_panel()

            (
                destination,
                controlnet_unit,
                controlnet_tab,
                save_btn,
                save_file,
            ) = self._more_panel()

            # The wires the browser adapter talks over. They are moved off
            # screen with CSS rather than hidden with visible=False, because
            # the adapter reads and writes their DOM nodes: a build that
            # renders a hidden component lazily would take the send path with
            # it.
            with gr.Column(elem_classes=["forge-touch-offscreen"]):
                payload = gr.Textbox(
                    "", label="payload", show_label=False,
                    elem_id="forge_touch_payload",
                )
                result = gr.Textbox(
                    "", label="result", show_label=False,
                    elem_id="forge_touch_result",
                )
                inbox = build(
                    gr.Image,
                    label="Incoming",
                    show_label=False,
                    type="pil",
                    image_mode="RGBA",
                    sources=["upload"],
                    elem_id="forge_touch_inbox",
                )

        self._wire(
            state=state,
            editor=editor,
            status=status,
            send_btn=send_btn,
            open_btn=open_btn,
            undo_btn=undo_btn,
            redo_btn=redo_btn,
            payload=payload,
            result=result,
            inbox=inbox,
            destination=destination,
            controlnet_unit=controlnet_unit,
            controlnet_tab=controlnet_tab,
            save_btn=save_btn,
            save_file=save_file,
            crop=crop_controls,
            mask=mask_controls,
            expand=expand_controls,
        )

    # -- panels ------------------------------------------------------------

    def _crop_panel(self):
        with gr.Column(
            elem_classes=["forge-touch-panel", "forge-touch-panel-crop"],
            elem_id="forge_touch_panel_crop",
        ) as panel:
            with gr.Row():
                preset = gr.Radio(
                    list(imaging.ASPECT_RATIOS),
                    value="Free",
                    show_label=False,
                    elem_id="forge_touch_crop_preset",
                    elem_classes=["forge-touch-chips"],
                )
            with gr.Accordion("Custom size", open=False):
                with gr.Row():
                    width = gr.Number(
                        label="Width", value=1024, precision=0, minimum=1,
                        elem_id="forge_touch_crop_width",
                    )
                    height = gr.Number(
                        label="Height", value=1024, precision=0, minimum=1,
                        elem_id="forge_touch_crop_height",
                    )
            with gr.Row(elem_classes=["forge-touch-panel-actions"]):
                cancel = gr.Button("Cancel", elem_id="forge_touch_crop_cancel")
                reset = gr.Button("Reset image", elem_id="forge_touch_crop_reset")
                apply = gr.Button(
                    "Apply Crop", variant="primary", elem_id="forge_touch_crop_apply"
                )
        return panel, {
            "preset": preset,
            "width": width,
            "height": height,
            "cancel": cancel,
            "reset": reset,
            "apply": apply,
        }

    def _mask_panel(self):
        with gr.Column(
            elem_classes=["forge-touch-panel", "forge-touch-panel-mask"],
            elem_id="forge_touch_panel_mask",
        ) as panel:
            with gr.Row():
                brush = gr.Button(
                    "Brush", variant="primary", elem_id="forge_touch_brush",
                    elem_classes=["forge-touch-mask-tool"],
                )
                erase = gr.Button(
                    "Erase", elem_id="forge_touch_erase",
                    elem_classes=["forge-touch-mask-tool"],
                )
                clear = gr.Button("Clear Mask", elem_id="forge_touch_mask_clear")
                invert = gr.Button("Invert", elem_id="forge_touch_mask_invert")
            with gr.Row():
                size = gr.Slider(
                    1, 400, value=40, step=1, label="Brush size",
                    elem_id="forge_touch_mask_size",
                )
                smoothing = gr.Radio(
                    imaging.SMOOTHING_LEVELS,
                    value="Off",
                    label="Mask smoothing (applied when sending)",
                    elem_id="forge_touch_mask_smoothing",
                )
            hint = gr.HTML("", elem_id="forge_touch_mask_hint")
        return panel, {
            "brush": brush,
            "erase": erase,
            "clear": clear,
            "invert": invert,
            "size": size,
            "smoothing": smoothing,
            "hint": hint,
        }

    def _expand_panel(self):
        snap_default = str(settings.get(settings.SNAP, "8"))
        if snap_default not in outpaint.SNAP_CHOICES:
            snap_default = "8"

        with gr.Column(
            elem_classes=["forge-touch-panel", "forge-touch-panel-expand"],
            elem_id="forge_touch_panel_expand",
        ) as panel:
            with gr.Row():
                amount = gr.Radio(
                    ["64", "128", "256"], value="128", label="Add",
                    elem_id="forge_touch_expand_amount",
                    elem_classes=["forge-touch-chips"],
                )
                side_buttons = {
                    side: gr.Button(side, elem_id=f"forge_touch_expand_{side.lower()}")
                    for side in SIDES
                }
                clear_sides = gr.Button("Clear", elem_id="forge_touch_expand_clear")
            preview = gr.Markdown(
                "No image yet.", elem_id="forge_touch_expand_preview",
                elem_classes=["forge-touch-preview"],
            )
            with gr.Accordion("Advanced expansion", open=False):
                with gr.Row():
                    numbers = {
                        side: gr.Number(
                            label=side, value=0, precision=0, minimum=0,
                            elem_id=f"forge_touch_expand_num_{side.lower()}",
                        )
                        for side in SIDES
                    }
                with gr.Row():
                    overlap = gr.Slider(
                        0, 256, value=32, step=8, label="Overlap into the original",
                        elem_id="forge_touch_expand_overlap",
                    )
                    fill = gr.Dropdown(
                        outpaint.FILL_POLICIES,
                        value=outpaint.DEFAULT_FILL,
                        label="New area",
                        elem_id="forge_touch_expand_fill",
                    )
                    snap = gr.Dropdown(
                        outpaint.SNAP_CHOICES,
                        value=snap_default,
                        label="Snap to",
                        elem_id="forge_touch_expand_snap",
                    )
            with gr.Row(elem_classes=["forge-touch-panel-actions"]):
                apply = gr.Button(
                    "Apply Expand", variant="primary", elem_id="forge_touch_expand_apply"
                )
        return panel, {
            "amount": amount,
            "sides": side_buttons,
            "clear": clear_sides,
            "preview": preview,
            "numbers": numbers,
            "overlap": overlap,
            "fill": fill,
            "snap": snap,
            "apply": apply,
        }

    def _more_panel(self):
        with gr.Accordion("More", open=False, elem_id="forge_touch_more"):
            with gr.Row():
                destination = gr.Dropdown(
                    bridge.DESTINATIONS,
                    value="Auto",
                    label="Send to",
                    elem_id="forge_touch_destination",
                )
                controlnet_tab = gr.Dropdown(
                    ["img2img", "txt2img"],
                    value="img2img",
                    label="ControlNet tab",
                    elem_id="forge_touch_cn_tab",
                )
                controlnet_unit = gr.Number(
                    label="ControlNet unit", value=0, precision=0, minimum=0,
                    elem_id="forge_touch_cn_unit",
                )
            save_file = None
            if has("DownloadButton"):
                save_btn = build(
                    gr.DownloadButton,
                    label="Save a copy",
                    elem_id="forge_touch_save",
                )
            else:  # pragma: no cover - Gradio builds without DownloadButton
                save_btn = gr.Button("Save a copy", elem_id="forge_touch_save")
                save_file = gr.File(
                    label="Saved copy", interactive=False, elem_id="forge_touch_save_file"
                )
            gr.Markdown(
                "Tool options change with the mode. Undo covers open, crop, "
                "expand and mask clears; the editor's own undo covers strokes.",
                elem_classes=["forge-touch-hint"],
            )
        return destination, controlnet_unit, controlnet_tab, save_btn, save_file

    # -- wiring ------------------------------------------------------------

    def _wire(self, **parts) -> None:
        state = parts["state"]
        editor = parts["editor"]
        status = parts["status"]
        send_btn = parts["send_btn"]
        crop = parts["crop"]
        mask = parts["mask"]
        expand = parts["expand"]

        # Every structural callback answers with the same four outputs.
        standard = [editor, state, status, send_btn]
        side_inputs = [expand["numbers"][side] for side in SIDES]
        preview_inputs = [state] + side_inputs + [expand["snap"]]

        def structural(event, fn, inputs=None):
            """Wire a step that can change the document's size.

            The expansion preview quotes those dimensions, so it is refreshed
            from the same click rather than waiting for the user to touch a
            side and notice it was stale.
            """
            return event(fn, inputs=inputs, outputs=standard).then(
                self.expand_preview, inputs=preview_inputs, outputs=[expand["preview"]]
            )

        structural(
            parts["open_btn"].upload, self.open_file, [parts["open_btn"], state]
        )
        structural(parts["inbox"].change, self.receive, [parts["inbox"], state])
        structural(parts["undo_btn"].click, self.undo, [state])
        structural(parts["redo_btn"].click, self.redo, [state])

        structural(
            crop["apply"].click,
            self.apply_crop,
            [editor, state, crop["preset"], crop["width"], crop["height"]],
        )
        structural(crop["cancel"].click, self.revert_editor, [state])
        structural(crop["reset"].click, self.reset, [state])

        structural(mask["clear"].click, self.clear_mask, [editor, state])
        structural(mask["invert"].click, self.invert_mask, [editor, state])

        for side, button in expand["sides"].items():
            button.click(
                lambda amount, left, right, top, bottom, snap, side=side: self.add_side(
                    side, amount, left, right, top, bottom, snap
                ),
                inputs=[expand["amount"]] + side_inputs + [expand["snap"]],
                outputs=side_inputs,
            ).then(
                self.expand_preview, inputs=preview_inputs, outputs=[expand["preview"]]
            )
        expand["clear"].click(self.clear_sides, outputs=side_inputs).then(
            self.expand_preview, inputs=preview_inputs, outputs=[expand["preview"]]
        )
        for control in side_inputs + [expand["snap"]]:
            control.change(
                self.expand_preview, inputs=preview_inputs, outputs=[expand["preview"]]
            )
        structural(
            expand["apply"].click,
            self.apply_expand,
            [editor, state] + side_inputs
            + [expand["overlap"], expand["fill"], expand["snap"]],
        )

        send_btn.click(
            self.prepare_send,
            inputs=[
                editor,
                state,
                parts["destination"],
                mask["smoothing"],
                parts["controlnet_unit"],
                parts["controlnet_tab"],
            ],
            outputs=[parts["payload"]] + standard,
        )
        parts["result"].change(
            self.transfer_result,
            inputs=[parts["result"], state],
            outputs=[status],
        )

        save = parts["save_btn"]
        save_file = parts["save_file"]
        save.click(
            self.download,
            inputs=[editor, state],
            outputs=[save if save_file is None else save_file],
        )


def create_ui() -> None:
    TouchCanvas().build()
