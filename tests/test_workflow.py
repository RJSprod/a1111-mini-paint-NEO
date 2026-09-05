"""Receive, crop, mask, expand, undo and send, checked on what the callbacks
return: the image and mask layer the canvas would show, and the values the
host's img2img / Inpaint inputs would receive.

The canvas is stood in for by what its two hidden textboxes deliver: the
image as PIL, and the scribble layer as an RGBA image whose alpha is the
mask.
"""

from harness import Results, setup_path, value_of

setup_path()

import gradio as gr  # noqa: E402
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
    r.check("every destination was found", set(canvas.targets) == {"img2img", "inpaint", "inpaint_mask", "extras"}, str(sorted(canvas.targets)))
    r.check("only Extras is written from the backend", canvas.image_targets == ["extras"], str(canvas.image_targets))
    r.check("the inpaint mask layer was found", canvas.targets["inpaint_mask"] is refs["init_img_with_mask"].foreground)

    # ---- the session rebuild Gradio does after an update output, under Forge's patches ----
    from minipaint_neo.canvas import surface as surface_module

    rebuilt = type(canvas.surface.background)(**canvas.surface.background.constructor_args)
    r.check("a rebuilt canvas textbox still reads images, not arrays", rebuilt.numpy is False and type(rebuilt) is surface_module.canvas_image_class())
    host_rebuilt = type(refs["init_img"].background)(**refs["init_img"].background.constructor_args)
    r.check("(the host's own textbox would not: that is why the backend never answers it)", host_rebuilt.numpy is True)

    # commit-shaped replies (see TouchCanvas._commit / _info / _mode_updates);
    # replies that start with the mask layer instead of the image are one shorter
    BG, FG, STATE, STATUS, PREVIEW, ASPECT, ORIGINAL, WAIT, MODE = range(9)
    # after the mode: 4 quick rows, 4 panels, 4 mode buttons, the send label, the tool, the options label,
    # then the layer menu, the visible-layers chips, the opacity, the name, the drag preview and underlay
    SEND_LABEL, TOOL, OPTIONS_LABEL = MODE + 13, MODE + 14, MODE + 15
    LAYER_PICK, LAYER_VISIBLE, LAYER_OPACITY, LAYER_NAME, LAYER_PREVIEW, LAYER_UNDERLAY = range(MODE + 16, MODE + 22)
    COMMIT_LEN = 2 + canvas.INFO_COUNT
    r.check("the commit shape is the two canvas values plus the information", COMMIT_LEN == 30 and canvas.MODE_COUNT == 22)

    # ---- receive from txt2img ----
    photo = Image.new("RGB", (640, 480), (20, 120, 220))
    out = canvas.receive([(photo, None)], None, "mask", "txt2img")
    doc = out[STATE]
    r.check("reply has the commit shape", len(out) == COMMIT_LEN)
    r.check("received image becomes the document", doc.size == (640, 480) and doc.origin == "txt2img")
    r.check("receive writes the image to the canvas", isinstance(out[BG], Image.Image) and out[BG].size == (640, 480))
    r.check("receive clears the mask layer", out[FG] is None and out[WAIT] == "")
    r.check("receive selects crop mode", out[MODE] == "crop")
    r.check("receive reports the original size", out[ORIGINAL] == "640x480")
    r.check("status says where it came from, on one line", "Received from txt2img" in out[STATUS] and "640 × 480" in out[STATUS] and "\n" not in out[STATUS])
    r.check("send label is plain img2img", value_of(out[SEND_LABEL]) == "Send to img2img")
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
    out = canvas.apply_crop(bg, None, doc, "crop", '{"x0": 100, "y0": 50, "x1": 420, "y1": 290}')
    doc = out[STATE]
    r.check("crop committed", doc.size == (320, 240) and "Cropped to 320 × 240" in out[STATUS])
    r.check("crop writes the cropped image", isinstance(out[BG], Image.Image) and out[BG].size == (320, 240))
    r.check("crop keeps the original", doc.original.size == (640, 480) and out[ORIGINAL] == "640x480")
    r.check("the previous size is one undo away", doc.history and doc.history[-1]["label"] == "crop")
    r.check("aspect is reset after a crop", value_of(out[ASPECT]) == "Free")
    r.check("expand preview shows the new size", "320 × 240" in out[PREVIEW])

    bg = imaging.to_rgba(doc.image)
    out = canvas.apply_crop(bg, None, doc, "crop", '{"x0": 0, "y0": 0, "x1": 320, "y1": 240}')
    r.check("a frame over the whole image is not a crop", "nothing to crop" in out[STATUS] and skipped(out[BG]))
    out = canvas.apply_crop(bg, None, doc, "crop", "")
    r.check("no frame is explained", "Put the frame" in out[STATUS] and skipped(out[BG]))
    r.check("a crop without an image is refused", "no image" in canvas.apply_crop(None, None, None, "crop", '{"x0":0,"y0":0,"x1":1,"y1":1}')[STATUS])

    # a crop with strokes on the canvas keeps the covered part of the mask
    stroke = Image.new("L", (320, 240), 0)
    stroke.paste(255, (60, 40, 120, 100))
    out = canvas.apply_crop(bg, layer(stroke, (320, 240)), doc, "crop", '{"x0": 40, "y0": 20, "x1": 240, "y1": 140}')
    doc = out[STATE]
    r.check("crop with a mask keeps the mask", doc.size == (200, 120) and doc.has_mask and doc.mask.getpixel((40, 40)) == 255 and doc.mask.getpixel((5, 5)) == 0)
    r.check("the mask layer waits for the image", skipped(out[FG]) and out[WAIT] == "wait")
    fg = canvas.commit_foreground(doc)
    r.check("the mask layer is written afterwards, at the new size", isinstance(fg, Image.Image) and fg.size == (200, 120) and fg.getchannel("A").getpixel((40, 40)) == 255)
    r.check("the mask layer uses the Inpaint tab's checkerboard", fg.getpixel((40, 40))[:3] in ((0, 0, 0), (255, 255, 255)))
    r.check("no mask means no layer step", skipped(canvas.commit_foreground(canvas.receive([(photo, None)], None, "crop", "txt2img")[STATE])))

    # ---- undo / redo of structural steps ----
    out = canvas.undo(doc, "crop")
    doc = out[STATE]
    r.check("undo restores the earlier crop, strokes included", doc.size == (320, 240) and "Undid crop" in out[STATUS] and doc.has_mask and out[WAIT] == "wait")
    out = canvas.redo(doc, "crop")
    doc = out[STATE]
    r.check("redo reapplies it, mask included", doc.size == (200, 120) and doc.has_mask and "Redid crop" in out[STATUS] and out[WAIT] == "wait")
    out = canvas.redo(doc, "crop")
    r.check("nothing to redo is a message, not a reload", "Nothing to redo" in out[STATUS] and skipped(out[BG]))

    # ---- mask mode ----
    updates = canvas.set_mode("mask", doc)
    r.check("mode updates have the mode shape", len(updates) == canvas.MODE_COUNT)
    r.check("mask mode relabels the send button and resets the tool", value_of(updates[13]) == "Send to img2img Inpaint" and value_of(updates[14]) == "Paint")
    r.check("mask mode shows its quick row and panel only", [u.get("visible") for u in updates[1:5]] == [False, True, False, False] and [u.get("visible") for u in updates[5:9]] == [False, True, False, False])
    r.check("mask mode relabels the options accordion", updates[15].get("label") == "Mask options")
    r.check("crop mode leaves the tool alone", skipped(canvas.set_mode("crop", doc)[14]))
    r.check("outside Layers mode the drag previews are not sent", skipped(updates[20]) and skipped(updates[21]))
    r.check("an unknown mode is crop", canvas.set_mode("nonsense", doc)[0] == "crop")

    # ---- undo and redo take a stroke first, when the browser took one back ----
    out = canvas.undo(doc, "mask", "stroke")
    r.check("a stroke undo touches nothing on the server", "Undid a stroke" in out[STATUS] and skipped(out[BG]) and out[STATE] is doc)
    r.check("a stroke redo likewise", "Redid a stroke" in canvas.redo(doc, "mask", "stroke")[STATUS])

    # ---- layers: the frame as a selection, a drag, order, visibility, merge, flatten ----
    bg = imaging.to_rgba(doc.image)
    fg = layer(doc.mask, doc.size)
    updates = canvas.set_mode("layers", doc)
    r.check("layers mode sends the drag previews", isinstance(updates[20], str) and updates[20].startswith('{"src": "data:image/png;base64,') and updates[21] == "")
    r.check("layers mode lists the one layer", updates[16].get("choices") == ["Background"] and updates[16].get("value") == "Background" and updates[17].get("value") == ["Background"])
    r.check("the previews are sent once until something changes", skipped(canvas.set_mode("layers", doc)[20]))

    out = canvas.new_layer_from_selection(bg, fg, doc, "layers", '{"x0": 20, "y0": 10, "x1": 120, "y1": 70}')
    doc = out[STATE]
    r.check("a selection becomes a layer above the active one", doc.layer_names() == ["Background", "Layer 2"] and doc.active == 1)
    r.check("it holds the pixels where they were", doc.active_layer.size == (100, 60) and (doc.active_layer.x, doc.active_layer.y) == (20, 10))
    r.check("the canvas is left alone: nothing changed on it", skipped(out[BG]) and skipped(out[FG]) and out[WAIT] == "")
    r.check("the status names the new layer", "Layer 2 holds the selection" in out[STATUS] and "2 layers" in out[STATUS])
    r.check("the layer menu and chips follow", out[LAYER_PICK].get("choices") == ["Background", "Layer 2"] and out[LAYER_PICK].get("value") == "Layer 2" and out[LAYER_VISIBLE].get("value") == ["Background", "Layer 2"])
    r.check("a new preview and an underlay go to the browser", isinstance(out[LAYER_PREVIEW], str) and '"x": 20' in out[LAYER_PREVIEW] and out[LAYER_UNDERLAY].startswith("data:image/png"))
    r.check("no frame is explained", "Put the frame" in canvas.new_layer_from_selection(bg, fg, doc, "layers", "")[STATUS])

    out = canvas.move_layer('{"dx": 15, "dy": -5, "t": 1}', bg, fg, doc, "layers")
    doc = out[STATE]
    r.check("a drag moves the active layer", (doc.active_layer.x, doc.active_layer.y) == (35, 5) and "at (35, 5)" in out[STATUS] and "Moved Layer 2" in out[STATUS])
    r.check("and reloads the composite", isinstance(out[BG], Image.Image) and out[BG].size == (200, 120))
    r.check("a drag of nothing changes nothing", skipped(canvas.move_layer('{"dx": 0, "dy": 0}', bg, fg, doc, "layers")[BG]))
    r.check("the composite shows the moved layer", doc.image.getpixel((5, 5))[3] == 255)

    out = canvas.pick_layer("Background", doc, "layers")
    doc = out[0]
    r.check("picking a layer makes it active without a reload", doc.active == 0 and "Background is the active layer" in out[1] and len(out) == canvas.INFO_COUNT)
    r.check("the preview follows the active layer", isinstance(out[LAYER_PREVIEW - 2], str) and '"name": "Background"' in out[LAYER_PREVIEW - 2])

    out = canvas.layer_order(1, bg, fg, doc, "layers")
    doc = out[STATE]
    r.check("move up reorders", doc.layer_names() == ["Layer 2", "Background"] and doc.active == 1)
    r.check("at the top it says so", "already at the top" in canvas.layer_order(1, bg, fg, doc, "layers")[STATUS])
    out = canvas.layer_order(-1, bg, fg, doc, "layers")
    doc = out[STATE]
    r.check("move down reorders back", doc.layer_names() == ["Background", "Layer 2"] and doc.active == 0)

    out = canvas.layer_visibility(["Layer 2"], bg, fg, doc, "layers")
    doc = out[STATE]
    r.check("hiding the background leaves the composite transparent there", not doc.layers[0].visible and doc.image.getpixel((150, 100))[3] == 0 and "Hidden: Background" in out[STATUS])
    out = canvas.layer_visibility(["Background", "Layer 2"], bg, fg, doc, "layers")
    doc = out[STATE]
    r.check("showing it again", all(l.visible for l in doc.layers))

    canvas.pick_layer("Layer 2", doc, "layers")
    out = canvas.layer_opacity(40, bg, fg, doc, "layers")
    doc = out[STATE]
    r.check("opacity applies to the active layer", doc.active_layer.opacity == 40 and "40% opacity" in out[STATUS] and out[LAYER_OPACITY].get("value") == 40)
    out = canvas.layer_rename("Cutout", bg, fg, doc, "layers")
    doc = out[STATE]
    r.check("rename without a reload", doc.active_layer.name == "Cutout" and skipped(out[BG]) and out[LAYER_PICK].get("value") == "Cutout")
    r.check("a blank name is refused", "Type a name" in canvas.layer_rename("  ", bg, fg, doc, "layers")[STATUS])
    out = canvas.layer_duplicate(bg, fg, doc, "layers")
    doc = out[STATE]
    r.check("duplicate copies the active layer above it", doc.layer_names() == ["Background", "Cutout", "Cutout copy"] and doc.active == 2)
    out = canvas.layer_delete(bg, fg, doc, "layers")
    doc = out[STATE]
    r.check("delete removes the active layer", doc.layer_names() == ["Background", "Cutout"] and "Cutout copy deleted" in out[STATUS])
    out = canvas.layer_merge(bg, fg, doc, "layers")
    doc = out[STATE]
    r.check("merge down leaves one layer, named after the lower", doc.layer_names() == ["Background"] and "Cutout merged into Background" in out[STATUS])
    r.check("with one layer there is nothing to merge or delete", "nothing below" in canvas.layer_merge(bg, fg, doc, "layers")[STATUS] and "last layer stays" in canvas.layer_delete(bg, fg, doc, "layers")[STATUS])
    out = canvas.undo(doc, "layers")
    doc = out[STATE]
    r.check("undo restores the two layers", "Undid merge down" in out[STATUS] and doc.layer_names() == ["Background", "Cutout"] and doc.active_layer.opacity == 40)
    out = canvas.layer_flatten(bg, fg, doc, "layers")
    doc = out[STATE]
    r.check("flatten leaves one layer", doc.layer_names() == ["Background"] and "2 layers flattened" in out[STATUS])
    canvas.undo(doc, "layers")

    # the mask as a freehand selection
    out = canvas.mask_to_layer(bg, layer(stroke.resize((200, 120)), (200, 120)), doc, "mask")
    doc = out[STATE]
    r.check("a masked area becomes a layer, trimmed to it, and the mode switches to Layers",
            doc.active_layer.name == "Layer 3" and doc.active_layer.size[0] < 200 and out[MODE] == "layers" and skipped(out[BG]))
    r.check("without a mask it says so", "Paint over the area" in canvas.mask_to_layer(bg, None, canvas.receive([(photo, None)], None, "mask", "txt2img")[STATE], "mask")[STATUS])
    canvas.undo(doc, "layers")
    canvas.pick_layer("Background", doc, "layers")
    out = canvas.set_mode("mask", doc)

    # ---- send: auto goes to Inpaint, image first, mask once the canvas has it ----
    bg = imaging.to_rgba(doc.image)
    fg = layer(doc.mask, doc.size)
    # a send reply: Extras, then the information (document first), then the instruction and the image payload
    T = len(canvas.image_targets)
    SENT_DOC, SENT_STATUS = T, T + 1
    out = canvas.send(bg, fg, doc, "mask", "Auto", "Off")
    doc = out[SENT_DOC]
    instruction, payload = out[-2], out[-1]
    r.check("auto with a mask targets inpaint, naming the size its canvas must reach", instruction == "inpaint:200x120")
    r.check("extras is skipped", skipped(out[0]))
    r.check("the image travels as a PNG data URL for the browser to write", payload.startswith("data:image/png;base64,") and decode_data_url(payload).size == (200, 120))
    r.check("status says sent", "Sent to img2img Inpaint" in out[SENT_STATUS])
    r.check("the layers are flattened for the destination", "2 layers were flattened" in out[SENT_STATUS])
    mask_out = canvas.send_mask(doc, instruction)
    r.check("inpaint then receives the mask as a data URL, alpha where the stroke was",
            mask_out.startswith("data:image/png;base64,") and (lambda m: m.size == (200, 120) and m.getchannel("A").getpixel((40, 40)) == 255 and m.getchannel("A").getpixel((5, 5)) == 0)(decode_data_url(mask_out)))
    r.check("the mask step is one-shot", canvas.send_mask(doc, instruction) == "")
    r.check("the document keeps the mask", doc.has_mask)

    # smoothing is applied on the way out only
    out = canvas.send(bg, fg, doc, "mask", "Auto", "Medium")
    r.check("smoothing note appears", "smoothing: Medium" in out[SENT_STATUS])
    smoothed = decode_data_url(canvas.send_mask(doc, out[-2]))
    r.check("the smoothed mask still covers the stroke", smoothed.getchannel("A").getpixel((40, 40)) == 255 and doc.mask.getpixel((40, 40)) == 255)

    # explicit img2img drops the mask, and says so
    out = canvas.send(bg, fg, doc, "mask", "img2img", "Off")
    r.check("explicit img2img sends the image as a payload for the browser", out[-1].startswith("data:image/png;base64,") and skipped(out[0]))
    r.check("dropping the mask is mentioned", "mask was not sent" in out[SENT_STATUS])
    r.check("switch goes to img2img", out[-2] == "img2img")
    r.check("no mask for img2img", canvas.send_mask(doc, "img2img") == "")

    # extras is the one destination written from the backend
    out = canvas.send(bg, fg, doc, "mask", "Extras", "Off")
    r.check("extras receives the image itself, with no payload", isinstance(out[0], Image.Image) and out[0].size == (200, 120) and out[-1] == "" and out[-2] == "extras")

    # inpaint without a mask clears the layer there
    plain = canvas.receive([(photo, None)], None, "crop", "txt2img")[STATE]
    out = canvas.send(imaging.to_rgba(plain.image), None, plain, "crop", "Inpaint", "Off")
    r.check("inpaint without a mask still goes to inpaint", out[-2] == "inpaint:640x480")
    r.check("and clears the layer there", canvas.send_mask(plain, out[-2]) == "")
    r.check("send without an image says so", "no image to send" in canvas.send(None, None, None, "crop", "Auto", "Off")[SENT_STATUS].lower())

    # ---- expand: new area auto-masked, transparent pixels filled on send ----
    out = canvas.apply_expand(bg, fg, doc, "crop", 0, 128, 0, 64, 16, "Transparent", "8")
    doc = out[STATE]
    r.check("expanded size", doc.size == (328, 184) and doc.has_expansion, str(doc.size))
    r.check("expand switches to mask mode", out[MODE] == "mask" and value_of(out[TOOL]) == "Paint")
    r.check("the new area is masked", doc.mask.getpixel((300, 90)) == 255 and doc.mask.getpixel((100, 100)) == 0)
    r.check("the old stroke is carried", doc.mask.getpixel((40, 40)) == 255)
    r.check("send label says outpaint", value_of(out[SEND_LABEL]) == "Send Outpaint to img2img")
    r.check("expand writes the image and waits for it before the mask", isinstance(out[BG], Image.Image) and out[BG].size == (328, 184) and out[WAIT] == "wait")
    r.check("the overlap is explained", "16px back" in out[STATUS])
    r.check("a mask layer follows at the new size", canvas.commit_foreground(doc).size == (328, 184))

    bg = imaging.to_rgba(doc.image)
    out = canvas.send(bg, layer(doc.mask, doc.size), doc, "mask", "Auto", "Off")
    sent = decode_data_url(out[-1])
    r.check("sent expansion has no transparency", sent.getchannel("A").getextrema() == (255, 255))
    r.check("fill note appears", "transparent pixels were filled" in out[SENT_STATUS])
    r.check("mask matches the image size", decode_data_url(canvas.send_mask(doc, out[-2])).size == sent.size)
    out = canvas.apply_expand(bg, layer(doc.mask, doc.size), doc, "mask", 0, 0, 0, 0, 0, "Transparent", "8")
    r.check("expanding nothing is refused without touching the canvas", skipped(out[BG]) and skipped(out[FG]) and out[STATE].has_mask)
    # the canvas is the truth: strokes that are not on it are not in the document either
    out = canvas.apply_expand(bg, None, doc, "mask", 0, 0, 0, 0, 0, "Transparent", "8")
    r.check("a canvas without strokes means no mask", not out[STATE].has_mask)
    doc = out[STATE]
    doc.commit(doc.image, doc.mask if doc.has_mask else None)
    out = canvas.invert_mask(bg, None, doc, "mask")  # everything
    out = canvas.invert_mask(bg, out[0], out[STATE - 1], "mask")  # back to nothing
    r.check("invert twice is nothing, and clears the layer", out[0] is None and not out[STATE - 1].has_mask)
    doc = out[STATE - 1]
    doc.commit(doc.image, stroke.resize((328, 184)))

    # ---- clear and invert ----
    out = canvas.clear_mask(bg, layer(doc.mask, doc.size), doc, "mask")
    doc = out[STATE - 1]
    r.check("mask cleared, image intact", not doc.has_mask and doc.size == (328, 184) and out[0] is None)
    out = canvas.clear_mask(bg, None, doc, "mask")
    r.check("clearing nothing is a message", "no mask to clear" in out[STATUS - 1] and skipped(out[0]))
    out = canvas.invert_mask(bg, None, doc, "mask")
    doc = out[STATE - 1]
    r.check("invert of nothing masks everything", doc.has_mask and doc.mask.getextrema() == (255, 255) and isinstance(out[0], Image.Image))
    out = canvas.invert_mask(bg, layer(stroke.resize((328, 184)), (328, 184)), doc, "mask")
    doc = out[STATE - 1]
    r.check("strokes on the canvas are read before inverting", doc.mask.getpixel((5, 5)) == 255 and doc.mask.getpixel((80, 60)) == 0)
    r.check("undo restores the mask before the invert", canvas.undo(doc, "mask")[STATE].mask.getpixel((80, 60)) == 255)

    # ---- reset to original ----
    out = canvas.reset(doc, "mask")
    doc = out[STATE]
    r.check("reset restores the received image", doc.size == (640, 480) and not doc.has_mask and not doc.has_expansion)
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
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
        Image.new("RGB", (90, 60), (1, 2, 3)).save(handle.name)
    out = canvas.open_file(handle.name, doc, "mask")
    doc = out[STATE]
    r.check("open loads the file", doc.size == (90, 60) and doc.filename and "Opened." in out[STATUS] and out[MODE] == "crop")
    r.check("open of a bad file is a message", "Could not open" in canvas.open_file(__file__, doc, "crop")[STATUS])
    r.check("open of nothing is a message", "No file" in canvas.open_file(None, doc, "crop")[STATUS])

    # ---- save a copy ----
    saved = canvas.save_copy(imaging.to_rgba(doc.image), None, doc)
    r.check("save writes a png", saved.get("visible") is True and Image.open(saved["value"]).size == (90, 60))

    return r


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
