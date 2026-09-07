"""The WanGP tab: one tab, built once, that changes what it shows and never
what it is.

The tab is a shell around four containers - the setup wizard, the starting
card, the error surface and the iframe - and exactly one of them is visible at
a time. They are all created during UI construction and none of them is ever
destroyed, because this repository has already been through a tab that
rebuilt part of itself and took the rest of the WebUI's component tree with
it. ``router.py`` learned that lesson for the Mini Paint tab; this module
applies the same two rules: build inside a guard that puts Gradio's build
context back if anything raises, and if the real tab cannot be built, hand
back a tab that says why under the same label and the same id. A tab that is
missing is a tab whose id other things stop finding.

Before setup there is a wizard, not a broken iframe. The wizard is the five
questions of section 8 and it ends in a checklist, not a "Finish" button that
means "I hope so": ``checklist`` turns a bag of observations into pass/fail
rows, ``mandatory_pass`` is the only thing that can enable Finish, and the
Finish handler asks it again rather than trusting the button's own enabled
state. ``initialized=true`` is written by exactly one path - write_pending
then promote_pending - so a setup that half-succeeded leaves the previous one
intact.

Startup is lazy and never blocking. Opening the tab may ask for WanGP; the
request returns immediately and the card offers "check again", because a
Gradio event that waits five minutes for torch to load is an event that has
already timed out somewhere. The Send menu may use ``warm_up`` for the same
purpose, and ``warm_up`` deliberately returns no receivers: a receiver is a
property of a live WanGP page, and inventing one for a process that just
started would be sending an image into a slot nobody has proved exists.

The iframe's ``src`` is ``/wan2gp/`` - a path, on the Forge origin, with no
host and no port in it. The backend port is not in this file, is not in the
state textbox, and is not in anything rendered here. The browser gets its
channel id from a hidden textbox instead, so the identifier that correlates
messages and the URL that carries traffic stay two separate things.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import re
import json
import threading
import traceback
import typing

import gradio as gr

from .. import paths
from . import bridge, config, diagnostics, discovery, errors, journal, runtime

TAB_LABEL = "WanGP"
TAB_ID = "wangp"

#: The four containers of section 7, plus the seams the browser half needs.
#: Ids are stable API: ``javascript/minipaint_wangp.js`` addresses them and a
#: rename is a break, not a tidy-up.
SETUP_ROOT_ID = "wangp_setup_root"
STARTING_ROOT_ID = "wangp_starting_root"
ERROR_ROOT_ID = "wangp_error_root"
IFRAME_ROOT_ID = "wangp_iframe_root"
IFRAME_ELEM_ID = "wangp_iframe"
CHANNEL_ELEM_ID = "wangp_channel"
STATE_ELEM_ID = "wangp_state"
OPEN_ELEM_ID = "wangp_open_request"
REFRESH_ELEM_ID = "wangp_refresh_request"
BROWSER_CHECK_ELEM_ID = "wangp_browser_check"
SESSION_ELEM_ID = "wangp_session"
CLIENT_LOG_ELEM_ID = "wangp_client_log"

#: The only URL the browser is ever given for WanGP. A path, so it resolves
#: against the Forge origin the page is already on; it must stay equal to
#: ``proxy.PROXY_PREFIX``, which is the same decision written at the other end.
PUBLIC_PATH = "/wan2gp/"

#: Which container is showing.
VIEW_SETUP = "setup"
VIEW_STARTING = "starting"
VIEW_ERROR = "error"
VIEW_IFRAME = "iframe"

#: The internal states of section 7, reported to the browser in the state
#: textbox so the Send menu can tell "not set up" from "not started".
STATE_SETUP_REQUIRED = "SETUP_REQUIRED"
STATE_STARTING = "STARTING"
STATE_READY = "READY"
STATE_DEGRADED = "DEGRADED"
STATE_ERROR = "ERROR"
STATE_STOPPED = "STOPPED"

#: What the error surface may offer. Section 31 decides which, per code.
ACTION_SETUP = "setup"
ACTION_RESTART = "restart"
ACTION_REINITIALIZE = "reinitialize"

#: Where the bridge plugin we install comes from.
BRIDGE_SOURCE_DIR = paths.root_path / "wan2gp_bridge"

_LOG_PREFIX = "MiniPaint WanGP:"

#: The promise section 36 makes, in the words a user reads before agreeing.
#: The list of things it does *not* touch is the point of the sentence.
REINITIALIZE_NOTICE = (
    "**Reinitialize forgets the setup, not your work.**\n\n"
    "It stops the WanGP that this extension started, forgets which installation, "
    "which Python environment and which GPU it was pointed at, keeps a copy of that "
    "setup so it can be restored, and brings this tab back to the wizard.\n\n"
    "It does **not** uninstall WanGP, and it deletes no models, no LoRAs, no presets, "
    "no outputs, no settings of yours and no plugin other than by installing a newer "
    "copy of this extension's own bridge if you ask the new setup to.\n\n"
    "The WanGP that is running now will stop."
)

#: The rows of section 8.5, in the order they are checked. The second element
#: is what the row says; whether it passed, and whether it is mandatory, is
#: decided by :func:`checklist` from what was actually observed.
CHECKS: typing.Tuple[typing.Tuple[str, str], ...] = (
    ("loopback", "WanGP is launched bound to 127.0.0.1 only"),
    ("no_listen", "--listen is absent from the command line"),
    ("no_share", "--share is absent from the command line"),
    ("no_wildcard", "nothing binds 0.0.0.0"),
    ("gpu", "the selected GPU is present and is the only one WanGP will see"),
    ("port", "a loopback port was assigned to this run"),
    ("root_path", "WanGP serves itself under /wan2gp"),
    ("bridge", "the MiniPaint bridge plugin is installed and switched on"),
    ("proxy_base", "the WanGP page loads through the proxy"),
    ("proxy_asset", "a WanGP asset loads through the proxy"),
    ("bridge_round", "the bridge answered through the proxied iframe"),
    ("origin", "the browser only ever talks to this Forge origin"),
    ("auth", "/wan2gp/ is covered by this Forge's sign-in"),
)

#: The one row that is only mandatory when there is something for it to be
#: mandatory about: a Forge with no authentication of its own cannot fail to
#: cover a route, and demanding coverage there would block every setup.
CONDITIONAL_CHECKS = frozenset({"auth"})

#: How often the wizard asks again while WanGP is starting. Long enough not to
#: be a spinner made of round trips, short enough that a model finishing its
#: load is noticed rather than waited out.
POLL_SECONDS = 3.0

#: What to do about a row that has not passed. A red row states a fact; this
#: says what to go and do about it, which is the part somebody staring at the
#: wizard actually needs. Shown folded, only on rows that failed, because a
#: checklist that explains thirteen passing things is a wall of text.
HELP: typing.Dict[str, str] = {
    "loopback": (
        "This reads the command line WanGP would be launched with, so it needs steps 1 and 2 "
        "answered first. If step 2 is not green, press **Try it against this WanGP** there - "
        "choosing an environment in the dropdown is not the same as proving it can run WanGP."
    ),
    "no_listen": "Same as the row above: it reads the launch command, which needs steps 1 and 2 green.",
    "no_share": "Same as the row above: it reads the launch command, which needs steps 1 and 2 green.",
    "no_wildcard": "Same as the row above: it reads the launch command, which needs steps 1 and 2 green.",
    "gpu": (
        "Press **List the GPUs** in step 3 and choose one. If the card you want is not listed, "
        "this extension cannot see it: check `nvidia-smi` runs in a terminal. A GPU that was "
        "chosen before and has since gone is never quietly swapped for another one."
    ),
    "port": (
        "This turns green once WanGP is actually running, because the port is assigned to a real "
        "process. **You do not start WanGP yourself - this extension starts it for you**, on the "
        "first press of *Run the checks*. A cold start loads a model and can take minutes, so "
        "press *Run the checks* again after a while. If it never goes green, the row will say "
        "what went wrong instead of this."
    ),
    "root_path": (
        "WanGP has to be running for this. It is started for you; give it time and press "
        "**Run the checks** again. If WanGP has crashed, the reason appears on this row and the "
        "full output is in the diagnostic report at the bottom of this tab."
    ),
    "bridge": (
        "Press **Check the bridge plugin** in step 4, and **Install or update it** if it is "
        "missing or not listed. Two things have to be true, and copying the folder is only the "
        "first: WanGP finds plugins by scanning `plugins/`, but it only *loads* the ones named "
        "in `enabled_plugins` in `wgp_config.json`. **Install or update it** does both, adding "
        "our folder to that list and leaving every other plugin and setting alone. WanGP builds "
        "its plugin list at startup, so it is stopped for you and starts again with the plugin "
        "loaded on the next **Run the checks**."
    ),
    "proxy_base": (
        "This fetches the WanGP page through this Forge, so WanGP has to be up first. Wait for "
        "the rows above to go green and press **Run the checks** again."
    ),
    "proxy_asset": "Same as the row above: it needs WanGP serving, then another press of **Run the checks**.",
    "bridge_round": (
        "The WanGP page below has to load and its bridge plugin has to answer. If the rows above "
        "are green and this one is not, the usual cause is that WanGP is running without the "
        "plugin: it is only loaded when `wgp_config.json` lists it in `enabled_plugins` **and** "
        "WanGP has started since. Press **Install or update it** in step 4 - that adds it to the "
        "list and stops WanGP - then press **Run the checks** to start it again."
    ),
    "origin": (
        "The page below is loaded from a path on this Forge, never from WanGP's own address. If "
        "this row is red the tab has not drawn the WanGP frame yet; finish the rows above."
    ),
    "auth": (
        "This Forge has a sign-in, and this code cannot see whether it covers `/wan2gp/`: Gradio "
        "checks a login per route, and a route added by an extension is invisible to that check "
        "either way. So somebody has to look. Sign out of Forge, open `/wan2gp/` in a private "
        "window, and confirm it refuses you the way the rest of Forge does. Then tick the box "
        "under this list. Until it is ticked `/wan2gp/` serves nothing at all - WanGP has no "
        "sign-in of its own, so if that page opened for a stranger they could drive it."
    ),
}


# ------------------------------------------------------------------ views --


def decide_view(
    setup_codes: typing.Optional[typing.Sequence[str]],
    snapshot: typing.Optional[dict],
    start_requested: bool = False,
) -> dict:
    """Which container to show, and what it should say.

    Pure, and the whole of the tab's decision-making: the Gradio handlers do
    nothing but collect ``setup_codes`` from the config and ``snapshot`` from
    the runtime, hand them here, and paint what comes back.

    The order of the questions is the order of the answers a user can act on.
    Setup first, because nothing else is answerable without it. Then a config
    that is initialized but no longer describes reality, which is an error and
    not a wizard - the wizard would ask the same questions and get the same
    answers. Then whatever the process is doing.
    """
    codes = [code for code in (setup_codes or []) if code]
    snapshot = snapshot or {}
    state = str(snapshot.get("state") or STATE_STOPPED)
    error_code = str(snapshot.get("error_code") or "")

    if errors.SETUP_REQUIRED in codes:
        return _view(VIEW_SETUP, STATE_SETUP_REQUIRED, errors.SETUP_REQUIRED, [ACTION_SETUP])
    if codes:
        return _view(VIEW_ERROR, STATE_ERROR, codes[0], error_actions(codes[0]))

    if error_code:
        # A bridge complaint against a WanGP that is still serving pages is
        # the DEGRADED state: the iframe is genuinely usable, and it is only
        # the intelligent Send that has to be switched off.
        if snapshot.get("port_bound") and error_code in (
            errors.BRIDGE_VERSION_MISMATCH,
            errors.BRIDGE_COMPONENT_INCOMPATIBLE,
        ):
            view = _view(VIEW_IFRAME, STATE_DEGRADED, error_code, error_actions(error_code))
            view["degraded"] = True
            return view
        return _view(VIEW_ERROR, STATE_ERROR, error_code, error_actions(error_code))

    if state == runtime.READY:
        return _view(VIEW_IFRAME, STATE_READY, "", [])
    if state == runtime.STARTING or start_requested:
        return _view(VIEW_STARTING, STATE_STARTING, "", [])
    return _view(VIEW_STARTING, STATE_STOPPED, "", [])


def _view(view: str, state: str, code: str, actions: typing.Sequence[str]) -> dict:
    return {
        "view": view,
        "state": state,
        "code": code,
        "message": errors.message(code) if code else "",
        "actions": list(actions),
        "degraded": False,
    }


def error_actions(code: str) -> typing.List[str]:
    """What the error surface offers for a code - section 31, exactly.

    ``GPU_UUID_MISSING`` is in ``REINIT_CODES`` and so lands on Reinitialize,
    which is the point: there is no "start anyway" here and there must not be
    one, because the only thing "anyway" could mean is another GPU.
    """
    if code == errors.SETUP_REQUIRED:
        return [ACTION_SETUP]
    if code in errors.REINIT_CODES:
        return [ACTION_REINITIALIZE]
    if code == errors.AUTH_BOUNDARY_FAILED:
        # Restarting changes nothing about who can reach the route, and the
        # integration deliberately stayed off. There is nothing to offer.
        return []
    return [ACTION_RESTART]


def setup_codes() -> typing.List[str]:
    """The config's own verdict on itself, as codes the views understand."""
    try:
        active = config.load()
    except errors.IntegrationError as error:
        return [error.code]
    if active is None:
        return [errors.SETUP_REQUIRED]
    return active.validate()


def current_view() -> dict:
    try:
        snapshot = runtime.current().health()
    except Exception:
        snapshot = {}
    return decide_view(setup_codes(), snapshot, start_requested=_start_pending())


# ------------------------------------------------------------- the iframe --


def iframe_html(channel_id: str = "") -> str:
    """The one iframe, pointed at the public path and nothing else.

    ``channel_id`` is not put in the URL, in a query string or in a fragment;
    it is here only so the element can be found to carry no destination but
    ``/wan2gp/``. The browser reads the channel from its own hidden textbox,
    which keeps "which session is this" and "where do the bytes go" as two
    separate facts that cannot be conflated by a copied link.
    """
    # The size is inline rather than in style.css because style.css belongs to
    # the Mini Paint tab: a WanGP that only fills its frame when a stylesheet
    # in another part of the extension has loaded is a WanGP that is 150 pixels
    # tall on the day that file is edited. The class is still there for anyone
    # who wants to restyle it.
    return (
        f'<iframe id="{IFRAME_ELEM_ID}" class="minipaint-wangp-frame" title="WanGP" '
        f'src="{html.escape(PUBLIC_PATH, quote=True)}" '
        'style="width:100%;height:80vh;min-height:480px;border:0;display:block" '
        'referrerpolicy="same-origin" allow="clipboard-read; clipboard-write; fullscreen">'
        "</iframe>"
    )


def _new_channel(snapshot: dict) -> str:
    """A channel id for this page's iframe load, or an empty string.

    Minted per event rather than at build time on purpose: a value baked into
    the component would be the same one in every browser tab, and section 13
    is built on one channel per page.
    """
    instance_id = str((snapshot or {}).get("instance_id") or "")
    if not instance_id:
        return ""
    try:
        return str(bridge.new_channel(instance_id).get("channel_id") or "")
    except errors.IntegrationError:
        return ""


def _keep_or_mint(channel: typing.Any) -> str:
    """This page's channel if it still describes the running WanGP, else a new
    one.

    The binding that matters is channel-to-instance: a channel from before a
    restart names a run that no longer exists, and the registry has already
    dropped it. Reusing one that survived is what keeps a repaint from
    orphaning a session; refusing to reuse one that did not is what keeps a
    page from talking about a WanGP that is gone.
    """
    snapshot = runtime.snapshot()
    existing = str(channel or "")
    if existing:
        try:
            session = bridge.registry().get(existing)
        except Exception:
            session = None
        if session is not None and session.instance_id == snapshot.get("instance_id"):
            return existing
    return _new_channel(snapshot)


# ----------------------------------------------------------- lazy startup --

_launch_lock = threading.Lock()
_launch: typing.Dict[str, typing.Any] = {"thread": None}


def _start_pending() -> bool:
    thread = _launch.get("thread")
    return bool(thread is not None and thread.is_alive())


def _launch_now(active: typing.Any) -> None:
    try:
        runtime.start(active)
    except errors.IntegrationError as error:
        print(f"{_LOG_PREFIX} WanGP did not start: {error.code} ({error.detail})")
    except Exception:  # pragma: no cover - the runtime turns its own into codes
        traceback.print_exc()
        with contextlib.suppress(Exception):
            runtime.mark(errors.INTERNAL_ERROR, "the launch raised an unexpected error")


def request_start(active: typing.Optional[typing.Any] = None) -> dict:
    """Ask for WanGP and return at once, whatever happens next.

    Section 11.1 allows opening the tab to start the process; nothing allows
    a UI event to sit still for the several minutes a cold model load takes.
    So the launch runs on its own thread and every caller gets today's
    snapshot - the tab then offers "check again" rather than a spinner that
    outlives the request that drew it.

    ``active`` lets the wizard start a candidate config that is not the active
    one yet, which is how the last validation step proves a setup before
    anything is promoted.
    """
    with _launch_lock:
        if _start_pending():
            return runtime.snapshot()

        candidate = active
        if candidate is None:
            try:
                candidate = config.load()
            except errors.IntegrationError:
                return runtime.snapshot()
        if candidate is None:
            return runtime.snapshot()

        # A config whose shape is wrong cannot start anything, and asking the
        # runtime to prove that again would only produce the same code from
        # further away. SETUP_REQUIRED is excluded because a candidate config
        # is not initialized yet by definition.
        if [code for code in candidate.validate() if code != errors.SETUP_REQUIRED]:
            return runtime.snapshot()

        thread = threading.Thread(
            target=_launch_now, args=(candidate,), name="minipaint-wangp-start", daemon=True
        )
        _launch["thread"] = thread
        thread.start()
    return runtime.snapshot()


def warm_up() -> dict:
    """What the Send menu may ask for: a nudge, and an honest answer.

    It starts WanGP if it is not running and returns immediately. It reports
    whether a live iframe session exists, and it never returns a receiver -
    not even a plausible one. A receiver belongs to a WanGP page that has said
    what it can accept; until such a page exists the menu's answer is
    ``IFRAME_NOT_READY``, which the Send menu renders as "open the WanGP tab
    and choose an input".
    """
    snapshot = request_start()
    try:
        sessions = bridge.registry().snapshot()
    except Exception:
        sessions = {}

    live = int(sessions.get("sessions", 0) or 0) > 0
    code = "" if live else errors.IFRAME_NOT_READY
    return {
        "state": snapshot.get("state", runtime.STOPPED),
        "ready": bool(snapshot.get("running")),
        "starting": _start_pending() or snapshot.get("state") == runtime.STARTING,
        "live_session": live,
        "code": snapshot.get("error_code") or code,
        "message": errors.message(snapshot.get("error_code") or code) if (snapshot.get("error_code") or code) else "",
    }


# --------------------------------------------------------- reinitializing --


def reinitialize() -> dict:
    """Section 36, in the order that keeps every step true when the next one
    fails.

    Stop the child first, because everything after it describes a process that
    should no longer exist. Then drop every browser session, so a page holding
    a receiver list for the run that just ended cannot act on it. Then the
    config: ``clear`` copies the active setup into the backup and marks the
    active one incomplete, which is what brings the tab back to the wizard and
    what leaves "restore the previous working integration" something to
    restore. Nothing is deleted from anybody's WanGP.
    """
    steps: typing.List[str] = []

    try:
        runtime.stop()
        steps.append("the WanGP this extension started was stopped")
    except Exception as error:
        steps.append(f"WanGP could not be stopped cleanly ({error})")

    try:
        # An empty instance id means every session, which is right here: after
        # a stop there is no run for any page to be bound to.
        bridge.registry().invalidate_instance("")
        steps.append("every browser session and receiver list was invalidated")
    except Exception as error:
        steps.append(f"the browser sessions could not be invalidated ({error})")

    try:
        config.clear()
        steps.append("the setup was copied to the backup and marked incomplete")
    except Exception as error:
        return {"ok": False, "steps": steps + [f"the setup could not be cleared ({error})"]}

    return {"ok": True, "steps": steps}


def restore_previous() -> dict:
    """Put the last working setup back, then make it prove itself again.

    Section 8.6 is explicit that restoring revalidates: a backup is a record
    of what worked once, not a promise that the folder, the environment and
    the card are still there.
    """
    try:
        restored = config.restore_backup()
    except errors.IntegrationError as error:
        return {"ok": False, "code": error.code, "message": error.user_message}
    if restored is None:
        return {"ok": False, "code": errors.SETUP_REQUIRED, "message": "There is no previous setup to restore."}

    codes = restored.validate()
    codes += [code for code in discovery.validate_root(restored.wangp_root) if code not in codes]
    if discovery.interpreter_for(restored.runtime) is None and errors.RUNTIME_MISSING not in codes:
        codes.append(errors.RUNTIME_MISSING)
    if codes:
        return {"ok": False, "code": codes[0], "message": errors.message(codes[0])}
    return {"ok": True, "code": "", "message": "The previous setup was restored and still checks out."}


# ------------------------------------------------------------- checklist ---


def command_checks(command: typing.Sequence[str]) -> typing.Dict[str, bool]:
    """The four network facts of section 8.5, read off the real argv.

    Read off the command that would actually be run, rather than asserted
    about the code that builds it: this is the check that survives somebody
    adding a flag in the future.

    No command means no answer, so all four fail. "``--listen`` is absent from
    an argv that could not be built" is true and worthless, and a green row
    for it would be the checklist saying yes to a question it never asked.
    """
    items = [str(item).strip().lower() for item in command or []]
    if not items:
        return {"loopback": False, "no_listen": False, "no_share": False, "no_wildcard": False}
    joined = " ".join(items)
    return {
        "loopback": "--server-name" in items and runtime.LOOPBACK in items,
        "no_listen": "--listen" not in items,
        "no_share": "--share" not in items,
        "no_wildcard": "0.0.0.0" not in joined,
    }


def checklist(observations: typing.Optional[dict]) -> typing.List[dict]:
    """The section 8.5 rows, as pass/fail with a reason each.

    Pure, and the only thing that decides whether a setup may be written.
    Anything not observed is a failure rather than an omission: the wizard
    starts with every row failed and rows turn green only when something
    proved them, which is what makes "not checked yet" and "checked and
    wrong" the same answer at the point where it matters.
    """
    seen = observations if isinstance(observations, dict) else {}
    command = command_checks(seen.get("command") or [])
    auth = seen.get("auth") if isinstance(seen.get("auth"), dict) else {}

    values = {
        "loopback": command["loopback"],
        "no_listen": command["no_listen"],
        "no_share": command["no_share"],
        "no_wildcard": command["no_wildcard"],
        "gpu": bool(seen.get("gpu_present")),
        "port": bool(seen.get("port_bound")),
        "root_path": bool(seen.get("root_path_ok")),
        "bridge": not seen.get("bridge_code", errors.BRIDGE_MISSING),
        "proxy_base": bool(seen.get("proxy_base_ok")),
        "proxy_asset": bool(seen.get("proxy_asset_ok")),
        "bridge_round": bool(seen.get("bridge_round_trip")),
        "origin": _origin_is_local(seen.get("iframe_src", "")),
        "auth": bool(auth.get("ok")),
    }
    details = seen.get("details") if isinstance(seen.get("details"), dict) else {}

    rows = []
    for key, label in CHECKS:
        mandatory = key not in CONDITIONAL_CHECKS or bool(seen.get("host_auth"))
        rows.append(
            {
                "key": key,
                "label": label,
                "ok": bool(values.get(key)),
                "mandatory": mandatory,
                "detail": str(details.get(key, "")),
            }
        )
    return rows


def _origin_is_local(src: typing.Any) -> bool:
    """Is what the iframe was given a path on this origin, and only that."""
    text = str(src or "")
    return bool(text) and text.startswith("/") and not text.startswith("//") and "://" not in text


def mandatory_pass(rows: typing.Optional[typing.Sequence[dict]]) -> bool:
    """Every mandatory row green. The only gate on ``initialized=true``."""
    rows = list(rows or [])
    if not rows:
        return False
    return all(row.get("ok") for row in rows if row.get("mandatory"))


def _help_html(key: str) -> str:
    """The fold-out "what do I do about this" for one failed row.

    A ``<details>`` rather than anything scripted: it is one element, it works
    inside a Gradio HTML block with no JavaScript of ours in the page, and it
    stays folded so a list of thirteen rows does not become an essay.
    """
    guidance = HELP.get(key)
    if not guidance:
        return ""
    # Markdown-ish emphasis, kept to the one form the help text uses.
    body = html.escape(guidance).replace("**", "\u0000")
    pieces = body.split("\u0000")
    rebuilt = "".join(
        piece if index % 2 == 0 else f"<b>{piece}</b>" for index, piece in enumerate(pieces)
    )
    rebuilt = re.sub(r"`([^`]+)`", r"<code>\1</code>", rebuilt)
    return (
        '<details class="minipaint-wangp-help">'
        "<summary>What to do</summary>"
        f"<div>{rebuilt}</div>"
        "</details>"
    )


def checklist_html(rows: typing.Optional[typing.Sequence[dict]]) -> str:
    """The checklist as the list of pass/fail rows section 8.5 asks for.

    A failed row also carries its own folded note saying what to do about it.
    Only a failed one: a green row needs no advice, and the wizard is already
    a long page.
    """
    items = []
    for row in rows or []:
        ok = bool(row.get("ok"))
        mark = "&#10003;" if ok else "&#10007;"
        tone = "minipaint-wangp-pass" if ok else "minipaint-wangp-fail"
        optional = "" if row.get("mandatory") else " <em>(only required when this Forge has a sign-in)</em>"
        detail = f" <span class=\"minipaint-wangp-detail\">- {html.escape(str(row['detail']))}</span>" if row.get("detail") else ""
        guidance = "" if ok else _help_html(str(row.get("key", "")))
        items.append(
            f'<li class="{tone}"><b>[{mark}]</b> {html.escape(str(row.get("label", "")))}{optional}{detail}{guidance}</li>'
        )
    body = "".join(items) or "<li>nothing has been checked yet</li>"
    return f'<ul class="minipaint-wangp-checklist">{body}</ul>'


# --------------------------------------------------------- the candidate ---


def config_from_wizard(candidate: typing.Optional[dict], initialized: bool = False) -> config.Config:
    """The wizard's four answers as a config document, and nothing else.

    Written through ``config.Config`` rather than as a dict so that the
    whitelist in ``config.as_dict`` is the only thing that decides what
    reaches the file - including for the pending copy, which is a real config
    file and gets the same treatment as the active one.
    """
    answers = candidate if isinstance(candidate, dict) else {}
    runtime_entry = answers.get("runtime") if isinstance(answers.get("runtime"), dict) else {}
    return config.Config(
        schema_version=config.SCHEMA_VERSION,
        initialized=bool(initialized),
        wangp_root=str(answers.get("root") or ""),
        runtime={
            "type": str(runtime_entry.get("type") or ""),
            "prefix": str(runtime_entry.get("prefix") or ""),
            "display_name": str(runtime_entry.get("display_name") or ""),
            "launch_strategy": str(runtime_entry.get("launch_strategy") or ""),
        },
        gpu={"uuid": str(answers.get("gpu_uuid") or "")},
        integration={
            "proxy_path": config.DEFAULT_PROXY_PATH,
            "auto_start": config.AUTO_START_LAZY,
            "auth_checked": bool(answers.get("auth_checked")),
        },
    )


def record_client_log(text: typing.Any) -> None:
    """One line the browser wants in the page's log. Never raises.

    The browser is a writer here like any other, so what it says is trimmed
    and scrubbed on the way in rather than trusted - it reaches a box a user
    is invited to copy into a bug report.
    """
    try:
        payload = json.loads(text) if isinstance(text, str) and text.strip() else None
        if isinstance(payload, dict) and payload.get("line"):
            journal.note("browser", payload["line"])
    except Exception:
        return


def record_session(channel: typing.Any, text: typing.Any) -> None:
    """Take down what the iframe last said about itself.

    The browser is the only half that ever hears the bridge, so this is the
    only way the Forge side learns that a page has a live WanGP session, which
    model it is on and what it will accept. Nothing here decides anything: the
    receiver list is answered by the live iframe at the moment the menu opens,
    and the gate that refuses a stale or unknown receiver runs inside WanGP
    next to the components. This is the record, for the diagnostics report and
    for keeping a channel across a repaint - so it is also written to be unable
    to fail, because a bookkeeping error must never cost a send.
    """
    try:
        payload = json.loads(text) if isinstance(text, str) and text.strip() else None
        if not isinstance(payload, dict):
            return
        registry = bridge.registry()
        if payload.get("introducing"):
            registry.hello(channel, payload.get("instance_id"))
            registry.ready(channel, payload)
        else:
            registry.receivers(channel, payload)
    except errors.IntegrationError as error:
        # A named refusal is worth knowing about - a plugin of the wrong
        # protocol is exactly the case section 31 wants reported as itself
        # rather than as an empty menu - but it is still only bookkeeping, so
        # it is recorded and never raised at the browser.
        if error.code == errors.BRIDGE_VERSION_MISMATCH:
            runtime.current().mark(error.code, error.detail)
    except Exception:
        return


def parse_browser_check(text: typing.Any) -> dict:
    """What the browser reported about the round trip through the iframe.

    ``javascript/minipaint_wangp.js`` writes a small JSON object into the
    hidden textbox after it has sent a HELLO into the proxied iframe and had a
    READY come back. Anything that is not that object is read as "nothing was
    reported", because a half-understood answer to "did the bridge answer" has
    to count as no.
    """
    try:
        payload = json.loads(str(text or ""))
    except ValueError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        "round_trip": bool(payload.get("round_trip")),
        "channel": str(payload.get("channel") or ""),
        "origin": str(payload.get("origin") or ""),
        "detail": str(payload.get("detail") or "")[:200],
    }


def _probe_proxy() -> dict:
    """The proxy's own step-by-step health, from a synchronous caller.

    Gradio runs these handlers on a worker thread, so there is no loop to
    schedule onto; a private one is opened and closed for the probe rather
    than left behind for a later call to trip over.
    """
    from . import proxy

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(proxy.probe())
    finally:
        with contextlib.suppress(Exception):
            loop.close()


_app: typing.Dict[str, typing.Any] = {"app": None}


def remember_app(app: typing.Any) -> None:
    """Keep the FastAPI app the proxy was installed on, for one question only.

    The authentication-coverage row of section 8.5 can only be answered by
    looking at the application object, and ``on_app_started`` is the one place
    it is handed to an extension. Nothing else is done with it.
    """
    _app["app"] = app


def _auth_report(acknowledged: bool = False) -> dict:
    app = _app.get("app")
    if app is None:
        return {}
    try:
        from . import proxy

        report = proxy.auth_boundary_report(app)
        if not report.get("ok") and acknowledged:
            return dict(report, ok=True, coverage="declared_by_operator",
                        detail="the checkbox in step 5 says this was checked with an unauthenticated request.")
        if not report.get("ok") and proxy.auth_override():
            # The gate honours this, so the checklist has to as well: an
            # operator who ran the unauthenticated request themselves and set
            # the variable has answered the one question this code cannot.
            report = dict(report, ok=True, coverage="declared_by_operator",
                          detail=f"{proxy.AUTH_OVERRIDE_ENV} is set: coverage was checked outside this process.")
        return report
    except Exception:
        return {}


def _why_not_serving(snapshot: typing.Optional[dict]) -> str:
    """One sentence saying why WanGP is not answering, from the runtime itself.

    The wizard starts WanGP on the user's behalf, so "it is not running" is
    never advice - the useful thing is whether it is still coming up, whether
    it fell over, and what it said on the way down.
    """
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    state = str(snapshot.get("state") or runtime.STOPPED)
    code = str(snapshot.get("error_code") or "")

    if code:
        sentence = errors.message(code)
        tail = [str(line) for line in (snapshot.get("stderr_tail") or []) if str(line).strip()]
        if tail:
            # The last thing the child said is nearly always the reason, and a
            # tail nobody can see is a tail nobody can act on.
            sentence = f"{sentence} WanGP's last output: {tail[-1][:200]}"
        return sentence

    if state == runtime.STARTING:
        return "WanGP is still starting - loading a model can take minutes. Press Run the checks again."
    if state == runtime.STOPPING:
        return "WanGP is shutting down; wait for it to stop, then press Run the checks again."
    if state == runtime.READY:
        return "WanGP is running but has not answered this yet."
    return "WanGP has not started yet. Press Run the checks - it is started for you, not by hand."


def observe(candidate: typing.Optional[dict], browser_text: typing.Any = "") -> dict:
    """Everything the checklist needs, collected once, from the real thing.

    Kept apart from ``checklist`` so the judging half stays pure: this is the
    half that touches the process, the driver, the network and the plugin
    folder, and it is the half a test replaces with a dictionary.
    """
    answers = candidate if isinstance(candidate, dict) else {}
    document = config_from_wizard(answers)
    details: typing.Dict[str, str] = {}

    try:
        # A placeholder port: this command is inspected, never run, and asking
        # for a real one would bind a socket to answer a question about flags.
        command = runtime.command_line(document, 1)
    except errors.IntegrationError as error:
        command = []
        # "No longer there" is the right sentence for a setup that used to work
        # and now does not. During the wizard it is simply wrong: nothing has
        # been chosen yet, and telling someone to set the integration up again
        # while they are setting it up is how they end up stuck on this page.
        unanswered = not str((answers.get("runtime") or {}).get("prefix") or "")
        sentence = (
            "no environment has been tried yet - choose one in step 2 and press "
            "\u201cTry it against this WanGP\u201d"
            if unanswered and error.code == errors.RUNTIME_MISSING
            else errors.message(error.code)
        )
        for key in ("loopback", "no_listen", "no_share", "no_wildcard"):
            details[key] = sentence
    except Exception as error:
        command = []
        details["loopback"] = str(error)

    uuid = str(answers.get("gpu_uuid") or "")
    try:
        device = discovery.find_gpu(uuid) if uuid else None
    except Exception:
        device = None
    if device is None:
        details["gpu"] = errors.message(errors.GPU_UUID_MISSING) if uuid else "no GPU has been chosen yet"

    bridge_code, bridge_detail = "", ""
    try:
        bridge_code, bridge_detail = discovery.bridge_status(
            answers.get("root") or "", diagnostics.shipped_bridge_version()
        )
    except Exception as error:
        bridge_code, bridge_detail = errors.BRIDGE_MISSING, str(error)
    details["bridge"] = bridge_detail

    try:
        snapshot = runtime.current().health()
    except Exception:
        snapshot = {}

    probe: typing.Dict[str, typing.Any] = {}
    if snapshot.get("state") == runtime.READY:
        try:
            probe = _probe_proxy()
        except Exception as error:
            details["proxy_base"] = str(error)
    else:
        # "Not serving yet" is true of a WanGP that is still loading a model
        # and of one that died on the way up, and the difference is the only
        # thing worth knowing. The runtime already has the answer - a state, a
        # code, and the tail of what the child printed - and dropping it here
        # was leaving people to press this button forever.
        because = _why_not_serving(snapshot)
        for key in ("port", "root_path", "proxy_base", "proxy_asset", "bridge_round"):
            details[key] = because

    steps = probe.get("steps") or {}
    for name, key in (("base_page", "proxy_base"), ("asset", "proxy_asset"), ("root_path", "root_path")):
        if name in steps and steps[name].get("detail"):
            details[key] = str(steps[name]["detail"])

    reported = parse_browser_check(browser_text)
    if not reported.get("round_trip") and snapshot.get("state") == runtime.READY:
        details["bridge_round"] = reported.get("detail") or "the WanGP page below has not answered yet"

    auth = _auth_report(bool(answers.get("auth_checked")))
    if auth and not auth.get("ok"):
        details["auth"] = str(auth.get("detail") or "")

    return {
        "command": command,
        "gpu_present": device is not None,
        "port_bound": bool(snapshot.get("port_bound")),
        "root_path_ok": bool(steps.get("root_path", {}).get("ok")),
        "proxy_base_ok": bool(steps.get("base_page", {}).get("ok")),
        "proxy_asset_ok": bool(steps.get("asset", {}).get("ok")),
        "bridge_code": bridge_code,
        "bridge_round_trip": bool(reported.get("round_trip")),
        "iframe_src": PUBLIC_PATH,
        "auth": auth,
        "host_auth": bool(auth.get("host_auth_configured")),
        "details": details,
    }


# ------------------------------------------------------------- the shell ---


@contextlib.contextmanager
def _keep_build_context():
    """Build a tab without letting a failure take the rest of the WebUI with it.

    The same guard as ``router._keep_build_context``, and duplicated rather
    than imported on purpose: ``router`` pulls in the Mini Paint frontends and
    the host's ``shared`` module, and the WanGP tab must neither depend on
    Mini Paint's component tree nor become unloadable outside a Forge. The
    reason is unchanged - Gradio's ``Blocks.__exit__`` does not restore the
    parent render context when the body raises, so a host that builds tabs
    inside its own Blocks would build every later component with no root.
    """
    try:
        from gradio.context import Context, get_render_context, set_render_context

        saved_block = get_render_context()
    except ImportError:  # pragma: no cover - Gradio 3.x
        from gradio.context import Context

        get_render_context = set_render_context = None
        saved_block = getattr(Context, "block", None)

    saved_root = getattr(Context, "root_block", None)
    try:
        yield
    finally:
        if set_render_context is not None:
            set_render_context(saved_block)
        else:  # pragma: no cover - Gradio 3.x
            Context.block = saved_block
        Context.root_block = saved_root


def _markdown(view: dict) -> str:
    """What the starting card says, for each of the three things it covers."""
    state = view.get("state")
    if state == STATE_STARTING:
        return (
            "### Starting WanGP\n\n"
            "It is loading in its own process, on its own GPU. The first start after a "
            "reboot is the slow one. Nothing is polling in the background - press "
            "**Check again** when you want a fresh answer."
        )
    return (
        "### WanGP is not running\n\n"
        "It starts when you ask for it, not when Forge starts. Press **Start WanGP** and "
        "this tab will show it as soon as it is serving."
    )


def _error_html(view: dict) -> str:
    code = view.get("code") or ""
    if not code:
        return ""
    extra = ""
    if code == errors.GPU_UUID_MISSING:
        # Said twice on purpose: the message already refuses the fallback, and
        # the surface has to be visibly free of a button that would do it.
        extra = (
            "<p>No other GPU was used, and none will be. Choose the card again in the "
            "wizard if this machine's hardware changed.</p>"
        )
    elif code == errors.BRIDGE_VERSION_MISMATCH:
        extra = (
            "<p>WanGP itself still works here. Only the intelligent <b>Send to</b> is off, "
            "until the bridge plugin in that WanGP is the one this extension ships.</p>"
        )
    elif code == errors.AUTH_BOUNDARY_FAILED:
        extra = "<p>Nothing was started. This is the one failure that is not worth retrying.</p>"
    return (
        f'<div class="minipaint-wangp-error"><h3>{html.escape(errors.message(code))}</h3>'
        f'<p class="minipaint-wangp-code">{html.escape(code)}</p>{extra}</div>'
    )


def create_ui() -> None:
    """The four containers, the seams the browser needs, and the events.

    Everything below is built once. The handlers only ever change visibility
    and content, and every one of them ends in the same ``_paint`` tuple so
    there is exactly one description of what "the tab looks like this now"
    means.
    """
    view = current_view()
    shell: typing.Dict[str, typing.Any] = {}

    with gr.Column(elem_id="wangp_root", elem_classes=["minipaint-wangp"]):
        # -- the seams the browser half uses -------------------------------
        # Values, not URLs: the channel id correlates messages and the state
        # lets the Send menu tell "not set up" from "not started". Neither
        # carries a port, a path or a secret.
        shell["channel"] = gr.Textbox("", visible=False, elem_id=CHANNEL_ELEM_ID)
        shell["state"] = gr.Textbox(json.dumps(view), visible=False, elem_id=STATE_ELEM_ID)
        shell["browser_check"] = gr.Textbox("", visible=False, elem_id=BROWSER_CHECK_ELEM_ID)
        # What the live iframe says about itself, so that this side knows any
        # of it. Without this the registry is never told a session exists, and
        # the diagnostics report cannot answer section 48's questions about the
        # model, the receivers or the revision - the browser is the only thing
        # that ever hears the bridge's answers.
        shell["session"] = gr.Textbox("", visible=False, elem_id=SESSION_ELEM_ID)
        # The browser's half of the log. The handshake happens entirely between
        # this page and the iframe, so without this the one thing that goes
        # wrong most often leaves no trace anywhere a user can reach.
        shell["client_log"] = gr.Textbox("", visible=False, elem_id=CLIENT_LOG_ELEM_ID)
        open_request = gr.Button("Open", visible=False, elem_id=OPEN_ELEM_ID)
        refresh_request = gr.Button("Refresh", visible=False, elem_id=REFRESH_ELEM_ID)

        with gr.Column(visible=view["view"] == VIEW_SETUP, elem_id=SETUP_ROOT_ID) as shell["setup_root"]:
            wizard = _build_wizard()

        with gr.Column(visible=view["view"] == VIEW_STARTING, elem_id=STARTING_ROOT_ID) as shell["starting_root"]:
            shell["starting_text"] = gr.Markdown(_markdown(view))
            with gr.Row():
                start_btn = gr.Button("Start WanGP", variant="primary", elem_id="wangp_start")
                recheck_btn = gr.Button("Check again", elem_id="wangp_recheck")

        with gr.Column(visible=view["view"] == VIEW_ERROR, elem_id=ERROR_ROOT_ID) as shell["error_root"]:
            shell["error_body"] = gr.HTML(_error_html(view))
            with gr.Row():
                shell["restart"] = gr.Button(
                    "Restart WanGP",
                    variant="primary",
                    visible=ACTION_RESTART in view["actions"],
                    elem_id="wangp_restart",
                )
                shell["error_reinit"] = gr.Button(
                    "Reinitialize the integration",
                    visible=ACTION_REINITIALIZE in view["actions"],
                    elem_id="wangp_error_reinitialize",
                )

        with gr.Column(visible=view["view"] == VIEW_IFRAME, elem_id=IFRAME_ROOT_ID) as shell["iframe_root"]:
            shell["iframe"] = gr.HTML(iframe_html() if view["view"] == VIEW_IFRAME else "")

        manage = _build_management()

    # -- what every handler paints -----------------------------------------
    # One list, one order, one description of "the tab looks like this now".
    painted = [
        shell["setup_root"],
        shell["starting_root"],
        shell["error_root"],
        shell["iframe_root"],
        shell["starting_text"],
        shell["error_body"],
        shell["iframe"],
        shell["restart"],
        shell["error_reinit"],
        shell["channel"],
        shell["state"],
    ]

    def paint(view: dict, channel: str = "") -> tuple:
        name = view["view"]
        actions = view.get("actions") or []
        return (
            gr.update(visible=name == VIEW_SETUP),
            gr.update(visible=name == VIEW_STARTING),
            gr.update(visible=name == VIEW_ERROR),
            gr.update(visible=name == VIEW_IFRAME),
            gr.update(value=_markdown(view)),
            gr.update(value=_error_html(view)),
            gr.update(value=iframe_html(channel) if name == VIEW_IFRAME else ""),
            gr.update(visible=ACTION_RESTART in actions),
            gr.update(visible=ACTION_REINITIALIZE in actions),
            gr.update(value=channel),
            gr.update(value=json.dumps(view)),
        )

    def show(channel: str = "") -> tuple:
        """Repaint from what is true now. Starts nothing.

        The channel this page already has is kept when it still belongs to the
        run that is serving: a repaint is not a new iframe load, and minting a
        second channel for the same load would leave the registry holding a
        session no page will ever speak on.
        """
        view = current_view()
        if view["view"] != VIEW_IFRAME:
            return paint(view, "")
        return paint(view, _keep_or_mint(channel))

    def open_tab(channel: str = "") -> tuple:
        """The tab was opened: section 11.1's one automatic start.

        The request returns immediately; what is painted is the starting card,
        and the user asks again when they want a newer answer.
        """
        view = current_view()
        if view["view"] == VIEW_STARTING and view["state"] == STATE_STOPPED:
            request_start()
            view = current_view()
        if view["view"] != VIEW_IFRAME:
            return paint(view, "")
        return paint(view, _keep_or_mint(channel))

    def restart() -> tuple:
        with contextlib.suppress(Exception):
            runtime.stop()
        request_start()
        return show()

    shell["session"].change(fn=record_session, inputs=[shell["channel"], shell["session"]], outputs=[])
    shell["client_log"].change(fn=record_client_log, inputs=[shell["client_log"]], outputs=[])
    open_request.click(fn=open_tab, inputs=[shell["channel"]], outputs=painted)
    refresh_request.click(fn=show, inputs=[shell["channel"]], outputs=painted)
    start_btn.click(fn=open_tab, inputs=[shell["channel"]], outputs=painted)
    recheck_btn.click(fn=show, inputs=[shell["channel"]], outputs=painted)
    shell["restart"].click(fn=restart, inputs=[], outputs=painted)

    # The console lives with the diagnostics, but it is the wizard that
    # fills it, so the wizard is handed the box to write into.
    _wire_wizard(wizard, shell, painted, show, manage["console"])
    _wire_management(manage, shell["error_reinit"], painted, show)


# --------------------------------------------------------------- wizard ----


def _build_wizard() -> dict:
    """The five questions of section 8, as components. No events yet."""
    parts: typing.Dict[str, typing.Any] = {}

    with gr.Column(elem_id="wangp_setup_intro") as parts["intro"]:
        gr.Markdown(
            "## Set up WanGP\n\n"
            "Connect this Forge to a WanGP you have already installed: its folder, the "
            "Python environment that runs it, and one GPU it may have to itself. Nothing "
            "is moved, copied or downloaded, and WanGP keeps its own models, presets and "
            "outputs exactly where they are."
        )
        parts["begin"] = gr.Button("Start Setup", variant="primary", elem_id="wangp_setup_begin")

    with gr.Column(visible=False, elem_id="wangp_setup_steps") as parts["steps"]:
        parts["candidate"] = gr.State({})
        parts["rows"] = gr.State([])

        gr.Markdown("### 1. The WanGP installation")
        parts["root"] = gr.Textbox(
            label="WanGP folder",
            placeholder="the folder that contains wgp.py",
            elem_id="wangp_setup_root_path",
        )
        parts["root_check"] = gr.Button("Check this folder", elem_id="wangp_setup_root_check")
        parts["root_status"] = gr.Markdown("")

        gr.Markdown("### 2. The Python environment that runs it")
        parts["runtime_scan"] = gr.Button("Find environments", elem_id="wangp_setup_runtime_scan")
        parts["runtime_choice"] = gr.Dropdown(
            choices=[], label="Environment", interactive=True, elem_id="wangp_setup_runtime_choice"
        )
        parts["runtime_prefix"] = gr.Textbox(
            label="…or the environment folder, typed in",
            placeholder="the folder with python.exe or bin/python in it",
            elem_id="wangp_setup_runtime_prefix",
        )
        parts["runtime_probe"] = gr.Button(
            "Try it against this WanGP", elem_id="wangp_setup_runtime_probe"
        )
        parts["runtime_status"] = gr.Markdown("")

        gr.Markdown("### 3. The GPU WanGP may use")
        parts["gpu_scan"] = gr.Button("List the GPUs", elem_id="wangp_setup_gpu_scan")
        parts["gpu_choice"] = gr.Dropdown(
            choices=[], label="GPU", interactive=True, elem_id="wangp_setup_gpu_choice"
        )
        parts["gpu_status"] = gr.Markdown(
            "WanGP will be started with this card and no other. If it is ever missing, "
            "nothing starts - this integration does not quietly move to a different one."
        )

        gr.Markdown("### 4. The MiniPaint bridge plugin")
        parts["bridge_check"] = gr.Button("Check the bridge plugin", elem_id="wangp_setup_bridge_check")
        parts["bridge_install"] = gr.Button(
            "Install or update it", elem_id="wangp_setup_bridge_install"
        )
        parts["bridge_status"] = gr.Markdown(
            "The bridge is a small plugin this extension owns, installed into "
            "`plugins/wan2gp-minipaint-bridge` in that WanGP. Nothing else in the install "
            "is written to."
        )

        gr.Markdown("### 5. Security and launch checks")
        parts["validate"] = gr.Button(
            "Run the checks", variant="primary", elem_id="wangp_setup_validate"
        )
        # While WanGP is coming up the answer changes without anybody pressing
        # anything, so the page asks again on its own. It runs only between
        # "a launch is in flight" and "the runtime has settled", and turns
        # itself off at that point - the standing rule against polling is
        # about a page that watches forever, not about waiting for a process
        # this very page started.
        parts["poll"] = gr.Timer(POLL_SECONDS, active=False)
        parts["checklist"] = gr.HTML(checklist_html(checklist({})), elem_id="wangp_setup_checklist")
        # The one row this code cannot settle for itself. Gradio's sign-in is a
        # per-route dependency, so a route added after the app was built is not
        # visibly covered by it either way - which is exactly the case section
        # 12.6 refuses to guess at. Somebody has to look, and this is where
        # they say they did. Until then /wan2gp/ serves nothing at all.
        parts["auth_checked"] = gr.Checkbox(
            False,
            label="I signed out and checked that /wan2gp/ asks me to sign in",
            info=(
                "Only tick this after checking. With Forge signed out, open /wan2gp/ in a private "
                "window: it must refuse you the way the rest of Forge does. WanGP has no sign-in "
                "of its own, so if that page opens, anyone who can reach this Forge can drive it."
            ),
            elem_id="wangp_setup_auth_checked",
        )
        parts["finish"] = gr.Button(
            "Finish setup", variant="primary", interactive=False, elem_id="wangp_setup_finish"
        )
        parts["restore"] = gr.Button(
            "Restore the previous working integration", elem_id="wangp_setup_restore"
        )
        parts["finish_status"] = gr.Markdown("")

    return parts


def _good(text: str) -> str:
    return f"**OK.** {text}"


def _bad(text: str) -> str:
    return f"**Not yet.** {text}"


def _next(text: str) -> str:
    """A step that has moved but is not done. Neither of the other two will do.

    Finding the environments is not choosing one, and saying "OK" there reads
    as a finished step: the wizard then sails on to the GPU, the checks come
    back saying the environment is "no longer there", and nothing on the page
    ever said which button was missed.
    """
    return f"**Next.** {text}"


def _wire_wizard(parts: dict, shell: dict, painted, show, console) -> None:
    """The wizard's events. Each one answers one question and says so.

    Every handler carries the candidate dictionary through: it is the only
    thing that becomes a config, it is per browser session because it lives in
    a ``gr.State``, and nothing is written to disk until Finish.
    """

    def check_root(root, candidate):
        candidate = dict(candidate or {})
        problems = discovery.root_problems(root)
        if problems:
            candidate.pop("root", None)
            reasons = "; ".join(detail for _code, detail in problems)
            return _bad(f"That is not a WanGP installation: {reasons}."), candidate
        candidate["root"] = str(root).strip()
        return _good("`wgp.py` and the project folders are there."), candidate

    parts["root_check"].click(
        fn=check_root,
        inputs=[parts["root"], parts["candidate"]],
        outputs=[parts["root_status"], parts["candidate"]],
    )

    def scan_runtimes(candidate):
        root = str((candidate or {}).get("root") or "")
        # A WanGP checked out with its own virtualenv beside it is the common
        # case and conda discovery will never find it, so the root's usual
        # environment folder names are offered alongside.
        extras = [f"{root}/{name}" for name in ("venv", ".venv", "env")] if root else []
        try:
            found = discovery.find_runtimes(extra_prefixes=extras)
        except Exception as error:
            return gr.update(), _bad(f"the environments could not be listed ({error}).")
        if not found:
            return gr.update(choices=[]), _bad(
                "no Conda environment was found. Type the environment folder below instead."
            )
        choices = [(f"{entry['display_name']} ({entry['type']}) - {entry['prefix']}", entry["prefix"]) for entry in found]
        return gr.update(choices=choices, value=choices[0][1]), _next(
            f"{len(choices)} environment(s) found. Choose the one that runs WanGP, then press "
            "**Try it against this WanGP** - until that has passed, no environment has been chosen."
        )

    parts["runtime_scan"].click(
        fn=scan_runtimes,
        inputs=[parts["candidate"]],
        outputs=[parts["runtime_choice"], parts["runtime_status"]],
    )

    def probe(chosen, typed, candidate):
        candidate = dict(candidate or {})
        candidate.pop("runtime", None)
        root = str(candidate.get("root") or "")
        if not root:
            return _bad("check the WanGP folder first - the probe runs against it."), candidate

        prefix = str(typed or "").strip() or str(chosen or "").strip()
        entry = discovery.runtime_for_prefix(prefix) if prefix else None
        if entry is None:
            return _bad(errors.message(errors.RUNTIME_MISSING)), candidate

        try:
            ok, strategy, detail = discovery.probe_runtime(entry, root)
        except Exception as error:
            return _bad(f"the probe could not be run ({error})."), candidate
        if not ok:
            return _bad(f"{errors.message(errors.RUNTIME_PROBE_FAILED)} {detail}"), candidate

        candidate["runtime"] = dict(entry, launch_strategy=strategy)
        how = "its own interpreter" if strategy == discovery.DIRECT_PYTHON else "conda run"
        return _good(f"`{entry['display_name']}` can start this WanGP, using {how}."), candidate

    parts["runtime_probe"].click(
        fn=probe,
        inputs=[parts["runtime_choice"], parts["runtime_prefix"], parts["candidate"]],
        outputs=[parts["runtime_status"], parts["candidate"]],
    )

    def forget_runtime(candidate):
        """A different environment is a different answer, so drop the old one.

        Otherwise a probe that passed for one environment would still be in the
        candidate after the user picked another, and setup would finish against
        an interpreter nothing had tried.
        """
        candidate = dict(candidate or {})
        had = candidate.pop("runtime", None)
        return _next(
            "press **Try it against this WanGP** to check this one."
            if had else "then press **Try it against this WanGP**."
        ), candidate

    parts["runtime_choice"].change(
        fn=forget_runtime, inputs=[parts["candidate"]], outputs=[parts["runtime_status"], parts["candidate"]]
    )
    parts["runtime_prefix"].change(
        fn=forget_runtime, inputs=[parts["candidate"]], outputs=[parts["runtime_status"], parts["candidate"]]
    )

    def scan_gpus():
        try:
            devices = discovery.list_gpus()
        except Exception as error:
            return gr.update(), _bad(f"the GPUs could not be listed ({error})."), ""
        if not devices:
            return (
                gr.update(choices=[]),
                _bad(
                    "no NVIDIA GPU was found. WanGP needs one, and this integration will not "
                    "guess at a device it cannot see."
                ),
                "",
            )
        # Friendly label shown, UUID carried: section 8.3's whole point.
        choices = [(device.label, device.uuid) for device in devices]
        return (
            gr.update(choices=choices, value=choices[0][1]),
            _good(f"{len(choices)} GPU(s) found. The one chosen here is the only one WanGP will see."),
            choices[0][1],
        )

    def scan_gpus_and_record(candidate):
        # The dropdown's own change event records a user's choice; the first
        # entry is recorded here as well, so a machine with one card does not
        # depend on a change event firing for a value nobody moved.
        update, status, uuid = scan_gpus()
        return update, status, pick_gpu(uuid, candidate)

    def pick_gpu(uuid, candidate):
        candidate = dict(candidate or {})
        text = str(uuid or "").strip()
        if not config.GPU_UUID_RE.match(text):
            candidate.pop("gpu_uuid", None)
            return candidate
        candidate["gpu_uuid"] = text
        return candidate

    parts["gpu_scan"].click(
        fn=scan_gpus_and_record,
        inputs=[parts["candidate"]],
        outputs=[parts["gpu_choice"], parts["gpu_status"], parts["candidate"]],
    )
    parts["gpu_choice"].change(
        fn=pick_gpu, inputs=[parts["gpu_choice"], parts["candidate"]], outputs=[parts["candidate"]]
    )

    def record_auth_check(ticked, candidate):
        candidate = dict(candidate or {})
        candidate["auth_checked"] = bool(ticked)
        return candidate

    parts["auth_checked"].change(
        fn=record_auth_check,
        inputs=[parts["auth_checked"], parts["candidate"]],
        outputs=[parts["candidate"]],
    )

    def check_bridge(candidate):
        root = str((candidate or {}).get("root") or "")
        if not root:
            return _bad("check the WanGP folder first.")
        code, detail = discovery.bridge_status(root, diagnostics.shipped_bridge_version())
        if code:
            return _bad(f"{errors.message(code)} ({detail})")
        return _good(f"the bridge plugin is installed and switched on - {detail}.")

    parts["bridge_check"].click(
        fn=check_bridge, inputs=[parts["candidate"]], outputs=[parts["bridge_status"]]
    )

    def install_bridge(candidate):
        root = str((candidate or {}).get("root") or "")
        if not root:
            return _bad("check the WanGP folder first.")
        try:
            result = discovery.install_bridge(root, BRIDGE_SOURCE_DIR)
        except errors.IntegrationError as error:
            return _bad(f"{error.user_message} ({error.detail})")
        except Exception as error:
            return _bad(f"the bridge could not be installed ({error}).")
        listed = (
            f" It was added to `enabled_plugins` in `{discovery.WGP_CONFIG_NAME}`, which is what "
            "makes WanGP load it; nothing else in that file was changed."
            if result.get("enabled")
            else f" `{discovery.WGP_CONFIG_NAME}` already listed it."
        )

        # WanGP builds its plugin list once, at startup, so a copy over a
        # running instance changes nothing until it comes back. We own that
        # process, so the restart is ours to do rather than ours to ask for.
        restarted = ""
        if result.get("restart_required"):
            try:
                if runtime.snapshot().get("state") != runtime.STOPPED:
                    runtime.stop()
                    restarted = " WanGP was stopped; the next Run the checks starts it with the plugin loaded."
                else:
                    restarted = " It loads the next time WanGP starts."
            except Exception as error:  # pragma: no cover - a stop that fails is still an install
                restarted = f" WanGP could not be stopped automatically ({error}); restart it to load the plugin."

        return _good(
            f"version {result['version']} written to `{result['path']}` "
            f"({result['files']} files).{listed}{restarted}"
        )

    parts["bridge_install"].click(
        fn=install_bridge, inputs=[parts["candidate"]], outputs=[parts["bridge_status"]]
    )

    def validate(candidate, reported):
        """Run every check that can be run right now, and start WanGP if the
        remaining ones need it running.

        Deliberately not one blocking call: the last four rows need a WanGP
        that is serving and an iframe that has answered, so the first press
        asks for the process and the next one reads the result. Nothing polls
        in between.
        """
        candidate = dict(candidate or {})
        document = config_from_wizard(candidate)
        started = runtime.snapshot()

        journal.note("checks", f"run: runtime is {started.get('state')}")
        # The two facts that decide whether the bridge can ever answer, read
        # from WanGP's own files rather than from anything we remember.
        root = str(candidate.get("root") or "")
        if root:
            try:
                code, detail = discovery.bridge_status(root, diagnostics.shipped_bridge_version())
                journal.note("checks", f"plugin folder: {discovery.bridge_dir(root)}")
                journal.note("checks", f"{discovery.WGP_CONFIG_NAME} enabled_plugins = {discovery.enabled_plugins(root)}")
                journal.note("checks", f"bridge status: {code or 'ok'} - {detail}")
            except Exception as error:
                journal.note("checks", f"could not read the plugin state: {type(error).__name__}: {error}")
        if started.get("state") != runtime.READY and not _start_pending():
            journal.note("checks", "asking for WanGP")
            request_start(document)
            started = runtime.snapshot()

        rows = checklist(observe(candidate, reported))
        ready = mandatory_pass(rows)
        channel = _new_channel(runtime.snapshot()) if runtime.snapshot().get("state") == runtime.READY else ""

        # Whether anything is still going to change on its own. Only a launch
        # in flight counts: once WanGP is up or has failed, the remaining red
        # rows are waiting on the person, not on the machine.
        working = _start_pending() or runtime.snapshot().get("state") == runtime.STARTING

        if ready:
            message = "Every mandatory check passed. Finish setup writes it down."
        elif working:
            message = (
                "WanGP is starting - loading a model can take a few minutes. This list is "
                "refreshing itself; nothing needs pressing until it stops."
            )
        else:
            message = (
                "Some checks have not passed yet. Open **What to do** under a red row for the "
                "next step."
            )
        for row in rows:
            if not row.get("ok"):
                journal.note("checks", f"[X] {row['label']}" + (f" - {row['detail']}" if row.get("detail") else ""))
        journal.note("checks", f"{sum(1 for row in rows if row.get('ok'))}/{len(rows)} rows pass")

        return (
            checklist_html(rows),
            rows,
            candidate,
            gr.update(interactive=ready),
            gr.update(visible=bool(channel)),
            iframe_html(channel) if channel else "",
            channel,
            message,
            gr.Timer(active=bool(working)),
            journal.text(),
        )

    parts["validate"].click(
        fn=validate,
        inputs=[parts["candidate"], shell["browser_check"]],
        outputs=[
            parts["checklist"],
            parts["rows"],
            parts["candidate"],
            parts["finish"],
            # The iframe is shown beside the wizard for the last two rows:
            # "the proxy serves the page" and "the bridge answered through it"
            # are only answerable by loading it, and there is exactly one
            # iframe to load.
            shell["iframe_root"],
            shell["iframe"],
            shell["channel"],
            parts["finish_status"],
            parts["poll"],
            console,
        ],
    )

    # The same answer, asked again by the clock rather than by a person, for
    # as long as the launch it started is still running.
    parts["poll"].tick(
        fn=validate,
        inputs=[parts["candidate"], shell["browser_check"]],
        outputs=[
            parts["checklist"],
            parts["rows"],
            parts["candidate"],
            parts["finish"],
            shell["iframe_root"],
            shell["iframe"],
            shell["channel"],
            parts["finish_status"],
            parts["poll"],
            console,
        ],
    )

    def finish(candidate, rows):
        """Write ``initialized=true`` - the only place that does.

        The button's enabled state is a fact about a browser and this is a
        fact about the setup, so the rows are judged again here. A caller that
        pressed a button it should not have been able to press gets a refusal,
        not a config file.
        """
        if not mandatory_pass(rows):
            return (_bad("not every mandatory check has passed; nothing was written."),) + show()

        document = config_from_wizard(candidate, initialized=True)
        try:
            config.write_pending(document)
            config.promote_pending()
        except errors.IntegrationError as error:
            return (_bad(f"{error.user_message} ({error.detail})"),) + show()
        except OSError as error:
            return (_bad(f"the setup could not be saved ({error})."),) + show()

        # The proxy refuses to serve on an unproven boundary, so a setup that
        # answers that question has to reach it now rather than at the next
        # restart - otherwise the tab this setup just finished shows an error.
        from . import proxy

        proxy.set_auth_acknowledged(bool(document.integration.get("auth_checked")))

        print(f"{_LOG_PREFIX} setup complete; the integration is initialized.")
        return (_good("Setup saved. The previous setup, if there was one, is kept as a backup."),) + show()

    parts["finish"].click(
        fn=finish,
        inputs=[parts["candidate"], parts["rows"]],
        outputs=[parts["finish_status"]] + painted,
    )

    def restore():
        result = restore_previous()
        text = _good(result["message"]) if result["ok"] else _bad(result["message"])
        return (text,) + show()

    parts["restore"].click(fn=restore, inputs=[], outputs=[parts["finish_status"]] + painted)

    parts["begin"].click(
        fn=lambda: (gr.update(visible=False), gr.update(visible=True)),
        inputs=[],
        outputs=[parts["intro"], parts["steps"]],
    )


# ---------------------------------------------------------- management -----


def _build_management() -> dict:
    """The panel section 10's Settings entry points at.

    It is outside the four containers because it has to be reachable in all of
    them: the tab is where Reinitialize lives, and an error surface is not the
    only place someone decides to use it.
    """
    parts: typing.Dict[str, typing.Any] = {}
    with gr.Accordion("Integration management", open=False, elem_id="wangp_manage_root") as parts["accordion"]:
        parts["reinit"] = gr.Button("Reinitialize the WanGP integration", elem_id="wangp_reinitialize")
        with gr.Column(visible=False, elem_id="wangp_reinitialize_confirm") as parts["confirm"]:
            gr.Markdown(REINITIALIZE_NOTICE)
            with gr.Row():
                parts["confirm_yes"] = gr.Button(
                    "Yes, reinitialize", variant="stop", elem_id="wangp_reinitialize_yes"
                )
                parts["confirm_no"] = gr.Button("Keep it as it is", elem_id="wangp_reinitialize_no")
        parts["status"] = gr.Markdown("")

        gr.Markdown("#### Diagnostics")
        parts["diagnostics"] = _copyable_textbox()
        parts["collect"] = gr.Button("Copy diagnostic report", elem_id="wangp_diagnostics_collect")

        # The console. Everything the integration just did, from whichever
        # side did it - this process, the WanGP child, and the browser. It is
        # the only place the handshake is visible at all: that conversation
        # happens between two windows and touches no server.
        gr.Markdown("#### Console")
        parts["console"] = _copyable_textbox(
            label="Console", lines=18, placeholder="Press Refresh, or run the checks above."
        )
        with gr.Row():
            parts["console_refresh"] = gr.Button("Refresh the console", elem_id="wangp_console_refresh")
            parts["console_clear"] = gr.Button("Clear it", elem_id="wangp_console_clear")
    return parts


def _copyable_textbox(label: str = "Diagnostic report", lines: int = 12, placeholder: str = ""):
    """A textbox with the host's own copy button where the host has one.

    ``show_copy_button`` is the affordance section 48 asks for and it needs no
    JavaScript of ours; a Gradio that predates it still gets a selectable box,
    which is the same text one keystroke further away.
    """
    common = dict(
        label=label,
        lines=lines,
        max_lines=max(lines, 40),
        interactive=False,
        placeholder=placeholder,
        elem_id="wangp_" + label.split()[0].lower(),
    )
    try:
        return gr.Textbox("", show_copy_button=True, **common)
    except TypeError:  # pragma: no cover - older Gradio
        return gr.Textbox("", **common)


def _wire_management(parts: dict, error_reinit_btn, painted, show) -> None:
    def ask():
        return gr.update(visible=True), gr.update(open=True), ""

    parts["reinit"].click(fn=ask, inputs=[], outputs=[parts["confirm"], parts["accordion"], parts["status"]])
    error_reinit_btn.click(
        fn=ask, inputs=[], outputs=[parts["confirm"], parts["accordion"], parts["status"]]
    )
    parts["confirm_no"].click(
        fn=lambda: (gr.update(visible=False), ""), inputs=[], outputs=[parts["confirm"], parts["status"]]
    )

    def confirmed():
        result = reinitialize()
        lines = "\n".join(f"- {step}" for step in result["steps"])
        text = ("**Reinitialized.**\n\n" if result["ok"] else "**Partly done.**\n\n") + lines
        return (gr.update(visible=False), text) + show()

    parts["confirm_yes"].click(
        fn=confirmed, inputs=[], outputs=[parts["confirm"], parts["status"]] + painted
    )

    def collect():
        probe_result = None
        if runtime.snapshot().get("state") == runtime.READY:
            try:
                probe_result = _probe_proxy()
            except Exception:
                probe_result = None
        return diagnostics.report(probe_result=probe_result)

    parts["collect"].click(fn=collect, inputs=[], outputs=[parts["diagnostics"]])

    def read_console():
        return journal.text()

    def clear_console():
        journal.clear()
        journal.note("console", "cleared by hand")
        return journal.text()

    parts["console_refresh"].click(fn=read_console, inputs=[], outputs=[parts["console"]])
    parts["console_clear"].click(fn=clear_console, inputs=[], outputs=[parts["console"]])


# ------------------------------------------------------------------ tab ----


def _fallback_tab(reason: str):
    """A tab that says what went wrong, under the same label and the same id.

    Returning nothing would be worse than returning this: the tab id is what
    hidden-tab settings, tab order and the browser half all address, and a tab
    that disappears when something inside it fails is the failure mode section
    7 is written against.
    """
    with gr.Blocks(analytics_enabled=False) as blocks:
        gr.Markdown(
            "### The WanGP tab could not be built\n\n"
            f"`{reason}`\n\n"
            "The full traceback is in the WebUI console. Mini Paint and the rest of the "
            "WebUI are unaffected."
        )
    return blocks


def on_ui_tabs() -> list:
    """One tab, always. Built once, and never rebuilt."""
    try:
        with _keep_build_context():
            with gr.Blocks(analytics_enabled=False) as blocks:
                create_ui()
        return [(blocks, TAB_LABEL, TAB_ID)]
    except Exception as error:
        traceback.print_exc()
        print(f"{_LOG_PREFIX} the tab failed to build; Mini Paint is unaffected.")
        return [(_fallback_tab(f"{type(error).__name__}: {error}".strip()), TAB_LABEL, TAB_ID)]
