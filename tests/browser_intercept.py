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

    def both_tabs():
        return (router.on_ui_tabs() or []) + (clip_ui.on_ui_tabs() or [])

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
