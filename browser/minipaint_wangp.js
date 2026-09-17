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

    const PROTOCOL = 5;

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
    // Protocol 5: where the tasks this page admitted are in WanGP's queue.
    const QUEUE_TRACK = "WANGP_QUEUE_TRACK";
    const QUEUE_TRACKED = "WANGP_QUEUE_TRACKED";
    // Protocol 6: commit the live form so a server-composed job runs at it.
    const FORM_FLUSH = "WANGP_FORM_FLUSH";
    const FORM_FLUSHED = "WANGP_FORM_FLUSHED";

    const TO_BRIDGE = [HELLO, GET_RECEIVERS, RECEIVE_IMAGE, FOCUS_RECEIVER, THEME_STATE, QUEUE_REQUEST, QUEUE_CONFIRM, QUEUE_TRACK, FORM_FLUSH];
    const TO_PARENT = [READY, RECEIVERS, RECEIVE_RESULT, RUNTIME_STATE, QUEUE_RESULT, QUEUE_STATUS, QUEUE_TRACKED, FORM_FLUSHED];

    // The flush outcomes, as protocol.py names them.
    const FLUSH_REQUESTED = "requested";
    const FLUSH_COMMITTED = "committed";
    const FLUSH_UNCHANGED = "unchanged";
    const FLUSH_UNAVAILABLE = "unavailable";
    const FLUSH_SUPPRESSED = "suppressed";
    // Long enough for a Gradio round trip on a loaded page, short enough that
    // a press never feels like it hung.
    //
    // It is deliberately not generous, because the case that spends the whole
    // budget is the ORDINARY one: a user who changed nothing since the last
    // commit produces an identical form, so the recorded fingerprint never
    // moves however long anyone waits. That answer is ``unchanged``, and
    // ``unchanged`` is a success - the record already matches the live form,
    // which is all the job needed. Waiting longer only taxes the common press
    // to shorten a race that is already bounded and already harmless.
    const FLUSH_BUDGET_MS = 900;
    const FLUSH_POLL_MS = 75;
    const FLUSH_CALL_TIMEOUT_MS = 8000;

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
    const QUEUE_TRACK_TIMEOUT_MS = 10000;
    const QUEUE_CONFIRM_DELAYS_MS = [0, 100, 300, 700, 1500, 3000];
    const PROMPT_MAX_CHARS = 12000;
    const MAX_QUEUE_REFERENCES = 16;
    const QUEUE_FIELDS = ["prompt", "start", "end", "references"];
    const ADMISSIONS = ["requested", "duplicate", "refused"];
    const QUEUE_STATUSES = ["pending", "queued", "started", "refused", "expired"];
    // Protocol 4: whether a request may start a generation. Omitted means
    // "auto"; the bridge decides inside WanGP from WanGP's own flag.
    const START_MODES = ["auto", "never"];
    const START_ANSWERS = ["auto", "never", "unknown"];
    const ROUTES = ["generate", "queue"];
    // Protocol 5: what a track answer says of one request's task.
    const TRACK_STATES = ["waiting", "generating", "finished", "unknown"];
    const MAX_TRACKED_REQUESTS = 32;
    const MODEL_TYPE_RE = /^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,119}$/;

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
    const MODEL_CHANGED = "MODEL_CHANGED";

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
        PROMPT_TOO_LONG: "The prompt is longer than WanGP queue requests allow (12000 characters).",
        QUEUE_BUSY: "WanGP is still taking the previous queue request; try again in a moment.",
        QUEUE_REQUEST_REFUSED: "WanGP did not take the queue request.",
        ADMISSION_UNCONFIRMED: "WanGP did not confirm that the request was added to the queue.",
        WANGP_VALIDATION_REFUSED: "WanGP declined the queue request; check the WanGP page for details.",
        MODEL_CHANGED: "The WanGP page moved to another model after the prompt was enhanced for it; retry to enhance it for the current model."
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
    // The tab's own opinion of which of its four views is showing, as JSON.
    // The tab writes it on every repaint, so it is how this side learns what
    // the server decided without asking over a second channel.
    const STATE_ELEM_ID = "wangp_state";
    // The tab's hidden Refresh, wired to ui.py show(): repaint from what is
    // true now, and start nothing. Never Open, which starts a process.
    const REFRESH_ELEM_ID = "wangp_refresh_request";
    const BROWSER_CHECK_ELEM_ID = "wangp_browser_check";
    const SESSION_ELEM_ID = "wangp_session";
    const CHANNEL_ATTRIBUTE = "data-minipaint-channel";
    const INSTANCE_ATTRIBUTE = "data-minipaint-instance";

    /* What the tab's state box says about whether an iframe belongs on the
     * page at all. Three answers and not two: the tab clears the iframe on
     * purpose whenever it paints setup, starting or error, so a missing
     * iframe is only a fault when the tab still says it wants one - and a
     * state this side cannot read is not permission to guess either way. */
    const WAN_VIEW_IFRAME = "iframe";
    const WAN_VIEW_NON_IFRAME = "non_iframe";
    const WAN_VIEW_UNKNOWN = "unknown";
    //: The views ui.py can paint. Anything else is a build newer than this
    //: file, and is read as UNKNOWN rather than forced into one of these.
    const NON_IFRAME_VIEWS = ["setup", "starting", "error"];

    // The element the browser writes its one recovery notice into, and the
    // class the stylesheet dresses it with. One id, so clearing it is exact.
    const RECOVERY_NOTICE_ID = "minipaint-wangp-recovery-notice";
    const RECOVERY_NOTICE_CLASS = "minipaint-wangp-recovery";
    //: The only text that notice ever carries. Never a server string, never a
    //: message payload, never an error detail - see section 23.
    const RECOVERY_NOTICE_TEXT =
        "The WanGP controls in this page could not be reconnected automatically. "
        + "Any job already accepted by the server may still be running. Avoid restarting "
        + "WanGP or Forge while it is working. If the controls remain unavailable, "
        + "refresh this browser page only as a last resort.";

    /* Which replies belong to a request that may have CHANGED something on
     * the WanGP side by the time this page stopped waiting for the answer.
     *
     * The distinction is not which error code comes back, it is who answered.
     * WanGP declining is an answer and a refusal; this page giving up is not
     * an answer at all, and reporting it as a refusal invites the user to
     * press again on work that may already be queued. So a give-up settles
     * these two with an explicit unconfirmed marker instead. See section 14.
     *
     * FORM_FLUSHED is deliberately not here: it writes the live form, but it
     * admits no task, starts no generation, and nothing downstream turns its
     * result into a terminal outbox state. If that ever changes it joins this
     * list. */
    const MUTATING_REPLIES = [QUEUE_RESULT, RECEIVE_RESULT];

    /* What an abandoned one of those says to a person. The code is
     * ADMISSION_UNCONFIRMED either way - it is the word the server already
     * knows and the one the outbox spends on its safe branch - but the
     * sentence is the operation's own, because "added to the queue" describes
     * nothing that an image send was doing. */
    const UNCONFIRMED_SENTENCES = {};
    UNCONFIRMED_SENTENCES[QUEUE_RESULT] = MESSAGES.ADMISSION_UNCONFIRMED;
    UNCONFIRMED_SENTENCES[RECEIVE_RESULT] =
        "WanGP did not confirm the image, and it may still have arrived. Check the WanGP page before sending it again.";

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
        //: The node S.watcher is bound to. Kept so that a root Forge REPLACED
        //: rather than emptied can be noticed: an observer on a detached node
        //: never fires again and never says so. See rebindRoot().
        watcherRoot: null,
        //: One repair cycle's worth of bookkeeping, and nothing that outlives
        //: the page. No storage, no coordinator, no state machine: while the
        //: tab is healthy every one of these stays at its resting value and
        //: nothing reads them.
        recovery: {
            active: false,      //: single-flight: one cycle, never two
            reason: "",         //: why this one started, for the log line
            startedAt: 0,       //: for duration_ms
            pressed: false,     //: whether this cycle pressed Refresh
            warningShown: false,//: one notice per unresolved episode
            graceTimer: 0,      //: the one-shot absence grace
            deadline: null      //: the visibility-aware arrival budget
        },
        //: The transport circuit breaker's bookkeeping. See onStreamState.
        //: At rest on a healthy page: no deadline, nothing shed.
        transport: {
            silentSince: 0,     //: when the event spine went quiet; 0 while it speaks
            deadline: null,     //: the on-screen budget a plain request has to come back in
            shed: false,        //: whether the iframe is unloaded to give its connections back
            shedAt: 0,
            sheds: 0,           //: how many times this page has had to, for a bug report
            src: "",            //: what the iframe was loading before it was shed
            reload: null        //: the on-screen budget before it is loaded again regardless
        },
        //: Whether the one boot-time press of the tab's Refresh has happened.
        //: See watchRoot.
        bootRepainted: false,
        //: The runtime-frame press: when the last one happened, the timer for
        //: the next, and what the server said last. See onRuntimeFrame.
        runtimePress: { at: 0, timer: 0, running: null },
        lastCode: "",
        theme: "",
        // Whether the bridge in this page can take a queue request at all: the
        // handshake says, and a build that lacks a queue-critical component
        // says no while still taking an image send.
        queue: false,
        // Protocol 4: whether it can start a generation (its generate trigger
        // resolved), and whether WanGP was generating at the last answer -
        // true, false, or null for a build that cannot say.
        start: false,
        generationRunning: null,
        // Protocol 5: whether it can say where admitted tasks are in the queue.
        track: false
    };

    //: The proactive flush's own state, declared here rather than beside the
    //: functions that use it because those run from events - a ready
    //: acknowledgement, a visibility change - and nothing guarantees this
    //: file has finished loading when one arrives. See flushProactively.
    const PROACTIVE_GAP_MS = 1500;

    const P = {
        //: null until the server says. Never assumed: committing a form that
        //: nothing reads is work done on the user's card for nobody.
        wanted: null,
        running: false,
        lastAt: 0,
        onScreen: null,
        observer: null
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
        return { label: text(raw.label, 120), type: text(raw.type, 120), family: text(raw.family, 60), architecture: text(raw.architecture, 120) };
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
        const root = rootElement();
        return root ? root.querySelector("iframe") : null;
    }

    /**
     * What the bridge's frame timer is doing inside the iframe, as one line.
     *
     * Why this is worth reporting at all: Gradio schedules every event trigger
     * inside requestAnimationFrame and gates it on a component-update flush
     * that is itself scheduled the same way. A document the browser is not
     * rendering - this iframe, whenever the WanGP tab is not the one on screen
     * - is given no frames, so the flush never runs and every bridge request
     * queues up behind it until somebody opens the tab. The bridge puts a
     * timer behind each frame to keep that moving, but it only covers Gradio's
     * own flush when it was installed in the page head, before Gradio captured
     * requestAnimationFrame. When it lands late instead, the queue stalls
     * exactly as described and nothing anywhere says why.
     *
     * The iframe is same-origin - it is served through this Forge's own
     * /wan2gp/ - so the handle the timer leaves on its window can simply be
     * read. A build where that is not true says so and costs nothing.
     */
    function frameTimerNote() {
        try {
            const frame = frameElement();
            const view = frame && frame.contentWindow;
            const handle = view && view.__minipaintFrames;
            if (!handle) {
                return "frames: no timer handle on the WanGP page - a hidden tab's requests may wait for the tab to be opened";
            }
            const where = handle.installed === "head"
                ? "in the page head (Gradio's own flush is covered)"
                : "installed late - Gradio's own flush is NOT covered, so a hidden tab's requests wait for the tab to be opened";
            return "frames: timer " + where
                + "; " + (handle.timedFrames || 0) + " run by timer, " + (handle.nativeFrames || 0) + " by the browser";
        } catch (e) {
            return "frames: the WanGP page's frame state could not be read (" + String(e && e.name || e).slice(0, 40) + ")";
        }
    }

    /** A hidden textbox of the WanGP tab, by id. Values, never URLs. */
    function box(elementId) {
        const scope = app();
        return scope.querySelector ? scope.querySelector("#" + elementId + " textarea") : null;
    }

    /**
     * Which of its four views the tab says it is showing, as one of three
     * answers.
     *
     * Three and not two, because a missing iframe is not by itself a fault.
     * The tab clears the iframe on purpose every time it paints setup,
     * starting or error - that is what those views ARE - so "the iframe is
     * gone" only means something once this side knows whether the tab still
     * wants one. And when the answer cannot be read at all, that is its own
     * third case rather than a guess: pressing Refresh on a page whose state
     * is unreadable reloads whatever WanGP was doing inside a frame that may
     * have been perfectly healthy, and calling it NON_IFRAME instead would
     * claim the server made a decision nobody saw it make.
     *
     * The VIEW field, never the STATE field. They sit side by side in the
     * same JSON and only one of them answers this question: a WanGP that a
     * bridge complaint has degraded is state DEGRADED and view iframe,
     * because the frame is genuinely usable and only the intelligent Send is
     * switched off. Reading STATE would file that tab as unrecognised, refuse
     * to repair its iframe, and warn a user whose tab was working.
     *
     * Pure, cheap, and safe to call as often as the cycle needs to.
     */
    function readWanGpView() {
        const raw = box(STATE_ELEM_ID);
        // Not an empty value: the box is born holding the real view's JSON
        // (ui.py builds it that way), so in practice this is the element
        // being absent from the DOM altogether - which is also the shape that
        // says the whole WanGP subtree went, not just the frame.
        if (!raw || !raw.value) { return WAN_VIEW_UNKNOWN; }
        let view = "";
        try {
            const state = JSON.parse(raw.value);
            view = state && typeof state.view === "string" ? state.view : "";
        } catch (e) {
            return WAN_VIEW_UNKNOWN;
        }
        if (view === WAN_VIEW_IFRAME) { return WAN_VIEW_IFRAME; }
        if (NON_IFRAME_VIEWS.indexOf(view) !== -1) { return WAN_VIEW_NON_IFRAME; }
        // A fifth view from a build newer than this file. Not a case today,
        // and deliberately read as "cannot prove it is safe" rather than as
        // either of the two answers that authorise an action.
        return WAN_VIEW_UNKNOWN;
    }

    /** The tab's iframe container, if the tab is still in the page at all. */
    function rootElement() {
        const scope = app();
        return scope.querySelector ? scope.querySelector("#" + IFRAME_ROOT_ID) : null;
    }

    /** Write one of those the way a user would, so Gradio sees the change. */
    function writeBox(elementId, value) {
        const target = box(elementId);
        if (!target) { return false; }
        // Through the host's own accessor, not a plain assignment: see
        // window.minipaintWriteInput in javascript/main.js. A framework keeps
        // its own record of what an input holds, and an assignment leaves it
        // untouched - the write lands and the event never happens.
        if (typeof window.minipaintWriteInput === "function") {
            return window.minipaintWriteInput(target, value);
        }
        target.value = value;
        target.dispatchEvent(new Event("input", { bubbles: true }));
        target.dispatchEvent(new Event("change", { bubbles: true }));
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

    /* ------------------------------------------------------------------ */
    /* Deadlines that only spend the time this page was on screen            */
    /* ------------------------------------------------------------------ */

    /**
     * A backgrounded tab is throttled, not stopped. It keeps running - it
     * posts these very lines - but its turn of the event loop comes around
     * seconds apart instead of milliseconds, and a reply that arrives in the
     * message queue sits there until it does.
     *
     * That is fatal to a deadline measured on the wall clock. The bridge
     * answers a receivers query in 0 ms and the page takes 23 seconds to
     * notice; ten-second deadlines expire one after another while nothing is
     * actually wrong, the admission goes unconfirmed, the prepared image is
     * swept, and a queue that was working reports failures for work the user
     * never saw fail. Every one of those is an artefact of measuring a
     * throttled page against a clock that was not.
     *
     * So a deadline spends only the time the page was on screen. It is held
     * when the page goes to the background and resumes with what it had left
     * when the page comes back, plus a moment for the replies that queued up
     * behind it to be delivered first. A request whose page is in somebody's
     * pocket is not late; it is waiting, and it finishes when they look.
     *
     * This is the page's own timeout. Whatever WanGP does with the request is
     * WanGP's business and is bounded on its side.
     */
    const DEADLINES = new Set();
    //: What a resumed page is given to drain the replies that queued up while
    //: it was away, before any deadline starts counting again.
    const RESUME_GRACE_MS = 1500;

    function onScreen() {
        try { return document.visibilityState !== "hidden"; } catch (e) { return true; }
    }

    function startDeadline(entry) {
        entry.startedAt = Date.now();
        entry.timer = setTimeout(function () {
            entry.timer = 0;
            DEADLINES.delete(entry);
            entry.fire();
        }, entry.remaining);
    }

    /** A timeout that pauses with the page. Returns a handle with cancel(). */
    function deadline(ms, fire) {
        const entry = { remaining: Math.max(0, Number(ms) || 0), fire: fire, timer: 0, startedAt: 0 };
        DEADLINES.add(entry);
        if (onScreen()) { startDeadline(entry); }
        return {
            cancel: function () {
                if (entry.timer) { clearTimeout(entry.timer); entry.timer = 0; }
                DEADLINES.delete(entry);
            }
        };
    }

    function holdDeadlines() {
        DEADLINES.forEach(function (entry) {
            if (!entry.timer) { return; }
            clearTimeout(entry.timer);
            entry.timer = 0;
            entry.remaining = Math.max(0, entry.remaining - (Date.now() - entry.startedAt));
        });
    }

    function resumeDeadlines() {
        DEADLINES.forEach(function (entry) {
            if (entry.timer) { return; }
            entry.remaining += RESUME_GRACE_MS;
            startDeadline(entry);
        });
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
                timer: 0,
                deadline: null
            };
            entry.deadline = deadline(timeout, function () {
                say(kind + ": no answer within " + timeout + " ms on screen (" + requestId.slice(0, 8) + ")");
                if (entry.late) {
                    // Kept, marked, and dropped later: an answer in the
                    // grace window is still this request's answer.
                    entry.expired = true;
                    entry.timer = setTimeout(function () { S.pending.delete(requestId); }, LATE_ANSWER_GRACE_MS);
                } else {
                    S.pending.delete(requestId);
                }
                // The same rule abandon() follows, for the same reason and in
                // the other half of the same window: the envelope crossed into
                // the iframe - post() said so - and then nothing came back.
                // That is this page giving up, not WanGP declining, and for a
                // request that may already have changed something over there
                // the difference is a job left for a person to judge instead
                // of a job offered straight back for a retry.
                if (MUTATING_REPLIES.indexOf(expects) !== -1) {
                    say("recovery: mutating request acknowledgement lost; outcome remains unconfirmed ("
                        + requestId.slice(0, 8) + ", " + expects + ")");
                    resolve(unconfirmed(kind, UNCONFIRMED_SENTENCES[expects]));
                    return;
                }
                resolve(failure(RECEIVER_QUERY_TIMEOUT, kind));
            });
            S.pending.set(requestId, entry);
            if (!post(kind, requestId, payload)) {
                entry.deadline.cancel();
                if (entry.timer) { clearTimeout(entry.timer); }
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
        if (entry.deadline) { entry.deadline.cancel(); }
        if (entry.timer) { clearTimeout(entry.timer); entry.timer = 0; }
        S.pending.delete(requestId);
        const waited = Date.now() - (entry.askedAt || Date.now());
        if (result && result.ok) {
            say(entry.type + ": answered after " + waited + " ms" + (entry.expired ? " (late, taken)" : ""));
        } else {
            // "refused" is a claim about what WanGP did, so it is only said
            // when WanGP is the one who answered. A settlement this side
            // decided says so instead - see unconfirmed().
            say(entry.type + ": " + (result && result.unconfirmed ? "unconfirmed" : "refused")
                + " after " + waited + " ms - " + ((result && result.code) || "?")
                + ((result && result.detail) ? " (" + result.detail + ")" : "") + (entry.expired ? " (late)" : ""));
        }
        if (entry.expired) {
            if (entry.late) { try { entry.late(result); } catch (e) { /* the menu's business */ } }
            return true;
        }
        entry.resolve(result);
        return true;
    }

    /**
     * What a request settles with when this page stopped waiting rather than
     * the bridge answering. Deliberately not failure(): a caller that reads
     * only ``ok`` still sees a non-success, and a caller that has to decide
     * what to tell a person - or what state to write down - can tell the two
     * apart by the marker.
     */
    function unconfirmed(reason, line) {
        S.lastCode = ADMISSION_UNCONFIRMED;
        return {
            ok: false,
            unconfirmed: true,
            code: ADMISSION_UNCONFIRMED,
            message: line || sentence(ADMISSION_UNCONFIRMED),
            detail: text(reason, 200)
        };
    }

    /**
     * Every request still waiting is settled. Called when the page it was
     * prepared for stops being the page that would answer it.
     *
     * ``giveUp`` says this side stopped waiting rather than the bridge
     * answering, and it is the whole of the difference that matters here.
     * No acknowledgement is not the same fact as a refusal: a queue request
     * or an image send may already have crossed into WanGP and been acted on
     * by the time the frame holding the answer went away, and the outbox
     * spends the two on opposite branches - unconfirmed is shown and left for
     * a person to judge, failed is offered straight back for a retry. A retry
     * of a generation that is already running is the one outcome the whole
     * recovery path exists to avoid, so a give-up never produces one.
     *
     * Non-mutating requests - the queries and the probes - are unchanged:
     * nothing downstream turns their answer into a terminal record, and a
     * caller is free to ask again once the bridge is back.
     */
    function abandon(failureCode, giveUp) {
        const waiting = Array.from(S.pending.keys());
        for (const requestId of waiting) {
            const entry = S.pending.get(requestId);
            if (giveUp && entry && MUTATING_REPLIES.indexOf(entry.type) !== -1) {
                // Said here, where the outcome is decided, rather than by
                // whoever reads it later: this is the line that would have
                // made a lost admission visible the first time it happened.
                say("recovery: mutating request acknowledgement lost; outcome remains unconfirmed ("
                    + requestId.slice(0, 8) + ", " + entry.type + ")");
                settle(requestId, unconfirmed(failureCode, UNCONFIRMED_SENTENCES[entry.type]));
                continue;
            }
            settle(requestId, failure(failureCode));
        }
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
            // that just replaced it. A give-up, not an answer - the page that
            // could have said yes or no is the one that just went away.
            abandon(BRIDGE_SESSION_MISMATCH, true);
            S.ready = false;
            S.queue = false;
            S.start = false;
            S.track = false;
            S.generationRunning = null;
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
            // A frame that arrived and then would not speak is exactly as
            // unusable as one that never arrived, so a repair cycle waiting on
            // this handshake ends here rather than at the arrival budget.
            handshakeSettledUnusable("no reply to " + HELLO_DELAYS.length + " offers");
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

    // Where the lines go. A plain route, not a Gradio event: a diagnostic
    // line should not ride on the machinery it exists to describe, and the
    // batch below used to be a full round trip through it.
    const CLIENT_LOG_ROUTE = "/minipaint-wangp/client-log";
    // Long enough that a burst of lines is one request, short enough that a
    // person watching the log does not wait for them.
    const LOG_FLUSH_MS = 1500;
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

    /**
     * When a line was written, on a clock the server can use.
     *
     * Not ``Date.now()``: this page's wall clock is the user's, which may be
     * minutes off the server's and is free to jump while the tab is asleep -
     * and asleep is exactly the case this exists for. ``performance.now()``
     * is monotonic and cares about none of that, so what travels is not a
     * time at all but an age, and the server counts back from its own clock.
     */
    function logClock() {
        try {
            if (window.performance && typeof window.performance.now === "function") {
                return window.performance.now();
            }
        } catch (e) { /* fall through to the wall clock */ }
        return Date.now();
    }

    /**
     * Hand the batch to the server.
     *
     * The window and the sequence numbers are kept exactly as they were: the
     * server still journals each number once per page, so a batch that is
     * lost on the way is carried by the next one and a batch that arrives
     * twice is dropped there. That property is why this can be a fire-and-
     * forget POST with nothing watching the answer.
     *
     * ``keepalive`` so the last batch still leaves a page that is being
     * closed, which is the batch most likely to say why.
     */
    function flushLog() {
        logFlush = 0;
        if (!logLines.length) { return Promise.resolve(); }
        // The age of each line AT THIS MOMENT, which is the moment the server
        // will count back from. A backgrounded tab holds its lines for as
        // long as the browser holds the tab - one stretch in a real report
        // was 208 seconds - and stamping them on arrival put a whole
        // incident's worth of lines in the same second, including a request
        // being asked for and timing out ten seconds later.
        const sending = logClock();
        const batch = logLines.slice(-LOG_WINDOW).map(function (entry) {
            return { s: entry.s, line: entry.line, ms: Math.max(0, Math.round(sending - entry.at)) };
        });
        const body = JSON.stringify({ p: LOG_PAGE, n: logSeq, lines: batch });
        try {
            return fetch(CLIENT_LOG_ROUTE, {
                method: "POST",
                credentials: "same-origin",
                cache: "no-store",
                keepalive: true,
                headers: { "Content-Type": "application/json" },
                body: body
            }).catch(function () { /* the next batch carries these lines */ });
        } catch (e) { /* a log line is never worth an exception */ }
        return Promise.resolve();
    }

    /**
     * Put the frame timer's state in the journal now, and wait until it is
     * there.
     *
     * The handshake reports this too, but a report is collected long after
     * the handshake - often on a page that has had no reason to shake hands
     * since Forge last restarted - and the answer would then be missing from
     * the one document somebody was going to send. The diagnostics button
     * runs this first and waits for it, so the report it builds afterwards
     * carries the line whether or not this page has a live bridge session.
     */
    async function reportFrames() {
        try {
            logSeq += 1;
            logLines.push({ s: logSeq, line: String(frameTimerNote()).slice(0, 300), at: logClock() });
            if (logLines.length > LOG_WINDOW) { logLines.splice(0, logLines.length - LOG_WINDOW); }
            if (logFlush) { clearTimeout(logFlush); logFlush = 0; }
            await flushLog();
        } catch (e) { /* the report will simply not carry the line */ }
    }

    function say(message) {
        try {
            logSeq += 1;
            if (typeof console !== "undefined" && console.debug) {
                console.debug("MiniPaint WanGP:", message);
            }
            logLines.push({ s: logSeq, line: String(message).slice(0, 300), at: logClock() });
            if (logLines.length > LOG_WINDOW) { logLines.splice(0, logLines.length - LOG_WINDOW); }
            if (!logFlush) { logFlush = setTimeout(flushLog, LOG_FLUSH_MS); }
        } catch (e) { /* a log line is never worth an exception */ }
    }

    // Whatever is still waiting when the page goes away. pagehide is the one
    // that fires on a mobile tab being put aside, which plain unload does not.
    try {
        window.addEventListener("pagehide", function () { if (logFlush) { clearTimeout(logFlush); } flushLog(); });
        document.addEventListener("visibilitychange", function () {
            if (document.visibilityState === "hidden") { if (logFlush) { clearTimeout(logFlush); } flushLog(); }
        });
    } catch (e) { /* an old browser keeps the timer and nothing else changes */ }

    /* ------------------------------------------------------------------ */
    /* What the page's own lifecycle did                                     */
    /* ------------------------------------------------------------------ */

    /**
     * Say when this page stopped running and when it started again.
     *
     * Everything this extension does while a request is in flight is done by
     * JavaScript in this page, so a page the browser has stopped running is a
     * queue that has stopped moving - and on a phone that is the ordinary
     * outcome of switching apps, not an edge case. From the outside it looks
     * exactly like a bug in the queue: nothing happens, then everything
     * happens at once when the tab comes back.
     *
     * The two are impossible to tell apart from a log of what the queue did,
     * and identical to a person watching. They are trivial to tell apart from
     * a log that also says when the page was running. So the page says it,
     * and says how long it was away, because the length is the part that
     * matters: a gap in the queue's activity either lines up with one of
     * these or it does not, and that single fact decides where to look next.
     *
     * Transitions only - four listeners and no timer. Nothing is polled to
     * produce this.
     */
    function watchLifecycle() {
        let awaySince = 0;

        // "hidden" is throttled, not stopped - the page keeps running and
        // keeps posting these lines, but its turn of the event loop comes
        // around seconds apart. "frozen" is stopped. Saying the first as
        // though it were the second was wrong and sent the last search in the
        // wrong direction, so the two are named for what they are.
        const gone = function (why, stopped) {
            if (awaySince) { return; }
            awaySince = Date.now();
            holdDeadlines();
            say("lifecycle: the page went to the background (" + why + ")"
                + (stopped ? " and is not running at all" : "; it is throttled, so replies queue up")
                + " - deadlines are held until it is back");
            if (logFlush) { clearTimeout(logFlush); logFlush = 0; }
            flushLog();
            // Last chance to carry an uncommitted form across; throttled or
            // frozen it may not land, which is why nothing waits for it.
            flushProactively("the page went to the background");
        };

        // Coming back is also the moment to find out whether anything went
        // away while nobody was looking. A page that is hidden for an hour can
        // return to a Forge that re-rendered the whole tab underneath it, and
        // until this ran there was nothing anywhere that would notice.
        // Healthy is free: one state read and two lookups, then nothing.
        const check = function (why) {
            try { verifyOnResume(why); } catch (e) { /* never worth breaking the lifecycle over */ }
        };

        const back = function (why) {
            if (!awaySince) { say("lifecycle: " + why); check(why); return; }
            const away = Math.round((Date.now() - awaySince) / 100) / 10;
            awaySince = 0;
            resumeDeadlines();
            say("lifecycle: back on screen after " + away + "s (" + why + ")"
                + "; deadlines resume with what they had left");
            check(why);
        };

        try {
            document.addEventListener("visibilitychange", function () {
                if (document.visibilityState === "hidden") { gone("hidden", false); } else { back("visible"); }
            });
            // The Page Lifecycle events, where the engine has them. A frozen
            // page runs nothing at all - not even a timer - which is the state
            // a throttled one is usually mistaken for.
            document.addEventListener("freeze", function () { gone("frozen", true); });
            document.addEventListener("resume", function () { back("resumed"); });
            // A page restored from the back-forward cache was not reloaded and
            // kept its state, so its queue picks up rather than starting over.
            window.addEventListener("pageshow", function (event) {
                if (event && event.persisted) { back("restored from the back-forward cache"); }
            });
            window.addEventListener("offline", function () { say("lifecycle: the browser reports it is offline"); });
            window.addEventListener("online", function () { say("lifecycle: the browser reports it is online again"); });
        } catch (e) { /* an engine without these tells us nothing, and costs nothing */ }
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
        if (message.type === QUEUE_STATUS) { onQueueStatus(message.requestId, message.payload); return; }
        if (message.type === QUEUE_TRACKED) { onQueueTracked(message.requestId, message.payload); return; }
        if (message.type === FORM_FLUSHED) { onFormFlushed(message.requestId, message.payload); }
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
            handshakeSettledUnusable(refused);
            return;
        }
        const instance = text(payload.instance_id, 128);
        const restarted = !!S.instanceId && !!instance && instance !== S.instanceId;
        if (S.bridgeSession && S.bridgeSession !== session) {
            // The bridge named a different session, which answers "who is
            // speaking now" and not "what became of that request".
            abandon(restarted ? WANGP_RESTARTED : BRIDGE_SESSION_MISMATCH, true);
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
        S.start = declared && !!(payload.capabilities && payload.capabilities.start === true);
        S.track = declared && !!(payload.capabilities && payload.capabilities.track === true);
        S.generationRunning = typeof payload.generation_running === "boolean" ? payload.generation_running : null;
        S.lastCode = declared ? "" : (failure || "BRIDGE_COMPONENT_INCOMPATIBLE");
        report(declared, S.lastCode);
        // Into the page's own log, which the diagnostics report carries, so
        // that "nothing happens until I open the WanGP tab" is a line somebody
        // can read rather than a thing they have to notice.
        say(frameTimerNote());
        recordSession(true);
        // The one that removes "generate once first": a page that has just
        // said it is ready has a live form and no recorded one, and this is
        // the first moment it can be asked to commit it.
        if (declared) { flushProactively("the WanGP page became ready"); }
        // Presentation only, and never a reason a picture cannot be sent.
        try { theme(S.theme || detectTheme()); } catch (e) { /* section 27.1 */ }
        // A bridge that introduced itself is the whole definition of the
        // controls being back. The cycle ends silently, and any notice a
        // previous episode left behind is taken away explicitly - a repaint
        // replaces the HTML inside the container, never the container, so
        // nothing else would have removed it.
        if (S.recovery.active) { finishIframeRepair(REPAIR_READY, ""); }
        else { clearRecoveryWarning(); }
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
            // An advisory about the process, not a verdict on the requests
            // that were in flight when it restarted.
            abandon(WANGP_RESTARTED, true);
            beginHandshake(true);
            return;
        }
        if (payload.state && payload.state !== "READY") {
            S.ready = false;
            abandon(WANGP_RESTARTED, true);
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
    /* The queue: minipaint.wangp.queue/v1 over protocol 5                  */
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
            model: normaliseModel(raw.model) || S.model || { type: "", label: "", family: "" },
            route: ROUTES.indexOf(raw.route) === -1 ? "" : raw.route,
            start: START_ANSWERS.indexOf(raw.start) === -1 ? "" : raw.start,
            generation_running: typeof raw.generation_running === "boolean" ? raw.generation_running : null
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
            detail: text(raw.detail, 200),
            queue_depth: ok && Number.isFinite(raw.queue_depth) && raw.queue_depth >= 0 ? Math.trunc(raw.queue_depth) : null,
            route: ROUTES.indexOf(raw.route) === -1 ? "" : raw.route
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

    /** One track answer (protocol 5): per request, where its task is. An
     * entry this side cannot read is "unknown", never "finished". */
    function normaliseQueueTrack(raw) {
        raw = raw && typeof raw === "object" ? raw : {};
        const ok = raw.ok === true && raw.tracked && typeof raw.tracked === "object" && !Array.isArray(raw.tracked);
        const tracked = {};
        if (ok) {
            for (const key in raw.tracked) {
                if (!Object.prototype.hasOwnProperty.call(raw.tracked, key) || !HEX32.test(key)) { continue; }
                const item = raw.tracked[key] && typeof raw.tracked[key] === "object" ? raw.tracked[key] : {};
                tracked[key] = {
                    state: TRACK_STATES.indexOf(item.state) === -1 ? "unknown" : item.state,
                    position: Number.isFinite(item.position) && item.position >= 0 ? Math.trunc(item.position) : null,
                    queue_depth: Number.isFinite(item.queue_depth) && item.queue_depth >= 0 ? Math.trunc(item.queue_depth) : null
                };
            }
        }
        return {
            ok: ok,
            tracked: tracked,
            code: ok ? "" : (code(raw.code) || REQUEST_INVALID),
            detail: text(raw.detail, 200),
            generation_running: typeof raw.generation_running === "boolean" ? raw.generation_running : null
        };
    }

    function onQueueTracked(requestId, payload) {
        const entry = S.pending.get(requestId);
        if (!entry || entry.type !== QUEUE_TRACKED) { return; }
        const answer = normaliseQueueTrack(payload);
        if (!answer.ok) {
            settle(requestId, Object.assign(failure(answer.code, answer.detail), { tracked: {} }));
            return;
        }
        settle(requestId, answer);
    }

    /**
     * The bridge's answer to a flush: what it did about the request, and the
     * fingerprint of the recorded form as it stood *before* it did it.
     *
     * Without this the answer arrived, passed every check in ``acceptable``
     * and was then dropped on the floor: the request stayed pending until its
     * call timeout, so every flush cost eight seconds and reported that the
     * bridge had not answered - when it had, immediately. A press waited for
     * it and inherited the previous settings anyway, which is exactly the
     * symptom the flush was written to remove.
     */
    function onFormFlushed(requestId, payload) {
        const entry = S.pending.get(requestId);
        if (!entry || entry.type !== FORM_FLUSHED) { return; }
        if (payload.ok === false) {
            settle(requestId, failure(code(payload.code) || INTERNAL_ERROR, text(payload.detail, 200)));
            return;
        }
        settle(requestId, {
            ok: true,
            flush: text(payload.flush, 40),
            fingerprint: text(payload.fingerprint, 128)
        });
    }

    /**
     * Where the tasks this page admitted are in WanGP's queue now (protocol
     * 5): one bounded question about up to MAX_TRACKED_REQUESTS request ids,
     * answered from WanGP's own record for this page and never written to.
     */
    function trackQueue(requestIds) {
        const ids = Array.isArray(requestIds) ? requestIds.map(String).filter(function (item) { return HEX32.test(item); }) : [];
        if (!ids.length || ids.length > MAX_TRACKED_REQUESTS) { return Promise.resolve(Object.assign(failure(REQUEST_INVALID, "one to " + MAX_TRACKED_REQUESTS + " request ids"), { tracked: {} })); }
        if (!ensure() || !S.ready || !S.bridgeSession) { return Promise.resolve(Object.assign(failure(IFRAME_NOT_READY, "no bridge session in this page"), { tracked: {} })); }
        if (!S.track) { return Promise.resolve(Object.assign(failure(BRIDGE_COMPONENT_INCOMPATIBLE, "this bridge does not offer tracking"), { tracked: {} })); }
        return ask(QUEUE_TRACK, { request_ids: ids, bridge_session: S.bridgeSession }, QUEUE_TRACK_TIMEOUT_MS, QUEUE_TRACKED);
    }

    /**
     * Protocol 6: make WanGP commit this page's live form, so that a job
     * composed later - on the server, with this page shut - runs at the
     * settings that were on screen when the button was pressed.
     *
     * WHY THIS WAITS INSTEAD OF JUST ASKING.
     *
     * The bridge does not read the form and hand it back. It writes one
     * hidden trigger, and WanGP's own chain commits its own form - which is
     * one component of coupling rather than the ninety a read would need.
     * The cost is that the commit happens *after* the call that asked for it
     * returns, so "it worked" cannot be part of that answer. What comes back
     * is the fingerprint of the recorded form before the write; this polls
     * until it moves.
     *
     * A poll that never moves is not a failure. The ordinary reason for it
     * is that the live form already matched what was recorded - nothing to
     * carry - and the ordinary reason for a timeout is a page that is slow
     * or gone. Both resolve rather than reject, because composing from the
     * recorded form is exactly what happened before any of this existed. A
     * flush is an optimisation and must never become a thing a press needs.
     */
    async function flushForm(options) {
        const settings = options || {};
        const budget = Number(settings.timeoutMs) > 0 ? Number(settings.timeoutMs) : FLUSH_BUDGET_MS;
        const started = Date.now();
        const why = settings.reason ? " [" + text(settings.reason, 60) + "]" : "";
        const give = function (outcome, detail) {
            // A press says what it got either way. A flush nobody asked for
            // says so only when it carried something, because a line every
            // time the user changes tab is noise in the one log that has to
            // stay readable.
            if (!settings.quiet || outcome === FLUSH_COMMITTED) {
                say("flush: " + outcome + why + (detail ? " (" + text(detail, 120) + ")" : ""));
            }
            return { ok: true, flush: outcome };
        };

        if (!ensure() || !S.ready || !S.bridgeSession) { return give(FLUSH_UNAVAILABLE, "no bridge session in this page"); }

        let first;
        try {
            first = await ask(FORM_FLUSH, {}, FLUSH_CALL_TIMEOUT_MS, FORM_FLUSHED);
        } catch (error) {
            return give(FLUSH_UNAVAILABLE, "the bridge did not answer");
        }
        if (!first || first.ok === false) { return give(FLUSH_UNAVAILABLE, first && first.code); }
        if (first.flush === FLUSH_SUPPRESSED) {
            // WanGP asked for the next commit to be skipped, and consuming
            // that one-shot here would let the model switch behind it clobber
            // settings the user just loaded. Their press is not worth that.
            return give(FLUSH_SUPPRESSED, "WanGP is mid settings-load");
        }
        if (first.flush !== FLUSH_REQUESTED) { return give(FLUSH_UNAVAILABLE, first.flush); }

        const before = String(first.fingerprint || "");
        while (Date.now() - started < budget) {
            await new Promise(function (resume) { window.setTimeout(resume, FLUSH_POLL_MS); });
            let probe;
            try {
                probe = await ask(FORM_FLUSH, { probe: true }, FLUSH_CALL_TIMEOUT_MS, FORM_FLUSHED);
            } catch (error) {
                return give(FLUSH_UNAVAILABLE, "the bridge stopped answering mid-flush");
            }
            if (!probe || probe.ok === false) { return give(FLUSH_UNAVAILABLE, probe && probe.code); }
            if (String(probe.fingerprint || "") !== before) { return give(FLUSH_COMMITTED); }
        }
        // Either there was nothing to commit, or the page never got to it.
        // The two are indistinguishable from here and have the same answer.
        return give(FLUSH_UNCHANGED, "the recorded form did not move within " + budget + " ms");
    }

    /* ------------------------------------------------------------------ */
    /* Proactive inheritance: the recorded form, kept current                */
    /* ------------------------------------------------------------------ */

    /**
     * WHY A FLUSH HAPPENS WHEN NOBODY PRESSED ANYTHING.
     *
     * With inheritance on, a queued job is built from the form Wan2GP
     * recorded for the model - and Wan2GP records that only when the user
     * *commits* it: Generate, its own Add to Queue, applying a LoRA set,
     * switching model. Nothing else writes it. So on a fresh start, with a
     * page the user has set up but not generated from, there is no recorded
     * form at all, and a job composed from the Clipboard tab falls through
     * to the settings Wan2GP loads for that model from disk. Close, often
     * identical, and not what the user is looking at.
     *
     * The fix that suggests itself - flush at the press - cannot be had from
     * the Clipboard tab: its button is a server-side Gradio event, and by
     * the time the server is composing, the moment to ask the browser for
     * anything has passed. Making the press wait on a round trip through a
     * second tab's iframe would put a page in the critical path of a queue
     * whose whole point is that pages are not in it.
     *
     * So the commit happens *before* any press, at the moments when it is
     * both free and certain to be needed:
     *
     *   - when the WanGP page becomes ready and inheritance is on, which is
     *     the one that removes "generate once first" entirely;
     *   - when the WanGP tab stops being the tab on screen, which is the
     *     last instant an uncommitted slider still exists and also, for
     *     anyone about to press Add to Queue in the Clipboard tab, the
     *     instant immediately before they do;
     *   - when the page goes to the background.
     *
     * Every one of them is advisory. A flush that does not happen leaves
     * exactly the behaviour there was before this existed, which is why none
     * of it is allowed to block, retry or report a failure.
     */
    /**
     * The server's answer to "is anything going to read this form". Pushed in
     * by the public API from the snapshot it already takes; see tellBridge.
     * Turning it on with a page already ready flushes once, so that switching
     * the setting on is enough - there is nothing else to do and nothing to
     * generate first.
     */
    function inheritSettings(on) {
        const wanted = on === true;
        const changed = P.wanted !== wanted;
        P.wanted = wanted;
        if (changed) { say("settings inheritance is " + (wanted ? "on; WanGP's live form is committed ahead of a press" : "off; WanGP's form is left alone")); }
        if (wanted && changed) { flushProactively("inheritance turned on"); }
        return wanted;
    }

    /**
     * One commit, at most one at a time, and never more often than the gap.
     * Returns nothing: no caller of this is waiting on it, by design.
     */
    function flushProactively(why) {
        if (P.wanted !== true) { return; }
        if (P.running) { return; }
        if (!S.ready || !S.bridgeSession) { return; }
        const now = Date.now();
        if (P.lastAt && now - P.lastAt < PROACTIVE_GAP_MS) { return; }
        P.lastAt = now;
        P.running = true;
        let done;
        try {
            done = flushForm({ reason: why, quiet: true });
        } catch (e) {
            P.running = false;
            return;
        }
        Promise.resolve(done).then(function () { P.running = false; }, function () { P.running = false; });
    }

    /**
     * The WanGP tab leaving the screen. Forge switches tabs by hiding the
     * panel rather than by navigating, so there is no unload to hang this
     * on - but an iframe in a hidden panel stops intersecting, which is the
     * same fact stated in a way the browser will tell us.
     */
    function watchOnScreen(frame) {
        if (!frame || typeof window.IntersectionObserver !== "function") { return; }
        try {
            if (P.observer) { P.observer.disconnect(); }
            P.onScreen = null;
            P.observer = new window.IntersectionObserver(function (entries) {
                for (const entry of entries) {
                    const showing = !!(entry && entry.isIntersecting);
                    const was = P.onScreen;
                    P.onScreen = showing;
                    // Only the leaving edge, and only once it has been seen
                    // on screen: a panel that was hidden all along has no
                    // uncommitted anything to carry.
                    if (was === true && !showing) { flushProactively("the WanGP tab left the screen"); }
                }
            });
            P.observer.observe(frame);
        } catch (e) { /* an engine without it loses the trigger, nothing else */ }
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
        if (request.start !== undefined && request.start !== null && request.start !== "") {
            if (START_MODES.indexOf(request.start) === -1) { return queueRefusal(REQUEST_INVALID, "start is not auto or never", requestId); }
            payload.start = request.start;
        }
        if (request.model_type !== undefined && request.model_type !== null && request.model_type !== "") {
            // Protocol 5: the model this request was composed for; the bridge
            // refuses with MODEL_CHANGED when the page has moved to another.
            if (typeof request.model_type !== "string" || !MODEL_TYPE_RE.test(request.model_type)) { return queueRefusal(REQUEST_INVALID, "model_type is not a model type", requestId); }
            payload.model_type = request.model_type;
        }
        const overrides = QUEUE_FIELDS.filter(function (field) {
            return field === "prompt" ? payload.prompt !== undefined
                : field === "references" ? !!payload.reference_handoff_ids
                : !!payload[field + "_handoff_id"];
        });
        say("queue " + requestId.slice(0, 8) + ": overrides " + (overrides.length ? overrides.join(", ") : "none (the live page as it is)")
            + "; start " + (payload.start || "auto") + (payload.model_type ? "; for model " + payload.model_type : ""));
        return ask(QUEUE_REQUEST, payload, QUEUE_REQUEST_TIMEOUT_MS, QUEUE_RESULT).then(function (answer) {
            if (answer && answer.ok) { return answer; }
            // "refused" is the bridge's word, so it is only supplied for an
            // answer that came from the bridge. A settlement this side decided
            // carries no admission at all - nothing was observed, and the
            // three-value vocabulary has no word for that on purpose.
            const base = answer && answer.unconfirmed
                ? { request_id: requestId }
                : { admission: "refused", request_id: requestId };
            return Object.assign(base, answer);
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
    async function queueAndConfirm(request, options) {
        const asked = await queue(request);
        if (asked && asked.ok && options && typeof options.onAdmitted === "function") {
            // The overlay is on the form from this moment; a caller that keeps
            // a record of its own (the outbox) wants to know before the wait.
            try { await options.onAdmitted(asked); } catch (e) { /* the caller's record, not this call */ }
        }
        const base = {
            request_id: asked.request_id || "",
            applied: asked.applied || normaliseQueueSummary({}).applied,
            inherited: asked.inherited || [],
            ignored: asked.ignored || [],
            model: asked.model || S.model || { type: "", label: "", family: "" },
            tasks_added: 0,
            route: asked.route || "",
            start: asked.start || "",
            generation_running: typeof asked.generation_running === "boolean" ? asked.generation_running : null,
            queue_depth: null
        };
        if (!asked.ok) {
            // The ask did not come back ok, and the two reasons it might not
            // are not interchangeable. The bridge declining is a fact about
            // the request and reports as a refusal; this page giving up while
            // the ask was in flight is a fact about this page, and the request
            // may already be queued inside WanGP. Reporting the second as the
            // first is how a job that was running came back as a failure with
            // a retry button under it. The confirmation loop below already
            // reaches "unconfirmed" correctly; this is the one window it never
            // covered, because it is the window before it starts.
            if (asked.unconfirmed) {
                return Object.assign(base, { ok: false, status: "unconfirmed", code: ADMISSION_UNCONFIRMED,
                                             message: sentence(ADMISSION_UNCONFIRMED), detail: asked.detail || "" });
            }
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
            if (status.status === "queued" || status.status === "started") {
                return Object.assign(base, { ok: true, status: status.status, tasks_added: status.tasks_added || 1, code: "", message: "", detail: "",
                                             queue_depth: status.queue_depth, route: status.route || base.route });
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
                start: !!S.start,
                track: !!S.track,
                generation_running: S.generationRunning,
                model: S.model ? Object.assign({}, S.model) : { type: "", label: "", family: "", architecture: "" },
                inputs: {
                    start: { supported: supported("start_frame") },
                    end: { supported: supported("end_frame") },
                    references: { supported: supported("reference"), max_count: byId.reference ? byId.reference.max_count : null }
                }
            };
        });
    }

    /* ------------------------------------------------------------------ */
    /* Recovery: an iframe that went away without the tab meaning it to       */
    /* ------------------------------------------------------------------ */

    /**
     * The failure this whole section exists for is narrow and it is not the
     * one it looks like.
     *
     * #wangp_iframe_root is still there, #wangp_iframe is not, and the server
     * is meanwhile carrying on perfectly well - admitting the job, composing
     * it, submitting it, generating. So an absent iframe is not evidence that
     * WanGP died; it is evidence that the browser lost its control surface,
     * and the two call for opposite responses. The iframe is server-rendered
     * into a gr.HTML the tab owns, and the observer below only ever handled
     * its ARRIVAL, so once it was gone nothing in this file would ever ask
     * for another one and the absence lasted for the life of the page. That
     * is what makes a user restart a browser over a working generation.
     *
     * What is added is the smallest thing that repairs it: notice the
     * absence, ask the tab whether it still wants a frame, give an ordinary
     * Gradio re-render a moment to land on its own, and otherwise press the
     * tab's own hidden Refresh - the one wired to show(), which repaints from
     * what is true now and starts nothing. Then attach and shake hands the
     * way this file already does.
     *
     * What is deliberately NOT added: no iframe built in JavaScript, no
     * process restart, no page reload, no polling, no storage, no second
     * bridge protocol, and no retry of a generation whose answer was lost.
     */

    //: One short re-check before anything is pressed, to let an ordinary
    //: Gradio DOM replacement finish. Not polling - it runs once per cycle
    //: and only while something is already wrong.
    const ABSENCE_GRACE_MS = 100;
    //: How long a replacement iframe has to appear after the press, in
    //: ON-SCREEN milliseconds. It rides the same visibility-aware deadline
    //: every bridge request uses, so a page in somebody's pocket is not
    //: reported as a failed repair.
    const REPAINT_ARRIVAL_MS = 12000;
    //: The whole of the handshake's own bounded schedule, plus a moment. Not
    //: a second handshake timer and never expected to fire: it is the backstop
    //: for the one case that schedule cannot settle by itself - a replacement
    //: frame that goes away again mid-offer, which stops the offers and would
    //: otherwise leave a cycle holding the single-flight flag for good.
    const HANDSHAKE_BUDGET_MS = HELLO_DELAYS.reduce(function (total, wait) { return total + wait; }, 0) + 2000;

    //: How one repair cycle ended.
    const REPAIR_READY = "ready";               //: the bridge answered; silent
    const REPAIR_NOT_WANTED = "not-wanted";     //: the tab does not want a frame; silent
    const REPAIR_UNPROVEN = "unproven";         //: state unreadable, nothing pressed
    const REPAIR_FAILED = "failed";             //: pressed, and it did not become usable
    const REPAIR_NO_SURFACE = "no-surface";     //: the whole tab subtree is gone
    const REPAIR_ABANDONED = "abandoned";       //: the frame went again; the next cycle judges

    /** The raw view word the tab wrote, for a log line only. "" when there is
     * nothing readable to quote. */
    function wanGpViewName() {
        const raw = box(STATE_ELEM_ID);
        if (!raw || !raw.value) { return ""; }
        try {
            const state = JSON.parse(raw.value);
            return state && typeof state.view === "string" ? text(state.view, 40) : "";
        } catch (e) { return ""; }
    }

    /** Press a hidden Gradio button by its id. Gradio wraps a Button in a
     * div, so the element the id names is usually not the clickable one -
     * which is the part of this that is easy to get wrong. */
    function pressHidden(elementId) {
        const scope = app();
        const element = scope.getElementById ? scope.getElementById(elementId)
            : (scope.querySelector ? scope.querySelector("#" + elementId) : null);
        if (!element) { return false; }
        const button = element.tagName === "BUTTON" ? element
            : (element.querySelector ? element.querySelector("button") : null);
        if (!button || typeof button.click !== "function") { return false; }
        button.click();
        return true;
    }

    /** The one notice this file ever renders, if it is on the page. */
    function noticeElement() {
        const scope = app();
        const direct = scope.getElementById ? scope.getElementById(RECOVERY_NOTICE_ID) : null;
        if (direct) { return direct; }
        return scope.querySelector ? scope.querySelector("#" + RECOVERY_NOTICE_ID) : null;
    }

    /**
     * Say, once, that the controls could not be brought back.
     *
     * Written by the browser rather than painted by the tab, and that is not
     * a preference: the tab's own error surface is only reachable through a
     * Gradio event, and both paths that end here exist precisely because that
     * channel either did not produce a usable result or could not safely be
     * asked. A notice that needs the machinery it is reporting on is a notice
     * that never appears.
     *
     * It goes inside #wangp_iframe_root, as a sibling of the gr.HTML the tab
     * owns - a repaint replaces that child and leaves this alone, which is
     * exactly why clearing it has to be explicit. Returns false when there is
     * no container to render into, and the caller must then NOT record that a
     * warning was shown: a silent no-op that counted as one would suppress
     * the next real warning.
     */
    function showRecoveryWarning() {
        const root = rootElement();
        if (!root || typeof root.appendChild !== "function") { return false; }
        if (S.recovery.warningShown && noticeElement()) { return true; }
        let notice = noticeElement();
        if (!notice) {
            try {
                notice = document.createElement("div");
            } catch (e) {
                return false;
            }
            notice.id = RECOVERY_NOTICE_ID;
            notice.className = RECOVERY_NOTICE_CLASS;
            try { notice.setAttribute("role", "status"); } catch (e) { /* presentation only */ }
            try { root.appendChild(notice); } catch (e) { return false; }
        }
        // textContent, and a constant: nothing a server said, nothing a
        // message payload carried, nothing from an error detail.
        notice.textContent = RECOVERY_NOTICE_TEXT;
        S.recovery.warningShown = true;
        say("recovery: warning shown");
        return true;
    }

    /**
     * Take it away again, explicitly.
     *
     * A Gradio repaint updates the gr.HTML inside the container; it does not
     * rewrite the container. So a notice appended beside that HTML survives
     * the very repaint that fixed the problem, and without this the user
     * would read "could not be reconnected automatically" next to a working
     * iframe. Correctness must not depend on somebody else's side effect.
     */
    function clearRecoveryWarning() {
        const notice = noticeElement();
        if (notice && notice.parentNode && typeof notice.parentNode.removeChild === "function") {
            try { notice.parentNode.removeChild(notice); } catch (e) { /* already gone */ }
        }
        if (notice || S.recovery.warningShown) { say("recovery: warning cleared"); }
        S.recovery.warningShown = false;
    }

    /**
     * Retire the browser's half of a frame that no longer exists.
     *
     * Browser-local, and that is the whole of it. A detached promise and a
     * running server job are separate facts: nothing here cancels an outbox
     * job, stops a generation, unloads a model or restarts a process, because
     * the job was accepted by the server over a loopback control surface with
     * no browser anywhere in the path and it is still going.
     *
     * S.watcher is deliberately untouched - the root observer outlives any one
     * frame, and it is the thing that will notice the replacement.
     */
    function detachFrame(reason) {
        say("recovery: retiring the browser's side of the old iframe (" + text(reason, 120) + ")");
        stopHandshake();
        S.helloStep = 0;
        if (P.observer) {
            // An IntersectionObserver on a removed element never fires again,
            // so this changes no behaviour - it just leaves no observer bound
            // to a node nobody can reach.
            try { P.observer.disconnect(); } catch (e) { /* nothing to undo */ }
            P.observer = null;
            P.onScreen = null;
        }
        // Section 14: a queue admission or an image send that was in flight is
        // retired UNCONFIRMED, never as a refusal. This is the new and more
        // frequent caller that made that distinction urgent.
        abandon(BRIDGE_SESSION_MISMATCH, true);
        S.frame = null;
        S.ready = false;
        S.bridgeSession = "";
        S.bridgeVersion = "";
        S.receivers = [];
        S.revision = "";
        S.query = null;
        S.queue = false;
        S.start = false;
        S.track = false;
        S.generationRunning = null;
        // A cycle past its grace is waiting on this frame - for it to arrive,
        // or for its bridge to speak. Losing the frame is the end of both, so
        // the cycle ends here rather than holding the single-flight flag until
        // its backstop expires. It ends without a verdict: every caller of this
        // follows with either a fresh repair or a clear, and warning on a frame
        // that is about to be repaired again would put a notice on screen for
        // the length of one grace.
        if (S.recovery.active && !S.recovery.graceTimer) {
            say("recovery: the iframe went away again before the bridge answered");
            finishIframeRepair(REPAIR_ABANDONED, "");
        }
        return true;
    }

    /**
     * Section 22. An observer bound to a node that was REPLACED rather than
     * emptied never fires again and never says so, which would silently cost
     * this file every one of the detections above. Three references to
     * S.watcher existed and none of them ever disconnected it, so this is a
     * pre-existing gap that the rest of this section now depends on.
     *
     * The fix is to notice and bind again, re-running the bounded lookup
     * ladder that bound it the first time. Not a document-wide observer.
     */
    function rebindRoot(reason) {
        if (!S.watcherRoot || S.watcherRoot.isConnected) { return false; }
        say("recovery: the WanGP root was replaced; rebinding the observer (" + text(reason, 80) + ")");
        if (S.watcher) {
            try { S.watcher.disconnect(); } catch (e) { /* the node is gone anyway */ }
        }
        S.watcher = null;
        S.watcherRoot = null;
        watchRoot(0);
        return true;
    }

    /** The cycle is over, whatever it ended as. One place, so the grace and
     * the deadline are always cancelled and the single-flight flag is always
     * released. */
    function finishIframeRepair(outcome, detail) {
        if (S.recovery.graceTimer) { clearTimeout(S.recovery.graceTimer); S.recovery.graceTimer = 0; }
        if (S.recovery.deadline) { S.recovery.deadline.cancel(); S.recovery.deadline = null; }
        const took = S.recovery.startedAt ? Date.now() - S.recovery.startedAt : 0;
        const pressed = S.recovery.pressed;
        S.recovery.active = false;
        S.recovery.reason = "";
        S.recovery.pressed = false;
        S.recovery.startedAt = 0;
        if (outcome === REPAIR_READY) {
            say("recovery: bridge ready after repaint in " + took + " ms");
            clearRecoveryWarning();
            return;
        }
        if (outcome === REPAIR_NOT_WANTED) {
            // The repaint LANDED and the answer is a legitimate server view.
            // The tab has already painted the card that explains it, and a
            // recovery warning on top of that card would contradict it.
            say(pressed
                ? "recovery: repaint answered view=" + (detail || "(unreadable)") + "; WanGP is not serving, no warning"
                : "recovery: iframe absent but current WanGP view does not expect one; no action");
            clearRecoveryWarning();
            return;
        }
        if (outcome === REPAIR_NO_SURFACE || outcome === REPAIR_ABANDONED) {
            // Neither of these is a verdict. NO_SURFACE is section 22 - there
            // is no container to render a notice into and the observer is what
            // needs attention. ABANDONED is a frame that went away again while
            // the cycle was waiting on it, and the absence it left behind is
            // about to be classified from the top. Both leave warningShown
            // exactly where they found it, so a later real failure can still
            // say something.
            return;
        }
        showRecoveryWarning();
    }

    /**
     * Press the tab's own Refresh, and only when pressing it is provably the
     * right thing to do.
     *
     * The three checks below are one synchronous block with nothing between
     * them, and the reason is specific: a repaint is a WanGP iframe RELOAD.
     * The tab's hidden Refresh stamps every iframe it paints with a fresh
     * paint token, so the string Gradio is handed is never the one it holds
     * and the element is always re-created - which is what makes this repair
     * work at all (Gradio's frontend applies an identical string as no change
     * and would leave a missing iframe missing), and it is also what makes a
     * press at the wrong moment destructive. A mutation burst can put the
     * frame back in the gap between deciding and pressing, and pressing after
     * that reloads the WanGP page out from under somebody who is using it.
     */
    function requestIframeRepaint(reason) {
        // ---------------- one synchronous block, section 10.1 --------------
        const view = readWanGpView();
        const frame = frameElement();
        const pressed = !frame && view === WAN_VIEW_IFRAME && pressHidden(REFRESH_ELEM_ID);
        // ------------------------- end of the block ------------------------
        if (pressed) {
            S.recovery.pressed = true;
            say("recovery: requesting existing WanGP repaint (reason=" + text(reason, 120) + ")");
            say("recovery: repaint requested");
            return true;
        }
        if (frame) {
            say("recovery: skipped the press; the iframe came back first");
            attachAndSettle();
            return false;
        }
        if (view === WAN_VIEW_NON_IFRAME) {
            finishIframeRepair(REPAIR_NOT_WANTED, wanGpViewName());
            return false;
        }
        if (view === WAN_VIEW_UNKNOWN) {
            if (!rootElement()) {
                say("recovery: state unknown and the root is gone; rebinding the observer");
                finishIframeRepair(REPAIR_NO_SURFACE, "");
                rebindRoot("the state and the root are both missing");
                return false;
            }
            // Not a repaint failure: a safety stop. The extension will not
            // reload a WanGP page that may be perfectly healthy on the
            // strength of a state it could not read.
            say("recovery: state unknown after grace; refresh not pressed");
            finishIframeRepair(REPAIR_UNPROVEN, "");
            return false;
        }
        // The view says iframe, the frame is absent, and the control that
        // would repaint it is not in the page either. Pressing nothing is not
        // a repair, and there is nothing smaller left to try.
        say("recovery: the tab's Refresh control is not in the page; no press");
        finishIframeRepair(rootElement() ? REPAIR_FAILED : REPAIR_NO_SURFACE, "");
        return false;
    }

    /**
     * Bind to a frame the cycle was waiting for, and make sure the cycle ends
     * whatever attach() made of it.
     *
     * A new binding hands the cycle on to the handshake, which settles it. Any
     * other answer - already bound, or a frame that stopped being findable in
     * between - leaves nothing that would ever settle it, and a cycle holding
     * the single-flight flag with nothing left to end it blocks every repair
     * this page would otherwise have made for the rest of its life.
     */
    function attachAndSettle(options) {
        attach(options || null);
        if (S.recovery.active && !S.recovery.graceTimer && !S.recovery.deadline) {
            finishIframeRepair(S.ready ? REPAIR_READY : REPAIR_ABANDONED, "");
        }
    }

    /** The replacement never came. Section 10.2's three outcomes, decided on
     * what the tab says NOW rather than on what it said before the press. */
    function repaintDeadlineExpired() {
        S.recovery.deadline = null;
        if (!S.recovery.active) { return; }
        if (frameElement()) {
            // It landed in the same instant the budget ran out. Take it.
            attachAndSettle();
            return;
        }
        const view = readWanGpView();
        if (view === WAN_VIEW_NON_IFRAME) {
            finishIframeRepair(REPAIR_NOT_WANTED, wanGpViewName());
            return;
        }
        say("recovery: repair failed; iframe did not return within " + REPAINT_ARRIVAL_MS + " ms on screen");
        if (view === WAN_VIEW_UNKNOWN) {
            // Grouped with failure on purpose. An unreadable state AFTER a
            // press is not evidence that the server chose a non-iframe view;
            // it is evidence that the outcome cannot be proved.
            say("recovery: repaint outcome unknown; no further automatic action");
        }
        finishIframeRepair(rootElement() ? REPAIR_FAILED : REPAIR_NO_SURFACE, "");
    }

    /** What happens once the grace has run and the frame is still missing. */
    function afterAbsenceGrace() {
        S.recovery.graceTimer = 0;
        if (!S.recovery.active) { return; }
        if (frameElement()) {
            // An ordinary Gradio replacement, arriving exactly as it should.
            // This is what the grace is for, and no press is issued.
            say("recovery: replacement appeared during grace; attaching");
            attachAndSettle();
            return;
        }
        if (requestIframeRepaint(S.recovery.reason)) {
            S.recovery.deadline = deadline(REPAINT_ARRIVAL_MS, repaintDeadlineExpired);
        }
    }

    /**
     * One repair cycle, start to finish, and never two at once.
     *
     * Single-flight matters more than it sounds: DOM mutation bursts are
     * ordinary, and a cycle per mutation would be a Refresh press per
     * mutation - each one a WanGP reload.
     */
    function requestIframeRepair(reason) {
        if (S.recovery.active) { return false; }
        S.recovery.active = true;
        S.recovery.reason = text(reason, 120);
        S.recovery.startedAt = Date.now();
        S.recovery.pressed = false;
        say("recovery: unexpected iframe disappearance; waiting for replacement grace (reason="
            + S.recovery.reason + ")");
        S.recovery.graceTimer = setTimeout(afterAbsenceGrace, ABSENCE_GRACE_MS);
        return true;
    }

    /** A replacement bound while a cycle was running. The arrival budget is
     * over; the cycle is not, because the handshake decides it. */
    function noteReplacementArrived() {
        if (!S.recovery.active) { return; }
        if (S.recovery.graceTimer) { clearTimeout(S.recovery.graceTimer); S.recovery.graceTimer = 0; }
        if (S.recovery.deadline) { S.recovery.deadline.cancel(); S.recovery.deadline = null; }
        const took = S.recovery.startedAt ? Date.now() - S.recovery.startedAt : 0;
        say("recovery: replacement iframe found after " + took + " ms");
        say("recovery: attached to replacement iframe");
        S.recovery.deadline = deadline(HANDSHAKE_BUDGET_MS, handshakeBackstop);
    }

    /** Nothing settled the handshake. In practice step() always does, and this
     * exists so that "in practice" is not what the single-flight flag rests on. */
    function handshakeBackstop() {
        S.recovery.deadline = null;
        if (!S.recovery.active) { return; }
        if (S.ready) { finishIframeRepair(REPAIR_READY, ""); return; }
        handshakeSettledUnusable("the handshake never settled");
    }

    /** The handshake settled without a usable bridge. The cycle ends here,
     * not at the arrival deadline: a frame that arrived and then would not
     * speak is exactly as unusable as one that never arrived. */
    function handshakeSettledUnusable(why) {
        if (!S.recovery.active) { return; }
        say("recovery: repair failed; bridge did not answer handshake (" + text(why, 80) + ")");
        finishIframeRepair(rootElement() ? REPAIR_FAILED : REPAIR_NO_SURFACE, "");
    }

    /**
     * Section 13. The page came back from hidden or frozen: check that what
     * this side believes is still true, and do nothing whatsoever if it is.
     *
     * Order matters. The observer rebind comes first because a root that was
     * replaced while the page was away makes every question below it
     * meaningless - readWanGpView() would answer UNKNOWN for a reason that
     * has nothing to do with the server.
     *
     * A healthy resume costs one state read and two DOM lookups. No request,
     * no press, no health endpoint, no poll, and no line in the journal
     * beyond the one lifecycle already writes.
     */
    function verifyOnResume(reason) {
        rebindRoot(reason);
        const view = readWanGpView();
        if (view === WAN_VIEW_NON_IFRAME) {
            if (S.frame || S.ready || S.bridgeSession) {
                detachFrame("the WanGP view changed while the page was away");
            }
            clearRecoveryWarning();
            return;
        }
        if (S.frame && !S.frame.isConnected) {
            detachFrame("frame detached while away");
            // Said only when a cycle actually starts. A resume that lands on
            // top of a repair already in flight is not news.
            if (requestIframeRepair(reason)) { say("recovery: page resumed; iframe missing, repairing"); }
            return;
        }
        const frame = frameElement();
        if (!frame) {
            if (S.frame || S.ready || S.bridgeSession) { detachFrame(reason); }
            if (requestIframeRepair(reason)) { say("recovery: page resumed; iframe missing, repairing"); }
            return;
        }
        if (frame !== S.frame) {
            attachAndSettle();
            return;
        }
        // The frame is the one we know and it is still in the page. If the
        // bridge never finished introducing itself, offer once more - the
        // existing bounded schedule, not a new one.
        if (!S.ready) { rearm(); }
    }

    /* ------------------------------------------------------------------ */
    /* Transport: six connections to one origin, and who is holding them     */
    /* ------------------------------------------------------------------ */

    //: After the event spine has gone quiet, how long this tab waits - in
    //: ON-SCREEN milliseconds - for the plain request the interop layer sends
    //: in answer to that silence to come back, before concluding that nothing
    //: from this page is getting a connection at all.
    //:
    //: The number is about the browser, not the server. A browser holds six
    //: connections to one origin under HTTP/1.1, and on the day this was
    //: written five of them were streams: Forge's heartbeat, Forge's queue,
    //: this extension's event spine, and the WanGP iframe's heartbeat and
    //: queue through the proxy. When the WanGP child stopped answering, the
    //: iframe's two were held open on a server that would never speak again,
    //: the sixth was a proxied request waiting on the same server, and from
    //: then on every click in the WebUI queued in the browser behind them.
    //: The server was fine the whole time - it finished jobs for nine minutes
    //: while the page could not reach it - and the only thing that freed the
    //: page was restarting the browser. This is the tab noticing in twenty
    //: seconds instead of never, and giving back the two connections that are
    //: its own to give.
    const TRANSPORT_STARVED_MS = 20000;
    //: After the shed: how long to wait, on screen, for the spine to say the
    //: transport is back before loading WanGP again regardless. The reload
    //: waits for that answer on purpose - a WanGP page load takes connections
    //: too, and Forge's queued requests deserve the freed ones first - and it
    //: does not wait for ever, because a blank tab is not a recovery.
    const RELOAD_AFTER_SHED_MS = 15000;
    //: A runtime frame or snapshot that disagrees with the tab presses the
    //: tab's Refresh, at most this often. STARTING and READY arrive seconds
    //: apart, and each would otherwise be a press.
    const RUNTIME_PRESS_GAP_MS = 3000;
    //: And never in the same instant as the frame: the burst is judged once,
    //: at its end, against the last thing the server said.
    const RUNTIME_PRESS_DEBOUNCE_MS = 300;
    //: The one event the interop layer dispatches, whatever it is about.
    const OUTBOX_EVENT = "minipaint:outbox";

    /**
     * What the interop layer says about its event stream. "silent" is the
     * stream having had no frame for longer than the server's heartbeat
     * allows, and the layer answering that by sending one plain request;
     * "answered" is that request coming back, however it came back; "open"
     * is the stream itself again. Only the first arms anything, and either
     * of the other two disarms it.
     */
    function onStreamState(detail) {
        const state = String(detail.state || "");
        if (state === "silent") {
            if (S.transport.deadline) { return; }
            S.transport.silentSince = Date.now();
            say("transport: the event spine has been silent for " + Math.round(Number(detail.silent_ms || 0) / 1000)
                + "s; waiting " + TRANSPORT_STARVED_MS + " ms on screen for a plain request to come back");
            S.transport.deadline = deadline(TRANSPORT_STARVED_MS, transportStarved);
            return;
        }
        if (state !== "answered" && state !== "open") { return; }
        if (S.transport.deadline) {
            S.transport.deadline.cancel();
            S.transport.deadline = null;
            say("transport: Forge answered (" + state + "); the silence was the stream's alone, nothing shed");
        }
        S.transport.silentSince = 0;
        if (S.transport.shed) { reloadAfterShed("the transport is back (" + state + ")"); }
    }

    /**
     * The budget ran out with the request still unanswered - not refused,
     * not failed, UNANSWERED, which a request that never got a connection is
     * and a request to a server that is down is not (that one fails, and
     * failing is an answer). Nothing from this page is getting through, and
     * the iframe's connections are the two this tab can give back.
     */
    function transportStarved() {
        S.transport.deadline = null;
        const silent = S.transport.silentSince ? Date.now() - S.transport.silentSince : 0;
        S.transport.silentSince = 0;
        // The element in the page, not the one this file remembers: a
        // remembered frame that Gradio has already replaced holds nothing.
        const frame = frameElement() || (S.frame && S.frame.isConnected ? S.frame : null);
        if (!frame) {
            say("transport: no answer from Forge " + silent + " ms after the spine went silent, and no WanGP iframe to shed;"
                + " nothing this tab holds is in the way");
            return;
        }
        S.transport.src = (frame.getAttribute("src") || "") || S.transport.src;
        S.transport.shed = true;
        S.transport.shedAt = Date.now();
        S.transport.sheds += 1;
        say("transport: no answer from Forge " + silent + " ms after the spine went silent; unloading the WanGP iframe"
            + " to give its connections back (shed " + S.transport.sheds + " of this page)");
        // Unloading the document is what closes its streams and aborts its
        // requests. Navigating it to about:blank needs no connection of its
        // own, which is the property that matters: a navigation to a URL
        // would wait for a connection behind the very requests it is meant to
        // free, and the old document would live on until it got one.
        try { frame.src = "about:blank"; } catch (e) { /* a frame this file cannot navigate is one it cannot shed */ }
        detachFrame("its connections were shed");
        S.transport.reload = deadline(RELOAD_AFTER_SHED_MS, function () {
            S.transport.reload = null;
            reloadAfterShed("nobody answered within " + RELOAD_AFTER_SHED_MS + " ms on screen; loading WanGP again regardless");
        });
    }

    /**
     * Load WanGP into the shed frame again: the same element, the same path
     * it was loading, and no server round trip - the shed was the browser's
     * doing and so is the undoing. When the element itself has gone in the
     * meantime, the tab's own Refresh paints a new one.
     */
    function reloadAfterShed(why) {
        if (!S.transport.shed) { return; }
        if (S.transport.reload) { S.transport.reload.cancel(); S.transport.reload = null; }
        S.transport.shed = false;
        const frame = frameElement();
        const src = S.transport.src;
        if (frame && src && (frame.getAttribute("src") || "") === "about:blank") {
            say("transport: loading WanGP again in the shed iframe (" + text(why, 120) + ")");
            try { frame.src = src; } catch (e) { /* then it stays blank, and the line above says so */ }
            attachAndSettle({ reload: true });
            return;
        }
        if (!frame && readWanGpView() === WAN_VIEW_IFRAME && pressHidden(REFRESH_ELEM_ID)) {
            say("transport: the shed iframe is gone; asking the tab to paint a new one (" + text(why, 120) + ")");
            return;
        }
        say("transport: nothing to load again (" + text(why, 120) + ")");
    }

    /**
     * A runtime frame from the spine, or the runtime summary of a snapshot:
     * the server saying whether WanGP is serving. The tab was painted from
     * that same fact when Forge built it and never since, so this is where a
     * page finds out the fact changed without it - WanGP started for a job
     * another page queued, or went down under an iframe that still shows it.
     * The tab's own Refresh paints what is true; nothing here decides what
     * that is.
     */
    function onRuntimeFrame(summary) {
        if (!summary || typeof summary !== "object" || typeof summary.running !== "boolean") { return; }
        S.runtimePress.running = summary.running;
        if (S.transport.shed || S.recovery.active) { return; }
        if (summary.running === !!frameElement()) { return; }
        scheduleRuntimePress(summary.running
            ? "WanGP is serving and the tab shows no iframe"
            : "WanGP is not serving (" + (text(summary.state, 40) || "state unknown") + ") and the tab still shows an iframe");
    }

    /** One press for a burst of frames, and never sooner than the gap after
     * the last. Judged again at the moment of pressing, against the latest
     * frame: a tab that has caught up in the meantime is left alone. */
    function scheduleRuntimePress(why) {
        if (S.runtimePress.timer) { return; }
        const wait = Math.max(RUNTIME_PRESS_DEBOUNCE_MS, RUNTIME_PRESS_GAP_MS - (Date.now() - S.runtimePress.at));
        S.runtimePress.timer = setTimeout(function () {
            S.runtimePress.timer = 0;
            if (S.transport.shed || S.recovery.active) { return; }
            if (S.runtimePress.running === null || S.runtimePress.running === !!frameElement()) { return; }
            S.runtimePress.at = Date.now();
            if (pressHidden(REFRESH_ELEM_ID)) { say("runtime: " + why + "; asking the tab to paint what is true now"); }
        }, wait);
    }

    function onOutboxEvent(event) {
        const detail = event && event.detail;
        if (!detail || typeof detail !== "object") { return; }
        try {
            if (detail.kind === "stream") { onStreamState(detail); }
            else if (detail.kind === "runtime") { onRuntimeFrame(detail.detail); }
            else if (detail.kind === "synced") { onRuntimeFrame(detail.runtime); }
        } catch (e) { /* never worth breaking the listener chain over */ }
    }

    /** Listen to the interop layer. It dispatches on the document, so this
     * costs nothing when that layer is not loaded and needs no handle on it
     * when it is. */
    function watchTransport() {
        try { document.addEventListener(OUTBOX_EVENT, onOutboxEvent); } catch (e) { /* no document, no transport to watch */ }
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
        if (S.transport.shed && (frame.getAttribute("src") || "") === "about:blank") {
            say("attach: the iframe is shed; not binding to it until it is loaded again");
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
        // If a repair cycle is running, this is the replacement it was waiting
        // for: the arrival budget is over, the cycle is not - the handshake
        // below decides how it ends.
        noteReplacementArrived();
        watchOnScreen(frame);
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
        // Only reached when there is no live frame, so this costs a healthy
        // page nothing: it is the cheapest existing moment to notice that the
        // root was replaced and the observer is watching a node nobody can
        // reach any more.
        rebindRoot("a caller found no live frame");
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
            start: S.start,
            track: S.track,
            generation_running: S.generationRunning,
            code: S.lastCode,
            // What the repair cycle is doing, if anything. All four are at
            // rest on a healthy page; they are here because "the controls
            // came back on their own" and "nobody noticed they had gone" look
            // identical in a bug report without them.
            recovery: {
                active: S.recovery.active,
                reason: S.recovery.reason,
                pressed: S.recovery.pressed,
                warning_shown: S.recovery.warningShown
            },
            // The transport breaker, for the same reason: "the tab went blank
            // for fifteen seconds" and "the tab shed its iframe on purpose"
            // are one line apart in a bug report only with these.
            transport: {
                waiting: !!S.transport.deadline,
                shed: S.transport.shed,
                sheds: S.transport.sheds
            },
            boot_repainted: S.bootRepainted
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
                    say(RECEIVE_IMAGE + ": no acknowledgement within " + RECEIVE_TIMEOUT_MS + " ms on screen (" + requestId.slice(0, 8) + ")");
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
        const root = rootElement();
        if (!root) {
            const next = ROOT_LOOKUPS[step_ + 1];
            if (next !== undefined) { setTimeout(function () { watchRoot(step_ + 1); }, next); }
            else { say("watchRoot: gave up looking for #" + IFRAME_ROOT_ID + "; the WanGP tab never drew it"); }
            return;
        }
        say("watchRoot: found #" + IFRAME_ROOT_ID + " after " + step_ + " attempt(s)");
        attach(null);
        // The card under this root was painted when Forge STARTED, not when
        // this page loaded, and nothing repaints it until something presses
        // a button on it. A page loaded an hour later therefore shows "Start
        // WanGP" over a WanGP that has been serving since. One press of the
        // tab's own Refresh at boot paints what is true now, and starts
        // nothing. Pressed only when there is no iframe to disturb: a frame
        // that is present speaks for itself through the handshake, and a
        // runtime frame corrects a wrong one.
        if (!S.bootRepainted) {
            S.bootRepainted = true;
            if (!frameElement() && pressHidden(REFRESH_ELEM_ID)) {
                say("boot: the tab was painted when Forge started; asking it to paint what is true now");
            }
        }
        if (typeof MutationObserver !== "function") { return; }
        // Kept rather than disconnected after the first iframe: the tab
        // re-renders that element whenever it mints a new channel, and each
        // new element is a new session to shake hands with.
        S.watcher = new MutationObserver(function (records) {
            // The notice this file writes lives inside the node being watched,
            // so putting it there or taking it away is itself a mutation here.
            // Acting on that would press Refresh because of a message saying
            // Refresh did not work.
            if (onlyTheNotice(records)) { return; }
            const frame = frameElement();
            if (frame) {
                if (frame !== S.frame) { attachAndSettle(); }
                return;
            }
            // The iframe is gone, and whether that is a fault is not a DOM
            // question. The tab clears it on purpose every time it paints
            // setup, starting or error - that is what those views are - so
            // ask the tab before touching anything.
            const view = readWanGpView();
            if (view === WAN_VIEW_NON_IFRAME) {
                if (S.frame || S.ready || S.bridgeSession) {
                    // Said only when there was something to retire. A tab
                    // sitting on its setup card re-renders like any other, and
                    // a line per render would be chatter on a healthy page.
                    say("recovery: iframe absent but current WanGP view does not expect one; no action");
                    detachFrame("the iframe no longer belongs to the current WanGP view");
                }
                if (S.recovery.active) { finishIframeRepair(REPAIR_NOT_WANTED, wanGpViewName()); }
                clearRecoveryWarning();
                return;
            }
            if (S.frame || S.ready || S.bridgeSession) { detachFrame("iframe removed"); }
            // IFRAME and UNKNOWN both enter the cycle; only IFRAME is ever
            // allowed to press, and that is decided after the grace.
            requestIframeRepair("root mutation");
        });
        S.watcher.observe(root, { childList: true, subtree: true });
        S.watcherRoot = root;
    }

    /** Whether a mutation batch is nothing but this file's own notice being
     * added or removed. Anything else - including a batch with no added or
     * removed nodes at all - is the page's business and is acted on. */
    function onlyTheNotice(records) {
        if (!records || !records.length) { return false; }
        for (const record of records) {
            const added = (record && record.addedNodes) || [];
            const removed = (record && record.removedNodes) || [];
            if (!added.length && !removed.length) { return false; }
            for (const node of Array.prototype.slice.call(added).concat(Array.prototype.slice.call(removed))) {
                if (!node || node.id !== RECOVERY_NOTICE_ID) { return false; }
            }
        }
        return true;
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
            document.addEventListener("DOMContentLoaded", function () { startAuthProbe(); watchRoot(0); watchLifecycle(); watchTransport(); }, { once: true });
        } else {
            startAuthProbe();
            watchRoot(0);
            watchLifecycle();
            watchTransport();
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
        frameTimer: frameTimerNote,
        reportFrames: reportFrames,
        switchToWanGP: switchToWanGP,
        theme: theme,
        message: sentence,
        // Protocol 3, the queue. window.minipaintInterop is the public face
        // of these; they are the mechanics, and their shapes may change.
        queue: queue,
        confirmQueue: confirmQueue,
        queueAndConfirm: queueAndConfirm,
        trackQueue: trackQueue,
        flushForm: flushForm,
        inheritSettings: inheritSettings,
        capabilities: capabilities,
        // One line into the same journal the handshake and the queries write
        // to, for the Canvas's half of a send. Text only; nothing is parsed.
        note: function (message) { say(text(message, 300)); }
    };
})();
