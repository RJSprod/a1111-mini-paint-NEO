"""The prompt history: the last ten prompts the Clipboard sent to WanGP, as typed.

What the composer's **History** button opens. Asked for in these words: "the
history for the last ten prompts submitted from clipboard to wangp. Not the
enhanced, but the original raw input" - a list to scroll, read and load from.

WHAT GOES IN. Every press of the Clipboard's own two doors that carries a
prompt and is accepted into the queue: the composer's Add to Queue and the
gallery popup's Generate (``outbox.submit`` calls ``remember``, for the
``clipboard`` and ``gallery`` origins only - another extension's request over
the public API is its own). The text is the prompt as it was typed, before any
enhancement: an enhanced press's written prompt replaces the request's own
prompt later, in the outbox, and never reaches this document. A press that was
refused stored nothing and is not here; a press that was stored and then
failed is, because it was sent. A request that inherited the WanGP page's
prompt has no text of its own to keep.

HOW MANY. Ten, newest first. The same text sent again moves to the top rather
than taking a second place: ten places are ten different prompts.

WHAT LOAD DOES. It puts the text in the composer's Prompt box - the shared
draft the popup reads too - and nothing else: the pictures, the enhancement
switch and the queue are left as they are, and nothing is queued. The way back
to a whole request (its pictures included) is a video's Load in View Outputs,
or Queue Send History.

It is a prompt history, not a log: the document holds the user's words because
keeping them is the feature, and nothing here writes a prompt anywhere else -
the journal sees ids, models and counts.
"""

from __future__ import annotations

import datetime
import re
import secrets
import threading
import typing

from ..wangp import protocol
from . import config

#: How many prompts are kept. Newest first; the oldest go.
MAX_PROMPTS = 10
SCHEMA = 1

ORIGIN_LABELS = {"clipboard": "", "gallery": "from the gallery"}
_ID_RE = re.compile(r"\A[0-9a-f]{16}\Z")

_lock = threading.RLock()


def _journal(message: str) -> None:
    try:
        from ..wangp import process_log

        process_log.note("clipboard", message)
    except Exception:
        pass


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _model(raw: typing.Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {key: str(raw.get(key) or "")[:120] for key in ("type", "label", "family", "architecture")}


def _target(value: typing.Any) -> str:
    from . import targets

    return value if value in targets.TARGETS else ""


def _normalize(raw: typing.Any) -> typing.Optional[dict]:
    """One entry as it is kept, or None for anything that is not one."""
    if not isinstance(raw, dict):
        return None
    entry_id = raw.get("id")
    prompt = raw.get("prompt")
    if not isinstance(entry_id, str) or not _ID_RE.match(entry_id) or not isinstance(prompt, str) or not prompt.strip():
        return None
    origin = raw.get("origin") if raw.get("origin") in ORIGIN_LABELS else "clipboard"
    return {
        "id": entry_id,
        "at": str(raw.get("at") or "")[:40],
        "prompt": prompt[:protocol.PROMPT_MAX_CHARS],
        "model": _model(raw.get("model")),
        "target": _target(raw.get("target")),
        "enhanced": raw.get("enhanced") is True,
        "origin": origin,
    }


def _load() -> typing.List[dict]:
    document = config.read_document(config.PROMPTS_NAME, {})
    listed = document.get("prompts") if isinstance(document, dict) else None
    if not isinstance(listed, list):
        return []
    kept = [entry for entry in (_normalize(item) for item in listed) if entry is not None]
    return kept[:MAX_PROMPTS]


def _save(entries: typing.List[dict]) -> None:
    config.write_document(config.PROMPTS_NAME, {"schema": SCHEMA, "prompts": entries[:MAX_PROMPTS]})


def entries() -> typing.List[dict]:
    """Every prompt kept, newest first."""
    with _lock:
        return [dict(entry) for entry in _load()]


def get(entry_id: typing.Any) -> typing.Optional[dict]:
    with _lock:
        for entry in _load():
            if entry["id"] == entry_id:
                return dict(entry)
    return None


def remember(prompt: typing.Any, *, model: typing.Any = None, target: str = "", enhanced: bool = False,
             origin: str = "clipboard") -> typing.Optional[dict]:
    """Keep one prompt, as typed, at the top. Returns the entry, or None for no text.

    The same text already in the list is moved up rather than kept twice.
    """
    text = protocol.clean_prompt(prompt)
    if text is None:
        return None
    entry = _normalize({
        "id": secrets.token_hex(8), "at": _now_iso(), "prompt": text, "model": _model(model),
        "target": target, "enhanced": bool(enhanced), "origin": origin if origin in ORIGIN_LABELS else "clipboard",
    })
    if entry is None:
        return None
    with _lock:
        before = _load()
        listed = [item for item in before if item["prompt"] != entry["prompt"]]
        moved = len(listed) < len(before)
        listed.insert(0, entry)
        _save(listed)
    _journal(f"prompt history: {entry['id'][:8]} kept ({'moved up' if moved else 'new'}; {min(len(listed), MAX_PROMPTS)} of {MAX_PROMPTS})")
    return dict(entry)


def view() -> typing.List[dict]:
    """The list as the History dialog draws it, newest first: the text in
    full, and the facts that say which press it was."""
    from . import targets

    shown = []
    for entry in entries():
        model = entry["model"]
        shown.append({
            "id": entry["id"],
            "at": entry["at"],
            "when": entry["at"].replace("T", " ").replace("+00:00", " UTC"),
            "prompt": entry["prompt"],
            "model": model.get("label") or model.get("type") or "",
            "target": entry["target"],
            "target_label": targets.LABELS.get(entry["target"], ""),
            "enhanced": entry["enhanced"],
            "origin": ORIGIN_LABELS.get(entry["origin"], ""),
        })
    return shown


def load(entry_id: typing.Any) -> typing.Optional[str]:
    """Put one prompt in the composer's draft. Returns its text, or None when
    it is no longer in the list. Nothing else in the draft is touched."""
    from . import history

    found = get(entry_id)
    if found is None:
        return None
    draft = history.load_draft()
    draft["prompt_override"] = found["prompt"]
    history.save_draft(draft)
    _journal(f"prompt history: {found['id'][:8]} loaded into the composer")
    return found["prompt"]


def reset_for_tests() -> None:
    with _lock:
        _save([])


__all__ = ["MAX_PROMPTS", "ORIGIN_LABELS", "SCHEMA", "entries", "get", "load", "remember", "reset_for_tests", "view"]
