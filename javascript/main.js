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
