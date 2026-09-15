"""What the touch Canvas needs from the WebUI, found without touching its tabs.

Two things cross the tab boundary, both as ordinary Gradio events:

* a small "send to Canvas" button in each output panel, created next to the
  host's own "send to extras" button while the host is building that row;
* the img2img / Inpaint / Extras inputs, looked up the same way the host's
  own "Send to img2img" buttons find them, so a handoff is a Gradio output
  followed by the host's own tab-switch helper;
* the reference gallery of the built-in ImageStitch script in txt2img and
  img2img, and the box that enables it, remembered as the host builds them.

Nothing here runs JavaScript at startup, watches the document, or keeps a
second copy of which tab is selected.
"""

from __future__ import annotations

import os
import tempfile
import typing

import gradio as gr

from .. import scrub

RECEIVE_TABS = ("txt2img", "img2img", "extras")
GALLERY_IDS = {f"{tab}_gallery": tab for tab in RECEIVE_TABS}
ANCHOR_IDS = {f"{tab}_send_to_extras": tab for tab in RECEIVE_TABS}
EXTRAS_IMAGE_ID = "extras_image"
TAB_LABEL = "Mini Paint"

# The built-in ImageStitch script (extensions-builtin/sd_forge_image_stitch):
# one instance per tab, each an InputAccordion - a hidden checkbox with the
# script's title as its label, then the accordion - holding the reference
# gallery, whose id the script derives from its title and tab.
STITCH_LABEL = "ImageStitch Integrated"
STITCH_GALLERY_IDS = {f"script_{tab}_imagestitch_integrated_ref_latent": f"stitch_{tab}" for tab in ("txt2img", "img2img")}

# Forge's helpers, in javascript/ui.js. They click the host's own tab buttons.
SWITCH_JS = {
    "txt2img": "switch_to_txt2img",
    "img2img": "switch_to_img2img",
    "inpaint": "switch_to_inpaint",
    "extras": "switch_to_extras",
}

_captured: typing.Dict[str, typing.Any] = {}
#: Every destination this module has handed out, by name, so that the page
#: they were wired into can be asked about them once it exists. See ``audit``.
_handed: typing.Dict[str, typing.Any] = {}
_foregrounds: typing.Dict[str, typing.Any] = {}
_pending: typing.Dict[str, typing.Any] = {}


def reset_capture() -> None:
    """Forget the previous UI's components: a Reload UI builds new ones."""
    _captured.clear()
    _foregrounds.clear()
    _pending.clear()
    _handed.clear()


def audit(demo: typing.Any) -> typing.List[str]:
    """Name any destination the finished page turns out not to contain.

    WHY THIS CANNOT BE CHECKED WHERE IT IS WIRED.

    Every destination here belongs to the host or to another extension, and
    is remembered as the host builds it. The host builds its tabs as separate
    ``gr.Blocks`` and composes them afterwards, so while our own tab is being
    built there is no registry that holds them all and nothing to check a
    destination against: "is this component on the page" has no answer yet.

    It has one here. By the time the app has started, Gradio's root block
    holds every component the page ended up with, and a destination that is
    not in it is one kept across a rebuild - a Reload UI, or anything else
    that builds the interface twice in one process.

    Left unsaid, that surfaces as ``KeyError: <some number>`` thrown from
    inside Gradio's ``postprocess_data`` when somebody uses the Send menu:
    no component name, no extension named anywhere in the stack, and nothing
    to connect it to the tab it came from. This turns that number back into a
    name, once, at startup, before anybody clicks anything.

    Returns the names, and says them. It changes nothing: by the time this
    runs the events are wired and Gradio offers no way to unwire one.
    """
    registry = getattr(demo, "blocks", None)
    if not isinstance(registry, dict) or not registry or not _handed:
        return []
    missing = sorted(
        key for key, component in _handed.items()
        if getattr(component, "_id", None) is not None and component._id not in registry
    )
    if missing:
        scrub.console(
            "these Send destinations are not on the finished page and belong to an earlier build of it: "
            + ", ".join(missing)
            + ". Sending to one of them will fail inside Gradio; restarting the WebUI clears it.",
            "MiniPaint:",
        )
    return missing


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

    # ImageStitch: its enabling box comes first, its gallery a few
    # components later; the gallery's id says which tab both belong to.
    if elem_id in STITCH_GALLERY_IDS:
        key = STITCH_GALLERY_IDS[elem_id]
        _captured[key] = component
        enable = _pending.pop("stitch_enable", None)
        if enable is not None:
            _captured[f"{key}_enable"] = enable
        return
    if isinstance(component, gr.Checkbox) and getattr(component, "label", None) == STITCH_LABEL and str(elem_id).endswith("-checkbox"):
        _pending["stitch_enable"] = component
        return

    tab = ANCHOR_IDS.get(elem_id)
    if tab is None or _tab_is_hidden():
        return

    # We are inside the host's output button row right now, so a button made
    # here lands next to the one that triggered this.
    _captured[receive_button_id(tab)] = _tool_button(
        "🖌️",
        elem_id=receive_button_id(tab),
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


def _on_the_page(component: typing.Any) -> bool:
    """Whether this component was ever put on a page, or only made.

    A destination is remembered as the host builds its UI, and the hook that
    remembers it fires when a component is CREATED. Creating one is not the
    same as putting it on the page: Gradio has ``render=False``, a scratch
    context is a normal thing to build in, and a script can make a component
    for one tab and place only the copy it made for another. What is left is
    a perfectly good component that no page contains.

    Wiring an event to one of those produces the fault this cost five builds
    to find. The event is in the page's graph and cannot run, because one of
    the components it names is not there - so it fails silently, every time,
    for ever, while every other event on the same tab works. Nothing reports
    it: no error, no console line, no failed request. The page simply does
    not answer.

    And it is not confined to the destination that is missing. Every send
    this tab makes named every backend destination in its outputs, so one
    component nobody rendered took sending to *all* of them with it - which
    is why a user could not send to img2img, a tab plainly on their screen.

    ``is_rendered`` is Gradio's own answer, set when a component is placed.
    A build that does not have it is left alone rather than guessed at.
    """
    return getattr(component, "is_rendered", True) is not False


def destinations() -> typing.Dict[str, typing.Any]:
    """Whatever this host has: img2img, inpaint (+ its mask layer), extras,
    and ImageStitch's galleries (+ their enabling boxes) in txt2img and img2img.

    Only what is actually on the page - see ``_on_the_page``.
    """
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

    for key in ("stitch_txt2img", "stitch_img2img"):
        gallery = _captured.get(key)
        enable = _captured.get(f"{key}_enable")
        if gallery is not None and enable is not None:
            found[key] = gallery
            found[f"{key}_enable"] = enable

    # A destination that was made but never placed is not a destination.
    # Dropped in one pass at the end so a pair - a gallery and the box that
    # enables it, a canvas and its mask layer - goes together: half a
    # destination is a worse answer than none.
    absent = sorted(key for key, component in found.items() if not _on_the_page(component))
    for key in absent:
        found.pop(key, None)
        for partner in (f"{key}_enable", f"{key}_mask"):
            found.pop(partner, None)
        if key.endswith("_enable"):
            found.pop(key[: -len("_enable")], None)
        if key.endswith("_mask"):
            found.pop(key[: -len("_mask")], None)
    if absent:
        scrub.console(
            "these Send destinations were built but never put on the page, so they are not offered: "
            + ", ".join(absent)
            + ". An event wired to one cannot run, and it would have taken every other destination with it.",
            "MiniPaint:",
        )

    _handed.update(found)
    return found


def temp_image_path(prefix: str) -> str:
    """A file for a picture one of the host's galleries or Image inputs will
    serve. Forge keeps such files in its own temporary directory (Settings
    -> Saving, cleaned at startup) and serves a picture that says where it
    is saved rather than copying it - into that directory, which a fresh
    install has not created yet. So: that directory, made if need be, else
    the system's."""
    directory: typing.Optional[str] = None
    try:
        from modules.shared import opts

        directory = str(getattr(opts, "temp_dir", "") or "") or None
    except Exception:
        directory = None
    if directory:
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            directory = None
    handle = tempfile.NamedTemporaryFile(prefix=prefix, suffix=".png", dir=directory, delete=False)
    handle.close()
    return handle.name


def staged(image):
    """The picture saved where the host will serve it from, and told so."""
    path = temp_image_path("minipaint-")
    image.save(path, format="PNG")
    image.already_saved_as = path
    return image


def gallery_file(payload: typing.Any) -> typing.Optional[str]:
    """The file a gallery item stands for, when the host proves it may be read.

    A Forge output gallery item names a file the host serves from its own
    temporary or output directory, and that file carries the generation
    metadata a decoded picture loses. The path is taken only through the
    host's own policy - the same ``check_tmp_file`` its paste machinery
    applies - so a payload naming anything else answers None and the caller
    decodes the pixels instead. Never a path the browser invented.
    """
    candidate = payload
    if isinstance(candidate, list) and candidate:
        candidate = candidate[0]
    if isinstance(candidate, tuple) and candidate:
        candidate = candidate[0]
    if isinstance(candidate, dict):
        nested = candidate.get("image")
        if isinstance(nested, dict):
            candidate = nested
        candidate = candidate.get("path") or candidate.get("name")
    if not isinstance(candidate, str) or not candidate:
        return None
    try:
        from modules import shared, ui_tempdir

        if not ui_tempdir.check_tmp_file(shared.demo, candidate):
            return None
    except Exception:
        return None
    try:
        return candidate if os.path.isfile(candidate) and not os.path.islink(candidate) else None
    except OSError:
        return None


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
            scrub.console(f"the host could not decode the gallery image ({error})")

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
