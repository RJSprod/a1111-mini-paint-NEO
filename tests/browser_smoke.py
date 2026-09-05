"""Drive the real extension in a real browser, inside a Forge-shaped page.

    python tests/browser_smoke.py [--keep] [--port 7860]

Needs Gradio, Pillow and Playwright with a Chromium. Optionally set
FORGE_ROOT to a Forge Neo checkout to use its own script.js and ui.js; without
it, small stand-ins provide the handful of helpers the extension calls
(gradioApp, switch_to_*, extract_image_from_gallery).

Order matters and is the one the tab-bar notes ask for: every top-level tab
must switch before a single Canvas feature is exercised, and that is checked
again with WebGL disabled, where the editor must stay unmounted and the tabs
must still work.
"""

from __future__ import annotations

import argparse
import base64
import io
import os
import pathlib
import sys
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

STAND_IN_JS = r"""
function gradioApp() {
    const elems = document.getElementsByTagName("gradio-app");
    const elem = elems.length == 0 ? document : elems[0];
    if (elem !== document) { elem.getElementById = function (id) { return document.getElementById(id); }; }
    return elem.shadowRoot ? elem.shadowRoot : elem;
}
function all_gallery_buttons() {
    const tab = gradioApp().querySelector('#tabs > .tabitem[id^=tab_]:not([style*="display: none"])');
    return Array.from((tab || gradioApp()).querySelectorAll(".thumbnail-item.thumbnail-small"));
}
function selected_gallery_index() {
    return all_gallery_buttons().findIndex((elem) => elem.classList.contains("selected"));
}
function extract_image_from_gallery(gallery) {
    if (gallery.length === 0) { return [null]; }
    let index = selected_gallery_index();
    if (index < 0 || index >= gallery.length) { index = 0; }
    return [[gallery[index]]];
}
function switch_to_img2img_tab(no) {
    gradioApp().querySelector("#tabs").querySelectorAll("button")[1].click();
    gradioApp().getElementById("mode_img2img").querySelectorAll("button")[no].click();
}
function switch_to_img2img() { switch_to_img2img_tab(0); return Array.from(arguments); }
function switch_to_inpaint() { switch_to_img2img_tab(2); return Array.from(arguments); }
function switch_to_extras() { gradioApp().querySelector("#tabs").querySelectorAll("button")[2].click(); return Array.from(arguments); }
"""


def head_html() -> str:
    forge_root = os.environ.get("FORGE_ROOT")
    parts = []
    if forge_root and (pathlib.Path(forge_root) / "javascript" / "ui.js").exists():
        for name in ("script.js", "javascript/ui.js"):
            parts.append("<script>" + (pathlib.Path(forge_root) / name).read_text(encoding="utf-8") + "</script>")
        print(f"using Forge's own script.js and ui.js from {forge_root}")
    else:
        parts.append("<script>" + STAND_IN_JS + "</script>")
    parts.append("<script>" + (ROOT / "javascript" / "minipaint_canvas.js").read_text(encoding="utf-8") + "</script>")
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

    def tabs():
        return router.on_ui_tabs()

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
        demo, refs = forge_like.build_host(tabs, extra_head=head_html())
    finally:
        forge_like.output_panel = original
    return demo, refs


def decode_data_url(text: str):
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


def wait_for(page, predicate, timeout=15.0, step=0.25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def textarea_value(page, elem_id, klass):
    return page.evaluate(f"() => {{ const t = document.querySelector('#{elem_id}.{klass} textarea'); return t ? t.value : null; }}")


def drag(page, x1, y1, x2, y2, steps=12):
    page.mouse.move(x1, y1)
    page.mouse.down()
    for i in range(1, steps + 1):
        page.mouse.move(x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps)
        time.sleep(0.02)
    page.mouse.up()


def touch_drag(page, x1, y1, x2, y2, steps=12):
    """A one-finger drag through the Chrome DevTools protocol (Playwright has no touch drag)."""
    cdp = page.context.new_cdp_session(page)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x1, "y": y1}]})
    for i in range(1, steps + 1):
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x1 + (x2 - x1) * i / steps, "y": y1 + (y2 - y1) * i / steps}]})
        time.sleep(0.02)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    cdp.detach()


def editor_canvas_box(page):
    return page.locator("#minipaint_canvas_editor .stage-wrap canvas").first.bounding_box()


def run_new_ui(r: Results, port: int, chromium: str, keep: bool) -> None:
    from playwright.sync_api import sync_playwright

    demo, refs = build_with_generator(old_ui=False)
    demo.queue().launch(server_name="127.0.0.1", server_port=port, prevent_thread_lock=True, quiet=True,
                        allowed_paths=[str(ROOT)])
    try:
        with sync_playwright() as p:
            # ---- 1. WebGL available: tabs first, then features ----
            browser = p.chromium.launch(executable_path=chromium, headless=not keep)
            context = browser.new_context(viewport={"width": 1280, "height": 900}, has_touch=True)
            page = context.new_page()
            errors = []

            def note_error(error):
                text = str(error)
                # Forge's own script.js handles Escape by reading the current
                # tab's Interrupt button, which no extension tab has; that
                # console error is the host's, and happens on every
                # extension tab in a real install too.
                if "reading 'style'" in text and os.environ.get("FORGE_ROOT"):
                    return
                errors.append(text)

            page.on("pageerror", note_error)
            page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
            page.wait_for_selector("#tabs .tab-nav button", timeout=30000)
            time.sleep(3)
            check_tabs(r, page, "webgl")
            r.check("webgl: editor mounted", page.evaluate("() => !!document.querySelector('#minipaint_canvas_editor canvas')"))
            r.check("webgl: editor hidden until an image arrives", page.evaluate("() => getComputedStyle(document.querySelector('#minipaint_canvas_editor')).display === 'none'"))
            r.check("webgl: placeholder shown", page.evaluate("() => getComputedStyle(document.querySelector('#minipaint_canvas_empty')).display !== 'none'"))

            # receive from txt2img
            click_tab(page, "txt2img")
            page.locator("#probe_make").click()
            r.check("a gallery image appears", wait_for(page, lambda: page.locator("#txt2img_gallery .thumbnail-item").count() > 0))
            page.locator("#txt2img_gallery .thumbnail-item").first.click()
            time.sleep(0.5)
            page.locator("#txt2img_send_to_minipaint").click()
            r.check("receive lands in the Canvas tab", wait_for(page, lambda: visible_panels(page) == ["tab_minipaint"]), str(visible_panels(page)))
            r.check("status reports the receive", wait_for(page, lambda: "Received from txt2img" in status_text(page)), status_text(page))
            r.check("editor is visible now", wait_for(page, lambda: page.evaluate("() => getComputedStyle(document.querySelector('#minipaint_canvas_editor')).display !== 'none'")))
            r.check("editor shows a 640x480 canvas", wait_for(page, lambda: page.evaluate("() => { const c = document.querySelector('#minipaint_canvas_editor .stage-wrap canvas'); return !!c && c.width >= 640 && c.height >= 480; }")),
                    str(page.evaluate("() => { const c = document.querySelector('#minipaint_canvas_editor .stage-wrap canvas'); return c && [c.width, c.height]; }")))
            box = editor_canvas_box(page)
            r.check("canvas fits inside the editor block",
                    box is not None and box["height"] <= page.locator("#minipaint_canvas_editor").bounding_box()["height"] + 1,
                    str(box))

            # crop with the mouse: select the crop tool, drag the bottom-right handle inwards
            page.locator("#minipaint_canvas_mode_crop").click()
            time.sleep(1.0)
            r.check("crop mode shows the crop handles", wait_for(page, lambda: page.locator("#minipaint_canvas_editor .hitbox").count() > 0, timeout=6))
            handle = page.locator("#minipaint_canvas_editor .hitbox.br").first.bounding_box()
            if handle:
                cx, cy = handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2
                drag(page, cx, cy, cx - 120, cy - 90)
                time.sleep(1.5)  # the cropper commits a drag after a second of stillness
            page.locator("#minipaint_canvas_crop_apply").click()
            r.check("apply crop reports a smaller image", wait_for(page, lambda: "Cropped to" in status_text(page)), status_text(page))
            crop_status = status_text(page)
            r.check("the crop is smaller than 640x480", "640 × 480" not in crop_status.split("Cropped to")[-1] if "Cropped to" in crop_status else False, crop_status)
            r.check("crop box reset after apply", page.evaluate("() => { const c = document.querySelector('#minipaint_canvas_editor .canvas'); const s = document.querySelector('#minipaint_canvas_editor .stage-wrap'); if (!c || !s) return false; const a = c.getBoundingClientRect(), b = s.getBoundingClientRect(); return Math.abs(a.width - b.width) < 6 && Math.abs(a.height - b.height) < 6; }"))

            # undo the crop structurally
            page.locator("#minipaint_canvas_undo").click()
            r.check("undo restores 640x480", wait_for(page, lambda: "Undid crop" in status_text(page) and "640 × 480" in status_text(page)), status_text(page))

            # touch crop: same drag with a finger
            page.locator("#minipaint_canvas_mode_crop").click()
            time.sleep(1.0)
            handle = page.locator("#minipaint_canvas_editor .hitbox.br").first.bounding_box()
            if handle:
                cx, cy = handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2
                touch_drag(page, cx, cy, cx - 100, cy - 60)
                time.sleep(1.5)
            page.locator("#minipaint_canvas_crop_apply").click()
            r.check("a finger can crop", wait_for(page, lambda: "Cropped to" in status_text(page) and "640 × 480" not in status_text(page).split("Cropped to")[-1]), status_text(page))

            # mask: draw a stroke, send, and check what Inpaint received
            page.locator("#minipaint_canvas_mode_mask").click()
            time.sleep(0.8)
            r.check("mask mode relabels send", wait_for(page, lambda: "Inpaint" in page.locator("#minipaint_canvas_send").inner_text()))
            box = editor_canvas_box(page)
            if box:
                x, y = box["x"] + box["width"] * 0.3, box["y"] + box["height"] * 0.5
                page.mouse.click(box["x"] + 5, box["y"] + 5)  # close the brush options popup if it opened
                time.sleep(0.3)
                drag(page, x, y, x + box["width"] * 0.3, y, steps=20)
                time.sleep(0.5)
            page.locator("#minipaint_canvas_send").click()
            r.check("send reports Inpaint", wait_for(page, lambda: "Sent to img2img Inpaint" in status_text(page)), status_text(page))
            r.check("send switches to the img2img tab", wait_for(page, lambda: visible_panels(page) == ["tab_img2img"]), str(visible_panels(page)))
            r.check("and to its Inpaint sub-tab", wait_for(page, lambda: page.evaluate("() => { const b = document.querySelector('#mode_img2img > .tab-nav > button.selected'); return b && b.textContent.trim() === 'Inpaint'; }")))
            bg = decode_data_url(textarea_value(page, refs["init_img_with_mask"].uuid, "logical_image_background"))
            fg = decode_data_url(textarea_value(page, refs["init_img_with_mask"].uuid, "logical_image_foreground"))
            r.check("inpaint got the image", bg is not None and bg.size[0] < 640, str(bg and bg.size))
            r.check("inpaint got a mask the same size", fg is not None and bg is not None and fg.size == bg.size, str(fg and fg.size))
            r.check("the mask has coverage", fg is not None and fg.getchannel("A").getbbox() is not None)

            # expand: back to the canvas, add 128 on the right, apply, send again
            click_tab(page, "Mini Paint")
            page.locator("#minipaint_canvas_mode_expand").click()
            time.sleep(0.5)
            page.locator("#minipaint_canvas_expand_right").click()
            r.check("side button previews the size", wait_for(page, lambda: "→" in page.evaluate("() => document.querySelector('#minipaint_canvas_expand_preview').innerText")))
            page.locator("#minipaint_canvas_expand_apply").click()
            r.check("expand applies", wait_for(page, lambda: "Expanded to" in status_text(page)), status_text(page))
            r.check("expand lands in mask mode", wait_for(page, lambda: "Outpaint" in page.locator("#minipaint_canvas_send").inner_text()))
            page.locator("#minipaint_canvas_send").click()
            r.check("outpaint sent to Inpaint", wait_for(page, lambda: "Sent to img2img Inpaint" in status_text(page)), status_text(page))
            bg2 = decode_data_url(textarea_value(page, refs["init_img_with_mask"].uuid, "logical_image_background"))
            fg2 = decode_data_url(textarea_value(page, refs["init_img_with_mask"].uuid, "logical_image_foreground"))
            r.check("expanded image is wider by 128", bg is not None and bg2 is not None and bg2.size[0] == bg.size[0] + 128, str((bg and bg.size, bg2 and bg2.size)))
            r.check("expanded image has no transparency", bg2 is not None and bg2.getchannel("A").getextrema() == (255, 255))
            r.check("new area is masked", fg2 is not None and fg2.getchannel("A").getpixel((fg2.size[0] - 10, fg2.size[1] // 2)) == 255)

            # focus mode
            click_tab(page, "Mini Paint")
            page.locator("#minipaint_canvas_focus").click()
            r.check("focus mode fixes the root", wait_for(page, lambda: page.evaluate("() => document.querySelector('#minipaint_canvas_root').classList.contains('minipaint-focus')")))
            page.keyboard.press("Escape")
            r.check("escape leaves focus mode", wait_for(page, lambda: page.evaluate("() => !document.querySelector('#minipaint_canvas_root').classList.contains('minipaint-focus')")))

            check_tabs(r, page, "after use")
            r.check("no page errors", not errors, "; ".join(e[:120] for e in errors[:3]))
            if keep:
                input("press Enter to close the browser...")
            browser.close()

            # ---- 2. no WebGL: the editor must not mount, tabs must still work ----
            browser = p.chromium.launch(executable_path=chromium, args=["--disable-3d-apis"])
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
            page.wait_for_selector("#tabs .tab-nav button", timeout=30000)
            time.sleep(3)
            r.check("no-webgl: browser really has no WebGL", not page.evaluate("() => window.minipaintCanvas.webglAvailable()"))
            check_tabs(r, page, "no-webgl")
            r.check("no-webgl: editor was not mounted", not page.evaluate("() => !!document.querySelector('#minipaint_canvas_editor canvas')"))
            r.check("no-webgl: notice shown", page.evaluate("() => !!document.querySelector('.minipaint-nowebgl')"))
            r.check("no-webgl: receive button disabled", page.evaluate("() => document.querySelector('#txt2img_send_to_minipaint').disabled"))
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
            browser = p.chromium.launch(executable_path=chromium)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
            page.wait_for_selector("#tabs .tab-nav button", timeout=30000)
            time.sleep(3)
            check_tabs(r, page, "legacy")
            r.check("legacy: iframe mounted", page.evaluate("() => !!document.querySelector('#a1111minipaint_iframe')"))
            r.check("legacy: no touch editor", not page.evaluate("() => !!document.querySelector('#minipaint_canvas_root')"))
            r.check("legacy: no receive buttons from the new UI", not page.evaluate("() => !!document.querySelector('#txt2img_send_to_minipaint')"))
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
