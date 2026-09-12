"""The Clipboard tab: a host-backed image workspace between Forge tabs, and
the first caller of the public WanGP queue API.

Registered from ``scripts/mini_paint.py`` as a third, independent
integration: its own top-level tab, its own routes, its own state files. If
any of it fails to import or build, one line says why, Mini Paint and WanGP
load exactly as before, and the gallery's "Send to Mini Paint" keeps its old
behaviour.

The split:

``config``      the storage root, the intercept switch, the sort order and
                the thumbnail size - what survives a restart.
``store``       the library: one folder on the Forge host, opaque asset ids,
                containment, import, refresh, rename, delete, thumbnails.
``history``     the composer's draft and the recipes confirmed queued.
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


def intercept_enabled() -> bool:
    """Whether the gallery's Send to Mini Paint goes to Clipboard instead.

    Read from the host-side config on every call, so Reload UI and a second
    browser see one mode. Never raises: a Clipboard that cannot answer is a
    Clipboard that is off, and the send passes through as it always did.
    """
    try:
        from . import config

        return bool(config.load().intercept)
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
    from . import routes

    routes.install(app)


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


__all__ = ["TAB_ID", "TAB_LABEL", "available", "intercept_enabled", "register"]
