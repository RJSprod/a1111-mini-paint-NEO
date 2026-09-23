/**
 * The Clipboard tab's browser half.
 *
 * The tab is Gradio components the server renders; this file only listens.
 * A tap on a thumbnail selects it and enables the +First / +Last / +Ref
 * buttons; the menu is a flyout drawn here whose every item presses a hidden
 * Gradio button or writes a hidden textbox, so each action is still a Gradio
 * event with the page's own values as inputs; a paste reads the browser's
 * clipboard and posts the bytes to the tab's own import route; a drop on a
 * slot card does the same and then assigns the imported picture.
 *
 * Add to Queue is the one thing that leaves the page, and even that only
 * indirectly: the server builds the public request from the draft -
 * inherited fields omitted - and appends it to its own queue outbox as a
 * job; this file then asks window.minipaintInterop to pump, which is the
 * same public API any extension uses, and the server re-renders the job
 * list as the pump reports. The one fact this file carries to the server
 * is the WanGP model the page is on, as the public API's capabilities
 * answer names it - a press with enhanced prompts on is written for that
 * model - and it goes into a hidden box like everything else. Nothing here
 * talks to the WanGP iframe, knows a bridge session, holds a queue, or
 * sees a file path.
 *
 * Nothing polls. The one bounded watcher runs for a few seconds after the
 * Add to Queue click, in case the chained change never reaches this page,
 * and stops the moment the instruction is delivered either way; the pump
 * itself stops when this page has nothing left to send, and the public
 * API's own tracking of a queued job ends when its task leaves WanGP.
 */
window.minipaintClipboard = (function () {
    "use strict";

    const ROOT_ID = "minipaint_clipboard_root";
    const BROWSER_ID = "minipaint_clipboard_browser";
    const COMPOSER_ID = "minipaint_clipboard_composer";
    const PROMPT_ID = "minipaint_clipboard_prompt";
    const GRID_ID = "minipaint_clipboard_grid";
    const MENU_ID = "minipaint_clipboard_menu";
    const SORT_BUTTON_ID = "minipaint_clipboard_sort_open";
    const SEND_BUTTON_ID = "minipaint_clipboard_send_open";
    const STATUS_ID = "minipaint_clipboard_status";
    const QUEUE_STATUS_ID = "minipaint_clipboard_queue_status";
    const QUEUE_BUTTON_ID = "minipaint_clipboard_queue";
    const OUTBOX_LIST_ID = "minipaint_clipboard_outbox_list";
    const HISTORY_LIST_ID = "minipaint_clipboard_history_list";
    const WANGP_LINE_ID = "minipaint_clipboard_wangp_line";
    const ENHANCE_PANEL_ID = "minipaint_clipboard_enhance_panel";
    //: What the stylesheet keys the full-window shape off. One class, on the
    //: panel that was already there - the editor is not moved, copied or
    //: rebuilt, so every control in it stays the one control it has been.
    const EDITOR_OPEN_CLASS = "minipaint-clip-fullscreen";
    //: Marked on every wrapper between the panel and the box being edited.
    //: The framework puts each component in two or three nested divs and
    //: sets flex-grow and display on some of them INLINE, so "the box fills
    //: what is left" cannot be said from the stylesheet alone - it has to be
    //: said about the particular chain, and the browser is what knows which
    //: chain that is.
    const EDITOR_GROW_CLASS = "minipaint-clip-grow";
    const TAB_PANEL_ID = "tab_minipaint_clipboard";
    //: Matches the stylesheet's own default for --minipaint-clip-thumb.
    const DEFAULT_THUMB = 144;
    //: The tile's frame on one axis - 4px of padding and a 2px border, twice
    //: - and the strip under the square that carries the name. A column is
    //: the thumbnail plus the first, a row is the thumbnail plus the second.
    //: The stylesheet declares the same two numbers, as
    //: --minipaint-clip-frame and --minipaint-clip-caption, for the rules
    //: that document the grid; test_clipboard_ui.py holds the two copies
    //: equal. Why the script states them at all is above pin().
    const TILE_FRAME = 12;
    const TILE_CAPTION = 32;
    const IMPORT_ROUTE = "/minipaint-clipboard/import";
    const SEND_ROUTE = "/minipaint-clipboard/send";
    const BOXES = {
        selected: "minipaint_clipboard_selected",
        sortRequest: "minipaint_clipboard_sort_request",
        //: The intercept destination, written after the route has saved it,
        //: like the sort: the second door, kept so a page whose Gradio is
        //: alive re-renders its menu state. Nothing waits on it.
        intercept: "minipaint_clipboard_intercept",
        slotAction: "minipaint_clipboard_slot_action",
        sendRequest: "minipaint_clipboard_send_request",
        sendAck: "minipaint_clipboard_send_ack",
        historyAction: "minipaint_clipboard_history_action",
        menuState: "minipaint_clipboard_menu_state",
        model: "minipaint_clipboard_model"
    };
    const PRESS = {
        refresh: "minipaint_clipboard_refresh",
        upload: "minipaint_clipboard_upload",
        folder: "minipaint_clipboard_folder_open",
        rename: "minipaint_clipboard_rename_open",
        remove: "minipaint_clipboard_delete_open",
        paste: "minipaint_clipboard_paste_open",
        history: "minipaint_clipboard_history_open",
        send: "minipaint_clipboard_send_press",
        sendBackend: "minipaint_clipboard_send_backend",
        //: The toolbar's own delete, which acts on the press with nothing to
        //: confirm - so the browser keeps it disabled while nothing is
        //: selected. See select().
        deleteNow: "minipaint_clipboard_delete_now"
    };
    const ROLE_IDS = { first: "minipaint_clipboard_to_first", last: "minipaint_clipboard_to_last", ref: "minipaint_clipboard_to_ref" };
    //: Every control that does something TO the selected picture.
    const NEEDS_SELECTION = function () {
        return Object.keys(ROLE_IDS).map(function (slot) { return ROLE_IDS[slot]; })
            .concat([PRESS.deleteNow]);
    };
    const SLOT_UPLOAD_PREFIX = "minipaint_clipboard_slot_upload_";
    const SLOT_FIELDS = { first: "start", last: "end", ref: "references" };
    // THE PRESS ACKNOWLEDGEMENT USED TO BE A HIDDEN BOX, WATCHED.
    //
    // A Gradio chained callback wrote the server's answer into it, and on
    // some installed versions the change event that would have said so never
    // fired - so the page kept an observer, then a delay, then a value poll
    // six times a second, for a value that arrives once. All of that existed
    // because the answer came back through a channel that might not deliver.
    //
    // It comes back in the response to the press now. There is nothing to
    // observe and nothing to poll: a press that was not answered is a failed
    // request, which says so.
    const CAPABILITY_THROTTLE_MS = 2500;
    // Job states the server runs. A page that sees one of these watches the
    // event stream instead of pumping: the job continues whether or not this
    // document exists.
    const SERVER_STATES = ["admitted", "waiting_turn", "enhanced", "ensuring_wangp", "composing",
        "waiting_for_card", "submitting_wangp", "wangp_waiting", "wangp_generating",
        "completed", "execution_unknown"];
    const OUTBOX_REFRESH_THROTTLE_MS = 300;

    //: What still needs the framework's channel, named so the notice can
    //: say which parts of the tab are stale rather than claiming all of it
    //: is. This list SHRINKS as rows move, and when it is empty the notice
    //: for a dead channel stops being raised at all.
    const STALE_WITHOUT_THE_CHANNEL = [
        "the composer's slot cards", "rename and delete", "the folder chooser",
        "the prompt enhancement panel", "Queue Send History"
    ];

    const S = {
        attached: false,
        selected: "",
        //: Set when a send went unanswered, cleared by the next
        //: acknowledgement: how long the page is willing to wait next time.
        queueDown: false,
        //: The timer and the watcher looking for the server's receipt for a
        //: send. See watchSend.
        sendWatch: null,
        sendPoll: null,
        //: The thumbnail size in pixels, remembered across refreshes: the
        //: grid element is replaced wholesale and takes its inline style
        //: with it, so without this every refresh snapped back to default.
        thumb: 0,
        menu: null,
        menuSection: null,
        //: The element the flyout is drawn under. The menu button when the
        //: menu opened it, a toolbar button when one of those did.
        menuAnchor: null,
        //: The gallery's own working set: what this page of it holds, which
        //: one is in the stage, and the element playing it.
        outputs: { items: [], page: 0, pages: 1, chosen: "", stage: null, strip: null, player: null },
        outputsKey: null,
        //: A flyout a toolbar button opened is the whole list: there is
        //: nothing behind it, so it shows no Back row and Escape closes it
        //: rather than stepping up a level that does not exist.
        menuStandalone: false,
        menuOutside: null,
        menuKey: null,
        queued: {},
        lastInstruction: "",
        capabilitiesAt: 0,
        capabilities: null,
        lastModel: "",
        pasteListener: null,
        outboxListener: null,
        outboxRefreshTimer: null,
        //: The one read of the queue route in flight, if any. Reads are
        //: coalesced: a second ask while one is out gets the same answer,
        //: because two identical questions to a server that is slow to
        //: answer the first are two held connections for one answer - and
        //: the browser has six to this origin in total. See askQueue.
        queueRead: null,
        toastTimer: null,
        //: The debounce on remembering the thumbnail size. See setThumbnailSize.
        thumbSave: 0,
        //: The Escape listener that belongs to the full-window editor, held
        //: only while it is open.
        editorKey: null,
        //: When the grid was last re-drawn. A page that has just drawn is a
        //: page whose selection and sizes need re-applying.
        renderedAt: 0,
        //: When a Gradio round trip was last seen to land on this page. The
        //: menu state carries a nonce for exactly this, so a callback that
        //: happened to return the same values still proves the channel is
        //: alive. It is the one thing an HTTP request cannot tell us.
        gradioSeenAt: 0,
        //: The last thing reportTiles said, so an unchanged grid says it once.
        tileReport: "",
        //: The self-check that runs only while the connection notice is up.
        retryTimer: 0,
        retryDelay: 0,
        retryPending: false,
        retryVisibility: null,
        //: Whether a retry CYCLE is open, which is not the same as a timer
        //: being pending: an attempt in flight has no timer and must not let
        //: a second cycle start beside it. See retryConnection.
        retrying: false,
        //: What the menu has been told since the page was built, over this
        //: tab's own route rather than through a Gradio render.
        menuOverride: null,
        //: The gallery's 🖌️ button, taken over while the setting says WanGP:
        //: the one capture-phase listener, and how many presses it finished.
        galleryListener: null,
        takeovers: 0,
        //: What is wrong, if anything. Two different faults with two
        //: different sentences; see renderNotice.
        offline: { server: false, queue: false, stale: STALE_WITHOUT_THE_CHANNEL },
        //: The grid, which the browser draws now. See "The library".
        library: {
            //: The library's own generation, as the index route said it.
            //: Not the event spine's: an unrelated job must not make this
            //: page re-fetch its pictures.
            revision: "",
            sort: "",
            page: 0,
            pages: 1,
            total: 0,
            size: 60,
            configured: true,
            //: Which page holds the selection, or -1. The pager's only job
            //: towards a selection the user has paged away from.
            selectedPage: -1,
            //: The tiles in the document, by asset id, so a page that is
            //: drawn again reuses the nodes it already has - which is what
            //: keeps decoded pictures, scroll position and the selection.
            tiles: new Map(),
            ids: [],
            busy: false,
            inflight: null,
            pending: false,
            //: The page that was last ASKED for, and which request's answer
            //: is still the current one. See stepPage and fetchLibrary.
            wanted: 0,
            ticket: 0
        }
    };

    /* ------------------------------------------------------------------ */
    /* Small helpers                                                         */
    /* ------------------------------------------------------------------ */

    function app() {
        try {
            if (typeof gradioApp === "function") { return gradioApp() || document; }
        } catch (e) { /* fall through */ }
        return document;
    }

    function byId(id) { return app().getElementById ? app().getElementById(id) : document.getElementById(id); }

    function root() { return byId(ROOT_ID); }

    function textarea(id) {
        const host = byId(id);
        return host ? host.querySelector("textarea, input") : null;
    }

    function boxValue(id) {
        const target = textarea(id);
        return target ? String(target.value || "") : "";
    }

    /** Write a hidden Gradio textbox the way a user would, so the server sees it. */
    function sendInput(id, text) {
        const target = textarea(id);
        if (!target) { return false; }
        // Through the host's own accessor, not a plain assignment: see
        // window.minipaintWriteInput in javascript/main.js. A framework keeps
        // its own record of what an input holds, and an assignment leaves it
        // untouched - the write lands and the event never happens.
        if (typeof window.minipaintWriteInput === "function") {
            return window.minipaintWriteInput(target, text);
        }
        target.value = text;
        target.dispatchEvent(new Event("input", { bubbles: true }));
        target.dispatchEvent(new Event("change", { bubbles: true }));
        return true;
    }

    /** Press a hidden Gradio button by its id. */
    function pressHidden(id) {
        const element = byId(id);
        if (!element) { return false; }
        const button = element.tagName === "BUTTON" ? element : element.querySelector("button");
        if (!button) { return false; }
        button.click();
        return true;
    }

    function menuState() {
        let state = {};
        try { state = JSON.parse(boxValue(BOXES.menuState) || "{}") || {}; } catch (e) { state = {}; }
        // What the tab's own routes have said since the page was built wins
        // over what the page was built with: a setting changed over HTTP is
        // saved whether or not a Gradio render ever comes back to confirm it.
        return S.menuOverride ? Object.assign({}, state, S.menuOverride) : state;
    }

    function note(message) {
        const api = window.minipaintWanGP;
        if (api && typeof api.note === "function") {
            try { api.note("clipboard: " + message); } catch (e) { /* never worth an exception */ }
        }
    }

    /** A short notice over the browser, gone by itself. */
    function toast(text, failure) {
        const container = byId(BROWSER_ID) || root();
        if (!container) { return; }
        let element = container.querySelector(".minipaint-clip-toast");
        if (!element) {
            element = document.createElement("div");
            element.className = "minipaint-clip-toast";
            element.setAttribute("role", "alert");
            container.appendChild(element);
        }
        if (S.toastTimer) { clearTimeout(S.toastTimer); S.toastTimer = null; }
        if (!text) { element.hidden = true; return; }
        element.textContent = text;
        element.classList.toggle("minipaint-clip-toast-failure", !!failure);
        element.hidden = false;
        S.toastTimer = setTimeout(function () { element.hidden = true; S.toastTimer = null; }, 4500);
    }

    /**
     * The one standing line this tab has, and what it is allowed to say.
     *
     * "THIS PAGE HAS LOST ITS LIVE CONNECTION TO FORGE" IS RETIRED. It was
     * raised when a framework's event stream sulked, which on a machine
     * where Forge is running happens constantly and means almost nothing -
     * a tab that was backgrounded, a session the server forgot. A user was
     * being told the server had gone while the server was answering every
     * request the page made.
     *
     * Two things can now be true, and they get different sentences:
     *
     *   the server is not answering - an HTTP request to this extension's
     *   own routes failed. On localhost that means Forge has actually
     *   stopped, which is worth being told and which no design can hide;
     *   over a network it means the network is gone. Either way it is rare,
     *   true and actionable.
     *
     *   the framework's channel is down and the server is fine - everything
     *   that has moved off that channel keeps working, and the line SAYS
     *   WHICH parts have not. A page whose grid is live and whose composer
     *   is stale should say that, rather than claiming the whole tab is off.
     *
     * A toast is the wrong shape for either: it hides itself after a few
     * seconds and what it reports lasts until something changes.
     */
    function noticeText() {
        if (S.offline.server) {
            return "Forge is not answering. Nothing on this tab can be read or changed until it does. "
                + "Trying again by itself; this line goes when the server answers.";
        }
        return "The composer's live channel to Forge is down on this page. Browsing, paging, sorting, "
            + "selecting, sending and the queue list all still work - they do not use it. "
            + (S.offline.stale.length ? S.offline.stale.join(", ") + " will not update until it comes back. " : "")
            + "Trying again by itself; this line goes when the server answers.";
    }

    function renderNotice() {
        const container = byId(BROWSER_ID) || root();
        if (!container) { return; }
        let bar = container.querySelector(".minipaint-clip-offline");
        const show = S.offline.server || S.offline.queue;
        if (!show) {
            stopRetrying();
            if (bar) { bar.hidden = true; }
            return;
        }
        if (!bar) {
            bar = document.createElement("div");
            bar.className = "minipaint-clip-offline";
            bar.setAttribute("role", "status");
            const line = document.createElement("span");
            line.className = "minipaint-clip-offline-text";
            const button = document.createElement("button");
            button.type = "button";
            button.className = "minipaint-clip-offline-reconnect";
            button.textContent = "Check again";
            button.title = "Asks the server again, and clears this line if it answers.";
            // NEVER A RELOAD. This offered one, and on a Forge behind its own
            // TLS front end reloading took the whole WebUI page with it - the
            // session gone, for a line that is only ever advisory. A button
            // that can cost more than the problem it reports is not a repair.
            button.addEventListener("click", function () { checkConnection(); });
            bar.appendChild(line);
            bar.appendChild(button);
            container.insertBefore(bar, container.firstChild);
        }
        bar.dataset.reason = S.offline.server ? "server" : "queue";
        const line = bar.querySelector(".minipaint-clip-offline-text");
        if (line) { line.textContent = noticeText(); }
        bar.hidden = false;
        retryConnection();
    }

    /**
     * Forge itself did not answer an HTTP request to one of our own routes.
     *
     * This is the only failure that deserves to say the server is gone, and
     * it is the one the whole programme is built to make rare: every row
     * moved off the framework's channel is a row that now fails here, where
     * failure means something, instead of there, where it meant "a stream
     * closed".
     */
    function serverSilent(show, what) {
        const on = !!show;
        if (!on && !S.offline.server) { return; }
        const was = S.offline.server;
        S.offline.server = on;
        // Once per transition. A server that is down stays down, and a line
        // per retry is a log nobody reads looking for the one that matters.
        if (on && !was && what) { note("server: " + what + " could not be reached"); }
        renderNotice();
    }

    /**
     * The framework's channel is not delivering, and the server is fine.
     *
     * Kept while anything on this tab still rides it, and honest about
     * which: see ``STALE_WITHOUT_THE_CHANNEL``.
     */
    function connectionNotice(show) {
        S.offline.queue = !!show;
        renderNotice();
    }

    //: How long the page waits before trying the server again, and the
    //: ceiling it backs off to. It starts short because most of what breaks
    //: a round trip is brief, and backs off because a server that is down is
    //: down and a page that asks every five seconds for an hour is a page
    //: nobody should have to have open.
    const RETRY_FIRST_MS = 5000;
    const RETRY_LIMIT_MS = 60000;

    /**
     * Keep asking, so nobody has to press anything.
     *
     * The notice used to sit there until the user pressed Check again, which
     * is the page asking a person to do what it could do itself - and the
     * honest answer to "why can I not just have it back" was that nothing
     * was trying. Now something is: while the line is up, and only while it
     * is up, the page asks for the library again on a backing-off timer and
     * takes the line down the moment an answer arrives. The button is still
     * there for somebody who does not want to wait.
     *
     * Bounded on both sides. It runs only while the notice is showing - a
     * healthy page has no timer at all - and it does not fire while the tab
     * is in the background, where the reply would be throttled and the
     * attempt wasted; coming back on screen tries immediately, which is the
     * moment a person is most likely to be looking at it.
     */
    /** Whether the line is actually up, which is what the retry follows. */
    function noticeShowing() {
        const container = byId(BROWSER_ID) || root();
        const bar = container ? container.querySelector(".minipaint-clip-offline") : null;
        return !!(bar && !bar.hidden);
    }

    function retryConnection() {
        // ONE CYCLE AT A TIME, AND IT IS THE GUARD THAT MATTERS.
        //
        // Everything that notices the server is still silent asks for a
        // retry, and the failure a retry causes is itself one of those
        // things: the fetch fails, says so, the notice is re-rendered, and
        // the re-render asks for a retry. Guarded only by "is a timer
        // pending" - which the attempt has just zeroed - that armed a second
        // timer beside the one the attempt was about to arm, and reset the
        // backoff to five seconds while doing it. A server that stayed down
        // doubled its pending timers every round. A page asking for its
        // library hundreds of times a minute is the exact opposite of what
        // this is for, and "nothing polls" is a rule this was breaking.
        if (S.retrying) { return; }
        S.retrying = true;
        S.retryDelay = RETRY_FIRST_MS;
        armRetry();
        if (!S.retryVisibility) {
            S.retryVisibility = function () {
                if (document.visibilityState !== "visible" || !noticeShowing() || !S.retryPending) { return; }
                S.retryPending = false;
                S.retryDelay = RETRY_FIRST_MS;
                armRetry();
            };
            document.addEventListener("visibilitychange", S.retryVisibility);
        }
    }

    /** The one pending attempt. Replaces any other, never joins it. */
    function armRetry() {
        if (S.retryTimer) { clearTimeout(S.retryTimer); }
        S.retryTimer = setTimeout(retryAttempt, S.retryDelay);
    }

    /** Still nothing: wait longer, and keep waiting longer. */
    function retryAgain() {
        if (!noticeShowing()) { stopRetrying(); return; }
        S.retryDelay = Math.min(S.retryDelay * 2, RETRY_LIMIT_MS);
        armRetry();
    }

    function retryAttempt() {
        S.retryTimer = 0;
        if (!noticeShowing()) { stopRetrying(); return; }
        if (document.visibilityState === "hidden") {
            // Nothing is thrown away: coming back on screen re-arms it, and
            // the deadline was never going to be honoured here. The cycle
            // stays open, so nothing else arms a second one meanwhile.
            S.retryPending = true;
            return;
        }
        // A server that is not answering is asked over the transport this
        // tab actually uses. The route takes the line down itself when it
        // answers, so there is nothing to poll for here.
        if (S.offline.server) {
            fetchLibrary({ quiet: true }).then(function () {
                if (!S.offline.server) {
                    note("server: answered again after " + Math.round(S.retryDelay / 1000) + "s");
                    toast("Forge is answering again.");
                    return;
                }
                retryAgain();
            }, retryAgain);
            return;
        }
        const before = S.gradioSeenAt;
        pressHidden(PRESS.refresh);
        setTimeout(function () {
            if (!noticeShowing()) { stopRetrying(); return; }
            if (S.gradioSeenAt !== before) {
                S.queueDown = false;
                connectionNotice(false);
                toast("The connection is back.");
                note("connection: came back on its own after " + Math.round(S.retryDelay / 1000) + "s");
                return;
            }
            retryAgain();
        }, 3000);
    }

    function stopRetrying() {
        if (S.retryTimer) { clearTimeout(S.retryTimer); S.retryTimer = 0; }
        S.retryPending = false;
        S.retrying = false;
    }

    /**
     * Ask the server for something small, and clear the notice if it answers.
     *
     * A re-render of the grid is a whole Gradio round trip - the press goes
     * over the queue and the answer comes back through the same channel the
     * notice is about - so one arriving is the evidence the line is stale.
     */
    function checkConnection() {
        const before = S.gradioSeenAt;
        const armed = Date.now();
        // Always the cheap HTTP question first: it is the one that decides
        // which of the two sentences the line should be showing at all.
        fetchLibrary({ quiet: true });
        const pressed = pressHidden(PRESS.refresh);
        let waited = 0;
        const timer = setInterval(function () {
            waited += 500;
            if (!noticeShowing()) { clearInterval(timer); return; }
            if (S.gradioSeenAt !== before) {
                clearInterval(timer);
                connectionNotice(false);
                toast("The connection is back.");
                note("connection: the server answered; the notice is cleared");
                return;
            }
            if (waited >= 8000) {
                clearInterval(timer);
                toast("Still no answer from the server. Sending works; the rest needs it back.", true);
                // The same three facts a silent send reports. This button is
                // the one thing a user presses when the notice is up, so it
                // is the cheapest place to collect them.
                const parts = ["the refresh button " + (pressed ? "was pressed" : "is not on this page")];
                try {
                    const journal = window.minipaintNetJournal;
                    parts.push(journal ? "the host's framework: " + journal.sentence(armed)
                                       : "this browser cannot report what left it");
                } catch (e) { /* the line simply does not carry it */ }
                try {
                    const root = window.minipaintHostRoot && window.minipaintHostRoot();
                    if (root && root.known && !root.ok) { parts.push(root.note); }
                } catch (e) { /* nor this one */ }
                note("connection: still nothing after 8s - " + parts.join("; "));
            }
        }, 500);
    }

    function tabVisible() {
        const panel = byId(TAB_PANEL_ID);
        if (!panel) { return false; }
        try { return getComputedStyle(panel).display !== "none"; } catch (e) { return true; }
    }

    /* ------------------------------------------------------------------ */
    /* Selection                                                             */
    /* ------------------------------------------------------------------ */

    function items() {
        const grid = byId(GRID_ID);
        return grid ? Array.from(grid.querySelectorAll(".minipaint-clip-item")) : [];
    }

    function select(assetId, quiet) {
        S.selected = assetId || "";
        for (const item of items()) {
            const on = !!S.selected && item.dataset.asset === S.selected;
            item.classList.toggle("minipaint-clip-selected", on);
            item.setAttribute("aria-selected", on ? "true" : "false");
        }
        // Everything that acts on the selected picture is dead without one.
        // Delete is in this list rather than beside it: it deletes on the
        // press, with nothing to confirm, so "nothing is selected" has to be
        // visible before the press rather than reported after it.
        for (const id of NEEDS_SELECTION()) {
            const host = byId(id);
            const button = host ? (host.tagName === "BUTTON" ? host : host.querySelector("button")) : null;
            if (button) { button.disabled = !S.selected; }
        }
        if (!quiet) { sendInput(BOXES.selected, S.selected); }
    }

    /**
     * The grid was re-rendered: keep the selection, re-apply the size.
     *
     * A selection is made here and travels to the server as a Gradio event,
     * so the server only knows the ones the queue delivered. Every render
     * carries the server's own answer back, and this used to adopt it -
     * which means one render while the queue was not delivering took the
     * picture out from under the user. That is how a Send to menu came to
     * have every destination greyed out with a picture plainly selected on
     * the grid, and how a send already in flight lost the asset it was
     * about to fall back with.
     *
     * So the page keeps its own selection. The server's is adopted only
     * when this page has none of its own, and a selection is given up only
     * when the grid is listing pictures and the selected one is not among
     * them - never merely because a render came back empty.
     */
    function afterRender() {
        const confirmed = boxValue(BOXES.selected);
        const listed = items();
        const onGrid = function (assetId) {
            return !!assetId && listed.some(function (item) { return item.dataset.asset === assetId; });
        };
        let keep = S.selected;
        if (!keep) { keep = onGrid(confirmed) ? confirmed : ""; }
        // A selection survives paging: the selected id is browser state and
        // does not belong to a page, and the send path names a picture by
        // id. So it is given up only when the library is listing pictures,
        // this page is the one the library says holds it, and it is not
        // among them - never merely because you paged away from it.
        else if (listed.length && !onGrid(keep) && S.library.selectedPage < 0) { keep = ""; }
        select(keep, true);
        S.renderedAt = Date.now();
        if (confirmed !== keep) { sendInput(BOXES.selected, keep); }
        applyThumbnailSize(S.thumb);
        refreshBadges();
        // Once the pictures have had a frame to lay out: measured before
        // that, every tile is "off-centre" because nothing has been drawn.
        setTimeout(reportTiles, 400);
        scheduleFit();
    }

    /** A Gradio round trip landed on this page, whatever it carried. */
    function menuStateChanged() {
        S.gradioSeenAt = Date.now();
        if (S.offline.queue) { connectionNotice(false); S.queueDown = false; }
    }

    /**
     * Size the thumbnails, by writing the variable the grid actually reads.
     *
     * On the grid itself, not on the Gradio block around it. Gradio renders
     * an HTML component's elem_classes onto both the outer block and the
     * inner "prose" div, so the stylesheet's default matched twice and the
     * inner match re-declared the variable - which meant a value written to
     * the outer element was shadowed before it ever reached the grid, and
     * the slider moved nothing. The grid is the element that reads the
     * variable, so it is the element that gets written.
     */
    function applyThumbnailSize(size) {
        const host = byId(GRID_ID);
        const grid = (host && host.querySelector(".minipaint-clip-grid"))
            || app().querySelector(".minipaint-clip-grid");
        const slider = byId("minipaint_clipboard_thumb");
        const input = slider ? slider.querySelector("input[type=range]") : null;
        const value = Number(size) || (input ? Number(input.value) : 0) || DEFAULT_THUMB;
        S.thumb = value;
        // Marked important like the rest of the grid's geometry (see pin),
        // so the one variable every track and ceiling below is measured
        // from cannot be restated by a stylesheet either.
        if (grid) { grid.style.setProperty("--minipaint-clip-thumb", value + "px", "important"); }
    }

    /**
     * The slider moved: size the tiles now, remember it shortly.
     *
     * Sizing is drawing and happens on the frame the slider moved. Keeping
     * it is a setting, and goes over this tab's own route - debounced,
     * because a drag is a hundred values and one of them is the answer.
     */
    function setThumbnailSize(size) {
        applyThumbnailSize(size);
        if (S.thumbSave) { clearTimeout(S.thumbSave); }
        const wanted = S.thumb;
        S.thumbSave = setTimeout(function () {
            S.thumbSave = 0;
            postSettings({ thumbnail: wanted }).catch(function () { /* the size is still applied */ });
        }, 400);
    }

    //: The three places the gallery's 🖌️ button can send, in the menu's
    //: order, for a page whose menu state predates the third.
    const INTERCEPTS = [["minipaint", "Mini Paint"], ["clipboard", "Clipboard"], ["wangp", "WanGP"]];

    function interceptTarget() {
        const state = menuState();
        if (typeof state.intercept_target === "string" && state.intercept_target) { return state.intercept_target; }
        return state.intercept ? "clipboard" : "minipaint";
    }

    /**
     * The gallery's 🖌️ button: to Mini Paint, into Clipboard, or a WanGP
     * request. Saved over the tab's own route first, then written to the
     * hidden box so a live Gradio re-renders the menu state; the answer
     * is the status line's, and nothing waits on the box.
     */
    function setIntercept(target) {
        const wanted = String(target || "");
        if (!INTERCEPTS.some(function (pair) { return pair[0] === wanted; })) { return Promise.resolve(false); }
        return postSettings({ intercept_target: wanted }).then(function (answer) {
            setStatus((answer && answer.status) || "");
            sendInput(BOXES.intercept, wanted + ":" + Date.now());
            note("the gallery's send button now goes to " + wanted);
            return true;
        }, function () {
            setStatus("That setting could not be saved.");
            return false;
        });
    }

    /** The older two-way switch, for anything that still calls it. */
    function toggleIntercept() {
        return setIntercept(interceptTarget() === "clipboard" ? "minipaint" : "clipboard");
    }

    /**
     * Say so when a thumbnail is not drawn in the middle of its tile.
     *
     * WHAT IS MEASURED, AND WHY IT IS NOT THE OBVIOUS THING. The <img> is
     * the full width of its box, so the element's own middle is the tile's
     * middle whatever the picture inside it does - measuring the element
     * reports zero on a grid that is visibly wrong, which is how this was
     * fixed twice and reported a third time. object-fit decides where the
     * pixels go inside that element, so the drawn rectangle is worked out
     * here the way the browser works it out, and that is what is judged.
     *
     * This grid lives on a page carrying Forge's stylesheet, a theme and
     * every other installed extension, any of which can say !important about
     * an image. The rules here are written to survive that; when they do not,
     * this is the line that says so, with the number and the declaration that
     * won - which is the difference between fixing it and guessing again.
     */
    function reportTiles() {
        const tiles = items();
        if (!tiles.length) { return; }
        // The cell every tile is meant to be: the column and row tracks are
        // measured from the same variable, and a column is narrower only in
        // a panel too narrow for one whole tile.
        const grid = gridElement();
        const gridStyle = grid ? getComputedStyle(grid) : null;
        const thumb = gridStyle ? parseFloat(gridStyle.getPropertyValue("--minipaint-clip-thumb")) : 0;
        const room = grid ? grid.clientWidth - parseFloat(gridStyle.paddingLeft) - parseFloat(gridStyle.paddingRight) : 0;
        const cellWidth = thumb > 0 ? Math.min(thumb + TILE_FRAME, room > 0 ? room : Infinity) : 0;
        const cellHeight = thumb > 0 ? thumb + TILE_CAPTION : 0;
        const misfit = [];
        const adrift = [];
        for (const tile of tiles.slice(0, 24)) {
            const tileBox = tile.getBoundingClientRect();
            const tileStyle = getComputedStyle(tile);
            // A tile that is not the size of its cell. This is the fault the
            // pictures were centred three times for: the tile's own box was
            // wider than its column, and nothing measured the box.
            if (tileBox.width && tileBox.height && cellWidth
                && (Math.abs(tileBox.width - cellWidth) > 1 || Math.abs(tileBox.height - cellHeight) > 1)) {
                misfit.push({ width: Math.round(tileBox.width), height: Math.round(tileBox.height),
                              display: tileStyle.display, minWidth: tileStyle.minWidth, boxWidth: tileStyle.width });
            }
            const picture = tile.querySelector("img");
            if (!picture || !picture.complete || !picture.naturalWidth || !picture.naturalHeight) { continue; }
            const box = picture.getBoundingClientRect();
            if (!box.width || !box.height) { continue; }
            const style = getComputedStyle(picture);
            const left = tileBox.left + parseFloat(tileStyle.borderLeftWidth) + parseFloat(tileStyle.paddingLeft);
            const right = tileBox.right - parseFloat(tileStyle.borderRightWidth) - parseFloat(tileStyle.paddingRight);
            const fits = style.objectFit === "contain" || style.objectFit === "scale-down";
            const scale = fits ? Math.min(box.width / picture.naturalWidth, box.height / picture.naturalHeight) : 0;
            const drawn = scale ? picture.naturalWidth * scale : box.width;
            const position = String(style.objectPosition || "50% 50%").split(/\s+/);
            const fraction = position[0] && position[0].indexOf("%") > 0 ? parseFloat(position[0]) / 100 : 0.5;
            const offset = Math.round(box.left + (box.width - drawn) * (isNaN(fraction) ? 0.5 : fraction)
                                      + drawn / 2 - (left + right) / 2);
            if (Math.abs(offset) > 1) {
                adrift.push({ offset: offset, fit: style.objectFit, at: style.objectPosition,
                              width: style.width, margins: style.marginLeft + "/" + style.marginRight });
            }
        }
        if (!adrift.length && !misfit.length) { S.tileReport = ""; return; }
        const parts = [];
        if (misfit.length) {
            const first = misfit[0];
            parts.push(misfit.length + " of " + tiles.length + " tile(s) not the size of their cell, first "
                + first.width + "x" + first.height + " for " + Math.round(cellWidth) + "x" + Math.round(cellHeight)
                + " (display " + first.display + ", width " + first.boxWidth + ", min-width " + first.minWidth + ")");
        }
        if (adrift.length) {
            const worst = adrift.reduce(function (a, b) { return Math.abs(b.offset) > Math.abs(a.offset) ? b : a; });
            parts.push(adrift.length + " of " + tiles.length + " thumbnail(s) drawn off-centre, worst "
                + worst.offset + "px (object-fit " + worst.fit + ", object-position " + worst.at
                + ", width " + worst.width + ", margins " + worst.margins + ")");
        }
        const line = "grid: " + parts.join("; ");
        // Once per situation. A render that changes nothing must not add a
        // line, or a grid the user scrolls fills the log with one sentence.
        if (line === S.tileReport) { return; }
        S.tileReport = line;
        note(line);
    }

    /* ------------------------------------------------------------------ */
    /* The library: the grid, its pager, and the patching                    */
    /* ------------------------------------------------------------------ */

    /**
     * WHY THE BROWSER DRAWS THIS NOW.
     *
     * The grid used to be markup the server rendered and a Gradio event
     * delivered: 571 bytes a tile, 279 KiB at five hundred pictures, over a
     * transport that dies when the tab is backgrounded, when a session is
     * forgotten and when Forge restarts. The same index as JSON is 73 KiB,
     * one page of it is 9 KiB, and it comes over the plain HTTP that has
     * kept working through every failure this tab has had.
     *
     * The division is the one the whole programme follows: the server owns
     * truth - what exists, in what order - and the browser owns drawing -
     * what a tile looks like and which tiles are on screen. The browser
     * never holds an opinion the server cannot overrule, and an event is
     * advisory: it says the library moved, never what it moved to, so a page
     * that sees one re-asks and patches from the answer. There are no deltas
     * to get wrong and no way for a missed event to leave a page quietly
     * incorrect.
     */
    const LIBRARY_ROUTE = "/minipaint-clipboard/library";
    const SETTINGS_ROUTE = "/minipaint-clipboard/settings";
    //: One re-fetch when the revision moved between asking and drawing. One,
    //: because the second answer is authoritative and a third would be a
    //: poll wearing a different hat.
    const LIBRARY_RETRY_MS = 250;

    function imageUrl(id, version) {
        return "/minipaint-clipboard/image/" + encodeURIComponent(id) + "?thumb=1"
            + (version ? "&v=" + encodeURIComponent(version) : "");
    }

    function sizeText(item) {
        const bytes = Number(item.bytes || 0);
        const human = bytes >= 1048576 ? (bytes / 1048576).toFixed(1) + " MB"
            : bytes >= 1024 ? Math.round(bytes / 1024) + " KB" : bytes + " B";
        return item.w + " × " + item.h + " · " + human;
    }

    /**
     * WHY THE GEOMETRY IS WRITTEN INLINE, WITH !important.
     *
     * The stylesheet says everything below already, scoped to the tab's
     * root, and it was not enough. This grid is drawn into somebody else's
     * page - Forge's own stylesheet, its theme and every other extension's
     * - and a theme is free to say !important about an element type. The
     * Lobe theme does: `button { min-width: fit-content !important }`, about
     * every button on the page, and a tile is a button. That one declaration
     * beat the tile's `min-width: 0`, made every tile as wide as its own
     * one-line caption, and each ran out of its cell under the next one:
     * a picture centred in a box two columns wide, a selection border the
     * width of two tiles. Three earlier fixes had centred the picture inside
     * the tile, and none of them could reach the tile's own box.
     *
     * An inline declaration marked important is the strongest thing in the
     * cascade: no stylesheet on the page, whatever its specificity and
     * whatever it says, can override it. So the script that builds the grid
     * and its tiles states their geometry here - sizes, placement, the box
     * model, the fit of the picture, the shape of the caption - and the
     * stylesheet keeps what it has always kept: colours, radii, the hover
     * and selection borders, the missing-picture mark. Nothing here names a
     * colour; test_clipboard_ui.py holds that line.
     *
     * The thumbnail size stays the one variable the slider writes onto the
     * grid (applyThumbnailSize); every other length below is a number this
     * file owns. reportTiles says so, with the values that won, if a tile is
     * ever not the size of its cell again.
     */
    function pin(element, declarations) {
        for (const property in declarations) {
            if (Object.prototype.hasOwnProperty.call(declarations, property)) {
                element.style.setProperty(property, declarations[property], "important");
            }
        }
    }

    //: The mount: the grid over its pager, laid out together.
    const MOUNT_GEOMETRY = {
        "display": "flex",
        "flex-direction": "column",
        "gap": "var(--minipaint-clip-gap, 8px)",
        "box-sizing": "border-box",
        "width": "auto",
        "min-width": "0",
        "max-width": "100%"
    };

    //: The grid: columns the size the slider names, never stretched to fill
    //: the row (a stretched column is a box wider than the picture in it),
    //: and rows of that size plus the caption, so every cell is identical
    //: before any picture has loaded. min() keeps a single column from
    //: overflowing a panel narrower than one tile.
    const GRID_GEOMETRY = {
        "display": "grid",
        "grid-template-columns": "repeat(auto-fill, min(calc(var(--minipaint-clip-thumb) + " + TILE_FRAME + "px), 100%))",
        "grid-auto-rows": "calc(var(--minipaint-clip-thumb) + " + TILE_CAPTION + "px)",
        "grid-auto-flow": "row",
        "justify-content": "start",
        "align-content": "start",
        "justify-items": "stretch",
        "align-items": "stretch",
        "gap": "var(--minipaint-clip-gap, 8px)",
        "padding": "var(--minipaint-clip-gap, 8px)",
        "margin": "0",
        "box-sizing": "border-box",
        "align-self": "stretch",
        "width": "auto",
        "min-width": "0",
        "max-width": "100%",
        "overflow-x": "hidden",
        "overflow-y": "auto"
    };

    //: The tile is the boundary: exactly its cell, one cell, and nothing it
    //: holds is drawn outside it. It centres what it holds; the two
    //: full-width children opt back out below.
    const TILE_GEOMETRY = {
        "display": "flex",
        "flex-direction": "column",
        "flex-wrap": "nowrap",
        "align-items": "center",
        "justify-content": "flex-start",
        "gap": "4px",
        "box-sizing": "border-box",
        "grid-column": "auto",
        "grid-row": "auto",
        "justify-self": "stretch",
        "align-self": "stretch",
        "width": "100%",
        "min-width": "0",
        "max-width": "100%",
        "height": "100%",
        "min-height": "0",
        "max-height": "100%",
        "margin": "0",
        "padding": "4px",
        "border-width": "2px",
        "border-style": "solid",
        "overflow": "hidden",
        "float": "none",
        "transform": "none",
        "aspect-ratio": "auto"
    };

    //: The square takes the room the caption leaves. Its height is what the
    //: picture's height: 100% resolves against, so it has to be definite:
    //: flex, inside a tile whose height the row has fixed.
    const THUMB_GEOMETRY = {
        "display": "flex",
        "align-items": "center",
        "justify-content": "center",
        "flex": "1 1 auto",
        "align-self": "stretch",
        "box-sizing": "border-box",
        "width": "100%",
        "min-width": "0",
        "max-width": "100%",
        "min-height": "0",
        "margin": "0",
        "padding": "0",
        "border-width": "0",
        "overflow": "hidden",
        "text-align": "center",
        "float": "none",
        "aspect-ratio": "auto"
    };

    //: The picture fills the square and is placed in it by object-fit alone,
    //: with a second ceiling stated in pixels for a square that has no
    //: height to give. A margin from anywhere cannot push it to one side,
    //: because there is no free space for a margin to take.
    const PICTURE_GEOMETRY = {
        "display": "block",
        "box-sizing": "border-box",
        "flex": "0 1 auto",
        "align-self": "center",
        "width": "100%",
        "min-width": "0",
        "max-width": "100%",
        "height": "100%",
        "min-height": "0",
        "max-height": "var(--minipaint-clip-thumb)",
        "object-fit": "contain",
        "object-position": "50% 50%",
        "margin": "0",
        "padding": "0",
        "border-width": "0",
        "position": "static",
        "float": "none",
        "transform": "none",
        "aspect-ratio": "auto"
    };

    //: The caption is the tile's width, not its own: one line, clipped with
    //: an ellipsis. Its type size is stated because the row's height was
    //: worked out from it; a theme's larger button text would otherwise take
    //: the difference out of the square.
    const NAME_GEOMETRY = {
        "display": "block",
        "flex": "0 0 auto",
        "align-self": "stretch",
        "box-sizing": "border-box",
        "width": "100%",
        "min-width": "0",
        "max-width": "100%",
        "height": "auto",
        "max-height": "none",
        "margin": "0",
        "padding": "0",
        "border-width": "0",
        "font-size": "12px",
        "line-height": "1.3",
        "white-space": "nowrap",
        "overflow": "hidden",
        "text-overflow": "ellipsis",
        "text-align": "left"
    };

    /** Where the browser draws, inside the block the server used to fill. */
    function gridMount() {
        const host = byId(GRID_ID);
        if (!host) { return null; }
        let mount = host.querySelector(".minipaint-clip-mount");
        if (mount) { return mount; }
        mount = document.createElement("div");
        mount.className = "minipaint-clip-mount";
        pin(mount, MOUNT_GEOMETRY);
        const grid = document.createElement("div");
        grid.className = "minipaint-clip-grid";
        grid.setAttribute("role", "listbox");
        grid.setAttribute("aria-label", "Clipboard images");
        grid.setAttribute("tabindex", "0");
        grid.dataset.count = "0";
        pin(grid, GRID_GEOMETRY);
        const pager = document.createElement("div");
        pager.className = "minipaint-clip-pager";
        pager.hidden = true;
        mount.appendChild(grid);
        mount.appendChild(pager);
        host.appendChild(mount);
        // The size the slider holds, on the grid before its first tile: the
        // tracks above are measured from it, and a page whose stylesheet
        // never arrived would otherwise have nothing to measure them from.
        applyThumbnailSize(S.thumb);
        return mount;
    }

    function gridElement() {
        const mount = gridMount();
        return mount ? mount.querySelector(".minipaint-clip-grid") : null;
    }

    /* ------------------------------------------------------------------ */
    /* Filling the window                                                   */
    /* ------------------------------------------------------------------ */
    //
    // The grid used to be capped at 70vh by the stylesheet and the composer
    // was as tall as its content, so on a tall window both columns stopped
    // two thirds of the way down and left the rest of it empty - more so in
    // focus mode, where the window is all the tab has.
    //
    // So both are fitted to the window, by measuring rather than by a
    // percentage: a percentage height inside a Gradio container resolves to
    // auto (see CLAUDE.md), and the one number that is right is "from here
    // to the bottom of the window, less what has to fit under it". The grid
    // gets that as its height; the Prompt box gets whatever the composer
    // leaves over, as a minimum height - its own height is Gradio's, which
    // grows it with what is typed, and a minimum is the one thing Gradio's
    // autosizing never writes.
    //
    // It is measured as though the page were scrolled to the top, so a fit
    // taken half way down the page is the fit the top would get. It observes
    // only boxes it never resizes - the rows above and below the grid, and
    // the composer's other blocks - and writes only when a number changed,
    // which is what keeps it out of the loop the WanGP frame once fell into.
    //
    // Stacked on a narrow window, the composer is under the grid rather than
    // beside it, and "to the bottom of the window" would mean a grid that
    // pushed the composer off the screen: the grid keeps the old cap and the
    // Prompt box its own size.

    //: The least the grid is given, whatever the window. Below it the page
    //: scrolls, as it always did.
    const FIT_FLOOR = 240;
    //: The Prompt box's own floor, the stylesheet's `min-height`.
    const PROMPT_FLOOR = 96;
    //: Breathing room under both columns.
    const FIT_GAP = 12;
    //: The share of the window the grid keeps when the columns are stacked.
    const STACKED_SHARE = 0.7;

    /** How far everything above this node has been scrolled. */
    function scrolledAbove(node) {
        let total = window.scrollY || window.pageYOffset || 0;
        for (let walk = node.parentElement; walk && walk !== document.documentElement; walk = walk.parentElement) {
            total += walk.scrollTop || 0;
        }
        return total;
    }

    function promptBox() {
        const host = byId(PROMPT_ID);
        return host ? host.querySelector("textarea") : null;
    }

    /** Write a pixel size, pinned, only if it is not already that. */
    function writeSize(node, properties, value) {
        const text = value === null ? "" : Math.round(value) + "px";
        let changed = false;
        for (const property of properties) {
            if (node.style.getPropertyValue(property) === text) { continue; }
            changed = true;
            if (text) { node.style.setProperty(property, text, "important"); }
            else { node.style.removeProperty(property); }
        }
        return changed;
    }

    function fitTab() {
        S.fitFrame = 0;
        if (!tabVisible()) { return null; }
        const grid = gridElement();
        const column = byId(BROWSER_ID);
        const composer = byId(COMPOSER_ID);
        if (!grid || !column) { return null; }
        const view = window.innerHeight || document.documentElement.clientHeight || 0;
        if (!view) { return null; }
        const g = grid.getBoundingClientRect();
        const c = column.getBoundingClientRect();
        const k = composer ? composer.getBoundingClientRect() : null;
        // Beside or under: the composer starts at or below the bottom of the
        // browser column when the row has wrapped.
        const stacked = !!k && k.top >= c.bottom - 1;
        const top = g.top + scrolledAbove(grid);
        const below = Math.max(0, c.bottom - g.bottom);
        let height = view - top - below - FIT_GAP;
        if (stacked) { height = Math.min(height, view * STACKED_SHARE); }
        height = Math.max(FIT_FLOOR, height);
        const result = { grid: Math.round(height), prompt: null, stacked: stacked };
        writeSize(grid, ["height", "max-height"], height);
        const box = promptBox();
        if (box && k && !stacked) {
            const bottom = k.bottom + scrolledAbove(composer);
            const current = box.getBoundingClientRect().height;
            const wanted = Math.max(PROMPT_FLOOR, current + (view - bottom - FIT_GAP));
            result.prompt = Math.round(wanted);
            writeSize(box, ["min-height"], wanted);
        } else if (box) {
            writeSize(box, ["min-height"], null);
        }
        S.fitted = result;
        return result;
    }

    function scheduleFit() {
        if (S.fitFrame) { return; }
        S.fitFrame = window.requestAnimationFrame(fitTab);
    }

    /** What moves the room there is, watched - and nothing that is resized. */
    function watchFit() {
        if (S.fitWatch) { return; }
        S.fitWatch = true;
        window.addEventListener("resize", scheduleFit);
        if (window.visualViewport) { window.visualViewport.addEventListener("resize", scheduleFit); }
        // Focus mode, from either extension, is a class on the body.
        if (typeof MutationObserver === "function" && document.body) {
            new MutationObserver(scheduleFit).observe(document.body, { attributes: true, attributeFilter: ["class"] });
        }
        if (typeof ResizeObserver !== "function") { return; }
        const sizes = new ResizeObserver(scheduleFit);
        S.fitObserver = sizes;
        const watchAll = function () {
            sizes.disconnect();
            const column = byId(BROWSER_ID);
            const composer = byId(COMPOSER_ID);
            const promptHost = byId(PROMPT_ID);
            // The browser column's rows other than the grid's own host: the
            // toolbar above it and the status line under it.
            if (column) {
                for (const child of Array.from(column.children)) {
                    if (!child.contains(gridElement())) { sizes.observe(child); }
                }
            }
            // The pager, which is inside the grid's host but never sized.
            const pager = pagerElement();
            if (pager) { sizes.observe(pager); }
            // Every block of the composer but the Prompt's own.
            if (composer) {
                for (const child of Array.from(composer.children)) {
                    if (!promptHost || !child.contains(promptHost)) { sizes.observe(child); }
                }
            }
        };
        watchAll();
        S.fitRewatch = watchAll;
    }

    function pagerElement() {
        const mount = gridMount();
        return mount ? mount.querySelector(".minipaint-clip-pager") : null;
    }

    /** One tile. Built once per picture and then reused - see drawLibrary. */
    function buildTile(item) {
        const tile = document.createElement("button");
        tile.type = "button";
        tile.className = "minipaint-clip-item";
        tile.setAttribute("role", "option");
        tile.setAttribute("aria-selected", "false");
        tile.dataset.asset = item.id;
        tile.dataset.name = item.name;
        tile.dataset.v = item.v || "";
        tile.title = item.name + " · " + sizeText(item);
        pin(tile, TILE_GEOMETRY);
        const thumb = document.createElement("span");
        thumb.className = "minipaint-clip-thumb";
        pin(thumb, THUMB_GEOMETRY);
        const picture = document.createElement("img");
        picture.alt = "";
        picture.loading = "lazy";
        picture.draggable = false;
        pin(picture, PICTURE_GEOMETRY);
        // A thumbnail that 404s costs that tile its picture and the page
        // nothing else: the name and a missing mark, where the picture was.
        // The picture is hidden inline, because its display is stated
        // inline and the stylesheet's rule for a missing one would lose.
        picture.addEventListener("error", function () {
            tile.classList.add("minipaint-clip-item-missing");
            pin(picture, { "display": "none" });
        });
        picture.src = imageUrl(item.id, item.v);
        thumb.appendChild(picture);
        const label = document.createElement("span");
        label.className = "minipaint-clip-name";
        label.textContent = item.name;
        pin(label, NAME_GEOMETRY);
        tile.appendChild(thumb);
        tile.appendChild(label);
        return tile;
    }

    /**
     * The same tile, told what changed about its picture.
     *
     * A rename changes the caption and not the file, so mtime is unchanged,
     * the URL is unchanged, and nothing is re-fetched - the asked-for
     * behaviour falling out of the versioned URL rather than being
     * special-cased. Only a genuinely new version costs a request.
     */
    function refreshTile(tile, item) {
        if (tile.dataset.name !== item.name) {
            tile.dataset.name = item.name;
            tile.title = item.name + " · " + sizeText(item);
            const label = tile.querySelector(".minipaint-clip-name");
            if (label) { label.textContent = item.name; }
        }
        if (String(tile.dataset.v || "") !== String(item.v || "")) {
            tile.dataset.v = item.v || "";
            tile.classList.remove("minipaint-clip-item-missing");
            const picture = tile.querySelector("img");
            if (picture) {
                // Shown again, in case the version before was the one that
                // 404ed and hid it.
                pin(picture, { "display": PICTURE_GEOMETRY.display });
                picture.src = imageUrl(item.id, item.v);
            }
        }
        return tile;
    }

    /**
     * Put this page of the library on screen, keeping every tile it can.
     *
     * Building a page and patching one are the same operation against the
     * same page, so there is one code path that puts tiles on screen and no
     * second one to drift. An import that lands on your page adds one node;
     * a delete removes one; a rename changes one caption. The other
     * fifty-nine are untouched, the scroll position is untouched, and
     * nothing is decoded twice.
     *
     * Re-ordering MOVES existing nodes: insertBefore on an element already
     * in the document relocates it rather than recreating it, and the loop
     * only touches the ones that are actually out of place.
     */
    function drawLibrary(answer) {
        const grid = gridElement();
        if (!grid) { return; }
        const items = Array.isArray(answer.items) ? answer.items : [];
        const next = new Map();
        for (const item of items) {
            const held = S.library.tiles.get(item.id);
            next.set(item.id, held ? refreshTile(held, item) : buildTile(item));
        }
        for (const entry of S.library.tiles) {
            if (!next.has(entry[0]) && entry[1].parentNode) { entry[1].parentNode.removeChild(entry[1]); }
        }
        let cursor = grid.firstElementChild;
        for (const item of items) {
            const tile = next.get(item.id);
            if (cursor === tile) { cursor = tile.nextElementSibling; continue; }
            grid.insertBefore(tile, cursor);
        }
        // Anything the grid still holds that this page does not name: a tile
        // from a render the server made before the browser took over.
        while (cursor) {
            const following = cursor.nextElementSibling;
            if (!next.has(cursor.dataset ? cursor.dataset.asset : "")) { grid.removeChild(cursor); }
            cursor = following;
        }
        S.library.tiles = next;
        S.library.ids = items.map(function (item) { return item.id; });
        S.library.revision = String(answer.revision || "");
        S.library.sort = String(answer.sort || S.library.sort);
        S.library.page = Number(answer.page || 0);
        S.library.pages = Math.max(1, Number(answer.pages || 1));
        S.library.total = Number(answer.total || 0);
        S.library.size = Number(answer.size || S.library.size);
        S.library.configured = answer.configured !== false;
        S.library.selectedPage = Number(answer.selected_page);
        S.library.wanted = S.library.page;
        grid.dataset.count = String(items.length);
        grid.classList.toggle("minipaint-clip-empty", !items.length);
        emptyNotice(grid, items.length ? "" : String(answer.reason || "empty"));
        drawPager();
        // The selection is the browser's and does not belong to a page: it
        // survives paging because the send path names a picture by id, and it
        // gives way to truth only when the library says the picture is not in
        // it at all. That rule, the size and the badges are all one step.
        afterRender();
    }

    /**
     * The empty and unconfigured states, in the words they have always had.
     *
     * They are states of the INDEX, not of the transport: the route answers
     * them with a total of zero and a reason, and the sentence is the
     * browser's because the browser is what draws this now.
     */
    function emptyNotice(grid, reason) {
        const existing = grid.querySelector(".minipaint-clip-nothing");
        if (!reason) {
            if (existing) { grid.removeChild(existing); }
            return;
        }
        const note = existing || document.createElement("p");
        note.className = "minipaint-clip-nothing";
        note.innerHTML = reason === "unconfigured"
            ? "<b>No storage folder yet.</b> Menu → <em>Choose storage folder</em> picks a folder on the machine "
              + "running Forge; Clipboard keeps its pictures there."
            : "No images yet. Upload or paste one from the menu, send one from Mini Paint, or point "
              + "<em>Intercept Options</em> at Clipboard and press 🖌️ under a result.";
        if (!existing) { grid.appendChild(note); }
    }

    /**
     * One row under the grid: Back, the page, Next, and the whole count.
     *
     * The ends are DISABLED rather than hidden, so the row does not change
     * width as you move through it; the number box is committed on Enter or
     * blur and clamped rather than refused; the count is the whole library,
     * because that is the number a person wants and it is free to say. The
     * row is hidden entirely when there is one page.
     */
    function drawPager() {
        const pager = pagerElement();
        if (!pager) { return; }
        const many = S.library.pages > 1;
        pager.hidden = !many && S.library.total <= S.library.size;
        // Hidden, not emptied. The controls are built once and then only
        // updated, so throwing them away here would leave the row built but
        // blank the next time a library grew past one page - a pager with no
        // buttons in it, which is worse than no pager at all.
        if (pager.hidden) { return; }
        if (!pager.dataset.built) {
            pager.dataset.built = "1";
            pager.innerHTML = "";
            const back = document.createElement("button");
            back.type = "button";
            back.className = "minipaint-clip-pager-back";
            back.textContent = "‹ Back";
            back.addEventListener("click", function () { stepPage(-1); });
            const label = document.createElement("span");
            label.className = "minipaint-clip-pager-label";
            const lead = document.createElement("span");
            lead.textContent = "Page ";
            const box = document.createElement("input");
            box.type = "number";
            box.className = "minipaint-clip-pager-number";
            box.min = "1";
            box.setAttribute("aria-label", "Page number");
            const commit = function () { goToPage(Number(box.value) - 1); };
            box.addEventListener("change", commit);
            box.addEventListener("blur", commit);
            box.addEventListener("keydown", function (event) {
                if (event.key === "Enter") { event.preventDefault(); commit(); }
            });
            const of = document.createElement("span");
            of.className = "minipaint-clip-pager-of";
            label.appendChild(lead);
            label.appendChild(box);
            label.appendChild(of);
            const next = document.createElement("button");
            next.type = "button";
            next.className = "minipaint-clip-pager-next";
            next.textContent = "Next ›";
            next.addEventListener("click", function () { stepPage(1); });
            const count = document.createElement("span");
            count.className = "minipaint-clip-pager-count";
            const mark = document.createElement("button");
            mark.type = "button";
            mark.className = "minipaint-clip-pager-mark";
            mark.hidden = true;
            mark.addEventListener("click", function () { goToPage(S.library.selectedPage); });
            pager.appendChild(back);
            pager.appendChild(label);
            pager.appendChild(next);
            pager.appendChild(mark);
            pager.appendChild(count);
        }
        const box = pager.querySelector(".minipaint-clip-pager-number");
        if (box && document.activeElement !== box) { box.value = String(S.library.page + 1); }
        if (box) { box.max = String(S.library.pages); }
        const of = pager.querySelector(".minipaint-clip-pager-of");
        if (of) { of.textContent = " of " + S.library.pages; }
        const back = pager.querySelector(".minipaint-clip-pager-back");
        if (back) { back.disabled = S.library.page <= 0; }
        const next = pager.querySelector(".minipaint-clip-pager-next");
        if (next) { next.disabled = S.library.page >= S.library.pages - 1; }
        const count = pager.querySelector(".minipaint-clip-pager-count");
        if (count) {
            count.textContent = S.library.total + " picture" + (S.library.total === 1 ? "" : "s");
        }
        // Where the selection is, when it is not here. The selected id is
        // browser state and does not belong to a page; this is the one thing
        // the pager owes a user who has paged away from it.
        const mark = pager.querySelector(".minipaint-clip-pager-mark");
        if (mark) {
            const elsewhere = S.selected && S.library.selectedPage >= 0 && S.library.selectedPage !== S.library.page;
            mark.hidden = !elsewhere;
            if (elsewhere) { mark.textContent = "selection on page " + (S.library.selectedPage + 1); }
        }
    }

    /**
     * Move by a page, from the page that was ASKED FOR rather than the one on
     * screen.
     *
     * Two presses of Next in quick succession are two pages, not one: the
     * second arrives before the first answer has landed, and computing from
     * what is drawn would make it a no-op. The pager is a control, and a
     * control that ignores a press because the network was slow is a control
     * people press twice more.
     */
    function stepPage(delta) {
        const base = S.library.wanted === undefined ? S.library.page : S.library.wanted;
        goToPage(base + delta);
    }

    function goToPage(number) {
        const wanted = Math.max(0, Math.min(S.library.pages - 1, Number(number) || 0));
        if (wanted === S.library.page && wanted === S.library.wanted) { drawPager(); return; }
        S.library.wanted = wanted;
        fetchLibrary({ page: wanted });
    }

    /** Busy is a state of the grid, not a reason to empty it. */
    function setBusy(on) {
        const grid = gridElement();
        if (grid) { grid.setAttribute("aria-busy", on ? "true" : "false"); }
        S.library.busy = !!on;
    }

    /**
     * Ask the server for one page of the index, and draw what it answers.
     *
     * Nothing on screen moves until the answer arrives. That is the whole of
     * "sorting is separate from drawing": a sort that cannot be fetched
     * leaves the grid exactly as it was AND SAYS SO, where a failed round
     * trip used to leave it stale with nothing to indicate the sort did not
     * take.
     */
    function fetchLibrary(options) {
        const opts = options || {};
        const page = opts.page === undefined ? S.library.page : Number(opts.page);
        const sort = opts.sort === undefined ? S.library.sort : String(opts.sort || "");
        // The page size is one constant and the route takes it, so it stays
        // a decision rather than becoming a migration. The answer says what
        // the server clamped it to, and that is what the pager counts in.
        if (opts.size !== undefined) { S.library.size = Number(opts.size) || S.library.size; }
        const url = LIBRARY_ROUTE + "?page=" + encodeURIComponent(page)
            + "&size=" + encodeURIComponent(S.library.size)
            + (sort ? "&sort=" + encodeURIComponent(sort) : "")
            + (opts.refresh ? "&refresh=1" : "")
            + (S.selected ? "&selected=" + encodeURIComponent(S.selected) : "");
        setBusy(true);
        // Only the newest answer draws. Two presses in flight at once can
        // come back in either order, and the page the user last asked for is
        // the page they get - never whichever request the network finished.
        const ticket = ++S.library.ticket;
        const request = fetch(url, { credentials: "same-origin", cache: "no-store" })
            .then(function (response) {
                if (!response.ok) { throw new Error("HTTP " + response.status); }
                return response.json();
            })
            .then(function (answer) {
                if (!answer || answer.ok !== true) { throw new Error((answer && answer.message) || "the library could not be read"); }
                serverSilent(false);
                if (ticket !== S.library.ticket) { return answer; }
                setBusy(false);
                S.library.inflight = null;
                drawLibrary(answer);
                if (!opts.quiet) { setStatus(answer.status || ""); }
                // The revision moved between asking and drawing: draw, then
                // re-ask once. Nine kilobytes, and the answer is
                // authoritative - which is cheaper than being wrong.
                if (S.library.pending) {
                    S.library.pending = false;
                    setTimeout(function () { fetchLibrary({ quiet: true }); }, LIBRARY_RETRY_MS);
                }
                return answer;
            }, function (error) {
                // A superseded request's failure is not news: the page that
                // replaced it is the one being waited on, and its own
                // handler will say whether the server answered.
                if (ticket !== S.library.ticket) { return null; }
                setBusy(false);
                S.library.inflight = null;
                // Whatever asked for a re-read while this was in flight is
                // answered by the retry the notice arms, not by a flag held
                // over a fetch that never landed.
                S.library.pending = false;
                // The tiles it has are kept: they were right when they were
                // drawn and nothing has said otherwise.
                note("library: could not be re-read (" + ((error && error.message) || error) + ")");
                setStatus("The library could not be re-read. Trying again.");
                serverSilent(true, "the library");
                return null;
            });
        S.library.inflight = request;
        return request;
    }

    /**
     * A LIBRARY event: re-ask for the page being shown and patch from it.
     *
     * The event carries a revision and a total and no contents, so this can
     * neither apply a delta wrongly nor be made stale by one it missed. A
     * page that was asleep resyncs through the same call.
     *
     * A new picture does not move you. An import while you are on page 3
     * updates the count and the page total; being relocated mid-task because
     * a background job finished is the kind of helpfulness nobody wants, so
     * the page you are on is the page you keep.
     */
    function onLibraryEvent(payload) {
        const revision = String((payload && payload.revision) || "");
        if (revision && revision === S.library.revision) { return; }
        if (S.library.inflight) { S.library.pending = true; return; }
        fetchLibrary({ quiet: true });
    }

    /** The sort, over HTTP: remembered first, then drawn when it arrives. */
    function setSort(mode) {
        const wanted = String(mode || "");
        if (!wanted || wanted === S.library.sort) { return Promise.resolve(false); }
        setBusy(true);
        return postSettings({ sort: wanted }).then(function (answer) {
            // Page 0 of the new sort. Not the page you were on: a position
            // in one order means nothing in another.
            return fetchLibrary({ sort: (answer && answer.sort) || wanted, page: 0 });
        }, function () {
            setBusy(false);
            setStatus("The sort could not be changed; the grid is as it was.");
            return null;
        });
    }

    /** What the menu remembers between sessions, over this tab's own route. */
    function postSettings(changes) {
        return fetch(SETTINGS_ROUTE, {
            method: "POST",
            credentials: "same-origin",
            cache: "no-store",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(changes || {})
        }).then(function (response) {
            if (!response.ok) { throw new Error("HTTP " + response.status); }
            return response.json();
        }).then(function (answer) {
            if (!answer || answer.ok !== true) { throw new Error((answer && answer.message) || "the setting was not saved"); }
            S.menuOverride = answer.menu || null;
            serverSilent(false);
            return answer;
        });
    }

    /**
     * The status line, which is the answer to what you just did.
     *
     * IT LOOKS LIKE A THING THE SERVER TELLS YOU. It is not: it is the
     * response to an action, and nothing else ever writes it. So every
     * answer carries its own sentence and the browser draws the line - and
     * during the move, where some actions still cross the framework and some
     * do not, both write the SAME element, which is why this looks for the
     * exact node the framework renders into rather than the block around it.
     * Writing the block's HTML detaches the node the framework holds, and
     * then its next answer lands somewhere nobody can see - which is the
     * failure this whole tab is named for, reintroduced by the fix for it.
     */
    function statusTarget(host) {
        return host.querySelector("span.md") || host.querySelector(".prose") || host;
    }

    function setStatus(text) {
        const host = byId(STATUS_ID);
        if (!host || !text) { return; }
        statusTarget(host).innerHTML = String(text);
    }

    /** PageUp / PageDown move a page while the grid has focus. A listbox is
     * expected to do this, and this grid says it is one. */
    function onLibraryKey(event) {
        if (event.key !== "PageUp" && event.key !== "PageDown") { return; }
        const grid = gridElement();
        if (!grid || !grid.contains(event.target)) { return; }
        if (S.library.pages <= 1) { return; }
        event.preventDefault();
        stepPage(event.key === "PageDown" ? 1 : -1);
    }

    /* ------------------------------------------------------------------ */
    /* The system prompt editor, full window                                 */
    /* ------------------------------------------------------------------ */

    /**
     * A system prompt is a page of prose, and this column is a third of the
     * window wide.
     *
     * So the panel gets a second shape rather than a second home: one class,
     * and the same components fill the window with the text box taking
     * nearly all of it. Nothing is duplicated, which is the whole reason it
     * is done this way - a second editor beside the first is two sets of
     * controls that drift, and this tab has spent enough builds on doors
     * that disagree with each other.
     */
    //: The custom property the stylesheet reads for where a full-window panel
    //: starts. Set on the root element rather than on a panel, because both
    //: panels read it and a theme that moves its header moves both at once.
    const TOP_INSET_PROPERTY = "--minipaint-clip-top";

    /**
     * How much of the top of the window the WebUI's own header is using.
     *
     * MEASURED, BECAUSE THE HEADER BELONGS TO A THEME THIS DOES NOT SHIP.
     *
     * A full-window panel used to start at the top of the viewport, which is
     * above whatever the theme has pinned there: with the Lobe theme that is
     * the tab bar, so a video played from View Outputs covered the way out of
     * the tab. A selector for one theme's markup would be a selector that is
     * wrong for the next one, and this extension runs under several.
     *
     * What is true of a header whatever draws it is that it is pinned to the
     * top of the window. So that is what is asked: the elements really at the
     * top edge, of which the pinned ones are asked how far down they reach.
     * Nothing pinned there means nothing to avoid, and the answer is 0.
     *
     * Capped at a third of the window, because a full-height fixed sidebar is
     * also pinned and also crosses the top edge, and treating one as a header
     * would leave the panel nowhere to be.
     */
    function headerInset() {
        if (typeof document.elementsFromPoint !== "function") { return 0; }
        let found;
        try {
            found = document.elementsFromPoint(Math.round(window.innerWidth / 2), 1) || [];
        } catch (e) { return 0; }
        const limit = Math.round(window.innerHeight / 3);
        let bottom = 0;
        for (let i = 0; i < found.length; i += 1) {
            const node = found[i];
            if (!node || node === document.body || node === document.documentElement) { continue; }
            // A panel of ours is pinned to the top as well. Measuring one of
            // those would push the next one that opened further down, and the
            // one after that further still.
            if (node.closest && node.closest("." + EDITOR_OPEN_CLASS)) { continue; }
            let style;
            try { style = window.getComputedStyle(node); } catch (e) { continue; }
            if (!style || (style.position !== "fixed" && style.position !== "sticky")) { continue; }
            const box = node.getBoundingClientRect();
            if (box.top <= 1 && box.bottom > bottom && box.bottom <= limit) { bottom = box.bottom; }
        }
        return Math.max(0, Math.round(bottom));
    }

    /** Publish the inset for the stylesheet. Never fatal: a panel that cannot
     *  be told where the header is still opens, at the top of the window. */
    function applyHeaderInset() {
        try {
            document.documentElement.style.setProperty(TOP_INSET_PROPERTY, headerInset() + "px");
        } catch (e) { /* a page that will not take a custom property still opens */ }
    }

    /** Re-measure while a panel is open: a header's height is a function of
     *  the window's width on every theme that wraps its tabs. */
    function watchHeaderInset() {
        if (S.headerWatch) { return; }
        S.headerWatch = function () {
            if (outputsOpen() || editorOpen()) { applyHeaderInset(); }
        };
        window.addEventListener("resize", S.headerWatch);
    }

    function editorPanel() { return byId(ENHANCE_PANEL_ID); }

    function editorOpen() {
        const panel = editorPanel();
        return !!(panel && panel.classList.contains(EDITOR_OPEN_CLASS));
    }

    /** Mark, or unmark, the wrappers between the panel and the text box. */
    function markEditorChain(panel, on) {
        const box = panel.querySelector(".minipaint-clip-system-prompt textarea");
        if (!box) { return; }
        let node = box.parentElement;
        while (node && node !== panel) {
            node.classList.toggle(EDITOR_GROW_CLASS, !!on);
            node = node.parentElement;
        }
    }

    function openPromptEditor() {
        const panel = editorPanel();
        if (!panel) { return false; }
        // The panel has no shape on the tab at all - the stylesheet gives it
        // none until this class is on it. There is nothing to expand and
        // nothing left behind in the composer when it goes away again.
        // Before the class, so the measurement does not find this panel.
        applyHeaderInset();
        watchHeaderInset();
        panel.classList.add(EDITOR_OPEN_CLASS);
        markEditorChain(panel, true);
        if (!S.editorKey) {
            // Captured, so Escape closes the editor rather than whatever
            // else on the page is listening for it.
            S.editorKey = function (event) {
                if (event.key !== "Escape" || !editorOpen()) { return; }
                event.stopPropagation();
                event.preventDefault();
                closePromptEditor();
            };
            document.addEventListener("keydown", S.editorKey, true);
        }
        // The box is what the view is for, so it is where the cursor goes -
        // after a frame, because the panel has just changed shape.
        const box = panel.querySelector(".minipaint-clip-system-prompt textarea");
        if (box) { setTimeout(function () { try { box.focus(); } catch (e) { /* not worth an exception */ } }, 60); }
        note("system prompts: opened full screen");
        return true;
    }

    function closePromptEditor() {
        const panel = editorPanel();
        if (panel) {
            panel.classList.remove(EDITOR_OPEN_CLASS);
            markEditorChain(panel, false);
        }
        if (S.editorKey) { document.removeEventListener("keydown", S.editorKey, true); S.editorKey = null; }
        return true;
    }


    /* ------------------------------------------------------------------ */
    /* View Outputs: what WanGP made, as a gallery                          */
    /* ------------------------------------------------------------------ */

    const OUTPUTS_PANEL_ID = "minipaint_clipboard_outputs_panel";
    const OUTPUTS_ROUTE = "/minipaint-clipboard/outputs";

    function outputsPanel() { return byId(OUTPUTS_PANEL_ID); }

    function outputsOpen() {
        const panel = outputsPanel();
        return !!(panel && panel.classList.contains(EDITOR_OPEN_CLASS));
    }

    /**
     * Stop the player and let go of the file.
     *
     * Clearing ``src`` is not tidiness: a <video> left with a source goes on
     * downloading it after the view is closed, and these are the largest
     * files this extension ever touches.
     */
    function stopPlayer() {
        const video = S.outputs.player;
        stopClock();
        leaveFullscreen();
        if (!video) { return; }
        try {
            video.pause();
            video.removeAttribute("src");
            video.load();
        } catch (e) { /* a player the page is already tearing down */ }
    }

    /* ------------------------------------------------------------------ */
    /* The player: controls on the page, not the browser's                  */
    /* ------------------------------------------------------------------ */
    //
    // The browser's own controls were the only way to drive a video here,
    // and they are a different set on every browser: Android's have no loop
    // at all, so a clip three seconds long could not be watched twice without
    // pressing play again. These are the page's own, the same everywhere,
    // every one a 44px target: play, a timeline to scrub, a frame back and a
    // frame forward (a generated clip is looked at a frame at a time), loop,
    // mute, speed and full screen.
    //
    // Loop, mute and speed are remembered, because they are preferences about
    // watching, not about one video.
    //
    // WHAT IT COSTS WHILE A VIDEO PLAYS. One callback per presented frame -
    // `requestVideoFrameCallback` where the browser has it, one animation
    // frame otherwise - that writes a number into the timeline and, when the
    // tenth of a second changes, two short strings. Nothing reads layout.
    // It runs only while the video is playing and the view is open, and a
    // paused video costs nothing at all.

    //: Where the three preferences live. Per browser, like the rest of the
    //: tab's view state.
    const PLAYER_PREFS = "minipaint-outputs-player:v1";
    const PLAYER_RATES = [0.25, 0.5, 1, 1.5, 2];
    //: A frame when the video has not said how long its frames are.
    const FRAME_FALLBACK = 1 / 24;

    function playerPrefs() {
        let found = null;
        try { found = JSON.parse(window.localStorage.getItem(PLAYER_PREFS) || "null"); } catch (e) { found = null; }
        found = found && typeof found === "object" ? found : {};
        return {
            // On by default: a generated clip is a few seconds long, and
            // watching it again is the usual thing to want.
            loop: found.loop !== false,
            muted: found.muted === true,
            rate: PLAYER_RATES.indexOf(found.rate) >= 0 ? found.rate : 1
        };
    }

    function savePlayerPrefs(video) {
        try {
            window.localStorage.setItem(PLAYER_PREFS, JSON.stringify({
                loop: !!video.loop, muted: !!video.muted, rate: video.playbackRate || 1
            }));
        } catch (e) { /* memory only; the player still works */ }
    }

    //: The glyphs, as SVG paths filled with the button's own text colour -
    //: which is what lets them follow a night theme, where an emoji would
    //: be the same colourful picture on every theme and every phone.
    const PLAYER_ICONS = {
        play: "M8 5v14l11-7z",
        pause: "M6 5h4v14H6zM14 5h4v14h-4z",
        restart: "M12 5V2L7 6l5 4V7a5 5 0 1 1-5 5H5a7 7 0 1 0 7-7z",
        back: "M6 6h2v12H6zM9.5 12L18 18V6z",
        forward: "M16 6h2v12h-2zM6 18l8.5-6L6 6z",
        loop: "M7 7h10v3l4-4-4-4v3H5v6h2zM17 17H7v-3l-4 4 4 4v-3h12v-6h-2z",
        sound: "M3 9v6h4l5 5V4L7 9zM16.5 12A4.5 4.5 0 0 0 14 8v8a4.5 4.5 0 0 0 2.5-4zM14 3.2v2.1a7 7 0 0 1 0 13.4v2.1a9 9 0 0 0 0-17.6z",
        muted: "M3 9v6h4l5 5V4L7 9zM16 9.4l1.4-1.4L19.5 10l2.1-2.1L23 9.4 20.9 11.5 23 13.6l-1.4 1.4-2.1-2.1-2.1 2.1L16 13.6l2.1-2.1z",
        full: "M5 5h5v2H7v3H5zM14 5h5v5h-2V7h-3zM5 14h2v3h3v2H5zM17 14h2v5h-5v-2h3z",
        exit: "M8 5h2v5H5V8h3zM14 5h2v3h3v2h-5zM5 14h5v5H8v-3H5zM14 14h5v2h-3v3h-2z"
    };

    function playerIcon(name) {
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("viewBox", "0 0 24 24");
        svg.setAttribute("aria-hidden", "true");
        svg.setAttribute("focusable", "false");
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        path.setAttribute("d", PLAYER_ICONS[name] || "");
        path.setAttribute("fill", "currentColor");
        svg.appendChild(path);
        return svg;
    }

    //: The same reason the tiles are pinned (see the theme note in CLAUDE.md):
    //: a theme's rule about `button` reaches these too, and a control that a
    //: theme can squash is a control a finger can miss.
    const CONTROL_GEOMETRY = {
        "display": "inline-flex",
        "align-items": "center",
        "justify-content": "center",
        "box-sizing": "border-box",
        "width": "44px",
        "min-width": "44px",
        "height": "44px",
        "padding": "0",
        "margin": "0",
        "flex": "0 0 auto"
    };

    function playerButton(name, label, onPress) {
        const button = el("button", "minipaint-clip-player-button");
        button.type = "button";
        button.dataset.control = name;
        button.setAttribute("aria-label", label);
        button.title = label;
        button.appendChild(playerIcon(name));
        pin(button, CONTROL_GEOMETRY);
        button.addEventListener("click", function (event) {
            event.stopPropagation();
            onPress(button);
        });
        return button;
    }

    function setIcon(button, name, label) {
        button.replaceChildren(playerIcon(name));
        button.setAttribute("aria-label", label);
        button.title = label;
    }

    /** `m:ss.t` - to the tenth, because the clips are seconds long. */
    function clock(seconds) {
        const value = isFinite(seconds) && seconds > 0 ? seconds : 0;
        const minutes = Math.floor(value / 60);
        const rest = value - minutes * 60;
        return minutes + ":" + (rest < 10 ? "0" : "") + rest.toFixed(1);
    }

    /** Stop the clock only if it is this video's. An event from a video that
     *  has just been replaced arrives after its successor started, and must
     *  not stop the successor's clock. */
    function stopClockFor(video) {
        if (S.outputs.clock && S.outputs.clock.video === video) { stopClock(); }
    }

    function stopClock() {
        const running = S.outputs.clock;
        S.outputs.clock = null;
        if (!running) { return; }
        try {
            if (running.kind === "video" && running.video.cancelVideoFrameCallback) {
                running.video.cancelVideoFrameCallback(running.handle);
            } else if (running.kind === "frame") {
                window.cancelAnimationFrame(running.handle);
            }
        } catch (e) { /* already stopped */ }
    }

    /** Draw where the video is. Writes only; never reads layout. */
    function drawTime(controls) {
        const video = controls.video;
        const duration = isFinite(video.duration) ? video.duration : 0;
        if (!controls.scrubbing) {
            controls.scrub.max = String(duration || 0);
            controls.scrub.value = String(video.currentTime || 0);
            controls.scrub.style.setProperty("--minipaint-clip-played",
                (duration ? (100 * video.currentTime / duration) : 0).toFixed(2) + "%");
        }
        const shown = clock(video.currentTime) + " / " + clock(duration);
        if (shown !== controls.shown) {
            controls.shown = shown;
            controls.time.textContent = shown;
        }
    }

    /** Follow the video frame by frame while it plays, and not otherwise. */
    function startClock(controls) {
        stopClock();
        const video = controls.video;
        const running = { video: video, kind: "", handle: 0 };
        S.outputs.clock = running;
        const useFrames = typeof video.requestVideoFrameCallback === "function";
        const tick = function (now, meta) {
            if (S.outputs.clock !== running || S.outputs.player !== video || video.paused) { return; }
            // The length of a frame, learned from the frames themselves: the
            // smallest step between two presented frames is one frame.
            if (meta && typeof meta.mediaTime === "number") {
                if (controls.lastMedia !== undefined) {
                    const step = meta.mediaTime - controls.lastMedia;
                    if (step > 0.004 && (!controls.frame || step < controls.frame)) { controls.frame = step; }
                }
                controls.lastMedia = meta.mediaTime;
            }
            drawTime(controls);
            running.kind = useFrames ? "video" : "frame";
            running.handle = useFrames ? video.requestVideoFrameCallback(tick)
                                       : window.requestAnimationFrame(tick);
        };
        running.kind = useFrames ? "video" : "frame";
        running.handle = useFrames ? video.requestVideoFrameCallback(tick) : window.requestAnimationFrame(tick);
    }

    function togglePlay(controls) {
        const video = controls.video;
        if (video.paused || video.ended) {
            const started = video.play();
            if (started && typeof started.catch === "function") {
                started.catch(function () { drawPlay(controls); });
            }
        } else {
            video.pause();
        }
    }

    function drawPlay(controls) {
        const playing = !controls.video.paused && !controls.video.ended;
        setIcon(controls.play, playing ? "pause" : "play", playing ? "Pause" : "Play");
        controls.root.classList.toggle("minipaint-clip-player-playing", playing);
    }

    function stepFrame(controls, direction, seconds) {
        const video = controls.video;
        if (!video.paused) { video.pause(); }
        const step = seconds || controls.frame || FRAME_FALLBACK;
        const duration = isFinite(video.duration) ? video.duration : 0;
        const target = Math.max(0, Math.min(duration || Infinity, (video.currentTime || 0) + direction * step));
        video.currentTime = target;
        drawTime(controls);
    }

    function inFullscreen() {
        return !!(document.fullscreenElement || document.webkitFullscreenElement);
    }

    function leaveFullscreen() {
        if (!inFullscreen()) { return; }
        try {
            const leave = document.exitFullscreen || document.webkitExitFullscreen;
            if (leave) { leave.call(document); }
        } catch (e) { /* the browser left it already */ }
    }

    /** The stage, not the video: in full screen the controls are still ours. */
    function toggleFullscreen(controls) {
        if (inFullscreen()) { leaveFullscreen(); return; }
        const stage = S.outputs.stage;
        const enter = stage && (stage.requestFullscreen || stage.webkitRequestFullscreen);
        try {
            if (enter) {
                const done = enter.call(stage);
                if (done && typeof done.catch === "function") { done.catch(function () { /* refused */ }); }
            } else if (controls.video.webkitEnterFullscreen) {
                // iOS: only the video itself can go full screen, with the
                // system's own controls. Better that than nothing.
                controls.video.webkitEnterFullscreen();
            }
        } catch (e) { /* refused; the button stays where it is */ }
    }

    function nextRate(rate) {
        const index = PLAYER_RATES.indexOf(rate);
        return PLAYER_RATES[(index + 1) % PLAYER_RATES.length];
    }

    function rateLabel(rate) {
        return (rate === 0.25 ? "\u00bc" : rate === 0.5 ? "\u00bd" : String(rate)) + "\u00d7";
    }

    /** The control bar for one video. */
    function buildPlayer(video) {
        const prefs = playerPrefs();
        video.loop = prefs.loop;
        video.muted = prefs.muted;
        // The default as well as the rate: loading a source puts the rate
        // back to the default, so a rate set on its own lasts only until the
        // file arrives.
        video.defaultPlaybackRate = prefs.rate;
        video.playbackRate = prefs.rate;

        const root = el("div", "minipaint-clip-player");
        root.setAttribute("role", "group");
        root.setAttribute("aria-label", "Video controls");
        const controls = { video: video, root: root, scrubbing: false, frame: 0, shown: "" };

        const line = el("div", "minipaint-clip-player-line");
        const scrub = el("input", "minipaint-clip-player-scrub");
        scrub.type = "range";
        scrub.min = "0";
        scrub.max = "0";
        scrub.step = "any";
        scrub.value = "0";
        scrub.setAttribute("aria-label", "Position in the video");
        const time = el("span", "minipaint-clip-player-time", "0:00.0 / 0:00.0");
        line.appendChild(scrub);
        line.appendChild(time);
        root.appendChild(line);
        Object.assign(controls, { scrub: scrub, time: time });

        // Scrubbing: the thumb belongs to the finger while it is held. A
        // coarse seek while dragging, where the browser offers one, and an
        // exact one where it is let go.
        const seek = function (exact) {
            const target = Number(scrub.value) || 0;
            try {
                if (!exact && typeof video.fastSeek === "function") { video.fastSeek(target); }
                else { video.currentTime = target; }
            } catch (e) { /* not seekable yet */ }
            scrub.style.setProperty("--minipaint-clip-played",
                (Number(scrub.max) ? (100 * target / Number(scrub.max)) : 0).toFixed(2) + "%");
        };
        scrub.addEventListener("pointerdown", function () { controls.scrubbing = true; });
        scrub.addEventListener("input", function () { controls.scrubbing = true; seek(false); });
        scrub.addEventListener("change", function () { seek(true); controls.scrubbing = false; drawTime(controls); });
        scrub.addEventListener("pointerup", function () { controls.scrubbing = false; });
        scrub.addEventListener("click", function (event) { event.stopPropagation(); });

        const row = el("div", "minipaint-clip-player-row");
        const restart = playerButton("restart", "Back to the start", function () {
            video.currentTime = 0;
            drawTime(controls);
        });
        const back = playerButton("back", "One frame back", function () { stepFrame(controls, -1); });
        const play = playerButton("play", "Play", function () { togglePlay(controls); });
        play.classList.add("minipaint-clip-player-main");
        const forward = playerButton("forward", "One frame forward", function () { stepFrame(controls, 1); });
        const loop = playerButton("loop", "Loop", function () {
            video.loop = !video.loop;
            loop.setAttribute("aria-pressed", String(video.loop));
            savePlayerPrefs(video);
        });
        loop.setAttribute("aria-pressed", String(video.loop));
        const sound = playerButton(video.muted ? "muted" : "sound", video.muted ? "Unmute" : "Mute", function () {
            video.muted = !video.muted;
            savePlayerPrefs(video);
        });
        const speed = el("button", "minipaint-clip-player-button minipaint-clip-player-speed", rateLabel(video.playbackRate));
        speed.type = "button";
        speed.dataset.control = "speed";
        speed.setAttribute("aria-label", "Playback speed " + video.playbackRate);
        speed.title = "Playback speed";
        pin(speed, Object.assign({}, CONTROL_GEOMETRY, { "width": "auto", "min-width": "52px", "padding": "0 8px" }));
        speed.addEventListener("click", function (event) {
            event.stopPropagation();
            const rate = nextRate(video.playbackRate);
            video.defaultPlaybackRate = rate;
            video.playbackRate = rate;
            savePlayerPrefs(video);
        });
        const full = playerButton("full", "Full screen", function () { toggleFullscreen(controls); });
        const canFullscreen = !!(document.fullscreenEnabled || document.webkitFullscreenEnabled
                                 || video.webkitEnterFullscreen);
        full.hidden = !canFullscreen;
        for (const button of [restart, back, play, forward, loop, sound, speed, full]) { row.appendChild(button); }
        root.appendChild(row);
        Object.assign(controls, { play: play, loop: loop, sound: sound, speed: speed, full: full });

        video.addEventListener("play", function () { drawPlay(controls); startClock(controls); });
        video.addEventListener("pause", function () { drawPlay(controls); stopClockFor(video); drawTime(controls); });
        video.addEventListener("ended", function () { drawPlay(controls); stopClockFor(video); drawTime(controls); });
        for (const name of ["loadedmetadata", "durationchange", "seeked", "timeupdate"]) {
            video.addEventListener(name, function () {
                // While playing the frame loop draws; these are for a paused
                // video that is being moved by hand.
                if (video.paused) { drawTime(controls); }
            });
        }
        video.addEventListener("volumechange", function () {
            setIcon(sound, video.muted ? "muted" : "sound", video.muted ? "Unmute" : "Mute");
        });
        // Drawn from the event, saved only from the press: the browser changes
        // the rate itself when a source loads, and that is not a preference.
        video.addEventListener("ratechange", function () {
            speed.textContent = rateLabel(video.playbackRate);
            speed.setAttribute("aria-label", "Playback speed " + video.playbackRate);
        });
        const onFullscreen = function () {
            const on = inFullscreen();
            setIcon(full, on ? "exit" : "full", on ? "Leave full screen" : "Full screen");
            if (S.outputs.stage) { S.outputs.stage.classList.toggle("minipaint-clip-output-full", on); }
        };
        document.addEventListener("fullscreenchange", onFullscreen);
        document.addEventListener("webkitfullscreenchange", onFullscreen);
        controls.forget = function () {
            document.removeEventListener("fullscreenchange", onFullscreen);
            document.removeEventListener("webkitfullscreenchange", onFullscreen);
        };
        drawPlay(controls);
        drawTime(controls);
        return controls;
    }

    /** The keys a player answers to while the view is open. */
    function playerKey(event) {
        const controls = S.outputs.controls;
        if (!controls || !outputsOpen()) { return false; }
        const target = event.target;
        if (target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName))
            && !(target.classList && target.classList.contains("minipaint-clip-player-scrub"))) {
            return false;
        }
        const key = event.key;
        if (key === " " || key === "k" || key === "K") { togglePlay(controls); }
        else if (key === "ArrowLeft") { stepFrame(controls, -1, event.shiftKey ? 1 : 0); }
        else if (key === "ArrowRight") { stepFrame(controls, 1, event.shiftKey ? 1 : 0); }
        else if (key === "l" || key === "L") { controls.loop.click(); }
        else if (key === "m" || key === "M") { controls.sound.click(); }
        else if (key === "f" || key === "F") { toggleFullscreen(controls); }
        else { return false; }
        event.preventDefault();
        event.stopPropagation();
        return true;
    }

    //: How far outside the strip a tile is loaded, and how far past that it
    //: is let go again. Two numbers rather than one so a tile resting near
    //: the edge is not attached and detached on every pixel of scroll.
    const TILE_NEAR = "200px";
    const TILE_FAR = "600px";

    /** Attach a strip tile's media only while it is near the strip's window,
     * and let it go again when it is not.
     *
     * A page of outputs is sixty items (``PAGE_SIZE``), and a video tile is a
     * `<video>`: the browser opens a connection, range-requests the header and
     * enough of the file to paint the frame at ``#t=1``, and spins up a
     * decoder for it. Sixty of those at once is sixty decoders competing for
     * CPU and - worse - for the six HTTP/1.1 connections this origin has,
     * which the page has already spent most of on streams that never close
     * (see the connection-starvation note in CLAUDE.md). The stage player's
     * own range requests queue behind all of it, which is what choppy
     * playback *is* here: the video is not slow to decode, it is slow to
     * arrive.
     *
     * `loading="lazy"` would do this for a picture and does nothing at all for
     * a video, so the videos are what this is for; pictures go through it too
     * so that one rule covers the strip.
     */
    function watchTiles(strip) {
        const tiles = Array.from(strip.querySelectorAll(".minipaint-clip-output-tile"));
        const mediaOf = function (tile) {
            return tile.querySelector(".minipaint-clip-output-thumb");
        };
        if (typeof IntersectionObserver !== "function") {
            // Nothing to be clever with: every tile loads, as it always did.
            for (const tile of tiles) { attachTile(mediaOf(tile)); }
            return;
        }
        // One pair of observers for the whole strip, not a pair per tile:
        // sixty tiles is a hundred and twenty observers for one question.
        //
        // Wired after the strip is in the document, because `rootMargin` is
        // measured against the root's own box and a root that is not laid out
        // has none - which is the difference between loading a tile a little
        // early and never loading it at all.
        const near = new IntersectionObserver(function (entries) {
            for (const entry of entries) {
                if (entry.isIntersecting) { attachTile(mediaOf(entry.target)); }
            }
        }, { root: strip, rootMargin: TILE_NEAR });
        const far = new IntersectionObserver(function (entries) {
            for (const entry of entries) {
                if (!entry.isIntersecting) { detachTile(mediaOf(entry.target)); }
            }
        }, { root: strip, rootMargin: TILE_FAR });
        for (const tile of tiles) { near.observe(tile); far.observe(tile); }
        S.outputs.watchers = [near, far];
    }

    function attachTile(media) {
        if (!media || media.getAttribute("src")) { return; }
        const wanted = media.dataset.src;
        if (wanted) { media.setAttribute("src", wanted); }
    }

    function detachTile(media) {
        if (!media || !media.getAttribute("src")) { return; }
        media.removeAttribute("src");
        // A `<video>` holds its decoder and its buffered bytes until it is
        // told to look again. Without this the element is quiet but the cost
        // of it is still on the page.
        if (media.tagName === "VIDEO" && typeof media.load === "function") {
            try { media.load(); } catch (e) { /* already torn down */ }
        }
    }

    /** Let go of every tile in the strip that is being replaced. */
    function stopTiles() {
        for (const observer of S.outputs.watchers || []) {
            try { observer.disconnect(); } catch (e) { /* already gone */ }
        }
        S.outputs.watchers = [];
        const strip = S.outputs.strip;
        if (!strip) { return; }
        for (const media of Array.from(strip.querySelectorAll(".minipaint-clip-output-thumb"))) {
            detachTile(media);
        }
    }

    /**
     * Load: put the recipe that made this output back into the composer.
     *
     * The same path the Queue Send History's own Load takes - the hidden box
     * the server's `history_action` reads - so it restores exactly what that
     * does: the prompt as it was typed, each picture that is still in the
     * library, and "Use WanGP" for what was inherited or has since gone. It
     * queues nothing. The view closes, because the composer is what it has
     * just changed.
     *
     * An output with no recipe - its request was made before history was
     * kept, or its record has been deleted or aged out - says so rather than
     * offering a button that would do nothing.
     */
    function loadButton(item) {
        const button = el("button", "minipaint-clip-output-load", "Load");
        button.type = "button";
        if (!item.recipe) {
            button.disabled = true;
            button.title = "No recipe was kept for this output, so there is nothing to load.";
            return button;
        }
        button.title = "Put this output's prompt and pictures back in the request, to generate again. Nothing is queued.";
        button.addEventListener("click", function (event) {
            event.stopPropagation();
            loadRecipe(item);
        });
        return button;
    }

    function loadRecipe(item) {
        if (!item || !item.recipe) { return false; }
        const sent = sendInput(BOXES.historyAction, "load:" + item.recipe + ":" + Date.now());
        note("outputs: load " + String(item.recipe).slice(0, 8) + (sent ? "" : " (the history box is missing)"));
        if (!sent) { return false; }
        closeOutputs();
        // The history list is re-read the way its own Load does it.
        setTimeout(function () { askQueue(null); }, 300);
        return true;
    }

    function outputTile(item, chosen) {
        const tile = el("button", "minipaint-clip-output-tile" + (chosen ? " minipaint-clip-output-chosen" : ""));
        tile.type = "button";
        tile.dataset.output = item.id;
        tile.title = item.name + (item.exact ? "" : " — matched to a request by when it was written");
        let media;
        if (item.kind === "video") {
            // The poster is the frame at one second, asked for with a media
            // fragment. No ffmpeg, no server-side thumbnailing, no second
            // copy of the file on disk: the browser already decodes video,
            // and metadata plus one frame is all it has to fetch to draw this.
            //
            // The address goes in `data-src` and not in `src`: see watchTile.
            media = el("video", "minipaint-clip-output-thumb");
            media.preload = "metadata";
            media.muted = true;
            media.playsInline = true;
            media.dataset.src = item.url + "#t=1";
        } else {
            media = el("img", "minipaint-clip-output-thumb");
            media.loading = "lazy";
            media.alt = "";
            media.dataset.src = item.url;
        }
        tile.appendChild(media);
        if (!item.exact) { tile.appendChild(el("span", "minipaint-clip-output-guess", "~")); }
        return tile;
    }

    /** Put one output in the stage. Videos play; pictures just show. */
    function showOutput(item) {
        const stage = S.outputs.stage;
        if (!stage || !item) { return; }
        S.outputs.chosen = item.id;
        stopPlayer();
        stage.innerHTML = "";
        S.outputs.player = null;
        let media;
        if (S.outputs.controls && S.outputs.controls.forget) { S.outputs.controls.forget(); }
        S.outputs.controls = null;
        let controls = null;
        if (item.kind === "video") {
            media = el("video", "minipaint-clip-output-stage-media");
            media.controls = false;
            media.playsInline = true;
            media.preload = "auto";
            S.outputs.player = media;
            controls = buildPlayer(media);
            S.outputs.controls = controls;
            // A press on the picture is play and pause, the way every video
            // on a phone works. Only on the picture: the bar has its own.
            media.addEventListener("click", function (event) {
                event.stopPropagation();
                togglePlay(controls);
            });
            media.src = item.url;
        } else {
            media = el("img", "minipaint-clip-output-stage-media");
            media.alt = item.name;
            media.src = item.url;
        }
        stage.appendChild(media);
        if (controls) {
            stage.appendChild(controls.root);
            const started = media.play();
            if (started && typeof started.catch === "function") {
                // Refused - a browser that wants a gesture first. The play
                // button says so by still being a play button.
                started.catch(function () { drawPlay(controls); });
            }
        }
        const caption = el("div", "minipaint-clip-output-caption");
        caption.appendChild(el("span", "minipaint-clip-output-name", item.name));
        if (item.prompt) {
            const prompt = el("span", "minipaint-clip-output-prompt", item.prompt);
            prompt.title = item.prompt;
            caption.appendChild(prompt);
        }
        if (!item.exact) {
            const guess = el("span", "minipaint-clip-output-guess-note",
                             "matched to a request by when it was written");
            caption.appendChild(guess);
        }
        caption.appendChild(loadButton(item));
        stage.appendChild(caption);
        const strip = S.outputs.strip;
        if (strip) {
            for (const tile of Array.from(strip.querySelectorAll(".minipaint-clip-output-tile"))) {
                tile.classList.toggle("minipaint-clip-output-chosen", tile.dataset.output === item.id);
            }
            const chosen = strip.querySelector(".minipaint-clip-output-chosen");
            if (chosen && chosen.scrollIntoView) { chosen.scrollIntoView({ block: "nearest", inline: "nearest" }); }
        }
    }

    function drawOutputs(answer) {
        const panel = outputsPanel();
        if (!panel) { return; }
        const items = (answer && answer.items) || [];
        S.outputs.items = items;
        // Counted, so a caller can tell this draw from the one left on the
        // panel by the last time it was open: closing does not clear it.
        S.outputs.drawn = (S.outputs.drawn || 0) + 1;
        S.outputs.page = (answer && answer.page) || 0;
        S.outputs.pages = (answer && answer.pages) || 1;
        panel.innerHTML = "";

        const stage = el("div", "minipaint-clip-output-stage");
        S.outputs.stage = stage;
        panel.appendChild(stage);

        const bar = el("div", "minipaint-clip-output-bar");
        bar.appendChild(el("span", "minipaint-clip-output-status", (answer && answer.status) || ""));
        if (S.outputs.pages > 1) {
            const pager = el("div", "minipaint-clip-output-pager");
            const back = el("button", "minipaint-clip-output-page", "‹");
            back.type = "button";
            back.disabled = S.outputs.page <= 0;
            back.addEventListener("click", function () { askOutputs(S.outputs.page - 1); });
            const forward = el("button", "minipaint-clip-output-page", "›");
            forward.type = "button";
            forward.disabled = S.outputs.page >= S.outputs.pages - 1;
            forward.addEventListener("click", function () { askOutputs(S.outputs.page + 1); });
            pager.appendChild(back);
            pager.appendChild(el("span", "minipaint-clip-output-count",
                                 "Page " + (S.outputs.page + 1) + " of " + S.outputs.pages));
            pager.appendChild(forward);
            bar.appendChild(pager);
        }
        const close = el("button", "minipaint-clip-output-close", "Close");
        close.type = "button";
        close.addEventListener("click", closeOutputs);
        bar.appendChild(close);
        panel.appendChild(bar);

        const strip = el("div", "minipaint-clip-output-strip");
        strip.addEventListener("click", function (event) {
            const tile = event.target && event.target.closest ? event.target.closest("[data-output]") : null;
            if (!tile) { return; }
            const wanted = (S.outputs.items || []).filter(function (one) { return one.id === tile.dataset.output; })[0];
            if (wanted) { showOutput(wanted); }
        });
        stopTiles();
        S.outputs.strip = strip;
        for (const item of items) { strip.appendChild(outputTile(item, false)); }
        panel.appendChild(strip);
        watchTiles(strip);

        if (items.length) {
            showOutput(items[0]);
        } else {
            stage.appendChild(el("div", "minipaint-clip-output-empty",
                                 (answer && answer.status) || "Nothing here yet."));
        }
    }

    function askOutputs(page) {
        const wanted = Math.max(0, page || 0);
        return fetch(OUTPUTS_ROUTE + "?page=" + encodeURIComponent(wanted),
                     { credentials: "same-origin", cache: "no-store" })
            .then(function (response) { return response.json(); })
            .then(function (answer) {
                if (!answer || answer.ok !== true) { throw new Error((answer && answer.message) || "outputs"); }
                serverSilent(false);
                drawOutputs(answer);
                return answer;
            }, function (error) {
                note("outputs: could not be read (" + ((error && error.message) || error) + ")");
                serverSilent(true, "the outputs");
                drawOutputs({ items: [], page: 0, pages: 1, status: "The outputs could not be read." });
                return null;
            });
    }

    /**
     * Open the gallery over the whole window.
     *
     * The same shape as the system-prompt editor - a panel with no shape on
     * the tab until a class puts it over everything - because they are the
     * same idea: a thing that wants the screen, reached from one button, and
     * closed by Escape or its own Close.
     */
    function openOutputs() {
        const panel = outputsPanel();
        if (!panel) { return false; }
        // Before the class, so the measurement does not find this panel.
        applyHeaderInset();
        watchHeaderInset();
        panel.classList.add(EDITOR_OPEN_CLASS);
        if (S.outputsKey) { document.removeEventListener("keydown", S.outputsKey, true); }
        S.outputsKey = function (event) {
            if (event.key === "Escape") {
                // Full screen first, the way every player does it: one
                // Escape leaves full screen, the next leaves the view.
                if (inFullscreen()) { return; }
                event.stopPropagation();
                closeOutputs();
                return;
            }
            playerKey(event);
        };
        document.addEventListener("keydown", S.outputsKey, true);
        askOutputs(0);
        return true;
    }

    function closeOutputs() {
        const panel = outputsPanel();
        stopPlayer();
        if (S.outputs.controls && S.outputs.controls.forget) { S.outputs.controls.forget(); }
        S.outputs.controls = null;
        if (panel) { panel.classList.remove(EDITOR_OPEN_CLASS); }
        if (S.outputsKey) { document.removeEventListener("keydown", S.outputsKey, true); S.outputsKey = null; }
        return true;
    }

    /* ------------------------------------------------------------------ */
    /* The menu                                                              */
    /* ------------------------------------------------------------------ */

    function buildMenu() {
        const column = byId(BROWSER_ID);
        if (!column || S.menu) { return; }
        const panel = document.createElement("div");
        panel.className = "minipaint-clip-menu";
        panel.hidden = true;
        panel.setAttribute("role", "menu");
        panel.addEventListener("click", function (event) {
            const item = event.target && event.target.closest ? event.target.closest("[data-menu]") : null;
            if (!item || !panel.contains(item)) { return; }
            event.preventDefault();
            menuAction(item.dataset.menu, item.dataset.value || "");
        });
        column.appendChild(panel);
        S.menu = panel;
    }

    function menuItems(section) {
        const state = menuState();
        const tick = function (on) { return on ? "✓ " : ""; };
        const back = S.menuStandalone ? [] : [{ menu: "back", label: "‹ Back" }];
        if (section === "sort") {
            const list = back.slice();
            for (const pair of state.sorts || []) {
                list.push({ menu: "sort", value: pair[0], label: tick(state.sort === pair[0]) + pair[1] });
            }
            return list;
        }
        if (section === "send") {
            const list = back.slice();
            if (!S.selected) { list.push({ menu: "status", label: "Select an image first" }); }
            for (const pair of state.destinations || []) {
                list.push({ menu: "send", value: pair[0], label: pair[1], disabled: !S.selected });
            }
            if (!(state.destinations || []).length) { list.push({ menu: "status", label: "No destination is available in this WebUI" }); }
            list.push({ menu: "close", label: "Cancel" });
            return list;
        }
        if (section === "intercept") {
            // Where the gallery's 🖌️ button sends: three mutually exclusive
            // choices, the current one ticked. The labels come from the
            // server when it has them, so a fourth destination is a change
            // there and not here.
            const list = back.slice();
            const current = interceptTarget();
            const choices = Array.isArray(state.intercepts) && state.intercepts.length ? state.intercepts : INTERCEPTS;
            for (const pair of choices) {
                list.push({ menu: "intercept", value: pair[0], label: tick(current === pair[0]) + "Send to “" + pair[1] + "”" });
            }
            return list;
        }
        return [
            { menu: "section", value: "intercept", label: "Intercept Options ›" },
            { menu: "press", value: PRESS.refresh, label: "Refresh" },
            { menu: "section", value: "sort", label: "Sort ›" },
            { menu: "paste", label: "Paste image" },
            { menu: "press", value: PRESS.upload, label: "Upload image(s)…" },
            { menu: "press", value: PRESS.folder, label: "Choose storage folder…" },
            { menu: "press", value: PRESS.rename, label: "Rename selected…", disabled: !S.selected },
            { menu: "press", value: PRESS.remove, label: "Delete selected…", disabled: !S.selected },
            { menu: "press", value: PRESS.history, label: "Queue Send History" }
        ];
    }

    function renderMenu(section) {
        const panel = S.menu;
        if (!panel) { return; }
        S.menuSection = section || null;
        panel.innerHTML = "";
        for (const item of menuItems(S.menuSection)) {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "minipaint-clip-menu-item" + (item.menu === "section" ? " minipaint-clip-menu-more" : "")
                + (item.menu === "back" ? " minipaint-clip-menu-back" : "");
            button.dataset.menu = item.menu;
            if (item.value) { button.dataset.value = item.value; }
            if (item.menu === "status" || item.disabled) { button.disabled = true; button.classList.add("minipaint-clip-menu-dim"); }
            button.setAttribute("role", "menuitem");
            button.textContent = item.label;
            panel.appendChild(button);
        }
    }

    function menuAction(kind, value) {
        switch (kind) {
            case "section": renderMenu(value); return;
            case "back": renderMenu(null); return;
            case "close": closeMenu(); return;
            case "press":
                closeMenu();
                pressHidden(value);
                if (value === PRESS.refresh) { fetchLibrary({ refresh: true }); }
                if (value === PRESS.history) { askQueue(null); }
                return;
            case "intercept": closeMenu(); setIntercept(value); return;
            // Over the tab's own route, and the grid re-drawn from the
            // answer. The hidden box still goes so the toolbar dropdown
            // follows on a page whose Gradio is alive; nothing waits on it.
            case "sort": closeMenu(); setSort(value); sendInput(BOXES.sortRequest, value + ":" + Date.now()); return;
            case "paste": closeMenu(); pasteFromClipboard(); return;
            case "send": closeMenu(); sendTo(value); return;
            default: return;
        }
    }

    function positionMenu() {
        const button = S.menuAnchor || byId(MENU_ID);
        const column = byId(BROWSER_ID);
        if (!button || !column || !S.menu) { return; }
        const b = button.getBoundingClientRect();
        const c = column.getBoundingClientRect();
        S.menu.style.top = (b.bottom - c.top + 4) + "px";
        // Kept inside the column: a button near its right edge would
        // otherwise hang the list off the side of the tab.
        const left = Math.min(b.left - c.left, Math.max(0, c.width - S.menu.offsetWidth));
        S.menu.style.left = Math.max(0, left) + "px";
    }

    function openMenu(section) {
        if (!S.menu) { return; }
        renderMenu(section || null);
        S.menu.hidden = false;
        positionMenu();
        S.menuOutside = function (event) {
            const target = event.target;
            if (S.menu.contains(target)) { return; }
            if (target && target.closest && target.closest("#" + MENU_ID)) { return; }
            if (S.menuAnchor && target && S.menuAnchor.contains(target)) { return; }
            closeMenu();
        };
        document.addEventListener("pointerdown", S.menuOutside, true);
        S.menuKey = function (event) {
            if (event.key === "Escape") { event.stopPropagation(); closeMenu(); }
        };
        document.addEventListener("keydown", S.menuKey, true);
    }

    function closeMenu() {
        if (!S.menu) { return; }
        S.menu.hidden = true;
        S.menuSection = null;
        S.menuAnchor = null;
        S.menuStandalone = false;
        if (S.menuOutside) { document.removeEventListener("pointerdown", S.menuOutside, true); S.menuOutside = null; }
        if (S.menuKey) { document.removeEventListener("keydown", S.menuKey, true); S.menuKey = null; }
    }

    function toggleMenu() {
        if (!S.menu) { buildMenu(); }
        if (!S.menu) { return; }
        const was = !S.menu.hidden && !S.menuStandalone;
        if (!S.menu.hidden) { closeMenu(); }
        if (was) { return; }
        S.menuAnchor = byId(MENU_ID);
        S.menuStandalone = false;
        openMenu(null);
    }

    /**
     * One of the toolbar's own flyouts: the same list the menu draws for that
     * section, anchored under the button that asked for it and standing on
     * its own. Pressing the same button again closes it, the way a menu does.
     */
    function openToolbarMenu(section) {
        if (!S.menu) { buildMenu(); }
        if (!S.menu) { return; }
        const anchor = byId(section === "send" ? SEND_BUTTON_ID : SORT_BUTTON_ID);
        const again = !S.menu.hidden && S.menuStandalone && S.menuSection === section;
        if (!S.menu.hidden) { closeMenu(); }
        if (again) { return; }
        S.menuAnchor = anchor;
        S.menuStandalone = true;
        openMenu(section);
    }

    /* ------------------------------------------------------------------ */
    /* Import: paste and drop                                                */
    /* ------------------------------------------------------------------ */

    async function importBlob(blob, name, source) {
        const response = await fetch(IMPORT_ROUTE + "?source=" + encodeURIComponent(source || "paste"), {
            method: "POST",
            credentials: "same-origin",
            cache: "no-store",
            headers: { "Content-Type": blob.type || "application/octet-stream", "X-MiniPaint-Filename": name || "" },
            body: blob
        });
        let answer = null;
        try { answer = await response.json(); } catch (e) { answer = null; }
        if (!answer || !answer.ok) {
            const message = answer && answer.message ? String(answer.message) : "The image could not be imported (" + response.status + ").";
            throw new Error(message);
        }
        return answer.asset;
    }

    /**
     * A picture handed in from another tab, put into the library here.
     *
     * The 🖌️ button under a result picks its picture in the browser and
     * hands it to the server as a Gradio event, so it stopped working for
     * the same reason sending out did. This is the other half of the same
     * repair: the library's import is an ordinary POST, so the page fetches
     * the picture the host is already serving and posts it itself.
     *
     * The host's own file is what is fetched, not a re-encode of what is on
     * screen, so a generated PNG keeps the metadata Forge wrote into it -
     * the same thing the server path is careful about.
     *
     * Only when Clipboard is the destination. With the intercept off the
     * picture was going to the Canvas, whose document lives on the server:
     * putting it in the library instead would be answering a different
     * question from the one the button asked.
     */
    async function receiveOverHttp(picked) {
        const target = interceptTarget();
        if (target === "minipaint") {
            note("receive: the picture never arrived, and the Canvas cannot be filled from here");
            toast("That picture did not reach Mini Paint - this page has lost its live connection "
                  + "to the server, and only the server can fill the Canvas.", true);
            connectionNotice(true);
            return false;
        }
        const url = galleryUrl(picked);
        if (!url) {
            note("receive: the handed-in picture named no file this page can read");
            toast("That picture could not be read from this page.", true);
            return false;
        }
        connectionNotice(true);
        if (target === "wangp") {
            // Only a page whose Clipboard bundle was not attached when the
            // button was pressed gets here: otherwise the press never became
            // a framework event at all (onGalleryButton). The finish is the
            // same one.
            note("receive: freezing the picture over the direct route without the queue");
            return openWanGPPopup(url, "");
        }
        try {
            const response = await fetch(url, { credentials: "same-origin", cache: "no-store" });
            if (!response.ok) { throw new Error("the host would not serve it (" + response.status + ")"); }
            const blob = await response.blob();
            const asset = await importBlob(blob, nameFromUrl(url), "forge_gallery");
            note("receive: put into the library over the direct route without the queue");
            toast("Put " + ((asset && asset.filename) || "the picture") + " in Clipboard "
                  + "(the page had lost its connection).");
            // For the composer's cards and the menu, which the server still
            // renders. The GRID hears about this from the library event the
            // import published, so it patches whether or not this press
            // reaches anything.
            pressHidden(PRESS.refresh);
            return true;
        } catch (error) {
            const why = (error && error.message) || String(error);
            note("receive: could not put the picture into the library: " + why);
            toast("That picture could not be put into Clipboard: " + why, true);
            return false;
        }
    }

    /* ------------------------------------------------------------------ */
    /* The gallery's button, while it is pointed at WanGP                    */
    /* ------------------------------------------------------------------ */

    //: The host's result tabs whose output row carries the 🖌️ button, and
    //: the id that button has under each: the server's receive_button_id.
    const GALLERY_TABS = ["txt2img", "img2img", "extras"];
    const GALLERY_BUTTON_SUFFIX = "_send_to_minipaint";

    /**
     * The picture the gallery under that tab is showing, as a URL this page
     * can fetch: the selected thumbnail, else the first - the choice the
     * host's own send buttons make - read off the page rather than asked of
     * the framework, because no framework event is in this chain.
     */
    function galleryPictureUrl(tab) {
        const gallery = byId(tab + "_gallery");
        if (!gallery) { return ""; }
        const thumbs = Array.from(gallery.querySelectorAll(".thumbnail-item"));
        const chosen = thumbs.filter(function (thumb) { return thumb.classList.contains("selected"); })[0] || thumbs[0] || null;
        let img = chosen ? chosen.querySelector("img") : null;
        if (!img) { img = gallery.querySelector("img"); }
        return img ? String(img.currentSrc || img.getAttribute("src") || "") : "";
    }

    /**
     * Freeze a picture the host serves and open the Send to WanGP popup on
     * it: the finish of a gallery press pointed at WanGP, and of the direct
     * route for a page whose bundle missed the press. The popup's own
     * bundle is fetched the first time, and never before a press needs it.
     */
    function openWanGPPopup(url, tab) {
        const bundle = String(menuState().intercept_bundle || "");
        const loader = window.minipaintAssets;
        const loading = window.minipaintIntercept ? Promise.resolve(true)
            : (loader && typeof loader.load === "function" && bundle ? loader.load([bundle]) : Promise.resolve(false));
        const failed = function () {
            note("gallery: the Send to WanGP popup could not be loaded");
            toast("That picture did not reach WanGP - the request popup could not be loaded.", true);
            return false;
        };
        return Promise.resolve(loading).then(function (ok) {
            const popup = window.minipaintIntercept;
            if (!ok || !popup || typeof popup.stageAndOpen !== "function") { return failed(); }
            return popup.stageAndOpen(url, tab, {});
        }, failed);
    }

    /** The result tab whose 🖌️ button a click landed on, or "". */
    function galleryButtonTab(target) {
        if (!target || typeof target.closest !== "function") { return ""; }
        for (const tab of GALLERY_TABS) {
            if (target.closest("#" + tab + GALLERY_BUTTON_SUFFIX)) { return tab; }
        }
        return "";
    }

    /**
     * The gallery's 🖌️ button, taken over by the page while the setting says
     * WanGP.
     *
     * The button's own handler is a Gradio event: the picked picture goes to
     * the server and a frozen copy comes back for the popup - two trips
     * through a queue that has to be alive, on a payload the framework has
     * to accept first (Forge Neo's own gallery helper answers in a shape
     * this Gradio refused before the function ran, and nothing said so
     * anywhere). The Clipboard tab's own sends never were that: they are
     * plain requests the page makes itself. So while the setting is WanGP a
     * press is finished the same way. The event is stopped here, in the
     * capture phase, before the framework's handler on the button sees it;
     * the picture the gallery is showing is fetched from the host and
     * frozen over the public API's staging route; the popup opens on it.
     * Mini Paint and Clipboard keep the framework path: the Canvas's
     * document lives on the server, and the library import is that path's
     * own job.
     */
    function onGalleryButton(event) {
        const tab = galleryButtonTab(event.target);
        if (!tab || interceptTarget() !== "wangp") { return; }
        event.stopPropagation();
        event.preventDefault();
        S.takeovers += 1;
        const url = galleryPictureUrl(tab);
        if (!url) {
            note("gallery: the " + tab + " gallery shows no picture to send to WanGP");
            toast("Generate or pick an image in the gallery first.", true);
            return;
        }
        note("gallery: the " + tab + " send is finished by the page, for WanGP, without the queue");
        openWanGPPopup(url, tab);
    }

    /** The file a gallery item stands for, as a URL this page can fetch. */
    function galleryUrl(picked) {
        let item = picked;
        if (Array.isArray(item)) { item = item.length ? item[0] : null; }
        if (!item) { return ""; }
        if (typeof item === "string") { return item; }
        const image = (item && typeof item === "object" && item.image) ? item.image : item;
        const direct = image && (image.url || image.data);
        if (direct) { return String(direct); }
        const path = image && (image.path || image.name);
        if (!path) { return ""; }
        // Forge serves a temporary file under its own file route; the page
        // asks for it the same way the gallery's own thumbnail does.
        return "/file=" + String(path).split("/").map(encodeURIComponent).join("/");
    }

    function nameFromUrl(url) {
        const clean = String(url || "").split("?")[0].split("#")[0];
        const last = clean.substring(clean.lastIndexOf("/") + 1);
        try { return decodeURIComponent(last) || "gallery.png"; } catch (e) { return last || "gallery.png"; }
    }

    function extensionFor(type) {
        if (type === "image/jpeg") { return ".jpg"; }
        if (type === "image/webp") { return ".webp"; }
        return ".png";
    }

    function stamp() {
        const now = new Date();
        const pad = function (n) { return String(n).padStart(2, "0"); };
        return now.getFullYear() + pad(now.getMonth() + 1) + pad(now.getDate()) + "-" + pad(now.getHours()) + pad(now.getMinutes()) + pad(now.getSeconds());
    }

    /** The menu's Paste: the browser's clipboard, read on this gesture. */
    async function pasteFromClipboard() {
        let blob = null;
        try {
            if (navigator.clipboard && typeof navigator.clipboard.read === "function") {
                const entries = await navigator.clipboard.read();
                for (const entry of entries) {
                    const type = (entry.types || []).find(function (t) { return t.indexOf("image/") === 0; });
                    if (type) { blob = await entry.getType(type); break; }
                }
            }
        } catch (error) {
            blob = null;
        }
        if (!blob) {
            toast("The browser did not let the page read its clipboard. Paste into the box that just opened instead.", true);
            pressHidden(PRESS.paste);
            return;
        }
        await takeBlob(blob, "paste-" + stamp() + extensionFor(blob.type), "paste");
    }

    async function takeBlob(blob, name, source, slot) {
        try {
            const asset = await importBlob(blob, name, source);
            note("imported one picture from " + source + (slot ? " into the " + slot + " slot" : ""));
            if (slot && asset && asset.asset_id) {
                sendInput(BOXES.slotAction, "assign:" + slot + ":" + asset.asset_id + ":" + Date.now());
            }
            // For the cards and the menu; the grid patches from the event.
            pressHidden(PRESS.refresh);
            toast(slot ? "Imported and placed in the slot." : "Imported into Clipboard.");
            return asset;
        } catch (error) {
            toast(String(error && error.message || error), true);
            return null;
        }
    }

    /** Ctrl+V anywhere on the tab, while it is showing. */
    function onDocumentPaste(event) {
        if (!tabVisible()) { return; }
        const target = event.target;
        if (target && target.closest && target.closest("textarea, input[type=text]")) { return; }
        const data = event.clipboardData;
        if (!data || !data.items) { return; }
        for (const item of data.items) {
            if (item.kind === "file" && item.type.indexOf("image/") === 0) {
                const blob = item.getAsFile();
                if (blob) {
                    event.preventDefault();
                    takeBlob(blob, "paste-" + stamp() + extensionFor(blob.type), "paste");
                    return;
                }
            }
        }
    }

    function bindDrop(element) {
        if (!element || element.dataset.minipaintDrop) { return; }
        element.dataset.minipaintDrop = "1";
        element.addEventListener("dragover", function (event) {
            const card = event.target && event.target.closest ? event.target.closest(".minipaint-clip-card") : null;
            if (!card) { return; }
            event.preventDefault();
            card.classList.add("minipaint-clip-card-over");
        });
        element.addEventListener("dragleave", function (event) {
            const card = event.target && event.target.closest ? event.target.closest(".minipaint-clip-card") : null;
            if (card) { card.classList.remove("minipaint-clip-card-over"); }
        });
        element.addEventListener("drop", function (event) {
            const card = event.target && event.target.closest ? event.target.closest(".minipaint-clip-card") : null;
            if (!card) { return; }
            event.preventDefault();
            card.classList.remove("minipaint-clip-card-over");
            const files = event.dataTransfer && event.dataTransfer.files;
            if (!files || !files.length) { return; }
            const file = files[0];
            if (file.type.indexOf("image/") !== 0) { toast("Only an image can be dropped on a slot.", true); return; }
            takeBlob(file, file.name || ("dropped-" + stamp() + extensionFor(file.type)), "slot", card.dataset.slot);
        });
    }

    /* ------------------------------------------------------------------ */
    /* Send out                                                              */
    /* ------------------------------------------------------------------ */

    //: How long a send may take to come back before the page says so. The
    //: server answers in well under a second on a working connection; this
    //: is long enough to cover a slow one and short enough to be useful.
    const SEND_TIMEOUT_MS = 12000;

    /**
     * Send the selected picture to another tab, and never do it silently.
     *
     * Writing the hidden box is only half of a send: the other half is a
     * Gradio event travelling the queue, and that is the half that can go
     * missing - a connection that dropped, a session the server no longer
     * knows, a queue that never drains. When it does, this used to be a
     * button that did nothing at all: no picture, no error, nothing in the
     * status line, and nothing in the log to say what had happened.
     *
     * So the round trip is watched. The server answers every send by
     * writing the status line, so if the status has not changed by the time
     * the watch expires, the send did not arrive - and the page says so
     * rather than leaving the user to guess whether they mis-clicked.
     */
    async function sendTo(target) {
        // Held for the whole round trip. The fallback runs twelve seconds
        // after this point and used to read the selection again when it
        // got there: a render landing in between left it with nothing to
        // send, and it returned without a word.
        const asset = S.selected;
        if (!asset) { toast("Select an image first.", true); return; }
        note("send " + target + " chosen from the menu");
        const stamp = String(Date.now());

        // The picture goes first, from here.
        //
        // This used to hand the send to Gradio and wait twelve seconds for
        // a receipt before trying anything else. On a phone that is the
        // wrong way round: the logs from one are full of "the page went to
        // the background" and "back on screen after 2386s", and Gradio's
        // event stream does not survive being backgrounded - so the queue
        // is not an occasional casualty there, it is down most of the time,
        // and every send was paying twelve seconds to rediscover it.
        //
        // So the delivery happens here, over plain HTTP and the page's own
        // DOM, which is the half that keeps working. The queued event still
        // goes - marked, so the server records the send without writing the
        // destination a second time - and nothing waits on it.
        const outcome = await deliverNow(target, asset);
        const marked = outcome && outcome.ok ? ":done" : "";
        const request = target + ":" + asset + ":" + stamp + marked;
        // The request, three ways, because the first two are not reliable
        // everywhere and this is what four builds of guessing cost.
        //
        // Writing the box used to be the whole of it. On one install the
        // server never heard a single one - not one send acknowledged across
        // four builds, on a desktop browser that never lost its connection -
        // while a pressed button on the same page worked every time. A
        // scripted write only reaches the host if the host notices it; a
        // press is a DOM event, and the only way to miss one is not to be
        // listening.
        //
        // But a press carries no payload: it makes the server READ the box,
        // and on a page whose writes are not heard the box still holds an
        // older send. So the request goes to the server's own route first,
        // over the transport that keeps working, and the server falls back
        // to it when the box has nothing new. Awaited, not fired off, so it
        // cannot lose the race against the press it is there to complete.
        //
        // The server answers whichever arrives first and hands the rest the
        // same receipt, so the picture is delivered exactly once.
        const recorded = await recordRequest(request);
        const written = sendInput(BOXES.sendRequest, request);
        const pressed = pressHidden(PRESS.send);
        // A destination the server can write gets its own press whenever the
        // page did not place the picture itself - not only when it never
        // could. It used to be the second of those, and the difference is a
        // silent non-delivery: a page whose transfer library did not load
        // can NAME the component for Extras or a stitch gallery and still
        // fail to fill it, and nobody was then asked to. The picture went
        // nowhere while the status said it had been sent.
        //
        // That event is the one whose outputs name another tab's component -
        // the one thing an event can name that may not be on this page - so
        // it is kept off the path every other send takes. If it cannot run,
        // this one destination fails and the rest are untouched.
        if (outcome && !outcome.ok && outcome.server) { pressHidden(PRESS.sendBackend); }
        if (!written || !pressed) {
            // Half-built tab: say which half is missing rather than "it did
            // not work", and only give up if the picture did not go either.
            note("send " + target + ": on this page the request box is "
                 + (written ? "there" : "MISSING") + " and the send button is "
                 + (pressed ? "there" : "MISSING"));
            if (!(outcome && outcome.ok) && !written && !pressed) {
                toast("Mini Paint could not reach its send control. Reload the page.", true);
                return;
            }
        }
        if (outcome && outcome.ok) {
            note("send " + target + ": delivered from the page" + (outcome.reason ? " (" + outcome.reason + ")" : "")
                 + (outcome.switched ? "; the destination is open" : "; the destination did not open"
                    + (outcome.switchReason ? " (" + outcome.switchReason + ")" : "")));
            toast(sentSentence(outcome, target), !outcome.switched);
        }
        watchSend(target, stamp, asset, outcome, recorded);
    }

    /**
     * Watch for the server's receipt for THIS send, in the background.
     *
     * The picture has already gone by the time this is armed, so nothing the
     * user is waiting for happens here. What it is still worth knowing is
     * whether the queued event arrived at all: it is what writes the status
     * line, the history and the log, and when it does not arrive the page
     * should say the live connection is gone rather than leaving that to be
     * inferred from a status line that never changes.
     *
     * The stamp is the whole point. The status line was the obvious thing to
     * watch and it is the wrong thing: it says "Sent ..." and is then
     * rewritten by the next refresh, so a successful send looks unanswered a
     * few seconds later and an unanswered one looks fine if anything else
     * wrote to it meanwhile. The acknowledgement box carries back the stamp
     * this call put on its own request, so there is exactly one value that
     * means "the server handled the thing I just asked for".
     */
    /**
     * Leave the request with the server, so a press is enough on its own.
     *
     * Never fatal: a page that could not reach this route is a page that
     * could not have sent anything either, and the box and the press are
     * still going. Silent, because the caller is about to say what happened
     * to the send as a whole.
     */
    async function recordRequest(request) {
        try {
            const response = await fetch(SEND_ROUTE, {
                method: "POST",
                credentials: "same-origin",
                cache: "no-store",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ request: request })
            });
            return !!(response && response.ok);
        } catch (error) {
            return false;
        }
    }

    /**
     * Why a send went unanswered, in one line somebody can act on.
     *
     * A send that is not acknowledged has three different causes wearing the
     * same face, and four builds of this extension were spent guessing
     * between them. Each line below rules one of them out or in:
     *
     *   what the page holds - whether the request box took the write at all,
     *   and whether the receipt box is empty or holds some OTHER send's
     *   stamp. A stamp that is not this one means the server answered a
     *   request this page had already made: the press arrived and carried a
     *   value the framework never updated, which is a different fault from
     *   the press not arriving;
     *
     *   what left the browser - whether the host's framework put a single
     *   request on the wire in the seconds after the press. Nothing on the
     *   wire means the event never fired and no amount of server-side
     *   looking will show anything;
     *
     *   where the host told this page to call it - the one configuration
     *   that blocks every framework request while leaving everything this
     *   extension does working, which is exactly how this looks.
     *
     * Only on failure, and only for the send that failed.
     */
    function whySilent(target, stamp, armed, recorded) {
        const parts = [];
        const ack = boxValue(BOXES.sendAck);
        const requestBox = textarea(BOXES.sendRequest);
        parts.push("the request box " + (!requestBox ? "is not on this page"
            : (String(requestBox.value || "").indexOf(stamp) !== -1 ? "holds this request" : "did NOT take the write")));
        parts.push("the receipt box " + (!ack ? "is empty" : (ack === stamp ? "holds this stamp" : "holds an older stamp (" + ack + ")")));
        parts.push("the send route " + (recorded ? "took the request" : "did NOT take the request"));
        try {
            const wiring = window.minipaintHostWiringNote;
            if (wiring) {
                parts.push(wiring("request box", BOXES.sendRequest));
                parts.push(wiring("send button", PRESS.send));
            }
        } catch (e) { /* the line simply does not carry it */ }
        try {
            const journal = window.minipaintNetJournal;
            parts.push(journal ? "the host's framework: " + journal.sentence(armed)
                               : "this browser cannot report what left it");
        } catch (e) { /* the line simply does not carry it */ }
        try {
            const root = window.minipaintHostRoot && window.minipaintHostRoot();
            if (root && root.known && !root.ok) { parts.push(root.note); }
        } catch (e) { /* nor this one */ }
        note("send " + target + ": why it went unanswered - " + parts.join("; "));
    }

    function watchSend(target, stamp, asset, outcome, recorded) {
        const delivered = !!(outcome && outcome.ok);
        //: When the request went out, so the journal is asked about the right
        //: window rather than about the whole session.
        const armed = Date.now();
        if (S.sendWatch) { clearTimeout(S.sendWatch); S.sendWatch = null; }
        if (S.sendPoll) { clearInterval(S.sendPoll); S.sendPoll = null; }
        const done = function () {
            if (S.sendPoll) { clearInterval(S.sendPoll); S.sendPoll = null; }
            if (S.sendWatch) { clearTimeout(S.sendWatch); S.sendWatch = null; }
        };
        S.sendPoll = setInterval(function () {
            if (boxValue(BOXES.sendAck) !== stamp) { return; }
            done();
            S.queueDown = false;
            connectionNotice(false);
            note("send " + target + ": recorded by the server");
        }, 250);
        S.sendWatch = setTimeout(async function () {
            done();
            if (boxValue(BOXES.sendAck) === stamp) { return; }
            S.queueDown = true;
            connectionNotice(true);
            whySilent(target, stamp, armed, recorded);
            if (delivered) {
                // The picture went; only the server's record of it did not.
                note("send " + target + ": the server never recorded it; the picture went from the page");
                return;
            }
            // The page could not place it either. One more try, in case the
            // destination has since appeared, and then say so plainly.
            note("send " + target + ": nothing arrived in "
                 + (SEND_TIMEOUT_MS / 1000) + "s; trying once more from the page");
            const again = await deliverNow(target, asset);
            if (again && again.ok) {
                note("send " + target + ": delivered from the page on the second try");
                toast(sentSentence(again, target) + (again.switched ? " (the page had lost its connection)." : ""),
                      !again.switched);
                return;
            }
            const why = (again && again.reason) || (outcome && outcome.reason) || "it did not land";
            toast("That picture did not reach " + ((again && again.label) || target) + ": " + why, true);
        }, SEND_TIMEOUT_MS);
    }

    /**
     * Put the picture in its destination now, from this page.
     *
     * The route answers the same question the queued path answers - what
     * does "send to X" mean - through one ``send_plan``, so the two cannot
     * disagree. What comes back is the picture and either the box to write
     * (a host canvas) or the component to hand it to (Extras, the stitch
     * galleries); the placing itself is the editor's own transfer library,
     * which checks that it landed rather than reporting that it wrote
     * something somewhere.
     *
     * Says nothing on its own. The caller knows whether this was the send
     * itself or a second attempt after the queue went quiet, and those want
     * different words.
     */
    async function deliverNow(target, asset) {
        const picture = asset || S.selected;
        if (!picture) {
            return { ok: false, reason: "nothing is selected any more" };
        }
        let plan;
        try {
            const response = await fetch(SEND_ROUTE, {
                method: "POST",
                credentials: "same-origin",
                cache: "no-store",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ target: target, asset: picture })
            });
            plan = await response.json();
        } catch (error) {
            return { ok: false, reason: "the server could not be reached" };
        }
        if (!plan || !plan.ok) {
            return { ok: false, reason: (plan && plan.message) || "that picture could not be sent",
                     code: (plan && plan.code) || "" };
        }
        const label = plan.label || target;
        // Whether this destination is one the SERVER can write, which is not
        // the same question as whether the browser just failed to. It is the
        // one the fallback press turns on: see sendTo.
        const server = !!plan.backend || !!plan.elem;
        if (plan.backend || !plan.payload) {
            // Nothing on this page to put it in: the server has to do it,
            // through its own event. See sendTo, which presses it.
            return { ok: false, backend: true, server: server, label: label, filename: plan.filename,
                     reason: label + " can only be filled in by the server" };
        }
        const canvas = window.minipaintCanvas;
        if (!canvas || typeof canvas.deliverToHost !== "function") {
            return { ok: false, server: server, label: label, filename: plan.filename,
                     reason: "the adapter this page delivers through is not loaded" };
        }
        // One delivery for every destination: the hidden box of a host
        // canvas, or the component's own upload.
        const delivered = await canvas.deliverToHost(plan.instruction, plan.payload, plan.box, {
            elem: plan.elem || "",
            adds: !!plan.adds,
            filename: plan.filename || "",
            label: label
        });
        return {
            ok: !!(delivered && delivered.ok),
            server: server,
            reason: (delivered && delivered.reason) || "",
            // Delivery and navigation are separate facts. A picture proved
            // to have landed in a tab that would not open is a send that
            // worked, said differently - never a send to try again.
            switched: !!(delivered && delivered.switched),
            switchReason: (delivered && delivered.switchReason) || "",
            label: label,
            filename: plan.filename,
            adds: !!plan.adds
        };
    }

    /**
     * What a send that worked is called, once its tab has been dealt with.
     *
     * Forge's own result buttons put the picture in the destination and make
     * that destination visible, and Clipboard keeps that: a target never has
     * to be opened first, and a verified send opens it afterwards. The one
     * case that needs its own sentence is the tab that would not open -
     * because the honest answer there is that the picture is waiting in a
     * tab the user has to find, and the wrong answer is to send it again.
     */
    function sentSentence(outcome, target) {
        const what = (outcome && outcome.filename) || "the picture";
        const where = (outcome && outcome.label) || target;
        if (outcome && !outcome.switched) {
            return "Sent " + what + " to " + where + ", but could not open that tab.";
        }
        return "Sent " + what + " to " + where + (outcome && outcome.adds ? " (added to what is already there)" : "");
    }

    /* ------------------------------------------------------------------ */
    /* Add to Queue                                                          */
    /* ------------------------------------------------------------------ */

    function interop() {
        const api = window.minipaintInterop;
        return api && api.wangp && typeof api.wangp.enqueue === "function" ? api : null;
    }

    /** This page's identity for the outbox, the public API's own. */
    function pageId() {
        const api = interop();
        if (api && api.wangp && typeof api.wangp.pageId === "function") { return api.wangp.pageId(); }
        if (!S.fallbackPage) {
            let text = "";
            for (let k = 0; k < 32; k++) { text += Math.floor(Math.random() * 16).toString(16); }
            S.fallbackPage = text;
        }
        return S.fallbackPage;
    }

    /** Ask the public API to run this page's jobs. Idempotent; bounded there. */
    function pump() {
        const api = interop();
        if (!api || !api.wangp || typeof api.wangp.pump !== "function") { return false; }
        try { api.wangp.pump(); } catch (e) { return false; }
        return true;
    }

    /** The instruction box changed: the server appended a job; start the pump. */
    function queue(instruction, fromWatcher) {
        const text = String(instruction || "");
        if (!text || text === S.lastInstruction) { return; }
        let parsed = null;
        try { parsed = JSON.parse(text); } catch (e) { parsed = null; }
        if (!parsed || !parsed.nonce) { return; }
        if (S.queued[parsed.nonce]) { return; }
        S.queued[parsed.nonce] = true;
        S.lastInstruction = text;
        if (!parsed.job_id) { return; }
        const how = fromWatcher ? " (" + fromWatcher + ")" : "";
        // Admitted, durably, and the press is over. A job the server runs
        // needs nothing from this page from here: it is watched, not pumped,
        // and the page may be closed the moment this line is written.
        if (parsed.executor === "server" || (parsed.state && SERVER_STATES.indexOf(String(parsed.state)) !== -1)) {
            note("queue: job " + String(parsed.job_id).slice(0, 8) + " admitted by the server" + how + "; watching");
            watch();
            return;
        }
        note("queue: job " + String(parsed.job_id).slice(0, 8) + " appended by the server" + how + "; pumping");
        if (!pump()) {
            toast("WanGP is not available in this page.", true);
        }
    }

    /**
     * Hold the one event stream this page has, so the grid can be told.
     *
     * THE GRID NOW DEPENDS ON THIS, and nothing else on the tab did. A job
     * opened the stream when there was a job to watch, so an idle page had
     * no stream at all - which was fine while the server re-rendered the
     * grid over the framework's channel and is not fine now: a picture
     * imported from another page, or deleted from this one, would leave
     * every open grid quietly stale until something else made it re-ask.
     *
     * Per the spine's own rule, a page with this tab closed is subscribed to
     * nothing: this bundle is fetched when the tab is first opened, so a
     * session that never opens Clipboard never gets here. A page that has it
     * open holds the one connection it already holds, and the interop
     * module's own lifecycle lets it go while the page is hidden and takes
     * it back on return.
     */
    function watch() {
        const api = interop();
        if (!api || !api.wangp || typeof api.wangp.watch !== "function") { return false; }
        try { api.wangp.watch(); } catch (e) { return false; }
        return true;
    }

    function outboxSentence(job) {
        if (!job) { return ""; }
        const enhanced = job.enhance && job.enhance.state === "done" ? " (with the enhanced prompt)" : "";
        if (job.state === "started") { return "WanGP started generating it" + enhanced + "."; }
        if (job.state === "queued") {
            const depth = job.result && Number.isFinite(job.result.queue_depth) ? job.result.queue_depth : null;
            return "Added to WanGP queue" + enhanced + "." + (depth ? " " + depth + " ahead of it." : "");
        }
        if (job.state === "enhancing") { return "The prompt is being enhanced."; }
        if (job.state === "unconfirmed") { return (job.error && job.error.message) || "WanGP did not confirm that the request was added to the queue."; }
        if (job.state === "failed") { return (job.error && job.error.message) || "The request was not queued."; }
        if (job.state === "cancelled") { return (job.error && job.error.message) || "The request was cancelled."; }
        return "";
    }

    function refreshOutbox() {
        if (S.outboxRefreshTimer) { return; }
        S.outboxRefreshTimer = setTimeout(function () {
            S.outboxRefreshTimer = null;
            askQueue(null);
        }, OUTBOX_REFRESH_THROTTLE_MS);
    }

    /** The public API says a job moved: show it, and let the server re-render the list. */
    function onOutboxEvent(event) {
        const detail = event && event.detail ? event.detail : {};
        // The library moved. Advisory: it says so and nothing else, so the
        // page re-asks for the page it is showing. See onLibraryEvent.
        if (detail.kind === "library") { onLibraryEvent(detail.detail || {}); return; }
        const job = detail.job || null;
        if (detail.kind === "done" && job) {
            const failed = job.state !== "queued" && job.state !== "started";
            const ignored = (job.result && job.result.ignored || []).map(function (item) { return item.field; });
            toast(outboxSentence(job) + (!failed && ignored.length ? " " + ignored.map(function (f) {
                return f === "references" ? "Reference" : f === "start" ? "First frame" : f === "end" ? "Last frame" : "Prompt";
            }).join(", ") + " was not used by the current model." : ""), failed);
            refreshCapabilities(true);
        }
        // "waiting" arrives every few seconds while the line ahead is held -
        // a prompt still being written, another page's turn - and "tracked"
        // whenever a queued task moved in WanGP; both are news for the list.
        refreshOutbox();
    }

    /* ------------------------------------------------------------------ */
    /* The queue list, the history, and the press that fills them            */
    /* ------------------------------------------------------------------ */

    /**
     * THE SCREEN THAT ANSWERS "I CLOSED THE BROWSER AND CAME BACK".
     *
     * The jobs already survived that: the outbox is work Forge owns, and the
     * browser's only job is to describe what the user wants and get a durable
     * acknowledgement before it disappears. What did not survive was the VIEW
     * of it - the list was Gradio-rendered, so a page that came back needed
     * the framework's channel to show a job that had run perfectly well
     * without it.
     *
     * Updates still arrive on the event spine, which is why nothing here
     * polls. Only the drawing moved: fetch instead of press, build nodes
     * instead of receive markup. Every sentence in the answer was composed
     * by ``outbox_view`` on the server, where the rest of this tab's wording
     * lives.
     */
    const QUEUE_ROUTE = "/minipaint-clipboard/queue";

    function queueHost() { return byId(OUTBOX_LIST_ID); }

    function listMount(host, className) {
        if (!host) { return null; }
        let mount = host.querySelector("." + className);
        if (mount) { return mount; }
        mount = document.createElement("div");
        mount.className = className;
        host.appendChild(mount);
        return mount;
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) { node.className = className; }
        if (text !== undefined && text !== null) { node.textContent = String(text); }
        return node;
    }

    function excerptNodes(prompt) {
        const nodes = [];
        if (!prompt || prompt.inherit) {
            nodes.push(el("div", "minipaint-clip-job-prompt minipaint-clip-inherit-text", "Prompt: Use WanGP"));
            return nodes;
        }
        if (prompt.typed !== undefined && prompt.enhanced !== undefined) {
            const typed = el("div", "minipaint-clip-job-prompt minipaint-clip-job-prompt-typed",
                             prompt.typed.slice(0, 90) + (prompt.typed.length > 90 ? "…" : ""));
            typed.title = prompt.typed;
            const written = el("div", "minipaint-clip-job-prompt minipaint-clip-job-prompt-enhanced",
                               prompt.enhanced.slice(0, 120) + (prompt.enhanced.length > 120 ? "…" : ""));
            written.title = prompt.enhanced;
            nodes.push(typed, written);
            return nodes;
        }
        const text = String(prompt.text || "");
        const one = el("div", "minipaint-clip-job-prompt", text.slice(0, 90) + (text.length > 90 ? "…" : ""));
        one.title = text;
        nodes.push(one);
        return nodes;
    }

    function jobNode(job) {
        const card = el("div", "minipaint-clip-job minipaint-clip-job-" + job.state);
        card.dataset.job = job.job_id;
        card.dataset.mine = job.mine ? "1" : "0";
        const head = el("div", "minipaint-clip-job-head");
        head.appendChild(el("span", "minipaint-clip-job-state", job.state_label));
        head.appendChild(el("span", "minipaint-clip-job-when", job.when));
        for (const badge of job.badges || []) {
            const mark = el("span", "minipaint-clip-badge" + (badge.kind === "warn" ? " minipaint-clip-badge-warn" : ""), badge.text);
            if (badge.title) { mark.title = badge.title; }
            head.appendChild(mark);
        }
        card.appendChild(head);
        for (const node of excerptNodes(job.prompt)) { card.appendChild(node); }
        card.appendChild(el("div", "minipaint-clip-job-fields", job.fields));
        card.appendChild(el("div", "minipaint-clip-job-outcome", job.outcome));
        for (const line of job.lines || []) {
            const row = el("div", "minipaint-clip-job-line");
            row.dataset.live = line.live || "";
            if (line.wangp) { row.dataset.wangp = line.wangp; }
            const label = el("b", "", line.label + ":");
            row.appendChild(label);
            row.appendChild(document.createTextNode(" " + line.text));
            card.appendChild(row);
        }
        if ((job.actions || []).length) {
            const row = el("div", "minipaint-clip-job-actions");
            for (const action of job.actions) {
                const button = el("button", "", action.label);
                button.type = "button";
                button.dataset.outboxAction = action.verb + ":" + job.job_id;
                if (action.title) { button.title = action.title; }
                row.appendChild(button);
            }
            card.appendChild(row);
        }
        return card;
    }

    function drawQueue(answer) {
        const mount = listMount(queueHost(), "minipaint-clip-outbox");
        if (!mount) { return; }
        // An answer that carries no list is an answer about something else -
        // a press this tab refused before it stored anything, say. It must
        // leave the list alone: emptying it would tell the user that the
        // queue they can see had gone, which is a different and untrue thing
        // from what just happened.
        if (!Array.isArray(answer.jobs)) {
            if (answer.history !== undefined) { drawHistory(answer.history); }
            if (answer.queue_button) { applyQueueButton(answer.queue_button); }
            if (answer.status) { setQueueStatus(answer.status); }
            return;
        }
        const jobs = answer.jobs;
        // Redrawn wholesale, and that is fine HERE in a way it was not
        // before: this list is short, bounded and rebuilt from one small
        // answer, and it is not the thing whose scroll position and decoded
        // pictures had to survive. The grid is; see drawLibrary.
        mount.innerHTML = "";
        mount.dataset.count = String(jobs.length);
        mount.classList.toggle("minipaint-clip-empty", !jobs.length);
        if (!jobs.length) {
            mount.appendChild(el("p", "", "No request has been sent from here yet."));
        } else {
            for (const job of jobs) { mount.appendChild(jobNode(job)); }
        }
        if (answer.history !== undefined) { drawHistory(answer.history); }
        if (answer.queue_button) { applyQueueButton(answer.queue_button); }
        if (answer.status) { setQueueStatus(answer.status); }
    }

    function drawHistory(entries) {
        const mount = listMount(byId(HISTORY_LIST_ID), "minipaint-clip-history");
        if (!mount) { return; }
        const list = Array.isArray(entries) ? entries : [];
        mount.innerHTML = "";
        mount.classList.toggle("minipaint-clip-empty", !list.length);
        if (!list.length) {
            mount.appendChild(el("p", "", "No request has been confirmed queued from here yet."));
            return;
        }
        for (const record of list) {
            const entry = el("div", "minipaint-clip-history-entry");
            entry.dataset.history = record.history_id;
            const head = el("div", "minipaint-clip-history-head");
            head.appendChild(el("span", "minipaint-clip-history-when", record.when));
            head.appendChild(el("span", "minipaint-clip-history-model", record.model));
            head.appendChild(el("span", "minipaint-clip-history-tasks",
                                 record.tasks + " task" + (record.tasks === 1 ? "" : "s")));
            entry.appendChild(head);
            const prompt = record.prompt || {};
            if (prompt.inherit) {
                entry.appendChild(el("div", "minipaint-clip-history-prompt minipaint-clip-inherit-text", "Prompt: Use WanGP"));
            } else {
                const typed = el("div", "minipaint-clip-history-prompt", prompt.typed || "");
                typed.title = prompt.typed || "";
                entry.appendChild(typed);
                if (prompt.enhanced) {
                    const written = el("div", "minipaint-clip-history-enhanced",
                                       "enhanced: " + prompt.enhanced.slice(0, 160) + (prompt.enhanced.length > 160 ? "…" : ""));
                    written.title = prompt.enhanced;
                    entry.appendChild(written);
                }
            }
            const thumbs = el("div", "minipaint-clip-history-thumbs");
            for (const slot of record.slots || []) {
                if (slot.state === "picture") {
                    const holder = el("span", "minipaint-clip-history-thumb");
                    holder.title = slot.label + ": " + slot.name;
                    const picture = document.createElement("img");
                    picture.src = slot.url;
                    picture.alt = "";
                    picture.draggable = false;
                    holder.appendChild(picture);
                    holder.appendChild(el("small", "", slot.label));
                    thumbs.appendChild(holder);
                } else if (slot.state === "missing") {
                    thumbs.appendChild(el("span", "minipaint-clip-badge minipaint-clip-badge-missing", slot.label + ": Missing image"));
                } else if (slot.state === "ignored") {
                    thumbs.appendChild(el("span", "minipaint-clip-badge minipaint-clip-badge-unsupported", slot.label + " was ignored"));
                } else {
                    thumbs.appendChild(el("span", "minipaint-clip-badge", slot.label + ": Use WanGP"));
                }
            }
            entry.appendChild(thumbs);
            const actions = el("div", "minipaint-clip-history-actions");
            for (const pair of [["load", "Load"], ["delete", "Delete"]]) {
                const button = el("button", "", pair[1]);
                button.type = "button";
                button.dataset.historyAction = pair[0] + ":" + record.history_id;
                actions.appendChild(button);
            }
            entry.appendChild(actions);
            mount.appendChild(entry);
        }
    }

    function applyQueueButton(state) {
        const host = byId(QUEUE_BUTTON_ID);
        const button = host ? (host.tagName === "BUTTON" ? host : host.querySelector("button")) : null;
        if (!button) { return; }
        button.textContent = state.label;
        button.disabled = !state.enabled;
    }

    function setQueueStatus(text) {
        const host = byId(QUEUE_STATUS_ID);
        if (!host || !text) { return; }
        statusTarget(host).innerHTML = String(text);
    }

    //: How long one ask of the queue route may hold a connection before the
    //: page gives up on it. Generous for a server that is merely busy, and a
    //: hard bound for one that is not answering: a request that waits for
    //: ever is a browser connection held for ever, and a page has six.
    const QUEUE_ASK_TIMEOUT_MS = 25000;

    /** One call for every way the queue section changes.

     * Reads coalesce and everything is bounded, and the day that mattered
     * is why: a server that stopped answering left twenty-four reads of this
     * route in flight at once, each holding one of the six connections a
     * browser allows to an origin, and every later request from the page -
     * a generation, a button, this tab's own journal - queued behind them
     * until the browser was restarted. One read at a time, for at most
     * QUEUE_ASK_TIMEOUT_MS, is a page that can still say "the server is not
     * answering" and then ask again when it is. */
    function askQueue(body) {
        if (!body && S.queueRead) { return S.queueRead; }
        let controller = null;
        let cutoff = 0;
        try {
            controller = typeof AbortController === "function" ? new AbortController() : null;
        } catch (e) { controller = null; }
        if (controller) {
            cutoff = setTimeout(function () {
                try { controller.abort(); } catch (e) { /* already settled */ }
            }, QUEUE_ASK_TIMEOUT_MS);
        }
        const signal = controller ? { signal: controller.signal } : {};
        const request = body
            ? fetch(QUEUE_ROUTE, Object.assign({
                method: "POST", credentials: "same-origin", cache: "no-store",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(Object.assign({ page: pageId() }, body))
            }, signal))
            : fetch(QUEUE_ROUTE + "?page=" + encodeURIComponent(pageId()), Object.assign({ credentials: "same-origin", cache: "no-store" }, signal));
        const drawn = request.then(function (response) {
            return response.json().then(function (answer) { return { response: response, answer: answer }; });
        }).then(function (pair) {
            const answer = pair.answer || {};
            if (!pair.response.ok && answer.ok !== true && !answer.status) {
                throw new Error(answer.message || ("HTTP " + pair.response.status));
            }
            serverSilent(false);
            drawQueue(answer);
            return answer;
        }, function (error) {
            const aborted = !!(error && (error.name === "AbortError"));
            note("queue: could not be read (" + (aborted ? "no answer within " + QUEUE_ASK_TIMEOUT_MS + " ms" : ((error && error.message) || error)) + ")");
            serverSilent(true, "the queue");
            return null;
        });
        // Whatever became of it - answered, refused, aborted, or a drawing
        // that threw - the read is over: the timer goes and the slot is
        // free. A slot left set by a rejection would make every later ask
        // return that same dead promise, and the section would never read
        // again for the life of the page.
        const finish = function () {
            if (cutoff) { clearTimeout(cutoff); }
            if (!body && S.queueRead === settled) { S.queueRead = null; }
        };
        const settled = drawn.then(
            function (answer) { finish(); return answer; },
            function (error) { finish(); throw error; }
        );
        if (!body) { S.queueRead = settled; }
        return settled;
    }

    /**
     * Add to Queue, over this tab's own route.
     *
     * The prompt as typed and the switch as it stands travel with the press,
     * because both live in the browser and the stored setting is only ever a
     * copy of one of them - see ``add_to_queue`` for what a checkbox that
     * does not decide cost. The page's identity and the WanGP model go with
     * it too, for the same reason: this page is the only thing that knows
     * either.
     */
    function addToQueue(prompt, enhanceOn) {
        let model = null;
        try { model = JSON.parse(modelJson() || "null"); } catch (e) { model = null; }
        return askQueue({
            action: "add",
            prompt: String(prompt === undefined || prompt === null ? promptValue() : prompt),
            enhance: typeof enhanceOn === "boolean" ? enhanceOn : undefined,
            model: model
        }).then(function (answer) {
            if (!answer) { toast("Forge is not answering; nothing was queued.", true); return null; }
            if (answer.instruction) { queue(JSON.stringify(answer.instruction), "the queue route"); }
            return answer;
        });
    }

    function promptValue() {
        const host = byId("minipaint_clipboard_prompt");
        const box = host ? host.querySelector("textarea, input") : null;
        return box ? String(box.value || "") : "";
    }

    function cancelAll() {
        return askQueue({ action: "cancel_all" }).then(function (answer) {
            const api = interop();
            if (api && api.wangp && typeof api.wangp.refreshWaiters === "function") {
                try { api.wangp.refreshWaiters(); } catch (e) { /* the callers' business */ }
            }
            return answer;
        });
    }

    function jobAction(verb, jobId) {
        return askQueue({ action: verb, job: jobId }).then(function (answer) {
            if (verb === "retry" || verb === "adopt") { setTimeout(pump, 400); }
            return answer;
        });
    }

    /* ------------------------------------------------------------------ */
    /* WanGP availability                                                    */
    /* ------------------------------------------------------------------ */

    function setLine(text, state) {
        const host = byId(WANGP_LINE_ID);
        const line = host ? host.querySelector(".minipaint-clip-wangp-line") : null;
        if (!line) { return; }
        line.textContent = text;
        line.dataset.state = state || "unknown";
    }

    function refreshBadges() {
        const caps = S.capabilities;
        for (const slot in SLOT_FIELDS) {
            const host = byId("minipaint_clipboard_card_" + slot);
            const badge = host ? host.querySelector(".minipaint-clip-badge-unsupported") : null;
            const card = host ? host.querySelector(".minipaint-clip-card") : null;
            if (!badge || !card) { continue; }
            const filled = card.classList.contains("minipaint-clip-card-override");
            const known = !!(caps && caps.ok && caps.inputs);
            const supported = known ? !!(caps.inputs[SLOT_FIELDS[slot]] && caps.inputs[SLOT_FIELDS[slot]].supported) : true;
            badge.hidden = !(filled && known && !supported);
        }
    }

    /** The model the page is on, as the capabilities answer named it, into
     * the hidden model box - once per change, so the server's line about
     * enhanced prompts follows the WanGP tab without a timer. */
    function sendModel(answer) {
        const raw = answer && answer.ok && answer.model && typeof answer.model === "object" ? answer.model : null;
        const text = raw ? JSON.stringify({ type: String(raw.type || "").slice(0, 120), label: String(raw.label || "").slice(0, 120),
                                             family: String(raw.family || "").slice(0, 120), architecture: String(raw.architecture || "").slice(0, 120) }) : "";
        if (text === S.lastModel) { return; }
        S.lastModel = text;
        sendInput(BOXES.model, text);
    }

    /** The model JSON a press carries, so the server writes the prompt for it. */
    function modelJson() {
        return S.lastModel || boxValue(BOXES.model) || "";
    }

    /** One bounded question to the public API, throttled, never on a timer. */
    function refreshCapabilities(force) {
        const api = interop();
        if (!api || typeof api.wangp.capabilities !== "function") { setLine("WanGP: not available in this page", "off"); S.capabilities = null; refreshBadges(); return; }
        if (!force && Date.now() - S.capabilitiesAt < CAPABILITY_THROTTLE_MS) { return; }
        S.capabilitiesAt = Date.now();
        setLine("WanGP: checking…", "checking");
        api.wangp.capabilities().then(function (answer) {
            S.capabilities = answer;
            sendModel(answer);
            if (!answer || !answer.ok) {
                setLine("WanGP unavailable" + (answer && answer.code ? " (" + answer.code + ")" : "") + " — Add to Queue will ask anyway", "off");
            } else {
                const inputs = answer.inputs || {};
                const takes = ["start", "end", "references"].filter(function (f) { return inputs[f] && inputs[f].supported; });
                const label = (answer.model && (answer.model.label || answer.model.type)) || "model";
                setLine("WanGP: " + label + " · takes " + (takes.length ? takes.map(function (f) { return f === "start" ? "first" : f === "end" ? "last" : "reference"; }).join(", ") : "no image")
                    + (answer.generation_running === true ? " · generating now, new requests join the run" : answer.start === true ? " · idle, the next request starts a run" : "")
                    + (answer.queue === false ? " · queue not offered by this bridge" : answer.start === false ? " · this bridge queues but cannot start a run" : ""),
                    answer.queue === false ? "off" : "ready");
            }
            refreshBadges();
        }, function () {
            setLine("WanGP unavailable — Add to Queue will ask anyway", "off");
            S.capabilities = null;
            refreshBadges();
        });
    }

    /* ------------------------------------------------------------------ */
    /* Attach                                                                */
    /* ------------------------------------------------------------------ */

    function onRootClick(event) {
        const target = event.target;
        if (!target || !target.closest) { return; }
        const item = target.closest(".minipaint-clip-item");
        if (item) {
            select(item.dataset.asset === S.selected ? "" : item.dataset.asset);
            return;
        }
        const clear = target.closest(".minipaint-clip-card-clear");
        if (clear) {
            event.preventDefault();
            sendInput(BOXES.slotAction, "clear:" + clear.dataset.clear + ":" + Date.now());
            return;
        }
        const choose = target.closest(".minipaint-clip-card-choose");
        if (choose) {
            event.preventDefault();
            pressHidden(SLOT_UPLOAD_PREFIX + choose.dataset.choose);
            return;
        }
        const action = target.closest("[data-history-action]");
        if (action) {
            event.preventDefault();
            // Load puts a recipe back into the composer, whose slot cards are
            // still rendered by the framework - so this one still crosses it.
            // See the V2 list. The list itself is re-read over HTTP either way.
            sendInput(BOXES.historyAction, action.dataset.historyAction + ":" + Date.now());
            setTimeout(function () { askQueue(null); }, 300);
            return;
        }
        const outbox = target.closest("[data-outbox-action]");
        if (outbox) {
            event.preventDefault();
            const parts = String(outbox.dataset.outboxAction || "").split(":");
            jobAction(parts[0], parts[1] || "");
        }
    }

    function onKeydown(event) {
        const item = event.target && event.target.closest ? event.target.closest(".minipaint-clip-item") : null;
        if (!item) { return; }
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(item.dataset.asset); }
    }

    function watchTab() {
        const nav = app().querySelector ? app().querySelector("#tabs > .tab-nav") : null;
        if (!nav || nav.dataset.minipaintClipboardWatch) { return; }
        nav.dataset.minipaintClipboardWatch = "1";
        nav.addEventListener("click", function (event) {
            const button = event.target && event.target.closest ? event.target.closest("button") : null;
            if (!button) { return; }
            setTimeout(function () {
                if (tabVisible()) {
                    // A tab that was hidden was not measured while it was.
                    if (S.fitRewatch) { S.fitRewatch(); }
                    scheduleFit();
                    // Over HTTP: coming back to this tab must not depend on
                    // a framework channel that a backgrounded tab has lost.
                    // With a folder re-read, because coming back to the tab
                    // is exactly when the folder may have moved underneath it.
                    watch();
                    fetchLibrary({ quiet: true, refresh: true });
                    refreshCapabilities(false);
                }
            }, 50);
        });
    }

    function attach() {
        const element = root();
        if (!element || S.attached) { return; }
        S.attached = true;
        element.addEventListener("click", onRootClick);
        element.addEventListener("keydown", onKeydown);
        bindDrop(element);
        buildMenu();
        if (!S.pasteListener) {
            S.pasteListener = onDocumentPaste;
            document.addEventListener("paste", S.pasteListener);
        }
        if (!S.outboxListener) {
            S.outboxListener = onOutboxEvent;
            document.addEventListener("minipaint:outbox", S.outboxListener);
        }
        if (!S.galleryListener) {
            // Capture phase, on the document: it runs before the framework's
            // own handler on the button whatever order the two were bound
            // in, and it survives the button being rebuilt by a Reload UI.
            S.galleryListener = onGalleryButton;
            document.addEventListener("click", S.galleryListener, true);
        }
        // A standing fault worth saying before anything is tried, rather than
        // after the first thing fails: a host that tells its own page to call
        // it somewhere the browser will not let the page call. Nothing this
        // tab does can work around it, and nothing else reports it - the page
        // loads, and only the framework's own requests are refused.
        try {
            const hostRoot = window.minipaintHostRoot && window.minipaintHostRoot();
            if (hostRoot && hostRoot.known && !hostRoot.ok) { note("host: " + hostRoot.note); }
        } catch (e) { /* never worth an exception */ }
        // The page's identity, so a press submits under it; and the jobs this
        // page composed before a reload resume without another press - as
        // does the tracking of the ones WanGP already took from this page.
        element.addEventListener("keydown", onLibraryKey);
        watchTab();
        // The grid, from the index route, before anything else is asked of
        // the server: it is the thing the user is looking at. The stream
        // first, so a change that lands between these two is not missed.
        watch();
        gridMount();
        watchFit();
        scheduleFit();
        fetchLibrary({ quiet: true, refresh: true });
        // And the queue: what ran while this browser was closed, from the
        // route rather than from a framework render that a closed browser
        // could never have received.
        askQueue(null);
        afterRender();
        if (tabVisible()) { refreshCapabilities(false); }
        setTimeout(pump, 250);
        setTimeout(function () {
            const api = interop();
            if (api && api.wangp && typeof api.wangp.resumeTracking === "function") {
                try { api.wangp.resumeTracking(); } catch (e) { /* bounded there */ }
            }
        }, 1500);
    }

    function debug() {
        return { attached: S.attached, selected: S.selected, menuOpen: !!(S.menu && !S.menu.hidden), menuSection: S.menuSection,
                 menuStandalone: S.menuStandalone,
                 capabilities: S.capabilities, lastInstruction: S.lastInstruction.slice(0, 40), page: pageId(),
                 model: S.lastModel, retrying: S.retrying || !!S.retryTimer || S.retryPending,
                 retryDelay: S.retryDelay,
                 offline: { server: S.offline.server, queue: S.offline.queue },
                 editorOpen: editorOpen(),
                 outputsOpen: outputsOpen(),
                 outputs: { page: S.outputs.page, pages: S.outputs.pages,
                            items: S.outputs.items.length, chosen: S.outputs.chosen,
                            drawn: S.outputs.drawn || 0,
                            player: !!S.outputs.controls, clock: !!S.outputs.clock },
                 intercept: interceptTarget() === "clipboard",
                 interceptTarget: interceptTarget(),
                 takeovers: S.takeovers,
                 sort: menuState().sort || S.library.sort,
                 library: { revision: S.library.revision, page: S.library.page, pages: S.library.pages,
                            total: S.library.total, shown: S.library.ids.length, busy: S.library.busy },
                 tileReport: S.tileReport,
                 fitted: S.fitted || null };
    }

    return {
        attach: attach,
        afterRender: afterRender,
        menuStateChanged: menuStateChanged,
        openPromptEditor: openPromptEditor,
        closePromptEditor: closePromptEditor,
        library: fetchLibrary,
        goToPage: goToPage,
        setSort: setSort,
        libraryState: function () {
            return { revision: S.library.revision, sort: S.library.sort, page: S.library.page,
                     pages: S.library.pages, total: S.library.total, size: S.library.size,
                     shown: S.library.ids.length, busy: S.library.busy,
                     selectedPage: S.library.selectedPage };
        },
        toggleMenu: toggleMenu,
        openToolbarMenu: openToolbarMenu,
        openOutputs: openOutputs,
        closeOutputs: closeOutputs,
        askOutputs: askOutputs,
        loadRecipe: loadRecipe,
        fit: fitTab,
        closeMenu: closeMenu,
        select: select,
        setThumbnailSize: setThumbnailSize,
        reportTiles: reportTiles,
        pasteFromClipboard: pasteFromClipboard,
        setIntercept: setIntercept,
        interceptTarget: interceptTarget,
        queue: queue,
        addToQueue: addToQueue,
        cancelAll: cancelAll,
        refreshQueue: refreshOutbox,
        pump: pump,
        pageId: pageId,
        modelJson: modelJson,
        refreshCapabilities: refreshCapabilities,
        pressHidden: pressHidden,
        showOffline: connectionNotice,
        receiveOverHttp: receiveOverHttp,
        galleryPictureUrl: galleryPictureUrl,
        debug: debug
    };
})();
