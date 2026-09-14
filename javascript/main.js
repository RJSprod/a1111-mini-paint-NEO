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
 */
window.minipaintAssets = window.minipaintAssets || (function () {
    "use strict";

    const loaded = Object.create(null);

    function one(url) {
        const key = String(url || "");
        if (!key) { return Promise.resolve(false); }
        if (loaded[key]) { return loaded[key]; }
        loaded[key] = new Promise(function (resolve) {
            // A script the document already carries - a reload that kept the
            // element, or a second tab asking for the same bundle - is not
            // added again, whatever this object remembers.
            const existing = document.querySelector('script[data-minipaint-bundle="' + key + '"]');
            if (existing) { resolve(true); return; }
            const element = document.createElement("script");
            element.src = key;
            element.async = false;
            element.defer = false;
            element.setAttribute("data-minipaint-bundle", key);
            element.addEventListener("load", function () { resolve(true); });
            element.addEventListener("error", function () {
                console.error("MiniPaint: the bundle " + key + " could not be loaded; that tab stays degraded.");
                // Forgotten, so a later tab activation may try again: a
                // bundle that failed once on a flaky connection should not
                // be permanently unavailable for the life of the page.
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

    function load(urls) {
        const wanted = Array.isArray(urls) ? urls : [urls];
        return Promise.all(wanted.map(one));
    }

    return {
        load: load,
        loadOnTab: loadOnTab,
        loaded: function () { return Object.keys(loaded); }
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
