"""The frame timer, into the page head - before Gradio's modules load.

Gradio's core captures ``requestAnimationFrame`` once, when its module is
evaluated, and schedules its component-update flush through that captured
reference. Every event trigger waits for that flush. A wrapper installed by
the page script - which Gradio runs after the app has mounted - is therefore
too late for the one frame that matters: a document the browser is not
rendering (this page, whenever the Forge tab holding its iframe is not the
one on screen) leaves the flush waiting for a frame that never comes, and
every bridge request behind it, until the WanGP tab is opened again.

The only place a script can precede the module is the HTML that loads it.
Gradio renders that HTML from a Jinja template, and this module wraps the
template loader's ``get_source`` so that the two page templates come back
with the script inserted just before the module tag. WanGP's own focus patch
goes into the page the same way, and the two wrappers chain in either order.
Nothing about the request path, the response, or any other template changes.
"""

from __future__ import annotations

import typing

#: What a page that already carries the script contains - the handle the
#: script leaves on the window. Seen in the source, nothing is inserted twice.
SENTINEL = "window.__minipaintFrames"

#: The templates Gradio renders for the page itself, and nothing else.
TEMPLATES = ("frontend/index.html", "frontend/share.html")

#: Where the script goes: right before Gradio's own module, so that it has
#: run by the time the module is evaluated.
MODULE_TAG = '<script type="module"'

#: The mark left on the loader, so that a second install is a no-op.
FLAG = "_minipaint_bridge_head_installed"


def inject(source: str, script: str) -> str:
    """The template source with the script placed before the module tag.

    Idempotent: a source that already carries the sentinel is returned as it
    is. A template without a module tag gets the script before ``</head>``,
    and one without either gets it at the end - the page then still loads,
    and the page script installs the timer late and says so.
    """
    text = str(source or "")
    if SENTINEL in text:
        return text
    tag = "\n<script>\n" + str(script or "") + "\n</script>\n"
    at = text.find(MODULE_TAG)
    if at == -1:
        at = text.find("</head>")
    if at == -1:
        return text + tag
    return text[:at] + tag + text[at:]


def install(script: str, templates: typing.Any = None) -> typing.Tuple[bool, str]:
    """Wrap Gradio's template loader so the page templates carry the script.

    ``templates`` is Gradio's ``Jinja2Templates`` object, or anything with an
    ``env`` whose ``loader`` has ``get_source``; None means Gradio's own,
    imported here so that a WanGP without it is a reason and not a crash.
    Returns (installed, why): True with ``installed`` or ``already installed``,
    False with what was missing.
    """
    if templates is None:
        try:
            import gradio.routes as routes  # type: ignore[import-not-found]
        except Exception as error:
            return False, f"gradio.routes could not be imported: {type(error).__name__}"
        templates = getattr(routes, "templates", None)
    env = getattr(templates, "env", None)
    loader = getattr(env, "loader", None)
    original = getattr(loader, "get_source", None)
    if loader is None or not callable(original):
        return False, "Gradio's template loader has no get_source to wrap"
    if getattr(loader, FLAG, False):
        return True, "already installed"

    def get_source(environment: typing.Any, template: typing.Any) -> typing.Any:
        source, filename, uptodate = original(environment, template)
        if template in TEMPLATES:
            source = inject(source, script)
        return source, filename, uptodate

    try:
        loader.get_source = get_source
        setattr(loader, FLAG, True)
    except Exception as error:
        return False, f"the template loader would not take the wrapper: {type(error).__name__}"
    cache = getattr(env, "cache", None)
    if cache is not None and hasattr(cache, "clear"):
        try:
            cache.clear()
        except Exception:
            pass
    return True, "installed"


__all__ = ["FLAG", "MODULE_TAG", "SENTINEL", "TEMPLATES", "inject", "install"]
