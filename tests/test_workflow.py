"""Receive, crop, mask, expand, undo and send, checked on what the callbacks
return: the editor value the browser would show and the values the host's
img2img / Inpaint inputs would receive.

The editor itself is stood in for by the dictionaries it produces - a
background, a list of layers, a composite - already cropped the way the
component crops on export.
"""

from harness import Results, pixels, setup_path, value_of

setup_path()

import gradio as gr  # noqa: E402
from modules import script_callbacks  # noqa: E402
from PIL import Image  # noqa: E402

import forge_like  # noqa: E402
from minipaint_neo import router  # noqa: E402
from minipaint_neo.canvas import host, imaging  # noqa: E402
from minipaint_neo.canvas import ui as canvas_ui  # noqa: E402


def editor_dict(image, mask=None, color=(255, 47, 47)):
    """What the editor hands back for an image with an optional drawn mask."""
    return imaging.editor_value(image, mask, color)


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
    r.check("destinations were found", set(canvas.target_order) == {"img2img", "inpaint", "inpaint_mask", "extras"}, str(canvas.target_order))

    # COMMIT output positions (see TouchCanvas._reply)
    EDITOR, PLACEHOLDER, STATE, STATUS, PREVIEW, ASPECT, MODE = range(7)
    SEND_LABEL = 13
    sides = (0, 0, 0, 0)

    # ---- receive from txt2img ----
    photo = Image.new("RGB", (640, 480), (20, 120, 220))
    out = canvas.receive([(photo, None)], None, "mask", "txt2img")
    doc = out[STATE]
    r.check("received image becomes the document", doc.size == (640, 480) and doc.origin == "txt2img")
    r.check("receive pushes the editor", isinstance(out[EDITOR], dict) and out[EDITOR].get("visible") is True)
    r.check("receive resets the crop box", out[EDITOR].get("crop_size") == "")
    r.check("receive hides the placeholder", out[PLACEHOLDER].get("visible") is False)
    r.check("receive selects crop mode", out[MODE] == "crop")
    r.check("status says where it came from", "Received from txt2img" in out[STATUS] and "640 × 480" in out[STATUS])
    r.check("send label is plain img2img", value_of(out[SEND_LABEL]) == "Send to img2img")
    r.check("nothing to receive is reported", "Pick an image" in canvas.receive(None, doc, "crop", "txt2img")[STATUS])

    # ---- apply crop: the editor exports a cropped background ----
    cropped_bg = imaging.to_rgba(photo).crop((100, 50, 420, 290))
    staged = canvas.stage_crop(editor_dict(cropped_bg), doc)
    r.check("crop is staged and asks for a flush", staged[2] == "flush" and staged[0].pending is not None)
    out = canvas.commit(staged[0], "crop", *sides, "8")
    doc = out[STATE]
    r.check("crop committed", doc.size == (320, 240) and "Cropped to 320 × 240" in out[STATUS])
    r.check("the previous size is one undo away", doc.history and doc.history[-1]["label"] == "crop")
    r.check("commit pushes the cropped image", out[EDITOR]["value"]["background"].size == (320, 240))
    r.check("aspect is reset after a crop", value_of(out[ASPECT]) == "Free")
    r.check("expand preview shows the new size", "320 × 240" in out[PREVIEW])

    unchanged = canvas.stage_crop(editor_dict(imaging.to_rgba(doc.image)), doc)
    r.check("an uncropped editor is not committed", unchanged[2] == "" and "Nothing to crop" in unchanged[1])

    # a crop padded with transparency by the component is trimmed
    padded = Image.new("RGBA", (320, 240), (0, 0, 0, 0))
    padded.paste(cropped_bg.crop((0, 0, 200, 120)), (10, 20))
    staged = canvas.stage_crop(editor_dict(padded), doc)
    out = canvas.commit(staged[0], "crop", *sides, "8")
    doc = out[STATE]
    r.check("padding is trimmed off a crop", doc.size == (200, 120), str(doc.size))

    # ---- undo / redo of structural steps ----
    staged = canvas.stage_undo(doc)
    out = canvas.commit(staged[0], "crop", *sides, "8")
    doc = out[STATE]
    r.check("undo restores the earlier crop", doc.size == (320, 240) and "Undid crop" in out[STATUS])
    staged = canvas.stage_redo(doc)
    out = canvas.commit(staged[0], "crop", *sides, "8")
    doc = out[STATE]
    r.check("redo reapplies it", doc.size == (200, 120) and "Redid crop" in out[STATUS])
    r.check("nothing to redo is a message, not a flush", canvas.stage_redo(doc)[2] == "")

    # ---- mask: strokes come back as a layer's alpha ----
    stroke = Image.new("L", (200, 120), 0)
    stroke.paste(255, (40, 30, 90, 80))
    with_mask = editor_dict(imaging.to_rgba(doc.image), stroke)
    label = canvas.set_mode("mask", doc)[7]
    r.check("mask mode relabels the send button", value_of(label) == "Send to img2img Inpaint")

    # ---- send: auto goes to Inpaint with the mask as the foreground alpha ----
    out = canvas.send(with_mask, doc, "mask", "Auto", "Off")
    targets = dict(zip(canvas.target_order, out[: len(canvas.target_order)]))
    reply = out[len(canvas.target_order) :]
    doc = reply[STATE]
    r.check("auto with a mask targets inpaint", reply[-1] == "inpaint")
    r.check("img2img is skipped", isinstance(targets["img2img"], dict) and "value" not in targets["img2img"])
    r.check("inpaint receives the image", isinstance(targets["inpaint"], Image.Image) and targets["inpaint"].size == (200, 120))
    fg = targets["inpaint_mask"]
    r.check("inpaint receives the mask as alpha", isinstance(fg, Image.Image) and fg.getchannel("A").getpixel((60, 50)) == 255 and fg.getchannel("A").getpixel((5, 5)) == 0)
    r.check("status says sent", "Sent to img2img Inpaint" in reply[STATUS])
    r.check("the document keeps the mask", doc.has_mask)

    # smoothing is applied on the way out only
    out = canvas.send(with_mask, doc, "mask", "Auto", "Medium")
    r.check("smoothing note appears", "smoothing: Medium" in out[len(canvas.target_order) + STATUS])

    # explicit img2img drops the mask, and says so
    out = canvas.send(with_mask, doc, "mask", "img2img", "Off")
    targets = dict(zip(canvas.target_order, out[: len(canvas.target_order)]))
    r.check("explicit img2img sends the image only", isinstance(targets["img2img"], Image.Image) and "value" not in targets["inpaint"])
    r.check("dropping the mask is mentioned", "mask was not sent" in out[len(canvas.target_order) + STATUS])
    r.check("switch goes to img2img", out[-1] == "img2img")

    # ---- expand: new area auto-masked, transparent pixels filled on send ----
    staged = canvas.stage_expand(editor_dict(imaging.to_rgba(doc.image), stroke), doc, 0, 128, 0, 64, 16, "Transparent", "8")
    r.check("expand staged", staged[2] == "flush")
    out = canvas.commit(staged[0], "crop", 0, 128, 0, 64, "8")
    doc = out[STATE]
    r.check("expanded size", doc.size == (328, 184) and doc.has_expansion, str(doc.size))
    r.check("expand switches to mask mode", out[MODE] == "mask")
    r.check("the new area is masked", doc.mask.getpixel((300, 90)) == 255 and doc.mask.getpixel((100, 100)) == 0)
    r.check("the old stroke is carried", doc.mask.getpixel((60, 50)) == 255)
    r.check("send label says outpaint", value_of(out[SEND_LABEL]) == "Send Outpaint to img2img")
    r.check("editor shows the expansion with its mask layer", len(out[EDITOR]["value"]["layers"]) == 1 and out[EDITOR]["value"]["background"].size == (328, 184))

    out = canvas.send(editor_dict(doc.image, doc.mask), doc, "mask", "Auto", "Off")
    targets = dict(zip(canvas.target_order, out[: len(canvas.target_order)]))
    sent = targets["inpaint"]
    r.check("sent expansion has no transparency", sent.getchannel("A").getextrema() == (255, 255))
    r.check("fill note appears", "transparent pixels were filled" in out[len(canvas.target_order) + STATUS])
    r.check("mask matches the image size", targets["inpaint_mask"].size == sent.size)

    # ---- clear and invert ----
    staged = canvas.stage_clear_mask(editor_dict(doc.image, doc.mask), doc)
    out = canvas.commit(staged[0], "mask", *sides, "8")
    doc = out[STATE]
    r.check("mask cleared, image intact", not doc.has_mask and doc.size == (328, 184))
    r.check("clearing nothing is a message", canvas.stage_clear_mask(editor_dict(doc.image), doc)[2] == "")
    staged = canvas.stage_invert_mask(editor_dict(doc.image), doc)
    out = canvas.commit(staged[0], "mask", *sides, "8")
    doc = out[STATE]
    r.check("invert of nothing masks everything", doc.has_mask and doc.mask.getextrema() == (255, 255))

    # ---- reset to original ----
    staged = canvas.stage_reset(doc)
    out = canvas.commit(staged[0], "mask", *sides, "8")
    doc = out[STATE]
    r.check("reset restores the received image", doc.size == (640, 480) and not doc.has_mask and not doc.has_expansion)
    r.check("reset goes back to crop mode", out[MODE] == "crop")

    # ---- expand helpers ----
    values = canvas.add_side("Right", "64", doc, 0, 0, 0, 0, "8")
    r.check("a side button adds the amount", values[1] == 64 and "704 × 480" in values[4])
    values = canvas.add_side("Right", "100", doc, 0, 64, 0, 0, "32")
    r.check("amounts snap", values[1] == 160)
    r.check("clear sides", canvas.clear_sides(doc)[:4] == (0, 0, 0, 0))

    # ---- the aspect picker drives the editor's crop constraint ----
    r.check("aspect update targets crop_size", canvas.set_aspect("16:9", 0, 0, doc).get("crop_size") == "16:9")
    r.check("original aspect uses the original", canvas.set_aspect("Original", 0, 0, doc).get("crop_size") == "640:480")
    r.check("free aspect clears it", canvas.set_aspect("Free", 0, 0, doc).get("crop_size") == "")

    # ---- an editor that is not a dict (the no-WebGL notice) never crashes ----
    r.check("a non-dict editor value is handled", "no image" in canvas.stage_crop("<div>notice</div>", doc)[1].lower())
    out = canvas.send("<div>notice</div>", doc, "crop", "Auto", "Off")
    r.check("send with no editor says so", "no image to send" in out[len(canvas.target_order) + STATUS].lower())

    return r


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
