"""A small host shaped like Forge Neo's ``modules/ui.py``, for tests.

It installs the same two things Forge does to Gradio - the component hooks
that fire ``after_component`` callbacks, and the ``repair`` wrapper that drops
unknown constructor arguments and maps ``_js`` to ``js`` - then builds
txt2img, img2img (with the stand-in ForgeCanvas from the ``modules_forge``
stub), Extras and Settings, asks the extension for its tab, and assembles
everything under one ``gr.Tabs(elem_id="tabs")`` exactly the way the WebUI
does.

Nothing here is copied from Forge; it reproduces the shapes the extension
depends on so that they can be checked without a running WebUI.
"""

from __future__ import annotations

import inspect
import warnings
from functools import wraps

import gradio as gr
import gradio.blocks
import gradio.component_meta

from modules import infotext_utils, script_callbacks  # the stubs

_installed = False


def install_metaclass_patches() -> None:
    """What Forge's ``gradio_extensions`` does to Gradio's component metaclass,
    before any Forge component class exists: no .pyi files for classes that
    ask not to have one, and no recording of constructor arguments for
    classes defined from here on. The second is what makes a session rebuild
    of Forge's ``LogicalImage`` come back with ``numpy=True``."""
    if getattr(gradio.component_meta, "_forge_like_patched", False):
        return
    gradio.component_meta._forge_like_patched = True
    original = gradio.component_meta.create_or_modify_pyi

    def create_or_modify_pyi(component_class, class_name, events):
        if hasattr(component_class, "webui_do_not_create_gradio_pyi_thank_you"):
            return
        try:
            original(component_class, class_name, events)
        except Exception:
            return

    gradio.component_meta.create_or_modify_pyi = create_or_modify_pyi
    gradio.component_meta.updateable = lambda x: x


install_metaclass_patches()

from modules_forge.forge_canvas.canvas import ForgeCanvas, LogicalImage, base64_to_image, image_to_base64  # noqa: E402,F401


def install_patches() -> None:
    """Forge's component hooks and constructor repair, once."""
    global _installed
    if _installed:
        return
    _installed = True

    original_component_init = gr.components.Component.__init__
    original_context_init = gradio.blocks.BlockContext.__init__

    def component_init(self, *args, **kwargs):
        self.webui_tooltip = kwargs.pop("tooltip", None)
        result = original_component_init(self, *args, **kwargs)
        self.elem_classes = [f"gradio-{self.get_block_name()}", *(getattr(self, "elem_classes", None) or [])]
        script_callbacks.after_component_callback(self, **kwargs)
        return result

    def context_init(self, *args, **kwargs):
        result = original_context_init(self, *args, **kwargs)
        self.elem_classes = [f"gradio-{self.get_block_name()}", *(getattr(self, "elem_classes", None) or [])]
        script_callbacks.after_component_callback(self, **kwargs)
        return result

    gr.components.Component.__init__ = component_init
    gradio.blocks.BlockContext.__init__ = context_init

    class EventWrapper:
        def __init__(self, replaced_event):
            self.replaced_event = replaced_event
            self.has_trigger = getattr(replaced_event, "has_trigger", None)
            self.event_name = getattr(replaced_event, "event_name", None)
            self.callback = getattr(replaced_event, "callback", None)
            self.real_self = getattr(replaced_event, "__self__", None)

        def __call__(self, *args, **kwargs):
            if "_js" in kwargs:
                kwargs["js"] = kwargs.pop("_js")
            return self.replaced_event(*args, **kwargs)

        @property
        def __self__(self):
            return self.real_self

    def repair(grclass):
        if not getattr(grclass, "EVENTS", None):
            return

        @wraps(grclass.__init__)
        def __repaired_init__(self, *args, tooltip=None, source=None, original=grclass.__init__, **kwargs):
            if source:
                kwargs["sources"] = [source]
            allowed = inspect.signature(original).parameters
            fixed = {}
            for key, value in kwargs.items():
                if key in allowed:
                    fixed[key] = value
                else:
                    warnings.warn(f"unexpected argument for {grclass.__name__}: {key}", stacklevel=2)
            original(self, *args, **fixed)
            self.webui_tooltip = tooltip
            for event in self.EVENTS:
                setattr(self, str(event), EventWrapper(getattr(self, str(event))))

        grclass.__init__ = __repaired_init__

    for name in set(gr.components.__all__ + gr.layouts.__all__):
        repair(getattr(gr, name, None))


class ToolButton(gr.Button):
    """Forge's small emoji button. @wraps matters: Gradio builds a component's
    config from its __init__ signature, so without it elem_id would vanish."""

    @wraps(gr.Button.__init__)
    def __init__(self, value="", *args, elem_classes=None, tooltip=None, **kwargs):
        super().__init__(*args, elem_classes=["tool", *(elem_classes or [])], value=value, **kwargs)
        self.webui_tooltip = tooltip

    def get_block_name(self):
        return "button"


_accordions = 0


def stitch_panel(tabname: str, refs: dict) -> None:
    """The built-in ImageStitch script as Forge builds it: an InputAccordion
    (a hidden checkbox labelled with the script's title, then the accordion
    that follows it) holding the reference gallery, whose id the script
    derives from its title and tab."""
    global _accordions
    accordion_id = f"input-accordion-{_accordions}"
    _accordions += 1
    enable = gr.Checkbox(value=False, label="ImageStitch Integrated", elem_id=f"{accordion_id}-checkbox", visible=False)
    # Forge's inputAccordion.js opens the accordion when the box changes; here
    # a class stands in for it, so a browser test can see the change fired.
    enable.change(fn=None, js=f"(checked) => {{ const a = document.getElementById('{accordion_id}'); if (a) a.classList.toggle('probe-open', !!checked); }}", inputs=[enable])
    with gr.Accordion(label="ImageStitch Integrated", elem_id=accordion_id, elem_classes=["input-accordion"], open=False):
        refs[f"{tabname}_stitch_gallery"] = gr.Gallery(value=None, type="pil", interactive=True, show_label=False, container=False,
                                                       show_download_button=False, show_share_button=False, label="Reference Image(s)",
                                                       height=200, columns=3, rows=1, allow_preview=False, object_fit="contain",
                                                       elem_id=f"script_{tabname}_imagestitch_integrated_ref_latent")
    refs[f"{tabname}_stitch_enable"] = enable


def output_panel(tabname: str, refs: dict) -> None:
    with gr.Column(elem_id=f"{tabname}_results"):
        refs[f"{tabname}_gallery"] = gr.Gallery(label="Output", show_label=False, elem_id=f"{tabname}_gallery", columns=4, preview=True, type="pil", interactive=False, object_fit="contain")
        with gr.Row(elem_id=f"image_buttons_{tabname}", elem_classes="image-buttons"):
            ToolButton("📂", elem_id=f"{tabname}_open_folder")
            ToolButton("🖼️", elem_id=f"{tabname}_send_to_img2img")
            ToolButton("🎨️", elem_id=f"{tabname}_send_to_inpaint")
            ToolButton("📐", elem_id=f"{tabname}_send_to_extras")


def build_host(extension_tabs_fn, extra_head: str = "", hidden_tabs=()):
    """Build the whole page the way ``modules/ui.py`` does. Returns (demo, refs)."""
    install_patches()
    infotext_utils.paste_fields.clear()
    refs: dict = {}

    with gr.Blocks(analytics_enabled=False) as txt2img:
        refs["txt2img_prompt"] = gr.Textbox(label="Prompt", elem_id="txt2img_prompt")
        refs["txt2img_generate"] = gr.Button("Generate", elem_id="txt2img_generate")
        stitch_panel("txt2img", refs)
        output_panel("txt2img", refs)
        infotext_utils.paste_fields["txt2img"] = {"init_img": None, "fields": []}

    with gr.Blocks(analytics_enabled=False) as img2img:
        with gr.Tabs(elem_id="mode_img2img"):
            refs["img2img_selected_tab"] = gr.Number(value=0, visible=False)
            with gr.TabItem("img2img", id="img2img", elem_id="img2img_img2img_tab") as tab_a:
                refs["init_img"] = ForgeCanvas(elem_id="img2img_image")
            with gr.TabItem("Sketch", id="img2img_sketch", elem_id="img2img_img2img_sketch_tab") as tab_b:
                refs["sketch"] = ForgeCanvas(elem_id="img2img_sketch")
            with gr.TabItem("Inpaint", id="inpaint", elem_id="img2img_inpaint_tab") as tab_c:
                refs["init_img_with_mask"] = ForgeCanvas(elem_id="img2maskimg", contrast_scribbles=True, scribble_color="#808080", scribble_color_fixed=True, scribble_alpha=75, scribble_alpha_fixed=True, scribble_softness_fixed=True)
            for i, tab in enumerate((tab_a, tab_b, tab_c)):
                tab.select(fn=lambda tabnum=i: tabnum, outputs=[refs["img2img_selected_tab"]])
        stitch_panel("img2img", refs)
        output_panel("img2img", refs)
        infotext_utils.paste_fields["img2img"] = {"init_img": refs["init_img"].background, "fields": []}
        infotext_utils.paste_fields["inpaint"] = {"init_img": refs["init_img_with_mask"].background, "fields": []}

    with gr.Blocks(analytics_enabled=False) as extras:
        refs["extras_image"] = gr.Image(label="Source", type="pil", elem_id="extras_image", image_mode="RGBA", sources="upload")
        output_panel("extras", refs)
        infotext_utils.paste_fields["extras"] = {"init_img": refs["extras_image"], "fields": []}

    with gr.Blocks(analytics_enabled=False) as pnginfo:
        gr.Textbox(label="PNG Info", elem_id="pnginfo_box")

    interfaces = [
        (txt2img, "txt2img", "txt2img"),
        (img2img, "img2img", "img2img"),
        (extras, "Extras", "extras"),
        (pnginfo, "PNG Info", "pnginfo"),
    ]
    interfaces += extension_tabs_fn() or []

    with gr.Blocks(analytics_enabled=False) as settings:
        gr.Textbox(label="Settings", elem_id="settings_box")
    interfaces += [(settings, "Settings", "settings")]
    with gr.Blocks(analytics_enabled=False) as extensions:
        gr.Textbox(label="Extensions", elem_id="extensions_box")
    interfaces += [(extensions, "Extensions", "extensions")]

    with gr.Blocks(analytics_enabled=False, title="forge-like", head=extra_head) as demo:
        with gr.Tabs(elem_id="tabs") as tabs:
            for interface, label, ifid in interfaces:
                if label in hidden_tabs:
                    continue
                with gr.TabItem(label, id=ifid, elem_id=f"tab_{ifid}"):
                    interface.render()
        refs["tabs"] = tabs
    return demo, refs
