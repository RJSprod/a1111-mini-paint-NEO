"""The Clipboard tab in a real browser: sending out, and the thumbnail grid.

    python tests/browser_clipboard.py

WHY THESE ARE BROWSER CHECKS AND NOT UNIT CHECKS.

``test_clipboard_ui.py`` already proves the Gradio graph: 181 checks over the
components, the events and what each callback returns. All of it passed while
the user could not send a picture out of the tab, and while the thumbnail
slider moved nothing - because both failures live in the browser, in the half
the graph cannot see:

*   a send is a hidden box written by script and a Gradio event crossing the
    queue. The write can succeed and the event never arrive, and the page
    used to say nothing at all when that happened.

*   the thumbnail size is a CSS custom property. It was being written to the
    Gradio block around the grid, and Gradio puts a component's elem_classes
    on both that block and the inner div it renders into - so the
    stylesheet's default matched twice, the inner match shadowed the value,
    and the grid never heard it. Nothing but a real layout can catch that.

The Canvas is stubbed here for the same reason as in ``browser_loading.py``:
none of this depends on the editor, so none of it should need a Forge Neo
checkout to run.
"""

from __future__ import annotations

import os
import pathlib
import sys
import time

for _key in ("no_proxy", "NO_PROXY"):
    os.environ[_key] = "127.0.0.1,localhost,::1," + os.environ.get(_key, "")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from harness import Results, setup_path  # noqa: E402

setup_path()

ROOT = pathlib.Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("MINIPAINT_CLIP_TEST_PORT", "8817"))

import browser_loading as loading  # noqa: E402  (its chromium finder and page helpers)


def build_page(library: pathlib.Path):
    """The Forge-shaped page with both tabs on it, and a seeded library."""
    import forge_like
    from modules import script_callbacks, shared
    from minipaint_neo import router, settings
    from minipaint_neo.canvas import host
    from minipaint_neo.clipboard import config as clip_config

    from PIL import Image

    library.mkdir(parents=True, exist_ok=True)
    for stale in library.glob("*"):
        stale.unlink()
    # Deliberately mixed shapes: a grid that only looks tidy with square
    # pictures is not tidy.
    for name, size in (("tall.png", (300, 900)), ("wide.png", (900, 300)), ("square.png", (500, 500)),
                       ("portrait.png", (400, 600)), ("landscape.png", (800, 450)), ("tiny.png", (64, 64))):
        Image.new("RGB", size, (30, 90, 200)).save(library / name)

    current = clip_config.load()
    current.storage_root = str(library)
    current.intercept = False
    clip_config.save(current)

    from minipaint_neo.clipboard import ui as clip_ui

    def both_tabs():
        return (router.on_ui_tabs() or []) + (clip_ui.on_ui_tabs() or [])

    shared.opts.data[settings.USE_OLD_UI] = False
    script_callbacks.callbacks["after_component"][:] = [host.on_after_component]
    host.reset_capture()
    demo, refs = forge_like.build_host(both_tabs, extra_head=head_html())
    return demo, refs


def head_html() -> str:
    """Only what Forge really puts on every page, plus a stand-in editor.

    No Forge checkout: the Canvas is not what is being tested, and requiring
    one would keep this suite out of CI - which is where the regressions it
    catches were found.
    """
    return (
        "<script>" + loading.FORGE_CANVAS_STUB + "</script>"
        "<script>" + (ROOT / "javascript" / "main.js").read_text(encoding="utf-8") + "</script>"
        "<style>" + (ROOT / "style.css").read_text(encoding="utf-8") + "</style>"
    )


# --------------------------------------------------------------------------
# Page helpers
# --------------------------------------------------------------------------

MENU_ITEMS_JS = ("() => Array.from(document.querySelectorAll('.minipaint-clip-menu .minipaint-clip-menu-item'))"
                 ".map(b => b.textContent.trim() + (b.disabled ? ' [disabled]' : ''))")

TILES_JS = """() => Array.from(document.querySelectorAll('.minipaint-clip-item')).map(el => {
    const r = el.getBoundingClientRect();
    const t = el.querySelector('.minipaint-clip-thumb');
    const tr = t ? t.getBoundingClientRect() : null;
    return {name: el.dataset.name || '', w: Math.round(r.width), h: Math.round(r.height),
            thumbW: tr ? Math.round(tr.width) : 0, thumbH: tr ? Math.round(tr.height) : 0};
})"""


def box(page, elem_id):
    return page.evaluate(
        "id => { const h = document.getElementById(id); const t = h && h.querySelector('textarea,input');"
        " return t ? String(t.value || '') : null; }", elem_id)


def menu_click(page, text):
    return page.evaluate("""t => {
        const items = Array.from(document.querySelectorAll('.minipaint-clip-menu .minipaint-clip-menu-item'))
            .filter(b => b.textContent.trim().indexOf(t) >= 0);
        if (!items.length) { return 'NOT FOUND: ' + t; }
        if (items[0].disabled) { return 'DISABLED'; }
        items[0].click();
        return 'clicked';
    }""", text)


def toast(page):
    return page.evaluate(
        "() => { const t = document.querySelector('.minipaint-clip-toast');"
        " return (t && !t.hidden) ? t.textContent.trim() : ''; }")


def open_clipboard(page):
    page.locator("#tabs > .tab-nav > button", has_text="Clipboard").first.click()
    time.sleep(2.5)


def select_first(page):
    """Click the first picture and wait until the server has confirmed it.

    The confirmation matters: the grid re-renders after a refresh and clears
    a selection it cannot find, so a fixed sleep here races that and leaves
    the Send menu disabled for reasons that have nothing to do with sending.
    """
    for _ in range(3):
        page.evaluate("() => { const c = document.querySelector('.minipaint-clip-item'); if (c) { c.click(); } }")
        for _ in range(20):
            time.sleep(0.25)
            if box(page, "minipaint_clipboard_selected"):
                return True
    return False


def send_selected(page, label):
    page.evaluate("() => window.minipaintClipboard.toggleMenu()")
    time.sleep(0.4)
    menu_click(page, "Send selected to")
    time.sleep(0.4)
    return menu_click(page, label)


def set_slider(page, value):
    page.evaluate("""v => {
        const host = document.getElementById('minipaint_clipboard_thumb');
        const input = host.querySelector('input[type=range]');
        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        setter.call(input, String(v));
        input.dispatchEvent(new Event('input', {bubbles: true}));
        input.dispatchEvent(new Event('change', {bubbles: true}));
    }""", value)
    time.sleep(1.0)


# --------------------------------------------------------------------------
# The thumbnail grid
# --------------------------------------------------------------------------

def check_grid(r: Results, page) -> None:
    tiles = page.evaluate(TILES_JS)
    r.check("the library's pictures are all on the grid", len(tiles) == 6, str(len(tiles)))
    heights = sorted({t["h"] for t in tiles})
    widths = sorted({t["w"] for t in tiles})
    thumbs = sorted({(t["thumbW"], t["thumbH"]) for t in tiles})
    r.check("every tile is the same size, whatever shape its picture is",
            len(heights) == 1 and len(widths) == 1, f"heights {heights} widths {widths}")
    r.check("and every thumbnail is the same square",
            len(thumbs) == 1 and abs(thumbs[0][0] - thumbs[0][1]) <= 1, str(thumbs))

    # The slider. It writes a CSS variable; the grid has to be the element
    # that hears it, which is the whole of the bug this covers.
    before = page.evaluate(TILES_JS)[0]["w"]
    for size, direction in ((260, "larger"), (96, "smaller")):
        set_slider(page, size)
        after = page.evaluate(TILES_JS)
        r.check(f"the size slider makes the tiles {direction}",
                after and (after[0]["w"] > before if size > 144 else after[0]["w"] < before),
                f"{before}px -> {after[0]['w'] if after else '?'}px at slider {size}")
        r.check(f"and they are still uniform at {size}",
                len({t['w'] for t in after}) == 1 and len({t['h'] for t in after}) == 1,
                str(sorted({(t['w'], t['h']) for t in after})))
        shown = page.evaluate("() => { const g = document.querySelector('.minipaint-clip-grid');"
                              " return g ? getComputedStyle(g).getPropertyValue('--minipaint-clip-thumb').trim() : ''; }")
        held = page.evaluate("() => { const i = document.querySelector('#minipaint_clipboard_thumb input[type=range]');"
                             " return i ? i.value : ''; }")
        r.check(f"the grid itself carries the size the slider holds (asked {size})",
                shown == held + "px", f"grid {shown!r} vs slider {held!r}")

    # A refresh replaces the grid element; the size must not snap back.
    set_slider(page, 220)
    page.evaluate("() => window.minipaintClipboard.pressHidden('minipaint_clipboard_refresh')")
    time.sleep(2.5)
    kept = page.evaluate(TILES_JS)
    r.check("and a refresh does not snap the tiles back to the default",
            kept and kept[0]["w"] > 144, f"{kept[0]['w'] if kept else '?'}px after refresh")


# --------------------------------------------------------------------------
# Sending out
# --------------------------------------------------------------------------

def check_send(r: Results, page, targets) -> None:
    """Each destination takes the picture, and the server says so.

    The img2img and Inpaint destinations are the host's own canvases, and
    their pictures are written from the browser into a hidden textbox whose
    id belongs to that canvas - so "did it arrive" is asked of the page,
    not of a component id captured on this side, which is a different object
    from the one the tab was wired to.
    """
    def host_pictures():
        return page.evaluate("""() => Array.from(document.querySelectorAll('textarea,input'))
            .filter(t => String(t.value || '').indexOf('data:image') === 0)
            .map(t => { const h = t.closest('[id]'); return h ? h.id : '?'; })
            .filter(id => id.indexOf('minipaint_clipboard') !== 0)""")

    def gallery_count(key):
        elem_id = getattr(targets.get(key), "elem_id", "") or ""
        if not elem_id:
            return None
        return page.evaluate("id => { const h = document.getElementById(id);"
                             " return h ? h.querySelectorAll('img').length : 'NO ELEMENT'; }", elem_id)

    def status():
        return page.evaluate("() => { const s = document.getElementById('minipaint_clipboard_status');"
                             " return s ? s.textContent.trim().slice(0, 120) : ''; }")

    for key, label in (("img2img", "img2img"),
                       ("stitch_txt2img", "ImageStitch (txt2img)"),
                       ("stitch_img2img", "ImageStitch (img2img)")):
        if key not in targets:
            continue
        r.check(f"send to {key}: a picture is selected first", select_first(page))
        stamp_before = box(page, "minipaint_clipboard_send_ack")
        before = len(host_pictures())
        r.check(f"send to {key}: the menu offers it", send_selected(page, label) == "clicked")
        time.sleep(3.5)
        if key == "img2img":
            arrived = len(host_pictures()) > before or bool(host_pictures())
            detail = f"host textboxes holding a picture: {host_pictures()}"
        else:
            count = gallery_count(key)
            arrived = isinstance(count, int) and count >= 1
            detail = f"gallery images: {count!r}"
        r.check(f"send to {key}: the picture arrives", arrived, f"{detail} status={status()!r}")
        r.check(f"send to {key}: the server acknowledges it",
                box(page, "minipaint_clipboard_send_ack") not in (None, "", stamp_before),
                repr(box(page, "minipaint_clipboard_send_ack")))
        r.check(f"send to {key}: the status confirms it", status().startswith("Sent "), status())
        r.check(f"send to {key}: and nothing is reported as lost", toast(page) == "", toast(page))
        open_clipboard(page)


def check_send_survives_a_dead_queue(r: Results, page, targets) -> None:
    """A send finishes even when the Gradio queue stops delivering.

    This is the failure the user hit. The menu worked, the hidden box was
    written, the note about it reached the server over plain HTTP - and the
    Gradio event carrying the send never arrived, so nothing happened and
    nothing said why. Everything else the tab does for a running job rides
    ordinary HTTP and kept working; only the actions were tied to the queue.

    So the queue is cut here while HTTP is left alone, which is the shape of
    the real failure, and the send has to finish anyway.
    """
    if not select_first(page):
        r.check("dead queue: a picture is selected first", False, "no selection")
        return

    box_id = getattr(targets.get("img2img"), "elem_id", "") or ""
    def host_value():
        return page.evaluate("id => { const h = document.querySelector('.logical_image_background[id=\"' + id + '\"]');"
                             " const t = h && h.querySelector('textarea,input'); return t ? String(t.value || '').length : -1; }", box_id)

    # Cut only Gradio's queue. Plain HTTP - the event stream, the routes -
    # is untouched, exactly as in the report.
    # Empty the destination first. An earlier send in this run put the same
    # picture there, and the same picture is the same number of bytes - so a
    # test that watches the length would pass without anything happening.
    page.evaluate("""id => {
        const h = document.querySelector('.logical_image_background[id="' + id + '"]');
        const t = h && h.querySelector('textarea,input');
        if (t) { t.value = ''; t.dispatchEvent(new Event('input', {bubbles: true})); }
    }""", box_id)
    time.sleep(1)
    r.check("dead queue: the destination starts empty", host_value() == 0, str(host_value()))

    page.route("**/queue/**", lambda route: route.abort())
    page.route("**/gradio_api/**", lambda route: route.abort())
    try:
        r.check("dead queue: the send is attempted", send_selected(page, "img2img") == "clicked")
        landed, said = False, ""
        for _ in range(26):
            time.sleep(1)
            said = toast(page) or said
            if host_value() > 0:
                landed = True
                break
        r.check("dead queue: the picture still reaches img2img, over plain HTTP",
                landed, f"host textbox length {host_value()}")
        r.check("dead queue: and the page says how it got there",
                "lost its connection" in said or said.startswith("Sent "), repr(said))
    finally:
        page.unroute("**/queue/**")
        page.unroute("**/gradio_api/**")
        time.sleep(3)


def check_backend_destination_is_honest(r: Results, page) -> None:
    """A destination the server writes cannot be finished without it."""
    if not select_first(page):
        return
    page.route("**/queue/**", lambda route: route.abort())
    page.route("**/gradio_api/**", lambda route: route.abort())
    try:
        send_selected(page, "ImageStitch (txt2img)")
        said = ""
        for _ in range(26):
            time.sleep(1)
            said = toast(page)
            if said:
                break
        r.check("dead queue: a server-written destination says so instead of looking sent",
                "needs the connection back" in said or "never reached" in said, repr(said))
    finally:
        page.unroute("**/queue/**")
        page.unroute("**/gradio_api/**")
        time.sleep(3)


def check_recovers_after_the_interruption(r: Results, page, targets) -> None:
    """And once the connection is back, sending works again."""
    open_clipboard(page)
    r.check("after the interruption: a picture can still be selected", select_first(page))
    before = box(page, "minipaint_clipboard_send_ack")
    r.check("after the interruption: the menu still sends", send_selected(page, "img2img") == "clicked")
    settled = False
    for _ in range(20):
        time.sleep(1)
        if box(page, "minipaint_clipboard_send_ack") not in (None, "", before):
            settled = True
            break
    r.check("after the interruption: the server acknowledges the send again", settled,
            repr(box(page, "minipaint_clipboard_send_ack")))


def check_tab_switching_survives_reordering(r: Results, page) -> None:
    """Tabs get reordered and hidden; a send must still land on the right one.

    The switch used to fall back to counting - the panel's index among the
    panels, then the button at that index - which only works while every
    panel has a button and both lists are in the same order. Reorder the
    tabs, or hide one, and it lands a tab over: a picture sent to Extras
    opened PNG Info.
    """
    def visible_panel():
        return page.evaluate("() => (Array.from(document.querySelectorAll('#tabs > .tabitem'))"
                             ".filter(i => getComputedStyle(i).display !== 'none')[0] || {}).id || ''")

    ours = {"canvas": "tab_minipaint", "clipboard": "tab_minipaint_clipboard"}
    for name, panel in ours.items():
        page.evaluate("n => window.minipaintCanvas.switchTo(n)", name)
        time.sleep(1.2)
        r.check(f"switchTo({name}) shows its own tab, in the page's natural order",
                visible_panel() == panel, f"{visible_panel()} (wanted {panel})")

    # Reverse the tab buttons. Nothing about which button controls which panel
    # changes - only their order - so every switch must still be exact.
    page.evaluate("""() => {
        const nav = document.querySelector('#tabs > .tab-nav');
        Array.from(nav.children).reverse().forEach(b => nav.appendChild(b));
    }""")
    time.sleep(0.5)
    for name, panel in ours.items():
        page.evaluate("n => window.minipaintCanvas.switchTo(n)", name)
        time.sleep(1.2)
        r.check(f"switchTo({name}) still shows its own tab with the tabs reversed",
                visible_panel() == panel, f"{visible_panel()} (wanted {panel})")

    # A tab that is not on the page is not a tab to guess at. This page has
    # no WanGP tab, and the old positional fallback would have counted its
    # way onto whichever tab happened to sit at that index.
    r.check("the page really has no WanGP tab to find",
            not page.evaluate("() => !!document.querySelector('#tab_minipaint_wangp')"))
    page.evaluate("() => window.minipaintCanvas.switchTo('canvas')")
    time.sleep(1.2)
    was = visible_panel()
    page.evaluate("() => window.minipaintCanvas.switchTo('wangp')")
    time.sleep(1.2)
    r.check("a switch to a tab that is not on the page moves nothing, rather than guessing",
            visible_panel() == was, f"{was} -> {visible_panel()}")
    page.reload(wait_until="load")
    page.wait_for_selector("#tabs .tab-nav button", timeout=30000)
    time.sleep(2)

    # A host that does not label its buttons with aria-controls leaves only
    # counting, and counting is sound only while every panel has a button.
    # Take one button away and the lists slide past each other - which is
    # exactly when the old code opened the tab next door.
    page.evaluate("""() => {
        document.querySelectorAll('#tabs > .tab-nav button').forEach(b => b.removeAttribute('aria-controls'));
    }""")
    page.evaluate("() => window.minipaintCanvas.switchTo('canvas')")
    time.sleep(1.2)
    r.check("without aria-controls, counting still finds the tab while the lists line up",
            visible_panel() == "tab_minipaint", visible_panel())

    page.evaluate("""() => {
        const nav = document.querySelector('#tabs > .tab-nav');
        if (nav.firstElementChild) { nav.firstElementChild.remove(); }
    }""")
    time.sleep(0.4)
    settled = visible_panel()
    page.evaluate("() => window.minipaintCanvas.switchTo('clipboard')")
    time.sleep(1.2)
    r.check("but with a button missing it refuses to count, rather than opening the tab next door",
            visible_panel() in (settled, "tab_minipaint_clipboard"),
            f"{settled} -> {visible_panel()}")
    r.check("and it certainly does not land on an unrelated tab",
            visible_panel() not in ("tab_txt2img", "tab_extras", "tab_pnginfo", "tab_settings", "tab_extensions"),
            visible_panel())
    page.reload(wait_until="load")
    page.wait_for_selector("#tabs .tab-nav button", timeout=30000)
    time.sleep(2)


def check_hidden_tab_is_not_offered(r: Results, targets) -> None:
    """A destination whose tab the user hid is not offered at all.

    Forge does not build a hidden tab, so its components are never captured
    and the menu cannot list it - which is the right answer, and worth a
    check because the alternative (offering it and sending into nothing) is
    exactly the kind of quiet half-success this tab has had enough of.
    """
    import forge_like
    from modules import script_callbacks, shared
    from minipaint_neo import router, settings
    from minipaint_neo.canvas import host

    shared.opts.data[settings.USE_OLD_UI] = False
    script_callbacks.callbacks["after_component"][:] = [host.on_after_component]
    host.reset_capture()
    forge_like.build_host(lambda: router.on_ui_tabs() or [], hidden_tabs=("Extras",))
    hidden = host.destinations()
    r.check("with the Extras tab hidden, Extras is not a destination",
            "extras" not in hidden, str(sorted(hidden)))
    r.check("and the tabs that are still there still are",
            "img2img" in hidden and "inpaint" in hidden, str(sorted(hidden)))


def run() -> Results:
    r = Results("browser clipboard")
    from playwright.sync_api import sync_playwright  # noqa: F401  (ImportError -> run.py skips)

    chromium = loading.chromium_path()
    library = pathlib.Path(os.environ.get("MINIPAINT_CLIP_LIBRARY")
                           or (ROOT / "tests" / ".clipboard-library"))
    demo, refs = build_page(library)
    from minipaint_neo import assets
    from minipaint_neo.canvas import host

    demo.queue().launch(server_name="127.0.0.1", server_port=PORT, prevent_thread_lock=True,
                        quiet=True, allowed_paths=[str(ROOT)])
    assets.install(demo.app)
    from minipaint_neo.clipboard import routes as clip_routes
    clip_routes.install(demo.app)
    targets = host.destinations()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chromium) if chromium else p.chromium.launch()
            page = browser.new_page(viewport={"width": 1400, "height": 950})
            try:
                page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
                page.wait_for_selector("#tabs .tab-nav button", timeout=30000)
                time.sleep(2)
                open_clipboard(page)
                r.check("the clipboard adapter attached",
                        page.evaluate("() => !!(window.minipaintClipboard && window.minipaintClipboard.debug)"))
                check_grid(r, page)
                r.check("a picture can be selected", select_first(page),
                        repr(box(page, "minipaint_clipboard_selected")))
                check_send(r, page, targets)
                check_send_survives_a_dead_queue(r, page, targets)
                check_backend_destination_is_honest(r, page)
                check_recovers_after_the_interruption(r, page, targets)
                check_tab_switching_survives_reordering(r, page)
            finally:
                browser.close()
    finally:
        for stale in library.glob("*"):
            try:
                stale.unlink()
            except OSError:
                pass
        # The library root is this suite's, not the user's: put the stored
        # settings back so a later suite does not inherit them.
        from minipaint_neo.clipboard import config as clip_config
        current = clip_config.load()
        current.storage_root = ""
        current.intercept = False
        clip_config.save(current)
    return r


if __name__ == "__main__":
    sys.exit(0 if run().report() else 1)
