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
#: Protocol 4: the queue outbox. A press or an enqueue() is a job the server
#: owns; a page claims the next one it may run and reports how it went.
#: Protocol 5 adds the enhancement stage, cancelling the whole line, and the
#: page's report of where a queued task is in WanGP.
OUTBOX_ROUTE = ROUTE_PREFIX + "/outbox"
OUTBOX_SUBMIT_ROUTE = OUTBOX_ROUTE + "/submit"
OUTBOX_CLAIM_ROUTE = OUTBOX_ROUTE + "/claim"
OUTBOX_REPORT_ROUTE = OUTBOX_ROUTE + "/report"
OUTBOX_CANCEL_ROUTE = OUTBOX_ROUTE + "/cancel"
OUTBOX_RETRY_ROUTE = OUTBOX_ROUTE + "/retry"
OUTBOX_ADOPT_ROUTE = OUTBOX_ROUTE + "/adopt"
#: Protocol 5: the whole line at once, and where a queued job's task is in
#: WanGP as the page that queued it sees it.
OUTBOX_CANCEL_ALL_ROUTE = OUTBOX_ROUTE + "/cancel_all"
OUTBOX_TRACK_ROUTE = OUTBOX_ROUTE + "/track"
#: Prompt enhancement through ModelSwitchRefiner: the switch, the LLM side
#: and the slot rules, for a caller that wants to know before it asks.
ENHANCE_ROUTE = ROUTE_PREFIX + "/enhance"
#: Protocol 6: the event spine. One long-lived stream per page saying what
#: the server is doing, and one authoritative snapshot that repairs a view
#: however badly it has drifted.
#:
#: These are for *observation*, and the distinction is the whole of principle
#: 2.9: losing the stream makes a screen stale and can never stop a job. That
#: is what lets the browser's own timers be removed rather than merely slowed
#: down - there is no longer server work hidden behind a page that has to be
#: prodded every few hundred milliseconds to make it happen.
EVENTS_ROUTE = ROUTE_PREFIX + "/events"
SYNC_ROUTE = ROUTE_PREFIX + "/sync"

#: How often a silent stream says it is alive. SSE comments are invisible to
#: JavaScript, so this is an application-visible frame a page can time
#: against; a page that sees none for a few of these reconnects and syncs.
HEARTBEAT_SECONDS = 15.0
#: How long one stream is held before the page is asked to reconnect. A
#: bounded connection survives a proxy that quietly drops long-lived ones,
#: and reconnecting costs one replay from the cursor.
STREAM_MAX_SECONDS = 30 * 60.0

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
        "start_modes": list(protocol.START_MODES),
        "outbox": OUTBOX_ROUTE,
        "enhance": ENHANCE_ROUTE,
        "track_states": list(protocol.TRACK_STATES),
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

    start = raw.get("start")
    if start is None or start == "":
        start = protocol.START_AUTO
    if start not in protocol.START_MODES:
        raise IntegrationError(errors.REQUEST_INVALID, "start is auto or never")

    request: typing.Dict[str, typing.Any] = {"request_id": request_id, "images": images, "start": start}
    if cleaned is not None:
        request["prompt"] = cleaned
    return request


# ------------------------------------------------------------ preparation --


def open_handle(handle: typing.Mapping[str, typing.Any]) -> typing.Any:
    """The picture behind a handle, as a PIL image. Never a path.

    Public for the one other server-side caller - the prompt enhancer, which
    hands the same picture to ModelSwitchRefiner as an object rather than a
    file - so that a handle is resolved in exactly one place.
    """
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


_open_handle = open_handle


def prepare(request: typing.Mapping[str, typing.Any]) -> dict:
    """Turn a normalised public request into the bridge's wire request.

    Every image handle becomes an ordinary WanGP handoff - a lossless PNG
    under the handoff root, named by a fresh id - and the wire request
    carries those ids and the prompt and nothing else. A handle that cannot
    be resolved refuses the whole request, and the handoffs already written
    for it are let go: a half-prepared request is not queued.
    """
    wire: typing.Dict[str, typing.Any] = {"request_id": request["request_id"], "start": request.get("start") or protocol.START_AUTO}
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
    started = time.monotonic()
    try:
        # One byte past the ceiling is enough to refuse without holding more.
        body = await request.body()
        # Written on arrival, so a send that never reaches Forge is visible as
        # the absence of this line - on 2026-09-23 that absence was the whole
        # diagnosis. A size, never a name.
        _journal(f"stage: received {len(body)} bytes")
        if len(body) > STAGE_MAX_BYTES:
            raise IntegrationError(errors.HANDOFF_TOO_LARGE, f"{len(body)} bytes uploaded")
        # Decoding and re-encoding a picture is CPU work. This route is a
        # coroutine on the server's one event loop - which, under the
        # auto-TLS extension's HTTP/2, serves every request of every page -
        # so the work goes to the threadpool rather than stalling them all.
        from starlette.concurrency import run_in_threadpool

        answer = await run_in_threadpool(stage_bytes, body, content_type)
    except IntegrationError as error:
        _journal(f"stage: refused - {error.code}")
        return _refused(error)
    except Exception as error:
        _journal(f"stage: failed - {type(error).__name__}")
        return _json({"ok": False, "code": errors.INTERNAL_ERROR, "message": errors.message(errors.INTERNAL_ERROR)}, 500)
    elapsed = int((time.monotonic() - started) * 1000)
    _journal(f"stage: image staged ({answer['width']}x{answer['height']}, {elapsed} ms)")
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


# ------------------------------------------------------------ the outbox --


async def _body(request: typing.Any) -> dict:
    try:
        raw = await request.json()
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _outbox():
    from .clipboard import outbox

    return outbox


async def _outbox_list_route(request: typing.Any) -> typing.Any:
    if not _signed_in(request):
        return _json({"ok": False, "code": errors.AUTH_BOUNDARY_FAILED, "message": "Sign in first."}, 401)
    try:
        box = _outbox()
        return _json({"ok": True, "jobs": box.jobs(), "running": box.wangp_running(), "counts": box.counts()})
    except Exception as error:
        _journal(f"outbox: list failed - {type(error).__name__}")
        return _json({"ok": False, "code": errors.INTERNAL_ERROR, "message": errors.message(errors.INTERNAL_ERROR)}, 500)


def _outbox_call(name: str, call: typing.Callable[[dict], typing.Any]):
    async def route(request: typing.Any) -> typing.Any:
        if not _signed_in(request):
            return _json({"ok": False, "code": errors.AUTH_BOUNDARY_FAILED, "message": "Sign in first."}, 401)
        raw = await _body(request)
        try:
            answer = call(raw)
        except IntegrationError as error:
            if error.code not in _QUIET_CODES:
                _journal(f"outbox {name}: refused - {error.code}")
            status = 409 if error.code in _CONFLICT_CODES else 404 if error.code == errors.QUEUE_JOB_UNKNOWN else 400
            return _refused(error, status)
        except Exception as error:
            _journal(f"outbox {name}: failed - {type(error).__name__}")
            return _json({"ok": False, "code": errors.INTERNAL_ERROR, "message": errors.message(errors.INTERNAL_ERROR)}, 500)
        return _json(dict({"ok": True}, **answer))

    route.__name__ = f"_outbox_{name}_route"
    return route


#: Refusals that are the state of the world rather than a caller's mistake:
#: answered 409, and not journalled - a page polls into them.
_CONFLICT_CODES = frozenset({
    errors.WANGP_NOT_RUNNING, errors.QUEUE_BUSY, errors.ENHANCE_UNAVAILABLE, errors.ENHANCE_QUEUE_FULL, errors.ENHANCE_MODEL_UNSUPPORTED,
})
_QUIET_CODES = frozenset({errors.WANGP_NOT_RUNNING, errors.QUEUE_BUSY})


def _submit(raw: dict) -> dict:
    origin = raw.get("origin") if raw.get("origin") in ("clipboard", "api") else "api"
    enhance = raw.get("enhance") if isinstance(raw.get("enhance"), bool) else None
    # A caller may insist on one executor - a script that wants the old
    # browser-driven behaviour, or one that wants the job run whether or not
    # anybody is looking - and otherwise the setting decides.
    executor = raw.get("executor") if raw.get("executor") in _outbox().EXECUTORS else None
    # Provenance, not permission: what the page managed to do about WanGP's
    # live form before it got here. Filtered against the known outcomes so a
    # caller cannot write a sentence of its own into the job's record, and
    # believed rather than checked because there is nothing here that could
    # check it - the fact it reports happened in another process.
    flushed = raw.get("settings_flush")
    flushed = flushed if flushed in protocol.FLUSH_OUTCOMES else ""
    # A caller may say which it wants; the setting decides when it does not.
    inherit = raw.get("inherit") if isinstance(raw.get("inherit"), bool) else None
    return {"job": _outbox().submit(raw.get("request"), raw.get("page"), origin, enhance=enhance,
                                    model=raw.get("model"), executor=executor,
                                    settings_flush=flushed, inherit=inherit)}


def _claim(raw: dict) -> dict:
    box = _outbox()
    if not box.wangp_running():
        raise IntegrationError(errors.WANGP_NOT_RUNNING, "the managed WanGP is not serving")
    return box.claim(raw.get("page"))


def _report(raw: dict) -> dict:
    return {"job": _outbox().report(raw.get("job_id"), raw.get("lease"), raw.get("phase"), raw.get("result"))}


def _cancel(raw: dict) -> dict:
    return {"job": _outbox().cancel(raw.get("job_id"))}


def _retry(raw: dict) -> dict:
    return {"job": _outbox().retry(raw.get("job_id"), raw.get("page"))}


def _adopt(raw: dict) -> dict:
    return {"job": _outbox().adopt(raw.get("job_id"), raw.get("page"))}


def _cancel_all(_raw: dict) -> dict:
    return _outbox().cancel_all()


def _track(raw: dict) -> dict:
    return {"job": _outbox().track(raw.get("job_id"), raw.get("page"), raw)}


async def _enhance_route(request: typing.Any) -> typing.Any:
    if not _signed_in(request):
        return _json({"ok": False, "code": errors.AUTH_BOUNDARY_FAILED, "message": "Sign in first."}, 401)
    try:
        from .clipboard import enhance

        return _json(dict({"ok": True}, **enhance.describe()))
    except Exception as error:
        _journal(f"enhance: describe failed - {type(error).__name__}")
        return _json({"ok": False, "code": errors.INTERNAL_ERROR, "message": errors.message(errors.INTERNAL_ERROR)}, 500)


# ------------------------------------------------------- the event spine --


def _events():
    from . import events

    return events


def _sse(record: typing.Mapping[str, typing.Any]) -> str:
    """One record as an SSE frame.

    A durable broadcast carries its ``id:`` - ``<epoch>:<revision>`` - which
    is what a browser resumes from. A heartbeat and a targeted advisory carry
    none, deliberately: resuming from a liveness frame would hand a page a
    cursor that names nothing, and replaying an advisory would re-offer an
    opportunity that has certainly been taken.
    """
    lines = []
    revision = record.get("revision")
    if revision:
        lines.append(f"id: {_events().cursor(revision)}")
    lines.append(f"event: {record.get('kind') or 'message'}")
    lines.append("data: " + json.dumps(record.get("payload") or {}, separators=(",", ":")))
    return "\n".join(lines) + "\n\n"


def snapshot(page: str = "") -> dict:
    """Everything a returning page needs, at one revision. The authority.

    A sync wins over anything a page is holding, however it got there, and
    taking one is an ordinary thing to do rather than a failure: a page that
    has been asleep, has changed device, or has simply lost confidence asks
    for this and is correct again in one request.

    The revision is read *first*, before the jobs, so a page that applies
    this snapshot and then replays buffered events from that revision cannot
    miss a transition that happened while the snapshot was being built.
    """
    events = _events()
    revision = events.revision()
    payload: typing.Dict[str, typing.Any] = {
        "ok": True,
        "server_epoch": events.epoch(),
        "revision": revision,
        "cursor": events.cursor(revision),
        "heartbeat_seconds": HEARTBEAT_SECONDS,
        "protocol": protocol.PROTOCOL,
        "contract": protocol.QUEUE_CONTRACT,
    }
    try:
        from .clipboard import outbox

        payload["jobs"] = outbox.jobs()
        payload["counts"] = outbox.counts()
        payload["unattended"] = outbox.unattended_enabled()
        # Read here for the same reason ``unattended`` is: the page has to
        # know whether the settings it is looking at are going to be the base
        # of a job before anybody presses anything. See the WanGP bridge's
        # proactive flush - it is the answer to "why did I have to generate
        # once before my LoRA came through".
        payload["inherit_settings"] = outbox.inherit_settings()
    except Exception:
        payload["jobs"] = []
        payload["counts"] = {}
        payload["unattended"] = False
        payload["inherit_settings"] = False
    try:
        from .clipboard import executor

        payload["executor"] = executor.snapshot()
    except Exception:
        payload["executor"] = {}
    payload["runtime"] = _runtime_summary()
    try:
        from .clipboard import enhance

        ready = enhance.capabilities()
        # A dependency summary, scrubbed: what it can do and why not, never
        # a model path, a token or anybody's prompt.
        payload["enhancer"] = {
            "found": ready["found"], "available": ready["available"], "enabled": ready["enabled"],
            "configured": ready["configured"], "vision": ready["vision"], "model": ready["model"],
        }
    except Exception:
        payload["enhancer"] = {}
    try:
        from .clipboard import job_inputs

        payload["inputs"] = job_inputs.counts()
    except Exception:
        payload["inputs"] = {}
    try:
        from .clipboard import store as clipboard_store

        # The library's own generation, so a page coming back from sleep
        # learns in the snapshot it was taking anyway whether the grid it
        # holds is still the library's. Advisory like the event: what the
        # page does about it is ask the index route.
        library = clipboard_store.store()
        payload["library"] = {"revision": library.revision(), "configured": library.configured()}
    except Exception:
        payload["library"] = {}
    if page:
        payload["page"] = page
    return payload


def _runtime_summary() -> dict:
    """The managed WanGP, coarsely, plus whether the card is busy.

    Whether WanGP's own work holds the card is a legitimate thing for a
    returning user to see - it is why their job has been waiting - and it is
    expressed as a busy/free fact and nothing more. No port, no pid, no path.
    """
    out = {"state": "", "running": False, "card_busy": None, "queue_depth": None}
    try:
        from .wangp import runtime

        current = runtime.current()
        out["state"] = current.state
        out["running"] = current.state == runtime.READY
    except Exception:
        return out
    if not out["running"]:
        return out
    try:
        from .wangp import control

        # The CACHED answer, never a fresh one. This runs on the event loop,
        # and every call into the child is a blocking socket read: asking
        # here would stall every page on this Forge for as long as the child
        # took. The executor thread keeps it fresh while the queue moves, and
        # a value too old to mean anything reads as "not known" - which is
        # the honest answer and is what the fields default to.
        hello = control.last_hello()
        if hello is not None:
            out["card_busy"] = hello["generation_running"]
            out["queue_depth"] = hello["queue_depth"]
            out["can_execute"] = hello["can_execute"]
    except Exception:
        pass
    return out


async def _sync_route(request: typing.Any) -> typing.Any:
    if not _signed_in(request):
        return _json({"ok": False, "code": errors.AUTH_BOUNDARY_FAILED, "message": "Sign in first."}, 401)
    page = str(request.query_params.get("page") or "")[:32]
    try:
        return _json(snapshot(page))
    except Exception as error:
        _journal(f"sync: failed - {type(error).__name__}")
        return _json({"ok": False, "code": errors.INTERNAL_ERROR, "message": errors.message(errors.INTERNAL_ERROR)}, 500)


async def _events_route(request: typing.Any) -> typing.Any:
    """One page's live feed of what the server is doing.

    Server-Sent Events over the connection the page already has: no new
    runtime dependency, no second transport, and one stream per page rather
    than one timer per thing a page is interested in.

    The cursor comes from the query string or from ``Last-Event-ID``, which
    is what a browser resends by itself after a dropped connection. A cursor
    this process cannot honour - another epoch, a revision it never issued,
    or one older than the replay ring still holds - is not an error: it is a
    page that needs a snapshot, and it is told so in one frame.
    """
    if not _signed_in(request):
        return _json({"ok": False, "code": errors.AUTH_BOUNDARY_FAILED, "message": "Sign in first."}, 401)
    from starlette.responses import StreamingResponse

    events = _events()
    page = str(request.query_params.get("page") or "")[:32]
    cursor = request.query_params.get("cursor") or request.headers.get("last-event-id") or ""

    async def stream() -> typing.AsyncIterator[str]:
        import asyncio

        started = time.monotonic()
        with events.subscribe(page) as feed:
            missed, reset = events.replay(cursor)
            yield _sse({"kind": "hello", "revision": None, "payload": {
                "server_epoch": events.epoch(),
                "revision": events.revision(),
                "cursor": events.cursor(),
                "heartbeat_seconds": HEARTBEAT_SECONDS,
                "resumed": bool(cursor) and not reset,
            }})
            if reset:
                yield _sse({"kind": events.RESET, "revision": None, "payload": {"reason": "cursor" if cursor else "new"}})
            for record in missed:
                yield _sse(record)
            while True:
                if time.monotonic() - started > STREAM_MAX_SECONDS:
                    # Bounded on purpose: a proxy that quietly drops a
                    # long-lived connection makes a page look connected while
                    # it is not, and reconnecting costs one replay.
                    yield _sse({"kind": events.RESET, "revision": None, "payload": {"reason": "rotate"}})
                    return
                try:
                    record = await feed.next(timeout=HEARTBEAT_SECONDS)
                except asyncio.CancelledError:
                    return
                if feed.overflowed:
                    # This subscriber fell far enough behind that its queue
                    # was given up on. Telling it to resync is honest;
                    # replaying an unbounded backlog into it is not.
                    yield _sse({"kind": events.RESET, "revision": None, "payload": {"reason": "overflow"}})
                    return
                if record is None:
                    # An application-visible frame, because an SSE comment is
                    # invisible to JavaScript and a silent connection and a
                    # dead one look identical to a remote browser.
                    yield _sse({"kind": events.HEARTBEAT, "revision": None, "payload": {"at": int(time.time())}})
                    continue
                yield _sse(record)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store, no-transform",
            "Connection": "keep-alive",
            # Nginx buffers text/event-stream by default and a buffered
            # stream is a stream that arrives in one lump when it closes.
            "X-Accel-Buffering": "no",
        },
    )


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
            Route(OUTBOX_ROUTE, endpoint=_outbox_list_route, methods=["GET"]),
            Route(OUTBOX_SUBMIT_ROUTE, endpoint=_outbox_call("submit", _submit), methods=["POST"]),
            Route(OUTBOX_CLAIM_ROUTE, endpoint=_outbox_call("claim", _claim), methods=["POST"]),
            Route(OUTBOX_REPORT_ROUTE, endpoint=_outbox_call("report", _report), methods=["POST"]),
            Route(OUTBOX_CANCEL_ROUTE, endpoint=_outbox_call("cancel", _cancel), methods=["POST"]),
            Route(OUTBOX_RETRY_ROUTE, endpoint=_outbox_call("retry", _retry), methods=["POST"]),
            Route(OUTBOX_ADOPT_ROUTE, endpoint=_outbox_call("adopt", _adopt), methods=["POST"]),
            Route(OUTBOX_CANCEL_ALL_ROUTE, endpoint=_outbox_call("cancel_all", _cancel_all), methods=["POST"]),
            Route(OUTBOX_TRACK_ROUTE, endpoint=_outbox_call("track", _track), methods=["POST"]),
            Route(ENHANCE_ROUTE, endpoint=_enhance_route, methods=["GET"]),
            Route(EVENTS_ROUTE, endpoint=_events_route, methods=["GET"]),
            Route(SYNC_ROUTE, endpoint=_sync_route, methods=["GET"]),
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
        # The first moment the whole page exists and can be asked what is on
        # it. See ``host.audit`` for what a destination that is not answers to.
        from .canvas import host

        host.audit(_demo)
    except Exception as error:  # pragma: no cover - a report is never fatal
        scrub.console(f"the Send destinations could not be checked ({error}).", _LOG_PREFIX)
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
    "ENHANCE_ROUTE",
    "EVENTS_ROUTE",
    "HEARTBEAT_SECONDS",
    "STREAM_MAX_SECONDS",
    "SYNC_ROUTE",
    "snapshot",
    "OUTBOX_ADOPT_ROUTE",
    "OUTBOX_CANCEL_ALL_ROUTE",
    "OUTBOX_CANCEL_ROUTE",
    "OUTBOX_CLAIM_ROUTE",
    "OUTBOX_REPORT_ROUTE",
    "OUTBOX_RETRY_ROUTE",
    "OUTBOX_ROUTE",
    "OUTBOX_SUBMIT_ROUTE",
    "OUTBOX_TRACK_ROUTE",
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
    "open_handle",
    "prepare",
    "register",
    "release",
    "resolve_staged",
    "stage_bytes",
    "stage_image",
    "staging_root",
    "sweep_staging",
]
