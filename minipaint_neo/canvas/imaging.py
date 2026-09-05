"""Image and mask primitives for the touch Canvas. Pillow only.

One rule runs through all of it: the mask is *coverage*, kept as an ``L``
image, and the colour it is painted in on screen is presentation only. The
canvas's scribble layer is how coverage travels to and from the browser; its
alpha channel is what Forge's Inpaint reads, and what is read here.
"""

from __future__ import annotations

import io
import typing

from PIL import Image, ImageChops, ImageFilter

try:  # Pillow >= 9.1
    NEAREST = Image.Resampling.NEAREST
except AttributeError:  # pragma: no cover - older Pillow
    NEAREST = Image.NEAREST

# Forge's inpaint reads the foreground's alpha and keeps pixels above 128, so
# that is the value anything we produce ourselves has to clear.
MASK_ON = 255
MASK_THRESHOLD = 128

SMOOTHING_LEVELS = ["Off", "Low", "Medium", "High"]
_SMOOTHING_RADIUS = {"Off": 0.0, "Low": 1.0, "Medium": 2.5, "High": 5.0}
_GROW_AT = 32
_SHRINK_AT = 255 - _GROW_AT

DEFAULT_MASK_COLOR = (128, 128, 128)

FILL_COLORS = {
    "Neutral gray": (127, 127, 127),
    "White": (255, 255, 255),
    "Black": (0, 0, 0),
}


def parse_color(value: typing.Any) -> typing.Tuple[int, int, int]:
    """A ``#rrggbb`` string as an RGB triple, falling back to the default."""
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        try:
            return tuple(max(0, min(255, int(channel))) for channel in value[:3])
        except (TypeError, ValueError):
            return DEFAULT_MASK_COLOR

    text = str(value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(character * 2 for character in text)
    if len(text) == 8:  # #rrggbbaa - the alpha is display-only, drop it
        text = text[:6]
    if len(text) != 6:
        return DEFAULT_MASK_COLOR
    try:
        return tuple(int(text[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return DEFAULT_MASK_COLOR


def to_rgba(image: Image.Image) -> Image.Image:
    return image if image.mode == "RGBA" else image.convert("RGBA")


def has_alpha_content(image: typing.Optional[Image.Image]) -> bool:
    """Does this image have anything less than fully opaque in it?"""
    if image is None:
        return False
    if image.mode not in ("RGBA", "LA", "PA") and "transparency" not in image.info:
        return False
    alpha = to_rgba(image).getchannel("A")
    return alpha.getextrema()[0] < 255


def mask_is_empty(mask: typing.Optional[Image.Image]) -> bool:
    return mask is None or mask.getbbox() is None


def binarize(mask: Image.Image) -> Image.Image:
    return mask.point(lambda v: MASK_ON if v >= MASK_THRESHOLD else 0)


def _premultiplied(image: Image.Image) -> typing.Tuple[Image.Image, Image.Image]:
    """(RGB scaled by alpha, alpha). A browser canvas stores pixels this way,
    so it is the only form in which a picture survives a round trip through
    one unchanged; the colour of a fully transparent pixel does not."""
    rgba = to_rgba(image)
    alpha = rgba.getchannel("A")
    rgb = ImageChops.multiply(rgba.convert("RGB"), Image.merge("RGB", (alpha, alpha, alpha)))
    return rgb, alpha


def images_equal(a: typing.Optional[Image.Image], b: typing.Optional[Image.Image]) -> bool:
    """Same picture, allowing for a browser's re-encoding of it."""
    if a is None or b is None:
        return a is b
    if a.size != b.size:
        return False
    rgb_a, alpha_a = _premultiplied(a)
    rgb_b, alpha_b = _premultiplied(b)
    if ImageChops.difference(alpha_a, alpha_b).getextrema()[1] > 2:
        return False
    return all(high <= 3 for _low, high in ImageChops.difference(rgb_a, rgb_b).getextrema())


def mask_from_foreground(
    foreground: typing.Optional[Image.Image], size: typing.Tuple[int, int]
) -> typing.Tuple[typing.Optional[Image.Image], typing.List[str]]:
    """Coverage from the canvas's scribble layer: its alpha channel."""
    notes: typing.List[str] = []
    if foreground is None:
        return None, notes
    alpha = to_rgba(foreground).getchannel("A")
    if alpha.size != size:
        notes.append(
            f"the mask layer came back {alpha.size[0]}x{alpha.size[1]} for a "
            f"{size[0]}x{size[1]} image and was resampled to match"
        )
        alpha = alpha.resize(size, NEAREST)
    if mask_is_empty(alpha):
        return None, notes
    return alpha, notes


def read_canvas(
    background: typing.Optional[Image.Image], foreground: typing.Optional[Image.Image]
) -> typing.Tuple[
    typing.Optional[Image.Image], typing.Optional[Image.Image], typing.List[str]
]:
    """What the canvas currently holds: working image, mask coverage, notes."""
    if background is None:
        return None, None, []
    image = to_rgba(background)
    mask, notes = mask_from_foreground(foreground, image.size)
    return image, mask, notes


def crop_box(raw: typing.Any, size: typing.Tuple[int, int]) -> typing.Optional[typing.Tuple[int, int, int, int]]:
    """The crop frame the browser reported, clamped to the image. None if unusable."""
    import json

    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else None
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    try:
        x0, y0, x1, y1 = (int(round(float(raw[key]))) for key in ("x0", "y0", "x1", "y1"))
    except (KeyError, TypeError, ValueError):
        return None
    width, height = size
    x0, x1 = max(0, min(width, x0)), max(0, min(width, x1))
    y0, y1 = max(0, min(height, y0)), max(0, min(height, y1))
    if x1 - x0 < 1 or y1 - y0 < 1:
        return None
    return (x0, y0, x1, y1)


def _grow(mask: Image.Image, radius: float) -> Image.Image:
    blurred = mask.filter(ImageFilter.GaussianBlur(radius))
    return blurred.point(lambda v: MASK_ON if v > _GROW_AT else 0)


def _shrink(mask: Image.Image, radius: float) -> Image.Image:
    blurred = mask.filter(ImageFilter.GaussianBlur(radius))
    return blurred.point(lambda v: MASK_ON if v > _SHRINK_AT else 0)


def _coverage(mask: Image.Image) -> int:
    """How many pixels the mask covers, alpha-weighted."""
    return sum(value * count for value, count in enumerate(mask.histogram()))


def smooth_mask(
    mask: typing.Optional[Image.Image], level: str
) -> typing.Optional[Image.Image]:
    """Round off finger jitter without eating the stroke that made it.

    Grow-then-shrink fills the notches a wobbling fingertip leaves along an
    edge and puts the boundary back where it was, which a plain blur-and-cut
    does not: that erodes by most of its radius and can erase a thin stroke
    outright. A gentle pass afterwards takes the sawtooth off the outer side.
    """
    if mask_is_empty(mask) or level not in _SMOOTHING_RADIUS:
        return mask
    radius = _SMOOTHING_RADIUS[level]
    if radius <= 0:
        return mask

    # Scale with the document so "Medium" means the same thing on a 512 and a
    # 2048 image, but keep it bounded so corners stay reachable.
    scale = min(3.0, max(1.0, min(mask.size) / 1024.0))
    radius = radius * scale

    smoothed = _shrink(_grow(mask, radius), radius)
    smoothed = smoothed.filter(ImageFilter.GaussianBlur(radius / 2.0)).point(
        lambda v: MASK_ON if v > MASK_THRESHOLD else 0
    )

    # A setting that quietly deletes most of what the user painted is worse
    # than no smoothing at all, so it is refused rather than sent.
    before = _coverage(mask)
    after = _coverage(smoothed)
    if before and after < before * 0.5:
        return mask
    return smoothed


def invert_mask(mask: typing.Optional[Image.Image], size: typing.Tuple[int, int]) -> Image.Image:
    if mask_is_empty(mask):
        return Image.new("L", size, MASK_ON)
    return binarize(mask).point(lambda v: MASK_ON - v)


def edge_color(image: Image.Image) -> typing.Tuple[int, int, int]:
    """Mean colour of the image's opaque border pixels."""
    rgba = to_rgba(image)
    width, height = rgba.size
    strips = [
        rgba.crop((0, 0, width, 1)),
        rgba.crop((0, height - 1, width, height)),
        rgba.crop((0, 0, 1, height)),
        rgba.crop((width - 1, 0, width, height)),
    ]

    totals = [0, 0, 0]
    count = 0
    for strip in strips:
        for pixel in strip.getdata():
            if pixel[3] == 0:
                continue
            totals[0] += pixel[0]
            totals[1] += pixel[1]
            totals[2] += pixel[2]
            count += 1

    if not count:
        return FILL_COLORS["Neutral gray"]
    return tuple(int(total / count) for total in totals)


def fill_color(name: str, image: typing.Optional[Image.Image]) -> typing.Tuple[int, int, int]:
    if name == "Edge color" and image is not None:
        return edge_color(image)
    return FILL_COLORS.get(name, FILL_COLORS["Neutral gray"])


def flatten(image: Image.Image, color: typing.Tuple[int, int, int]) -> Image.Image:
    """Composite onto a solid colour so the destination gets real pixels."""
    base = Image.new("RGBA", image.size, tuple(color[:3]) + (255,))
    return Image.alpha_composite(base, to_rgba(image))


# The canvas's high-contrast brush: 10px black and white squares, anchored
# at the drawing canvas's origin. The same tile here, so a mask written from
# Python looks like one that was painted.
CONTRAST_SQUARE = 10


def _checkerboard(size: typing.Tuple[int, int]) -> Image.Image:
    tile = Image.new("RGB", (CONTRAST_SQUARE * 2, CONTRAST_SQUARE * 2), (0, 0, 0))
    tile.paste((255, 255, 255), (0, 0, CONTRAST_SQUARE, CONTRAST_SQUARE))
    tile.paste((255, 255, 255), (CONTRAST_SQUARE, CONTRAST_SQUARE, CONTRAST_SQUARE * 2, CONTRAST_SQUARE * 2))
    board = Image.new("RGB", size)
    for y in range(0, size[1], tile.height):
        for x in range(0, size[0], tile.width):
            board.paste(tile, (x, y))
    return board


def foreground_layer(
    mask: Image.Image,
    size: typing.Tuple[int, int],
    color: typing.Tuple[int, int, int] = DEFAULT_MASK_COLOR,
    contrast: bool = False,
) -> Image.Image:
    """Coverage as a scribble layer: the colour where the mask is, alpha 255.

    Forge takes the alpha channel and keeps pixels above 128, so coverage is
    carried as alpha, binarised, and the colour is only what the user sees:
    the Inpaint brush colour, or its checkerboard when high contrast is on.
    """
    if mask.size != size:
        mask = mask.resize(size, NEAREST)
    if contrast:
        layer = _checkerboard(size).convert("RGBA")
    else:
        layer = Image.new("RGBA", size, tuple(color[:3]) + (0,))
    layer.putalpha(binarize(mask))
    return layer


# -- layers --------------------------------------------------------------


def scale_alpha(image: Image.Image, opacity: int) -> Image.Image:
    """The image with its alpha multiplied by opacity/100."""
    rgba = to_rgba(image)
    if opacity >= 100:
        return rgba
    faded = rgba.copy()
    faded.putalpha(rgba.getchannel("A").point(lambda v: v * max(0, opacity) // 100))
    return faded


def _intersection(a, b):
    box = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    return box if box[2] > box[0] and box[3] > box[1] else None


def paste_clipped(canvas: Image.Image, image: Image.Image, x: int, y: int) -> None:
    """Alpha-composite ``image`` onto ``canvas`` at (x, y), clipped to the canvas."""
    inter = _intersection((x, y, x + image.width, y + image.height), (0, 0, canvas.width, canvas.height))
    if inter is None:
        return
    piece = to_rgba(image).crop((inter[0] - x, inter[1] - y, inter[2] - x, inter[3] - y))
    canvas.alpha_composite(piece, dest=(inter[0], inter[1]))


def composite(layers, size: typing.Tuple[int, int]) -> Image.Image:
    """The visible layers, bottom to top, on a transparent canvas."""
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    for layer in layers:
        if not layer.visible or layer.opacity <= 0:
            continue
        paste_clipped(canvas, scale_alpha(layer.image, layer.opacity), layer.x, layer.y)
    return canvas


def merge_layers(lower, upper) -> typing.Tuple[Image.Image, int, int]:
    """``upper`` composited onto ``lower`` as the canvas shows them; the
    result covers both. Returns (image, x, y)."""
    x0 = min(lower.x, upper.x)
    y0 = min(lower.y, upper.y)
    x1 = max(lower.x + lower.image.width, upper.x + upper.image.width)
    y1 = max(lower.y + lower.image.height, upper.y + upper.image.height)
    merged = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
    paste_clipped(merged, scale_alpha(lower.image, lower.opacity if lower.visible else 0), lower.x - x0, lower.y - y0)
    if upper.visible:
        paste_clipped(merged, scale_alpha(upper.image, upper.opacity), upper.x - x0, upper.y - y0)
    return merged, x0, y0


def layer_pixels_in_box(image: Image.Image, x: int, y: int, box) -> typing.Optional[typing.Tuple[Image.Image, int, int]]:
    """The part of a layer (at x, y) inside a document box: (image, x, y), or None."""
    inter = _intersection((x, y, x + image.width, y + image.height), tuple(box))
    if inter is None:
        return None
    piece = to_rgba(image).crop((inter[0] - x, inter[1] - y, inter[2] - x, inter[3] - y))
    return piece, inter[0], inter[1]


def layer_pixels_under_mask(image: Image.Image, x: int, y: int, mask: Image.Image) -> typing.Optional[typing.Tuple[Image.Image, int, int]]:
    """The layer's pixels where the document mask covers them, trimmed to
    what is left: (image, x, y), or None when nothing is."""
    inter = _intersection((x, y, x + image.width, y + image.height), (0, 0, mask.width, mask.height))
    if inter is None:
        return None
    piece = to_rgba(image).crop((inter[0] - x, inter[1] - y, inter[2] - x, inter[3] - y))
    coverage = binarize(mask.crop(inter))
    alpha = ImageChops.multiply(piece.getchannel("A"), coverage)
    bbox = alpha.getbbox()
    if bbox is None:
        return None
    piece = piece.crop(bbox)
    piece.putalpha(alpha.crop(bbox))
    return piece, inter[0] + bbox[0], inter[1] + bbox[1]


def to_png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def to_data_url(image: Image.Image) -> str:
    """The PNG data URL the host's hidden image textboxes carry."""
    import base64

    return "data:image/png;base64," + base64.b64encode(to_png_bytes(to_rgba(image))).decode("ascii")


def from_png_bytes(data: bytes) -> Image.Image:
    with Image.open(io.BytesIO(data)) as opened:
        opened.load()
        return opened.copy()


def open_file(path: str) -> Image.Image:
    """Decode an image file as RGBA. Raises whatever Pillow raises."""
    with Image.open(path) as opened:
        opened.load()
        # convert() builds a new image, but for a file that is already RGBA it
        # would hand back the one this block is about to close.
        return opened.copy() if opened.mode == "RGBA" else opened.convert("RGBA")


def megapixels(size: typing.Tuple[int, int]) -> float:
    return round(size[0] * size[1] / 1_000_000.0, 2)
