"""Where the extension lives, and how its files are served to the browser."""

from __future__ import annotations

import os.path
import pathlib
import typing
import urllib.parse

try:
    root_path = pathlib.Path(__file__).resolve().parents[1]
except NameError:  # pragma: no cover - only when exec'd without a __file__
    import inspect

    root_path = pathlib.Path(inspect.getfile(lambda: None)).resolve().parents[1]


def get_asset_url(
    file_path: pathlib.Path, append: typing.Optional[dict[str, str]] = None
) -> str:
    """A ``/file=`` URL for one of our files, cache-busted by its mtime."""
    if append is None:
        append = {"v": str(os.path.getmtime(file_path))}
    else:
        append = append.copy()
        append["v"] = str(os.path.getmtime(file_path))
    return f"/file={file_path.absolute()}?{urllib.parse.urlencode(append)}"


def write_config_file() -> pathlib.Path:
    """The legacy editor's config file. Created empty if it does not exist."""
    config_dir = root_path / "downloads"
    config_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    config_path = config_dir / "config.json"
    if not config_path.exists():
        # get_asset_url() stats this file, so it has to exist.
        config_path.write_text("{}", encoding="utf-8")
    return config_path
