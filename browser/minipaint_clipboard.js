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
 * The WanGP section exists only while WanGP can take what it makes. The
 * readiness route (``targets.readiness`` on the server) says whether WanGP
 * is running, its bridge answering, and the page on one of the four models
 * this sends to, defined and downloaded; this file writes the answer onto
 * the tab root as ``data-wangp-ready``, ``data-wangp-target`` and
 * ``data-wangp-references`` and the stylesheet does the rest - no cards, no
 * prompt, no enhancement, no Add to Queue and no +First / +Last / +Ref while
 * it is not, one quiet line with Check again in their place, and no
 * reference card for a model that is sent none (LTX 2.3 and 2.5 Distilled,
 * by the server's own list of what each model is sent).
 * It is asked when the tab opens, when the page learns WanGP's model, after
 * a job ends, on Check again and in every refused press's answer; never on
 * a timer. The press is checked again on the server, fresh, whatever this
 * page last drew.
 *
 * The grid shows the storage folder, paged, or - from Menu → Archives - an
 * archive in its place: a read-only folder on the Forge host listed whole,
 * its tiles empty places until they are in view, its thumbnails asked for in
 * view first and re-ordered whenever the view moves (see "Archives"). Any
 * tile of either opens its picture full screen from the small button in its
 * corner, the one time a whole picture is loaded.
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
    //: Matches the stylesheet's own default for --minipaint-clip-thumb, and
    //: is the size a session's first row is counted from: as many pictures
    //: per row as come nearest to it. See applyPerRow.
    const DEFAULT_THUMB = 144;
    //: The smallest and largest tile the per-row slider reaches, as the
    //: host's config names them (THUMBNAIL_MIN and THUMBNAIL_MAX; a check in
    //: test_clipboard_ui.py holds the copies equal): below the first a
    //: picture is a smudge, above the second it is larger than the thumbnail
    //: the server makes, 320 on its longer side.
    const THUMB_MIN = 72;
    const THUMB_MAX = 320;
    //: Where this browser tab keeps its pictures-per-row between reloads. Per
    //: session by the user's word: a new tab starts filled at the default.
    const PER_ROW_KEY = "minipaint-clipboard:per-row";
    //: The tile's frame on one axis - 4px of padding and a 2px border, twice
    //: - and the strip under the square that carries the name. A column is
    //: the thumbnail plus the first, a row is the thumbnail plus the second.
    //: The stylesheet declares the same two numbers, as
    //: --minipaint-clip-frame and --minipaint-clip-caption, for the rules
    //: that document the grid; test_clipboard_ui.py holds the two copies
    //: equal. Why the script states them at all is above pin().
    const TILE_FRAME = 12;
    const TILE_CAPTION = 32;
    //: The smallest tile that shows its full-screen button: below it the
    //: button would cover too much of the picture it opens.
    const EXPAND_MIN_THUMB = 120;
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
        //: The server's last readiness answer for the WanGP section, the
        //: read in flight, and the last line the journal was given about it.
        readiness: null,
        readinessRead: null,
        readinessSaid: "",
        //: Set when a send went unanswered, cleared by the next
        //: acknowledgement: how long the page is willing to wait next time.
        queueDown: false,
        //: The timer and the watcher looking for the server's receipt for a
        //: send. See watchSend.
        sendWatch: null,
        sendPoll: null,
        //: The thumbnail size in pixels, worked out from the pictures per
        //: row and the grid's width (applyPerRow) - fractional, so a row is
        //: filled to the pixel.
        thumb: 0,
        //: Pictures per row: what is drawn, what the slider asked for this
        //: session (0 for the default), and the counts the width allows.
        perRow: 0,
        perRowWanted: 0,
        perRowRange: null,
        //: The grid whose size and scroll are watched (watchGrid), and its
        //: observer.
        gridWatched: null,
        gridObserver: null,
        //: The scrubber: the hold while it is pressed, and its next frame.
        scrub: null,
        scrubFrame: 0,
        menu: null,
        menuSection: null,
        //: The element the flyout is drawn under. The menu button when the
        //: menu opened it, a toolbar button when one of those did.
        menuAnchor: null,
        //: The gallery's own working set: what this page of it holds, which
        //: one is in the stage, and the element playing it.
        outputs: { items: [], page: 0, pages: 1, chosen: "", stage: null, strip: null, player: null },
        outputsKey: null,
        //: The History dialog: its nodes, the one read of its list in flight,
        //: the Load in flight, how many entries it drew, and its Escape.
        prompts: { dom: null, read: null, loading: "", entries: 0, drawn: 0 },
        promptsKey: null,
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
        },
        //: An archive shown in place of the storage folder. See "Archives".
        archive: {
            //: The archive's id, or "" while the storage folder is shown.
            view: "",
            mounted: false,
            //: Every picture: [key, name, bytes, version], in the order asked.
            items: [],
            //: key -> position in items.
            index: new Map(),
            sort: "",
            busy: false,
            ticket: 0,
            //: The worked-out grid (columns, cell, gap, rows, height).
            layout: null,
            //: position -> tile, for the rows the document holds.
            rendered: new Map(),
            //: key -> { url, state, tries }: the thumbnails this page has.
            thumbs: new Map(),
            //: key -> { controller, timer, ... }: the requests out now.
            inflight: new Map(),
            scrolledAt: 0,
            //: The reader's place, kept for the grid's return from a hidden tab.
            scrollTop: 0,
            startedAt: 0,
            pumpTimer: 0,
            frame: 0,
            watched: null,
            observer: null,
            //: What the loader did, for the journal and the checks: how many
            //: thumbnails it asked for, dropped, got and gave up on, and the
            //: order it asked for the last few hundred in.
            stats: { started: 0, dropped: 0, done: 0, failed: 0, order: [] }
        },
        //: The Archives screen, and its Escape.
        archivesDom: null,
        archivesKey: null,
        //: The full-screen view of one picture, and the folder it steps through.
        viewer: null,
        //: The last tap on a tile, for the double tap. See isDoubleTap.
        lastTileTap: null
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
            // An archive is read-only: its pictures can be sent and put on a
            // card (as a copy in the storage folder), never deleted.
            if (button) { button.disabled = !S.selected || (id === PRESS.deleteNow && isArchiveRef(S.selected)); }
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
        if (archiveActive()) {
            // The grid is an archive's: its tiles are drawn only near the
            // view, so "not on the grid" means nothing here. A selection is
            // kept while the archive still lists it.
            const confirmed = boxValue(BOXES.selected);
            let keep = S.selected;
            const listed = function (id) {
                return isArchiveRef(id) && id.indexOf(ARCHIVE_REF_PREFIX + S.archive.view + "-") === 0
                    && S.archive.index.has(id.slice(ARCHIVE_REF_PREFIX.length + 17));
            };
            if (keep && !listed(keep) && S.archive.items.length) { keep = ""; }
            if (!keep && listed(confirmed)) { keep = confirmed; }
            select(keep, true);
            S.renderedAt = Date.now();
            if (confirmed !== keep) { sendInput(BOXES.selected, keep); }
            applyPerRow();
            refreshBadges();
            scheduleFit();
            scheduleScrubber();
            return;
        }
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
        applyPerRow();
        refreshBadges();
        // Once the pictures have had a frame to lay out: measured before
        // that, every tile is "off-centre" because nothing has been drawn.
        setTimeout(reportTiles, 400);
        scheduleFit();
        scheduleScrubber();
    }

    /** A Gradio round trip landed on this page, whatever it carried. */
    function menuStateChanged() {
        S.gradioSeenAt = Date.now();
        if (S.offline.queue) { connectionNotice(false); S.queueDown = false; }
    }

    /* -- pictures per row --------------------------------------------- */
    //
    // THE SLIDER COUNTS PICTURES, NOT PIXELS. It used to name a tile size in
    // steps of 8 px, and the columns were that size and never stretched, so
    // almost every size left a strip of empty grid at the right-hand end of
    // every row. Asked for: "slide based on number of images that can fit
    // one row without empty space". So the slider's value is how many
    // pictures a row holds, the columns are that many equal shares of the
    // row, and the tile size is worked out from the row: the width less the
    // gaps, shared out, less the tile's frame. Every row is filled to the
    // pixel at every count, and the picture keeps its shape inside its square
    // (object-fit: contain, PICTURE_GEOMETRY) - nothing is cut off and
    // nothing is stretched.
    //
    // The counts the slider offers are the ones whose tiles come out between
    // THUMB_MIN and THUMB_MAX at this width, and they move with the width. A
    // new session starts at the count nearest DEFAULT_THUMB, and follows the
    // width while it is still on that default; a count the slider chose is
    // this browser tab's (sessionStorage) and is kept through a resize,
    // within the counts the new width allows.
    //
    // The grid's own scroll bar is not drawn (the scrubber is the grid's
    // bar), so the width a row has does not change with what it holds, and
    // sizing the tiles can never take a scroll bar away and change the width
    // it was sized from.

    /** The width a row has, and the gap between its pictures. */
    function rowRoom(grid) {
        const style = getComputedStyle(grid);
        const gap = parseFloat(style.columnGap) || parseFloat(style.rowGap) || 8;
        const room = (grid.clientWidth || 0) - (parseFloat(style.paddingLeft) || 0) - (parseFloat(style.paddingRight) || 0);
        return { room: room, gap: gap };
    }

    /** The counts a row of this width may hold: tiles from THUMB_MAX down to THUMB_MIN. */
    function perRowRange(room, gap) {
        const fewest = Math.max(1, Math.ceil((room + gap) / (THUMB_MAX + TILE_FRAME + gap) - 1e-9));
        const most = Math.max(fewest, Math.floor((room + gap) / (THUMB_MIN + TILE_FRAME + gap) + 1e-9));
        return { min: fewest, max: most };
    }

    /** The picture's square when `count` tiles share the row exactly. */
    function thumbFor(room, gap, count) {
        return Math.max(1, (room - (count - 1) * gap) / count - TILE_FRAME);
    }

    function clampCount(count, range) {
        return Math.min(range.max, Math.max(range.min, Math.round(Number(count) || 0)));
    }

    function sessionPerRow() {
        try {
            const kept = Number(window.sessionStorage.getItem(PER_ROW_KEY));
            return kept >= 1 ? Math.round(kept) : 0;
        } catch (e) {
            return 0;
        }
    }

    function keepPerRow(count) {
        try { window.sessionStorage.setItem(PER_ROW_KEY, String(count)); } catch (e) { /* this tab only, then */ }
    }

    function thumbSlider() {
        const host = byId("minipaint_clipboard_thumb");
        return {
            range: host ? host.querySelector("input[type=range]") : null,
            number: host ? host.querySelector("input[type=number]") : null
        };
    }

    /**
     * Show the count and the counts there are on the slider.
     *
     * The framework's slider keeps its value in its own state, which follows
     * the range input's `input` event, and draws its filled track from the
     * input's own min and max - so the limits are written on the inputs and
     * a changed value is handed over as an input event. That event comes
     * back as the slider's change, carrying the count that is already drawn,
     * which setPerRow takes for the echo it is.
     */
    function syncSlider(count, range) {
        const slider = thumbSlider();
        for (const input of [slider.range, slider.number]) {
            if (!input) { continue; }
            if (input.min !== String(range.min)) { input.min = String(range.min); }
            if (input.max !== String(range.max)) { input.max = String(range.max); }
            if (input.step !== "1") { input.step = "1"; }
            input.title = count + " picture" + (count === 1 ? "" : "s") + " per row";
        }
        const input = slider.range;
        if (!input) { return; }
        if (Number(input.value) !== count) {
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value");
            if (setter && setter.set) { setter.set.call(input, String(count)); } else { input.value = String(count); }
            input.dispatchEvent(new Event("input", { bubbles: true }));
        }
        // The filled part of the track, as the framework draws it, for limits
        // it did not set itself.
        const span = range.max - range.min;
        input.style.backgroundSize = (span > 0 ? ((count - range.min) / span) * 100 : 100) + "% 100%";
        if (slider.number && Number(slider.number.value) !== count) { slider.number.value = String(count); }
    }

    /**
     * Fill the row: the count asked for (or the session's, or the default),
     * held to what this width allows, and the tile size it makes.
     *
     * On the grid itself, not on the Gradio block around it. Gradio renders
     * an HTML component's elem_classes onto both the outer block and the
     * inner "prose" div, so the stylesheet's default matched twice and the
     * inner match re-declared the variable - which meant a value written to
     * the outer element was shadowed before it ever reached the grid, and
     * the slider moved nothing. The grid is the element that reads the
     * variables, so it is the element that gets written.
     */
    function applyPerRow(wanted) {
        if (wanted) { S.perRowWanted = Math.max(1, Math.round(Number(wanted) || 1)); }
        if (!S.perRowWanted) { S.perRowWanted = sessionPerRow(); }
        const host = byId(GRID_ID);
        const grid = (host && host.querySelector(".minipaint-clip-grid"))
            || app().querySelector(".minipaint-clip-grid");
        if (!grid) { return false; }
        const measured = rowRoom(grid);
        // Hidden with its tab, the grid has no width: it keeps the size it
        // had, and the grid's resize observer sizes it again on its return.
        if (!(measured.room > 0)) {
            if (!S.thumb) { S.thumb = DEFAULT_THUMB; }
            return false;
        }
        const range = perRowRange(measured.room, measured.gap);
        const count = S.perRowWanted
            ? clampCount(S.perRowWanted, range)
            : clampCount((measured.room + measured.gap) / (DEFAULT_THUMB + TILE_FRAME + measured.gap), range);
        const thumb = thumbFor(measured.room, measured.gap, count);
        S.thumb = thumb;
        S.perRow = count;
        S.perRowRange = range;
        // Marked important like the rest of the grid's geometry (see pin),
        // so the two variables every track and ceiling below is measured
        // from cannot be restated by a stylesheet either.
        const size = (Math.round(thumb * 100) / 100) + "px";
        let moved = false;
        if (grid.style.getPropertyValue("--minipaint-clip-thumb") !== size) {
            grid.style.setProperty("--minipaint-clip-thumb", size, "important");
            moved = true;
        }
        if (grid.style.getPropertyValue("--minipaint-clip-cols") !== String(count)) {
            grid.style.setProperty("--minipaint-clip-cols", String(count), "important");
            moved = true;
        }
        grid.dataset.perRow = String(count);
        // The full-screen button, on tiles large enough to hold it.
        const expand = thumb >= EXPAND_MIN_THUMB ? "1" : "0";
        if (grid.dataset.expand !== expand) { grid.dataset.expand = expand; }
        syncSlider(count, range);
        if (moved && archiveActive()) { onArchiveResize(); }
        if (moved) { scheduleScrubber(); }
        return moved;
    }

    /**
     * The slider moved: that many pictures per row, now, and for the rest
     * of this browser tab's session. Nothing is saved on the host.
     */
    function setPerRow(count) {
        const wanted = Math.round(Number(count) || 0);
        if (!(wanted >= 1)) { return; }
        // The slider's change for the count just drawn: the echo of
        // syncSlider's own event, or a press that changed nothing.
        if (wanted === S.perRow) { return; }
        S.perRowWanted = wanted;
        keepPerRow(wanted);
        applyPerRow(wanted);
        // Held to the width's counts: what is drawn is what is kept.
        if (S.perRow && S.perRow !== wanted) { S.perRowWanted = S.perRow; keepPerRow(S.perRow); }
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
        // An archive's listing has no dimensions: nothing is opened to list it.
        return (item.w && item.h ? item.w + " × " + item.h + " · " : "") + human;
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
     * The thumbnail size and the pictures per row stay the two variables
     * the slider writes onto the grid (applyPerRow); every other length
     * below is a number this file owns. reportTiles says so, with the values that won, if a tile is
     * ever not the size of its cell again.
     */
    function pin(element, declarations) {
        for (const property in declarations) {
            if (Object.prototype.hasOwnProperty.call(declarations, property)) {
                element.style.setProperty(property, declarations[property], "important");
            }
        }
    }

    //: The mount: the grid over its pager, laid out together, and what the
    //: scrubber is placed against.
    const MOUNT_GEOMETRY = {
        "position": "relative",
        "display": "flex",
        "flex-direction": "column",
        "gap": "var(--minipaint-clip-gap, 8px)",
        "box-sizing": "border-box",
        "width": "auto",
        "min-width": "0",
        "max-width": "100%"
    };

    //: The grid: as many equal columns as the slider counts, sharing the row
    //: to the pixel, and rows of the tile size worked out from that share
    //: plus the caption (applyPerRow) - so a column is exactly the tile, its
    //: square exactly the picture's box, and every cell is identical before
    //: any picture has loaded. No scroll bar of its own: the scrubber is the
    //: grid's bar, and a bar that came and went would change the width the
    //: row was shared out from.
    const GRID_GEOMETRY = {
        "display": "grid",
        "grid-template-columns": "repeat(var(--minipaint-clip-cols, 4), minmax(0, 1fr))",
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
        "overflow-y": "auto",
        "scrollbar-width": "none"
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
        "aspect-ratio": "auto",
        //: A double tap is the tile's own (PNG Info), never the browser's zoom.
        "touch-action": "manipulation",
        //: What the full-screen button in the corner is placed against. An
        //: archive's tile is placed absolutely instead, which does the same.
        "position": "relative"
    };

    //: The full-screen button: a small square in the tile's top right corner,
    //: over the picture. Whether it is shown is the stylesheet's (keyed on
    //: the grid's data-expand), so its display is the one thing not here.
    const EXPAND_GEOMETRY = {
        "position": "absolute",
        "top": "6px",
        "right": "6px",
        "left": "auto",
        "bottom": "auto",
        "box-sizing": "border-box",
        "width": "26px",
        "height": "26px",
        "min-width": "0",
        "min-height": "0",
        "margin": "0",
        "padding": "4px",
        "border-width": "1px",
        "border-style": "solid",
        "align-items": "center",
        "justify-content": "center",
        "z-index": "1"
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
        // The row the slider counts, on the grid before its first tile: the
        // tracks above are measured from it, and a page whose stylesheet
        // never arrived would otherwise have nothing to measure them from.
        applyPerRow();
        watchGrid(grid);
        return mount;
    }

    /**
     * The grid's own size, watched: a new width is a new share of the row,
     * a new height a new view for the archive's rows and the scrubber.
     *
     * Only read here. Nothing this writes can change the grid's own box -
     * its height is the fit's (fitTab) and its width the column's - so the
     * observer cannot feed itself: the tiles inside move, the box does not.
     */
    function watchGrid(grid) {
        if (S.gridWatched === grid) { return; }
        S.gridWatched = grid;
        grid.addEventListener("scroll", onGridScroll, { passive: true });
        if (typeof ResizeObserver !== "function") { return; }
        if (S.gridObserver) { S.gridObserver.disconnect(); }
        S.gridObserver = new ResizeObserver(function () {
            applyPerRow();
            if (archiveActive()) { onArchiveResize(); }
            drawScrubber();
        });
        S.gridObserver.observe(grid);
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
    // THE COMPOSER IS A WINDOW, NOT A PAGE. Its queue grows with every
    // request, and a Prompt box at its floor cannot give back more than it
    // had, so a long enough queue made the column taller than the window and
    // the whole page scrolled to reach the bottom of it - the grid, the
    // toolbar and the tab bar going with it. Asked for: "the entire clipboard
    // tab to fit, and if scrolling happens, the column should be windowed so
    // it scrolls and the entire page does not". So the composer is held to
    // the same bottom as the browser column (a measured max-height, pinned
    // like the grid's) and scrolls inside itself; what it holds beyond that
    // is read as its overflow and taken out of the Prompt box first, down to
    // the box's floor. Its two full-window views are `position: fixed`, so
    // the column's scroll box does not clip them.
    //
    // Stacked on a narrow window, the composer is under the grid rather than
    // beside it, and "to the bottom of the window" would mean a grid that
    // pushed the composer off the screen: the grid keeps the old cap, the
    // Prompt box its own size, and the composer is the page's to scroll to,
    // as it always was - a column under another one has no window of its own
    // to fit, and a scroller inside a page a finger scrolls takes the swipe.

    //: The least the grid is given, whatever the window. Below it the page
    //: scrolls, as it always did. The composer's window has the same floor.
    const FIT_FLOOR = 240;
    //: The Prompt box's own floor, the stylesheet's `min-height`.
    const PROMPT_FLOOR = 96;
    //: Breathing room under both columns.
    const FIT_GAP = 12;
    //: The share of the window the grid keeps when the columns are stacked.
    const STACKED_SHARE = 0.7;
    //: What makes the composer a window, pinned while it is beside the
    //: browser. `contain` keeps a scroll that reaches the column's end from
    //: carrying on into the page; the stable gutter keeps a scrollbar that
    //: comes and goes from narrowing the column under the reader, which
    //: would also rewrap it and move every number the fit just read. And
    //: `nowrap`, because Gradio gives every column in a row `flex-wrap:
    //: wrap` (its rule for a row's children), and a wrapping column given a
    //: height does not scroll what it cannot hold - it starts a second
    //: column of blocks beside the first, out of sight to the right.
    const COMPOSER_WINDOW = {
        "flex-wrap": "nowrap",
        "overflow-y": "auto",
        "overscroll-behavior": "contain",
        "scrollbar-gutter": "stable"
    };
    //: Less than this past the window is a rounding, not a queue.
    const OVERFLOW_SLACK = 1;

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

    /** Hold the composer to `room` pixels with a scroll of its own, or let it go (null). */
    function windowComposer(composer, room) {
        const on = room !== null;
        let changed = writeSize(composer, ["max-height"], room);
        for (const property in COMPOSER_WINDOW) {
            if (!Object.prototype.hasOwnProperty.call(COMPOSER_WINDOW, property)) { continue; }
            const wanted = on ? COMPOSER_WINDOW[property] : "";
            if (composer.style.getPropertyValue(property) === wanted) { continue; }
            changed = true;
            if (wanted) { composer.style.setProperty(property, wanted, "important"); }
            else { composer.style.removeProperty(property); }
        }
        // The stylesheet's readable copy is keyed on this, and so is a check.
        if (on !== (composer.dataset.minipaintWindowed === "1")) {
            changed = true;
            if (on) { composer.dataset.minipaintWindowed = "1"; }
            else { delete composer.dataset.minipaintWindowed; }
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
        const result = { grid: Math.round(height), prompt: null, stacked: stacked,
                         composer: null, overflow: 0 };
        writeSize(grid, ["height", "max-height"], height);
        if (!composer || !k) {
            S.fitted = result;
            return result;
        }
        const box = promptBox();
        if (stacked) {
            windowComposer(composer, null);
            if (box) { writeSize(box, ["min-height"], null); }
            S.fitted = result;
            return result;
        }
        // Everything is read before the composer is written, so the two
        // readings describe one layout. Where the composer ends plus what it
        // holds past its window is where its content ends, whatever window
        // it had: so the room this fit gives it does not depend on the room
        // the last one did.
        const composerTop = k.top + scrolledAbove(composer);
        const bottom = k.bottom + scrolledAbove(composer);
        let overflow = composer.scrollHeight - composer.clientHeight;
        if (overflow <= OVERFLOW_SLACK) { overflow = 0; }
        const current = box ? box.getBoundingClientRect().height : 0;
        // Rounded out, and the box's share rounded in, so a column that fits
        // to the pixel is never half a pixel over and given a scrollbar.
        const room = Math.ceil(Math.max(FIT_FLOOR, view - composerTop - FIT_GAP));
        result.composer = room;
        result.overflow = overflow;
        windowComposer(composer, room);
        if (box) {
            const wanted = Math.floor(Math.max(PROMPT_FLOOR, current + (view - bottom - FIT_GAP) - overflow));
            result.prompt = wanted;
            writeSize(box, ["min-height"], wanted);
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

    /* ------------------------------------------------------------------ */
    /* The scrubber                                                          */
    /* ------------------------------------------------------------------ */
    //
    // Asked for: "some sort of scrubber / scroll bar mechanism so I can
    // scroll the images. I have thousands of images so scrolling one page at
    // a time is horrible ... a compact solution that does not take up space
    // or change the layout."
    //
    // A thin bar laid over the grid's right-hand edge, on the grid's own
    // padding: it takes no room from the row and moves nothing when it comes
    // and goes. Its handle is where the view is in the whole list, as tall as
    // the share of it in view and never shorter than a fingertip. Drag the
    // handle and the grid follows it; press anywhere else on the bar and the
    // view jumps there and follows the pointer from then on. While it is
    // held a bubble beside the handle says where you are - which picture of
    // how many, and the date, name or size the sort orders by - and an
    // archive asks for no thumbnails until it is let go, so a scrub across
    // ten thousand pictures asks for the screen it stops on and nothing on
    // the way there. The wheel, a finger on the grid and the keys scroll the
    // grid as they always did; the bar follows them.
    //
    // It is shown only while the grid holds more than it shows.

    //: How far the bar sits inside the grid's frame, top and bottom.
    const SCRUB_INSET = 4;
    //: The handle's least height: a target, not a sliver.
    const SCRUB_MIN_HANDLE = 32;
    //: Held still this long, the scrubber asks for the view's thumbnails as
    //: though it had been let go. Asked for: "if i pause with out releasing
    //: the scrubber, the thumbs in that view should start loading".
    const SCRUB_PAUSE_MS = 300;

    //: The bar: a strip the width of a fingertip's edge over the grid's
    //: right-hand padding. Its top, height and right edge are measured
    //: (placeScrubber); this is everything else, pinned like the grid.
    //: Its display is the stylesheet's, like the full-screen button's: an
    //: inline !important display would beat the rule that hides it.
    const SCRUBBER_GEOMETRY = {
        "position": "absolute",
        "box-sizing": "border-box",
        "width": "14px",
        "min-width": "0",
        "max-width": "none",
        "margin": "0",
        "padding": "0",
        "border-width": "0",
        "overflow": "visible",
        "z-index": "2",
        "touch-action": "none",
        "user-select": "none",
        "-webkit-user-select": "none",
        "cursor": "pointer"
    };
    const SCRUB_RAIL_GEOMETRY = {
        "position": "absolute",
        "top": "0",
        "bottom": "0",
        "right": "4px",
        "width": "2px",
        "margin": "0",
        "padding": "0",
        "pointer-events": "none"
    };
    const SCRUB_HANDLE_GEOMETRY = {
        "position": "absolute",
        "top": "0",
        "right": "2px",
        "box-sizing": "border-box",
        "margin": "0",
        "padding": "0",
        "border-width": "0",
        "pointer-events": "none"
    };
    const SCRUB_BUBBLE_GEOMETRY = {
        "position": "absolute",
        "right": "18px",
        "top": "0",
        "box-sizing": "border-box",
        "margin": "0",
        "padding": "4px 8px",
        "max-width": "260px",
        "white-space": "nowrap",
        "overflow": "hidden",
        "text-overflow": "ellipsis",
        "font-size": "12px",
        "line-height": "1.35",
        "text-align": "right",
        "pointer-events": "none"
    };

    function scrubberElement() {
        const mount = gridMount();
        if (!mount) { return null; }
        let bar = mount.querySelector(".minipaint-clip-scrubber");
        if (bar) { return bar; }
        bar = document.createElement("div");
        bar.className = "minipaint-clip-scrubber";
        bar.hidden = true;
        bar.setAttribute("role", "scrollbar");
        bar.setAttribute("aria-orientation", "vertical");
        bar.setAttribute("aria-label", "Scroll through the pictures");
        bar.setAttribute("aria-valuemin", "0");
        bar.setAttribute("aria-valuemax", "100");
        bar.setAttribute("aria-valuenow", "0");
        bar.title = "Drag, or press anywhere on it, to move through the pictures";
        pin(bar, SCRUBBER_GEOMETRY);
        const rail = document.createElement("span");
        rail.className = "minipaint-clip-scrubber-rail";
        pin(rail, SCRUB_RAIL_GEOMETRY);
        const handle = document.createElement("span");
        handle.className = "minipaint-clip-scrubber-handle";
        pin(handle, SCRUB_HANDLE_GEOMETRY);
        const bubble = document.createElement("span");
        bubble.className = "minipaint-clip-scrubber-bubble";
        bubble.hidden = true;
        bubble.setAttribute("aria-live", "polite");
        pin(bubble, SCRUB_BUBBLE_GEOMETRY);
        bar.appendChild(rail);
        bar.appendChild(handle);
        bar.appendChild(bubble);
        bar.addEventListener("pointerdown", onScrubDown);
        bar.addEventListener("pointermove", onScrubMove);
        bar.addEventListener("pointerup", onScrubEnd);
        bar.addEventListener("pointercancel", onScrubEnd);
        bar.addEventListener("lostpointercapture", onScrubEnd);
        // The bar is the drag's: a page that pans under a finger the page
        // has claimed (a Gecko trap the sibling extension met) is told so
        // from a listener that can still say no.
        bar.addEventListener("touchmove", function (event) { if (S.scrub && event.cancelable) { event.preventDefault(); } }, { passive: false });
        mount.appendChild(bar);
        return bar;
    }

    /** Where the grid's scroll stands: how far down, how far it can go. */
    function scrollFacts(grid) {
        const max = Math.max(0, grid.scrollHeight - grid.clientHeight);
        return { top: grid.scrollTop, max: max, view: grid.clientHeight, whole: grid.scrollHeight };
    }

    /** Lay the bar over the grid's right-hand edge, inside its frame. */
    function placeScrubber(bar, grid) {
        const style = getComputedStyle(grid);
        const borderTop = parseFloat(style.borderTopWidth) || 0;
        const borderRight = parseFloat(style.borderRightWidth) || 0;
        const mount = bar.parentNode;
        const top = grid.offsetTop + borderTop + SCRUB_INSET;
        const height = Math.max(0, grid.clientHeight - 2 * SCRUB_INSET);
        const right = Math.max(0, (mount ? mount.clientWidth : 0) - (grid.offsetLeft + grid.offsetWidth) + borderRight);
        const wanted = { "top": top + "px", "height": height + "px", "right": right + "px" };
        for (const property in wanted) {
            if (bar.style.getPropertyValue(property) !== wanted[property]) { bar.style.setProperty(property, wanted[property], "important"); }
        }
        return height;
    }

    /** The handle's height and place on a bar of `height`. */
    function handleBox(height, facts) {
        const size = Math.min(height, Math.max(SCRUB_MIN_HANDLE, facts.whole ? height * facts.view / facts.whole : height));
        const travel = Math.max(0, height - size);
        return { size: size, travel: travel, top: facts.max ? travel * Math.min(1, Math.max(0, facts.top / facts.max)) : 0 };
    }

    function drawScrubber() {
        S.scrubFrame = 0;
        const host = byId(GRID_ID);
        const grid = host ? host.querySelector(".minipaint-clip-grid") : null;
        if (!grid) { return; }
        const bar = scrubberElement();
        if (!bar) { return; }
        const facts = scrollFacts(grid);
        // More than the grid shows, by more than a rounding.
        const shown = grid.clientHeight > 0 && facts.max > 2;
        if (bar.hidden === shown) { bar.hidden = !shown; }
        if (!shown) {
            if (S.scrub) { endScrub(); }
            return;
        }
        const height = placeScrubber(bar, grid);
        const box = handleBox(height, facts);
        const handle = bar.querySelector(".minipaint-clip-scrubber-handle");
        if (handle) {
            const top = Math.round(box.top) + "px";
            const size = Math.round(box.size) + "px";
            if (handle.style.getPropertyValue("top") !== top) { handle.style.setProperty("top", top, "important"); }
            if (handle.style.getPropertyValue("height") !== size) { handle.style.setProperty("height", size, "important"); }
        }
        const now = String(facts.max ? Math.round(100 * facts.top / facts.max) : 0);
        if (bar.getAttribute("aria-valuenow") !== now) { bar.setAttribute("aria-valuenow", now); }
        if (S.scrub) { drawBubble(bar, box); }
    }

    function scheduleScrubber() {
        if (S.scrubFrame) { return; }
        S.scrubFrame = window.requestAnimationFrame(drawScrubber);
    }

    function onGridScroll() { scheduleScrubber(); }

    /** The date a version names: ``<mtime_ns>-<size>``. */
    function versionDate(version) {
        const ns = String(version || "").split("-")[0];
        if (!/^\d{7,}$/.test(ns)) { return ""; }
        const when = new Date(Number(ns.slice(0, -6)));
        if (isNaN(when.getTime())) { return ""; }
        try {
            return when.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
        } catch (e) {
            return when.toDateString();
        }
    }

    function bytesText(bytes) {
        const value = Number(bytes || 0);
        return value >= 1048576 ? (value / 1048576).toFixed(1) + " MB"
            : value >= 1024 ? Math.round(value / 1024) + " KB" : value + " B";
    }

    /**
     * Where the view is: the first picture in it, as a position in the
     * whole list, and what the sort orders it by.
     */
    function scrubPlace() {
        const grid = gridElement();
        if (!grid) { return null; }
        const style = getComputedStyle(grid);
        const gap = parseFloat(style.rowGap) || 8;
        const padTop = parseFloat(style.paddingTop) || 0;
        const facts = scrollFacts(grid);
        const perRow = Math.max(1, S.perRow || 1);
        const rowHeight = (S.thumb || DEFAULT_THUMB) + TILE_CAPTION + gap;
        // The last screen's first row is the end of the list, whatever row
        // the arithmetic lands on: at the bottom, say the last picture.
        const atEnd = facts.max > 0 && facts.top >= facts.max - 1;
        let row = Math.max(0, Math.floor(Math.max(0, facts.top - padTop) / rowHeight + 0.5));
        let sort = "";
        let total = 0;
        let offset = 0;
        let pick = null;
        if (archiveActive()) {
            sort = S.archive.sort;
            total = S.archive.items.length;
            const at = atEnd ? total - 1 : Math.min(total - 1, row * perRow);
            const item = S.archive.items[at];
            if (!item) { return null; }
            pick = { index: at, name: item[1], bytes: item[2], v: item[3] };
        } else {
            sort = S.library.sort;
            const tiles = items();
            total = S.library.total || tiles.length;
            offset = S.library.page * S.library.size;
            const at = atEnd ? tiles.length - 1 : Math.min(tiles.length - 1, row * perRow);
            const tile = tiles[at];
            if (!tile) { return null; }
            pick = { index: offset + at, name: tile.dataset.name || "", bytes: tile.dataset.bytes, v: tile.dataset.v };
        }
        let detail = "";
        if (sort === "newest" || sort === "oldest") { detail = versionDate(pick.v); }
        else if (sort === "largest" || sort === "smallest") { detail = pick.bytes ? bytesText(pick.bytes) : ""; }
        else { detail = pick.name; }
        return { position: pick.index + 1, total: total, detail: detail };
    }

    function drawBubble(bar, box) {
        const bubble = bar.querySelector(".minipaint-clip-scrubber-bubble");
        if (!bubble) { return; }
        const place = scrubPlace();
        if (!place) { bubble.hidden = true; return; }
        const count = function (value) { try { return Number(value).toLocaleString(); } catch (e) { return String(value); } };
        const text = count(place.position) + " of " + count(place.total);
        let first = bubble.firstChild;
        if (!first) {
            first = document.createElement("b");
            bubble.appendChild(first);
            bubble.appendChild(document.createElement("br"));
            bubble.appendChild(document.createElement("span"));
        }
        if (first.textContent !== text) { first.textContent = text; }
        const second = bubble.lastChild;
        if (second.textContent !== place.detail) { second.textContent = place.detail; }
        bubble.hidden = false;
        // Level with the handle's middle, and kept on the bar.
        const height = bubble.offsetHeight || 34;
        const middle = box.top + box.size / 2 - height / 2;
        const room = Math.max(0, (parseFloat(bar.style.getPropertyValue("height")) || 0) - height);
        bubble.style.setProperty("top", Math.round(Math.min(room, Math.max(0, middle))) + "px", "important");
    }

    function onScrubDown(event) {
        if (event.pointerType === "mouse" && event.button !== 0) { return; }
        const grid = gridElement();
        const bar = event.currentTarget;
        if (!grid || !bar || bar.hidden) { return; }
        event.preventDefault();
        event.stopPropagation();
        const facts = scrollFacts(grid);
        const height = parseFloat(bar.style.getPropertyValue("height")) || grid.clientHeight;
        const box = handleBox(height, facts);
        const rect = bar.getBoundingClientRect();
        const y = event.clientY - rect.top;
        // On the handle it is held where it was taken; anywhere else the
        // handle's middle comes to the pointer.
        const onHandle = y >= box.top && y <= box.top + box.size;
        S.scrub = { pointer: event.pointerId, grab: onHandle ? y - box.top : box.size / 2, moved: false,
                    paused: false, pauseTimer: 0 };
        armScrubPause();
        try { bar.setPointerCapture(event.pointerId); } catch (e) { /* the moves still come to the bar */ }
        bar.classList.add("minipaint-clip-scrubbing");
        if (!onHandle) { scrubTo(bar, event.clientY); }
        drawScrubber();
    }

    function scrubTo(bar, clientY) {
        const grid = gridElement();
        if (!grid || !S.scrub) { return; }
        const facts = scrollFacts(grid);
        const height = parseFloat(bar.style.getPropertyValue("height")) || grid.clientHeight;
        const box = handleBox(height, facts);
        const rect = bar.getBoundingClientRect();
        const fraction = box.travel > 0 ? (clientY - rect.top - S.scrub.grab) / box.travel : 0;
        const top = Math.round(Math.min(1, Math.max(0, fraction)) * facts.max);
        if (Math.abs(grid.scrollTop - top) >= 1) { grid.scrollTop = top; S.scrub.moved = true; }
    }

    function onScrubMove(event) {
        if (!S.scrub || event.pointerId !== S.scrub.pointer) { return; }
        event.preventDefault();
        const before = gridElement() ? gridElement().scrollTop : 0;
        scrubTo(event.currentTarget, event.clientY);
        // Moving again: nothing new is asked for until it rests or is let go.
        if (gridElement() && gridElement().scrollTop !== before) { S.scrub.paused = false; armScrubPause(); }
        drawScrubber();
    }

    /** Rest on the scrubber and the view fills in, as at a release. */
    function armScrubPause() {
        const held = S.scrub;
        if (!held) { return; }
        if (held.pauseTimer) { clearTimeout(held.pauseTimer); }
        held.pauseTimer = setTimeout(function () {
            held.pauseTimer = 0;
            if (S.scrub !== held) { return; }
            held.paused = true;
            if (archiveActive()) { renderArchive(); pumpThumbs(true); }
        }, SCRUB_PAUSE_MS);
    }

    function onScrubEnd(event) {
        if (!S.scrub || (event && event.pointerId !== undefined && event.pointerId !== S.scrub.pointer)) { return; }
        endScrub();
    }

    /** Let go: the bubble put away, and the view's thumbnails asked for. */
    function endScrub() {
        const bar = gridMount() ? gridMount().querySelector(".minipaint-clip-scrubber") : null;
        const held = S.scrub;
        S.scrub = null;
        if (held && held.pauseTimer) { clearTimeout(held.pauseTimer); }
        if (bar) {
            bar.classList.remove("minipaint-clip-scrubbing");
            if (held) { try { bar.releasePointerCapture(held.pointer); } catch (e) { /* released already */ } }
            const bubble = bar.querySelector(".minipaint-clip-scrubber-bubble");
            if (bubble) { bubble.hidden = true; }
        }
        if (archiveActive()) { renderArchive(); pumpThumbs(true); }
    }

    function pagerElement() {
        const mount = gridMount();
        return mount ? mount.querySelector(".minipaint-clip-pager") : null;
    }

    /** The full-screen button's glyph: four corners, drawn, never a character. */
    function expandGlyph() {
        const ns = "http://www.w3.org/2000/svg";
        const svg = document.createElementNS(ns, "svg");
        svg.setAttribute("viewBox", "0 0 24 24");
        svg.setAttribute("width", "16");
        svg.setAttribute("height", "16");
        svg.setAttribute("aria-hidden", "true");
        svg.setAttribute("focusable", "false");
        const path = document.createElementNS(ns, "path");
        path.setAttribute("d", "M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5");
        path.setAttribute("fill", "none");
        path.setAttribute("stroke", "currentColor");
        path.setAttribute("stroke-width", "2.2");
        path.setAttribute("stroke-linecap", "round");
        path.setAttribute("stroke-linejoin", "round");
        svg.appendChild(path);
        return svg;
    }

    /** One tile. Built once per picture and then reused - see drawLibrary.
     *  An archive's tile is built without a picture address: it is a place
     *  until its thumbnail's turn comes (see pumpThumbs). */
    function buildTile(item, placeholder) {
        const tile = document.createElement("button");
        tile.type = "button";
        tile.className = "minipaint-clip-item";
        tile.setAttribute("role", "option");
        tile.setAttribute("aria-selected", "false");
        tile.dataset.asset = item.id;
        tile.dataset.name = item.name;
        tile.dataset.v = item.v || "";
        tile.dataset.bytes = String(item.bytes || 0);
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
        if (!placeholder) { picture.src = imageUrl(item.id, item.v); }
        thumb.appendChild(picture);
        const label = document.createElement("span");
        label.className = "minipaint-clip-name";
        label.textContent = item.name;
        pin(label, NAME_GEOMETRY);
        // A span, not a button: a button inside the tile's own button is not
        // a thing a page may have. A press on it is told apart in onRootClick.
        const expand = document.createElement("span");
        expand.className = "minipaint-clip-expand";
        expand.setAttribute("role", "button");
        expand.setAttribute("aria-label", "Full screen");
        expand.title = "Full screen (double-tap to close)";
        pin(expand, EXPAND_GEOMETRY);
        expand.appendChild(expandGlyph());
        tile.appendChild(thumb);
        tile.appendChild(label);
        tile.appendChild(expand);
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
            tile.dataset.bytes = String(item.bytes || 0);
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
        // An archive is on screen: the storage folder's page is not drawn
        // under it. Going back reads it again.
        if (archiveActive()) { return; }
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
            : reason === "archive-empty"
            ? "No PNG, JPEG or WebP pictures in this archive's folder. Subfolders are not shown."
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
        // The storage folder is not on screen; it is read again on the way
        // back to it (leaveArchive).
        if (archiveActive()) { return Promise.resolve(null); }
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
     * Something on this page changed the library: re-ask for the page being
     * shown and patch from it.
     *
     * THE GRID IS READ, NOT TOLD. It used to patch itself from events the
     * server pushed down a connection held open for the life of the page;
     * that connection is gone (see CLAUDE.md, "Nothing of ours is held
     * open"). So the grid is read when the tab is opened, after this page
     * pastes, imports, uploads, renames or deletes, and when Refresh is
     * pressed. A picture another page or a finished WanGP job put in the
     * library shows up at the next of those - which is when anybody looks.
     *
     * A new picture does not move you. An import while you are on page 3
     * updates the count and the page total; the page you are on is the page
     * you keep.
     */
    function libraryChanged() {
        if (S.library.inflight) { S.library.pending = true; return; }
        fetchLibrary({ quiet: true });
    }

    /** The toolbar's Refresh: the grid, the composer's cards and the
     * history, read now. The one way to see what other pages and finished
     * jobs did while this one was open. */
    function refreshAll() {
        pressHidden(PRESS.refresh);
        refreshGrid();
        askQueue(null);
    }

    /** The grid read again: the storage folder, or the archive on screen. */
    function refreshGrid() {
        if (archiveActive()) { return fetchArchive({}); }
        return fetchLibrary({ refresh: true });
    }

    /** The sort, over HTTP: remembered first, then drawn when it arrives. */
    function setSort(mode) {
        const wanted = String(mode || "");
        if (!wanted || wanted === S.library.sort) { return Promise.resolve(false); }
        setBusy(true);
        return postSettings({ sort: wanted }).then(function (answer) {
            S.library.sort = (answer && answer.sort) || wanted;
            if (archiveActive()) { return fetchArchive({ sort: S.library.sort }); }
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
            if (node.closest && (node.closest("." + EDITOR_OPEN_CLASS) || node.closest("." + PROMPTS_CLASS))) { continue; }
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
            if (outputsOpen() || editorOpen() || promptHistoryOpen()) { applyHeaderInset(); }
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

    /** Whatever is full screen, the player's or not. */
    function fullscreenNow() {
        return document.fullscreenElement || document.webkitFullscreenElement || null;
    }

    /**
     * Is the stage what is full screen? Only that is the player's.
     *
     * The page itself can be full screen too - the Forge Assistant's focus
     * mode asks for the whole document - and a player that took any full
     * screen for its own ended that one: its button "left" the page's full
     * screen instead of entering its own, its icon said Leave, and closing
     * the view or picking another output took the page out of full screen,
     * and focus mode with it. Stacked on the page's, the stage's own full
     * screen comes off by itself and leaves the page's where it was.
     */
    function inFullscreen() {
        const now = fullscreenNow();
        return !!now && now === S.outputs.stage;
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
     * The recipe is the output's own - the ledger keeps it from the job that
     * made the file (``outputs``) - so a video can be loaded whoever asked
     * for it and however long ago: one the server generated while nobody
     * watched, one the gallery's popup asked for, an LTX 2.3 or 2.5 run that
     * outlasted the queue's two minutes. It goes through the hidden box the
     * server's `history_action` reads, as the Queue Send History's own Load
     * does, because what it changes - the slot cards - is rendered there:
     * the prompt as it was typed, never the enhanced one, each picture that
     * is still in the library, and "Use WanGP" for what was inherited or has
     * gone. It queues nothing. The view closes, because the composer is what
     * it has just changed.
     *
     * An output with no recipe - made before recipes were kept, with no
     * history record either - says so rather than offering a button that
     * would do nothing.
     */
    function loadButton(item) {
        const button = el("button", "minipaint-clip-output-load", "Load");
        button.type = "button";
        if (!item.recipe) {
            button.disabled = true;
            button.title = "No recipe was kept for this output, so there is nothing to load.";
            return button;
        }
        button.title = "Put this output's prompt, as it was typed, and its pictures back in the request, to generate again. Nothing is queued.";
        button.addEventListener("click", function (event) {
            event.stopPropagation();
            loadRecipe(item);
        });
        return button;
    }

    function loadRecipe(item) {
        if (!item || !item.recipe) { return false; }
        const sent = sendInput(BOXES.historyAction, "output:" + item.recipe + ":" + Date.now());
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
        if (item.model) { caption.appendChild(el("span", "minipaint-clip-output-model", item.model)); }
        // The prompt as it was typed, which is what Load puts back; the one an
        // enhancement wrote from it - what WanGP was given - is under it on
        // hover, and marked.
        const shown = item.prompt || item.written || "";
        if (shown) {
            const prompt = el("span", "minipaint-clip-output-prompt", shown);
            prompt.title = item.written && item.prompt
                ? "As typed: " + item.prompt + "\n\nWritten for WanGP: " + item.written
                : shown;
            caption.appendChild(prompt);
            if (item.written && item.prompt) {
                const mark = el("span", "minipaint-clip-badge", "enhanced");
                mark.title = "WanGP was given the prompt the writer made of these words.";
                caption.appendChild(mark);
            }
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
                // Escape leaves full screen, the next leaves the view. Any
                // full screen, not only the stage's: the browser spends
                // this Escape on the page's as well.
                if (fullscreenNow()) { return; }
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
    /* History: the last ten prompts sent from here, as typed                */
    /* ------------------------------------------------------------------ */

    /**
     * Asked for: "the history for the last ten prompts submitted from
     * clipboard to wangp. Not the enhanced, but the original raw input ... a
     * new button in the right column 'History' ... a list that i can scroll,
     * view, and choose to load."
     *
     * The list is the server's (``prompts``), read over this tab's own route
     * when the dialog opens and never held open: a page has one connection to
     * Forge under HTTP/2 and six under HTTP/1.1, and both reads here carry a
     * deadline. Load writes the composer's draft over the same route and then
     * puts the very text it answered with in the Prompt box - so it works with
     * the framework's channel gone, and the box and the draft cannot disagree.
     * It touches nothing else: the pictures, the enhancement switch and the
     * queue stay as they are, and nothing is queued.
     *
     * A dialog, on the page's dialog layer, so nothing another extension
     * floats over Forge covers it; and announced as one (``minipaint:overlay``),
     * so the Forge Assistant's panel puts itself away while it is up, exactly
     * as it does for the Send to WanGP popup.
     */
    const PROMPTS_ROUTE = "/minipaint-clipboard/prompts";
    const PROMPTS_CLASS = "minipaint-clip-prompts";
    const PROMPTS_BUTTON_ID = "minipaint_clipboard_prompt_history";
    const PROMPTS_TITLE_ID = "minipaint_clipboard_prompts_title";
    //: One ask of the route: a list of ten from a small document, or one write.
    const PROMPTS_TIMEOUT_MS = 15000;
    //: Said whenever this dialog takes the page and gives it back. See the
    //: popup's own OVERLAY_EVENT in minipaint_intercept.js.
    const OVERLAY_EVENT = "minipaint:overlay";
    const PROMPTS_OVERLAY = "clipboard-history";

    function promptsRoot() { return S.prompts.dom ? S.prompts.dom.root : null; }

    function promptHistoryOpen() {
        const element = promptsRoot();
        return !!(element && !element.hidden);
    }

    function announceOverlay(open) { announceOverlayAs(PROMPTS_OVERLAY, open); }

    function announceOverlayAs(name, open) {
        try {
            document.dispatchEvent(new CustomEvent(OVERLAY_EVENT, {
                detail: { name: name, open: !!open, modal: true }
            }));
        } catch (e) { /* nothing here depends on being heard */ }
    }

    function buildPromptHistory() {
        if (S.prompts.dom && S.prompts.dom.root.isConnected) { return S.prompts.dom; }
        const host = root();
        if (!host) { return null; }
        // The layer: the window below the theme's header, transparent, so a
        // press anywhere outside the dialog is a way out of it.
        const layer = el("div", PROMPTS_CLASS);
        layer.hidden = true;
        const dialog = el("div", PROMPTS_CLASS + "-dialog");
        dialog.setAttribute("role", "dialog");
        dialog.setAttribute("aria-modal", "true");
        dialog.setAttribute("aria-labelledby", PROMPTS_TITLE_ID);
        const head = el("div", PROMPTS_CLASS + "-head");
        const title = el("b", PROMPTS_CLASS + "-title", "Prompt history");
        title.id = PROMPTS_TITLE_ID;
        // A word, not a cross: the host's theme turns a span or a button
        // holding one of its glyphs into an icon of its own.
        const close = el("button", PROMPTS_CLASS + "-close", "Close");
        close.type = "button";
        close.title = "Close (Escape)";
        head.appendChild(title);
        head.appendChild(close);
        dialog.appendChild(head);
        dialog.appendChild(el("div", PROMPTS_CLASS + "-hint",
            "The last ten prompts sent to WanGP from here, as you typed them. Load puts one in the Prompt box; nothing is queued."));
        const message = el("div", PROMPTS_CLASS + "-message");
        message.setAttribute("role", "status");
        message.hidden = true;
        dialog.appendChild(message);
        const list = el("ol", PROMPTS_CLASS + "-list");
        dialog.appendChild(list);
        layer.appendChild(dialog);
        layer.addEventListener("click", onPromptHistoryClick);
        host.appendChild(layer);
        S.prompts.dom = { root: layer, dialog: dialog, list: list, message: message, close: close };
        return S.prompts.dom;
    }

    function promptsMessage(text, failure) {
        const dom = S.prompts.dom;
        if (!dom) { return; }
        dom.message.textContent = text || "";
        dom.message.hidden = !text;
        dom.message.classList.toggle(PROMPTS_CLASS + "-failure", !!failure);
    }

    /** One ask of the route, bounded. ``body`` null is the list; else a POST. */
    function askPrompts(body) {
        if (!body && S.prompts.read) { return S.prompts.read; }
        let controller = null;
        let cutoff = 0;
        try { controller = typeof AbortController === "function" ? new AbortController() : null; } catch (e) { controller = null; }
        if (controller) {
            cutoff = setTimeout(function () { try { controller.abort(); } catch (e) { /* settled */ } }, PROMPTS_TIMEOUT_MS);
        }
        const options = { credentials: "same-origin", cache: "no-store" };
        if (body) {
            options.method = "POST";
            options.headers = { "Content-Type": "application/json" };
            options.body = JSON.stringify(body);
        }
        if (controller) { options.signal = controller.signal; }
        const asked = fetch(PROMPTS_ROUTE, options).then(function (response) {
            return response.json();
        }).then(function (answer) {
            serverSilent(false);
            return answer && typeof answer === "object" ? answer : null;
        }, function (error) {
            note("prompt history: no answer (" + ((error && error.message) || error) + ")");
            serverSilent(true, "the prompt history");
            return null;
        }).then(function (answer) {
            if (cutoff) { clearTimeout(cutoff); }
            if (!body && S.prompts.read === asked) { S.prompts.read = null; }
            return answer;
        });
        if (!body) { S.prompts.read = asked; }
        return asked;
    }

    function promptEntry(entry) {
        const item = el("li", PROMPTS_CLASS + "-entry");
        item.dataset.prompt = entry.id;
        const meta = el("div", PROMPTS_CLASS + "-meta");
        meta.appendChild(el("span", PROMPTS_CLASS + "-when", entry.when || ""));
        if (entry.model) {
            const model = el("span", PROMPTS_CLASS + "-model", entry.model);
            model.title = entry.target_label ? entry.model + " (" + entry.target_label + ")" : entry.model;
            meta.appendChild(model);
        }
        if (entry.enhanced) {
            const enhanced = el("span", "minipaint-clip-badge", "enhanced");
            enhanced.title = "This press was enhanced: WanGP was sent the prompt the writer made of these words. Load puts back the words.";
            meta.appendChild(enhanced);
        }
        if (entry.origin) { meta.appendChild(el("span", "minipaint-clip-badge", entry.origin)); }
        // One line of facts and the two buttons, then the words: ten entries
        // should be mostly words.
        const row = el("div", PROMPTS_CLASS + "-row");
        row.appendChild(meta);
        item.appendChild(row);
        // The whole text, as it was typed - its line breaks too, because to
        // WanGP each line can be a prompt of its own. Clamped until More.
        const text = el("div", PROMPTS_CLASS + "-text", entry.prompt || "");
        text.dataset.expanded = "0";
        item.appendChild(text);
        const actions = el("div", PROMPTS_CLASS + "-actions");
        const more = el("button", PROMPTS_CLASS + "-more", "More");
        more.type = "button";
        more.dataset.promptMore = entry.id;
        more.setAttribute("aria-expanded", "false");
        more.hidden = true;
        const load = el("button", PROMPTS_CLASS + "-load", "Load");
        load.type = "button";
        load.dataset.promptLoad = entry.id;
        load.title = "Put this prompt in the Prompt box. The pictures stay as they are, and nothing is queued.";
        actions.appendChild(more);
        actions.appendChild(load);
        row.appendChild(actions);
        return item;
    }

    /** More is offered only where the clamp is hiding something. */
    function offerMore(list) {
        for (const item of Array.from(list.querySelectorAll("." + PROMPTS_CLASS + "-entry"))) {
            const text = item.querySelector("." + PROMPTS_CLASS + "-text");
            const more = item.querySelector("." + PROMPTS_CLASS + "-more");
            if (!text || !more || text.dataset.expanded === "1") { continue; }
            more.hidden = !(text.scrollHeight > text.clientHeight + 1);
        }
    }

    function drawPromptHistory(answer) {
        const dom = buildPromptHistory();
        if (!dom) { return; }
        const entries = answer && Array.isArray(answer.entries) ? answer.entries : [];
        S.prompts.entries = entries.length;
        dom.list.innerHTML = "";
        dom.list.classList.toggle("minipaint-clip-empty", !entries.length);
        if (!entries.length) {
            dom.list.appendChild(el("li", PROMPTS_CLASS + "-empty", "No prompt has been sent to WanGP from here yet."));
            return;
        }
        for (const entry of entries) { dom.list.appendChild(promptEntry(entry)); }
        const settle = typeof requestAnimationFrame === "function" ? requestAnimationFrame : function (fn) { return setTimeout(fn, 0); };
        settle(function () { offerMore(dom.list); });
    }

    function openPromptHistory() {
        const dom = buildPromptHistory();
        if (!dom) { return false; }
        if (S.menu && !S.menu.hidden) { closeMenu(); }
        const was = promptHistoryOpen();
        applyHeaderInset();
        watchHeaderInset();
        dom.root.hidden = false;
        if (!was) {
            announceOverlay(true);
            if (S.promptsKey) { document.removeEventListener("keydown", S.promptsKey, true); }
            S.promptsKey = function (event) {
                if (event.key !== "Escape" || !promptHistoryOpen()) { return; }
                event.stopPropagation();
                event.preventDefault();
                closePromptHistory();
            };
            document.addEventListener("keydown", S.promptsKey, true);
        }
        promptsMessage("", false);
        if (!dom.list.children.length) { dom.list.appendChild(el("li", PROMPTS_CLASS + "-empty", "Reading…")); }
        try { dom.close.focus(); } catch (e) { /* focus is a courtesy */ }
        S.prompts.drawn = (S.prompts.drawn || 0) + 1;
        const asked = S.prompts.drawn;
        return askPrompts(null).then(function (answer) {
            if (asked !== S.prompts.drawn || !promptHistoryOpen()) { return answer; }
            if (!answer || answer.ok !== true) {
                drawPromptHistory({ entries: [] });
                promptsMessage((answer && answer.message) || "Forge did not answer, so the history could not be read. Close and try again.", true);
                return answer;
            }
            drawPromptHistory(answer);
            note("prompt history: opened (" + (answer.entries || []).length + " of " + (answer.max || 10) + ")");
            return answer;
        });
    }

    function closePromptHistory() {
        const element = promptsRoot();
        if (!element || element.hidden) { return false; }
        element.hidden = true;
        if (S.promptsKey) { document.removeEventListener("keydown", S.promptsKey, true); S.promptsKey = null; }
        announceOverlay(false);
        const host = byId(PROMPTS_BUTTON_ID);
        const button = host ? (host.tagName === "BUTTON" ? host : host.querySelector("button")) : null;
        if (button && typeof button.focus === "function") {
            try { button.focus(); } catch (e) { /* focus is a courtesy */ }
        }
        return true;
    }

    /** Load: the server puts the words in the draft, then this page puts the
     *  same words in the box. Nothing else on the composer moves. */
    function loadPrompt(entryId, button) {
        if (!entryId || S.prompts.loading) { return Promise.resolve(null); }
        S.prompts.loading = entryId;
        if (button) { button.disabled = true; }
        return askPrompts({ action: "load", id: entryId }).then(function (answer) {
            S.prompts.loading = "";
            if (answer && answer.ok === true && typeof answer.prompt === "string") {
                const written = sendInput(PROMPT_ID, answer.prompt);
                note("prompt history: " + String(entryId).slice(0, 8) + " loaded" + (written ? "" : " (the prompt box is missing)"));
                closePromptHistory();
                toast(answer.status || "Prompt loaded from History. Nothing was queued.");
                return answer;
            }
            if (button) { button.disabled = false; }
            if (answer && Array.isArray(answer.entries)) { drawPromptHistory(answer); }
            promptsMessage((answer && answer.message) || "Forge did not answer; nothing was loaded.", true);
            return answer;
        });
    }

    function onPromptHistoryClick(event) {
        const dom = S.prompts.dom;
        const target = event.target;
        if (!dom || !target) { return; }
        if (target === dom.root) { closePromptHistory(); return; }
        if (!target.closest) { return; }
        if (target.closest("." + PROMPTS_CLASS + "-close")) { closePromptHistory(); return; }
        const load = target.closest("[data-prompt-load]");
        if (load) { loadPrompt(load.dataset.promptLoad, load); return; }
        const more = target.closest("[data-prompt-more]");
        if (more) {
            const item = more.closest("." + PROMPTS_CLASS + "-entry");
            const text = item ? item.querySelector("." + PROMPTS_CLASS + "-text") : null;
            if (!text) { return; }
            const open = text.dataset.expanded !== "1";
            text.dataset.expanded = open ? "1" : "0";
            more.textContent = open ? "Less" : "More";
            more.setAttribute("aria-expanded", open ? "true" : "false");
        }
    }

    /* ------------------------------------------------------------------ */
    /* Archives: other folders, shown in place of the storage folder        */
    /* ------------------------------------------------------------------ */
    //
    // An archive is a folder on the Forge host - usually thousands of
    // pictures in one place - that the grid can show instead of the storage
    // folder, one at a time, and that nothing writes to (``archives.py``).
    // It is not paged: the server answers every picture in one listing, read
    // from the folder's directory entries alone, and the grid draws all of
    // them - as tiles that are only places until they are looked at.
    //
    // WHAT A TILE COSTS. Nothing, until it is on screen: a tile's picture has
    // no address until its turn comes, so ten thousand tiles are ten
    // thousand empty boxes and no request. And only the rows near the view
    // are in the document at all; the rest are a height the scroll bar
    // knows about. A grid of a whole archive is a few hundred nodes.
    //
    // WHAT LOADS, AND IN WHAT ORDER. What is in view, in reading order, and
    // then what is about to be. A thumbnail is asked for over plain HTTP - no
    // stream, nothing held open - a few at a time, each with a deadline, and
    // made on the server the first time anybody looks (``Thumbnails``). The
    // order is decided again whenever the view moves: a request for a tile
    // that has left the view is dropped while what is now in view waits, so
    // what you are looking at is always what comes next - scroll a long way
    // and the pictures you scrolled past are never asked for at all. The
    // server finishes a thumbnail it started, so a dropped request costs it
    // nothing it will not use: the next look at that tile is a cache read.
    //
    // WHAT IS KEPT. Each thumbnail's URL carries the file's size and time, so
    // the server answers it as immutable and the browser keeps it; a picture
    // seen once is not asked of the server again until the file changes or
    // the browser's cache is cleared. The page keeps the last few hundred it
    // drew as object URLs besides, so scrolling back is instant.

    const ARCHIVES_ROUTE = "/minipaint-clipboard/archives";
    const ARCHIVE_LIST_ROUTE = "/minipaint-clipboard/archive/list";
    const ARCHIVE_IMAGE_ROUTE = "/minipaint-clipboard/archive/image/";
    const ARCHIVE_REF_PREFIX = "ar-";
    //: One listing of a folder of tens of thousands, on a slow disk.
    const ARCHIVE_LIST_TIMEOUT_MS = 60000;
    //: One thumbnail: a cache read in milliseconds, a large photograph's
    //: first decode in a second or two. Past this the request is dropped and
    //: tried once more later; a second miss marks the tile.
    const THUMB_TIMEOUT_MS = 20000;
    const THUMB_TRIES = 2;
    //: How many thumbnails are asked for at once. Over HTTP/1.1 a page has six
    //: connections to this origin; this leaves the rest to everything else.
    const THUMB_CONCURRENCY = 3;
    //: Drawn thumbnails kept as object URLs, for scrolling back.
    const THUMB_KEEP = 800;
    //: Rows in the document beyond the view, each way.
    const ARCHIVE_OVERSCAN_ROWS = 2;
    //: How far past the view is asked for once the view itself is done, in
    //: screens.
    const ARCHIVE_AHEAD_SCREENS = 1;
    //: New requests wait for the view to settle this long after a scroll -
    //: a fling across a thousand rows starts nothing for the rows it passes -
    //: and are started at least this often during a long slow scroll.
    const PUMP_SETTLE_MS = 80;
    const PUMP_EVERY_MS = 200;
    const ARCHIVES_TIMEOUT_MS = 20000;
    const ARCHIVES_CLASS = "minipaint-clip-archives";
    const ARCHIVES_TITLE_ID = "minipaint_clipboard_archives_title";
    const ARCHIVES_OVERLAY = "clipboard-archives";

    //: The archive's tile: absolutely placed in its row and column, which
    //: the script works out; everything else is the library tile's.
    const ARCHIVE_TRACK_GEOMETRY = {
        "display": "block",
        "position": "relative",
        "box-sizing": "border-box",
        "width": "100%",
        "min-width": "0",
        "max-width": "100%",
        "margin": "0",
        "padding": "0",
        "border-width": "0",
        "overflow": "visible"
    };

    function isArchiveRef(id) { return typeof id === "string" && id.indexOf(ARCHIVE_REF_PREFIX) === 0; }

    function archiveActive() { return !!S.archive.view; }

    function archiveThumbUrl(ref, version) {
        return ARCHIVE_IMAGE_ROUTE + encodeURIComponent(ref) + "?thumb=1"
            + (version ? "&v=" + encodeURIComponent(version) : "");
    }

    function archiveFullUrl(ref, version) {
        return ARCHIVE_IMAGE_ROUTE + encodeURIComponent(ref) + (version ? "?v=" + encodeURIComponent(version) : "");
    }

    function archiveRef(key) { return ARCHIVE_REF_PREFIX + S.archive.view + "-" + key; }

    /** One bounded JSON request: an answer, or null with the reason noted. */
    function askJson(url, body, timeoutMs, what) {
        let controller = null;
        let cutoff = 0;
        try { controller = typeof AbortController === "function" ? new AbortController() : null; } catch (e) { controller = null; }
        if (controller) {
            cutoff = setTimeout(function () { try { controller.abort(); } catch (e) { /* settled */ } }, timeoutMs);
        }
        const options = { credentials: "same-origin", cache: "no-store" };
        if (body) {
            options.method = "POST";
            options.headers = { "Content-Type": "application/json" };
            options.body = JSON.stringify(body);
        }
        if (controller) { options.signal = controller.signal; }
        return fetch(url, options).then(function (response) {
            return response.json();
        }).then(function (answer) {
            serverSilent(false);
            return answer && typeof answer === "object" ? answer : null;
        }, function (error) {
            note(what + ": no answer (" + ((error && error.message) || error) + ")");
            serverSilent(true, what);
            return null;
        }).then(function (answer) {
            if (cutoff) { clearTimeout(cutoff); }
            return answer;
        });
    }

    /** The archives and the view, as the menu's facts carry them. */
    function archiveList() {
        const state = menuState();
        return Array.isArray(state.archives) ? state.archives : [];
    }

    function archiveName(id) {
        const found = archiveList().find(function (pair) { return pair[0] === id; });
        return found ? found[1] : "";
    }

    /* -- which folder the grid shows ------------------------------------ */

    //: The Menu button's own word, while there is nothing else to say.
    const MENU_LABEL = "☰ Menu";
    //: A folder's name on the Menu button is cut to this many characters: a
    //: theme can say `button { min-width: fit-content !important }`, which
    //: no max-width beats, so the button is only as wide as what it says.
    const MENU_NAME_CHARS = 24;

    /**
     * Which folder the grid is showing, said where it is changed.
     *
     * There used to be a row over the grid for this - the folder's name as a
     * button, and a note - and it took a row from the pictures. Asked for:
     * "lets make switching between storage and folder a menu option". So the
     * switch is Menu → Archives › and nothing else, and the folder's name is
     * on the Menu button once there is an archive to choose (the storage
     * folder's, or the archive's, cut to MENU_NAME_CHARS); a Clipboard with
     * no archives keeps its plain Menu. The status line says the rest when
     * the view changes: how many pictures, that the archive is read-only,
     * and where new pictures go.
     */
    function drawViewName() {
        const list = archiveList();
        const name = archiveActive() ? (archiveName(S.archive.view) || "Archive") : "Storage folder";
        const button = byId(MENU_ID);
        if (button) {
            const shown = list.length > 0 || archiveActive();
            const cut = name.length > MENU_NAME_CHARS ? name.slice(0, MENU_NAME_CHARS - 1) + "…" : name;
            const label = shown ? "☰ " + cut : MENU_LABEL;
            if (button.textContent !== label) { button.textContent = label; }
            const title = shown ? "Menu · showing " + (archiveActive() ? "the archive " + name : "the storage folder") : "Menu";
            if (button.title !== title) { button.title = title; }
        }
        const element = byId(ROOT_ID);
        const view = archiveActive() ? "archive" : "storage";
        if (element && element.dataset.clipView !== view) { element.dataset.clipView = view; }
    }

    /* -- switching ----------------------------------------------------- */

    /**
     * Show an archive in place of the storage folder, or the storage folder
     * again (""). Kept on the host like the sort, so a reload and a second
     * browser open on the same view.
     */
    function setView(id) {
        const wanted = String(id || "");
        return askJson(ARCHIVES_ROUTE, { action: "view", id: wanted }, ARCHIVES_TIMEOUT_MS, "archives").then(function (answer) {
            if (!answer || answer.ok !== true) {
                setStatus((answer && answer.message) || "Forge did not answer; the grid is as it was.");
                return false;
            }
            if (answer.menu) { S.menuOverride = answer.menu; }
            applyView(String(answer.view || ""));
            setStatus(answer.message || "");
            note("view: " + (answer.view ? "an archive" : "the storage folder"));
            return true;
        });
    }

    /** Draw the view the server holds, from scratch when it changed. */
    function applyView(id) {
        const wanted = String(id || "");
        if (wanted && archiveName(wanted) === "") {
            // An archive that has left the list is the storage folder.
            return applyView("");
        }
        if (wanted === S.archive.view && (wanted ? S.archive.mounted : !S.archive.mounted)) {
            drawViewName();
            return;
        }
        if (S.selected && (isArchiveRef(S.selected) || wanted)) { select("", false); }
        if (wanted) {
            enterArchive(wanted);
        } else {
            leaveArchive();
        }
        drawViewName();
    }

    function enterArchive(id) {
        const grid = gridElement();
        if (!grid) { return; }
        stopThumbs();
        S.archive.view = id;
        S.archive.items = [];
        S.archive.index = new Map();
        S.archive.rendered = new Map();
        // The storage folder's tiles leave the document but not the page's
        // memory: going back reuses every one of them, decoded.
        for (const tile of S.library.tiles.values()) {
            if (tile.parentNode) { tile.parentNode.removeChild(tile); }
        }
        emptyNotice(grid, "");
        const pager = pagerElement();
        if (pager) { pager.hidden = true; }
        pin(grid, { "display": "block" });
        grid.classList.add("minipaint-clip-archive-grid");
        grid.setAttribute("aria-label", "Archive images");
        let track = grid.querySelector(".minipaint-clip-track");
        if (!track) {
            track = document.createElement("div");
            track.className = "minipaint-clip-track";
            pin(track, ARCHIVE_TRACK_GEOMETRY);
            grid.appendChild(track);
        }
        track.innerHTML = "";
        pin(track, { "height": "0px" });
        grid.scrollTop = 0;
        S.archive.scrollTop = 0;
        S.archive.mounted = true;
        watchArchive(grid);
        fetchArchive({});
    }

    function leaveArchive() {
        const grid = gridElement();
        stopThumbs();
        S.archive.view = "";
        S.archive.items = [];
        S.archive.index = new Map();
        S.archive.rendered = new Map();
        S.archive.mounted = false;
        S.archive.ticket += 1;
        if (!grid) { return; }
        const track = grid.querySelector(".minipaint-clip-track");
        if (track) { grid.removeChild(track); }
        grid.classList.remove("minipaint-clip-archive-grid");
        grid.setAttribute("aria-label", "Clipboard images");
        pin(grid, { "display": GRID_GEOMETRY.display });
        emptyNotice(grid, "");
        fetchLibrary({ quiet: true, refresh: true });
    }

    /* -- the listing --------------------------------------------------- */

    /** Ask for the whole archive and draw it. The newest ask wins. */
    function fetchArchive(options) {
        const opts = options || {};
        if (!archiveActive()) { return Promise.resolve(null); }
        const id = S.archive.view;
        const sort = opts.sort || S.library.sort || menuState().sort || "";
        const ticket = ++S.archive.ticket;
        S.archive.busy = true;
        setBusy(true);
        drawViewName();
        const url = ARCHIVE_LIST_ROUTE + "?id=" + encodeURIComponent(id) + (sort ? "&sort=" + encodeURIComponent(sort) : "");
        return askJson(url, null, ARCHIVE_LIST_TIMEOUT_MS, "archive listing").then(function (answer) {
            if (ticket !== S.archive.ticket || id !== S.archive.view) { return answer; }
            S.archive.busy = false;
            setBusy(false);
            if (!answer || answer.ok !== true) {
                setStatus((answer && answer.message) || "The archive could not be read. Press Refresh to try again.");
                drawViewName();
                return answer;
            }
            drawArchive(answer);
            if (!opts.quiet) { setStatus(answer.status || ""); }
            return answer;
        });
    }

    function drawArchive(answer) {
        const items = Array.isArray(answer.items) ? answer.items : [];
        S.archive.items = items;
        S.archive.sort = String(answer.sort || "");
        const index = new Map();
        for (let i = 0; i < items.length; i += 1) { index.set(items[i][0], i); }
        S.archive.index = index;
        // A tile drawn for an order that is no longer this one is a tile in
        // the wrong place: every one is placed again.
        const track = archiveTrack();
        for (const tile of S.archive.rendered.values()) {
            if (tile.parentNode) { tile.parentNode.removeChild(tile); }
        }
        S.archive.rendered = new Map();
        if (track) { track.innerHTML = ""; }
        const grid = gridElement();
        if (grid) {
            grid.dataset.count = String(items.length);
            grid.classList.toggle("minipaint-clip-empty", !items.length);
            emptyNotice(grid, items.length ? "" : "archive-empty");
        }
        if (S.selected && isArchiveRef(S.selected)) {
            const key = S.selected.slice(ARCHIVE_REF_PREFIX.length + 17);
            if (!index.has(key)) { select("", false); }
        }
        layoutArchive();
        renderArchive();
        drawViewName();
        pumpThumbs(true);
        S.renderedAt = Date.now();
        applyPerRow();
        refreshBadges();
        scheduleFit();
        scheduleScrubber();
    }

    function archiveTrack() {
        const grid = gridElement();
        return grid ? grid.querySelector(".minipaint-clip-track") : null;
    }

    /* -- placing tiles ------------------------------------------------- */

    /**
     * The storage folder's grid, worked out rather than laid out: the same
     * columns its `repeat(auto-fill, ...)` makes (the thumbnail plus the
     * tile's frame, never stretched, at least one), the same row height and
     * the same gap - so an archive looks exactly like the storage folder,
     * and the place of tile ten thousand is a multiplication.
     */
    function layoutArchive() {
        const grid = gridElement();
        const track = archiveTrack();
        if (!grid || !track) { return null; }
        const style = getComputedStyle(grid);
        const gap = parseFloat(style.rowGap) || parseFloat(style.columnGap) || 8;
        const room = Math.max(1, track.clientWidth || (grid.clientWidth - parseFloat(style.paddingLeft || 0) - parseFloat(style.paddingRight || 0)));
        // The storage folder's row, shared out the same way (applyPerRow).
        const columns = Math.max(1, S.perRow || Math.floor((room + gap) / (DEFAULT_THUMB + TILE_FRAME + gap)) || 1);
        const cellWidth = Math.max(1, (room - (columns - 1) * gap) / columns);
        const thumb = cellWidth - TILE_FRAME;
        const cellHeight = thumb + TILE_CAPTION;
        const rows = Math.ceil(S.archive.items.length / columns);
        const height = rows ? rows * cellHeight + (rows - 1) * gap : 0;
        const layout = { columns: columns, rows: rows, cellWidth: cellWidth, cellHeight: cellHeight, gap: gap,
                         padTop: parseFloat(style.paddingTop || 0), height: height };
        const before = S.archive.layout;
        S.archive.layout = layout;
        if (track.style.getPropertyValue("height") !== height + "px") { pin(track, { "height": height + "px" }); }
        if (before && (before.columns !== columns || before.cellWidth !== cellWidth || before.cellHeight !== cellHeight)) {
            for (const entry of S.archive.rendered) { placeTile(entry[1], entry[0]); }
        }
        return layout;
    }

    /** The rows in view, and the ones the document holds around them. */
    function archiveRows() {
        const grid = gridElement();
        const layout = S.archive.layout;
        if (!grid || !layout || !layout.rows) { return null; }
        const step = layout.cellHeight + layout.gap;
        const top = Math.max(0, grid.scrollTop - layout.padTop);
        const bottom = top + Math.max(0, grid.clientHeight);
        const first = Math.max(0, Math.floor(top / step));
        const last = Math.min(layout.rows - 1, Math.max(first, Math.floor(Math.max(0, bottom - 1) / step)));
        return { first: first, last: last, step: step, screenRows: Math.max(1, last - first + 1) };
    }

    function placeTile(tile, index) {
        const layout = S.archive.layout;
        if (!layout) { return; }
        const row = Math.floor(index / layout.columns);
        const column = index % layout.columns;
        // Fractions kept: the row is shared to the pixel, and a column
        // rounded on its own would leave the row a pixel short or over.
        const px = function (value) { return (Math.round(value * 100) / 100) + "px"; };
        pin(tile, {
            "position": "absolute",
            "left": px(column * (layout.cellWidth + layout.gap)),
            "top": px(row * (layout.cellHeight + layout.gap)),
            "width": px(layout.cellWidth),
            "height": px(layout.cellHeight)
        });
    }

    function buildArchiveTile(index) {
        const item = S.archive.items[index];
        const ref = archiveRef(item[0]);
        const tile = buildTile({ id: ref, name: item[1], v: item[3], bytes: item[2] }, true);
        tile.dataset.key = item[0];
        placeTile(tile, index);
        const on = S.selected === ref;
        tile.classList.toggle("minipaint-clip-selected", on);
        tile.setAttribute("aria-selected", on ? "true" : "false");
        paintThumb(tile, item[0]);
        return tile;
    }

    /** Put the rows near the view in the document, and only those. */
    function renderArchive() {
        const track = archiveTrack();
        const rows = archiveRows();
        if (!track) { return; }
        if (!rows) {
            for (const tile of S.archive.rendered.values()) { if (tile.parentNode) { tile.parentNode.removeChild(tile); } }
            S.archive.rendered = new Map();
            return;
        }
        const columns = S.archive.layout.columns;
        const from = Math.max(0, (rows.first - ARCHIVE_OVERSCAN_ROWS) * columns);
        const to = Math.min(S.archive.items.length, (rows.last + 1 + ARCHIVE_OVERSCAN_ROWS) * columns);
        for (const entry of Array.from(S.archive.rendered)) {
            if (entry[0] < from || entry[0] >= to) {
                if (entry[1].parentNode) { entry[1].parentNode.removeChild(entry[1]); }
                S.archive.rendered.delete(entry[0]);
            }
        }
        for (let index = from; index < to; index += 1) {
            if (S.archive.rendered.has(index)) { continue; }
            const tile = buildArchiveTile(index);
            S.archive.rendered.set(index, tile);
            track.appendChild(tile);
        }
    }

    /* -- the thumbnails, in view first ---------------------------------- */

    function paintThumb(tile, key) {
        const held = S.archive.thumbs.get(key);
        const picture = tile.querySelector("img");
        if (!picture) { return; }
        if (held && held.url) {
            // Used again: the newest end of the keep list.
            S.archive.thumbs.delete(key);
            S.archive.thumbs.set(key, held);
            pin(picture, { "display": PICTURE_GEOMETRY.display });
            tile.classList.remove("minipaint-clip-item-missing");
            if (picture.getAttribute("src") !== held.url) { picture.src = held.url; }
        } else if (held && held.state === "failed") {
            tile.classList.add("minipaint-clip-item-missing");
            pin(picture, { "display": "none" });
        }
    }

    function needsThumb(key) {
        const held = S.archive.thumbs.get(key);
        if (S.archive.inflight.has(key)) { return false; }
        return !held || (!held.url && held.state !== "failed");
    }

    /**
     * What to ask for, in order: the tiles in view in reading order, then
     * the rows below the view, then the rows above it, nearest first.
     */
    function wantedThumbs() {
        const rows = archiveRows();
        const visible = [];
        const ahead = [];
        if (!rows || !tabVisible()) { return { visible: visible, ahead: ahead }; }
        const columns = S.archive.layout.columns;
        const count = S.archive.items.length;
        const take = function (row, into) {
            for (let index = row * columns; index < Math.min(count, (row + 1) * columns); index += 1) {
                into.push(S.archive.items[index][0]);
            }
        };
        for (let row = rows.first; row <= rows.last; row += 1) { take(row, visible); }
        const reach = Math.ceil(rows.screenRows * ARCHIVE_AHEAD_SCREENS);
        for (let step = 1; step <= reach; step += 1) {
            if (rows.last + step < S.archive.layout.rows) { take(rows.last + step, ahead); }
            if (rows.first - step >= 0) { take(rows.first - step, ahead); }
        }
        return { visible: visible, ahead: ahead };
    }

    /**
     * Decide again what is asked for, and ask.
     *
     * Every request for a tile outside the view and what is just past it is
     * dropped; so is every request for a tile outside the view while a tile
     * IN it is still waiting - what you are looking at goes first, always.
     * New requests start only once the view has settled (``PUMP_SETTLE_MS``)
     * or every ``PUMP_EVERY_MS`` while it keeps moving, so a fling across the
     * archive asks for nothing on its way.
     */
    function pumpThumbs(now) {
        S.archive.pumpTimer = 0;
        if (!archiveActive()) { return; }
        const want = wantedThumbs();
        const visible = new Set(want.visible);
        const keep = new Set(want.visible.concat(want.ahead));
        const waiting = want.visible.some(needsThumb);
        for (const entry of Array.from(S.archive.inflight)) {
            if (!keep.has(entry[0]) || (waiting && !visible.has(entry[0]))) { dropThumb(entry[0], entry[1]); }
        }
        // Held on the scrubber and moving: nothing new is asked for until it
        // rests (armScrubPause) or is let go (endScrub), and then only for
        // where it stopped.
        if (S.scrub && !S.scrub.paused) { return; }
        const moment = Date.now();
        const settled = now || moment - S.archive.scrolledAt >= PUMP_SETTLE_MS || moment - S.archive.startedAt >= PUMP_EVERY_MS;
        if (!settled) {
            const wait = Math.max(0, Math.min(PUMP_SETTLE_MS - (moment - S.archive.scrolledAt),
                                              PUMP_EVERY_MS - (moment - S.archive.startedAt)));
            schedulePump(wait + 1);
            return;
        }
        const order = want.visible.concat(want.ahead).filter(needsThumb);
        while (S.archive.inflight.size < THUMB_CONCURRENCY && order.length) {
            startThumb(order.shift());
        }
    }

    function schedulePump(delay) {
        if (S.archive.pumpTimer) { return; }
        S.archive.pumpTimer = setTimeout(function () { pumpThumbs(false); }, Math.max(0, delay || 0));
    }

    function startThumb(key) {
        const index = S.archive.index.get(key);
        if (index === undefined) { return; }
        const item = S.archive.items[index];
        const ref = archiveRef(key);
        let controller = null;
        try { controller = typeof AbortController === "function" ? new AbortController() : null; } catch (e) { controller = null; }
        const job = { controller: controller, timedOut: false, dropped: false, timer: 0, view: S.archive.view };
        job.timer = setTimeout(function () {
            job.timedOut = true;
            if (controller) { try { controller.abort(); } catch (e) { /* settled */ } }
        }, THUMB_TIMEOUT_MS);
        S.archive.inflight.set(key, job);
        S.archive.startedAt = Date.now();
        S.archive.stats.started += 1;
        S.archive.stats.order.push(key);
        if (S.archive.stats.order.length > 400) { S.archive.stats.order.splice(0, 200); }
        const options = { credentials: "same-origin" };
        if (controller) { options.signal = controller.signal; }
        fetch(archiveThumbUrl(ref, item[3]), options).then(function (response) {
            if (!response.ok) {
                const error = new Error("HTTP " + response.status);
                error.status = response.status;
                throw error;
            }
            return response.blob();
        }).then(function (blob) {
            if (job.view !== S.archive.view) { return; }
            const url = URL.createObjectURL(blob);
            S.archive.thumbs.set(key, { url: url, state: "done", tries: 0 });
            S.archive.stats.done += 1;
            keepThumbs();
            const at = S.archive.index.get(key);
            const tile = at === undefined ? null : S.archive.rendered.get(at);
            if (tile) { paintThumb(tile, key); }
        }).catch(function (error) {
            if (job.view !== S.archive.view) { return; }
            if (job.dropped) { return; }
            const held = S.archive.thumbs.get(key) || { url: "", state: "idle", tries: 0 };
            held.tries = (held.tries || 0) + 1;
            const status = error && error.status;
            // A picture the server says is not there, or not a picture, will
            // not be one on the next try either. A deadline missed, or the
            // server's own trouble, is tried once more.
            const final = (status >= 400 && status < 500) || held.tries >= THUMB_TRIES;
            held.state = final ? "failed" : "idle";
            S.archive.thumbs.set(key, held);
            if (final) {
                S.archive.stats.failed += 1;
                const at = S.archive.index.get(key);
                const tile = at === undefined ? null : S.archive.rendered.get(at);
                if (tile) { paintThumb(tile, key); }
            }
            if (job.timedOut) { note("archive: a thumbnail took longer than " + (THUMB_TIMEOUT_MS / 1000) + "s" + (final ? "; its tile is marked" : "; it will be asked for again")); }
        }).then(function () {
            clearTimeout(job.timer);
            if (S.archive.inflight.get(key) === job) { S.archive.inflight.delete(key); }
            // A request this page dropped is not a free slot to fill at once:
            // it was dropped because the view moved, and the view's own pump
            // decides what comes next - otherwise a fling would start and
            // drop three requests a frame all the way down.
            if (!job.dropped && job.view === S.archive.view) { pumpThumbs(true); }
        });
    }

    /** A request for a tile that is no longer wanted first. */
    function dropThumb(key, job) {
        job.dropped = true;
        clearTimeout(job.timer);
        if (job.controller) { try { job.controller.abort(); } catch (e) { /* settled */ } }
        S.archive.inflight.delete(key);
        S.archive.stats.dropped += 1;
    }

    /** Only so many object URLs: the oldest not on screen go first. */
    function keepThumbs() {
        if (S.archive.thumbs.size <= THUMB_KEEP) { return; }
        const shown = new Set();
        for (const tile of S.archive.rendered.values()) { shown.add(tile.dataset.key); }
        for (const entry of Array.from(S.archive.thumbs)) {
            if (S.archive.thumbs.size <= THUMB_KEEP) { break; }
            if (shown.has(entry[0]) || !entry[1].url) { continue; }
            try { URL.revokeObjectURL(entry[1].url); } catch (e) { /* gone already */ }
            S.archive.thumbs.delete(entry[0]);
        }
    }

    /** Every request dropped and every object URL let go: a new view. */
    function stopThumbs() {
        for (const entry of Array.from(S.archive.inflight)) { dropThumb(entry[0], entry[1]); }
        for (const held of S.archive.thumbs.values()) {
            if (held.url) { try { URL.revokeObjectURL(held.url); } catch (e) { /* gone already */ } }
        }
        S.archive.thumbs = new Map();
        if (S.archive.pumpTimer) { clearTimeout(S.archive.pumpTimer); S.archive.pumpTimer = 0; }
    }

    /** The grid moved: the rows are drawn at once, the asking re-decided. */
    function onArchiveScroll() {
        if (!archiveActive()) { return; }
        S.archive.scrolledAt = Date.now();
        // Where the reader is, kept while the grid is on screen: a hidden
        // tab's grid has no layout, and coming back to it is coming back to
        // the top - of five thousand pictures, after a send to PNG Info.
        const grid = gridElement();
        if (grid && grid.clientHeight > 0) { S.archive.scrollTop = grid.scrollTop; }
        if (S.archive.frame) { return; }
        S.archive.frame = window.requestAnimationFrame(function () {
            S.archive.frame = 0;
            renderArchive();
            pumpThumbs(false);
        });
    }

    function onArchiveResize() {
        if (!archiveActive()) { return; }
        const grid = gridElement();
        // Hidden with its tab: nothing to measure, and a layout worked out
        // from a width of nothing would be one column ten thousand rows high.
        if (!grid || !grid.clientWidth || !grid.clientHeight) { return; }
        layoutArchive();
        if (S.archive.scrollTop && Math.abs(grid.scrollTop - S.archive.scrollTop) > 1) {
            grid.scrollTop = S.archive.scrollTop;
        }
        renderArchive();
        pumpThumbs(true);
    }

    function watchArchive(grid) {
        if (S.archive.watched === grid) { return; }
        S.archive.watched = grid;
        grid.addEventListener("scroll", onArchiveScroll, { passive: true });
        // Its size is watched with the grid's (watchGrid), which sizes the
        // row first and then places the archive's tiles from it.
        watchGrid(grid);
    }

    /* -- the Archives screen ------------------------------------------- */

    function archivesRoot() { return S.archivesDom ? S.archivesDom.root : null; }

    function archivesOpen() {
        const element = archivesRoot();
        return !!(element && !element.hidden);
    }

    function buildArchives() {
        if (S.archivesDom && S.archivesDom.root.isConnected) { return S.archivesDom; }
        const host = root();
        if (!host) { return null; }
        const layer = el("div", ARCHIVES_CLASS);
        layer.hidden = true;
        const dialog = el("div", ARCHIVES_CLASS + "-dialog");
        dialog.setAttribute("role", "dialog");
        dialog.setAttribute("aria-modal", "true");
        dialog.setAttribute("aria-labelledby", ARCHIVES_TITLE_ID);
        const head = el("div", ARCHIVES_CLASS + "-head");
        const title = el("b", ARCHIVES_CLASS + "-title", "Archives");
        title.id = ARCHIVES_TITLE_ID;
        const close = el("button", ARCHIVES_CLASS + "-close", "Close");
        close.type = "button";
        close.title = "Close (Escape)";
        head.appendChild(title);
        head.appendChild(close);
        dialog.appendChild(head);
        dialog.appendChild(el("div", ARCHIVES_CLASS + "-hint",
            "Folders on the machine running Forge that the Clipboard can show in place of its storage folder. "
            + "Nothing in them is written to; new pictures still go to the storage folder."));
        const form = el("form", ARCHIVES_CLASS + "-add");
        const input = document.createElement("input");
        input.type = "text";
        input.className = ARCHIVES_CLASS + "-path";
        input.placeholder = "/path/on/forge/host";
        input.setAttribute("aria-label", "Folder on the Forge host");
        input.autocomplete = "off";
        input.spellcheck = false;
        const add = el("button", ARCHIVES_CLASS + "-add-button", "Add");
        add.type = "submit";
        form.appendChild(input);
        form.appendChild(add);
        form.addEventListener("submit", function (event) {
            event.preventDefault();
            addArchive(input.value);
        });
        dialog.appendChild(form);
        const message = el("div", ARCHIVES_CLASS + "-message");
        message.setAttribute("role", "status");
        message.hidden = true;
        dialog.appendChild(message);
        const list = el("ul", ARCHIVES_CLASS + "-list");
        dialog.appendChild(list);
        layer.appendChild(dialog);
        layer.addEventListener("click", onArchivesClick);
        host.appendChild(layer);
        S.archivesDom = { root: layer, dialog: dialog, list: list, message: message, close: close, input: input, add: add };
        return S.archivesDom;
    }

    function archivesMessage(text, failure) {
        const dom = S.archivesDom;
        if (!dom) { return; }
        dom.message.textContent = text || "";
        dom.message.hidden = !text;
        dom.message.classList.toggle(ARCHIVES_CLASS + "-failure", !!failure);
    }

    function archiveEntry(archive, current) {
        const item = el("li", ARCHIVES_CLASS + "-entry");
        item.dataset.archive = archive.id;
        const words = el("div", ARCHIVES_CLASS + "-words");
        const name = el("b", ARCHIVES_CLASS + "-name", archive.name);
        words.appendChild(name);
        if (archive.id === current) { words.appendChild(el("span", "minipaint-clip-badge", "showing")); }
        if (!archive.available) { words.appendChild(el("span", "minipaint-clip-badge", "cannot be read now")); }
        words.appendChild(el("div", ARCHIVES_CLASS + "-where", archive.path));
        item.appendChild(words);
        const actions = el("div", ARCHIVES_CLASS + "-actions");
        const show = el("button", ARCHIVES_CLASS + "-show", archive.id === current ? "Showing" : "Show");
        show.type = "button";
        show.dataset.archiveShow = archive.id;
        show.disabled = archive.id === current;
        const remove = el("button", ARCHIVES_CLASS + "-remove", "Remove");
        remove.type = "button";
        remove.dataset.archiveRemove = archive.id;
        remove.title = "Take it off this list. The folder and its pictures are not touched.";
        actions.appendChild(show);
        actions.appendChild(remove);
        item.appendChild(actions);
        return item;
    }

    function drawArchives(answer) {
        const dom = buildArchives();
        if (!dom) { return; }
        const list = answer && Array.isArray(answer.archives) ? answer.archives : [];
        if (answer && answer.menu) { S.menuOverride = answer.menu; }
        dom.list.innerHTML = "";
        if (!list.length) {
            dom.list.appendChild(el("li", ARCHIVES_CLASS + "-empty", "No archives yet. Type a folder above and press Add."));
        }
        for (const archive of list) { dom.list.appendChild(archiveEntry(archive, String(answer.view || ""))); }
        drawViewName();
    }

    function openArchives() {
        const dom = buildArchives();
        if (!dom) { return Promise.resolve(null); }
        if (S.menu && !S.menu.hidden) { closeMenu(); }
        const was = archivesOpen();
        applyHeaderInset();
        watchHeaderInset();
        dom.root.hidden = false;
        if (!was) {
            announceOverlayAs(ARCHIVES_OVERLAY, true);
            if (S.archivesKey) { document.removeEventListener("keydown", S.archivesKey, true); }
            S.archivesKey = function (event) {
                if (event.key !== "Escape" || !archivesOpen()) { return; }
                event.stopPropagation();
                event.preventDefault();
                closeArchives();
            };
            document.addEventListener("keydown", S.archivesKey, true);
        }
        archivesMessage("", false);
        if (!dom.list.children.length) { dom.list.appendChild(el("li", ARCHIVES_CLASS + "-empty", "Reading…")); }
        try { dom.input.focus(); } catch (e) { /* focus is a courtesy */ }
        return askJson(ARCHIVES_ROUTE, null, ARCHIVES_TIMEOUT_MS, "archives").then(function (answer) {
            if (!archivesOpen()) { return answer; }
            if (!answer || answer.ok !== true) {
                drawArchives({ archives: [] });
                archivesMessage((answer && answer.message) || "Forge did not answer, so the archives could not be read. Close and try again.", true);
                return answer;
            }
            drawArchives(answer);
            return answer;
        });
    }

    function closeArchives() {
        const element = archivesRoot();
        if (!element || element.hidden) { return false; }
        element.hidden = true;
        if (S.archivesKey) { document.removeEventListener("keydown", S.archivesKey, true); S.archivesKey = null; }
        announceOverlayAs(ARCHIVES_OVERLAY, false);
        return true;
    }

    function archiveAction(body, done) {
        const dom = S.archivesDom;
        if (dom) { dom.add.disabled = true; }
        return askJson(ARCHIVES_ROUTE, body, ARCHIVES_TIMEOUT_MS, "archives").then(function (answer) {
            if (dom) { dom.add.disabled = false; }
            if (!answer) {
                archivesMessage("Forge did not answer; nothing was changed.", true);
                return null;
            }
            if (Array.isArray(answer.archives)) { drawArchives(answer); }
            archivesMessage(answer.message || "", answer.ok !== true);
            if (answer.ok === true && done) { done(answer); }
            return answer;
        });
    }

    function addArchive(path) {
        const text = String(path || "").trim();
        if (!text) { archivesMessage("Type a folder on the machine running Forge.", true); return Promise.resolve(null); }
        return archiveAction({ action: "add", path: text }, function () {
            if (S.archivesDom) { S.archivesDom.input.value = ""; }
            note("archives: one added");
        });
    }

    function onArchivesClick(event) {
        const dom = S.archivesDom;
        const target = event.target;
        if (!dom || !target) { return; }
        if (target === dom.root) { closeArchives(); return; }
        if (!target.closest) { return; }
        if (target.closest("." + ARCHIVES_CLASS + "-close")) { closeArchives(); return; }
        const show = target.closest("[data-archive-show]");
        if (show) {
            closeArchives();
            setView(show.dataset.archiveShow);
            return;
        }
        const remove = target.closest("[data-archive-remove]");
        if (remove) {
            const id = remove.dataset.archiveRemove;
            archiveAction({ action: "remove", id: id }, function (answer) {
                applyView(String(answer.view || ""));
                note("archives: one removed");
            });
        }
    }

    /* ------------------------------------------------------------------ */
    /* Full screen, from a tile                                              */
    /* ------------------------------------------------------------------ */
    //
    // The one place the grid ever loads a whole picture, and only the one
    // that was asked for. The thumbnail the tile already has is shown at
    // once and the full picture replaces it when it has arrived. Real full
    // screen where the browser grants it; where it does not (some phone
    // browsers), the view fills the window instead. A double tap - or a
    // double click, or Escape - closes it.
    //
    // Its own full screen, never the page's: the Forge Assistant's focus
    // mode makes the whole document full screen, and a view that asked "is
    // anything full screen?" would end the assistant's when it closed. See
    // inFullscreen, which the Outputs player learnt this from.

    //
    // THE WHOLE FOLDER, ONE PICTURE AT A TIME. Asked for: "make full screen
    // image view capable of browsing the folder in either random or date
    // sorted order. Exiting the view should land that image in view in the
    // gallery as if i scrolled to it." So the view steps through every
    // picture in the folder the grid shows - the whole storage folder, not
    // its page, or the whole archive - by the arrow keys, a swipe, or the
    // arrows drawn at its sides. **Date** is newest to oldest by the file's
    // time; **Random** shuffles the folder once, with the picture on screen
    // first, so stepping back retraces the way. The choice is the browser
    // tab's (sessionStorage), like the pictures per row. The next and the
    // previous whole pictures are fetched while one is looked at, so a step
    // is a swap. Leaving the view selects the picture it ended on and scrolls
    // the grid to it, turning the storage folder's page if it has to.

    const VIEWER_CLASS = "minipaint-clip-viewer";
    const VIEWER_OVERLAY = "clipboard-viewer";
    //: Two taps this close in time and place are a double tap.
    const DOUBLE_TAP_MS = 350;
    const DOUBLE_TAP_PX = 32;
    //: How long the hint says how to leave.
    const VIEWER_HINT_MS = 2200;
    //: A swipe: this far sideways, and more sideways than up or down.
    const SWIPE_PX = 48;
    //: The browser tab's choice of order.
    const VIEWER_ORDER_KEY = "minipaint-clipboard:viewer-order";
    const VIEWER_ORDERS = [["date", "Date"], ["random", "Random"]];
    //: One read of the storage folder's whole order.
    const VIEWER_ORDER_TIMEOUT_MS = 20000;

    function viewerOpen() { return !!(S.viewer && S.viewer.root && !S.viewer.root.hidden); }

    /** An arrow, drawn: a chevron pointing left or right. */
    function chevron(left) {
        const ns = "http://www.w3.org/2000/svg";
        const svg = document.createElementNS(ns, "svg");
        svg.setAttribute("viewBox", "0 0 24 24");
        svg.setAttribute("width", "28");
        svg.setAttribute("height", "28");
        svg.setAttribute("aria-hidden", "true");
        svg.setAttribute("focusable", "false");
        const path = document.createElementNS(ns, "path");
        path.setAttribute("d", left ? "M15 5l-7 7 7 7" : "M9 5l7 7-7 7");
        path.setAttribute("fill", "none");
        path.setAttribute("stroke", "currentColor");
        path.setAttribute("stroke-width", "2.4");
        path.setAttribute("stroke-linecap", "round");
        path.setAttribute("stroke-linejoin", "round");
        svg.appendChild(path);
        return svg;
    }

    function buildViewer() {
        if (S.viewer && S.viewer.root.isConnected) { return S.viewer; }
        const host = root();
        if (!host) { return null; }
        const layer = el("div", VIEWER_CLASS);
        layer.hidden = true;
        layer.setAttribute("role", "dialog");
        layer.setAttribute("aria-modal", "true");
        layer.setAttribute("aria-label", "Full screen picture. Arrows step through the folder; double-tap to close.");
        layer.tabIndex = -1;
        const picture = document.createElement("img");
        picture.className = VIEWER_CLASS + "-picture";
        picture.alt = "";
        picture.draggable = false;
        const hint = el("div", VIEWER_CLASS + "-hint", "Double-tap to close");
        const previous = el("button", VIEWER_CLASS + "-nav " + VIEWER_CLASS + "-prev");
        previous.type = "button";
        previous.dataset.viewer = "prev";
        previous.setAttribute("aria-label", "Previous picture");
        previous.title = "Previous (←)";
        previous.appendChild(chevron(true));
        const next = el("button", VIEWER_CLASS + "-nav " + VIEWER_CLASS + "-next");
        next.type = "button";
        next.dataset.viewer = "next";
        next.setAttribute("aria-label", "Next picture");
        next.title = "Next (→)";
        next.appendChild(chevron(false));
        const bar = el("div", VIEWER_CLASS + "-bar");
        const where = el("span", VIEWER_CLASS + "-where", "");
        where.setAttribute("aria-live", "polite");
        const orders = el("span", VIEWER_CLASS + "-orders");
        orders.setAttribute("role", "radiogroup");
        orders.setAttribute("aria-label", "Order");
        for (const pair of VIEWER_ORDERS) {
            const choice = el("button", VIEWER_CLASS + "-order", pair[1]);
            choice.type = "button";
            choice.dataset.viewer = "order";
            choice.dataset.order = pair[0];
            choice.setAttribute("role", "radio");
            orders.appendChild(choice);
        }
        bar.appendChild(where);
        bar.appendChild(orders);
        layer.appendChild(picture);
        layer.appendChild(previous);
        layer.appendChild(next);
        layer.appendChild(bar);
        layer.appendChild(hint);
        layer.addEventListener("pointerdown", onViewerDown);
        layer.addEventListener("pointerup", onViewerTap);
        layer.addEventListener("click", onViewerClick);
        layer.addEventListener("dblclick", function (event) { event.preventDefault(); });
        // The page's own menu is not this view's; a right click here does nothing.
        layer.addEventListener("contextmenu", function (event) { event.preventDefault(); });
        host.appendChild(layer);
        S.viewer = { root: layer, picture: picture, hint: hint, lastTap: null, full: null, hintTimer: 0,
                     fullscreen: false, key: null, change: null, url: "",
                     previous: previous, next: next, where: where, orders: orders, down: null,
                     // The folder: its date order, the order in use, where
                     // the view is in it, and what it shows.
                     dated: null, list: null, at: -1, current: null, order: viewerOrder(),
                     reading: null, ahead: [] };
        return S.viewer;
    }

    function viewerOrder() {
        try {
            const kept = window.sessionStorage.getItem(VIEWER_ORDER_KEY);
            return kept === "random" ? "random" : "date";
        } catch (e) {
            return "date";
        }
    }

    function viewerInFullscreen() {
        const now = fullscreenNow();
        return !!(now && S.viewer && now === S.viewer.root);
    }

    /** One picture of the folder: its id, its name and its version. */
    function viewerItemOf(tile) {
        return { id: tile.dataset.asset || "", name: tile.dataset.name || "", v: tile.dataset.v || "" };
    }

    function viewerFullUrl(item) {
        return isArchiveRef(item.id) ? archiveFullUrl(item.id, item.v)
            : "/minipaint-clipboard/image/" + encodeURIComponent(item.id) + (item.v ? "?v=" + encodeURIComponent(item.v) : "");
    }

    function viewerThumbUrl(item) {
        return isArchiveRef(item.id) ? archiveThumbUrl(item.id, item.v) : imageUrl(item.id, item.v);
    }

    /** Newest first, by the time in a version (``<mtime_ns>-<size>``). */
    function newestFirst(a, b) {
        const x = String(a.v || "").split("-")[0];
        const y = String(b.v || "").split("-")[0];
        if (x.length !== y.length) { return y.length - x.length; }
        return x < y ? 1 : x > y ? -1 : 0;
    }

    /**
     * The folder the grid shows, newest first. An archive's listing is
     * already on the page; the storage folder's is read once, whole, since
     * the grid holds only its page.
     */
    function viewerFolder(source) {
        if (isArchiveRef(source)) {
            const items = S.archive.items.map(function (item) { return { id: archiveRef(item[0]), name: item[1], v: item[3] }; });
            items.sort(newestFirst);
            return Promise.resolve(items);
        }
        return askJson(LIBRARY_ROUTE + "?every=1&sort=newest", null, VIEWER_ORDER_TIMEOUT_MS, "the viewer's folder")
            .then(function (answer) {
                if (!answer || answer.ok !== true || !Array.isArray(answer.items)) { return null; }
                return answer.items.map(function (item) { return { id: item[0], name: item[1], v: item[2] }; });
            });
    }

    /** A shuffle of the folder with the picture on screen first. */
    function shuffled(items, first) {
        const rest = items.filter(function (item) { return item.id !== first; });
        for (let i = rest.length - 1; i > 0; i -= 1) {
            const j = Math.floor(Math.random() * (i + 1));
            const swap = rest[i];
            rest[i] = rest[j];
            rest[j] = swap;
        }
        const head = items.find(function (item) { return item.id === first; });
        return head ? [head].concat(rest) : rest;
    }

    /** Put the folder in the chosen order around the picture on screen. */
    function arrangeViewer(view) {
        if (!view.dated) { view.list = null; view.at = -1; drawViewerControls(view); return; }
        const id = view.current ? view.current.id : "";
        view.list = view.order === "random" ? shuffled(view.dated, id) : view.dated.slice();
        view.at = view.list.findIndex(function (item) { return item.id === id; });
        drawViewerControls(view);
        preloadAround(view);
    }

    function drawViewerControls(view) {
        const count = view.list ? view.list.length : 0;
        const at = view.at;
        view.previous.disabled = !(count && at > 0);
        view.next.disabled = !(count && at >= 0 && at < count - 1);
        const words = function (value) { try { return Number(value).toLocaleString(); } catch (e) { return String(value); } };
        view.where.textContent = count && at >= 0 ? words(at + 1) + " of " + words(count)
            : view.reading ? "Reading the folder…" : "";
        for (const choice of Array.from(view.orders.children)) {
            const on = choice.dataset.order === view.order;
            choice.setAttribute("aria-checked", on ? "true" : "false");
            choice.classList.toggle(VIEWER_CLASS + "-order-on", on);
        }
    }

    /** Show one picture: the whole one if it is here, its thumbnail until it is. */
    function showInViewer(view, item, small) {
        view.current = item;
        const full = viewerFullUrl(item);
        view.url = full;
        const ready = view.ahead.find(function (held) { return held.url === full && held.image.complete && held.image.naturalWidth; });
        const stand = small || viewerThumbUrl(item);
        view.picture.src = ready ? full : stand;
        view.root.dataset.loaded = ready ? "1" : "0";
        view.root.dataset.current = item.id;
        if (ready) { view.full = null; return; }
        const loader = new Image();
        view.full = loader;
        loader.onload = function () {
            if (view.full !== loader || !viewerOpen()) { return; }
            view.picture.src = full;
            view.root.dataset.loaded = "1";
        };
        loader.onerror = function () {
            if (view.full !== loader) { return; }
            view.root.dataset.loaded = "failed";
            note("viewer: the full picture could not be read");
        };
        loader.src = full;
    }

    /** The whole pictures either side, fetched while this one is looked at. */
    function preloadAround(view) {
        if (!view.list || view.at < 0) { return; }
        const wanted = [view.list[view.at + 1], view.list[view.at - 1]].filter(Boolean).map(viewerFullUrl);
        view.ahead = view.ahead.filter(function (held) { return wanted.indexOf(held.url) !== -1; });
        for (const url of wanted) {
            if (view.ahead.some(function (held) { return held.url === url; })) { continue; }
            const image = new Image();
            image.src = url;
            view.ahead.push({ url: url, image: image });
        }
    }

    /** One step through the folder: +1 is the next picture, -1 the one before. */
    function stepViewer(by) {
        const view = S.viewer;
        if (!viewerOpen() || !view.list || view.at < 0) { return false; }
        const at = view.at + by;
        if (at < 0 || at >= view.list.length) { return false; }
        view.at = at;
        showInViewer(view, view.list[at], "");
        drawViewerControls(view);
        preloadAround(view);
        return true;
    }

    function setViewerOrder(order) {
        const view = S.viewer;
        const wanted = order === "random" ? "random" : "date";
        if (!view) { return; }
        try { window.sessionStorage.setItem(VIEWER_ORDER_KEY, wanted); } catch (e) { /* this view only, then */ }
        // Random again reshuffles; Date again changes nothing.
        if (wanted === view.order && wanted === "date") { drawViewerControls(view); return; }
        view.order = wanted;
        arrangeViewer(view);
    }

    /**
     * Open a tile's picture over everything. Called inside the press, so
     * the browser takes the full-screen request as the user's own.
     */
    function openViewer(tile) {
        if (!tile) { return false; }
        const view = buildViewer();
        if (!view) { return false; }
        const item = viewerItemOf(tile);
        const archive = isArchiveRef(item.id);
        const shown = tile.querySelector("img");
        const small = shown && shown.getAttribute("src") ? shown.getAttribute("src") : "";
        view.ahead = [];
        view.order = viewerOrder();
        showInViewer(view, item, small);
        const was = viewerOpen();
        view.root.hidden = false;
        if (!was) {
            announceOverlayAs(VIEWER_OVERLAY, true);
            view.key = onViewerKey;
            document.addEventListener("keydown", view.key, true);
            view.change = function () {
                // Left by the browser's own way out (Escape, a gesture):
                // the view goes with it. Only when it was this view's.
                if (view.fullscreen && !viewerInFullscreen()) { view.fullscreen = false; closeViewer(); }
            };
            document.addEventListener("fullscreenchange", view.change);
            document.addEventListener("webkitfullscreenchange", view.change);
        }
        // The folder, read afresh on every open (what was added since is in
        // it) while the first picture is already on screen.
        view.dated = null;
        view.list = null;
        view.at = -1;
        const reading = viewerFolder(archive ? item.id : "");
        view.reading = reading;
        reading.then(function (items) {
            if (view.reading !== reading) { return; }
            view.reading = null;
            if (!items) { note("viewer: the folder could not be read; this picture only"); drawViewerControls(view); return; }
            view.dated = items;
            if (viewerOpen()) { arrangeViewer(view); }
        });
        drawViewerControls(view);
        const enter = view.root.requestFullscreen || view.root.webkitRequestFullscreen;
        if (enter && (document.fullscreenEnabled || document.webkitFullscreenEnabled)) {
            try {
                const asked = enter.call(view.root);
                view.fullscreen = true;
                if (asked && typeof asked.then === "function") {
                    asked.then(null, function () { view.fullscreen = false; view.root.dataset.fill = "window"; });
                }
            } catch (e) {
                view.fullscreen = false;
                view.root.dataset.fill = "window";
            }
        } else {
            view.root.dataset.fill = "window";
        }
        view.hint.hidden = false;
        if (view.hintTimer) { clearTimeout(view.hintTimer); }
        view.hintTimer = setTimeout(function () { view.hint.hidden = true; }, VIEWER_HINT_MS);
        try { view.root.focus({ preventScroll: true }); } catch (e) { /* focus is a courtesy */ }
        note("viewer: opened " + (archive ? "an archive picture" : "a library picture"));
        return true;
    }

    function closeViewer() {
        const view = S.viewer;
        if (!view || view.root.hidden) { return false; }
        if (viewerInFullscreen()) {
            const leave = document.exitFullscreen || document.webkitExitFullscreen;
            if (leave) { try { leave.call(document); } catch (e) { /* it was leaving anyway */ } }
        }
        const landed = view.current;
        view.fullscreen = false;
        view.full = null;
        view.ahead = [];
        view.root.hidden = true;
        delete view.root.dataset.fill;
        view.picture.removeAttribute("src");
        if (view.hintTimer) { clearTimeout(view.hintTimer); view.hintTimer = 0; }
        if (view.key) { document.removeEventListener("keydown", view.key, true); view.key = null; }
        if (view.change) {
            document.removeEventListener("fullscreenchange", view.change);
            document.removeEventListener("webkitfullscreenchange", view.change);
            view.change = null;
        }
        announceOverlayAs(VIEWER_OVERLAY, false);
        if (landed && landed.id) { landOn(landed.id); }
        return true;
    }

    /**
     * The grid, at the picture the view ended on: selected, and scrolled to
     * as though you had scrolled there - in the middle of the grid where it
     * can be. The storage folder turns to the page that holds it first.
     */
    function landOn(id) {
        if (S.selected !== id) { select(id); }
        if (isArchiveRef(id)) {
            const key = id.slice(ARCHIVE_REF_PREFIX.length + 17);
            const index = S.archive.index.get(key);
            const grid = gridElement();
            const layout = S.archive.layout;
            if (index === undefined || !grid || !layout) { return Promise.resolve(false); }
            const row = Math.floor(index / layout.columns);
            const top = layout.padTop + row * (layout.cellHeight + layout.gap);
            grid.scrollTop = Math.max(0, Math.round(top - (grid.clientHeight - layout.cellHeight) / 2));
            S.archive.scrollTop = grid.scrollTop;
            renderArchive();
            pumpThumbs(true);
            return Promise.resolve(true);
        }
        const centre = function () {
            const tile = S.library.tiles.get(id);
            const grid = gridElement();
            if (!tile || !grid || !tile.isConnected) { return false; }
            const box = tile.getBoundingClientRect();
            const frame = grid.getBoundingClientRect();
            grid.scrollTop = Math.max(0, Math.round(grid.scrollTop + (box.top - frame.top) - (grid.clientHeight - box.height) / 2));
            return true;
        };
        if (S.library.tiles.has(id) && S.library.ids.indexOf(id) !== -1) { return Promise.resolve(centre()); }
        // Another page: the server says which (it alone holds the whole
        // order), and that page is drawn before the grid is moved.
        return fetchLibrary({ quiet: true }).then(function (answer) {
            const page = answer ? Number(answer.selected_page) : -1;
            if (!answer || page < 0 || page === Number(answer.page)) { return answer; }
            return fetchLibrary({ page: page, quiet: true });
        }).then(function () {
            return centre();
        });
    }

    function onViewerKey(event) {
        if (!viewerOpen()) { return; }
        const key = event.key;
        let handled = true;
        if (key === "Escape") { closeViewer(); }
        else if (key === "ArrowRight" || key === "ArrowDown" || key === "PageDown") { stepViewer(1); }
        else if (key === "ArrowLeft" || key === "ArrowUp" || key === "PageUp") { stepViewer(-1); }
        else { handled = false; }
        if (handled) { event.stopPropagation(); event.preventDefault(); }
    }

    function onViewerClick(event) {
        const control = event.target && event.target.closest ? event.target.closest("[data-viewer]") : null;
        if (!control) { return; }
        event.preventDefault();
        event.stopPropagation();
        const what = control.dataset.viewer;
        if (what === "prev") { stepViewer(-1); }
        else if (what === "next") { stepViewer(1); }
        else if (what === "order") { setViewerOrder(control.dataset.order); }
    }

    function onViewerDown(event) {
        const view = S.viewer;
        if (!view) { return; }
        view.down = { x: event.clientX, y: event.clientY, id: event.pointerId };
    }

    /**
     * A swipe steps; two taps (or clicks) in one place, close together,
     * close. A press on the view's own controls is theirs, never a tap.
     */
    function onViewerTap(event) {
        const view = S.viewer;
        if (!view || (event.button !== undefined && event.button > 0)) { return; }
        const down = view.down;
        view.down = null;
        if (event.target && event.target.closest && event.target.closest("[data-viewer]")) { view.lastTap = null; return; }
        if (down && down.id === event.pointerId) {
            const dx = event.clientX - down.x;
            const dy = event.clientY - down.y;
            if (Math.abs(dx) >= SWIPE_PX && Math.abs(dx) > Math.abs(dy) * 1.5) {
                view.lastTap = null;
                event.preventDefault();
                stepViewer(dx < 0 ? 1 : -1);
                return;
            }
        }
        const now = Date.now();
        const last = view.lastTap;
        if (last && now - last.at <= DOUBLE_TAP_MS
            && Math.abs(event.clientX - last.x) <= DOUBLE_TAP_PX && Math.abs(event.clientY - last.y) <= DOUBLE_TAP_PX) {
            view.lastTap = null;
            event.preventDefault();
            closeViewer();
            return;
        }
        view.lastTap = { at: now, x: event.clientX, y: event.clientY };
    }

    /** What the view is showing, for the checks. */
    function viewerState() {
        const view = S.viewer;
        if (!view) { return { open: false }; }
        return { open: viewerOpen(), current: view.current ? view.current.id : "", at: view.at,
                 count: view.list ? view.list.length : 0, order: view.order,
                 list: view.list ? view.list.map(function (item) { return item.id; }) : [],
                 ahead: view.ahead.map(function (held) { return held.url; }), loaded: view.root.dataset.loaded || "" };
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
        if (section === "archives") {
            // What the grid shows: the storage folder or one archive, ticked;
            // and the screen that adds and removes archives.
            const list = back.slice();
            const current = archiveActive() ? S.archive.view : "";
            list.push({ menu: "view", value: "", label: tick(!current) + "Storage folder" });
            for (const pair of archiveList()) {
                list.push({ menu: "view", value: pair[0], label: tick(current === pair[0]) + pair[1] });
            }
            if (!archiveList().length) { list.push({ menu: "status", label: "No archives yet" }); }
            list.push({ menu: "archives", label: "Manage archives…" });
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
            { menu: "section", value: "archives", label: "Archives ›" },
            { menu: "press", value: PRESS.rename, label: "Rename selected…", disabled: !S.selected || isArchiveRef(S.selected) },
            { menu: "press", value: PRESS.remove, label: "Delete selected…", disabled: !S.selected || isArchiveRef(S.selected) },
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
                if (value === PRESS.refresh) { refreshGrid(); }
                if (value === PRESS.history) { askQueue(null); }
                return;
            case "intercept": closeMenu(); setIntercept(value); return;
            // Over the tab's own route, and the grid re-drawn from the
            // answer. The hidden box still goes so the toolbar dropdown
            // follows on a page whose Gradio is alive; nothing waits on it.
            case "sort": closeMenu(); setSort(value); sendInput(BOXES.sortRequest, value + ":" + Date.now()); return;
            case "paste": closeMenu(); pasteFromClipboard(); return;
            case "send": closeMenu(); sendTo(value); return;
            case "view": closeMenu(); setView(value); return;
            case "archives": closeMenu(); openArchives(); return;
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
            // renders; the grid is read over HTTP, so it is right whether or
            // not this press reaches anything.
            pressHidden(PRESS.refresh);
            libraryChanged();
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
            // For the cards and the menu; the grid is read over HTTP.
            pressHidden(PRESS.refresh);
            libraryChanged();
            toast(slot ? "Imported and placed in the slot."
                : archiveActive() ? "Saved to the storage folder. The archive on screen is not written to."
                : "Imported into Clipboard.");
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
        // needs nothing from this page from here - not a pump, not a watch -
        // and the page may be closed the moment this line is written. It is
        // a history entry now; the history says what became of it.
        if (parsed.executor === "server" || (parsed.state && SERVER_STATES.indexOf(String(parsed.state)) !== -1)) {
            note("queue: job " + String(parsed.job_id).slice(0, 8) + " admitted by the server" + how + "; it is in the history");
            toast("Sent to WanGP. It is in Queue Send History.");
            return;
        }
        note("queue: job " + String(parsed.job_id).slice(0, 8) + " appended by the server" + how + "; pumping");
        if (!pump()) {
            toast("WanGP is not available in this page.", true);
        }
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

    /** This page's own queue work moved - a submission, or a job this page
     * ran itself finishing: show it, and re-read the history once. Nothing
     * here arrives from the server on its own any more. */
    function onOutboxEvent(event) {
        const detail = event && event.detail ? event.detail : {};
        if (detail.kind !== "submitted" && detail.kind !== "done") { return; }
        const job = detail.job || null;
        if (detail.kind === "done" && job) {
            const failed = job.state !== "queued" && job.state !== "started";
            const ignored = (job.result && job.result.ignored || []).map(function (item) { return item.field; });
            toast(outboxSentence(job) + (!failed && ignored.length ? " " + ignored.map(function (f) {
                return f === "references" ? "Reference" : f === "start" ? "First frame" : f === "end" ? "Last frame" : "Prompt";
            }).join(", ") + " was not used by the current model." : ""), failed);
            refreshCapabilities(true);
        }
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
    //: The WanGP page's own save before a press keeps to two seconds; this
    //: is the backstop for one that does not, a little longer on purpose.
    const SAVE_TIMEOUT_MS = 2500;

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
            if (record.settings) {
                entry.appendChild(el("div", "minipaint-clip-history-settings", "Settings: " + record.settings));
            }
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
        // Read now, before the wait below: the press is what was on screen.
        const typed = String(prompt === undefined || prompt === null ? promptValue() : prompt);
        return saveWanGP().then(function (flushed) {
            return askQueue({
                action: "add",
                prompt: typed,
                enhance: typeof enhanceOn === "boolean" ? enhanceOn : undefined,
                model: model,
                settings_flush: flushed
            });
        }).then(function (answer) {
            if (!answer) { toast("Forge is not answering; nothing was queued.", true); return null; }
            // A refused press carries the server's fresh readiness answer:
            // the section redraws itself blocked from it, with its reason.
            if (answer.readiness) { applyReadiness(answer.readiness); }
            if (answer.instruction) { queue(JSON.stringify(answer.instruction), "the queue route"); }
            return answer;
        });
    }

    /**
     * WanGP's form, saved just before the press goes to the server, which
     * builds the job from what WanGP saved. Two seconds at most, and never a
     * reason not to queue: see the WanGP bundle's saveForSend.
     */
    function saveWanGP() {
        const bridge = window.minipaintWanGP;
        if (!bridge || typeof bridge.saveForSend !== "function") { return Promise.resolve("unavailable"); }
        let asked;
        try { asked = bridge.saveForSend("Clipboard's Add to Queue"); } catch (e) { return Promise.resolve("unavailable"); }
        return new Promise(function (resolve) {
            let settled = false;
            const done = function (value) { if (!settled) { settled = true; clearTimeout(backstop); resolve(value); } };
            const backstop = setTimeout(function () { done("unavailable"); }, SAVE_TIMEOUT_MS);
            Promise.resolve(asked).then(function (answer) {
                done(answer && typeof answer.flush === "string" ? answer.flush : "unavailable");
            }, function () { done("unavailable"); });
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
        if (!api || typeof api.wangp.capabilities !== "function") { setLine("WanGP: not available in this page", "off"); S.capabilities = null; refreshBadges(); refreshReadiness(false); return; }
        if (!force && Date.now() - S.capabilitiesAt < CAPABILITY_THROTTLE_MS) { return; }
        S.capabilitiesAt = Date.now();
        setLine("WanGP: checking…", "checking");
        api.wangp.capabilities().then(function (answer) {
            S.capabilities = answer;
            sendModel(answer);
            // The model is what readiness is about, so it is asked again the
            // moment the page knows which one WanGP is on.
            refreshReadiness(false);
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
            refreshReadiness(false);
        });
    }

    /* ------------------------------------------------------------------ */
    /* WanGP readiness: the section, only while WanGP can take it           */
    /* ------------------------------------------------------------------ */

    const READINESS_ROUTE = "/minipaint-clipboard/readiness";
    const BLOCKED_ID = "minipaint_clipboard_blocked";
    //: One ask of the readiness route. The check behind it is milliseconds
    //: inside WanGP and the server has its own, shorter deadline for a
    //: bridge that is not answering; this one is for a Forge that is not.
    const READINESS_TIMEOUT_MS = 15000;
    const READINESS_CHECKING = "Checking whether WanGP is ready…";
    const READINESS_SILENT = "Forge did not say whether WanGP is ready. Press Check again.";

    /** The answer, onto the page: the root's two attributes, the quiet line. */
    function applyReadiness(view) {
        S.readiness = view && typeof view === "object" ? view : null;
        const ready = !!(S.readiness && S.readiness.ready === true);
        const element = root();
        if (element) {
            element.dataset.wangpReady = ready ? "1" : "0";
            element.dataset.wangpTarget = ready ? String(S.readiness.target || "") : "";
            // Whether the model is sent a reference, from the fields the
            // server says it is sent (``targets.FIELDS``) - so a model added
            // there needs no rule here. Said only for a known target: a ready
            // answer that names none keeps every card, as it always did.
            const fields = ready && S.readiness.target && Array.isArray(S.readiness.fields) ? S.readiness.fields : null;
            element.dataset.wangpReferences = fields ? (fields.indexOf("references") >= 0 ? "1" : "0") : "";
        }
        const host = byId(BLOCKED_ID);
        const box = host ? host.querySelector(".minipaint-clip-blocked") : null;
        const text = box ? box.querySelector(".minipaint-clip-blocked-text") : null;
        if (box) { box.dataset.code = ready ? "" : String((S.readiness && S.readiness.code) || "checking"); }
        if (text) { text.textContent = ready ? "" : String((S.readiness && S.readiness.message) || READINESS_CHECKING); }
        // A blocked section has no enhancement button, so it has no editor
        // open over the window either - and no prompt box for History to
        // load into, so no History open over it.
        if (!ready && editorOpen()) { closePromptEditor(); }
        if (!ready && promptHistoryOpen()) { closePromptHistory(); }
        const said = ready ? "ready for " + String(S.readiness.target || "?")
            : "blocked - " + String((S.readiness && S.readiness.code) || "no answer");
        if (said !== S.readinessSaid) {
            S.readinessSaid = said;
            note("readiness: " + said);
        }
        return ready;
    }

    /**
     * Ask whether the section may be used, for the model this page last
     * learned WanGP is on. Reads coalesce; ``fresh`` (Check again) skips the
     * server's few seconds of cache. An answer for a model the page has since
     * moved off is followed by one more ask, for the model it is on now.
     */
    function refreshReadiness(fresh) {
        if (S.readinessRead && !fresh) { return S.readinessRead; }
        const asked = modelJson();
        let model = null;
        try { model = JSON.parse(asked || "null"); } catch (e) { model = null; }
        let controller = null;
        let cutoff = 0;
        try { controller = typeof AbortController === "function" ? new AbortController() : null; } catch (e) { controller = null; }
        if (controller) {
            cutoff = setTimeout(function () { try { controller.abort(); } catch (e) { /* settled */ } }, READINESS_TIMEOUT_MS);
        }
        const read = fetch(READINESS_ROUTE, Object.assign({
            method: "POST", credentials: "same-origin", cache: "no-store",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ model: model, fresh: !!fresh, page: pageId() })
        }, controller ? { signal: controller.signal } : {})).then(function (response) {
            return response.json();
        }).then(function (answer) {
            if (answer && answer.ok && answer.readiness) { return answer.readiness; }
            return { ready: false, code: (answer && answer.code) || "UNANSWERED", message: (answer && answer.message) || READINESS_SILENT };
        }, function () {
            return { ready: false, code: "UNANSWERED", message: READINESS_SILENT };
        }).then(function (view) {
            if (cutoff) { clearTimeout(cutoff); }
            if (S.readinessRead === settled) { S.readinessRead = null; }
            applyReadiness(view);
            if (modelJson() !== asked) { refreshReadiness(false); }
            return view;
        });
        const settled = read;
        S.readinessRead = settled;
        return settled;
    }

    /* ------------------------------------------------------------------ */
    /* Attach                                                                */
    /* ------------------------------------------------------------------ */

    function onRootClick(event) {
        const target = event.target;
        if (!target || !target.closest) { return; }
        const expand = target.closest(".minipaint-clip-expand");
        if (expand) {
            const tile = expand.closest(".minipaint-clip-item");
            event.preventDefault();
            event.stopPropagation();
            // Selected as well as shown, so a send after closing it sends
            // the picture that was just looked at.
            if (tile && tile.dataset.asset !== S.selected) { select(tile.dataset.asset); }
            openViewer(tile);
            return;
        }
        const item = target.closest(".minipaint-clip-item");
        if (item) {
            // Two taps (or clicks) on one picture, close together: PNG Info.
            // The first has already selected it, or taken a selection away,
            // so the second leaves it selected whatever the first did.
            if (isDoubleTap(item, event)) {
                event.preventDefault();
                sendToPngInfo(item);
                return;
            }
            select(item.dataset.asset === S.selected ? "" : item.dataset.asset);
            return;
        }
        const recheck = target.closest(".minipaint-clip-blocked-check");
        if (recheck) {
            event.preventDefault();
            refreshCapabilities(true);
            refreshReadiness(true);
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

    /* -- a picture's own gestures ------------------------------------- */
    //
    // Asked for: "give pictures a double tap action that sends them to PNG
    // info. This should be a permanent shortcut", and "make right click on a
    // image open the full screen image view". Both on every tile, in the
    // storage folder and in an archive; neither is a setting. A tile says
    // `touch-action: manipulation` (TILE_GEOMETRY), so a phone's browser
    // neither zooms on the double tap nor holds each tap back to wait for one.
    // A long press on a phone is the browser's own context menu, so it opens
    // the full-screen view too.

    //: The last tap on a tile: which, when and where.
    function isDoubleTap(item, event) {
        const now = Date.now();
        const last = S.lastTileTap;
        const x = Number(event.clientX) || 0;
        const y = Number(event.clientY) || 0;
        const twice = !!last && last.asset === item.dataset.asset && now - last.at <= DOUBLE_TAP_MS
            && Math.abs(x - last.x) <= DOUBLE_TAP_PX && Math.abs(y - last.y) <= DOUBLE_TAP_PX;
        // A third tap starts a new pair rather than sending twice.
        S.lastTileTap = twice ? null : { asset: item.dataset.asset, at: now, x: x, y: y };
        return twice;
    }

    /** The picture, selected, to PNG Info - the one shortcut that is not a menu's. */
    function sendToPngInfo(item) {
        const asset = item.dataset.asset || "";
        if (!asset) { return; }
        if (S.selected !== asset) { select(asset); }
        const offered = (menuState().destinations || []).some(function (pair) { return pair[0] === "pnginfo"; });
        if (!offered) {
            toast("This WebUI has no PNG Info tab to send to.", true);
            return;
        }
        note("send pnginfo by a double tap");
        sendTo("pnginfo");
    }

    /** A right click (a long press on a phone): the picture full screen. */
    function onRootContextMenu(event) {
        const target = event.target;
        const item = target && target.closest ? target.closest(".minipaint-clip-item") : null;
        if (!item || !root() || !root().contains(item)) { return; }
        event.preventDefault();
        if (item.dataset.asset !== S.selected) { select(item.dataset.asset); }
        openViewer(item);
    }

    function onKeydown(event) {
        const item = event.target && event.target.closest ? event.target.closest(".minipaint-clip-item") : null;
        if (!item) { return; }
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(item.dataset.asset); }
        if (event.key === "f" || event.key === "F") { event.preventDefault(); select(item.dataset.asset); openViewer(item); }
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
                    // Opening the tab is one of the moments the grid is read -
                    // over HTTP, and with a folder re-read, because coming
                    // back to the tab is exactly when the folder may have
                    // moved underneath it.
                    fetchLibrary({ quiet: true, refresh: true });
                    // An archive is not re-listed for a look - an archive is
                    // an archive - but its view was not measured while hidden.
                    if (archiveActive()) { onArchiveResize(); }
                    refreshCapabilities(false);
                    // Whatever the throttle above decided: coming back to
                    // the tab is when a model changed in the WanGP tab shows.
                    refreshReadiness(false);
                }
            }, 50);
        });
    }

    function attach() {
        const element = root();
        if (!element || S.attached) { return; }
        S.attached = true;
        element.addEventListener("click", onRootClick);
        element.addEventListener("contextmenu", onRootContextMenu);
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
        // the server: it is the thing the user is looking at.
        gridMount();
        watchFit();
        scheduleFit();
        // The view this page was built with first - an archive, when one was
        // on screen - and then the one the host holds now, over HTTP, since a
        // page's built-in state is as old as the page.
        drawViewName();
        applyView(String(menuState().view || ""));
        askJson(ARCHIVES_ROUTE, null, ARCHIVES_TIMEOUT_MS, "archives").then(function (answer) {
            if (!answer || answer.ok !== true) { return; }
            if (answer.menu) { S.menuOverride = Object.assign({}, S.menuOverride || {}, answer.menu); }
            applyView(String(answer.view || ""));
        });
        fetchLibrary({ quiet: true, refresh: true });
        // And the queue: what ran while this browser was closed, from the
        // route rather than from a framework render that a closed browser
        // could never have received.
        askQueue(null);
        afterRender();
        // Blocked until the first answer: the server rendered the quiet line
        // and nothing else of the section is drawn before this page says so.
        applyReadiness(null);
        refreshReadiness(false);
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
                 readiness: S.readiness ? { ready: S.readiness.ready === true, code: S.readiness.code || "",
                                            target: S.readiness.target || "" } : null,
                 retryDelay: S.retryDelay,
                 offline: { server: S.offline.server, queue: S.offline.queue },
                 editorOpen: editorOpen(),
                 outputsOpen: outputsOpen(),
                 promptHistory: { open: promptHistoryOpen(), entries: S.prompts.entries, loading: S.prompts.loading },
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
                 archive: archiveState(),
                 archivesOpen: archivesOpen(),
                 viewerOpen: viewerOpen(),
                 fitted: S.fitted || null };
    }

    /** What the archive loader is doing, for the journal and the checks. */
    function archiveState() {
        const rows = archiveActive() ? archiveRows() : null;
        return { view: S.archive.view, total: S.archive.items.length, busy: S.archive.busy,
                 rendered: S.archive.rendered.size, inflight: Array.from(S.archive.inflight.keys()),
                 thumbs: S.archive.thumbs.size, columns: S.archive.layout ? S.archive.layout.columns : 0,
                 rows: rows ? { first: rows.first, last: rows.last } : null,
                 started: S.archive.stats.started, dropped: S.archive.stats.dropped,
                 done: S.archive.stats.done, failed: S.archive.stats.failed,
                 order: S.archive.stats.order.slice(-200),
                 visible: archiveActive() ? wantedThumbs().visible : [],
                 scrubbing: !!S.scrub, paused: !!(S.scrub && S.scrub.paused) };
    }

    /** The row as drawn: for the checks, and for a console. */
    function rowState() {
        return { perRow: S.perRow, wanted: S.perRowWanted, thumb: S.thumb,
                 range: S.perRowRange ? { min: S.perRowRange.min, max: S.perRowRange.max } : null };
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
        openPromptHistory: openPromptHistory,
        closePromptHistory: closePromptHistory,
        loadPrompt: loadPrompt,
        askOutputs: askOutputs,
        loadRecipe: loadRecipe,
        fit: fitTab,
        closeMenu: closeMenu,
        select: select,
        setPerRow: setPerRow,
        rowState: rowState,
        viewerState: viewerState,
        stepViewer: stepViewer,
        setViewerOrder: setViewerOrder,
        reportTiles: reportTiles,
        pasteFromClipboard: pasteFromClipboard,
        setIntercept: setIntercept,
        interceptTarget: interceptTarget,
        queue: queue,
        addToQueue: addToQueue,
        cancelAll: cancelAll,
        refreshQueue: refreshOutbox,
        refreshAll: refreshAll,
        libraryChanged: libraryChanged,
        pump: pump,
        pageId: pageId,
        modelJson: modelJson,
        refreshCapabilities: refreshCapabilities,
        refreshReadiness: refreshReadiness,
        readiness: function () { return S.readiness ? Object.assign({}, S.readiness) : null; },
        pressHidden: pressHidden,
        showOffline: connectionNotice,
        receiveOverHttp: receiveOverHttp,
        galleryPictureUrl: galleryPictureUrl,
        setView: setView,
        openArchives: openArchives,
        closeArchives: closeArchives,
        archiveState: archiveState,
        openViewer: function (assetId) {
            const grid = gridElement();
            const tile = grid ? Array.from(grid.querySelectorAll(".minipaint-clip-item")).find(function (one) {
                return one.dataset.asset === assetId;
            }) : null;
            return openViewer(tile || null);
        },
        closeViewer: closeViewer,
        debug: debug
    };
})();
