"""What Clipboard remembers between restarts, and where it keeps it.

Four documents, each its own file under the extension's data directory -
the same ``<data root>/a1111-mini-paint-NEO/`` the WanGP setup lives in - and
none of them inside the user's chosen image folder, which stays an ordinary
directory of pictures:

* ``clipboard.json``            the storage root, the intercept switch, the
                                sort order and the thumbnail size;
* ``clipboard-index.json``      the asset index: opaque ids for the files of
                                every root Clipboard has known;
* ``clipboard-draft.json``      the composer: a prompt override and up to
                                three asset ids;
* ``clipboard-history.json``    the recipes that were confirmed queued.

Every write is atomic - a temp file in the same directory, then a rename -
and every read is forgiving: a document that will not parse is moved aside
with a timestamp, one line says so, and the tab loads with defaults rather
than not at all. The storage root is the one value here that is a path, and
it is a path on the machine running Forge; it is shown back to the user in
the folder dialog, because choosing it is the feature, and it is never
carried by any protocol.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import os
import pathlib
import typing

from .. import scrub
from ..wangp import config as wangp_config

SCHEMA_VERSION = 1

CONFIG_NAME = "clipboard.json"
INDEX_NAME = "clipboard-index.json"
DRAFT_NAME = "clipboard-draft.json"
HISTORY_NAME = "clipboard-history.json"

#: The sort orders the browser offers, and how the menu names them.
SORT_MODES = ("name_asc", "name_desc", "newest", "oldest", "largest", "smallest")
SORT_LABELS = {
    "name_asc": "Name A-Z",
    "name_desc": "Name Z-A",
    "newest": "Newest",
    "oldest": "Oldest",
    "largest": "Largest",
    "smallest": "Smallest",
}
DEFAULT_SORT = "newest"

#: Thumbnail size is presentation: a CSS variable the browser sizes the grid
#: by, remembered here so a reload keeps it.
THUMBNAIL_MIN = 72
THUMBNAIL_MAX = 320
THUMBNAIL_DEFAULT = 144

#: Existing installs keep today's behaviour until they opt in.
DEFAULT_INTERCEPT = False

_LOG_PREFIX = "MiniPaint Clipboard:"

#: The directory override, for tests. None means the extension's own data dir.
_state: typing.Dict[str, typing.Any] = {"dir": None}


# ---------------------------------------------------------------- location --


def use_config_dir(directory: typing.Optional[typing.Any]) -> None:
    """Point every path helper at ``directory``; None restores the host's.

    A seam for tests, so nothing writes into a real data root. Nothing in the
    extension calls it.
    """
    _state["dir"] = pathlib.Path(str(directory)) if directory is not None else None


def config_dir() -> pathlib.Path:
    directory = _state.get("dir")
    if directory is None:
        directory = wangp_config.config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def path_of(name: str) -> pathlib.Path:
    return config_dir() / name


# --------------------------------------------------------------- documents --


def _stamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def quarantine(path: pathlib.Path, why: str) -> None:
    """Move a document that will not parse out of the way, and say so once.

    Moved rather than deleted: the file may be somebody's history, and a
    broken one can still be read by hand. The name says when and nothing
    else; the console line names the document, never the folder.
    """
    try:
        aside = path.with_name(f"{path.name}.broken-{_stamp()}")
        os.replace(str(path), str(aside))
        scrub.console(f"{path.name} could not be read ({why}) and was moved aside; Clipboard starts with defaults.", _LOG_PREFIX)
    except OSError:
        scrub.console(f"{path.name} could not be read ({why}); Clipboard starts with defaults.", _LOG_PREFIX)


def read_document(name: str, default: typing.Any) -> typing.Any:
    """One JSON document, or ``default`` when it is absent or unreadable."""
    path = path_of(name)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    except OSError as error:
        scrub.console(f"{name} could not be read ({error}); Clipboard starts with defaults.", _LOG_PREFIX)
        return default
    try:
        data = json.loads(text)
    except ValueError as error:
        quarantine(path, f"not JSON: {error}")
        return default
    if type(data) is not type(default):
        quarantine(path, f"expected {type(default).__name__}, found {type(data).__name__}")
        return default
    return data


def write_document(name: str, payload: typing.Any) -> None:
    """Write one document so a reader sees the old one or the new one."""
    wangp_config.atomic_write(path_of(name), json.dumps(payload, indent=2, sort_keys=True) + "\n")


# ------------------------------------------------------------------ config --


def root_id_for(path: typing.Any) -> str:
    """A stable, opaque id for a storage root: a digest of its resolved form.

    Kept in every asset record so an asset knows which root it belongs to
    without carrying the path, and so that a root chosen again later is
    recognised as the same library.
    """
    text = os.path.normcase(str(path or ""))
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def clamp_thumbnail(value: typing.Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return THUMBNAIL_DEFAULT
    return max(THUMBNAIL_MIN, min(THUMBNAIL_MAX, number))


@dataclasses.dataclass
class Config:
    """The whole of what a restart may know about Clipboard."""

    schema_version: int = SCHEMA_VERSION
    storage_root: str = ""
    root_id: str = ""
    intercept: bool = DEFAULT_INTERCEPT
    sort: str = DEFAULT_SORT
    thumbnail: int = THUMBNAIL_DEFAULT

    def as_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "storage_root": str(self.storage_root or ""),
            "root_id": str(self.root_id or ""),
            "intercept": bool(self.intercept),
            "sort": self.sort if self.sort in SORT_MODES else DEFAULT_SORT,
            "thumbnail": clamp_thumbnail(self.thumbnail),
        }

    @classmethod
    def from_dict(cls, data: typing.Any) -> "Config":
        data = data if isinstance(data, dict) else {}
        root = data.get("storage_root")
        root = str(root) if isinstance(root, str) else ""
        root_id = data.get("root_id")
        root_id = root_id if isinstance(root_id, str) and root_id else (root_id_for(root) if root else "")
        sort = data.get("sort")
        return cls(
            schema_version=SCHEMA_VERSION,
            storage_root=root,
            root_id=root_id,
            intercept=bool(data.get("intercept", DEFAULT_INTERCEPT)),
            sort=sort if sort in SORT_MODES else DEFAULT_SORT,
            thumbnail=clamp_thumbnail(data.get("thumbnail", THUMBNAIL_DEFAULT)),
        )

    @property
    def configured(self) -> bool:
        return bool(self.storage_root)


def load() -> Config:
    """The config, always. Never raises; a bad file is quarantined."""
    data = read_document(CONFIG_NAME, {})
    version = data.get("schema_version") if isinstance(data, dict) else None
    if isinstance(version, int) and not isinstance(version, bool) and version > SCHEMA_VERSION:
        quarantine(path_of(CONFIG_NAME), f"schema {version} is newer than {SCHEMA_VERSION}")
        return Config()
    return Config.from_dict(data)


def save(config: Config) -> None:
    write_document(CONFIG_NAME, config.as_dict())


def update(**changes: typing.Any) -> Config:
    """Change some fields and write the rest back as they were."""
    config = load()
    for key, value in changes.items():
        if hasattr(config, key):
            setattr(config, key, value)
    config = Config.from_dict(config.as_dict())
    save(config)
    return config


__all__ = [
    "CONFIG_NAME",
    "Config",
    "DEFAULT_INTERCEPT",
    "DEFAULT_SORT",
    "DRAFT_NAME",
    "HISTORY_NAME",
    "INDEX_NAME",
    "SORT_LABELS",
    "SORT_MODES",
    "THUMBNAIL_DEFAULT",
    "THUMBNAIL_MAX",
    "THUMBNAIL_MIN",
    "clamp_thumbnail",
    "config_dir",
    "load",
    "path_of",
    "read_document",
    "root_id_for",
    "save",
    "update",
    "use_config_dir",
    "write_document",
]
