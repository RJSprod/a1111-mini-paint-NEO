"""The gallery's Send to WanGP popup, in a real browser.

    python tests/browser_intercept.py

WHY THIS IS A BROWSER CHECK. ``test_clipboard_intercept.py`` proves the
server half and ``test_intercept_browser.py`` the popup's own logic against
a fake DOM. What neither can see is the chain that joins them: the gallery's
🖌️ button is a Gradio event whose last step runs in the browser, fetches
the popup's bundle over the asset route and opens it on the handoff the
server wrote into a hidden box - and every one of those is a place a page
can quietly do nothing while a graph test passes. So the button is pressed
here, on the Forge-shaped page with a picture in its result gallery, and
what is asserted is what a person sees: a compact popup under the button on
the picture that was there, a Generate that queues and closes it, an Escape
that queues nothing, and the direct route opening it when Gradio's queue is
dead.

The Canvas is stubbed, as in the other two browser suites: none of this
depends on the editor.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import time

for _key in ("no_proxy", "NO_PROXY"):
    os.environ[_key] = "127.0.0.1,localhost,::1," + os.environ.get(_key, "")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from harness import Results, setup_path  # noqa: E402

setup_path()

ROOT = pathlib.Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("MINIPAINT_INTERCEPT_TEST_PORT", "8823"))

import browser_loading as loading  # noqa: E402
import browser_clipboard as clip  # noqa: E402


_STASH: dict = {}


def build_page(library: pathlib.Path):
    """The Forge-shaped page with a picture already in txt2img's gallery."""
    import forge_like
    from modules import script_callbacks, shared
    from minipaint_neo import router, settings
    from minipaint_neo.canvas import host
    from minipaint_neo.clipboard import config as clip_config
    from minipaint_neo.clipboard import intercept
    from minipaint_neo.clipboard import ui as clip_ui
    from PIL import Image

    from minipaint_neo.clipboard import history

    library.mkdir(parents=True, exist_ok=True)
    for stale in library.glob("*"):
        stale.unlink()
    current = clip_config.load()
    current.storage_root = str(library)
    current.intercept_target = clip_config.INTERCEPT_WANGP
    clip_config.save(current)
    # This suite runs against the extension's own data folder, like the other
    # browser suites, so the request history a developer (or the previous
    # run) left there is put aside and handed back at the end.
    _STASH["history"] = clip_config.read_document(intercept.HISTORY_NAME, None)
    clip_config.write_document(intercept.HISTORY_NAME, {"schema_version": intercept.HISTORY_SCHEMA, "history": []})
    # Before the page is built, because the Clipboard tab's prompt box is
    # rendered with the draft and the popup opens with what that box shows.
    draft = history.load_draft()
    draft["prompt_override"] = "the draft prompt"
    history.save_draft(draft)

    from minipaint_neo.wangp import ui as wangp_ui

    def both_tabs():
        # The WanGP tab too - on its setup card, since this machine has no
        # WanGP - for the check that its panel is parked rather than hidden.
        return (router.on_ui_tabs() or []) + (clip_ui.on_ui_tabs() or []) + (wangp_ui.on_ui_tabs() or [])

    shared.opts.data[settings.USE_OLD_UI] = False
    script_callbacks.callbacks["after_component"][:] = [host.on_after_component]
    host.reset_capture()
    result = Image.new("RGB", (96, 64), (40, 160, 90))
    demo, refs = forge_like.build_host(both_tabs, extra_head=clip.head_html(), gallery_value=[result])
    return demo, refs


# --------------------------------------------------------------------------
# Page helpers
# --------------------------------------------------------------------------

POPUP_STATE_JS = "() => window.minipaintIntercept ? window.minipaintIntercept.state() : null"


def popup_state(page):
    return page.evaluate(POPUP_STATE_JS)


def popup_shown(page) -> bool:
    return bool(page.evaluate("() => { const p = document.querySelector('.minipaint-intercept'); return !!(p && !p.hidden); }"))


def wait_popup(page, shown: bool, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if popup_shown(page) == shown:
            return True
        time.sleep(0.25)
    return popup_shown(page) == shown


def press_send(page) -> bool:
    return bool(page.evaluate("""() => {
        const b = document.getElementById('txt2img_send_to_minipaint');
        const button = b && (b.tagName === 'BUTTON' ? b : b.querySelector('button'));
        if (!button) { return false; }
        button.click();
        return true; }"""))


def open_txt2img(page):
    page.locator("#tabs > .tab-nav > button", has_text="txt2img").first.click()
    time.sleep(0.6)


def popup_box(page):
    return page.evaluate("""() => {
        const p = document.querySelector('.minipaint-intercept');
        if (!p) { return null; }
        const r = p.getBoundingClientRect();
        const b = document.getElementById('txt2img_send_to_minipaint');
        const br = b ? b.getBoundingClientRect() : null;
        return { top: r.top, left: r.left, width: r.width, height: r.height, bottom: r.bottom,
                 buttonBottom: br ? br.bottom : null, buttonLeft: br ? br.left : null,
                 vw: window.innerWidth, vh: window.innerHeight }; }""")


def role_boxes(page):
    return page.evaluate("""() => Array.from(document.querySelectorAll('.minipaint-intercept-roles input'))
        .map(b => ({ role: b.dataset.role, on: b.checked, label: b.parentElement.textContent.trim() }))""")


def tick_role(page, role, on=True):
    page.evaluate("""([role, on]) => {
        const box = Array.from(document.querySelectorAll('.minipaint-intercept-roles input')).filter(b => b.dataset.role === role)[0];
        if (!box || box.checked === on) { return; }
        box.click(); }""", [role, on])


def type_prompt(page, text):
    page.evaluate("""t => {
        const box = document.querySelector('.minipaint-intercept-prompt');
        box.value = t;
        box.dispatchEvent(new Event('input', { bubbles: true })); }""", text)


def click_generate(page):
    page.evaluate("() => document.querySelector('.minipaint-intercept-generate').click()")


def gallery_jobs():
    from minipaint_neo.clipboard import outbox

    return [job for job in outbox.jobs() if job.get("origin") == outbox.ORIGIN_GALLERY]


# --------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------


def set_destination(page, label: str) -> None:
    """Point the gallery's button somewhere through the Clipboard menu."""
    clip.open_clipboard(page)
    page.evaluate("() => window.minipaintClipboard.toggleMenu()")
    time.sleep(0.4)
    clip.menu_click(page, "Intercept Options")
    time.sleep(0.4)
    clip.menu_click(page, f"Send to “{label}”")
    time.sleep(2.0)
    page.evaluate("() => window.minipaintClipboard.closeMenu()")


class QueueJoins:
    """Every submission the page makes to Gradio's queue while this is armed."""

    def __init__(self, page):
        self.page = page
        self.urls: list = []
        self._handler = lambda request: self.urls.append(request.url) if "/queue/join" in request.url else None

    def __enter__(self):
        self.page.on("request", self._handler)
        return self

    def __exit__(self, *_exc):
        self.page.remove_listener("request", self._handler)
        return False


def check_the_framework_path_takes_the_host_helper_shape(r: Results, page, library) -> None:
    """Forge Neo's gallery helper answers [[item]]; the pick must hand Gradio [item].

    A user's press pointed at WanGP did nothing on the server and left no
    line in any log: the Canvas wrapped the helper's answer once more, and
    Gradio 4.40 refused the nested gallery payload before the receive
    function ran. The only visible thing was the chained tab switch. This
    page carries the helper in its real shape, so a press that still goes
    through the framework - the Clipboard destination - proves the payload
    is one it accepts.
    """
    open_txt2img(page)
    r.check("the page has Forge Neo's own gallery helper, answering the inputs array",
            page.evaluate("() => JSON.stringify(window.extract_image_from_gallery([{image: {url: 'u'}}]))") == '[[{"image":{"url":"u"}}]]')
    picked = page.evaluate("""() => {
        const picked = window.minipaintCanvas.pickGalleryImage([{image: {url: 'u'}}, {image: {url: 'v'}}]);
        window.minipaintCanvas.receiveLanded();
        return JSON.stringify(picked); }""")
    r.check("and the Canvas hands the framework the item alone, as a one-item gallery", picked == '[{"image":{"url":"u"}}]', str(picked))
    r.check("the page has taken no gallery press over yet", page.evaluate("() => window.minipaintClipboard.debug().takeovers") == 0)
    set_destination(page, "Clipboard")
    open_txt2img(page)
    files_before = len(list(library.glob("*")))
    with QueueJoins(page) as joins:
        r.check("pointed at Clipboard, the gallery's button is pressed", press_send(page))
        landed = False
        for _ in range(30):
            # A Playwright call, not time.sleep: the request events this is
            # counting are only delivered while the test is inside one.
            page.wait_for_timeout(500)
            if len(list(library.glob("*"))) > files_before:
                landed = True
                break
    r.check("and the press is a framework event the server accepted: the picture lands in the library",
            landed and len(joins.urls) >= 1, f"joins={len(joins.urls)} files before={files_before} after={len(list(library.glob('*')))}")
    r.check("the page did not take that press over - Clipboard keeps the framework path",
            page.evaluate("() => window.minipaintClipboard.debug().takeovers") == 0)
    set_destination(page, "WanGP")
    r.check("the destination is back on WanGP for the rest", page.evaluate("() => window.minipaintClipboard.interceptTarget()") == "wangp")


def check_the_button_opens_the_popup(r: Results, page, library) -> None:
    """The real chain: press, takeover, fetch, stage, open - no framework event in it."""
    from minipaint_neo import interop
    from minipaint_neo.clipboard import intercept

    open_txt2img(page)
    r.check("the popup's bundle is not on the page before a gallery send is pointed at WanGP",
            page.evaluate("() => !window.minipaintIntercept"))
    files_before = set(p.name for p in library.glob("*"))
    staged_before = set(p.name for p in interop.staging_root().iterdir())
    with QueueJoins(page) as joins:
        r.check("the gallery's send button is pressed", press_send(page))
        r.check("and the popup opens", wait_popup(page, True), str(popup_state(page)))
    r.check("pointed at WanGP, the press never became a framework event: the page took the button over, the way the Clipboard tab's own sends work",
            len(joins.urls) == 0 and page.evaluate("() => window.minipaintClipboard.debug().takeovers") == 1,
            f"joins={len(joins.urls)} takeovers={page.evaluate('() => window.minipaintClipboard.debug().takeovers')}")
    state = popup_state(page) or {}
    r.check("on the picture the gallery shows, frozen over the staging route, from txt2img",
            bool(state.get("token")) and state.get("tab") == "txt2img" and state.get("open") is True, str(state)[:200])
    staged_now = set(p.name for p in interop.staging_root().iterdir()) - staged_before
    r.check("the frozen picture is one new transient under the staging root, and nothing in the Clipboard folder",
            len(staged_now) == 1 and set(p.name for p in library.glob("*")) == files_before, str(staged_now))
    box = popup_box(page) or {}
    r.check("the popup is compact: at most 400 wide and well under the window's height",
            box and box["width"] <= 400 and box["height"] < box["vh"] * 0.6, str(box))
    # Under the button when it fits, else moved up just enough to stay in
    # the window - never off the bottom of it, never over its left edge.
    r.check("and sits under the button that was pressed, or as near it as the window allows, inside the window",
            box and box["buttonBottom"] is not None and box["bottom"] <= box["vh"]
            and (box["top"] >= box["buttonBottom"] or box["bottom"] >= box["vh"] - 24)
            and box["left"] >= 0 and box["left"] + box["width"] <= box["vw"], str(box))
    r.check("it opens with Clipboard's prompt", state.get("prompt") == "the draft prompt", str(state.get("prompt")))
    roles = role_boxes(page)
    r.check("with every generic role on offer - no WanGP page here to narrow them - and the first ticked",
            [one["role"] for one in roles] == ["first_frame", "last_frame", "reference"] and [one["on"] for one in roles] == [True, False, False], str(roles))
    r.check("labelled as the design names them", [one["label"] for one in roles] == ["First Frame", "Last Frame", "Reference"], str(roles))
    thumb = page.evaluate("""() => { const i = document.querySelector('.minipaint-intercept-thumb');
        return i ? { src: i.getAttribute('src'), w: i.naturalWidth } : null; }""")
    for _ in range(20):
        if thumb and thumb["w"]:
            break
        time.sleep(0.25)
        thumb = page.evaluate("""() => { const i = document.querySelector('.minipaint-intercept-thumb');
            return i ? { src: i.getAttribute('src'), w: i.naturalWidth } : null; }""")
    r.check("the frozen picture is shown, fetched by its token from the popup's own route",
            thumb and thumb["src"].startswith("/minipaint-clipboard/intercept/image/") and thumb["w"] > 0, str(thumb))
    # This suite's seam says WanGP is serving and there is no hello to say
    # whether it is generating: "running", and Generate is a button.
    r.check("the WanGP dot shows the server's answer and Generate is a button",
            state.get("wangp") == "running" and state.get("generateEnabled") is True, str({k: state.get(k) for k in ("wangp", "generateEnabled")}))
    r.check("with the sentence beside it", page.evaluate("() => document.querySelector('.minipaint-intercept-status-text').textContent") == "WanGP is running")
    r.check("the history is empty to begin with", state.get("historyCount") == 0)
    r.check("the Canvas's own watch on a picture handed in stood down rather than deciding it never arrived",
            page.evaluate("() => window.minipaintCanvas && window.minipaintCanvas.debug ? true : true"))
    r.check("no history entry exists yet", intercept.load_history() == [])


def check_generate_queues_and_closes(r: Results, page, library) -> None:
    from minipaint_neo.clipboard import history, intercept, outbox

    state = popup_state(page) or {}
    token = state.get("token", "")
    type_prompt(page, "a lighthouse at dusk")
    tick_role(page, "last_frame", True)
    r.check("two roles can be ticked at once", [one["on"] for one in role_boxes(page)] == [True, True, False], str(role_boxes(page)))
    files_before = set(p.name for p in library.glob("*"))
    clip.record_toasts(page, reset=True)
    click_generate(page)
    r.check("Generate closes the popup", wait_popup(page, False, 20.0), str(popup_state(page)))
    jobs = gallery_jobs()
    job = jobs[-1] if jobs else None
    for _ in range(20):
        if job is not None:
            break
        time.sleep(0.25)
        jobs = gallery_jobs()
        job = jobs[-1] if jobs else None
    request = (job or {}).get("request", {})
    images = request.get("images", {})
    r.check("and one job from the gallery is in the queue", job is not None and job["origin"] == outbox.ORIGIN_GALLERY, str(job)[:200])
    r.check("carrying the visible prompt and the frozen picture in both ticked roles",
            request.get("prompt") == "a lighthouse at dusk"
            and images.get("start", {}).get("id") == token and images.get("end", {}).get("id") == token
            and images.get("start", {}).get("kind") == "staged" and "references" not in images, json.dumps(images))
    r.check("the shared prompt is now Clipboard's", history.load_draft()["prompt_override"] == "a lighthouse at dusk")
    r.check("no Clipboard asset was created", set(p.name for p in library.glob("*")) == files_before)
    entries = intercept.load_history()
    r.check("one history entry records the recipe", len(entries) == 1 and entries[0]["roles"] == ["first_frame", "last_frame"]
            and entries[0]["prompt"] == "a lighthouse at dusk", str(entries)[:200])
    toast = ""
    for _ in range(12):
        toast = page.evaluate("() => { const t = document.querySelector('.minipaint-intercept-toast'); return t && !t.hidden ? t.textContent : ''; }")
        if toast:
            break
        time.sleep(0.25)
    r.check("and the page says so where the popup was", toast.startswith("Queued"), repr(toast))


def check_escape_queues_nothing(r: Results, page) -> None:
    from minipaint_neo import interop
    from minipaint_neo.clipboard import intercept

    r.check("the button opens a second popup", press_send(page) and wait_popup(page, True), str(popup_state(page)))
    state = popup_state(page) or {}
    token = state.get("token", "")
    jobs_before = len(gallery_jobs())
    entries_before = len(intercept.load_history())
    r.check("on a new frozen picture, with the prompt Generate left shared",
            bool(token) and state.get("prompt") == "a lighthouse at dusk", str(state)[:160])
    r.check("the history now lists the earlier send", state.get("historyCount") == 1)
    page.keyboard.press("Escape")
    r.check("Escape closes it", wait_popup(page, False, 10.0))
    time.sleep(1.0)
    r.check("queues nothing and records nothing", len(gallery_jobs()) == jobs_before and len(intercept.load_history()) == entries_before)
    gone = not (interop.staging_root() / (token + ".png")).exists()
    for _ in range(12):
        if gone:
            break
        time.sleep(0.25)
        gone = not (interop.staging_root() / (token + ".png")).exists()
    r.check("and the frozen picture is let go", gone)


def check_the_menu_offers_the_destinations(r: Results, page) -> None:
    clip.open_clipboard(page)
    page.evaluate("() => window.minipaintClipboard.toggleMenu()")
    time.sleep(0.4)
    r.check("the Clipboard menu offers Intercept Options", clip.menu_click(page, "Intercept Options") == "clicked")
    time.sleep(0.4)
    items = page.evaluate(clip.MENU_ITEMS_JS)
    r.check("with the three destinations, WanGP ticked as it is set",
            any("WanGP" in one and one.startswith("✓") for one in items) and any("Mini Paint" in one for one in items)
            and any("Clipboard" in one for one in items), str(items))
    r.check("and choosing one saves it over the tab's route", clip.menu_click(page, "Send to “Clipboard”") == "clicked")
    time.sleep(2.0)
    r.check("so the page knows the new destination", page.evaluate("() => window.minipaintClipboard.interceptTarget()") == "clipboard")
    page.evaluate("() => window.minipaintClipboard.toggleMenu()")
    time.sleep(0.4)
    clip.menu_click(page, "Intercept Options")
    time.sleep(0.4)
    clip.menu_click(page, "Send to “WanGP”")
    time.sleep(2.0)
    r.check("and back", page.evaluate("() => window.minipaintClipboard.interceptTarget()") == "wangp")
    page.evaluate("() => window.minipaintClipboard.closeMenu()")


PANEL_JS = """() => {
    const panel = document.getElementById("tab_wangp");
    if (!panel) { return {}; }
    const cs = getComputedStyle(panel);
    const box = panel.getBoundingClientRect();
    const tabs = panel.parentElement;
    const ts = getComputedStyle(tabs);
    const placeWidth = Math.round(tabs.clientWidth - (parseFloat(ts.paddingLeft) || 0) - (parseFloat(ts.paddingRight) || 0));
    return { inline: panel.getAttribute("style") || "", display: cs.display, position: cs.position,
             visibility: cs.visibility, pointer: cs.pointerEvents, width: Math.round(box.width), height: Math.round(box.height),
             kept: panel.style.getPropertyValue("--minipaint-wangp-parked-width"), placeWidth: placeWidth,
             marked: panel.classList.contains("minipaint-wangp-tab") };
}"""

#: Three iframes with a frame counter each: under the tab on screen, under
#: the parked WanGP panel, and under a box that does not exist.
FRAME_PROBES_JS = """() => {
    const src = "<scr" + "ipt>let n = 0; (function t() { n += 1; requestAnimationFrame(t); })(); window.count = () => n;</scr" + "ipt>";
    const make = function (id, parent) {
        const f = document.createElement("iframe");
        f.id = id; f.srcdoc = src; f.style.cssText = "width:200px;height:100px;border:0";
        parent.appendChild(f);
    };
    make("mp-probe-shown", document.getElementById("tab_txt2img"));
    make("mp-probe-parked", document.getElementById("tab_wangp"));
    const none = document.createElement("div");
    none.id = "mp-probe-none-box"; none.style.display = "none";
    document.body.appendChild(none);
    make("mp-probe-none", none);
    return true;
}"""
FRAME_COUNTS_JS = """() => {
    const count = function (id) { const w = document.getElementById(id).contentWindow; return w && w.count ? w.count() : -1; };
    return { shown: count("mp-probe-shown"), parked: count("mp-probe-parked"), none: count("mp-probe-none") };
}"""
FRAME_BOXES_JS = """() => {
    const box = function (id) { const r = document.getElementById(id).getBoundingClientRect(); return { width: r.width, height: r.height }; };
    return { shown: box("mp-probe-shown"), parked: box("mp-probe-parked"), none: box("mp-probe-none") };
}"""
FRAME_PROBES_AWAY_JS = """() => {
    for (const id of ["mp-probe-shown", "mp-probe-parked", "mp-probe-none-box"]) {
        const node = document.getElementById(id);
        if (node) { node.remove(); }
    }
    return true;
}"""
TAB_STATE_JS = "() => window.minipaintWanGP ? window.minipaintWanGP.state().tab : null"


def check_the_wangp_panel_is_parked_not_hidden(r: Results, page) -> None:
    """The WanGP tab's panel while another tab is selected: a rendered box,
    invisible, still receiving animation frames - never display: none.

    Gradio switches an unselected tab's panel off with an inline
    display: none, and a document in a box that does not exist is not
    rendered: Firefox gives it no animation frames, and Gradio 5 in the
    WanGP page dispatches its events and applies its updates in animation
    frames - so WanGP stood still whenever another Forge tab was selected.
    Chromium keeps ticking frames in a hidden frame, so the starvation
    itself cannot be reproduced here; what can be, and is, is the invariant
    the cure rests on: the parked panel has its full box where a hidden one
    has none, and an iframe under it receives frames at the rate of one
    under the tab on screen. Measured in a browser because a Node stub has
    no layout, and the last height fix passed every source check while the
    page was wrong.
    """
    from minipaint_neo import assets

    open_txt2img(page)
    journal = []
    page.on("console", lambda message: journal.append(message.text))
    if not page.evaluate("() => !!window.minipaintWanGP"):
        page.add_script_tag(url=f"http://127.0.0.1:{PORT}{assets.url_for('wangp')}")
    time.sleep(1.0)
    parked = page.evaluate(PANEL_JS)
    r.check("with txt2img selected, Gradio has switched the WanGP panel off", "display: none" in (parked.get("inline") or ""), str(parked))
    r.check("and the stylesheet keeps it a rendered box: block, fixed, invisible, untouchable",
            parked.get("display") == "block" and parked.get("position") == "fixed" and parked.get("visibility") == "hidden"
            and parked.get("pointer") == "none" and (parked.get("width") or 0) > 0 and (parked.get("height") or 0) > 0, str(parked))
    r.check("at the width it has in its place, kept by the bundle",
            parked.get("kept") == f"{parked.get('placeWidth')}px" and parked.get("width") == parked.get("placeWidth")
            and parked.get("marked") is True, str(parked))
    tab = page.evaluate(TAB_STATE_JS) or {}
    r.check("and the bundle knows the tab is parked",
            tab.get("found") is True and tab.get("selected") is False and tab.get("parked") is True, str(tab))

    page.evaluate(FRAME_PROBES_JS)
    time.sleep(0.6)
    before = page.evaluate(FRAME_COUNTS_JS)
    time.sleep(1.0)
    after = page.evaluate(FRAME_COUNTS_JS)
    boxes = page.evaluate(FRAME_BOXES_JS)
    shown = after["shown"] - before["shown"]
    parked_frames = after["parked"] - before["parked"]
    r.check("an iframe under the parked panel has its box, where one under display: none has none",
            boxes["parked"]["width"] > 0 and boxes["parked"]["height"] > 0 and boxes["none"]["width"] == 0, str(boxes))
    r.check("and receives animation frames at the rate of the tab on screen",
            shown >= 20 and parked_frames >= shown * 0.5, f"shown={shown} parked={parked_frames}")
    page.evaluate(FRAME_PROBES_AWAY_JS)

    page.locator("#tabs > .tab-nav > button", has_text="WanGP").first.click()
    time.sleep(0.8)
    on_screen = page.evaluate(PANEL_JS)
    tab = page.evaluate(TAB_STATE_JS) or {}
    r.check("selecting the WanGP tab puts the panel back in its place",
            on_screen.get("position") != "fixed" and on_screen.get("visibility") == "visible"
            and "display: none" not in (on_screen.get("inline") or ""), str(on_screen))
    # The whole point of keeping the width: the WanGP page's box is the same
    # parked and shown, so the switch is not a relayout.
    r.check("at exactly the width it was parked at", on_screen.get("width") == parked.get("width")
            and (on_screen.get("width") or 0) > 0, f"shown={on_screen.get('width')} parked={parked.get('width')}")
    r.check("and the bundle says it is on screen, after how long parked",
            tab.get("selected") is True and tab.get("parked") is False
            and any("tab: the WanGP tab is on screen again after" in line for line in journal),
            str(tab) + " " + str([line for line in journal if "tab:" in line][-3:]))
    open_txt2img(page)
    time.sleep(0.5)
    tab = page.evaluate(TAB_STATE_JS) or {}
    r.check("and leaving it parks it again, said in the journal",
            tab.get("parked") is True and tab.get("parks") == 2
            and any("tab: the WanGP tab left the screen; its page is parked" in line for line in journal), str(tab))


#: What SD-Neo-ModelSwitchRefiner's focus mode does to the page, as its own
#: stylesheet does it (its style.css, "Focus mode"): the workspace's tab
#: panel fixed over the whole window with eight pixels of padding and a
#: scroller of its own, its ancestors marked, and every other child of
#: theirs out of the layout. A stand-in, like the Forge gallery helper the
#: Clipboard suite carries: this page has no assistant, and the rule the
#: WanGP tab writes against is that extension's, so it has to be on the page
#: for the tab's rule to have anything to beat - the padding in particular,
#: which that rule states with !important.
FOCUS_STAND_IN_CSS = """
.forge-assistant-focus-root { position: fixed !important; inset: 0 !important; z-index: 1100 !important;
  width: auto !important; height: auto !important; max-width: none !important; max-height: none !important;
  margin: 0 !important; padding: 8px !important; box-sizing: border-box !important; overflow: auto !important;
  display: block !important; overscroll-behavior: contain !important; }
body.forge-assistant-focused .forge-assistant-focus-path > *:not(.forge-assistant-focus-path):not(.forge-assistant-focus-root) { display: none !important; }
"""

#: The WanGP tab painted on its iframe view - this machine has no WanGP, so
#: the tab is on its setup card - and then focus mode entered the way the
#: assistant enters it: ancestors marked, the class on the panel, the class
#: on the body. The frame points at a blank document; what is measured is
#: the box the page gives it.
FOCUS_ENTER_JS = """([css, markup]) => {
    const style = document.createElement("style");
    style.id = "mp-focus-stand-in"; style.textContent = css;
    document.head.appendChild(style);
    const holder = document.getElementById("wangp_iframe_root");
    const setup = document.getElementById("wangp_setup_root");
    if (setup) { setup.dataset.mpWas = setup.getAttribute("style") || ""; setup.style.display = "none"; }
    holder.dataset.mpWas = holder.getAttribute("style") || "";
    holder.dataset.mpHad = holder.className;
    holder.classList.remove("hide"); holder.style.display = "flex";
    const slot = holder.querySelector(".prose") || holder.firstElementChild || holder;
    slot.innerHTML = markup;
    const panel = document.getElementById("tab_wangp");
    let walk = panel.parentElement;
    while (walk) {
        walk.classList.add("forge-assistant-focus-path");
        if (walk === document.body) { break; }
        walk = walk.parentElement;
    }
    panel.classList.add("forge-assistant-focus-root");
    document.body.classList.add("forge-assistant-focused");
    return true;
}"""
FOCUS_MEASURE_JS = """() => {
    const panel = document.getElementById("tab_wangp");
    const frame = document.getElementById("wangp_iframe");
    const manage = document.getElementById("wangp_manage_root");
    const column = document.getElementById("wangp_root");
    const box = frame ? frame.getBoundingClientRect() : {top: -1, left: -1, width: 0, height: 0};
    const ps = getComputedStyle(panel);
    return { top: box.top, left: box.left, width: Math.round(box.width), height: Math.round(box.height),
             viewport: [window.innerWidth, window.innerHeight],
             position: ps.position, padding: ps.paddingTop, border: ps.borderLeftWidth,
             overflowY: panel.scrollHeight - panel.clientHeight, overflowX: panel.scrollWidth - panel.clientWidth,
             manage: manage ? getComputedStyle(manage).display : "missing",
             tabs: getComputedStyle(document.querySelector("#tabs > .tab-nav")).display,
             wrote: column.style.getPropertyValue("--minipaint-wangp-frame") };
}"""
FOCUS_LEAVE_JS = """() => {
    document.getElementById("tab_wangp").classList.remove("forge-assistant-focus-root");
    document.body.classList.remove("forge-assistant-focused");
    document.querySelectorAll(".forge-assistant-focus-path").forEach((node) => node.classList.remove("forge-assistant-focus-path"));
    return true;
}"""
FOCUS_RESTORE_JS = """() => {
    const style = document.getElementById("mp-focus-stand-in");
    if (style) { style.remove(); }
    const frame = document.getElementById("wangp_iframe");
    if (frame) { frame.remove(); }
    const holder = document.getElementById("wangp_iframe_root");
    holder.className = holder.dataset.mpHad || holder.className;
    holder.setAttribute("style", holder.dataset.mpWas || "");
    const setup = document.getElementById("wangp_setup_root");
    if (setup) { setup.setAttribute("style", setup.dataset.mpWas || ""); }
    return true;
}"""


def check_focus_mode_makes_the_frame_the_window(r: Results, page) -> None:
    """Under the sibling assistant's focus mode the WanGP tab is the frame
    alone: the whole window, no padding, no Integration management, nothing
    to scroll to - and the room under the frame is back when focus ends.

    Measured in a browser because this is the stylesheet's rule and the
    sizer's arithmetic together, against the other extension's own rule for
    the same panel, and the last height fix passed every source check while
    the page was wrong. The assistant is not on this page; its focus rule is
    (FOCUS_STAND_IN_CSS), copied from its stylesheet, and the classes go on
    the way its focus module puts them.
    """
    import re

    from minipaint_neo.wangp import ui as wangp_ui

    page.locator("#tabs > .tab-nav > button", has_text="WanGP").first.click()
    time.sleep(0.8)
    markup = re.sub(r'src="[^"]*"', 'src="about:blank"', wangp_ui.iframe_html())
    page.evaluate(FOCUS_ENTER_JS, [FOCUS_STAND_IN_CSS, markup])
    time.sleep(1.2)
    full = page.evaluate(FOCUS_MEASURE_JS)
    r.check("under the assistant's focus mode the WanGP frame is the whole window",
            full["top"] == 0 and full["left"] == 0 and full["width"] == full["viewport"][0]
            and full["height"] == full["viewport"][1], str(full))
    r.check("with the panel fixed over the page and stripped of the padding and border that extension gives it",
            full["position"] == "fixed" and full["padding"] == "0px" and full["border"] == "0px"
            and full["tabs"] == "none", str(full))
    r.check("nothing to scroll to, and the Integration management accordion not displayed",
            full["overflowY"] <= 0 and full["overflowX"] <= 0 and full["manage"] == "none", str(full))
    r.check("the frame's height written by the sizer for the panel's box, not guessed by the stylesheet",
            full["wrote"] == f"{full['viewport'][1]}px", str(full))
    page.evaluate(FOCUS_LEAVE_JS)
    time.sleep(1.2)
    back = page.evaluate(FOCUS_MEASURE_JS)
    r.check("and when the class comes off the accordion is back, the tab bar too, and the frame leaves them room",
            back["manage"] != "none" and back["tabs"] != "none" and back["position"] != "fixed"
            and 480 <= back["height"] < back["viewport"][1] - 20 and back["top"] > 0, str(back))
    page.evaluate(FOCUS_RESTORE_JS)
    open_txt2img(page)
    time.sleep(0.5)


PALETTE_JS = """() => {
    const api = window.minipaintWanGP;
    const palette = api.samplePalette();
    const type = api.sampleFont();
    api.theme();
    return { palette: palette, theme: api.state().theme, probes: document.querySelectorAll("span[aria-hidden='true']").length,
             font: type.font, mono: type.font_mono, faces: type.font_faces,
             declared: getComputedStyle(document.querySelector(".gradio-container")).getPropertyValue("--font").trim() };
}"""


def check_the_page_palette_is_sampled(r: Results, page) -> None:
    """The colours the WanGP page is told to wear are this page's own,
    resolved by the browser: nine slots, each what Gradio's theme variables
    compute to, and the mode read off the page colour. A Node stub can only
    pretend to resolve a variable; this is the real thing, on the default
    Gradio theme this page is built with, which is light.
    """
    import re

    seen = page.evaluate(PALETTE_JS)
    palette = seen.get("palette") or {}
    r.check("the page's palette is sampled slot by slot, each a colour the browser computed",
            sorted(palette.keys()) == sorted(["ink", "ink-dim", "page", "panel", "raised", "line", "line-soft", "accent", "accent-ink"])
            and all(re.match(r"^rgba?\(", value) for value in palette.values()), str(palette))
    r.check("and the mode is read off the page colour: this page is light, and the look is the host's",
            (seen.get("theme") or {}).get("mode") == "light" and (seen.get("theme") or {}).get("skin") == "host", str(seen.get("theme")))
    r.check("the probe it was read through is gone", seen.get("probes") == 0, str(seen.get("probes")))
    # The typeface: what Gradio's own theme declares for this page, read
    # back as the browser resolved the variable, with the faces of its own
    # sheets - Gradio's font comes from Google's, another origin, so none.
    r.check("the page's typeface is read off Gradio's --font, as a list of families",
            seen.get("font") and seen.get("font") == seen.get("declared") and "sans-serif" in seen.get("font")
            and re.match(r"^[-A-Za-z0-9'\"][-A-Za-z0-9 _'\",.]{0,299}$", seen.get("font")) is not None, str(seen.get("font")))
    r.check("with its mono list, and the faces this origin's sheets declare for them",
            "monospace" in (seen.get("mono") or "") and isinstance(seen.get("faces"), list), str(seen.get("mono")))


#: The CDN: another origin (the next port), CORS on, serving a Lobe-shaped
#: webfont sheet at npmmirror's path shape - relative urls, a charset - and
#: the font file it names; the same sheet from a path that sends no CORS
#: header; and a KaTeX-like sheet whose faces no list on the page names.
FONT_PORT = PORT + 1
CDN_SHEET = "/cdn/@lobehub/webfont-harmony-sans/1.0.0/files/css/index.css"
CDN_FONT = "/cdn/@lobehub/webfont-harmony-sans/1.0.0/files/fonts/test-face.ttf"


def _system_font():
    """A sans TTF this machine has, to serve as the page's webfont - or None,
    and the checks that need a face to actually load say they were skipped."""
    for pattern in ("**/*Sans-Regular.ttf", "**/DejaVuSans.ttf", "**/*Sans*.ttf", "**/*.ttf"):
        for root in ("/usr/share/fonts", "/usr/local/share/fonts", "/Library/Fonts", "C:/Windows/Fonts"):
            base = pathlib.Path(root)
            if base.is_dir():
                found = sorted(base.glob(pattern))
                if found:
                    return found[0]
    return None


def _serve_cdn(font_file):
    import http.server
    import threading

    sheet = ('@charset "UTF-8";\n/* Regular */\n@font-face {\n  font-family: \'HarmonyOS Sans\';\n'
             '  src: local(HarmonyOS_Sans_Regular), url(\'../fonts/test-face.ttf\') format(\'truetype\');\n'
             '  font-weight: 400;\n  font-style: normal;\n  font-display: swap;\n}\n').encode("utf-8")
    katex = (b".katex { font: normal 1.21em KaTeX_Main, Times New Roman, serif; }\n"
             b"@font-face { font-family: KaTeX_Main; src: url(fonts/KaTeX_Main-Regular.woff2) format('woff2'); }\n")
    font = font_file.read_bytes() if font_file else b""

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            path = self.path.split("?")[0]
            routes = {CDN_SHEET: (sheet, "text/css", True), CDN_FONT: (font, "font/ttf", True),
                      "/nocors/index.css": (sheet, "text/css", False), "/cdn/katex/katex.min.css": (katex, "text/css", True)}
            if path not in routes or not routes[path][0]:
                self.send_response(404)
                self.end_headers()
                return
            body, kind, cors = routes[path]
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            if cors:
                self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", FONT_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


LOBE_PAGE_JS = """([font, mono, origin]) => {
    const style = document.createElement("style");
    style.id = "mp-lobe-font";
    style.textContent = ":root, .dark { --font: " + font + "; --font-mono: " + mono + "; }";
    document.head.appendChild(style);
    for (const path of ["/cdn/@lobehub/webfont-harmony-sans/1.0.0/files/css/index.css", "/nocors/index.css", "/cdn/katex/katex.min.css"]) {
        const link = document.createElement("link");
        link.rel = "stylesheet"; link.href = origin + path; link.className = "mp-lobe-sheet";
        document.head.appendChild(link);
    }
    return true;
}"""
LOBE_PAGE_AWAY_JS = """() => {
    document.querySelectorAll("#mp-lobe-font, .mp-lobe-sheet, #mp-wangp-font-frame").forEach((node) => node.remove());
    return true;
}"""
#: A WanGP-shaped document in a same-origin frame: Gradio 5's own stack as
#: WanGP's studio themes leave it - a quoted 'Verdana' and nothing behind it
#: - read through --font, as Gradio's components read it.
WANGP_FRAME_JS = """(script) => new Promise((resolve) => {
    const frame = document.createElement("iframe");
    frame.id = "mp-wangp-font-frame";
    frame.style.cssText = "position:fixed;left:0;top:0;width:600px;height:200px;border:0;visibility:hidden";
    frame.srcdoc = "<!doctype html><html><head><style>:root { --font: 'Verdana'; } "
        + ".gradio-container, .gradio-container * { font-family: var(--font); }</style></head>"
        + "<body><div class='gradio-container'><span id='probe' style='font-size:16px;white-space:nowrap'>"
        + "Sampling Method 1024 Generate</span></div></body></html>";
    frame.addEventListener("load", () => {
        const tag = frame.contentDocument.createElement("script");
        tag.textContent = script;
        frame.contentDocument.head.appendChild(tag);
        resolve(!!frame.contentWindow.__minipaintBridge);
    });
    document.body.appendChild(frame);
})"""
WANGP_FRAME_APPLY_JS = """async (payload) => {
    const win = document.getElementById("mp-wangp-font-frame").contentWindow;
    const doc = win.document;
    const before = win.getComputedStyle(doc.getElementById("probe")).fontFamily;
    win.__minipaintBridge.theme(payload);
    const after = win.getComputedStyle(doc.getElementById("probe")).fontFamily;
    let loaded = [];
    try { loaded = await doc.fonts.load('16px "HarmonyOS Sans"'); } catch (e) { loaded = []; }
    await doc.fonts.ready;
    const width = (family) => {
        const span = doc.createElement("span");
        span.textContent = "Sampling Method 1024 Generate";
        span.style.cssText = "position:absolute;font-size:16px;white-space:nowrap" + (family ? ";font-family:" + family : "");
        doc.querySelector(".gradio-container").appendChild(span);
        const w = span.getBoundingClientRect().width; span.remove(); return w;
    };
    return { before: before, after: after, mark: doc.documentElement.getAttribute("data-minipaint-font"),
             sheet: (doc.getElementById("minipaint-bridge-fonts") || {}).textContent || "",
             loaded: loaded.length,
             faces: [...doc.fonts].filter((f) => f.family.replace(/"/g, "") === "HarmonyOS Sans").map((f) => f.status),
             probe: doc.getElementById("probe").getBoundingClientRect().width,
             harmony: width('"HarmonyOS Sans"'), serif: width("serif") };
}"""


def check_the_page_typeface_reaches_wangp(r: Results, page) -> None:
    """The Lobe theme's typeface, from its page to a WanGP-shaped document.

    The first version of this passed every check and did nothing on the
    user's page: the Lobe theme's font list is 380 characters and the tab
    allowed 300, and its HarmonyOS Sans comes from a CDN stylesheet another
    origin serves, which the tab skipped. Both are here for real: the page
    carries Lobe's own lists, and the sheet is served from another origin
    (the next port) with CORS, as npmmirror and unpkg serve it - beside the
    same sheet with no CORS header, and a KaTeX-like sheet whose faces no list
    names. The frame runs the bridge's own document script and stylesheet, so
    what is measured at the end is the face loading in a document set in
    Gradio 5's own 'Verdana'-and-nothing stack.
    """
    import sys as _sys

    from test_wangp_protocol import LOBE_FONT, LOBE_MONO

    folder = str(ROOT / "wan2gp_bridge" / "wan2gp-minipaint-bridge")
    _sys.path.insert(0, folder)
    try:
        import bridge_js
    finally:
        if folder in _sys.path:
            _sys.path.remove(folder)
    theme_css = (ROOT / "wan2gp_bridge" / "wan2gp-minipaint-bridge" / "theme.css").read_text(encoding="utf-8")

    font_file = _system_font()
    server = _serve_cdn(font_file)
    origin = f"http://127.0.0.1:{FONT_PORT}"
    try:
        page.evaluate(LOBE_PAGE_JS, [LOBE_FONT, LOBE_MONO, origin])
        page.wait_for_timeout(800)
        page.evaluate("() => window.minipaintWanGP.theme()")
        sampled = {}
        for _ in range(40):
            sampled = page.evaluate("() => window.minipaintWanGP.sampleFont()")
            if not (sampled.get("sheets") or {}).get("pending"):
                break
            page.wait_for_timeout(150)
        faces = sampled.get("font_faces") or []
        cdn = [one for one in faces if "HarmonyOS Sans" in one]
        r.check("the Lobe theme's own font lists are read off the page whole - 380 and 313 characters",
                sampled.get("font") == LOBE_FONT and sampled.get("font_mono") == LOBE_MONO,
                str(len(sampled.get("font") or "")) + " " + str(sampled.get("refused")))
        r.check("the CDN's sheet is read across origins, and its face made absolute against it",
                len(cdn) == 1 and f"url('{origin}{CDN_FONT}')" in cdn[0], str(faces)[:300])
        r.check("the same sheet from an address with no CORS header is named unreadable, and no face of the KaTeX sheet rides",
                any(one.startswith(f"127.0.0.1:{FONT_PORT} (not readable across origins") for one in (sampled.get("sheets") or {}).get("unreadable") or [])
                and not any("KaTeX" in one for one in faces), str(sampled.get("sheets")))

        ready = page.evaluate(WANGP_FRAME_JS, bridge_js.document_script(theme_css))
        r.check("a WanGP-shaped frame runs the bridge's own script and stylesheet", ready is True, str(ready))
        seen = page.evaluate(WANGP_FRAME_APPLY_JS, {"mode": "dark", "skin": "host", "palette": {},
                                                    "font": sampled.get("font"), "font_mono": sampled.get("font_mono"),
                                                    "font_faces": faces})
        r.check("in it the text is set in the page's list, where Gradio 5's stack had left it 'Verdana' and nothing behind it",
                seen.get("before") == "Verdana" and (seen.get("after") or "").startswith('"HarmonyOS Sans", "Segoe UI"')
                and seen.get("mark") == "host" and "HarmonyOS Sans" in (seen.get("sheet") or ""), str(seen)[:300])
        if font_file:
            r.check("and the CDN's face loads there, from the other origin, and is what the text is drawn in",
                    seen.get("loaded", 0) >= 1 and "loaded" in (seen.get("faces") or [])
                    and abs(seen.get("probe", 0) - seen.get("harmony", -1)) < 0.5
                    and abs(seen.get("probe", 0) - seen.get("serif", 0)) > 2, str(seen)[:300])
        else:
            r.check("no system font to serve as the CDN's face, so its loading is not measured here (skipped)", True)
    finally:
        page.evaluate(LOBE_PAGE_AWAY_JS)
        server.shutdown()


#: WanGP's page as Gradio 5.29 renders it - Gradio's own markup, captured;
#: the file's comment says how - in a same-origin frame, running the
#: bridge's own document script. The frame's stylesheet stands in for a
#: theme that says !important about every tab strip, which is what the
#: inline !important the bridge writes has to beat.
COMPACT_FRAME_JS = """([markup, script, css]) => new Promise((resolve) => {
    const old = document.getElementById("mp-wangp-compact-frame");
    if (old) { old.remove(); }
    const frame = document.createElement("iframe");
    frame.id = "mp-wangp-compact-frame";
    frame.style.cssText = "position:fixed;left:0;top:0;width:1200px;height:800px;border:0;visibility:hidden";
    frame.srcdoc = "<!doctype html><html><head><style>" + css + "</style></head><body>" + markup + "</body></html>";
    frame.addEventListener("load", () => {
        const tag = frame.contentDocument.createElement("script");
        tag.textContent = script;
        frame.contentDocument.head.appendChild(tag);
        resolve(!!frame.contentWindow.__minipaintBridge);
    });
    document.body.appendChild(frame);
})"""
COMPACT_APPLY_JS = """(payload) => document.getElementById("mp-wangp-compact-frame").contentWindow.__minipaintBridge.layout(payload)"""
COMPACT_MEASURE_JS = """() => {
    const doc = document.getElementById("mp-wangp-compact-frame").contentDocument;
    const q = (s) => doc.querySelector(s);
    const shown = (el) => !!el && el.getClientRects().length > 0;
    const gallery = q("#wangp-gallery-tabs");
    const panel = gallery ? gallery.parentElement.closest("[role='tabpanel']") : null;
    const main = panel ? panel.parentElement : null;
    const title = main ? main.parentElement.querySelector("h1") : null;
    return {
        title: shown(title), strip: shown(main && main.querySelector(":scope > .tab-wrapper")),
        filter: shown(q("#wangp_model_output_filter")), family: shown(q("#family_list")),
        tools: shown(q("#wangp_model_tool_search")), header: shown(q(".header-markdown-group")),
        lset: Math.round(q("#lset").getBoundingClientRect().top),
        gallery: shown(gallery), galleryStrip: shown(gallery && gallery.querySelector(":scope > .tab-wrapper")),
        modeStrip: shown(q("#lset").closest(".column").querySelector(".tabs > .tab-wrapper")),
        generationTime: shown(q("#gentime")), bridge: !!q(".minipaint-bridge-column") && !q(".minipaint-bridge-column").closest("[data-minipaint-chrome]"),
        inline: [...doc.querySelectorAll("[data-minipaint-chrome]")].map((e) => e.style.getPropertyValue("display") + " " + e.style.getPropertyPriority("display")),
        marks: [...doc.querySelectorAll("[data-minipaint-chrome]")].map((e) => e.getAttribute("data-minipaint-chrome")),
        leftovers: [...doc.querySelectorAll("[style*='display: none']")].filter((e) => !e.classList.contains("tabitem") && !e.hasAttribute("data-minipaint-chrome")).length,
        attr: doc.documentElement.getAttribute("data-minipaint-layout")
    };
}"""
COMPACT_HOSTILE_CSS = ".tabs > .tab-wrapper { display: flex !important; } .header-markdown-group { display: block !important; }"


def check_focus_compacts_the_wangp_page(r: Results, page) -> None:
    """Under focus the WanGP page is its generator form alone (bridge 1.11.0).

    The bridge's own script runs on WanGP's page as Gradio 5.29 really draws
    it (tests/wangp_page_gradio_5_29.html) and is told compact, then whole:
    the title, the main tab strip, the model row and the model's description
    go, the form moves to the top with its own tabs and the galleries, the
    bridge's column stays, and everything comes back exactly as it was - the
    inline display each element had included. Measured in a browser because
    what is asked is whether a real layout lost those boxes; the numbers are
    the form's top edge before and after.

    Two decisions are reverted to prove the checks can see them: without the
    inline !important, a theme's own !important keeps the tab strip; without
    putting back on whole, the page stays compact after focus ends.
    """
    import sys as _sys

    folder = str(ROOT / "wan2gp_bridge" / "wan2gp-minipaint-bridge")
    _sys.path.insert(0, folder)
    try:
        import bridge_js
    finally:
        if folder in _sys.path:
            _sys.path.remove(folder)
    markup = (ROOT / "tests" / "wangp_page_gradio_5_29.html").read_text(encoding="utf-8")
    script = bridge_js.document_script("")

    def run(source):
        ready = page.evaluate(COMPACT_FRAME_JS, [markup, source, COMPACT_HOSTILE_CSS])
        before = page.evaluate(COMPACT_MEASURE_JS)
        told = page.evaluate(COMPACT_APPLY_JS, {"compact": True})
        compact = page.evaluate(COMPACT_MEASURE_JS)
        page.evaluate(COMPACT_APPLY_JS, {"compact": False})
        whole = page.evaluate(COMPACT_MEASURE_JS)
        return ready, before, told, compact, whole

    try:
        ready, before, told, compact, whole = run(script)
        r.check("WanGP's page as Gradio 5.29 draws it runs the bridge's own script", ready is True, str(ready))
        r.check("whole, it shows the title, the main tab strip, the model row and the model's description",
                all(before.get(k) for k in ("title", "strip", "filter", "family", "tools", "header")) and before.get("attr") is None,
                str(before))
        r.check("told compact, the title, the main tab strip, the model row and the description are gone",
                not any(compact.get(k) for k in ("title", "strip", "filter", "family", "tools", "header"))
                and compact.get("attr") == "compact" and told.get("hidden") == len(compact.get("marks") or []),
                str(compact) + " " + str(told))
        r.check("each named for what it is, and hidden inline with !important, over a theme's own !important",
                sorted(set(compact.get("marks") or [])) == ["model", "tabs", "title"]
                and all(one == "none important" for one in compact.get("inline") or []), str(compact.get("marks")) + str(compact.get("inline")))
        r.check("the form is at the top: the Lora preset row rises by the height of what went",
                before.get("lset", 0) - compact.get("lset", 0) > 150 and compact.get("lset", 999) < 40,
                f"{before.get('lset')} -> {compact.get('lset')}")
        r.check("and the form keeps its own tab strip, the generation time, the galleries and theirs, and the bridge's column",
                compact.get("modeStrip") and compact.get("generationTime") and compact.get("gallery") and compact.get("galleryStrip")
                and compact.get("bridge"), str(compact))
        r.check("told whole, every part is back where it was, with nothing left marked or hidden",
                {k: whole.get(k) for k in ("title", "strip", "filter", "family", "tools", "header", "lset")}
                == {k: before.get(k) for k in ("title", "strip", "filter", "family", "tools", "header", "lset")}
                and whole.get("marks") == [] and whole.get("attr") is None and whole.get("leftovers") == before.get("leftovers"),
                str(whole))

        # A form not drawn yet: nothing is hidden and nothing is claimed,
        # and the layout lands when the form appears.
        page.evaluate(COMPACT_FRAME_JS, [markup, script, ""])
        page.evaluate("""() => document.getElementById("mp-wangp-compact-frame").contentDocument
                          .getElementById("wangp-gallery-tabs").id = "not-yet" """)
        early = page.evaluate(COMPACT_APPLY_JS, {"compact": True})
        early_seen = page.evaluate(COMPACT_MEASURE_JS.replace('q("#wangp-gallery-tabs")', 'q("#not-yet")'))
        page.evaluate("""() => document.getElementById("mp-wangp-compact-frame").contentDocument
                          .getElementById("not-yet").id = "wangp-gallery-tabs" """)
        page.wait_for_timeout(700)
        late = page.evaluate(COMPACT_MEASURE_JS)
        r.check("with no form drawn yet nothing is hidden and the page is not called compact",
                early.get("hidden") == 0 and early_seen.get("marks") == [] and early_seen.get("attr") is None
                and early_seen.get("title") and early_seen.get("strip"), str(early) + str(early_seen))
        r.check("and when the form appears the layout lands after all",
                late.get("attr") == "compact" and not late.get("strip") and not late.get("header"), str(late))

        # Every decision here has a mutation that must break its check.
        for name, old, new, broken in (
            ("without the inline !important a theme's own keeps the tab strip",
             'element.style.setProperty("display", "none", "important");', 'element.style.setProperty("display", "none");',
             lambda c, w: c.get("strip") is True),
            ("without putting it all back on whole the page stays compact after focus",
             "    if (!layoutWanted) {\n      showChrome();", "    if (!layoutWanted) {\n",
             lambda c, w: w.get("strip") is False),
        ):
            anchored = old in script
            _ready, _before, _told, mutated, mutated_whole = run(script.replace(old, new)) if anchored else (None, {}, {}, {}, {})
            r.check(f"mutation: {name}", anchored and broken(mutated, mutated_whole),
                    "anchor gone from the script" if not anchored else str(mutated)[:200] + str(mutated_whole)[:200])
    finally:
        page.evaluate("""() => { const f = document.getElementById("mp-wangp-compact-frame"); if (f) { f.remove(); } }""")


def check_the_direct_route_when_the_queue_is_dead(r: Results, page) -> None:
    """Gradio's queue cut: the takeover never needed it, so nothing changes."""
    open_txt2img(page)
    page.route("**/queue/**", lambda route: route.abort())
    try:
        state_before = popup_state(page) or {}
        r.check("dead queue: the button is pressed", press_send(page))
        opened = wait_popup(page, True, 25.0)
        state = popup_state(page) or {}
        r.check("dead queue: the popup still opens, on a NEW picture staged over the staging route, never the previous press's",
                opened and state.get("token") and state.get("token") != state_before.get("token"), str(state)[:160])
        r.check("dead queue: and knows which tab it came from, so it sits under that button",
                state.get("tab") == "txt2img", str(state.get("tab")))
        page.keyboard.press("Escape")
        wait_popup(page, False, 10.0)
    finally:
        page.unroute("**/queue/**")
        time.sleep(2.0)


def run() -> Results:
    r = Results("browser intercept")
    from playwright.sync_api import sync_playwright  # noqa: F401  (ImportError -> run.py skips)

    chromium = loading.chromium_path()
    scratch = pathlib.Path(tempfile.mkdtemp(prefix="minipaint-intercept-browser-"))
    library = scratch / "library"
    from minipaint_neo.clipboard import config as clip_config
    from minipaint_neo.clipboard import outbox, executor

    demo, _refs = build_page(library)
    from minipaint_neo import assets
    from minipaint_neo import interop as interop_routes
    from minipaint_neo.clipboard import routes as clip_routes

    demo.queue().launch(server_name="127.0.0.1", server_port=PORT, prevent_thread_lock=True,
                        quiet=True, allowed_paths=[str(ROOT)])
    assets.install(demo.app)
    clip_routes.install(demo.app)
    interop_routes.install(demo.app)
    # A page-run queue with WanGP "running": the job is stored and stays where
    # the checks can read it, rather than being handed to a coordinator that
    # would try to start a WanGP this machine does not have.
    outbox.use_running(lambda: True)
    outbox.use_executor(outbox.EXECUTOR_BROWSER)
    executor.use_thread(False)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chromium) if chromium else p.chromium.launch()
            page = browser.new_page(viewport={"width": 1400, "height": 950})
            try:
                page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
                page.wait_for_selector("#tabs .tab-nav button", timeout=30000)
                time.sleep(2.5)
                check_the_framework_path_takes_the_host_helper_shape(r, page, library)
                check_the_button_opens_the_popup(r, page, library)
                check_generate_queues_and_closes(r, page, library)
                check_escape_queues_nothing(r, page)
                check_the_menu_offers_the_destinations(r, page)
                check_the_wangp_panel_is_parked_not_hidden(r, page)
                check_focus_mode_makes_the_frame_the_window(r, page)
                check_the_page_palette_is_sampled(r, page)
                check_the_page_typeface_reaches_wangp(r, page)
                check_focus_compacts_the_wangp_page(r, page)
                check_the_direct_route_when_the_queue_is_dead(r, page)
            finally:
                browser.close()
    finally:
        outbox.reset_for_tests()
        executor.reset_for_tests()
        current = clip_config.load()
        current.storage_root = ""
        current.intercept_target = clip_config.INTERCEPT_MINIPAINT
        clip_config.save(current)
        from minipaint_neo.clipboard import history, intercept

        draft = history.load_draft()
        draft["prompt_override"] = ""
        history.save_draft(draft)
        if "history" in _STASH:
            kept = _STASH.pop("history")
            if kept is None:
                clip_config.path_of(intercept.HISTORY_NAME).unlink(missing_ok=True)
            else:
                clip_config.write_document(intercept.HISTORY_NAME, kept)
    return r


if __name__ == "__main__":
    sys.exit(0 if run().report() else 1)
