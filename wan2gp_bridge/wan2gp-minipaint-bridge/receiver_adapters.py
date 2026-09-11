"""How each logical receiver is actually read, filled and checked.

Section 15's point is that there is no such thing as "the way WanGP takes an
image". A start frame is one component holding one picture; a reference is a
list that grows; a control image may be neither by the time this matters. The
value shape differs, the update differs, and what counts as proof that the
image arrived differs. So the generic half of the bridge knows only this
interface, and every WanGP-shaped fact lives behind it.

Two rules run through all of it. The first is that the *bridge* decides
whether a receiver replaces or appends - MiniPaint never infers it from the
role - so an adapter that says ``append`` is refusing a replace even if the
caller asked for one. The second is section 22, which is the one worth
spelling out: a reference append reads the existing list, checks the capacity,
appends, keeps the earlier entries in their original order and shape, and then
counts. It never writes a one-element list. Losing four reference images to a
send that reported success is the failure this whole design exists to prevent.

Verification here is honest about what it can see. An adapter runs inside the
callback that produces the new value, so it can prove that the value being
handed to Gradio decodes to the pixels that were sent and that the list grew
by exactly one - "pixel-equivalent" and "structurally verified" in section
23.3's vocabulary. It cannot prove from inside that call that Gradio kept it;
that is what the next receiver query's fingerprint shows, and it is why the
acceptance criterion in section 49.2 is a human pressing Generate.
"""

from __future__ import annotations

import dataclasses
import typing

try:
    from . import compatibility, handoff, protocol, receiver_state
except ImportError:  # pragma: no cover - depends on how WanGP imports plugins
    import compatibility  # type: ignore[no-redef]
    import handoff  # type: ignore[no-redef]
    import protocol  # type: ignore[no-redef]
    import receiver_state  # type: ignore[no-redef]

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None  # type: ignore[assignment]


#: Section 23.3's levels, strongest first. Spelled the way the Forge side
#: spells them, because it compares the string.
BYTE_IDENTICAL = "byte-identical"
PIXEL_EQUIVALENT = "pixel-equivalent"
STRUCTURALLY_VERIFIED = "structurally verified"
UNVERIFIED = "unverified"


@dataclasses.dataclass(frozen=True)
class Applied:
    """What one apply produced, before Gradio has been given it."""

    receiver_id: str
    role: str
    operation: str
    component_key: str
    value: typing.Any
    previous_count: int
    new_count: int
    chained: typing.Tuple[str, ...] = ()
    #: The selector updates that switch this receiver on, by component key -
    #: empty when it already was. ``plugin.py`` places them by position.
    switch_updates: typing.Mapping[str, typing.Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class Verification:
    """Whether the value about to be applied is the picture that was sent."""

    ok: bool
    level: str
    code: str = ""
    detail: str = ""
    receiver_pixel_digest: str = ""


# ------------------------------------------------------------ value shapes --


def as_image(value: typing.Any) -> typing.Any:
    """Whatever a Gradio image-ish value is, as a PIL image or None.

    Every shape a receiver has been seen to hold is tried, because the whole
    reason for the adapter layer is that the shape is not ours to fix: a path,
    a ``(path, caption)`` pair, a dict with the file under one of several
    keys, a numpy array, or an image already.
    """
    if value is None or Image is None:
        return None
    if isinstance(value, Image.Image):
        return value
    if isinstance(value, (tuple, list)):
        return as_image(value[0]) if value else None
    if isinstance(value, dict):
        for key in ("image", "name", "path", "url", "data"):
            if value.get(key) is not None:
                return as_image(value[key])
        return None
    if isinstance(value, str):
        try:
            with Image.open(value) as opened:
                opened.load()
                return opened.convert("RGBA")
        except Exception:
            return None
    if hasattr(value, "shape") and hasattr(value, "dtype"):
        try:
            return Image.fromarray(value)
        except Exception:
            return None
    return None


def gallery_shaped(component: typing.Any) -> bool:
    """Whether a receiver takes a list rather than one picture.

    Wan2GP's start and end frames are galleries ("images as starting points
    for new videos in the queue" - one video per image), not single Image
    components, and a gallery handed a bare PIL image raises inside Gradio's
    postprocess after the callback has returned - which the bridge would never
    see. So a single-image receiver writes ``[image]`` when the component it
    resolved is a Gallery and the image itself otherwise.
    """
    return component is not None and type(component).__name__ == "Gallery"


def gallery_entries(value: typing.Any) -> typing.List[typing.Any]:
    """A gallery's current contents as a plain list, order untouched."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _entry_for(existing: typing.Sequence[typing.Any], image: typing.Any) -> typing.Any:
    """A new gallery entry shaped like the ones already there.

    VERIFY ON A REAL INSTALL: Gradio's gallery accepts a heterogeneous list
    and normalises each item on postprocess, so appending a bare image to a
    list of paths is expected to work. Confirm it on the target Gradio, and if
    that build insists on one shape, this function is the only place that has
    to learn it - the caption is left empty rather than invented.
    """
    if existing:
        last = existing[-1]
        if isinstance(last, tuple) and len(last) == 2:
            return (image, "")
        if isinstance(last, dict):
            shaped = {key: None for key in last}
            for key in ("image", "name", "path"):
                if key in shaped:
                    shaped[key] = image
                    return shaped
    return image


# ------------------------------------------------------------- the interface --


class ReceiverAdapter:
    """One logical receiver, and everything WanGP-shaped about it.

    Subclasses set the class attributes and, where the shape demands it,
    override ``snapshot``/``apply``/``verify``. Nothing outside this module
    may name a component key: ``component_key`` comes from
    ``compatibility.RECEIVER_COMPONENTS`` so that even the adapters get their
    ids from the table rather than spelling them.
    """

    logical_id = ""
    role = ""
    operation = protocol.REPLACE
    label = ""

    def __init__(self, compat: "compatibility.Compatibility") -> None:
        self.compat = compat

    # -- identity -----------------------------------------------------------

    @property
    def component_key(self) -> str:
        return compatibility.RECEIVER_COMPONENTS.get(self.logical_id, "")

    def component(self) -> typing.Any:
        return self.compat.component(self.component_key)

    def focus_hint(self) -> str:
        """A hint the parent may act on, and only ever an id we resolved.

        Never a CSS selector and never a label: the parent forwards this into
        the iframe, and the bridge's own JavaScript will only scroll to an
        element whose id it published in the same receiver list.
        """
        return self.compat.receiver_elem_id(self.logical_id)

    # -- description ---------------------------------------------------------

    def capacity(self, state: "receiver_state.SessionState") -> typing.Tuple[int, typing.Optional[int]]:
        """``(count, max_count)`` for right now. ``None`` means unbounded.

        The count says whether this slot already holds a picture; the limit is
        deliberately ``None``. A one-image slot that replaces what it holds has
        no maximum in the sense section 16.5 means - reporting ``1 of 1`` would
        say "full", and a start frame that already has an image is exactly the
        one a user most wants to send a new picture to.
        """
        return (1 if receiver_state.value_present(state.value(self.component_key)) else 0, None)

    def can_describe(self, state: "receiver_state.SessionState") -> typing.Optional[dict]:
        """The descriptor for this receiver, or None if it is not on offer.

        None rather than a disabled entry when the component never resolved:
        an input the bridge could not find is not an input the user can be
        told about. A receiver that exists but is switched off does come back,
        disabled and with a reason code, because that is a diagnosable state.
        """
        if self.component() is None:
            return None
        count, max_count = self.capacity(state)
        active = state.active(self.logical_id)
        offered = state.offered(self.logical_id)
        return {
            "id": self.logical_id,
            "role": self.role,
            "label": self.label,
            "menu_label": protocol.DEFAULT_MENU_LABELS.get(self.logical_id, ""),
            "operation": self.operation,
            "accept": ["image/png"],
            "enabled": offered,
            "visible": offered,
            "count": count,
            "max_count": max_count,
            "focus_hint": self.focus_hint(),
            # Switched on right now, as opposed to offered because the model
            # allows it - and what a send would flip in the latter case.
            "selected": active,
            "switch": "" if active or not offered else compatibility.SWITCH_TOKENS.get(self.logical_id, ""),
            "reason_code": "" if offered else compatibility.RECEIVER_DISABLED,
        }

    # -- reading and writing --------------------------------------------------

    def snapshot(self, state: "receiver_state.SessionState") -> typing.Any:
        """The receiver's current value, normalised only as far as it must be."""
        return state.value(self.component_key)

    def apply(
        self,
        loaded: "handoff.LoadedHandoff",
        operation: str,
        state: "receiver_state.SessionState",
    ) -> Applied:
        raise NotImplementedError

    def verify(self, loaded: "handoff.LoadedHandoff", applied: Applied) -> Verification:
        """Decode what is about to be applied and compare it with what was sent.

        Deliberately re-derived from ``applied.value`` rather than trusting the
        object identity: the adapter may have wrapped, converted or re-shaped
        the image on its way into the component, and it is the wrapped thing
        the user will generate from.
        """
        image = self._verifiable_image(applied)
        if image is None:
            return Verification(False, UNVERIFIED, compatibility.RECEIVER_VERIFY_FAILED, "the applied value did not decode")

        digest = handoff.pixel_digest(image)
        if digest and loaded.pixel_digest and digest == loaded.pixel_digest:
            return Verification(True, PIXEL_EQUIVALENT, receiver_pixel_digest=digest)
        if (image.width, image.height) != (loaded.width, loaded.height):
            return Verification(
                False,
                UNVERIFIED,
                compatibility.RECEIVER_VERIFY_FAILED,
                f"{image.width}x{image.height} applied, {loaded.width}x{loaded.height} sent",
                digest,
            )
        return Verification(False, UNVERIFIED, compatibility.RECEIVER_VERIFY_FAILED, "pixels differ", digest)

    def _verifiable_image(self, applied: Applied) -> typing.Any:
        return as_image(applied.value)

    # -- section 49.2 ---------------------------------------------------------

    def downstream_updates(
        self,
        state: "receiver_state.SessionState",
        applied: Applied,
    ) -> typing.Mapping[str, typing.Any]:
        """Extra component updates a human upload would have caused.

        Empty for every v1 adapter, and that emptiness is a claim that has to
        be tested rather than a fact that has been established.

        VERIFY ON A REAL INSTALL (section 49.2, per adapter): populate the
        receiver through the bridge, look at the visible UI, then press
        Generate *without touching the field*. If the generation does not use
        the inserted image, or a preview/dependent control stayed stale, the
        official downstream event goes here - returned as component updates
        from the same event, never simulated in the browser.
        """
        return {}

    # -- shared guards --------------------------------------------------------

    def _require(self, state: "receiver_state.SessionState", operation: typing.Any) -> None:
        if self.component() is None:
            raise compatibility.BridgeError(
                compatibility.BRIDGE_COMPONENT_INCOMPATIBLE,
                f"{self.logical_id} has no resolved component in this build",
            )
        receiver_state.require_offered(state, self.logical_id)
        if operation and operation != self.operation:
            # The descriptor said what this receiver does. A caller asking for
            # something else is out of date, and guessing which one it meant
            # is how references get deleted.
            raise compatibility.BridgeError(
                compatibility.RECEIVER_APPLY_FAILED,
                f"{self.logical_id} is {self.operation}, not {operation}",
            )


# ------------------------------------------------------------- v1 adapters --


class SingleImageAdapter(ReceiverAdapter):
    """A receiver holding exactly one picture, replaced outright.

    The base for start and end, and the base a control or style adapter would
    subclass: give it a logical id, a role and a row in
    ``compatibility.RECEIVER_COMPONENTS`` and it needs nothing else.
    """

    operation = protocol.REPLACE

    def apply(
        self,
        loaded: "handoff.LoadedHandoff",
        operation: str,
        state: "receiver_state.SessionState",
    ) -> Applied:
        self._require(state, operation)
        previous = 1 if receiver_state.value_present(self.snapshot(state)) else 0
        # The PIL image itself, not the handoff path. Gradio imports it into
        # its own cache on postprocess, which is section 20.7's "copy it into
        # the receiver's normal backing mechanism" - the receiver never ends
        # up pointing at a file another process is about to sweep. As a
        # one-element list when the receiver is a gallery, which is what
        # Wan2GP's start and end frames are - see ``gallery_shaped``.
        value: typing.Any = [loaded.image] if gallery_shaped(self.component()) else loaded.image
        return Applied(
            receiver_id=self.logical_id,
            role=self.role,
            operation=self.operation,
            component_key=self.component_key,
            value=value,
            previous_count=previous,
            new_count=1,
        )


class StartFrameAdapter(SingleImageAdapter):
    logical_id = protocol.START_FRAME
    role = "start"
    label = "Start Frame"


class EndFrameAdapter(SingleImageAdapter):
    logical_id = protocol.END_FRAME
    role = "end"
    label = "End Frame"


class GalleryAppendAdapter(ReceiverAdapter):
    """A receiver holding an ordered list that grows by one.

    The base for reference images, and for any later additive role. All of
    section 22 lives here: snapshot, capacity, append, order, update, count.
    """

    operation = protocol.APPEND
    fallback_max = compatibility.FALLBACK_REFERENCE_MAX

    def snapshot(self, state: "receiver_state.SessionState") -> typing.List[typing.Any]:
        return gallery_entries(state.value(self.component_key))

    def capacity(self, state: "receiver_state.SessionState") -> typing.Tuple[int, typing.Optional[int]]:
        """What WanGP says the limit is, or a finite one of our own.

        The declared limit is preferred in every case where WanGP declares
        one, because section 16.5 is explicit that the number belongs to the
        model. The fallback exists only so that "unbounded" never means "keep
        appending into a list nobody bounded"; it is not a claim about any
        model, and MiniPaint never sees it as anything but ``max_count``.
        """
        declared = self.compat.declared_capacity(self.logical_id)
        return (len(self.snapshot(state)), int(declared) if declared else self.fallback_max)

    def apply(
        self,
        loaded: "handoff.LoadedHandoff",
        operation: str,
        state: "receiver_state.SessionState",
    ) -> Applied:
        self._require(state, operation)

        existing = self.snapshot(state)
        count, max_count = self.capacity(state)
        if max_count is not None and count >= max_count:
            raise compatibility.BridgeError(
                compatibility.RECEIVER_LIMIT_REACHED,
                f"{self.logical_id} holds {count} of {max_count}",
            )

        # Order matters and so does identity: the existing entries are carried
        # across exactly as they were read, never re-derived, never re-encoded
        # and never sorted. The new one goes on the end.
        updated = list(existing)
        updated.append(_entry_for(existing, loaded.image))

        return Applied(
            receiver_id=self.logical_id,
            role=self.role,
            operation=self.operation,
            component_key=self.component_key,
            value=updated,
            previous_count=len(existing),
            new_count=len(updated),
        )

    def verify(self, loaded: "handoff.LoadedHandoff", applied: Applied) -> Verification:
        """The appended entry must be the image, and the list must be one longer."""
        if applied.new_count != applied.previous_count + 1:
            return Verification(
                False,
                UNVERIFIED,
                compatibility.RECEIVER_VERIFY_FAILED,
                f"{applied.previous_count} entries became {applied.new_count}",
            )
        return super().verify(loaded, applied)

    def _verifiable_image(self, applied: Applied) -> typing.Any:
        entries = gallery_entries(applied.value)
        return as_image(entries[-1]) if entries else None


class ReferenceImageAdapter(GalleryAppendAdapter):
    logical_id = protocol.REFERENCE
    role = "reference"
    label = "Reference Image"


# --------------------------------------------------------------- registry --

#: The adapters this version ships. Adding a receiver is three declarations
#: and no new mechanism: a row in ``compatibility.COMPONENTS``, a row in
#: ``RECEIVER_COMPONENTS`` and ``SELECTION_RULES``, and a subclass of
#: ``SingleImageAdapter`` or ``GalleryAppendAdapter`` listed here. Nothing in
#: MiniPaint changes, because it already knows every logical id in the
#: protocol and simply never sees the ones the bridge does not offer.
#:
#: Deliberately absent in v1: control_image, positioned_ref and style_ref.
#: They have no selection rule, so no state can report them active, and they
#: therefore cannot be sent to by accident before somebody has confirmed how
#: the installed WanGP exposes them.
ADAPTER_TYPES: typing.Tuple[type, ...] = (StartFrameAdapter, EndFrameAdapter, ReferenceImageAdapter)


def build_adapters(compat: "compatibility.Compatibility") -> "typing.Dict[str, ReceiverAdapter]":
    """One adapter per shipped receiver, in protocol order."""
    made = {adapter_type.logical_id: adapter_type(compat) for adapter_type in ADAPTER_TYPES}
    return {key: made[key] for key in protocol.RECEIVER_IDS if key in made}


def describe(
    adapters: "typing.Mapping[str, ReceiverAdapter]",
    state: "receiver_state.SessionState",
    include_inactive: bool = False,
) -> typing.List[dict]:
    """The receiver list for one page, normalised the way MiniPaint will read it.

    Run through ``protocol.normalize_receivers`` here rather than on the far
    side so that a descriptor this bridge cannot fully describe is dropped
    where the reason is still visible, and so that the order is the protocol's
    order rather than the dictionary's.
    """
    raw = []
    for receiver_id in protocol.RECEIVER_IDS:
        adapter = adapters.get(receiver_id)
        if adapter is None:
            continue
        descriptor = adapter.can_describe(state)
        if descriptor is None:
            continue
        if descriptor.get("enabled") or include_inactive:
            raw.append(descriptor)
    return protocol.normalize_receivers(raw)
