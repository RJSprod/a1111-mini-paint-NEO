"""Both frontends, built against the Gradio the host actually has.

Covers the compatibility promise: the setting exists and is saved, the touch
Canvas is the default, the legacy editor comes back when asked for, only one
of them is ever mounted, the whole page still assembles around either, the
receive buttons land in the host's output rows, the Canvas is the host's own
canvas wired the way the timing needs, and a Canvas that fails to build
leaves a working legacy tab and a working host behind.
"""

from harness import Results, setup_path

setup_path()

import json  # noqa: E402
import pathlib  # noqa: E402

import forge_like  # noqa: E402  (first: it applies Forge's metaclass patches before the canvas stub is defined)
from modules import script_callbacks, shared  # noqa: E402
from modules_forge.forge_canvas import canvas as forge_canvas  # noqa: E402
from PIL import Image  # noqa: E402
from minipaint_neo import router, settings  # noqa: E402
from minipaint_neo.canvas import host, surface  # noqa: E402


def config_of(blocks) -> dict:
    return json.loads(json.dumps(blocks.get_config_file(), default=str))


def elem_ids(config) -> set:
    return {c["props"].get("elem_id") for c in config["components"] if c.get("props")}


def run() -> Results:
    r = Results("frontends")

    # ---- settings registration ----
    settings.on_ui_settings()
    labels = shared.opts.data_labels
    r.check("old-ui option registered", settings.USE_OLD_UI in labels)
    r.check("old-ui default is the new UI", shared.opts.data[settings.USE_OLD_UI] is False)
    r.check("old-ui needs a reload", labels[settings.USE_OLD_UI].reload_ui is True)
    r.check("section is miniPaint / Canvas", labels[settings.USE_OLD_UI].section == ("minipaint_canvas", "miniPaint / Canvas"))
    r.check("every option is in that section", all(i.section == settings.SECTION for i in labels.values()))
    r.check("use_old_ui reads False", settings.use_old_ui() is False)
    r.check("unknown category is not claimed", settings._category("no-such-category") is None)
    r.check("get falls back", settings.get("no_such_option", "fallback") == "fallback")
    r.check("brush width default", settings.brush_width() == 25)
    r.check("canvas height default", settings.canvas_height_percent() == 70)
    shared.opts.data[settings.BRUSH_SIZE] = 500
    shared.opts.data[settings.CANVAS_HEIGHT] = "nonsense"
    r.check("brush width is clamped", settings.brush_width() == 100)
    r.check("a bad height falls back", settings.canvas_height_percent() == 70)
    shared.opts.data[settings.BRUSH_SIZE] = 25
    shared.opts.data[settings.CANVAS_HEIGHT] = 70

    # ---- the host's canvas is found, and its mask style is read ----
    r.check("the host canvas is present", surface.missing() == "")
    style = surface.host_mask_style()
    r.check("mask style follows the Inpaint settings", style == {"color": "#808080", "alpha": 75, "contrast": True, "consistent": False}, str(style))

    # ---- an image survives a trip through the host's hidden textbox ----
    photo = Image.new("RGB", (64, 48), (200, 30, 30))
    probe = forge_canvas.LogicalImage(numpy=False)
    returned = probe.preprocess(probe.postprocess(photo))
    r.check("the textbox keeps the image", returned.size == (64, 48) and returned.getpixel((0, 0)) == (200, 30, 30, 255))
    r.check("the textbox refuses non-images", probe.preprocess("echo") is None and probe.preprocess("") is None)
    r.check("nothing postprocesses to nothing", probe.postprocess(None) is None)

    # ---- the whole page, new UI ----
    script_callbacks.callbacks["after_component"][:] = [host.on_after_component]
    host.reset_capture()
    demo, refs = forge_like.build_host(router.on_ui_tabs)
    config = config_of(demo)
    ids = elem_ids(config)

    for needed in ["minipaint_canvas_root", "minipaint_canvas_surface", "minipaint_canvas_send",
                   "minipaint_canvas_open", "minipaint_canvas_undo", "minipaint_canvas_redo", "minipaint_canvas_fit",
                   "minipaint_canvas_focus", "minipaint_canvas_focus_exit", "minipaint_canvas_status",
                   "minipaint_canvas_mode_crop", "minipaint_canvas_mode_mask", "minipaint_canvas_mode_expand",
                   "minipaint_canvas_crop_apply", "minipaint_canvas_crop_aspect", "minipaint_canvas_expand_apply",
                   "minipaint_canvas_mask_tool", "minipaint_canvas_mask_size", "minipaint_canvas_mask_clear",
                   "minipaint_canvas_mask_invert", "minipaint_canvas_reset", "minipaint_canvas_destination",
                   "minipaint_canvas_mode", "minipaint_canvas_crop_box", "minipaint_canvas_original_size",
                   "minipaint_canvas_wait", "minipaint_canvas_switch", "minipaint_canvas_event",
                   "minipaint_canvas_quick_crop", "minipaint_canvas_quick_mask", "minipaint_canvas_quick_expand",
                   "minipaint_canvas_options", "minipaint_canvas_panel_crop", "minipaint_canvas_panel_mask",
                   "minipaint_canvas_panel_expand", "minipaint_canvas_more",
                   "txt2img_send_to_minipaint", "img2img_send_to_minipaint", "extras_send_to_minipaint",
                   "tab_minipaint", "tab_txt2img", "tab_settings", "tab_extensions"]:
        r.check(f"component {needed}", needed in ids)
    r.check("the legacy iframe is not mounted", "a1111minipaint_main" not in ids)
    r.check("no ImageEditor anywhere in the page", "imageeditor" not in {c["type"] for c in config["components"]})

    by_id = {c["id"]: c for c in config["components"]}

    def component(elem_id, klass=None):
        for c in config["components"]:
            if c["props"].get("elem_id") == elem_id and (klass is None or klass in (c["props"].get("elem_classes") or [])):
                return c
        return None

    # the surface: the host's markup with our id, and the two hidden textboxes
    surface_block = component("minipaint_canvas_surface")
    r.check("the surface is an HTML block", surface_block and surface_block["type"] == "html")
    html = (surface_block or {}).get("props", {}).get("value", "")
    uuid = None
    for c in config["components"]:
        if c["type"] == "textbox" and "logical_image_background" in (c["props"].get("elem_classes") or []) and f'id="container_{c["props"].get("elem_id")}"' in html:
            uuid = c["props"]["elem_id"]
    r.check("the surface carries its own canvas id", uuid is not None and uuid.startswith("uuid_"))
    background = component(uuid, "logical_image_background")
    foreground = component(uuid, "logical_image_foreground")
    r.check("background and foreground textboxes exist and are hidden",
            background and foreground and background["props"].get("visible") is False and foreground["props"].get("visible") is False)
    r.check("the toolbar is always visible", 'class="forge-toolbar-static"' in html and 'class="forge-toolbar"' not in html)
    r.check("the remove button is still in the markup (hidden by css)", f'removeButton_{uuid}' in html)

    # exactly one load event of ours, calling attach with the options
    loads = [d for d in config["dependencies"] if any(t[1] == "load" for t in d["targets"]) and "minipaintCanvas" in (d.get("js") or "")]
    r.check("one load event attaches the canvas", len(loads) == 1 and not loads[0]["backend_fn"], str(len(loads)))
    r.check("attach gets the id and the Inpaint brush style",
            loads and f'"{uuid}"' in loads[0]["js"] and '"contrast": true' in loads[0]["js"] and '"alpha": 75' in loads[0]["js"] and '"heightPercent": 70' in loads[0]["js"])
    r.check("attach is told to fit the window", loads and '"fit": true' in loads[0]["js"])

    # nothing in the tab is positioned over the canvas: no sticky or fixed rule outside focus mode
    css = (config_of(demo) and (pathlib.Path(__file__).resolve().parents[1] / "style.css").read_text(encoding="utf-8"))
    r.check("no sticky rules in the stylesheet", "sticky" not in css)
    fixed_blocks = [block for block in css.split("}") if "position: fixed" in block]
    r.check("the only fixed rule is focus mode", len(fixed_blocks) == 1 and "minipaint-focus" in fixed_blocks[0].split("{")[0])

    # the receive buttons sit in the host's rows, next to "send to extras"
    def row_children(elem_id):
        def find(node):
            comp = by_id.get(node["id"])
            if comp and comp["props"].get("elem_id") == elem_id:
                return node
            for child in node.get("children", []):
                found = find(child)
                if found:
                    return found
            return None
        node = find(config["layout"])
        return [by_id[c["id"]]["props"].get("elem_id") for c in node.get("children", [])] if node else []

    for tab in ("txt2img", "img2img", "extras"):
        children = row_children(f"image_buttons_{tab}")
        r.check(f"{tab} receive button is in the output row, after send-to-extras",
                children[-2:] == [f"{tab}_send_to_extras", f"{tab}_send_to_minipaint"], str(children))

    # every dependency resolves to a component in the page
    known = {c["id"] for c in config["components"]}
    # (a load event's target is the Blocks it was declared in, which is not a
    # component; that is how the host's own canvases are attached too)
    broken = [d for d in config["dependencies"] if any(i not in known for i in d["inputs"] + d["outputs"])
              or any(t[0] not in known and t[0] is not None and t[1] != "load" for t in d["targets"])]
    r.check("every event resolves inside the page", not broken, str(broken[:2]))

    deps = config["dependencies"]

    def deps_targeting(cid, trigger="click"):
        return [d for d in deps if [cid, trigger] in d["targets"]]

    def by_elem(elem_id, trigger="click"):
        return deps_targeting(component(elem_id)["id"], trigger)

    def followers(dep):
        return [d for d in deps if d.get("trigger_after") == dep["id"]]

    def chain(dep):
        steps = [dep]
        while True:
            nxt = followers(steps[-1])
            if not nxt:
                return steps
            steps.append(nxt[0])

    status_id = component("minipaint_canvas_status")["id"]
    wait_id = component("minipaint_canvas_wait")["id"]
    mode_id = component("minipaint_canvas_mode")["id"]

    # -- a structural step: image -> wait for the canvas -> mask layer
    apply_crop = by_elem("minipaint_canvas_crop_apply")
    steps = chain(apply_crop[0]) if apply_crop else []
    r.check("apply crop is one chain of three backend steps", len(apply_crop) == 1 and len(steps) == 3 and all(s["backend_fn"] for s in steps), str(len(steps)))
    r.check("apply crop reads the canvas and the frame",
            steps and background["id"] in steps[0]["inputs"] and foreground["id"] in steps[0]["inputs"]
            and component("minipaint_canvas_crop_box")["id"] in steps[0]["inputs"] and "cropBox()" in steps[0]["js"] and "mark()" in steps[0]["js"])
    r.check("apply crop writes the image, the status and the wait flag",
            steps and {background["id"], foreground["id"], status_id, wait_id, mode_id} <= set(steps[0]["outputs"]))
    r.check("then waits for the canvas", len(steps) > 1 and "waitForImage" in steps[1]["js"] and steps[1]["inputs"] == [wait_id])
    r.check("then writes the mask layer only", len(steps) > 2 and steps[2]["outputs"] == [foreground["id"]] and not steps[2].get("js"))

    for elem_id in ("minipaint_canvas_undo", "minipaint_canvas_redo", "minipaint_canvas_reset", "minipaint_canvas_expand_apply"):
        d = by_elem(elem_id)
        r.check(f"{elem_id} is the same three-step chain", len(d) == 1 and len(chain(d[0])) == 3 and "mark()" in d[0]["js"])
    for elem_id, helper in (("minipaint_canvas_undo", "undoStroke"), ("minipaint_canvas_redo", "redoStroke")):
        d = by_elem(elem_id)[0]
        r.check(f"{elem_id} tries the canvas's stroke history first and says which it did",
                helper in d["js"] and component("minipaint_canvas_event")["id"] in d["inputs"])
    opened = by_elem("minipaint_canvas_open", "upload")
    r.check("open is the same chain on upload", len(opened) == 1 and len(chain(opened[0])) == 3)

    # -- receive: pick from the gallery, the chain, then the host's tab switch
    receive = by_elem("txt2img_send_to_minipaint")
    steps = chain(receive[0]) if receive else []
    r.check("receive picks the gallery image in the browser", len(receive) == 1 and "pickGalleryImage" in receive[0]["js"] and refs["txt2img_gallery"]._id in receive[0]["inputs"] and receive[0]["backend_fn"])
    r.check("receive ends by switching to the Canvas tab", len(steps) == 4 and "switchTo('canvas')" in (steps[3].get("js") or "") and not steps[3]["backend_fn"], str(len(steps)))

    # -- what the canvas holds: the input event, filtered in the browser
    canvas_input = deps_targeting(background["id"], "input")
    r.check("the canvas image has one input handler", len(canvas_input) == 1 and canvas_input[0]["backend_fn"])
    r.check("it is filtered in the browser and carries the kind",
            canvas_input and "canvasInput" in canvas_input[0]["js"] and component("minipaint_canvas_event")["id"] in canvas_input[0]["inputs"])
    r.check("it can clear the strokes and updates the status", canvas_input and {foreground["id"], status_id} <= set(canvas_input[0]["outputs"]))
    r.check("no change handler is bound to the canvas image", not deps_targeting(background["id"], "change"))
    r.check("nothing is bound to the mask layer", not deps_targeting(foreground["id"], "input") and not deps_targeting(foreground["id"], "change"))

    # -- modes: a backend switch on the buttons, the browser following the mode textbox
    options_id = component("minipaint_canvas_options")["id"]
    for mode in ("crop", "mask", "expand"):
        d = by_elem(f"minipaint_canvas_mode_{mode}")
        r.check(f"{mode} mode button has one backend event that writes the mode", len(d) == 1 and d[0]["backend_fn"] and mode_id in d[0]["outputs"])
        r.check(f"{mode} mode switch shows its quick row, its panel and relabels the options",
                d and {component(f"minipaint_canvas_quick_{mode}")["id"], component(f"minipaint_canvas_panel_{mode}")["id"], options_id} <= set(d[0]["outputs"]))
    r.check("the options accordion starts closed", component("minipaint_canvas_options")["props"].get("open") is False)
    r.check("the aspect is a dropdown in the crop quick row", component("minipaint_canvas_crop_aspect")["type"] == "dropdown")
    mode_change = deps_targeting(mode_id, "change")
    r.check("the browser follows the mode textbox", len(mode_change) == 1 and "onMode" in mode_change[0]["js"] and not mode_change[0]["backend_fn"])

    # -- mask tool and size are browser-only; aspect too
    tool = deps_targeting(component("minipaint_canvas_mask_tool")["id"], "change")
    r.check("tool change is browser-only", len(tool) == 1 and "setTool" in tool[0]["js"] and not tool[0]["backend_fn"])
    size_id = component("minipaint_canvas_mask_size")["id"]
    r.check("brush size is browser-only", all("setBrushSize" in d["js"] and not d["backend_fn"] for d in deps_targeting(size_id, "change") + deps_targeting(size_id, "release")) and deps_targeting(size_id, "release"))
    aspect = deps_targeting(component("minipaint_canvas_crop_aspect")["id"], "change")
    r.check("aspect is browser-only and reads the original size", len(aspect) == 1 and "setAspect" in aspect[0]["js"] and component("minipaint_canvas_original_size")["id"] in aspect[0]["inputs"])
    for elem_id, js in (("minipaint_canvas_fit", "fit()"), ("minipaint_canvas_focus", "setFocus(true)"), ("minipaint_canvas_focus_exit", "setFocus(false)")):
        d = by_elem(elem_id)
        r.check(f"{elem_id} is browser-only", len(d) == 1 and js in d[0]["js"] and not d[0]["backend_fn"])

    # -- send: the image into the host's inputs, the tab switch, the mask once Inpaint has the image
    send = by_elem("minipaint_canvas_send")
    r.check("send has one backend event", len(send) == 1 and send[0]["backend_fn"])
    outputs = set(send[0]["outputs"]) if send else set()
    switch_id = component("minipaint_canvas_switch")["id"]
    payload_id = component("minipaint_canvas_payload")["id"]
    inpaint = refs["init_img_with_mask"]
    host_boxes = {refs["init_img"].background._id, inpaint.background._id, inpaint.foreground._id}
    r.check("the backend never writes the host's hidden image textboxes", not (host_boxes & outputs))
    r.check("send writes extras from the backend", refs["extras_image"]._id in outputs)
    r.check("send writes the instruction and the image payload", {switch_id, payload_id} <= outputs)
    follow = followers(send[0]) if send else []
    deliver = [f for f in follow if set(f["outputs"]) == {refs["init_img"].background._id, inpaint.background._id}]
    r.check("a browser-only step writes the chosen host textbox and leaves the other untouched",
            len(deliver) == 1 and not deliver[0]["backend_fn"] and '"__type__": "update"' in deliver[0]["js"] and deliver[0]["inputs"] == [switch_id, payload_id])
    r.check("send is followed by the tab switch", any("switchTo" in (f.get("js") or "") and not f["backend_fn"] for f in follow))
    waits = [f for f in follow if "waitForHostImage" in (f.get("js") or "")]
    r.check("and by a wait on the Inpaint canvas", len(waits) == 1 and waits[0]["backend_fn"] and f'"{inpaint.uuid}"' in waits[0]["js"])
    after_wait = followers(waits[0]) if waits else []
    mask_payload_id = component("minipaint_canvas_mask_payload")["id"]
    r.check("then the mask layer is prepared as a payload", len(after_wait) == 1 and after_wait[0]["outputs"] == [mask_payload_id] and after_wait[0]["backend_fn"])
    after_mask = followers(after_wait[0]) if after_wait else []
    r.check("and written into Inpaint from the browser", len(after_mask) == 1 and not after_mask[0]["backend_fn"] and after_mask[0]["outputs"] == [inpaint.foreground._id])
    r.check("no backend event anywhere writes a host image textbox", not any(d["backend_fn"] and (host_boxes & set(d["outputs"])) for d in deps))

    r.check("no javascript runs at startup apart from attaching the canvases",
            all("ForgeCanvas" in (d.get("js") or "") or "attach" in (d.get("js") or "") for d in deps if any(t[1] == "load" for t in d["targets"])))

    # ---- the whole page, legacy UI ----
    shared.opts.data[settings.USE_OLD_UI] = True
    script_callbacks.callbacks["after_component"][:] = []
    host.reset_capture()
    legacy_demo, _ = forge_like.build_host(router.on_ui_tabs)
    lconfig = config_of(legacy_demo)
    lids = elem_ids(lconfig)
    r.check("legacy mounts the iframe html", "a1111minipaint_main" in lids)
    r.check("legacy passes the controlnet count", "a1111minipaint_controlnet_max" in lids)
    r.check("legacy does not mount the new canvas", "minipaint_canvas_surface" not in lids and "minipaint_canvas_root" not in lids)
    r.check("legacy has no receive buttons from us", "txt2img_send_to_minipaint" not in lids)
    r.check("legacy keeps the same tab", "tab_minipaint" in lids)
    lknown = {c["id"] for c in lconfig["components"]}
    r.check("legacy page events all resolve", not [d for d in lconfig["dependencies"] if any(i not in lknown for i in d["inputs"] + d["outputs"])])
    r.check("legacy adds no load event", not any("minipaintCanvas" in (d.get("js") or "") for d in lconfig["dependencies"]))
    shared.opts.data[settings.USE_OLD_UI] = False

    # ---- a failing Canvas must not take the rest of the WebUI with it ----
    import minipaint_neo.canvas.ui as canvas_ui

    print("  (the traceback below is this test breaking the Canvas on purpose)")
    working = canvas_ui.create_ui
    canvas_ui.create_ui = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        script_callbacks.callbacks["after_component"][:] = [host.on_after_component]
        host.reset_capture()
        broken_demo, _ = forge_like.build_host(router.on_ui_tabs)
    finally:
        canvas_ui.create_ui = working
    bconfig = config_of(broken_demo)
    bids = elem_ids(bconfig)
    r.check("a broken Canvas still gives a tab", "tab_minipaint" in bids)
    r.check("the fallback is the legacy editor", "a1111minipaint_main" in bids)
    r.check("the fallback says so", "minipaint_fallback_warning" in bids)
    r.check("the new canvas is not also mounted", "minipaint_canvas_surface" not in bids)
    r.check("the host's other tabs are intact", {"tab_txt2img", "tab_img2img", "tab_settings", "tab_extensions"} <= bids)
    bknown = {c["id"] for c in bconfig["components"]}
    r.check("no dangling events after the failure", not [d for d in bconfig["dependencies"] if any(i not in bknown for i in d["inputs"] + d["outputs"])])

    # ---- a WebUI without Forge's canvas picks legacy instead of failing ----
    real_host_canvas = surface.host_canvas
    surface.host_canvas = lambda: None
    try:
        r.check("a missing canvas is named, not raised", "Forge Canvas" in router.missing_components())
        older = router.on_ui_tabs()
        older_ids = elem_ids(config_of(older[0][0]))
        r.check("a WebUI without it gets the legacy editor", "a1111minipaint_main" in older_ids)
        r.check("and is told why", "minipaint_fallback_warning" in older_ids)
    finally:
        surface.host_canvas = real_host_canvas

    # ---- the escape hatch that does not need a working UI ----
    import os

    os.environ[settings.OLD_UI_ENV] = "1"
    try:
        r.check("the environment can force the old UI", settings.use_old_ui() is True)
    finally:
        del os.environ[settings.OLD_UI_ENV]
    r.check("and stops forcing it when unset", settings.use_old_ui() is False)

    return r


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
