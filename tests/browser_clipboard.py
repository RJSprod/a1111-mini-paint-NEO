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

# The geometry of every tile, and of the picture actually DRAWN in it.
#
# offX - the <img> element's box against the tile's - is what this used to
# measure, and it cannot see the bug it was written for. The element is
# width:100% of its box, so its middle is the tile's middle by construction:
# offX reads 0 whether the picture inside it is centred, against one edge,
# or not drawn at all. The grid was reported off-centre a third time with
# every one of these checks passing.
#
# What the eye judges is where object-fit puts the pixels inside that
# element, so that is computed here from the picture's own dimensions,
# exactly as the browser resolves `contain` and `object-position`. paintOffX
# is the number the tile is actually accused of.
TILES_JS = """() => Array.from(document.querySelectorAll('.minipaint-clip-item')).map(el => {
    const r = el.getBoundingClientRect();
    const t = el.querySelector('.minipaint-clip-thumb');
    const tr = t ? t.getBoundingClientRect() : null;
    const im = el.querySelector('img');
    const ir = im ? im.getBoundingClientRect() : null;
    let paint = null;
    if (im && ir && im.naturalWidth && im.naturalHeight && ir.width && ir.height) {
        const cs = getComputedStyle(im);
        const fit = cs.objectFit;
        // `contain` fits the whole picture in; `cover`/`fill`/`none` are not
        // used here, and are reported as they land rather than assumed.
        const s = fit === 'contain' || fit === 'scale-down'
            ? Math.min(ir.width / im.naturalWidth, ir.height / im.naturalHeight,
                       fit === 'scale-down' ? 1 : Infinity)
            : (fit === 'cover' ? Math.max(ir.width / im.naturalWidth, ir.height / im.naturalHeight) : 0);
        const pw = s ? im.naturalWidth * s : ir.width;
        const ph = s ? im.naturalHeight * s : ir.height;
        const pos = String(cs.objectPosition || '50% 50%').split(/\\s+/);
        const fx = pos[0] && pos[0].endsWith('%') ? parseFloat(pos[0]) / 100 : 0.5;
        const fy = pos[1] && pos[1].endsWith('%') ? parseFloat(pos[1]) / 100 : 0.5;
        paint = {left: ir.left + (ir.width - pw) * (isNaN(fx) ? 0.5 : fx),
                 top: ir.top + (ir.height - ph) * (isNaN(fy) ? 0.5 : fy), w: pw, h: ph};
    }
    // The tile's own inside: what the picture is meant to be centred in.
    const cs = getComputedStyle(el);
    const inL = r.left + parseFloat(cs.borderLeftWidth) + parseFloat(cs.paddingLeft);
    const inR = r.right - parseFloat(cs.borderRightWidth) - parseFloat(cs.paddingRight);
    return {name: el.dataset.name || '', w: Math.round(r.width), h: Math.round(r.height),
            thumbW: tr ? Math.round(tr.width) : 0, thumbH: tr ? Math.round(tr.height) : 0,
            // How far the picture reaches past the tile that is meant to
            // hold it, on the two sides a picture can run over.
            spillY: ir ? Math.round(ir.bottom - r.bottom) : 0,
            spillX: ir ? Math.round(ir.right - r.right) : 0,
            // How far the picture's middle sits from the tile's middle. A
            // picture that fits but hugs one edge is not in its cell.
            offX: ir ? Math.round(((ir.left + ir.right) / 2) - ((r.left + r.right) / 2)) : 0,
            // The same question asked of the drawn pixels.
            paintW: paint ? Math.round(paint.w) : 0,
            paintH: paint ? Math.round(paint.h) : 0,
            paintOffX: paint ? Math.round((paint.left + paint.w / 2) - ((inL + inR) / 2)) : 0,
            drawn: !!paint};
})"""


def adrift(tiles):
    """Tiles whose DRAWN picture is not in the middle of the tile."""
    return [(t["name"], t["paintOffX"]) for t in tiles if not t["drawn"] or abs(t["paintOffX"]) > 1]

# The tile's own geometry, with the box inside it prevented from doing any
# of the work: the shape the grid was in when it was reported a second time.
# Nothing here is hypothetical - it is what the page looked like on the
# machine that reported it, and what the tile has to survive on its own.
NO_THUMB_BOX_CSS = """
#minipaint_clipboard_root .minipaint-clip-thumb {
    display: inline !important;
    width: auto !important;
    flex: 0 0 auto !important;
    aspect-ratio: auto !important;
    overflow: visible !important;
}
#minipaint_clipboard_root .minipaint-clip-thumb img {
    width: auto !important;
    height: auto !important;
}
"""


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


def failure_toast(page):
    """A notice the page is showing as a failure, rather than any notice.

    Every send says so now, because the page is what places the picture and
    the status line is the server's. So "said nothing" stopped being the
    test for "nothing went wrong"; "said nothing bad" is.
    """
    return page.evaluate(
        "() => { const t = document.querySelector('.minipaint-clip-toast');"
        " return (t && !t.hidden && t.classList.contains('minipaint-clip-toast-failure'))"
        " ? t.textContent.trim() : ''; }")


def record_toasts(page, reset=True):
    """Watch for every notice the page shows, rather than sampling for one.

    A toast takes itself down after a few seconds, so a poll that happens to
    look a moment early and a moment late sees nothing at all - and the
    interesting ones here appear twelve seconds after a click, which is
    exactly when a poll is doing something else.
    """
    page.evaluate("""reset => {
        if (reset || !window.__toasts) {
            window.__toasts = []; window.__toastSeen = new Set(); window.__toastT0 = performance.now();
        }
        const seen = window.__toastSeen;
        const look = () => {
            const t = document.querySelector('.minipaint-clip-toast');
            if (!t || t.hidden) { return; }
            const text = (t.textContent || '').trim();
            // When, as well as what: whether a send made the user wait is
            // the thing being checked, and a list of words cannot say.
            if (text && !seen.has(text)) {
                seen.add(text);
                window.__toasts.push({text: text, at: performance.now() - window.__toastT0});
            }
        };
        if (window.__toastTimer) { clearInterval(window.__toastTimer); }
        window.__toastTimer = setInterval(look, 120);
        look();
    }""", reset)


def recorded_toasts(page):
    """What the page has said since the recording started."""
    return [entry["text"] for entry in page.evaluate("() => window.__toasts || []")]


def said_when(page, prefix):
    """How long after the recording started the page first said this, in seconds."""
    for entry in page.evaluate("() => window.__toasts || []"):
        if str(entry.get("text", "")).startswith(prefix):
            return float(entry.get("at", 0)) / 1000.0
    return None


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
    r.check("and every thumbnail box is the same size", len(thumbs) == 1, str(thumbs))
    spilling = [(t["name"], t["spillX"], t["spillY"]) for t in tiles if t["spillY"] > 1 or t["spillX"] > 1]
    r.check("and no picture is drawn outside the tile holding it", not spilling, str(spilling))
    off = adrift(tiles)
    r.check("and every picture is DRAWN in the middle of its tile", not off, str(off))
    check_a_hostile_page_cannot_move_a_picture(r, page)

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
        # The tile has to be the SHAPE of the picture it holds, not just a
        # uniform size. The columns used to be stretched to fill the row
        # while the rows came from the slider, so every box was wider than it
        # was tall - at 260px, 369 wide and 264 tall - and a square picture
        # sat in it with a hundred pixels of dead space around it. Reported
        # as "the tile's border extends well beyond the thumbnail", which is
        # what it looks like from outside, and not a centring fault at all.
        box = after[0]
        r.check(f"and the box a picture is drawn in is square at {size}",
                abs(box["thumbW"] - box["thumbH"]) <= 2,
                f"{box['thumbW']}x{box['thumbH']}")
        r.check(f"so a picture as wide as it is tall fills it at {size}",
                any(t["paintW"] >= box["thumbW"] - 2 and t["paintH"] >= box["thumbH"] - 2 for t in after),
                str(sorted((t["paintW"], t["paintH"]) for t in after)))

    # A refresh replaces the grid element; the size must not snap back.
    set_slider(page, 220)
    page.evaluate("() => window.minipaintClipboard.pressHidden('minipaint_clipboard_refresh')")
    time.sleep(2.5)
    kept = page.evaluate(TILES_JS)
    r.check("and a refresh does not snap the tiles back to the default",
            kept and kept[0]["w"] > 144, f"{kept[0]['w'] if kept else '?'}px after refresh")

    check_tiles_hold_without_the_thumb_box(r, page)


def check_tiles_hold_without_the_thumb_box(r: Results, page) -> None:
    """The tile is uniform even when the box inside it is not helping.

    The grid was reported a second time with the pictures at their own
    natural sizes: tiles from 54px to 352px tall in one row, names running
    out past the tile, tall pictures four times the height of the tile
    meant to hold them. The rules were on the page and the markup was
    right; the box inside the tile simply was not shaping anything, and a
    tile whose height is whatever its content comes to has nothing left to
    say when that happens.

    So the grid is checked with that box explicitly prevented from doing any
    of the work. The row's height comes from the same variable as the
    columns and the tile clips what it holds, so neither the tile nor the
    picture can grow - and the picture's own ceiling is stated in pixels
    rather than as a percentage of a box that may have no height to give.
    """
    set_slider(page, 144)
    time.sleep(0.6)
    style = page.add_style_tag(content=NO_THUMB_BOX_CSS)
    try:
        page.wait_for_function(
            "() => { const i = document.querySelectorAll('.minipaint-clip-item img');"
            " return i.length && Array.from(i).every(x => x.complete); }", timeout=20000)
        time.sleep(0.4)
        tiles = page.evaluate(TILES_JS)
        heights = sorted({t["h"] for t in tiles})
        widths = sorted({t["w"] for t in tiles})
        r.check("with the thumb box doing nothing, the tiles are still all one size",
                len(tiles) > 1 and len(heights) == 1 and len(widths) == 1,
                f"heights {heights} widths {widths}")
        spilling = [(t["name"], t["spillX"], t["spillY"]) for t in tiles if t["spillY"] > 1 or t["spillX"] > 1]
        r.check("and no picture is drawn outside its tile even then", not spilling, str(spilling))
        off = adrift(tiles)
        r.check("and every picture is still drawn in the middle of its tile", not off, str(off))
    finally:
        style.evaluate("el => el.remove()")
        time.sleep(0.4)


# The page this grid actually lives on. Forge's own stylesheet, its theme and
# every other installed extension are loaded alongside this one, and any of
# them may say !important about an `img`. None of these rules is invented:
# each is a way the reported symptom - a thumbnail hard against one side of
# its cell, everything else about the grid correct - is produced from
# outside. The tile has to hold its picture through all of them.
HOSTILE_PAGE_CSS = """
img { margin-right: 0 !important; margin-left: auto !important; }
.gradio-container img { object-position: right center !important; }
.prose img, .gradio-container .prose img { width: auto !important; max-width: 100% !important; }
#minipaint_clipboard_root .minipaint-clip-thumb { justify-content: flex-end !important; }
#minipaint_clipboard_root .minipaint-clip-item { align-items: flex-end !important; text-align: right !important; }
"""


def check_a_hostile_page_cannot_move_a_picture(r: Results, page) -> None:
    """The host page cannot push a thumbnail off-centre.

    This grid was reported off-centre three times, and each fix centred it
    again on the page the suite builds - which is a Gradio page and nothing
    else. The page it has to be right on carries Forge's stylesheet, a
    theme, and every other extension the user installed, and any of those
    can say `!important` about an image.

    So the rules that decide where a picture is drawn are checked against a
    page that is trying to move it, rather than against a clean one.
    """
    style = page.add_style_tag(content=HOSTILE_PAGE_CSS)
    try:
        time.sleep(0.5)
        tiles = page.evaluate(TILES_JS)
        r.check("a hostile page still leaves every picture drawn in the middle of its tile",
                tiles and not adrift(tiles), str(adrift(tiles)))
        r.check("and still inside it", tiles and not [
            t for t in tiles if t["spillY"] > 1 or t["spillX"] > 1],
            str([(t["name"], t["spillX"], t["spillY"]) for t in tiles
                 if t["spillY"] > 1 or t["spillX"] > 1]))
    finally:
        style.evaluate("el => el.remove()")
        time.sleep(0.4)


# --------------------------------------------------------------------------
# Sending out
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Paging, patching, and the sort that does not disturb anything
# --------------------------------------------------------------------------

#: The identity of every tile on screen, and enough about it to tell one
#: render from another. The stamp is written onto the element once and never
#: again, so a tile that survived a patch carries the stamp it was built
#: with and a tile that was rebuilt does not.
STAMP_TILES_JS = """() => {
    let n = 0;
    return Array.from(document.querySelectorAll('.minipaint-clip-item')).map(el => {
        if (!el.dataset.stamp) { el.dataset.stamp = 'tile-' + (n++) + '-' + Math.random().toString(16).slice(2); }
        return { asset: el.dataset.asset, name: el.dataset.name, stamp: el.dataset.stamp };
    });
}"""

READ_TILES_JS = """() => Array.from(document.querySelectorAll('.minipaint-clip-item')).map(el => (
    { asset: el.dataset.asset, name: el.dataset.name, stamp: el.dataset.stamp || '' }))"""


def library_state(page):
    return page.evaluate("() => window.minipaintClipboard.libraryState()")


def show_page(page, options):
    page.evaluate("o => window.minipaintClipboard.library(o)", options)
    time.sleep(1.2)


def pager(page):
    return page.evaluate("""() => {
        const row = document.querySelector('.minipaint-clip-pager');
        if (!row) { return null; }
        const box = row.querySelector('.minipaint-clip-pager-number');
        const back = row.querySelector('.minipaint-clip-pager-back');
        const next = row.querySelector('.minipaint-clip-pager-next');
        const mark = row.querySelector('.minipaint-clip-pager-mark');
        return {
            hidden: !!row.hidden,
            number: box ? box.value : '',
            of: (row.querySelector('.minipaint-clip-pager-of') || {}).textContent || '',
            count: (row.querySelector('.minipaint-clip-pager-count') || {}).textContent || '',
            backOff: !!(back && back.disabled), nextOff: !!(next && next.disabled),
            mark: mark && !mark.hidden ? mark.textContent : ''
        };
    }""")


def check_paging(r: Results, page, library) -> None:
    """A page of the library, its row of controls, and the ends of it.

    Enough pictures are put in the folder to page through at the smallest
    size the route allows - the clamp is part of the contract, so the test
    works inside it rather than around it - and taken out again afterwards.
    """
    from PIL import Image

    made = []
    for number in range(18):
        name = f"page-{number:02d}.png"
        Image.new("RGB", (40, 30), (number * 10 % 255, 60, 120)).save(library / name)
        made.append(library / name)
    open_clipboard(page)
    show_page(page, {"size": 10, "page": 0, "sort": "name_asc", "refresh": True})
    try:
        state = library_state(page)
        r.check("a page shows exactly `size` tiles", state["shown"] == 10, str(state["shown"]))
        r.check("and the answer's totals describe the whole library, not the page",
                state["total"] == 24 and state["pages"] == 3 and state["page"] == 0, str(state))
        row = pager(page)
        r.check("the pager says where you are and how many there are",
                row and not row["hidden"] and row["number"] == "1" and row["of"].strip() == "of 3", str(row))
        r.check("and the count is the whole library, because that is the number a person wants",
                row and row["count"].startswith("24 picture"), str(row and row["count"]))
        r.check("Back is disabled at the start, and Next is not - disabled rather than hidden, so the row does not change width",
                row and row["backOff"] is True and row["nextOff"] is False, str(row))

        first = [t["name"] for t in page.evaluate(READ_TILES_JS)]
        page.evaluate("() => document.querySelector('.minipaint-clip-pager-next').click()")
        time.sleep(1.4)
        second = [t["name"] for t in page.evaluate(READ_TILES_JS)]
        r.check("Next reaches the next pictures, and only them",
                len(second) == 10 and not set(second) & set(first), f"{first[:3]} -> {second[:3]}")

        page.evaluate("() => document.querySelector('.minipaint-clip-pager-next').click()")
        time.sleep(1.4)
        row = pager(page)
        r.check("and at the end Next is disabled and Back is not",
                row and row["nextOff"] is True and row["backOff"] is False and row["number"] == "3", str(row))
        r.check("the last page is what is left over", len(page.evaluate(READ_TILES_JS)) == 4,
                str(len(page.evaluate(READ_TILES_JS))))

        page.evaluate("() => document.querySelector('.minipaint-clip-pager-back').click()")
        page.evaluate("() => document.querySelector('.minipaint-clip-pager-back').click()")
        time.sleep(1.6)
        r.check("Back comes back to exactly the pictures that were there",
                [t["name"] for t in page.evaluate(READ_TILES_JS)] == first, str(first[:3]))

        page.evaluate("""() => {
            const box = document.querySelector('.minipaint-clip-pager-number');
            box.value = '99';
            box.dispatchEvent(new Event('change', {bubbles: true}));
        }""")
        time.sleep(1.4)
        r.check("go-to is clamped rather than refused: a page past the end is the last one that exists",
                library_state(page)["page"] == 2, str(library_state(page)["page"]))

        page.evaluate("""() => {
            const grid = document.querySelector('.minipaint-clip-grid');
            grid.focus();
            grid.dispatchEvent(new KeyboardEvent('keydown', {key: 'PageUp', bubbles: true}));
        }""")
        time.sleep(1.4)
        r.check("PageUp moves a page while the grid has focus, because a listbox is expected to",
                library_state(page)["page"] == 1, str(library_state(page)["page"]))

        # -- a selection is browser state and does not belong to a page
        page.evaluate("() => { const c = document.querySelector('.minipaint-clip-item'); if (c) { c.click(); } }")
        time.sleep(0.6)
        chosen = page.evaluate("() => window.minipaintClipboard.debug().selected")
        r.check("paging: a picture on this page is selected", bool(chosen), repr(chosen))
        page.evaluate("() => document.querySelector('.minipaint-clip-pager-next').click()")
        time.sleep(1.4)
        r.check("and it is still selected a page away, because the send path names it by id",
                page.evaluate("() => window.minipaintClipboard.debug().selected") == chosen, chosen)
        r.check("and the pager marks the page that is holding it",
                "selection on page 2" in (pager(page) or {}).get("mark", ""), str(pager(page)))
        r.check("so the Send menu is still live, rather than greyed out with a picture plainly chosen",
                page.evaluate("""() => { window.minipaintClipboard.toggleMenu();
                    const items = () => Array.from(document.querySelectorAll('.minipaint-clip-menu .minipaint-clip-menu-item'));
                    const send = items().filter(b => b.textContent.indexOf('Send selected') === 0)[0];
                    if (send) { send.click(); }
                    const on = items().filter(b => !b.disabled && b.textContent.indexOf('\u2039') !== 0
                                                   && b.textContent !== 'Cancel').length;
                    window.minipaintClipboard.closeMenu();
                    return on; }""") >= 1)
    finally:
        for one in made:
            try:
                one.unlink()
            except OSError:
                pass
        show_page(page, {"size": 60, "page": 0, "sort": "name_asc", "refresh": True})


def check_patching(r: Results, page, library) -> None:
    """An import that lands on your page adds ONE node, and nothing else moves.

    Asserted by element identity: a stamp is written onto each tile once and
    never again, so a tile that survived the patch carries the stamp it was
    built with and a tile that was rebuilt does not. Markup equality would
    pass for a grid that was torn out and rebuilt, which is exactly the thing
    that loses a scroll position and a decoded picture.
    """
    from PIL import Image

    open_clipboard(page)
    show_page(page, {"size": 60, "page": 0, "sort": "name_asc", "refresh": True})
    before = page.evaluate(STAMP_TILES_JS)
    r.check("patching: the grid is drawn and stamped", len(before) == 6, str(len(before)))

    page.evaluate("() => { const g = document.querySelector('.minipaint-clip-grid'); g.scrollTop = 24; }")
    scrolled = page.evaluate("() => document.querySelector('.minipaint-clip-grid').scrollTop")

    Image.new("RGB", (200, 200), (10, 200, 60)).save(library / "aaa-new.png")
    page.evaluate("() => window.minipaintClipboard.library({refresh: true})")
    time.sleep(2.5)

    after = page.evaluate(READ_TILES_JS)
    kept = [t for t in after if t["stamp"]]
    fresh = [t for t in after if not t["stamp"]]
    r.check("one picture in means one node in", len(after) == 7 and len(fresh) == 1, f"{len(after)} tiles, {len(fresh)} new")
    r.check("AND THE OTHER SIX ARE THE SAME ELEMENTS, not new ones that look the same",
            sorted(t["stamp"] for t in kept) == sorted(t["stamp"] for t in before), str(len(kept)))
    r.check("the new one is in its place in the order, not appended",
            after[0]["name"] == "aaa-new.png", str([t["name"] for t in after]))
    r.check("and the scroll position survives the patch",
            page.evaluate("() => document.querySelector('.minipaint-clip-grid').scrollTop") == scrolled,
            str(page.evaluate("() => document.querySelector('.minipaint-clip-grid').scrollTop")))

    page.evaluate("() => { const c = document.querySelectorAll('.minipaint-clip-item')[3]; if (c) { c.click(); } }")
    time.sleep(0.6)
    chosen = page.evaluate("() => window.minipaintClipboard.debug().selected")
    (library / "aaa-new.png").unlink()
    page.evaluate("() => window.minipaintClipboard.library({refresh: true})")
    time.sleep(2.5)
    left = page.evaluate(READ_TILES_JS)
    r.check("a picture out means one node out, and the rest are still the same elements",
            len(left) == 6 and len([t for t in left if t["stamp"]]) == 6, str(len(left)))
    r.check("and the selection survives a patch, for free, because its tile was not rebuilt",
            page.evaluate("() => window.minipaintClipboard.debug().selected") == chosen, chosen)


def check_the_failure_modes_have_answers(r: Results, page, library) -> None:
    """Every failure in section 15 has a defined thing the page says.

    The whole cost of the last six builds was a fault with no symptom, so
    "it went quiet" is not an outcome any of these is allowed to have.
    """
    from PIL import Image

    open_clipboard(page)
    show_page(page, {"size": 60, "page": 0, "sort": "name_asc", "refresh": True})

    # -- a selection gives way to truth, and only to truth
    page.evaluate("() => { const c = document.querySelector('.minipaint-clip-item'); if (c) { c.click(); } }")
    time.sleep(0.6)
    chosen = page.evaluate("() => window.minipaintClipboard.debug().selected")
    name = page.evaluate("() => (document.querySelector('.minipaint-clip-item') || {}).dataset.name")
    r.check("failure modes: a picture is selected", bool(chosen) and bool(name), repr(chosen))
    copy = (library / name).read_bytes()
    (library / name).unlink()
    page.evaluate("() => window.minipaintClipboard.library({refresh: true})")
    time.sleep(2.5)
    r.check("the selected picture being deleted clears the selection - a selection gives way to truth",
            page.evaluate("() => window.minipaintClipboard.debug().selected") == "",
            repr(page.evaluate("() => window.minipaintClipboard.debug().selected")))
    (library / name).write_bytes(copy)
    page.evaluate("() => window.minipaintClipboard.library({refresh: true})")
    time.sleep(2.5)

    # -- a thumbnail that will not load costs that tile its picture, and
    #    nothing else on the page anything at all
    # A real 404 from the real route, rather than a stubbed one: an id the
    # library has never minted is exactly what a tile holds when the file
    # behind it has gone.
    page.evaluate("""() => {
        const tile = document.querySelectorAll('.minipaint-clip-item')[1];
        tile.querySelector('img').src = '/minipaint-clipboard/image/' + '0'.repeat(32) + '?thumb=1&v=1-1';
    }""")
    time.sleep(2.5)
    r.check("a thumbnail that 404s marks its tile and leaves its name readable",
            page.evaluate("""() => {
                const tile = document.querySelectorAll('.minipaint-clip-item')[1];
                return tile.classList.contains('minipaint-clip-item-missing') && !!tile.dataset.name;
            }"""))
    r.check("and the page is otherwise unaffected",
            len(page.evaluate(READ_TILES_JS)) == 6, str(len(page.evaluate(READ_TILES_JS))))

    # -- the index route unreachable: keep what is on screen, say so, retry
    held = [t["name"] for t in page.evaluate(READ_TILES_JS)]
    page.route("**/minipaint-clipboard/library*", lambda route: route.abort())
    try:
        page.evaluate("() => window.minipaintClipboard.library({})")
        time.sleep(2.5)
        r.check("an index that cannot be reached keeps the tiles that were right when they were drawn",
                [t["name"] for t in page.evaluate(READ_TILES_JS)] == held, str(held))
        r.check("and the page says FORGE IS NOT ANSWERING - the one line that means the server is gone",
                page.evaluate("() => window.minipaintClipboard.debug().offline.server") is True)
        r.check("in those words, and not in the retired ones about a live connection",
                page.evaluate("""() => { const bar = document.querySelector('.minipaint-clip-offline');
                    return bar && !bar.hidden ? bar.textContent : ''; }""").startswith("Forge is not answering"),
                page.evaluate("""() => { const bar = document.querySelector('.minipaint-clip-offline');
                    return bar ? bar.textContent.slice(0, 60) : 'NO BAR'; }"""))
        r.check("and it is trying again by itself rather than only offering a button",
                page.evaluate("() => window.minipaintClipboard.debug().retrying") is True)
    finally:
        page.unroute("**/minipaint-clipboard/library*")
    page.evaluate("() => window.minipaintClipboard.library({refresh: true})")
    time.sleep(2.5)
    r.check("and the line goes by itself the moment the server answers",
            page.evaluate("() => window.minipaintClipboard.debug().offline.server") is False)


def check_sorting_is_separate_from_drawing(r: Results, page) -> None:
    """Changing the sort does not touch a tile until the new order arrives.

    And a sort that cannot be fetched leaves the grid exactly as it was AND
    SAYS SO - where a failed round trip used to leave it stale with nothing
    to indicate the sort did not take.
    """
    open_clipboard(page)
    show_page(page, {"size": 60, "page": 0, "sort": "name_asc"})
    before = [t["name"] for t in page.evaluate(READ_TILES_JS)]

    # Asked for and read back in the SAME turn of the browser's own loop, so
    # what is asserted is the state the sort leaves the page in before any
    # answer can possibly have arrived - rather than a state that depends on
    # how long a stubbed network was told to take.
    asked = page.evaluate("""() => {
        window.minipaintClipboard.setSort('name_desc');
        return {
            busy: document.querySelector('.minipaint-clip-grid').getAttribute('aria-busy'),
            names: Array.from(document.querySelectorAll('.minipaint-clip-item')).map(e => e.dataset.name)
        };
    }""")
    r.check("the moment a sort is asked for, the grid is marked busy", asked["busy"] == "true", str(asked["busy"]))
    r.check("and not one tile has moved: the order on screen is the one that arrived last",
            asked["names"] == before, str(asked["names"]))
    time.sleep(3.0)
    r.check("and when the new order lands, it is the one on screen",
            [t["name"] for t in page.evaluate(READ_TILES_JS)] == list(reversed(before)),
            str([t["name"] for t in page.evaluate(READ_TILES_JS)]))

    page.route("**/minipaint-clipboard/library*", lambda route: route.abort())
    try:
        held = [t["name"] for t in page.evaluate(READ_TILES_JS)]
        page.evaluate("() => window.minipaintClipboard.library({sort: 'name_asc'})")
        time.sleep(2.0)
        r.check("a sort that cannot be fetched leaves the grid exactly as it was",
                [t["name"] for t in page.evaluate(READ_TILES_JS)] == held, str(held))
        r.check("and says so, rather than leaving a stale grid with nothing to show for it",
                page.evaluate("""() => { const s = document.getElementById('minipaint_clipboard_status');
                    return s ? s.textContent : ''; }""").find("could not be re-read") >= 0
                or page.evaluate("() => window.minipaintClipboard.debug().offline.server") is True)
    finally:
        page.unroute("**/minipaint-clipboard/library*")
    page.evaluate("() => window.minipaintClipboard.library({sort: 'name_asc', refresh: true})")
    time.sleep(2.0)


def check_a_thumbnail_is_fetched_once(r: Results, page) -> None:
    """A tile whose version has not changed does not ask for its picture again.

    From PerformanceObserver, which this page already carries: what the
    browser actually put on the wire, rather than what the code meant to.
    """
    open_clipboard(page)
    page.evaluate("""() => {
        window.__minipaintImageRequests = [];
        const observer = new PerformanceObserver(list => {
            for (const entry of list.getEntries()) {
                if (String(entry.name).indexOf('/minipaint-clipboard/image/') !== -1) {
                    window.__minipaintImageRequests.push(entry.name);
                }
            }
        });
        observer.observe({ type: 'resource', buffered: false });
    }""")
    time.sleep(0.5)
    page.evaluate("() => window.minipaintClipboard.library({refresh: true, quiet: true})")
    time.sleep(2.5)
    asked = page.evaluate("() => window.__minipaintImageRequests.length")
    r.check("a re-draw of the same pictures puts NO thumbnail request on the wire",
            asked == 0, f"{asked} request(s)")
    r.check("because every tile's URL carries the version of its bytes",
            page.evaluate("""() => Array.from(document.querySelectorAll('.minipaint-clip-item img'))
                .every(i => i.src.indexOf('v=') !== -1)"""))

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
        r.check(f"send to {key}: and nothing is reported as lost", failure_toast(page) == "", failure_toast(page))
        r.check(f"send to {key}: and the page says it went", toast(page).startswith("Sent "), toast(page))
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
    #
    # How long this takes is part of what is being checked. The picture used
    # to be placed only after the queued event had been given twelve seconds
    # to arrive, and on a phone - where the page is backgrounded constantly
    # and Gradio's stream does not survive it - the queue is down far more
    # often than not, so every send paid that wait. The page places the
    # picture itself now and lets the event catch up, so a dead queue costs
    # a round trip, not a deadline.
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
        record_toasts(page)  # its clock starts here, one line before the press
        r.check("dead queue: the send is attempted", send_selected(page, "img2img") == "clicked")
        landed, said = False, []
        for _ in range(30):
            time.sleep(1)
            landed = landed or host_value() > 0
            said = recorded_toasts(page)
            # Both halves: the picture, and the line explaining how it got
            # there. They are written one after the other but a poll can land
            # between them, so neither alone ends the wait.
            if landed and said:
                break
        r.check("dead queue: the picture still reaches img2img, over plain HTTP",
                landed, f"host textbox length {host_value()}")
        # Timed from the page's own clock, against the notice the user
        # actually sees. Measuring the textbox instead would have called a
        # send fast because the canvas had put its own picture back.
        when = said_when(page, "Sent ")
        r.check("dead queue: and it does not wait out a deadline first",
                when is not None and when < 8, "never said it went" if when is None else f"{when:.1f}s after the press")
        r.check("dead queue: and the page says how it got there",
                any("lost its connection" in one or one.startswith("Sent ") for one in said), str(said))
    finally:
        page.unroute("**/queue/**")
        page.unroute("**/gradio_api/**")
        time.sleep(3)


def check_a_component_destination_fills_without_the_queue(r: Results, page, targets) -> None:
    """The destinations the server writes are filled here instead.

    img2img and Inpaint were always finishable in the browser: their picture
    goes in a hidden textbox on the page. Extras and the two ImageStitch
    galleries hold their value in the component, and the server fills them
    by returning a new value - a Gradio event, and so the queue, which is
    the half of the connection that had stopped. Those three were the sends
    that kept doing nothing.

    A component that takes uploads will take the same picture from its own
    file input, and an upload is ordinary HTTP. So the queue is cut, and the
    gallery has to end up holding the picture anyway.
    """
    open_clipboard(page)
    key = "stitch_txt2img" if "stitch_txt2img" in targets else ("extras" if "extras" in targets else "")
    elem_id = getattr(targets.get(key), "elem_id", "") or ""
    if not elem_id:
        r.check("dead queue: a component destination is on the page to test", False, f"{key!r}")
        return
    label = {"stitch_txt2img": "ImageStitch (txt2img)", "extras": "Extras"}[key]

    def pictures():
        return page.evaluate("id => { const h = document.getElementById(id);"
                             " return h ? h.querySelectorAll('img').length : -1; }", elem_id)

    if not select_first(page):
        r.check(f"dead queue: a picture is selected before sending to {label}", False, "no selection")
        return
    before = pictures()
    # Only the queue. The upload route, the event stream and the thumbnails
    # are plain HTTP and were working throughout the reported failure.
    page.route("**/queue/**", lambda route: route.abort())
    try:
        record_toasts(page, reset=True)
        r.check(f"dead queue: the menu offers {label}", send_selected(page, label) == "clicked")
        landed, said = False, []
        for _ in range(30):
            time.sleep(1)
            landed = landed or pictures() > before
            said = recorded_toasts(page)
            if landed and said:
                break
        r.check(f"dead queue: {label} is filled anyway, over the upload route",
                landed, f"pictures in {elem_id}: {before} -> {pictures()}")
        r.check("dead queue: and the page says it went",
                any(one.startswith("Sent ") for one in said), str(said))
        # The picture arrives at once now; the standing notice is raised
        # later, when the server's receipt for the same send has not come.
        # Reading it the moment the picture lands is reading it too early -
        # which passed here and failed on a slower machine, because the two
        # are not the same event and never were.
        offered = False
        for _ in range(24):
            offered = page.evaluate(
                "() => { const b = document.querySelector('.minipaint-clip-offline');"
                " return !!(b && !b.hidden && b.querySelector('.minipaint-clip-offline-reconnect')); }")
            if offered:
                break
            time.sleep(1)
        r.check("dead queue: the page offers to reconnect rather than leaving it to be guessed", offered)
    finally:
        page.unroute("**/queue/**")
        time.sleep(3)



def check_a_picture_handed_in_arrives_without_the_queue(r: Results, page, library) -> None:
    """The other direction: a result from another tab, into the library.

    The button under a txt2img, img2img or Extras result picks its picture
    in the browser and hands it to the server as a Gradio event - the same
    half that stops when the queue stops, which is why "I used to be able to
    send from other tabs into Clipboard" stopped being true. The library's
    import is an ordinary POST, so the page can finish the handover itself:
    it fetches the file the host is already serving and posts that, which
    also keeps whatever metadata Forge wrote into it.
    """
    open_clipboard(page)
    if not select_first(page):
        r.check("handed in: a picture is in the library to hand back", False, "no selection")
        return
    asset = box(page, "minipaint_clipboard_selected")
    # The picture as the host would serve one: a URL this page can fetch.
    url = f"/minipaint-clipboard/image/{asset}"

    page.evaluate("() => window.minipaintClipboard.toggleMenu()")
    time.sleep(0.4)
    r.check("handed in: the intercept can be turned on", menu_click(page, "Intercept") == "clicked")
    time.sleep(2.5)
    # The page's own answer, not a hidden box's: the switch is saved over
    # this tab's route now, so what the menu is acting on is what matters.
    r.check("handed in: and the page knows it is on",
            page.evaluate("() => window.minipaintClipboard.debug().intercept") is True)

    before = len(list(library.glob("*")))
    page.route("**/queue/**", lambda route: route.abort())
    try:
        record_toasts(page, reset=True)
        page.evaluate("u => window.minipaintClipboard.receiveOverHttp([{image: {url: u}}])", url)
        landed, said = False, []
        for _ in range(20):
            time.sleep(1)
            landed = len(list(library.glob("*"))) > before
            said = recorded_toasts(page)
            if landed and said:
                break
        r.check("handed in: the picture reaches the library with the queue cut",
                landed, f"{before} file(s) before, {len(list(library.glob('*')))} after")
        r.check("handed in: and the page says it went in",
                any("in Clipboard" in one for one in said), str(said))
    finally:
        page.unroute("**/queue/**")
        time.sleep(2)
        # Put the intercept back: it is stored, and the next suite inherits it.
        page.evaluate("() => window.minipaintClipboard.toggleMenu()")
        time.sleep(0.4)
        menu_click(page, "Intercept")
        time.sleep(2.0)
        page.evaluate("() => window.minipaintClipboard.closeMenu()")



def check_a_send_reaches_a_framework_owned_input(r: Results, page, targets) -> None:
    """The server hears the send even when its inputs are framework-owned.

    Every send this tab makes crosses to the server by writing a hidden
    textbox. That write was a plain assignment, and a plain assignment is
    exactly what a framework does not hear: it keeps its own record of what
    an input holds, an assignment touches the DOM and not the record, so the
    framework compares the two, sees no change and sends nothing. The write
    succeeds and the event never happens - which is what a user's logs
    showed, every send "delivered from the page" and not one of them ever
    acknowledged, on a desktop browser that never lost its connection.

    It worked on the Gradio this suite runs against, which is why nothing
    caught it. So the input is made to behave the way a framework-owned one
    does: its own value property ignores assignment, while the prototype's
    setter - the way in that every framework leaves open - still works.
    """
    open_clipboard(page)
    if not select_first(page):
        r.check("framework-owned input: a picture is selected first", False, "no selection")
        return

    ignored = page.evaluate("""() => {
        const host = document.getElementById('minipaint_clipboard_send_request');
        const el = host && host.querySelector('textarea, input');
        if (!el) { return false; }
        const prototype = el.tagName === 'TEXTAREA'
            ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
        const native = Object.getOwnPropertyDescriptor(prototype, 'value');
        Object.defineProperty(el, 'value', {
            configurable: true,
            get() { return native.get.call(this); },
            set(_v) { /* owned by the framework: an assignment is not a change */ }
        });
        return true;
    }""")
    r.check("framework-owned input: the hidden box is owned the way a framework owns it", ignored)
    r.check("framework-owned input: and a plain assignment really is ignored",
            page.evaluate("""() => {
                const host = document.getElementById('minipaint_clipboard_send_request');
                const el = host.querySelector('textarea, input');
                el.value = 'ignore me';
                return el.value !== 'ignore me';
            }"""))
    try:
        before = box(page, "minipaint_clipboard_send_ack")
        r.check("framework-owned input: the send is attempted", send_selected(page, "img2img") == "clicked")
        answered = False
        for _ in range(20):
            time.sleep(1)
            if box(page, "minipaint_clipboard_send_ack") not in (None, "", before):
                answered = True
                break
        r.check("framework-owned input: the server still receives the send",
                answered, repr(box(page, "minipaint_clipboard_send_ack")))
    finally:
        page.evaluate("""() => {
            const host = document.getElementById('minipaint_clipboard_send_request');
            const el = host && host.querySelector('textarea, input');
            if (el) { delete el.value; }
        }""")
        time.sleep(0.5)


def check_a_send_survives_a_box_the_host_never_hears(r: Results, page, targets) -> None:
    """The send still reaches the server when the written box is not heard.

    THE INSTALL THIS IS FOR. One user's logs, across four builds of this
    extension, contain not a single acknowledged send - and in the same logs,
    on the same pages, fifty-six Add to Queue round trips that went to the
    server and came back. The difference between the two is not the
    connection, the session or the queue, all of which were working: it is
    that Add to Queue is a button somebody presses and a send was a hidden
    box written by script. Three fixes were spent on better ways to write the
    box; the logs after each say exactly what they said before.

    So the send does not rest on the write any more. It writes the box - that
    is still the request - and then presses a hidden button, and the server
    answers whichever arrives. Here the write is made unhearable the way that
    install behaves: the events it dispatches are stopped before they reach
    the element, so the framework is never told. Nothing else is touched.

    The press has to carry the send through that. If it ever stops doing so
    this check fails, and the extension is back to the state those logs
    describe.
    """
    open_clipboard(page)
    if not select_first(page):
        r.check("unheard box: a picture is selected first", False, "no selection")
        return
    deafened = page.evaluate("""() => {
        const host = document.getElementById('minipaint_clipboard_send_request');
        const el = host && host.querySelector('textarea, input');
        if (!el) { return false; }
        // Capture on an ancestor: the event is stopped on the way down, so
        // the listener the framework put on the element never runs. The
        // value still lands in the DOM, exactly as it does today.
        window.__minipaintDeafen = function (event) {
            if (event.target === el) { event.stopPropagation(); }
        };
        for (const kind of ['input', 'change']) {
            document.addEventListener(kind, window.__minipaintDeafen, true);
        }
        return true;
    }""")
    r.check("unheard box: the host is made deaf to the write", deafened)
    try:
        before = box(page, "minipaint_clipboard_send_ack")
        r.check("unheard box: the send is attempted", send_selected(page, "img2img") == "clicked")
        answered = False
        for _ in range(20):
            time.sleep(1)
            if box(page, "minipaint_clipboard_send_ack") not in (None, "", before):
                answered = True
                break
        r.check("unheard box: the server still receives the send, carried by the press",
                answered, repr(box(page, "minipaint_clipboard_send_ack")))
    finally:
        page.evaluate("""() => {
            if (!window.__minipaintDeafen) { return; }
            for (const kind of ['input', 'change']) {
                document.removeEventListener(kind, window.__minipaintDeafen, true);
            }
            window.__minipaintDeafen = null;
        }""")
        time.sleep(0.5)


def check_the_notice_takes_itself_down(r: Results, page) -> None:
    """The page gets its own connection back, without being asked to.

    The notice used to stand until somebody pressed Check again - the page
    asking a person to do the one thing it could do itself, and the honest
    answer to "why can I not just have it back" was that nothing was trying.
    Something is now: while the line is up the page asks the server for the
    library again on a backing-off timer, and takes the line down when an
    answer arrives.

    Driven here by putting the line up with the server perfectly healthy,
    which is exactly the state a page is in a moment after a round trip is
    lost and comes back.
    """
    open_clipboard(page)
    page.evaluate("() => window.minipaintClipboard.debug && null")
    shown = page.evaluate("""() => {
        const api = window.minipaintClipboard;
        if (!api || typeof api.showOffline !== 'function') { return false; }
        api.showOffline(true);
        const bar = document.querySelector('.minipaint-clip-offline');
        return !!(bar && !bar.hidden);
    }""")
    r.check("the connection notice can be raised", shown)
    if not shown:
        return
    r.check("and says it is trying again by itself, rather than only offering a button",
            "by itself" in page.evaluate("() => { const b = document.querySelector('.minipaint-clip-offline-text');"
                                         " return b ? b.textContent : ''; }"))
    gone = False
    for _ in range(25):
        time.sleep(1)
        if page.evaluate("() => { const b = document.querySelector('.minipaint-clip-offline');"
                         " return !b || b.hidden; }"):
            gone = True
            break
    r.check("and it takes itself down once the server answers, with nothing pressed", gone)
    r.check("leaving no timer behind on a page that is fine",
            page.evaluate("() => window.minipaintClipboard.debug().retrying") is False,
            str(page.evaluate("() => window.minipaintClipboard.debug().retrying")))


def check_the_page_can_say_why_a_send_was_silent(r: Results, page) -> None:
    """A silent send produces facts, not another guess.

    Four builds went on this because no log could tell three faults apart:
    the event never fired, it fired and the request failed, or it fired and
    the answer never came back. The page can distinguish them - it knows what
    its own boxes hold, the browser will tell it what left, and the host's
    config says where the host thinks it is - so it says so, and the next
    report names the fault instead of inviting a fifth guess.
    """
    r.check("the page is watching what the host's framework puts on the wire",
            page.evaluate("() => !!(window.minipaintNetJournal && window.minipaintNetJournal.watching())"))
    seen = page.evaluate("() => window.minipaintNetJournal.since(0)")
    r.check("and has seen the host's own API calls on this page",
            isinstance(seen, list) and len(seen) > 0 and all("path" in row for row in seen),
            f"{len(seen) if isinstance(seen, list) else seen} call(s)")
    r.check("which it can put in a log line",
            "request(s):" in page.evaluate("() => window.minipaintNetJournal.sentence(0)"),
            page.evaluate("() => window.minipaintNetJournal.sentence(0)"))
    # The wiring itself. An event whose outputs name a component that is not
    # in this build cannot run, and it fails exactly the way an unreachable
    # server fails - so "is it even on the page" has to be answerable without
    # pressing anything.
    for name, trigger in (("send_request", "input"), ("send_press", "click")):
        wiring = page.evaluate("id => window.minipaintHostWiring(id)", f"minipaint_clipboard_{name}")
        r.check(f"the page can see that {name} is wired for {trigger}",
                wiring.get("known") and wiring.get("elements") == 1 and wiring.get("components") == 1
                and trigger in (wiring.get("triggers") or []), str(wiring))
        r.check(f"and that its {trigger} names nothing that is not on this page",
                wiring.get("missing") == 0, str(wiring))
    r.check("and says so in a clause a log line can carry",
            "wired for" in page.evaluate("() => window.minipaintHostWiringNote('the request box',"
                                         " 'minipaint_clipboard_send_request')"),
            page.evaluate("() => window.minipaintHostWiringNote('the request box', 'minipaint_clipboard_send_request')"))
    r.check("a control nothing is wired to is named as such, not merely missing",
            "NO EVENT IS WIRED" in page.evaluate("() => window.minipaintHostWiringNote('the payload box',"
                                                 " 'minipaint_clipboard_payload')"),
            page.evaluate("() => window.minipaintHostWiringNote('the payload box', 'minipaint_clipboard_payload')"))
    # The fault this is really for: the trigger is wired, and the event still
    # cannot run because one of the components it writes belongs to a build
    # this page is not. Put one in and the page has to name it.
    broken = page.evaluate("""() => {
        const config = window.gradio_config;
        const mine = config.components.find(c => c.props && c.props.elem_id === 'minipaint_clipboard_send_press');
        const fake = {targets: [[mine.id, 'click']], inputs: [], outputs: [987654321], backend_fn: true};
        config.dependencies.push(fake);
        try { return window.minipaintHostWiringNote('send button', 'minipaint_clipboard_send_press'); }
        finally { config.dependencies.pop(); }
    }""")
    r.check("an event naming a component from another build is named as one the host cannot run",
            "NOT ON THIS PAGE" in broken, broken)
    r.check("and the page is left as it was",
            page.evaluate("() => window.minipaintHostWiring('minipaint_clipboard_send_press').missing") == 0)

    # The one configuration that blocks every framework request while leaving
    # everything this extension does over a relative URL working. This page is
    # served straight, so it has nothing to report; the verdict is checked
    # against the roots that do, which the function takes as arguments because
    # a browser will not let `location.protocol` be redefined.
    straight = page.evaluate("() => window.minipaintHostRoot()")
    r.check("on a page the host addresses correctly, there is nothing to report",
            straight.get("known") and straight.get("ok"), str(straight))
    crossed = page.evaluate("() => window.minipaintHostRoot('http://127.0.0.1:7860',"
                            " {origin: 'https://forge.example.com', protocol: 'https:'})")
    r.check("a host that tells an https page to call it over http is named as mixed content",
            crossed.get("known") and crossed.get("ok") is False and crossed.get("mixed") is True
            and "mixed content" in crossed.get("note", ""), str(crossed)[:200])
    r.check("and the note says where the fix is",
            "x-forwarded-proto" in crossed.get("note", ""), crossed.get("note", "")[:200])
    other = page.evaluate("() => window.minipaintHostRoot('https://elsewhere.example.com',"
                          " {origin: 'https://forge.example.com', protocol: 'https:'})")
    r.check("a host on another origin entirely is reported, but not as mixed content",
            other.get("ok") is False and other.get("mixed") is False and "CORS" in other.get("note", ""),
            str(other)[:160])
    same = page.evaluate("() => window.minipaintHostRoot('https://forge.example.com/x',"
                         " {origin: 'https://forge.example.com', protocol: 'https:'})")
    r.check("and a host that agrees with its own page is not reported at all", same.get("ok") is True, str(same))


def check_a_render_cannot_take_the_selection(r: Results, page) -> None:
    """A render must not clear a selection this page made.

    A selection is made in the browser and reaches the server as a Gradio
    event, so while the queue is not delivering, the server's idea of what
    is selected stays empty - and every render carries that back. Adopting
    it took the picture out from under the user: the Send menu came up with
    every destination greyed out and "Select an image first" at the top,
    with a picture plainly selected on the grid, and a send already waiting
    on its fallback lost the asset it was about to send.
    """
    open_clipboard(page)
    if not select_first(page):
        r.check("a render cannot take the selection: a picture is selected first", False, "no selection")
        return
    chosen = page.evaluate("() => window.minipaintClipboard.debug().selected")
    # What a render from a server that never heard about the selection looks
    # like when it lands: the box emptied, then the page told to re-read it.
    page.evaluate("""() => {
        const host = document.getElementById('minipaint_clipboard_selected');
        const t = host && host.querySelector('textarea,input');
        if (t) { t.value = ''; t.dispatchEvent(new Event('input', {bubbles: true})); }
        window.minipaintClipboard.afterRender();
    }""")
    time.sleep(0.5)
    kept = page.evaluate("() => window.minipaintClipboard.debug().selected")
    r.check("a render saying nothing is selected does not clear the selection",
            kept == chosen and bool(kept), f"{chosen!r} -> {kept!r}")
    r.check("and the tile is still shown as the selected one",
            page.evaluate("() => { const el = document.querySelector('.minipaint-clip-item.minipaint-clip-selected');"
                          " return el ? el.dataset.asset : ''; }") == chosen)
    page.evaluate("() => window.minipaintClipboard.toggleMenu()")
    time.sleep(0.4)
    menu_click(page, "Send selected to")
    time.sleep(0.4)
    offered = page.evaluate(MENU_ITEMS_JS)
    r.check("and the send menu still offers its destinations",
            any(one.startswith("img2img") and "[disabled]" not in one for one in offered), str(offered))
    page.evaluate("() => window.minipaintClipboard.closeMenu()")
    time.sleep(0.3)


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


# --------------------------------------------------------------------------
# The Send-to contract: deliver, prove, then show
# --------------------------------------------------------------------------

#: Which of the host's tab panels is on screen. By identity, never by
#: position: the panels and their buttons only line up while every panel has
#: one, and a picture sent to Extras once opened PNG Info because of it.
VISIBLE_PANEL_JS = ("() => (Array.from(document.querySelectorAll('#tabs > .tabitem'))"
                    ".filter(i => getComputedStyle(i).display !== 'none')[0] || {}).id || ''")

#: The panel each destination is expected to open.
DESTINATION_PANELS = {
    "img2img": "tab_img2img", "inpaint": "tab_img2img", "extras": "tab_extras",
    "stitch_txt2img": "tab_txt2img", "stitch_img2img": "tab_img2img",
}


def visible_panel(page):
    return page.evaluate(VISIBLE_PANEL_JS)


def hold_delivery(page):
    """Take over the transfer, so this test decides when a picture lands.

    The alternative is stalling the network and hoping the timing holds,
    which is how a check ends up proving that a stub was slow. What the
    contract actually says is about ORDER - deliver, prove, then show - and
    order is what a held promise can assert exactly.
    """
    page.evaluate("""() => {
        const canvas = window.minipaintCanvas;
        window.__sendCalls = 0;
        window.__realDeliver = window.__realDeliver || canvas.deliverToHost;
        window.__release = null;
        canvas.deliverToHost = function () {
            window.__sendCalls += 1;
            return new Promise(function (resolve) { window.__release = resolve; });
        };
    }""")


def answer_delivery(page, answer):
    page.evaluate("a => { if (window.__release) { const f = window.__release; window.__release = null; f(a); } }", answer)


def stub_delivery(page, answer):
    """Every delivery answers with this, at once."""
    page.evaluate("""a => {
        const canvas = window.minipaintCanvas;
        window.__sendCalls = 0;
        window.__realDeliver = window.__realDeliver || canvas.deliverToHost;
        canvas.deliverToHost = function () { window.__sendCalls += 1; return Promise.resolve(a); };
    }""", answer)


def restore_delivery(page):
    page.evaluate("""() => {
        if (window.__realDeliver) { window.minipaintCanvas.deliverToHost = window.__realDeliver; window.__realDeliver = null; }
        window.__release = null;
    }""")


def check_a_destination_is_never_pre_opened(r: Results, page, targets) -> None:
    """From a fresh page: open only Clipboard, send, and arrive at the target.

    FORGE'S OWN RESULT BUTTONS ESTABLISH THE RULE. connect_paste_params_buttons
    binds the source image to the destination component AND binds the same
    press to switch_to_<tabname>, so a user on txt2img sends to img2img or
    Extras without first visiting either, and the destination becomes the
    visible tab. Being visible is an OUTCOME of the send, not a precondition
    for it - and a send that only works after the user has visited the target
    is a bug.
    """
    page.reload(wait_until="load")
    page.wait_for_selector("#tabs .tab-nav button", timeout=30000)
    time.sleep(2.5)
    open_clipboard(page)
    time.sleep(1.5)
    r.check("no destination has been opened on this page: Clipboard is the only tab visited",
            visible_panel(page) == "tab_minipaint_clipboard", visible_panel(page))

    for key, label in (("img2img", "img2img"), ("inpaint", "Inpaint"), ("extras", "Extras"),
                       ("stitch_txt2img", "ImageStitch (txt2img)"), ("stitch_img2img", "ImageStitch (img2img)")):
        if key not in targets:
            continue
        open_clipboard(page)
        if not select_first(page):
            r.check(f"no pre-open: a picture is selected for {key}", False, "no selection")
            continue
        r.check(f"no pre-open: the menu offers {key} although its tab has never been opened",
                send_selected(page, label) == "clicked")
        time.sleep(4.0)
        landed = page.evaluate("""id => {
            const host = id ? document.getElementById(id) : null;
            if (host && host.querySelectorAll('img').length) { return true; }
            return Array.from(document.querySelectorAll('textarea,input'))
                .filter(t => String(t.value || '').indexOf('data:image') === 0)
                .some(t => { const h = t.closest('[id]'); return h && h.id.indexOf('minipaint_clipboard') !== 0; });
        }""", getattr(targets.get(key), "elem_id", "") or "")
        r.check(f"no pre-open: the picture lands in {key} through its already-built component", landed)
        r.check(f"a verified send to {key} SHOWS that destination, with the picture already there",
                visible_panel(page) == DESTINATION_PANELS[key],
                f"{visible_panel(page)} (wanted {DESTINATION_PANELS[key]})")
    open_clipboard(page)


def check_navigation_is_never_what_makes_a_send_work(r: Results, page, targets) -> None:
    """Held open: the user stays in Clipboard until the picture is proved there."""
    open_clipboard(page)
    if not select_first(page):
        r.check("held delivery: a picture is selected first", False, "no selection")
        return
    hold_delivery(page)
    try:
        send_selected(page, "img2img")
        time.sleep(2.5)
        r.check("with the transfer held open, the page has not moved: Clipboard is still the visible tab",
                visible_panel(page) == "tab_minipaint_clipboard", visible_panel(page))
        r.check("and the delivery was attempted exactly once",
                page.evaluate("() => window.__sendCalls") == 1, str(page.evaluate("() => window.__sendCalls")))
        answer_delivery(page, {"ok": True, "switched": True, "switchReason": ""})
        time.sleep(1.5)
    finally:
        restore_delivery(page)


def settle_sends(page) -> None:
    """Let any watcher armed by an earlier send expire before the next one.

    A send that went unanswered tries once more from the page twelve seconds
    later, which is the right behaviour and the wrong thing to have running
    underneath a check that counts deliveries.
    """
    time.sleep(14)


def check_a_failed_send_leaves_you_in_clipboard(r: Results, page, targets) -> None:
    """A destination that cannot take the picture does not get the user sent to it."""
    settle_sends(page)
    open_clipboard(page)
    if not select_first(page):
        r.check("failed send: a picture is selected first", False, "no selection")
        return
    # Nothing must deliver: the page's own transfer is broken here, and the
    # framework's path is cut, so a switch could only be navigation used as
    # evidence that delivery worked.
    stub_delivery(page, {"ok": False, "reason": "the destination is broken", "switched": False, "switchReason": ""})
    page.route("**/queue/**", lambda route: route.abort())
    page.route("**/gradio_api/**", lambda route: route.abort())
    try:
        record_toasts(page, reset=True)
        r.check("failed send: the send is attempted", send_selected(page, "img2img") == "clicked")
        said = []
        for _ in range(18):
            time.sleep(1)
            said = recorded_toasts(page)
            if said:
                break
        r.check("a send that failed leaves the user in Clipboard, not in an empty destination",
                visible_panel(page) == "tab_minipaint_clipboard", visible_panel(page))
        r.check("and names the failure rather than going quiet",
                any("did not reach" in one for one in said), str(said))
    finally:
        page.unroute("**/queue/**")
        page.unroute("**/gradio_api/**")
        restore_delivery(page)
        time.sleep(1.5)


def check_delivery_and_navigation_are_separate_facts(r: Results, page, targets) -> None:
    """Proved to have landed, and the tab would not open: say so, never resend.

    The picture is where it was sent. Sending it again to make the view
    follow would put a second one in a gallery, which is the one thing a
    destination that appends cannot be given twice.
    """
    settle_sends(page)
    open_clipboard(page)
    if not select_first(page):
        r.check("switch failure: a picture is selected first", False, "no selection")
        return
    stub_delivery(page, {"ok": True, "reason": "", "switched": False,
                         "switchReason": "there is no img2img tab on this page to open"})
    try:
        record_toasts(page, reset=True)
        r.check("switch failure: the send is attempted", send_selected(page, "img2img") == "clicked")
        said = []
        for _ in range(18):
            time.sleep(1)
            said = recorded_toasts(page)
            if said:
                break
        r.check("a delivery whose tab would not open says exactly that",
                any("but could not open that tab" in one for one in said), str(said))
        r.check("and the user is left where they were rather than on a tab that did not open",
                visible_panel(page) == "tab_minipaint_clipboard", visible_panel(page))
        time.sleep(14)
        r.check("AND THE PICTURE IS NEVER SENT AGAIN: one delivery, one picture",
                page.evaluate("() => window.__sendCalls") == 1,
                str(page.evaluate("() => window.__sendCalls")))
    finally:
        restore_delivery(page)
        time.sleep(1.5)


def check_mini_paint_keeps_the_same_contract(r: Results, page) -> None:
    """Mini Paint's transport is still the framework's. Its UX is not different.

    Section 13 names Mini Paint's DELIVERY as the one thing that stays on
    Gradio until the Canvas is migrated. It does not grant Mini Paint a
    different Send-to experience: it still does not have to be opened first,
    a receive that landed shows it, and a receive that did not stays here.
    """
    open_clipboard(page)
    offered = page.evaluate("""() => { window.minipaintClipboard.toggleMenu();
        const items = () => Array.from(document.querySelectorAll('.minipaint-clip-menu .minipaint-clip-menu-item'));
        const send = items().filter(b => b.textContent.indexOf('Send selected') === 0)[0];
        if (send) { send.click(); }
        const names = items().map(b => b.textContent.trim());
        window.minipaintClipboard.closeMenu();
        return names; }""")
    if not any("Mini Paint" in one for one in offered):
        r.check("Mini Paint is not a destination on this page, so its contract is not checked here",
                True, str(offered))
        return

    if not select_first(page):
        r.check("Mini Paint: a picture is selected first", False, "no selection")
        return
    r.check("Mini Paint has never been opened on this page", visible_panel(page) == "tab_minipaint_clipboard")
    r.check("Mini Paint: the send is offered anyway", send_selected(page, "Mini Paint") == "clicked")
    landed = False
    for _ in range(20):
        time.sleep(1)
        if visible_panel(page) == "tab_minipaint":
            landed = True
            break
    r.check("a receive that landed shows Mini Paint, without it having been visited first", landed,
            visible_panel(page))

    open_clipboard(page)
    if not select_first(page):
        return
    page.route("**/queue/**", lambda route: route.abort())
    page.route("**/gradio_api/**", lambda route: route.abort())
    try:
        record_toasts(page, reset=True)
        send_selected(page, "Mini Paint")
        said = []
        for _ in range(18):
            time.sleep(1)
            said = recorded_toasts(page)
            if said:
                break
        r.check("with the framework's channel cut, that receive fails visibly", any(said), str(said))
        r.check("and does not navigate: a failed receive leaves the user in Clipboard",
                visible_panel(page) == "tab_minipaint_clipboard", visible_panel(page))
    finally:
        page.unroute("**/queue/**")
        page.unroute("**/gradio_api/**")
        time.sleep(1.5)


def check_the_whole_tab_works_with_the_channel_cut(r: Results, page, targets) -> None:
    """Browse, page, sort, select, send, come back, and read the queue - with
    the framework's channel delivering nothing at all."""
    open_clipboard(page)
    page.route("**/queue/**", lambda route: route.abort())
    page.route("**/gradio_api/**", lambda route: route.abort())
    try:
        page.evaluate("() => window.minipaintClipboard.library({refresh: true, sort: 'name_asc', page: 0})")
        time.sleep(2.0)
        state = library_state(page)
        r.check("cut: the library is read and drawn", state["shown"] > 0 and state["total"] > 0, str(state))
        page.evaluate("() => window.minipaintClipboard.setSort('name_desc')")
        time.sleep(2.0)
        r.check("cut: the sort takes, and is remembered", library_state(page)["sort"] == "name_desc",
                str(library_state(page)["sort"]))
        r.check("cut: a picture can still be selected", select_first(page))
        r.check("cut: and sent to a destination that has never been opened",
                send_selected(page, "img2img") == "clicked")
        # The page places the picture itself and verifies it, which is a
        # round trip and a re-encode rather than an instant; what is being
        # checked is that it happens at all with the channel dead, not how
        # quickly.
        arrived = False
        for _ in range(25):
            time.sleep(1)
            if visible_panel(page) == "tab_img2img":
                arrived = True
                break
        r.check("cut: which is where the user now is", arrived, visible_panel(page))
        open_clipboard(page)
        queued = page.evaluate("""() => fetch('/minipaint-clipboard/queue?page=' + window.minipaintClipboard.pageId(),
            {credentials: 'same-origin', cache: 'no-store'}).then(res => res.json()).then(a => !!(a && a.ok === true))""")
        r.check("cut: and the queue list is readable - which is what 'I closed the browser and came back' means",
                queued is True, str(queued))
        r.check("cut: the notice, if it is up at all, is the one about the framework's channel and not about Forge",
                page.evaluate("() => window.minipaintClipboard.debug().offline.server") is False)
    finally:
        page.unroute("**/queue/**")
        page.unroute("**/gradio_api/**")
        page.evaluate("() => window.minipaintClipboard.library({refresh: true, sort: 'name_asc', page: 0})")
        time.sleep(1.5)

def check_tab_switching_survives_reordering(r: Results, page) -> None:
    """Tabs get reordered and hidden; a send must still land on the right one.

    The switch used to fall back to counting - the panel's index among the
    panels, then the button at that index - which only works while every
    panel has a button and both lists are in the same order. Reorder the
    tabs, or hide one, and it lands a tab over: a picture sent to Extras
    opened PNG Info.
    """
    def visible_panel():
        return page.evaluate(VISIBLE_PANEL_JS)

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
                check_paging(r, page, library)
                check_patching(r, page, library)
                check_sorting_is_separate_from_drawing(r, page)
                check_the_failure_modes_have_answers(r, page, library)
                check_a_thumbnail_is_fetched_once(r, page)
                r.check("a picture can be selected", select_first(page),
                        repr(box(page, "minipaint_clipboard_selected")))
                check_send(r, page, targets)
                # Tab switching first: the checks below cut Gradio's queue
                # with page routing, and a reload while a route handler is
                # in flight is a good way to hang a browser for no reason
                # that has anything to do with what is being tested.
                check_tab_switching_survives_reordering(r, page)
                check_recovers_after_the_interruption(r, page, targets)
                check_send_survives_a_dead_queue(r, page, targets)
                check_a_component_destination_fills_without_the_queue(r, page, targets)
                check_a_render_cannot_take_the_selection(r, page)
                check_a_send_reaches_a_framework_owned_input(r, page, targets)
                check_a_send_survives_a_box_the_host_never_hears(r, page, targets)
                check_the_page_can_say_why_a_send_was_silent(r, page)
                check_the_notice_takes_itself_down(r, page)
                check_a_picture_handed_in_arrives_without_the_queue(r, page, library)
                # The Send-to contract, last because the first of these
                # reloads the page to prove nothing was opened beforehand.
                check_a_destination_is_never_pre_opened(r, page, targets)
                check_navigation_is_never_what_makes_a_send_work(r, page, targets)
                check_a_failed_send_leaves_you_in_clipboard(r, page, targets)
                check_delivery_and_navigation_are_separate_facts(r, page, targets)
                check_mini_paint_keeps_the_same_contract(r, page)
                check_the_whole_tab_works_with_the_channel_cut(r, page, targets)
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
