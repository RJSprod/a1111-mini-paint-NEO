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
    ):
        r.check(f"the browser names {name} the same way", js_string(source, name) == value, js_string(source, name))

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


def run() -> Results:
    r = Results("wangp protocol")
    copy_checks(r)
    browser_checks(r)
    envelope_checks(r)
    revision_checks(r)
    receiver_checks(r)
    fullness_checks(r)
    session_isolation_checks(r)
    loader_checks(r)
    handoff_checks(r)
    return r


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
