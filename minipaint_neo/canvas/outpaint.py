"""Canvas expansion: a bigger image, the old one placed in it, the new area masked.

Nothing here is an AI operation. The editor makes room and says which pixels
are new; Forge generates them.
"""

from __future__ import annotations

import typing

from PIL import Image, ImageChops, ImageDraw

from . import imaging

FILL_POLICIES = ["Transparent", "Edge (stretch)", "Neutral gray", "White", "Black"]
DEFAULT_FILL = "Transparent"
SIDES = ("Left", "Right", "Top", "Bottom")
AMOUNTS = ["64", "128", "256"]

# Above these the browser, not the server, is what falls over: canvases have
# both a total-pixel budget and a per-side one, and Safari's is the lowest of
# the lot. Refuse rather than hand a tab an image it cannot hold.
HARD_LIMIT_PIXELS = 64_000_000
HARD_LIMIT_SIDE = 16_384
WARN_MEGAPIXELS = 16.0


def snap_from_choice(choice: typing.Any) -> int:
    if choice in (None, "Off", "off", ""):
        return 0
    try:
        return int(choice)
    except (TypeError, ValueError):
        return 0


def snap_value(value: typing.Any, snap: typing.Any) -> int:
    """Round one side's amount to the configured multiple."""
    try:
        amount = max(0, int(round(float(value))))
    except (TypeError, ValueError):
        return 0
    try:
        step = int(snap)
    except (TypeError, ValueError):
        step = 0
    if step > 1 and amount:
        amount = max(step, int(round(amount / step)) * step)
    return amount


def resulting_size(
    size: typing.Tuple[int, int], sides: typing.Sequence[int]
) -> typing.Tuple[int, int]:
    left, right, top, bottom = sides
    return (size[0] + left + right, size[1] + top + bottom)


def describe(
    size: typing.Optional[typing.Tuple[int, int]], sides: typing.Sequence[int]
) -> str:
    """The line above Apply: what this expansion would produce."""
    if size is None:
        return "No image yet."
    if not any(sides):
        return f"{size[0]} × {size[1]} — tap a side to add to it."

    new_width, new_height = resulting_size(size, sides)
    line = f"{size[0]} × {size[1]}  →  **{new_width} × {new_height}**"
    if new_width % 8 or new_height % 8:
        line += "  (not a multiple of 8)"
    if imaging.megapixels((new_width, new_height)) > WARN_MEGAPIXELS:
        line += f"  — {imaging.megapixels((new_width, new_height))} megapixels may use significant memory"
    return line


def _edge_stretch(canvas: Image.Image, image: Image.Image, sides) -> None:
    """Pull the border pixels outwards so the new area is not a flat slab."""
    left, right, top, bottom = sides
    width, height = image.size

    if left:
        canvas.paste(image.crop((0, 0, 1, height)).resize((left, height)), (0, top))
    if right:
        canvas.paste(
            image.crop((width - 1, 0, width, height)).resize((right, height)),
            (left + width, top),
        )
    if top:
        canvas.paste(image.crop((0, 0, width, 1)).resize((width, top)), (left, 0))
    if bottom:
        canvas.paste(
            image.crop((0, height - 1, width, height)).resize((width, bottom)),
            (left, top + height),
        )

    corners = (
        (left and top, (0, 0, 1, 1), (left, top), (0, 0)),
        (right and top, (width - 1, 0, width, 1), (right, top), (left + width, 0)),
        (left and bottom, (0, height - 1, 1, height), (left, bottom), (0, top + height)),
        (
            right and bottom,
            (width - 1, height - 1, width, height),
            (right, bottom),
            (left + width, top + height),
        ),
    )
    for wanted, box, target_size, position in corners:
        if wanted:
            canvas.paste(image.crop(box).resize(target_size), position)


def _new_area_mask(
    size: typing.Tuple[int, int], sides: typing.Sequence[int], overlap: int
) -> Image.Image:
    """Everything that was added, plus ``overlap`` pixels back into the original."""
    width, height = size
    left, right, top, bottom = sides
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)

    def band(x0, y0, x1, y1):
        x0 = max(0, min(width, x0))
        y0 = max(0, min(height, y0))
        x1 = max(0, min(width, x1))
        y1 = max(0, min(height, y1))
        if x1 > x0 and y1 > y0:
            draw.rectangle([x0, y0, x1 - 1, y1 - 1], fill=imaging.MASK_ON)

    if left:
        band(0, 0, left + overlap, height)
    if right:
        band(width - right - overlap, 0, width, height)
    if top:
        band(0, 0, width, top + overlap)
    if bottom:
        band(0, height - bottom - overlap, width, height)

    return mask


def expand(
    image: Image.Image,
    mask: typing.Optional[Image.Image],
    sides: typing.Sequence[int],
    overlap: int = 0,
    fill: str = DEFAULT_FILL,
) -> typing.Tuple[Image.Image, Image.Image, dict]:
    """Grow the canvas and mask everything that is new.

    Returns the new image, the new mask, and what happened - the caller turns
    that into the status line.
    """
    left, right, top, bottom = (max(0, int(side)) for side in sides)
    sides = (left, right, top, bottom)
    if not any(sides):
        raise ValueError("Nothing to expand: every side is 0.")

    source = imaging.to_rgba(image)
    width, height = source.size
    new_width, new_height = resulting_size(source.size, sides)

    if max(new_width, new_height) > HARD_LIMIT_SIDE:
        raise ValueError(
            f"{new_width} × {new_height} has a side over {HARD_LIMIT_SIDE}px, which is "
            "past what a browser canvas can hold. Expand less, or crop first."
        )
    if new_width * new_height > HARD_LIMIT_PIXELS:
        raise ValueError(
            f"{new_width} × {new_height} is {imaging.megapixels((new_width, new_height))} "
            "megapixels, which no browser canvas will hold. Expand less, or crop first."
        )

    overlap = max(0, int(overlap))
    # Overlap that swallows the original would mask the whole image, which is
    # a generation rather than an outpaint.
    overlap = min(overlap, max(0, min(width, height) // 2 - 1))

    if fill in ("Transparent", "Edge (stretch)"):
        background = (0, 0, 0, 0)
    else:
        background = imaging.fill_color(fill, source) + (255,)

    canvas = Image.new("RGBA", (new_width, new_height), background)
    if fill == "Edge (stretch)":
        _edge_stretch(canvas, source, sides)
    canvas.paste(source, (left, top))

    new_mask = _new_area_mask((new_width, new_height), sides, overlap)
    if not imaging.mask_is_empty(mask):
        carried = Image.new("L", (new_width, new_height), 0)
        carried.paste(mask, (left, top))
        new_mask = ImageChops.lighter(new_mask, carried)

    return (
        canvas,
        new_mask,
        {
            "sides": sides,
            "overlap": overlap,
            "fill": fill,
            "from": (width, height),
            "to": (new_width, new_height),
            "megapixels": imaging.megapixels((new_width, new_height)),
        },
    )
