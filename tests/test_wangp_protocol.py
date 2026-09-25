"""The shared vocabulary, and the three copies of it that must not drift.

``protocol.py`` exists in two files and a third of it exists again in
JavaScript, because the WanGP-side plugin is installed into someone else's
Python environment and may import nothing of ours, and the browser half cannot
import Python at all. Duplication of that kind decays quietly: somebody fixes
the Forge-side normaliser, the plugin keeps the old one, and six months later
an image goes into a slot that was described by a rule nobody is running any
more. The SHARED marker exists so that decay is a test failure on the day it
happens, and the first half of this file is that test.

The rest holds the shared functions to the promises the other modules and the
browser make about them. ``valid_envelope`` is the whole of the message
filter that is not a browser fact, so every one of its refusals is asserted
rather than assumed. ``state_revision`` is what section 17 rests on: two
sides compute it independently and compare, so it has to be stable, sensitive
to a single moved value, and blind to the order a dictionary happened to be
built in. ``normalize_receivers`` is where "fail closed" is written down -
anything the bridge could not fully describe is dropped rather than guessed
at, because offering it would mean offering to send into a slot nobody proved
exists.

Nothing here needs WanGP, Forge, Gradio or a network. The bridge's copy is
loaded straight off disk as its own module, which is also how WanGP loads it.
"""

from harness import Results, ROOT, setup_path

setup_path()

import importlib.util  # noqa: E402
import json  # noqa: E402
import pathlib  # noqa: E402
import re  # noqa: E402

from minipaint_neo.wangp import protocol  # noqa: E402

FORGE_COPY = ROOT / "minipaint_neo" / "wangp" / "protocol.py"
BRIDGE_COPY = ROOT / "wan2gp_bridge" / "wan2gp-minipaint-bridge" / "protocol.py"
BROWSER_COPY = ROOT / "browser" / "minipaint_wangp.js"

#: The markers themselves are matched on bytes, so a file that grew a BOM or
#: changed its line endings is a difference rather than a surprise later.
OPEN_MARKER = b"SHARED --"
CLOSE_MARKER = b"END SHARED --"


# ------------------------------------------------------------- the copies --


def shared_block(path):
    """The lines strictly between the two markers, as bytes.

    Bytes rather than text, and lines rather than one blob, so that a trailing
    space or a stray carriage return counts as drift and can still be pointed
    at by line number.
    """
    lines = path.read_bytes().split(b"\n")
    opens = [i for i, line in enumerate(lines) if OPEN_MARKER in line and CLOSE_MARKER not in line]
    closes = [i for i, line in enumerate(lines) if CLOSE_MARKER in line]
    if not opens or not closes or closes[0] < opens[0]:
        return None
    return lines[opens[0] + 1 : closes[0]]


def drift(forge, bridge) -> str:
    """"" when the two blocks agree, else a sentence naming the side that moved.

    The message is the point of this check: "the protocol copies differ" sends
    somebody to diff two files by hand, and this integration has two of them
    precisely because nobody can import the other. So the failure says which
    file, which line, and what each of them says there.
    """
    forge_name = FORGE_COPY.relative_to(ROOT).as_posix()
    bridge_name = BRIDGE_COPY.relative_to(ROOT).as_posix()
    if forge is None:
        return f"{forge_name} has no SHARED / END SHARED markers left"
    if bridge is None:
        return f"{bridge_name} has no SHARED / END SHARED markers left"

    for index, (ours, theirs) in enumerate(zip(forge, bridge), start=1):
        if ours != theirs:
            return (
                f"the shared block drifted at line {index}: "
                f"{forge_name} has {ours!r}, {bridge_name} has {theirs!r}"
            )
    if len(forge) != len(bridge):
        longer, shorter = (bridge_name, forge_name) if len(bridge) > len(forge) else (forge_name, bridge_name)
        extra = (bridge if len(bridge) > len(forge) else forge)[min(len(forge), len(bridge))]
        return (
            f"the shared block drifted in length: {longer} has "
            f"{abs(len(forge) - len(bridge))} line(s) that {shorter} does not, "
            f"starting with {extra!r}"
        )
    return ""


def bridge_module():
    """The plugin's own copy, loaded the way WanGP loads it: off the path."""
    spec = importlib.util.spec_from_file_location("wangp_bridge_protocol_copy", BRIDGE_COPY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------ the browser --


def js_number(source: str, name: str):
    """A ``const NAME = 256 * 1024;`` as an integer, or None.

    The arithmetic is read rather than evaluated: the JavaScript says
    ``256 * 1024`` because that is what protocol.py says, and a test that ran
    the expression through ``eval`` would be one that also ran whatever else
    anybody put on that line.
    """
    match = re.search(rf"\bconst\s+{name}\s*=\s*([0-9][0-9*\s]*);", source)
    if not match:
        return None
    total = 1
    for part in match.group(1).split("*"):
        total *= int(part.strip())
    return total


def js_string(source: str, name: str) -> str:
    match = re.search(rf'\bconst\s+{name}\s*=\s*"([^"]*)";', source)
    return match.group(1) if match else ""


def js_strings(source: str, name: str) -> tuple:
    """The elements of a ``const NAME = [...]``, whether written out or named.

    The type whitelists are lists of the constants declared just above them,
    so an element is resolved as a name before it is given up on; the id and
    role lists are written out as literals. Both forms have to read the same
    here, because the point is what the browser ends up allowing.
    """
    match = re.search(rf"\bconst\s+{name}\s*=\s*\[(.*?)\];", source, re.S)
    if not match:
        return ()
    values = []
    for element in (part.strip() for part in match.group(1).split(",")):
        if not element:
            continue
        literal = re.match(r'\A"([^"]*)"\Z', element)
        values.append(literal.group(1) if literal else js_string(source, element))
    return tuple(values)


def js_regex(source: str, name: str) -> str:
    match = re.search(rf"\bconst\s+{name}\s*=\s*/(.*?)/;", source)
    return match.group(1) if match else ""


def js_labels(source: str, name: str) -> dict:
    match = re.search(rf"\bconst\s+{name}\s*=\s*\{{(.*?)\}};", source, re.S)
    if not match:
        return {}
    return dict(re.findall(r'(\w+)\s*:\s*"([^"]*)"', match.group(1)))


def as_javascript(pattern: str) -> str:
    """A Python pattern written the way the same rule reads in JavaScript."""
    return pattern.replace("\\A", "^").replace("\\Z", "$")


# ----------------------------------------------------------- the fixtures --


def descriptor(receiver_id, role, **extra) -> dict:
    """One raw receiver as the bridge would send it, before normalisation."""
    raw = {"id": receiver_id, "role": role, "operation": protocol.REPLACE}
    raw.update(extra)
    return raw


SAMPLE_STATE = {
    "model": "t2v",
    "mode": "reference",
    "controls": {"guidance": 5.0, "steps": 30},
    "references": ["one", "two"],
}


# --------------------------------------------------------------- the copies --


def copy_checks(r: Results) -> None:
    forge, bridge = shared_block(FORGE_COPY), shared_block(BRIDGE_COPY)
    difference = drift(forge, bridge)
    r.check("the two SHARED blocks are byte-identical", difference == "", difference)
    r.check("the shared block is not empty", forge and len([line for line in forge if line.strip()]) > 40,
            str(len(forge or [])))

    plugin = bridge_module()
    r.check("both copies speak the same protocol number", plugin.PROTOCOL == protocol.PROTOCOL)
    r.check("both copies name the same receivers", plugin.RECEIVER_IDS == protocol.RECEIVER_IDS)
    r.check("both copies name the same roles and operations",
            plugin.ROLES == protocol.ROLES and plugin.OPERATIONS == protocol.OPERATIONS)
    r.check("both copies agree on the two directions",
            plugin.TO_BRIDGE == protocol.TO_BRIDGE and plugin.TO_PARENT == protocol.TO_PARENT)
    r.check("both copies agree on the handoff shape",
            plugin.HANDOFF_ID_RE.pattern == protocol.HANDOFF_ID_RE.pattern
            and plugin.HANDOFF_SUFFIX == protocol.HANDOFF_SUFFIX)
    r.check("both copies agree on the ceilings",
            (plugin.MAX_HANDOFF_BYTES, plugin.MAX_HANDOFF_PIXELS, plugin.MAX_HANDOFF_SIDE, plugin.MAX_ENVELOPE_BYTES)
            == (protocol.MAX_HANDOFF_BYTES, protocol.MAX_HANDOFF_PIXELS, protocol.MAX_HANDOFF_SIDE, protocol.MAX_ENVELOPE_BYTES))

    # The two sides never compare source, only answers: the revision one
    # computes is the revision the other recomputes and refuses to match.
    r.check("both copies fingerprint one state the same way",
            plugin.state_revision(SAMPLE_STATE) == protocol.state_revision(SAMPLE_STATE))
    raw = [descriptor(protocol.REFERENCE, "reference", operation=protocol.APPEND, count=1, max_count=3)]
    r.check("both copies normalise one receiver list the same way",
            plugin.normalize_receivers(raw) == protocol.normalize_receivers(raw))
    request = {"request_id": "a" * 32, "prompt": " a cat\r\n\x07 ", "start_handoff_id": "b" * 32,
               "reference_handoff_ids": ["c" * 32, ""], "model": "dropped"}
    r.check("both copies normalise one queue request the same way",
            plugin.normalize_queue_request(request) == protocol.normalize_queue_request(request))
    r.check("and hash it the same way",
            plugin.queue_payload_hash(protocol.normalize_queue_request(request)[0])
            == protocol.queue_payload_hash(protocol.normalize_queue_request(request)[0]))
    r.check("both copies name the queue contract", plugin.QUEUE_CONTRACT == protocol.QUEUE_CONTRACT == "minipaint.wangp.queue/v1")


# ------------------------------------------------------------- the browser --


def browser_checks(r: Results) -> None:
    source = BROWSER_COPY.read_text(encoding="utf-8")

    r.check("the browser speaks the same protocol number", js_number(source, "PROTOCOL") == protocol.PROTOCOL,
            str(js_number(source, "PROTOCOL")))
    r.check("the browser drops the same oversized envelope",
            js_number(source, "MAX_ENVELOPE_BYTES") == protocol.MAX_ENVELOPE_BYTES,
            str(js_number(source, "MAX_ENVELOPE_BYTES")))
    r.check("the browser waits exactly as long for a receiver answer",
            js_number(source, "RECEIVER_QUERY_TIMEOUT_MS") == protocol.RECEIVER_QUERY_TIMEOUT_MS,
            str(js_number(source, "RECEIVER_QUERY_TIMEOUT_MS")))
    r.check("the browser waits exactly as long for an acknowledgement",
            js_number(source, "RECEIVE_TIMEOUT_MS") == protocol.RECEIVE_TIMEOUT_MS,
            str(js_number(source, "RECEIVE_TIMEOUT_MS")))
    r.check("the browser requires the same handoff id shape",
            js_regex(source, "HEX32") == as_javascript(protocol.HANDOFF_ID_RE.pattern),
            js_regex(source, "HEX32"))

    for name, value in (
        ("HELLO", protocol.HELLO),
        ("READY", protocol.READY),
        ("GET_RECEIVERS", protocol.GET_RECEIVERS),
        ("RECEIVERS", protocol.RECEIVERS),
        ("RECEIVE_IMAGE", protocol.RECEIVE_IMAGE),
        ("RECEIVE_RESULT", protocol.RECEIVE_RESULT),
        ("FOCUS_RECEIVER", protocol.FOCUS_RECEIVER),
        ("THEME_STATE", protocol.THEME_STATE),
        ("RUNTIME_STATE", protocol.RUNTIME_STATE),
        ("QUEUE_REQUEST", protocol.QUEUE_REQUEST),
        ("QUEUE_RESULT", protocol.QUEUE_RESULT),
        ("QUEUE_CONFIRM", protocol.QUEUE_CONFIRM),
        ("QUEUE_STATUS", protocol.QUEUE_STATUS),
    ):
        r.check(f"the browser names {name} the same way", js_string(source, name) == value, js_string(source, name))
    for name, value in (
        ("QUEUE_REQUEST_TIMEOUT_MS", protocol.QUEUE_REQUEST_TIMEOUT_MS),
        ("QUEUE_CONFIRM_TIMEOUT_MS", protocol.QUEUE_CONFIRM_TIMEOUT_MS),
        ("PROMPT_MAX_CHARS", protocol.PROMPT_MAX_CHARS),
        ("MAX_QUEUE_REFERENCES", protocol.MAX_QUEUE_REFERENCES),
    ):
        r.check(f"the browser agrees on {name}", js_number(source, name) == value, str(js_number(source, name)))
    delays = re.search(r"const QUEUE_CONFIRM_DELAYS_MS = \[([0-9, ]+)\];", source)
    r.check("the browser confirms on the same bounded schedule, starting at once",
            delays is not None and tuple(int(x) for x in delays.group(1).split(",")) == protocol.QUEUE_CONFIRM_DELAYS_MS,
            delays.group(1) if delays else "missing")
    r.check("the browser knows the four queue fields in order",
            js_strings(source, "QUEUE_FIELDS") == protocol.QUEUE_FIELDS, str(js_strings(source, "QUEUE_FIELDS")))
    r.check("and the admissions and statuses", js_strings(source, "ADMISSIONS") == protocol.ADMISSIONS
            and js_strings(source, "QUEUE_STATUSES") == protocol.QUEUE_STATUSES)

    r.check("the browser allows the same types out of the parent",
            set(js_strings(source, "TO_BRIDGE")) == set(protocol.TO_BRIDGE), str(js_strings(source, "TO_BRIDGE")))
    r.check("the browser allows the same types out of the iframe",
            set(js_strings(source, "TO_PARENT")) == set(protocol.TO_PARENT), str(js_strings(source, "TO_PARENT")))
    r.check("the browser knows the same receivers, in the same order",
            js_strings(source, "RECEIVER_IDS") == protocol.RECEIVER_IDS, str(js_strings(source, "RECEIVER_IDS")))
    r.check("the browser knows the same roles and operations",
            js_strings(source, "ROLES") == protocol.ROLES and js_strings(source, "OPERATIONS") == protocol.OPERATIONS)
    r.check("the browser shows the same default menu labels",
            js_labels(source, "DEFAULT_MENU_LABELS") == protocol.DEFAULT_MENU_LABELS,
            str(js_labels(source, "DEFAULT_MENU_LABELS")))


# ------------------------------------------------------------- envelopes ----


def envelope_checks(r: Results) -> None:
    good = protocol.envelope(protocol.GET_RECEIVERS, "c" * 32, "r1", {"bridge_session": "bs"})
    r.check("a good envelope is accepted", protocol.valid_envelope(good, protocol.TO_BRIDGE) is True)
    r.check("an envelope built with no payload still carries an object",
            protocol.envelope(protocol.HELLO, "c" * 32, "r1")["payload"] == {})

    wrong_protocol = dict(good, protocol=protocol.PROTOCOL - 1)
    r.check("a message from another protocol version is refused",
            protocol.valid_envelope(wrong_protocol, protocol.TO_BRIDGE) is False)
    r.check("a protocol number sent as text is refused",
            protocol.valid_envelope(dict(good, protocol=str(protocol.PROTOCOL)), protocol.TO_BRIDGE) is False)
    missing_protocol = {key: value for key, value in good.items() if key != "protocol"}
    r.check("a message with no protocol number is refused",
            protocol.valid_envelope(missing_protocol, protocol.TO_BRIDGE) is False)

    r.check("an unknown type is refused",
            protocol.valid_envelope(dict(good, type="WANGP_DO_AS_I_SAY"), protocol.TO_BRIDGE) is False)
    r.check("a type from the other direction is refused",
            protocol.valid_envelope(dict(good, type=protocol.READY), protocol.TO_BRIDGE) is False)
    r.check("and refused the other way round too",
            protocol.valid_envelope(dict(good, type=protocol.GET_RECEIVERS), protocol.TO_PARENT) is False)
    r.check("a legal answer is accepted in its own direction",
            protocol.valid_envelope(dict(good, type=protocol.RECEIVERS), protocol.TO_PARENT) is True)

    r.check("a blank channel id is refused", protocol.valid_envelope(dict(good, channel_id=""), protocol.TO_BRIDGE) is False)
    r.check("a missing channel id is refused",
            protocol.valid_envelope({k: v for k, v in good.items() if k != "channel_id"}, protocol.TO_BRIDGE) is False)
    r.check("a channel id that is not a string is refused",
            protocol.valid_envelope(dict(good, channel_id=17), protocol.TO_BRIDGE) is False)

    r.check("a blank request id is refused", protocol.valid_envelope(dict(good, request_id=""), protocol.TO_BRIDGE) is False)
    r.check("a missing request id is refused",
            protocol.valid_envelope({k: v for k, v in good.items() if k != "request_id"}, protocol.TO_BRIDGE) is False)
    r.check("a request id that is not a string is refused",
            protocol.valid_envelope(dict(good, request_id=None), protocol.TO_BRIDGE) is False)

    for payload in ([], "reference", 3, None):
        r.check(f"a payload that is not an object is refused ({type(payload).__name__})",
                protocol.valid_envelope(dict(good, payload=payload), protocol.TO_BRIDGE) is False)
    r.check("an absent payload is read as an empty object",
            protocol.valid_envelope({k: v for k, v in good.items() if k != "payload"}, protocol.TO_BRIDGE) is True)

    for message in (None, "WANGP_BRIDGE_HELLO", [good], 5):
        r.check(f"a message that is not an object is refused ({type(message).__name__})",
                protocol.valid_envelope(message, protocol.TO_BRIDGE) is False)


# -------------------------------------------------------------- revisions ---


def revision_checks(r: Results) -> None:
    revision = protocol.state_revision(SAMPLE_STATE)
    r.check("the revision is the declared length", len(revision) == protocol.REVISION_LENGTH, str(len(revision)))
    r.check("the revision is lowercase hex", re.match(r"\A[0-9a-f]+\Z", revision) is not None, revision)
    r.check("the same state fingerprints the same way twice", protocol.state_revision(SAMPLE_STATE) == revision)

    moved = dict(SAMPLE_STATE, controls={"guidance": 5.0, "steps": 31})
    r.check("one changed value changes the fingerprint", protocol.state_revision(moved) != revision)
    turned_off = dict(SAMPLE_STATE, mode="start")
    r.check("a changed mode changes the fingerprint", protocol.state_revision(turned_off) != revision)
    emptied = dict(SAMPLE_STATE, references=[])
    r.check("an emptied list changes the fingerprint", protocol.state_revision(emptied) != revision)

    reordered = {
        "references": ["one", "two"],
        "controls": {"steps": 30, "guidance": 5.0},
        "mode": "reference",
        "model": "t2v",
    }
    r.check("the order dictionary keys were built in does not matter",
            protocol.state_revision(reordered) == revision)

    # A list is not a set: two references in the other order is a different
    # state, and section 22 cares which one is first.
    swapped = dict(SAMPLE_STATE, references=["two", "one"])
    r.check("the order of a list does matter", protocol.state_revision(swapped) != revision)

    r.check("an unfingerprintable value still fingerprints",
            len(protocol.state_revision({"widget": object()})) == protocol.REVISION_LENGTH)
    r.check("nothing at all is still a fingerprint", len(protocol.state_revision(None)) == protocol.REVISION_LENGTH)
    r.check("the canonical form is compact and sorted",
            protocol.canonical_json({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}',
            protocol.canonical_json({"b": 1, "a": [1, 2]}))


# -------------------------------------------------------------- receivers ---


def receiver_checks(r: Results) -> None:
    r.check("an unknown receiver id is dropped, not guessed at",
            protocol.normalize_receivers([descriptor("prompt_box", "reference")]) == [])
    r.check("an unknown operation is dropped",
            protocol.normalize_receivers([descriptor(protocol.REFERENCE, "reference", operation="merge")]) == [])
    r.check("a missing operation is dropped",
            protocol.normalize_receivers([{"id": protocol.REFERENCE, "role": "reference"}]) == [])
    r.check("an unknown role is dropped",
            protocol.normalize_receivers([descriptor(protocol.REFERENCE, "everything")]) == [])
    r.check("a receiver that is not an object is dropped", protocol.normalize_receivers(["reference", None, 7]) == [])
    r.check("one bad receiver does not take the good ones with it",
            [item["id"] for item in protocol.normalize_receivers(
                [descriptor("nonsense", "reference"), descriptor(protocol.START_FRAME, "start")])]
            == [protocol.START_FRAME])

    full = protocol.normalize_receivers(
        [descriptor(protocol.REFERENCE, "reference", operation=protocol.APPEND, count=4, max_count=4)]
    )
    r.check("a full receiver is reported full", full and full[0]["full"] is True)
    r.check("a full receiver comes back disabled", full and full[0]["enabled"] is False)
    room = protocol.normalize_receivers(
        [descriptor(protocol.REFERENCE, "reference", operation=protocol.APPEND, count=3, max_count=4)]
    )
    r.check("a receiver with room left is enabled", room and room[0]["enabled"] is True and room[0]["full"] is False)
    r.check("a receiver with no ceiling is never full",
            protocol.normalize_receivers([descriptor(protocol.REFERENCE, "reference", operation=protocol.APPEND, count=99)])[0]["full"]
            is False)
    r.check("a receiver the bridge says is off stays off",
            protocol.normalize_receivers([descriptor(protocol.START_FRAME, "start", enabled=False)])[0]["enabled"] is False)
    r.check("a receiver the bridge says is hidden stays off",
            protocol.normalize_receivers([descriptor(protocol.START_FRAME, "start", visible=False)])[0]["enabled"] is False)

    shuffled = [
        descriptor(protocol.STYLE_REF, "style"),
        descriptor(protocol.START_FRAME, "start"),
        descriptor(protocol.REFERENCE, "reference", operation=protocol.APPEND),
        descriptor(protocol.END_FRAME, "end"),
    ]
    r.check("the order follows RECEIVER_IDS, not the bridge's order",
            [item["id"] for item in protocol.normalize_receivers(shuffled)]
            == [protocol.START_FRAME, protocol.END_FRAME, protocol.REFERENCE, protocol.STYLE_REF],
            str([item["id"] for item in protocol.normalize_receivers(shuffled)]))

    duplicated = protocol.normalize_receivers(
        [descriptor(protocol.REFERENCE, "reference", operation=protocol.APPEND, count=1),
         descriptor(protocol.REFERENCE, "reference", operation=protocol.REPLACE, count=9)]
    )
    r.check("a duplicate id collapses to one entry", len(duplicated) == 1, str(len(duplicated)))
    r.check("and the first description of it is the one kept",
            duplicated and duplicated[0]["operation"] == protocol.APPEND and duplicated[0]["count"] == 1)

    for wrong in (None, {}, "reference", 3, (descriptor(protocol.REFERENCE, "reference"),)):
        r.check(f"a receiver list that is not a list is empty ({type(wrong).__name__})",
                protocol.normalize_receivers(wrong) == [])

    labelled = protocol.normalize_receivers([descriptor(protocol.CONTROL_IMAGE, "control")])[0]
    r.check("a receiver with no menu label gets the declared one",
            labelled["menu_label"] == protocol.DEFAULT_MENU_LABELS[protocol.CONTROL_IMAGE], labelled["menu_label"])
    r.check("the bridge's own label wins when it sends one",
            protocol.normalize_receivers([descriptor(protocol.CONTROL_IMAGE, "control", menu_label="Send to Pose")])[0]["menu_label"]
            == "Send to Pose")
    r.check("a count that is not a number reads as none at all",
            protocol.normalize_receivers([descriptor(protocol.REFERENCE, "reference", operation=protocol.APPEND, count="two")])[0]["count"] == 0)
    r.check("a boolean is not a count",
            protocol.normalize_receivers([descriptor(protocol.REFERENCE, "reference", operation=protocol.APPEND, count=True)])[0]["count"] == 0)
    r.check("a focus hint that is not a string is dropped",
            labelled["focus_hint"] == "" and protocol.normalize_receivers(
                [descriptor(protocol.CONTROL_IMAGE, "control", focus_hint={"selector": "#x"})])[0]["focus_hint"] == "")


# ---------------------------------------------------------------- handoffs --


def offered_checks(r: Results) -> None:
    """The descriptor says *why* an input is offered: switched on, or allowed.

    ``enabled`` is what the menu acts on and it now covers both. ``selected``
    says whether WanGP's own selector already has the input switched on and
    ``switch`` names what a send will set when it does not - so a Forge side
    can tell the two apart, and so a bridge that predates the distinction (it
    only ever offered what was switched on) reads as "selected".
    """
    base = {"role": "start", "operation": protocol.REPLACE, "enabled": True}
    allowed = protocol.normalize_receiver({"id": protocol.START_FRAME, "selected": False, "switch": "location", **base})
    r.check("an allowed, unselected receiver is enabled", allowed["enabled"] is True)
    r.check("and not selected", allowed["selected"] is False)
    r.check("and names the switch a send will make", allowed["switch"] == "location", repr(allowed["switch"]))

    selected = protocol.normalize_receiver({"id": protocol.START_FRAME, "selected": True, "switch": "location", **base})
    r.check("a selected receiver carries no switch, whatever the bridge sent", selected["switch"] == "")

    legacy = protocol.normalize_receiver({"id": protocol.START_FRAME, **base})
    r.check("a bridge that says nothing about selection is read as selected", legacy["selected"] is True)
    r.check("and switches nothing", legacy["switch"] == "")

    off = protocol.normalize_receiver({"id": protocol.START_FRAME, "role": "start", "operation": protocol.REPLACE,
                                       "enabled": False, "selected": False, "switch": "location"})
    r.check("a disabled receiver carries no switch either", off["switch"] == "" and off["enabled"] is False)

    for wrong in ("Location", "loc ation", "x" * 41, 7, None, ["location"]):
        odd = protocol.normalize_receiver({"id": protocol.START_FRAME, "selected": False, "switch": wrong, **base})
        r.check(f"a switch that is not a token is dropped ({wrong!r})", odd["switch"] == "")
    r.check("a selection flag that is not a boolean falls back to enabled",
            protocol.normalize_receiver({"id": protocol.START_FRAME, "selected": "yes", **base})["selected"] is True)
    r.check("the query waits longer than one busy Gradio round trip",
            protocol.RECEIVER_QUERY_TIMEOUT_MS >= 10000, str(protocol.RECEIVER_QUERY_TIMEOUT_MS))


def handoff_checks(r: Results) -> None:
    r.check("32 lowercase hex characters is a handoff id",
            protocol.valid_handoff_id("0123456789abcdef0123456789abcdef") is True)
    for wrong in (
        "0123456789ABCDEF0123456789ABCDEF",
        "0123456789abcdef0123456789abcde",
        "0123456789abcdef0123456789abcdef0",
        "0123456789abcdef0123456789abcde.",
        "../0123456789abcdef0123456789abc",
        "0123456789abcdef0123456789abcdef.png",
        "0123456789abcdef/0123456789abcdef",
        "0123456789abcdef\\0123456789abcdef",
        "0123456789abcdef0123456789abcdef\n",
        "",
        "  0123456789abcdef0123456789abcdef  ",
    ):
        r.check(f"anything else is refused outright ({wrong!r})", protocol.valid_handoff_id(wrong) is False)
    for wrong in (None, 12345, b"0123456789abcdef0123456789abcdef", ["0123456789abcdef0123456789abcdef"]):
        r.check(f"a handoff id that is not a string is refused ({type(wrong).__name__})",
                protocol.valid_handoff_id(wrong) is False)
    r.check("the handoff file is a PNG and says so", protocol.HANDOFF_SUFFIX == ".png")
    r.check("the ceilings are finite",
            protocol.MAX_HANDOFF_BYTES > 0 and protocol.MAX_HANDOFF_PIXELS > 0 and protocol.MAX_HANDOFF_SIDE > 0)


def fullness_checks(r: Results) -> None:
    """A replace receiver is never full, however much it already holds.

    The regression this guards: a start frame reported ``1 of 1`` the moment
    it held a picture, the normaliser read that as full, and the menu stopped
    offering the one receiver a user most wants to send a second image to -
    which is the whole of "Start send replaces start" in section 45.
    """
    for receiver_id, role in (("start_frame", "start"), ("end_frame", "end"), ("control_image", "control")):
        held = protocol.normalize_receiver(
            {"id": receiver_id, "role": role, "operation": protocol.REPLACE, "count": 1, "max_count": 1}
        )
        r.check(f"a {receiver_id} holding an image is not full", held["full"] is False)
        r.check(f"a {receiver_id} holding an image can still be sent to", held["enabled"] is True)
        r.check(f"a {receiver_id} still reports what it holds", held["count"] == 1)

    # The append side keeps its limit, because there it means something.
    full = protocol.normalize_receiver(
        {"id": "reference", "role": "reference", "operation": protocol.APPEND, "count": 9, "max_count": 9}
    )
    r.check("a reference list at its maximum is full", full["full"] is True)
    r.check("a full reference list is not offered", full["enabled"] is False)
    room = protocol.normalize_receiver(
        {"id": "reference", "role": "reference", "operation": protocol.APPEND, "count": 2, "max_count": 9}
    )
    r.check("a reference list with room is offered", room["enabled"] is True and room["full"] is False)
    over = protocol.normalize_receiver(
        {"id": "reference", "role": "reference", "operation": protocol.APPEND, "count": 12, "max_count": 9}
    )
    r.check("a reference list past its maximum is still full", over["full"] is True)

    # An explicitly disabled receiver stays disabled either way: fullness is
    # one reason to withhold an action, never the only one.
    off = protocol.normalize_receiver(
        {"id": "start_frame", "role": "start", "operation": protocol.REPLACE, "enabled": False, "count": 1, "max_count": 1}
    )
    r.check("a switched-off start frame stays switched off", off["enabled"] is False)


def session_isolation_checks(r: Results) -> None:
    """Two browser tabs on two models must not read each other's schema.

    The receiver-affecting values already come from the bridge event's own
    inputs, which are session-scoped by construction. The model did not: it was
    read through ``request_global``, which is one value for the whole process,
    so whichever model the server saw last decided what every page was offered.
    Section 13.2 says receiver state is per browser session, and section 44
    tests it by changing the model in tab A and expecting tab B not to move.
    """
    import sys as _sys

    folder = str(BRIDGE_COPY.parent)
    added = folder not in _sys.path
    if added:
        _sys.path.insert(0, folder)
    try:
        import compatibility
    except ImportError as error:  # pragma: no cover - a broken bridge copy
        r.check("the bridge compatibility module imports", False, str(error))
        return
    finally:
        if added and folder in _sys.path:
            _sys.path.remove(folder)

    class OneProcess:
        """The plugin globals: one model for the whole server, as they are."""

        def read_global(self, name):
            return {
                "model_type": "t2v_A",
                "model_def": {"media_inputs": {"image": {"reference": True, "start": True}}},
            }.get(name)

    compat = compatibility.Compatibility.__new__(compatibility.Compatibility)
    compat.host = OneProcess()

    r.check("the model selector is a component the bridge reads",
            compatibility.MODEL_SELECTOR in compatibility.COMPONENTS_BY_KEY)
    r.check("and it is not mandatory, so a build without it still works",
            compatibility.COMPONENTS_BY_KEY[compatibility.MODEL_SELECTOR].mandatory is False)

    # Tab A is on the model the process globals describe.
    r.check("the page on the current model agrees with the globals", compat.selection_is_current("t2v_A"))
    r.check("and reads the schema", compat.model_capabilities(selected="t2v_A")["reference"] is True)
    r.check("and is named by it", compat.model_descriptor("t2v_A")["type"] == "t2v_A")

    # Tab B is on another one. It must not inherit tab A's answers.
    r.check("a page on another model does not claim to agree", not compat.selection_is_current("i2v_B"))
    borrowed = compat.model_capabilities(selected="i2v_B")
    r.check("and borrows no capability from it", set(borrowed.values()) == {None}, repr(borrowed))
    r.check("and reports its own model, not the other page's",
            compat.model_descriptor("i2v_B")["type"] == "i2v_B")

    # A build where the dropdown never resolved behaves exactly as before.
    r.check("an unresolved selector falls back to the globals",
            compat.selection_is_current("") and compat.selection_is_current(None))
    r.check("and still reads the schema", compat.model_capabilities(selected=None)["start_frame"] is True)

    # Whatever shape the dropdown hands back.
    for label, shape in (("a bare string", "t2v_A"), ("a pair", ("Label", "t2v_A")), ("a list of one", ["t2v_A"])):
        r.check(f"{label} is recognised as the current model", compat.selection_is_current(shape), repr(shape))


def loader_checks(r: Results) -> None:
    """WanGP's own loader must be able to find our plugin class.

    It imports ``<folder>.plugin`` and takes any class where
    ``issubclass(obj, WAN2GPPlugin)`` holds. A plugin whose class is built on
    anything else is skipped with no message at all - it still appears in the
    Plugins tab, still sits in ``enabled_plugins``, and simply never runs.
    That is exactly what happened: the base class was searched for under five
    module names, none of which was the real one.

    So this builds a WanGP-shaped tree with the base class where WanGP really
    keeps it, and runs the loader's own two lines against the real folder.
    """
    import importlib
    import inspect
    import pathlib as _pathlib
    import shutil
    import sys
    import tempfile

    source = BRIDGE_COPY.parent
    r.check("the real module path is the first place we look",
            _first_candidate() == ("shared.utils.plugins", "WAN2GPPlugin"), repr(_first_candidate()))

    with tempfile.TemporaryDirectory(prefix="minipaint-wangp-loader-") as scratch:
        root = _pathlib.Path(scratch)
        (root / "shared" / "utils").mkdir(parents=True)
        (root / "shared" / "__init__.py").write_text("", encoding="utf-8")
        (root / "shared" / "utils" / "__init__.py").write_text("", encoding="utf-8")
        (root / "shared" / "utils" / "plugins.py").write_text(
            "class WAN2GPPlugin:\n    name = 'unnamed'\n", encoding="utf-8"
        )
        plugins = root / "plugins"
        plugins.mkdir()
        shutil.copytree(str(source), str(plugins / source.name))

        added = [str(root), str(plugins)]
        for entry in added:
            sys.path.insert(0, entry)
        buried = {name: module for name, module in sys.modules.items()
                  if name.split(".")[0] in ("shared", source.name)}
        for name in buried:
            sys.modules.pop(name, None)
        try:
            base = importlib.import_module("shared.utils.plugins").WAN2GPPlugin
            module = importlib.import_module(f"{source.name}.plugin")
            accepted = [obj for _name, obj in inspect.getmembers(module, inspect.isclass)
                        if issubclass(obj, base) and obj is not base]
            r.check("WanGP's loader would accept our plugin class", bool(accepted),
                    "no subclass of WAN2GPPlugin - WanGP skips this silently")
            r.check("and it is the bridge plugin",
                    any(obj.__name__ == "MiniPaintBridgePlugin" for obj in accepted),
                    str([obj.__name__ for obj in accepted]))
        finally:
            for name in list(sys.modules):
                if name.split(".")[0] in ("shared", source.name):
                    sys.modules.pop(name, None)
            sys.modules.update(buried)
            for entry in added:
                if entry in sys.path:
                    sys.path.remove(entry)


class _Wgp:
    """What a plugin sees of Wan2GP: the hooks, and the globals it injects as
    attributes - here ``get_model_def`` and ``get_state_model_type``, the two
    the bridge reads a page's model through."""

    def __init__(self, definitions):
        self.definitions = definitions
        self.asked_globals = []
        self.asked_components = []

    def request_component(self, name):
        self.asked_components.append(name)

    def request_global(self, name):
        self.asked_globals.append(name)

    def get_model_def(self, model_type):
        return self.definitions.get(model_type)

    @staticmethod
    def get_state_model_type(state):
        return state["model_type"]


class _Handed:
    """A Gradio-shaped component, as ``post_ui_setup`` hands them over."""

    def __init__(self, name):
        self._id = name
        self.name = name

    def get_config(self):
        return {}


class Gallery(_Handed):
    """Shaped and named like Gradio's Gallery, which is what Wan2GP's start
    and end frames are."""


_VIDEO_MODEL = {
    "name": "A video model",
    "image_prompt_types_allowed": "TSEV",
    "image_ref_choices": {"choices": [("None", ""), ("People / Objects", "I"), ("Landscape then people", "KI")],
                          "letters_filter": "KFI"},
}
_TEXT_ONLY_MODEL = {"name": "Text only", "image_prompt_types_allowed": "T"}
_IMAGE_MODEL = {"name": "An image model", "image_prompt_types_allowed": "TSV",
                "image_ref_choices": {"choices": [("None", ""), ("Reference", "I")], "letters_filter": "I"}}

_FORM_NAMES = ("image_start", "image_end", "image_refs", "image_prompt_type", "video_prompt_type", "model_choice",
               "image_mode", "state", "image_prompt_type_radio", "image_prompt_type_endcheckbox",
               "video_prompt_type_image_refs", "image_start_row", "image_end_row", "image_refs_row")


def _bridge_on(bridge_plugin, definitions, names=_FORM_NAMES, root="/tmp", galleries=()):
    """A resolved bridge inside a Wan2GP that hands over ``names`` and knows ``definitions``."""
    wgp = _Wgp(definitions)
    bridge = bridge_plugin.MiniPaintBridge(
        host=bridge_plugin.compatibility.Host(wgp),
        environ={"MINIPAINT_WANGP_INSTANCE_ID": "i", "MINIPAINT_WANGP_HANDOFF_ROOT": str(root)},
    )
    bridge.compat.declare_globals()
    handed = {name: (Gallery(name) if name in galleries else _Handed(name)) for name in names}
    bridge.compat.host.accept_components(handed)
    bridge.resolve()
    return bridge, wgp


def _page(compatibility, **overrides):
    """A fresh page on a video model, keyed by the bridge's component keys -
    the shape ``live_values`` produces from the event's positional inputs -
    with ``overrides`` applied by key."""
    page = {
        compatibility.IMAGE_PROMPT_TYPE: "T",
        compatibility.VIDEO_PROMPT_TYPE: "",
        compatibility.IMAGE_MODE: 0,
        compatibility.SESSION_STATE: {"model_type": "video"},
        compatibility.MODEL_SELECTOR: "video",
        compatibility.START_IMAGE: None,
        compatibility.END_IMAGE: None,
        compatibility.REFERENCE_GALLERY: [],
    }
    page.update(overrides)
    return page


def _on_model(compatibility, page, model_type, **overrides):
    """The same page after switching to ``model_type``."""
    changed = dict(page)
    changed[compatibility.SESSION_STATE] = {"model_type": model_type}
    changed[compatibility.MODEL_SELECTOR] = model_type
    changed.update(overrides)
    return changed


def _answer(bridge, live, op="receivers", **extra):
    """One bridge event, with this page's values, as Gradio would call it."""
    request = {"op": op, "request_id": "r1", "channel_id": "c" * 32, **extra}
    values = [live.get(key) for key in bridge.state_keys]
    return bridge.handle(json.dumps(request), values, "session-hash-of-this-page")


def allowance_checks(r: Results) -> None:
    """What the model allows is offered, and a send switches it on.

    Section 25 had two layers: what the model could take and what the page has
    switched on, and an input was offered only when both said yes. That made
    the Send menu empty on a fresh WanGP page - the Location radio starts on
    Text Prompt - until the user went to the WanGP tab and changed it. Now
    the first layer is read the way Wan2GP reads it, from the page's own
    model definition, and a send to an allowed input sets the selector
    itself, in the same event that places the image. Unknown is never
    allowed: a build that hands over less offers exactly what it did.
    """
    import sys as _sys

    folder = str(BRIDGE_COPY.parent)
    added = folder not in _sys.path
    if added:
        _sys.path.insert(0, folder)
    try:
        import plugin as bridge_plugin
        compatibility = bridge_plugin.compatibility
    finally:
        if added and folder in _sys.path:
            _sys.path.remove(folder)

    fresh = _page(compatibility)
    bridge, wgp = _bridge_on(bridge_plugin, {"video": _VIDEO_MODEL, "text": _TEXT_ONLY_MODEL, "image": _IMAGE_MODEL})

    r.check("the globals Wan2GP injects at construction are asked for before setup_ui",
            {"get_model_def", "get_state_model_type"} <= set(wgp.asked_globals), str(wgp.asked_globals))
    r.check("and a requested global is read off the plugin object, which is where Wan2GP puts it",
            callable(bridge.compat.host.read_global("get_model_def")))
    r.check("while an attribute that was never asked for is not mistaken for a global",
            bridge.compat.host.read_global("definitions") is None)

    # -- a fresh page on a video model: nothing selected, everything allowed
    ack, applied = _answer(bridge, fresh)
    r.check("a fresh page answers ready", ack.get("ok") is True and ack.get("ready") is True, str(ack.get("code")))
    by_id = {item["id"]: item for item in ack["receivers"]}
    r.check("start, end and reference are all offered on a video model",
            all(by_id[key]["enabled"] for key in ("start_frame", "end_frame", "reference")), repr(by_id))
    r.check("and none of them is selected yet",
            not any(by_id[key]["selected"] for key in ("start_frame", "end_frame", "reference")))
    r.check("each names what a send would switch",
            (by_id["start_frame"]["switch"], by_id["end_frame"]["switch"], by_id["reference"]["switch"])
            == ("location", "end_images", "reference_images"), repr([v["switch"] for v in by_id.values()]))
    r.check("so the answer does not claim the model takes no image", ack.get("reason_code") != "NO_ACTIVE_RECEIVER")

    # -- the switch a send would make, computed from Wan2GP's own letter rules
    start = bridge.compat.switch_for("start_frame", fresh)
    r.check("a start frame send sets the Location radio to S",
            start.updates.get(compatibility.IMAGE_PROMPT_RADIO) == "S", repr(dict(start.updates)))
    r.check("and rewrites the letter string generation reads, dropping T the way the radio handler does",
            start.updates.get(compatibility.IMAGE_PROMPT_TYPE) == "S")
    r.check("and shows the start row", start.updates.get(compatibility.START_ROW) == {"visible": True})
    r.check("and says so", start.token == "location")

    end = bridge.compat.switch_for("end_frame", fresh)
    r.check("an end frame send ticks End Image(s)", end.updates.get(compatibility.END_IMAGES_CHECKBOX) is True)
    r.check("and, with no start or continue chosen, chooses Start with Image too",
            end.updates.get(compatibility.IMAGE_PROMPT_RADIO) == "S")
    letters = end.updates.get(compatibility.IMAGE_PROMPT_TYPE, "")
    r.check("so the letters carry both S and E", "S" in letters and "E" in letters, repr(letters))

    reference = bridge.compat.switch_for("reference", fresh)
    r.check("a reference send picks the first dropdown choice that injects references",
            reference.updates.get(compatibility.REFERENCE_SELECTOR) == "I", repr(dict(reference.updates)))
    r.check("and writes I into the video letters, since the dropdown's .input handler will not run",
            reference.updates.get(compatibility.VIDEO_PROMPT_TYPE) == "I")
    r.check("and shows the reference row", reference.updates.get(compatibility.REFERENCE_ROW) == {"visible": True})

    # -- already switched on: offered, selected, nothing to switch
    chosen = dict(fresh, image_prompt_type="SE", video_prompt_type="KI")
    ack, _ = _answer(bridge, chosen)
    by_id = {item["id"]: item for item in ack["receivers"]}
    r.check("a selected receiver reads as selected", by_id["start_frame"]["selected"] is True)
    r.check("and carries no switch", by_id["start_frame"]["switch"] == "" and by_id["reference"]["switch"] == "")
    r.check("a switch for a selected receiver is empty", not bridge.compat.switch_for("start_frame", chosen))
    r.check("and so is one for references already on", not bridge.compat.switch_for("reference", chosen))

    # -- a text-only model: nothing is offered, and it says so
    text = _on_model(compatibility, fresh, "text")
    ack, _ = _answer(bridge, text)
    r.check("a model that takes no image offers nothing",
            not any(item["enabled"] for item in ack["receivers"]), repr(ack["receivers"]))
    r.check("and the answer says that is why", ack.get("reason_code") == "NO_ACTIVE_RECEIVER")

    # -- image output mode strips the start/end choice, as Wan2GP does
    image = _on_model(compatibility, fresh, "image", **{compatibility.IMAGE_MODE: 1})
    ack, _ = _answer(bridge, image)
    by_id = {item["id"]: item for item in ack["receivers"]}
    r.check("in image-output mode a start frame is not offered", by_id["start_frame"]["enabled"] is False)
    r.check("but a reference still is", by_id["reference"]["enabled"] is True)

    # -- unknown is not allowed: fewer components, same old behaviour
    fewer = tuple(name for name in _FORM_NAMES if name not in ("image_prompt_type_radio", "image_prompt_type_endcheckbox"))
    lesser, _ = _bridge_on(bridge_plugin, {"video": _VIDEO_MODEL}, names=fewer)
    ack, _ = _answer(lesser, fresh)
    by_id = {item["id"]: item for item in ack["receivers"]}
    r.check("without the Location radio a start frame is offered only when selected",
            by_id["start_frame"]["enabled"] is False and by_id["end_frame"]["enabled"] is False)
    r.check("while references, whose dropdown resolved, still are", by_id["reference"]["enabled"] is True)
    ack, _ = _answer(lesser, chosen)
    r.check("and a selected start frame is offered as it always was",
            {item["id"]: item for item in ack["receivers"]}["start_frame"]["enabled"] is True)

    blind, _ = _bridge_on(bridge_plugin, {})
    ack, _ = _answer(blind, fresh)
    r.check("a build whose model definition cannot be read offers nothing beyond the selection",
            not any(item["enabled"] for item in ack["receivers"]))
    ack, _ = _answer(blind, chosen)
    r.check("and everything the selection switched on",
            all(item["enabled"] for item in ack["receivers"]))


def switch_apply_checks(r: Results) -> None:
    """A send to an allowed input places the image and flips the selector in
    one event, and its acknowledgement says which; one to an input that is
    neither selected nor allowed is refused without choosing another."""
    import sys as _sys
    import tempfile

    try:
        from PIL import Image
    except ImportError:
        r.check("Pillow is available for the switch apply checks (skipped)", True)
        return

    folder = str(BRIDGE_COPY.parent)
    added = folder not in _sys.path
    if added:
        _sys.path.insert(0, folder)
    try:
        import plugin as bridge_plugin
        compatibility = bridge_plugin.compatibility
    finally:
        if added and folder in _sys.path:
            _sys.path.remove(folder)

    fresh = _page(compatibility)

    with tempfile.TemporaryDirectory(prefix="minipaint-wangp-switch-") as root:
        handoff_id = "0123456789abcdef0123456789abcdef"
        picture = Image.new("RGBA", (4, 4), (10, 200, 30, 255))
        picture.save(pathlib.Path(root) / f"{handoff_id}.png", format="PNG")

        bridge, _ = _bridge_on(bridge_plugin, {"video": _VIDEO_MODEL, "text": _TEXT_ONLY_MODEL},
                               root=root, galleries=("image_start", "image_end"))
        answer, _ = _answer(bridge, fresh)
        revision = answer["state_revision"]

        ack, applied = _answer(bridge, fresh, op="receive", handoff_id=handoff_id, receiver_id="start_frame",
                               state_revision=revision, source={})
        r.check("the send to an unselected start frame is accepted", ack.get("ok") is True, str(ack.get("code")))
        r.check("and verified against the pixels that were sent", ack.get("verification") == "pixel-equivalent")
        r.check("the acknowledgement says the Location was switched", ack.get("switched") == "location")
        r.check("and lists the components it updated",
                set(ack.get("chained", ())) >= {compatibility.IMAGE_PROMPT_RADIO, compatibility.IMAGE_PROMPT_TYPE})
        r.check("the fingerprint after the send differs from the one before",
                ack.get("state_revision_after") and ack["state_revision_after"] != revision)
        r.check("a gallery-shaped start frame is written as a one-element list",
                isinstance(applied.value, list) and len(applied.value) == 1)

        outputs = bridge.outputs(ack, applied)
        r.check("the event returns the acknowledgement, every receiver, then every switch component",
                len(outputs) == 1 + len(bridge.receiver_keys) + len(bridge.switch_keys), str(len(outputs)))
        offset = 1 + len(bridge.receiver_keys)
        placed = dict(zip(bridge.switch_keys, outputs[offset:]))
        r.check("the radio output is S", placed.get(compatibility.IMAGE_PROMPT_RADIO) == "S", repr(placed))
        r.check("the letter string output carries S", placed.get(compatibility.IMAGE_PROMPT_TYPE) == "S")
        row = placed.get(compatibility.START_ROW)
        r.check("the start row is shown", isinstance(row, dict) and row.get("visible") is True, repr(row))
        untouched = placed.get(compatibility.REFERENCE_SELECTOR)
        r.check("a component the switch did not name is left alone",
                untouched is None or (isinstance(untouched, dict) and "value" not in untouched), repr(untouched))
        r.check("the start frame itself is written at its own position",
                outputs[1 + bridge.receiver_keys.index("start_frame")] is applied.value)

        # Already selected: the image lands and nothing is switched.
        chosen = dict(fresh, image_prompt_type="S")
        answer, _ = _answer(bridge, chosen)
        ack, applied = _answer(bridge, chosen, op="receive", handoff_id=handoff_id, receiver_id="start_frame",
                               state_revision=answer["state_revision"], source={})
        r.check("a send to a selected start frame switches nothing",
                ack.get("ok") is True and ack.get("switched") == "" and not applied.switch_updates)

        # Neither selected nor allowed: refused, and nothing else chosen.
        text = _on_model(compatibility, fresh, "text")
        answer, _ = _answer(bridge, text)
        ack, applied = _answer(bridge, text, op="receive", handoff_id=handoff_id, receiver_id="start_frame",
                               state_revision=answer["state_revision"], source={})
        r.check("a send to an input the model does not take is refused",
                ack.get("ok") is False and ack.get("code") == compatibility.RECEIVER_DISABLED, str(ack.get("code")))
        r.check("and nothing was applied", applied is None)

        # The plugin asks for its globals when it is constructed, not later.
        class Recording(bridge_plugin.MiniPaintBridgePlugin):
            def __init__(self):
                self.asked = []
                super().__init__()

            def request_global(self, name):
                self.asked.append(name)

        constructed = Recording()
        r.check("MiniPaintBridgePlugin asks for its globals in __init__",
                "get_model_def" in constructed.asked and "wan2gp_version" in constructed.asked, str(constructed.asked))


_FRAME_HARNESS = r"""
// The real bridge script, in a page that is either rendered (frames fire) or
// not (frames never fire), driven through one hello. What Gradio does is
// modelled the way Gradio really does it: its core captures
// requestAnimationFrame when its module loads and schedules its component
// flush through that captured reference; a click schedules the trigger
// inside requestAnimationFrame, and the trigger waits for the pending flush.
// The head script, when given, runs before the capture - as it does on the
// page - and the page script after it; the acknowledgement is delivered the
// way the chained .then(js=...) delivers it.
const fs = require("fs");
const script = fs.readFileSync(process.argv[2], "utf8");
const mode = process.argv[3];
const headPath = process.argv[4];
const ORIGIN = "http://forge.test";
const posted = [];
const nativeFrames = [];
const listeners = {};
let clicks = 0;
let callbackRuns = 0;
let nativeRuns = 0;
const parent = { postMessage(envelope, origin) { posted.push({ type: envelope.type, origin: origin, code: envelope.payload && envelope.payload.code || "" }); } };
class Event { constructor(type) { this.type = type; } }
const box = { tagName: "TEXTAREA", value: "", dispatchEvent() {} };
const ack = { tagName: "TEXTAREA", value: "" };
const button = { tagName: "BUTTON", click() { clicks += 1; onClick(); } };
const column = {
  id: "minipaint_bridge_1", parentElement: null,
  querySelector(selector) {
    if (selector === ".minipaint-bridge-request") { return box; }
    if (selector === ".minipaint-bridge-ack") { return ack; }
    if (selector === ".minipaint-bridge-trigger") { return button; }
    return null;
  }
};
const window = {
  location: { origin: ORIGIN }, parent: parent,
  addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
  setTimeout: setTimeout, clearTimeout: clearTimeout, setInterval: setInterval, clearInterval: clearInterval,
  performance: { now() { return Date.now(); } },
  requestAnimationFrame(callback) {
    const id = nativeFrames.push(callback);
    if (mode === "visible") {
      setTimeout(function () { const fn = nativeFrames[id - 1]; if (fn) { nativeFrames[id - 1] = null; nativeRuns += 1; fn(Date.now()); } }, 5);
    }
    return id;
  },
  cancelAnimationFrame(id) { nativeFrames[id - 1] = null; }
};
const document = {
  getElementsByClassName(name) { return name === "minipaint-bridge-column" ? [column] : []; },
  getElementById() { return null; },
  documentElement: { setAttribute() {} }, head: { appendChild() {} }, body: null,
  createElement() { return { textContent: "" }; }
};
globalThis.Event = Event;
globalThis.TextEncoder = require("util").TextEncoder;
if (headPath && headPath !== "-") {
  new Function("window", "document", fs.readFileSync(headPath, "utf8"))(window, document);
}
// Gradio's core, loading: the reference it will schedule every flush through.
const captured = window.requestAnimationFrame;
let flushPending = false;
let flushRan = false;
const waiters = [];
function scheduleFlush() {
  if (flushPending) { return; }
  flushPending = true;
  captured.call(window, function () { flushPending = false; flushRan = true; waiters.splice(0).forEach(function (fn) { fn(); }); });
}
function onClick() {
  // Blocks: requestAnimationFrame(() => wait_then_trigger_api_call(...)), and
  // the trigger waits for the pending flush before it does anything.
  window.requestAnimationFrame(function () {
    const go = function () {
      callbackRuns += 1;
      const request = JSON.parse(box.value);
      ack.value = JSON.stringify({ op: request.op, request_id: request.request_id, channel_id: request.channel_id,
        ok: true, ready: true, bridge_session: "s", instance_id: "i", receivers: [], state_revision: "abcdef12" });
      window.__minipaintBridge.deliver(ack.value);
    };
    if (flushPending) { waiters.push(go); } else { go(); }
  });
}
new Function("window", "document", script)(window, document);
// A flush left pending from before any request - what a hidden page accumulates.
scheduleFlush();
const channel = "c".repeat(32);
(listeners.message || []).forEach(function (fn) {
  fn({ origin: ORIGIN, source: parent, data: { protocol: 5, type: "WANGP_BRIDGE_HELLO", channel_id: channel, request_id: "r1", payload: {} } });
});
if (process.argv[5] === "refuse") {
  // After the hello: one send the page cannot act on (a handoff id that is
  // not one), and one on a channel this page was never bound to.
  setTimeout(function () {
    (listeners.message || []).forEach(function (fn) {
      fn({ origin: ORIGIN, source: parent, data: { protocol: 5, type: "WANGP_RECEIVE_IMAGE", channel_id: channel, request_id: "r2",
        payload: { handoff_id: "nope", receiver_id: "start_frame", state_revision: "abcdef12" } } });
      fn({ origin: ORIGIN, source: parent, data: { protocol: 5, type: "WANGP_RECEIVE_IMAGE", channel_id: "d".repeat(32), request_id: "r3",
        payload: { handoff_id: "e".repeat(32), receiver_id: "start_frame", state_revision: "abcdef12" } } });
    });
  }, 150);
}
setTimeout(function () {
  const handle = window.__minipaintFrames || null;
  console.log(JSON.stringify({
    posted: posted.map(function (p) { return p.type; }),
    codes: posted.map(function (p) { return p.code; }),
    origins: posted.map(function (p) { return p.origin; }),
    clicks: clicks, callbackRuns: callbackRuns, nativeRuns: nativeRuns, flushRan: flushRan,
    framesLeftUnrun: nativeFrames.filter(Boolean).length,
    replaced: window.requestAnimationFrame.name === "requestFrame",
    where: handle ? handle.installed : null,
    timed: handle ? handle.timedFrames : null
  }));
  process.exit(0);
}, 400);
"""


def frame_fallback_checks(r: Results) -> None:
    """A hidden iframe still answers: Gradio's frames get a timer, from the head.

    Gradio schedules every event trigger inside requestAnimationFrame and
    gates it on its component flush, which its core schedules through a
    reference captured when the module loaded. A document that is not
    rendered - the WanGP iframe whenever the Forge tab holding it is not on
    screen, which is when the Send menu asks - is given no frames. The frame
    timer therefore has to be in the page before the module: this drives the
    real scripts through a hello in a hidden and in a rendered page, with the
    head copy installed and without it, and shows that without it a hidden
    page never answers - which is the failure this exists to prevent.
    """
    import json as _json
    import shutil
    import subprocess
    import sys as _sys
    import tempfile

    node = shutil.which("node")
    if not node:
        r.check("node is available for the frame fallback checks (skipped)", True)
        return

    folder = str(BRIDGE_COPY.parent)
    added = folder not in _sys.path
    if added:
        _sys.path.insert(0, folder)
    try:
        import bridge_js
    finally:
        if added and folder in _sys.path:
            _sys.path.remove(folder)

    with tempfile.TemporaryDirectory(prefix="minipaint-wangp-frames-") as scratch:
        root = pathlib.Path(scratch)
        (root / "bridge.js").write_text(bridge_js.document_script(""), encoding="utf-8")
        (root / "head.js").write_text(bridge_js.head_script(), encoding="utf-8")
        (root / "harness.js").write_text(_FRAME_HARNESS, encoding="utf-8")
        results = {}
        for mode, head, extra in (("hidden", "head", ""), ("hidden", "-", ""), ("visible", "head", ""), ("visible", "-", ""),
                                  ("hidden", "head", "refuse")):
            try:
                run = subprocess.run([node, str(root / "harness.js"), str(root / "bridge.js"), mode,
                                      str(root / "head.js") if head == "head" else "-"] + ([extra] if extra else []),
                                     capture_output=True, text=True, timeout=30, check=False)
                results[(mode, head, extra)] = (_json.loads(run.stdout.strip().splitlines()[-1]) if run.stdout.strip()
                                                else {"error": run.stderr[-300:]})
            except Exception as error:
                results[(mode, head, extra)] = {"error": str(error)[:300]}

    hidden = results.get(("hidden", "head", ""), {})
    r.check("the head script replaces the page's requestAnimationFrame before Gradio captures it",
            hidden.get("replaced") is True and hidden.get("where") == "head", repr(hidden))
    r.check("a hidden page's click is dispatched once", hidden.get("clicks") == 1, repr(hidden))
    r.check("the flush Gradio scheduled through its captured reference runs, from the timer",
            hidden.get("flushRan") is True and hidden.get("nativeRuns") == 0, repr(hidden))
    r.check("and Gradio's trigger runs behind it exactly once", hidden.get("callbackRuns") == 1, repr(hidden))
    r.check("so the acknowledgement reaches the parent although no frame ever fired",
            hidden.get("posted") == ["WANGP_BRIDGE_READY"], repr(hidden))
    r.check("and the frames the browser never gave are cancelled rather than left waiting",
            hidden.get("framesLeftUnrun") == 0, repr(hidden))
    r.check("and it is posted to the page's own origin, nothing wider",
            hidden.get("origins") == ["http://forge.test"], repr(hidden))

    late = results.get(("hidden", "-", ""), {})
    r.check("without the head copy the page script installs the timer late and says so",
            late.get("replaced") is True and late.get("where") == "late", repr(late))
    r.check("and then a hidden page's click still runs its own frame by timer",
            late.get("clicks") == 1 and (late.get("timed") or 0) >= 1, repr(late))
    r.check("but Gradio's flush, scheduled through the reference captured before, never runs - "
            "the trigger waits for it and nothing is answered: the failure the head copy exists for",
            late.get("flushRan") is False and late.get("callbackRuns") == 0 and late.get("posted") == [], repr(late))

    for head in ("head", "-"):
        visible = results.get(("visible", head, ""), {})
        r.check(f"a rendered page's own frames win ({'with' if head == 'head' else 'without'} the head copy) - "
                "the flush and the click's - and the trigger runs exactly once",
                visible.get("callbackRuns") == 1 and visible.get("nativeRuns") == 2 and visible.get("flushRan") is True
                and visible.get("posted") == ["WANGP_BRIDGE_READY"], repr(visible))
        r.check("and no frame is left pending behind the timer", visible.get("framesLeftUnrun") == 0, repr(visible))

    refused = results.get(("hidden", "head", "refuse"), {})
    r.check("a send the page cannot act on is refused at once with a code, not dropped for the parent to time out on",
            refused.get("posted") == ["WANGP_BRIDGE_READY", "WANGP_RECEIVE_RESULT"] and refused.get("codes", [None, None])[1] == "HANDOFF_INVALID_ID",
            repr(refused))
    r.check("and never becomes a click", refused.get("clicks") == 1, repr(refused))
    r.check("while a send on a channel this page was never bound to is not answered at all",
            refused.get("posted", []).count("WANGP_RECEIVE_RESULT") == 1, repr(refused))


class _FakeLoader:
    """Jinja's loader, as far as ``page_head`` touches it."""

    def __init__(self, sources):
        self.sources = dict(sources)
        self.calls = 0

    def get_source(self, environment, template):
        self.calls += 1
        return self.sources[template], "/templates/" + template, (lambda: True)


class _FakeTemplates:
    def __init__(self, sources):
        self.env = type("Env", (), {})()
        self.env.loader = _FakeLoader(sources)
        self.env.cleared = 0
        self.env.cache = type("Cache", (), {"clear": lambda cache: setattr(self.env, "cleared", self.env.cleared + 1)})()


_PAGE_TEMPLATE = (
    "<!doctype html><html><head><meta charset=\"utf-8\">\n"
    "<script>window.__gradioFocusQueuePatch = {};</script>\n"
    "<script type=\"module\" crossorigin src=\"./assets/index-abc.js\"></script>\n"
    "</head><body></body></html>"
)


#: The parent page, its iframe, and just enough browser to see when a flush
#: happens and when it does not. The iframe is answered the way the real
#: bridge answers - a hello gets a READY, a flush request gets the recorded
#: form's fingerprint, and a probe gets one that has moved - so what is being
#: driven here is the real message round trip, not a mock of it.
_PROACTIVE_HARNESS = r"""
const fs = require("fs");

const MODE = process.argv[3] || "on";
const listeners = {};
const posted = [];
const observers = [];
let channelId = "";

function fire(kind, event) { for (const fn of listeners[kind] || []) { fn(event); } }

const contentWindow = {
  postMessage: function (message, target) {
    posted.push(message);
    if (message.type === "WANGP_BRIDGE_HELLO") {
      channelId = message.channel_id;
      reply("WANGP_BRIDGE_READY", message.request_id, {
        bridge_session: "abcdef0123456789abcdef0123456789", instance_id: "inst-1", version: "1.6.4",
        ready: true, receivers: [], state_revision: "r1", capabilities: { queue: true, start: true, track: true }
      });
      return;
    }
    if (message.type === "WANGP_FORM_FLUSH") {
      const probe = !!(message.payload && message.payload.probe);
      // A real flush moves the fingerprint; a probe reads it.
      if (!probe) { flushCalls += 1; }
      reply("WANGP_FORM_FLUSHED", message.request_id, probe
        ? { ok: true, flush: "requested", fingerprint: "fp" + flushCalls }
        : { ok: true, flush: "requested", fingerprint: "fp" + (flushCalls - 1) });
    }
  }
};
let flushCalls = 0;

function reply(type, requestId, payload) {
  setTimeout(function () {
    fire("message", {
      origin: "http://forge.test", source: contentWindow,
      data: { protocol: 5, type: type, channel_id: channelId, request_id: requestId, payload: payload }
    });
  }, 0);
}

const frame = {
  tagName: "IFRAME", isConnected: true, dataset: {}, contentWindow: contentWindow,
  getAttribute: function (n) { return n === "src" ? "/wan2gp/" : null; },
  setAttribute: function () {}, addEventListener: function () {}
};
const root = { querySelector: function () { return frame; } };

const doc = {
  readyState: "complete", visibilityState: "visible",
  addEventListener: function (k, f) { (listeners[k] = listeners[k] || []).push(f); },
  getElementById: function (id) { return id === "wangp_iframe" ? frame : null; },
  querySelector: function (sel) { return sel === "#wangp_iframe_root" ? root : null; },
  querySelectorAll: function () { return []; },
  createElement: function () { return { style: {}, setAttribute() {}, appendChild() {}, addEventListener() {} }; },
  documentElement: { classList: { contains: () => false }, style: {}, getAttribute: () => null },
  body: { classList: { contains: () => false } }, head: { appendChild() {} }
};
const win = {
  location: { href: "http://forge.test/", origin: "http://forge.test" },
  addEventListener: function (k, f) { (listeners[k] = listeners[k] || []).push(f); },
  setTimeout, clearTimeout, setInterval, clearInterval, document: doc,
  matchMedia: () => ({ matches: false, addEventListener() {} }),
  MutationObserver: class { observe() {} disconnect() {} },
  IntersectionObserver: class { constructor(fn) { this.fn = fn; observers.push(this); } observe() {} disconnect() {} },
  requestAnimationFrame: (f) => setTimeout(f, 0),
  crypto: { getRandomValues: (b) => { for (let i = 0; i < b.length; i++) b[i] = (i * 13 + 5) % 256; return b; } }
};
global.window = win; global.document = doc;
global.fetch = () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}), text: () => Promise.resolve("") });

new Function("window", "document", "fetch", fs.readFileSync(process.argv[2], "utf8"))(win, doc, global.fetch);

const api = win.minipaintWanGP;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
function screen(showing) { for (const o of observers) { o.fn([{ isIntersecting: showing }]); } }

async function main() {
  // "preset": the server's answer arrives before the WanGP page is ready,
  // which is the ordinary order - the Clipboard tab syncs long before
  // anybody opens the WanGP tab.
  if (MODE === "preset") { api.inheritSettings(true); }

  api.attach(null);
  await wait(300);                      // hello -> ready -> any ready flush
  const afterReady = flushCalls;

  if (MODE === "on" || MODE === "off") { api.inheritSettings(MODE === "on"); }
  await wait(300);
  const afterSetting = flushCalls;

  screen(true);                         // the WanGP tab is on screen
  await wait(1700);                     // and past the throttle
  const afterOnScreen = flushCalls;

  screen(false);                        // the user goes to the Clipboard tab
  await wait(300);
  const afterLeft = flushCalls;

  screen(false);                        // the same edge again
  await wait(1700);
  const afterRepeat = flushCalls;

  console.log(JSON.stringify({
    afterReady, afterSetting, afterOnScreen, afterLeft, afterRepeat,
    pending: api.state().pending, ready: api.state().ready,
    observers: observers.length
  }));
  process.exit(0);
}
main();
"""


def proactive_flush_checks(r: Results) -> None:
    """Inheritance without generating once first, and without a press waiting.

    WHAT THIS EXISTS TO CATCH.

    A queued job is built from the form Wan2GP recorded for the model, and
    Wan2GP writes that only when the user commits the form - Generate, its
    own Add to Queue, a LoRA set applied, a model switched. A page set up but
    not generated from has no recorded form at all, so a job composed from
    the Clipboard tab fell through to the settings Wan2GP loads from disk:
    close, often identical, and not what was on screen.

    The commit therefore happens ahead of any press, at the moments where it
    is free and certain to be wanted, and the order those moments arrive in
    is not fixed: the Clipboard tab syncs the setting long before anybody
    opens the WanGP tab, but a user who turns the setting on has a page that
    is already ready. Both are asserted, because only one of them was
    implemented the first time.

    Driven through Node against a stubbed window for the same reason the
    frame-timer checks are: the logic under test is this file's, and the
    round trip it depends on is the one thing a static read of the source
    could not have told us was broken - the acknowledgement was accepted,
    and then dropped, so every flush waited out its own timeout and reported
    that the bridge had not answered.
    """
    import json as _json
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if not node:
        r.check("node is available for the proactive flush checks (skipped)", True)
        return

    answers = {}
    with tempfile.TemporaryDirectory(prefix="minipaint-wangp-flush-") as scratch:
        root = pathlib.Path(scratch)
        (root / "harness.js").write_text(_PROACTIVE_HARNESS, encoding="utf-8")
        for mode in ("on", "off", "preset"):
            try:
                run = subprocess.run([node, str(root / "harness.js"), str(BROWSER_COPY), mode],
                                     capture_output=True, text=True, timeout=60, check=False)
                answers[mode] = (_json.loads(run.stdout.strip().splitlines()[-1]) if run.stdout.strip()
                                 else {"error": run.stderr[-300:]})
            except Exception as error:
                answers[mode] = {"error": str(error)[:300]}

    on = answers.get("on", {})
    r.check("nothing is committed before the server says anything reads it",
            on.get("afterReady") == 0, repr(on))
    r.check("turning inheritance on commits the live form at once - no generation first",
            on.get("afterSetting") == 1, repr(on))
    r.check("and the acknowledgement is matched, so nothing is left waiting on a timeout",
            on.get("pending") == 0, repr(on))
    r.check("coming on screen commits nothing; there is nothing new to carry",
            on.get("afterOnScreen") == 1, repr(on))
    r.check("leaving the screen does - the last instant an uncommitted slider exists, "
            "and the instant before a press in the other tab",
            on.get("afterLeft") == 2, repr(on))
    r.check("and the same edge again does not, so a flurry of visibility events costs one commit",
            on.get("afterRepeat") == 2, repr(on))

    off = answers.get("off", {})
    r.check("with inheritance off nothing is ever committed - no job reads it, so it is work for nobody",
            off.get("afterReady") == 0 and off.get("afterSetting") == 0 and off.get("afterLeft") == 0, repr(off))

    preset = answers.get("preset", {})
    r.check("a setting already known when the page becomes ready commits it there instead",
            preset.get("afterReady") == 1, repr(preset))
    r.check("and that page then behaves exactly like the other one",
            preset.get("afterLeft") == 2 and preset.get("afterRepeat") == 2, repr(preset))


#: The parent page, its WanGP iframe and a WanGP bridge inside it, on a
#: virtual clock: the heartbeat counts in five-second beats and twenty-second
#: silences, the saves in one-second quiets, and a suite that waited for those
#: on the wall clock would be a suite nobody ran. Every timer the bundle sets -
#: bare, through ``window``, or an interval - runs on this clock, in order,
#: with the promises it releases settled between one timer and the next.
#:
#: The bridge answers the way the real one does: a hello with a READY that
#: lists what it can do, a press of the form with the recorded fingerprint as
#: it was, a look with the fingerprint as it is, and WanGP's own commit lands a
#: moment after a press that had something to carry. It can be told to stop
#: answering, and the frame can be reloaded - which loses the document, fires
#: ``pagehide`` in it, and fires the frame's ``load`` when the new one is up.
_LIVE_HARNESS = r"""
const fs = require("fs");
const MODE = process.argv[3] || "save";
const ORIGIN = "http://forge.test";
const realImmediate = setImmediate;

// ---- the clock ------------------------------------------------------------
let now = 1700000000000;
let seq = 0;
const timers = new Map();
function vSet(fn, ms) { seq += 1; timers.set(seq, { at: now + Math.max(0, Number(ms) || 0), seq: seq, fn: fn, every: 0 }); return seq; }
function vEvery(fn, ms) { seq += 1; const every = Math.max(1, Number(ms) || 1); timers.set(seq, { at: now + every, seq: seq, fn: fn, every: every }); return seq; }
function vClear(id) { timers.delete(id); }
global.setTimeout = vSet; global.clearTimeout = vClear; global.setInterval = vEvery; global.clearInterval = vClear;
Date.now = function () { return now; };
const thrown = [];
async function settle() { for (let i = 0; i < 10; i += 1) { await new Promise(function (r) { realImmediate(r); }); } }
async function advance(ms) {
  const until = now + ms;
  for (;;) {
    let next = null;
    for (const t of timers.values()) {
      if (t.at > until) { continue; }
      if (!next || t.at < next.at || (t.at === next.at && t.seq < next.seq)) { next = t; }
    }
    if (!next) { break; }
    now = next.at;
    if (next.every) { next.at = now + next.every; } else { timers.delete(next.seq); }
    try { next.fn(); } catch (e) { thrown.push(String(e && e.stack || e)); }
    await settle();
  }
  now = until;
  await settle();
}
// The page was busy: the time passes, and everything that came due meanwhile
// runs at the end of it - late, and knowing it.
async function stall(ms) {
  now += ms;
  for (;;) {
    let next = null;
    for (const t of timers.values()) {
      if (t.at > now) { continue; }
      if (!next || t.at < next.at || (t.at === next.at && t.seq < next.seq)) { next = t; }
    }
    if (!next) { break; }
    if (next.every) { next.at = now + next.every; } else { timers.delete(next.seq); }
    try { next.fn(); } catch (e) { thrown.push(String(e && e.stack || e)); }
    await settle();
  }
}

// ---- the journal ----------------------------------------------------------
const lines = [];
console.debug = function (prefix, message) { if (prefix === "MiniPaint WanGP:") { lines.push(String(message)); } };

// ---- a page ---------------------------------------------------------------
class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase(); this.children = []; this.parentNode = null; this.style = { cssText: "" };
    this.attrs = {}; this.listeners = {}; this.id = ""; this.className = ""; this.textContent = ""; this.hidden = false;
    this.dataset = {};
  }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === "id") { this.id = String(v); } }
  getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) { this.children.splice(i, 1); } c.parentNode = null; return c; }
  addEventListener(k, f) { (this.listeners[k] = this.listeners[k] || []).push(f); }
  click() { for (const f of this.listeners.click || []) { f({}); } }
  querySelector(sel) { return find(this, sel); }
  get isConnected() { let n = this; while (n) { if (n === docRoot) { return true; } n = n.parentNode; } return false; }
}
function matches(el, sel) {
  if (sel[0] === "#") { return el.id === sel.slice(1); }
  if (sel[0] === ".") { return (" " + el.className + " ").indexOf(" " + sel.slice(1) + " ") !== -1; }
  return el.tagName === sel.toUpperCase();
}
function find(node, sel) {
  if (sel.indexOf(" ") !== -1) { return null; }
  for (const c of node.children) { if (matches(c, sel)) { return c; } const deep = find(c, sel); if (deep) { return deep; } }
  return null;
}
const docRoot = new El("body");
const root = new El("div"); root.id = "wangp_iframe_root"; docRoot.appendChild(root);
const frame = new El("iframe"); frame.id = "wangp_iframe"; frame.attrs.src = "/wan2gp/"; root.appendChild(frame);

// ---- WanGP, inside the frame -------------------------------------------------
const B = {
  loaded: true, answerPing: true, answerFlush: true, answerLook: true, suppress: false, dirty: false, fp: 1, commitMs: 100, flushReplyMs: 5,
  // The page attaches while it loads, so an older bridge is one from the start.
  capabilities: MODE === "legacy" ? { queue: true, start: true, track: true }
    : MODE === "noping" ? { queue: true, start: true, track: true, form_watch: true }
    : { queue: true, start: true, track: true, ping: true, form_watch: true },
  hellos: [], presses: 0, looks: 0, pings: 0, busyMs: 0, channel: "", navigations: 0, reloadMs: 500,
  // What a reloaded document can do: the same as before, unless told.
  afterReload: null
};
let inner = null;
function newDocument() {
  inner = { listeners: {}, addEventListener: function (k, f) { (this.listeners[k] = this.listeners[k] || []).push(f); },
            postMessage: function (message) { toBridge(message); } };
  frame.contentWindow = inner;
}
newDocument();
function reply(type, requestId, payload) {
  const from = inner;
  vSet(function () {
    if (from !== inner || !B.loaded) { return; }
    fire("message", { origin: ORIGIN, source: inner, data: { protocol: 5, type: type, channel_id: B.channel, request_id: requestId, payload: payload } });
  }, type === "WANGP_FORM_FLUSHED" ? B.flushReplyMs : 5);
}
function toBridge(message) {
  if (!B.loaded) { return; }
  if (message.type === "WANGP_BRIDGE_HELLO") {
    B.channel = message.channel_id;
    B.hellos.push(message.payload || {});
    reply("WANGP_BRIDGE_READY", message.request_id, {
      bridge_session: "abcdef0123456789abcdef0123456789", instance_id: "inst-1", version: "1.7.0",
      ready: true, receivers: [], state_revision: "r1", capabilities: B.capabilities
    });
    return;
  }
  if (message.channel_id !== B.channel) { return; }
  if (message.type === "WANGP_PING") {
    B.pings += 1;
    if (B.answerPing) { reply("WANGP_PONG", message.request_id, { busy_ms: B.busyMs, op: "queue", waiting: 0 }); }
    return;
  }
  if (message.type === "WANGP_FORM_FLUSH") {
    const look = !!(message.payload && message.payload.probe);
    if (look) { B.looks += 1; } else { B.presses += 1; }
    if (!B.answerFlush || (look && !B.answerLook)) { return; }
    if (!look && B.suppress) { reply("WANGP_FORM_FLUSHED", message.request_id, { ok: true, flush: "suppressed", fingerprint: "fp" + B.fp }); return; }
    const before = "fp" + B.fp;
    if (!look && B.dirty) { B.dirty = false; vSet(function () { B.fp += 1; }, B.commitMs); }
    reply("WANGP_FORM_FLUSHED", message.request_id, { ok: true, flush: "requested", fingerprint: look ? "fp" + B.fp : before });
  }
}
// Somebody touched the form in the WanGP page.
function changed() {
  B.dirty = true;
  const from = inner;
  vSet(function () {
    if (from !== inner || !B.loaded) { return; }
    fire("message", { origin: ORIGIN, source: inner, data: { protocol: 5, type: "WANGP_FORM_CHANGED", channel_id: B.channel, request_id: "change-" + now, payload: { touches: 1 } } });
  }, 1);
}
// The document goes away (WanGP reloading its page, say), with no new one yet.
function documentLeaves() {
  B.loaded = false;
  for (const f of (inner.listeners.pagehide || [])) { f({}); }
}
frame.setAttribute = function (k, v) {
  El.prototype.setAttribute.call(frame, k, v);
  if (k !== "src") { return; }
  B.navigations += 1;
  documentLeaves();
  vSet(function () {
    newDocument();
    B.loaded = true;
    B.channel = "";
    if (B.afterReload) { B.afterReload(); }
    for (const f of (frame.listeners.load || [])) { f({}); }
  }, B.reloadMs);
};

// ---- the parent page --------------------------------------------------------
const listeners = {};
function fire(kind, event) { for (const fn of listeners[kind] || []) { fn(event); } }
const observers = [];
const doc = {
  readyState: "complete", visibilityState: "visible",
  addEventListener: function (k, f) { (listeners["doc:" + k] = listeners["doc:" + k] || []).push(f); },
  getElementById: function (id) { return id === docRoot.id ? docRoot : find(docRoot, "#" + id); },
  querySelector: function (sel) { return find(docRoot, sel); },
  querySelectorAll: function () { return []; },
  createElement: function (tag) { return new El(tag); },
  documentElement: { classList: { contains: () => false }, style: {}, getAttribute: () => null },
  body: docRoot, head: new El("head")
};
const win = {
  location: { href: ORIGIN + "/", origin: ORIGIN },
  addEventListener: function (k, f) { (listeners[k] = listeners[k] || []).push(f); },
  setTimeout: vSet, clearTimeout: vClear, setInterval: vEvery, clearInterval: vClear, document: doc,
  matchMedia: () => ({ matches: false, addEventListener() {} }),
  MutationObserver: class { observe() {} disconnect() {} },
  IntersectionObserver: class { constructor(fn) { this.fn = fn; observers.push(this); } observe() {} disconnect() {} },
  getComputedStyle: function () { return { position: "static", display: "block" }; },
  requestAnimationFrame: (f) => vSet(f, 16), innerHeight: 900,
  crypto: { getRandomValues: (b) => { for (let i = 0; i < b.length; i++) { b[i] = Math.floor(Math.random() * 256); } return b; } }
};
if (MODE === "pull" || MODE === "pulloff") {
  // The public API loaded first and has already had its snapshot.
  win.minipaintInterop = { wangp: { snapshotState: function () { return { inherit: true, unattended: MODE === "pull" }; } } };
}
global.window = win; global.document = doc;
global.MutationObserver = win.MutationObserver;
global.fetch = () => Promise.resolve({ ok: true, status: 204, type: "basic", json: () => Promise.resolve({}), text: () => Promise.resolve("") });
new Function("window", "document", "fetch", fs.readFileSync(process.argv[2], "utf8"))(win, doc, global.fetch);
const api = win.minipaintWanGP;

function screen(showing) { for (const o of observers) { o.fn([{ isIntersecting: showing }]); } }
function hidden(yes) { doc.visibilityState = yes ? "hidden" : "visible"; fire("doc:visibilitychange", {}); }
function bar() { return find(docRoot, "#minipaint-wangp-stuck"); }
function barText() { const b = bar(); const t = b ? find(b, ".minipaint-wangp-stuck-text") : null; return t ? t.textContent : ""; }
function pressBar(which) { const b = bar(); const button = b ? find(b, ".minipaint-wangp-stuck-" + which) : null; if (button) { button.click(); } return !!button; }
function linesWith(text) { return lines.filter(function (line) { return line.indexOf(text) !== -1; }); }
function settleSend(promise) { const box = { answer: null, at: 0 }; promise.then(function (a) { box.answer = a; box.at = now; }); return box; }
// On screen, with the first beat at the instant returned: every beat after it
// is a whole multiple of five seconds from there.
async function bootOnScreen() { api.attach(null); await advance(100); const first = now; screen(true); await advance(1); return first; }
async function until(at) { await advance(Math.max(0, at - now)); }

const scenarios = {
  // Saving as the form changes.
  save: async function () {
    api.inheritSettings(true);
    api.attach(null);
    await advance(1000);
    const afterReady = { presses: B.presses, looks: B.looks, current: api.state().settings.current };
    changed(); await advance(300); changed(); await advance(300); changed();
    await advance(990);
    const beforeQuiet = B.presses;
    await advance(20);
    const atQuiet = B.presses;
    await advance(600);
    const afterSave = { presses: B.presses, looks: B.looks, current: api.state().settings.current, fp: B.fp };
    changed(); await advance(1001);
    changed();
    await advance(200);
    const midRunning = api.state().settings.saving;
    await advance(3000);
    return { afterReady, beforeQuiet, atQuiet, afterSave, midRunning,
             trailing: B.presses - afterSave.presses, current: api.state().settings.current, hello: B.hellos[0] || {},
             saves: api.state().settings.saves };
  },
  // WanGP slow to answer: a touch's quiet second runs out while the save
  // before it is still waiting, and it must still get a save of its own.
  slow: async function () {
    api.inheritSettings(true);
    api.attach(null);
    await advance(5000);
    B.flushReplyMs = 1500;
    const base = B.presses;
    changed(); await advance(1002);
    const first = B.presses - base;
    changed();
    await advance(1100);
    const whileRunning = { presses: B.presses - base, saving: api.state().settings.saving };
    // The first save's look comes back 3.4s after the second touch: done,
    // and the second touch's own save not yet asked for.
    await advance(2400);
    const between = { current: api.state().settings.current, presses: B.presses - base, saving: api.state().settings.saving };
    await advance(6000);
    return { first, whileRunning, between, after: B.presses - base, current: api.state().settings.current };
  },
  // A touch, then the WanGP tab left a moment later: that save carries the
  // touch, and the one the touch had coming is not made as well.
  leave: async function () {
    api.inheritSettings(true);
    api.attach(null);
    await advance(1000);
    screen(true);
    await advance(10);
    const base = B.presses;
    changed(); await advance(300);
    screen(false);
    await advance(3000);
    return { saves: B.presses - base, current: api.state().settings.current, fp: B.fp };
  },
  // Nothing reads the record: no touch saves anything.
  off: async function () {
    api.inheritSettings(false);
    api.attach(null);
    await advance(1000);
    changed(); await advance(3000);
    const sent = settleSend(api.saveForSend("the gallery's Generate"));
    await advance(10);
    return { presses: B.presses, send: sent.answer };
  },
  // Save before a send.
  send: async function () {
    api.inheritSettings(true);
    api.attach(null);
    await advance(1000);
    const base = B.presses;
    const t0 = now;
    const quick = settleSend(api.saveForSend("the gallery's Generate"));
    await advance(0);
    const quickResult = { answer: quick.answer, took: quick.at - t0, presses: B.presses - base };

    changed(); await advance(50);
    const t1 = now;
    const dirty = settleSend(api.saveForSend("the gallery's Generate"));
    await advance(2500);
    const dirtyResult = { answer: dirty.answer, took: dirty.at - t1, current: api.state().settings.current, fp: B.fp };

    B.answerFlush = false;
    changed(); await advance(50);
    const t2 = now;
    const mute = settleSend(api.saveForSend("Clipboard's Add to Queue"));
    await advance(1999);
    const beforeLimit = mute.answer;
    await advance(1);
    const muteResult = { before: beforeLimit, answer: mute.answer, took: mute.at - t2, current: api.state().settings.current,
                         said: linesWith("save before send: no answer within 2000 ms").length };
    B.answerFlush = true;
    await advance(10000);

    // WanGP takes the press and then goes quiet: every look after it hangs.
    B.answerLook = false;
    changed(); await advance(50);
    const t3 = now;
    const quiet = settleSend(api.saveForSend("the gallery's Generate"));
    await advance(1999);
    const quietBefore = quiet.answer;
    await advance(1);
    const quietResult = { before: quietBefore, answer: quiet.answer, took: quiet.at - t3 };
    B.answerLook = true;
    await advance(10000);

    B.suppress = true;
    changed(); await advance(50);
    const loading = settleSend(api.saveForSend("the gallery's Generate"));
    await advance(2500);
    const loadingResult = { answer: loading.answer, current: api.state().settings.current };
    return { quick: quickResult, dirty: dirtyResult, mute: muteResult, quiet: quietResult, loading: loadingResult };
  },
  // A bridge that does not report its changes: no send may take the record as current.
  legacy: async function () {
    api.inheritSettings(true);
    api.attach(null);
    await advance(1000);
    const base = B.presses;
    const t0 = now;
    const sent = settleSend(api.saveForSend("the gallery's Generate"));
    await advance(2500);
    return { answer: sent.answer, took: sent.at - t0, presses: B.presses - base, watched: api.state().settings.watched,
             hello: B.hellos[0] || {} };
  },
  // The public API loaded first: the tab reads what it knows.
  pull: async function () {
    api.attach(null);
    await advance(1000);
    return { wanted: api.state().settings.wanted, presses: B.presses };
  },
  pulloff: async function () {
    api.attach(null);
    await advance(1000);
    return { wanted: api.state().settings.wanted, presses: B.presses };
  },

  // The heartbeat.
  beat: async function () {
    api.attach(null);
    await advance(100);
    const beforeScreen = B.pings;
    screen(true);
    await advance(20001);
    const onScreen = B.pings;
    screen(false);
    await advance(30000);
    const offScreen = B.pings - onScreen;
    screen(true);
    await advance(1);
    const back = B.pings - onScreen;
    hidden(true);
    await advance(30000);
    const whileHidden = B.pings - onScreen - back;
    hidden(false);
    await advance(1);
    return { beforeScreen, onScreen, offScreen, back, whileHidden, misses: linesWith("heartbeat: no answer").length,
             state: api.state().heartbeat, started: linesWith("heartbeat: watching the WanGP page").length };
  },
  noping: async function () {
    await bootOnScreen();
    await advance(60000);
    return { pings: B.pings, state: api.state().heartbeat, bar: !!bar() };
  },
  stuck: async function () {
    // The beat at t0 is answered; the one at t0+5s is the first that is not,
    // so the silence is twenty seconds old at t0+25s.
    const t0 = await bootOnScreen();
    B.answerPing = false;
    await until(t0 + 24999);
    const before = { bar: !!bar(), misses: linesWith("heartbeat: no answer from the WanGP page").length };
    await until(t0 + 25000);
    const shown = { bar: !!bar(), text: barText(), onTime: linesWith("this page was on time").length };
    await until(t0 + 30000);
    const counting = barText();
    B.afterReload = function () { B.answerPing = true; };
    await until(t0 + 34999);
    const beforeReload = B.navigations;
    await until(t0 + 35000);
    const reloaded = B.navigations;
    await advance(1000);
    return { before, shown, counting, beforeReload, reloaded, barAfter: !!bar(), state: api.state().heartbeat,
             reloadLine: linesWith("heartbeat: reloading the WanGP view").length, ready: api.state().ready };
  },
  dismiss: async function () {
    const t0 = await bootOnScreen();
    B.answerPing = false;
    await until(t0 + 25000);
    const shown = !!bar();
    const pressed = pressBar("dismiss");
    await until(t0 + 55000);
    const quiet = { bar: !!bar(), navigations: B.navigations, dismissed: api.state().heartbeat.dismissed };
    B.answerPing = true;
    await until(t0 + 60100);
    const recovered = { dismissed: api.state().heartbeat.dismissed, answered: linesWith("heartbeat: the WanGP page answered again after").length,
                        repeated: linesWith("showing 'WanGP stopped responding'").length };
    B.answerPing = false;
    await advance(25000);
    return { shown, pressed, quiet, recovered, again: !!bar() };
  },
  button: async function () {
    const t0 = await bootOnScreen();
    B.answerPing = false;
    await until(t0 + 25000);
    B.afterReload = function () { B.answerPing = true; };
    const pressed = pressBar("reload");
    await advance(2000);
    return { pressed, navigations: B.navigations, state: api.state().heartbeat, bar: !!bar() };
  },
  limits: async function () {
    await bootOnScreen();
    B.answerPing = false;
    const reloadsAt = [];
    let seen = 0;
    const start = now;
    let textAtEnd = "";
    for (let i = 0; i < 180; i += 1) {
      await advance(5000);
      if (B.navigations > seen) { seen = B.navigations; reloadsAt.push(now - start); }
    }
    textAtEnd = barText();
    return { reloadsAt, navigations: B.navigations, text: textAtEnd, state: api.state().heartbeat };
  },
  late: async function () {
    // The beat due at t0+10s runs three seconds late.
    const t0 = await bootOnScreen();
    B.answerPing = false;
    await until(t0 + 9999);
    await stall(3001);
    return { busy: linesWith("this whole page was busy").length, late: linesWith("ran 3000 ms late").length,
             lines: linesWith("heartbeat: no answer") };
  },
  loading: async function () {
    await bootOnScreen();
    await advance(10000);
    documentLeaves();
    await advance(30000);
    const at30 = !!bar();
    await advance(35000);
    return { at30, at65: !!bar(), state: api.state().heartbeat };
  },
  busy: async function () {
    const t0 = await bootOnScreen();
    B.busyMs = 20000;
    await until(t0 + 20100);
    const noted = linesWith("its bridge has waited 20s for WanGP to answer 'queue'").length;
    B.busyMs = 0;
    await until(t0 + 25100);
    return { noted, cleared: linesWith("no longer waiting on WanGP").length, bar: !!bar(), navigations: B.navigations };
  }
};

(scenarios[MODE] || scenarios.save)().then(function (result) {
  console.log(JSON.stringify(Object.assign({ thrown: thrown.slice(0, 3) }, result)));
  process.exit(0);
}, function (error) {
  console.log(JSON.stringify({ error: String(error && error.stack || error) }));
  process.exit(0);
});
"""


def _run_live(modes):
    """Every mode against the real bundle, as {mode: answer}."""
    import json as _json
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if not node:
        return None
    answers = {}
    with tempfile.TemporaryDirectory(prefix="minipaint-wangp-live-") as scratch:
        root = pathlib.Path(scratch)
        (root / "harness.js").write_text(_LIVE_HARNESS, encoding="utf-8")
        for mode in modes:
            try:
                run = subprocess.run([node, str(root / "harness.js"), str(BROWSER_COPY), mode],
                                     capture_output=True, text=True, timeout=120, check=False)
                out = run.stdout.strip().splitlines()
                answers[mode] = _json.loads(out[-1]) if out else {"error": run.stderr[-400:]}
            except Exception as error:
                answers[mode] = {"error": str(error)[:300]}
    return answers


def live_settings_checks(r: Results) -> None:
    """WanGP's form saved as it changes, and again just before every send.

    WHAT THIS EXISTS TO CATCH.

    A job is composed on the server from the form WanGP recorded, and WanGP
    records it only when it is committed. The free moments to commit it -
    the page becoming ready, the tab leaving the screen, the page going to
    the background - leave one hole: a LoRA weight changed while the WanGP
    tab stays in front is committed by none of them. So the WanGP page says
    when its form is touched, and about a second after the last touch the
    form is saved (one press, one look), and a send waits - two seconds at
    most - for a save of anything still unsaved. Every answer is a word the
    job records, so a job can say where its settings came from.
    """
    answers = _run_live(("save", "slow", "leave", "off", "send", "legacy", "pull", "pulloff"))
    if answers is None:
        r.check("node is available for the live settings checks (skipped)", True)
        return

    save = answers.get("save", {})
    r.check("the harness drove the real bundle through its saves", "error" not in save and not save.get("thrown"), repr(save)[:400])
    r.check("the hello asks the WanGP page to say when its form is touched",
            (save.get("hello") or {}).get("watch_form") is True, repr(save.get("hello")))
    r.check("the page becoming ready saves once, and one look settles it - no poll",
            save.get("afterReady") == {"presses": 1, "looks": 1, "current": True}, repr(save.get("afterReady")))
    r.check("touches a second apart are not saved while they keep coming",
            save.get("beforeQuiet") == 1, repr(save))
    r.check("and one save follows about a second after the last of them",
            save.get("atQuiet") == 2, repr(save))
    after = save.get("afterSave") or {}
    r.check("that save carried the change into WanGP's record, and the page knows the record is current",
            after.get("fp") == 2 and after.get("current") is True and after.get("presses") == 2 and after.get("looks") == 2,
            repr(after))
    r.check("a touch while a save is running waits for it and then gets a save of its own",
            save.get("midRunning") is True and save.get("trailing") == 2 and save.get("current") is True, repr(save))

    slow = answers.get("slow", {})
    r.check("a touch whose quiet second runs out while the save before it still waits on WanGP is not dropped",
            slow.get("first") == 1 and (slow.get("whileRunning") or {}).get("presses") == 1
            and (slow.get("whileRunning") or {}).get("saving") is True, repr(slow))
    r.check("the record is not taken as current when the save that finished began before the last touch",
            (slow.get("between") or {}).get("current") is False and (slow.get("between") or {}).get("saving") is False
            and (slow.get("between") or {}).get("presses") == 1, repr(slow))
    r.check("that touch gets a save of its own once the first is done, and then the record is current",
            slow.get("after") == 2 and slow.get("current") is True, repr(slow))

    leave = answers.get("leave", {})
    r.check("a touch and then the WanGP tab leaving the screen is one save, made as it leaves, not two",
            leave.get("saves") == 1 and leave.get("current") is True and leave.get("fp") == 2, repr(leave))

    off = answers.get("off", {})
    r.check("with inheritance off, touches save nothing and a send asks nothing",
            off.get("presses") == 0 and (off.get("send") or {}).get("flush") == "", repr(off))

    send = answers.get("send", {})
    quick = send.get("quick") or {}
    r.check("a send with nothing new since the last save is answered at once, without asking WanGP",
            (quick.get("answer") or {}).get("flush") == "unchanged" and quick.get("took") == 0 and quick.get("presses") == 0,
            repr(quick))
    dirty = send.get("dirty") or {}
    r.check("a send right after a touch saves first and answers when the record has moved",
            (dirty.get("answer") or {}).get("flush") == "committed" and 0 < (dirty.get("took") or 0) < 1000
            and dirty.get("current") is True, repr(dirty))
    mute = send.get("mute") or {}
    r.check("a WanGP page that does not answer holds a send two seconds and no longer",
            mute.get("before") is None and (mute.get("answer") or {}).get("flush") == "unavailable"
            and mute.get("took") == 2000, repr(mute))
    r.check("and says so in the journal, and does not take the record as current",
            mute.get("said") == 1 and mute.get("current") is False, repr(mute))
    quiet = send.get("quiet") or {}
    r.check("and one that takes the press and then goes quiet mid-wait holds it no longer either",
            quiet.get("before") is None and (quiet.get("answer") or {}).get("flush") == "unavailable"
            and quiet.get("took") == 2000, repr(quiet))
    loading = send.get("loading") or {}
    r.check("a WanGP page mid settings-load answers suppressed, and the record is not taken as current",
            (loading.get("answer") or {}).get("flush") == "suppressed" and loading.get("current") is False, repr(loading))

    legacy = answers.get("legacy", {})
    r.check("a bridge that never said it reports changes is never taken at its word: a send saves and waits",
            legacy.get("watched") is False and legacy.get("presses") == 1
            and (legacy.get("answer") or {}).get("flush") == "unchanged" and 1000 < (legacy.get("took") or 0) <= 2000,
            repr(legacy))

    pull = answers.get("pull", {})
    r.check("a WanGP tab that loads after the public API took its snapshot still learns that jobs use its settings",
            pull.get("wanted") is True and pull.get("presses") == 1, repr(pull))
    pulloff = answers.get("pulloff", {})
    r.check("and learns it as off when this page runs its own queue",
            pulloff.get("wanted") is False and pulloff.get("presses") == 0, repr(pulloff))


def heartbeat_checks(r: Results) -> None:
    """The WanGP page asked every five seconds whether it is there.

    WHAT THIS EXISTS TO CATCH.

    A WanGP page that reloaded and did not come back, landed on an error
    page, or lost its script used to stay that way until somebody reloaded
    the whole browser tab; PR #102 took away the only automatic recovery.
    The heartbeat is a message inside this browser - no request, nothing
    held open - sent only while the WanGP tab is on screen. Twenty seconds
    of silence puts up "WanGP stopped responding" with Reload view, and the
    view reloads ten seconds later unless dismissed: at most once in two
    minutes, three times in the life of the page.
    """
    answers = _run_live(("beat", "noping", "stuck", "dismiss", "button", "limits", "late", "loading", "busy"))
    if answers is None:
        r.check("node is available for the heartbeat checks (skipped)", True)
        return

    beat = answers.get("beat", {})
    r.check("the harness drove the real bundle through a heartbeat", "error" not in beat and not beat.get("thrown"), repr(beat)[:400])
    r.check("nothing is asked before the WanGP tab has been on screen", beat.get("beforeScreen") == 0, repr(beat))
    r.check("on screen it asks at once and then every five seconds", beat.get("onScreen") == 5, repr(beat))
    r.check("off screen it asks nothing, however long", beat.get("offScreen") == 0, repr(beat))
    r.check("back on screen it asks at once", beat.get("back") == 1, repr(beat))
    r.check("and a hidden page asks nothing", beat.get("whileHidden") == 0, repr(beat))
    r.check("a WanGP page that answers every time writes no miss and says once that it is watched",
            beat.get("misses") == 0 and beat.get("started") == 1, repr(beat))

    noping = answers.get("noping", {})
    r.check("a bridge that never said it answers pings is never pinged, and never called stuck",
            noping.get("pings") == 0 and (noping.get("state") or {}).get("armed") is False and noping.get("bar") is False,
            repr(noping))

    stuck = answers.get("stuck", {})
    r.check("the harness drove a WanGP page that stopped answering", "error" not in stuck and not stuck.get("thrown"), repr(stuck)[:400])
    before = stuck.get("before") or {}
    r.check("every unanswered beat is written down, and nothing is shown before twenty seconds of silence",
            before.get("bar") is False and before.get("misses") == 3, repr(stuck))
    shown = stuck.get("shown") or {}
    r.check("twenty seconds after a beat went unanswered the tab says WanGP stopped responding, and when it will reload",
            shown.get("bar") is True and shown.get("text") == "WanGP stopped responding. Reloading the view in 10s.", repr(shown))
    r.check("and the journal said, each time, that this page was on time - so the silence is the WanGP page's",
            shown.get("onTime") == 4, repr(shown))
    r.check("the countdown counts down", stuck.get("counting") == "WanGP stopped responding. Reloading the view in 5s.",
            repr(stuck.get("counting")))
    r.check("ten seconds after the bar, and not before, the view reloads - in the same frame",
            stuck.get("beforeReload") == 0 and stuck.get("reloaded") == 1 and stuck.get("reloadLine") == 1, repr(stuck))
    r.check("and when the reloaded page answers, the bar is gone and the tab is ready again",
            stuck.get("barAfter") is False and stuck.get("ready") is True
            and (stuck.get("state") or {}).get("automatic_reloads") == 1, repr(stuck))

    dismiss = answers.get("dismiss", {})
    quiet = dismiss.get("quiet") or {}
    r.check("Dismiss takes the bar away and nothing is reloaded behind it",
            dismiss.get("shown") is True and dismiss.get("pressed") is True and quiet.get("bar") is False
            and quiet.get("navigations") == 0 and quiet.get("dismissed") is True, repr(dismiss))
    recovered = dismiss.get("recovered") or {}
    r.check("and the journal does not claim a bar is showing while it is dismissed",
            recovered.get("repeated") == 1, repr(dismiss))
    r.check("a WanGP page that answers again ends the episode, dismissal and all",
            recovered.get("dismissed") is False and recovered.get("answered") == 1, repr(dismiss))
    r.check("so the next silence is shown again", dismiss.get("again") is True, repr(dismiss))

    button = answers.get("button", {})
    r.check("Reload view reloads at once, and is not counted against the automatic ones",
            button.get("pressed") is True and button.get("navigations") == 1
            and (button.get("state") or {}).get("automatic_reloads") == 0 and (button.get("state") or {}).get("reloads") == 1
            and button.get("bar") is False, repr(button))

    limits = answers.get("limits", {})
    at = limits.get("reloadsAt") or []
    r.check("a WanGP page that never recovers is reloaded automatically three times in all",
            limits.get("navigations") == 3 and len(at) == 3, repr(limits))
    r.check("and never twice within two minutes",
            len(at) == 3 and all(later - earlier >= 120000 for earlier, later in zip(at, at[1:])), repr(at))
    r.check("after which the bar only offers the button, and says why",
            "It has been reloaded automatically 3 times" in (limits.get("text") or "")
            and (limits.get("state") or {}).get("counting_down") is False, repr(limits.get("text")))

    late = answers.get("late", {})
    r.check("a beat that ran late says the whole page was busy, not only WanGP, and by how much",
            late.get("busy") == 1 and late.get("late") == 1, repr(late))

    loading = answers.get("loading", {})
    r.check("a WanGP page still loading is given a minute before it is called stuck, not twenty seconds",
            loading.get("at30") is False and loading.get("at65") is True, repr(loading))

    busy = answers.get("busy", {})
    r.check("a bridge stuck waiting on WanGP's own answer is written down once, and cleared once",
            busy.get("noted") == 1 and busy.get("cleared") == 1, repr(busy))
    r.check("and nothing is done about it - a generation can hold a request for minutes",
            busy.get("bar") is False and busy.get("navigations") == 0, repr(busy))


#: The script in the WanGP document, answering the heartbeat and reporting
#: touches. What matters is what it does NOT do: a PING never becomes a click
#: (a Gradio round trip could sit behind a generation for minutes), a touch
#: is only a person's own event and never this script's own write or WanGP's,
#: and nothing is reported to a parent that did not ask.
_BRIDGE_LIVE_HARNESS = r"""
const fs = require("fs");
const script = fs.readFileSync(process.argv[2], "utf8");
const MODE = process.argv[3] || "watch";
const ORIGIN = "http://forge.test";
const posted = [];
const listeners = {};
const docListeners = {};
let clicks = 0;
const parent = { postMessage(envelope, origin) { posted.push({ type: envelope.type, channel: envelope.channel_id, request: envelope.request_id, payload: envelope.payload || {}, origin: origin }); } };
class Event { constructor(type) { this.type = type; } }
const box = { tagName: "TEXTAREA", value: "", dispatchEvent(event) { touch("input", false, this); } };
const ack = { tagName: "TEXTAREA", value: "" };
const button = { tagName: "BUTTON", click() { clicks += 1; } };
const column = {
  id: "minipaint_bridge_1", parentElement: null,
  querySelector(selector) {
    if (selector === ".minipaint-bridge-request") { return box; }
    if (selector === ".minipaint-bridge-ack") { return ack; }
    if (selector === ".minipaint-bridge-trigger") { return button; }
    return null;
  }
};
const inColumn = { closest(sel) { return sel === ".minipaint-bridge-column" ? column : null; } };
const onForm = { closest() { return null; } };
const window = {
  location: { origin: ORIGIN }, parent: parent,
  addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
  setTimeout: setTimeout, clearTimeout: clearTimeout, setInterval: setInterval, clearInterval: clearInterval,
  performance: { now() { return Date.now(); } },
  requestAnimationFrame(callback) { return setTimeout(callback, 1); },
  cancelAnimationFrame(id) { clearTimeout(id); }
};
const document = {
  getElementsByClassName(name) { return name === "minipaint-bridge-column" ? [column] : []; },
  getElementById() { return null; },
  addEventListener(type, fn, capture) { (docListeners[type] = docListeners[type] || []).push({ fn: fn, capture: capture === true }); },
  documentElement: { setAttribute() {} }, head: { appendChild() {} }, body: null,
  createElement() { return { textContent: "" }; }
};
globalThis.Event = Event;
globalThis.TextEncoder = require("util").TextEncoder;
new Function("window", "document", script)(window, document);

// A person (trusted) or a script (not) touching something on the page.
function touch(type, trusted, target) {
  for (const entry of docListeners[type] || []) { entry.fn({ type: type, isTrusted: trusted, target: target || onForm }); }
}
function send(type, payload, channel, request) {
  (listeners.message || []).forEach(function (fn) {
    fn({ origin: ORIGIN, source: parent, data: { protocol: 5, type: type, channel_id: channel, request_id: request || "r" + Math.random().toString(16).slice(2, 10), payload: payload || {} } });
  });
}
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const channel = "c".repeat(32);
const count = (type) => posted.filter((p) => p.type === type).length;

async function main() {
  send("WANGP_BRIDGE_HELLO", MODE === "unasked" ? {} : { watch_form: true }, channel, "hello1");
  await wait(20);
  const clicksAfterHello = clicks;

  // The heartbeat: answered at once, from this script, with nothing clicked.
  send("WANGP_PING", {}, channel, "ping1");
  const pongNow = count("WANGP_PONG");
  const pong = posted.filter((p) => p.type === "WANGP_PONG")[0] || null;
  send("WANGP_PING", {}, "d".repeat(32), "ping2");
  const strangerPong = count("WANGP_PONG") - pongNow;

  // Touches. Capture-phase listeners, so a component that stops propagation
  // cannot hide one.
  const captured = ["input", "change", "click", "keyup", "drop", "paste"].every((t) => (docListeners[t] || []).some((e) => e.capture));
  touch("input", false, onForm);                 // a script - WanGP loading a model's settings
  touch("input", true, inColumn);                // the bridge's own controls
  await wait(30);
  const fromScripts = count("WANGP_FORM_CHANGED");
  touch("click", true, onForm);                  // a person: a dropdown choice fires no input event
  await wait(30);
  const first = count("WANGP_FORM_CHANGED");
  const firstPayload = (posted.filter((p) => p.type === "WANGP_FORM_CHANGED")[0] || {}).payload || null;
  for (let i = 0; i < 12; i += 1) { touch("input", true, onForm); await wait(20); }
  const duringDrag = count("WANGP_FORM_CHANGED");
  await wait(700);
  const afterDrag = count("WANGP_FORM_CHANGED");
  console.log(JSON.stringify({
    clicksAfterHello, clicksAfter: clicks, pongNow, pong: pong && { channel: pong.channel, request: pong.request, payload: pong.payload, origin: pong.origin },
    strangerPong, captured, fromScripts, first, firstPayload, duringDrag, afterDrag,
    channels: posted.filter((p) => p.type === "WANGP_FORM_CHANGED").every((p) => p.channel === channel)
  }));
  process.exit(0);
}
main();
"""


def bridge_liveness_checks(r: Results) -> None:
    """The WanGP document's half of the heartbeat and of saving on change."""
    import json as _json
    import shutil
    import subprocess
    import sys as _sys
    import tempfile

    node = shutil.which("node")
    if not node:
        r.check("node is available for the bridge liveness checks (skipped)", True)
        return

    folder = str(BRIDGE_COPY.parent)
    added = folder not in _sys.path
    if added:
        _sys.path.insert(0, folder)
    try:
        import bridge_js
    finally:
        if added and folder in _sys.path:
            _sys.path.remove(folder)

    results = {}
    with tempfile.TemporaryDirectory(prefix="minipaint-wangp-bridge-live-") as scratch:
        root = pathlib.Path(scratch)
        (root / "bridge.js").write_text(bridge_js.document_script(""), encoding="utf-8")
        (root / "harness.js").write_text(_BRIDGE_LIVE_HARNESS, encoding="utf-8")
        for mode in ("watch", "unasked"):
            try:
                run = subprocess.run([node, str(root / "harness.js"), str(root / "bridge.js"), mode],
                                     capture_output=True, text=True, timeout=30, check=False)
                out = run.stdout.strip().splitlines()
                results[mode] = _json.loads(out[-1]) if out else {"error": run.stderr[-300:]}
            except Exception as error:
                results[mode] = {"error": str(error)[:300]}

    config = bridge_js.configuration("")
    r.check("the script knows the three new words from protocol.py, not from a copy of its own",
            config["types"].get("ping") == protocol.PING and config["types"].get("pong") == protocol.PONG
            and config["types"].get("formChanged") == protocol.FORM_CHANGED and protocol.PING in config["inbound"],
            repr(config["types"]))
    r.check("the parent may send a PING, and the bridge may send a PONG and a change notice - nothing else was added",
            protocol.PING in protocol.TO_BRIDGE and {protocol.PONG, protocol.FORM_CHANGED} <= protocol.TO_PARENT
            and protocol.PONG not in protocol.TO_BRIDGE and protocol.FORM_CHANGED not in protocol.TO_BRIDGE)

    watch = results.get("watch", {})
    r.check("the harness drove the real WanGP document script", "error" not in watch, repr(watch)[:400])
    r.check("a PING is answered on the spot", watch.get("pongNow") == 1, repr(watch))
    pong = watch.get("pong") or {}
    r.check("to the channel and request that asked, at this origin only",
            pong.get("channel") == "c" * 32 and pong.get("request") == "ping1" and pong.get("origin") == "http://forge.test",
            repr(pong))
    r.check("saying how long a bridge request has waited on Gradio, and nothing about the form",
            set((pong.get("payload") or {}).keys()) == {"busy_ms", "op", "waiting"}, repr(pong.get("payload")))
    r.check("and never through Gradio: answering it clicks nothing",
            watch.get("clicksAfter") == watch.get("clicksAfterHello"), repr(watch))
    r.check("a PING on a channel this page is not bound to is not answered", watch.get("strangerPong") == 0, repr(watch))
    r.check("touches are listened for in the capture phase, clicks and keys included", watch.get("captured") is True, repr(watch))
    r.check("a script's change is not a touch - neither WanGP's own nor this script's writes to its request box",
            watch.get("fromScripts") == 0, repr(watch))
    r.check("a person's click is, and it is said at once", watch.get("first") == 1, repr(watch))
    r.check("with a count and nothing else - no value crosses",
            watch.get("firstPayload") == {"touches": 1}, repr(watch.get("firstPayload")))
    r.check("a drag of a dozen input events inside half a second after a notice sends none while it lasts",
            watch.get("duringDrag") == watch.get("first"), repr(watch))
    r.check("and exactly one when the half second is up, so the last touch is never left unsaid",
            watch.get("afterDrag") == (watch.get("first") or 0) + 1, repr(watch))
    r.check("every notice goes to the channel that asked for them", watch.get("channels") is True, repr(watch))

    unasked = results.get("unasked", {})
    r.check("a parent whose hello did not ask is told of no touch at all",
            "error" not in unasked and unasked.get("first") == 0 and unasked.get("afterDrag") == 0, repr(unasked))
    r.check("but is still answered when it pings", unasked.get("pongNow") == 1, repr(unasked))


def page_head_checks(r: Results) -> None:
    """The frame timer goes into the page head, before Gradio's module.

    Gradio's core captures ``requestAnimationFrame`` when its module loads;
    a script that must be seen by that capture has to be in the HTML before
    the module tag. ``page_head`` wraps the template loader the way WanGP's
    own focus patch does, for the two page templates only, once.
    """
    import sys as _sys

    folder = str(BRIDGE_COPY.parent)
    added = folder not in _sys.path
    if added:
        _sys.path.insert(0, folder)
    try:
        import bridge_js
        import page_head
        import plugin as bridge_plugin
    finally:
        if added and folder in _sys.path:
            _sys.path.remove(folder)

    script = bridge_js.head_script()
    r.check("the head script is the frame timer and nothing else - no protocol, no parent, no controls",
            "installFrames(window" in script and "__minipaintFrames" in script
            and "postMessage" not in script and "minipaint-bridge" not in script, str(len(script)))
    r.check("and it carries the two delays the page script uses",
            '"frameFallbackMs": %d' % bridge_js.FRAME_FALLBACK_MS in script
            and '"idleFrameFallbackMs": %d' % bridge_js.IDLE_FRAME_FALLBACK_MS in script)

    injected = page_head.inject(_PAGE_TEMPLATE, "SCRIPT")
    module_at = injected.find(page_head.MODULE_TAG)
    script_at = injected.find("SCRIPT")
    patch_at = injected.find("__gradioFocusQueuePatch")
    r.check("inject puts the script before the module tag", 0 <= script_at < module_at, str((script_at, module_at)))
    r.check("and after what was already there - WanGP's own head script keeps its place",
            patch_at < script_at, str((patch_at, script_at)))
    r.check("in a script tag of its own", "<script>\nSCRIPT\n</script>" in injected)
    r.check("a source that already carries the sentinel is left alone",
            page_head.inject(injected.replace("SCRIPT", page_head.SENTINEL), "AGAIN") == injected.replace("SCRIPT", page_head.SENTINEL))
    without_module = "<html><head><title>x</title></head><body></body></html>"
    r.check("a template without a module tag gets it before </head>",
            page_head.inject(without_module, "S").find("S") < page_head.inject(without_module, "S").find("</head>"))
    r.check("and one without either gets it at the end, never nothing",
            page_head.inject("<p>bare</p>", "S").endswith("<script>\nS\n</script>\n"))

    fake = _FakeTemplates({"frontend/index.html": _PAGE_TEMPLATE, "frontend/share.html": _PAGE_TEMPLATE,
                           "frontend/other.html": _PAGE_TEMPLATE})
    installed, why = page_head.install("SCRIPT", fake)
    r.check("install wraps the loader and clears the template cache", installed and why == "installed" and fake.env.cleared == 1,
            str((installed, why, fake.env.cleared)))
    source, filename, uptodate = fake.env.loader.get_source("env", "frontend/index.html")
    r.check("the page template comes back with the script before the module tag, name and freshness untouched",
            0 <= source.find("SCRIPT") < source.find(page_head.MODULE_TAG) and filename == "/templates/frontend/index.html"
            and uptodate() is True, str(filename))
    share, _, _ = fake.env.loader.get_source("env", "frontend/share.html")
    other, _, _ = fake.env.loader.get_source("env", "frontend/other.html")
    r.check("the share page too, and no other template", "SCRIPT" in share and "SCRIPT" not in other)
    again, why_again = page_head.install("OTHER", fake)
    source_again, _, _ = fake.env.loader.get_source("env", "frontend/index.html")
    r.check("a second install is a no-op that says so", again and why_again == "already installed"
            and "OTHER" not in source_again and fake.env.cleared == 1, str((again, why_again)))
    bare = type("Templates", (), {"env": type("Env", (), {"loader": object()})()})()
    refused, why_refused = page_head.install("SCRIPT", bare)
    r.check("a loader with no get_source is a reason, not a crash", refused is False and "get_source" in why_refused, why_refused)
    r.check("and no templates at all is a reason too", page_head.install("SCRIPT", None)[0] in (True, False))

    class Recorder:
        def __init__(self):
            self.order = []
        def request_component(self, elem_id):
            self.order.append("request_component")
        def request_global(self, name):
            self.order.append("request_global")
        def add_custom_js(self, script):
            self.order.append("add_custom_js")

    host = Recorder()
    templates = _FakeTemplates({"frontend/index.html": _PAGE_TEMPLATE})
    instance = _bare_plugin(bridge_plugin, host, add_custom_js=host.add_custom_js, page_templates=templates)
    instance.setup_ui()
    page, _, _ = templates.env.loader.get_source("env", "frontend/index.html")
    r.check("setup_ui places the head script through the template loader, in the same phase as the page script",
            getattr(instance, "head_installed", False) is True and "installFrames(window" in page
            and page.find("installFrames(window") < page.find(page_head.MODULE_TAG), str(host.order[:4]))
    calls = templates.env.cleared
    instance._inject_head()
    r.check("and a second pass does not wrap the loader twice", templates.env.cleared == calls)

    def broken(script, templates=None):
        raise RuntimeError("no loader today")

    original = page_head.install
    page_head.install = broken
    try:
        quiet = _bare_plugin(bridge_plugin, host, add_custom_js=host.add_custom_js)
        quiet._inject_head()
    finally:
        page_head.install = original
    r.check("a loader that cannot be wrapped leaves the flag down and the page script to its late install",
            getattr(quiet, "head_installed", None) is False)


def _first_candidate():
    """The first place ``compatibility`` looks for WanGP's base class."""
    import sys

    folder = str(BRIDGE_COPY.parent)
    added = folder not in sys.path
    if added:
        sys.path.insert(0, folder)
    try:
        import compatibility

        return tuple(compatibility.PLUGIN_BASE_CANDIDATES[0])
    finally:
        if added and folder in sys.path:
            sys.path.remove(folder)


def component_handoff_checks(r: Results) -> None:
    """The components arrive as an argument, and nowhere else.

    WanGP resolves what ``setup_ui`` asked for and hands the mapping to
    ``post_ui_setup``. Reading them off the plugin object instead finds
    nothing, and every receiver then reports itself missing however right its
    elem_id was - which is exactly what a real install said.
    """
    import sys as _sys

    folder = str(BRIDGE_COPY.parent)
    added = folder not in _sys.path
    if added:
        _sys.path.insert(0, folder)
    try:
        import compatibility
    finally:
        if added and folder in _sys.path:
            _sys.path.remove(folder)

    host = compatibility.Host(None)
    compat = compatibility.Compatibility(host=host)

    asked = compat.declare()
    r.check("setup_ui asks for every candidate id", len(asked) > 5, str(len(asked)))
    for wanted in ("image_start", "image_end", "image_refs", "image_prompt_type", "video_prompt_type"):
        r.check(f"{wanted} is among the ids asked for", wanted in asked)

    # Nothing handed over yet: this is the state a real install reported.
    empty = compat.resolve()
    r.check("with nothing handed over, the mandatory ones are missing",
            bool(empty.missing_mandatory), repr(empty.missing_mandatory))

    # Now the documented handoff. Real component shapes, because a value that
    # is not one is rejected on the way in - see the crash below.
    class Handed:
        def __init__(self, name):
            self._id = name
            self.name = name

        def get_config(self):
            return {}

    handed = {name: Handed(name) for name in
              ("image_start", "image_end", "image_refs", "image_prompt_type", "video_prompt_type")}
    taken = host.accept_components(handed)
    r.check("the mapping post_ui_setup was called with is taken", taken == len(handed), str(taken))

    resolved = compat.resolve()
    r.check("and every mandatory receiver then resolves",
            not resolved.missing_mandatory, repr(resolved.missing_mandatory))
    r.check("start and end frames are wired to the ids WanGP handed over",
            resolved.elem_ids.get(compatibility.START_IMAGE) == "image_start"
            and resolved.elem_ids.get(compatibility.END_IMAGE) == "image_end",
            repr(resolved.elem_ids))
    r.check("and the component itself is the object handed over, not a copy",
            resolved.components.get(compatibility.START_IMAGE) is handed["image_start"])

    # A host that hands over nothing usable must not pretend otherwise.
    r.check("a non-mapping is ignored rather than trusted",
            compatibility.Host(None).accept_components(["not", "a", "dict"]) == 0)

    # The crash this cost a real install: WanGP answers components and globals
    # out of one mapping, and their names overlap. A value that is not a
    # component reached a Gradio event, create_ui() raised, and WanGP restarted
    # into safe mode with every user plugin disabled - other people's too.
    class Block:
        _id = 1

        def get_config(self):
            return {}

    r.check("a string is not mistaken for a component", not compatibility.Host.is_component("t2v_A"))
    r.check("nor a dict, nor None",
            not compatibility.Host.is_component({"a": 1}) and not compatibility.Host.is_component(None))
    r.check("a gradio-shaped block is one", compatibility.Host.is_component(Block()))

    mixed = compatibility.Host(None)
    kept = mixed.accept_components({
        "image_start": Block(), "model_type": "t2v_A", "state": {"queue": []},
    })
    r.check("only the real component is kept from a mixed mapping", kept == 1, str(kept))
    r.check("and the global's value is not readable as a component",
            mixed.read_component("model_type") is None)

    # And the name that collided is no longer asked for as a widget.
    selector = compatibility.COMPONENTS_BY_KEY[compatibility.MODEL_SELECTOR]
    r.check("the model selector never asks for the global's name",
            "model_type" not in selector.candidates, str(selector.candidates))

    # Whatever WanGP hands over, nothing that is not a component reaches an event.
    host2 = compatibility.Host(None)
    compat2 = compatibility.Compatibility(host=host2)
    compat2.declare()
    handed2 = {name: Block() for name in
               ("image_start", "image_end", "image_refs", "image_prompt_type", "video_prompt_type")}
    handed2.update({"model_type": "t2v_A", "state": {}})
    host2.accept_components(handed2)
    compat2.resolve()
    passed = [component for _key, component in compat2.state_components()]
    r.check("every input handed to Gradio is a real component",
            passed and all(compatibility.Host.is_component(item) for item in passed),
            repr([type(item).__name__ for item in passed]))


def injection_timing_checks(r: Results) -> None:
    """The browser half is declared, not added afterwards.

    Everything a WanGP plugin declares - the components it wants, the script
    it adds - is declared in ``setup_ui``, before the main UI is built. Doing
    it in ``post_ui_setup`` instead is accepted without complaint and never
    reaches the document, which is a bridge that loads, resolves every
    component, and is silent.
    """
    import sys as _sys

    folder = str(BRIDGE_COPY.parent)
    added = folder not in _sys.path
    if added:
        _sys.path.insert(0, folder)
    try:
        import plugin as bridge_plugin
    finally:
        if added and folder in _sys.path:
            _sys.path.remove(folder)

    class Recorder:
        def __init__(self):
            self.order = []
        def request_component(self, elem_id):
            self.order.append("request_component")
        def request_global(self, name):
            self.order.append("request_global")
        def add_custom_js(self, script):
            self.order.append("add_custom_js")
            self.script = script

    host = Recorder()
    instance = bridge_plugin.MiniPaintBridgePlugin.__new__(bridge_plugin.MiniPaintBridgePlugin)
    instance.bridge = bridge_plugin.MiniPaintBridge(
        host=bridge_plugin.compatibility.Host(host),
        environ={"MINIPAINT_WANGP_INSTANCE_ID": "i", "MINIPAINT_WANGP_HANDOFF_ROOT": "/tmp"},
    )
    instance.declared = False
    instance.controls = None
    instance.wired = False
    instance.instances = 0
    instance.injected = False
    instance.add_custom_js = host.add_custom_js
    instance.page_templates = _FakeTemplates({"frontend/index.html": _PAGE_TEMPLATE})

    instance.setup_ui()
    r.check("the script is handed over during setup_ui", "add_custom_js" in host.order, str(host.order[:3]))
    r.check("and it is a real script, not an empty string",
            len(getattr(host, "script", "")) > 500, str(len(getattr(host, "script", ""))))
    r.check("the components are asked for in the same phase", "request_component" in host.order)

    handed = host.order.count("add_custom_js")
    instance._inject_script()
    r.check("a second pass does not hand it over twice",
            host.order.count("add_custom_js") == handed, str(host.order.count("add_custom_js")))

    # A build that offers no way to add JavaScript must say so rather than go
    # quiet - a silent bridge is what cost several rounds of this.
    quiet = bridge_plugin.MiniPaintBridgePlugin.__new__(bridge_plugin.MiniPaintBridgePlugin)
    quiet.bridge = instance.bridge
    quiet.declared = False
    quiet.controls = None
    quiet.wired = False
    quiet.instances = 0
    quiet.injected = False
    for name in ("add_custom_js", "add_js", "custom_js"):
        setattr(quiet, name, None)
    quiet._inject_script()
    r.check("a WanGP with no JavaScript hook leaves the flag down", quiet.injected is False)

    # And one that refuses the script is not mistaken for one that took it.
    def refuse(_script):
        raise RuntimeError("not accepted")

    refused = bridge_plugin.MiniPaintBridgePlugin.__new__(bridge_plugin.MiniPaintBridgePlugin)
    refused.bridge = instance.bridge
    refused.declared = False
    refused.controls = None
    refused.wired = False
    refused.instances = 0
    refused.injected = False
    for name in ("add_custom_js", "add_js", "custom_js"):
        setattr(refused, name, refuse)
    refused._inject_script()
    r.check("a refused script leaves the flag down too", refused.injected is False)

def _bare_plugin(bridge_plugin, host, **hooks):
    """A plugin instance without WanGP's base class, with the hooks given."""
    instance = bridge_plugin.MiniPaintBridgePlugin.__new__(bridge_plugin.MiniPaintBridgePlugin)
    instance.bridge = bridge_plugin.MiniPaintBridge(
        host=bridge_plugin.compatibility.Host(host),
        environ={"MINIPAINT_WANGP_INSTANCE_ID": "i", "MINIPAINT_WANGP_HANDOFF_ROOT": "/tmp"},
    )
    instance.declared = False
    instance.controls = None
    instance.wired = False
    instance.instances = 0
    instance.injected = False
    # The head script goes through Gradio's template loader; a fake one keeps
    # these checks off the real Gradio in this process.
    instance.page_templates = _FakeTemplates({"frontend/index.html": _PAGE_TEMPLATE})
    for name, hook in hooks.items():
        setattr(instance, name, hook)
    return instance


def _wangp_insertions(all_components, requests):
    """WanGP's own ``insert_after`` processing, in the shape it really has.

    After every plugin's ``post_ui_setup``, WanGP takes each request, enters
    the target's container, calls the builder, and moves the container's last
    child to sit behind the target. What a builder creates is on the page
    because of the ``with parent:`` - and only because of it.
    """
    placed = []
    for target_name, builder in requests:
        target = all_components.get(target_name)
        parent = getattr(target, "parent", None)
        if not target or not parent or not hasattr(parent, "children"):
            placed.append(None)
            continue
        target_index = parent.children.index(target)
        with parent:
            builder()
        newly_added = parent.children.pop(-1)
        parent.children.insert(target_index + 1, newly_added)
        placed.append(newly_added)
    return placed


def placement_checks(r: Results) -> None:
    """The controls are on the page, or the bridge says why not.

    A component created outside a Blocks context belongs to no page: it has
    an id, an event can name it, and the page config still does not contain
    it. ``setup_ui`` runs before WanGP's Blocks exist, so a bridge that built
    its controls there had a trigger the browser could never find - the
    handshake's ``pump`` retried its twenty times and gave up, every load.
    The only way onto the page is WanGP's ``insert_after``, and this drives
    the bridge through it exactly as WanGP does, against real Gradio.
    """
    import contextlib
    import io
    import json as _json
    import sys as _sys

    import gradio as gr

    folder = str(BRIDGE_COPY.parent)
    added = folder not in _sys.path
    if added:
        _sys.path.insert(0, folder)
    try:
        import bridge_js
        import bridge_ui
        import plugin as bridge_plugin
    finally:
        if added and folder in _sys.path:
            _sys.path.remove(folder)

    class Host:
        """What WanGP's base class offers a plugin, and nothing more."""

        def __init__(self):
            self.inserts = []
            self.script = ""

        def request_component(self, elem_id):
            pass

        def request_global(self, name):
            pass

        def add_custom_js(self, script):
            self.script = script

        def insert_after(self, target, builder):
            self.inserts.append((target, builder))

    def page_of(blocks):
        return _json.loads(_json.dumps(blocks.get_config_file(), default=str))

    def by_elem_id(page, elem_id):
        for component in page["components"]:
            if component.get("props", {}).get("elem_id") == elem_id:
                return component
        return None

    def dangling(page):
        known = {component["id"] for component in page["components"]}
        return [d for d in page["dependencies"] if any(i not in known for i in d["inputs"] + d["outputs"])]

    host = Host()
    plugin = _bare_plugin(bridge_plugin, host, insert_after=host.insert_after, add_custom_js=host.add_custom_js)

    with gr.Blocks() as demo:
        # The Media Generator form, as far as the bridge can tell it apart.
        with gr.Row() as form:
            image_start = gr.Image(label="start")
            image_end = gr.Image(label="end")
            image_refs = gr.Gallery(label="refs")
            image_prompt_type = gr.Text(value="S", visible=False)
            video_prompt_type = gr.Text(value="", visible=False)
            neighbour = gr.Textbox(label="what WanGP keeps after the target")
        handed = {
            "image_start": image_start, "image_end": image_end, "image_refs": image_refs,
            "image_prompt_type": image_prompt_type, "video_prompt_type": video_prompt_type,
            "model_type": "t2v_A",
        }
        fns_before = len(demo.fns)

        plugin.setup_ui()
        r.check("setup_ui builds nothing: there is no page to build into yet",
                plugin.controls is None and len(demo.fns) == fns_before)

        plugin.post_ui_setup(handed)
        r.check("post_ui_setup asks WanGP to place the controls after the image prompt type",
                len(host.inserts) == 1 and host.inserts[0][0] == "image_prompt_type", repr([i[0] for i in host.inserts]))
        r.check("and still builds nothing itself - the builder has not been called",
                plugin.controls is None and len(demo.fns) == fns_before)

        placed = _wangp_insertions(dict(handed, neighbour=neighbour), host.inserts)
        column = placed[0]
        # Gradio wraps text fields in a Form of their own when their row
        # closes, so the target's container is that Form, not the Row - which
        # is also where WanGP's inserter puts the column, because it goes by
        # the target's own parent.
        container = image_prompt_type.parent
        r.check("the target sits in a container of Gradio's making, as it does in WanGP",
                container is not form and container in form.children, type(container).__name__)
        r.check("WanGP's inserter put one column right behind the target",
                column is not None and container.children.index(column) == container.children.index(image_prompt_type) + 1)
        r.check("and it is the bridge's own column", plugin.controls is not None and column is plugin.controls.column)
        r.check("WanGP's neighbour is where it was", container.children[-1] is neighbour)
        r.check("the event was wired inside the builder: a click and a then",
                plugin.wired is True and len(demo.fns) == fns_before + 2, f"{plugin.wired} {len(demo.fns) - fns_before}")

        first = page_of(demo)
        ids = {c["props"].get("elem_id") for c in first["components"] if c.get("props")}
        for prefix in (bridge_ui.COLUMN_ELEM_ID, bridge_ui.REQUEST_ELEM_ID,
                       bridge_ui.ACK_ELEM_ID, bridge_ui.TRIGGER_ELEM_ID):
            r.check(f"{prefix}_1 is in the page config", f"{prefix}_1" in ids)
        trigger = by_elem_id(first, f"{bridge_ui.TRIGGER_ELEM_ID}_1")
        request = by_elem_id(first, f"{bridge_ui.REQUEST_ELEM_ID}_1")
        r.check("the trigger carries the class the script looks for",
                trigger is not None and bridge_ui.TRIGGER_CLASS in trigger["props"].get("elem_classes", []))
        r.check("so does the request box",
                request is not None and bridge_ui.REQUEST_CLASS in request["props"].get("elem_classes", []))
        r.check("and the column", bridge_ui.COLUMN_CLASS in by_elem_id(first, f"{bridge_ui.COLUMN_ELEM_ID}_1")["props"].get("elem_classes", []))
        r.check("none of it is visible", all(
            by_elem_id(first, f"{prefix}_1")["props"].get("visible") is False
            for prefix in (bridge_ui.COLUMN_ELEM_ID, bridge_ui.REQUEST_ELEM_ID, bridge_ui.TRIGGER_ELEM_ID)))

        click = [d for d in first["dependencies"] if any(t[0] == trigger["id"] and t[1] == "click" for t in d["targets"])]
        r.check("the trigger's click is a registered event", len(click) == 1, str(len(click)))
        if click:
            r.check("its first input is the request box, then this form's state",
                    click[0]["inputs"][0] == request["id"] and image_prompt_type._id in click[0]["inputs"]
                    and image_start._id in click[0]["inputs"], repr(click[0]["inputs"]))
            r.check("its outputs are the acknowledgement and then this form's receivers",
                    click[0]["outputs"][0] == plugin.controls.ack._id and image_start._id in click[0]["outputs"]
                    and image_refs._id in click[0]["outputs"], repr(click[0]["outputs"]))
        r.check("nothing on the page dangles", not dangling(first), str(dangling(first)[:1]))

        # WanGP builds the form again for its hidden Edit tab and calls
        # post_ui_setup again with that form's components. Each set must be
        # bound to its own form: the first form's trigger must not read the
        # second form's values, and the ids must not collide.
        with gr.Row() as edit_form:
            image_start2 = gr.Image(label="start (edit)")
            image_end2 = gr.Image(label="end (edit)")
            image_refs2 = gr.Gallery(label="refs (edit)")
            image_prompt_type2 = gr.Text(value="S", visible=False)
            video_prompt_type2 = gr.Text(value="", visible=False)
        handed2 = {
            "image_start": image_start2, "image_end": image_end2, "image_refs": image_refs2,
            "image_prompt_type": image_prompt_type2, "video_prompt_type": video_prompt_type2,
        }
        host.inserts.clear()
        plugin.post_ui_setup(handed2)
        placed2 = _wangp_insertions(handed2, host.inserts)
        r.check("the second form gets its own set of controls",
                placed2 and placed2[0] is not None and placed2[0] is not column and plugin.instances == 2)
        second = page_of(demo)
        ids2 = {c["props"].get("elem_id") for c in second["components"] if c.get("props")}
        r.check("with element ids of its own", f"{bridge_ui.TRIGGER_ELEM_ID}_2" in ids2 and f"{bridge_ui.REQUEST_ELEM_ID}_2" in ids2)
        r.check("and the first set's ids are still there", f"{bridge_ui.TRIGGER_ELEM_ID}_1" in ids2)
        trigger2 = by_elem_id(second, f"{bridge_ui.TRIGGER_ELEM_ID}_2")
        click2 = [d for d in second["dependencies"] if any(t[0] == trigger2["id"] and t[1] == "click" for t in d["targets"])]
        r.check("the second set's event reads the second form",
                click2 and image_prompt_type2._id in click2[0]["inputs"] and image_start2._id in click2[0]["outputs"])
        r.check("and not the first", click2 and image_prompt_type._id not in click2[0]["inputs"]
                and image_start._id not in click2[0]["outputs"])
        click1 = [d for d in second["dependencies"] if any(t[0] == trigger["id"] and t[1] == "click" for t in d["targets"])]
        r.check("the first set's event still reads the first form, not the second",
                click1 and image_prompt_type._id in click1[0]["inputs"] and image_prompt_type2._id not in click1[0]["inputs"])
        r.check("nothing dangles with two sets either", not dangling(second))

    # A WanGP without insert_after cannot be given the controls. That is a
    # sentence on its console, not a crash in its startup, and not silence.
    class OldHost(Host):
        insert_after = None

    old_host = OldHost()
    old = _bare_plugin(bridge_plugin, old_host, add_custom_js=old_host.add_custom_js)
    old.insert_after = None
    said = io.StringIO()
    with gr.Blocks():
        with gr.Row():
            handed_old = {"image_prompt_type": gr.Text(visible=False), "image_start": gr.Image()}
        old.setup_ui()
        with contextlib.redirect_stdout(said):
            old.post_ui_setup(handed_old)
    r.check("a WanGP without insert_after is told so", "insert_after" in said.getvalue(), said.getvalue())
    r.check("and nothing was wired or built", old.wired is False and old.controls is None)

    # Handed nothing, there is nowhere to go, and that is said too.
    nowhere = _bare_plugin(bridge_plugin, host, insert_after=host.insert_after, add_custom_js=host.add_custom_js)
    host.inserts.clear()
    said = io.StringIO()
    nowhere.setup_ui()
    with contextlib.redirect_stdout(said):
        nowhere.post_ui_setup({})
    r.check("handed no component, the bridge says it has nowhere to go",
            "no component" in said.getvalue() and not host.inserts, said.getvalue())

    # Handed only globals - WanGP mixes them into the same mapping - there is
    # still nowhere to go: a global's name is a target WanGP cannot find.
    host.inserts.clear()
    said = io.StringIO()
    with contextlib.redirect_stdout(said):
        nowhere.post_ui_setup({"model_type": "t2v_A", "state": {"queue": []}})
    r.check("a global's name is never asked for as the place to sit",
            not host.inserts and "no component" in said.getvalue(), repr([i[0] for i in host.inserts]))

    # A builder that cannot wire still hands WanGP exactly one column, so
    # WanGP's "move the last child" moves ours and not its own.
    broken_host = Host()
    broken = _bare_plugin(bridge_plugin, broken_host, insert_after=broken_host.insert_after,
                          add_custom_js=broken_host.add_custom_js)
    broken._make_handler = lambda: (lambda *a, **k: None)
    real_wire = bridge_ui.wire

    def refuse(*_args, **_kwargs):
        raise RuntimeError("no event for you")

    with gr.Blocks() as demo3:
        with gr.Row() as form3:
            target3 = gr.Text(visible=False)
            keeper = gr.Textbox(label="WanGP's own last child")
        broken.setup_ui()
        broken.post_ui_setup({"image_prompt_type": target3})
        bridge_ui.wire = refuse
        said = io.StringIO()
        try:
            with contextlib.redirect_stdout(said):
                placed3 = _wangp_insertions({"image_prompt_type": target3}, broken_host.inserts)
        finally:
            bridge_ui.wire = real_wire
    r.check("a wiring failure is a note", "could not be wired" in said.getvalue(), said.getvalue())
    r.check("and WanGP still received the bridge's column, not its own last child",
            placed3 and placed3[0] is broken.controls.column and target3.parent.children[-1] is keeper)
    r.check("with the failure recorded on the plugin", broken.wired is None)

    # The browser half looks for the set that is on screen, by class.
    script = bridge_js.document_script("")
    r.check("the script finds the controls by class", "columnClass" in script and "getElementsByClassName" in script)
    r.check("and never one of them by id",
            "getElementById(CONFIG" not in script and "requestElemId" not in script and "triggerElemId" not in script)
    r.check("and prefers the set whose surroundings are displayed", "surroundingsDisplayed" in script)
    config = bridge_js.configuration()
    r.check("the classes in the script are the ones the components carry",
            config["requestClass"] == bridge_ui.REQUEST_CLASS and config["triggerClass"] == bridge_ui.TRIGGER_CLASS
            and config["columnClass"] == bridge_ui.COLUMN_CLASS)


def _predict(client, body):
    """Gradio's predict endpoint, wherever this Gradio keeps it.

    Gradio 5 serves it under ``/gradio_api/``; Gradio 4 - Forge Neo's pin -
    at the root. The first that answers anything but 404 is the one.
    """
    for path in ("/gradio_api/run/predict", "/run/predict", "/api/predict"):
        response = client.post(path, json=body)
        if response.status_code != 404:
            return response
    return response


def session_hash_checks(r: Results) -> None:
    """The bridge's event is handed Gradio's request, and so a session hash.

    Gradio fills in its request object only for a *positional* parameter
    annotated as one, and stops reading the signature at the first parameter
    that is not positional. A handler written ``(*values, request=None)``
    therefore never receives it, derives no session, and refuses every hello
    with BRIDGE_SESSION_MISMATCH - which is exactly what a real install did
    once its controls were finally on the page. This drives the wired event
    through Gradio's own predict endpoint with a session hash and reads the
    session it derives.
    """
    import json as _json
    import sys as _sys

    import gradio as gr
    from gradio import helpers
    from gradio.routes import App
    from starlette.testclient import TestClient

    folder = str(BRIDGE_COPY.parent)
    added = folder not in _sys.path
    if added:
        _sys.path.insert(0, folder)
    try:
        import plugin as bridge_plugin
    finally:
        if added and folder in _sys.path:
            _sys.path.remove(folder)

    class Host:
        def __init__(self):
            self.inserts = []

        def request_component(self, elem_id):
            pass

        def request_global(self, name):
            pass

        def add_custom_js(self, script):
            pass

        def insert_after(self, target, builder):
            self.inserts.append((target, builder))

    host = Host()
    plugin = _bare_plugin(bridge_plugin, host, insert_after=host.insert_after, add_custom_js=host.add_custom_js)

    # First the rule itself, on the handler alone.
    handler = plugin._make_handler()
    inputs = ["the request json"]
    helpers.special_args(handler, inputs=inputs, request="THE REQUEST")
    r.check("Gradio fills the handler's request in, ahead of the request json",
            inputs and inputs[0] == "THE REQUEST" and inputs[1] == "the request json", repr(inputs))

    # Then the whole path: a wired event, Gradio's own endpoint, a session hash.
    with gr.Blocks() as demo:
        with gr.Row():
            image_start = gr.Image(label="start")
            image_end = gr.Image(label="end")
            image_refs = gr.Gallery(label="refs")
            image_prompt_type = gr.Text(value="S", visible=False)
            video_prompt_type = gr.Text(value="", visible=False)
        handed = {
            "image_start": image_start, "image_end": image_end, "image_refs": image_refs,
            "image_prompt_type": image_prompt_type, "video_prompt_type": video_prompt_type,
        }
        plugin.setup_ui()
        plugin.post_ui_setup(handed)
        _wangp_insertions(handed, host.inserts)
        trigger_id = plugin.controls.trigger._id

    app = App.create_app(demo)
    client = TestClient(app)
    config = client.get("/config").json()
    index = next(i for i, d in enumerate(config["dependencies"])
                 if any(t[0] == trigger_id and t[1] == "click" for t in d["targets"]))
    width = len(config["dependencies"][index]["inputs"])

    def hello(session_hash):
        body = {
            "data": [_json.dumps({"op": "hello", "request_id": "r1", "channel_id": "a" * 32})] + [None] * (width - 1),
            "fn_index": index,
            "session_hash": session_hash,
        }
        response = _predict(client, body)
        r.check("Gradio ran the bridge's event", response.status_code == 200, str(response.status_code))
        return _json.loads(response.json()["data"][0]) if response.status_code == 200 else {}

    hex32 = re.compile(r"^[0-9a-f]{32}$")
    first = hello("page-one")
    r.check("a hello through Gradio is answered with a session for that page",
            bool(hex32.match(str(first.get("bridge_session", "")))), repr(first.get("bridge_session")))
    r.check("and is not the refusal a real install kept getting",
            first.get("code") != "BRIDGE_SESSION_MISMATCH", repr(first.get("code")))
    r.check("the acknowledgement still names the instance and the request",
            first.get("instance_id") == "i" and first.get("request_id") == "r1")
    again = hello("page-one")
    r.check("the same page gets the same session", again.get("bridge_session") == first.get("bridge_session"))
    other = hello("page-two")
    r.check("another page gets another", other.get("bridge_session") != first.get("bridge_session"))

    # And the parent, should it ever meet that refusal again, says so where
    # the checklist reads it instead of counting the answer as silence.
    parent = BROWSER_COPY.read_text(encoding="utf-8")
    r.check("the parent reports a refused hello as a refusal, with its code",
            "answered but refused" in parent and "stopHandshake();" in parent)


def queue_request_checks(r: Results) -> None:
    """Section 28.1: omitted is inherit, null and empty are omitted, ids are
    exact, the prompt is bounded and cleaned, unknown fields go."""
    good = "0123456789abcdef0123456789abcdef"
    other = "fedcba9876543210fedcba9876543210"

    r.check("a request id is 32 lowercase hex", protocol.valid_request_id(good) and not protocol.valid_request_id(good.upper()))
    request, code = protocol.normalize_queue_request({"request_id": good})
    r.check("a request with nothing in it is valid, and means auto", code == "" and request == {"request_id": good, "bridge_session": "", "start": "auto"}, repr(request))
    r.check("and supplies nothing", protocol.queue_overrides(request) == [])
    for label, raw in (("null", None), ("empty", ""), ("whitespace", " \n\t ")):
        request, code = protocol.normalize_queue_request({"request_id": good, "prompt": raw, "start_handoff_id": None if raw is None else "",
                                                          "end_handoff_id": None, "reference_handoff_ids": [] if raw is None else None})
        r.check(f"{label} values normalise to absence", code == "" and "prompt" not in request and "start_handoff_id" not in request
                and "reference_handoff_ids" not in request, repr(request))
    request, code = protocol.normalize_queue_request({"request_id": good, "reference_handoff_ids": []})
    r.check("an empty reference list is absence", code == "" and "reference_handoff_ids" not in request)
    request, code = protocol.normalize_queue_request({"request_id": good, "prompt": "  one\r\ntwo\x00\x1b[31m  ", "bridge_session": "s.1"})
    r.check("the prompt keeps its newlines and loses its control characters", request.get("prompt") == "one\ntwo[31m", repr(request.get("prompt")))
    r.check("a bridge session token is kept", request.get("bridge_session") == "s.1")
    r.check("an emoji sequence survives cleaning", protocol.clean_prompt("a \U0001F469\u200D\U0001F4BB b") == "a \U0001F469\u200D\U0001F4BB b")
    request, code = protocol.normalize_queue_request({"request_id": good, "prompt": "x" * protocol.PROMPT_MAX_CHARS})
    r.check("a prompt at the ceiling is taken", code == "" and len(request["prompt"]) == protocol.PROMPT_MAX_CHARS)
    request, code = protocol.normalize_queue_request({"request_id": good, "prompt": "x" * (protocol.PROMPT_MAX_CHARS + 1)})
    r.check("one over is PROMPT_TOO_LONG", code == "PROMPT_TOO_LONG")
    request, code = protocol.normalize_queue_request({"request_id": good, "prompt": 7})
    r.check("a prompt that is not text is REQUEST_INVALID", code == "REQUEST_INVALID")
    for label, raw in (("no id", {}), ("a short id", {"request_id": good[:-1]}), ("a path as start", {"request_id": good, "start_handoff_id": "../" + good[3:]}),
                       ("a list of paths", {"request_id": good, "reference_handoff_ids": ["/etc/passwd"]}),
                       ("references not a list", {"request_id": good, "reference_handoff_ids": good}),
                       ("too many references", {"request_id": good, "reference_handoff_ids": [good] * (protocol.MAX_QUEUE_REFERENCES + 1)}),
                       ("not an object", [good])):
        _request, code = protocol.normalize_queue_request(raw)
        r.check(f"{label} is REQUEST_INVALID", code == "REQUEST_INVALID", code)
    request, code = protocol.normalize_queue_request({"request_id": good, "start_handoff_id": other, "end_handoff_id": good,
                                                      "reference_handoff_ids": [other, good], "prompt": "p",
                                                      "model": "t2v", "resolution": "1x1", "path": "/tmp/x", "kwargs": {"a": 1}})
    r.check("every override is carried and nothing unlisted is", set(request) == {"request_id", "bridge_session", "prompt", "start_handoff_id",
            "end_handoff_id", "reference_handoff_ids", "start"}, str(sorted(request)))
    r.check("the overrides are named in field order", protocol.queue_overrides(request) == list(protocol.QUEUE_FIELDS))

    first = protocol.queue_payload_hash(protocol.normalize_queue_request({"request_id": good, "prompt": "a"})[0])
    same = protocol.queue_payload_hash(protocol.normalize_queue_request({"request_id": other, "prompt": "a"})[0])
    different = protocol.queue_payload_hash(protocol.normalize_queue_request({"request_id": good, "prompt": "b"})[0])
    r.check("the payload hash ignores the request id and sees the payload", first == same and first != different)
    r.check("and is a hex digest, never the prompt", re.match(r"\A[0-9a-f]{64}\Z", first) is not None and "a" * 3 not in first)

    summary = protocol.queue_summary({"prompt": True, "references": 2}, ["end", "start", "prompt"], [{"field": "start", "code": "RECEIVER_DISABLED"}, {"field": "nope"}])
    r.check("a summary puts each field in exactly one place",
            summary == {"applied": {"prompt": True, "start": False, "end": False, "references": 2},
                        "inherited": ["end"], "ignored": [{"field": "start", "code": "RECEIVER_DISABLED"}]}, repr(summary))
    result = protocol.normalize_queue_result({"ok": True, "admission": "requested", "request_id": good, "applied": {"start": True},
                                              "inherited": ["prompt"], "model": {"label": "M", "type": "m"}})
    r.check("an admitted result normalises", result["ok"] and result["admission"] == "requested" and result["applied"]["start"]
            and result["inherited"] == ["prompt"] and result["model"]["label"] == "M" and result["code"] == "", repr(result))
    r.check("an admission the parent cannot read is a refusal",
            protocol.normalize_queue_result({"ok": True, "admission": "queued"})["ok"] is False
            and protocol.normalize_queue_result({"ok": True, "admission": "queued"})["code"] == "QUEUE_REQUEST_REFUSED")
    r.check("a refusal keeps its code", protocol.normalize_queue_result({"ok": False, "code": "QUEUE_BUSY"})["code"] == "QUEUE_BUSY")
    status = protocol.normalize_queue_status({"ok": True, "status": "queued", "tasks_added": 3, "request_id": good})
    r.check("a status normalises", status == {"ok": True, "request_id": good, "status": "queued", "tasks_added": 3, "code": "", "queue_depth": None, "route": ""}, repr(status))
    r.check("an unreadable status is pending with no tasks, never queued",
            protocol.normalize_queue_status({"ok": True, "status": "done", "tasks_added": 9})["status"] == "pending"
            and protocol.normalize_queue_status({"status": "queued"})["tasks_added"] == 0)
    r.check("the queue messages are whitelisted in their directions",
            protocol.valid_envelope(protocol.envelope(protocol.QUEUE_REQUEST, "c" * 32, good), protocol.TO_BRIDGE)
            and protocol.valid_envelope(protocol.envelope(protocol.QUEUE_STATUS, "c" * 32, good), protocol.TO_PARENT)
            and not protocol.valid_envelope(protocol.envelope(protocol.QUEUE_RESULT, "c" * 32, good), protocol.TO_BRIDGE))
    r.check("a full prompt fits the envelope with room to spare, however it is escaped",
            len(protocol.canonical_json({"prompt": "\u2603" * protocol.PROMPT_MAX_CHARS, "reference_handoff_ids": [good] * protocol.MAX_QUEUE_REFERENCES}))
            * 3 < protocol.MAX_ENVELOPE_BYTES)


# ----------------------------------------------------------- recovery ------

#: The DOM stub the recovery checks drive. It is richer than the flush stub
#: above because the thing under test is a set of distinctions, and each one
#: needs a shape the stub can actually express: an iframe present or absent, a
#: root present or absent, a state box that says one of four views - or that is
#: NOT IN THE PAGE, which is the case that really happens - a hidden Refresh
#: whose presses can be counted, and the browser's own notice.
#:
#: Time is virtual. The recovery budget is twelve seconds of on-screen time and
#: the handshake's schedule is longer again, so a suite on the real clock would
#: spend minutes finding out what it could have known at once.
_RECOVERY_HARNESS = r"""// A Node DOM stub rich enough to express the shapes recovery has to tell
// apart: an iframe that is there or not, a root that is there or not, a state
// box that says one of four views or is missing altogether, a hidden Refresh
// whose presses can be counted, and a notice element that can be looked for.
//
// Time is virtual. The recovery budget is twelve seconds of on-screen time
// and the handshake's schedule is longer still, so a suite on real timers
// would spend minutes waiting to find out what it already knows.
const fs = require("fs");

const SOURCE = fs.readFileSync(process.argv[2], "utf8");
const realSetImmediate = global.setImmediate;
// The page writes a journal line for everything it does, through console.debug
// on its way to the batched log route. Taken here rather than out of the route:
// the route only ever carries the last few lines, and section 25 makes promises
// about lines that a long scenario would have pushed out of that window long
// before it ended. Nothing is printed - the one JSON line is the output.
const realDateNow = Date.now;

function settle() { return new Promise(function (r) { realSetImmediate(r); }); }

function clock() {
    let now = 1600000000000;
    let seq = 0;
    const timers = new Map();
    return {
        now: function () { return now; },
        set: function (fn, ms) {
            seq += 1;
            timers.set(seq, { at: now + Math.max(0, Number(ms) || 0), seq: seq, fn: fn });
            return seq;
        },
        clear: function (id) { timers.delete(id); },
        advance: async function (ms) {
            const until = now + Math.max(0, Number(ms) || 0);
            for (;;) {
                let next = null;
                for (const entry of timers.values()) {
                    if (entry.at > until) { continue; }
                    if (!next || entry.at < next.at || (entry.at === next.at && entry.seq < next.seq)) { next = entry; }
                }
                if (!next) { break; }
                timers.delete(next.seq);
                now = next.at;
                try { next.fn(); } catch (e) { /* the page's business */ }
                await settle();
                await settle();
            }
            now = until;
            await settle();
        }
    };
}

// ------------------------------------------------------------------ nodes --

function node(tag, id) {
    return {
        tagName: tag,
        id: id || "",
        className: "",
        textContent: "",
        value: "",
        dataset: {},
        style: {},
        children: [],
        parentNode: null,
        isConnected: true,
        attributes: {},
        setAttribute: function (name, value) { this.attributes[name] = String(value); },
        getAttribute: function (name) {
            if (name === "src" && this.src !== undefined) { return this.src; }
            return this.attributes[name] === undefined ? null : this.attributes[name];
        },
        addEventListener: function (kind, fn) {
            this.on = this.on || {};
            (this.on[kind] = this.on[kind] || []).push(fn);
        },
        appendChild: function (child) {
            child.parentNode = this;
            mark(child, this.isConnected);
            this.children.push(child);
            return child;
        },
        removeChild: function (child) {
            const at = this.children.indexOf(child);
            if (at !== -1) { this.children.splice(at, 1); }
            child.parentNode = null;
            mark(child, false);
            return child;
        },
        querySelector: function (selector) { return search(this.children, selector); },
        closest: function () { return null; }
    };
}

function mark(element, connected) {
    element.isConnected = !!connected;
    for (const child of element.children) { mark(child, connected); }
}

function matches(element, selector) {
    const parts = String(selector).trim().split(/\s+/);
    if (parts.length > 1) { return false; }
    const one = parts[0];
    if (one.charAt(0) === "#") { return element.id === one.slice(1); }
    if (one.charAt(0) === ".") { return String(element.className).split(/\s+/).indexOf(one.slice(1)) !== -1; }
    return element.tagName.toLowerCase() === one.toLowerCase();
}

/** querySelector over the fake tree. Supports "tag", "#id", ".class" and the
 * one descendant form this file uses, "#id tag". */
function search(roots, selector) {
    const parts = String(selector).trim().split(/\s+/);
    if (parts.length === 2) {
        const host = search(roots, parts[0]);
        return host ? search(host.children, parts[1]) : null;
    }
    for (const element of roots) {
        if (matches(element, selector)) { return element; }
        const deeper = search(element.children, selector);
        if (deeper) { return deeper; }
    }
    return null;
}

function walk(roots, fn) {
    for (const element of roots) { fn(element); walk(element.children, fn); }
}

// ------------------------------------------------------------------ world --

function world(options) {
    options = options || {};
    const tick = clock();
    const listeners = {};
    const watchers = [];
    const screens = [];
    const seen = { presses: 0, hellos: 0, frames: 0 };

    function boxNode(id, value) {
        const host = node("DIV", id);
        const area = node("TEXTAREA", "");
        area.value = value === undefined ? "" : value;
        host.appendChild(area);
        return host;
    }

    function iframe(name) {
        const frame = node("IFRAME", "wangp_iframe");
        frame.src = "/wan2gp/";
        frame.name = name || "one";
        frame.speaks = true;
        frame.contentWindow = {
            postMessage: function (message) {
                posted.push(message);
            bound = message.channel_id || bound;
            if (message.type !== "WANGP_BRIDGE_HELLO") { return; }
                seen.hellos += 1;
                if (!frame.speaks) { return; }
                tick.set(function () {
                    deliver(frame, "WANGP_BRIDGE_READY", message.request_id, {
                        bridge_session: frame.session || "abcdef0123456789abcdef0123456789",
                        instance_id: "inst-1", version: "1.6.4", ready: true, receivers: [],
                        state_revision: "r1", capabilities: { queue: true, start: true, track: true }
                    }, message.channel_id);
                }, 0);
            }
        };
        return frame;
    }

    const page = node("DIV", "wangp_page");
    let root = node("DIV", "wangp_iframe_root");
    let restoreAtLookup = 0;
    //: The channel the page is actually speaking on. A reply sent on any
    //: other one is dropped before anything else is looked at, which would
    //: make every delivery below a silent no-op.
    let bound = "";
    const lines = [];
    const posted = [];
    const refresh = node("DIV", "wangp_refresh_request");
    const button = node("BUTTON", "");
    button.click = function () { seen.presses += 1; };
    refresh.appendChild(button);

    const stateBox = boxNode("wangp_state", options.state === undefined
        ? JSON.stringify({ view: "iframe", state: "READY" }) : options.state);
    const channelBox = boxNode("wangp_channel", "");
    const checkBox = boxNode("wangp_browser_check", "");
    const sessionBox = boxNode("wangp_session", "");
    const logBox = boxNode("wangp_client_log", "");

    page.appendChild(channelBox);
    if (options.state !== null) { page.appendChild(stateBox); }
    page.appendChild(checkBox);
    page.appendChild(sessionBox);
    page.appendChild(logBox);
    if (options.refresh !== false) { page.appendChild(refresh); }
    if (options.root !== false) { page.appendChild(root); }

    let frame = null;

    function addFrame(name) {
        const built = iframe(name || ("n" + (seen.frames + 1)));
        root.appendChild(built);
        frame = built;
        seen.frames += 1;
        return built;
    }

    if (options.iframe !== false) { addFrame("one"); }

    function fire(kind, event) { for (const fn of (listeners[kind] || []).slice()) { fn(event); } }

    function deliver(from, type, requestId, payload, channelId) {
        fire("message", {
            origin: "http://forge.test",
            source: from.contentWindow,
            data: { protocol: 5, type: type, channel_id: channelId || bound, request_id: requestId, payload: payload }
        });
    }

    const doc = {
        readyState: "complete",
        visibilityState: "visible",
        addEventListener: function (kind, fn) { (listeners[kind] = listeners[kind] || []).push(fn); },
        getElementById: function (id) {
            let found = null;
            walk([page], function (element) { if (!found && element.id === id) { found = element; } });
            // The one thing a synchronous block cannot be tested without: a
            // frame that turns up BETWEEN two lookups. Counted down here so a
            // scenario can put it back exactly in the gap between the grace's
            // question and the press's.
            if (id === "wangp_iframe" && restoreAtLookup > 0) {
                restoreAtLookup -= 1;
                if (restoreAtLookup === 0) { addFrame("race"); }
            }
            return found;
        },
        querySelector: function (selector) { return search([page], selector); },
        querySelectorAll: function () { return []; },
        createElement: function (tag) { return node(String(tag).toUpperCase(), ""); },
        documentElement: { classList: { contains: function () { return false; } }, style: {}, getAttribute: function () { return null; } },
        body: { classList: { contains: function () { return false; } } },
        head: { appendChild: function () {} }
    };

    const win = {
        location: { href: "http://forge.test/", origin: "http://forge.test" },
        addEventListener: function (kind, fn) { (listeners[kind] = listeners[kind] || []).push(fn); },
        document: doc,
        matchMedia: function () { return { matches: false, addEventListener: function () {} }; },
        MutationObserver: function (fn) {
            this.fn = fn; this.dead = false; this.target = null;
            this.observe = function (target) { this.target = target; };
            this.disconnect = function () { this.dead = true; };
            watchers.push(this);
        },
        IntersectionObserver: function (fn) {
            this.fn = fn; this.dead = false;
            this.observe = function () {};
            this.disconnect = function () { this.dead = true; };
            screens.push(this);
        },
        requestAnimationFrame: function (fn) { return tick.set(fn, 0); },
        crypto: {
            getRandomValues: function (bytes) {
                for (let i = 0; i < bytes.length; i++) { bytes[i] = (world.entropy = (world.entropy || 0) + 7) % 256; }
                return bytes;
            }
        }
    };

    return {
        tick: tick, doc: doc, win: win, page: page, root: root, refresh: refresh,
        stateBox: stateBox, seen: seen, watchers: watchers, screens: screens,
        frame: function () { return frame; },
        iframe: iframe,
        fire: fire,
        deliver: deliver,
        /** Deliver a mutation batch to every live observer, exactly as a
         * browser would: only observers still bound to a connected node. */
        mutate: function (records) {
            for (const watcher of watchers.slice()) {
                if (watcher.dead || !watcher.target || !watcher.target.isConnected) { continue; }
                try { watcher.fn(records || []); } catch (e) { /* contained, as in a browser */ }
            }
        },
        removeFrame: function () {
            if (frame && frame.parentNode) { frame.parentNode.removeChild(frame); }
            frame = null;
        },
        addFrame: addFrame,
        restoreAtLookup: function (count) { restoreAtLookup = count; },
        posted: posted,
        lines: lines,
        said: function (fragment) { return lines.some(function (line) { return line.indexOf(fragment) !== -1; }); },
        note: function (line) { lines.push(line); },
        rootNode: function () { return root; },
        setState: function (value) {
            const area = search([page], "#wangp_state textarea");
            if (area) { area.value = value; }
        },
        dropState: function () { if (stateBox.parentNode) { stateBox.parentNode.removeChild(stateBox); } },
        restoreState: function (value) {
            const area = search([stateBox], "textarea");
            if (area) { area.value = value; }
            if (!stateBox.parentNode) { page.appendChild(stateBox); }
        },
        dropRoot: function () { if (root.parentNode) { root.parentNode.removeChild(root); } frame = null; },
        restoreRoot: function () { if (!root.parentNode) { page.appendChild(root); } },
        replaceRoot: function () {
            if (root.parentNode) { root.parentNode.removeChild(root); }
            root = node("DIV", "wangp_iframe_root");
            frame = null;
            page.appendChild(root);
            return root;
        },
        notice: function () { return search([page], "#minipaint-wangp-recovery-notice"); },
        hidden: function (yes) {
            doc.visibilityState = yes ? "hidden" : "visible";
            fire("visibilitychange", {});
        }
    };
}

async function load(w) {
    global.window = w.win;
    global.document = w.doc;
    global.setTimeout = function (fn, ms) { return w.tick.set(fn, ms); };
    global.clearTimeout = function (id) { w.tick.clear(id); };
    global.setInterval = function () { return 0; };
    global.clearInterval = function () {};
    global.fetch = function () {
        return Promise.resolve({ ok: true, status: 204, type: "basic", json: () => Promise.resolve({}), text: () => Promise.resolve("") });
    };
    // Bare `new MutationObserver(...)` resolves to the global, not to
    // window's, so the page's own observer is never built without this.
    global.MutationObserver = w.win.MutationObserver;
    global.IntersectionObserver = w.win.IntersectionObserver;
    console.debug = function (prefix, message) { w.note(String(message === undefined ? prefix : message)); };
    Date.now = w.tick.now;
    w.win.setTimeout = global.setTimeout;
    w.win.clearTimeout = global.clearTimeout;
    w.win.fetch = global.fetch;
    new Function("window", "document", "fetch", SOURCE)(w.win, w.doc, global.fetch);
    await w.tick.advance(0);
    return w.win.minipaintWanGP;
}

function restore() { Date.now = realDateNow; }

module.exports = { world: world, load: load, restore: restore, settle: settle,
                   realSetTimeout: global.setTimeout, realClearTimeout: global.clearTimeout };
"""


#: One scenario per behaviour the design promises, run in one Node process
#: against a fresh page each time. Each returns a small record; the assertions
#: are in Python, next to the sentence of the design they belong to.
_RECOVERY_SCENARIOS = r"""const H = require("./harness.js");

const S = {};
const VIEW_IFRAME = JSON.stringify({ view: "iframe", state: "READY" });
const VIEW_ERROR = JSON.stringify({ view: "error", state: "ERROR" });
const VIEW_SETUP = JSON.stringify({ view: "setup", state: "SETUP_REQUIRED" });
const VIEW_DEGRADED = JSON.stringify({ view: "iframe", state: "DEGRADED", degraded: true });

async function healthy(options) {
    const w = H.world(options || {});
    const api = await H.load(w);
    await w.tick.advance(400);
    return { w: w, api: api };
}

async function snap(w, api, extra) {
    // The journal is written in batches on a timer, and these scenarios end
    // long before one would fire. This is the page's own "put it on the wire
    // now, and wait until it is there", which is exactly what it exists for.
    await api.reportFrames();
    const state = api.state();
    return Object.assign({
        presses: w.seen.presses,
        notice: !!w.notice(),
        ready: state.ready,
        attached: state.attached,
        recovery: state.recovery,
        queued: w.posted.filter(function (m) { return m.type === "WANGP_QUEUE_REQUEST"; }).length
    }, extra || {});
}

// 27.1 -- an iframe that goes while the tab is idle and still wants one.
S["idle removal"] = async function () {
    const { w, api } = await healthy();
    w.removeFrame();
    w.mutate([]);
    const atOnce = w.seen.presses;
    await w.tick.advance(150);
    const afterGrace = w.seen.presses;
    w.addFrame("two");
    w.mutate([]);
    await w.tick.advance(400);
    return await snap(w, api, { atOnce: atOnce, afterGrace: afterGrace, saidRepaint: w.said("recovery: repaint requested") });
};

// 27.2 -- the tab changed view on purpose, so the absence is not a fault.
S["view changed"] = async function () {
    const { w, api } = await healthy();
    w.setState(VIEW_ERROR);
    w.removeFrame();
    w.mutate([]);
    await w.tick.advance(600);
    return await snap(w, api, { saidNoAction: w.said("does not expect one; no action") });
};

// 3.4 -- a DEGRADED but serving tab is still an iframe tab, and its frame is
// repaired like any other. Keying off the state field would refuse this.
S["degraded still repairs"] = async function () {
    const { w, api } = await healthy();
    w.setState(VIEW_DEGRADED);
    w.removeFrame();
    w.mutate([]);
    await w.tick.advance(150);
    return await snap(w, api);
};

// 27.3 -- an ordinary Gradio replacement, landing inside the grace.
S["replacement in the grace"] = async function () {
    const { w, api } = await healthy();
    w.removeFrame();
    w.mutate([]);
    await w.tick.advance(50);
    w.addFrame("two");
    await w.tick.advance(500);
    return await snap(w, api, { saidGrace: w.said("replacement appeared during grace") });
};

// 27.19 -- the frame comes back between the grace's question and the press.
S["frame returns at the last instant"] = async function () {
    const { w, api } = await healthy();
    w.removeFrame();
    w.mutate([]);
    // Absent when the grace asks, back before the press asks: two lookups.
    w.restoreAtLookup(2);
    await w.tick.advance(150);
    await w.tick.advance(400);
    return await snap(w, api, { saidSkipped: w.said("skipped the press; the iframe came back first") });
};

// 27.5 -- the repaint is slow. One press, and the budget waits.
S["repaint is slow"] = async function () {
    const { w, api } = await healthy();
    w.removeFrame();
    w.mutate([]);
    await w.tick.advance(150);
    const pressed = w.seen.presses;
    await w.tick.advance(8000);
    const stillOne = w.seen.presses;
    w.addFrame("two");
    w.mutate([]);
    await w.tick.advance(400);
    return await snap(w, api, { pressed: pressed, stillOne: stillOne });
};

// 27.7 / 27.17 -- the press lands nowhere. The state still says iframe.
S["repaint never arrives"] = async function () {
    const { w, api } = await healthy();
    w.removeFrame();
    w.mutate([]);
    await w.tick.advance(150);
    const pressed = w.seen.presses;
    await w.tick.advance(13000);
    return await snap(w, api, {
        pressed: pressed,
        notices: w.rootNode().children.filter(function (c) { return c.id === "minipaint-wangp-recovery-notice"; }).length,
        text: (w.notice() || {}).textContent || "",
        saidFailed: w.said("iframe did not return within 12000 ms on screen")
    });
};

// 27.6 -- backgrounded while the repair is waiting. Hidden time is not spent.
S["hidden while repairing"] = async function () {
    const { w, api } = await healthy();
    w.removeFrame();
    w.mutate([]);
    await w.tick.advance(150);
    w.hidden(true);
    await w.tick.advance(120000);
    const whileAway = !!w.notice();
    w.hidden(false);
    await w.tick.advance(200);
    const onReturn = !!w.notice();
    w.addFrame("two");
    w.mutate([]);
    await w.tick.advance(400);
    return await snap(w, api, { whileAway: whileAway, onReturn: onReturn });
};

// 27.8 -- a stale S.frame with a perfectly good iframe in the page.
S["stale frame reference"] = async function () {
    const { w, api } = await healthy();
    w.removeFrame();
    w.addFrame("two");
    api.attach(null);
    await w.tick.advance(400);
    return await snap(w, api);
};

// 27.9 / 27.12 -- a queue request in flight when the frame is replaced.
S["replaced under a queue request"] = async function () {
    const { w, api } = await healthy();
    const pending = api.queueAndConfirm({ prompt: "a cat" });
    await w.tick.advance(10);
    w.removeFrame();
    w.addFrame("two");
    w.mutate([]);
    await w.tick.advance(400);
    const result = await pending;
    return await snap(w, api, {
        status: result.status, code: result.code, ok: result.ok,
        saidUnconfirmed: w.said("acknowledgement lost; outcome remains unconfirmed")
    });
};

// 27.12 -- the same, by removal rather than replacement. THIS IS THE ONE THAT
// MUST FAIL AGAINST UNPATCHED HEAD: it read "refused" there.
S["queue request loses its answer"] = async function () {
    const { w, api } = await healthy();
    const pending = api.queueAndConfirm({ prompt: "a cat" });
    await w.tick.advance(10);
    w.removeFrame();
    w.mutate([]);
    await w.tick.advance(50);
    const result = await pending;
    return await snap(w, api, { status: result.status, code: result.code, ok: result.ok });
};

// 14.6 -- a non-mutating query keeps today's behaviour exactly.
S["a query keeps its refusal"] = async function () {
    const { w, api } = await healthy();
    const pending = api.receivers();
    await w.tick.advance(10);
    w.removeFrame();
    w.mutate([]);
    await w.tick.advance(50);
    const result = await pending;
    return await snap(w, api, { code: result.code, unconfirmed: !!result.unconfirmed });
};

// 27.10 -- a retired iframe speaking after a new one was bound.
S["late reply from the old frame"] = async function () {
    const { w, api } = await healthy();
    const old = w.frame();
    w.removeFrame();
    const two = w.addFrame("two");
    two.session = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
    w.mutate([]);
    await w.tick.advance(400);
    const bound = api.state().bridge_session;
    w.deliver(old, "WANGP_BRIDGE_READY", "cccccccccccccccccccccccccccccccc", {
        bridge_session: "cccccccccccccccccccccccccccccccc", instance_id: "inst-1",
        version: "1.6.4", ready: true, receivers: [], state_revision: "r1"
    });
    await w.tick.advance(50);
    return await snap(w, api, { bound: bound, after: api.state().bridge_session });
};

// 27.11 -- a session change while the iframe is present presses nothing.
S["session mismatch"] = async function () {
    const { w, api } = await healthy();
    w.deliver(w.frame(), "WANGP_BRIDGE_READY", "dddddddddddddddddddddddddddddddd", {
        bridge_session: "cccccccccccccccccccccccccccccccc", instance_id: "inst-1",
        version: "1.6.4", ready: true, receivers: [], state_revision: "r1"
    });
    await w.tick.advance(100);
    return await snap(w, api, { session: api.state().bridge_session });
};

// 27.13 -- a tab put aside and brought back with nothing wrong.
S["hidden and back, healthy"] = async function () {
    const { w, api } = await healthy();
    w.hidden(true);
    await w.tick.advance(120000);
    w.hidden(false);
    await w.tick.advance(2000);
    return await snap(w, api, { hellos: w.seen.hellos });
};

// 27.14 -- a tab brought back to find the iframe gone.
S["hidden and back, frame gone"] = async function () {
    const { w, api } = await healthy();
    w.hidden(true);
    w.removeFrame();
    await w.tick.advance(60000);
    w.hidden(false);
    await w.tick.advance(200);
    const pressed = w.seen.presses;
    w.addFrame("two");
    w.mutate([]);
    await w.tick.advance(400);
    return await snap(w, api, { pressed: pressed, saidResumed: w.said("page resumed; iframe missing, repairing") });
};

// 27.15 -- the root node itself replaced, which the observer cannot see.
S["root node replaced"] = async function () {
    const { w, api } = await healthy();
    w.replaceRoot();
    w.mutate([]);
    const blind = w.seen.presses;
    w.hidden(true);
    w.hidden(false);
    await w.tick.advance(200);
    const afterRebind = w.seen.presses;
    w.addFrame("two");
    w.mutate([]);
    await w.tick.advance(400);
    const readyAgain = api.state().ready;
    w.removeFrame();
    w.mutate([]);
    await w.tick.advance(150);
    return await snap(w, api, { blind: blind, afterRebind: afterRebind, readyAgain: readyAgain,
                          saidRebind: w.said("root was replaced; rebinding the observer") });
};

// 27.16 -- WanGP is genuinely not serving. The repaint LANDS and says so.
S["wangp is not serving"] = async function () {
    const { w, api } = await healthy();
    w.removeFrame();
    w.mutate([]);
    await w.tick.advance(150);
    const pressed = w.seen.presses;
    w.setState(VIEW_ERROR);
    await w.tick.advance(13000);
    return await snap(w, api, { pressed: pressed, saidAnswered: w.said("repaint answered view=error") });
};

// 27.18 -- a burst of mutations is one cycle and one press.
S["mutation burst"] = async function () {
    const { w, api } = await healthy();
    w.removeFrame();
    for (let i = 0; i < 6; i++) { w.mutate([]); }
    await w.tick.advance(150);
    for (let i = 0; i < 6; i++) { w.mutate([]); }
    await w.tick.advance(150);
    return await snap(w, api);
};

// 16.3 -- the notice this file writes must not retrigger the observer that
// would write it again.
S["the notice does not retrigger"] = async function () {
    const { w, api } = await healthy();
    w.removeFrame();
    w.mutate([]);
    await w.tick.advance(150);
    await w.tick.advance(13000);
    const afterWarning = w.seen.presses;
    const notice = w.notice();
    w.mutate([{ addedNodes: [notice], removedNodes: [] }]);
    await w.tick.advance(400);
    return await snap(w, api, { afterWarning: afterWarning });
};

// 27.20 -- UNKNOWN before any press, with the root still there. The element is
// ABSENT, which is the cause that actually occurs; the box is born populated.
S["unknown before the press"] = async function () {
    const { w, api } = await healthy();
    w.dropState();
    w.removeFrame();
    w.mutate([]);
    await w.tick.advance(200);
    return await snap(w, api, { saidUnknown: w.said("state unknown after grace; refresh not pressed") });
};

// 27.21 -- UNKNOWN that becomes IFRAME inside the grace may be pressed.
S["unknown becomes iframe"] = async function () {
    const { w, api } = await healthy();
    w.dropState();
    w.removeFrame();
    w.mutate([]);
    await w.tick.advance(50);
    w.restoreState(VIEW_IFRAME);
    await w.tick.advance(150);
    return await snap(w, api);
};

// 27.22 / 27.25 -- UNKNOWN that becomes NON_IFRAME presses nothing, and takes
// a notice an earlier episode left behind away with it.
S["unknown becomes non-iframe"] = async function () {
    const { w, api } = await healthy();
    w.dropState();
    w.removeFrame();
    w.mutate([]);
    await w.tick.advance(200);
    const warned = !!w.notice();
    w.restoreState(VIEW_SETUP);
    w.mutate([]);
    await w.tick.advance(200);
    return await snap(w, api, { warned: warned, saidCleared: w.said("recovery: warning cleared") });
};

// 27.23 -- the state goes missing AFTER a press. Unprovable, so warned.
S["unknown after the press"] = async function () {
    const { w, api } = await healthy();
    w.removeFrame();
    w.mutate([]);
    await w.tick.advance(150);
    const pressed = w.seen.presses;
    w.dropState();
    await w.tick.advance(13000);
    return await snap(w, api, { pressed: pressed, saidUnprovable: w.said("repaint outcome unknown") });
};

// 27.24 -- a later handshake takes the notice away, with no Gradio round trip
// having replaced anything.
S["warning clears on a later handshake"] = async function () {
    const { w, api } = await healthy();
    w.removeFrame();
    w.mutate([]);
    await w.tick.advance(150);
    await w.tick.advance(13000);
    const warned = !!w.notice();
    w.addFrame("two");
    w.mutate([]);
    await w.tick.advance(400);
    return await snap(w, api, { warned: warned });
};

// 27.26 -- UNKNOWN with the root gone as well. No surface, so no notice: this
// is the observer-rebind case, not a recovery failure.
S["unknown with no root"] = async function () {
    const { w, api } = await healthy();
    w.hidden(true);
    w.dropState();
    w.dropRoot();
    w.hidden(false);
    await w.tick.advance(300);
    const mid = await snap(w, api, {});
    // The tab is drawn again. The bounded ladder finds it and binds, and a
    // later removal is detected normally.
    w.restoreState(VIEW_IFRAME);
    w.restoreRoot();
    await w.tick.advance(3000);
    w.addFrame("two");
    w.mutate([]);
    await w.tick.advance(400);
    const readyAgain = api.state().ready;
    w.removeFrame();
    w.mutate([]);
    await w.tick.advance(150);
    return await snap(w, api, {
        midPresses: mid.presses, midNotice: mid.notice, midWarningShown: mid.recovery.warning_shown,
        readyAgain: readyAgain, saidNoSurface: w.said("state unknown and the root is gone")
    });
};

// 27.4 (browser half) / acceptance 18 -- recovery never submits anything.
S["recovery submits nothing"] = async function () {
    const { w, api } = await healthy();
    const before = w.posted.filter(function (m) { return m.type === "WANGP_QUEUE_REQUEST"; }).length;
    w.removeFrame();
    w.mutate([]);
    await w.tick.advance(150);
    w.addFrame("two");
    w.mutate([]);
    await w.tick.advance(400);
    return await snap(w, api, { before: before });
};

// The refresh control itself missing: no press, and no pretence of one.
S["no refresh control"] = async function () {
    const { w, api } = await healthy({ refresh: false });
    w.removeFrame();
    w.mutate([]);
    await w.tick.advance(200);
    return await snap(w, api, { saidNoControl: w.said("Refresh control is not in the page") });
};

// An unrecognised view from a build newer than this file reads as UNKNOWN.
S["a fifth view"] = async function () {
    const { w, api } = await healthy();
    w.setState(JSON.stringify({ view: "hologram", state: "READY" }));
    w.removeFrame();
    w.mutate([]);
    await w.tick.advance(200);
    return await snap(w, api);
};

// Malformed JSON reads as UNKNOWN too, and presses nothing.
S["malformed state"] = async function () {
    const { w, api } = await healthy();
    w.setState("{not json");
    w.removeFrame();
    w.mutate([]);
    await w.tick.advance(200);
    return await snap(w, api);
};

// ------------------------------------------------------------------------
// What the server says WanGP is doing arrives in a snapshot - the page holds
// no live connection for it to arrive over any other way.
// ------------------------------------------------------------------------
const RUNTIME = function (running, state) {
    return { detail: { kind: "synced", runtime: { running: running, state: state || (running ? "READY" : "STOPPED") } } };
};
function frameSrc(w) { const f = w.doc.getElementById("wangp_iframe"); return f ? String(f.src) : null; }

// Hidden, then frozen while hidden - the order a browser nearly always does
// it in - then thawed while still hidden, then shown. Each is its own fact.
S["frozen while hidden"] = async function () {
    const { w, api } = await healthy();
    w.hidden(true);
    await w.tick.advance(1000);
    w.fire("freeze", {});
    await w.tick.advance(600000);
    w.fire("resume", {});
    const backWhileHidden = w.said("back on screen");
    await w.tick.advance(1000);
    w.hidden(false);
    await w.tick.advance(100);
    return await snap(w, api, { saidFrozen: w.said("the browser had frozen this page for 600s"),
                                backWhileHidden: backWhileHidden, saidBack: w.said("back on screen after") });
};

// Discarded in the background and loaded fresh on return.
S["a discarded page says so"] = async function () {
    const w = H.world({});
    w.doc.wasDiscarded = true;
    const api = await H.load(w);
    await w.tick.advance(400);
    return await snap(w, api, { saidDiscarded: w.said("the browser had discarded this page") });
};
S["a page loaded normally says nothing of a discard"] = async function () {
    const { w, api } = await healthy();
    return await snap(w, api, { saidDiscarded: w.said("the browser had discarded this page") });
};

// ------------------------------------------------------------------------
// The tab painted when Forge started, and the runtime frames that correct it.
// ------------------------------------------------------------------------
S["boot with a stale card"] = async function () {
    const w = H.world({ iframe: false, state: VIEW_SETUP });
    const api = await H.load(w);
    await w.tick.advance(400);
    return await snap(w, api, { saidBoot: w.said("boot: the tab was painted when Forge started") });
};

S["boot without a refresh control"] = async function () {
    const w = H.world({ iframe: false, state: VIEW_SETUP, refresh: false });
    const api = await H.load(w);
    await w.tick.advance(400);
    return await snap(w, api);
};

S["boot with an iframe presses nothing"] = async function () {
    const { w, api } = await healthy();
    return await snap(w, api, { saidBoot: w.said("boot:") });
};

S["runtime says serving, tab shows a card"] = async function () {
    const { w, api } = await healthy();
    w.setState(VIEW_ERROR);
    w.removeFrame();
    w.mutate([]);
    await w.tick.advance(200);
    const before = w.seen.presses;
    w.fire("minipaint:outbox", RUNTIME(true));
    await w.tick.advance(3100);
    const pressed = w.seen.presses - before;
    // The press lands: the server paints the iframe view back.
    w.setState(VIEW_IFRAME);
    w.addFrame("two");
    w.mutate([]);
    await w.tick.advance(400);
    return await snap(w, api, { pressed: pressed, saidRuntime: w.said("runtime: WanGP is serving and the tab shows no iframe") });
};

S["runtime says stopped, tab shows an iframe"] = async function () {
    const { w, api } = await healthy();
    const before = w.seen.presses;
    w.fire("minipaint:outbox", RUNTIME(false, "CRASHED"));
    await w.tick.advance(3100);
    const pressed = w.seen.presses - before;
    // The press lands: the server paints the error card.
    w.setState(VIEW_ERROR);
    w.removeFrame();
    w.mutate([]);
    await w.tick.advance(600);
    return await snap(w, api, { pressed: pressed, saidRuntime: w.said("WanGP is not serving (CRASHED)") });
};

S["runtime frames coalesce"] = async function () {
    const { w, api } = await healthy();
    w.setState(VIEW_ERROR);
    w.removeFrame();
    w.mutate([]);
    await w.tick.advance(200);
    const before = w.seen.presses;
    for (let i = 0; i < 6; i += 1) { w.fire("minipaint:outbox", RUNTIME(true)); await w.tick.advance(40); }
    await w.tick.advance(3500);
    return await snap(w, api, { pressed: w.seen.presses - before });
};

S["a runtime frame that agrees presses nothing"] = async function () {
    const { w, api } = await healthy();
    w.fire("minipaint:outbox", RUNTIME(true));
    await w.tick.advance(3500);
    return await snap(w, api);
};

S["a snapshot carries the same truth"] = async function () {
    const { w, api } = await healthy();
    w.setState(VIEW_ERROR);
    w.removeFrame();
    w.mutate([]);
    await w.tick.advance(200);
    const before = w.seen.presses;
    w.fire("minipaint:outbox", { detail: { kind: "synced", runtime: { running: true, state: "READY" } } });
    await w.tick.advance(3500);
    return await snap(w, api, { pressed: w.seen.presses - before });
};

// Gradio paints the iframe on its own before the press falls due.
S["a tab that caught up is left alone"] = async function () {
    const { w, api } = await healthy();
    w.setState(VIEW_ERROR);
    w.removeFrame();
    w.mutate([]);
    await w.tick.advance(200);
    const before = w.seen.presses;
    w.fire("minipaint:outbox", RUNTIME(true));
    await w.tick.advance(100);
    w.setState(VIEW_IFRAME);
    w.addFrame("two");
    w.mutate([]);
    await w.tick.advance(3500);
    return await snap(w, api, { pressed: w.seen.presses - before });
};


/** A scenario that never finishes is a result, not a hang: a build whose
 * promises are left unsettled is exactly what several of these check for, and
 * it must be reported rather than quietly ending the process. Measured on the
 * real clock, which the scenario's virtual one has no say over. */
function bounded(name, run) {
    let timer = 0;
    return Promise.race([
        Promise.resolve().then(run).then(
            function (value) { H.realClearTimeout(timer); return value; },
            function (error) { H.realClearTimeout(timer); return { error: String((error && error.stack) || error).slice(0, 400) }; }
        ),
        new Promise(function (resolve) {
            timer = H.realSetTimeout(function () { resolve({ error: "did not finish: " + name }); }, 5000);
        })
    ]);
}

async function main() {
    const out = {};
    for (const name of Object.keys(S)) {
        out[name] = await bounded(name, S[name]);
        H.restore();
    }
    console.log(JSON.stringify(out));
    process.exit(0);
}

main().catch(function (e) { console.error("ERR", (e && e.stack) || e); process.exit(1); });
"""


#: Every mutation below reverts one decision and names the check that must then
#: fail. This is the project's convention written down as code rather than as a
#: promise: a check that does not fail when its behaviour is taken away is not
#: checking anything, and these are cheap enough - the whole set runs in under a
#: second - that there is no reason to take that on trust.
#:
#: An anchor that no longer matches is itself a failure. It means the code moved
#: and nobody came back to ask whether the check still bites.
_RECOVERY_MUTATIONS = (
    (
        "the observer only ever handled arrival",
        """            const frame = frameElement();
            if (frame) {
                if (frame !== S.frame) { attachAndSettle(); }
                return;
            }""",
        """            const frame = frameElement();
            if (frame) {
                if (frame !== S.frame) { attachAndSettle(); }
            }
            return;""",
        "idle removal", "presses", 1, 0,
    ),
    (
        "a browser give-up settles a mutating request as a refusal",
        "if (giveUp && entry && MUTATING_REPLIES.indexOf(entry.type) !== -1) {",
        "if (false && giveUp && entry && MUTATING_REPLIES.indexOf(entry.type) !== -1) {",
        "queue request loses its answer", "status", "unconfirmed", "refused",
    ),
    (
        "an unreadable state authorises the press",
        "const pressed = !frame && view === WAN_VIEW_IFRAME && pressHidden(REFRESH_ELEM_ID);",
        "const pressed = !frame && view !== WAN_VIEW_NON_IFRAME && pressHidden(REFRESH_ELEM_ID);",
        "unknown before the press", "presses", 0, 1,
    ),
    (
        "the press does not re-check for a live frame",
        "const pressed = !frame && view === WAN_VIEW_IFRAME && pressHidden(REFRESH_ELEM_ID);",
        "const pressed = view === WAN_VIEW_IFRAME && pressHidden(REFRESH_ELEM_ID);",
        "frame returns at the last instant", "presses", 0, 1,
    ),
    (
        "there is no grace before the press",
        "S.recovery.graceTimer = setTimeout(afterAbsenceGrace, ABSENCE_GRACE_MS);",
        "afterAbsenceGrace();",
        "replacement in the grace", "presses", 0, 1,
    ),
    (
        "the state field is read instead of the view field",
        'view = state && typeof state.view === "string" ? state.view : "";',
        'view = state && typeof state.state === "string" ? state.state : "";',
        "degraded still repairs", "presses", 1, 0,
    ),
    (
        "repair is not single-flight",
        "        if (S.recovery.active) { return false; }\n        S.recovery.active = true;",
        "        S.recovery.active = true;",
        "mutation burst", "presses", 1, 12,
    ),
    (
        "the root observer is never rebound",
        "if (!S.watcherRoot || S.watcherRoot.isConnected) { return false; }",
        "if (!S.watcherRoot || true) { return false; }",
        "root node replaced", "readyAgain", True, False,
    ),
    (
        "the notice waits for Gradio to remove it",
        "    function clearRecoveryWarning() {\n        const notice = noticeElement();",
        "    function clearRecoveryWarning() {\n        if (true) { return; }\n        const notice = noticeElement();",
        "warning clears on a later handshake", "notice", False, True,
    ),
    # -- the lifecycle journal --
    (
        "a freeze after hiding is dropped",
        "                if (!frozenSince) { frozenSince = Date.now(); }\n",
        "",
        "frozen while hidden", "saidFrozen", True, False,
    ),
    (
        "a thaw is called being back on screen",
        'if (document.visibilityState !== "hidden") { back("resumed"); }',
        'back("resumed");',
        "frozen while hidden", "backWhileHidden", False, True,
    ),
    (
        "a discard goes unsaid",
        "            if (document.wasDiscarded) {",
        "            if (false) {",
        "a discarded page says so", "saidDiscarded", True, False,
    ),
    # -- boot and runtime frames --
    (
        "the boot repaint reloads an iframe that is showing",
        "if (!frameElement() && pressHidden(REFRESH_ELEM_ID)) {",
        "if (pressHidden(REFRESH_ELEM_ID)) {",
        "boot with an iframe presses nothing", "presses", 0, 1,
    ),
    (
        "runtime frames are not coalesced",
        "        if (S.runtimePress.timer) { return; }\n        const wait = ",
        "        const wait = ",
        "runtime frames coalesce", "pressed", 1, 6,
    ),
    (
        "the press is not judged again against the tab as it is then",
        "if (S.runtimePress.running === null || S.runtimePress.running === !!frameElement()) { return; }",
        "if (S.runtimePress.running === null) { return; }",
        "a tab that caught up is left alone", "pressed", 0, 1,
    ),
)

#: The one mutation that takes two edits: warning on an unreadable state
#: whether or not there is anything to render the warning into, AND counting
#: that no-op as a warning shown. Together they are the implementation 27.26
#: exists to rule out - the one that suppresses the next real warning.
_RECOVERY_WARN_ANYWAY = (
    (
        '            if (!rootElement()) {\n'
        '                say("recovery: state unknown and the root is gone; rebinding the observer");',
        '            if (false && !rootElement()) {\n'
        '                say("recovery: state unknown and the root is gone; rebinding the observer");',
    ),
    (
        '        if (!root || typeof root.appendChild !== "function") { return false; }',
        '        if (!root || typeof root.appendChild !== "function") { S.recovery.warningShown = true; return false; }',
    ),
)


def _run_recovery(node, scratch, source_path):
    """Every scenario against one copy of the browser file, as a dict."""
    import json as _json
    import subprocess

    run = subprocess.run([node, str(scratch / "scenarios.js"), str(source_path)],
                         capture_output=True, text=True, timeout=180, check=False, cwd=str(scratch))
    lines = run.stdout.strip().splitlines()
    if not lines:
        return {"__error__": (run.stderr or "no output")[-400:]}
    try:
        return _json.loads(lines[-1])
    except ValueError:
        return {"__error__": lines[-1][-400:]}


def recovery_checks(r: Results) -> None:
    """In-place repair of a WanGP iframe that went away on its own.

    WHAT THIS EXISTS TO CATCH.

    The failure is not the one it looks like. ``#wangp_iframe_root`` is still
    in the page, ``#wangp_iframe`` is not, and the server carries on admitting
    and generating the whole time - so an absent iframe is evidence that the
    browser lost its control surface, not that WanGP died, and the two call for
    opposite responses. The iframe is server-rendered into a gr.HTML the tab
    owns and the root observer only ever handled its ARRIVAL, so once it was
    gone nothing in the file would ever ask for another one: the absence lasted
    for the life of the page and a user restarted a browser over a generation
    that was running perfectly well.

    The repair is deliberately small, and almost every check here is about a
    line it must NOT cross. The tab clears the iframe on purpose whenever it
    paints setup, starting or error, so absence alone never authorises
    anything; a repaint is a WanGP page RELOAD, so pressing Refresh at the
    wrong moment is the damage this exists to prevent; and a request that may
    already have reached WanGP is never reported as refused because nobody
    answered it - that is how a job that was running comes back with a retry
    button under it.

    Driven through Node against a stubbed window, on a virtual clock, for the
    same reason the flush checks are: the logic is this file's, and half of
    what is asserted - which of three states was read, whether a button was
    pressed, whether a promise settled as a refusal or as unconfirmed - leaves
    no trace a static read of the source could find.
    """
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if not node:
        r.check("node is available for the recovery checks (skipped)", True)
        return

    source = BROWSER_COPY.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="minipaint-wangp-recovery-") as scratch_dir:
        scratch = pathlib.Path(scratch_dir)
        (scratch / "harness.js").write_text(_RECOVERY_HARNESS, encoding="utf-8")
        (scratch / "scenarios.js").write_text(_RECOVERY_SCENARIOS, encoding="utf-8")
        try:
            got = _run_recovery(node, scratch, BROWSER_COPY)
        except subprocess.TimeoutExpired:
            got = {"__error__": "the recovery harness did not finish"}
        if "__error__" in got:
            r.check("the recovery harness runs", False, got["__error__"])
            return
        broken = [name for name, answer in got.items() if "error" in answer]
        if not r.check("every recovery scenario finished", not broken, ", ".join(broken)[:300]):
            return

        def check(name, scenario, key, expected):
            answer = got.get(scenario) or {}
            r.check(name, answer.get(key) == expected, f"{scenario}.{key}={answer.get(key)!r}")

        # -- the incident itself ---------------------------------------------
        check("an iframe that goes while the view still wants one is repaired",
              "idle removal", "presses", 1)
        check("and nothing is pressed before the grace has run",
              "idle removal", "atOnce", 0)
        check("and the repaint is what the journal says it asked for",
              "idle removal", "saidRepaint", True)
        check("and the replacement is attached and shakes hands",
              "idle removal", "ready", True)
        check("and a repair that worked shows the user nothing",
              "idle removal", "notice", False)

        # -- absence that is not a fault --------------------------------------
        check("a view that changed on purpose presses nothing",
              "view changed", "presses", 0)
        check("and warns about nothing", "view changed", "notice", False)
        check("and says so once, rather than every re-render",
              "view changed", "saidNoAction", True)
        check("a DEGRADED but serving tab is still an iframe tab, and is repaired",
              "degraded still repairs", "presses", 1)
        check("an unrecognised view presses nothing", "a fifth view", "presses", 0)
        check("and neither does a state box that will not parse",
              "malformed state", "presses", 0)

        # -- the two halves of the race ---------------------------------------
        check("a replacement that lands inside the grace is taken, not pressed over",
              "replacement in the grace", "presses", 0)
        check("and the journal says the grace is what caught it",
              "replacement in the grace", "saidGrace", True)
        check("a frame that returns between the grace and the press is not pressed over",
              "frame returns at the last instant", "presses", 0)
        check("and the journal says the press was skipped",
              "frame returns at the last instant", "saidSkipped", True)
        check("a burst of mutations is one cycle and one press",
              "mutation burst", "presses", 1)
        check("and the notice the browser writes does not restart the cycle",
              "the notice does not retrigger", "afterWarning", 1)

        # -- bounded, and only in on-screen time ------------------------------
        check("a slow repaint is waited for rather than pressed again",
              "repaint is slow", "stillOne", 1)
        check("and it attaches when it finally lands", "repaint is slow", "ready", True)
        check("a repair that is waiting spends no budget while the page is hidden",
              "hidden while repairing", "whileAway", False)
        check("and none of the catching-up either", "hidden while repairing", "onReturn", False)
        check("and still finishes when the replacement arrives",
              "hidden while repairing", "ready", True)

        # -- the two ways it ends badly ---------------------------------------
        check("a repaint that never arrives stops, rather than pressing again",
              "repaint never arrives", "presses", 1)
        check("and warns exactly once", "repaint never arrives", "notices", 1)
        check("and the warning does not claim the server's job failed",
              "repaint never arrives", "text",
              "The WanGP controls in this page could not be reconnected automatically. "
              "Any job already accepted by the server may still be running. Avoid restarting "
              "WanGP or Forge while it is working. If the controls remain unavailable, "
              "refresh this browser page only as a last resort.")
        check("an unreadable state never authorises a press",
              "unknown before the press", "presses", 0)
        check("and stops and warns instead", "unknown before the press", "notice", True)
        check("an unreadable state that becomes iframe inside the grace may be pressed",
              "unknown becomes iframe", "presses", 1)
        check("one that becomes a non-iframe view may not",
              "unknown becomes non-iframe", "presses", 0)
        check("and takes an earlier notice away with it",
              "unknown becomes non-iframe", "notice", False)
        check("a state that goes missing after the press is unproved, not a success",
              "unknown after the press", "notice", True)
        check("and it is still only one press", "unknown after the press", "presses", 1)
        check("a tab with no Refresh control left presses nothing",
              "no refresh control", "presses", 0)

        # -- the server answering is a success, not a failure ------------------
        check("a repaint that lands and says WanGP is not serving presses no further",
              "wangp is not serving", "presses", 1)
        check("and shows no warning over the server's own card",
              "wangp is not serving", "notice", False)
        check("and the journal records which view answered",
              "wangp is not serving", "saidAnswered", True)

        # -- the notice is the browser's to remove ----------------------------
        check("a later handshake takes the notice away without Gradio's help",
              "warning clears on a later handshake", "notice", False)
        check("having actually shown it first",
              "warning clears on a later handshake", "warned", True)

        # -- a request whose answer was lost ----------------------------------
        check("a queue request abandoned by frame loss is unconfirmed, never refused",
              "queue request loses its answer", "status", "unconfirmed")
        check("and carries the code the outbox spends on its safe branch",
              "queue request loses its answer", "code", "ADMISSION_UNCONFIRMED")
        check("the same when the frame is replaced rather than removed",
              "replaced under a queue request", "status", "unconfirmed")
        check("and the journal says so from the code that decided it",
              "replaced under a queue request", "saidUnconfirmed", True)
        check("a query keeps today's refusal exactly",
              "a query keeps its refusal", "code", "BRIDGE_SESSION_MISMATCH")
        check("and is not dressed up as unconfirmed",
              "a query keeps its refusal", "unconfirmed", False)
        check("recovery never submits a queue request of its own",
              "recovery submits nothing", "queued", 0)

        # -- what was already true, kept true ---------------------------------
        check("a stale frame reference is discarded without a press",
              "stale frame reference", "presses", 0)
        check("and the live iframe is attached instead",
              "stale frame reference", "ready", True)
        check("a retired iframe cannot answer for the current one",
              "late reply from the old frame", "after", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
        check("a session change with the iframe present presses nothing",
              "session mismatch", "presses", 0)
        check("and is accepted as the new session",
              "session mismatch", "session", "cccccccccccccccccccccccccccccccc")
        check("a healthy tab put aside and brought back does nothing at all",
              "hidden and back, healthy", "presses", 0)
        check("and does not shake hands again either",
              "hidden and back, healthy", "hellos", 1)

        # -- resume, and the root that was replaced ---------------------------
        check("a tab that comes back to a missing iframe repairs it",
              "hidden and back, frame gone", "pressed", 1)
        check("and says that is why", "hidden and back, frame gone", "saidResumed", True)
        check("a replaced root is invisible to the observer bound to the old one",
              "root node replaced", "blind", 0)
        check("the next integrity check notices and rebinds",
              "root node replaced", "saidRebind", True)
        check("and a removal under the new root is detected normally",
              "root node replaced", "readyAgain", True)

        # -- no surface is not a recovery failure ------------------------------
        check("an unreadable state with no root presses nothing",
              "unknown with no root", "midPresses", 0)
        check("and creates no notice, because there is nowhere to put one",
              "unknown with no root", "midNotice", False)
        check("and does not record one either, so a later real failure can warn",
              "unknown with no root", "midWarningShown", False)
        check("it rebinds the observer instead",
              "unknown with no root", "saidNoSurface", True)
        check("and a tab drawn again is picked up by the bounded ladder",
              "unknown with no root", "readyAgain", True)

        # -- the lifecycle journal: frozen, thawed, discarded ------------------
        check("a freeze that follows hiding is said, with how long it lasted",
              "frozen while hidden", "saidFrozen", True)
        check("a thaw while still hidden is not called being back on screen",
              "frozen while hidden", "backWhileHidden", False)
        check("and being shown afterwards still is", "frozen while hidden", "saidBack", True)
        check("a page the browser discarded says so when it loads again",
              "a discarded page says so", "saidDiscarded", True)
        check("and a page loaded normally does not", "a page loaded normally says nothing of a discard", "saidDiscarded", False)

        # -- the tab painted when Forge started ------------------------------
        # The card under the root is painted once, when Forge builds the UI,
        # and nothing repaints it until something presses one of its buttons.
        # A page loaded an hour later showed "Start WanGP" over a WanGP that
        # had been serving since, and the user pressed Start after every
        # reload. One press of the tab's own Refresh at boot, and only when
        # there is no iframe to disturb.
        check("a page that loads onto a card presses Refresh once", "boot with a stale card", "presses", 1)
        check("and says that is why", "boot with a stale card", "saidBoot", True)
        check("a page that loads onto an iframe presses nothing", "boot with an iframe presses nothing", "presses", 0)
        check("and does not claim to have", "boot with an iframe presses nothing", "saidBoot", False)
        check("a tab with no Refresh control is left as it is", "boot without a refresh control", "presses", 0)

        # -- runtime frames ----------------------------------------------------
        # The server says on the spine when the process changes. A page that
        # hears it and disagrees with itself presses Refresh; the server, not
        # the page, decides what the tab then shows.
        check("WanGP serving under a card is repainted", "runtime says serving, tab shows a card", "pressed", 1)
        check("and the journal says which disagreement", "runtime says serving, tab shows a card", "saidRuntime", True)
        check("and the iframe that answers is attached", "runtime says serving, tab shows a card", "ready", True)
        check("WanGP gone under an iframe is repainted", "runtime says stopped, tab shows an iframe", "pressed", 1)
        check("naming the state the server gave", "runtime says stopped, tab shows an iframe", "saidRuntime", True)
        check("and the card that answers is not treated as a fault",
              "runtime says stopped, tab shows an iframe", "notice", False)
        check("and nothing stays bound to the retired iframe", "runtime says stopped, tab shows an iframe", "attached", False)
        check("and that was the only press", "runtime says stopped, tab shows an iframe", "presses", 1)
        check("a burst of runtime frames is one press", "runtime frames coalesce", "pressed", 1)
        check("a frame that agrees with the tab presses nothing", "a runtime frame that agrees presses nothing", "presses", 0)
        check("a snapshot's runtime summary counts the same", "a snapshot carries the same truth", "pressed", 1)
        check("a tab that caught up before the press is left alone", "a tab that caught up is left alone", "pressed", 0)

        # -- and every one of those checks actually bites ----------------------
        for name, old, new, scenario, key, well, ill in _RECOVERY_MUTATIONS:
            if not r.check(f"the check for '{name}' still points at live code", source.count(old) == 1,
                           f"{source.count(old)} matches"):
                continue
            broken_path = scratch / "mutated.js"
            broken_path.write_text(source.replace(old, new), encoding="utf-8")
            answer = (_run_recovery(node, scratch, broken_path).get(scenario) or {})
            r.check(f"'{scenario}' fails when {name}",
                    answer.get(key) == ill and well != ill, f"{key}={answer.get(key)!r}")

        mutated = source
        anchored = True
        for old, new in _RECOVERY_WARN_ANYWAY:
            if mutated.count(old) != 1:
                anchored = False
                break
            mutated = mutated.replace(old, new)
        if r.check("the check for warning without a surface still points at live code", anchored):
            broken_path = scratch / "mutated.js"
            broken_path.write_text(mutated, encoding="utf-8")
            answer = (_run_recovery(node, scratch, broken_path).get("unknown with no root") or {})
            r.check("'unknown with no root' fails when an unreadable state warns wherever it is",
                    answer.get("midWarningShown") is True, str(answer.get("midWarningShown")))


def run() -> Results:
    r = Results("wangp protocol")
    copy_checks(r)
    queue_request_checks(r)
    browser_checks(r)
    envelope_checks(r)
    revision_checks(r)
    receiver_checks(r)
    offered_checks(r)
    fullness_checks(r)
    allowance_checks(r)
    switch_apply_checks(r)
    frame_fallback_checks(r)
    proactive_flush_checks(r)
    live_settings_checks(r)
    heartbeat_checks(r)
    bridge_liveness_checks(r)
    recovery_checks(r)
    page_head_checks(r)
    session_isolation_checks(r)
    loader_checks(r)
    component_handoff_checks(r)
    injection_timing_checks(r)
    placement_checks(r)
    session_hash_checks(r)
    handoff_checks(r)
    return r


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
