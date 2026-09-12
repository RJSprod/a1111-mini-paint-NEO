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
 * Add to Queue is the one thing that leaves the page. The server builds the
 * public request from the draft - inherited fields omitted - and writes it
 * into a hidden box; the box's change hands it to window.minipaintInterop,
 * the same API any extension uses, and the result comes back into another
 * hidden box for the server to record. Nothing here talks to the WanGP
 * iframe, knows a bridge session, or sees a file path.
 *
 * Nothing polls. The one bounded watcher runs for a few seconds after the
 * Add to Queue click, in case the chained change never reaches this page,
 * and stops the moment the instruction is delivered either way.
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
    const IMPORT_ROUTE = "/minipaint-clipboard/import";
    const BOXES = {
        selected: "minipaint_clipboard_selected",
        sortRequest: "minipaint_clipboard_sort_request",
        slotAction: "minipaint_clipboard_slot_action",
        sendRequest: "minipaint_clipboard_send_request",
        historyAction: "minipaint_clipboard_history_action",
        menuState: "minipaint_clipboard_menu_state",
        queueInstruction: "minipaint_clipboard_queue_instruction",
        queueResult: "minipaint_clipboard_queue_result"
    };
    const PRESS = {
        refresh: "minipaint_clipboard_refresh",
        upload: "minipaint_clipboard_upload",
        intercept: "minipaint_clipboard_intercept",
        folder: "minipaint_clipboard_folder_open",
        rename: "minipaint_clipboard_rename_open",
        remove: "minipaint_clipboard_delete_open",
        paste: "minipaint_clipboard_paste_open",
        history: "minipaint_clipboard_history_open"
    };
    const ROLE_IDS = { first: "minipaint_clipboard_to_first", last: "minipaint_clipboard_to_last", ref: "minipaint_clipboard_to_ref" };
    const SLOT_UPLOAD_PREFIX = "minipaint_clipboard_slot_upload_";
    const SLOT_FIELDS = { first: "start", last: "end", ref: "references" };
    const QUEUE_WATCH_MS = 150;
    const QUEUE_WATCH_LIMIT_MS = 15000;
    const CAPABILITY_THROTTLE_MS = 2500;

    const S = {
        attached: false,
        selected: "",
        menu: null,
        menuSection: null,
        menuOutside: null,
        menuKey: null,
        queued: {},
        armedAt: 0,
        watch: 0,
        lastInstruction: "",
        capabilitiesAt: 0,
        capabilities: null,
        pasteListener: null,
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

    /** The grid was re-rendered: keep the selection the server confirmed, re-apply the size. */
    function afterRender() {
        const confirmed = boxValue(BOXES.selected);
        const present = items().some(function (item) { return item.dataset.asset === confirmed; });
        select(present ? confirmed : "", true);
        if (!present && confirmed) { sendInput(BOXES.selected, ""); }
        applyThumbnailSize();
        refreshBadges();
    }

    function applyThumbnailSize(size) {
        const grid = byId(GRID_ID);
        const slider = byId("minipaint_clipboard_thumb");
        const input = slider ? slider.querySelector("input[type=range]") : null;
        const value = Number(size) || (input ? Number(input.value) : 0) || 144;
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

    function sendTo(target) {
        if (!S.selected) { toast("Select an image first.", true); return; }
        note("send " + target + " chosen from the menu");
        sendInput(BOXES.sendRequest, target + ":" + S.selected + ":" + Date.now());
    }

    /* ------------------------------------------------------------------ */
    /* Add to Queue                                                          */
    /* ------------------------------------------------------------------ */

    function interop() {
        const api = window.minipaintInterop;
        return api && api.wangp && typeof api.wangp.enqueue === "function" ? api : null;
    }

    /** The click was made: watch the instruction box briefly in case the
     * chained change never runs on this page. */
    function armQueue() {
        S.armedAt = Date.now();
        if (S.watch) { clearInterval(S.watch); S.watch = 0; }
        const started = Date.now();
        S.watch = setInterval(function () {
            const value = boxValue(BOXES.queueInstruction);
            if (value && value !== S.lastInstruction) {
                clearInterval(S.watch); S.watch = 0;
                queue(value, true);
                return;
            }
            if (Date.now() - started > QUEUE_WATCH_LIMIT_MS) { clearInterval(S.watch); S.watch = 0; }
        }, QUEUE_WATCH_MS);
    }

    function writeResult(nonce, request, result) {
        sendInput(BOXES.queueResult, JSON.stringify({
            nonce: nonce,
            request_id: (result && result.request_id) || (request && request.request_id) || "",
            ok: !!(result && result.ok),
            status: (result && result.status) || "refused",
            code: (result && result.code) || "",
            tasks_added: (result && result.tasks_added) || 0,
            applied: (result && result.applied) || {},
            inherited: (result && result.inherited) || [],
            ignored: (result && result.ignored) || [],
            model: (result && result.model) || {},
            t: Date.now()
        }));
    }

    /** The instruction box changed: hand the public request to the public API. */
    async function queue(instruction, fromWatcher) {
        const text = String(instruction || "");
        if (!text || text === S.lastInstruction) { return; }
        let parsed = null;
        try { parsed = JSON.parse(text); } catch (e) { parsed = null; }
        if (!parsed || !parsed.nonce || !parsed.request) { return; }
        if (S.queued[parsed.nonce]) { return; }
        S.queued[parsed.nonce] = true;
        S.lastInstruction = text;
        if (S.watch) { clearInterval(S.watch); S.watch = 0; }
        const api = interop();
        if (!api) {
            note("queue: the public API is not on this page");
            writeResult(parsed.nonce, parsed.request, { ok: false, status: "refused", code: "IFRAME_NOT_READY" });
            toast("WanGP is not available in this page.", true);
            return;
        }
        note("queue " + String(parsed.request.request_id || "").slice(0, 8) + ": handed to window.minipaintInterop" + (fromWatcher ? " (read from the box)" : ""));
        let result;
        try {
            result = await api.wangp.enqueue(parsed.request);
        } catch (error) {
            result = { ok: false, status: "refused", code: "INTERNAL_ERROR", message: String(error && error.message || error).slice(0, 120) };
        }
        writeResult(parsed.nonce, parsed.request, result);
        if (result && result.ok) {
            const ignored = (result.ignored || []).map(function (item) { return item.field; });
            toast("Added to WanGP queue." + (ignored.length ? " " + ignored.map(function (f) { return f === "references" ? "Reference" : f === "start" ? "First frame" : f === "end" ? "Last frame" : "Prompt"; }).join(", ") + " was not used by the current model." : ""));
        } else {
            toast((result && result.message) || "The request was not queued.", true);
        }
        refreshCapabilities(true);
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

    /** One bounded question to the public API, throttled, never on a timer. */
    function refreshCapabilities(force) {
        const api = interop();
        if (!api || typeof api.wangp.capabilities !== "function") { setLine("WanGP: not available in this page", "off"); S.capabilities = null; refreshBadges(); return; }
        if (!force && Date.now() - S.capabilitiesAt < CAPABILITY_THROTTLE_MS) { return; }
        S.capabilitiesAt = Date.now();
        setLine("WanGP: checking…", "checking");
        api.wangp.capabilities().then(function (answer) {
            S.capabilities = answer;
            if (!answer || !answer.ok) {
                setLine("WanGP unavailable" + (answer && answer.code ? " (" + answer.code + ")" : "") + " — Add to Queue will ask anyway", "off");
            } else {
                const inputs = answer.inputs || {};
                const takes = ["start", "end", "references"].filter(function (f) { return inputs[f] && inputs[f].supported; });
                const label = (answer.model && (answer.model.label || answer.model.type)) || "model";
                setLine("WanGP: " + label + " · takes " + (takes.length ? takes.map(function (f) { return f === "start" ? "first" : f === "end" ? "last" : "reference"; }).join(", ") : "no image")
                    + (answer.queue === false ? " · queue not offered by this bridge" : ""), answer.queue === false ? "off" : "ready");
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
        watchTab();
        afterRender();
        if (tabVisible()) { refreshCapabilities(false); }
    }

    function debug() {
        return { attached: S.attached, selected: S.selected, menuOpen: !!(S.menu && !S.menu.hidden), menuSection: S.menuSection,
                 capabilities: S.capabilities, watching: !!S.watch, lastInstruction: S.lastInstruction.slice(0, 40) };
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
        refreshCapabilities: refreshCapabilities,
        pressHidden: pressHidden,
        debug: debug
    };
})();
