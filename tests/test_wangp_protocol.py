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
    component_handoff_checks(r)
    injection_timing_checks(r)
    placement_checks(r)
    handoff_checks(r)
    return r


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
