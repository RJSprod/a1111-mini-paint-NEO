"""Receive, crop, mask, expand, undo and send, checked on what the callbacks
return: the image and mask layer the canvas would show, and the values the
host's img2img / Inpaint inputs would receive.

The canvas is stood in for by what its two hidden textboxes deliver: the
image as PIL, and the scribble layer as an RGBA image whose alpha is the
mask.
"""

from harness import Results, setup_path, value_of

setup_path()

import json  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402

from modules import script_callbacks  # noqa: E402
from PIL import Image  # noqa: E402

import forge_like  # noqa: E402
from minipaint_neo import router  # noqa: E402
from minipaint_neo.canvas import host, imaging  # noqa: E402
from minipaint_neo.canvas import ui as canvas_ui  # noqa: E402


def layer(mask, size):
    """The scribble layer the canvas would hold for a mask."""
    return imaging.foreground_layer(mask, size, (128, 128, 128)) if mask is not None else None


def decode_data_url(text):
    import base64
    import io

    return Image.open(io.BytesIO(base64.b64decode(text.split(",", 1)[1])))


def skipped(value) -> bool:
    return isinstance(value, dict) and "value" not in value and value.get("__type__") == "update"


def run() -> Results:
    r = Results("workflow")

    built = {}
    original_create_ui = canvas_ui.create_ui

    def capturing_create_ui():
        built["canvas"] = original_create_ui()
        return built["canvas"]

    canvas_ui.create_ui = capturing_create_ui
    script_callbacks.callbacks["after_component"][:] = [host.on_after_component]
    host.reset_capture()
    try:
        demo, refs = forge_like.build_host(router.on_ui_tabs)
    finally:
        canvas_ui.create_ui = original_create_ui

    canvas = built.get("canvas")
    r.check("the canvas was built", canvas is not None)
    if canvas is None:
        return r
    r.check("every destination was found", set(canvas.targets) == {"img2img", "inpaint", "inpaint_mask", "extras", "stitch_txt2img", "stitch_txt2img_enable", "stitch_img2img", "stitch_img2img_enable"}, str(sorted(canvas.targets)))
    r.check("Extras and the ImageStitch galleries are written from the backend", canvas.image_targets == ["extras", "stitch_txt2img", "stitch_img2img"], str(canvas.image_targets))
    r.check("the inpaint mask layer was found", canvas.targets["inpaint_mask"] is refs["init_img_with_mask"].foreground)
    r.check("the ImageStitch galleries and their boxes are the host's", canvas.targets["stitch_txt2img"] is refs["txt2img_stitch_gallery"] and canvas.targets["stitch_img2img_enable"] is refs["img2img_stitch_enable"])
    r.check("the destinations offered are the ones found, in the menu's order", canvas.destinations == ["img2img", "inpaint", "extras", "stitch_txt2img", "stitch_img2img"], str(canvas.destinations))

    # ---- the session rebuild Gradio does after an update output, under Forge's patches ----
    from minipaint_neo.canvas import surface as surface_module

    rebuilt = type(canvas.surface.background)(**canvas.surface.background.constructor_args)
    r.check("a rebuilt canvas textbox still reads images, not arrays", rebuilt.numpy is False and type(rebuilt) is surface_module.canvas_image_class())
    host_rebuilt = type(refs["init_img"].background)(**refs["init_img"].background.constructor_args)
    r.check("(the host's own textbox would not: that is why the backend never answers it)", host_rebuilt.numpy is True)

    # commit-shaped replies (see TouchCanvas._commit / _info / _mode_updates);
    # replies that start with the mask layer instead of the image are one shorter
    BG, FG, STATE, STATUS, PREVIEW, ASPECT, ORIGINAL, WAIT, SUGGEST, MODE = range(10)
    # after the mode: 4 rail panels, the tool, then the layer list, the size,
    # the opacity, the name, the drag preview and underlay
    TOOL = MODE + 5
    LAYER_LIST, LAYER_SCALE, LAYER_OPACITY, LAYER_NAME, LAYER_PREVIEW, LAYER_UNDERLAY = range(MODE + 6, MODE + 12)
    COMMIT_LEN = 2 + canvas.INFO_COUNT
    r.check("the commit shape is the two canvas values plus the information", COMMIT_LEN == 21 and canvas.MODE_COUNT == 12)

    # ---- receive from txt2img ----
    photo = Image.new("RGB", (640, 480), (20, 120, 220))
    out = canvas.receive([(photo, None)], None, "mask", "txt2img")
    doc = out[STATE]
    r.check("reply has the commit shape", len(out) == COMMIT_LEN)
    r.check("received image becomes the document", doc.size == (640, 480) and doc.origin == "txt2img")
    r.check("receive writes a display copy of the image to the canvas (JPEG: it is opaque)", isinstance(out[BG], str) and out[BG].startswith("data:image/jpeg;base64,") and decode_data_url(out[BG]).size == (640, 480))
    r.check("receive clears the mask layer", out[FG] is None and out[WAIT] == "")
    r.check("receive selects crop mode", out[MODE] == "crop")
    r.check("the picture is Layer 1 over a white Background", doc.layer_names() == ["Background", "Layer 1"] and doc.active == 1 and doc.layers[0].image.getpixel((0, 0)) == (255, 255, 255, 255))
    r.check("the layer list is sent, with both layers, the picture on top and selected", isinstance(out[LAYER_LIST], str) and out[LAYER_LIST].count('role="listitem"') == 2
            and out[LAYER_LIST].index('data-name="Layer 1"') < out[LAYER_LIST].index('data-name="Background"') and 'minipaint-layer selected active" role="listitem" data-name="Layer 1"' in out[LAYER_LIST])
    r.check("receive reports the original size", out[ORIGINAL] == "640x480")
    r.check("status says where it came from, on one line", "Received from txt2img" in out[STATUS] and "640 × 480" in out[STATUS] and "\n" not in out[STATUS])
    r.check("a plain picture suggests img2img", out[SUGGEST] == "img2img")
    r.check("a second receive says the first is one undo away", "one Undo away" in canvas.receive([(photo, None)], doc, "crop", "img2img")[STATUS])
    unchanged = canvas.receive(None, doc, "crop", "txt2img")
    r.check("nothing to receive is reported without touching the canvas", "Pick an image" in unchanged[STATUS] and skipped(unchanged[BG]) and skipped(unchanged[FG]))

    # ---- the canvas echoes what it was sent: nothing happens ----
    bg = imaging.to_rgba(doc.image)
    echo = canvas.on_canvas_image(None, doc, "crop", "echo")
    r.check("an echo changes nothing", len(echo) == 1 + canvas.INFO_COUNT and all(skipped(v) for v in echo))
    same = canvas.on_canvas_image(imaging.from_png_bytes(imaging.to_png_bytes(bg)), doc, "crop", "user")
    r.check("the same picture opened again changes nothing", all(skipped(v) for v in same))

    # ---- a picture opened on the canvas in the browser becomes the document ----
    opened = Image.new("RGB", (300, 200), (5, 5, 5))
    out = canvas.on_canvas_image(opened, doc, "mask", "user")
    doc = out[STATE - 1]
    r.check("an opened picture is taken as the document", doc.size == (300, 200) and doc.origin == "file")
    r.check("its stale strokes are cleared", out[0] is None)
    r.check("and the previous picture is one undo away", "Opened." in out[STATUS - 1] and "one Undo away" in out[STATUS - 1] and doc.history[-1]["label"] == "open")
    r.check("opening lands in crop mode with a free aspect", out[MODE - 1] == "crop" and value_of(out[ASPECT - 1]) == "Free")
    out = canvas.undo(doc, "crop")
    doc = out[STATE]
    r.check("undo of the open restores the received image", doc.size == (640, 480) and "Undid open" in out[STATUS])

    # ---- apply crop: the frame the browser reported ----
    bg = imaging.to_rgba(doc.image)
    out = canvas.apply_crop(None, doc, "crop", '{"x0": 100, "y0": 50, "x1": 420, "y1": 290}')
    doc = out[STATE]
    r.check("crop committed, on both layers", doc.size == (320, 240) and "Cropped to 320 × 240" in out[STATUS] and doc.layers[0].size == (320, 240) and doc.layers[1].size == (320, 240))
    r.check("crop writes the cropped image", isinstance(out[BG], str) and decode_data_url(out[BG]).size == (320, 240))
    r.check("crop keeps the original", doc.original.size == (640, 480) and out[ORIGINAL] == "640x480")
    r.check("the previous size is one undo away", doc.history and doc.history[-1]["label"] == "crop")
    r.check("aspect is reset after a crop", value_of(out[ASPECT]) == "Free")
    r.check("expand preview shows the new size", "320 × 240" in out[PREVIEW])

    bg = imaging.to_rgba(doc.image)
    out = canvas.apply_crop(None, doc, "crop", '{"x0": 0, "y0": 0, "x1": 320, "y1": 240}')
    r.check("a frame over the whole image is not a crop", "nothing to crop" in out[STATUS] and skipped(out[BG]))
    out = canvas.apply_crop(None, doc, "crop", "")
    r.check("no frame is explained", "Draw a rectangle" in out[STATUS] and skipped(out[BG]))
    r.check("a crop without an image is refused", "no image" in canvas.apply_crop(None, None, "crop", '{"x0":0,"y0":0,"x1":1,"y1":1}')[STATUS])

    # a crop with strokes on the canvas keeps the covered part of the mask
    stroke = Image.new("L", (320, 240), 0)
    stroke.paste(255, (60, 40, 120, 100))
    out = canvas.apply_crop(layer(stroke, (320, 240)), doc, "crop", '{"x0": 40, "y0": 20, "x1": 240, "y1": 140}')
    doc = out[STATE]
    r.check("crop with a mask keeps the mask", doc.size == (200, 120) and doc.has_mask and doc.mask.getpixel((40, 40)) == 255 and doc.mask.getpixel((5, 5)) == 0)
    r.check("a mask suggests Inpaint", out[SUGGEST] == "inpaint")
    r.check("the mask layer waits for the image", skipped(out[FG]) and out[WAIT] == "wait")
    fg = canvas.commit_foreground(doc, out[WAIT])
    r.check("the mask layer is written afterwards, at the new size", isinstance(fg, Image.Image) and fg.size == (200, 120) and fg.getchannel("A").getpixel((40, 40)) == 255)
    r.check("the mask layer uses the Inpaint tab's checkerboard", fg.getpixel((40, 40))[:3] in ((0, 0, 0), (255, 255, 255)))
    r.check("no mask means no layer step", skipped(canvas.commit_foreground(canvas.receive([(photo, None)], None, "crop", "txt2img")[STATE], "wait")))
    r.check("a step that left the canvas alone leaves the strokes alone too", skipped(canvas.commit_foreground(doc, "")))

    # ---- undo / redo of structural steps ----
    out = canvas.undo(doc, "crop")
    doc = out[STATE]
    r.check("undo restores the earlier crop, strokes included", doc.size == (320, 240) and "Undid crop" in out[STATUS] and doc.has_mask and out[WAIT] == "wait")
    out = canvas.redo(doc, "crop")
    doc = out[STATE]
    r.check("redo reapplies it, mask included", doc.size == (200, 120) and doc.has_mask and "Redid crop" in out[STATUS] and out[WAIT] == "wait")
    out = canvas.redo(doc, "crop")
    r.check("nothing to redo is a message, not a reload", "Nothing to redo" in out[STATUS] and skipped(out[BG]))

    # ---- mask mode, asked for through the menu ----
    updates = canvas.set_mode("mask", doc)
    r.check("mode updates have the mode shape", len(updates) == canvas.MODE_COUNT)
    r.check("the tool button picks the mode", updates[0] == "mask")
    r.check("mask mode resets the tool", value_of(updates[5]) == "Paint")
    r.check("mask mode shows its rail panel only", [u.get("visible") for u in updates[1:5]] == [False, True, False, False])
    r.check("crop mode leaves the tool alone", skipped(canvas.set_mode("crop", doc)[5]))
    r.check("outside Layers mode the drag previews are not sent", skipped(updates[10]) and skipped(updates[11]))
    r.check("an unknown mode is crop", canvas.set_mode("nonsense", doc)[0] == "crop" and canvas.set_mode(None, doc)[0] == "crop")
    r.check("the layer list is only re-sent when it changed", skipped(canvas.set_mode("mask", doc)[6]))

    # ---- undo and redo take a stroke first, when the browser took one back ----
    out = canvas.undo(doc, "mask", "stroke")
    r.check("a stroke undo touches nothing on the server", "Undid a stroke" in out[STATUS] and skipped(out[BG]) and out[STATE] is doc)
    r.check("a stroke redo likewise", "Redid a stroke" in canvas.redo(doc, "mask", "stroke")[STATUS])

    # ---- layers: the frame as a selection, the list, a drag, order, visibility, merge, flatten ----
    bg = imaging.to_rgba(doc.image)
    fg = layer(doc.mask, doc.size)
    updates = canvas.set_mode("layers", doc)
    r.check("layers mode sends the drag previews: the picture, and the Background under it", isinstance(updates[10], str) and '"src": "data:image/' in updates[10] and '"name": "Layer 1"' in updates[10] and '"canvas": [200, 120]' in updates[10] and updates[11].startswith("data:image/"))
    r.check("the previews are sent once until something changes", skipped(canvas.set_mode("layers", doc)[11]))

    def act(op, name, state, mode="layers"):
        return canvas.layer_action(json.dumps({"op": op, "name": name, "t": 1}), fg, state, mode)

    out = canvas.new_layer_from_selection(fg, doc, "layers", '{"x0": 20, "y0": 10, "x1": 120, "y1": 70}')
    doc = out[STATE]
    r.check("a selection becomes a layer above the active one", doc.layer_names() == ["Background", "Layer 1", "Layer 2"] and doc.active == 2)
    r.check("it holds the pixels where they were", doc.active_layer.size == (100, 60) and (doc.active_layer.x, doc.active_layer.y) == (20, 10))
    r.check("the canvas is left alone: nothing changed on it", skipped(out[BG]) and skipped(out[FG]) and out[WAIT] == "")
    r.check("the status names the new layer", "Layer 2 holds the selection" in out[STATUS] and "3 layers" in out[STATUS])
    listing = out[LAYER_LIST]
    r.check("the layer list follows, top layer first, the new one selected and primary",
            isinstance(listing, str) and listing.index('data-name="Layer 2"') < listing.index('data-name="Layer 1"') < listing.index('data-name="Background"')
            and 'minipaint-layer selected active" role="listitem" data-name="Layer 2"' in listing and 'minipaint-layer" role="listitem" data-name="Background"' in listing)
    r.check("the list carries every action", all(f'data-op="{op}"' in listing for op in ("pick", "toggle", "eye", "up", "down")) and 'data-op="up" data-name="Layer 2" title="Move Layer 2 up" disabled' in listing)
    r.check("a new preview and an underlay go to the browser", isinstance(out[LAYER_PREVIEW], str) and '"x": 20' in out[LAYER_PREVIEW] and '"src": "data:image/' in out[LAYER_PREVIEW] and out[LAYER_UNDERLAY].startswith("data:image/"))
    r.check("no frame is explained", "Draw a selection" in canvas.new_layer_from_selection(fg, doc, "layers", "")[STATUS])

    out = canvas.move_layer('{"dx": 15, "dy": -5, "t": 1}', fg, doc, "layers")
    doc = out[STATE]
    r.check("a drag moves the selected layer", (doc.active_layer.x, doc.active_layer.y) == (35, 5) and "at (35, 5)" in out[STATUS] and "Moved Layer 2" in out[STATUS])
    r.check("and not the layers beneath", (doc.layers[0].x, doc.layers[0].y) == (0, 0) and (doc.layers[1].x, doc.layers[1].y) == (0, 0))
    r.check("and reloads the composite", isinstance(out[BG], str) and decode_data_url(out[BG]).size == (200, 120))
    r.check("a move sends the new box without the picture, and no new underlay", '"x": 35' in out[LAYER_PREVIEW] and '"src"' not in out[LAYER_PREVIEW] and skipped(out[LAYER_UNDERLAY]))
    r.check("a drag of nothing changes nothing", skipped(canvas.move_layer('{"dx": 0, "dy": 0}', fg, doc, "layers")[BG]))
    r.check("the composite shows the moved layer", doc.image.getpixel((5, 5))[3] == 255)

    out = act("pick", "Background", doc)
    doc = out[STATE]
    r.check("tapping a layer selects it alone, without a reload", doc.selected_names() == ["Background"] and "Background is the active layer" in out[STATUS] and skipped(out[BG]) and out[WAIT] == "")
    r.check("the preview follows the selection", isinstance(out[LAYER_PREVIEW], str) and '"name": "Background"' in out[LAYER_PREVIEW])
    out = act("toggle", "Layer 2", doc)
    doc = out[STATE]
    r.check("the box adds a layer to the selection", doc.selected_names() == ["Background", "Layer 2"] and "2 layers selected" in out[STATUS] and doc.active == 2)
    r.check("the preview covers both, over the layer between them", '"name": "Background, Layer 2"' in out[LAYER_PREVIEW] and '"src": "data:image/' in out[LAYER_PREVIEW] and out[LAYER_UNDERLAY].startswith("data:image/"))
    out = canvas.move_layer('{"dx": 3, "dy": 3, "t": 2}', fg, doc, "layers")
    doc = out[STATE]
    r.check("a drag moves every selected layer and no other", (doc.layers[0].x, doc.layers[0].y) == (3, 3) and (doc.layers[2].x, doc.layers[2].y) == (38, 8) and (doc.layers[1].x, doc.layers[1].y) == (0, 0) and "Moved Background, Layer 2" in out[STATUS])
    out = canvas.undo(doc, "layers")
    doc = out[STATE]
    r.check("undo takes the move back, selection included", (doc.layers[0].x, doc.layers[0].y) == (0, 0) and doc.selected_names() == ["Background", "Layer 2"])
    out = act("toggle", "Background", doc)
    doc = out[STATE]
    r.check("the box takes a layer out again", doc.selected_names() == ["Layer 2"] and "Layer 2 is the active layer" in out[STATUS])
    r.check("a tap on a layer that is not there changes nothing", skipped(act("pick", "nope", doc)[BG]) and doc.selected_names() == ["Layer 2"])
    r.check("an unknown action changes nothing", skipped(act("dance", "Layer 2", doc)[BG]))

    out = canvas.layer_center(fg, doc, "layers")
    doc = out[STATE]
    r.check("re-center brings the layer to the middle of the canvas", (doc.active_layer.x, doc.active_layer.y) == (50, 30) and "Centered Layer 2" in out[STATUS] and isinstance(out[BG], str))
    out = canvas.undo(doc, "layers")
    doc = out[STATE]
    r.check("undo takes the centering back", "Undid center layer" in out[STATUS] and (doc.active_layer.x, doc.active_layer.y) == (35, 5))

    # resizing: the slider, the half / double / 100% buttons
    out = canvas.layer_scale_by(2, fg, doc, "layers")
    doc = out[STATE]
    r.check("double makes the layer twice as big about its centre, and reloads", doc.active_layer.size == (200, 120) and (doc.active_layer.x, doc.active_layer.y) == (-15, -25)
            and "Layer 2 at 200% size" in out[STATUS] and isinstance(out[BG], str) and out[LAYER_SCALE].get("value") == 200 and "200% size" in out[LAYER_LIST]
            and '"src": "data:image/' in out[LAYER_PREVIEW])
    out = canvas.layer_scale(50, fg, doc, "layers")
    doc = out[STATE]
    r.check("the slider sets a size from the original pixels", doc.active_layer.size == (50, 30) and doc.active_layer.percent == 50 and "at 50% size" in out[STATUS])
    r.check("the same size again changes nothing and leaves no undo step", skipped(canvas.layer_scale(50, fg, doc, "layers")[BG]) and doc.history[-1]["label"] == "resize layer")
    out = canvas.layer_scale_by(None, fg, doc, "layers")
    doc = out[STATE]
    r.check("100% is the original pixels again", doc.active_layer.size == (100, 60) and doc.active_layer.source is None and (doc.active_layer.x, doc.active_layer.y) == (35, 5))
    r.check("a size no browser canvas holds is refused", "keep a side under" in canvas.layer_scale(400, fg, canvas.receive([(Image.new("RGB", (3000, 3000)), None)], None, "layers", "txt2img")[STATE], "layers")[STATUS])
    # transform mode's Done: the box as the browser left it
    out = canvas.transform_layer('{"x": 10, "y": 8, "w": 50, "t": 1}', fg, doc, "layers")
    doc = out[STATE]
    r.check("Done places and sizes the layer, and says so", "Layer 2: 50 × 30 at (10, 8)." in out[STATUS] and doc.active_layer.percent == 50 and (doc.active_layer.x, doc.active_layer.y) == (10, 8) and isinstance(out[BG], str) and doc.history[-1]["label"] == "transform layer")
    r.check("its preview carries the new picture", '"src": "data:image/' in out[LAYER_PREVIEW] and '"x": 10' in out[LAYER_PREVIEW])
    steps = len(doc.history)
    out = canvas.transform_layer('{"x": 10, "y": 8, "w": 50, "t": 2}', fg, doc, "layers")
    r.check("the same box again changes nothing, leaves no undo step, and still sends the picture (the browser waits for it)", "Nothing moved" in out[STATUS] and len(doc.history) == steps and isinstance(out[BG], str))
    r.check("a transform that cannot be read is ignored, the picture sent", "could not be read" in canvas.transform_layer('{"x": 1}', fg, doc, "layers")[STATUS] and isinstance(canvas.transform_layer("nonsense", fg, doc, "layers")[BG], str) and len(doc.history) == steps)
    big = canvas.receive([(Image.new("RGB", (3000, 3000)), None)], None, "layers", "txt2img")[STATE]
    out = canvas.transform_layer('{"x": 0, "y": 0, "w": 9000}', fg, big, "layers")
    r.check("a transform past what a canvas holds is refused and undone", "keep a side under" in out[STATUS] and big.active_layer.percent == 100 and not big.history and isinstance(out[BG], str))

    # a picture file added as a layer: fitted to the 200 x 120 canvas, which keeps its size; the browser is asked to open transform mode
    with tempfile.NamedTemporaryFile(prefix="wide-cat-", suffix=".png", delete=False) as handle:
        Image.new("RGB", (400, 100), (10, 200, 10)).save(handle.name)
        added_path = handle.name
    names_before = doc.layer_names()
    out = canvas.add_image_layer(added_path, fg, doc, "crop")
    doc = out[STATE]
    r.check("the file becomes a layer fitted to the canvas, selected, in Layers mode", out[MODE] == "layers" and "added as a layer, fitted to the canvas (200 × 50)" in out[STATUS] and doc.size == (200, 120) and len(doc.layer_names()) == len(names_before) + 1 and doc.active_layer.name.startswith("wide-cat-") and doc.active_layer.size == (200, 50) and (doc.active_layer.x, doc.active_layer.y) == (0, 35))
    r.check("from its own pixels", doc.active_layer.source is not None and doc.active_layer.source.size == (400, 100) and doc.active_layer.percent == 50)
    r.check("the reply reloads the picture and its preview asks for transform mode", isinstance(out[BG], str) and '"transform": true' in out[LAYER_PREVIEW] and '"src": "data:image/' in out[LAYER_PREVIEW])
    r.check("only once", '"transform"' not in doc.preview_payload())
    r.check("and it is one undo away", doc.history[-1]["label"] == "add layer" and "Undid add layer" in canvas.undo(doc, "layers")[STATUS] and doc.layer_names() == names_before)
    r.check("a file that is not an image says so", "Could not open" in canvas.add_image_layer(__file__, fg, doc, "layers")[STATUS] and "No file was chosen" in canvas.add_image_layer(None, fg, doc, "layers")[STATUS])
    r.check("without a picture there is nothing to add to", "no image" in canvas.add_image_layer(added_path, None, None, "layers")[STATUS].lower())
    os.unlink(added_path)
    out = canvas.undo(doc, "layers")
    doc = out[STATE]
    r.check("undo takes the transform back", "Undid transform layer" in out[STATUS] and doc.active_layer.percent == 100)
    r.check("nonsense is ignored", skipped(canvas.layer_scale("big", fg, doc, "layers")[BG]))
    out = canvas.undo(doc, "layers")
    doc = out[STATE]
    r.check("undo takes a resize back", "Undid resize layer" in out[STATUS] and doc.active_layer.size == (50, 30))
    doc = canvas.layer_scale_by(None, fg, doc, "layers")[STATE]

    out = act("down", "Layer 2", doc)
    doc = out[STATE]
    r.check("the arrow reorders", doc.layer_names() == ["Background", "Layer 2", "Layer 1"] and doc.active == 1 and "Layer 2 moved down" in out[STATUS] and isinstance(out[BG], str))
    doc = act("down", "Layer 2", doc)[STATE]
    r.check("at the bottom it says so", doc.layer_names() == ["Layer 2", "Background", "Layer 1"] and "already at the bottom" in act("down", "Layer 2", doc)[STATUS])
    doc = act("up", "Layer 2", doc)[STATE]
    out = act("up", "Layer 2", doc)
    doc = out[STATE]
    r.check("and back up", doc.layer_names() == ["Background", "Layer 1", "Layer 2"] and doc.active == 2)

    out = act("eye", "Background", doc)
    doc = out[STATE]
    r.check("the eye hides a layer and reloads the composite", not doc.layers[0].visible and "Background hidden" in out[STATUS] and isinstance(out[BG], str))
    r.check("the list shows it hidden, and the selection did not move", 'minipaint-layer hidden-layer" role="listitem" data-name="Background"' in out[LAYER_LIST] and doc.selected_names() == ["Layer 2"])
    doc = act("eye", "Layer 1", doc)[STATE]
    r.check("with the picture hidden too the composite is see-through where the layer is not", doc.image.getpixel((150, 100))[3] == 0 and doc.image.getpixel((40, 10))[3] == 255)
    doc = act("eye", "Layer 1", doc)[STATE]
    out = act("eye", "Background", doc)
    doc = out[STATE]
    r.check("the eye shows it again", all(l.visible for l in doc.layers) and "Background shown" in out[STATUS])

    out = canvas.layer_opacity(40, fg, doc, "layers")
    doc = out[STATE]
    r.check("opacity applies to the selected layer", doc.active_layer.opacity == 40 and "Layer 2 at 40% opacity" in out[STATUS] and out[LAYER_OPACITY].get("value") == 40)
    out = canvas.layer_rename("Cutout", fg, doc, "layers")
    doc = out[STATE]
    r.check("rename without a reload", doc.active_layer.name == "Cutout" and skipped(out[BG]) and out[LAYER_NAME].get("value") == "Cutout" and 'data-name="Cutout"' in out[LAYER_LIST])
    r.check("a blank name is refused", "Type a name" in canvas.layer_rename("  ", fg, doc, "layers")[STATUS])
    out = canvas.layer_duplicate(fg, doc, "layers")
    doc = out[STATE]
    r.check("duplicate copies the selected layer above it", doc.layer_names() == ["Background", "Layer 1", "Cutout", "Cutout copy"] and doc.active == 3 and "Cutout copy is a copy" in out[STATUS])
    out = canvas.layer_delete(fg, doc, "layers")
    doc = out[STATE]
    r.check("delete removes the selected layer", doc.layer_names() == ["Background", "Layer 1", "Cutout"] and "Cutout copy deleted" in out[STATUS] and doc.selected_names() == ["Cutout"])
    out = canvas.layer_merge(fg, doc, "layers")
    doc = out[STATE]
    r.check("merge with one layer selected merges down, named after the lower", doc.layer_names() == ["Background", "Layer 1"] and "Cutout merged into Layer 1" in out[STATUS])
    doc = act("pick", "Background", doc)[STATE]
    r.check("the bottom layer has nothing to merge into", "nothing below Background" in canvas.layer_merge(fg, doc, "layers")[STATUS])
    out = canvas.undo(doc, "layers")
    doc = out[STATE]
    r.check("undo restores the three layers", "Undid merge down" in out[STATUS] and doc.layer_names() == ["Background", "Layer 1", "Cutout"] and doc.layers[2].opacity == 40)

    # several selected: duplicate, delete and merge act on all of them
    doc = act("toggle", "Background", doc)[STATE]
    out = canvas.layer_duplicate(fg, doc, "layers")
    doc = out[STATE]
    r.check("duplicate copies every selected layer", doc.layer_names() == ["Background", "Background copy", "Layer 1", "Cutout", "Cutout copy"] and doc.selected_names() == ["Background copy", "Cutout copy"] and "are copies" in out[STATUS])
    out = canvas.layer_delete(fg, doc, "layers")
    doc = out[STATE]
    r.check("delete removes every selected layer", doc.layer_names() == ["Background", "Layer 1", "Cutout"] and "Background copy, Cutout copy deleted" in out[STATUS])
    doc = act("toggle", "Background", doc)[STATE]
    out = canvas.layer_merge(fg, doc, "layers")
    doc = out[STATE]
    r.check("merge with several selected merges them into one, named after the lowest", doc.layer_names() == ["Background", "Layer 1"] and "Background, Cutout merged into Background" in out[STATUS])
    out = canvas.undo(doc, "layers")
    doc = out[STATE]
    r.check("undo restores them", "Undid merge layers" in out[STATUS] and doc.layer_names() == ["Background", "Layer 1", "Cutout"])
    out = canvas.layer_flatten(fg, doc, "layers")
    doc = out[STATE]
    r.check("flatten leaves one layer", doc.layer_names() == ["Background"] and "3 layers flattened" in out[STATUS])
    r.check("with one layer there is nothing to merge or delete", "nothing below" in canvas.layer_merge(fg, doc, "layers")[STATUS] and "last layer stays" in canvas.layer_delete(fg, doc, "layers")[STATUS])
    canvas.undo(doc, "layers")

    # the mask as a freehand selection
    out = canvas.mask_to_layer(layer(stroke.resize((200, 120)), (200, 120)), doc, "mask")
    doc = out[STATE]
    r.check("a masked area becomes a layer, trimmed to it, and the mode switches to Layers",
            doc.active_layer.name == "Layer 3" and doc.active_layer.size[0] < 200 and out[MODE] == "layers" and skipped(out[BG]))
    r.check("without a mask it says so", "Paint over the area" in canvas.mask_to_layer(None, canvas.receive([(photo, None)], None, "mask", "txt2img")[STATE], "mask")[STATUS])
    canvas.undo(doc, "layers")
    act("pick", "Background", doc)
    out = canvas.set_mode("mask", doc)

    # ---- send, from Menu -> Send to: Inpaint gets the image first, the mask once its canvas has it ----
    bg = imaging.to_rgba(doc.image)
    fg = layer(doc.mask, doc.size)
    # a send reply: Extras and the two ImageStitch galleries, then the information (document first), then the instruction and the image payload
    T = len(canvas.image_targets)
    SENT_DOC, SENT_STATUS = T, T + 1
    EXTRAS, STITCH_T2I, STITCH_I2I = 0, 1, 2
    out = canvas.send(fg, doc, "mask", "inpaint:1700000001", "Off")
    doc = out[SENT_DOC]
    instruction, payload = out[-2], out[-1]
    r.check("inpaint names the size its canvas must reach", instruction == "inpaint:200x120")
    r.check("the request's nonce is dropped, and no request at all takes the suggestion", canvas.send(fg, doc, "mask", "", "Off")[-2] == "inpaint:200x120")
    r.check("extras and the stitch galleries are skipped", skipped(out[EXTRAS]) and skipped(out[STITCH_T2I]) and skipped(out[STITCH_I2I]))
    r.check("the image travels as a PNG data URL for the browser to write", payload.startswith("data:image/png;base64,") and decode_data_url(payload).size == (200, 120))
    r.check("status says sent", "Sent to img2img Inpaint" in out[SENT_STATUS])
    r.check("the layers are flattened for the destination", "3 layers were flattened" in out[SENT_STATUS])
    mask_out = canvas.send_mask(doc, instruction)
    r.check("inpaint then receives the mask as a data URL, alpha where the stroke was",
            mask_out.startswith("data:image/png;base64,") and (lambda m: m.size == (200, 120) and m.getchannel("A").getpixel((40, 40)) == 255 and m.getchannel("A").getpixel((5, 5)) == 0)(decode_data_url(mask_out)))
    r.check("the mask step is one-shot", canvas.send_mask(doc, instruction) == "")
    r.check("the document keeps the mask", doc.has_mask)

    # smoothing is applied on the way out only
    out = canvas.send(fg, doc, "mask", "inpaint", "Medium")
    r.check("smoothing note appears", "smoothing: Medium" in out[SENT_STATUS])
    smoothed = decode_data_url(canvas.send_mask(doc, out[-2]))
    r.check("the smoothed mask still covers the stroke", smoothed.getchannel("A").getpixel((40, 40)) == 255 and doc.mask.getpixel((40, 40)) == 255)

    # img2img drops the mask, and says so
    out = canvas.send(fg, doc, "mask", "img2img:1700000002", "Off")
    r.check("explicit img2img sends the image as a payload for the browser", out[-1].startswith("data:image/png;base64,") and skipped(out[EXTRAS]))
    r.check("dropping the mask is mentioned", "mask was not sent" in out[SENT_STATUS])
    r.check("switch goes to img2img", out[-2] == "img2img")
    r.check("no mask for img2img", canvas.send_mask(doc, "img2img") == "")

    # extras and the ImageStitch galleries are written from the backend
    out = canvas.send(fg, doc, "mask", "extras", "Off")
    r.check("extras receives the image itself, with no payload", isinstance(out[EXTRAS], Image.Image) and out[EXTRAS].size == (200, 120) and out[-1] == "" and out[-2] == "extras")
    out = canvas.send(fg, doc, "mask", "stitch_txt2img:1700000003", "Off")
    r.check("ImageStitch receives the image as the gallery's only entry", isinstance(out[STITCH_T2I], list) and len(out[STITCH_T2I]) == 1 and out[STITCH_T2I][0].size == (200, 120) and skipped(out[STITCH_I2I]) and skipped(out[EXTRAS]))
    staged = getattr(out[STITCH_T2I][0], "already_saved_as", None)
    r.check("the picture is saved where the host serves it from, and says so", staged and os.path.isfile(staged) and Image.open(staged).size == (200, 120), str(staged))
    r.check("Extras got one too", os.path.isfile(getattr(canvas.send(fg, doc, "mask", "extras", "Off")[EXTRAS], "already_saved_as", "")))
    r.check("the instruction names the stitch, with no payload", out[-2] == "stitch_txt2img" and out[-1] == "")
    r.check("status says so, and that the mask stayed", "Sent to ImageStitch (txt2img)" in out[SENT_STATUS] and "only reference image" in out[SENT_STATUS] and "mask was not sent" in out[SENT_STATUS])
    r.check("no mask step for a stitch", canvas.send_mask(doc, out[-2]) == "")
    out = canvas.send(fg, doc, "mask", "stitch_img2img", "Off")
    r.check("the img2img stitch is the other gallery", isinstance(out[STITCH_I2I], list) and skipped(out[STITCH_T2I]) and out[-2] == "stitch_img2img")

    # ---- see-through pixels: kept for img2img and the stitches, white for Extras, or filled by the setting ----
    from modules import shared  # noqa: E402
    from minipaint_neo import settings  # noqa: E402

    holed = doc
    holed.set_visible("Background", False)
    holed.set_visible("Layer 1", False)
    bg2 = imaging.to_rgba(holed.image)
    r.check("with the Background and the picture hidden the composite has see-through pixels", bg2.getpixel((150, 100))[3] == 0)
    out = canvas.send(fg, holed, "layers", "img2img", "Off")
    r.check("img2img gets them as they are, and the note says who fills them",
            decode_data_url(out[-1]).getpixel((150, 100))[3] == 0 and "see-through pixels were kept" in out[SENT_STATUS] and "img2img background colour" in out[SENT_STATUS])
    out = canvas.send(fg, holed, "layers", "stitch_txt2img", "Off")
    r.check("ImageStitch too", out[STITCH_T2I][0].getpixel((150, 100))[3] == 0)
    out = canvas.send(fg, holed, "layers", "extras", "Off")
    r.check("Extras gets white instead, since it has no background colour of its own", out[EXTRAS].getpixel((150, 100)) == (255, 255, 255, 255) and "filled with rgb(255, 255, 255)" in out[SENT_STATUS])
    shared.opts.data[settings.SEND_FILL] = "Black"
    out = canvas.send(fg, holed, "layers", "img2img", "Off")
    r.check("the setting can fill them with a colour instead", decode_data_url(out[-1]).getpixel((150, 100)) == (0, 0, 0, 255) and "filled with rgb(0, 0, 0)" in out[SENT_STATUS])
    shared.opts.data[settings.SEND_FILL] = settings.KEEP_TRANSPARENT
    holed.set_visible("Background", True)
    holed.set_visible("Layer 1", True)

    # inpaint without a mask clears the layer there
    plain = canvas.receive([(photo, None)], None, "crop", "txt2img")[STATE]
    out = canvas.send(None, plain, "crop", "inpaint", "Off")
    r.check("inpaint without a mask still goes to inpaint", out[-2] == "inpaint:640x480")
    r.check("and clears the layer there", canvas.send_mask(plain, out[-2]) == "")
    r.check("send without an image says so", "no image to send" in canvas.send(None, None, "crop", "Auto", "Off")[SENT_STATUS].lower())

    # ---- expand: new area auto-masked, transparent pixels filled on send ----
    out = canvas.apply_expand(fg, doc, "crop", 0, 128, 0, 64, 16, "Transparent", "8")
    doc = out[STATE]
    r.check("expanded size", doc.size == (328, 184) and doc.has_expansion, str(doc.size))
    r.check("expand grows the Background and shifts the layers on it", doc.layers[0].size == (328, 184) and doc.layers[1].size == (200, 120) and (doc.layers[1].x, doc.layers[1].y) == (0, 0))
    r.check("expand switches to mask mode", out[MODE] == "mask" and value_of(out[TOOL]) == "Paint")
    r.check("the new area is masked", doc.mask.getpixel((300, 90)) == 255 and doc.mask.getpixel((100, 100)) == 0)
    r.check("the old stroke is carried", doc.mask.getpixel((40, 40)) == 255)
    r.check("an expansion suggests Inpaint, and says so", out[SUGGEST] == "inpaint expansion")
    r.check("expand writes the image and waits for it before the mask", isinstance(out[BG], str) and decode_data_url(out[BG]).size == (328, 184) and out[WAIT] == "wait")
    r.check("the overlap is explained", "16px back" in out[STATUS])
    r.check("a mask layer follows at the new size", canvas.commit_foreground(doc).size == (328, 184))

    bg = imaging.to_rgba(doc.image)
    out = canvas.send(layer(doc.mask, doc.size), doc, "mask", "", "Off")
    sent = decode_data_url(out[-1])
    r.check("the expansion is sent see-through by default, for the WebUI to fill", sent.getchannel("A").getextrema()[0] == 0 and "see-through pixels were kept" in out[SENT_STATUS])
    shared.opts.data[settings.SEND_FILL] = "Black"
    out = canvas.send(layer(doc.mask, doc.size), doc, "mask", "", "Off")
    sent = decode_data_url(out[-1])
    r.check("or filled, by the setting", sent.getchannel("A").getextrema() == (255, 255) and "filled with rgb(0, 0, 0)" in out[SENT_STATUS])
    shared.opts.data[settings.SEND_FILL] = settings.KEEP_TRANSPARENT
    r.check("mask matches the image size", decode_data_url(canvas.send_mask(doc, out[-2])).size == sent.size)
    out = canvas.apply_expand(layer(doc.mask, doc.size), doc, "mask", 0, 0, 0, 0, 0, "Transparent", "8")
    r.check("expanding nothing is refused without touching the canvas", skipped(out[BG]) and skipped(out[FG]) and out[STATE].has_mask)
    # the canvas is the truth: strokes that are not on it are not in the document either
    out = canvas.apply_expand(None, doc, "mask", 0, 0, 0, 0, 0, "Transparent", "8")
    r.check("a canvas without strokes means no mask", not out[STATE].has_mask)
    doc = out[STATE]
    doc.commit(doc.image, doc.mask if doc.has_mask else None)
    out = canvas.invert_mask(None, doc, "mask")  # everything
    out = canvas.invert_mask(out[0], out[STATE - 1], "mask")  # back to nothing
    r.check("invert twice is nothing, and clears the layer", out[0] is None and not out[STATE - 1].has_mask)
    doc = out[STATE - 1]
    doc.commit(doc.image, stroke.resize((328, 184)))

    # ---- clear and invert ----
    out = canvas.clear_mask(layer(doc.mask, doc.size), doc, "mask")
    doc = out[STATE - 1]
    r.check("mask cleared, image intact", not doc.has_mask and doc.size == (328, 184) and out[0] is None)
    out = canvas.clear_mask(None, doc, "mask")
    r.check("clearing nothing is a message", "no mask to clear" in out[STATUS - 1] and skipped(out[0]))
    out = canvas.invert_mask(None, doc, "mask")
    doc = out[STATE - 1]
    r.check("invert of nothing masks everything", doc.has_mask and doc.mask.getextrema() == (255, 255) and isinstance(out[0], Image.Image))
    out = canvas.invert_mask(layer(stroke.resize((328, 184)), (328, 184)), doc, "mask")
    doc = out[STATE - 1]
    r.check("strokes on the canvas are read before inverting", doc.mask.getpixel((5, 5)) == 255 and doc.mask.getpixel((80, 60)) == 0)
    r.check("undo restores the mask before the invert", canvas.undo(doc, "mask")[STATE].mask.getpixel((80, 60)) == 255)

    # ---- reset to original ----
    out = canvas.reset(doc, "mask")
    doc = out[STATE]
    r.check("reset restores the received image, as Layer 1 over a Background", doc.size == (640, 480) and not doc.has_mask and not doc.has_expansion and doc.layer_names() == ["Background", "Layer 1"])
    r.check("reset goes back to crop mode", out[MODE] == "crop")
    r.check("reset with nothing is a message", "nothing to reset" in canvas.reset(None, "crop")[STATUS].lower())

    # ---- expand helpers ----
    values = canvas.add_side("Right", "64", doc, 0, 0, 0, 0, "8")
    r.check("a side button adds the amount", values[1] == 64 and "704 × 480" in values[4])
    values = canvas.add_side("Right", "100", doc, 0, 64, 0, 0, "32")
    r.check("amounts snap", values[1] == 160)
    r.check("clear sides", canvas.clear_sides(doc)[:4] == (0, 0, 0, 0))
    r.check("preview follows the numbers", "768 × 480" in canvas.expand_preview(doc, 64, 64, 0, 0, "Off"))

    # ---- open from a file ----

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
        Image.new("RGB", (90, 60), (1, 2, 3)).save(handle.name)
    out = canvas.open_file(handle.name, doc, "mask")
    doc = out[STATE]
    r.check("open loads the file", doc.size == (90, 60) and doc.filename and "Opened." in out[STATUS] and out[MODE] == "crop")
    r.check("open of a bad file is a message", "Could not open" in canvas.open_file(__file__, doc, "crop")[STATUS])
    r.check("open of nothing is a message", "No file" in canvas.open_file(None, doc, "crop")[STATUS])

    # ---- save a copy ----
    saved = canvas.save_copy(None, doc)
    r.check("save writes a png", saved.get("visible") is True and Image.open(saved["value"]).size == (90, 60))

    return r


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
