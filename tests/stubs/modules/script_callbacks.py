callbacks = {
    "ui_settings": [],
    "ui_tabs": [],
    "app_started": [],
    "before_ui": [],
    "after_component": [],
}


def on_ui_settings(fn, *, name=None):
    callbacks["ui_settings"].append(fn)


def on_ui_tabs(fn, *, name=None):
    callbacks["ui_tabs"].append(fn)


def on_app_started(fn, *, name=None):
    callbacks["app_started"].append(fn)


def on_before_ui(fn, *, name=None):
    callbacks["before_ui"].append(fn)


def on_after_component(fn, *, name=None):
    callbacks["after_component"].append(fn)


def after_component_callback(component, **kwargs):
    for fn in callbacks["after_component"]:
        fn(component, **kwargs)
