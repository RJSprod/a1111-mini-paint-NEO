"""The gallery's send button, pointed at WanGP: a request launcher over Clipboard.

The 🖌️ button under a txt2img / img2img / Extras result has one setting
behind it, the *intercept destination*: Mini Paint (the Canvas, as it always
was), Clipboard (the picture goes into the library), or WanGP. This module
is the whole of the third choice's server half, and it is deliberately a
thin layer over what Clipboard already does:

* the picked gallery picture is **frozen** the moment the button is
  pressed - staged through the public API's own staging area
  (``interop.stage_image``) as a transient, never as a library asset - and
  the browser is handed one opaque token for it;
* the compact popup the browser then opens asks *here* what it may offer:
  which generic image roles the current WanGP model takes, which of them is
  the default, the shared prompt and enhancer switch, whether WanGP is idle
  or generating, whether Generate is a button at all;
* Generate hands back the popup's visible values plus that token, and this
  module builds the request the same way the Clipboard composer does -
  ``history.public_request`` over the draft, or over an empty one when the
  user asked not to inherit Clipboard's inputs - overlays the frozen picture
  on the chosen roles, and submits it through ``outbox.submit``, which is
  the one door every WanGP request goes through;
* every accepted submission is one entry in a history of its own, pinnable,
  capped at a hundred unpinned entries, reloadable against whatever model
  Clipboard is mapped to later.

WHAT IS MODEL-AGNOSTIC HERE, AND WHY IT MATTERS. Roles are the three
generic ids the enhancer already spells - ``first_frame``, ``last_frame``,
``reference`` - and which of them exist for the current model comes from
the live page's own input support, as the bridge reports it, narrowed by
what Clipboard sends that model at all (``targets.FIELDS``: LTX 2.3 and
2.5 Distilled are never sent a reference). The *default* role is the one thing
that leans on a model family, and it leans on the mapping
``enhance.SLOTS_FOR`` already holds rather than on a name spelled here.
Nothing in the popup, and nothing in this file's public answers, says
"MiniMax", "FL2VA", "REF2VA" or "LTX": a future model family is a change to
those mappings and to nothing in the intercept path.

THE POPUP IS BLOCKED THE WAY THE COMPOSER IS. ``describe`` carries the
Clipboard's readiness answer (``targets.readiness``): unless WanGP is
running, its bridge answering and the page on a supported, downloaded model,
the popup shows that one quiet sentence instead of its prompt, its switch,
its roles and Generate - and Generate, pressed anyway, is refused by the
same fresh check every Clipboard press meets.

Nothing here logs a prompt, a filename or a path. The journal sees roles,
counts and the first eight characters of an id.
"""

from __future__ import annotations

import collections
import datetime
import io
import re
import secrets
import threading
import time
import typing

from ..wangp import errors, protocol
from ..wangp.errors import IntegrationError
from . import config, enhance, history, outbox

#: What a Canvas receive hands the browser for the WanGP destination:
#: ``wangp:<token>:<width>x<height>:<tab>``. The prefix is what the browser
#: keys on, so it lives here, once.
POPUP_PREFIX = "wangp:"

#: The generic image roles, as the enhancer already names them. The label
#: is what the popup shows; the field is the public request field the role
#: fills. This is Clipboard's mapping layer and it is the only place the
#: three are tied together.
ROLE_FIRST = enhance.SLOT_FIRST
ROLE_LAST = enhance.SLOT_LAST
ROLE_REFERENCE = enhance.SLOT_REFERENCE
ROLES: typing.Tuple[typing.Tuple[str, str, str], ...] = (
    (ROLE_FIRST, "First Frame", protocol.QUEUE_FIELD_START),
    (ROLE_LAST, "Last Frame", protocol.QUEUE_FIELD_END),
    (ROLE_REFERENCE, "Reference", protocol.QUEUE_FIELD_REFERENCES),
)
ROLE_IDS: typing.Tuple[str, ...] = tuple(role for role, _label, _field in ROLES)
ROLE_LABELS = {role: label for role, label, _field in ROLES}
ROLE_FIELDS = {role: field for role, _label, field in ROLES}
FIELD_ROLES = {field: role for role, _label, field in ROLES}
#: Which Clipboard draft slot each role's field comes from, when inheriting.
FIELD_SLOTS = {
    protocol.QUEUE_FIELD_START: ("first_asset_id", "First Frame"),
    protocol.QUEUE_FIELD_END: ("last_asset_id", "Last Frame"),
    protocol.QUEUE_FIELD_REFERENCES: ("reference_asset_ids", "Reference"),
}

#: The popup's history: its own document, because it is a record of
#: launcher recipes and not of the composer's confirmed queue sends.
HISTORY_NAME = "clipboard-intercept-history.json"
HISTORY_SCHEMA = 1
#: How many unpinned entries are kept. Pinned entries never count and are
#: never trimmed.
MAX_UNPINNED = 100
#: A ceiling on pinned entries too, so a document cannot grow without
#: bound; far above anything a person pins by hand.
MAX_PINNED = 500
#: The summary line kept with an entry, and the width the preview is
#: served at.
SUMMARY_MAX = 200
PREVIEW_SIDE = 160

#: The one word a WanGP status can be, for the popup's dot.
STATUS_OFF = "off"
STATUS_IDLE = "idle"
STATUS_BUSY = "busy"
STATUS_RUNNING = "running"
STATUS_UNKNOWN = "unknown"

_TAB_RE = re.compile(r"\A[a-z0-9_]{0,24}\Z")
_HISTORY_ID_RE = re.compile(r"\A[0-9a-f]{16}\Z")

_LOG_PREFIX = "MiniPaint Clipboard:"


def _journal(message: str) -> None:
    try:
        from ..wangp import process_log

        process_log.note("intercept", message)
    except Exception:
        pass


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


# ----------------------------------------------------------------- the token --


def handoff_text(token: str, width: int, height: int, tab: str = "") -> str:
    """The string a receive hands the browser: prefix, token, size, source tab."""
    name = str(tab or "")
    if not _TAB_RE.match(name):
        name = ""
    return f"{POPUP_PREFIX}{token}:{int(width)}x{int(height)}:{name}"


def parse_handoff(text: typing.Any) -> typing.Optional[dict]:
    """The token, size and tab out of a handoff string, or None for anything else.

    Forgiving about the tail - a token alone is a handoff - and strict about
    the token, which is the only part that names a file.
    """
    raw = str(text or "")
    if not raw.startswith(POPUP_PREFIX):
        return None
    parts = raw[len(POPUP_PREFIX):].split(":")
    token = parts[0] if parts else ""
    if not protocol.valid_handoff_id(token):
        return None
    width = height = 0
    if len(parts) > 1 and "x" in parts[1]:
        left, _, right = parts[1].partition("x")
        try:
            width, height = max(0, int(left)), max(0, int(right))
        except ValueError:
            width = height = 0
    tab = parts[2] if len(parts) > 2 and _TAB_RE.match(parts[2] or "") else ""
    return {"token": token, "width": width, "height": height, "tab": tab}


def stage(image: typing.Any, tab: str = "") -> str:
    """Freeze the picked gallery picture for one request. Returns the handoff.

    Through the public API's staging area, which is exactly the "transient
    request input" the design asks for: swept by age, never indexed, never a
    library asset. The Clipboard folder is not touched.
    """
    from .. import interop

    answer = interop.stage_image(image)
    token = answer["image"]["id"]
    _journal(f"staged a gallery picture ({answer['width']}x{answer['height']}) from {tab or 'a gallery'} as {token[:8]}")
    return handoff_text(token, answer["width"], answer["height"], tab)


def staged_exists(token: str) -> bool:
    from .. import interop

    try:
        interop.resolve_staged(token)
        return True
    except IntegrationError:
        return False


def discard(token: typing.Any) -> bool:
    """Let the frozen picture go. Never raises; a bad token removes nothing."""
    from .. import interop

    if not protocol.valid_handoff_id(token):
        return False
    existed = staged_exists(str(token))
    interop.discard_staged(str(token))
    return existed


def preview(token: typing.Any, side: int = PREVIEW_SIDE) -> typing.Tuple[bytes, str]:
    """A small copy of the frozen picture, for the popup's thumbnail.

    Made on demand and not cached: the token lives minutes and the popup
    asks once. Refused for a token that is not a staged image.
    """
    from PIL import Image

    from .. import interop

    path = interop.resolve_staged(token)
    with Image.open(str(path)) as opened:
        opened.load()
        small = opened.convert("RGBA")
        small.thumbnail((max(16, int(side)), max(16, int(side))))
    buffer = io.BytesIO()
    try:
        small.save(buffer, format="WEBP", quality=82, method=0)
        return buffer.getvalue(), "image/webp"
    except Exception:
        buffer = io.BytesIO()
        small.save(buffer, format="PNG")
        return buffer.getvalue(), "image/png"


# ------------------------------------------------------------ capabilities --


def _support(inputs: typing.Any) -> typing.Optional[typing.Dict[str, bool]]:
    """The live page's input support as ``{field: bool}``, or None when unknown.

    Two shapes are read: the public API's ``inputs`` block
    (``{start: {supported}, end: {...}, references: {...}}``) and a flat
    ``{start: true}``. An answer that names none of the fields is no answer.
    """
    if not isinstance(inputs, dict):
        return None
    found: typing.Dict[str, bool] = {}
    for field in (protocol.QUEUE_FIELD_START, protocol.QUEUE_FIELD_END, protocol.QUEUE_FIELD_REFERENCES):
        value = inputs.get(field)
        if isinstance(value, dict):
            found[field] = value.get("supported") is True
        elif isinstance(value, bool):
            found[field] = value
    return found if found else None


def capabilities(model: typing.Any = None, inputs: typing.Any = None) -> dict:
    """What the popup may offer for the frozen picture, in generic terms.

    ``roles`` are the ones the live page takes - every one of them when the
    page has not said (``known`` False), because a request is judged again,
    live, when it runs and a role the model ignores is reported rather than
    refused. ``default_roles`` comes from the enhancer's slot mapping for
    the model's variant when it has one, else the first role on offer.
    """
    from . import targets

    block = enhance.model_block(model)
    support = _support(inputs)
    known = support is not None
    variant = enhance.target_for_model(block)
    sent = targets.fields_for(variant)
    available = [role for role in ROLE_IDS
                 if ROLE_FIELDS[role] in sent and (not known or support.get(ROLE_FIELDS[role], False))]
    preferred = [FIELD_ROLES[field] for field in enhance.SLOTS_FOR.get(variant, {}) if field in FIELD_ROLES]
    defaults = [role for role in preferred if role in available][:1] or available[:1]
    return {
        "roles": [{"id": role, "label": ROLE_LABELS[role]} for role in available],
        "default_roles": defaults,
        "inherit_supported": True,
        "known": known,
        "model": block,
        "all_roles": [{"id": role, "label": ROLE_LABELS[role]} for role in ROLE_IDS],
    }


def reconcile_roles(wanted: typing.Any, caps: typing.Mapping[str, typing.Any]) -> typing.Tuple[typing.List[str], typing.List[str]]:
    """The roles to use from what was chosen: still-valid ones kept, the rest
    dropped, and the defaults only when nothing valid remains."""
    available = [role["id"] for role in caps.get("roles") or []]
    chosen = [role for role in ROLE_IDS if isinstance(wanted, (list, tuple)) and role in wanted]
    kept = [role for role in chosen if role in available]
    dropped = [role for role in chosen if role not in available]
    if not kept:
        kept = [role for role in caps.get("default_roles") or [] if role in available]
    return kept, dropped


def auto_roles(caps: typing.Mapping[str, typing.Any], chosen: typing.Any = None) -> typing.Tuple[typing.List[str], typing.List[str]]:
    """Auto's roles against what the model takes: ``(kept, dropped)``.

    The Clipboard tab's Auto, for a picture in the grid. The gallery popup's
    Auto puts the picture in the roles ticked in the popup instead, since
    2026-11-23 (``submit``), and never reads these.

    ``chosen`` is the Auto Options setting unless a caller names its own. A
    role the model on the WanGP page does not read is dropped - Reference on
    LTX, a frame on Ref2VA - and, unlike ``reconcile_roles``, nothing stands
    in for it: Auto with no role left is refused (``auto_refusal``) rather
    than sending the picture in a role nobody chose.
    """
    roles = config.auto_roles_of(config.load().auto_roles if chosen is None else chosen)
    available = [role["id"] for role in caps.get("roles") or []]
    return [role for role in roles if role in available], [role for role in roles if role not in available]


def auto_refusal(dropped: typing.Sequence[str], caps: typing.Mapping[str, typing.Any]) -> str:
    """Why Auto queued nothing: every one of its roles is one the model ignores."""
    block = caps.get("model") if isinstance(caps.get("model"), dict) else {}
    model = str(block.get("label") or block.get("type") or "") or "the current WanGP model"
    roles = config.auto_roles_text(dropped)
    return f"Auto sends the picture as {roles}, and {model} reads none of them. Nothing was queued."


# ------------------------------------------------------------------ status --


def wangp_status() -> dict:
    """Whether the managed WanGP is off, idle or generating, coarsely.

    From the runtime's snapshot and the executor's cached hello - never a
    fresh call into the child, because this answers a route. The browser
    may know more (the bridge's live flag) and says so itself.
    """
    # The same question the queue button asks, through the same seam, so
    # the dot and the button cannot disagree about whether WanGP is there.
    running = bool(outbox.wangp_running())
    if not running:
        # Not "Generate queues on the server, which starts it" any more: a
        # press from here is refused while WanGP is stopped (see targets).
        return {"state": STATUS_OFF, "running": False, "generating": None, "text": "WanGP is not running"}
    generating: typing.Optional[bool] = None
    try:
        from ..wangp import control

        hello = control.last_hello()
        if hello is not None and isinstance(hello.get("generation_running"), bool):
            generating = hello["generation_running"]
    except Exception:
        generating = None
    if generating is True:
        return {"state": STATUS_BUSY, "running": True, "generating": True, "text": "WanGP is generating; a new request joins its queue"}
    if generating is False:
        return {"state": STATUS_IDLE, "running": True, "generating": False, "text": "WanGP is idle"}
    return {"state": STATUS_RUNNING, "running": True, "generating": None, "text": "WanGP is running"}


def generate_button(ready: typing.Optional[typing.Mapping[str, typing.Any]] = None) -> dict:
    """Whether Generate is a button: the same decision Add to Queue makes.

    ``ready`` is the Clipboard's readiness answer when the caller has one;
    a section that is blocked has no Generate, whatever the executor says.
    """
    if ready is not None and not ready.get("ready"):
        return {"label": "Generate", "enabled": False, "reason": str(ready.get("message") or "")}
    try:
        from . import ui

        tab = ui.current()
        if tab is not None:
            view = tab._queue_button_view()
            return {"label": "Generate", "enabled": bool(view.get("enabled")),
                    "reason": "" if view.get("enabled") else str(view.get("label") or "")}
    except Exception:
        pass
    if outbox.chosen_executor() == outbox.EXECUTOR_SERVER:
        return {"label": "Generate", "enabled": True, "reason": ""}
    alive = outbox.wangp_running()
    return {"label": "Generate", "enabled": alive, "reason": "" if alive else "WanGP is not running"}


# ------------------------------------------------------------------ history --


def _normalize_entry(raw: typing.Any) -> typing.Optional[dict]:
    raw = raw if isinstance(raw, dict) else None
    if raw is None:
        return None
    entry_id = raw.get("id")
    if not isinstance(entry_id, str) or not _HISTORY_ID_RE.match(entry_id):
        return None
    roles = [role for role in ROLE_IDS if isinstance(raw.get("roles"), list) and role in raw["roles"]]
    available = [role for role in ROLE_IDS if isinstance(raw.get("available_roles"), list) and role in raw["available_roles"]]
    image = raw.get("image") if isinstance(raw.get("image"), dict) else {}
    prompt = raw.get("prompt") if isinstance(raw.get("prompt"), str) else ""

    def whole(value: typing.Any) -> int:
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0

    return {
        "id": entry_id,
        "created_at": str(raw.get("created_at") or "")[:40],
        "prompt": prompt[:protocol.PROMPT_MAX_CHARS],
        "enhance": raw.get("enhance") is True,
        # A prompt the editor's Enhance wrote and the user took: sent as it
        # was, and loaded back as one - never enhanced again (2026-11-23).
        "prewritten": raw.get("prewritten") is True,
        "inherit": raw.get("inherit") is not False,
        "roles": roles,
        "available_roles": available,
        "model": enhance.model_block(raw.get("model")),
        "summary": str(raw.get("summary") or "")[:SUMMARY_MAX],
        "pinned": raw.get("pinned") is True,
        "job_id": str(raw.get("job_id") or "")[:32],
        "request_id": raw["request_id"] if protocol.valid_handoff_id(raw.get("request_id")) else "",
        "executor": str(raw.get("executor") or "")[:16],
        "image": {"width": whole(image.get("width")), "height": whole(image.get("height")),
                  "tab": str(image.get("tab") or "")[:24]},
        # Where the job's WanGP settings came from: one of the outbox's keys,
        # filled in once the job has been composed. See history_view.
        "settings": raw.get("settings") if raw.get("settings") in outbox.SETTINGS_KEYS else "",
    }


def _load_entries() -> typing.List[dict]:
    document = config.read_document(HISTORY_NAME, {})
    version = document.get("schema_version") if isinstance(document, dict) else None
    if isinstance(version, int) and not isinstance(version, bool) and version > HISTORY_SCHEMA:
        config.quarantine(config.path_of(HISTORY_NAME), f"schema {version} is newer than {HISTORY_SCHEMA}")
        return []
    listed = document.get("history") if isinstance(document, dict) else None
    if not isinstance(listed, list):
        return []
    return [entry for entry in (_normalize_entry(item) for item in listed) if entry is not None]


def _save_entries(entries: typing.List[dict]) -> None:
    config.write_document(HISTORY_NAME, {"schema_version": HISTORY_SCHEMA, "history": entries})


def trim(entries: typing.List[dict]) -> typing.List[dict]:
    """The retention rule: every pinned entry, and the newest MAX_UNPINNED others.

    The list is kept newest first, so trimming is keeping the first hundred
    unpinned entries in order; pinned ones are never counted and never
    dropped by this. Only a person deletes a pinned entry.
    """
    pinned = [entry for entry in entries if entry["pinned"]][:MAX_PINNED]
    unpinned = [entry for entry in entries if not entry["pinned"]][:MAX_UNPINNED]
    kept = pinned + unpinned
    kept.sort(key=lambda entry: entry["created_at"], reverse=True)
    return kept


def load_history() -> typing.List[dict]:
    """Every entry, pinned first, newest first within each group."""
    entries = trim(_load_entries())
    return [entry for entry in entries if entry["pinned"]] + [entry for entry in entries if not entry["pinned"]]


def add_history(entry: typing.Mapping[str, typing.Any]) -> dict:
    cleaned = _normalize_entry(dict(entry, id=entry.get("id") or secrets.token_hex(8), created_at=entry.get("created_at") or _now_iso()))
    if cleaned is None:
        raise ValueError("not a history entry")
    entries = _load_entries()
    entries.insert(0, cleaned)
    _save_entries(trim(entries))
    return cleaned


def delete_history(entry_id: typing.Any) -> bool:
    entries = _load_entries()
    kept = [entry for entry in entries if entry["id"] != entry_id]
    if len(kept) == len(entries):
        return False
    _save_entries(kept)
    return True


def pin_history(entry_id: typing.Any, pinned: typing.Any) -> typing.Optional[dict]:
    """Pin or unpin one entry. Returns it, or None when it is gone."""
    entries = _load_entries()
    found = None
    for entry in entries:
        if entry["id"] == entry_id:
            entry["pinned"] = bool(pinned)
            found = entry
    if found is None:
        return None
    _save_entries(trim(entries))
    return dict(found)


def get_history(entry_id: typing.Any) -> typing.Optional[dict]:
    for entry in _load_entries():
        if entry["id"] == entry_id:
            return dict(entry)
    return None


def _summary(request: typing.Mapping[str, typing.Any], roles: typing.Sequence[str], inherit: bool, enhance_wanted: bool) -> str:
    parts = []
    if request.get("prompt") is not None:
        parts.append("prompt" + (" (enhanced)" if enhance_wanted else ""))
    parts.append("picture as " + ", ".join(ROLE_LABELS[role].lower() for role in roles))
    images = request.get("images") if isinstance(request.get("images"), dict) else {}
    kept = [FIELD_SLOTS[field][1].lower() for field in FIELD_SLOTS
            if field in images and FIELD_ROLES[field] not in roles]
    if kept:
        parts.append("Clipboard's " + ", ".join(kept))
    return "; ".join(parts)[:SUMMARY_MAX]


def history_view(model: typing.Any = None, inputs: typing.Any = None) -> typing.List[dict]:
    """The history as the popup draws it: pinned first, each with its live outcome.

    The outcome is joined on from the outbox while the job is still there
    (minutes after it finishes); afterwards an entry is a recipe and says
    nothing about how it went, which is the truth.
    """
    caps = capabilities(model, inputs)
    available = {role["id"] for role in caps["roles"]}
    try:
        from . import ui as ui_module

        labels = dict(ui_module.OUTBOX_LABELS)
    except Exception:
        labels = {}
    view = []
    learned: typing.Dict[str, str] = {}
    for entry in load_history():
        outcome = ""
        settings = entry.get("settings") or ""
        if entry["job_id"]:
            job = outbox.get(entry["job_id"])
            if job is not None:
                state = str(job.get("state") or "")
                outcome = labels.get(state, state)
                # Unlike the outcome, this is a fact about the recipe that
                # stays true after the job has gone, so it is kept.
                known = outbox.settings_source(job)
                if known and known != settings:
                    settings = learned[entry["id"]] = known
        view.append({
            "id": entry["id"],
            "when": entry["created_at"].replace("T", " ").replace("+00:00", " UTC"),
            "prompt": entry["prompt"][:160],
            "enhance": entry["enhance"],
            "prewritten": entry["prewritten"],
            "inherit": entry["inherit"],
            "roles": [{"id": role, "label": ROLE_LABELS[role], "valid": role in available} for role in entry["roles"]],
            "summary": entry["summary"],
            "pinned": entry["pinned"],
            "model": entry["model"].get("label") or entry["model"].get("type") or "",
            "outcome": outcome,
            "image": dict(entry["image"]),
            "settings": outbox.SETTINGS_TEXT.get(settings, ""),
        })
    if learned:
        _remember_settings(learned)
    return view


def _remember_settings(learned: typing.Mapping[str, str]) -> None:
    """Write down where these entries' settings came from. Best effort: a
    view that could not save it says it anyway, and says it again next time."""
    try:
        entries = _load_entries()
        for entry in entries:
            if entry["id"] in learned:
                entry["settings"] = learned[entry["id"]]
        _save_entries(entries)
    except Exception:
        pass


def recipe(entry_id: typing.Any, model: typing.Any = None, inputs: typing.Any = None) -> dict:
    """A history entry as a request recipe, reconciled with what is on offer now.

    Roles the current mapping no longer supports are dropped and named;
    when none survive the current defaults are used and the popup says so.
    Loading restores the shared prompt and the enhancer switch here, on the
    server, because both are Clipboard's state rather than the popup's.
    """
    entry = get_history(entry_id)
    if entry is None:
        raise IntegrationError(errors.REQUEST_INVALID, "that history entry is gone")
    caps = capabilities(model, inputs)
    kept, dropped = reconcile_roles(entry["roles"], caps)
    draft = history.load_draft()
    draft["prompt_override"] = entry["prompt"]
    history.save_draft(draft)
    # A prompt the editor wrote was sent with the switch as the user had it,
    # and off for that press alone; loading it leaves the switch alone too.
    enabled = enhance.enabled() if entry["prewritten"] else enhance.set_enabled(entry["enhance"])
    config.update(intercept_inherit=entry["inherit"])
    _journal(f"history {entry['id'][:8]} loaded: {len(kept)} role(s) kept, {len(dropped)} dropped")
    return {
        "ok": True,
        "id": entry["id"],
        "prompt": entry["prompt"],
        "enhance": enabled,
        "prewritten": entry["prewritten"],
        "inherit": entry["inherit"],
        "roles": kept,
        "dropped_roles": [{"id": role, "label": ROLE_LABELS[role]} for role in dropped],
        "defaulted": bool(dropped) and not [role for role in entry["roles"] if role in kept],
        "capabilities": caps,
    }


# ------------------------------------------------------------- the request --


def _draft_images_summary(draft: typing.Mapping[str, typing.Any]) -> typing.List[dict]:
    """Which Clipboard slots hold a picture, by role and name, for the popup's
    inheritance hint. Names only, never a path."""
    try:
        from . import store as store_module

        library = store_module.store()
    except Exception:
        return []
    found = []
    for field, (key, label) in FIELD_SLOTS.items():
        ids = draft.get(key) if field == protocol.QUEUE_FIELD_REFERENCES else ([draft.get(key)] if draft.get(key) else [])
        names = []
        for asset_id in ids or []:
            asset = library.get(asset_id) if protocol.valid_handoff_id(asset_id) else None
            names.append(asset.filename if asset is not None else "missing image")
        if names:
            found.append({"role": FIELD_ROLES[field], "label": label, "names": names})
    return found


def build_request(prompt: typing.Any, roles: typing.Sequence[str], token: str, inherit: bool) -> dict:
    """The public request: Clipboard's draft as the base when inheriting, an
    empty one otherwise, then the frozen picture on every chosen role.

    The base is built by the same function the composer's press uses, so
    what "inherit Clipboard's inputs" means is decided in one place. A
    draft slot whose file has left the folder refuses the request here,
    before anything is stored - unless the picture overrides that slot,
    in which case the missing file was never going to be read.
    """
    draft = history.load_draft() if inherit else history.empty_draft()
    draft["prompt_override"] = str(prompt or "")
    overridden = {ROLE_FIELDS[role] for role in roles}
    if inherit:
        try:
            from . import store as store_module

            library = store_module.store()
        except Exception:
            library = None
        for field, (key, label) in FIELD_SLOTS.items():
            if field in overridden or library is None:
                continue
            ids = draft.get(key) if field == protocol.QUEUE_FIELD_REFERENCES else ([draft.get(key)] if draft.get(key) else [])
            if any(library.get(item) is None for item in ids or []):
                raise IntegrationError(errors.CLIPBOARD_ASSET_UNKNOWN, f"{label}: the image is no longer in the folder")
    request = history.public_request(draft)
    handle = {"kind": "staged", "id": token}
    for role in roles:
        field = ROLE_FIELDS[role]
        request["images"][field] = [dict(handle)] if field == protocol.QUEUE_FIELD_REFERENCES else dict(handle)
    request["start"] = protocol.START_AUTO
    return request


def describe(handoff: typing.Any, model: typing.Any = None, inputs: typing.Any = None, fresh: bool = False) -> dict:
    """Everything the popup draws, in one answer. ``fresh`` is Check again's:
    the readiness check skips its few seconds of cache."""
    parsed = parse_handoff(handoff)
    if parsed is None:
        return {"ok": False, "code": errors.REQUEST_INVALID, "message": "That is not a WanGP request handoff."}
    if not staged_exists(parsed["token"]):
        return {"ok": False, "code": errors.INTERCEPT_IMAGE_EXPIRED, "message": errors.message(errors.INTERCEPT_IMAGE_EXPIRED)}
    from . import targets

    draft = history.load_draft()
    current = config.load()
    availability = enhance.availability(model)
    ready = targets.readiness(model, fresh=bool(fresh))
    caps = capabilities(model, inputs)
    return {
        "ok": True,
        "token": parsed["token"],
        "image": {"width": parsed["width"], "height": parsed["height"], "tab": parsed["tab"]},
        "readiness": ready,
        "capabilities": caps,
        "prompt": draft["prompt_override"],
        "enhance": {"enabled": enhance.enabled(), "state": availability.get("state", "unknown"),
                    "text": availability.get("text", "")},
        "inherit": {"default": bool(current.intercept_inherit), "supported": True, "draft": _draft_images_summary(draft)},
        "wangp": wangp_status(),
        "generate": generate_button(ready),
        "executor": outbox.chosen_executor(),
        "history": history_view(model, inputs),
    }


def save_prompt(prompt: typing.Any) -> dict:
    """The shared prompt, written into Clipboard's draft. What Cancel leaves."""
    draft = history.load_draft()
    draft["prompt_override"] = str(prompt or "")
    history.save_draft(draft)
    return {"ok": True}


def cancel(handoff: typing.Any) -> dict:
    """Cancel: the frozen picture is let go and nothing else happens."""
    parsed = parse_handoff(handoff)
    released = discard(parsed["token"]) if parsed else False
    if parsed:
        _journal(f"cancelled {parsed['token'][:8]}; the staged picture {'released' if released else 'was already gone'}")
    return {"ok": True, "released": released}


def submit(
    handoff: typing.Any,
    prompt: typing.Any,
    roles: typing.Any,
    inherit: typing.Any,
    enhance_wanted: typing.Any,
    page: typing.Any,
    model: typing.Any = None,
    inputs: typing.Any = None,
    settings_flush: typing.Any = "",
    auto: bool = False,
    prewritten: bool = False,
    typed: typing.Any = None,
) -> dict:
    """Generate: capture, revalidate, build, submit, record - in that order.

    Every refusal is answered before anything is stored, with the code's
    sentence, and the frozen picture is kept so the popup can try again.
    A success closes the popup on the browser side; here it is one job in
    the outbox, one history entry, and - when the server executes the job
    and so already owns its own pinned copy of the picture - the staged
    original let go.

    ``settings_flush`` is what the popup's page managed to do about WanGP's
    form just before it pressed - saved it, found nothing new, or got no
    answer - and it is recorded on the job, never believed beyond that: see
    ``outbox.submit``.

    ``auto`` is the popup's Auto rather than Generate, and since 2026-11-23
    it puts the picture exactly where Generate does: in the roles ticked in
    this popup, one or several, at the user's word ("when I choose auto and
    I select only one of the options or multiple of the options, it should
    just respect whatever I selected within that pop-up"). Auto Options,
    which chose them here before, are the Clipboard tab's Auto's alone.
    Everything else is Generate's too - the prompt, Enhance and Inherit as
    they stand - and what Auto changes is only what the answer says.

    ``prewritten`` is a prompt the enhancer already wrote in the editor and
    the user took (2026-11-23): it is sent as it is - never enhanced a second
    time, whatever the switch says, and the switch is left where it is -
    with ``typed``, what the user had typed before it was written, as the
    prompt the History keeps.
    """
    parsed = parse_handoff(handoff)
    if parsed is None:
        return {"ok": False, "code": errors.REQUEST_INVALID, "message": "That is not a WanGP request handoff."}
    token = parsed["token"]
    if not staged_exists(token):
        return {"ok": False, "code": errors.INTERCEPT_IMAGE_EXPIRED, "message": errors.message(errors.INTERCEPT_IMAGE_EXPIRED)}
    caps = capabilities(model, inputs)
    # The ticked roles, for Auto as for Generate (2026-11-23).
    kept, dropped = reconcile_roles(roles, caps)
    if not kept:
        return {"ok": False, "code": errors.INTERCEPT_NO_IMAGE_ROLE, "message": errors.message(errors.INTERCEPT_NO_IMAGE_ROLE)}
    # Only an Inherit the press says is on: anything else - off, or a body
    # that does not say - sends the picture in the ticked roles and nothing
    # of Clipboard's with it (2026-11-23).
    use_draft = inherit is True
    written = prewritten is True
    wanted = False if written else (None if enhance_wanted is None else bool(enhance_wanted))
    typed_text = str(typed) if written and isinstance(typed, str) and typed.strip() else None
    block = enhance.model_block(model)
    # The shared prompt is Clipboard's the moment Generate is pressed - the
    # visible value, not whatever a blur may or may not have saved.
    save_prompt(prompt)
    try:
        request = build_request(prompt, kept, token, use_draft)
        if not written and wanted is not None and wanted != enhance.enabled():
            # The switch on screen and the setting on disk had drifted, as
            # they can when a change event is lost; the press is the answer.
            # Not for a prompt the editor already wrote: that press is off
            # for itself alone, and the switch stays as the user left it.
            enhance.set_enabled(wanted)
        job = outbox.submit(request, page, outbox.ORIGIN_GALLERY, model=block, enhance=wanted,
                            settings_flush=str(settings_flush or ""), typed_prompt=typed_text)
    except IntegrationError as error:
        _journal(f"generate refused before storing - {error.code}")
        notes = []
        view = error.extra.get("readiness") if isinstance(error.extra.get("readiness"), dict) else None
        if error.code == errors.WANGP_NOT_RUNNING:
            notes.append("nothing was stored; open the WanGP tab and let it start")
        elif error.code.startswith("ENHANCE_"):
            notes.append("nothing was stored; switch Enhance off to send the prompt as typed")
        elif error.code == errors.CLIPBOARD_ASSET_UNKNOWN:
            notes.append("a Clipboard slot names a file that is gone; switch off Inherit, or press Refresh in Clipboard")
        elif view is not None:
            notes.append("nothing was stored")
        message = (view or {}).get("message") or errors.message(error.code)
        if error.code == errors.WANGP_VALIDATION_REFUSED:
            # WanGP's own check of the job at its settings said no: its
            # reason, in its words, and the popup stays open to say it.
            message = outbox.validation_refusal(error)
            notes = [outbox.VALIDATION_REFUSED_NOTE]
        refused = {"ok": False, "code": error.code, "message": message, "notes": notes}
        if view is not None:
            refused["readiness"] = view
        return refused
    config.update(intercept_inherit=use_draft)
    entry = add_history({
        "prompt": str(prompt or ""),
        "enhance": bool(job.get("enhance_requested")),
        "prewritten": written,
        "inherit": use_draft,
        "roles": kept,
        "available_roles": [role["id"] for role in caps["roles"]],
        "model": block,
        "summary": _summary(request, kept, use_draft, bool(job.get("enhance_requested"))),
        "pinned": False,
        "job_id": job["job_id"],
        "request_id": request["request_id"],
        "executor": job.get("executor", ""),
        "image": {"width": parsed["width"], "height": parsed["height"], "tab": parsed["tab"]},
    })
    server = job.get("executor") == outbox.EXECUTOR_SERVER
    if server:
        # Admission copied the picture into a pinned input of the job's own;
        # the staged original has nothing left to do. A browser-executed job
        # prepares its handoff from the staged file when it runs, so that one
        # keeps it until the staging sweep.
        discard(token)
    pending = outbox.pending_count()
    _journal(f"{'auto' if auto else 'generate'}: job {job['job_id'][:8]} from {parsed['tab'] or 'a gallery'} as {', '.join(kept)}"
             f"{'; inheriting Clipboard' if use_draft else ''}{'; enhanced' if job.get('enhance_requested') else ''}"
             f"{'; written in the editor' if written else ''}"
             f"{'; ' + str(len(dropped)) + ' role(s) no longer offered dropped' if dropped else ''}; {pending} waiting"
             f"; {outbox.flush_note(job)}")
    line = "Queued on the server." if server else "Queued for WanGP."
    if job.get("enhance"):
        line = "Queued; the prompt is being enhanced first."
    if auto:
        line = f"Auto: the picture as {config.auto_roles_text(kept)}. {line}"
    settings = outbox.settings_view(job)
    notes = [f"{pending - 1} ahead of it" if pending > 1 else "it goes next"]
    if settings["warning"]:
        # First among the notes, and in the answer for the popup to say out
        # loud: a job that will not run at what WanGP is showing.
        notes.insert(0, settings["warning"])
        _journal(f"{'auto' if auto else 'generate'}: job {job['job_id'][:8]} - not the settings on WanGP's screen "
                 f"({settings['source'] or 'read at its turn'})")
    if written:
        notes.append("the prompt written in the editor goes as it is")
    if dropped:
        notes.append(", ".join(ROLE_LABELS[role] for role in dropped) + " is not offered by the current model and was dropped")
    if job.get("narrowed"):
        notes.append(", ".join(ROLE_LABELS[FIELD_ROLES[field]] for field in job["narrowed"] if field in FIELD_ROLES)
                     + " not sent: this model takes a first and a last frame only")
    if not server:
        notes.append("keep a page with the WanGP tab open until it is queued")
    return {
        "ok": True,
        "job_id": job["job_id"],
        "instruction": {"nonce": secrets.token_hex(4), "job_id": job["job_id"],
                        "executor": job.get("executor", outbox.EXECUTOR_BROWSER), "state": job.get("state", "")},
        "status": line,
        "notes": notes,
        "settings": settings,
        "roles": kept,
        "dropped_roles": dropped,
        "auto": bool(auto),
        "history_id": entry["id"],
        "released": server,
        "history": history_view(model, inputs),
    }


# ------------------------------------------------- enhance, in the editor --
#
# The prompt pop-over's Enhance (2026-11-23): the prompt is written by the
# enhancer there and then, from the editor, and shown - not at Generate, and
# not stored as a job. Asked for: "I should be able to role an enhance right
# there and see the results, and also edit the system prompt used for
# enhancing using that pop over stacked."
#
# IT IS THE ENHANCEMENT A PRESS WOULD MAKE. The request is built exactly as
# Generate builds it - the frozen picture in the ticked roles, Clipboard's
# inputs only when Inherit is on - narrowed to what the model is sent, and
# planned by ``enhance.plan`` against the model the readiness check names.
# So the writer, the instructions and the picture the writer looks at are
# the ones a press with Enhance on would have used, and the system prompt
# the editor opens on is that set.
#
# NOTHING IS HELD OPEN. ``enhance_start`` asks for the prompt and answers
# with the writer's job id; the page then reads ``enhance_status`` - one
# short request at a time, each answered at once from the API's own record -
# until the job is over or the editor is closed, when ``enhance_cancel``
# stops it. Only ids started here are answered, so the route is not a way to
# read another caller's prompts.

#: How many editor enhancements are remembered by id, and for how long. A
#: person rolls a few; these bounds are for a page left polling by mistake.
PREVIEW_KEPT = 32
PREVIEW_TTL_SECONDS = 3600.0
PREVIEW_CANCEL_REASON = "cancelled from the Send to WanGP prompt editor"

_previews: "collections.OrderedDict[str, float]" = collections.OrderedDict()
_previews_lock = threading.Lock()


def _remember_preview(llm_id: str) -> None:
    with _previews_lock:
        now = time.monotonic()
        _previews[llm_id] = now
        _previews.move_to_end(llm_id)
        for key in [key for key, started in _previews.items() if now - started > PREVIEW_TTL_SECONDS]:
            _previews.pop(key, None)
        while len(_previews) > PREVIEW_KEPT:
            _previews.popitem(last=False)


def _known_preview(llm_id: typing.Any) -> bool:
    if not isinstance(llm_id, str) or not llm_id:
        return False
    with _previews_lock:
        started = _previews.get(llm_id)
        return started is not None and time.monotonic() - started <= PREVIEW_TTL_SECONDS


def _plan_enhancement(handoff: typing.Any, prompt: typing.Any, roles: typing.Any, inherit: typing.Any,
                      model: typing.Any = None, inputs: typing.Any = None) -> typing.Tuple[dict, dict]:
    """The plan an enhancement of this popup's request would run under, and
    the facts the editor says about it. Raises IntegrationError, with the
    readiness view when the Clipboard is blocked."""
    from .. import interop
    from . import targets

    parsed = parse_handoff(handoff)
    if parsed is None:
        raise IntegrationError(errors.REQUEST_INVALID, "not a WanGP request handoff")
    if not staged_exists(parsed["token"]):
        raise IntegrationError(errors.INTERCEPT_IMAGE_EXPIRED, "the frozen picture is gone")
    ready = targets.readiness(model)
    if not ready.get("ready"):
        raise IntegrationError(ready.get("code") or errors.INTERNAL_ERROR, str(ready.get("message") or "")[:200], readiness=ready)
    target = str(ready.get("target") or "")
    caps = capabilities(model, inputs)
    kept, _dropped = reconcile_roles(roles, caps)
    text = str(prompt or "")
    # The prompt itself does not decide the plan; a blank one only means a
    # plan asked for before anything was typed.
    request = build_request(text if text.strip() else "-", kept, parsed["token"], inherit is True)
    normalised = interop.normalize_public_request(request)
    targets.narrow(normalised, target)
    block = enhance.model_block(model)
    if not block.get("type") and (ready.get("model") or {}).get("type"):
        block = enhance.model_block(ready["model"])
    planned = enhance.plan(normalised, block, target)
    variant = planned["variant"]
    facts = {
        "variant": variant,
        "variant_label": enhance.VARIANT_LABELS.get(variant, variant),
        "prompt_set": enhance.prompt_set(variant),
        "mode": enhance.MODE_IMAGE if planned.get("has_image") else enhance.MODE_TEXT,
        "described": [enhance.SLOT_LABELS.get(slot, slot) for slot in enhance.SLOTS if slot in (planned.get("slots") or {})],
        "left_out": [ROLE_LABELS.get(FIELD_ROLES.get(field, ""), field) for field in planned.get("dropped") or []],
        "roles": kept,
        # The model the plan was made for, so the system prompt editor the
        # prompt editor stacks can say it too.
        "model": block,
    }
    return planned, facts


def _refusal(error: IntegrationError) -> dict:
    refused = {"ok": False, "code": error.code, "message": errors.message(error.code)}
    view = error.extra.get("readiness") if isinstance(error.extra.get("readiness"), dict) else None
    if view is not None:
        refused["readiness"] = view
        refused["message"] = str(view.get("message") or refused["message"])
    return refused


def enhance_plan(handoff: typing.Any, prompt: typing.Any, roles: typing.Any, inherit: typing.Any,
                 model: typing.Any = None, inputs: typing.Any = None) -> dict:
    """What Enhance in the editor would do, before it is pressed: which writer,
    under which of the six instruction sets, looking at which picture - and
    whether the LLM side can take it at all. Starts nothing."""
    try:
        _planned, facts = _plan_enhancement(handoff, prompt, roles, inherit, model, inputs)
    except IntegrationError as error:
        return _refusal(error)
    availability = enhance.availability(model)
    return dict(facts, ok=True, available=enhance.capabilities()["available"],
                availability={"state": availability.get("state", "unknown"), "text": availability.get("text", "")})


def enhance_start(handoff: typing.Any, prompt: typing.Any, roles: typing.Any, inherit: typing.Any,
                  model: typing.Any = None, inputs: typing.Any = None) -> dict:
    """Ask the writer for the prompt now, as a press would. Answers with the
    writer's job id; ``enhance_status`` reads it from there."""
    text = str(prompt or "")
    if not text.strip():
        return {"ok": False, "code": errors.ENHANCE_PROMPT_REQUIRED, "message": "Type a prompt to enhance first."}
    try:
        planned, facts = _plan_enhancement(handoff, text, roles, inherit, model, inputs)
        started = enhance.submit(text, planned)
    except IntegrationError as error:
        _journal(f"enhance in the editor refused - {error.code}")
        return _refusal(error)
    llm_id = started["llm_id"]
    _remember_preview(llm_id)
    _journal(f"enhance in the editor: llm {llm_id[:8]} as {facts['variant']}"
             f" ({', '.join(facts['described']) or 'no picture'}{'; override' if started.get('system_override') else ''})")
    return dict(facts, ok=True, llm_id=llm_id, override=bool(started.get("system_override")))


def enhance_status(llm_id: typing.Any) -> dict:
    """Where one editor enhancement is: the API's own state and stage, and the
    written prompt once it is done. Only for an id started here."""
    if not _known_preview(llm_id):
        return {"ok": False, "code": errors.REQUEST_INVALID, "message": "That enhancement was not started from this editor."}
    found = enhance.status(llm_id)
    if found is None:
        return {"ok": True, "state": "lost", "stage": "", "message": errors.message(errors.ENHANCE_LOST)}
    answer = {"ok": True, "state": found["state"], "stage": found["stage"], "position": found["position"],
              "elapsed": found["elapsed"], "image_used": enhance.SLOT_LABELS.get(found["image_used"], "")}
    if found["state"] == enhance.LLM_DONE:
        answer["prompt"] = found["prompt"]
    elif found["state"] == enhance.LLM_FAILED:
        answer["message"] = found["error"] or errors.message(errors.ENHANCE_FAILED)
    elif found["state"] == enhance.LLM_CANCELLED:
        answer["message"] = found["reason"] or errors.message(errors.ENHANCE_CANCELLED)
    return answer


def enhance_cancel(llm_id: typing.Any) -> dict:
    """Stop one editor enhancement, queued or running. Only for an id started here."""
    if not _known_preview(llm_id):
        return {"ok": False, "code": errors.REQUEST_INVALID, "message": "That enhancement was not started from this editor."}
    answer = enhance.cancel(llm_id, PREVIEW_CANCEL_REASON)
    _journal(f"enhance in the editor: llm {str(llm_id)[:8]} {'cancelled' if answer.get('ok') else 'could not be cancelled'}")
    return {"ok": True, "cancelled": answer.get("ok") is True}


def forget_previews() -> None:
    """For the checks: no editor enhancement is remembered."""
    with _previews_lock:
        _previews.clear()


__all__ = [
    "FIELD_ROLES", "HISTORY_NAME", "HISTORY_SCHEMA", "MAX_PINNED", "MAX_UNPINNED", "POPUP_PREFIX", "PREVIEW_CANCEL_REASON",
    "PREVIEW_KEPT", "PREVIEW_SIDE", "PREVIEW_TTL_SECONDS",
    "ROLES", "ROLE_FIELDS", "ROLE_FIRST", "ROLE_IDS", "ROLE_LABELS", "ROLE_LAST", "ROLE_REFERENCE",
    "STATUS_BUSY", "STATUS_IDLE", "STATUS_OFF", "STATUS_RUNNING", "STATUS_UNKNOWN",
    "add_history", "auto_refusal", "auto_roles", "build_request", "cancel", "capabilities", "delete_history", "describe", "discard",
    "enhance_cancel", "enhance_plan", "enhance_start", "enhance_status", "forget_previews",
    "generate_button", "get_history", "handoff_text", "history_view", "load_history", "parse_handoff", "pin_history",
    "preview", "recipe", "reconcile_roles", "save_prompt", "stage", "staged_exists", "submit", "trim", "wangp_status",
]
