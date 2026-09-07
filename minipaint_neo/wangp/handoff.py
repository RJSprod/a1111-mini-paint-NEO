"""PNGs on their way to WanGP, where the id is the whole transport.

The browser asks for a send and is handed thirty-two hex characters. Nothing
else crosses: not a filename, not a directory, not a temp path with the user's
name in it. That is the entire security posture of this module, and it is a
posture rather than a check because every design in which a path travels is a
design in which, one day, a different path travels. A fixed-shape id cannot be
made to mean "one directory up"; a path can, and no amount of sanitising it
afterwards is as reliable as never having accepted one. So the id is minted
here, the filename is derived from the id, the directory is made here, and the
only file this module will ever open is the one that construction produces.

The manifest - what was written, how big it was, what it hashed to - lives in
this process's memory and nowhere else. Writing it beside the PNG would leave a
file whose whole purpose is to name a handoff, which is precisely the thing
section 9 says never survives a restart; keeping it in memory also gives the
sweeper the one fact it cannot get from the filesystem, namely which files are
still in flight and must not be collected out from under a send.

The WanGP side re-does all of this from its own copy: it is installed into
someone else's Python environment and cannot import us. ``validate_file`` is
the reference version of the checks its ``handoff.py`` mirrors, which is why it
takes a path and a manifest rather than an id - by then the id has already done
its job.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import os
import pathlib
import secrets
import stat
import tempfile
import threading
import time
import typing

from PIL import Image

from ..canvas import imaging
from . import protocol
from .config import runtime_dir
from .errors import (
    HANDOFF_DIGEST_MISMATCH,
    HANDOFF_INVALID_ID,
    HANDOFF_INVALID_IMAGE,
    HANDOFF_NOT_FOUND,
    HANDOFF_TOO_LARGE,
    IntegrationError,
)

#: The subfolder of the per-run runtime directory. Never configurable: a
#: settable handoff root is a settable destination for whatever the send path
#: is holding, and there is no question a user could answer better than this.
DIRECTORY_NAME = "handoff"

CONTENT_TYPE = "image/png"

#: The first eight bytes of every PNG. Checked before Pillow is handed the
#: data, because "the file decoded" and "the file was the type we asked for"
#: are different claims and only the second one is about trust.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: Half-written handoffs get this while they are being written, so a reader
#: never sees a truncated PNG under the real name and the sweeper can still
#: recognise the debris if the process dies mid-write.
PART_SUFFIX = ".part"

#: Files older than this are collected on startup. Six hours is long past the
#: point where a send is still going to happen and short of "the user's disk
#: quietly filled up".
DEFAULT_MAX_AGE_SECONDS = 6 * 3600

#: Everything ever written by this process and not yet discarded, keyed by id.
#: In memory on purpose - see the module docstring - and behind a lock because
#: a Gradio worker writes while an app-startup sweep walks the directory.
_lock = threading.Lock()
_manifests: typing.Dict[str, dict] = {}


@dataclasses.dataclass
class Handoff:
    """One prepared image: the id that travels, the path that does not."""

    id: str
    path: pathlib.Path
    manifest: dict


# ------------------------------------------------------------------- root --


def handoff_root() -> pathlib.Path:
    """``<runtime dir>/handoff``, created if it is not there.

    Made by us, so that no filename under it was ever chosen by anybody else.
    0o700 where the platform has the concept: the contents are the user's
    picture, on a machine that may have other accounts on it.
    """
    directory = runtime_dir() / DIRECTORY_NAME
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass  # Windows, and most network filesystems; the path is still ours
    return directory


def new_id() -> str:
    """128 bits of urandom as lowercase hex - exactly ``HANDOFF_ID_RE``."""
    return secrets.token_hex(16)


def path_for(handoff_id: str) -> pathlib.Path:
    """The file an id names, or an error. Nothing in between.

    The id is not cleaned, stripped or normalised: it either matches the one
    shape this extension issues or it is refused. A rejection is a fixed
    decision about a fixed grammar, whereas a sanitiser is a running argument
    with whoever is supplying the input, and that argument gets lost eventually.
    """
    if not protocol.valid_handoff_id(handoff_id):
        raise IntegrationError(HANDOFF_INVALID_ID, f"id is not 32 lowercase hex characters: {handoff_id!r}")
    return handoff_root() / (handoff_id + protocol.HANDOFF_SUFFIX)


def _same_directory(left: pathlib.Path, right: pathlib.Path) -> bool:
    """Two resolved paths naming one directory, as this platform sees it."""
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def resolve(handoff_id: str) -> pathlib.Path:
    """The file an id names, proved to be a real file of ours.

    Three separate questions, because they have three different answers when
    something is wrong. Is the id ours - ``path_for``. Is anything there - a
    handoff that was swept or discarded early is HANDOFF_NOT_FOUND, which is a
    timing accident rather than an attack. Is what is there a plain file that
    is still inside our directory - and that is the one that has to survive
    Windows.

    ``os.lstat`` settles a symlinked *file*: ``S_ISREG`` is false for a link,
    so a link planted under our name is refused without ever being opened. It
    does not settle a junction planted at a *directory* in the path, which
    Windows follows transparently and which lstat reports as an ordinary
    directory. So the path is resolved and its parent compared against the
    resolved root: if the two have stopped agreeing, the id is naming a file
    somewhere else and is refused rather than read.
    """
    path = path_for(handoff_id)

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise IntegrationError(HANDOFF_NOT_FOUND, f"{handoff_id} is not in the handoff root")
    except OSError as error:
        raise IntegrationError(HANDOFF_NOT_FOUND, f"{handoff_id}: {error}")

    if not stat.S_ISREG(info.st_mode):
        # Not "invalid image": we never wrote this, whatever it is.
        raise IntegrationError(HANDOFF_INVALID_ID, f"{handoff_id} is not a regular file")

    try:
        resolved = path.resolve()
        root = handoff_root().resolve()
    except OSError as error:
        raise IntegrationError(HANDOFF_INVALID_ID, f"{handoff_id} could not be resolved: {error}")

    if not _same_directory(resolved.parent, root):
        raise IntegrationError(HANDOFF_INVALID_ID, f"{handoff_id} resolves outside the handoff root")
    return resolved


# ------------------------------------------------------------------ write --


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _measure(image: typing.Any) -> typing.Tuple[int, int]:
    """The size of something that claims to be a PIL image."""
    try:
        width, height = int(image.size[0]), int(image.size[1])
    except (AttributeError, TypeError, ValueError, IndexError) as error:
        raise IntegrationError(HANDOFF_INVALID_IMAGE, f"not a usable image: {error}")
    if width < 1 or height < 1:
        raise IntegrationError(HANDOFF_INVALID_IMAGE, f"degenerate size {width}x{height}")
    return width, height


def _refuse_oversized(width: int, height: int, size_bytes: typing.Optional[int] = None) -> None:
    """The ceilings, as three separate sentences in the log.

    The code is what makes the screen say "the image is too large for the
    WanGP handoff" rather than something about an internal error; the detail
    says which of the three limits it was, and is for the log only.
    """
    if width > protocol.MAX_HANDOFF_SIDE or height > protocol.MAX_HANDOFF_SIDE:
        raise IntegrationError(
            HANDOFF_TOO_LARGE,
            f"{width}x{height} exceeds the {protocol.MAX_HANDOFF_SIDE} pixel side limit",
        )
    if width * height > protocol.MAX_HANDOFF_PIXELS:
        raise IntegrationError(
            HANDOFF_TOO_LARGE,
            f"{width}x{height} is {width * height} pixels, over the {protocol.MAX_HANDOFF_PIXELS} limit",
        )
    if size_bytes is not None and size_bytes > protocol.MAX_HANDOFF_BYTES:
        raise IntegrationError(
            HANDOFF_TOO_LARGE,
            f"{size_bytes} bytes of PNG, over the {protocol.MAX_HANDOFF_BYTES} limit",
        )


def _write_bytes(path: pathlib.Path, data: bytes) -> None:
    """Put ``data`` at ``path`` so a reader sees all of it or none of it.

    The reader is a different process on the other side of the proxy, and it
    is told to look the moment the send is issued. A plain open-and-write
    would let it find a PNG that stops halfway; a rename within the directory
    cannot. ``mkstemp`` makes the temp file 0600, which is the mode the final
    file should have anyway.
    """
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.stem + ".", suffix=PART_SUFFIX)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            try:
                os.fsync(stream.fileno())
            except (OSError, AttributeError, ValueError):
                pass  # not every filesystem or platform supports it
        os.replace(temp_name, str(path))
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def write(image: Image.Image) -> Handoff:
    """Export one image as a lossless PNG handoff and record what it was.

    The caller has already flattened the visible layers - that is MiniPaint's
    job and it happens in the canvas - so what arrives here is the picture as
    the user sees it. It is encoded as PNG and only as PNG: the display copy
    the browser was showing is JPEG or lossy WebP, and handing a generator a
    re-compressed approximation of what the user drew would be a silent
    quality loss nobody asked for.

    The manifest is registered *before* the bytes land, so that a sweep which
    happens to run during a large encode cannot collect the file between the
    rename and the bookkeeping.
    """
    width, height = _measure(image)
    _refuse_oversized(width, height)

    data = imaging.to_png_bytes(imaging.to_rgba(image))
    _refuse_oversized(width, height, len(data))

    handoff_id = new_id()
    path = path_for(handoff_id)
    manifest = {
        "id": handoff_id,
        "created_at": _now_iso(),
        "content_type": CONTENT_TYPE,
        "bytes": len(data),
        "width": width,
        "height": height,
        "sha256": hashlib.sha256(data).hexdigest(),
    }

    with _lock:
        _manifests[handoff_id] = manifest
    try:
        _write_bytes(path, data)
    except BaseException:
        with _lock:
            _manifests.pop(handoff_id, None)
        raise

    return Handoff(id=handoff_id, path=path, manifest=dict(manifest))


def manifest_of(handoff_id: typing.Any) -> typing.Optional[dict]:
    """What this process recorded for an id, or None once it is discarded."""
    if not protocol.valid_handoff_id(handoff_id):
        return None
    with _lock:
        manifest = _manifests.get(handoff_id)
    return dict(manifest) if manifest is not None else None


# --------------------------------------------------------------- validate --


def _ihdr_size(data: bytes) -> typing.Optional[typing.Tuple[int, int]]:
    """Width and height straight out of the PNG header, without decoding.

    A PNG announces its dimensions in the first twenty-four bytes and only
    then asks for that much memory. Reading them first means an image far over
    the ceiling is refused before Pillow allocates anything for it, which is
    the difference between "too large" and a decompression bomb.
    """
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def validate_file(path: typing.Any, manifest: typing.Optional[dict] = None) -> dict:
    """Everything that has to be true of a handoff file before it is used.

    This is the check the receiving side runs, so it assumes nothing about who
    wrote the file: existence, a plain file, the extension we issue, the size
    ceiling, the PNG signature, an actual successful decode, sane dimensions,
    and - when the sender said what it wrote - a digest that still matches.
    Each failure gets its own code, because "the file is not a PNG", "the file
    is enormous" and "the file is not the one that was prepared" are three
    different things to tell somebody.

    Returns the facts it established, shaped like the manifest's own fields so
    a caller can compare or log them. Deliberately without the path: a manifest
    never carries one, and this dictionary ends up in logs.
    """
    path = pathlib.Path(str(path))

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise IntegrationError(HANDOFF_NOT_FOUND, f"{path.name} is not there")
    except OSError as error:
        raise IntegrationError(HANDOFF_NOT_FOUND, f"{path.name}: {error}")

    if not stat.S_ISREG(info.st_mode):
        raise IntegrationError(HANDOFF_INVALID_IMAGE, f"{path.name} is not a regular file")
    if path.suffix.lower() != protocol.HANDOFF_SUFFIX:
        raise IntegrationError(HANDOFF_INVALID_IMAGE, f"{path.name} is not a {protocol.HANDOFF_SUFFIX} file")
    if info.st_size > protocol.MAX_HANDOFF_BYTES:
        raise IntegrationError(
            HANDOFF_TOO_LARGE, f"{info.st_size} bytes on disk, over the {protocol.MAX_HANDOFF_BYTES} limit"
        )

    # One byte past the ceiling is enough to know: the size above was a
    # promise made by stat, and this is the read that keeps it.
    try:
        with open(path, "rb") as stream:
            data = stream.read(protocol.MAX_HANDOFF_BYTES + 1)
    except OSError as error:
        raise IntegrationError(HANDOFF_INVALID_IMAGE, f"{path.name} could not be read: {error}")

    if len(data) > protocol.MAX_HANDOFF_BYTES:
        raise IntegrationError(
            HANDOFF_TOO_LARGE, f"more than {protocol.MAX_HANDOFF_BYTES} bytes of file"
        )
    if not data.startswith(PNG_SIGNATURE):
        raise IntegrationError(HANDOFF_INVALID_IMAGE, f"{path.name} does not start with a PNG signature")

    header = _ihdr_size(data)
    if header is None:
        raise IntegrationError(HANDOFF_INVALID_IMAGE, f"{path.name} has no readable PNG header")
    _refuse_oversized(header[0], header[1])

    try:
        decoded = imaging.from_png_bytes(data)
        width, height = _measure(decoded)
    except IntegrationError:
        raise
    except Exception as error:
        # Pillow raises a whole family of things for a broken file, plus its
        # own bomb errors; none of them are worth distinguishing here.
        raise IntegrationError(HANDOFF_INVALID_IMAGE, f"{path.name} did not decode: {error}")
    _refuse_oversized(width, height)

    facts = {
        "content_type": CONTENT_TYPE,
        "bytes": len(data),
        "width": width,
        "height": height,
        "sha256": hashlib.sha256(data).hexdigest(),
    }

    if isinstance(manifest, dict):
        # A file that decodes but is not the file that was prepared is the
        # worst of the three failures, because everything else about the send
        # still looks correct. Size and dimensions are checked alongside the
        # digest so a mismatch is described rather than merely reported.
        for key in ("sha256", "bytes", "width", "height"):
            expected = manifest.get(key)
            if expected is not None and facts[key] != expected:
                raise IntegrationError(
                    HANDOFF_DIGEST_MISMATCH, f"{key} is {facts[key]!r}, the manifest said {expected!r}"
                )

    return facts


# ---------------------------------------------------------------- cleanup --


def discard(handoff_id: typing.Any) -> None:
    """Forget an id and remove its file. Never raises.

    Called on the way out of a send that worked and a send that failed, which
    is exactly when an exception is least welcome: the interesting outcome has
    already happened and been logged, and a cleanup error would replace it.
    The path still goes through ``path_for``, so a bad id removes nothing at
    all rather than removing something else.
    """
    with _lock:
        if isinstance(handoff_id, str):
            _manifests.pop(handoff_id, None)

    try:
        path = path_for(handoff_id)
    except IntegrationError:
        return
    try:
        os.unlink(path)
    except OSError:
        pass  # already gone, held open by the reader, or not ours to remove


def _sweepable_id(name: str) -> typing.Optional[str]:
    """The handoff id a filename in our root belongs to, if it is one of ours.

    Only two shapes are ever created here - ``<id>.png`` and the ``<id>.*.part``
    a write in progress uses - and anything else in the directory was not put
    there by this module. The sweeper leaves those alone: deleting an unknown
    file because it is in a directory we happen to own is how a cleanup pass
    turns into data loss.
    """
    head, _, tail = name.partition(".")
    if not protocol.valid_handoff_id(head):
        return None
    if name == head + protocol.HANDOFF_SUFFIX or name.endswith(PART_SUFFIX):
        return head
    return None


def sweep(max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS, now: typing.Optional[float] = None) -> int:
    """Remove stale handoff files under the root, and count them.

    Runs at startup, where "stale" means "written by a Forge that is no longer
    running". Two things it will not do. It will not touch a handoff this
    process still has a manifest for, however old the clock says it is - an
    in-flight send owns its file until it discards it, and a slow generation
    is not an abandoned one. And it will not follow anything out of the root:
    entries are examined without following links, non-files are skipped, and
    unlinking a symlink removes the link and not whatever it pointed at.
    """
    root = handoff_root()
    moment = time.time() if now is None else now
    removed = 0

    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0

    with _lock:
        live = set(_manifests)

    for entry in entries:
        handoff_id = _sweepable_id(entry.name)
        if handoff_id is None or handoff_id in live:
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
