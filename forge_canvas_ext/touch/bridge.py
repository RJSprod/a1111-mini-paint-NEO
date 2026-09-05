"""Preparing a handoff to img2img.

Python decides *what* is sent and normalises it; the browser adapter does the
sending, because the destinations are host DOM components whose backing values
have to be written and then read back to know the transfer landed. The two
halves meet at a small JSON payload and a pair of ``/file=`` URLs - the image
bytes never travel through the event channel.
"""

from __future__ import annotations

import json
import time
import typing
import uuid

from PIL import Image

from ..paths import get_asset_url, prune_tmp, tmp_dir
from . import imaging

# What the user can pick. "Auto" is the one that should almost always be right.
DESTINATIONS = ["Auto", "img2img", "Inpaint", "ControlNet", "Extras", "Back to source"]

_TARGETS = {
    "img2img": {
        "key": "img2img_img2img",
        "selector": "#img2img_image",
        "label": "img2img",
        "switch": "img2img",
        "mask": False,
    },
    "Inpaint": {
        "key": "img2img_inpaint",
        "selector": "#img2maskimg",
        "label": "img2img Inpaint",
        "switch": "inpaint",
        "mask": True,
    },
    "Extras": {
        "key": "extras",
        "selector": "#extras_image",
        "label": "Extras",
        "switch": "extras",
        "mask": False,
    },
}


# Where an image came from, and where "Back to source" therefore returns it.
_ORIGINS = {"img2img": "img2img", "txt2img": "img2img", "extras": "Extras"}


def resolve_destination(
    choice: str, has_mask: bool, has_expansion: bool, origin: str = "none"
) -> str:
    """Which destination a send goes to. Deterministic, and never a surprise.

    Auto exists so the common case takes no decision: anything that carries a
    mask - drawn or created by an expansion - is an inpaint, everything else is
    a plain img2img.
    """
    if choice == "Back to source":
        return _ORIGINS.get(origin, "img2img")
    if choice and choice != "Auto":
        return choice
    return "Inpaint" if (has_mask or has_expansion) else "img2img"


def send_label(has_mask: bool, has_expansion: bool) -> str:
    if has_expansion:
        return "Send Outpaint to img2img"
    if has_mask:
        return "Send to img2img Inpaint"
    return "Send to img2img"


def _write(image: Image.Image, name: str) -> str:
    path = tmp_dir() / name
    image.save(path, format="PNG")
    return path


def prepare(
    image: Image.Image,
    mask: typing.Optional[Image.Image],
    destination: str,
    fill_name: str,
    controlnet_unit: int = 0,
    controlnet_tab: str = "img2img",
) -> typing.Tuple[dict, typing.List[str]]:
    """Normalise the document and stage it for the browser to deliver.

    Returns the payload and any notes worth putting on screen. Raises
    ``ValueError`` when there is nothing sendable, which the caller turns into
    a status line rather than an exception in the log.
    """
    if image is None:
        raise ValueError("There is no image to send.")

    notes: typing.List[str] = []
    target_name = destination if destination in _TARGETS else None
    controlnet = None
    if destination == "ControlNet":
        target = {
            "key": f"controlnet_{controlnet_tab}_{int(controlnet_unit)}",
            "selector": None,
            "label": f"ControlNet unit {int(controlnet_unit)} ({controlnet_tab})",
            "switch": controlnet_tab,
            "mask": False,
        }
        controlnet = {"tab": controlnet_tab, "index": int(controlnet_unit)}
    else:
        target = _TARGETS[target_name or "img2img"]

    working = imaging.to_rgba(image)

    # img2img needs real pixels everywhere. Expanded area is transparent in the
    # editor on purpose - it is obvious there what is new - so it is filled on
    # the way out rather than left for the destination to composite onto black.
    if imaging.has_alpha_content(working):
        colour = imaging.fill_colour(fill_name, working)
        working = imaging.flatten(working, colour)
        notes.append(
            f"transparent pixels were filled with rgb{colour} for the destination"
        )
    else:
        working = working.convert("RGB").convert("RGBA")

    token = uuid.uuid4().hex[:12]
    image_path = _write(working, f"send-{token}-image.png")

    mask_url = None
    mask_path = None
    if not imaging.mask_is_empty(mask) and target["mask"]:
        # Forge reads the inpaint mask as the *alpha* of the canvas foreground
        # and thresholds it at 128, so coverage is carried as alpha and the
        # colour underneath it is irrelevant.
        foreground = Image.new("RGBA", working.size, (255, 255, 255, 0))
        foreground.putalpha(
            mask if mask.size == working.size else mask.resize(working.size, imaging.NEAREST)
        )
        mask_path = _write(foreground, f"send-{token}-mask.png")
        mask_url = get_asset_url(mask_path)
    elif not imaging.mask_is_empty(mask):
        notes.append(
            f"the mask was not sent: {target['label']} takes an image only"
        )

    prune_tmp(keep=[path for path in (image_path, mask_path) if path is not None])

    payload = {
        "token": token,
        "destination": target["key"],
        "selector": target["selector"],
        "label": target["label"],
        "switch": target["switch"],
        "controlnet": controlnet,
        "image": get_asset_url(image_path),
        "mask": mask_url,
        "width": working.width,
        "height": working.height,
        "filename": f"canvas-{token}.png",
        "startedAt": time.time(),
    }
    return payload, notes


def payload_json(payload: dict) -> str:
    return json.dumps(payload)


def read_result(raw: typing.Any) -> dict:
    """Parse what the adapter wrote back. It is our own JSON, but still input."""
    if not raw:
        return {}
    try:
        result = json.loads(raw)
    except (TypeError, ValueError):
        return {"ok": False, "message": "the browser reported something unreadable"}
    return result if isinstance(result, dict) else {}


def save_for_download(image: Image.Image) -> str:
    """The working image as a PNG on disk, for the Save button."""
    token = uuid.uuid4().hex[:12]
    path = _write(imaging.to_rgba(image), f"send-{token}-download.png")
    prune_tmp(keep=[path])
    return str(path)
