"""The WanGP integration: a top-level tab holding the real WanGP UI, and an
intelligent Send to that asks the live WanGP session what it can accept.

Nothing here is imported by the Mini Paint tab's own code paths. The tab is
registered from ``scripts/mini_paint.py``; if any of this fails to import,
Mini Paint keeps working exactly as it did.

The split is deliberate and is the whole architecture:

``config``      what survives a restart, and only that.
``discovery``   is this a WanGP install, is this a usable interpreter, which
                physical GPUs exist.
``runtime``     the WanGP child process: one at a time, on a loopback port,
                with one GPU UUID visible, owned and tracked.
``proxy``       ``/wan2gp/*`` on the Forge origin, streamed to that loopback
                port and nowhere else.
``handoff``     PNGs on their way to WanGP: opaque ids, a controlled root,
                validation, cleanup.
``bridge``      the Forge-side record of which browser page is talking to
                which live WanGP session, and what it last said.
``ui``          the tab shell: setup wizard, health, iframe.
``settings``    one action - Reinitialize.
``diagnostics`` a report safe to paste into a bug.

The WanGP-side half lives in ``wan2gp_bridge/`` at the repository root and is
installed into the user's own WanGP ``plugins`` folder. It is the only piece
that knows WanGP component ids; MiniPaint never does.
"""

PROTOCOL = 2

_LOG_PREFIX = "MiniPaint WanGP:"


def _on_app_started(_demo, app) -> None:
    """The lifecycle half: the proxy's routes, and the two things that need
    the FastAPI app to exist.

    ``send_log`` already proves that ``on_app_started`` is where an extension
    attaches routes, so this is the same shape rather than a new one. Nothing
    is started here - startup is lazy, and a Forge that boots is a Forge with
    no WanGP process in it.
    """
    from . import handoff, proxy, ui

    ui.remember_app(app)
    proxy.install(app)

    # Prepared images from a previous run are files nobody will ever ask for
    # again; boot is the one moment it is certain no send is in flight.
    try:
        removed = handoff.sweep()
        if removed:
            print(f"{_LOG_PREFIX} cleared {removed} stale handoff file(s).")
    except Exception as error:  # pragma: no cover - a cleanup is never fatal
        print(f"{_LOG_PREFIX} the handoff folder could not be swept ({error}).")


def register(script_callbacks) -> None:
    """Add the WanGP tab, its settings entry and its routes to the host.

    Called from ``scripts/mini_paint.py`` and deliberately the only thing it
    calls, so the entry point stays a list of registrations and the reverse
    proxy, the child process and the bridge stay out of it.

    Each registration is contained on its own. The three are independent -
    a settings entry that cannot be added is not a reason to lose the tab -
    and none of them is a reason to lose Mini Paint, which is why the caller
    wraps this whole function as well.
    """
    from . import settings, ui

    for name, hook, callback in (
        ("settings entry", "on_ui_settings", settings.on_ui_settings),
        ("tab", "on_ui_tabs", ui.on_ui_tabs),
        ("routes", "on_app_started", _on_app_started),
    ):
        try:
            getattr(script_callbacks, hook)(callback)
        except Exception as error:  # pragma: no cover - depends on the host
            print(f"{_LOG_PREFIX} the {name} could not be registered ({error}).")

    # Where the host offers it, stop the child we own when the extension is
    # unloaded. Only ever our own tracked process; nothing is looked up by name.
    if hasattr(script_callbacks, "on_script_unloaded"):
        try:
            from . import runtime

            script_callbacks.on_script_unloaded(lambda: runtime.stop())
        except Exception as error:  # pragma: no cover - depends on the host
            print(f"{_LOG_PREFIX} the cleanup hook could not be registered ({error}).")
