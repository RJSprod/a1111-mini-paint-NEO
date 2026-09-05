"""Both frontends, built against the Gradio the host actually has.

Covers the compatibility promise: the setting exists and is saved, the touch
Canvas is the default, the legacy editor comes back when asked for, only one
of them is ever mounted, the whole page still assembles around either, the
receive buttons land in the host's output rows, and a Canvas that fails to
build leaves a working legacy tab and a working host behind.
"""

from harness import Results, setup_path

setup_path()

import json  # noqa: E402

import gradio as gr  # noqa: E402
from modules import script_callbacks, shared  # noqa: E402
from PIL import Image  # noqa: E402

import forge_like  # noqa: E402
from minipaint_neo import router, settings  # noqa: E402
from minipaint_neo.canvas import host, imaging  # noqa: E402


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
    r.check("brush size 0 is auto", settings.brush_size() == "auto")

    # ---- the mask survives a trip through the component ----
    photo = Image.new("RGB", (64, 48), (200, 30, 30))
    drawn = Image.new("L", (64, 48), 0)
    drawn.paste(255, (10, 10, 30, 30))
    with gr.Blocks():
        probe = gr.ImageEditor(type="pil", image_mode="RGBA", layers=False, format="png")
    returned = probe.preprocess(probe.postprocess(imaging.editor_value(photo, drawn, (255, 47, 47))))
    seen, seen_mask, notes = imaging.read_editor(returned)
    r.check("the editor keeps the image size", seen.size == (64, 48))
    r.check("the editor keeps the pixels", seen.convert("RGB").getpixel((0, 0)) == (200, 30, 30))
    r.check("the mask comes back unchanged", list(seen_mask.getdata()) == list(drawn.getdata()), str(notes))
    r.check("an empty document postprocesses to nothing", probe.postprocess(None) is None)

    # ---- the whole page, new UI ----
    script_callbacks.callbacks["after_component"][:] = [host.on_after_component]
    host.reset_capture()
    demo, refs = forge_like.build_host(router.on_ui_tabs)
    config = config_of(demo)
    ids = elem_ids(config)

    for needed in ["minipaint_canvas_root", "minipaint_canvas_editor", "minipaint_canvas_send",
                   "minipaint_canvas_open", "minipaint_canvas_undo", "minipaint_canvas_redo",
                   "minipaint_canvas_focus", "minipaint_canvas_focus_exit", "minipaint_canvas_status",
                   "minipaint_canvas_mode_crop", "minipaint_canvas_mode_mask", "minipaint_canvas_mode_expand",
                   "minipaint_canvas_crop_apply", "minipaint_canvas_expand_apply", "minipaint_canvas_mask_clear",
                   "minipaint_canvas_mask_invert", "minipaint_canvas_reset", "minipaint_canvas_destination",
                   "txt2img_send_to_minipaint", "img2img_send_to_minipaint", "extras_send_to_minipaint",
                   "tab_minipaint", "tab_txt2img", "tab_settings", "tab_extensions"]:
        r.check(f"component {needed}", needed in ids)
    r.check("the legacy iframe is not mounted", "a1111minipaint_main" not in ids)

    editor = [c for c in config["components"] if c["props"].get("elem_id") == "minipaint_canvas_editor"][0]
    props = editor["props"]
    r.check("the canvas is an ImageEditor", editor["type"] == "imageeditor", editor["type"])
    r.check("editor returns PIL", props.get("type") == "pil")
    r.check("editor keeps PNG", props.get("format") == "png", str(props.get("format")))
    r.check("editor has no layer UI", props.get("layers") is False)
    r.check("editor can crop", list(props.get("transforms") or []) == ["crop"])
    r.check("editor has no upload of its own", list(props.get("sources") or []) == [])
    r.check("brush colour is fixed", (props.get("brush") or {}).get("color_mode") == "fixed")
    r.check("editor starts hidden behind the placeholder", props.get("visible") is False)
    r.check("editor height follows the setting", props.get("height") == "70vh", str(props.get("height")))

    # the receive buttons sit in the host's rows, next to "send to extras"
    layout = json.dumps(config["layout"])
    by_id = {c["id"]: c for c in config["components"]}

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
    broken = [d for d in config["dependencies"] if any(i not in known for i in d["inputs"] + d["outputs"]) or any(t[0] not in known and t[0] is not None for t in d["targets"])]
    r.check("every event resolves inside the page", not broken, str(broken[:2]))

    def deps_targeting(elem_id, trigger="click"):
        cid = next(c["id"] for c in config["components"] if c["props"].get("elem_id") == elem_id)
        return [d for d in config["dependencies"] if [cid, trigger] in d["targets"]]

    send = deps_targeting("minipaint_canvas_send")
    r.check("send has one backend event", len(send) == 1 and send[0]["backend_fn"])
    outputs = {by_id[i]["props"].get("elem_id") for i in send[0]["outputs"]}
    inpaint_id = refs["init_img_with_mask"].background.elem_id
    r.check("send writes img2img's hidden image", refs["init_img"].background._id in send[0]["outputs"])
    r.check("send writes inpaint's image and mask", refs["init_img_with_mask"].background._id in send[0]["outputs"] and refs["init_img_with_mask"].foreground._id in send[0]["outputs"], str(outputs))
    r.check("send writes extras", refs["extras_image"]._id in send[0]["outputs"])
    follow = [d for d in config["dependencies"] if d.get("trigger_after") == send[0]["id"]]
    r.check("send is followed by the host's tab switch", follow and "switchTo" in (follow[0].get("js") or ""), str([f.get("js") for f in follow]))

    receive = deps_targeting("txt2img_send_to_minipaint")
    r.check("receive starts with a flush that has a backend step", len(receive) == 1 and "flushEditor" in receive[0]["js"] and receive[0]["backend_fn"])
    chain1 = [d for d in config["dependencies"] if d.get("trigger_after") == receive[0]["id"]]
    r.check("then transfers from the gallery", chain1 and refs["txt2img_gallery"]._id in chain1[0]["inputs"] and "pickGalleryImage" in chain1[0]["js"])
    chain2 = [d for d in config["dependencies"] if d.get("trigger_after") == chain1[0]["id"]] if chain1 else []
    r.check("then switches to the Canvas tab", chain2 and "switchTo('canvas')" in chain2[0]["js"])

    apply_crop = deps_targeting("minipaint_canvas_crop_apply")
    step2 = [d for d in config["dependencies"] if d.get("trigger_after") == apply_crop[0]["id"]] if apply_crop else []
    step3 = [d for d in config["dependencies"] if d.get("trigger_after") == step2[0]["id"]] if step2 else []
    r.check("apply crop reads the editor first", apply_crop and editor["id"] in apply_crop[0]["inputs"])
    r.check("then flushes with a backend step", step2 and "flushEditor" in step2[0]["js"] and step2[0]["backend_fn"])
    r.check("then commits back into the editor", step3 and editor["id"] in step3[0]["outputs"] and step3[0]["backend_fn"])

    for mode in ("crop", "mask", "expand"):
        deps = deps_targeting(f"minipaint_canvas_mode_{mode}")
        r.check(f"{mode} mode has a backend switch and a browser tool select",
                any(d["backend_fn"] for d in deps) and any(f"onMode('{mode}')" in (d.get("js") or "") for d in deps))

    r.check("no change/input event is bound to the editor",
            not any([editor["id"], "change"] in d["targets"] or [editor["id"], "input"] in d["targets"] for d in config["dependencies"]))
    r.check("no load event was added by the extension", not any("minipaint" in (d.get("js") or "") and any(t[1] == "load" for t in d["targets"]) for d in config["dependencies"]))

    # ---- the whole page, legacy UI ----
    shared.opts.data[settings.USE_OLD_UI] = True
    script_callbacks.callbacks["after_component"][:] = []
    host.reset_capture()
    legacy_demo, _ = forge_like.build_host(router.on_ui_tabs)
    lconfig = config_of(legacy_demo)
    lids = elem_ids(lconfig)
    r.check("legacy mounts the iframe html", "a1111minipaint_main" in lids)
    r.check("legacy passes the controlnet count", "a1111minipaint_controlnet_max" in lids)
    r.check("legacy does not mount the new canvas", "minipaint_canvas_editor" not in lids)
    r.check("legacy has no receive buttons from us", "txt2img_send_to_minipaint" not in lids)
    r.check("legacy keeps the same tab", "tab_minipaint" in lids)
    lknown = {c["id"] for c in lconfig["components"]}
    r.check("legacy page events all resolve", not [d for d in lconfig["dependencies"] if any(i not in lknown for i in d["inputs"] + d["outputs"])])
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
    r.check("the new canvas is not also mounted", "minipaint_canvas_editor" not in bids)
    r.check("the host's other tabs are intact", {"tab_txt2img", "tab_img2img", "tab_settings", "tab_extensions"} <= bids)
    bknown = {c["id"] for c in bconfig["components"]}
    r.check("no dangling events after the failure", not [d for d in bconfig["dependencies"] if any(i not in bknown for i in d["inputs"] + d["outputs"])])

    # ---- a Gradio without ImageEditor picks legacy instead of failing ----
    editor_cls = gr.ImageEditor
    try:
        del gr.ImageEditor
        r.check("a missing component is named, not raised", "ImageEditor" in router.missing_components())
        older = router.on_ui_tabs()
        older_ids = elem_ids(config_of(older[0][0]))
        r.check("an older Gradio gets the legacy editor", "a1111minipaint_main" in older_ids)
        r.check("and is told why", "minipaint_fallback_warning" in older_ids)
    finally:
        gr.ImageEditor = editor_cls

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
