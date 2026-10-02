"""Whether WanGP's GGUF kernels are the ones its code was written for.

WanGP accelerates GGUF models with a compiled package of llama.cpp CUDA
kernels, ``llamacpp_gguf_cuda``. It is installed by hand, from a wheel named in
WanGP's ``docs/INSTALLATION.md``, and not by ``requirements.txt`` - so a WanGP
updated the ordinary way (a pull, then the requirements) keeps the kernels it
had. On 2026-10-02 that left WanGP's newer GGUF path running on 1.0.11 kernels
where it asked for 1.0.13 or newer: every GGUF step of LTX 2.3 took about 100
seconds on an RTX 5090 that runs the same model in int8 at 3, and nothing said
so except one line from the prompt enhancer, which is the only feature WanGP
checks the version for. Updating the wheel made it fast again.

So this module says it before a run does. Two sources, either enough:

* **The files.** At every launch the installed version is read from the
  environment's ``site-packages`` (the wheel's ``.dist-info`` folder) and set
  against the newest version WanGP's own install guide names. Older than the
  guide is a warning: the guide is what that WanGP was written against.
* **WanGP's own words.** Every line the child prints passes through
  :func:`observe`, which recognises WanGP saying the kernels are too old for
  it ("... do not meet the 1.0.13+ requirement") or that they could not be
  loaded at all ("kernels unavailable, using fallback").

What it finds goes to the journal and Forge's console once, and to the page
through the interop snapshot (``wangp_warnings``), where the WanGP tab shows it
over the frame. Nothing here ever changes what is installed, and a file that
cannot be read is no warning: this is advice, never a gate.
"""

from __future__ import annotations

import glob
import logging
import os
import re
import threading
import typing

from . import journal

logger = logging.getLogger(__name__)

PACKAGE = "llamacpp_gguf_cuda"

#: Where WanGP's install guide lives, relative to its root.
INSTALL_GUIDE = os.path.join("docs", "INSTALLATION.md")

#: The guide is read up to this many bytes. It is a few dozen kilobytes; a
#: file far bigger than that is not the guide this module knows how to read.
GUIDE_LIMIT = 1024 * 1024

#: The wheel names in the guide: ``llamacpp_gguf_cuda-1.0.25+torch210...``,
#: URL-encoded or not.
_GUIDE_VERSION = re.compile(r"llamacpp_gguf_cuda-(\d+(?:\.\d+){1,3})")

#: WanGP saying its kernels are too old for it. Its own sentence, from the
#: prompt enhancer: "GGUF kernels 1.0.11+torch210cu130py311 do not meet the
#: 1.0.13+ requirement."
_TOO_OLD = re.compile(r"GGUF kernels\s+(\d+(?:\.\d+){1,3})\S*\s+do not meet the\s+(\d+(?:\.\d+){1,3})\+?\s+requirement")

#: WanGP saying it could not load them at all.
_UNAVAILABLE = re.compile(r"\[GGUF\]\[llama\.cpp CUDA\]\s+kernels unavailable", re.IGNORECASE)

TOO_OLD = "GGUF_KERNELS_OUTDATED"
UNAVAILABLE = "GGUF_KERNELS_UNAVAILABLE"

_lock = threading.Lock()
_found: typing.Dict[str, dict] = {}
"""What is known about this run, by code. Cleared at every launch."""
_said: typing.Set[str] = set()
"""Which warnings have been written to the journal and console this run."""


def version_tuple(text: typing.Any) -> typing.Tuple[int, ...]:
    """``"1.0.11+torch210cu130py311"`` as ``(1, 0, 11)``; ``()`` if unreadable."""
    head = str(text or "").strip().split("+", 1)[0]
    parts = []
    for piece in head.split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts)


def _plain(version: typing.Tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


def installed_version(prefix: typing.Any) -> str:
    """The kernels' version in the environment at ``prefix``, or ``""``.

    Read from the wheel's ``.dist-info`` folder, whose name carries it, so
    nothing in WanGP's environment is imported or run. Windows keeps
    ``site-packages`` under ``Lib``, everything else under
    ``lib/pythonX.Y``.
    """
    root = str(prefix or "").strip()
    if not root or not os.path.isdir(root):
        return ""
    patterns = (
        os.path.join(root, "Lib", "site-packages", PACKAGE + "-*.dist-info"),
        os.path.join(root, "lib", "python*", "site-packages", PACKAGE + "-*.dist-info"),
    )
    best: typing.Tuple[int, ...] = ()
    for pattern in patterns:
        for found in glob.glob(pattern):
            name = os.path.basename(found)[len(PACKAGE) + 1:-len(".dist-info")]
            version = version_tuple(name)
            if version > best:
                best = version
    return _plain(best) if best else ""


def guide_version(wangp_root: typing.Any) -> str:
    """The newest kernel version WanGP's install guide names, or ``""``."""
    root = str(wangp_root or "").strip()
    if not root:
        return ""
    try:
        with open(os.path.join(root, INSTALL_GUIDE), "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(GUIDE_LIMIT)
    except OSError:
        return ""
    best: typing.Tuple[int, ...] = ()
    for match in _GUIDE_VERSION.finditer(text):
        version = version_tuple(match.group(1))
        if version > best:
            best = version
    return _plain(best) if best else ""


def _record(code: str, installed: str = "", wanted: str = "", source: str = "") -> None:
    if code == TOO_OLD:
        message = (f"WanGP's GGUF kernels are {installed or 'older than it needs'}; this WanGP asks for "
                   f"{wanted} or newer. GGUF models can run many times slower until they are updated.")
        fix = (f"Update {PACKAGE} in WanGP's environment to the wheel its docs/INSTALLATION.md names "
               f"(pip install --no-deps <that wheel>). requirements.txt does not install it.")
    else:
        message = ("WanGP could not load its GGUF kernels and is using its fallback. "
                   "GGUF models run much slower that way.")
        fix = (f"Install the {PACKAGE} wheel for this environment's Python, torch and CUDA, "
               f"as WanGP's docs/INSTALLATION.md describes.")
    entry = {"code": code, "installed": installed, "wanted": wanted, "source": source,
             "message": message, "fix": fix}
    with _lock:
        _found[code] = entry
        first = code not in _said
        _said.add(code)
    if first:
        journal.note("kernels", f"{message} {fix}")
        try:
            logger.warning("Mini Paint NEO: %s %s", message, fix)
        except Exception:
            pass


def begin_run(wangp_root: typing.Any, prefix: typing.Any) -> None:
    """A new WanGP run: forget the last one's findings and read the files."""
    with _lock:
        _found.clear()
        _said.clear()
    try:
        installed = installed_version(prefix)
        wanted = guide_version(wangp_root)
    except Exception:
        return
    if installed and wanted and version_tuple(installed) < version_tuple(wanted):
        _record(TOO_OLD, installed, wanted, "files")


def observe(line: typing.Any) -> None:
    """One line WanGP printed. Cheap enough for every line of its output."""
    text = str(line or "")
    if "GGUF" not in text:
        return
    match = _TOO_OLD.search(text)
    if match:
        installed, wanted = _plain(version_tuple(match.group(1))), _plain(version_tuple(match.group(2)))
        with _lock:
            known = _found.get(TOO_OLD)
        # The newer of two requirements is the one that matters: the files
        # may know the guide's version, WanGP only the floor it checks.
        if known and version_tuple(known["wanted"]) >= version_tuple(wanted):
            return
        _record(TOO_OLD, installed, wanted, "wangp")
        return
    if _UNAVAILABLE.search(text):
        _record(UNAVAILABLE, source="wangp")


def warnings() -> typing.List[dict]:
    """What the page shows: each finding of this run, in a fixed order."""
    with _lock:
        return [dict(_found[code]) for code in (TOO_OLD, UNAVAILABLE) if code in _found]


def forget() -> None:
    """For tests."""
    with _lock:
        _found.clear()
        _said.clear()
