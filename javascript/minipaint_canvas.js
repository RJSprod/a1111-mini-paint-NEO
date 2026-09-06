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
 *   - the menu: one button, a flyout drawn here with Open, Edit, Tools,
 *     Panels, Focus and Send to, each item pressing a hidden Gradio control,
 *   - the taps in the layer list (select, add to the selection, show or
 *     hide, reorder), handed to the server as one action each,
 *   - an outline of the document canvas and, in Layers mode, of the
 *     selected layers; a drag that starts on the selection moves it, with
 *     a live preview, and hands the offset it settled on to the server;
 *     a drag elsewhere pans,
 *   - a grip on the crop frame's top edge that moves the frame,
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
    const LAYER_TRANSFORM_ID = "minipaint_canvas_layer_transform";
    const TRANSFORM_CLASS = "minipaint-transforming";
    // Edges snap to the canvas's edges and to other layers' when this close (screen pixels).
    const SNAP_PX = 14;
    const MIN_LAYER_SIDE = 8;
    const LAYER_ACTION_ID = "minipaint_canvas_layer_action";
    const MENU_ID = "minipaint_canvas_menu";
    const SEND_REQUEST_ID = "minipaint_canvas_send_request";
    const TARGETS_ID = "minipaint_canvas_targets";
    const SUGGEST_ID = "minipaint_canvas_suggest";
    const PRESS_IDS = {
        open: "minipaint_canvas_open", undo: "minipaint_canvas_undo", redo: "minipaint_canvas_redo",
        reset: "minipaint_canvas_reset", save: "minipaint_canvas_save"
    };
    const MODE_NAMES = { crop: "Crop", mask: "Mask", expand: "Expand", layers: "Layers" };
    const TOOL_ID_PREFIX = "minipaint_canvas_tool_";
    const CURRENT_CLASS = "minipaint-current";
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
        bounds: null,
        layerBounds: null,
        menu: null,
        menuSection: null,
        menuOutside: null,
        menuKey: null,
        frameGrip: null,
        frameDrag: null,
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
        transform: null,
        transformEl: null,
        transformBox: null,
        transformSize: null,
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
        buildBounds();
        buildFrame();
        buildTransform();
        buildMenu();
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
            if (child === S.menu) { continue; }  // flies over the canvas; takes no room
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
                if (!child.contains(S.container) && child !== S.menu) { observer.observe(child); }
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
        // canvas records the first stroke state of the new image. The
        // canvas's own version draws the picture into a scratch canvas and
        // PNG-encodes it into the textbox (hundreds of milliseconds at a
        // few megapixels) so the server can read it; for a picture the
        // server itself just wrote there, that would replace its value with
        // a bigger copy of the same pixels and echo it back - skipped.
        const updateBackground = i.updateBackgroundImageData.bind(i);
        i.updateBackgroundImageData = function () {
            if (S.serverLoad) {
                S.echoValue = bind.target ? bind.target.value : null;
            } else {
                updateBackground();
                S.echoValue = null;
            }
            const kept = restoreView();
            endTransform(false);
            endLayerDrag(false);
            S.loaded += 1;
            setTimeout(function () {
                trimHistory();
                refreshFrame(!kept);
                refreshOverlays();
            }, 0);
        };

        // The picture is redrawn on every pan and zoom; a layer preview and
        // the other layers standing in for the picture follow it.
        const drawImage = i.drawImage.bind(i);
        i.drawImage = function () {
            drawImage();
            if (S.underlaySwapped && S.underlaySrc && S.imageEl) { S.imageEl.src = S.underlaySrc; }
            positionOverlay();
            positionBounds();
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
            ? { imgX: i.imgX, imgY: i.imgY, imgScale: i.imgScale, w: i.orgWidth, h: i.orgHeight,
                cw: S.container.clientWidth, ch: S.container.clientHeight }
            : null;
    }

    function restoreView() {
        const k = S.keepView;
        const i = S.instance;
        S.keepView = null;
        if (!k || !i || !i.img || i.orgWidth !== k.w || i.orgHeight !== k.h) { return false; }
        // A box that changed size meanwhile (a rotated tablet) gets the fresh fit instead.
        if (S.container.clientWidth !== k.cw || S.container.clientHeight !== k.ch) { return false; }
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
        if (S.mode === "layers" && mode !== "layers") { endTransform(true); endLayerDrag(true); }
        // A mode's panel is a flyout beside the canvas: choosing a mode
        // brings the rail back if it was put away.
        if (mode !== S.mode && railHidden()) { setRail(true); }
        S.mode = mode;
        markTool(mode);
        const i = S.instance;
        if (!i) { return; }
        if (mode === "mask") {
            setTool(S.tool);
        } else {
            i.no_scribbles = true;
        }
        refreshFrame(false);
        refreshOverlays();
    }

    /** The bar's tool buttons: the current one is marked, each says its name. */
    function markTool(mode) {
        for (const each of Object.keys(MODE_NAMES)) {
            const button = document.getElementById(TOOL_ID_PREFIX + each);
            if (!button) { continue; }
            button.classList.toggle(CURRENT_CLASS, each === mode);
            button.setAttribute("aria-pressed", each === mode ? "true" : "false");
            if (!button.title) { button.title = MODE_NAMES[each]; }
        }
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
        // The grip on the top edge moves the whole frame, so a frame drawn
        // once can be put over another part of the picture.
        const grip = document.createElement("div");
        grip.className = "minipaint-frame-grip";
        grip.title = "Drag to move the frame";
        grip.appendChild(document.createElement("span"));
        box.appendChild(grip);
        wrap.appendChild(box);
        S.imageContainer.appendChild(wrap);
        S.frame = wrap;
        S.frameBox = box;
        S.frameSize = size;
        S.frameGrip = grip;

        box.addEventListener("pointerdown", function (event) {
            const target = event.target && event.target.closest ? event.target : null;
            const gripHit = target ? target.closest(".minipaint-frame-grip") : null;
            if (gripHit && S.frameRect) {
                event.preventDefault();
                event.stopPropagation();
                try { gripHit.setPointerCapture(event.pointerId); } catch (e) { /* optional */ }
                S.frameDrag = { id: event.pointerId, x: event.clientX, y: event.clientY, left: S.frameRect.left, top: S.frameRect.top };
                return;
            }
            const handle = target ? target.closest(".minipaint-frame-handle") : null;
            if (!handle) { return; }
            event.preventDefault();
            event.stopPropagation();
            try { handle.setPointerCapture(event.pointerId); } catch (e) { /* optional */ }
            S.handleDrag = { corner: handle.dataset.corner, id: event.pointerId };
        });
        box.addEventListener("pointermove", function (event) {
            if (S.frameDrag && event.pointerId === S.frameDrag.id) {
                event.preventDefault();
                event.stopPropagation();
                moveFrame(event.clientX - S.frameDrag.x, event.clientY - S.frameDrag.y);
                return;
            }
            if (!S.handleDrag || event.pointerId !== S.handleDrag.id) { return; }
            event.preventDefault();
            event.stopPropagation();
            resizeFrame(S.handleDrag.corner, containerPoint(event));
        });
        function endHandle(event) {
            if (S.frameDrag && event.pointerId === S.frameDrag.id) { S.frameDrag = null; }
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
        const show = (S.mode === "crop" || S.mode === "layers") && hasImage() && !S.transform;
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

    /** Slide the whole frame by the grip; its size is kept. */
    function moveFrame(dx, dy) {
        const r = S.frameRect;
        const size = containerSize();
        if (!r || !S.frameDrag) { return; }
        r.left = clamp(S.frameDrag.left + dx, 0, Math.max(0, size.w - r.width));
        r.top = clamp(S.frameDrag.top + dy, 0, Math.max(0, size.h - r.height));
        applyFrame();
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
    /* The menu                                                              */
    /* ------------------------------------------------------------------ */

    /**
     * One button, one flyout under it, drawn here: Open, Edit (Undo, Redo,
     * Reset, Save a copy), Tools (the four modes), Panels, Focus and Send
     * to (every destination the server listed). An item presses the hidden
     * Gradio control for it - a hidden button, or a hidden textbox with a
     * nonce so the same request twice still counts - and the menu closes.
     * A tap outside or Escape closes it too.
     */
    function buildMenu() {
        const column = work();
        if (!column || S.menu) { return; }
        const panel = document.createElement("div");
        panel.className = "minipaint-menu";
        panel.hidden = true;
        panel.setAttribute("role", "menu");
        panel.addEventListener("click", function (event) {
            const item = event.target && event.target.closest ? event.target.closest("[data-menu]") : null;
            if (!item || !panel.contains(item)) { return; }
            event.preventDefault();
            menuAction(item.dataset.menu, item.dataset.value || "");
        });
        column.appendChild(panel);
        S.menu = panel;
    }

    function readTargets() {
        const box = document.querySelector("#" + TARGETS_ID + " textarea");
        try {
            const list = JSON.parse(box && box.value ? box.value : "[]");
            return Array.isArray(list) ? list : [];
        } catch (e) { return []; }
    }

    /**
     * Whether anything is drawn on the mask layer: the canvas keeps the
     * layer's pixels after every stroke, so the latest entry says. Null
     * when it keeps none (no picture yet).
     */
    function hasStrokes() {
        const i = S.instance;
        const state = i && Array.isArray(i.history) ? i.history[i.historyIndex] : null;
        if (!state || !state.data || !i.img) { return null; }
        const bytes = state.data;
        const words = new Uint32Array(bytes.buffer, bytes.byteOffset, Math.floor(bytes.byteLength / 4));
        for (let k = 0; k < words.length; k++) {
            if (words[k]) { return true; }
        }
        return false;
    }

    /**
     * Which destination the Send to menu marks as suggested. The server
     * writes what it knows at each step ("inpaint expansion", "inpaint",
     * "img2img"); strokes drawn since then are known only here.
     */
    function suggestedTarget() {
        const box = document.querySelector("#" + SUGGEST_ID + " textarea");
        const words = String(box && box.value ? box.value : "").split(/\s+/).filter(Boolean);
        if (words.indexOf("expansion") !== -1) { return "inpaint"; }
        const strokes = hasStrokes();
        if (strokes === null) { return words[0] || ""; }
        return strokes ? "inpaint" : "img2img";
    }

    function focusOn() {
        const element = root();
        return !!element && element.classList.contains(FOCUS_CLASS);
    }

    function menuItems(section) {
        const tick = function (on) { return on ? "✓ " : ""; };
        if (section === "edit") {
            return [
                { menu: "back", label: "‹ Back" },
                { menu: "press", value: PRESS_IDS.undo, label: "Undo" },
                { menu: "press", value: PRESS_IDS.redo, label: "Redo" },
                { menu: "press", value: PRESS_IDS.reset, label: "Reset to original" },
                { menu: "press", value: PRESS_IDS.save, label: "Save a copy" }
            ];
        }
        if (section === "send") {
            const suggest = suggestedTarget();
            const items = [{ menu: "back", label: "‹ Back" }];
            for (const target of readTargets()) {
                items.push({ menu: "send", value: String(target[0]), label: String(target[1]) + (target[0] === suggest ? "  · suggested" : "") });
            }
            items.push({ menu: "close", label: "Cancel" });
            return items;
        }
        return [
            { menu: "press", value: PRESS_IDS.open, label: "Open…" },
            { menu: "section", value: "edit", label: "Edit ›" },
            { menu: "panels", label: tick(!railHidden()) + "Panels" },
            { menu: "focus", label: tick(focusOn()) + "Focus" },
            { menu: "section", value: "send", label: "Send to ›" }
        ];
    }

    function renderMenu(section) {
        const panel = S.menu;
        if (!panel) { return; }
        S.menuSection = section || null;
        panel.innerHTML = "";
        for (const item of menuItems(S.menuSection)) {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "minipaint-menu-item" +
                (item.menu === "section" ? " minipaint-menu-more" : "") +
                (item.menu === "back" ? " minipaint-menu-back" : "");
            button.dataset.menu = item.menu;
            if (item.value) { button.dataset.value = item.value; }
            button.setAttribute("role", "menuitem");
            button.textContent = item.label;
            panel.appendChild(button);
        }
    }

    /** Press a hidden Gradio button by its id: the same click a finger would give it. */
    function pressHidden(id) {
        const element = document.getElementById(id);
        if (!element) { return false; }
        const button = element.tagName === "BUTTON" ? element : element.querySelector("button");
        if (!button) { return false; }
        button.click();
        return true;
    }

    function menuAction(kind, value) {
        switch (kind) {
            case "section": renderMenu(value); return;
            case "back": renderMenu(null); return;
            case "close": closeMenu(); return;
            case "press": closeMenu(); pressHidden(value); return;
            case "send": closeMenu(); sendInput(SEND_REQUEST_ID, value + ":" + Date.now()); return;
            case "panels": closeMenu(); setRail(railHidden()); return;
            case "focus": closeMenu(); setFocus(!focusOn()); return;
            default: return;
        }
    }

    function positionMenu() {
        const button = document.getElementById(MENU_ID);
        const column = work();
        if (!button || !column || !S.menu) { return; }
        const b = button.getBoundingClientRect();
        const c = column.getBoundingClientRect();
        S.menu.style.top = (b.bottom - c.top + 4) + "px";
        S.menu.style.left = Math.max(0, b.left - c.left) + "px";
    }

    function openMenu() {
        if (!S.menu) { return; }
        renderMenu(null);
        S.menu.hidden = false;
        positionMenu();
        // Listened for only while the menu is open, and taken down with it.
        S.menuOutside = function (event) {
            const target = event.target;
            if (S.menu.contains(target)) { return; }
            if (target && target.closest && target.closest("#" + MENU_ID)) { return; }
            closeMenu();
        };
        document.addEventListener("pointerdown", S.menuOutside, true);
        S.menuKey = function (event) {
            if (event.key === "Escape") { event.stopPropagation(); closeMenu(); }
        };
        document.addEventListener("keydown", S.menuKey, true);
    }

    function closeMenu() {
        if (!S.menu) { return; }
        S.menu.hidden = true;
        S.menuSection = null;
        if (S.menuOutside) { document.removeEventListener("pointerdown", S.menuOutside, true); S.menuOutside = null; }
        if (S.menuKey) { document.removeEventListener("keydown", S.menuKey, true); S.menuKey = null; }
    }

    function toggleMenu() {
        if (!S.menu) { return; }
        if (S.menu.hidden) { openMenu(); } else { closeMenu(); }
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

    /**
     * Two outlines over the picture: the document canvas (the thin line,
     * always), and in Layers mode the selected layers (dashed), where a
     * drag has to start to move them. Both are positioned from the same
     * numbers the drag preview uses, and follow every pan and zoom.
     */
    function buildBounds() {
        const bounds = document.createElement("div");
        bounds.className = "minipaint-canvas-bounds";
        bounds.hidden = true;
        const selection = document.createElement("div");
        selection.className = "minipaint-layer-bounds";
        selection.hidden = true;
        S.imageContainer.appendChild(bounds);
        S.imageContainer.appendChild(selection);
        S.bounds = bounds;
        S.layerBounds = selection;
    }

    /** The selected layers on screen, in container pixels, or null. */
    function selectionRect() {
        const i = S.instance;
        if (!i || !i.img || !(i.imgScale > 0)) { return null; }
        const s = i.imgScale;
        const t = S.transform;
        if (t) { return { left: i.imgX + t.x * s, top: i.imgY + t.y * s, width: t.w * s, height: t.h * s }; }
        const p = S.mode === "layers" ? layerPreview() : null;
        if (!p) { return null; }
        const d = S.layerDrag;
        return { left: i.imgX + (p.x + (d ? d.dx : 0)) * s, top: i.imgY + (p.y + (d ? d.dy : 0)) * s, width: p.w * s, height: p.h * s };
    }

    function place(element, rect) {
        element.hidden = !rect;
        if (!rect) { return; }
        element.style.left = rect.left + "px";
        element.style.top = rect.top + "px";
        element.style.width = rect.width + "px";
        element.style.height = rect.height + "px";
    }

    function positionBounds() {
        if (!S.bounds) { return; }
        place(S.bounds, imageRect());
        // The transform box draws its own edge.
        place(S.layerBounds, S.transform ? null : selectionRect());
        positionTransform();
    }

    /** After the server sent a new preview, or the view changed. */
    function refreshOverlays() {
        positionOverlay();
        positionBounds();
    }

    /** Whether a pointer is on the selected layers (with a little slack). */
    function hitSelection(event) {
        const r = selectionRect();
        if (!r) { return false; }
        const point = containerPoint(event);
        const slack = 8;
        return point.x >= r.left - slack && point.x <= r.left + r.width + slack &&
            point.y >= r.top - slack && point.y <= r.top + r.height + slack;
    }

    /**
     * The selected layers as the server last described them. A payload
     * without a picture ("src") means the picture has not changed since
     * the last one that had it - a move changes the box, not the pixels -
     * so that picture is kept.
     */
    function layerPreview() {
        const target = document.querySelector("#" + LAYER_PREVIEW_ID + " textarea");
        const text = target ? target.value : "";
        if (!text) { return null; }
        if (S.previewCache.text !== text) {
            let data = null;
            try { data = JSON.parse(text); } catch (e) { data = null; }
            const last = S.previewCache.data;
            if (data && !data.src && last && last.src) { data.src = last.src; }
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
        const t = S.transform;
        if (!S.overlay || S.overlay.hidden || !i || (!d && !t)) { return; }
        const s = i.imgScale;
        const rect = t
            ? { left: i.imgX + t.x * s, top: i.imgY + t.y * s, width: t.w * s, height: t.h * s }
            : { left: i.imgX + (d.preview.x + d.dx) * s, top: i.imgY + (d.preview.y + d.dy) * s, width: d.preview.w * s, height: d.preview.h * s };
        S.overlay.style.left = rect.left + "px";
        S.overlay.style.top = rect.top + "px";
        S.overlay.style.width = rect.width + "px";
        S.overlay.style.height = rect.height + "px";
        if (S.layerBounds) { place(S.layerBounds, t ? null : selectionRect()); }
        positionTransform();
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
        const p = d.preview;
        let dx = (event.clientX - d.x) / s;
        let dy = (event.clientY - d.y) / s;
        const targets = snapTargets(p);
        const tolerance = snapTolerance();
        dx += snapDelta([p.x + dx, p.x + p.w + dx], targets.xs, tolerance);
        dy += snapDelta([p.y + dy, p.y + p.h + dy], targets.ys, tolerance);
        d.dx = dx;
        d.dy = dy;
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
    /* Snapping                                                              */
    /* ------------------------------------------------------------------ */

    function snapTolerance() {
        const s = S.instance ? S.instance.imgScale : 1;
        return SNAP_PX / (s > 0 ? s : 1);
    }

    /** The edges a dragged layer can land on: the canvas's (whether or not
     * the Background is shown) and those of the other visible layers, as
     * the server listed them with the preview. Image pixels. */
    function snapTargets(preview) {
        const p = preview || layerPreview();
        const xs = [];
        const ys = [];
        if (p && p.canvas) { xs.push(0, p.canvas[0]); ys.push(0, p.canvas[1]); }
        for (const b of (p && p.others) || []) {
            xs.push(b[0], b[0] + b[2]);
            ys.push(b[1], b[1] + b[3]);
        }
        return { xs: xs, ys: ys };
    }

    /** The smallest shift that puts one of the edges on a target, or 0. */
    function snapDelta(edges, targets, tolerance) {
        let best = 0;
        let bestAbs = tolerance + 1;
        for (const edge of edges) {
            for (const target of targets) {
                const d = target - edge;
                const a = Math.abs(d);
                if (a <= tolerance && a < bestAbs) { best = d; bestAbs = a; }
            }
        }
        return best;
    }

    /* ------------------------------------------------------------------ */
    /* Transform mode: one box around the selection, corners to resize,     */
    /* the inside to move; Done hands the result to the server             */
    /* ------------------------------------------------------------------ */

    function buildTransform() {
        const wrap = document.createElement("div");
        wrap.className = "minipaint-transform";
        wrap.hidden = true;
        const box = document.createElement("div");
        box.className = "minipaint-transform-box";
        const size = document.createElement("div");
        size.className = "minipaint-transform-size";
        box.appendChild(size);
        for (const corner of HANDLES) {
            const handle = document.createElement("div");
            handle.className = "minipaint-transform-handle " + corner;
            handle.dataset.corner = corner;
            box.appendChild(handle);
        }
        wrap.appendChild(box);
        S.imageContainer.appendChild(wrap);
        S.transformEl = wrap;
        S.transformBox = box;
        S.transformSize = size;

        box.addEventListener("pointerdown", function (event) {
            const t = S.transform;
            if (!t || t.done || t.drag) { return; }
            if (event.pointerType === "mouse" && event.button !== 0) { return; }
            event.preventDefault();
            event.stopPropagation();
            const handle = event.target && event.target.closest ? event.target.closest(".minipaint-transform-handle") : null;
            t.drag = { id: event.pointerId, kind: handle ? handle.dataset.corner : "move", start: imagePoint(event), box0: { x: t.x, y: t.y, w: t.w, h: t.h } };
            try { box.setPointerCapture(event.pointerId); } catch (e) { /* optional */ }
        });
        box.addEventListener("pointermove", function (event) {
            const t = S.transform;
            if (!t || !t.drag || t.drag.id !== event.pointerId) { return; }
            event.preventDefault();
            event.stopPropagation();
            const point = imagePoint(event);
            if (t.drag.kind === "move") { moveTransform(t, point); } else { scaleTransform(t, point); }
            positionOverlay();
        });
        function end(event) {
            const t = S.transform;
            if (!t || !t.drag || t.drag.id !== event.pointerId) { return; }
            t.drag = null;
            try { box.releasePointerCapture(event.pointerId); } catch (e) { /* optional */ }
        }
        box.addEventListener("pointerup", end);
        box.addEventListener("pointercancel", end);
    }

    /** A pointer's place in image pixels. */
    function imagePoint(event) {
        const i = S.instance;
        const point = containerPoint(event);
        const s = i && i.imgScale > 0 ? i.imgScale : 1;
        return { x: (point.x - (i ? i.imgX : 0)) / s, y: (point.y - (i ? i.imgY : 0)) / s };
    }

    function moveTransform(t, point) {
        const b = t.drag.box0;
        let x = b.x + (point.x - t.drag.start.x);
        let y = b.y + (point.y - t.drag.start.y);
        const targets = snapTargets(t.preview);
        const tolerance = snapTolerance();
        x += snapDelta([x, x + t.w], targets.xs, tolerance);
        y += snapDelta([y, y + t.h], targets.ys, tolerance);
        t.x = x;
        t.y = y;
    }

    /** A corner drag scales about the opposite corner, keeping the shape;
     * the pointer's travel along the diagonal sets the size, and the two
     * edges that move snap. */
    function scaleTransform(t, point) {
        const b = t.drag.box0;
        const kind = t.drag.kind;
        const leftSide = kind === "tl" || kind === "bl";
        const topSide = kind === "tl" || kind === "tr";
        const ax = leftSide ? b.x + b.w : b.x;
        const ay = topSide ? b.y + b.h : b.y;
        const dx0 = (leftSide ? b.x : b.x + b.w) - ax;
        const dy0 = (topSide ? b.y : b.y + b.h) - ay;
        const len2 = dx0 * dx0 + dy0 * dy0;
        if (!len2) { return; }
        const minF = Math.max(MIN_LAYER_SIDE / Math.max(1, b.w), MIN_LAYER_SIDE / Math.max(1, b.h));
        let f = Math.max(minF, ((point.x - ax) * dx0 + (point.y - ay) * dy0) / len2);
        const targets = snapTargets(t.preview);
        const tolerance = snapTolerance();
        const sx = snapDelta([ax + dx0 * f], targets.xs, tolerance);
        const sy = snapDelta([ay + dy0 * f], targets.ys, tolerance);
        let snapped = null;
        if (sx && (!sy || Math.abs(sx) <= Math.abs(sy))) { snapped = (dx0 * f + sx) / dx0; }
        else if (sy) { snapped = (dy0 * f + sy) / dy0; }
        if (snapped !== null && snapped >= minF) { f = snapped; }
        t.w = b.w * f;
        t.h = b.h * f;
        t.x = leftSide ? ax - t.w : ax;
        t.y = topSide ? ay - t.h : ay;
    }

    function positionTransform() {
        const t = S.transform;
        if (!S.transformEl) { return; }
        S.transformEl.hidden = !t || t.done;
        if (!t || t.done) { return; }
        const rect = selectionRect();
        if (!rect) { return; }
        S.transformBox.style.left = rect.left + "px";
        S.transformBox.style.top = rect.top + "px";
        S.transformBox.style.width = rect.width + "px";
        S.transformBox.style.height = rect.height + "px";
        S.transformSize.textContent = Math.round(t.w) + " × " + Math.round(t.h);
    }

    /** Enter transform mode around the selection. False when there is
     * nothing to transform (no picture, no layer, not in Layers mode). */
    function startTransform() {
        const preview = layerPreview();
        const i = S.instance;
        const element = root();
        if (S.mode !== "layers" || !preview || !preview.src || !i || !i.img || !S.overlay || !element) { return false; }
        if (S.transform) { return true; }
        if (S.layerDrag) { endLayerDrag(true); }
        S.transform = { x: preview.x, y: preview.y, w: preview.w, h: preview.h, preview: preview, drag: null, done: false };
        if (S.overlay.getAttribute("src") !== preview.src) { S.overlay.src = preview.src; }
        S.overlay.style.opacity = String((preview.opacity == null ? 100 : preview.opacity) / 100);
        S.overlay.hidden = false;
        const under = underlaySource();
        if (under && S.imageEl) {
            S.underlaySrc = under;
            S.underlaySwapped = true;
            S.imageEl.src = under;
        }
        element.classList.add(TRANSFORM_CLASS);
        refreshFrame(false);
        positionOverlay();
        positionBounds();
        return true;
    }

    /** Done: the box as it was left goes to the server; the preview stays
     * until the new picture has been drawn. Nothing changed: just leave. */
    function finishTransform() {
        const t = S.transform;
        if (!t || t.done) { return false; }
        t.drag = null;
        const x = Math.round(t.x);
        const y = Math.round(t.y);
        const w = Math.round(t.w);
        const changed = x !== t.preview.x || y !== t.preview.y || w !== t.preview.w;
        if (!changed) { endTransform(true); return false; }
        t.done = true;
        const element = root();
        if (element) { element.classList.remove(TRANSFORM_CLASS); }
        positionTransform();
        if (!sendInput(LAYER_TRANSFORM_ID, JSON.stringify({ x: x, y: y, w: w, t: Date.now() }))) { endTransform(true); return false; }
        return true;
    }

    /** Leave transform mode; redraw the picture unless the server's new one
     * is what is being drawn. */
    function endTransform(redraw) {
        if (!S.transform) { return; }
        S.transform = null;
        const element = root();
        if (element) { element.classList.remove(TRANSFORM_CLASS); }
        if (S.transformEl) { S.transformEl.hidden = true; }
        if (S.overlay) { S.overlay.hidden = true; }
        const swapped = S.underlaySwapped;
        S.underlaySwapped = false;
        if (redraw && swapped && S.instance) { S.instance.drawImage(); }
        refreshFrame(false);
        positionBounds();
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
                event.target.closest(".forge-toolbar, .forge-toolbar-static, .minipaint-frame-handle, .minipaint-frame-grip, .minipaint-transform")) { return; }
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
            if (S.pointers.size === 1 && S.mode === "layers") {
                if ((S.layerDrag && S.layerDrag.done) || (S.transform && S.transform.done)) {
                    // A drop is on its way to the server; a drag or a pan now
                    // would be undone by the picture that comes back.
                    event.preventDefault();
                    S.pointers.delete(event.pointerId);
                    return;
                }
                // Only what is selected moves, and only when the drag starts
                // on it. In transform mode the box takes the drags itself.
                if (!S.layerDrag && !S.transform && hitSelection(event) && startLayerDrag(event)) { event.preventDefault(); return; }
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
                 transform: S.transform ? { x: S.transform.x, y: S.transform.y, w: S.transform.w, h: S.transform.h, done: S.transform.done, dragging: !!S.transform.drag } : null,
                 overlay: !!(S.overlay && !S.overlay.hidden), keepView: !!S.keepView, preview: !!layerPreview(),
                 frame: S.frameRect ? Object.assign({}, S.frameRect) : null, box: cropBoxObject(), rail: railState(),
                 menuOpen: !!(S.menu && !S.menu.hidden), menuSection: S.menuSection,
                 bounds: S.bounds && !S.bounds.hidden ? imageRect() : null, selection: selectionRect(), frameDrag: !!S.frameDrag,
                 pointers: S.pointers.size, pan: !!S.pan, pinch: !!S.pinch };
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
        toggleMenu: toggleMenu,
        startTransform: startTransform,
        finishTransform: finishTransform,
        endTransform: endTransform,
        closeMenu: closeMenu,
        refreshOverlays: refreshOverlays,
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
