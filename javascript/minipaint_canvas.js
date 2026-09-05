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
 *   - Undo and Redo of strokes, before the server's structural steps,
 *   - a canvas height that fits what the window has left, with the rail of
 *     panels beside it kept no taller than that (it scrolls inside), so
 *     nothing scrolls and nothing floats over the canvas; the Panels
 *     button hides and shows the rail,
 *   - the taps in the layer list (select, add to the selection, show or
 *     hide, reorder), handed to the server as one action each,
 *   - in Layers mode, dragging the selected layers with a live preview and
 *     handing the offset they settled on to the server,
 *   - keeping the zoom and position across a reload that does not change
 *     the picture's size,
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
    const WORK_ID = "minipaint_canvas_work";
    const RAIL_ID = "minipaint_canvas_rail";
    const TAB_PANEL_ID = "tab_minipaint";
    const FOCUS_CLASS = "minipaint-focus";
    const RAIL_HIDDEN_CLASS = "minipaint-rail-hidden";
    const HANDLES = ["tl", "tr", "bl", "br"];
    const MIN_FRAME = 32;
    const LOAD_TIMEOUT = 8000;
    const MIN_HEIGHT = 240;
    const BOTTOM_ROOM = 8;
    const LAYER_MOVE_ID = "minipaint_canvas_layer_move";
    const LAYER_ACTION_ID = "minipaint_canvas_layer_action";
    const LAYER_LIST_ID = "minipaint_canvas_layer_list";
    const LAYER_PREVIEW_ID = "minipaint_canvas_layer_preview";
    const LAYER_UNDERLAY_ID = "minipaint_canvas_layer_underlay";

    const S = {
        instance: null,
        uuid: null,
        container: null,
        imageContainer: null,
        drawingCanvas: null,
        baseHeight: 0,
        fit: true,
        fitTimer: null,
        alpha: 75,
        contrast: false,
        loaded: 0,
        marker: 0,
        keepView: null,
        serverLoad: false,
        echoValue: null,
        trimAfterDrawing: false,
        imageEl: null,
        overlay: null,
        layerDrag: null,
        underlaySrc: "",
        underlaySwapped: false,
        previewCache: { text: null, data: null },
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

    /** The column that holds the action row, the canvas and the status. */
    function work() {
        return document.getElementById(WORK_ID) || root();
    }

    function rail() {
        return document.getElementById(RAIL_ID);
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
        S.fit = options.fit !== false;
        const percent = Number(options.heightPercent) || 70;
        S.baseHeight = Math.max(MIN_HEIGHT, Math.round(window.innerHeight * percent / 100));

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
        S.imageEl = document.getElementById("image_" + uuid);

        // The canvas applies the mask opacity to its whole scribble layer on
        // the first stroke; applied now, a layer written from the server
        // looks the same before any stroke. (High contrast sets its own.)
        if (!S.contrast) { S.drawingCanvas.style.opacity = String(S.alpha / 100); }

        hookInstance(instance);
        buildOverlay();
        buildFrame();
        bindGestures();
        bindLayerList();
        watchLayout();
        onMode(S.mode);
        fitHeight();
    }

    /* ------------------------------------------------------------------ */
    /* Height                                                                */
    /* ------------------------------------------------------------------ */

    /** The nearest ancestor that scrolls, or the document. */
    function scrollParent(element) {
        let node = element.parentElement;
        while (node && node !== document.body && node !== document.documentElement) {
            const overflow = getComputedStyle(node).overflowY;
            if ((overflow === "auto" || overflow === "scroll") && node.scrollHeight > node.clientHeight + 1) { return node; }
            node = node.parentElement;
        }
        return null;
    }

    /**
     * Give the canvas the height the window has left once every other row
     * of its column is accounted for, so the whole tab is in view without
     * scrolling. The rows are measured, not assumed, so a wrapped action
     * row or status line shrinks the canvas rather than pushing the
     * controls off screen. The rail of panels beside the canvas is then
     * capped to the column's height and scrolls inside it; on a window too
     * narrow for both, the rail has wrapped under the canvas and takes its
     * share of the height instead. What the page puts after the tab (a
     * version footer, a theme's own blocks) is left where the page put it.
     */
    function fitHeight() {
        const element = root();
        const column = work();
        if (!element || !column || !S.container || !S.fit) { return; }
        if (S.instance && S.instance.maximized) { return; }
        const surface = S.container.closest("#" + column.id + " > *") || S.container;
        const rect = column.getBoundingClientRect();
        if (!rect.width) { return; }  // the tab is hidden; measured again when it shows

        const scroller = scrollParent(element);
        const viewport = scroller ? scroller.clientHeight : window.innerHeight;
        const scrolled = scroller ? scroller.scrollTop : (window.scrollY || 0);
        const origin = scroller ? scroller.getBoundingClientRect().top : 0;
        const top = rect.top - origin + scrolled;

        let others = 0;
        const style = getComputedStyle(column);
        const gap = parseFloat(style.rowGap || style.gap) || 0;
        let rows = 0;
        for (const child of column.children) {
            if (child === surface) { rows += 1; continue; }
            const height = child.getBoundingClientRect().height;
            if (height > 0) { others += height; rows += 1; }
        }
        const padding = (parseFloat(style.paddingTop) || 0) + (parseFloat(style.paddingBottom) || 0);

        const panels = rail();
        let below = 0;
        if (panels && railBelow(panels, column)) {
            const rootStyle = getComputedStyle(element);
            below = panels.getBoundingClientRect().height + (parseFloat(rootStyle.rowGap || rootStyle.gap) || 0);
        }

        let height = viewport - top - others - below - gap * Math.max(0, rows - 1) - padding - BOTTOM_ROOM;
        height = Math.round(clamp(height, MIN_HEIGHT, Math.max(MIN_HEIGHT, viewport - BOTTOM_ROOM)));
        const current = parseFloat(S.container.style.height) || 0;
        if (Math.abs(height - current) > 1) { S.container.style.height = height + "px"; }
        sizeRail(panels, column);
    }

    /** True when the rail has wrapped under the work column (a narrow window). */
    function railBelow(panels, column) {
        const r = panels.getBoundingClientRect();
        if (!r.width || !r.height) { return false; }  // hidden
        return r.top >= column.getBoundingClientRect().bottom - 1;
    }

    /** Beside the canvas the rail is never taller than the work column; it
     * scrolls inside that. Under the canvas it is as tall as the stylesheet
     * lets it be. */
    function sizeRail(panels, column) {
        if (!panels) { return; }
        if (railBelow(panels, column)) {
            if (panels.style.maxHeight) { panels.style.maxHeight = ""; }
            return;
        }
        const height = Math.round(column.getBoundingClientRect().height);
        if (height > 0 && Math.abs(height - (parseFloat(panels.style.maxHeight) || 0)) > 1) {
            panels.style.maxHeight = height + "px";
        }
    }

    function railHidden() {
        const element = root();
        return !!element && element.classList.contains(RAIL_HIDDEN_CLASS);
    }

    /** Show or hide the rail of panels; the canvas takes the room. */
    function setRail(on) {
        const element = root();
        if (!element) { return; }
        element.classList.toggle(RAIL_HIDDEN_CLASS, !on);
        scheduleFit();
    }

    function toggleRail() {
        setRail(railHidden());
    }

    function scheduleFit() {
        if (S.fitTimer) { return; }
        S.fitTimer = requestAnimationFrame(function () { S.fitTimer = null; fitHeight(); });
    }

    /** The other rows of the column change height when a line wraps; the
     * rail when a panel or accordion opens; the window when it is resized
     * or the tablet turns. The canvas follows. */
    function watchLayout() {
        const column = work();
        if (!column) { return; }
        if (typeof ResizeObserver === "function") {
            const observer = new ResizeObserver(scheduleFit);
            for (const child of column.children) {
                if (!child.contains(S.container)) { observer.observe(child); }
            }
            const panels = rail();
            if (panels) { observer.observe(panels); }
        }
        window.addEventListener("resize", scheduleFit);
        window.addEventListener("orientationchange", scheduleFit);
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
            // The picture about to be drawn is the answer to a dragged layer:
            // stop putting the other layers in its place, keep the preview
            // until it has been drawn.
            S.underlaySwapped = false;
            return loadImage(base64);
        };

        // Called at the end of every successful image load, just before the
        // canvas records the first stroke state of the new image.
        const updateBackground = i.updateBackgroundImageData.bind(i);
        i.updateBackgroundImageData = function () {
            updateBackground();
            const kept = restoreView();
            endLayerDrag(false);
            S.echoValue = S.serverLoad && bind.target ? bind.target.value : null;
            S.loaded += 1;
            setTimeout(function () {
                trimHistory();
                refreshFrame(!kept);
            }, 0);
        };

        // The picture is redrawn on every pan and zoom; a layer preview and
        // the other layers standing in for the picture follow it.
        const drawImage = i.drawImage.bind(i);
        i.drawImage = function () {
            drawImage();
            if (S.underlaySwapped && S.underlaySrc && S.imageEl) { S.imageEl.src = S.underlaySrc; }
            positionOverlay();
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

    /**
     * Note the moment before the server replaces the image. With ``keep``,
     * remember the zoom and position too, to put back if the picture comes
     * back the same size (a layer moved, an undo of one).
     */
    function mark(keep) {
        S.marker = S.loaded;
        const i = S.instance;
        S.keepView = (keep && i && i.img)
            ? { imgX: i.imgX, imgY: i.imgY, imgScale: i.imgScale, w: i.orgWidth, h: i.orgHeight }
            : null;
    }

    function restoreView() {
        const k = S.keepView;
        const i = S.instance;
        S.keepView = null;
        if (!k || !i || !i.img || i.orgWidth !== k.w || i.orgHeight !== k.h) { return false; }
        i.imgX = k.imgX;
        i.imgY = k.imgY;
        i.imgScale = k.imgScale;
        i.drawImage();
        return true;
    }

    /** Write a value into one of this tab's hidden textboxes the way a user
     * would, so the Gradio event bound to it fires. */
    function sendInput(id, text) {
        const target = document.querySelector("#" + id + " textarea");
        if (!target) { return false; }
        target.value = text;
        target.dispatchEvent(new Event("input", { bubbles: true }));
        return true;
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

    /** Take the last stroke back, if there is one. True when it was. */
    function undoStroke() {
        const i = S.instance;
        if (!i || !i.img || !(i.historyIndex > 0)) { return false; }
        i.undo();
        return true;
    }

    function redoStroke() {
        const i = S.instance;
        if (!i || !i.img || !Array.isArray(i.history) || !(i.historyIndex < i.history.length - 1)) { return false; }
        i.redo();
        return true;
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
        if (["crop", "mask", "expand", "layers"].indexOf(mode) === -1) { return; }
        if (S.mode === "layers" && mode !== "layers") { endLayerDrag(true); }
        // A mode's panel is a flyout beside the canvas: choosing a mode
        // brings the rail back if it was put away.
        if (mode !== S.mode && railHidden()) { setRail(true); }
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
                requestAnimationFrame(function () { fitHeight(); refreshFrame(true); });
            }).observe(S.imageContainer);
        }
    }

    /** Show the frame in Crop mode over an image; reset it to the whole
     * image (at the chosen aspect) when asked, else keep it where it is. */
    function refreshFrame(reset) {
        if (!S.frame) { return; }
        const show = (S.mode === "crop" || S.mode === "layers") && hasImage();
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
    /* The layer list                                                        */
    /* ------------------------------------------------------------------ */

    /**
     * The list is HTML the server renders; every button in it says what it
     * does (select, add to or take out of the selection, show or hide, up,
     * down) and for which layer. One listener on the tab's root hands that
     * to the server through a hidden textbox, so the list can be replaced
     * wholesale by the next reply without rebinding anything. Shift, Ctrl
     * or Cmd with a tap on the name adds to the selection instead.
     */
    function bindLayerList() {
        const element = root();
        if (!element || element.dataset.minipaintLayerList) { return; }
        element.dataset.minipaintLayerList = "1";
        element.addEventListener("click", function (event) {
            const button = event.target && event.target.closest ? event.target.closest("#" + LAYER_LIST_ID + " [data-op]") : null;
            if (!button || button.disabled) { return; }
            event.preventDefault();
            let op = button.dataset.op;
            if (op === "pick" && (event.shiftKey || event.ctrlKey || event.metaKey)) { op = "toggle"; }
            sendInput(LAYER_ACTION_ID, JSON.stringify({ op: op, name: button.dataset.name || "", t: Date.now() }));
        });
    }

    /* ------------------------------------------------------------------ */
    /* Dragging a layer                                                      */
    /* ------------------------------------------------------------------ */

    /**
     * In Layers mode the server keeps two hidden textboxes filled: the
     * selected layers as one picture (with offset, size, opacity) and the
     * other layers composited without them. A drag shows the first as an
     * overlay over the second, moves the overlay with the finger, and
     * hands the offset it settled on to the server, which answers with the
     * new composite.
     */
    function buildOverlay() {
        const img = document.createElement("img");
        img.className = "minipaint-layer-preview";
        img.hidden = true;
        img.draggable = false;
        S.imageContainer.appendChild(img);
        S.overlay = img;
    }

    function layerPreview() {
        const target = document.querySelector("#" + LAYER_PREVIEW_ID + " textarea");
        const text = target ? target.value : "";
        if (!text) { return null; }
        if (S.previewCache.text !== text) {
            let data = null;
            try { data = JSON.parse(text); } catch (e) { data = null; }
            S.previewCache = { text: text, data: data };
        }
        return S.previewCache.data;
    }

    function underlaySource() {
        const target = document.querySelector("#" + LAYER_UNDERLAY_ID + " textarea");
        return target ? target.value : "";
    }

    function positionOverlay() {
        const i = S.instance;
        const d = S.layerDrag;
        if (!S.overlay || S.overlay.hidden || !i || !d) { return; }
        const s = i.imgScale;
        S.overlay.style.left = (i.imgX + (d.preview.x + d.dx) * s) + "px";
        S.overlay.style.top = (i.imgY + (d.preview.y + d.dy) * s) + "px";
        S.overlay.style.width = (d.preview.w * s) + "px";
        S.overlay.style.height = (d.preview.h * s) + "px";
    }

    function startLayerDrag(event) {
        const preview = layerPreview();
        if (!preview || !preview.src || !S.overlay || !S.instance || !S.instance.img) { return false; }
        S.layerDrag = { id: event.pointerId, x: event.clientX, y: event.clientY, dx: 0, dy: 0, preview: preview, done: false };
        if (S.overlay.getAttribute("src") !== preview.src) { S.overlay.src = preview.src; }
        S.overlay.style.opacity = String((preview.opacity == null ? 100 : preview.opacity) / 100);
        S.overlay.hidden = false;
        const under = underlaySource();
        if (under && S.imageEl) {
            S.underlaySrc = under;
            S.underlaySwapped = true;
            S.imageEl.src = under;
        }
        positionOverlay();
        try { S.container.setPointerCapture(event.pointerId); } catch (e) { /* optional */ }
        return true;
    }

    function moveLayerDrag(event) {
        const d = S.layerDrag;
        const s = S.instance.imgScale;
        d.dx = (event.clientX - d.x) / s;
        d.dy = (event.clientY - d.y) / s;
        positionOverlay();
    }

    function finishLayerDrag() {
        const d = S.layerDrag;
        if (!d || d.done) { return; }
        const dx = Math.round(d.dx);
        const dy = Math.round(d.dy);
        if (!dx && !dy) {
            endLayerDrag(true);
            return;
        }
        d.dx = dx;
        d.dy = dy;
        d.done = true;
        positionOverlay();
        // The preview stays where the finger left it until the server's
        // composite has been drawn. The time stamp makes a repeated offset
        // still count as a change.
        if (!sendInput(LAYER_MOVE_ID, JSON.stringify({ dx: dx, dy: dy, t: Date.now() }))) { endLayerDrag(true); }
    }

    /** Put the canvas back the way it was (or leave it, when the server's
     * new picture is what is being drawn). */
    function endLayerDrag(redraw) {
        if (!S.layerDrag && !S.underlaySwapped && (!S.overlay || S.overlay.hidden)) { return; }
        S.layerDrag = null;
        if (S.overlay) { S.overlay.hidden = true; }
        const swapped = S.underlaySwapped;
        S.underlaySwapped = false;
        if (redraw && swapped && S.instance) { S.instance.drawImage(); }
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
                if (S.layerDrag && !S.layerDrag.done) { endLayerDrag(true); }
                startPinch();
                return;
            }
            if (S.pointers.size === 1 && S.mode === "layers" && !S.layerDrag) {
                if (startLayerDrag(event)) { event.preventDefault(); return; }
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
            if (S.layerDrag && !S.layerDrag.done && S.layerDrag.id === event.pointerId) {
                event.preventDefault();
                moveLayerDrag(event);
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
            if (S.layerDrag && !S.layerDrag.done && S.layerDrag.id === event.pointerId) { finishLayerDrag(); }
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
                 history: Array.isArray(i.history) ? i.history.length : 0, historyIndex: i.historyIndex, echo: !!S.echoValue,
                 height: parseFloat(S.container.style.height) || 0, fitting: S.fit,
                 frameHidden: !S.frame || S.frame.hidden,
                 layerDrag: S.layerDrag ? { dx: S.layerDrag.dx, dy: S.layerDrag.dy, done: S.layerDrag.done } : null,
                 overlay: !!(S.overlay && !S.overlay.hidden), keepView: !!S.keepView, preview: !!layerPreview(),
                 frame: S.frameRect ? Object.assign({}, S.frameRect) : null, box: cropBoxObject(), rail: railState() };
    }

    function railState() {
        const panels = rail();
        const column = work();
        if (!panels || !column) { return null; }
        const r = panels.getBoundingClientRect();
        return { hidden: railHidden(), shown: r.width > 0 && r.height > 0, below: railBelow(panels, column),
                 height: r.height, maxHeight: parseFloat(panels.style.maxHeight) || 0, workHeight: column.getBoundingClientRect().height };
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
        const helpers = {
            txt2img: "switch_to_txt2img", img2img: "switch_to_img2img", inpaint: "switch_to_inpaint", extras: "switch_to_extras",
            stitch_txt2img: "switch_to_txt2img", stitch_img2img: "switch_to_img2img"
        };
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
            if (S.fit) { scheduleFit(); }
            else { S.container.style.height = (on ? Math.max(MIN_HEIGHT, window.innerHeight - 250) : S.baseHeight) + "px"; }
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
        undoStroke: undoStroke,
        redoStroke: redoStroke,
        sendInput: sendInput,
        fitHeight: fitHeight,
        fit: fit,
        setRail: setRail,
        toggleRail: toggleRail,
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
