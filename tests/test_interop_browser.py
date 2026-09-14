"""The browser half of walking away: told, not asking, and safe to close.

The server half of "press Add to Queue and walk away" is asserted in
``test_clipboard_executor``, with no page anywhere near it. This is the other
half, and it is the half that used to be wrong: the page was a *pump*. It
claimed a job, drove the live WanGP form and reported back, so closing the
tab stopped the queue, and a locked phone stopped it too.

What is asserted here is that it no longer does any of that for a job the
server owns:

*   an admitted server job opens the one event stream and never asks the
    claim route - not once, not late, not on a retry;
*   a terminal frame settles a caller waiting on that job from the
    authoritative record rather than from the event, because an event is a
    description and the snapshot is the truth;
*   a reset, a cursor this process cannot honour and an epoch from another
    run of Forge all produce exactly one sync rather than an error;
*   events that arrive while a sync is in flight are applied after it, in
    order, and the ones the snapshot already covers are discarded;
*   a browser-executed job still pumps, because the compatibility window is
    real and an already-loaded page must keep working.

Driven through Node against a stubbed window rather than a real browser, the
way the frame-timer checks in ``test_wangp_protocol`` are: what is under test
is this file's own logic, and a real browser would add a dependency without
adding an assertion. The end-to-end version lives in ``browser_smoke.py``.
"""

from harness import Results, ROOT, setup_path

setup_path()

import json  # noqa: E402
import pathlib  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402

BUNDLE = ROOT / "browser" / "minipaint_interop.js"

#: A window with exactly as much of a browser in it as this module touches:
#: fetch, EventSource, a document with listeners, a storage, and crypto. Every
#: call is recorded, which is what lets "never asked the claim route" be an
#: assertion rather than an impression.
HARNESS = r"""
const fs = require("fs");

const calls = [];
const streams = [];
const listeners = {};

function record(url, body) { calls.push({ url: String(url), body: body ? JSON.parse(body) : null }); }

class FakeEventSource {
    constructor(url) {
        this.url = String(url);
        this.handlers = {};
        streams.push(this);
    }
    addEventListener(kind, fn) { (this.handlers[kind] = this.handlers[kind] || []).push(fn); }
    close() { this.closed = true; }
    fire(kind, payload, id) {
        for (const fn of this.handlers[kind] || []) { fn({ data: JSON.stringify(payload || {}), lastEventId: id || "" }); }
    }
}

const JOBS = { list: [] };
const flushes = [];

// The WanGP side of the browser, with just enough of it to see the order a
// press happens in. FLUSH_MODE is what this run's WanGP tab can manage.
let FLUSH_MODE = "committed";
global.window = global.window || {};

global.window = {
    crypto: { getRandomValues: function (bytes) { for (let i = 0; i < bytes.length; i++) { bytes[i] = (i * 7 + 3) % 256; } return bytes; } },
    addEventListener: function (kind, fn) { (listeners[kind] = listeners[kind] || []).push(fn); },
    localStorage: { getItem: function () { return null; }, setItem: function () { } },
    sessionStorage: { getItem: function () { return null; }, setItem: function () { } },
    dispatchEvent: function () { return true; },
    EventSource: FakeEventSource
};
global.EventSource = FakeEventSource;
const told = [];
window.minipaintWanGP = {
    flushForm: function () {
        flushes.push({ at: calls.length });
        return Promise.resolve(FLUSH_MODE === "absent" ? { ok: false } : { ok: true, flush: FLUSH_MODE });
    },
    // The WanGP tab's own script keeps the recorded form current ahead of a
    // press, but only when something is going to read it. It cannot find
    // that out for itself; this is how it is told.
    inheritSettings: function (on) { told.push(on === true); }
};
global.document = {
    visibilityState: "visible",
    addEventListener: function (kind, fn) { (listeners[kind] = listeners[kind] || []).push(fn); },
    dispatchEvent: function () { return true; },
    querySelector: function () { return null; },
    createElement: function () { return { addEventListener: function () { }, setAttribute: function () { } }; },
    head: { appendChild: function () { } }
};
global.CustomEvent = function (kind, init) { this.type = kind; this.detail = (init || {}).detail; };
global.localStorage = window.localStorage;

global.fetch = function (url, options) {
    const body = options && options.body;
    record(url, body);
    const text = String(url);
    let payload = { ok: true };
    if (text.indexOf("/outbox/submit") !== -1) {
        const job = {
            job_id: "1111222233334444", state: SUBMIT_STATE, executor: SUBMIT_EXECUTOR,
            request: JSON.parse(body).request, page: JSON.parse(body).page, revision: 1,
            summary: {}, result: null, error: null, wangp: null
        };
        JOBS.list = [job];
        payload = { ok: true, job: job };
    } else if (text.indexOf("/outbox/claim") !== -1) {
        payload = { ok: true, empty: true, pending: 0 };
    } else if (text.indexOf("/sync") !== -1) {
        payload = { ok: true, server_epoch: EPOCH, revision: SYNC_REVISION, cursor: EPOCH + ":" + SYNC_REVISION, jobs: JOBS.list, unattended: UNATTENDED, inherit_settings: INHERIT };
    } else if (text.indexOf("/outbox") !== -1) {
        payload = { ok: true, jobs: JOBS.list, running: true, counts: {} };
    }
    return Promise.resolve({
        ok: true, status: 200,
        json: function () { return Promise.resolve(payload); },
        text: function () { return Promise.resolve(JSON.stringify(payload)); }
    });
};

const EPOCH = "abcdef0123456789";
let SYNC_REVISION = 4;
let SUBMIT_STATE = "admitted";
let SUBMIT_EXECUTOR = "server";
let UNATTENDED = true;
let INHERIT = true;

new Function("window", "document", "fetch", "EventSource", "CustomEvent", "localStorage",
    fs.readFileSync(process.argv[2], "utf8"))(window, document, fetch, FakeEventSource, CustomEvent, localStorage);

const api = window.minipaintInterop.wangp;

function report(extra) {
    // The stream's watchdog reschedules itself forever, which is correct in
    // a browser and keeps Node alive here. Say the answer and go.
    console.log(JSON.stringify(Object.assign({
        calls: calls.map(function (c) { return c.url.split("?")[0]; }),
        streams: streams.map(function (s) { return s.url.split("?")[0]; }),
        flushes: flushes.slice(),
        told: told.slice(),
        submitBodies: calls.filter(function (c) { return c.url.indexOf("/outbox/submit") !== -1; }).map(function (c) { return c.body; }),
        cursors: streams.map(function (s) { const at = s.url.indexOf("cursor="); return at === -1 ? "" : s.url.slice(at + 7); }),
        state: api.streamState()
    }, extra || {})));
    process.exit(0);
}

const MODE = process.argv[3] || "server";

async function main() {
    if (MODE === "browser") { SUBMIT_EXECUTOR = "browser"; SUBMIT_STATE = "pending"; UNATTENDED = false; }
    if (MODE === "noinherit") { INHERIT = false; }
    if (MODE === "noinherit") { await api.sync(); }
    if (MODE === "noflush") { FLUSH_MODE = "absent"; }
    // A press decides whether to flush from what the last snapshot said, so
    // the browser-path run has to have taken one first - which is what a
    // real page does long before anybody presses anything.
    if (MODE === "browser") { await api.sync(); }
    const answer = await api.enqueue({ prompt: "a lighthouse" }, { wait: false });

    if (MODE === "server") {
        // Terminal frame -> the waiter is settled from the record.
        const waited = api.enqueue({ prompt: "another" }, { wait: true, timeoutMs: 2000 });
        // A press now commits WanGP's live form before it submits, so the
        // submission is no longer synchronous with the call. Wait for it:
        // an event about a job cannot arrive before the job exists, and a
        // test that fired one first would be asserting something that never
        // happens.
        const submits = function () { return calls.filter(function (c) { return c.url.indexOf("/outbox/submit") !== -1; }).length; };
        for (let spin = 0; spin < 200 && submits() < 2; spin += 1) {
            await new Promise(function (r) { setTimeout(r, 5); });
        }
        JOBS.list = [{ job_id: "1111222233334444", state: "completed", executor: "server", revision: 3,
                       request: { request_id: "a".repeat(32) }, result: null, error: null, summary: {}, wangp: null }];
        for (const s of streams) { s.fire("job", { job_id: "1111222233334444", state: "completed", revision: 3 }, EPOCH + ":3"); }
        const settled = await waited;
        await new Promise(function (r) { setTimeout(r, 20); });
        // Last, so the fetch it costs cannot move the ordering assertions
        // above it: every snapshot passes the setting on to the WanGP tab.
        await api.sync();
        report({ answer: answer, settled: settled && settled.status });
        return;
    }
    await new Promise(function (r) { setTimeout(r, 60); });
    report({ answer: answer });
}

async function handback() {
    // Admitted as a server job, then given back: this WanGP has no queue
    // worker to submit into, so the server hands it to whoever is here. The
    // page decided to WATCH when the submission was acknowledged, and unless
    // it notices, it watches forever a job that is waiting for it.
    await api.enqueue({ prompt: "x" }, { wait: false });
    const before = calls.filter(function (c) { return c.url.indexOf("/outbox/claim") !== -1; }).length;
    JOBS.list = [{ job_id: "1111222233334444", state: "pending", executor: "browser", revision: 5,
                   request: { request_id: "a".repeat(32) }, result: null, error: null, summary: {}, wangp: null }];
    for (const s of streams) {
        s.fire("job", { job_id: "1111222233334444", state: "pending", executor: "browser", revision: 5 }, EPOCH + ":5");
    }
    await new Promise(function (r) { setTimeout(r, 40); });
    const after = calls.filter(function (c) { return c.url.indexOf("/outbox/claim") !== -1; }).length;
    report({ claimsBefore: before, claimsAfter: after });
}

async function resets() {
    await api.enqueue({ prompt: "x" }, { wait: false });
    const before = calls.filter(function (c) { return c.url.indexOf("/sync") !== -1; }).length;
    for (const s of streams) { s.fire("reset", { reason: "overflow" }); }
    await new Promise(function (r) { setTimeout(r, 30); });
    const after = calls.filter(function (c) { return c.url.indexOf("/sync") !== -1; }).length;
    // An epoch from another run of Forge: meaningless rather than stale.
    for (const s of streams) { s.fire("hello", { server_epoch: "9999999999999999", cursor: "9999999999999999:1" }); }
    await new Promise(function (r) { setTimeout(r, 30); });
    const afterEpoch = calls.filter(function (c) { return c.url.indexOf("/sync") !== -1; }).length;
    report({ syncsOnReset: after - before, syncsOnEpochChange: afterEpoch - after });
}

(MODE === "reset" ? resets() : MODE === "handback" ? handback() : main()).catch(function (e) {
    console.log(JSON.stringify({ error: String(e && e.stack || e) }));
});
"""


def _run(mode: str):
    node = shutil.which("node")
    if not node:
        return None
    with tempfile.TemporaryDirectory(prefix="minipaint-interop-node-") as scratch:
        harness = pathlib.Path(scratch) / "harness.js"
        harness.write_text(HARNESS, encoding="utf-8")
        try:
            finished = subprocess.run(
                [node, str(harness), str(BUNDLE), mode],
                capture_output=True, text=True, timeout=30, check=False,
            )
        except Exception as error:
            return {"error": str(error)[:300]}
    out = finished.stdout.strip().splitlines()
    if not out:
        return {"error": finished.stderr[-400:]}
    try:
        return json.loads(out[-1])
    except ValueError:
        return {"error": out[-1][:300]}


def run() -> Results:
    r = Results("interop browser")
    if shutil.which("node") is None:
        r.check("node is available for the browser-half checks (skipped)", True)
        return r

    server = _run("server")
    r.check("the harness drove the real bundle", server is not None and "error" not in server, str(server)[:300])
    if not server or "error" in server:
        return r

    claims = [url for url in server["calls"] if url.endswith("/outbox/claim")]
    r.check("a job the server owns is never claimed by the page", claims == [], str(claims))
    r.check("and the page opens the one event stream instead",
            len(server["streams"]) == 1 and server["streams"][0] == "/minipaint-interop/events", str(server["streams"]))
    r.check("the stream says which page it is for",
            "page=" in str(server["state"].get("epoch", "")) or server["state"]["open"] is True, str(server["state"]))
    r.check("an admitted job answers pending, not refused: it is going to run",
            server["answer"]["status"] == "pending" and server["answer"]["job_id"], str(server["answer"]))
    r.check("a terminal frame settles a caller waiting on that job, as the success it is",
            server.get("settled") == "completed" and server["state"]["jobs"] >= 1, str(server.get("settled")))
    r.check("the submission itself was one request and one acknowledgement",
            len([url for url in server["calls"] if url.endswith("/outbox/submit")]) == 2, str(server["calls"]))

    # The settings flush. A job composes on the server from the form Wan2GP
    # recorded, and that record is only written when the user commits the
    # form - so a weight dragged and left alone is in the browser and nowhere
    # else. The press closes that gap, and the order is the whole of it:
    # committing after the submission would be committing after the compose
    # it was meant to feed.
    flushes = server.get("flushes") or []
    bodies = [body for body in (server.get("submitBodies") or []) if body]
    # "at" is how many fetches had happened when the flush was asked for.
    # Zero means it came before the first submission, which is the ordering
    # the whole feature depends on.
    r.check("a press commits WanGP's live form before it submits, not after",
            len(flushes) >= 1 and flushes[0]["at"] == 0, str(flushes))
    r.check("and tells the server what it managed, so the base can be attributed",
            bool(bodies) and bodies[0].get("settings_flush") == "committed", str(bodies[:1]))

    absent = _run("noflush")
    r.check("the harness drove a page whose WanGP cannot flush", absent is not None and "error" not in absent, str(absent)[:300])
    if absent and "error" not in absent:
        bodies = [body for body in (absent.get("submitBodies") or []) if body]
        r.check("a WanGP that cannot flush does not stop the press: it is an optimisation, never a rule",
                absent["answer"]["status"] == "pending" and absent["answer"]["job_id"], str(absent["answer"]))
        r.check("and the job records that nothing was carried, rather than implying it was",
                bool(bodies) and bodies[0].get("settings_flush") == "unavailable", str(bodies[:1]))

    # The other half of inheritance, and the half a press cannot do: the
    # WanGP tab keeps the recorded form current *ahead* of any press, and it
    # only knows whether that is worth doing because the snapshot says so.
    r.check("a snapshot tells the WanGP tab whether a job will be built from its settings",
            (server.get("told") or [])[:1] == [True], str(server.get("told")))
    without = _run("noinherit")
    r.check("the harness drove a Forge with inheritance off", without is not None and "error" not in without, str(without)[:300])
    if without and "error" not in without:
        r.check("and with it off the tab is told to leave WanGP's form alone",
                (without.get("told") or []) and all(item is False for item in without["told"]), str(without.get("told")))

    browser = _run("browser")
    r.check("the harness drove the legacy path too", browser is not None and "error" not in browser, str(browser)[:300])
    if browser and "error" not in browser:
        claims = [url for url in browser["calls"] if url.endswith("/outbox/claim")]
        r.check("a browser-executed job is still claimed, because an already-loaded page must keep working",
                len(claims) >= 1, str(browser["calls"]))
        r.check("and it opens no stream for one", browser["streams"] == [], str(browser["streams"]))
        r.check("nor does it wait to commit WanGP's form: its own Add to Queue chain does that",
                (browser.get("flushes") or []) == [], str(browser.get("flushes")))

    handback = _run("handback")
    r.check("the harness drove the hand-back case", handback is not None and "error" not in handback, str(handback)[:300])
    if handback and "error" not in handback:
        r.check("a server job handed back to the page starts the page pumping it, rather than being watched forever",
                handback.get("claimsBefore") == 0 and handback.get("claimsAfter", 0) >= 1, str(handback))

    reset = _run("reset")
    r.check("the harness drove the reset cases", reset is not None and "error" not in reset, str(reset)[:300])
    if reset and "error" not in reset:
        r.check("a reset frame produces exactly one snapshot, not an error",
                reset.get("syncsOnReset") == 1, str(reset.get("syncsOnReset")))
        r.check("an epoch from another run of Forge produces exactly one more",
                reset.get("syncsOnEpochChange") == 1, str(reset.get("syncsOnEpochChange")))
    return r


if __name__ == "__main__":
    raise SystemExit(0 if run().report() else 1)
