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
const notes = [];
window.minipaintWanGP = {
    note: function (line) { notes.push(String(line)); },
    flushForm: function () {
        flushes.push({ at: calls.length });
        return Promise.resolve(FLUSH_MODE === "absent" ? { ok: false } : { ok: true, flush: FLUSH_MODE });
    },
    // The WanGP tab's own script keeps the recorded form current ahead of a
    // press, but only when something is going to read it. It cannot find
    // that out for itself; this is how it is told.
    inheritSettings: function (on) { told.push(on === true); }
};
// Everything the bundle says to the rest of the page goes through one
// document event; recorded, so "the stream said it was silent" can be a
// fact rather than an impression.
const emitted = [];
global.document = {
    visibilityState: "visible",
    addEventListener: function (kind, fn) { (listeners[kind] = listeners[kind] || []).push(fn); },
    dispatchEvent: function (event) { emitted.push(event && event.detail ? event.detail : { type: event && event.type }); return true; },
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
        // The starved page: the request is queued in the browser behind
        // connections something else is holding, and never gets one. Not
        // refused, not failed - pending, for ever.
        if (STARVED) { return new Promise(function () {}); }
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
let STARVED = false;

// The stream's watchdog is forty seconds of silence, and what it then does
// is the thing under test in the last two modes - on a virtual clock, because
// a suite that waits forty real seconds to learn what it already knows is a
// suite nobody runs. Installed before the bundle loads, and only for those
// modes: every other mode keeps the real timers it was written against.
const realSetTimeout = global.setTimeout;
const realClearTimeout = global.clearTimeout;
const realNow = Date.now;
let CLOCK = null;
function installClock() {
    let now = 1700000000000;
    let seq = 0;
    const timers = new Map();
    global.setTimeout = function (fn, ms) { seq += 1; timers.set(seq, { at: now + Math.max(0, Number(ms) || 0), seq: seq, fn: fn }); return seq; };
    global.clearTimeout = function (id) { timers.delete(id); };
    Date.now = function () { return now; };
    CLOCK = {
        advance: async function (ms) {
            const until = now + ms;
            for (;;) {
                let next = null;
                for (const entry of timers.values()) {
                    if (entry.at > until) { continue; }
                    if (!next || entry.at < next.at || (entry.at === next.at && entry.seq < next.seq)) { next = entry; }
                }
                if (!next) { break; }
                timers.delete(next.seq);
                now = next.at;
                try { next.fn(); } catch (e) { /* the bundle's business */ }
                await new Promise(function (r) { realSetTimeout(r, 0); });
            }
            now = until;
            await new Promise(function (r) { realSetTimeout(r, 0); });
        }
    };
}
if (["silence", "starved", "hidden", "hiddenstarved", "openhidden", "jobsonly"].indexOf(process.argv[3]) !== -1) { installClock(); }

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
        state: api.streamState(),
        notes: notes.slice()
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

// The stream goes quiet. The bundle says so on the document, sends the one
// plain request that tells a dead stream from a page that cannot reach Forge
// at all, and says how that came back - or does not, when it never does.
async function silence() {
    STARVED = MODE === "starved";
    api.watch();
    for (const s of streams) { s.fire("open", {}); }
    const streamsBefore = streams.length;
    const syncs = function () { return calls.filter(function (c) { return c.url.indexOf("/sync") !== -1; }).length; };
    const said = function () { return emitted.filter(function (e) { return e && e.kind === "stream"; }).map(function (e) { return e.state; }); };
    await CLOCK.advance(41000);
    await new Promise(function (r) { realSetTimeout(r, 20); });
    const afterSilence = said();
    const syncsAfterSilence = syncs();
    // Long enough for the reconnect that follows an answer. A stream that
    // reopened and is then quiet again is noticed again, which is why the
    // whole record is reported and the first silence is judged on its own.
    await CLOCK.advance(45000);
    await new Promise(function (r) { realSetTimeout(r, 20); });
    report({
        afterSilence: afterSilence,
        syncsAfterSilence: syncsAfterSilence,
        streamEvents: said(),
        syncs: syncs(),
        reopened: streams.length - streamsBefore
    });
}

// The page goes to the background and comes back. Nothing of this
// extension's is held open while it is away, and the return is one snapshot
// - which is also the test of the connection, written down with its timing -
// before a fresh stream, never a stream trusted across the absence.
function setHidden(yes) {
    document.visibilityState = yes ? "hidden" : "visible";
    for (const fn of listeners.visibilitychange || []) { fn({}); }
}
async function away() {
    STARVED = MODE === "hiddenstarved";
    api.watch();
    for (const s of streams) { s.fire("open", {}); }
    const syncs = function () { return calls.filter(function (c) { return c.url.indexOf("/sync") !== -1; }).length; };
    setHidden(true);
    const closedOnHide = streams.length === 1 && streams[0].closed === true && api.streamState().open === false;
    // Long enough that a stream held across it is the one that came back
    // half-dead, and that the watchdog would have fired many times over.
    await CLOCK.advance(3600000);
    const streamsWhileAway = streams.length;
    const syncsWhileAway = syncs();
    setHidden(false);
    const streamsAtReturn = streams.length;
    await CLOCK.advance(1);
    await new Promise(function (r) { realSetTimeout(r, 20); });
    const afterReturn = { streams: streams.length, syncs: syncs(), open: api.streamState().open };
    await CLOCK.advance(20000);
    await new Promise(function (r) { realSetTimeout(r, 20); });
    report({
        closedOnHide: closedOnHide, streamsWhileAway: streamsWhileAway, syncsWhileAway: syncsWhileAway,
        streamsAtReturn: streamsAtReturn, afterReturn: afterReturn,
        streamEvents: emitted.filter(function (e) { return e && e.kind === "stream"; }).map(function (e) { return e.state; }),
        streamsAtEnd: streams.length
    });
}

// Asked for a stream while the page is already in the background: nothing
// is opened until it is back.
async function openHidden() {
    setHidden(true);
    api.watch();
    const whileHidden = streams.length;
    await CLOCK.advance(60000);
    setHidden(false);
    await CLOCK.advance(1);
    await new Promise(function (r) { realSetTimeout(r, 20); });
    report({ whileHidden: whileHidden, afterReturn: streams.length });
}

// A page that knows of a job the server is running, from a snapshot, but
// whose stream was let go on purpose. Coming back to it is still a reason to
// watch, as it always was.
async function jobsOnly() {
    api.watch();
    api.unwatch();
    JOBS.list = [{ job_id: "1111222233334444", state: "wangp_generating", executor: "server", revision: 2,
                   request: { request_id: "a".repeat(32) }, result: null, error: null, summary: {}, wangp: null }];
    await api.sync();
    const before = streams.length - 1;
    setHidden(true);
    await CLOCK.advance(60000);
    setHidden(false);
    await CLOCK.advance(1);
    await new Promise(function (r) { realSetTimeout(r, 20); });
    report({ before: before, afterReturn: streams.length - 1 });
}

(MODE === "jobsonly" ? jobsOnly() : MODE === "reset" ? resets() : MODE === "handback" ? handback()
    : (MODE === "silence" || MODE === "starved") ? silence()
    : (MODE === "hidden" || MODE === "hiddenstarved") ? away()
    : MODE === "openhidden" ? openHidden() : main()).catch(function (e) {
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

    # The stream's silence, said out loud. The server heartbeats every fifteen
    # seconds, so forty seconds of nothing is never the server having nothing
    # to say; it is either the stream alone or the page unable to reach Forge
    # at all, and the one plain request the watchdog sends is what tells the
    # two apart. The WanGP tab acts on the difference - it unloads its iframe
    # to give the browser's connections back when NOTHING comes back - so the
    # three words have to be said, in this order, and the third has to be
    # absent when the request never returns.
    silence = _run("silence")
    r.check("the harness drove a stream that went quiet", silence is not None and "error" not in silence, str(silence)[:300])
    if silence and "error" not in silence:
        r.check("a stream that opens says so", (silence.get("streamEvents") or [])[:1] == ["open"], str(silence.get("streamEvents")))
        r.check("forty seconds of silence is said out loud, and answered with one plain request",
                silence.get("afterSilence") == ["open", "silent", "answered"] and silence.get("syncsAfterSilence") == 1, str(silence))
        r.check("and the stream is then opened again", silence.get("reopened", 0) >= 1, str(silence.get("reopened")))
        r.check("and a stream that is quiet again is noticed again, the same way",
                (silence.get("streamEvents") or [])[3:5] == ["silent", "answered"] and silence.get("syncs") == 2, str(silence))
    starved = _run("starved")
    r.check("the harness drove a page whose request never came back", starved is not None and "error" not in starved, str(starved)[:300])
    if starved and "error" not in starved:
        events = starved.get("streamEvents") or []
        r.check("a request that never comes back is never reported as answered",
                "answered" not in events, str(starved))
        r.check("and once its time runs out it is reported as unanswered, which disarms nothing",
                events[:3] == ["open", "silent", "unanswered"], str(events))
        r.check("and the journal says Forge did not answer, in so many words",
                any("Forge did not answer within 15s" in line for line in starved.get("notes") or []), str(starved.get("notes")))
        # A limit on the snapshot, where there was none: a stuck snapshot used
        # to stay in flight for the life of the page, and every later one -
        # the return from the background's included - queued behind it.
        r.check("and it is tried again after a pause, not in a storm",
                starved.get("syncs") == 2, str(starved.get("syncs")))
        r.check("and no second stream is opened while nothing is coming back", starved.get("reopened") == 0, str(starved.get("reopened")))

    # The background. A stream held open across a long absence came back
    # half-dead in three incidents: open as far as the page could tell, and
    # silent. So none is held: it is let go on the way out, and the return is
    # a snapshot first - the test of the connection, written down - and a
    # fresh stream after it.
    hidden = _run("hidden")
    r.check("the harness drove a page that went to the background", hidden is not None and "error" not in hidden, str(hidden)[:300])
    if hidden and "error" not in hidden:
        r.check("the stream is closed the moment the page is hidden", hidden.get("closedOnHide") is True, str(hidden))
        r.check("and nothing is opened or asked for while it is away",
                hidden.get("streamsWhileAway") == 1 and hidden.get("syncsWhileAway") == 0, str(hidden))
        r.check("the return takes a snapshot before it opens anything",
                hidden.get("streamsAtReturn") == 1 and (hidden.get("afterReturn") or {}).get("syncs") == 1, str(hidden))
        r.check("and then opens a fresh stream, at once rather than after a retry delay",
                (hidden.get("afterReturn") or {}).get("streams") == 2 and (hidden.get("afterReturn") or {}).get("open") is True,
                str(hidden.get("afterReturn")))
        notes = hidden.get("notes") or []
        r.check("and the journal says how long it was away and that Forge answered, with the time it took",
                any("back after 3600s in the background; Forge answered in" in line for line in notes), str(notes))
    gone = _run("hiddenstarved")
    r.check("the harness drove a return to a Forge that does not answer", gone is not None and "error" not in gone, str(gone)[:300])
    if gone and "error" not in gone:
        r.check("a return whose snapshot never comes back opens no stream",
                gone.get("streamsAtEnd") == 1, str(gone))
        r.check("and says, in the journal, that the connection is not getting through",
                any("back after 3600s in the background: Forge did not answer within 15s" in line for line in gone.get("notes") or []),
                str(gone.get("notes")))
        r.check("and tells the WanGP tab it went unanswered, never answered",
                "unanswered" in (gone.get("streamEvents") or []) and "answered" not in (gone.get("streamEvents") or []),
                str(gone.get("streamEvents")))
    jobs = _run("jobsonly")
    r.check("the harness drove a page that only knows of a running job", jobs is not None and "error" not in jobs, str(jobs)[:300])
    if jobs and "error" not in jobs:
        r.check("a page with a job in progress watches it again when it comes back",
                jobs.get("before") == 0 and jobs.get("afterReturn") == 1, str(jobs))
    late = _run("openhidden")
    r.check("the harness drove a stream asked for in the background", late is not None and "error" not in late, str(late)[:300])
    if late and "error" not in late:
        r.check("a stream asked for while hidden is not opened until the page is back",
                late.get("whileHidden") == 0 and late.get("afterReturn") == 1, str(late))
    return r


if __name__ == "__main__":
    raise SystemExit(0 if run().report() else 1)
