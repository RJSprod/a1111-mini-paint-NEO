"""Image and mask primitives for the touch Canvas. Pillow only.

One rule runs through all of it: the mask is *coverage*, kept as an ``L``
image, and the colour it is painted in on screen is presentation only. The
editor's layers are how coverage travels to and from the browser; they are
never the thing that gets sent.
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

DEFAULT_MASK_COLOR = (255, 47, 47)

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


def color_hex(color: typing.Tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % tuple(color[:3])


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


def mask_from_layers(
    layers: typing.Sequence[typing.Optional[Image.Image]],
    size: typing.Tuple[int, int],
) -> typing.Tuple[typing.Optional[Image.Image], typing.List[str]]:
    """Union of the editor layers' alpha, as coverage at ``size``."""
    notes: typing.List[str] = []
    combined: typing.Optional[Image.Image] = None

    for layer in layers or []:
        if layer is None:
            continue
        alpha = to_rgba(layer).getchannel("A")
        if alpha.size != size:
            notes.append(
                f"a mask layer came back {alpha.size[0]}x{alpha.size[1]} for a "
                f"{size[0]}x{size[1]} image and was resampled to match"
            )
            alpha = alpha.resize(size, NEAREST)
        combined = alpha if combined is None else ImageChops.lighter(combined, alpha)

    if mask_is_empty(combined):
        return None, notes
    return combined, notes


def mask_layer(
    mask: typing.Optional[Image.Image],
    color: typing.Tuple[int, int, int] = DEFAULT_MASK_COLOR,
) -> typing.Optional[Image.Image]:
    """Coverage rendered as an editor layer, so painting continues on top of it."""
    if mask_is_empty(mask):
        return None
    layer = Image.new("RGBA", mask.size, tuple(color[:3]) + (0,))
    layer.putalpha(mask)
    return layer


def composite_preview(
    image: Image.Image,
    mask: typing.Optional[Image.Image],
    color: typing.Tuple[int, int, int] = DEFAULT_MASK_COLOR,
) -> Image.Image:
    layer = mask_layer(mask, color)
    if layer is None:
        return to_rgba(image)
    return Image.alpha_composite(to_rgba(image), layer)


def editor_value(
    image: typing.Optional[Image.Image],
    mask: typing.Optional[Image.Image] = None,
    color: typing.Tuple[int, int, int] = DEFAULT_MASK_COLOR,
) -> typing.Optional[dict]:
    """The ``EditorValue`` an ImageEditor takes: background, layers, composite."""
    if image is None:
        return None
    background = to_rgba(image)
    layer = mask_layer(mask, color)
    return {
        "background": background,
        "layers": [layer] if layer is not None else [],
        "composite": composite_preview(background, mask, color),
    }


def read_editor(
    value: typing.Any,
) -> typing.Tuple[
    typing.Optional[Image.Image], typing.Optional[Image.Image], typing.List[str]
]:
    """What the editor currently holds: working image, mask coverage, notes.

    The editor exports background, layers and composite all cropped to its
    current crop box, so ``background`` is the truth about size. A layer that
    disagrees is resampled onto it rather than trusted.
    """
    notes: typing.List[str] = []
    if not isinstance(value, dict):
        return None, None, notes

    background = value.get("background")
    composite = value.get("composite")
    layers = value.get("layers") or []

    base = background if background is not None else composite
    if base is None:
        return None, None, notes
    if background is None:
        notes.append("the editor had no background; its composite was used")

    image = to_rgba(base)
    if (
        background is not None
        and composite is not None
        and composite.size != background.size
    ):
        notes.append(
            f"the editor's composite is {composite.size[0]}x{composite.size[1]} but "
            f"its image is {background.size[0]}x{background.size[1]}; the image wins"
        )

    mask, mask_notes = mask_from_layers(layers, image.size)
    notes.extend(mask_notes)
    return image, mask, notes


def trim_transparent_frame(
    image: Image.Image, mask: typing.Optional[Image.Image]
) -> typing.Tuple[Image.Image, typing.Optional[Image.Image], typing.Optional[str]]:
    """Drop a transparent border the component padded a crop with.

    Only ever called when the image going *in* was fully opaque, so any
    transparent frame coming out is padding rather than the user's pixels.
    """
    alpha = to_rgba(image).getchannel("A")
    box = alpha.getbbox()
    if box is None or box == (0, 0, image.width, image.height):
        return image, mask, None

    cropped = image.crop(box)
    cropped_mask = None if mask is None else mask.crop(box)
    return (
        cropped,
        cropped_mask,
        f"trimmed {image.width}x{image.height} down to the "
        f"{cropped.width}x{cropped.height} the crop actually covers",
    )


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
    return mask.point(lambda v: MASK_ON - v)


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


def inpaint_foreground(
    mask: Image.Image,
    size: typing.Tuple[int, int],
    color: typing.Tuple[int, int, int] = DEFAULT_MASK_COLOR,
) -> Image.Image:
    """The scribble layer Forge's Inpaint canvas reads its mask from.

    Forge takes the alpha channel and keeps pixels above 128, so coverage is
    carried as alpha, binarised, and the colour underneath is only what the
    user sees on the Inpaint canvas.
    """
    if mask.size != size:
        mask = mask.resize(size, NEAREST)
    foreground = Image.new("RGBA", size, tuple(color[:3]) + (0,))
    foreground.putalpha(binarize(mask))
    return foreground


def to_png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


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
