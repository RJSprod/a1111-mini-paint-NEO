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
        toastTimer: null,
        //: When the grid was last re-rendered by the server. A round trip
        //: landing is what tells the page the connection is back.
        renderedAt: 0
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
     * reloaded. What is lost is not the sending - that happens here now -
     * but everything the server has to answer for: the status line, the
     * queue, a grid that shows a picture added since. Said once, plainly,
     * rather than left to be inferred from a page that quietly stops
     * keeping up.
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
            line.textContent = "This page has lost its live connection to Forge. Sending still works - "
                + "pictures are placed by the page itself. The status line, the queue and new "
                + "thumbnails need the connection back.";
            const button = document.createElement("button");
            button.type = "button";
            button.className = "minipaint-clip-offline-reconnect";
            button.textContent = "Check again";
            button.title = "Asks the server for the library again, and clears this line if it answers.";
            // NEVER A RELOAD. This offered one, and on a Forge behind its own
            // TLS front end reloading took the whole WebUI page with it - the
            // session gone, for a line that is only ever advisory. A button
            // that can cost more than the problem it reports is not a repair.
            button.addEventListener("click", function () { checkConnection(); });
            bar.appendChild(line);
            bar.appendChild(button);
            container.insertBefore(bar, container.firstChild);
        }
        bar.hidden = false;
    }

    /**
     * Ask the server for something small, and clear the notice if it answers.
     *
     * A re-render of the grid is a whole Gradio round trip - the press goes
     * over the queue and the answer comes back through the same channel the
     * notice is about - so one arriving is the evidence the line is stale.
     */
    function checkConnection() {
        const before = S.renderedAt;
        pressHidden(PRESS.refresh);
        let waited = 0;
        const timer = setInterval(function () {
            waited += 500;
            if (S.renderedAt !== before) {
                clearInterval(timer);
                connectionNotice(false);
                toast("The connection is back.");
                return;
            }
            if (waited >= 8000) {
                clearInterval(timer);
                toast("Still no answer from the server. Sending works; the rest needs it back.", true);
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
        S.renderedAt = Date.now();
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
        const state = menuState();
        if (!state.intercept) {
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
        try {
            const response = await fetch(url, { credentials: "same-origin", cache: "no-store" });
            if (!response.ok) { throw new Error("the host would not serve it (" + response.status + ")"); }
            const blob = await response.blob();
            const asset = await importBlob(blob, nameFromUrl(url), "forge_gallery");
            note("receive: put into the library over the direct route without the queue");
            toast("Put " + ((asset && asset.filename) || "the picture") + " in Clipboard "
                  + "(the page had lost its connection).");
            pressHidden(PRESS.refresh);
            return true;
        } catch (error) {
            const why = (error && error.message) || String(error);
            note("receive: could not put the picture into the library: " + why);
            toast("That picture could not be put into Clipboard: " + why, true);
            return false;
        }
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
        if (!sendInput(BOXES.sendRequest, target + ":" + asset + ":" + stamp + marked)) {
            // The hidden box is not on the page: the tab is half-built. The
            // picture may still have gone, so say which happened.
            note("send " + target + ": the hidden request box is not on the page");
            if (!(outcome && outcome.ok)) {
                toast("Mini Paint could not reach its send control. Reload the page.", true);
                return;
            }
        }
        if (outcome && outcome.ok) {
            note("send " + target + ": delivered from the page" + (outcome.reason ? " (" + outcome.reason + ")" : ""));
            toast("Sent " + (outcome.filename || "the picture") + " to " + (outcome.label || target)
                  + (outcome.adds ? " (added to what is already there)" : ""));
        }
        watchSend(target, stamp, asset, outcome);
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
    function watchSend(target, stamp, asset, outcome) {
        const delivered = !!(outcome && outcome.ok);
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
                toast("Sent " + (again.filename || "the picture") + " to " + (again.label || target)
                      + " (the page had lost its connection).");
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
        if (plan.backend || !plan.payload) {
            // Nothing on this page to put it in: the server has to do it.
            return { ok: false, label: label, filename: plan.filename,
                     reason: label + " is filled in by the server, and this page has no live connection to it" };
        }
        const canvas = window.minipaintCanvas;
        if (!canvas || typeof canvas.deliverToHost !== "function") {
            return { ok: false, label: label, filename: plan.filename,
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
            reason: (delivered && delivered.reason) || "",
            label: label,
            filename: plan.filename,
            adds: !!plan.adds
        };
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
        receiveOverHttp: receiveOverHttp,
        debug: debug
    };
})();
