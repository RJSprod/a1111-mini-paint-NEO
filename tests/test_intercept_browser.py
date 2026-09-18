"""The Send to WanGP popup, in Node against a small fake DOM.

The popup is the one screen of this feature, and the rules it keeps are
the design intent's: it opens on the picture the server froze and on
nothing else; it draws the roles Clipboard reports and ticks the defaults
Clipboard names; it lets several be ticked; Generate sends the prompt and
the switch as they stand on screen, the roles as ticked, the token, and the
page's identity; a refusal leaves it open with the sentence; Cancel sends
one cancel and no submit, and shares an edited prompt; a second picture
replaces the first and cancels it; the live page's facts, when the bridge
is here, travel with every ask; the Clipboard tab's own prompt box and
switch on the same page are what the popup starts from and mirrors into;
Escape cancels and Ctrl+Enter generates; the history's buttons post the
verbs they say. Nothing in here names a model.

Driven the way ``test_interop_browser`` drives the queue API: Node, a
stubbed window, every fetch recorded. The DOM is a few dozen lines of fake -
elements with children, classes, datasets, listeners and a selector
matcher for the handful of shapes the popup uses - because what is under
test is this file's logic, not a browser. The real popup on a real page is
``browser_intercept.py``.
"""

from harness import Results, ROOT, setup_path

setup_path()

import json  # noqa: E402
import pathlib  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402

BUNDLE = ROOT / "browser" / "minipaint_intercept.js"

HARNESS = r"""
const fs = require("fs");

/* ---------------------------------------------------------------- a DOM -- */

class FakeEvent {
    constructor(type, init) { this.type = type; this.bubbles = !!(init && init.bubbles); this.target = null; this.key = (init && init.key) || ""; this.ctrlKey = !!(init && init.ctrlKey); this.metaKey = false; this.defaultPrevented = false; }
    preventDefault() { this.defaultPrevented = true; }
    stopPropagation() { this.stopped = true; }
}

function matchesOne(node, selector) {
    const sel = selector.trim();
    if (!sel) { return false; }
    let rest = sel, tag = "", id = "", classes = [], attrs = [];
    const tagMatch = rest.match(/^[a-zA-Z][\w-]*/);
    if (tagMatch) { tag = tagMatch[0].toUpperCase(); rest = rest.slice(tagMatch[0].length); }
    for (;;) {
        let m;
        if ((m = rest.match(/^#([\w-]+)/))) { id = m[1]; rest = rest.slice(m[0].length); continue; }
        if ((m = rest.match(/^\.([\w-]+)/))) { classes.push(m[1]); rest = rest.slice(m[0].length); continue; }
        if ((m = rest.match(/^\[([\w-]+)(?:=([^\]]*))?\]/))) { attrs.push([m[1], m[2] === undefined ? null : m[2].replace(/^["']|["']$/g, "")]); rest = rest.slice(m[0].length); continue; }
        break;
    }
    if (tag && node.tagName !== tag) { return false; }
    if (id && node.id !== id) { return false; }
    for (const cls of classes) { if (!node.classList.contains(cls)) { return false; } }
    for (const [name, value] of attrs) {
        const held = name.indexOf("data-") === 0 ? node.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] : (name === "type" ? node.type : node.attributes[name]);
        if (held === undefined || held === null) { return false; }
        if (value !== null && String(held) !== value) { return false; }
    }
    return true;
}

function matchesChain(node, compounds) {
    // Descendant combinators: the node matches the last compound, and each
    // earlier one is matched by some ancestor, in order.
    if (!matchesOne(node, compounds[compounds.length - 1])) { return false; }
    let ancestor = node.parentNode;
    for (let i = compounds.length - 2; i >= 0; i -= 1) {
        while (ancestor && !matchesOne(ancestor, compounds[i])) { ancestor = ancestor.parentNode; }
        if (!ancestor) { return false; }
        ancestor = ancestor.parentNode;
    }
    return true;
}

function matches(node, selector) {
    return selector.split(",").some(function (part) {
        const compounds = part.trim().split(/\s+/).filter(Boolean);
        return compounds.length ? matchesChain(node, compounds) : false;
    });
}

class Node {
    constructor(tag) {
        this.tagName = String(tag).toUpperCase();
        this.children = [];
        this.parentNode = null;
        this.attributes = {};
        this.dataset = {};
        this.style = { setProperty: function () { } };
        this._listeners = {};
        this._classes = new Set();
        this._text = "";
        this.hidden = false;
        this.disabled = false;
        this.value = "";
        this.checked = false;
        this.title = "";
        this.id = "";
        this.type = "";
        this.isConnected = true;
        this.offsetHeight = 300;
        const node = this;
        this.classList = {
            add: function (c) { node._classes.add(c); },
            remove: function (c) { node._classes.delete(c); },
            toggle: function (c, on) { if (on === undefined) { on = !node._classes.has(c); } if (on) { node._classes.add(c); } else { node._classes.delete(c); } return on; },
            contains: function (c) { return node._classes.has(c); }
        };
    }
    get className() { return Array.from(this._classes).join(" "); }
    set className(text) { this._classes = new Set(String(text).split(/\s+/).filter(Boolean)); }
    get textContent() { return this._text + this.children.map(function (c) { return c.textContent; }).join(""); }
    set textContent(text) { this._text = String(text); this.children = []; }
    appendChild(child) { if (child.parentNode) { child.parentNode.removeChild(child); } child.parentNode = this; this.children.push(child); return child; }
    removeChild(child) { const at = this.children.indexOf(child); if (at !== -1) { this.children.splice(at, 1); child.parentNode = null; } return child; }
    remove() { if (this.parentNode) { this.parentNode.removeChild(this); } }
    setAttribute(name, value) { this.attributes[name] = String(value); if (name === "id") { this.id = String(value); } }
    getAttribute(name) { return this.attributes[name] === undefined ? null : this.attributes[name]; }
    addEventListener(kind, fn) { (this._listeners[kind] = this._listeners[kind] || []).push(fn); }
    removeEventListener(kind, fn) { const list = this._listeners[kind] || []; const at = list.indexOf(fn); if (at !== -1) { list.splice(at, 1); } }
    dispatchEvent(event) {
        if (!event.target) { event.target = this; }
        for (const fn of (this._listeners[event.type] || []).slice()) { fn(event); }
        if (event.bubbles && this.parentNode && !event.stopped) { this.parentNode.dispatchEvent(event); }
        return !event.defaultPrevented;
    }
    click() { return this.dispatchEvent(new FakeEvent("click", { bubbles: true })); }
    focus() { }
    contains(node) { for (let n = node; n; n = n.parentNode) { if (n === this) { return true; } } return false; }
    closest(selector) { for (let n = this; n; n = n.parentNode) { if (n.matches && n.matches(selector)) { return n; } } return null; }
    matches(selector) { return matches(this, selector); }
    querySelectorAll(selector) {
        const found = [];
        const walk = function (node) { for (const child of node.children) { if (matches(child, selector)) { found.push(child); } walk(child); } };
        walk(this);
        return found;
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    getBoundingClientRect() { return { top: 100, left: 50, bottom: 130, right: 90, width: 40, height: 30 }; }
}

const body = new Node("body");
const docListeners = {};
const document = {
    body: body,
    documentElement: body,
    visibilityState: "visible",
    createElement: function (tag) { return new Node(tag); },
    getElementById: function (id) { return body.querySelector("#" + id); },
    querySelector: function (selector) { return body.querySelector(selector); },
    querySelectorAll: function (selector) { return body.querySelectorAll(selector); },
    addEventListener: function (kind, fn) { (docListeners[kind] = docListeners[kind] || []).push(fn); },
    removeEventListener: function (kind, fn) { const list = docListeners[kind] || []; const at = list.indexOf(fn); if (at !== -1) { list.splice(at, 1); } },
    dispatchEvent: function (event) { for (const fn of (docListeners[event.type] || []).slice()) { fn(event); } return true; }
};
global.document = document;
global.Event = FakeEvent;
global.CustomEvent = FakeEvent;

/* ---------------------------------------------------------- the window -- */

const calls = [];
const loaded = [];
let SESSION = {};
const window = {
    innerWidth: 1400, innerHeight: 900,
    sessionStorage: { getItem: function (k) { return SESSION[k] === undefined ? null : SESSION[k]; }, setItem: function (k, v) { SESSION[k] = String(v); } },
    crypto: { getRandomValues: function (bytes) { for (let i = 0; i < bytes.length; i++) { bytes[i] = (i * 13 + 5) % 256; } return bytes; } },
    HTMLInputElement: { prototype: {} },
    minipaintAssets: { load: function (urls) { loaded.push(urls); return Promise.resolve(true); } }
};
global.window = window;
window.document = document;

/* ---------------------------------------------------------- the server -- */

const MODE = process.argv[3] || "open";
const H1 = "wangp:" + "a".repeat(32) + ":64x48:txt2img";
const H2 = "wangp:" + "b".repeat(32) + ":32x32:img2img";
const ALL = [{ id: "first_frame", label: "First Frame" }, { id: "last_frame", label: "Last Frame" }, { id: "reference", label: "Reference" }];
const TWO = [{ id: "first_frame", label: "First Frame" }, { id: "last_frame", label: "Last Frame" }];

function describeAnswer(body) {
    const withFacts = !!(body.model || body.inputs);
    return {
        ok: true, token: body.handoff.split(":")[1],
        image: { width: 64, height: 48, tab: "txt2img" },
        capabilities: { roles: withFacts ? TWO : ALL, default_roles: withFacts ? ["last_frame"] : ["reference"], inherit_supported: true, known: withFacts },
        prompt: "the server's prompt",
        enhance: { enabled: true, state: "ready", text: "ready line" },
        inherit: { default: false, supported: true, draft: [{ role: "first_frame", label: "First Frame", names: ["forest.png"] }] },
        wangp: { state: "idle", running: true, generating: false, text: "WanGP is idle" },
        generate: { label: "Generate", enabled: true, reason: "" },
        executor: "server",
        history: MODE === "history" ? HISTORY : []
    };
}

//: As the server lists it: pinned first, then the rest, newest first.
const HISTORY = [
    { id: "2222222222222222", when: "yesterday", prompt: "pinned one", enhance: true, inherit: false,
      roles: [{ id: "first_frame", label: "First Frame", valid: true }], summary: "s", pinned: true, model: "", outcome: "", image: {} },
    { id: "1111111111111111", when: "today", prompt: "an earlier prompt", enhance: false, inherit: true,
      roles: [{ id: "reference", label: "Reference", valid: true }], summary: "s", pinned: false, model: "", outcome: "Queued", image: {} }
];

global.fetch = function (url, options) {
    const text = String(url);
    const body = options && typeof options.body === "string" ? JSON.parse(options.body) : null;
    calls.push({ url: text, body: body, method: (options && options.method) || "GET" });
    let payload = { ok: true };
    if (text.indexOf("/minipaint-clipboard/intercept") === 0) {
        const action = body.action;
        if (action === "describe") { payload = describeAnswer(body); }
        else if (action === "submit") {
            payload = MODE === "refused"
                ? { ok: false, code: "WANGP_NOT_RUNNING", message: "WanGP is not running.", notes: ["nothing was stored"] }
                : { ok: true, job_id: "0123456789abcdef", instruction: { nonce: "n", job_id: "0123456789abcdef", executor: "server", state: "admitted" },
                    status: "Queued on the server.", notes: ["it goes next"], roles: body.roles, dropped_roles: [], history: [] };
        }
        else if (action === "history_load") {
            payload = { ok: true, id: body.id, prompt: "loaded prompt", enhance: false, inherit: true, roles: ["reference"], dropped_roles: [], defaulted: false,
                        capabilities: { roles: ALL, default_roles: ["reference"] } };
        }
        else if (action === "history_pin" || action === "history_delete") { payload = { ok: true, history: HISTORY }; }
        else { payload = { ok: true, released: true }; }
    } else if (text.indexOf("/minipaint-interop/stage") === 0) {
        payload = { ok: true, image: { kind: "staged", id: "c".repeat(32) }, width: 80, height: 60 };
    } else if (text.indexOf("/gallery/") === 0) {
        return Promise.resolve({ ok: true, status: 200, blob: function () { return Promise.resolve({ type: "image/png", size: 3 }); } });
    }
    return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(payload); } });
};

/* ---------------------------------------------------------- the bridge -- */

if (MODE === "facts") {
    window.minipaintWanGP = {
        state: function () { return { present: true, ready: true, model: { type: "m", label: "M", family: "", architecture: "" } }; },
        capabilities: function () { return Promise.resolve({ ok: true, model: { type: "m", label: "M" }, generation_running: true,
            inputs: { start: { supported: true }, end: { supported: true }, references: { supported: false } } }); },
        note: function () { }
    };
    window.minipaintInterop = { wangp: { capabilities: function () { return window.minipaintWanGP.capabilities(); }, pageId: function () { return "d".repeat(32); } } };
}
if (MODE === "shared") {
    // The Clipboard tab, attached on this page, with its own prompt box and switch.
    const host = new Node("div"); host.setAttribute("id", "minipaint_clipboard_prompt");
    const box = new Node("textarea"); box.value = "from the tab"; host.appendChild(box); body.appendChild(host);
    const toggleHost = new Node("div"); toggleHost.setAttribute("id", "minipaint_clipboard_enhance_toggle");
    const toggle = new Node("input"); toggle.type = "checkbox"; toggle.checked = false; toggleHost.appendChild(toggle); body.appendChild(toggleHost);
    window.minipaintClipboard = { debug: function () { return { attached: true }; }, queue: function (text) { window.__queued = text; }, refreshQueue: function () { window.__refreshed = true; } };
    window.minipaintWriteInput = function (element, value) { element.value = String(value); window.__written = String(value); return true; };
}

new Function("window", "document", "fetch", "Event", "CustomEvent",
    fs.readFileSync(process.argv[2], "utf8"))(window, document, fetch, FakeEvent, FakeEvent);

const popup = window.minipaintIntercept;

function tick(ms) { return new Promise(function (r) { setTimeout(r, ms || 15); }); }
function posted(action) { return calls.filter(function (c) { return c.url.indexOf("/minipaint-clipboard/intercept") === 0 && c.body && c.body.action === action; }); }
function root() { return body.querySelector(".minipaint-intercept"); }
function roleBoxes() { return root().querySelectorAll(".minipaint-intercept-roles input"); }
function tickRole(id, on) { const box = roleBoxes().filter(function (b) { return b.dataset.role === id; })[0]; box.checked = on !== false; box.dispatchEvent(new FakeEvent("change")); }
function report(extra) {
    console.log(JSON.stringify(Object.assign({
        state: popup.state(),
        describes: posted("describe").map(function (c) { return c.body; }),
        submits: posted("submit").map(function (c) { return c.body; }),
        cancels: posted("cancel").map(function (c) { return c.body.handoff; }),
        drafts: posted("draft").map(function (c) { return c.body.prompt; }),
        loaded: loaded,
        hidden: root() ? root().hidden : null,
        toast: (function () { const t = body.querySelector(".minipaint-intercept-toast"); return t && !t.hidden ? t.textContent : ""; })(),
        message: (function () { const m = root() && root().querySelector(".minipaint-intercept-message"); return m && !m.hidden ? m.textContent : ""; })(),
        roleIds: root() ? roleBoxes().map(function (b) { return b.dataset.role + ":" + (b.checked ? "on" : "off"); }) : [],
        calls: calls.map(function (c) { return c.url.split("?")[0] + (c.body && c.body.action ? "#" + c.body.action : ""); })
    }, extra || {})));
    process.exit(0);
}

async function main() {
    if (MODE === "open") {
        popup.open(H1, { bundles: { wangp: "/w.js", interop: "/i.js" } });
        await tick(40);
        const before = JSON.parse(JSON.stringify(popup.state()));
        root().querySelector(".minipaint-intercept-prompt").value = "typed in the popup";
        root().querySelector(".minipaint-intercept-prompt").dispatchEvent(new FakeEvent("input"));
        tickRole("first_frame", true);
        await popup.generate();
        await tick(20);
        // The chained browser step runs after a failed receive too, with the
        // previous press's handoff: the popup must not open on it again.
        const reopened = popup.open(H1, {});
        await tick(20);
        report({ before: before, reopened: reopened, describesAfter: posted("describe").length });
    }
    if (MODE === "norole") {
        popup.open(H1, {});
        await tick(40);
        tickRole("reference", false);
        const attempt = await popup.generate();
        await tick(10);
        report({ attempt: attempt });
    }
    if (MODE === "refused") {
        popup.open(H1, {});
        await tick(40);
        await popup.generate();
        await tick(20);
        report();
    }
    if (MODE === "cancel") {
        popup.open(H1, {});
        await tick(40);
        root().querySelector(".minipaint-intercept-prompt").value = "edited then cancelled";
        root().querySelector(".minipaint-intercept-prompt").dispatchEvent(new FakeEvent("input"));
        popup.cancel();
        await tick(30);
        report();
    }
    if (MODE === "replace") {
        popup.open(H1, {});
        await tick(40);
        root().querySelector(".minipaint-intercept-prompt").value = "kept across";
        root().querySelector(".minipaint-intercept-prompt").dispatchEvent(new FakeEvent("input"));
        popup.open(H2, {});
        await tick(40);
        report();
    }
    if (MODE === "facts") {
        popup.open(H1, {});
        await tick(80);
        await popup.generate();
        await tick(20);
        report();
    }
    if (MODE === "shared") {
        popup.open(H1, {});
        await tick(40);
        const opened = JSON.parse(JSON.stringify(popup.state()));
        root().querySelector(".minipaint-intercept-prompt").value = "mirrored out";
        root().querySelector(".minipaint-intercept-prompt").dispatchEvent(new FakeEvent("input"));
        const enhance = root().querySelector(".minipaint-intercept-enhance");
        enhance.checked = true;
        enhance.dispatchEvent(new FakeEvent("change"));
        await tick(20);
        const toggle = body.querySelector("#minipaint_clipboard_enhance_toggle input");
        await popup.generate();
        await tick(20);
        report({ opened: opened, written: window.__written, toggleChecked: toggle.checked, queued: window.__queued, refreshed: !!window.__refreshed,
                 enhanceSaves: calls.filter(function (c) { return c.url.indexOf("enhance-settings") !== -1; }).map(function (c) { return c.body; }) });
    }
    if (MODE === "keys") {
        popup.open(H1, {});
        await tick(40);
        const escape = new FakeEvent("keydown", { key: "Escape" });
        document.dispatchEvent(escape);
        await tick(20);
        const closedByEscape = root().hidden;
        popup.open(H2, {});
        await tick(40);
        const enter = new FakeEvent("keydown", { key: "Enter", ctrlKey: true });
        enter.target = root().querySelector(".minipaint-intercept-prompt");
        document.dispatchEvent(enter);
        await tick(40);
        report({ closedByEscape: closedByEscape });
    }
    if (MODE === "history") {
        popup.open(H1, {});
        await tick(40);
        root().querySelector(".minipaint-intercept-history-toggle").click();
        const rows = root().querySelectorAll(".minipaint-intercept-entry").map(function (row) { return row.dataset.history + (row.classList.contains("minipaint-intercept-entry-pinned") ? ":pinned" : ""); });
        const loadButton = root().querySelectorAll("[data-history-action=load]")[0];
        loadButton.click();
        await tick(30);
        const afterLoad = JSON.parse(JSON.stringify(popup.state()));
        root().querySelectorAll("[data-history-action=pin]")[0].click();
        await tick(20);
        root().querySelectorAll("[data-history-action=delete]").slice(-1)[0] && root().querySelectorAll("[data-history-action=delete]").slice(-1)[0].click();
        await tick(20);
        report({ rows: rows, afterLoad: afterLoad,
                 pins: posted("history_pin").map(function (c) { return [c.body.id, c.body.pinned]; }),
                 deletes: posted("history_delete").map(function (c) { return c.body.id; }),
                 loads: posted("history_load").map(function (c) { return c.body.id; }) });
    }
    if (MODE === "stage") {
        const ok = await popup.stageAndOpen("/gallery/one.png", "extras", {});
        await tick(40);
        report({ staged: ok, stageCalls: calls.filter(function (c) { return c.url.indexOf("/minipaint-interop/stage") === 0; }).length });
    }
}

main().catch(function (e) { console.log(JSON.stringify({ error: String(e && e.stack || e) })); process.exit(0); });
"""


def _run(mode: str):
    node = shutil.which("node")
    if not node:
        return None
    with tempfile.TemporaryDirectory(prefix="minipaint-intercept-node-") as scratch:
        harness = pathlib.Path(scratch) / "harness.js"
        harness.write_text(HARNESS, encoding="utf-8")
        try:
            finished = subprocess.run([node, str(harness), str(BUNDLE), mode], capture_output=True, text=True, timeout=30, check=False)
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
    r = Results("intercept browser")
    if shutil.which("node") is None:
        r.check("node is available for the popup checks (skipped)", True)
        return r
    text = BUNDLE.read_text(encoding="utf-8").lower()
    r.check("these checks and the popup name no model family",
            not any(name in text for name in ("ref2va", "fl2va", "minimax")) and not any(name in HARNESS.lower() for name in ("ref2va", "fl2va", "minimax")))

    opened = _run("open")
    r.check("the harness drove the real bundle", opened is not None and "error" not in opened, str(opened)[:300])
    if opened and "error" not in opened:
        before = opened.get("before") or {}
        r.check("the popup opens and asks Clipboard what it may offer, for the picture it was handed",
                len(opened["describes"]) >= 1 and opened["describes"][0]["handoff"] == "wangp:" + "a" * 32 + ":64x48:txt2img" and before.get("open") is True)
        r.check("it draws the roles Clipboard reports and ticks the default Clipboard names",
                before.get("roles", {}).get("available") == ["first_frame", "last_frame", "reference"] and before.get("roles", {}).get("selected") == ["reference"],
                str(before.get("roles")))
        r.check("it opens with Clipboard's prompt and switch, the inheritance choice as last used, and WanGP's state",
                before.get("prompt") == "the server's prompt" and before.get("enhance") is True and before.get("inherit") is False and before.get("wangp") == "idle",
                str({k: before.get(k) for k in ("prompt", "enhance", "inherit", "wangp")}))
        r.check("it fetches the bridge and the queue API beside itself, once", opened["loaded"] == [["/w.js", "/i.js"]], str(opened["loaded"]))
        submit = opened["submits"][0] if opened["submits"] else {}
        r.check("Generate sends the prompt as it stands on screen, not the one it opened with", submit.get("prompt") == "typed in the popup", str(submit)[:200])
        r.check("every ticked role, the frozen token, the switch and the inheritance choice, and this page's identity",
                submit.get("roles") == ["first_frame", "reference"] and submit.get("handoff") == "wangp:" + "a" * 32 + ":64x48:txt2img"
                and submit.get("enhance") is True and submit.get("inherit") is False and len(str(submit.get("page", ""))) == 32, str(submit)[:200])
        r.check("and closes the moment the server has stored it, saying so", opened["hidden"] is True and opened["state"]["open"] is False
                and opened["toast"].startswith("Queued on the server.") and "it goes next" in opened["toast"], str(opened["toast"]))
        r.check("no cancel was sent for a picture that was generated", opened["cancels"] == [])
        r.check("a handoff already opened once is refused - the step that hands it over runs after a failed receive too",
                opened.get("reopened") is False and opened["hidden"] is True and opened.get("describesAfter") == 1, str(opened.get("reopened")))

    norole = _run("norole")
    if norole and "error" not in norole:
        r.check("with every role unticked, Generate asks for one rather than sending nothing",
                norole["submits"] == [] and "at least one role" in norole["message"] and norole["hidden"] is False, str(norole["message"]))

    refused = _run("refused")
    if refused and "error" not in refused:
        r.check("a refusal keeps the popup open with the server's sentence and the first note, and records the code",
                refused["hidden"] is False and refused["message"].startswith("WanGP is not running.") and "nothing was stored" in refused["message"]
                and refused["state"]["last"]["code"] == "WANGP_NOT_RUNNING" and refused["state"]["busy"] is False, str(refused["message"]))

    cancelled = _run("cancel")
    if cancelled and "error" not in cancelled:
        r.check("Cancel sends one cancel for the frozen picture and no submit, and closes",
                cancelled["cancels"] == ["wangp:" + "a" * 32 + ":64x48:txt2img"] and cancelled["submits"] == [] and cancelled["hidden"] is True)
        r.check("and an edited prompt is shared with Clipboard on the way out", cancelled["drafts"] == ["edited then cancelled"], str(cancelled["drafts"]))

    replaced = _run("replace")
    if replaced and "error" not in replaced:
        r.check("a second picture replaces the first, which is cancelled on the server, and the prompt typed so far is kept",
                replaced["cancels"] == ["wangp:" + "a" * 32 + ":64x48:txt2img"] and replaced["state"]["token"] == "b" * 32
                and replaced["state"]["prompt"] == "kept across" and replaced["state"]["tab"] == "img2img", str(replaced["state"])[:200])

    facts = _run("facts")
    if facts and "error" not in facts:
        r.check("with the WanGP bridge on the page, the popup asks again with the live page's model and inputs",
                len(facts["describes"]) == 2 and "model" not in facts["describes"][0] and facts["describes"][1].get("inputs", {}).get("references", {}).get("supported") is False,
                str(facts["describes"])[:300])
        r.check("and redraws the roles from that answer, the new default ticked",
                facts["state"]["roles"]["available"] == ["first_frame", "last_frame"] and facts["submits"] and facts["submits"][0]["roles"] == ["last_frame"],
                str(facts["state"]["roles"]))
        r.check("the live flag says generating, so the dot does", facts["state"]["wangp"] == "busy")
        r.check("and the facts travel with Generate, under the queue API's own page identity",
                facts["submits"][0].get("model", {}).get("type") == "m" and "inputs" in facts["submits"][0] and facts["submits"][0]["page"] == "d" * 32)

    shared = _run("shared")
    if shared and "error" not in shared:
        opened_with = shared.get("opened") or {}
        r.check("on a page where the Clipboard tab is in use, the popup opens with that tab's own prompt box and switch",
                opened_with.get("prompt") == "from the tab" and opened_with.get("enhance") is False, str({k: opened_with.get(k) for k in ("prompt", "enhance")}))
        r.check("typing in the popup mirrors into the Clipboard prompt box", shared.get("written") == "mirrored out", str(shared.get("written")))
        r.check("ticking Enhance mirrors into the Clipboard switch and saves the shared setting",
                shared.get("toggleChecked") is True and shared.get("enhanceSaves") == [{"action": "toggle", "enabled": True}], str(shared.get("enhanceSaves")))
        r.check("and after Generate the Clipboard tab is handed the instruction and re-reads its queue",
                shared.get("queued") and json.loads(shared["queued"])["job_id"] == "0123456789abcdef" and shared.get("refreshed") is True)

    keys = _run("keys")
    if keys and "error" not in keys:
        r.check("Escape cancels", keys.get("closedByEscape") is True and keys["cancels"] == ["wangp:" + "a" * 32 + ":64x48:txt2img"])
        r.check("and Ctrl+Enter generates", len(keys["submits"]) == 1 and keys["hidden"] is True
                and keys["submits"][0]["handoff"] == "wangp:" + "b" * 32 + ":32x32:img2img")

    listed = _run("history")
    if listed and "error" not in listed:
        r.check("the history list draws pinned entries first", listed["rows"] == ["2222222222222222:pinned", "1111111111111111"], str(listed["rows"]))
        after = listed.get("afterLoad") or {}
        r.check("Load posts the entry and restores prompt, switch, inheritance and roles from the answer",
                listed["loads"] == ["2222222222222222"] and after.get("prompt") == "loaded prompt" and after.get("enhance") is False
                and after.get("inherit") is True and after.get("roles", {}).get("selected") == ["reference"], str(after)[:200])
        r.check("Pin and Delete post the verbs they say, for the entry they sit on",
                listed["pins"] == [["2222222222222222", False]] and listed["deletes"] == ["1111111111111111"], f"{listed['pins']} {listed['deletes']}")

    staged = _run("stage")
    if staged and "error" not in staged:
        r.check("the direct route fetches the picture, stages it over the queue API and opens on the token it got",
                staged.get("staged") is True and staged.get("stageCalls") == 1 and staged["state"]["token"] == "c" * 32 and staged["state"]["tab"] == "extras",
                str(staged["state"])[:160])
    for name, answer in (("norole", norole), ("refused", refused), ("cancel", cancelled), ("replace", replaced), ("facts", facts),
                         ("shared", shared), ("keys", keys), ("history", listed), ("stage", staged)):
        r.check(f"the {name} scenario ran", answer is not None and "error" not in answer, str(answer)[:300])
    return r


if __name__ == "__main__":
    raise SystemExit(0 if run().report() else 1)
