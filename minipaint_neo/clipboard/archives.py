"""Archives: folders on the Forge host the Clipboard shows, and never writes to.

The storage folder is the Clipboard's own: it imports into it, renames and
deletes in it, and keeps an index of opaque ids over it. An archive is the
opposite on every count. It is a folder the user already has - usually
thousands of pictures in one place - that the Clipboard may show *instead of*
the storage folder, one at a time, and nothing in this extension writes to:
no file is created, renamed, deleted, touched or indexed inside it, and its
thumbnails live in the extension's own data folder. A picture from an archive
that has to become a Clipboard asset (a slot card holds only those) is
*copied* into the storage folder, byte for byte; the archive is left as it
was.

WHY NOTHING HERE READS A PICTURE TO LIST IT.

The storage folder's Refresh reads and hashes every new file, because that is
how a file renamed by hand keeps its id. Done to an archive of ten thousand
photographs that would read the whole archive off the disk just to draw its
grid. So an archive is listed from what ``scandir`` already knows - the name,
the size and the modification time - and a picture is opened only when its
thumbnail is asked for, which the browser does only for the tiles in view.

WHY A PICTURE IS NAMED BY ITS NAME.

The library names a picture by an id it minted and keeps in an index; an
archive has no index, because keeping one is writing something about the
archive and re-reading it is the cost above. A picture in an archive is named
by a *reference*, ``ar-<archive id>-<name>``, with the name in URL-safe
base64: the archive id says which folder - never its path - and the name
says which file, as a plain filename that ``resolve`` proves is a regular
file directly inside that folder before anything opens it. The same picture
has the same reference after a restart, so its thumbnail's URL - which also
carries the file's size and time - is the same URL, and the browser's copy of
it, cached as immutable, is used rather than asked for again.

WHY ITS OWN THUMBNAIL CACHE.

The library's disk cache is held to 64 MiB, a few thousand thumbnails, least
recently used out. One archive would turn it over and push the storage
folder's out with it. Archives get a directory of their own beside it, with a
budget of its own (``THUMBNAIL_DISK_BUDGET``) and the same rules: the whole
identity of what a thumbnail was made from is in its file name, so a changed
file simply does not hit, and nothing in it is ever load-bearing.
"""

from __future__ import annotations

import base64
import binascii
import collections
import hashlib
import io
import os
import pathlib
import re
import stat
import threading
import typing

from .. import scrub
from ..wangp import errors
from ..wangp.errors import IntegrationError
from . import config
from . import store as store_module

_LOG_PREFIX = "MiniPaint Clipboard:"

#: How many archives the list may hold: the settings document's own bound.
ARCHIVE_LIMIT = config.ARCHIVE_LIMIT
#: How many pictures one listing answers with. Past it the listing says how
#: many it left out rather than answering with a document nobody can draw.
LISTING_LIMIT = 100_000

#: The thumbnail's longest side: the library's own, so a tile at the size
#: slider's largest looks the same in an archive as in the storage folder.
THUMBNAIL_SIDE = store_module.THUMBNAIL_SIDE
#: What a thumbnail is encoded at. ~10-12 KiB for a photograph at 320 px,
#: which is what lets a browser cache keep thousands of them.
THUMBNAIL_QUALITY = 82
#: Made thumbnails kept in this process, for a grid scrolled back and forth.
THUMBNAIL_CACHE_SIZE = 1024
#: The disk cache, in the extension's data directory and never in an archive.
THUMBNAIL_DIR_NAME = "clipboard-archive-thumbnails"
#: What that directory may weigh before the least recently used go: about
#: eighty-five thousand thumbnails, several archives' worth.
THUMBNAIL_DISK_BUDGET = 1024 * 1024 * 1024
#: Bytes written between sweeps; a sweep is a listing and a sort.
THUMBNAIL_SWEEP_AFTER = 64 * 1024 * 1024
#: ``<archive id>-<name digest>-<mtime_ns>-<size>-<side>.<ext>``: the whole
#: identity of what the thumbnail was made from. Anything else in the
#: directory is not ours, and is neither counted nor removed.
THUMBNAIL_NAME_RE = re.compile(r"\A([0-9a-f]{16})-([0-9a-f]{40})-(\d{1,24})-(\d{1,20})-(\d{1,5})\.(webp|png)\Z")
THUMBNAIL_MIME = {"webp": "image/webp", "png": "image/png"}
#: How long a second request for a thumbnail that is being made waits for
#: the first rather than making it again.
MAKING_WAIT_SECONDS = 30.0

#: A picture in an archive: the archive's id, then its file name in URL-safe
#: base64 without padding. No colon in it anywhere, because a send request
#: is ``target:picture:stamp`` and splits on colons.
REF_PREFIX = "ar-"
REF_RE = re.compile(r"\Aar-([0-9a-f]{16})-([A-Za-z0-9_-]{2,1400})\Z")
ARCHIVE_ID_RE = re.compile(r"\A[0-9a-f]{16}\Z")


# ------------------------------------------------------------- references --


def _name_bytes(name: str) -> bytes:
    return str(name).encode("utf-8", "surrogateescape")


def key_of(name: str) -> str:
    """The name, as the second half of a reference."""
    return base64.urlsafe_b64encode(_name_bytes(name)).rstrip(b"=").decode("ascii")


def ref_for(archive_id: str, name: str) -> str:
    return f"{REF_PREFIX}{archive_id}-{key_of(name)}"


def is_ref(value: typing.Any) -> bool:
    return isinstance(value, str) and bool(REF_RE.match(value))


def parse_ref(value: typing.Any) -> typing.Optional[typing.Tuple[str, str]]:
    """``(archive id, file name)`` from a reference, or None.

    The name is only decoded here, never trusted: ``resolve`` is what proves
    it is a plain file inside the archive.
    """
    if not is_ref(value):
        return None
    match = REF_RE.match(value)
    key = match.group(2)
    try:
        raw = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))
        name = raw.decode("utf-8", "surrogateescape")
    except (binascii.Error, ValueError):
        return None
    if not name or "\x00" in name:
        return None
    return match.group(1), name


def display_name(name: str) -> str:
    """A name for the screen: one a filesystem gave us that is not valid
    UTF-8 is shown with a replacement mark rather than not at all."""
    return _name_bytes(name).decode("utf-8", "replace")


# ------------------------------------------------------------ the list ------


def _records() -> typing.List[dict]:
    """The archives as the settings document holds them, in their order."""
    return [dict(item) for item in (config.load().archives or [])]


def all_archives() -> typing.List[dict]:
    """Every archive: id, name, path, and whether it can be read now.

    The path is shown on the Archives screen, where choosing it is the point;
    it is never put in a URL or a request.
    """
    found = []
    for record in _records():
        path = pathlib.Path(record.get("path") or "")
        try:
            available = path.is_dir()
        except OSError:
            available = False
        found.append({"id": record.get("id", ""), "name": record.get("name", ""),
                      "path": str(record.get("path") or ""), "available": bool(available)})
    return found


def find(archive_id: typing.Any) -> dict:
    """The archive with this id, or ARCHIVE_UNKNOWN."""
    wanted = str(archive_id or "")
    if ARCHIVE_ID_RE.match(wanted):
        for record in _records():
            if record.get("id") == wanted:
                return record
    raise IntegrationError(errors.ARCHIVE_UNKNOWN, f"{wanted[:8]} is not an archive")


def validate_archive(text: typing.Any) -> typing.Tuple[typing.Optional[pathlib.Path], str, str]:
    """``(path, "", detail)`` for a folder that can be an archive, else ``(None, code, detail)``.

    Unlike the storage folder, nothing is created and nothing is written to
    prove it usable: an archive only has to be a folder this process can
    list. The storage folder itself is refused - it is already the
    Clipboard's own view, and as an archive it would show the same pictures
    with Rename and Delete taken away.
    """
    raw = str(text or "").strip()
    if not raw:
        return None, "ARCHIVE_EMPTY", "type a folder on the Forge host"
    try:
        path = pathlib.Path(os.path.expanduser(raw)).absolute().resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as error:
        return None, "ARCHIVE_INVALID", f"that is not a usable path ({type(error).__name__})"
    try:
        exists, is_dir = path.exists(), path.is_dir()
    except OSError as error:
        return None, "ARCHIVE_INVALID", f"that path cannot be checked ({type(error).__name__})"
    if not exists:
        return None, "ARCHIVE_MISSING", "that folder does not exist on the Forge host"
    if not is_dir:
        return None, "ARCHIVE_NOT_A_DIRECTORY", "that exists and is not a folder"
    try:
        next(iter(os.scandir(path)), None)
    except OSError as error:
        return None, "ARCHIVE_UNREADABLE", f"the folder cannot be listed ({type(error).__name__})"
    storage = store_module.store().root()
    if storage is not None:
        try:
            if os.path.normcase(str(storage.resolve())) == os.path.normcase(str(path)):
                return None, "ARCHIVE_IS_STORAGE", "that is the storage folder, which the Clipboard already shows"
        except OSError:
            pass
    return path, "", "usable"


def _name_for(path: pathlib.Path, taken: typing.Iterable[str]) -> str:
    """The folder's own name, made unique among the archives' names."""
    base = path.name or str(path)
    names = {str(name).lower() for name in taken}
    if base.lower() not in names:
        return base
    counter = 2
    while f"{base} ({counter})".lower() in names:
        counter += 1
    return f"{base} ({counter})"


def add(text: typing.Any) -> dict:
    """Add a folder to the list. Reads nothing in it but its first entry."""
    path, code, detail = validate_archive(text)
    if path is None:
        return {"ok": False, "code": code, "message": detail}
    archive_id = config.root_id_for(path)
    records = _records()
    for record in records:
        if record.get("id") == archive_id:
            return {"ok": True, "code": "", "message": f"{record.get('name')} is already an archive.",
                    "archive": public(record), "added": False}
    if len(records) >= ARCHIVE_LIMIT:
        return {"ok": False, "code": "ARCHIVE_LIMIT", "message": f"the list already holds {ARCHIVE_LIMIT} archives"}
    record = {"id": archive_id, "path": str(path), "name": _name_for(path, (r.get("name", "") for r in records))}
    records.append(record)
    config.update(archives=records)
    scrub.console(f"an archive was added ({len(records)} in the list); nothing in it is written to.", _LOG_PREFIX)
    return {"ok": True, "code": "", "message": f"Added {record['name']}.", "archive": public(record), "added": True}


def remove(archive_id: typing.Any) -> dict:
    """Take an archive off the list. The folder and its files are untouched;
    its thumbnails in the cache are left for the sweep, like any other."""
    try:
        record = find(archive_id)
    except IntegrationError as error:
        return {"ok": False, "code": error.code, "message": errors.message(error.code)}
    records = [r for r in _records() if r.get("id") != record.get("id")]
    current = config.load()
    changes: typing.Dict[str, typing.Any] = {"archives": records}
    if current.view == record.get("id"):
        changes["view"] = ""
    config.update(**changes)
    return {"ok": True, "code": "", "message": f"Removed {record.get('name')} from the list. The folder was not touched."}


def set_view(archive_id: typing.Any) -> dict:
    """Show an archive in place of the storage folder, or the storage folder
    again (an empty id). Kept on the host, like the sort and the tile size."""
    wanted = str(archive_id or "")
    if not wanted:
        config.update(view="")
        return {"ok": True, "view": "", "message": "Showing the storage folder."}
    try:
        record = find(wanted)
    except IntegrationError as error:
        return {"ok": False, "code": error.code, "message": errors.message(error.code), "view": config.load().view}
    config.update(view=record["id"])
    return {"ok": True, "view": record["id"], "message": f"Showing the archive {record['name']}. It is read-only; new pictures still go to the storage folder."}


def current_view() -> str:
    """The archive being shown, or "" for the storage folder. An id that has
    left the list reads as the storage folder."""
    wanted = config.load().view
    if not wanted:
        return ""
    try:
        return find(wanted)["id"]
    except IntegrationError:
        return ""


def public(record: typing.Mapping[str, typing.Any]) -> dict:
    return {"id": str(record.get("id") or ""), "name": str(record.get("name") or "")}


# ---------------------------------------------------------- the pictures ---


def _root_of(record: typing.Mapping[str, typing.Any]) -> pathlib.Path:
    try:
        root = pathlib.Path(str(record.get("path") or "")).resolve(strict=False)
        if root.is_dir():
            return root
    except OSError:
        pass
    raise IntegrationError(errors.ARCHIVE_UNREADABLE, "the archive folder is not there")


def _sorted(rows: typing.List[typing.Tuple[str, int, int]], mode: str) -> typing.List[typing.Tuple[str, int, int]]:
    """The storage folder's orders, from the same three facts."""
    if mode == "name_desc":
        return sorted(rows, key=lambda row: (row[0].lower(), row[0]), reverse=True)
    if mode == "oldest":
        return sorted(rows, key=lambda row: (row[2], row[0].lower()))
    if mode == "newest":
        return sorted(rows, key=lambda row: (-row[2], row[0].lower()))
    if mode == "largest":
        return sorted(rows, key=lambda row: (-row[1], row[0].lower()))
    if mode == "smallest":
        return sorted(rows, key=lambda row: (row[1], row[0].lower()))
    return sorted(rows, key=lambda row: (row[0].lower(), row[0]))


def listing(archive_id: typing.Any, sort: typing.Any = "") -> dict:
    """Every picture in an archive, in one answer, from ``scandir`` alone.

    ``items`` is ``[key, name, bytes, version]`` per picture, sorted: the key
    makes the picture's reference (``ar-<id>-<key>``), the version
    (``<mtime_ns>-<size>``) makes its thumbnail URL one the browser may keep
    for a year. Arrays rather than objects because there may be tens of
    thousands of them.

    What is not listed: hidden files, links, anything but a regular file with
    a PNG, JPEG or WebP name, an empty file, and one over the handoff ceiling
    (the storage folder's own rules); ``skipped`` counts the last two.
    Nothing is opened, so a file whose name lies about it is listed and its
    tile says it cannot be shown.
    """
    record = find(archive_id)
    root = _root_of(record)
    mode = str(sort or "") if str(sort or "") in config.SORT_MODES else config.load().sort
    rows: typing.List[typing.Tuple[str, int, int]] = []
    skipped = 0
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                name = entry.name
                if name.startswith("."):
                    continue
                if os.path.splitext(name)[1].lower() not in store_module.SUPPORTED_SUFFIXES:
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if info.st_size <= 0 or info.st_size > store_module.MAX_BYTES:
                    skipped += 1
                    continue
                rows.append((name, int(info.st_size), int(info.st_mtime_ns)))
    except OSError as error:
        raise IntegrationError(errors.ARCHIVE_UNREADABLE, f"the archive cannot be listed ({type(error).__name__})")
    ordered = _sorted(rows, mode)
    left_out = max(0, len(ordered) - LISTING_LIMIT)
    ordered = ordered[:LISTING_LIMIT]
    items = [[key_of(name), display_name(name), size, f"{mtime}-{size}"] for name, size, mtime in ordered]
    total = len(items)
    status = f"{total} picture{'s' if total != 1 else ''} in {record['name']} (read-only)."
    if left_out:
        status += f" {left_out} more were left out; an archive lists at most {LISTING_LIMIT}."
    return {"ok": True, "archive": public(record), "sort": mode, "total": total, "skipped": skipped,
            "left_out": left_out, "items": items, "status": status}


def resolve(ref: typing.Any) -> typing.Tuple[dict, str, pathlib.Path, os.stat_result]:
    """The file a reference names, proved to be a regular file directly
    inside its archive. The one place an archive and a name are joined."""
    parsed = parse_ref(ref)
    if parsed is None:
        raise IntegrationError(errors.ARCHIVE_FILE_UNKNOWN, "not an archive picture")
    archive_id, name = parsed
    record = find(archive_id)
    if "/" in name or "\\" in name or name in (".", "..") or name.startswith("."):
        raise IntegrationError(errors.ARCHIVE_FILE_OUTSIDE, "not a plain filename")
    if os.path.splitext(name)[1].lower() not in store_module.SUPPORTED_SUFFIXES:
        raise IntegrationError(errors.ARCHIVE_FILE_OUTSIDE, "not a picture this lists")
    root = _root_of(record)
    path = root / name
    try:
        info = os.lstat(path)
    except OSError:
        raise IntegrationError(errors.ARCHIVE_FILE_UNKNOWN, "no longer in the archive")
    if stat.S_ISLNK(info.st_mode):
        raise IntegrationError(errors.ARCHIVE_FILE_OUTSIDE, "a link is not an archive picture")
    if not stat.S_ISREG(info.st_mode):
        raise IntegrationError(errors.ARCHIVE_FILE_OUTSIDE, "not a regular file")
    try:
        resolved = path.resolve()
    except OSError as error:
        raise IntegrationError(errors.ARCHIVE_FILE_OUTSIDE, f"could not be resolved: {type(error).__name__}")
    if os.path.normcase(str(resolved.parent)) != os.path.normcase(str(root)):
        raise IntegrationError(errors.ARCHIVE_FILE_OUTSIDE, "resolves outside the archive")
    if info.st_size > store_module.MAX_BYTES:
        raise IntegrationError(errors.HANDOFF_TOO_LARGE, f"{info.st_size} bytes on disk")
    return record, name, resolved, info


def version_of(info: os.stat_result) -> str:
    return f"{int(info.st_mtime_ns)}-{int(info.st_size)}"


def canonical_version(ref: typing.Any) -> str:
    """What the file on disk is now, for the immutable header's decision."""
    try:
        return version_of(resolve(ref)[3])
    except IntegrationError:
        return ""


def filename_of(ref: typing.Any) -> str:
    parsed = parse_ref(ref)
    return display_name(parsed[1]) if parsed else ""


def mime_of(name: str) -> str:
    return store_module.SUPPORTED_SUFFIXES.get(os.path.splitext(name)[1].lower(), "application/octet-stream")


def read_bytes(ref: typing.Any) -> typing.Tuple[bytes, str, str]:
    """``(bytes, mime, display name)``: the file as it is, for a send that
    must keep its metadata and for the copy into the storage folder."""
    _record, name, path, _info = resolve(ref)
    try:
        data = path.read_bytes()
    except OSError as error:
        raise IntegrationError(errors.ARCHIVE_FILE_UNKNOWN, f"could not be read: {type(error).__name__}")
    if len(data) > store_module.MAX_BYTES:
        raise IntegrationError(errors.HANDOFF_TOO_LARGE, f"{len(data)} bytes")
    return data, mime_of(name), display_name(name)


def open_image(ref: typing.Any) -> typing.Any:
    """The picture as a PIL image, upright, for a destination that takes pixels."""
    from PIL import ImageOps

    from ..canvas import imaging

    _record, name, path, _info = resolve(ref)
    try:
        image = imaging.open_file(str(path))
        try:
            image = ImageOps.exif_transpose(image) or image
        except Exception:
            pass
    except Exception as error:
        raise IntegrationError(errors.HANDOFF_INVALID_IMAGE, f"did not decode: {type(error).__name__}")
    store_module._refuse_oversized(image.width, image.height)
    return image


def adopt(ref: typing.Any) -> "store_module.Asset":
    """Copy an archive picture into the storage folder, byte for byte, and
    answer with its asset - or with the asset that already holds those bytes,
    so pressing +First twice on one picture does not copy it twice. The
    archive is read and nothing else."""
    data, _mime, name = read_bytes(ref)
    library = store_module.store()
    digest = hashlib.sha256(data).hexdigest()
    for asset in library.assets():
        if asset.sha256 == digest and asset.size_bytes == len(data):
            try:
                library.resolve(asset.asset_id)
            except IntegrationError:
                continue
            return asset
    return library.import_bytes(data, name, source="archive")


# ------------------------------------------------------------ thumbnails ----


class Thumbnails:
    """Memory, then the disk cache, then Pillow - each made at most once.

    A second request for a thumbnail that is being made waits for the first
    rather than decoding the same photograph again beside it. That happens:
    the browser drops a request for a tile scrolled out of view and asks
    again when it comes back, and the server finishes what it started.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._memory: "collections.OrderedDict[tuple, typing.Tuple[bytes, str]]" = collections.OrderedDict()
        self._making: typing.Dict[tuple, threading.Event] = {}
        self._written = 0
        self._complaint = ""
        #: How many thumbnails were made with Pillow, for the checks: the one
        #: number that says whether the caches above it are doing their job.
        self.made = 0

    def forget(self) -> None:
        with self._lock:
            self._memory.clear()

    def directory(self) -> typing.Optional[pathlib.Path]:
        try:
            folder = config.config_dir() / THUMBNAIL_DIR_NAME
            folder.mkdir(parents=True, exist_ok=True)
            return folder
        except (OSError, ValueError) as error:
            self._off(f"could not be opened ({type(error).__name__})")
            return None

    def _off(self, why: str) -> None:
        if self._complaint:
            return
        self._complaint = why
        scrub.console(f"the archive thumbnail cache on disk {why}; archive thumbnails are made fresh each time.", _LOG_PREFIX)

    @staticmethod
    def _disk_name(key: tuple, extension: str) -> str:
        return f"{key[0]}-{key[1]}-{int(key[2])}-{int(key[3])}-{int(key[4])}.{extension}"

    def _disk_read(self, key: tuple) -> typing.Optional[typing.Tuple[bytes, str]]:
        folder = self.directory()
        if folder is None:
            return None
        for extension, mime in THUMBNAIL_MIME.items():
            path = folder / self._disk_name(key, extension)
            try:
                data = path.read_bytes()
            except (OSError, ValueError):
                continue
            if not data:
                continue
            try:
                os.utime(str(path), None)
            except OSError:
                pass
            return data, mime
        return None

    def _disk_write(self, key: tuple, data: bytes, mime: str) -> None:
        folder = self.directory()
        if folder is None:
            return
        extension = "png" if mime == "image/png" else "webp"
        try:
            store_module._write_bytes(folder / self._disk_name(key, extension), data)
        except (OSError, IntegrationError) as error:
            self._off(f"could not be written ({type(error).__name__})")
            return
        with self._lock:
            self._written += len(data)
            due = self._written >= THUMBNAIL_SWEEP_AFTER
            if due:
                self._written = 0
        if due:
            self.sweep()

    def sweep(self, budget: int = THUMBNAIL_DISK_BUDGET) -> dict:
        """Bring the cache back inside its budget, least recently used first."""
        folder = self.directory()
        if folder is None:
            return {"ok": False, "kept": 0, "removed": 0, "bytes": 0}
        entries = []
        total = 0
        try:
            for entry in os.scandir(folder):
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
            self._off(f"could not be listed ({type(error).__name__})")
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

    def _remember(self, key: tuple, made: typing.Tuple[bytes, str]) -> None:
        with self._lock:
            self._memory[key] = made
            self._memory.move_to_end(key)
            while len(self._memory) > THUMBNAIL_CACHE_SIZE:
                self._memory.popitem(last=False)

    def _cached(self, key: tuple) -> typing.Optional[typing.Tuple[bytes, str]]:
        with self._lock:
            found = self._memory.get(key)
            if found is not None:
                self._memory.move_to_end(key)
                return found
        stored = self._disk_read(key)
        if stored is not None:
            self._remember(key, stored)
        return stored

    def thumbnail(self, ref: typing.Any, side: int = THUMBNAIL_SIDE) -> typing.Tuple[bytes, str]:
        record, name, path, info = resolve(ref)
        key = (record["id"], hashlib.sha1(_name_bytes(name)).hexdigest(), int(info.st_mtime_ns), int(info.st_size), int(side))
        found = self._cached(key)
        if found is not None:
            return found
        # One maker per thumbnail. A second asker waits for the first and
        # then reads what it left; if the first failed, it makes its own.
        with self._lock:
            waiting = self._making.get(key)
            if waiting is None:
                mine = threading.Event()
                self._making[key] = mine
        if waiting is not None:
            waiting.wait(MAKING_WAIT_SECONDS)
            found = self._cached(key)
            if found is not None:
                return found
            return self._make(key, path, side, None)
        return self._make(key, path, side, mine)

    def _make(self, key: tuple, path: pathlib.Path, side: int, mine: typing.Optional[threading.Event]) -> typing.Tuple[bytes, str]:
        from PIL import Image, ImageOps

        try:
            try:
                with Image.open(str(path)) as opened:
                    if getattr(opened, "is_animated", False) and getattr(opened, "n_frames", 1) > 1:
                        raise IntegrationError(errors.HANDOFF_INVALID_IMAGE, "animated")
                    store_module._refuse_oversized(int(opened.size[0]), int(opened.size[1]))
                    # A JPEG decoded at a fraction of its size: a 24 MP
                    # photograph read at an eighth is most of the cost gone,
                    # and the result is still at least the size asked for.
                    try:
                        opened.draft("RGB", (max(16, int(side)), max(16, int(side))))
                    except Exception:
                        pass
                    opened.load()
                    upright = ImageOps.exif_transpose(opened) or opened
                    alpha = upright.mode in ("RGBA", "LA", "PA") or (upright.mode == "P" and "transparency" in upright.info)
                    small = upright.convert("RGBA" if alpha else "RGB")
                    small.thumbnail((max(16, int(side)), max(16, int(side))))
            except IntegrationError:
                raise
            except Exception as error:
                raise IntegrationError(errors.HANDOFF_INVALID_IMAGE, f"did not decode: {type(error).__name__}")
            buffer = io.BytesIO()
            try:
                small.save(buffer, format="WEBP", quality=THUMBNAIL_QUALITY, method=0)
                made = (buffer.getvalue(), "image/webp")
            except Exception:
                buffer = io.BytesIO()
                small.save(buffer, format="PNG")
                made = (buffer.getvalue(), "image/png")
            with self._lock:
                self.made += 1
            self._remember(key, made)
            self._disk_write(key, made[0], made[1])
            return made
        finally:
            if mine is not None:
                with self._lock:
                    if self._making.get(key) is mine:
                        del self._making[key]
                mine.set()


_thumbnails: typing.Optional[Thumbnails] = None
_thumbnails_lock = threading.Lock()


def thumbnails() -> Thumbnails:
    global _thumbnails
    with _thumbnails_lock:
        if _thumbnails is None:
            _thumbnails = Thumbnails()
        return _thumbnails


def reset_for_tests() -> None:
    global _thumbnails
    with _thumbnails_lock:
        _thumbnails = None


__all__ = [
    "ARCHIVE_LIMIT",
    "LISTING_LIMIT",
    "REF_PREFIX",
    "THUMBNAIL_DIR_NAME",
    "THUMBNAIL_DISK_BUDGET",
    "THUMBNAIL_SIDE",
    "Thumbnails",
    "add",
    "adopt",
    "all_archives",
    "canonical_version",
    "current_view",
    "display_name",
    "filename_of",
    "find",
    "is_ref",
    "key_of",
    "listing",
    "open_image",
    "parse_ref",
    "public",
    "read_bytes",
    "ref_for",
    "remove",
    "reset_for_tests",
    "resolve",
    "set_view",
    "thumbnails",
    "validate_archive",
    "version_of",
]
