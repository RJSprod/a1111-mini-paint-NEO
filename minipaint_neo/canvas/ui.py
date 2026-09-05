"""The touch-first Canvas: one large ImageEditor with ordinary Gradio controls.

Layout, top to bottom: an action bar, the editor, a status line, the three
modes (Crop / Mask / Expand), one panel of options per mode, and a "More"
accordion. Buttons are Buttons and sliders are Sliders, so the host theme
decides how everything looks.

What goes to Python is the low-frequency work: receiving or opening an
image, committing a crop, expanding the canvas, clearing or inverting the
mask, undo/redo of those steps, and the handoff to img2img. Painting and crop
dragging stay inside the editor.

Every step that replaces the editor's contents is three chained events:
stage (Python reads the editor and queues the next document), flush (the
browser winds the editor's own history back, which is the only way its crop
box resets on this Gradio), and commit (Python pushes the queued document).
See ``document.py`` and ``javascript/minipaint_canvas.js``.
"""

from __future__ import annotations

import os.path
import typing

import gradio as gr

from .. import settings
from ..send_log import announce_send_log, log_quietly
from . import document, host, imaging, outpaint

MODES = ("crop", "mask", "expand")
MODE_LABELS = {"crop": "Crop", "mask": "Mask", "expand": "Expand"}
ASPECTS = ["Free", "Original", "1:1", "4:3", "3:4", "16:9", "9:16", "3:2", "2:3", "Custom"]
DESTINATIONS = ["Auto", "img2img", "Inpaint", "Extras"]
DESTINATION_KEYS = {"img2img": "img2img", "Inpaint": "inpaint", "Extras": "extras"}
DESTINATION_LABELS = {"img2img": "img2img", "inpaint": "img2img Inpaint", "extras": "Extras"}

PREFIX = "minipaint_canvas"


def _id(name: str) -> str:
    return f"{PREFIX}_{name}"


# All browser-side helpers live in javascript/minipaint_canvas.js and are only
# ever called from these events, never at startup.
_JS = "window.minipaintCanvas"
FLUSH_JS = f"(flag) => (flag === 'flush' && {_JS}) ? {_JS}.flushEditor() : undefined"
FLUSH_ALWAYS_JS = f"() => {_JS} ? {_JS}.flushEditor() : undefined"
PICK_JS = f"(gallery, state, mode) => [{_JS} ? {_JS}.pickGalleryImage(gallery) : null, state, mode]"
SWITCH_JS = f"(target) => {_JS} && {_JS}.switchTo(target)"
SWITCH_CANVAS_JS = f"() => {_JS} && {_JS}.switchTo('canvas')"
FOCUS_ON_JS = f"() => {_JS} && {_JS}.setFocus(true)"
FOCUS_OFF_JS = f"() => {_JS} && {_JS}.setFocus(false)"


def _mode_js(mode: str) -> str:
    return f"() => {_JS} && {_JS}.onMode('{mode}')"


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


def aspect_constraint(choice: str, original_size, custom_width, custom_height) -> str:
    """The editor's crop_size string for an aspect choice. Empty means free."""
    if choice == "Original" and original_size:
        return f"{int(original_size[0])}:{int(original_size[1])}"
    if choice == "Custom":
        try:
            width, height = int(custom_width or 0), int(custom_height or 0)
        except (TypeError, ValueError):
            return ""
        return f"{width}:{height}" if width > 0 and height > 0 else ""
    if choice and ":" in choice:
        return choice
    return ""


class TouchCanvas:
    """Builds the tab and owns its callbacks. One instance per mounted UI."""

    def __init__(self) -> None:
        self.mask_color = imaging.parse_color(settings.get(settings.MASK_COLOR))
        self.canvas_height = settings.canvas_height_percent()
        self.brush_size = settings.brush_size()
        self.snap_default = str(settings.get(settings.EXPAND_SNAP, "8"))
        if self.snap_default not in settings.SNAP_CHOICES:
            self.snap_default = "8"
        self.targets: typing.Dict[str, typing.Any] = {}
        self.target_order: typing.List[str] = []

    # -- replies -------------------------------------------------------------

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
        )

    def _reply(
        self,
        doc: document.Document,
        mode: str,
        message: str,
        notes: typing.Sequence[str] = (),
        *,
        push: bool = False,
        sides: typing.Sequence[int] = (0, 0, 0, 0),
        reset_aspect: bool = False,
    ) -> tuple:
        """The outputs every commit-style callback returns, in COMMIT order."""
        notes = [note for note in notes if note]
        if doc.image is not None:
            size = imaging.megapixels(doc.image.size)
            if size > outpaint.WARN_MEGAPIXELS:
                notes.append(
                    f"{size} megapixels is a lot for a browser editor, especially on a tablet"
                )

        if push:
            if doc.image is not None:
                editor = gr.update(
                    value=imaging.editor_value(doc.image, doc.mask, self.mask_color),
                    visible=True,
                    crop_size="",
                )
            else:
                editor = gr.update(value=None, visible=False)
        else:
            editor = gr.skip()

        summary = f"**{doc.describe()}**"
        if doc.has_image:
            target = resolve_destination("Auto", doc.has_mask, doc.has_expansion)
            summary += f" — Auto sends to {DESTINATION_LABELS[target]}"

        return (
            editor,
            gr.update(visible=not doc.has_image),
            doc,
            _status(f"{summary}  \n{message}" if message else summary, notes),
            outpaint.describe(doc.size, sides) if push else gr.skip(),
            gr.update(value="Free") if reset_aspect else gr.skip(),
            *self._mode_updates(mode, doc),
        )

    def _staged(self, doc: document.Document, message: str, flush: bool = True) -> tuple:
        """The outputs of a stage callback: state, a status line, and whether
        the browser should wind the editor back before the commit."""
        return doc, _status(message), "flush" if flush else ""

    # -- callbacks: modes ---------------------------------------------------

    def set_mode(self, mode: str, state):
        doc = document.ensure(state)
        return self._mode_updates(mode if mode in MODES else "crop", doc)

    def set_aspect(self, choice, custom_width, custom_height, state):
        doc = document.ensure(state)
        original = doc.original.size if doc.original is not None else doc.size
        return gr.update(crop_size=aspect_constraint(choice, original, custom_width, custom_height))

    # -- callbacks: receive and open ---------------------------------------

    def receive(self, payload, state, mode, tab: str):
        """An image arriving from a txt2img / img2img / Extras gallery."""
        doc = document.ensure(state)
        image = host.gallery_image(payload)
        if image is None:
            return self._reply(doc, mode, "Pick an image in the gallery first.")

        notes = []
        if doc.has_image:
            doc.checkpoint("receive")
            notes.append("the previous image is one Undo away")
        doc.load(imaging.to_rgba(image), tab)
        log_quietly({"destination": f"{tab} -> Canvas", "outcome": f"received {doc.image.width}x{doc.image.height}"})
        return self._reply(doc, "crop", f"Received from {tab}.", notes, push=True, reset_aspect=True)

    def stage_open(self, file, state):
        doc = document.ensure(state)
        path = getattr(file, "name", file)
        if not path:
            return self._staged(doc, "No file was chosen.", flush=False)
        try:
            image = imaging.open_file(str(path))
        except Exception as error:  # Pillow raises a family of these
            return self._staged(doc, f"Could not open this image format. ({error})", flush=False)

        doc.stage(
            "open",
            "Opened.",
            ["the previous image is one Undo away"] if doc.has_image else (),
            image=image,
            load=True,
            origin="file",
            filename=os.path.basename(str(path)),
            mode="crop",
        )
        return self._staged(doc, "Opening…")

    # -- callbacks: crop ----------------------------------------------------

    def _read(self, editor, doc: document.Document):
        """The editor's current pixels, with a padded crop trimmed off."""
        image, mask, notes = imaging.read_editor(editor)
        if image is None:
            return None, None, notes
        # A crop that pads with transparency is a component quirk, not the
        # user's picture - but only when the picture going in had no
        # transparency of its own, the one case where trimming cannot eat pixels.
        if doc.image is not None and not imaging.has_alpha_content(doc.image):
            image, mask, note = imaging.trim_transparent_frame(image, mask)
            if note:
                notes.append(note)
        return image, mask, notes

    def stage_crop(self, editor, state):
        doc = document.ensure(state)
        image, mask, notes = self._read(editor, doc)
        if image is None:
            return self._staged(doc, "There is no image to crop.", flush=False)
        if doc.image is not None and image.size == doc.image.size:
            return self._staged(
                doc,
                "Nothing to crop yet — drag the handles on the image, or pick an aspect, then Apply.",
                flush=False,
            )
        doc.stage(
            "crop",
            f"Cropped to {image.width} × {image.height}.",
            notes,
            image=image,
            mask=mask,
        )
        return self._staged(doc, "Cropping…")

    # -- callbacks: mask ----------------------------------------------------

    def stage_clear_mask(self, editor, state):
        doc = document.ensure(state)
        image, mask, notes = self._read(editor, doc)
        if image is None:
            return self._staged(doc, "There is no image yet.", flush=False)
        if imaging.mask_is_empty(mask):
            return self._staged(doc, "There is no mask to clear.", flush=False)
        doc.stage("clear mask", "Mask cleared. The image is untouched.", notes, image=image, mask=None)
        return self._staged(doc, "Clearing…")

    def stage_invert_mask(self, editor, state):
        doc = document.ensure(state)
        image, mask, notes = self._read(editor, doc)
        if image is None:
            return self._staged(doc, "There is no image yet.", flush=False)
        doc.stage(
            "invert mask",
            "Mask inverted.",
            notes,
            image=image,
            mask=imaging.invert_mask(mask, image.size),
        )
        return self._staged(doc, "Inverting…")

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

    def stage_expand(self, editor, state, left, right, top, bottom, overlap, fill, snap_choice):
        doc = document.ensure(state)
        image, mask, notes = self._read(editor, doc)
        if image is None:
            return self._staged(doc, "There is no image to expand.", flush=False)

        sides = self._sides((left, right, top, bottom), snap_choice)
        try:
            expanded, new_mask, info = outpaint.expand(image, mask, sides, int(overlap or 0), fill)
        except ValueError as error:
            return self._staged(doc, str(error), flush=False)

        if info["overlap"]:
            notes.append(
                f"the mask reaches {info['overlap']}px back into the original so the model has room to blend"
            )
        doc.stage(
            "expand",
            f"Expanded to {info['to'][0]} × {info['to'][1]}. The new area is masked — "
            "refine it with the brush if you like, then send.",
            notes,
            image=expanded,
            mask=new_mask,
            expansion=info,
            mode="mask",
        )
        return self._staged(doc, "Expanding…")

    # -- callbacks: history --------------------------------------------------

    def stage_undo(self, state):
        doc = document.ensure(state)
        if not doc.history:
            return self._staged(doc, "Nothing to undo here. The editor's own ↶ undoes strokes and crop drags.", flush=False)
        doc.stage("undo", "", restore="undo")
        return self._staged(doc, "Undoing…")

    def stage_redo(self, state):
        doc = document.ensure(state)
        if not doc.future:
            return self._staged(doc, "Nothing to redo.", flush=False)
        doc.stage("redo", "", restore="redo")
        return self._staged(doc, "Redoing…")

    def stage_reset(self, state):
        doc = document.ensure(state)
        if doc.original is None:
            return self._staged(doc, "There is nothing to reset to.", flush=False)
        doc.stage(
            "reset",
            "Back to the image as it arrived.",
            image=doc.original,
            load=True,
            origin=doc.origin,
            filename=doc.filename,
            mode="crop",
        )
        return self._staged(doc, "Resetting…")

    # -- the commit half of every structural step --------------------------

    def commit(self, state, mode, left, right, top, bottom, snap_choice):
        doc = document.ensure(state)
        pending = doc.commit_pending()
        if pending is None:
            return self._reply(doc, mode, "")

        message = pending["message"]
        if pending["restore"] == "undo":
            message = f"Undid {pending['label']}."
        elif pending["restore"] == "redo":
            message = f"Redid {pending['label']}."

        new_mode = pending.get("mode") or mode
        return self._reply(
            doc,
            new_mode,
            message,
            pending["notes"],
            push=True,
            sides=self._sides((left, right, top, bottom), snap_choice),
            reset_aspect=True,
        )

    # -- callbacks: send ----------------------------------------------------

    def send(self, editor, state, mode, destination_choice, smoothing):
        doc = document.ensure(state)
        skips = [gr.skip() for _ in self.target_order]
        image, mask, notes = self._read(editor, doc)
        if image is None:
            return (*skips, *self._reply(doc, mode, "There is no image to send."), "")

        doc.commit(image, mask)
        target = resolve_destination(destination_choice, doc.has_mask, doc.has_expansion)
        label = DESTINATION_LABELS[target]
        if target not in self.targets:
            return (
                *skips,
                *self._reply(doc, mode, f"{label} was not found in this WebUI, so nothing was sent."),
                "",
            )

        outgoing = imaging.to_rgba(doc.image)
        if imaging.has_alpha_content(outgoing):
            fill_name = str(settings.get(settings.SEND_FILL, "Neutral gray"))
            color = imaging.fill_color(fill_name, outgoing)
            outgoing = imaging.flatten(outgoing, color)
            notes.append(f"transparent pixels were filled with rgb{color} for {label}")

        mask_out = imaging.smooth_mask(doc.mask, smoothing) if doc.has_mask else None
        if doc.has_mask and smoothing != "Off":
            notes.append(f"mask edge smoothing: {smoothing}")

        values = {}
        if target == "img2img":
            values["img2img"] = outgoing
            if doc.has_mask:
                notes.append("the mask was not sent: img2img takes an image only")
        elif target == "inpaint":
            values["inpaint"] = outgoing
            values["inpaint_mask"] = (
                imaging.inpaint_foreground(mask_out, outgoing.size, self.mask_color)
                if mask_out is not None
                else None
            )
        else:
            values["extras"] = outgoing
            if doc.has_mask:
                notes.append("the mask was not sent: Extras takes an image only")

        doc.last_send = label
        log_quietly(
            {
                "destination": f"Canvas -> {label}",
                "outcome": f"sent {outgoing.width}x{outgoing.height}"
                + (" with mask" if values.get("inpaint_mask") is not None else ""),
                "steps": notes,
            }
        )
        outputs = [values[key] if key in values else gr.skip() for key in self.target_order]
        return (
            *outputs,
            *self._reply(doc, mode, f"Sent to {label}.", notes),
            target,
        )

    def save_copy(self, editor, state):
        doc = document.ensure(state)
        image, mask, _notes = self._read(editor, doc)
        if image is None:
            return gr.update()
        doc.commit(image, mask)
        import tempfile

        handle = tempfile.NamedTemporaryFile(prefix="minipaint-canvas-", suffix=".png", delete=False)
        handle.close()
        doc.image.save(handle.name, format="PNG")
        return gr.update(value=handle.name, visible=True)

    # -- layout --------------------------------------------------------------

    def build(self) -> None:
        announce_send_log("the touch Canvas")
        color_hex = imaging.color_hex(self.mask_color)
        self.targets = host.destinations()
        self.target_order = [key for key in ("img2img", "inpaint", "inpaint_mask", "extras") if key in self.targets]

        with gr.Column(elem_id=_id("root"), elem_classes=["minipaint-canvas-root"]):
            state = gr.State(None)
            mode_state = gr.State("crop")

            # The one inline style: the canvas height from Settings, as a CSS
            # variable style.css reads. The block itself is hidden by CSS.
            gr.HTML(
                f"<style>#{_id('root')}{{--minipaint-canvas-height:{self.canvas_height}vh;}}</style>",
                elem_id=_id("style"),
            )

            with gr.Row(elem_id=_id("topbar"), elem_classes=["minipaint-topbar"]):
                open_btn = gr.UploadButton(
                    "Open",
                    file_types=["image"],
                    type="filepath",
                    elem_id=_id("open"),
                    elem_classes=["minipaint-action", host.NEEDS_EDITOR_CLASS],
                )
                undo_btn = gr.Button("Undo", elem_id=_id("undo"), elem_classes=["minipaint-action", host.NEEDS_EDITOR_CLASS])
                redo_btn = gr.Button("Redo", elem_id=_id("redo"), elem_classes=["minipaint-action", host.NEEDS_EDITOR_CLASS])
                focus_btn = gr.Button("Focus", elem_id=_id("focus"), elem_classes=["minipaint-action", "minipaint-focus-enter"])
                focus_exit_btn = gr.Button(
                    "Exit focus", elem_id=_id("focus_exit"), elem_classes=["minipaint-action", "minipaint-focus-exit"]
                )
                send_btn = gr.Button(
                    "Send to img2img",
                    variant="primary",
                    elem_id=_id("send"),
                    elem_classes=["minipaint-action", "minipaint-send", host.NEEDS_EDITOR_CLASS],
                )

            placeholder = gr.Markdown(
                "No image yet. **Open** a file, or press 🖌️ under a txt2img, img2img or Extras result.",
                elem_id=_id("empty"),
                elem_classes=["minipaint-empty"],
            )

            editor = gr.ImageEditor(
                label="Canvas",
                show_label=False,
                type="pil",
                image_mode="RGBA",
                # No upload or paste inside the editor: on this Gradio the
                # editor keeps its crop box across a new image, so every way
                # in goes through Open / receive, which reset it first.
                sources=[],
                transforms=["crop"],
                brush=gr.Brush(
                    default_size=self.brush_size,
                    colors=[color_hex, "#ffffff", "#000000"],
                    default_color=color_hex,
                    color_mode="fixed",
                ),
                eraser=gr.Eraser(default_size=self.brush_size),
                layers=False,
                # PNG on purpose: the default (webp) is lossy, and the mask
                # travels as a layer's alpha.
                format="png",
                show_download_button=False,
                show_share_button=False,
                interactive=True,
                visible=False,
                height=f"{self.canvas_height}vh",
                elem_id=_id("editor"),
                elem_classes=["minipaint-editor"],
            )

            status = gr.Markdown(
                "**No image**",
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

            # Hidden wires between chained events. Values, not DOM: nothing
            # in the browser reads or writes these directly.
            flush_flag = gr.Textbox("", visible=False, elem_id=_id("flush"))
            switch_box = gr.Textbox("", visible=False, elem_id=_id("switch"))

        self._wire(
            state=state,
            mode_state=mode_state,
            editor=editor,
            placeholder=placeholder,
            status=status,
            open_btn=open_btn,
            undo_btn=undo_btn,
            redo_btn=redo_btn,
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
            flush_flag=flush_flag,
            switch_box=switch_box,
        )

    # -- panels --------------------------------------------------------------

    def _crop_panel(self):
        with gr.Column(elem_id=_id("panel_crop"), elem_classes=["minipaint-panel"]) as panel:
            aspect = gr.Radio(
                ASPECTS,
                value="Free",
                label="Aspect",
                show_label=False,
                elem_id=_id("crop_aspect"),
                elem_classes=["minipaint-chips"],
            )
            with gr.Accordion("Custom ratio", open=False, elem_id=_id("crop_custom")):
                with gr.Row():
                    width = gr.Number(label="Width", value=1024, precision=0, minimum=1, elem_id=_id("crop_custom_w"))
                    height = gr.Number(label="Height", value=1024, precision=0, minimum=1, elem_id=_id("crop_custom_h"))
            with gr.Row(elem_classes=["minipaint-panel-actions"]):
                gr.Markdown(
                    "Drag the handles on the image. The editor's ↶ undoes a drag; "
                    "**Apply Crop** makes it permanent. Sending applies it too.",
                    elem_classes=["minipaint-hint"],
                )
                apply = gr.Button(
                    "Apply Crop",
                    variant="primary",
                    elem_id=_id("crop_apply"),
                    elem_classes=["minipaint-apply", host.NEEDS_EDITOR_CLASS],
                )
        return panel, {"aspect": aspect, "width": width, "height": height, "apply": apply}

    def _mask_panel(self):
        with gr.Column(elem_id=_id("panel_mask"), elem_classes=["minipaint-panel"], visible=False) as panel:
            with gr.Row():
                clear = gr.Button("Clear Mask", elem_id=_id("mask_clear"), elem_classes=[host.NEEDS_EDITOR_CLASS])
                invert = gr.Button("Invert Mask", elem_id=_id("mask_invert"), elem_classes=[host.NEEDS_EDITOR_CLASS])
                smoothing = gr.Radio(
                    imaging.SMOOTHING_LEVELS,
                    value="Off",
                    label="Edge smoothing (applied when sending)",
                    elem_id=_id("mask_smoothing"),
                    elem_classes=["minipaint-chips"],
                )
            gr.Markdown(
                "Paint over what should change. Brush, eraser, size and colour are in the "
                "editor's own toolbar under the image; its ↶ undoes a stroke.",
                elem_classes=["minipaint-hint"],
            )
        return panel, {"clear": clear, "invert": invert, "smoothing": smoothing}

    def _expand_panel(self):
        with gr.Column(elem_id=_id("panel_expand"), elem_classes=["minipaint-panel"], visible=False) as panel:
            with gr.Row():
                amount = gr.Radio(
                    outpaint.AMOUNTS,
                    value="128",
                    label="Add",
                    elem_id=_id("expand_amount"),
                    elem_classes=["minipaint-chips"],
                )
                side_buttons = {
                    side: gr.Button(side, elem_id=_id(f"expand_{side.lower()}"), elem_classes=["minipaint-side"])
                    for side in outpaint.SIDES
                }
                clear_sides = gr.Button("Clear", elem_id=_id("expand_clear"), elem_classes=["minipaint-side"])
            preview = gr.Markdown("No image yet.", elem_id=_id("expand_preview"), elem_classes=["minipaint-preview"])
            with gr.Accordion("Advanced expansion", open=False, elem_id=_id("expand_advanced")):
                with gr.Row():
                    numbers = {
                        side: gr.Number(
                            label=side, value=0, precision=0, minimum=0, elem_id=_id(f"expand_num_{side.lower()}")
                        )
                        for side in outpaint.SIDES
                    }
                with gr.Row():
                    overlap = gr.Slider(
                        0, 256, value=32, step=8, label="Overlap into the original (px)", elem_id=_id("expand_overlap")
                    )
                    fill = gr.Dropdown(
                        outpaint.FILL_POLICIES, value=outpaint.DEFAULT_FILL, label="New area", elem_id=_id("expand_fill")
                    )
                    snap = gr.Dropdown(
                        settings.SNAP_CHOICES, value=self.snap_default, label="Snap to", elem_id=_id("expand_snap")
                    )
            with gr.Row(elem_classes=["minipaint-panel-actions"]):
                gr.Markdown(
                    "Adds room around the image and masks the new area automatically.",
                    elem_classes=["minipaint-hint"],
                )
                apply = gr.Button(
                    "Apply Expand",
                    variant="primary",
                    elem_id=_id("expand_apply"),
                    elem_classes=["minipaint-apply", host.NEEDS_EDITOR_CLASS],
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
        with gr.Accordion("More", open=False, elem_id=_id("more")):
            with gr.Row():
                destination = gr.Dropdown(
                    DESTINATIONS, value="Auto", label="Send to", elem_id=_id("destination")
                )
                reset_btn = gr.Button("Reset to original", elem_id=_id("reset"), elem_classes=[host.NEEDS_EDITOR_CLASS])
                save_btn = gr.Button("Save a copy", elem_id=_id("save"), elem_classes=[host.NEEDS_EDITOR_CLASS])
            save_file = gr.File(label="Saved copy", interactive=False, visible=False, elem_id=_id("save_file"))
            gr.Markdown(
                "Auto sends a plain image to img2img, and an image with a mask to Inpaint. "
                "Undo and Redo cover Open, Apply Crop, Apply Expand, Clear and Invert; "
                "strokes and crop drags are undone inside the editor.",
                elem_classes=["minipaint-hint"],
            )
        return destination, reset_btn, save_btn, save_file

    # -- wiring --------------------------------------------------------------

    def _wire(self, **parts) -> None:
        state = parts["state"]
        mode_state = parts["mode_state"]
        editor = parts["editor"]
        status = parts["status"]
        send_btn = parts["send_btn"]
        crop = parts["crop"]
        mask = parts["mask"]
        expand = parts["expand"]
        panels = parts["panels"]
        mode_btns = parts["mode_btns"]
        flush_flag = parts["flush_flag"]
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
        ]
        commit_outputs = [
            editor,
            parts["placeholder"],
            state,
            status,
            expand["preview"],
            crop["aspect"],
            *mode_outputs,
        ]
        stage_outputs = [state, status, flush_flag]
        commit_inputs = [state, mode_state] + side_inputs + [expand["snap"]]

        def structural(event, stage_fn, inputs):
            """stage -> flush (browser) -> commit. See the module docstring."""
            return (
                event(stage_fn, inputs=inputs, outputs=stage_outputs)
                .then(_noop, js=FLUSH_JS, inputs=[flush_flag], outputs=None)
                .then(self.commit, inputs=commit_inputs, outputs=commit_outputs)
            )

        # -- modes
        for mode, button in mode_btns.items():
            button.click(
                lambda state, mode=mode: self.set_mode(mode, state),
                inputs=[state],
                outputs=mode_outputs,
            )
            button.click(None, js=_mode_js(mode))

        # -- crop
        aspect_inputs = [crop["aspect"], crop["width"], crop["height"], state]
        crop["aspect"].change(self.set_aspect, inputs=aspect_inputs, outputs=[editor])
        for number in (crop["width"], crop["height"]):
            number.change(self.set_aspect, inputs=aspect_inputs, outputs=[editor])
        structural(crop["apply"].click, self.stage_crop, [editor, state])

        # -- mask
        structural(mask["clear"].click, self.stage_clear_mask, [editor, state])
        structural(mask["invert"].click, self.stage_invert_mask, [editor, state])

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
            self.stage_expand,
            [editor, state] + side_inputs + [expand["overlap"], expand["fill"], expand["snap"]],
        )

        # -- open, history, reset
        structural(parts["open_btn"].upload, self.stage_open, [parts["open_btn"], state])
        structural(parts["undo_btn"].click, self.stage_undo, [state])
        structural(parts["redo_btn"].click, self.stage_redo, [state])
        structural(parts["reset_btn"].click, self.stage_reset, [state])

        # -- focus mode: purely a class on our own root
        parts["focus_btn"].click(None, js=FOCUS_ON_JS)
        parts["focus_exit_btn"].click(None, js=FOCUS_OFF_JS)

        # -- save
        parts["save_btn"].click(self.save_copy, inputs=[editor, state], outputs=[parts["save_file"]])

        # -- send: an ordinary Gradio output into the host's own input
        # components, then the host's own tab switch.
        target_components = [self.targets[key] for key in self.target_order]
        send_btn.click(
            self.send,
            inputs=[editor, state, mode_state, parts["destination"], mask["smoothing"]],
            outputs=target_components + commit_outputs + [switch_box],
        ).then(None, js=SWITCH_JS, inputs=[switch_box], outputs=None)

        # -- receive: the small button next to "send to extras" in each
        # output panel. Flush first (the editor is being replaced), then the
        # transfer, then the host's tab switch.
        for tab, button, gallery in host.receive_buttons():
            button.click(_noop, js=FLUSH_ALWAYS_JS, inputs=None, outputs=None).then(
                lambda payload, state, mode, tab=tab: self.receive(payload, state, mode, tab),
                js=PICK_JS,
                inputs=[gallery, state, mode_state],
                outputs=commit_outputs,
            ).then(None, js=SWITCH_CANVAS_JS)


def create_ui() -> TouchCanvas:
    canvas = TouchCanvas()
    canvas.build()
    return canvas
