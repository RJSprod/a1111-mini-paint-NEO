"""The touch-first Canvas: the WebUI's own canvas, with ordinary Gradio
controls around it.

Layout, top to bottom: an action bar, the canvas, a status line, the four
modes (Crop / Mask / Expand / Layers), one compact row of controls for the
chosen mode, an "Options" accordion with the rest of that mode's settings,
and a "More" accordion. Nothing floats over the canvas: the canvas takes the height
the window has left, so the whole tab is in view without scrolling. Buttons
are Buttons and sliders are Sliders, so the host theme decides how everything
looks; the canvas is the same one img2img uses.

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

MODES = ("crop", "mask", "expand", "layers")
MODE_LABELS = {"crop": "Crop", "mask": "Mask", "expand": "Expand", "layers": "Layers"}
OPTIONS_LABELS = {"crop": "Crop options", "mask": "Mask options", "expand": "Expand options", "layers": "Layer options"}
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

    MODE_COUNT = 1 + 3 * len(MODES) + 3 + 6

    def _mode_updates(self, mode: str, doc: document.Document) -> tuple:
        """The mode textbox, the quick row and options panel of each mode,
        the mode buttons, the send label, the tool, the options label, and
        the layer widgets."""
        return (
            mode,
            *(gr.update(visible=mode == each) for each in MODES),
            *(gr.update(visible=mode == each) for each in MODES),
            *(gr.update(variant="primary" if mode == each else "secondary") for each in MODES),
            gr.update(value=send_label(doc.has_mask, doc.has_expansion, mode)),
            gr.update(value="Paint") if mode == "mask" else gr.skip(),
            gr.update(label=OPTIONS_LABELS[mode]),
            *self._layer_updates(doc, mode),
        )

    def _layer_updates(self, doc: document.Document, mode: str) -> list:
        """The layer menu, the visible-layers chips, the opacity and name of
        the active layer, and - in Layers mode, when they changed - what the
        browser needs to drag the active layer: the layer itself and the
        other layers composited under it."""
        names = doc.layer_names()
        active = doc.active_layer
        updates = [
            gr.update(choices=names, value=active.name if active else None),
            gr.update(choices=names, value=doc.visible_names()),
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
        made from a selection, a rename) leaves the canvas alone."""
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
        structural step (open, crop, expand, clear, invert, reset)."""
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

    def commit_foreground(self, state):
        """The mask layer, written once the canvas has the image it belongs to."""
        doc = document.ensure(state)
        if doc.has_mask and doc.image is not None:
            return self._layer(doc.mask, doc.image.size)
        return gr.skip()

    # -- callbacks: layers ----------------------------------------------------

    def pick_layer(self, name, state, mode):
        doc = document.ensure(state)
        names = doc.layer_names()
        if name in names:
            doc.active = names.index(name)
        return self._info(doc, mode, f"{name} is the active layer." if name in names else "")

    def new_layer_from_selection(self, background, foreground, state, mode, box_raw):
        """The active layer's pixels inside the frame become a layer of their own."""
        doc = document.ensure(state)
        image, mask, notes = self._sync(doc, background, foreground)
        if image is None:
            return self._unchanged(doc, mode, "There is no image yet.")
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
        image, mask, notes = self._sync(doc, background, foreground)
        if image is None:
            return self._unchanged(doc, mode, "There is no image yet.")
        if not doc.has_mask:
            return self._unchanged(doc, mode, "Paint over the area to copy first.")
        source = doc.active_layer
        piece = imaging.layer_pixels_under_mask(source.image, source.x, source.y, doc.mask)
        if piece is None:
            return self._unchanged(doc, mode, f"{source.name} has no pixels under the mask.")
        doc.checkpoint("new layer")
        layer = doc.add_layer(piece[0], piece[1], piece[2])
        return self._commit(doc, "layers", f"{layer.name} holds the masked area. Drag the image to move it.", notes, reload=False)

    def move_layer(self, payload, background, foreground, state, mode):
        """The browser dragged the active layer: the offset it settled on."""
        import json as _json

        doc = document.ensure(state)
        try:
            data = _json.loads(payload or "{}")
            dx, dy = int(round(float(data.get("dx", 0)))), int(round(float(data.get("dy", 0))))
        except (TypeError, ValueError, AttributeError):
            dx = dy = 0
        image, mask, notes = self._sync(doc, background, foreground)
        if image is None or (dx == 0 and dy == 0):
            return self._unchanged(doc, mode, "")
        doc.checkpoint("move layer")
        doc.move_active(dx, dy)
        return self._commit(doc, mode, f"Moved {doc.active_layer.name}.", notes)

    def layer_visibility(self, visible_names, background, foreground, state, mode):
        doc = document.ensure(state)
        image, mask, notes = self._sync(doc, background, foreground)
        if image is None:
            return self._unchanged(doc, mode, "There is no image yet.")
        doc.checkpoint("layer visibility")
        doc.set_visibility(visible_names or [])
        hidden = [layer.name for layer in doc.layers if not layer.visible]
        return self._commit(doc, mode, ("Hidden: " + ", ".join(hidden) + ".") if hidden else "Every layer is visible.", notes)

    def layer_opacity(self, value, background, foreground, state, mode):
        doc = document.ensure(state)
        image, mask, notes = self._sync(doc, background, foreground)
        if image is None:
            return self._unchanged(doc, mode, "There is no image yet.")
        doc.checkpoint("layer opacity")
        doc.set_opacity(value)
        layer = doc.active_layer
        return self._commit(doc, mode, f"{layer.name} at {layer.opacity}% opacity.", notes)

    def layer_rename(self, name, background, foreground, state, mode):
        doc = document.ensure(state)
        image, mask, notes = self._sync(doc, background, foreground)
        if image is None:
            return self._unchanged(doc, mode, "There is no image yet.")
        if not str(name or "").strip():
            return self._unchanged(doc, mode, "Type a name first.")
        doc.checkpoint("rename layer")
        new_name = doc.rename_active(str(name))
        return self._commit(doc, mode, f"Renamed to {new_name}.", notes, reload=False)

    def layer_order(self, step, background, foreground, state, mode):
        doc = document.ensure(state)
        image, mask, notes = self._sync(doc, background, foreground)
        if image is None:
            return self._unchanged(doc, mode, "There is no image yet.")
        target = doc.active + step
        if not (0 <= target < len(doc.layers)):
            return self._unchanged(doc, mode, f"{doc.active_layer.name} is already at the {'top' if step > 0 else 'bottom'}.")
        doc.checkpoint("layer order")
        doc.reorder_active(step)
        return self._commit(doc, mode, f"{doc.active_layer.name} moved {'up' if step > 0 else 'down'}.", notes)

    def layer_merge(self, background, foreground, state, mode):
        doc = document.ensure(state)
        image, mask, notes = self._sync(doc, background, foreground)
        if image is None:
            return self._unchanged(doc, mode, "There is no image yet.")
        if doc.active < 1:
            return self._unchanged(doc, mode, f"There is nothing below {doc.active_layer.name} to merge into.")
        upper = doc.active_layer.name
        doc.checkpoint("merge down")
        lower = doc.merge_down()
        return self._commit(doc, mode, f"{upper} merged into {lower}.", notes)

    def layer_delete(self, background, foreground, state, mode):
        doc = document.ensure(state)
        image, mask, notes = self._sync(doc, background, foreground)
        if image is None:
            return self._unchanged(doc, mode, "There is no image yet.")
        if len(doc.layers) < 2:
            return self._unchanged(doc, mode, "The last layer stays; Undo or Reset changes the picture.")
        doc.checkpoint("delete layer")
        removed = doc.delete_active()
        return self._commit(doc, mode, f"{removed} deleted.", notes)

    def layer_duplicate(self, background, foreground, state, mode):
        doc = document.ensure(state)
        image, mask, notes = self._sync(doc, background, foreground)
        if image is None:
            return self._unchanged(doc, mode, "There is no image yet.")
        doc.checkpoint("duplicate layer")
        copy = doc.duplicate_active()
        return self._commit(doc, mode, f"{copy.name} is a copy of the layer below it.", notes)

    def layer_flatten(self, background, foreground, state, mode):
        doc = document.ensure(state)
        image, mask, notes = self._sync(doc, background, foreground)
        if image is None:
            return self._unchanged(doc, mode, "There is no image yet.")
        if len(doc.layers) < 2:
            return self._unchanged(doc, mode, "There is only one layer.")
        doc.checkpoint("flatten")
        count = doc.flatten()
        return self._commit(doc, mode, f"{count} layers flattened into one.", notes)

    # -- callbacks: send ----------------------------------------------------

    def send(self, background, foreground, state, mode, destination_choice, smoothing):
        """The handoff. The image goes to the destination's own input: for
        Extras straight into its image component, for img2img and Inpaint as
        a PNG data URL that the next (browser-side) step writes into the
        host's hidden image textbox. For Inpaint the mask follows once that
        canvas has taken the image (``send_mask``). The last two values are
        the browser's instructions: the target, with the size the Inpaint
        canvas must reach, and the image payload.
        """
        doc = document.ensure(state)
        skips = [gr.skip() for _ in self.image_targets]
        image, mask, notes = self._sync(doc, background, foreground)
        if image is None:
            return (*skips, *self._info(doc, mode, "There is no image to send."), "", "")

        target = resolve_destination(destination_choice, doc.has_mask, doc.has_expansion)
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
        # Only Extras (an ordinary gr.Image) is written from the backend; the
        # host's hidden image textboxes are written from the browser.
        self.image_targets = [key for key in ("extras",) if key in self.targets]
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
                fit_window=settings.canvas_fits_window(),
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

            # One compact row per mode: what a finger needs while working.
            quick_crop, crop = self._crop_quick()
            quick_mask, mask = self._mask_quick()
            quick_expand, expand = self._expand_quick()
            quick_layers, layers = self._layers_quick()

            # The rest of each mode's settings, behind one accordion.
            with gr.Accordion(OPTIONS_LABELS["crop"], open=False, elem_id=_id("options"), elem_classes=["minipaint-options"]) as options:
                panel_crop = self._crop_panel(crop)
                panel_mask = self._mask_panel(mask)
                panel_expand = self._expand_panel(expand)
                panel_layers = self._layers_panel(layers)

            destination, reset_btn, save_btn, save_file = self._more_panel()

            # Hidden wires between chained events. Values, not DOM.
            crop_box = gr.Textbox("", visible=False, elem_id=_id("crop_box"))
            original_size = gr.Textbox("", visible=False, elem_id=_id("original_size"))
            wait_flag = gr.Textbox("", visible=False, elem_id=_id("wait"))
            switch_box = gr.Textbox("", visible=False, elem_id=_id("switch"))
            event_kind = gr.Textbox("", visible=False, elem_id=_id("event"))
            payload_box = gr.Textbox("", visible=False, elem_id=_id("payload"))
            mask_payload_box = gr.Textbox("", visible=False, elem_id=_id("mask_payload"))
            # The browser drops a dragged layer here; the server answers with
            # what it needs for the next drag.
            layers["move"] = gr.Textbox("", visible=False, elem_id=_id("layer_move"))
            layers["preview"] = gr.Textbox("", visible=False, elem_id=_id("layer_preview"))
            layers["underlay"] = gr.Textbox("", visible=False, elem_id=_id("layer_underlay"))

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
            quick={"crop": quick_crop, "mask": quick_mask, "expand": quick_expand, "layers": quick_layers},
            panels={"crop": panel_crop, "mask": panel_mask, "expand": panel_expand, "layers": panel_layers},
            options=options,
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

    # -- quick rows ----------------------------------------------------------

    def _crop_quick(self):
        with gr.Row(elem_id=_id("quick_crop"), elem_classes=["minipaint-quick"]) as row:
            aspect = gr.Dropdown(
                ASPECTS, value="Free", label="Aspect", show_label=False, elem_id=_id("crop_aspect"), elem_classes=["minipaint-aspect"]
            )
            apply = gr.Button("Apply Crop", variant="primary", elem_id=_id("crop_apply"), elem_classes=["minipaint-apply"])
        return row, {"aspect": aspect, "apply": apply}

    def _mask_quick(self):
        with gr.Row(elem_id=_id("quick_mask"), elem_classes=["minipaint-quick"], visible=False) as row:
            tool = gr.Radio(TOOLS, value="Paint", label="Tool", show_label=False, elem_id=_id("mask_tool"), elem_classes=["minipaint-chips"])
            size = gr.Slider(1, 100, value=self.brush_width, step=1, label="Brush size", elem_id=_id("mask_size"), elem_classes=["minipaint-size"])
            clear = gr.Button("Clear Mask", elem_id=_id("mask_clear"), elem_classes=["minipaint-quick-action"])
            invert = gr.Button("Invert Mask", elem_id=_id("mask_invert"), elem_classes=["minipaint-quick-action"])
        return row, {"tool": tool, "size": size, "clear": clear, "invert": invert}

    def _expand_quick(self):
        with gr.Column(elem_id=_id("quick_expand"), elem_classes=["minipaint-quick", "minipaint-quick-column"], visible=False) as column:
            with gr.Row():
                amount = gr.Radio(outpaint.AMOUNTS, value="128", label="Add", show_label=False, elem_id=_id("expand_amount"), elem_classes=["minipaint-chips"])
                side_buttons = {
                    side: gr.Button(side, elem_id=_id(f"expand_{side.lower()}"), elem_classes=["minipaint-side"]) for side in outpaint.SIDES
                }
                clear_sides = gr.Button("Clear", elem_id=_id("expand_clear"), elem_classes=["minipaint-side"])
                apply = gr.Button("Apply Expand", variant="primary", elem_id=_id("expand_apply"), elem_classes=["minipaint-apply"])
            preview = gr.Markdown("No image yet.", elem_id=_id("expand_preview"), elem_classes=["minipaint-preview"])
        return column, {"amount": amount, "sides": side_buttons, "clear": clear_sides, "preview": preview, "apply": apply}

    def _layers_quick(self):
        with gr.Row(elem_id=_id("quick_layers"), elem_classes=["minipaint-quick"], visible=False) as row:
            pick = gr.Dropdown([], value=None, label="Layer", show_label=False, elem_id=_id("layer_pick"), elem_classes=["minipaint-layer-pick"])
            new = gr.Button("New from selection", variant="primary", elem_id=_id("layer_new"), elem_classes=["minipaint-quick-action"])
            merge = gr.Button("Merge down", elem_id=_id("layer_merge"), elem_classes=["minipaint-quick-action"])
            delete = gr.Button("Delete layer", elem_id=_id("layer_delete"), elem_classes=["minipaint-quick-action"])
        return row, {"pick": pick, "new": new, "merge": merge, "delete": delete}

    # -- options panels ------------------------------------------------------

    def _crop_panel(self, crop):
        with gr.Column(elem_id=_id("panel_crop"), elem_classes=["minipaint-panel"]) as panel:
            with gr.Row():
                crop["width"] = gr.Number(label="Custom ratio width", value=1024, precision=0, minimum=1, elem_id=_id("crop_custom_w"))
                crop["height"] = gr.Number(label="Custom ratio height", value=1024, precision=0, minimum=1, elem_id=_id("crop_custom_h"))
            gr.Markdown(
                "The frame starts over the whole image. Drag the image under it with one finger, pinch or scroll to zoom, "
                "drag a corner to resize it; the aspect locks its shape. **Apply Crop** keeps what is inside.",
                elem_classes=["minipaint-hint"],
            )
        return panel

    def _mask_panel(self, mask):
        with gr.Column(elem_id=_id("panel_mask"), elem_classes=["minipaint-panel"], visible=False) as panel:
            mask["to_layer"] = gr.Button("Masked area → new layer", elem_id=_id("mask_to_layer"))
            mask["smoothing"] = gr.Radio(
                imaging.SMOOTHING_LEVELS,
                value="Off",
                label="Edge smoothing (applied when sending)",
                elem_id=_id("mask_smoothing"),
                elem_classes=["minipaint-chips"],
            )
            gr.Markdown(
                "Paint over what should change. Two fingers pan and zoom; Move lets one finger pan. "
                "Undo takes strokes back first, then the bigger steps.",
                elem_classes=["minipaint-hint"],
            )
        return panel

    def _expand_panel(self, expand):
        with gr.Column(elem_id=_id("panel_expand"), elem_classes=["minipaint-panel"], visible=False) as panel:
            with gr.Row():
                expand["numbers"] = {
                    side: gr.Number(label=side, value=0, precision=0, minimum=0, elem_id=_id(f"expand_num_{side.lower()}"))
                    for side in outpaint.SIDES
                }
            with gr.Row():
                expand["overlap"] = gr.Slider(0, 256, value=32, step=8, label="Overlap into the original (px)", elem_id=_id("expand_overlap"))
                expand["fill"] = gr.Dropdown(outpaint.FILL_POLICIES, value=outpaint.DEFAULT_FILL, label="New area", elem_id=_id("expand_fill"))
                expand["snap"] = gr.Dropdown(settings.SNAP_CHOICES, value=self.snap_default, label="Snap to", elem_id=_id("expand_snap"))
            gr.Markdown(
                "Tap an amount and a side, or type exact numbers here. **Apply Expand** adds the room and masks the new area, "
                "plus the overlap back into the original so the model can blend.",
                elem_classes=["minipaint-hint"],
            )
        return panel

    def _layers_panel(self, layers):
        with gr.Column(elem_id=_id("panel_layers"), elem_classes=["minipaint-panel"], visible=False) as panel:
            layers["visible"] = gr.CheckboxGroup(
                [], value=[], label="Visible layers", elem_id=_id("layer_visible"), elem_classes=["minipaint-chips", "minipaint-chips-wrap"]
            )
            with gr.Row():
                layers["opacity"] = gr.Slider(0, 100, value=100, step=1, label="Opacity of the active layer", elem_id=_id("layer_opacity"))
                layers["name"] = gr.Textbox("", label="Name of the active layer", elem_id=_id("layer_name"))
                layers["rename"] = gr.Button("Rename", elem_id=_id("layer_rename"))
            with gr.Row():
                layers["up"] = gr.Button("Move up", elem_id=_id("layer_up"))
                layers["down"] = gr.Button("Move down", elem_id=_id("layer_down"))
                layers["duplicate"] = gr.Button("Duplicate", elem_id=_id("layer_duplicate"))
                layers["flatten"] = gr.Button("Flatten all", elem_id=_id("layer_flatten"))
            gr.Markdown(
                "The frame is the selection: **New from selection** copies what the active layer has inside it into a "
                "layer of its own; *Mask options → Masked area → new layer* does the same for a painted area. "
                "In Layers mode one finger (or the left mouse button) drags the active layer; two fingers, the wheel "
                "and the right button still pan and zoom. Sending flattens the visible layers.",
                elem_classes=["minipaint-hint"],
            )
        return panel

    def _more_panel(self):
        with gr.Accordion("More", open=False, elem_id=_id("more")):
            with gr.Row():
                destination = gr.Dropdown(DESTINATIONS, value="Auto", label="Send to", elem_id=_id("destination"))
                reset_btn = gr.Button("Reset to original", elem_id=_id("reset"))
                save_btn = gr.Button("Save a copy", elem_id=_id("save"))
            save_file = gr.File(label="Saved copy", interactive=False, visible=False, elem_id=_id("save_file"))
            gr.Markdown(
                "Auto sends a plain image to img2img, and an image with a mask to Inpaint. "
                "Undo and Redo cover strokes, Open, Apply Crop, Apply Expand, Clear and Invert.",
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
        quick = parts["quick"]
        panels = parts["panels"]
        mode_btns = parts["mode_btns"]
        background = self.surface.background
        foreground = self.surface.foreground
        wait_flag = parts["wait_flag"]
        switch_box = parts["switch_box"]
        event_kind = parts["event_kind"]

        side_inputs = [expand["numbers"][side] for side in outpaint.SIDES]
        preview_inputs = [state] + side_inputs + [expand["snap"]]

        mode_outputs = [
            mode_state,
            *(quick[mode] for mode in MODES),
            *(panels[mode] for mode in MODES),
            *(mode_btns[mode] for mode in MODES),
            send_btn,
            mask["tool"],
            parts["options"],
            layers["pick"],
            layers["visible"],
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
                .then(self.commit_foreground, inputs=[state], outputs=[foreground], **quiet)
            )

        # -- modes: panels and labels from Python; the browser follows the
        # mode textbox, whichever step changed it
        for mode, button in mode_btns.items():
            button.click(lambda state, mode=mode: self.set_mode(mode, state), inputs=[state], outputs=mode_outputs, **quiet)
        mode_state.change(None, js=MODE_JS, inputs=[mode_state])

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

        # -- layers: selection to layer, the drag's landing, and the panel
        structural(layers["new"].click, self.new_layer_from_selection, canvas_inputs + [parts["crop_box"]], js=SELECT_JS)
        structural(mask["to_layer"].click, self.mask_to_layer, canvas_inputs, keep=True)
        structural(layers["move"].input, self.move_layer, [layers["move"]] + canvas_inputs, keep=True)
        layers["pick"].input(self.pick_layer, inputs=[layers["pick"], state, mode_state], outputs=info_outputs, **quiet)
        structural(layers["visible"].input, self.layer_visibility, [layers["visible"]] + canvas_inputs, keep=True)
        structural(layers["opacity"].release, self.layer_opacity, [layers["opacity"]] + canvas_inputs, keep=True)
        structural(layers["rename"].click, self.layer_rename, [layers["name"]] + canvas_inputs, keep=True)
        structural(layers["up"].click, lambda *args: self.layer_order(1, *args), canvas_inputs, keep=True)
        structural(layers["down"].click, lambda *args: self.layer_order(-1, *args), canvas_inputs, keep=True)
        structural(layers["merge"].click, self.layer_merge, canvas_inputs, keep=True)
        structural(layers["delete"].click, self.layer_delete, canvas_inputs, keep=True)
        structural(layers["duplicate"].click, self.layer_duplicate, canvas_inputs, keep=True)
        structural(layers["flatten"].click, self.layer_flatten, canvas_inputs, keep=True)

        # -- open, history, reset
        structural(parts["open_btn"].upload, self.open_file, [parts["open_btn"], state, mode_state])
        structural(parts["undo_btn"].click, self.undo, [state, mode_state, event_kind], js=UNDO_JS)
        structural(parts["redo_btn"].click, self.redo, [state, mode_state, event_kind], js=REDO_JS)
        structural(parts["reset_btn"].click, self.reset, [state, mode_state])

        # -- view: fit and focus are browser-only
        parts["fit_btn"].click(None, js=FIT_JS)
        parts["focus_btn"].click(None, js=FOCUS_ON_JS)
        parts["focus_exit_btn"].click(None, js=FOCUS_OFF_JS)

        # -- save
        parts["save_btn"].click(self.save_copy, inputs=[background, foreground, state], outputs=[parts["save_file"]], **quiet)

        # -- send: the image into the host's own inputs (Extras from the
        # backend, img2img and Inpaint from the browser), then the host's own
        # tab switch; for Inpaint, the mask layer once that canvas has the
        # image.
        payload_box = parts["payload_box"]
        target_components = [self.targets[key] for key in self.image_targets]
        sent = send_btn.click(
            self.send,
            inputs=[background, foreground, state, mode_state, parts["destination"], mask["smoothing"]],
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
