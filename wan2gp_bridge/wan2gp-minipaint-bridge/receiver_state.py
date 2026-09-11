"""The one true picture of what this page has selected, and its fingerprint.

Section 17's problem, plainly: the Send menu is a photograph. Between the
moment WanGP answers "you may append a reference image" and the moment the
user clicks that item, they may have changed the model, turned reference
images off, or filled the last free slot in another browser interaction.
Sending anyway would put a picture somewhere nobody asked for, and picking a
different destination instead would be worse.

So every answer carries a fingerprint of the live values a receiver decision
depends on, and every apply recomputes that fingerprint from freshly read
values and compares before it touches anything. A counter incremented by
event handlers would not do: it only notices changes somebody remembered to
register a callback for, and the one that breaks us is by definition the one
nobody thought of. A digest of the state itself has no such blind spot -
``protocol.state_revision`` is shared with the Forge side precisely so both
halves agree on what "the same state" means.

Everything here is a pure function of values handed in. The module never
reads a component, never touches Gradio and never keeps a process-global
"current state": that variable is exactly the bug section 13.2 describes, one
browser tab deciding where another tab's image goes. Live values arrive as
the inputs of a Gradio event, which is the only source that is true of one
page, and leave again as a ``SessionState`` the caller holds for the length of
one request.
"""

from __future__ import annotations

import dataclasses
import typing

try:
    from . import compatibility, protocol
except ImportError:  # pragma: no cover - depends on how WanGP imports plugins
    import compatibility  # type: ignore[no-redef]
    import protocol  # type: ignore[no-redef]


#: The canonical state's key set, fixed and ordered. Adding a key changes
#: every fingerprint, which is correct - the state being described is a
#: different state - but it also invalidates every menu open at that moment,
#: so it is a deliberate act rather than a side effect of a refactor.
CANONICAL_KEYS = (
    "model_type",
    "model_mode",
    "active_view",
    "image_prompt_type",
    "video_prompt_type",
    "audio_prompt_type",
    "image_mode",
    "reference_count",
    "start_present",
    "end_present",
    "receiver_flags",
)


def selection_text(value: typing.Any) -> str:
    """A selection value as a comparable string.

    WanGP's prompt-type settings are letter flags, but a build may hand back a
    list of choices or an enum instead. Anything that is not already a string
    is rendered once, here, so that the fingerprint does not change because
    Gradio started passing a tuple where it used to pass a list.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set)):
        return ",".join(sorted(selection_text(item) for item in value))
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def item_count(value: typing.Any) -> int:
    """How many things are in a list-shaped live value.

    Deliberately shape-agnostic: the gallery's element type is the adapter's
    business, and the fingerprint only needs to notice that the number moved.
    """
    if value is None:
        return 0
    if isinstance(value, (str, bytes)):
        return 1
    if isinstance(value, dict):
        return 1
    try:
        return len(value)
    except TypeError:
        return 1


def value_present(value: typing.Any) -> bool:
    """Whether a single-image receiver currently holds anything."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


@dataclasses.dataclass(frozen=True)
class SessionState:
    """One page's receiver-affecting state, as read once.

    Frozen because a state that could be edited after its fingerprint was
    taken is not a fingerprint of anything. A new read makes a new object.
    """

    values: typing.Mapping[str, typing.Any]
    canonical: typing.Mapping[str, typing.Any]
    revision: str
    model: typing.Mapping[str, str]
    capabilities: typing.Mapping[str, typing.Optional[bool]]
    view: str
    #: Layer A on its own: what this page's model could take if the selector
    #: were set, per receiver. See ``compatibility.Compatibility.allowances``.
    allowances: typing.Mapping[str, typing.Optional[bool]] = dataclasses.field(default_factory=dict)

    def value(self, component_key: str) -> typing.Any:
        return self.values.get(component_key)

    def allowed(self, receiver_id: str) -> bool:
        """The model takes this input and a send could switch it on.

        Only a definite yes counts. "Unknown" - a build that handed over no
        model definition, or none of the controls a switch would need - is
        not permission, which keeps a build that lacks the new components on
        exactly the behaviour it had: offered only while switched on.
        """
        return self.allowances.get(receiver_id) is True

    def offered(self, receiver_id: str) -> bool:
        """What the menu enables: switched on now, or allowed and switchable."""
        return self.active(receiver_id) or self.allowed(receiver_id)

    def active(self, receiver_id: str) -> bool:
        """Layer A and layer B together, in the order section 25 states them.

        The schema says what could exist; the live selection says what is
        switched on. A capability the schema explicitly denies is not active
        however the selector looks, and a capability the schema is silent
        about is decided by the selector alone - silence is not permission to
        assume "no", or a build that stopped publishing one metadata key would
        take the whole menu with it.
        """
        rule = compatibility.SELECTION_RULES.get(receiver_id)
        if rule is None:
            return False
        if self.capabilities.get(receiver_id) is False:
            return False
        return rule.satisfied_by(self.values.get(rule.component_key))

    def matches(self, revision: typing.Any) -> bool:
        return isinstance(revision, str) and bool(revision) and revision == self.revision


def canonical_state(
    values: typing.Mapping[str, typing.Any],
    model: typing.Mapping[str, str],
    capabilities: typing.Mapping[str, typing.Optional[bool]],
    view: str = "",
) -> dict:
    """The normalised state, in the exact shape both halves hash.

    Normalisation is the whole trick. Two reads of an unchanged UI must
    produce the same bytes even if Gradio handed back a list one time and a
    tuple the next, and two genuinely different states must not collide. So
    every value is reduced to a string, an int or a bool before it goes in,
    and the flags are computed rather than copied.
    """
    flags = {}
    for receiver_id in protocol.RECEIVER_IDS:
        rule = compatibility.SELECTION_RULES.get(receiver_id)
        if rule is None:
            continue
        allowed = capabilities.get(receiver_id)
        flags[receiver_id] = bool(allowed is not False and rule.satisfied_by(values.get(rule.component_key)))

    return {
        "model_type": str(model.get("type", "")),
        "model_mode": selection_text(values.get(compatibility.MODEL_MODE)),
        "active_view": str(view or ""),
        "image_prompt_type": selection_text(values.get(compatibility.IMAGE_PROMPT_TYPE)),
        "video_prompt_type": selection_text(values.get(compatibility.VIDEO_PROMPT_TYPE)),
        "audio_prompt_type": selection_text(values.get(compatibility.AUDIO_PROMPT_TYPE)),
        "image_mode": selection_text(values.get(compatibility.IMAGE_MODE)),
        "reference_count": item_count(values.get(compatibility.REFERENCE_GALLERY)),
        "start_present": value_present(values.get(compatibility.START_IMAGE)),
        "end_present": value_present(values.get(compatibility.END_IMAGE)),
        "receiver_flags": flags,
    }


def build(
    values: typing.Mapping[str, typing.Any],
    model: typing.Optional[typing.Mapping[str, str]] = None,
    capabilities: typing.Optional[typing.Mapping[str, typing.Optional[bool]]] = None,
    view: str = "",
    allowances: typing.Optional[typing.Mapping[str, typing.Optional[bool]]] = None,
) -> SessionState:
    """Read once, normalise once, fingerprint once."""
    model = dict(model or {})
    capabilities = dict(capabilities or {})
    canonical = canonical_state(values, model, capabilities, view)
    return SessionState(
        values=dict(values),
        canonical=canonical,
        revision=protocol.state_revision(canonical),
        model=model,
        capabilities=capabilities,
        view=str(view or ""),
        allowances=dict(allowances or {}),
    )


def read(
    compat: "compatibility.Compatibility",
    live: typing.Mapping[str, typing.Any],
    schema: typing.Any = None,
) -> SessionState:
    """The state of the page whose event supplied ``live``.

    ``live`` is the component-key-to-value mapping assembled from the bridge
    event's own inputs, so it is session-scoped by construction - and the model
    selector is one of those inputs, which is what keeps two browser tabs on
    two models from reading each other's schema. The plugin API's globals are
    still consulted, but only once this page's own selection agrees with them.
    """
    selected = live.get(compatibility.MODEL_SELECTOR)
    model_type, definition = compat.page_model(live)
    return build(
        values=live,
        model=compat.model_descriptor(selected, definition=definition, model_type=model_type),
        capabilities=compat.model_capabilities(schema if schema is not None else definition, selected),
        view=str(compat.host.read_global("active_view") or ""),
        allowances=compat.allowances(live),
    )


def require_unchanged(state: SessionState, revision: typing.Any) -> None:
    """The gate of section 17.1, and it only ever refuses.

    No redirect to another receiver, no automatic model or mode switch, no
    rounding a stale revision up to the current one. The menu is reopened and
    the user chooses again from what is true now.
    """
    if not state.matches(revision):
        raise compatibility.BridgeError(
            compatibility.STALE_RECEIVER_STATE,
            f"revision {str(revision)[:16]!r} no longer describes this page",
        )


def require_active(state: SessionState, receiver_id: typing.Any) -> None:
    """That receiver must exist in the protocol and be switched on right now."""
    if receiver_id not in protocol.RECEIVER_IDS:
        raise compatibility.BridgeError(compatibility.UNKNOWN_RECEIVER, f"no such logical receiver: {receiver_id!r}")
    if not state.active(str(receiver_id)):
        raise compatibility.BridgeError(compatibility.RECEIVER_DISABLED, f"{receiver_id} is not selected in this session")


def require_offered(state: SessionState, receiver_id: typing.Any) -> None:
    """That receiver must exist and be switched on, or allowed and switchable.

    The gate a send passes through. The second half is the whole of the
    "send to a scenario" feature: a model that takes a start frame is a
    destination whether or not the Location radio has been touched, and the
    send flips the radio itself. Neither switched on nor allowed is still a
    refusal, and still without choosing something else.
    """
    if receiver_id not in protocol.RECEIVER_IDS:
        raise compatibility.BridgeError(compatibility.UNKNOWN_RECEIVER, f"no such logical receiver: {receiver_id!r}")
    if not state.offered(str(receiver_id)):
        raise compatibility.BridgeError(
            compatibility.RECEIVER_DISABLED,
            f"{receiver_id} is neither selected on this page nor allowed by its model",
        )


def changed_keys(before: SessionState, after: SessionState) -> typing.List[str]:
    """Which canonical fields moved between two reads. For diagnostics only.

    The refusal is decided by the fingerprint; this exists so the WanGP
    console can say *what* changed instead of only that something did.
    """
    return [key for key in CANONICAL_KEYS if before.canonical.get(key) != after.canonical.get(key)]
