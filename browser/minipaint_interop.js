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
 * The server owns the line. enqueue() submits a job to the queue outbox on
 * the Forge server and returns when that job has ended; the server hands out
 * one lease at a time across every browser page, in the order the jobs were
 * submitted, and each page runs only the jobs it submitted - the WanGP form
 * a job overlays is Gradio session state belonging to the iframe in *this*
 * document, so "the page's current settings" means this page's. A refresh,
 * a closed tab or a second browser cannot lose or duplicate work, because
 * nothing here holds a queue: this page is a pump that asks the server "is
 * it my turn, and what do I run", runs it against the live page, and
 * reports back. The server is also asked to hold bytes behind a token and
 * to turn tokens into the opaque handoffs the bridge reads.
 *
 * Protocol 4: a request may say start: "auto" (the default - generating as
 * soon as WanGP can) or "never" (stage the task only); the bridge decides
 * inside WanGP, from WanGP's own flag, and the result says "started" or
 * "queued". A press while WanGP is not running is refused, not stored.
 *
 * Protocol 5: a job may be *enhanced* first - its prompt rewritten by the
 * ModelSwitchRefiner extension's MiniMax H3 writer for the model the page is
 * on - and waits in the line as "enhancing" until that prompt exists; the
 * whole line can be cancelled at once; and once WanGP has a job, the page
 * that queued it keeps asking the bridge where its task is (waiting,
 * generating, gone) and tells the server, so the list shows each job's life
 * in WanGP and not only its admission. A job composed for one model is
 * refused, untouched, when the page has moved to another.
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
    const ENHANCE_ROUTE = "/minipaint-interop/enhance";
    const OUTBOX_ROUTE = "/minipaint-interop/outbox";
    const OUTBOX_SUBMIT_ROUTE = OUTBOX_ROUTE + "/submit";
    const OUTBOX_CLAIM_ROUTE = OUTBOX_ROUTE + "/claim";
    const OUTBOX_REPORT_ROUTE = OUTBOX_ROUTE + "/report";
    const OUTBOX_CANCEL_ROUTE = OUTBOX_ROUTE + "/cancel";
    const OUTBOX_CANCEL_ALL_ROUTE = OUTBOX_ROUTE + "/cancel_all";
    const OUTBOX_RETRY_ROUTE = OUTBOX_ROUTE + "/retry";
    const OUTBOX_ADOPT_ROUTE = OUTBOX_ROUTE + "/adopt";
    const OUTBOX_TRACK_ROUTE = OUTBOX_ROUTE + "/track";
    const EVENTS_ROUTE = "/minipaint-interop/events";
    const SYNC_ROUTE = "/minipaint-interop/sync";
    const HEX32 = /^[0-9a-f]{32}$/;
    const CODE_RE = /^[A-Z][A-Z0-9_]{2,59}$/;
    const PROMPT_MAX_CHARS = 12000;
    const MAX_REFERENCES = 16;
    const KINDS = ["staged", "clipboard_asset"];
    const FIELDS = ["prompt", "start", "end", "references"];
    const START_MODES = ["auto", "never"];
    const ROUTES = ["generate", "queue"];
    const TRACK_STATES = ["waiting", "generating", "finished", "unknown"];
    const WANGP_OPEN = ["accepted", "waiting", "generating"];
    const STAGE_MAX_BYTES = 32 * 1024 * 1024;
    // How long enqueue() waits for its job to end before answering "pending"
    // with the job id; how long a pump keeps asking while the line is held
    // up ahead of it (an enhancement can take minutes, and several queue);
    // how often it says so; how often a queued job's task is looked for in
    // WanGP, and for how long at most.
    const ENQUEUE_WAIT_MS = 5 * 60 * 1000;
    const PUMP_MAX_WAIT_MS = 4 * 60 * 60 * 1000;
    const WAITING_EVENT_MS = 3000;
    const TRACK_MS = 3000;
    const TRACK_MAX_MS = 6 * 60 * 60 * 1000;
    const PAGE_KEY = "minipaint.interop.page";
    // The event spine. A page that is told what changed makes no request at
    // all while nothing does, which is what lets the timers above be removed
    // for server-executed jobs rather than merely lengthened - the mean
    // latency of a poll is half its interval, and lengthening one trades
    // requests for staleness in both directions.
    //
    // STREAM_DEAD_MS is deliberately more than twice the server's heartbeat:
    // a watchdog that fires on one missed frame turns an ordinary scheduling
    // hiccup into a reconnect storm.
    const STREAM_DEAD_MS = 40000;
    const STREAM_RETRY_MS = 2000;
    const STREAM_RETRY_MAX_MS = 30000;
    // Jobs the server runs. A page neither claims nor tracks one of these:
    // it watches, and what it is watching continues whether or not it does.
    const SERVER_STATES = ["admitted", "waiting_turn", "enhanced", "ensuring_wangp", "composing",
        "waiting_for_card", "submitting_wangp", "wangp_waiting", "wangp_generating"];
    const SERVER_TERMINAL = ["completed", "failed", "cancelled", "execution_unknown"];
    //: The answers that mean "this worked". A browser-executed job could
    //: only ever reach the first two; a server-executed one ends on the
    //: third, because it is followed all the way to a generated file.
    const POSITIVE = ["queued", "started", "completed"];

    // The sentences a caller may show. The server's errors.py owns the
    // wording; these are the ones this side needs before it can ask.
    const MESSAGES = {
        IFRAME_NOT_READY: "WanGP is not available in this page.",
        BRIDGE_COMPONENT_INCOMPATIBLE: "This WanGP bridge needs to be reinstalled or updated.",
        REQUEST_INVALID: "That queue request is not one this extension can carry.",
        REQUEST_ID_CONFLICT: "That request id was already used for a different request.",
        PROMPT_TOO_LONG: "The prompt is longer than WanGP queue requests allow (12000 characters).",
        IMAGE_STAGE_INVALID: "That image could not be staged for WanGP.",
        IMAGE_STAGE_EXPIRED: "The staged image is no longer there; stage it again.",
        HANDOFF_TOO_LARGE: "The image is too large for the WanGP handoff.",
        QUEUE_BUSY: "WanGP is still taking the previous queue request; try again in a moment.",
        QUEUE_REQUEST_REFUSED: "WanGP did not take the queue request.",
        ADMISSION_UNCONFIRMED: "WanGP did not confirm that the request was added to the queue.",
        WANGP_VALIDATION_REFUSED: "WanGP declined the queue request; check the WanGP page for details.",
        WANGP_NOT_RUNNING: "WanGP is not running. Open the WanGP tab and start it before adding to its queue.",
        WANGP_RESTARTED: "WanGP restarted while the request was on its way.",
        QUEUE_JOB_PENDING: "The request is waiting its turn in the queue outbox.",
        QUEUE_JOB_UNKNOWN: "That queue job is no longer in the outbox.",
        ENHANCE_UNAVAILABLE: "Prompt enhancement is not available: ModelSwitchRefiner's LLM Studio is not installed, is switched off, or has no model set up.",
        ENHANCE_MODEL_UNSUPPORTED: "Enhanced prompts need a MiniMax H3 model (FL2VA or Ref2VA) loaded in WanGP; the WanGP page is on another model.",
        ENHANCE_PROMPT_REQUIRED: "Enhanced mode needs a prompt typed in Clipboard; the WanGP page's own prompt cannot be enhanced from here.",
        ENHANCE_NO_VISION: "The language model running in LLM Studio cannot see pictures, so a request with an image cannot be enhanced.",
        ENHANCE_IMAGE_UNREADABLE: "One of the pictures could not be read for the enhancement.",
        ENHANCE_QUEUE_FULL: "LLM Studio's request queue is full; try again in a moment.",
        ENHANCE_REFUSED: "LLM Studio refused the enhancement request.",
        ENHANCE_FAILED: "The prompt enhancement failed.",
        ENHANCE_CANCELLED: "The prompt enhancement was cancelled.",
        ENHANCE_LOST: "The enhancement's record was gone before its result was collected; retry to enhance again.",
        MODEL_CHANGED: "The WanGP page moved to another model after the prompt was enhanced for it; retry to enhance it for the current model.",
        EXECUTION_UNKNOWN: "Whether WanGP ran this generation could not be proved, so it was not sent again. Check WanGP's outputs and retry if it did not run.",
        CONTROL_UNAVAILABLE: "The MiniPaint bridge inside WanGP is not answering, so unattended jobs cannot be run.",
        SERVICE_UNAVAILABLE: "This WanGP build does not expose the generation service the unattended queue submits through.",
        COMPOSE_UNAVAILABLE: "WanGP's settings for that model could not be read, so nothing was queued at settings nobody chose.",
        MODEL_UNAVAILABLE: "The model this job was composed for is not available in WanGP any more.",
        JOB_INPUT_MISSING: "An image this job owns is no longer on disk.",
        AUTH_BOUNDARY_FAILED: "Sign in to Forge first.",
        INTERNAL_ERROR: "The WanGP integration hit an unexpected problem."
    };
    const ENHANCING_MESSAGE = "The prompt is being enhanced before it is queued.";

    /** What the server says this job is waiting for, if it said anything. */
    function stageOf(job) {
        const text = job && typeof job.stage === "string" ? job.stage : "";
        return text || "The request is waiting its turn in the queue outbox.";
    }

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
        if (raw.start !== undefined && raw.start !== null && raw.start !== "") {
            if (START_MODES.indexOf(raw.start) === -1) { return refusal("REQUEST_INVALID", requestId, "start must be \"auto\" or \"never\"."); }
            request.start = raw.start;
        } else {
            request.start = "auto";
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
    /* Results                                                               */
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
        return { type: String(raw.type || "").slice(0, 120), label: String(raw.label || "").slice(0, 120), family: String(raw.family || "").slice(0, 120),
                 architecture: String(raw.architecture || "").slice(0, 120) };
    }

    /** Where a queued job's task is in WanGP, as the server last heard. */
    function wangpOf(job) {
        const raw = job && job.wangp && typeof job.wangp === "object" ? job.wangp : null;
        if (!raw) { return null; }
        return { state: String(raw.state || "accepted"), position: Number.isFinite(raw.position) ? raw.position : null,
                 queue_depth: Number.isFinite(raw.queue_depth) ? raw.queue_depth : null };
    }

    /** The public result of section 11.5: statuses, codes, counts and the
     * model - never a prompt, a filename or a path. */
    function publicResult(result, requestId, jobId, job) {
        const three = summary(result);
        const base = { request_id: requestId || "", job_id: jobId || "" };
        if (job && job.enhance_requested) { base.enhanced = !!(job.enhance && job.enhance.state === "done"); }
        // "queued" and "started" are as far as a browser-executed job ever
        // got - WanGP had taken it, and that was all a page could see.
        // "completed" is the one a server-executed job ends on, and it means
        // the generation finished: the count of what it produced comes with
        // it, and the paths do not.
        if (result && result.ok && POSITIVE.indexOf(result.status) !== -1) {
            return Object.assign(base, {
                ok: true, status: result.status, tasks_added: Math.max(1, Math.trunc(result.tasks_added || 1)),
                queue_depth: Number.isFinite(result.queue_depth) && result.queue_depth >= 0 ? Math.trunc(result.queue_depth) : null,
                route: ROUTES.indexOf(result.route) === -1 ? "" : result.route,
                model: model(result), applied: three.applied, inherited: three.inherited, ignored: three.ignored,
                wangp: wangpOf(job),
                generated_count: job && Number.isFinite(job.generated_count) ? job.generated_count : null
            });
        }
        const status = result && result.status === "unconfirmed" ? "unconfirmed" : result && result.status === "pending" ? "pending" : "refused";
        const why = code(result && result.code) || (status === "unconfirmed" ? "ADMISSION_UNCONFIRMED" : status === "pending" ? "QUEUE_JOB_PENDING" : "QUEUE_REQUEST_REFUSED");
        return Object.assign(base, { ok: false, status: status, code: why, message: (result && result.message) || sentence(why) });
    }

    /** A job as the server holds it, as the public result a caller sees. */
    function resultOfJob(job) {
        if (!job || typeof job !== "object") { return publicResult({ ok: false, code: "QUEUE_JOB_UNKNOWN" }, "", ""); }
        const requestId = job.request && job.request.request_id ? job.request.request_id : "";
        if (job.state === "queued" || job.state === "started") {
            return publicResult(Object.assign({ ok: true }, job.result || {}, { status: job.state }), requestId, job.job_id, job);
        }
        // A server-executed job ends when the generation does, not when
        // WanGP accepts it: "completed" is the success a caller waits for.
        if (job.state === "completed") {
            return publicResult(Object.assign({ ok: true }, job.result || {}, { status: "completed" }), requestId, job.job_id, job);
        }
        if (job.state === "enhancing") {
            return publicResult({ ok: false, status: "pending", code: "QUEUE_JOB_PENDING", message: ENHANCING_MESSAGE }, requestId, job.job_id, job);
        }
        // Every stage before the generation is *pending*, not refused. Each
        // of these is a job that is going to run; falling through to the
        // refusal below would report a queued job as a failed one, which is
        // both wrong and the kind of wrong a caller acts on.
        if (job.state === "pending" || job.state === "sending" || SERVER_STATES.indexOf(String(job.state)) !== -1) {
            return publicResult({ ok: false, status: "pending", code: "QUEUE_JOB_PENDING", message: stageOf(job) }, requestId, job.job_id, job);
        }
        // "Whether WanGP ran this could not be proved" is its own answer and
        // is never a refusal: a caller that treats it as one retries, and a
        // retry is the second generation the whole design refuses to make.
        if (job.state === "execution_unknown") {
            const unknown = job.error || {};
            return publicResult({ ok: false, status: "unconfirmed", code: unknown.code || "EXECUTION_UNKNOWN",
                                  message: unknown.message || sentence("EXECUTION_UNKNOWN") }, requestId, job.job_id, job);
        }
        if (job.state === "cancelled") {
            const error = job.error || {};
            return publicResult({ ok: false, status: "refused", code: error.code || "QUEUE_REQUEST_REFUSED",
                                  message: error.message || "The request was cancelled before it was sent." }, requestId, job.job_id, job);
        }
        const error = job.error || {};
        return publicResult({ ok: false, status: job.state === "unconfirmed" ? "unconfirmed" : "refused", code: error.code, message: error.message }, requestId, job.job_id, job);
    }

    /* ------------------------------------------------------------------ */
    /* Running one job against the live page                                 */
    /* ------------------------------------------------------------------ */

    async function execute(request, hooks, job) {
        const api = bridge();
        if (!api) { return refusal("IFRAME_NOT_READY", request.request_id); }
        const state = api.state();
        if (!state.present) { return refusal("IFRAME_NOT_READY", request.request_id); }
        if (state.ready && !state.queue) { return refusal("BRIDGE_COMPONENT_INCOMPATIBLE", request.request_id); }

        let wire = { request_id: request.request_id, start: request.start || "auto" };
        if (request.prompt !== undefined) { wire.prompt = request.prompt; }
        // A job composed for one model - an enhanced prompt is written for
        // one - insists on it: the bridge refuses with MODEL_CHANGED when the
        // page has moved. Refused here first when this side already knows.
        const composedFor = job && job.model && typeof job.model.type === "string" ? job.model.type : "";
        if (composedFor && job.enhance_requested) {
            const live = state.model && typeof state.model.type === "string" ? state.model.type : "";
            if (live && live !== composedFor) { return refusal("MODEL_CHANGED", request.request_id); }
            wire.model_type = composedFor;
        }
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
        note("run " + request.request_id.slice(0, 8) + ": " + (FIELDS.filter(function (field) {
            return field === "prompt" ? wire.prompt !== undefined : field === "references" ? !!wire.reference_handoff_ids : !!wire[field + "_handoff_id"];
        }).join(", ") || "no overrides") + "; start " + wire.start + (handoffs.length ? " (" + handoffs.length + " image(s) prepared)" : "")
            + (wire.model_type ? "; for model " + wire.model_type : ""));

        let result;
        try {
            result = await api.queueAndConfirm(wire, { onAdmitted: hooks && hooks.onAdmitted });
        } catch (error) {
            result = { ok: false, status: "refused", code: "INTERNAL_ERROR" };
        } finally {
            if (handoffs.length) {
                // The bridge has decoded the pictures into WanGP's own values;
                // the files have done their job whichever way this ended.
                post(RELEASE_ROUTE, JSON.stringify({ handoff_ids: handoffs })).catch(function () { /* the sweep will */ });
            }
        }
        return publicResult(result, request.request_id, job ? job.job_id : "", job);
    }

    /* ------------------------------------------------------------------ */
    /* The outbox: the server owns the line; this page pumps its own jobs    */
    /* ------------------------------------------------------------------ */

    let pageToken = "";

    /** This page's identity for the outbox: kept per browser tab, so a reload
     * resumes the jobs it composed and a second tab is a second page. */
    function pageId() {
        if (pageToken) { return pageToken; }
        try { pageToken = String(window.sessionStorage.getItem(PAGE_KEY) || ""); } catch (e) { pageToken = ""; }
        if (!HEX32.test(pageToken)) {
            pageToken = hex32();
            try { window.sessionStorage.setItem(PAGE_KEY, pageToken); } catch (e) { /* this tab's memory only */ }
        }
        return pageToken;
    }

    const waiters = {};
    let pumping = false;
    let pumpAgain = false;

    function emit(kind, job, extra) {
        try {
            document.dispatchEvent(new CustomEvent("minipaint:outbox", { detail: Object.assign({ kind: kind, job: job || null, page: pageId() }, extra || {}) }));
        } catch (e) { /* a listener is never worth an exception */ }
    }

    function settleWaiters(job) {
        const list = waiters[job.job_id];
        if (!list) { return; }
        delete waiters[job.job_id];
        const result = resultOfJob(job);
        for (const resolve of list) { try { resolve(result); } catch (e) { /* the caller's */ } }
    }

    async function runJob(job, lease) {
        emit("sending", job);
        const result = await execute(job.request, {
            onAdmitted: function () {
                return post(OUTBOX_REPORT_ROUTE, JSON.stringify({ job_id: job.job_id, lease: lease, phase: "sent" }));
            }
        }, job);
        let reported = null;
        try { reported = await post(OUTBOX_REPORT_ROUTE, JSON.stringify({ job_id: job.job_id, lease: lease, phase: "done", result: result })); } catch (e) { reported = null; }
        const settled = reported && reported.ok && reported.job ? reported.job : Object.assign({}, job, {
            state: result.ok ? result.status : (result.status === "unconfirmed" ? "unconfirmed" : "failed"),
            result: result, error: result.ok ? null : { code: result.code, message: result.message }
        });
        settleWaiters(settled);
        emit("done", settled);
        if (settled.state === "queued" || settled.state === "started") { startTracking(settled); }
        return settled;
    }

    /** Ask the server for this page's next job until there is none, one at a
     * time. Bounded: it stops when the outbox has nothing of this page's,
     * when WanGP is not running, or after a long wait for the line ahead -
     * another page's turn, or a prompt still being written - and it never
     * polls with nothing to do. */
    async function pump() {
        if (pumping) { pumpAgain = true; return; }
        pumping = true;
        try {
            let waitingSince = 0;
            let lastSaid = 0;
            while (true) {
                pumpAgain = false;
                let answer;
                try { answer = await post(OUTBOX_CLAIM_ROUTE, JSON.stringify({ page: pageId() })); } catch (e) { answer = { ok: false, code: "INTERNAL_ERROR" }; }
                if (!answer.ok) { note("pump: stopped - " + (code(answer.code) || "INTERNAL_ERROR")); emit("stopped", null, { code: code(answer.code) }); break; }
                if (answer.job && serverRun(answer.job)) {
                    // Cannot happen - the outbox does not offer these - and
                    // is checked anyway, because running one here would mean
                    // two things driving one job.
                    note("pump: the server owns job " + String(answer.job.job_id).slice(0, 8) + "; not running it here");
                    break;
                }
                if (answer.job) { waitingSince = 0; await runJob(answer.job, answer.lease); continue; }
                if (answer.wait && answer.pending > 0) {
                    const now = Date.now();
                    if (!waitingSince) { waitingSince = now; }
                    if (now - waitingSince > PUMP_MAX_WAIT_MS) { note("pump: the line ahead has not moved for hours; this page resumes on its next press"); break; }
                    if (now - lastSaid >= WAITING_EVENT_MS) {
                        lastSaid = now;
                        emit("waiting", null, { reason: String(answer.reason || ""), pending: answer.pending, head: String(answer.job_id || "") });
                    }
                    await pause(answer.wait);
                    continue;
                }
                break;
            }
        } finally {
            pumping = false;
            if (pumpAgain) { pumpAgain = false; setTimeout(pump, 0); }
        }
    }

    function pause(ms) {
        return new Promise(function (resolve) { setTimeout(resolve, ms); });
    }

    function awaitJob(jobId, timeoutMs) {
        return new Promise(function (resolve) {
            (waiters[jobId] = waiters[jobId] || []).push(resolve);
            if (!(timeoutMs > 0)) { return; }
            setTimeout(function () {
                const list = waiters[jobId];
                if (!list) { return; }
                const index = list.indexOf(resolve);
                if (index !== -1) { list.splice(index, 1); }
                if (!list.length) { delete waiters[jobId]; }
                jobs().then(function (answer) {
                    const found = (answer && answer.jobs || []).filter(function (item) { return item.job_id === jobId; })[0];
                    resolve(resultOfJob(found || null));
                }, function () {
                    resolve(publicResult({ ok: false, status: "pending", code: "QUEUE_JOB_PENDING" }, "", jobId));
                });
            }, timeoutMs);
        });
    }

    /** The WanGP model this page is on, as the bridge last described it. */
    function liveModel() {
        const api = bridge();
        if (!api) { return null; }
        try {
            const state = api.state();
            return state && state.model && typeof state.model === "object" ? model({ model: state.model }) : null;
        } catch (e) { return null; }
    }

    /**
     * Hand WanGP's live settings to a job before the server composes it.
     *
     * The job's base is the form Wan2GP recorded for the model, and that is
     * only written when the user *commits* the form - Generate, Add to Queue
     * inside WanGP, applying a LoRA set, switching model. A weight dragged
     * and then left alone lives in the browser and nowhere else, so a press
     * from this tab would quietly compose at the previous value.
     *
     * The only place those values exist is the page, so this is the only
     * place the gap can be closed - and it is closed by asking WanGP to
     * commit its own form rather than by reading it. Awaited before the
     * submission because the server composes from the record, so the record
     * has to be current first.
     *
     * Never load-bearing, in any of its outcomes: no WanGP tab, a bridge that
     * does not offer it, a page mid settings-load, or a wait that runs out
     * all mean the job composes from the recorded form, which is what it did
     * before flushing existed.
     */
    async function flushSettings() {
        const bridge = window.minipaintWanGP;
        // The legacy path pays nothing for this. A browser-executed job is
        // run by driving the live form and pressing WanGP's own Add to
        // Queue, whose chain commits the form itself - so flushing first
        // would buy a wait and nothing else. Unknown means flush: the
        // unattended queue is the default, and latency is the cheaper wrong
        // guess of the two.
        if (stream.unattended === false) { return ""; }
        if (!bridge || typeof bridge.flushForm !== "function") { return "unavailable"; }
        try {
            const answer = await bridge.flushForm();
            return (answer && answer.flush) || "unavailable";
        } catch (e) {
            return "unavailable";
        }
    }

    /**
     * Add the live WanGP page - with these overrides, if any - to its queue.
     * The request becomes a job in the server's outbox at once; this page
     * runs it when the server says it is its turn; the promise resolves when
     * the job has ended: ``started`` (this request is generating now),
     * ``queued`` (in WanGP's queue), ``refused`` with a code, or
     * ``unconfirmed``. Pass {wait: false} to get the job id back at once,
     * or {timeoutMs} to bound the wait; a wait that runs out answers
     * ``pending`` with the job id, never a guess.
     *
     * {enhance: true|false} asks for, or declines, the MiniMax H3 rewrite
     * of the prompt before it is queued; left out, the Clipboard tab's
     * switch decides. The model the page is on travels with the request so
     * the server can choose the H3 variant; pass {model} to say it yourself.
     */
    async function enqueue(request, options) {
        const normalised = normaliseRequest(request);
        if (!normalised.ok) { return normalised; }
        const wait = !(options && options.wait === false);
        const timeoutMs = options && Number.isFinite(options.timeoutMs) ? options.timeoutMs : ENQUEUE_WAIT_MS;
        const body = { request: normalised.request, page: pageId(), origin: "api" };
        if (options && typeof options.enhance === "boolean") { body.enhance = options.enhance; }
        const known = options && options.model && typeof options.model === "object" ? model({ model: options.model }) : liveModel();
        if (known) { body.model = known; }
        // Before the submission, not after: the server composes from the
        // recorded form and the whole point is that it be current when it
        // does. ``false`` is how a caller that has already flushed - or one
        // that means to compose against the recorded form deliberately -
        // opts out.
        body.settings_flush = (options && options.flush === false) ? "" : await flushSettings();
        return post(OUTBOX_SUBMIT_ROUTE, JSON.stringify(body)).then(function (answer) {
            if (!answer.ok || !answer.job) { return refusal(code(answer.code) || "REQUEST_INVALID", normalised.request.request_id, answer.message); }
            const job = answer.job;
            note("enqueue " + job.job_id.slice(0, 8) + ": submitted (start " + (normalised.request.start || "auto") + (job.state === "enhancing" ? ", enhancing" : "") + ")");
            const promised = wait ? awaitJob(job.job_id, timeoutMs) : Promise.resolve(resultOfJob(job));
            emit("submitted", job);
            if (serverRun(job)) {
                // Admitted. From here the server owns it, and this page may
                // be closed, frozen, discarded or thrown in a river without
                // the job noticing. All this does is watch.
                openStream();
            } else {
                setTimeout(pump, 0);
            }
            return promised;
        }, function () {
            return refusal("INTERNAL_ERROR", normalised.request.request_id, "The queue outbox could not be reached.");
        });
    }

    /** Every job the server holds, with whether WanGP is running. */
    async function jobs() {
        try {
            const response = await fetch(OUTBOX_ROUTE, { credentials: "same-origin", cache: "no-store" });
            const payload = await response.json();
            return payload && typeof payload === "object" ? payload : { ok: false, jobs: [] };
        } catch (e) {
            return { ok: false, jobs: [], code: "INTERNAL_ERROR" };
        }
    }

    function cancel(jobId) {
        return post(OUTBOX_CANCEL_ROUTE, JSON.stringify({ job_id: String(jobId || "") })).then(function (answer) {
            if (answer.ok && answer.job) { settleWaiters(answer.job); emit("changed", answer.job); }
            return answer;
        });
    }

    /** Everything still waiting - enhancing or pending, whichever page
     * pressed it - cancelled at once. A job already being sent is left to
     * finish and counted in the answer as in_flight. */
    function cancelAll() {
        return post(OUTBOX_CANCEL_ALL_ROUTE, JSON.stringify({ page: pageId() })).then(function (answer) {
            if (answer.ok) {
                for (const job of Array.isArray(answer.jobs) ? answer.jobs : []) { settleWaiters(job); }
                note("cancel all: " + (answer.cancelled || 0) + " cancelled, " + (answer.in_flight || 0) + " in flight");
                emit("changed", null, { cancelled: answer.cancelled || 0, in_flight: answer.in_flight || 0 });
            }
            return answer;
        });
    }

    /** Callers waiting on jobs the server has since settled - cancelled
     * from the tab, say - get their answers. */
    async function refreshWaiters() {
        const pendingIds = Object.keys(waiters);
        if (!pendingIds.length) { return 0; }
        const answer = await jobs();
        let settled = 0;
        for (const job of (answer && answer.jobs) || []) {
            if (waiters[job.job_id] && ["queued", "started", "failed", "unconfirmed", "cancelled"].indexOf(job.state) !== -1) { settleWaiters(job); settled += 1; }
        }
        return settled;
    }

    function retry(jobId) {
        return post(OUTBOX_RETRY_ROUTE, JSON.stringify({ job_id: String(jobId || ""), page: pageId() })).then(function (answer) {
            if (answer.ok && answer.job) { emit("submitted", answer.job); setTimeout(pump, 0); }
            return answer;
        });
    }

    function adopt(jobId) {
        return post(OUTBOX_ADOPT_ROUTE, JSON.stringify({ job_id: String(jobId || ""), page: pageId() })).then(function (answer) {
            if (answer.ok && answer.job) { emit("changed", answer.job); setTimeout(pump, 0); }
            return answer;
        });
    }

    /* ------------------------------------------------------------------ */
    /* Tracking: where this page's queued tasks are in WanGP                 */
    /* ------------------------------------------------------------------ */

    const tracked = new Map();
    let trackTimer = 0;
    let tracking = false;

    /** Follow a job WanGP took, until its task has left WanGP's queue or the
     * page can no longer see it. Only this page's own jobs: the bridge
     * answers for the session that admitted them. */
    function startTracking(job) {
        if (!job || !job.job_id || !job.request || !HEX32.test(String(job.request.request_id || ""))) { return false; }
        // A server-executed job is followed by the server, which owns the
        // WanGP side of it and publishes what it sees once for every page.
        // Asking the bridge where it is would be a second, worse answer -
        // and one that stops the moment this page does.
        if (serverRun(job)) { return false; }
        if (job.page && job.page !== pageId()) { return false; }
        const seen = wangpOf(job);
        if (seen && WANGP_OPEN.indexOf(seen.state) === -1) { return false; }
        if (!tracked.has(job.job_id)) { tracked.set(job.job_id, { request_id: job.request.request_id, since: Date.now() }); }
        scheduleTrack(0);
        return true;
    }

    function scheduleTrack(delay) {
        if (trackTimer || !tracked.size) { return; }
        trackTimer = setTimeout(function () { trackTimer = 0; trackTick(); }, delay);
    }

    async function trackTick() {
        if (tracking || !tracked.size) { return; }
        tracking = true;
        try {
            const api = bridge();
            const now = Date.now();
            for (const [jobId, entry] of Array.from(tracked.entries())) {
                if (now - entry.since > TRACK_MAX_MS) { tracked.delete(jobId); }
            }
            if (!tracked.size) { return; }
            if (!api || typeof api.trackQueue !== "function") { scheduleTrack(TRACK_MS); return; }
            const state = api.state();
            if (!state.ready || !state.track) { scheduleTrack(TRACK_MS); return; }
            const entries = Array.from(tracked.entries()).slice(0, 32);
            const answer = await api.trackQueue(entries.map(function (pair) { return pair[1].request_id; }));
            if (!answer || !answer.ok) { scheduleTrack(TRACK_MS); return; }
            for (const [jobId, entry] of entries) {
                const found = answer.tracked && answer.tracked[entry.request_id];
                if (!found) { continue; }
                let reported = null;
                try {
                    reported = await post(OUTBOX_TRACK_ROUTE, JSON.stringify({ job_id: jobId, page: pageId(), state: found.state, position: found.position, queue_depth: found.queue_depth }));
                } catch (e) { reported = null; }
                if (found.state === "finished" || found.state === "unknown") { tracked.delete(jobId); }
                if (reported && reported.ok && reported.job) {
                    const seen = wangpOf(reported.job);
                    if (seen && WANGP_OPEN.indexOf(seen.state) === -1) { tracked.delete(jobId); }
                    emit("tracked", reported.job);
                } else if (reported && reported.ok === false && (code(reported.code) === "QUEUE_JOB_UNKNOWN" || code(reported.code) === "REQUEST_INVALID")) {
                    // The server no longer holds the job, or it is not this page's to report on: nothing left to follow.
                    tracked.delete(jobId);
                }
            }
            if (tracked.size) { scheduleTrack(TRACK_MS); }
        } finally {
            tracking = false;
        }
    }

    /** After a reload: pick up this page's queued jobs whose tasks were last
     * seen still in WanGP. Bounded by the same clock as a fresh track. */
    async function resumeTracking() {
        const answer = await jobs();
        let count = 0;
        if (((answer && answer.jobs) || []).some(serverRun)) { openStream(); }
        for (const job of (answer && answer.jobs) || []) {
            if (serverRun(job)) { continue; }
            if ((job.state === "queued" || job.state === "started") && job.page === pageId()) {
                const seen = wangpOf(job);
                if (!seen || WANGP_OPEN.indexOf(seen.state) !== -1) {
                    const updated = Number(job.updated || 0) * 1000;
                    if (updated && Date.now() - updated > TRACK_MAX_MS) { continue; }
                    if (startTracking(job)) { count += 1; }
                }
            }
        }
        return count;
    }

    /* ------------------------------------------------------------------ */
    /* The event spine: told, rather than asking                             */
    /* ------------------------------------------------------------------ */

    const stream = {
        source: null, cursor: "", epoch: "", lastFrameAt: 0, watchdog: 0, lifecycle: false,
        retry: STREAM_RETRY_MS, wanted: false, syncing: null, buffered: [], jobs: new Map(),
        // null until a snapshot says. Whether this Forge runs the queue
        // unattended decides whether a press needs to commit WanGP's live
        // form first; see flushSettings.
        unattended: null
    };

    /** Open the one stream this page has, or do nothing if it already has it.
     *
     * One transport per page, shared by everything that wants to know what
     * the server is doing. It is an observer and nothing else: losing it
     * makes this page stale and cannot stop a job, which is exactly why the
     * server-executed path can drop the pump and the tracking timers instead
     * of slowing them down. */
    function openStream() {
        if (stream.source || typeof EventSource !== "function") { return false; }
        stream.wanted = true;
        installLifecycle();
        let url = EVENTS_ROUTE + "?page=" + encodeURIComponent(pageId());
        if (stream.cursor) { url += "&cursor=" + encodeURIComponent(stream.cursor); }
        let source;
        try { source = new EventSource(url, { withCredentials: true }); } catch (e) { return false; }
        stream.source = source;
        stream.lastFrameAt = Date.now();
        source.addEventListener("open", function () { stream.retry = STREAM_RETRY_MS; });
        for (const kind of ["hello", "job", "enhance", "handoff", "runtime", "wangp", "reset", "heartbeat", "claim_ready"]) {
            source.addEventListener(kind, function (event) { onFrame(kind, event); });
        }
        source.addEventListener("error", function () { reopenStream(); });
        armWatchdog();
        return true;
    }

    function closeStream() {
        stream.wanted = false;
        if (stream.watchdog) { clearTimeout(stream.watchdog); stream.watchdog = 0; }
        if (stream.source) { try { stream.source.close(); } catch (e) { /* already gone */ } stream.source = null; }
    }

    function reopenStream() {
        if (stream.source) { try { stream.source.close(); } catch (e) { /* already gone */ } stream.source = null; }
        if (!stream.wanted) { return; }
        const delay = stream.retry;
        stream.retry = Math.min(STREAM_RETRY_MAX_MS, Math.round(stream.retry * 1.8));
        setTimeout(function () { if (stream.wanted) { openStream(); } }, delay);
    }

    /** A cheap local timer against the last frame seen. It makes no request
     * while frames arrive, which is the point: a healthy page that is being
     * told things asks for nothing at all. */
    function armWatchdog() {
        if (stream.watchdog) { clearTimeout(stream.watchdog); }
        stream.watchdog = setTimeout(function () {
            stream.watchdog = 0;
            if (!stream.wanted) { return; }
            if (Date.now() - stream.lastFrameAt < STREAM_DEAD_MS) { armWatchdog(); return; }
            note("stream: no frame for " + Math.round((Date.now() - stream.lastFrameAt) / 1000) + "s; reconnecting");
            sync().then(function () { reopenStream(); }, function () { reopenStream(); });
        }, STREAM_DEAD_MS);
    }

    function onFrame(kind, event) {
        stream.lastFrameAt = Date.now();
        armWatchdog();
        let payload = {};
        try { payload = JSON.parse(event.data || "{}") || {}; } catch (e) { payload = {}; }
        if (event.lastEventId) { stream.cursor = event.lastEventId; }
        if (kind === "hello") {
            if (stream.epoch && payload.server_epoch && payload.server_epoch !== stream.epoch) {
                // A different run of Forge. Nothing this page holds means
                // anything against it, so it takes a snapshot rather than
                // trying to reconcile two epochs.
                stream.cursor = "";
                sync();
            }
            stream.epoch = String(payload.server_epoch || "");
            if (!stream.cursor && payload.cursor) { stream.cursor = String(payload.cursor); }
            return;
        }
        if (kind === "heartbeat") { return; }
        if (kind === "reset") {
            stream.cursor = "";
            sync();
            return;
        }
        if (kind === "claim_ready") { setTimeout(pump, 0); return; }
        if (stream.syncing) { stream.buffered.push({ kind: kind, payload: payload, cursor: stream.cursor }); return; }
        applyFrame(kind, payload);
    }

    function applyFrame(kind, payload) {
        if (kind !== "job" || !payload || !payload.job_id) { emit("server", null, { kind: kind, detail: payload }); return; }
        const known = stream.jobs.get(payload.job_id) || {};
        if (Number(payload.revision || 0) < Number(known.revision || 0)) { return; }
        stream.jobs.set(payload.job_id, payload);
        emit("server", null, { kind: "job", detail: payload });
        if (SERVER_TERMINAL.indexOf(String(payload.state)) !== -1) { settleFromServer(payload.job_id); }
    }

    /** A caller waiting on a server-executed job gets its answer from the
     * authoritative record, not from the event: the event is a description
     * and the snapshot is the truth. */
    function settleFromServer(jobId) {
        if (!waiters[jobId]) { return; }
        jobs().then(function (answer) {
            const found = (answer && answer.jobs || []).filter(function (item) { return item.job_id === jobId; })[0];
            if (found) { settleWaiters(found); emit("done", found); }
        }, function () { /* the next sync will settle it */ });
    }

    /**
     * One authoritative snapshot. What a page does on return, on reconnect,
     * and whenever it has any reason to doubt what it holds.
     *
     * Events that arrive while this is in flight are buffered and applied
     * after it, in order, discarding anything at or below the snapshot's own
     * revision - so a transition that happened during the request is neither
     * lost nor applied twice.
     */
    function sync() {
        if (stream.syncing) { return stream.syncing; }
        stream.buffered = [];
        stream.syncing = fetch(SYNC_ROUTE + "?page=" + encodeURIComponent(pageId()), { credentials: "same-origin", cache: "no-store" })
            .then(function (response) { return response.json(); })
            .then(function (payload) {
                if (!payload || payload.ok !== true) { return payload || { ok: false }; }
                stream.epoch = String(payload.server_epoch || "");
                stream.cursor = String(payload.cursor || "");
                // Read here rather than asked for separately: a press needs
                // to know whether the job it is about to make will be run by
                // the server, and the snapshot already says.
                if (typeof payload.unattended === "boolean") { stream.unattended = payload.unattended; }
                stream.jobs.clear();
                for (const job of Array.isArray(payload.jobs) ? payload.jobs : []) {
                    stream.jobs.set(job.job_id, { job_id: job.job_id, state: job.state, stage: job.stage || "", revision: job.revision || 0 });
                    if (SERVER_TERMINAL.indexOf(String(job.state)) !== -1 && waiters[job.job_id]) { settleWaiters(job); }
                }
                emit("synced", null, { revision: payload.revision, jobs: (payload.jobs || []).length, runtime: payload.runtime || {} });
                return payload;
            }, function () { return { ok: false, code: "INTERNAL_ERROR" }; })
            .then(function (payload) {
                const at = Number((payload && payload.revision) || 0);
                const buffered = stream.buffered;
                stream.buffered = [];
                stream.syncing = null;
                for (const item of buffered) {
                    const revision = Number((item.payload && item.payload.revision) || 0);
                    if (revision && revision <= at) { continue; }
                    applyFrame(item.kind, item.payload);
                }
                return payload;
            });
        return stream.syncing;
    }

    /**
     * The page lifecycle, which server-owned execution makes simple.
     *
     * There used to be a rule that a hidden page holding work had to keep
     * running it - which is a promise the browser does not let anybody keep.
     * Timer throttling, freezing, discard, app switching and network loss are
     * browser policy, and no amount of care here changes any of them. Now no
     * page owns executable work, so the whole lifecycle is about one thing:
     * whether this page's *view* is worth keeping fresh.
     *
     *   hidden    let the transport go if the browser wants it; the job is
     *             unaffected either way, and there is nothing to drain
     *   frozen    abandon it, and mark what we hold as uncertain
     *   visible   reopen, sync once, render the truth
     *   pageshow  the same, including a restore from the back/forward cache
     *   closed    nothing at all is required
     */
    function installLifecycle() {
        if (stream.lifecycle) { return; }
        stream.lifecycle = true;
        try {
            document.addEventListener("visibilitychange", function () {
                if (document.visibilityState === "visible") {
                    if (stream.wanted || stream.jobs.size) { openStream(); sync(); }
                }
            });
            window.addEventListener("pageshow", function (event) {
                if (event && event.persisted) { stream.cursor = ""; }
                if (stream.wanted || stream.jobs.size) { openStream(); sync(); }
            });
            window.addEventListener("freeze", function () { closeStream(); stream.wanted = true; });
            window.addEventListener("resume", function () { if (stream.wanted) { openStream(); sync(); } });
            window.addEventListener("pagehide", function () {
                if (stream.source) { try { stream.source.close(); } catch (e) { /* going away */ } stream.source = null; }
            });
        } catch (e) { /* an older environment without one of these keeps the stream as it is */ }
    }

    /** Whether a job is one the server runs. A page watches these; it never
     * claims one, and it never asks WanGP where one is. */
    function serverRun(job) {
        if (!job) { return false; }
        if (job.executor === "server") { return true; }
        return SERVER_STATES.indexOf(String(job.state)) !== -1;
    }

    /* ------------------------------------------------------------------ */
    /* Capabilities                                                          */
    /* ------------------------------------------------------------------ */

    /**
     * What the live page can take right now. Advisory: a job is judged
     * again, live, when it runs.
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
                start: answer.start === true,
                track: answer.track === true,
                generation_running: typeof answer.generation_running === "boolean" ? answer.generation_running : null,
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

    /** Whether prompts are enhanced, and whether they can be: the tab's
     * switch, the LLM side, the variants and the slot rules. From the server. */
    async function enhance() {
        try {
            const response = await fetch(ENHANCE_ROUTE, { credentials: "same-origin", cache: "no-store" });
            const payload = await response.json();
            return payload && typeof payload === "object" ? payload : { ok: false, code: "INTERNAL_ERROR" };
        } catch (e) {
            return { ok: false, code: "INTERNAL_ERROR", message: sentence("INTERNAL_ERROR") };
        }
    }

    return {
        version: VERSION,
        contract: CONTRACT,
        wangp: {
            capabilities: capabilities,
            enhance: enhance,
            stageImage: stageImage,
            enqueue: enqueue,
            jobs: jobs,
            cancel: cancel,
            cancelAll: cancelAll,
            retry: retry,
            adopt: adopt,
            pump: pump,
            track: startTracking,
            resumeTracking: resumeTracking,
            refreshWaiters: refreshWaiters,
            pageId: pageId,
            // The event spine. ``watch`` opens the one stream this page has;
            // ``sync`` is the authoritative snapshot that repairs a view
            // however badly it drifted, and is what a page does on return.
            watch: openStream,
            unwatch: closeStream,
            sync: sync,
            serverRun: serverRun,
            streamState: function () {
                return {
                    open: !!stream.source, epoch: stream.epoch, cursor: stream.cursor,
                    lastFrameAt: stream.lastFrameAt, jobs: stream.jobs.size
                };
            }
        },
        // The sentence for a code, for a caller that wants the same words.
        message: function (failureCode) { return sentence(failureCode); }
    };
})();
