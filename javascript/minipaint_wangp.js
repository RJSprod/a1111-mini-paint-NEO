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

    const PROTOCOL = 2;

    const HELLO = "WANGP_BRIDGE_HELLO";
    const READY = "WANGP_BRIDGE_READY";
    const GET_RECEIVERS = "WANGP_GET_RECEIVERS";
    const RECEIVERS = "WANGP_RECEIVERS";
    const RECEIVE_IMAGE = "WANGP_RECEIVE_IMAGE";
    const RECEIVE_RESULT = "WANGP_RECEIVE_RESULT";
    const FOCUS_RECEIVER = "WANGP_FOCUS_RECEIVER";
    const THEME_STATE = "WANGP_THEME_STATE";
    const RUNTIME_STATE = "WANGP_RUNTIME_STATE";

    const TO_BRIDGE = [HELLO, GET_RECEIVERS, RECEIVE_IMAGE, FOCUS_RECEIVER, THEME_STATE];
    const TO_PARENT = [READY, RECEIVERS, RECEIVE_RESULT, RUNTIME_STATE];

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
    const RECEIVER_QUERY_TIMEOUT_MS = 4000;
    const RECEIVE_TIMEOUT_MS = 30000;

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

    // The few sentences this side needs before the server can supply one. The
    // wording is errors.py's, kept short enough for a menu line.
    const MESSAGES = {
        IFRAME_NOT_READY: "Open the WanGP tab and choose an input.",
        BRIDGE_SESSION_MISMATCH: "That WanGP page is no longer the one this send was prepared for.",
        RECEIVER_QUERY_TIMEOUT: "WanGP did not answer in time.",
        NO_ACTIVE_RECEIVER: "This WanGP model and mode take no image right now.",
        UNKNOWN_RECEIVER: "WanGP does not have that input.",
        STALE_RECEIVER_STATE: "The WanGP input changed; reopen Send to.",
        RECEIVER_DISABLED: "That WanGP input is not active right now.",
        RECEIVER_LIMIT_REACHED: "That WanGP input is full.",
        RECEIVER_VERIFY_FAILED: "WanGP took an image, but it could not be confirmed as the one that was sent.",
        HANDOFF_INVALID_ID: "That is not a handoff this extension made.",
        WANGP_RESTARTED: "WanGP restarted while the image was on its way.",
        INTERNAL_ERROR: "The WanGP integration hit an unexpected problem."
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
        theme: ""
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
        return {
            id: id,
            role: raw.role,
            label: label,
            menu_label: text(raw.menu_label, 120) || DEFAULT_MENU_LABELS[id] || ("Send Image to WanGP " + label),
            operation: raw.operation,
            enabled: raw.enabled !== false && raw.visible !== false && !full,
            count: count,
            max_count: maxCount,
            full: full,
            focus_hint: text(raw.focus_hint, 120),
            reason_code: code(raw.reason_code)
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

    /** A request that expects one answer, with one deadline and no retry. */
    function ask(kind, payload, timeout, expects) {
        const requestId = hex32();
        return new Promise(function (resolve) {
            const entry = {
                type: expects,
                channel: S.channelId,
                session: S.bridgeSession,
                resolve: resolve,
                timer: setTimeout(function () {
                    S.pending.delete(requestId);
                    resolve(failure(RECEIVER_QUERY_TIMEOUT, kind));
                }, timeout)
            };
            S.pending.set(requestId, entry);
            if (!post(kind, requestId, payload)) {
                clearTimeout(entry.timer);
                S.pending.delete(requestId);
                resolve(failure(IFRAME_NOT_READY, kind));
            }
        });
    }

    function settle(requestId, result) {
        const entry = S.pending.get(requestId);
        if (!entry) { return false; }
        clearTimeout(entry.timer);
        S.pending.delete(requestId);
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
    function say(message) {
        try {
            logSeq += 1;
            if (typeof console !== "undefined" && console.debug) {
                console.debug("MiniPaint WanGP:", message);
            }
            writeBox(CLIENT_LOG_ELEM_ID, JSON.stringify({ n: logSeq, line: String(message).slice(0, 300) }));
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
        if (message.type === RECEIVE_RESULT) { onResult(message.requestId, message.payload); }
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
        if (!OPAQUE_RE.test(session)) { return; }
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
    function receivers() {
        if (!ensure()) { return Promise.resolve(failure(IFRAME_NOT_READY, "no WanGP iframe in this page")); }
        if (!S.ready || !S.bridgeSession) {
            rearm();
            return Promise.resolve(failure(IFRAME_NOT_READY, "no bridge session in this page"));
        }
        if (S.query) { return S.query; }
        const query = ask(GET_RECEIVERS, { bridge_session: S.bridgeSession }, RECEIVER_QUERY_TIMEOUT_MS, RECEIVERS)
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
    function send(receiverId, revision, handoffId, expected) {
        if (!ensure() || !S.ready || !S.bridgeSession) {
            return Promise.resolve(failure(IFRAME_NOT_READY, "no bridge session in this page"));
        }
        if (RECEIVER_IDS.indexOf(receiverId) === -1) { return Promise.resolve(failure(UNKNOWN_RECEIVER, receiverId)); }
        if (!HEX32.test(String(handoffId || ""))) { return Promise.resolve(failure(HANDOFF_INVALID_ID)); }
        if (!REVISION_RE.test(String(revision || ""))) { return Promise.resolve(failure(STALE_RECEIVER_STATE, "no revision was captured")); }
        const session = expected && expected.bridge_session ? String(expected.bridge_session) : S.bridgeSession;
        if (session !== S.bridgeSession) { return Promise.resolve(failure(BRIDGE_SESSION_MISMATCH)); }
        const receiver = known(receiverId);
        if (receiver && receiver.full) { return Promise.resolve(failure(RECEIVER_LIMIT_REACHED, receiverId)); }
        if (receiver && !receiver.enabled) { return Promise.resolve(failure(RECEIVER_DISABLED, receiverId)); }

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
                    resolve(failure(RECEIVER_QUERY_TIMEOUT, "no acknowledgement"));
                }, RECEIVE_TIMEOUT_MS)
            };
            S.pending.set(requestId, entry);
            if (!post(RECEIVE_IMAGE, requestId, payload)) {
                clearTimeout(entry.timer);
                S.pending.delete(requestId);
                resolve(failure(IFRAME_NOT_READY, "the iframe would not take the message"));
            }
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
        message: sentence
    };
})();
