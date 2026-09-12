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
BROWSER_COPY = ROOT / "javascript" / "minipaint_wangp.js"

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
  fn({ origin: ORIGIN, source: parent, data: { protocol: 3, type: "WANGP_BRIDGE_HELLO", channel_id: channel, request_id: "r1", payload: {} } });
});
if (process.argv[5] === "refuse") {
  // After the hello: one send the page cannot act on (a handoff id that is
  // not one), and one on a channel this page was never bound to.
  setTimeout(function () {
    (listeners.message || []).forEach(function (fn) {
      fn({ origin: ORIGIN, source: parent, data: { protocol: 3, type: "WANGP_RECEIVE_IMAGE", channel_id: channel, request_id: "r2",
        payload: { handoff_id: "nope", receiver_id: "start_frame", state_revision: "abcdef12" } } });
      fn({ origin: ORIGIN, source: parent, data: { protocol: 3, type: "WANGP_RECEIVE_IMAGE", channel_id: "d".repeat(32), request_id: "r3",
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
    r.check("a request with nothing in it is valid", code == "" and request == {"request_id": good, "bridge_session": ""}, repr(request))
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
            "end_handoff_id", "reference_handoff_ids"}, str(sorted(request)))
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
    r.check("a status normalises", status == {"ok": True, "request_id": good, "status": "queued", "tasks_added": 3, "code": ""}, repr(status))
    r.check("an unreadable status is pending with no tasks, never queued",
            protocol.normalize_queue_status({"ok": True, "status": "done", "tasks_added": 9})["status"] == "pending"
            and protocol.normalize_queue_status({"status": "queued"})["tasks_added"] == 0)
    r.check("the queue messages are whitelisted in their directions",
            protocol.valid_envelope(protocol.envelope(protocol.QUEUE_REQUEST, "c" * 32, good), protocol.TO_BRIDGE)
            and protocol.valid_envelope(protocol.envelope(protocol.QUEUE_STATUS, "c" * 32, good), protocol.TO_PARENT)
            and not protocol.valid_envelope(protocol.envelope(protocol.QUEUE_RESULT, "c" * 32, good), protocol.TO_BRIDGE))
    r.check("a full prompt fits the envelope many times over",
            len(protocol.canonical_json({"prompt": "\u2603" * protocol.PROMPT_MAX_CHARS, "reference_handoff_ids": [good] * protocol.MAX_QUEUE_REFERENCES}))
            * 8 < protocol.MAX_ENVELOPE_BYTES)


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
