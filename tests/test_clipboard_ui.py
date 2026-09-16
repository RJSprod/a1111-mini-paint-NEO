"""The Clipboard tab, built against the Gradio the host actually has.

Section 28.4 of the Clipboard design intent, and section 0's rule as the
user meets it. The tab is one top-level tab under one id, a browser column
twice the width of a composer column, every one of them an ordinary Gradio
component coloured by the host's theme; every event on the assembled page
resolves; Add to Queue is a button whatever the composer holds, and an
empty composer asks WanGP to queue the live page exactly as it is; a slot
put back to "Use WanGP" changes nothing on the WanGP page; the request the
tab hands the public API names Clipboard assets by id and never by path; a
confirmed admission becomes a history record, a refusal does not; a recipe
loaded from history fills the composer and queues nothing. Then the two
directions the tab shares with the rest of the extension: the gallery's
Send to Mini Paint goes into Clipboard while the intercept is on and passes
through when Clipboard cannot take it, the Canvas's Send to menu offers
Clipboard, and a picture sent from the browser to Mini Paint takes the
Canvas's own receive chain. Last, a tab that cannot be built is a tab that
says so, under the same label and id, with the rest of the WebUI intact.

No WanGP and no browser: the page is assembled the way Forge assembles it
and the callbacks are called the way Gradio calls them.
"""

from harness import Results, setup_path

setup_path()

import io  # noqa: E402
import json  # noqa: E402
import pathlib  # noqa: E402
import re  # noqa: E402
import tempfile  # noqa: E402

import forge_like  # noqa: E402  (first: it applies Forge's metaclass patches before the canvas stub is defined)
from modules import script_callbacks  # noqa: E402
from PIL import Image  # noqa: E402

from minipaint_neo import clipboard as clipboard_package  # noqa: E402
from minipaint_neo import router  # noqa: E402
from minipaint_neo.canvas import document, host  # noqa: E402
from minipaint_neo.canvas import ui as canvas_ui  # noqa: E402
from minipaint_neo.clipboard import config, history, outbox, routes, store  # noqa: E402
from minipaint_neo.clipboard import ui as clipboard_ui  # noqa: E402
from minipaint_neo.wangp import config as wangp_config  # noqa: E402
from minipaint_neo.wangp import errors, process_log, protocol  # noqa: E402
from minipaint_neo.wangp import ui as wangp_ui  # noqa: E402
from minipaint_neo.wangp.errors import IntegrationError  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Where a receive's document and status line sit in the Canvas's reply.
RECEIVE_STATE, RECEIVE_STATUS = 2, 3


def config_of(blocks) -> dict:
    return json.loads(json.dumps(blocks.get_config_file(), default=str))


def elem_ids(page: dict) -> set:
    return {c["props"].get("elem_id") for c in page["components"] if c.get("props")}


def component_of(page: dict, elem_id: str):
    for component in page["components"]:
        if component.get("props") and component["props"].get("elem_id") == elem_id:
            return component
    return None


def dangling(page: dict) -> list:
    known = {c["id"] for c in page["components"]}
    return [d for d in page["dependencies"] if any(i not in known for i in d["inputs"] + d["outputs"])
            or any(t[0] not in known and t[0] is not None and t[1] != "load" for t in d["targets"])]


def _write(path: pathlib.Path, fmt: str, size=(64, 48), colour=(10, 200, 30)) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA" if fmt == "PNG" else "RGB", size, colour + ((255,) if fmt == "PNG" else ())).save(path, format=fmt)
    return str(path)


def _value(update):
    return update.get("value") if isinstance(update, dict) else update


def _visible(update) -> bool:
    return bool(update.get("visible")) if isinstance(update, dict) else bool(update)


def _skipped(value) -> bool:
    return isinstance(value, dict) and "value" not in value and value.get("__type__") == "update"


def _queued(tab, *args, **keywords) -> tuple:
    """Add to Queue's answer, as the four things the press decides.

    The press is a route now, so its answer is JSON rather than four Gradio
    updates; these checks read the same four facts out of it.
    """
    answer = tab.add_to_queue(*args, **keywords)
    return (answer.get("instruction") or None, answer.get("status", ""),
            answer.get("jobs") or [], answer.get("queue_button") or {})


def _queue_view(tab, page) -> tuple:
    answer = tab.queue_answer(page)
    return answer["jobs"], answer["status"], answer["history"], answer["queue_button"]


def _job(view, job_id) -> dict:
    return next((job for job in view if job["job_id"] == job_id), {})


def _verbs(view, job_id) -> list:
    return [action["verb"] for action in _job(view, job_id).get("actions", [])]


def _states(view) -> list:
    return [job["state_label"] for job in view]


def _flat(view) -> str:
    return json.dumps(view)


def _listed(sort: str = "", page: int = 0, size: int = 500, selected: str = "") -> dict:
    """What the browser's grid would draw: the index route's own answer.

    The grid is not markup the server renders any more, so what these checks
    read is the thing that replaced it. ``routes.library_page`` is what the
    route hands out, and the route is a thin signed-in wrapper over it.
    """
    return routes.library_page(sort, page, size, selected)


def _ids(sort: str = "", page: int = 0, size: int = 500) -> list:
    return [item["id"] for item in _listed(sort, page, size)["items"]]


def build_page():
    """The whole page, as Forge assembles it, with the tab instance kept."""
    built = {}
    original = clipboard_ui.create_ui

    def capturing():
        built["tab"] = original()
        return built["tab"]

    clipboard_ui.create_ui = capturing
    script_callbacks.callbacks["after_component"][:] = [host.on_after_component]
    host.reset_capture()
    try:
        demo, refs = forge_like.build_host(lambda: router.on_ui_tabs() + wangp_ui.on_ui_tabs() + clipboard_ui.on_ui_tabs())
    finally:
        clipboard_ui.create_ui = original
    return demo, refs, built.get("tab")


# ------------------------------------------------------------------ the page --


#: What this tab is allowed to carry. Measured on the built page: a
#: source-level count double-counts a component built in a loop and counts
#: every ``gr.update(visible=False)`` a callback returns as if it were a
#: declaration, which is how the figure this workstream started from came
#: out a fifth too high.
#:
#: THE CEILING COMES DOWN, AND NEVER GOES BACK UP. Every hidden component is
#: a message channel the browser writes or a control its menu presses, and
#: every one of those is a purchased dependency on a transport whose failures
#: are invisible. So as rows of this tab move onto plain HTTP the number here
#: is lowered to what is left - which is what stops the tab quietly keeping
#: both doors open for ever once the interesting part of a move is done.
#:
#: 34 before the grid, the pager, the queue list and the history moved;
#: 26 after. What went: the queue instruction box and its acknowledgement
#: watcher, the hidden queue refresh, the job-action box, and the page-id box
#: the three of them carried. What came: one receipt box, so a picture handed
#: to Mini Paint is acknowledged before its tab is shown.
CLIPBOARD_HIDDEN_CEILING = 26


def page_checks(r: Results, base: pathlib.Path):
    """One tab, its parts, and every event on the assembled page."""
    tabs = clipboard_ui.on_ui_tabs()
    r.check("the extension offers exactly one Clipboard tab", len(tabs) == 1, str(len(tabs)))
    _blocks, label, ident = tabs[0]
    r.check("under the id and label the design names",
            ident == "minipaint_clipboard" == clipboard_package.TAB_ID and label == "Clipboard" == clipboard_package.TAB_LABEL, f"{label}/{ident}")

    demo, _refs, tab = build_page()
    page = config_of(demo)
    ids = elem_ids(page)
    r.check("the page has the Clipboard tab", "tab_minipaint_clipboard" in ids)
    r.check("and exactly one of it", len([c for c in page["components"] if c["props"].get("elem_id") == "tab_minipaint_clipboard"]) == 1)
    r.check("beside Mini Paint and WanGP, which are still there", {"tab_minipaint", "tab_wangp", "tab_txt2img", "tab_img2img", "tab_settings"} <= ids)
    order = [component_of(page, name)["id"] for name in ("tab_minipaint", "tab_wangp", "tab_minipaint_clipboard")]
    r.check("after Mini Paint and WanGP in the tab bar", order == sorted(order), str(order))
    r.check("the tab instance was built once and kept", tab is not None)

    # C2, this tab's half. Counted on the built page, and asserted rather
    # than reported: a new hidden textbox is a decision somebody makes, not
    # something that accretes. Every one of these is a message channel the
    # browser writes or a control its menu presses; the inventory by purpose
    # is in docs/clipboard/CONTRACTS.md.
    hidden = [c for c in page["components"]
              if str(c["props"].get("elem_id") or "").startswith("minipaint_clipboard_")
              and c["props"].get("visible") is False
              and c["type"] not in ("column", "row", "tab", "tabitem", "group", "accordion")]
    r.check(f"the Clipboard tab carries {len(hidden)} hidden components, at or below its ceiling",
            len(hidden) <= CLIPBOARD_HIDDEN_CEILING, f"{len(hidden)} > {CLIPBOARD_HIDDEN_CEILING}")
    r.check("and the ones a moved row used to need are gone, not merely unused",
            not [c for c in hidden if str(c["props"].get("elem_id")).split("minipaint_clipboard_")[-1]
                 in ("queue_instruction", "outbox_action", "outbox_refresh", "page_id")],
            str([c["props"].get("elem_id") for c in hidden]))

    needed = [
        "root", "body", "browser", "composer", "toolbar", "menu", "to_first", "to_last", "to_ref", "sort_open", "send_open", "thumb",
        "paste_now", "delete_now", "grid", "status", "folder_panel", "folder", "folder_use", "folder_create", "folder_close", "folder_status",
        "rename_panel", "rename_text", "rename_ok", "rename_cancel", "delete_panel", "delete_ok", "delete_cancel",
        "paste_panel", "paste", "paste_close", "refresh", "upload", "intercept", "folder_open", "rename_open",
        "delete_open", "paste_open", "history_open", "selected", "sort_request", "slot_action", "send_request", "send_press", "send_backend",
        "history_action", "menu_state", "switch", "payload", "to_canvas", "mask_clear", "wangp_line", "cards",
        "card_first", "card_last", "card_ref", "slot_upload_first", "slot_upload_last", "slot_upload_ref", "prompt",
        "queue", "queue_status", "outbox_list",
        "history_panel", "history_list", "history_close",
        # prompt enhancement, and the whole line's cancel
        "enhance_panel", "enhance_line", "enhance_toggle", "sp_variant", "sp_mode", "system_prompt", "sp_state", "sp_apply", "sp_restore", "sp_reload",
        "sp_open", "sp_close",
        # the gallery of what WanGP made
        "outputs_open", "outputs_panel",
        "model", "cancel_all",
    ]
    missing = [name for name in needed if f"minipaint_clipboard_{name}" not in ids]
    r.check("every part of the tab is on the page", not missing, str(missing))
    r.check("every event resolves inside the page", not dangling(page), str(dangling(page)[:2]))

    # -- the layout: Gradio components, the browser twice the composer's width
    browser = component_of(page, "minipaint_clipboard_browser")
    composer = component_of(page, "minipaint_clipboard_composer")
    r.check("the browser is a Column of weight 2", browser["type"] == "column" and browser["props"].get("scale") == 2, str(browser["props"].get("scale")))
    r.check("the composer is a Column of weight 1", composer["type"] == "column" and composer["props"].get("scale") == 1, str(composer["props"].get("scale")))
    root = component_of(page, "minipaint_clipboard_root")
    r.check("the root carries the class the theme rules are scoped to", "minipaint-clipboard" in (root["props"].get("elem_classes") or []))
    kinds = {component_of(page, f"minipaint_clipboard_{name}")["type"] for name in ("grid", "card_first", "card_last", "card_ref", "history_list")}
    r.check("the grid's mount, the cards and the history are HTML components", kinds == {"html"}, str(kinds))
    r.check("but the grid is a MOUNT and not a render: the server ships no tiles",
            component_of(page, "minipaint_clipboard_grid")["props"].get("value", "") == "",
            repr(component_of(page, "minipaint_clipboard_grid")["props"].get("value", ""))[:80])
    for name, kind in (("sort_open", "button"), ("send_open", "button"), ("thumb", "slider"), ("prompt", "textbox"), ("queue", "button"), ("upload", "uploadbutton"), ("paste", "image"), ("status", "markdown")):
        component = component_of(page, f"minipaint_clipboard_{name}")
        r.check(f"{name} is a Gradio {kind}", component["type"] == kind, component["type"])

    prompt = component_of(page, "minipaint_clipboard_prompt")["props"]
    r.check("the prompt says that empty means the WanGP prompt", prompt.get("placeholder") == "Use current WanGP prompt", str(prompt.get("placeholder")))
    r.check("and is a small multi-line box", prompt.get("lines") == 4)
    queue = component_of(page, "minipaint_clipboard_queue")["props"]
    r.check("Add to Queue is the primary button", queue.get("value") == "Add to Queue" and queue.get("variant") == "primary", str(queue))
    r.check("and it is enabled before anything is composed, WanGP running", queue.get("interactive") is not False and queue.get("visible") is not False)
    r.check("the queue list is a mount too, drawn by the browser from the route",
            component_of(page, "minipaint_clipboard_outbox_list")["props"].get("value", "") == ""
            and component_of(page, "minipaint_clipboard_history_list")["props"].get("value", "") == "")
    for name in ("selected", "sort_request", "slot_action", "send_request", "send_press", "send_backend", "history_action", "menu_state", "switch", "payload",
                 "to_canvas", "mask_clear", "receive_receipt", "model", "refresh", "upload", "intercept", "folder_open",
                 "rename_open", "delete_open", "paste_open", "history_open", "slot_upload_first", "slot_upload_last", "slot_upload_ref"):
        if component_of(page, f"minipaint_clipboard_{name}")["props"].get("visible") is not False:
            r.check(f"{name} is hidden - the menu and the cards are its face", False)
    r.check("no storage folder yet: the folder panel is open, and the index says why",
            component_of(page, "minipaint_clipboard_folder_panel")["props"].get("visible") is True
            and _listed()["reason"] == "unconfigured" and _listed()["total"] == 0, str(_listed()["reason"]))
    r.check("the three cards start as Use WanGP", all(
        "minipaint-clip-card-inherit" in component_of(page, f"minipaint_clipboard_card_{slot}")["props"].get("value", "") for slot in ("first", "last", "ref")))
    r.check("a card carries the unsupported badge, hidden until the live page says otherwise", all(
        'minipaint-clip-badge-unsupported" hidden' in component_of(page, f"minipaint_clipboard_card_{slot}")["props"].get("value", "") for slot in ("first", "last", "ref")))

    # -- the events
    deps = page["dependencies"]

    def cid(name):
        return component_of(page, f"minipaint_clipboard_{name}")["id"]

    def targeting(name, trigger):
        return [d for d in deps if [cid(name), trigger] in d["targets"]]

    def followers(dep):
        return [d for d in deps if d.get("trigger_after") == dep["id"]]

    def chain(dep):
        steps = [dep]
        while followers(steps[-1]):
            steps.append(followers(steps[-1])[0])
        return steps

    click = targeting("queue", "click")
    r.check("Add to Queue reaches the server over this tab's own route, not over the framework",
            len(click) == 1 and not click[0]["backend_fn"] and ".addToQueue(" in (click[0].get("js") or ""), str(len(click)))
    # The switch is an input of the press, not a setting the press looks up.
    # It used to be looked up, and the two drifted: the box said off while the
    # press enhanced, because the value on screen and the value on disk are
    # only ever reconciled by a change event and one had been lost.
    r.check("and the prompt and the enhancement switch travel with the press, so what is on screen is what happens",
            click and click[0]["inputs"] == [cid("prompt"), cid("enhance_toggle")], str(click[0]["inputs"] if click else None))
    # -- the system prompt editor's second shape
    opened = targeting("sp_open", "click")
    r.check("the editor fills the window from the browser AND is loaded by the server in one press",
            len(opened) == 1 and opened[0]["backend_fn"] and ".openPromptEditor(" in (opened[0].get("js") or ""),
            str(len(opened)))
    r.check("it is told the page's WanGP model and what is selected, and it sets both selectors and the box",
            opened and opened[0]["inputs"] == [cid("model"), cid("sp_variant"), cid("sp_mode")]
            and opened[0]["outputs"] == [cid("sp_variant"), cid("sp_mode"), cid("system_prompt"), cid("sp_state")],
            str(opened[0]["outputs"] if opened else None))
    r.check("and closing it is the browser's alone, because which shape a panel is in is not the server's business",
            all(not d["backend_fn"] and ".closePromptEditor(" in (d.get("js") or "") for d in targeting("sp_close", "click"))
            and targeting("sp_close", "click"))
    # The panel is PROMOTED, not copied: one set of controls, so there is no
    # second editor to drift from the first.
    r.check("there is one system prompt box on the page, not one per shape",
            len([c for c in page["components"]
                 if str(c["props"].get("elem_id") or "") == "minipaint_clipboard_system_prompt"]) == 1)
    hints = " ".join(str(c["props"].get("value") or "") for c in page["components"] if c["type"] == "markdown")
    r.check("and the paragraph explaining what the switch does is gone from the page",
            "Off by default" not in hints and "a page on another model is refused" not in hints,
            hints[:120])

    # -- the toolbar's two flyouts, and the gallery
    for name, opener in (("sort_open", ".openToolbarMenu(\"sort\")"), ("send_open", ".openToolbarMenu(\"send\")")):
        pressed = targeting(name, "click")
        r.check(f"{name} opens its list in the browser and asks the server nothing",
                len(pressed) == 1 and not pressed[0]["backend_fn"] and opener in (pressed[0].get("js") or ""),
                str(pressed[0].get("js") if pressed else None))
    r.check("there is no Sort dropdown left, so one verb has one door",
            not [c for c in page["components"]
                 if str(c["props"].get("elem_id") or "") == "minipaint_clipboard_sort"])
    r.check("and the sort's one write path answers the menu state alone",
            all(d["outputs"] == [cid("menu_state")] for d in targeting("sort_request", "input")),
            str([d["outputs"] for d in targeting("sort_request", "input")]))
    opened = targeting("outputs_open", "click")
    r.check("View Outputs is the browser's alone: the gallery is drawn from this tab's own route",
            len(opened) == 1 and not opened[0]["backend_fn"] and ".openOutputs(" in (opened[0].get("js") or ""),
            str(opened[0].get("js") if opened else None))
    r.check("and its panel is a MOUNT, shipped empty, like the grid and the queue",
            component_of(page, "minipaint_clipboard_outputs_panel")["props"].get("value", "") == "")
    r.check("no event renders the gallery either", all(cid("outputs_panel") not in d["outputs"] for d in deps))

    r.check("Cancel everything is the browser's too",
            all(not d["backend_fn"] and ".cancelAll(" in (d.get("js") or "") for d in targeting("cancel_all", "click"))
            and targeting("cancel_all", "click"))
    r.check("and no event on this page renders the queue list or the history",
            all(cid("outbox_list") not in d["outputs"] and cid("history_list") not in d["outputs"] for d in deps))
    r.check("every refresh still sets the button from WanGP's state", cid("queue") in targeting("refresh", "click")[0]["outputs"])
    r.check("the menu button is browser-only", all(not d["backend_fn"] and "toggleMenu" in (d.get("js") or "") for d in targeting("menu", "click")) and targeting("menu", "click"))
    r.check("the grid is drawn by the browser, so no event names it at all",
            not targeting("grid", "change") and all(cid("grid") not in d["outputs"] for d in page["dependencies"]))
    r.check("every server render carries a nonce the browser can see land",
            any(".menuStateChanged" in (d.get("js") or "") for d in targeting("menu_state", "change")))
    r.check("the thumbnail slider resizes in the browser as it moves and is saved when released",
            any("setThumbnailSize" in (d.get("js") or "") for d in targeting("thumb", "change")) and any(d["backend_fn"] for d in targeting("thumb", "release")))
    r.check("the prompt is saved into the draft when the box loses focus", any(d["backend_fn"] for d in targeting("prompt", "blur")))
    for name in ("to_first", "to_last", "to_ref"):
        r.check(f"{name} assigns the selection from the backend", any(d["backend_fn"] and d["inputs"] == [cid("selected")] for d in targeting(name, "click")))
    # The toolbar's own two, beside the menu. Both exist in the menu as well;
    # these are the ones under the thumb for the mode this tab is actually
    # used in - a picture in from somewhere else, a picture out when it has
    # served its purpose, over and over.
    for name in ("paste_now", "delete_now"):
        part = component_of(page, f"minipaint_clipboard_{name}")
        r.check(f"{name} is a visible button in the toolbar",
                part["type"] == "button" and part["props"].get("visible") is not False,
                f"{part['type']}/{part['props'].get('visible')}")
    pasting = targeting("paste_now", "click")
    r.check("Paste is browser work and has no server half: reading the system clipboard needs the browser",
            len(pasting) == 1 and pasting[0]["backend_fn"] is False
            and "pasteFromClipboard" in (pasting[0].get("js") or ""), str(pasting))
    deleting = targeting("delete_now", "click")
    confirmed = targeting("delete_ok", "click")
    r.check("Delete acts on the press, through the same callback the confirmation panel uses",
            len(deleting) == 1 and deleting[0]["backend_fn"]
            and confirmed and deleting[0]["backend_fn"] == confirmed[0]["backend_fn"],
            str(deleting))
    r.check("and writes the same outputs, so the grid and the cards follow it",
            deleting and confirmed and deleting[0]["outputs"] == confirmed[0]["outputs"],
            f"{len(deleting[0]['outputs']) if deleting else '?'} vs {len(confirmed[0]['outputs']) if confirmed else '?'}")
    r.check("and opens no panel on the way - that is the whole point of it",
            deleting and cid("delete_panel") not in deleting[0]["inputs"], str(deleting[0]["inputs"] if deleting else ""))
    load_events = [d for d in deps if any(t[1] == "load" for t in d["targets"]) and ".attach()" in (d.get("js") or "")]
    r.check("the page's load event attaches the browser script", len(load_events) == 1, str(len(load_events)))

    # -- send out, the Canvas's own routes
    sent = targeting("send_request", "input")
    r.check("a send request is one backend event writing the host inputs, the instruction and the payload",
            len(sent) == 1 and sent[0]["backend_fn"] and {cid("switch"), cid("payload"), cid("to_canvas"), cid("status")} <= set(sent[0]["outputs"]), str(sent))
    # The same send, carried by a press as well as by the written box.
    #
    # Not redundancy for its own sake: on one user's Forge the written box
    # never reached the server - four builds, not one send acknowledged -
    # while a pressed button on the same page worked every time. Neither way
    # in is reliable everywhere, so the tab offers both, and the two are
    # required to be the same event with the same outputs and the same
    # follow-up steps. A press wired to a different callback, or to fewer
    # outputs, is a second implementation of sending waiting to drift.
    # The fault that cost five builds: an event naming a component that is
    # not on the page cannot run - silently, for ever - and while the send
    # named other tabs' components, one absent component stopped every send
    # this tab made, including to a canvas plainly on the page. The send is
    # now required to name nothing but this tab's own boxes.
    foreign = [o for o in sent[0]["outputs"] if o not in
               {cid(n) for n in ("switch", "payload", "to_canvas", "status", "send_ack")}] if sent else []
    r.check("a send names nothing but this tab's own components, so it can always run",
            sent and not foreign, str(foreign))
    backend = targeting("send_backend", "click")
    r.check("the destinations only the server can write are on an event of their own",
            len(backend) == 1 and backend[0]["backend_fn"]
            and cid("status") in backend[0]["outputs"], str(backend))
    r.check("and that is the only event carrying another tab's component",
            backend and len(backend[0]["outputs"]) == len(tab.image_targets) + 1,
            f"{len(backend[0]['outputs']) if backend else '?'} vs {len(tab.image_targets) + 1}")
    pressed = targeting("send_press", "click")
    r.check("the same send is also carried by a press, for a page whose written box is not heard",
            len(pressed) == 1 and pressed[0]["backend_fn"], str(pressed))
    if pressed and sent:
        r.check("the press and the written box are the same event",
                pressed[0]["backend_fn"] == sent[0]["backend_fn"]
                and pressed[0]["inputs"] == sent[0]["inputs"]
                and pressed[0]["outputs"] == sent[0]["outputs"],
                f"{pressed[0]['inputs']} vs {sent[0]['inputs']}")
        r.check("and both are followed by the same steps",
                [f.get("js") for f in followers(pressed[0])] == [f.get("js") for f in followers(sent[0])],
                f"{len(followers(pressed[0]))} vs {len(followers(sent[0]))}")
    follow = followers(sent[0]) if sent else []
    r.check("followed by browser-only steps that write the host textboxes and switch tabs",
            len(follow) >= 3 and all(not f["backend_fn"] for f in follow if "switchTo" in (f.get("js") or "")) and any("switchTo" in (f.get("js") or "") for f in follow), str(len(follow)))
    r.check("and no backend step ever writes a host image textbox",
            all(f["backend_fn"] is False for f in follow if any(o == component_of(page, "img2img_image")["id"] for o in f["outputs"])) if component_of(page, "img2img_image") else True)
    to_canvas = targeting("to_canvas", "change")
    r.check("a picture for Mini Paint goes through the Canvas's own receive chain",
            len(to_canvas) == 1 and to_canvas[0]["backend_fn"] and len(to_canvas[0]["outputs"]) >= canvas_ui.TouchCanvas.INFO_COUNT, str(len(to_canvas)))
    receive_steps = chain(to_canvas[0]) if to_canvas else []
    r.check("and that chain ends by switching to the Canvas tab",
            receive_steps and "switchTo" in (receive_steps[-1].get("js") or "") and not receive_steps[-1]["backend_fn"], str(len(receive_steps)))

    # -- what the tab knows of the rest of the page
    r.check("the tab found the Canvas it was built beside", tab is not None and tab.canvas is not None and tab.canvas is canvas_ui.current())
    r.check("Mini Paint is the first destination it offers", tab is not None and tab.destinations and tab.destinations[0] == ("minipaint", "Mini Paint"), str(tab.destinations if tab else None))
    r.check("then every host destination the Canvas knows, except Clipboard itself",
            tab is not None and {"img2img", "inpaint", "extras"} <= {key for key, _ in tab.destinations} and "clipboard" not in dict(tab.destinations))
    r.check("the Canvas's Send to menu now offers Clipboard", tab is not None and "clipboard" in tab.canvas.destinations, str(tab.canvas.destinations if tab else None))
    menu = json.loads(component_of(page, "minipaint_clipboard_menu_state")["props"]["value"])
    r.check("the menu state tells the browser the intercept is off, the folder is not chosen and the sorts on offer",
            menu["intercept"] is False and menu["configured"] is False and [m for m, _ in menu["sorts"]] == list(config.SORT_MODES), str(menu))
    return page, tab


# --------------------------------------------------------------- the browser --


def browser_checks(r: Results, base: pathlib.Path, tab) -> dict:
    """The folder, the files, and every operation the menu offers on them."""
    out = tab.refresh("")
    r.check("before a folder is chosen, Refresh says to choose one",
            "Choose a storage folder" in out[0] and _listed()["reason"] == "unconfigured", str(out[0]))

    library_root = base / "library"
    folder_status, panel, *rest = tab.choose_folder(str(library_root), False)
    r.check("a folder that does not exist is not used behind the user's back", folder_status.startswith("**Not used.**") and "Create it" in folder_status and _visible(panel) and not library_root.exists())
    folder_status, panel, status, selected, menu_state, *cards = tab.choose_folder(str(library_root), True)
    r.check("and is used once created on request", folder_status.startswith("**Using**") and not _visible(panel) and library_root.is_dir() and "0 images" in status, folder_status)
    r.check("the menu learns the folder is configured", json.loads(menu_state)["configured"] is True)
    r.check("the empty folder says so as a state of the index, not of the transport",
            _listed()["reason"] == "empty" and _listed()["total"] == 0 and _listed()["configured"] is True, str(_listed()["reason"]))

    a = _write(base / "in" / "a.png", "PNG")
    b = _write(base / "in" / "b.jpg", "JPEG", (32, 24), (200, 10, 30))
    status, selected, menu_state, *cards = tab.upload([a, b], "")
    r.check("Upload takes several files and says how many", status.startswith("Imported 2 images."), status)
    listed = _listed()
    ids = [item["id"] for item in listed["items"]]
    r.check("both are in the index with opaque ids", len(ids) == 2 and all(protocol.valid_handoff_id(i) for i in ids) and listed["total"] == 2, str(ids))
    r.check("and each carries the version of its bytes, so the browser can cache it for ever",
            all(item["v"] and item["v"].count("-") == 1 for item in listed["items"]), str([i["v"] for i in listed["items"]]))
    r.check("the last import is selected", selected == ids[0] or selected in ids, selected)
    r.check("nothing on the page names the host folder", str(base) not in json.dumps(listed) and str(base) not in status and str(base) not in menu_state and str(base) not in "".join(c for c in cards if isinstance(c, str)))
    assets = {asset.filename: asset for asset in tab.library.assets()}
    r.check("the files keep their names and bytes", set(assets) == {"a.png", "b.jpg"} and (library_root / "b.jpg").read_bytes() == pathlib.Path(b).read_bytes(), str(sorted(assets)))
    id_a, id_b = assets["a.png"].asset_id, assets["b.jpg"].asset_id

    text = base / "in" / "notes.txt"
    text.write_text("not an image", encoding="utf-8")
    status, selected, menu_state, *cards = tab.upload([str(text)], id_a)
    r.check("a file that is not an image is refused with a sentence, and the selection is kept",
            status.startswith("Nothing was imported.") and "<small>" in status and selected == id_a, status)

    # One write path, whichever flyout pressed it: the toolbar's Sort button
    # and the menu's Sort submenu both write this box. There is no dropdown
    # left to answer, so the menu state is the whole reply.
    menu_state = tab.sort_request("name_asc:nonce", id_b)
    r.check("a sort press reorders the whole library and is remembered",
            _ids() == [id_a, id_b] and config.load().sort == "name_asc", str(_ids()))
    r.check("and the reply is the menu state alone, which is what ticks the list next time it opens",
            json.loads(menu_state)["sort"] == "name_asc", str(menu_state)[:80])
    menu_state = tab.sort_request("name_desc:nonce", id_b)
    r.check("a second press with a fresh nonce sorts the other way", _ids() == [id_b, id_a])
    menu_state = tab.sort_request("sideways", id_b)
    r.check("an unknown sort changes nothing", config.load().sort == "name_desc")
    r.check("and the index falls back to the stored sort rather than refusing an unknown one",
            _listed("sideways")["sort"] == "name_desc", _listed("sideways")["sort"])
    r.check("the thumbnail size is saved, clamped", json.loads(tab.thumbnail_changed(200))["thumbnail"] == 200 and json.loads(tab.thumbnail_changed(5000))["thumbnail"] == config.THUMBNAIL_MAX)

    panel, textbox, status = tab.open_rename("")
    r.check("Rename with nothing selected says so", not _visible(panel) and "Select an image" in status)
    panel, textbox, status = tab.open_rename(id_a)
    r.check("Rename opens on the stem of the selected file", _visible(panel) and _value(textbox) == "a" and _skipped(status))
    panel, status, *cards = tab.rename(id_a, "bad/name")
    r.check("a name with a separator is refused and the panel stays", _visible(panel) and status.startswith("Not renamed:"), status)
    was = next(item["v"] for item in _listed()["items"] if item["id"] == id_a)
    panel, status, *cards = tab.rename(id_a, "renamed")
    r.check("a good name renames the file, keeps its id and extension, and closes the panel",
            not _visible(panel) and status == "Renamed to renamed.png." and tab.library.get(id_a).filename == "renamed.png" and (library_root / "renamed.png").is_file())
    r.check("and the caption moves while the picture's version does not, so nothing is re-fetched",
            next(item["name"] for item in _listed()["items"] if item["id"] == id_a) == "renamed.png"
            and next(item["v"] for item in _listed()["items"] if item["id"] == id_a) == was)

    paste = _write(base / "in" / "pasted.jpg", "JPEG", (20, 20), (1, 2, 3))
    status, selected, menu_state, *cards, image, panel = tab.pasted(paste, "")
    r.check("a paste through Gradio's own surface lands as a PNG, selected, and the panel closes",
            status.startswith("Pasted paste-") and status.endswith(".png.") and selected in _ids() and _value(image) is None and not _visible(panel), status)
    status, selected, menu_state, *cards, image, panel = tab.pasted("", id_a)
    r.check("an empty paste is a sentence and the selection is kept", status.startswith("Nothing was pasted.") and selected == id_a)
    pasted_id = next(asset.asset_id for asset in tab.library.assets() if asset.filename.startswith("paste-"))

    panel, status = tab.open_delete("")
    r.check("Delete with nothing selected says so", not _visible(panel) and "Select an image" in status)
    panel, status = tab.open_delete(pasted_id)
    r.check("Delete asks first", _visible(panel) and _skipped(status))
    panel, status, selected, menu_state, *cards = tab.delete(pasted_id)
    r.check("and then removes the file, clears the selection and says so",
            not _visible(panel) and status.startswith("Deleted paste-") and selected == "" and pasted_id not in _ids() and not (library_root / "paste").exists(), status)

    menu_state, status = tab.toggle_intercept()
    r.check("the intercept switch turns on from the menu", json.loads(menu_state)["intercept"] is True and config.load().intercept is True and "Clipboard" in status)
    menu_state, status = tab.toggle_intercept()
    r.check("and off again", json.loads(menu_state)["intercept"] is False and config.load().intercept is False and "Mini Paint again" in status)

    (base / "library" / "dropped.png").write_bytes(pathlib.Path(a).read_bytes())
    status, selected, menu_state, *cards = tab.refresh(id_a)
    r.check("a file dropped into the folder by hand appears on Refresh",
            "3 images" in status and "dropped.png" in [item["name"] for item in _listed()["items"]], status)
    return {"a": id_a, "b": id_b, "root": library_root}


# -------------------------------------------------------------- the composer --


def composer_checks(r: Results, base: pathlib.Path, tab, ids: dict) -> None:
    """Sparse overrides: assign, clear, prepare, the result, the history."""
    id_a, id_b = ids["a"], ids["b"]

    first, last, ref, status = tab.assign("first", id_a)
    r.check("+First puts the selected picture in the First Frame card",
            "minipaint-clip-card-override" in first and f'data-asset="{id_a}"' in first and status == "First Frame: renamed.png." and "minipaint-clip-card-inherit" in last, status)
    r.check("the card names the file, never its folder", "renamed.png" in first and str(base) not in first)
    first, last, ref, status = tab.assign("ref", "")
    r.check("+Ref with nothing selected says so and changes nothing", "Select an image" in status and "minipaint-clip-card-inherit" in ref)
    first, last, ref, status = tab.slot_action(f"assign:last:{id_b}")
    r.check("a drop on the Last Frame card assigns it", "minipaint-clip-card-override" in last and f'data-asset="{id_b}"' in last and status == "Last Frame: b.jpg.")
    first, last, ref, status = tab.slot_action("clear:first")
    r.check("the card's × puts the slot back to Use WanGP and says the WanGP page was not touched",
            "minipaint-clip-card-inherit" in first and status.startswith("First Frame: Use WanGP.") and "nothing on the WanGP page was changed" in status, status)
    first, last, ref, status = tab.slot_action("nonsense")
    r.check("an unreadable card action changes nothing", _skipped(status) and "minipaint-clip-card-override" in last)
    draft = history.load_draft()
    r.check("the draft on disk follows", draft["first_asset_id"] == "" and draft["last_asset_id"] == id_b and draft["reference_asset_ids"] == [])

    chosen = _write(base / "in" / "ref.png", "PNG", (16, 16), (5, 5, 5))
    status, selected, menu_state, first, last, ref, _button = tab.slot_upload("ref", chosen, "")
    r.check("Choose a file on a card imports the file first and then assigns it",
            "minipaint-clip-card-override" in ref and status.startswith("Reference: ref.png.") and "imported into the folder first" in status and selected in _ids(), status)
    ref_id = history.load_draft()["reference_asset_ids"][0]
    r.check("the reference slot holds that new asset", tab.library.get(ref_id).filename == "ref.png")

    r.check("the prompt is kept in the draft as typed", tab.prompt_changed(" a prompt ") is None and history.load_draft()["prompt_override"] == " a prompt ")

    # -- Add to Queue: a job in the server's outbox, run by this page
    page_id = "c" * 16
    instruction, status, listing, button = _queued(tab, " a prompt ", page_id)
    r.check("Add to Queue appends a job and says so", instruction and status.startswith("Queued for WanGP.") and "it goes next" in status, status)
    parsed = instruction
    job = outbox.jobs()[-1]
    request = job["request"]
    r.check("the instruction names the job for the browser's pump", parsed.get("nonce") and parsed.get("job_id") == job["job_id"], str(parsed)[:80])
    r.check("the job is this page's, pending, from the Clipboard tab, and asks to start when WanGP can",
            job["page"] == page_id and job["state"] == "pending" and job["origin"] == "clipboard" and request["start"] == "auto")
    r.check("the prompt is cleaned, the empty slot omitted, the filled ones named by asset id",
            request.get("prompt") == "a prompt" and "start" not in request["images"]
            and request["images"]["end"] == {"kind": "clipboard_asset", "id": id_b}
            and request["images"]["references"] == [{"kind": "clipboard_asset", "id": ref_id}], json.dumps(request))
    r.check("and never a path", str(base) not in json.dumps(request) and "/" not in json.dumps(request["images"]))
    r.check("the list shows it waiting, with Cancel",
            _job(listing, job["job_id"]).get("state_label") == "Waiting" and _verbs(listing, job["job_id"]) == ["cancel"]
            and str(base) not in _flat(listing), _flat(listing)[:120])
    r.check("the button stays a button", button.get("label") == clipboard_ui.QUEUE_BUTTON_LABEL and button.get("enabled") is True)


    # -- the page runs it: claim, admitted, confirmed queued -> history
    claimed = outbox.claim(page_id)
    r.check("the page claims its job", claimed["job"]["job_id"] == job["job_id"])
    outbox.report(job["job_id"], claimed["lease"], "sent")
    outbox.report(job["job_id"], claimed["lease"], "done", {
        "ok": True, "status": "queued", "request_id": request["request_id"], "tasks_added": 1, "queue_depth": 0, "route": "queue",
        "applied": {"prompt": True, "start": False, "end": True, "references": 1}, "inherited": ["start"], "ignored": [],
        "model": {"type": "video", "label": "A video model"},
    })
    listing, status, history_listing, button = _queue_view(tab, page_id)
    r.check("the refresh says it was added", status.startswith("Added to WanGP queue."), status)
    r.check("and the list shows it queued, with nothing left to press on it",
            _job(listing, job["job_id"]).get("state_label") == "Queued" and _verbs(listing, job["job_id"]) == [])
    # Handed over is not finished: the card stays until WanGP's own queue
    # has let the task go, which is the furthest a browser-run job can see.
    outbox.track(job["job_id"], page_id, {"state": "generating", "position": 0, "queue_depth": 0})
    r.check("a job WanGP is still generating is still listed",
            bool(_job(tab._outbox_view(page_id), job["job_id"])))
    outbox.track(job["job_id"], page_id, {"state": "finished", "position": None, "queue_depth": None})
    r.check("and the moment it leaves WanGP's queue the card goes, though the record is still there to ask about",
            not _job(tab._outbox_view(page_id), job["job_id"]) and outbox.get(job["job_id"])["state"] == "queued",
            str(outbox.get(job["job_id"]).get("wangp")))
    records = history.load_history()
    r.check("and records one history entry", len(records) == 1 and records[0]["request_id"] == request["request_id"], str(len(records)))
    record = records[0] if records else {}
    r.check("with the recipe: prompt override, last frame and reference overrides, start inherited",
            record.get("prompt_mode") == "override" and record.get("prompt_override") == "a prompt" and record.get("first_mode") == "inherit"
            and record.get("last_mode") == "override" and record.get("last_asset_id") == id_b and record.get("reference_mode") == "override"
            and record.get("reference_asset_ids") == [ref_id] and record.get("tasks_added") == 1 and record.get("model_label") == "A video model", json.dumps(record))
    r.check("the history list shows it, by id and name, never by path",
            history_listing and history_listing[0]["history_id"] and "b.jpg" in _flat(history_listing)
            and "A video model" in _flat(history_listing) and str(base) not in _flat(history_listing))
    r.check("a second refresh records nothing twice", tab.queue_answer(page_id) and len(history.load_history()) == 1)

    # -- a started job, with an ignored field
    instruction, status, listing, button = _queued(tab, "", page_id)
    job2 = outbox.jobs()[-1]
    r.check("an empty prompt is omitted from the request, not sent as empty", "prompt" not in job2["request"])
    claimed = outbox.claim(page_id)
    outbox.report(job2["job_id"], claimed["lease"], "done", {"ok": True, "status": "started", "request_id": job2["request"]["request_id"], "tasks_added": 2, "queue_depth": 0, "route": "generate",
                                                             "applied": {"end": True}, "inherited": ["prompt", "start"], "ignored": [{"field": "references", "code": "RECEIVER_DISABLED"}], "model": {}})
    listing, status, history_listing, button = _queue_view(tab, page_id)
    r.check("a started job says WanGP is generating it, and names the field the model did not use",
            status.startswith("WanGP started generating it.") and "reference was not used by the current model" in status.lower(), status)
    newest = history.load_history()[0]
    r.check("and the record says so", newest["reference_mode"] == "ignored" and newest["prompt_mode"] == "inherit" and newest["tasks_added"] == 2, json.dumps(newest))
    r.check("the list shows Generating", "Generating" in _states(listing), str(_states(listing)))

    # -- refusals and doubts record nothing, and offer Retry
    before = len(history.load_history())
    instruction, status, listing, button = _queued(tab, "x", page_id)
    job3 = outbox.jobs()[-1]
    claimed = outbox.claim(page_id)
    outbox.report(job3["job_id"], claimed["lease"], "done", {"ok": False, "status": "refused", "code": "QUEUE_BUSY"})
    listing, status, history_listing, button = _queue_view(tab, page_id)
    r.check("a refusal shows the code's sentence, records nothing, and offers Retry",
            status.startswith(errors.message(errors.QUEUE_BUSY)) and len(history.load_history()) == before
            and _verbs(listing, job3["job_id"]) == ["retry", "dismiss"], status)
    instruction, status, listing, button = _queued(tab, "x", page_id)
    job4 = outbox.jobs()[-1]
    claimed = outbox.claim(page_id)
    outbox.report(job4["job_id"], claimed["lease"], "sent")
    outbox.report(job4["job_id"], claimed["lease"], "done", {"ok": False, "status": "unconfirmed"})
    listing, status, history_listing, button = _queue_view(tab, page_id)
    r.check("an unconfirmed admission is reported as unconfirmed with Retry anyway, never as queued",
            status.startswith(errors.message(errors.ADMISSION_UNCONFIRMED)) and len(history.load_history()) == before
            and "Retry anyway" in _flat(listing), status)

    # -- the buttons on a job
    instruction, status, listing, button = _queued(tab, "to cancel", page_id)
    job5 = outbox.jobs()[-1]
    acted = tab.outbox_action(f"cancel:{job5['job_id']}:{page_id}", page_id)
    listing, status = acted["jobs"], acted["status"]
    r.check("Cancel cancels a pending job", status.startswith("Cancelled.") and outbox.get(job5["job_id"])["state"] == "cancelled"
            and _job(listing, job5["job_id"]).get("state_label") == "Cancelled")
    acted = tab.outbox_action(f"retry:{job3['job_id']}:{page_id}", page_id)
    listing, status = acted["jobs"], acted["status"]
    retried = outbox.jobs()[-1]
    r.check("Retry sends a refused job again as a new request for this page",
            status.startswith("Sent again") and retried["retry_of"] == job3["job_id"] and retried["page"] == page_id and retried["state"] == "pending", status)
    other = outbox.submit({"prompt": "from elsewhere"}, "d" * 16, "clipboard")
    listing = tab._outbox_view(page_id)
    r.check("a job composed on another page is marked so and offers Run from this page",
            "composed on another page" in _flat(listing) and "adopt" in _verbs(listing, other["job_id"]), _flat(listing)[:120])
    acted = tab.outbox_action(f"adopt:{other['job_id']}:{page_id}", page_id)
    listing, status = acted["jobs"], acted["status"]
    r.check("Run from this page adopts it", "This page will run it" in status and outbox.get(other["job_id"])["page"] == page_id)

    # -- every verb a card can offer is a verb the route will take
    #
    # Written as a property rather than a list, because a list here is a
    # second copy of the one in routes.py and the two drift silently: a
    # button drawn on a card that the route answers with "not a queue
    # action" looks, from the tab, exactly like a press that did nothing.
    # That is the shape of the fault this check exists for.
    import inspect

    from minipaint_neo.clipboard import routes as clip_routes

    source = inspect.getsource(clip_routes._queue)
    offered = set()
    for view in (tab._outbox_view(page_id),):
        for card in view:
            offered.update(action["verb"] for action in card.get("actions", []))
    r.check("every button a job card offers is a verb the queue route accepts",
            offered and all(f'"{verb}"' in source for verb in offered), str(sorted(offered)))

    # -- Dismiss: the other half of every failure
    r.check("a cancelled job offers Dismiss beside Retry, because it is waiting for a person",
            _verbs(tab._outbox_view(page_id), job5["job_id"]) == ["retry", "dismiss"],
            str(_verbs(tab._outbox_view(page_id), job5["job_id"])))
    acted = tab.outbox_action(f"dismiss:{job5['job_id']}:{page_id}", page_id)
    r.check("Dismiss takes the card off the queue at once", acted["ok"] and not _job(acted["jobs"], job5["job_id"]),
            str(acted.get("status")))
    r.check("but the record is still there, so a page or an API caller waiting on it still gets an answer",
            outbox.get(job5["job_id"])["state"] == "cancelled" and outbox.get(job5["job_id"])["dismissed"] is True)
    acted = tab.outbox_action(f"dismiss:{other['job_id']}:{page_id}", page_id)
    r.check("and a job that has not failed cannot be dismissed - it is refused, not quietly ignored",
            not acted["ok"] and bool(_job(tab._outbox_view(page_id), other["job_id"])), str(acted.get("code")))
    acted = tab.outbox_action("cancel:0000000000000000:" + page_id, page_id)
    listing, status = acted["jobs"], acted["status"]
    r.check("a job that is gone says so", status.startswith(errors.message(errors.QUEUE_JOB_UNKNOWN)))
    for job in outbox.jobs():
        if job["state"] == "pending":
            outbox.cancel(job["job_id"])

    # -- WanGP not running: the button is off and a press is refused, not stored
    outbox.use_running(lambda: False)
    button = tab._queue_button()
    r.check("with WanGP not running the button is off and says why", button.get("interactive") is False and _value(button) == clipboard_ui.QUEUE_BUTTON_BLOCKED)
    r.check("every refresh sets it so", tab.refresh("")[-1].get("interactive") is False)
    r.check("and the route says the same two facts about it",
            tab._queue_button_view() == {"label": clipboard_ui.QUEUE_BUTTON_BLOCKED, "enabled": False}, str(tab._queue_button_view()))
    count = len(outbox.jobs())
    instruction, status, listing, button = _queued(tab, "while off", page_id)
    r.check("a press while WanGP is not running is refused with the sentence, and nothing is stored",
            instruction is None and status.startswith(errors.message(errors.WANGP_NOT_RUNNING)) and len(outbox.jobs()) == count and button.get("enabled") is False, status)
    outbox.use_running(lambda: True)
    r.check("and comes back when WanGP does", tab._queue_button().get("interactive") is True)

    # -- the composer with nothing in it still asks
    tab.slot_action("clear:last")
    tab.slot_action("clear:ref")
    instruction, status, listing, button = _queued(tab, "", page_id)
    empty = outbox.jobs()[-1]["request"]
    r.check("an empty composer still produces a request: the live WanGP page, as it is",
            instruction and set(empty) == {"request_id", "images", "start"} and empty["images"] == {}, json.dumps(empty))
    outbox.cancel(outbox.jobs()[-1]["job_id"])

    # -- a slot whose file is gone fails before WanGP is asked
    tab.assign("first", id_a)
    (ids["root"] / "renamed.png").unlink()
    tab.library.refresh()
    count = len(outbox.jobs())
    instruction, status, listing, button = _queued(tab, "", page_id)
    r.check("a missing image refuses before storing anything and keeps the draft",
            instruction is None and status.startswith("First Frame: the image is no longer in the folder") and "nothing was asked of WanGP" in status
            and history.load_draft()["first_asset_id"] == id_a and len(outbox.jobs()) == count, status)
    first, last, ref = tab._cards(missing=["first"])
    r.check("the card can say Missing image", "minipaint-clip-card-missing" in first and "Missing image" in first)
    status, selected, menu_state, first, last, ref, _button = tab.refresh("")
    r.check("Refresh puts the slot back to Use WanGP and says why",
            "minipaint-clip-card-inherit" in first and "First Frame: the image is no longer in the folder" in status and history.load_draft()["first_asset_id"] == "", status)

    # -- history: load fills the composer, queues nothing; delete removes the record
    panel = tab.show_history()
    listing = tab._history_view()
    r.check("Queue Send History opens, and its list comes from the route",
            _visible(panel) and listing and all(entry.get("history_id") for entry in listing), _flat(listing)[:120])
    record = history.load_history()[-1]  # the first one queued: prompt "a prompt" (as sent), end b, ref
    before = len(history.load_history())
    prompt, first, last, ref, status = tab.history_action(f"load:{record['history_id']}", "")
    r.check("Load puts the recipe back into the composer and queues nothing",
            prompt == "a prompt" and "minipaint-clip-card-override" in last and "minipaint-clip-card-override" in ref and status.startswith("Recipe loaded. Nothing was queued.")
            and history.load_draft()["last_asset_id"] == id_b and len(history.load_history()) == before, status)
    prompt, first, last, ref, status = tab.history_action(f"delete:{record['history_id']}", "")
    r.check("Delete removes the record and touches no file",
            status.startswith("History entry deleted.") and len(history.load_history()) == before - 1 and (ids["root"] / "b.jpg").is_file())
    prompt, first, last, ref, status = tab.history_action("load:nope", "")
    r.check("a record that is gone says so", "gone" in status and _skipped(prompt))
    tab.library.delete(id_b)
    remaining = history.load_history()[0]
    listing = tab._history_view()
    r.check("a record whose image was deleted says Missing image and still loads what it can",
            any(slot.get("state") == "missing" for entry in listing for slot in entry["slots"]) and remaining["last_asset_id"] == id_b)
    prompt, first, last, ref, status = tab.history_action(f"load:{remaining['history_id']}", "")
    r.check("with the missing slot back to Use WanGP, and named", "Missing image" not in last and "minipaint-clip-card-inherit" in last and "last" in status.lower())


# ---------------------------------------------------------------- send out --


def send_checks(r: Results, base: pathlib.Path, tab) -> None:
    """A picture leaves the browser the way it leaves the Canvas."""
    picture = _write(base / "in" / "send.png", "PNG", (40, 30), (9, 8, 7))
    status, selected, *_rest = tab.upload([picture], "")
    n = 0  # send() names no other tab's components at all now; see send_backend
    if not selected:
        r.check("an upload to send", False, status)
        return
    out = tab.send(f"img2img:{selected}:1700000001", "")
    r.check("Send to img2img writes the instruction and a PNG payload, and names no other tab",
            out[n] == "img2img" and str(out[n + 1]).startswith("data:image/png;base64,") and out[n + 2] == ""
            and len(out) == 5 and out[n + 3].startswith("Sent send.png to img2img."), str(out[n + 3]))
    out = tab.send(f"inpaint:{selected}", "")
    r.check("Send to Inpaint names the size the Inpaint canvas must reach", out[n] == "inpaint:40x30" and str(out[n + 1]).startswith("data:image/png"))
    if "extras" in tab.image_targets:
        out = tab.send_backend(f"extras:{selected}", "")
        index = tab.image_targets.index("extras")
        r.check("Send to Extras writes its image component directly, saved where the host serves it from",
                hasattr(out[index], "already_saved_as"), str(out[index])[:80])
    stitch = [key for key in tab.image_targets if key in canvas_ui.STITCH_TARGETS]
    if stitch:
        out = tab.send_backend(f"{stitch[0]}:{selected}", "")
        index = tab.image_targets.index(stitch[0])
        r.check("Send to ImageStitch replaces its gallery with this one picture",
                isinstance(out[index], list) and len(out[index]) == 1 and "only reference image" in out[-1], str(out[-1])[:90])
    out = tab.send(f"minipaint:{selected}:1700000002", "")
    r.check("Send to Mini Paint hands the asset to the Canvas's receive box and nothing to the host",
            out[n + 2].startswith(f"{selected}:") and out[n] == "" and out[n + 1] == "" and out[n + 3].startswith("Sent send.png to Mini Paint."), str(out[n + 2]))
    # The page places the picture itself and marks the event, so the event
    # records the send instead of performing it. Writing the destination a
    # second time is harmless for a canvas and one picture too many for a
    # gallery that appends.
    out = tab.send(f"img2img:{selected}:1700000003:done", "")
    r.check("a send the page already made is recorded, not performed again",
            out[n] == "" and out[n + 1] == "" and out[n + 2] == ""
            and out[n + 3].startswith("Sent send.png to img2img.") and out[n + 4] == "1700000003",
            str(out[n + 3]))
    if stitch:
        out = tab.send_backend(f"{stitch[0]}:{selected}:1700000004:done", "")
        index = tab.image_targets.index(stitch[0])
        tab.send_backend(f"{stitch[0]}:{selected}:1700000004:done", "")
        twice = tab.send_backend(f"{stitch[0]}:{selected}:1700000004:done", "")
        r.check("and a gallery is not appended to twice", _skipped(twice[index]), str(twice[index])[:80])
    # Two events carry one send - a press and the written box - because on
    # some installs only one of them arrives. On a healthy install both do,
    # and the second must not deliver the picture again: to a canvas that is
    # waste, to a gallery that appends it is one picture too many. So the
    # repeat is answered with the receipt and nothing else.
    first = tab.send(f"img2img:{selected}:1700000005", "")
    again = tab.send(f"img2img:{selected}:1700000005", "")
    r.check("the same request arriving twice is delivered once and acknowledged twice",
            first[n] == "img2img" and str(first[n + 1]).startswith("data:image/png")
            and _skipped(again[n]) and _skipped(again[n + 1]) and again[n + 4] == "1700000005",
            f"{again[n]!r} {again[n + 4]!r}")
    third = tab.send(f"img2img:{selected}:1700000007", "")
    r.check("while the next request is a new send, not a repeat",
            third[n] == "img2img" and str(third[n + 1]).startswith("data:image/png") and third[n + 4] == "1700000007",
            str(third[n + 4]))

    # The press that carries no payload.
    #
    # A press makes the server read the hidden box, and it reads whatever
    # value the framework holds for it - which on the install this is for is
    # not what the page wrote. So the browser leaves the request on the send
    # route on its way out, and this falls back to it when the box offers
    # nothing it has not already answered. Without it the press is answered
    # with the receipt for an older send and the picture never moves.
    from minipaint_neo.clipboard import routes as clip_routes

    clip_routes.forget_request()
    stale = tab.send(f"img2img:{selected}:1700000007", "")
    r.check("with nothing posted, a stale box is still only answered once",
            _skipped(stale[n]) and stale[n + 4] == "1700000007", str(stale[n + 4]))
    clip_routes.remember_request(f"img2img:{selected}:1700000009")
    carried = tab.send(f"img2img:{selected}:1700000007", "")
    r.check("a press whose box is stale is answered from the request the browser posted",
            carried[n] == "img2img" and str(carried[n + 1]).startswith("data:image/png")
            and carried[n + 4] == "1700000009", str(carried[n + 4]))
    repeat = tab.send(f"img2img:{selected}:1700000007", "")
    r.check("and that request is not then delivered again by the next press",
            _skipped(repeat[n]) and repeat[n + 4] == "1700000007", str(repeat[n + 4]))
    clip_routes.remember_request(f"img2img:{selected}:1700000010")
    fresh_box = tab.send(f"img2img:{selected}:1700000011", "")
    r.check("while a box with something new to say is always preferred to it",
            fresh_box[n + 4] == "1700000011", str(fresh_box[n + 4]))
    clip_routes.forget_request()
    out = tab.send("nowhere:" + selected, "")
    r.check("an unknown destination is refused by name", "not available" in out[n + 3])
    out = tab.send("img2img", "")
    r.check("a send with nothing selected says so", "Select an image" in out[n + 3])
    r.check("the Canvas takes the picture from the box by id", tab._picture_for_canvas(f"{selected}:1700000002").size == (40, 30) and tab._picture_for_canvas("") is None)


# ----------------------------------------------------- the rest of the page --


def integration_checks(r: Results, base: pathlib.Path, tab) -> None:
    """The intercept, Send to Clipboard from the Canvas, and the way back."""
    canvas = tab.canvas
    photo = Image.new("RGB", (64, 48), (200, 30, 30))
    r.check("the Clipboard package answers that it is available", clipboard_package.available())
    config.update(intercept=False)
    r.check("the intercept reads False when off", clipboard_package.intercept_enabled() is False)

    out = canvas.receive([(photo, None)], None, "crop", "txt2img")
    doc = out[RECEIVE_STATE]
    r.check("with the intercept off a gallery send lands on the Canvas", "Received from txt2img" in out[RECEIVE_STATUS] and doc.has_image)
    r.check("and the follow-up step switches to the Canvas", canvas.after_receive(doc) == "canvas" and doc.pending_switch == "")

    config.update(intercept=True)
    r.check("the intercept reads True when on", clipboard_package.intercept_enabled() is True)
    before = {asset.asset_id for asset in tab.library.assets()}
    out = canvas.receive([(photo, None)], doc, "crop", "img2img")
    r.check("with the intercept on the same send goes into Clipboard and leaves the Canvas alone",
            "Sent to Clipboard." in out[RECEIVE_STATUS] and _skipped(out[0]) and _skipped(out[1]) and doc.image.size == (64, 48), out[RECEIVE_STATUS])
    r.check("and the follow-up step switches to the Clipboard tab", canvas.after_receive(doc) == "clipboard")
    arrived = [asset for asset in tab.library.assets() if asset.asset_id not in before]
    r.check("the picture is in the library as a PNG named after the tab it came from, marked as a gallery import",
            len(arrived) == 1 and arrived[0].filename.startswith("img2img-output") and arrived[0].filename.endswith(".png") and arrived[0].source == "forge_gallery"
            and arrived[0].width == 64, str([a.filename for a in arrived]))

    original = store.Store.import_image

    def refusing(self, image, basename="", source="paste"):
        raise IntegrationError(errors.CLIPBOARD_NOT_CONFIGURED, "no folder")

    store.Store.import_image = refusing
    try:
        out = canvas.receive([(photo, None)], None, "crop", "txt2img")
    finally:
        store.Store.import_image = original
    r.check("a Clipboard that cannot take the picture passes it through to the Canvas and says why",
            "Received from txt2img" in out[RECEIVE_STATUS] and "CLIPBOARD_NOT_CONFIGURED" in out[RECEIVE_STATUS] and out[RECEIVE_STATE].has_image, out[RECEIVE_STATUS])
    r.check("and switches to the Canvas", canvas.after_receive(out[RECEIVE_STATE]) == "canvas")
    config.update(intercept=False)

    # -- Send to Clipboard from the Canvas
    n = len(canvas.image_targets)
    before = {asset.asset_id for asset in tab.library.assets()}
    out = canvas.send(None, doc, "crop", "clipboard", "Off")
    r.check("Menu -> Send to -> Clipboard puts the composite into the library and says as what",
            isinstance(out[n + 1], str) and "Sent to Clipboard as minipaint.png." in out[n + 1], str(out[n + 1]))
    arrived = [asset for asset in tab.library.assets() if asset.asset_id not in before]
    r.check("as a PNG marked as coming from Mini Paint, with no host input written",
            len(arrived) == 1 and arrived[0].source == "minipaint" and arrived[0].width == 64 and all(_skipped(v) for v in out[:n]) and out[-2] == "" and out[-1] == "")
    r.check("the document stays on the Canvas", doc.has_image and doc.last_send == "Clipboard")
    out = canvas.send(None, None, "crop", "clipboard", "Off")
    r.check("with no image there is nothing to send", "no image to send" in out[n + 1].lower())

    # -- the way back: a Clipboard picture into the Canvas
    image = tab.library.open_image(arrived[0].asset_id)
    out = canvas.receive_picture(image, doc, "crop", "clipboard", "Clipboard")
    r.check("a picture handed in from Clipboard lands like a gallery send, one Undo away",
            "Received from Clipboard" in out[RECEIVE_STATUS] and "one Undo away" in out[RECEIVE_STATUS] and out[RECEIVE_STATE].has_image)
    out = canvas.receive_picture(None, doc, "crop", "clipboard", "Clipboard")
    r.check("and nothing to hand over is a sentence", "Clipboard had nothing to send" in out[RECEIVE_STATUS])


# --------------------------------------------------------------- fallbacks --


def enhance_switch_checks(r: Results, base: pathlib.Path, tab) -> None:
    """The switch decides the press, and a disagreement is repaired.

    A press used to ask the STORED setting whether to enhance, and the box on
    screen only wrote that setting through a change event. Lose the event - a
    click during a reload, a page opened before the setting moved, a second
    browser - and the box said off while every press enhanced, with nothing to
    tell the user why. A checkbox that does not decide is not a checkbox.

    Last, and with its own jobs, because these presses add to the same outbox
    the checks above read by position.
    """
    from minipaint_neo.clipboard import enhance as clipboard_enhance

    page_id = "e" * 16
    clipboard_enhance.set_enabled(True)
    r.check("the stored setting can be on while the box on screen says off", clipboard_enhance.enabled() is True)
    tab.add_to_queue("another prompt", page_id, None, False)
    queued = outbox.jobs()[-1]
    r.check("a press with the switch off is not enhanced, whatever the stored setting says",
            queued["enhance_requested"] is False and queued["enhance"] is None, str(queued["enhance_requested"]))
    r.check("and the disagreement is repaired rather than left to mislead the next press",
            clipboard_enhance.enabled() is False)

    # The other direction, and no enhancer here to run it: what matters is
    # that the press asked for it rather than consulting the setting.
    asked = {}
    original_enabled = clipboard_enhance.enabled
    clipboard_enhance.enabled = lambda: asked.setdefault("consulted", True) or False
    try:
        tab.add_to_queue("a third prompt", page_id, None, False)
    finally:
        clipboard_enhance.enabled = original_enabled
    r.check("a press that carries the switch never falls back to the stored setting to decide",
            outbox.jobs()[-1]["enhance_requested"] is False)

    # A caller that supplies no switch - anything that is not the tab - still
    # gets the stored setting, which is the contract the public API has.
    clipboard_enhance.set_enabled(False)
    r.check("and a caller with no switch is still decided by the setting",
            outbox.submit({"prompt": "x", "request_id": "0" * 32}, page_id)["enhance_requested"] is False)

def fallback_checks(r: Results) -> None:
    """A tab that cannot be built says so and takes nothing else with it."""
    print("  (the traceback below is this test breaking the Clipboard tab on purpose)")
    working = clipboard_ui.create_ui
    clipboard_ui.create_ui = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        tabs = clipboard_ui.on_ui_tabs()
        r.check("a broken Clipboard still gives one tab under the same label and id",
                len(tabs) == 1 and tabs[0][1] == clipboard_package.TAB_LABEL and tabs[0][2] == clipboard_package.TAB_ID)
        fallback = config_of(tabs[0][0])
        text = json.dumps(fallback)
        r.check("the fallback says the tab could not be built and names the error", "could not be built" in text and "RuntimeError: boom" in text)
        r.check("and that Mini Paint and WanGP are unaffected", "unaffected" in text)
        r.check("nothing of the real tab is mounted", "minipaint_clipboard_root" not in elem_ids(fallback))

        script_callbacks.callbacks["after_component"][:] = [host.on_after_component]
        host.reset_capture()
        broken_demo, _refs = forge_like.build_host(lambda: router.on_ui_tabs() + clipboard_ui.on_ui_tabs())
    finally:
        clipboard_ui.create_ui = working
    page = config_of(broken_demo)
    ids = elem_ids(page)
    r.check("the host's other tabs are intact around it", {"tab_minipaint", "tab_txt2img", "tab_img2img", "tab_settings", "tab_extensions", "tab_minipaint_clipboard"} <= ids)
    r.check("the Canvas is still there", "minipaint_canvas_surface" in ids)
    r.check("no dangling events after the failure", not dangling(page), str(dangling(page)[:2]))
    r.check("the gallery's Send to Mini Paint keeps its old behaviour when Clipboard is off",
            clipboard_package.intercept_enabled() is False)


# ------------------------------------------------------------------ theming --


def theming_checks(r: Results) -> None:
    """Only the host's theme variables decide a colour: night mode reaches everything."""
    css = (ROOT / "style.css").read_text(encoding="utf-8")
    marker = css.find(" * Clipboard tab")
    start = css.rfind("/*", 0, marker) if marker > 0 else -1
    r.check("the Clipboard rules are one block at the end of style.css", start > 0)
    block = re.sub(r"/\*.*?\*/", "", css[start:], flags=re.S)  # the comments say "white" only to forbid it
    r.check("scoped to the tab's root", "#minipaint_clipboard_root" in block and not re.search(r"^\.minipaint-clip", block, re.M))
    # A mask is not a colour decision. Only its alpha is used: what the user
    # sees is the background painted THROUGH it, and that background is
    # currentColor - the button's own text colour, which is the theme's. So
    # the paint inside a mask's own SVG says nothing about what anything looks
    # like, and the scan below skips it. The exclusion is guarded rather than
    # trusted: an icon that is not painted from currentColor fails the check
    # under it, which is the thing this section is really asserting.
    # To end of line, not to the next ";": a data URI contains one of its own
    # ("data:image/svg+xml;utf8,"), so a semicolon does not end the
    # declaration and a scan that assumed it did stopped inside the icon.
    painted = re.sub(r"(?m)^\s*(?:(?:-webkit-)?mask|--minipaint-clip-icon)\s*:.*$", "", block)
    r.check("no white, no #fff, no black as a colour",
            not re.search(r"(^|[^-\w])white\b(?!-space)", painted) and not re.search(r"#fff\b|#ffffff\b|#000\b|#000000\b|(^|[^-\w])black\b", painted, re.I))
    r.check("and an icon is painted with the button's own text colour, so its mask carries none",
            ("mask:" not in block) or "background-color: currentColor" in block)
    backgrounds = re.findall(r"background(?:-color)?\s*:\s*([^;]+);", block)
    r.check("every background is a theme variable, transparent, or the text colour",
            backgrounds and all(v.strip().startswith("var(--") or v.strip() in ("transparent", "none", "currentColor") for v in backgrounds), str([v for v in backgrounds if not v.strip().startswith("var(--")][:3]))
    colours = re.findall(r"(?<![-\w])color\s*:\s*([^;]+);", block)
    r.check("every text colour is a theme variable or inherited",
            colours and all(v.strip().startswith("var(--") or v.strip() in ("inherit", "currentColor", "transparent") for v in colours), str([v for v in colours if not v.strip().startswith("var(--")][:3]))
    gradio_vars = {
        "--body-background-fill", "--background-fill-primary", "--background-fill-secondary", "--block-background-fill",
        "--border-color-primary", "--border-color-accent-subdued", "--body-text-color", "--body-text-color-subdued",
        "--color-accent", "--block-radius", "--button-secondary-background-fill", "--button-secondary-background-fill-hover",
        "--button-secondary-text-color", "--error-text-color", "--shadow-drop-lg",
        # Gradio's typography and radius tokens, used by the enhancement panel
        # and the job cards' lines; a theme sets all three.
        "--font-mono", "--radius-sm", "--text-sm",
        # The pager's go-to-page box is a text input, and a theme fills one
        # the same way Gradio fills its own.
        "--input-background-fill",
    }
    used = set(re.findall(r"var\((--[\w-]+)", block))
    r.check("every variable is Gradio's own theme variable or the tab's own size",
            used and all(name in gradio_vars or name.startswith("--minipaint-clip-") for name in used), str(sorted(name for name in used if name not in gradio_vars and not name.startswith("--minipaint-clip-"))))
    r.check("and the ones a night theme recolours are among them", {"--background-fill-primary", "--block-background-fill", "--body-text-color", "--border-color-primary", "--color-accent"} <= used)
    r.check("a fallback colour is neutral, never white", all("255, 255, 255" not in v and "#fff" not in v.lower() for v in re.findall(r"var\(--[\w-]+,\s*([^)]+)\)", block)))
    r.check("the grid's thumbnail size is a CSS variable the browser sets", "--minipaint-clip-thumb" in block)
    r.check("and the two columns stack on a narrow tab", "@container" in block or "@media" in block)

    markup = (ROOT / "minipaint_neo" / "clipboard" / "ui.py").read_text(encoding="utf-8")
    r.check("the server-rendered markup carries no inline styles", "style=" not in markup)
    script = (ROOT / "browser" / "minipaint_clipboard.js").read_text(encoding="utf-8")
    r.check("the browser script sets no colours of its own", "#fff" not in script.lower() and not re.search(r"(^|[^-\w])white\b(?!-space)", script) and "backgroundColor" not in script)
    r.check("and never puts the prompt or a filename into the journal", "note(" in script and "request.prompt" not in script and not re.search(r'note\([^)]*\bname\b', script))
    r.check("the queue list, too, is theme variables only", "minipaint-clip-job" in block and "background: var(--" in block.split(".minipaint-clip-job {")[1].split("}")[0])


# ---------------------------------------------------------------------- run --


def run() -> Results:
    r = Results("clipboard ui")
    with tempfile.TemporaryDirectory(prefix="minipaint-clipboard-ui-") as scratch:
        base = pathlib.Path(scratch)
        wangp_config.use_config_dir(base / "data")
        config.use_config_dir(base / "data")
        process_log.use_log_dir(base / "logs")
        store.reset_for_tests()
        outbox.reset_for_tests()
        outbox.use_running(lambda: True)
        try:
            _page, tab = page_checks(r, base)
            if tab is not None:
                ids = browser_checks(r, base, tab)
                composer_checks(r, base, tab, ids)
                send_checks(r, base, tab)
                unrendered_destination_checks(r)
                integration_checks(r, base, tab)
                enhance_switch_checks(r, base, tab)
            fallback_checks(r)
            theming_checks(r)
        finally:
            outbox.reset_for_tests()
            wangp_config.use_config_dir(None)
            config.use_config_dir(None)
            process_log.use_log_dir(None)
            store.reset_for_tests()
    return r



def unrendered_destination_checks(r: Results) -> None:
    """A destination that was built but never put on the page.

    THE FAULT THIS IS FOR, in the shape it actually arrived in. A user could
    not send a picture to img2img - a canvas plainly on their screen - and
    nothing anywhere said why. Not a connection, not the queue, not a session:
    the send event named, among its outputs, one component that no page
    contained, and an event naming a component that is not there cannot run.
    Gradio does not complain about that. It simply never answers, on every
    build, for ever, while every other event on the same tab works perfectly.

    Two things are checked, because either alone would have let it through:

    *   a component that was made but never rendered is not offered as a
        destination at all - ``host.destinations`` asks Gradio's own
        ``is_rendered`` rather than assuming that having been handed a
        component means it is somewhere;

    *   and the send event names nothing from another tab whatever happens,
        so even a destination that slips through costs that destination
        instead of every send the tab makes. That is the part that turns this
        from a silent total failure into a named one.
    """
    from minipaint_neo.canvas import host as canvas_host

    class _Made:
        """A component the way Gradio leaves one that was never placed."""

        def __init__(self, elem_id, rendered):
            self.elem_id = elem_id
            self._id = abs(hash(elem_id)) % 100000
            self.is_rendered = rendered

    canvas_host.reset_capture()
    canvas_host._captured["stitch_txt2img"] = _Made("stitch_gallery", True)
    canvas_host._captured["stitch_txt2img_enable"] = _Made("stitch_enable", True)
    canvas_host._captured["stitch_img2img"] = _Made("stitch_gallery_2", False)
    canvas_host._captured["stitch_img2img_enable"] = _Made("stitch_enable_2", True)
    try:
        offered = canvas_host.destinations()
        r.check("a destination that was built but never put on the page is not offered",
                "stitch_img2img" not in offered, str(sorted(offered)))
        r.check("and neither is the box that went with it, rather than half of it",
                "stitch_img2img_enable" not in offered, str(sorted(offered)))
        r.check("while the one that IS on the page is still offered",
                offered.get("stitch_txt2img") is not None and offered.get("stitch_txt2img_enable") is not None,
                str(sorted(offered)))
    finally:
        canvas_host.reset_capture()

    # And the guarantee that does not depend on spotting it: whatever ends up
    # in the destinations, the send event never names one.
    demo, _refs, tab = build_page()
    page = config_of(demo)
    own = {component_of(page, f"minipaint_clipboard_{name}")["id"]
           for name in ("switch", "payload", "to_canvas", "status", "send_ack")}
    box = component_of(page, "minipaint_clipboard_send_request")["id"]
    sends = [d for d in page["dependencies"] if any(t[0] == box for t in d["targets"])]
    r.check("every event that carries a send names only this tab's own components",
            sends and all(set(d["outputs"]) <= own for d in sends),
            str([sorted(set(d["outputs"]) - own) for d in sends]))


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
