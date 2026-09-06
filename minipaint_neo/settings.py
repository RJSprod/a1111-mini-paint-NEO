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
CANVAS_FIT = "minipaint_canvas_fit"
CANVAS_HEIGHT = "minipaint_canvas_height"
BRUSH_SIZE = "minipaint_brush_size"
EXPAND_SNAP = "minipaint_expand_snap"
# A new key on purpose: the earlier one ("minipaint_send_fill") defaulted to
# a gray fill and got saved along with everything else when settings were
# applied, so a saved gray kept overriding the transparency later rounds
# meant to send. A value saved under the old key is simply not read.
SEND_FILL = "minipaint_send_transparency"

SNAP_CHOICES = ["Off", "8", "16", "32", "64"]
KEEP_TRANSPARENT = "Keep transparent"
FILL_CHOICES = [KEEP_TRANSPARENT, "White", "Edge color", "Black"]

DEFAULTS: dict[str, typing.Any] = {
    USE_OLD_UI: False,
    CANVAS_FIT: True,
    CANVAS_HEIGHT: 70,
    BRUSH_SIZE: 25,
    EXPAND_SNAP: "8",
    SEND_FILL: KEEP_TRANSPARENT,
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


def canvas_fits_window() -> bool:
    """Size the canvas to what the window has left, so the whole tab is in
    view without scrolling; off means the fixed percentage below."""
    return bool(get(CANVAS_FIT, True))


def canvas_height_percent() -> int:
    try:
        return max(30, min(95, int(get(CANVAS_HEIGHT, 70))))
    except (TypeError, ValueError):
        return 70


def brush_width() -> int:
    """The canvas's brush width setting (1-100), as the Inpaint tab counts it."""
    try:
        width = int(get(BRUSH_SIZE, 25))
    except (TypeError, ValueError):
        width = 25
    return max(1, min(100, width))


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
        CANVAS_FIT,
        OptionInfo(
            DEFAULTS[CANVAS_FIT],
            "Canvas height: fit the window",
            section=SECTION,
            category_id=category,
        )
        .info(
            "the canvas takes whatever height the window has left below the controls, so the "
            "whole tab is in view without scrolling; off uses the fixed percentage below"
        )
        .needs_reload_ui(),
    )

    _add(
        CANVAS_HEIGHT,
        OptionInfo(
            DEFAULTS[CANVAS_HEIGHT],
            "Canvas height when not fitting the window (% of the browser window)",
            gr.Slider,
            {"minimum": 30, "maximum": 95, "step": 5},
            section=SECTION,
            category_id=category,
        )
        .info("touch Canvas only; the canvas's ⛶ button fills the window")
        .needs_reload_ui(),
    )

    _add(
        BRUSH_SIZE,
        OptionInfo(
            DEFAULTS[BRUSH_SIZE],
            "Mask brush size when the Canvas opens",
            gr.Slider,
            {"minimum": 1, "maximum": 100, "step": 1},
            section=SECTION,
            category_id=category,
        )
        .info("same scale as the Inpaint tab's brush; the mask colour and opacity follow the Inpaint settings")
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
            "Send: see-through pixels",
            gr.Dropdown,
            {"choices": FILL_CHOICES},
            section=SECTION,
            category_id=category,
        ).info(
            "what a hidden layer or an expansion leaves see-through: kept as transparency "
            "(the WebUI then fills it at generation time with the colour in Settings → img2img → "
            "'For img2img, fill the transparent parts of the input image with this color', gray unless "
            "changed; Extras gets white), or filled with a colour on the way out"
        ),
    )
