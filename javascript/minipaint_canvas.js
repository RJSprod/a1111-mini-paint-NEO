/**
 * Touch Canvas, browser side.
 *
 * Loaded into <head> by the WebUI in both UI modes. Nothing here runs at
 * startup except one guarded check (below) that only does anything when this
 * page carries the touch Canvas editor AND the browser has no WebGL. Every
 * other function is called from a Gradio event on a user action, and every
 * selector starts at the extension's own root. Nothing here watches the
 * document, clicks the host's tabs at startup, or keeps its own idea of which
 * tab is selected.
 */
window.minipaintCanvas = (function () {
    "use strict";

    const ROOT_ID = "minipaint_canvas_root";
    const EDITOR_ID = "minipaint_canvas_editor";
    const TAB_PANEL_ID = "tab_minipaint";
    const FOCUS_CLASS = "minipaint-focus";
    const NEEDS_EDITOR_CLASS = "minipaint-needs-editor";
    const UNDO_LIMIT = 4000;

    function root() {
        return document.getElementById(ROOT_ID);
    }

    function editor() {
        return document.getElementById(EDITOR_ID);
    }

    function tick() {
        return new Promise(function (resolve) { setTimeout(resolve, 0); });
    }

    /* ------------------------------------------------------------------ */
    /* WebGL guard                                                          */
    /* ------------------------------------------------------------------ */

    /**
     * The same test PixiJS makes before it will create a renderer. Gradio's
     * ImageEditor is built on PixiJS, and when this fails the editor throws
     * while mounting - inside Svelte's render pass - and the whole page
     * stops updating: it shows txt2img and no tab will ever switch again.
     */
    function webglAvailable() {
        try {
            const canvas = document.createElement("canvas");
            const options = { stencil: true };
            const gl = canvas.getContext("webgl", options) || canvas.getContext("experimental-webgl", options);
            const ok = !!(gl && gl.getContextAttributes && gl.getContextAttributes().stencil);
            if (gl) {
                const lose = gl.getExtension("WEBGL_lose_context");
                if (lose) { lose.loseContext(); }
            }
            return ok;
        } catch (e) {
            return false;
        }
    }

    const NO_WEBGL_NOTICE =
        '<div class="minipaint-nowebgl">' +
        "<strong>The touch Canvas needs WebGL, which this browser does not provide.</strong><br>" +
        "Every other tab keeps working. To use the original editor here instead, turn on " +
        "<em>Settings → miniPaint / Canvas → Use Old UI (legacy miniPaint)</em> and Reload UI, " +
        "or enable hardware acceleration / WebGL in this browser." +
        "</div>";

    /**
     * Runs once, before Gradio boots, and only on a page whose embedded config
     * carries our editor. If WebGL is missing, our editor's entry becomes a
     * plain HTML notice and our editor-dependent buttons are disabled. Only
     * components this extension created are touched; the host's are not.
     */
    function guardWebGL() {
        const config = window.gradio_config;
        if (!config || !Array.isArray(config.components)) { return; }
        const ours = config.components.find(function (c) {
            return c && c.type === "imageeditor" && c.props && c.props.elem_id === EDITOR_ID;
        });
        if (!ours) { return; }
        if (webglAvailable()) { return; }

        let htmlClassId = null;
        for (const c of config.components) {
            if (c && c.type === "html" && c.component_class_id) { htmlClassId = c.component_class_id; break; }
        }
        ours.type = "html";
        if (htmlClassId) { ours.component_class_id = htmlClassId; }
        ours.props = {
            value: NO_WEBGL_NOTICE,
            visible: true,
            elem_id: EDITOR_ID,
            elem_classes: ["minipaint-editor", "minipaint-nowebgl-notice"],
            name: "html",
            label: null,
            show_label: false,
            container: true,
            min_width: 160
        };
        for (const c of config.components) {
            const classes = (c && c.props && c.props.elem_classes) || [];
            if (classes.indexOf(NEEDS_EDITOR_CLASS) !== -1) {
                c.props.interactive = false;
            }
        }
        console.warn("MiniPaint: WebGL is not available in this browser; the touch Canvas editor was not mounted. " +
            "Use Settings -> miniPaint / Canvas -> Use Old UI (legacy miniPaint) for the original editor.");
        try {
            fetch("/minipaint/log", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    destination: "touch Canvas startup",
                    outcome: "WebGL is not available in this browser; the editor was not mounted and every other tab was left alone",
                    steps: [navigator.userAgent]
                })
            }).catch(function () { /* the console line above is enough */ });
        } catch (e) { /* ignore */ }
    }

    try { guardWebGL(); } catch (e) { console.error("MiniPaint: WebGL guard failed", e); }

    /* ------------------------------------------------------------------ */
    /* Editor history                                                       */
    /* ------------------------------------------------------------------ */

    /**
     * Wind the editor's own history back to the start by pressing its Undo
     * button until it is disabled. Called only right before the server
     * replaces the editor's contents: on this Gradio the editor keeps its
     * crop box across a new image, and undoing the crop is the only way to
     * reset it. Everything undone here has already been read by the server.
     */
    async function flushEditor() {
        const host = editor();
        if (!host) { return; }
        const undo = host.querySelector('button[aria-label="Undo"]');
        if (!undo) { return; }
        for (let i = 0; i < UNDO_LIMIT && !undo.disabled; i++) {
            undo.click();
            await tick();
        }
    }

    /* ------------------------------------------------------------------ */
    /* Galleries and tabs                                                   */
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
        if (target in helpers) {
            if (typeof window[helpers[target]] === "function") { window[helpers[target]](); }
            return;
        }
        if (target !== "canvas") { return; }
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
    /* Modes                                                                */
    /* ------------------------------------------------------------------ */

    const TOOL_LABELS = { crop: "Transform button", draw: "Draw button", erase: "Erase button" };

    /**
     * Select one of the editor's own tools for the mode the user chose.
     * Looked up by the label the editor gives its buttons; if a Gradio
     * update renames them this does nothing, and the editor's toolbar is
     * still right there.
     */
    function selectTool(name) {
        const host = editor();
        const label = TOOL_LABELS[name];
        if (!host || !label) { return; }
        const button = host.querySelector('button[aria-label="' + label + '"]');
        if (button) { button.click(); }
    }

    /**
     * The editor's crop handles listen for mouse events only. Fingers and
     * pens send pointer events, so while a finger drags a handle the
     * matching mouse events are replayed for it. Bound once, on our own
     * editor element, the first time a mode is chosen.
     */
    function enableTouchCrop() {
        const host = editor();
        if (!host || host.dataset.minipaintTouch === "1") { return; }
        host.dataset.minipaintTouch = "1";

        let dragging = null;

        function isTouchLike(event) {
            return event.pointerType === "touch" || event.pointerType === "pen";
        }

        function mouse(type, event) {
            return new MouseEvent(type, {
                bubbles: true,
                cancelable: true,
                clientX: event.clientX,
                clientY: event.clientY,
                screenX: event.screenX,
                screenY: event.screenY,
                button: 0,
                buttons: type === "mouseup" ? 0 : 1
            });
        }

        host.addEventListener("pointerdown", function (event) {
            if (!isTouchLike(event) || !event.target || !event.target.closest) { return; }
            const handle = event.target.closest(".hitbox, .grid");
            if (!handle || !host.contains(handle)) { return; }
            dragging = handle;
            try { host.setPointerCapture(event.pointerId); } catch (e) { /* optional */ }
            event.preventDefault();
            handle.dispatchEvent(mouse("mousedown", event));
        }, { passive: false });

        host.addEventListener("pointermove", function (event) {
            if (!dragging || !isTouchLike(event)) { return; }
            event.preventDefault();
            window.dispatchEvent(mouse("mousemove", event));
        }, { passive: false });

        function finish(event) {
            if (!dragging || !isTouchLike(event)) { return; }
            dragging = null;
            try { host.releasePointerCapture(event.pointerId); } catch (e) { /* optional */ }
            window.dispatchEvent(mouse("mouseup", event));
        }
        host.addEventListener("pointerup", finish);
        host.addEventListener("pointercancel", finish);
    }

    function onMode(mode) {
        enableTouchCrop();
        if (mode === "crop") { selectTool("crop"); }
        else if (mode === "mask") { selectTool("draw"); }
    }

    /* ------------------------------------------------------------------ */
    /* Focus mode                                                           */
    /* ------------------------------------------------------------------ */

    let escapeListener = null;

    function setFocus(on) {
        const element = root();
        if (!element) { return; }
        element.classList.toggle(FOCUS_CLASS, !!on);
        if (on && !escapeListener) {
            escapeListener = function (event) {
                if (event.key === "Escape") { setFocus(false); }
            };
            document.addEventListener("keydown", escapeListener);
        } else if (!on && escapeListener) {
            document.removeEventListener("keydown", escapeListener);
            escapeListener = null;
        }
    }

    return {
        webglAvailable: webglAvailable,
        flushEditor: flushEditor,
        pickGalleryImage: pickGalleryImage,
        switchTo: switchTo,
        selectTool: selectTool,
        enableTouchCrop: enableTouchCrop,
        onMode: onMode,
        setFocus: setFocus
    };
})();
