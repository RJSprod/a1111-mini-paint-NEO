"""Forge settings for the extension: Settings -> miniPaint / Canvas.

The frontend the tab mounts is a saved WebUI option, not browser state, so it
survives a restart and can be changed from a tablet that has no dev tools.
"""

from __future__ import annotations

import os
import typing

import gradio as gr

from modules import shared

SECTION = ("minipaint_canvas", "miniPaint / Canvas")

USE_OLD_UI = "minipaint_use_old_ui"
CANVAS_HEIGHT = "minipaint_canvas_height"
MASK_COLOR = "minipaint_mask_color"
BRUSH_SIZE = "minipaint_brush_size"
EXPAND_SNAP = "minipaint_expand_snap"
SEND_FILL = "minipaint_send_fill"

SNAP_CHOICES = ["Off", "8", "16", "32", "64"]
FILL_CHOICES = ["Neutral gray", "Edge color", "White", "Black"]

DEFAULTS: dict[str, typing.Any] = {
    USE_OLD_UI: False,
    CANVAS_HEIGHT: 70,
    MASK_COLOR: "#ff2f2f",
    BRUSH_SIZE: 0,
    EXPAND_SNAP: "8",
    SEND_FILL: "Neutral gray",
}

# The setting is the way to switch editors - but it lives in a UI, and the one
# time it is needed most is when a UI is not co-operating. This lever needs no
# UI at all: MINIPAINT_OLD_UI=1 in the environment forces the legacy editor.
OLD_UI_ENV = "MINIPAINT_OLD_UI"
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def get(key: str, default: typing.Any = None) -> typing.Any:
    """Read a saved option without assuming this WebUI registered it yet."""
    if default is None:
        default = DEFAULTS.get(key)
    try:
        value = getattr(shared.opts, key)
    except (AttributeError, KeyError):
        return default
    return default if value is None else value


def use_old_ui() -> bool:
    override = os.environ.get(OLD_UI_ENV, "").strip().lower()
    if override in _TRUE:
        return True
    if override in _FALSE:
        return False
    return bool(get(USE_OLD_UI, False))


def canvas_height_percent() -> int:
    try:
        return max(30, min(95, int(get(CANVAS_HEIGHT, 70))))
    except (TypeError, ValueError):
        return 70


def brush_size():
    """Brush radius in pixels, or "auto" for the editor's own default."""
    try:
        size = int(get(BRUSH_SIZE, 0))
    except (TypeError, ValueError):
        size = 0
    return size if size > 0 else "auto"


def _category(name: str):
    """Only claim a settings category the host actually has."""
    try:
        from modules import options

        mapping = getattr(getattr(options, "categories", None), "mapping", None)
        if mapping and name in mapping:
            return name
    except Exception:
        pass
    return None


def _color_picker():
    try:
        from modules.ui_components import FormColorPicker

        return FormColorPicker
    except Exception:
        return gr.ColorPicker


def _add(key: str, info) -> None:
    """Register one option, and never let it be the reason Settings breaks.

    The one that matters is the frontend switch, registered first. Losing a
    later one to a host that does not accept it is a missing checkbox; taking
    the Settings page down with it would also take down the way back to the
    legacy editor.
    """
    try:
        shared.opts.add_option(key, info)
    except Exception as error:  # pragma: no cover - depends on the host
        print(f"MiniPaint: could not register the {key} setting ({error})")


def on_ui_settings() -> None:
    OptionInfo = shared.OptionInfo
    category = _category("ui")

    _add(
        USE_OLD_UI,
        OptionInfo(
            DEFAULTS[USE_OLD_UI],
            "Use Old UI (legacy miniPaint)",
            section=SECTION,
            category_id=category,
        )
        .info(
            "Use the original miniPaint editor instead of the touch-first Canvas. "
            "The legacy editor remains fully installed as a fallback."
        )
        .needs_reload_ui(),
    )

    _add(
        CANVAS_HEIGHT,
        OptionInfo(
            DEFAULTS[CANVAS_HEIGHT],
            "Canvas height (% of the browser window)",
            gr.Slider,
            {"minimum": 30, "maximum": 95, "step": 5},
            section=SECTION,
            category_id=category,
        )
        .info("touch Canvas only")
        .needs_reload_ui(),
    )

    _add(
        MASK_COLOR,
        OptionInfo(
            DEFAULTS[MASK_COLOR],
            "Mask brush color",
            _color_picker(),
            {},
            section=SECTION,
            category_id=category,
        )
        .info("display only - the mask is sent as coverage, never as this color")
        .needs_reload_ui(),
    )

    _add(
        BRUSH_SIZE,
        OptionInfo(
            DEFAULTS[BRUSH_SIZE],
            "Mask brush radius in pixels",
            gr.Slider,
            {"minimum": 0, "maximum": 200, "step": 1},
            section=SECTION,
            category_id=category,
        )
        .info("0 = automatic, from the image size; the editor's brush menu changes it per session")
        .needs_reload_ui(),
    )

    _add(
        EXPAND_SNAP,
        OptionInfo(
            DEFAULTS[EXPAND_SNAP],
            "Expand: snap side amounts to a multiple of",
            gr.Dropdown,
            {"choices": SNAP_CHOICES},
            section=SECTION,
            category_id=category,
        ).needs_reload_ui(),
    )

    _add(
        SEND_FILL,
        OptionInfo(
            DEFAULTS[SEND_FILL],
            "Send: fill transparent pixels with",
            gr.Dropdown,
            {"choices": FILL_CHOICES},
            section=SECTION,
            category_id=category,
        ).info(
            "img2img needs real pixels everywhere; expanded area is transparent in the "
            "editor and filled with this on the way out"
        ),
    )
