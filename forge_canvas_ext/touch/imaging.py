"""Image and mask primitives for the touch Canvas.

One rule runs through all of it: the mask is *coverage*, stored as an ``L``
image, and the colour it is drawn in on screen is presentation only. The
editor's layers are how that coverage travels to and from the browser; they
are never the thing that gets sent.
"""

from __future__ import annotations

import io
import typing

from PIL import Image, ImageChops, ImageFilter

try:  # Pillow >= 9.1
    NEAREST = Image.Resampling.NEAREST
    BILINEAR = Image.Resampling.BILINEAR
except AttributeError:  # pragma: no cover - older Pillow
    NEAREST = Image.NEAREST
    BILINEAR = Image.BILINEAR

# Forge's inpaint reads the foreground's alpha and thresholds it at 128, so
# that is the value anything we generate ourselves has to clear.
MASK_ON = 255
MASK_THRESHOLD = 128

SMOOTHING_LEVELS = ["Off", "Low", "Medium", "High"]
_SMOOTHING_RADIUS = {"Off": 0.0, "Low": 1.0, "Medium": 2.5, "High": 5.0}

# Thresholds either side of the middle, so blurring and cutting acts as a
# grow or a shrink of roughly the blur radius.
_GROW_AT = 32
_SHRINK_AT = 255 - _GROW_AT

DEFAULT_MASK_COLOUR = (255, 47, 47)

ASPECT_RATIOS = {
    "Free": None,
    "Original": "original",
    "1:1": (1.0, 1.0),
    "4:3": (4.0, 3.0),
    "3:4": (3.0, 4.0),
    "16:9": (16.0, 9.0),
    "9:16": (9.0, 16.0),
    "3:2": (3.0, 2.0),
    "2:3": (2.0, 3.0),
    "Custom": "custom",
}


def parse_colour(value: typing.Any) -> typing.Tuple[int, int, int]:
    """A ``#rrggbb`` string as an RGB triple, falling back to the default."""
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        try:
            return tuple(max(0, min(255, int(channel))) for channel in value[:3])
        except (TypeError, ValueError):
            return DEFAULT_MASK_COLOUR

    text = str(value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(character * 2 for character in text)
    if len(text) == 8:  # #rrggbbaa - the alpha is display-only, drop it
        text = text[:6]
    if len(text) != 6:
        return DEFAULT_MASK_COLOUR
    try:
        return tuple(int(text[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return DEFAULT_MASK_COLOUR


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
    colour: typing.Tuple[int, int, int] = DEFAULT_MASK_COLOUR,
) -> typing.Optional[Image.Image]:
    """Coverage rendered as an editor layer, so painting continues on top of it."""
    if mask_is_empty(mask):
        return None
    layer = Image.new("RGBA", mask.size, colour + (0,))
    layer.putalpha(mask)
    return layer


def composite_preview(
    image: Image.Image,
    mask: typing.Optional[Image.Image],
    colour: typing.Tuple[int, int, int] = DEFAULT_MASK_COLOUR,
) -> Image.Image:
    layer = mask_layer(mask, colour)
    if layer is None:
        return to_rgba(image)
    return Image.alpha_composite(to_rgba(image), layer)


def editor_value(
    image: typing.Optional[Image.Image],
    mask: typing.Optional[Image.Image] = None,
    colour: typing.Tuple[int, int, int] = DEFAULT_MASK_COLOUR,
) -> typing.Optional[dict]:
    """The ``EditorValue`` an ImageEditor takes: background, layers, composite."""
    if image is None:
        return None
    background = to_rgba(image)
    layer = mask_layer(mask, colour)
    return {
        "background": background,
        "layers": [layer] if layer is not None else [],
        "composite": composite_preview(background, mask, colour),
    }


def read_editor(
    value: typing.Any,
) -> typing.Tuple[
    typing.Optional[Image.Image], typing.Optional[Image.Image], typing.List[str]
]:
    """What the editor currently holds: working image, mask coverage, notes.

    ``background`` is the truth about size; a build that hands back layers of a
    different size (cropping has been buggy in more than one Gradio release)
    gets resampled onto it rather than being trusted to agree.
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
        notes.append("the editor had no background layer; its composite was used")

    image = to_rgba(base)
    if (
        background is not None
        and composite is not None
        and composite.size != background.size
    ):
        notes.append(
            f"the editor's composite is {composite.size[0]}x{composite.size[1]} but "
            f"its image is {background.size[0]}x{background.size[1]}; the image won"
        )

    mask, mask_notes = mask_from_layers(layers, image.size)
    notes.extend(mask_notes)
    return image, mask, notes


def _grow(mask: Image.Image, radius: float) -> Image.Image:
    blurred = mask.filter(ImageFilter.GaussianBlur(radius))
    return blurred.point(lambda v: MASK_ON if v > _GROW_AT else 0)


def _shrink(mask: Image.Image, radius: float) -> Image.Image:
    blurred = mask.filter(ImageFilter.GaussianBlur(radius))
    return blurred.point(lambda v: MASK_ON if v > _SHRINK_AT else 0)


def smooth_mask(
    mask: typing.Optional[Image.Image], level: str
) -> typing.Optional[Image.Image]:
    """Round off finger jitter without eating the stroke that made it.

    Grow-then-shrink fills the notches a wobbling fingertip leaves along an
    edge and puts the boundary back where it was, which a plain blur and cut
    does not: that erodes by most of its radius, and on a thin stroke it can
    erase the mask outright. The gentle pass afterwards takes the sawtooth off
    the outer side too.
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


def _coverage(mask: Image.Image) -> int:
    """How many pixels the mask actually covers, alpha-weighted."""
    return sum(value * count for value, count in enumerate(mask.histogram()))


def crop_box_for_ratio(
    size: typing.Tuple[int, int], ratio: typing.Tuple[float, float]
) -> typing.Tuple[int, int, int, int]:
    """The largest centred box of this aspect that fits - never a stretch."""
    width, height = size
    target = ratio[0] / ratio[1]
    if width / height > target:
        new_width = max(1, int(round(height * target)))
        new_height = height
    else:
        new_width = width
        new_height = max(1, int(round(width / target)))
    new_width = min(new_width, width)
    new_height = min(new_height, height)
    left = (width - new_width) // 2
    top = (height - new_height) // 2
    return (left, top, left + new_width, top + new_height)


def crop_box_for_size(
    size: typing.Tuple[int, int], want_width: int, want_height: int
) -> typing.Tuple[int, int, int, int]:
    """A centred box of exactly this many pixels, clamped to the image."""
    width, height = size
    new_width = max(1, min(int(want_width), width))
    new_height = max(1, min(int(want_height), height))
    left = (width - new_width) // 2
    top = (height - new_height) // 2
    return (left, top, left + new_width, top + new_height)


def apply_box(
    image: Image.Image,
    mask: typing.Optional[Image.Image],
    box: typing.Tuple[int, int, int, int],
) -> typing.Tuple[Image.Image, typing.Optional[Image.Image]]:
    """Crop image and mask with the same coordinates. They never diverge."""
    cropped = image.crop(box)
    if mask is None:
        return cropped, None
    return cropped, mask.crop(box)


def trim_transparent_frame(
    image: Image.Image, mask: typing.Optional[Image.Image]
) -> typing.Tuple[Image.Image, typing.Optional[Image.Image], typing.Optional[str]]:
    """Drop a transparent border the component padded a crop with.

    Only ever called when the image going *in* was fully opaque, so any
    transparent frame coming out is padding rather than the user's pixels.
    """
    alpha = to_rgba(image).getchannel("A")
    box = alpha.getbbox()
    if box is None:
        return image, mask, None
    if box == (0, 0, image.width, image.height):
        return image, mask, None

    cropped, cropped_mask = apply_box(image, mask, box)
    return (
        cropped,
        cropped_mask,
        f"trimmed {image.width}x{image.height} down to the "
        f"{cropped.width}x{cropped.height} the crop actually covers",
    )


def edge_colour(image: Image.Image) -> typing.Tuple[int, int, int]:
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
        return (127, 127, 127)
    return tuple(int(total / count) for total in totals)


FILL_COLOURS = {
    "Neutral gray": (127, 127, 127),
    "White": (255, 255, 255),
    "Black": (0, 0, 0),
}


def fill_colour(name: str, image: typing.Optional[Image.Image]) -> typing.Tuple[int, int, int]:
    if name == "Edge colour" and image is not None:
        return edge_colour(image)
    return FILL_COLOURS.get(name, FILL_COLOURS["Neutral gray"])


def flatten(image: Image.Image, colour: typing.Tuple[int, int, int]) -> Image.Image:
    """Composite onto a solid colour so the destination gets real pixels."""
    base = Image.new("RGBA", image.size, colour + (255,))
    return Image.alpha_composite(base, to_rgba(image))


def to_png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def from_png_bytes(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).copy()


def megapixels(size: typing.Tuple[int, int]) -> float:
    return round(size[0] * size[1] / 1_000_000.0, 2)
