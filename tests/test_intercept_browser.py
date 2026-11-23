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
verbs they say; Generate asks the WanGP page to save its form first - two
and a half seconds at the very most - and tells the server what came of it,
and a popup closed during that wait sends nothing. Nothing in here names a
model.

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
    constructor(type, init) { this.type = type; this.bubbles = !!(init && init.bubbles); this.target = null; this.key = (init && init.key) || ""; this.ctrlKey = !!(init && init.ctrlKey); this.metaKey = false; this.defaultPrevented = false; this.detail = (init && init.detail) || null; }
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

/* What the popup announces to the rest of the page, in order. Anything that
   floats over Forge listens for these so it can get out of a dialog's way;
   the Forge Assistant's launcher is the one that prompted them. */
const overlays = [];
document.addEventListener("minipaint:overlay", function (event) {
    overlays.push((event.detail && event.detail.name) + ":" + ((event.detail && event.detail.open) ? "open" : "closed"));
});

/* ---------------------------------------------------------- the window -- */

const calls = [];
const loaded = [];
let SESSION = {};
const window = {
    innerWidth: 1400, innerHeight: 900,
    sessionStorage: { getItem: function (k) { return SESSION[k] === undefined ? null : SESSION[k]; }, setItem: function (k, v) { SESSION[k] = String(v); } },
    crypto: { getRandomValues: function (bytes) { for (let i = 0; i < bytes.length; i++) { bytes[i] = (i * 13 + 5) % 256; } return bytes; } },
    HTMLInputElement: { prototype: {} },
    minipaintAssets: { load: function (urls) { loaded.push(urls); return Promise.resolve(true); } },
    _listeners: {},
    addEventListener: function (kind, fn) { (this._listeners[kind] = this._listeners[kind] || []).push(fn); },
    fire: function (kind) { (this._listeners[kind] || []).forEach(function (fn) { fn({ type: kind }); }); }
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
        // "inheritunsaid": an answer that does not say what Inherit was last
        // left at; "inheriton": one that says it was left on.
        inherit: MODE === "inheritunsaid" ? { supported: true, draft: [{ role: "first_frame", label: "First Frame", names: ["forest.png"] }] }
            : { default: MODE === "inheriton", supported: true, draft: [{ role: "first_frame", label: "First Frame", names: ["forest.png"] }] },
        wangp: { state: "idle", running: true, generating: false, text: "WanGP is idle" },
        generate: MODE === "autooff" ? { label: "Generate", enabled: false, reason: "WanGP is not running" }
                                     : { label: "Generate", enabled: true, reason: "" },
        auto: { roles: ["first_frame", "reference"], dropped: [], label: "First Frame and Reference" },
        executor: "server",
        history: MODE === "history" || MODE === "editorhistory" ? HISTORY : [],
        readiness: MODE === "editorblocked" ? { ready: false, code: "WANGP_NOT_RUNNING", message: "WanGP is not running." } : { ready: true }
    };
}

//: As the server lists it: pinned first, then the rest, newest first.
const HISTORY = [
    { id: "2222222222222222", when: "yesterday", prompt: "pinned one", enhance: true, inherit: false,
      roles: [{ id: "first_frame", label: "First Frame", valid: true }], summary: "s", pinned: true, model: "", outcome: "", image: {} },
    { id: "1111111111111111", when: "today", prompt: "an earlier prompt", enhance: false, inherit: true,
      roles: [{ id: "reference", label: "Reference", valid: true }], summary: "s", pinned: false, model: "", outcome: "Queued", image: {} }
];

//: How many reads of the gallery picture never answer, per scenario.
let galleryHangs = MODE === "stagehang" ? 1 : (MODE === "stagedead" || MODE === "stageleaving" ? 99 : 0);
//: The journal, for the scenarios that read it. A file name and a prompt are
//: planted where a careless line would pick them up.
const journal = [];
const SECRET_NAME = "Jane_Doe_private_holiday.png";
if (MODE.indexOf("stage") === 0) {
    window.minipaintWanGP = { note: function (line) { journal.push(String(line)); } };
    window.minipaintInterceptTiming = { timeout: 50, retryDelay: 10 };
}
if (MODE === "settings") { window.minipaintWanGP = { note: function (line) { journal.push(String(line)); } }; }

//: What the server says of a job that will not run at the settings on
//: WanGP's screen (outbox.settings_warning).
const NOT_IN_VIEW = "Not the settings on WanGP's screen: this job runs at WanGP's last saved settings (its form was not saved at send)";

global.fetch = function (url, options) {
    const text = String(url);
    const body = options && typeof options.body === "string" ? JSON.parse(options.body) : null;
    calls.push({ url: text, body: body, method: (options && options.method) || "GET" });
    let payload = { ok: true };
    if (text.indexOf("/minipaint-clipboard/intercept") === 0) {
        const action = body.action;
        if (action === "describe") { payload = describeAnswer(body); }
        else if (action === "submit") {
            // Auto puts the picture in the ticked roles, as Generate does and
            // as the real route has since 2026-11-23.
            payload = MODE === "refused"
                ? { ok: false, code: "WANGP_NOT_RUNNING", message: "WanGP is not running.", notes: ["nothing was stored"] }
                : MODE === "autorefused"
                ? { ok: false, code: "INTERCEPT_NO_IMAGE_ROLE", message: "The current WanGP model takes no image, so the picture has nowhere to go. Nothing was queued.",
                    notes: ["nothing was stored"] }
                : MODE === "settings" && body.prompt === "warn me"
                ? { ok: true, job_id: "0123456789abcdef", instruction: { nonce: "w", job_id: "0123456789abcdef", executor: "server", state: "admitted" },
                    status: "Queued on the server.", notes: [NOT_IN_VIEW, "it goes next"],
                    settings: { source: "last_saved_no_answer", sentence: "WanGP's last saved settings (its form was not saved at send)",
                                in_view: false, warning: NOT_IN_VIEW },
                    roles: body.roles, dropped_roles: [], auto: false, history: [] }
                : { ok: true, job_id: "0123456789abcdef", instruction: { nonce: "n", job_id: "0123456789abcdef", executor: "server", state: "admitted" },
                    status: body.auto ? "Auto: the picture as First Frame and Reference. Queued on the server." : "Queued on the server.",
                    notes: ["it goes next"], roles: body.roles, dropped_roles: [], auto: !!body.auto, history: [],
                    settings: { source: "saved_at_send", sentence: "saved from the WanGP page at send", in_view: true, warning: "" } };
        }
        else if (action === "history_load") {
            payload = { ok: true, id: body.id, prompt: "loaded prompt", enhance: MODE === "editorhistory", inherit: true, roles: ["reference"], dropped_roles: [], defaulted: false,
                        prewritten: MODE === "editorhistory", capabilities: { roles: ALL, default_roles: ["reference"] } };
        }
        else if (action === "history_pin" || action === "history_delete") { payload = { ok: true, history: HISTORY }; }
        else { payload = { ok: true, released: true }; }
    } else if (text.indexOf("/minipaint-interop/stage") === 0) {
        payload = { ok: true, image: { kind: "staged", id: "c".repeat(32) }, width: 80, height: 60 };
    } else if (text.indexOf("/gallery/") === 0) {
        const picture = { ok: true, status: 200, blob: function () { return Promise.resolve({ type: "image/png", size: 3 }); } };
        if (galleryHangs > 0) {
            // A request on a stalled connection: nothing comes back until it is aborted.
            galleryHangs -= 1;
            return new Promise(function (resolve, reject) {
                const signal = options && options.signal;
                if (signal) { signal.addEventListener("abort", function () { const e = new Error("The operation was aborted."); e.name = "AbortError"; reject(e); }); }
            });
        }
        if (MODE === "stagepresses") { return new Promise(function (resolve) { setTimeout(function () { resolve(picture); }, 60); }); }
        return Promise.resolve(picture);
    }
    return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(payload); } });
};

/* ---------------------------------------------------------- the bridge -- */

//: The WanGP page's save before a send, as the popup sees it: asked for by
//: name, answered with a word - or, for "savemute", never answered at all.
const saves = [];
if (MODE === "save" || MODE === "savemute" || MODE === "saveclosed") {
    window.minipaintWanGP = {
        note: function () { },
        saveForSend: function (reason) {
            saves.push({ reason: String(reason), at: calls.length, submitsBefore: posted("submit").length });
            if (MODE === "savemute") { return new Promise(function () { }); }
            return new Promise(function (resolve) { setTimeout(function () { resolve({ ok: true, flush: "committed" }); }, MODE === "saveclosed" ? 80 : 10); });
        }
    };
}

if (MODE === "facts") {
    window.minipaintWanGP = {
        state: function () { return { present: true, ready: true, model: { type: "m", label: "M", family: "", architecture: "" } }; },
        capabilities: function () { return Promise.resolve({ ok: true, model: { type: "m", label: "M" }, generation_running: true,
            inputs: { start: { supported: true }, end: { supported: true }, references: { supported: false } } }); },
        note: function () { }
    };
    window.minipaintInterop = { wangp: { capabilities: function () { return window.minipaintWanGP.capabilities(); }, pageId: function () { return "d".repeat(32); } } };
}
if (MODE === "editor") {
    // The Clipboard tab's switch on this page, on: a written prompt's press
    // must leave it on.
    const toggleHost = new Node("div"); toggleHost.setAttribute("id", "minipaint_clipboard_enhance_toggle");
    const toggle = new Node("input"); toggle.type = "checkbox"; toggle.checked = true; toggleHost.appendChild(toggle); body.appendChild(toggleHost);
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

/* ------------------------------------------------------ the editor ----- */

//: The shared pop-over (minipaint_sheet.js), stood in for: what the popup
//: asks it for is recorded, and a check answers for the person - Done with
//: some text, Cancel, or a stacked system prompt. The sheet itself is
//: checked in a real browser (browser_intercept.py).
const sheets = { prompts: [], systems: [], closed: [] };
function standIn(kind, options) {
    const sheet = { name: options.name, kind: kind, options: options, spec: {}, closed: false };
    sheet.close = function (why) {
        if (sheet.closed) { return false; }
        sheet.closed = true;
        sheets.closed.push(options.name + ":" + why);
        if (typeof sheet.spec.closed === "function") { sheet.spec.closed(why); }
        return true;
    };
    (kind === "prompt" ? sheets.prompts : sheets.systems).push(sheet);
    return sheet;
}
if (MODE !== "nosheet") {
    window.minipaintSheet = {
        prompt: function (options) { return standIn("prompt", options); },
        systemPrompt: function (options) { return standIn("system", options); }
    };
}

new Function("window", "document", "fetch", "Event", "CustomEvent",
    fs.readFileSync(process.argv[2], "utf8"))(window, document, fetch, FakeEvent, FakeEvent);

const popup = window.minipaintIntercept;

function tick(ms) { return new Promise(function (r) { setTimeout(r, ms || 15); }); }
function posted(action) { return calls.filter(function (c) { return c.url.indexOf("/minipaint-clipboard/intercept") === 0 && c.body && c.body.action === action; }); }
function root() { return body.querySelector(".minipaint-intercept"); }
function roleBoxes() { return root().querySelectorAll(".minipaint-intercept-roles input"); }
/** The person taps the prompt and types in the editor, then Done. */
function typePrompt(text, prewritten) {
    root().querySelector(".minipaint-intercept-prompt").click();
    const sheet = sheets.prompts[sheets.prompts.length - 1];
    if (!sheet || sheet.closed) { throw new Error("the prompt editor did not open"); }
    sheet.options.done(text, { prewritten: prewritten || null, changed: true });
    sheet.close("done");
    return sheet;
}
function tickRole(id, on) { const box = roleBoxes().filter(function (b) { return b.dataset.role === id; })[0]; box.checked = on !== false; box.dispatchEvent(new FakeEvent("change")); }
function report(extra) {
    console.log(JSON.stringify(Object.assign({
        state: popup.state(),
        describes: posted("describe").map(function (c) { return c.body; }),
        submits: posted("submit").map(function (c) { return c.body; }),
        cancels: posted("cancel").map(function (c) { return c.body.handoff; }),
        drafts: posted("draft").map(function (c) { return c.body.prompt; }),
        loaded: loaded,
        overlays: overlays.slice(),
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
        typePrompt("typed in the popup");
        tickRole("first_frame", true);
        await popup.generate();
        await tick(20);
        // The chained browser step runs after a failed receive too, with the
        // previous press's handoff: the popup must not open on it again.
        const reopened = popup.open(H1, {});
        await tick(20);
        report({ before: before, reopened: reopened, describesAfter: posted("describe").length });
    }
    if (MODE === "inheritunsaid" || MODE === "inheriton") {
        popup.open(H1, {});
        await tick(40);
        const before = JSON.parse(JSON.stringify(popup.state()));
        await popup.generate();
        await tick(20);
        report({ before: before });
    }
    if (MODE === "save" || MODE === "savemute") {
        popup.open(H1, {});
        await tick(40);
        const started = Date.now();
        await popup.generate();
        const took = Date.now() - started;
        await tick(20);
        report({ saves: saves, took: took });
    }
    if (MODE === "saveclosed") {
        popup.open(H1, {});
        await tick(40);
        const pressed = popup.generate();
        await tick(20);
        popup.cancel();
        await pressed;
        await tick(120);
        report({ saves: saves });
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
    if (MODE === "auto") {
        popup.open(H1, {});
        await tick(40);
        const before = JSON.parse(JSON.stringify(popup.state()));
        const order = root().querySelectorAll(".minipaint-intercept-actions button").map(function (b) { return b.textContent; });
        // Nothing ticked: Auto asks for a role, as Generate does, and sends nothing.
        tickRole("reference", false);
        const untickedTitle = popup.state().autoTitle;
        root().querySelector(".minipaint-intercept-auto").click();
        await tick(40);
        const untickedMessage = (function () { const m = root().querySelector(".minipaint-intercept-message"); return m && !m.hidden ? m.textContent : ""; })();
        const submitsUnticked = posted("submit").length;
        // Two ticked: the tooltip says them, and Auto sends exactly those.
        tickRole("first_frame", true);
        tickRole("reference", true);
        const tickedTitle = popup.state().autoTitle;
        root().querySelector(".minipaint-intercept-auto").click();
        await tick(40);
        report({ before: before, order: order, untickedTitle: untickedTitle, untickedMessage: untickedMessage,
                 submitsUnticked: submitsUnticked, tickedTitle: tickedTitle });
    }
    if (MODE === "settings") {
        // One press whose job runs at the settings on screen, then one whose
        // job will not: only the second says so, marked.
        const toastNow = function () {
            const t = body.querySelector(".minipaint-intercept-toast");
            return t && !t.hidden ? { text: t.textContent, failure: t.classList.contains("minipaint-intercept-toast-failure") } : null;
        };
        popup.open(H1, {});
        await tick(40);
        typePrompt("in view");
        await popup.generate();
        await tick(20);
        const inView = toastNow();
        popup.open(H2, {});
        await tick(40);
        typePrompt("warn me");
        await popup.generate();
        await tick(20);
        report({ inView: inView, warned: toastNow(), journal: journal.slice() });
    }
    if (MODE === "autorefused") {
        popup.open(H1, {});
        await tick(40);
        root().querySelector(".minipaint-intercept-auto").click();
        await tick(40);
        report({ autoLabel: root().querySelector(".minipaint-intercept-auto").textContent });
    }
    if (MODE === "autooff") {
        popup.open(H1, {});
        await tick(40);
        report();
    }
    if (MODE === "cancel") {
        popup.open(H1, {});
        await tick(40);
        typePrompt("edited then cancelled");
        popup.cancel();
        await tick(30);
        report();
    }
    if (MODE === "replace") {
        popup.open(H1, {});
        await tick(40);
        typePrompt("kept across");
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
        typePrompt("mirrored out");
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
    if (MODE === "editor") {
        popup.open(H1, {});
        await tick(40);
        const display = root().querySelector(".minipaint-intercept-prompt");
        const shown = { tag: display.tagName, role: display.getAttribute("role"), text: display.textContent };
        // A tap opens the editor on the prompt as it stands; nothing is typed in the popup.
        display.click();
        const sheet = sheets.prompts[0];
        const opened = sheet ? { name: sheet.options.name, text: sheet.options.text, placeholder: sheet.options.placeholder,
                                 enhance: Object.keys(sheet.options.enhance || {}).sort(), system: typeof sheet.options.systemPrompt } : null;
        // Enter on the box opens it too; Ctrl+Enter is still Generate's.
        sheet.close("done");
        const enter = new FakeEvent("keydown", { key: "Enter" });
        enter.target = display;
        display.dispatchEvent(enter);
        const byEnter = sheets.prompts.length;
        // The editor's Enhance asks the popup's own route, as a press would:
        // the ticks, Inherit, the frozen picture.
        const again = sheets.prompts[sheets.prompts.length - 1];
        tickRole("first_frame", true);
        await again.options.enhance.plan();
        await again.options.enhance.start("a fox in snow");
        await again.options.enhance.status("feedfacefeedface");
        await again.options.enhance.cancel("feedfacefeedface");
        // System prompt: stacked, on the set the plan named.
        const system = again.options.systemPrompt({ ok: true, prompt_set: "set-a", mode: "image" });
        const systemAsked = system ? { name: system.options.name, variant: system.options.variant, mode: system.options.mode } : null;
        // The switch turned in the stacked editor reaches the popup's own box.
        system.options.switched(false);
        const switchAfter = popup.state().enhance;
        system.options.switched(true);
        system.close("close");
        // The enhancer's prompt, taken: Done hands it back as written.
        again.options.done("A red fox in deep snow, low sun.", { prewritten: { typed: "a fox in snow", written: "A red fox in deep snow, low sun." }, changed: true });
        again.close("done");
        await tick(10);
        const afterDone = JSON.parse(JSON.stringify(popup.state()));
        const note = root().querySelector(".minipaint-intercept-enhance").parentNode.querySelector(".minipaint-intercept-note").textContent;
        const marked = root().querySelector(".minipaint-intercept-prompt").classList.contains("minipaint-intercept-prompt-written");
        // The switch is on: a written prompt still goes as it is.
        await popup.generate();
        await tick(20);
        report({ shown: shown, opened: opened, byEnter: byEnter, systemAsked: systemAsked, switchAfter: switchAfter, afterDone: afterDone,
                 note: note, marked: marked, sheets: { closed: sheets.closed },
                 toggleAfter: body.querySelector("#minipaint_clipboard_enhance_toggle input").checked,
                 enhanceCalls: calls.filter(function (c) { return c.body && /^enhance_/.test(c.body.action || ""); }).map(function (c) { return c.body; }),
                 toggles: calls.filter(function (c) { return c.url.indexOf("enhance-settings") !== -1; }).map(function (c) { return c.body; }) });
    }
    if (MODE === "editorcancel") {
        popup.open(H1, {});
        await tick(40);
        root().querySelector(".minipaint-intercept-prompt").click();
        const sheet = sheets.prompts[0];
        sheet.options.cancel && sheet.options.cancel();
        sheet.close("cancel");
        await tick(10);
        const afterCancel = popup.state().prompt;
        // A popup that closes takes the editor it opened with it.
        root().querySelector(".minipaint-intercept-prompt").click();
        const open = sheets.prompts[1];
        popup.cancel();
        await tick(20);
        report({ afterCancel: afterCancel, openClosedWithPopup: open.closed, closed: sheets.closed });
    }
    if (MODE === "editorblocked") {
        // Blocked by the answer itself (readiness not ready): a press on the
        // prompt, and Enter on it, open nothing.
        popup.open(H1, {});
        await tick(40);
        const blocked = popup.state().blocked;
        const display = root().querySelector(".minipaint-intercept-prompt");
        display.click();
        const enter = new FakeEvent("keydown", { key: "Enter" });
        enter.target = display;
        display.dispatchEvent(enter);
        await tick(10);
        report({ blocked: blocked, opened: sheets.prompts.length });
    }
    if (MODE === "nosheet") {
        popup.open(H1, { bundles: { sheet: "/s.js" } });
        await tick(40);
        root().querySelector(".minipaint-intercept-prompt").click();
        await tick(40);
        report({ loadedSheet: loaded.filter(function (urls) { return urls.indexOf("/s.js") !== -1; }).length });
    }
    if (MODE === "editorhistory") {
        // A prompt the editor wrote, loaded back from the history, goes as it is again.
        popup.open(H1, {});
        await tick(40);
        root().querySelector(".minipaint-intercept-history-toggle").click();
        root().querySelectorAll("[data-history-action=load]")[0].click();
        await tick(30);
        const loadedState = JSON.parse(JSON.stringify(popup.state()));
        await popup.generate();
        await tick(20);
        report({ loadedState: loadedState });
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
    const stageCalls = function () { return calls.filter(function (c) { return c.url.indexOf("/minipaint-interop/stage") === 0; }).length; };
    const galleryCalls = function () { return calls.filter(function (c) { return c.url.indexOf("/gallery/") === 0; }).length; };
    const PICTURE = "/gallery/" + SECRET_NAME;
    if (MODE === "stage") {
        const ok = await popup.stageAndOpen("/gallery/one.png", "extras", {});
        await tick(40);
        report({ staged: ok, stageCalls: stageCalls() });
    }
    if (MODE === "stagehang") {
        // The first read never answers: it is cancelled on its deadline and the send tried again.
        const ok = await popup.stageAndOpen(PICTURE, "txt2img", {});
        await tick(40);
        report({ staged: ok, stageCalls: stageCalls(), galleryCalls: galleryCalls(), journal: journal });
    }
    if (MODE === "stagedead") {
        const ok = await popup.stageAndOpen(PICTURE, "txt2img", {});
        const toastNow = (function () { const t = body.querySelector(".minipaint-intercept-toast"); return t && !t.hidden ? t.textContent : ""; })();
        report({ staged: ok, stageCalls: stageCalls(), galleryCalls: galleryCalls(), journal: journal, failureToast: toastNow });
    }
    if (MODE === "stagepresses") {
        // Eleven presses in a row, as on 2026-09-23, while the first is still on its way.
        const runs = [];
        for (let i = 0; i < 11; i++) { runs.push(popup.stageAndOpen(PICTURE, "txt2img", {})); await tick(2); }
        const toastWhileBusy = (function () { const t = body.querySelector(".minipaint-intercept-toast"); return t && !t.hidden ? t.textContent : ""; })();
        const results = await Promise.all(runs);
        await tick(40);
        report({ results: results, stageCalls: stageCalls(), galleryCalls: galleryCalls(), journal: journal, toastWhileBusy: toastWhileBusy });
    }
    if (MODE === "stageleaving") {
        // A reload cancels the requests in flight; that is not a failure to shout about.
        const run = popup.stageAndOpen(PICTURE, "txt2img", {});
        await tick(5);
        window.fire("beforeunload");
        const ok = await run;
        const toastNow = (function () { const t = body.querySelector(".minipaint-intercept-toast"); return t && !t.hidden ? t.textContent : ""; })();
        report({ staged: ok, galleryCalls: galleryCalls(), journal: journal, toastAfter: toastNow });
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
        r.check("a page with no WanGP to ask still sends, and says nothing was saved",
                submit.get("settings_flush") == "unavailable", str(submit.get("settings_flush")))
        r.check("a generated picture closes the popup and says the overlay has gone",
                opened["overlays"] == ["intercept:open", "intercept:closed"], str(opened["overlays"]))
        r.check("a handoff already opened once is refused - the step that hands it over runs after a failed receive too",
                opened.get("reopened") is False and opened["hidden"] is True and opened.get("describesAfter") == 1, str(opened.get("reopened")))

    # Inherit Clipboard inputs is off until it is chosen (2026-11-23): an
    # answer that does not say it was left on opens the popup with it off,
    # so a role nobody ticked is not filled from the Clipboard's cards.
    unsaid = _run("inheritunsaid")
    r.check("an answer that does not say Inherit was left on opens the popup with it off, and Generate sends it off",
            unsaid is not None and "error" not in unsaid and (unsaid.get("before") or {}).get("inherit") is False
            and bool(unsaid.get("submits")) and unsaid["submits"][0].get("inherit") is False, str(unsaid)[:300])
    left_on = _run("inheriton")
    r.check("and one that says it was left on opens it on, and Generate sends it on",
            left_on is not None and "error" not in left_on and (left_on.get("before") or {}).get("inherit") is True
            and bool(left_on.get("submits")) and left_on["submits"][0].get("inherit") is True, str(left_on)[:300])

    # Auto: since 2026-11-23 the same press as Generate - the picture in the
    # roles ticked here, one or several - and never Auto Options' roles,
    # which are the Clipboard tab's Auto's alone.
    auto = _run("auto")
    r.check("the Auto scenario ran", auto is not None and "error" not in auto, str(auto)[:300])
    if auto and "error" not in auto:
        before = auto.get("before") or {}
        submit = auto["submits"][0] if auto["submits"] else {}
        r.check("Auto sits between Cancel and Generate, a button while Generate is one",
                auto.get("order") == ["Cancel", "Auto", "Generate"] and before.get("autoEnabled") is True, str(auto.get("order")))
        r.check("and says, as its tooltip, the roles ticked in the popup, following the ticks",
                before.get("autoTitle") == "Send the picture as Reference, as ticked here"
                and auto.get("untickedTitle") == "Tick at least one role for the picture"
                and auto.get("tickedTitle") == "Send the picture as First Frame and Reference, as ticked here",
                str((before.get("autoTitle"), auto.get("untickedTitle"), auto.get("tickedTitle"))))
        r.check("with nothing ticked Auto asks for a role, as Generate does, and sends nothing",
                auto.get("submitsUnticked") == 0 and auto.get("untickedMessage") == "Tick at least one role for the picture.",
                str((auto.get("submitsUnticked"), auto.get("untickedMessage"))))
        r.check("its press is Generate's submit, marked auto, with exactly the roles ticked",
                len(auto["submits"]) == 1 and submit.get("auto") is True and submit.get("roles") == ["first_frame", "reference"]
                and submit.get("handoff") == "wangp:" + "a" * 32 + ":64x48:txt2img", str(submit)[:200])
        r.check("and closes the popup with the server's sentence, like Generate",
                auto["hidden"] is True and auto["toast"].startswith("Auto: the picture as First Frame and Reference."), str(auto["toast"]))
    # The settings in view: a press whose job will not run at them says so.
    told = _run("settings")
    r.check("the settings scenario ran", told is not None and "error" not in told, str(told)[:300])
    if told and "error" not in told:
        r.check("a press whose job runs at the settings on WanGP's screen closes with the server's sentence, unmarked",
                told.get("inView") == {"text": "Queued on the server. it goes next.", "failure": False}, str(told.get("inView")))
        r.check("one whose job will not says so in the notice, in place of the first note, marked",
                told.get("warned") == {"text": "Queued on the server. Not the settings on WanGP's screen: this job runs at "
                                               "WanGP's last saved settings (its form was not saved at send).", "failure": True},
                str(told.get("warned")))
        r.check("and the journal says it with the source's key, never the prompt",
                [line for line in told.get("journal") or [] if "not the settings" in line]
                == ["intercept: generate: not the settings on WanGP's screen (last_saved_no_answer)"]
                and not [line for line in told.get("journal") or [] if "warn me" in line], str(told.get("journal")))
    refused = _run("autorefused")
    if refused and "error" not in refused:
        r.check("an Auto the server refuses leaves the popup open with its sentence and its note",
                refused["hidden"] is False and refused["state"]["open"] is True and len(refused["submits"]) == 1
                and refused["message"].startswith("The current WanGP model takes no image, so the picture has nowhere to go.")
                and "nothing was stored" in refused["message"] and refused.get("autoLabel") == "Auto", str(refused.get("message")))
    else:
        r.check("the refused Auto scenario ran", False, str(refused)[:300])
    off = _run("autooff")
    if off and "error" not in off:
        r.check("Auto is off exactly when Generate is, and says the same reason",
                off["state"]["autoEnabled"] is False and off["state"]["generateEnabled"] is False
                and off["state"]["autoTitle"] == "WanGP is not running", str(off["state"])[:200])
    else:
        r.check("the Auto-off scenario ran", False, str(off)[:300])

    # WanGP's form, saved just before the job is handed over - because the
    # server builds the job from what WanGP saved, and a LoRA weight changed
    # in the WanGP tab a moment ago is otherwise not in it.
    saved = _run("save")
    r.check("the harness drove a Generate on a page with a WanGP to ask", saved is not None and "error" not in saved, str(saved)[:300])
    if saved and "error" not in saved:
        asked = saved.get("saves") or []
        r.check("Generate asks the WanGP page to save its form, once, naming who asked",
                len(asked) == 1 and asked[0]["reason"] == "the gallery's Generate", str(asked))
        r.check("before the job is handed over, not after", asked and asked[0]["submitsBefore"] == 0, str(asked))
        sent = saved["submits"][0] if saved.get("submits") else {}
        r.check("and the job says what came of it", sent.get("settings_flush") == "committed", str(sent)[:200])
        r.check("and the popup closes as it always did", saved.get("hidden") is True, str(saved.get("hidden")))
    # A harness that never reports is the failure here, not a reason to skip:
    # a Generate waiting for ever on a WanGP that never answers IS the fault.
    mute = _run("savemute") or {"error": "no answer"}
    sent = mute["submits"][0] if mute.get("submits") else {}
    r.check("a WanGP page that never answers holds Generate no longer than the popup's own limit, and the job still goes",
            "error" not in mute and sent.get("settings_flush") == "unavailable" and 2400 <= (mute.get("took") or 0) < 4000
            and mute.get("hidden") is True, str({"error": mute.get("error"), "took": mute.get("took"), "sent": sent.get("settings_flush")}))
    closed = _run("saveclosed") or {"error": "no answer"}
    r.check("a popup cancelled while WanGP saves sends nothing - the picture it froze has been let go",
            "error" not in closed and closed.get("submits") == [] and len(closed.get("cancels") or []) == 1,
            str({"error": closed.get("error"), "submits": closed.get("submits"), "cancels": closed.get("cancels")}))

    norole = _run("norole")
    if norole and "error" not in norole:
        r.check("with every role unticked, Generate asks for one rather than sending nothing",
                norole["submits"] == [] and "at least one role" in norole["message"] and norole["hidden"] is False, str(norole["message"]))

    refused = _run("refused")
    if refused and "error" not in refused:
        r.check("a refusal keeps the popup open with the server's sentence and the first note, and records the code",
                refused["hidden"] is False and refused["message"].startswith("WanGP is not running.") and "nothing was stored" in refused["message"]
                and refused["state"]["last"]["code"] == "WANGP_NOT_RUNNING" and refused["state"]["busy"] is False, str(refused["message"]))

    # The prompt is not typed in the popup (2026-11-23): the box shows it, and
    # a tap opens the shared editor, whose Enhance and System prompt are the
    # popup's own press and its instructions.
    edited = _run("editor")
    r.check("the editor scenario ran", edited is not None and "error" not in edited, str(edited)[:300])
    if edited and "error" not in edited:
        shown = edited.get("shown") or {}
        r.check("the popup's prompt is a box to read and press, not a text field",
                shown.get("tag") == "DIV" and shown.get("role") == "button" and shown.get("text") == "the server's prompt", str(shown))
        opened = edited.get("opened") or {}
        r.check("a tap opens the shared editor on the prompt as it stands, with Enhance and System prompt",
                opened.get("name") == "wangp-prompt" and opened.get("text") == "the server's prompt"
                and opened.get("enhance") == ["cancel", "plan", "start", "status"] and opened.get("system") == "function", str(opened))
        r.check("and Enter on the box opens it too", edited.get("byEnter") == 2, str(edited.get("byEnter")))
        asked = edited.get("enhanceCalls") or []
        r.check("Enhance in the editor asks the popup's own route - plan, start, read, stop - in that order",
                [call.get("action") for call in asked] == ["enhance_plan", "enhance_start", "enhance_status", "enhance_cancel"], str(asked)[:300])
        r.check("as a press would: the frozen picture, the roles ticked and Inherit as they stand, the words typed",
                len(asked) == 4 and all(call.get("handoff") == "wangp:" + "a" * 32 + ":64x48:txt2img" and call.get("roles") == ["first_frame", "reference"]
                                        and call.get("inherit") is False for call in asked[:2])
                and asked[1].get("prompt") == "a fox in snow" and asked[2].get("llm_id") == "feedfacefeedface"
                and asked[3].get("llm_id") == "feedfacefeedface", str(asked)[:300])
        r.check("System prompt opens the instructions the plan named, stacked",
                edited.get("systemAsked") == {"name": "system-prompt", "variant": "set-a", "mode": "image"}, str(edited.get("systemAsked")))
        r.check("and the switch turned there is the popup's switch at once", edited.get("switchAfter") is False, str(edited.get("switchAfter")))
        after = edited.get("afterDone") or {}
        r.check("Done hands back the enhancer's prompt, taken, with what was typed before it",
                after.get("prompt") == "A red fox in deep snow, low sun."
                and after.get("prewritten") == {"typed": "a fox in snow", "written": "A red fox in deep snow, low sun."}, str(after)[:300])
        r.check("which the popup marks, and says goes as it is",
                edited.get("marked") is True and edited.get("note") == "written in the editor, so it goes as it is", str(edited.get("note")))
        sent = edited["submits"][0] if edited.get("submits") else {}
        r.check("Generate sends it as written: not enhanced again, marked prewritten, with the words typed for the History",
                sent.get("prompt") == "A red fox in deep snow, low sun." and sent.get("enhance") is False and sent.get("prewritten") is True
                and sent.get("typed") == "a fox in snow", str(sent)[:300])
        r.check("and the switch, on, is left on: nothing posted to the setting, and the Clipboard tab's own switch still on",
                edited.get("toggles") == [] and edited.get("toggleAfter") is True, str((edited.get("toggles"), edited.get("toggleAfter"))))
    cancelled_edit = _run("editorcancel")
    r.check("Cancel in the editor leaves the popup's prompt as it was",
            cancelled_edit is not None and cancelled_edit.get("afterCancel") == "the server's prompt", str(cancelled_edit)[:300])
    r.check("and a popup that closes closes the editor it opened",
            cancelled_edit is not None and cancelled_edit.get("openClosedWithPopup") is True
            and "wangp-prompt:popup closed" in (cancelled_edit.get("closed") or []), str(cancelled_edit)[:300])
    blocked_edit = _run("editorblocked")
    r.check("a popup blocked by the Clipboard's readiness opens no editor, pressed or Enter",
            blocked_edit is not None and blocked_edit.get("blocked") is True and blocked_edit.get("opened") == 0, str(blocked_edit)[:300])
    missing = _run("nosheet")
    r.check("a page without the editor's bundle fetches it on the press, and says so when it cannot",
            missing is not None and missing.get("loadedSheet") == 1 and "could not be loaded" in (missing.get("message") or ""), str(missing)[:300])
    from_history = _run("editorhistory")
    loaded_state = (from_history or {}).get("loadedState") or {}
    sent = (from_history or {}).get("submits") or [{}]
    r.check("a written prompt loaded back from the history goes as it is again, the switch untouched",
            loaded_state.get("prewritten") == {"typed": "", "written": "loaded prompt"} and sent[0].get("prewritten") is True
            and sent[0].get("enhance") is False, str(from_history)[:300])

    cancelled = _run("cancel")
    if cancelled and "error" not in cancelled:
        r.check("Cancel sends one cancel for the frozen picture and no submit, and closes",
                cancelled["cancels"] == ["wangp:" + "a" * 32 + ":64x48:txt2img"] and cancelled["submits"] == [] and cancelled["hidden"] is True)
        r.check("and an edited prompt is shared with Clipboard on the way out", cancelled["drafts"] == ["edited then cancelled"], str(cancelled["drafts"]))
        # Announced so that anything floating over Forge can get out of a
        # dialog's way. Closing has to be announced too, or whatever moved
        # aside never comes back.
        r.check("opening and closing are announced to the page, in that order",
                cancelled["overlays"] == ["intercept:open", "intercept:closed"], str(cancelled["overlays"]))

    replaced = _run("replace")
    if replaced and "error" not in replaced:
        r.check("a second picture replaces the first, which is cancelled on the server, and the prompt typed so far is kept",
                replaced["cancels"] == ["wangp:" + "a" * 32 + ":64x48:txt2img"] and replaced["state"]["token"] == "b" * 32
                and replaced["state"]["prompt"] == "kept across" and replaced["state"]["tab"] == "img2img", str(replaced["state"])[:200])
        # One overlay, still up. A second "open" would make a listener that
        # counts them think two dialogs were on screen, and the one that
        # matters here puts a panel away on open and back on close.
        r.check("a replacement picture is the same overlay, announced once",
                replaced["overlays"] == ["intercept:open"], str(replaced["overlays"]))

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
    secret = "Jane_Doe_private_holiday"

    def private(answer):
        """Nothing written down or shown names the picture's file."""
        text = json.dumps({k: answer.get(k) for k in ("journal", "toast", "failureToast", "toastWhileBusy", "toastAfter")})
        return secret not in text

    hung = _run("stagehang")
    if hung and "error" not in hung:
        r.check("a read that never answers is cancelled on its deadline and the send tried once more, which then opens the popup",
                hung.get("staged") is True and hung.get("galleryCalls") == 2 and hung.get("stageCalls") == 1 and hung["state"]["open"] is True,
                str({k: hung.get(k) for k in ("staged", "galleryCalls", "stageCalls")}))
        r.check("the journal says what timed out, which attempt, and how the page reaches Forge",
                any("reading the picture: no answer within" in line and "attempt 1 of 2" in line and "page over" in line
                    and "trying once more" in line for line in hung["journal"]), str(hung["journal"])[:400])
        r.check("a stalled send never writes the picture's file name down", private(hung), str(hung["journal"])[:400])

    dead = _run("stagedead")
    if dead and "error" not in dead:
        r.check("a connection that stays stalled gives up after two bounded tries, with no request left behind",
                dead.get("staged") is False and dead.get("galleryCalls") == 2 and dead.get("stageCalls") == 0, str(dead)[:300])
        r.check("and says so on screen, with what to do", "could not be sent to WanGP" in dead.get("failureToast", "")
                and "Press the button again" in dead.get("failureToast", ""), dead.get("failureToast", ""))
        r.check("the failure is private too", private(dead), str(dead["journal"])[:400])

    presses = _run("stagepresses")
    if presses and "error" not in presses:
        r.check("eleven presses while one send is on its way make one send, not eleven",
                presses.get("galleryCalls") == 1 and presses.get("stageCalls") == 1 and all(v is True for v in presses.get("results", [])),
                str({k: presses.get(k) for k in ("galleryCalls", "stageCalls", "results")}))
        r.check("a press while one is on its way is told so", "Still sending" in presses.get("toastWhileBusy", ""), presses.get("toastWhileBusy", ""))
        r.check("the presses are private", private(presses))

    leaving = _run("stageleaving")
    if leaving and "error" not in leaving:
        r.check("a send cancelled by a reload is not reported as a failure and not retried",
                leaving.get("staged") is False and leaving.get("toastAfter", "") == "" and leaving.get("galleryCalls") == 1
                and any("page is being reloaded or closed" in line for line in leaving["journal"]), str(leaving)[:400])

    for name, answer in (("norole", norole), ("refused", refused), ("cancel", cancelled), ("replace", replaced), ("facts", facts),
                         ("shared", shared), ("keys", keys), ("history", listed), ("stage", staged), ("stagehang", hung),
                         ("stagedead", dead), ("stagepresses", presses), ("stageleaving", leaving)):
        r.check(f"the {name} scenario ran", answer is not None and "error" not in answer, str(answer)[:300])
    return r


if __name__ == "__main__":
    raise SystemExit(0 if run().report() else 1)
