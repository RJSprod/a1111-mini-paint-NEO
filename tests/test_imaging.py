"""The image maths, with nothing but Pillow.

These decide what pixels reach img2img: the mask is coverage carried as
alpha, the crop is the frame the browser reported, expansion masks exactly
what is new, and the scribble layer is what Forge's threshold expects.
"""

from harness import Results, pixels, setup_path

setup_path()

from PIL import Image  # noqa: E402

from minipaint_neo.canvas import document, imaging, outpaint  # noqa: E402
from minipaint_neo.canvas.ui import resolve_destination, send_label  # noqa: E402


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
    r.check("rgba hex drops alpha", imaging.parse_color("#80808080") == (128, 128, 128))
    r.check("tuples pass through", imaging.parse_color((1, 2, 3)) == (1, 2, 3))
    r.check("bad colour falls back", imaging.parse_color("nope") == imaging.DEFAULT_MASK_COLOR)

    # ---- what the canvas holds: image, and coverage as the scribble layer's alpha
    image = photo()
    mask = square_mask()
    layer = imaging.foreground_layer(mask, (64, 48), (255, 47, 47))
    r.check("layer is RGBA", layer.mode == "RGBA" and layer.size == (64, 48))
    r.check("layer alpha is the mask", pixels(layer.getchannel("A")) == pixels(mask))
    r.check("layer colour is the display colour", layer.getpixel((15, 15))[:3] == (255, 47, 47))
    seen, seen_mask, notes = imaging.read_canvas(image, layer)
    r.check("read gives the image back", seen.size == (64, 48) and seen.getpixel((0, 0))[:3] == (200, 30, 30))
    r.check("read gives the mask back", pixels(seen_mask) == pixels(mask), str(notes))
    r.check("read of nothing is nothing", imaging.read_canvas(None, layer) == (None, None, []))
    r.check("no layer is no mask", imaging.read_canvas(image, None)[1] is None)
    r.check("an empty layer is no mask", imaging.read_canvas(image, Image.new("RGBA", (64, 48), (0, 0, 0, 0)))[1] is None)
    odd_mask, odd_notes = imaging.mask_from_foreground(imaging.foreground_layer(square_mask((32, 24), (5, 5, 15, 15)), (32, 24)), (64, 48))
    r.check("a layer of the wrong size is resampled and noted", odd_mask.size == (64, 48) and odd_notes)

    # ---- the high-contrast layer: the canvas's checkerboard, alpha still the mask
    board = imaging.foreground_layer(mask, (64, 48), contrast=True)
    r.check("contrast layer alpha is the mask", pixels(board.getchannel("A")) == pixels(mask))
    r.check("contrast layer is a 10px checkerboard",
            board.getpixel((5, 5))[:3] == (255, 255, 255) and board.getpixel((15, 5))[:3] == (0, 0, 0)
            and board.getpixel((15, 15))[:3] == (255, 255, 255) and board.getpixel((25, 5))[:3] == (255, 255, 255))

    # ---- telling an echo from a new picture
    r.check("the same image is equal", imaging.images_equal(image, image.copy()))
    r.check("a re-encoded image is equal", imaging.images_equal(image, imaging.from_png_bytes(imaging.to_png_bytes(image))))
    r.check("a different image is not", not imaging.images_equal(image, photo(color=(0, 0, 0))))
    r.check("a different size is not", not imaging.images_equal(image, photo(32, 24)))
    r.check("none is only equal to none", imaging.images_equal(None, None) and not imaging.images_equal(image, None))
    soft = Image.new("RGBA", (8, 8), (200, 100, 50, 3))
    browsered = Image.new("RGBA", (8, 8), (170, 85, 0, 3))  # what a canvas makes of a nearly transparent pixel
    r.check("transparent pixels compare premultiplied", imaging.images_equal(soft, browsered))
    r.check("opaque pixels still have to match", not imaging.images_equal(Image.new("RGBA", (8, 8), (200, 100, 50, 255)), Image.new("RGBA", (8, 8), (170, 85, 0, 255))))

    # ---- the crop frame the browser reports
    r.check("crop box parses json", imaging.crop_box('{"x0": 10, "y0": 5, "x1": 50, "y1": 40}', (64, 48)) == (10, 5, 50, 40))
    r.check("crop box takes a dict", imaging.crop_box({"x0": 0, "y0": 0, "x1": 64, "y1": 48}, (64, 48)) == (0, 0, 64, 48))
    r.check("crop box is clamped", imaging.crop_box({"x0": -5, "y0": -5, "x1": 100, "y1": 100}, (64, 48)) == (0, 0, 64, 48))
    r.check("crop box rounds", imaging.crop_box({"x0": 9.6, "y0": 4.4, "x1": 50.2, "y1": 40}, (64, 48)) == (10, 4, 50, 40))
    r.check("an empty crop box is none", imaging.crop_box("", (64, 48)) is None and imaging.crop_box(None, (64, 48)) is None)
    r.check("a degenerate box is none", imaging.crop_box({"x0": 10, "y0": 10, "x1": 10, "y1": 30}, (64, 48)) is None)
    r.check("a box off the image is none", imaging.crop_box({"x0": 70, "y0": 0, "x1": 90, "y1": 10}, (64, 48)) is None)
    r.check("garbage is none", imaging.crop_box("{not json", (64, 48)) is None and imaging.crop_box({"x0": "a"}, (64, 48)) is None)

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
    padded = Image.new("RGBA", (64, 48), (0, 0, 0, 0))
    padded.paste(Image.new("RGBA", (20, 10), (1, 2, 3, 255)), (5, 7))
    r.check("named fills", imaging.fill_color("White", None) == (255, 255, 255))
    r.check("unknown fill is gray", imaging.fill_color("???", None) == (127, 127, 127))
    edged = Image.new("RGBA", (10, 10), (0, 0, 255, 255))
    r.check("edge colour reads the border", imaging.fill_color("Edge color", edged) == (0, 0, 255))
    flat = imaging.flatten(padded, (9, 9, 9))
    r.check("flatten fills transparency", flat.getpixel((0, 0)) == (9, 9, 9, 255) and flat.getpixel((6, 8)) == (1, 2, 3, 255))
    r.check("alpha content detected", imaging.has_alpha_content(padded) and not imaging.has_alpha_content(image))

    # ---- the inpaint layer Forge reads: alpha, binarised at 128
    soft_mask = Image.new("L", (64, 48), 0)
    soft_mask.paste(100, (0, 0, 10, 10))
    soft_mask.paste(200, (20, 20, 30, 30))
    fg = imaging.foreground_layer(soft_mask, (64, 48), (255, 47, 47))
    alpha = fg.getchannel("A")
    r.check("weak coverage is dropped", alpha.getpixel((5, 5)) == 0)
    r.check("strong coverage is kept", alpha.getpixel((25, 25)) == 255)
    r.check("foreground is resized to the image", imaging.foreground_layer(square_mask((32, 24)), (64, 48)).size == (64, 48))

    # ---- png round trip
    r.check("png round trip", pixels(imaging.from_png_bytes(imaging.to_png_bytes(mask))) == pixels(mask))
    r.check("megapixels", imaging.megapixels((1024, 1024)) == 1.05)

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

    # ---- layers: compositing, selections, merging
    L = document.Layer
    red = Image.new("RGBA", (20, 10), (255, 0, 0, 255))
    blue = Image.new("RGBA", (10, 10), (0, 0, 255, 255))
    flat = imaging.composite([L(red, 0, 0, "a"), L(blue, 15, 5, "b")], (30, 20))
    r.check("layers composite at their offsets", flat.getpixel((5, 5)) == (255, 0, 0, 255) and flat.getpixel((22, 8)) == (0, 0, 255, 255) and flat.getpixel((28, 18)) == (0, 0, 0, 0))
    r.check("a layer past the edge is clipped", imaging.composite([L(red, -10, -5, "a")], (30, 20)).getpixel((5, 2)) == (255, 0, 0, 255))
    r.check("a hidden layer is not drawn", imaging.composite([L(red, 0, 0, "a", visible=False)], (30, 20)).getpixel((5, 5)) == (0, 0, 0, 0))
    r.check("opacity scales the alpha", imaging.composite([L(red, 0, 0, "a", opacity=50)], (30, 20)).getpixel((5, 5))[3] == 127)
    merged, mx, my = imaging.merge_layers(L(red, 0, 0, "a"), L(blue, 15, 5, "b"))
    r.check("merging covers both layers", merged.size == (25, 15) and (mx, my) == (0, 0) and merged.getpixel((20, 8)) == (0, 0, 255, 255))
    piece = imaging.layer_pixels_in_box(red, 5, 5, (10, 0, 40, 8))
    r.check("the part of a layer inside a box", piece is not None and piece[0].size == (15, 3) and (piece[1], piece[2]) == (10, 5))
    r.check("a box that misses gives nothing", imaging.layer_pixels_in_box(red, 5, 5, (30, 30, 40, 40)) is None)
    coverage = Image.new("L", (30, 20), 0)
    coverage.paste(255, (2, 2, 8, 6))
    under = imaging.layer_pixels_under_mask(red, 0, 0, coverage)
    r.check("the part of a layer under a mask, trimmed", under is not None and under[0].size == (6, 4) and (under[1], under[2]) == (2, 2) and under[0].getpixel((0, 0))[3] == 255)
    r.check("a mask that misses gives nothing", imaging.layer_pixels_under_mask(blue, 20, 20, coverage) is None)

    # ---- the document and its history of structural steps
    doc = document.Document()
    r.check("empty document", not doc.has_image and doc.describe() == "No image" and doc.original_size_text() == "")
    doc.load(image, "txt2img")
    r.check("loaded document", doc.has_image and doc.origin == "txt2img" and "from txt2img" in doc.describe())
    r.check("one layer to start with", doc.layer_names() == ["Background"] and not doc.layered and doc.image.size == (64, 48))
    r.check("original size text", doc.original_size_text() == "64x48")
    doc.checkpoint("crop")
    doc.commit(image.crop((0, 0, 32, 24)), None)
    r.check("commit replaces the image", doc.size == (32, 24) and doc.original.size == (64, 48))
    r.check("undo restores", doc.undo() == "crop" and doc.size == (64, 48))
    r.check("redo reapplies", doc.redo() == "crop" and doc.size == (32, 24))
    r.check("nothing more to redo", doc.redo() is None)
    doc.checkpoint("mask")
    doc.commit(doc.image, square_mask((32, 24), (2, 2, 10, 10)))
    r.check("a mask is kept with the image", doc.has_mask and "mask" in doc.describe())
    doc.commit(doc.image, square_mask((64, 48)))
    r.check("a mask of the wrong size is resampled", doc.mask.size == (32, 24))
    doc.commit(doc.image, Image.new("L", (32, 24), 0))
    r.check("an empty mask is no mask", not doc.has_mask)
    r.check("undo brings the mask back", doc.undo() == "mask" and not doc.has_mask)
    for step in range(document.HISTORY_LIMIT + 3):
        doc.checkpoint(f"step {step}")
    r.check("history is bounded", len(doc.history) == document.HISTORY_LIMIT)
    doc.checkpoint("expand")
    doc.commit(base, grown_mask.resize((100, 80)))
    doc.has_expansion = True
    r.check("expansion is remembered", doc.has_expansion and doc.has_mask and "expanded" in doc.describe())

    # layers on the document: add, move, crop and expand across them, undo
    doc.checkpoint("layer")
    added = doc.add_layer(Image.new("RGBA", (20, 20), (9, 9, 9, 255)), 10, 10)
    r.check("a layer is added above the active one and becomes active", doc.layer_names() == ["Background", "Layer 2"] and doc.active == 1 and doc.layered)
    r.check("the composite shows it", doc.image.getpixel((15, 15)) == (9, 9, 9, 255))
    doc.commit(Image.new("RGBA", (100, 80), (1, 1, 1, 255)), None)
    r.check("with layers the canvas's picture is not taken as the document", doc.layer_names() == ["Background", "Layer 2"] and doc.image.getpixel((15, 15)) == (9, 9, 9, 255))
    doc.move_selected(50, 0)
    r.check("moving keeps the pixels and changes the offset", (added.x, added.y) == (60, 10) and doc.image.getpixel((65, 15)) == (9, 9, 9, 255) and doc.image.getpixel((15, 15)) != (9, 9, 9, 255))
    r.check("moving the selection leaves the layer beneath alone", (doc.layers[0].x, doc.layers[0].y) == (0, 0))
    doc.checkpoint("crop layers")
    doc.crop((55, 5, 95, 45), None)
    r.check("a crop trims every layer and shifts offsets", doc.size == (40, 40) and doc.layers[1].size == (20, 20) and (doc.layers[1].x, doc.layers[1].y) == (5, 5))
    doc.crop((0, 0, 4, 4), None)
    r.check("a crop that misses a layer drops it", doc.layer_names() == ["Background"] and doc.size == (4, 4))
    doc.undo()
    r.check("undo brings the layers and the canvas size back", doc.size == (100, 80) and doc.layer_names() == ["Background", "Layer 2"] and (doc.layers[1].x, doc.layers[1].y) == (60, 10))
    expanded, grown_mask2, _ = outpaint.expand(doc.base_full(), None, (16, 0, 8, 0))
    doc.expand((16, 0, 8, 0), expanded, grown_mask2)
    r.check("expanding grows the base and shifts the others", doc.size == (116, 88) and (doc.layers[1].x, doc.layers[1].y) == (76, 18) and doc.layers[0].size == (116, 88))
    r.check("unique names", doc.unique_name("Layer 2") == "Layer 2 2" and doc.unique_name("Fresh") == "Fresh")
    r.check("the drag preview describes the selected layer", '"w": 20' in doc.preview_payload() and doc.underlay_payload().startswith("data:image/png"))

    # the selection: one layer, several, the primary, and what acts on it
    third = doc.add_layer(Image.new("RGBA", (10, 10), (7, 7, 7, 255)), 0, 0, "Third")
    r.check("a new layer is the selection", doc.selected_names() == ["Third"] and doc.active_layer is third)
    r.check("select picks one", doc.select("Background") and doc.selected_names() == ["Background"] and doc.active == 0)
    r.check("selecting a name that is not there is refused", not doc.select("nope") and doc.selected_names() == ["Background"])
    r.check("toggle adds to the selection and makes it the primary", doc.toggle_selected("Third") and doc.selected_names() == ["Background", "Third"] and doc.active_layer is third)
    r.check("toggle takes it out again, the primary falls back", doc.toggle_selected("Third") and doc.selected_names() == ["Background"] and doc.active == 0)
    r.check("the selection never empties", doc.toggle_selected("Background") and doc.selected_names() == ["Background"])
    doc.toggle_selected("Layer 2")
    doc.toggle_selected("Third")
    r.check("three selected, in stacking order", doc.selected_names() == ["Background", "Layer 2", "Third"])
    payload = doc.preview_payload()
    r.check("the drag preview covers every selected layer", '"x": 0' in payload and '"y": 0' in payload and '"w": 116' in payload and doc.underlay_payload() == "")
    doc.select("Third")
    doc.toggle_selected("Layer 2")
    doc.move_selected(-6, 4)
    r.check("moving moves every selected layer", (third.x, third.y) == (-6, 4) and (doc.layers[1].x, doc.layers[1].y) == (70, 22) and (doc.layers[0].x, doc.layers[0].y) == (0, 0))
    doc.center_selected()
    r.check("center puts each selected layer in the middle of the canvas", (third.x, third.y) == (53, 39) and (doc.layers[1].x, doc.layers[1].y) == (48, 34))
    r.check("the eye toggles one layer", doc.set_visible("Background") is False and not doc.layers[0].visible and doc.set_visible("Background") is True)
    r.check("the eye on a missing layer is refused", doc.set_visible("nope") is None)
    doc.set_opacity(50)
    r.check("opacity applies to the selection", third.opacity == 50 and doc.layers[1].opacity == 50 and doc.layers[0].opacity == 100)
    rows = doc.layer_rows()
    r.check("the panel lists top layer first with the selection, the primary being the last added", [row["name"] for row in rows] == ["Third", "Layer 2", "Background"]
            and rows[1]["active"] and not rows[0]["active"] and rows[0]["selected"] and rows[1]["selected"] and not rows[2]["selected"] and rows[0]["top"] and rows[2]["bottom"] and rows[1]["opacity"] == 50)
    r.check("reorder swaps neighbours and keeps the selection", doc.reorder("Third", -1) and doc.layer_names() == ["Background", "Third", "Layer 2"] and doc.selected_names() == ["Third", "Layer 2"] and doc.active_layer.name == "Layer 2")
    r.check("reorder past the end is refused", not doc.reorder("Background", -1) and not doc.reorder("Layer 2", 1))
    copies = doc.duplicate_selected()
    r.check("duplicate copies each selected layer above its original", [c.name for c in copies] == ["Third copy", "Layer 2 copy"] and doc.layer_names() == ["Background", "Third", "Third copy", "Layer 2", "Layer 2 copy"] and doc.selected_names() == ["Third copy", "Layer 2 copy"])
    r.check("delete removes the selection and selects the layer below", doc.delete_selected() == ["Third copy", "Layer 2 copy"] and doc.layer_names() == ["Background", "Third", "Layer 2"] and doc.selected_names() == ["Layer 2"])
    doc.select("Third")
    doc.toggle_selected("Layer 2")
    doc.checkpoint("merge layers")
    r.check("merging several selected layers makes one, named after the lowest", doc.merge_selected() == "Third" and doc.layer_names() == ["Background", "Third"] and doc.selected_names() == ["Third"])
    doc.checkpoint("merge down")
    r.check("one selected layer merges down", doc.merge_selected() == "Background" and doc.layer_names() == ["Background"])
    r.check("the bottom layer has nothing to merge into", doc.merge_selected() is None)
    r.check("undo restores layers and selection", doc.undo() == "merge down" and doc.undo() == "merge layers" and doc.layer_names() == ["Background", "Third", "Layer 2"] and doc.selected_names() == ["Third", "Layer 2"])
    doc.checkpoint("delete")
    r.check("delete keeps the last layer", (doc.select("Background") and doc.toggle_selected("Third") and doc.toggle_selected("Layer 2") and doc.delete_selected() == ["Third", "Layer 2"] and doc.layer_names() == ["Background"]))
    doc.undo()
    r.check("flatten leaves one layer", doc.flatten() == 3 and doc.layer_names() == ["Background"] and doc.underlay_payload() == "")
    doc.load(image, "file", "cat.png")
    r.check("load resets expansion and names the file", not doc.has_expansion and "cat.png" in doc.describe() and not doc.has_mask)
    doc.clear()
    r.check("clear empties the document", not doc.has_image and doc.original is None)
    r.check("ensure makes a document", isinstance(document.ensure(None), document.Document))
    r.check("ensure keeps a document", document.ensure(doc) is doc)

    # ---- send decisions
    r.check("auto with nothing is img2img", resolve_destination("Auto", False, False) == "img2img")
    r.check("auto with a mask is inpaint", resolve_destination("Auto", True, False) == "inpaint")
    r.check("auto after expand is inpaint", resolve_destination("Auto", False, True) == "inpaint")
    r.check("explicit choice wins", resolve_destination("Extras", True, True) == "extras")
    r.check("labels follow state", send_label(False, False, "crop") == "Send to img2img"
            and send_label(True, False, "crop") == "Send to img2img Inpaint"
            and send_label(False, False, "mask") == "Send to img2img Inpaint"
            and send_label(True, True, "mask") == "Send Outpaint to img2img")
    r.check("an explicit destination names itself", send_label(True, True, "mask", "Extras") == "Send to Extras"
            and send_label(False, False, "crop", "ImageStitch (txt2img)") == "Send to ImageStitch (txt2img)"
            and resolve_destination("ImageStitch (img2img)", True, False) == "stitch_img2img")

    return r


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
