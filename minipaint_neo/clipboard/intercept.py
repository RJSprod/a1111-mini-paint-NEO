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
the live page's own input support, as the bridge reports it. The *default*
role is the one thing that leans on a model family, and it leans on the
mapping ``enhance.SLOTS_FOR`` already holds rather than on a name spelled
here. Nothing in the popup, and nothing in this file's public answers, says
"MiniMax", "FL2VA" or "REF2VA": a future model family is a change to that
mapping and to nothing in the intercept path.

Nothing here logs a prompt, a filename or a path. The journal sees roles,
counts and the first eight characters of an id.
"""

from __future__ import annotations

import datetime
import io
import re
import secrets
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
    block = enhance.model_block(model)
    support = _support(inputs)
    known = support is not None
    available = [role for role in ROLE_IDS if not known or support.get(ROLE_FIELDS[role], False)]
    variant = enhance.variant_for_model(block)
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
    unattended = outbox.chosen_executor() == outbox.EXECUTOR_SERVER
    if not running:
        text = "WanGP is not running" + ("; Generate queues on the server, which starts it" if unattended else "")
        return {"state": STATUS_OFF, "running": False, "generating": None, "text": text}
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


def generate_button() -> dict:
    """Whether Generate is a button: the same decision Add to Queue makes."""
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
    enabled = enhance.set_enabled(entry["enhance"])
    config.update(intercept_inherit=entry["inherit"])
    _journal(f"history {entry['id'][:8]} loaded: {len(kept)} role(s) kept, {len(dropped)} dropped")
    return {
        "ok": True,
        "id": entry["id"],
        "prompt": entry["prompt"],
        "enhance": enabled,
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


def describe(handoff: typing.Any, model: typing.Any = None, inputs: typing.Any = None) -> dict:
    """Everything the popup draws, in one answer."""
    parsed = parse_handoff(handoff)
    if parsed is None:
        return {"ok": False, "code": errors.REQUEST_INVALID, "message": "That is not a WanGP request handoff."}
    if not staged_exists(parsed["token"]):
        return {"ok": False, "code": errors.INTERCEPT_IMAGE_EXPIRED, "message": errors.message(errors.INTERCEPT_IMAGE_EXPIRED)}
    draft = history.load_draft()
    current = config.load()
    availability = enhance.availability(model)
    return {
        "ok": True,
        "token": parsed["token"],
        "image": {"width": parsed["width"], "height": parsed["height"], "tab": parsed["tab"]},
        "capabilities": capabilities(model, inputs),
        "prompt": draft["prompt_override"],
        "enhance": {"enabled": enhance.enabled(), "state": availability.get("state", "unknown"),
                    "text": availability.get("text", "")},
        "inherit": {"default": bool(current.intercept_inherit), "supported": True, "draft": _draft_images_summary(draft)},
        "wangp": wangp_status(),
        "generate": generate_button(),
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
    """
    parsed = parse_handoff(handoff)
    if parsed is None:
        return {"ok": False, "code": errors.REQUEST_INVALID, "message": "That is not a WanGP request handoff."}
    token = parsed["token"]
    if not staged_exists(token):
        return {"ok": False, "code": errors.INTERCEPT_IMAGE_EXPIRED, "message": errors.message(errors.INTERCEPT_IMAGE_EXPIRED)}
    caps = capabilities(model, inputs)
    kept, dropped = reconcile_roles(roles, caps)
    if not kept:
        return {"ok": False, "code": errors.INTERCEPT_NO_IMAGE_ROLE, "message": errors.message(errors.INTERCEPT_NO_IMAGE_ROLE)}
    use_draft = inherit is not False
    wanted = None if enhance_wanted is None else bool(enhance_wanted)
    block = enhance.model_block(model)
    # The shared prompt is Clipboard's the moment Generate is pressed - the
    # visible value, not whatever a blur may or may not have saved.
    save_prompt(prompt)
    try:
        request = build_request(prompt, kept, token, use_draft)
        if wanted is not None and wanted != enhance.enabled():
            # The switch on screen and the setting on disk had drifted, as
            # they can when a change event is lost; the press is the answer.
            enhance.set_enabled(wanted)
        job = outbox.submit(request, page, outbox.ORIGIN_GALLERY, model=block, enhance=wanted,
                            settings_flush=str(settings_flush or ""))
    except IntegrationError as error:
        _journal(f"generate refused before storing - {error.code}")
        notes = []
        if error.code == errors.WANGP_NOT_RUNNING:
            notes.append("nothing was stored; open the WanGP tab and let it start")
        elif error.code.startswith("ENHANCE_"):
            notes.append("nothing was stored; switch Enhance off to send the prompt as typed")
        elif error.code == errors.CLIPBOARD_ASSET_UNKNOWN:
            notes.append("a Clipboard slot names a file that is gone; switch off Inherit, or press Refresh in Clipboard")
        return {"ok": False, "code": error.code, "message": errors.message(error.code), "notes": notes}
    config.update(intercept_inherit=use_draft)
    entry = add_history({
        "prompt": str(prompt or ""),
        "enhance": bool(job.get("enhance_requested")),
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
    _journal(f"generate: job {job['job_id'][:8]} from {parsed['tab'] or 'a gallery'} as {', '.join(kept)}"
             f"{'; inheriting Clipboard' if use_draft else ''}{'; enhanced' if job.get('enhance_requested') else ''}"
             f"{'; ' + str(len(dropped)) + ' role(s) no longer offered dropped' if dropped else ''}; {pending} waiting"
             f"; {outbox.flush_note(job)}")
    line = "Queued on the server." if server else "Queued for WanGP."
    if job.get("enhance"):
        line = "Queued; the prompt is being enhanced first."
    notes = [f"{pending - 1} ahead of it" if pending > 1 else "it goes next"]
    if dropped:
        notes.append(", ".join(ROLE_LABELS[role] for role in dropped) + " is not offered by the current model and was dropped")
    if not server:
        notes.append("keep a page with the WanGP tab open until it is queued")
    return {
        "ok": True,
        "job_id": job["job_id"],
        "instruction": {"nonce": secrets.token_hex(4), "job_id": job["job_id"],
                        "executor": job.get("executor", outbox.EXECUTOR_BROWSER), "state": job.get("state", "")},
        "status": line,
        "notes": notes,
        "roles": kept,
        "dropped_roles": dropped,
        "history_id": entry["id"],
        "released": server,
        "history": history_view(model, inputs),
    }


__all__ = [
    "FIELD_ROLES", "HISTORY_NAME", "HISTORY_SCHEMA", "MAX_PINNED", "MAX_UNPINNED", "POPUP_PREFIX", "PREVIEW_SIDE",
    "ROLES", "ROLE_FIELDS", "ROLE_FIRST", "ROLE_IDS", "ROLE_LABELS", "ROLE_LAST", "ROLE_REFERENCE",
    "STATUS_BUSY", "STATUS_IDLE", "STATUS_OFF", "STATUS_RUNNING", "STATUS_UNKNOWN",
    "add_history", "build_request", "cancel", "capabilities", "delete_history", "describe", "discard",
    "generate_button", "get_history", "handoff_text", "history_view", "load_history", "parse_handoff", "pin_history",
    "preview", "recipe", "reconcile_roles", "save_prompt", "stage", "staged_exists", "submit", "trim", "wangp_status",
]
