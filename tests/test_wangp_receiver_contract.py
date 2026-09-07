"""Fail closed: the gate, the sessions behind it, and the tab that holds them.

Section 4's twentieth decision is the one this file is written against - a
destination that cannot be proven correct is not sent to - and every check
below is an attempt to get an image somewhere nobody agreed to. The gate is
asked for a receiver that does not exist, one that exists but is switched off,
one that is full, and one described by a revision that has moved on; each time
what matters is not only that it refused but that it refused *without
choosing something else*, so every refusal asserts that no decision came back
at all.

The session half is section 44's list, made concrete. Two browser pages hold
two sessions, and moving the model in one must leave the other's menu exactly
where it was; a reloaded iframe replaces its session rather than merging into
it, so an answer prepared before the reload cannot complete after it; and a
runtime that restarts takes its sessions with it, which is what turns "the
image landed in the new WanGP" into ``WANGP_RESTARTED``. The append race of
section 35.2 is driven with two real threads, because "serialised" is a claim
about what happens when two of them arrive at once and nothing else proves it.

The last part builds the whole page with ``tests/forge_like.py``: one stable
WanGP tab with its four containers, beside an intact Mini Paint tab, and then
the same page again with the WanGP tab deliberately broken - the failure this
integration is most likely to have one day is the one where a second tab takes
the first one down with it.

No WanGP, no Forge, no network and no child process is involved anywhere here:
the registry takes its clock, ``send_verified`` takes the function that would
have talked to the bridge, and the log takes the appender.
"""

from harness import Results, setup_path

setup_path()

import hashlib  # noqa: E402
import json  # noqa: E402
import pathlib  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import forge_like  # noqa: E402  (first: it applies Forge's metaclass patches before the canvas stub is defined)
from modules import script_callbacks  # noqa: E402
from minipaint_neo import router  # noqa: E402
from minipaint_neo.canvas import host  # noqa: E402
from minipaint_neo.wangp import bridge, config, errors, protocol  # noqa: E402
from minipaint_neo.wangp import ui as wangp_ui  # noqa: E402

#: Two pages, two ids of the one shape a channel id may have.
CHANNEL_A = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
CHANNEL_B = "0f9e8d7c6b5a49382716f5e4d3c2b1a0"

INSTANCE = "wangp-run-1"
NEXT_INSTANCE = "wangp-run-2"
SESSION = "bridge-session-1"
NEXT_SESSION = "bridge-session-2"

REVISION_A = protocol.state_revision({"model": "h3-fl2va", "start": True})
REVISION_A2 = protocol.state_revision({"model": "h3-ref2va", "references": 1})
REVISION_B = protocol.state_revision({"model": "h3-fl2va", "start": True, "page": "b"})

#: A picture that never exists: only its dimensions and its digests travel.
SOURCE_SHA = hashlib.sha256(b"the flattened canvas").hexdigest()
SOURCE_PIXELS = hashlib.sha256(b"the flattened canvas, decoded").hexdigest()
OTHER_SHA = hashlib.sha256(b"a different picture entirely").hexdigest()
MANIFEST = {"width": 1024, "height": 1024, "sha256": SOURCE_SHA, "pixel_digest": SOURCE_PIXELS}

#: Planted in every field the send log is forbidden to keep. If it appears
#: anywhere in a built record, something crossed from a decision into a file.
SENTINEL = "MUST-NOT-BE-LOGGED"


# ---------------------------------------------------------------- fixtures --


def descriptor(receiver_id: str, role: str, **extra) -> dict:
    raw = {"id": receiver_id, "role": role, "operation": protocol.REPLACE}
    raw.update(extra)
    return raw


def reference(**extra) -> dict:
    """The mutable list receiver: appended to, capped, and raced over."""
    raw = {"count": 0, "max_count": 4}
    raw.update(extra)
    return descriptor(protocol.REFERENCE, "reference", operation=protocol.APPEND, **raw)


def announcement(receivers, revision, session: str = SESSION, instance: str = INSTANCE, **extra) -> dict:
    """What an iframe says about itself, in a READY or a RECEIVERS answer."""
    payload = {
        "protocol": protocol.PROTOCOL,
        "bridge_session": session,
        "instance_id": instance,
        "state_revision": revision,
        "receivers": list(receivers),
        "model": {"type": "t2v", "label": "Wan 2.2 H3", "family": "h3"},
        "ready": True,
        "view": "generate",
        "bridge_version": "1.0.0",
    }
    payload.update(extra)
    return payload


def open_session(book, channel, receivers, revision, session=SESSION, instance=INSTANCE, **extra):
    """A page that has loaded, introduced itself and described its inputs."""
    book.hello(channel, instance)
    return book.ready(channel, announcement(receivers, revision, session, instance, **extra))


def gate(book, channel, receiver_id, revision):
    """``(code, decision)`` - exactly one of the two is ever filled in.

    Both halves are returned on purpose: "it raised the right code" is only
    half of what the gate promises, and the other half is that nothing came
    back describing somewhere else to put the image.
    """
    try:
        return "", book.check_send(channel, receiver_id, revision)
    except errors.IntegrationError as error:
        return error.code, None


def refused(call, *args, **keywords) -> str:
    """The code a call refused with, or "" when it did not refuse at all."""
    try:
        call(*args, **keywords)
    except errors.IntegrationError as error:
        return error.code
    return ""


# -------------------------------------------------------------- the gate ----


def gate_checks(r: Results) -> None:
    book = bridge.Registry()

    code, decision = gate(book, CHANNEL_A, protocol.START_FRAME, REVISION_A)
    r.check("a page with no session cannot send", code == errors.IFRAME_NOT_READY and decision is None, code)

    book.hello(CHANNEL_A, INSTANCE)
    code, decision = gate(book, CHANNEL_A, protocol.START_FRAME, REVISION_A)
    r.check("a page that has said hello but not introduced itself cannot send either",
            code == errors.IFRAME_NOT_READY and decision is None, code)
    r.check("a channel id of the wrong shape is not a session", book.get("nope") is None)

    receivers = [
        descriptor(protocol.START_FRAME, "start", enabled=False, reason_code="MODE_INACTIVE"),
        descriptor(protocol.END_FRAME, "end"),
        reference(count=4),
        descriptor(protocol.CONTROL_IMAGE, "control"),
    ]
    open_session(book, CHANNEL_A, receivers, REVISION_A)

    code, decision = gate(book, CHANNEL_A, protocol.END_FRAME, REVISION_A)
    r.check("a live, enabled receiver at the right revision is allowed", code == "" and decision is not None, code)
    r.check("and the decision names the receiver that was asked for",
            decision and decision["receiver_id"] == protocol.END_FRAME and decision["receiver"]["id"] == protocol.END_FRAME)
    r.check("the decision carries the identity a verification will need",
            decision and decision["instance_id"] == INSTANCE and decision["bridge_session"] == SESSION
            and decision["state_revision"] == REVISION_A)

    code, decision = gate(book, CHANNEL_A, "prompt_box", REVISION_A)
    r.check("a receiver nobody ever defined is unknown",
            code == errors.UNKNOWN_RECEIVER and decision is None, code)
    code, decision = gate(book, CHANNEL_A, protocol.POSITIONED_REF, REVISION_A)
    r.check("a receiver this state does not offer is unknown too",
            code == errors.UNKNOWN_RECEIVER and decision is None, code)
    code, decision = gate(book, CHANNEL_A, None, REVISION_A)
    r.check("no receiver at all is unknown", code == errors.UNKNOWN_RECEIVER and decision is None, code)

    code, decision = gate(book, CHANNEL_A, protocol.START_FRAME, REVISION_A)
    r.check("a receiver the bridge switched off is refused as disabled",
            code == errors.RECEIVER_DISABLED and decision is None, code)
    r.check("and refusing it does not hand back the enabled one next to it", decision is None)

    code, decision = gate(book, CHANNEL_A, protocol.REFERENCE, REVISION_A)
    r.check("a full receiver is refused as full, not as disabled",
            code == errors.RECEIVER_LIMIT_REACHED and decision is None, code)

    for stale in (REVISION_B, "", None, 17, REVISION_A.upper(), REVISION_A[:-1]):
        code, decision = gate(book, CHANNEL_A, protocol.END_FRAME, stale)
        r.check(f"a send prepared at another revision is refused ({stale!r})",
                code == errors.STALE_RECEIVER_STATE and decision is None, code)

    # Order matters: a receiver that does not exist is wrong at every
    # revision, and saying "reopen Send to" about it would send the user back
    # to a menu that will never offer it.
    code, _decision = gate(book, CHANNEL_A, "prompt_box", REVISION_B)
    r.check("an unknown receiver is named as unknown even when the revision is stale too",
            code == errors.UNKNOWN_RECEIVER, code)

    # The module-level gate is what the tab and the menu import, so it is
    # asked the same question once, against the process's own registry.
    bridge.reset_for_tests()
    try:
        r.check("the module-level gate fails closed for a page it has never seen",
                refused(bridge.check_send, CHANNEL_A, protocol.START_FRAME, REVISION_A) == errors.IFRAME_NOT_READY)
        bridge.registry().hello(CHANNEL_B, INSTANCE)
        bridge.registry().ready(CHANNEL_B, announcement([descriptor(protocol.START_FRAME, "start")], REVISION_B))
        r.check("and allows the one it has", bridge.check_send(CHANNEL_B, protocol.START_FRAME, REVISION_B)["ok"] is True)
    finally:
        bridge.reset_for_tests()

    r.check("a channel id of the wrong shape is refused rather than repaired",
            refused(book.hello, "A1B2C3D4E5F60718293A4B5C6D7E8F90", INSTANCE) == errors.IFRAME_NOT_READY)
    r.check("a page with no runtime to belong to is refused",
            refused(book.hello, CHANNEL_B, "") == errors.IFRAME_NOT_READY)
    r.check("a bridge speaking another protocol is a named failure, not an empty menu",
            refused(book.ready, CHANNEL_A, dict(announcement([], REVISION_A), protocol=1))
            == errors.BRIDGE_VERSION_MISMATCH)
    r.check("a bridge that will not name its session is refused",
            refused(book.ready, CHANNEL_A, dict(announcement([], REVISION_A), bridge_session=""))
            == errors.BRIDGE_SESSION_MISMATCH)


# ------------------------------------------------------------- two pages ----


def isolation_checks(r: Results) -> None:
    """Section 44: two Forge tabs, two sessions, no shared "current state"."""
    book = bridge.Registry()
    open_session(book, CHANNEL_A, [descriptor(protocol.START_FRAME, "start")], REVISION_A)
    open_session(book, CHANNEL_B, [descriptor(protocol.START_FRAME, "start"), reference()], REVISION_B,
                 session=NEXT_SESSION)

    before = book.get(CHANNEL_B)
    r.check("two pages hold two sessions", book.get(CHANNEL_A) is not before)
    r.check("each page keeps its own bridge session",
            book.get(CHANNEL_A).bridge_session == SESSION and before.bridge_session == NEXT_SESSION)

    # Page A changes model: a fresh receiver answer, a fresh revision, and a
    # receiver list that no longer contains the start frame at all.
    book.receivers(CHANNEL_A, announcement([reference()], REVISION_A2, model={"type": "ref2v", "label": "Ref2VA"}))

    after = book.get(CHANNEL_B)
    r.check("page B's revision did not move", after.state_revision == REVISION_B, after.state_revision)
    r.check("page B's receivers did not move",
            [item["id"] for item in after.receiver_list()] == [protocol.START_FRAME, protocol.REFERENCE],
            str([item["id"] for item in after.receiver_list()]))
    r.check("page B's model did not move", after.model == before.model, json.dumps(after.model))

    code, decision = gate(book, CHANNEL_B, protocol.START_FRAME, REVISION_B)
    r.check("page B can still send to the input its own menu was drawn from",
            code == "" and decision is not None and decision["receiver_id"] == protocol.START_FRAME, code)

    code, decision = gate(book, CHANNEL_A, protocol.START_FRAME, REVISION_A)
    r.check("page A's own menu has gone stale", code == errors.STALE_RECEIVER_STATE and decision is None, code)
    code, decision = gate(book, CHANNEL_A, protocol.START_FRAME, REVISION_A2)
    r.check("and at page A's new revision that input simply is not there",
            code == errors.UNKNOWN_RECEIVER and decision is None, code)

    book.drop(CHANNEL_A)
    r.check("dropping one page leaves the other alone", book.get(CHANNEL_A) is None and book.get(CHANNEL_B) is not None)

    snapshot = book.snapshot()
    r.check("a diagnostics snapshot shortens the ids it shows",
            snapshot["detail"] and all(len(entry["channel"]) <= 8 and len(entry["bridge_session"]) <= 8
                                       for entry in snapshot["detail"]), json.dumps(snapshot["detail"]))
    r.check("and never prints a whole channel id", CHANNEL_B not in json.dumps(snapshot))


# ------------------------------------------------------------ a new hello ---


def reload_checks(r: Results) -> None:
    book = bridge.Registry()
    open_session(book, CHANNEL_A, [descriptor(protocol.START_FRAME, "start")], REVISION_A)
    r.check("the page can send before the reload", gate(book, CHANNEL_A, protocol.START_FRAME, REVISION_A)[0] == "")

    book.hello(CHANNEL_A, INSTANCE)  # the iframe loaded again
    reloaded = book.get(CHANNEL_A)
    r.check("a new hello replaces the session rather than merging into it",
            reloaded is not None and not reloaded.usable and reloaded.receivers == () and reloaded.state_revision == "",
            str(reloaded))
    code, decision = gate(book, CHANNEL_A, protocol.START_FRAME, REVISION_A)
    r.check("nothing prepared before the reload can be sent after it",
            code == errors.IFRAME_NOT_READY and decision is None, code)
    r.check("and an answer to a query issued before it is not recorded",
            refused(book.receivers, CHANNEL_A, announcement([reference()], REVISION_A)) == errors.IFRAME_NOT_READY)

    book.ready(CHANNEL_A, announcement([reference()], REVISION_A2, session=NEXT_SESSION))
    r.check("an in-flight answer from the previous load is refused by name",
            refused(book.receivers, CHANNEL_A, announcement([reference(count=3)], REVISION_A2, session=SESSION))
            == errors.BRIDGE_SESSION_MISMATCH)
    r.check("the revision from the previous load is stale in the new one",
            gate(book, CHANNEL_A, protocol.REFERENCE, REVISION_A)[0] == errors.STALE_RECEIVER_STATE)
    r.check("the new load can send at its own revision",
            gate(book, CHANNEL_A, protocol.REFERENCE, REVISION_A2)[0] == "")

    # An acknowledgement from the load that has ended must not update the
    # session that replaced it, or a closed page could still move the count.
    settled = book.settle(CHANNEL_A, {"bridge_session": SESSION, "receiver_id": protocol.REFERENCE,
                                      "new_count": 9, "state_revision_after": "0123456789abcdef"})
    r.check("an acknowledgement from the previous load changes nothing",
            settled is not None and settled.state_revision == REVISION_A2
            and settled.receiver(protocol.REFERENCE)["count"] == 0, str(settled.state_revision))


# -------------------------------------------------------------- a restart ---


def restart_checks(r: Results) -> None:
    book = bridge.Registry()
    open_session(book, CHANNEL_A, [reference()], REVISION_A)

    book.invalidate_instance(INSTANCE)
    r.check("a dead run takes its sessions with it", book.get(CHANNEL_A) is None)
    r.check("and is remembered as retired rather than as unknown", book.retired(INSTANCE) is True)
    r.check("a run that never existed is not retired", book.retired("wangp-run-never") is False)
    r.check("a send on a page bound to the dead run is refused",
            gate(book, CHANNEL_A, protocol.REFERENCE, REVISION_A)[0] == errors.IFRAME_NOT_READY)

    # The new child is serving, and the same browser page has re-handshaked.
    open_session(book, CHANNEL_A, [reference()], REVISION_A2, session=NEXT_SESSION, instance=NEXT_INSTANCE)
    code, decision = gate(book, CHANNEL_A, protocol.REFERENCE, REVISION_A)
    r.check("a menu drawn from the old run cannot send into the new one",
            code == errors.STALE_RECEIVER_STATE and decision is None, code)

    r.check("a page answering for the run that ended is refused by name",
            refused(book.receivers, CHANNEL_A, announcement([reference()], REVISION_A2, session=NEXT_SESSION,
                                                            instance=INSTANCE)) == errors.WANGP_RESTARTED)

    # Section 35.4: the restart happens while the image is in flight. The
    # bridge answers "yes, applied" - and it is refused anyway, because the
    # slot it describes belongs to a process that is gone.
    def apply_and_restart(decision):
        book.invalidate_instance(NEXT_INSTANCE)
        open_session(book, CHANNEL_A, [reference()], REVISION_B, session="bridge-session-3", instance="wangp-run-3")
        return {
            "ok": True,
            "receiver_id": protocol.REFERENCE,
            "operation": protocol.APPEND,
            "width": 1024,
            "height": 1024,
            "receiver_digest": SOURCE_SHA,
            "previous_count": 0,
            "new_count": 1,
            "state_revision_after": "beefbeefbeefbeef",
        }

    result = bridge.send_verified(CHANNEL_A, protocol.REFERENCE, REVISION_A2, MANIFEST, apply_and_restart, book=book)
    r.check("a send caught by a restart fails", result["ok"] is False, json.dumps(result["code"]))
    r.check("and says the runtime restarted", result["code"] == errors.WANGP_RESTARTED, result["code"])
    r.check("and claims no verification level", result["verification"] == bridge.UNVERIFIED, result["verification"])
    landed = book.get(CHANNEL_A)
    r.check("nothing was written into the session that replaced it",
            landed.state_revision == REVISION_B and landed.receiver(protocol.REFERENCE)["count"] == 0,
            landed.state_revision)

    book.invalidate_instance("")
    r.check("invalidating everything leaves no page behind", book.snapshot()["sessions"] == 0)


# ---------------------------------------------------------- serialisation ---


def concurrency_checks(r: Results) -> None:
    """Section 35.2's lost update, driven with two threads that really race."""
    book = bridge.Registry()
    open_session(book, CHANNEL_A, [reference(count=0, max_count=4), descriptor(protocol.START_FRAME, "start")],
                 REVISION_A)

    store = {"count": 0, "inside": 0, "peak": 0}
    counters = threading.Lock()
    both_ready = threading.Barrier(2)

    def apply(_decision) -> dict:
        with counters:
            store["inside"] += 1
            store["peak"] = max(store["peak"], store["inside"])
        # Read, pause, write: the shape of the race, with the window opened
        # wide enough that an unserialised pair would certainly lose one.
        seen = store["count"]
        time.sleep(0.05)
        store["count"] = seen + 1
        with counters:
            store["inside"] -= 1
        return {
            "ok": True,
            "receiver_id": protocol.REFERENCE,
            "operation": protocol.APPEND,
            "width": MANIFEST["width"],
            "height": MANIFEST["height"],
            "previous_count": seen,
            "new_count": seen + 1,
            "receiver_present": True,
        }

    results: list = []

    def send() -> None:
        both_ready.wait()
        try:
            results.append(bridge.send_verified(CHANNEL_A, protocol.REFERENCE, REVISION_A, MANIFEST, apply, book=book))
        except errors.IntegrationError as error:  # pragma: no cover - a failure the checks below name
            results.append({"ok": False, "code": error.code, "verification": bridge.UNVERIFIED})

    threads = [threading.Thread(target=send, name=f"append-{index}") for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    r.check("both appends ran", len(results) == 2, str(len(results)))
    r.check("both appends succeeded", all(item["ok"] for item in results), json.dumps([i.get("code") for i in results]))
    r.check("neither append was lost", store["count"] == 2, str(store["count"]))
    r.check("the two never overlapped", store["peak"] == 1, str(store["peak"]))
    r.check("each append verified structurally, on the count it changed",
            all(item["verification"] == bridge.STRUCTURALLY_VERIFIED for item in results),
            json.dumps([i.get("verification") for i in results]))
    r.check("the session ended holding both", book.get(CHANNEL_A).receiver(protocol.REFERENCE)["count"] == 2,
            str(book.get(CHANNEL_A).receiver(protocol.REFERENCE)["count"]))

    # The exclusion itself, without the timing: a held receiver refuses a
    # second send, and a different receiver on the same page does not.
    lock = book.send_lock(CHANNEL_A, protocol.REFERENCE)
    lock.acquire()
    try:
        held = ""
        try:
            with bridge.serialised_send(CHANNEL_A, protocol.REFERENCE, timeout=0.05, book=book):
                pass  # pragma: no cover - reaching here is the failure below
        except errors.IntegrationError as error:
            held = error.code
        r.check("a second send to a busy receiver waits, then refuses",
                held == errors.RECEIVER_APPLY_FAILED, held)
        entered = False
        with bridge.serialised_send(CHANNEL_A, protocol.START_FRAME, timeout=0.05, book=book):
            entered = True
        r.check("another receiver on the same page is not held up by it", entered is True)
    finally:
        lock.release()


# ---------------------------------------------------------- verification ----


def verification_checks(r: Results) -> None:
    expected = bridge.expectation(MANIFEST, {"receiver_id": protocol.REFERENCE, "operation": protocol.APPEND,
                                             "bridge_session": SESSION, "instance_id": INSTANCE})
    r.check("the expectation is assembled from the manifest and the decision",
            expected["width"] == 1024 and expected["sha256"] == SOURCE_SHA
            and expected["receiver_id"] == protocol.REFERENCE, json.dumps(expected))

    def ack(**extra) -> dict:
        base = {
            "ok": True,
            "receiver_id": protocol.REFERENCE,
            "operation": protocol.APPEND,
            "bridge_session": SESSION,
            "instance_id": INSTANCE,
            "width": 1024,
            "height": 1024,
        }
        base.update(extra)
        return base

    ok, level, code = bridge.verify_result(expected, ack(receiver_digest=SOURCE_SHA, source_digest=SOURCE_SHA))
    r.check("the same dimensions and the same file digest is byte-identical",
            ok is True and level == bridge.BYTE_IDENTICAL and code == "", f"{level} {code}")

    ok, level, code = bridge.verify_result(
        expected, ack(source_pixel_digest=SOURCE_PIXELS, receiver_pixel_digest=SOURCE_PIXELS))
    r.check("the same dimensions and the same pixels is pixel-equivalent",
            ok is True and level == bridge.PIXEL_EQUIVALENT and code == "", f"{level} {code}")
    r.check("and pixel-equivalent is good enough to call a send done",
            bridge.accepted(bridge.PIXEL_EQUIVALENT) is True)

    ok, level, code = bridge.verify_result(expected, ack(width=512, receiver_digest=SOURCE_SHA))
    r.check("a receiver holding another size failed verification",
            ok is False and code == errors.RECEIVER_VERIFY_FAILED and level == bridge.UNVERIFIED, f"{level} {code}")
    ok, _level, code = bridge.verify_result(expected, ack(height=999, source_pixel_digest=SOURCE_PIXELS,
                                                          receiver_pixel_digest=SOURCE_PIXELS))
    r.check("a receiver holding another height failed too, digests or not",
            ok is False and code == errors.RECEIVER_VERIFY_FAILED, code)
    ok, _level, code = bridge.verify_result(expected, ack(width=0, height=0))
    r.check("an acknowledgement with no dimensions at all failed",
            ok is False and code == errors.RECEIVER_VERIFY_FAILED, code)

    ok, level, code = bridge.verify_result(expected, {"ok": True})
    r.check("a bare ok:true does not even name a receiver, and fails",
            ok is False and level == bridge.UNVERIFIED and code == errors.UNKNOWN_RECEIVER, f"{level} {code}")
    ok, level, code = bridge.verify_result(expected, ack())
    r.check("an acknowledgement that proves nothing is a failure, not a pass",
            ok is False and level == bridge.UNVERIFIED and code == errors.RECEIVER_VERIFY_FAILED, f"{level} {code}")
    r.check("and 'unverified' is not a level anything accepts",
            bridge.accepted(bridge.UNVERIFIED) is False and bridge.verification_rank(bridge.UNVERIFIED) == 0)
    r.check("nor is a level nobody defined", bridge.accepted("looked-fine") is False)

    ok, _level, code = bridge.verify_result(
        expected, ack(source_pixel_digest=SOURCE_PIXELS, receiver_pixel_digest=OTHER_SHA))
    r.check("two decoded images that differ is worse than no evidence",
            ok is False and code == errors.RECEIVER_VERIFY_FAILED, code)
    ok, _level, code = bridge.verify_result(expected, ack(source_digest=OTHER_SHA, receiver_digest=SOURCE_SHA))
    r.check("a file that changed on the way is named as that",
            ok is False and code == errors.HANDOFF_DIGEST_MISMATCH, code)
    ok, _level, code = bridge.verify_result(expected, ack(receiver_id=protocol.START_FRAME, receiver_digest=SOURCE_SHA))
    r.check("an acknowledgement about another receiver is refused",
            ok is False and code == errors.UNKNOWN_RECEIVER, code)
    ok, _level, code = bridge.verify_result(expected, ack(operation=protocol.REPLACE, receiver_digest=SOURCE_SHA))
    r.check("a replace where an append was agreed is a failure even though an image arrived",
            ok is False and code == errors.RECEIVER_VERIFY_FAILED, code)
    ok, _level, code = bridge.verify_result(expected, ack(instance_id=NEXT_INSTANCE, receiver_digest=SOURCE_SHA))
    r.check("an acknowledgement from another run is refused", ok is False and code == errors.WANGP_RESTARTED, code)
    ok, _level, code = bridge.verify_result(expected, ack(bridge_session=NEXT_SESSION, receiver_digest=SOURCE_SHA))
    r.check("an acknowledgement from another page is refused",
            ok is False and code == errors.BRIDGE_SESSION_MISMATCH, code)

    ok, _level, code = bridge.verify_result(expected, {"ok": False, "code": errors.RECEIVER_LIMIT_REACHED})
    r.check("the bridge's own refusal is the code that is reported",
            ok is False and code == errors.RECEIVER_LIMIT_REACHED, code)
    ok, _level, code = bridge.verify_result(expected, {"ok": False})
    r.check("a refusal with no code still has one", ok is False and code == errors.RECEIVER_APPLY_FAILED, code)
    ok, _level, code = bridge.verify_result(expected, "applied")
    r.check("an acknowledgement that is not an object at all is refused",
            ok is False and code == errors.RECEIVER_APPLY_FAILED, code)

    # An append is verified by the count it moved; a replace by there being
    # something in the slot at all.
    ok, level, _code = bridge.verify_result(expected, ack(previous_count=2, new_count=3))
    r.check("an append that moved the count by one is structurally verified",
            ok is True and level == bridge.STRUCTURALLY_VERIFIED, level)
    ok, _level, code = bridge.verify_result(expected, ack(previous_count=2, new_count=2))
    r.check("an append that moved nothing is not", ok is False and code == errors.RECEIVER_VERIFY_FAILED, code)
    replace = bridge.expectation(MANIFEST, {"receiver_id": protocol.START_FRAME, "operation": protocol.REPLACE})
    ok, level, _code = bridge.verify_result(
        replace, ack(receiver_id=protocol.START_FRAME, operation=protocol.REPLACE, receiver_present=True))
    r.check("a replace that filled the slot is structurally verified",
            ok is True and level == bridge.STRUCTURALLY_VERIFIED, level)
    ok, _level, code = bridge.verify_result(
        replace, ack(receiver_id=protocol.START_FRAME, operation=protocol.REPLACE, receiver_present=False))
    r.check("a replace that left it empty is not", ok is False and code == errors.RECEIVER_VERIFY_FAILED, code)


# ------------------------------------------------------------- the log -----


def log_checks(r: Results) -> None:
    event = {
        "destination": "WanGP - Reference",
        "runtime_state": "READY",
        "bridge_protocol": protocol.PROTOCOL,
        "bridge_version": "1.0.0",
        "model_label": "Wan 2.2 H3",
        "model_type": "ref2v",
        "receiver_id": protocol.REFERENCE,
        "receiver_role": "reference",
        "operation": protocol.APPEND,
        "state_revision": REVISION_A,
        "width": 1024,
        "height": 768,
        "verification": bridge.PIXEL_EQUIVALENT,
        "latency": {"receiver_query": 42, "export": 18, "handoff_write": 5,
                    "send_wait": 2, "bridge_receive": 31, "receiver_verify": 12},
        # Everything section 24 forbids and everything section 9 says never
        # survives a run, each carrying the sentinel or a recognisable number.
        "secret": SENTINEL,
        "bridge_secret": SENTINEL,
        "token": SENTINEL,
        "cookie": SENTINEL,
        "cookies": [SENTINEL],
        "headers": {"authorization": SENTINEL},
        "authorization": SENTINEL,
        "password": SENTINEL,
        "session_hash": SENTINEL,
        "channel_id": CHANNEL_A,
        "bridge_session": SESSION,
        "instance_id": INSTANCE,
        "handoff_id": "0123456789abcdef0123456789abcdef",
        "handoff_path": f"/home/someone/{SENTINEL}.png",
        "path": f"/home/someone/{SENTINEL}",
        "port": 45999,
        "pid": 987654,
        "image": SENTINEL,
        "image_data": SENTINEL,
        "pixels": [SENTINEL],
        "contents": SENTINEL,
        "file_contents": SENTINEL,
    }

    record = bridge.build_log_record(event)
    rendered = json.dumps(record)

    r.check("the record keeps only fields section 24 asks for",
            set(record["fields"]) <= set(bridge.LOGGED_FIELDS), str(sorted(set(record["fields"]) - set(bridge.LOGGED_FIELDS))))
    for field in ("destination", "runtime_state", "bridge_protocol", "bridge_version", "model_label", "model_type",
                  "receiver_id", "receiver_role", "operation", "state_revision", "width", "height",
                  "verification", "latency"):
        r.check(f"the record keeps {field}", field in record["fields"])
    for field in sorted(bridge.FORBIDDEN_FIELDS):
        r.check(f"the record drops {field}", field not in record["fields"])
    for name in ("channel_id", "bridge_session", "instance_id", "handoff_id", "handoff_path",
                 "path", "port", "pid", "secret", "bridge_secret", "session_hash", "cookie",
                 "cookies", "headers", "authorization", "image", "image_data", "pixels",
                 "contents", "file_contents", "token", "password"):
        r.check(f"and {name} is nowhere in the written record", f'"{name}"' not in rendered)

    r.check("nothing forbidden reached the record by value either", SENTINEL not in rendered)
    r.check("the backend port is not in it", "45999" not in rendered)
    r.check("the pid is not in it", "987654" not in rendered)
    r.check("the channel id is not in it", CHANNEL_A not in rendered)
    r.check("the bridge session is not in it", SESSION not in rendered)
    r.check("the handoff id is not in it", "0123456789abcdef0123456789abcdef" not in rendered)

    r.check("the destination is what the log line is titled", record["destination"] == "WanGP - Reference")
    r.check("a success says how it was verified, into what, and how big",
            record["outcome"] == f"{bridge.PIXEL_EQUIVALENT} - 1024x768 into {protocol.REFERENCE} ({protocol.APPEND})",
            record["outcome"])
    r.check("the steps say which runtime and which bridge", "runtime: READY" in record["steps"]
            and f"bridge: protocol {protocol.PROTOCOL}, plugin 1.0.0" in record["steps"], str(record["steps"]))
    r.check("the steps name the model and the receiver",
            "model: Wan 2.2 H3 (ref2v)" in record["steps"]
            and f"receiver: {protocol.REFERENCE} / reference, {protocol.APPEND}" in record["steps"], str(record["steps"]))
    r.check("the steps carry the revision the send was judged at",
            f"state revision: {REVISION_A}" in record["steps"], str(record["steps"]))
    r.check("the latency breakdown is section 24's, labelled and in order",
            bridge.format_latency(event["latency"]) == ["receiver query: 42 ms", "flatten/export: 18 ms",
                                                        "handoff write: 5 ms", "send wait: 2 ms",
                                                        "bridge receive: 31 ms", "receiver verify: 12 ms"],
            str(bridge.format_latency(event["latency"])))
    r.check("a step nobody measured is simply absent", bridge.format_latency({"export": "quick"}) == [])
    r.check("the last step is the verification level", record["steps"][-1] == f"final: {bridge.PIXEL_EQUIVALENT}",
            record["steps"][-1])

    failure = bridge.build_log_record(dict(event, error_code=errors.RECEIVER_VERIFY_FAILED,
                                           error_detail="the receiver held a 512x512 image"))
    r.check("a failure is logged as its code and the sentence errors.py owns",
            failure["outcome"] == f"failed: {errors.RECEIVER_VERIFY_FAILED} - {errors.MESSAGES[errors.RECEIVER_VERIFY_FAILED]}",
            failure["outcome"])
    r.check("the detail is a step, not the sentence",
            "detail: the receiver held a 512x512 image" in failure["steps"], str(failure["steps"]))
    r.check("a failed send does not also claim a verification level",
            not any(step.startswith("final:") for step in failure["steps"]), str(failure["steps"]))

    r.check("an empty event still builds a record", bridge.build_log_record({})["destination"] == "WanGP")
    r.check("so does something that is not an event at all", bridge.build_log_record(None)["fields"] == {})

    written: list = []
    bridge.log_transfer(event, logger=written.append)
    r.check("the transfer is written once", len(written) == 1 and written[0]["destination"] == "WanGP - Reference")

    def broken(_record):
        raise OSError("the log is read-only")

    r.check("a log that cannot be written is not why a send failed",
            bridge.log_transfer(event, logger=broken)["destination"] == "WanGP - Reference")


# -------------------------------------------------------------- the tab ----


def config_of(blocks) -> dict:
    return json.loads(json.dumps(blocks.get_config_file(), default=str))


def elem_ids(page: dict) -> set:
    return {component["props"].get("elem_id") for component in page["components"] if component.get("props")}


def component_of(page: dict, elem_id: str):
    for component in page["components"]:
        if component["props"].get("elem_id") == elem_id:
            return component
    return None


def dangling(page: dict) -> list:
    known = {component["id"] for component in page["components"]}
    return [dependency for dependency in page["dependencies"]
            if any(item not in known for item in dependency["inputs"] + dependency["outputs"])]


def tab_checks(r: Results) -> None:
    """Section 7: one tab, four containers, and a failure that stays inside it."""
    saved_hooks = list(script_callbacks.callbacks["after_component"])
    with tempfile.TemporaryDirectory(prefix="minipaint-wangp-tab-") as directory:
        # A config directory with nothing in it is a WanGP that has never been
        # set up, which is the state the tab must be buildable in and the one
        # that starts no process.
        config.use_config_dir(pathlib.Path(directory))
        try:
            tabs = wangp_ui.on_ui_tabs()
            r.check("the extension offers exactly one WanGP tab", len(tabs) == 1, str(len(tabs)))
            _blocks, label, ident = tabs[0]
            r.check("under the id everything else addresses",
                    ident == "wangp" == wangp_ui.TAB_ID and label == wangp_ui.TAB_LABEL, f"{label}/{ident}")

            script_callbacks.callbacks["after_component"][:] = [host.on_after_component]
            host.reset_capture()
            demo, _refs = forge_like.build_host(lambda: router.on_ui_tabs() + wangp_ui.on_ui_tabs())
            page = config_of(demo)
            ids = elem_ids(page)

            r.check("the page has the WanGP tab", "tab_wangp" in ids)
            r.check("and exactly one of it",
                    len([c for c in page["components"] if c["props"].get("elem_id") == "tab_wangp"]) == 1)
            for container in (wangp_ui.SETUP_ROOT_ID, wangp_ui.STARTING_ROOT_ID,
                              wangp_ui.ERROR_ROOT_ID, wangp_ui.IFRAME_ROOT_ID):
                r.check(f"the tab holds {container}", container in ids)
            for seam in (wangp_ui.CHANNEL_ELEM_ID, wangp_ui.STATE_ELEM_ID, wangp_ui.BROWSER_CHECK_ELEM_ID,
                         wangp_ui.OPEN_ELEM_ID, wangp_ui.REFRESH_ELEM_ID):
                r.check(f"the browser half's {seam} is there", seam in ids)

            r.check("only the setup container is showing before setup",
                    component_of(page, wangp_ui.SETUP_ROOT_ID)["props"].get("visible", True) is True
                    and all(component_of(page, other)["props"].get("visible") is False
                            for other in (wangp_ui.STARTING_ROOT_ID, wangp_ui.ERROR_ROOT_ID, wangp_ui.IFRAME_ROOT_ID)))
            state = json.loads(component_of(page, wangp_ui.STATE_ELEM_ID)["props"]["value"])
            r.check("and it says so in the state the Send menu reads",
                    state["view"] == wangp_ui.VIEW_SETUP and state["state"] == wangp_ui.STATE_SETUP_REQUIRED,
                    json.dumps(state))
            r.check("the channel textbox starts empty",
                    component_of(page, wangp_ui.CHANNEL_ELEM_ID)["props"]["value"] == "")

            # The whole page, not just the tab: the second integration must not
            # cost the first one anything.
            r.check("the Mini Paint tab is still there", "tab_minipaint" in ids)
            r.check("with its canvas", "minipaint_canvas_surface" in ids and "minipaint_canvas_root" in ids)
            r.check("and its receive buttons in the host's rows",
                    {"txt2img_send_to_minipaint", "img2img_send_to_minipaint", "extras_send_to_minipaint"} <= ids)
            r.check("the host's own tabs are intact",
                    {"tab_txt2img", "tab_img2img", "tab_extras", "tab_settings", "tab_extensions"} <= ids)
            r.check("every event on the assembled page resolves", not dangling(page), str(dangling(page)[:1]))

            # The checklist prints the words "127.0.0.1" in a row about the
            # command line, which is the point of that row; what must not be
            # anywhere is a URL the browser could dial the backend on.
            rendered = json.dumps(page)
            r.check("nothing in the page hands the browser a backend address",
                    not any(scheme in rendered for scheme in ("http://127.0.0.1", "https://127.0.0.1",
                                                              "//localhost", "http://0.0.0.0")))
            frame = wangp_ui.iframe_html("0123456789abcdef0123456789abcdef")
            r.check("the iframe is pointed at a path on this origin, and only that",
                    f'src="{wangp_ui.PUBLIC_PATH}"' in frame and "://" not in frame, frame)
            r.check("and the channel id is not in the URL it is given",
                    "0123456789abcdef0123456789abcdef" not in frame)

            # ---- and now the failure this whole shape exists for ----
            print("  (the traceback below is this test breaking the WanGP tab on purpose)")
            working = wangp_ui.create_ui
            wangp_ui.create_ui = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
            try:
                script_callbacks.callbacks["after_component"][:] = [host.on_after_component]
                host.reset_capture()
                broken_demo, _broken_refs = forge_like.build_host(
                    lambda: router.on_ui_tabs() + wangp_ui.on_ui_tabs())
            finally:
                wangp_ui.create_ui = working
            broken = config_of(broken_demo)
            broken_ids = elem_ids(broken)

            r.check("a WanGP tab that cannot be built is still a tab", "tab_wangp" in broken_ids)
            r.check("and says why",
                    any("could not be built" in str(c["props"].get("value", ""))
                        for c in broken["components"] if c.get("props")))
            r.check("none of the four containers is half-built",
                    not ({wangp_ui.SETUP_ROOT_ID, wangp_ui.STARTING_ROOT_ID,
                          wangp_ui.ERROR_ROOT_ID, wangp_ui.IFRAME_ROOT_ID} & broken_ids))
            r.check("the Mini Paint tab survives it", "tab_minipaint" in broken_ids
                    and "minipaint_canvas_surface" in broken_ids)
            r.check("the host's tabs survive it",
                    {"tab_txt2img", "tab_img2img", "tab_settings", "tab_extensions"} <= broken_ids)
            r.check("and the failure left no dangling events", not dangling(broken), str(dangling(broken)[:1]))
        finally:
            script_callbacks.callbacks["after_component"][:] = saved_hooks
            host.reset_capture()
            config.use_config_dir(None)


def handoff_release_checks(r: Results) -> None:
    """A finished send lets go of the file it prepared.

    The prepared PNG is the one thing a send leaves behind on disk, and the
    browser's report is the only moment anything knows the transfer is over -
    the bridge has already decoded the picture into WanGP's own component
    value, so nothing points at the file any more. Section 20.7 asks for it to
    go on success and on failure alike; the regression this guards is a report
    that carried no id at all, which left every send's file on disk for the
    life of the process.
    """
    import json as _json
    import tempfile as _tempfile

    from minipaint_neo.canvas import ui as canvas_ui
    from minipaint_neo.wangp import config as wangp_config
    from minipaint_neo.wangp import handoff as wangp_handoff
    from PIL import Image

    canvas = object.__new__(canvas_ui.TouchCanvas)
    # Never ask for the real one, not even to put it back: resolving it makes
    # it, and a suite that runs in a checkout would leave a folder behind.
    with _tempfile.TemporaryDirectory(prefix="minipaint-wangp-release-") as temporary:
        wangp_config.use_config_dir(pathlib.Path(temporary) / "data")
        try:
            # What the browser says, and what of it is believed.
            prepared = wangp_handoff.write(Image.new("RGBA", (12, 8), (9, 9, 9, 255)))
            read = canvas_ui.wangp_report(_json.dumps({"handoff_id": prepared.id, "receiver_id": "start_frame", "ok": True}))
            r.check("a real handoff id is carried back", read["handoff_id"] == prepared.id)
            for label, bad in (("a path", "../" + "a" * 29), ("a short id", "a" * 31), ("upper case", "A" * 32), ("nothing", "")):
                spoiled = canvas_ui.wangp_report(_json.dumps({"handoff_id": bad, "receiver_id": "start_frame"}))
                r.check(f"{label} is not accepted as a handoff id", spoiled["handoff_id"] == "")

            # A send that worked: the file goes.
            r.check("the prepared file exists to begin with", prepared.path.exists())
            canvas.wangp_result(None, _json.dumps({
                "handoff_id": prepared.id, "receiver_id": "start_frame", "ok": True,
                "role": "start", "operation": "replace", "verification": "pixel-equivalent",
                "width": 12, "height": 8, "state_revision": "r",
            }))
            r.check("a verified send removes its file", not prepared.path.exists())
            r.check("and forgets its manifest", wangp_handoff.manifest_of(prepared.id) is None)

            # A send that failed: the file goes too, and so does one whose
            # report names no receiver at all - the early return used to skip
            # the cleanup entirely.
            failed = wangp_handoff.write(Image.new("RGBA", (4, 4)))
            canvas.wangp_result(None, _json.dumps({
                "handoff_id": failed.id, "receiver_id": "start_frame", "ok": False, "code": "RECEIVER_VERIFY_FAILED",
            }))
            r.check("a failed send removes its file too", not failed.path.exists())

            nameless = wangp_handoff.write(Image.new("RGBA", (4, 4)))
            canvas.wangp_result(None, _json.dumps({
                "handoff_id": nameless.id, "receiver_id": "", "ok": False, "code": "IFRAME_NOT_READY",
            }))
            r.check("a send that never named a receiver still releases its file", not nameless.path.exists())

            # Nothing is deleted on the strength of an id we did not mint.
            survivor = wangp_handoff.write(Image.new("RGBA", (4, 4)))
            canvas.wangp_result(None, _json.dumps({"handoff_id": "../../etc/passwd", "receiver_id": "start_frame", "ok": True}))
            r.check("a bad id in a report removes nothing", survivor.path.exists())
            wangp_handoff.discard(survivor.id)
        finally:
            wangp_config.use_config_dir(None)


def run() -> Results:
    r = Results("wangp receiver contract")
    try:
        gate_checks(r)
        isolation_checks(r)
        reload_checks(r)
        restart_checks(r)
        concurrency_checks(r)
        verification_checks(r)
        log_checks(r)
        tab_checks(r)
        handoff_release_checks(r)
    finally:
        bridge.reset_for_tests()
    return r


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
