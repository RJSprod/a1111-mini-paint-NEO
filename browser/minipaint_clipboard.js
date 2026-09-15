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
    const GRID_ID = "minipaint_clipboard_grid";
    const MENU_ID = "minipaint_clipboard_menu";
    const STATUS_ID = "minipaint_clipboard_status";
    const QUEUE_STATUS_ID = "minipaint_clipboard_queue_status";
    const WANGP_LINE_ID = "minipaint_clipboard_wangp_line";
    const TAB_PANEL_ID = "tab_minipaint_clipboard";
    //: Matches the stylesheet's own default for --minipaint-clip-thumb.
    const DEFAULT_THUMB = 144;
    const IMPORT_ROUTE = "/minipaint-clipboard/import";
    const SEND_ROUTE = "/minipaint-clipboard/send";
    const BOXES = {
        selected: "minipaint_clipboard_selected",
        sortRequest: "minipaint_clipboard_sort_request",
        slotAction: "minipaint_clipboard_slot_action",
        sendRequest: "minipaint_clipboard_send_request",
        sendAck: "minipaint_clipboard_send_ack",
        historyAction: "minipaint_clipboard_history_action",
        menuState: "minipaint_clipboard_menu_state",
        queueInstruction: "minipaint_clipboard_queue_instruction",
        outboxAction: "minipaint_clipboard_outbox_action",
        pageId: "minipaint_clipboard_page_id",
        model: "minipaint_clipboard_model"
    };
    const PRESS = {
        refresh: "minipaint_clipboard_refresh",
        upload: "minipaint_clipboard_upload",
        intercept: "minipaint_clipboard_intercept",
        folder: "minipaint_clipboard_folder_open",
        rename: "minipaint_clipboard_rename_open",
        remove: "minipaint_clipboard_delete_open",
        paste: "minipaint_clipboard_paste_open",
        history: "minipaint_clipboard_history_open",
        outboxRefresh: "minipaint_clipboard_outbox_refresh"
    };
    const ROLE_IDS = { first: "minipaint_clipboard_to_first", last: "minipaint_clipboard_to_last", ref: "minipaint_clipboard_to_ref" };
    const SLOT_UPLOAD_PREFIX = "minipaint_clipboard_slot_upload_";
    const SLOT_FIELDS = { first: "start", last: "end", ref: "references" };
    // The press acknowledgement. A Gradio chained callback writes the
    // server's answer into a hidden box, and on some installed Gradio
    // versions the change event that would tell us never fires - so there is
    // a fallback that reads the value.
    //
    // It used to read it six times a second from the moment of the press.
    // That is a busy loop on the main thread for a value that arrives once,
    // and it ran whether or not anything was in flight. Now: an observer on
    // the box first, because a mutation is the event the poll was standing
    // in for, and the value read only starts after the delay below - long
    // enough that a working install never reaches it - and only while a
    // press is actually outstanding.
    const QUEUE_ACK_DELAY_MS = 2000;
    const QUEUE_WATCH_MS = 500;
    const QUEUE_WATCH_LIMIT_MS = 15000;
    const CAPABILITY_THROTTLE_MS = 2500;
    // Job states the server runs. A page that sees one of these watches the
    // event stream instead of pumping: the job continues whether or not this
    // document exists.
    const SERVER_STATES = ["admitted", "waiting_turn", "enhanced", "ensuring_wangp", "composing",
        "waiting_for_card", "submitting_wangp", "wangp_waiting", "wangp_generating",
        "completed", "execution_unknown"];
    const OUTBOX_REFRESH_THROTTLE_MS = 300;

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
        menuOutside: null,
        menuKey: null,
        queued: {},
        armedAt: 0,
        watch: 0,
        lastInstruction: "",
        ackDelay: 0,
        ackObserver: null,
        capabilitiesAt: 0,
        capabilities: null,
        lastModel: "",
        pasteListener: null,
        outboxListener: null,
        outboxRefreshTimer: null,
        toastTimer: null
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
        target.value = text;
        target.dispatchEvent(new Event("input", { bubbles: true }));
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
        try { return JSON.parse(boxValue(BOXES.menuState) || "{}") || {}; } catch (e) { return {}; }
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
     * A standing line saying the live connection is gone, with the one
     * action that brings it back.
     *
     * A toast is the wrong shape for this: it hides itself after a few
     * seconds, and the thing it is reporting lasts until the page is
     * reloaded. Every send after it goes the long way round - twelve
     * seconds of waiting before the direct route is tried - so the user is
     * told once, plainly, rather than being left to infer it from a send
     * that took an age.
     */
    function connectionNotice(show) {
        const container = byId(BROWSER_ID) || root();
        if (!container) { return; }
        let bar = container.querySelector(".minipaint-clip-offline");
        if (!show) {
            if (bar) { bar.hidden = true; }
            return;
        }
        if (!bar) {
            bar = document.createElement("div");
            bar.className = "minipaint-clip-offline";
            bar.setAttribute("role", "status");
            const line = document.createElement("span");
            line.className = "minipaint-clip-offline-text";
            line.textContent = "This page has lost its live connection to Forge. Sending still works - it "
                + "goes the direct way and takes a few seconds longer. Reconnecting makes it quick again.";
            const button = document.createElement("button");
            button.type = "button";
            button.className = "minipaint-clip-offline-reconnect";
            button.textContent = "Reconnect";
            button.title = "Reloads this page.";
            button.addEventListener("click", function () { window.location.reload(); });
            bar.appendChild(line);
            bar.appendChild(button);
            container.insertBefore(bar, container.firstChild);
        }
        bar.hidden = false;
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
        for (const slot in ROLE_IDS) {
            const host = byId(ROLE_IDS[slot]);
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
        else if (listed.length && !onGrid(keep)) { keep = ""; }
        select(keep, true);
        if (confirmed !== keep) { sendInput(BOXES.selected, keep); }
        // The grid element is new after every refresh, so the size written
        // onto the old one went with it. Put the remembered one back rather
        // than falling to the default and snapping every tile back to 144.
        applyThumbnailSize(S.thumb);
        refreshBadges();
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
        if (grid) { grid.style.setProperty("--minipaint-clip-thumb", value + "px"); }
    }

    function setThumbnailSize(size) { applyThumbnailSize(size); }

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
        if (section === "sort") {
            const list = [{ menu: "back", label: "‹ Back" }];
            for (const pair of state.sorts || []) {
                list.push({ menu: "sort", value: pair[0], label: tick(state.sort === pair[0]) + pair[1] });
            }
            return list;
        }
        if (section === "send") {
            const list = [{ menu: "back", label: "‹ Back" }];
            if (!S.selected) { list.push({ menu: "status", label: "Select an image first" }); }
            for (const pair of state.destinations || []) {
                list.push({ menu: "send", value: pair[0], label: pair[1], disabled: !S.selected });
            }
            if (!(state.destinations || []).length) { list.push({ menu: "status", label: "No destination is available in this WebUI" }); }
            list.push({ menu: "close", label: "Cancel" });
            return list;
        }
        return [
            { menu: "press", value: PRESS.intercept, label: tick(!!state.intercept) + "Intercept “Send to Mini Paint”" },
            { menu: "press", value: PRESS.refresh, label: "Refresh" },
            { menu: "section", value: "sort", label: "Sort ›" },
            { menu: "paste", label: "Paste image" },
            { menu: "press", value: PRESS.upload, label: "Upload image(s)…" },
            { menu: "press", value: PRESS.folder, label: "Choose storage folder…" },
            { menu: "press", value: PRESS.rename, label: "Rename selected…", disabled: !S.selected },
            { menu: "press", value: PRESS.remove, label: "Delete selected…", disabled: !S.selected },
            { menu: "section", value: "send", label: "Send selected to ›" },
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
            case "press": closeMenu(); pressHidden(value); return;
            case "sort": closeMenu(); sendInput(BOXES.sortRequest, value + ":" + Date.now()); return;
            case "paste": closeMenu(); pasteFromClipboard(); return;
            case "send": closeMenu(); sendTo(value); return;
            default: return;
        }
    }

    function positionMenu() {
        const button = byId(MENU_ID);
        const column = byId(BROWSER_ID);
        if (!button || !column || !S.menu) { return; }
        const b = button.getBoundingClientRect();
        const c = column.getBoundingClientRect();
        S.menu.style.top = (b.bottom - c.top + 4) + "px";
        S.menu.style.left = Math.max(0, b.left - c.left) + "px";
    }

    function openMenu() {
        if (!S.menu) { return; }
        renderMenu(null);
        S.menu.hidden = false;
        positionMenu();
        S.menuOutside = function (event) {
            const target = event.target;
            if (S.menu.contains(target)) { return; }
            if (target && target.closest && target.closest("#" + MENU_ID)) { return; }
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
        if (S.menuOutside) { document.removeEventListener("pointerdown", S.menuOutside, true); S.menuOutside = null; }
        if (S.menuKey) { document.removeEventListener("keydown", S.menuKey, true); S.menuKey = null; }
    }

    function toggleMenu() {
        if (!S.menu) { buildMenu(); }
        if (!S.menu) { return; }
        if (S.menu.hidden) { openMenu(); } else { closeMenu(); }
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
    //: Once a send has gone unanswered, the queue is not carrying anything
    //: and every send after it would sit through the same wait for the same
    //: answer. The page stops giving it as long: the direct route is tried
    //: almost at once, and the first acknowledgement to arrive puts the
    //: patient deadline back.
    const SEND_RETRY_TIMEOUT_MS = 2500;

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
    function sendTo(target) {
        // Held for the whole round trip. The fallback runs twelve seconds
        // after this point and used to read the selection again when it
        // got there: a render landing in between left it with nothing to
        // send, and it returned without a word.
        const asset = S.selected;
        if (!asset) { toast("Select an image first.", true); return; }
        note("send " + target + " chosen from the menu");
        const stamp = String(Date.now());
        if (!sendInput(BOXES.sendRequest, target + ":" + asset + ":" + stamp)) {
            // The hidden box is not on the page: the tab is half-built and
            // nothing was sent. This path used to return false and be
            // dropped by the caller, which is how a dead button stayed quiet.
            note("send " + target + ": the hidden request box is not on the page; nothing was sent");
            toast("Mini Paint could not reach its send control. Reload the page.", true);
            return;
        }
        watchSend(target, stamp, asset);
    }

    /**
     * Wait for the server's receipt for THIS send, and say so if it never
     * comes.
     *
     * The stamp is the whole point. The status line was the obvious thing to
     * watch and it is the wrong thing: it says "Sent ..." and is then
     * rewritten by the next refresh, so a successful send looks unanswered a
     * few seconds later and an unanswered one looks fine if anything else
     * wrote to it meanwhile. The acknowledgement box carries back the stamp
     * this call put on its own request, so there is exactly one value that
     * means "the server handled the thing I just asked for".
     */
    function watchSend(target, stamp, asset) {
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
            note("send " + target + ": acknowledged by the server");
        }, 250);
        const deadline = S.queueDown ? SEND_RETRY_TIMEOUT_MS : SEND_TIMEOUT_MS;
        S.sendWatch = setTimeout(function () {
            done();
            if (boxValue(BOXES.sendAck) === stamp) { return; }
            S.queueDown = true;
            note("send " + target + ": no acknowledgement after "
                 + (deadline / 1000) + "s; falling back to the direct route");
            connectionNotice(true);
            sendOverHttp(target, asset);
        }, deadline);
    }

    /**
     * Finish a send over plain HTTP, because the queue did not carry it.
     *
     * The event stream, the imports and the thumbnails all ride ordinary
     * HTTP and keep working when Gradio's queue stops delivering; only the
     * actions were tied to the queue, which is why a page that could still
     * talk to the server could not send a picture out of this tab.
     *
     * img2img and Inpaint finish completely here: their pictures are
     * delivered by writing a hidden textbox, which is browser work either
     * way, so nothing is missing. The destinations the server writes cannot
     * be finished without it, and say so rather than looking like they went.
     */
    async function sendOverHttp(target, asset) {
        const picture = asset || S.selected;
        if (!picture) {
            // Reachable only if a send started with nothing selected, which
            // sendTo refuses - but a fallback that can return without
            // saying anything is how the last one of these went missing.
            note("send " + target + ": there is no longer a picture to send");
            toast("That send could not be finished: nothing is selected any more.", true);
            return false;
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
            note("send " + target + ": the direct route could not be reached either");
            toast("That send never reached the server - this page has lost its connection. Reload it and try again.", true);
            return false;
        }
        if (!plan || !plan.ok) {
            note("send " + target + ": the direct route refused it (" + ((plan && plan.code) || "unknown") + ")");
            toast((plan && plan.message) || "That picture could not be sent.", true);
            return false;
        }
        if (plan.elem && plan.payload) {
            const outcome = deliverByUpload(plan.elem, plan.payload, plan.filename);
            if (outcome === "sent") {
                note("send " + target + ": handed to " + plan.elem + " as an upload, without the queue");
                toast("Sent " + plan.filename + " to " + (plan.label || target)
                      + (plan.adds ? " (added to what is already there)" : "")
                      + " (the page had lost its connection).");
                return true;
            }
            if (outcome === "occupied") {
                note("send " + target + ": " + plan.elem + " already holds a picture, so it has no upload to use");
                toast((plan.label || target) + " already has a picture in it. Clear that one and send again, "
                      + "or reconnect the page.", true);
                return false;
            }
            note("send " + target + ": " + (outcome === "missing"
                 ? plan.elem + " is not on this page" : "the picture could not be turned into a file"));
            toast("There is no " + (plan.label || target) + " on this page to send to.", true);
            return false;
        }
        if (plan.backend || !plan.payload) {
            note("send " + target + ": prepared, but " + (plan.label || target)
                 + " is not on this page to be written directly");
            toast((plan.label || target) + " is filled in by the server, so this one needs the connection back. "
                  + "Reload the page and try again.", true);
            return false;
        }
        const canvas = window.minipaintCanvas;
        if (!canvas || typeof canvas.deliverToHost !== "function") {
            note("send " + target + ": the Canvas adapter this page delivers through is not loaded");
            toast("That send could not be completed by this page. Reload it and try again.", true);
            return false;
        }
        const delivered = canvas.deliverToHost(plan.instruction, plan.payload, plan.box);
        if (!delivered) {
            note("send " + target + ": nowhere on this page to put it");
            toast("There is no " + (plan.label || target) + " on this page to send to.", true);
            return false;
        }
        note("send " + target + ": delivered over the direct route without the queue");
        toast("Sent " + plan.filename + " to " + (plan.label || target) + " (the page had lost its connection).");
        return true;
    }

    /**
     * Hand a Gradio component a picture as a file, the way a person would.
     *
     * The Extras image and the ImageStitch galleries hold their value in
     * the component rather than in a box on the page, and the server writes
     * them by returning a new value - a Gradio event, and so the queue. But
     * a component that accepts uploads will take the same picture from its
     * own file input, which travels the ordinary upload route: the half of
     * the connection that is still working when the queue is not.
     *
     * The input is only there while the component is empty; once it holds a
     * picture Gradio shows that instead. Saying so is more use than
     * silently doing nothing.
     */
    function fileFromDataUrl(dataUrl, filename) {
        const comma = String(dataUrl || "").indexOf(",");
        if (comma < 0) { return null; }
        const head = dataUrl.slice(0, comma);
        const match = head.match(/data:([^;,]+)/);
        const mime = (match && match[1]) || "image/png";
        let binary;
        try { binary = atob(dataUrl.slice(comma + 1)); } catch (e) { return null; }
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i += 1) { bytes[i] = binary.charCodeAt(i); }
        return new File([bytes], filename || "clipboard.png", { type: mime });
    }

    function deliverByUpload(elemId, dataUrl, filename) {
        const target = byId(elemId);
        if (!target) { return "missing"; }
        const input = target.querySelector('input[type="file"]');
        if (!input) { return "occupied"; }
        const file = fileFromDataUrl(dataUrl, filename);
        if (!file) { return "unreadable"; }
        try {
            const transfer = new DataTransfer();
            transfer.items.add(file);
            input.files = transfer.files;
        } catch (e) {
            return "unreadable";
        }
        input.dispatchEvent(new Event("change", { bubbles: true }));
        return "sent";
    }

    /* ------------------------------------------------------------------ */
    /* Add to Queue                                                          */
    /* ------------------------------------------------------------------ */

    function interop() {
        const api = window.minipaintInterop;
        return api && api.wangp && typeof api.wangp.enqueue === "function" ? api : null;
    }

    /** The click was made: notice the server's answer, however it arrives.
     *
     * Three ways, in the order they cost anything: the chained callback's own
     * change event (free, and what happens on a healthy install), a mutation
     * observer on the box (free, and what catches a Gradio that writes the
     * value without dispatching), and only then a value read on a timer that
     * starts two seconds late and stops the moment the answer lands or the
     * press gives up. A page sitting idle runs none of them. */
    function armQueue() {
        S.armedAt = Date.now();
        disarmQueue();
        const started = Date.now();
        const box = textarea(BOXES.queueInstruction);

        function settle(from) {
            const value = boxValue(BOXES.queueInstruction);
            if (!value || value === S.lastInstruction) { return false; }
            disarmQueue();
            queue(value, from);
            return true;
        }

        if (box && typeof MutationObserver === "function") {
            S.ackObserver = new MutationObserver(function () { settle("observer"); });
            try {
                S.ackObserver.observe(box, { attributes: true, attributeFilter: ["value"], childList: true, characterData: true, subtree: true });
            } catch (e) { S.ackObserver = null; }
        }
        S.ackDelay = setTimeout(function () {
            S.ackDelay = 0;
            if (settle("value")) { return; }
            S.watch = setInterval(function () {
                if (settle("value")) { return; }
                if (Date.now() - started > QUEUE_WATCH_LIMIT_MS) { disarmQueue(); }
            }, QUEUE_WATCH_MS);
        }, QUEUE_ACK_DELAY_MS);
    }

    function disarmQueue() {
        if (S.watch) { clearInterval(S.watch); S.watch = 0; }
        if (S.ackDelay) { clearTimeout(S.ackDelay); S.ackDelay = 0; }
        if (S.ackObserver) { try { S.ackObserver.disconnect(); } catch (e) { /* already gone */ } S.ackObserver = null; }
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
        disarmQueue();
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

    /** Ask the public API to keep this page's view fresh. Idempotent. */
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

    /** The public API's cancel-all has run on the server; callers still
     * waiting on those jobs get their answers, and the list is re-read. */
    function afterCancelAll() {
        const api = interop();
        if (api && api.wangp && typeof api.wangp.refreshWaiters === "function") {
            try { api.wangp.refreshWaiters(); } catch (e) { /* the callers' business */ }
        }
        refreshOutbox();
    }

    function refreshOutbox() {
        if (S.outboxRefreshTimer) { return; }
        S.outboxRefreshTimer = setTimeout(function () {
            S.outboxRefreshTimer = null;
            pressHidden(PRESS.outboxRefresh);
        }, OUTBOX_REFRESH_THROTTLE_MS);
    }

    /** The public API says a job moved: show it, and let the server re-render the list. */
    function onOutboxEvent(event) {
        const detail = event && event.detail ? event.detail : {};
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
            sendInput(BOXES.historyAction, action.dataset.historyAction + ":" + Date.now());
            return;
        }
        const outbox = target.closest("[data-outbox-action]");
        if (outbox) {
            event.preventDefault();
            const verb = String(outbox.dataset.outboxAction || "").split(":")[0];
            sendInput(BOXES.outboxAction, outbox.dataset.outboxAction + ":" + pageId() + ":" + Date.now());
            if (verb === "retry" || verb === "adopt") { setTimeout(pump, 400); }
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
                    pressHidden(PRESS.refresh);
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
        // The page's identity, so a press submits under it; and the jobs this
        // page composed before a reload resume without another press - as
        // does the tracking of the ones WanGP already took from this page.
        sendInput(BOXES.pageId, pageId());
        watchTab();
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
                 capabilities: S.capabilities, watching: !!S.watch, lastInstruction: S.lastInstruction.slice(0, 40), page: pageId(),
                 model: S.lastModel };
    }

    return {
        attach: attach,
        afterRender: afterRender,
        toggleMenu: toggleMenu,
        closeMenu: closeMenu,
        select: select,
        setThumbnailSize: setThumbnailSize,
        pasteFromClipboard: pasteFromClipboard,
        armQueue: armQueue,
        queue: queue,
        pump: pump,
        pageId: pageId,
        modelJson: modelJson,
        afterCancelAll: afterCancelAll,
        refreshCapabilities: refreshCapabilities,
        pressHidden: pressHidden,
        debug: debug
    };
})();
