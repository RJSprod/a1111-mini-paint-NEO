# miniPaint for Forge Neo — touch-first Canvas, with the original editor kept

A WebUI extension that adds one **Mini Paint** tab with a choice of two frontends:

* **Canvas** (default): a touch-first image preparation workspace built from ordinary
  Gradio components around **the WebUI's own canvas** — the same ForgeCanvas box the
  img2img and Inpaint tabs use, with its zoom, pan, drop, paste and stroke undo. It does
  four things — **Crop**, **Mask**, **Expand**, **Layers** — and hands the result straight
  to img2img or Inpaint. No WebGL is involved anywhere.
* **Old UI**: the original [miniPaint](https://github.com/viliusle/miniPaint) editor in an
  iframe, exactly as before, with its send buttons, ControlNet/Extras destinations,
  verification and transfer log.

Settings → **miniPaint / Canvas** → *Use Old UI (legacy miniPaint)* picks between them
(Reload UI after changing it). Only one frontend is ever built, under the same tab name and
id, so tab order, hidden-tab settings and themes see one stable tab.

Works with [Forge Neo](https://github.com/Haoming02/sd-webui-forge-classic/tree/neo) on
Gradio 4.40 and follows the host theme, including Lobe in dark mode, because every control
is a native Gradio component, the canvas is the host's own, and the extension's CSS only
sets geometry.

## Installation

Extensions → Install from URL → this repository's URL → Install → Reload UI.

## The Canvas

```
 [Open] [Undo] [Redo] [Fit] [Focus]                [ Send to img2img ]
 +---------------------------------------------------------------+
 |             ⛶ 📂 ✠ 🔄 ↩️ ↪️                                    |
 |           the WebUI's own canvas (ForgeCanvas),               |
 |        as tall as the window has room for, no scrolling       |
 +---------------------------------------------------------------+
 1024 × 1024 · from txt2img — Received from txt2img. Auto sends to img2img.
 [     Crop     ] [     Mask     ] [    Expand    ] [    Layers    ]
 one row of controls for that mode      (e.g. [Aspect ▾] [Apply Crop])
 Crop options ▸   (the rest of that mode's settings, closed by default)
 More ▸           (destination, reset, save a copy)
```

The canvas takes whatever height the window has left below the controls, and gives
some back when the options accordion is opened, so the whole tab is always in view and
nothing ever floats over the picture. On a tablet in portrait the rows wrap; on a phone
the buttons stack.

**Getting an image in.** Press 🖌️ in the button row under a txt2img, img2img or Extras
result, use **Open** or the canvas's own 📂 for a local file, or drop or paste a picture
onto the canvas. The image is fitted into the box, in Crop mode. Two fingers pinch to zoom
and pan; the mouse wheel zooms and the right button drags, exactly as in img2img; the
canvas's ✠ refits and ⛶ fills the screen.

**Crop.** A frame with corner handles sits over the image; its size in image pixels is
written on it. Move the image under the frame with one finger (or the left mouse button),
zoom to fit more or less of it in, and drag a corner to resize the frame — with a finger
or the mouse. The **Aspect** menu (Free, Original, 1:1, 4:3, 3:4, 16:9, 9:16, 3:2, 2:3, or
a custom ratio from *Crop options*) locks the frame's shape. **Apply Crop** keeps what is
inside the frame and shows the new size; the frame then covers the whole result again, so
nothing is ever cropped twice by accident. A crop never stretches.

**Mask.** Paint over what should change, with the same brush the Inpaint tab has: same
colour, same opacity, same high-contrast checkerboard if that setting is on. **Paint /
Erase / Move** pick what one finger does; the **Brush size** slider is the one from
Inpaint's toolbar, moved into the row where a finger can reach it. **Clear Mask** and
**Invert Mask** are one tap. Edge smoothing (Off / Low / Medium / High, in *Mask options*)
is applied to the mask when it is sent, so painting stays exactly what the canvas does
natively. The mask is coverage: the colour on screen is never part of what is sent.

**Expand.** Tap an amount (64 / 128 / 256) and a side (Left / Right / Top / Bottom), read
the resulting size, press **Apply Expand**. The new area is masked automatically, plus an
overlap band back into the original so the model has room to blend. *Advanced expansion*
has exact per-side numbers, the overlap width, what to fill the new area with (transparent,
edge-stretched, gray, white, black) and snapping to a pixel multiple. After Apply the
Canvas switches to Mask so the automatic mask can be refined.

**Layers.** The picture that arrives is the *Background* layer; more layers come from
selections. In Layers mode the frame is a rectangle selection: **New from selection**
copies what the active layer has inside it into a layer of its own, in place, above the
active layer. *Mask options → Masked area → new layer* does the same for a painted area,
trimmed to it — the mask brush is the freehand selection. One finger (or the left mouse
button) then **drags the active layer** with a live preview; the other layers stay put,
and the picture keeps its zoom and position when the server's new composite comes back.
Two fingers, the wheel and the right button still pan and zoom. The row holds the layer
menu, **New from selection**, **Merge down** and **Delete layer**; *Layer options* has the
visible-layers chips, the active layer's opacity and name, **Move up / down**,
**Duplicate** and **Flatten all**. Layers sit on a document canvas the size of the first
picture; Crop trims every layer and Expand grows the Background while the others keep
their place. Sending flattens the visible layers. Every layer step is one Undo away. What
does not carry over from miniPaint is transforms, text and filters — the Old UI stays the
place for those.

**Send.** The big button always goes to img2img: a plain image goes to the img2img
sub-tab, an image with a mask (drawn, or created by an expansion) goes to **Inpaint** with
the mask in place, and the WebUI switches there — using its own tab-switch helpers, the
same ones its "Send to img2img" buttons use. The button's label says which it will be.
*More → Send to* overrides this (img2img, Inpaint, Extras). Transparent pixels from an
expansion are filled on the way out (setting: *Send: fill transparent pixels with*).

**Undo / Redo** in the top bar take strokes back first, then the bigger steps — Open,
Apply Crop, Apply Expand, Clear, Invert, every layer step — in the order they happened. *More → Reset to
original* goes back to the image as it arrived. **Fit** puts the image and the frame back
the way they arrived.

**Focus** makes the Canvas fill the window (Exit focus or Escape brings the WebUI back).
On tablets and phones the option panels dock to the bottom edge and every control is at
least 44 px tall.

### Settings (Settings → miniPaint / Canvas)

| option | default | |
| --- | --- | --- |
| Use Old UI (legacy miniPaint) | off | Reload UI to switch |
| Canvas height: fit the window | on | the canvas takes the height the window has left |
| Canvas height when not fitting the window (% of the browser window) | 70 | the canvas's ⛶ fills the window |
| Mask brush size when the Canvas opens | 25 | same scale as the Inpaint brush |
| Expand: snap side amounts to a multiple of | 8 | |
| Send: fill transparent pixels with | Neutral gray | or edge colour, white, black |

The mask's colour, opacity and high-contrast checkerboard are the Inpaint tab's own
settings (Settings → img2img), because that is where the mask is going. The canvas's
background (checkerboard or plain colour) follows Settings → Forge Canvas.

`MINIPAINT_OLD_UI=1` in the environment forces the legacy editor for that run without
touching any setting — the lever for when the UI itself is the problem.

## Why the earlier redesign broke the tab bar, and what this one does about it

The first Canvas redesign left Forge Neo showing txt2img with **no top-level tab able to
switch**, and it could not be reproduced on the machine it was built on. The cause is
known and covered by a test: Gradio 4.40's `ImageEditor` is built on PixiJS, which needs
WebGL. In a browser without WebGL (hardware acceleration off, a remote desktop, a policy,
some tablets) the editor throws while it is being mounted, inside Svelte's render pass,
and the whole page stops reacting — every tab, every button — while still looking
rendered. Nothing about the tab bar was ever touched; a single component that fails to
mount is enough.

The answer is not to guard that component but not to use it. The Canvas now draws with
**ForgeCanvas**, the WebUI's own canvas: plain Canvas2D, no WebGL, already on every page,
already themed, already the box users know from img2img. The extension takes the host's
markup and its two hidden image textboxes, creates the JavaScript instance for its tab the
way the host creates its own (one `load` event per canvas), and adds only what the tab
needs on top, inside its own container: a crop frame, one-finger panning and two-finger
pinch, and the mode / tool / size / aspect choices made in the Gradio panels. The smoke
test runs the whole flow with WebGL disabled and expects nothing to differ.

Beyond that, the tab-bar rules from the failed attempt are followed to the letter:

* no JavaScript runs at startup except attaching the canvas — the same kind of `load`
  event the host registers for each of its own canvases — and every other function is
  called from a Gradio event on a user action, with every selector starting at the
  extension's own elements;
* no document-wide observers, no polling of the page, no synthetic clicks on host tabs
  while the page is coming up;
* the host's tab system is only touched at the one handoff point, by calling the host's
  own `switch_to_img2img` / `switch_to_inpaint` / `switch_to_extras`, and, to reach the
  Canvas, by clicking its own native tab button exactly as those helpers do;
* the "send to Canvas" buttons are Gradio buttons created by the WebUI's ordinary
  component hook, next to "send to extras", not injected DOM;
* the transfer to img2img / Inpaint is a plain Gradio output into the same hidden
  ForgeCanvas inputs the host's own send buttons write, so there is no second copy of any
  host state;
* CSS is scoped under `#minipaint_canvas_root` and uses Gradio's theme variables;
* a Canvas that fails to build falls back to the legacy editor, restores Gradio's build
  context, and says so on the tab; a WebUI without ForgeCanvas does the same.

The smoke test in `tests/browser_smoke.py` switches every top-level tab — with WebGL and
without — before it touches a single Canvas feature.

## Working with ForgeCanvas

A few behaviours of the host's canvas shaped the wiring; each has a small answer in
`minipaint_neo/canvas/ui.py` and `javascript/minipaint_canvas.js`:

* **An image and its mask layer travel through two textboxes, and the layer can only be
  drawn once the image has loaded** (the drawing canvas takes the image's size at that
  moment). So every step that replaces the image — receive, Open, Apply Crop, Apply
  Expand, Undo, Redo, Reset — is three chained events: write the image, wait in the
  browser until the canvas has taken it, write the mask layer. The same wait guards the
  handoff into the Inpaint tab: its image first, then its mask once its canvas has the
  image's size. Without this a mask arriving a moment early is silently wiped.
* **The canvas echoes every image it loads** back through its textbox, re-encoded. The
  browser knows which of those are echoes of an image the server sent and strips them,
  so an echo costs a tiny request rather than an upload of the whole picture; anything
  else on that textbox is a picture the user opened, dropped or pasted, which becomes the
  document (with the previous one an Undo away) and clears the old strokes.
* **Stroke history is per image.** A new image or a mask layer written from the server
  starts the canvas's own ↩️ history afresh, so an undo never restores strokes from a
  different picture.
* **The mask's look is the Inpaint tab's.** Colour, opacity and the high-contrast
  checkerboard are read from the host's settings for both the on-screen brush and the
  layers written from Python (Invert, Undo, Expand), so a restored mask looks like a
  painted one.
* **Painting, panning and zooming never go to the server.** No events are bound to the
  mask layer; the canvas's pixels are read only when Apply, Clear, Invert, Save or Send
  is pressed, and the crop frame is read only by Apply Crop and New from selection, as a
  box in image pixels. Dragging a layer is browser-side too: in Layers mode the server
  keeps two hidden textboxes filled — the active layer and the other layers composited
  without it — and the drag shows the one over the other, hands the offset it settled on
  to the server through a third hidden textbox, and the composite comes back with the
  zoom and position kept.
* **Gradio rebuilds a component after an update output, and under Forge the rebuilt
  copy of a ForgeCanvas textbox reads arrays instead of images.** Any event that answers
  a component with `gr.update()` or `gr.skip()` makes Gradio keep a per-session copy of
  it, reconstructed from the arguments it was created with. Forge switches that
  recording off before its own `LogicalImage` class is defined, so the copy is built from
  the plain Textbox arguments and comes back with `numpy=True`; every read of that
  textbox from then on is a numpy array, and code expecting an image fails. That was the
  "crop, undo, crop again" error. The Canvas's own two textboxes are therefore a subclass
  whose default is `numpy=False`, which survives the rebuild; and the host's img2img and
  Inpaint textboxes are never answered by the backend at all — the image and the mask
  travel as PNG data URLs and a browser-side step writes exactly the chosen textbox,
  leaving the others untouched. Only Extras, an ordinary `gr.Image`, is written from the
  backend.

## Legacy editor (Old UI)

Everything below is unchanged from the original extension and applies when *Use Old UI* is
on.

![preview](images/img1.png)

It is a simple image editing tool but still satisfies most needs when trying to edit images.
It provides the ability to send images to Img2Img, Controlnet and Extras.![Send button](images/img2.png)
Images can also be sent from txt2img, img2img and extras directly to the extension via the 'Send to miniPaint' Button.![Send to miniPaint](images/img3.png)

Forge Neo mixes component types - img2img, Inpaint and ControlNet inputs are `ForgeCanvas`, while
Extras is an ordinary `gr.Image`. The extension detects the type of each destination it writes to,
so it does not need to know which WebUI or Gradio version it is running under.

Sending an image waits for the destination to actually accept it before switching tabs, so on a
remote or slow connection the target tab does not appear until the image is committed - and a
transfer that fails says so in the console instead of leaving you on a tab that looks ready. For
`ForgeCanvas` the image is written to the hidden textbox that Forge submits, not just to the
canvas you can see, because those are not the same value.

img2img generates from whichever of its sub-tabs (img2img / Inpaint / ...) the WebUI *itself*
has recorded, and it only learns about a sub-tab change through a request to the server. Sending
an image therefore also waits for that to be acknowledged, otherwise pressing Generate straight
after a send can render from a different slot - the classic "my image is right there and it was
ignored".

Every send is checked against the value the WebUI will actually submit: miniPaint decodes it back
and compares it, pixel for pixel, with the image it exported. A send that does not match is
retried, and if it still does not match you get a toast in the editor saying it could not send
and why, rather than a destination that merely looks right. Successful sends say so too, and the
console line records whether the value is byte-identical or was re-encoded by the host.

When a send fails, the editor puts the whole report on screen with a **Copy all** button, so it
can be read and copied on a phone or tablet where there is no developer console. The same report
is available at any time from the editor's menu: **Send -> Send log ...**

If a transfer does not work, open the browser console and run:

```js
a1111minipaint.debugReport()
```

It prints the Gradio version, whether ForgeCanvas is present, which destination IDs were found,
and how many ControlNet units are mounted - please include that output in bug reports.

## The transfer log (both frontends)

Every send is written to a log file inside this extension's folder:

```
extensions/a1111-mini-paint-NEO/logs/send-log.txt
```

The legacy editor writes each transfer step by step with timings; the Canvas writes one
line per receive, per picture opened on the canvas, and per send.
The file is created when the extension loads, before any image is sent, and its first line
says which frontend loaded. **If `logs/send-log.txt` does not exist after restarting the
WebUI, this version of the extension is not the one running.** It rotates once it passes
1 MB.

## Layout of the code

```
scripts/mini_paint.py            entry point: registers the callbacks, nothing else
minipaint_neo/
    settings.py                  Settings -> miniPaint / Canvas
    router.py                    builds exactly one frontend, with the fallbacks
    legacy_ui.py                 the original iframe tab, unchanged in behaviour
    send_log.py                  logs/send-log.txt and its route
    canvas/ui.py                 the touch Canvas: components and events
    canvas/surface.py            the host's canvas, built from its pieces for this tab
    canvas/host.py               what the Canvas needs from the WebUI, found without touching its tabs
    canvas/imaging.py            mask, crop and fill maths (Pillow only)
    canvas/outpaint.py           expansion with automatic mask
    canvas/document.py           layers on a canvas, the composite, the mask, and the history of structural steps
javascript/main.js               legacy bridge, parent-frame side (unchanged)
javascript/minipaint_canvas.js   attaches the canvas; crop frame, touch gestures, tools, waits, focus mode
style.css                        legacy rules, then rules scoped to the Canvas root
miniPaint/                       the legacy editor itself
tests/                           see tests/README.md
```

## Issues, Code ownership and contribution

This extension is mostly code slammed together from other extensions all being free to use. If you want to grab parts of it, go ahead.
If you find a bug, just report it over the issues section on github.

## Modifying the legacy editor

If you want to customize things inside the miniPaint iframe, go into the miniPaint directory and run `npm run dev` or `npm run build` and then reload your ui.
