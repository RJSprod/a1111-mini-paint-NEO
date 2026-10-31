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

*   a tile is a button, and the page it is drawn into carries a theme that
    says `button { min-width: fit-content !important }`. The stylesheet's
    `min-width: 0` lost, every tile grew to its own caption's width, and the
    grid was reported a fourth time with every check passing - because each
    check measured the picture inside the tile and none measured the tile
    against its cell. The script states the geometry inline now, and this
    suite puts that theme's rule on the page to prove it holds.

The Canvas is stubbed here for the same reason as in ``browser_loading.py``:
none of this depends on the editor, so none of it should need a Forge Neo
checkout to run.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile
import json
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
    # pictures is not tidy. And one name the length Forge actually gives a
    # picture - a timestamp, a seed and the start of the prompt - because a
    # tile only grows past its cell under a theme's `min-width: fit-content`
    # when its caption is wider than the cell, and "wide.png" never is.
    for name, size in (("tall.png", (300, 900)), ("wide.png", (900, 300)), ("square.png", (500, 500)),
                       ("portrait.png", (400, 600)), ("tiny.png", (64, 64)),
                       ("2026-01-25-19h04m08s_seed431100828_Make this a reference.png", (800, 450))):
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
        "<script>" + FORGE_GALLERY_HELPERS + "</script>"
        "<script>" + (ROOT / "javascript" / "main.js").read_text(encoding="utf-8") + "</script>"
        "<style>" + (ROOT / "style.css").read_text(encoding="utf-8") + "</style>"
    )


# Forge Neo's own gallery helpers (javascript/ui.js), the same in shape: the
# helper answers a Gradio js function, so what it returns is the INPUTS
# array - a one-item gallery inside a one-element array - and not the item.
# A page without this exercised a helper no real Forge has, which is how a
# pick that wrapped the answer once more, into a list Gradio 4.40 refuses
# before the receive function runs, passed every suite.
FORGE_GALLERY_HELPERS = """
function selected_gallery_index() {
    const app = typeof gradioApp === "function" ? gradioApp() : document;
    return Array.from(app.querySelectorAll('.thumbnail-item.thumbnail-small')).findIndex(el => el.classList.contains('selected'));
}
function extract_image_from_gallery(gallery) {
    if (gallery.length === 0) { return [null]; }
    let index = selected_gallery_index();
    if (index < 0 || index >= gallery.length) { index = 0; }
    return [[gallery[index]]];
}
"""


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
    const nm = el.querySelector('.minipaint-clip-name');
    const nr = nm ? nm.getBoundingClientRect() : null;
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
            x: Math.round(r.left), y: Math.round(r.top),
            // What the page's stylesheets made of the tile's own box: the
            // declaration that won, for the report when one is not its cell.
            minWidth: cs.minWidth, boxWidth: cs.width, display: cs.display,
            thumbW: tr ? Math.round(tr.width) : 0, thumbH: tr ? Math.round(tr.height) : 0,
            // The caption, which has to stay inside the tile on every side:
            // a caption wider than the tile is what made the tile wider.
            nameSpillX: nr ? Math.round(Math.max(nr.right - r.right, r.left - nr.left)) : 0,
            nameSpillY: nr ? Math.round(nr.bottom - r.bottom) : 0,
            // How wide the caption would be if nothing clipped it.
            nameFull: nm ? nm.scrollWidth : 0,
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
            // The picture's own shape, and how it is fitted: what "keeps its
            // aspect ratio and is never cut off" is measured against.
            natW: im ? im.naturalWidth : 0, natH: im ? im.naturalHeight : 0,
            paintWf: paint ? paint.w : 0, paintHf: paint ? paint.h : 0,
            fit: im ? getComputedStyle(im).objectFit : '',
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
    """The toolbar's Send selection to button, which is the only door now."""
    page.evaluate("() => window.minipaintClipboard.openToolbarMenu('send')")
    time.sleep(0.4)
    return menu_click(page, label)


def set_slider(page, value):
    """Move the size slider to ``value`` pictures per row, as a hand would."""
    page.evaluate("""v => {
        const host = document.getElementById('minipaint_clipboard_thumb');
        const input = host.querySelector('input[type=range]');
        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        setter.call(input, String(v));
        input.dispatchEvent(new Event('input', {bubbles: true}));
        input.dispatchEvent(new Event('change', {bubbles: true}));
    }""", value)
    time.sleep(1.0)


#: The row as the page drew it: the count, the tile size it made, the counts
#: the width allows, and the grid's own measures.
ROW_JS = """() => {
    const api = window.minipaintClipboard;
    const row = api && api.rowState ? api.rowState() : {};
    const grid = document.querySelector('.minipaint-clip-grid');
    const cs = grid ? getComputedStyle(grid) : null;
    const input = document.querySelector('#minipaint_clipboard_thumb input[type=range]');
    return Object.assign({}, row, {
        room: grid ? grid.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight) : 0,
        gap: cs ? parseFloat(cs.columnGap) : 0,
        cols: cs ? cs.getPropertyValue('--minipaint-clip-cols').trim() : '',
        thumbVar: cs ? parseFloat(cs.getPropertyValue('--minipaint-clip-thumb')) : 0,
        sliderValue: input ? Number(input.value) : 0,
        sliderMin: input ? Number(input.min) : 0,
        sliderMax: input ? Number(input.max) : 0,
        // The grid's own scroll bar, if one is drawn: what it takes from the
        // row. Playwright starts headless Chromium with --hide-scrollbars, so
        // that is 0 here whatever the page says, and what the page says is
        // read as well.
        bar: grid ? grid.offsetWidth - grid.clientWidth - parseFloat(cs.borderLeftWidth) - parseFloat(cs.borderRightWidth) : 0,
        scrollbarWidth: cs ? cs.scrollbarWidth : '',
    });
}"""


def set_tile_size(page, size):
    """The count whose tiles come nearest ``size`` px, on the slider - for
    checks that want a large or a small tile rather than a number per row."""
    row = page.evaluate(ROW_JS)
    rng = row.get("range") or {"min": 1, "max": 1}
    count = round((row["room"] + row["gap"]) / (size + 12 + row["gap"])) if row["room"] > 0 else 1
    set_slider(page, max(rng["min"], min(rng["max"], count)))


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
    check_a_hostile_theme_cannot_resize_a_tile(r, page)

    # The slider. It counts pictures per row and the page writes the size
    # that makes as CSS variables; the grid has to be the element that hears
    # them, which is the whole of the bug this covers.
    before = page.evaluate(TILES_JS)[0]["w"]
    start = page.evaluate(ROW_JS)
    rng = start.get("range") or {"min": 1, "max": 1}
    for count, direction in ((max(rng["min"], start["perRow"] - 2), "larger"),
                             (min(rng["max"], start["perRow"] + 3), "smaller")):
        set_slider(page, count)
        after = page.evaluate(TILES_JS)
        r.check(f"the size slider makes the tiles {direction}",
                after and (after[0]["w"] > before if direction == "larger" else after[0]["w"] < before),
                f"{before}px -> {after[0]['w'] if after else '?'}px at {count} per row (from {start['perRow']})")
        r.check(f"and they are still uniform at {count} per row",
                len({t['w'] for t in after}) == 1 and len({t['h'] for t in after}) == 1,
                str(sorted({(t['w'], t['h']) for t in after})))
        row = page.evaluate(ROW_JS)
        r.check(f"the grid itself carries the count the slider holds (asked {count})",
                row["cols"] == str(row["sliderValue"]) == str(count) and abs(row["thumbVar"] - row["thumb"]) < 0.05,
                f"grid {row['cols']!r}/{row['thumbVar']} vs slider {row['sliderValue']!r}/{row['thumb']}")
        size = count
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
    set_tile_size(page, 220)
    wanted = page.evaluate(ROW_JS)["perRow"]
    page.evaluate("() => window.minipaintClipboard.pressHidden('minipaint_clipboard_refresh')")
    time.sleep(2.5)
    kept = page.evaluate(TILES_JS)
    r.check("and a refresh does not snap the tiles back to the default",
            kept and kept[0]["w"] > 170 and page.evaluate(ROW_JS)["perRow"] == wanted,
            f"{kept[0]['w'] if kept else '?'}px after refresh")

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

    The script now states the box's and the picture's geometry inline, with
    !important, and nothing on the page can stop the box shaping while it
    does - so those pins are taken off first, and put back after. With them
    on, this check would be asking nothing; what it asks is what the tile
    and the stylesheet do on their own, which is the layer under the pins.
    """
    set_tile_size(page, 144)
    time.sleep(0.6)
    kept = page.evaluate("""() => Array.from(document.querySelectorAll('.minipaint-clip-thumb, .minipaint-clip-thumb img'))
        .map(el => { const s = el.getAttribute('style') || ''; el.removeAttribute('style'); return s; })""")
    r.check("(the script had pinned the box and the picture inline, so there was something to take off)",
            kept and all("!important" in s for s in kept), str(kept[:1]))
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
        page.evaluate("""kept => Array.from(document.querySelectorAll('.minipaint-clip-thumb, .minipaint-clip-thumb img'))
            .forEach((el, i) => { if (kept[i]) { el.setAttribute('style', kept[i]); } })""", kept)
        time.sleep(0.4)


# What the page this grid lives on says about a button. The first rule is
# the Lobe theme's, verbatim (src/styles/components/button.ts): it is what
# made every tile as wide as its own caption on the user's machine, each
# running out of its cell under the next one, with three fixes to the
# picture already in place - none of which could reach the tile's own box.
# The rest are the other ways a theme can size a button, a span, a picture
# or the grid itself, said at a specificity above the stylesheet's own, so
# that only the script's inline declarations stand between them and the
# tile.
LOBE_BUTTON_CSS = "button { cursor: pointer; min-width: fit-content !important; }"
HOSTILE_THEME_CSS = LOBE_BUTTON_CSS + """
#tabs #minipaint_clipboard_root button {
    display: inline-block !important; width: fit-content !important; max-width: none !important;
    height: auto !important; min-height: 44px !important; max-height: none !important;
    padding: 12px 24px !important; margin: 6px !important;
    font-size: 18px !important; line-height: 2 !important; white-space: nowrap !important;
}
#tabs #minipaint_clipboard_root span {
    display: inline !important; width: auto !important; min-width: max-content !important;
    font-size: 18px !important; line-height: 2 !important;
}
#tabs #minipaint_clipboard_root img {
    width: auto !important; height: auto !important; max-width: none !important; max-height: none !important;
}
#tabs #minipaint_clipboard_root .minipaint-clip-grid {
    display: flex !important; flex-wrap: wrap !important; gap: 24px !important; padding: 0 !important;
}
#tabs #minipaint_clipboard_root .minipaint-clip-mount { display: block !important; }
"""


def check_a_hostile_theme_cannot_resize_a_tile(r: Results, page) -> None:
    """A theme's rules about buttons cannot change the size of a tile.

    The fourth report of this grid, with a screenshot: pictures centred in
    boxes two columns wide, a selection border the width of two tiles,
    captions clipped at the wrong end. The grid's tracks were right - the
    columns were the slider's size - and the tiles were not: the Lobe theme
    says `button { min-width: fit-content !important }` about every button
    on the page, a tile is a button, and that beat the tile's `min-width:
    0`. Each tile was as wide as its one-line caption, ran out of its cell
    under the next tile, and the last in each row showed the whole of it.

    Every earlier fix was a stylesheet rule about the picture, and a
    stylesheet rule cannot beat a theme's !important on the tile. What can
    is an inline declaration marked !important, which is what the script
    now writes for every geometric property of the grid, the tile, the box,
    the picture and the caption. This is the check that those hold: the
    theme's own rule, and every other way a stylesheet can size the same
    elements, are put on the page, and every tile has to stay exactly its
    cell - not merely uniform, and not merely with its picture centred.
    """
    expected = page.evaluate("""() => {
        const grid = document.querySelector('.minipaint-clip-grid');
        const thumb = parseFloat(getComputedStyle(grid).getPropertyValue('--minipaint-clip-thumb'));
        return {w: thumb + 12, h: thumb + 32, gap: parseFloat(getComputedStyle(grid).columnGap)};
    }""")
    r.check("the grid knows the size the slider holds", expected["w"] > 12 and expected["h"] > 32, str(expected))
    before = page.evaluate(TILES_JS)
    r.check("(one caption is wider than its cell, which is what the theme's rule grows a tile to)",
            any(t["nameFull"] > t["w"] for t in before), str([(t["name"][:12], t["nameFull"], t["w"]) for t in before]))
    # The theme's one rule first, alone and verbatim - the report - and then
    # the whole of what a stylesheet could say about these elements.
    for what, css in (("the Lobe theme's `button { min-width: fit-content !important }`", LOBE_BUTTON_CSS),
                      ("every other way a stylesheet can size a button, a span, a picture or the grid", HOSTILE_THEME_CSS)):
        style = page.add_style_tag(content=css)
        try:
            time.sleep(0.5)
            tiles = page.evaluate(TILES_JS)
            sizes = sorted({(t["w"], t["h"]) for t in tiles})
            # A cell is the row shared out, so its width can be a fraction:
            # a tile's rounded size is within a pixel of it.
            r.check(f"under {what}, every tile is still exactly its cell",
                    tiles and all(abs(w - expected["w"]) <= 1 and abs(h - expected["h"]) <= 1 for w, h in sizes),
                    f"{sizes} for {round(expected['w'])}x{round(expected['h'])}; "
                    f"min-width {sorted({t['minWidth'] for t in tiles})}, display {sorted({t['display'] for t in tiles})}")
            # Neighbours on a row are one column apart: a tile that had run
            # out of its cell would be under the next one, and the two would
            # overlap rather than sit a gap apart.
            rows = {}
            for t in tiles:
                rows.setdefault(t["y"], []).append(t["x"])
            steps = sorted({b - a for xs in rows.values() for a, b in zip(sorted(xs), sorted(xs)[1:])})
            r.check("and each tile sits in its own column, a gap from the next",
                    steps and all(abs(step - (expected["w"] + expected["gap"])) <= 1 for step in steps),
                    f"column steps {steps} for {expected}")
            r.check("and every caption stays inside its tile",
                    not [t["name"] for t in tiles if t["nameSpillX"] > 1 or t["nameSpillY"] > 1],
                    str([(t["name"][:12], t["nameSpillX"], t["nameSpillY"]) for t in tiles if t["nameSpillX"] > 1 or t["nameSpillY"] > 1]))
            spilling = [(t["name"][:12], t["spillX"], t["spillY"]) for t in tiles if t["spillY"] > 1 or t["spillX"] > 1]
            r.check("and every picture is still inside its tile", not spilling, str(spilling))
            r.check("and still drawn in the middle of it", not adrift(tiles), str(adrift(tiles)))
            boxes = sorted({(t["thumbW"], t["thumbH"]) for t in tiles})
            r.check("and the box the picture is drawn in is still one square",
                    len(boxes) == 1 and abs(boxes[0][0] - boxes[0][1]) <= 2, str(boxes))
        finally:
            style.evaluate("el => el.remove()")
            time.sleep(0.4)
    check_a_tile_that_is_not_its_cell_is_reported(r, page)


def check_a_tile_that_is_not_its_cell_is_reported(r: Results, page) -> None:
    """If a tile is ever not its cell again, the journal says what won.

    Four reports, and each fix was made against the page the suite builds
    rather than the one the user has - because nothing on the user's page
    said which declaration had beaten the stylesheet. So the tile check the
    page runs after every render measures the tile against its cell too,
    and its line carries the tile's computed display, width and min-width:
    the difference between fixing the next one and guessing again.

    Proven the only way it can be: the theme's rule on the page and the
    script's pins taken off the tiles, which is the old page exactly. The
    line has to name the fault and the winning value, and clear again once
    the tiles are their cells.
    """
    style = page.add_style_tag(content=LOBE_BUTTON_CSS)
    kept = page.evaluate("""() => Array.from(document.querySelectorAll('.minipaint-clip-item'))
        .map(el => { const s = el.getAttribute('style') || ''; el.removeAttribute('style'); return s; })""")
    try:
        time.sleep(0.3)
        report = page.evaluate("() => { window.minipaintClipboard.reportTiles(); return window.minipaintClipboard.debug().tileReport; }")
        r.check("a tile that is not the size of its cell is reported, with the size it is, the size it should be and the min-width that won",
                "tile(s) not the size of their cell" in report and " for " in report and "min-width fit-content" in report, repr(report))
    finally:
        page.evaluate("""kept => Array.from(document.querySelectorAll('.minipaint-clip-item'))
            .forEach((el, i) => { if (kept[i]) { el.setAttribute('style', kept[i]); } })""", kept)
        style.evaluate("el => el.remove()")
        time.sleep(0.4)
    cleared = page.evaluate("() => { window.minipaintClipboard.reportTiles(); return window.minipaintClipboard.debug().tileReport; }")
    r.check("and the report clears once every tile is its cell again", cleared == "", repr(cleared))


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
    """Read the listing of the folder on screen, as the page itself does."""
    page.evaluate("o => window.minipaintClipboard.library(o)", options)
    time.sleep(1.2)


def _grid(page):
    """What the grid's loader is doing: the folder on screen, its rows, the
    thumbnails asked for and in what order, and the selection's mark."""
    return page.evaluate("() => window.minipaintClipboard.gridState()")


def _pictures_in(folder) -> int:
    return len([one for one in folder.iterdir() if one.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")])


def _seed_storage(library, count, prefix, salt):
    """``count`` small pictures in the storage folder, named so that by name
    they come first and in the order of their numbers - and every one
    different, from each other and from any other check's (``salt``): the
    library knows a file by its bytes as well as its name, so a file with
    the bytes of one deleted a moment ago is that picture renamed, its id
    and its thumbnail already known."""
    from PIL import Image

    made = []
    for number in range(count):
        path = library / f"{prefix}{number:03d}.png"
        Image.new("RGB", (40, 30), (number % 256, number // 256, salt)).save(path)
        made.append(path)
    return made


def _list_storage(page, total):
    """The storage folder read again, by name, and waited for whole. Whether
    it came whole is the caller's to check: a listing that never does is a
    failed check, not a run that stops here."""
    page.evaluate("() => window.minipaintClipboard.library({sort: 'name_asc', refresh: true})")
    try:
        page.wait_for_function(f"() => window.minipaintClipboard.libraryState().total === {total}"
                               " && !window.minipaintClipboard.libraryState().busy", timeout=30000)
    except Exception:
        pass
    page.wait_for_timeout(300)


GRID_JS = """() => {
    const grid = document.querySelector('#minipaint_clipboard_root .minipaint-clip-grid');
    const tiles = Array.from(grid.querySelectorAll('.minipaint-clip-item'));
    return { tiles: tiles.length, names: tiles.map(t => t.dataset.name),
             withSrc: tiles.filter(t => t.querySelector('img').getAttribute('src')).length,
             scroll: grid.scrollHeight, view: grid.clientHeight, top: grid.scrollTop,
             max: grid.scrollHeight - grid.clientHeight };
}"""

STORAGE_COUNT = 240
#: The loader's folder: at the smallest tiles a screen is some seventy
#: pictures and the rows asked for past it as many again, so it takes this
#: many for a fling to pass over hundreds that must never be asked for.
LOADER_COUNT = 600


def check_the_storage_folder_is_one_listing(r: Results, page, library) -> None:
    """Asked for: "the clipboard storage thumbnail gallery still works by
    pages, but i prefer the way archive loads, which is not pages, but only
    rendering thumbnails in view and cache. Let's make the storage option
    work like that."

    So the storage folder is one listing, as an archive is: every picture in
    one answer, no pager, only the rows near the view in the document. And
    what the pages did for a person is still done, the virtual way: the keys
    move a screen where they moved a page, a selection far away is kept and
    the scrubber marks where it is - the pager's "selection on page N" - and
    a press on the mark brings it back, and a rename changes its tile's
    caption in place wherever it is in the folder.
    """
    made = _seed_storage(library, STORAGE_COUNT, "0-pic-", 77)
    renamed = []
    listings = []
    heard = lambda request: listings.append(request.url) if "/minipaint-clipboard/library" in request.url else None
    try:
        open_clipboard(page)
        total = _pictures_in(library)
        page.on("request", heard)
        _list_storage(page, total)
        page.remove_listener("request", heard)
        state = page.evaluate("() => window.minipaintClipboard.libraryState()")
        r.check("the whole storage folder is one answer: every picture listed, read with one request",
                state["total"] == total and len(listings) == 1, str({"state": state, "asked": listings}))
        r.check("and nothing asks for a page of it: the request names a sort and no page or size",
                listings and "page=" not in listings[0] and "size=" not in listings[0] and "sort=name_asc" in listings[0],
                str(listings[:1]))
        r.check("there is no pager: no row of Back, the page and Next under the grid",
                page.evaluate("() => !document.querySelector('.minipaint-clip-pager')"))
        first = page.evaluate(GRID_JS)
        r.check("only the rows near the view are in the document, not every tile",
                0 < first["tiles"] < total / 2 and first["scroll"] > first["view"] * 3, str({k: first[k] for k in ("tiles", "scroll", "view")}))
        r.check("and they are in the document in reading order, so a Tab from one goes to the next",
                first["names"] == sorted(first["names"], key=lambda name: (name.lower(), name)), str(first["names"][:4]))

        # -- the keys move a screen, where they used to move a page
        page.evaluate("() => { const g = document.querySelector('#minipaint_clipboard_root .minipaint-clip-grid'); g.focus(); }")
        page.keyboard.press("PageDown")
        page.wait_for_timeout(500)
        down = page.evaluate(GRID_JS)
        r.check("PageDown on the grid moves it a screen, as a listbox's does, and draws the rows under it",
                down["top"] >= first["view"] * 0.5 and down["names"] != first["names"], str({"top": down["top"], "view": first["view"]}))
        page.keyboard.press("End")
        page.wait_for_timeout(600)
        end = page.evaluate(GRID_JS)
        last = sorted((one.name for one in library.iterdir() if one.suffix.lower() == ".png"), key=lambda n: (n.lower(), n))[-1]
        r.check("End reaches the last picture in the folder, drawn there and nowhere else",
                end["top"] >= end["max"] - 2 and last in end["names"] and first["names"][0] not in end["names"], str(end["names"][-3:]))
        page.keyboard.press("Home")
        page.wait_for_timeout(600)
        back = page.evaluate(GRID_JS)
        r.check("and Home comes back to exactly the pictures that were there", back["top"] == 0 and back["names"] == first["names"],
                str(back["names"][:3]))

        # -- a selection far away is kept: it is the page's, not the view's
        chosen = _select_tile(page, first["names"][0])
        r.check("a picture at the top of the folder is selected", bool(chosen), repr(chosen))
        page.evaluate("() => { const g = document.querySelector('#minipaint_clipboard_root .minipaint-clip-grid'); g.scrollTop = g.scrollHeight; }")
        page.wait_for_timeout(600)
        page.evaluate("() => window.minipaintClipboard.library({refresh: true})")
        page.wait_for_function("() => !window.minipaintClipboard.libraryState().busy", timeout=20000)
        page.wait_for_timeout(400)
        away = page.evaluate("""a => ({ drawn: !!Array.from(document.querySelectorAll('.minipaint-clip-item')).find(t => t.dataset.asset === a),
                                        selected: window.minipaintClipboard.debug().selected })""", chosen)
        r.check("scrolled to the far end and read again, the selected picture is drawn nowhere and is still the selection",
                not away["drawn"] and away["selected"] == chosen and box(page, "minipaint_clipboard_selected") == chosen, str(away))
        r.check("so the Send menu is still live, rather than greyed out with a picture plainly chosen",
                page.evaluate("""() => { window.minipaintClipboard.openToolbarMenu('send');
                    const items = () => Array.from(document.querySelectorAll('.minipaint-clip-menu .minipaint-clip-menu-item'));
                    const on = items().filter(b => !b.disabled && b.textContent.indexOf('\u2039') !== 0
                                                   && b.textContent !== 'Cancel').length;
                    window.minipaintClipboard.closeMenu();
                    return on; }""") >= 1)
        page.wait_for_timeout(300)
        scrub = page.evaluate(SCRUB_JS)
        r.check("and the scrubber marks where it is in the folder - the pager's 'selection on page N', the virtual way",
                _grid(page)["mark"] is True and scrub["mark"] and scrub["mark"]["top"] - scrub["bar"]["top"] < scrub["bar"]["height"] / 4,
                str({"mark": scrub["mark"], "bar": scrub["bar"]}))
        if scrub["mark"]:
            page.mouse.click(scrub["mark"]["left"] + scrub["mark"]["width"] / 2, scrub["mark"]["top"] + scrub["mark"]["height"] / 2)
            page.wait_for_timeout(600)
        r.check("a press on the mark brings the selected picture back into view, and the mark goes",
                _in_view(page, chosen) and _grid(page)["mark"] is False and page.evaluate(
                    "() => window.minipaintClipboard.debug().selected") == chosen, str(_grid(page)["rows"]))

        # -- a rename changes its tile in place, wherever it is
        page.evaluate("""() => { const g = document.querySelector('#minipaint_clipboard_root .minipaint-clip-grid');
            g.scrollTop = Math.round(g.scrollHeight / 2); }""")
        page.wait_for_timeout(800)
        # A picture in view half way down; "-renamed" after its stem keeps
        # it in its place by name, so the rename moves nothing but a caption.
        found = page.evaluate("""() => {
            const grid = document.querySelector('#minipaint_clipboard_root .minipaint-clip-grid');
            const g = grid.getBoundingClientRect();
            const tile = Array.from(grid.querySelectorAll('.minipaint-clip-item')).find(t => {
                const b = t.getBoundingClientRect();
                return t.dataset.name.indexOf('0-pic-') === 0 && b.top >= g.top && b.bottom <= g.bottom; });
            return tile ? [tile.dataset.asset, tile.dataset.name] : ['', '']; }""")
        target, called = found
        stem = called[:-len(".png")] if called.endswith(".png") else called
        renamed.append(library / (stem + "-renamed.png"))
        r.check("(a picture to rename is in view, half way down the folder)",
                bool(target) and 60 < int(stem[-3:] or 0) < STORAGE_COUNT - 60, repr(called))
        if target:
            _select_tile(page, called)
            stamped = page.evaluate(STAMP_TILES_JS)
            # The ones in view: the rename's panel opens under the grid and
            # shortens its view while it is open, which may take the far
            # edge of the rows drawn past it out of the document - a scroll,
            # not the listing - so the rows in view are the ones asked about.
            seen = set(page.evaluate("""() => {
                const grid = document.querySelector('#minipaint_clipboard_root .minipaint-clip-grid');
                const g = grid.getBoundingClientRect();
                return Array.from(grid.querySelectorAll('.minipaint-clip-item')).filter(t => {
                    const b = t.getBoundingClientRect(); return b.top >= g.top && b.bottom <= g.bottom; })
                    .map(t => t.dataset.asset); }"""))
            scrolled = page.evaluate(GRID_JS)["top"]
            asked = []
            thumbs = lambda request: asked.append(request.url) if "/minipaint-clipboard/image/" in request.url else None
            page.on("request", thumbs)
            try:
                page.evaluate("() => window.minipaintClipboard.pressHidden('minipaint_clipboard_rename_open')")
                page.wait_for_selector("#minipaint_clipboard_rename_text textarea, #minipaint_clipboard_rename_text input",
                                       state="visible", timeout=10000)
                page.locator("#minipaint_clipboard_rename_text textarea, #minipaint_clipboard_rename_text input").first.fill(stem + "-renamed")
                page.click("#minipaint_clipboard_rename_ok")
                page.wait_for_function("""a => { const t = Array.from(document.querySelectorAll('.minipaint-clip-item'))
                    .find(x => x.dataset.asset === a[0]); return !!t && t.dataset.name === a[1]; }""",
                                       arg=[target, stem + "-renamed.png"], timeout=15000)
                page.wait_for_timeout(500)
            finally:
                page.remove_listener("request", thumbs)
            after = page.evaluate(READ_TILES_JS)
            mine = next((tile for tile in after if tile["asset"] == target), {})
            was = next((tile for tile in stamped if tile["asset"] == target), {})
            r.check("a rename changes its tile's caption in place: the same element, half way down the folder",
                    mine.get("name") == stem + "-renamed.png" and mine.get("stamp") and mine.get("stamp") == was.get("stamp")
                    and renamed[0].is_file(), str({"was": was, "now": mine}))
            kept = {tile["asset"]: tile["stamp"] for tile in stamped if tile["asset"] in seen}
            still = {tile["asset"]: tile["stamp"] for tile in after}
            r.check("and every other tile in view is the same element it was: the listing moved nothing",
                    len(kept) > 6 and all(still.get(asset) == stamp for asset, stamp in kept.items()),
                    str({"in view": len(kept), "rebuilt": [asset[:8] for asset, stamp in kept.items() if still.get(asset) != stamp][:4]}))
            r.check("and nothing is asked for again: the file's version is the same, so is its thumbnail",
                    not asked, str(asked[:2]))
            r.check("and the grid did not move, and the picture is still the selection",
                    abs(page.evaluate(GRID_JS)["top"] - scrolled) <= 1
                    and page.evaluate("() => window.minipaintClipboard.debug().selected") == target,
                    str({"before": scrolled, "after": page.evaluate(GRID_JS)["top"]}))

            # -- a file changed where it stands is a new picture: its tile
            #    stays, and only its thumbnail is asked for again
            from PIL import Image
            from urllib.parse import quote

            old_version = page.evaluate("""a => (Array.from(document.querySelectorAll('.minipaint-clip-item'))
                .find(x => x.dataset.asset === a) || {dataset: {}}).dataset.v || ''""", target)
            Image.new("RGB", (40, 30), (250, 10, 10)).save(renamed[0])
            asked.clear()
            page.on("request", thumbs)
            try:
                page.evaluate("() => window.minipaintClipboard.library({refresh: true})")
                try:
                    page.wait_for_function("""a => { const t = Array.from(document.querySelectorAll('.minipaint-clip-item'))
                        .find(x => x.dataset.asset === a[0]); const i = t && t.querySelector('img');
                        return !!i && t.dataset.v !== a[1] && (i.getAttribute('src') || '').indexOf('blob:') === 0
                            && i.complete && i.naturalWidth > 0; }""", arg=[target, old_version], timeout=15000)
                except Exception:
                    pass
                page.wait_for_timeout(300)
            finally:
                page.remove_listener("request", thumbs)
            changed = page.evaluate(READ_TILES_JS)
            again = next((tile for tile in changed if tile["asset"] == target), {})
            r.check("a file changed where it stands keeps its tile - the same element - and only its new thumbnail is asked for",
                    again.get("stamp") == was.get("stamp") and len(asked) == 1
                    and quote(target, safe="") in asked[0] and "v=" in asked[0] and quote(old_version, safe="") not in asked[0],
                    str({"asked": asked[:3], "same": again.get("stamp") == was.get("stamp")}))

        # -- a new order starts at the top: a place in one means nothing in another
        from minipaint_neo.clipboard import config as clip_config

        stored = clip_config.load().sort
        page.evaluate("""() => { const g = document.querySelector('#minipaint_clipboard_root .minipaint-clip-grid');
            g.scrollTop = Math.round(g.scrollHeight / 2); }""")
        page.wait_for_timeout(500)
        page.evaluate("() => window.minipaintClipboard.setSort(window.minipaintClipboard.libraryState().sort === 'name_desc' ? 'name_asc' : 'name_desc')")
        page.wait_for_function("() => !window.minipaintClipboard.libraryState().busy", timeout=15000)
        page.wait_for_timeout(500)
        sorted_view = page.evaluate(GRID_JS)
        ordered = page.evaluate("() => window.minipaintClipboard.libraryState().sort")
        r.check("a new sort starts the grid at its top, the new first picture first - what page 0 of a new sort was",
                sorted_view["top"] == 0 and sorted_view["names"][:1] == ([last] if ordered == "name_desc" else [first["names"][0]]),
                str({"top": sorted_view["top"], "first": sorted_view["names"][:1], "sort": ordered}))
        page.evaluate("s => window.minipaintClipboard.setSort(s)", stored)
        page.wait_for_timeout(800)
    finally:
        try:
            page.remove_listener("request", heard)
        except Exception:
            pass
        for one in made + renamed:
            try:
                one.unlink()
            except OSError:
                pass
        page.evaluate("() => window.minipaintClipboard.select('')")
        page.evaluate("() => { const g = document.querySelector('#minipaint_clipboard_root .minipaint-clip-grid'); g.scrollTop = 0; }")
        show_page(page, {"sort": "name_asc", "refresh": True})


def check_the_storage_folder_loads_what_is_in_view_first(r: Results, page, library) -> None:
    """The storage folder's thumbnails come the archive's way: in view first,
    in reading order, a few at a time, dropped the moment the view moves on,
    and never asked for the rows a fling passes over.

    The server is made slow on purpose, as for the archive - the first six
    thumbnails take 1.5 s each, the rest a moment - so there are requests in
    flight for the top of the folder when the grid is flung to the bottom,
    and still in flight 300 ms later; what the page has done by then is the
    measurement.
    """
    from minipaint_neo.clipboard import store as clip_store

    made = []
    library_store = clip_store.store()
    real = library_store.thumbnail
    counted = {"made": 0}

    def slow(*args, **keywords):
        counted["made"] += 1
        time.sleep(1.5 if counted["made"] <= 6 else 0.05)
        return real(*args, **keywords)

    requests = []
    listener = lambda request: requests.append(request.url) if ("/minipaint-clipboard/image/" in request.url
                                                                and "thumb=1" in request.url) else None
    try:
        open_clipboard(page)
        set_tile_size(page, 72)
        # Put in the folder only now: opening the tab reads the folder, and
        # must not have read these before the server was made slow.
        made = _seed_storage(library, LOADER_COUNT, "0-load-", 154)
        total = _pictures_in(library)
        before = _grid(page)
        library_store.thumbnail = slow
        page.on("request", listener)
        page.evaluate("() => window.minipaintClipboard.library({sort: 'name_asc', refresh: true})")
        try:
            page.wait_for_function(f"() => window.minipaintClipboard.libraryState().total === {total}", timeout=30000)
        except Exception:
            pass
        page.wait_for_timeout(250)
        state = _grid(page)
        r.check("storage: the folder is listed whole before anything is measured", state["total"] == total,
                str({"listed": state["total"], "folder": total}))
        visible_top = list(state["visible"])
        geometry = page.evaluate(GRID_JS)
        asked_top = state["order"][-(state["started"] - before["started"]):] if state["started"] > before["started"] else []
        r.check("storage: only the rows near the view are in the document",
                0 < geometry["tiles"] < total / 2 and geometry["scroll"] > geometry["view"] * 3, str(geometry)[:160])
        r.check("storage: a tile is a place until its turn - none outside the view has a picture address",
                geometry["withSrc"] <= len(visible_top), str({"withSrc": geometry["withSrc"], "visible": len(visible_top)}))
        r.check("storage: the first thumbnails asked for are the ones in view, in reading order, three at a time",
                asked_top[:3] == visible_top[:3] and len(state["inflight"]) <= 3,
                str({"asked": asked_top[:4], "visible": visible_top[:4], "inflight": len(state["inflight"])}))

        # Flung to the bottom while the top is still loading.
        in_flight = list(state["inflight"])
        page.evaluate("() => { const g = document.querySelector('#minipaint_clipboard_root .minipaint-clip-grid'); g.scrollTop = g.scrollHeight; }")
        page.wait_for_timeout(300)
        after = _grid(page)
        visible_bottom = list(after["visible"])
        r.check("storage: the fling leaves the top of the folder out of view", not (set(visible_bottom) & set(visible_top)),
                str(after["rows"]))
        r.check("storage: the requests still out for the top were dropped the moment the view moved - not at the next answer",
                after["dropped"] > state["dropped"] and not (set(after["inflight"]) & set(in_flight)),
                str({"dropped": after["dropped"] - state["dropped"], "in_flight_before": len(in_flight), "made": counted["made"]}))
        since = after["order"][-(after["started"] - state["started"]):] if after["started"] > state["started"] else []
        r.check("storage: and the next thumbnails asked for are ones now in view - what you are seeing goes first",
                bool(since) and all(key in set(visible_bottom) for key in since[:3]),
                str({"first": since[:3], "visible": visible_bottom[:3]}))
        # Judged once the loader has nothing left it wants: a loader that
        # wanted the whole folder would still be asking, and is caught by
        # what it asked for on the way rather than by when it was looked at.
        quiet = 0
        for _ in range(60):
            page.wait_for_timeout(250)
            quiet = quiet + 1 if not _grid(page)["inflight"] else 0
            if quiet >= 4:
                break
        later = _grid(page)
        listing = page.evaluate("""() => fetch('/minipaint-clipboard/library?sort=name_asc', {credentials: 'same-origin'})
            .then(r => r.json()).then(a => a.items.map(i => i[0]))""")
        place = {key: at for at, key in enumerate(listing)}
        columns = later["columns"] or 1
        reach = 2 * (len(visible_top) // columns + 1) * columns
        asked = later["order"][-(later["started"] - before["started"]):] if later["started"] > before["started"] else []
        positions = [place[key] for key in asked if key in place]
        passed = [at for at in positions if reach <= at < len(listing) - reach]
        r.check("storage: and the pictures the fling passed over were never asked for at all",
                positions and len(positions) == len(asked) and not passed,
                str({"passed": sorted(passed)[:5], "reach": reach, "asked": len(asked)}))
        try:
            page.wait_for_function("""() => {
                const s = window.minipaintClipboard.gridState();
                const shown = Array.from(document.querySelectorAll('#minipaint_clipboard_root .minipaint-clip-item'))
                    .filter(t => s.visible.indexOf(t.dataset.key) !== -1);
                return shown.length > 0 && shown.every(t => (t.querySelector('img').getAttribute('src') || '').indexOf('blob:') === 0);
            }""", timeout=30000)
            filled = True
        except Exception:
            filled = False
        r.check("storage: every tile in view gets its thumbnail", filled, str(_grid(page))[:300])
        r.check("storage: over plain requests, a small fraction of the folder", 0 < len(requests) < total / 3, str(len(requests)))
        r.check("storage: each carrying the file's version, so the browser may keep it",
                all("v=" in url and "thumb=1" in url for url in requests), str(requests[:2]))
    finally:
        page.remove_listener("request", listener)
        try:
            del library_store.thumbnail
        except AttributeError:
            pass
        for one in made:
            try:
                one.unlink()
            except OSError:
                pass
        page.evaluate("() => { const g = document.querySelector('#minipaint_clipboard_root .minipaint-clip-grid'); g.scrollTop = 0; }")
        show_page(page, {"sort": "name_asc", "refresh": True})
        set_tile_size(page, 144)


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
    show_page(page, {"sort": "name_asc", "refresh": True})
    before = page.evaluate(STAMP_TILES_JS)
    r.check("patching: the grid is drawn and stamped", len(before) == 6, str(len(before)))

    page.evaluate("() => { const g = document.querySelector('.minipaint-clip-grid'); g.scrollTop = 24; }")
    scrolled = page.evaluate("() => document.querySelector('.minipaint-clip-grid').scrollTop")

    # A PICTURE FROM SOMEWHERE ELSE. It goes into the library through the
    # same import route a paste or a drop takes, but not from this page - so
    # nothing tells this grid. Nothing is pushed to the page any more (no
    # connection is held open to push it over); the grid shows it when it is
    # next read, and Refresh is how somebody asks for that.
    landed = page.evaluate("""async () => {
        const id = document.querySelector('.minipaint-clip-item').dataset.asset;
        const blob = await (await fetch('/minipaint-clipboard/image/' + id,
                                        { credentials: 'same-origin' })).blob();
        const answer = await fetch('/minipaint-clipboard/import?source=paste', {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': blob.type || 'image/png', 'X-MiniPaint-Filename': '0000-new.png' },
            body: blob
        });
        return (await answer.json()).ok === true;
    }""")
    r.check("patching: a picture is imported from outside the grid", landed is True, str(landed))
    time.sleep(3.0)
    r.check("and the grid does not change by itself: nothing is pushed to it",
            len(page.evaluate(READ_TILES_JS)) == 6, str(len(page.evaluate(READ_TILES_JS))))
    r.check("no event stream was opened by the tab", page.evaluate("""() => performance.getEntriesByType('resource')
        .filter(e => e.name.indexOf('/minipaint-interop/events') !== -1).length""") == 0)
    pressed = page.evaluate("""() => {
        const host = document.getElementById('minipaint_clipboard_refresh_now');
        const b = host && (host.tagName === 'BUTTON' ? host : host.querySelector('button'));
        if (!b) { return false; }
        b.click();
        return true;
    }""")
    r.check("the toolbar has a Refresh button", pressed is True)
    for _ in range(20):
        time.sleep(0.5)
        if len(page.evaluate(READ_TILES_JS)) == 7:
            break

    after = page.evaluate(READ_TILES_JS)
    kept = [t for t in after if t["stamp"]]
    fresh = [t for t in after if not t["stamp"]]
    r.check("Refresh brings it in: one picture in means one node in",
            len(after) == 7 and len(fresh) == 1, f"{len(after)} tiles, {len(fresh)} new")
    r.check("AND THE OTHER SIX ARE THE SAME ELEMENTS, not new ones that look the same",
            sorted(t["stamp"] for t in kept) == sorted(t["stamp"] for t in before), str(len(kept)))
    # Named to sort first by name, ahead of the timestamp-named picture in
    # the seeded library, so "in its place" is the first place.
    r.check("the new one is in its place in the order, not appended",
            after[0]["name"] == "0000-new.png", str([t["name"] for t in after]))
    r.check("and the scroll position survives the patch",
            page.evaluate("() => document.querySelector('.minipaint-clip-grid').scrollTop") == scrolled,
            str(page.evaluate("() => document.querySelector('.minipaint-clip-grid').scrollTop")))

    # And out again from this page: the toolbar's Delete is a server action,
    # and the action itself asks the grid to read again - it is not handed
    # new markup, and it is not told by a stream.
    page.evaluate("""() => {
        const doomed = Array.from(document.querySelectorAll('.minipaint-clip-item'))
            .filter(t => t.dataset.name === '0000-new.png')[0];
        if (doomed) { doomed.click(); }
    }""")
    time.sleep(0.8)
    page.evaluate("() => document.getElementById('minipaint_clipboard_delete_now').click()")
    for _ in range(24):
        time.sleep(0.5)
        if len(page.evaluate(READ_TILES_JS)) == 6:
            break
    left = page.evaluate(READ_TILES_JS)
    r.check("a picture out means one node out, and the rest are still the same elements",
            len(left) == 6 and len([t for t in left if t["stamp"]]) == 6,
            f"{len(left)} tiles, {len([t for t in left if t['stamp']])} kept")
    r.check("and the ones that survived are the ones that were there before",
            sorted(t["name"] for t in left) == sorted(t["name"] for t in before),
            str(sorted(t["name"] for t in left)))


def check_the_empty_and_unconfigured_notices(r: Results, page) -> None:
    """The grid's two empty states, said where the tiles would be.

    They are states of the index, not of the transport: the route answers
    them with no pictures and a reason, and the page says the sentence that
    reason has always had, in place of the tiles - and puts the tiles back
    and the sentence away the moment the folder lists pictures again. A
    selection is not given up for an empty answer either: only a listing
    that holds pictures, none of them it, says a picture has gone. Answered
    here in the route's own shape, so the check needs neither an empty
    folder nor no folder at all.
    """
    open_clipboard(page)
    show_page(page, {"sort": "name_asc", "refresh": True})
    chosen = _select_tile(page, page.evaluate("() => (document.querySelector('.minipaint-clip-item') || {dataset: {}}).dataset.name || ''"))
    r.check("(a picture is selected before the folder answers empty)", bool(chosen), repr(chosen))

    def answering(reason, configured):
        def handler(route):
            route.fulfill(json={"ok": True, "revision": "0:0", "configured": configured, "sort": "name_asc", "total": 0,
                                "left_out": 0, "reason": reason, "status": "", "items": []})
        return handler

    drawn_js = """() => { const grid = document.querySelector('#minipaint_clipboard_root .minipaint-clip-grid');
        const note = grid.querySelector('.minipaint-clip-nothing');
        const box = note ? note.getBoundingClientRect() : null;
        return { tiles: grid.querySelectorAll('.minipaint-clip-item').length, text: note ? note.textContent : '',
                 shown: !!box && box.height > 0 && box.width > 0, empty: grid.classList.contains('minipaint-clip-empty'),
                 selected: window.minipaintClipboard.debug().selected }; }"""
    for reason, configured, words in (("unconfigured", False, "No storage folder yet"), ("empty", True, "No images yet")):
        page.route("**/minipaint-clipboard/library*", answering(reason, configured))
        try:
            page.evaluate("() => window.minipaintClipboard.library({})")
            try:
                page.wait_for_function("() => window.minipaintClipboard.libraryState().total === 0"
                                       " && !window.minipaintClipboard.libraryState().busy", timeout=10000)
            except Exception:
                pass
            page.wait_for_timeout(300)
            state = page.evaluate(drawn_js)
            r.check(f"a folder answered {reason} says so where the tiles were - '{words}' - and draws no tile",
                    state["tiles"] == 0 and words in state["text"] and state["shown"] and state["empty"], str(state))
            r.check(f"and the selection is not given up for an answer with no pictures in it ({reason})",
                    state["selected"] == chosen, str(state["selected"]))
        finally:
            page.unroute("**/minipaint-clipboard/library*")
    page.evaluate("() => window.minipaintClipboard.library({refresh: true})")
    try:
        page.wait_for_function("() => window.minipaintClipboard.libraryState().total > 0"
                               " && !window.minipaintClipboard.libraryState().busy", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(300)
    state = page.evaluate(drawn_js)
    r.check("and a folder with pictures in it puts the tiles back and the sentence away, the selection still there",
            state["tiles"] > 0 and not state["text"] and not state["empty"] and state["selected"] == chosen, str(state))
    page.evaluate("() => window.minipaintClipboard.select('')")


def check_the_failure_modes_have_answers(r: Results, page, library) -> None:
    """Every failure in section 15 has a defined thing the page says.

    The whole cost of the last six builds was a fault with no symptom, so
    "it went quiet" is not an outcome any of these is allowed to have.
    """
    from PIL import Image

    open_clipboard(page)
    show_page(page, {"sort": "name_asc", "refresh": True})

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
    #
    # A real 404 from the real route, rather than a stubbed one: an id the
    # library has never minted is exactly what a listing names when the file
    # behind it has gone since it was read. The listing is the one thing
    # added to; the thumbnail is asked for by the loader, of the server.
    gone = "0" * 32
    before = page.evaluate(STAMP_TILES_JS)

    def with_a_gone_picture(route):
        response = route.fetch()
        answer = response.json()
        answer["items"].insert(1, [gone, "gone.png", 1234, "1-1234", 10, 10])
        answer["total"] = len(answer["items"])
        route.fulfill(response=response, json=answer)

    page.route("**/minipaint-clipboard/library*", with_a_gone_picture)
    try:
        page.evaluate("() => window.minipaintClipboard.library({})")
        try:
            page.wait_for_function("""id => { const t = Array.from(document.querySelectorAll('.minipaint-clip-item'))
                .find(x => x.dataset.asset === id); return !!t && t.classList.contains('minipaint-clip-item-missing'); }""",
                                   arg=gone, timeout=15000)
        except Exception:
            pass
        r.check("a thumbnail that 404s marks its tile and leaves its name readable",
                page.evaluate("""id => {
                    const tile = Array.from(document.querySelectorAll('.minipaint-clip-item')).find(x => x.dataset.asset === id);
                    return !!tile && tile.classList.contains('minipaint-clip-item-missing') && tile.dataset.name === 'gone.png'
                        && getComputedStyle(tile.querySelector('img')).display === 'none';
                }""", gone), str(_grid(page)["failed"]))
        others = [tile for tile in page.evaluate(READ_TILES_JS) if tile["asset"] != gone]
        r.check("and the page is otherwise unaffected: every other tile is the one it was, and none is marked",
                len(others) == len(before) and all(tile["stamp"] for tile in others)
                and page.evaluate("""id => Array.from(document.querySelectorAll('.minipaint-clip-item-missing'))
                    .every(t => t.dataset.asset === id)""", gone),
                f"{len(before)} -> {len(others)}")
    finally:
        page.unroute("**/minipaint-clipboard/library*")
    page.evaluate("() => window.minipaintClipboard.library({refresh: true})")
    time.sleep(2.0)

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
    # -- and while it stays down, it is asked a handful of times and not a storm
    #
    # Every failure says the server is still silent, and the notice that
    # re-renders on the way asks for a retry of its own. Guarded only by "is
    # a timer pending" - which the attempt has just cleared - each round
    # armed a second timer beside the one it was about to arm AND reset the
    # backoff, so a server that stayed down was asked exponentially more
    # often the longer it was gone. Counted, because a page that hammers a
    # server it says is down is worse than the fault it is reporting.
    asked = []
    page.route("**/minipaint-clipboard/library*", lambda route: (asked.append(1), route.abort()))
    try:
        page.evaluate("() => window.minipaintClipboard.library({})")
        time.sleep(2.5)
        r.check("the line is up again with the server still refusing",
                page.evaluate("() => window.minipaintClipboard.debug().offline.server") is True)
        asked.clear()
        first = page.evaluate("() => window.minipaintClipboard.debug().retryDelay")
        time.sleep(32)
        later = page.evaluate("() => window.minipaintClipboard.debug().retryDelay")
        r.check("a server that stays down is asked a handful of times over half a minute, not scores of them",
                len(asked) <= 4, f"{len(asked)} request(s) in 32s")
        # Only once the cycle has actually gone round twice is there a second
        # wait to be longer than the first; a page the browser backgrounded
        # is allowed to have made no attempt at all, which is the design.
        if len(asked) >= 2:
            r.check("because each wait is longer than the last, rather than being reset by the failure",
                    later > first, f"{first}ms -> {later}ms after {len(asked)} attempts")
    finally:
        page.unroute("**/minipaint-clipboard/library*")

    page.evaluate("() => window.minipaintClipboard.library({refresh: true})")
    time.sleep(2.5)
    r.check("and the line goes by itself the moment the server answers",
            page.evaluate("() => window.minipaintClipboard.debug().offline.server") is False)
    r.check("leaving no retry cycle behind on a page that is fine",
            page.evaluate("() => window.minipaintClipboard.debug().retrying") is False)


def check_the_queue_section_is_the_browsers(r: Results, page) -> None:
    """The one screen that answers "I closed the browser and came back".

    The jobs already survived that - the outbox is work Forge owns - but the
    VIEW of them did not: the list was server-rendered markup delivered over
    the framework's channel, so a page that came back needed that channel to
    show a job that had run perfectly well without it.
    """
    open_clipboard(page)
    time.sleep(1.5)
    r.check("the queue list is drawn by the browser, into the block the server used to fill",
            page.evaluate("""() => {
                const host = document.getElementById('minipaint_clipboard_outbox_list');
                return !!(host && host.querySelector('.minipaint-clip-outbox'));
            }"""))
    # A failure does not age out of this list any more - it waits for a
    # person, which is what Dismiss is. So clear whatever the checks above
    # left before asserting what an empty queue says, and use the button to
    # do it: that is the press being checked.
    def queue_text():
        return page.evaluate("""() => { const h = document.getElementById('minipaint_clipboard_outbox_list');
            return h ? h.textContent : ''; }""")

    dismissed = 0
    for _ in range(12):
        pressed = page.evaluate("""() => {
            const buttons = Array.from(document.querySelectorAll(
                '#minipaint_clipboard_outbox_list .minipaint-clip-job-actions button'));
            const one = buttons.filter(b => b.textContent.trim() === 'Dismiss')[0];
            if (!one) { return false; }
            one.click();
            return true; }""")
        if not pressed:
            break
        dismissed += 1
        time.sleep(1.2)
    r.check("a failure the checks above left offers Dismiss, and pressing it takes the card off the queue",
            dismissed == 0 or "Refused" not in queue_text(), queue_text()[:80])
    r.check("and says so when nothing has been sent from here yet",
            "No request has been sent" in queue_text(), queue_text()[:80] or "NO HOST")

    # Add to Queue is a press to this tab's own route now. Whether WanGP is
    # there to take it is not what is being checked: that the press reaches
    # the server and comes back with a sentence and a button is.
    before = page.evaluate("""() => { const h = document.getElementById('minipaint_clipboard_queue_status');
        return h ? h.textContent.trim() : ''; }""")
    presses = []

    def pressed(request):
        if request.method == "POST" and "/minipaint-clipboard/queue" in request.url:
            try:
                presses.append(json.loads(request.post_data or "{}"))
            except ValueError:
                presses.append({})

    page.on("request", pressed)
    try:
        page.evaluate("""() => { const h = document.getElementById('minipaint_clipboard_queue');
            const b = h && (h.tagName === 'BUTTON' ? h : h.querySelector('button'));
            if (b) { b.click(); } }""")
        said = ""
        for _ in range(12):
            time.sleep(1)
            said = page.evaluate("""() => { const h = document.getElementById('minipaint_clipboard_queue_status');
                return h ? h.textContent.trim() : ''; }""")
            if said and said != before:
                break
    finally:
        page.remove_listener("request", pressed)
    r.check("Add to Queue reaches the server over that route and comes back with a sentence",
            bool(said) and said != before, repr(said))
    adds = [body for body in presses if body.get("action") == "add"]
    # No WanGP page is in this one, so nothing can be saved: the press says
    # so rather than leaving the server to guess, and it was not held up.
    r.check("and it tells the server what came of saving WanGP's form first - here, that there was nothing to ask",
            len(adds) == 1 and adds[0].get("settings_flush") in ("", "unavailable"), str(adds)[:200])

    r.check("the history list is the browser's too",
            page.evaluate("""() => {
                const items = () => Array.from(document.querySelectorAll('.minipaint-clip-menu .minipaint-clip-menu-item'));
                window.minipaintClipboard.toggleMenu();
                const entry = items().filter(b => b.textContent.indexOf('Queue Send History') >= 0)[0];
                if (entry) { entry.click(); }
                return true; }"""))
    time.sleep(2.5)
    r.check("and is drawn, with its own words, into the panel the menu opens",
            page.evaluate("""() => {
                const host = document.getElementById('minipaint_clipboard_history_list');
                return !!(host && host.querySelector('.minipaint-clip-history'));
            }"""),
            page.evaluate("""() => { const h = document.getElementById('minipaint_clipboard_history_list');
                return h ? h.innerHTML.slice(0, 120) : 'NO HOST'; }"""))


def check_queue_reads_are_one_at_a_time(r: Results, page) -> None:
    """A queue read that has not come back is not asked again beside itself.

    The day this mattered, a server that had stopped answering left
    twenty-four reads of this route in flight at once, each holding one of
    the six connections a browser allows to an origin, and everything the
    page asked for afterwards - a generation, a button, this tab's own
    journal - queued in the browser behind them. One read at a time is the
    rule; this holds a read on the wire and asks again to see that the
    rule holds.
    """
    held = []

    def hold(route):
        if route.request.method == "GET":
            held.append(route)
        else:
            route.continue_()

    page.route("**/minipaint-clipboard/queue*", hold)
    # Waited for with the page's own clock rather than time.sleep: a route
    # handler is only ever run while this thread is inside a Playwright call,
    # so a sleep here would be a request the browser has paused and nobody
    # has looked at yet.
    try:
        page.evaluate("() => window.minipaintClipboard.refreshQueue()")
        for _ in range(50):
            if held:
                break
            page.wait_for_timeout(100)
        r.check("(a queue read is on the wire and held there)", len(held) == 1, str(len(held)))
        # Three more asks while it is out, each past the refresh debounce so
        # that each one reaches the read itself rather than the timer.
        for _ in range(3):
            page.evaluate("() => window.minipaintClipboard.refreshQueue()")
            page.wait_for_timeout(500)
        r.check("asking again while a read is out opens no second connection", len(held) == 1, str(len(held)))
        held[0].fulfill(status=200, content_type="application/json",
                        body=json.dumps({"ok": True, "jobs": [], "running": False, "counts": {}}))
        page.wait_for_timeout(500)
        page.evaluate("() => window.minipaintClipboard.refreshQueue()")
        for _ in range(50):
            if len(held) >= 2:
                break
            page.wait_for_timeout(100)
        r.check("and once the answer is in, the next ask is a new read", len(held) == 2, str(len(held)))
    finally:
        # Answered before the route goes: a handler still holding a request
        # when it is unrouted is a cancelled future Playwright complains about.
        for route in held:
            try:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": True, "jobs": [], "running": False, "counts": {}}))
            except Exception:
                pass  # already answered
        page.wait_for_timeout(300)
        page.unroute("**/minipaint-clipboard/queue*")
        page.wait_for_timeout(300)


def check_the_prompt_editor_fills_the_window(r: Results, page) -> None:
    """A system prompt is a page of prose; the composer is a third of a window.

    So the panel gets a second shape rather than a second home. What has to
    be true: the press fills the window, the box being edited takes most of
    it, every control that was in the panel is still in it, and there is one
    of each - a second editor beside the first is two sets of controls that
    drift.
    """
    open_clipboard(page)
    time.sleep(1.0)
    r.check("the editor is not on the tab at all until it is asked for",
            page.evaluate("() => window.minipaintClipboard.debug().editorOpen") is False)
    # THE TAB KEEPS ONE BUTTON AND NOTHING ELSE. Not a collapsed section that
    # still takes a third of the composer to say its own name: the panel has
    # no shape here, and none of its controls is measurable on the page.
    hidden = page.evaluate("""() => {
        const panel = document.getElementById('minipaint_clipboard_enhance_panel');
        const names = ['enhance_line', 'enhance_toggle', 'sp_variant', 'sp_mode',
                       'system_prompt', 'sp_state', 'sp_apply', 'sp_restore', 'sp_reload', 'sp_close'];
        return {
            panel: panel ? Math.round(panel.getBoundingClientRect().height) : -1,
            showing: names.filter(n => {
                const el = document.getElementById('minipaint_clipboard_' + n);
                return !!el && el.getBoundingClientRect().height > 0;
            })
        };
    }""")
    r.check("the enhancement panel takes no room on the tab", hidden["panel"] == 0, str(hidden["panel"]))
    r.check("and not one of its controls is on the tab either", not hidden["showing"], str(hidden["showing"]))
    r.check("the button that opens it is, though",
            page.evaluate("""() => {
                const b = document.getElementById('minipaint_clipboard_sp_open');
                return !!b && b.getBoundingClientRect().height > 0;
            }"""))

    pressed = page.evaluate("""() => {
        const host = document.getElementById('minipaint_clipboard_sp_open');
        const b = host && (host.tagName === 'BUTTON' ? host : host.querySelector('button'));
        if (!b) { return false; }
        b.click();
        return true;
    }""")
    r.check("the editor has a way in", pressed is True)
    time.sleep(2.0)

    shape = page.evaluate("""() => {
        const panel = document.getElementById('minipaint_clipboard_enhance_panel');
        const box = panel && panel.querySelector('.minipaint-clip-system-prompt textarea');
        const p = panel ? panel.getBoundingClientRect() : null;
        const t = box ? box.getBoundingClientRect() : null;
        return {
            open: !!(panel && panel.classList.contains('minipaint-clip-fullscreen')),
            panelW: p ? Math.round(p.width) : 0, panelH: p ? Math.round(p.height) : 0,
            boxH: t ? Math.round(t.height) : 0, boxW: t ? Math.round(t.width) : 0,
            windowW: window.innerWidth, windowH: window.innerHeight,
            text: box ? String(box.value || '').length : 0
        };
    }""")
    r.check("the press fills the window", shape["open"] is True
            and shape["panelW"] >= shape["windowW"] * 0.9 and shape["panelH"] >= shape["windowH"] * 0.9,
            f"{shape['panelW']}x{shape['panelH']} of {shape['windowW']}x{shape['windowH']}")
    r.check("with MOSTLY the box being edited, which is what the view is for",
            shape["boxH"] >= shape["windowH"] * 0.45 and shape["boxW"] >= shape["panelW"] * 0.8,
            f"box {shape['boxW']}x{shape['boxH']} in {shape['panelW']}x{shape['panelH']}")
    # What is IN the box is the server's answer, and this page has no
    # ModelSwitchRefiner to read a default from - the unit suite checks the
    # loading against a fake one. What has to be true here is that the box is
    # the thing the view puts you in front of.
    r.check("the cursor is already in the box, because typing in it is what the view is for",
            page.evaluate("""() => {
                const box = document.querySelector('#minipaint_clipboard_system_prompt textarea');
                return !!box && document.activeElement === box;
            }"""))
    page.keyboard.type("edited in the full window")
    time.sleep(0.4)
    typed = page.evaluate("""() => {
        const box = document.querySelector('#minipaint_clipboard_system_prompt textarea');
        return {value: box ? String(box.value || '') : '', focus: document.activeElement ? (document.activeElement.id
                || document.activeElement.className || document.activeElement.tagName) : ''};
    }""")
    r.check("and it takes what is typed into it", typed["value"] == "edited in the full window", str(typed))

    controls = page.evaluate("""() => {
        const panel = document.getElementById('minipaint_clipboard_enhance_panel');
        const on = id => {
            const host = document.getElementById(id);
            if (!host || !panel.contains(host)) { return false; }
            return getComputedStyle(host).display !== 'none' && host.getBoundingClientRect().height > 0;
        };
        return {
            toggle: on('minipaint_clipboard_enhance_toggle'),
            variant: on('minipaint_clipboard_sp_variant'),
            mode: on('minipaint_clipboard_sp_mode'),
            prompt: on('minipaint_clipboard_system_prompt'),
            apply: on('minipaint_clipboard_sp_apply'),
            restore: on('minipaint_clipboard_sp_restore'),
            reload: on('minipaint_clipboard_sp_reload'),
            close: on('minipaint_clipboard_sp_close')
        };
    }""")
    missing = [name for name, there in controls.items() if not there]
    r.check("every control the panel had is in the view, and Close with them", not missing, str(missing))
    hint = page.evaluate("""() => {
        const panel = document.getElementById('minipaint_clipboard_enhance_panel');
        return panel ? panel.textContent : '';
    }""")
    r.check("the paragraph about what the switch does is not in the view either",
            "Off by default" not in hint and "is refused, not enhanced" not in hint, hint[:140])

    page.evaluate("""() => document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))""")
    time.sleep(1.0)
    r.check("Escape puts it away", page.evaluate("() => window.minipaintClipboard.debug().editorOpen") is False)
    r.check("leaving the panel back in the column, with what was typed still in it",
            page.evaluate("""() => {
                const panel = document.getElementById('minipaint_clipboard_enhance_panel');
                const box = document.querySelector('#minipaint_clipboard_system_prompt textarea');
                return !!panel && getComputedStyle(panel).position !== 'fixed'
                    && !!box && String(box.value || '').indexOf('edited in the full window') === 0;
            }"""))

    page.evaluate("""() => {
        const host = document.getElementById('minipaint_clipboard_sp_open');
        const b = host && (host.tagName === 'BUTTON' ? host : host.querySelector('button'));
        if (b) { b.click(); }
    }""")
    time.sleep(1.5)
    r.check("and opening it again re-reads the instructions rather than keeping an unapplied edit",
            page.evaluate("""() => {
                const box = document.querySelector('#minipaint_clipboard_system_prompt textarea');
                return box ? String(box.value || '').indexOf('edited in the full window') : -1;
            }""") != 0)
    page.evaluate("""() => {
        const host = document.getElementById('minipaint_clipboard_sp_close');
        const b = host && (host.tagName === 'BUTTON' ? host : host.querySelector('button'));
        if (b) { b.click(); }
    }""")
    time.sleep(1.5)
    r.check("and Close puts it away too", page.evaluate("() => window.minipaintClipboard.debug().editorOpen") is False)


def check_sorting_is_separate_from_drawing(r: Results, page) -> None:
    """Changing the sort does not touch a tile until the new order arrives.

    And a sort that cannot be fetched leaves the grid exactly as it was AND
    SAYS SO - where a failed round trip used to leave it stale with nothing
    to indicate the sort did not take.
    """
    open_clipboard(page)
    show_page(page, {"sort": "name_asc"})
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


def check_the_toolbar_flyouts_are_the_one_door(r: Results, page) -> None:
    """Sort and Send selection to are buttons on the bar, not a dropdown and
    a submenu.

    What has to be true: each button opens its own list under itself, the
    list has no Back row because there is nothing behind it, pressing the
    same button again closes it, and the Send entry is gone from the menu -
    one verb, one door.
    """
    open_clipboard(page)
    time.sleep(0.6)
    r.check("there is no Sort dropdown on the toolbar any more",
            page.evaluate("() => !document.querySelector('#minipaint_clipboard_sort')"))
    for name in ("sort_open", "send_open"):
        r.check(f"{name} is a button on the toolbar, and it is drawn",
                page.evaluate("""n => { const b = document.getElementById('minipaint_clipboard_' + n);
                    if (!b) { return false; }
                    const r = b.getBoundingClientRect();
                    return r.width > 8 && r.height > 8; }""", name))

    page.evaluate("() => window.minipaintClipboard.openToolbarMenu('sort')")
    time.sleep(0.4)
    state = page.evaluate("() => window.minipaintClipboard.debug()")
    r.check("the Sort button opens the sort list, standing on its own",
            state["menuOpen"] and state["menuSection"] == "sort" and state["menuStandalone"] is True, str(state["menuSection"]))
    labels = page.evaluate("""() => Array.from(document.querySelectorAll(
        '.minipaint-clip-menu .minipaint-clip-menu-item')).map(b => b.textContent.trim())""")
    r.check("with every sort in it and no Back row, because nothing is behind it",
            len(labels) >= 6 and not any(one.startswith("‹") for one in labels), str(labels))
    under = page.evaluate("""() => { const b = document.getElementById('minipaint_clipboard_sort_open').getBoundingClientRect();
        const m = document.querySelector('.minipaint-clip-menu').getBoundingClientRect();
        return { below: m.top >= b.bottom - 1, near: Math.abs(m.left - b.left) < 400 }; }""")
    r.check("and it is drawn under the button that opened it, not under the menu button",
            under["below"] and under["near"], str(under))

    page.evaluate("() => window.minipaintClipboard.openToolbarMenu('send')")
    time.sleep(0.4)
    state = page.evaluate("() => window.minipaintClipboard.debug()")
    r.check("the Send button opens the destinations instead, without closing and reopening by hand",
            state["menuOpen"] and state["menuSection"] == "send", str(state["menuSection"]))
    page.evaluate("() => window.minipaintClipboard.openToolbarMenu('send')")
    time.sleep(0.3)
    r.check("pressing the same button again closes it, the way a menu does",
            page.evaluate("() => window.minipaintClipboard.debug().menuOpen") is False)

    page.evaluate("() => window.minipaintClipboard.toggleMenu()")
    time.sleep(0.4)
    labels = page.evaluate("""() => Array.from(document.querySelectorAll(
        '.minipaint-clip-menu .minipaint-clip-menu-item')).map(b => b.textContent.trim())""")
    r.check("and the menu no longer offers Send selected to, so there is no second door to it",
            not any(one.startswith("Send selected") for one in labels), str(labels))
    r.check("but the menu still opens as the menu, not as a section",
            page.evaluate("() => window.minipaintClipboard.debug().menuStandalone") is False)
    page.evaluate("() => window.minipaintClipboard.closeMenu()")
    time.sleep(0.2)


def _seed_outputs(folder, count, kind="mp4", request_id="seeded", job_id="f" * 16, reset=True):
    """Put ``count`` files in ``folder`` and hand them to the ledger.

    The .mp4 bytes are not a video and are not meant to be: what is being
    checked here is the gallery - the shape it takes, the strip - and none of
    that is the codec's business. The .png is a real picture, so at least one
    tile in this suite is one the browser genuinely decodes and draws. The
    .webm IS a video - see ``_real_clip`` - for the checks that have to watch
    one play.
    """
    from PIL import Image

    from minipaint_neo.clipboard import config as clip_config
    from minipaint_neo.clipboard import outputs

    if reset:
        outputs.reset_for_tests()
    folder.mkdir(parents=True, exist_ok=True)
    clip_config.update(outputs_folder=str(folder))
    made = []
    for index in range(count):
        if kind == "png":
            path = folder / f"shot{index:03d}.png"
            Image.new("RGB", (64, 36), (20, 40 + index, 90)).save(path)
        elif kind == "webm":
            path = folder / f"clip{index:03d}.webm"
            _real_clip(path)
        else:
            path = folder / f"clip{index:03d}.mp4"
            path.write_bytes(b"not really a video" * 8)
        made.append(str(path))
    outputs.remember(job_id, made, request_id=request_id, model="A video model")
    return made


#: The clip the player's checks watch: two seconds at 24 frames a second, so
#: one frame is a known 1/24 s and the end is a known 2.0 s.
CLIP_FRAMES = 48
CLIP_FPS = 24


def _ffmpeg():
    """An ffmpeg that can write WebM, or None.

    Playwright installs one beside its browsers - it records video with it -
    and that one is enough: it reads piped JPEG frames and writes VP8 WebM,
    which is exactly what this needs. A system ffmpeg is the fallback.
    """
    named = os.environ.get("FFMPEG", "")
    if named and pathlib.Path(named).exists():
        return named
    roots = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""), "/opt/pw-browsers",
             str(pathlib.Path.home() / ".cache" / "ms-playwright"),
             str(pathlib.Path.home() / "Library" / "Caches" / "ms-playwright"),
             str(pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright")]
    for root in roots:
        if not root or not pathlib.Path(root).is_dir():
            continue
        for name in ("ffmpeg-linux", "ffmpeg-mac", "ffmpeg-win64.exe"):
            for candidate in sorted(pathlib.Path(root).glob(f"ffmpeg-*/{name}")):
                return str(candidate)
    return shutil.which("ffmpeg")


def _real_clip(path):
    """A real video the browser decodes: numbered frames, VP8 in WebM.

    VP8 because the Chromium that Playwright drives has no H.264 - an MP4 of
    the kind WanGP writes would not play here at all - and WebM is one of the
    suffixes the gallery lists as a video.
    """
    import io
    import subprocess

    from PIL import Image, ImageDraw

    binary = _ffmpeg()
    if not binary:
        raise RuntimeError("no ffmpeg to make a clip with")
    frames = []
    for index in range(CLIP_FRAMES):
        picture = Image.new("RGB", (160, 90), (index * 5 % 255, 80, 200 - index * 3))
        ImageDraw.Draw(picture).text((10, 35), f"frame {index}", fill=(255, 255, 255))
        buffer = io.BytesIO()
        picture.save(buffer, format="JPEG", quality=85)
        frames.append(buffer.getvalue())
    subprocess.run([binary, "-hide_banner", "-loglevel", "error", "-y",
                    # `pipe:0` rather than `-`: Playwright's build has the pipe
                    # protocol and not the shorthand for it.
                    "-f", "image2pipe", "-c:v", "mjpeg", "-framerate", str(CLIP_FPS), "-i", "pipe:0",
                    "-c:v", "libvpx", "-b:v", "400k", "-auto-alt-ref", "0", str(path)],
                   input=b"".join(frames), check=True, timeout=60)
    return path


def check_view_outputs_is_a_gallery(r: Results, page) -> None:
    """What WanGP made, over the whole window: a stage and a strip.

    What has to be true: the panel has no shape on the tab, the button
    fills the window with it, the stage is the biggest thing in it, every
    output has a tile in the strip, choosing one puts it in the stage,
    tapping the stage hides and shows the controls, and it pages at 60 like
    the grid does.
    """
    folder = pathlib.Path(tempfile.mkdtemp(prefix="minipaint-gallery-"))
    try:
        _seed_outputs(folder, 3)
        open_clipboard(page)
        time.sleep(0.6)

        r.check("the gallery is not on the tab at all until it is asked for",
                page.evaluate("() => window.minipaintClipboard.debug().outputsOpen") is False)
        r.check("and its panel takes no room on the tab",
                page.evaluate("""() => { const p = document.getElementById('minipaint_clipboard_outputs_panel');
                    return p ? p.getBoundingClientRect().height : -1; }""") == 0)
        r.check("the way in is a button under the queue, and it is drawn",
                page.evaluate("""() => { const b = document.getElementById('minipaint_clipboard_outputs_open');
                    if (!b) { return false; }
                    const r = b.getBoundingClientRect();
                    return r.width > 8 && r.height > 8; }"""))

        page.evaluate("() => window.minipaintClipboard.openOutputs()")
        page.wait_for_function("() => window.minipaintClipboard.debug().outputs.items > 0", timeout=8000)
        time.sleep(0.5)
        shape = page.evaluate("""() => {
            const panel = document.getElementById('minipaint_clipboard_outputs_panel');
            const stage = panel.querySelector('.minipaint-clip-output-stage');
            const strip = panel.querySelector('.minipaint-clip-output-strip');
            const p = panel.getBoundingClientRect();
            const s = stage ? stage.getBoundingClientRect() : {width: 0, height: 0};
            const t = strip ? strip.getBoundingClientRect() : {width: 0, height: 0};
            return { pw: p.width, ph: p.height, sw: s.width, sh: s.height, tw: t.width, th: t.height,
                     vw: window.innerWidth, vh: window.innerHeight,
                     tiles: panel.querySelectorAll('.minipaint-clip-output-tile').length,
                     media: !!panel.querySelector('.minipaint-clip-output-stage-media') }; }""")
        r.check("the press fills the window",
                shape["pw"] >= shape["vw"] * 0.9 and shape["ph"] >= shape["vh"] * 0.9, str(shape))
        r.check("the stage is the biggest thing in it, which is the point of the view",
                shape["sh"] >= shape["vh"] * 0.45 and shape["sw"] >= shape["pw"] * 0.8, str(shape))
        r.check("the filmstrip is under it, one row and not half the screen",
                0 < shape["th"] <= shape["vh"] * 0.25, str(shape))
        r.check("every output has a tile in the strip", shape["tiles"] == 3, str(shape["tiles"]))
        r.check("and the first one is already in the stage", shape["media"])

        kinds = page.evaluate("""() => { const p = document.getElementById('minipaint_clipboard_outputs_panel');
            return { stage: p.querySelector('.minipaint-clip-output-stage-media').tagName,
                     thumb: p.querySelector('.minipaint-clip-output-thumb').tagName }; }""")
        r.check("a video is played in a video element, in the stage and in the strip",
                kinds["stage"] == "VIDEO" and kinds["thumb"] == "VIDEO", str(kinds))
        # The address is attached when the tile comes near the strip's window,
        # so this waits for the first one rather than reading it the instant
        # the panel is drawn. What it asserts has not changed: the poster is
        # one frame of the file itself, and nothing makes a thumbnail anywhere.
        page.wait_for_function(
            """() => { const t = document.querySelector('.minipaint-clip-output-thumb');
                return !!t && !!t.getAttribute('src'); }""", timeout=8000)
        r.check("and the strip's poster is asked for as one frame, so no thumbnail has to be made anywhere",
                page.evaluate("""() => { const t = document.querySelector('.minipaint-clip-output-thumb');
                    return (t.getAttribute('src') || '').indexOf('#t=') > 0; }"""))
        r.check("the address waits in data-src until then, so a tile off the end of the strip fetches nothing",
                page.evaluate("""() => { const t = document.querySelector('.minipaint-clip-output-thumb');
                    return (t.dataset.src || '').indexOf('#t=') > 0; }"""))

        first = page.evaluate("() => window.minipaintClipboard.debug().outputs.chosen")
        page.evaluate("""() => { const tiles = document.querySelectorAll('.minipaint-clip-output-tile');
            tiles[tiles.length - 1].click(); }""")
        time.sleep(0.4)
        second = page.evaluate("() => window.minipaintClipboard.debug().outputs.chosen")
        r.check("choosing a tile puts that one in the stage", second and second != first, f"{first} -> {second}")
        r.check("and the strip marks which one is playing",
                page.evaluate("() => document.querySelectorAll('.minipaint-clip-output-chosen').length") == 1)

        # The controls used to be the browser's own, drawn over the picture,
        # and a tap on the stage hid them so the picture could be seen. They
        # are the page's now and sit UNDER the picture, so there is nothing
        # over it to hide - which is what these two hold the line on. The
        # player itself is checked with a real clip in
        # check_the_player_is_the_pages.
        placed = page.evaluate("""() => {
            const video = document.querySelector('.minipaint-clip-output-stage-media');
            const bar = document.querySelector('.minipaint-clip-player');
            if (!video || !bar) { return null; }
            return { native: video.controls, videoBottom: video.getBoundingClientRect().bottom,
                     barTop: bar.getBoundingClientRect().top };
        }""")
        r.check("the browser's own controls are off, and the page's are there instead",
                bool(placed) and placed["native"] is False, str(placed))
        r.check("and they are under the picture, never over it, so nothing has to be hidden to see it",
                bool(placed) and placed["barTop"] >= placed["videoBottom"] - 1, str(placed))

        # A page of outputs is sixty items, and a video tile is a `<video>`:
        # the browser opens a connection, range-requests enough of the file to
        # paint the frame at #t=1, and spins up a decoder. Sixty of those at
        # once is sixty decoders competing for CPU and for the six connections
        # this origin has - which the page has already spent most of on
        # streams that never close. The stage player's own range requests
        # queue behind all of it, and that is what choppy playback is here.
        with tempfile.TemporaryDirectory(prefix="minipaint-outputs-many-") as many:
            folder = pathlib.Path(many)
            _seed_outputs(folder, 40)
            # Counted on the wire, not in the DOM. The tiles that scroll away
            # have their address taken off again, so a moment after the strip
            # is drawn the page LOOKS the same whether or not it asked for all
            # forty - the difference is the burst of requests it made getting
            # there, and that burst is the whole problem.
            asked = set()
            def note(request):
                if "/minipaint-clipboard/output/" in request.url:
                    asked.add(request.url.split("?")[0].split("#")[0])
            page.on("request", note)
            page.evaluate("() => window.minipaintClipboard.openOutputs()")
            page.wait_for_function("() => window.minipaintClipboard.debug().outputs.items > 20", timeout=8000)
            page.wait_for_function(
                """() => { const t = document.querySelector('.minipaint-clip-output-thumb');
                    return !!t && !!t.getAttribute('src'); }""", timeout=8000)
            time.sleep(1.2)
            page.remove_listener("request", note)
            tiles = page.evaluate(
                "() => document.querySelectorAll('.minipaint-clip-output-thumb').length")
            r.check("forty outputs make forty tiles", tiles == 40, str(tiles))
            # One row of 132px tiles: what is near the strip's window is a
            # dozen or so however wide the browser is, plus the one playing.
            r.check("but the page asks for only the files near the strip's window, not all forty",
                    0 < len(asked) < 30, f"{len(asked)} of {tiles} requested")
        # Back to the three this check started with, for what follows.
        _seed_outputs(folder, 3)
        page.evaluate("() => window.minipaintClipboard.openOutputs()")
        page.wait_for_function("() => window.minipaintClipboard.debug().outputs.items > 0", timeout=8000)
        time.sleep(0.3)

        page.keyboard.press("Escape")
        time.sleep(0.4)
        r.check("Escape closes it", page.evaluate("() => window.minipaintClipboard.debug().outputsOpen") is False)
        r.check("and the player lets go of the file, rather than going on downloading it behind a closed view",
                page.evaluate("""() => { const v = document.querySelector('.minipaint-clip-output-stage-media');
                    return !v || !v.getAttribute('src'); }"""))

        page.evaluate("() => window.minipaintClipboard.openOutputs()")
        page.wait_for_function("() => window.minipaintClipboard.debug().outputs.items > 0", timeout=8000)
        time.sleep(0.3)
        page.evaluate("""() => { const b = Array.from(document.querySelectorAll('.minipaint-clip-output-close'))[0];
            if (b) { b.click(); } }""")
        time.sleep(0.4)
        r.check("and so does its own Close", page.evaluate("() => window.minipaintClipboard.debug().outputsOpen") is False)

        # A picture is not played, it is shown - and this one is a real PNG,
        # so the tile below is one the browser actually decoded.
        _seed_outputs(folder / "shots", 1, kind="png")
        page.evaluate("() => window.minipaintClipboard.openOutputs()")
        page.wait_for_function("() => window.minipaintClipboard.debug().outputs.items > 0", timeout=8000)
        time.sleep(0.5)
        drawn = page.evaluate("""() => { const p = document.getElementById('minipaint_clipboard_outputs_panel');
            const img = p.querySelector('.minipaint-clip-output-stage-media');
            return { tag: img.tagName, w: img.naturalWidth, h: img.naturalHeight }; }""")
        r.check("a picture is shown in an image element, and the browser really decoded it",
                drawn["tag"] == "IMG" and drawn["w"] == 64 and drawn["h"] == 36, str(drawn))
        page.evaluate("() => window.minipaintClipboard.closeOutputs()")
        time.sleep(0.3)

        # And the pager, at the same 60 the picture grid uses.
        _seed_outputs(folder / "many", 63)
        page.evaluate("() => window.minipaintClipboard.openOutputs()")
        page.wait_for_function("() => window.minipaintClipboard.debug().outputs.items > 0", timeout=15000)
        time.sleep(0.6)
        paged = page.evaluate("() => window.minipaintClipboard.debug().outputs")
        r.check("a page of the gallery is 60, the same as the grid's, and it says how many there are",
                paged["items"] == 60 and paged["pages"] == 2, str(paged))
        page.evaluate("""() => { const b = Array.from(document.querySelectorAll('.minipaint-clip-output-page'));
            b[b.length - 1].click(); }""")
        page.wait_for_function("() => window.minipaintClipboard.debug().outputs.page === 1", timeout=8000)
        time.sleep(0.4)
        paged = page.evaluate("() => window.minipaintClipboard.debug().outputs")
        r.check("and the next page holds the rest", paged["items"] == 3 and paged["page"] == 1, str(paged))
        page.evaluate("() => window.minipaintClipboard.closeOutputs()")
        time.sleep(0.3)

        # And it starts BELOW whatever the theme has pinned to the top.
        #
        # "Over the whole window" used to mean the viewport, which includes
        # the WebUI's own header: under the Lobe theme a video played here
        # covered the tab bar, and with it the way out of the tab. The header
        # is a theme's, not this extension's, so what is put on the page is
        # the only thing every theme's header has in common - something
        # pinned to the top edge - and the panel has to clear it without
        # being told it is there.
        page.evaluate("""() => {
            const bar = document.createElement('div');
            bar.id = 'a-theme-header';
            bar.style.cssText = 'position:fixed;top:0;left:0;right:0;height:64px;z-index:9999;background:#123';
            document.body.appendChild(bar);
        }""")
        page.evaluate("() => window.minipaintClipboard.openOutputs()")
        page.wait_for_function("() => window.minipaintClipboard.debug().outputs.items > 0", timeout=8000)
        time.sleep(0.5)
        placed = page.evaluate("""() => {
            const panel = document.getElementById('minipaint_clipboard_outputs_panel');
            const media = panel.querySelector('.minipaint-clip-output-stage-media');
            const bar = document.getElementById('a-theme-header');
            const p = panel.getBoundingClientRect();
            const m = media ? media.getBoundingClientRect() : null;
            return { top: Math.round(p.top), bottom: Math.round(p.bottom),
                     mediaTop: m ? Math.round(m.top) : -1,
                     header: Math.round(bar.getBoundingClientRect().bottom),
                     vh: window.innerHeight }; }""")
        r.check("the panel starts under the theme's header rather than over it",
                placed["top"] >= placed["header"], str(placed))
        r.check("and the video with it, which is what covered the tab bar",
                placed["mediaTop"] >= placed["header"], str(placed))
        r.check("it still reaches the bottom of the window, so the room is taken not lost",
                placed["bottom"] >= placed["vh"] - 2, str(placed))
        page.evaluate("() => window.minipaintClipboard.closeOutputs()")
        page.evaluate("""() => { const bar = document.getElementById('a-theme-header');
            if (bar) { bar.remove(); }
            document.documentElement.style.removeProperty('--minipaint-clip-top'); }""")
        time.sleep(0.3)
    finally:
        from minipaint_neo.clipboard import config as clip_config
        from minipaint_neo.clipboard import outputs

        outputs.reset_for_tests()
        clip_config.update(outputs_folder="")
        shutil.rmtree(folder, ignore_errors=True)


#: A request id of the shape the protocol accepts, so the ledger, the history
#: and the join between them all take it.
PLAYER_REQUEST = "ab" * 16
LOAD_REQUEST = "cd" * 16
NO_RECIPE_REQUEST = "ef" * 16


def _open_outputs_fresh(page):
    """Open View Outputs and wait for THIS opening's draw.

    Closing the view leaves its last draw on the panel, so "there are items"
    is already true before the new answer arrives - which is how a check once
    read the previous check's outputs and reported that none had a recipe.
    """
    before = page.evaluate("() => window.minipaintClipboard.debug().outputs.drawn")
    page.evaluate("() => window.minipaintClipboard.openOutputs()")
    page.wait_for_function("n => window.minipaintClipboard.debug().outputs.drawn > n", arg=before, timeout=8000)


def _open_outputs_on(page, name):
    """Open View Outputs and wait until ``name`` is in the stage and playable."""
    _open_outputs_fresh(page)
    page.evaluate("""name => {
        const item = [...document.querySelectorAll('.minipaint-clip-output-tile')]
            .find((t) => (t.title || '').indexOf(name) === 0);
        if (item) { item.click(); }
    }""", name)


def _video_ready(page, timeout=10000):
    page.wait_for_function("""() => {
        const v = document.querySelector('.minipaint-clip-output-stage-media');
        return !!v && v.tagName === 'VIDEO' && v.readyState >= 2 && isFinite(v.duration) && v.duration > 1;
    }""", timeout=timeout)


def check_the_player_is_the_pages(r: Results, page) -> None:
    """Asked for: "real playback controls ... on my mobile device i cannot
    loop the video ... scrubbing timeline and mute toggle".

    Everything here is against a real clip the browser decodes, because a
    player is only checked by watching it play: two seconds, 24 frames a
    second, so a frame and the end are both known numbers.
    """
    from minipaint_neo.clipboard import outputs

    binary = _ffmpeg()
    r.check("a real clip can be made for the player's checks (Playwright's ffmpeg, or one on PATH)",
            bool(binary), "no ffmpeg found")
    if not binary:
        return
    folder = pathlib.Path(tempfile.mkdtemp(prefix="minipaint-player-"))
    try:
        try:
            _seed_outputs(folder, 1, kind="webm", request_id=PLAYER_REQUEST, job_id="e" * 16)
        except Exception as error:
            # One failed check, not a suite that stops here: everything after
            # this in the run is about other parts of the tab.
            r.check("the clip for the player's checks was made", False, f"{type(error).__name__}: {error}")
            return
        page.evaluate("() => localStorage.removeItem('minipaint-outputs-player:v1')")
        # The theme's rule about buttons, on the page, so the sizes below are
        # sizes a Lobe user gets.
        page.evaluate("""() => {
            const s = document.createElement('style');
            s.id = 'hostile-button-rule';
            s.textContent = 'button { min-width: fit-content !important; padding: 0 !important; }';
            document.head.appendChild(s);
        }""")
        _open_outputs_on(page, "clip000.webm")
        _video_ready(page)

        controls = page.evaluate("""() => {
            const bar = document.querySelector('.minipaint-clip-player');
            const names = [...bar.querySelectorAll('[data-control]')].map((b) => b.dataset.control);
            const small = [...bar.querySelectorAll('.minipaint-clip-player-button')]
                .filter((b) => !b.hidden)
                .map((b) => b.getBoundingClientRect())
                .filter((box) => box.width < 43.5 || box.height < 43.5).length;
            const scrub = bar.querySelector('.minipaint-clip-player-scrub').getBoundingClientRect();
            return { names, small, scrubHeight: scrub.height };
        }""")
        r.check("the bar has play, a frame each way, back to the start, loop, mute, speed and full screen",
                set(controls["names"]) >= {"play", "back", "forward", "restart", "loop", "sound", "speed", "full"},
                str(controls["names"]))
        r.check("every button is a finger wide and a finger tall, with a theme's button rule on the page",
                controls["small"] == 0, str(controls))
        r.check("and the timeline is a finger tall too", controls["scrubHeight"] >= 43.5, str(controls))

        page.wait_for_timeout(600)
        if page.evaluate("() => document.querySelector('.minipaint-clip-output-stage-media').paused"):
            page.click(".minipaint-clip-player [data-control='play']")
        page.wait_for_function("() => !document.querySelector('.minipaint-clip-output-stage-media').paused",
                               timeout=5000)
        r.check("it plays, and the play button says Pause while it does",
                page.evaluate("() => document.querySelector(\"[data-control='play']\").getAttribute('aria-label')") == "Pause")
        first = page.evaluate("() => document.querySelector('.minipaint-clip-player-time').textContent")
        page.wait_for_timeout(700)
        second = page.evaluate("() => document.querySelector('.minipaint-clip-player-time').textContent")
        r.check("the time moves while it plays", first != second, f"{first} -> {second}")
        r.check("and the clock that moves it is running", page.evaluate("() => window.minipaintClipboard.debug().outputs.clock") is True)

        r.check("loop is on by default, because a clip is a few seconds long",
                page.evaluate("""() => document.querySelector('.minipaint-clip-output-stage-media').loop
                    && document.querySelector("[data-control='loop']").getAttribute('aria-pressed') === 'true'"""))
        page.wait_for_timeout(2300)
        r.check("and it really loops: still playing after the two seconds are up",
                page.evaluate("""() => { const v = document.querySelector('.minipaint-clip-output-stage-media');
                    return !v.paused && !v.ended; }"""))

        # A press on the picture is pause, the way a phone's player works.
        page.click(".minipaint-clip-output-stage-media")
        page.wait_for_timeout(200)
        r.check("a press on the picture pauses it",
                page.evaluate("() => document.querySelector('.minipaint-clip-output-stage-media').paused"))
        r.check("and a paused video costs nothing: the clock has stopped",
                page.evaluate("() => window.minipaintClipboard.debug().outputs.clock") is False)

        # A frame each way, from a known place.
        page.evaluate("""() => { const v = document.querySelector('.minipaint-clip-output-stage-media');
            v.currentTime = 0.5; }""")
        page.wait_for_timeout(300)
        page.click(".minipaint-clip-player [data-control='forward']")
        page.wait_for_timeout(300)
        forward = page.evaluate("() => document.querySelector('.minipaint-clip-output-stage-media').currentTime")
        r.check("a frame forward is one frame", abs(forward - (0.5 + 1 / CLIP_FPS)) < 0.01, str(forward))
        page.click(".minipaint-clip-player [data-control='back']")
        page.wait_for_timeout(300)
        back = page.evaluate("() => document.querySelector('.minipaint-clip-output-stage-media').currentTime")
        r.check("and a frame back is one frame back", abs(back - 0.5) < 0.01, str(back))

        # The timeline, the way a finger drives it: input while held, change
        # where it is let go. While held it seeks coarsely where the browser
        # can (`fastSeek`, which Firefox and Safari have and Chromium does
        # not), so this page is given one that lands on whole seconds - the
        # way a keyframe seek does - and the exact seek on letting go is what
        # has to bring it to 1.5. Without the stand-in, Chromium's plain seek
        # is exact either way and the check could not tell.
        page.evaluate("""() => {
            HTMLMediaElement.prototype.fastSeek = function (t) { this.currentTime = Math.round(t); };
            const s = document.querySelector('.minipaint-clip-player-scrub');
            s.value = '1.5';
            s.dispatchEvent(new Event('input', {bubbles: true}));
        }""")
        page.wait_for_timeout(300)
        held = page.evaluate("() => document.querySelector('.minipaint-clip-output-stage-media').currentTime")
        page.evaluate("""() => {
            const s = document.querySelector('.minipaint-clip-player-scrub');
            s.dispatchEvent(new Event('change', {bubbles: true}));
            delete HTMLMediaElement.prototype.fastSeek;
        }""")
        page.wait_for_timeout(400)
        scrubbed = page.evaluate("() => document.querySelector('.minipaint-clip-output-stage-media').currentTime")
        r.check("while the thumb is held the seek is the quick one", abs(held - 2.0) < 0.05, str(held))
        r.check("and where it is let go the video is exactly there", abs(scrubbed - 1.5) < 0.05, str(scrubbed))
        r.check("and its length is the video's", abs(float(page.evaluate(
            "() => document.querySelector('.minipaint-clip-player-scrub').max")) - CLIP_FRAMES / CLIP_FPS) < 0.1)

        # Loop off, mute on, speed up: remembered across a new video.
        page.click(".minipaint-clip-player [data-control='loop']")
        page.click(".minipaint-clip-player [data-control='sound']")
        page.click(".minipaint-clip-player [data-control='speed']")
        page.wait_for_timeout(200)
        now = page.evaluate("""() => { const v = document.querySelector('.minipaint-clip-output-stage-media');
            return { loop: v.loop, muted: v.muted, rate: v.playbackRate,
                     sound: document.querySelector("[data-control='sound']").getAttribute('aria-label'),
                     speed: document.querySelector("[data-control='speed']").textContent }; }""")
        r.check("loop turns off, mute turns on, and speed steps up",
                now["loop"] is False and now["muted"] is True and now["rate"] == 1.5, str(now))
        r.check("and the buttons say so", now["sound"] == "Unmute" and now["speed"] == "1.5×", str(now))
        page.evaluate("() => window.minipaintClipboard.closeOutputs()")
        page.wait_for_timeout(200)
        _open_outputs_on(page, "clip000.webm")
        _video_ready(page)
        kept = page.evaluate("""() => { const v = document.querySelector('.minipaint-clip-output-stage-media');
            return { loop: v.loop, muted: v.muted, rate: v.playbackRate,
                     pressed: document.querySelector("[data-control='loop']").getAttribute('aria-pressed') }; }""")
        r.check("all three are remembered for the next video",
                kept == {"loop": False, "muted": True, "rate": 1.5, "pressed": "false"}, str(kept))

        # The keys, while the view is open.
        page.evaluate("() => { const v = document.querySelector('.minipaint-clip-output-stage-media'); v.pause(); }")
        page.keyboard.press("m")
        page.wait_for_timeout(150)
        r.check("M is mute", page.evaluate("() => document.querySelector('.minipaint-clip-output-stage-media').muted") is False)
        page.keyboard.press("k")
        page.wait_for_timeout(300)
        r.check("K is play", page.evaluate("() => !document.querySelector('.minipaint-clip-output-stage-media').paused"))
        page.keyboard.press("k")
        page.wait_for_timeout(150)

        # Replacing a playing video: the old one's pause arrives after the new
        # one has started, and must not stop the new one's clock.
        _seed_outputs(folder, 2, kind="webm", request_id=PLAYER_REQUEST, job_id="e" * 16)
        page.evaluate("() => window.minipaintClipboard.closeOutputs()")
        page.wait_for_timeout(200)
        _open_outputs_on(page, "clip000.webm")
        _video_ready(page)
        page.evaluate("""() => { const v = document.querySelector('.minipaint-clip-output-stage-media');
            window.__replaced = v; v.play(); }""")
        page.wait_for_timeout(300)
        page.evaluate("""() => { const tile = [...document.querySelectorAll('.minipaint-clip-output-tile')]
            .find((t) => (t.title || '').indexOf('clip001.webm') === 0); tile.click(); }""")
        _video_ready(page)
        page.wait_for_function("() => !document.querySelector('.minipaint-clip-output-stage-media').paused", timeout=5000)
        page.wait_for_timeout(300)
        # Chromium happens to deliver the old video's pause before the new
        # one's play; nothing promises that order, so the late one is sent
        # here on purpose.
        page.evaluate("() => { window.__replaced.dispatchEvent(new Event('pause')); delete window.__replaced; }")
        page.wait_for_timeout(300)
        r.check("the next video's clock is running, whatever the last one's pause said on its way out",
                page.evaluate("() => window.minipaintClipboard.debug().outputs.clock") is True)

        page.evaluate("() => window.minipaintClipboard.closeOutputs()")
        page.wait_for_timeout(200)
        gone = page.evaluate("""() => ({ clock: window.minipaintClipboard.debug().outputs.clock,
            player: window.minipaintClipboard.debug().outputs.player })""")
        r.check("closing the view stops the clock and lets go of the controls", gone == {"clock": False, "player": False}, str(gone))
    finally:
        page.evaluate("() => { const s = document.getElementById('hostile-button-rule'); if (s) { s.remove(); } }")
        page.evaluate("() => localStorage.removeItem('minipaint-outputs-player:v1')")
        outputs.reset_for_tests()
        shutil.rmtree(folder, ignore_errors=True)


def _full_screen(page) -> str:
    """What is full screen: the page, the player's stage, something else, or nothing."""
    return page.evaluate("""() => {
        const now = document.fullscreenElement;
        if (!now) { return 'nothing'; }
        if (now === document.documentElement) { return 'page'; }
        return now.classList.contains('minipaint-clip-output-stage') ? 'stage' : 'other';
    }""")


def check_the_players_full_screen_is_its_own(r: Results, page) -> None:
    """The page can be full screen without the player: the Forge Assistant's
    focus mode asks for the whole document. The player used to take any full
    screen for its own - its button "left" the page's instead of entering the
    stage's, and closing the view ended the page's, and focus mode with it.

    Real full screen, asked for with real presses: a browser grants it only
    inside one, and headless Chromium grants it like any other.
    """
    from minipaint_neo.clipboard import outputs

    binary = _ffmpeg()
    if not binary:
        r.check("a real clip can be made for the full-screen checks", False, "no ffmpeg found")
        return
    folder = pathlib.Path(tempfile.mkdtemp(prefix="minipaint-full-"))
    full = ".minipaint-clip-player [data-control='full']"
    label = f"() => document.querySelector(\"{full}\").getAttribute('aria-label')"
    try:
        try:
            _seed_outputs(folder, 1, kind="webm", request_id=PLAYER_REQUEST, job_id="e" * 16)
        except Exception as error:
            r.check("the clip for the full-screen checks was made", False, f"{type(error).__name__}: {error}")
            return
        # The page's own full screen, the way the assistant asks for it: a
        # press, and the document. Above the view, so it can be pressed.
        page.evaluate("""() => {
            const b = document.createElement('button');
            b.id = 'page-full-screen';
            b.textContent = 'page';
            b.style.cssText = 'position:fixed;left:0;bottom:0;z-index:2147483647';
            b.onclick = () => document.documentElement.requestFullscreen();
            document.body.appendChild(b);
        }""")
        _open_outputs_on(page, "clip000.webm")
        _video_ready(page)

        # On its own: the stage, and back.
        page.click(full)
        page.wait_for_timeout(400)
        alone = {"on": _full_screen(page), "label": page.evaluate(label)}
        page.click(full)
        page.wait_for_timeout(400)
        alone["off"] = _full_screen(page)
        r.check("on its own, the button makes the stage full screen and says Leave",
                alone["on"] == "stage" and alone["label"] == "Leave full screen", str(alone))
        r.check("and a second press gives the screen back", alone["off"] == "nothing", str(alone))

        # With the page already full screen.
        page.click("#page-full-screen")
        page.wait_for_timeout(400)
        before = {"full": _full_screen(page), "label": page.evaluate(label),
                  "stage": page.evaluate("""() => document.querySelector('.minipaint-clip-output-stage')
                      .classList.contains('minipaint-clip-output-full')""")}
        r.check("the page going full screen is not the player going full screen",
                before == {"full": "page", "label": "Full screen", "stage": False}, str(before))
        page.click(full)
        page.wait_for_timeout(400)
        stacked = _full_screen(page)
        r.check("its button still makes the stage full screen, on top of the page's",
                stacked == "stage", stacked)
        page.click(full)
        page.wait_for_timeout(400)
        unstacked = _full_screen(page)
        r.check("and a second press leaves the page's full screen where it was",
                unstacked == "page", unstacked)
        page.evaluate("() => window.minipaintClipboard.closeOutputs()")
        page.wait_for_timeout(400)
        closed = _full_screen(page)
        r.check("closing the view leaves the page's full screen alone too", closed == "page", closed)
    finally:
        page.evaluate("""() => {
            if (document.fullscreenElement) { document.exitFullscreen().catch(() => {}); }
            const b = document.getElementById('page-full-screen');
            if (b) { b.remove(); }
        }""")
        page.wait_for_timeout(300)
        page.evaluate("() => window.minipaintClipboard.closeOutputs()")
        outputs.reset_for_tests()
        shutil.rmtree(folder, ignore_errors=True)


def check_an_output_loads_its_recipe(r: Results, page) -> None:
    """Asked for: "a way to Load from the view outputs ... load the prompt and
    images if available so i can generate again".

    Load is the Queue Send History's own Load, reached from the output that
    request made: the prompt as typed, each picture still in the library, and
    nothing queued.
    """
    from minipaint_neo.clipboard import history, outputs

    folder = pathlib.Path(tempfile.mkdtemp(prefix="minipaint-load-"))
    asset = page.evaluate("() => { const c = document.querySelector('.minipaint-clip-item'); return c ? c.dataset.asset : ''; }")
    record = None
    try:
        record = history.add_history({
            "history_id": "load" + "0" * 12, "request_id": LOAD_REQUEST,
            "admitted_at": "2026-09-23T10:00:00+00:00", "model_type": "t2v", "model_label": "A video model",
            "prompt_mode": "override", "prompt_override": "a red fox crossing the snow at dusk",
            "first_mode": "override" if asset else "inherit", "first_asset_id": asset,
            "last_mode": "inherit", "reference_mode": "inherit", "tasks_added": 1,
        })
        _seed_outputs(folder / "made", 1, kind="png", request_id=LOAD_REQUEST, job_id="d" * 16)
        _seed_outputs(folder / "stray", 1, kind="png", request_id=NO_RECIPE_REQUEST, job_id="c" * 16, reset=False)
        page.evaluate("() => { const box = document.querySelector('#minipaint_clipboard_prompt textarea'); return box ? box.value : null; }")
        _open_outputs_fresh(page)
        buttons = page.evaluate("""() => {
            const items = [...document.querySelectorAll('.minipaint-clip-output-tile')];
            const seen = {};
            for (const tile of items) {
                tile.click();
                const load = document.querySelector('.minipaint-clip-output-load');
                seen[tile.dataset.output] = load ? { disabled: load.disabled, title: load.title } : null;
            }
            return Object.values(seen);
        }""")
        enabled = [b for b in buttons if b and not b["disabled"]]
        disabled = [b for b in buttons if b and b["disabled"]]
        if not enabled:
            # Say what the server had, so a miss here names its own cause.
            buttons = {"buttons": buttons,
                       "history": [(h.get("request_id") or "")[:6] for h in history.load_history()[:5]],
                       "ledger": [(f.get("request_id") or "")[:6] for f in outputs.files(refresh=False)]}
        r.check("an output whose request left a recipe offers Load, and says nothing is queued",
                len(enabled) == 1 and "Nothing is queued" in enabled[0]["title"], str(buttons))
        r.check("and one with no recipe says so rather than offering a button that does nothing",
                len(disabled) == 1 and "No recipe" in disabled[0]["title"], str(buttons))

        page.evaluate("""() => {
            for (const tile of document.querySelectorAll('.minipaint-clip-output-tile')) {
                tile.click();
                const load = document.querySelector('.minipaint-clip-output-load');
                if (load && !load.disabled) { load.click(); return; }
            }
        }""")
        page.wait_for_function("""() => {
            const box = document.querySelector('#minipaint_clipboard_prompt textarea');
            return !!box && box.value === 'a red fox crossing the snow at dusk';
        }""", timeout=10000)
        r.check("pressing it puts the prompt back in the request", True)
        r.check("and the view gets out of the way of the request it has just changed",
                page.evaluate("() => window.minipaintClipboard.debug().outputsOpen") is False)
        if asset:
            r.check("with the first frame it was made from",
                    page.evaluate("""a => { const card = document.getElementById('minipaint_clipboard_card_first');
                        return !!card && card.innerHTML.indexOf(a) >= 0; }""", asset),
                    "the first-frame card does not show the picture")
        r.check("and nothing was queued by it",
                page.evaluate("() => { const s = document.getElementById('minipaint_clipboard_status'); return s ? s.textContent : ''; }").find("Nothing was queued") >= 0
                or page.evaluate("() => document.body.textContent.indexOf('Nothing was queued') >= 0"))

        # An LTX 2.5 video whose request is in no history at all - the gallery
        # popup asked for it, or the server finished it with nobody watching -
        # loads from the recipe the ledger kept for it.
        assets = page.evaluate("() => [...document.querySelectorAll('.minipaint-clip-item')].map((c) => c.dataset.asset).slice(0, 2)")
        first, last = (assets + ["", ""])[:2]
        from minipaint_neo.clipboard import outputs as clip_outputs

        made = folder / "ltx25"
        made.mkdir(parents=True, exist_ok=True)
        from PIL import Image

        picture = made / "ltx25.png"
        Image.new("RGB", (64, 36), (90, 40, 20)).save(picture)
        clip_outputs.remember("e" * 16, [str(picture)], request_id="", model="LTX-2 2.5 Distilled 22B", recipe={
            "prompt": "she turns from the window and waves", "written": "WRITTEN: she turns from the window and waves at the camera.",
            "first_asset_id": first, "last_asset_id": last, "reference_asset_ids": [],
            "model_type": "ltx2_25_22B_distilled", "model_label": "LTX-2 2.5 Distilled 22B", "origin": "gallery"})
        _open_outputs_on(page, "ltx25.png")
        page.wait_for_function("() => { const n = document.querySelector('.minipaint-clip-output-caption .minipaint-clip-output-name');"
                               " return !!n && n.textContent === 'ltx25.png'; }", timeout=8000)
        caption = page.evaluate("""() => {
            const c = document.querySelector('.minipaint-clip-output-caption');
            const load = c ? c.querySelector('.minipaint-clip-output-load') : null;
            const prompt = c ? c.querySelector('.minipaint-clip-output-prompt') : null;
            return { model: (c && c.querySelector('.minipaint-clip-output-model') || {}).textContent || '',
                     prompt: prompt ? prompt.textContent : '', title: prompt ? prompt.title : '',
                     enhanced: !!(c && [...c.querySelectorAll('.minipaint-clip-badge')].find((b) => b.textContent === 'enhanced')),
                     load: load ? !load.disabled : null }; }""")
        r.check("a video with no history record but a kept recipe offers Load, and names the model that made it",
                caption["load"] is True and caption["model"] == "LTX-2 2.5 Distilled 22B", str(caption))
        r.check("its caption is the prompt as it was typed, the written one under it on hover, marked enhanced",
                caption["prompt"] == "she turns from the window and waves" and "Written for WanGP: WRITTEN:" in caption["title"]
                and caption["enhanced"], str(caption))
        page.evaluate("() => { const b = document.querySelector('.minipaint-clip-output-caption .minipaint-clip-output-load'); if (b) { b.click(); } }")
        page.wait_for_function("""() => { const box = document.querySelector('#minipaint_clipboard_prompt textarea');
            return !!box && box.value === 'she turns from the window and waves'; }""", timeout=10000)
        r.check("Load puts back the typed prompt - not the one WanGP was given", True)
        if first and last:
            r.check("with its first and last frames", page.evaluate("""a => {
                const f = document.getElementById('minipaint_clipboard_card_first'), l = document.getElementById('minipaint_clipboard_card_last');
                return !!f && !!l && f.innerHTML.indexOf(a[0]) >= 0 && l.innerHTML.indexOf(a[1]) >= 0; }""", [first, last]),
                    "the frame cards do not show the pictures")
    finally:
        if record:
            history.delete_history(record["history_id"])
        history.save_draft(history.empty_draft())
        page.evaluate("""() => { const box = document.querySelector('#minipaint_clipboard_prompt textarea');
            if (box && window.minipaintWriteInput) { window.minipaintWriteInput(box, ''); } }""")
        outputs.reset_for_tests()
        shutil.rmtree(folder, ignore_errors=True)


def check_the_tab_fills_the_window(r: Results, page) -> None:
    """Asked for: "it doesnt fill the page ... resize the thumbnail browser and
    the right side column ... to responsively fill the browser window better
    vertically".

    The grid was capped at 70vh, and the composer was as tall as its content.
    Both now end at the bottom of the window, less a small gap - measured,
    because nothing a stylesheet can say inside Gradio's containers comes out
    as a pixel height.
    """
    GAP = 12

    def measure():
        return page.evaluate("""() => {
            window.minipaintClipboard.fit();
            const grid = document.querySelector('#minipaint_clipboard_grid .minipaint-clip-grid');
            const column = document.getElementById('minipaint_clipboard_browser');
            const composer = document.getElementById('minipaint_clipboard_composer');
            const box = document.querySelector('#minipaint_clipboard_prompt textarea');
            return { view: window.innerHeight, scrolled: window.scrollY,
                     grid: grid.getBoundingClientRect().height,
                     columnBottom: column.getBoundingClientRect().bottom + window.scrollY,
                     composerBottom: composer.getBoundingClientRect().bottom + window.scrollY,
                     composerTop: composer.getBoundingClientRect().top,
                     columnTop: column.getBoundingClientRect().top,
                     prompt: box ? box.getBoundingClientRect().height : 0,
                     promptMin: box ? box.style.getPropertyValue('min-height') : '',
                     fitted: window.minipaintClipboard.debug().fitted };
        }""")

    try:
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(300)
        first = measure()
        r.check("the grid is no longer held to seventy percent of the window", first["grid"] > first["view"] * 0.7 - 1
                or first["columnBottom"] >= first["view"] - GAP - 4, str(first))
        r.check("the browser column ends at the bottom of the window",
                abs(first["columnBottom"] - (first["view"] - GAP)) <= 4, str(first))
        # However long the queue is by now in this run: either the Prompt box
        # took what was left, or the column is held to the window and the
        # rest of it scrolls inside. Never past the bottom of the window.
        r.check("and so does the request column, whatever the queue under it holds",
                abs(first["composerBottom"] - (first["view"] - GAP)) <= 4, str(first))

        page.set_viewport_size({"width": 1400, "height": 1600})
        page.wait_for_timeout(500)
        taller = measure()
        r.check("a taller window gives the grid the difference",
                abs((taller["grid"] - first["grid"]) - 650) <= 6, f"{first['grid']} -> {taller['grid']}")
        # How tall the request column's content is with its Prompt box at the
        # floor: if that fits in this window, the box must have grown to fill it.
        at_floor = first["composerBottom"] + (first["fitted"] or {}).get("overflow", 0) - first["prompt"] + 96
        if at_floor < taller["view"] - GAP:
            r.check("and the Prompt box takes whatever the request column has left over",
                    abs(taller["composerBottom"] - (taller["view"] - GAP)) <= 4 and taller["prompt"] > first["prompt"],
                    str(taller))
        else:
            r.check("the request column is longer than even this window, so its Prompt box stays at its floor",
                    taller["promptMin"] == "96px", str(taller))

        # Idle, nothing is rewritten: the fit is not feeding itself.
        writes = page.evaluate("""() => new Promise((resolve) => {
            const grid = document.querySelector('#minipaint_clipboard_grid .minipaint-clip-grid');
            const box = document.querySelector('#minipaint_clipboard_prompt textarea');
            const composer = document.getElementById('minipaint_clipboard_composer');
            let count = 0;
            const watch = new MutationObserver((records) => { count += records.length; });
            watch.observe(grid, { attributes: true, attributeFilter: ['style'] });
            if (box) { watch.observe(box, { attributes: true, attributeFilter: ['style'] }); }
            watch.observe(composer, { attributes: true, attributeFilter: ['style', 'data-minipaint-windowed'] });
            setTimeout(() => { watch.disconnect(); resolve(count); }, 1500);
        })""")
        r.check("left alone it writes nothing, so nothing is feeding itself", writes == 0, str(writes))

        page.set_viewport_size({"width": 1400, "height": 950})
        page.wait_for_timeout(500)
        check_a_long_request_column_scrolls_inside_itself(r, page)

        page.set_viewport_size({"width": 560, "height": 900})
        page.wait_for_timeout(600)
        narrow = measure()
        # 560 is narrower than the two columns' minimum widths together
        # (320 + 280), so Gradio stacks them. Asserted, so the two checks
        # after it can never be skipped by a layout that did not stack.
        r.check("at 560px wide the request column is under the browser, not beside it",
                narrow["composerTop"] >= narrow["columnTop"] + 10 and narrow["fitted"]["stacked"] is True, str(narrow))
        r.check("stacked on a narrow window the grid keeps a share of it, so the request is still reachable",
                narrow["grid"] <= narrow["view"] * 0.7 + 1, str(narrow))
        r.check("and the Prompt box is its own size again", narrow["promptMin"] == "", str(narrow))
        stacked = page.evaluate("""() => {
            const composer = document.getElementById('minipaint_clipboard_composer');
            return { windowed: composer.dataset.minipaintWindowed || '',
                     cap: composer.style.getPropertyValue('max-height'),
                     scroll: getComputedStyle(composer).overflowY };
        }""")
        r.check("and the request column, under the browser, is the page's to scroll to rather than a window of its own",
                stacked["windowed"] == "" and stacked["cap"] == "" and stacked["scroll"] == "visible", str(stacked))
    finally:
        page.set_viewport_size({"width": 1400, "height": 950})
        page.wait_for_timeout(400)


def check_a_long_request_column_scrolls_inside_itself(r: Results, page) -> None:
    """Asked for: "the right column ... grows in size to be off page and
    require scrolling. i want the entire clipboard tab to fit, and if
    scrolling happens, the column should be windowed so it scrolls and the
    entire page does not, just the right column".

    The queue grows with every request, and the Prompt box at its floor has
    nothing left to give, so the column used to run off the bottom of the
    window and the page scrolled - the grid and the tab bar with it. A queue
    made taller than any window stands in for a long one: the mount the
    queue is drawn into keeps its style across redraws, and growing it is
    what a real queue does to the column - the fit hears it through the same
    observer, not through a call from here.
    """
    GAP = 12

    def state():
        return page.evaluate("""() => {
            const composer = document.getElementById('minipaint_clipboard_composer');
            const root = document.getElementById('minipaint_clipboard_root');
            const box = document.querySelector('#minipaint_clipboard_prompt textarea');
            const k = composer.getBoundingClientRect();
            return { view: window.innerHeight, scrolled: window.scrollY,
                     composerBottom: k.bottom + window.scrollY,
                     rootBottom: root.getBoundingClientRect().bottom + window.scrollY,
                     scrollHeight: composer.scrollHeight, clientHeight: composer.clientHeight,
                     scrollTop: composer.scrollTop,
                     overflowY: getComputedStyle(composer).overflowY,
                     windowed: composer.dataset.minipaintWindowed || '',
                     promptMin: box ? box.style.getPropertyValue('min-height') : '',
                     fitted: window.minipaintClipboard.debug().fitted };
        }""")

    def grow(pixels):
        page.evaluate("""(pixels) => {
            const list = document.querySelector('#minipaint_clipboard_outbox_list .minipaint-clip-outbox');
            if (!list) { return; }
            if (pixels) {
                list.style.setProperty('min-height', pixels + 'px', 'important');
                list.style.setProperty('max-height', 'none', 'important');
            } else {
                list.style.removeProperty('min-height');
                list.style.removeProperty('max-height');
            }
        }""", pixels)

    open_clipboard(page)
    page.evaluate("() => { document.getElementById('minipaint_clipboard_composer').scrollTop = 0; window.scrollTo(0, 0); }")
    r.check("the queue is on the page to make long",
            page.evaluate("() => !!document.querySelector('#minipaint_clipboard_outbox_list .minipaint-clip-outbox')"))
    before = state()
    try:
        grow(1800)
        page.wait_for_timeout(700)
        long = state()
        r.check("a request column longer than the window still ends at the bottom of it",
                abs(long["composerBottom"] - (long["view"] - GAP)) <= 4, str(long))
        r.check("and so does the whole tab, so the page has nothing of it to scroll to",
                long["rootBottom"] <= long["view"] + 1, str(long))
        r.check("what does not fit is the column's own to scroll",
                long["windowed"] == "1" and long["overflowY"] == "auto"
                and long["scrollHeight"] > long["clientHeight"] + 1000, str(long))
        r.check("and the Prompt box gave back what it could first, down to its floor",
                long["promptMin"] == "96px", str(long))

        # A theme that makes every block of the column a box that clips (a
        # flex item that clips may shrink to nothing): the blocks keep their
        # height and the column scrolls to them, rather than squeezing them
        # into its window.
        heights = """() => Array.from(document.getElementById('minipaint_clipboard_composer').children)
            .filter(e => e.getBoundingClientRect().height > 0)
            .map(e => [e.id || e.className.slice(0, 30), Math.round(e.getBoundingClientRect().height)])"""
        own = page.evaluate(heights)
        page.evaluate("""() => {
            const style = document.createElement('style');
            style.id = 'minipaint-test-clipping-theme';
            style.textContent = '#minipaint_clipboard_composer > * { overflow: hidden !important; }';
            document.head.appendChild(style);
        }""")
        page.wait_for_timeout(500)
        clipped = page.evaluate(heights)
        # Shorter is what squeezing does; a block that clips may come out a
        # margin taller, because it no longer lets its children's margins
        # collapse through it.
        squeezed = [(pair, other) for pair, other in zip(own, clipped)
                    if pair[0] != other[0] or other[1] < pair[1] - 2]
        r.check("a theme that clips the column's blocks cannot squeeze them into its window",
                not squeezed and len(own) == len(clipped), f"{own} -> {clipped}")
        page.evaluate("() => { const s = document.getElementById('minipaint-test-clipping-theme'); if (s) { s.remove(); } }")
        page.wait_for_timeout(300)

        # A page that goes on below the tab, as Forge's does with its footer:
        # there is a page to scroll now, and it still must not move.
        page.evaluate("""() => {
            const footer = document.createElement('div');
            footer.id = 'minipaint-test-footer';
            footer.style.height = '900px';
            document.body.appendChild(footer);
            window.scrollTo(0, 0);
        }""")
        r.check("the page under this check has somewhere to scroll to",
                page.evaluate("() => document.documentElement.scrollHeight > window.innerHeight + 400"))

        # A wheel over the column turns the column, and the page stays put -
        # past the column's end too, where a scroll would otherwise carry on
        # into the page.
        box = page.evaluate("""() => { const k = document.getElementById('minipaint_clipboard_composer').getBoundingClientRect();
            return { x: k.left + k.width / 2, y: k.top + Math.min(k.height / 2, 120) }; }""")
        page.mouse.move(box["x"], box["y"])
        for _ in range(4):
            page.mouse.wheel(0, 400)
            page.wait_for_timeout(150)
        page.wait_for_timeout(400)
        turned = state()
        r.check("a wheel over it scrolls the request column",
                turned["scrollTop"] > 0, str(turned))
        r.check("and not the page", turned["scrolled"] == 0, str(turned))
        for _ in range(12):
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(80)
        page.wait_for_timeout(500)
        end = state()
        r.check("not even once the column has reached its end",
                end["scrolled"] == 0 and end["scrollTop"] >= end["scrollHeight"] - end["clientHeight"] - 2, str(end))

        # The two full-window views live inside this column. Scrolled or not,
        # its scroll box must not clip them: they are the window, not a part
        # of the column.
        page.evaluate("""() => {
            const host = document.getElementById('minipaint_clipboard_sp_open');
            const b = host && (host.tagName === 'BUTTON' ? host : host.querySelector('button'));
            if (b) { b.click(); }
        }""")
        page.wait_for_timeout(1500)
        view = page.evaluate("""() => {
            const panel = document.getElementById('minipaint_clipboard_enhance_panel');
            const p = panel.getBoundingClientRect();
            const inside = (x, y) => { const hit = document.elementFromPoint(x, y); return !!hit && panel.contains(hit); };
            const w = window.innerWidth, h = window.innerHeight;
            return { open: window.minipaintClipboard.debug().editorOpen,
                     width: p.width, height: p.height, w: w, h: h,
                     corners: [inside(12, h - 12), inside(w - 24, h - 12), inside(w / 2, h / 2), inside(12, h / 2)],
                     columnScrolled: document.getElementById('minipaint_clipboard_composer').scrollTop };
        }""")
        r.check("the system prompt editor, opened from a scrolled column, still fills the window",
                view["open"] is True and view["columnScrolled"] > 0
                and view["width"] >= view["w"] * 0.9 and view["height"] >= view["h"] * 0.9, str(view))
        r.check("and none of it is cut off by the column it came from",
                all(view["corners"]), str(view))
        page.evaluate("""() => document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))""")
        page.wait_for_timeout(800)
        r.check("and Escape puts it back", page.evaluate("() => window.minipaintClipboard.debug().editorOpen") is False)
    finally:
        grow(0)
        page.evaluate("""() => {
            for (const id of ['minipaint-test-footer', 'minipaint-test-clipping-theme']) {
                const node = document.getElementById(id);
                if (node) { node.remove(); }
            }
            document.getElementById('minipaint_clipboard_composer').scrollTop = 0;
            window.scrollTo(0, 0);
        }""")
        page.wait_for_timeout(700)
    back = state()
    r.check("a queue that is short again gives the Prompt box back the room it had, and the column what it held before",
            back["promptMin"] == before["promptMin"]
            and abs((back["scrollHeight"] - back["clientHeight"]) - (before["scrollHeight"] - before["clientHeight"])) <= 2
            and abs(back["composerBottom"] - (back["view"] - GAP)) <= 4,
            f"before {before} after {back}")


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
    # A tile's picture is the thumbnail the page holds for it (an object
    # URL), and what was asked of the server for it is what the browser may
    # keep: every address the loader ever asked for carries the version.
    versions = page.evaluate("""() => performance.getEntriesByType('resource').map(e => String(e.name))
        .filter(n => n.indexOf('/minipaint-clipboard/image/') !== -1 && n.indexOf('thumb=1') !== -1)""")
    r.check("because every thumbnail address the page has asked for carries the version of its bytes",
            versions and all("v=" in url for url in versions), str(versions[:2]))
    r.check("and every tile drawn shows the thumbnail the page already holds",
            page.evaluate("""() => Array.from(document.querySelectorAll('.minipaint-clip-item img'))
                .every(i => (i.getAttribute('src') || '').indexOf('blob:') === 0)"""))

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
    # The intercept is a destination now: a section of the menu with three
    # mutually exclusive choices, the current one ticked.
    r.check("handed in: the menu offers Intercept Options", menu_click(page, "Intercept Options") == "clicked")
    time.sleep(0.4)
    choices = page.evaluate(MENU_ITEMS_JS)
    r.check("handed in: with Mini Paint, Clipboard and WanGP as its choices, Mini Paint ticked",
            any("Mini Paint" in item and item.startswith("✓") for item in choices)
            and any("Clipboard" in item for item in choices) and any("WanGP" in item for item in choices), str(choices))
    r.check("handed in: the intercept can be pointed at Clipboard", menu_click(page, "Send to “Clipboard”") == "clicked")
    time.sleep(2.5)
    # The page's own answer, not a hidden box's: the switch is saved over
    # this tab's route now, so what the menu is acting on is what matters.
    r.check("handed in: and the page knows it is on",
            page.evaluate("() => window.minipaintClipboard.debug().intercept") is True
            and page.evaluate("() => window.minipaintClipboard.debug().interceptTarget") == "clipboard")

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
        menu_click(page, "Intercept Options")
        time.sleep(0.4)
        menu_click(page, "Send to “Mini Paint”")
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
    page.evaluate("() => window.minipaintClipboard.openToolbarMenu('send')")
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
        # Polled rather than slept: the page places the picture itself and
        # then verifies it, and a gallery's upload takes as long as it takes.
        # What is asserted is the outcome, never the latency.
        for _ in range(25):
            time.sleep(1)
            if visible_panel(page) == DESTINATION_PANELS[key]:
                break
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
        page.evaluate("() => window.minipaintClipboard.library({refresh: true, sort: 'name_asc'})")
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
        page.evaluate("() => window.minipaintClipboard.library({refresh: true, sort: 'name_asc'})")
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


def check_the_toolbar_pastes_and_deletes(r: Results, page) -> None:
    """The two verbs this tab is actually used for, one press each.

    A clipboard is a place things pass through: something comes in from
    somewhere else, gets used, and goes. Both of those have always been in
    the menu, which is right for discovering them and wrong for doing them
    forty times in a row. These are the same two actions with the flyout
    taken out of the way.

    Delete has no confirmation on purpose, so the thing that has to be true
    is that it cannot be pressed with nothing selected - the guard moves from
    after the press to before it. That is checked first, because it is the
    only thing standing between a toolbar button and a file that is gone.
    """
    open_clipboard(page)
    disabled = page.evaluate("""() => {
        window.minipaintClipboard.select('');
        const host = document.getElementById('minipaint_clipboard_delete_now');
        const b = host && (host.tagName === 'BUTTON' ? host : host.querySelector('button'));
        return b ? b.disabled : null;
    }""")
    r.check("with nothing selected, the toolbar's Delete cannot be pressed", disabled is True, str(disabled))
    if not select_first(page):
        r.check("toolbar delete: a picture is selected first", False, "no selection")
        return
    enabled = page.evaluate("""() => {
        const host = document.getElementById('minipaint_clipboard_delete_now');
        const b = host && (host.tagName === 'BUTTON' ? host : host.querySelector('button'));
        return b ? b.disabled : null;
    }""")
    r.check("and selecting one enables it", enabled is False, str(enabled))

    before = page.evaluate(TILES_JS)
    doomed = before[0]["name"] if before else ""
    page.evaluate("() => document.getElementById('minipaint_clipboard_delete_now').click()")
    gone = False
    for _ in range(20):
        time.sleep(1)
        now = page.evaluate(TILES_JS)
        if len(now) == len(before) - 1 and all(tile["name"] != doomed for tile in now):
            gone = True
            break
    r.check("one press deletes the selected picture, with nothing to confirm", gone,
            f"{len(before)} -> {len(page.evaluate(TILES_JS))}")
    r.check("and the confirmation panel was never opened",
            not page.evaluate("() => { const p = document.getElementById('minipaint_clipboard_delete_panel');"
                              " return !!(p && getComputedStyle(p).display !== 'none'); }"))
    r.check("the status line says which picture went",
            "Deleted" in page.evaluate("() => { const s = document.getElementById('minipaint_clipboard_status');"
                                       " return s ? s.textContent : ''; }"),
            page.evaluate("() => { const s = document.getElementById('minipaint_clipboard_status');"
                          " return (s ? s.textContent : '').slice(0, 80); }"))
    # Paste reads the system clipboard, which a headless browser has no
    # permission for and this suite must not grant: what is checked is that
    # the button is wired to the page's own paste, not to a server round
    # trip that could not do it anyway.
    r.check("the toolbar's Paste is the page's own clipboard read",
            page.evaluate("() => typeof window.minipaintClipboard.pasteFromClipboard === 'function'"))
    r.check("and it is always pressable, because pasting needs no selection",
            page.evaluate("""() => {
                const host = document.getElementById('minipaint_clipboard_paste_now');
                const b = host && (host.tagName === 'BUTTON' ? host : host.querySelector('button'));
                return b ? b.disabled === false : null;
            }""") is True)


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


HISTORY_LONG = ("Scene: a rainy street at night.\n" + "\n".join(
    f"Beat {index}: she crosses under the neon sign while the rain gets heavier." for index in range(1, 9)))


def check_the_history_button_opens_the_last_prompts(r: Results, page) -> None:
    """Asked for: "the history for the last ten prompts submitted from
    clipboard to wangp. Not the enhanced, but the original raw input. Make
    this viewable for recall by pressing a new button in the right column
    'History' where i can view and load from. It should pop up a list that i
    can scroll, view, and choose to load."
    """
    from minipaint_neo.clipboard import config as clip_config
    from minipaint_neo.clipboard import history, prompts

    ltx23 = {"type": "ltx2_22B_distilled", "label": "LTX-2 2.3 Distilled 1.0 22B", "family": "ltx2", "architecture": "ltx2_22B"}
    ltx25 = {"type": "ltx2_25_22B_distilled", "label": "LTX-2 2.5 Distilled 22B", "family": "ltx2", "architecture": "ltx2_25_22B"}
    stash = clip_config.read_document(clip_config.PROMPTS_NAME, None)
    drawn = """() => {
        const layer = document.querySelector('.minipaint-clip-prompts');
        const dialog = layer ? layer.querySelector('.minipaint-clip-prompts-dialog') : null;
        const list = layer ? layer.querySelector('.minipaint-clip-prompts-list') : null;
        const entries = layer ? [...layer.querySelectorAll('.minipaint-clip-prompts-entry')] : [];
        const text = (i) => entries[i] ? entries[i].querySelector('.minipaint-clip-prompts-text') : null;
        const more = (i) => entries[i] ? entries[i].querySelector('.minipaint-clip-prompts-more') : null;
        const box = dialog ? dialog.getBoundingClientRect() : null;
        return { open: !!layer && !layer.hidden && !!box && box.width > 0,
                 z: layer ? Number(getComputedStyle(layer).zIndex) : 0,
                 role: dialog ? dialog.getAttribute('role') : null, modal: dialog ? dialog.getAttribute('aria-modal') : null,
                 count: entries.length,
                 firstText: text(0) ? text(0).textContent : '',
                 firstClamped: text(0) ? text(0).scrollHeight > text(0).clientHeight + 1 : null,
                 firstExpanded: text(0) ? text(0).dataset.expanded : null,
                 firstMore: more(0) ? (more(0).hidden ? '' : more(0).textContent) : null,
                 secondMore: more(1) ? !more(1).hidden : null,
                 secondText: text(1) ? text(1).textContent : '',
                 meta: entries[0] ? entries[0].querySelector('.minipaint-clip-prompts-meta').textContent : '',
                 scrolls: list ? list.scrollHeight > list.clientHeight + 1 : null,
                 inWindow: !!box && box.top >= 0 && box.bottom <= window.innerHeight + 1,
                 overlays: (window.__historyOverlays || []).slice() }; }"""
    try:
        prompts.reset_for_tests()
        for index in range(11):
            prompts.remember(f"history prompt {index}", model=ltx25 if index % 2 else ltx23,
                             target="ltx25" if index % 2 else "ltx23", enhanced=index % 3 == 0)
        prompts.remember(HISTORY_LONG, model=ltx25, target="ltx25", enhanced=True)
        open_clipboard(page)
        page.evaluate("""() => { window.__historyOverlays = [];
            document.addEventListener('minipaint:overlay', (e) => window.__historyOverlays.push(e.detail)); }""")
        geometry = page.evaluate("""() => {
            const b = document.getElementById('minipaint_clipboard_prompt_history');
            const p = document.getElementById('minipaint_clipboard_prompt');
            const c = document.getElementById('minipaint_clipboard_composer');
            if (!b || !p || !c) { return null; }
            const bb = b.getBoundingClientRect(), pb = p.getBoundingClientRect(), cb = c.getBoundingClientRect();
            return { text: b.textContent.trim(), under: bb.top >= pb.bottom - 1, inside: bb.left >= cb.left - 1 && bb.right <= cb.right + 1,
                     shown: bb.width > 0 && bb.height > 0 }; }""")
        r.check("History is a button on the right column, under the Prompt box",
                geometry is not None and geometry["text"] == "History" and geometry["shown"] and geometry["under"] and geometry["inside"],
                str(geometry))
        page.locator("#minipaint_clipboard_prompt_history").click()
        page.wait_for_function("() => document.querySelectorAll('.minipaint-clip-prompts-entry').length > 0", timeout=10000)
        page.wait_for_timeout(300)
        state = page.evaluate(drawn)
        r.check("it pops up a dialog, over everything else on the page",
                state["open"] and state["role"] == "dialog" and state["modal"] == "true" and state["z"] >= 2000 and state["inWindow"], str(state))
        r.check("listing the last ten, newest first, as typed - line breaks and all",
                state["count"] == 10 and state["firstText"] == HISTORY_LONG and state["secondText"] == "history prompt 10", str(state)[:300])
        r.check("each says when, for which model, and whether it was enhanced",
                "UTC" in state["meta"] and "LTX-2 2.5 Distilled 22B" in state["meta"] and "enhanced" in state["meta"], state["meta"])
        r.check("a long prompt is clamped with More beside it, and a short one has no More",
                state["firstClamped"] is True and state["firstMore"] == "More" and state["secondMore"] is False, str(state))
        # A list that scrolls: in a window shorter than the ten entries, the
        # list itself takes a scroll and the dialog stays inside the window.
        # Measured in a short window rather than the suite's own, because
        # whether ten short prompts overflow 950 pixels is a question about
        # the fonts the browser happens to have - and on the CI runner's they
        # fit, so a check that waited for them not to never passed there.
        tall = page.viewport_size or {"width": 1400, "height": 950}
        page.set_viewport_size({"width": tall["width"], "height": 480})
        page.wait_for_timeout(300)
        short = page.evaluate("""() => {
            const layer = document.querySelector('.minipaint-clip-prompts');
            const list = layer.querySelector('.minipaint-clip-prompts-list');
            const box = layer.querySelector('.minipaint-clip-prompts-dialog').getBoundingClientRect();
            const page = document.scrollingElement ? document.scrollingElement.scrollTop : 0;
            list.scrollTop = 0;
            list.scrollTop = list.scrollHeight;
            const moved = list.scrollTop;
            const after = document.scrollingElement ? document.scrollingElement.scrollTop : 0;
            list.scrollTop = 0;
            return { overflows: list.scrollHeight > list.clientHeight + 1, moved: moved,
                     inWindow: box.top >= 0 && box.bottom <= window.innerHeight + 1, pageMoved: after !== page }; }""")
        page.set_viewport_size(tall)
        page.wait_for_timeout(300)
        r.check("and in a window too short for the list, the list scrolls inside the dialog and the dialog stays in the window",
                short["overflows"] and short["moved"] > 0 and short["inWindow"] and not short["pageMoved"], str(short))
        r.check("the dialog says it has taken the page, so the Forge Assistant puts its panel away",
                state["overlays"] == [{"name": "clipboard-history", "open": True, "modal": True}], str(state["overlays"]))

        page.locator(".minipaint-clip-prompts-entry").first.locator(".minipaint-clip-prompts-more").click()
        state = page.evaluate(drawn)
        r.check("More shows the whole prompt, and becomes Less",
                state["firstExpanded"] == "1" and state["firstClamped"] is False and state["firstMore"] == "Less", str(state))

        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        state = page.evaluate(drawn)
        r.check("Escape closes it, and it says it has given the page back",
                not state["open"] and state["overlays"][-1] == {"name": "clipboard-history", "open": False, "modal": True}, str(state["overlays"]))

        page.locator("#minipaint_clipboard_prompt_history").click()
        page.wait_for_function("() => document.querySelectorAll('.minipaint-clip-prompts-entry').length === 10", timeout=10000)
        page.locator(".minipaint-clip-prompts-entry").nth(1).locator(".minipaint-clip-prompts-load").click()
        page.wait_for_function("""() => { const box = document.querySelector('#minipaint_clipboard_prompt textarea');
            return !!box && box.value === 'history prompt 10'; }""", timeout=10000)
        state = page.evaluate(drawn)
        r.check("Load puts those words in the Prompt box, and the dialog gets out of the way", not state["open"], str(state))
        r.check("and the server's draft holds the same words, so the gallery popup and a reload agree",
                history.load_draft()["prompt_override"] == "history prompt 10")

        page.locator("#minipaint_clipboard_prompt_history").click()
        page.wait_for_function("() => window.minipaintClipboard.debug().promptHistory.open === true", timeout=8000)
        size = page.viewport_size or {"width": 1400, "height": 950}
        page.mouse.click(8, size["height"] - 8)
        page.wait_for_timeout(200)
        r.check("a press outside the dialog closes it too", page.evaluate("() => window.minipaintClipboard.debug().promptHistory.open") is False)
    finally:
        if stash is None:
            clip_config.path_of(clip_config.PROMPTS_NAME).unlink(missing_ok=True)
        else:
            clip_config.write_document(clip_config.PROMPTS_NAME, stash)
        history.save_draft(history.empty_draft())
        page.evaluate("""() => { const box = document.querySelector('#minipaint_clipboard_prompt textarea');
            if (box && window.minipaintWriteInput) { window.minipaintWriteInput(box, ''); } }""")


def check_the_section_is_blocked_until_wangp_is_ready(r: Results, page) -> None:
    """The WanGP section exists only while WanGP can take what it makes.

    The readiness answer is the server's (``targets``); this is what the page
    draws from it on a real page: nothing that sends while it says not ready -
    no cards, no prompt, no enhancement button, no Add to Queue, no +First,
    +Last or +Ref - and one quiet line instead; no reference card for LTX 2.3
    Distilled; and everything back the moment it says ready again.
    """
    from minipaint_neo.clipboard import enhance, targets

    open_clipboard(page)
    wan = {"type": "t2v_2_2", "label": "Wan2.2 Text2Video 14B", "family": "wan", "architecture": "t2v_2_2"}
    blocked = targets._blocked("TARGET_UNSUPPORTED", enhance.model_block(wan),
                               message="WanGP is on Wan2.2 Text2Video 14B. This sends only to MiniMax H3 FL2VA, MiniMax H3 Ref2VA or LTX 2.3 Distilled; choose one in the WanGP tab.")
    shown = """() => {
        const root = document.getElementById('minipaint_clipboard_root');
        const visible = (id) => { const e = document.getElementById(id); if (!e) { return null; }
            const s = getComputedStyle(e); return s.display !== 'none' && s.visibility !== 'hidden' && e.getClientRects().length > 0; };
        const line = document.querySelector('#minipaint_clipboard_blocked .minipaint-clip-blocked');
        return { ready: root ? root.dataset.wangpReady : null, target: root ? root.dataset.wangpTarget : null,
                 cards: visible('minipaint_clipboard_cards'), prompt: visible('minipaint_clipboard_prompt'),
                 enhance: visible('minipaint_clipboard_sp_open'), queue: visible('minipaint_clipboard_queue'),
                 history: visible('minipaint_clipboard_prompt_history'),
                 references: root ? root.dataset.wangpReferences : null,
                 first: visible('minipaint_clipboard_to_first'), ref: visible('minipaint_clipboard_to_ref'),
                 auto: visible('minipaint_clipboard_auto'),
                 cardFirst: visible('minipaint_clipboard_card_first'), cardRef: visible('minipaint_clipboard_card_ref'),
                 outbox: visible('minipaint_clipboard_outbox_list'), blocked: visible('minipaint_clipboard_blocked'),
                 text: line ? line.textContent : '', size: line ? getComputedStyle(line).fontSize : '' }; }"""
    try:
        targets.use_readiness(lambda model=None, fresh=False: blocked)
        page.evaluate("() => window.minipaintClipboard.refreshReadiness(true)")
        time.sleep(1.0)
        state = page.evaluate(shown)
        r.check("blocked: no cards, prompt, History, enhancement button, Add to Queue, +First / +Ref or Auto",
                state["ready"] == "0" and not any(state[key] for key in ("cards", "prompt", "history", "enhance", "queue", "first", "ref", "auto")), str(state))
        r.check("one quiet line in their place, with the reason and Check again, at a hint's size",
                state["blocked"] and "Wan2.2 Text2Video 14B" in state["text"] and "Check again" in state["text"]
                and state["size"] == "12px", str(state))
        r.check("and the queue - the record of what was sent - stays", state["outbox"] is True, str(state))
        ltx = {"type": "ltx2_22B_distilled", "label": "LTX-2 2.3 Distilled 1.0 22B", "family": "ltx2", "architecture": "ltx2_22B"}
        ready = targets._view(True, "", "Ready.", enhance.model_block(ltx), "ltx23", checked=True)
        targets.use_readiness(lambda model=None, fresh=False: ready)
        page.locator("#minipaint_clipboard_blocked .minipaint-clip-blocked-check").click()
        time.sleep(1.0)
        state = page.evaluate(shown)
        r.check("Check again asks afresh, and a ready answer brings the section back",
                state["ready"] == "1" and state["cards"] and state["prompt"] and state["enhance"] and state["queue"]
                and state["first"] and not state["blocked"], str(state))
        r.check("for LTX 2.3 Distilled: a first-frame card and no reference card, no +Ref",
                state["target"] == "ltx23" and state["cardFirst"] and state["cardRef"] is False and state["ref"] is False
                and state["references"] == "0" and state["history"], str(state))
        r.check("but Auto stays: it puts the picture in the first frame there, and leaves the reference out",
                state["auto"] is True, str(state))
        ltx25 = {"type": "ltx2_25_22B_distilled", "label": "LTX-2 2.5 Distilled 22B", "family": "ltx2", "architecture": "ltx2_25_22B"}
        ready = targets._view(True, "", "Ready.", enhance.model_block(ltx25), "ltx25", checked=True)
        targets.use_readiness(lambda model=None, fresh=False: ready)
        page.evaluate("() => window.minipaintClipboard.refreshReadiness(true)")
        page.wait_for_function("() => document.getElementById('minipaint_clipboard_root').dataset.wangpTarget === 'ltx25'", timeout=8000)
        state = page.evaluate(shown)
        r.check("and for LTX 2.5 Distilled the same: the frame cards, no reference card and no +Ref - from the fields the server lists",
                state["ready"] == "1" and state["target"] == "ltx25" and state["cardFirst"] and state["cardRef"] is False
                and state["ref"] is False and state["references"] == "0", str(state))
    finally:
        targets.use_readiness(targets.always_ready)
        page.evaluate("() => window.minipaintClipboard.refreshReadiness(true)")
        time.sleep(0.8)
    state = page.evaluate(shown)
    r.check("and ready for MiniMax, every card is there again", state["ready"] == "1" and state["cardRef"] and state["ref"], str(state))
    r.check("a ready answer that names no model leaves the reference alone", state["references"] in ("", "1"), str(state))


def check_auto_sends_the_selected_picture(r: Results, page) -> None:
    """Auto: a tap on a picture, a tap on Auto, and the request is queued.

    Asked for: "set auto to FIRST and REFERENCE (prompt and last image would
    is what's set in clip board, and if not set, inherited from wan2gp) ...
    tapping to select and tapping auto. Two taps to add to queue." So on a
    real page: Auto is off until a picture is selected and says where it puts
    one; the menu's Auto Options ticks the roles, several at once, and stays
    open while it does; a press is one ask of the queue route that the server
    stores as a job with the picture in those roles and the composer's prompt;
    the cards stay as they were; and two presses reach the server one after
    the other, in the order made.
    """
    from minipaint_neo.clipboard import config as clip_config
    from minipaint_neo.clipboard import history as clip_history
    from minipaint_neo.clipboard import outbox as clip_outbox

    open_clipboard(page)
    auto_state = """() => { const h = document.getElementById('minipaint_clipboard_auto');
        const b = h && (h.tagName === 'BUTTON' ? h : h.querySelector('button'));
        return b ? { disabled: b.disabled, title: b.title, text: b.textContent.trim(),
                     shown: getComputedStyle(b).display !== 'none' && b.getClientRects().length > 0 } : null; }"""
    items = """() => Array.from(document.querySelectorAll('.minipaint-clip-menu [role=menuitemcheckbox]'))
        .map(b => [b.textContent.replace('\u2713', '').trim(), b.getAttribute('aria-checked')])"""

    def settle_jobs():
        for job in clip_outbox.jobs():
            if job["state"] == "pending":
                clip_outbox.cancel(job["job_id"])
            elif job["state"] in ("failed", "cancelled", "unconfirmed") and not job.get("dismissed"):
                try:
                    clip_outbox.dismiss(job["job_id"])
                except Exception:
                    pass

    presses = []

    def pressed(request):
        if request.method == "POST" and "/minipaint-clipboard/queue" in request.url:
            try:
                presses.append(("sent", json.loads(request.post_data or "{}")))
            except ValueError:
                presses.append(("sent", {}))

    def answered(request):
        if request.method == "POST" and "/minipaint-clipboard/queue" in request.url:
            try:
                presses.append(("answered", json.loads(request.post_data or "{}")))
            except ValueError:
                presses.append(("answered", {}))

    settle_jobs()
    draft = clip_history.load_draft()
    for key in ("first_asset_id", "last_asset_id"):
        draft[key] = ""
    draft["reference_asset_ids"] = []
    clip_history.save_draft(draft)
    page.on("request", pressed)
    page.on("requestfinished", answered)
    try:
        page.evaluate("() => window.minipaintClipboard.select('')")
        state = page.evaluate(auto_state)
        r.check("the toolbar has Auto, off while no picture is selected",
                bool(state) and state["text"] == "Auto" and state["shown"] and state["disabled"] is True, str(state))
        picked = _select_tile(page, "square.png")
        state = page.evaluate(auto_state)
        r.check("and on once one is, saying where it puts the picture",
                bool(picked) and state["disabled"] is False
                and state["title"] == "Send the selected picture to WanGP as First Frame and Reference (Menu → Auto Options)", str(state))

        page.evaluate("() => window.minipaintClipboard.toggleMenu()")
        r.check("the menu has Auto Options", menu_click(page, "Auto Options") == "clicked")
        listed = page.evaluate(items)
        r.check("three roles, First Frame and Reference ticked until changed",
                listed == [["First Frame", "true"], ["Last Frame", "false"], ["Reference", "true"]], str(listed))
        menu_click(page, "Last Frame")
        page.wait_for_function("() => (window.minipaintClipboard.debug().auto || []).length === 3", timeout=8000)
        listed = page.evaluate(items)
        r.check("a press ticks one more and leaves the list open, and the host keeps it",
                listed == [["First Frame", "true"], ["Last Frame", "true"], ["Reference", "true"]]
                and page.evaluate("() => window.minipaintClipboard.debug().menuOpen") is True
                and clip_config.load().auto_roles == ["first_frame", "last_frame", "reference"], str(listed))
        r.check("and the tooltip follows", page.evaluate(auto_state)["title"].startswith(
            "Send the selected picture to WanGP as First Frame, Last Frame and Reference"), page.evaluate(auto_state)["title"])
        menu_click(page, "Last Frame")
        page.wait_for_function("() => (window.minipaintClipboard.debug().auto || []).length === 2", timeout=8000)
        r.check("pressed again it is unticked", clip_config.load().auto_roles == ["first_frame", "reference"]
                and page.evaluate(items)[1] == ["Last Frame", "false"], str(page.evaluate(items)))
        page.keyboard.press("Escape")

        page.fill("#minipaint_clipboard_prompt textarea", "a slow push in")
        page.evaluate("""() => {
            window.__toasts = [];
            const root = document.getElementById('minipaint_clipboard_root');
            const seen = () => { const t = root.querySelector('.minipaint-clip-toast');
                const text = t && !t.hidden ? t.textContent : '';
                if (text && window.__toasts[window.__toasts.length - 1] !== text) { window.__toasts.push(text); } };
            window.__toastWatch = new MutationObserver(seen);
            window.__toastWatch.observe(root, { subtree: true, childList: true, characterData: true, attributes: true }); }""")
        before = len(clip_outbox.jobs())
        del presses[:]
        page.evaluate("""() => { const h = document.getElementById('minipaint_clipboard_auto');
            const b = h && (h.tagName === 'BUTTON' ? h : h.querySelector('button')); if (b) { b.click(); } }""")
        # Waited on inside Playwright, not with time.sleep: the request
        # listeners only run while this thread is in a Playwright call.
        for _ in range(60):
            if len(clip_outbox.jobs()) > before:
                break
            page.wait_for_timeout(100)
        page.wait_for_timeout(300)
        job = clip_outbox.jobs()[-1] if len(clip_outbox.jobs()) > before else {}
        images = (job.get("request") or {}).get("images") or {}
        sent = [body for kind, body in presses if kind == "sent" and body.get("action") == "auto"]
        r.check("Auto is one ask of the queue route, with the selected picture and the prompt as typed",
                len(sent) == 1 and sent[0].get("picture") == picked and sent[0].get("prompt") == "a slow push in", str(sent)[:200])
        r.check("which the server stores as a job with that picture as the first frame and the reference",
                images.get("start") == {"kind": "clipboard_asset", "id": picked}
                and images.get("references") == [{"kind": "clipboard_asset", "id": picked}]
                and (job.get("request") or {}).get("prompt") == "a slow push in" and "end" not in images, json.dumps(job.get("request"))[:240])
        after = clip_history.load_draft()
        r.check("and the composer's cards are as they were", after["first_asset_id"] == "" and after["reference_asset_ids"] == [], json.dumps(after))
        try:
            page.wait_for_function("() => (window.__toasts || []).some(t => t.indexOf('Auto: square.png as First Frame and Reference.') === 0)", timeout=8000)
            told = True
        except Exception:
            told = False
        r.check("the page says which picture went where, over the browser where the tap was",
                told, str(page.evaluate("() => window.__toasts")))

        # Two pictures in a row: the second waits for the first's answer.
        ids = page.evaluate("""() => Array.from(document.querySelectorAll('#minipaint_clipboard_root .minipaint-clip-item'))
            .filter(t => ['tall.png', 'wide.png'].indexOf(t.dataset.name) !== -1)
            .sort((a, b) => a.dataset.name < b.dataset.name ? -1 : 1).map(t => t.dataset.asset)""")
        before = len(clip_outbox.jobs())
        del presses[:]
        page.evaluate("""ids => { const c = window.minipaintClipboard;
            c.select(ids[0], true); const one = c.autoQueue('', false);
            c.select(ids[1], true); const two = c.autoQueue('', false);
            return Promise.all([one, two]).then(answers => answers.map(a => !!(a && a.ok))); }""", ids)
        for _ in range(60):
            if len(clip_outbox.jobs()) >= before + 2:
                break
            page.wait_for_timeout(100)
        page.wait_for_timeout(300)
        order = [(kind, body.get("picture")) for kind, body in presses if body.get("action") == "auto"]
        stored = [((job.get("request") or {}).get("images") or {}).get("start", {}).get("id") for job in clip_outbox.jobs()[before:]]
        r.check("two quick Autos reach the server one after the other: the second is sent once the first is answered",
                len(ids) == 2 and order == [("sent", ids[0]), ("answered", ids[0]), ("sent", ids[1]), ("answered", ids[1])], str(order))
        r.check("and are stored in the order they were pressed", stored == ids, f"{stored} {ids}")
    finally:
        page.remove_listener("request", pressed)
        page.remove_listener("requestfinished", answered)
        page.evaluate("() => { if (window.__toastWatch) { window.__toastWatch.disconnect(); } }")
        page.fill("#minipaint_clipboard_prompt textarea", "")
        clip_config.update(auto_roles=list(clip_config.DEFAULT_AUTO_ROLES))
        draft = clip_history.load_draft()
        draft["prompt_override"] = ""
        clip_history.save_draft(draft)
        page.evaluate("() => window.minipaintClipboard.closeMenu()")
        settle_jobs()


def check_a_press_says_when_the_settings_in_view_were_not_used(r: Results, page) -> None:
    """The settings in view: a press whose job will not run at them says so.

    Asked for: "I want all of my send to wan2gp to use the settings that are
    in view!" A job built from a save WanGP made at the press runs at them,
    and the page says no more than it did; one that falls back - WanGP did
    not save its form in time, was loading a model's settings, or could not
    be asked at the press - is said out loud, as the notice left on screen
    and marked, by both of the tab's presses. The server's answers are
    stood in for here; which answer a job gets is the outbox's, and its
    checks hold every row.
    """
    open_clipboard(page)
    warning = ("Not the settings on WanGP's screen: this job runs at WanGP's last saved settings "
               "(its form was not saved at send)")
    refusal = "WanGP would not generate this with the settings on its screen: You must provide a Start Image"
    asked = []

    def answer(route, request):
        if request.method != "POST":
            route.continue_()
            return
        try:
            body = json.loads(request.post_data or "{}")
        except ValueError:
            body = {}
        asked.append(body.get("action"))
        if body.get("prompt") == "refuse me":
            # WanGP's own check would not take the job: nothing stored.
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "ok": False, "code": "WANGP_VALIDATION_REFUSED", "instruction": None,
                "status": refusal + " <small>nothing was stored; change the settings in the WanGP tab and press again</small>"}))
            return
        warned = body.get("prompt") == "warn me"
        settings = ({"source": "last_saved_no_answer", "sentence": "WanGP's last saved settings (its form was not saved at send)",
                     "in_view": False, "warning": warning} if warned else
                    {"source": "saved_at_send", "sentence": "saved from the WanGP page at send", "in_view": True, "warning": ""})
        status = ("Auto: square.png as First Frame and Reference. " if body.get("action") == "auto" else "") \
            + "Queued on the server. <small>it goes next</small>"
        mark = 7000 + len(asked)
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "ok": True, "status": status, "settings": settings,
            "instruction": {"nonce": "%08x" % mark, "job_id": "%032x" % mark, "executor": "server", "state": "admitted"}}))

    shown = """() => { const t = document.querySelector('#minipaint_clipboard_root .minipaint-clip-toast');
        return t && !t.hidden ? [t.textContent, t.classList.contains('minipaint-clip-toast-failure')] : null; }"""
    page.route("**/minipaint-clipboard/queue*", answer)
    try:
        picked = _select_tile(page, "square.png")
        page.evaluate("() => window.minipaintClipboard.addToQueue('in view', false)")
        in_view = page.evaluate(shown)
        page.evaluate("() => window.minipaintClipboard.addToQueue('warn me', false)")
        added = page.evaluate(shown)
        page.evaluate("() => window.minipaintClipboard.autoQueue('warn me', false)")
        auto = page.evaluate(shown)
        page.evaluate("() => window.minipaintClipboard.autoQueue('in view', false)")
        auto_in_view = page.evaluate(shown)
        page.evaluate("() => window.minipaintClipboard.addToQueue('refuse me', false)")
        add_refused = page.evaluate(shown)
        page.evaluate("() => window.minipaintClipboard.autoQueue('refuse me', false)")
        auto_refused = page.evaluate(shown)
        r.check("the presses reached the queue route, Add to Queue's and Auto's",
                bool(picked) and asked == ["add", "add", "auto", "auto", "add", "auto"], str(asked))
        r.check("a press WanGP's check refused leaves WanGP's reason as the notice, marked - Add to Queue's and Auto's",
                add_refused == [refusal, True] and auto_refused == [refusal, True], str((add_refused, auto_refused)))
        r.check("a press whose job runs at the settings on WanGP's screen says what it always said, unmarked",
                in_view == ["Sent to WanGP. It is in Queue Send History.", False]
                and auto_in_view == ["Auto: square.png as First Frame and Reference. Queued on the server.", False],
                str((in_view, auto_in_view)))
        r.check("Add to Queue whose job will not run at them leaves the warning as the notice, marked",
                added == [warning + ".", True], str(added))
        r.check("and Auto says where the picture went and the warning together, marked",
                auto == ["Auto: square.png as First Frame and Reference. Queued on the server. " + warning + ".", True], str(auto))
    finally:
        page.unroute("**/minipaint-clipboard/queue*")
        page.evaluate("() => window.minipaintClipboard.select('')")


def check_wangp_settings_is_a_choice_in_the_menu(r: Results, page) -> None:
    """Menu → WanGP Settings: where every WanGP job's settings come from.

    Asked for once it had gone - "I didn't know this was a setting. I couldn't
    find it. So I actually want to bring the setting back so I can have the
    old style" - and put where the presses that use it are, beside Auto
    Options: what the WanGP tab shows (the default), or WanGP's saved settings
    for the model, or its defaults. A choice is saved over the tab's route,
    said where the press was, and the page told at once with one snapshot, so
    the WanGP tab stops (or starts) saving its form without a reload.
    """
    from minipaint_neo.clipboard import config as clip_config
    from minipaint_neo.clipboard import outbox as clip_outbox

    open_clipboard(page)
    radios = """() => Array.from(document.querySelectorAll('.minipaint-clip-menu [role=menuitemradio]'))
        .map(b => [b.textContent.replace('\u2713', '').trim(), b.getAttribute('aria-checked')])"""
    synced = []

    def heard(request):
        if "/minipaint-interop/sync" in request.url:
            synced.append(request.url)

    clip_outbox.use_inherit(None)
    page.on("request", heard)
    try:
        page.evaluate("() => window.minipaintClipboard.toggleMenu()")
        listed = page.evaluate("""() => Array.from(document.querySelectorAll('.minipaint-clip-menu .minipaint-clip-menu-item'))
            .map(b => b.textContent.trim())""")
        r.check("the menu has WanGP Settings, right after Auto Options",
                "WanGP Settings ›" in listed and listed.index("WanGP Settings ›") == listed.index("Auto Options ›") + 1, str(listed))
        r.check("which opens", menu_click(page, "WanGP Settings") == "clicked")
        r.check("on two choices, what the WanGP tab shows ticked until changed",
                page.evaluate(radios) == [["What the WanGP tab shows", "true"], ["WanGP's saved settings, or the defaults", "false"]],
                str(page.evaluate(radios)))
        before = len(synced)
        r.check("choosing WanGP's saved settings is one press", menu_click(page, "WanGP's saved settings") == "clicked")
        page.wait_for_function("() => window.minipaintClipboard.wangpSettings() === 'saved'", timeout=8000)
        for _ in range(40):
            if len(synced) > before:
                break
            page.wait_for_timeout(100)
        r.check("which the host keeps, and every later press reads",
                clip_config.load().wangp_settings == clip_config.WANGP_SETTINGS_SAVED and clip_outbox.inherit_settings() is False,
                str(clip_config.load().wangp_settings))
        said = toast(page)
        r.check("and says what it means, where the press was, closing the menu",
                said.startswith("WanGP jobs now take WanGP’s saved settings for the model, or its defaults")
                and page.evaluate("() => window.minipaintClipboard.debug().menuOpen") is False, said)
        r.check("and the page is told at once - one snapshot - so the WanGP tab stops saving its form",
                len(synced) == before + 1, str(synced[before:]))
        page.evaluate("() => window.minipaintClipboard.toggleMenu()")
        menu_click(page, "WanGP Settings")
        r.check("opened again, the tick has moved",
                page.evaluate(radios) == [["What the WanGP tab shows", "false"], ["WanGP's saved settings, or the defaults", "true"]],
                str(page.evaluate(radios)))
        menu_click(page, "What the WanGP tab shows")
        page.wait_for_function("() => window.minipaintClipboard.wangpSettings() === 'in_view'", timeout=8000)
        r.check("and choosing what the WanGP tab shows puts every later press back on it",
                clip_config.load().wangp_settings == clip_config.WANGP_SETTINGS_IN_VIEW and clip_outbox.inherit_settings() is True
                and toast(page).startswith("WanGP jobs now take what the WanGP tab shows"), toast(page))
    finally:
        page.remove_listener("request", heard)
        clip_config.update(wangp_settings=clip_config.WANGP_SETTINGS_IN_VIEW)
        page.evaluate("() => window.minipaintClipboard.closeMenu()")


# --------------------------------------------------------------------------
# PNG Info, archives and the full-screen view
# --------------------------------------------------------------------------

PARAMETERS_TEXT = "a lighthouse at dusk\nSteps: 20, Sampler: Euler a, CFG scale: 7, Seed: 98765"


def _png_with_parameters(path, colour=(40, 120, 200), size=(96, 64), text=None):
    from PIL import Image, PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_text("parameters", text or PARAMETERS_TEXT)
    Image.new("RGB", size, colour).save(path, format="PNG", pnginfo=info)


def _pnginfo_text(page):
    return page.evaluate("() => { const h = document.getElementById('pnginfo_html'); return h ? h.textContent : null; }")


def _png_info_opened(page, timeout=15000):
    """Wait for the send's last step: the PNG Info tab shown.

    PNG Info's own text changes the moment the file lands, and the Clipboard
    opens the tab only after it has proved the delivery - a moment later, and
    longer in a slower browser. A check that went back to the Clipboard on the
    text alone was pulled back to PNG Info by the switch that followed, and the
    Clipboard's buttons were on a hidden tab (CI's headless shell, run 107).
    """
    try:
        page.wait_for_function("""() => { const p = document.getElementById('tab_pnginfo');
            return !!p && getComputedStyle(p).display !== 'none'; }""", timeout=timeout)
        return True
    except Exception:
        return False


def _select_tile(page, name):
    """Select the tile showing this name, and wait for the server to hear it."""
    found = page.evaluate("""n => {
        const tile = Array.from(document.querySelectorAll('#minipaint_clipboard_root .minipaint-clip-item'))
            .find(t => t.dataset.name === n);
        if (!tile) { return ''; }
        if (!tile.classList.contains('minipaint-clip-selected')) { tile.click(); }
        return tile.dataset.asset;
    }""", name)
    if not found:
        return ""
    for _ in range(40):
        if box(page, "minipaint_clipboard_selected") == found:
            break
        time.sleep(0.1)
    return found


def check_a_picture_sends_to_png_info(r: Results, page, library) -> None:
    """Asked for: "send to PNG info" from the Clipboard. PNG Info reads the
    parameters Forge wrote into the file, so it is handed the file's own
    bytes over the upload path - a re-encoded picture would arrive with none."""
    open_clipboard(page)
    _png_with_parameters(library / "with-parameters.png")
    page.evaluate("() => window.minipaintClipboard.refreshAll()")
    page.wait_for_function("""() => Array.from(document.querySelectorAll('.minipaint-clip-item'))
        .some(t => t.dataset.name === 'with-parameters.png')""", timeout=15000)
    r.check("PNG Info: the picture with parameters is selected", bool(_select_tile(page, "with-parameters.png")))
    page.evaluate("() => window.minipaintClipboard.openToolbarMenu('send')")
    time.sleep(0.4)
    offered = page.evaluate("""() => Array.from(document.querySelectorAll('.minipaint-clip-menu .minipaint-clip-menu-item'))
        .map(b => b.textContent.trim())""")
    r.check("PNG Info is in Send selection to", "PNG Info" in offered, str(offered))
    r.check("and pressing it sends", menu_click(page, "PNG Info") == "clicked")
    try:
        page.wait_for_function("""() => { const h = document.getElementById('pnginfo_html');
            return !!h && h.textContent.indexOf('Seed: 98765') !== -1; }""", timeout=15000)
        arrived = True
    except Exception:
        arrived = False
    r.check("PNG Info reads the picture's generation parameters - the file arrived as it is",
            arrived, repr(_pnginfo_text(page)))
    r.check("and the PNG Info tab is opened once it has", _png_info_opened(page))
    time.sleep(0.6)
    r.check("and the page says it went", (toast(page) or "").startswith("Sent "), toast(page))
    open_clipboard(page)

    # The permanent shortcut: a double tap on a picture, nothing selected first.
    # Asked for: "give pictures a double tap action that sends them to PNG info".
    _png_with_parameters(library / "double-tap.png", colour=(200, 60, 40),
                         text=PARAMETERS_TEXT.replace("Seed: 98765", "Seed: 24680"))
    page.evaluate("() => window.minipaintClipboard.refreshAll()")
    page.wait_for_function("""() => Array.from(document.querySelectorAll('.minipaint-clip-item'))
        .some(t => t.dataset.name === 'double-tap.png')""", timeout=15000)
    page.evaluate("() => window.minipaintClipboard.select('')")
    tile = page.locator("#minipaint_clipboard_root .minipaint-clip-item[data-name='double-tap.png']")
    r.check("a picture says the double tap is its own, not a zoom",
            page.evaluate("() => getComputedStyle(document.querySelector(\"#minipaint_clipboard_root .minipaint-clip-item[data-name='double-tap.png']\")).touchAction")
            == "manipulation")
    tile.dblclick()
    try:
        page.wait_for_function("""() => { const h = document.getElementById('pnginfo_html');
            return !!h && h.textContent.indexOf('Seed: 24680') !== -1; }""", timeout=15000)
        doubled = True
    except Exception:
        doubled = False
    r.check("a double tap on a picture sends it to PNG Info, nothing selected beforehand", doubled, repr(_pnginfo_text(page)))
    r.check("and opens PNG Info, as the menu's send does", _png_info_opened(page))
    open_clipboard(page)
    r.check("and leaves the picture selected", page.evaluate("() => window.minipaintClipboard.debug().selected")
            == tile.get_attribute("data-asset"))
    for name in ("with-parameters.png", "double-tap.png"):
        try:
            (library / name).unlink()
        except OSError:
            pass
    page.evaluate("() => window.minipaintClipboard.refreshAll()")
    time.sleep(1.0)


ARCHIVE_COUNT = 480


def _seed_archive(folder):
    from PIL import Image

    folder.mkdir(parents=True, exist_ok=True)
    for stale in folder.glob("*"):
        stale.unlink()
    for index in range(ARCHIVE_COUNT):
        shade = (index * 37) % 255
        Image.new("RGB", (48, 36), (shade, 255 - shade, (index * 11) % 255)).save(folder / f"img-{index:04d}.png")
    _png_with_parameters(folder / "img-0000.png")


def _archive(page):
    """The grid's loader, while an archive is the folder on screen."""
    return _grid(page)


def _snapshot(folder):
    return {one.name: (one.stat().st_size, one.stat().st_mtime_ns) for one in sorted(folder.iterdir())}


def check_an_archive_loads_what_is_in_view_first(r: Results, page) -> None:
    """Asked for: archives "on the host", switched to in place of the storage
    folder, "load the entire archive" with no pagination, "only load the place
    holder ... until the user scrolls over it into view", "sequentially" make
    and load the thumbnails of what is in view - and "if [I] start loading
    then scroll, we need to requeue to load what i am seeing first".

    The server is made slow on purpose - the first six thumbnails take 1.5 s
    each, the rest a moment - so there are requests in flight for the top of
    the archive when the grid is flung to the bottom, and still in flight
    300 ms later; what the page has done by then is the measurement. A page
    that only re-decided at the next answer would have done nothing yet.
    """
    from minipaint_neo.clipboard import archives, config as clip_config

    base = pathlib.Path(tempfile.mkdtemp(prefix="minipaint-archive-"))
    folder = base / "Archive of 480"
    _seed_archive(folder)
    before = _snapshot(folder)
    cache = archives.thumbnails()
    real_make = cache._make
    made = {"count": 0}

    def slow_make(*args, **keywords):
        made["count"] += 1
        time.sleep(1.5 if made["count"] <= 6 else 0.05)
        return real_make(*args, **keywords)

    cache._make = slow_make
    requests = []
    listener = lambda request: requests.append(request.url) if "/minipaint-clipboard/archive/image/" in request.url else None
    page.on("request", listener)
    sort_before = clip_config.load().sort
    try:
        open_clipboard(page)
        set_tile_size(page, 72)
        # By name, so a tile's place in the archive is the number in its name.
        page.evaluate("() => window.minipaintClipboard.setSort('name_asc')")
        time.sleep(0.8)
        # Added from the Archives screen, the way a person does it.
        page.evaluate("() => window.minipaintClipboard.openArchives()")
        page.wait_for_selector(".minipaint-clip-archives-path", state="visible", timeout=8000)
        page.fill(".minipaint-clip-archives-path", str(folder))
        page.click(".minipaint-clip-archives-add-button")
        page.wait_for_function("""() => Array.from(document.querySelectorAll('.minipaint-clip-archives-entry'))
            .some(e => e.textContent.indexOf('Archive of 480') !== -1)""", timeout=10000)
        r.check("an archive is added from the Archives screen and listed there",
                len(clip_config.load().archives) == 1 and clip_config.load().view == "", str(clip_config.load().archives))
        r.check("and with an archive to choose, the Menu button names the folder the grid shows",
                page.evaluate(MENU_LABEL_JS) == "☰ Storage folder", repr(page.evaluate(MENU_LABEL_JS)))
        page.click(".minipaint-clip-archives-show")
        page.wait_for_function(f"() => window.minipaintClipboard.gridState().total === {ARCHIVE_COUNT}", timeout=20000)
        state = _archive(page)
        r.check("Show puts the archive in place of the storage folder, every picture listed, no pages",
                state["view"] == clip_config.load().view and state["total"] == ARCHIVE_COUNT
                and page.evaluate("() => !document.querySelector('.minipaint-clip-pager')"), str(state)[:200])
        # No row over the grid for it: the Menu button carries the name and
        # the status line the rest. Asked for: "I don't like that the archive
        # switcher button is now a new row, lets make switching between
        # storage and folder a menu option".
        layout = page.evaluate("""() => {
            const mount = document.querySelector('#minipaint_clipboard_root .minipaint-clip-mount');
            const grid = mount.querySelector('.minipaint-clip-grid');
            return { rows: Array.from(mount.children).filter(c => getComputedStyle(c).position !== 'absolute'
                         && !c.hidden && c.offsetHeight > 0).map(c => c.className),
                     top: Math.round(grid.getBoundingClientRect().top - mount.getBoundingClientRect().top),
                     bar: !!document.querySelector('.minipaint-clip-source') };
        }""")
        r.check("no row over the grid says which folder: the grid is the mount's first row",
                layout["top"] == 0 and not layout["bar"] and layout["rows"][:1] == ["minipaint-clip-grid minipaint-clip-archive-grid"],
                str(layout))
        r.check("the Menu button names the archive instead",
                page.evaluate(MENU_LABEL_JS) == "☰ Archive of 480", repr(page.evaluate(MENU_LABEL_JS)))
        said = page.evaluate("() => document.getElementById('minipaint_clipboard_status').textContent")
        r.check("and the status line says how many, that it is read-only, and where new pictures go",
                "480" in said and "read-only" in said and "storage folder" in said, said)
        page.wait_for_timeout(250)
        state = _archive(page)
        visible_top = list(state["visible"])
        geometry = page.evaluate("""() => {
            const grid = document.querySelector('#minipaint_clipboard_root .minipaint-clip-grid');
            const tiles = Array.from(grid.querySelectorAll('.minipaint-clip-item'));
            return { tiles: tiles.length, withSrc: tiles.filter(t => t.querySelector('img').getAttribute('src')).length,
                     scroll: grid.scrollHeight, view: grid.clientHeight };
        }""")
        r.check("only the rows near the view are in the document, not every tile",
                0 < geometry["tiles"] < ARCHIVE_COUNT / 2 and geometry["scroll"] > geometry["view"] * 3, str(geometry))
        r.check("a tile is a place until its turn: none outside the view has a picture address",
                geometry["withSrc"] <= len(visible_top), str(geometry))
        r.check("the first thumbnails asked for are the ones in view, in reading order",
                state["order"][:3] == visible_top[:3] and len(state["inflight"]) <= 3, str({"order": state["order"][:4], "visible": visible_top[:4]}))

        # Flung to the bottom while the top is still loading.
        started_before = state["started"]
        dropped_before = state["dropped"]
        in_flight = list(state["inflight"])
        page.evaluate("""() => { const g = document.querySelector('#minipaint_clipboard_root .minipaint-clip-grid');
            g.scrollTop = g.scrollHeight; }""")
        page.wait_for_timeout(300)
        after = _archive(page)
        visible_bottom = list(after["visible"])
        r.check("the fling leaves the top of the archive out of view", not (set(visible_bottom) & set(visible_top)),
                str(after["rows"]))
        asked_after = after["order"][-(after["started"] - started_before):] if after["started"] > started_before else []
        r.check("the requests still out for the top were dropped the moment the view moved - not at the next answer",
                after["dropped"] > dropped_before and not (set(after["inflight"]) & set(in_flight)),
                str({"dropped": after["dropped"] - dropped_before, "in_flight_before": len(in_flight), "made": made["count"]}))
        r.check("and the next thumbnails asked for are ones now in view - what you are seeing goes first",
                bool(asked_after) and all(key in set(visible_bottom) for key in asked_after[:3]),
                str({"first": asked_after[:3], "visible": visible_bottom[:3]}))
        page.wait_for_timeout(1500)
        later = _archive(page)
        # What was ever asked for: the top's view and the screen under it,
        # and the bottom's view and the screen over it - nothing between.
        import base64 as _b64
        columns = later["columns"] or 1
        reach = 2 * (len(visible_top) // columns + 1) * columns
        asked = set(state["order"]) | set(later["order"])
        positions = []
        for key in asked:
            name = _b64.urlsafe_b64decode(key + "=" * (-len(key) % 4)).decode("utf-8")
            positions.append(int(name[4:8]))
        passed = [at for at in positions if reach <= at < ARCHIVE_COUNT - reach]
        r.check("and the pictures the fling passed over were never asked for at all",
                positions and not passed, str({"passed": sorted(passed)[:5], "reach": reach}))
        try:
            page.wait_for_function("""() => {
                const s = window.minipaintClipboard.gridState();
                const shown = Array.from(document.querySelectorAll('#minipaint_clipboard_root .minipaint-clip-item'))
                    .filter(t => s.visible.indexOf(t.dataset.key) !== -1);
                return shown.length > 0 && shown.every(t => (t.querySelector('img').getAttribute('src') || '').indexOf('blob:') === 0);
            }""", timeout=30000)
            filled = True
        except Exception:
            filled = False
        r.check("every tile in view gets its thumbnail", filled, str(_archive(page))[:300])
        r.check("over plain requests, a small fraction of the archive", 0 < len(requests) < ARCHIVE_COUNT / 3, str(len(requests)))
        r.check("each carrying the file's version, so the browser may keep it",
                all("v=" in url and "thumb=1" in url for url in requests), str(requests[:2]))
        r.check("and no thumbnail ever made inside the archive: it is exactly as it was",
                _snapshot(folder) == before)

        check_the_scrubber_moves_through_the_archive(r, page)
        check_the_viewer_lands_in_the_archive(r, page)

        # Somewhere in the middle, away and back: the place is kept.
        page.evaluate("""() => { const g = document.querySelector('#minipaint_clipboard_root .minipaint-clip-grid');
            g.scrollTop = Math.round(g.scrollHeight / 2); }""")
        page.wait_for_timeout(400)
        middle_at = page.evaluate("() => document.querySelector('#minipaint_clipboard_root .minipaint-clip-grid').scrollTop")
        page.locator("#tabs > .tab-nav > button", has_text="PNG Info").first.click()
        page.wait_for_timeout(500)
        open_clipboard(page)
        back_at = page.evaluate("() => document.querySelector('#minipaint_clipboard_root .minipaint-clip-grid').scrollTop")
        r.check("leaving the tab and coming back keeps the place in the archive",
                middle_at > 1000 and abs(back_at - middle_at) <= 2, str({"left": middle_at, "back": back_at}))

        # An archive picture, sent to PNG Info, and put on a slot card.
        page.evaluate("""() => { const g = document.querySelector('#minipaint_clipboard_root .minipaint-clip-grid'); g.scrollTop = 0; }""")
        page.wait_for_timeout(600)
        selected = _select_tile(page, "img-0000.png")
        r.check("an archive picture can be selected", selected.startswith("ar-"), selected)
        flags = page.evaluate("""() => {
            const del = document.querySelector('#minipaint_clipboard_delete_now button, button#minipaint_clipboard_delete_now');
            window.minipaintClipboard.toggleMenu();
            const items = Array.from(document.querySelectorAll('.minipaint-clip-menu .minipaint-clip-menu-item'));
            const find = t => items.find(b => b.textContent.indexOf(t) !== -1);
            const out = { del: del ? del.disabled : null, rename: find('Rename') ? find('Rename').disabled : null,
                          remove: find('Delete selected') ? find('Delete selected').disabled : null,
                          archives: !!find('Archives') };
            window.minipaintClipboard.closeMenu();
            return out; }""")
        r.check("it cannot be renamed or deleted: the toolbar's Delete and the menu's two are off",
                flags == {"del": True, "rename": True, "remove": True, "archives": True}, str(flags))
        page.evaluate("() => window.minipaintClipboard.openToolbarMenu('send')")
        time.sleep(0.4)
        r.check("an archive picture goes to PNG Info", menu_click(page, "PNG Info") == "clicked")
        try:
            page.wait_for_function("""() => { const h = document.getElementById('pnginfo_html');
                return !!h && h.textContent.indexOf('Seed: 98765') !== -1; }""", timeout=15000)
            arrived = True
        except Exception:
            arrived = False
        r.check("with its parameters, read straight from the archive's file", arrived, repr(_pnginfo_text(page)))
        r.check("and the send ends on the PNG Info tab", _png_info_opened(page))
        time.sleep(0.5)
        open_clipboard(page)
        storage = pathlib.Path(clip_config.load().storage_root)
        copies_before = len(list(storage.glob("img-0000*.png")))
        page.locator("#minipaint_clipboard_to_first").click()
        try:
            page.wait_for_function("""() => { const s = document.getElementById('minipaint_clipboard_status');
                return !!s && s.textContent.indexOf('copied from the archive') !== -1; }""", timeout=15000)
            said = True
        except Exception:
            said = False
        r.check("+First puts it on the card as a copy in the storage folder, and says so",
                said and len(list(storage.glob("img-0000*.png"))) == copies_before + 1,
                page.evaluate("() => document.getElementById('minipaint_clipboard_status').textContent"))
        page.locator("#minipaint_clipboard_to_first").click()
        time.sleep(1.5)
        r.check("and a second press copies nothing more", len(list(storage.glob("img-0000*.png"))) == copies_before + 1)
        r.check("the archive is still exactly as it was", _snapshot(folder) == before)

        # Back to the storage folder from the menu, the one way there is.
        page.click("#minipaint_clipboard_menu")
        time.sleep(0.3)
        r.check("Menu → Archives › opens the list of folders", menu_click(page, "Archives ›") == "clicked")
        time.sleep(0.3)
        offered = page.evaluate(MENU_ITEMS_JS)
        r.check("which ticks the archive on screen and offers the storage folder",
                "✓ Archive of 480" in offered and "Storage folder" in offered, str(offered))
        r.check("and the storage folder is chosen from it", menu_click(page, "Storage folder") == "clicked")
        page.wait_for_function("() => window.minipaintClipboard.gridState().view === ''", timeout=10000)
        page.wait_for_function("""() => { const t = document.querySelectorAll('#minipaint_clipboard_root .minipaint-clip-grid .minipaint-clip-item');
            return t.length > 0 && Array.from(t).every(x => x.dataset.asset.indexOf('ar-') !== 0); }""", timeout=10000)
        back = page.evaluate("""() => { const grid = document.querySelector('#minipaint_clipboard_root .minipaint-clip-grid');
            const tiles = Array.from(grid.querySelectorAll('.minipaint-clip-item'));
            return { view: document.getElementById('minipaint_clipboard_root').dataset.clipView,
                     tiles: tiles.length, archived: tiles.filter(t => t.dataset.asset.indexOf('ar-') === 0).length,
                     archiveGrid: grid.classList.contains('minipaint-clip-archive-grid'),
                     label: grid.getAttribute('aria-label'),
                     total: window.minipaintClipboard.libraryState().total }; }""")
        r.check("and the storage folder's grid is back: its own listing, drawn the same way, nothing of the archive's left",
                back["view"] == "storage" and back["tiles"] > 0 and back["archived"] == 0 and not back["archiveGrid"]
                and back["label"] == "Clipboard images" and back["total"] == _pictures_in(storage), str(back))
        r.check("with the Menu button saying so", page.evaluate(MENU_LABEL_JS) == "☰ Storage folder",
                repr(page.evaluate(MENU_LABEL_JS)))
        time.sleep(0.4)
        scrub = page.evaluate(SCRUB_JS)
        r.check("the scrubber is drawn only while the grid holds more than it shows",
                scrub["hidden"] == (scrub["overflow"] <= 2), str(scrub))
    finally:
        page.remove_listener("request", listener)
        cache._make = real_make
        try:
            page.evaluate("() => window.minipaintClipboard.setView('')")
        except Exception:
            pass
        for record in list(clip_config.load().archives):
            archives.remove(record["id"])
        set_tile_size(page, 144)
        if sort_before != "name_asc":
            page.evaluate(f"() => window.minipaintClipboard.setSort('{sort_before}')")
            time.sleep(0.8)
        shutil.rmtree(base, ignore_errors=True)
        for copy in pathlib.Path(clip_config.load().storage_root or ".").glob("img-0000*.png"):
            try:
                copy.unlink()
            except OSError:
                pass


MENU_LABEL_JS = "() => { const b = document.getElementById('minipaint_clipboard_menu'); return b ? b.textContent.trim() : ''; }"

#: The scrubber, the grid it lies over and where the grid's scroll stands.
SCRUB_JS = """() => {
    const mount = document.querySelector('#minipaint_clipboard_root .minipaint-clip-mount');
    const grid = mount.querySelector('.minipaint-clip-grid');
    const bar = mount.querySelector('.minipaint-clip-scrubber');
    const g = grid.getBoundingClientRect();
    const cs = getComputedStyle(grid);
    const rect = el => { if (!el) { return null; } const r = el.getBoundingClientRect();
        return {left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height}; };
    const handle = bar ? bar.querySelector('.minipaint-clip-scrubber-handle') : null;
    const bubble = bar ? bar.querySelector('.minipaint-clip-scrubber-bubble') : null;
    const mark = bar ? bar.querySelector('.minipaint-clip-scrubber-mark') : null;
    return {hidden: !bar || bar.hidden, bar: rect(bar), handle: rect(handle), grid: rect(g ? grid : null),
            mark: mark && !mark.hidden ? rect(mark) : null,
            position: bar ? getComputedStyle(bar).position : '',
            innerRight: g.left + parseFloat(cs.borderLeftWidth) + grid.clientWidth,
            paddingRight: parseFloat(cs.paddingRight),
            top: grid.scrollTop, max: grid.scrollHeight - grid.clientHeight, overflow: grid.scrollHeight - grid.clientHeight,
            scrollbar: grid.offsetWidth - grid.clientWidth - parseFloat(cs.borderLeftWidth) - parseFloat(cs.borderRightWidth),
            scrollbarWidth: cs.scrollbarWidth,
            bubble: bubble && !bubble.hidden ? bubble.textContent : '',
            scrubbing: !!(bar && bar.classList.contains('minipaint-clip-scrubbing'))};
}"""


def check_the_viewer_lands_in_the_archive(r: Results, page) -> None:
    """The view steps through the whole archive, and leaving it lands the
    grid on the picture it ended on - rows away from where it was opened."""
    page.evaluate("() => { const g = document.querySelector('#minipaint_clipboard_root .minipaint-clip-grid'); g.scrollTop = 0; }")
    page.wait_for_timeout(500)
    first = page.locator("#minipaint_clipboard_root .minipaint-clip-item[data-name='img-0000.png']")
    first.click(button="right")
    page.wait_for_function(f"() => window.minipaintClipboard.viewerState().count === {ARCHIVE_COUNT}", timeout=10000)
    seen = page.evaluate(VIEWER_JS)
    r.check("in an archive the view holds every picture in it",
            seen["count"] == ARCHIVE_COUNT and seen["current"] == first.get_attribute("data-asset"), str(seen["count"]))
    # Somewhere far from the top of a grid sorted by name.
    page.click(".minipaint-clip-viewer-order[data-order='random']")
    page.wait_for_timeout(200)
    for _ in range(3):
        page.keyboard.press("ArrowRight")
    page.wait_for_timeout(200)
    landing = page.evaluate(VIEWER_JS)["current"]
    page.keyboard.press("Escape")
    page.wait_for_timeout(800)
    state = _archive(page)
    r.check("leaving it selects the picture it ended on and scrolls the archive to it",
            page.evaluate("() => window.minipaintClipboard.debug().selected") == landing and _in_view(page, landing),
            str({"landing": landing, "rows": state["rows"]}))
    r.check("and the thumbnails there are asked for, as after a scroll",
            any(key in landing for key in state["visible"]))
    page.evaluate("() => sessionStorage.removeItem('minipaint-clipboard:viewer-order')")
    page.evaluate("() => { const g = document.querySelector('#minipaint_clipboard_root .minipaint-clip-grid'); g.scrollTop = 0; }")
    page.wait_for_timeout(400)


def check_the_scrubber_moves_through_the_archive(r: Results, page) -> None:
    """A thin bar over the grid's edge moves through thousands of pictures.

    Asked for: "some sort of scrubber / scroll bar mechanism so I can scroll
    the images. I have thousands of images so scrolling one page at a time is
    horrible ... a compact solution that does not take up space or change
    the layout." Dragged by a real mouse: the grid follows the handle, a
    bubble says where the view is, and the archive asks for nothing while it
    is held - only for where it is let go.
    """
    page.evaluate("() => { const g = document.querySelector('#minipaint_clipboard_root .minipaint-clip-grid'); g.scrollTop = 0; }")
    page.wait_for_timeout(1500)
    scrub = page.evaluate(SCRUB_JS)
    r.check("the scrubber is shown over an archive that holds more than the grid shows",
            not scrub["hidden"] and scrub["overflow"] > 1000, str(scrub)[:300])
    r.check("laid over the grid's right-hand edge, inside its frame, out of the layout",
            scrub["bar"] and scrub["position"] == "absolute"
            and scrub["bar"]["right"] <= scrub["innerRight"] + 0.5
            and scrub["bar"]["left"] >= scrub["innerRight"] - 16
            and scrub["bar"]["top"] >= scrub["grid"]["top"] and scrub["bar"]["bottom"] <= scrub["grid"]["bottom"],
            str({k: scrub[k] for k in ("bar", "grid", "innerRight", "position")}))
    r.check("and the grid draws no scroll bar of its own beside it",
            abs(scrub["scrollbar"]) < 0.5 and scrub["scrollbarWidth"] == "none", str((scrub["scrollbar"], scrub["scrollbarWidth"])))
    edge = page.evaluate("""() => {
        const grid = document.querySelector('#minipaint_clipboard_root .minipaint-clip-grid');
        const g = grid.getBoundingClientRect(); const cs = getComputedStyle(grid);
        const end = g.left + parseFloat(cs.borderLeftWidth) + grid.clientWidth - parseFloat(cs.paddingRight);
        const right = Math.max(...Array.from(grid.querySelectorAll('.minipaint-clip-item')).map(t => t.getBoundingClientRect().right));
        return {end: end, right: right};
    }""")
    r.check("the archive's rows are filled to the grid's padding too", abs(edge["right"] - edge["end"]) <= 1, str(edge))
    handle = scrub["handle"]
    r.check("its handle is at the top, as tall as the share in view and never a sliver",
            handle and handle["top"] - scrub["bar"]["top"] <= 1 and 32 <= handle["height"] < scrub["bar"]["height"] / 2,
            str(handle))

    page.mouse.move(handle["left"] + handle["width"] / 2, handle["top"] + handle["height"] / 2)
    page.mouse.down()
    page.wait_for_timeout(50)
    # Counted from the press: a request that finished just before it may
    # start the next one, and that is not the scrubber's.
    before = _archive(page)
    bottom = scrub["bar"]["bottom"] - 2
    for step in range(1, 11):
        y = handle["top"] + handle["height"] / 2 + (bottom - handle["top"] - handle["height"] / 2) * step / 10
        page.mouse.move(handle["left"] + handle["width"] / 2, y)
        page.wait_for_timeout(30)
    held = page.evaluate(SCRUB_JS)
    r.check("dragging the handle to the bottom takes the grid to the end of the archive",
            held["top"] >= held["max"] - 2, str({"top": held["top"], "max": held["max"]}))
    r.check("while it is held a bubble says where the view is: the picture of how many, and its name by this sort",
            held["scrubbing"] and f"of {ARCHIVE_COUNT}" in held["bubble"] and f"{ARCHIVE_COUNT} of" in held["bubble"]
            and "img-0479.png" in held["bubble"], repr(held["bubble"]))
    # On, still held, to the middle: the bottom's thumbnails came with the
    # fling above, so the middle is where nothing has been asked for yet.
    middle = scrub["bar"]["top"] + scrub["bar"]["height"] * 0.55
    for step in range(1, 6):
        page.mouse.move(handle["left"] + handle["width"] / 2, bottom + (middle - bottom) * step / 5)
        page.wait_for_timeout(30)
    during = _archive(page)
    r.check("and nothing new is asked for while it is moving, down to the end and back to the middle",
            during["scrubbing"] and not during["paused"] and during["started"] == before["started"],
            str({"before": before["started"], "during": during["started"]}))
    # Held still, not let go: the view fills in as though it had been.
    # Asked for: "if i pause with out releasing the scrubber, the thumbs in
    # that view should start loading as if i released the scrubber".
    page.wait_for_timeout(700)
    resting = _archive(page)
    rested = resting["order"][-(resting["started"] - during["started"]):] if resting["started"] > during["started"] else []
    r.check("held still without letting go, the thumbnails in view are asked for",
            resting["scrubbing"] and resting["paused"] and rested
            and all(key in set(resting["visible"]) for key in rested[:3]),
            str({"asked": rested[:3], "visible": resting["visible"][:3], "paused": resting["paused"]}))
    during = resting
    page.mouse.up()
    page.wait_for_timeout(600)
    after = _archive(page)
    let_go = page.evaluate(SCRUB_JS)
    asked = after["order"][-(after["started"] - during["started"]):] if after["started"] > during["started"] else []
    # What was asked for since it came to rest: the screen it stopped on.
    asked = rested + asked
    r.check("let go, the bubble is put away and the screen it stopped on is asked for",
            not let_go["bubble"] and not after["scrubbing"] and asked and all(key in set(after["visible"]) for key in asked[:3]),
            str({"asked": asked[:3], "visible": after["visible"][:3], "bubble": let_go["bubble"]}))

    # A press on the bar away from the handle jumps there: from the top, so
    # the view was nowhere near the middle before the press.
    page.evaluate("() => { const g = document.querySelector('#minipaint_clipboard_root .minipaint-clip-grid'); g.scrollTop = 0; }")
    page.wait_for_timeout(400)
    bar = page.evaluate(SCRUB_JS)["bar"]
    page.mouse.click(bar["left"] + bar["width"] / 2, bar["top"] + bar["height"] / 2)
    page.wait_for_timeout(400)
    jumped = page.evaluate(SCRUB_JS)
    r.check("a press anywhere else on the bar jumps the view there",
            0.35 * jumped["max"] <= jumped["top"] <= 0.65 * jumped["max"], str({"top": jumped["top"], "max": jumped["max"]}))
    # The wheel still scrolls the grid, and the handle follows it.
    grid = jumped["grid"]
    page.mouse.move(grid["left"] + grid["width"] / 2, grid["top"] + grid["height"] / 2)
    page.mouse.wheel(0, 900)
    page.wait_for_timeout(500)
    wheeled = page.evaluate(SCRUB_JS)
    r.check("and the wheel still scrolls the grid, the handle following it",
            wheeled["top"] > jumped["top"] + 300 and wheeled["handle"]["top"] > jumped["handle"]["top"],
            str({"before": jumped["top"], "after": wheeled["top"]}))
    page.evaluate("() => { const g = document.querySelector('#minipaint_clipboard_root .minipaint-clip-grid'); g.scrollTop = 0; }")
    page.wait_for_timeout(400)


def _viewer(page):
    return page.evaluate("""() => {
        const v = document.querySelector('#minipaint_clipboard_root .minipaint-clip-viewer');
        const now = document.fullscreenElement;
        return { open: !!v && !v.hidden, full: !now ? 'nothing' : now === document.documentElement ? 'page' : now === v ? 'viewer' : 'other',
                 src: v ? (v.querySelector('img').getAttribute('src') || '') : '', loaded: v ? v.dataset.loaded : null };
    }""")


def check_the_row_is_filled_per_row(r: Results, page, browser) -> None:
    """The slider counts pictures per row, and every row is filled.

    Asked for: "I want it to slide based on number of images that can fit
    one row without empty space ... By default (per session)". The slider
    used to name a size in steps of 8 px with columns that never stretched,
    so almost every size left empty grid at the right-hand end of each row.
    Now its value is how many pictures a row holds, and the tile size is
    worked out from the row: so at every count the last tile of a row ends
    where the grid's padding begins. And, confirmed with the user, every
    picture keeps its shape and is never cut off - a wide one and a tall one
    are each drawn whole inside their square, touching two of its sides.
    """
    row = page.evaluate(ROW_JS)
    rng = row.get("range") or {}
    r.check("the slider's limits are the counts this width allows, written on the slider itself",
            rng and row["sliderMin"] == rng["min"] and row["sliderMax"] == rng["max"] and rng["min"] <= rng["max"],
            str(row))
    r.check("and they are the counts whose tiles fall between 72 and 320 px",
            rng and (row["room"] - (rng["min"] - 1) * row["gap"]) / rng["min"] - 12 <= 320.5
            and (rng["min"] == 1 or (row["room"] - (rng["min"] - 2) * row["gap"]) / (rng["min"] - 1) - 12 > 320)
            and (row["room"] - (rng["max"] - 1) * row["gap"]) / rng["max"] - 12 >= 71.5
            and (row["room"] - rng["max"] * row["gap"]) / (rng["max"] + 1) - 12 < 72, str(row))
    r.check("the grid draws no scroll bar of its own to take width from the row",
            abs(row["bar"]) < 0.5 and row["scrollbarWidth"] == "none", str((row["bar"], row["scrollbarWidth"])))
    library_tiles = page.evaluate(TILES_JS)
    shapes = sorted({("wide" if t["natW"] > t["natH"] * 1.2 else "tall" if t["natH"] > t["natW"] * 1.2 else "square")
                     for t in library_tiles if t.get("natW")})
    r.check("(the library holds a wide, a tall and a square picture to measure)",
            {"wide", "tall", "square"} <= set(shapes), str(shapes))
    counts = sorted({rng["min"], rng["max"], max(rng["min"], min(rng["max"], 3)), max(rng["min"], min(rng["max"], 5))})
    for count in counts:
        set_slider(page, count)
        row = page.evaluate(ROW_JS)
        tiles = page.evaluate(TILES_JS)
        edges = page.evaluate("""() => {
            const grid = document.querySelector('.minipaint-clip-grid');
            const g = grid.getBoundingClientRect(); const cs = getComputedStyle(grid);
            const inside = g.left + parseFloat(cs.borderLeftWidth) + parseFloat(cs.paddingLeft);
            const end = g.left + parseFloat(cs.borderLeftWidth) + grid.clientWidth - parseFloat(cs.paddingRight);
            const tiles = Array.from(grid.querySelectorAll('.minipaint-clip-item')).map(t => t.getBoundingClientRect());
            const top = tiles.length ? Math.min(...tiles.map(t => t.top)) : 0;
            const first = tiles.filter(t => Math.abs(t.top - top) < 1);
            return {inside: inside, end: end, inRow: first.length,
                    left: first.length ? Math.min(...first.map(t => t.left)) : 0,
                    right: first.length ? Math.max(...first.map(t => t.right)) : 0};
        }""")
        full = len(tiles) >= count
        r.check(f"at {count} per row the slider, the row and the grid's columns agree",
                row["perRow"] == count and row["sliderValue"] == count and row["cols"] == str(count)
                and (not full or edges["inRow"] == count), f"{row} {edges}")
        r.check(f"and the tile size is the row shared out: (width - gaps) / {count} - frame",
                abs(row["thumb"] - ((row["room"] - (count - 1) * row["gap"]) / count - 12)) < 0.05
                and abs(row["thumbVar"] - row["thumb"]) < 0.05, str(row))
        if full:
            r.check(f"so a full row at {count} runs from edge to edge, no empty space at its end",
                    abs(edges["left"] - edges["inside"]) <= 1 and abs(edges["right"] - edges["end"]) <= 1, str(edges))
        r.check(f"and every tile at {count} is the same size",
                len({t["w"] for t in tiles}) == 1 and len({t["h"] for t in tiles}) == 1,
                str(sorted({(t["w"], t["h"]) for t in tiles})))
        page.wait_for_function(
            "() => { const i = document.querySelectorAll('.minipaint-clip-item img');"
            " return i.length && Array.from(i).every(x => x.complete); }", timeout=20000)
        tiles = page.evaluate(TILES_JS)
        whole = []
        for t in tiles:
            if not t.get("natW") or not t.get("paintW"):
                continue
            ratio = t["natW"] / t["natH"]
            drawn = t["paintWf"] / t["paintHf"]
            # Whole: inside its box on both axes. Kept: the drawn shape is the
            # picture's own. Fitted: it touches the box on one axis, so it is
            # as large as it can be without either.
            inside = t["paintW"] <= t["thumbW"] + 1 and t["paintH"] <= t["thumbH"] + 1
            touches = abs(t["paintW"] - t["thumbW"]) <= 1.5 or abs(t["paintH"] - t["thumbH"]) <= 1.5
            whole.append((t["name"][:14], round(ratio, 3), round(drawn, 3), inside, touches, t["fit"]))
        bad = [w for w in whole if abs(w[1] - w[2]) > 0.02 or not w[3] or not w[4] or w[5] != "contain"]
        r.check(f"at {count} per row every picture keeps its shape and is drawn whole, nothing cut off",
                whole and not bad, str(bad or whole[:3]))

    # Per session: the browser tab keeps the count through a reload; a new
    # tab starts filled at the default; nothing is told to the host.
    settings_posts = []
    page.on("request", lambda req: settings_posts.append(req.post_data or "")
            if "/minipaint-clipboard/settings" in req.url and req.method == "POST" else None)
    chosen = max(rng["min"], min(rng["max"], 4))
    if chosen == page.evaluate(ROW_JS)["perRow"]:
        chosen = rng["min"] if chosen != rng["min"] else rng["max"]
    set_slider(page, chosen)
    time.sleep(0.6)
    r.check("a count chosen on the slider is not saved on the host",
            not [body for body in settings_posts if "thumbnail" in body], str(settings_posts[:2]))
    kept = page.evaluate("() => sessionStorage.getItem('minipaint-clipboard:per-row')")
    r.check("it is this browser tab's, for its session", kept == str(chosen), repr(kept))
    page.reload(wait_until="load")
    page.wait_for_selector("#tabs .tab-nav button", timeout=30000)
    time.sleep(2)
    open_clipboard(page)
    time.sleep(1.0)
    after = page.evaluate(ROW_JS)
    r.check("and a reload of the tab keeps it", after["perRow"] == chosen and after["sliderValue"] == chosen, str(after))
    other = browser.new_page(viewport={"width": 1400, "height": 950})
    try:
        other.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
        other.wait_for_selector("#tabs .tab-nav button", timeout=30000)
        time.sleep(2)
        open_clipboard(other)
        time.sleep(1.0)
        fresh = other.evaluate(ROW_JS)
        nearest = min(range(fresh["range"]["min"], fresh["range"]["max"] + 1),
                      key=lambda n: abs((fresh["room"] - (n - 1) * fresh["gap"]) / n - 12 - 144))
        r.check("a new browser tab starts at the count whose tiles come nearest 144 px",
                fresh["perRow"] == nearest and fresh["wanted"] in (0, None), f"{fresh} nearest {nearest}")
    finally:
        other.close()
    # A narrower window: the count is kept while the width allows it, and the
    # row is still filled.
    page.set_viewport_size({"width": 1000, "height": 950})
    time.sleep(1.0)
    narrow = page.evaluate(ROW_JS)
    r.check("a narrower window keeps the chosen count while it still fits, and shares the row again",
            narrow["perRow"] == max(narrow["range"]["min"], min(narrow["range"]["max"], chosen))
            and abs(narrow["thumb"] - ((narrow["room"] - (narrow["perRow"] - 1) * narrow["gap"]) / narrow["perRow"] - 12)) < 0.05,
            str(narrow))
    page.set_viewport_size({"width": 1400, "height": 950})
    time.sleep(1.0)
    page.evaluate("() => sessionStorage.removeItem('minipaint-clipboard:per-row')")


VIEWER_JS = "() => window.minipaintClipboard.viewerState()"


def _in_view(page, asset):
    """Whether the tile for this picture is inside the grid's visible box."""
    return page.evaluate("""a => {
        const grid = document.querySelector('#minipaint_clipboard_root .minipaint-clip-grid');
        const tile = Array.from(grid.querySelectorAll('.minipaint-clip-item')).find(t => t.dataset.asset === a);
        if (!tile) { return false; }
        const g = grid.getBoundingClientRect(), t = tile.getBoundingClientRect();
        return t.top >= g.top - 1 && t.bottom <= g.bottom + 1;
    }""", asset)


def check_the_viewer_browses_the_folder(r: Results, page, library) -> None:
    """A right click opens the full-screen view, and it steps through the folder.

    Asked for: "make right click on a image open the full screen image view",
    and "make full screen image view capable of browser the folder in either
    random or date sorted order. Exiting the view should land that image in
    view in the gallery as if i scrolled to it." Thirteen pictures are added
    with their times a second apart, newest last by name, so the date order
    is known - and a hundred and twenty older ones behind them, so that by
    date the view soon reaches pictures whose rows are far below the top of
    the grid by name, and landing on one has somewhere to scroll to.
    """
    from PIL import Image

    made = []
    future = time.time() + 1000
    for number in range(13):
        path = library / f"browse-{number:02d}.png"
        Image.new("RGB", (60 + number, 40), (number * 15, 90, 160)).save(path)
        os.utime(path, (future + number, future + number))
        made.append(path)
    past = time.time() - 100000
    for number in range(120):
        path = library / f"browse-filler-{number:03d}.png"
        Image.new("RGB", (40, 30), (number % 256, 30, 90)).save(path)
        os.utime(path, (past + number, past + number))
        made.append(path)
    open_clipboard(page)
    show_page(page, {"sort": "name_asc", "refresh": True})
    page.evaluate("() => { window.__menuShown = null; window.addEventListener('contextmenu', e => { window.__menuShown = !e.defaultPrevented; }); }")
    try:
        total = library_state(page)["total"]
        tile = page.locator("#minipaint_clipboard_root .minipaint-clip-item[data-name='browse-06.png']")
        opened = tile.get_attribute("data-asset")
        tile.click(button="right")
        page.wait_for_timeout(500)
        state = _viewer(page)
        r.check("a right click on a picture opens it full screen, and the browser's own menu does not open",
                state["open"] and state["full"] == "viewer" and page.evaluate("() => window.__menuShown") is False,
                str({**state, "menu": page.evaluate("() => window.__menuShown")}))
        r.check("and it is the selection", page.evaluate("() => window.minipaintClipboard.debug().selected") == opened)
        page.wait_for_function(f"() => window.minipaintClipboard.viewerState().count === {total}", timeout=10000)
        seen = page.evaluate(VIEWER_JS)
        pairs = page.evaluate("""() => fetch('/minipaint-clipboard/library?sort=newest', {credentials: 'same-origin'})
            .then(r => r.json()).then(a => a.items.map(i => [i[0], i[1]]))""")
        server = [pair[0] for pair in pairs]
        called = dict(pairs)
        names = [called.get(asset, asset) for asset in seen["list"]]
        r.check("the view holds the whole folder, as the server orders it newest first, by default",
                seen["count"] == total > 100 and seen["order"] == "date" and seen["list"] == server
                and names[:3] == ["browse-12.png", "browse-11.png", "browse-10.png"], str(names[:5]))
        r.check("and opens at the picture pressed", seen["current"] == opened and seen["list"][seen["at"]] == opened,
                str({"at": seen["at"], "current": seen["current"]}))
        from urllib.parse import quote
        either = [seen["list"][at] for at in (seen["at"] - 1, seen["at"] + 1) if 0 <= at < seen["count"]]
        r.check("with the whole pictures either side already being fetched",
                len(seen["ahead"]) == len(either) == 2
                and all(any(quote(asset, safe="") in url and "thumb=1" not in url for url in seen["ahead"]) for asset in either),
                str(seen["ahead"]))

        def now():
            state = page.evaluate(VIEWER_JS)
            return state, called.get(state["current"], state["current"])

        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(200)
        state, name = now()
        r.check("→ steps to the next picture by date", name == "browse-05.png" and state["at"] == seen["at"] + 1, name)
        page.click(".minipaint-clip-viewer-next")
        page.wait_for_timeout(200)
        state, name = now()
        r.check("so does the arrow drawn at its side", name == "browse-04.png", name)
        page.keyboard.press("ArrowLeft")
        page.wait_for_timeout(200)
        state, name = now()
        r.check("← steps back", name == "browse-05.png", name)
        size = page.viewport_size or {"width": 1400, "height": 950}
        page.mouse.move(size["width"] * 0.7, size["height"] / 2)
        page.mouse.down()
        page.mouse.move(size["width"] * 0.45, size["height"] / 2, steps=6)
        page.mouse.move(size["width"] * 0.3, size["height"] / 2, steps=6)
        page.mouse.up()
        page.wait_for_timeout(300)
        state, name = now()
        r.check("a swipe to the left steps forward, and the view stays open", name == "browse-04.png" and _viewer(page)["open"], name)
        r.check("the picture on screen is the one stepped to",
                page.evaluate("() => document.querySelector('.minipaint-clip-viewer').dataset.current") == state["current"]
                and quote(state["current"], safe="") in (_viewer(page)["src"] or ""), _viewer(page)["src"])

        page.click(".minipaint-clip-viewer-order[data-order='random']")
        page.wait_for_timeout(200)
        shuffled = page.evaluate(VIEWER_JS)
        r.check("Random shuffles the same folder, with the picture on screen first",
                shuffled["order"] == "random" and sorted(shuffled["list"]) == sorted(server)
                and shuffled["list"][0] == state["current"] and shuffled["at"] == 0
                and page.evaluate("() => sessionStorage.getItem('minipaint-clipboard:viewer-order')") == "random",
                str(shuffled["list"][:3]))
        page.keyboard.press("ArrowRight")
        page.keyboard.press("ArrowRight")
        page.keyboard.press("ArrowLeft")
        page.keyboard.press("ArrowLeft")
        page.wait_for_timeout(200)
        back = page.evaluate(VIEWER_JS)
        r.check("and stepping back retraces the shuffle",
                back["current"] == state["current"] and back["list"] == shuffled["list"], str(back["at"]))
        page.click(".minipaint-clip-viewer-order[data-order='date']")
        page.wait_for_timeout(200)
        dated = page.evaluate(VIEWER_JS)
        r.check("Date puts it back in date order, where the picture on screen is",
                dated["order"] == "date" and dated["list"] == server and dated["list"][dated["at"]] == state["current"], str(dated["at"]))

        # Out to a picture whose row is not in the document: the grid
        # scrolls to it, as turning to the page that held it used to.
        shown = set(page.evaluate("() => Array.from(document.querySelectorAll('#minipaint_clipboard_root .minipaint-clip-item')).map(t => t.dataset.asset)"))
        top = page.evaluate("() => document.querySelector('#minipaint_clipboard_root .minipaint-clip-grid').scrollTop")
        for _ in range(total):
            if page.evaluate(VIEWER_JS)["current"] not in shown:
                break
            page.keyboard.press("ArrowRight")
            page.wait_for_timeout(60)
        landing = page.evaluate(VIEWER_JS)["current"]
        page.keyboard.press("Escape")
        page.wait_for_timeout(1800)
        r.check("leaving the view selects the picture it ended on",
                not _viewer(page)["open"] and landing not in shown
                and page.evaluate("() => window.minipaintClipboard.debug().selected") == landing, landing)
        landed = page.evaluate("() => document.querySelector('#minipaint_clipboard_root .minipaint-clip-grid').scrollTop")
        r.check("and the grid scrolls to it, rows away from where it was, with it in view",
                landed > top + 300 and _in_view(page, landing), str({"from": top, "to": landed, "rows": _grid(page)["rows"]}))
    finally:
        page.evaluate("() => window.minipaintClipboard.closeViewer()")
        page.evaluate("() => sessionStorage.removeItem('minipaint-clipboard:viewer-order')")
        for path in made:
            try:
                path.unlink()
            except OSError:
                pass
        show_page(page, {"sort": "newest", "refresh": True})
        page.evaluate("() => window.minipaintClipboard.select('')")


def check_a_tile_opens_full_screen(r: Results, page) -> None:
    """Asked for: "a tiny button to the upper right of a large sized thumb,
    that lets me expand to full screen and double tap to exit"."""
    open_clipboard(page)
    page.evaluate("() => window.minipaintClipboard.refreshAll()")
    time.sleep(1.0)
    set_tile_size(page, 96)
    small = page.evaluate("""() => { const e = document.querySelector('#minipaint_clipboard_root .minipaint-clip-expand');
        return e ? getComputedStyle(e).display : 'none-found'; }""")
    r.check("a small tile has no full-screen button", small == "none", small)
    set_tile_size(page, 200)
    corner = page.evaluate("""() => {
        const tile = document.querySelector('#minipaint_clipboard_root .minipaint-clip-item');
        const e = tile.querySelector('.minipaint-clip-expand');
        const t = tile.getBoundingClientRect(), b = e.getBoundingClientRect();
        return { display: getComputedStyle(e).display, top: b.top - t.top, right: t.right - b.right, size: b.width,
                 text: e.textContent, svg: !!e.querySelector('svg') }; }""")
    r.check("a large tile has one, small, in its upper right corner, drawn rather than written",
            corner["display"] == "flex" and 0 <= corner["top"] <= 12 and 0 <= corner["right"] <= 12
            and corner["size"] <= 32 and corner["text"] == "" and corner["svg"], str(corner))
    page.evaluate("""() => {
        window.__fullRequests = [];
        new PerformanceObserver(list => { for (const e of list.getEntries()) {
            if (String(e.name).indexOf('/minipaint-clipboard/image/') !== -1 && String(e.name).indexOf('thumb=1') === -1) {
                window.__fullRequests.push(e.name); } } }).observe({ type: 'resource', buffered: false });
    }""")
    first = page.locator("#minipaint_clipboard_root .minipaint-clip-item").first
    name = first.get_attribute("data-name")
    first.locator(".minipaint-clip-expand").click()
    page.wait_for_timeout(700)
    state = _viewer(page)
    r.check("pressing it opens the picture over everything, in the browser's own full screen",
            state["open"] and state["full"] == "viewer", str(state))
    # The view steps through the folder now, so the whole pictures either
    # side are fetched too - and nothing else in the folder.
    page.wait_for_function("() => window.minipaintClipboard.viewerState().count > 0", timeout=10000)
    page.wait_for_timeout(300)
    seen = page.evaluate("() => window.minipaintClipboard.viewerState()")
    neighbours = {seen["list"][at] for at in (seen["at"] - 1, seen["at"], seen["at"] + 1) if 0 <= at < len(seen["list"])}
    fetched = page.evaluate("() => window.__fullRequests")
    from urllib.parse import unquote
    named = {unquote(url.split("/minipaint-clipboard/image/")[1].split("?")[0]) for url in fetched}
    r.check("the whole picture is loaded for it - and only for it and the two either side",
            state["loaded"] == "1" and "thumb=1" not in state["src"]
            and first.get_attribute("data-asset") in named and named <= neighbours and len(fetched) <= 3,
            str({"named": sorted(named), "neighbours": sorted(neighbours)}))
    r.check("and the tile it came from is the selection",
            page.evaluate("() => window.minipaintClipboard.debug().selected") == first.get_attribute("data-asset"), name)
    size = page.viewport_size or {"width": 1400, "height": 950}
    page.mouse.dblclick(size["width"] / 2, size["height"] / 2)
    page.wait_for_timeout(500)
    state = _viewer(page)
    r.check("a double tap closes it and gives the screen back", not state["open"] and state["full"] == "nothing", str(state))

    # Over the page's own full screen - the Forge Assistant's focus mode.
    page.evaluate("""() => {
        const b = document.createElement('button');
        b.id = 'page-full-screen';
        b.textContent = 'page';
        b.style.cssText = 'position:fixed;left:0;bottom:0;z-index:2147483647';
        b.onclick = () => document.documentElement.requestFullscreen();
        document.body.appendChild(b);
    }""")
    try:
        page.click("#page-full-screen")
        page.wait_for_timeout(400)
        page.locator("#minipaint_clipboard_root .minipaint-clip-item").first.locator(".minipaint-clip-expand").click()
        page.wait_for_timeout(500)
        stacked = _viewer(page)["full"]
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
        state = _viewer(page)
        r.check("over the page's own full screen it stacks, and leaving it leaves the page's where it was",
                stacked == "viewer" and not state["open"] and state["full"] == "page", str({"stacked": stacked, **state}))
    finally:
        page.evaluate("""() => {
            if (document.fullscreenElement) { document.exitFullscreen().catch(() => {}); }
            const b = document.getElementById('page-full-screen');
            if (b) { b.remove(); }
        }""")
        page.wait_for_timeout(300)
        page.evaluate("() => window.minipaintClipboard.closeViewer()")
        set_tile_size(page, 144)


def only_words() -> list:
    """The checks to run alone, while working: ``--only archive,scrubber`` or
    ``MINIPAINT_BROWSER_ONLY=archive,scrubber``, each word matched against
    the checks' names. The adapter's own attach always runs. A check that
    leans on one before it (a picture it added, a size it set) may not hold
    alone; the whole suite is the judge, and CI runs it whole."""
    text = os.environ.get("MINIPAINT_BROWSER_ONLY", "")
    if "--only" in sys.argv:
        at = sys.argv.index("--only")
        text = sys.argv[at + 1] if at + 1 < len(sys.argv) else ""
    return [word.strip() for word in text.split(",") if word.strip()]


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
    # The event spine, which this page now needs rather than merely uses: the
    # grid is told that the library moved and re-asks. Without these routes a
    # page draws once and then quietly stops keeping up, which is the whole
    # failure this suite exists to catch - so the suite serves what Forge
    # serves rather than a subset of it.
    from minipaint_neo import interop as interop_routes
    interop_routes.install(demo.app)
    # The WanGP section's gate, as it was before targets existed: these checks
    # are about the tab, and there is no WanGP here. The gate itself has
    # check_the_section_is_blocked_until_wangp_is_ready, and the whole of its
    # decision test_clipboard_targets.
    from minipaint_neo.clipboard import targets as clip_targets
    clip_targets.use_readiness(clip_targets.always_ready)
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
                steps = [
                    ("check_grid", lambda: check_grid(r, page)),
                    ("check_the_storage_folder_is_one_listing", lambda: check_the_storage_folder_is_one_listing(r, page, library)),
                    ("check_the_storage_folder_loads_what_is_in_view_first",
                     lambda: check_the_storage_folder_loads_what_is_in_view_first(r, page, library)),
                    ("check_patching", lambda: check_patching(r, page, library)),
                    ("check_the_empty_and_unconfigured_notices", lambda: check_the_empty_and_unconfigured_notices(r, page)),
                    ("check_sorting_is_separate_from_drawing", lambda: check_sorting_is_separate_from_drawing(r, page)),
                    ("check_the_failure_modes_have_answers", lambda: check_the_failure_modes_have_answers(r, page, library)),
                    ("check_the_section_is_blocked_until_wangp_is_ready", lambda: check_the_section_is_blocked_until_wangp_is_ready(r, page)),
                    ("check_the_queue_section_is_the_browsers", lambda: check_the_queue_section_is_the_browsers(r, page)),
                    ("check_queue_reads_are_one_at_a_time", lambda: check_queue_reads_are_one_at_a_time(r, page)),
                    ("check_the_prompt_editor_fills_the_window", lambda: check_the_prompt_editor_fills_the_window(r, page)),
                    ("check_the_history_button_opens_the_last_prompts", lambda: check_the_history_button_opens_the_last_prompts(r, page)),
                    ("check_auto_sends_the_selected_picture", lambda: check_auto_sends_the_selected_picture(r, page)),
                    ("check_a_press_says_when_the_settings_in_view_were_not_used",
                     lambda: check_a_press_says_when_the_settings_in_view_were_not_used(r, page)),
                    ("check_wangp_settings_is_a_choice_in_the_menu",
                     lambda: check_wangp_settings_is_a_choice_in_the_menu(r, page)),
                    ("check_the_toolbar_flyouts_are_the_one_door", lambda: check_the_toolbar_flyouts_are_the_one_door(r, page)),
                    ("check_view_outputs_is_a_gallery", lambda: check_view_outputs_is_a_gallery(r, page)),
                    ("check_the_player_is_the_pages", lambda: check_the_player_is_the_pages(r, page)),
                    ("check_the_players_full_screen_is_its_own", lambda: check_the_players_full_screen_is_its_own(r, page)),
                    ("check_an_output_loads_its_recipe", lambda: check_an_output_loads_its_recipe(r, page)),
                    ("check_the_tab_fills_the_window", lambda: check_the_tab_fills_the_window(r, page)),
                    ("check_a_thumbnail_is_fetched_once", lambda: check_a_thumbnail_is_fetched_once(r, page)),
                    ("check_the_row_is_filled_per_row", lambda: check_the_row_is_filled_per_row(r, page, browser)),
                    ("check_a_tile_opens_full_screen", lambda: check_a_tile_opens_full_screen(r, page)),
                    ("check_the_viewer_browses_the_folder", lambda: check_the_viewer_browses_the_folder(r, page, library)),
                    ("check_a_picture_sends_to_png_info", lambda: check_a_picture_sends_to_png_info(r, page, library)),
                    ("check_an_archive_loads_what_is_in_view_first", lambda: check_an_archive_loads_what_is_in_view_first(r, page)),
                    ("select_first", lambda: r.check("a picture can be selected", select_first(page),
                                                     repr(box(page, "minipaint_clipboard_selected")))),
                    ("check_send", lambda: check_send(r, page, targets)),
                    # Tab switching first: the checks below cut Gradio's queue
                    # with page routing, and a reload while a route handler is
                    # in flight is a good way to hang a browser for no reason
                    # that has anything to do with what is being tested.
                    ("check_tab_switching_survives_reordering", lambda: check_tab_switching_survives_reordering(r, page)),
                    ("check_recovers_after_the_interruption", lambda: check_recovers_after_the_interruption(r, page, targets)),
                    ("check_send_survives_a_dead_queue", lambda: check_send_survives_a_dead_queue(r, page, targets)),
                    ("check_a_component_destination_fills_without_the_queue", lambda: check_a_component_destination_fills_without_the_queue(r, page, targets)),
                    ("check_a_render_cannot_take_the_selection", lambda: check_a_render_cannot_take_the_selection(r, page)),
                    ("check_a_send_reaches_a_framework_owned_input", lambda: check_a_send_reaches_a_framework_owned_input(r, page, targets)),
                    ("check_a_send_survives_a_box_the_host_never_hears", lambda: check_a_send_survives_a_box_the_host_never_hears(r, page, targets)),
                    ("check_the_page_can_say_why_a_send_was_silent", lambda: check_the_page_can_say_why_a_send_was_silent(r, page)),
                    ("check_the_notice_takes_itself_down", lambda: check_the_notice_takes_itself_down(r, page)),
                    ("check_a_picture_handed_in_arrives_without_the_queue", lambda: check_a_picture_handed_in_arrives_without_the_queue(r, page, library)),
                    # Before the Send-to contract, because its Delete is a
                    # framework event and the checks below cut that channel.
                    ("check_the_toolbar_pastes_and_deletes", lambda: check_the_toolbar_pastes_and_deletes(r, page)),
                    # Last: the first of these reloads the page to prove no
                    # destination was opened beforehand.
                    ("check_a_destination_is_never_pre_opened", lambda: check_a_destination_is_never_pre_opened(r, page, targets)),
                    ("check_navigation_is_never_what_makes_a_send_work", lambda: check_navigation_is_never_what_makes_a_send_work(r, page, targets)),
                    ("check_a_failed_send_leaves_you_in_clipboard", lambda: check_a_failed_send_leaves_you_in_clipboard(r, page, targets)),
                    ("check_delivery_and_navigation_are_separate_facts", lambda: check_delivery_and_navigation_are_separate_facts(r, page, targets)),
                    ("check_mini_paint_keeps_the_same_contract", lambda: check_mini_paint_keeps_the_same_contract(r, page)),
                    ("check_the_whole_tab_works_with_the_channel_cut", lambda: check_the_whole_tab_works_with_the_channel_cut(r, page, targets)),
                ]
                words = only_words()
                chosen = [name for name, _ in steps if not words or any(word in name for word in words)]
                if words:
                    print(f"browser clipboard: --only {','.join(words)}: {len(chosen)} of {len(steps)} checks: "
                          + ", ".join(chosen))
                    r.check("--only names at least one check", chosen, ",".join(words))
                for name, step in steps:
                    if name in chosen:
                        step()
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
