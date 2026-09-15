/**
 * Mini Paint <-> WebUI bridge, parent-frame side.
 *
 * Loaded by the WebUI itself (AUTOMATIC1111 / Forge / Forge Neo). The Mini
 * Paint iframe calls a1111minipaint.onload() once its own bundle is ready.
 *
 * Note: we deliberately do NOT register through onUiLoaded(). That callback
 * list is fired exactly once, as soon as #txt2img_prompt appears - long before
 * this extension's iframe finishes loading - and registering afterwards never
 * runs. We wait for the concrete elements instead.
 */
window.a1111minipaint = window.a1111minipaint || {};

/**
 * The loader for this extension's other bundles.
 *
 * Forge loads every file in an extension's javascript/ folder into every
 * page, always. The Canvas adapter, the WanGP bridge, the public queue API
 * and the Clipboard tab are none of them wanted by a session that opens
 * txt2img and nothing else, and all four used to be parsed on the main
 * thread during hydration, which is the busiest moment the page has. So they
 * live outside that folder now and each tab asks for its own when it loads.
 *
 * Idempotent by URL, and that is the load-bearing part: Reload UI rebuilds
 * the page without reloading the document, so a loader that appended a
 * script per rebuild would install a second copy of every listener its
 * bundle registers - two menu handlers, two pumps, two of everything.
 *
 * A bundle that will not load costs exactly the tab it belongs to. The
 * promise is kept either way, so a caller that awaits one is not left
 * hanging by a network that refused.
 *
 * TWO RULES THIS FILE LEARNED THE HARD WAY.
 *
 * `load()` resolves to a BOOLEAN, not to the array Promise.all hands back.
 * An array is truthy whatever is in it, so `if (!ok)` on `[false]` is a
 * guard that never fires; every caller here reads the result as a truth
 * value and one of them decides whether the Canvas attaches.
 *
 * A <script> element in the document is NOT proof that its bundle ran. A
 * failed load leaves its element behind, and treating that corpse as
 * success turns a retry into a false positive - the caller is told the
 * bundle is there and goes looking for globals that were never defined.
 * The element carries its own state, and only "loaded" counts.
 */
/**
 * Write a value into one of the host's inputs, so the host actually hears it.
 *
 * WHY THIS IS NOT `element.value = text`.
 *
 * Gradio's inputs are owned by its framework, and a framework keeps its own
 * record of what an input holds. Assigning to `.value` writes the DOM and
 * leaves that record untouched, so the framework can compare the two, see no
 * change, and send nothing - the write succeeds and the event never happens.
 * Nothing says so, because from the page's side the write had worked.
 *
 * The prototype's own setter is the way in that every framework leaves open:
 * it goes through the accessor the framework wrapped, so the framework hears
 * it and updates its record. Then "input" and "change" - a build listens for
 * one or the other, and dispatching both costs nothing.
 *
 * The editor's transfer library has had this since long before the Canvas
 * (set_native_value, used for the same reason on the host's canvases); this
 * is that lesson, applied to this extension's own boxes at last.
 *
 * WHAT THIS DID NOT FIX, so nobody spends another build on it. It was put in
 * to explain an install where no written box ever reached the server, and it
 * did not: the logs from that install after this landed say exactly what
 * they said before. It is still the right way to write an input - a plain
 * assignment is a real hazard on some builds - but the send no longer rests
 * on it. See ``sendTo`` in the Clipboard bundle: the request is written here
 * AND a hidden button is pressed, and the press is the half that install
 * never lost.
 */
window.minipaintWriteInput = window.minipaintWriteInput || function (element, value) {
    if (!element) { return false; }
    const text = String(value == null ? "" : value);
    let native = false;
    try {
        const prototype = element.tagName === "TEXTAREA"
            ? window.HTMLTextAreaElement.prototype
            : window.HTMLInputElement.prototype;
        const descriptor = Object.getOwnPropertyDescriptor(prototype, "value");
        if (descriptor && descriptor.set) {
            descriptor.set.call(element, text);
            native = true;
        }
    } catch (e) {
        /* fall through to the plain assignment */
    }
    if (!native) {
        try { element.value = text; } catch (e) { return false; }
    }
    try {
        element.dispatchEvent(new Event("input", { bubbles: true }));
        element.dispatchEvent(new Event("change", { bubbles: true }));
    } catch (e) {
        return false;
    }
    return true;
};

/**
 * What the host's own framework put on the wire, and when.
 *
 * WHY THIS EXISTS. When a page asks Gradio to do something and nothing
 * happens, there are three different faults with one symptom, and no way to
 * tell them apart from inside the page:
 *
 *   1. the event never fired - the framework did not hear the page, so
 *      nothing was ever requested;
 *   2. it fired and the request failed - blocked, refused, or timed out;
 *   3. it fired and succeeded, and the answer never got back here.
 *
 * Four builds were spent on this extension's sending, each fixing a
 * plausible version of (1), because nothing in any log could rule (2) or (3)
 * out. One line saying whether a request left the browser at all separates
 * them, and it is worth more than any amount of reasoning about what Gradio
 * might be doing.
 *
 * Read-only, on purpose. The obvious way to collect this is to wrap
 * window.fetch, and wrapping the host's fetch to diagnose a fault is a good
 * way to become one. PerformanceObserver is told about every request the
 * page makes and cannot affect any of them; the cost is the HTTP status,
 * which only some browsers report here - and "did a request happen" is the
 * question that matters.
 *
 * Nothing here is sent anywhere by itself. A caller that has just watched
 * something fail asks for the window it cares about and puts a sentence in
 * the log.
 */
window.minipaintNetJournal = window.minipaintNetJournal || (function () {
    "use strict";

    //: The host's own API calls, whatever version it is on: Gradio 3's
    //: predict/queue, Gradio 4's, and the /gradio_api prefix 5 moved to.
    const HOST_CALL = /\/(?:gradio_api\/)?(?:queue\/(?:join|data)|run\/predict|api\/predict|reset|upload|stream)(?:[/?]|$)/;
    const KEEP = 80;
    const seen = [];
    let observing = false;

    function pathOf(url) {
        try { return new URL(url, document.baseURI).pathname.slice(0, 120); } catch (e) { return String(url).slice(0, 120); }
    }

    function take(entries) {
        for (const entry of entries) {
            const name = String(entry.name || "");
            if (!HOST_CALL.test(name)) { continue; }
            seen.push({
                //: Wall-clock, so a caller can line this up against its own
                //: Date.now() deadlines without knowing about timeOrigin.
                at: Math.round(performance.timeOrigin + entry.startTime),
                ms: Math.round(entry.duration),
                path: pathOf(name),
                //: Chrome only; 0 elsewhere, and 0 from a request that never
                //: got an answer - which is itself the finding.
                status: typeof entry.responseStatus === "number" ? entry.responseStatus : 0,
                bytes: typeof entry.transferSize === "number" ? entry.transferSize : -1
            });
        }
        if (seen.length > KEEP) { seen.splice(0, seen.length - KEEP); }
    }

    function start() {
        if (observing) { return; }
        try {
            const observer = new PerformanceObserver(function (list) { take(list.getEntries()); });
            observer.observe({ type: "resource", buffered: true });
            observing = true;
        } catch (e) {
            // No observer: fall back to reading the buffer when asked. It is
            // capped by the browser and may have dropped the oldest entries,
            // which is still better than nothing to look at.
            observing = false;
        }
    }

    function sweep() {
        if (observing) { return; }
        try { take(performance.getEntriesByType("resource")); } catch (e) { /* nothing to read */ }
    }

    start();

    return {
        /** Every host API call since ``at`` (a Date.now() value). */
        since: function (at) {
            sweep();
            const from = Number(at) || 0;
            const recent = seen.filter(function (row) { return row.at >= from - 250; });
            // Duplicates are possible through the fallback; one row per call.
            const unique = [];
            for (const row of recent) {
                if (!unique.some(function (k) { return k.at === row.at && k.path === row.path; })) { unique.push(row); }
            }
            return unique;
        },
        /** That window as one line for a log. */
        sentence: function (at) {
            const rows = this.since(at);
            if (!rows.length) { return "no request left this browser"; }
            return rows.length + " request(s): " + rows.map(function (row) {
                return row.path + (row.status ? " " + row.status : "") + " in " + row.ms + "ms";
            }).join(", ");
        },
        /** Whether this page is even able to answer the question. */
        watching: function () { return observing; }
    };
})();

/**
 * Where the host has told this page to call it, against where it is.
 *
 * Gradio's frontend does not use relative URLs: it reads an absolute root
 * out of the config the server inlined and builds every API call from that.
 * The server works that root out from the request it saw, so a Forge behind
 * anything that terminates TLS - a front end, a tunnel, a browser extension
 * that upgrades the address bar - can serve a page over https whose config
 * says http. The browser then blocks every one of those calls as mixed
 * content, silently, while everything this extension does over a relative
 * URL keeps working perfectly.
 *
 * Reported, not repaired. The repair is one line - put the page's own origin
 * in the config before Gradio boots - and it is the host's page, the host's
 * framework and the host's config; an extension that quietly rewrites it is
 * one upgrade away from breaking an install that was fine. Said plainly, it
 * takes a user about a minute to fix at the source.
 *
 * ``root`` and ``here`` are for the checks: production passes neither and
 * this reads the page it is on. A browser will not let `location.protocol`
 * be redefined, so the one verdict that matters - an https page told to call
 * an http host - cannot be exercised any other way.
 */
window.minipaintHostRoot = window.minipaintHostRoot || function (root, here) {
    if (root === undefined) {
        try { root = String((window.gradio_config && window.gradio_config.root) || ""); } catch (e) { root = ""; }
    }
    root = String(root || "");
    const page = here || window.location;
    if (!root) { return { known: false, note: "this page has no host config to read" }; }
    if (!/^https?:\/\//i.test(root)) {
        // A relative root is built onto the page's own origin: nothing to
        // disagree about.
        return { known: true, ok: true, root: root, note: "the host's root is relative to this page" };
    }
    let parsed;
    try { parsed = new URL(root); } catch (e) { return { known: false, note: "the host's root is not a URL" }; }
    if (parsed.origin === page.origin) {
        return { known: true, ok: true, root: parsed.origin, note: "the host's root is this page's own origin" };
    }
    const mixed = page.protocol === "https:" && parsed.protocol === "http:";
    return {
        known: true, ok: false, root: parsed.origin, mixed: mixed,
        note: "the host tells this page to call it at " + parsed.origin + " while the page is on "
            + page.origin + (mixed
                ? " - a browser blocks that as mixed content, so no Gradio event can leave this page."
                  + " Forge has to be told it is behind TLS (an x-forwarded-proto header from whatever"
                  + " terminates it, or --subpath/root_path), or reached over plain http."
                : " - a browser will not let the page call the other origin without CORS.")
    };
};

/**
 * Whether the host actually wired up the control this page is pressing.
 *
 * THE QUESTION NOTHING ELSE ANSWERS. A control that does nothing looks the
 * same whether the page is failing to reach the server or the event was
 * never on the page to begin with - and the second is not hypothetical. An
 * event whose outputs name a component that is not in this build is a
 * dependency the frontend cannot run, and it fails the way an unreachable
 * server fails: silently, every time, on every build. Everything else on the
 * same tab keeps working, because everything else names only its own
 * components.
 *
 * The host inlines its whole graph into the page, so this is simply a
 * lookup: find the component that carries this elem_id, then find the events
 * that fire from it. Nothing is written and nothing is fetched.
 *
 * ``elements`` is the other half. Two DOM nodes carrying one id is a page
 * built twice, and then getElementById hands the script whichever came
 * first, which may not be the one the framework is listening to - a write
 * that lands in the document and is heard by nobody.
 */
window.minipaintHostWiring = window.minipaintHostWiring || function (elemId) {
    const found = { id: String(elemId || ""), elements: 0, components: 0, triggers: [], known: false };
    try {
        found.elements = document.querySelectorAll('[id="' + String(elemId).replace(/["\\]/g, "\\$&") + '"]').length;
    } catch (e) { /* the count is simply not known */ }
    let config = null;
    try { config = window.gradio_config; } catch (e) { config = null; }
    if (!config || !Array.isArray(config.components)) { return found; }
    found.known = true;
    const ids = [];
    for (const component of config.components) {
        const props = component && component.props;
        if (props && props.elem_id === found.id) { ids.push(component.id); }
    }
    found.components = ids.length;
    const everything = new Set();
    for (const component of config.components) { if (component) { everything.add(component.id); } }
    const dependencies = Array.isArray(config.dependencies) ? config.dependencies : [];
    // Components an event names that are not on this page.
    //
    // An event is not runnable just because its trigger is wired: its inputs
    // and outputs are component ids too, and one of them belonging to a
    // build this page is not - a tab the host hid, a component captured
    // before the page was rebuilt - leaves a dependency the frontend cannot
    // run. It then fails the way an unreachable server fails, on every
    // press, for ever, while every event on the same tab that names only its
    // own components carries on working. Sending is the one thing this
    // extension does whose outputs belong to other tabs, which is exactly
    // the shape of a fault that hits sending and nothing else.
    found.missing = 0;
    for (const dependency of dependencies) {
        const targets = Array.isArray(dependency.targets) ? dependency.targets : [];
        let mine = false;
        for (const target of targets) {
            //: Gradio 4 pairs the component with its trigger; 3 named the
            //: trigger on the dependency itself.
            const component = Array.isArray(target) ? target[0] : target;
            const trigger = Array.isArray(target) ? target[1] : (dependency.trigger || "?");
            if (ids.indexOf(component) === -1) { continue; }
            mine = true;
            if (found.triggers.indexOf(trigger) === -1) { found.triggers.push(trigger); }
        }
        if (!mine) { continue; }
        for (const wired of [].concat(dependency.inputs || [], dependency.outputs || [])) {
            if (!everything.has(wired)) { found.missing += 1; }
        }
    }
    return found;
};

/** ``minipaintHostWiring`` as a clause for a log line. */
window.minipaintHostWiringNote = window.minipaintHostWiringNote || function (label, elemId) {
    let wiring;
    try { wiring = window.minipaintHostWiring(elemId); } catch (e) { return label + ": not known"; }
    if (!wiring.known) { return label + ": " + wiring.elements + " element(s), this page has no host graph to read"; }
    return label + ": " + wiring.elements + " element(s), " + wiring.components + " component(s), "
        + (wiring.triggers.length ? "wired for " + wiring.triggers.join("+")
                                  : "NO EVENT IS WIRED TO IT ON THIS PAGE")
        + (wiring.missing ? " but naming " + wiring.missing + " component(s) NOT ON THIS PAGE, which is an event"
                            + " the host cannot run" : "");
};

window.minipaintAssets = window.minipaintAssets || (function () {
    "use strict";

    const loaded = Object.create(null);

    const STATE = "data-minipaint-state";
    const BUNDLE = "data-minipaint-bundle";

    function element_for(key) {
        return document.querySelector("script[" + BUNDLE + '="' + key + '"]');
    }

    function one(url) {
        const key = String(url || "");
        if (!key) { return Promise.resolve(false); }
        if (loaded[key]) { return loaded[key]; }
        loaded[key] = new Promise(function (resolve) {
            // A script the document already carries is only worth reusing if
            // it actually ran. The element alone proves nothing: a 404 also
            // leaves one behind, and a reload that kept the DOM keeps the
            // failures along with the successes. One that is still loading
            // is joined rather than duplicated - two tabs asking for the
            // same bundle at once must not fetch it twice.
            const existing = element_for(key);
            if (existing) {
                const state = existing.getAttribute(STATE);
                if (state === "loaded") { resolve(true); return; }
                if (state === "loading") {
                    existing.addEventListener("load", function () { resolve(true); });
                    existing.addEventListener("error", function () { resolve(false); });
                    return;
                }
                // Failed, or from a build that did not mark its state. Neither
                // can be trusted, and leaving it would make the next lookup
                // find it again.
                try { existing.remove(); } catch (e) { /* not fatal */ }
            }
            const element = document.createElement("script");
            element.src = key;
            element.async = false;
            element.defer = false;
            element.setAttribute(BUNDLE, key);
            element.setAttribute(STATE, "loading");
            element.addEventListener("load", function () {
                element.setAttribute(STATE, "loaded");
                resolve(true);
            });
            element.addEventListener("error", function () {
                console.error("MiniPaint: the bundle " + key + " could not be loaded; that tab stays degraded.");
                // Both halves matter. The cache entry is dropped so a later
                // attempt builds a new Promise, and the element is taken out
                // of the document so that attempt makes a real request
                // instead of finding this one and calling it success.
                element.setAttribute(STATE, "failed");
                try { element.remove(); } catch (e) { /* not fatal */ }
                delete loaded[key];
                resolve(false);
            });
            (document.head || document.documentElement).appendChild(element);
        });
        return loaded[key];
    }

    function app() {
        try {
            if (typeof gradioApp === "function") { return gradioApp() || document; }
        } catch (e) { /* fall through */ }
        return document;
    }

    /** Whether a top-level tab's panel is the one on screen. */
    function showing(panel) {
        if (!panel) { return false; }
        if (panel.style && panel.style.display === "none") { return false; }
        return !!(panel.offsetParent || (panel.getClientRects && panel.getClientRects().length));
    }

    /**
     * Load these bundles when a tab is first opened - or now, if it is
     * already open, or if this page has no such tab to watch.
     *
     * The fallbacks are the point. A tab that cannot be found, a nav that
     * is shaped differently, a host that renders its tabs some other way:
     * every one of those loads the bundle immediately rather than leaving a
     * tab that never works. Being lazy is the optimisation; being there is
     * the requirement.
     */
    function loadOnTab(panelId, urls) {
        const root = app();
        const panel = root.querySelector ? root.querySelector("#" + panelId) : null;
        if (!panel || showing(panel)) { return load(urls); }
        const button = root.querySelector('button[aria-controls="' + panelId + '"]');
        if (!button) { return load(urls); }
        return new Promise(function (resolve) {
            let settled = false;
            const go = function () {
                if (settled) { return; }
                settled = true;
                button.removeEventListener("click", go);
                load(urls).then(resolve, function () { resolve(false); });
            };
            button.addEventListener("click", go);
            // A tab that becomes visible without this button being clicked -
            // another script switching to it, a deep link - is still a tab
            // whose bundle is wanted.
            if (typeof MutationObserver === "function") {
                const observer = new MutationObserver(function () {
                    if (!showing(panel)) { return; }
                    observer.disconnect();
                    go();
                });
                observer.observe(panel, { attributes: true, attributeFilter: ["style", "class"] });
            }
        });
    }

    /**
     * Load every URL given, and resolve true only if all of them ran.
     *
     * The reduction is the contract. Promise.all resolves to an array, and
     * an array is truthy even when every entry in it is false, so a caller
     * guarding on the result of an unreduced load() is guarding on nothing.
     * Optional bundles therefore get their own load() call: one optional
     * false must not become a required false.
     */
    function load(urls) {
        const wanted = Array.isArray(urls) ? urls : [urls];
        return Promise.all(wanted.map(one)).then(function (results) {
            return results.every(Boolean);
        });
    }

    /** Whether a bundle is loaded and ran, without asking for it. */
    function ready(url) {
        const existing = element_for(String(url || ""));
        return !!existing && existing.getAttribute(STATE) === "loaded";
    }

    //: Imported modules by URL. A module is not a bundle: it has exports,
    //: so it is imported rather than appended as a <script>, and what the
    //: caller wants is the namespace and not a boolean.
    const modules = {};

    /**
     * Import a shared module once, and hand every later caller the same one.
     *
     * The transfer library both tabs deliver pictures with is a module, and
     * both tabs ask for it on their own load - so the request has to be
     * shared rather than repeated. A failed import resolves to null and is
     * forgotten, so a later attempt is a real attempt: a tab left unable to
     * send because one import failed once is the whole failure mode this
     * loader exists to avoid.
     */
    function module(url) {
        const key = String(url || "");
        if (!key) { return Promise.resolve(null); }
        if (modules[key]) { return modules[key]; }
        const pending = import(key).then(function (namespace) {
            // The transfer library, named so a later caller can ask for it
            // again by URL rather than having to be told where it lives.
            if (namespace && typeof namespace.set_image_file === "function") {
                window.minipaintHost = window.minipaintHost || namespace;
                window.minipaintHostUrl = window.minipaintHostUrl || key;
            }
            return namespace;
        }, function (error) {
            delete modules[key];
            console.error("MiniPaint: could not import " + key, error);
            return null;
        });
        modules[key] = pending;
        return pending;
    }

    return {
        load: load,
        loadOnTab: loadOnTab,
        ready: ready,
        module: module,
        loaded: function () { return Object.keys(loaded); }
    };
})();

/**
 * Canvas readiness: the one thing that means "the editor is actually there".
 *
 * WHY THIS LIVES HERE AND NOT IN THE CANVAS BUNDLE.
 *
 * Everything that can reach the Canvas from outside its own tab - Send to
 * Mini Paint on the txt2img row, the Clipboard hand-off, a deep link - can
 * run before the Canvas bundle has been fetched, which is precisely when
 * ``window.minipaintCanvas`` does not exist yet. A readiness flag kept
 * inside that bundle would be unreachable exactly when it is needed. So it
 * lives in the bootstrap, which is on every page from the start.
 *
 * WHAT THE CONTRACT IS.
 *
 *   ensure()     -> Promise<boolean>, settles only once the Canvas bundle
 *                   has settled AND attach() has reported. true means there
 *                   is a live editor on the page.
 *   whenReady()  -> the same promise, for a caller that only wants to wait.
 *   retry()      -> forget a failure and try the whole thing again.
 *   state()      -> "idle" | "loading" | "ready" | "failed".
 *
 * Calling ensure() twice does not load twice, attach twice, or return two
 * different answers; a failure is retryable and a success is permanent
 * until the page is rebuilt. Optional integrations are deliberately NOT
 * part of this promise - a WanGP bundle that 404s must cost WanGP and
 * nothing else.
 */
window.minipaintCanvasReady = window.minipaintCanvasReady || (function () {
    "use strict";

    const S = { url: "", uuid: "", options: null, status: "", state: "idle", pending: null, watchers: [] };

    const ALERT_CLASS = "minipaint-canvas-alert";

    /**
     * Say so on the page when the editor did not start.
     *
     * A failed startup is invisible otherwise: the Gradio shell renders
     * perfectly well without an adapter behind it, which is what made this
     * failure so confusing to begin with - the tab looked healthy and
     * nothing in it worked. This is a diagnostic and a way back, not a new
     * screen, so it is one line and a button.
     *
     * It is a sibling of the status line rather than its content: that
     * Markdown block belongs to the server, which rewrites it whenever the
     * picture changes, and the two would take turns erasing each other.
     */
    function render() {
        if (!S.status) { return; }
        const status = document.getElementById(S.status);
        if (!status) { return; }
        // Below the whole top row, not beside the status line inside it. That
        // row is a nowrap flex container, so a sibling of the status text is
        // squeezed in next to the tool buttons instead of getting its own
        // line - and this notice has a button on it.
        const anchor = status.closest(".minipaint-topbar") || status;
        if (!anchor.parentNode) { return; }
        const existing = anchor.parentNode.querySelector(":scope > ." + ALERT_CLASS);
        if (S.state !== "failed") {
            if (existing) { existing.remove(); }
            return;
        }
        if (existing) { return; }
        const alert = document.createElement("div");
        alert.className = ALERT_CLASS;
        const text = document.createElement("span");
        text.textContent = "Mini Paint failed to initialize.";
        const button = document.createElement("button");
        button.type = "button";
        button.className = "minipaint-canvas-alert-retry";
        button.textContent = "Retry";
        button.addEventListener("click", function () {
            button.disabled = true;
            button.textContent = "Retrying…";
            // Only the Canvas bundle and its attach, which is the whole of
            // what failed. Optional integrations are not retried from here.
            retry().then(function (ok) {
                if (ok) { return; }
                button.disabled = false;
                button.textContent = "Retry";
            });
        });
        alert.appendChild(text);
        alert.appendChild(button);
        anchor.parentNode.insertBefore(alert, anchor.nextSibling);
    }

    function announce() {
        const state = S.state;
        S.watchers.slice().forEach(function (fn) {
            try { fn(state); } catch (e) { /* a watcher must not break the load */ }
        });
    }

    function moveTo(state) {
        if (S.state === state) { return; }
        S.state = state;
        try { render(); } catch (e) { /* the page must still load */ }
        announce();
    }

    /**
     * Told by the page which surface to attach and where the bundle is.
     *
     * Reload UI builds a second surface with a new uuid, so a configure for
     * a different uuid invalidates whatever was decided for the old one.
     */
    function configure(config) {
        config = config || {};
        const uuid = String(config.uuid || "");
        const changed = uuid && uuid !== S.uuid;
        S.url = String(config.url || S.url || "");
        if (config.status) { S.status = String(config.status); }
        if (uuid) { S.uuid = uuid; }
        if (config.options) { S.options = config.options; }
        if (changed) {
            // Through moveTo, so a notice left over from the previous
            // surface's failure comes down with it.
            S.pending = null;
            moveTo("idle");
        }
        return S.uuid;
    }

    function attempt() {
        const assets = window.minipaintAssets;
        if (!assets || !assets.load || !S.url || !S.uuid) {
            moveTo("failed");
            return Promise.resolve(false);
        }
        moveTo("loading");
        return assets.load([S.url]).then(function (ok) {
            const api = window.minipaintCanvas;
            if (!ok || !api || typeof api.attach !== "function") {
                moveTo("failed");
                return false;
            }
            // attach() is the authority, not the bundle arriving: the
            // container can be missing even when the script ran perfectly.
            //
            // And it is called inside a try, because a promise that neither
            // resolves nor rejects is the worst of the three outcomes: every
            // entry point now waits on this one, so an exception escaping
            // here would hang Send to Mini Paint for the life of the page
            // rather than failing it. A throw is a failed startup, which is
            // a state the page can show and the user can retry.
            let attached = false;
            try {
                attached = api.attach(S.uuid, S.options) !== false;
            } catch (error) {
                console.error("MiniPaint: the Canvas adapter could not attach.", error);
                attached = false;
            }
            moveTo(attached ? "ready" : "failed");
            return attached;
        }).catch(function (error) {
            console.error("MiniPaint: the Canvas could not be started.", error);
            moveTo("failed");
            return false;
        });
    }

    function ensure() {
        if (S.state === "ready") {
            const api = window.minipaintCanvas;
            const live = !!(api && api.attachedTo && api.attachedTo(S.uuid));
            if (live) { return Promise.resolve(true); }
            // Ready, but the editor is not on the page any more: the surface
            // was replaced under us. The settled promise that said "true" is
            // now wrong, and returning it would make every later caller wrong
            // too, so it goes and the next attempt is a real one.
            S.pending = null;
            moveTo("idle");
        }
        if (S.pending) { return S.pending; }
        S.pending = attempt().then(function (ok) {
            // A failure is forgotten so the next caller - or the Retry
            // button - makes a real attempt rather than being handed this
            // one's answer forever.
            if (!ok) { S.pending = null; }
            return ok;
        });
        return S.pending;
    }

    function retry() {
        S.pending = null;
        if (S.state !== "ready") { S.state = "idle"; }
        return ensure();
    }

    function onState(fn) {
        if (typeof fn === "function") {
            S.watchers.push(fn);
            try { fn(S.state); } catch (e) { /* as above */ }
        }
    }

    return {
        configure: configure,
        ensure: ensure,
        whenReady: ensure,
        retry: retry,
        onState: onState,
        state: function () { return S.state; },
        uuid: function () { return S.uuid; }
    };
})();

(function () {
    "use strict";

    const TIMEOUT_MS = 30000;

    // [output button row, gallery that row belongs to]
    const TARGETS = [
        ["image_buttons_txt2img", "txt2img_gallery"],
        ["image_buttons_img2img", "img2img_gallery"],
        ["image_buttons_extras", "extras_gallery"],
    ];

    function root() {
        try {
            if (typeof gradioApp === "function") {
                return gradioApp() || document;
            }
        } catch (e) {
            /* fall through */
        }
        return document;
    }

    /**
     * The narrowest container that holds every output row we bind to.
     *
     * Forge puts all of them inside one `gr.Tabs(elem_id="tabs")`. Observing
     * that instead of the whole application is the difference between
     * watching one subtree during hydration and watching every node the
     * WebUI creates - galleries, progress bars, every extension's own UI -
     * for the whole of it. The document is the fallback for a host that has
     * no such container, not the default.
     */
    function scope() {
        const app = root();
        return (app.querySelector && app.querySelector("#tabs")) || app;
    }

    /**
     * Resolve once every selector exists, watching one subtree once.
     *
     * ONE observer for all of them, not one each. Six observers over the
     * document during hydration is six callbacks per mutation, and hydration
     * is thousands of mutations; they also each outlived their own target
     * until the whole set had resolved. This checks first - by the time the
     * iframe has loaded its bundle the rows are usually already there, which
     * is the common case and costs no observer at all - and otherwise
     * watches the smallest container that can contain them, disconnecting
     * the moment the last one appears.
     *
     * Still not `onUiLoaded`, and the reason has not changed: that callback
     * list fires once, as soon as #txt2img_prompt exists, which is long
     * before this extension's iframe finishes loading. Registering after it
     * has fired never runs at all.
     */
    function waitForSelectors(selectors, timeoutMs) {
        function found() {
            const app = root();
            const out = {};
            for (const selector of selectors) {
                const element = app.querySelector(selector);
                if (!element) { return null; }
                out[selector] = element;
            }
            return out;
        }

        const ready = found();
        if (ready) { return Promise.resolve(ready); }

        return new Promise(function (resolve, reject) {
            let observer = null;
            const timer = setTimeout(function () {
                if (observer) { observer.disconnect(); }
                reject(new Error("MiniPaint: the output rows were not found within " + timeoutMs + "ms"));
            }, timeoutMs);

            observer = new MutationObserver(function () {
                const all = found();
                if (!all) { return; }
                clearTimeout(timer);
                observer.disconnect();
                resolve(all);
            });

            observer.observe(scope(), { childList: true, subtree: true });
        });
    }

    /**
     * Wait for the iframe to publish its hooks. This is not a DOM change, so
     * it is polled rather than observed.
     */
    function waitForBridge(timeoutMs) {
        const deadline = Date.now() + timeoutMs;

        return new Promise(function (resolve, reject) {
            (function poll() {
                if (typeof window.a1111minipaint.createSendButton === "function") {
                    resolve(true);
                } else if (Date.now() > deadline) {
                    reject(new Error("MiniPaint: the iframe never registered createSendButton()"));
                } else {
                    setTimeout(poll, 100);
                }
            })();
        });
    }

    async function bindButtons() {
        await waitForBridge(TIMEOUT_MS);

        const selectors = [];
        for (const target of TARGETS) { selectors.push("#" + target[0], "#" + target[1]); }
        try {
            await waitForSelectors(selectors, TIMEOUT_MS);
        } catch (e) {
            // A host without one of the rows - a Forge with extras switched
            // off, say - must not cost the tabs that are there, so the wait
            // is best-effort and each binding is still tried on its own.
            console.error(e);
        }

        const app = root();
        for (const target of TARGETS) {
            const buttonsId = target[0];
            const gallery = app.querySelector("#" + target[1]);
            if (!app.querySelector("#" + buttonsId) || !gallery) { continue; }
            try {
                window.a1111minipaint.createSendButton(buttonsId, gallery);
            } catch (e) {
                // One missing tab must not stop the others from binding.
                console.error(e);
            }
        }
    }

    let pending = null;

    /**
     * Called from the iframe's onload. The WebUI can reload its UI, which
     * reloads the iframe, so this may run several times per page: chain the
     * runs so concurrent scans cannot interleave. createSendButton() itself is
     * idempotent.
     */
    window.a1111minipaint.onload = function () {
        pending = (pending || Promise.resolve())
            .catch(function () { })
            .then(bindButtons)
            .catch(function (e) { console.error(e); });
        return pending;
    };
})();
