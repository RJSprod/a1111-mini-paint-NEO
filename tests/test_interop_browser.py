"""The browser half of walking away: nothing held open, and safe to close.

The server half of "press Add to Queue and walk away" is asserted in
``test_clipboard_executor``, with no page anywhere near it. This is the other
half. The page used to be a *pump* - it claimed a job, drove the live WanGP
form and reported back - and then a *watcher*, holding an event stream open
for the life of the page. A connection held open while it waits on nothing is
the one that came back half-dead in every incident that locked the page up,
so the page now holds none. What is asserted here:

*   an admitted server job answers at once, as pending with its id - the
    server has it, and nothing waits on it or watches it afterwards;
*   no event stream is ever opened, by a job or by anything else;
*   a snapshot has a limit: one that never comes back answers SYNC_TIMEOUT
    instead of holding every later snapshot behind it for ever;
*   a return from a real absence takes exactly one snapshot and writes down
    whether Forge answered, and how fast; a tab flick takes none;
*   a page takes one snapshot when it loads, unasked, so that the WanGP tab
    is told whether jobs are built from its settings - before this existed
    the first snapshot waited on a listener only a snapshot could install,
    and none was ever taken;
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
        flushes.push({ at: calls.length, how: "flushForm" });
        return Promise.resolve(FLUSH_MODE === "absent" ? { ok: false } : { ok: true, flush: FLUSH_MODE });
    },
    // The save every send path shares: two seconds at most, and none at all
    // when the WanGP page says its record is already current.
    saveForSend: function (reason) {
        flushes.push({ at: calls.length, how: "saveForSend", reason: String(reason) });
        return Promise.resolve({ ok: true, flush: FLUSH_MODE === "absent" ? "unavailable" : FLUSH_MODE });
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
if (["starved", "hidden", "hiddenstarved", "flick", "bootstarved"].indexOf(process.argv[3]) !== -1) { installClock(); }

new Function("window", "document", "fetch", "EventSource", "CustomEvent", "localStorage",
    fs.readFileSync(process.argv[2], "utf8"))(window, document, fetch, FakeEventSource, CustomEvent, localStorage);

const api = window.minipaintInterop.wangp;

function report(extra) {
    // Said and gone: a pending timer of the bundle's would otherwise keep
    // Node alive after the answer is known.
    console.log(JSON.stringify(Object.assign({
        calls: calls.map(function (c) { return c.url.split("?")[0]; }),
        streams: streams.map(function (s) { return s.url.split("?")[0]; }),
        flushes: flushes.slice(),
        told: told.slice(),
        submitBodies: calls.filter(function (c) { return c.url.indexOf("/outbox/submit") !== -1; }).map(function (c) { return c.body; }),
        state: api.snapshotState(),
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
        // The default is to wait - and for a job the server runs, the wait
        // is over the moment the server has it. Bounded here on the real
        // clock, so a build that waited for the job to END is a result and
        // not a hang.
        const waited = await Promise.race([
            api.enqueue({ prompt: "another" }, { wait: true }),
            new Promise(function (r) { setTimeout(function () { r({ status: "still waiting" }); }, 1500); })
        ]);
        await new Promise(function (r) { setTimeout(r, 20); });
        const requestsAfter = calls.length;
        await new Promise(function (r) { setTimeout(r, 200); });
        // Last, so the fetch it costs cannot move the ordering assertions
        // above it: every snapshot passes the setting on to the WanGP tab.
        await api.sync();
        report({ answer: answer, waited: waited && waited.status, quietAfter: calls.length === requestsAfter + 1 });
        return;
    }
    await new Promise(function (r) { setTimeout(r, 60); });
    report({ answer: answer });
}

// A snapshot that never comes back. It answers SYNC_TIMEOUT when its time
// runs out, and the next one is sent rather than queued behind it.
async function starved() {
    // The page's own snapshot at load comes first, as it does on a real page;
    // on this clock it would otherwise be taken in the middle of the scenario.
    await CLOCK.advance(1);
    const atLoad = calls.filter(function (c) { return c.url.indexOf("/sync") !== -1; }).length;
    STARVED = true;
    const first = api.sync();
    await CLOCK.advance(16000);
    const answer = await first;
    STARVED = false;
    const second = await api.sync();
    report({ first: answer && answer.code, second: second && second.ok,
             syncs: calls.filter(function (c) { return c.url.indexOf("/sync") !== -1; }).length - atLoad });
}

// A page that has only loaded: nobody pressed, nobody came back from
// anywhere. It takes exactly one snapshot, and the WanGP tab hears from it.
async function booted() {
    if (MODE === "bootstarved") { STARVED = true; }
    await new Promise(function (r) { realSetTimeout(r, 50); });
    if (CLOCK) { await CLOCK.advance(16000); }
    await new Promise(function (r) { realSetTimeout(r, 20); });
    report({ syncs: syncs() });
}

// The page goes to the background and comes back. Nothing was open to
// close; the return from a real absence is one snapshot, written down with
// whether Forge answered and how long it took. A tab flick is nothing.
function setHidden(yes) {
    document.visibilityState = yes ? "hidden" : "visible";
    for (const fn of listeners.visibilitychange || []) { fn({}); }
}
const syncs = function () { return calls.filter(function (c) { return c.url.indexOf("/sync") !== -1; }).length; };
async function away() {
    // The snapshot a page takes when it loads, first, as on a real page: on
    // this clock it would otherwise be taken while the page is "away".
    await CLOCK.advance(1);
    await api.sync();
    STARVED = MODE === "hiddenstarved";
    const before = syncs();
    setHidden(true);
    await CLOCK.advance(MODE === "flick" ? 5000 : 3600000);
    const whileAway = syncs() - before;
    setHidden(false);
    await CLOCK.advance(1);
    await new Promise(function (r) { realSetTimeout(r, 20); });
    await CLOCK.advance(16000);
    await new Promise(function (r) { realSetTimeout(r, 20); });
    report({ whileAway: whileAway, onReturn: syncs() - before });
}

(MODE === "starved" ? starved()
    : (MODE === "hidden" || MODE === "hiddenstarved" || MODE === "flick") ? away()
    : (MODE === "boot" || MODE === "bootstarved") ? booted() : main()).catch(function (e) {
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
    r.check("and no event stream is opened for it, or for anything", server["streams"] == [], str(server["streams"]))
    r.check("an admitted job answers pending, not refused: it is going to run",
            server["answer"]["status"] == "pending" and server["answer"]["job_id"], str(server["answer"]))
    r.check("a caller that waits is answered the moment the server has the job, not when it ends",
            server.get("waited") == "pending", str(server.get("waited")))
    r.check("and nothing is asked about the job afterwards", server.get("quietAfter") is True, str(server["calls"]))
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
    r.check("through the same save the gallery and the Clipboard tab wait on, saying who asked",
            bool(flushes) and flushes[0].get("how") == "saveForSend" and flushes[0].get("reason") == "a queue request", str(flushes))

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

    # The snapshot's limit. Without one, a snapshot to a Forge that never
    # answered stayed in flight for the life of the page and every later one
    # queued behind it.
    starved = _run("starved")
    r.check("the harness drove a snapshot that never came back", starved is not None and "error" not in starved, str(starved)[:300])
    if starved and "error" not in starved:
        r.check("a snapshot that never comes back answers SYNC_TIMEOUT when its time runs out",
                starved.get("first") == "SYNC_TIMEOUT", str(starved))
        r.check("and the next one is sent, not queued behind it", starved.get("second") is True and starved.get("syncs") == 2, str(starved))
        r.check("and still no stream is opened", starved.get("streams") == [], str(starved.get("streams")))

    # The background. Nothing was open, so nothing is closed or reopened; a
    # return from a real absence is one snapshot and one line that says how
    # it went - the fact that tells a page that slept from a connection that
    # stopped working.
    hidden = _run("hidden")
    r.check("the harness drove a page that went to the background", hidden is not None and "error" not in hidden, str(hidden)[:300])
    if hidden and "error" not in hidden:
        r.check("nothing is asked for while the page is away", hidden.get("whileAway") == 0, str(hidden))
        r.check("the return from a real absence takes exactly one snapshot", hidden.get("onReturn") == 1, str(hidden))
        r.check("and the journal says how long it was away and that Forge answered, with the time it took",
                any("back after 3600s in the background; Forge answered in" in line for line in hidden.get("notes") or []),
                str(hidden.get("notes")))
        r.check("and no stream is opened on the way back", hidden.get("streams") == [], str(hidden.get("streams")))
    gone = _run("hiddenstarved")
    r.check("the harness drove a return to a Forge that does not answer", gone is not None and "error" not in gone, str(gone)[:300])
    if gone and "error" not in gone:
        r.check("a return whose snapshot never comes back says the connection is not getting through",
                any("back after 3600s in the background: Forge did not answer within 15s" in line for line in gone.get("notes") or []),
                str(gone.get("notes")))
    flick = _run("flick")
    r.check("the harness drove a tab flick", flick is not None and "error" not in flick, str(flick)[:300])
    if flick and "error" not in flick:
        r.check("a few seconds away is not a return worth a request", flick.get("onReturn") == 0, str(flick))

    # The load. PR #102 made every snapshot one a page asks for, and the only
    # thing that asked was a return from the background - through a listener
    # that only a snapshot installed. So no page ever took one, the WanGP tab
    # was never told that jobs are built from its settings, and a LoRA weight
    # changed there never reached a job sent from anywhere else.
    boot = _run("boot")
    r.check("the harness drove a page that only loaded", boot is not None and "error" not in boot, str(boot)[:300])
    if boot and "error" not in boot:
        r.check("a page takes exactly one snapshot when it loads, with nobody asking", boot.get("syncs") == 1, str(boot))
        r.check("and that snapshot tells the WanGP tab whether jobs are built from its settings",
                boot.get("told") == [True], str(boot.get("told")))
        r.check("and keeps what it said where a WanGP tab that loads later can read it",
                (boot.get("state") or {}).get("inherit") is True and (boot.get("state") or {}).get("unattended") is True,
                str(boot.get("state")))
        r.check("and the journal says it was taken, how fast, and what it said",
                any("snapshot: at page load, Forge answered in" in line and "built from the WanGP page's settings" in line
                    for line in boot.get("notes") or []), str(boot.get("notes")))
        r.check("and it opens no stream", boot.get("streams") == [], str(boot.get("streams")))
    silent = _run("bootstarved")
    r.check("the harness drove a page whose Forge does not answer at load", silent is not None and "error" not in silent, str(silent)[:300])
    if silent and "error" not in silent:
        r.check("a load snapshot that never comes back is bounded and says so, and is not repeated",
                silent.get("syncs") == 1 and any("snapshot: at page load, Forge did not answer within 15s" in line
                                                  for line in silent.get("notes") or []), str(silent))
        r.check("and the WanGP tab is told nothing it was not told", silent.get("told") == [], str(silent.get("told")))
    return r


if __name__ == "__main__":
    raise SystemExit(0 if run().report() else 1)
