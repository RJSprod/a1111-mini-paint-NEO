"""The bridge's copy of the shared vocabulary.

This plugin is installed into someone else's Python environment - WanGP's -
and may import nothing from the Mini Paint extension, so the one thing both
halves must agree on word for word is duplicated here instead of shared. The
block below is copied verbatim from ``minipaint_neo/wangp/protocol.py``;
``tests/test_wangp_protocol.py`` holds the two files to each other, so a fix
to one that is not made to the other is a test failure rather than a
mysterious protocol drift six months later. Do not reflow it, reorder it or
tidy it up: change the Forge-side file, then copy it here again.
"""

from __future__ import annotations

import hashlib
import json
import re
import typing
import unicodedata

# ---------------------------------------------------------------- SHARED --
# Everything between this marker and END SHARED is duplicated verbatim in
# wan2gp_bridge/wan2gp-minipaint-bridge/protocol.py. Change one, change both;
# the test fails otherwise.

PROTOCOL = 5

#: postMessage types. Anything not in here is dropped without a reply.
HELLO = "WANGP_BRIDGE_HELLO"
READY = "WANGP_BRIDGE_READY"
GET_RECEIVERS = "WANGP_GET_RECEIVERS"
RECEIVERS = "WANGP_RECEIVERS"
RECEIVE_IMAGE = "WANGP_RECEIVE_IMAGE"
RECEIVE_RESULT = "WANGP_RECEIVE_RESULT"
FOCUS_RECEIVER = "WANGP_FOCUS_RECEIVER"
THEME_STATE = "WANGP_THEME_STATE"
RUNTIME_STATE = "WANGP_RUNTIME_STATE"
#: Protocol 3: add the live page to WanGP's queue, and ask whether it landed.
QUEUE_REQUEST = "WANGP_QUEUE_REQUEST"
QUEUE_RESULT = "WANGP_QUEUE_RESULT"
QUEUE_CONFIRM = "WANGP_QUEUE_CONFIRM"
QUEUE_STATUS = "WANGP_QUEUE_STATUS"
#: Protocol 5: where the tasks this page admitted are in WanGP's queue now.
QUEUE_TRACK = "WANGP_QUEUE_TRACK"
QUEUE_TRACKED = "WANGP_QUEUE_TRACKED"

#: What the parent page may send into the iframe.
TO_BRIDGE = frozenset({HELLO, GET_RECEIVERS, RECEIVE_IMAGE, FOCUS_RECEIVER, THEME_STATE, QUEUE_REQUEST, QUEUE_CONFIRM, QUEUE_TRACK})
#: What the iframe may send out to the parent page.
TO_PARENT = frozenset({READY, RECEIVERS, RECEIVE_RESULT, RUNTIME_STATE, QUEUE_RESULT, QUEUE_STATUS, QUEUE_TRACKED})

#: Stable logical receiver ids. The bridge maps these onto whatever the
#: installed WanGP calls them; MiniPaint only ever sees these.
START_FRAME = "start_frame"
END_FRAME = "end_frame"
REFERENCE = "reference"
CONTROL_IMAGE = "control_image"
POSITIONED_REF = "positioned_ref"
STYLE_REF = "style_ref"

RECEIVER_IDS = (START_FRAME, END_FRAME, REFERENCE, CONTROL_IMAGE, POSITIONED_REF, STYLE_REF)

#: The roles a receiver can play. Roles group receivers for the menu; ids
#: identify them.
ROLES = ("start", "end", "reference", "control", "positioned", "style")

#: How an image joins what is already in a receiver. The bridge decides this
#: per receiver, per state - MiniPaint never infers it from the role.
REPLACE = "replace"
APPEND = "append"
OPERATIONS = (REPLACE, APPEND)

#: The menu label for each receiver, when the bridge does not supply one.
DEFAULT_MENU_LABELS = {
    START_FRAME: "Send Image to WanGP Start Frame",
    END_FRAME: "Send Image to WanGP End Frame",
    REFERENCE: "Send Image to WanGP Reference",
    CONTROL_IMAGE: "Send Image to WanGP Control Image",
    POSITIONED_REF: "Send Image to WanGP Positioned Reference",
    STYLE_REF: "Send Image to WanGP Style Reference",
}

#: A handoff id is 32 lowercase hex characters and nothing else. No dot, no
#: separator, no traversal - the id is not sanitised, it is required to be
#: exactly this and rejected otherwise.
HANDOFF_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")
HANDOFF_SUFFIX = ".png"

#: What a send will switch on in WanGP for a receiver the model allows but
#: the page has not selected: a short token from the bridge, or nothing.
SWITCH_RE = re.compile(r"\A[a-z_]{1,40}\Z")

#: Ceilings for one handoff. Generous for a high-resolution edit, finite.
MAX_HANDOFF_BYTES = 64 * 1024 * 1024
MAX_HANDOFF_PIXELS = 64 * 1024 * 1024
MAX_HANDOFF_SIDE = 16384

#: A postMessage envelope this size or larger is dropped unread. A queue
#: request is a prompt of at most PROMPT_MAX_CHARS characters - under 72 KiB
#: of JSON however it is escaped - plus a handful of 32-character ids, so the
#: ceiling that was already here holds it three times over and is not raised.
#: Image bytes never cross postMessage.
MAX_ENVELOPE_BYTES = 256 * 1024

#: How long the parent waits, in milliseconds. The receiver query is one
#: Gradio round trip inside WanGP; on a machine that is loading a model or
#: pinning LoRAs at that moment, four seconds was not enough and read as
#: "unavailable".
RECEIVER_QUERY_TIMEOUT_MS = 10000
RECEIVE_TIMEOUT_MS = 30000

#: How the state fingerprint is shortened for transport.
REVISION_LENGTH = 32

# -- the queue: minipaint.wangp.queue/v1 -----------------------------------

#: The public contract the queue operation carries, by name, so a caller can
#: ask for it and a log can say which one answered.
QUEUE_CONTRACT = "minipaint.wangp.queue/v1"
PUBLIC_API_VERSION = 1

#: A queue request id is the same shape as a handoff id, for the same
#: reason: a fixed grammar cannot be talked into meaning something else. It
#: is also the client_id WanGP's own queue tasks carry, which is how an
#: admission is proved.
REQUEST_ID_RE = HANDOFF_ID_RE

#: The four things a queue request may override. Anything else on the live
#: page - model, resolution, length, steps, seed, guidance, LoRAs - is always
#: the page's own.
QUEUE_FIELD_PROMPT = "prompt"
QUEUE_FIELD_START = "start"
QUEUE_FIELD_END = "end"
QUEUE_FIELD_REFERENCES = "references"
QUEUE_FIELDS = (QUEUE_FIELD_PROMPT, QUEUE_FIELD_START, QUEUE_FIELD_END, QUEUE_FIELD_REFERENCES)

#: Which logical receiver each image field of a request addresses.
QUEUE_FIELD_RECEIVERS = {
    QUEUE_FIELD_START: START_FRAME,
    QUEUE_FIELD_END: END_FRAME,
    QUEUE_FIELD_REFERENCES: REFERENCE,
}

#: Prompt ceiling, in characters. Longer is refused, never truncated: a
#: prompt cut short is a different prompt, and nobody asked for that one.
#: Protocol 5 raised it from 4000: a MiniMax H3 prompt written by an
#: enhancer is several sections long, and a Ref2VA one runs to two thousand
#: tokens. The postMessage ceiling below still holds it many times over.
PROMPT_MAX_CHARS = 12000

#: How many reference images one request may supply. A bound rather than a
#: model fact; the live page decides what the model takes.
MAX_QUEUE_REFERENCES = 16

#: What the bridge answers the moment the request event ran. "requested" and
#: "duplicate" mean the admission attempt was made (or had already been made
#: for this id); neither means WanGP's queue holds it yet - only a confirmed
#: status says that.
ADMISSION_REQUESTED = "requested"
ADMISSION_DUPLICATE = "duplicate"
ADMISSION_REFUSED = "refused"
ADMISSIONS = (ADMISSION_REQUESTED, ADMISSION_DUPLICATE, ADMISSION_REFUSED)

#: What a confirmation answers. "expired" is a pending admission that ran out
#: of time without proof either way; it is never reported as a refusal.
QUEUE_PENDING = "pending"
QUEUE_QUEUED = "queued"
#: Protocol 4: the request's task is the one WanGP is generating now. A
#: positive observation, like "queued", and as sticky.
QUEUE_STARTED = "started"
QUEUE_REFUSED = "refused"
QUEUE_EXPIRED = "expired"
QUEUE_STATUSES = (QUEUE_PENDING, QUEUE_QUEUED, QUEUE_STARTED, QUEUE_REFUSED, QUEUE_EXPIRED)
QUEUE_POSITIVE = (QUEUE_QUEUED, QUEUE_STARTED)

#: Protocol 4: whether a request may start a generation. "auto" (the default
#: when a caller says nothing) means "generating as soon as WanGP can" -
#: starting a run when WanGP is idle, joining the running one otherwise;
#: "never" stages the task only. The decision is the bridge's, made inside
#: WanGP's process from WanGP's own process-wide flag, never the caller's
#: and never a page's guess.
START_AUTO = "auto"
START_NEVER = "never"
START_MODES = (START_AUTO, START_NEVER)
#: What the answer says the bridge did about starting: the mode it honoured,
#: or "unknown" when WanGP's flag could not be read and the request was
#: staged rather than started - the fail-safe direction.
START_UNKNOWN = "unknown"
START_ANSWERS = (START_AUTO, START_NEVER, START_UNKNOWN)
#: Which of WanGP's own triggers the bridge wrote to commit the task.
ROUTE_GENERATE = "generate"
ROUTE_QUEUE = "queue"
ROUTES = (ROUTE_GENERATE, ROUTE_QUEUE)

#: Protocol 5: what a track answer says about one request's task. "waiting"
#: and "generating" are read from WanGP's own queue for this page; "finished"
#: means a task this page once saw queued is no longer there - finished, or
#: removed in WanGP, which the bridge cannot tell apart; "unknown" means
#: this page never admitted it (a reloaded page is a new session and cannot
#: vouch for the old one's tasks). "finished" is never said of a request the
#: page has no admission record for.
TRACK_WAITING = "waiting"
TRACK_GENERATING = "generating"
TRACK_FINISHED = "finished"
TRACK_UNKNOWN = "unknown"
TRACK_STATES = (TRACK_WAITING, TRACK_GENERATING, TRACK_FINISHED, TRACK_UNKNOWN)
#: How many requests one track message may ask about.
MAX_TRACKED_REQUESTS = 32
#: A WanGP model type, when a request insists on the one it was composed
#: for: an enhanced prompt is written for one H3 model, and the bridge
#: refuses with MODEL_CHANGED rather than send it to another.
MODEL_TYPE_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:+-]{0,119}\Z")

#: Timeouts for the two queue round trips, and the bounded confirmation
#: schedule: one confirmation at once, then a short sequence, then nothing.
#: The parent never installs a permanent timer and never confirms past the
#: end of the sequence.
QUEUE_REQUEST_TIMEOUT_MS = 30000
QUEUE_CONFIRM_TIMEOUT_MS = 10000
QUEUE_TRACK_TIMEOUT_MS = 10000
QUEUE_CONFIRM_DELAYS_MS = (0, 100, 300, 700, 1500, 3000)

#: How long the bridge keeps an unconfirmed admission as the owner of the
#: live form before it is expired and the overrides put back. Long enough
#: for a busy WanGP to run its own queue chain; short enough that a request
#: nobody could confirm does not lock the page for a coffee break.
PENDING_ADMISSION_SECONDS = 20.0
#: How long a terminal record is kept, so a retried request id is recognised.
ADMISSION_RECORD_SECONDS = 600.0
MAX_ADMISSION_RECORDS = 512

#: The failure codes the queue contract can name from either side. Spelled
#: here as well as in errors.py / compatibility.py so the normalisers, which
#: both copies share, can refuse with them.
QUEUE_CODE_REQUEST_INVALID = "REQUEST_INVALID"
QUEUE_CODE_PROMPT_TOO_LONG = "PROMPT_TOO_LONG"
QUEUE_CODE_REQUEST_ID_CONFLICT = "REQUEST_ID_CONFLICT"
QUEUE_CODE_BUSY = "QUEUE_BUSY"
QUEUE_CODE_REFUSED = "QUEUE_REQUEST_REFUSED"
QUEUE_CODE_UNCONFIRMED = "ADMISSION_UNCONFIRMED"
QUEUE_CODE_VALIDATION_REFUSED = "WANGP_VALIDATION_REFUSED"
QUEUE_CODE_MODEL_CHANGED = "MODEL_CHANGED"

#: The shape of a failure code, so a bridge cannot hand the menu a sentence.
CODE_RE = re.compile(r"\A[A-Z][A-Z0-9_]{2,59}\Z")


def valid_handoff_id(value: typing.Any) -> bool:
    return isinstance(value, str) and bool(HANDOFF_ID_RE.match(value))


def valid_request_id(value: typing.Any) -> bool:
    return isinstance(value, str) and bool(REQUEST_ID_RE.match(value))


def canonical_json(value: typing.Any) -> str:
    """One byte-for-byte answer for one state, on either side of the bridge."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=True)


def state_revision(state: typing.Any) -> str:
    """A fingerprint of the receiver-affecting live state.

    A counter would miss a control nobody registered a callback for; a
    digest of the state itself cannot. The bridge sends this with every
    receiver list and recomputes it before it applies an image: if a single
    normalised value moved, the send is refused rather than redirected.
    """
    digest = hashlib.sha256(canonical_json(state).encode("utf-8")).hexdigest()
    return digest[:REVISION_LENGTH]


def envelope(kind: str, channel_id: str, request_id: str, payload: typing.Any = None) -> dict:
    return {
        "protocol": PROTOCOL,
        "type": kind,
        "channel_id": str(channel_id or ""),
        "request_id": str(request_id or ""),
        "payload": payload if payload is not None else {},
    }


def valid_envelope(message: typing.Any, allowed: typing.Iterable[str]) -> bool:
    """A message is worth reading only if all of this holds.

    Origin and source window are checked by the caller - they are browser
    facts, not message facts - and this is everything else: our protocol,
    a type from the whitelist for this direction, string ids, and a payload
    that is an object.
    """
    if not isinstance(message, dict):
        return False
    if message.get("protocol") != PROTOCOL:
        return False
    if message.get("type") not in set(allowed):
        return False
    if not isinstance(message.get("channel_id"), str) or not message["channel_id"]:
        return False
    if not isinstance(message.get("request_id"), str) or not message["request_id"]:
        return False
    return isinstance(message.get("payload", {}), dict)


def normalize_receiver(raw: typing.Any) -> typing.Optional[dict]:
    """One receiver descriptor as MiniPaint will use it, or None.

    A descriptor the bridge cannot fully describe is dropped rather than
    guessed at: an unknown id, an unknown operation or a missing label all
    mean "do not offer this", because offering it would mean sending into a
    slot nobody proved exists.
    """
    if not isinstance(raw, dict):
        return None
    receiver_id = raw.get("id")
    if receiver_id not in RECEIVER_IDS:
        return None
    operation = raw.get("operation")
    if operation not in OPERATIONS:
        return None
    role = raw.get("role")
    if role not in ROLES:
        return None

    count = raw.get("count")
    max_count = raw.get("max_count")
    count = int(count) if isinstance(count, (int, float)) and not isinstance(count, bool) else 0
    if isinstance(max_count, (int, float)) and not isinstance(max_count, bool):
        max_count = int(max_count)
    else:
        max_count = None

    # Fullness is an append-only idea. A receiver that replaces what it holds
    # can always take another picture - that is what replacing means - so a
    # start frame with an image in it must not come back disabled, which is
    # what a naive "count >= max_count" does to a single-image slot.
    full = operation == APPEND and max_count is not None and count >= max_count
    enabled = bool(raw.get("enabled", True)) and bool(raw.get("visible", True)) and not full
    # Switched on right now, as opposed to offered because the model allows
    # it. A bridge that predates the distinction only ever offered what was
    # switched on, so its silence means "selected".
    selected = raw.get("selected")
    selected = bool(selected) if isinstance(selected, bool) else enabled
    switch = raw.get("switch")
    switch = switch if isinstance(switch, str) and SWITCH_RE.match(switch) else ""

    label = str(raw.get("label") or receiver_id.replace("_", " ").title())
    menu_label = str(raw.get("menu_label") or DEFAULT_MENU_LABELS.get(receiver_id) or f"Send Image to WanGP {label}")
    focus_hint = raw.get("focus_hint")

    return {
        "id": receiver_id,
        "role": role,
        "label": label,
        "menu_label": menu_label,
        "operation": operation,
        "enabled": enabled,
        "count": count,
        "max_count": max_count,
        "full": full,
        "focus_hint": str(focus_hint) if isinstance(focus_hint, str) else "",
        "reason_code": str(raw.get("reason_code") or ""),
        "selected": selected,
        "switch": switch if enabled and not selected else "",
    }


def normalize_receivers(raw: typing.Any) -> list:
    """The receiver list, in the order the ids are declared above.

    A stable order matters more than the bridge's order: the menu must not
    reshuffle itself between two openings that describe the same state.
    """
    if not isinstance(raw, list):
        return []
    seen: dict[str, dict] = {}
    for item in raw:
        descriptor = normalize_receiver(item)
        if descriptor and descriptor["id"] not in seen:
            seen[descriptor["id"]] = descriptor
    return [seen[key] for key in RECEIVER_IDS if key in seen]


# -- the queue: normalisation -------------------------------------------------


def clean_prompt(value: typing.Any) -> typing.Optional[str]:
    """The prompt as it may cross, or None for "inherit the page's".

    Absent, None, not a string, empty or whitespace-only all mean inherit:
    for v1 there is no way to say "clear the prompt", and an empty box in a
    UI means "use WanGP's". Control characters are removed - the ones in
    the Unicode ``Cc`` class - except ordinary newlines and tabs, since a
    prompt is lines and WanGP's own semantics apply to those. Nothing else is
    touched: joiners, marks and emoji are the user's text.
    """
    if not isinstance(value, str):
        return None
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    kept = "".join(
        character for character in text
        if character in "\n\t" or unicodedata.category(character) != "Cc"
    )
    kept = kept.strip()
    return kept or None


def normalize_queue_request(raw: typing.Any) -> typing.Tuple[dict, str]:
    """``(request, code)``: the request with exactly the whitelisted fields,
    or a code saying why it cannot be one.

    The semantics of section 0 are written here and nowhere else: a field
    that is absent, null or UI-empty is *inherited* from the live page, and a
    field that is supplied overrides the page for this one queued request.
    An empty reference list is absence. Nothing unlisted survives - not a
    path, not a filename, not a model, not a setting - and an id that is not
    exactly the handoff shape is refused rather than repaired.
    """
    if not isinstance(raw, dict):
        return {}, QUEUE_CODE_REQUEST_INVALID

    request_id = raw.get("request_id")
    if not valid_request_id(request_id):
        return {}, QUEUE_CODE_REQUEST_INVALID

    session = raw.get("bridge_session")
    request: dict = {
        "request_id": request_id,
        "bridge_session": session if isinstance(session, str) and re.match(r"\A[A-Za-z0-9._:-]{1,128}\Z", session) else "",
    }

    prompt = raw.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        return {}, QUEUE_CODE_REQUEST_INVALID
    cleaned = clean_prompt(prompt)
    if cleaned is not None:
        if len(cleaned) > PROMPT_MAX_CHARS:
            return {}, QUEUE_CODE_PROMPT_TOO_LONG
        request["prompt"] = cleaned

    for key in ("start_handoff_id", "end_handoff_id"):
        value = raw.get(key)
        if value is None or value == "":
            continue
        if not valid_handoff_id(value):
            return {}, QUEUE_CODE_REQUEST_INVALID
        request[key] = value

    references = raw.get("reference_handoff_ids")
    if references is not None:
        if not isinstance(references, (list, tuple)):
            return {}, QUEUE_CODE_REQUEST_INVALID
        if len(references) > MAX_QUEUE_REFERENCES:
            return {}, QUEUE_CODE_REQUEST_INVALID
        kept = []
        for value in references:
            if value is None or value == "":
                continue
            if not valid_handoff_id(value):
                return {}, QUEUE_CODE_REQUEST_INVALID
            kept.append(value)
        if kept:
            request["reference_handoff_ids"] = kept

    start = raw.get("start")
    if start is None or start == "":
        start = START_AUTO
    if start not in START_MODES:
        return {}, QUEUE_CODE_REQUEST_INVALID
    request["start"] = start

    # Protocol 5: the model the request was composed for, when it insists.
    wanted = raw.get("model_type")
    if wanted is not None and wanted != "":
        if not isinstance(wanted, str) or not MODEL_TYPE_RE.match(wanted):
            return {}, QUEUE_CODE_REQUEST_INVALID
        request["model_type"] = wanted

    return request, ""


def queue_overrides(request: typing.Mapping[str, typing.Any]) -> typing.List[str]:
    """Which of the four fields a normalised request supplies, in order."""
    supplied = []
    if request.get("prompt") is not None:
        supplied.append(QUEUE_FIELD_PROMPT)
    if request.get("start_handoff_id"):
        supplied.append(QUEUE_FIELD_START)
    if request.get("end_handoff_id"):
        supplied.append(QUEUE_FIELD_END)
    if request.get("reference_handoff_ids"):
        supplied.append(QUEUE_FIELD_REFERENCES)
    return supplied


def queue_payload_hash(request: typing.Mapping[str, typing.Any]) -> str:
    """One digest of what a request overrides, for idempotency.

    The same request id with the same digest is a retry and is answered from
    the record; the same id with a different digest is a conflict. The prompt
    text goes into the hash and never into the record, so a dedupe table
    holds no words.
    """
    canonical = {
        "prompt": request.get("prompt"),
        "start": request.get("start_handoff_id") or "",
        "end": request.get("end_handoff_id") or "",
        "references": list(request.get("reference_handoff_ids") or []),
        "start_mode": request.get("start") or START_AUTO,
        "model_type": request.get("model_type") or "",
    }
    return hashlib.sha256(canonical_json(canonical).encode("utf-8")).hexdigest()


def queue_summary(
    applied: typing.Any = None,
    inherited: typing.Any = None,
    ignored: typing.Any = None,
) -> dict:
    """The applied / inherited / ignored triple, in one shape on both sides.

    ``applied`` says per field whether the request's value went in (a count
    for references); ``inherited`` lists the fields the page kept its own
    value for; ``ignored`` lists a field the request supplied that the live
    model could not use, with the code that says why. A field is in exactly
    one of the three.
    """
    applied = applied if isinstance(applied, dict) else {}
    out_applied = {
        QUEUE_FIELD_PROMPT: bool(applied.get(QUEUE_FIELD_PROMPT)),
        QUEUE_FIELD_START: bool(applied.get(QUEUE_FIELD_START)),
        QUEUE_FIELD_END: bool(applied.get(QUEUE_FIELD_END)),
        QUEUE_FIELD_REFERENCES: 0,
    }
    count = applied.get(QUEUE_FIELD_REFERENCES)
    if isinstance(count, bool):
        count = 1 if count else 0
    if isinstance(count, (int, float)) and count > 0:
        out_applied[QUEUE_FIELD_REFERENCES] = int(count)

    out_ignored = []
    seen = set()
    for item in ignored if isinstance(ignored, (list, tuple)) else []:
        if not isinstance(item, dict):
            continue
        field = item.get("field")
        code = item.get("code")
        if field not in QUEUE_FIELDS or field in seen:
            continue
        seen.add(field)
        out_ignored.append({"field": field, "code": code if isinstance(code, str) and CODE_RE.match(code) else "RECEIVER_DISABLED"})

    named = set(inherited) if isinstance(inherited, (list, tuple, set)) else set()
    out_inherited = [
        field for field in QUEUE_FIELDS
        if field in named and not out_applied[field] and field not in seen
    ]
    return {"applied": out_applied, "inherited": out_inherited, "ignored": out_ignored}


def _code_or(value: typing.Any, fallback: str = "") -> str:
    return value if isinstance(value, str) and CODE_RE.match(value) else fallback


def _whole(value: typing.Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value) if value > 0 else 0


def _tristate(value: typing.Any) -> typing.Optional[bool]:
    """True, False, or "not known" - never a guess from a stray value."""
    return value if isinstance(value, bool) else None


def _depth(value: typing.Any) -> typing.Optional[int]:
    """How many tasks sit ahead, when the bridge could count; else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value >= 0 else None


def model_block(raw: typing.Any) -> dict:
    """The model a bridge answer names: four short strings, never more.
    ``architecture`` (protocol 5) is the base model a finetune stands on."""
    raw = raw if isinstance(raw, dict) else {}
    return {key: str(raw.get(key) or "")[:120] for key in ("type", "label", "family", "architecture")}


def normalize_queue_result(raw: typing.Any) -> dict:
    """The immediate answer to a queue request, as the parent will use it.

    A result that does not say a valid admission is a refusal: the parent
    must never show "added to queue" from this message, and it must not show
    it from a message it cannot read either.
    """
    raw = raw if isinstance(raw, dict) else {}
    admission = raw.get("admission")
    ok = raw.get("ok") is True and admission in (ADMISSION_REQUESTED, ADMISSION_DUPLICATE)
    summary = queue_summary(raw.get("applied"), raw.get("inherited"), raw.get("ignored"))
    model = raw.get("model")
    return {
        "ok": ok,
        "request_id": raw.get("request_id") if valid_request_id(raw.get("request_id")) else "",
        "bridge_session": raw.get("bridge_session") if isinstance(raw.get("bridge_session"), str) else "",
        "admission": admission if admission in ADMISSIONS else ADMISSION_REFUSED,
        "applied": summary["applied"],
        "inherited": summary["inherited"],
        "ignored": summary["ignored"],
        "code": "" if ok else _code_or(raw.get("code"), QUEUE_CODE_REFUSED),
        "model": model_block(model),
        # Protocol 4: which trigger was written, what the request asked about
        # starting, and whether WanGP was generating when the bridge decided.
        "route": raw.get("route") if raw.get("route") in ROUTES else "",
        "start": raw.get("start") if raw.get("start") in START_ANSWERS else "",
        "generation_running": _tristate(raw.get("generation_running")),
    }


def normalize_queue_status(raw: typing.Any) -> dict:
    """One confirmation answer. An unreadable status is "pending", never
    "queued": only a positive, well-formed observation counts."""
    raw = raw if isinstance(raw, dict) else {}
    status = raw.get("status")
    ok = raw.get("ok") is True and status in QUEUE_STATUSES
    return {
        "ok": ok,
        "request_id": raw.get("request_id") if valid_request_id(raw.get("request_id")) else "",
        "status": status if ok else QUEUE_PENDING,
        "tasks_added": _whole(raw.get("tasks_added")) if ok else 0,
        "code": _code_or(raw.get("code"), "" if ok else QUEUE_CODE_UNCONFIRMED),
        # Protocol 4: best-effort, and absent rather than estimated.
        "queue_depth": _depth(raw.get("queue_depth")) if ok else None,
        "route": raw.get("route") if raw.get("route") in ROUTES else "",
    }


def normalize_queue_track(raw: typing.Any) -> dict:
    """One track answer (protocol 5): per request, where its task is now.

    An entry the bridge could not describe is "unknown", never "finished",
    and an answer that is not one is empty with a code: a page must not
    show "finished" from a message it cannot read.
    """
    raw = raw if isinstance(raw, dict) else {}
    ok = raw.get("ok") is True and isinstance(raw.get("tracked"), dict)
    tracked: dict = {}
    if ok:
        for key, value in raw["tracked"].items():
            if not valid_request_id(key) or not isinstance(value, dict):
                continue
            state = value.get("state") if value.get("state") in TRACK_STATES else TRACK_UNKNOWN
            tracked[key] = {"state": state, "position": _depth(value.get("position")), "queue_depth": _depth(value.get("queue_depth"))}
    return {
        "ok": ok,
        "tracked": tracked,
        "code": "" if ok else _code_or(raw.get("code"), QUEUE_CODE_REQUEST_INVALID),
        "generation_running": _tristate(raw.get("generation_running")),
    }




# -- protocol 6: the control plane -----------------------------------------
#
# Everything above this line is a browser talking to an iframe. This part is
# not: it is Forge talking to the WanGP child directly, over loopback, with
# no page in between and frequently with no page in existence. That is the
# whole point of it - a job the user walked away from has nobody to relay for
# it - and it is why the vocabulary is separate rather than folded into the
# postMessage types above. Nothing here ever crosses to a browser.
#
# The transport is an ordinary JSON POST to the child's own Gradio app, under
# a prefix of ours, carrying the per-launch secret Forge minted for the child
# and exported into its environment. Loopback alone is not the boundary: the
# moment a route on that socket can start a generation with no session, port
# plus loopback stops being sufficient, so the secret is compared on every
# request and a mismatch is refused before the body is read.

#: Where the child answers. Under the child's own root, so the Forge-side
#: proxy's ``/wan2gp/`` prefix is not involved and a browser that reached the
#: proxy cannot reach this by removing a path segment: the proxy only ever
#: forwards ``/wan2gp/``-prefixed paths, and this is not one.
CONTROL_PREFIX = "/minipaint-bridge"
#: Bumped when the shape of a control request or answer changes in a way an
#: older Forge could misread. Reported by ``hello`` and checked there.
#:
#: Versioned separately from ``PROTOCOL`` on purpose, and that is why the
#: number above did not move when this whole section was added. ``PROTOCOL``
#: is the postMessage envelope a browser and an iframe filter on: bumping it
#: makes every already-loaded page stop answering, which is a real cost, and
#: nothing about what those two say to each other changed here. This plane
#: has different participants, a different transport and a different
#: lifetime, so it carries its own number.
CONTROL_VERSION = 1
#: The header the per-launch secret travels in. Never logged, never in an
#: answer, never in a diagnostics report.
CONTROL_SECRET_HEADER = "x-minipaint-bridge-secret"

#: What the control plane can be asked. Anything else is a 404 rather than a
#: refusal, so a probe cannot enumerate operations that exist but are off.
CONTROL_HELLO = "hello"
CONTROL_COMPOSE = "compose"
CONTROL_SUBMIT = "submit"
CONTROL_STATUS = "status"
CONTROL_CANCEL = "cancel"
CONTROL_FORGET = "forget"
CONTROL_OPERATIONS = (CONTROL_HELLO, CONTROL_COMPOSE, CONTROL_SUBMIT, CONTROL_STATUS, CONTROL_CANCEL, CONTROL_FORGET)

#: An execution id is the same grammar as a request id, for the same reason.
#: It is the ledger key, and the bridge writes it into WanGP's own
#: ``client_id`` so that the artifacts a generation returns identify
#: themselves - the task's settings carry the composing page's client_id
#: otherwise, and an adapter that did not overwrite it would misattribute
#: every file it got back.
EXECUTION_ID_RE = HANDOFF_ID_RE

#: Where one execution is, as the child reports it. These are the child's
#: words; the Forge job states in ``clipboard/outbox.py`` are derived from
#: them and are not the same vocabulary.
EXEC_ACCEPTED = "accepted"
EXEC_QUEUED = "queued"
EXEC_RUNNING = "running"
EXEC_DONE = "done"
EXEC_FAILED = "failed"
EXEC_CANCELLED = "cancelled"
#: A submission was recorded and its outcome never was. The ledger answers
#: this rather than guessing, and it is what produces EXECUTION_UNKNOWN on
#: the Forge side instead of a second generation.
EXEC_UNKNOWN = "unknown"
EXEC_STATES = (EXEC_ACCEPTED, EXEC_QUEUED, EXEC_RUNNING, EXEC_DONE, EXEC_FAILED, EXEC_CANCELLED, EXEC_UNKNOWN)
EXEC_TERMINAL = (EXEC_DONE, EXEC_FAILED, EXEC_CANCELLED, EXEC_UNKNOWN)
EXEC_OPEN = (EXEC_ACCEPTED, EXEC_QUEUED, EXEC_RUNNING)

#: Where a composed settings base came from. Recorded on the job and shown,
#: because a job that silently ran at factory settings when the user had
#: configured something else is the failure the whole compose exists to
#: prevent.
BASE_RECORDED = "recorded_form"
BASE_SESSION = "session"
BASE_FACTORY = "factory_defaults"
BASE_SOURCES = (BASE_RECORDED, BASE_SESSION, BASE_FACTORY)

#: The media slots a submission may fill, and the settings keys each one
#: lands in. The keys are WanGP's; they are named in compatibility.py on the
#: child side and never anywhere else, so this table carries only the logical
#: names and the order they are applied in.
EXEC_SLOT_START = "start"
EXEC_SLOT_END = "end"
EXEC_SLOT_REFERENCES = "references"
EXEC_SLOTS = (EXEC_SLOT_START, EXEC_SLOT_END, EXEC_SLOT_REFERENCES)

#: Control-plane refusals. Spelled here so both sides match exactly; the
#: Forge copy in ``errors.py`` carries the sentences.
CONTROL_UNAUTHORISED = "CONTROL_UNAUTHORISED"
CONTROL_UNAVAILABLE = "CONTROL_UNAVAILABLE"
CONTROL_VERSION_MISMATCH = "CONTROL_VERSION_MISMATCH"
EXECUTION_ID_CONFLICT = "EXECUTION_ID_CONFLICT"
EXECUTION_REFUSED = "EXECUTION_REFUSED"
EXECUTION_UNKNOWN = "EXECUTION_UNKNOWN"
SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
COMPOSE_UNAVAILABLE = "COMPOSE_UNAVAILABLE"
MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"


def valid_execution_id(value: typing.Any) -> bool:
    return isinstance(value, str) and bool(EXECUTION_ID_RE.match(value))


def normalize_compose_request(raw: typing.Any) -> typing.Tuple[dict, str]:
    """What ``compose`` takes: a model to compose for, and nothing else.

    ``model_type`` empty means "whatever the child is on", which is the
    honest answer when no page has ever chosen one. A session hash may be
    supplied to prefer that page's own live settings over the process-wide
    recorded form; it is an optimisation and its absence is not an error.
    """
    if not isinstance(raw, dict):
        return {}, QUEUE_CODE_REQUEST_INVALID
    model_type = raw.get("model_type")
    if model_type is not None and not isinstance(model_type, str):
        return {}, QUEUE_CODE_REQUEST_INVALID
    session = raw.get("session_hash")
    if session is not None and not isinstance(session, str):
        return {}, QUEUE_CODE_REQUEST_INVALID
    return {"model_type": str(model_type or "")[:200], "session_hash": str(session or "")[:200]}, ""


def normalize_media_map(raw: typing.Any) -> typing.Tuple[dict, str]:
    """The handoff ids a submission fills its media slots from.

    Ids only. A path never crosses this boundary in either direction: Forge
    writes the pixels into the handoff root both processes already share and
    names them, and the child resolves them under its own root or refuses.
    """
    if raw is None:
        return {}, ""
    if not isinstance(raw, dict):
        return {}, QUEUE_CODE_REQUEST_INVALID
    out: typing.Dict[str, typing.Any] = {}
    for slot in (EXEC_SLOT_START, EXEC_SLOT_END):
        value = raw.get(slot)
        if value is None or value == "":
            continue
        if not valid_handoff_id(value):
            return {}, QUEUE_CODE_REQUEST_INVALID
        out[slot] = value
    references = raw.get(EXEC_SLOT_REFERENCES)
    if references is not None:
        if not isinstance(references, (list, tuple)):
            return {}, QUEUE_CODE_REQUEST_INVALID
        kept = []
        for item in references:
            if item is None or item == "":
                continue
            if not valid_handoff_id(item):
                return {}, QUEUE_CODE_REQUEST_INVALID
            kept.append(item)
        if len(kept) > MAX_QUEUE_REFERENCES:
            return {}, QUEUE_CODE_REQUEST_INVALID
        if kept:
            out[EXEC_SLOT_REFERENCES] = kept
    return out, ""


def normalize_execution_request(raw: typing.Any) -> typing.Tuple[dict, str]:
    """One server-executed submission, as the child will take it.

    The settings dict is passed through rather than validated field by field:
    it is WanGP's own shape, composed by the child from the user's own
    configuration, and this side of the wire has no business knowing what is
    in it. What is checked is everything that is *ours* - the execution id
    that keys the ledger, the prompt, the media ids, the model the snapshot
    was composed for - because those are the parts a mistake here would
    silently corrupt.
    """
    if not isinstance(raw, dict):
        return {}, QUEUE_CODE_REQUEST_INVALID
    execution_id = raw.get("execution_id")
    if not valid_execution_id(execution_id):
        return {}, QUEUE_CODE_REQUEST_INVALID
    settings = raw.get("settings")
    if not isinstance(settings, dict) or not settings:
        return {}, QUEUE_CODE_REQUEST_INVALID
    prompt = raw.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        return {}, QUEUE_CODE_REQUEST_INVALID
    cleaned = clean_prompt(prompt)
    if cleaned is not None and len(cleaned) > PROMPT_MAX_CHARS:
        return {}, QUEUE_CODE_PROMPT_TOO_LONG
    media, code = normalize_media_map(raw.get("media"))
    if code:
        return {}, code
    model_type = raw.get("model_type")
    if model_type is not None and not isinstance(model_type, str):
        return {}, QUEUE_CODE_REQUEST_INVALID
    request = {
        "execution_id": execution_id,
        "settings": dict(settings),
        "media": media,
        "model_type": str(model_type or "")[:200],
        # Unattended work never jumps the user's own queue. ``priority`` is
        # for a job somebody is sitting in front of waiting for, and it is
        # off unless a caller says otherwise.
        "priority": raw.get("priority") is True,
    }
    if cleaned is not None:
        request["prompt"] = cleaned
    return request, ""


def normalize_execution_record(raw: typing.Any) -> dict:
    """One ledger record, as Forge will read it.

    An unreadable record is ``unknown``, never ``done``: the whole reason the
    ledger exists is that "I cannot prove what happened" and "it finished"
    must not be the same answer.
    """
    raw = raw if isinstance(raw, dict) else {}
    state = raw.get("state") if raw.get("state") in EXEC_STATES else EXEC_UNKNOWN
    files = raw.get("generated_files")
    kept = [str(item)[:400] for item in files if isinstance(item, str) and item][:64] if isinstance(files, (list, tuple)) else []
    return {
        "execution_id": raw.get("execution_id") if valid_execution_id(raw.get("execution_id")) else "",
        "state": state,
        "stage": str(raw.get("stage") or "")[:160],
        "position": _depth(raw.get("position")),
        "queue_depth": _depth(raw.get("queue_depth")),
        "submitted_at": float(raw.get("submitted_at") or 0.0),
        "finished_at": float(raw.get("finished_at") or 0.0),
        "generated_files": kept,
        "file_count": len(kept),
        "code": _code_or(raw.get("code")),
        "message": str(raw.get("message") or "")[:200],
        # Which residency key this task ran under, and whether it forced a
        # model load. A job that took three minutes longer deserves the
        # reason, and it is the only way to catch a setting that is quietly
        # defeating residency.
        "residency_key": str(raw.get("residency_key") or "")[:200],
        "reload_reason": str(raw.get("reload_reason") or "")[:120],
        # Which run of the child this record belongs to. A record whose
        # instance is not the child now running is a record about a process
        # that is gone, which is the whole of the restart reconciliation.
        "instance": str(raw.get("instance") or "")[:64],
    }


def normalize_control_hello(raw: typing.Any) -> dict:
    """The child's answer to "are you there, and can you take a job".

    READY on the Forge side is a liveness and transport fact. It does not say
    the bridge is on the page, that the service arbiter resolved, or that a
    settings base can be composed - so the child answers those itself and the
    executor does not treat a running process as an admission.
    """
    raw = raw if isinstance(raw, dict) else {}
    return {
        "ok": raw.get("ok") is True,
        "control_version": _whole(raw.get("control_version")),
        "protocol": _whole(raw.get("protocol")),
        "bridge_version": str(raw.get("bridge_version") or "")[:40],
        "wan2gp_version": str(raw.get("wan2gp_version") or "")[:40],
        "instance": str(raw.get("instance") or "")[:64],
        #: Whether a job can actually be submitted right now, and if not, why.
        "can_execute": raw.get("can_execute") is True,
        "can_compose": raw.get("can_compose") is True,
        #: The one process-wide generation arbiter. False means the shared
        #: queue cannot be reached and server execution is off for this run -
        #: never a reason to fall back to a path that runs beside it.
        "service": raw.get("service") is True,
        "generation_running": _tristate(raw.get("generation_running")),
        "queue_depth": _depth(raw.get("queue_depth")),
        "model_type": str(raw.get("model_type") or "")[:200],
        "code": _code_or(raw.get("code")),
        "message": str(raw.get("message") or "")[:200],
    }


def normalize_compose_answer(raw: typing.Any) -> dict:
    """A composed settings base and where it came from."""
    raw = raw if isinstance(raw, dict) else {}
    settings = raw.get("settings")
    ok = raw.get("ok") is True and isinstance(settings, dict) and bool(settings)
    source = raw.get("source") if raw.get("source") in BASE_SOURCES else BASE_FACTORY
    return {
        "ok": ok,
        "settings": dict(settings) if ok else {},
        "source": source if ok else "",
        "model_type": str(raw.get("model_type") or "")[:200],
        "wan2gp_version": str(raw.get("wan2gp_version") or "")[:40],
        "residency_key": str(raw.get("residency_key") or "")[:200],
        "model": model_block(raw.get("model")),
        "code": "" if ok else _code_or(raw.get("code"), COMPOSE_UNAVAILABLE),
        "message": str(raw.get("message") or "")[:200],
    }

# ------------------------------------------------------------ END SHARED --
