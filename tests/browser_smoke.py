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
import json
import os
import pathlib
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


def handle_center(page, corner):
    box = page.locator(f"#minipaint_canvas_surface .minipaint-frame-handle.{corner}").first.bounding_box()
    return (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2) if box else None


def image_box(page, uuid):
    return page.locator(f"#image_{uuid}").bounding_box()


def run_flow(r: Results, page, refs, uuid: str, label: str, with_upload: bool) -> None:
    """Everything the Canvas does, once, from a page that is already open."""
    inpaint = refs["init_img_with_mask"]
    check_tabs(r, page, label)

    d = debug(page)
    r.check(f"{label}: the canvas is attached, without an image", d is not None and not d["hasImage"] and d["frameHidden"], str(d))
    r.check(f"{label}: the host's toolbar is visible and finger sized", page.evaluate(f"() => {{ const b = document.querySelector('#maxButton_{uuid}'); const s = b && getComputedStyle(b); return !!s && s.display !== 'none' && parseFloat(s.minHeight) >= 36; }}"))
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
    time.sleep(1.2)
    r.check(f"{label}: the echo of the sent image did not count as an opened one", "Received from txt2img" in status_text(page) and "Opened" not in status_text(page), status_text(page))
    r.check(f"{label}: stroke history starts fresh", debug(page)["history"] == 1, str(debug(page)["history"]))

    # ---- crop with the mouse: drag the bottom-right handle inwards ----
    br = handle_center(page, "br")
    r.check(f"{label}: the frame has handles", br is not None)
    if br:
        drag(page, br[0], br[1], br[0] - 120, br[1] - 90)
    box = debug(page)["box"]
    r.check(f"{label}: dragging a handle shrinks the frame", box and box["x1"] < 640 and box["y1"] < 480 and box["x0"] == 0, str(box))
    readout = page.locator("#minipaint_canvas_surface .minipaint-frame-size").inner_text()
    r.check(f"{label}: the readout shows the crop size", readout == f"{box['x1'] - box['x0']} × {box['y1'] - box['y0']}", readout)
    page.locator("#minipaint_canvas_crop_apply").click()
    r.check(f"{label}: apply crop reports a smaller image", wait_for(page, lambda: "Cropped to" in status_text(page)), status_text(page))
    expected = f"Cropped to {box['x1'] - box['x0']} × {box['y1'] - box['y0']}"
    r.check(f"{label}: the crop is exactly the frame", expected in status_text(page), status_text(page))
    r.check(f"{label}: the canvas shows the cropped image and the frame resets to it",
            wait_for(page, lambda: debug(page)["orgWidth"] == box["x1"] and debug(page)["box"] == {"x0": 0, "y0": 0, "x1": box["x1"], "y1": box["y1"]}), str(debug(page)))

    # ---- undo the crop structurally ----
    page.locator("#minipaint_canvas_undo").click()
    r.check(f"{label}: undo restores 640x480", wait_for(page, lambda: "Undid crop" in status_text(page) and debug(page)["orgWidth"] == 640), status_text(page))

    # ---- aspect: the frame follows the chips ----
    page.locator("#minipaint_canvas_crop_aspect label", has_text="1:1").first.click()
    r.check(f"{label}: a 1:1 aspect squares the frame", wait_for(page, lambda: (lambda b: b and b["x1"] - b["x0"] == b["y1"] - b["y0"] == 480)(debug(page)["box"])), str(debug(page)["box"]))
    page.locator("#minipaint_canvas_crop_aspect label", has_text="Free").first.click()
    r.check(f"{label}: free aspect covers the image again", wait_for(page, lambda: debug(page)["box"] == {"x0": 0, "y0": 0, "x1": 640, "y1": 480}))

    # ---- touch: one finger pans the image under the frame, two fingers pinch ----
    before = debug(page)
    img = image_box(page, uuid)
    cx, cy = img["x"] + img["width"] / 2, img["y"] + img["height"] / 2
    touch(page, [(cx, cy, cx + 60, cy + 30)])
    after = debug(page)
    r.check(f"{label}: one finger pans in crop mode", round(after["imgX"] - before["imgX"]) == 60 and round(after["imgY"] - before["imgY"]) == 30, str((before["imgX"], after["imgX"])))
    r.check(f"{label}: panning moves the crop box, not the frame", after["frame"] == before["frame"] and after["box"] != before["box"])
    touch(page, [(cx - 40, cy, cx - 100, cy), (cx + 40, cy, cx + 100, cy)])
    pinched = debug(page)
    r.check(f"{label}: two fingers pinch to zoom", pinched["imgScale"] > after["imgScale"] * 1.5, str((after["imgScale"], pinched["imgScale"])))
    page.locator("#minipaint_canvas_fit").click()
    r.check(f"{label}: fit refits the image and the frame", wait_for(page, lambda: abs(debug(page)["imgScale"] - before["imgScale"]) < 0.01 and debug(page)["box"] == {"x0": 0, "y0": 0, "x1": 640, "y1": 480}))

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
    page.locator("#minipaint_canvas_mode_mask").click()
    r.check(f"{label}: mask mode relabels send", wait_for(page, lambda: "Inpaint" in page.locator("#minipaint_canvas_send").inner_text()))
    r.check(f"{label}: mask mode hides the frame and arms the brush", wait_for(page, lambda: debug(page)["frameHidden"] and not debug(page)["noScribbles"] and debug(page)["mode"] == "mask"), str(debug(page)))
    img = image_box(page, uuid)
    y = img["y"] + img["height"] * 0.5
    drag(page, img["x"] + img["width"] * 0.2, y, img["x"] + img["width"] * 0.7, y, steps=20)
    r.check(f"{label}: a stroke reaches the mask layer", wait_for(page, lambda: (lambda fg: fg is not None and fg.getchannel("A").getbbox() is not None)(canvas_layer(page, uuid, "logical_image_foreground"))))
    fg = canvas_layer(page, uuid, "logical_image_foreground")
    painted = fg.getchannel("A").getbbox()
    r.check(f"{label}: the stroke is where the finger went", painted and painted[0] < cropped[0] * 0.25 and painted[2] > cropped[0] * 0.65, str(painted))
    r.check(f"{label}: the stroke is opaque in the layer (opacity is display only)", fg.getchannel("A").getextrema()[1] == 255)
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

    page.locator("#minipaint_canvas_send").click()
    r.check(f"{label}: send reports Inpaint", wait_for(page, lambda: "Sent to img2img Inpaint" in status_text(page)), status_text(page))
    r.check(f"{label}: send switches to the img2img tab", wait_for(page, lambda: visible_panels(page) == ["tab_img2img"]), str(visible_panels(page)))
    r.check(f"{label}: and to its Inpaint sub-tab", wait_for(page, lambda: page.evaluate("() => { const b = document.querySelector('#mode_img2img > .tab-nav > button.selected'); return b && b.textContent.trim() === 'Inpaint'; }")))
    r.check(f"{label}: inpaint got the image", wait_for(page, lambda: (lambda bg: bg is not None and bg.size == cropped)(canvas_layer(page, inpaint.uuid, "logical_image_background"))), str(canvas_layer(page, inpaint.uuid, "logical_image_background")))
    r.check(f"{label}: inpaint got the mask, same size, once its canvas had the image",
            wait_for(page, lambda: (lambda fg: fg is not None and fg.size == cropped and fg.getchannel("A").getbbox() is not None)(canvas_layer(page, inpaint.uuid, "logical_image_foreground"))), str(canvas_layer(page, inpaint.uuid, "logical_image_foreground")))
    r.check(f"{label}: the inpaint canvas is showing the image", page.evaluate(f"() => {{ const c = document.querySelector('#drawingCanvas_{inpaint.uuid}'); return c && c.width === {cropped[0]} && c.height === {cropped[1]}; }}"))
    r.check(f"{label}: and the mask is drawn on it", page.evaluate(f"() => {{ const c = document.querySelector('#drawingCanvas_{inpaint.uuid}'); const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data; let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i] > 128) n++; return n > 100; }}"))

    # ---- expand: back to the canvas, add 128 on the right, apply, send again ----
    click_tab(page, "Mini Paint")
    page.locator("#minipaint_canvas_mode_expand").click()
    r.check(f"{label}: expand mode disables the brush", wait_for(page, lambda: debug(page)["noScribbles"] and debug(page)["mode"] == "expand"))
    page.locator("#minipaint_canvas_expand_right").click()
    r.check(f"{label}: side button previews the size", wait_for(page, lambda: "→" in page.evaluate("() => document.querySelector('#minipaint_canvas_expand_preview').innerText")))
    page.locator("#minipaint_canvas_expand_apply").click()
    r.check(f"{label}: expand applies", wait_for(page, lambda: "Expanded to" in status_text(page)), status_text(page))
    r.check(f"{label}: expand lands in mask mode", wait_for(page, lambda: "Outpaint" in page.locator("#minipaint_canvas_send").inner_text() and debug(page)["mode"] == "mask"))
    r.check(f"{label}: the canvas shows the wider image with its mask layer",
            wait_for(page, lambda: debug(page)["orgWidth"] == cropped[0] + 128 and (lambda fg: fg is not None and fg.size[0] == cropped[0] + 128 and fg.getchannel("A").getpixel((cropped[0] + 100, 5)) == 255)(canvas_layer(page, uuid, "logical_image_foreground"))), str(debug(page)))
    page.locator("#minipaint_canvas_send").click()
    r.check(f"{label}: outpaint sent to Inpaint", wait_for(page, lambda: "Sent to img2img Inpaint" in status_text(page) and "transparent pixels were filled" in status_text(page)), status_text(page))
    r.check(f"{label}: expanded image is wider by 128 and opaque",
            wait_for(page, lambda: (lambda bg: bg is not None and bg.size[0] == cropped[0] + 128 and bg.getchannel("A").getextrema() == (255, 255))(canvas_layer(page, inpaint.uuid, "logical_image_background"))))
    r.check(f"{label}: new area is masked in Inpaint",
            wait_for(page, lambda: (lambda fg: fg is not None and fg.size[0] == cropped[0] + 128 and fg.getchannel("A").getpixel((fg.size[0] - 10, fg.size[1] // 2)) == 255)(canvas_layer(page, inpaint.uuid, "logical_image_foreground"))))

    # ---- clear and invert write the layer ----
    click_tab(page, "Mini Paint")
    page.locator("#minipaint_canvas_mask_clear").click()
    r.check(f"{label}: clear mask empties the layer", wait_for(page, lambda: "Mask cleared" in status_text(page) and (lambda fg: fg is None or fg.getchannel("A").getbbox() is None)(canvas_layer(page, uuid, "logical_image_foreground"))), status_text(page))
    page.locator("#minipaint_canvas_mask_invert").click()
    r.check(f"{label}: invert of nothing masks everything", wait_for(page, lambda: "Mask inverted" in status_text(page) and (lambda fg: fg is not None and fg.getchannel("A").getextrema() == (255, 255))(canvas_layer(page, uuid, "logical_image_foreground"))), status_text(page))

    # ---- reset, then a picture opened on the canvas itself ----
    page.locator("#minipaint_canvas_more").locator("button").first.click()
    time.sleep(0.3)
    page.locator("#minipaint_canvas_reset").click()
    r.check(f"{label}: reset goes back to the received image", wait_for(page, lambda: "as it arrived" in status_text(page) and debug(page)["orgWidth"] == 640 and debug(page)["mode"] == "crop"), status_text(page))
    if with_upload:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            sample_image(300, 200).save(handle.name)
        page.locator(f"#imageInput_{uuid}").set_input_files(handle.name)
        r.check(f"{label}: a picture opened on the canvas becomes the document", wait_for(page, lambda: "Opened." in status_text(page) and "300 × 200" in status_text(page)), status_text(page))
        r.check(f"{label}: and the previous one is one undo away", "one Undo away" in status_text(page))
        r.check(f"{label}: the canvas kept the opened picture (no reload)", debug(page)["orgWidth"] == 300)
        page.locator("#minipaint_canvas_undo").click()
        r.check(f"{label}: undo brings the received image back", wait_for(page, lambda: "Undid open" in status_text(page) and debug(page)["orgWidth"] == 640), status_text(page))

    # ---- focus mode ----
    page.locator("#minipaint_canvas_focus").click()
    r.check(f"{label}: focus mode fixes the root", wait_for(page, lambda: page.evaluate("() => document.querySelector('#minipaint_canvas_root').classList.contains('minipaint-focus')")))
    page.keyboard.press("Escape")
    r.check(f"{label}: escape leaves focus mode", wait_for(page, lambda: page.evaluate("() => !document.querySelector('#minipaint_canvas_root').classList.contains('minipaint-focus')")))

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
