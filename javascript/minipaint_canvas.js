/**
 * Touch Canvas, browser side.
 *
 * The canvas itself is the WebUI's own ForgeCanvas (the img2img box). This
 * file creates that instance for the Canvas tab when the page loads - the
 * host does the same for its own canvases - and adds what the tab needs on
 * top of it, inside its own container only:
 *
 *   - a crop frame with corner handles and a live size readout,
 *   - one-finger panning in Crop mode and with the Move tool, two-finger
 *     pinch to zoom and pan anywhere (ForgeCanvas pans with the right mouse
 *     button and zooms with the wheel; those still work),
 *   - the mask tool, brush size and aspect chosen in the Gradio panels,
 *   - the waits between writing an image and writing a mask layer, here
 *     and for the handoff into the Inpaint tab's canvas,
 *   - telling an image the canvas echoed from one the user opened,
 *   - focus mode.
 *
 * Every other function is called from a Gradio event on a user action, and
 * every selector starts at this extension's own elements. Nothing here
 * watches the document, clicks the host's tabs at startup, or keeps its own
 * idea of which tab is selected.
 */
window.minipaintCanvas = (function () {
    "use strict";

    const ROOT_ID = "minipaint_canvas_root";
    const TAB_PANEL_ID = "tab_minipaint";
    const FOCUS_CLASS = "minipaint-focus";
    const HANDLES = ["tl", "tr", "bl", "br"];
    const MIN_FRAME = 32;
    const LOAD_TIMEOUT = 8000;

    const S = {
        instance: null,
        uuid: null,
        container: null,
        imageContainer: null,
        drawingCanvas: null,
        baseHeight: 0,
        alpha: 75,
        contrast: false,
        loaded: 0,
        marker: 0,
        serverLoad: false,
        echoValue: null,
        trimAfterDrawing: false,
        mode: "crop",
        tool: "Paint",
        aspect: 0,
        frame: null,
        frameBox: null,
        frameSize: null,
        frameRect: null,
        pointers: new Map(),
        pinch: null,
        pan: null,
        handleDrag: null,
        escapeListener: null
    };

    function tick(ms) {
        return new Promise(function (resolve) { setTimeout(resolve, ms || 0); });
    }

    function clamp(n, lo, hi) {
        return n < lo ? lo : n > hi ? hi : n;
    }

    function root() {
        return document.getElementById(ROOT_ID);
    }

    function hasImage() {
        return !!(S.instance && S.instance.img);
    }

    /* ------------------------------------------------------------------ */
    /* The canvas                                                            */
    /* ------------------------------------------------------------------ */

    /**
     * Create the host's canvas for our container. Called once from the
     * page's load event with the options the Python side collected: the
     * height setting and the Inpaint tab's brush colour, opacity and
     * high-contrast choice, so the mask looks here the way it will there.
     */
    function attach(uuid, options) {
        if (S.instance) { return; }
        // The host declares its class at the top level of a classic script:
        // a global binding, not a window property.
        if (typeof ForgeCanvas !== "function") {
            console.warn("MiniPaint: the WebUI's ForgeCanvas is not on this page; the Canvas tab has no editor.");
            return;
        }
        const container = document.getElementById("container_" + uuid);
        if (!container) { return; }
        options = options || {};
        S.alpha = Number(options.alpha);
        if (!(S.alpha >= 0)) { S.alpha = 75; }
        S.contrast = !!options.contrast;
        const percent = Number(options.heightPercent) || 70;
        S.baseHeight = Math.max(240, Math.round(window.innerHeight * percent / 100));

        // no_upload=false, no_scribbles=false (toggled per mode below), and
        // colour / opacity / softness fixed so the toolbar keeps only what a
        // mask needs; the width is driven by the Gradio slider.
        const instance = new ForgeCanvas(
            uuid,
            false, false, S.contrast, S.baseHeight,
            String(options.color || "#808080"), true,
            Number(options.brushWidth) || 25, true, !!options.consistent,
            S.alpha, true,
            0, true
        );
        S.instance = instance;
        S.uuid = uuid;
        S.container = container;
        S.imageContainer = document.getElementById("imageContainer_" + uuid);
        S.drawingCanvas = document.getElementById("drawingCanvas_" + uuid);

        // The canvas applies the mask opacity to its whole scribble layer on
        // the first stroke; applied now, a layer written from the server
        // looks the same before any stroke. (High contrast sets its own.)
        if (!S.contrast) { S.drawingCanvas.style.opacity = String(S.alpha / 100); }

        hookInstance(instance);
        buildFrame();
        bindGestures();
        onMode(S.mode);
    }

    /**
     * Wrap the few methods whose timing matters. The canvas loads what the
     * server wrote (its textbox's value) the same way as what the user
     * opened, dropped or pasted (anything else); telling them apart is what
     * lets an echo be skipped and an opened picture be taken.
     */
    function hookInstance(i) {
        const bind = i.background_gradio_bind;
        const loadImage = i.loadImage.bind(i);
        i.loadImage = function (base64) {
            S.serverLoad = !!base64 && !!bind.target && base64 === bind.target.value;
            return loadImage(base64);
        };

        // Called at the end of every successful image load, just before the
        // canvas records the first stroke state of the new image.
        const updateBackground = i.updateBackgroundImageData.bind(i);
        i.updateBackgroundImageData = function () {
            updateBackground();
            S.echoValue = S.serverLoad && bind.target ? bind.target.value : null;
            S.loaded += 1;
            setTimeout(function () {
                trimHistory();
                refreshFrame(true);
            }, 0);
        };

        // A mask layer written from the server starts the stroke history,
        // as a layer that was never painted should.
        const loadDrawing = i.loadDrawing.bind(i);
        i.loadDrawing = function (base64) {
            loadDrawing(base64);
            if (base64) { S.trimAfterDrawing = true; } else { trimHistory(); }
        };
        const updateDrawing = i.updateDrawingData.bind(i);
        i.updateDrawingData = function () {
            updateDrawing();
            if (S.trimAfterDrawing) {
                S.trimAfterDrawing = false;
                trimHistory();
            }
        };
    }

    function attached() {
        return !!S.instance;
    }

    /** Note the moment before the server replaces the image. */
    function mark() {
        S.marker = S.loaded;
    }

    /**
     * Resolve once the canvas has loaded an image since mark(), or after a
     * bounded wait. The mask layer can only be drawn onto a canvas that
     * already has its image's size, which is why the server writes them in
     * two steps with this in between.
     */
    async function waitForImage() {
        const deadline = Date.now() + LOAD_TIMEOUT;
        while (S.instance && S.loaded === S.marker && Date.now() < deadline) {
            await tick(40);
        }
    }

    /**
     * The same wait for the Inpaint tab's canvas after a send: its drawing
     * canvas takes the image's size when the image has loaded, and only
     * then can the mask layer be written to it. The instruction is what
     * the server returned, "inpaint:WIDTHxHEIGHT".
     */
    async function waitForHostImage(uuid, instruction) {
        const match = /^inpaint:(\d+)x(\d+)$/.exec(String(instruction || ""));
        const canvas = document.getElementById("drawingCanvas_" + uuid);
        if (!match || !canvas) { return; }
        const width = Number(match[1]);
        const height = Number(match[2]);
        const deadline = Date.now() + LOAD_TIMEOUT;
        while ((canvas.width !== width || canvas.height !== height) && Date.now() < deadline) {
            await tick(40);
        }
    }

    /**
     * The hook on the canvas's image textbox. An echo of an image the server
     * sent goes up as an empty value and the word "echo"; anything else is
     * a picture the user put on the canvas, and goes up whole.
     */
    function canvasInput(bg, state, mode) {
        const echo = !!bg && bg === S.echoValue;
        return [echo ? "" : bg, state, mode, echo ? "echo" : "user"];
    }

    /** A new image or layer starts a fresh stroke history. */
    function trimHistory() {
        const i = S.instance;
        if (!i || !Array.isArray(i.history) || !i.history.length) { return; }
        i.history = [i.history[i.history.length - 1]];
        i.historyIndex = 0;
        if (typeof i.updateUndoRedoButtons === "function") { i.updateUndoRedoButtons(); }
    }

    function fit() {
        const i = S.instance;
        if (!i || !i.img) { return; }
        i.adjustInitialPositionAndScale();
        i.drawImage();
        refreshFrame(true);
    }

    /* ------------------------------------------------------------------ */
    /* Modes and tools                                                       */
    /* ------------------------------------------------------------------ */

    function onMode(mode) {
        if (["crop", "mask", "expand"].indexOf(mode) === -1) { return; }
        S.mode = mode;
        const i = S.instance;
        if (!i) { return; }
        if (mode === "mask") {
            setTool(S.tool);
        } else {
            i.no_scribbles = true;
        }
        refreshFrame(false);
    }

    function setTool(tool) {
        S.tool = tool;
        const i = S.instance;
        if (!i || S.mode !== "mask") { return; }
        if (tool === "Move") {
            i.no_scribbles = true;
            return;
        }
        i.no_scribbles = false;
        i.scribbleAlpha = tool === "Erase" ? 0 : S.alpha;
    }

    function setBrushSize(size) {
        const i = S.instance;
        const n = Number(size);
        if (i && n > 0) { i.scribbleWidth = n; }
    }

    /* ------------------------------------------------------------------ */
    /* Crop frame                                                            */
    /* ------------------------------------------------------------------ */

    function buildFrame() {
        const wrap = document.createElement("div");
        wrap.className = "minipaint-frame";
        wrap.hidden = true;
        const box = document.createElement("div");
        box.className = "minipaint-frame-box";
        const size = document.createElement("div");
        size.className = "minipaint-frame-size";
        box.appendChild(size);
        for (const corner of HANDLES) {
            const handle = document.createElement("div");
            handle.className = "minipaint-frame-handle " + corner;
            handle.dataset.corner = corner;
            box.appendChild(handle);
        }
        wrap.appendChild(box);
        S.imageContainer.appendChild(wrap);
        S.frame = wrap;
        S.frameBox = box;
        S.frameSize = size;

        box.addEventListener("pointerdown", function (event) {
            const handle = event.target && event.target.closest ? event.target.closest(".minipaint-frame-handle") : null;
            if (!handle) { return; }
            event.preventDefault();
            event.stopPropagation();
            try { handle.setPointerCapture(event.pointerId); } catch (e) { /* optional */ }
            S.handleDrag = { corner: handle.dataset.corner, id: event.pointerId };
        });
        box.addEventListener("pointermove", function (event) {
            if (!S.handleDrag || event.pointerId !== S.handleDrag.id) { return; }
            event.preventDefault();
            event.stopPropagation();
            resizeFrame(S.handleDrag.corner, containerPoint(event));
        });
        function endHandle(event) {
            if (!S.handleDrag || event.pointerId !== S.handleDrag.id) { return; }
            S.handleDrag = null;
        }
        box.addEventListener("pointerup", endHandle);
        box.addEventListener("pointercancel", endHandle);

        // The canvas refits its image when its box changes size (the tab
        // opening, focus mode, a rotated tablet); the frame follows, a
        // frame later so the refit has happened.
        if (typeof ResizeObserver === "function") {
            new ResizeObserver(function () {
                requestAnimationFrame(function () { refreshFrame(true); });
            }).observe(S.imageContainer);
        }
    }

    /** Show the frame in Crop mode over an image; reset it to the whole
     * image (at the chosen aspect) when asked, else keep it where it is. */
    function refreshFrame(reset) {
        if (!S.frame) { return; }
        const show = S.mode === "crop" && hasImage();
        S.frame.hidden = !show;
        if (show) { layoutFrame(reset || !S.frameRect); }
        updateReadout();
    }

    function containerSize() {
        return { w: S.imageContainer.clientWidth, h: S.imageContainer.clientHeight };
    }

    function containerPoint(event) {
        const rect = S.container.getBoundingClientRect();
        return { x: event.clientX - rect.left, y: event.clientY - rect.top };
    }

    /** Where the image is on screen, in container pixels. */
    function imageRect() {
        const i = S.instance;
        if (!i || !i.img || !(i.imgScale > 0)) { return null; }
        return { left: i.imgX, top: i.imgY, width: i.orgWidth * i.imgScale, height: i.orgHeight * i.imgScale };
    }

    function layoutFrame(reset) {
        const size = containerSize();
        if (!size.w || !size.h) { return; }
        let rect;
        if (reset || !S.frameRect) {
            // The visible part of the image, or the whole box without one.
            const img = imageRect();
            const base = img
                ? { left: Math.max(0, img.left), top: Math.max(0, img.top),
                    right: Math.min(size.w, img.left + img.width), bottom: Math.min(size.h, img.top + img.height) }
                : { left: 0, top: 0, right: size.w, bottom: size.h };
            let fw = Math.max(MIN_FRAME, base.right - base.left);
            let fh = Math.max(MIN_FRAME, base.bottom - base.top);
            if (S.aspect > 0) {
                if (fw / fh > S.aspect) { fw = fh * S.aspect; } else { fh = fw / S.aspect; }
            }
            rect = { left: (base.left + base.right - fw) / 2, top: (base.top + base.bottom - fh) / 2, width: fw, height: fh };
        } else {
            rect = S.frameRect;
        }
        rect.width = clamp(rect.width, MIN_FRAME, size.w);
        rect.height = clamp(rect.height, MIN_FRAME, size.h);
        rect.left = clamp(rect.left, 0, size.w - rect.width);
        rect.top = clamp(rect.top, 0, size.h - rect.height);
        S.frameRect = rect;
        applyFrame();
    }

    function applyFrame() {
        const r = S.frameRect;
        if (!r || !S.frameBox) { return; }
        S.frameBox.style.left = r.left + "px";
        S.frameBox.style.top = r.top + "px";
        S.frameBox.style.width = r.width + "px";
        S.frameBox.style.height = r.height + "px";
        updateReadout();
    }

    /** Drag one corner; the opposite corner stays put; aspect is kept if set. */
    function resizeFrame(corner, point) {
        const r = S.frameRect;
        const size = containerSize();
        if (!r) { return; }
        const anchorX = corner.indexOf("l") !== -1 ? r.left + r.width : r.left;
        const anchorY = corner.indexOf("t") !== -1 ? r.top + r.height : r.top;
        const px = clamp(point.x, 0, size.w);
        const py = clamp(point.y, 0, size.h);
        let width = Math.abs(px - anchorX);
        let height = Math.abs(py - anchorY);
        if (S.aspect > 0) {
            if (width / S.aspect >= height) { height = width / S.aspect; } else { width = height * S.aspect; }
            const maxWidth = corner.indexOf("l") !== -1 ? anchorX : size.w - anchorX;
            const maxHeight = corner.indexOf("t") !== -1 ? anchorY : size.h - anchorY;
            if (width > maxWidth) { width = maxWidth; height = width / S.aspect; }
            if (height > maxHeight) { height = maxHeight; width = height * S.aspect; }
        }
        width = Math.max(MIN_FRAME, width);
        height = Math.max(MIN_FRAME, height);
        S.frameRect = {
            left: corner.indexOf("l") !== -1 ? anchorX - width : anchorX,
            top: corner.indexOf("t") !== -1 ? anchorY - height : anchorY,
            width: width,
            height: height
        };
        applyFrame();
    }

    function setAspect(choice, width, height, original) {
        let aspect = 0;
        if (choice === "Original" && typeof original === "string" && original.indexOf("x") !== -1) {
            const parts = original.split("x").map(Number);
            if (parts[0] > 0 && parts[1] > 0) { aspect = parts[0] / parts[1]; }
        } else if (choice === "Custom") {
            if (Number(width) > 0 && Number(height) > 0) { aspect = Number(width) / Number(height); }
        } else if (typeof choice === "string" && choice.indexOf(":") !== -1) {
            const parts = choice.split(":").map(Number);
            if (parts[0] > 0 && parts[1] > 0) { aspect = parts[0] / parts[1]; }
        }
        S.aspect = aspect;
        refreshFrame(true);
    }

    /** The frame in image pixels, clamped to the image. Null when it misses. */
    function cropBoxObject() {
        const i = S.instance;
        const r = S.frameRect;
        if (!i || !i.img || !r || !(i.imgScale > 0)) { return null; }
        const s = i.imgScale;
        let x0 = Math.round((r.left - i.imgX) / s);
        let y0 = Math.round((r.top - i.imgY) / s);
        let x1 = Math.round((r.left + r.width - i.imgX) / s);
        let y1 = Math.round((r.top + r.height - i.imgY) / s);
        x0 = clamp(x0, 0, i.orgWidth);
        x1 = clamp(x1, 0, i.orgWidth);
        y0 = clamp(y0, 0, i.orgHeight);
        y1 = clamp(y1, 0, i.orgHeight);
        if (x1 - x0 < 1 || y1 - y0 < 1) { return null; }
        return { x0: x0, y0: y0, x1: x1, y1: y1 };
    }

    function cropBox() {
        const box = cropBoxObject();
        return box ? JSON.stringify(box) : "";
    }

    function updateReadout() {
        if (!S.frameSize) { return; }
        const box = cropBoxObject();
        S.frameSize.textContent = box ? (box.x1 - box.x0) + " × " + (box.y1 - box.y0) : "";
    }

    /* ------------------------------------------------------------------ */
    /* Touch gestures                                                        */
    /* ------------------------------------------------------------------ */

    function canPan() {
        return !!(S.instance && S.instance.no_scribbles);
    }

    /** Throw away a stroke a first finger started before a second arrived. */
    function abortStroke() {
        const i = S.instance;
        if (!i || !i.drawing) { return; }
        i.drawing = false;
        try {
            if (i.temp_draw_bg) { S.drawingCanvas.getContext("2d").putImageData(i.temp_draw_bg, 0, 0); }
        } catch (e) { /* the stroke stays; nothing else is affected */ }
        S.drawingCanvas.style.cursor = "";
    }

    function pinchGeometry() {
        const points = Array.from(S.pointers.values());
        const rect = S.container.getBoundingClientRect();
        const a = points[0];
        const b = points[1];
        return {
            distance: Math.hypot(a.x - b.x, a.y - b.y),
            mid: { x: (a.x + b.x) / 2 - rect.left, y: (a.y + b.y) / 2 - rect.top }
        };
    }

    function startPinch() {
        abortStroke();
        S.pan = null;
        const g = pinchGeometry();
        S.pinch = { d0: Math.max(1, g.distance), m0: g.mid, s0: S.instance.imgScale, x0: S.instance.imgX, y0: S.instance.imgY };
    }

    function updatePinch() {
        if (!S.pinch || S.pointers.size < 2) { return; }
        const i = S.instance;
        const g = pinchGeometry();
        const scale = clamp(S.pinch.s0 * (g.distance / S.pinch.d0), 0.05, 40);
        // The image point that was under the first midpoint stays under the finger midpoint.
        const px = (S.pinch.m0.x - S.pinch.x0) / S.pinch.s0;
        const py = (S.pinch.m0.y - S.pinch.y0) / S.pinch.s0;
        i.imgScale = scale;
        i.imgX = g.mid.x - px * scale;
        i.imgY = g.mid.y - py * scale;
        i.drawImage();
        updateReadout();
    }

    function bindGestures() {
        const c = S.container;

        // Capture phase, so a second finger is seen before the canvas starts
        // a second stroke with it.
        c.addEventListener("pointerdown", function (event) {
            if (!hasImage()) { return; }
            if (event.target && event.target.closest &&
                event.target.closest(".forge-toolbar, .forge-toolbar-static, .minipaint-frame-handle")) { return; }
            if (event.pointerType === "mouse" && event.button !== 0) { return; }
            S.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
            if (S.pointers.size === 2) {
                event.stopPropagation();
                event.preventDefault();
                // Captured, so the finger's release reaches this container
                // even when it is lifted somewhere else on the page.
                try { c.setPointerCapture(event.pointerId); } catch (e) { /* optional */ }
                startPinch();
                return;
            }
            if (S.pointers.size === 1 && canPan()) {
                S.pan = { id: event.pointerId, x: event.clientX, y: event.clientY, imgX: S.instance.imgX, imgY: S.instance.imgY };
                try { c.setPointerCapture(event.pointerId); } catch (e) { /* optional */ }
            }
        }, true);

        c.addEventListener("pointermove", function (event) {
            if (!S.pointers.has(event.pointerId)) { return; }
            S.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
            if (S.pinch) {
                event.preventDefault();
                updatePinch();
                return;
            }
            if (S.pan && S.pan.id === event.pointerId) {
                event.preventDefault();
                S.instance.imgX = S.pan.imgX + (event.clientX - S.pan.x);
                S.instance.imgY = S.pan.imgY + (event.clientY - S.pan.y);
                S.instance.drawImage();
                updateReadout();
            }
        }, true);

        function end(event) {
            S.pointers.delete(event.pointerId);
            if (S.pinch && S.pointers.size < 2) { S.pinch = null; }
            if (S.pan && S.pan.id === event.pointerId) { S.pan = null; }
            try {
                if (c.hasPointerCapture(event.pointerId)) { c.releasePointerCapture(event.pointerId); }
            } catch (e) { /* optional */ }
        }
        c.addEventListener("pointerup", end, true);
        c.addEventListener("pointercancel", end, true);

        // The canvas zooms on the wheel and refits from its own toolbar
        // (centre and reset); the frame and readout follow.
        c.addEventListener("wheel", function () { requestAnimationFrame(updateReadout); }, { passive: true });
        c.addEventListener("click", function (event) {
            const refit = event.target && event.target.closest &&
                event.target.closest('[id^="centerButton_"], [id^="resetButton_"]');
            requestAnimationFrame(function () { if (refit) { refreshFrame(true); } else { updateReadout(); } });
        });
    }

    /** A few numbers for tests and the console. */
    function debug() {
        const i = S.instance;
        if (!i) { return { attached: false, hasImage: false, frameHidden: true, history: 0, forgeCanvas: typeof ForgeCanvas === "function" }; }
        return { attached: true, hasImage: !!i.img, imgX: i.imgX, imgY: i.imgY, imgScale: i.imgScale,
                 orgWidth: i.orgWidth, orgHeight: i.orgHeight, loaded: S.loaded, mode: S.mode, tool: S.tool,
                 noScribbles: !!i.no_scribbles, alpha: i.scribbleAlpha, width: i.scribbleWidth,
                 history: Array.isArray(i.history) ? i.history.length : 0, echo: !!S.echoValue,
                 frameHidden: !S.frame || S.frame.hidden,
                 frame: S.frameRect ? Object.assign({}, S.frameRect) : null, box: cropBoxObject() };
    }

    /* ------------------------------------------------------------------ */
    /* Galleries and tabs                                                    */
    /* ------------------------------------------------------------------ */

    /** The selected gallery item, the way the host's own send buttons pick it. */
    function pickGalleryImage(gallery) {
        if (!Array.isArray(gallery) || gallery.length === 0) { return null; }
        if (typeof window.extract_image_from_gallery === "function") {
            try {
                const picked = window.extract_image_from_gallery(gallery);
                if (Array.isArray(picked) && picked.length) { return picked[0]; }
            } catch (e) { /* fall through */ }
        }
        return [gallery[0]];
    }

    function app() {
        try {
            if (typeof gradioApp === "function") { return gradioApp() || document; }
        } catch (e) { /* fall through */ }
        return document;
    }

    /**
     * Go to a host tab after a handoff, using the host's own helpers for
     * its tabs. For our tab, click its native tab button - the same thing
     * the host's helpers do for theirs - found by the panel it controls.
     */
    function switchTo(target) {
        const helpers = { img2img: "switch_to_img2img", inpaint: "switch_to_inpaint", extras: "switch_to_extras" };
        const name = String(target || "").split(":")[0];
        if (name in helpers) {
            if (typeof window[helpers[name]] === "function") { window[helpers[name]](); }
            return;
        }
        if (name !== "canvas") { return; }
        const nav = app().querySelector("#tabs > .tab-nav");
        if (!nav) { return; }
        let button = nav.querySelector('button[aria-controls="' + TAB_PANEL_ID + '"]');
        if (!button) {
            const panel = app().querySelector("#" + TAB_PANEL_ID);
            const panels = Array.from(app().querySelectorAll("#tabs > .tabitem"));
            const index = panels.indexOf(panel);
            const buttons = nav.querySelectorAll("button");
            if (index >= 0 && buttons[index]) { button = buttons[index]; }
        }
        if (button && !button.classList.contains("selected")) { button.click(); }
    }

    /* ------------------------------------------------------------------ */
    /* Focus mode                                                            */
    /* ------------------------------------------------------------------ */

    function setFocus(on) {
        const element = root();
        if (!element) { return; }
        element.classList.toggle(FOCUS_CLASS, !!on);
        if (S.container && S.instance && !S.instance.maximized) {
            S.container.style.height = (on ? Math.max(240, window.innerHeight - 250) : S.baseHeight) + "px";
        }
        if (on && !S.escapeListener) {
            S.escapeListener = function (event) {
                if (event.key === "Escape") { setFocus(false); }
            };
            document.addEventListener("keydown", S.escapeListener);
        } else if (!on && S.escapeListener) {
            document.removeEventListener("keydown", S.escapeListener);
            S.escapeListener = null;
        }
    }

    return {
        attach: attach,
        attached: attached,
        mark: mark,
        waitForImage: waitForImage,
        waitForHostImage: waitForHostImage,
        canvasInput: canvasInput,
        fit: fit,
        onMode: onMode,
        setTool: setTool,
        setBrushSize: setBrushSize,
        setAspect: setAspect,
        cropBox: cropBox,
        pickGalleryImage: pickGalleryImage,
        switchTo: switchTo,
        setFocus: setFocus,
        debug: debug
    };
})();
