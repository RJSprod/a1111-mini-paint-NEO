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
