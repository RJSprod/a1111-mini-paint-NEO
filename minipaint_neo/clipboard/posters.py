"""A poster for every output: one frame, made once and kept three times.

View Outputs' gallery is a grid of every video in WanGP's output folder, and
asked for: "The gallery should work much like the clipboard archives and load
as I slide into video." An archive's tile is a thumbnail the server made once
and the browser keeps for a year, asked for only when it is in view. A video
has no thumbnail to read, so this makes one: the frame at one second (the
middle of a clip shorter than two), at most ``POSTER_SIDE`` on its longest
side, as a WebP of a few tens of kilobytes.

WHY ON THE SERVER. The filmstrip used to ask the browser for its posters: a
``<video preload="metadata" src="…#t=1">`` per tile, which is a range request
for the file's header and enough of it to paint one frame, and a decoder, for
every tile - nothing kept, all of it again on the next opening. That was fine
for a strip of a dozen; for a grid of a whole folder it is twenty decoders and
twenty multi-megabyte reads on every screen, against the six connections a page
has to this origin, while the one video that is meant to play waits behind
them. Made here once, a poster is one small image the browser keeps.

HOW. With PyAV, which Forge Neo installs (``av`` in its requirements): the file
is opened, the nearest keyframe before the wanted time is sought, and frames
are decoded up to it - a few frames, never the clip - and scaled by the
decoder's own scaler. OpenCV (also Forge Neo's) is the fallback. With neither,
the route says so (``OUTPUT_POSTER_UNAVAILABLE``) and the page draws its
posters the old way. A picture output is made smaller with Pillow.

WHAT IS KEPT. In this process's memory (``POSTER_CACHE_SIZE``), on disk in this
extension's data folder under a budget of its own (``POSTER_DISK_BUDGET``, the
least recently used out), and - by the URL that carries the file's version - in
the browser for a year. The whole identity of what a poster was made from is
in its file name (the output's id, the file's time and size, the side), so a
file that changed simply does not hit, and nothing in the folder is ever
load-bearing. The name also carries the clip's length, which the gallery shows
on the tile, so a poster read from disk says it without opening the video.

Nothing here ever writes beside a video, moves one or names a path to the
browser: the route takes the output's opaque id, as the file route does.
"""

from __future__ import annotations

import collections
import io
import os
import pathlib
import re
import threading
import time
import typing

from .. import scrub
from ..wangp import errors
from ..wangp.errors import IntegrationError
from . import config
from . import store as store_module

_LOG_PREFIX = "MiniPaint Clipboard:"

#: The poster's longest side. Larger than a library thumbnail's 320, because a
#: video gallery is looked at a few to a row and a tile can be wide; still a
#: WebP of twenty or thirty kilobytes.
POSTER_SIDE = 480
#: What a poster is encoded at.
POSTER_QUALITY = 80
#: Where in a clip its poster is taken: past a first frame that is often a
#: fade or the source picture itself, and never past the middle of a short one.
POSTER_AT_SECONDS = 1.0
#: How many frames past the keyframe are decoded looking for that moment. A
#: generated clip has a keyframe every second or two; a file with none is not
#: decoded to its end for one picture.
POSTER_DECODE_LIMIT = 240
#: Made posters kept in this process, for a gallery scrolled back and forth.
POSTER_CACHE_SIZE = 1024
#: The disk cache, in the extension's data directory and never in WanGP's.
POSTER_DIR_NAME = "clipboard-output-posters"
#: What that directory may weigh before the least recently used go: about
#: twenty thousand posters, a whole output folder's worth.
POSTER_DISK_BUDGET = 512 * 1024 * 1024
#: Bytes written between sweeps; a sweep is a listing and a sort.
POSTER_SWEEP_AFTER = 32 * 1024 * 1024
#: ``<output id>-<mtime_ns>-<size>-<side>-<duration ms>.<ext>``. Anything else
#: in the directory is not ours, and is neither counted nor removed.
POSTER_NAME_RE = re.compile(r"\A([0-9a-z]{8,40})-(\d{1,24})-(\d{1,20})-(\d{1,5})-(\d{1,10})\.(webp|png)\Z")
POSTER_MIME = {"webp": "image/webp", "png": "image/png"}
#: How long a second request for a poster being made waits for the first.
MAKING_WAIT_SECONDS = 30.0
#: Posters made at once. Each is a decoder; a fling across the gallery that
#: started forty of them would take every core from the video being watched.
MAKING_AT_ONCE = 3
#: How long a file that would not decode is not tried again. A video WanGP is
#: still writing has no index yet; once it is finished it has a new size and
#: so a new version, which is tried at once.
FAILED_SECONDS = 600.0

VIDEO_SUFFIXES = (".mp4", ".webm", ".mkv", ".mov", ".m4v")


def version_of(info: os.stat_result) -> str:
    """The version an output's URLs carry: ``<mtime in ms>-<size>``.

    Milliseconds rather than nanoseconds because the folder listing reads a
    file's time as a float (``outputs._media_in``), and both sides must
    arrive at the same string from the same stat.
    """
    return f"{int(round(float(info.st_mtime) * 1000))}-{int(info.st_size)}"


def decoder() -> str:
    """Which video decoder this process has: "pyav", "opencv" or ""."""
    found = _decoder.get("name")
    if found is not None:
        return found
    name = ""
    try:
        import av  # noqa: F401

        name = "pyav"
    except Exception:
        try:
            import cv2  # noqa: F401

            name = "opencv"
        except Exception:
            name = ""
    _decoder["name"] = name
    return name


_decoder: typing.Dict[str, typing.Optional[str]] = {"name": None}


def _fit(width: int, height: int, side: int) -> typing.Tuple[int, int]:
    """The size a frame is scaled to: at most ``side`` on its longest side,
    never larger than it is, and even on both, which every scaler takes."""
    width = max(1, int(width))
    height = max(1, int(height))
    scale = min(1.0, float(side) / float(max(width, height)))
    fitted_w = max(2, int(round(width * scale / 2.0)) * 2)
    fitted_h = max(2, int(round(height * scale / 2.0)) * 2)
    return fitted_w, fitted_h


def _moment(duration: typing.Optional[float]) -> float:
    if duration and duration > 0:
        return min(POSTER_AT_SECONDS, duration / 2.0)
    return POSTER_AT_SECONDS


def _frame_pyav(path: pathlib.Path, side: int) -> typing.Tuple[typing.Any, float]:
    """The poster frame and the clip's length in seconds (0 when unknown)."""
    import av

    with av.open(str(path)) as container:
        stream = next((one for one in container.streams if one.type == "video"), None)
        if stream is None:
            raise IntegrationError(errors.OUTPUT_POSTER_UNREADABLE, "no video stream")
        try:
            stream.thread_type = "AUTO"
        except Exception:
            pass
        duration = 0.0
        try:
            if stream.duration and stream.time_base:
                duration = float(stream.duration * stream.time_base)
            elif container.duration:
                duration = float(container.duration) / float(av.time_base)
        except Exception:
            duration = 0.0
        target = _moment(duration)
        if target > 0 and stream.time_base:
            try:
                container.seek(int(target / float(stream.time_base)), stream=stream, backward=True)
            except Exception:
                try:
                    container.seek(0)
                except Exception:
                    pass
        chosen = None
        for count, frame in enumerate(container.decode(stream)):
            chosen = frame
            when = frame.time
            if when is None or when >= target - 0.001 or count >= POSTER_DECODE_LIMIT:
                break
        if chosen is None:
            raise IntegrationError(errors.OUTPUT_POSTER_UNREADABLE, "no frame decoded")
        width, height = _fit(chosen.width, chosen.height, side)
        image = chosen.reformat(width=width, height=height, format="rgb24").to_image()
        return image, duration


def _frame_opencv(path: pathlib.Path, side: int) -> typing.Tuple[typing.Any, float]:
    import cv2
    from PIL import Image

    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise IntegrationError(errors.OUTPUT_POSTER_UNREADABLE, "not opened")
        rate = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        frames = float(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
        duration = frames / rate if rate > 0 and frames > 0 else 0.0
        target = _moment(duration)
        if target > 0:
            capture.set(cv2.CAP_PROP_POS_MSEC, target * 1000.0)
        ok, frame = capture.read()
        if not ok and target > 0:
            capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = capture.read()
        if not ok or frame is None:
            raise IntegrationError(errors.OUTPUT_POSTER_UNREADABLE, "no frame decoded")
        image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        image.thumbnail(_fit(image.width, image.height, side))
        return image, duration
    finally:
        try:
            capture.release()
        except Exception:
            pass


def _picture(path: pathlib.Path, side: int) -> typing.Any:
    from PIL import Image, ImageOps

    with Image.open(str(path)) as opened:
        store_module._refuse_oversized(int(opened.size[0]), int(opened.size[1]))
        try:
            opened.draft("RGB", (side, side))
        except Exception:
            pass
        opened.load()
        upright = ImageOps.exif_transpose(opened) or opened
        alpha = upright.mode in ("RGBA", "LA", "PA") or (upright.mode == "P" and "transparency" in upright.info)
        small = upright.convert("RGBA" if alpha else "RGB")
        small.thumbnail((side, side))
        return small


class Posters:
    """Memory, then the disk cache, then a decoder - each poster made once.

    A second request for a poster that is being made waits for the first
    rather than decoding the same video again beside it: the gallery drops
    a request for a tile scrolled away and asks again when it comes back,
    and the server finishes what it started.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._memory: "collections.OrderedDict[tuple, typing.Tuple[bytes, str, int]]" = collections.OrderedDict()
        self._making: typing.Dict[tuple, threading.Event] = {}
        self._failed: typing.Dict[tuple, typing.Tuple[float, str]] = {}
        self._gate = threading.BoundedSemaphore(MAKING_AT_ONCE)
        #: key -> the file name on disk, read from the folder once.
        self._disk: typing.Optional[typing.Dict[tuple, str]] = None
        self._written = 0
        self._complaint = ""
        #: How many posters were decoded, for the checks: the one number that
        #: says whether the caches above it are doing their job.
        self.made = 0

    # -- the disk -------------------------------------------------------

    def directory(self) -> typing.Optional[pathlib.Path]:
        try:
            folder = config.config_dir() / POSTER_DIR_NAME
            folder.mkdir(parents=True, exist_ok=True)
            return folder
        except (OSError, ValueError) as error:
            self._off(f"could not be opened ({type(error).__name__})")
            return None

    def _off(self, why: str) -> None:
        if self._complaint:
            return
        self._complaint = why
        scrub.console(f"the output poster cache on disk {why}; posters are made fresh each time.", _LOG_PREFIX)

    @staticmethod
    def _disk_name(key: tuple, duration_ms: int, extension: str) -> str:
        return f"{key[0]}-{int(key[1])}-{int(key[2])}-{int(key[3])}-{max(0, int(duration_ms))}.{extension}"

    def _index(self) -> typing.Dict[tuple, str]:
        """Every poster on disk by what it was made from. Read once; kept in
        step by this class's own writes and sweeps."""
        with self._lock:
            if self._disk is not None:
                return self._disk
        found: typing.Dict[tuple, str] = {}
        folder = self.directory()
        if folder is not None:
            try:
                for entry in os.scandir(folder):
                    match = POSTER_NAME_RE.match(entry.name)
                    if match:
                        found[(match.group(1), int(match.group(2)), int(match.group(3)), int(match.group(4)))] = entry.name
            except OSError as error:
                self._off(f"could not be listed ({type(error).__name__})")
        with self._lock:
            if self._disk is None:
                self._disk = found
            return self._disk

    def _disk_read(self, key: tuple) -> typing.Optional[typing.Tuple[bytes, str, int]]:
        name = self._index().get(key)
        folder = self.directory() if name else None
        if not name or folder is None:
            return None
        match = POSTER_NAME_RE.match(name)
        path = folder / name
        try:
            data = path.read_bytes()
        except (OSError, ValueError):
            with self._lock:
                if self._disk is not None:
                    self._disk.pop(key, None)
            return None
        if not data or not match:
            return None
        try:
            os.utime(str(path), None)
        except OSError:
            pass
        return data, POSTER_MIME[match.group(6)], int(match.group(5))

    def _disk_write(self, key: tuple, made: typing.Tuple[bytes, str, int]) -> None:
        folder = self.directory()
        if folder is None:
            return
        data, mime, duration_ms = made
        name = self._disk_name(key, duration_ms, "png" if mime == "image/png" else "webp")
        try:
            store_module._write_bytes(folder / name, data)
        except (OSError, IntegrationError) as error:
            self._off(f"could not be written ({type(error).__name__})")
            return
        with self._lock:
            index = self._index()
            index[key] = name
            self._written += len(data)
            due = self._written >= POSTER_SWEEP_AFTER
            if due:
                self._written = 0
        if due:
            self.sweep()

    def sweep(self, budget: int = POSTER_DISK_BUDGET) -> dict:
        """Bring the cache back inside its budget, least recently used first."""
        folder = self.directory()
        if folder is None:
            return {"ok": False, "kept": 0, "removed": 0, "bytes": 0}
        entries = []
        total = 0
        try:
            for entry in os.scandir(folder):
                if not POSTER_NAME_RE.match(entry.name):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                entries.append((float(info.st_mtime), int(info.st_size), entry.path, entry.name))
                total += int(info.st_size)
        except OSError as error:
            self._off(f"could not be listed ({type(error).__name__})")
            return {"ok": False, "kept": 0, "removed": 0, "bytes": 0}
        removed = 0
        gone = []
        if total > max(0, int(budget)):
            entries.sort()
            for _when, size, path, name in entries:
                if total <= max(0, int(budget)):
                    break
                try:
                    os.unlink(path)
                except OSError:
                    continue
                total -= size
                removed += 1
                gone.append(name)
        if gone:
            with self._lock:
                if self._disk is not None:
                    names = set(gone)
                    for key in [key for key, name in self._disk.items() if name in names]:
                        del self._disk[key]
        return {"ok": True, "kept": len(entries) - removed, "removed": removed, "bytes": total}

    # -- memory ---------------------------------------------------------

    def _remember(self, key: tuple, made: typing.Tuple[bytes, str, int]) -> None:
        with self._lock:
            self._memory[key] = made
            self._memory.move_to_end(key)
            while len(self._memory) > POSTER_CACHE_SIZE:
                self._memory.popitem(last=False)

    def _cached(self, key: tuple) -> typing.Optional[typing.Tuple[bytes, str, int]]:
        with self._lock:
            found = self._memory.get(key)
            if found is not None:
                self._memory.move_to_end(key)
                return found
        stored = self._disk_read(key)
        if stored is not None:
            self._remember(key, stored)
        return stored

    def forget(self) -> None:
        with self._lock:
            self._memory.clear()
            self._failed.clear()
            self._disk = None

    # -- the one way in ---------------------------------------------------

    def poster(self, file_id: str, path: pathlib.Path, info: os.stat_result,
               side: int = POSTER_SIDE) -> typing.Tuple[bytes, str, float]:
        """``(bytes, mime, duration in seconds)`` for one output's poster.

        Raises ``IntegrationError``: ``OUTPUT_POSTER_UNAVAILABLE`` when this
        process has no video decoder at all, ``OUTPUT_POSTER_UNREADABLE``
        when this file did not give a frame.
        """
        side = max(32, min(1024, int(side)))
        key = (str(file_id).lower(), int(info.st_mtime_ns), int(info.st_size), side)
        found = self._cached(key)
        if found is not None:
            return found[0], found[1], found[2] / 1000.0
        with self._lock:
            failed = self._failed.get(key)
            if failed and time.monotonic() - failed[0] < FAILED_SECONDS:
                raise IntegrationError(errors.OUTPUT_POSTER_UNREADABLE, failed[1])
            waiting = self._making.get(key)
            if waiting is None:
                mine = threading.Event()
                self._making[key] = mine
        if waiting is not None:
            waiting.wait(MAKING_WAIT_SECONDS)
            found = self._cached(key)
            if found is not None:
                return found[0], found[1], found[2] / 1000.0
            return self._make(key, path, side, None)
        return self._make(key, path, side, mine)

    def _make(self, key: tuple, path: pathlib.Path, side: int,
              mine: typing.Optional[threading.Event]) -> typing.Tuple[bytes, str, float]:
        try:
            video = path.name.lower().endswith(VIDEO_SUFFIXES)
            if video and not decoder():
                raise IntegrationError(errors.OUTPUT_POSTER_UNAVAILABLE, "no decoder")
            with self._gate:
                try:
                    if video:
                        maker = _frame_pyav if decoder() == "pyav" else _frame_opencv
                        image, duration = maker(path, side)
                    else:
                        image, duration = _picture(path, side), 0.0
                except IntegrationError as error:
                    if error.code == errors.OUTPUT_POSTER_UNAVAILABLE:
                        raise
                    raise IntegrationError(errors.OUTPUT_POSTER_UNREADABLE, error.detail or error.code)
                except Exception as error:
                    raise IntegrationError(errors.OUTPUT_POSTER_UNREADABLE, f"did not decode: {type(error).__name__}")
            buffer = io.BytesIO()
            try:
                image.save(buffer, format="WEBP", quality=POSTER_QUALITY, method=0)
                encoded, mime = buffer.getvalue(), "image/webp"
            except Exception:
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                encoded, mime = buffer.getvalue(), "image/png"
            made = (encoded, mime, int(round(max(0.0, float(duration or 0.0)) * 1000)))
            with self._lock:
                self.made += 1
            self._remember(key, made)
            self._disk_write(key, made)
            return made[0], made[1], made[2] / 1000.0
        except IntegrationError as error:
            if error.code == errors.OUTPUT_POSTER_UNREADABLE:
                with self._lock:
                    self._failed[key] = (time.monotonic(), error.detail or "")
                    if len(self._failed) > 4096:
                        self._failed.pop(next(iter(self._failed)))
            raise
        finally:
            if mine is not None:
                with self._lock:
                    if self._making.get(key) is mine:
                        del self._making[key]
                mine.set()


_posters: typing.Optional[Posters] = None
_posters_lock = threading.Lock()


def posters() -> Posters:
    global _posters
    with _posters_lock:
        if _posters is None:
            _posters = Posters()
        return _posters


def reset_for_tests(name: typing.Optional[str] = None) -> None:
    """A fresh cache, and the decoder asked again - or, with ``name``, told:
    "" is a process with no video decoder at all."""
    global _posters
    with _posters_lock:
        _posters = None
    _decoder["name"] = name


__all__ = [
    "POSTER_AT_SECONDS",
    "POSTER_DIR_NAME",
    "POSTER_DISK_BUDGET",
    "POSTER_SIDE",
    "Posters",
    "decoder",
    "posters",
    "reset_for_tests",
    "version_of",
]
