"""One entry on the Settings page, and deliberately not a switch.

The integration has exactly one thing worth managing from outside its own tab
- Reinitialize - and Reinitialize is an action, not a value. Forge's settings
system stores values: every control on that page is bound to a key that is
written to config.json when Apply is pressed and read back on the next start.
A checkbox called "Reinitialize WanGP integration" would therefore be a
checkbox that is saved, so it would either stay on forever and reinitialize on
every restart, or have to be quietly reset behind the user's back. Section
49.7 names that shape and rules it out, and this module exists to not build
it.

What is built instead is one informational entry that does not persist
anything. Where the host offers a settings control with no value - A1111 and
its descendants have ``shared.OptionHTML``, whose ``do_not_save`` flag is
exactly this case - that is what is used, and the entry carries the sentence
that says what Reinitialize will and will not delete plus a link into the
WanGP tab, where the real button lives. Where the host has no such mechanism
the same entry is registered as an ordinary option holding an empty string
with ``do_not_save`` set, so the worst case is a stored empty string and never
a stored boolean that means "do it again".

The wizard's own fields are not here and must not be added here: duplicating
them would give a user two places to set a WanGP root and no way to know which
one the process was launched from.
"""

from __future__ import annotations

import typing

#: The one key. Named for what the entry is - a pointer at the management
#: surface - rather than for an action, because nothing here performs one.
MANAGE_KEY = "minipaint_wangp_manage"

_LOG_PREFIX = "MiniPaint WanGP:"

#: What Reinitialize does and, more usefully, what it leaves alone. The same
#: promise is repeated on the tab's own confirmation, because this is the text
#: a user reads before deciding and the tab's is the text they read while
#: deciding.
NOTICE_HTML = (
    "<b>WanGP integration.</b> Setup, restart and <b>Reinitialize</b> all live in the "
    "<b>WanGP</b> tab, so there is one place that knows what the running process was "
    "started from. "
    '<a href="#" id="minipaint_wangp_settings_link" '
    "onclick=\"if(window.minipaintWanGP&&window.minipaintWanGP.switchToWanGP){window.minipaintWanGP.switchToWanGP();}return false;\""
    ">Open the WanGP tab</a>."
    "<br>Reinitialize only forgets which WanGP installation, environment and GPU this "
    "extension was pointed at. It does not uninstall WanGP, and it deletes no models, "
    "no LoRAs, no presets, no outputs and no unrelated plugins."
)


def option_info(shared: typing.Any) -> typing.Any:
    """Build the one entry, using the best mechanism this host actually has.

    Detected at runtime rather than chosen at import time: the same extension
    runs on Forge Neo, on Forge and on A1111, and which of them exposes a
    value-free settings control is a fact about the installation in front of
    us. ``OptionHTML`` is the native one; the fallback is the same object with
    the flag set by hand, which costs a stored empty string on a host that
    ignores it - and an empty string cannot be mistaken for an instruction.
    """
    section = _section()
    category = _category(shared, "ui")

    option_html = getattr(shared, "OptionHTML", None)
    if option_html is not None:
        info = option_html(NOTICE_HTML)
        # OptionHTML fixes label and component; the section is ours to set, so
        # the entry lands next to the extension's other settings rather than
        # at the bottom of whichever section happened to be open.
        info.section = section
        if category is not None:
            info.category_id = category
        return info

    import gradio as gr

    info = shared.OptionInfo(
        "",
        "WanGP integration",
        gr.HTML,
        {"value": NOTICE_HTML},
        section=section,
        category_id=category,
    )
    # The flag a host that has never heard of it will ignore. Setting it is
    # still right: on a host that honours it nothing is stored at all, and on
    # one that does not, what is stored is "".
    info.do_not_save = True
    return info


def _section() -> typing.Tuple[str, str]:
    """The extension's own settings section, borrowed rather than reinvented.

    Importing ``minipaint_neo.settings`` here and not at module import time
    keeps this module loadable without a Forge on the path, which is what lets
    the WanGP half be exercised on its own.
    """
    try:
        from .. import settings as minipaint_settings

        return minipaint_settings.SECTION
    except Exception:
        return ("minipaint_canvas", "miniPaint / Canvas")


def _category(shared: typing.Any, name: str) -> typing.Optional[str]:
    """Only claim a settings category the host actually has - as in
    ``minipaint_neo.settings``, whose behaviour this must match exactly or the
    two halves land in different places."""
    try:
        from modules import options

        mapping = getattr(getattr(options, "categories", None), "mapping", None)
        if mapping and name in mapping:
            return name
    except Exception:
        pass
    return None


def on_ui_settings() -> None:
    """Register the one entry, and never be the reason Settings breaks.

    Mini Paint's own options are registered by ``minipaint_neo.settings`` and
    are not touched here; this adds one more to the same section. A failure is
    a missing paragraph on a settings page, so it is printed and swallowed -
    taking the page down would also take down the way to change frontends.
    """
    from modules import shared

    try:
        shared.opts.add_option(MANAGE_KEY, option_info(shared))
    except Exception as error:  # pragma: no cover - depends on the host
        print(f"{_LOG_PREFIX} could not register the {MANAGE_KEY} settings entry ({error})")
