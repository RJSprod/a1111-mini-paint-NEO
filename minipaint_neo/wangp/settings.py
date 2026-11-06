"""Settings -> WanGP: the integration's own page on Forge's Settings, and its management.

The user asked for it (2026-11-06): "give wan2gp its own settings menu option as
well, for integration management. I dont want integration management to appear
below the wan2gp frame when the frame is fully loaded in view, the whole tab
should now be the frame". So the WanGP tab is the frame, and Reinitialize, the
emergency restart, the diagnostic report and the console - the accordion that
sat under the frame - are a section of Forge's Settings page of their own,
*WanGP*, beside *miniPaint / Canvas*.

Still one entry, and still deliberately not a switch. Reinitialize and Restart
are actions, not values: Forge's settings system stores values - every control
on that page is bound to a key written to config.json when Apply is pressed - so
a checkbox called "Reinitialize WanGP integration" would either stay on forever
and reinitialize on every restart, or have to be reset behind the user's back.
Section 49.7 names that shape and rules it out.

What is registered is one option that stores nothing (``do_not_save``, the flag
Forge's own ``OptionHTML`` sets), whose ``component`` Forge calls inside the
section to draw it: ``minipaint_neo.wangp.ui.build_settings_panel`` builds the
notice and the management panel there, with its buttons and their events, and
hands Forge back the notice - an HTML block holding this module's sentence, the
value Apply reads and never stores. A host whose Settings page cannot build it
gets the notice alone, as before.

The wizard's own fields are not here and must not be added here: duplicating
them would give a user two places to set a WanGP root and no way to know which
one the process was launched from. Setup stays in the WanGP tab, which shows it
until the integration is set up.
"""

from __future__ import annotations

import typing

from .. import scrub

#: The one key. Named for what the entry is - the management surface - rather
#: than for an action, because the option itself performs none.
MANAGE_KEY = "minipaint_wangp_manage"

#: WanGP's own section of Forge's Settings page: its own entry in the list of
#: sections, beside miniPaint / Canvas.
SECTION = ("minipaint_wangp", "WanGP")

_LOG_PREFIX = "MiniPaint WanGP:"

#: What Reinitialize does and, more usefully, what it leaves alone. The same
#: promise is repeated on the tab's own confirmation, because this is the text
#: a user reads before deciding and the tab's is the text they read while
#: deciding.
NOTICE_HTML = (
    "<b>WanGP integration.</b> Reinitialize, the emergency restart, the diagnostic report "
    "and the console are here; setting up - which WanGP, which environment, which GPU - is "
    "done in the <b>WanGP</b> tab, which shows it until the integration is set up, so there "
    "is one place that knows what the running process was started from. "
    '<a href="#" id="minipaint_wangp_settings_link" '
    "onclick=\"if(window.minipaintWanGP&&window.minipaintWanGP.switchToWanGP){window.minipaintWanGP.switchToWanGP();}return false;\""
    ">Open the WanGP tab</a>."
    "<br>Reinitialize only forgets which WanGP installation, environment and GPU this "
    "extension was pointed at. It does not uninstall WanGP, and it deletes no models, "
    "no LoRAs, no presets, no outputs and no unrelated plugins."
    "<br>Where a queued WanGP job takes its settings from - what the WanGP tab shows, or "
    "WanGP's saved settings for the model (or its defaults) - is chosen in "
    "<b>Clipboard → Menu → WanGP Settings</b>."
)


def option_info(shared: typing.Any) -> typing.Any:
    """The one entry: Settings -> WanGP's notice and, below it, the management panel.

    ``component`` is how Forge draws an option: it calls it inside the option's
    section with the label, the value and the element id (``create_setting_component``
    in its ``modules/ui_settings.py``), so the panel built by :func:`panel` is
    part of that section and its events are the Settings page's. ``OptionHTML``
    is the same shape with ``gr.HTML`` as the component; this is that, with the
    panel under the HTML. ``do_not_save`` is set by hand, as ``OptionHTML``
    sets it: on a host that honours it nothing is stored, and on one that does
    not, what is stored is the notice - a string, never an instruction.
    """
    info = shared.OptionInfo(
        NOTICE_HTML,
        "",
        panel,
        section=SECTION,
        category_id=_category(shared, "ui"),
    )
    info.do_not_save = True
    return info


def panel(**kwargs: typing.Any) -> typing.Any:
    """Draw Settings -> WanGP where Forge asks for the option's component.

    The notice and the management panel (``ui.build_settings_panel``), or - if
    building the panel fails for any reason - the notice alone, the way Forge's
    own ``OptionHTML`` draws one: a Settings page that cannot be drawn would
    also take down the way to change frontends.
    """
    import gradio as gr

    try:
        from . import ui

        return ui.build_settings_panel(**kwargs)
    except Exception as error:  # pragma: no cover - depends on the host
        scrub.console(f"could not build Settings -> WanGP ({type(error).__name__}); "
                      "showing its notice alone", _LOG_PREFIX)
        kwargs.pop("label", None)
        return gr.HTML(elem_classes="settings-info", **kwargs)


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

    Mini Paint's own options are registered by ``minipaint_neo.settings`` in
    *miniPaint / Canvas* and are not touched here; this is WanGP's own section.
    A failure is a missing section on a settings page, so it is printed and
    swallowed - taking the page down would also take down the way to change
    frontends.
    """
    from modules import shared

    try:
        shared.opts.add_option(MANAGE_KEY, option_info(shared))
    except Exception as error:  # pragma: no cover - depends on the host
        scrub.console(f"could not register the {MANAGE_KEY} settings entry ({error})", _LOG_PREFIX)
