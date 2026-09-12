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
from minipaint_neo.clipboard import config, history, store  # noqa: E402
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


def _ids_in(grid_html: str) -> list:
    return re.findall(r'data-asset="([0-9a-f]{32})"', grid_html)


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

    needed = [
        "root", "body", "browser", "composer", "toolbar", "menu", "to_first", "to_last", "to_ref", "sort", "thumb",
        "grid", "status", "folder_panel", "folder", "folder_use", "folder_create", "folder_close", "folder_status",
        "rename_panel", "rename_text", "rename_ok", "rename_cancel", "delete_panel", "delete_ok", "delete_cancel",
        "paste_panel", "paste", "paste_close", "refresh", "upload", "intercept", "folder_open", "rename_open",
        "delete_open", "paste_open", "history_open", "selected", "sort_request", "slot_action", "send_request",
        "history_action", "menu_state", "switch", "payload", "to_canvas", "mask_clear", "wangp_line", "cards",
        "card_first", "card_last", "card_ref", "slot_upload_first", "slot_upload_last", "slot_upload_ref", "prompt",
        "queue", "queue_status", "queue_instruction", "queue_result", "history_panel", "history_list", "history_close",
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
    r.check("the grid, the cards and the history are server-rendered HTML components", kinds == {"html"}, str(kinds))
    for name, kind in (("sort", "dropdown"), ("thumb", "slider"), ("prompt", "textbox"), ("queue", "button"), ("upload", "uploadbutton"), ("paste", "image"), ("status", "markdown")):
        component = component_of(page, f"minipaint_clipboard_{name}")
        r.check(f"{name} is a Gradio {kind}", component["type"] == kind, component["type"])

    prompt = component_of(page, "minipaint_clipboard_prompt")["props"]
    r.check("the prompt says that empty means the WanGP prompt", prompt.get("placeholder") == "Use current WanGP prompt", str(prompt.get("placeholder")))
    r.check("and is a small multi-line box", prompt.get("lines") == 4)
    queue = component_of(page, "minipaint_clipboard_queue")["props"]
    r.check("Add to Queue is the primary button", queue.get("value") == "Add to Queue" and queue.get("variant") == "primary", str(queue))
    r.check("and it is enabled before anything is composed", queue.get("interactive") is not False and queue.get("visible") is not False)
    for name in ("selected", "sort_request", "slot_action", "send_request", "history_action", "menu_state", "switch", "payload",
                 "to_canvas", "mask_clear", "queue_instruction", "queue_result", "refresh", "upload", "intercept", "folder_open",
                 "rename_open", "delete_open", "paste_open", "history_open", "slot_upload_first", "slot_upload_last", "slot_upload_ref"):
        if component_of(page, f"minipaint_clipboard_{name}")["props"].get("visible") is not False:
            r.check(f"{name} is hidden - the menu and the cards are its face", False)
    r.check("no storage folder yet: the folder panel is open and the grid says so",
            component_of(page, "minipaint_clipboard_folder_panel")["props"].get("visible") is True
            and "No storage folder yet" in component_of(page, "minipaint_clipboard_grid")["props"].get("value", ""))
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
    r.check("Add to Queue is one backend event that arms the browser first",
            len(click) == 1 and click[0]["backend_fn"] and "armQueue" in (click[0].get("js") or ""), str(len(click)))
    r.check("with the prompt and the page session as its inputs, and the instruction, the status and the session as outputs",
            click and click[0]["inputs"] == [cid("prompt"), [c for c in page["components"] if c["type"] == "state" and c["id"] in click[0]["inputs"]][0]["id"]]
            and click[0]["outputs"][:2] == [cid("queue_instruction"), cid("queue_status")], str(click[0]["outputs"] if click else None))
    handoff = targeting("queue_instruction", "change")
    r.check("the instruction's change hands it to the browser script and to nothing on the server",
            len(handoff) == 1 and not handoff[0]["backend_fn"] and ".queue(" in (handoff[0].get("js") or "") and handoff[0]["outputs"] == [], str(handoff))
    result = targeting("queue_result", "input")
    r.check("the result box's input is the backend step that records history",
            len(result) == 1 and result[0]["backend_fn"] and result[0]["outputs"][:2] == [cid("queue_status"), cid("history_list")], str(result))
    r.check("the menu button is browser-only", all(not d["backend_fn"] and "toggleMenu" in (d.get("js") or "") for d in targeting("menu", "click")) and targeting("menu", "click"))
    r.check("the grid's re-render tells the browser script", any(".afterRender" in (d.get("js") or "") for d in targeting("grid", "change")))
    r.check("the thumbnail slider resizes in the browser as it moves and is saved when released",
            any("setThumbnailSize" in (d.get("js") or "") for d in targeting("thumb", "change")) and any(d["backend_fn"] for d in targeting("thumb", "release")))
    r.check("the prompt is saved into the draft when the box loses focus", any(d["backend_fn"] for d in targeting("prompt", "blur")))
    for name in ("to_first", "to_last", "to_ref"):
        r.check(f"{name} assigns the selection from the backend", any(d["backend_fn"] and d["inputs"] == [cid("selected")] for d in targeting(name, "click")))
    load_events = [d for d in deps if any(t[1] == "load" for t in d["targets"]) and ".attach()" in (d.get("js") or "")]
    r.check("the page's load event attaches the browser script", len(load_events) == 1, str(len(load_events)))

    # -- send out, the Canvas's own routes
    sent = targeting("send_request", "input")
    r.check("a send request is one backend event writing the host inputs, the instruction and the payload",
            len(sent) == 1 and sent[0]["backend_fn"] and {cid("switch"), cid("payload"), cid("to_canvas"), cid("status")} <= set(sent[0]["outputs"]), str(sent))
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
    r.check("before a folder is chosen, Refresh says to choose one", "Choose a storage folder" in out[1] and "No storage folder yet" in out[0])

    library_root = base / "library"
    folder_status, panel, *rest = tab.choose_folder(str(library_root), False)
    r.check("a folder that does not exist is not used behind the user's back", folder_status.startswith("**Not used.**") and "Create it" in folder_status and _visible(panel) and not library_root.exists())
    folder_status, panel, grid, status, selected, menu_state, *cards = tab.choose_folder(str(library_root), True)
    r.check("and is used once created on request", folder_status.startswith("**Using**") and not _visible(panel) and library_root.is_dir() and "0 images" in status, folder_status)
    r.check("the menu learns the folder is configured", json.loads(menu_state)["configured"] is True)
    r.check("the empty folder invites an upload, a paste, a send or the intercept", "No images yet" in grid and "Intercept" in grid)

    a = _write(base / "in" / "a.png", "PNG")
    b = _write(base / "in" / "b.jpg", "JPEG", (32, 24), (200, 10, 30))
    grid, status, selected, menu_state, *cards = tab.upload([a, b], "")
    r.check("Upload takes several files and says how many", status.startswith("Imported 2 images."), status)
    ids = _ids_in(grid)
    r.check("both are in the grid with opaque ids", len(ids) == 2 and all(protocol.valid_handoff_id(i) for i in ids) and 'data-count="2"' in grid, str(ids))
    r.check("the last import is selected", selected == ids[0] or selected in ids, selected)
    r.check("nothing on the page names the host folder", str(base) not in grid and str(base) not in status and str(base) not in menu_state and str(base) not in "".join(cards))
    assets = {asset.filename: asset for asset in tab.library.assets()}
    r.check("the files keep their names and bytes", set(assets) == {"a.png", "b.jpg"} and (library_root / "b.jpg").read_bytes() == pathlib.Path(b).read_bytes(), str(sorted(assets)))
    id_a, id_b = assets["a.png"].asset_id, assets["b.jpg"].asset_id

    text = base / "in" / "notes.txt"
    text.write_text("not an image", encoding="utf-8")
    grid, status, selected, menu_state, *cards = tab.upload([str(text)], id_a)
    r.check("a file that is not an image is refused with a sentence, and the selection is kept",
            status.startswith("Nothing was imported.") and "<small>" in status and selected == id_a, status)

    grid, menu_state = tab.sort_changed("name_asc", id_b)
    r.check("the sort dropdown reorders the grid and is remembered", _ids_in(grid) == [id_a, id_b] and config.load().sort == "name_asc" and 'minipaint-clip-selected' in grid)
    dropdown, grid, menu_state = tab.sort_request("name_desc:nonce", id_b)
    r.check("the menu's Sort submenu does the same and updates the dropdown", _value(dropdown) == "name_desc" and _ids_in(grid) == [id_b, id_a])
    dropdown, grid, menu_state = tab.sort_request("sideways", id_b)
    r.check("an unknown sort changes nothing", _value(dropdown) == "name_desc" and config.load().sort == "name_desc")
    r.check("the thumbnail size is saved, clamped", json.loads(tab.thumbnail_changed(200))["thumbnail"] == 200 and json.loads(tab.thumbnail_changed(5000))["thumbnail"] == config.THUMBNAIL_MAX)

    panel, textbox, status = tab.open_rename("")
    r.check("Rename with nothing selected says so", not _visible(panel) and "Select an image" in status)
    panel, textbox, status = tab.open_rename(id_a)
    r.check("Rename opens on the stem of the selected file", _visible(panel) and _value(textbox) == "a" and _skipped(status))
    panel, grid, status, *cards = tab.rename(id_a, "bad/name")
    r.check("a name with a separator is refused and the panel stays", _visible(panel) and status.startswith("Not renamed:"), status)
    panel, grid, status, *cards = tab.rename(id_a, "renamed")
    r.check("a good name renames the file, keeps its id and extension, and closes the panel",
            not _visible(panel) and status == "Renamed to renamed.png." and tab.library.get(id_a).filename == "renamed.png" and (library_root / "renamed.png").is_file())

    paste = _write(base / "in" / "pasted.jpg", "JPEG", (20, 20), (1, 2, 3))
    grid, status, selected, menu_state, *cards, image, panel = tab.pasted(paste, "")
    r.check("a paste through Gradio's own surface lands as a PNG, selected, and the panel closes",
            status.startswith("Pasted paste-") and status.endswith(".png.") and selected in _ids_in(grid) and _value(image) is None and not _visible(panel), status)
    grid, status, selected, menu_state, *cards, image, panel = tab.pasted("", id_a)
    r.check("an empty paste is a sentence and the selection is kept", status.startswith("Nothing was pasted.") and selected == id_a)
    pasted_id = next(asset.asset_id for asset in tab.library.assets() if asset.filename.startswith("paste-"))

    panel, status = tab.open_delete("")
    r.check("Delete with nothing selected says so", not _visible(panel) and "Select an image" in status)
    panel, status = tab.open_delete(pasted_id)
    r.check("Delete asks first", _visible(panel) and _skipped(status))
    panel, grid, status, selected, menu_state, *cards = tab.delete(pasted_id)
    r.check("and then removes the file, clears the selection and says so",
            not _visible(panel) and status.startswith("Deleted paste-") and selected == "" and pasted_id not in _ids_in(grid) and not (library_root / "paste").exists(), status)

    menu_state, status = tab.toggle_intercept()
    r.check("the intercept switch turns on from the menu", json.loads(menu_state)["intercept"] is True and config.load().intercept is True and "Clipboard" in status)
    menu_state, status = tab.toggle_intercept()
    r.check("and off again", json.loads(menu_state)["intercept"] is False and config.load().intercept is False and "Mini Paint again" in status)

    (base / "library" / "dropped.png").write_bytes(pathlib.Path(a).read_bytes())
    grid, status, selected, menu_state, *cards = tab.refresh(id_a)
    r.check("a file dropped into the folder by hand appears on Refresh", "3 images" in status and "dropped.png" in grid, status)
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
    grid, status, selected, menu_state, first, last, ref, = tab.slot_upload("ref", chosen, "")
    r.check("Choose a file on a card imports the file first and then assigns it",
            "minipaint-clip-card-override" in ref and status.startswith("Reference: ref.png.") and "imported into the folder first" in status and selected in _ids_in(grid), status)
    ref_id = history.load_draft()["reference_asset_ids"][0]
    r.check("the reference slot holds that new asset", tab.library.get(ref_id).filename == "ref.png")

    r.check("the prompt is kept in the draft as typed", tab.prompt_changed(" a prompt ") is None and history.load_draft()["prompt_override"] == " a prompt ")

    # -- Add to Queue
    session = {"pending": {}}
    instruction, status, session = tab.prepare_queue(" a prompt ", session)
    r.check("Add to Queue produces an instruction and says it is asking", instruction and "Asking WanGP" in status, status)
    parsed = json.loads(instruction)
    request = parsed["request"]
    r.check("the instruction carries a nonce and a public request", parsed.get("nonce") and protocol.valid_request_id(request.get("request_id")), str(parsed)[:80])
    r.check("the prompt is cleaned, the empty slot omitted, the filled ones named by asset id",
            request.get("prompt") == "a prompt" and "start" not in request["images"]
            and request["images"]["end"] == {"kind": "clipboard_asset", "id": id_b}
            and request["images"]["references"] == [{"kind": "clipboard_asset", "id": ref_id}], json.dumps(request))
    r.check("and never a path", str(base) not in instruction and "/" not in json.dumps(request["images"]))
    r.check("the page session remembers the recipe it sent", request["request_id"] in session["pending"] and session["pending"][request["request_id"]]["draft"]["last_asset_id"] == id_b)

    # -- the answer: a confirmed admission becomes history
    answer = {
        "nonce": parsed["nonce"], "request_id": request["request_id"], "ok": True, "status": "queued", "tasks_added": 1,
        "applied": {"prompt": True, "start": False, "end": True, "references": 1},
        "inherited": ["start"], "ignored": [], "model": {"type": "video", "label": "A video model"},
    }
    status, listing, session = tab.queue_result(json.dumps(answer), session)
    r.check("a queued answer says so", status.startswith("Added to WanGP queue."), status)
    records = history.load_history()
    r.check("and records one history entry", len(records) == 1 and records[0]["request_id"] == request["request_id"], str(len(records)))
    record = records[0] if records else {}
    r.check("with the recipe: prompt override, last frame and reference overrides, start inherited",
            record.get("prompt_mode") == "override" and record.get("prompt_override") == " a prompt " and record.get("first_mode") == "inherit"
            and record.get("last_mode") == "override" and record.get("last_asset_id") == id_b and record.get("reference_mode") == "override"
            and record.get("reference_asset_ids") == [ref_id] and record.get("tasks_added") == 1 and record.get("model_label") == "A video model", json.dumps(record))
    r.check("the history list shows it, by id and name, never by path",
            'data-history="' in listing and "b.jpg" in listing and "A video model" in listing and str(base) not in listing)
    r.check("the pending recipe is forgotten once answered", request["request_id"] not in session["pending"])

    # -- an ignored field is recorded as such
    instruction, status, session = tab.prepare_queue("", session)
    request2 = json.loads(instruction)["request"]
    r.check("an empty prompt is omitted from the request, not sent as empty", "prompt" not in request2)
    answer = {"request_id": request2["request_id"], "ok": True, "status": "queued", "tasks_added": 2,
              "applied": {"end": True}, "inherited": ["prompt", "start"], "ignored": [{"field": "references", "code": "RECEIVER_DISABLED"}], "model": {}}
    status, listing, session = tab.queue_result(json.dumps(answer), session)
    r.check("a field the model did not use is said so on the status line", "reference was not used by the current model" in status.lower(), status)
    newest = history.load_history()[0]
    r.check("and in the record", newest["reference_mode"] == "ignored" and newest["prompt_mode"] == "inherit" and newest["tasks_added"] == 2, json.dumps(newest))

    # -- refusals record nothing
    before = len(history.load_history())
    instruction, status, session = tab.prepare_queue("x", session)
    request3 = json.loads(instruction)["request"]
    status, listing, session = tab.queue_result(json.dumps({"request_id": request3["request_id"], "ok": False, "status": "refused", "code": "QUEUE_BUSY"}), session)
    r.check("a refusal shows the code's sentence and records nothing",
            status.startswith(errors.message(errors.QUEUE_BUSY)) and "no history was recorded" in status and _skipped(listing) and len(history.load_history()) == before, status)
    instruction, status, session = tab.prepare_queue("x", session)
    request4 = json.loads(instruction)["request"]
    status, listing, session = tab.queue_result(json.dumps({"request_id": request4["request_id"], "ok": False, "status": "unconfirmed"}), session)
    r.check("an unconfirmed admission is reported as unconfirmed, not as queued",
            status.startswith(errors.message(errors.ADMISSION_UNCONFIRMED)) and len(history.load_history()) == before, status)
    status, listing, session = tab.queue_result("this is not json", session)
    r.check("an unreadable answer is a refusal, not a crash", status.startswith(errors.message(errors.QUEUE_REQUEST_REFUSED)) and len(history.load_history()) == before)
    status, listing, session = tab.queue_result(json.dumps({"request_id": "0" * 32, "ok": True, "status": "queued", "tasks_added": 1}), session)
    r.check("a queued answer for a request this page never sent records nothing", len(history.load_history()) == before and "no history was recorded" in status)
    r.check("the session forgot none of the pending recipes it still owns", not session["pending"])

    # -- the composer with nothing in it still asks
    tab.slot_action("clear:last")
    tab.slot_action("clear:ref")
    instruction, status, session = tab.prepare_queue("", session)
    empty = json.loads(instruction)["request"]
    r.check("an empty composer still produces a request: the live WanGP page, as it is",
            instruction and set(empty) == {"request_id", "images"} and empty["images"] == {}, json.dumps(empty))

    # -- a slot whose file is gone fails before WanGP is asked
    tab.assign("first", id_a)
    (ids["root"] / "renamed.png").unlink()
    tab.library.refresh()
    instruction, status, session = tab.prepare_queue("", session)
    r.check("a missing image refuses before asking and keeps the draft",
            instruction == "" and status.startswith("First Frame: the image is no longer in the folder") and "nothing was asked of WanGP" in status
            and history.load_draft()["first_asset_id"] == id_a, status)
    first, last, ref = tab._cards(missing=["first"])
    r.check("the card can say Missing image", "minipaint-clip-card-missing" in first and "Missing image" in first)
    grid, status, selected, menu_state, first, last, ref = tab.refresh("")
    r.check("Refresh puts the slot back to Use WanGP and says why",
            "minipaint-clip-card-inherit" in first and "First Frame: the image is no longer in the folder" in status and history.load_draft()["first_asset_id"] == "", status)

    # -- history: load fills the composer, queues nothing; delete removes the record
    panel, listing = tab.show_history()
    r.check("Queue Send History opens with its list", _visible(panel) and 'data-history-action="load:' in listing)
    record = history.load_history()[-1]  # the first one queued: prompt " a prompt ", end b, ref
    before = len(history.load_history())
    prompt, first, last, ref, status, listing = tab.history_action(f"load:{record['history_id']}", "")
    r.check("Load puts the recipe back into the composer and queues nothing",
            prompt == " a prompt " and "minipaint-clip-card-override" in last and "minipaint-clip-card-override" in ref and status.startswith("Recipe loaded. Nothing was queued.")
            and history.load_draft()["last_asset_id"] == id_b and len(history.load_history()) == before, status)
    prompt, first, last, ref, status, listing = tab.history_action(f"delete:{record['history_id']}", "")
    r.check("Delete removes the record and touches no file",
            status.startswith("History entry deleted.") and len(history.load_history()) == before - 1 and (ids["root"] / "b.jpg").is_file())
    prompt, first, last, ref, status, listing = tab.history_action("load:nope", "")
    r.check("a record that is gone says so", "gone" in status and _skipped(prompt))
    tab.library.delete(id_b)
    remaining = history.load_history()[0]
    listing = tab._history()
    r.check("a record whose image was deleted says Missing image and still loads what it can",
            "Missing image" in listing and remaining["last_asset_id"] == id_b)
    prompt, first, last, ref, status, listing = tab.history_action(f"load:{remaining['history_id']}", "")
    r.check("with the missing slot back to Use WanGP, and named", "Missing image" not in last and "minipaint-clip-card-inherit" in last and "last" in status.lower())


# ---------------------------------------------------------------- send out --


def send_checks(r: Results, base: pathlib.Path, tab) -> None:
    """A picture leaves the browser the way it leaves the Canvas."""
    picture = _write(base / "in" / "send.png", "PNG", (40, 30), (9, 8, 7))
    grid, status, selected, *_rest = tab.upload([picture], "")
    n = len(tab.image_targets)
    if not selected:
        r.check("an upload to send", False, status)
        return
    out = tab.send(f"img2img:{selected}:1700000001", "")
    r.check("Send to img2img writes the instruction and a PNG payload, nothing into the backend targets",
            out[n] == "img2img" and str(out[n + 1]).startswith("data:image/png;base64,") and out[n + 2] == "" and all(_skipped(v) for v in out[:n])
            and out[n + 3].startswith("Sent send.png to img2img."), str(out[n + 3]))
    out = tab.send(f"inpaint:{selected}", "")
    r.check("Send to Inpaint names the size the Inpaint canvas must reach", out[n] == "inpaint:40x30" and str(out[n + 1]).startswith("data:image/png"))
    if "extras" in tab.image_targets:
        out = tab.send(f"extras:{selected}", "")
        index = tab.image_targets.index("extras")
        r.check("Send to Extras writes its image component directly, saved where the host serves it from",
                hasattr(out[index], "already_saved_as") and out[n] == "extras" and out[n + 1] == "")
    stitch = [key for key in tab.image_targets if key in canvas_ui.STITCH_TARGETS]
    if stitch:
        out = tab.send(f"{stitch[0]}:{selected}", "")
        index = tab.image_targets.index(stitch[0])
        r.check("Send to ImageStitch replaces its gallery with this one picture",
                isinstance(out[index], list) and len(out[index]) == 1 and "only reference image" in out[n + 3])
    out = tab.send(f"minipaint:{selected}:1700000002", "")
    r.check("Send to Mini Paint hands the asset to the Canvas's receive box and nothing to the host",
            out[n + 2].startswith(f"{selected}:") and out[n] == "" and out[n + 1] == "" and out[n + 3].startswith("Sent send.png to Mini Paint."), str(out[n + 2]))
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
    r.check("no white, no #fff, no black as a colour",
            not re.search(r"(^|[^-\w])white\b(?!-space)", block) and not re.search(r"#fff\b|#ffffff\b|#000\b|#000000\b|(^|[^-\w])black\b", block, re.I))
    backgrounds = re.findall(r"background(?:-color)?\s*:\s*([^;]+);", block)
    r.check("every background is a theme variable or transparent",
            backgrounds and all(v.strip().startswith("var(--") or v.strip() in ("transparent", "none") for v in backgrounds), str([v for v in backgrounds if not v.strip().startswith("var(--")][:3]))
    colours = re.findall(r"(?<![-\w])color\s*:\s*([^;]+);", block)
    r.check("every text colour is a theme variable or inherited",
            colours and all(v.strip().startswith("var(--") or v.strip() in ("inherit", "currentColor", "transparent") for v in colours), str([v for v in colours if not v.strip().startswith("var(--")][:3]))
    gradio_vars = {
        "--body-background-fill", "--background-fill-primary", "--background-fill-secondary", "--block-background-fill",
        "--border-color-primary", "--border-color-accent-subdued", "--body-text-color", "--body-text-color-subdued",
        "--color-accent", "--block-radius", "--button-secondary-background-fill", "--button-secondary-background-fill-hover",
        "--button-secondary-text-color", "--error-text-color", "--shadow-drop-lg",
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
    script = (ROOT / "javascript" / "minipaint_clipboard.js").read_text(encoding="utf-8")
    r.check("the browser script sets no colours of its own", "#fff" not in script.lower() and not re.search(r"(^|[^-\w])white\b(?!-space)", script) and "backgroundColor" not in script)
    r.check("and never puts the prompt or a filename into the journal", "note(" in script and "request.prompt" not in script and not re.search(r'note\([^)]*\bname\b', script))


# ---------------------------------------------------------------------- run --


def run() -> Results:
    r = Results("clipboard ui")
    with tempfile.TemporaryDirectory(prefix="minipaint-clipboard-ui-") as scratch:
        base = pathlib.Path(scratch)
        wangp_config.use_config_dir(base / "data")
        config.use_config_dir(base / "data")
        process_log.use_log_dir(base / "logs")
        store.reset_for_tests()
        try:
            _page, tab = page_checks(r, base)
            if tab is not None:
                ids = browser_checks(r, base, tab)
                composer_checks(r, base, tab, ids)
                send_checks(r, base, tab)
                integration_checks(r, base, tab)
            fallback_checks(r)
            theming_checks(r)
        finally:
            wangp_config.use_config_dir(None)
            config.use_config_dir(None)
            process_log.use_log_dir(None)
            store.reset_for_tests()
    return r


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
