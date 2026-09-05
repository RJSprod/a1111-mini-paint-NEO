"""Crop, mask, expansion and handoff maths - no Gradio, no WebUI.

These are the parts that decide what pixels get sent, so they are worth
checking without a browser in the way.
"""

from harness import Results, pixels, setup_path

setup_path()

from PIL import Image  # noqa: E402

from forge_canvas_ext.touch import bridge, document, imaging, outpaint  # noqa: E402


def run() -> Results:
    results = Results("image maths")

    results.check("parse #ff2f2f", imaging.parse_colour("#ff2f2f") == (255, 47, 47))
    results.check("parse f00", imaging.parse_colour("f00") == (255, 0, 0))
    results.check("parse rubbish", imaging.parse_colour("nope") == imaging.DEFAULT_MASK_COLOUR)
    results.check("parse rgba hex", imaging.parse_colour("#00ff00ff") == (0, 255, 0))

    # ---------- editor round trip ----------
    img = Image.new("RGB", (200, 100), (10, 120, 200))
    mask = Image.new("L", (200, 100), 0)
    mask.paste(255, (10, 10, 60, 60))

    value = imaging.editor_value(img, mask, (255, 47, 47))
    results.check("editor value keys", set(value) == {"background", "layers", "composite"})
    results.check("one layer", len(value["layers"]) == 1)
    results.check("layer size", value["layers"][0].size == (200, 100))
    results.check("layer alpha is the mask", pixels(value["layers"][0].getchannel("A")) == pixels(mask))

    back_image, back_mask, notes = imaging.read_editor(value)
    results.check("read back size", back_image.size == (200, 100))
    results.check("read back mask matches", pixels(back_mask) == pixels(mask), str(notes))

    empty = imaging.editor_value(img, None)
    results.check("no mask -> no layers", empty["layers"] == [])
    _, none_mask, _ = imaging.read_editor(empty)
    results.check("no mask reads as None", none_mask is None)

    # layer of the wrong size gets resampled, with a note
    odd = imaging.editor_value(img, mask)
    odd["layers"] = [odd["layers"][0].resize((100, 50))]
    oi, om, on = imaging.read_editor(odd)
    results.check("mismatched layer resampled", om.size == (200, 100))
    results.check("mismatch reported", any("resampled" in n for n in on), str(on))

    # ---------- smoothing ----------
    def coverage(m):
        return sum(v * n for v, n in enumerate(m.histogram())) // 255

    # A wide stroke with a sawtooth bitten out of one edge - what a fingertip
    # leaves behind.
    jagged = Image.new("L", (256, 256), 0)
    jagged.paste(255, (60, 40, 140, 210))
    for y in range(40, 210, 8):
        jagged.paste(0, (130, y, 140, y + 4))

    smoothed = imaging.smooth_mask(jagged, "Medium")
    results.check("smoothing stays binary", set(pixels(smoothed)) <= {0, 255})
    results.check("smoothing fills the notches", smoothed.getpixel((135, 44)) == 255)
    results.check("smoothing keeps the stroke", 0.8 < coverage(smoothed) / coverage(jagged) < 1.25,
          f"{coverage(smoothed)} vs {coverage(jagged)}")
    results.check("smoothing keeps the far edge", smoothed.getpixel((62, 120)) == 255)
    results.check("smoothing keeps outside clear", smoothed.getpixel((20, 120)) == 0)

    # A thin stroke must survive smoothing rather than be quietly deleted.
    thin = Image.new("L", (256, 256), 0)
    thin.paste(255, (100, 40, 106, 210))
    thin_smoothed = imaging.smooth_mask(thin, "High")
    results.check("a thin stroke survives High", coverage(thin_smoothed) > coverage(thin) * 0.5,
          f"{coverage(thin_smoothed)} vs {coverage(thin)}")

    # So must a mask made of features smaller than the radius.
    comb = Image.new("L", (256, 256), 0)
    for x in range(40, 200, 4):
        comb.paste(255, (x, 60, x + 2, 190))
    results.check("a comb is not erased", coverage(imaging.smooth_mask(comb, "High")) > coverage(comb) * 0.5)

    results.check("smoothing Off is identity", imaging.smooth_mask(jagged, "Off") is jagged)
    results.check("smoothing None mask", imaging.smooth_mask(None, "High") is None)

    # ---------- crop boxes ----------
    results.check("1:1 of 200x100", imaging.crop_box_for_ratio((200, 100), (1, 1)) == (50, 0, 150, 100))
    results.check("16:9 of 100x100", imaging.crop_box_for_ratio((100, 100), (16, 9)) == (0, 22, 100, 78))
    results.check("custom clamps", imaging.crop_box_for_size((200, 100), 400, 400) == (0, 0, 200, 100))
    results.check("custom centres", imaging.crop_box_for_size((200, 100), 100, 50) == (50, 25, 150, 75))

    ci, cm = imaging.apply_box(imaging.to_rgba(img), mask, (50, 0, 150, 100))
    results.check("crop keeps image and mask in step", ci.size == cm.size == (100, 100))

    # ---------- transparent frame trimming ----------
    padded = Image.new("RGBA", (120, 120), (0, 0, 0, 0))
    padded.paste(imaging.to_rgba(Image.new("RGB", (60, 40), (255, 0, 0))), (30, 40))
    pmask = Image.new("L", (120, 120), 0)
    ti, tm, note = imaging.trim_transparent_frame(padded, pmask)
    results.check("trim finds the real bounds", ti.size == (60, 40) and tm.size == (60, 40), str(ti.size))
    results.check("trim explains itself", bool(note))
    solid = imaging.to_rgba(Image.new("RGB", (30, 30), (1, 2, 3)))
    _, _, no_note = imaging.trim_transparent_frame(solid, None)
    results.check("nothing to trim on an opaque image", no_note is None)
    results.check("has_alpha_content: opaque", imaging.has_alpha_content(solid) is False)
    results.check("has_alpha_content: padded", imaging.has_alpha_content(padded) is True)

    # ---------- flatten ----------
    flat = imaging.flatten(padded, (127, 127, 127))
    results.check("flatten is opaque", not imaging.has_alpha_content(flat))
    results.check("flatten keeps the picture", flat.getpixel((35, 45))[:3] == (255, 0, 0))
    results.check("flatten fills the hole", flat.getpixel((0, 0))[:3] == (127, 127, 127))

    # ---------- outpaint ----------
    base = imaging.to_rgba(Image.new("RGB", (100, 80), (9, 9, 9)))
    grown, gmask, ginfo = outpaint.expand(base, None, (32, 0, 16, 0), overlap=0, fill="Transparent")
    results.check("expanded size", grown.size == (132, 96), str(grown.size))
    results.check("expand info", ginfo["to"] == (132, 96))
    results.check("original pixels moved", grown.getpixel((32 + 5, 16 + 5))[:3] == (9, 9, 9))
    results.check("new area transparent", grown.getpixel((0, 0))[3] == 0)
    results.check("left band masked", gmask.getpixel((5, 50)) == 255)
    results.check("top band masked", gmask.getpixel((60, 5)) == 255)
    results.check("original not masked", gmask.getpixel((100, 60)) == 0)
    results.check("right edge not masked", gmask.getpixel((131, 60)) == 0)

    # overlap reaches back into the original
    _, omask, oinfo = outpaint.expand(base, None, (32, 0, 0, 0), overlap=16, fill="Transparent")
    results.check("overlap applied", oinfo["overlap"] == 16)
    results.check("overlap masks into the original", omask.getpixel((32 + 8, 40)) == 255)
    results.check("overlap stops", omask.getpixel((32 + 20, 40)) == 0)

    # an existing mask survives the move
    premask = Image.new("L", (100, 80), 0)
    premask.paste(255, (70, 60, 90, 75))
    _, cmask, _ = outpaint.expand(base, premask, (10, 0, 10, 0), overlap=0, fill="Transparent")
    results.check("carried mask moved with the image", cmask.getpixel((80, 75)) == 255)

    # edge stretch fills with real pixels
    gradient = Image.new("RGB", (10, 10))
    gradient.putdata([(x * 25, 0, 0) for y in range(10) for x in range(10)])
    stretched, _, _ = outpaint.expand(imaging.to_rgba(gradient), None, (5, 5, 0, 0), 0, "Edge (stretch)")
    results.check("edge stretch is opaque on the left", stretched.getpixel((0, 5))[3] == 255)
    results.check("edge stretch copies the edge colour", stretched.getpixel((0, 5))[0] == 0)
    results.check("edge stretch copies the right edge", stretched.getpixel((19, 5))[0] == 225)

    # snapping and refusals
    results.check("snap 100 to 64", outpaint.snap_value(100, 64) == 128)
    results.check("snap 20 to 64", outpaint.snap_value(20, 64) == 64)
    results.check("snap 0 stays 0", outpaint.snap_value(0, 64) == 0)
    results.check("snap off", outpaint.snap_value(100, 0) == 100)
    results.check("snap from choice", outpaint.snap_from_choice("Off") == 0 and outpaint.snap_from_choice("32") == 32)
    try:
        outpaint.expand(base, None, (0, 0, 0, 0))
        results.check("refuses a no-op expansion", False)
    except ValueError:
        results.check("refuses a no-op expansion", True)
    for absurd, why in (((40000, 40000, 0, 0), "one huge side"), ((6000, 6000, 6000, 6000), "too many pixels")):
        try:
            outpaint.expand(base, None, absurd)
            results.check(f"refuses an absurd expansion: {why}", False)
        except ValueError:
            results.check(f"refuses an absurd expansion: {why}", True)
    results.check("describe", "132 x 96" in outpaint.describe((100, 80), (32, 0, 16, 0)), outpaint.describe((100, 80), (32, 0, 16, 0)))
    results.check("describe warns off-grid", "multiple of 8" in outpaint.describe((100, 80), (1, 0, 0, 0)))

    # ---------- document / history ----------
    doc = document.Document()
    doc.load(img, "local", "a.png")
    results.check("loaded", doc.has_image and not doc.has_mask)
    doc.checkpoint("crop")
    doc.commit(img.crop((0, 0, 50, 50)), None)
    results.check("committed crop", doc.size == (50, 50))
    results.check("undo label", doc.undo() == "crop")
    results.check("undo restored size", doc.size == (200, 100))
    results.check("redo", doc.redo() == "crop" and doc.size == (50, 50))
    results.check("undo runs out", (doc.undo(), doc.undo())[1] is None)
    doc.commit(img, mask)
    results.check("mask committed", doc.has_mask)
    results.check("describe mentions the mask", "mask" in doc.describe())
    for i in range(20):
        doc.checkpoint(f"step {i}")
    results.check("history is bounded", len(doc.history) == document.HISTORY_LIMIT)

    # a mask that arrives at the wrong size is resampled, never left to diverge
    doc.commit(img, mask.resize((20, 10)))
    results.check("commit keeps sizes equal", doc.mask.size == doc.image.size)

    # ---------- destinations ----------
    results.check("auto -> img2img", bridge.resolve_destination("Auto", False, False) == "img2img")
    results.check("auto + mask -> Inpaint", bridge.resolve_destination("Auto", True, False) == "Inpaint")
    results.check("auto + expand -> Inpaint", bridge.resolve_destination("Auto", False, True) == "Inpaint")
    results.check("explicit wins", bridge.resolve_destination("Extras", True, True) == "Extras")
    results.check("back to source", bridge.resolve_destination("Back to source", False, False, "extras") == "Extras")
    results.check("back to source default", bridge.resolve_destination("Back to source", False, False, "none") == "img2img")
    results.check("label plain", bridge.send_label(False, False) == "Send to img2img")
    results.check("label mask", bridge.send_label(True, False) == "Send to img2img Inpaint")
    results.check("label outpaint", bridge.send_label(True, True) == "Send Outpaint to img2img")

    # ---------- staging a send ----------
    payload, pnotes = bridge.prepare(padded, pmask.point(lambda v: 0), "img2img", "Neutral gray")
    results.check("payload has an image url", payload["image"].startswith("/file="))
    results.check("payload has no mask", payload["mask"] is None)
    results.check("payload selector", payload["selector"] == "#img2img_image")
    results.check("fill was reported", any("filled" in n for n in pnotes), str(pnotes))

    real_mask = Image.new("L", padded.size, 0)
    real_mask.paste(255, (10, 10, 40, 40))
    payload2, notes2 = bridge.prepare(padded, real_mask, "Inpaint", "Neutral gray")
    results.check("inpaint carries a mask", payload2["mask"] is not None)
    import pathlib, urllib.parse
    mask_file = pathlib.Path(urllib.parse.urlparse(payload2["mask"][len("/file="):]).path)
    staged = Image.open(mask_file)
    results.check("staged mask is RGBA", staged.mode == "RGBA")
    results.check("staged mask alpha is the coverage", staged.getchannel("A").getpixel((20, 20)) == 255)
    results.check("staged mask alpha is clear elsewhere", staged.getchannel("A").getpixel((100, 100)) == 0)
    staged_image = Image.open(pathlib.Path(urllib.parse.urlparse(payload2["image"][len("/file="):]).path))
    results.check("staged image matches the mask size", staged_image.size == staged.size)

    payload3, notes3 = bridge.prepare(padded, real_mask, "Extras", "Neutral gray")
    results.check("extras drops the mask", payload3["mask"] is None)
    results.check("extras says why", any("image only" in n for n in notes3), str(notes3))

    cn, _ = bridge.prepare(padded, None, "ControlNet", "White", 2, "img2img")
    results.check("controlnet payload", cn["controlnet"] == {"tab": "img2img", "index": 2})
    results.check("controlnet has no selector", cn["selector"] is None)

    results.check("json round trip", bridge.read_result(bridge.payload_json({"ok": True}))["ok"] is True)
    results.check("garbage result", bridge.read_result("{oops")["ok"] is False)
    results.check("empty result", bridge.read_result("") == {})

    return results


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
