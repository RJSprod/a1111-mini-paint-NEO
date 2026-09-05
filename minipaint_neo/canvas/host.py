"""What the touch Canvas needs from the WebUI, found without touching its tabs.

Two things cross the tab boundary, both as ordinary Gradio events:

* a small "send to Canvas" button in each output panel, created next to the
  host's own "send to extras" button while the host is building that row;
* the img2img / Inpaint / Extras inputs, looked up the same way the host's
  own "Send to img2img" buttons find them, so a handoff is a Gradio output
  followed by the host's own tab-switch helper.

Nothing here runs JavaScript at startup, watches the document, or keeps a
second copy of which tab is selected.
"""

from __future__ import annotations

import typing

import gradio as gr

RECEIVE_TABS = ("txt2img", "img2img", "extras")
GALLERY_IDS = {f"{tab}_gallery": tab for tab in RECEIVE_TABS}
ANCHOR_IDS = {f"{tab}_send_to_extras": tab for tab in RECEIVE_TABS}
EXTRAS_IMAGE_ID = "extras_image"
NEEDS_EDITOR_CLASS = "minipaint-needs-editor"
TAB_LABEL = "Mini Paint"

# Forge's helpers, in javascript/ui.js. They click the host's own tab buttons.
SWITCH_JS = {
    "img2img": "switch_to_img2img",
    "inpaint": "switch_to_inpaint",
    "extras": "switch_to_extras",
}

_captured: typing.Dict[str, typing.Any] = {}
_foregrounds: typing.Dict[str, typing.Any] = {}


def reset_capture() -> None:
    """Forget the previous UI's components: a Reload UI builds new ones."""
    _captured.clear()
    _foregrounds.clear()


def receive_button_id(tab: str) -> str:
    return f"{tab}_send_to_minipaint"


def _tab_is_hidden() -> bool:
    try:
        from modules.shared import opts

        return TAB_LABEL in (opts.hidden_tabs or [])
    except Exception:
        return False


def _tool_button(label: str, **kwargs):
    """The host's own small emoji button, so ours matches the row it joins."""
    try:
        from modules.ui_components import ToolButton
    except Exception:
        ToolButton = None

    if ToolButton is not None:
        try:
            return ToolButton(label, **kwargs)
        except TypeError:
            kwargs.pop("tooltip", None)
            return ToolButton(label, **kwargs)
    classes = ["tool", *(kwargs.pop("elem_classes", None) or [])]
    kwargs.pop("tooltip", None)
    return gr.Button(label, elem_classes=classes, **kwargs)


def _building_ui() -> bool:
    """True while the host is constructing its UI.

    The same hook also fires for the throwaway instances Gradio makes when an
    event returns an update, long after the UI exists; those are not ours to
    react to.
    """
    try:
        from gradio.context import Context

        return Context.root_block is not None
    except Exception:
        return True


def on_after_component(component, **kwargs) -> None:
    """Runs for every component the host builds. Cheap on purpose."""
    elem_id = getattr(component, "elem_id", None)
    if not elem_id or not _building_ui():
        return

    classes = getattr(component, "elem_classes", None) or []
    if "logical_image_foreground" in classes:
        _foregrounds[elem_id] = component
        return

    if elem_id in GALLERY_IDS or elem_id == EXTRAS_IMAGE_ID:
        _captured[elem_id] = component
        return

    tab = ANCHOR_IDS.get(elem_id)
    if tab is None or _tab_is_hidden():
        return

    # We are inside the host's output button row right now, so a button made
    # here lands next to the one that triggered this.
    _captured[receive_button_id(tab)] = _tool_button(
        "🖌️",
        elem_id=receive_button_id(tab),
        elem_classes=[NEEDS_EDITOR_CLASS],
        tooltip="Send image to the Canvas tab (Mini Paint).",
    )


def receive_buttons() -> typing.List[typing.Tuple[str, typing.Any, typing.Any]]:
    """(tab, button, gallery) for every output panel that got a button."""
    found = []
    for tab in RECEIVE_TABS:
        button = _captured.get(receive_button_id(tab))
        gallery = _captured.get(f"{tab}_gallery")
        if button is not None and gallery is not None:
            found.append((tab, button, gallery))
    return found


def _paste_fields() -> dict:
    try:
        from modules import infotext_utils

        return infotext_utils.paste_fields
    except Exception:
        pass
    try:  # older forks
        from modules import generation_parameters_copypaste

        return generation_parameters_copypaste.paste_fields
    except Exception:
        return {}


def _init_img(tabname: str):
    entry = _paste_fields().get(tabname) or {}
    return entry.get("init_img")


def _inpaint_foreground(background) -> typing.Any:
    """The Inpaint canvas's scribble layer, which Forge reads the mask from.

    ForgeCanvas gives its two hidden textboxes the same elem_id and tells
    them apart by class, so the foreground is the sibling that shares the
    background's id.
    """
    if background is None:
        return None
    elem_id = getattr(background, "elem_id", None)
    parent = getattr(background, "parent", None)
    for child in getattr(parent, "children", None) or []:
        if child is background:
            continue
        classes = getattr(child, "elem_classes", None) or []
        if "logical_image_foreground" in classes and getattr(child, "elem_id", None) == elem_id:
            return child
    return _foregrounds.get(elem_id)


def destinations() -> typing.Dict[str, typing.Any]:
    """Whatever this host has: img2img, inpaint (+ its mask layer), extras."""
    found: typing.Dict[str, typing.Any] = {}

    img2img = _init_img("img2img")
    if img2img is not None:
        found["img2img"] = img2img

    inpaint = _init_img("inpaint")
    if inpaint is not None:
        foreground = _inpaint_foreground(inpaint)
        if foreground is not None:
            found["inpaint"] = inpaint
            found["inpaint_mask"] = foreground

    extras = _init_img("extras") or _captured.get(EXTRAS_IMAGE_ID)
    if extras is not None:
        found["extras"] = extras

    return found


def gallery_image(payload: typing.Any):
    """The image the browser picked out of a gallery, as PIL.

    Goes through the host's own decoder first so the rules for temp-file
    access are the host's, not ours.
    """
    from PIL import Image

    if isinstance(payload, Image.Image):
        return payload

    decoder = None
    try:
        from modules import infotext_utils

        decoder = infotext_utils.image_from_url_text
    except Exception:
        try:
            from modules import generation_parameters_copypaste

            decoder = generation_parameters_copypaste.image_from_url_text
        except Exception:
            decoder = None

    if decoder is not None:
        try:
            image = decoder(payload)
            if isinstance(image, Image.Image):
                return image
        except Exception as error:
            print(f"MiniPaint: the host could not decode the gallery image ({error})")

    # Gradio 4 galleries hand back (image, caption) tuples, possibly in a list.
    if isinstance(payload, list) and payload:
        payload = payload[0]
    if isinstance(payload, tuple) and payload and isinstance(payload[0], Image.Image):
        return payload[0]
    if isinstance(payload, dict):
        candidate = payload.get("image")
        if isinstance(candidate, Image.Image):
            return candidate
    return None
