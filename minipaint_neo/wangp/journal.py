"""A short, in-memory record of what the integration just did.

The tab shows this. It exists because everything interesting about starting
WanGP happens somewhere a user cannot see: a child process's output goes to
the console Forge was launched from, the proxy's probes happen between two
sockets, and the bridge handshake happens inside an iframe. When one of those
goes wrong the screen has room for one sentence, and the sentence is never
enough to act on.

So every step writes a line here, from whichever side it happened on -
including the browser, which posts its own lines back. In memory it is a ring
buffer and nothing more: it is not the transfer log, it does not survive a
restart, and nothing reads it back to make a decision. Every line is also
mirrored into ``process_log``, which is the durable copy on disk - the tab
needs the last few hundred lines and a bug report needs the whole run, and
those are different requirements rather than one requirement with a setting.

Nothing that identifies a person reaches it. Prompts, output filenames, paths,
addresses and secrets are taken out by ``minipaint_neo.scrub`` on the way in
rather than trusted not to be passed, because the largest writer here is
WanGP itself, relayed a line at a time by ``runtime``, and what a third-party
application prints is not something this code gets to promise anything about.
The backend port goes too: it is not a secret the way a token is, but the
browser is not supposed to learn it and this is rendered inside the tab.
"""

from __future__ import annotations

import collections
import datetime
import threading
import typing

from .. import scrub as _scrub
from . import process_log

#: Enough to hold a whole failed setup attempt, short enough to paste.
MAX_LINES = 400
MAX_LINE = _scrub.MAX_LINE


def scrub(text: typing.Any) -> str:
    """One line, with everything that identifies somebody taken out.

    Kept as this module's own name for the operation because the tab, the
    tests and the browser route all call it, and because what "safe enough for
    the journal" means - no PII *and* no backend port - is a stricter rule
    than the one the WebUI console runs under.
    """
    return _scrub.private(text, limit=MAX_LINE)


_lock = threading.Lock()
_lines: typing.Deque[str] = collections.deque(maxlen=MAX_LINES)


def note(source: str, message: typing.Any) -> None:
    """Record one step. Never raises: a journal is never worth an exception."""
    try:
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {str(source)[:16]:<16} {scrub(message)}"
        with _lock:
            _lines.append(line)
    except Exception:  # pragma: no cover - a clock or a deque cannot really fail
        pass
    # Outside the lock, and after the in-memory copy: the tab must not wait on
    # a disk, and a disk that refuses must not cost the tab its line.
    process_log.note(source, message)


def lines() -> typing.List[str]:
    with _lock:
        return list(_lines)


def text() -> str:
    """The whole journal, oldest first, ready to be shown or pasted."""
    collected = lines()
    return "\n".join(collected) if collected else "(nothing recorded yet)"


def clear() -> None:
    """Empty what the tab shows. The file on disk is deliberately untouched:
    a button that says "clear the console" is not a request to destroy the
    only durable record of the run that has just gone wrong."""
    with _lock:
        _lines.clear()
