"""The transfer log both frontends write to.

Kept as a plain FastAPI route rather than a Gradio event on purpose: this has
to keep working when what failed *is* the Gradio round trip.
"""

from __future__ import annotations

import datetime
import threading
import typing

from .paths import root_path

SEND_LOG_ROUTE = "/minipaint/log"
SEND_LOG_PATH = root_path / "logs" / "send-log.txt"
SEND_LOG_MAX_BYTES = 1_000_000
SEND_LOG_MAX_STEPS = 200
SEND_LOG_MAX_LINE = 500
_send_log_lock = threading.Lock()


def _clean(value: typing.Any, limit: int = SEND_LOG_MAX_LINE) -> str:
    """One printable line. The browser is the only writer, but it is still input."""
    text = value if isinstance(value, str) else repr(value)
    text = "".join(character if character.isprintable() else " " for character in text)
    return text[:limit]


def format_send_entry(record: typing.Any) -> str:
    """Render one transfer the way it will appear in the log file."""
    if not isinstance(record, dict):
        record = {}

    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    destination = _clean(record.get("destination", "unknown destination"), 120)
    outcome = _clean(record.get("outcome", "unknown outcome"), 300)

    lines = [f"[{stamp}] {destination} -> {outcome}"]

    steps = record.get("steps")
    if isinstance(steps, list):
        for step in steps[:SEND_LOG_MAX_STEPS]:
            lines.append(f"    {_clean(step)}")

    return "\n".join(lines) + "\n\n"


def append_send_log(record: typing.Any):
    """Append one transfer to logs/send-log.txt, rotating it when it grows."""
    entry = format_send_entry(record)

    with _send_log_lock:
        SEND_LOG_PATH.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        if SEND_LOG_PATH.exists() and SEND_LOG_PATH.stat().st_size > SEND_LOG_MAX_BYTES:
            SEND_LOG_PATH.replace(SEND_LOG_PATH.with_name("send-log.previous.txt"))
        with SEND_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(entry)

    return SEND_LOG_PATH


def announce_send_log(frontend: str) -> None:
    """Create logs/send-log.txt as soon as the extension loads.

    An empty folder is a useless answer to "where is the log?": if the file is
    missing after a restart, the extension being loaded is what to doubt, so
    the file says when it was loaded, which frontend is mounted, and where
    transfers will appear.
    """
    try:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        append_send_log(
            {
                "destination": "extension loaded",
                "outcome": (
                    f"ready - {frontend} frontend, transfers will be logged here "
                    f"from {stamp}"
                ),
                "steps": [f"log route: POST {SEND_LOG_ROUTE}"],
            }
        )
    except OSError as error:
        print(f"MiniPaint: could not write {SEND_LOG_PATH}: {error}")


def on_app_started(_demo, app) -> None:
    """Let either editor write its transfer log next to the extension."""

    # The parameter has to be annotated: without the type, FastAPI reads
    # "request" as a required query parameter and answers every POST with 422,
    # which is what happened - the log route was there and refused everything.
    from fastapi import Request

    @app.post(SEND_LOG_ROUTE)
    async def minipaint_log(request: Request):
        try:
            record = await request.json()
        except Exception:
            return {"ok": False, "error": "expected a JSON body"}
        try:
            path = append_send_log(record)
        except OSError as error:
            return {"ok": False, "error": str(error)}
        return {"ok": True, "path": str(path)}

    print(
        f"MiniPaint: transfer log route ready at {SEND_LOG_ROUTE}, "
        f"writing to {SEND_LOG_PATH}"
    )
