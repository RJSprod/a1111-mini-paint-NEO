"""The touch-first Canvas: the WebUI's own canvas, with ordinary Gradio
controls around it, laid out the way an image editor is.

Left, top to bottom: one row of actions (Open, Undo, Redo, the mode -
Crop / Mask / Expand / Layers - Panels, Focus, Send), the canvas, and one
status line. Right: a rail of panels, one per mode, with the layer list in
the Layers panel and an "Options" accordion (destination, reset, save a copy)
at the bottom. The canvas takes the height the window has left and the rail
scrolls inside that height, so the whole tab is in view without scrolling
and nothing floats over the picture; on a narrow window the rail moves under
the canvas. Buttons are Buttons and sliders are Sliders, so the host theme
decides how everything looks; the canvas is the same one img2img uses.

What goes to Python is the low-frequency work: receiving or opening an
image, committing a crop, expanding, clearing or inverting the mask, every
layer step, undo and redo of those, and the handoff to img2img, Extras or
ImageStitch. Painting, panning and zooming stay in the browser.

The canvas takes its image and its scribble layer through two hidden
textboxes, the way the host's own canvases do. The scribble layer can only
be drawn once the image has finished loading, so every step that changes
the image is three chained events: write the image, wait for the canvas to
show it, write the mask layer. The same wait guards the handoff into the
Inpaint tab's canvas. See ``surface.py`` and ``javascript/minipaint_canvas.js``.
"""

from __future__ import annotations

import html as html_escape
import json
import os.path
import typing

import gradio as gr

from .. import settings
from ..send_log import announce_send_log, log_quietly
from . import document, host, imaging, outpaint, surface

MODES = ("crop", "mask", "expand", "layers")
MODE_LABELS = {"crop": "Crop", "mask": "Mask", "expand": "Expand", "layers": "Layers"}
MODE_BY_LABEL = {label: mode for mode, label in MODE_LABELS.items()}
ASPECTS = ["Free", "Original", "1:1", "4:3", "3:4", "16:9", "9:16", "3:2", "2:3", "Custom"]
TOOLS = ["Paint", "Erase", "Move"]
# The dropdown's choices, and the host key each one stands for. The Canvas
# only offers the ones this WebUI has.
DESTINATION_KEYS = {
    "img2img": "img2img",
    "Inpaint": "inpaint",
    "Extras": "extras",
    "ImageStitch (txt2img)": "stitch_txt2img",
    "ImageStitch (img2img)": "stitch_img2img",
}
DESTINATION_LABELS = {
    "img2img": "img2img",
    "inpaint": "img2img Inpaint",
    "extras": "Extras",
    "stitch_txt2img": "ImageStitch (txt2img)",
    "stitch_img2img": "ImageStitch (img2img)",
}
DESTINATIONS = ["Auto", *DESTINATION_KEYS]
# Destinations written from the backend: an Image and the ImageStitch
# galleries. The host's hidden image textboxes are written from the browser.
BACKEND_TARGETS = ("extras", "stitch_txt2img", "stitch_img2img")
STITCH_TARGETS = ("stitch_txt2img", "stitch_img2img")

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
RAIL_JS = f"() => {{ if ({_JS}) {_JS}.toggleRail(); }}"
FOCUS_ON_JS = f"() => {{ if ({_JS}) {_JS}.setFocus(true); }}"
FOCUS_OFF_JS = f"() => {{ if ({_JS}) {_JS}.setFocus(false); }}"
SWITCH_JS = f"(target) => {{ if ({_JS}) {_JS}.switchTo(target); }}"
SWITCH_CANVAS_JS = f"() => {{ if ({_JS}) {_JS}.switchTo('canvas'); }}"
CROP_JS = (
    f"(bg, fg, state, mode, box) => {{ if ({_JS}) {_JS}.mark(); "
    f"return [bg, fg, state, mode, {_JS} ? {_JS}.cropBox() : '']; }}"
)
# The frame as a selection: the same box, but the view is kept, because the
# picture does not change size.
SELECT_JS = (
    f"(bg, fg, state, mode, box) => {{ if ({_JS}) {_JS}.mark(true); "
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


# Undo and Redo take the last stroke back first, in the browser, and only
# then a structural step on the server; the third input says which happened.
UNDO_JS = (
    f"(state, mode, kind) => {{ const done = {_JS} ? {_JS}.undoStroke() : false; "
    f"if ({_JS}) {_JS}.mark(true); return [state, mode, done ? 'stroke' : 'document']; }}"
)
REDO_JS = (
    f"(state, mode, kind) => {{ const done = {_JS} ? {_JS}.redoStroke() : false; "
    f"if ({_JS}) {_JS}.mark(true); return [state, mode, done ? 'stroke' : 'document']; }}"
)


def _mark_js(input_count: int, keep: bool = False) -> str:
    """A pass-through that notes the moment before the image is replaced, so
    the wait step can tell a fresh load from the last one. ``keep`` asks the
    browser to keep its zoom and position when the picture comes back the
    same size (a layer moved, an undo of one)."""
    flag = "true" if keep else ""
    return f"(...args) => {{ if ({_JS}) {_JS}.mark({flag}); return args.slice(0, {input_count}); }}"


# The host's img2img and Inpaint inputs are its own hidden image textboxes.
# They are written from the browser, as a plain value into exactly the one
# chosen, and the others are left untouched with an empty update. A backend
# output would have to answer every target on every send, and an answer of
# "no change" makes Gradio rebuild a per-session copy of the host's component
# - which under Forge comes back reading images as arrays (see surface.py).
_KEEP = '{"__type__": "update"}'
DELIVER_IMAGE_JS = (
    f"(target, payload) => {{ const t = String(target || ''); "
    f"return [t.indexOf('img2img') === 0 ? payload : {_KEEP}, t.indexOf('inpaint') === 0 ? payload : {_KEEP}]; }}"
)
DELIVER_MASK_JS = (
    f"(target, payload) => [String(target || '').indexOf('inpaint') === 0 ? (payload || '') : {_KEEP}]"
)


def _stitch_enable_js(keys: typing.Sequence[str]) -> str:
    """Tick the "ImageStitch Integrated" box of the tab an image was just
    sent to, from the browser: the host's own accordion follows its box, so
    the references open and count. The other box is left untouched."""
    values = ", ".join(f"t === '{key}' ? true : {_KEEP}" for key in keys)
    return f"(target) => {{ const t = String(target || ''); return [{values}]; }}"


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
    """One line, always: the status never changes height, so the canvas
    above it never has to refit. Notes follow the message, small."""
    parts = [message] if message else []
    parts.extend(f"<small>{note}</small>" for note in notes if note)
    return " ".join(parts)


def _names(names: typing.Sequence[str]) -> str:
    return ", ".join(names)


def send_label(has_mask: bool, has_expansion: bool, mode: str, choice: str = "Auto") -> str:
    """What the Send button says it will do."""
    key = DESTINATION_KEYS.get(choice)
    if key is not None:
        return f"Send to {DESTINATION_LABELS[key]}"
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


def layer_list_html(doc: document.Document) -> str:
    """The layers panel, top layer first, as the browser shows it: an eye
    to show or hide, the name to select, a box to add to or take out of the
    selection, arrows to reorder. Every button carries what it does and
    which layer, and the browser hands that to the server as one action."""
    rows = doc.layer_rows()
    if not rows:
        return '<div class="minipaint-layers minipaint-layers-empty">No image yet.</div>'
    parts = ['<div class="minipaint-layers" role="list">']
    for row in rows:
        name = html_escape.escape(row["name"], quote=True)
        classes = ["minipaint-layer"]
        if row["selected"]:
            classes.append("selected")
        if row["active"]:
            classes.append("active")
        if not row["visible"]:
            classes.append("hidden-layer")
        meta = f"{row['size']} at {row['at']}"
        if row["opacity"] < 100:
            meta += f" · {row['opacity']}%"
        eye_title = f"Hide {name}" if row["visible"] else f"Show {name}"
        check_title = f"Take {name} out of the selection" if row["selected"] else f"Add {name} to the selection"
        parts.append(
            f'<div class="{" ".join(classes)}" role="listitem" data-name="{name}">'
            f'<button type="button" class="minipaint-layer-eye" data-op="eye" data-name="{name}" title="{eye_title}" '
            f'aria-pressed="{"true" if row["visible"] else "false"}">{"👁" if row["visible"] else "⊘"}</button>'
            f'<button type="button" class="minipaint-layer-pick" data-op="pick" data-name="{name}" '
            f'title="Select {name} (shift or ctrl adds it to the selection)">'
            f'<span class="minipaint-layer-name">{name}</span><span class="minipaint-layer-meta">{meta}</span></button>'
            f'<button type="button" class="minipaint-layer-check" data-op="toggle" data-name="{name}" title="{check_title}" '
            f'aria-pressed="{"true" if row["selected"] else "false"}">{"☑" if row["selected"] else "☐"}</button>'
            f'<button type="button" class="minipaint-layer-up" data-op="up" data-name="{name}" title="Move {name} up"'
            f'{" disabled" if row["top"] else ""}>▲</button>'
            f'<button type="button" class="minipaint-layer-down" data-op="down" data-name="{name}" title="Move {name} down"'
            f'{" disabled" if row["bottom"] else ""}>▼</button>'
            "</div>"
        )
    parts.append("</div>")
    return "".join(parts)


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
        self.image_targets: typing.List[str] = []
        self.stitch_targets: typing.List[str] = []
        self.destinations: typing.List[str] = list(DESTINATIONS)
        self.surface: typing.Optional[surface.Surface] = None

    # -- replies -------------------------------------------------------------

    def _layer(self, mask, size):
        """The mask as the scribble layer the canvases show."""
        return imaging.foreground_layer(mask, size, self.mask_color, self.mask_style["contrast"])

    # the mode textbox, the mode chips, one rail panel per mode, the send
    # label, the tool, then the layer list, opacity, name, drag preview and underlay
    MODE_COUNT = 2 + len(MODES) + 2 + 5

    def _mode_updates(self, mode: str, doc: document.Document) -> tuple:
        return (
            mode,
            gr.update(value=MODE_LABELS[mode]),
            *(gr.update(visible=mode == each) for each in MODES),
            gr.update(value=send_label(doc.has_mask, doc.has_expansion, mode, doc.destination)),
            gr.update(value="Paint") if mode == "mask" else gr.skip(),
            *self._layer_updates(doc, mode),
        )

    def _layer_updates(self, doc: document.Document, mode: str) -> list:
        """The layer list when it changed, the opacity and name of the primary
        layer, and - in Layers mode, when they changed - what the browser
        needs to drag the selection: the selected layers as one picture and
        the other layers composited under them."""
        active = doc.active_layer
        listing = layer_list_html(doc)
        if listing != doc.layer_list_sent:
            doc.layer_list_sent = listing
            updates = [listing]
        else:
            updates = [gr.skip()]
        updates += [
            gr.update(value=active.opacity if active else 100),
            gr.update(value=active.name if active else ""),
        ]
        if mode == "layers" and doc.has_image and doc.preview_key() != doc.preview_sent:
            doc.preview_sent = doc.preview_key()
            updates += [doc.preview_payload(), doc.underlay_payload()]
        else:
            updates += [gr.skip(), gr.skip()]
        return updates

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
        if doc.has_image and (mode == "layers" or len(doc.layers) > 1) and doc.active_layer is not None:
            chosen = doc.selected_layers()
            if len(chosen) > 1:
                summary += f" · {len(chosen)} layers selected: {_names(doc.selected_names())}"
            else:
                summary += f" · {doc.active_layer.describe()}"
        if message:
            summary += f" — {message}"

        return (
            doc,
            _status(summary, notes),
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
        reload: bool = True,
    ) -> tuple:
        """Push the document into the canvas: the image now, the mask layer
        in the step that follows the canvas having loaded it. A step that
        changed the layers without changing what the canvas shows (a layer
        made from a selection, a rename, a pick) leaves the canvas alone."""
        if not reload:
            return (gr.skip(), gr.skip(), *self._info(doc, mode, message, notes, sides=sides, reset_aspect=False))
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

    INFO_COUNT = 6 + MODE_COUNT

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

    def pick_mode(self, label, state):
        """The mode chips in the top row."""
        return self.set_mode(MODE_BY_LABEL.get(str(label or ""), "crop"), state)

    def choose_destination(self, choice, state, mode):
        """Options -> Send to: the button says what it will do."""
        doc = document.ensure(state)
        doc.destination = choice if choice in DESTINATIONS else "Auto"
        return doc, gr.update(value=send_label(doc.has_mask, doc.has_expansion, mode, doc.destination))

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
        doc.crop(box, mask)
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
            expanded, new_mask, info = outpaint.expand(doc.base_full(), mask, sides, int(overlap or 0), fill)
        except ValueError as error:
            return self._unchanged(doc, mode, str(error), notes)

        if info["overlap"]:
            notes.append(f"the mask reaches {info['overlap']}px back into the original so the model has room to blend")
        doc.checkpoint("expand")
        doc.expand(sides, expanded, new_mask)
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

    def undo(self, state, mode, kind="document"):
        """Undo: a stroke, if the browser took one back, else the last
        structural step (open, crop, expand, clear, invert, reset, a layer step)."""
        doc = document.ensure(state)
        if kind == "stroke":
            return self._unchanged(doc, mode, "Undid a stroke.")
        label = doc.undo()
        if label is None:
            return self._unchanged(doc, mode, "Nothing to undo.")
        return self._commit(doc, mode, f"Undid {label}.")

    def redo(self, state, mode, kind="document"):
        doc = document.ensure(state)
        if kind == "stroke":
            return self._unchanged(doc, mode, "Redid a stroke.")
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

    def commit_foreground(self, state, flag="wait"):
        """The mask layer, written once the canvas has the image it belongs
        to. Only after a step that replaced the image: a step that left the
        canvas alone leaves the strokes and their history alone too."""
        doc = document.ensure(state)
        if flag == "wait" and doc.has_mask and doc.image is not None:
            return self._layer(doc.mask, doc.image.size)
        return gr.skip()

    # -- callbacks: layers ----------------------------------------------------

    def _ready(self, doc, background, foreground, mode):
        """The canvas's pixels, or the reply that says there is nothing."""
        image, mask, notes = self._sync(doc, background, foreground)
        if image is None:
            return None, self._unchanged(doc, mode, "There is no image yet.")
        return notes, None

    def layer_action(self, payload, background, foreground, state, mode):
        """One tap in the layer list: select a layer (pick), add it to or
        take it out of the selection (toggle), show or hide it (eye), or
        move it one step up or down."""
        doc = document.ensure(state)
        try:
            data = json.loads(payload or "{}")
        except ValueError:
            data = {}
        op = str(data.get("op") or "")
        name = str(data.get("name") or "")
        notes, failed = self._ready(doc, background, foreground, mode)
        if failed:
            return failed
        if doc.index_of(name) is None:
            return self._unchanged(doc, mode, "")

        if op == "pick":
            doc.select(name)
            return self._commit(doc, mode, f"{name} is the active layer.", notes, reload=False)
        if op == "toggle":
            doc.toggle_selected(name)
            names = doc.selected_names()
            message = f"{len(names)} layers selected." if len(names) > 1 else f"{names[0]} is the active layer."
            return self._commit(doc, mode, message, notes, reload=False)
        if op == "eye":
            doc.checkpoint("layer visibility")
            visible = doc.set_visible(name)
            return self._commit(doc, mode, f"{name} {'shown' if visible else 'hidden'}.", notes)
        if op in ("up", "down"):
            step = 1 if op == "up" else -1
            target = doc.index_of(name) + step
            if not (0 <= target < len(doc.layers)):
                return self._unchanged(doc, mode, f"{name} is already at the {'top' if step > 0 else 'bottom'}.")
            doc.checkpoint("layer order")
            doc.reorder(name, step)
            return self._commit(doc, mode, f"{name} moved {'up' if step > 0 else 'down'}.", notes)
        return self._unchanged(doc, mode, "")

    def new_layer_from_selection(self, background, foreground, state, mode, box_raw):
        """The active layer's pixels inside the frame become a layer of their own."""
        doc = document.ensure(state)
        notes, failed = self._ready(doc, background, foreground, mode)
        if failed:
            return failed
        box = imaging.crop_box(box_raw, doc.size)
        if box is None:
            return self._unchanged(doc, mode, "Put the frame over the part to copy first.")
        source = doc.active_layer
        piece = imaging.layer_pixels_in_box(source.image, source.x, source.y, box)
        if piece is None:
            return self._unchanged(doc, mode, f"{source.name} has nothing inside the frame.")
        doc.checkpoint("new layer")
        layer = doc.add_layer(piece[0], piece[1], piece[2])
        return self._commit(
            doc, "layers", f"{layer.name} holds the selection. Drag the image to move it; the rest stays put.", notes, reload=False
        )

    def mask_to_layer(self, background, foreground, state, mode):
        """The active layer's pixels under the mask become a layer of their own."""
        doc = document.ensure(state)
        notes, failed = self._ready(doc, background, foreground, mode)
        if failed:
            return failed
        if not doc.has_mask:
            return self._unchanged(doc, mode, "Paint over the area to copy first (Mask mode).")
        source = doc.active_layer
        piece = imaging.layer_pixels_under_mask(source.image, source.x, source.y, doc.mask)
        if piece is None:
            return self._unchanged(doc, mode, f"{source.name} has no pixels under the mask.")
        doc.checkpoint("new layer")
        layer = doc.add_layer(piece[0], piece[1], piece[2])
        return self._commit(doc, "layers", f"{layer.name} holds the masked area. Drag the image to move it.", notes, reload=False)

    def move_layer(self, payload, background, foreground, state, mode):
        """The browser dragged the selected layers: the offset they settled on."""
        doc = document.ensure(state)
        try:
            data = json.loads(payload or "{}")
            dx, dy = int(round(float(data.get("dx", 0)))), int(round(float(data.get("dy", 0))))
        except (TypeError, ValueError, AttributeError):
            dx = dy = 0
        image, mask, notes = self._sync(doc, background, foreground)
        if image is None or (dx == 0 and dy == 0):
            return self._unchanged(doc, mode, "")
        doc.checkpoint("move layer")
        doc.move_selected(dx, dy)
        return self._commit(doc, mode, f"Moved {_names(doc.selected_names())}.", notes)

    def layer_center(self, background, foreground, state, mode):
        """The selected layers back to the middle of the canvas."""
        doc = document.ensure(state)
        notes, failed = self._ready(doc, background, foreground, mode)
        if failed:
            return failed
        doc.checkpoint("center layer")
        doc.center_selected()
        return self._commit(doc, mode, f"Centered {_names(doc.selected_names())}.", notes)

    def layer_opacity(self, value, background, foreground, state, mode):
        doc = document.ensure(state)
        notes, failed = self._ready(doc, background, foreground, mode)
        if failed:
            return failed
        doc.checkpoint("layer opacity")
        doc.set_opacity(value)
        return self._commit(doc, mode, f"{_names(doc.selected_names())} at {doc.active_layer.opacity}% opacity.", notes)

    def layer_rename(self, name, background, foreground, state, mode):
        doc = document.ensure(state)
        notes, failed = self._ready(doc, background, foreground, mode)
        if failed:
            return failed
        if not str(name or "").strip():
            return self._unchanged(doc, mode, "Type a name first.")
        doc.checkpoint("rename layer")
        new_name = doc.rename_active(str(name))
        return self._commit(doc, mode, f"Renamed to {new_name}.", notes, reload=False)

    def layer_merge(self, background, foreground, state, mode):
        """Two or more selected layers into one; one selected layer into the
        layer below it."""
        doc = document.ensure(state)
        notes, failed = self._ready(doc, background, foreground, mode)
        if failed:
            return failed
        chosen = doc.selected_names()
        if len(chosen) < 2 and doc.active < 1:
            return self._unchanged(doc, mode, f"There is nothing below {doc.active_layer.name} to merge into.")
        if len(chosen) < 2:
            upper = doc.active_layer.name
            doc.checkpoint("merge down")
            lower = doc.merge_selected()
            return self._commit(doc, mode, f"{upper} merged into {lower}.", notes)
        doc.checkpoint("merge layers")
        result = doc.merge_selected()
        return self._commit(doc, mode, f"{_names(chosen)} merged into {result}.", notes)

    def layer_delete(self, background, foreground, state, mode):
        doc = document.ensure(state)
        notes, failed = self._ready(doc, background, foreground, mode)
        if failed:
            return failed
        if len(doc.layers) < 2:
            return self._unchanged(doc, mode, "The last layer stays; Undo or Reset changes the picture.")
        doc.checkpoint("delete layer")
        removed = doc.delete_selected()
        return self._commit(doc, mode, f"{_names(removed)} deleted.", notes)

    def layer_duplicate(self, background, foreground, state, mode):
        doc = document.ensure(state)
        notes, failed = self._ready(doc, background, foreground, mode)
        if failed:
            return failed
        doc.checkpoint("duplicate layer")
        copies = doc.duplicate_selected()
        names = [copy.name for copy in copies]
        message = f"{names[0]} is a copy of the layer below it." if len(names) == 1 else f"{_names(names)} are copies, each above its original."
        return self._commit(doc, mode, message, notes)

    def layer_flatten(self, background, foreground, state, mode):
        doc = document.ensure(state)
        notes, failed = self._ready(doc, background, foreground, mode)
        if failed:
            return failed
        if len(doc.layers) < 2:
            return self._unchanged(doc, mode, "There is only one layer.")
        doc.checkpoint("flatten")
        count = doc.flatten()
        return self._commit(doc, mode, f"{count} layers flattened into one.", notes)

    # -- callbacks: send ----------------------------------------------------

    def send(self, background, foreground, state, mode, destination_choice, smoothing):
        """The handoff. The image goes to the destination's own input: for
        Extras straight into its image component, for ImageStitch into its
        reference gallery (replacing what was there), for img2img and
        Inpaint as a PNG data URL that the next (browser-side) step writes
        into the host's hidden image textbox. For Inpaint the mask follows
        once that canvas has taken the image (``send_mask``). The last two
        values are the browser's instructions: the target, with the size
        the Inpaint canvas must reach, and the image payload.
        """
        doc = document.ensure(state)
        doc.destination = destination_choice if destination_choice in DESTINATIONS else "Auto"
        skips = [gr.skip() for _ in self.image_targets]
        image, mask, notes = self._sync(doc, background, foreground)
        if image is None:
            return (*skips, *self._info(doc, mode, "There is no image to send."), "", "")

        target = resolve_destination(doc.destination, doc.has_mask, doc.has_expansion)
        label = DESTINATION_LABELS[target]
        if target not in self.targets:
            return (*skips, *self._info(doc, mode, f"{label} was not found in this WebUI, so nothing was sent."), "", "")

        outgoing = imaging.to_rgba(doc.image)
        if len(doc.layers) > 1:
            notes.append(f"{len(doc.layers)} layers were flattened for {label}")
        if imaging.has_alpha_content(outgoing):
            fill_name = str(settings.get(settings.SEND_FILL, "Neutral gray"))
            color = imaging.fill_color(fill_name, outgoing)
            outgoing = imaging.flatten(outgoing, color)
            notes.append(f"transparent pixels were filled with rgb{color} for {label}")

        payload = ""
        if target == "inpaint":
            if doc.has_mask and smoothing != "Off":
                notes.append(f"mask edge smoothing: {smoothing}")
            doc.pending_send = {"target": target, "smoothing": smoothing, "size": outgoing.size}
            instruction = f"inpaint:{outgoing.width}x{outgoing.height}"
            payload = imaging.to_data_url(outgoing)
        else:
            doc.pending_send = None
            instruction = target
            if target == "img2img":
                payload = imaging.to_data_url(outgoing)
            if target in STITCH_TARGETS:
                notes.append("it is now the only reference image there")
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
        # A gallery takes a list; an Image takes the picture. Either is
        # served from a file the host is told about (see host.staged).
        outputs = []
        for key in self.image_targets:
            if key != target:
                outputs.append(gr.skip())
            elif key in STITCH_TARGETS:
                outputs.append([host.staged(outgoing)])
            else:
                outputs.append(host.staged(outgoing))
        return (*outputs, *self._info(doc, mode, f"Sent to {label}.", notes), instruction, payload)

    def send_mask(self, state, instruction):
        """The mask layer for the Inpaint canvas as a data URL, or an empty
        string to clear it when the send carried no mask. Empty for any other
        destination, where the browser leaves the Inpaint canvas alone."""
        doc = document.ensure(state)
        pending = getattr(doc, "pending_send", None)
        if not str(instruction or "").startswith("inpaint") or not pending:
            return ""
        doc.pending_send = None
        if not doc.has_mask:
            return ""
        mask = imaging.smooth_mask(doc.mask, pending.get("smoothing", "Off"))
        return imaging.to_data_url(self._layer(mask, pending["size"]))

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
        # Extras (an Image) and the ImageStitch galleries are written from
        # the backend; the host's hidden image textboxes from the browser.
        self.image_targets = [key for key in BACKEND_TARGETS if key in self.targets]
        self.stitch_targets = [key for key in STITCH_TARGETS if key in self.targets]
        self.destinations = ["Auto"] + [choice for choice, key in DESTINATION_KEYS.items() if key in self.targets]

        with gr.Row(elem_id=_id("root"), elem_classes=["minipaint-canvas-root"], equal_height=False):
            with gr.Column(elem_id=_id("work"), elem_classes=["minipaint-work"], scale=1, min_width=320):
                state = gr.State(None)
                # A textbox rather than a State: the browser follows its changes.
                mode_state = gr.Textbox("crop", visible=False, elem_id=_id("mode"))

                with gr.Row(elem_id=_id("topbar"), elem_classes=["minipaint-topbar"]):
                    open_btn = gr.UploadButton(
                        "Open", file_types=["image"], type="filepath", elem_id=_id("open"), elem_classes=["minipaint-action"]
                    )
                    undo_btn = gr.Button("Undo", elem_id=_id("undo"), elem_classes=["minipaint-action"])
                    redo_btn = gr.Button("Redo", elem_id=_id("redo"), elem_classes=["minipaint-action"])
                    # In a column of its own, so the chips take their own width
                    # rather than a share of the row's.
                    with gr.Column(elem_id=_id("modes"), elem_classes=["minipaint-modes"], scale=0, min_width=0):
                        mode_pick = gr.Radio(
                            [MODE_LABELS[mode] for mode in MODES],
                            value=MODE_LABELS["crop"],
                            label="Mode",
                            show_label=False,
                            elem_id=_id("mode_pick"),
                            elem_classes=["minipaint-chips", "minipaint-mode-pick"],
                        )
                    panels_btn = gr.Button("Panels", elem_id=_id("panels"), elem_classes=["minipaint-action", "minipaint-panels"])
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
                    fit_window=settings.canvas_fits_window(),
                    brush_width=self.brush_width,
                    attach_js=ATTACH_JS,
                )

                status = gr.Markdown(
                    "**No image yet.** Open a file, drop or paste one onto the canvas, or press 🖌️ under a txt2img, img2img or Extras result.",
                    elem_id=_id("status"),
                    elem_classes=["minipaint-status"],
                )

                # Hidden wires between chained events. Values, not DOM.
                crop_box = gr.Textbox("", visible=False, elem_id=_id("crop_box"))
                original_size = gr.Textbox("", visible=False, elem_id=_id("original_size"))
                wait_flag = gr.Textbox("", visible=False, elem_id=_id("wait"))
                switch_box = gr.Textbox("", visible=False, elem_id=_id("switch"))
                event_kind = gr.Textbox("", visible=False, elem_id=_id("event"))
                payload_box = gr.Textbox("", visible=False, elem_id=_id("payload"))
                mask_payload_box = gr.Textbox("", visible=False, elem_id=_id("mask_payload"))
                # The browser drops a dragged layer or a tap in the layer list
                # here; the server answers with what the next drag needs.
                layer_move = gr.Textbox("", visible=False, elem_id=_id("layer_move"))
                layer_action = gr.Textbox("", visible=False, elem_id=_id("layer_action"))
                layer_preview = gr.Textbox("", visible=False, elem_id=_id("layer_preview"))
                layer_underlay = gr.Textbox("", visible=False, elem_id=_id("layer_underlay"))

            # The rail: one panel per mode, the Options at the bottom. Its
            # width is the stylesheet's (by the room the tab has), so no
            # minimum here.
            with gr.Column(elem_id=_id("rail"), elem_classes=["minipaint-rail"], scale=0, min_width=0):
                panel_crop, crop = self._crop_panel()
                panel_mask, mask = self._mask_panel()
                panel_expand, expand = self._expand_panel()
                panel_layers, layers = self._layers_panel()
                destination, reset_btn, save_btn, save_file = self._options_panel()

        layers["move"] = layer_move
        layers["action"] = layer_action
        layers["preview"] = layer_preview
        layers["underlay"] = layer_underlay

        self._wire(
            state=state,
            mode_state=mode_state,
            status=status,
            open_btn=open_btn,
            undo_btn=undo_btn,
            redo_btn=redo_btn,
            mode_pick=mode_pick,
            panels_btn=panels_btn,
            focus_btn=focus_btn,
            focus_exit_btn=focus_exit_btn,
            send_btn=send_btn,
            panels={"crop": panel_crop, "mask": panel_mask, "expand": panel_expand, "layers": panel_layers},
            crop=crop,
            mask=mask,
            expand=expand,
            layers=layers,
            destination=destination,
            reset_btn=reset_btn,
            save_btn=save_btn,
            save_file=save_file,
            crop_box=crop_box,
            original_size=original_size,
            wait_flag=wait_flag,
            switch_box=switch_box,
            event_kind=event_kind,
            payload_box=payload_box,
            mask_payload_box=mask_payload_box,
        )

    # -- the rail's panels ---------------------------------------------------

    def _crop_panel(self):
        with gr.Column(elem_id=_id("panel_crop"), elem_classes=["minipaint-panel"]) as panel:
            gr.Markdown("**Crop**", elem_classes=["minipaint-panel-title"])
            aspect = gr.Dropdown(ASPECTS, value="Free", label="Aspect", elem_id=_id("crop_aspect"), elem_classes=["minipaint-aspect"])
            with gr.Row(elem_classes=["minipaint-pair"]):
                width = gr.Number(label="Custom ratio width", value=1024, precision=0, minimum=1, elem_id=_id("crop_custom_w"))
                height = gr.Number(label="Custom ratio height", value=1024, precision=0, minimum=1, elem_id=_id("crop_custom_h"))
            apply = gr.Button("Apply Crop", variant="primary", elem_id=_id("crop_apply"), elem_classes=["minipaint-apply"])
            gr.Markdown(
                "The frame starts over the whole image. Drag the image under it with one finger, pinch or scroll to zoom, "
                "drag a corner to resize it; the aspect locks its shape. **Apply Crop** keeps what is inside.",
                elem_classes=["minipaint-hint"],
            )
        return panel, {"aspect": aspect, "width": width, "height": height, "apply": apply}

    def _mask_panel(self):
        with gr.Column(elem_id=_id("panel_mask"), elem_classes=["minipaint-panel"], visible=False) as panel:
            gr.Markdown("**Mask**", elem_classes=["minipaint-panel-title"])
            tool = gr.Radio(TOOLS, value="Paint", label="Tool", show_label=False, elem_id=_id("mask_tool"), elem_classes=["minipaint-chips"])
            size = gr.Slider(1, 100, value=self.brush_width, step=1, label="Brush size", elem_id=_id("mask_size"), elem_classes=["minipaint-size"])
            with gr.Row(elem_classes=["minipaint-pair"]):
                clear = gr.Button("Clear Mask", elem_id=_id("mask_clear"), elem_classes=["minipaint-quick-action"])
                invert = gr.Button("Invert Mask", elem_id=_id("mask_invert"), elem_classes=["minipaint-quick-action"])
            smoothing = gr.Radio(
                imaging.SMOOTHING_LEVELS,
                value="Off",
                label="Edge smoothing (applied when sending)",
                elem_id=_id("mask_smoothing"),
                elem_classes=["minipaint-chips"],
            )
            gr.Markdown(
                "Paint over what should change. Two fingers pan and zoom; Move lets one finger pan. "
                "Undo takes strokes back first, then the bigger steps. The painted area can become a layer: Layers → Masked area → new layer.",
                elem_classes=["minipaint-hint"],
            )
        return panel, {"tool": tool, "size": size, "clear": clear, "invert": invert, "smoothing": smoothing}

    def _expand_panel(self):
        with gr.Column(elem_id=_id("panel_expand"), elem_classes=["minipaint-panel"], visible=False) as panel:
            gr.Markdown("**Expand**", elem_classes=["minipaint-panel-title"])
            amount = gr.Radio(outpaint.AMOUNTS, value="128", label="Add", show_label=False, elem_id=_id("expand_amount"), elem_classes=["minipaint-chips"])
            with gr.Row(elem_classes=["minipaint-sides"]):
                side_buttons = {
                    side: gr.Button(side, elem_id=_id(f"expand_{side.lower()}"), elem_classes=["minipaint-side"]) for side in outpaint.SIDES
                }
            clear_sides = gr.Button("Clear sides", elem_id=_id("expand_clear"), elem_classes=["minipaint-quick-action"])
            preview = gr.Markdown("No image yet.", elem_id=_id("expand_preview"), elem_classes=["minipaint-preview"])
            apply = gr.Button("Apply Expand", variant="primary", elem_id=_id("expand_apply"), elem_classes=["minipaint-apply"])
            with gr.Accordion("Exact amounts and fill", open=False, elem_id=_id("expand_advanced")):
                with gr.Row(elem_classes=["minipaint-sides"]):
                    numbers = {
                        side: gr.Number(label=side, value=0, precision=0, minimum=0, elem_id=_id(f"expand_num_{side.lower()}"))
                        for side in outpaint.SIDES
                    }
                overlap = gr.Slider(0, 256, value=32, step=8, label="Overlap into the original (px)", elem_id=_id("expand_overlap"))
                # Chips, not menus: a menu that opens inside the scrolling
                # rail is cut off at its edge.
                fill = gr.Radio(outpaint.FILL_POLICIES, value=outpaint.DEFAULT_FILL, label="New area", elem_id=_id("expand_fill"), elem_classes=["minipaint-chips"])
                snap = gr.Radio(settings.SNAP_CHOICES, value=self.snap_default, label="Snap to", elem_id=_id("expand_snap"), elem_classes=["minipaint-chips"])
            gr.Markdown(
                "Tap an amount and a side, or type exact numbers. **Apply Expand** adds the room and masks the new area, "
                "plus the overlap back into the original so the model can blend.",
                elem_classes=["minipaint-hint"],
            )
        return panel, {
            "amount": amount,
            "sides": side_buttons,
            "clear": clear_sides,
            "preview": preview,
            "apply": apply,
            "numbers": numbers,
            "overlap": overlap,
            "fill": fill,
            "snap": snap,
        }

    def _layers_panel(self):
        with gr.Column(elem_id=_id("panel_layers"), elem_classes=["minipaint-panel"], visible=False) as panel:
            gr.Markdown("**Layers**", elem_classes=["minipaint-panel-title"])
            new = gr.Button("New from selection", variant="primary", elem_id=_id("layer_new"), elem_classes=["minipaint-apply"])
            to_layer = gr.Button("Masked area → new layer", elem_id=_id("mask_to_layer"), elem_classes=["minipaint-quick-action"])
            listing = gr.HTML(layer_list_html(document.Document()), elem_id=_id("layer_list"), elem_classes=["minipaint-layer-list"])
            with gr.Row(elem_classes=["minipaint-pair"]):
                merge = gr.Button("Merge", elem_id=_id("layer_merge"), elem_classes=["minipaint-quick-action"])
                delete = gr.Button("Delete", elem_id=_id("layer_delete"), elem_classes=["minipaint-quick-action"])
            with gr.Row(elem_classes=["minipaint-pair"]):
                duplicate = gr.Button("Duplicate", elem_id=_id("layer_duplicate"), elem_classes=["minipaint-quick-action"])
                center = gr.Button("Center", elem_id=_id("layer_center"), elem_classes=["minipaint-quick-action"])
            flatten = gr.Button("Flatten all", elem_id=_id("layer_flatten"), elem_classes=["minipaint-quick-action"])
            opacity = gr.Slider(0, 100, value=100, step=1, label="Opacity of the selected layers", elem_id=_id("layer_opacity"))
            with gr.Row(elem_classes=["minipaint-pair"]):
                name = gr.Textbox("", label="Name of the active layer", elem_id=_id("layer_name"), scale=3, min_width=0)
                rename = gr.Button("Rename", elem_id=_id("layer_rename"), scale=1, min_width=0)
            gr.Markdown(
                "Tap a layer to select it; the box selects several; the eye shows or hides; the arrows reorder. "
                "The frame is the selection: **New from selection** copies what the active layer has inside it into a "
                "layer of its own. One finger (or the left mouse button) drags the selected layers; two fingers, "
                "the wheel and the right button pan and zoom. **Center** brings a layer back into view. Sending flattens the visible layers.",
                elem_classes=["minipaint-hint"],
            )
        return panel, {
            "new": new,
            "to_layer": to_layer,
            "list": listing,
            "merge": merge,
            "delete": delete,
            "duplicate": duplicate,
            "center": center,
            "flatten": flatten,
            "opacity": opacity,
            "name": name,
            "rename": rename,
        }

    def _options_panel(self):
        with gr.Accordion("Options", open=False, elem_id=_id("options"), elem_classes=["minipaint-options"]):
            # Chips rather than a menu: a menu opening at the bottom of the
            # scrolling rail would be cut off at its edge.
            destination = gr.Radio(self.destinations, value="Auto", label="Send to", elem_id=_id("destination"), elem_classes=["minipaint-chips"])
            reset_btn = gr.Button("Reset to original", elem_id=_id("reset"), elem_classes=["minipaint-quick-action"])
            save_btn = gr.Button("Save a copy", elem_id=_id("save"), elem_classes=["minipaint-quick-action"])
            save_file = gr.File(label="Saved copy", interactive=False, visible=False, elem_id=_id("save_file"))
            gr.Markdown(
                "Auto sends a plain image to img2img and an image with a mask to Inpaint. ImageStitch receives the "
                "image as its only reference. Undo and Redo cover strokes, Open, Apply Crop, Apply Expand, Clear, Invert and every layer step.",
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
        layers = parts["layers"]
        panels = parts["panels"]
        background = self.surface.background
        foreground = self.surface.foreground
        wait_flag = parts["wait_flag"]
        switch_box = parts["switch_box"]
        event_kind = parts["event_kind"]

        side_inputs = [expand["numbers"][side] for side in outpaint.SIDES]
        preview_inputs = [state] + side_inputs + [expand["snap"]]

        mode_outputs = [
            mode_state,
            parts["mode_pick"],
            *(panels[mode] for mode in MODES),
            send_btn,
            mask["tool"],
            layers["list"],
            layers["opacity"],
            layers["name"],
            layers["preview"],
            layers["underlay"],
        ]
        assert len(mode_outputs) == self.MODE_COUNT
        info_outputs = [state, status, expand["preview"], crop["aspect"], parts["original_size"], wait_flag, *mode_outputs]
        commit_outputs = [background, foreground, *info_outputs]
        canvas_inputs = [background, foreground, state, mode_state]

        quiet = {"show_progress": "hidden"}

        def structural(event, fn, inputs, js=None, keep=False):
            """image -> wait for the canvas -> mask layer. See the module docstring."""
            return (
                event(fn, inputs=inputs, outputs=commit_outputs, js=js or _mark_js(len(inputs), keep), **quiet)
                .then(_noop, js=WAIT_JS, inputs=[wait_flag], outputs=None, **quiet)
                .then(self.commit_foreground, inputs=[state, wait_flag], outputs=[foreground], **quiet)
            )

        # -- modes: the chips in the top row switch the rail's panel from
        # Python; the browser follows the mode textbox, whichever step changed it
        parts["mode_pick"].input(self.pick_mode, inputs=[parts["mode_pick"], state], outputs=mode_outputs, **quiet)
        mode_state.change(None, js=MODE_JS, inputs=[mode_state])
        parts["panels_btn"].click(None, js=RAIL_JS)

        # -- what the canvas holds, when a file is opened, dropped or pasted onto it
        background.input(
            self.on_canvas_image,
            inputs=[background, state, mode_state, event_kind],
            outputs=[foreground, *info_outputs],
            js=CANVAS_INPUT_JS,
            **quiet,
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
        mask["clear"].click(self.clear_mask, inputs=canvas_inputs, outputs=[foreground, *info_outputs], **quiet)
        mask["invert"].click(self.invert_mask, inputs=canvas_inputs, outputs=[foreground, *info_outputs], **quiet)

        # -- expand
        for side, button in expand["sides"].items():
            button.click(
                lambda amount, state, left, right, top, bottom, snap, side=side: self.add_side(
                    side, amount, state, left, right, top, bottom, snap
                ),
                inputs=[expand["amount"], state] + side_inputs + [expand["snap"]],
                outputs=side_inputs + [expand["preview"]],
                **quiet,
            )
        expand["clear"].click(self.clear_sides, inputs=[state], outputs=side_inputs + [expand["preview"]], **quiet)
        for control in side_inputs + [expand["snap"]]:
            control.change(self.expand_preview, inputs=preview_inputs, outputs=[expand["preview"]], **quiet)
        structural(
            expand["apply"].click,
            self.apply_expand,
            canvas_inputs + side_inputs + [expand["overlap"], expand["fill"], expand["snap"]],
        )

        # -- layers: selection to layer, the list, the drag's landing, and the panel
        structural(layers["new"].click, self.new_layer_from_selection, canvas_inputs + [parts["crop_box"]], js=SELECT_JS)
        structural(layers["to_layer"].click, self.mask_to_layer, canvas_inputs, keep=True)
        structural(layers["action"].input, self.layer_action, [layers["action"]] + canvas_inputs, keep=True)
        structural(layers["move"].input, self.move_layer, [layers["move"]] + canvas_inputs, keep=True)
        structural(layers["opacity"].release, self.layer_opacity, [layers["opacity"]] + canvas_inputs, keep=True)
        structural(layers["rename"].click, self.layer_rename, [layers["name"]] + canvas_inputs, keep=True)
        structural(layers["merge"].click, self.layer_merge, canvas_inputs, keep=True)
        structural(layers["delete"].click, self.layer_delete, canvas_inputs, keep=True)
        structural(layers["duplicate"].click, self.layer_duplicate, canvas_inputs, keep=True)
        structural(layers["center"].click, self.layer_center, canvas_inputs, keep=True)
        structural(layers["flatten"].click, self.layer_flatten, canvas_inputs, keep=True)

        # -- open, history, reset
        structural(parts["open_btn"].upload, self.open_file, [parts["open_btn"], state, mode_state])
        structural(parts["undo_btn"].click, self.undo, [state, mode_state, event_kind], js=UNDO_JS)
        structural(parts["redo_btn"].click, self.redo, [state, mode_state, event_kind], js=REDO_JS)
        structural(parts["reset_btn"].click, self.reset, [state, mode_state])

        # -- view: focus is browser-only
        parts["focus_btn"].click(None, js=FOCUS_ON_JS)
        parts["focus_exit_btn"].click(None, js=FOCUS_OFF_JS)

        # -- save
        parts["save_btn"].click(self.save_copy, inputs=[background, foreground, state], outputs=[parts["save_file"]], **quiet)

        # -- send: the image into the host's own inputs (Extras and the
        # ImageStitch galleries from the backend, img2img and Inpaint from
        # the browser), the ImageStitch box ticked from the browser, then the
        # host's own tab switch; for Inpaint, the mask layer once that
        # canvas has the image.
        destination = parts["destination"]
        destination.input(self.choose_destination, inputs=[destination, state, mode_state], outputs=[state, send_btn], **quiet)
        payload_box = parts["payload_box"]
        target_components = [self.targets[key] for key in self.image_targets]
        sent = send_btn.click(
            self.send,
            inputs=[background, foreground, state, mode_state, destination, mask["smoothing"]],
            outputs=target_components + info_outputs + [switch_box, payload_box],
            **quiet,
        )
        textbox_targets = [self.targets[key] for key in ("img2img", "inpaint") if key in self.targets]
        if len(textbox_targets) == 2:
            sent.then(None, js=DELIVER_IMAGE_JS, inputs=[switch_box, payload_box], outputs=textbox_targets)
        elif textbox_targets:
            only = "img2img" if "img2img" in self.targets else "inpaint"
            sent.then(
                None,
                js=f"(target, payload) => [String(target || '').indexOf('{only}') === 0 ? payload : {_KEEP}]",
                inputs=[switch_box, payload_box],
                outputs=textbox_targets,
            )
        if self.stitch_targets:
            enables = [self.targets[f"{key}_enable"] for key in self.stitch_targets]
            sent.then(None, js=_stitch_enable_js(self.stitch_targets), inputs=[switch_box], outputs=enables)
        sent.then(None, js=SWITCH_JS, inputs=[switch_box], outputs=None)
        if "inpaint_mask" in self.targets:
            inpaint_uuid = getattr(self.targets["inpaint"], "elem_id", "") or ""
            mask_payload_box = parts["mask_payload_box"]
            sent.then(_noop, js=_host_wait_js(inpaint_uuid), inputs=[switch_box], outputs=None, **quiet).then(
                self.send_mask, inputs=[state, switch_box], outputs=[mask_payload_box], **quiet
            ).then(None, js=DELIVER_MASK_JS, inputs=[switch_box, mask_payload_box], outputs=[self.targets["inpaint_mask"]])

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
