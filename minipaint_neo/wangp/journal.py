"""A short, in-memory record of what the integration just did.

The tab shows this. It exists because everything interesting about starting
WanGP happens somewhere a user cannot see: a child process's output goes to
the console Forge was launched from, the proxy's probes happen between two
sockets, and the bridge handshake happens inside an iframe. When one of those
goes wrong the screen has room for one sentence, and the sentence is never
enough to act on.

So every step writes a line here, from whichever side it happened on -
including the browser, which posts its own lines back. It is a ring buffer in
memory and nothing else: it is not the transfer log, it does not survive a
restart, and nothing reads it back to make a decision.

Secrets never reach it. The bridge secret and the backend port are the two
things that must not be in something a user pastes into an issue, so they are
replaced on the way in rather than trusted not to be passed.
"""

from __future__ import annotations

import collections
import datetime
import re
import threading
import typing

#: Enough to hold a whole failed setup attempt, short enough to paste.
MAX_LINES = 400
MAX_LINE = 400

_lock = threading.Lock()
_lines: typing.Deque[str] = collections.deque(maxlen=MAX_LINES)

#: Anything shaped like our runtime secret, and the backend port in the two
#: forms it could be written. The port is not a secret in the way a token is,
#: but the browser is not supposed to learn it and a pasted log is a browser
#: away from anywhere.
_SECRET = re.compile(r"(secret|token|password|authorization)\s*[=:]\s*\S+", re.IGNORECASE)
_LOOPBACK = re.compile(r"\b(127\.0\.0\.1|localhost)[:/](\d{2,5})\b")


def scrub(text: typing.Any) -> str:
    """One line, with the two things that must not travel taken out."""
    line = str(text)
    line = _SECRET.sub(lambda match: f"{match.group(1)}=<hidden>", line)
    line = _LOOPBACK.sub(lambda match: f"{match.group(1)}:<port>", line)
    line = "".join(character if character.isprintable() else " " for character in line)
    return line[:MAX_LINE]


def note(source: str, message: typing.Any) -> None:
    """Record one step. Never raises: a journal is never worth an exception."""
    try:
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {str(source)[:16]:<16} {scrub(message)}"
        with _lock:
            _lines.append(line)
    except Exception:  # pragma: no cover - a clock or a deque cannot really fail
        pass


def lines() -> typing.List[str]:
    with _lock:
        return list(_lines)


def text() -> str:
    """The whole journal, oldest first, ready to be shown or pasted."""
    collected = lines()
    return "\n".join(collected) if collected else "(nothing recorded yet)"


def clear() -> None:
    with _lock:
        _lines.clear()
