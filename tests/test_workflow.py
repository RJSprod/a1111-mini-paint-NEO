"""The Canvas callbacks, driven the way Gradio drives them.

Receive, open, crop, mask, expand, undo and send, each checked on what the
user would see: the value handed back to the editor, the status line, and the
files a handoff stages for the browser.
"""

import json
import pathlib
import tempfile

from harness import Results, setup_path, value_of as text

setup_path()

from PIL import Image  # noqa: E402

from forge_canvas_ext import settings  # noqa: E402
from forge_canvas_ext.paths import TMP_DIR  # noqa: E402
from forge_canvas_ext.touch import document, imaging  # noqa: E402
from forge_canvas_ext.touch import ui as touch_ui  # noqa: E402


def photo(width=400, height=300):
    image = Image.new("RGB", (width, height))
    image.putdata(
        [((x * 7) % 256, (y * 5) % 256, 90) for y in range(height) for x in range(width)]
    )
    return image


def run() -> Results:
    results = Results("workflow")
    settings.on_ui_settings()
    canvas = touch_ui.TouchCanvas()
    scratch = pathlib.Path(tempfile.mkdtemp(prefix="forge-canvas-test-"))

# ---- receive an image from the WebUI ----

    ed, state, status, send = canvas.receive(photo(), None)
    results.check("receive produces an editor value", set(ed) == {"background", "layers", "composite"})
    results.check("receive sizes the background", ed["background"].size == (400, 300))
    results.check("receive has no mask", ed["layers"] == [])
    results.check("receive keeps the state", isinstance(state, document.Document))
    results.check("status names the size", "400 x 300" in text(status), text(status))
    results.check("send button says img2img", text(send) == "Send to img2img", text(send))
    results.check("preview quotes the size", "400 x 300" in canvas.expand_preview(state, 0, 0, 0, 0, "8"))

    # ---- open a local file ----
    tmp = scratch / "opened.png"
    photo(120, 90).save(tmp)
    ed, state, status, send = canvas.open_file(str(tmp), state)
    results.check("open loads the file", ed["background"].size == (120, 90))
    results.check("open names the file", "opened.png" in text(status), text(status))
    results.check("open warns the old image is undoable", "Undo" in text(status))

    bad = scratch / "notanimage.png"
    bad.write_text("nope")
    _, state, status, _ = canvas.open_file(str(bad), state)
    results.check("a bad file is explained, not raised", "Could not open" in text(status), text(status))
    results.check("a bad file leaves the document alone", state.size == (120, 90))

    # ---- crop ----
    ed, state, status, send = canvas.receive(photo(400, 300), state)
    value = imaging.editor_value(state.image, None)
    _, state, status, _ = canvas.apply_crop(value, state, "Free", 1024, 1024)
    results.check("free crop with no drag says so", "Nothing to crop" in text(status), text(status))
    results.check("free crop with no drag does not resize", state.size == (400, 300))

    _, state, status, send = canvas.apply_crop(value, state, "1:1", 1024, 1024)
    results.check("1:1 crop", state.size == (300, 300), str(state.size))
    results.check("crop reports the new size", "300 x 300" in text(status), text(status))

    # a crop the component padded with transparency is trimmed back
    padded = Image.new("RGBA", (500, 400), (0, 0, 0, 0))
    padded.paste(imaging.to_rgba(photo(300, 300)), (100, 50))
    _, state, status, _ = canvas.apply_crop(
        {"background": padded, "layers": [], "composite": padded}, state, "Free", 0, 0)
    results.check("padding from the component is trimmed", state.size == (300, 300), str(state.size))
    results.check("the trim is reported", "trimmed" in text(status), text(status))

    _, state, status, _ = canvas.apply_crop(
        imaging.editor_value(state.image, None), state, "Custom", 100, 50)
    results.check("custom crop", state.size == (100, 50), str(state.size))
    _, state, status, _ = canvas.apply_crop(
        imaging.editor_value(state.image, None), state, "Custom", 4000, 4000)
    results.check("custom crop clamps", state.size == (100, 50))
    results.check("a crop that would do nothing says so", "nothing to crop" in text(status), text(status))
    _, state, status, _ = canvas.apply_crop(
        imaging.editor_value(state.image, None), state, "Custom", 40, 4000)
    results.check("custom crop clamps one side", state.size == (40, 50), str(state.size))
    results.check("clamping is reported", "clamped" in text(status), text(status))

    # ---- undo across structural steps ----
    _, state, status, _ = canvas.undo(state)
    results.check("undo after crop", state.size == (100, 50), str(state.size))
    _, state, status, _ = canvas.undo(state)
    results.check("undo again", state.size == (300, 300), str(state.size))
    _, state, status, _ = canvas.redo(state)
    results.check("redo", state.size == (100, 50), str(state.size))
    _, state, status, _ = canvas.reset(state)
    results.check("reset goes back to the received image", state.size == (400, 300), str(state.size))

    # ---- mask ----
    ed, state, status, send = canvas.receive(photo(256, 256), state)
    mask = Image.new("L", (256, 256), 0)
    mask.paste(255, (40, 40, 120, 120))
    masked_value = imaging.editor_value(state.image, mask, canvas.mask_colour)

    _, state, status, send = canvas.invert_mask(masked_value, state)
    results.check("invert produces a mask", state.has_mask)
    results.check("invert flips the painted area", state.mask.getpixel((60, 60)) == 0)
    results.check("invert fills the rest", state.mask.getpixel((200, 200)) == 255)
    results.check("send button follows the mask", text(send) == "Send to img2img Inpaint", text(send))

    _, state, status, send = canvas.clear_mask(imaging.editor_value(state.image, state.mask), state)
    results.check("clear removes the mask", not state.has_mask)
    results.check("clear keeps the image", state.size == (256, 256))
    results.check("clear says it left the image alone", "image is untouched" in text(status), text(status))
    results.check("send button goes back", text(send) == "Send to img2img")
    _, state, _, _ = canvas.undo(state)
    results.check("undo brings the mask back", state.has_mask)

    # ---- expand ----
    _, state, status, send = canvas.receive(photo(256, 256), state)
    results.check("preview with no sides", "pick a side" in canvas.expand_preview(state, 0, 0, 0, 0, "8"))
    results.check("preview quotes the result",
          "384 x 256" in canvas.expand_preview(state, 128, 0, 0, 0, "8"),
          canvas.expand_preview(state, 128, 0, 0, 0, "8"))
    results.check("preview snaps to the nearest multiple",
          "384 x 256" in canvas.expand_preview(state, 100, 0, 0, 0, "64"),
          canvas.expand_preview(state, 100, 0, 0, 0, "64"))
    results.check("a small amount snaps up rather than to nothing",
          "320 x 256" in canvas.expand_preview(state, 10, 0, 0, 0, "64"),
          canvas.expand_preview(state, 10, 0, 0, 0, "64"))
    results.check("one tap adds to a side", canvas.add_side("Left", "128", 0, 0, 0, 0, "8") == (128, 0, 0, 0))
    results.check("two taps add twice", canvas.add_side("Left", "128", 128, 0, 0, 0, "8") == (256, 0, 0, 0))
    results.check("clear sides", canvas.clear_sides() == (0, 0, 0, 0))

    ed, state, status, send = canvas.apply_expand(
        imaging.editor_value(state.image, None), state, 128, 0, 64, 0, 32, "Transparent", "8")
    results.check("expand resizes", state.size == (384, 320), str(state.size))
    results.check("expand auto-masks", state.has_mask and state.mask.getpixel((10, 200)) == 255)
    results.check("expand leaves the middle alone", state.mask.getpixel((300, 250)) == 0)
    results.check("expand marks the document", state.has_expansion)
    results.check("expand explains the overlap", "blend" in text(status), text(status))
    results.check("send button follows the outpaint", text(send) == "Send Outpaint to img2img", text(send))
    results.check("the editor gets the mask layer", len(ed["layers"]) == 1)
    results.check("expand refuses nothing to do",
          "every side is 0" in text(canvas.apply_expand(
              imaging.editor_value(state.image, None), state, 0, 0, 0, 0, 0, "Transparent", "8")[2]))
    before = state.size
    canvas.apply_expand(imaging.editor_value(state.image, None), state, 90000, 0, 0, 0, 0, "Transparent", "Off")
    results.check("a refused expansion changes nothing", state.size == before)

    # ---- send ----
    payload_json, ed, state, status, send = canvas.prepare_send(
        imaging.editor_value(state.image, state.mask, canvas.mask_colour),
        state, "Auto", "Medium", 0, "img2img")
    payload = json.loads(payload_json)
    results.check("auto picks Inpaint for an outpaint", payload["destination"] == "img2img_inpaint", payload["destination"])
    results.check("payload carries a mask", payload["mask"] is not None)
    results.check("payload selector", payload["selector"] == "#img2maskimg")
    results.check("payload switches to inpaint", payload["switch"] == "inpaint")
    results.check("status says it is sending", "Sending to" in text(status), text(status))
    results.check("smoothing is reported", "Medium" in text(status), text(status))
    results.check("transparent fill is reported", "filled" in text(status), text(status))
    results.check("the editor is left alone while sending", ed == {"__type__": "update"}, str(ed)[:80])

    staged = Image.open(pathlib.Path(payload["image"][len("/file="):].split("?")[0]))
    staged_mask = Image.open(pathlib.Path(payload["mask"][len("/file="):].split("?")[0]))
    results.check("staged image is the document size", staged.size == state.size)
    results.check("staged mask matches it exactly", staged_mask.size == staged.size)
    results.check("staged image has no holes left", staged.getpixel((5, 5))[3] == 255)
    results.check("staged mask is alpha coverage", staged_mask.getchannel("A").getpixel((5, 5)) == 255)

    empty_payload, *_rest = canvas.prepare_send(None, document.Document(), "Auto", "Off", 0, "img2img")
    results.check("nothing to send is refused", empty_payload == "")
    results.check("nothing to send says why", "no image to send" in text(_rest[2]), text(_rest[2]))

    # ---- what the browser reports back ----
    results.check("a success is rendered",
          "Sent to" in text(canvas.transfer_result(
              json.dumps({"ok": True, "label": "img2img Inpaint", "detail": "384x320"}), state)))
    results.check("a failure is rendered",
          "Transfer failed" in text(canvas.transfer_result(
              json.dumps({"ok": False, "message": "nope"}), state)))
    results.check("a failure keeps the document",
          "untouched" in text(canvas.transfer_result(json.dumps({"ok": False}), state)))
    results.check("junk from the browser is ignored",
          canvas.transfer_result("", state) == {"__type__": "update"})

    # ---- save a copy ----
    saved = canvas.download(imaging.editor_value(state.image, state.mask), state)
    results.check("save writes a file", pathlib.Path(saved["value"]).exists())

    # ---- the staging folder does not grow without bound ----
    for i in range(40):
        canvas.prepare_send(imaging.editor_value(photo(64, 64), None), state, "img2img", "Off", 0, "img2img")
    staged_files = list(TMP_DIR.glob("send-*"))
    results.check("staged files are pruned", len(staged_files) <= 30, f"{len(staged_files)} left")

    return results


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
