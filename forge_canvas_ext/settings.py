"""Forge Neo settings for the Canvas tab.

The frontend the tab mounts is a saved WebUI setting, not browser state, so it
survives a restart and can be changed from a phone that has no dev tools.
"""

from __future__ import annotations

import inspect
import typing

import gradio as gr

from modules import shared

SECTION = ("minipaint_canvas", "miniPaint / Canvas")

USE_OLD_UI = "minipaint_use_old_ui"
CANVAS_HEIGHT = "minipaint_canvas_height"
DEFAULT_TOOL = "minipaint_canvas_default_tool"
MASK_COLOUR = "minipaint_canvas_mask_colour"
SNAP = "minipaint_canvas_snap"
SEND_FILL = "minipaint_canvas_send_fill"
WARN_MEGAPIXELS = "minipaint_canvas_warn_megapixels"

TOOLS = ["Crop", "Mask", "Expand"]
SNAP_CHOICES = ["Off", "8", "16", "32", "64"]
FILL_CHOICES = ["Neutral gray", "White", "Black", "Edge colour"]


def get(key: str, default: typing.Any) -> typing.Any:
    """Read a saved option without assuming this WebUI registered it."""
    try:
        value = getattr(shared.opts, key)
    except (AttributeError, KeyError):
        return default
    return default if value is None else value


def use_old_ui() -> bool:
    return bool(get(USE_OLD_UI, False))


def _option_info(default, label, *component, **kwargs):
    """Build an OptionInfo with only the keyword arguments this build accepts.

    ``category_id`` arrived after ``section`` did, and this extension is meant
    to load on the whole Forge family rather than one commit of it. The
    component and its arguments go positionally because they have been the
    third and fourth parameters throughout.
    """
    accepted = kwargs
    try:
        parameters = inspect.signature(shared.OptionInfo.__init__).parameters
        accepted = {k: v for k, v in kwargs.items() if k in parameters}
    except (TypeError, ValueError):  # pragma: no cover - exotic builds
        pass
    return shared.OptionInfo(default, label, *component, **accepted)


def _add(key, option, info: str = "", reload_ui: bool = False) -> None:
    if info and hasattr(option, "info"):
        option = option.info(info)
    if reload_ui and hasattr(option, "needs_reload_ui"):
        option = option.needs_reload_ui()
    shared.opts.add_option(key, option)


def on_ui_settings() -> None:
    _add(
        USE_OLD_UI,
        _option_info(
            False,
            "Use Old UI (legacy miniPaint)",
            section=SECTION,
            category_id="ui",
        ),
        "Use the original miniPaint editor instead of the touch-first Canvas "
        "redesign. The legacy editor remains fully installed as a fallback.",
        reload_ui=True,
    )

    _add(
        CANVAS_HEIGHT,
        _option_info(
            70,
            "Canvas height (% of the browser window)",
            gr.Slider,
            {"minimum": 40, "maximum": 95, "step": 5},
            section=SECTION,
            category_id="ui",
        ),
        "How much of the window the image takes up in the new Canvas UI.",
        reload_ui=True,
    )

    _add(
        DEFAULT_TOOL,
        _option_info(
            "Crop",
            "Tool selected when the Canvas opens",
            gr.Radio,
            {"choices": TOOLS},
            section=SECTION,
            category_id="ui",
        ),
        reload_ui=True,
    )

    _add(
        MASK_COLOUR,
        _option_info(
            "#ff2f2f",
            "Mask overlay colour",
            gr.ColorPicker,
            section=SECTION,
            category_id="ui",
        ),
        "Display only. The mask itself is sent as coverage, never as this colour.",
        reload_ui=True,
    )

    _add(
        SNAP,
        _option_info(
            "8",
            "Snap expansion amounts to a multiple of",
            gr.Dropdown,
            {"choices": SNAP_CHOICES},
            section=SECTION,
        ),
        "Outpaint sides are rounded to this many pixels.",
    )

    _add(
        SEND_FILL,
        _option_info(
            "Neutral gray",
            "Fill transparent pixels with, when sending",
            gr.Dropdown,
            {"choices": FILL_CHOICES},
            section=SECTION,
        ),
        "img2img needs real pixels everywhere. Expanded area stays transparent "
        "in the editor and is filled with this on the way out.",
    )

    _add(
        WARN_MEGAPIXELS,
        _option_info(
            16,
            "Warn above this many megapixels",
            gr.Slider,
            {"minimum": 4, "maximum": 64, "step": 1},
            section=SECTION,
        ),
        "Browsers get slow and phones run out of memory well before the "
        "server does.",
    )
