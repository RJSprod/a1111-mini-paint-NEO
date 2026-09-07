"""The Mini Paint bridge, as WanGP sees it.

A companion plugin installed into someone else's application, so the whole of
this package is written to be inert until it is spoken to and harmless when it
cannot work. Importing it never imports WanGP, never imports Gradio at module
level in a way that could fail loudly, and never touches the filesystem. If
the plugin cannot be built - a WanGP whose plugin base class moved, a Gradio
that will not load - the import here still succeeds and ``PLUGIN_CLASS`` is
None, because a bridge that does not load must look like a missing feature to
the user and like nothing at all to WanGP.

The pieces:

``protocol``            the vocabulary, copied verbatim from the Forge side.
``compatibility``       the only file that knows a WanGP id or setting name.
``receiver_state``      the normalised live state and its fingerprint.
``receiver_adapters``   how each logical receiver is read, filled and checked.
``handoff``             validating the PNG Forge left behind an opaque id.
``bridge_ui``           the three invisible Gradio components.
``bridge_js``           the script that runs inside the WanGP document.
``plugin``              the hooks, and the single event everything runs on.
"""

from __future__ import annotations

import typing

BRIDGE_NAME = "wan2gp-minipaint-bridge"

PLUGIN_CLASS: typing.Optional[type]
try:
    from .plugin import MiniPaintBridge, MiniPaintBridgePlugin, create

    PLUGIN_CLASS = MiniPaintBridgePlugin
    Plugin = MiniPaintBridgePlugin
except Exception as error:  # pragma: no cover - reported, never raised
    print(f"[{BRIDGE_NAME}] did not load: {error!r}")
    MiniPaintBridge = None  # type: ignore[assignment]
    MiniPaintBridgePlugin = None  # type: ignore[assignment]
    PLUGIN_CLASS = None
    Plugin = None  # type: ignore[assignment]

    def create(*args: typing.Any, **kwargs: typing.Any) -> typing.Any:  # type: ignore[misc]
        return None


__all__ = ["BRIDGE_NAME", "MiniPaintBridge", "MiniPaintBridgePlugin", "PLUGIN_CLASS", "Plugin", "create"]
