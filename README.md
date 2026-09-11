# miniPaint for Forge Neo — touch-first Canvas, with the original editor kept

A WebUI extension that adds one **Mini Paint** tab with a choice of two frontends:

* **Canvas** (default): a touch-first image preparation workspace built from ordinary
  Gradio components around **the WebUI's own canvas** — the same ForgeCanvas box the
  img2img and Inpaint tabs use, with its zoom, pan, drop, paste and stroke undo. It does
  four things — **Crop**, **Mask**, **Expand**, **Layers** — laid out like an image
  editor (a big canvas, panels in a rail on the right) and hands the result straight to
  img2img, Inpaint, Extras or ImageStitch. No WebGL is involved anywhere.
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

There is also an optional second tab, **WanGP**, which runs a WanGP install you already
have in its own process, shows its real UI inside Forge, and gives the Canvas exact WanGP
destinations to send to. It stays out of the way until it is set up, and nothing about it
can stop the Mini Paint tab loading — see [The WanGP tab](#the-wangp-tab).

## Installation

Extensions → Install from URL → this repository's URL → Install → Reload UI.

## The Canvas

```
 [☰ Menu] [⌗] [◒] [⤢] [◈]  1024 × 1024 · 3 layers — Layer 2 holds the selection.
 +------------------------------------------------------+  +-------------------+
 |                                    ⛶ 📂 ✠ 🔄 ↩️ ↪️     |  | Layers            |
 |                                                      |  | [New from selection]
 |           the WebUI's own canvas (ForgeCanvas),      |  | 👁 Layer 2   ☑ ▲ ▼ |
 |        as tall as the window has room for,           |  | 👁 Layer 1   ☐ ▲ ▼ |
 |        no scrolling, nothing floating over it        |  | 👁 Background ☐ ▲ ▼ |
 |                                                      |  | [Merge] [Delete]  |
 +------------------------------------------------------+  | [Resize / move by hand]
                                                           | [Re-center layer] |
   ☰ Menu:  Open… · Edit › (Undo, Redo, Reset, Save a copy)  | Size ─── ½ 100% ×2 |
            Panels · Focus                                    | Opacity ────────  |
            Send to › (img2img, Inpaint, Extras, ImageStitch…, Cancel)
```

The action row is one **Menu** button, the four tools as icon buttons — **Crop**, **Mask**,
**Expand**, **Layers**, the current one filled — and the status line, truncated to what
is left. The menu holds everything that is not a tool's own control: *Open…*, *Edit*
(Undo, Redo, Reset to original, Save a copy), *Panels* and *Focus* (toggles), and *Send
to* with every destination this WebUI has and a Cancel. Each list closes on the choice; a
tap outside or Escape closes it too. Each tool's controls live in the **rail** on the
right — the panel for the chosen tool is the one showing. The canvas takes whatever height the window has left
below the action row; the rail is never taller than that and scrolls inside it, so the
whole tab is always in view and nothing ever floats over the picture. *Panels* puts the
rail away for a canvas the full width of the window and brings it back (choosing a tool
brings it back too). On a phone the rail moves under the canvas with a bounded share of
the height.

**Getting an image in.** Press 🖌️ in the button row under a txt2img, img2img or Extras
result, use *Menu → Open…* or the canvas's own 📂 for a local file, or drop or paste a
picture onto the canvas. The image is fitted into the box, in Crop, as **Layer 1 over a
white Background of the same size**; a thin line on the canvas marks that size. Two
fingers pinch to zoom and pan; the mouse wheel zooms and the right button drags, exactly
as in img2img; the canvas's ✠ refits and ⛶ fills the screen.

**Crop.** Crop opens with **nothing selected** — a frame over the whole picture is never
a crop. Drag over the part to keep and a frame with corner handles appears over what you
dragged, its size in image pixels written on it; one smaller than 128 × 128 is dismissed
with a *Tiny Debounce* notice in the status line, and the next drag draws again. Then
drag a corner to resize the frame, drag the **grip on its top edge** to slide it, and
move the picture under it with one finger (or the left mouse button); pinch or scroll to
zoom. **Reselect** clears the frame so the next drag draws a new one. The **Aspect** menu
(Free, Original, 1:1, 4:3, 3:4, 16:9, 9:16, 3:2, 2:3, or a custom ratio) reshapes the
frame you have in place and shapes the ones you draw next. **Apply Crop** keeps what is
inside and shows the new size; nothing is selected afterwards, so nothing is ever cropped
twice by accident. A frame you drew survives a trip through another tool; the automatic
one Layers starts with never enters Crop. A crop never stretches.

**Mask.** Paint over what should change, with the same brush the Inpaint tab has: same
colour, same opacity, same high-contrast checkerboard if that setting is on. **Paint /
Erase / Move** pick what one finger does; the **Brush size** slider is the one from
Inpaint's toolbar, moved into the panel where a finger can reach it. **Clear Mask** and
**Invert Mask** are one tap. Edge smoothing (Off / Low / Medium / High) is applied to the
mask when it is sent, so painting stays exactly what the canvas does natively. The mask
is coverage: the colour on screen is never part of what is sent.

**Expand.** Tap an amount (64 / 128 / 256) and a side (Left / Right / Top / Bottom), read
the resulting size, press **Apply Expand**. The new area is masked automatically, plus an
overlap band back into the original so the model has room to blend. *Exact amounts* has
per-side numbers, the overlap width, what to fill the new area with (transparent,
edge-stretched, gray, white, black) and snapping to a pixel multiple. After Apply the
Canvas switches to Mask so the automatic mask can be refined.

**Layers.** The picture is *Layer 1*; under it is a white *Background* the size of the
canvas, so a layer can be hidden, moved or deleted and the canvas keeps its edge. More
layers come from selections: in Layers the frame is a rectangle selection, and **New from
selection** copies what the active layer has inside it into a layer of its own, in place,
above the active layer. **Masked area → new layer** does the same for a painted area,
trimmed to it — the mask brush is the freehand selection. The panel's **layer list** is
the one from miniPaint and Photoshop, top layer first: tap a name to select that layer
alone, tap the box (or shift/ctrl-tap the name) to select several, the eye shows or hides
a layer, the arrows move it up or down the stack; the selected rows are highlighted and
the primary one carries a bar. The selected layers are outlined on the canvas with a
dashed line: **a drag that starts inside that outline moves them**, with a live preview,
and nothing else moves; a drag that starts outside pans the picture, as in Crop. The
picture keeps its zoom and position when the server's new composite comes back; two
fingers, the wheel and the right button still pan and zoom. **Reselect** clears the selection frame so the next drag on the canvas draws a new one
(Layers otherwise starts with the whole picture selected). **Add image as layer…** opens
the file chooser and drops the picture in as a layer of its own, fitted to the canvas —
scaled until it touches it on one side and centred, from its own pixels — without
changing the canvas's size, and opens it for placing at once (below); leave it and it
stays fitted. **Resize / move by hand**
puts a box with round corner knobs on the selected layer and hides everything else in the
panel but **Done**: drag a corner to resize it (the shape is kept; the opposite corner
stays put), drag inside the box to move it, as many times as you like, then *Done* applies
it in one step (one Undo away). **Edges snap**: while a layer is dragged or resized, an
edge that comes within about 14 screen pixels of the canvas's edge — whether or not the
Background is shown — or of another visible layer's edge lands exactly on it, so a layer
meant to sit flush against a border or a neighbour does, without pixel-hunting.
**Re-center layer** brings a layer that was dragged out of view back to the middle of the
canvas. **Size** resizes the selected layers — the slider, or ½, 100% and ×2 — about
their centre, always from the pixels the layer started with, so resizing again does not
blur it twice. **Merge** joins
the selected layers into one (or one selected layer into the one below it); **Delete**,
**Duplicate**, the opacity slider and **Flatten all** act on the selection; the name box
renames the primary layer. Crop trims every layer and Expand grows the Background while
the others keep their place. Sending flattens the visible layers. Every layer step is one
Undo away. What does not carry over from miniPaint is rotation, text and filters — the
Old UI stays the place for those.

**Send.** *Menu → Send to* lists every destination: **img2img**, **img2img Inpaint**
(the mask goes with the image), **Extras**, and **ImageStitch** in txt2img or img2img —
the image replaces whatever reference images the ImageStitch panel of that tab held,
becomes its only one, the panel is switched on, and the WebUI goes to that tab. The one a
plain send would pick is marked *suggested*: Inpaint when there is a mask or an
expansion, img2img otherwise. The WebUI switches to the destination's tab using its own
tab-switch helpers, the same ones its "Send to img2img" buttons use. Pixels that are
see-through — a hidden layer, a smaller layer over a hidden Background, an expansion —
are **sent as they are**: the picture arrives with real transparency, and at generation
time the WebUI fills it with the colour in *Settings → img2img → For img2img, fill the
transparent parts of the input image with this color* (gray unless you change it; white is
a common choice). Extras gets white. The setting *Send: see-through pixels* can fill them
with white, the edge colour or black on the way out instead; gray is not offered.

**Undo / Redo** (*Menu → Edit*) take strokes back first, then the bigger steps — Open,
Apply Crop, Apply Expand, Clear, Invert, every layer step — in the order they happened.
*Menu → Edit → Reset to original* goes back to the image as it arrived. The canvas's own
✠ puts the image and the frame back the way they arrived.

**What keeps it quick.** The canvas is only ever shown a *display copy* of the composite
— a JPEG, or a WebP when the picture is see-through — while the document on the server
keeps the real pixels; so a step uploads the strokes, never the picture, the history keeps
references rather than encodings, and the drag preview is made at screen size and sent
again only when the pixels changed, not for every move. On a 1664 × 1152 photo a dropped
layer is redrawn in well under half a second where it took several before. Sends and
saves still go out as PNG.

**Focus** (*Menu → Focus*) makes the Canvas fill the window; choosing it again, or
Escape, brings the WebUI back. How it is done, for another extension, is in
`docs/focus-mode.txt`. Every control is at least 44 px tall; on a phone the rail sits
under the canvas.

### Settings (Settings → miniPaint / Canvas)

| option | default | |
| --- | --- | --- |
| Use Old UI (legacy miniPaint) | off | Reload UI to switch |
| Canvas height: fit the window | on | the canvas takes the height the window has left |
| Canvas height when not fitting the window (% of the browser window) | 70 | the canvas's ⛶ fills the window |
| Mask brush size when the Canvas opens | 25 | same scale as the Inpaint brush |
| Expand: snap side amounts to a multiple of | 8 | |
| Send: see-through pixels | Keep transparent | or white, edge colour, black; the WebUI fills kept transparency at generation time with its own img2img background colour (gray unless changed) |

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
  keeps two hidden textboxes filled — the selected layers as one picture and the other
  layers composited without them — and the drag shows the one over the other, hands the
  offset it settled on to the server through a third hidden textbox, and the composite
  comes back with the zoom and position kept. The layer list is HTML the server renders;
  one listener on the tab hands each tap (select, add to the selection, eye, up, down) to
  the server through a fourth hidden textbox, so the list can be replaced wholesale by the
  next reply.
* **ImageStitch is reached the way its own buttons reach it.** Its reference gallery is
  an ordinary Gallery the send writes from the backend, replacing the list; the box that
  enables the panel is Forge's own InputAccordion checkbox, ticked from the browser so the
  host's accordion follows it, exactly as when a user ticks it.
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

## The WanGP tab

A second top-level tab, **WanGP**, that runs the [WanGP](https://github.com/deepbeepmeep/Wan2GP)
you already have in its own process and shows **its real UI** inside Forge — not a
Forge-side copy of its controls — and puts WanGP destinations in the Canvas's *Send to*
menu. It is entirely separate from Mini Paint: its own tab, its own settings entry, its own
routes, registered alongside the ones above rather than in place of them. If any part of it
fails to load, one line says so in the console and the Mini Paint tab is exactly as it was.

**Setting it up.** Open the tab and press *Start Setup*. Five steps: the folder that
contains `wgp.py`, the Python environment that runs it (Conda environments are found for
you, and either way the wizard *probes* the one you pick — it runs a harmless command in it
and records whether `<prefix>/python` or `conda run -p <prefix>` is the one that actually
works), the GPU it may use, the bridge plugin, and a checklist of thirteen security and
launch checks. Nothing is written until every mandatory row is green, and the last four
rows need WanGP up and the page loaded, so *Run the checks* is usually pressed twice.
Nothing is moved, copied or downloaded: your WanGP keeps its models, presets, LoRAs and
outputs where they are, and Forge's own Python never imports it. The setup is remembered in
`<Forge data_path>/a1111-mini-paint-NEO/wan2gp.json`, and the setup it replaces is kept as
a backup you can restore.

**What actually runs.** Nothing starts when Forge boots; opening the tab asks for WanGP and
the tab shows a *starting* card with a *Check again* button rather than holding a Gradio
event open for a cold model load. The child binds `127.0.0.1` on a port the kernel picks,
with `CUDA_VISIBLE_DEVICES` set to the **UUID** of the card you chose — not its index, so a
reboot cannot repoint it, and a card that is missing means nothing starts rather than
something starting on the wrong one. `--listen`, `--share`, `--open-browser` and `0.0.0.0`
are *refused* by the code that builds the command line, not merely left out of it. The
browser never learns that port: the tab's iframe is `src="/wan2gp/"` — a path on the Forge
origin — and a reverse proxy inside Forge streams both directions with nothing buffered, so
a long generation's queue events are not cut short and a large upload is not held in
memory. Its destination comes from the runtime object and from nowhere a request can reach.
Stopping only ever touches the child this extension started, by its own process group or
Windows job object; nothing is ever killed by name, so a WanGP you started yourself is left
alone.

**Sending a picture.** *Menu → Send to* asks the WanGP page in your browser what image
inputs it can accept *right now* — one bounded question, when the menu opens, and never
again while it stays open — and turns the answer into exact lines: *Send Image to WanGP
Start Frame*, *… End Frame*, *… Reference*. There is no remembered list and no table of
model names, so an input that is switched off or full is a line that says so and does
nothing, and when there is no live WanGP page in that Forge tab the menu says to open it
and choose a model. Choosing a line flattens the canvas to a PNG on the server and hands
the browser nothing but that file's 32-character id — never a path — and a small plugin
inside WanGP reads it, puts the picture in the input you named through an ordinary Gradio
event, and proves that is where it went. Only that acknowledgement switches you to the
WanGP tab; a send that cannot be proved leaves you in the Canvas with one line saying why,
and the detail in `logs/send-log.txt`. References **append**: two already there become
three, in their original order, never one. Nothing is generated for you — you set the rest
up in WanGP and press Generate yourself.

**The bridge plugin.** The piece inside WanGP is `wan2gp-minipaint-bridge`, shipped in
`wan2gp_bridge/` and installed by setup step 4 into `<WanGP root>/plugins/`. It writes
inside its own folder and nowhere else, refuses to replace a folder that is not ours, and
needs WanGP restarted afterwards because plugins load once at startup. It is the only part
of the whole integration that knows a WanGP element id — one file, `compatibility.py` — so
when WanGP moves an input that is the file to correct and nothing in Forge or the browser
changes. A build that does not expose an input the bridge needs fails closed: the tab keeps
working, and *Send to* offers nothing rather than something that looked about right.

**Reinitialize.** Settings → *miniPaint / Canvas* has one WanGP entry, and it is a
paragraph with a link rather than a switch — the wizard's fields are not duplicated there,
and there is no checkbox that would mean "reinitialize" forever. The button itself is in
the tab, under *Integration management*, next to *Copy diagnostic report*. It stops the
WanGP this extension started, forgets which install, environment and GPU it was pointed at,
and brings the tab back to the wizard. It uninstalls nothing and deletes no models, LoRAs,
presets, outputs or other plugins.

**Not yet proven on real hardware.** The integration was written without a WanGP, a Forge
or an NVIDIA driver to hand, so every claim about a WanGP element id, a proxied WebSocket,
Forge's authentication boundary or Windows process ownership is unverified.
`docs/wangp/PHASE0.md` is the checklist for settling them on a real install — what to run
and what result proves it — and it is honest about which boxes nobody has ticked.
`docs/wangp/README.md` is the operator's guide: what is stored where, what is deliberately
never stored, the invariants you can check yourself, and how to read a failure code. The
specification both follow is `docs/WAN2GP_TAB_DESIGN_INTENT_REVISED_2026-09-06.txt`.

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
line per receive, per picture opened on the canvas, and per send. A send to WanGP writes
one line too, naming the input it went to, whether it replaced or appended, how the
transfer was verified and the per-step timings — and never a handoff id, a channel or a
port.
The file is created when the extension loads, before any image is sent, and its first line
says which frontend loaded. **If `logs/send-log.txt` does not exist after restarting the
WebUI, this version of the extension is not the one running.** It rotates once it passes
1 MB.

## The WanGP process log

WanGP is started as a child process, so everything it prints — which model it loaded, which
plugins it found, the traceback when it fell over — used to exist only on the console Forge
was launched from, and only until it scrolled. It is now also written to:

```
extensions/a1111-mini-paint-NEO/logs/wangp-log.txt
```

Every line is timestamped and tagged with which half of the integration said it (`wangp`
for the child's own words, plus `runtime`, `proxy`, `browser` and `checks`). It rotates to
`wangp-log.previous.txt` at 2 MB, so there is always at least one full run of history and
never more than two files.

## Nothing in a log identifies you

Prompts and output filenames are the same thing twice — WanGP names a file after the prompt
that produced it — and both used to reach the WebUI console verbatim, because the child's
output was relayed there unchanged. That is fixed at the source: every line the extension
writes anywhere, to the console or to either log file, goes through one redaction pass
first. What it takes out:

* **prompts and captions**, in every syntax they get printed in — `Prompt: …`,
  `--prompt "…"`, `{"prompt": "…"}`;
* **paths**, down to `<path>/*.mp4` — or `<wangp>/outputs/*.mp4` where the directory is one
  the integration knows, since the tree a file sits in is structure and the folders above it
  are your account name;
* **filenames** with a content extension, with or without a directory in front;
* **URLs, e-mail addresses and non-loopback IP addresses**;
* **credentials** — `key=value` secrets, vendor-prefixed tokens, and any opaque run long
  enough to be an id or a digest.

What stays is what a failure is diagnosed from: exception types and messages, error codes,
module filenames and line numbers in a traceback, version numbers, resolutions, frame
counts, timings, and the names of WanGP's own settings. `<wangp>/outputs/*.mp4` still tells
you a video was written and where it went.

Both log files are therefore safe to attach to a bug report without reading them line by
line first, which is the point — a log you have to audit before sharing is a log you will
not share.

One thing the pass cannot remove is a bare personal name typed into free text with nothing
structural around it. In practice that means a **model, LoRA or preset you named yourself**,
which is kept deliberately: "which model failed" is the question these logs exist to answer.
Renaming it is the only fix.

## Layout of the code

```
scripts/mini_paint.py            entry point: registers the callbacks, nothing else
minipaint_neo/
    settings.py                  Settings -> miniPaint / Canvas
    router.py                    builds exactly one frontend, with the fallbacks
    legacy_ui.py                 the original iframe tab, unchanged in behaviour
    send_log.py                  logs/send-log.txt and its route
    scrub.py                     the one redaction pass every writer goes through
    canvas/ui.py                 the touch Canvas: components and events
    canvas/surface.py            the host's canvas, built from its pieces for this tab
    canvas/host.py               what the Canvas needs from the WebUI (galleries, inputs, ImageStitch), found without touching its tabs
    canvas/imaging.py            mask, crop and fill maths (Pillow only)
    canvas/outpaint.py           expansion with automatic mask
    canvas/document.py           layers on a canvas (the picture over a white Background), the composite, the mask, and the history of structural steps
    wangp/                       the WanGP tab, all of it (see docs/wangp/README.md)
        config.py                what survives a restart, and only that
        discovery.py             WanGP root, Conda/venv runtimes, GPUs, the bridge plugin
        runtime.py               the child process: one, ours, loopback, one GPU by UUID
        proxy.py                 /wan2gp/* on the Forge origin, streamed to that one port
        handoff.py               PNGs on their way out, as opaque ids under a fixed root
        bridge.py                which browser page is talking to which live WanGP session
        protocol.py              the vocabulary all three sides share
        errors.py                the failure codes and their sentences
        journal.py               the tab's console: the last 400 steps, in memory
        process_log.py           logs/wangp-log.txt: the same steps, on disk, bounded
        ui.py / settings.py / diagnostics.py    the tab, the one Settings entry, the report
javascript/main.js               legacy bridge, parent-frame side (unchanged)
javascript/minipaint_canvas.js   attaches the canvas; crop frame, touch gestures, tools, the rail's height, the layer list, waits, focus mode
javascript/minipaint_wangp.js    the WanGP iframe: handshake, receiver query, verified send
wan2gp_bridge/                   the companion plugin, installed into your WanGP
style.css                        legacy rules, then rules scoped to the Canvas root
miniPaint/                       the legacy editor itself
docs/wangp/                      the WanGP operator's guide, the Phase 0 checklist, the module contracts
tests/                           see tests/README.md
```

## Issues, Code ownership and contribution

This extension is mostly code slammed together from other extensions all being free to use. If you want to grab parts of it, go ahead.
If you find a bug, just report it over the issues section on github.

## Modifying the legacy editor

If you want to customize things inside the miniPaint iframe, go into the miniPaint directory and run `npm run dev` or `npm run build` and then reload your ui.
