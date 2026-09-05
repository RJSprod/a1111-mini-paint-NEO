"""Drive the real extension in a real browser, inside a Forge-shaped page.

    FORGE_ROOT=/path/to/forge-neo python tests/browser_smoke.py [--keep] [--port 7860]

Needs Gradio, Pillow, Playwright with a Chromium, and a Forge Neo checkout
in FORGE_ROOT: the Canvas is the host's own canvas, so the host's canvas.js,
canvas.css, script.js and ui.js are loaded into the page exactly as the
WebUI loads them.

Order matters and is the one the tab-bar notes ask for: every top-level tab
must switch before a single Canvas feature is exercised. The whole flow is
then run again with WebGL disabled, where nothing may differ, and once more
with the legacy editor.
"""

from __future__ import annotations

import argparse
import base64
import io
import os
import pathlib
import re
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

from harness import Results, setup_path  # noqa: E402

setup_path()

import gradio as gr  # noqa: E402
from modules import script_callbacks, shared  # noqa: E402
from PIL import Image  # noqa: E402

import forge_like  # noqa: E402
from minipaint_neo import router, settings  # noqa: E402
from minipaint_neo.canvas import host  # noqa: E402


def forge_root() -> pathlib.Path:
    root = os.environ.get("FORGE_ROOT", "")
    path = pathlib.Path(root) if root else None
    if path is None or not (path / "modules_forge" / "forge_canvas" / "canvas.js").exists():
        raise SystemExit("set FORGE_ROOT to a Forge Neo checkout (its modules_forge/forge_canvas/canvas.js is the editor)")
    return path


def head_html() -> str:
    root = forge_root()
    parts = []
    for name in ("script.js", "javascript/ui.js", "modules_forge/forge_canvas/canvas.js"):
        parts.append("<script>" + (root / name).read_text(encoding="utf-8") + "</script>")
    parts.append("<style>" + (root / "modules_forge" / "forge_canvas" / "canvas.css").read_text(encoding="utf-8") + "</style>")
    # The WebUI loads every file in an extension's javascript folder, whichever frontend is mounted.
    for name in ("main.js", "minipaint_canvas.js"):
        parts.append("<script>" + (ROOT / "javascript" / name).read_text(encoding="utf-8") + "</script>")
    parts.append("<style>" + (ROOT / "style.css").read_text(encoding="utf-8") + "</style>")
    return "\n".join(parts)


def sample_image(width=640, height=480):
    image = Image.new("RGB", (width, height), (30, 90, 200))
    for x in range(0, width, 40):
        for y in range(0, height, 40):
            if (x // 40 + y // 40) % 2:
                image.paste((230, 200, 40), (x, y, x + 40, y + 40))
    return image


def build(old_ui: bool):
    shared.opts.data[settings.USE_OLD_UI] = old_ui
    script_callbacks.callbacks["after_component"][:] = [] if old_ui else [host.on_after_component]
    host.reset_capture()
    demo, refs = forge_like.build_host(router.on_ui_tabs, extra_head=head_html())
    return demo, refs


def build_with_generator(old_ui: bool):
    """Same page, plus a Generate button that puts an image in the gallery."""
    made = {}

    shared.opts.data[settings.USE_OLD_UI] = old_ui
    script_callbacks.callbacks["after_component"][:] = [] if old_ui else [host.on_after_component]
    host.reset_capture()

    original = forge_like.output_panel

    def panel_with_generator(tabname, refs):
        original(tabname, refs)
        if tabname == "txt2img":
            made["btn"] = gr.Button("make", elem_id="probe_make")
            made["btn"].click(lambda: [(sample_image(), "sample")], outputs=[refs["txt2img_gallery"]])

    forge_like.output_panel = panel_with_generator
    try:
        demo, refs = forge_like.build_host(router.on_ui_tabs, extra_head=head_html())
    finally:
        forge_like.output_panel = original
    return demo, refs


def decode_data_url(text):
    if not text or not text.startswith("data:image/png;base64,"):
        return None
    return Image.open(io.BytesIO(base64.b64decode(text.split(",", 1)[1])))


def visible_panels(page):
    return page.evaluate("() => Array.from(document.querySelectorAll('#tabs > .tabitem')).filter(i => getComputedStyle(i).display !== 'none').map(i => i.id)")


def click_tab(page, name):
    page.locator("#tabs > .tab-nav > button", has_text=name).first.click()
    time.sleep(0.4)


def check_tabs(r, page, label):
    for name, panel in (("img2img", "tab_img2img"), ("Extras", "tab_extras"), ("Settings", "tab_settings"),
                        ("Extensions", "tab_extensions"), ("Mini Paint", "tab_minipaint"), ("txt2img", "tab_txt2img")):
        click_tab(page, name)
        r.check(f"{label}: tab {name} switches", visible_panels(page) == [panel], str(visible_panels(page)))


def status_text(page):
    return page.evaluate("() => (document.querySelector('#minipaint_canvas_status') || {}).innerText || ''")


def debug(page):
    return page.evaluate("() => window.minipaintCanvas.debug()")


def wait_for(page, predicate, timeout=15.0, step=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


def textarea_value(page, elem_id, klass):
    return page.evaluate(f"() => {{ const t = document.querySelector('#{elem_id}.{klass} textarea'); return t ? t.value : null; }}")


def canvas_layer(page, uuid, klass):
    return decode_data_url(textarea_value(page, uuid, klass))


def drag(page, x1, y1, x2, y2, steps=12):
    page.mouse.move(x1, y1)
    page.mouse.down()
    for i in range(1, steps + 1):
        page.mouse.move(x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps)
        time.sleep(0.02)
    page.mouse.up()


def touch(page, gesture, steps=12):
    """Fingers through the Chrome DevTools protocol (Playwright has no touch drag).

    ``gesture`` is a list of fingers, each (x1, y1, x2, y2)."""
    cdp = page.context.new_cdp_session(page)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": g[0], "y": g[1], "id": i} for i, g in enumerate(gesture)]})
    for step in range(1, steps + 1):
        points = [{"x": g[0] + (g[2] - g[0]) * step / steps, "y": g[1] + (g[3] - g[1]) * step / steps, "id": i} for i, g in enumerate(gesture)]
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": points})
        time.sleep(0.02)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    cdp.detach()


def pick_option(page, elem_id, text):
    """Choose an entry of a Gradio dropdown the way a finger would."""
    page.locator(f"{elem_id} input").first.click()
    page.locator(f"{elem_id} ul li, {elem_id} [role=option], .options li, [role=listbox] [role=option]", has_text=text).first.click()
    time.sleep(0.3)


def pick_chip(page, elem_id, text):
    """Choose one of a Radio's chips by its exact value."""
    page.locator(f"{elem_id} label:has(input[value='{text}'])").first.click()
    time.sleep(0.3)


def tab_fits(page):
    """The Canvas tab ends above the bottom of the window (what the page puts after it is its business)."""
    return page.evaluate("() => document.querySelector('#minipaint_canvas_root').getBoundingClientRect().bottom <= window.innerHeight + 1")


def rail_beside(page):
    """The rail sits to the right of the work column and is no taller than it."""
    return page.evaluate("""() => {
        const w = document.querySelector('#minipaint_canvas_work').getBoundingClientRect();
        const r = document.querySelector('#minipaint_canvas_rail').getBoundingClientRect();
        return r.width > 200 && r.left >= w.right - 1 && r.top < w.bottom && r.bottom <= w.bottom + 1;
    }""")


def menu_open(page):
    page.locator("#minipaint_canvas_menu").click()
    return wait_for(page, lambda: debug(page)["menuOpen"], timeout=5)


def menu_items(page):
    return page.evaluate("() => Array.from(document.querySelectorAll('.minipaint-menu .minipaint-menu-item')).map(b => b.textContent)")


def menu_click(page, text):
    """An item of the open menu by its words, whatever tick or arrow it carries."""
    page.locator(".minipaint-menu .minipaint-menu-item", has_text=re.compile(r"^(\u2713 |\u2039 )?" + re.escape(text) + r"( \u203a|  \u00b7 suggested)?$")).first.click()


def menu_pick(page, *path):
    """Open the menu and follow a path of items, e.g. ("Tools", "Mask")."""
    menu_open(page)
    for step in path:
        menu_click(page, step)
        time.sleep(0.15)


def layer_row(page, name):
    return page.locator(f"#minipaint_canvas_layer_list .minipaint-layer[data-name='{name}']").first


def layer_rows(page):
    return page.evaluate("() => Array.from(document.querySelectorAll('#minipaint_canvas_layer_list .minipaint-layer')).map(r => [r.dataset.name, r.classList.contains('selected'), r.classList.contains('active'), r.classList.contains('hidden-layer')])")


def handle_center(page, corner):
    box = page.locator(f"#minipaint_canvas_surface .minipaint-frame-handle.{corner}").first.bounding_box()
    return (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2) if box else None


def image_box(page, uuid):
    return page.locator(f"#image_{uuid}").bounding_box()


def run_flow(r: Results, page, refs, uuid: str, label: str, with_upload: bool) -> None:
    """Everything the Canvas does, once, from a page that is already open."""
    inpaint = refs["init_img_with_mask"]
    check_tabs(r, page, label)

    click_tab(page, "Mini Paint")
    time.sleep(0.6)
    d = debug(page)
    r.check(f"{label}: the canvas is attached, without an image", d is not None and d["attached"] and not d["hasImage"] and d["frameHidden"], str(d))
    r.check(f"{label}: the canvas took the height the window had left", d["fitting"] and 240 <= d["height"] < 900, str(d["height"]))
    r.check(f"{label}: the whole tab fits the window without scrolling", tab_fits(page), str(page.evaluate("() => [document.querySelector('#minipaint_canvas_root').getBoundingClientRect().bottom, window.innerHeight]")))
    r.check(f"{label}: the rail of panels is beside the canvas, no taller than it", rail_beside(page) and d["rail"] and not d["rail"]["below"] and d["rail"]["maxHeight"] > 0, str(d["rail"]))
    r.check(f"{label}: the crop panel is showing in the rail, the others are not", page.evaluate("() => ['crop', 'mask', 'expand', 'layers'].map(m => document.querySelector('#minipaint_canvas_panel_' + m).getBoundingClientRect().height > 0)") == [True, False, False, False])
    r.check(f"{label}: the action row is one menu button and the status line", page.evaluate("""() => {
        const m = document.querySelector('#minipaint_canvas_menu').getBoundingClientRect();
        const s = document.querySelector('#minipaint_canvas_status').getBoundingClientRect();
        const gone = ['#minipaint_canvas_send', '#minipaint_canvas_mode_pick', '#minipaint_canvas_panels', '#minipaint_canvas_focus', '#minipaint_canvas_options'].every(id => !document.querySelector(id));
        return m.width > 0 && s.left > m.right && Math.abs(m.top + m.height / 2 - (s.top + s.height / 2)) < m.height && gone;
    }"""))
    r.check(f"{label}: the menu button names the tool", page.locator("#minipaint_canvas_menu").inner_text().strip() == "☰ Crop", page.locator("#minipaint_canvas_menu").inner_text())
    r.check(f"{label}: what the menu presses is not shown", page.evaluate("() => ['open', 'undo', 'redo', 'reset', 'save'].every(n => document.getElementById('minipaint_canvas_' + n).getBoundingClientRect().height === 0)"))
    r.check(f"{label}: nothing lies over the canvas", page.evaluate("""() => {
        const box = document.querySelector('#minipaint_canvas_surface .forge-container').getBoundingClientRect();
        const probe = (x, y) => document.elementFromPoint(x, y);
        const inside = (el) => !!el && !!el.closest('#minipaint_canvas_surface');
        return inside(probe(box.left + box.width / 2, box.bottom - 6)) && inside(probe(box.left + 6, box.bottom - 6)) && inside(probe(box.right - 6, box.top + box.height / 2));
    }"""))

    # ---- the menu: its lists, and the three ways out ----
    r.check(f"{label}: the menu opens under its button", menu_open(page) and page.evaluate("() => { const b = document.querySelector('#minipaint_canvas_menu').getBoundingClientRect(); const m = document.querySelector('.minipaint-menu').getBoundingClientRect(); return m.top >= b.bottom && m.left >= b.left - 1 && m.width >= 200; }"))
    r.check(f"{label}: the menu lists Open, Edit, Tools, Panels, Focus and Send to", menu_items(page) == ["Open…", "Edit ›", "Tools ›", "✓ Panels", "Focus", "Send to ›"], str(menu_items(page)))
    menu_click(page, "Send to")
    r.check(f"{label}: Send to lists every destination, the suggested one marked, and Cancel", menu_items(page) == ["‹ Back", "img2img  · suggested", "img2img Inpaint", "Extras", "ImageStitch (txt2img)", "ImageStitch (img2img)", "Cancel"], str(menu_items(page)))
    menu_click(page, "Cancel")
    r.check(f"{label}: Cancel closes the menu", wait_for(page, lambda: not debug(page)["menuOpen"], timeout=4))
    menu_open(page)
    menu_click(page, "Tools")
    r.check(f"{label}: Tools lists the four tools with the current one ticked", menu_items(page) == ["‹ Back", "✓ Crop", "Mask", "Expand", "Layers"], str(menu_items(page)))
    menu_click(page, "Back")
    r.check(f"{label}: Back returns to the top of the menu", menu_items(page)[0] == "Open…")
    page.keyboard.press("Escape")
    r.check(f"{label}: Escape closes the menu", wait_for(page, lambda: not debug(page)["menuOpen"], timeout=4))
    menu_open(page)
    box = page.locator("#minipaint_canvas_surface .forge-container").bounding_box()
    page.mouse.click(box["x"] + box["width"] - 20, box["y"] + box["height"] - 20)
    r.check(f"{label}: a tap outside closes the menu", wait_for(page, lambda: not debug(page)["menuOpen"], timeout=4))
    menu_open(page)
    menu_click(page, "Edit")
    r.check(f"{label}: Edit lists Undo, Redo, Reset and Save", menu_items(page) == ["‹ Back", "Undo", "Redo", "Reset to original", "Save a copy"], str(menu_items(page)))
    menu_click(page, "Undo")
    r.check(f"{label}: an item closes the menu and presses the hidden button", wait_for(page, lambda: not debug(page)["menuOpen"] and "Nothing to undo" in status_text(page), timeout=6), status_text(page))

    work_width = page.evaluate("() => document.querySelector('#minipaint_canvas_work').getBoundingClientRect().width")
    menu_pick(page, "Panels")
    r.check(f"{label}: Menu -> Panels puts the rail away and the canvas takes the width", wait_for(page, lambda: debug(page)["rail"]["hidden"] and page.evaluate("() => document.querySelector('#minipaint_canvas_work').getBoundingClientRect().width") > work_width + 200 and tab_fits(page), timeout=6), str(debug(page)["rail"]))
    menu_open(page)
    r.check(f"{label}: the menu shows Panels unticked while the rail is away", "Panels" in menu_items(page) and "✓ Panels" not in menu_items(page), str(menu_items(page)))
    menu_click(page, "Panels")
    r.check(f"{label}: and brings it back", wait_for(page, lambda: not debug(page)["rail"]["hidden"] and rail_beside(page), timeout=6), str(debug(page)["rail"]))
    r.check(f"{label}: the host's toolbar is visible, finger sized, and away from the top centre where the grip lives", page.evaluate(f"""() => {{
        const b = document.querySelector('#maxButton_{uuid}'); const s = b && getComputedStyle(b);
        const bar = document.querySelector('#minipaint_canvas_surface .forge-toolbar-static').getBoundingClientRect();
        const c = document.querySelector('#minipaint_canvas_surface .forge-container').getBoundingClientRect();
        return !!s && s.display !== 'none' && parseFloat(s.minHeight) >= 36 && bar.right < c.right - 40 && bar.left > c.left + c.width / 2;
    }}"""))
    r.check(f"{label}: the brush controls of the toolbar are hidden", page.evaluate("() => getComputedStyle(document.querySelector('#minipaint_canvas_surface .forge-toolbar-box-b')).display === 'none'"))

    # ---- receive from txt2img ----
    click_tab(page, "txt2img")
    page.locator("#probe_make").click()
    r.check(f"{label}: a gallery image appears", wait_for(page, lambda: page.locator("#txt2img_gallery .thumbnail-item").count() > 0))
    page.locator("#txt2img_gallery .thumbnail-item").first.click()
    time.sleep(0.5)
    page.locator("#txt2img_send_to_minipaint").click()
    r.check(f"{label}: receive lands in the Canvas tab", wait_for(page, lambda: visible_panels(page) == ["tab_minipaint"]), str(visible_panels(page)))
    r.check(f"{label}: status reports the receive", wait_for(page, lambda: "Received from txt2img" in status_text(page)), status_text(page))
    r.check(f"{label}: the canvas shows the 640x480 image", wait_for(page, lambda: debug(page)["orgWidth"] == 640 and debug(page)["orgHeight"] == 480), str(debug(page)))
    r.check(f"{label}: the image is fitted into the box", wait_for(page, lambda: (image_box(page, uuid) or {}).get("width", 0) > 300), str(image_box(page, uuid)))
    r.check(f"{label}: the crop frame covers the whole image", wait_for(page, lambda: debug(page)["box"] == {"x0": 0, "y0": 0, "x1": 640, "y1": 480} and not debug(page)["frameHidden"]), str(debug(page)))
    r.check(f"{label}: the canvas outline marks the picture's edge", wait_for(page, lambda: (lambda b, i: bool(b) and bool(i) and abs(b["width"] - i["width"]) < 2 and abs(b["height"] - i["height"]) < 2)(debug(page)["bounds"], image_box(page, uuid))), str(debug(page)["bounds"]))
    r.check(f"{label}: the picture is Layer 1 over a Background", wait_for(page, lambda: layer_rows(page) == [["Layer 1", True, True, False], ["Background", False, False, False]]), str(layer_rows(page)))
    time.sleep(1.2)
    r.check(f"{label}: the echo of the sent image did not count as an opened one", "Received from txt2img" in status_text(page) and "Opened" not in status_text(page), status_text(page))
    r.check(f"{label}: stroke history starts fresh", debug(page)["history"] == 1, str(debug(page)["history"]))

    # ---- crop with the mouse: drag the bottom-right handle inwards, then move the frame by its grip ----
    br = handle_center(page, "br")
    r.check(f"{label}: the frame has handles", br is not None)
    if br:
        drag(page, br[0], br[1], br[0] - 120, br[1] - 90)
    box = debug(page)["box"]
    r.check(f"{label}: dragging a handle shrinks the frame", box and box["x1"] < 640 and box["y1"] < 480 and box["x0"] == 0, str(box))
    readout = page.locator("#minipaint_canvas_surface .minipaint-frame-size").inner_text()
    r.check(f"{label}: the readout shows the crop size", readout == f"{box['x1'] - box['x0']} × {box['y1'] - box['y0']}", readout)
    grip = page.locator("#minipaint_canvas_surface .minipaint-frame-grip").first.bounding_box()
    r.check(f"{label}: the frame has a grip on its top edge", grip is not None and grip["width"] >= 44, str(grip))
    if grip:
        drag(page, grip["x"] + grip["width"] / 2, grip["y"] + grip["height"] / 2, grip["x"] + grip["width"] / 2 + 50, grip["y"] + grip["height"] / 2 + 30)
    moved = debug(page)["box"]
    r.check(f"{label}: the grip moves the frame without changing its size", moved and moved["x0"] > box["x0"] and moved["y0"] > box["y0"]
            and abs((moved["x1"] - moved["x0"]) - (box["x1"] - box["x0"])) <= 1 and abs((moved["y1"] - moved["y0"]) - (box["y1"] - box["y0"])) <= 1, str((box, moved)))
    box = moved
    page.locator("#minipaint_canvas_crop_apply").click()
    r.check(f"{label}: apply crop reports a smaller image", wait_for(page, lambda: "Cropped to" in status_text(page)), status_text(page))
    expected = f"Cropped to {box['x1'] - box['x0']} × {box['y1'] - box['y0']}"
    r.check(f"{label}: the crop is exactly the frame", expected in status_text(page), status_text(page))
    r.check(f"{label}: the canvas shows the cropped image and the frame resets to it",
            wait_for(page, lambda: debug(page)["orgWidth"] == box["x1"] - box["x0"] and debug(page)["box"] == {"x0": 0, "y0": 0, "x1": box["x1"] - box["x0"], "y1": box["y1"] - box["y0"]}), str(debug(page)))

    # ---- undo the crop structurally, then crop again: the sequence that failed in the field ----
    menu_pick(page, "Edit", "Undo")
    r.check(f"{label}: undo restores 640x480", wait_for(page, lambda: "Undid crop" in status_text(page) and debug(page)["orgWidth"] == 640), status_text(page))
    br = handle_center(page, "br")
    if br:
        drag(page, br[0], br[1], br[0] - 60, br[1] - 40)
    again = debug(page)["box"]
    page.locator("#minipaint_canvas_crop_apply").click()
    r.check(f"{label}: a crop after an undo works", again and wait_for(page, lambda: f"Cropped to {again['x1'] - again['x0']} × {again['y1'] - again['y0']}" in status_text(page)), status_text(page))
    menu_pick(page, "Edit", "Undo")
    r.check(f"{label}: and can be undone again", wait_for(page, lambda: "Undid crop" in status_text(page) and debug(page)["orgWidth"] == 640), status_text(page))

    # ---- aspect: the frame follows the dropdown ----
    pick_option(page, "#minipaint_canvas_crop_aspect", "1:1")
    r.check(f"{label}: a 1:1 aspect squares the frame", wait_for(page, lambda: (lambda b: b and b["x1"] - b["x0"] == b["y1"] - b["y0"] == 480)(debug(page)["box"])), str(debug(page)["box"]))
    pick_option(page, "#minipaint_canvas_crop_aspect", "Free")
    r.check(f"{label}: free aspect covers the image again", wait_for(page, lambda: debug(page)["box"] == {"x0": 0, "y0": 0, "x1": 640, "y1": 480}))

    # ---- touch: one finger pans the image under the frame, two fingers pinch ----
    before = debug(page)
    img = image_box(page, uuid)
    cx, cy = img["x"] + img["width"] / 2, img["y"] + img["height"] / 2
    touch(page, [(cx, cy, cx + 60, cy + 30)])
    after = debug(page)
    r.check(f"{label}: one finger pans in crop mode", round(after["imgX"] - before["imgX"]) == 60 and round(after["imgY"] - before["imgY"]) == 30, str((before["imgX"], after["imgX"])))
    r.check(f"{label}: panning moves the crop box, not the frame", after["frame"] == before["frame"] and after["box"] != before["box"])
    r.check(f"{label}: the canvas outline follows the picture", (lambda b: bool(b) and abs(b["left"] - after["imgX"]) < 1)(after["bounds"]), str(after["bounds"]))
    touch(page, [(cx - 40, cy, cx - 100, cy), (cx + 40, cy, cx + 100, cy)])
    pinched = debug(page)
    r.check(f"{label}: two fingers pinch to zoom", pinched["imgScale"] > after["imgScale"] * 1.5, str((after["imgScale"], pinched["imgScale"])))
    page.locator(f"#centerButton_{uuid}").click()
    r.check(f"{label}: the canvas's own fit button refits the image and the frame", wait_for(page, lambda: abs(debug(page)["imgScale"] - before["imgScale"]) < 0.01 and debug(page)["box"] == {"x0": 0, "y0": 0, "x1": 640, "y1": 480}))

    # ---- touch crop: a finger on a handle ----
    tl = handle_center(page, "tl")
    if tl:
        touch(page, [(tl[0], tl[1], tl[0] + 80, tl[1] + 60)])
    box = debug(page)["box"]
    r.check(f"{label}: a finger drags a handle", box and box["x0"] > 0 and box["y0"] > 0 and box["x1"] == 640, str(box))
    page.locator("#minipaint_canvas_crop_apply").click()
    r.check(f"{label}: a finger can crop", wait_for(page, lambda: f"Cropped to {box['x1'] - box['x0']} × {box['y1'] - box['y0']}" in status_text(page)), status_text(page))
    cropped = (box["x1"] - box["x0"], box["y1"] - box["y0"])
    r.check(f"{label}: the canvas has the finger crop", wait_for(page, lambda: (debug(page)["orgWidth"], debug(page)["orgHeight"]) == cropped))

    # ---- mask: paint a stroke, erase part of it, send ----
    menu_pick(page, "Tools", "Mask")
    r.check(f"{label}: Menu -> Tools -> Mask switches the tool and the button says so", wait_for(page, lambda: page.locator("#minipaint_canvas_menu").inner_text().strip() == "☰ Mask"), page.locator("#minipaint_canvas_menu").inner_text())
    r.check(f"{label}: mask mode hides the frame and arms the brush", wait_for(page, lambda: debug(page)["frameHidden"] and not debug(page)["noScribbles"] and debug(page)["mode"] == "mask"), str(debug(page)))
    r.check(f"{label}: the rail shows the mask panel now", wait_for(page, lambda: page.evaluate("() => ['crop', 'mask', 'expand', 'layers'].map(m => document.querySelector('#minipaint_canvas_panel_' + m).getBoundingClientRect().height > 0)") == [False, True, False, False]) and rail_beside(page))
    img = image_box(page, uuid)
    y = img["y"] + img["height"] * 0.5
    drag(page, img["x"] + img["width"] * 0.2, y, img["x"] + img["width"] * 0.7, y, steps=20)
    r.check(f"{label}: a stroke reaches the mask layer", wait_for(page, lambda: (lambda fg: fg is not None and fg.getchannel("A").getbbox() is not None)(canvas_layer(page, uuid, "logical_image_foreground"))))
    fg = canvas_layer(page, uuid, "logical_image_foreground")
    painted = fg.getchannel("A").getbbox()
    r.check(f"{label}: the stroke is where the finger went", painted and painted[0] < cropped[0] * 0.25 and painted[2] > cropped[0] * 0.65, str(painted))
    r.check(f"{label}: the stroke is opaque in the layer (opacity is display only)", fg.getchannel("A").getextrema()[1] == 255)
    before_undo = fg.getchannel("A").getbbox()
    menu_pick(page, "Edit", "Undo")
    r.check(f"{label}: undo takes the stroke back, not the crop", wait_for(page, lambda: "Undid a stroke" in status_text(page) and (lambda fg: fg is None or fg.getchannel("A").getbbox() is None)(canvas_layer(page, uuid, "logical_image_foreground"))) and (debug(page)["orgWidth"], debug(page)["orgHeight"]) == cropped, status_text(page))
    menu_pick(page, "Edit", "Redo")
    r.check(f"{label}: redo brings the stroke back", wait_for(page, lambda: "Redid a stroke" in status_text(page) and (lambda fg: fg is not None and fg.getchannel("A").getbbox() == before_undo)(canvas_layer(page, uuid, "logical_image_foreground"))), status_text(page))
    page.locator("#minipaint_canvas_mask_tool label", has_text="Erase").first.click()
    r.check(f"{label}: erase sets the brush to zero opacity", wait_for(page, lambda: float(debug(page)["alpha"]) == 0))
    drag(page, img["x"] + img["width"] * 0.55, y - 5, img["x"] + img["width"] * 0.75, y + 5, steps=12)
    r.check(f"{label}: erasing shortens the stroke", wait_for(page, lambda: (lambda fg: fg is not None and fg.getchannel("A").getbbox() is not None and fg.getchannel("A").getbbox()[2] < painted[2] - 10)(canvas_layer(page, uuid, "logical_image_foreground"))))
    page.locator("#minipaint_canvas_mask_tool label", has_text="Move").first.click()
    r.check(f"{label}: move disables the brush", wait_for(page, lambda: debug(page)["noScribbles"]))
    page.locator("#minipaint_canvas_mask_tool label", has_text="Paint").first.click()
    r.check(f"{label}: paint restores the brush at the Inpaint opacity", wait_for(page, lambda: not debug(page)["noScribbles"] and float(debug(page)["alpha"]) == 75))
    page.locator("#minipaint_canvas_mask_size input[type=range]").first.fill("40")
    r.check(f"{label}: the size slider drives the brush", wait_for(page, lambda: float(debug(page)["width"]) == 40))

    menu_open(page)
    menu_click(page, "Send to")
    r.check(f"{label}: with a mask the menu suggests Inpaint", "img2img Inpaint  · suggested" in menu_items(page), str(menu_items(page)))
    menu_click(page, "img2img Inpaint")
    r.check(f"{label}: send reports Inpaint", wait_for(page, lambda: "Sent to img2img Inpaint" in status_text(page)), status_text(page))
    r.check(f"{label}: send switches to the img2img tab", wait_for(page, lambda: visible_panels(page) == ["tab_img2img"]), str(visible_panels(page)))
    r.check(f"{label}: and to its Inpaint sub-tab", wait_for(page, lambda: page.evaluate("() => { const b = document.querySelector('#mode_img2img > .tab-nav > button.selected'); return b && b.textContent.trim() === 'Inpaint'; }")))
    r.check(f"{label}: inpaint got the image", wait_for(page, lambda: (lambda bg: bg is not None and bg.size == cropped)(canvas_layer(page, inpaint.uuid, "logical_image_background"))), str(canvas_layer(page, inpaint.uuid, "logical_image_background")))
    r.check(f"{label}: inpaint got the mask, same size, once its canvas had the image",
            wait_for(page, lambda: (lambda fg: fg is not None and fg.size == cropped and fg.getchannel("A").getbbox() is not None)(canvas_layer(page, inpaint.uuid, "logical_image_foreground"))), str(canvas_layer(page, inpaint.uuid, "logical_image_foreground")))
    r.check(f"{label}: the inpaint canvas is showing the image", page.evaluate(f"() => {{ const c = document.querySelector('#drawingCanvas_{inpaint.uuid}'); return c && c.width === {cropped[0]} && c.height === {cropped[1]}; }}"))
    r.check(f"{label}: and the mask is drawn on it", wait_for(page, lambda: page.evaluate(f"() => {{ const c = document.querySelector('#drawingCanvas_{inpaint.uuid}'); const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data; let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i] > 128) n++; return n > 100; }}"), timeout=6))

    # ---- expand: back to the canvas, add 128 on the right, apply, send again ----
    click_tab(page, "Mini Paint")
    menu_pick(page, "Tools", "Expand")
    r.check(f"{label}: expand mode disables the brush", wait_for(page, lambda: debug(page)["noScribbles"] and debug(page)["mode"] == "expand"))
    page.locator("#minipaint_canvas_expand_right").click()
    r.check(f"{label}: side button previews the size", wait_for(page, lambda: "→" in page.evaluate("() => document.querySelector('#minipaint_canvas_expand_preview').innerText")))
    r.check(f"{label}: the exact amounts sit two per line in the rail", page.evaluate("() => { const l = document.querySelector('#minipaint_canvas_expand_num_left').getBoundingClientRect(); const r = document.querySelector('#minipaint_canvas_expand_num_right').getBoundingClientRect(); const t = document.querySelector('#minipaint_canvas_expand_num_top').getBoundingClientRect(); return l.width > 60 && Math.abs(l.top - r.top) < 4 && r.left > l.right && t.top > l.bottom; }"))
    page.locator("#minipaint_canvas_expand_apply").click()
    r.check(f"{label}: expand applies", wait_for(page, lambda: "Expanded to" in status_text(page)), status_text(page))
    r.check(f"{label}: expand lands in mask mode, the button says so", wait_for(page, lambda: debug(page)["mode"] == "mask" and page.locator("#minipaint_canvas_menu").inner_text().strip() == "☰ Mask"))
    r.check(f"{label}: the canvas shows the wider image with its mask layer",
            wait_for(page, lambda: debug(page)["orgWidth"] == cropped[0] + 128 and (lambda fg: fg is not None and fg.size[0] == cropped[0] + 128 and fg.getchannel("A").getpixel((cropped[0] + 100, 5)) == 255)(canvas_layer(page, uuid, "logical_image_foreground"))), str(debug(page)))
    menu_pick(page, "Send to", "img2img Inpaint")
    r.check(f"{label}: outpaint sent to Inpaint with its see-through area kept", wait_for(page, lambda: "Sent to img2img Inpaint" in status_text(page) and "see-through pixels were kept" in status_text(page)), status_text(page))
    r.check(f"{label}: expanded image is wider by 128, see-through where it was expanded",
            wait_for(page, lambda: (lambda bg: bg is not None and bg.size[0] == cropped[0] + 128 and bg.getchannel("A").getpixel((bg.size[0] - 10, bg.size[1] // 2)) == 0 and bg.getchannel("A").getpixel((10, 10)) == 255)(canvas_layer(page, inpaint.uuid, "logical_image_background"))))
    r.check(f"{label}: new area is masked in Inpaint",
            wait_for(page, lambda: (lambda fg: fg is not None and fg.size[0] == cropped[0] + 128 and fg.getchannel("A").getpixel((fg.size[0] - 10, fg.size[1] // 2)) == 255)(canvas_layer(page, inpaint.uuid, "logical_image_foreground"))))

    # ---- clear and invert write the layer ----
    click_tab(page, "Mini Paint")
    page.locator("#minipaint_canvas_mask_clear").click()
    r.check(f"{label}: clear mask empties the layer", wait_for(page, lambda: "Mask cleared" in status_text(page) and (lambda fg: fg is None or fg.getchannel("A").getbbox() is None)(canvas_layer(page, uuid, "logical_image_foreground"))), status_text(page))
    page.locator("#minipaint_canvas_mask_invert").click()
    r.check(f"{label}: invert of nothing masks everything", wait_for(page, lambda: "Mask inverted" in status_text(page) and (lambda fg: fg is not None and fg.getchannel("A").getextrema() == (255, 255))(canvas_layer(page, uuid, "logical_image_foreground"))), status_text(page))

    # ---- layers: the frame as a selection, a drag on the selection with the mouse and with a finger, the list ----
    page.locator("#minipaint_canvas_mask_clear").click()
    wait_for(page, lambda: "Mask cleared" in status_text(page))
    menu_pick(page, "Tools", "Layers")
    r.check(f"{label}: layers mode shows the frame as a selection and the layer panel", wait_for(page, lambda: debug(page)["mode"] == "layers" and not debug(page)["frameHidden"] and debug(page)["preview"]), str(debug(page)))
    r.check(f"{label}: the layer list has the picture over the Background, the picture selected", wait_for(page, lambda: layer_rows(page) == [["Layer 1", True, True, False], ["Background", False, False, False]]), str(layer_rows(page)))
    # (the picture is narrower than the canvas since the expansion: the outline is inside the canvas's, most of its width)
    r.check(f"{label}: the selected layer is outlined on the canvas", wait_for(page, lambda: (lambda s, b: bool(s) and bool(b) and s["left"] >= b["left"] - 1 and s["left"] + s["width"] <= b["left"] + b["width"] + 1 and s["width"] > b["width"] * 0.5)(debug(page)["selection"], debug(page)["bounds"])), str((debug(page)["selection"], debug(page)["bounds"])))
    before = debug(page)
    tl = handle_center(page, "tl")
    if tl:
        drag(page, tl[0], tl[1], tl[0] + 120, tl[1] + 90)
    box = debug(page)["box"]
    page.locator("#minipaint_canvas_layer_new").click()
    r.check(f"{label}: a selection becomes a new layer without a reload", wait_for(page, lambda: "Layer 2 holds the selection" in status_text(page)) and debug(page)["loaded"] == before["loaded"], status_text(page))
    r.check(f"{label}: the list shows it on top, selected, the others not", wait_for(page, lambda: layer_rows(page) == [["Layer 2", True, True, False], ["Layer 1", False, False, False], ["Background", False, False, False]]), str(layer_rows(page)))
    r.check(f"{label}: the browser got the layer to drag and the underlay", wait_for(page, lambda: debug(page)["preview"] and page.evaluate("() => document.querySelector('#minipaint_canvas_layer_underlay textarea').value.startsWith('data:image/png')")))
    r.check(f"{label}: the outline moved to the new layer", wait_for(page, lambda: (lambda s, b: bool(s) and bool(b) and s["width"] < b["width"] - 20 and s["left"] > b["left"] + 20)(debug(page)["selection"], debug(page)["bounds"])), str(debug(page)["selection"]))
    # drag the layer with the mouse, starting on it: the view is kept, the layer lands where it was dropped
    img = image_box(page, uuid)
    cx, cy = img["x"] + img["width"] * 0.5, img["y"] + img["height"] * 0.5
    scale = debug(page)["imgScale"]
    drag(page, cx, cy, cx + 40 * scale, cy + 20 * scale, steps=16)
    r.check(f"{label}: the dropped layer is where the mouse left it", wait_for(page, lambda: "Moved Layer 2" in status_text(page)), status_text(page))
    landed = page.evaluate("() => (document.querySelector('#minipaint_canvas_status').innerText.match(/ at \\((-?\\d+), (-?\\d+)\\)/) || []).slice(1).map(Number)")
    r.check(f"{label}: by the distance dragged", landed and abs(landed[0] - (box["x0"] + 40)) <= 2 and abs(landed[1] - (box["y0"] + 20)) <= 2, str((landed, box)))
    r.check(f"{label}: the composite was reloaded with the view kept", wait_for(page, lambda: debug(page)["loaded"] == before["loaded"] + 1 and abs(debug(page)["imgScale"] - scale) < 0.001 and not debug(page)["overlay"]), str(debug(page)))
    r.check(f"{label}: the preview is gone after the drop", not debug(page)["layerDrag"])
    # and with a finger
    touch(page, [(cx, cy, cx - 30 * scale, cy - 10 * scale)])
    r.check(f"{label}: a finger drags the layer too", wait_for(page, lambda: (lambda l: bool(l) and abs(l[0] - (box["x0"] + 10)) <= 2 and abs(l[1] - (box["y0"] + 10)) <= 2)(page.evaluate("() => (document.querySelector('#minipaint_canvas_status').innerText.match(/ at \\((-?\\d+), (-?\\d+)\\)/) || []).slice(1).map(Number)"))), status_text(page))
    menu_pick(page, "Edit", "Undo")
    r.check(f"{label}: undo takes the move back", wait_for(page, lambda: "Undid move layer" in status_text(page) and "is at (" not in status_text(page).split("Undid")[0]), status_text(page))
    r.check(f"{label}: and reloads the composite", wait_for(page, lambda: debug(page)["loaded"] == before["loaded"] + 3), str(debug(page)["loaded"]))
    # a drag that starts off the selection pans the picture instead of moving anything
    off = debug(page)
    drag(page, img["x"] + 12, img["y"] + 12, img["x"] + 42, img["y"] + 32, steps=8)
    time.sleep(0.6)
    panned = debug(page)
    r.check(f"{label}: a drag off the selected layer pans, and moves no layer", round(panned["imgX"] - off["imgX"]) == 30 and round(panned["imgY"] - off["imgY"]) == 20 and panned["loaded"] == off["loaded"] and "Moved" not in status_text(page), str((off["imgX"], panned["imgX"], status_text(page))))
    page.locator(f"#centerButton_{uuid}").click()
    wait_for(page, lambda: abs(debug(page)["imgX"] - off["imgX"]) < 1)
    # the list: tap a name to select, the eye to hide, the box to select several, the arrows to reorder
    layer_row(page, "Background").locator(".minipaint-layer-pick").click()
    r.check(f"{label}: tapping a layer in the list selects it", wait_for(page, lambda: "Background is the active layer" in status_text(page) and layer_rows(page) == [["Layer 2", False, False, False], ["Layer 1", False, False, False], ["Background", True, True, False]]), str(layer_rows(page)))
    time.sleep(0.6)
    r.check(f"{label}: selecting did not reload the canvas", debug(page)["loaded"] == before["loaded"] + 3, str(debug(page)["loaded"]))
    r.check(f"{label}: the outline moved to the Background", wait_for(page, lambda: (lambda s, b: bool(s) and bool(b) and abs(s["width"] - b["width"]) < 2)(debug(page)["selection"], debug(page)["bounds"])), str(debug(page)["selection"]))
    layer_row(page, "Background").locator(".minipaint-layer-eye").click()
    r.check(f"{label}: the eye hides a layer and reloads the composite", wait_for(page, lambda: "Background hidden" in status_text(page)), status_text(page))
    r.check(f"{label}: the list marks it hidden", wait_for(page, lambda: layer_rows(page)[2] == ["Background", True, True, True]), str(layer_rows(page)))
    layer_row(page, "Layer 1").locator(".minipaint-layer-eye").click()
    r.check(f"{label}: with the picture hidden too the canvas is see-through there", wait_for(page, lambda: "Layer 1 hidden" in status_text(page) and (lambda bg: bg is not None and bg.getchannel("A").getpixel((2, 2)) == 0)(canvas_layer(page, uuid, "logical_image_background"))), status_text(page))
    r.check(f"{label}: the canvas outline still marks the edge of a see-through canvas", (lambda b, i: bool(b) and bool(i) and abs(b["width"] - i["width"]) < 2)(debug(page)["bounds"], image_box(page, uuid)))
    menu_pick(page, "Send to", "img2img")
    r.check(f"{label}: a see-through canvas is sent as it is", wait_for(page, lambda: "Sent to img2img" in status_text(page) and "see-through pixels were kept" in status_text(page)), status_text(page))
    r.check(f"{label}: img2img got it see-through, not gray", wait_for(page, lambda: (lambda bg: bg is not None and bg.getchannel("A").getpixel((2, 2)) == 0)(canvas_layer(page, refs["init_img"].uuid, "logical_image_background"))))
    click_tab(page, "Mini Paint")
    layer_row(page, "Layer 1").locator(".minipaint-layer-eye").click()
    wait_for(page, lambda: "Layer 1 shown" in status_text(page))
    layer_row(page, "Background").locator(".minipaint-layer-eye").click()
    r.check(f"{label}: the eye shows them again", wait_for(page, lambda: "Background shown" in status_text(page) and layer_rows(page)[2] == ["Background", True, True, False]), status_text(page))
    layer_row(page, "Layer 2").locator(".minipaint-layer-check").click()
    r.check(f"{label}: the box adds a second layer to the selection", wait_for(page, lambda: "2 layers selected" in status_text(page) and layer_rows(page) == [["Layer 2", True, True, False], ["Layer 1", False, False, False], ["Background", True, False, False]]), str(layer_rows(page)))
    r.check(f"{label}: the drag preview covers both, over the layer between", wait_for(page, lambda: page.evaluate("() => { const p = JSON.parse(document.querySelector('#minipaint_canvas_layer_preview textarea').value || '{}'); return p.name === 'Background, Layer 2' && document.querySelector('#minipaint_canvas_layer_underlay textarea').value.startsWith('data:image/png'); }")))
    layer_row(page, "Background").locator(".minipaint-layer-check").click()
    r.check(f"{label}: and takes it out again", wait_for(page, lambda: layer_rows(page) == [["Layer 2", True, True, False], ["Layer 1", False, False, False], ["Background", False, False, False]]), str(layer_rows(page)))
    loaded = debug(page)["loaded"]
    layer_row(page, "Layer 2").locator(".minipaint-layer-down").click()
    r.check(f"{label}: the arrow moves a layer down the stack", wait_for(page, lambda: "Layer 2 moved down" in status_text(page) and [row[0] for row in layer_rows(page)] == ["Layer 1", "Layer 2", "Background"]), str(layer_rows(page)))
    wait_for(page, lambda: debug(page)["loaded"] == loaded + 1)
    loaded = debug(page)["loaded"]
    layer_row(page, "Layer 2").locator(".minipaint-layer-down").click()
    r.check(f"{label}: and under the Background", wait_for(page, lambda: [row[0] for row in layer_rows(page)] == ["Layer 1", "Background", "Layer 2"]), str(layer_rows(page)))
    r.check(f"{label}: at the bottom the arrow is disabled", page.evaluate("() => document.querySelector(\"#minipaint_canvas_layer_list .minipaint-layer[data-name='Layer 2'] .minipaint-layer-down\").disabled"))
    wait_for(page, lambda: debug(page)["loaded"] == loaded + 1)
    for step in range(2):
        loaded = debug(page)["loaded"]
        layer_row(page, "Layer 2").locator(".minipaint-layer-up").click()
        wait_for(page, lambda: debug(page)["loaded"] == loaded + 1)
    r.check(f"{label}: and up again, twice", wait_for(page, lambda: "Layer 2 moved up" in status_text(page) and [row[0] for row in layer_rows(page)] == ["Layer 2", "Layer 1", "Background"]), str(layer_rows(page)))
    r.check(f"{label}: reordering reloads the composite with the view kept", abs(debug(page)["imgScale"] - scale) < 0.001, str(debug(page)))
    # resizing: double, then back to the original
    outline = debug(page)["selection"]
    sized = debug(page)["loaded"]
    page.locator("#minipaint_canvas_layer_double").click()
    r.check(f"{label}: ×2 doubles the layer", wait_for(page, lambda: "Layer 2 at 200% size" in status_text(page) and "200% size" in page.locator("#minipaint_canvas_layer_list").inner_text()), status_text(page))
    r.check(f"{label}: the outline and the size slider follow", wait_for(page, lambda: (lambda s: bool(s) and abs(s["width"] - 2 * outline["width"]) < 4)(debug(page)["selection"]) and page.locator("#minipaint_canvas_layer_scale input[type=range]").first.input_value() == "200"), str(debug(page)["selection"]))
    page.locator("#minipaint_canvas_layer_full").click()
    r.check(f"{label}: 100% brings the original size back", wait_for(page, lambda: "Layer 2 at 100% size" in status_text(page) and (lambda s: bool(s) and abs(s["width"] - outline["width"]) < 4)(debug(page)["selection"])), status_text(page))
    # each resize reloads the composite; a drag that starts before the picture has arrived is dropped with it
    r.check(f"{label}: both resizes reloaded the composite", wait_for(page, lambda: debug(page)["loaded"] == sized + 2 and not debug(page)["layerDrag"]), str((debug(page)["loaded"], sized)))
    # re-center brings a layer dragged away back into view
    sel = debug(page)["selection"]  # container pixels: add the container's place on the page
    origin = page.locator("#minipaint_canvas_surface .forge-container").bounding_box()
    sx, sy = origin["x"] + sel["left"] + sel["width"] / 2, origin["y"] + sel["top"] + sel["height"] / 2
    drag(page, sx, sy, sx + 400, sy + 300, steps=10)
    r.check(f"{label}: a layer can be dragged off the canvas", wait_for(page, lambda: "Moved Layer 2" in status_text(page)), status_text(page))
    page.locator("#minipaint_canvas_layer_center").click()
    def centered(text):
        # "707 × 434 · 3 layers · ... · Layer 2: 479 × 407 at (114, 13) — Centered Layer 2."
        canvas = re.search(r"(\d+) × (\d+) · \d+ layers", text)
        layer = re.search(r"Layer 2: (\d+) × (\d+) at \((-?\d+), (-?\d+)\)", text)
        if not canvas or not layer:
            return False
        cw, ch = int(canvas.group(1)), int(canvas.group(2))
        w, h, x, y = (int(layer.group(k)) for k in range(1, 5))
        return abs(x - (cw - w) // 2) <= 1 and abs(y - (ch - h) // 2) <= 1
    r.check(f"{label}: Re-center brings it back to the middle", wait_for(page, lambda: "Centered Layer 2" in status_text(page) and centered(status_text(page))), status_text(page))
    page.locator("#minipaint_canvas_layer_merge").click()
    r.check(f"{label}: merge with one layer selected merges it into the picture", wait_for(page, lambda: "Layer 2 merged into Layer 1" in status_text(page) and layer_rows(page) == [["Layer 1", True, True, False], ["Background", False, False, False]]), status_text(page))
    menu_pick(page, "Edit", "Undo")
    r.check(f"{label}: undo restores the three layers", wait_for(page, lambda: "Undid merge down" in status_text(page) and "3 layers" in status_text(page)), status_text(page))
    page.locator("#minipaint_canvas_layer_delete").click()
    r.check(f"{label}: delete removes the selected layer", wait_for(page, lambda: "Layer 2 deleted" in status_text(page)), status_text(page))

    # ---- send to ImageStitch from the menu: the reference gallery gets the image, its box is ticked, the tab switches ----
    menu_pick(page, "Send to", "ImageStitch (txt2img)")
    r.check(f"{label}: send reports ImageStitch", wait_for(page, lambda: "Sent to ImageStitch (txt2img)" in status_text(page)), status_text(page))
    r.check(f"{label}: and switches to txt2img", wait_for(page, lambda: visible_panels(page) == ["tab_txt2img"]), str(visible_panels(page)))
    r.check(f"{label}: the reference gallery holds the image, alone", wait_for(page, lambda: page.locator("#script_txt2img_imagestitch_integrated_ref_latent .thumbnail-item").count() == 1), str(page.locator("#script_txt2img_imagestitch_integrated_ref_latent .thumbnail-item").count()))
    r.check(f"{label}: the ImageStitch box was ticked from the browser and its change fired", wait_for(page, lambda: page.evaluate("() => { const c = document.querySelector('#input-accordion-0-checkbox input'); const a = document.getElementById('input-accordion-0'); return !!c && c.checked && !!a && a.classList.contains('probe-open'); }")))
    r.check(f"{label}: the img2img box was left alone", not page.evaluate("() => document.querySelector('#input-accordion-1-checkbox input').checked"))
    click_tab(page, "Mini Paint")
    menu_pick(page, "Send to", "ImageStitch (txt2img)")
    r.check(f"{label}: a second send replaces the reference rather than adding one", wait_for(page, lambda: visible_panels(page) == ["tab_txt2img"]) and wait_for(page, lambda: page.locator("#script_txt2img_imagestitch_integrated_ref_latent .thumbnail-item").count() == 1, timeout=4) and page.locator("#script_txt2img_imagestitch_integrated_ref_latent .thumbnail-item").count() == 1)
    click_tab(page, "Mini Paint")

    # ---- reset from the menu, then a picture opened from the menu and one dropped on the canvas ----
    menu_pick(page, "Edit", "Reset to original")
    r.check(f"{label}: reset goes back to the received image", wait_for(page, lambda: "as it arrived" in status_text(page) and debug(page)["orgWidth"] == 640 and debug(page)["mode"] == "crop"), status_text(page))
    if with_upload:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            sample_image(300, 200).save(handle.name)
        with page.expect_file_chooser(timeout=8000) as chooser:
            menu_pick(page, "Open…")
        chooser.value.set_files(handle.name)
        r.check(f"{label}: Menu -> Open opens a file into the canvas", wait_for(page, lambda: "Opened." in status_text(page) and "300 × 200" in status_text(page) and debug(page)["orgWidth"] == 300), status_text(page))
        r.check(f"{label}: and the previous one is one undo away", "one Undo away" in status_text(page))
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            sample_image(320, 240).save(handle.name)
        page.locator(f"#imageInput_{uuid}").set_input_files(handle.name)
        r.check(f"{label}: a picture opened on the canvas itself becomes the document", wait_for(page, lambda: "Opened." in status_text(page) and "320 × 240" in status_text(page)), status_text(page))
        r.check(f"{label}: the canvas kept that picture (no reload)", debug(page)["orgWidth"] == 320)
        menu_pick(page, "Edit", "Undo")
        r.check(f"{label}: undo brings the earlier picture back", wait_for(page, lambda: "Undid open" in status_text(page) and debug(page)["orgWidth"] == 300), status_text(page))

    # ---- focus mode from the menu ----
    menu_pick(page, "Focus")
    r.check(f"{label}: focus mode fixes the root", wait_for(page, lambda: page.evaluate("() => document.querySelector('#minipaint_canvas_root').classList.contains('minipaint-focus')")))
    menu_open(page)
    r.check(f"{label}: the menu shows Focus ticked", "✓ Focus" in menu_items(page), str(menu_items(page)))
    page.keyboard.press("Escape")
    r.check(f"{label}: escape closes the menu first", wait_for(page, lambda: not debug(page)["menuOpen"]) and page.evaluate("() => document.querySelector('#minipaint_canvas_root').classList.contains('minipaint-focus')"))
    page.keyboard.press("Escape")
    r.check(f"{label}: and then leaves focus mode", wait_for(page, lambda: page.evaluate("() => !document.querySelector('#minipaint_canvas_root').classList.contains('minipaint-focus')")))

    check_tabs(r, page, f"{label} after use")


def open_page(p, port, chromium, args, keep=False, touch=True):
    browser = p.chromium.launch(executable_path=chromium, headless=not keep, args=args)
    context = browser.new_context(viewport={"width": 1280, "height": 900}, has_touch=touch)
    page = context.new_page()
    errors = []

    def note_error(error):
        text = str(error)
        # Forge's own script.js handles Escape by reading the current tab's
        # Interrupt button, which no extension tab has; that console error
        # is the host's, and happens on every extension tab in a real
        # install too.
        if "reading 'style'" in text:
            return
        errors.append(text)

    page.on("pageerror", note_error)
    page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
    page.wait_for_selector("#tabs .tab-nav button", timeout=30000)
    time.sleep(3)
    return browser, page, errors


def run_new_ui(r: Results, port: int, chromium: str, keep: bool) -> None:
    from playwright.sync_api import sync_playwright

    demo, refs = build_with_generator(old_ui=False)
    uuid = page_uuid = None
    demo.queue().launch(server_name="127.0.0.1", server_port=port, prevent_thread_lock=True, quiet=True,
                        allowed_paths=[str(ROOT)])
    try:
        with sync_playwright() as p:
            # ---- 1. an ordinary browser ----
            browser, page, errors = open_page(p, port, chromium, [], keep=keep)
            uuid = page.evaluate("() => { const t = document.querySelector('#minipaint_canvas_surface .forge-container'); return t ? t.id.replace('container_', '') : null; }")
            r.check("the surface is on the page", bool(uuid))
            r.check("webgl: the browser has WebGL", page.evaluate("() => { try { const c = document.createElement('canvas'); return !!(c.getContext('webgl') || c.getContext('experimental-webgl')); } catch (e) { return false; } }"))
            run_flow(r, page, refs, uuid, "webgl", with_upload=True)
            r.check("webgl: no page errors", not errors, "; ".join(e[:160] for e in errors[:3]))
            if keep:
                input("press Enter to close the browser...")
            browser.close()

            # ---- 2. the same, with WebGL disabled: nothing may differ ----
            browser, page, errors = open_page(p, port, chromium, ["--disable-3d-apis"])
            r.check("no-webgl: the browser really has no WebGL", not page.evaluate("() => { try { const c = document.createElement('canvas'); return !!(c.getContext('webgl') || c.getContext('experimental-webgl')); } catch (e) { return false; } }"))
            run_flow(r, page, refs, uuid, "no-webgl", with_upload=False)
            r.check("no-webgl: no page errors", not errors, "; ".join(e[:160] for e in errors[:3]))
            browser.close()
    finally:
        demo.close()


def run_legacy(r: Results, port: int, chromium: str) -> None:
    from playwright.sync_api import sync_playwright

    demo, _ = build(old_ui=True)
    demo.queue().launch(server_name="127.0.0.1", server_port=port, prevent_thread_lock=True, quiet=True,
                        allowed_paths=[str(ROOT)])
    try:
        with sync_playwright() as p:
            browser, page, errors = open_page(p, port, chromium, [], touch=False)
            check_tabs(r, page, "legacy")
            r.check("legacy: iframe mounted", page.evaluate("() => !!document.querySelector('#a1111minipaint_iframe')"))
            r.check("legacy: no touch editor", not page.evaluate("() => !!document.querySelector('#minipaint_canvas_root')"))
            r.check("legacy: no receive buttons from the new UI", not page.evaluate("() => !!document.querySelector('#txt2img_send_to_minipaint')"))
            r.check("legacy: no page errors", not errors, "; ".join(e[:160] for e in errors[:3]))
            browser.close()
    finally:
        demo.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--keep", action="store_true", help="show the browser and wait before closing it")
    parser.add_argument("--chromium", default=os.environ.get("CHROMIUM", "/opt/pw-browsers/chromium"))
    args = parser.parse_args()
    if not os.path.exists(args.chromium):
        args.chromium = None  # let Playwright find its own

    r = Results("browser")
    run_new_ui(r, args.port, args.chromium, args.keep)
    run_legacy(r, args.port + 1, args.chromium)
    return 0 if r.report() else 1


if __name__ == "__main__":
    sys.exit(main())
