"""Protocol 4: the start decision, made inside WanGP from WanGP's own flag.

A queue request now says whether it may start a generation: ``auto`` (the
default) means "generating as soon as WanGP can", ``never`` means "stage the
task only". The bridge decides which of WanGP's two triggers to write - the
generate trigger, whose change runs the same chain as Add to queue and then
``process_tasks``, or the add-to-queue trigger - and it decides from
``is_generation_in_progress()``, the module function Wan2GP keeps for
exactly this question, read live and process-wide. The rule is asymmetric on
purpose: only a definite "nothing is generating" from that flag, on a build
that handed over the generate trigger, takes the generate route; a running
generation, a flag the build cannot read, or a missing trigger all take the
queue route and say so. A task that waits costs a click; a second
concurrent run costs the loaded model.

Confirmation then tells ``started`` from ``queued`` by WanGP's own record -
the request's task at the head of the page's queue while the page's loop is
running - reports how many tasks sit ahead, and keeps a positive answer
sticky. The same stub host as the queue suite, plus the flag.
"""

from harness import Results, setup_path

setup_path()

import json  # noqa: E402
import pathlib  # noqa: E402
import tempfile  # noqa: E402

from minipaint_neo.wangp import protocol  # noqa: E402

import test_wangp_queue as base  # noqa: E402  (the stub host, the page, the pictures)

FORM_WITH_GENERATE = base.FORM_NAMES + ("generate_trigger",)
ID_A, ID_B, ID_C, ID_D = base.ID_A, base.ID_B, base.ID_C, base.ID_D


class _Wgp(base._Wgp):
    """The queue suite's Wan2GP, with the process-wide generation flag."""

    def __init__(self, definitions, running=False):
        super().__init__(definitions)
        self.running = running
        self.flag_reads = 0

    def is_generation_in_progress(self):
        self.flag_reads += 1
        return self.running


class _StaleWgp(base._Wgp):
    """A host that injected the flag's *value* rather than the function: a
    number that can only ever be as old as the injection, so not an answer."""

    is_generation_in_progress = False


def _bridge(root, names=FORM_WITH_GENERATE, running=False, wgp=None, clock=None):
    plugin, compatibility, _admission, _ui = base._modules()
    wgp = wgp if wgp is not None else _Wgp({"video": base.VIDEO_MODEL, "text": base.TEXT_ONLY_MODEL}, running=running)
    bridge = plugin.MiniPaintBridge(
        host=compatibility.Host(wgp),
        environ={"MINIPAINT_WANGP_INSTANCE_ID": "i", "MINIPAINT_WANGP_HANDOFF_ROOT": str(root)},
        clock=clock or base._Clock(),
    )
    bridge.compat.declare_globals()
    bridge.compat.host.accept_components({name: (base.Gallery(name) if name in base.GALLERIES else base._Handed(name)) for name in names})
    bridge.resolve()
    return bridge, wgp


def _tasks(page, K, *ids, in_progress=False):
    """WanGP's own record for the page: these tasks, in this order."""
    page[K.SESSION_STATE]["gen"] = {"queue": [{"id": index + 1, "params": {"client_id": item}} for index, item in enumerate(ids)], "in_progress": in_progress}


def decision_checks(r: Results, root: str) -> None:
    plugin, compatibility, admission, _ui = base._modules()
    K = compatibility
    session = plugin.bridge_session_for("session-hash-of-this-page", "i")

    # -- the handshake says the page can start, and whether WanGP is generating
    bridge, wgp = _bridge(root)
    page = base._page(K)
    hello, _ = base._call(bridge, page, "hello")
    r.check("the flag is asked for as a global when the plugin is constructed", "is_generation_in_progress" in wgp.asked_globals, str(wgp.asked_globals))
    r.check("a build with the generate trigger offers start in its handshake",
            hello.get("capabilities", {}).get("start") is True and hello.get("capabilities", {}).get("queue") is True, str(hello.get("capabilities")))
    r.check("and says, live, that nothing is generating", hello.get("generation_running") is False)
    r.check("the generate trigger is the last queue-only output", bridge.queue_keys[-1] == K.GENERATE_TRIGGER, str(bridge.queue_keys))
    r.check("protocol 5 is what both halves speak", hello.get("protocol") == 5 == protocol.PROTOCOL and hello.get("bridge_version") == "1.4.0")

    # -- auto while idle: the generate route
    ack, writes = base._call(bridge, page, "queue", {"request_id": ID_A, "bridge_session": session, "prompt": "go"})
    r.check("auto on an idle WanGP is admitted through the generate route",
            ack.get("ok") is True and ack.get("route") == "generate" and ack.get("start") == "auto" and ack.get("generation_running") is False, json.dumps(ack)[:200])
    r.check("the generate trigger is written and the add-to-queue trigger is not",
            K.GENERATE_TRIGGER in writes and K.ADD_TO_QUEUE_TRIGGER not in writes and base._raw(writes[K.GENERATE_TRIGGER]).startswith("unique-"), str(sorted(writes)))
    r.check("the client id is still the request id", base._raw(writes[K.CLIENT_ID]) == ID_A)
    r.check("the flag was read for the decision", wgp.flag_reads >= 1)
    r.check("the acknowledgement never carries the prompt", "go" not in json.dumps({k: v for k, v in ack.items() if k not in ("applied", "inherited", "ignored")}))
    after = base._apply(page, writes)

    # -- started: the task at the head, the loop running
    _tasks(after, K, ID_A, in_progress=True)
    status, restore = base._call(bridge, after, "confirm", {"request_id": ID_A, "bridge_session": session})
    r.check("the task at the head of a running loop is started", status.get("status") == "started" and status.get("tasks_added") == 1 and status.get("queue_depth") == 0, json.dumps(status)[:200])
    r.check("and the overrides are put back, the trigger never among them",
            K.PROMPT in (status.get("restored") or []) and K.CLIENT_ID in (status.get("restored") or []) and K.GENERATE_TRIGGER not in (status.get("restored") or []), str(status.get("restored")))
    r.check("the answer keeps the route", status.get("route") == "generate")
    _tasks(after, K)  # the run finished and the queue emptied
    again, _ = base._call(bridge, after, "confirm", {"request_id": ID_A, "bridge_session": session})
    r.check("started sticks once seen", again.get("status") == "started")

    # -- auto while generating: the queue route, and a depth
    wgp.running = True
    page = base._page(K)
    hello, _ = base._call(bridge, page, "receivers")
    r.check("a receivers answer says generation is running", hello.get("generation_running") is True)
    ack, writes = base._call(bridge, page, "queue", {"request_id": ID_B, "bridge_session": session, "prompt": "join"})
    r.check("auto on a busy WanGP joins the run through the add-to-queue trigger",
            ack.get("route") == "queue" and ack.get("start") == "auto" and ack.get("generation_running") is True
            and K.ADD_TO_QUEUE_TRIGGER in writes and K.GENERATE_TRIGGER not in writes, json.dumps(ack)[:200])
    after = base._apply(page, writes)
    _tasks(after, K, ID_A, ID_B, in_progress=True)
    status, _ = base._call(bridge, after, "confirm", {"request_id": ID_B, "bridge_session": session})
    r.check("a task behind the running one is queued, one ahead of it", status.get("status") == "queued" and status.get("queue_depth") == 1, json.dumps(status)[:200])

    # -- never: the queue route whatever the flag says
    wgp.running = False
    page = base._page(K)
    ack, writes = base._call(bridge, page, "queue", {"request_id": ID_C, "bridge_session": session, "start": "never"})
    r.check("never takes the queue route on an idle WanGP", ack.get("route") == "queue" and ack.get("start") == "never" and K.ADD_TO_QUEUE_TRIGGER in writes and K.GENERATE_TRIGGER not in writes, json.dumps(ack)[:200])
    after = base._apply(page, writes)
    _tasks(after, K, ID_C, in_progress=False)
    status, _ = base._call(bridge, after, "confirm", {"request_id": ID_C, "bridge_session": session})
    r.check("and a staged task at the head of an idle queue is queued, not started", status.get("status") == "queued" and status.get("queue_depth") == 0)

    # -- the same id with another start mode is another payload
    page = base._page(K)
    ack, _ = base._call(bridge, page, "queue", {"request_id": ID_C, "bridge_session": session, "start": "auto"})
    r.check("changing only the start mode under a used id is REQUEST_ID_CONFLICT", ack.get("code") == "REQUEST_ID_CONFLICT", json.dumps(ack)[:200])
    ack, writes = base._call(bridge, page, "queue", {"request_id": ID_D, "bridge_session": session, "start": "later"})
    r.check("an unknown start mode is REQUEST_INVALID, and nothing is written", ack.get("code") == "REQUEST_INVALID" and writes in (None, {}), json.dumps(ack)[:200])

    # -- a flag this build cannot read: the fail-safe direction
    stale_bridge, _stale = _bridge(root, wgp=_StaleWgp({"video": base.VIDEO_MODEL}))
    page = base._page(K)
    hello, _ = base._call(stale_bridge, page, "hello")
    r.check("a flag injected as a value, not a function, is unknown", hello.get("generation_running") is None and hello.get("capabilities", {}).get("start") is True)
    ack, writes = base._call(stale_bridge, page, "queue", {"request_id": ID_D, "bridge_session": session, "prompt": "x"})
    r.check("auto with an unreadable flag stages the task and says start unknown",
            ack.get("route") == "queue" and ack.get("start") == "unknown" and ack.get("generation_running") is None and K.ADD_TO_QUEUE_TRIGGER in writes, json.dumps(ack)[:200])

    # -- a build without the generate trigger: start is off, the queue is not
    older, _wgp = _bridge(root, names=base.FORM_NAMES)
    page = base._page(K)
    hello, _ = base._call(older, page, "hello")
    r.check("a build without generate_trigger does not offer start, and names it",
            hello.get("capabilities", {}).get("start") is False and hello.get("capabilities", {}).get("queue") is True and hello.get("start_missing") == ["generate_trigger"], json.dumps(hello.get("capabilities")))
    ack, writes = base._call(older, page, "queue", {"request_id": ID_D, "bridge_session": session})
    r.check("auto there degrades to the queue route and says unknown", ack.get("route") == "queue" and ack.get("start") == "unknown" and K.ADD_TO_QUEUE_TRIGGER in writes)
    r.check("its queue outputs are the protocol 3 four", older.queue_keys == (K.PROMPT, K.WIZARD_PROMPT, K.CLIENT_ID, K.ADD_TO_QUEUE_TRIGGER), str(older.queue_keys))

    # -- the shared normalisers carry the new fields, and nothing invented
    result = protocol.normalize_queue_result({"ok": True, "admission": "requested", "request_id": ID_A, "route": "generate", "start": "auto", "generation_running": False})
    r.check("a result carries route, start and the flag", result["route"] == "generate" and result["start"] == "auto" and result["generation_running"] is False)
    odd = protocol.normalize_queue_result({"ok": True, "admission": "requested", "request_id": ID_A, "route": "sideways", "start": 3, "generation_running": "yes"})
    r.check("and drops shapes it does not know", odd["route"] == "" and odd["start"] == "" and odd["generation_running"] is None)
    status = protocol.normalize_queue_status({"ok": True, "request_id": ID_A, "status": "started", "tasks_added": 1, "queue_depth": 0, "route": "generate"})
    r.check("a started status is a positive status with a depth", status["status"] == "started" and status["queue_depth"] == 0 and status["route"] == "generate")
    r.check("a depth that is not a count is absent", protocol.normalize_queue_status({"ok": True, "request_id": ID_A, "status": "queued", "queue_depth": -1})["queue_depth"] is None)
    request, code = protocol.normalize_queue_request({"request_id": ID_A})
    r.check("a request that says nothing about starting means auto", code == "" and request["start"] == "auto")
    r.check("the statuses list started between queued and refused", protocol.QUEUE_STATUSES == ("pending", "queued", "started", "refused", "expired"))


def run() -> Results:
    r = Results("wangp start")
    with tempfile.TemporaryDirectory(prefix="minipaint-wangp-start-") as root:
        base._pictures(root)
        decision_checks(r, root)
    return r


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
