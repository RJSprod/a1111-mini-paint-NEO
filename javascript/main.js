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

    /** Resolve once `selector` exists in the WebUI DOM. */
    function waitForSelector(selector, timeoutMs) {
        const existing = root().querySelector(selector);
        if (existing) {
            return Promise.resolve(existing);
        }

        return new Promise(function (resolve, reject) {
            const timer = setTimeout(function () {
                observer.disconnect();
                reject(new Error("MiniPaint: " + selector + " was not found within " + timeoutMs + "ms"));
            }, timeoutMs);

            const observer = new MutationObserver(function () {
                const element = root().querySelector(selector);
                if (element) {
                    clearTimeout(timer);
                    observer.disconnect();
                    resolve(element);
                }
            });

            observer.observe(root(), { childList: true, subtree: true });
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

        for (const target of TARGETS) {
            const buttonsId = target[0];
            const galleryId = target[1];
            try {
                await waitForSelector("#" + buttonsId, TIMEOUT_MS);
                const gallery = await waitForSelector("#" + galleryId, TIMEOUT_MS);
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
