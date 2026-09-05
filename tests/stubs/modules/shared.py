"""Enough of the WebUI's shared.opts to build the extension UI outside it."""


class OptionInfo:
    def __init__(self, default=None, label="", component=None, component_args=None,
                 onchange=None, section=None, refresh=None, comment_before="",
                 comment_after="", infotext=None, restrict_api=False, category_id=None):
        self.default = default
        self.label = label
        self.component = component
        self.component_args = component_args
        self.section = section
        self.category_id = category_id
        self.comment_before = comment_before
        self.comment_after = comment_after
        self.reload_ui = False

    def info(self, text):
        self.comment_after += f"<span class='info'>({text})</span>"
        return self

    def needs_reload_ui(self):
        self.reload_ui = True
        self.comment_after += " <span class='info'>(requires Reload UI)</span>"
        return self


class Options:
    def __init__(self):
        # The host options the Canvas reads, at Forge Neo's defaults.
        self.data = {
            "forge_canvas_height": 512,
            "forge_canvas_toolbar_always": False,
            "forge_canvas_consistent_brush": False,
            "forge_canvas_plain": False,
            "forge_canvas_plain_color": "#808080",
            "img2img_inpaint_mask_brush_color": "#808080",
            "img2img_inpaint_mask_high_contrast": True,
            "img2img_inpaint_mask_scribble_alpha": 75,
        }
        self.data_labels = {}
        self.hidden_tabs = []

    def add_option(self, key, info):
        self.data_labels[key] = info
        self.data.setdefault(key, info.default)

    def __getattr__(self, item):
        data = self.__dict__.get("data", {})
        if item in data:
            return data[item]
        raise AttributeError(item)


opts = Options()
