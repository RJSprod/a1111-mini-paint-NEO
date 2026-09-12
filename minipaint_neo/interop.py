"""The server half of the public inter-extension API: ``minipaint.wangp.queue/v1``.

The API itself is browser-centric - ``window.minipaintInterop`` in
``javascript/minipaint_interop.js`` - because the live WanGP form belongs to
the WanGP iframe in one browser page and a server can only guess which page
a caller means. What a server *can* do is hold bytes, and that is all this
module does for a caller: it takes an image, proves it is one, keeps it under
a root it made, and hands back an opaque token. When a request is about to
be queued the tokens - and Clipboard's own asset ids - are turned into
ordinary WanGP handoffs, the same files the Send menu writes, and only their
ids cross into the page. No caller ever names a path, and no answer ever
carries one.

The rest is the shape of a public request, written once here for the
server's benefit and once in the browser wrapper: an image handle is a kind
and an id, a request is a request id plus a prompt and up to three handles,
an omitted field inherits the live page, an unknown kind is refused.

Contained like the other integrations: ``register`` adds the routes and a
startup sweep, and if any of it fails to load, one line says so and nothing
else in the extension notices.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import secrets
import stat
import time
import typing

from . import scrub
from .wangp import errors, handoff, protocol
from .wangp.config import runtime_dir
from .wangp.errors import IntegrationError

#: Where the routes live. A prefix of the extension's own, next to
#: ``/minipaint/log`` and ``/minipaint-clipboard/``.
ROUTE_PREFIX = "/minipaint-interop"
STAGE_ROUTE = ROUTE_PREFIX + "/stage"
PREPARE_ROUTE = ROUTE_PREFIX + "/prepare"
RELEASE_ROUTE = ROUTE_PREFIX + "/release"
CONTRACT_ROUTE = ROUTE_PREFIX + "/contract"

#: The subfolder of the per-run runtime directory that holds staged images.
#: Never configurable, for the reason the handoff root is not.
STAGING_DIRECTORY_NAME = "staging"

#: An upload this size or larger is refused before it is decoded. Half the
#: handoff ceiling: a staged image is re-encoded as PNG on its way into a
#: handoff, and this leaves room for that.
STAGE_MAX_BYTES = protocol.MAX_HANDOFF_BYTES // 2
#: How long a staged image is kept for a request that never came.
STAGE_MAX_AGE_SECONDS = 30 * 60

#: What a caller may upload. Anything else is refused unread; a body that
#: claims one of these and is not is refused decoded.
ACCEPTED_CONTENT_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "application/octet-stream"})
ACCEPTED_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})

#: The two kinds of image handle a request may carry.
KIND_STAGED = "staged"
KIND_CLIPBOARD = "clipboard_asset"
KINDS = (KIND_STAGED, KIND_CLIPBOARD)

#: The image fields of a public request, in the order the contract lists them.
IMAGE_FIELDS = (protocol.QUEUE_FIELD_START, protocol.QUEUE_FIELD_END, protocol.QUEUE_FIELD_REFERENCES)

_LOG_PREFIX = "MiniPaint interop:"
_INSTALLED_FLAG = "_minipaint_interop_installed"


def contract() -> dict:
    """What the API is, by name and number, for a caller that asks."""
    return {
        "ok": True,
        "contract": protocol.QUEUE_CONTRACT,
        "api_version": protocol.PUBLIC_API_VERSION,
        "protocol": protocol.PROTOCOL,
        "prompt_max_chars": protocol.PROMPT_MAX_CHARS,
        "max_references": protocol.MAX_QUEUE_REFERENCES,
        "kinds": list(KINDS),
    }


# ---------------------------------------------------------------- staging --


def staging_root() -> pathlib.Path:
    """``<runtime dir>/staging``, made by us, 0o700 where the platform has it."""
    directory = runtime_dir() / STAGING_DIRECTORY_NAME
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    return directory


def new_token() -> str:
    return secrets.token_hex(16)


def valid_token(value: typing.Any) -> bool:
    return protocol.valid_handoff_id(value)


def _path_for(token: str) -> pathlib.Path:
    if not valid_token(token):
        raise IntegrationError(errors.IMAGE_STAGE_INVALID, "a staging token is 32 lowercase hex characters")
    return staging_root() / (token + protocol.HANDOFF_SUFFIX)


def resolve_staged(token: typing.Any) -> pathlib.Path:
    """The file a token names, proved to be a plain file under our root.

    The same three questions ``handoff.resolve`` asks, with the queue's
    codes: a token of the wrong shape was never ours, a missing file is a
    staged image that expired or was already used, and anything that is not
    a regular file inside the resolved root is refused unopened.
    """
    path = _path_for(str(token))
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise IntegrationError(errors.IMAGE_STAGE_EXPIRED, "the staged image is not there")
    except OSError as error:
        raise IntegrationError(errors.IMAGE_STAGE_EXPIRED, f"the staged image could not be read: {error}")
    if not stat.S_ISREG(info.st_mode):
        raise IntegrationError(errors.IMAGE_STAGE_INVALID, "the staged token does not name a regular file")
    try:
        resolved = path.resolve()
        root = staging_root().resolve()
    except OSError as error:
        raise IntegrationError(errors.IMAGE_STAGE_INVALID, f"the staged image could not be resolved: {error}")
    if os.path.normcase(str(resolved.parent)) != os.path.normcase(str(root)):
        raise IntegrationError(errors.IMAGE_STAGE_INVALID, "the staged image resolves outside its root")
    return resolved


def _refuse_oversized(width: int, height: int) -> None:
    if width < 1 or height < 1:
        raise IntegrationError(errors.IMAGE_STAGE_INVALID, f"degenerate size {width}x{height}")
    if width > protocol.MAX_HANDOFF_SIDE or height > protocol.MAX_HANDOFF_SIDE:
        raise IntegrationError(errors.HANDOFF_TOO_LARGE, f"{width}x{height} exceeds the {protocol.MAX_HANDOFF_SIDE} pixel side limit")
    if width * height > protocol.MAX_HANDOFF_PIXELS:
        raise IntegrationError(errors.HANDOFF_TOO_LARGE, f"{width}x{height} is over the {protocol.MAX_HANDOFF_PIXELS} pixel limit")


def decode_image_bytes(data: bytes) -> typing.Any:
    """Bytes that claim to be an image, as an RGBA PIL image, or a refusal.

    The header is read before the pixels: Pillow announces the size from the
    first bytes and only allocates on ``load``, so an image far over the
    ceiling is refused before it costs memory.
    """
    from PIL import Image

    if not isinstance(data, (bytes, bytearray)) or not data:
        raise IntegrationError(errors.IMAGE_STAGE_INVALID, "no image bytes")
    if len(data) > STAGE_MAX_BYTES:
        raise IntegrationError(errors.HANDOFF_TOO_LARGE, f"{len(data)} bytes, over the {STAGE_MAX_BYTES} limit")
    try:
        with Image.open(io.BytesIO(bytes(data))) as opened:
            if (opened.format or "").upper() not in ACCEPTED_FORMATS:
                raise IntegrationError(errors.IMAGE_STAGE_INVALID, f"format {opened.format!r} is not one of PNG, JPEG, WebP")
            width, height = int(opened.size[0]), int(opened.size[1])
            _refuse_oversized(width, height)
            if getattr(opened, "is_animated", False) and getattr(opened, "n_frames", 1) > 1:
                raise IntegrationError(errors.IMAGE_STAGE_INVALID, "animated images are not staged; the first frame would be a guess")
            opened.load()
            image = opened.convert("RGBA")
    except IntegrationError:
        raise
    except Exception as error:
        raise IntegrationError(errors.IMAGE_STAGE_INVALID, f"the bytes did not decode: {type(error).__name__}")
    return image


def stage_image(image: typing.Any) -> dict:
    """A PIL image into the staging root, as a lossless PNG, behind a token.

    The Python helper a trusted extension may call; the route below is the
    same thing for bytes. The answer is the public handle and the size, and
    nothing about where the file went.
    """
    return _stage(image)


def _stage(image: typing.Any) -> dict:
    from .canvas import imaging

    try:
        width, height = int(image.size[0]), int(image.size[1])
    except Exception as error:
        raise IntegrationError(errors.IMAGE_STAGE_INVALID, f"not a usable image: {error}")
    _refuse_oversized(width, height)
    data = imaging.to_png_bytes(imaging.to_rgba(image))
    if len(data) > protocol.MAX_HANDOFF_BYTES:
        raise IntegrationError(errors.HANDOFF_TOO_LARGE, f"{len(data)} bytes of PNG, over the {protocol.MAX_HANDOFF_BYTES} limit")
    token = new_token()
    path = _path_for(token)
    handoff._write_bytes(path, data)
    return {"ok": True, "image": {"kind": KIND_STAGED, "id": token}, "width": width, "height": height}


def stage_bytes(data: bytes, content_type: str = "") -> dict:
    """Bytes from a caller into the staging root. See ``stage_image``."""
    declared = str(content_type or "").split(";", 1)[0].strip().lower()
    if declared and declared not in ACCEPTED_CONTENT_TYPES:
        raise IntegrationError(errors.IMAGE_STAGE_INVALID, f"content type {declared!r} is not an image this API takes")
    return _stage(decode_image_bytes(data))


def discard_staged(token: typing.Any) -> None:
    """Forget a staged image. Never raises; a bad token removes nothing."""
    try:
        path = _path_for(str(token))
    except IntegrationError:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def sweep_staging(max_age_seconds: int = STAGE_MAX_AGE_SECONDS, now: typing.Optional[float] = None) -> int:
    """Remove staged images nobody asked for, and count them.

    Only files of our own shape, only under our root, never through a link:
    the same discipline as ``handoff.sweep``.
    """
    root = staging_root()
    moment = time.time() if now is None else now
    removed = 0
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0
    for entry in entries:
        head, _, _tail = entry.name.partition(".")
        if not valid_token(head) or not (entry.name == head + protocol.HANDOFF_SUFFIX or entry.name.endswith(handoff.PART_SUFFIX)):
            continue
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            if moment - entry.stat(follow_symlinks=False).st_mtime <= max_age_seconds:
                continue
            os.unlink(entry.path)
        except OSError:
            continue
        removed += 1
    return removed


# ------------------------------------------------------------- the request --


def normalize_handle(raw: typing.Any) -> dict:
    """One image handle: a known kind and a 32-hex id, or a refusal."""
    if not isinstance(raw, dict):
        raise IntegrationError(errors.REQUEST_INVALID, "an image handle is an object with a kind and an id")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise IntegrationError(errors.REQUEST_INVALID, f"unknown image kind {str(kind)[:40]!r}")
    token = raw.get("id")
    if not valid_token(token):
        raise IntegrationError(errors.REQUEST_INVALID, "an image id is 32 lowercase hex characters")
    return {"kind": kind, "id": token}


def normalize_public_request(raw: typing.Any) -> dict:
    """A public request as the API takes it, with its inheritance rules.

    ``request_id`` is minted when absent. ``prompt`` and each image are
    optional and an absent, null or empty one inherits the live page; an
    empty reference list is absence. Anything unlisted is dropped. The
    result never carries a path because there is no field one could go in.
    """
    if not isinstance(raw, dict):
        raise IntegrationError(errors.REQUEST_INVALID, "a queue request is an object")
    request_id = raw.get("request_id")
    if request_id is None or request_id == "":
        request_id = new_token()
    elif not protocol.valid_request_id(request_id):
        raise IntegrationError(errors.REQUEST_INVALID, "a request id is 32 lowercase hex characters")

    prompt = raw.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        raise IntegrationError(errors.REQUEST_INVALID, "the prompt is not text")
    cleaned = protocol.clean_prompt(prompt)
    if cleaned is not None and len(cleaned) > protocol.PROMPT_MAX_CHARS:
        raise IntegrationError(errors.PROMPT_TOO_LONG, f"{len(cleaned)} characters")

    images_raw = raw.get("images")
    if images_raw is not None and not isinstance(images_raw, dict):
        raise IntegrationError(errors.REQUEST_INVALID, "images is an object")
    images: typing.Dict[str, typing.Any] = {}
    images_raw = images_raw or {}
    for field in (protocol.QUEUE_FIELD_START, protocol.QUEUE_FIELD_END):
        handle = images_raw.get(field)
        if handle is None or handle == "":
            continue
        images[field] = normalize_handle(handle)
    references = images_raw.get(protocol.QUEUE_FIELD_REFERENCES)
    if references is not None:
        if not isinstance(references, (list, tuple)):
            raise IntegrationError(errors.REQUEST_INVALID, "references is a list of image handles")
        kept = [normalize_handle(item) for item in references if item is not None and item != ""]
        if len(kept) > protocol.MAX_QUEUE_REFERENCES:
            raise IntegrationError(errors.REQUEST_INVALID, f"more than {protocol.MAX_QUEUE_REFERENCES} reference images")
        if kept:
            images[protocol.QUEUE_FIELD_REFERENCES] = kept

    request: typing.Dict[str, typing.Any] = {"request_id": request_id, "images": images}
    if cleaned is not None:
        request["prompt"] = cleaned
    return request


# ------------------------------------------------------------ preparation --


def _open_handle(handle: typing.Mapping[str, typing.Any]) -> typing.Any:
    """The picture behind a handle, as a PIL image. Never a path."""
    from .canvas import imaging

    if handle["kind"] == KIND_STAGED:
        path = resolve_staged(handle["id"])
        try:
            return imaging.open_file(str(path))
        except Exception as error:
            raise IntegrationError(errors.IMAGE_STAGE_INVALID, f"the staged image did not decode: {type(error).__name__}")
    # A Clipboard asset: the durable library's own resolver proves containment.
    try:
        from .clipboard import store
    except Exception as error:
        raise IntegrationError(errors.CLIPBOARD_NOT_CONFIGURED, f"the Clipboard package is not available ({type(error).__name__})")
    return store.open_image(handle["id"])


def prepare(request: typing.Mapping[str, typing.Any]) -> dict:
    """Turn a normalised public request into the bridge's wire request.

    Every image handle becomes an ordinary WanGP handoff - a lossless PNG
    under the handoff root, named by a fresh id - and the wire request
    carries those ids and the prompt and nothing else. A handle that cannot
    be resolved refuses the whole request, and the handoffs already written
    for it are let go: a half-prepared request is not queued.
    """
    wire: typing.Dict[str, typing.Any] = {"request_id": request["request_id"]}
    if request.get("prompt") is not None:
        wire["prompt"] = request["prompt"]
    written: typing.List[str] = []
    images = request.get("images") if isinstance(request.get("images"), dict) else {}
    try:
        for field, key in ((protocol.QUEUE_FIELD_START, "start_handoff_id"), (protocol.QUEUE_FIELD_END, "end_handoff_id")):
            handle = images.get(field)
            if not handle:
                continue
            prepared = handoff.write(_open_handle(handle))
            written.append(prepared.id)
            wire[key] = prepared.id
        references = images.get(protocol.QUEUE_FIELD_REFERENCES) or []
        if references:
            ids = []
            for handle in references:
                prepared = handoff.write(_open_handle(handle))
                written.append(prepared.id)
                ids.append(prepared.id)
            wire["reference_handoff_ids"] = ids
    except IntegrationError:
        release(written)
        raise
    except Exception as error:
        release(written)
        raise IntegrationError(errors.INTERNAL_ERROR, f"preparing the request failed: {type(error).__name__}")
    wire["handoff_ids"] = list(written)
    return wire


def release(handoff_ids: typing.Any) -> int:
    """Let go of the handoffs a request wrote. Never the durable asset."""
    count = 0
    for item in handoff_ids if isinstance(handoff_ids, (list, tuple)) else []:
        if protocol.valid_handoff_id(item):
            handoff.discard(item)
            count += 1
    return count


# ------------------------------------------------------------------ routes --


def _signed_in(request: typing.Any) -> bool:
    """The same sign-in the WanGP proxy applies, where it is available."""
    try:
        from .wangp import proxy

        return bool(proxy.signed_in(request))
    except Exception:
        return True


def _journal(message: str) -> None:
    try:
        from .wangp import process_log

        process_log.note("interop", message)
    except Exception:
        pass


def _json(payload: dict, status: int = 200) -> typing.Any:
    from starlette.responses import JSONResponse

    return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})


def _refused(error: IntegrationError, status: int = 400) -> typing.Any:
    return _json(error.as_dict(), status)


async def _stage_route(request: typing.Any) -> typing.Any:
    if not _signed_in(request):
        return _json({"ok": False, "code": errors.AUTH_BOUNDARY_FAILED, "message": "Sign in first."}, 401)
    content_type = request.headers.get("content-type", "")
    try:
        # One byte past the ceiling is enough to refuse without holding more.
        body = await request.body()
        if len(body) > STAGE_MAX_BYTES:
            raise IntegrationError(errors.HANDOFF_TOO_LARGE, f"{len(body)} bytes uploaded")
        answer = stage_bytes(body, content_type)
    except IntegrationError as error:
        _journal(f"stage: refused - {error.code}")
        return _refused(error)
    except Exception as error:
        _journal(f"stage: failed - {type(error).__name__}")
        return _json({"ok": False, "code": errors.INTERNAL_ERROR, "message": errors.message(errors.INTERNAL_ERROR)}, 500)
    _journal(f"stage: image staged ({answer['width']}x{answer['height']})")
    return _json(answer)


async def _prepare_route(request: typing.Any) -> typing.Any:
    if not _signed_in(request):
        return _json({"ok": False, "code": errors.AUTH_BOUNDARY_FAILED, "message": "Sign in first."}, 401)
    try:
        raw = await request.json()
    except Exception:
        return _json({"ok": False, "code": errors.REQUEST_INVALID, "message": errors.message(errors.REQUEST_INVALID)}, 400)
    try:
        public = normalize_public_request((raw or {}).get("request") if isinstance(raw, dict) else None)
        wire = prepare(public)
    except IntegrationError as error:
        _journal(f"prepare: refused - {error.code}")
        return _refused(error)
    except Exception as error:
        _journal(f"prepare: failed - {type(error).__name__}")
        return _json({"ok": False, "code": errors.INTERNAL_ERROR, "message": errors.message(errors.INTERNAL_ERROR)}, 500)
    _journal(
        f"prepare {wire['request_id'][:8]}: {len(wire.get('handoff_ids', []))} image(s) prepared; "
        f"overrides {', '.join(protocol.queue_overrides(wire)) or 'none'}"
    )
    return _json({"ok": True, "request": wire})


async def _release_route(request: typing.Any) -> typing.Any:
    if not _signed_in(request):
        return _json({"ok": False, "code": errors.AUTH_BOUNDARY_FAILED, "message": "Sign in first."}, 401)
    try:
        raw = await request.json()
    except Exception:
        raw = {}
    count = release((raw or {}).get("handoff_ids") if isinstance(raw, dict) else None)
    return _json({"ok": True, "released": count})


async def _contract_route(_request: typing.Any) -> typing.Any:
    return _json(contract())


def install(app: typing.Any) -> None:
    """Put the routes on Forge's FastAPI app. Called from ``on_app_started``."""
    if getattr(app, _INSTALLED_FLAG, False):
        return
    try:
        from starlette.routing import Route

        routes = [
            Route(STAGE_ROUTE, endpoint=_stage_route, methods=["POST"]),
            Route(PREPARE_ROUTE, endpoint=_prepare_route, methods=["POST"]),
            Route(RELEASE_ROUTE, endpoint=_release_route, methods=["POST"]),
            Route(CONTRACT_ROUTE, endpoint=_contract_route, methods=["GET"]),
        ]
        app.router.routes[0:0] = routes
        setattr(app, _INSTALLED_FLAG, True)
    except Exception as error:
        scrub.console(f"the interop routes could not be registered ({error}); the public queue API stays off.", _LOG_PREFIX)
        return
    scrub.console(f"public queue API ({protocol.QUEUE_CONTRACT}) ready under {ROUTE_PREFIX}/.", _LOG_PREFIX)


def _on_app_started(_demo: typing.Any, app: typing.Any) -> None:
    install(app)
    try:
        removed = sweep_staging()
        if removed:
            scrub.console(f"cleared {removed} stale staged image(s).", _LOG_PREFIX)
    except Exception as error:  # pragma: no cover - a cleanup is never fatal
        scrub.console(f"the staging folder could not be swept ({error}).", _LOG_PREFIX)


def register(script_callbacks: typing.Any) -> None:
    """Add the routes to the host. The only thing the entry point calls."""
    try:
        script_callbacks.on_app_started(_on_app_started)
    except Exception as error:  # pragma: no cover - depends on the host
        scrub.console(f"the routes could not be registered ({error}).", _LOG_PREFIX)


__all__ = [
    "CONTRACT_ROUTE",
    "KINDS",
    "KIND_CLIPBOARD",
    "KIND_STAGED",
    "PREPARE_ROUTE",
    "RELEASE_ROUTE",
    "STAGE_ROUTE",
    "contract",
    "decode_image_bytes",
    "discard_staged",
    "install",
    "normalize_handle",
    "normalize_public_request",
    "prepare",
    "register",
    "release",
    "resolve_staged",
    "stage_bytes",
    "stage_image",
    "staging_root",
    "sweep_staging",
]
