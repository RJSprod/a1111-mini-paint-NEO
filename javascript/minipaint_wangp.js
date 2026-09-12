/**
 * The WanGP side of the browser: one iframe, one channel, one bounded question.
 *
 * WanGP runs in its own process behind /wan2gp/ on this same origin, and what
 * it can accept right now is a fact about one browser page, not about the
 * server: Gradio keeps a session per page, so the model and the reference list
 * this Forge tab is looking at belong to the iframe in *this* document and
 * nowhere else. That is the whole reason this file exists. It talks to that one
 * iframe, by postMessage, and it refuses to answer from anything else - not
 * from a remembered list, not from a server-side default, not from a second
 * page's session. When there is no live iframe here there is no receiver, and
 * the Send menu says so instead of guessing.
 *
 * Every message is checked in both directions before it counts: the exact
 * origin of this page, the exact window the iframe owns, our protocol version,
 * a type that is legal for that direction, our channel id, a request id we are
 * waiting on, and a size under the shared ceiling. Anything else is dropped
 * without a reply and without a trace, because a message that fails any of
 * those is either a bug or somebody else's, and there is no third case. The
 * target origin is always this origin; "*" is never written here.
 *
 * The channel id is correlation, not authentication - code already running on
 * the Forge origin can read it like anything else on the page. What it buys is
 * that a message from a previous iframe load, a different page or the wrong
 * frame is recognised and ignored rather than acted on. When the iframe
 * reloads, the old channel is dead: a fresh handshake mints a new one, and any
 * request still waiting on the old one is failed rather than quietly re-aimed
 * at a session that never agreed to receive it.
 *
 * Nothing here polls. The handshake retries a bounded number of times after an
 * iframe load and then stops; a receiver query is one question with one
 * deadline; a send is one request with one acknowledgement. Two things do
 * outlive a call, and both are as narrow as they can be: the window's message
 * listener, which is how a bridge answers at all, and one observer on the
 * tab's own iframe container, because the moment WanGP's page appears in this
 * document is a moment nothing else announces. The document itself is never
 * watched and no tab is ever clicked to make any of this happen.
 */
window.minipaintWanGP = (function () {
    "use strict";

    /* -------------------------------------------------------------------- */
    /* The shared vocabulary. Mirrors minipaint_neo/wangp/protocol.py; the    */
    /* names, the ids and the ceilings are that file's, not this one's.       */
    /* -------------------------------------------------------------------- */

    const PROTOCOL = 3;

    const HELLO = "WANGP_BRIDGE_HELLO";
    const READY = "WANGP_BRIDGE_READY";
    const GET_RECEIVERS = "WANGP_GET_RECEIVERS";
    const RECEIVERS = "WANGP_RECEIVERS";
    const RECEIVE_IMAGE = "WANGP_RECEIVE_IMAGE";
    const RECEIVE_RESULT = "WANGP_RECEIVE_RESULT";
    const FOCUS_RECEIVER = "WANGP_FOCUS_RECEIVER";
    const THEME_STATE = "WANGP_THEME_STATE";
    const RUNTIME_STATE = "WANGP_RUNTIME_STATE";
    // Protocol 3: the queue. A request overlays a prompt and images on the
    // live page and asks it to add itself to WanGP's own queue; a
    // confirmation asks whether it landed. See minipaint.wangp.queue/v1.
    const QUEUE_REQUEST = "WANGP_QUEUE_REQUEST";
    const QUEUE_RESULT = "WANGP_QUEUE_RESULT";
    const QUEUE_CONFIRM = "WANGP_QUEUE_CONFIRM";
    const QUEUE_STATUS = "WANGP_QUEUE_STATUS";

    const TO_BRIDGE = [HELLO, GET_RECEIVERS, RECEIVE_IMAGE, FOCUS_RECEIVER, THEME_STATE, QUEUE_REQUEST, QUEUE_CONFIRM];
    const TO_PARENT = [READY, RECEIVERS, RECEIVE_RESULT, RUNTIME_STATE, QUEUE_RESULT, QUEUE_STATUS];

    const RECEIVER_IDS = ["start_frame", "end_frame", "reference", "control_image", "positioned_ref", "style_ref"];
    const ROLES = ["start", "end", "reference", "control", "positioned", "style"];
    const OPERATIONS = ["replace", "append"];
    const DEFAULT_MENU_LABELS = {
        start_frame: "Send Image to WanGP Start Frame",
        end_frame: "Send Image to WanGP End Frame",
        reference: "Send Image to WanGP Reference",
        control_image: "Send Image to WanGP Control Image",
        positioned_ref: "Send Image to WanGP Positioned Reference",
        style_ref: "Send Image to WanGP Style Reference"
    };
    const VERIFICATION_LEVELS = ["byte-identical", "pixel-equivalent", "structurally verified"];

    const MAX_ENVELOPE_BYTES = 256 * 1024;
    const RECEIVER_QUERY_TIMEOUT_MS = 10000;
    // How long an answer is still taken after its query timed out. The menu
    // has already said "did not answer in time"; an answer inside this window
    // corrects it in place rather than being thrown away.
    const LATE_ANSWER_GRACE_MS = 60000;
    const RECEIVE_TIMEOUT_MS = 30000;
    // The queue's two round trips, and the bounded confirmation schedule:
    // once at once, then a short sequence, then never again. No timer
    // outlives the call that started it.
    const QUEUE_REQUEST_TIMEOUT_MS = 30000;
    const QUEUE_CONFIRM_TIMEOUT_MS = 10000;
    const QUEUE_CONFIRM_DELAYS_MS = [0, 100, 300, 700, 1500, 3000];
    const PROMPT_MAX_CHARS = 4000;
    const MAX_QUEUE_REFERENCES = 16;
    const QUEUE_FIELDS = ["prompt", "start", "end", "references"];
    const ADMISSIONS = ["requested", "duplicate", "refused"];
    const QUEUE_STATUSES = ["pending", "queued", "refused", "expired"];

    // A handoff id and a channel id have one shape each, and a value that is
    // not exactly that shape is refused rather than repaired. See handoff.py.
    const HEX32 = /^[0-9a-f]{32}$/;
    const REVISION_RE = /^[0-9a-f]{8,64}$/;
    // What may pass for a failure code coming back from the bridge: the shape
    // errors.py uses, so a bridge cannot hand the menu a sentence to display.
    const CODE_RE = /^[A-Z][A-Z0-9_]{2,59}$/;
    const OPAQUE_RE = /^[A-Za-z0-9._:-]{1,128}$/;

    /* The failure codes this file can produce. errors.py owns the sentences;
     * these are the codes the menu and the send log agree on. */
    const IFRAME_NOT_READY = "IFRAME_NOT_READY";
    const BRIDGE_SESSION_MISMATCH = "BRIDGE_SESSION_MISMATCH";
    const RECEIVER_QUERY_TIMEOUT = "RECEIVER_QUERY_TIMEOUT";
    const NO_ACTIVE_RECEIVER = "NO_ACTIVE_RECEIVER";
    const UNKNOWN_RECEIVER = "UNKNOWN_RECEIVER";
    const STALE_RECEIVER_STATE = "STALE_RECEIVER_STATE";
    const RECEIVER_DISABLED = "RECEIVER_DISABLED";
    const RECEIVER_LIMIT_REACHED = "RECEIVER_LIMIT_REACHED";
    const RECEIVER_VERIFY_FAILED = "RECEIVER_VERIFY_FAILED";
    const HANDOFF_INVALID_ID = "HANDOFF_INVALID_ID";
    const WANGP_RESTARTED = "WANGP_RESTARTED";
    const INTERNAL_ERROR = "INTERNAL_ERROR";
    const BRIDGE_COMPONENT_INCOMPATIBLE = "BRIDGE_COMPONENT_INCOMPATIBLE";
    const REQUEST_INVALID = "REQUEST_INVALID";
    const REQUEST_ID_CONFLICT = "REQUEST_ID_CONFLICT";
    const PROMPT_TOO_LONG = "PROMPT_TOO_LONG";
    const QUEUE_BUSY = "QUEUE_BUSY";
    const QUEUE_REQUEST_REFUSED = "QUEUE_REQUEST_REFUSED";
    const ADMISSION_UNCONFIRMED = "ADMISSION_UNCONFIRMED";
    const WANGP_VALIDATION_REFUSED = "WANGP_VALIDATION_REFUSED";

    // The few sentences this side needs before the server can supply one. The
    // wording is errors.py's, kept short enough for a menu line.
    const MESSAGES = {
        IFRAME_NOT_READY: "Open the WanGP tab and choose an input.",
        BRIDGE_SESSION_MISMATCH: "That WanGP page is no longer the one this send was prepared for.",
        RECEIVER_QUERY_TIMEOUT: "WanGP did not answer in time.",
        NO_ACTIVE_RECEIVER: "This WanGP model takes no image.",
        UNKNOWN_RECEIVER: "WanGP does not have that input.",
        STALE_RECEIVER_STATE: "The WanGP input changed; reopen Send to.",
        RECEIVER_DISABLED: "That WanGP input is not active right now.",
        RECEIVER_LIMIT_REACHED: "That WanGP input is full.",
        RECEIVER_VERIFY_FAILED: "WanGP took an image, but it could not be confirmed as the one that was sent.",
        HANDOFF_INVALID_ID: "That is not a handoff this extension made.",
        WANGP_RESTARTED: "WanGP restarted while the image was on its way.",
        INTERNAL_ERROR: "The WanGP integration hit an unexpected problem.",
        BRIDGE_COMPONENT_INCOMPATIBLE: "This WanGP bridge needs to be reinstalled or updated.",
        REQUEST_INVALID: "That queue request is not one this extension can carry.",
        REQUEST_ID_CONFLICT: "That request id was already used for a different request.",
        PROMPT_TOO_LONG: "The prompt is longer than WanGP queue requests allow (4000 characters).",
        QUEUE_BUSY: "WanGP is still taking the previous queue request; try again in a moment.",
        QUEUE_REQUEST_REFUSED: "WanGP did not take the queue request.",
        ADMISSION_UNCONFIRMED: "WanGP did not confirm that the request was added to the queue.",
        WANGP_VALIDATION_REFUSED: "WanGP declined the queue request; check the WanGP page for details."
    };

    /* The tab's own elements. The tab builds these once and only changes what
     * is visible, so finding them is a lookup, never a watch. */
    const IFRAME_ROOT_ID = "wangp_iframe_root";
    const IFRAME_ID = "wangp_iframe";
    // The tab mints the channel id per iframe load and puts it in a hidden
    // textbox rather than in the iframe's URL, so that "which session is this"
    // and "where do the bytes go" stay two separate facts. The attributes are
    // the fallback for a page that hands it over some other way.
    const CHANNEL_ELEM_ID = "wangp_channel";
    const BROWSER_CHECK_ELEM_ID = "wangp_browser_check";
    const SESSION_ELEM_ID = "wangp_session";
    const CHANNEL_ATTRIBUTE = "data-minipaint-channel";
    const INSTANCE_ATTRIBUTE = "data-minipaint-instance";

    // How often the container is looked for before this file gives up on the
    // page having a WanGP tab at all, in milliseconds. Six lookups and then
    // nothing, ever - the container is built with the page, so not finding it
    // by now means this build has no tab to find.
    const ROOT_LOOKUPS = [0, 200, 800, 2000, 5000, 9000];

    // How long the parent keeps offering a handshake after an iframe load, in
    // milliseconds between attempts. WanGP's own Gradio app can take a while
    // to run its scripts, so the last attempts are far apart - and then it
    // stops. A menu that finds no session re-arms one round; nothing repeats
    // on its own.
    const HELLO_DELAYS = [200, 500, 1200, 2500, 5000, 7000, 9000];

    const S = {
        frame: null,
        channelId: "",
        instanceId: "",
        bridgeSession: "",
        bridgeVersion: "",
        ready: false,
        model: null,
        receivers: [],
        revision: "",
        pending: new Map(),
        authProbe: null,
        query: null,
        helloTimer: null,
        helloStep: 0,
        listening: false,
        watcher: null,
        lastCode: "",
        theme: "",
        // Whether the bridge in this page can take a queue request at all: the
        // handshake says, and a build that lacks a queue-critical component
        // says no while still taking an image send.
        queue: false
    };

    /* ------------------------------------------------------------------ */
    /* Shapes                                                                */
    /* ------------------------------------------------------------------ */

    function hex32() {
        const bytes = new Uint8Array(16);
        if (window.crypto && typeof window.crypto.getRandomValues === "function") {
            window.crypto.getRandomValues(bytes);
        } else {
            // Not a secret (section 13.3), only an id two frames must agree on.
            for (let k = 0; k < bytes.length; k++) { bytes[k] = Math.floor(Math.random() * 256); }
        }
        let text = "";
        for (const byte of bytes) { text += byte.toString(16).padStart(2, "0"); }
        return text;
    }

    function text(value, limit) {
        return typeof value === "string" ? value.slice(0, limit || 200) : "";
    }

    function code(value) {
        const word = text(value, 60);
        return CODE_RE.test(word) ? word : "";
    }

    function sentence(failure) {
        return MESSAGES[failure] || MESSAGES.INTERNAL_ERROR;
    }

    function failure(failureCode, detail) {
        S.lastCode = failureCode;
        return { ok: false, code: failureCode, message: sentence(failureCode), detail: text(detail, 200) };
    }

    /** The size of an envelope as the shared ceiling counts it. A value that
     * cannot even be stringified is over every limit there is. */
    function envelopeBytes(value) {
        let serialised;
        try {
            serialised = JSON.stringify(value);
        } catch (e) {
            return Infinity;
        }
        if (typeof serialised !== "string") { return Infinity; }
        if (typeof TextEncoder === "function") {
            try { return new TextEncoder().encode(serialised).length; } catch (e) { /* fall through */ }
        }
        return serialised.length * 2;
    }

    /**
     * One receiver descriptor as the menu will use it, or null. The bridge
     * has already normalised this with its own copy of protocol.py; it is
     * done again here because a descriptor the parent cannot fully account
     * for must not become a clickable destination. An unknown id, an unknown
     * operation or an unknown role means "do not offer this".
     */
    function normaliseReceiver(raw) {
        if (!raw || typeof raw !== "object") { return null; }
        const id = raw.id;
        if (RECEIVER_IDS.indexOf(id) === -1) { return null; }
        if (OPERATIONS.indexOf(raw.operation) === -1) { return null; }
        if (ROLES.indexOf(raw.role) === -1) { return null; }

        const count = Number.isFinite(raw.count) ? Math.trunc(raw.count) : 0;
        const maxCount = Number.isFinite(raw.max_count) ? Math.trunc(raw.max_count) : null;
        const full = maxCount !== null && count >= maxCount;
        const label = text(raw.label, 80) || id.replace(/_/g, " ");
        const enabled = raw.enabled !== false && raw.visible !== false && !full;
        // Switched on right now, as opposed to offered because the model
        // allows it; a bridge that predates the distinction only ever offered
        // what was switched on, so its silence means "selected".
        const selected = raw.selected === undefined ? enabled : raw.selected === true;
        const switchToken = (typeof raw.switch === "string" && /^[a-z_]{1,40}$/.test(raw.switch)) ? raw.switch : "";
        return {
            id: id,
            role: raw.role,
            label: label,
            menu_label: text(raw.menu_label, 120) || DEFAULT_MENU_LABELS[id] || ("Send Image to WanGP " + label),
            operation: raw.operation,
            enabled: enabled,
            count: count,
            max_count: maxCount,
            full: full,
            focus_hint: text(raw.focus_hint, 120),
            reason_code: code(raw.reason_code),
            selected: selected,
            switch: enabled && !selected ? switchToken : ""
        };
    }

    /** The list in the order the ids are declared, so two openings that
     * describe the same state cannot reshuffle the menu. */
    function normaliseReceivers(raw) {
        if (!Array.isArray(raw)) { return []; }
        const seen = {};
        for (const item of raw) {
            const descriptor = normaliseReceiver(item);
            if (descriptor && !seen[descriptor.id]) { seen[descriptor.id] = descriptor; }
        }
        return RECEIVER_IDS.filter(function (id) { return !!seen[id]; }).map(function (id) { return seen[id]; });
    }

    function normaliseModel(raw) {
        if (!raw || typeof raw !== "object") { return null; }
        return { label: text(raw.label, 120), type: text(raw.type, 120), family: text(raw.family, 60) };
    }

    /* ------------------------------------------------------------------ */
    /* The iframe                                                            */
    /* ------------------------------------------------------------------ */

    function app() {
        try {
            if (typeof gradioApp === "function") { return gradioApp() || document; }
        } catch (e) { /* fall through */ }
        return document;
    }

    /** The tab's iframe, if this build has the tab and the tab has built it. */
    function frameElement() {
        const scope = app();
        const direct = scope.getElementById ? scope.getElementById(IFRAME_ID) : null;
        if (direct && direct.tagName === "IFRAME") { return direct; }
        const root = scope.querySelector ? scope.querySelector("#" + IFRAME_ROOT_ID) : null;
        return root ? root.querySelector("iframe") : null;
    }

    /** A hidden textbox of the WanGP tab, by id. Values, never URLs. */
    function box(elementId) {
        const scope = app();
        return scope.querySelector ? scope.querySelector("#" + elementId + " textarea") : null;
    }

    /** Write one of those the way a user would, so Gradio sees the change. */
    function writeBox(elementId, value) {
        const target = box(elementId);
        if (!target) { return false; }
        target.value = value;
        target.dispatchEvent(new Event("input", { bubbles: true }));
        return true;
    }

    function attribute(element, name) {
        const own = element && element.getAttribute ? element.getAttribute(name) : null;
        if (own) { return own; }
        const parent = element && element.closest ? element.closest("#" + IFRAME_ROOT_ID) : null;
        return (parent && parent.getAttribute(name)) || "";
    }

    function origin() {
        return window.location.origin;
    }

    /* ------------------------------------------------------------------ */
    /* Messages out                                                          */
    /* ------------------------------------------------------------------ */

    /**
     * Post one envelope into the iframe. The same rules apply on the way out
     * as on the way in - our protocol, a type legal for this direction, our
     * channel, a request id, a size under the ceiling - because a message
     * this side cannot justify is a message the bridge should never have to
     * judge. The target origin is this page's own, always.
     */
    function post(kind, requestId, payload) {
        const frame = S.frame;
        const window_ = frame ? frame.contentWindow : null;
        if (!window_ || TO_BRIDGE.indexOf(kind) === -1) { return false; }
        if (!HEX32.test(S.channelId) || !HEX32.test(requestId)) { return false; }
        const envelope = {
            protocol: PROTOCOL,
            type: kind,
            channel_id: S.channelId,
            request_id: requestId,
            payload: payload && typeof payload === "object" ? payload : {}
        };
        if (envelopeBytes(envelope) >= MAX_ENVELOPE_BYTES) { return false; }
        try {
            window_.postMessage(envelope, origin());
        } catch (e) {
            return false;
        }
        return true;
    }

    /** A request that expects one answer, with one deadline and no retry.
     * ``late``, when given, is called with an answer that arrives after the
     * deadline but inside the grace window - the promise has already resolved
     * with the timeout by then, and this is how the answer still lands. */
    function ask(kind, payload, timeout, expects, late) {
        const requestId = hex32();
        const askedAt = Date.now();
        return new Promise(function (resolve) {
            const entry = {
                type: expects,
                channel: S.channelId,
                session: S.bridgeSession,
                askedAt: askedAt,
                resolve: resolve,
                late: typeof late === "function" ? late : null,
                expired: false,
                timer: setTimeout(function () {
                    say(kind + ": no answer within " + timeout + " ms (" + requestId.slice(0, 8) + ")");
                    if (entry.late) {
                        // Kept, marked, and dropped later: an answer in the
                        // grace window is still this request's answer.
                        entry.expired = true;
                        entry.timer = setTimeout(function () { S.pending.delete(requestId); }, LATE_ANSWER_GRACE_MS);
                    } else {
                        S.pending.delete(requestId);
                    }
                    resolve(failure(RECEIVER_QUERY_TIMEOUT, kind));
                }, timeout)
            };
            S.pending.set(requestId, entry);
            if (!post(kind, requestId, payload)) {
                clearTimeout(entry.timer);
                S.pending.delete(requestId);
                resolve(failure(IFRAME_NOT_READY, kind));
                return;
            }
            say(kind + ": asked (" + requestId.slice(0, 8) + ")");
        });
    }

    function settle(requestId, result) {
        const entry = S.pending.get(requestId);
        if (!entry) { return false; }
        clearTimeout(entry.timer);
        S.pending.delete(requestId);
        const waited = Date.now() - (entry.askedAt || Date.now());
        if (result && result.ok) {
            say(entry.type + ": answered after " + waited + " ms" + (entry.expired ? " (late, taken)" : ""));
        } else {
            say(entry.type + ": refused after " + waited + " ms - " + ((result && result.code) || "?")
                + ((result && result.detail) ? " (" + result.detail + ")" : "") + (entry.expired ? " (late)" : ""));
        }
        if (entry.expired) {
            if (entry.late) { try { entry.late(result); } catch (e) { /* the menu's business */ } }
            return true;
        }
        entry.resolve(result);
        return true;
    }

    /** Every request still waiting is failed. Called when the page it was
     * prepared for stops being the page that would answer it. */
    function abandon(failureCode) {
        const waiting = Array.from(S.pending.keys());
        for (const requestId of waiting) { settle(requestId, failure(failureCode)); }
        S.query = null;
    }

    /* ------------------------------------------------------------------ */
    /* The handshake                                                         */
    /* ------------------------------------------------------------------ */

    function stopHandshake() {
        if (S.helloTimer) { clearTimeout(S.helloTimer); S.helloTimer = null; }
    }

    /**
     * Offer a handshake a bounded number of times and then stop. WanGP's own
     * page may still be booting when this iframe fires its load event, so a
     * single HELLO would often be spoken to nobody; a schedule that ends is
     * the difference between that and a page that pings forever.
     */
    function beginHandshake(fresh) {
        stopHandshake();
        if (!S.frame) { return; }
        if (fresh) {
            // A reload is a new session: the old channel cannot be reused,
            // and nothing that was waiting on it may be answered by the page
            // that just replaced it.
            abandon(BRIDGE_SESSION_MISMATCH);
            S.ready = false;
            S.queue = false;
            S.bridgeSession = "";
            S.receivers = [];
            S.revision = "";
            S.model = null;
            const supplied = box(CHANNEL_ELEM_ID);
            const declared = (supplied && supplied.value) || attribute(S.frame, CHANNEL_ATTRIBUTE);
            // The tab's own id when it gave one - it is the one the Forge side
            // opened - and otherwise one minted here, because a page with no
            // channel has no way to tell its own iframe from anyone else's.
            S.channelId = HEX32.test(String(declared || "")) ? String(declared) : hex32();
            S.instanceId = text(attribute(S.frame, INSTANCE_ATTRIBUTE), 128);
        }
        say("handshake: starting, channel=" + S.channelId.slice(0, 8) + " origin=" + origin());
        S.helloStep = 0;
        step();
    }

    function step() {
        S.helloTimer = null;
        if (S.ready || !S.frame) { return; }
        if (S.helloStep >= HELLO_DELAYS.length) {
            // The offers are over. Say so where the tab's setup checklist
            // reads it, rather than leaving a row that never resolves.
            say(
                "handshake: gave up after " + HELLO_DELAYS.length + " offers with no reply. " +
                "Either the WanGP page has no MiniPaint bridge plugin loaded, or its script " +
                "did not run."
            );
            report(false, "the WanGP page in this browser did not answer the handshake");
            return;
        }
        const wait = HELLO_DELAYS[S.helloStep];
        S.helloStep += 1;
        say("handshake: offer " + S.helloStep + "/" + HELLO_DELAYS.length + " sent into the iframe");
        post(HELLO, hex32(), { instance_id: S.instanceId, protocol: PROTOCOL });
        S.helloTimer = setTimeout(step, wait);
    }

    /** One more round of offers, for a menu that found no session. Bounded
     * like the first, and a no-op while one is already running. */
    function rearm() {
        if (S.ready || S.helloTimer || !S.frame) { return; }
        S.helloStep = 0;
        step();
    }

    /**
     * Tell the tab's setup checklist whether a message went into the proxied
     * iframe and came back. This side is the only one that can know - the
     * round trip happens entirely in the browser - and it is a report, not a
     * gate: nothing here decides whether an image may be handed over.
     */
    /**
     * Hand this page's session to the Forge side. Bookkeeping only: the
     * receiver list a send is judged against is the one the live iframe
     * answers with at the moment the menu opens, and the gate that refuses a
     * stale or unknown receiver runs inside WanGP. This is what lets the
     * diagnostics report say which model a page is on, and what lets a channel
     * survive a repaint - so it is written to be unable to throw.
     */
    function recordSession(introducing) {
        try {
            if (!S.channelId || !S.bridgeSession) { return; }
            writeBox(SESSION_ELEM_ID, JSON.stringify({
                protocol: PROTOCOL,
                introducing: !!introducing,
                bridge_session: S.bridgeSession,
                instance_id: S.instanceId || "",
                bridge_version: S.bridgeVersion || "",
                state_revision: S.revision || "",
                model: S.model || {},
                receivers: S.receivers || [],
                ready: !!S.ready,
                code: S.lastCode || ""
            }));
        } catch (e) { /* a record is never worth an exception */ }
    }

    const CLIENT_LOG_ELEM_ID = "wangp_client_log";
    const AUTH_PROBE_PATH = "/wan2gp/__minipaint_auth_probe";
    let logSeq = 0;

    /**
     * Say what just happened, where somebody can read it. The handshake lives
     * entirely in this window and the iframe's, so when it does not happen
     * there is otherwise nothing at all to look at - no request, no console
     * anybody thought to open, and one red row that only says "did not
     * answer". Each line goes to the page's own log through a hidden textbox.
     */
    // The journal box is one Gradio textbox, and Gradio reads it when its
    // change event runs, not when the value is written: two lines written in
    // the same tick reached the server as the second one twice and the first
    // one never. So every line gets a sequence number, the box always holds
    // the last few lines (LOG_WINDOW) rather than the last one, and the
    // server journals each sequence number once - a write that is skipped
    // over is carried by the next, and a write that is read twice is dropped.
    const LOG_WINDOW = 24;
    const LOG_PAGE = hex32().slice(0, 12);
    const logLines = [];
    let logFlush = 0;

    function flushLog() {
        logFlush = 0;
        try {
            writeBox(CLIENT_LOG_ELEM_ID, JSON.stringify({ p: LOG_PAGE, n: logSeq, lines: logLines.slice(-LOG_WINDOW) }));
        } catch (e) { /* a log line is never worth an exception */ }
    }

    function say(message) {
        try {
            logSeq += 1;
            if (typeof console !== "undefined" && console.debug) {
                console.debug("MiniPaint WanGP:", message);
            }
            logLines.push({ s: logSeq, line: String(message).slice(0, 300) });
            if (logLines.length > LOG_WINDOW) { logLines.splice(0, logLines.length - LOG_WINDOW); }
            if (!logFlush) { logFlush = setTimeout(flushLog, 0); }
        } catch (e) { /* a log line is never worth an exception */ }
    }

    function report(roundTrip, detail) {
        try {
            writeBox(BROWSER_CHECK_ELEM_ID, JSON.stringify({
                round_trip: !!roundTrip,
                channel: S.channelId,
                origin: origin(),
                detail: text(detail, 200),
                auth_probe: S.authProbe
            }));
        } catch (e) { /* a checklist row is not worth an exception */ }
    }

    /**
     * Ask this Forge, without credentials, for a route under our own prefix
     * that answers nothing. The server cannot do this for itself: it would
     * have to guess its own address, and behind TLS or a reverse proxy the
     * guess is wrong in ways that matter. The page already knows the exact
     * origin a real visitor uses, so it asks from there.
     *
     * 401 or 403 means Forge's sign-in got there first, and /wan2gp/ is
     * behind it. 204 means it did not, and the route is open to anyone who
     * can reach this Forge. Either way it is an answer, which is what the
     * checklist could not get before.
     */
    function probeAuth() {
        if (typeof fetch !== "function") {
            S.authProbe = { ran: false, reason: "this browser has no fetch()" };
            return Promise.resolve(S.authProbe);
        }
        return fetch(AUTH_PROBE_PATH, {
            method: "GET",
            credentials: "omit",
            cache: "no-store",
            redirect: "manual"
        }).then(function (response) {
            S.authProbe = { ran: true, status: response.status, type: response.type };
            say("auth probe: unauthenticated GET " + AUTH_PROBE_PATH + " answered " + response.status);
            return S.authProbe;
        }).catch(function (error) {
            S.authProbe = { ran: false, reason: String(error && error.message ? error.message : error).slice(0, 120) };
            say("auth probe: could not be made (" + S.authProbe.reason + ")");
            return S.authProbe;
        });
    }

    /* ------------------------------------------------------------------ */
    /* Messages in                                                           */
    /* ------------------------------------------------------------------ */

    /**
     * Everything a message must be before any of it is believed. Origin and
     * source window first, because they are the only two facts a page cannot
     * forge from inside another frame; then the envelope, which is only worth
     * reading once those hold.
     */
    function acceptable(event) {
        // Every rejection below is deliberate and stays silent to the sender.
        // Only a message that looks like it was meant for us is worth a line
        // in the log - one that failed a check we care about while claiming
        // our protocol. Anything else on the page's message bus is not ours
        // and is not news.
        const claims = event && event.data && typeof event.data === "object" && event.data.protocol !== undefined;
        if (event.origin !== origin()) {
            if (claims) { dropped("wrong origin, expected " + origin(), event); }
            return null;
        }
        if (!S.frame || event.source !== S.frame.contentWindow) {
            if (claims) { dropped(S.frame ? "not from the WanGP iframe" : "no iframe is attached", event); }
            return null;
        }
        const message = event.data;
        if (!message || typeof message !== "object" || Array.isArray(message)) { return null; }
        if (message.protocol !== PROTOCOL) {
            dropped("protocol " + message.protocol + ", this build speaks " + PROTOCOL, event);
            return null;
        }
        if (TO_PARENT.indexOf(message.type) === -1) {
            dropped("type is not one the iframe may send", event);
            return null;
        }
        if (typeof message.channel_id !== "string" || message.channel_id !== S.channelId) {
            dropped("channel " + String(message.channel_id).slice(0, 8) + ", expected " + S.channelId.slice(0, 8), event);
            return null;
        }
        if (typeof message.request_id !== "string" || !message.request_id) {
            dropped("no request id", event);
            return null;
        }
        const payload = message.payload === undefined ? {} : message.payload;
        if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
            dropped("payload is not an object", event);
            return null;
        }
        if (envelopeBytes(message) >= MAX_ENVELOPE_BYTES) {
            dropped("envelope is too large", event);
            return null;
        }
        return { type: message.type, requestId: message.request_id, payload: payload };
    }

    function onMessage(event) {
        const message = acceptable(event);
        if (!message) { return; }
        say("received " + message.type + " from the iframe");
        if (message.type === READY) { onReady(message.payload); return; }
        if (message.type === RUNTIME_STATE) { onRuntimeState(message.payload); return; }
        if (message.type === RECEIVERS) { onReceivers(message.requestId, message.payload); return; }
        if (message.type === RECEIVE_RESULT) { onResult(message.requestId, message.payload); return; }
        if (message.type === QUEUE_RESULT) { onQueueResult(message.requestId, message.payload); return; }
        if (message.type === QUEUE_STATUS) { onQueueStatus(message.requestId, message.payload); }
    }

    /** Why an inbound message was dropped. Every rejection is silent by
     * design - answering an unknown sender is how a bridge gets abused - but
     * silent to the sender is not the same as invisible to the operator. */
    function dropped(why, event) {
        try {
            const from = event && event.origin ? String(event.origin) : "(no origin)";
            const kind = event && event.data && event.data.type ? String(event.data.type) : "(no type)";
            say("dropped a message: " + why + " (from " + from + ", type " + kind + ")");
        } catch (e) { /* nothing */ }
    }

    /**
     * The bridge introducing itself, or introducing a new session in place of
     * the one we had. A different bridge session means the iframe reloaded or
     * WanGP restarted underneath it: whatever was in flight was prepared for a
     * page that no longer exists, and it fails rather than lands.
     */
    function onReady(payload) {
        const session = text(payload.bridge_session, 128);
        if (!OPAQUE_RE.test(session)) {
            // The bridge answered and could not even name a session for this
            // page: the request reached its Gradio event, and that event had
            // nothing to derive one from. That is an answer - a refusal with
            // a code - not silence, and it is reported as one so the checklist
            // row says why rather than "did not answer". Offering again would
            // get the same refusal, so the handshake stops here.
            const refused = text(payload.code || payload.reason_code, 60) || BRIDGE_SESSION_MISMATCH;
            stopHandshake();
            S.ready = false;
            S.lastCode = refused;
            say("handshake: the bridge answered but refused - " + refused + " (it named no session for this page)");
            report(false, "the bridge answered but refused: " + refused);
            return;
        }
        const instance = text(payload.instance_id, 128);
        const restarted = !!S.instanceId && !!instance && instance !== S.instanceId;
        if (S.bridgeSession && S.bridgeSession !== session) {
            abandon(restarted ? WANGP_RESTARTED : BRIDGE_SESSION_MISMATCH);
        }
        stopHandshake();
        S.bridgeSession = session;
        if (instance) { S.instanceId = instance; }
        S.bridgeVersion = text(payload.version, 40);

        // The handshake says whether the bridge can actually work here, and it
        // is allowed to say no: a plugin of the wrong version, or one that
        // could not resolve a component this WanGP build was meant to have,
        // reports ready false and the reason. Taking the word "READY" as the
        // answer instead would leave the menu saying a model takes no image
        // when what is really wrong is the plugin - so believe the flag, keep
        // the code, and let the ordinary failure path explain it.
        const declared = payload.ready !== false;
        const failure = text(payload.code || payload.reason_code, 60);
        S.model = normaliseModel(payload.model);
        S.receivers = declared ? normaliseReceivers(payload.receivers) : [];
        S.revision = declared && REVISION_RE.test(text(payload.state_revision, 64)) ? payload.state_revision : "";
        S.ready = declared;
        S.queue = declared && !!(payload.capabilities && payload.capabilities.queue === true);
        S.lastCode = declared ? "" : (failure || "BRIDGE_COMPONENT_INCOMPATIBLE");
        report(declared, S.lastCode);
        recordSession(true);
        // Presentation only, and never a reason a picture cannot be sent.
        try { theme(S.theme || detectTheme()); } catch (e) { /* section 27.1 */ }
    }

    /** The bridge saying the process behind it changed state. Advisory: it can
     * take readiness away, never grant it. */
    function onRuntimeState(payload) {
        const instance = text(payload.instance_id, 128);
        if (instance && S.instanceId && instance !== S.instanceId) {
            S.ready = false;
            S.bridgeSession = "";
            S.receivers = [];
            S.revision = "";
            abandon(WANGP_RESTARTED);
            beginHandshake(true);
            return;
        }
        if (payload.state && payload.state !== "READY") {
            S.ready = false;
            abandon(WANGP_RESTARTED);
        }
    }

    function onReceivers(requestId, payload) {
        const entry = S.pending.get(requestId);
        if (!entry || entry.type !== RECEIVERS) { return; }
        if (payload.ok === false) {
            // The bridge answered and refused, and its code is the diagnosis.
            // An error acknowledgement carries no state revision, so filing it
            // as a stale revision - which is what happened - reached the menu
            // as "unavailable" with the actual reason lost on the way.
            settle(requestId, failure(code(payload.code) || INTERNAL_ERROR, text(payload.detail, 200)));
            return;
        }
        const session = text(payload.bridge_session, 128) || S.bridgeSession;
        if (entry.session && session !== entry.session) {
            settle(requestId, failure(BRIDGE_SESSION_MISMATCH));
            return;
        }
        const revision = text(payload.state_revision, 64);
        if (!REVISION_RE.test(revision)) {
            settle(requestId, failure(STALE_RECEIVER_STATE, "the bridge sent no usable state revision"));
            return;
        }
        const receivers = normaliseReceivers(payload.receivers);
        S.receivers = receivers;
        S.revision = revision;
        S.bridgeSession = session;
        S.model = normaliseModel(payload.model) || S.model;
        recordSession(false);
        settle(requestId, {
            ok: true,
            code: "",
            receivers: receivers,
            state_revision: revision,
            bridge_session: session,
            model: S.model
        });
    }

    /**
     * The acknowledgement, judged rather than trusted. Section 23: an answer
     * that does not name the receiver that was asked for, at a verification
     * level this side recognises, has not proved anything, and an unproved
     * send is a failed send.
     */
    /** A hex digest, or "". Never anything else, whatever the bridge said. */
    function digest(value) {
        const seen = text(value, 128);
        return /^[0-9a-f]{32,128}$/.test(seen) ? seen : "";
    }

    function onResult(requestId, payload) {
        const entry = S.pending.get(requestId);
        if (!entry || entry.type !== RECEIVE_RESULT) { return; }
        const failed = code(payload.code);
        if (payload.ok !== true) {
            settle(requestId, failure(failed || RECEIVER_VERIFY_FAILED, text(payload.detail, 200)));
            return;
        }
        if (payload.receiver_id !== entry.receiver) {
            settle(requestId, failure(RECEIVER_VERIFY_FAILED, "the acknowledgement named another input"));
            return;
        }
        const verification = text(payload.verification, 40);
        if (VERIFICATION_LEVELS.indexOf(verification) === -1) {
            settle(requestId, failure(RECEIVER_VERIFY_FAILED, "unrecognised verification level"));
            return;
        }
        const after = text(payload.state_revision_after, 64);
        if (REVISION_RE.test(after)) { S.revision = after; }
        settle(requestId, {
            ok: true,
            code: "",
            receiver_id: payload.receiver_id,
            role: ROLES.indexOf(payload.role) === -1 ? "" : payload.role,
            operation: OPERATIONS.indexOf(payload.operation) === -1 ? "" : payload.operation,
            verification: verification,
            width: Number.isFinite(payload.width) ? Math.trunc(payload.width) : 0,
            height: Number.isFinite(payload.height) ? Math.trunc(payload.height) : 0,
            new_count: Number.isFinite(payload.new_count) ? Math.trunc(payload.new_count) : 0,
            state_revision_after: REVISION_RE.test(after) ? after : "",
            // Carried, not read: the digests mean nothing here, and everything
            // to the side that still holds the manifest of the file it wrote.
            source_digest: digest(payload.source_digest),
            receiver_digest: digest(payload.receiver_digest),
            source_pixel_digest: digest(payload.source_pixel_digest),
            receiver_pixel_digest: digest(payload.receiver_pixel_digest),
            message: ""
        });
    }

    /* ------------------------------------------------------------------ */
    /* The queue: minipaint.wangp.queue/v1 over protocol 3                  */
    /* ------------------------------------------------------------------ */

    /** applied / inherited / ignored as the shared normaliser shapes them: a
     * field is in exactly one of the three, an ignored one carries a code. */
    function normaliseQueueSummary(raw) {
        const applied = raw && typeof raw.applied === "object" && raw.applied ? raw.applied : {};
        const out = { prompt: applied.prompt === true, start: applied.start === true, end: applied.end === true, references: 0 };
        if (Number.isFinite(applied.references) && applied.references > 0) { out.references = Math.trunc(applied.references); }
        else if (applied.references === true) { out.references = 1; }
        const ignored = [];
        const seen = {};
        for (const item of Array.isArray(raw && raw.ignored) ? raw.ignored : []) {
            if (!item || typeof item !== "object" || QUEUE_FIELDS.indexOf(item.field) === -1 || seen[item.field]) { continue; }
            seen[item.field] = true;
            ignored.push({ field: item.field, code: code(item.code) || RECEIVER_DISABLED });
        }
        const named = Array.isArray(raw && raw.inherited) ? raw.inherited : [];
        const inherited = QUEUE_FIELDS.filter(function (field) { return named.indexOf(field) !== -1 && !out[field] && !seen[field]; });
        return { applied: out, inherited: inherited, ignored: ignored };
    }

    function normaliseQueueResult(raw) {
        raw = raw && typeof raw === "object" ? raw : {};
        const admission = ADMISSIONS.indexOf(raw.admission) === -1 ? "refused" : raw.admission;
        const ok = raw.ok === true && admission !== "refused";
        const summary = normaliseQueueSummary(raw);
        return {
            ok: ok,
            request_id: HEX32.test(String(raw.request_id || "")) ? raw.request_id : "",
            bridge_session: text(raw.bridge_session, 128),
            admission: admission,
            applied: summary.applied,
            inherited: summary.inherited,
            ignored: summary.ignored,
            code: ok ? "" : (code(raw.code) || QUEUE_REQUEST_REFUSED),
            detail: text(raw.detail, 200),
            model: normaliseModel(raw.model) || S.model || { type: "", label: "", family: "" }
        };
    }

    function normaliseQueueStatus(raw) {
        raw = raw && typeof raw === "object" ? raw : {};
        const ok = raw.ok === true && QUEUE_STATUSES.indexOf(raw.status) !== -1;
        return {
            ok: ok,
            request_id: HEX32.test(String(raw.request_id || "")) ? raw.request_id : "",
            status: ok ? raw.status : "pending",
            tasks_added: ok && Number.isFinite(raw.tasks_added) && raw.tasks_added > 0 ? Math.trunc(raw.tasks_added) : 0,
            code: code(raw.code) || (ok ? "" : ADMISSION_UNCONFIRMED),
            detail: text(raw.detail, 200)
        };
    }

    function onQueueResult(requestId, payload) {
        const entry = S.pending.get(requestId);
        if (!entry || entry.type !== QUEUE_RESULT) { return; }
        const result = normaliseQueueResult(payload);
        const session = result.bridge_session || S.bridgeSession;
        if (entry.session && session !== entry.session) {
            settle(requestId, failure(BRIDGE_SESSION_MISMATCH));
            return;
        }
        if (!result.ok) {
            settle(requestId, Object.assign(failure(result.code, result.detail), { admission: "refused", request_id: result.request_id }));
            return;
        }
        settle(requestId, result);
    }

    function onQueueStatus(requestId, payload) {
        const entry = S.pending.get(requestId);
        if (!entry || entry.type !== QUEUE_STATUS) { return; }
        const status = normaliseQueueStatus(payload);
        if (!status.ok) {
            settle(requestId, Object.assign(failure(status.code, status.detail), { status: "pending", request_id: status.request_id }));
            return;
        }
        settle(requestId, status);
    }

    function queueRefusal(failureCode, detail, requestId) {
        say("queue: refused before asking - " + failureCode + (detail ? " (" + text(detail, 120) + ")" : ""));
        return Promise.resolve(Object.assign(failure(failureCode, detail), { admission: "refused", request_id: requestId || "" }));
    }

    /**
     * Section 12.1: ask the live page to add itself to WanGP's queue with the
     * given overrides. Every field is optional and an absent one is
     * inherited from the page; the ids are handoff ids of files this
     * extension wrote; the prompt is text and nothing else travels. Resolves
     * with the immediate admission answer, which is never "queued" - that
     * needs a confirmation.
     */
    function queue(request) {
        request = request && typeof request === "object" ? request : {};
        const requestId = HEX32.test(String(request.request_id || "")) ? String(request.request_id) : hex32();
        if (request.request_id !== undefined && request.request_id !== null && request.request_id !== "" && requestId !== request.request_id) {
            return queueRefusal(REQUEST_INVALID, "the request id is not 32 lowercase hex characters", "");
        }
        if (!ensure() || !S.ready || !S.bridgeSession) {
            rearm();
            return queueRefusal(IFRAME_NOT_READY, "no bridge session in this page", requestId);
        }
        if (!S.queue) { return queueRefusal(BRIDGE_COMPONENT_INCOMPATIBLE, "this bridge does not offer the queue", requestId); }
        const payload = { request_id: requestId, bridge_session: S.bridgeSession };
        if (request.prompt !== undefined && request.prompt !== null) {
            if (typeof request.prompt !== "string") { return queueRefusal(REQUEST_INVALID, "the prompt is not text", requestId); }
            if (request.prompt.length > PROMPT_MAX_CHARS) { return queueRefusal(PROMPT_TOO_LONG, "over " + PROMPT_MAX_CHARS + " characters", requestId); }
            if (request.prompt.trim()) { payload.prompt = request.prompt; }
        }
        for (const name of ["start_handoff_id", "end_handoff_id"]) {
            const value = request[name];
            if (value === undefined || value === null || value === "") { continue; }
            if (!HEX32.test(String(value))) { return queueRefusal(REQUEST_INVALID, name + " is not a handoff id", requestId); }
            payload[name] = String(value);
        }
        const refs = request.reference_handoff_ids;
        if (refs !== undefined && refs !== null) {
            if (!Array.isArray(refs)) { return queueRefusal(REQUEST_INVALID, "reference_handoff_ids is not a list", requestId); }
            const kept = refs.filter(function (item) { return item !== undefined && item !== null && item !== ""; });
            if (kept.length > MAX_QUEUE_REFERENCES) { return queueRefusal(REQUEST_INVALID, "more than " + MAX_QUEUE_REFERENCES + " reference images", requestId); }
            for (const item of kept) {
                if (!HEX32.test(String(item))) { return queueRefusal(REQUEST_INVALID, "a reference id is not a handoff id", requestId); }
            }
            if (kept.length) { payload.reference_handoff_ids = kept.map(String); }
        }
        const overrides = QUEUE_FIELDS.filter(function (field) {
            return field === "prompt" ? payload.prompt !== undefined
                : field === "references" ? !!payload.reference_handoff_ids
                : !!payload[field + "_handoff_id"];
        });
        say("queue " + requestId.slice(0, 8) + ": overrides " + (overrides.length ? overrides.join(", ") : "none (the live page as it is)"));
        return ask(QUEUE_REQUEST, payload, QUEUE_REQUEST_TIMEOUT_MS, QUEUE_RESULT).then(function (answer) {
            if (answer && answer.ok) { return answer; }
            return Object.assign({ admission: "refused", request_id: requestId }, answer);
        });
    }

    /** One bounded admission check for a request this page asked for. */
    function confirmQueue(requestId) {
        if (!HEX32.test(String(requestId || ""))) { return Promise.resolve(failure(REQUEST_INVALID, "not a request id")); }
        if (!ensure() || !S.ready || !S.bridgeSession) { return Promise.resolve(failure(IFRAME_NOT_READY, "no bridge session in this page")); }
        return ask(QUEUE_CONFIRM, { request_id: String(requestId), bridge_session: S.bridgeSession }, QUEUE_CONFIRM_TIMEOUT_MS, QUEUE_STATUS);
    }

    function pause(ms) {
        return new Promise(function (resolve) { setTimeout(resolve, ms); });
    }

    /**
     * The whole of one queue request: ask, then confirm on the bounded
     * schedule of section 12.3, and stop at the first terminal answer.
     * Resolves ``queued`` only on a positive observation; ``refused`` only on
     * the bridge's own refusal or WanGP's correlated one; and ``unconfirmed``
     * when the schedule ends with neither - never a guess either way.
     */
    async function queueAndConfirm(request) {
        const asked = await queue(request);
        const base = {
            request_id: asked.request_id || "",
            applied: asked.applied || normaliseQueueSummary({}).applied,
            inherited: asked.inherited || [],
            ignored: asked.ignored || [],
            model: asked.model || S.model || { type: "", label: "", family: "" },
            tasks_added: 0
        };
        if (!asked.ok) {
            return Object.assign(base, { ok: false, status: "refused", code: asked.code || QUEUE_REQUEST_REFUSED,
                                         message: sentence(asked.code || QUEUE_REQUEST_REFUSED), detail: asked.detail || "" });
        }
        let last = null;
        for (const delay of QUEUE_CONFIRM_DELAYS_MS) {
            if (delay) { await pause(delay); }
            const status = await confirmQueue(asked.request_id);
            last = status;
            if (!status.ok) {
                // The confirmation itself was refused or timed out. A refusal
                // naming the request (no record, wrong session) will not
                // change on the next try; a timeout may.
                if (status.code === REQUEST_INVALID || status.code === BRIDGE_SESSION_MISMATCH || status.code === WANGP_RESTARTED) { break; }
                continue;
            }
            if (status.status === "queued") {
                return Object.assign(base, { ok: true, status: "queued", tasks_added: status.tasks_added || 1, code: "", message: "", detail: "" });
            }
            if (status.status === "refused") {
                const why = status.code || WANGP_VALIDATION_REFUSED;
                return Object.assign(base, { ok: false, status: "refused", code: why, message: sentence(why), detail: status.detail || "" });
            }
            if (status.status === "expired") { break; }
        }
        say("queue " + String(asked.request_id).slice(0, 8) + ": admission unconfirmed after " + QUEUE_CONFIRM_DELAYS_MS.length + " confirmation(s)"
            + (last && last.code ? " (" + last.code + ")" : ""));
        return Object.assign(base, { ok: false, status: "unconfirmed", code: ADMISSION_UNCONFIRMED, message: sentence(ADMISSION_UNCONFIRMED),
                                     detail: (last && last.detail) || "" });
    }

    /**
     * What the live page can take right now, as the public API describes it:
     * one bounded receiver query, turned into per-input support flags.
     * Advisory - a queue request is judged again, live, when it runs.
     */
    function capabilities() {
        return receivers().then(function (answer) {
            if (!answer || !answer.ok) {
                const why = (answer && answer.code) || IFRAME_NOT_READY;
                return { ok: false, code: why, message: sentence(why) };
            }
            const byId = {};
            for (const receiver of answer.receivers || []) { byId[receiver.id] = receiver; }
            const supported = function (id) { return !!(byId[id] && byId[id].enabled); };
            return {
                ok: true,
                api_version: 1,
                ready: true,
                queue: !!S.queue,
                model: S.model ? Object.assign({}, S.model) : { type: "", label: "", family: "" },
                inputs: {
                    start: { supported: supported("start_frame") },
                    end: { supported: supported("end_frame") },
                    references: { supported: supported("reference"), max_count: byId.reference ? byId.reference.max_count : null }
                }
            };
        });
    }

    /* ------------------------------------------------------------------ */
    /* The public side                                                       */
    /* ------------------------------------------------------------------ */

    /**
     * Bind to the tab's iframe and start the handshake. Called by the WanGP
     * tab when it shows the iframe, and again by this file itself the first
     * time the Send menu asks anything, so a page whose tab forgot to call it
     * still ends up with a session rather than a permanent "not ready".
     */
    function attach(options) {
        const frame = frameElement();
        if (!frame) {
            say("attach: no iframe found under #" + IFRAME_ROOT_ID + " yet");
            return false;
        }
        say("attach: bound to the iframe, src=" + (frame.getAttribute("src") || "(none)"));
        if (!S.listening) {
            window.addEventListener("message", onMessage);
            S.listening = true;
        }
        if (S.frame === frame) {
            if (options && options.reload) { beginHandshake(true); }
            return true;
        }
        S.frame = frame;
        if (!frame.dataset.minipaintWangp) {
            frame.dataset.minipaintWangp = "1";
            // The element's own load event, not the document's: a reload of
            // this iframe is exactly the moment the old channel dies.
            frame.addEventListener("load", function () { beginHandshake(true); });
        }
        if (options && typeof options.channelId === "string" && HEX32.test(options.channelId)) {
            frame.setAttribute(CHANNEL_ATTRIBUTE, options.channelId);
        }
        if (options && typeof options.instanceId === "string") {
            frame.setAttribute(INSTANCE_ATTRIBUTE, text(options.instanceId, 128));
        }
        beginHandshake(true);
        return true;
    }

    function ensure() {
        if (S.frame && S.frame.isConnected) { return true; }
        return attach(null);
    }

    /**
     * One bounded question: what can the live WanGP page take right now.
     * Resolves either with the normalised list and the state revision it
     * belongs to, or with a failure code the menu can turn into a line. It
     * does not poll, it does not retry, and two overlapping calls share the
     * one question rather than asking twice.
     */
    function receivers(options) {
        if (!ensure()) { return Promise.resolve(failure(IFRAME_NOT_READY, "no WanGP iframe in this page")); }
        if (!S.ready || !S.bridgeSession) {
            rearm();
            return Promise.resolve(failure(IFRAME_NOT_READY, "no bridge session in this page"));
        }
        if (S.query) { return S.query; }
        const late = options && typeof options.late === "function" ? options.late : null;
        const query = ask(GET_RECEIVERS, { bridge_session: S.bridgeSession }, RECEIVER_QUERY_TIMEOUT_MS, RECEIVERS, late)
            .then(function (answer) {
                if (S.query === query) { S.query = null; }
                return answer;
            });
        S.query = query;
        return query;
    }

    /** What this side knows, for the menu and for a bug report. A copy: the
     * caller must not be able to edit the session's own idea of itself. */
    function state() {
        return {
            present: !!(S.frame || frameElement()),
            attached: !!S.frame,
            ready: S.ready,
            bridge_session: S.bridgeSession,
            bridge_version: S.bridgeVersion,
            state_revision: S.revision,
            model: S.model ? Object.assign({}, S.model) : null,
            receivers: S.receivers.map(function (receiver) { return Object.assign({}, receiver); }),
            pending: S.pending.size,
            queue: S.queue,
            code: S.lastCode
        };
    }

    function known(receiverId) {
        for (const receiver of S.receivers) {
            if (receiver.id === receiverId) { return receiver; }
        }
        return null;
    }

    /**
     * Hand one prepared image to one receiver. The image itself never travels:
     * what crosses is the opaque id of a PNG this extension wrote, and the
     * bridge reads that file itself. The revision is the one the menu was
     * built from, not the one this side happens to hold now - that is the
     * whole point of section 17: if the WanGP page moved since the user read
     * the menu, the bridge refuses rather than redirects.
     */
    /** A send that stops here, before anything is asked: said once, in the
     * journal, because a send that ends without a line anywhere is the one
     * kind of failure nobody can diagnose afterwards. */
    function refuse(failureCode, detail) {
        say("send: refused before asking - " + failureCode + (detail ? " (" + text(detail, 120) + ")" : ""));
        return Promise.resolve(failure(failureCode, detail));
    }

    function send(receiverId, revision, handoffId, expected) {
        if (!ensure() || !S.ready || !S.bridgeSession) {
            return refuse(IFRAME_NOT_READY, "no bridge session in this page");
        }
        if (RECEIVER_IDS.indexOf(receiverId) === -1) { return refuse(UNKNOWN_RECEIVER, receiverId); }
        if (!HEX32.test(String(handoffId || ""))) { return refuse(HANDOFF_INVALID_ID, "the prepared file's id is not one this extension mints"); }
        if (!REVISION_RE.test(String(revision || ""))) { return refuse(STALE_RECEIVER_STATE, "no revision was captured when the menu was built"); }
        const session = expected && expected.bridge_session ? String(expected.bridge_session) : S.bridgeSession;
        if (session !== S.bridgeSession) { return refuse(BRIDGE_SESSION_MISMATCH, "the menu was built for another bridge session"); }
        const receiver = known(receiverId);
        if (receiver && receiver.full) { return refuse(RECEIVER_LIMIT_REACHED, receiverId); }
        if (receiver && !receiver.enabled) { return refuse(RECEIVER_DISABLED, receiverId); }

        const requestId = hex32();
        const payload = {
            handoff_id: handoffId,
            receiver_id: receiverId,
            bridge_session: session,
            state_revision: revision
        };
        return new Promise(function (resolve) {
            const entry = {
                type: RECEIVE_RESULT,
                receiver: receiverId,
                channel: S.channelId,
                session: session,
                resolve: resolve,
                timer: setTimeout(function () {
                    S.pending.delete(requestId);
                    // Nothing came back, so nothing is proved: the send is a
                    // failure even though the image may in fact have landed.
                    say(RECEIVE_IMAGE + ": no acknowledgement within " + RECEIVE_TIMEOUT_MS + " ms (" + requestId.slice(0, 8) + ")");
                    resolve(failure(RECEIVER_QUERY_TIMEOUT, "no acknowledgement"));
                }, RECEIVE_TIMEOUT_MS)
            };
            S.pending.set(requestId, entry);
            if (!post(RECEIVE_IMAGE, requestId, payload)) {
                clearTimeout(entry.timer);
                S.pending.delete(requestId);
                say(RECEIVE_IMAGE + ": the iframe would not take the message");
                resolve(failure(IFRAME_NOT_READY, "the iframe would not take the message"));
                return;
            }
            say(RECEIVE_IMAGE + ": asked (" + requestId.slice(0, 8) + ") for " + receiverId);
        });
    }

    /**
     * Ask the bridge to bring a receiver into view. Only for a receiver the
     * bridge itself offered a focus hint for - this side never reaches into
     * the iframe's document, and a hint it did not give is not one to act on.
     * Cosmetic: a send is complete whether or not this arrives.
     */
    function focus(receiverId) {
        if (!S.ready || !S.frame) { return false; }
        const receiver = known(receiverId);
        if (!receiver || !receiver.focus_hint) { return false; }
        return post(FOCUS_RECEIVER, hex32(), {
            receiver_id: receiver.id,
            focus_hint: receiver.focus_hint,
            bridge_session: S.bridgeSession
        });
    }

    /** What the parent page looks like, as the host says it. */
    function detectTheme() {
        try {
            const body = document.body;
            if (body && body.classList.contains("dark")) { return "dark"; }
            if (app().querySelector && app().querySelector(".dark")) { return "dark"; }
            if (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches) { return "light"; }
        } catch (e) { /* fall through */ }
        return "dark";
    }

    /** Presentation, and nothing else. Section 27.1: a theme that does not
     * arrive must never be a reason an image cannot be handed over. */
    function theme(mode, accent) {
        const wanted = mode === "light" ? "light" : "dark";
        S.theme = wanted;
        if (!S.ready) { return false; }
        return post(THEME_STATE, hex32(), { mode: wanted, accent: text(accent, 40) });
    }

    /** The WanGP top-level tab, through the Canvas's one tab switcher. There
     * is deliberately no second one in this file. */
    function switchToWanGP() {
        const canvas = window.minipaintCanvas;
        if (!canvas || typeof canvas.switchTo !== "function") { return false; }
        canvas.switchTo("wangp");
        return true;
    }

    /**
     * Bind to the tab's iframe as soon as there is one, and not before.
     *
     * The container is built with the page and the iframe appears inside it
     * only once WanGP is serving, which is a moment nothing in this document
     * announces. So: a handful of lookups for the container - it either exists
     * by then or this build has no WanGP tab - and then one observer on that
     * single element. Not the document, not a subtree of the app, and nothing
     * that keeps running when the answer is "no tab here".
     */
    function watchRoot(step_) {
        if (S.watcher) { return; }
        const scope = app();
        const root = scope.querySelector ? scope.querySelector("#" + IFRAME_ROOT_ID) : null;
        if (!root) {
            const next = ROOT_LOOKUPS[step_ + 1];
            if (next !== undefined) { setTimeout(function () { watchRoot(step_ + 1); }, next); }
            else { say("watchRoot: gave up looking for #" + IFRAME_ROOT_ID + "; the WanGP tab never drew it"); }
            return;
        }
        say("watchRoot: found #" + IFRAME_ROOT_ID + " after " + step_ + " attempt(s)");
        attach(null);
        if (typeof MutationObserver !== "function") { return; }
        // Kept rather than disconnected after the first iframe: the tab
        // re-renders that element whenever it mints a new channel, and each
        // new element is a new session to shake hands with.
        S.watcher = new MutationObserver(function () {
            const frame = frameElement();
            if (frame && frame !== S.frame) { attach(null); }
        });
        S.watcher.observe(root, { childList: true, subtree: true });
    }

    /**
     * The sign-in probe needs nothing but this page. It must not wait for the
     * iframe, because the iframe is precisely what cannot load until the probe
     * has answered - the proxy refuses every request while the boundary is
     * unproven, and the frame's own request is one of those. Hanging it off
     * the handshake made the deadlock it was written to break.
     */
    function startAuthProbe() {
        probeAuth().then(function () { report(S.ready, ""); });
    }

    try {
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", function () { startAuthProbe(); watchRoot(0); }, { once: true });
        } else {
            startAuthProbe();
            watchRoot(0);
        }
    } catch (e) {
        // A page this file cannot bind to is a Send menu without WanGP lines,
        // and nothing worse than that.
    }

    return {
        attach: attach,
        probeAuth: probeAuth,
        receivers: receivers,
        send: send,
        focus: focus,
        state: state,
        switchToWanGP: switchToWanGP,
        theme: theme,
        message: sentence,
        // Protocol 3, the queue. window.minipaintInterop is the public face
        // of these; they are the mechanics, and their shapes may change.
        queue: queue,
        confirmQueue: confirmQueue,
        queueAndConfirm: queueAndConfirm,
        capabilities: capabilities,
        // One line into the same journal the handshake and the queries write
        // to, for the Canvas's half of a send. Text only; nothing is parsed.
        note: function (message) { say(text(message, 300)); }
    };
})();
