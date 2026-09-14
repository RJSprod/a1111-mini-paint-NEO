"""One route of the Canvas's own: a display copy by its id.

``/minipaint-canvas/display/<id>`` serves the bytes a structural edit
prepared for the browser to draw, after ``display.resolve`` has proved the
id names a regular file inside the folder this extension made. It is the
only way those bytes reach a page, and it is deliberately not the WebUI's
``/file=<path>``: a route that takes a path is a route that one day takes
another path.

Three properties, and each of them is load-bearing rather than decorative:

*   **Fixed grammar, no path.** The id is 32 lowercase hex characters. It is
    not sanitised into that shape - a request that is not already that shape
    is refused before anything touches a filesystem - and there is no field
    in the URL for a filename, a folder, a prompt or a session.

*   **The same sign-in as everything else.** Gradio checks its login per
    route, so a route an extension adds is not covered by it; this runs the
    same check the WanGP proxy runs, against the same app.

*   **Immutable, so it may be cached.** One id names one byte sequence until
    it expires, which is what makes a long cache lifetime honest rather than
    a way of showing somebody yesterday's picture.
"""

from __future__ import annotations

import typing

from .. import scrub
from ..wangp import errors
from ..wangp.errors import IntegrationError
from . import display

_LOG_PREFIX = "MiniPaint Canvas:"
_INSTALLED_FLAG = "_minipaint_canvas_routes_installed"

#: How long a browser may keep one. An id is immutable, so this is as long as
#: the object itself lives and no longer; ``immutable`` tells the browser it
#: need never revalidate, which is the whole point of serving a resource
#: rather than a value.
CACHE_CONTROL = f"private, max-age={display.DEFAULT_MAX_AGE_SECONDS}, immutable"


def _signed_in(request: typing.Any) -> bool:
    try:
        from ..wangp import proxy

        return bool(proxy.signed_in(request))
    except Exception:
        return True


def _json(payload: dict, status: int = 200) -> typing.Any:
    from starlette.responses import JSONResponse

    return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})


async def _display(request: typing.Any) -> typing.Any:
    if not _signed_in(request):
        return _json({"ok": False, "code": errors.AUTH_BOUNDARY_FAILED, "message": "Sign in first."}, 401)
    from starlette.responses import FileResponse

    display_id = request.path_params.get("display_id", "")
    try:
        path, content_type = display.resolve(display_id)
    except IntegrationError as error:
        # Both refusals are a 404: "that is not an id of ours" and "that
        # object is gone" are the same fact to a caller, and telling them
        # apart would say which ids exist.
        return _json(error.as_dict(), 404)
    except Exception:
        return _json({"ok": False, "code": errors.INTERNAL_ERROR, "message": errors.message(errors.INTERNAL_ERROR)}, 500)
    return FileResponse(str(path), media_type=content_type, headers={"Cache-Control": CACHE_CONTROL})


def install(app: typing.Any) -> None:
    """Put the route on Forge's FastAPI app. Called from ``on_app_started``."""
    if getattr(app, _INSTALLED_FLAG, False):
        return
    try:
        from starlette.routing import Route

        app.router.routes.insert(0, Route(display.IMAGE_ROUTE, endpoint=_display, methods=["GET", "HEAD"]))
        setattr(app, _INSTALLED_FLAG, True)
    except Exception as error:
        scrub.console(f"the display route could not be registered ({error}); the canvas keeps sending data URLs.", _LOG_PREFIX)


__all__ = ["CACHE_CONTROL", "install"]
