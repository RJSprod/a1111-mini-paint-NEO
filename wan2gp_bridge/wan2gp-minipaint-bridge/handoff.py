"""Reading the picture Forge left for us, and refusing everything else.

The browser never tells this plugin where a file is. It tells it an id: 32
lowercase hex characters, nothing else, no dot and no separator. The path is
built here, from a root that arrived in an environment variable set by the
process that launched WanGP, and it is built the only way it is ever built -
``root / (id + ".png")``. That is the entire transport, and it is shaped that
way on purpose: a path that is never accepted cannot be traversed, and an id
whose syntax is *required* rather than *sanitised* has no clever spelling
that survives. An id that is not exactly right is rejected outright; nothing
here strips a character and tries again.

What follows the path is section 20.4's list, in order and all of it: the file
exists, it is a regular file, it is under the root once both are resolved, it
carries the expected extension, it is smaller than the ceiling, it starts with
a PNG signature, it decodes, its dimensions are sane, and its digest matches
when Forge supplied one. Each of those has its own failure code, because "the
prepared image was not a readable PNG" and "the image is too large for the
WanGP handoff" are different things for the person reading the message.

The module deliberately does not delete anything. Cleanup of the handoff root
belongs to the side that created it - this plugin has no business removing
files from a directory another process owns, and a bridge that tidied up
would race the very sweep that owns the job.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import os
import pathlib
import typing

try:
    from . import compatibility, protocol
except ImportError:  # pragma: no cover - depends on how WanGP imports plugins
    import compatibility  # type: ignore[no-redef]
    import protocol  # type: ignore[no-redef]

try:
    from PIL import Image
except Exception:  # pragma: no cover - Pillow ships with WanGP; never assume
    Image = None  # type: ignore[assignment]


#: The first eight bytes of every PNG. Checked before the decoder is handed
#: the file, so a mislabelled JPEG or an HTML error page fails as "not a
#: readable PNG" rather than somewhere inside Pillow.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclasses.dataclass(frozen=True)
class LoadedHandoff:
    """One validated image, and the facts the acknowledgement will quote."""

    id: str
    image: typing.Any
    width: int
    height: int
    bytes: int
    sha256: str
    pixel_digest: str

    def as_source(self) -> dict:
        return {
            "width": self.width,
            "height": self.height,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "pixel_digest": self.pixel_digest,
        }


def handoff_root(environ: typing.Optional[typing.Mapping[str, str]] = None) -> pathlib.Path:
    """The one directory a handoff may come from.

    Absent means WanGP was not launched by the integration, which is a
    configuration answer rather than an image answer - so it is
    ``HANDOFF_NOT_FOUND`` with a detail that says why, not an invalid-image
    complaint about a file nobody looked for.
    """
    source = os.environ if environ is None else environ
    raw = str(source.get(compatibility.ENV_HANDOFF_ROOT) or "").strip()
    if not raw:
        raise compatibility.BridgeError(
            compatibility.HANDOFF_NOT_FOUND,
            f"{compatibility.ENV_HANDOFF_ROOT} is not set; this WanGP was not started by Mini Paint",
        )
    return pathlib.Path(raw)


def path_for(handoff_id: typing.Any, environ: typing.Optional[typing.Mapping[str, str]] = None) -> pathlib.Path:
    """``root/<id>.png``, for an id that is already known to be well formed."""
    if not protocol.valid_handoff_id(handoff_id):
        raise compatibility.BridgeError(compatibility.HANDOFF_INVALID_ID, "a handoff id is 32 lowercase hex characters")
    return handoff_root(environ) / (str(handoff_id) + protocol.HANDOFF_SUFFIX)


def resolve(handoff_id: typing.Any, environ: typing.Optional[typing.Mapping[str, str]] = None) -> pathlib.Path:
    """The file for an id, having proved it is a file and it is ours.

    The containment check is done on both resolved paths because Windows
    junctions and POSIX symlinks are exactly the case section 20.5 raises: a
    name that looks like a child of the root is not necessarily a child of the
    root once it is followed.
    """
    path = path_for(handoff_id, environ)
    root = handoff_root(environ)

    try:
        resolved = path.resolve()
        resolved_root = root.resolve()
    except OSError as error:
        raise compatibility.BridgeError(compatibility.HANDOFF_NOT_FOUND, str(error))

    if resolved.parent != resolved_root:
        raise compatibility.BridgeError(compatibility.HANDOFF_INVALID_ID, "the handoff resolves outside its root")
    if resolved.suffix.lower() != protocol.HANDOFF_SUFFIX:
        raise compatibility.BridgeError(compatibility.HANDOFF_INVALID_ID, "unexpected handoff extension")
    if not resolved.exists():
        raise compatibility.BridgeError(compatibility.HANDOFF_NOT_FOUND, "the prepared image is no longer there")
    if not resolved.is_file():
        raise compatibility.BridgeError(compatibility.HANDOFF_INVALID_IMAGE, "the handoff is not a regular file")
    return resolved


def pixel_digest(image: typing.Any) -> str:
    """A digest of what the image *is*, not of how it was encoded.

    Section 23.3: a file SHA-256 proves transport and stops proving anything
    the moment Gradio decodes and re-encodes. Normalising to RGBA and hashing
    the raw bytes with the dimensions gives both halves of the bridge a value
    that survives a re-encode, which is what "pixel-equivalent" means.
    """
    if image is None:
        return ""
    try:
        converted = image if getattr(image, "mode", "") == "RGBA" else image.convert("RGBA")
        digest = hashlib.sha256()
        digest.update(f"{converted.width}x{converted.height}:".encode("ascii"))
        digest.update(converted.tobytes())
        return digest.hexdigest()
    except Exception:
        return ""


def _refuse_oversized(size: int, width: int = 0, height: int = 0) -> None:
    if size > protocol.MAX_HANDOFF_BYTES:
        raise compatibility.BridgeError(compatibility.HANDOFF_TOO_LARGE, f"{size} bytes")
    if width and height:
        if width > protocol.MAX_HANDOFF_SIDE or height > protocol.MAX_HANDOFF_SIDE:
            raise compatibility.BridgeError(compatibility.HANDOFF_TOO_LARGE, f"{width}x{height}")
        if width * height > protocol.MAX_HANDOFF_PIXELS:
            raise compatibility.BridgeError(compatibility.HANDOFF_TOO_LARGE, f"{width}x{height}")


def load(
    handoff_id: typing.Any,
    source: typing.Any = None,
    environ: typing.Optional[typing.Mapping[str, str]] = None,
) -> LoadedHandoff:
    """Section 20.4, in order, and then an image nobody else has touched.

    ``source`` is the manifest Forge sent with the request: dimensions and a
    digest it computed before the file left. It is optional because the file
    is validated on its own merits either way, and it is checked when present
    because a file that changed between the two processes is the one case no
    amount of local validation can see.
    """
    if Image is None:
        raise compatibility.BridgeError(compatibility.INTERNAL_ERROR, "Pillow is not available in this WanGP environment")

    path = resolve(handoff_id, environ)

    try:
        size = path.stat().st_size
    except OSError as error:
        raise compatibility.BridgeError(compatibility.HANDOFF_NOT_FOUND, str(error))
    _refuse_oversized(size)

    try:
        data = path.read_bytes()
    except OSError as error:
        raise compatibility.BridgeError(compatibility.HANDOFF_NOT_FOUND, str(error))
    if not data.startswith(PNG_SIGNATURE):
        raise compatibility.BridgeError(compatibility.HANDOFF_INVALID_IMAGE, "no PNG signature")

    digest = hashlib.sha256(data).hexdigest()
    expected = _expected_digest(source)
    if expected and expected != digest:
        raise compatibility.BridgeError(compatibility.HANDOFF_DIGEST_MISMATCH, "the file changed on the way here")

    try:
        with Image.open(io.BytesIO(data)) as opened:
            if (opened.format or "").upper() != "PNG":
                raise compatibility.BridgeError(compatibility.HANDOFF_INVALID_IMAGE, f"format {opened.format!r}")
            opened.load()
            image = opened.convert("RGBA")
    except compatibility.BridgeError:
        raise
    except Exception as error:
        raise compatibility.BridgeError(compatibility.HANDOFF_INVALID_IMAGE, str(error))

    width, height = int(image.width), int(image.height)
    if width < 1 or height < 1:
        raise compatibility.BridgeError(compatibility.HANDOFF_INVALID_IMAGE, "the image has no area")
    _refuse_oversized(size, width, height)

    _check_dimensions(source, width, height)

    return LoadedHandoff(
        id=str(handoff_id),
        image=image,
        width=width,
        height=height,
        bytes=len(data),
        sha256=digest,
        pixel_digest=pixel_digest(image),
    )


def _expected_digest(source: typing.Any) -> str:
    if not isinstance(source, dict):
        return ""
    value = source.get("sha256")
    if not isinstance(value, str):
        return ""
    value = value.strip().lower()
    return value if len(value) == 64 and all(character in "0123456789abcdef" for character in value) else ""


def _check_dimensions(source: typing.Any, width: int, height: int) -> None:
    """The picture that arrived must be the picture that was described.

    Only checked when Forge said what it sent. A mismatch here means the two
    processes are looking at different files, which is a digest problem even
    though the digest itself may not have been supplied.
    """
    if not isinstance(source, dict):
        return
    for key, actual in (("width", width), ("height", height)):
        claimed = source.get(key)
        if isinstance(claimed, bool) or not isinstance(claimed, (int, float)):
            continue
        if int(claimed) != actual:
            raise compatibility.BridgeError(
                compatibility.HANDOFF_DIGEST_MISMATCH,
                f"{key} {int(claimed)} was announced, {actual} arrived",
            )
