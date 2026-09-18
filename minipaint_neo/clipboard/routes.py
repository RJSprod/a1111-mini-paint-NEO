"""Clipboard's own routes: a picture by its id, bytes in, and a send.

``/minipaint-clipboard/image/<asset_id>`` serves a library file - or, with
``?thumb=1``, a small copy of it - after the store has proved the id names a
regular file inside the configured root. It is the Clipboard contract for
showing a picture in the browser; the WebUI's ``/file=<path>`` route is not,
because a route that takes a path is a route that one day takes another.

``/minipaint-clipboard/import`` takes image bytes and nothing else - a paste
from the browser's clipboard, a file dropped on a slot - and answers with
the asset id the store gave them. No path, no filename beyond the basename
the browser suggests, and that one is validated like every other.

``/minipaint-clipboard/send`` is everything about sending a picture out of
the tab that does not need Gradio: given a destination it answers with what
the browser needs to place the picture itself, and given a request it keeps
that request for the tab's own Gradio event to fall back on. Both halves
exist because the browser's other way of reaching the server - writing a
hidden box and hoping the framework noticed - is not reliable on every
install.

All three are gated by the same sign-in the WanGP proxy applies, where the
host has one.
"""

from __future__ import annotations

import mimetypes
import time
import typing

from .. import scrub
from ..wangp import errors
from ..wangp.errors import IntegrationError
from . import config, history, outputs
from . import store as store_module

ROUTE_PREFIX = "/minipaint-clipboard"
IMAGE_ROUTE = ROUTE_PREFIX + "/image/{asset_id}"
IMPORT_ROUTE = ROUTE_PREFIX + "/import"
SEND_ROUTE = ROUTE_PREFIX + "/send"
LIBRARY_ROUTE = ROUTE_PREFIX + "/library"
SETTINGS_ROUTE = ROUTE_PREFIX + "/settings"
QUEUE_ROUTE = ROUTE_PREFIX + "/queue"
ENHANCE_SETTINGS_ROUTE = ROUTE_PREFIX + "/enhance-settings"
OUTPUTS_ROUTE = ROUTE_PREFIX + "/outputs"
OUTPUT_FILE_ROUTE = ROUTE_PREFIX + "/output/{file_id}"
#: The gallery's Send to WanGP popup: everything it asks, over one POST, and
#: the small copy of the picture it froze.
INTERCEPT_ROUTE = ROUTE_PREFIX + "/intercept"
INTERCEPT_IMAGE_ROUTE = ROUTE_PREFIX + "/intercept/image/{token}"

#: How many pictures one page of the grid carries.
#:
#: THE BINDING CONSTRAINT IS CONNECTIONS, NOT BYTES. Every thumbnail is its
#: own request and a browser allows six per origin on HTTP/1.1 - already
#: shared with Gradio's event stream, this extension's event stream and the
#: WanGP iframe through the proxy. Sixty fills in six to ten round-trip
#: waves; 250 would be forty, and would feel like the grid never finishes.
#: Sixty also lands well in the layout - eight to ten rows at 144px tiles -
#: and four pages of it fit inside the in-memory thumbnail cache, so paging
#: back and forth stays warm.
#:
#: One constant, and the route takes ``size``, so this stays a decision
#: rather than becoming a migration.
PAGE_SIZE = 60
#: Below this the round trips buy nothing; above it the waves do.
PAGE_SIZE_MIN = 10
PAGE_SIZE_MAX = 250

_LOG_PREFIX = "MiniPaint Clipboard:"
_INSTALLED_FLAG = "_minipaint_clipboard_installed"


def image_url(asset_id: str, thumb: bool = True, version: typing.Any = "") -> str:
    """The URL the tab puts in an ``<img>``: the id, never the name.

    ``version`` is ``<mtime_ns>-<size_bytes>`` - what ``store.version_of``
    says about the asset - and it is what makes the browser's own cache
    work: a URL carrying the file's current identity is answered
    ``immutable``, so it is fetched once and never re-validated, and a file
    that changed is a different URL rather than a stale picture. A rename
    changes the caption and not the file, so the URL does not move and
    nothing is re-fetched.
    """
    query = []
    if thumb:
        query.append("thumb=1")
    if version:
        query.append(f"v={version}")
    return f"{ROUTE_PREFIX}/image/{asset_id}" + (("?" + "&".join(query)) if query else "")


#: What a picture is cached as when the URL carries its current identity.
#: ``immutable`` is the part that works: the browser stops re-validating on
#: reload, so a thumbnail fetched once is never fetched again while it is in
#: the cache.
IMMUTABLE_CACHE = "private, max-age=31536000, immutable"
#: And what one without that identity gets. An unversioned or falsely
#: versioned URL must never be blessed immutable: it would be a year of a
#: picture the file no longer holds.
REVALIDATED_CACHE = "private, max-age=3600"


def _signed_in(request: typing.Any) -> bool:
    try:
        from ..wangp import proxy

        return bool(proxy.signed_in(request))
    except Exception:
        return True


def _json(payload: dict, status: int = 200) -> typing.Any:
    from starlette.responses import JSONResponse

    return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})


async def _image(request: typing.Any) -> typing.Any:
    from starlette.responses import Response

    if not _signed_in(request):
        return _json({"ok": False, "code": errors.AUTH_BOUNDARY_FAILED, "message": "Sign in first."}, 401)
    asset_id = request.path_params.get("asset_id", "")
    thumb = str(request.query_params.get("thumb", "") or "") in ("1", "true", "yes")
    wanted = str(request.query_params.get("v", "") or "")
    try:
        if thumb:
            data, mime = store_module.store().thumbnail(asset_id)
        else:
            data, mime = store_module.store().read_bytes(asset_id)
    except IntegrationError as error:
        status = 404 if error.code in (errors.CLIPBOARD_ASSET_UNKNOWN, errors.CLIPBOARD_NOT_CONFIGURED) else 403
        return _json(error.as_dict(), status)
    except Exception:
        return _json({"ok": False, "code": errors.INTERNAL_ERROR, "message": errors.message(errors.INTERNAL_ERROR)}, 500)
    # Cached by the browser against the version the tab put in the URL; a
    # renamed or replaced file gets a new URL and never a stale picture.
    #
    # Only the asset's CURRENT canonical version earns the immutable header.
    # Anything else - no version at all, or a version somebody made up - is
    # answered with the careful one, because the whole strength of
    # ``immutable`` is that the browser will not ask again for a year.
    canonical = store_module.store().canonical_version(asset_id) if wanted else ""
    fresh = bool(wanted) and wanted == canonical
    return Response(content=data, media_type=mime,
                    headers={"Cache-Control": IMMUTABLE_CACHE if fresh else REVALIDATED_CACHE})


def clamp_size(value: typing.Any) -> int:
    """A page size inside its bounds. A bad one is answered, not refused."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return PAGE_SIZE
    return max(PAGE_SIZE_MIN, min(PAGE_SIZE_MAX, number))


def page_of(value: typing.Any, pages: int) -> int:
    """A page number inside the library, counting from zero.

    Zero-based because that is what the grid asks for - "page 0 of the new
    sort" - and because a pager that shows "Page 3 of 8" is displaying
    ``page + 1``, which is presentation and belongs in the browser. Out of
    range is clamped rather than refused: a grid that shows nothing because
    a query string was wrong is a worse answer than a grid.
    """
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        number = 0
    return max(0, min(max(1, int(pages)) - 1, number))


def library_page(sort: typing.Any = "", page: typing.Any = 0, size: typing.Any = PAGE_SIZE,
                 selected: typing.Any = "", refresh: bool = False) -> dict:
    """One page of the picture index, and everything the grid draws it from.

    SORTING IS APPLIED OVER THE WHOLE LIBRARY, THEN SLICED. ``store.assets``
    already returns the complete ordered list from the in-memory index, so
    this is a slice of work the store does correctly today: the ordering
    logic is not moved, rewritten or duplicated.

    Bad input is answered rather than refused throughout. An unknown sort
    falls back to the stored one, a page past the end returns the last page
    that exists, and a size outside its bounds is clamped.
    """
    library = store_module.store()
    if refresh:
        # Read the folder again first. The index is what this route slices,
        # and the index is a memory of the folder rather than the folder: a
        # file somebody deleted outside Clipboard is still in it, and a tile
        # for a file that is gone is a broken picture. Opening the tab has
        # always re-read the folder; this is that, over HTTP, so it no longer
        # needs the framework's channel to be the thing that heals a page.
        try:
            library.refresh()
        except IntegrationError:
            pass
    current = config.load()
    mode = str(sort or "")
    if mode not in config.SORT_MODES:
        mode = current.sort
    wanted = clamp_size(size)
    configured = library.configured()
    assets = library.assets(mode) if configured else []
    total = len(assets)
    pages = max(1, -(-total // wanted))
    index = page_of(page, pages)
    shown = assets[index * wanted:(index + 1) * wanted]
    # Which page holds the selection, so the pager can mark it. The selected
    # id is the browser's and does not belong to a page; this is the one
    # thing the server can say about it that the browser cannot work out for
    # itself, because only the server holds the whole order.
    chosen = str(selected or "")
    at = next((position for position, asset in enumerate(assets) if asset.asset_id == chosen), -1)
    if not configured:
        reason, status = "unconfigured", "Choose a storage folder from the menu to begin."
    elif not total:
        reason, status = "empty", "No images in the folder yet."
    else:
        reason, status = "", f"{total} image{'s' if total != 1 else ''} in the folder."
    return {
        "ok": True,
        # The library's identity, not the event spine's: see ``Store.moved``.
        "revision": library.revision(),
        "configured": configured,
        "sort": mode,
        "total": total,
        "page": index,
        "pages": pages,
        "selected_page": (at // wanted) if at >= 0 else -1,
        "size": wanted,
        "reason": reason,
        "status": status,
        "items": [
            {
                "id": asset.asset_id,
                "name": asset.filename,
                "w": int(asset.width),
                "h": int(asset.height),
                "bytes": int(asset.size_bytes),
                # The version of this picture's bytes, which is what makes
                # the browser's own thumbnail cache work. See ``image_url``.
                "v": store_module.version_of(asset),
            }
            for asset in shown
        ],
    }


async def _library(request: typing.Any) -> typing.Any:
    """The picture index, as its own route.

    NOT AN EXTENSION OF ``/send``: that one already has two jobs - prepare a
    send, remember a request - and this is a third with a different cache
    policy and a different meaning when it fails. A send that fails is a
    picture that did not move; an index that fails is a page that keeps the
    tiles it has and says the library could not be re-read.
    """
    if not _signed_in(request):
        return _json({"ok": False, "code": errors.AUTH_BOUNDARY_FAILED, "message": "Sign in first."}, 401)
    params = request.query_params
    try:
        answer = library_page(params.get("sort"), params.get("page"), params.get("size"), params.get("selected"),
                              str(params.get("refresh") or "") in ("1", "true", "yes"))
    except Exception as error:
        scrub.console(f"the library index could not be read ({type(error).__name__}).", _LOG_PREFIX)
        return _json({"ok": False, "code": errors.INTERNAL_ERROR, "message": errors.message(errors.INTERNAL_ERROR)}, 500)
    return _json(answer)


#: What the status line says when the gallery's button is pointed somewhere.
INTERCEPT_SENTENCES = {
    config.INTERCEPT_MINIPAINT: "The gallery’s 🖌️ button sends to Mini Paint again.",
    config.INTERCEPT_CLIPBOARD: "The gallery’s 🖌️ button now sends into Clipboard.",
    config.INTERCEPT_WANGP: "The gallery’s 🖌️ button now opens a WanGP request.",
}


def menu_facts(current: typing.Optional[config.Config] = None) -> dict:
    """What the browser's menu draws itself from, in one place.

    Answered by the settings route and carried by the tab's own menu-state
    box, so a menu that changed a setting over HTTP and one that was built
    from a render agree on what they show.
    """
    current = current if current is not None else config.load()
    return {
        "intercept": bool(current.intercept),
        "intercept_target": current.intercept_target,
        "intercepts": [[target, config.INTERCEPT_LABELS[target]] for target in config.INTERCEPT_TARGETS],
        "configured": bool(current.configured),
        "sort": current.sort,
        "thumbnail": current.thumbnail,
        "sorts": [[mode, config.SORT_LABELS[mode]] for mode in config.SORT_MODES],
    }


def apply_settings(changes: typing.Mapping[str, typing.Any]) -> dict:
    """The three things the menu remembers: the sort, the tile size, the intercept.

    Every one of them is request-and-response - press a thing, get an answer
    - so none of them needs anything held open, and each answers with its own
    sentence for the status line rather than leaving the page to compose one
    from a render that may never come.

    A value that is not one falls back rather than refusing: a menu that
    cannot change the sort because a string was wrong is a worse answer than
    a sort that did not move.

    The intercept is a destination now - ``intercept_target`` - and the
    older switch is still taken, meaning Clipboard or Mini Paint, so a page
    from before the third destination existed keeps working.
    """
    changes = changes if isinstance(changes, dict) else {}
    said = []
    wanted: typing.Dict[str, typing.Any] = {}
    if "sort" in changes:
        mode = str(changes.get("sort") or "")
        if mode in config.SORT_MODES:
            wanted["sort"] = mode
            said.append(f"Sorted by {config.SORT_LABELS[mode].lower()}.")
    if "thumbnail" in changes:
        wanted["thumbnail"] = config.clamp_thumbnail(changes.get("thumbnail"))
    if "intercept_target" in changes:
        target = str(changes.get("intercept_target") or "")
        if target in config.INTERCEPT_TARGETS:
            wanted["intercept_target"] = target
            said.append(INTERCEPT_SENTENCES[target])
    elif "intercept" in changes:
        target = config.INTERCEPT_CLIPBOARD if changes.get("intercept") else config.INTERCEPT_MINIPAINT
        wanted["intercept_target"] = target
        said.append(INTERCEPT_SENTENCES[target])
    current = config.update(**wanted) if wanted else config.load()
    return {
        "ok": True,
        "status": " ".join(said),
        "sort": current.sort,
        "thumbnail": current.thumbnail,
        "intercept": current.intercept,
        "intercept_target": current.intercept_target,
        # What the browser's menu draws itself from. Answered here so a menu
        # that changed a setting is correct without a Gradio render.
        "menu": menu_facts(current),
    }


async def _settings(request: typing.Any) -> typing.Any:
    if not _signed_in(request):
        return _json({"ok": False, "code": errors.AUTH_BOUNDARY_FAILED, "message": "Sign in first."}, 401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        return _json(apply_settings(body))
    except Exception as error:
        scrub.console(f"a Clipboard setting could not be saved ({type(error).__name__}).", _LOG_PREFIX)
        return _json({"ok": False, "code": errors.INTERNAL_ERROR, "message": errors.message(errors.INTERNAL_ERROR)}, 500)


def _tab():
    """The Clipboard tab of the UI being built, or None when there is none."""
    from . import ui as ui_module

    return ui_module.current()


def _no_tab() -> dict:
    return {"ok": False, "code": errors.INTERNAL_ERROR,
            "message": "The Clipboard tab has not been built on this Forge."}


async def _queue(request: typing.Any) -> typing.Any:
    """The queue section: what is in it, and everything a press does to it.

    THE ONE SCREEN THAT ANSWERS "I CLOSED THE BROWSER AND CAME BACK". The
    jobs themselves already survive that - ``outbox.chosen_executor`` hands
    them to the server unless unattended execution is switched off - but the
    VIEW of them did not: the list was Gradio-rendered, so a page that came
    back needed the framework to show the job that had run fine without it.

    GET answers with the list, the history and the status sentence; POST
    takes ``{action}`` - ``add``, ``cancel``, ``retry``, ``adopt``,
    ``cancel_all`` - and answers with the same shape, so one code path draws
    the section however it changed.
    """
    if not _signed_in(request):
        return _json({"ok": False, "code": errors.AUTH_BOUNDARY_FAILED, "message": "Sign in first."}, 401)
    tab = _tab()
    if tab is None:
        return _json(_no_tab(), 503)
    if request.method == "GET":
        try:
            return _json(tab.queue_answer(request.query_params.get("page") or ""))
        except Exception as error:
            scrub.console(f"the queue could not be read ({type(error).__name__}).", _LOG_PREFIX)
            return _json({"ok": False, "code": errors.INTERNAL_ERROR, "message": errors.message(errors.INTERNAL_ERROR)}, 500)
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    action = str(body.get("action") or "add")
    page = body.get("page") or ""
    try:
        if action == "add":
            answer = tab.add_to_queue(body.get("prompt") or "", page, body.get("model"),
                                      body.get("enhance") if isinstance(body.get("enhance"), bool) else None)
        elif action == "cancel_all":
            answer = tab.cancel_all(page)
        elif action in ("cancel", "retry", "adopt", "dismiss"):
            answer = tab.outbox_action(f"{action}:{body.get('job') or ''}:{page}", page)
        else:
            return _json({"ok": False, "code": errors.REQUEST_INVALID, "message": f"{action} is not a queue action."}, 400)
    except Exception as error:
        scrub.console(f"a queue action failed ({type(error).__name__}).", _LOG_PREFIX)
        return _json({"ok": False, "code": errors.INTERNAL_ERROR, "message": errors.message(errors.INTERNAL_ERROR)}, 500)
    return _json(dict({"ok": True}, **answer))


def outputs_page(page: typing.Any = 0, size: typing.Any = PAGE_SIZE) -> dict:
    """One page of what WanGP made for this tab, newest first.

    The same shape and the same page size as the picture index, because it
    is the same pager drawing it: a gallery that counted its pages
    differently from the grid beside it would be a second thing to learn
    for no reason.

    A prompt is joined on from the history rather than copied into the
    ledger. The recipe already lives there, keyed by the request that made
    it, and one copy of a prompt is enough.
    """
    listed = outputs.files()
    wanted = clamp_size(size)
    total = len(listed)
    pages = max(1, -(-total // wanted))
    index = page_of(page, pages)
    shown = listed[index * wanted:(index + 1) * wanted]
    prompts = {}
    if shown:
        wanted_ids = {item["request_id"] for item in shown if item["request_id"]}
        if wanted_ids:
            for record in history.load_history():
                found = record.get("request_id")
                if found in wanted_ids and found not in prompts:
                    prompts[found] = record.get("enhanced_prompt") or record.get("prompt_override") or ""
    where = outputs.folder()
    if where is None:
        reason = "unconfigured"
        status = "WanGP has no output folder here yet. Launch it once, or set the folder in Clipboard's settings."
    elif not total:
        reason = "empty"
        status = "Nothing yet. A video appears here after WanGP finishes a request made from this tab."
    else:
        reason = ""
        status = f"{total} output{'s' if total != 1 else ''} from this tab."
    return {
        "ok": True,
        "total": total,
        "page": index,
        "pages": pages,
        "size": wanted,
        "reason": reason,
        "status": status,
        "items": [
            {
                "id": item["id"],
                "name": item["name"],
                "kind": item["kind"],
                "size": item["size"],
                "at": item["at"],
                # Whether WanGP named this file itself or it was matched to
                # the request by when it was written. The gallery says so,
                # because a match is not a fact and pretending otherwise is
                # how a user comes to trust the wrong video.
                "exact": item["exact"],
                "prompt": str(prompts.get(item["request_id"], ""))[:400],
                "url": output_url(item["id"]),
            }
            for item in shown
        ],
    }


def output_url(file_id: str) -> str:
    """The address of one output. Opaque: the path never leaves the server."""
    return f"{OUTPUT_FILE_ROUTE.replace('{file_id}', str(file_id))}"


async def _outputs(request: typing.Any) -> typing.Any:
    """What WanGP made, paged. Reconciles the ledger first - see ``outputs.sync``."""
    if not _signed_in(request):
        return _json({"ok": False, "code": errors.AUTH_BOUNDARY_FAILED, "message": "Sign in first."}, 401)
    params = request.query_params
    try:
        answer = outputs_page(page=params.get("page", 0), size=params.get("size", PAGE_SIZE))
    except Exception as error:
        scrub.console(f"the outputs index could not be read ({type(error).__name__}).", _LOG_PREFIX)
        return _json({"ok": False, "code": errors.INTERNAL_ERROR, "message": errors.message(errors.INTERNAL_ERROR)}, 500)
    return _json(answer)


def _byte_range(header: typing.Any, size: int) -> typing.Optional[typing.Tuple[int, int]]:
    """``bytes=start-end`` against a file of ``size``, or ``None``.

    Only the single-range form, which is the only one a media element ever
    sends. A header that asks for something else is not an error worth a
    416: it is answered with the whole file, which is always a correct
    answer to a range request.
    """
    text = str(header or "").strip()
    if not text.lower().startswith("bytes=") or "," in text:
        return None
    spec = text[6:].strip()
    start_text, _, end_text = spec.partition("-")
    try:
        if not start_text:
            # A suffix range: the last N bytes.
            length = int(end_text)
            if length <= 0:
                return None
            return max(0, size - length), size - 1
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    except (TypeError, ValueError):
        return None
    if start < 0 or start >= size or end < start:
        return None
    return start, min(end, size - 1)


#: How much of a file one range answer will carry. A media element asks for
#: what it needs, but a request for "everything from here" on a two-gigabyte
#: file should not become a two-gigabyte read into memory.
RANGE_CHUNK = 4 * 1024 * 1024


async def _output_file(request: typing.Any) -> typing.Any:
    """One output, by its opaque id, with byte ranges.

    RANGES ARE THE WHOLE POINT. A ``<video>`` element will play a file
    served without them, but it cannot seek in one: the browser has no way
    to ask for the middle, so the scrub bar does nothing. That is the
    difference between a gallery and a list of things that play from the
    start, so this answers 206 with ``Content-Range`` rather than only 200.
    """
    from starlette.responses import Response

    if not _signed_in(request):
        return _json({"ok": False, "code": errors.AUTH_BOUNDARY_FAILED, "message": "Sign in first."}, 401)
    path = outputs.path_of(request.path_params.get("file_id", ""))
    if path is None:
        return _json({"ok": False, "code": errors.CLIPBOARD_ASSET_UNKNOWN, "message": "That output is no longer there."}, 404)
    try:
        size = path.stat().st_size
    except OSError:
        return _json({"ok": False, "code": errors.CLIPBOARD_ASSET_UNKNOWN, "message": "That output is no longer there."}, 404)
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    common = {
        # Said on every answer, range or not: a browser decides whether to
        # bother asking for one from this.
        "Accept-Ranges": "bytes",
        "Cache-Control": REVALIDATED_CACHE,
    }
    if request.method == "HEAD":
        return Response(status_code=200, media_type=mime,
                        headers=dict(common, **{"Content-Length": str(size)}))
    span = _byte_range(request.headers.get("range"), size)
    try:
        with path.open("rb") as handle:
            if span is None:
                data = handle.read()
                return Response(content=data, media_type=mime, headers=dict(common, **{"Content-Length": str(len(data))}))
            start, end = span
            end = min(end, start + RANGE_CHUNK - 1)
            handle.seek(start)
            data = handle.read(end - start + 1)
    except OSError:
        return _json({"ok": False, "code": errors.CLIPBOARD_ASSET_UNKNOWN, "message": "That output could not be read."}, 404)
    return Response(content=data, status_code=206, media_type=mime,
                    headers=dict(common, **{
                        "Content-Range": f"bytes {start}-{start + len(data) - 1}/{size}",
                        "Content-Length": str(len(data)),
                    }))


async def _enhance_settings(request: typing.Any) -> typing.Any:
    """The prompt-enhancement settings, over HTTP: describe, toggle, edit.

    The describe half has existed since the enhancer did; these are the
    write halves it never had - the switch, an override for one of the four
    instruction sets, and the restore that forgets one. Each is
    request-and-response and none of them was ever a push.
    """
    if not _signed_in(request):
        return _json({"ok": False, "code": errors.AUTH_BOUNDARY_FAILED, "message": "Sign in first."}, 401)
    from . import enhance as enhance_module

    if request.method == "GET":
        variant = request.query_params.get("variant") or ""
        mode = request.query_params.get("mode") or ""
        try:
            answer = {"ok": True, "enabled": enhance_module.enabled(),
                      "capabilities": enhance_module.capabilities()}
            if variant or mode:
                text, source = enhance_module.effective_prompt(variant, mode)
                answer["prompt"] = {"variant": variant, "mode": mode, "text": text, "source": source}
            return _json(answer)
        except IntegrationError as error:
            return _refused_here(error)
        except Exception:
            return _json({"ok": False, "code": errors.INTERNAL_ERROR, "message": errors.message(errors.INTERNAL_ERROR)}, 500)
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    action = str(body.get("action") or "")
    try:
        if action == "toggle":
            wanted = enhance_module.set_enabled(bool(body.get("enabled")))
            return _json({"ok": True, "enabled": wanted,
                          "status": f"Prompt enhancement is {'on' if wanted else 'off'}."})
        if action == "override":
            enhance_module.set_override(body.get("variant"), body.get("mode"), body.get("text"))
            text, source = enhance_module.effective_prompt(body.get("variant"), body.get("mode"))
            return _json({"ok": True, "text": text, "source": source,
                          "status": "Override saved. Every enhanced press from now on uses it, "
                                    "on every page, after a restart too."})
        if action == "restore":
            had = enhance_module.clear_override(body.get("variant"), body.get("mode"))
            text, source = enhance_module.effective_prompt(body.get("variant"), body.get("mode"))
            return _json({"ok": True, "text": text, "source": source,
                          "status": "Back to the default." if had else "There was no override; the default is shown."})
    except IntegrationError as error:
        return _refused_here(error)
    except Exception:
        return _json({"ok": False, "code": errors.INTERNAL_ERROR, "message": errors.message(errors.INTERNAL_ERROR)}, 500)
    return _json({"ok": False, "code": errors.REQUEST_INVALID, "message": f"{action or 'that'} is not a settings action."}, 400)


def _refused_here(error: IntegrationError) -> typing.Any:
    return _json(dict(error.as_dict(), status=errors.message(error.code)), 400)


#: What the popup may ask of the intercept route, and the arguments each
#: takes out of the body. One list, read by the route and by the checks.
INTERCEPT_ACTIONS = ("describe", "submit", "cancel", "draft", "history", "history_load", "history_delete", "history_pin")


def intercept_action(body: typing.Mapping[str, typing.Any]) -> typing.Tuple[dict, int]:
    """The popup's one door, as a function: ``(answer, HTTP status)``.

    Every action is request-and-response. ``describe`` is what the popup
    draws itself from; ``submit`` is Generate; ``cancel`` lets the frozen
    picture go; ``draft`` keeps an edited prompt shared with Clipboard when
    the popup closes without generating; the ``history_*`` verbs are the
    list's own buttons. Refusals carry the code's sentence and a 4xx that
    says whether the caller or the world was wrong.
    """
    from . import intercept

    body = body if isinstance(body, dict) else {}
    action = str(body.get("action") or "describe")
    if action not in INTERCEPT_ACTIONS:
        return {"ok": False, "code": errors.REQUEST_INVALID, "message": f"{action} is not a Send to WanGP action."}, 400
    model = body.get("model") if isinstance(body.get("model"), dict) else None
    inputs = body.get("inputs") if isinstance(body.get("inputs"), dict) else None
    try:
        if action == "describe":
            answer = intercept.describe(body.get("handoff"), model, inputs)
        elif action == "submit":
            answer = intercept.submit(
                body.get("handoff"), body.get("prompt") or "", body.get("roles"), body.get("inherit"),
                body.get("enhance") if isinstance(body.get("enhance"), bool) else None,
                body.get("page") or "", model, inputs,
            )
        elif action == "cancel":
            answer = intercept.cancel(body.get("handoff"))
        elif action == "draft":
            answer = intercept.save_prompt(body.get("prompt") or "")
        elif action == "history":
            answer = {"ok": True, "history": intercept.history_view(model, inputs)}
        elif action == "history_load":
            answer = intercept.recipe(body.get("id"), model, inputs)
        elif action == "history_delete":
            removed = intercept.delete_history(body.get("id"))
            answer = {"ok": True, "removed": removed, "history": intercept.history_view(model, inputs)}
        else:
            pinned = intercept.pin_history(body.get("id"), body.get("pinned") is True)
            answer = {"ok": pinned is not None, "entry": pinned, "history": intercept.history_view(model, inputs)}
            if pinned is None:
                answer.update({"code": errors.REQUEST_INVALID, "message": "That history entry is gone."})
    except IntegrationError as error:
        return dict(error.as_dict(), status=errors.message(error.code)), 400
    if answer.get("ok") is False:
        code = str(answer.get("code") or "")
        status = 409 if code in (errors.WANGP_NOT_RUNNING, errors.QUEUE_BUSY, errors.INTERCEPT_IMAGE_EXPIRED) else 400
        return answer, status
    return answer, 200


async def _intercept(request: typing.Any) -> typing.Any:
    """The Send to WanGP popup's route. See ``intercept_action``."""
    if not _signed_in(request):
        return _json({"ok": False, "code": errors.AUTH_BOUNDARY_FAILED, "message": "Sign in first."}, 401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        answer, status = intercept_action(body)
    except Exception as error:
        scrub.console(f"a Send to WanGP action failed ({type(error).__name__}).", _LOG_PREFIX)
        return _json({"ok": False, "code": errors.INTERNAL_ERROR, "message": errors.message(errors.INTERNAL_ERROR)}, 500)
    return _json(answer, status)


async def _intercept_image(request: typing.Any) -> typing.Any:
    """The frozen picture, small, for the popup's thumbnail. By token only."""
    from starlette.responses import Response

    if not _signed_in(request):
        return _json({"ok": False, "code": errors.AUTH_BOUNDARY_FAILED, "message": "Sign in first."}, 401)
    from . import intercept

    try:
        data, mime = intercept.preview(request.path_params.get("token", ""))
    except IntegrationError as error:
        return _json(error.as_dict(), 404)
    except Exception:
        return _json({"ok": False, "code": errors.INTERNAL_ERROR, "message": errors.message(errors.INTERNAL_ERROR)}, 500)
    return Response(content=data, media_type=mime, headers={"Cache-Control": "no-store"})


async def _import(request: typing.Any) -> typing.Any:
    if not _signed_in(request):
        return _json({"ok": False, "code": errors.AUTH_BOUNDARY_FAILED, "message": "Sign in first."}, 401)
    try:
        body = await request.body()
        if len(body) > store_module.MAX_BYTES:
            raise IntegrationError(errors.HANDOFF_TOO_LARGE, f"{len(body)} bytes uploaded")
        name = request.headers.get("x-minipaint-filename") or request.query_params.get("name") or ""
        source = request.query_params.get("source") or "upload"
        asset = store_module.store().import_bytes(body, name, source if source in store_module.SOURCES else "upload")
    except IntegrationError as error:
        return _json(error.as_dict(), 400 if error.code != errors.CLIPBOARD_NOT_CONFIGURED else 409)
    except Exception as error:
        scrub.console(f"an import failed ({type(error).__name__}).", _LOG_PREFIX)
        return _json({"ok": False, "code": errors.INTERNAL_ERROR, "message": errors.message(errors.INTERNAL_ERROR)}, 500)
    return _json({"ok": True, "asset": asset.public()})


async def _send(request: typing.Any) -> typing.Any:
    """Prepare a send without Gradio being involved at all.

    WHY THIS ROUTE EXISTS.

    Sending a picture out of the Clipboard was a hidden box written by
    script and a Gradio event carrying it across the queue. When that queue
    stops delivering - a connection that dropped and did not come back, a
    session the server has forgotten - the box is written and nothing
    happens, for as long as the page stays open. Everything else the tab
    does for a running job survives that, because it rides plain HTTP: the
    event stream, the imports, the thumbnails. The actions did not.

    So the same work is reachable here, over the transport that still
    works. This route only PREPARES the send - it reads the picture and
    returns what the browser needs to complete it - because the two
    destinations that matter most, img2img and Inpaint, are delivered by
    the browser writing a hidden textbox anyway. Those complete with no
    server events at all.

    A destination the backend has to write (Extras, the ImageStitch
    galleries) cannot be completed this way, and says so rather than
    pretending: ``backend`` is true in the answer and the browser reports
    that the connection has to come back for that one.
    """
    if not _signed_in(request):
        return _json({"ok": False, "code": errors.AUTH_BOUNDARY_FAILED, "message": "Sign in first."}, 401)
    try:
        body = await request.json()
    except Exception:
        return _json({"ok": False, "code": errors.REQUEST_INVALID, "message": "A send needs a target and a picture."}, 400)
    target = str((body or {}).get("target") or "")
    asset_id = str((body or {}).get("asset") or "")
    request_text = str((body or {}).get("request") or "")

    # A request and no destination is the browser leaving the request itself
    # here, once it knows how the send went. Nothing is prepared and nothing
    # is read: see ``remember_request`` for what it is for.
    if request_text and not target:
        remember_request(request_text)
        return _json({"ok": True, "recorded": True})

    from . import ui as ui_module

    plan = ui_module.send_plan(target, asset_id)
    if not plan.get("ok"):
        return _json(plan, 400 if plan.get("code") != errors.CLIPBOARD_NOT_CONFIGURED else 409)
    if request_text:
        remember_request(request_text)
    return _json(plan)


#: The last send request the browser posted here, and when.
#:
#: WHAT THIS IS FOR. The Gradio event that records a send reads the request
#: out of a hidden textbox - which means it reads whatever value the host's
#: framework holds for that box, not what the page wrote there. On a page
#: where a scripted write is not heard, those are different: the press
#: arrives carrying a request from some earlier send, or nothing at all, and
#: the user is told about a picture they have moved on from.
#:
#: The browser posts the request here on its way out, over the transport that
#: keeps working when the framework does not, and ``ClipboardTab.send`` falls
#: back to it when the box offers nothing new. It is posted after the picture
#: has been placed rather than before, because the request says whether it
#: was - a send already made is recorded, one that was not is performed, and
#: guessing wrong appends a second picture to a gallery.
#:
#: One slot. A send is a user action on a page, and this is only ever read in
#: the seconds between a press and its receipt.
_last_request: typing.Optional[typing.Tuple[str, float]] = None

#: How long that slot is worth anything. Long enough to cover a slow event,
#: short enough that a request nobody followed up on cannot be picked up by
#: an unrelated press much later.
REQUEST_MEMORY_SECONDS = 120.0


def remember_request(text: str) -> None:
    """Keep this request for ``ClipboardTab.send`` to fall back on."""
    global _last_request

    _last_request = (str(text)[:200], time.monotonic())


def recent_request() -> str:
    """The last request posted to the send route, if it is still fresh."""
    if _last_request is None:
        return ""
    text, at = _last_request
    return text if (time.monotonic() - at) <= REQUEST_MEMORY_SECONDS else ""


def forget_request() -> None:
    """Drop the remembered request. For the checks, and for a fresh tab."""
    global _last_request
    _last_request = None


def install(app: typing.Any) -> None:
    """Put the routes on Forge's FastAPI app. Called from ``on_app_started``."""
    if getattr(app, _INSTALLED_FLAG, False):
        return
    try:
        from starlette.routing import Route

        routes = [
            Route(IMAGE_ROUTE, endpoint=_image, methods=["GET", "HEAD"]),
            Route(LIBRARY_ROUTE, endpoint=_library, methods=["GET"]),
            Route(SETTINGS_ROUTE, endpoint=_settings, methods=["POST"]),
            Route(QUEUE_ROUTE, endpoint=_queue, methods=["GET", "POST"]),
            Route(ENHANCE_SETTINGS_ROUTE, endpoint=_enhance_settings, methods=["GET", "POST"]),
            Route(OUTPUTS_ROUTE, endpoint=_outputs, methods=["GET"]),
            Route(OUTPUT_FILE_ROUTE, endpoint=_output_file, methods=["GET", "HEAD"]),
            Route(IMPORT_ROUTE, endpoint=_import, methods=["POST"]),
            Route(SEND_ROUTE, endpoint=_send, methods=["POST"]),
            Route(INTERCEPT_ROUTE, endpoint=_intercept, methods=["POST"]),
            Route(INTERCEPT_IMAGE_ROUTE, endpoint=_intercept_image, methods=["GET"]),
        ]
        app.router.routes[0:0] = routes
        setattr(app, _INSTALLED_FLAG, True)
    except Exception as error:
        scrub.console(f"the Clipboard routes could not be registered ({error}); thumbnails and paste stay off.", _LOG_PREFIX)
        return
    scrub.console(f"routes ready under {ROUTE_PREFIX}/.", _LOG_PREFIX)
    # The one sweep that is not "occasionally after a write": a Forge that
    # was killed mid-session left whatever it had written, and this is the
    # moment nothing is waiting on the answer. Never load-bearing.
    try:
        store_module.store().sweep_thumbnails()
    except Exception as error:  # pragma: no cover - a cache is never a reason to fail
        scrub.console(f"the thumbnail cache could not be swept ({type(error).__name__}); it will be swept after the next writes.", _LOG_PREFIX)


__all__ = ["IMAGE_ROUTE", "IMMUTABLE_CACHE", "IMPORT_ROUTE", "INTERCEPT_ACTIONS", "INTERCEPT_IMAGE_ROUTE",
           "INTERCEPT_ROUTE", "INTERCEPT_SENTENCES", "LIBRARY_ROUTE", "PAGE_SIZE",
           "PAGE_SIZE_MAX", "PAGE_SIZE_MIN", "REVALIDATED_CACHE", "ROUTE_PREFIX",
           "SEND_ROUTE", "SETTINGS_ROUTE", "QUEUE_ROUTE", "ENHANCE_SETTINGS_ROUTE", "REQUEST_MEMORY_SECONDS",
           "apply_settings", "clamp_size", "forget_request", "image_url", "install", "intercept_action", "library_page",
           "menu_facts", "page_of", "recent_request", "remember_request"]
