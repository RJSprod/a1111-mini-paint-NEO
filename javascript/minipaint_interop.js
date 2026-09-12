/**
 * The public inter-extension API: window.minipaintInterop, version 1.
 *
 * Any extension on this Forge page may add a request to the live WanGP page's
 * own queue through this object, and Clipboard - the first caller - uses it
 * the same way a third party would. A caller supplies a prompt and images
 * only where it wants them; everything it leaves out is inherited from the
 * WanGP page exactly as it is, which is the one rule the whole contract is
 * built on (minipaint.wangp.queue/v1, section 0). A caller never learns a
 * postMessage type, a bridge session, a Gradio component id, a filesystem
 * path or a WanGP letter flag: it hands over a Blob and gets a token, hands
 * over tokens and gets a code.
 *
 * Why it lives in the browser: the WanGP form a request overlays is Gradio
 * session state belonging to the iframe in *this* document. A server-only
 * endpoint could not know which page's session a caller meant; a page can.
 * The server is asked for two things only - to hold bytes behind a token,
 * and to turn tokens into the opaque handoffs the bridge reads.
 *
 * Calls to enqueue() are serialised on one FIFO: a queue request owns the
 * live form from the moment the bridge writes into it until the admission is
 * settled and the overrides are put back, so a second request must wait its
 * turn rather than write over the first one's overlay. The bridge enforces
 * the same rule as defence in depth (QUEUE_BUSY); an ordinary caller never
 * sees it because the queue here goes first.
 *
 * The wrapper normalises its public contract itself and delegates the
 * mechanics to window.minipaintWanGP, so that a change to the internal wire
 * protocol cannot change what a caller of version 1 sees.
 */
window.minipaintInterop = (function () {
    "use strict";

    const VERSION = 1;
    const CONTRACT = "minipaint.wangp.queue/v1";
    const STAGE_ROUTE = "/minipaint-interop/stage";
    const PREPARE_ROUTE = "/minipaint-interop/prepare";
    const RELEASE_ROUTE = "/minipaint-interop/release";
    const HEX32 = /^[0-9a-f]{32}$/;
    const CODE_RE = /^[A-Z][A-Z0-9_]{2,59}$/;
    const PROMPT_MAX_CHARS = 4000;
    const MAX_REFERENCES = 16;
    const KINDS = ["staged", "clipboard_asset"];
    const FIELDS = ["prompt", "start", "end", "references"];
    const STAGE_MAX_BYTES = 32 * 1024 * 1024;

    // The sentences a caller may show. The server's errors.py owns the
    // wording; these are the ones this side needs before it can ask.
    const MESSAGES = {
        IFRAME_NOT_READY: "WanGP is not available in this page.",
        BRIDGE_COMPONENT_INCOMPATIBLE: "This WanGP bridge needs to be reinstalled or updated.",
        REQUEST_INVALID: "That queue request is not one this extension can carry.",
        REQUEST_ID_CONFLICT: "That request id was already used for a different request.",
        PROMPT_TOO_LONG: "The prompt is longer than WanGP queue requests allow (4000 characters).",
        IMAGE_STAGE_INVALID: "That image could not be staged for WanGP.",
        IMAGE_STAGE_EXPIRED: "The staged image is no longer there; stage it again.",
        HANDOFF_TOO_LARGE: "The image is too large for the WanGP handoff.",
        QUEUE_BUSY: "WanGP is still taking the previous queue request; try again in a moment.",
        QUEUE_REQUEST_REFUSED: "WanGP did not take the queue request.",
        ADMISSION_UNCONFIRMED: "WanGP did not confirm that the request was added to the queue.",
        WANGP_VALIDATION_REFUSED: "WanGP declined the queue request; check the WanGP page for details.",
        INTERNAL_ERROR: "The WanGP integration hit an unexpected problem."
    };

    let chain = Promise.resolve();

    function bridge() {
        const api = window.minipaintWanGP;
        return api && typeof api.queueAndConfirm === "function" ? api : null;
    }

    function hex32() {
        const bytes = new Uint8Array(16);
        if (window.crypto && typeof window.crypto.getRandomValues === "function") {
            window.crypto.getRandomValues(bytes);
        } else {
            for (let k = 0; k < bytes.length; k++) { bytes[k] = Math.floor(Math.random() * 256); }
        }
        let text = "";
        for (const byte of bytes) { text += byte.toString(16).padStart(2, "0"); }
        return text;
    }

    function code(value) {
        return typeof value === "string" && CODE_RE.test(value) ? value : "";
    }

    function sentence(failureCode, fallback) {
        return MESSAGES[failureCode] || fallback || MESSAGES.INTERNAL_ERROR;
    }

    function refusal(failureCode, requestId, message) {
        const why = code(failureCode) || "INTERNAL_ERROR";
        return { ok: false, status: "refused", request_id: requestId || "", code: why, message: message || sentence(why) };
    }

    function note(message) {
        const api = window.minipaintWanGP;
        if (api && typeof api.note === "function") {
            try { api.note("interop: " + message); } catch (e) { /* a log line is never worth an exception */ }
        }
    }

    /* ------------------------------------------------------------------ */
    /* The request                                                           */
    /* ------------------------------------------------------------------ */

    /** One image handle as the contract defines it, or null. */
    function normaliseHandle(raw) {
        if (!raw || typeof raw !== "object") { return null; }
        if (KINDS.indexOf(raw.kind) === -1) { return null; }
        if (!HEX32.test(String(raw.id || ""))) { return null; }
        return { kind: raw.kind, id: String(raw.id) };
    }

    /**
     * A public request, normalised: an absent, null or empty field means
     * "inherit the WanGP page's value", an empty reference list is absence,
     * and anything unlisted is dropped. Returns {ok, request} or a refusal.
     */
    function normaliseRequest(raw) {
        raw = raw && typeof raw === "object" ? raw : {};
        let requestId = "";
        if (raw.request_id !== undefined && raw.request_id !== null && raw.request_id !== "") {
            if (!HEX32.test(String(raw.request_id))) { return refusal("REQUEST_INVALID", "", "request_id must be 32 lowercase hex characters."); }
            requestId = String(raw.request_id);
        } else {
            requestId = hex32();
        }
        const request = { request_id: requestId, images: {} };
        if (raw.prompt !== undefined && raw.prompt !== null) {
            if (typeof raw.prompt !== "string") { return refusal("REQUEST_INVALID", requestId, "prompt must be text."); }
            const trimmed = raw.prompt.trim();
            if (trimmed.length > PROMPT_MAX_CHARS) { return refusal("PROMPT_TOO_LONG", requestId); }
            if (trimmed) { request.prompt = raw.prompt; }
        }
        const images = raw.images && typeof raw.images === "object" ? raw.images : {};
        for (const field of ["start", "end"]) {
            const value = images[field];
            if (value === undefined || value === null || value === "") { continue; }
            const handle = normaliseHandle(value);
            if (!handle) { return refusal("REQUEST_INVALID", requestId, "images." + field + " is not a known image handle."); }
            request.images[field] = handle;
        }
        if (images.references !== undefined && images.references !== null) {
            if (!Array.isArray(images.references)) { return refusal("REQUEST_INVALID", requestId, "images.references must be a list."); }
            const kept = [];
            for (const item of images.references) {
                if (item === undefined || item === null || item === "") { continue; }
                const handle = normaliseHandle(item);
                if (!handle) { return refusal("REQUEST_INVALID", requestId, "a reference is not a known image handle."); }
                kept.push(handle);
            }
            if (kept.length > MAX_REFERENCES) { return refusal("REQUEST_INVALID", requestId, "more than " + MAX_REFERENCES + " reference images."); }
            if (kept.length) { request.images.references = kept; }
        }
        return { ok: true, request: request };
    }

    function hasImages(request) {
        return !!(request.images.start || request.images.end || request.images.references);
    }

    /* ------------------------------------------------------------------ */
    /* The server: bytes behind a token, tokens into handoffs               */
    /* ------------------------------------------------------------------ */

    async function post(route, body, contentType) {
        const response = await fetch(route, {
            method: "POST",
            credentials: "same-origin",
            cache: "no-store",
            headers: { "Content-Type": contentType || "application/json" },
            body: body
        });
        let payload = null;
        try { payload = await response.json(); } catch (e) { payload = null; }
        if (!payload || typeof payload !== "object") {
            return { ok: false, code: response.status === 401 || response.status === 403 ? "AUTH_BOUNDARY_FAILED" : "INTERNAL_ERROR",
                     message: "The server did not answer the request (" + response.status + ")." };
        }
        return payload;
    }

    /** Whatever a caller has an image as, as a Blob, or null. */
    async function asBlob(input) {
        if (typeof Blob !== "undefined" && input instanceof Blob) { return input; }
        if (input instanceof ArrayBuffer) { return new Blob([input]); }
        if (ArrayBuffer.isView(input)) { return new Blob([input]); }
        if (typeof HTMLCanvasElement !== "undefined" && input instanceof HTMLCanvasElement) {
            return new Promise(function (resolve) { input.toBlob(function (blob) { resolve(blob || null); }, "image/png"); });
        }
        if (typeof input === "string" && input.indexOf("data:image/") === 0) {
            try { return await (await fetch(input)).blob(); } catch (e) { return null; }
        }
        return null;
    }

    /**
     * Hand the server an image and get an opaque handle back. Takes a Blob
     * or File, an ArrayBuffer or typed array, a canvas, or a data: URL; the
     * bytes go to a same-origin route this extension owns, and only a token
     * comes back. The server decodes, bounds and re-encodes the picture as a
     * lossless PNG; the handle is good for a while and for one request.
     */
    async function stageImage(input, options) {
        const blob = await asBlob(input);
        if (!blob) { return { ok: false, code: "IMAGE_STAGE_INVALID", message: "stageImage takes a Blob, File, ArrayBuffer, canvas or data: URL." }; }
        if (blob.size > STAGE_MAX_BYTES) { return { ok: false, code: "HANDOFF_TOO_LARGE", message: sentence("HANDOFF_TOO_LARGE") }; }
        const type = (options && options.contentType) || blob.type || "application/octet-stream";
        let answer;
        try {
            answer = await post(STAGE_ROUTE, blob, type);
        } catch (error) {
            return { ok: false, code: "INTERNAL_ERROR", message: "The image could not be uploaded for staging." };
        }
        if (!answer.ok) { return { ok: false, code: code(answer.code) || "IMAGE_STAGE_INVALID", message: String(answer.message || sentence("IMAGE_STAGE_INVALID")) }; }
        const handle = normaliseHandle(answer.image);
        if (!handle) { return { ok: false, code: "IMAGE_STAGE_INVALID", message: sentence("IMAGE_STAGE_INVALID") }; }
        return { ok: true, image: handle, width: answer.width || 0, height: answer.height || 0 };
    }

    /* ------------------------------------------------------------------ */
    /* The queue                                                             */
    /* ------------------------------------------------------------------ */

    function summary(result) {
        const applied = result && result.applied && typeof result.applied === "object" ? result.applied : {};
        const out = { prompt: applied.prompt === true, start: applied.start === true, end: applied.end === true, references: 0 };
        if (Number.isFinite(applied.references) && applied.references > 0) { out.references = Math.trunc(applied.references); }
        const inherited = Array.isArray(result && result.inherited) ? result.inherited.filter(function (f) { return FIELDS.indexOf(f) !== -1; }) : [];
        const ignored = [];
        for (const item of Array.isArray(result && result.ignored) ? result.ignored : []) {
            if (item && FIELDS.indexOf(item.field) !== -1) { ignored.push({ field: item.field, code: code(item.code) || "RECEIVER_DISABLED" }); }
        }
        return { applied: out, inherited: inherited, ignored: ignored };
    }

    function model(result) {
        const raw = result && result.model && typeof result.model === "object" ? result.model : {};
        return { type: String(raw.type || "").slice(0, 120), label: String(raw.label || "").slice(0, 120), family: String(raw.family || "").slice(0, 120) };
    }

    /** The public result of section 11.5: codes, counts and the model - never
     * a prompt, a filename or a path. */
    function publicResult(result, requestId) {
        const three = summary(result);
        if (result && result.ok && result.status === "queued") {
            return { ok: true, status: "queued", request_id: requestId, tasks_added: Math.max(1, Math.trunc(result.tasks_added || 1)),
                     model: model(result), applied: three.applied, inherited: three.inherited, ignored: three.ignored };
        }
        const status = result && result.status === "unconfirmed" ? "unconfirmed" : "refused";
        const why = code(result && result.code) || (status === "unconfirmed" ? "ADMISSION_UNCONFIRMED" : "QUEUE_REQUEST_REFUSED");
        return { ok: false, status: status, request_id: requestId, code: why, message: (result && result.message) || sentence(why) };
    }

    async function enqueueNow(request) {
        const api = bridge();
        if (!api) { return refusal("IFRAME_NOT_READY", request.request_id); }
        const state = api.state();
        if (!state.present) { return refusal("IFRAME_NOT_READY", request.request_id); }
        if (state.ready && !state.queue) { return refusal("BRIDGE_COMPONENT_INCOMPATIBLE", request.request_id); }

        let wire = { request_id: request.request_id };
        if (request.prompt !== undefined) { wire.prompt = request.prompt; }
        let handoffs = [];
        if (hasImages(request)) {
            let prepared;
            try {
                prepared = await post(PREPARE_ROUTE, JSON.stringify({ request: request }));
            } catch (error) {
                return refusal("INTERNAL_ERROR", request.request_id, "The images could not be prepared for WanGP.");
            }
            if (!prepared.ok || !prepared.request || typeof prepared.request !== "object") {
                return refusal(code(prepared.code) || "REQUEST_INVALID", request.request_id, prepared.message);
            }
            wire = Object.assign(wire, {
                start_handoff_id: prepared.request.start_handoff_id,
                end_handoff_id: prepared.request.end_handoff_id,
                reference_handoff_ids: prepared.request.reference_handoff_ids
            });
            handoffs = Array.isArray(prepared.request.handoff_ids) ? prepared.request.handoff_ids : [];
        }
        note("enqueue " + request.request_id.slice(0, 8) + ": " + FIELDS.filter(function (field) {
            return field === "prompt" ? wire.prompt !== undefined : field === "references" ? !!wire.reference_handoff_ids : !!wire[field + "_handoff_id"];
        }).join(", ") + (handoffs.length ? " (" + handoffs.length + " image(s) prepared)" : ""));

        let result;
        try {
            result = await api.queueAndConfirm(wire);
        } catch (error) {
            result = { ok: false, status: "refused", code: "INTERNAL_ERROR" };
        } finally {
            if (handoffs.length) {
                // The bridge has decoded the pictures into WanGP's own values;
                // the files have done their job whichever way this ended.
                post(RELEASE_ROUTE, JSON.stringify({ handoff_ids: handoffs })).catch(function () { /* the sweep will */ });
            }
        }
        return publicResult(result, request.request_id);
    }

    /**
     * Add the live WanGP page - with these overrides, if any - to its queue.
     * Resolves only after a bounded admission check: ``queued`` when a task
     * was seen in WanGP's queue, ``refused`` with a code otherwise, or
     * ``unconfirmed`` when the check ran out without proof either way. One
     * call at a time, in order.
     */
    function enqueue(request) {
        const normalised = normaliseRequest(request);
        if (!normalised.ok) { return Promise.resolve(normalised); }
        const run = chain.then(function () { return enqueueNow(normalised.request); })
            .catch(function (error) { return refusal("INTERNAL_ERROR", normalised.request.request_id, String(error && error.message || error).slice(0, 120)); });
        chain = run.then(function () { }, function () { });
        return run;
    }

    /**
     * What the live page can take right now. Advisory: enqueue() judges the
     * request again, live, when it runs.
     */
    function capabilities() {
        const api = bridge();
        if (!api || typeof api.capabilities !== "function") { return Promise.resolve({ ok: false, code: "IFRAME_NOT_READY", message: sentence("IFRAME_NOT_READY") }); }
        return api.capabilities().then(function (answer) {
            if (!answer || !answer.ok) {
                const why = code(answer && answer.code) || "IFRAME_NOT_READY";
                return { ok: false, code: why, message: sentence(why) };
            }
            const inputs = answer.inputs || {};
            const references = inputs.references || {};
            return {
                ok: true,
                api_version: VERSION,
                ready: answer.ready === true,
                queue: answer.queue === true,
                model: model(answer),
                inputs: {
                    start: { supported: !!(inputs.start && inputs.start.supported) },
                    end: { supported: !!(inputs.end && inputs.end.supported) },
                    references: { supported: !!references.supported, max_count: Number.isFinite(references.max_count) ? references.max_count : null }
                }
            };
        }, function () {
            return { ok: false, code: "IFRAME_NOT_READY", message: sentence("IFRAME_NOT_READY") };
        });
    }

    return {
        version: VERSION,
        contract: CONTRACT,
        wangp: {
            capabilities: capabilities,
            stageImage: stageImage,
            enqueue: enqueue
        },
        // The sentence for a code, for a caller that wants the same words.
        message: function (failureCode) { return sentence(failureCode); }
    };
})();
