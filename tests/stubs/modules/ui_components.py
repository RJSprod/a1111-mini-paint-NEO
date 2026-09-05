from functools import wraps

import gradio as gr


class ToolButton(gr.Button):
    """Small button with single emoji as text, fits inside gradio forms"""

    @wraps(gr.Button.__init__)
    def __init__(self, value="", *args, elem_classes=None, tooltip=None, **kwargs):
        elem_classes = elem_classes or []
        super().__init__(*args, elem_classes=["tool", *elem_classes], value=value, **kwargs)
        self.webui_tooltip = tooltip

    def get_block_name(self):
        return "button"


class FormColorPicker(gr.ColorPicker):
    def get_block_name(self):
        return "colorpicker"
