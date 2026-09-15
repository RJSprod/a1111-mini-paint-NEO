"""This extension's browser bundles, served on demand rather than always.

WHAT THIS IS FOR.

Forge loads every file in an extension's ``javascript/`` folder into every
page, on every load, whether or not anything on that page will ever use one.
For this extension that is the Canvas adapter, the WanGP bridge, the public
queue API and the Clipboard tab - four bundles, and a session that only ever
opens txt2img uses none of them. They are parsed on the main thread during
hydration, which is the busiest moment a WebUI page has.

So the tab-specific halves live in ``browser/`` instead, which Forge does not
auto-load, and each tab asks for its own on its own load event. What stays in
``javascript/`` is ``main.js``, which is the bootstrap and the loader and is
small enough to be worth having everywhere.

THE RULES, AND WHY EACH ONE IS HERE.

*   **A fixed name, not a path.** The only thing a caller may name is a
    bundle out of ``BUNDLES`` below. There is no path parameter, so there is
    nothing to traverse; a name that is not in the table is a 404 before
    anything touches a filesystem.

*   **Immutable, with the content in the URL.** Each bundle's URL carries a
    digest of its own bytes, so the browser may cache it forever and a change
    to the file changes the URL. That is what makes "cache it" honest - the
    alternative is either revalidating on every load or showing somebody
    yesterday's JavaScript after an update.

*   **A failure costs exactly itself.** A bundle that will not load leaves
    the tab it belongs to degraded and every other tab exactly as it was,
    which is the same rule every other integration in this extension follows.

*   **No build step.** The files are served as they are written. Minification
    would want a build dependency this change is not allowed to introduce,
    and the win here is not loading four bundles on a page that wants none -
    which is worth more than the bytes of any of them.
"""

from __future__ import annotations

import hashlib
import pathlib
import threading
import typing

from . import scrub

ROUTE_PREFIX = "/minipaint-assets"
SCRIPT_ROUTE = ROUTE_PREFIX + "/js/{name}"

#: The bundles, by the name a caller may ask for, and the file each one is.
#: A caller names one of these keys and nothing else; the value never comes
#: from a request.
BUNDLES: typing.Mapping[str, str] = {
    "canvas": "minipaint_canvas.js",
    "wangp": "minipaint_wangp.js",
    "interop": "minipaint_interop.js",
    "clipboard": "minipaint_clipboard.js",
}

#: Where they live: a folder of this repository that Forge does not load on
#: its own. Renaming it is a breaking change to nothing but this file.
DIRECTORY_NAME = "browser"

#: Files shared with the legacy editor, which live in its own source tree
#: rather than in ``browser/``.
#:
#: ``host`` is the transfer library the editor has always delivered pictures
#: with: it classifies a destination by what is actually in it, clears a
#: Gradio image before uploading into it, writes a ForgeCanvas through the
#: native value setter, and then reads back what the WebUI will submit and
#: compares it with what was sent, retrying when they differ. The new UI
#: sends to the same destinations in the same page, so it uses the same
#: implementation rather than a second one that can drift from it.
#:
#: Same rules as ``BUNDLES``: a fixed table, no path from a request, and the
#: file has to resolve inside this extension or it is not served.
SHARED: typing.Mapping[str, str] = {
    "host": "miniPaint/src/js/libs/webui-host.js",
}

#: Forever, because the digest is in the URL. A changed file is a changed
#: URL, so there is nothing for a browser to revalidate.
CACHE_CONTROL = "private, max-age=31536000, immutable"

_LOG_PREFIX = "MiniPaint:"
_INSTALLED_FLAG = "_minipaint_assets_installed"

_lock = threading.RLock()
_digests: typing.Dict[str, str] = {}


def extension_root() -> pathlib.Path:
    """This extension's own folder. Nothing is served from outside it."""
    return pathlib.Path(__file__).resolve().parent.parent


def root() -> pathlib.Path:
    """The folder the bundles are in. Inside this extension, always."""
    return extension_root() / DIRECTORY_NAME


def names() -> typing.Tuple[str, ...]:
    """Every name a caller may ask for: the bundles and the shared files."""
    return tuple(BUNDLES) + tuple(SHARED)


def path_for(name: typing.Any) -> pathlib.Path:
    """The file a bundle name means, or a refusal.

    The name is looked up in the table; it is never joined onto anything.
    That is the whole of the path safety here, and it is why there is a
    table rather than a naming convention.
    """
    key = str(name or "")
    filename = BUNDLES.get(key)
    if filename is not None:
        return root() / filename
    shared = SHARED.get(key)
    if shared is None:
        raise KeyError(key[:40])
    # A table entry, not a request - but it is the only value here with a
    # path in it, so it is checked rather than trusted.
    base = extension_root()
    found = (base / shared).resolve()
    if base != found and base not in found.parents:
        raise KeyError(key[:40])
    return found


def digest(name: str) -> str:
    """A short digest of a bundle's bytes, cached for the process's life.

    Read once: the file does not change while Forge is running, and a
    reinstall is a restart. A file that cannot be read digests as empty,
    which makes its URL cacheable-but-plain rather than failing the page.
    """
    with _lock:
        found = _digests.get(name)
        if found is not None:
            return found
    try:
        data = path_for(name).read_bytes()
        found = hashlib.sha256(data).hexdigest()[:16]
    except Exception:
        found = ""
    with _lock:
        _digests[name] = found
    return found


def url_for(name: str) -> str:
    """The URL a page loads a bundle from, with its content in it."""
    if name not in names():
        raise KeyError(str(name)[:40])
    stamp = digest(name)
    return f"{ROUTE_PREFIX}/js/{name}.js" + (f"?v={stamp}" if stamp else "")


def manifest() -> typing.Dict[str, str]:
    """Every bundle and its URL, for a page that wants to know up front."""
    return {name: url_for(name) for name in names()}


def tab_loader_js(panel_id: str, names: typing.Sequence[str]) -> str:
    """A Gradio ``js=`` that loads these bundles when a tab is first opened.

    The laziness C1 is actually after: a session that never opens a tab
    never parses its browser half. It degrades in the safe direction - a
    panel that cannot be found, or one that is already on screen, loads
    immediately - because being lazy is the optimisation and being there is
    the requirement.
    """
    wanted = [name for name in names if name in BUNDLES]
    urls = repr([url_for(name) for name in wanted]).replace("'", '"')
    return (
        "() => { const w = window.minipaintAssets; "
        f"return w && w.loadOnTab ? w.loadOnTab({panel_id!r}, {urls}) : Promise.resolve(false); }}".replace("'", '"')
    )


def module_js(name: str) -> str:
    """A Gradio ``js=`` that imports a shared module and keeps it.

    A bundle is a classic script the loader appends; this one is an ES
    module with exports, so it is imported rather than appended. The result
    is kept by the page, so the second tab to ask for it gets the first
    tab's copy.
    """
    url = url_for(name)
    return (
        "() => { const w = window.minipaintAssets; "
        f'return (w && w.module) ? w.module({url!r}) : Promise.resolve(null); }}'.replace("'", '"')
    )


def loader_js(names: typing.Sequence[str]) -> str:
    """A Gradio ``js=`` that asks the page to load these bundles, once.

    Idempotent by URL, which is what makes Reload UI safe: the WebUI rebuilds
    its page without reloading the document, so a loader that appended a
    script per rebuild would install a second copy of every listener the
    bundle registers. ``main.js`` keeps the record; this is the call.

    RETURNS A PROMISE, IN EVERY BRANCH.

    This wrapper used to call ``w.load(...)`` and return nothing. Its caller
    awaited it, and ``await undefined`` resumes on the microtask queue while
    a <script> runs in a task - so the await always finished first, the
    check for the adapter that followed it always failed, and the Canvas was
    never attached on any machine at any speed. A wrapper used inside an
    async flow returns its promise or it is not part of that flow at all.
    """
    wanted = [name for name in names if name in BUNDLES]
    urls = repr([url_for(name) for name in wanted]).replace("'", '"')
    return (
        "() => { const w = window.minipaintAssets; "
        f"return (w && w.load) ? w.load({urls}) : Promise.resolve(false); }}"
    )


def _signed_in(request: typing.Any) -> bool:
    try:
        from .wangp import proxy

        return bool(proxy.signed_in(request))
    except Exception:
        return True


async def _script(request: typing.Any) -> typing.Any:
    from starlette.responses import JSONResponse, Response

    name = str(request.path_params.get("name", ""))
    if name.endswith(".js"):
        name = name[:-3]
    if name not in names():
        return JSONResponse({"ok": False, "code": "REQUEST_INVALID"}, status_code=404, headers={"Cache-Control": "no-store"})
    if not _signed_in(request):
        return JSONResponse({"ok": False, "code": "AUTH_BOUNDARY_FAILED"}, status_code=401, headers={"Cache-Control": "no-store"})
    try:
        body = path_for(name).read_text(encoding="utf-8")
    except Exception:
        return JSONResponse({"ok": False, "code": "INTERNAL_ERROR"}, status_code=404, headers={"Cache-Control": "no-store"})
    return Response(
        body,
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": CACHE_CONTROL, "X-Content-Type-Options": "nosniff"},
    )


def install(app: typing.Any) -> None:
    """Put the route on Forge's FastAPI app. Called from ``on_app_started``."""
    if getattr(app, _INSTALLED_FLAG, False):
        return
    try:
        from starlette.routing import Route

        app.router.routes.insert(0, Route(SCRIPT_ROUTE, endpoint=_script, methods=["GET", "HEAD"]))
        setattr(app, _INSTALLED_FLAG, True)
    except Exception as error:
        scrub.console(f"the bundle route could not be registered ({error}); the tabs load nothing extra.", _LOG_PREFIX)


def reset_for_tests() -> None:
    with _lock:
        _digests.clear()


__all__ = [
    "BUNDLES", "CACHE_CONTROL", "DIRECTORY_NAME", "ROUTE_PREFIX", "SCRIPT_ROUTE", "SHARED",
    "digest", "extension_root", "install", "loader_js", "manifest", "module_js", "names",
    "path_for", "reset_for_tests", "root", "tab_loader_js", "url_for",
]
