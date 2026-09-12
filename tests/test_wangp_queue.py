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
    ack, writes = _call(bridge, fresh, "queue", {"request_id": ID_B, "bridge_session": session, "prompt": "x" * 4001})
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
        restore_checks(r, root)
    return r


if __name__ == "__main__":
    sys.exit(0 if run().report() else 1)
