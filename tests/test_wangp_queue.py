"""The queue operation, inside the plugin: overlay, admit, confirm, restore.

Section 0 of the Clipboard design intent is one rule - a field the caller
leaves out is the page's own, a field it supplies overrides the page for that
one queued task - and every check here is that rule meeting the mechanics
that carry it. A request with nothing in it writes WanGP's client id and its
add-to-queue trigger and touches nothing else. A prompt goes into the box
generation actually reads, wizard on or off. An image is a replacement, never
an append, and one the live model cannot take is reported and left out
without stopping the rest. The trigger is never written when an image fails
to stage, when the id was already used, or when another request still owns
the form.

Confirmation is the bounded admission check of section 15.2 and nothing more:
a task carrying the request as its client id proves it queued and that proof
sticks; a correlated queue error refuses it; time running out expires it; and
the absence of a task is never a refusal. Whichever way it ends, the overrides
go back - each only where the page still holds what the bridge wrote, so a
user's edit made meanwhile is theirs.

No WanGP, no Gradio event, no browser: the bridge is built on a stub host with
the real component names, driven through its own ``handle`` with a dictionary
of live values, and read back through ``outputs``.
"""

from harness import Results, setup_path

setup_path()

import json  # noqa: E402
import os  # noqa: E402
import pathlib  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402

from minipaint_neo.wangp import protocol  # noqa: E402

BRIDGE_DIR = pathlib.Path(__file__).resolve().parents[1] / "wan2gp_bridge" / "wan2gp-minipaint-bridge"

#: The generator form's real variable names, as WanGP hands them over,
#: including the six the queue needs.
FORM_NAMES = (
    "image_start", "image_end", "image_refs", "image_prompt_type", "video_prompt_type", "model_choice",
    "image_mode", "state", "image_prompt_type_radio", "image_prompt_type_endcheckbox",
    "video_prompt_type_image_refs", "image_start_row", "image_end_row", "image_refs_row",
    "prompt", "wizard_prompt", "wizard_prompt_activated_var", "client_id", "add_to_queue_trigger",
    "current_gallery_tab",
)
GALLERIES = ("image_start", "image_end", "image_refs")

VIDEO_MODEL = {
    "name": "A video model",
    "image_prompt_types_allowed": "TSEV",
    "image_ref_choices": {"choices": [("None", ""), ("People / Objects", "I"), ("Landscape then people", "KI")],
                          "letters_filter": "KFI"},
}
TEXT_ONLY_MODEL = {"name": "Text only", "image_prompt_types_allowed": "T"}

ID_A = "0123456789abcdef0123456789abcdef"
ID_B = "fedcba9876543210fedcba9876543210"
ID_C = "aaaaaaaabbbbbbbbccccccccdddddddd"
ID_D = "11112222333344445555666677778888"
ID_MISSING = "99990000aaaabbbbccccddddeeeeffff"


def _modules():
    """The plugin's modules, loaded off its folder the way WanGP loads them."""
    added = str(BRIDGE_DIR) not in sys.path
    if added:
        sys.path.insert(0, str(BRIDGE_DIR))
    try:
        import admission
        import bridge_ui
        import compatibility
        import plugin
    finally:
        if added and str(BRIDGE_DIR) in sys.path:
            sys.path.remove(str(BRIDGE_DIR))
    return plugin, compatibility, admission, bridge_ui


class _Wgp:
    """What a plugin sees of Wan2GP: the hooks, and the globals it injects as
    attributes - the two the bridge reads a page's model through, and the
    two the queue reads: the trigger value the native button writes, and the
    per-page generation record."""

    def __init__(self, definitions):
        self.definitions = definitions
        self.asked_globals = []
        self.unique = 0

    def request_component(self, name):
        pass

    def request_global(self, name):
        self.asked_globals.append(name)

    def get_model_def(self, model_type):
        return self.definitions.get(model_type)

    @staticmethod
    def get_state_model_type(state):
        return state["model_type"]

    def get_unique_id(self):
        self.unique += 1
        return f"unique-{self.unique}"

    @staticmethod
    def get_gen_info(state):
        return state["gen"]


class _Handed:
    def __init__(self, name):
        self._id = name
        self.name = name

    def get_config(self):
        return {}


class Gallery(_Handed):
    """Named like Gradio's Gallery: what Wan2GP's start and end frames are."""


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _bridge(definitions, root, names=FORM_NAMES, galleries=GALLERIES, clock=None):
    plugin, compatibility, _admission, _ui = _modules()
    wgp = _Wgp(definitions)
    bridge = plugin.MiniPaintBridge(
        host=compatibility.Host(wgp),
        environ={"MINIPAINT_WANGP_INSTANCE_ID": "i", "MINIPAINT_WANGP_HANDOFF_ROOT": str(root)},
        clock=clock or _Clock(),
    )
    bridge.compat.declare_globals()
    bridge.compat.host.accept_components({name: (Gallery(name) if name in galleries else _Handed(name)) for name in names})
    bridge.resolve()
    return bridge, wgp


def _page(compatibility, **overrides):
    """A fresh page on a video model, keyed by the bridge's component keys."""
    page = {
        compatibility.IMAGE_PROMPT_TYPE: "T",
        compatibility.VIDEO_PROMPT_TYPE: "",
        compatibility.IMAGE_MODE: 0,
        compatibility.SESSION_STATE: {"model_type": "video", "gen": {"queue": []}},
        compatibility.MODEL_SELECTOR: "video",
        compatibility.START_IMAGE: None,
        compatibility.END_IMAGE: None,
        compatibility.REFERENCE_GALLERY: [],
        compatibility.IMAGE_PROMPT_RADIO: "T",
        compatibility.END_IMAGES_CHECKBOX: False,
        compatibility.REFERENCE_SELECTOR: "",
        compatibility.PROMPT: "the page's own prompt",
        compatibility.WIZARD_PROMPT: "",
        compatibility.WIZARD_ACTIVE: "off",
        compatibility.CLIENT_ID: "",
        compatibility.GALLERY_TAB: 0,
    }
    page.update(overrides)
    return page


def _call(bridge, live, op, queue=None, correlation="r1"):
    """One bridge event with this page's values, as Gradio would call it."""
    request = {"op": op, "request_id": correlation, "channel_id": "c" * 32}
    if queue is not None:
        request["queue"] = queue
    values = [live.get(key) for key in bridge.state_keys]
    return bridge.handle(json.dumps(request), values, "session-hash-of-this-page")


def _raw(value):
    """A write's plain value, whatever wrapper it travels in."""
    return value.value if hasattr(value, "value") and type(value).__name__ == "Raw" else value


def _apply(live, writes):
    """The page as Gradio will have it once these outputs land."""
    after = dict(live)
    for key, value in (writes or {}).items():
        if isinstance(value, dict):
            continue  # a property update (a row shown), not a value
        after[key] = _raw(value)
    return after


def _pictures(root):
    from PIL import Image

    for handoff_id, colour in ((ID_A, (10, 200, 30, 255)), (ID_B, (200, 10, 30, 255)), (ID_C, (30, 10, 200, 255)), (ID_D, (90, 90, 90, 255))):
        Image.new("RGBA", (4, 4), colour).save(pathlib.Path(root) / f"{handoff_id}.png", format="PNG")


# --------------------------------------------------------------- the request --


def request_checks(r: Results, root: str) -> None:
    plugin, compatibility, admission, bridge_ui = _modules()
    session = plugin.bridge_session_for("session-hash-of-this-page", "i")
    K = compatibility

    # -- the handshake says the queue is on offer
    bridge, wgp = _bridge({"video": VIDEO_MODEL, "text": TEXT_ONLY_MODEL}, root)
    r.check("the queue globals are asked for when the plugin is constructed",
            {"get_unique_id", "get_gen_info"} <= set(wgp.asked_globals), str(wgp.asked_globals))
    fresh = _page(K)
    hello, _ = _call(bridge, fresh, "hello")
    r.check("a build with every queue component offers the queue in its handshake",
            hello.get("ready") is True and hello.get("capabilities", {}).get("queue") is True, str(hello.get("capabilities")))
    r.check("the event's inputs include the prompt boxes, the wizard flag and the client id",
            {K.PROMPT, K.WIZARD_PROMPT, K.WIZARD_ACTIVE, K.CLIENT_ID} <= set(bridge.state_keys), str(bridge.state_keys))
    r.check("and the queue-only outputs follow the receivers and the switches, in a fixed order",
            bridge.queue_keys == (K.PROMPT, K.WIZARD_PROMPT, K.CLIENT_ID, K.ADD_TO_QUEUE_TRIGGER), str(bridge.queue_keys))

    # -- no overrides: the live page as it is
    ack, writes = _call(bridge, fresh, "queue", {"request_id": ID_A, "bridge_session": session})
    r.check("an empty request is admitted", ack.get("ok") is True and ack.get("admission") == "requested", str(ack.get("code")))
    r.check("and writes only the client id and the trigger",
            set(writes) == {K.CLIENT_ID, K.ADD_TO_QUEUE_TRIGGER}, str(sorted(writes)))
    r.check("the client id is the request id", _raw(writes[K.CLIENT_ID]) == ID_A)
    r.check("the trigger carries what WanGP's own button would write", _raw(writes[K.ADD_TO_QUEUE_TRIGGER]) == "unique-1")
    r.check("every field reads as inherited", ack.get("inherited") == list(protocol.QUEUE_FIELDS), str(ack.get("inherited")))
    r.check("and nothing as applied", not any(ack["applied"][field] for field in protocol.QUEUE_FIELDS), str(ack.get("applied")))
    r.check("the answer names the public request id", ack.get("queue_request_id") == ID_A)
    r.check("the answer carries the page's model", ack.get("model", {}).get("label") == "A video model", str(ack.get("model")))
    r.check("and never the prompt", "prompt" not in json.dumps({k: v for k, v in ack.items() if k != "applied" and k != "inherited"}))
    outputs = bridge.outputs(ack, writes)
    r.check("the event returns the acknowledgement, the receivers, the switches, then the queue components",
            len(outputs) == 1 + len(bridge.receiver_keys) + len(bridge.switch_keys) + len(bridge.queue_keys), str(len(outputs)))
    tail = dict(zip(bridge.queue_keys, outputs[1 + len(bridge.receiver_keys) + len(bridge.switch_keys):]))
    r.check("the trigger output is at its own position", tail.get(K.ADD_TO_QUEUE_TRIGGER) == "unique-1", repr(tail))
    r.check("and the prompt output is no change",
            isinstance(tail.get(K.PROMPT), dict) and "value" not in tail[K.PROMPT], repr(tail.get(K.PROMPT)))

    # -- a prompt: into the box generation reads
    bridge, _ = _bridge({"video": VIDEO_MODEL}, root)
    ack, writes = _call(bridge, fresh, "queue", {"request_id": ID_B, "bridge_session": session, "prompt": "  a cat on a mat\r\n\x07second line  "})
    r.check("a prompt override goes into the plain prompt box when the wizard is off",
            _raw(writes.get(K.PROMPT)) == "a cat on a mat\nsecond line", repr(_raw(writes.get(K.PROMPT))))
    r.check("and not into the wizard's", K.WIZARD_PROMPT not in writes)
    r.check("and is reported applied", ack["applied"]["prompt"] is True and "prompt" not in ack["inherited"])
    wizard = _page(K, **{K.WIZARD_ACTIVE: "on", K.WIZARD_PROMPT: "wizard text"})
    bridge, _ = _bridge({"video": VIDEO_MODEL}, root)
    ack, writes = _call(bridge, wizard, "queue", {"request_id": ID_B, "bridge_session": session, "prompt": "a dog"})
    r.check("with the wizard on the prompt goes into wizard_prompt",
            _raw(writes.get(K.WIZARD_PROMPT)) == "a dog" and K.PROMPT not in writes, str(sorted(writes)))
    bridge, _ = _bridge({"video": VIDEO_MODEL}, root)
    ack, writes = _call(bridge, fresh, "queue", {"request_id": ID_B, "bridge_session": session, "prompt": "   "})
    r.check("a whitespace prompt is inheritance, not a clear",
            K.PROMPT not in writes and "prompt" in ack["inherited"], str(sorted(writes)))
    bridge, _ = _bridge({"video": VIDEO_MODEL}, root)
    ack, writes = _call(bridge, fresh, "queue", {"request_id": ID_B, "bridge_session": session, "prompt": "x" * (protocol.PROMPT_MAX_CHARS + 1)})
    r.check("a prompt over the ceiling is refused, not cut", ack.get("ok") is False and ack.get("code") == "PROMPT_TOO_LONG", str(ack.get("code")))
    r.check("and nothing is written for it", writes is None)

    # -- images: exact replacements, and the switch a send would make
    bridge, _ = _bridge({"video": VIDEO_MODEL}, root)
    ack, writes = _call(bridge, fresh, "queue", {"request_id": ID_A, "bridge_session": session, "start_handoff_id": ID_A})
    start = _raw(writes.get(K.START_IMAGE))
    r.check("a start override is a one-element list on a gallery-shaped start frame",
            isinstance(start, list) and len(start) == 1 and getattr(start[0], "size", None) == (4, 4), repr(type(start)))
    r.check("a supported but unselected start frame is switched on: the radio",
            _raw(writes.get(K.IMAGE_PROMPT_RADIO)) == "S", repr(writes.get(K.IMAGE_PROMPT_RADIO)))
    r.check("and the hidden letters", _raw(writes.get(K.IMAGE_PROMPT_TYPE)) == "S")
    r.check("and the row is shown", writes.get(K.START_ROW) == {"visible": True})
    r.check("applied says start", ack["applied"]["start"] is True and ack["inherited"] == ["prompt", "end", "references"], str(ack.get("inherited")))
    r.check("the trigger is written after the overrides", K.ADD_TO_QUEUE_TRIGGER in writes and K.CLIENT_ID in writes)

    with_refs = _page(K, **{K.REFERENCE_GALLERY: [("old-one.png", None), ("old-two.png", None)], K.VIDEO_PROMPT_TYPE: "KI"})
    bridge, _ = _bridge({"video": VIDEO_MODEL}, root)
    ack, writes = _call(bridge, with_refs, "queue", {"request_id": ID_C, "bridge_session": session,
                                                    "reference_handoff_ids": [ID_B, ID_C], "end_handoff_id": ID_D})
    refs = _raw(writes.get(K.REFERENCE_GALLERY))
    r.check("a reference override replaces the list rather than appending to it",
            isinstance(refs, list) and len(refs) == 2 and all(hasattr(item, "size") for item in refs), repr(refs)[:80])
    r.check("and is counted", ack["applied"]["references"] == 2, str(ack.get("applied")))
    r.check("references already on stay on: no reference switch is written", K.REFERENCE_SELECTOR not in writes)
    end = _raw(writes.get(K.END_IMAGE))
    r.check("an end override is a one-element list", isinstance(end, list) and len(end) == 1)
    r.check("an end frame on a page with no start chosen ticks the box and chooses a start too",
            _raw(writes.get(K.END_IMAGES_CHECKBOX)) is True and _raw(writes.get(K.IMAGE_PROMPT_RADIO)) == "S"
            and set(_raw(writes.get(K.IMAGE_PROMPT_TYPE)) or "") >= {"S", "E"}, repr(writes.get(K.IMAGE_PROMPT_TYPE)))

    bridge, _ = _bridge({"video": VIDEO_MODEL}, root)
    ack, writes = _call(bridge, fresh, "queue", {"request_id": ID_D, "bridge_session": session,
                                                "start_handoff_id": ID_A, "end_handoff_id": ID_B})
    r.check("start then end in one request: the letters carry both",
            set(_raw(writes.get(K.IMAGE_PROMPT_TYPE)) or "") == {"S", "E"}, repr(writes.get(K.IMAGE_PROMPT_TYPE)))

    # -- an unsupported image: ignored, kept out of the form, the rest queued
    text = _page(K, **{K.SESSION_STATE: {"model_type": "text", "gen": {"queue": []}}, K.MODEL_SELECTOR: "text"})
    bridge, _ = _bridge({"video": VIDEO_MODEL, "text": TEXT_ONLY_MODEL}, root)
    ack, writes = _call(bridge, text, "queue", {"request_id": ID_A, "bridge_session": session,
                                               "start_handoff_id": ID_A, "reference_handoff_ids": [ID_B], "prompt": "still queued"})
    r.check("a model that takes no image still admits the request", ack.get("ok") is True and ack.get("admission") == "requested", str(ack.get("code")))
    r.check("the images are reported ignored with the reason",
            ack.get("ignored") == [{"field": "start", "code": "RECEIVER_DISABLED"}, {"field": "references", "code": "RECEIVER_DISABLED"}],
            str(ack.get("ignored")))
    r.check("and not written", K.START_IMAGE not in writes and K.REFERENCE_GALLERY not in writes and K.IMAGE_PROMPT_RADIO not in writes)
    r.check("while the prompt still is", _raw(writes.get(K.PROMPT)) == "still queued")
    r.check("an ignored field is neither applied nor inherited",
            ack["applied"]["start"] is False and "start" not in ack["inherited"] and ack["inherited"] == ["end"], str(ack.get("inherited")))

    # -- a failed image refuses the whole request before anything is written
    bridge, _ = _bridge({"video": VIDEO_MODEL}, root)
    ack, writes = _call(bridge, fresh, "queue", {"request_id": ID_A, "bridge_session": session,
                                                "prompt": "never written", "start_handoff_id": ID_MISSING})
    r.check("a handoff that is not there refuses the request", ack.get("ok") is False and ack.get("code") == "HANDOFF_NOT_FOUND", str(ack.get("code")))
    r.check("and the trigger is not written", writes is None)
    r.check("and the refusal still names the request", ack.get("queue_request_id") == ID_A and ack.get("admission") == "refused")
    ack, writes = _call(bridge, fresh, "queue", {"request_id": ID_A, "bridge_session": session, "prompt": "now it works"})
    r.check("a refused request leaves no record: the same id can be used again",
            ack.get("ok") is True and ack.get("admission") == "requested", str(ack.get("code")))

    # -- idempotency and ownership
    bridge, wgp = _bridge({"video": VIDEO_MODEL}, root)
    first, writes = _call(bridge, fresh, "queue", {"request_id": ID_A, "bridge_session": session, "prompt": "once"})
    again, writes_again = _call(bridge, _apply(fresh, writes), "queue", {"request_id": ID_A, "bridge_session": session, "prompt": "once"})
    r.check("the same request again is a duplicate", again.get("ok") is True and again.get("admission") == "duplicate", str(again.get("code")))
    r.check("and writes nothing - no second trigger", writes_again == {})
    r.check("so WanGP's button value was minted once", wgp.unique == 1, str(wgp.unique))
    conflict, _ = _call(bridge, _apply(fresh, writes), "queue", {"request_id": ID_A, "bridge_session": session, "prompt": "twice"})
    r.check("the same id with another payload is a conflict", conflict.get("code") == "REQUEST_ID_CONFLICT", str(conflict.get("code")))
    busy, busy_writes = _call(bridge, _apply(fresh, writes), "queue", {"request_id": ID_B, "bridge_session": session, "prompt": "me too"})
    r.check("a distinct request while the first owns the form is refused QUEUE_BUSY", busy.get("code") == "QUEUE_BUSY", str(busy.get("code")))
    r.check("with no component and no trigger written", busy_writes is None)
    r.check("the ledger still has one owner", bridge.ledger.owner(session) is not None and bridge.ledger.owner(session).request_id == ID_A)

    # -- the session and the components
    other, _ = _call(bridge, fresh, "queue", {"request_id": ID_C, "bridge_session": "somebody-else"})
    r.check("a request prepared for another page is refused", other.get("code") == "BRIDGE_SESSION_MISMATCH")
    lesser, _ = _bridge({"video": VIDEO_MODEL}, root, names=tuple(name for name in FORM_NAMES if name != "client_id"))
    hello, _ = _call(lesser, fresh, "hello")
    r.check("a build without client_id says the queue is off", hello.get("capabilities", {}).get("queue") is False
            and "client_id" in hello.get("queue_missing", []), str(hello.get("queue_missing")))
    r.check("while the image send is still ready", hello.get("ready") is True)
    ack, writes = _call(lesser, fresh, "queue", {"request_id": ID_A, "bridge_session": session})
    r.check("and a queue request is refused with BRIDGE_COMPONENT_INCOMPATIBLE",
            ack.get("code") == "BRIDGE_COMPONENT_INCOMPATIBLE" and writes is None, str(ack.get("code")))
    bad, _ = _call(bridge, fresh, "queue", {"request_id": "not-an-id", "bridge_session": session})
    r.check("a malformed request id is REQUEST_INVALID", bad.get("code") == "REQUEST_INVALID")
    bad, _ = _call(bridge, fresh, "queue", {"request_id": ID_C, "bridge_session": session, "start_handoff_id": "../x"})
    r.check("a path where a handoff id belongs is REQUEST_INVALID", bad.get("code") == "REQUEST_INVALID")
    bad, _ = _call(bridge, fresh, "queue", {"request_id": ID_C, "bridge_session": session, "model": "t2v", "resolution": "1x1"})
    r.check("unknown fields are dropped rather than acted on: the request is a valid empty one",
            bad.get("code") in ("", None, "QUEUE_BUSY"), str(bad.get("code")))

    # -- the fallback trigger value, for a build that hands over no get_unique_id
    class _Quiet(_Wgp):
        get_unique_id = None

    plugin_mod, compatibility, _a, _u = _modules()
    quiet = plugin_mod.MiniPaintBridge(host=compatibility.Host(_Quiet({"video": VIDEO_MODEL})),
                                       environ={"MINIPAINT_WANGP_INSTANCE_ID": "i", "MINIPAINT_WANGP_HANDOFF_ROOT": str(root)})
    quiet.compat.declare_globals()
    quiet.compat.host.accept_components({name: (Gallery(name) if name in GALLERIES else _Handed(name)) for name in FORM_NAMES})
    quiet.resolve()
    ack, writes = _call(quiet, fresh, "queue", {"request_id": ID_A, "bridge_session": session})
    r.check("without get_unique_id the trigger still changes", ack.get("ok") is True and str(_raw(writes.get(K.ADD_TO_QUEUE_TRIGGER))).startswith("minipaint-"))


# ---------------------------------------------------------- the confirmation --


def _queued(live, request_id, count=1):
    after = dict(live)
    state = dict(after[live and next(iter(k for k in after if k == "session_state"))])
    state["gen"] = {"queue": [{"params": {"client_id": request_id}, "id": n} for n in range(count)]}
    after["session_state"] = state
    return after


def confirm_checks(r: Results, root: str) -> None:
    plugin, compatibility, admission, bridge_ui = _modules()
    session = plugin.bridge_session_for("session-hash-of-this-page", "i")
    K = compatibility
    fresh = _page(K)

    r.check("the first confirmation is allowed at once: the schedule starts at zero",
            protocol.QUEUE_CONFIRM_DELAYS_MS[0] == 0 and all(a < b for a, b in zip(protocol.QUEUE_CONFIRM_DELAYS_MS, protocol.QUEUE_CONFIRM_DELAYS_MS[1:])))

    clock = _Clock()
    bridge, _ = _bridge({"video": VIDEO_MODEL}, root, clock=clock)
    ack, writes = _call(bridge, fresh, "queue", {"request_id": ID_A, "bridge_session": session, "prompt": "queued prompt", "start_handoff_id": ID_A})
    after = _apply(fresh, writes)

    # -- pending: no task, no error, not yet expired
    status, more = _call(bridge, after, "confirm", {"request_id": ID_A, "bridge_session": session})
    r.check("with no task and no error the admission is pending", status.get("ok") is True and status.get("status") == "pending", str(status))
    r.check("and never refused merely by absence", status.get("code", "") == "")
    r.check("and nothing is restored yet", more == {})
    r.check("the record still owns the form", bridge.ledger.owner(session) is not None)

    # -- queued: a task carries the client id
    landed = _queued(after, ID_A, count=2)
    status, restore = _call(bridge, landed, "confirm", {"request_id": ID_A, "bridge_session": session})
    r.check("a task carrying the request as its client id proves it queued", status.get("status") == "queued", str(status))
    r.check("and the number of matching tasks is reported", status.get("tasks_added") == 2, str(status.get("tasks_added")))
    r.check("the terminal confirmation restores the client id", _raw(restore.get(K.CLIENT_ID)) == "")
    r.check("and the prompt, still what the bridge wrote", _raw(restore.get(K.PROMPT)) == "the page's own prompt")
    r.check("and the start frame", K.START_IMAGE in restore and _raw(restore[K.START_IMAGE]) is None)
    r.check("and the selector with its letters, together",
            _raw(restore.get(K.IMAGE_PROMPT_RADIO)) == "T" and _raw(restore.get(K.IMAGE_PROMPT_TYPE)) == "T", str(sorted(restore)))
    r.check("the trigger is not touched by a restore", K.ADD_TO_QUEUE_TRIGGER not in restore)
    r.check("the answer says what was put back", set(status.get("restored", [])) >= {K.PROMPT, K.CLIENT_ID, K.START_IMAGE, K.IMAGE_PROMPT_RADIO})
    r.check("ownership is released", bridge.ledger.owner(session) is None)
    record = bridge.ledger.get(session, ID_A)
    r.check("the private originals are dropped once terminal", record is not None and record.originals == {} and record.written == {})

    # -- sticky: the task leaves the queue, the answer stays queued
    gone = dict(after)
    status, more = _call(bridge, gone, "confirm", {"request_id": ID_A, "bridge_session": session})
    r.check("a request once seen queued stays queued when its task has left the queue", status.get("status") == "queued" and status.get("tasks_added") == 2)
    r.check("and is not restored twice", more == {})

    # -- refused: explicit, correlated evidence only
    bridge, _ = _bridge({"video": VIDEO_MODEL}, root, clock=clock)
    ack, writes = _call(bridge, fresh, "queue", {"request_id": ID_B, "bridge_session": session, "prompt": "will be refused"})
    after = _apply(fresh, writes)
    errored = dict(after)
    errored[K.SESSION_STATE] = {"model_type": "video", "gen": {"queue": [], "queue_errors": {ID_B: "resolution not supported"}}}
    status, restore = _call(bridge, errored, "confirm", {"request_id": ID_B, "bridge_session": session})
    r.check("a queue error keyed by the request refuses it", status.get("status") == "refused" and status.get("code") == "WANGP_VALIDATION_REFUSED", str(status))
    r.check("and restores the prompt", _raw(restore.get(K.PROMPT)) == "the page's own prompt")
    bridge, _ = _bridge({"video": VIDEO_MODEL}, root, clock=clock)
    _call(bridge, fresh, "queue", {"request_id": ID_B, "bridge_session": session})
    someone_else = dict(fresh)
    someone_else[K.SESSION_STATE] = {"model_type": "video", "gen": {"queue": [{"params": {"client_id": ID_C}}], "queue_errors": {ID_C: "theirs"}}}
    status, _ = _call(bridge, someone_else, "confirm", {"request_id": ID_B, "bridge_session": session})
    r.check("another client's task and error prove nothing about this request", status.get("status") == "pending", str(status))

    # -- expired: time runs out with neither
    bridge, _ = _bridge({"video": VIDEO_MODEL}, root, clock=clock)
    ack, writes = _call(bridge, fresh, "queue", {"request_id": ID_C, "bridge_session": session, "prompt": "nobody answers"})
    after = _apply(fresh, writes)
    clock.now += protocol.PENDING_ADMISSION_SECONDS + 1
    status, restore = _call(bridge, after, "confirm", {"request_id": ID_C, "bridge_session": session})
    r.check("a pending admission past its time expires, unconfirmed", status.get("status") == "expired" and status.get("code") == "ADMISSION_UNCONFIRMED", str(status))
    r.check("and restores what it wrote", _raw(restore.get(K.PROMPT)) == "the page's own prompt" and _raw(restore.get(K.CLIENT_ID)) == "")
    r.check("and releases the form", bridge.ledger.owner(session) is None)

    # -- an unknown request id
    status, _ = _call(bridge, fresh, "confirm", {"request_id": ID_D, "bridge_session": session})
    r.check("a confirmation for an unknown id is refused, not invented", status.get("ok") is False and status.get("code") == "REQUEST_INVALID", str(status))

    # -- an expired owner is cleaned up by the next distinct request, which then proceeds
    bridge, _ = _bridge({"video": VIDEO_MODEL}, root, clock=clock)
    ack, writes = _call(bridge, fresh, "queue", {"request_id": ID_A, "bridge_session": session, "prompt": "stale overlay"})
    after = _apply(fresh, writes)
    clock.now += protocol.PENDING_ADMISSION_SECONDS + 1
    ack2, writes2 = _call(bridge, after, "queue", {"request_id": ID_B, "bridge_session": session})
    r.check("a new request after the owner expired is admitted", ack2.get("admission") == "requested", str(ack2.get("code")))
    r.check("and the stale prompt is put back in the same event", _raw(writes2.get(K.PROMPT)) == "the page's own prompt", str(sorted(writes2)))
    r.check("so the new request cannot inherit the previous overlay",
            bridge.ledger.get(session, ID_B).originals.get(K.CLIENT_ID) == "" and K.PROMPT not in bridge.ledger.get(session, ID_B).originals)
    r.check("and the old record is expired", bridge.ledger.get(session, ID_A).status == "expired")


# ---------------------------------------------------------- protocol 5 --


def track_checks(r: Results, root: str) -> None:
    """Where this page's admitted tasks are in WanGP's queue, and the model
    a request may insist on. Read from the page's own record, never written;
    finished only for a request this page once saw queued; another page's
    session cannot ask; a request composed for another model is refused
    before anything is written."""
    plugin, compatibility, admission, bridge_ui = _modules()
    session = plugin.bridge_session_for("session-hash-of-this-page", "i")
    K = compatibility
    fresh = _page(K)
    clock = _Clock()
    bridge, _ = _bridge({"video": dict(VIDEO_MODEL, architecture="wan_video_base")}, root, clock=clock)

    hello, _ = _call(bridge, fresh, "hello")
    r.check("the handshake offers track beside queue and start", hello.get("capabilities", {}).get("track") is True and hello.get("protocol") == 5, str(hello.get("capabilities")))
    r.check("and the model block names the architecture the definition carries",
            hello.get("model", {}).get("architecture") == "wan_video_base" and hello["model"]["type"] == "video", str(hello.get("model")))

    ack, writes = _call(bridge, fresh, "queue", {"request_id": ID_A, "bridge_session": session, "prompt": "to be tracked"})
    after = _apply(fresh, writes)
    tracked, more = _call(bridge, after, "track", {"request_ids": [ID_A], "bridge_session": session})
    r.check("before any task exists a request this page has not seen queued is unknown, not finished",
            tracked.get("ok") is True and tracked["tracked"][ID_A]["state"] == "unknown" and more == {}, str(tracked))
    landed = _queued(after, ID_A, count=1)
    status, _restore = _call(bridge, landed, "confirm", {"request_id": ID_A, "bridge_session": session})
    r.check("(the admission is confirmed)", status.get("status") == "queued")
    tracked, more = _call(bridge, landed, "track", {"request_ids": [ID_A, ID_D], "bridge_session": session})
    r.check("a track answers per request: waiting at the head for the queued task, unknown for one this page never admitted",
            tracked.get("ok") is True and tracked["tracked"][ID_A] == {"state": "waiting", "position": 0, "queue_depth": 0}
            and tracked["tracked"][ID_D]["state"] == "unknown" and tracked.get("queue_length") == 1 and more == {}, str(tracked))
    behind = dict(landed)
    behind[K.SESSION_STATE] = {"model_type": "video", "gen": {"queue": [{"params": {"client_id": ID_C}}, {"params": {"client_id": ID_A}}], "in_progress": True}}
    tracked, _ = _call(bridge, behind, "track", {"request_ids": [ID_A], "bridge_session": session})
    r.check("a task behind another is waiting with its position, running loop or not", tracked["tracked"][ID_A] == {"state": "waiting", "position": 1, "queue_depth": 1}, str(tracked))
    running = dict(landed)
    running[K.SESSION_STATE] = {"model_type": "video", "gen": {"queue": [{"params": {"client_id": ID_A}}], "in_progress": True}}
    tracked, _ = _call(bridge, running, "track", {"request_ids": [ID_A], "bridge_session": session})
    r.check("a task at the head of a running loop is generating", tracked["tracked"][ID_A]["state"] == "generating" and tracked.get("generation_running") in (False, None), str(tracked))
    tracked, _ = _call(bridge, fresh, "track", {"request_ids": [ID_A], "bridge_session": session})
    r.check("once the task has left the queue a request this page saw admitted is finished, never unknown", tracked["tracked"][ID_A]["state"] == "finished", str(tracked))
    clock.now += protocol.ADMISSION_RECORD_SECONDS + 1
    bridge.ledger.get(session, ID_B)  # prunes the admission records
    tracked, _ = _call(bridge, fresh, "track", {"request_ids": [ID_A], "bridge_session": session})
    r.check("and stays finished long after the admission record itself is pruned", tracked["tracked"][ID_A]["state"] == "finished" and bridge.ledger.get(session, ID_A) is None)
    clock.now += admission.TRACKED_ADMISSION_SECONDS + 1
    bridge.ledger.remember_admitted(session, ID_B)
    r.check("a day later the memory has lapsed", bridge.ledger.was_admitted(session, ID_A) is False and bridge.ledger.was_admitted(session, ID_B) is True)

    tracked, _ = _call(bridge, fresh, "track", {"request_ids": [], "bridge_session": session})
    r.check("a track with no ids is refused, with an empty answer", tracked.get("ok") is False and tracked.get("code") == "REQUEST_INVALID" and tracked.get("tracked") == {}, str(tracked))
    tracked, _ = _call(bridge, fresh, "track", {"request_ids": ["nope"], "bridge_session": session})
    r.check("and so is one with an id that is not one", tracked.get("ok") is False and tracked.get("code") == "REQUEST_INVALID")
    tracked, _ = _call(bridge, fresh, "track", {"request_ids": [ID_A] * (protocol.MAX_TRACKED_REQUESTS + 1), "bridge_session": session})
    r.check("and one that asks about too many", tracked.get("ok") is False)
    other = plugin.bridge_session_for("another-page", "i")
    tracked, _ = _call(bridge, fresh, "track", {"request_ids": [ID_A], "bridge_session": other})
    r.check("another page's session cannot ask about this page's tasks", tracked.get("ok") is False and tracked.get("code") == "BRIDGE_SESSION_MISMATCH")

    # -- the model a request insists on
    ack, writes = _call(bridge, fresh, "queue", {"request_id": ID_B, "bridge_session": session, "prompt": "for another model", "model_type": "minimax_h3_fl2va"})
    r.check("a request composed for another model is MODEL_CHANGED, and nothing is written",
            ack.get("ok") is False and ack.get("code") == "MODEL_CHANGED" and ack.get("admission") == "refused" and not writes and bridge.ledger.owner(session) is None, str(ack))
    ack, writes = _call(bridge, fresh, "queue", {"request_id": ID_B, "bridge_session": session, "prompt": "for this model", "model_type": "video"})
    r.check("one composed for the model the page is on is admitted", ack.get("admission") == "requested" and _raw(writes.get(K.CLIENT_ID)) == ID_B, str(ack.get("code")))
    ack, _ = _call(bridge, fresh, "queue", {"request_id": ID_C, "bridge_session": session, "prompt": "x", "model_type": "not a model!"})
    r.check("a model type that is not one is REQUEST_INVALID", ack.get("ok") is False and ack.get("code") == "REQUEST_INVALID")
    r.check("the shared normaliser carries the model type and hashes it into the payload",
            protocol.normalize_queue_request({"request_id": ID_D, "model_type": "video"})[0].get("model_type") == "video"
            and protocol.queue_payload_hash({"request_id": ID_D, "model_type": "video"}) != protocol.queue_payload_hash({"request_id": ID_D}))


# ------------------------------------------------------------- the restore --


def restore_checks(r: Results, root: str) -> None:
    plugin, compatibility, admission, bridge_ui = _modules()
    session = plugin.bridge_session_for("session-hash-of-this-page", "i")
    K = compatibility
    fresh = _page(K)
    clock = _Clock()

    # -- the user changed the prompt meanwhile: theirs now
    bridge, _ = _bridge({"video": VIDEO_MODEL}, root, clock=clock)
    ack, writes = _call(bridge, fresh, "queue", {"request_id": ID_A, "bridge_session": session, "prompt": "the overlay", "start_handoff_id": ID_A})
    after = _apply(fresh, writes)
    after[K.PROMPT] = "what the user typed while it was pending"
    status, restore = _call(bridge, _queued(after, ID_A), "confirm", {"request_id": ID_A, "bridge_session": session})
    r.check("a prompt the user changed since the write is not restored", K.PROMPT not in restore, str(sorted(restore)))
    r.check("and the skip is reported", K.PROMPT in status.get("restore_skipped", []), str(status.get("restore_skipped")))
    r.check("while the start frame, unchanged, is", K.START_IMAGE in restore)
    r.check("the admission is still queued", status.get("status") == "queued")

    # -- the user changed the selector: the group is left whole
    bridge, _ = _bridge({"video": VIDEO_MODEL}, root, clock=clock)
    ack, writes = _call(bridge, fresh, "queue", {"request_id": ID_B, "bridge_session": session, "start_handoff_id": ID_A})
    after = _apply(fresh, writes)
    after[K.IMAGE_PROMPT_RADIO] = "V"
    after[K.IMAGE_PROMPT_TYPE] = "V"
    status, restore = _call(bridge, _queued(after, ID_B), "confirm", {"request_id": ID_B, "bridge_session": session})
    r.check("a selector the user moved is not put back", K.IMAGE_PROMPT_RADIO not in restore)
    r.check("and neither is its letter string - the group goes together", K.IMAGE_PROMPT_TYPE not in restore)
    r.check("the picture and the client id still are", K.START_IMAGE in restore and K.CLIENT_ID in restore)

    # -- WanGP's own handler reordered the letters: still the bridge's write
    bridge, _ = _bridge({"video": VIDEO_MODEL}, root, clock=clock)
    ack, writes = _call(bridge, fresh, "queue", {"request_id": ID_C, "bridge_session": session, "start_handoff_id": ID_A, "end_handoff_id": ID_B})
    after = _apply(fresh, writes)
    after[K.IMAGE_PROMPT_TYPE] = "".join(reversed(_raw(writes[K.IMAGE_PROMPT_TYPE])))
    status, restore = _call(bridge, _queued(after, ID_C), "confirm", {"request_id": ID_C, "bridge_session": session})
    r.check("letters in another order are the same selection: restored",
            _raw(restore.get(K.IMAGE_PROMPT_TYPE)) == "T" and _raw(restore.get(K.END_IMAGES_CHECKBOX)) is False, str(sorted(restore)))

    # -- the gallery came back from Gradio as paths: compared by pixels
    from PIL import Image

    bridge, _ = _bridge({"video": VIDEO_MODEL}, root, clock=clock)
    ack, writes = _call(bridge, fresh, "queue", {"request_id": ID_D, "bridge_session": session, "reference_handoff_ids": [ID_B, ID_C]})
    after = _apply(fresh, writes)
    cached = []
    for index, handoff_id in enumerate((ID_B, ID_C)):
        path = pathlib.Path(root) / f"cache-{index}.png"
        Image.open(pathlib.Path(root) / f"{handoff_id}.png").save(path, format="PNG")
        cached.append((str(path), None))
    after[K.REFERENCE_GALLERY] = cached
    status, restore = _call(bridge, _queued(after, ID_D), "confirm", {"request_id": ID_D, "bridge_session": session})
    r.check("a gallery holding the same pictures under Gradio's own paths is restored",
            K.REFERENCE_GALLERY in restore and _raw(restore[K.REFERENCE_GALLERY]) == [], str(sorted(restore)))
    bridge, _ = _bridge({"video": VIDEO_MODEL}, root, clock=clock)
    ack, writes = _call(bridge, fresh, "queue", {"request_id": ID_D, "bridge_session": session, "reference_handoff_ids": [ID_B]})
    after = _apply(fresh, writes)
    after[K.REFERENCE_GALLERY] = [cached[0], cached[1]]  # the user added one
    status, restore = _call(bridge, _queued(after, ID_D), "confirm", {"request_id": ID_D, "bridge_session": session})
    r.check("a gallery the user added to is left alone", K.REFERENCE_GALLERY not in restore)

    # -- an image send is unaffected: its outputs are placed as before
    bridge, _ = _bridge({"video": VIDEO_MODEL}, root, clock=clock)
    answer, _ = _call(bridge, fresh, "receivers")
    ack, applied = _call(bridge, fresh, "receive", correlation="r2", **{})
    r.check("a receive without a handoff is still refused as before", ack.get("ok") is False)
    outputs = bridge.outputs({"ok": True}, None)
    r.check("no-change outputs cover every wired component",
            len(outputs) == 1 + len(bridge.receiver_keys) + len(bridge.switch_keys) + len(bridge.queue_keys))

    # -- the ledger is bounded and forgets
    ledger = admission.Ledger(clock=clock)
    for index in range(protocol.MAX_ADMISSION_RECORDS + 10):
        record = admission.PendingAdmission(request_id=f"{index:032x}", bridge_session="s", payload_hash="h", created_at=clock.now)
        record.settle("queued", "", clock.now)
        ledger.add(record)
    r.check("the ledger keeps at most the declared number of records per session",
            len(ledger._sessions["s"]) == protocol.MAX_ADMISSION_RECORDS, str(len(ledger._sessions["s"])))
    clock.now += protocol.ADMISSION_RECORD_SECONDS + 1
    r.check("and a terminal record is forgotten after its time", ledger.get("s", f"{5:032x}") is None and "s" not in ledger._sessions)
    other = admission.PendingAdmission(request_id=ID_A, bridge_session="t", payload_hash="h", created_at=clock.now)
    ledger.add(other)
    r.check("a page cannot see another page's owner", ledger.owner("s") is None and ledger.owner("t") is other)


# ------------------------------------------------------- the prompt (1.14.0) --


def _edit(bridge, live, edit, correlation="p1"):
    """One prompt read or write, as the page's script submits it."""
    request = {"op": "prompt", "request_id": correlation, "channel_id": "c" * 32, "edit": edit}
    values = [live.get(key) for key in bridge.state_keys]
    return bridge.handle(json.dumps(request), values, "session-hash-of-this-page")


def prompt_edit_checks(r: Results, root: str) -> None:
    """Bridge 1.14.0: the box WanGP shows its prompt in, read and written for
    the Forge Assistant's prompt box on the WanGP tab. The box generation
    reads, wizard on or off; a write is the page's own value, kept by nothing
    and put back by nothing; and nothing of the text reaches WanGP's console."""
    plugin, compatibility, _admission, _ui = _modules()
    session = plugin.bridge_session_for("session-hash-of-this-page", "i")
    K = compatibility
    fresh = _page(K)

    bridge, _ = _bridge({"video": VIDEO_MODEL}, root)
    hello, _ = _call(bridge, fresh, "hello")
    r.check("a build that hands over the prompt box offers the prompt edit in its handshake",
            hello.get("capabilities", {}).get("prompt_edit") is True, str(hello.get("capabilities")))
    r.check("and the operation is one the trigger understands", "prompt" in plugin.OPERATIONS)

    ack, writes = _edit(bridge, fresh, {})
    r.check("a read answers with what the plain prompt box holds while the wizard is off",
            ack.get("ok") is True and ack.get("prompt") == "the page's own prompt" and ack.get("wizard") is False
            and ack.get("written") is False, str({k: ack.get(k) for k in ("ok", "code", "prompt", "wizard", "written")}))
    r.check("and writes nothing", writes == {}, str(writes))
    outputs = bridge.outputs(ack, writes)
    tail = dict(zip(bridge.queue_keys, outputs[1 + len(bridge.receiver_keys) + len(bridge.switch_keys):]))
    r.check("so the prompt output is no change",
            isinstance(tail.get(K.PROMPT), dict) and "value" not in tail[K.PROMPT], repr(tail.get(K.PROMPT)))
    r.check("the answer is correlated like every other",
            ack.get("op") == "prompt" and ack.get("request_id") == "p1" and ack.get("channel_id") == "c" * 32, str(ack.get("op")))

    wizard = _page(K, **{K.WIZARD_ACTIVE: "on", K.WIZARD_PROMPT: "the wizard's text"})
    ack, _ = _edit(bridge, wizard, {})
    r.check("with the wizard on, a read answers with the wizard's box",
            ack.get("prompt") == "the wizard's text" and ack.get("wizard") is True, str(ack.get("prompt")))

    ack, writes = _edit(bridge, fresh, {"prompt": "  a cat on a mat\r\nsecond line\n"})
    r.check("a write goes into the plain prompt box, as typed - spaces, line breaks and the trailing newline kept",
            _raw(writes.get(K.PROMPT)) == "  a cat on a mat\nsecond line\n" and K.WIZARD_PROMPT not in writes, repr(writes))
    r.check("and answers with what the box holds now",
            ack.get("ok") is True and ack.get("written") is True and ack.get("prompt") == "  a cat on a mat\nsecond line\n", str(ack.get("prompt")))
    outputs = bridge.outputs(ack, writes)
    tail = dict(zip(bridge.queue_keys, outputs[1 + len(bridge.receiver_keys) + len(bridge.switch_keys):]))
    r.check("the prompt output carries the text, and every other output is no change",
            tail.get(K.PROMPT) == "  a cat on a mat\nsecond line\n"
            and all(isinstance(value, dict) and "value" not in value for key, value in tail.items() if key != K.PROMPT), repr(tail))
    ack, writes = _edit(bridge, wizard, {"prompt": "a dog"})
    r.check("with the wizard on, a write goes into the wizard's box and nowhere else",
            _raw(writes.get(K.WIZARD_PROMPT)) == "a dog" and K.PROMPT not in writes, str(sorted(writes)))
    ack, writes = _edit(bridge, fresh, {"prompt": ""})
    r.check("an empty box is a prompt cleared", ack.get("ok") is True and _raw(writes.get(K.PROMPT)) == "", repr(writes))

    ack, writes = _edit(bridge, fresh, {"prompt": "x" * (protocol.PROMPT_MAX_CHARS + 1)})
    r.check("a write over the ceiling is PROMPT_TOO_LONG and writes nothing",
            ack.get("ok") is False and ack.get("code") == "PROMPT_TOO_LONG" and not writes, str(ack.get("code")))
    ack, writes = _edit(bridge, fresh, {"prompt": 7})
    r.check("a prompt that is not text is REQUEST_INVALID", ack.get("ok") is False and ack.get("code") == "REQUEST_INVALID" and not writes)
    long_page = _page(K, **{K.PROMPT: "y" * (protocol.PROMPT_MAX_CHARS + 1)})
    ack, _ = _edit(bridge, long_page, {})
    r.check("a page whose prompt is over the ceiling is not read out - it could not be written back",
            ack.get("ok") is False and ack.get("code") == "PROMPT_TOO_LONG" and "prompt" not in ack, str(ack.get("code")))

    # A write is not an overlay: no record, so the next queue request is not busy.
    bridge, _ = _bridge({"video": VIDEO_MODEL}, root)
    _edit(bridge, fresh, {"prompt": "from the assistant"})
    r.check("a write keeps no admission record", bridge.ledger.owner(session) is None)
    ack, _ = _call(bridge, _page(K, **{K.PROMPT: "from the assistant"}), "queue", {"request_id": ID_A, "bridge_session": session})
    r.check("so a queue request right after it is admitted, not QUEUE_BUSY", ack.get("ok") is True, str(ack.get("code")))

    # A write over a pending overlay: the overlay's restore leaves it alone.
    bridge, _ = _bridge({"video": VIDEO_MODEL}, root)
    ack, writes = _call(bridge, fresh, "queue", {"request_id": ID_B, "bridge_session": session, "prompt": "the overlay"})
    after = _apply(fresh, writes)
    _ack, written = _edit(bridge, after, {"prompt": "typed in the assistant's box"})
    after = _apply(after, written)
    status, restore = _call(bridge, _queued(after, ID_B), "confirm", {"request_id": ID_B, "bridge_session": session})
    r.check("a prompt written while a queue request's overlay is pending is not put back by its restore",
            K.PROMPT not in restore and K.PROMPT in status.get("restore_skipped", []), str(sorted(restore)) + str(status.get("restore_skipped")))

    # A build that did not hand over the prompt box.
    names = tuple(name for name in FORM_NAMES if name != "prompt")
    bridge, _ = _bridge({"video": VIDEO_MODEL}, root, names=names)
    hello, _ = _call(bridge, fresh, "hello")
    r.check("a build without the prompt box does not offer the prompt edit",
            hello.get("capabilities", {}).get("prompt_edit") is False, str(hello.get("capabilities")))
    ack, writes = _edit(bridge, fresh, {"prompt": "a cat"})
    r.check("and refuses one with BRIDGE_COMPONENT_INCOMPATIBLE, writing nothing",
            ack.get("ok") is False and ack.get("code") == "BRIDGE_COMPONENT_INCOMPATIBLE" and not writes, str(ack.get("code")))

    # WanGP's console: how much, never what.
    secret = "a very particular prompt"
    line = plugin._summary("prompt", {"prompt": secret, "wizard": False, "written": True}, 0.012)
    r.check("the console line says a prompt was written and how long it is, never the text",
            secret not in line and line.startswith("prompt: written") and str(len(secret)) in line, line)


# ----------------------------------------------------- the settings (1.17.0) --

#: The generator form's names, with the three a settings load writes.
SETTINGS_NAMES = FORM_NAMES + ("refresh_form_trigger", "model_choice_target", "main_tabs")
#: What ``time.time()`` says in WanGP's answers below.
LOADED_AT = "1761900000.25"


class _Loader:
    """WanGP's ``load_settings_from_file(state, file_path)`` as v19 has it
    (wgp.py at ee20770): the settings a file carries put in the page's state;
    for the page's own model a new ``refresh_form_trigger`` value, for
    another a ``model_choice_target`` of ``model|time`` with the switch's
    ``ignore_save_form`` set; a file it reads nothing from changes nothing and
    answers two no-changes. ``fail`` is a loader that raises."""

    def __init__(self, carried):
        self.carried = carried
        self.read = []
        self.fail = False

    def __call__(self, state, file_path):
        self.read.append(str(file_path))
        if self.fail:
            raise ValueError(f"cannot parse {file_path}")
        configs = self.carried.get(pathlib.Path(str(file_path)).name)
        if configs is None:
            return {"__type__": "update"}, {"__type__": "update"}, None
        state.setdefault("settings", {})[configs["model_type"]] = dict(configs)
        if configs["model_type"] == state["model_type"]:
            return LOADED_AT, {"__type__": "update"}, None
        state["ignore_save_form"] = True
        return {"__type__": "update"}, configs["model_type"] + "|" + LOADED_AT, None


def _load(bridge, live, settings, correlation="s1"):
    """One settings load, as the page's script submits it."""
    request = {"op": "settings", "request_id": correlation, "channel_id": "c" * 32, "settings": settings}
    values = [live.get(key) for key in bridge.state_keys]
    return bridge.handle(json.dumps(request), values, "session-hash-of-this-page")


def _note(root, handoff_id, body):
    """A reference note in the handoff root: Forge's shape when ``body`` is a path."""
    note = pathlib.Path(root) / (handoff_id + protocol.HANDOFF_REFERENCE_SUFFIX)
    text = body if isinstance(body, str) and not body.startswith("/") and not body[1:3] == ":\\" else None
    if text is None:
        text = json.dumps({"path": str(body), "written": "2026-10-31T12:00:00Z"})
    note.write_text(text, encoding="utf-8")
    return note


def _unchanged(value):
    """``gr.update()`` with nothing in it - or None where Gradio is absent."""
    return value is None or (isinstance(value, dict) and set(value) <= {"__type__"})


def settings_load_checks(r: Results, root: str) -> None:
    """Bridge 1.17.0: a file's settings loaded into the page, for View
    Outputs' Send to WanGP - read by WanGP's own loader from the file a
    reference note in the handoff root names, never from anything the
    browser says, and answered where WanGP's own Load Settings box writes
    its answer: the refresh trigger for the page's own model, the model
    switch for another, and the main tabs put on the generator's form."""
    plugin, compatibility, _admission, _ui = _modules()
    K = compatibility
    ref = lambda n: f"{n:032x}"  # noqa: E731 - ids no other check uses
    with tempfile.TemporaryDirectory(prefix="minipaint-wangp-outputs-") as folder:
        outputs = pathlib.Path(folder)
        for name in ("same.mp4", "other.mkv", "blank.mov", "clip.webm", "picture.png"):
            (outputs / name).write_bytes(b"\x00" * 16)
        loader = _Loader({"same.mp4": {"model_type": "video", "prompt": "p"},
                          "other.mkv": {"model_type": "ltx2_22B_distilled", "prompt": "q"},
                          "picture.png": {"model_type": "video", "prompt": "r"}})
        bridge, wgp = _bridge({"video": VIDEO_MODEL}, root, names=SETTINGS_NAMES)
        wgp.load_settings_from_file = loader
        fresh = _page(K)
        hello, _ = _call(bridge, fresh, "hello")
        r.check("a build that hands over WanGP's loader and both of its triggers offers the settings load",
                hello.get("capabilities", {}).get("settings_load") is True, str(hello.get("capabilities")))
        r.check("the operation is one the trigger understands, and the loader one of the globals asked for",
                "settings" in plugin.OPERATIONS and "load_settings_from_file" in wgp.asked_globals, str(wgp.asked_globals))

        _note(root, ref(1), outputs / "same.mp4")
        ack, writes = _load(bridge, fresh, {"handoff_id": ref(1)})
        r.check("a file of the page's own model is read by WanGP's loader, from the path its note names",
                ack.get("ok") is True and ack.get("loaded") is True and loader.read[-1:] == [str(outputs / "same.mp4")],
                str({k: ack.get(k) for k in ("ok", "code", "loaded")}) + str(loader.read[-1:]))
        r.check("and fills the form again through refresh_form_trigger, switching nothing",
                writes.get(K.REFRESH_FORM_TRIGGER) == LOADED_AT and K.MODEL_CHOICE_TARGET not in writes
                and ack.get("switched") is False and ack.get("model_type") == "video", str(sorted(writes)) + str(ack.get("model_type")))
        r.check("with the generator's form put on screen in WanGP's main tabs",
                writes.get(K.MAIN_TABS) == {"selected": compatibility.MEDIA_TAB_ID} and compatibility.MEDIA_TAB_ID == "media_gen",
                repr(writes.get(K.MAIN_TABS)))
        r.check("the answer is correlated like every other",
                ack.get("op") == "settings" and ack.get("request_id") == "s1" and ack.get("channel_id") == "c" * 32, str(ack.get("op")))
        placed = bridge.outputs(ack, writes)
        tail = dict(zip(bridge.queue_keys, placed[1 + len(bridge.receiver_keys) + len(bridge.switch_keys):]))
        r.check("the event's outputs carry the trigger's value and the tab's selection, and leave every other output alone",
                tail.get(K.REFRESH_FORM_TRIGGER) == LOADED_AT
                and isinstance(tail.get(K.MAIN_TABS), dict) and tail[K.MAIN_TABS].get("selected") == "media_gen"
                and all(_unchanged(value) for key, value in tail.items() if key not in (K.REFRESH_FORM_TRIGGER, K.MAIN_TABS))
                and all(_unchanged(value) for value in placed[1:1 + len(bridge.receiver_keys) + len(bridge.switch_keys)]), repr(tail))

        other_page = _page(K)
        _note(root, ref(2), outputs / "other.mkv")
        ack, writes = _load(bridge, other_page, {"handoff_id": ref(2)})
        r.check("a file of another model switches the page to it through model_choice_target, as WanGP's box does",
                ack.get("loaded") is True and ack.get("switched") is True and ack.get("model_type") == "ltx2_22B_distilled"
                and writes.get(K.MODEL_CHOICE_TARGET) == "ltx2_22B_distilled|" + LOADED_AT and K.REFRESH_FORM_TRIGGER not in writes
                and writes.get(K.MAIN_TABS) == {"selected": "media_gen"}, str(ack) + str(sorted(writes)))
        r.check("and the switch's one-shot is WanGP's own loader's doing, in this page's state",
                other_page[K.SESSION_STATE].get("ignore_save_form") is True
                and "ltx2_22B_distilled" in other_page[K.SESSION_STATE].get("settings", {}))

        _note(root, ref(3), outputs / "picture.png")
        ack, writes = _load(bridge, _page(K), {"handoff_id": ref(3)})
        r.check("a picture WanGP made is loaded the same way", ack.get("loaded") is True and writes.get(K.REFRESH_FORM_TRIGGER) == LOADED_AT,
                str(ack))

        _note(root, ref(4), outputs / "blank.mov")
        reads = len(loader.read)
        ack, writes = _load(bridge, _page(K), {"handoff_id": ref(4)})
        r.check("a file WanGP reads nothing from answers ok and not loaded, and writes nothing - not even the tabs",
                ack.get("ok") is True and ack.get("loaded") is False and ack.get("switched") is False and writes == {}
                and "no settings" in ack.get("message", "") and len(loader.read) == reads + 1, str(ack) + str(writes))

        editing = _page(K, **{K.SESSION_STATE: {"model_type": "video", "gen": {"queue": []}, "active_form": "edit"}})
        ack, writes = _load(bridge, editing, {"handoff_id": ref(1)})
        r.check("on the Edit form - a queued task being edited - the tabs are left where they are",
                ack.get("loaded") is True and K.MAIN_TABS not in writes and K.REFRESH_FORM_TRIGGER in writes, str(sorted(writes)))

        ack, writes = _load(bridge, _page(K), {"handoff_id": ref(1), "path": str(outputs / "other.mkv"), "model_type": "t2v"})
        r.check("a path or a model beside the id is not read: the note's file is the one loaded",
                ack.get("loaded") is True and loader.read[-1] == str(outputs / "same.mp4") and ack.get("switched") is False, str(loader.read[-1:]))

        # Everything the browser or a stranger could put in its place.
        (outputs / "folder.mp4").mkdir()
        elsewhere = outputs / "elsewhere"
        elsewhere.mkdir()
        cases = [
            ("a path in place of the id", {"handoff_id": str(outputs / "same.mp4")}, None, "REQUEST_INVALID"),
            ("an id in upper case", {"handoff_id": ID_B.upper()}, None, "REQUEST_INVALID"),
            ("no id at all", {}, None, "REQUEST_INVALID"),
            ("an id no note was written for", {"handoff_id": ref(40)}, None, "HANDOFF_NOT_FOUND"),
            ("a picture's handoff, which is no note", {"handoff_id": ID_A}, None, "HANDOFF_NOT_FOUND"),
            ("a note naming a relative path", {"handoff_id": ref(5)}, json.dumps({"path": "same.mp4"}), "HANDOFF_INVALID_ID"),
            ("a note naming a WebM, which WanGP keeps no settings in", {"handoff_id": ref(6)}, outputs / "clip.webm", "HANDOFF_INVALID_ID"),
            ("a note naming a file that has gone", {"handoff_id": ref(7)}, outputs / "gone.mp4", "HANDOFF_NOT_FOUND"),
            ("a note naming a folder", {"handoff_id": ref(8)}, outputs / "folder.mp4", "HANDOFF_INVALID_ID"),
            ("a note that is not JSON", {"handoff_id": ref(9)}, "not json at all", "HANDOFF_INVALID_ID"),
            ("a note naming nothing", {"handoff_id": ref(10)}, json.dumps({"written": "now"}), "HANDOFF_INVALID_ID"),
            ("a note with a NUL in its path", {"handoff_id": ref(11)}, json.dumps({"path": str(outputs / "same.mp4") + "\x00.mp4"}), "HANDOFF_INVALID_ID"),
            # Good JSON, padded past the limit: the read alone, cut at the limit,
            # would still parse it, so only the size check refuses it.
            ("a note larger than any Forge writes", {"handoff_id": ref(12)},
             json.dumps({"path": str(outputs / "same.mp4")}) + " " * protocol.MAX_REFERENCE_BYTES, "HANDOFF_INVALID_ID"),
        ]
        for label, settings, body, expected in cases:
            if body is not None:
                _note(root, settings["handoff_id"], body)
            reads = len(loader.read)
            ack, writes = _load(bridge, _page(K), settings)
            r.check(f"{label} is {expected}, and WanGP's loader is never called",
                    ack.get("ok") is False and ack.get("code") == expected and not writes and len(loader.read) == reads,
                    str({k: ack.get(k) for k in ("ok", "code")}) + str(writes))

        if _can_symlink(outputs):
            os.symlink(str(outputs / "same.mp4"), str(outputs / "link.mp4"))
            _note(root, ref(13), outputs / "link.mp4")
            os.symlink(str(_note(elsewhere, ref(14), outputs / "same.mp4")), str(pathlib.Path(root) / (ref(14) + protocol.HANDOFF_REFERENCE_SUFFIX)))
            # A link to one of our own notes, inside the root: it resolves
            # where a note should, so only the note's own file type refuses it.
            os.symlink(str(pathlib.Path(root) / (ref(1) + protocol.HANDOFF_REFERENCE_SUFFIX)),
                       str(pathlib.Path(root) / (ref(16) + protocol.HANDOFF_REFERENCE_SUFFIX)))
            for label, handoff_id in (("a note naming a link, even to an output", ref(13)),
                                      ("a note that is a link out of the root", ref(14)),
                                      ("a note that is a link to another note in the root", ref(16))):
                reads = len(loader.read)
                ack, writes = _load(bridge, _page(K), {"handoff_id": handoff_id})
                r.check(f"{label} is HANDOFF_INVALID_ID, and WanGP's loader is never called",
                        ack.get("code") == "HANDOFF_INVALID_ID" and not writes and len(loader.read) == reads, str(ack.get("code")))

        loader.fail = True
        ack, writes = _load(bridge, _page(K), {"handoff_id": ref(1)})
        r.check("a loader that raises is INTERNAL_ERROR, writing nothing",
                ack.get("ok") is False and ack.get("code") == "INTERNAL_ERROR" and not writes, str(ack.get("code")))
        loader.fail = False

        stateless = _page(K, **{K.SESSION_STATE: None})
        ack, writes = _load(bridge, stateless, {"handoff_id": ref(1)})
        r.check("a page whose state was not handed over is refused, not loaded into a stranger's",
                ack.get("code") == "BRIDGE_COMPONENT_INCOMPATIBLE" and not writes, str(ack.get("code")))

        # Builds that cannot: no loader handed over, or no trigger to write its answer to.
        for label, names, hand_over in (("WanGP's loader", SETTINGS_NAMES, False),
                                        ("refresh_form_trigger", tuple(n for n in SETTINGS_NAMES if n != "refresh_form_trigger"), True),
                                        ("model_choice_target", tuple(n for n in SETTINGS_NAMES if n != "model_choice_target"), True)):
            bare, bare_wgp = _bridge({"video": VIDEO_MODEL}, root, names=names)
            if hand_over:
                bare_wgp.load_settings_from_file = loader
            hello, _ = _call(bare, fresh, "hello")
            reads = len(loader.read)
            ack, writes = _load(bare, _page(K), {"handoff_id": ref(1)})
            r.check(f"a build without {label} does not offer the load, and refuses one with BRIDGE_COMPONENT_INCOMPATIBLE",
                    hello.get("capabilities", {}).get("settings_load") is False and ack.get("code") == "BRIDGE_COMPONENT_INCOMPATIBLE"
                    and not writes and len(loader.read) == reads, str(hello.get("capabilities", {}).get("settings_load")) + str(ack.get("code")))
        tabless, tabless_wgp = _bridge({"video": VIDEO_MODEL}, root, names=tuple(n for n in SETTINGS_NAMES if n != "main_tabs"))
        tabless_wgp.load_settings_from_file = loader
        ack, writes = _load(tabless, _page(K), {"handoff_id": ref(1)})
        r.check("a build without the main tabs still loads, and writes no tab",
                ack.get("loaded") is True and K.MAIN_TABS not in writes and K.REFRESH_FORM_TRIGGER in writes, str(sorted(writes)))

        # WanGP's console: the model, never the file.
        line = plugin._summary("settings", {"loaded": True, "model_type": "ltx2_22B_distilled", "switched": True}, 0.012)
        nothing = plugin._summary("settings", {"loaded": False}, 0.012)
        r.check("the console line names the model and whether WanGP is switching, and nothing of the file",
                line.startswith("settings: loaded for ltx2_22B_distilled") and "switching" in line
                and nothing.startswith("settings: nothing") and ".mp4" not in line + nothing, line + " | " + nothing)
        printed = []
        answers = []
        original = plugin._note
        plugin._note = printed.append
        try:
            # A note naming a file inside a file: an OSError that is not "not
            # found", whose own text is the whole path.
            (outputs / "a very private name.mp4").write_bytes(b"\x00" * 16)
            _note(root, ref(15), outputs / "a very private name.mp4" / "inside.mp4")
            answers.append(_load(bridge, _page(K), {"handoff_id": ref(15)})[0])
            answers.append(_load(bridge, _page(K), {"handoff_id": ref(1)})[0])
            answers.append(_load(bridge, _page(K), {"handoff_id": ref(7)})[0])
        finally:
            plugin._note = original
        r.check("a file that cannot be looked at is HANDOFF_NOT_FOUND",
                answers[0].get("code") == "HANDOFF_NOT_FOUND", str(answers[0].get("code")))
        r.check("and neither a refusal nor a load puts the file's name or its folder in WanGP's console",
                len(printed) == 3 and not any("private" in line or folder in line or "same.mp4" in line or "gone.mp4" in line
                                              for line in printed), str(printed))
        r.check("nor in the answer the page gets: it holds the model, never the file",
                not any(folder in json.dumps(ack) or ".mp4" in json.dumps(ack) for ack in answers), str(answers)[:300])


def _can_symlink(base) -> bool:
    probe = pathlib.Path(base) / "probe-link"
    try:
        os.symlink(str(base), str(probe))
    except (OSError, NotImplementedError):
        return False
    probe.unlink()
    return True


def run() -> Results:
    r = Results("wangp queue")
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        r.check("Pillow is available for the queue checks (skipped)", True)
        return r
    with tempfile.TemporaryDirectory(prefix="minipaint-wangp-queue-") as root:
        _pictures(root)
        request_checks(r, root)
        confirm_checks(r, root)
        track_checks(r, root)
        restore_checks(r, root)
        prompt_edit_checks(r, root)
        settings_load_checks(r, root)
    return r


if __name__ == "__main__":
    sys.exit(0 if run().report() else 1)
