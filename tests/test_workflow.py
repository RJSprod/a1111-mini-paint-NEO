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
    r.check("image destinations were found", canvas.image_targets == ["img2img", "inpaint", "extras"], str(canvas.image_targets))
    r.check("the inpaint mask layer was found", canvas.targets["inpaint_mask"] is refs["init_img_with_mask"].foreground)

    # commit-shaped replies (see TouchCanvas._commit / _info / _mode_updates);
    # replies that start with the mask layer instead of the image are one shorter
    BG, FG, STATE, STATUS, PREVIEW, ASPECT, ORIGINAL, WAIT, MODE = range(9)
    SEND_LABEL, TOOL = 15, 16
    COMMIT_LEN = 17

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
    r.check("status says where it came from", "Received from txt2img" in out[STATUS] and "640 × 480" in out[STATUS])
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
    r.check("mask mode relabels the send button and resets the tool", value_of(updates[7]) == "Send to img2img Inpaint" and value_of(updates[8]) == "Paint")
    r.check("crop mode leaves the tool alone", skipped(canvas.set_mode("crop", doc)[8]))
    r.check("an unknown mode is crop", canvas.set_mode("nonsense", doc)[0] == "crop")

    # ---- send: auto goes to Inpaint, image first, mask once the canvas has it ----
    bg = imaging.to_rgba(doc.image)
    fg = layer(doc.mask, doc.size)
    # a send reply: the image targets, then the information (document first), then the instruction
    T = len(canvas.image_targets)
    SENT_DOC, SENT_STATUS = T, T + 1
    out = canvas.send(bg, fg, doc, "mask", "Auto", "Off")
    targets = dict(zip(canvas.image_targets, out[:T]))
    doc = out[SENT_DOC]
    r.check("auto with a mask targets inpaint", out[-1] == "inpaint:200x120")
    r.check("img2img and extras are skipped", skipped(targets["img2img"]) and skipped(targets["extras"]))
    r.check("inpaint receives the image", isinstance(targets["inpaint"], Image.Image) and targets["inpaint"].size == (200, 120))
    r.check("status says sent", "Sent to img2img Inpaint" in out[SENT_STATUS])
    mask_out = canvas.send_mask(doc, out[-1])
    r.check("inpaint then receives the mask as alpha", isinstance(mask_out, Image.Image) and mask_out.size == (200, 120)
            and mask_out.getchannel("A").getpixel((40, 40)) == 255 and mask_out.getchannel("A").getpixel((5, 5)) == 0)
    r.check("the mask step is one-shot", skipped(canvas.send_mask(doc, out[-1])))
    r.check("the document keeps the mask", doc.has_mask)

    # smoothing is applied on the way out only
    out = canvas.send(bg, fg, doc, "mask", "Auto", "Medium")
    r.check("smoothing note appears", "smoothing: Medium" in out[SENT_STATUS])
    smoothed = canvas.send_mask(doc, out[-1])
    r.check("the smoothed mask still covers the stroke", smoothed.getchannel("A").getpixel((40, 40)) == 255 and doc.mask.getpixel((40, 40)) == 255)

    # explicit img2img drops the mask, and says so
    out = canvas.send(bg, fg, doc, "mask", "img2img", "Off")
    targets = dict(zip(canvas.image_targets, out[: len(canvas.image_targets)]))
    r.check("explicit img2img sends the image only", isinstance(targets["img2img"], Image.Image) and skipped(targets["inpaint"]))
    r.check("dropping the mask is mentioned", "mask was not sent" in out[SENT_STATUS])
    r.check("switch goes to img2img", out[-1] == "img2img")
    r.check("no mask step for img2img", skipped(canvas.send_mask(doc, "img2img")))

    # inpaint without a mask clears the layer there
    plain = canvas.receive([(photo, None)], None, "crop", "txt2img")[STATE]
    out = canvas.send(imaging.to_rgba(plain.image), None, plain, "crop", "Inpaint", "Off")
    r.check("inpaint without a mask still goes to inpaint", out[-1] == "inpaint:640x480")
    r.check("and clears the layer there", canvas.send_mask(plain, out[-1]) is None)
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
    targets = dict(zip(canvas.image_targets, out[: len(canvas.image_targets)]))
    sent = targets["inpaint"]
    r.check("sent expansion has no transparency", sent.getchannel("A").getextrema() == (255, 255))
    r.check("fill note appears", "transparent pixels were filled" in out[SENT_STATUS])
    r.check("mask matches the image size", canvas.send_mask(doc, out[-1]).size == sent.size)
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
