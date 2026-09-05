"""The image maths, with nothing but Pillow.

These decide what pixels reach img2img: the mask is coverage carried as
alpha, crops trim only padding, expansion masks exactly what is new, and the
inpaint layer is what Forge's threshold expects.
"""

from harness import Results, pixels, setup_path

setup_path()

from PIL import Image  # noqa: E402

from minipaint_neo.canvas import document, imaging, outpaint  # noqa: E402
from minipaint_neo.canvas.ui import aspect_constraint, resolve_destination, send_label  # noqa: E402


def photo(width=64, height=48, color=(200, 30, 30)):
    return Image.new("RGB", (width, height), color)


def square_mask(size=(64, 48), box=(10, 10, 30, 30)):
    mask = Image.new("L", size, 0)
    mask.paste(255, box)
    return mask


def run() -> Results:
    r = Results("imaging")

    # ---- colours
    r.check("hex colour parses", imaging.parse_color("#ff2f2f") == (255, 47, 47))
    r.check("short hex parses", imaging.parse_color("#f00") == (255, 0, 0))
    r.check("bad colour falls back", imaging.parse_color("nope") == imaging.DEFAULT_MASK_COLOR)
    r.check("colour round-trips", imaging.color_hex((255, 47, 47)) == "#ff2f2f")

    # ---- editor value: coverage travels as a layer's alpha and comes back
    image = photo()
    mask = square_mask()
    value = imaging.editor_value(image, mask, (255, 47, 47))
    r.check("background is RGBA", value["background"].mode == "RGBA")
    r.check("one mask layer", len(value["layers"]) == 1)
    r.check("layer alpha is the mask", pixels(value["layers"][0].getchannel("A")) == pixels(mask))
    r.check("composite shows the mask colour", value["composite"].getpixel((15, 15))[:3] == (255, 47, 47))
    r.check("composite keeps the image elsewhere", value["composite"].getpixel((50, 40))[:3] == (200, 30, 30))
    r.check("no mask means no layers", imaging.editor_value(image, None)["layers"] == [])
    r.check("no image means no value", imaging.editor_value(None) is None)

    seen, seen_mask, notes = imaging.read_editor(value)
    r.check("read gives the image back", seen.size == (64, 48) and seen.getpixel((0, 0))[:3] == (200, 30, 30))
    r.check("read gives the mask back", pixels(seen_mask) == pixels(mask), str(notes))
    r.check("read of nothing is nothing", imaging.read_editor(None) == (None, None, []))
    r.check("read of a string is nothing", imaging.read_editor("text")[0] is None)
    r.check("an empty layer is no mask",
            imaging.read_editor({"background": image, "layers": [Image.new("RGBA", (64, 48), (0, 0, 0, 0))], "composite": image})[1] is None)

    # a layer of the wrong size is resampled rather than trusted
    odd = {"background": image, "layers": [imaging.mask_layer(square_mask((32, 24), (5, 5, 15, 15)))], "composite": image}
    _, odd_mask, odd_notes = imaging.read_editor(odd)
    r.check("odd layer resampled to the image", odd_mask.size == (64, 48) and odd_notes)

    # ---- trimming a padded crop
    padded = Image.new("RGBA", (64, 48), (0, 0, 0, 0))
    padded.paste(Image.new("RGBA", (20, 10), (1, 2, 3, 255)), (5, 7))
    trimmed, trimmed_mask, note = imaging.trim_transparent_frame(padded, mask)
    r.check("padding trimmed", trimmed.size == (20, 10) and note)
    r.check("mask trimmed with identical box", trimmed_mask.size == (20, 10))
    r.check("opaque image untouched", imaging.trim_transparent_frame(imaging.to_rgba(image), None)[2] is None)

    # ---- smoothing rounds a jagged edge but never eats a thin stroke
    jagged = Image.new("L", (256, 256), 0)
    for y in range(60, 200):
        jagged.paste(255, (60 + (7 if y % 2 else 0), y, 200 - (7 if y % 3 else 0), y + 1))
    smoothed = imaging.smooth_mask(jagged, "Medium")
    r.check("smoothing keeps most of the coverage", 0.85 < imaging._coverage(smoothed) / imaging._coverage(jagged) < 1.15)
    r.check("smoothing is binary", set(pixels(smoothed)) <= {0, 255})
    thin = Image.new("L", (256, 256), 0)
    thin.paste(255, (10, 100, 246, 103))
    r.check("a 3px stroke survives High", imaging._coverage(imaging.smooth_mask(thin, "High")) > imaging._coverage(thin) * 0.5)
    r.check("Off leaves the mask alone", imaging.smooth_mask(jagged, "Off") is jagged)
    r.check("no mask stays no mask", imaging.smooth_mask(None, "High") is None)

    # ---- invert
    inverted = imaging.invert_mask(mask, (64, 48))
    r.check("invert flips coverage", inverted.getpixel((15, 15)) == 0 and inverted.getpixel((50, 40)) == 255)
    r.check("invert of nothing is everything", imaging.invert_mask(None, (8, 8)).getpixel((0, 0)) == 255)

    # ---- fill and flatten
    r.check("named fills", imaging.fill_color("White", None) == (255, 255, 255))
    r.check("unknown fill is gray", imaging.fill_color("???", None) == (127, 127, 127))
    edged = Image.new("RGBA", (10, 10), (0, 0, 255, 255))
    r.check("edge colour reads the border", imaging.fill_color("Edge color", edged) == (0, 0, 255))
    flat = imaging.flatten(padded, (9, 9, 9))
    r.check("flatten fills transparency", flat.getpixel((0, 0)) == (9, 9, 9, 255) and flat.getpixel((6, 8)) == (1, 2, 3, 255))
    r.check("alpha content detected", imaging.has_alpha_content(padded) and not imaging.has_alpha_content(image))

    # ---- the inpaint layer Forge reads: alpha, binarised at 128
    soft = Image.new("L", (64, 48), 0)
    soft.paste(100, (0, 0, 10, 10))
    soft.paste(200, (20, 20, 30, 30))
    fg = imaging.inpaint_foreground(soft, (64, 48), (255, 47, 47))
    alpha = fg.getchannel("A")
    r.check("weak coverage is dropped", alpha.getpixel((5, 5)) == 0)
    r.check("strong coverage is kept", alpha.getpixel((25, 25)) == 255)
    r.check("foreground colour is the display colour", fg.getpixel((25, 25))[:3] == (255, 47, 47))
    r.check("foreground is resized to the image", imaging.inpaint_foreground(square_mask((32, 24)), (64, 48)).size == (64, 48))

    # ---- png round trip
    r.check("png round trip", pixels(imaging.from_png_bytes(imaging.to_png_bytes(mask))) == pixels(mask))

    # ---- expansion
    base = Image.new("RGBA", (100, 80), (10, 20, 30, 255))
    grown, grown_mask, info = outpaint.expand(base, None, (16, 0, 0, 24), overlap=0)
    r.check("expanded size", grown.size == (116, 104) and info["to"] == (116, 104))
    r.check("original placed at the offset", grown.getpixel((16, 0)) == (10, 20, 30, 255))
    r.check("new area is transparent", grown.getpixel((0, 0)) == (0, 0, 0, 0))
    r.check("new area is masked", grown_mask.getpixel((5, 5)) == 255 and grown_mask.getpixel((115, 100)) == 255)
    r.check("original is not masked", grown_mask.getpixel((60, 40)) == 0)

    _, overlap_mask, info2 = outpaint.expand(base, None, (0, 32, 0, 0), overlap=16)
    r.check("overlap reaches into the original", overlap_mask.getpixel((100 - 8, 40)) == 255 and overlap_mask.getpixel((100 - 24, 40)) == 0)
    r.check("overlap is reported", info2["overlap"] == 16)

    carried = square_mask((100, 80), (10, 10, 20, 20))
    _, carried_mask, _ = outpaint.expand(base, carried, (0, 0, 8, 0))
    r.check("existing mask moves with the image", carried_mask.getpixel((15, 23)) == 255 and carried_mask.getpixel((15, 5)) == 255)

    filled, _, _ = outpaint.expand(base, None, (8, 8, 8, 8), fill="White")
    r.check("solid fill gives real pixels", filled.getpixel((0, 0)) == (255, 255, 255, 255))
    stretched, _, _ = outpaint.expand(base, None, (8, 0, 0, 0), fill="Edge (stretch)")
    r.check("edge stretch copies the border", stretched.getpixel((2, 40)) == (10, 20, 30, 255))

    try:
        outpaint.expand(base, None, (0, 0, 0, 0))
        r.check("nothing to expand is refused", False)
    except ValueError:
        r.check("nothing to expand is refused", True)
    try:
        outpaint.expand(base, None, (20000, 0, 0, 0))
        r.check("a canvas no browser can hold is refused", False)
    except ValueError:
        r.check("a canvas no browser can hold is refused", True)

    r.check("snap rounds to the multiple", outpaint.snap_value(70, 64) == 64 and outpaint.snap_value(1, 8) == 8 and outpaint.snap_value(0, 8) == 0)
    r.check("snap off leaves values", outpaint.snap_value(70, 0) == 70)
    r.check("snap choice parses", outpaint.snap_from_choice("Off") == 0 and outpaint.snap_from_choice("32") == 32)
    r.check("preview names the size", "116 × 104" in outpaint.describe((100, 80), (16, 0, 0, 24)))
    r.check("preview with no image", outpaint.describe(None, (1, 1, 1, 1)) == "No image yet.")

    # ---- document history and the stage / commit handshake
    doc = document.Document()
    r.check("empty document", not doc.has_image and doc.describe() == "No image")
    doc.load(image, "txt2img")
    r.check("loaded document", doc.has_image and doc.origin == "txt2img" and "from txt2img" in doc.describe())
    doc.stage("crop", "Cropped.", image=image.crop((0, 0, 32, 24)), mask=None)
    r.check("staging changes nothing yet", doc.size == (64, 48))
    pending = doc.commit_pending()
    r.check("commit applies the staged image", doc.size == (32, 24) and pending["label"] == "crop")
    r.check("commit is one-shot", doc.commit_pending() is None)
    r.check("undo restores", doc.undo() == "crop" and doc.size == (64, 48))
    r.check("redo reapplies", doc.redo() == "crop" and doc.size == (32, 24))
    doc.stage("undo", "", restore="undo")
    doc.commit_pending()
    r.check("staged undo works", doc.size == (64, 48))
    doc.stage("expand", "", image=base, mask=grown_mask.resize((100, 80)), expansion={"to": (100, 80)})
    doc.commit_pending()
    r.check("expansion is remembered", doc.has_expansion and doc.has_mask and "expanded" in doc.describe())
    r.check("history is bounded", len(doc.history) <= document.HISTORY_LIMIT)
    r.check("ensure makes a document", isinstance(document.ensure(None), document.Document))

    # ---- send decisions
    r.check("auto with nothing is img2img", resolve_destination("Auto", False, False) == "img2img")
    r.check("auto with a mask is inpaint", resolve_destination("Auto", True, False) == "inpaint")
    r.check("auto after expand is inpaint", resolve_destination("Auto", False, True) == "inpaint")
    r.check("explicit choice wins", resolve_destination("Extras", True, True) == "extras")
    r.check("labels follow state", send_label(False, False, "crop") == "Send to img2img"
            and send_label(True, False, "crop") == "Send to img2img Inpaint"
            and send_label(False, False, "mask") == "Send to img2img Inpaint"
            and send_label(True, True, "mask") == "Send Outpaint to img2img")
    r.check("aspect: free is empty", aspect_constraint("Free", (64, 48), 0, 0) == "")
    r.check("aspect: preset passes through", aspect_constraint("16:9", None, 0, 0) == "16:9")
    r.check("aspect: original uses the image", aspect_constraint("Original", (640, 480), 0, 0) == "640:480")
    r.check("aspect: custom uses the numbers", aspect_constraint("Custom", None, 3, 2) == "3:2")
    r.check("aspect: bad custom is free", aspect_constraint("Custom", None, 0, 2) == "")

    return r


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
