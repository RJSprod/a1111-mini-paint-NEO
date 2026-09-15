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

import time
import typing

from .. import scrub
from ..wangp import errors
from ..wangp.errors import IntegrationError
from . import store as store_module

ROUTE_PREFIX = "/minipaint-clipboard"
IMAGE_ROUTE = ROUTE_PREFIX + "/image/{asset_id}"
IMPORT_ROUTE = ROUTE_PREFIX + "/import"
SEND_ROUTE = ROUTE_PREFIX + "/send"

_LOG_PREFIX = "MiniPaint Clipboard:"
_INSTALLED_FLAG = "_minipaint_clipboard_installed"


def image_url(asset_id: str, thumb: bool = True, version: typing.Any = "") -> str:
    """The URL the tab puts in an ``<img>``: the id, never the name."""
    query = []
    if thumb:
        query.append("thumb=1")
    if version:
        query.append(f"v={version}")
    return f"{ROUTE_PREFIX}/image/{asset_id}" + (("?" + "&".join(query)) if query else "")


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
    return Response(content=data, media_type=mime, headers={"Cache-Control": "private, max-age=3600"})


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
            Route(IMPORT_ROUTE, endpoint=_import, methods=["POST"]),
            Route(SEND_ROUTE, endpoint=_send, methods=["POST"]),
        ]
        app.router.routes[0:0] = routes
        setattr(app, _INSTALLED_FLAG, True)
    except Exception as error:
        scrub.console(f"the Clipboard routes could not be registered ({error}); thumbnails and paste stay off.", _LOG_PREFIX)
        return
    scrub.console(f"routes ready under {ROUTE_PREFIX}/.", _LOG_PREFIX)


__all__ = ["IMAGE_ROUTE", "IMPORT_ROUTE", "ROUTE_PREFIX", "SEND_ROUTE", "REQUEST_MEMORY_SECONDS",
           "forget_request", "image_url", "install", "recent_request", "remember_request"]
