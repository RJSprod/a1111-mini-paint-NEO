/**
 * Send to WanGP, from the gallery: the compact request popup.
 *
 * The 🖌️ button under a txt2img / img2img / Extras result can be pointed at
 * WanGP (Clipboard's menu, Intercept Options). When it is, the server freezes
 * the picked picture as a transient - never a Clipboard asset - and the
 * receive chain's last step hands this file one opaque handoff and opens the
 * popup. Everything the popup shows comes from one answer of Clipboard's
 * intercept route: which generic image roles the current model takes and
 * which is the default, the shared prompt and enhancer switch, whether WanGP
 * is idle or generating, whether Generate is a button, and the history.
 * Generate hands the visible values back to the same route, which builds the
 * request the way the Clipboard composer would and puts it in the same
 * queue; the popup closes the moment the server has stored it.
 *
 * THIS FILE KNOWS NO MODEL. It renders the roles it is given, ticks the
 * defaults it is given, and sends the ids it is given. Which roles exist for
 * which model, and what "reference" means to WanGP, is Clipboard's mapping
 * layer on the server; a new model family is a change there and not here.
 * Nothing here parses a model name, names a WanGP component, or holds a
 * queue of its own.
 *
 * Fetched lazily - the first time a gallery send is actually pointed at
 * WanGP - and never parsed by a session that keeps the button on Mini Paint.
 * The WanGP bridge and the public queue API are loaded beside it, for the
 * one question only the live page can answer (what does it take right now)
 * and for the one job this page has to run itself (a browser-executed one).
 * Every request here is bounded; nothing is held open.
 */
window.minipaintIntercept = (function () {
    "use strict";

    const ROUTE = "/minipaint-clipboard/intercept";
    const IMAGE_ROUTE = "/minipaint-clipboard/intercept/image/";
    const STAGE_ROUTE = "/minipaint-interop/stage";
    const ENHANCE_SETTINGS_ROUTE = "/minipaint-clipboard/enhance-settings";
    const PREFIX = "wangp:";
    //: The public API's own key, so a page that loads the API later shares
    //: the identity its jobs were submitted under.
    const PAGE_KEY = "minipaint.interop.page";
    const HEX32 = /^[0-9a-f]{32}$/;
    //: How long one request to the server may take. Bounded, because a
    //: request that never returns is a browser connection held for ever on
    //: HTTP/1.1, and a page has six.
    const ASK_TIMEOUT_MS = 20000;
    //: How long the live page is given to say what it takes, before the
    //: popup goes with what the server knows. The bridge has its own bound;
    //: this is the popup's, and it is the shorter of the two on purpose.
    const BRIDGE_TIMEOUT_MS = 6000;
    //: The direct route (stageAndOpen): how long each of its two requests may
    //: take, how many times it tries, and how long it waits in between. On
    //: 2026-09-23 the page's one HTTP/2 connection to Forge stalled after the
    //: tab had been in the background for an hour; every press of the button
    //: added another request that never came back, eleven in all, the popup
    //: never opened, and nothing said why until a reload cancelled them. The
    //: connection answered again as soon as those requests were cancelled, so
    //: a stalled send is cancelled on a deadline and tried once more.
    const STAGE_TIMEOUT_MS = 15000;
    const STAGE_ATTEMPTS = 2;
    const STAGE_RETRY_DELAY_MS = 1500;
    const CLIPBOARD_PROMPT_ID = "minipaint_clipboard_prompt";
    const CLIPBOARD_ENHANCE_ID = "minipaint_clipboard_enhance_toggle";
    const CLASS = "minipaint-intercept";
    const TOAST_MS = 4500;
    //: Published when this popup takes over the page, and again when it gives
    //: way. Anything else that floats over Forge can put itself away while a
    //: dialog is up and bring itself back afterwards, without either side
    //: importing the other or knowing the other's class names.
    //:
    //: Dispatched on `document`, because that is the one node every extension
    //: on the page can reach. `detail.name` says which overlay, so a listener
    //: can ignore the ones it does not care about; `detail.open` says which
    //: way. The event is fire-and-forget: nothing here waits for a listener,
    //: and a page with no listeners behaves exactly as it did.
    //:
    //: Its first listener is SD-Neo-ModelSwitchRefiner's Forge Assistant,
    //: whose launcher sat on top of this popup.
    const OVERLAY_EVENT = "minipaint:overlay";
    const OVERLAY_NAME = "intercept";

    const S = {
        dom: null,
        open: false,
        //: The direct route's send in progress, if any (see stageAndOpen),
        //: and whether the page is being reloaded or closed.
        staging: null,
        leaving: false,
        leavingWatched: false,
        handoff: "",
        token: "",
        tab: "",
        size: "",
        //: The server's last describe answer, and the live page's facts as
        //: last gathered (model, inputs, generating), sent back with every
        //: submission so the roles are judged against what is true now.
        describe: null,
        facts: null,
        roles: { available: [], defaults: [] },
        //: The roles the user last chose in this session, kept across
        //: popups so a run of sends does not re-tick the same boxes.
        userRoles: null,
        //: Every token this page has opened on. A handoff is one press's,
        //: and a press that reaches this file twice - Gradio runs a chained
        //: browser step whether or not the step before it succeeded, so a
        //: send that failed to reach the server hands the previous press's
        //: handoff back - must not open on a picture from earlier.
        seen: {},
        enhanceTouched: false,
        inheritTouched: false,
        promptDirty: false,
        busy: false,
        bundles: {},
        bridgeLoad: null,
        keyHandler: null,
        toastTimer: 0,
        historyOpen: false,
        opened: 0,
        //: What happened last, for a bug report and the checks.
        last: { action: "", code: "", message: "" }
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

    function byId(id) {
        const root = app();
        return (root.getElementById ? root.getElementById(id) : document.getElementById(id)) || null;
    }

    function note(message) {
        const api = window.minipaintWanGP;
        if (api && typeof api.note === "function") {
            try { api.note("intercept: " + message); } catch (e) { /* never worth an exception */ }
        }
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) { node.className = className; }
        if (text !== undefined && text !== null) { node.textContent = String(text); }
        return node;
    }

    function parseHandoff(text) {
        const raw = String(text || "");
        if (raw.indexOf(PREFIX) !== 0) { return null; }
        const parts = raw.slice(PREFIX.length).split(":");
        const token = parts[0] || "";
        if (!HEX32.test(token)) { return null; }
        let width = 0, height = 0;
        if (parts[1] && parts[1].indexOf("x") > 0) {
            const pair = parts[1].split("x");
            width = Math.max(0, parseInt(pair[0], 10) || 0);
            height = Math.max(0, parseInt(pair[1], 10) || 0);
        }
        const tab = /^[a-z0-9_]{0,24}$/.test(parts[2] || "") ? (parts[2] || "") : "";
        return { token: token, width: width, height: height, tab: tab };
    }

    /**
     * Which roles to keep: the ones chosen that are still on offer, and the
     * defaults only when none survive. The same rule the server applies at
     * Generate, so what the popup shows ticked is what it will send.
     */
    function reconcileRoles(chosen, available, defaults) {
        const offered = (available || []).map(function (role) { return typeof role === "string" ? role : role.id; });
        const kept = (chosen || []).filter(function (role) { return offered.indexOf(role) !== -1; });
        if (kept.length) { return offered.filter(function (role) { return kept.indexOf(role) !== -1; }); }
        return (defaults || []).filter(function (role) { return offered.indexOf(role) !== -1; });
    }

    /** This page's identity for the queue: the public API's, or the same key. */
    function pageId() {
        const api = window.minipaintInterop;
        if (api && api.wangp && typeof api.wangp.pageId === "function") {
            try { return api.wangp.pageId(); } catch (e) { /* fall through */ }
        }
        let token = "";
        try { token = String(window.sessionStorage.getItem(PAGE_KEY) || ""); } catch (e) { token = ""; }
        if (!HEX32.test(token)) {
            const bytes = new Uint8Array(16);
            if (window.crypto && typeof window.crypto.getRandomValues === "function") {
                window.crypto.getRandomValues(bytes);
            } else {
                for (let k = 0; k < bytes.length; k++) { bytes[k] = Math.floor(Math.random() * 256); }
            }
            token = "";
            for (const byte of bytes) { token += byte.toString(16).padStart(2, "0"); }
            try { window.sessionStorage.setItem(PAGE_KEY, token); } catch (e) { /* this tab's memory only */ }
        }
        return token;
    }

    /** One bounded POST to the intercept route. Never throws. */
    function post(body) {
        let controller = null;
        try { controller = typeof AbortController === "function" ? new AbortController() : null; } catch (e) { controller = null; }
        const cutoff = controller ? setTimeout(function () { try { controller.abort(); } catch (e) { /* settled */ } }, ASK_TIMEOUT_MS) : 0;
        const options = {
            method: "POST", credentials: "same-origin", cache: "no-store",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body || {})
        };
        if (controller) { options.signal = controller.signal; }
        return fetch(ROUTE, options).then(function (response) {
            return response.json().then(function (answer) {
                return answer && typeof answer === "object" ? answer
                    : { ok: false, code: "INTERNAL_ERROR", message: "The server did not answer (" + response.status + ")." };
            }, function () {
                return { ok: false, code: "INTERNAL_ERROR", message: "The server did not answer (" + response.status + ")." };
            });
        }, function (error) {
            const aborted = !!(error && error.name === "AbortError");
            return { ok: false, code: "INTERNAL_ERROR", unreachable: true,
                     message: aborted ? "Forge did not answer within " + Math.round(ASK_TIMEOUT_MS / 1000) + " seconds."
                                      : "Forge is not answering." };
        }).then(function (answer) {
            if (cutoff) { clearTimeout(cutoff); }
            return answer;
        });
    }

    /* ------------------------------------------------------------------ */
    /* The shared surfaces on the same page                                   */
    /* ------------------------------------------------------------------ */

    function clipboardAttached() {
        const clipboard = window.minipaintClipboard;
        if (!clipboard || typeof clipboard.debug !== "function") { return false; }
        try { return !!clipboard.debug().attached; } catch (e) { return false; }
    }

    function clipboardPromptBox() {
        const host = byId(CLIPBOARD_PROMPT_ID);
        return host ? host.querySelector("textarea, input") : null;
    }

    function clipboardEnhanceBox() {
        const host = byId(CLIPBOARD_ENHANCE_ID);
        return host ? host.querySelector("input[type=checkbox]") : null;
    }

    /**
     * The prompt the popup opens with: the Clipboard tab's own box on this
     * page when the page has one, because that is the prompt this page
     * shows and may hold an edit no blur has saved yet; the server's draft
     * otherwise (a Clipboard tab that could not be built). Both surfaces
     * are one state, and this page's view of it is the one on screen.
     */
    function sharedPrompt(serverPrompt) {
        const box = clipboardPromptBox();
        if (box) { return String(box.value || ""); }
        return String(serverPrompt || "");
    }

    function sharedEnhance(serverFlag) {
        const box = clipboardEnhanceBox();
        if (box) { return !!box.checked; }
        return !!serverFlag;
    }

    /** Which of the host's result tabs is on screen, for a handoff that
     * did not say - the direct route's - so the popup can sit under its
     * button. "" when none is. */
    function visibleTab() {
        for (const name of ["txt2img", "img2img", "extras"]) {
            const panel = byId("tab_" + name);
            if (!panel) { continue; }
            try {
                if (panel.getClientRects && panel.getClientRects().length && getComputedStyle(panel).display !== "none") { return name; }
            } catch (e) { /* the next one */ }
        }
        return "";
    }

    /** Write the Clipboard tab's prompt box on this page, so both say the same. */
    function mirrorPrompt(text) {
        const box = clipboardPromptBox();
        if (!box || String(box.value || "") === String(text)) { return; }
        if (typeof window.minipaintWriteInput === "function") {
            window.minipaintWriteInput(box, text);
            return;
        }
        try {
            box.value = String(text);
            box.dispatchEvent(new Event("input", { bubbles: true }));
        } catch (e) { /* a page without the helper still keeps the server copy */ }
    }

    /** And its enhancer switch, through the native setter, so the framework hears it. */
    function mirrorEnhance(flag) {
        const box = clipboardEnhanceBox();
        if (!box || !!box.checked === !!flag) { return; }
        try {
            const descriptor = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "checked");
            if (descriptor && descriptor.set) { descriptor.set.call(box, !!flag); } else { box.checked = !!flag; }
            box.dispatchEvent(new Event("input", { bubbles: true }));
            box.dispatchEvent(new Event("change", { bubbles: true }));
        } catch (e) { /* the server copy is written either way */ }
    }

    /** The shared setting itself, over the tab's own route. */
    function saveEnhance(flag) {
        return fetch(ENHANCE_SETTINGS_ROUTE, {
            method: "POST", credentials: "same-origin", cache: "no-store",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "toggle", enabled: !!flag })
        }).then(function (response) { return response.ok; }, function () { return false; });
    }

    /* ------------------------------------------------------------------ */
    /* The live page: what WanGP takes right now                             */
    /* ------------------------------------------------------------------ */

    /** Fetch the bridge and the public API once, if the page lacks them. */
    function loadBridge() {
        if (window.minipaintWanGP && window.minipaintInterop) { return Promise.resolve(true); }
        if (S.bridgeLoad) { return S.bridgeLoad; }
        const loader = window.minipaintAssets;
        const urls = [S.bundles.wangp, S.bundles.interop].filter(Boolean);
        if (!loader || typeof loader.load !== "function" || !urls.length) { return Promise.resolve(false); }
        S.bridgeLoad = loader.load(urls).then(function (ok) {
            if (!ok) { S.bridgeLoad = null; }
            return !!ok;
        }, function () { S.bridgeLoad = null; return false; });
        return S.bridgeLoad;
    }

    function withTimeout(promise, ms, fallback) {
        return new Promise(function (resolve) {
            let done = false;
            const timer = setTimeout(function () { if (!done) { done = true; resolve(fallback); } }, ms);
            promise.then(function (value) { if (!done) { done = true; clearTimeout(timer); resolve(value); } },
                         function () { if (!done) { done = true; clearTimeout(timer); resolve(fallback); } });
        });
    }

    /**
     * What the WanGP page in this document takes, as facts for the server:
     * the model block, the per-input support, and whether it is generating.
     * Null when there is no WanGP page here, which is an ordinary state and
     * not a failure - the server then offers every role and judges the
     * request live when it runs.
     */
    function askBridge() {
        const bridge = window.minipaintWanGP;
        if (!bridge || typeof bridge.state !== "function") { return Promise.resolve(null); }
        let state;
        try { state = bridge.state(); } catch (e) { return Promise.resolve(null); }
        if (!state || !state.present) { return Promise.resolve(null); }
        const api = window.minipaintInterop;
        const ask = api && api.wangp && typeof api.wangp.capabilities === "function"
            ? api.wangp.capabilities()
            : (typeof bridge.capabilities === "function" ? bridge.capabilities() : Promise.resolve(null));
        return withTimeout(Promise.resolve(ask), BRIDGE_TIMEOUT_MS, null).then(function (answer) {
            if (!answer || !answer.ok) {
                const model = state.model && typeof state.model === "object" ? state.model : null;
                return model ? { model: model, inputs: null, generating: null } : null;
            }
            return {
                model: answer.model && typeof answer.model === "object" ? answer.model : (state.model || null),
                inputs: answer.inputs && typeof answer.inputs === "object" ? answer.inputs : null,
                generating: typeof answer.generation_running === "boolean" ? answer.generation_running : null
            };
        });
    }

    /* ------------------------------------------------------------------ */
    /* The popup                                                             */
    /* ------------------------------------------------------------------ */

    function build() {
        if (S.dom && S.dom.root && S.dom.root.isConnected) { return S.dom; }
        const root = el("div", CLASS);
        root.setAttribute("role", "dialog");
        root.setAttribute("aria-label", "Send to WanGP");
        root.hidden = true;

        const head = el("div", CLASS + "-head");
        const thumb = el("img", CLASS + "-thumb");
        thumb.alt = "";
        thumb.draggable = false;
        const titles = el("div", CLASS + "-titles");
        const title = el("b", CLASS + "-title", "Send to WanGP");
        const sub = el("small", CLASS + "-sub", "");
        titles.appendChild(title);
        titles.appendChild(sub);
        const close = el("button", CLASS + "-close", "×");
        close.type = "button";
        close.title = "Cancel (Escape)";
        close.setAttribute("aria-label", "Cancel");
        close.addEventListener("click", function () { cancel(); });
        head.appendChild(thumb);
        head.appendChild(titles);
        head.appendChild(close);
        root.appendChild(head);

        const prompt = el("textarea", CLASS + "-prompt");
        prompt.rows = 3;
        prompt.placeholder = "Use current WanGP prompt";
        prompt.setAttribute("aria-label", "Prompt");
        prompt.addEventListener("input", function () { S.promptDirty = true; mirrorPrompt(prompt.value); });
        root.appendChild(prompt);

        const enhanceRow = el("label", CLASS + "-row " + CLASS + "-check");
        const enhance = el("input");
        enhance.type = "checkbox";
        enhance.className = CLASS + "-enhance";
        enhance.addEventListener("change", function () {
            S.enhanceTouched = true;
            mirrorEnhance(enhance.checked);
            saveEnhance(enhance.checked);
            renderEnhanceNote();
        });
        enhanceRow.appendChild(enhance);
        enhanceRow.appendChild(el("span", "", "Enhance"));
        const enhanceNote = el("span", CLASS + "-note", "");
        enhanceRow.appendChild(enhanceNote);
        root.appendChild(enhanceRow);

        const rolesRow = el("div", CLASS + "-row " + CLASS + "-roles-row");
        rolesRow.appendChild(el("span", CLASS + "-label", "Use image as"));
        const roles = el("div", CLASS + "-roles");
        roles.setAttribute("role", "group");
        roles.setAttribute("aria-label", "Use the image as");
        rolesRow.appendChild(roles);
        root.appendChild(rolesRow);

        const inheritRow = el("label", CLASS + "-row " + CLASS + "-check");
        const inherit = el("input");
        inherit.type = "checkbox";
        inherit.className = CLASS + "-inherit";
        inherit.addEventListener("change", function () { S.inheritTouched = true; renderInheritNote(); });
        inheritRow.appendChild(inherit);
        inheritRow.appendChild(el("span", "", "Inherit Clipboard inputs"));
        const inheritNote = el("span", CLASS + "-note", "");
        inheritRow.appendChild(inheritNote);
        root.appendChild(inheritRow);

        const status = el("div", CLASS + "-status");
        const dot = el("span", CLASS + "-dot");
        dot.dataset.state = "unknown";
        const statusText = el("span", CLASS + "-status-text", "WanGP: checking…");
        const historyToggle = el("button", CLASS + "-history-toggle", "History");
        historyToggle.type = "button";
        historyToggle.addEventListener("click", function () { S.historyOpen = !S.historyOpen; renderHistory(); });
        status.appendChild(dot);
        status.appendChild(statusText);
        status.appendChild(historyToggle);
        root.appendChild(status);

        const message = el("div", CLASS + "-message");
        message.setAttribute("role", "status");
        message.hidden = true;
        root.appendChild(message);

        const history = el("div", CLASS + "-history");
        history.hidden = true;
        history.addEventListener("click", onHistoryClick);
        root.appendChild(history);

        const actions = el("div", CLASS + "-actions");
        const cancelButton = el("button", CLASS + "-cancel", "Cancel");
        cancelButton.type = "button";
        cancelButton.addEventListener("click", function () { cancel(); });
        const generate = el("button", CLASS + "-generate", "Generate");
        generate.type = "button";
        generate.title = "Ctrl+Enter";
        generate.addEventListener("click", function () { submit(); });
        actions.appendChild(cancelButton);
        actions.appendChild(generate);
        root.appendChild(actions);

        (document.body || document.documentElement).appendChild(root);
        S.dom = { root: root, thumb: thumb, sub: sub, prompt: prompt, enhance: enhance, enhanceNote: enhanceNote,
                  roles: roles, inherit: inherit, inheritNote: inheritNote, dot: dot, statusText: statusText,
                  historyToggle: historyToggle, message: message, history: history, cancel: cancelButton, generate: generate };
        return S.dom;
    }

    /** Under the button that was pressed, clamped to the window; else top right. */
    function position() {
        const dom = S.dom;
        if (!dom) { return; }
        const root = dom.root;
        root.style.left = "";
        root.style.right = "";
        root.style.top = "";
        const width = Math.min(400, Math.max(280, window.innerWidth - 24));
        root.style.width = width + "px";
        let top = 64, left = -1;
        const button = S.tab ? byId(S.tab + "_send_to_minipaint") : null;
        if (button && button.getBoundingClientRect) {
            const box = button.getBoundingClientRect();
            if (box.width || box.height) {
                top = box.bottom + 8;
                left = box.left;
            }
        }
        const height = Math.min(root.offsetHeight || 360, window.innerHeight - 24);
        if (top + height > window.innerHeight - 12) { top = Math.max(12, window.innerHeight - height - 12); }
        if (left < 0) { left = window.innerWidth - width - 16; }
        left = Math.max(12, Math.min(left, window.innerWidth - width - 12));
        root.style.top = Math.round(top) + "px";
        root.style.left = Math.round(left) + "px";
    }

    function selectedRoles() {
        const dom = S.dom;
        if (!dom) { return []; }
        return Array.from(dom.roles.querySelectorAll("input[type=checkbox]"))
            .filter(function (box) { return box.checked; })
            .map(function (box) { return box.dataset.role; });
    }

    function renderRoles(selected) {
        const dom = S.dom;
        if (!dom) { return; }
        dom.roles.textContent = "";
        const available = S.roles.available;
        if (!available.length) {
            dom.roles.appendChild(el("span", CLASS + "-note", S.describe && S.describe.capabilities && S.describe.capabilities.known
                ? "the current model takes no image" : "no image role is on offer"));
            return;
        }
        for (const role of available) {
            const label = el("label", CLASS + "-role");
            const box = el("input");
            box.type = "checkbox";
            box.dataset.role = role.id;
            box.checked = selected.indexOf(role.id) !== -1;
            box.addEventListener("change", function () {
                S.userRoles = selectedRoles();
                renderInheritNote();
                showMessage("");
            });
            label.appendChild(box);
            label.appendChild(el("span", "", role.label));
            dom.roles.appendChild(label);
        }
    }

    function renderEnhanceNote() {
        const dom = S.dom;
        if (!dom) { return; }
        const info = (S.describe && S.describe.enhance) || {};
        const state = String(info.state || "unknown");
        dom.enhanceNote.dataset.state = state;
        dom.enhanceNote.title = String(info.text || "");
        if (!dom.enhance.checked) { dom.enhanceNote.textContent = ""; return; }
        dom.enhanceNote.textContent = state === "ready" ? "ready" : state === "unknown" ? "model not known yet" : "not available now";
    }

    function renderInheritNote() {
        const dom = S.dom;
        if (!dom) { return; }
        const info = (S.describe && S.describe.inherit) || {};
        const held = Array.isArray(info.draft) ? info.draft : [];
        if (!dom.inherit.checked || !held.length) {
            dom.inheritNote.textContent = held.length || !dom.inherit.checked ? "" : "Clipboard holds no picture";
            return;
        }
        const chosen = selectedRoles();
        const kept = held.filter(function (slot) { return chosen.indexOf(slot.role) === -1; });
        dom.inheritNote.textContent = kept.length
            ? "keeps " + kept.map(function (slot) { return slot.label + ": " + slot.names.join(", "); }).join(" · ")
            : "every Clipboard picture is replaced by this one";
    }

    function renderStatus() {
        const dom = S.dom;
        if (!dom) { return; }
        const wangp = (S.describe && S.describe.wangp) || {};
        let state = String(wangp.state || "unknown");
        let text = String(wangp.text || "WanGP: not known");
        // The live page knows more than the server's cached hello: its own
        // flag says whether a generation is running right now.
        if (S.facts && typeof S.facts.generating === "boolean" && (state === "running" || state === "idle" || state === "busy")) {
            state = S.facts.generating ? "busy" : "idle";
            text = S.facts.generating ? "WanGP is generating; a new request joins its queue" : "WanGP is idle";
        }
        dom.dot.dataset.state = state;
        dom.dot.title = text;
        dom.statusText.textContent = text;
        const generate = (S.describe && S.describe.generate) || { enabled: true };
        dom.generate.disabled = !!S.busy || generate.enabled === false;
        dom.generate.title = generate.enabled === false ? String(generate.reason || "WanGP is not running") : "Ctrl+Enter";
    }

    function showMessage(text, failure) {
        const dom = S.dom;
        if (!dom) { return; }
        if (!text) { dom.message.hidden = true; dom.message.textContent = ""; return; }
        dom.message.textContent = String(text);
        dom.message.classList.toggle(CLASS + "-message-failure", !!failure);
        dom.message.hidden = false;
    }

    function historyEntry(entry) {
        const row = el("div", CLASS + "-entry" + (entry.pinned ? " " + CLASS + "-entry-pinned" : ""));
        row.dataset.history = entry.id;
        const head = el("div", CLASS + "-entry-head");
        const pin = el("button", CLASS + "-entry-pin", entry.pinned ? "★" : "☆");
        pin.type = "button";
        pin.title = entry.pinned ? "Unpin" : "Pin to the top";
        pin.dataset.historyAction = "pin";
        head.appendChild(pin);
        head.appendChild(el("span", CLASS + "-entry-when", entry.when || ""));
        if (entry.outcome) { head.appendChild(el("span", CLASS + "-entry-outcome", entry.outcome)); }
        row.appendChild(head);
        const prompt = el("div", CLASS + "-entry-prompt", entry.prompt ? entry.prompt.slice(0, 90) + (entry.prompt.length > 90 ? "…" : "") : "Use current WanGP prompt");
        prompt.title = entry.prompt || "";
        row.appendChild(prompt);
        const details = [];
        details.push((entry.roles || []).map(function (role) { return role.label + (role.valid === false ? " (not offered now)" : ""); }).join(" + ") || "no role");
        if (entry.enhance) { details.push("enhanced"); }
        details.push(entry.inherit ? "inherits Clipboard" : "image only");
        if (entry.model) { details.push(entry.model); }
        row.appendChild(el("div", CLASS + "-entry-details", details.join(" · ")));
        const actions = el("div", CLASS + "-entry-actions");
        for (const pair of [["load", "Load"], ["delete", "Delete"]]) {
            const button = el("button", "", pair[1]);
            button.type = "button";
            button.dataset.historyAction = pair[0];
            actions.appendChild(button);
        }
        row.appendChild(actions);
        return row;
    }

    function renderHistory() {
        const dom = S.dom;
        if (!dom) { return; }
        const list = (S.describe && Array.isArray(S.describe.history)) ? S.describe.history : [];
        dom.historyToggle.textContent = "History" + (list.length ? " (" + list.length + ")" : "");
        dom.historyToggle.setAttribute("aria-expanded", S.historyOpen ? "true" : "false");
        dom.history.hidden = !S.historyOpen;
        if (!S.historyOpen) { return; }
        dom.history.textContent = "";
        if (!list.length) {
            dom.history.appendChild(el("p", CLASS + "-note", "Nothing sent from the gallery yet."));
            return;
        }
        for (const entry of list) { dom.history.appendChild(historyEntry(entry)); }
    }

    function onHistoryClick(event) {
        const target = event.target;
        const button = target && target.closest ? target.closest("[data-history-action]") : null;
        if (!button) { return; }
        const row = button.closest("[data-history]");
        if (!row) { return; }
        event.preventDefault();
        const id = row.dataset.history;
        const verb = button.dataset.historyAction;
        if (verb === "load") { loadHistory(id); }
        else if (verb === "delete") { deleteHistory(id); }
        else if (verb === "pin") { pinHistory(id, !row.classList.contains(CLASS + "-entry-pinned")); }
    }

    function factsBody() {
        const body = {};
        if (S.facts && S.facts.model) { body.model = S.facts.model; }
        if (S.facts && S.facts.inputs) { body.inputs = S.facts.inputs; }
        return body;
    }

    function loadHistory(id) {
        post(Object.assign({ action: "history_load", id: id }, factsBody())).then(function (answer) {
            if (!answer || !answer.ok) { showMessage((answer && answer.message) || "That entry could not be loaded.", true); return; }
            const dom = S.dom;
            if (!dom || !S.open) { return; }
            dom.prompt.value = String(answer.prompt || "");
            S.promptDirty = true;
            mirrorPrompt(dom.prompt.value);
            dom.enhance.checked = !!answer.enhance;
            mirrorEnhance(dom.enhance.checked);
            dom.inherit.checked = answer.inherit !== false;
            S.inheritTouched = true;
            if (answer.capabilities) {
                S.roles.available = answer.capabilities.roles || [];
                S.roles.defaults = answer.capabilities.default_roles || [];
            }
            S.userRoles = Array.isArray(answer.roles) ? answer.roles.slice() : [];
            renderRoles(reconcileRoles(S.userRoles, S.roles.available, S.roles.defaults));
            renderEnhanceNote();
            renderInheritNote();
            const dropped = Array.isArray(answer.dropped_roles) ? answer.dropped_roles : [];
            showMessage(dropped.length
                ? "Loaded. " + dropped.map(function (role) { return role.label; }).join(", ") + " is not offered by the current model"
                  + (answer.defaulted ? ", so the default role is ticked instead." : ", so it was left out.")
                : "Loaded. Nothing was queued.");
            note("history entry loaded" + (dropped.length ? " with " + dropped.length + " role(s) dropped" : ""));
        });
    }

    function deleteHistory(id) {
        post(Object.assign({ action: "history_delete", id: id }, factsBody())).then(function (answer) {
            if (!answer || !answer.ok) { showMessage((answer && answer.message) || "That entry could not be deleted.", true); return; }
            if (S.describe) { S.describe.history = answer.history || []; }
            renderHistory();
        });
    }

    function pinHistory(id, pinned) {
        post(Object.assign({ action: "history_pin", id: id, pinned: !!pinned }, factsBody())).then(function (answer) {
            if (!answer || !answer.ok) { showMessage((answer && answer.message) || "That entry could not be pinned.", true); return; }
            if (S.describe) { S.describe.history = answer.history || []; }
            renderHistory();
        });
    }

    /** The server's answer, onto the form - keeping what the user already did. */
    function applyDescribe(answer) {
        const dom = S.dom;
        if (!dom) { return; }
        S.describe = answer;
        if (!S.promptDirty) { dom.prompt.value = sharedPrompt(answer.prompt); }
        if (!S.enhanceTouched) { dom.enhance.checked = sharedEnhance(answer.enhance && answer.enhance.enabled); }
        const caps = answer.capabilities || {};
        S.roles.available = Array.isArray(caps.roles) ? caps.roles : [];
        S.roles.defaults = Array.isArray(caps.default_roles) ? caps.default_roles : [];
        const chosen = S.userRoles || selectedRoles();
        renderRoles(reconcileRoles(chosen, S.roles.available, S.roles.defaults));
        if (!S.inheritTouched) { dom.inherit.checked = !(answer.inherit && answer.inherit.default === false); }
        renderEnhanceNote();
        renderInheritNote();
        renderStatus();
        renderHistory();
        dom.sub.textContent = (S.tab ? S.tab + " · " : "") + (S.size || "") + (caps.known === false ? " · WanGP page not open here" : "");
        position();
    }

    function describe() {
        const token = S.token;
        return post(Object.assign({ action: "describe", handoff: S.handoff }, factsBody())).then(function (answer) {
            if (!S.open || S.token !== token) { return answer; }
            if (!answer || !answer.ok) {
                S.last = { action: "describe", code: (answer && answer.code) || "", message: (answer && answer.message) || "" };
                showMessage((answer && answer.message) || "The request could not be prepared.", true);
                if (S.dom) { S.dom.generate.disabled = true; }
                return answer;
            }
            applyDescribe(answer);
            return answer;
        });
    }

    function installKeys() {
        if (S.keyHandler) { return; }
        S.keyHandler = function (event) {
            if (!S.open) { return; }
            if (event.key === "Escape") { event.stopPropagation(); event.preventDefault(); cancel(); return; }
            if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
                const inside = S.dom && S.dom.root.contains(event.target);
                if (inside) { event.stopPropagation(); event.preventDefault(); submit(); }
            }
        };
        document.addEventListener("keydown", S.keyHandler, true);
    }

    function removeKeys() {
        if (!S.keyHandler) { return; }
        document.removeEventListener("keydown", S.keyHandler, true);
        S.keyHandler = null;
    }

    /**
     * Open the popup on a frozen picture.
     *
     * A popup already open on another picture gives that one up - its token
     * is cancelled on the server - and keeps the prompt and the roles as
     * they stand: pressing 🖌️ under the next result is how a run of sends
     * goes, and re-typing the prompt for each would be the opposite of the
     * point.
     */
    function open(handoff, options) {
        const parsed = parseHandoff(handoff);
        if (!parsed) { note("open: not a handoff"); return false; }
        if (S.seen[parsed.token]) {
            // The step that hands over a handoff runs after a failed receive
            // too, carrying the value the box held from the press before.
            // A picture is frozen for one press; an old one is not this one.
            note("open: ignored a handoff already opened once (" + parsed.token.slice(0, 8) + ")");
            return false;
        }
        S.seen[parsed.token] = true;
        if (options && options.bundles && typeof options.bundles === "object") {
            S.bundles = Object.assign({}, S.bundles, options.bundles);
        }
        const replacing = S.open && !!S.handoff;
        if (replacing && S.token !== parsed.token) {
            post({ action: "cancel", handoff: S.handoff });
            note("replaced the frozen picture with a newer one");
        }
        const dom = build();
        S.open = true;
        S.opened += 1;
        S.handoff = String(handoff);
        S.token = parsed.token;
        S.tab = parsed.tab;
        S.size = parsed.width && parsed.height ? parsed.width + "×" + parsed.height : "";
        S.busy = false;
        if (!replacing) {
            // A fresh popup starts from the shared state; one that is only
            // being handed a newer picture keeps what was typed and ticked.
            S.promptDirty = false;
            S.enhanceTouched = false;
            S.inheritTouched = false;
        }
        S.facts = null;
        dom.thumb.src = IMAGE_ROUTE + encodeURIComponent(parsed.token);
        dom.sub.textContent = (parsed.tab ? parsed.tab + " · " : "") + S.size;
        dom.root.hidden = false;
        dom.generate.disabled = false;
        showMessage("");
        renderStatus();
        position();
        installKeys();
        setTimeout(function () { try { dom.prompt.focus(); } catch (e) { /* not worth an exception */ } }, 30);
        // First with what the page knows now, so the popup is drawn at once;
        // then again with the live page's facts when it has any, which is
        // the only way the roles on offer can follow the model.
        const token = S.token;
        describe();
        loadBridge().then(function () { return askBridge(); }).then(function (facts) {
            if (!S.open || S.token !== token || !facts) { return; }
            S.facts = facts;
            describe();
        });
        if (!replacing) { announce(true); }
        note("opened for " + (parsed.tab || "a gallery") + " picture " + parsed.token.slice(0, 8));
        return true;
    }

    /** Say that an overlay of ours has taken over, or given way.
     *
     * Never fails a caller: an overlay that could not be announced is still an
     * overlay, and the popup is no less usable for a page whose browser has no
     * `CustomEvent` constructor.
     */
    function announce(open) {
        try {
            document.dispatchEvent(new CustomEvent(OVERLAY_EVENT, {
                detail: { name: OVERLAY_NAME, open: !!open, modal: true }
            }));
        } catch (error) { /* nothing here depends on being heard */ }
    }

    function close() {
        const was = S.open;
        S.open = false;
        removeKeys();
        if (S.dom) { S.dom.root.hidden = true; }
        if (was) { announce(false); }
    }

    /** Cancel: the frozen picture is let go; an edited prompt stays shared. */
    function cancel() {
        if (!S.open) { return false; }
        const handoff = S.handoff;
        const dom = S.dom;
        const promptText = dom ? dom.prompt.value : "";
        const dirty = S.promptDirty;
        close();
        S.last = { action: "cancel", code: "", message: "" };
        post({ action: "cancel", handoff: handoff }).then(function () {
            if (dirty) { post({ action: "draft", prompt: promptText }); }
        });
        note("cancelled; nothing was queued");
        return true;
    }

    /** What Generate sends: the visible values, the frozen token, the facts. */
    function planSubmission() {
        const dom = S.dom;
        return Object.assign({
            action: "submit",
            handoff: S.handoff,
            prompt: dom ? String(dom.prompt.value || "") : "",
            enhance: dom ? !!dom.enhance.checked : false,
            inherit: dom ? !!dom.inherit.checked : true,
            roles: selectedRoles(),
            page: pageId()
        }, factsBody());
    }

    /** After the server stored the job: whoever runs it, told. */
    function afterSubmit(instruction) {
        if (!instruction) { return; }
        const clipboard = window.minipaintClipboard;
        if (clipboard && typeof clipboard.queue === "function") {
            // The Clipboard tab is on this page and already knows how to
            // watch a server job or pump a browser one - and its queue list
            // is what shows the job, so it re-reads too.
            try { clipboard.queue(JSON.stringify(instruction), "the gallery"); } catch (e) { /* below */ }
            if (typeof clipboard.refreshQueue === "function") { try { clipboard.refreshQueue(); } catch (e) { /* fine */ } }
            return;
        }
        if (instruction.executor === "browser") {
            loadBridge().then(function () {
                const api = window.minipaintInterop;
                if (api && api.wangp && typeof api.wangp.pump === "function") {
                    try { api.wangp.pump(); } catch (e) { note("pump failed to start"); }
                } else {
                    note("no queue API on this page; the job waits for a page that can run it");
                }
            });
            return;
        }
        const api = window.minipaintInterop;
        if (api && api.wangp && typeof api.wangp.watch === "function") {
            try { api.wangp.watch(); } catch (e) { /* the job runs regardless */ }
        }
    }

    function submit() {
        if (!S.open || S.busy) { return Promise.resolve(null); }
        const dom = S.dom;
        const body = planSubmission();
        if (!body.roles.length && S.roles.available.length) {
            showMessage("Tick at least one role for the picture.", true);
            return Promise.resolve(null);
        }
        S.busy = true;
        renderStatus();
        dom.generate.textContent = "Sending…";
        showMessage("");
        const token = S.token;
        return post(body).then(function (answer) {
            if (!S.open || S.token !== token) { return answer; }
            S.busy = false;
            dom.generate.textContent = "Generate";
            if (!answer || !answer.ok) {
                S.last = { action: "submit", code: (answer && answer.code) || "", message: (answer && answer.message) || "" };
                const notes = answer && Array.isArray(answer.notes) && answer.notes.length ? " " + answer.notes[0] + "." : "";
                showMessage(((answer && answer.message) || "The request was not queued.") + notes, true);
                renderStatus();
                note("generate refused - " + ((answer && answer.code) || "no answer"));
                return answer;
            }
            S.last = { action: "submit", code: "", message: String(answer.status || "") };
            S.userRoles = body.roles.slice();
            mirrorPrompt(body.prompt);
            mirrorEnhance(body.enhance);
            close();
            const first = Array.isArray(answer.notes) && answer.notes.length ? " " + answer.notes[0] + "." : "";
            toast(String(answer.status || "Queued for WanGP.") + first, false);
            afterSubmit(answer.instruction);
            note("generate: job " + String(answer.job_id || "").slice(0, 8) + " stored as " + (body.roles.join(", ") || "no role"));
            return answer;
        });
    }

    /**
     * A brief notice where the popup was, after it has closed. Its own,
     * because the Clipboard tab's toast lives inside that tab and the user
     * is in txt2img.
     */
    function toast(text, failure) {
        let element = document.querySelector("." + CLASS + "-toast");
        if (!element) {
            element = el("div", CLASS + "-toast");
            element.setAttribute("role", "status");
            (document.body || document.documentElement).appendChild(element);
        }
        if (S.toastTimer) { clearTimeout(S.toastTimer); S.toastTimer = 0; }
        if (!text) { element.hidden = true; return; }
        element.textContent = String(text);
        element.classList.toggle(CLASS + "-toast-failure", !!failure);
        if (S.dom && S.dom.root) {
            element.style.top = S.dom.root.style.top;
            element.style.left = S.dom.root.style.left;
        }
        element.hidden = false;
        S.toastTimer = setTimeout(function () { element.hidden = true; S.toastTimer = 0; }, TOAST_MS);
    }

    /**
     * The fallback when the receive chain never came back: freeze the
     * picture from here, over the public API's staging route, and open on
     * it. The Clipboard bundle calls this when Gradio's queue is dead and
     * the button is pointed at WanGP, with the URL the host serves the
     * gallery picture from.
     *
     * One send at a time: a press while one is on its way says so and waits
     * for that one, rather than stacking another request on a connection
     * that may be the thing that is stuck. Every request has a deadline;
     * one that misses it is cancelled and the send tried once more. A send
     * cancelled because the page is being reloaded or closed is not a
     * failure and says nothing on screen.
     *
     * The journal gets timings, the connection's protocol, status codes and
     * error kinds - never the picture's address, which carries its file
     * name, and never a prompt.
     */
    function stageAndOpen(url, tab, options) {
        const source = String(url || "");
        if (!source) { return Promise.resolve(false); }
        // A press is proof the user is still here, whatever a cancelled
        // reload said earlier.
        S.leaving = false;
        if (S.staging) {
            note("stage: pressed again while the last picture is still on its way - waiting for that one");
            toast("Still sending the last picture to WanGP…", false);
            return S.staging;
        }
        watchLeaving();
        note("stage: sending a picture - " + transportFacts());
        toast("Sending the picture to WanGP…", false);
        const started = now();
        const run = stageAttempt(source, tab, options, 1, started);
        S.staging = run;
        const clear = function () { if (S.staging === run) { S.staging = null; } };
        run.then(clear, clear);
        return run;
    }

    function stageAttempt(source, tab, options, attempt, started) {
        return stageOnce(source).then(function (staged) {
            const answer = staged.answer;
            const handoff = PREFIX + answer.image.id + ":" + (answer.width || 0) + "x" + (answer.height || 0) + ":" + String(tab || visibleTab() || "");
            note("staged the picture over HTTP without the queue (reading it took " + staged.readMs + " ms, staging "
                 + staged.stageMs + " ms, attempt " + attempt + ", " + (now() - started) + " ms in all)");
            toast("", false);
            return open(handoff, options);
        }, function (error) {
            const why = describeFailure(error);
            if (S.leaving) {
                note("stage: cancelled because the page is being reloaded or closed (" + why + ") - not a failure");
                toast("", false);
                return false;
            }
            const retry = attempt < STAGE_ATTEMPTS && !(error && error.answered);
            note("could not stage the picture over HTTP: " + why + " - attempt " + attempt + " of " + STAGE_ATTEMPTS
                 + ", " + (now() - started) + " ms since the press; " + transportFacts()
                 + (retry ? "; the stalled request was cancelled, trying once more" : ""));
            if (retry) {
                toast("WanGP did not answer yet - trying once more…", false);
                return wait(timing().retryDelay).then(function () {
                    if (S.leaving) { return false; }
                    return stageAttempt(source, tab, options, attempt + 1, started);
                });
            }
            toast(error && error.answered
                ? "That picture could not be sent to WanGP: " + why + "."
                : "That picture could not be sent to WanGP: Forge did not answer this page. "
                  + "Press the button again in a moment; if it keeps happening, reload the page.", true);
            return false;
        });
    }

    /** The picture read from the host, then staged. Rejects with a described error. */
    function stageOnce(source) {
        const readStart = now();
        let readMs = 0;
        return boundedFetch(source, { credentials: "same-origin", cache: "no-store" }, "reading the picture").then(function (response) {
            if (!response.ok) { throw answered("the host would not serve the picture (" + response.status + ")"); }
            return response.blob();
        }).then(function (blob) {
            readMs = now() - readStart;
            return boundedFetch(STAGE_ROUTE, {
                method: "POST", credentials: "same-origin", cache: "no-store",
                headers: { "Content-Type": blob.type || "application/octet-stream" }, body: blob
            }, "staging it");
        }).then(function (response) {
            return response.json().then(null, function () {
                throw answered("Forge answered " + response.status + " without a readable body");
            });
        }).then(function (answer) {
            if (!answer || !answer.ok || !answer.image || !HEX32.test(String(answer.image.id || ""))) {
                throw answered((answer && answer.message) || "the picture could not be staged");
            }
            return { answer: answer, readMs: readMs, stageMs: now() - readStart - readMs };
        });
    }

    /**
     * fetch with a deadline. A request that misses it is aborted - on HTTP/2
     * that cancels its stream rather than leaving it on the page's one
     * connection - and rejects saying which step it was and how long it had.
     */
    function boundedFetch(url, init, step) {
        const limit = timing().timeout;
        let controller = null;
        try { controller = typeof AbortController === "function" ? new AbortController() : null; } catch (e) { controller = null; }
        let timedOut = false;
        const cutoff = controller ? setTimeout(function () {
            timedOut = true;
            try { controller.abort(); } catch (e) { /* settled */ }
        }, limit) : 0;
        const options = Object.assign({}, init);
        if (controller) { options.signal = controller.signal; }
        const settle = function () { if (cutoff) { clearTimeout(cutoff); } };
        return fetch(url, options).then(function (response) { settle(); return response; }, function (error) {
            settle();
            const failure = new Error(timedOut ? "no answer within " + Math.round(limit / 1000) + " s"
                                               : String((error && error.message) || error || "the request failed"));
            failure.kind = timedOut ? "timeout" : ((error && error.name) || "network");
            failure.step = step;
            throw failure;
        });
    }

    function answered(message) {
        const error = new Error(message);
        error.answered = true;   // the server did reply; trying again would get the same reply
        return error;
    }

    /** What went wrong, fit for the journal: the step, the kind, the words - no address. */
    function describeFailure(error) {
        if (!error) { return "unknown failure"; }
        if (error.answered) { return String(error.message || "refused"); }
        const step = error.step ? error.step + ": " : "";
        return step + (error.kind === "timeout" ? String(error.message) : String(error.kind || "error") + " - " + String(error.message || ""));
    }

    /**
     * How this page reaches Forge, in the terms a failure is diagnosed from:
     * the protocol the page itself arrived over (h2 when the auto-TLS
     * extension's HTTP/2 is doing its job), whether the browser thinks it is
     * online, and whether the tab is on screen. Nothing that names a person.
     */
    function transportFacts() {
        let protocol = "unknown";
        try {
            const entries = typeof performance !== "undefined" && performance.getEntriesByType
                ? performance.getEntriesByType("navigation") : [];
            if (entries && entries[0] && entries[0].nextHopProtocol) { protocol = String(entries[0].nextHopProtocol); }
        } catch (e) { /* unknown */ }
        let online = "unknown";
        try { if (typeof navigator !== "undefined" && typeof navigator.onLine === "boolean") { online = navigator.onLine ? "online" : "offline"; } } catch (e) { /* unknown */ }
        const visible = document && document.visibilityState ? String(document.visibilityState) : "unknown";
        return "page over " + protocol + ", browser " + online + ", tab " + visible;
    }

    //: The page is being reloaded or closed: requests are about to be
    //: cancelled by the browser, and that is not a failure worth a toast.
    function watchLeaving() {
        if (S.leavingWatched || typeof window.addEventListener !== "function") { return; }
        S.leavingWatched = true;
        const leaving = function () { S.leaving = true; };
        window.addEventListener("beforeunload", leaving);
        window.addEventListener("pagehide", leaving);
        window.addEventListener("pageshow", function () { S.leaving = false; });
    }

    function timing() {
        // The checks shorten the waits; nothing on a real page sets this.
        const override = window.minipaintInterceptTiming || {};
        return {
            timeout: Number(override.timeout) > 0 ? Number(override.timeout) : STAGE_TIMEOUT_MS,
            retryDelay: Number(override.retryDelay) >= 0 && override.retryDelay !== undefined ? Number(override.retryDelay) : STAGE_RETRY_DELAY_MS
        };
    }

    function wait(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }
    function now() { return Date.now(); }

    function state() {
        return {
            open: S.open, token: S.token, tab: S.tab, busy: S.busy, opened: S.opened,
            roles: { available: S.roles.available.map(function (role) { return role.id; }), defaults: S.roles.defaults.slice(),
                     selected: selectedRoles() },
            prompt: S.dom ? S.dom.prompt.value : "",
            enhance: S.dom ? !!S.dom.enhance.checked : false,
            inherit: S.dom ? !!S.dom.inherit.checked : true,
            facts: S.facts ? { hasModel: !!S.facts.model, hasInputs: !!S.facts.inputs, generating: S.facts.generating } : null,
            seen: Object.keys(S.seen).length,
            wangp: S.dom ? S.dom.dot.dataset.state : "",
            generateEnabled: S.dom ? !S.dom.generate.disabled : false,
            historyOpen: S.historyOpen,
            historyCount: S.describe && Array.isArray(S.describe.history) ? S.describe.history.length : 0,
            last: Object.assign({}, S.last)
        };
    }

    return {
        open: open,
        close: close,
        cancel: cancel,
        generate: submit,
        stageAndOpen: stageAndOpen,
        state: state,
        debug: state,
        // Pure, for the checks: the rules the popup applies, with no page.
        parseHandoff: parseHandoff,
        reconcileRoles: reconcileRoles,
        planSubmission: planSubmission
    };
})();
