"""The Clipboard tab: a host-backed image workspace between Forge tabs, and
the first caller of the public WanGP queue API.

Registered from ``scripts/mini_paint.py`` as a third, independent
integration: its own top-level tab, its own routes, its own state files. If
any of it fails to import or build, one line says why, Mini Paint and WanGP
load exactly as before, and the gallery's "Send to Mini Paint" keeps its old
behaviour.

The split:

``config``      the storage root, the intercept destination, the sort order
                and the thumbnail size - what survives a restart.
``intercept``   the gallery's send button pointed at WanGP: the frozen
                picture, the popup's capabilities, its submission through
                the outbox, and its own pinned history.
``store``       the library: one folder on the Forge host, opaque asset ids,
                containment, import, refresh, rename, delete, thumbnails.
``history``     the composer's draft and the recipes confirmed queued.
``outbox``      the queue: every press a job the server owns, in press order.
``executor``    the coordinator that advances those jobs with no browser
                open - cold WanGP, the enhancer, the card, the generation.
``job_inputs``  the pictures a queued job owns, pinned until it is done.
``enhance``     the prompt enhancer: ModelSwitchRefiner's MiniMax H3 writer,
                the switch, the four system prompts and their overrides.
``routes``      a picture by its id, and bytes in.
``ui``          the tab: a browser on the left, a WanGP request composer on
                the right, and the Add to Queue flow through
                ``window.minipaintInterop``.

What Clipboard says to WanGP it says through the public API and nothing
else; there is no private shortcut a third-party extension could not use.
"""

from __future__ import annotations

import typing

TAB_LABEL = "Clipboard"
TAB_ID = "minipaint_clipboard"

_LOG_PREFIX = "MiniPaint Clipboard:"


def intercept_target() -> str:
    """Where the gallery's Send to Mini Paint goes: minipaint, clipboard or wangp.

    Read from the host-side config on every call, so Reload UI and a second
    browser see one mode. Never raises: a Clipboard that cannot answer is a
    Clipboard that is off, and the send passes through as it always did.
    """
    try:
        from . import config

        return config.load().intercept_target
    except Exception:
        return "minipaint"


def intercept_enabled() -> bool:
    """Whether the gallery's Send to Mini Paint goes to Clipboard instead.

    The older question, kept for its callers: true for the Clipboard
    destination and for nothing else.
    """
    try:
        from . import config

        return intercept_target() == config.INTERCEPT_CLIPBOARD
    except Exception:
        return False


def available() -> bool:
    """Whether the Clipboard package loads here at all."""
    try:
        from . import config, history, store  # noqa: F401

        return True
    except Exception:
        return False


def _on_app_started(_demo: typing.Any, app: typing.Any) -> None:
    from .. import scrub
    from . import routes

    routes.install(app)

    # RECOVERY BEFORE ANY SWEEP, AND THAT ORDER IS THE PROTECTION.
    #
    # A queued job owns its pictures through a pin the sweeper reads. The
    # sweepers run at boot, and so does this; a sweep that ran first would
    # already have deleted the thing the pin was about to protect. Both
    # sweeps this competes with live in ``wangp`` and ``interop``, and both
    # are registered before this one - so the registration order below is
    # not incidental either.
    try:
        from . import executor

        counted = executor.recover()
        if counted.get("resumed") or counted.get("unknown") or counted.get("adopted"):
            scrub.console(
                f"queue recovery: {counted.get('resumed', 0)} job(s) resumed, "
                f"{counted.get('adopted', 0)} result(s) adopted, "
                f"{counted.get('unknown', 0)} left for a person to decide.",
                _LOG_PREFIX,
            )
    except Exception as error:  # pragma: no cover - never a reason to lose the tab
        scrub.console(f"the queue could not be recovered ({type(error).__name__}); nothing was resumed.", _LOG_PREFIX)

    # Inputs whose jobs are long finished, after the recovery above has
    # re-registered every pin that is still live.
    try:
        from . import job_inputs

        dropped = job_inputs.sweep()
        if dropped:
            scrub.console(f"released {dropped} finished job input(s).", _LOG_PREFIX)
    except Exception:
        pass

    # AFTER the recovery, and this order matters too: a claim left open by
    # a Forge that went away mid-generation is closed here, and recovery is
    # what has just decided whether its job is alive or gone. Closing first
    # would finish a claim whose job is about to resume.
    #
    # This is the whole of "closing the WebUI does not empty View Outputs":
    # nothing told this extension that WanGP wrote a file while it was not
    # running, so it goes and looks, once, at the start of every session.
    try:
        from . import outputs

        outputs.sync()
    except Exception as error:  # pragma: no cover - a gallery is never worth the tab
        scrub.console(f"the output ledger could not be reconciled ({type(error).__name__}).", _LOG_PREFIX)


def register(script_callbacks: typing.Any) -> None:
    """Add the tab and the routes to the host. The only thing the entry point calls.

    Each registration is contained on its own, the way ``wangp.register``
    does it: a route that cannot be added is not a reason to lose the tab,
    and none of them is a reason to lose Mini Paint.
    """
    from .. import scrub
    from . import ui

    for name, hook, callback in (
        ("tab", "on_ui_tabs", ui.on_ui_tabs),
        ("routes", "on_app_started", _on_app_started),
    ):
        try:
            getattr(script_callbacks, hook)(callback)
        except Exception as error:  # pragma: no cover - depends on the host
            scrub.console(f"the {name} could not be registered ({error}).", _LOG_PREFIX)


__all__ = ["TAB_ID", "TAB_LABEL", "available", "intercept_enabled", "intercept_target", "register"]
