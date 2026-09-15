"""The Clipboard library: one folder on the Forge host, and the opaque ids
that name the pictures in it.

The folder is the user's. It is an ordinary directory of PNG, JPEG and WebP
files that they may add to, rename and delete with anything they like, and
Clipboard notices on Refresh. What Clipboard adds is an index: every file
gets a 32-hex asset id that stays with it across renames made here and, when
size and digest still match, across renames made elsewhere. Everything else
in the extension - the composer's slots, the history, the queue request -
refers to a picture by that id and never by its name or its path.

The path is the whole security posture of this module, and it is a posture
because every other design in which a path travels is a design in which,
one day, a different path travels. Only this module combines the configured
root with a relative name, and it does so in one place, ``resolve``, which
proves the result is a regular file directly inside the resolved root before
anything opens, serves, renames or deletes it. A symlink is refused whatever
it points at. A name with a separator in it is refused before it becomes a
path. The public queue protocol never accepts a path at all.

Files are preserved on import when they are already a valid supported image,
so a Forge-generated PNG keeps its generation metadata; pixels that only
exist in memory - a paste, a Canvas render - are encoded losslessly to PNG.
A collision keeps the requested name and adds " (2)" before the extension;
nothing is ever overwritten because something else had the same name.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime
import hashlib
import io
import os
import pathlib
import re
import secrets
import stat
import tempfile
import threading
import typing

from .. import events, scrub
from ..wangp import protocol
from ..wangp.errors import (
    CLIPBOARD_ASSET_OUTSIDE_ROOT,
    CLIPBOARD_ASSET_UNKNOWN,
    CLIPBOARD_NOT_CONFIGURED,
    HANDOFF_INVALID_IMAGE,
    HANDOFF_TOO_LARGE,
    REQUEST_INVALID,
    IntegrationError,
)
from . import config

#: The v1 library formats, by extension, and what each is served as.
SUPPORTED_SUFFIXES: typing.Dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
#: What Pillow calls each of them, for the check that a file is what its
#: name says.
FORMAT_FOR_SUFFIX = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}
SUFFIX_FOR_FORMAT = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}

#: Files Clipboard writes while importing; never indexed, never served.
TEMP_PREFIX = ".minipaint-"

#: Where a picture came from. Kept in the record for the browser's tooltip
#: and for nothing else.
SOURCES = ("upload", "paste", "forge_gallery", "minipaint", "external", "slot")

#: Ceilings for one library file, the handoff's own: what cannot be handed to
#: WanGP has no business being a Clipboard asset either.
MAX_BYTES = protocol.MAX_HANDOFF_BYTES
MAX_PIXELS = protocol.MAX_HANDOFF_PIXELS
MAX_SIDE = protocol.MAX_HANDOFF_SIDE

#: How long a filename may be, and what it may not contain.
MAX_NAME_LENGTH = 120
_FORBIDDEN_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"} | {f"com{n}" for n in range(1, 10)} | {f"lpt{n}" for n in range(1, 10)}
)
_COLLISION_SUFFIX = re.compile(r"\A(.*) \((\d+)\)\Z")

#: Thumbnails are made on demand and kept in memory, keyed by the file's
#: identity: a rename keeps them, an external edit replaces them.
THUMBNAIL_SIDE = 320
#: How many made thumbnails the process keeps. One is ~12 KiB, so this is
#: about 6 MiB - a rounding error beside a model, and enough to cover
#: several pages of the grid so paging back and forth stays instant.
THUMBNAIL_CACHE_SIZE = 512

#: The disk cache, in the extension's own data directory - never in the
#: user's picture folder, which is theirs and should not acquire files it
#: did not ask for. Without this, every Forge restart re-encodes at ~46 ms
#: each whatever the user looks at.
THUMBNAIL_DIR_NAME = "clipboard-thumbnails"
#: What that directory is allowed to weigh, in bytes, before the least
#: recently used entries go. A cache that grows for ever is a bug with a
#: long fuse; at ~12 KiB an entry this is some thousands of pictures.
THUMBNAIL_DISK_BUDGET = 64 * 1024 * 1024
#: How many bytes may be written between sweeps. The sweep is a directory
#: listing and a sort, so it is not free and does not want to run per write.
THUMBNAIL_SWEEP_AFTER = THUMBNAIL_DISK_BUDGET // 8
#: A cache file names the whole identity its contents were made from, so a
#: file that changed simply does not hit and there is no invalidation logic
#: to get wrong. Anything else in that directory is not ours and is left.
THUMBNAIL_NAME_RE = re.compile(r"\A([0-9a-f]{32})-(\d{1,24})-(\d{1,20})-(\d{1,5})\.(webp|png)\Z")
#: The mime each cached extension carries back.
THUMBNAIL_MIME = {"webp": "image/webp", "png": "image/png"}

_LOG_PREFIX = "MiniPaint Clipboard:"


@dataclasses.dataclass
class Asset:
    """One picture in the library, by an id that outlives its name."""

    asset_id: str
    root_id: str
    relative_path: str
    filename: str
    size_bytes: int = 0
    width: int = 0
    height: int = 0
    mime: str = "image/png"
    sha256: str = ""
    mtime_ns: int = 0
    imported_at: str = ""
    source: str = "external"

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: typing.Any) -> typing.Optional["Asset"]:
        if not isinstance(data, dict):
            return None
        asset_id = data.get("asset_id")
        relative = data.get("relative_path")
        if not protocol.valid_handoff_id(asset_id) or not isinstance(relative, str) or not relative:
            return None
        try:
            return cls(
                asset_id=asset_id,
                root_id=str(data.get("root_id") or ""),
                relative_path=relative,
                filename=str(data.get("filename") or relative),
                size_bytes=int(data.get("size_bytes") or 0),
                width=int(data.get("width") or 0),
                height=int(data.get("height") or 0),
                mime=str(data.get("mime") or "image/png"),
                sha256=str(data.get("sha256") or ""),
                mtime_ns=int(data.get("mtime_ns") or 0),
                imported_at=str(data.get("imported_at") or ""),
                source=str(data.get("source") or "external"),
            )
        except (TypeError, ValueError):
            return None

    def public(self) -> dict:
        """What the browser is told about an asset: no path, only the name."""
        return {
            "asset_id": self.asset_id,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "width": self.width,
            "height": self.height,
            "mime": self.mime,
            "mtime_ns": self.mtime_ns,
            "source": self.source,
        }


# ---------------------------------------------------------------- helpers --


def new_asset_id() -> str:
    return secrets.token_hex(16)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def version_of(asset: typing.Any) -> str:
    """The version of a picture's bytes: ``<mtime_ns>-<size_bytes>``.

    The same cheap identity the thumbnail caches are keyed by, so a URL
    carrying it can be blessed immutable and a file that changed simply gets
    a different one. The asset id is already in the path, so these two file
    facts complete the key.
    """
    try:
        return f"{int(getattr(asset, 'mtime_ns', 0) or 0)}-{int(getattr(asset, 'size_bytes', 0) or 0)}"
    except (TypeError, ValueError):
        return ""


def _same_directory(left: pathlib.Path, right: pathlib.Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def safe_basename(name: typing.Any, default_suffix: str = ".png") -> str:
    """A filename the library will accept, or a refusal.

    The last component only - a separator anywhere is refused rather than
    stripped, because a name that arrived with one was not a name - then
    the characters no filesystem takes, hidden and temporary prefixes, the
    reserved device names, and a bound on length. An extension outside the
    supported set gets the default.
    """
    text = str(name or "").strip()
    if not text or text in (".", ".."):
        raise IntegrationError(REQUEST_INVALID, "a filename is required")
    if "/" in text or "\\" in text:
        raise IntegrationError(REQUEST_INVALID, "a filename may not contain a separator")
    if _FORBIDDEN_CHARACTERS.search(text):
        raise IntegrationError(REQUEST_INVALID, "the filename contains characters no filesystem takes")
    if text.startswith(".") or text.lower().startswith(TEMP_PREFIX):
        raise IntegrationError(REQUEST_INVALID, "a filename may not start with a dot")
    stem, suffix = os.path.splitext(text)
    suffix = suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        stem, suffix = text, default_suffix
    stem = stem.rstrip(". ")
    if not stem:
        raise IntegrationError(REQUEST_INVALID, "a filename needs a name before its extension")
    if stem.lower() in _RESERVED_NAMES:
        raise IntegrationError(REQUEST_INVALID, "that name is reserved by the operating system")
    if len(stem) + len(suffix) > MAX_NAME_LENGTH:
        stem = stem[: MAX_NAME_LENGTH - len(suffix)].rstrip(". ") or "image"
    return stem + suffix


def unique_name(root: pathlib.Path, basename: str) -> str:
    """``basename``, or ``basename (2)`` and so on until nothing is in the way."""
    stem, suffix = os.path.splitext(basename)
    match = _COLLISION_SUFFIX.match(stem)
    base_stem, start = (match.group(1), int(match.group(2)) + 1) if match else (stem, 2)
    candidate = basename
    counter = start
    while (root / candidate).exists() or (root / candidate).is_symlink():
        candidate = f"{base_stem} ({counter}){suffix}"
        counter += 1
        if counter > 10000:
            raise IntegrationError(REQUEST_INVALID, "no free name for that file")
    return candidate


def _refuse_oversized(width: int, height: int, size_bytes: int = 0) -> None:
    if width < 1 or height < 1:
        raise IntegrationError(HANDOFF_INVALID_IMAGE, f"degenerate size {width}x{height}")
    if width > MAX_SIDE or height > MAX_SIDE:
        raise IntegrationError(HANDOFF_TOO_LARGE, f"{width}x{height} exceeds the {MAX_SIDE} pixel side limit")
    if width * height > MAX_PIXELS:
        raise IntegrationError(HANDOFF_TOO_LARGE, f"{width}x{height} is over the {MAX_PIXELS} pixel limit")
    if size_bytes > MAX_BYTES:
        raise IntegrationError(HANDOFF_TOO_LARGE, f"{size_bytes} bytes, over the {MAX_BYTES} limit")


def inspect_bytes(data: bytes) -> typing.Tuple[str, int, int]:
    """``(format, width, height)`` of a supported, static image, or a refusal.

    The header only: Pillow reads the size before it allocates pixels, so an
    image far over the ceiling is refused before it costs anything.
    """
    from PIL import Image

    if not data:
        raise IntegrationError(HANDOFF_INVALID_IMAGE, "no bytes")
    if len(data) > MAX_BYTES:
        raise IntegrationError(HANDOFF_TOO_LARGE, f"{len(data)} bytes, over the {MAX_BYTES} limit")
    try:
        with Image.open(io.BytesIO(data)) as opened:
            fmt = (opened.format or "").upper()
            if fmt not in SUFFIX_FOR_FORMAT:
                raise IntegrationError(HANDOFF_INVALID_IMAGE, f"format {opened.format!r} is not PNG, JPEG or WebP")
            if getattr(opened, "is_animated", False) and getattr(opened, "n_frames", 1) > 1:
                raise IntegrationError(HANDOFF_INVALID_IMAGE, "animated images are not library files in v1")
            width, height = int(opened.size[0]), int(opened.size[1])
    except IntegrationError:
        raise
    except Exception as error:
        raise IntegrationError(HANDOFF_INVALID_IMAGE, f"the bytes did not decode: {type(error).__name__}")
    _refuse_oversized(width, height, len(data))
    return fmt, width, height


def _write_bytes(path: pathlib.Path, data: bytes) -> None:
    """All of the file or none of it, under a temporary name Refresh ignores."""
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=TEMP_PREFIX, suffix=".part")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            try:
                os.fsync(stream.fileno())
            except (OSError, AttributeError, ValueError):
                pass
        os.replace(temp_name, str(path))
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _stat(path: pathlib.Path) -> typing.Optional[os.stat_result]:
    try:
        return os.lstat(path)
    except OSError:
        return None


def _signature(assets: typing.Iterable[Asset]) -> typing.Set[tuple]:
    """What the grid would draw, as facts: the id, the name, and the bytes.

    Everything the index route answers with comes out of these, so two reads
    with the same signature are two reads a page cannot tell apart - which
    is the definition of a refresh that found no difference.
    """
    return {(asset.asset_id, asset.relative_path, int(asset.mtime_ns), int(asset.size_bytes)) for asset in assets}


def sort_assets(assets: typing.Iterable[Asset], mode: str) -> typing.List[Asset]:
    """The browser's order. Names compare case-insensitively; ties keep the id order."""
    items = list(assets)
    if mode == "name_desc":
        return sorted(items, key=lambda a: (a.filename.lower(), a.asset_id), reverse=True)
    if mode == "oldest":
        return sorted(items, key=lambda a: (a.mtime_ns, a.asset_id))
    if mode == "largest":
        return sorted(items, key=lambda a: (-a.size_bytes, a.filename.lower()))
    if mode == "smallest":
        return sorted(items, key=lambda a: (a.size_bytes, a.filename.lower()))
    if mode == "newest":
        return sorted(items, key=lambda a: (-a.mtime_ns, a.asset_id))
    return sorted(items, key=lambda a: (a.filename.lower(), a.asset_id))


# ---------------------------------------------------------------- the root --


def validate_root(text: typing.Any, create: bool = False) -> typing.Tuple[typing.Optional[pathlib.Path], str, str]:
    """``(path, "", detail)`` for a usable storage folder, else ``(None, code, detail)``.

    The path is the operator's, on the machine running Forge: expanded for
    ``~``, made absolute, resolved. An existing non-directory is refused; a
    missing one is created only when asked; and a directory Clipboard cannot
    list, or cannot write a small file into, is refused with the reason. The
    detail may name the path - this is the one screen where it is the point.
    """
    raw = str(text or "").strip()
    if not raw:
        return None, "ROOT_EMPTY", "type the folder on the Forge host that Clipboard should keep its images in"
    try:
        path = pathlib.Path(os.path.expanduser(raw)).absolute()
        path = path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as error:
        return None, "ROOT_INVALID", f"that is not a usable path ({type(error).__name__})"
    if path.exists() and not path.is_dir():
        return None, "ROOT_NOT_A_DIRECTORY", "that exists and is not a folder"
    if not path.exists():
        if not create:
            return None, "ROOT_MISSING", "that folder does not exist yet; press Create it to make it"
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            return None, "ROOT_UNWRITABLE", f"the folder could not be created ({type(error).__name__})"
    try:
        next(iter(os.scandir(path)), None)
    except OSError as error:
        return None, "ROOT_UNREADABLE", f"the folder cannot be listed ({type(error).__name__})"
    try:
        handle, temp_name = tempfile.mkstemp(dir=str(path), prefix=TEMP_PREFIX, suffix=".probe")
        os.close(handle)
        os.unlink(temp_name)
    except OSError as error:
        return None, "ROOT_UNWRITABLE", f"the folder is not writable ({type(error).__name__})"
    return path, "", "usable"


# --------------------------------------------------------------- the store --


class Store:
    """The library: the configured root, its index, and every file operation.

    One instance for the process (``store()``); the class takes nothing so a
    test can make its own against a temporary config directory. Every method
    that touches a file goes through ``resolve``.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._index: typing.Optional[typing.Dict[str, typing.Dict[str, Asset]]] = None
        self._thumbnails: "collections.OrderedDict[tuple, typing.Tuple[bytes, str]]" = collections.OrderedDict()
        #: The most recent import, so the tab can select it when it next
        #: refreshes: the asset id and when. In memory only.
        self.last_import: typing.Optional[typing.Tuple[str, float]] = None
        #: Files that were read once and turned out not to be pictures this
        #: library can hold, keyed by (name, size, mtime). A folder with a
        #: large ``.png`` that is not a PNG - a truncated download, something
        #: another tool wrote - would otherwise be read in full on every
        #: press of Refresh, forever, and never become anything. Keyed on the
        #: metadata so a file that is *replaced* is read again; in memory,
        #: bounded, and never a reason to hide a file that has changed.
        self._rejected: "collections.OrderedDict[tuple, bool]" = collections.OrderedDict()
        #: How many bytes have gone into the disk cache since it was last
        #: swept, and the one complaint it is allowed to make. See
        #: ``sweep_thumbnails`` and ``_disk_off``.
        self._disk_written = 0
        self._disk_complaint = ""
        #: The library's own generation. It advances when the set of
        #: pictures or their order could have changed and at no other time -
        #: which is the whole reason it is not ``events.revision()``, a
        #: counter that job, enhancement, runtime and WanGP events move
        #: constantly while this library sits still.
        self._revision = 0

    # -- the library's identity --------------------------------------------

    def revision(self) -> str:
        """``<epoch>:<library revision>`` - what a page holds its grid against.

        The epoch is the event spine's, minted once per process, so a value
        from a previous Forge is not stale but meaningless and says so. The
        number is this library's own, so a page only re-asks when the
        library has actually moved.
        """
        with self._lock:
            return f"{events.epoch()}:{self._revision}"

    def moved(self, total: typing.Optional[int] = None) -> str:
        """The library changed: take the next revision and say so once.

        Advisory, per the spine's own first rule: the event carries the new
        revision and the new total and nothing else. A page that sees one
        re-asks for the page it is showing - 9 KiB for sixty items - and
        patches from the answer, so there are no deltas to get wrong and a
        missed event cannot leave a page quietly incorrect.
        """
        with self._lock:
            self._revision += 1
            revision = f"{events.epoch()}:{self._revision}"
            if total is None:
                try:
                    total = len(self._records())
                except IntegrationError:
                    total = 0
        try:
            events.publish(events.LIBRARY, {"revision": revision, "total": int(total)})
        except Exception:  # pragma: no cover - a notice is never load-bearing
            pass
        return revision

    # -- the index --------------------------------------------------------

    def _load_index(self) -> typing.Dict[str, typing.Dict[str, Asset]]:
        if self._index is None:
            document = config.read_document(config.INDEX_NAME, {})
            roots = document.get("roots") if isinstance(document, dict) else None
            index: typing.Dict[str, typing.Dict[str, Asset]] = {}
            for root_id, records in (roots or {}).items() if isinstance(roots, dict) else []:
                if not isinstance(records, dict):
                    continue
                kept = {}
                for asset_id, data in records.items():
                    asset = Asset.from_dict(data)
                    if asset is not None and asset.asset_id == asset_id:
                        kept[asset_id] = asset
                index[str(root_id)] = kept
            self._index = index
        return self._index

    def _save_index(self) -> None:
        index = self._load_index()
        config.write_document(
            config.INDEX_NAME,
            {"schema_version": config.SCHEMA_VERSION,
             "roots": {root_id: {asset_id: asset.as_dict() for asset_id, asset in records.items()} for root_id, records in index.items()}},
        )

    def forget(self) -> None:
        """Drop the in-memory index and thumbnails; the next call re-reads."""
        with self._lock:
            self._index = None
            self._thumbnails.clear()

    # -- the root ---------------------------------------------------------

    def root(self) -> typing.Optional[pathlib.Path]:
        """The configured storage folder, resolved, or None when there is none."""
        current = config.load()
        if not current.configured:
            return None
        try:
            return pathlib.Path(current.storage_root).resolve(strict=False)
        except (OSError, RuntimeError):
            return None

    def root_id(self) -> str:
        current = config.load()
        return current.root_id if current.configured else ""

    def configured(self) -> bool:
        return self.root() is not None

    def set_root(self, text: typing.Any, create: bool = False) -> dict:
        """Choose the storage folder. Changes nothing about the old one.

        Files are never moved; history keeps its records and marks the ones
        from another root unavailable; the draft's slots are cleared where
        the asset cannot be resolved in the new library (the tab does that
        on its next refresh, through ``get``).
        """
        path, code, detail = validate_root(text, create=create)
        if path is None:
            return {"ok": False, "code": code, "message": detail}
        with self._lock:
            config.update(storage_root=str(path), root_id=config.root_id_for(path))
            self._thumbnails.clear()
        scrub.console("the storage folder was changed; the library is re-read from it.", _LOG_PREFIX)
        # A different folder is a different library: every page showing the
        # old one is holding a revision that no longer describes anything.
        self.moved()
        return {"ok": True, "code": "", "message": "usable", "path": str(path), "root_id": config.root_id_for(path)}

    def _require_root(self) -> typing.Tuple[pathlib.Path, str]:
        root = self.root()
        if root is None:
            raise IntegrationError(CLIPBOARD_NOT_CONFIGURED, "no storage folder has been chosen")
        return root, self.root_id()

    # -- lookup and containment -------------------------------------------

    def _records(self) -> typing.Dict[str, Asset]:
        _root, root_id = self._require_root()
        return self._load_index().setdefault(root_id, {})

    def get(self, asset_id: typing.Any) -> typing.Optional[Asset]:
        """The record for an id in the current root, or None. Never raises."""
        if not protocol.valid_handoff_id(asset_id):
            return None
        try:
            with self._lock:
                return self._records().get(asset_id)
        except IntegrationError:
            return None

    def assets(self, sort: str = config.DEFAULT_SORT) -> typing.List[Asset]:
        try:
            with self._lock:
                return sort_assets(self._records().values(), sort)
        except IntegrationError:
            return []

    def resolve(self, asset_id: typing.Any) -> typing.Tuple[Asset, pathlib.Path]:
        """The file an id names, proved to be a regular file inside the root.

        The one place root and name are joined. A name with a separator, a
        symlink, anything but a plain file, or a resolved path whose parent
        is not the resolved root is refused with its own code - and the id
        of a file that has gone is CLIPBOARD_ASSET_UNKNOWN, which the tab
        answers with Refresh.
        """
        if not protocol.valid_handoff_id(asset_id):
            raise IntegrationError(CLIPBOARD_ASSET_UNKNOWN, "an asset id is 32 lowercase hex characters")
        root, _root_id = self._require_root()
        with self._lock:
            asset = self._records().get(asset_id)
        if asset is None:
            raise IntegrationError(CLIPBOARD_ASSET_UNKNOWN, f"{asset_id[:8]} is not in the library")
        name = asset.relative_path
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise IntegrationError(CLIPBOARD_ASSET_OUTSIDE_ROOT, "the indexed name is not a plain filename")
        path = root / name
        info = _stat(path)
        if info is None:
            raise IntegrationError(CLIPBOARD_ASSET_UNKNOWN, f"{asset_id[:8]} is no longer in the folder")
        if stat.S_ISLNK(info.st_mode):
            raise IntegrationError(CLIPBOARD_ASSET_OUTSIDE_ROOT, "a link is not a library file")
        if not stat.S_ISREG(info.st_mode):
            raise IntegrationError(CLIPBOARD_ASSET_OUTSIDE_ROOT, "not a regular file")
        try:
            resolved = path.resolve()
            resolved_root = root.resolve()
        except OSError as error:
            raise IntegrationError(CLIPBOARD_ASSET_OUTSIDE_ROOT, f"could not be resolved: {type(error).__name__}")
        if not _same_directory(resolved.parent, resolved_root):
            raise IntegrationError(CLIPBOARD_ASSET_OUTSIDE_ROOT, "resolves outside the storage folder")
        return asset, resolved

    def open_image(self, asset_id: typing.Any) -> typing.Any:
        """The picture behind an id, as an RGBA PIL image. Never a path."""
        from ..canvas import imaging

        asset, path = self.resolve(asset_id)
        info = _stat(path)
        if info is not None and info.st_size > MAX_BYTES:
            raise IntegrationError(HANDOFF_TOO_LARGE, f"{info.st_size} bytes on disk")
        try:
            image = imaging.open_file(str(path))
        except Exception as error:
            raise IntegrationError(HANDOFF_INVALID_IMAGE, f"{asset.asset_id[:8]} did not decode: {type(error).__name__}")
        _refuse_oversized(image.width, image.height)
        return image

    def read_bytes(self, asset_id: typing.Any) -> typing.Tuple[bytes, str]:
        """The file's own bytes and mime, for serving and for byte-preserving copies."""
        asset, path = self.resolve(asset_id)
        try:
            data = path.read_bytes()
        except OSError as error:
            raise IntegrationError(CLIPBOARD_ASSET_UNKNOWN, f"could not be read: {type(error).__name__}")
        if len(data) > MAX_BYTES:
            raise IntegrationError(HANDOFF_TOO_LARGE, f"{len(data)} bytes")
        return data, SUPPORTED_SUFFIXES.get(path.suffix.lower(), asset.mime)

    # -- refresh ----------------------------------------------------------

    def refresh(self) -> typing.List[Asset]:
        """Read the folder again: top-level files only, ids kept by name.

        A file that is still where the index says it was keeps its id; one
        that moved elsewhere in the folder with the same size and digest
        recovers it; one that is new gets a fresh id; one that is gone leaves
        the index, and history shows it as missing. Hidden files, temporary
        files, links and anything not a supported still image are ignored.
        """
        root, root_id = self._require_root()
        with self._lock:
            records = self._load_index().setdefault(root_id, {})
            # What the library looked like before this read, by the three
            # facts that decide what the grid shows and in what order. A
            # refresh that finds none of them moved publishes nothing: an
            # event nobody can act on is a page re-fetching for no reason.
            before = _signature(records.values())
            by_name = {asset.relative_path: asset for asset in records.values()}
            seen: typing.Dict[str, Asset] = {}
            unclaimed: typing.List[Asset] = []
            try:
                entries = sorted(os.scandir(root), key=lambda entry: entry.name)
            except OSError as error:
                raise IntegrationError(CLIPBOARD_NOT_CONFIGURED, f"the storage folder cannot be listed ({type(error).__name__})")
            for entry in entries:
                name = entry.name
                suffix = os.path.splitext(name)[1].lower()
                if name.startswith(".") or suffix not in SUPPORTED_SUFFIXES:
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if info.st_size > MAX_BYTES:
                    continue
                existing = by_name.get(name)
                if existing is not None and existing.mtime_ns == info.st_mtime_ns and existing.size_bytes == info.st_size:
                    # Metadata proves it unchanged: no read, no hash. This is
                    # the whole cost of a refresh on a folder of large stills.
                    seen[existing.asset_id] = existing
                    continue
                stamp = (name, int(info.st_size), int(info.st_mtime_ns))
                if stamp in self._rejected:
                    continue
                facts = self._inspect_file(pathlib.Path(entry.path), info)
                if facts is None:
                    self._remember_rejected(stamp)
                    continue
                if existing is not None:
                    updated = dataclasses.replace(existing, **facts)
                    seen[updated.asset_id] = updated
                else:
                    unclaimed.append(Asset(asset_id="", root_id=root_id, relative_path=name, filename=name, imported_at=_now_iso(), source="external", **facts))
            # Files that left their indexed name: an external rename recovers
            # the id when the bytes are the same bytes.
            gone = {asset.asset_id: asset for asset in records.values() if asset.asset_id not in seen}
            for fresh in unclaimed:
                match = next((old for old in gone.values() if old.size_bytes == fresh.size_bytes and old.sha256 and old.sha256 == fresh.sha256), None)
                if match is not None:
                    gone.pop(match.asset_id, None)
                    recovered = dataclasses.replace(fresh, asset_id=match.asset_id, imported_at=match.imported_at, source=match.source)
                    seen[recovered.asset_id] = recovered
                else:
                    created = dataclasses.replace(fresh, asset_id=new_asset_id())
                    seen[created.asset_id] = created
            self._load_index()[root_id] = seen
            self._save_index()
            changed = _signature(seen.values()) != before
            listed = sort_assets(seen.values(), config.load().sort)
        if changed:
            self.moved(len(listed))
        return listed

    #: How many rejections are remembered. Generous for a folder somebody
    #: keeps working files in, finite because this is memory.
    MAX_REJECTED = 256

    def _remember_rejected(self, stamp: tuple) -> None:
        self._rejected[stamp] = True
        self._rejected.move_to_end(stamp)
        while len(self._rejected) > self.MAX_REJECTED:
            self._rejected.popitem(last=False)

    def _inspect_file(self, path: pathlib.Path, info: os.stat_result) -> typing.Optional[dict]:
        """Size, digest and dimensions of one file, or None when it is not an image."""
        try:
            data = path.read_bytes()
            fmt, width, height = inspect_bytes(data)
        except (OSError, IntegrationError):
            return None
        if FORMAT_FOR_SUFFIX.get(path.suffix.lower()) != fmt:
            return None
        return {
            "size_bytes": int(info.st_size),
            "width": width,
            "height": height,
            "mime": SUPPORTED_SUFFIXES[path.suffix.lower()],
            "sha256": hashlib.sha256(data).hexdigest(),
            "mtime_ns": int(info.st_mtime_ns),
        }

    # -- import -----------------------------------------------------------

    def _register(self, root: pathlib.Path, root_id: str, name: str, data: bytes, fmt: str, width: int, height: int, source: str) -> Asset:
        path = root / name
        _write_bytes(path, data)
        info = _stat(path)
        asset = Asset(
            asset_id=new_asset_id(),
            root_id=root_id,
            relative_path=name,
            filename=name,
            size_bytes=len(data),
            width=width,
            height=height,
            mime=SUPPORTED_SUFFIXES[os.path.splitext(name)[1].lower()],
            sha256=hashlib.sha256(data).hexdigest(),
            mtime_ns=int(info.st_mtime_ns) if info is not None else 0,
            imported_at=_now_iso(),
            source=source if source in SOURCES else "external",
        )
        self._records()[asset.asset_id] = asset
        self._save_index()
        self.last_import = (asset.asset_id, __import__("time").monotonic())
        scrub.console(f"imported one {fmt} ({width}x{height}) from {asset.source}.", _LOG_PREFIX)
        self.moved()
        return asset

    def import_bytes(self, data: bytes, filename: typing.Any = "", source: str = "upload") -> Asset:
        """A file's own bytes into the library, unchanged, under its own name.

        The bytes must already be a supported still image whose format
        matches the name's extension - a JPEG called ``.png`` is refused, not
        renamed - so a Forge PNG arrives with every chunk it left with.
        """
        fmt, width, height = inspect_bytes(data)
        wanted = safe_basename(filename or f"image{SUFFIX_FOR_FORMAT[fmt]}", default_suffix=SUFFIX_FOR_FORMAT[fmt])
        suffix = os.path.splitext(wanted)[1].lower()
        if FORMAT_FOR_SUFFIX.get(suffix) != fmt:
            wanted = os.path.splitext(wanted)[0] + SUFFIX_FOR_FORMAT[fmt]
        with self._lock:
            root, root_id = self._require_root()
            name = unique_name(root, wanted)
            return self._register(root, root_id, name, bytes(data), fmt, width, height, source)

    def import_image(self, image: typing.Any, basename: typing.Any = "", source: str = "paste") -> Asset:
        """Pixels that exist only in memory, encoded losslessly to PNG."""
        from ..canvas import imaging

        try:
            width, height = int(image.size[0]), int(image.size[1])
        except Exception as error:
            raise IntegrationError(HANDOFF_INVALID_IMAGE, f"not a usable image: {type(error).__name__}")
        _refuse_oversized(width, height)
        data = imaging.to_png_bytes(imaging.to_rgba(image))
        if len(data) > MAX_BYTES:
            raise IntegrationError(HANDOFF_TOO_LARGE, f"{len(data)} bytes of PNG")
        stem = os.path.splitext(str(basename or "").strip())[0] if basename else ""
        wanted = safe_basename((stem or f"{source}-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}") + ".png")
        with self._lock:
            root, root_id = self._require_root()
            name = unique_name(root, wanted)
            return self._register(root, root_id, name, data, "PNG", width, height, source)

    # -- rename and delete ------------------------------------------------

    def rename(self, asset_id: typing.Any, new_name: typing.Any, allow_suffix: bool = False) -> Asset:
        """A new basename for an asset; the id and the bytes stay.

        The extension is kept when the new name has none or a different one:
        the content is what it was, and its name must not lie about it. A
        collision is refused unless the caller asked for a generated suffix.
        """
        with self._lock:
            asset, path = self.resolve(asset_id)
            root = path.parent
            wanted = safe_basename(new_name, default_suffix=path.suffix.lower())
            if os.path.splitext(wanted)[1].lower() != path.suffix.lower():
                wanted = os.path.splitext(wanted)[0] + path.suffix.lower()
            if wanted == asset.relative_path:
                return asset
            target = root / wanted
            if target.exists() or target.is_symlink():
                if not allow_suffix:
                    raise IntegrationError(REQUEST_INVALID, "a file with that name is already there")
                wanted = unique_name(root, wanted)
                target = root / wanted
            try:
                os.rename(str(path), str(target))
            except OSError as error:
                raise IntegrationError(REQUEST_INVALID, f"the file could not be renamed ({type(error).__name__})")
            info = _stat(target)
            renamed = dataclasses.replace(asset, relative_path=wanted, filename=wanted, mtime_ns=int(info.st_mtime_ns) if info else asset.mtime_ns)
            self._records()[asset.asset_id] = renamed
            self._save_index()
        # A rename changes a caption and an order, never a picture's bytes:
        # the URL is unchanged, so nothing is re-fetched for it.
        self.moved()
        return renamed

    def delete(self, asset_id: typing.Any) -> Asset:
        """Remove one library file. History keeps its record and says Missing."""
        with self._lock:
            asset, path = self.resolve(asset_id)
            try:
                os.unlink(str(path))
            except OSError as error:
                raise IntegrationError(REQUEST_INVALID, f"the file could not be deleted ({type(error).__name__})")
            self._records().pop(asset.asset_id, None)
            self._save_index()
            self._thumbnails = collections.OrderedDict(
                (key, value) for key, value in self._thumbnails.items() if key[0] != asset.asset_id
            )
        self._forget_disk_thumbnails(asset.asset_id)
        self.moved()
        return asset

    # -- thumbnails -------------------------------------------------------

    def canonical_version(self, asset_id: typing.Any) -> str:
        """What ``version_of`` would say about the file on disk right now.

        Read from the file rather than from the index, because the index is
        what a refresh brings up to date and the header this decides - a
        thumbnail blessed immutable for a year - must never be granted to a
        version the file does not actually have. Empty when the id names
        nothing servable, which is a request that gets the careful header.
        """
        try:
            _asset, path = self.resolve(asset_id)
        except IntegrationError:
            return ""
        info = _stat(path)
        return f"{int(info.st_mtime_ns)}-{int(info.st_size)}" if info is not None else ""

    def thumbnail_dir(self) -> typing.Optional[pathlib.Path]:
        """The disk cache's directory, made if it is not there yet.

        None when it cannot be had at all. Nothing here is load-bearing: an
        unreadable or unwritable cache is a slower tab, never a broken one.
        """
        try:
            directory = config.config_dir() / THUMBNAIL_DIR_NAME
            directory.mkdir(parents=True, exist_ok=True)
            return directory
        except OSError as error:
            self._disk_off(f"could not be opened ({type(error).__name__})")
            return None

    def _disk_off(self, why: str) -> None:
        """Say once that the disk cache is not working, and stop trying to.

        Once, because a folder that cannot be written will not be writable on
        the next thumbnail either, and a line per tile is a log nobody reads.
        """
        if self._disk_complaint:
            return
        self._disk_complaint = why
        scrub.console(f"the thumbnail cache on disk {why}; thumbnails are made fresh each time.", _LOG_PREFIX)

    @staticmethod
    def _disk_name(key: tuple, extension: str) -> str:
        """``<asset id>-<mtime_ns>-<size_bytes>-<side>.webp``.

        The whole file identity is in the name, which is why there is no
        invalidation logic anywhere in this module: a file that changed
        produces a different name and simply does not hit.
        """
        return f"{key[0]}-{int(key[1])}-{int(key[2])}-{int(key[3])}.{extension}"

    def _disk_read(self, key: tuple) -> typing.Optional[typing.Tuple[bytes, str]]:
        directory = self.thumbnail_dir()
        if directory is None:
            return None
        for extension, mime in THUMBNAIL_MIME.items():
            path = directory / self._disk_name(key, extension)
            try:
                data = path.read_bytes()
            except (OSError, ValueError):
                continue
            if not data:
                continue
            # Touched so least-recently-used means what it says. atime is off
            # on most filesystems worth having; mtime is the one fact a sweep
            # can rely on, so a hit re-stamps it.
            try:
                os.utime(str(path), None)
            except OSError:
                pass
            return data, mime
        return None

    def _disk_write(self, key: tuple, data: bytes, mime: str) -> None:
        directory = self.thumbnail_dir()
        if directory is None:
            return
        extension = "png" if mime == "image/png" else "webp"
        path = directory / self._disk_name(key, extension)
        try:
            _write_bytes(path, data)
        except (OSError, IntegrationError) as error:
            self._disk_off(f"could not be written ({type(error).__name__})")
            return
        with self._lock:
            self._disk_written += len(data)
            due = self._disk_written >= THUMBNAIL_SWEEP_AFTER
            if due:
                self._disk_written = 0
        if due:
            self.sweep_thumbnails()

    def sweep_thumbnails(self, budget: int = THUMBNAIL_DISK_BUDGET) -> dict:
        """Bring the disk cache back inside its budget, oldest use first.

        Run at startup and occasionally after writes rather than on every
        one: it is a directory listing and a sort, and the budget is a
        ceiling rather than a line the cache has to sit exactly on.

        Files in that directory that are not ours - anything whose name is
        not the identity a thumbnail is stored under - are counted by nobody
        and removed by nobody.
        """
        directory = self.thumbnail_dir()
        if directory is None:
            return {"ok": False, "kept": 0, "removed": 0, "bytes": 0}
        entries = []
        total = 0
        try:
            for entry in os.scandir(directory):
                if not THUMBNAIL_NAME_RE.match(entry.name):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                entries.append((float(info.st_mtime), int(info.st_size), entry.path))
                total += int(info.st_size)
        except OSError as error:
            self._disk_off(f"could not be listed ({type(error).__name__})")
            return {"ok": False, "kept": 0, "removed": 0, "bytes": 0}
        removed = 0
        if total > max(0, int(budget)):
            entries.sort()
            for _when, size, path in entries:
                if total <= max(0, int(budget)):
                    break
                try:
                    os.unlink(path)
                except OSError:
                    continue
                total -= size
                removed += 1
        return {"ok": True, "kept": len(entries) - removed, "removed": removed, "bytes": total}

    def _forget_disk_thumbnails(self, asset_id: str) -> None:
        """Drop one picture's cached copies. Tidiness, never correctness:
        the name carries the identity, so a stale entry could not be hit."""
        directory = self.thumbnail_dir()
        if directory is None:
            return
        try:
            for entry in os.scandir(directory):
                match = THUMBNAIL_NAME_RE.match(entry.name)
                if match is not None and match.group(1) == asset_id:
                    try:
                        os.unlink(entry.path)
                    except OSError:
                        continue
        except OSError:
            return

    def thumbnail(self, asset_id: typing.Any, side: int = THUMBNAIL_SIDE) -> typing.Tuple[bytes, str]:
        """A small copy for the browser, made once per file identity.

        Three places are asked in the order they cost anything: this
        process's memory, the extension's own data directory, and - only
        then - Pillow, which is ~46 ms of decoding and encoding for a
        photograph and some hundreds of milliseconds for a phone picture.
        """
        from PIL import Image

        asset, path = self.resolve(asset_id)
        info = _stat(path)
        key = (asset.asset_id, int(info.st_mtime_ns) if info else 0, int(info.st_size) if info else 0, int(side))
        with self._lock:
            cached = self._thumbnails.get(key)
            if cached is not None:
                self._thumbnails.move_to_end(key)
                return cached
        stored = self._disk_read(key)
        if stored is not None:
            self._remember_thumbnail(key, stored)
            return stored
        try:
            with Image.open(str(path)) as opened:
                if getattr(opened, "is_animated", False) and getattr(opened, "n_frames", 1) > 1:
                    raise IntegrationError(HANDOFF_INVALID_IMAGE, "animated")
                _refuse_oversized(int(opened.size[0]), int(opened.size[1]))
                opened.load()
                small = opened.convert("RGBA")
                small.thumbnail((max(16, int(side)), max(16, int(side))))
        except IntegrationError:
            raise
        except Exception as error:
            raise IntegrationError(HANDOFF_INVALID_IMAGE, f"did not decode: {type(error).__name__}")
        buffer = io.BytesIO()
        try:
            small.save(buffer, format="WEBP", quality=82, method=0)
            made = (buffer.getvalue(), "image/webp")
        except Exception:
            buffer = io.BytesIO()
            small.save(buffer, format="PNG")
            made = (buffer.getvalue(), "image/png")
        self._remember_thumbnail(key, made)
        self._disk_write(key, made[0], made[1])
        return made

    def _remember_thumbnail(self, key: tuple, made: typing.Tuple[bytes, str]) -> None:
        with self._lock:
            self._thumbnails[key] = made
            self._thumbnails.move_to_end(key)
            while len(self._thumbnails) > THUMBNAIL_CACHE_SIZE:
                self._thumbnails.popitem(last=False)


_store: typing.Optional[Store] = None
_store_lock = threading.Lock()


def store() -> Store:
    """The process's library. One object; the root it reads is the config's."""
    global _store
    with _store_lock:
        if _store is None:
            _store = Store()
        return _store


def reset_for_tests() -> None:
    global _store
    with _store_lock:
        _store = None


def open_image(asset_id: typing.Any) -> typing.Any:
    """Module-level entry the public API resolves a Clipboard asset through."""
    return store().open_image(asset_id)


__all__ = [
    "Asset",
    "MAX_BYTES",
    "THUMBNAIL_DISK_BUDGET",
    "THUMBNAIL_DIR_NAME",
    "version_of",
    "SOURCES",
    "SUPPORTED_SUFFIXES",
    "Store",
    "inspect_bytes",
    "new_asset_id",
    "open_image",
    "reset_for_tests",
    "safe_basename",
    "sort_assets",
    "store",
    "unique_name",
    "validate_root",
]
