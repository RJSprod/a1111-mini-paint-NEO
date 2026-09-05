# Checks

```
python tests/run.py                                   # image maths + both frontends + the workflow
FORGE_ROOT=/path/to/forge-neo python tests/browser_smoke.py   # the real extension in a real browser
```

Nothing in `run.py` starts the WebUI. `tests/stubs/modules` stands in for the
handful of `modules.*` names the extension touches, `tests/stubs/modules_forge`
for Forge's canvas module (its markup, its hidden image textbox, and the
`ForgeCanvas` the host builds img2img with), and `tests/forge_like.py` builds
a page shaped like Forge Neo's `modules/ui.py` — the same component hooks, the
same constructor repair, the same two metaclass patches applied in the same
order (which is what makes a session rebuild of Forge's textbox lose its
`numpy=False`), the same canvases in img2img and Inpaint, and one
`gr.Tabs(elem_id="tabs")` around everything.

| file | what it holds the line on |
| --- | --- |
| `test_imaging.py` | the mask is coverage carried as the scribble layer's alpha and comes back unchanged; the high-contrast layer is the canvas's own checkerboard; an echo of a picture is recognised even after a browser's premultiplied re-encoding; the crop frame the browser reports is parsed, rounded and clamped; smoothing rounds an edge without eating a thin stroke; expansion places the original correctly, masks only what is new, carries an existing mask, refuses a canvas no browser can hold; the inpaint layer is what Forge's threshold reads; the document's undo history, original included |
| `test_frontends.py` | the Old UI setting exists, is saved, defaults to the touch Canvas and asks for a Reload UI; the mask style is the Inpaint tab's; each frontend mounts alone under the same tab; the surface is the host's markup with its two hidden textboxes and exactly one `load` event to attach it, told to fit the window; no sticky rule and no fixed rule outside focus mode; each mode has a quick row and an options panel behind one closed accordion; the receive button lands in each output row right after "send to extras"; every event on the assembled page resolves; every step that replaces the image is image → wait → mask layer; Undo and Redo try the canvas's stroke history first; the canvas's image textbox has one input handler, filtered in the browser; painting, tools, aspect, fit and focus are browser-only; **no backend event writes a host image textbox**: send writes Extras from the backend, the instruction and an image payload, and browser-only steps write the chosen host textbox (the other untouched), switch tabs, and after a wait on the Inpaint canvas write its mask; a Canvas that throws leaves a working legacy tab **and a working host**; a WebUI without ForgeCanvas picks legacy and says why |
| `test_workflow.py` | the session rebuild Gradio does after an update output, under Forge's metaclass patches: a rebuilt Canvas textbox still reads images, a rebuilt host textbox would not; receive, an echo changing nothing, a picture opened on the canvas becoming the document, crop from a reported frame (with and without strokes), undo, redo (strokes first, then steps), mask mode, send to Inpaint as an image payload then a mask payload, explicit img2img dropping the mask, Extras written directly, Inpaint without a mask clearing the layer there, expand with auto-mask and fill-on-send, clear, invert, reset, the side buttons and snapping, open from a file, save a copy |
| `browser_smoke.py` | every top-level tab switches *before* any Canvas feature is touched; the canvas attaches without an image and takes the height the window has left, the tab fits without scrolling, nothing lies over the canvas, and opening the options accordion shrinks the canvas rather than overflowing; receive from a gallery lands in the tab, fitted, with the frame over the whole image, and the echo is not mistaken for an opened picture; a handle drag with the mouse and with a finger crops exactly the frame, and the frame resets to the result; crop → undo → crop again → undo; the aspect menu; one-finger pan and two-finger pinch; Fit; a stroke, Undo taking it back (not the crop) and Redo, an erase, the tools and the size slider; a send to Inpaint arrives as image then same-size mask, drawn on that canvas, and the host switches to the Inpaint sub-tab; Expand adds 128px, masks it, sends opaque pixels; Clear and Invert; Reset; a picture opened on the canvas; focus mode and Escape. **The whole flow runs again with WebGL disabled and must not differ.** Legacy mode: the iframe mounts alone. |

`browser_smoke.py` needs Playwright with a Chromium (`CHROMIUM=/path/to/chrome`
if it is not where Playwright keeps it) and `FORGE_ROOT` pointing at a Forge
Neo checkout: the page loads Forge's own `canvas.js`, `canvas.css`, `script.js`
and `ui.js`, because the Canvas is the host's canvas. The unit suites use the
real `canvas.html` from `FORGE_ROOT` when it is set and a minimal copy with the
same element ids otherwise.

`test_frontends.py` prints one traceback on purpose — it breaks the Canvas to
prove the fallback catches it.
