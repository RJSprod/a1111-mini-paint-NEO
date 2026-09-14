"""Display copies of the canvas's picture, as bytes behind an opaque id.

WHAT THIS REPLACES, AND WHY IT IS NOT SIMPLY SMALLER.

Every structural edit hands the canvas a copy of the picture to draw. That
copy travels as a base64 data URL in a hidden Gradio textbox, which has four
costs and only one of them is size:

*   base64 is a third larger than the bytes it carries;
*   the whole thing is a JSON string value in a Gradio event payload, encoded
    on the server and parsed on the main thread of the browser;
*   Gradio keeps component values per session, so every copy is retained for
    as long as the session is;
*   and the browser cannot cache any of it, because it is a value rather than
    a resource, so an identical picture crosses again on the next edit.

A same-origin URL has none of those. The bytes are written once, in the
format they are already encoded in - JPEG for an opaque picture, WebP where
transparency has to be kept - and the browser fetches them the way it fetches
any other image, with its own cache in front.

THE SECURITY SHAPE IS ``handoff.py``'s, DELIBERATELY.

A route that serves a picture must not be a route that one day serves a file.
So: the id is a fixed grammar and is never sanitised into one; nothing but
that grammar reaches the filesystem; the root is ours and is made by us; a
resolved path is proved to be a regular file directly inside it; and the
route is behind the same sign-in as everything else. There is no path
parameter, no filename, no prompt and no session hash anywhere in the URL,
and the answer is bytes and a content type and nothing else.

An id is immutable: one id names one byte sequence until it expires. That is
what makes the cache headers honest and what lets an out-of-order load be
harmless rather than wrong.

GATED, BECAUSE THE HOST'S HALF IS UNVERIFIED.

Whether Forge's own ForgeCanvas takes an ordinary same-origin URL where it
takes a data URL today is a question about somebody else's JavaScript, and it
cannot be answered by reading it. Until it is answered on a real install the
canvas keeps sending data URLs; everything here is built, tested and ready,
and the setting turns it on. The one thing that is *not* gated is the
server-side read-back: ``surface.py`` must understand a URL reaching it from
a page whatever this setting says, because a page loaded before the setting
changed can still send one.
"""

from __future__ import annotations

import os
import pathlib
import re
import secrets
import stat
import threading
import time
import typing

from ..wangp import errors
from ..wangp.config import runtime_dir
from ..wangp.errors import IntegrationError

#: Where the objects live: a folder of the per-run runtime directory, made by
#: us, never configurable - for the reason the handoff root is not.
DIRECTORY_NAME = "display"

#: The grammar, and the whole of it. An id is 32 lowercase hex characters
#: followed by one of our own suffixes. Not sanitised into that shape -
#: required to be it, and refused otherwise.
DISPLAY_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")

#: The formats a display copy is stored in, and what each is served as. JPEG
#: for an opaque picture, WebP where transparency has to survive, PNG only as
#: the fallback for a Pillow built without WebP.
SUFFIXES: typing.Mapping[str, str] = {
    ".jpg": "image/jpeg",
    ".webp": "image/webp",
    ".png": "image/png",
}
CONTENT_TYPES: typing.Mapping[str, str] = {value: key for key, value in SUFFIXES.items()}

PART_SUFFIX = ".part"

#: How long an object nobody asked for is kept. Shorter than the handoff's
#: six hours because a display copy is superseded by the next edit, and the
#: one that matters is always the newest.
DEFAULT_MAX_AGE_SECONDS = 2 * 3600

#: A ceiling on the folder, so a long session of edits cannot fill a disk.
#: Oldest first, and never the objects a live canvas is still referencing.
MAX_OBJECTS = 64

_lock = threading.RLock()
#: The ids this process has handed out and not let go, newest last. What the
#: sweeper will not touch: the newest of these is on a canvas right now.
_live: typing.List[str] = []


def display_root() -> pathlib.Path:
    """``<runtime dir>/display``, made by us, 0o700 where the platform has it."""
    directory = runtime_dir() / DIRECTORY_NAME
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    return directory


def new_id() -> str:
    return secrets.token_hex(16)


def valid_id(value: typing.Any) -> bool:
    return isinstance(value, str) and bool(DISPLAY_ID_RE.match(value))


def _name(display_id: str, suffix: str) -> str:
    return display_id + suffix


def path_for(display_id: str, suffix: str) -> pathlib.Path:
    """The file an id and a suffix name. Refuses anything that is not ours."""
    if not valid_id(display_id):
        raise IntegrationError(errors.HANDOFF_INVALID_ID, "a display id is 32 lowercase hex characters")
    if suffix not in SUFFIXES:
        raise IntegrationError(errors.HANDOFF_INVALID_ID, f"unknown display format {str(suffix)[:12]!r}")
    return display_root() / _name(display_id, suffix)


def resolve(display_id: typing.Any) -> typing.Tuple[pathlib.Path, str]:
    """``(path, content type)`` for an id, proved to be a file under our root.

    The three questions ``handoff.resolve`` asks, for the same reasons: an id
    of the wrong shape was never ours, a missing file is an object that
    expired or was superseded, and anything that is not a regular file
    directly inside the resolved root is refused unopened - which is what
    stops a symlink planted in the folder from serving something else.
    """
    if not valid_id(display_id):
        raise IntegrationError(errors.HANDOFF_INVALID_ID, "a display id is 32 lowercase hex characters")
    root = display_root()
    for suffix, content_type in SUFFIXES.items():
        path = root / _name(str(display_id), suffix)
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode):
            raise IntegrationError(errors.HANDOFF_INVALID_ID, "that display id does not name a regular file")
        try:
            resolved = path.resolve()
            resolved_root = root.resolve()
        except OSError as error:
            raise IntegrationError(errors.HANDOFF_INVALID_ID, f"the display object could not be resolved: {error}")
        if os.path.normcase(str(resolved.parent)) != os.path.normcase(str(resolved_root)):
            raise IntegrationError(errors.HANDOFF_INVALID_ID, "the display object resolves outside its root")
        return resolved, content_type
    raise IntegrationError(errors.HANDOFF_NOT_FOUND, "the display object is not there")


def _write_bytes(path: pathlib.Path, data: bytes) -> None:
    """Atomically, so a half-written object can never be served."""
    temporary = path.with_name(path.name + PART_SUFFIX)
    with open(temporary, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(temporary), str(path))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def write(data: bytes, content_type: str) -> str:
    """Bytes already in a display format, behind a fresh id. Returns the id.

    Already encoded, and re-encoding them would be the point missed: the
    picture has been turned into a JPEG or a WebP once, and turning that into
    a PNG so it can go through the handoff's wire format would cost both the
    encode and the size the whole change exists to avoid.
    """
    suffix = CONTENT_TYPES.get(str(content_type or "").split(";", 1)[0].strip().lower())
    if suffix is None:
        raise IntegrationError(errors.HANDOFF_INVALID_IMAGE, f"{str(content_type)[:40]!r} is not a display format")
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise IntegrationError(errors.HANDOFF_INVALID_IMAGE, "no display bytes")
    display_id = new_id()
    _write_bytes(path_for(display_id, suffix), bytes(data))
    with _lock:
        _live.append(display_id)
        superseded = _live[:-2] if len(_live) > 2 else []
        del _live[: len(superseded)]
    # Superseded objects go now rather than at the age sweep: the canvas has
    # one picture on it and the one before it may still be loading, and
    # everything older than those two is certainly nobody's.
    for old in superseded:
        discard(old)
    return display_id


def write_image(image: typing.Any) -> typing.Tuple[str, str]:
    """A PIL image as a display object. Returns ``(id, content type)``.

    The same choice ``imaging.display_data_url`` makes, because it is the
    same copy: lossy where the picture is opaque, WebP with its alpha kept
    where it is not, and the document still holds the real pixels.
    """
    from . import imaging

    data, content_type = imaging.display_bytes(image)
    return write(data, content_type), content_type


def discard(display_id: typing.Any) -> None:
    """Forget one object. Never raises; a bad id removes nothing."""
    if not valid_id(display_id):
        return
    root = display_root()
    for suffix in SUFFIXES:
        for name in (_name(str(display_id), suffix), _name(str(display_id), suffix) + PART_SUFFIX):
            try:
                os.unlink(root / name)
            except OSError:
                continue
    with _lock:
        if display_id in _live:
            _live.remove(display_id)


def live_ids() -> typing.List[str]:
    """The objects a canvas may still be referencing. Never swept."""
    with _lock:
        return list(_live)


def sweep(max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS, now: typing.Optional[float] = None) -> int:
    """Objects nobody will ask for again, and a bound on the folder.

    Two things it will not do, and they are the same two the handoff sweeper
    will not do. It will not touch an object this process is still
    referencing - the picture on the canvas now, and the one that may still
    be loading - however old the clock says it is. And it will not follow
    anything out of the root: entries are examined without following links,
    non-files are skipped, and unlinking a symlink removes the link rather
    than whatever it pointed at.
    """
    root = display_root()
    moment = time.time() if now is None else now
    live = set(live_ids())
    removed = 0
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0

    kept: typing.List[typing.Tuple[float, str]] = []
    for entry in entries:
        head, _, _tail = entry.name.partition(".")
        if not valid_id(head) or not any(entry.name.startswith(head + suffix) for suffix in SUFFIXES):
            continue
        if head in live:
            continue
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            stamp = entry.stat(follow_symlinks=False).st_mtime
        except OSError:
            continue
        if moment - stamp > max_age_seconds:
            try:
                os.unlink(entry.path)
            except OSError:
                continue
            removed += 1
            continue
        kept.append((stamp, entry.path))

    # The ceiling, oldest first, after the age rule has had its turn.
    kept.sort()
    while len(kept) + len(live) > MAX_OBJECTS and kept:
        _stamp, path = kept.pop(0)
        try:
            os.unlink(path)
        except OSError:
            continue
        removed += 1
    return removed


# ------------------------------------------------------------------ the url --

#: Where the bytes are served. Under the extension's own prefix, with the id
#: as the last segment and nothing else in it: no path, no filename, no
#: prompt, no session hash - there is no field any of those could go in.
ROUTE_PREFIX = "/minipaint-canvas"
IMAGE_ROUTE = ROUTE_PREFIX + "/display/{display_id}"
_URL_RE = re.compile(re.escape(ROUTE_PREFIX) + r"/display/([0-9a-f]{32})(?:[?#].*)?\Z")


def url_for(display_id: str) -> str:
    """The same-origin URL an ``<img>`` - or the canvas - may be given."""
    if not valid_id(display_id):
        raise IntegrationError(errors.HANDOFF_INVALID_ID, "a display id is 32 lowercase hex characters")
    return f"{ROUTE_PREFIX}/display/{display_id}"


def id_in_url(value: typing.Any) -> str:
    """The id inside one of our URLs, or "" for anything else.

    Matched against the whole string rather than searched for, so a value
    that merely *contains* something shaped like our URL is not one.
    """
    if not isinstance(value, str) or not value:
        return ""
    text = value.strip()
    if text.startswith("http://") or text.startswith("https://"):
        # Same-origin only: a page may hand back the absolute form of the URL
        # it was given, and the path is the part that means anything.
        marker = text.find(ROUTE_PREFIX + "/display/")
        if marker < 0:
            return ""
        text = text[marker:]
    found = _URL_RE.match(text)
    return found.group(1) if found else ""


def enabled() -> bool:
    """Whether the canvas is given URLs rather than data URLs.

    OFF UNTIL SOMEBODY HAS PROVED THE OTHER HALF. Whether Forge's own
    ForgeCanvas takes a same-origin URL where it takes a data URL is a
    question about somebody else's JavaScript on somebody else's install, and
    reading it is not an answer. Everything on this side is built and tested;
    this is the switch, and the prototype is what earns it.
    """
    try:
        from .. import settings

        return bool(settings.display_objects())
    except Exception:
        return False


def reset_for_tests() -> None:
    with _lock:
        _live.clear()


__all__ = [
    "CONTENT_TYPES", "DEFAULT_MAX_AGE_SECONDS", "DIRECTORY_NAME", "DISPLAY_ID_RE", "MAX_OBJECTS",
    "IMAGE_ROUTE", "PART_SUFFIX", "ROUTE_PREFIX", "SUFFIXES", "discard", "display_root", "enabled",
    "id_in_url", "live_ids", "new_id", "path_for", "reset_for_tests", "resolve", "sweep", "url_for",
    "valid_id", "write", "write_image",
]
