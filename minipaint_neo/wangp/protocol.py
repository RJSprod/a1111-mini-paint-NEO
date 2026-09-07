"""The vocabulary the three sides agree on: Forge, the browser, and the
plugin running inside WanGP.

Nothing here knows a WanGP component id, a model name or a DOM selector. It
knows *logical* receivers - "the start frame", "a reference image" - and the
shape of the messages that ask about them and fill them. That is the whole
point of the layer: when WanGP moves an input, only the WanGP-side adapter
changes, and this file does not.

The WanGP plugin ships its own copy of this module, because it is installed
into someone else's Python environment and cannot import anything of ours.
``tests/test_wangp_protocol.py`` holds the two copies to each other.
"""

from __future__ import annotations

import hashlib
import json
import re
import typing

# ---------------------------------------------------------------- SHARED --
# Everything between this marker and END SHARED is duplicated verbatim in
# wan2gp_bridge/wan2gp-minipaint-bridge/protocol.py. Change one, change both;
# the test fails otherwise.

PROTOCOL = 2

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

#: What the parent page may send into the iframe.
TO_BRIDGE = frozenset({HELLO, GET_RECEIVERS, RECEIVE_IMAGE, FOCUS_RECEIVER, THEME_STATE})
#: What the iframe may send out to the parent page.
TO_PARENT = frozenset({READY, RECEIVERS, RECEIVE_RESULT, RUNTIME_STATE})

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

#: Ceilings for one handoff. Generous for a high-resolution edit, finite.
MAX_HANDOFF_BYTES = 64 * 1024 * 1024
MAX_HANDOFF_PIXELS = 64 * 1024 * 1024
MAX_HANDOFF_SIDE = 16384

#: A postMessage envelope this size or larger is dropped unread.
MAX_ENVELOPE_BYTES = 256 * 1024

#: How long the parent waits, in milliseconds.
RECEIVER_QUERY_TIMEOUT_MS = 4000
RECEIVE_TIMEOUT_MS = 30000

#: How the state fingerprint is shortened for transport.
REVISION_LENGTH = 32


def valid_handoff_id(value: typing.Any) -> bool:
    return isinstance(value, str) and bool(HANDOFF_ID_RE.match(value))


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


# ------------------------------------------------------------ END SHARED --
