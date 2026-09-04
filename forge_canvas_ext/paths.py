"""Where the extension keeps things, and how the browser addresses them."""

from __future__ import annotations

import os.path
import pathlib
import time
import typing
import urllib.parse

try:
    root_path = pathlib.Path(__file__).resolve().parents[1]
except NameError:  # pragma: no cover - only when exec'd without a __file__
    import inspect

    root_path = pathlib.Path(inspect.getfile(lambda: None)).resolve().parents[1]


def get_asset_url(
    file_path: pathlib.Path, append: typing.Optional[dict] = None
) -> str:
    """A ``/file=`` URL for a file inside the extension, cache-busted by mtime."""
    if append is None:
        append = {"v": str(os.path.getmtime(file_path))}
    else:
        append = dict(append)
        append["v"] = str(os.path.getmtime(file_path))
    return f"/file={file_path.absolute()}?{urllib.parse.urlencode(append)}"


def write_config_file() -> pathlib.Path:
    """The miniPaint config the legacy iframe reads. It has to exist to be stat'd."""
    config_dir = root_path / "downloads"
    config_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    config_path = config_dir / "config.json"
    if not config_path.exists():
        config_path.write_text("{}", encoding="utf-8")
    return config_path


TMP_DIR = root_path / "tmp"

# How long a handoff file is worth keeping. The browser fetches it within
# seconds of it being written; anything older is a send that is over.
TMP_MAX_AGE_SECONDS = 15 * 60
TMP_MAX_FILES = 24


def tmp_dir() -> pathlib.Path:
    TMP_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    return TMP_DIR


def prune_tmp(keep: typing.Iterable[pathlib.Path] = ()) -> None:
    """Drop handoff files that no browser is going to ask for any more.

    Best effort on purpose: a file that cannot be removed (Windows still has
    it open, say) must never be the reason a transfer fails.
    """
    keep_set = {path.resolve() for path in keep}
    try:
        entries = sorted(
            (entry for entry in TMP_DIR.glob("send-*") if entry.is_file()),
            key=lambda entry: entry.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return

    now = time.time()
    for index, entry in enumerate(entries):
        if entry.resolve() in keep_set:
            continue
        try:
            too_old = now - entry.stat().st_mtime > TMP_MAX_AGE_SECONDS
        except OSError:
            continue
        if too_old or index >= TMP_MAX_FILES:
            try:
                entry.unlink()
            except OSError:
                pass
