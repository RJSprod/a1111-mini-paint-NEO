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
