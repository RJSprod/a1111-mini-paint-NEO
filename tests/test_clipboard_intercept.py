"""The gallery's send button, pointed at WanGP: the server half of the popup.

The design intent's regression list, on the Forge-shaped page and with no
browser: the intercept is a destination and the old switch migrates onto
it; each destination keeps its behaviour; a send pointed at WanGP freezes
the picked picture as a transient - never a Clipboard asset - and changing
the selection afterwards changes nothing about it; the popup is drawn from
Clipboard's own answer, whose roles come from the live page's input support
and whose default comes from Clipboard's mapping, never from a model name
in the popup; Generate uses the visible prompt and switch, overrides every
chosen role, inherits Clipboard's other inputs or not, revalidates the
roles, records one history entry and only on success; Cancel queues,
records and stores nothing; the history is pinnable, capped at a hundred
unpinned entries, and reloads against whatever the current mapping offers.
Then the routes on a bare app, and the two bundles read as text: the popup
names no model and no WanGP component, and the Clipboard menu offers the
three choices.
"""

from harness import Results, setup_path

setup_path()

import io  # noqa: E402
import json  # noqa: E402
import pathlib  # noqa: E402
import re  # noqa: E402
import tempfile  # noqa: E402

import forge_like  # noqa: E402,F401  (first: the metaclass patches)
from PIL import Image  # noqa: E402

from harness import ROOT  # noqa: E402
from minipaint_neo import clipboard as clipboard_package  # noqa: E402
from minipaint_neo import interop  # noqa: E402
from minipaint_neo.clipboard import config, enhance, executor, history, intercept, outbox, routes, store  # noqa: E402
from minipaint_neo.clipboard import ui as clipboard_ui  # noqa: E402
from minipaint_neo.wangp import config as wangp_config  # noqa: E402
from minipaint_neo.wangp import errors, process_log, protocol  # noqa: E402
from test_clipboard_enhance import FakeApi  # noqa: E402
from test_clipboard_ui import RECEIVE_STATE, RECEIVE_STATUS, _skipped, build_page, component_of, config_of, dangling  # noqa: E402

PAGE = "f" * 16

#: The live page's input support, in the shape the public API's
#: capabilities() answers it. Generic: which inputs exist, and nothing
#: about which model that is.
FIRST_AND_LAST = {"start": {"supported": True}, "end": {"supported": True}, "references": {"supported": False}}
ALL_THREE = {"start": {"supported": True}, "end": {"supported": True}, "references": {"supported": True}}
NO_IMAGE = {"start": {"supported": False}, "end": {"supported": False}, "references": {"supported": False}}

#: Model blocks for the one place a model family legitimately matters - the
#: DEFAULT role, which Clipboard's enhancer mapping decides. Built from the
#: enhancer's own constants rather than spelled here, so this suite names no
#: model family of its own; the popup's checks name none at all.
_H3 = f"{enhance.MODEL_KEY}_h3"
FRAMES_MODEL = {"type": f"{_H3}_{enhance.FL2VA}", "label": "frames model", "family": _H3, "architecture": f"{_H3}_{enhance.FL2VA}"}
REFERENCE_MODEL = {"type": f"{_H3}_{enhance.REF2VA}", "label": "reference model", "family": _H3, "architecture": f"{_H3}_{enhance.REF2VA}"}
PLAIN_MODEL = {"type": "some_video_model", "label": "A video model", "family": "", "architecture": ""}


def _photo(colour, size=(64, 48)):
    return Image.new("RGB", size, colour)


def _staged_of(handoff: str) -> str:
    parsed = intercept.parse_handoff(handoff)
    return parsed["token"] if parsed else ""


def _staged_path(token: str) -> pathlib.Path:
    return interop.staging_root() / (token + protocol.HANDOFF_SUFFIX)


def _write_asset(library, name, colour, size=(32, 24)):
    return library.import_image(_photo(colour, size), name, "upload")


# --------------------------------------------------------------- the setting --


def setting_checks(r: Results) -> None:
    """The destination, its migration, and the routes that carry it."""
    config.write_document(config.CONFIG_NAME, {"schema_version": 1, "intercept": False})
    r.check("legacy intercept=false reads as Mini Paint", clipboard_package.intercept_target() == config.INTERCEPT_MINIPAINT
            and clipboard_package.intercept_enabled() is False)
    config.write_document(config.CONFIG_NAME, {"schema_version": 1, "intercept": True})
    r.check("legacy intercept=true reads as Clipboard", clipboard_package.intercept_target() == config.INTERCEPT_CLIPBOARD
            and clipboard_package.intercept_enabled() is True)
    config.write_document(config.CONFIG_NAME, {"schema_version": 1, "intercept": True, "intercept_target": "wangp"})
    r.check("a stored destination wins over the switch beside it, and the switch reads false for WanGP",
            clipboard_package.intercept_target() == config.INTERCEPT_WANGP and clipboard_package.intercept_enabled() is False)
    r.check("the three destinations are the ones the design names",
            config.INTERCEPT_TARGETS == ("minipaint", "clipboard", "wangp"))
    answer = routes.apply_settings({"intercept": True})
    r.check("the settings route still takes the old switch and means Clipboard by it",
            answer["intercept_target"] == config.INTERCEPT_CLIPBOARD and answer["menu"]["intercept"] is True)
    answer = routes.apply_settings({"intercept_target": "wangp"})
    r.check("and the destination by name, answering the menu with all three choices",
            answer["intercept_target"] == "wangp" and answer["menu"]["intercept_target"] == "wangp"
            and [t for t, _l in answer["menu"]["intercepts"]] == list(config.INTERCEPT_TARGETS) and "WanGP" in answer["status"], str(answer)[:200])
    facts = routes.menu_facts()
    r.check("the menu facts the tab carries say the same", facts["intercept_target"] == "wangp" and facts["intercept"] is False)
    config.update(intercept_target=config.INTERCEPT_MINIPAINT)


# ------------------------------------------------------------------ the page --


def page_checks(r: Results):
    """The receive chain ends in one browser step that can open the popup."""
    demo, _refs, tab = build_page()
    page = config_of(demo)
    r.check("every event on the page resolves", not dangling(page), str(dangling(page)[:2]))
    box = component_of(page, "minipaint_clipboard_intercept")
    r.check("the intercept control is a hidden box the menu writes a destination into",
            box is not None and box["type"] == "textbox" and box["props"].get("visible") is False, str(box and box["type"]))
    deps = page["dependencies"]
    setting = [d for d in deps if any(t[0] == box["id"] and t[1] == "input" for t in d["targets"])] if box else []
    r.check("and writing it sets the destination on the server, answering the menu state and the status",
            len(setting) == 1 and setting[0]["backend_fn"]
            and setting[0]["outputs"] == [component_of(page, "minipaint_clipboard_menu_state")["id"], component_of(page, "minipaint_clipboard_status")["id"]],
            str(setting))
    menu = json.loads(component_of(page, "minipaint_clipboard_menu_state")["props"]["value"])
    r.check("the menu state names the destination, the three choices and the popup's bundle",
            menu["intercept_target"] == "minipaint" and [t for t, _l in menu["intercepts"]] == list(config.INTERCEPT_TARGETS)
            and str(menu.get("intercept_bundle", "")).startswith("/minipaint-assets/js/intercept.js"), str(menu)[:200])

    button = component_of(page, "txt2img_send_to_minipaint")
    r.check("the gallery's send button is on the page", button is not None)
    presses = [d for d in deps if button and any(t[0] == button["id"] and t[1] == "click" for t in d["targets"])]
    r.check("its press is one backend receive", len(presses) == 1 and presses[0]["backend_fn"], str(len(presses)))

    def followers(dep):
        return [d for d in deps if d.get("trigger_after") == dep["id"]]

    steps = [presses[0]] if presses else []
    while steps and followers(steps[-1]):
        steps.append(followers(steps[-1])[0])
    last = steps[-1] if steps else {}
    js = str(last.get("js") or "")
    r.check("the chain's last step is browser-only with no outputs, so it names nothing of another tab's",
            last and not last["backend_fn"] and last["outputs"] == [], str(last.get("outputs")))
    r.check("it opens the popup on a WanGP handoff, fetching the popup's own bundle, and switches tabs otherwise",
            'indexOf("wangp:") === 0' in js and "minipaintIntercept" in js and "/minipaint-assets/js/intercept.js" in js
            and ".switchTo(t)" in js and "receiveLanded" in js, js[:200])
    r.check("and brings the WanGP bridge and the queue API with it, for the live page's answer and the pump",
            "/minipaint-assets/js/wangp.js" in js and "/minipaint-assets/js/interop.js" in js)
    return tab


# --------------------------------------------------------------- the receive --


def receive_checks(r: Results, tab) -> None:
    """Each destination keeps its behaviour; WanGP freezes a transient."""
    canvas = tab.canvas
    library = tab.library
    photo = _photo((200, 30, 30))

    config.update(intercept_target=config.INTERCEPT_MINIPAINT)
    out = canvas.receive([(photo, None)], None, "crop", "txt2img")
    doc = out[RECEIVE_STATE]
    r.check("Mini Paint: a gallery send lands on the Canvas as before", "Received from txt2img" in out[RECEIVE_STATUS] and doc.has_image)
    r.check("and the follow-up step switches to the Canvas", canvas.after_receive(doc) == "canvas")

    config.update(intercept_target=config.INTERCEPT_CLIPBOARD)
    before = {asset.asset_id for asset in library.assets()}
    out = canvas.receive([(photo, None)], doc, "crop", "img2img")
    r.check("Clipboard: the same send goes into the library and leaves the Canvas alone",
            "Sent to Clipboard." in out[RECEIVE_STATUS] and _skipped(out[0]) and canvas.after_receive(doc) == "clipboard"
            and len({a.asset_id for a in library.assets()} - before) == 1, out[RECEIVE_STATUS])

    config.update(intercept_target=config.INTERCEPT_WANGP)
    before = {asset.asset_id for asset in library.assets()}
    staged_before = set(p.name for p in interop.staging_root().iterdir())
    size_before = doc.image.size
    out = canvas.receive([(photo, None)], doc, "crop", "txt2img")
    handoff = canvas.after_receive(doc)
    parsed = intercept.parse_handoff(handoff)
    r.check("WanGP: the send leaves the Canvas untouched and says where the picture went",
            _skipped(out[0]) and _skipped(out[1]) and "WanGP request popup" in out[RECEIVE_STATUS] and doc.image.size == size_before, out[RECEIVE_STATUS])
    r.check("the follow-up step is handed a WanGP handoff naming the frozen picture, its size and its tab",
            parsed is not None and parsed["width"] == 64 and parsed["height"] == 48 and parsed["tab"] == "txt2img", str(handoff))
    r.check("the picture is staged as a transient - one new file under the staging root",
            parsed is not None and _staged_path(parsed["token"]).is_file()
            and len(set(p.name for p in interop.staging_root().iterdir()) - staged_before) == 1)
    r.check("and NOT in the Clipboard library", {a.asset_id for a in library.assets()} == before)
    r.check("the handoff names no path", "/" not in handoff and str(interop.staging_root()) not in handoff)

    # Frozen: the picture the popup will submit is the one picked at the
    # press. Another press, on another picture, is another token; the first
    # still resolves to the first picture.
    other = _photo((30, 200, 30), (40, 30))
    canvas.receive([(other, None)], doc, "crop", "img2img")
    second = intercept.parse_handoff(canvas.after_receive(doc))
    first = interop.open_handle({"kind": "staged", "id": parsed["token"]})
    r.check("a second send freezes a second, distinct picture",
            second is not None and second["token"] != parsed["token"] and second["width"] == 40 and second["tab"] == "img2img")
    r.check("and changing the gallery's selection afterwards does not change the first frozen picture",
            first.size == (64, 48) and first.convert("RGB").getpixel((1, 1)) == (200, 30, 30))
    intercept.discard(second["token"])

    # A WanGP that cannot be prepared for passes the picture through to
    # the Canvas with the reason, like a Clipboard that cannot take it.
    original = intercept.stage

    def refusing(image, tab=""):
        raise errors.IntegrationError(errors.IMAGE_STAGE_INVALID, "no")

    intercept.stage = refusing
    try:
        out = canvas.receive([(photo, None)], doc, "crop", "txt2img")
    finally:
        intercept.stage = original
    r.check("a freeze that fails passes the picture through to the Canvas and says why",
            "Received from txt2img" in out[RECEIVE_STATUS] and "IMAGE_STAGE_INVALID" in out[RECEIVE_STATUS]
            and canvas.after_receive(out[RECEIVE_STATE]) == "canvas", out[RECEIVE_STATUS])
    r.check("the handoff text round-trips", intercept.parse_handoff(intercept.handoff_text("a" * 32, 10, 20, "extras"))
            == {"token": "a" * 32, "width": 10, "height": 20, "tab": "extras"})
    r.check("and anything that is not one is nothing",
            intercept.parse_handoff("canvas") is None and intercept.parse_handoff("wangp:nope") is None
            and intercept.parse_handoff("wangp:" + "b" * 32)["tab"] == "")
    return parsed["token"]


# ------------------------------------------------------------- capabilities --


def capability_checks(r: Results) -> None:
    """Roles from the live page's inputs; the default from Clipboard's mapping."""
    unknown = intercept.capabilities(None, None)
    r.check("with no live page the popup is offered every generic role, the first as default",
            [role["id"] for role in unknown["roles"]] == ["first_frame", "last_frame", "reference"]
            and unknown["default_roles"] == ["first_frame"] and unknown["known"] is False and unknown["inherit_supported"] is True, str(unknown))
    frames = intercept.capabilities(FRAMES_MODEL, FIRST_AND_LAST)
    r.check("a page taking a first and last frame offers those two, first frame the default",
            [role["id"] for role in frames["roles"]] == ["first_frame", "last_frame"] and frames["default_roles"] == ["first_frame"] and frames["known"] is True,
            str(frames["roles"]))
    r.check("with the labels the popup shows", [role["label"] for role in frames["roles"]] == ["First Frame", "Last Frame"])
    reference = intercept.capabilities(REFERENCE_MODEL, ALL_THREE)
    r.check("a page taking all three offers all three, the reference the default - from Clipboard's mapping, not a name here",
            [role["id"] for role in reference["roles"]] == ["first_frame", "last_frame", "reference"] and reference["default_roles"] == ["reference"],
            str(reference["default_roles"]))
    plain = intercept.capabilities(PLAIN_MODEL, ALL_THREE)
    r.check("a model the mapping does not know defaults to the first role on offer", plain["default_roles"] == ["first_frame"])
    r.check("the roles follow the inputs and not the model: the same model with other inputs offers other roles",
            [role["id"] for role in intercept.capabilities(REFERENCE_MODEL, FIRST_AND_LAST)["roles"]] == ["first_frame", "last_frame"])
    none = intercept.capabilities(PLAIN_MODEL, NO_IMAGE)
    r.check("a page that takes no image offers no role", none["roles"] == [] and none["default_roles"] == [])
    flat = intercept.capabilities(None, {"start": True, "end": False, "references": True})
    r.check("a flat support map is read too", [role["id"] for role in flat["roles"]] == ["first_frame", "reference"])
    kept, dropped = intercept.reconcile_roles(["reference"], frames)
    r.check("a chosen role the page no longer offers is dropped, and the default stands in only when nothing valid remains",
            kept == ["first_frame"] and dropped == ["reference"], f"{kept} {dropped}")
    kept, dropped = intercept.reconcile_roles(["last_frame", "reference", "first_frame"], frames)
    r.check("still-valid choices are kept in the roles' own order, the invalid one dropped",
            kept == ["first_frame", "last_frame"] and dropped == ["reference"], f"{kept} {dropped}")
    r.check("no choice at all is the default", intercept.reconcile_roles([], reference) == (["reference"], []))
    r.check("the role ids are the enhancer's slot ids, so one vocabulary serves both",
            intercept.ROLE_IDS == (enhance.SLOT_FIRST, enhance.SLOT_LAST, enhance.SLOT_REFERENCE))


# ----------------------------------------------------------- the popup's ask --


def describe_checks(r: Results, tab, token: str) -> None:
    """What the popup is told, and the shared state it starts from."""
    handoff = intercept.handoff_text(token, 64, 48, "txt2img")
    draft = history.load_draft()
    draft["prompt_override"] = "the shared prompt"
    history.save_draft(draft)
    enhance.set_enabled(True)
    config.update(intercept_inherit=False)
    outbox.use_running(lambda: False)
    outbox.use_executor(outbox.EXECUTOR_BROWSER)
    told = intercept.describe(handoff, None, None)
    r.check("the popup opens with Clipboard's prompt", told["ok"] and told["prompt"] == "the shared prompt", str(told.get("prompt")))
    r.check("and Clipboard's enhancer switch, with the enhancer's own availability line",
            told["enhance"]["enabled"] is True and told["enhance"]["state"] in ("blocked", "model", "unknown", "ready") and told["enhance"]["text"])
    r.check("the inheritance control starts as last used", told["inherit"]["default"] is False and told["inherit"]["supported"] is True)
    r.check("with WanGP not running the status says so and Generate follows the queue's own rule for a page-run job",
            told["wangp"]["state"] == "off" and told["wangp"]["running"] is False and told["generate"]["enabled"] is False, str(told["wangp"]))
    outbox.use_running(lambda: True)
    told = intercept.describe(handoff, None, None)
    r.check("with WanGP running the status is running or idle and Generate is a button",
            told["wangp"]["state"] in ("running", "idle") and told["generate"]["enabled"] is True, str(told["wangp"]))
    outbox.use_executor(outbox.EXECUTOR_SERVER)
    outbox.use_running(lambda: False)
    told = intercept.describe(handoff, None, None)
    r.check("under the unattended queue Generate is a button even while WanGP is off, and the status says the server starts it",
            told["generate"]["enabled"] is True and told["wangp"]["state"] == "off" and "starts it" in told["wangp"]["text"], str(told["wangp"]))
    outbox.use_running(lambda: True)
    r.check("the frozen picture is described by size and tab, never by path",
            told["image"] == {"width": 64, "height": 48, "tab": "txt2img"} and str(interop.staging_root()) not in json.dumps(told))
    r.check("the capabilities and the history travel with it", "roles" in told["capabilities"] and isinstance(told["history"], list))
    gone = intercept.describe(intercept.handoff_text("0" * 32, 1, 1, ""), None, None)
    r.check("a handoff whose picture is gone is refused by name", gone["ok"] is False and gone["code"] == errors.INTERCEPT_IMAGE_EXPIRED)
    r.check("and one that is not a handoff at all", intercept.describe("canvas")["code"] == errors.REQUEST_INVALID)
    data, mime = intercept.preview(token)
    small = Image.open(io.BytesIO(data))
    r.check("the preview is a small copy of the frozen picture", mime in ("image/webp", "image/png") and max(small.size) <= intercept.PREVIEW_SIDE
            and small.size[0] > small.size[1], f"{mime} {small.size}")
    enhance.set_enabled(False)
    config.update(intercept_inherit=True)


# ----------------------------------------------------------------- generate --


def submit_checks(r: Results, tab) -> None:
    """Generate builds the request Clipboard would, and records one entry."""
    library = tab.library
    forest = _write_asset(library, "forest.png", (10, 120, 10))
    dog = _write_asset(library, "dog.png", (120, 80, 10))
    draft = history.empty_draft()
    draft["first_asset_id"] = forest.asset_id
    draft["reference_asset_ids"] = [dog.asset_id]
    draft["prompt_override"] = "an old prompt"
    history.save_draft(draft)
    outbox.use_running(lambda: True)
    outbox.use_executor(outbox.EXECUTOR_BROWSER)
    enhance.set_enabled(False)

    def freeze(colour=(200, 30, 30), tab_name="txt2img"):
        return intercept.stage(_photo(colour), tab_name)

    # -- inheritance on: Clipboard's other inputs kept, the chosen role overridden
    handoff = freeze()
    token = _staged_of(handoff)
    assets_before = {asset.asset_id for asset in library.assets()}
    history_before = len(intercept.load_history())
    answer = intercept.submit(handoff, "a dog in the forest", ["reference"], True, False, PAGE, REFERENCE_MODEL, ALL_THREE)
    job = outbox.get(answer.get("job_id", ""))
    request = job["request"] if job else {}
    images = request.get("images", {})
    r.check("Generate stores one job from the gallery, pending for this page",
            answer["ok"] and job is not None and job["origin"] == outbox.ORIGIN_GALLERY and job["page"] == PAGE and job["state"] == "pending", str(answer)[:200])
    r.check("with inheritance on, Clipboard's first frame is kept and the gallery picture is the reference",
            images.get("start") == {"kind": "clipboard_asset", "id": forest.asset_id}
            and images.get("references") == [{"kind": "staged", "id": token}] and "end" not in images, json.dumps(images))
    r.check("the visible prompt is the one sent, and it became Clipboard's prompt",
            request.get("prompt") == "a dog in the forest" and history.load_draft()["prompt_override"] == "a dog in the forest")
    r.check("the request asks to start when WanGP can, like a composer press", request.get("start") == "auto")
    r.check("the visible switch decided: not enhanced", job["enhance_requested"] is False)
    r.check("no Clipboard asset was created by the send", {a.asset_id for a in library.assets()} == assets_before)
    r.check("one history entry was recorded, unpinned, with the recipe",
            len(intercept.load_history()) == history_before + 1 and intercept.load_history()[0]["roles"] == ["reference"]
            and intercept.load_history()[0]["inherit"] is True and intercept.load_history()[0]["prompt"] == "a dog in the forest"
            and intercept.load_history()[0]["pinned"] is False and intercept.load_history()[0]["job_id"] == job["job_id"], str(intercept.load_history()[0]))
    r.check("the entry's summary names roles, never a file or a path",
            "reference" in intercept.load_history()[0]["summary"] and "/" not in json.dumps(intercept.load_history()[0]))
    r.check("a page-run job keeps its frozen picture staged for the page to prepare from",
            answer["released"] is False and _staged_path(token).is_file())
    r.check("the answer tells the browser who runs the job", answer["instruction"]["executor"] == "browser" and answer["instruction"]["job_id"] == job["job_id"])
    r.check("the Queue names where it came from", any(b.get("text") == "from the gallery" for b in tab._outbox_view(PAGE)[0]["badges"]))
    r.check("Queue Send History leaves gallery sends to their own history", outbox.unrecorded(outbox.ORIGIN_CLIPBOARD) == [])

    # -- inheritance off: nothing of Clipboard's, the picture alone
    handoff = freeze((30, 30, 200))
    token = _staged_of(handoff)
    answer = intercept.submit(handoff, "just the picture", ["first_frame"], False, False, PAGE, REFERENCE_MODEL, ALL_THREE)
    images = outbox.get(answer["job_id"])["request"]["images"]
    r.check("with inheritance off, only the gallery picture goes, in the chosen role",
            images == {"start": {"kind": "staged", "id": token}}, json.dumps(images))
    r.check("the last inheritance choice is remembered for the next popup", config.load().intercept_inherit is False)

    # -- multi-role: the same picture in every chosen role
    handoff = freeze((90, 90, 90))
    token = _staged_of(handoff)
    answer = intercept.submit(handoff, "both ends", ["first_frame", "last_frame", "reference"], False, False, PAGE, REFERENCE_MODEL, ALL_THREE)
    images = outbox.get(answer["job_id"])["request"]["images"]
    r.check("the gallery picture overrides every chosen role",
            images.get("start") == {"kind": "staged", "id": token} and images.get("end") == {"kind": "staged", "id": token}
            and images.get("references") == [{"kind": "staged", "id": token}], json.dumps(images))

    # -- roles are judged against what the page offers NOW
    handoff = freeze((50, 60, 70))
    answer = intercept.submit(handoff, "moved model", ["reference"], False, False, PAGE, FRAMES_MODEL, FIRST_AND_LAST)
    images = outbox.get(answer["job_id"])["request"]["images"]
    r.check("a chosen role the page no longer offers is dropped, the default used, and the drop reported",
            answer["roles"] == ["first_frame"] and answer["dropped_roles"] == ["reference"] and "start" in images and "references" not in images
            and any("not offered" in note for note in answer["notes"]), str(answer.get("notes")))
    handoff = freeze((50, 60, 70))
    answer = intercept.submit(handoff, "text only", ["first_frame"], False, False, PAGE, PLAIN_MODEL, NO_IMAGE)
    r.check("a page taking no image refuses rather than sending a request the picture is not in",
            answer["ok"] is False and answer["code"] == errors.INTERCEPT_NO_IMAGE_ROLE and _staged_path(_staged_of(handoff)).is_file())
    intercept.discard(_staged_of(handoff))

    # -- the visible switch decides, and repairs the stored setting
    enhance.set_enabled(True)
    handoff = freeze((1, 2, 3))
    answer = intercept.submit(handoff, "switch off on screen", ["first_frame"], False, False, PAGE, REFERENCE_MODEL, ALL_THREE)
    r.check("Enhance unticked on screen is not enhanced, whatever the stored setting said, and the setting is repaired",
            answer["ok"] and outbox.get(answer["job_id"])["enhance_requested"] is False and enhance.enabled() is False)

    # -- rapid presses queue independently, in order
    ids = []
    for colour in ((1, 1, 1), (2, 2, 2), (3, 3, 3)):
        answer = intercept.submit(freeze(colour), "burst", ["first_frame"], False, False, PAGE, REFERENCE_MODEL, ALL_THREE)
        ids.append(answer.get("job_id"))
    listed = [job["job_id"] for job in outbox.jobs() if job["state"] == "pending" and job["origin"] == outbox.ORIGIN_GALLERY]
    r.check("three rapid sends are three independent pending jobs, in press order",
            all(ids) and len(set(ids)) == 3 and [job_id for job_id in listed if job_id in ids] == ids, str(ids))
    tokens = {outbox.get(job_id)["request"]["images"]["start"]["id"] for job_id in ids}
    r.check("each carrying its own frozen picture", len(tokens) == 3)

    # -- refusals store nothing and record nothing
    outbox.use_running(lambda: False)
    handoff = freeze((7, 7, 7))
    count, entries = len(outbox.jobs()), len(intercept.load_history())
    answer = intercept.submit(handoff, "while off", ["first_frame"], False, False, PAGE, REFERENCE_MODEL, ALL_THREE)
    r.check("with WanGP not running a page-run press is refused with the sentence, stores nothing and records nothing",
            answer["ok"] is False and answer["code"] == errors.WANGP_NOT_RUNNING and len(outbox.jobs()) == count
            and len(intercept.load_history()) == entries and answer["notes"], str(answer)[:200])
    r.check("and keeps the frozen picture, so the popup can try again", _staged_path(_staged_of(handoff)).is_file())
    outbox.use_running(lambda: True)
    library.delete(dog.asset_id)
    draft = history.load_draft()
    answer = intercept.submit(handoff, "missing slot", ["first_frame"], True, False, PAGE, REFERENCE_MODEL, ALL_THREE)
    r.check("a Clipboard slot whose file is gone refuses an inheriting request before storing, unless the picture overrides that slot",
            answer["ok"] is False and answer["code"] == errors.CLIPBOARD_ASSET_UNKNOWN and len(outbox.jobs()) == count
            and history.load_draft()["reference_asset_ids"] == draft["reference_asset_ids"], str(answer)[:160])
    answer = intercept.submit(handoff, "missing slot overridden", ["reference"], True, False, PAGE, REFERENCE_MODEL, ALL_THREE)
    r.check("which it does when the gallery picture is what fills it",
            answer["ok"] and outbox.get(answer["job_id"])["request"]["images"]["references"][0]["kind"] == "staged")

    # -- cancel: nothing at all, and the transient let go
    handoff = freeze((8, 8, 8))
    token = _staged_of(handoff)
    count, entries, assets_before = len(outbox.jobs()), len(intercept.load_history()), {a.asset_id for a in library.assets()}
    cancelled = intercept.cancel(handoff)
    r.check("Cancel releases the frozen picture and creates no job, no history entry and no asset",
            cancelled["ok"] and cancelled["released"] is True and not _staged_path(token).is_file() and len(outbox.jobs()) == count
            and len(intercept.load_history()) == entries and {a.asset_id for a in library.assets()} == assets_before)
    r.check("cancelling twice is harmless", intercept.cancel(handoff)["released"] is False)
    answer = intercept.submit(handoff, "after cancel", ["first_frame"], False, False, PAGE, REFERENCE_MODEL, ALL_THREE)
    r.check("and Generate on a cancelled picture is refused by name", answer["ok"] is False and answer["code"] == errors.INTERCEPT_IMAGE_EXPIRED)
    r.check("an edited prompt left by Cancel still reaches Clipboard", intercept.save_prompt("left behind")["ok"] and history.load_draft()["prompt_override"] == "left behind")

    # -- the server executor: the job owns its own copy, so the transient goes
    outbox.use_executor(outbox.EXECUTOR_SERVER)
    handoff = freeze((9, 9, 9))
    token = _staged_of(handoff)
    answer = intercept.submit(handoff, "unattended", ["first_frame"], False, False, PAGE, REFERENCE_MODEL, ALL_THREE)
    job = outbox.get(answer.get("job_id", ""))
    r.check("under the unattended queue the job is admitted to the server with a pinned copy of the picture",
            answer["ok"] and job is not None and job["executor"] == "server" and job["state"] == "admitted"
            and protocol.valid_handoff_id((job.get("inputs") or {}).get("start")), str(job and (job.get("state"), job.get("inputs"))))
    r.check("and the staged original is released, because the job no longer needs it",
            answer["released"] is True and not _staged_path(token).is_file() and "server" in answer["status"].lower())
    for stored in outbox.jobs():
        if stored["state"] in ("pending", "admitted"):
            try:
                outbox.cancel(stored["job_id"])
            except Exception:
                pass
    outbox.use_executor(outbox.EXECUTOR_BROWSER)


def enhancer_checks(r: Results, tab) -> None:
    """Enhance on: the enhancer's own path, with Clipboard's saved instructions."""
    fake = FakeApi()
    enhance.use_api(fake)
    try:
        enhance.set_override(enhance.REF2VA, enhance.MODE_IMAGE, "Describe the picture the way I like it.")
        outbox.use_running(lambda: True)
        outbox.use_executor(outbox.EXECUTOR_BROWSER)
        handoff = intercept.stage(_photo((4, 5, 6)), "txt2img")
        answer = intercept.submit(handoff, "a rough idea", ["reference"], False, True, PAGE, REFERENCE_MODEL, ALL_THREE)
        job = outbox.get(answer.get("job_id", ""))
        r.check("Enhance ticked on screen asks the writer first, and the job waits as enhancing",
                answer["ok"] and job is not None and job["state"] == "enhancing" and job["enhance_requested"] is True, str(answer)[:200])
        r.check("and the switch on screen turned the shared setting on", enhance.enabled() is True)
        submitted = fake.submissions[-1] if fake.submissions else {}
        r.check("the system prompt saved in Clipboard is what the writer runs under, never the default",
                submitted.get("system_prompt") == "Describe the picture the way I like it." and job["enhance"]["system_override"] is True, str(submitted)[:160])
        r.check("the frozen picture is the one described, as an object and not a path",
                submitted.get("images") == ["reference"] and hasattr(submitted.get("pictures", {}).get("reference"), "size")
                and "path" not in json.dumps({k: v for k, v in submitted.items() if k != "pictures"}, default=str), str(submitted.get("images")))
        r.check("the history entry says it was enhanced", intercept.load_history()[0]["enhance"] is True)
        handoff = intercept.stage(_photo((4, 5, 6)), "txt2img")
        count = len(outbox.jobs())
        answer = intercept.submit(handoff, "unsupported", ["first_frame"], False, True, PAGE, PLAIN_MODEL, ALL_THREE)
        r.check("a model the enhancer cannot write for refuses the press before storing, with the code",
                answer["ok"] is False and answer["code"] == errors.ENHANCE_MODEL_UNSUPPORTED and len(outbox.jobs()) == count
                and any("switch Enhance off" in note for note in answer["notes"]), str(answer)[:200])
        intercept.discard(_staged_of(handoff))
        for stored in outbox.jobs():
            if stored["state"] in ("pending", "enhancing"):
                try:
                    outbox.cancel(stored["job_id"])
                except Exception:
                    pass
    finally:
        enhance.clear_override(enhance.REF2VA, enhance.MODE_IMAGE)
        enhance.set_enabled(False)
        enhance.use_api(None)


# ------------------------------------------------------------------ history --


def history_checks(r: Results) -> None:
    """Load, delete, pin and unpin; the hundred-entry cap; reload against a new mapping."""
    config.write_document(intercept.HISTORY_NAME, {"schema_version": intercept.HISTORY_SCHEMA, "history": []})
    entries = []
    for index in range(105):
        entries.append(intercept.add_history({
            "prompt": f"prompt {index}", "enhance": index % 2 == 0, "inherit": True, "roles": ["reference"],
            "available_roles": ["first_frame", "last_frame", "reference"], "model": REFERENCE_MODEL, "summary": "s",
            "pinned": False, "created_at": f"2026-09-18T10:{index // 60:02d}:{index % 60:02d}+00:00",
        }))
    listed = intercept.load_history()
    r.check("unpinned entries are capped at a hundred, the oldest going first",
            len(listed) == intercept.MAX_UNPINNED == 100 and listed[0]["prompt"] == "prompt 104" and listed[-1]["prompt"] == "prompt 5", str(len(listed)))
    early = intercept.add_history({"prompt": "pinned early", "roles": ["first_frame"], "available_roles": ["first_frame"], "pinned": True,
                                   "created_at": "2026-09-18T09:00:00+00:00", "summary": "s", "inherit": False, "enhance": False})
    pinned_later = intercept.pin_history(listed[-1]["id"], True)
    r.check("Pin moves an entry into the pinned group at the top, however old it is",
            pinned_later is not None and intercept.load_history()[0]["pinned"] and intercept.load_history()[1]["pinned"]
            and {intercept.load_history()[0]["id"], intercept.load_history()[1]["id"]} == {early["id"], listed[-1]["id"]})
    for index in range(20):
        intercept.add_history({"prompt": f"later {index}", "roles": ["reference"], "available_roles": ["reference"], "pinned": False,
                               "created_at": f"2026-09-18T12:{index:02d}:00+00:00", "summary": "s", "inherit": True, "enhance": False})
    now = intercept.load_history()
    r.check("pinned entries survive the trimming that takes unpinned ones and do not count towards the cap",
            len(now) == 102 and sum(1 for e in now if e["pinned"]) == 2 and now[0]["pinned"] and now[1]["pinned"]
            and all(e["id"] in (early["id"], listed[-1]["id"]) for e in now[:2]) and "prompt 5" in [e["prompt"] for e in now]
            and "prompt 6" not in [e["prompt"] for e in now], str(len(now)))
    r.check("the two groups are each newest first",
            [e["prompt"] for e in now[2:5]] == ["later 19", "later 18", "later 17"])
    unpinned = intercept.pin_history(early["id"], False)
    r.check("Unpin returns an entry to the ordinary retention rules", unpinned is not None and unpinned["pinned"] is False
            and sum(1 for e in intercept.load_history() if e["pinned"]) == 1)
    before = [e["id"] for e in intercept.load_history()]
    r.check("Delete removes exactly that entry", intercept.delete_history(before[3]) and [e["id"] for e in intercept.load_history()] == before[:3] + before[4:])
    r.check("and says so for one that is gone", intercept.delete_history(before[3]) is False and intercept.pin_history("nope", True) is None)

    # -- Load: the recipe, reconciled with what is on offer now
    entry = intercept.add_history({"prompt": "recipe prompt", "enhance": True, "inherit": False, "roles": ["reference"],
                                   "available_roles": ["first_frame", "last_frame", "reference"], "model": REFERENCE_MODEL, "summary": "s", "pinned": False})
    enhance.set_enabled(False)
    config.update(intercept_inherit=True)
    loaded = intercept.recipe(entry["id"], REFERENCE_MODEL, ALL_THREE)
    r.check("Load restores the prompt, the enhancer state, the inheritance choice and the roles",
            loaded["ok"] and loaded["prompt"] == "recipe prompt" and loaded["enhance"] is True and loaded["inherit"] is False
            and loaded["roles"] == ["reference"] and loaded["dropped_roles"] == [], str(loaded)[:200])
    r.check("into the shared state: Clipboard's prompt and switch and the remembered choice",
            history.load_draft()["prompt_override"] == "recipe prompt" and enhance.enabled() is True and config.load().intercept_inherit is False)
    moved = intercept.recipe(entry["id"], FRAMES_MODEL, FIRST_AND_LAST)
    r.check("loaded under a mapping without that role, Clipboard drops it, names it, and ticks the current default",
            moved["roles"] == ["first_frame"] and [role["id"] for role in moved["dropped_roles"]] == ["reference"] and moved["defaulted"] is True, str(moved)[:200])
    view = intercept.history_view(FRAMES_MODEL, FIRST_AND_LAST)
    r.check("the list marks a saved role the current mapping does not offer",
            any(role["id"] == "reference" and role["valid"] is False for e in view for role in e["roles"]))
    r.check("and never carries a path", "/" not in json.dumps(view))
    try:
        intercept.recipe("0" * 16, None, None)
        r.check("loading an entry that is gone is refused", False)
    except errors.IntegrationError as error:
        r.check("loading an entry that is gone is refused", error.code == errors.REQUEST_INVALID)
    enhance.set_enabled(False)
    config.update(intercept_inherit=True)
    config.path_of(intercept.HISTORY_NAME).write_text("{broken", encoding="utf-8")
    r.check("a broken history document is moved aside and read as empty", intercept.load_history() == []
            and any(p.name.startswith(intercept.HISTORY_NAME + ".broken-") for p in config.config_dir().iterdir()))


# ------------------------------------------------------------------- routes --


def route_checks(r: Results) -> None:
    """The popup's one door, on a bare app."""
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    app = Starlette()
    routes.install(app)
    client = TestClient(app)
    outbox.use_running(lambda: True)
    outbox.use_executor(outbox.EXECUTOR_BROWSER)
    handoff = intercept.stage(_photo((11, 12, 13)), "txt2img")
    token = _staged_of(handoff)
    told = client.post(routes.INTERCEPT_ROUTE, json={"action": "describe", "handoff": handoff, "inputs": ALL_THREE})
    r.check("describe answers over HTTP with the roles and the shared state",
            told.status_code == 200 and told.json()["ok"] and [role["id"] for role in told.json()["capabilities"]["roles"]] == ["first_frame", "last_frame", "reference"],
            told.text[:200])
    picture = client.get(routes.INTERCEPT_IMAGE_ROUTE.replace("{token}", token))
    r.check("the frozen picture's preview is served by token, uncached",
            picture.status_code == 200 and picture.headers.get("content-type", "").startswith("image/") and "no-store" in picture.headers.get("cache-control", ""))
    r.check("and refused for a token that names nothing", client.get(routes.INTERCEPT_IMAGE_ROUTE.replace("{token}", "0" * 32)).status_code == 404)
    sent = client.post(routes.INTERCEPT_ROUTE, json={"action": "submit", "handoff": handoff, "prompt": "over http", "roles": ["first_frame"],
                                                     "inherit": False, "enhance": False, "page": PAGE, "inputs": ALL_THREE})
    r.check("submit stores the job and answers the instruction", sent.status_code == 200 and sent.json()["ok"] and sent.json()["instruction"]["job_id"], sent.text[:200])
    again = client.post(routes.INTERCEPT_ROUTE, json={"action": "submit", "handoff": handoff, "prompt": "x", "roles": ["first_frame"], "page": PAGE, "inputs": NO_IMAGE})
    r.check("a refusal is a 4xx carrying the code and the sentence", again.status_code == 400 and again.json()["code"] == errors.INTERCEPT_NO_IMAGE_ROLE)
    outbox.use_running(lambda: False)
    off = client.post(routes.INTERCEPT_ROUTE, json={"action": "submit", "handoff": handoff, "prompt": "x", "roles": ["first_frame"], "inherit": False,
                                                    "page": PAGE, "inputs": ALL_THREE})
    r.check("WanGP not running is the world's refusal, 409", off.status_code == 409 and off.json()["code"] == errors.WANGP_NOT_RUNNING,
            f"{off.status_code} {off.text[:160]}")
    outbox.use_running(lambda: True)
    listed = client.post(routes.INTERCEPT_ROUTE, json={"action": "history"}).json()
    r.check("history lists over HTTP", listed["ok"] and listed["history"] and listed["history"][0]["prompt"] == "over http")
    pinned = client.post(routes.INTERCEPT_ROUTE, json={"action": "history_pin", "id": listed["history"][0]["id"], "pinned": True}).json()
    r.check("pin over HTTP", pinned["ok"] and pinned["history"][0]["pinned"] is True)
    loaded = client.post(routes.INTERCEPT_ROUTE, json={"action": "history_load", "id": listed["history"][0]["id"], "inputs": FIRST_AND_LAST}).json()
    r.check("load over HTTP reconciles against the inputs sent", loaded["ok"] and loaded["roles"] == ["first_frame"])
    removed = client.post(routes.INTERCEPT_ROUTE, json={"action": "history_delete", "id": listed["history"][0]["id"]}).json()
    r.check("delete over HTTP", removed["ok"] and removed["removed"] is True)
    drafted = client.post(routes.INTERCEPT_ROUTE, json={"action": "draft", "prompt": "kept on close"}).json()
    r.check("an edited prompt is shared on close", drafted["ok"] and history.load_draft()["prompt_override"] == "kept on close")
    cancelled = client.post(routes.INTERCEPT_ROUTE, json={"action": "cancel", "handoff": handoff}).json()
    r.check("cancel over HTTP releases the picture", cancelled["ok"] and cancelled["released"] is True)
    r.check("an unknown action is refused", client.post(routes.INTERCEPT_ROUTE, json={"action": "explode"}).status_code == 400)
    r.check("every action the popup may send is one the route takes",
            set(routes.INTERCEPT_ACTIONS) == {"describe", "submit", "cancel", "draft", "history", "history_load", "history_delete", "history_pin"})
    for stored in outbox.jobs():
        if stored["state"] == "pending":
            outbox.cancel(stored["job_id"])


# ------------------------------------------------------------- the bundles --


def bundle_checks(r: Results) -> None:
    """The popup knows no model; the menu offers the three destinations."""
    popup = (ROOT / "browser" / "minipaint_intercept.js").read_text(encoding="utf-8")
    lowered = popup.lower()
    r.check("the popup bundle names no model family",
            not any(name in lowered for name in ("ref2va", "fl2va", "minimax", "ltx", "ideogram", "wan2.", "hunyuan")))
    r.check("nor a WanGP component, a bridge session or a path",
            not any(name in popup for name in ("generate_trigger", "add_to_queue_trigger", "wizard_prompt", "client_id", "bridge_session", "/file=")))
    r.check("it sends the roles it was given and asks for the roles it may show, by id",
            "reconcileRoles" in popup and "default_roles" in popup and '"roles"' not in popup.replace("roles:", ""))
    r.check("it sets no colour of its own", "#fff" not in lowered and not re.search(r"(^|[^-\w])white\b(?!-space)", popup) and "backgroundColor" not in popup)
    r.check("and never puts the prompt into the journal", not re.search(r"note\([^)]*prompt", popup))
    r.check("every request it makes is bounded", "AbortController" in popup and "ASK_TIMEOUT_MS" in popup)
    r.check("it is a bundle the asset route serves", "intercept" in __import__("minipaint_neo.assets", fromlist=["BUNDLES"]).BUNDLES)

    menu = (ROOT / "browser" / "minipaint_clipboard.js").read_text(encoding="utf-8")
    r.check("the Clipboard menu gains Intercept Options with the three destinations",
            "Intercept Options" in menu and '["minipaint", "Mini Paint"]' in menu and '["clipboard", "Clipboard"]' in menu and '["wangp", "WanGP"]' in menu)
    r.check("its direct route freezes the picture for WanGP when the queue is dead", "stageAndOpen" in menu and 'target === "wangp"' in menu)
    canvas = (ROOT / "browser" / "minipaint_canvas.js").read_text(encoding="utf-8")
    r.check("the Canvas lets the popup acknowledge a receive, so the twelve-second watch stands down", "receiveLanded: receiveLanded" in canvas)
    css = (ROOT / "style.css").read_text(encoding="utf-8")
    block = css[css.find(" * Send to WanGP popup"):css.find(" * Clipboard tab")]
    r.check("the popup's rules are their own block, before the Clipboard tab's, in theme variables",
            block and ".minipaint-intercept {" in block and "position: fixed" in block
            and all(v.strip().startswith("var(--") or v.strip() in ("transparent", "none", "currentColor")
                    for v in re.findall(r"background(?:-color)?\s*:\s*([^;]+);", block)))
    r.check("and it is compact: a fixed width under the button, never the window",
            "min(400px" in block and "100vh" not in block.split(".minipaint-intercept {")[1].split("}")[0].replace("max-height: calc(100vh - 24px)", ""))

    # A dialog is the top of the page, and this one was not. `z-index: 60` was
    # above everything this extension draws and below two things another
    # extension does: SD-Neo-ModelSwitchRefiner's focused workspace (1100) and
    # its assistant panel (1200). So the popup opened underneath whatever was
    # in front - reported as "I cannot open this menu in focus mode", which it
    # had been opening all along. The sibling repository has the mirror of this
    # check: its own layers must stay below this number.
    layer = re.search(r"--minipaint-dialog-layer:\s*(\d+)", css)
    r.check("the dialog layer is a named number, above every focus layer on the page",
            layer is not None and int(layer.group(1)) > 1200, layer.group(1) if layer else "absent")
    r.check("and the popup and its toast are drawn on it, not on a number of their own",
            "z-index: var(--minipaint-dialog-layer" in block
            and "calc(var(--minipaint-dialog-layer" in block)


# ---------------------------------------------------------------------- run --


def run() -> Results:
    r = Results("clipboard intercept")
    with tempfile.TemporaryDirectory(prefix="minipaint-intercept-") as scratch:
        base = pathlib.Path(scratch)
        wangp_config.use_config_dir(base / "data")
        config.use_config_dir(base / "data")
        process_log.use_log_dir(base / "logs")
        store.reset_for_tests()
        outbox.reset_for_tests()
        # No coordinator thread: a server-executed job admitted here stays
        # admitted, which is what the check about it is looking at.
        executor.reset_for_tests()
        executor.use_thread(False)
        outbox.use_running(lambda: True)
        outbox.use_executor(outbox.EXECUTOR_BROWSER)
        try:
            setting_checks(r)
            tab = page_checks(r)
            if tab is not None:
                folder = base / "library"
                folder.mkdir()
                tab.choose_folder(str(folder), True)
                token = receive_checks(r, tab)
                capability_checks(r)
                describe_checks(r, tab, token)
                submit_checks(r, tab)
                enhancer_checks(r, tab)
                history_checks(r)
                route_checks(r)
            bundle_checks(r)
        finally:
            outbox.reset_for_tests()
            executor.reset_for_tests()
            enhance.reset_for_tests()
            wangp_config.use_config_dir(None)
            config.use_config_dir(None)
            process_log.use_log_dir(None)
            store.reset_for_tests()
    return r


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
