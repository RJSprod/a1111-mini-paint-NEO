"""The touch-first Canvas: the WebUI's own canvas, with ordinary Gradio
controls around it.

Layout, top to bottom: an action bar, the canvas, a status line, the three
modes (Crop / Mask / Expand), one panel of options per mode, and a "More"
accordion. Buttons are Buttons and sliders are Sliders, so the host theme
decides how everything looks; the canvas is the same one img2img uses.

What goes to Python is the low-frequency work: receiving or opening an
image, committing a crop, expanding, clearing or inverting the mask, undo
and redo of those steps, and the handoff to img2img. Painting, panning and
zooming stay in the browser.

The canvas takes its image and its scribble layer through two hidden
textboxes, the way the host's own canvases do. The scribble layer can only
be drawn once the image has finished loading, so every step that changes
the image is three chained events: write the image, wait for the canvas to
show it, write the mask layer. The same wait guards the handoff into the
Inpaint tab's canvas. See ``surface.py`` and ``javascript/minipaint_canvas.js``.
"""

from __future__ import annotations

import json
import os.path
import typing

import gradio as gr

from .. import settings
from ..send_log import announce_send_log, log_quietly
from . import document, host, imaging, outpaint, surface

MODES = ("crop", "mask", "expand")
MODE_LABELS = {"crop": "Crop", "mask": "Mask", "expand": "Expand"}
ASPECTS = ["Free", "Original", "1:1", "4:3", "3:4", "16:9", "9:16", "3:2", "2:3", "Custom"]
TOOLS = ["Paint", "Erase", "Move"]
DESTINATIONS = ["Auto", "img2img", "Inpaint", "Extras"]
DESTINATION_KEYS = {"img2img": "img2img", "Inpaint": "inpaint", "Extras": "extras"}
DESTINATION_LABELS = {"img2img": "img2img", "inpaint": "img2img Inpaint", "extras": "Extras"}

PREFIX = "minipaint_canvas"


def _id(name: str) -> str:
    return f"{PREFIX}_{name}"


# All browser-side helpers live in javascript/minipaint_canvas.js. Apart from
# attaching the canvas when the page loads (the host does the same for its
# own canvases), every one is called from a Gradio event on a user action.
# A hook that runs before a backend step must hand back the step's inputs;
# one that runs alone (no backend step) returns nothing.
_JS = "window.minipaintCanvas"
ATTACH_JS = f"{_JS} && {_JS}.attach"
WAIT_JS = (
    f"async (flag) => {{ if (flag === 'wait' && {_JS}) {{ await {_JS}.waitForImage(); }} return [flag]; }}"
)
MODE_JS = f"(mode) => {{ if ({_JS}) {_JS}.onMode(mode); }}"
ASPECT_JS = f"(choice, w, h, original) => {{ if ({_JS}) {_JS}.setAspect(choice, w, h, original); }}"
TOOL_JS = f"(tool) => {{ if ({_JS}) {_JS}.setTool(tool); }}"
SIZE_JS = f"(size) => {{ if ({_JS}) {_JS}.setBrushSize(size); }}"
FIT_JS = f"() => {{ if ({_JS}) {_JS}.fit(); }}"
FOCUS_ON_JS = f"() => {{ if ({_JS}) {_JS}.setFocus(true); }}"
FOCUS_OFF_JS = f"() => {{ if ({_JS}) {_JS}.setFocus(false); }}"
SWITCH_JS = f"(target) => {{ if ({_JS}) {_JS}.switchTo(target); }}"
SWITCH_CANVAS_JS = f"() => {{ if ({_JS}) {_JS}.switchTo('canvas'); }}"
CROP_JS = (
    f"(bg, fg, state, mode, box) => {{ if ({_JS}) {_JS}.mark(); "
    f"return [bg, fg, state, mode, {_JS} ? {_JS}.cropBox() : '']; }}"
)
PICK_JS = (
    f"(gallery, state, mode) => {{ if ({_JS}) {_JS}.mark(); "
    f"return [{_JS} ? {_JS}.pickGalleryImage(gallery) : null, state, mode]; }}"
)
# The canvas echoes every image it loads back through its textbox. The
# browser knows which of those are echoes and strips them here, so an echo
# costs one tiny request instead of an upload of the whole picture.
CANVAS_INPUT_JS = (
    f"(bg, state, mode, kind) => {_JS} ? {_JS}.canvasInput(bg, state, mode) : [bg, state, mode, 'user']"
)


def _mark_js(input_count: int) -> str:
    """A pass-through that notes the moment before the image is replaced, so
    the wait step can tell a fresh load from the last one."""
    return f"(...args) => {{ if ({_JS}) {_JS}.mark(); return args.slice(0, {input_count}); }}"


def _host_wait_js(uuid: str) -> str:
    """Wait until the Inpaint tab's canvas has taken the image just sent to
    it, so the mask layer written next lands on a canvas of the right size."""
    return (
        f"async (target) => {{ if ({_JS} && String(target).indexOf('inpaint') === 0) "
        f"{{ await {_JS}.waitForHostImage({json.dumps(uuid)}, target); }} return [target]; }}"
    )


def _noop(*_args):
    return None


def _status(message: str, notes: typing.Sequence[str] = ()) -> str:
    lines = [message] if message else []
    lines.extend(f"<small>{note}</small>" for note in notes if note)
    return "  \n".join(lines)


def send_label(has_mask: bool, has_expansion: bool, mode: str) -> str:
    if has_expansion:
        return "Send Outpaint to img2img"
    if has_mask or mode == "mask":
        return "Send to img2img Inpaint"
    return "Send to img2img"


def resolve_destination(choice: str, has_mask: bool, has_expansion: bool) -> str:
    """Which destination a send goes to. Deterministic, never a surprise.

    Auto exists so the common case takes no decision: anything carrying a
    mask - drawn, or created by an expansion - is an inpaint, everything
    else is a plain img2img.
    """
    if choice in DESTINATION_KEYS:
        return DESTINATION_KEYS[choice]
    return "inpaint" if (has_mask or has_expansion) else "img2img"


class TouchCanvas:
    """Builds the tab and owns its callbacks. One instance per mounted UI."""

    def __init__(self) -> None:
        self.canvas_height = settings.canvas_height_percent()
        self.brush_width = settings.brush_width()
        self.snap_default = str(settings.get(settings.EXPAND_SNAP, "8"))
        if self.snap_default not in settings.SNAP_CHOICES:
            self.snap_default = "8"
        self.mask_style = surface.host_mask_style()
        self.mask_color = imaging.parse_color(self.mask_style["color"])
        self.targets: typing.Dict[str, typing.Any] = {}
        self.target_order: typing.List[str] = []
        self.surface: typing.Optional[surface.Surface] = None

    # -- replies -------------------------------------------------------------

    def _layer(self, mask, size):
        """The mask as the scribble layer the canvases show."""
        return imaging.foreground_layer(mask, size, self.mask_color, self.mask_style["contrast"])

    def _mode_updates(self, mode: str, doc: document.Document) -> tuple:
        return (
            mode,
            gr.update(visible=mode == "crop"),
            gr.update(visible=mode == "mask"),
            gr.update(visible=mode == "expand"),
            gr.update(variant="primary" if mode == "crop" else "secondary"),
            gr.update(variant="primary" if mode == "mask" else "secondary"),
            gr.update(variant="primary" if mode == "expand" else "secondary"),
            gr.update(value=send_label(doc.has_mask, doc.has_expansion, mode)),
            gr.update(value="Paint") if mode == "mask" else gr.skip(),
        )

    def _info(
        self,
        doc: document.Document,
        mode: str,
        message: str,
        notes: typing.Sequence[str] = (),
        *,
        sides: typing.Sequence[int] = (0, 0, 0, 0),
        reset_aspect: bool = False,
        wait: bool = False,
    ) -> tuple:
        """Everything a callback reports back apart from the canvas itself."""
        notes = [note for note in notes if note]
        if doc.image is not None:
            size = imaging.megapixels(doc.image.size)
            if size > outpaint.WARN_MEGAPIXELS:
                notes.append(f"{size} megapixels is a lot for a browser canvas, especially on a tablet")

        summary = f"**{doc.describe()}**"
        if doc.has_image:
            target = resolve_destination("Auto", doc.has_mask, doc.has_expansion)
            summary += f" — Auto sends to {DESTINATION_LABELS[target]}"

        return (
            doc,
            _status(f"{summary}  \n{message}" if message else summary, notes),
            outpaint.describe(doc.size, sides),
            gr.update(value="Free") if reset_aspect else gr.skip(),
            doc.original_size_text(),
            "wait" if wait else "",
            *self._mode_updates(mode, doc),
        )

    def _commit(
        self,
        doc: document.Document,
        mode: str,
        message: str,
        notes: typing.Sequence[str] = (),
        *,
        sides: typing.Sequence[int] = (0, 0, 0, 0),
        reset_aspect: bool = True,
    ) -> tuple:
        """Push the document into the canvas: the image now, the mask layer
        in the step that follows the canvas having loaded it."""
        background = doc.image if doc.image is not None else None
        foreground = gr.skip() if doc.has_mask else None
        return (
            background,
            foreground,
            *self._info(doc, mode, message, notes, sides=sides, reset_aspect=reset_aspect, wait=doc.has_mask),
        )

    def _unchanged(self, doc: document.Document, mode: str, message: str, notes: typing.Sequence[str] = ()) -> tuple:
        """The commit-shaped reply for a step that changed nothing."""
        return (gr.skip(), gr.skip(), *self._info(doc, mode, message, notes))

    INFO_COUNT = 6 + 9

    def _skip_info(self) -> tuple:
        return tuple(gr.skip() for _ in range(self.INFO_COUNT))

    def _sync(self, doc: document.Document, background, foreground):
        """The canvas's current pixels become the document's."""
        image, mask, notes = imaging.read_canvas(background, foreground)
        if image is not None:
            doc.commit(image, mask)
        return image, mask, notes

    # -- callbacks: modes and tools ----------------------------------------

    def set_mode(self, mode: str, state):
        doc = document.ensure(state)
        return self._mode_updates(mode if mode in MODES else "crop", doc)

    # -- callbacks: what the canvas holds ----------------------------------

    def on_canvas_image(self, background, state, mode, kind):
        """The canvas's image changed in the browser: a file was opened,
        dropped or pasted onto it (``kind`` "user"), or the canvas echoed an
        image this side sent ("echo", already stripped of its pixels).

        Replies with the scribble layer first, then the usual information:
        a picture opened in the browser starts a fresh document, so the
        strokes left over from the previous one are cleared.
        """
        doc = document.ensure(state)
        if kind == "echo":
            return (gr.skip(), *self._skip_info())
        if background is None:
            if not doc.has_image:
                return (gr.skip(), *self._skip_info())
            doc.checkpoint("clear")
            doc.clear()
            return (None, *self._info(doc, mode, "The canvas was cleared. Undo brings the image back."))
        if imaging.images_equal(background, doc.image):
            return (gr.skip(), *self._skip_info())

        notes = ["the previous image is one Undo away"] if doc.has_image else []
        doc.checkpoint("open")
        doc.load(background, "file")
        log_quietly({"destination": "canvas", "outcome": f"opened {doc.image.width}x{doc.image.height} in the browser"})
        return (None, *self._info(doc, "crop", "Opened.", notes, reset_aspect=True))

    def receive(self, payload, state, mode, tab: str):
        """An image arriving from a txt2img / img2img / Extras gallery."""
        doc = document.ensure(state)
        image = host.gallery_image(payload)
        if image is None:
            return self._unchanged(doc, mode, "Pick an image in the gallery first.")

        notes = ["the previous image is one Undo away"] if doc.has_image else []
        doc.checkpoint("receive")
        doc.load(imaging.to_rgba(image), tab)
        log_quietly({"destination": f"{tab} -> Canvas", "outcome": f"received {doc.image.width}x{doc.image.height}"})
        return self._commit(doc, "crop", f"Received from {tab}.", notes)

    def open_file(self, file, state, mode):
        doc = document.ensure(state)
        path = getattr(file, "name", file)
        if not path:
            return self._unchanged(doc, mode, "No file was chosen.")
        try:
            image = imaging.open_file(str(path))
        except Exception as error:  # Pillow raises a family of these
            return self._unchanged(doc, mode, f"Could not open this image format. ({error})")

        notes = ["the previous image is one Undo away"] if doc.has_image else []
        doc.checkpoint("open")
        doc.load(image, "file", os.path.basename(str(path)))
        return self._commit(doc, "crop", "Opened.", notes)

    # -- callbacks: crop ----------------------------------------------------

    def apply_crop(self, background, foreground, state, mode, box_raw):
        doc = document.ensure(state)
        image, mask, notes = self._sync(doc, background, foreground)
        if image is None:
            return self._unchanged(doc, mode, "There is no image to crop.")
        box = imaging.crop_box(box_raw, image.size)
        if box is None:
            return self._unchanged(doc, mode, "Put the frame over the part to keep: drag the image under it, pinch or scroll to zoom, drag a corner to resize.")
        if box == (0, 0, image.width, image.height):
            return self._unchanged(doc, mode, "The frame already covers the whole image — nothing to crop.")

        doc.checkpoint("crop")
        doc.commit(image.crop(box), mask.crop(box) if mask is not None else None)
        return self._commit(doc, mode, f"Cropped to {doc.image.width} × {doc.image.height}.", notes)

    # -- callbacks: mask ----------------------------------------------------

    def clear_mask(self, background, foreground, state, mode):
        doc = document.ensure(state)
        image, mask, notes = self._sync(doc, background, foreground)
        if image is None:
            return (gr.skip(), *self._info(doc, mode, "There is no image yet."))
        if imaging.mask_is_empty(mask):
            return (gr.skip(), *self._info(doc, mode, "There is no mask to clear.", notes))
        doc.checkpoint("clear mask")
        doc.commit(image, None)
        return (None, *self._info(doc, mode, "Mask cleared. The image is untouched.", notes))

    def invert_mask(self, background, foreground, state, mode):
        doc = document.ensure(state)
        image, mask, notes = self._sync(doc, background, foreground)
        if image is None:
            return (gr.skip(), *self._info(doc, mode, "There is no image yet."))
        doc.checkpoint("invert mask")
        doc.commit(image, imaging.invert_mask(mask, image.size))
        # Inverting a mask that covered everything leaves nothing: clear the layer.
        layer = self._layer(doc.mask, doc.image.size) if doc.has_mask else None
        return (layer, *self._info(doc, mode, "Mask inverted.", notes))

    # -- callbacks: expand --------------------------------------------------

    def _sides(self, values, snap_choice) -> typing.Tuple[int, int, int, int]:
        step = outpaint.snap_from_choice(snap_choice)
        return tuple(outpaint.snap_value(value, step) for value in values)

    def expand_preview(self, state, left, right, top, bottom, snap_choice):
        doc = document.ensure(state)
        return outpaint.describe(doc.size, self._sides((left, right, top, bottom), snap_choice))

    def add_side(self, side, amount, state, left, right, top, bottom, snap_choice):
        """One tap adds the chosen amount to one side."""
        step = outpaint.snap_from_choice(snap_choice)
        values = {"Left": left, "Right": right, "Top": top, "Bottom": bottom}
        try:
            addition = int(amount)
        except (TypeError, ValueError):
            addition = 0
        values[side] = outpaint.snap_value((values.get(side) or 0) + addition, step)
        sides = tuple(values[name] for name in outpaint.SIDES)
        doc = document.ensure(state)
        return (*sides, outpaint.describe(doc.size, sides))

    def clear_sides(self, state):
        doc = document.ensure(state)
        return 0, 0, 0, 0, outpaint.describe(doc.size, (0, 0, 0, 0))

    def apply_expand(self, background, foreground, state, mode, left, right, top, bottom, overlap, fill, snap_choice):
        doc = document.ensure(state)
        image, mask, notes = self._sync(doc, background, foreground)
        if image is None:
            return self._unchanged(doc, mode, "There is no image to expand.")

        sides = self._sides((left, right, top, bottom), snap_choice)
        try:
            expanded, new_mask, info = outpaint.expand(image, mask, sides, int(overlap or 0), fill)
        except ValueError as error:
            return self._unchanged(doc, mode, str(error), notes)

        if info["overlap"]:
            notes.append(f"the mask reaches {info['overlap']}px back into the original so the model has room to blend")
        doc.checkpoint("expand")
        doc.commit(expanded, new_mask)
        doc.has_expansion = True
        doc.expansion = dict(info)
        return self._commit(
            doc,
            "mask",
            f"Expanded to {info['to'][0]} × {info['to'][1]}. The new area is masked — "
            "paint or erase to refine it, then send.",
            notes,
            sides=sides,
        )

    # -- callbacks: history --------------------------------------------------

    def undo(self, state, mode):
        doc = document.ensure(state)
        label = doc.undo()
        if label is None:
            return self._unchanged(doc, mode, "Nothing to undo here. The canvas's own ↩️ undoes strokes.")
        return self._commit(doc, mode, f"Undid {label}.")

    def redo(self, state, mode):
        doc = document.ensure(state)
        label = doc.redo()
        if label is None:
            return self._unchanged(doc, mode, "Nothing to redo.")
        return self._commit(doc, mode, f"Redid {label}.")

    def reset(self, state, mode):
        doc = document.ensure(state)
        if doc.original is None:
            return self._unchanged(doc, mode, "There is nothing to reset to.")
        doc.checkpoint("reset")
        doc.load(doc.original, doc.origin, doc.filename)
        return self._commit(doc, "crop", "Back to the image as it arrived.")

    def commit_foreground(self, state):
        """The mask layer, written once the canvas has the image it belongs to."""
        doc = document.ensure(state)
        if doc.has_mask and doc.image is not None:
            return self._layer(doc.mask, doc.image.size)
        return gr.skip()

    # -- callbacks: send ----------------------------------------------------

    def send(self, background, foreground, state, mode, destination_choice, smoothing):
        """The handoff: the image into the destination's own input.

        For Inpaint the mask follows in a later step (``send_mask``), once
        the Inpaint canvas has taken the image, because that canvas can only
        hold a mask layer of the size it currently has. The last value
        returned is the browser's instruction: which tab to switch to, and
        for Inpaint the size the canvas must reach first.
        """
        doc = document.ensure(state)
        skips = [gr.skip() for _ in self.image_targets]
        image, mask, notes = self._sync(doc, background, foreground)
        if image is None:
            return (*skips, *self._info(doc, mode, "There is no image to send."), "")

        target = resolve_destination(destination_choice, doc.has_mask, doc.has_expansion)
        label = DESTINATION_LABELS[target]
        if target not in self.targets:
            return (*skips, *self._info(doc, mode, f"{label} was not found in this WebUI, so nothing was sent."), "")

        outgoing = imaging.to_rgba(doc.image)
        if imaging.has_alpha_content(outgoing):
            fill_name = str(settings.get(settings.SEND_FILL, "Neutral gray"))
            color = imaging.fill_color(fill_name, outgoing)
            outgoing = imaging.flatten(outgoing, color)
            notes.append(f"transparent pixels were filled with rgb{color} for {label}")

        if target == "inpaint":
            if doc.has_mask and smoothing != "Off":
                notes.append(f"mask edge smoothing: {smoothing}")
            doc.pending_send = {"target": target, "smoothing": smoothing, "size": outgoing.size}
            instruction = f"inpaint:{outgoing.width}x{outgoing.height}"
        else:
            doc.pending_send = None
            instruction = target
            if doc.has_mask:
                notes.append(f"the mask was not sent: {label} takes an image only")

        doc.last_send = label
        log_quietly(
            {
                "destination": f"Canvas -> {label}",
                "outcome": f"sent {outgoing.width}x{outgoing.height}" + (" with mask" if target == "inpaint" and doc.has_mask else ""),
                "steps": notes,
            }
        )
        outputs = [outgoing if key == target else gr.skip() for key in self.image_targets]
        return (*outputs, *self._info(doc, mode, f"Sent to {label}.", notes), instruction)

    def send_mask(self, state, instruction):
        """The mask layer for the Inpaint canvas, or a clear when the send
        carried no mask. Nothing for any other destination."""
        doc = document.ensure(state)
        pending = getattr(doc, "pending_send", None)
        if not str(instruction or "").startswith("inpaint") or not pending:
            return gr.skip()
        doc.pending_send = None
        if not doc.has_mask:
            return None
        mask = imaging.smooth_mask(doc.mask, pending.get("smoothing", "Off"))
        return self._layer(mask, pending["size"])

    def save_copy(self, background, foreground, state):
        doc = document.ensure(state)
        image, _mask, _notes = self._sync(doc, background, foreground)
        if image is None:
            return gr.update()
        import tempfile

        handle = tempfile.NamedTemporaryFile(prefix="minipaint-canvas-", suffix=".png", delete=False)
        handle.close()
        doc.image.save(handle.name, format="PNG")
        return gr.update(value=handle.name, visible=True)

    # -- layout --------------------------------------------------------------

    def build(self) -> None:
        announce_send_log("the touch Canvas")
        self.targets = host.destinations()
        self.image_targets = [key for key in ("img2img", "inpaint", "extras") if key in self.targets]
        self.target_order = list(self.image_targets)

        with gr.Column(elem_id=_id("root"), elem_classes=["minipaint-canvas-root"]):
            state = gr.State(None)
            # A textbox rather than a State: the browser follows its changes.
            mode_state = gr.Textbox("crop", visible=False, elem_id=_id("mode"))

            with gr.Row(elem_id=_id("topbar"), elem_classes=["minipaint-topbar"]):
                open_btn = gr.UploadButton(
                    "Open", file_types=["image"], type="filepath", elem_id=_id("open"), elem_classes=["minipaint-action"]
                )
                undo_btn = gr.Button("Undo", elem_id=_id("undo"), elem_classes=["minipaint-action"])
                redo_btn = gr.Button("Redo", elem_id=_id("redo"), elem_classes=["minipaint-action"])
                fit_btn = gr.Button("Fit", elem_id=_id("fit"), elem_classes=["minipaint-action"])
                focus_btn = gr.Button("Focus", elem_id=_id("focus"), elem_classes=["minipaint-action", "minipaint-focus-enter"])
                focus_exit_btn = gr.Button(
                    "Exit focus", elem_id=_id("focus_exit"), elem_classes=["minipaint-action", "minipaint-focus-exit"]
                )
                send_btn = gr.Button(
                    "Send to img2img", variant="primary", elem_id=_id("send"), elem_classes=["minipaint-action", "minipaint-send"]
                )

            self.surface = surface.Surface(
                _id("surface"),
                height_percent=self.canvas_height,
                brush_width=self.brush_width,
                attach_js=ATTACH_JS,
            )

            status = gr.Markdown(
                "**No image yet.** Open a file, drop or paste one onto the canvas, or press 🖌️ under a txt2img, img2img or Extras result.",
                elem_id=_id("status"),
                elem_classes=["minipaint-status"],
            )

            with gr.Row(elem_id=_id("modebar"), elem_classes=["minipaint-modebar"]):
                mode_btns = {
                    mode: gr.Button(
                        MODE_LABELS[mode],
                        elem_id=_id(f"mode_{mode}"),
                        elem_classes=["minipaint-mode"],
                        variant="primary" if mode == "crop" else "secondary",
                    )
                    for mode in MODES
                }

            with gr.Group(elem_id=_id("options"), elem_classes=["minipaint-options"]):
                crop_panel, crop = self._crop_panel()
                mask_panel, mask = self._mask_panel()
                expand_panel, expand = self._expand_panel()

            destination, reset_btn, save_btn, save_file = self._more_panel()

            # Hidden wires between chained events. Values, not DOM.
            crop_box = gr.Textbox("", visible=False, elem_id=_id("crop_box"))
            original_size = gr.Textbox("", visible=False, elem_id=_id("original_size"))
            wait_flag = gr.Textbox("", visible=False, elem_id=_id("wait"))
            switch_box = gr.Textbox("", visible=False, elem_id=_id("switch"))
            event_kind = gr.Textbox("", visible=False, elem_id=_id("event"))

        self._wire(
            state=state,
            mode_state=mode_state,
            status=status,
            open_btn=open_btn,
            undo_btn=undo_btn,
            redo_btn=redo_btn,
            fit_btn=fit_btn,
            focus_btn=focus_btn,
            focus_exit_btn=focus_exit_btn,
            send_btn=send_btn,
            mode_btns=mode_btns,
            panels={"crop": crop_panel, "mask": mask_panel, "expand": expand_panel},
            crop=crop,
            mask=mask,
            expand=expand,
            destination=destination,
            reset_btn=reset_btn,
            save_btn=save_btn,
            save_file=save_file,
            crop_box=crop_box,
            original_size=original_size,
            wait_flag=wait_flag,
            switch_box=switch_box,
            event_kind=event_kind,
        )

    # -- panels --------------------------------------------------------------

    def _crop_panel(self):
        with gr.Column(elem_id=_id("panel_crop"), elem_classes=["minipaint-panel"]) as panel:
            aspect = gr.Radio(
                ASPECTS, value="Free", label="Aspect", show_label=False, elem_id=_id("crop_aspect"), elem_classes=["minipaint-chips"]
            )
            with gr.Accordion("Custom ratio", open=False, elem_id=_id("crop_custom")):
                with gr.Row():
                    width = gr.Number(label="Width", value=1024, precision=0, minimum=1, elem_id=_id("crop_custom_w"))
                    height = gr.Number(label="Height", value=1024, precision=0, minimum=1, elem_id=_id("crop_custom_h"))
            with gr.Row(elem_classes=["minipaint-panel-actions"]):
                gr.Markdown(
                    "Drag the image under the frame, pinch or scroll to zoom, drag a corner of the frame to resize it. "
                    "**Apply Crop** keeps what is inside the frame.",
                    elem_classes=["minipaint-hint"],
                )
                apply = gr.Button("Apply Crop", variant="primary", elem_id=_id("crop_apply"), elem_classes=["minipaint-apply"])
        return panel, {"aspect": aspect, "width": width, "height": height, "apply": apply}

    def _mask_panel(self):
        with gr.Column(elem_id=_id("panel_mask"), elem_classes=["minipaint-panel"], visible=False) as panel:
            with gr.Row():
                tool = gr.Radio(TOOLS, value="Paint", label="Tool", show_label=False, elem_id=_id("mask_tool"), elem_classes=["minipaint-chips"])
                size = gr.Slider(1, 100, value=self.brush_width, step=1, label="Brush size", elem_id=_id("mask_size"))
            with gr.Row():
                clear = gr.Button("Clear Mask", elem_id=_id("mask_clear"))
                invert = gr.Button("Invert Mask", elem_id=_id("mask_invert"))
                smoothing = gr.Radio(
                    imaging.SMOOTHING_LEVELS,
                    value="Off",
                    label="Edge smoothing (applied when sending)",
                    elem_id=_id("mask_smoothing"),
                    elem_classes=["minipaint-chips"],
                )
            gr.Markdown(
                "Paint over what should change. Two fingers pan and zoom; Move lets one finger pan. "
                "The canvas's own ↩️ undoes a stroke, 🔄 clears all strokes and refits the image.",
                elem_classes=["minipaint-hint"],
            )
        return panel, {"tool": tool, "size": size, "clear": clear, "invert": invert, "smoothing": smoothing}

    def _expand_panel(self):
        with gr.Column(elem_id=_id("panel_expand"), elem_classes=["minipaint-panel"], visible=False) as panel:
            with gr.Row():
                amount = gr.Radio(outpaint.AMOUNTS, value="128", label="Add", elem_id=_id("expand_amount"), elem_classes=["minipaint-chips"])
                side_buttons = {
                    side: gr.Button(side, elem_id=_id(f"expand_{side.lower()}"), elem_classes=["minipaint-side"]) for side in outpaint.SIDES
                }
                clear_sides = gr.Button("Clear", elem_id=_id("expand_clear"), elem_classes=["minipaint-side"])
            preview = gr.Markdown("No image yet.", elem_id=_id("expand_preview"), elem_classes=["minipaint-preview"])
            with gr.Accordion("Advanced expansion", open=False, elem_id=_id("expand_advanced")):
                with gr.Row():
                    numbers = {
                        side: gr.Number(label=side, value=0, precision=0, minimum=0, elem_id=_id(f"expand_num_{side.lower()}"))
                        for side in outpaint.SIDES
                    }
                with gr.Row():
                    overlap = gr.Slider(0, 256, value=32, step=8, label="Overlap into the original (px)", elem_id=_id("expand_overlap"))
                    fill = gr.Dropdown(outpaint.FILL_POLICIES, value=outpaint.DEFAULT_FILL, label="New area", elem_id=_id("expand_fill"))
                    snap = gr.Dropdown(settings.SNAP_CHOICES, value=self.snap_default, label="Snap to", elem_id=_id("expand_snap"))
            with gr.Row(elem_classes=["minipaint-panel-actions"]):
                gr.Markdown("Adds room around the image and masks the new area automatically.", elem_classes=["minipaint-hint"])
                apply = gr.Button("Apply Expand", variant="primary", elem_id=_id("expand_apply"), elem_classes=["minipaint-apply"])
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
        with gr.Accordion("More", open=False, elem_id=_id("more")):
            with gr.Row():
                destination = gr.Dropdown(DESTINATIONS, value="Auto", label="Send to", elem_id=_id("destination"))
                reset_btn = gr.Button("Reset to original", elem_id=_id("reset"))
                save_btn = gr.Button("Save a copy", elem_id=_id("save"))
            save_file = gr.File(label="Saved copy", interactive=False, visible=False, elem_id=_id("save_file"))
            gr.Markdown(
                "Auto sends a plain image to img2img, and an image with a mask to Inpaint. "
                "Undo and Redo cover Open, Apply Crop, Apply Expand, Clear and Invert; "
                "strokes are undone on the canvas itself.",
                elem_classes=["minipaint-hint"],
            )
        return destination, reset_btn, save_btn, save_file

    # -- wiring --------------------------------------------------------------

    def _wire(self, **parts) -> None:
        state = parts["state"]
        mode_state = parts["mode_state"]
        status = parts["status"]
        send_btn = parts["send_btn"]
        crop = parts["crop"]
        mask = parts["mask"]
        expand = parts["expand"]
        panels = parts["panels"]
        mode_btns = parts["mode_btns"]
        background = self.surface.background
        foreground = self.surface.foreground
        wait_flag = parts["wait_flag"]
        switch_box = parts["switch_box"]

        side_inputs = [expand["numbers"][side] for side in outpaint.SIDES]
        preview_inputs = [state] + side_inputs + [expand["snap"]]

        mode_outputs = [
            mode_state,
            panels["crop"],
            panels["mask"],
            panels["expand"],
            mode_btns["crop"],
            mode_btns["mask"],
            mode_btns["expand"],
            send_btn,
            mask["tool"],
        ]
        info_outputs = [state, status, expand["preview"], crop["aspect"], parts["original_size"], wait_flag, *mode_outputs]
        commit_outputs = [background, foreground, *info_outputs]
        canvas_inputs = [background, foreground, state, mode_state]

        def structural(event, fn, inputs, js=None):
            """image -> wait for the canvas -> mask layer. See the module docstring."""
            return (
                event(fn, inputs=inputs, outputs=commit_outputs, js=js or _mark_js(len(inputs)))
                .then(_noop, js=WAIT_JS, inputs=[wait_flag], outputs=None)
                .then(self.commit_foreground, inputs=[state], outputs=[foreground])
            )

        # -- modes: panels and labels from Python; the browser follows the
        # mode textbox, whichever step changed it
        for mode, button in mode_btns.items():
            button.click(lambda state, mode=mode: self.set_mode(mode, state), inputs=[state], outputs=mode_outputs)
        mode_state.change(None, js=MODE_JS, inputs=[mode_state])

        # -- what the canvas holds, when a file is opened, dropped or pasted onto it
        background.input(
            self.on_canvas_image,
            inputs=[background, state, mode_state, parts["event_kind"]],
            outputs=[foreground, *info_outputs],
            js=CANVAS_INPUT_JS,
        )

        # -- crop: the frame lives in the browser; Apply reads it
        aspect_inputs = [crop["aspect"], crop["width"], crop["height"], parts["original_size"]]
        crop["aspect"].change(None, js=ASPECT_JS, inputs=aspect_inputs)
        for number in (crop["width"], crop["height"]):
            number.change(None, js=ASPECT_JS, inputs=aspect_inputs)
        structural(crop["apply"].click, self.apply_crop, canvas_inputs + [parts["crop_box"]], js=CROP_JS)

        # -- mask: tool and size are browser-only; clear and invert write the layer
        mask["tool"].change(None, js=TOOL_JS, inputs=[mask["tool"]])
        mask["size"].change(None, js=SIZE_JS, inputs=[mask["size"]])
        mask["size"].release(None, js=SIZE_JS, inputs=[mask["size"]])
        mask["clear"].click(self.clear_mask, inputs=canvas_inputs, outputs=[foreground, *info_outputs])
        mask["invert"].click(self.invert_mask, inputs=canvas_inputs, outputs=[foreground, *info_outputs])

        # -- expand
        for side, button in expand["sides"].items():
            button.click(
                lambda amount, state, left, right, top, bottom, snap, side=side: self.add_side(
                    side, amount, state, left, right, top, bottom, snap
                ),
                inputs=[expand["amount"], state] + side_inputs + [expand["snap"]],
                outputs=side_inputs + [expand["preview"]],
            )
        expand["clear"].click(self.clear_sides, inputs=[state], outputs=side_inputs + [expand["preview"]])
        for control in side_inputs + [expand["snap"]]:
            control.change(self.expand_preview, inputs=preview_inputs, outputs=[expand["preview"]])
        structural(
            expand["apply"].click,
            self.apply_expand,
            canvas_inputs + side_inputs + [expand["overlap"], expand["fill"], expand["snap"]],
        )

        # -- open, history, reset
        structural(parts["open_btn"].upload, self.open_file, [parts["open_btn"], state, mode_state])
        structural(parts["undo_btn"].click, self.undo, [state, mode_state])
        structural(parts["redo_btn"].click, self.redo, [state, mode_state])
        structural(parts["reset_btn"].click, self.reset, [state, mode_state])

        # -- view: fit and focus are browser-only
        parts["fit_btn"].click(None, js=FIT_JS)
        parts["focus_btn"].click(None, js=FOCUS_ON_JS)
        parts["focus_exit_btn"].click(None, js=FOCUS_OFF_JS)

        # -- save
        parts["save_btn"].click(self.save_copy, inputs=[background, foreground, state], outputs=[parts["save_file"]])

        # -- send: an ordinary Gradio output into the host's own inputs, then
        # the host's own tab switch; for Inpaint, the mask layer once that
        # canvas has the image.
        target_components = [self.targets[key] for key in self.image_targets]
        sent = send_btn.click(
            self.send,
            inputs=[background, foreground, state, mode_state, parts["destination"], mask["smoothing"]],
            outputs=target_components + info_outputs + [switch_box],
        )
        sent.then(None, js=SWITCH_JS, inputs=[switch_box], outputs=None)
        if "inpaint_mask" in self.targets:
            inpaint_uuid = getattr(self.targets["inpaint"], "elem_id", "") or ""
            sent.then(_noop, js=_host_wait_js(inpaint_uuid), inputs=[switch_box], outputs=None).then(
                self.send_mask, inputs=[state, switch_box], outputs=[self.targets["inpaint_mask"]]
            )

        # -- receive: the small button next to "send to extras" in each output panel
        for tab, button, gallery in host.receive_buttons():
            structural(
                button.click,
                lambda payload, state, mode, tab=tab: self.receive(payload, state, mode, tab),
                [gallery, state, mode_state],
                js=PICK_JS,
            ).then(None, js=SWITCH_CANVAS_JS)


def create_ui() -> TouchCanvas:
    canvas = TouchCanvas()
    canvas.build()
    return canvas
