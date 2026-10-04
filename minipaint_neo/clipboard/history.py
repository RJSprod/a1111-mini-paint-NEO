"""The composer's draft, and the recipes that were confirmed queued.

The draft is what the right-hand side of the tab holds: a prompt override,
or none, and up to three asset ids. It is persisted so a Reload UI or a
browser refresh does not throw away a composition, and it is a set of ids
and one string - never a path, never a copy of a picture.

History is a Clipboard feature, not WanGP's queue. A record means "this
Clipboard recipe was confirmed admitted to WanGP's queue at this time", and
it is a recipe, not a snapshot: when the prompt was inherited the record
says so and holds no prompt, because reading WanGP's private prompt back
merely to file it here would be taking something the user did not put in
Clipboard. The same goes for inherited images. Loading a record later
restores the recipe - inherit what was inherited, override what was
overridden - and never queues anything by itself. Deleting a record deletes
the record and nothing else.

A record stores prompt text exactly when the user typed or pasted it into
Clipboard as an override. That is product state the user asked to keep. It
still never reaches a log. When the prompt was enhanced before it was sent,
the record keeps both: the typed prompt is the recipe (Load puts it back,
and a new press enhances it again), and the prompt the writer produced is
shown beside it as what WanGP actually received.
"""

from __future__ import annotations

import datetime
import secrets
import typing

from ..wangp import protocol
from . import config

DRAFT_KEYS = ("prompt_override", "first_asset_id", "last_asset_id", "reference_asset_ids")

#: How many records are kept. Newest first; the oldest go.
MAX_HISTORY = 200

MODE_OVERRIDE = "override"
MODE_INHERIT = "inherit"
MODE_IGNORED = "ignored"


def _asset_id(value: typing.Any) -> str:
    return value if protocol.valid_handoff_id(value) else ""


def _asset_ids(value: typing.Any, limit: int = protocol.MAX_QUEUE_REFERENCES) -> typing.List[str]:
    if not isinstance(value, (list, tuple)):
        return []
    kept = [item for item in value if protocol.valid_handoff_id(item)]
    return kept[:limit]


# ------------------------------------------------------------------ draft --


def empty_draft() -> dict:
    return {"prompt_override": "", "first_asset_id": "", "last_asset_id": "", "reference_asset_ids": []}


def normalize_draft(raw: typing.Any) -> dict:
    """A draft as the tab holds it: text, three ids, nothing else."""
    raw = raw if isinstance(raw, dict) else {}
    prompt = raw.get("prompt_override")
    return {
        "prompt_override": prompt if isinstance(prompt, str) else "",
        "first_asset_id": _asset_id(raw.get("first_asset_id")),
        "last_asset_id": _asset_id(raw.get("last_asset_id")),
        "reference_asset_ids": _asset_ids(raw.get("reference_asset_ids")),
    }


def load_draft() -> dict:
    return normalize_draft(config.read_document(config.DRAFT_NAME, {}))


def save_draft(draft: typing.Any) -> dict:
    cleaned = normalize_draft(draft)
    config.write_document(config.DRAFT_NAME, cleaned)
    return cleaned


def draft_overrides(draft: typing.Mapping[str, typing.Any]) -> typing.List[str]:
    """Which fields a draft supplies, in the contract's order."""
    draft = normalize_draft(draft)
    supplied = []
    if protocol.clean_prompt(draft["prompt_override"]) is not None:
        supplied.append(protocol.QUEUE_FIELD_PROMPT)
    if draft["first_asset_id"]:
        supplied.append(protocol.QUEUE_FIELD_START)
    if draft["last_asset_id"]:
        supplied.append(protocol.QUEUE_FIELD_END)
    if draft["reference_asset_ids"]:
        supplied.append(protocol.QUEUE_FIELD_REFERENCES)
    return supplied


def public_request(draft: typing.Mapping[str, typing.Any], request_id: str = "") -> dict:
    """The draft as a ``minipaint.wangp.queue/v1`` request: inherited fields omitted."""
    draft = normalize_draft(draft)
    request: typing.Dict[str, typing.Any] = {"request_id": request_id or secrets.token_hex(16), "images": {}}
    prompt = protocol.clean_prompt(draft["prompt_override"])
    if prompt is not None:
        request["prompt"] = prompt
    if draft["first_asset_id"]:
        request["images"]["start"] = {"kind": "clipboard_asset", "id": draft["first_asset_id"]}
    if draft["last_asset_id"]:
        request["images"]["end"] = {"kind": "clipboard_asset", "id": draft["last_asset_id"]}
    if draft["reference_asset_ids"]:
        request["images"]["references"] = [{"kind": "clipboard_asset", "id": item} for item in draft["reference_asset_ids"]]
    return request


# ---------------------------------------------------------------- history --


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def normalize_record(raw: typing.Any) -> typing.Optional[dict]:
    raw = raw if isinstance(raw, dict) else None
    if raw is None:
        return None
    history_id = raw.get("history_id")
    if not isinstance(history_id, str) or not history_id:
        return None
    modes = {MODE_OVERRIDE, MODE_INHERIT, MODE_IGNORED}

    def mode(value: typing.Any) -> str:
        return value if value in modes else MODE_INHERIT

    prompt_mode = mode(raw.get("prompt_mode"))
    prompt = raw.get("prompt_override") if prompt_mode == MODE_OVERRIDE and isinstance(raw.get("prompt_override"), str) else ""
    ignored = []
    for item in raw.get("ignored") if isinstance(raw.get("ignored"), list) else []:
        if isinstance(item, dict) and item.get("field") in protocol.QUEUE_FIELDS:
            ignored.append({"field": item["field"], "code": str(item.get("code") or "")[:60]})
    tasks = raw.get("tasks_added")
    enhanced = raw.get("enhanced") is True
    written = raw.get("enhanced_prompt") if enhanced and isinstance(raw.get("enhanced_prompt"), str) else ""
    return {
        "history_id": history_id[:32],
        "request_id": _asset_id(raw.get("request_id")),
        "admitted_at": str(raw.get("admitted_at") or "")[:40],
        "model_type": str(raw.get("model_type") or "")[:120],
        "model_label": str(raw.get("model_label") or "")[:120],
        "prompt_mode": prompt_mode,
        "prompt_override": prompt,
        "enhanced": enhanced,
        "enhanced_prompt": written[:protocol.PROMPT_MAX_CHARS],
        "first_mode": mode(raw.get("first_mode")),
        "first_asset_id": _asset_id(raw.get("first_asset_id")),
        "last_mode": mode(raw.get("last_mode")),
        "last_asset_id": _asset_id(raw.get("last_asset_id")),
        "reference_mode": mode(raw.get("reference_mode")),
        "reference_asset_ids": _asset_ids(raw.get("reference_asset_ids")),
        "tasks_added": int(tasks) if isinstance(tasks, (int, float)) and not isinstance(tasks, bool) and tasks > 0 else 0,
        "ignored": ignored,
        # A key from a closed list, so a hand-edited file cannot put words of
        # its own on the screen; anything else is "not recorded".
        "settings": raw.get("settings") if raw.get("settings") in _settings_keys() else "",
    }


def _settings_keys() -> typing.Tuple[str, ...]:
    from . import outbox

    return outbox.SETTINGS_KEYS


def load_history() -> typing.List[dict]:
    """Every record, newest first."""
    raw = config.read_document(config.HISTORY_NAME, [])
    records = [normalize_record(item) for item in raw]
    return [record for record in records if record is not None][:MAX_HISTORY]


def _save(records: typing.List[dict]) -> None:
    config.write_document(config.HISTORY_NAME, records[:MAX_HISTORY])


def make_record(draft: typing.Mapping[str, typing.Any], result: typing.Mapping[str, typing.Any], enhanced_prompt: str = "",
                settings: str = "") -> dict:
    """A history record from the draft that was sent and the confirmed result.

    Each field's mode comes from the result: applied means override,
    reported ignored means ignored, anything else - including a slot that
    was empty - means inherit. The prompt text is stored only when it was an
    override. ``enhanced_prompt`` is what the writer made of it, when the
    press was enhanced; the draft still holds the typed prompt. ``settings``
    is where the job's WanGP settings came from, as one of the outbox's
    ``SETTINGS_*`` keys.
    """
    draft = normalize_draft(draft)
    applied = result.get("applied") if isinstance(result.get("applied"), dict) else {}
    ignored = [item for item in (result.get("ignored") or []) if isinstance(item, dict) and item.get("field") in protocol.QUEUE_FIELDS]
    ignored_fields = {item["field"] for item in ignored}
    model = result.get("model") if isinstance(result.get("model"), dict) else {}

    def mode_of(field: str, supplied: bool) -> str:
        if field in ignored_fields:
            return MODE_IGNORED
        if applied.get(field) and supplied:
            return MODE_OVERRIDE
        return MODE_INHERIT

    prompt_supplied = protocol.clean_prompt(draft["prompt_override"]) is not None
    prompt_mode = mode_of(protocol.QUEUE_FIELD_PROMPT, prompt_supplied)
    record = {
        "history_id": secrets.token_hex(8),
        "request_id": _asset_id(result.get("request_id")),
        "admitted_at": _now_iso(),
        "model_type": str(model.get("type") or "")[:120],
        "model_label": str(model.get("label") or "")[:120],
        "prompt_mode": prompt_mode,
        "prompt_override": draft["prompt_override"] if prompt_mode == MODE_OVERRIDE else "",
        "enhanced": bool(enhanced_prompt) and prompt_mode == MODE_OVERRIDE,
        "enhanced_prompt": str(enhanced_prompt or "") if prompt_mode == MODE_OVERRIDE else "",
        "first_mode": mode_of(protocol.QUEUE_FIELD_START, bool(draft["first_asset_id"])),
        "first_asset_id": draft["first_asset_id"] if draft["first_asset_id"] else "",
        "last_mode": mode_of(protocol.QUEUE_FIELD_END, bool(draft["last_asset_id"])),
        "last_asset_id": draft["last_asset_id"] if draft["last_asset_id"] else "",
        "reference_mode": mode_of(protocol.QUEUE_FIELD_REFERENCES, bool(draft["reference_asset_ids"])),
        "reference_asset_ids": list(draft["reference_asset_ids"]),
        "tasks_added": int(result.get("tasks_added") or 0) if isinstance(result.get("tasks_added"), (int, float)) else 0,
        "ignored": [{"field": item["field"], "code": str(item.get("code") or "")[:60]} for item in ignored],
        "settings": settings,
    }
    return normalize_record(record) or record


def add_history(record: dict) -> dict:
    records = load_history()
    cleaned = normalize_record(record)
    if cleaned is None:
        raise ValueError("not a history record")
    records.insert(0, cleaned)
    _save(records)
    return cleaned


def delete_history(history_id: typing.Any) -> bool:
    """Remove one record. The pictures it names are not touched."""
    records = load_history()
    kept = [record for record in records if record["history_id"] != history_id]
    if len(kept) == len(records):
        return False
    _save(kept)
    return True


def draft_from_record(record: typing.Mapping[str, typing.Any], available: typing.Callable[[str], bool]) -> typing.Tuple[dict, typing.List[str]]:
    """The recipe a record describes, as a draft, and which slots it could not fill.

    An override whose asset is gone becomes inherit and is named in the
    second value, so the tab can say "Missing image" rather than pretend.
    """
    record = normalize_record(record) or {}
    missing: typing.List[str] = []
    draft = empty_draft()
    if record.get("prompt_mode") == MODE_OVERRIDE:
        draft["prompt_override"] = record.get("prompt_override", "")
    for key, field, mode_key in (("first_asset_id", "first", "first_mode"), ("last_asset_id", "last", "last_mode")):
        asset_id = record.get(key, "")
        if record.get(mode_key) in (MODE_OVERRIDE, MODE_IGNORED) and asset_id:
            if available(asset_id):
                draft[key] = asset_id
            else:
                missing.append(field)
    if record.get("reference_mode") in (MODE_OVERRIDE, MODE_IGNORED):
        kept = [item for item in record.get("reference_asset_ids", []) if available(item)]
        if len(kept) < len(record.get("reference_asset_ids", [])):
            missing.append("reference")
        draft["reference_asset_ids"] = kept
    return draft, missing


# ---------------------------------------------------------------- recipes --
#
# What View Outputs' Load restores, kept on the output itself.
#
# A history record is made when a page next asks for the queue after its job
# was confirmed, and only for the composer's own jobs; and a finished job is
# swept from the queue two minutes after it finishes. So a long unattended run
# - an LTX video the server generated while nobody had the tab open - could
# finish, be swept and never be recorded, and its video in View Outputs had
# nothing to load; nor did any video the gallery popup asked for, or one whose
# record had aged out of the two hundred. The output ledger now keeps the
# recipe of the request that made it, taken from the job while the job is
# still there (``outputs.sync`` and the executor's ``outputs.remember``).
# A recipe is the request as it was typed: the prompt the user wrote - never
# the one an enhancement wrote, which is kept beside it as ``written`` - and
# the library pictures by id. A picture that was not a library picture (the
# gallery's frozen picture, another extension's upload) is named as
# ``transient``: it is gone by the time anybody loads the recipe.

_FIELD_KEYS = (
    (protocol.QUEUE_FIELD_START, "first_asset_id"),
    (protocol.QUEUE_FIELD_END, "last_asset_id"),
    (protocol.QUEUE_FIELD_REFERENCES, "reference_asset_ids"),
)


def normalize_recipe(raw: typing.Any) -> typing.Optional[dict]:
    """A recipe as an output keeps it, or None for anything that is not one."""
    if not isinstance(raw, dict):
        return None
    prompt = raw.get("prompt") if isinstance(raw.get("prompt"), str) else ""
    written = raw.get("written") if isinstance(raw.get("written"), str) else ""
    transient = [field for field in (raw.get("transient") or []) if field in protocol.QUEUE_FIELDS] \
        if isinstance(raw.get("transient"), (list, tuple)) else []
    return {
        "prompt": prompt[:protocol.PROMPT_MAX_CHARS],
        "written": written[:protocol.PROMPT_MAX_CHARS],
        "first_asset_id": _asset_id(raw.get("first_asset_id")),
        "last_asset_id": _asset_id(raw.get("last_asset_id")),
        "reference_asset_ids": _asset_ids(raw.get("reference_asset_ids")),
        "transient": transient,
        "model_type": str(raw.get("model_type") or "")[:120],
        "model_label": str(raw.get("model_label") or "")[:120],
        "origin": str(raw.get("origin") or "")[:16],
    }


def recipe_of_job(job: typing.Mapping[str, typing.Any]) -> typing.Optional[dict]:
    """The recipe a queued job was made from, as an output keeps it.

    ``job`` is the outbox's view of one job. The prompt is the one that was
    typed: for an enhanced job that is the enhancement's ``prompt_original``,
    because by the time WanGP has the job its request carries the written
    prompt instead, which is kept as ``written``. None for anything that is
    not a job.
    """
    if not isinstance(job, typing.Mapping) or not isinstance(job.get("request"), typing.Mapping):
        return None
    request = job["request"]
    record = job.get("enhance") if isinstance(job.get("enhance"), typing.Mapping) else {}
    asked = request.get("prompt") if isinstance(request.get("prompt"), str) else ""
    finished = record.get("state") == "done" and isinstance(record.get("prompt_original"), str) and record.get("prompt_original")
    images = request.get("images") if isinstance(request.get("images"), typing.Mapping) else {}
    recipe: typing.Dict[str, typing.Any] = {
        "prompt": record["prompt_original"] if finished else asked,
        "written": asked if finished else "",
        "first_asset_id": "", "last_asset_id": "", "reference_asset_ids": [], "transient": [],
        "model_type": str((job.get("model") or {}).get("type") or ""),
        "model_label": str((job.get("model") or {}).get("label") or ""),
        "origin": str(job.get("origin") or ""),
    }

    def library_id(handle: typing.Any) -> str:
        if isinstance(handle, typing.Mapping) and handle.get("kind") == "clipboard_asset":
            return _asset_id(handle.get("id"))
        return ""

    for field, key in _FIELD_KEYS:
        value = images.get(field)
        if not value:
            continue
        handles = list(value) if field == protocol.QUEUE_FIELD_REFERENCES and isinstance(value, (list, tuple)) else [value]
        ids = [library_id(handle) for handle in handles]
        kept = [item for item in ids if item]
        if len(kept) < len(handles):
            recipe["transient"].append(field)
        if field == protocol.QUEUE_FIELD_REFERENCES:
            recipe[key] = kept
        elif kept:
            recipe[key] = kept[0]
    return normalize_recipe(recipe)


def draft_from_recipe(recipe: typing.Mapping[str, typing.Any], available: typing.Callable[[str], bool]) -> typing.Tuple[dict, typing.List[str]]:
    """The composer's draft a recipe describes, and the slots it could not fill.

    The rule ``draft_from_record`` keeps: what was inherited is inherited
    again, a picture still in the library is put back, and one that is not -
    deleted since, or never a library picture - leaves its slot on Use WanGP
    and is named in the second value (``first``, ``last``, ``reference``).
    """
    recipe = normalize_recipe(recipe) or {}
    missing: typing.List[str] = []
    draft = empty_draft()
    draft["prompt_override"] = recipe.get("prompt", "")
    for key, field, name in (("first_asset_id", protocol.QUEUE_FIELD_START, "first"),
                             ("last_asset_id", protocol.QUEUE_FIELD_END, "last")):
        asset_id = recipe.get(key, "")
        if asset_id and available(asset_id):
            draft[key] = asset_id
        elif asset_id or field in recipe.get("transient", []):
            missing.append(name)
    references = recipe.get("reference_asset_ids", [])
    kept = [item for item in references if available(item)]
    draft["reference_asset_ids"] = kept
    if len(kept) < len(references) or protocol.QUEUE_FIELD_REFERENCES in recipe.get("transient", []):
        missing.append("reference")
    return draft, missing


__all__ = [
    "DRAFT_KEYS",
    "MAX_HISTORY",
    "MODE_IGNORED",
    "MODE_INHERIT",
    "MODE_OVERRIDE",
    "add_history",
    "delete_history",
    "draft_from_recipe",
    "draft_from_record",
    "draft_overrides",
    "empty_draft",
    "load_draft",
    "load_history",
    "make_record",
    "normalize_draft",
    "normalize_recipe",
    "normalize_record",
    "public_request",
    "recipe_of_job",
    "save_draft",
]
