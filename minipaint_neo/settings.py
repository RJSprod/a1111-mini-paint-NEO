"""Forge settings for the extension: Settings -> miniPaint / Canvas.

The frontend the tab mounts is a saved WebUI option, not browser state, so it
survives a restart and can be changed from a tablet that has no dev tools.
"""

from __future__ import annotations

import os
import typing

import gradio as gr

from modules import shared

from . import scrub

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
#: Whether Forge runs queued WanGP jobs itself. On is the feature: press Add
#: to Queue and walk away, and the server starts a cold WanGP, waits for the
#: card if the user is using it, submits and tracks the generation to a
#: result. Off is what this extension used to do - the page that pressed the
#: button runs the job, so closing it stops the queue - and it is kept as a
#: way back, not as a default.
UNATTENDED_QUEUE = "minipaint_unattended_queue"
#: Whether the canvas is handed a same-origin URL for its display copy
#: rather than a base64 data URL. Off until somebody has proved on a real
#: install that Forge's own ForgeCanvas takes one: that is a question about
#: somebody else's JavaScript and reading it is not an answer. Everything on
#: this side is built and tested; this is the switch.
DISPLAY_OBJECTS = "minipaint_display_objects"
#: Whether a queued job is built from the settings the WanGP page is on, or
#: left for WanGP to fill in from its own defaults.
#:
#: On, the press commits the live form and the job composes from what was on
#: screen - the LoRAs, steps, guidance and profile the user has set up. Off,
#: nothing is read and nothing is carried: WanGP loads the model's saved
#: defaults, which for somebody whose defaults are already what they want is
#: the same answer with none of the moving parts.
#:
#: Off is the default because it is the path with nothing in it to go wrong.
#: Inheriting is the better promise and the more fragile one, and a person
#: who wants it should get to say so.
INHERIT_SETTINGS = "minipaint_inherit_wangp_settings"

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
    UNATTENDED_QUEUE: True,
    DISPLAY_OBJECTS: False,
    INHERIT_SETTINGS: False,
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


def unattended_queue() -> bool:
    """Whether the server advances queued jobs with no browser.

    Read on every press rather than cached: turning it off should stop new
    jobs being admitted to the server executor, and it should not need a
    Reload UI to do it. Jobs already admitted keep their own executor - a
    job halfway through a generation does not change hands because a
    checkbox moved.
    """
    return bool(get(UNATTENDED_QUEUE, True))


def inherit_settings() -> bool:
    """Whether a queued job is built from the WanGP page's live settings.

    See INHERIT_SETTINGS. Read on every press, like the executor choice, so
    turning it off stops the next job carrying settings without needing a
    Reload UI - and so a job already composed keeps the base it was composed
    with, because that is frozen and is not a checkbox's to change.
    """
    return bool(get(INHERIT_SETTINGS, False))


def display_objects() -> bool:
    """Whether a display copy travels as a URL rather than as base64.

    See DISPLAY_OBJECTS. The server side understands both whatever this
    says, because a page loaded while it was on can still hand a URL back
    after it is turned off.
    """
    return bool(get(DISPLAY_OBJECTS, False))


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
        scrub.console(f"could not register the {key} setting ({error})")


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
        UNATTENDED_QUEUE,
        OptionInfo(
            DEFAULTS[UNATTENDED_QUEUE],
            "WanGP queue: run queued jobs on the server",
            section=SECTION,
            category_id=category,
        ).info(
            "press Add to Queue and walk away - Forge starts WanGP if it is cold, waits if you are "
            "generating in the WanGP tab, and tracks the generation to a result with no browser open. "
            "Off means the page that pressed the button runs the job, so closing it stops the queue"
        ),
    )

    _add(
        INHERIT_SETTINGS,
        OptionInfo(
            DEFAULTS[INHERIT_SETTINGS],
            "WanGP queue: build queued jobs from the WanGP page's settings",
            section=SECTION,
            category_id=category,
        ).info(
            "on, a press commits whatever the WanGP tab is set to - LoRAs and their weights, steps, "
            "guidance, resolution, profile - and the job runs at that. Off, nothing is read and nothing "
            "is carried: WanGP fills the job in from that model's own saved defaults, which is the same "
            "answer with fewer moving parts if your defaults are already what you want"
        ),
    )

    _add(
        DISPLAY_OBJECTS,
        OptionInfo(
            DEFAULTS[DISPLAY_OBJECTS],
            "Canvas: send display copies as links instead of embedding them",
            section=SECTION,
            category_id=category,
        ).info(
            "the picture the canvas draws is fetched from this extension rather than embedded in the "
            "page update - about a third smaller, decoded off the main thread and cached by the browser. "
            "Off by default until it has been proven against the Forge you are running: if the canvas "
            "goes blank after an edit, turn it back off and say so"
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
