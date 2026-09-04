callbacks = {"ui_settings": [], "ui_tabs": [], "app_started": []}


def on_ui_settings(fn):
    callbacks["ui_settings"].append(fn)


def on_ui_tabs(fn):
    callbacks["ui_tabs"].append(fn)


def on_app_started(fn):
    callbacks["app_started"].append(fn)
