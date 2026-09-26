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

import copy
import dataclasses
import hashlib
import json
import os
import typing

try:  # the plugin folder is loaded both as a package and as a bare directory
    from . import protocol
except ImportError:  # pragma: no cover - depends on how WanGP imports plugins
    import protocol  # type: ignore[no-redef]


BRIDGE_VERSION = "1.8.0"

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
#: The queue operation's own codes (protocol 3). The same strings as
#: ``protocol.QUEUE_CODE_*`` and as errors.py's, for the same reason.
REQUEST_INVALID = "REQUEST_INVALID"
REQUEST_ID_CONFLICT = "REQUEST_ID_CONFLICT"
PROMPT_TOO_LONG = "PROMPT_TOO_LONG"
QUEUE_BUSY = "QUEUE_BUSY"
QUEUE_REQUEST_REFUSED = "QUEUE_REQUEST_REFUSED"
ADMISSION_UNCONFIRMED = "ADMISSION_UNCONFIRMED"
WANGP_VALIDATION_REFUSED = "WANGP_VALIDATION_REFUSED"
#: Protocol 5: a request composed for one model reached a page on another.
MODEL_CHANGED = "MODEL_CHANGED"


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
#: Protocol 3, the queue: the prompt boxes, the wizard flag that says which
#: of them generation reads, WanGP's own correlation id, and the hidden
#: trigger whose change runs WanGP's native add-to-queue chain. All optional
#: for the image send; the queue refuses without the critical ones.
PROMPT = "prompt"
WIZARD_PROMPT = "wizard_prompt"
WIZARD_ACTIVE = "wizard_active"
CLIENT_ID = "client_id"
ADD_TO_QUEUE_TRIGGER = "add_to_queue_trigger"
#: Protocol 4: the hidden text whose change runs WanGP's generate chain.
GENERATE_TRIGGER = "generate_trigger"
GALLERY_TAB = "gallery_tab"
#: Protocol 6: the hidden text whose change makes WanGP commit its own live
#: form. Wan2GP wires it in ``generate_media_tab`` as
#: ``set_save_form_event(save_form_trigger.change)``, which runs
#: ``validate_wizard_prompt`` and then ``save_inputs(target="state")`` over the
#: *whole* form - and ``save_inputs`` ends in
#: ``service.record_model_form(model_type, cleaned_inputs)``, the process-wide
#: snapshot a server-side compose reads. Writing it is therefore how a page
#: hands its live settings to a job that will run with no page at all.
SAVE_FORM_TRIGGER = "save_form_trigger"

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
    # -- the queue (protocol 3) ----------------------------------------------
    # Confirmed against Wan2GP at 362c346: ``add_to_queue_trigger.change``
    # runs validate_wizard_prompt -> save_inputs -> process_prompt_and_add_tasks
    # -> update_status, and never process_tasks; ``save_inputs`` reads
    # ``client_id`` into every task it makes.
    ComponentSpec(key=PROMPT, candidates=("prompt",), kind="text", mandatory=False,
                  known_for="Wan2GP: the prompt textbox generation reads when the wizard is off"),
    ComponentSpec(key=WIZARD_PROMPT, candidates=("wizard_prompt",), kind="text", mandatory=False,
                  known_for="Wan2GP: the prompt textbox shown, and read, when the wizard is on"),
    ComponentSpec(key=WIZARD_ACTIVE, candidates=("wizard_prompt_activated_var",), kind="text", mandatory=False,
                  known_for='Wan2GP: hidden gr.Text, "on" while the prompt wizard is on'),
    ComponentSpec(key=CLIENT_ID, candidates=("client_id",), kind="text", mandatory=False,
                  known_for="Wan2GP: hidden gr.Textbox; save_inputs copies it into each queued task's params"),
    ComponentSpec(key=ADD_TO_QUEUE_TRIGGER, candidates=("add_to_queue_trigger",), kind="text", mandatory=False,
                  known_for="Wan2GP: hidden gr.Text; its change runs the native queue-only chain"),
    # Confirmed against Wan2GP at 362c346 as well: ``generate_trigger.change``
    # runs the same chain and then prepare_generate_media -> activate_status ->
    # process_tasks; the Generate button's whole effect is to write it.
    ComponentSpec(key=GENERATE_TRIGGER, candidates=("generate_trigger",), kind="text", mandatory=False,
                  known_for="Wan2GP: hidden gr.Text declared beside add_to_queue_trigger; its change generates"),
    ComponentSpec(key=GALLERY_TAB, candidates=("current_gallery_tab",), kind="state", mandatory=False,
                  known_for="Wan2GP: which output gallery is showing; read by process_prompt_and_add_tasks"),
    # It carries no elem_id, and does not need one: a plugin is handed the
    # generator tab's whole local scope by variable name
    # (``app.run_component_insertion(locals_dict)``), and this is written as a
    # Gradio event *output* rather than through the DOM - the same way WanGP's
    # own post-processing buttons write ``generate_trigger`` to start a run.
    ComponentSpec(key=SAVE_FORM_TRIGGER, candidates=("save_form_trigger",), kind="text", mandatory=False,
                  known_for="Wan2GP: hidden gr.Text; its change commits the live form process-wide"),
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
    # Protocol 3, the queue: what the native Add-to-queue button writes into
    # its trigger, and the per-page generation record the confirmation reads.
    "get_unique_id", "get_gen_info",
    # Protocol 4: WanGP's own process-wide answer to "is a generation running",
    # a module function so that what the bridge reads is live, not a value
    # copied once at injection. It is what decides generate versus queue.
    "is_generation_in_progress",
    # Protocol 5: the base model a finetune stands on, for the model block -
    # an H3 finetune is still an H3 model to a prompt written for one.
    "get_base_model_type",
    # Protocol 6, compose: the settings a submission runs at. The first two
    # are the session path and are an optimisation only - a server-side
    # compose has no session - and the last two are the browser-independent
    # ones: the factory floor, and the shape validate_task merges onto it.
    "get_current_model_settings", "collect_current_model_settings",
    "get_default_settings", "get_factory_settings", "get_model_settings",
    # Protocol 6, residency: what decides whether a submission reloads the
    # video model. Read for diagnostics, never written. B7B rule 5's key is
    # (model_type, profile, config, VAE upsampling), and the profile is
    # derived per output type rather than per model.
    "get_profile_type_for_model", "get_model_name",
    # Protocol 6, execution: the one process-wide generation arbiter. Asked
    # for here in case a build re-exports it from wgp's namespace; when it
    # does not, ``service_access`` imports it from shared.deepy.hybrid, which
    # is the only module import this whole plugin makes into Wan2GP and is
    # confined to this file on purpose.
    "service_for",
)

#: The exact Wan2GP revision the protocol-6 control plane was proven against.
#: Pinned here, beside the element ids, under the same rule: when a Wan2GP
#: release moves the service, the queue dict or the compose seams, this is
#: the one file to correct.
#:
#: The API surface has no published stability guarantee, and this design uses
#: it in a combination Wan2GP's own plugin documentation does not describe -
#: submitting into the WebUI's own queue and worker from a plugin, with no
#: browser. That is deliberate: the documented alternative injects a
#: WebUI-backed session that refuses without a live Gradio request, and the
#: headless session that would work runs *beside* the arbiter rather than
#: inside it. See docs/wangp/SERVER_EXECUTION.md.
WAN2GP_EXECUTION_REVISION = "cd832e9d676907f0c055f3fafba472f2f61a4c91"
WAN2GP_EXECUTION_REVISION_DATE = "2026-09-13"

#: Where the arbiter lives, in the order the bridge looks for it. A module
#: import rather than a requested global is a departure from this file's own
#: rule, so it is written down rather than buried: Wan2GP hands plugins the
#: wgp namespace, and the service is not in it on the proven revision.
SERVICE_MODULE = "shared.deepy.hybrid"
SERVICE_FACTORY = "service_for"
#: Names a build might keep the one service under, when the factory needs a
#: state this caller does not have. Tried in order, and each is only accepted
#: when it looks like a service - has a ``start_generation`` and a state.
#: Where the one service actually lives, in order of how sure we are.
#:
#: ``_deepy_hybrid`` is first because it is the real one: ``wgp.py`` declares
#: it at module level and assigns it while it builds the generator tab
#: (``global _deepy_hybrid; _deepy_hybrid = HybridService(...)``). The rest
#: are names a future build might use instead, kept because asking costs
#: nothing and a wrong name is caught by ``is_service``.
SERVICE_SINGLETONS: typing.Tuple[str, ...] = (
    "_deepy_hybrid", "_service", "service", "SERVICE", "_hybrid_service", "current_service",
)

#: The modules that may hold it, looked up in ``sys.modules`` and NEVER
#: imported. Wan2GP is launched as ``python wgp.py``, so its module object is
#: ``__main__`` - and ``import wgp`` would not find that object, it would
#: execute the whole application a second time inside its own process.
SERVICE_HOLDERS: typing.Tuple[str, ...] = ("__main__", "wgp", SERVICE_MODULE)

#: The key an inline submission is left under for ``load_queue_action`` to
#: pick up, and the command that makes the service look. Both are Wan2GP's
#: own - it is exactly what Deepy does - so this is a use of the primitive,
#: not a re-implementation of the chain around it.
INLINE_QUEUE_KEY = "inline_queue"
LOAD_QUEUE_COMMAND = "load_queue_trigger"
#: Plugin state, which travels BESIDE a task's params and never inside them.
#: Wan2GP's queue entries are ``{id, params, plugin_data, ...}``; its unpacker
#: reads this key off the manifest entry, and its worker passes what it finds
#: to ``generate_media`` as an explicit keyword *while also* splatting params.
#: ``generate_media`` names the same parameter, so a copy of this key left
#: inside params is not ignored - it is passed twice, and the task dies with
#: "got multiple values for keyword argument 'plugin_data'" before it has
#: generated anything. Named here because the composed base is a form
#: snapshot, and what a third-party plugin records into one is not ours to
#: predict.
PLUGIN_DATA_KEY = "plugin_data"
#: The shared generation record, and the three fields of it that may be read
#: without going stale. ``main_process_running`` and ``process_status`` are
#: deliberately absent: the first is set one line before a call that can
#: raise and is cleared two branches later, so an exception leaks it True for
#: the life of the process, and gating on it would turn an upstream slip into
#: a permanent unrecoverable wait.
GEN_QUEUE_KEY = "queue"
GEN_IN_PROGRESS_KEY = "in_progress"

#: What the queue operation cannot do without. The image send keeps working
#: on a build that lacks any of these; a queue request is refused with
#: BRIDGE_COMPONENT_INCOMPATIBLE naming the missing one.
QUEUE_CRITICAL: typing.Tuple[str, ...] = (CLIENT_ID, ADD_TO_QUEUE_TRIGGER, PROMPT, SESSION_STATE)

#: What starting a generation cannot do without, beyond the queue set. A
#: build that lacks it still queues; "auto" then degrades to the queue route
#: and the answer says ``start: "unknown"``.
START_CRITICAL: typing.Tuple[str, ...] = (GENERATE_TRIGGER,)

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

#: Which protocol-6 media slot fills which logical receiver. The slot names
#: are the control plane's; the receiver ids are this file's; the settings
#: key each one lands in is whatever ``settings_key`` resolves.
_SLOT_FOR_RECEIVER: typing.Mapping[str, str] = {
    protocol.START_FRAME: protocol.EXEC_SLOT_START,
    protocol.END_FRAME: protocol.EXEC_SLOT_END,
    protocol.REFERENCE: protocol.EXEC_SLOT_REFERENCES,
}

#: The only switch updates that belong in a settings dict: the two letter
#: strings generation itself reads. Everything else a switch touches is a
#: control on a page.
SETTINGS_FLAG_COMPONENTS: typing.Tuple[str, ...] = (IMAGE_PROMPT_TYPE, VIDEO_PROMPT_TYPE)

#: Which flag string each receiver lives in, and the letters that mean "this
#: slot is in use". Struck out for a slot a submission does not fill, so a
#: composed base cannot describe pictures this job does not have.
#:
#: VERIFY ON A REAL INSTALL: the letters, like every other WanGP identifier
#: in this file. K/F/I are the reference choices a model publishes; a model
#: that names its own filter widens this, and these are the fallback.
_FLAG_LETTERS: typing.Mapping[str, typing.Tuple[str, str]] = {
    protocol.START_FRAME: (IMAGE_PROMPT_TYPE, "S"),
    protocol.END_FRAME: (IMAGE_PROMPT_TYPE, "E"),
    protocol.REFERENCE: (VIDEO_PROMPT_TYPE, "KFI"),
}

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
#: Protocol 6: the loopback port Forge kept for the control surface before it
#: launched this child, and the directory both processes use for the durable
#: execution ledger. Absent means server-owned execution is off for this run -
#: an older Forge, or a WanGP somebody started by hand.
ENV_CONTROL_PORT = "MINIPAINT_WANGP_CONTROL_PORT"
ENV_LEDGER_ROOT = "MINIPAINT_WANGP_LEDGER_ROOT"


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


def bridge_secret(environ: typing.Optional[typing.Mapping[str, str]] = None) -> str:
    """The per-launch credential the control surface compares.

    Read here and compared in ``control.py``; it appears in no answer, no log
    line and no diagnostics report. An empty value means Forge did not mint
    one for this run, and the control surface then refuses to open at all
    rather than listening without a check.
    """
    source = os.environ if environ is None else environ
    return str(source.get(ENV_BRIDGE_SECRET) or "").strip()


def control_port(environ: typing.Optional[typing.Mapping[str, str]] = None) -> int:
    """The loopback port Forge kept for the control surface, or 0.

    Forge chooses it before the launch and exports it, the same way it
    chooses the Gradio port: the child never picks a number and never tells
    anybody one, so there is no discovery step to get wrong and no file for a
    stale port to sit in.
    """
    source = os.environ if environ is None else environ
    try:
        port = int(str(source.get(ENV_CONTROL_PORT) or "0").strip())
    except ValueError:
        return 0
    return port if 0 < port < 65536 else 0


def ledger_root(environ: typing.Optional[typing.Mapping[str, str]] = None) -> str:
    """Where the durable execution ledger lives, or "".

    Forge's own runtime directory, handed over rather than derived: the child
    has no idea where Forge keeps its data and must not go looking.
    """
    source = os.environ if environ is None else environ
    return str(source.get(ENV_LEDGER_ROOT) or "").strip()


def server_execution_available(environ: typing.Optional[typing.Mapping[str, str]] = None) -> bool:
    """Whether this run was launched with everything the control plane needs."""
    source = os.environ if environ is None else environ
    return bool(managed(source)) and bool(bridge_secret(source)) and control_port(source) > 0 and bool(ledger_root(source))


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
        #: What a live page has taught this process that a control-plane
        #: thread cannot work out for itself. See ``remember_service``.
        self._learned: typing.Dict[str, typing.Any] = {}

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
            # Protocol 3: what a queue request may overlay, read so that the
            # original can be captured and put back; the selectors a switch
            # may set, for the same reason; and which gallery tab is showing.
            PROMPT, WIZARD_PROMPT, WIZARD_ACTIVE, CLIENT_ID, GALLERY_TAB,
            IMAGE_PROMPT_RADIO, END_IMAGES_CHECKBOX, REFERENCE_SELECTOR,
        )
        return [(key, self.resolution.component(key)) for key in keys if self.resolution.component(key) is not None]

    def queue_components(self) -> typing.List[typing.Tuple[str, typing.Any]]:
        """The components a receiver never writes but an operation does.

        The tail of the bridge event's outputs, after the receivers and the
        switch components, in this fixed order. Only the ones this build
        resolved, for the reason ``switch_components`` gives.

        ``SAVE_FORM_TRIGGER`` is last and is not a queue component: a flush
        writes it and nothing else does. It lives here because this is the
        block of the event's outputs that exists to be written by name, and
        giving it a fourth block of its own would buy nothing but another
        ordering to keep in step.
        """
        keys = (PROMPT, WIZARD_PROMPT, CLIENT_ID, ADD_TO_QUEUE_TRIGGER, GENERATE_TRIGGER,
                SAVE_FORM_TRIGGER)
        return [(key, self.resolution.component(key)) for key in keys if self.resolution.component(key) is not None]

    def queue_missing(self) -> typing.List[str]:
        """The queue-critical components this build did not hand over."""
        return [key for key in QUEUE_CRITICAL if self.resolution.component(key) is None]

    def start_missing(self) -> typing.List[str]:
        """What the generate route needs and this build did not hand over."""
        return [key for key in START_CRITICAL if self.resolution.component(key) is None]

    # -- protocol 6: the one arbiter -----------------------------------------

    def remember_service(self, state: typing.Any) -> typing.Any:
        """Learn the service from a live page, and keep it for the control plane.

        THE SUPPORTED SEAM, AND THE ONE THAT ACTUALLY WORKS.

        ``service_for(state)`` is how Wan2GP itself gets the service, and it
        needs a session state - which a browser request has and a control-plane
        thread never does. Everything else this class tries is archaeology
        around that fact: a global that is injected as a snapshot before the
        service exists, a module attribute whose name and module differ
        between revisions.

        So the bridge stops guessing and takes the answer from the one caller
        that has it. Every request from a page carries its state; the first
        one resolves the service and it is kept for the life of the process,
        because it is a process-wide singleton and the control plane needs the
        same object the page would have got.

        Keeping a reference is safe in a way that keeping a *state* would not
        be: the service outlives any one page by construction - it is what the
        pages share - whereas a session state belongs to a browser tab that
        may already be gone.
        """
        found = self._call_factory(self.host.read_global(SERVICE_FACTORY), state)
        if found is None:
            found = self._gen_state_service(state)
        if found is not None:
            self._learned["service"] = found
        return found

    def _gen_state_service(self, state: typing.Any) -> typing.Any:
        """``state.service``, when the factory is not among the globals we got.

        ``service_for`` is one line and reads exactly this attribute. A build
        that did not hand the function over still hands over the state, and
        the attribute is the same attribute.
        """
        try:
            candidate = getattr(state, "service", None)
        except Exception:
            candidate = None
        return candidate if self.is_service(candidate) else None

    def service(self, state: typing.Any = None) -> typing.Any:
        """Wan2GP's one process-wide generation service, or None.

        THIS IS THE WHOLE OF THE EXCLUSION, AND IT IS NOT A MECHANISM.

        Wan2GP has two generation paths and they exclude each other in one
        direction only: a headless submission takes a module lock the WebUI
        never takes, and the WebUI's Generate button consults nothing a
        plugin can own. An adapter that runs beside that - one constructed
        with its own ``gen`` dict - is outside every per-state guard in the
        application, not merely outside the queue: the guard on Force Unload
        Models from RAM reads the browser's dict, sees no generation, and
        frees the model out from under an unattended run mid-inference.

        So the bridge does not build an exclusion. It submits into the one
        queue the one worker drains, which is what the WebUI itself does,
        which means both arrival orders are covered by the gate that is
        already there: ``start_generation`` holds a mutation lock and returns
        the existing worker rather than starting a second one. There is no
        new lock to get right, no flag to write, and nothing to release on
        cancel - and an unattended job becomes visible to WanGP's own queue,
        progress display and guards for free.

        Reached by module import rather than through ``request_global``,
        which is the one place this plugin departs from its own rule. The
        proven revision is pinned above; a build that has moved it answers
        None here and the control surface then reports SERVICE_UNAVAILABLE
        and refuses server execution, which is the correct failure. It is
        never a reason to fall back to a path that runs beside the arbiter.
        """
        factory = self.host.read_global(SERVICE_FACTORY)
        found = self._call_factory(factory, state)
        if found is not None:
            return found
        if state is not None:
            found = self._gen_state_service(state)
            if found is not None:
                return found
        # What a page told us, if one has. This is the answer on every build
        # where the module-level archaeology below does not apply, which is
        # every build where the service is reached the documented way.
        learned = self._learned.get("service")
        if self.is_service(learned):
            return learned
        # The factory again, off the module this time - a build that did not
        # hand it over as a global may still have it. Its absence is not fatal
        # and must not end the search: the singleton below is the route that
        # works for a caller with no session, and gating it on an optional
        # import is how a missing module turned into "this WanGP has no
        # generation service".
        try:
            import importlib

            module = importlib.import_module(SERVICE_MODULE)
        except Exception:
            module = None
        if module is not None:
            found = self._call_factory(getattr(module, SERVICE_FACTORY, None), state)
            if found is not None:
                return found
        return self._singleton()

    def _singleton(self) -> typing.Any:
        """The process-wide service, read live out of the module that owns it.

        THE FACTORY CANNOT ANSWER THIS ONE, AND NOT BY ACCIDENT.

        ``service_for`` is one line - ``state.service if isinstance(state,
        SharedState) else None`` - so a server-side caller, which by
        definition has no session state, gets None from it every time. It is
        the right answer to the question it was asked; it is just not the
        question the control plane has.

        Nor can the global be requested through the plugin API. Wan2GP injects
        globals by *copying* each requested name onto the plugin object
        (``inject_globals``: ``setattr(plugin, name, references[name])``), and
        it does that while the plugins load - long before the generator tab is
        built and the service exists. The snapshot would be None for the life
        of the process.

        So it is read here, at call time, straight off the module object, and
        read out of ``sys.modules`` rather than imported. That is the part
        worth being careful about: Wan2GP is launched as ``python wgp.py``, so
        its module object is ``__main__``, and ``import wgp`` would not return
        that object at all - it would execute the entire application a second
        time inside its own process. Nothing here imports anything.
        """
        import sys

        for name in SERVICE_HOLDERS:
            module = sys.modules.get(name)
            if module is None:
                continue
            for attribute in SERVICE_SINGLETONS:
                try:
                    candidate = getattr(module, attribute, None)
                except Exception:
                    continue
                if self.is_service(candidate):
                    return candidate
        # By shape, when the name is wrong. Every name above is a name read
        # out of one revision of Wan2GP, and a rename there would leave this
        # answering "no generation service" about a process that has one -
        # which is the worst answer available, because it is indistinguishable
        # from a build that genuinely cannot execute.
        #
        # ``vars`` rather than ``dir``/``getattr``: it reads the module's own
        # __dict__ and so cannot trigger a lazy attribute or a module-level
        # __getattr__, which is what makes sweeping somebody else's namespace
        # safe to do at all.
        for name in SERVICE_HOLDERS:
            module = sys.modules.get(name)
            if module is None:
                continue
            try:
                namespace = dict(vars(module))
            except Exception:
                continue
            for value in namespace.values():
                if self.is_service(value):
                    return value
        return None

    def service_possible(self) -> bool:
        """Whether this Wan2GP could EVER run a server-side job.

        "Not yet" and "never" need opposite answers from a caller, and until
        this existed they were the same code. A job on a build that has the
        service waits a few seconds while the UI finishes; a job on a build
        that does not wait forever and then fails, having held up the queue
        the whole time, which is what happened.

        The test is whether the module that defines the service is *findable*
        at all. ``find_spec`` asks the import system where the module would
        come from without executing it, so a build carrying it that simply
        has not imported it yet still answers True - which is the safe way
        round, because True only costs a wait and False costs the feature.
        """
        if self.service() is not None:
            return True
        try:
            import importlib.util

            return importlib.util.find_spec(SERVICE_MODULE) is not None
        except Exception:
            # An import system that will not answer is not evidence of
            # absence, and refusing the feature on it would be a guess.
            return True

    def service_diagnosis(self) -> str:
        """Why there is no service, in the words of what was actually looked at.

        SERVICE_UNAVAILABLE is one code for several different worlds - a
        factory that answered None, a module that is not loaded, a global that
        has not been assigned yet, a build that has none of this - and a log
        line carrying only the code sends whoever reads it guessing. This is
        the sentence that stops that, and it is deliberately about what was
        seen rather than what it means.
        """
        import sys

        if self.service() is not None:
            return "resolved"
        parts: typing.List[str] = []
        factory = self.host.read_global(SERVICE_FACTORY)
        parts.append(f"{SERVICE_FACTORY}={'callable' if callable(factory) else 'absent'}")
        parts.append("learned from a page=no")
        # Which file is actually running, and whether the service module could
        # be imported at all. Together these say whether this is an install
        # without the service or a lookup that cannot see one that is there -
        # and those had been indistinguishable for two rounds of reports.
        try:
            main = sys.modules.get("__main__")
            parts.append("__main__ file=" + str(getattr(main, "__file__", "?")).replace("\\", "/").rsplit("/", 1)[-1])
        except Exception:
            pass
        try:
            import importlib.util

            parts.append(f"{SERVICE_MODULE} importable="
                         + ("yes" if importlib.util.find_spec(SERVICE_MODULE) is not None else "no"))
        except Exception:
            parts.append(f"{SERVICE_MODULE} importable=cannot tell")
        for name in SERVICE_HOLDERS:
            module = sys.modules.get(name)
            if module is None:
                parts.append(f"{name}=not loaded")
                continue
            try:
                namespace = dict(vars(module))
            except Exception:
                parts.append(f"{name}=unreadable")
                continue
            named = [key for key in SERVICE_SINGLETONS if key in namespace]
            if not named:
                parts.append(f"{name}=loaded, none of the known names")
                continue
            parts.append(f"{name}=" + ", ".join(
                f"{key} is {type(namespace[key]).__name__}" for key in named
            ))
        return "; ".join(parts)[:400]

    def _call_factory(self, factory: typing.Any, state: typing.Any) -> typing.Any:
        """``service_for`` with a state if we have one, without if we do not."""
        if not callable(factory):
            return None
        for arguments in ((state,), ()) if state is not None else ((), (None,)):
            try:
                found = factory(*arguments)
            except Exception:
                continue
            if self.is_service(found):
                return found
        return None

    @staticmethod
    def is_service(value: typing.Any) -> bool:
        """Whether this is really the arbiter and not something named like it.

        Two attributes rather than a type check, for the reason every other
        shape check in this file gives: the class lives in somebody else's
        package and may be renamed, but a thing that cannot start a
        generation is not the thing the submission needs, whatever it is
        called.
        """
        if value is None or isinstance(value, type):
            # A class is not a service. It matters because the class itself is
            # importable and sits in the same namespaces the instance does, so
            # anything sweeping for the shape finds ``HybridService`` before it
            # finds ``_deepy_hybrid`` - and a class answers every call with
            # "missing self" rather than with a generation.
            return False
        return callable(getattr(value, "start_generation", None)) and callable(getattr(value, "command", None))

    def shared_gen(self, service: typing.Any = None, state: typing.Any = None) -> typing.Any:
        """The one generation record every page shares, or None.

        Shared by reference rather than copied, which is why a submission
        placed in it is a submission the running worker will drain and why a
        guard keyed on it starts protecting unattended work the moment the
        work lives there.
        """
        if state is not None:
            found = self._gen_of_state(state)
            if found is not None:
                return found
        if service is None:
            service = self.service(state)
        if service is None:
            return None
        for attribute in ("_state", "state", "shared_state"):
            found = self._gen_of_state(getattr(service, attribute, None))
            if found is not None:
                return found
        return None

    def _gen_of_state(self, state: typing.Any) -> typing.Any:
        if state is None:
            return None
        getter = self.host.read_global("get_gen_info")
        if callable(getter):
            try:
                found = getter(state)
                if isinstance(found, dict):
                    return found
            except Exception:
                pass
        try:
            found = state["gen"]
        except Exception:
            return None
        return found if isinstance(found, dict) else None

    def service_generation_running(self, service: typing.Any) -> typing.Optional[bool]:
        """Whether the one worker exists, from the thread handle rather than a flag.

        ``generation_running`` on the service is ``self._queue_worker is not
        None``: a live object, not a boolean somebody remembered to clear. It
        is the one signal here that cannot go stale, which is why the bridge
        reads it and never reads ``main_process_running`` or
        ``process_status``.
        """
        if service is None:
            return None
        try:
            found = getattr(service, "generation_running", None)
            if callable(found):
                found = found()
            return None if found is None else bool(found)
        except Exception:
            return None

    def generation_running(self) -> typing.Optional[bool]:
        """WanGP's own process-wide flag, read live, or None when it cannot be.

        Only a callable counts: Wan2GP injects the requested module function,
        and calling it reads the flag as it is now. A bare value under the
        same name would be the flag as it was at injection, which is not an
        answer, and None is what makes the start decision fall back to the
        queue route rather than guess.

        READ, NEVER WRITTEN. The flag has four unconditional writers already
        and no owner or nesting count, and the headless clear sits outside
        the lock that guards the run, so it under-reports even for two runs
        of the same kind. Nothing in this plugin assigns it; where the bridge
        needs to know whether the card is busy it asks the service, whose
        answer is a thread handle.
        """
        getter = self.host.read_global("is_generation_in_progress")
        if not callable(getter):
            return None
        try:
            return bool(getter())
        except Exception:
            return None

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
        """The model block of the receiver answer: type, label, family, and
        (protocol 5) architecture.

        Strings only, and only the four the protocol names. The bridge does
        not publish a model database and MiniPaint does not keep one; this is
        for the diagnostics line, the send log, and - the architecture - for
        a caller that writes a prompt for one model family. A type and
        definition already read for this page (``page_model``) are used as
        given; the process-wide globals are the fallback for a page that
        agrees with them.
        """
        current = self.selection_is_current(selected)
        if not model_type:
            model_type = self.host.read_global("model_type") if current else _first_scalar(selected)
        if not isinstance(definition, dict):
            definition = self.host.read_global("model_def") if current else None
        label = _walk(definition, ("name",)) or _walk(definition, ("label",))
        family = _walk(definition, ("family",)) or _walk(definition, ("architecture",))
        architecture = _walk(definition, ("architecture",))
        if not architecture and model_type:
            base = self.host.read_global("get_base_model_type")
            if callable(base):
                try:
                    architecture = base(model_type)
                except Exception:
                    architecture = ""
        return {
            "type": _short(model_type),
            "label": _short(label) or _short(model_type),
            "family": _short(family),
            "architecture": _short(architecture),
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

    # -- protocol 6: the settings dict ---------------------------------------

    def settings_key(self, component_key: str) -> str:
        """What this build's settings dict calls a component's value.

        A WanGP settings dict is keyed by the parameter names its own form
        components carry, so the elem_id that resolved *is* the key. The
        first candidate is the fallback for a component this build did not
        hand over, because a settings key can be right even when the widget
        behind it was never given to the plugin.
        """
        found = self.resolution.elem_id(component_key)
        if found:
            return found
        spec = COMPONENTS_BY_KEY.get(component_key)
        return spec.candidates[0] if spec is not None and spec.candidates else ""

    def settings_as_values(self, settings: typing.Mapping[str, typing.Any]) -> typing.Dict[str, typing.Any]:
        """A settings dict read back as the component values it describes.

        The selection rules, the switch computation and the model read are
        all written against live component values, and a composed settings
        dict carries the same facts under its own names. Translating once
        here is what lets the flags be recomputed by exactly the code a
        person clicking the control would have run.
        """
        values: typing.Dict[str, typing.Any] = {}
        for key in COMPONENTS_BY_KEY:
            name = self.settings_key(key)
            if name and name in settings:
                values[key] = settings[name]
        return values

    def apply_media_to_settings(
        self,
        settings: typing.Dict[str, typing.Any],
        media: typing.Mapping[str, typing.Any],
    ) -> typing.List[str]:
        """Put the job's own media into a composed base, flags and all.

        Returns the receiver ids that were switched on, for the record.

        THE FLAGS ARE RECOMPUTED, NOT INHERITED. A composed base carries the
        letter strings describing which slots the *user's page* had in use,
        and this job's media are not those. Wan2GP's own back-fill only adds
        flags implied by media that is present - it never removes a flag
        whose media is gone - so a base that said "start and end frame" and a
        job that supplies only a start frame would otherwise reach
        ``generate_media`` claiming a last frame it does not have. So: every
        slot this submission does not fill is cleared first, and each slot it
        does fill is switched on through the same computation a click makes.
        """
        switched: typing.List[str] = []
        for receiver_id in V1_RECEIVERS:
            name = self.settings_key(RECEIVER_COMPONENTS[receiver_id])
            if not name:
                continue
            slot = _SLOT_FOR_RECEIVER.get(receiver_id, "")
            supplied = media.get(slot) if slot else None
            if not supplied:
                # Cleared rather than left: see the docstring. A slot with no
                # media must not be described by a flag that says it has some.
                settings[name] = [] if receiver_id == protocol.REFERENCE else None
                continue
            settings[name] = list(supplied) if receiver_id == protocol.REFERENCE else supplied[0]
        # The flags for the slots this job does not fill, struck out. This is
        # the half that is easy to leave out and that a base composed from
        # somebody else's configuration makes necessary: WanGP's own
        # back-fill only ADDS flags implied by media that is present, so a
        # base whose letters said "start and end frame" would still say so
        # after the end frame was cleared, and generation would go looking
        # for a picture that is not there. The switch below then puts back
        # exactly the letters this job's own media earns.
        for receiver_id in V1_RECEIVERS:
            slot = _SLOT_FOR_RECEIVER.get(receiver_id, "")
            if slot and media.get(slot):
                continue
            flag_key, letters = _FLAG_LETTERS.get(receiver_id, ("", ""))
            name = self.settings_key(flag_key) if flag_key else ""
            if name and name in settings:
                settings[name] = _del_letters(settings[name], letters)
        # The flags, after every value is in place, so a switch computed for
        # one slot sees the others as they will be.
        for receiver_id in V1_RECEIVERS:
            slot = _SLOT_FOR_RECEIVER.get(receiver_id, "")
            if not slot or not media.get(slot):
                continue
            switch = self.switch_for(receiver_id, self.settings_as_values(settings))
            for component_key, change in switch.updates.items():
                # Only the letter strings. The radio, the checkbox and the
                # dropdown are controls a person operates to produce them;
                # a settings dict carries the result, not the widget, and a
                # key WanGP's own schema does not have would be dropped by
                # ``clean_settings`` anyway - or worse, kept.
                if component_key not in SETTINGS_FLAG_COMPONENTS:
                    continue
                name = self.settings_key(component_key)
                if name:
                    settings[name] = change
            if switch.token:
                switched.append(receiver_id)
        return switched

    def residency_key(self, settings: typing.Mapping[str, typing.Any], model_type: str = "") -> str:
        """B7B rule 5's key, as far as this build will say.

        Not ``model_type`` alone. The reload branch fires on model_type,
        profile, config or ``reload_needed``, and the profile is derived per
        output type rather than per model - so switching one model between
        image and video output changes it - while a change of spatial
        upsampling can change the VAE-upsampling requirement and set
        ``reload_needed`` on its own. A job that forces a reload records
        which of these moved, which is the only way to catch a setting that
        is quietly defeating residency.
        """
        chosen = str(model_type or settings.get(self.settings_key(MODEL_SELECTOR)) or settings.get("model_type") or "")
        image_mode = settings.get(self.settings_key(IMAGE_MODE))
        profile = ""
        getter = self.host.read_global("get_profile_type_for_model")
        if callable(getter) and chosen:
            try:
                profile = str(getter(chosen, image_mode))
            except Exception:
                profile = ""
        upsampling = settings.get("spatial_upsampling")
        return "|".join(
            [
                chosen,
                profile,
                str(image_mode if image_mode is not None else ""),
                str(upsampling if upsampling is not None else ""),
            ]
        )[:200]

    def factory_settings(self, model_type: str) -> typing.Optional[dict]:
        """The settings WanGP itself loads for a model, if it will say.

        ``get_default_settings`` is the preferred seam and is better than its
        name: it reads the model's saved settings file - the one WanGP writes
        and then loads into the page whenever you switch to that model - and
        only falls back to factory values when no such file exists yet. So
        what comes back here is usually the user's own configuration at rest.

        It is still the fallback of last resort and never the preferred base,
        because "at rest" is not "on screen": anything changed in the page
        and not committed is not in it. A base that came from here is
        recorded as such and said out loud on the job.
        """
        for name in ("get_default_settings", "get_factory_settings"):
            getter = self.host.read_global(name)
            if not callable(getter):
                continue
            try:
                found = getter(model_type)
            except Exception:
                continue
            if isinstance(found, dict) and found:
                return dict(found)
        return None

    def recorded_form(self, service: typing.Any, model_type: str) -> typing.Optional[dict]:
        """The settings the user last committed for this model, process-wide.

        THE BROWSER-INDEPENDENT BASE, AND THE REASON COMPOSE CAN RUN AT ALL.

        The obvious seam - the session's own stored settings - cannot be used
        by a server-side caller, and not merely because the answer would be
        wrong. A fresh session's dict has no settings for the model, so the
        getter returns None, and the preferred variant then assigns a key on
        that None and raises. It also mutates what it reads: it takes the
        session's stored dict by reference, sets a key on it and hands it to
        a normaliser that pops keys and rewrites the LoRA list, so composing
        would change what the user's own tab will generate next.

        Wan2GP already keeps what is wanted instead: ``save_inputs`` records
        a process-wide snapshot of the committed form per model beside the
        session copy, shared across sessions for the same reason the ``gen``
        dict is. It is not factory defaults - it is what the user last
        committed - and reading it needs no session, mutates nothing, and
        works when no page has ever been open.
        """
        if service is None or not model_type:
            return None
        for name in ("load_model_form", "get_model_form", "model_form"):
            loader = getattr(service, name, None)
            if not callable(loader):
                continue
            try:
                found = loader(model_type)
            except Exception:
                continue
            if isinstance(found, dict) and found:
                return copy.deepcopy(found)
        return None

    def recorded_fingerprint(self, service: typing.Any, model_type: str) -> str:
        """A short, stable digest of the recorded form, or "" if there is none.

        THE ONLY WAY A FLUSH CAN BE OBSERVED RATHER THAN ASSUMED.

        Writing ``save_form_trigger`` starts a Gradio chain that runs *after*
        the event which wrote it has returned, so no single call can both ask
        for the commit and see its result. Nothing in Wan2GP counts these
        commits either - ``record_model_form`` just replaces a dict - so the
        observable has to be built here, out of the recorded form itself.

        Hashed rather than compared field by field because the caller only
        ever asks one question ("has it changed since I asked you to flush"),
        and because a digest cannot accidentally carry a prompt or a path back
        to a browser the way a settings diff could.
        """
        recorded = self.recorded_form(service, model_type)
        if not recorded:
            return ""
        try:
            canonical = json.dumps(recorded, sort_keys=True, default=repr)
        except Exception:
            return ""
        return hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()[:32]

    def current_model_type(self, service: typing.Any = None) -> str:
        """The model this child is on, with no page to ask.

        Read off the shared state where there is one, then off the injected
        global. Empty is a real answer - a child nobody has chosen a model in
        - and the caller composes for the model the job named instead.
        """
        gen = self.shared_gen(service)
        if isinstance(gen, dict):
            for key in ("model_type", "last_model_type"):
                found = gen.get(key)
                if isinstance(found, str) and found:
                    return found
        found = self.host.read_global("model_type")
        return found if isinstance(found, str) else ""

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
                # Protocol 3. False on a build that lacks a queue-critical
                # component: the image send still works, a queue request is
                # refused with BRIDGE_COMPONENT_INCOMPATIBLE naming it.
                "queue": bool(ready and not self.queue_missing()),
                # Protocol 4. Whether a request may start a generation here:
                # the queue set plus the generate trigger. Without it "auto"
                # still queues, and says so.
                "start": bool(ready and not self.queue_missing() and not self.start_missing()),
                # Protocol 5. Where this page's admitted tasks are in WanGP's
                # queue: needs the same session state the queue needs.
                "track": bool(ready and not self.queue_missing()),
                # The heartbeat and the change notices. Both are the script in
                # the WanGP document talking for itself - no component, no
                # Gradio event - so they are offered whatever the form made
                # of this build; a parent that never hears this word never
                # sends a PING and never counts on being told of a change.
                "ping": True,
                "form_watch": True,
                # Bridge 1.8.0: every answer says whether the page's Gradio
                # session is still the one it had (``session`` in the ack),
                # and the guard keeps a session heard recently from being
                # expired after a dropped heartbeat. See session_guard.
                "session": True,
                # Theme is presentation. Section 27.1: it may fail on its own
                # without taking image handoff with it, so it is reported
                # separately and never gates ``ready``.
                "theme": True,
            },
            # Live, and process-wide: true while any page's generation runs,
            # false when none does, None when this build cannot say.
            "generation_running": self.generation_running(),
        }
        if not ready:
            payload["code"] = BRIDGE_COMPONENT_INCOMPATIBLE
            missing = list(resolution.missing_mandatory)
            if not version_ok:
                missing.append(f"wan2gp_version={self.wan2gp_version or 'unknown'}")
            payload["missing"] = missing
        if resolution.missing_optional:
            payload["absent"] = list(resolution.missing_optional)
        if ready and self.queue_missing():
            payload["queue_missing"] = list(self.queue_missing())
        if ready and self.start_missing():
            payload["start_missing"] = list(self.start_missing())
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
