"""Everything this plugin knows about WanGP's insides, in one file.

The rest of the bridge is written as if WanGP had no component ids, no
setting names and no flag letters. It does, of course, and they move between
releases - which is the whole reason the integration exists in this shape at
all. So they live here, declared as *data*: a table of candidate element ids
per logical thing we need, each candidate carrying the WanGP versions it is
known for and whether the bridge can work without it. Resolution happens
through the documented plugin API (``request_component`` / ``request_global``
during ``setup_ui``, read back after ``post_ui_setup``); nothing here reaches
into a module global, and nothing outside this file names a WanGP identifier.

The other half of the file is the failure vocabulary. The codes are the same
strings ``minipaint_neo/wangp/errors.py`` defines, spelled out again rather
than imported, because a plugin living in WanGP's environment may import
nothing of the extension's. They are the words the acknowledgement speaks, so
they must match exactly; a code invented here would reach the Forge side as
"unknown" and be reported as an internal error.

The rule the whole file serves is fail-closed. When a mandatory v1 component
cannot be resolved the handshake says ``ready=false`` with
``BRIDGE_COMPONENT_INCOMPATIBLE`` and the Send menu offers nothing. It never
falls back to a guess, a nearby-looking component or a DOM selector: an input
that cannot be proven to be the one the user is looking at is not an input we
are willing to put their picture into.
"""

from __future__ import annotations

import dataclasses
import os
import typing

try:  # the plugin folder is loaded both as a package and as a bare directory
    from . import protocol
except ImportError:  # pragma: no cover - depends on how WanGP imports plugins
    import protocol  # type: ignore[no-redef]


BRIDGE_VERSION = "1.1.3"

#: The early filter, and only the early filter. Section 14.2: a version string
#: alone never proves compatibility - functional resolution does - but a build
#: outside this range is worth naming before anything else goes wrong.
WAN2GP_MIN_VERSION = "8.0"
WAN2GP_MAX_VERSION = ""  # empty means "no known upper bound yet"


# ------------------------------------------------------------ failure codes --
# Duplicated from minipaint_neo/wangp/errors.py; the Forge side matches on the
# exact string, so these are contract, not convenience.

BRIDGE_COMPONENT_INCOMPATIBLE = "BRIDGE_COMPONENT_INCOMPATIBLE"
BRIDGE_SESSION_MISMATCH = "BRIDGE_SESSION_MISMATCH"
NO_ACTIVE_RECEIVER = "NO_ACTIVE_RECEIVER"
UNKNOWN_RECEIVER = "UNKNOWN_RECEIVER"
STALE_RECEIVER_STATE = "STALE_RECEIVER_STATE"
RECEIVER_DISABLED = "RECEIVER_DISABLED"
RECEIVER_LIMIT_REACHED = "RECEIVER_LIMIT_REACHED"
RECEIVER_APPLY_FAILED = "RECEIVER_APPLY_FAILED"
RECEIVER_VERIFY_FAILED = "RECEIVER_VERIFY_FAILED"
HANDOFF_NOT_FOUND = "HANDOFF_NOT_FOUND"
HANDOFF_INVALID_ID = "HANDOFF_INVALID_ID"
HANDOFF_INVALID_IMAGE = "HANDOFF_INVALID_IMAGE"
HANDOFF_TOO_LARGE = "HANDOFF_TOO_LARGE"
HANDOFF_DIGEST_MISMATCH = "HANDOFF_DIGEST_MISMATCH"
INTERNAL_ERROR = "INTERNAL_ERROR"


class BridgeError(Exception):
    """A refusal with a code the Forge side already understands.

    ``detail`` is for the WanGP console and the diagnostics report. It never
    travels in an acknowledgement, because it is the one field likely to
    contain a path.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = str(detail or "")
        super().__init__(f"{code}: {self.detail}" if self.detail else code)


# ---------------------------------------------------------- the id tables --


@dataclasses.dataclass(frozen=True)
class ComponentSpec:
    """One thing the bridge needs out of WanGP's UI.

    ``candidates`` are tried in order and the first that resolves wins, so the
    current release's id goes first and older spellings follow. ``known_for``
    is documentation, not logic - it is what a maintainer reads when a
    candidate stops resolving.
    """

    key: str
    candidates: typing.Tuple[str, ...]
    kind: str
    mandatory: bool
    known_for: str
    note: str = ""


#: Component keys. The rest of the bridge uses these; only this file knows
#: which WanGP element each one is.
START_IMAGE = "start_image"
END_IMAGE = "end_image"
REFERENCE_GALLERY = "reference_gallery"
IMAGE_PROMPT_TYPE = "image_prompt_type"
VIDEO_PROMPT_TYPE = "video_prompt_type"
AUDIO_PROMPT_TYPE = "audio_prompt_type"
MODEL_MODE = "model_mode"
MODEL_SELECTOR = "model_selector"
#: Everything below is optional: what a send may *switch*, and what it needs
#: to know whether switching is allowed. A build that hands none of them over
#: behaves exactly as before - a receiver is offered only while WanGP's own
#: selector already has it switched on.
IMAGE_MODE = "image_mode"
SESSION_STATE = "session_state"
IMAGE_PROMPT_RADIO = "image_prompt_radio"
END_IMAGES_CHECKBOX = "end_images_checkbox"
REFERENCE_SELECTOR = "reference_selector"
START_ROW = "start_row"
END_ROW = "end_row"
REFERENCE_ROW = "reference_row"

# VERIFY ON A REAL INSTALL (section 49.1): every elem_id below was taken from
# WanGP's documented media-input naming and from the receiver example in
# section 16.2 of the design intent, not from a running build. Before this
# ships, open the target WanGP revision, list the elem_ids its generator view
# actually registers, and correct the first candidate of each row. The bridge
# is written so that this is the only file that has to change.
COMPONENTS: typing.Tuple[ComponentSpec, ...] = (
    ComponentSpec(
        key=START_IMAGE,
        candidates=("image_start", "image_prompt_start", "start_image"),
        kind="image",
        mandatory=True,
        known_for="Wan2GP 8.x media_inputs.image.start",
    ),
    ComponentSpec(
        key=END_IMAGE,
        candidates=("image_end", "image_prompt_end", "end_image"),
        kind="image",
        mandatory=True,
        known_for="Wan2GP 8.x media_inputs.image.end",
    ),
    ComponentSpec(
        key=REFERENCE_GALLERY,
        candidates=("image_refs", "image_references", "reference_images"),
        kind="gallery",
        mandatory=True,
        known_for="Wan2GP 8.x media_inputs.image.reference",
        note="value shape varies by Gradio version; receiver_adapters normalises it",
    ),
    ComponentSpec(
        key=IMAGE_PROMPT_TYPE,
        candidates=("image_prompt_type",),
        kind="selection",
        mandatory=True,
        known_for="Wan2GP 8.x, documented as a normalised setting",
        note="the live switch that decides whether start/end are in play at all",
    ),
    ComponentSpec(
        key=VIDEO_PROMPT_TYPE,
        candidates=("video_prompt_type",),
        kind="selection",
        mandatory=True,
        known_for="Wan2GP 8.x, documented as a normalised setting",
        note="carries the reference-image selection among other things",
    ),
    ComponentSpec(
        key=AUDIO_PROMPT_TYPE,
        candidates=("audio_prompt_type",),
        kind="selection",
        mandatory=False,
        known_for="present on models with audio conditioning only",
        note="read when present because some builds gate image roles behind it",
    ),
    ComponentSpec(
        key=MODEL_MODE,
        candidates=("model_mode", "mode_selector", "model_variant"),
        kind="selection",
        mandatory=False,
        known_for="model-specific mode selector; absent on many models",
    ),
    ComponentSpec(
        key=MODEL_SELECTOR,
        # Never "model_type": that is the name of a *global* this plugin also
        # asks for, and a host that answers both from one mapping would hand
        # back the model's name where a component belongs.
        candidates=("model_list", "model_choice", "model_selector", "model_type_selector"),
        kind="selection",
        mandatory=False,
        known_for="the model dropdown itself; read so the model is a fact about this page",
    ),
    # -- what a send may switch, and what decides whether it may ------------
    # Wan2GP hands plugins the generator form's own locals by name
    # (``locals_dict = locals()`` in wgp.py, then
    # ``run_component_insertion_and_setup``), so these are the variable names
    # of that form. Confirmed against Wan2GP at 362c346.
    ComponentSpec(
        key=IMAGE_MODE,
        candidates=("image_mode",),
        kind="selection",
        mandatory=False,
        known_for="Wan2GP: hidden gr.Number, 0 = video output, 1/2 = image output",
        note="in image-output mode Wan2GP strips S/E/V/L from what the model allows",
    ),
    ComponentSpec(
        key=SESSION_STATE,
        candidates=("state",),
        kind="state",
        mandatory=False,
        known_for="Wan2GP: the per-page gr.State; get_state_model_type(state) is this page's model",
    ),
    ComponentSpec(
        key=IMAGE_PROMPT_RADIO,
        candidates=("image_prompt_type_radio",),
        kind="selection",
        mandatory=False,
        known_for="Wan2GP: the Location radio; its .change recomputes image_prompt_type and shows the start row",
    ),
    ComponentSpec(
        key=END_IMAGES_CHECKBOX,
        candidates=("image_prompt_type_endcheckbox",),
        kind="selection",
        mandatory=False,
        known_for="Wan2GP: the End Image(s) checkbox; its .change adds E and shows the end row",
    ),
    ComponentSpec(
        key=REFERENCE_SELECTOR,
        candidates=("video_prompt_type_image_refs",),
        kind="selection",
        mandatory=False,
        known_for="Wan2GP: the reference-images dropdown; wired with .input, so a value set from the server does not run its handler",
    ),
    ComponentSpec(key=START_ROW, candidates=("image_start_row",), kind="container", mandatory=False,
                  known_for="Wan2GP: the row holding image_start"),
    ComponentSpec(key=END_ROW, candidates=("image_end_row",), kind="container", mandatory=False,
                  known_for="Wan2GP: the row holding image_end"),
    ComponentSpec(key=REFERENCE_ROW, candidates=("image_refs_row",), kind="container", mandatory=False,
                  known_for="Wan2GP: the row holding image_refs"),
)

COMPONENTS_BY_KEY = {spec.key: spec for spec in COMPONENTS}

#: Globals asked for through ``request_global``. These are session/process
#: facts rather than widgets, and the plugin API is the only sanctioned way to
#: see them.
GLOBALS: typing.Tuple[str, ...] = (
    "model_type", "model_def", "state", "server_config", "wan2gp_version", "active_view",
    # Wan2GP passes its whole module namespace to ``inject_globals`` and sets
    # each requested name as an attribute on the plugin, so functions can be
    # asked for too. These two are how Wan2GP's own handlers find the model
    # a page is on and what that model allows.
    "get_model_def", "get_state_model_type",
)

#: What a send switches on for a receiver that the model allows but the page
#: has not selected. Short tokens, carried in the receiver descriptor so the
#: menu can say a send will change the Location, and in the acknowledgement
#: so the log says it did.
SWITCH_TOKENS: typing.Mapping[str, str] = {
    protocol.START_FRAME: "location",
    protocol.END_FRAME: "end_images",
    protocol.REFERENCE: "reference_images",
}


@dataclasses.dataclass(frozen=True)
class SelectionRule:
    """How a live selection value answers "is this receiver switched on?".

    WanGP encodes several independent choices as letters in one string -
    ``image_prompt_type`` and ``video_prompt_type`` both work that way - so
    the test is membership of a token, and a rule that finds no source value
    at all is *not* satisfied. That asymmetry is the fail-closed rule in
    miniature: a receiver is active only when something says so.
    """

    component_key: str
    tokens: typing.Tuple[str, ...]
    known_for: str

    def satisfied_by(self, value: typing.Any) -> bool:
        if value is None:
            return False
        text = value if isinstance(value, str) else str(value)
        return any(token in text for token in self.tokens)


# VERIFY ON A REAL INSTALL: the flag letters. WanGP packs the start/end choice
# into image_prompt_type and the reference-image choice into
# video_prompt_type. Confirm the exact letters in the target revision by
# switching the visible selector and printing the value; a wrong letter here
# does not corrupt anything, it makes a receiver look permanently inactive.
SELECTION_RULES: typing.Mapping[str, SelectionRule] = {
    protocol.START_FRAME: SelectionRule(IMAGE_PROMPT_TYPE, ("S",), "Wan2GP 8.x"),
    protocol.END_FRAME: SelectionRule(IMAGE_PROMPT_TYPE, ("E",), "Wan2GP 8.x"),
    protocol.REFERENCE: SelectionRule(VIDEO_PROMPT_TYPE, ("I",), "Wan2GP 8.x"),
    # Extension point. control_image, positioned_ref and style_ref are not in
    # v1: no rule is declared for them, so nothing can report them active, and
    # adding one here plus an adapter is the whole of adding the receiver.
}

#: Where the generic model schema says a capability *could* exist. Layer A of
#: section 25 - what the model allows, before any live value is consulted.
#: Paths are walked defensively; a build that exposes none of them simply
#: leaves every capability unknown, and layer B decides on its own.
CAPABILITY_PATHS: typing.Mapping[str, typing.Tuple[str, ...]] = {
    protocol.START_FRAME: ("media_inputs", "image", "start"),
    protocol.END_FRAME: ("media_inputs", "image", "end"),
    protocol.REFERENCE: ("media_inputs", "image", "reference"),
    protocol.CONTROL_IMAGE: ("media_inputs", "image", "control"),
    protocol.POSITIONED_REF: ("media_inputs", "image", "background"),
    protocol.STYLE_REF: ("media_inputs", "image", "style"),
}

#: Which component a receiver reads and writes. One place, so an adapter never
#: spells a component key it did not get from here.
RECEIVER_COMPONENTS: typing.Mapping[str, str] = {
    protocol.START_FRAME: START_IMAGE,
    protocol.END_FRAME: END_IMAGE,
    protocol.REFERENCE: REFERENCE_GALLERY,
}

#: The v1 receivers. A build that cannot resolve the components behind these
#: is incompatible; anything else is merely unavailable.
V1_RECEIVERS: typing.Tuple[str, ...] = (protocol.START_FRAME, protocol.END_FRAME, protocol.REFERENCE)

#: A ceiling used only when WanGP does not state one. Section 16.5 forbids
#: MiniPaint from knowing a model's reference limit; it does not forbid the
#: bridge from refusing to append forever into a list nobody bounded.
FALLBACK_REFERENCE_MAX = 16

#: Where the reference limit is published when it is published at all.
CAPACITY_PATHS: typing.Mapping[str, typing.Tuple[typing.Tuple[str, ...], ...]] = {
    protocol.REFERENCE: (
        ("media_inputs", "image", "reference_max"),
        ("media_inputs", "image", "max_reference"),
        ("max_reference_images",),
    ),
}


# ------------------------------------------------------------ environment --

ENV_INSTANCE_ID = "MINIPAINT_WANGP_INSTANCE_ID"
ENV_HANDOFF_ROOT = "MINIPAINT_WANGP_HANDOFF_ROOT"
ENV_BRIDGE_SECRET = "MINIPAINT_WANGP_BRIDGE_SECRET"


def instance_id(environ: typing.Optional[typing.Mapping[str, str]] = None) -> str:
    """The runtime instance this WanGP child was launched as, or "".

    An empty answer means WanGP is running standalone rather than under the
    Forge integration, which is a perfectly normal thing for it to do; the
    bridge then stays quiet instead of announcing a session nobody asked for.
    """
    source = os.environ if environ is None else environ
    return str(source.get(ENV_INSTANCE_ID) or "").strip()


def managed(environ: typing.Optional[typing.Mapping[str, str]] = None) -> bool:
    """Whether this process was started by the Mini Paint integration."""
    source = os.environ if environ is None else environ
    return bool(instance_id(source)) and bool(str(source.get(ENV_HANDOFF_ROOT) or "").strip())


# --------------------------------------------------------------- versions --


def _version_tuple(value: typing.Any) -> typing.Tuple[int, ...]:
    """A comparable version, best effort. Unparseable parts stop the tuple."""
    parts: typing.List[int] = []
    for chunk in str(value or "").strip().split("."):
        digits = ""
        for character in chunk:
            if not character.isdigit():
                break
            digits += character
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def version_supported(version: typing.Any, minimum: str = WAN2GP_MIN_VERSION, maximum: str = WAN2GP_MAX_VERSION) -> bool:
    """The early filter of section 14.2.

    An unreadable or absent WanGP version is *not* treated as a failure: the
    functional checks that follow are the real test, and refusing to run
    because a version string could not be parsed would fail closed against the
    wrong thing.
    """
    found = _version_tuple(version)
    if not found:
        return True
    if minimum and found < _version_tuple(minimum):
        return False
    if maximum and found > _version_tuple(maximum):
        return False
    return True


# --------------------------------------------------------- the plugin base --


#: Where WanGP's plugin base class lives. The first entry is the real one -
#: ``shared/utils/plugins.py`` is the module whose loader imports us - and the
#: rest are kept for a build that moves it. Getting this wrong is silent by
#: construction on WanGP's side, so it is worth being explicit about.
PLUGIN_BASE_CANDIDATES: typing.Tuple[typing.Tuple[str, str], ...] = (
    ("shared.utils.plugins", "WAN2GPPlugin"),
    ("shared.utils.plugin", "WAN2GPPlugin"),
    ("shared.plugins", "WAN2GPPlugin"),
    ("wan2gp_plugin", "WAN2GPPlugin"),
    ("plugins.plugin_base", "WAN2GPPlugin"),
    ("plugins.base", "WAN2GPPlugin"),
    ("shared.plugin", "WAN2GPPlugin"),
    ("wgp_plugin", "WAN2GPPlugin"),
)


def plugin_base() -> type:
    """WanGP's plugin class, or a stand-in with the same shape.

    The stand-in exists so this folder can be imported, read and unit-tested
    anywhere - on a machine with no WanGP at all - and so that a WanGP whose
    plugin module moved produces an ordinary "bridge did not load" rather than
    an import-time crash inside someone else's application. The fallback is
    deliberately inert: it defines the hook names and does nothing, so a
    plugin built on it never pretends to have wired anything up.
    """
    for module_name, attribute in PLUGIN_BASE_CANDIDATES:
        try:
            module = __import__(module_name, fromlist=[attribute])
            candidate = getattr(module, attribute, None)
        except Exception:
            continue
        if isinstance(candidate, type):
            return candidate

    # Landing here means every subclass hook below is attached to a class
    # WanGP has never heard of. Its loader finds plugins with
    # ``issubclass(obj, WAN2GPPlugin)`` and skips anything else without a
    # word, so this would otherwise be a plugin that installs, enables,
    # appears in the Plugins tab and simply never runs. Say so where the log
    # will pick it up - WanGP's stdout is read by the tab that installed us.
    print(
        "[MiniPaint bridge] WanGP's plugin base class was not found in any of "
        + ", ".join(name for name, _ in PLUGIN_BASE_CANDIDATES)
        + ". The bridge cannot be loaded by this WanGP build: its loader only "
        "accepts a subclass of WAN2GPPlugin. Nothing else about WanGP is affected."
    )
    return FallbackPlugin


class FallbackPlugin:
    """The shape of a WanGP plugin, with none of the behaviour.

    VERIFY ON A REAL INSTALL: the hook names below are the ones section 3.2
    lists as confirmed. If the installed WanGP calls them with different
    signatures, the real base class is in charge and this class is never used
    - but the bridge's own methods must still match, so check the signatures
    once against the target revision.
    """

    name = "minipaint-bridge"

    def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        self.plugin_args = args
        self.plugin_kwargs = kwargs

    def setup_ui(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        return None

    def post_ui_setup(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        return None

    def request_component(self, elem_id: str) -> None:
        return None

    def request_global(self, name: str) -> None:
        return None

    def on_model_change(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        return None

    def add_custom_js(self, script: str) -> None:
        return None


# ------------------------------------------------------------------ host --


class Host:
    """A stable face for whatever the installed WanGP plugin API looks like.

    Every call goes through a small list of plausible method names and returns
    None rather than raising when none of them exist. That is not defensive
    padding: the bridge's answer to "I could not reach that" is a specific
    handshake failure, and it can only produce one if the lookup itself is
    allowed to come back empty.

    ``owner`` is the live plugin object in production and a stub in a test,
    which is why nothing here is a module-level function.
    """

    _REQUEST_COMPONENT = ("request_component", "requestComponent", "want_component")
    _READ_COMPONENT = ("get_component", "component", "resolve_component")
    _REQUEST_GLOBAL = ("request_global", "requestGlobal", "want_global")
    _READ_GLOBAL = ("get_global", "global_value", "resolve_global")

    def __init__(self, owner: typing.Any = None) -> None:
        self.owner = owner
        #: What ``post_ui_setup`` was handed, keyed by elem_id.
        self.handed: typing.Dict[str, typing.Any] = {}
        #: The globals asked for, by name: the only ones ``read_global`` will
        #: take off the plugin object itself.
        self.requested_globals: typing.Set[str] = set()

    def _call(self, names: typing.Sequence[str], *args: typing.Any) -> typing.Any:
        for name in names:
            method = getattr(self.owner, name, None)
            if not callable(method):
                continue
            try:
                return method(*args)
            except Exception:
                # A plugin API that raises on an unknown id is normal; it means
                # "not this one", and the next candidate gets its turn.
                return None
        return None

    def request_component(self, elem_id: str) -> typing.Any:
        return self._call(self._REQUEST_COMPONENT, elem_id)

    @staticmethod
    def is_component(value: typing.Any) -> bool:
        """Whether this is really a Gradio component and not something named
        like one.

        Gradio identifies a block by ``_id`` and builds an event's config from
        exactly that attribute, so anything without it cannot be an input or
        an output - it raises inside Gradio, during WanGP's own UI build,
        where the blame lands on WanGP. One value that is not a component
        reached an event this way and took the whole application into safe
        mode, so the shape is checked here rather than assumed anywhere.
        """
        return value is not None and hasattr(value, "_id") and hasattr(value, "get_config")

    def accept_components(self, handed: typing.Any) -> int:
        """Take the mapping ``post_ui_setup`` was called with. Returns its size.

        This is how the components actually arrive: WanGP resolves what was
        asked for in ``setup_ui`` and passes the lot as the argument to
        ``post_ui_setup``. Reading them off the plugin object instead - which
        is what this did - finds nothing at all, and every receiver then looks
        missing however right its elem_id was.
        """
        if not isinstance(handed, dict):
            return 0
        # A host may hand back globals and components in one mapping - the
        # names overlap - so only what is really a component is kept.
        self.handed.update(
            {str(key): value for key, value in handed.items() if self.is_component(value)}
        )
        return len(self.handed)

    def read_component(self, elem_id: str) -> typing.Any:
        # What post_ui_setup was handed, first: it is the documented route and
        # the only one that needs nothing of the host but the call itself.
        if elem_id in self.handed:
            return self.handed[elem_id]
        found = self._call(self._READ_COMPONENT, elem_id)
        if self.is_component(found):
            return found
        # A host that keeps them on the plugin instead is still understood.
        for name in ("components", "requested_components", "resolved_components"):
            mapping = getattr(self.owner, name, None)
            if isinstance(mapping, dict) and self.is_component(mapping.get(elem_id)):
                return mapping[elem_id]
        return None

    def request_global(self, name: str) -> typing.Any:
        self.requested_globals.add(str(name))
        return self._call(self._REQUEST_GLOBAL, name)

    def read_global(self, name: str) -> typing.Any:
        found = self._call(self._READ_GLOBAL, name)
        if found is not None:
            return found
        # Wan2GP's own route (shared/utils/plugins.py, inject_globals): every
        # requested global is set as an attribute of that name on the plugin
        # object, and a restricted one is set to None. Read here rather than
        # through a method the API does not have, and only for a name that was
        # asked for - a plugin attribute that merely shares a name is not a
        # WanGP global.
        if name in self.requested_globals:
            found = getattr(self.owner, name, None)
            if found is not None:
                return found
        for attribute in ("globals", "requested_globals", "resolved_globals"):
            mapping = getattr(self.owner, attribute, None)
            if isinstance(mapping, dict) and name in mapping:
                return mapping[name]
        return None


# ------------------------------------------------------------ resolution --


@dataclasses.dataclass(frozen=True)
class Resolution:
    """What this build turned out to have."""

    components: typing.Mapping[str, typing.Any]
    elem_ids: typing.Mapping[str, str]
    missing_mandatory: typing.Tuple[str, ...]
    missing_optional: typing.Tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.missing_mandatory

    def component(self, key: str) -> typing.Any:
        return self.components.get(key)

    def elem_id(self, key: str) -> str:
        return str(self.elem_ids.get(key) or "")


EMPTY_RESOLUTION = Resolution(
    components={},
    elem_ids={},
    missing_mandatory=tuple(spec.key for spec in COMPONENTS if spec.mandatory),
    missing_optional=tuple(spec.key for spec in COMPONENTS if not spec.mandatory),
)


class Compatibility:
    """The bridge's opinion of the WanGP it woke up inside.

    Two phases, because the plugin API has two: ``declare`` runs while WanGP
    is building its UI and only *asks* for things, and ``resolve`` runs after
    ``post_ui_setup`` and records what actually arrived. Splitting them keeps
    the ordering requirement of the host's API out of every other module.
    """

    def __init__(
        self,
        host: typing.Optional[Host] = None,
        specs: typing.Sequence[ComponentSpec] = COMPONENTS,
        global_names: typing.Sequence[str] = GLOBALS,
        environ: typing.Optional[typing.Mapping[str, str]] = None,
    ) -> None:
        self.host = host if host is not None else Host(None)
        self.specs = tuple(specs)
        self.global_names = tuple(global_names)
        self.environ = os.environ if environ is None else environ
        self.resolution = EMPTY_RESOLUTION
        self.wan2gp_version = ""
        self.notes: typing.List[str] = []

    # -- phase one ----------------------------------------------------------

    def declare(self) -> typing.List[str]:
        """Ask for every candidate id and every global. Returns what was asked.

        Every candidate is requested, not just the first: the host cannot tell
        us which one exists until later, and asking for an id a build does not
        have costs nothing.
        """
        asked: typing.List[str] = []
        for spec in self.specs:
            for elem_id in spec.candidates:
                self.host.request_component(elem_id)
                asked.append(elem_id)
        self.declare_globals()
        return asked

    def declare_globals(self) -> typing.List[str]:
        """Ask for the globals. Safe to call more than once, and early.

        Wan2GP injects globals once, right after it constructs the plugin
        (``initialize_plugins``: load, then ``inject_globals``) and long before
        ``setup_ui`` runs - so a global first asked for in ``setup_ui`` is
        never delivered, and every ``read_global`` answers None. The plugin
        calls this from ``__init__`` for that reason; ``declare`` calls it
        again because asking twice costs nothing and a host that injects
        later still gets the request.
        """
        for name in self.global_names:
            self.host.request_global(name)
        return list(self.global_names)

    # -- phase two ----------------------------------------------------------

    def resolve(self) -> Resolution:
        """Record which candidates the host handed back."""
        components: typing.Dict[str, typing.Any] = {}
        elem_ids: typing.Dict[str, str] = {}
        missing_mandatory: typing.List[str] = []
        missing_optional: typing.List[str] = []

        for spec in self.specs:
            found = None
            for elem_id in spec.candidates:
                candidate = self.host.read_component(elem_id)
                if candidate is not None:
                    found, elem_ids[spec.key] = candidate, elem_id
                    break
            if found is None:
                (missing_mandatory if spec.mandatory else missing_optional).append(spec.key)
                continue
            components[spec.key] = found

        self.wan2gp_version = str(self.host.read_global("wan2gp_version") or "")
        self.resolution = Resolution(
            components=components,
            elem_ids=elem_ids,
            missing_mandatory=tuple(missing_mandatory),
            missing_optional=tuple(missing_optional),
        )
        return self.resolution

    def forget(self) -> None:
        """Drop cached model-derived facts. Called from ``on_model_change``.

        The resolution itself survives: components are built once with the UI
        and do not change when a model does. What does change is the static
        schema, and section 14.6 is explicit that the model-change hook is
        good for exactly this and not for knowing live choices.
        """
        self.notes = []

    # -- what other modules ask ---------------------------------------------

    def component(self, key: str) -> typing.Any:
        return self.resolution.component(key)

    def receiver_component(self, receiver_id: str) -> typing.Any:
        key = RECEIVER_COMPONENTS.get(receiver_id, "")
        return self.resolution.component(key) if key else None

    def receiver_elem_id(self, receiver_id: str) -> str:
        """The focus hint for a receiver: an id we resolved, never a selector."""
        key = RECEIVER_COMPONENTS.get(receiver_id, "")
        return self.resolution.elem_id(key) if key else ""

    def output_components(self) -> typing.List[typing.Any]:
        """The v1 receiver components, in receiver order, for a Gradio event.

        The order is the contract between ``plugin.py`` and the adapters: an
        apply returns one value per entry of this list, and ``gr.update()`` -
        "no change" - for every entry it is not addressing.
        """
        found: typing.List[typing.Any] = []
        for receiver_id in V1_RECEIVERS:
            component = self.receiver_component(receiver_id)
            if component is not None:
                found.append(component)
        return found

    def output_receivers(self) -> typing.List[str]:
        """The receiver ids matching ``output_components`` position for position."""
        return [receiver_id for receiver_id in V1_RECEIVERS if self.receiver_component(receiver_id) is not None]

    def state_components(self) -> typing.List[typing.Tuple[str, typing.Any]]:
        """The live values a receiver decision depends on, as (key, component).

        These become the inputs of the bridge event, which is how the state
        fingerprint gets read from the *session* rather than from a global:
        Gradio hands a callback the values belonging to the page that fired
        it, and there is no other source that is true of one browser tab.
        """
        keys = (
            IMAGE_PROMPT_TYPE, VIDEO_PROMPT_TYPE, AUDIO_PROMPT_TYPE, MODEL_MODE,
            REFERENCE_GALLERY, START_IMAGE, END_IMAGE,
            # The model belongs here for the same reason as the rest of them.
            # Read through ``request_global`` it is one value for the whole
            # process, so two browser tabs on two models would share whichever
            # the server saw last - section 13.2's warning exactly, and what
            # section 44 tests when it changes the model in tab A and expects
            # tab B's menu not to move.
            MODEL_SELECTOR,
            # What the model *allows* depends on the output mode and on the
            # model this page is on, and both are session values too.
            IMAGE_MODE, SESSION_STATE,
        )
        return [(key, self.resolution.component(key)) for key in keys if self.resolution.component(key) is not None]

    def switch_components(self) -> typing.List[typing.Tuple[str, typing.Any]]:
        """The components a send may update besides the receiver, as (key, component).

        In a fixed order, because they become the tail of the bridge event's
        outputs and ``plugin.py`` places each update by position. Only the
        ones this build resolved: an output that is not a component is the
        crash ``Host.is_component`` exists to prevent.
        """
        keys = (
            IMAGE_PROMPT_TYPE, VIDEO_PROMPT_TYPE,
            IMAGE_PROMPT_RADIO, END_IMAGES_CHECKBOX, REFERENCE_SELECTOR,
            START_ROW, END_ROW, REFERENCE_ROW,
        )
        return [(key, self.resolution.component(key)) for key in keys if self.resolution.component(key) is not None]

    # -- layer A -------------------------------------------------------------

    def model_capabilities(
        self, schema: typing.Any = None, selected: typing.Any = None
    ) -> typing.Dict[str, typing.Optional[bool]]:
        """Which image roles the current model's schema allows.

        Three answers, not two: True (the schema says yes), False (the schema
        says no) and None (this build does not publish the fact). None is not
        "no" - refusing every receiver because a build stopped publishing a
        metadata key would break the integration on an upgrade that changed
        nothing that matters.
        """
        if schema is None and not self.selection_is_current(selected):
            # This page is on a different model from the one the process-wide
            # schema describes, and the plugin API offers no way to ask for
            # another model's definition. Answering from the other page's
            # schema would be worse than answering nothing: None leaves the
            # decision to the live selectors, and those really are this page's.
            return {receiver_id: None for receiver_id in CAPABILITY_PATHS}
        source = schema if schema is not None else self.host.read_global("model_def")
        answers: typing.Dict[str, typing.Optional[bool]] = {}
        for receiver_id, path in CAPABILITY_PATHS.items():
            answers[receiver_id] = _walk_flag(source, path)
        return answers

    def declared_capacity(self, receiver_id: str, schema: typing.Any = None) -> typing.Optional[int]:
        """The maximum WanGP publishes for a receiver, or None if it publishes none."""
        source = schema if schema is not None else self.host.read_global("model_def")
        for path in CAPACITY_PATHS.get(receiver_id, ()):
            value = _walk(source, path)
            if isinstance(value, bool):
                continue
            if isinstance(value, int) and value > 0:
                return value
            if isinstance(value, float) and value > 0:
                return int(value)
        return None

    def selection_is_current(self, selected: typing.Any) -> bool:
        """Whether this page's model is the one the process globals describe.

        Deliberately loose about shape, and deliberately biased towards True.
        A dropdown may hand back the model type, a (label, value) pair or a
        list of one, and which half of a pair is the model is not something
        this can know - so any scalar in the selection matching counts as
        agreement, and only a selection that matches nothing is treated as
        another model. Erring this way costs a page on another model the
        schema layer, which is recoverable; erring the other way would let one
        page read another page's schema, which is what section 13.2 forbids.
        An unresolved selector answers True: there is nothing to disagree with,
        and behaviour is then exactly what it was before it was read at all.
        """
        chosen = [_short(item) for item in _scalars(selected) if _short(item)]
        if not chosen:
            return True
        return _short(self.host.read_global("model_type")) in chosen

    def model_descriptor(
        self,
        selected: typing.Any = None,
        definition: typing.Any = None,
        model_type: typing.Any = "",
    ) -> typing.Dict[str, str]:
        """The model block of the receiver answer: type, label, family.

        Strings only, and only the three the protocol names. The bridge does
        not publish a model database and MiniPaint does not keep one; this is
        for the diagnostics line and the send log. A type and definition
        already read for this page (``page_model``) are used as given; the
        process-wide globals are the fallback for a page that agrees with them.
        """
        current = self.selection_is_current(selected)
        if not model_type:
            model_type = self.host.read_global("model_type") if current else _first_scalar(selected)
        if not isinstance(definition, dict):
            definition = self.host.read_global("model_def") if current else None
        label = _walk(definition, ("name",)) or _walk(definition, ("label",))
        family = _walk(definition, ("family",)) or _walk(definition, ("architecture",))
        return {
            "type": _short(model_type),
            "label": _short(label) or _short(model_type),
            "family": _short(family),
        }

    # -- what this page's model allows: layer A, read the way Wan2GP reads it --

    def page_model(self, live: typing.Mapping[str, typing.Any]) -> typing.Tuple[str, typing.Optional[dict]]:
        """``(model_type, model_def)`` for the page whose event supplied ``live``.

        The same two steps Wan2GP's own handlers take: the model type out of
        this page's ``state`` through ``get_state_model_type``, then its
        definition through ``get_model_def``. Both are globals this plugin
        asked for; a build that hands over neither leaves the definition None,
        and None means "unknown", never "allowed". The dropdown value is the
        fallback for the type, and the process-wide globals the fallback for
        a page that agrees with them - which is exactly what they were before.
        """
        model_type = ""
        reader = self.host.read_global("get_state_model_type")
        state = live.get(SESSION_STATE)
        if callable(reader) and isinstance(state, dict):
            try:
                model_type = _short(reader(state))
            except Exception:
                model_type = ""
        if not model_type:
            model_type = _short(_first_scalar(live.get(MODEL_SELECTOR)))
        current = self.selection_is_current(live.get(MODEL_SELECTOR))
        if not model_type and current:
            model_type = _short(self.host.read_global("model_type"))

        definition: typing.Any = None
        getter = self.host.read_global("get_model_def")
        if callable(getter) and model_type:
            try:
                definition = getter(model_type)
            except Exception:
                definition = None
        if not isinstance(definition, dict) and current:
            definition = self.host.read_global("model_def")
        return model_type, definition if isinstance(definition, dict) else None

    def allowances(self, live: typing.Mapping[str, typing.Any]) -> typing.Dict[str, typing.Optional[bool]]:
        """Which receivers this page's model could take if its selector were set.

        True, False or None per receiver, and None is "not known", which the
        rest of the bridge treats exactly like False: an input is offered
        beyond the live selection only when the definition says the model
        takes it *and* this build handed over the control a send would have
        to switch. The rules are Wan2GP's own (wgp.py, generate_video_tab):

        * start: ``S`` in ``image_prompt_types_allowed``, and only in video
          output mode - ``image_mode != 0`` strips ``SEVL`` before the radio
          is even built;
        * end: ``E`` allowed, and either always enabled for the model or a
          start choice possible, since the checkbox only shows beside one
          (``end_frames_option_visible``);
        * reference: an ``image_ref_choices`` entry whose letters include
          ``I``, which is what the reference dropdown offers; a model without
          the key hides the dropdown and takes no reference image.
        """
        _model_type, definition = self.page_model(live)
        if not isinstance(definition, dict):
            return {receiver_id: None for receiver_id in V1_RECEIVERS}

        allowed_types = str(definition.get("image_prompt_types_allowed") or "")
        mode = _image_mode(live.get(IMAGE_MODE))
        start: typing.Optional[bool]
        end: typing.Optional[bool]
        if mode is None:
            start = end = None
        else:
            if mode != 0:
                allowed_types = _del_letters(allowed_types, "SEVL")
            start = "S" in allowed_types
            end = "E" in allowed_types and (start or bool(definition.get("end_frames_always_enabled", False)))
        reference: typing.Optional[bool] = _reference_choice(definition) is not None

        def gated(answer: typing.Optional[bool], switchable: bool) -> typing.Optional[bool]:
            # A "yes" the bridge could not act on is not offered; a "no" is a
            # no whatever was handed over.
            if answer is False:
                return False
            return answer if switchable else None

        return {
            protocol.START_FRAME: gated(start, self.component(IMAGE_PROMPT_RADIO) is not None),
            protocol.END_FRAME: gated(
                end, self.component(END_IMAGES_CHECKBOX) is not None and self.component(IMAGE_PROMPT_RADIO) is not None
            ),
            protocol.REFERENCE: gated(reference, self.component(REFERENCE_SELECTOR) is not None),
        }

    def switch_for(self, receiver_id: str, live: typing.Mapping[str, typing.Any]) -> "Switch":
        """The component updates that switch ``receiver_id`` on for this page.

        Empty when it already is on. Otherwise the same edits Wan2GP's own
        handlers make when a person uses the control - the selector's value
        *and* the hidden letter string generation reads, because the radio's
        ``.change`` handler will recompute the string from these very values
        while the reference dropdown's ``.input`` handler does not run for a
        value set from the server. Writing both makes the outcome the same
        either way. The rows are shown for the same reason: what a click
        would have revealed, a send reveals.
        """
        image_prompt = _text_value(live.get(IMAGE_PROMPT_TYPE))
        video_prompt = _text_value(live.get(VIDEO_PROMPT_TYPE))
        updates: typing.Dict[str, typing.Any] = {}

        if receiver_id == protocol.START_FRAME:
            if "S" in image_prompt:
                return Switch()
            updates[IMAGE_PROMPT_RADIO] = "S"
            updates[IMAGE_PROMPT_TYPE] = _add_letters(_del_letters(image_prompt, "VLTS"), "S")
            if self.component(START_ROW) is not None:
                updates[START_ROW] = {"visible": True}
            return Switch(SWITCH_TOKENS[receiver_id], updates)

        if receiver_id == protocol.END_FRAME:
            if "E" in image_prompt:
                return Switch()
            letters = _add_letters(image_prompt, "E")
            if not any(letter in letters for letter in "SVL"):
                updates[IMAGE_PROMPT_RADIO] = "S"
                letters = _add_letters(_del_letters(letters, "VLTS"), "S")
                if self.component(START_ROW) is not None:
                    updates[START_ROW] = {"visible": True}
            updates[END_IMAGES_CHECKBOX] = True
            updates[IMAGE_PROMPT_TYPE] = letters
            if self.component(END_ROW) is not None:
                updates[END_ROW] = {"visible": True}
            return Switch(SWITCH_TOKENS[receiver_id], updates)

        if receiver_id == protocol.REFERENCE:
            if "I" in video_prompt:
                return Switch()
            _model_type, definition = self.page_model(live)
            choice = _reference_choice(definition)
            if choice is None:
                return Switch()
            block = definition.get("image_ref_choices") if isinstance(definition, dict) else None
            letters_filter = str((block or {}).get("letters_filter") or "KFI")
            updates[REFERENCE_SELECTOR] = choice
            updates[VIDEO_PROMPT_TYPE] = _add_letters(_del_letters(video_prompt, letters_filter), choice)
            if self.component(REFERENCE_ROW) is not None:
                updates[REFERENCE_ROW] = {"visible": True}
            return Switch(SWITCH_TOKENS[receiver_id], updates)

        return Switch()

    # -- the handshake -------------------------------------------------------

    def handshake(self, bridge_session: str = "", environ: typing.Optional[typing.Mapping[str, str]] = None) -> dict:
        """Section 14.2's answer, with ``ready`` meaning what it says.

        ready is true only when a mandatory component resolved for every v1
        receiver *and* the state components the fingerprint is built from
        resolved too. Anything less is ``BRIDGE_COMPONENT_INCOMPATIBLE`` with
        the missing keys named, because "the menu was empty" is a much worse
        bug report than "this build does not expose image_refs".
        """
        source = self.environ if environ is None else environ
        resolution = self.resolution
        version_ok = version_supported(self.wan2gp_version)

        ready = bool(resolution.ok and version_ok)
        payload = {
            "protocol": protocol.PROTOCOL,
            "bridge_version": BRIDGE_VERSION,
            "bridge_session": str(bridge_session or ""),
            "instance_id": instance_id(source),
            "wan2gp_version": self.wan2gp_version,
            "ready": ready,
            "capabilities": {
                "receiver_query": ready,
                "image_handoff": ready,
                "verified_ack": ready,
                # Theme is presentation. Section 27.1: it may fail on its own
                # without taking image handoff with it, so it is reported
                # separately and never gates ``ready``.
                "theme": True,
            },
        }
        if not ready:
            payload["code"] = BRIDGE_COMPONENT_INCOMPATIBLE
            missing = list(resolution.missing_mandatory)
            if not version_ok:
                missing.append(f"wan2gp_version={self.wan2gp_version or 'unknown'}")
            payload["missing"] = missing
        if resolution.missing_optional:
            payload["absent"] = list(resolution.missing_optional)
        return payload


@dataclasses.dataclass(frozen=True)
class Switch:
    """What a send changes besides the receiver: a token for the record, and
    the updates by component key. Empty means the receiver is already on."""

    token: str = ""
    updates: typing.Mapping[str, typing.Any] = dataclasses.field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.updates)


# ----------------------------------------------------------------- helpers --


def _del_letters(source: typing.Any, letters: str) -> str:
    """Wan2GP's ``del_in_sequence``: drop each of ``letters`` from the flags."""
    text = str(source or "")
    for letter in letters:
        text = text.replace(letter, "")
    return text


def _add_letters(source: typing.Any, letters: str) -> str:
    """Wan2GP's ``add_to_sequence``: append each of ``letters`` not already there."""
    text = str(source or "")
    for letter in letters:
        if letter not in text:
            text += letter
    return text


def _text_value(value: typing.Any) -> str:
    """A flag string as Gradio hands it back: a string, or nothing."""
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _image_mode(value: typing.Any) -> typing.Optional[int]:
    """Wan2GP's ``image_mode`` as an int, or None when the build did not hand it over."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _reference_choice(definition: typing.Any) -> typing.Optional[str]:
    """The first reference-dropdown value that injects reference images.

    Wan2GP builds the dropdown from ``image_ref_choices["choices"]``, a list
    of ``(label, letters)`` pairs, and reads ``I`` in the chosen letters as
    "use the reference gallery". The first such choice is the one a person
    reaching for references would pick; a model without the key has no
    dropdown and no reference input.
    """
    if not isinstance(definition, dict):
        return None
    block = definition.get("image_ref_choices")
    if not isinstance(block, dict):
        return None
    choices = block.get("choices")
    if not isinstance(choices, (list, tuple)):
        return None
    for entry in choices:
        letters = entry[1] if isinstance(entry, (list, tuple)) and len(entry) >= 2 else entry
        if isinstance(letters, str) and "I" in letters:
            return letters
    return None


def _walk(source: typing.Any, path: typing.Sequence[str]) -> typing.Any:
    """Follow a path through nested mappings/objects, or give up quietly."""
    current = source
    for step in path:
        if isinstance(current, dict):
            current = current.get(step)
        else:
            current = getattr(current, step, None)
        if current is None:
            return None
    return current


def _walk_flag(source: typing.Any, path: typing.Sequence[str]) -> typing.Optional[bool]:
    value = _walk(source, path)
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "none")
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _scalars(value: typing.Any) -> typing.List[typing.Any]:
    """Every scalar a selection component might have meant, outermost first.

    Gradio dropdowns hand back a string, a (label, value) pair, or a list of
    one of those depending on the build and on ``multiselect``. Which half of
    a pair is the model is not knowable from here, so both are kept and the
    caller decides what a match means.
    """
    found: typing.List[typing.Any] = []
    queue = [value]
    while queue and len(found) < 8:
        item = queue.pop(0)
        if isinstance(item, (list, tuple)):
            queue.extend(list(item)[:4])
        elif isinstance(item, (str, int, float)) and not isinstance(item, bool):
            found.append(item)
    return found


def _first_scalar(value: typing.Any) -> typing.Any:
    """The one scalar to report a selection by. See ``_scalars``."""
    found = _scalars(value)
    return found[0] if found else ""


def _short(value: typing.Any, limit: int = 80) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return text.strip()[:limit]
