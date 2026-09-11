"""One line of WanGP's console with nothing in it that identifies a person.

A miniature of the extension's ``minipaint_neo/scrub.py``, for the same reason
``handoff.py`` here is a miniature of the extension's: this plugin is installed
into somebody else's Python environment and cannot import the extension. The
rules are the same rules, kept deliberately short - this side only ever writes
its own sentences, never a user's prompt - and the extension's copy is the
reference if the two ever disagree.

What this exists to stop is narrow and real. The bridge's own notes are
written to be harmless, but two things it says are not written by it: the
repr of an exception it did not expect, and a traceback. Both carry absolute
paths, and on a single-user machine an absolute path carries the account name.
When WanGP was started by the integration these lines are scrubbed again on
the way through, but WanGP is just as often started by hand, and then this is
the only pass there is.
"""

from __future__ import annotations

import re
import typing

MAX_LINE = 400

#: Nothing may start a match after a placeholder, so scrubbing twice is
#: scrubbing once.
_GUARD = r"(?<![\w<>*])"

_SECRET = re.compile(
    r"(?i)(secrets?|tokens?|passwords?|authorization|api[_-]?keys?|cookies?)\b\s*[=:]\s*\S+"
)
_TEXT_KEY = re.compile(r"""(?i)\b((?:negative[ _-]?|positive[ _-]?)?(?:prompts?|captions?))["']?\s*[=:]\s*(?!\s*["']?<prompt>).*""")
_EMAIL = re.compile(_GUARD + r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_URL = re.compile(_GUARD + r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>]+")
_OPAQUE = re.compile(_GUARD + r"\b(?:[0-9a-fA-F]{32,}|[A-Za-z0-9_-]{43,})\b")

#: A rooted path, down to its last component. Kept when that component is a
#: module - a traceback with no filenames in it is not worth printing - and
#: dropped otherwise, because the last component of an output path is the
#: prompt that produced it.
_PATH = re.compile(
    _GUARD + r"""(?:[A-Za-z]:[\\/]|\\\\|~[\\/]|/(?=[^\s/]))[^\s'"<>|*?;,)\]}]*"""
)
_KEEP_SUFFIXES = (".py", ".pyc", ".pyd", ".so", ".dll", ".exe", ".sh", ".bat")


def _replace_path(match: "re.Match[str]") -> str:
    raw = match.group(0).rstrip(".,;:)]}'\"")
    trailing = match.group(0)[len(raw) :]
    name = re.split(r"[\\/]+", raw)[-1] if raw else ""
    if name.lower().endswith(_KEEP_SUFFIXES):
        return f"<path>/{name}{trailing}"
    dot = name.rfind(".")
    return (f"<path>/*{name[dot:].lower()}" if dot > 0 else "<path>") + trailing


def line(text: typing.Any, limit: int = MAX_LINE) -> str:
    """Never raises. A rule that fails returns a placeholder, not the original."""
    try:
        cleaned = str(text)
        cleaned = _SECRET.sub(lambda m: f"{m.group(1)}=<hidden>", cleaned)
        cleaned = _TEXT_KEY.sub(lambda m: f"{m.group(1)}: <prompt>", cleaned)
        cleaned = _EMAIL.sub("<email>", cleaned)
        cleaned = _URL.sub("<url>", cleaned)
        cleaned = _PATH.sub(_replace_path, cleaned)
        cleaned = _OPAQUE.sub("<hidden>", cleaned)
        cleaned = "".join(character if character.isprintable() else " " for character in cleaned)
    except Exception:  # pragma: no cover
        return "(a log line could not be scrubbed and was dropped)"
    return cleaned[:limit] if limit and limit > 0 else cleaned


def block(text: typing.Any) -> str:
    """A traceback, scrubbed frame by frame."""
    try:
        return "\n".join(line(one, 0) for one in str(text).splitlines())
    except Exception:  # pragma: no cover
        return "(a traceback could not be scrubbed and was dropped)"
