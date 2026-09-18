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
**Expand**, **Layers**, the current one filled — a **Send to** button, and the status
line, truncated to what is left. The menu holds everything that is not a tool's own
control: *Open…*, *Edit* (Undo, Redo, Reset to original, Save a copy), *Panels* and
*Focus* (toggles), and *Send to* with every destination this WebUI has and a Cancel. The
button on the bar opens that same *Send to* list, under itself, one press instead of two
— one list, drawn in one place, so a destination cannot appear in one and not the other.
Each list closes on the choice; a tap outside or Escape closes it too.
Each tool's controls live in the **rail** on the right — the panel for the chosen tool
is the one showing. The canvas takes whatever height the window has left
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

**Send.** *Send to* on the action row — or *Menu → Send to* — lists every destination:
**img2img**, **img2img Inpaint**
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
background follows Settings → Forge Canvas: a plain colour when that is turned on, and
otherwise the transparency checkerboard - painted here in the theme's own block colour
under a translucent neutral, the way the PNG Info tab's image box is painted, rather than
in the fixed light grey Forge uses. On this tab the pattern fills the window instead of a
thumbnail, and a fixed light grey is a slab of daylight on a night theme.

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
* **A send names nothing of another tab's, and the browser places the picture.** This
  is the one rule the whole send path is shaped around, and it was learnt the hard way:
  an event that names a component the running page does not contain dies *after its
  function has run*, so the work happens, the send log records a successful send, and
  the answer never reaches the page — along with every browser step chained behind it.
  One stale component from another tab therefore stopped the Canvas sending anywhere at
  all. So the server now flattens the picture, says where it goes and hands both to the
  browser as a plan, and the browser places it: into a host canvas's hidden textbox, or
  into a Gradio component through the ordinary upload route, the way a person dropping a
  file does. The one event that names those components is a hidden button the browser
  presses only when it has just found out it cannot place the picture itself, so a
  component that is not on the page costs that destination and nothing else.
  `docs/CANVAS_SEND_2026-09-18.txt` is the incident and the change.
* **ImageStitch is reached the way its own buttons reach it.** Its reference gallery is
  an ordinary Gallery, emptied and then handed the file — a Gradio gallery that is
  holding a picture has no upload input until it is cleared, which is also what makes
  the arriving image its only reference; the box that enables the panel is Forge's own
  InputAccordion checkbox, ticked from the browser so the host's accordion follows it,
  exactly as when a user ticks it.
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

**One WanGP, and an emergency stop.** The extension runs one WanGP per machine, not just
per Forge: a second Forge server started against the same install finds the first one's
lock and refuses to start another (`WANGP_ALREADY_MANAGED`) rather than putting a second
process on the same card. *Integration management* also has **Restart WanGP now**, the
emergency option: it ends every process of the WanGP this extension started — the whole
process tree, a generation in progress included — waits until each one is gone, reads the
chosen GPU before and after so the report can say how much memory came back, invalidates
every browser session and any queue request in flight, and starts a fresh WanGP. It never
touches a WanGP you started yourself or any other process on the card.

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

## The Clipboard tab

A third top-level tab, **Clipboard**: a small file browser over one folder on the Forge
host (left, two thirds), and a small **WanGP request** composer (right, one third) — three
cards for *First Frame*, *Last Frame* and *Reference*, a prompt box, and one button, **Add
to Queue**. It is the first client of a **public browser API** any extension may call,
`window.minipaintInterop`, contract `minipaint.wangp.queue/v1`.

**The rule.** A request is an overlay on the live WanGP page. A card left at *Use WanGP* and
an empty prompt mean *whatever the WanGP page has right now*; a filled card or a typed
prompt overrides the page for that one queued task and is put back afterwards. So the
button is a button whenever WanGP is running: an empty composer asks WanGP to run the page
exactly as it is, a card put back with its × changes nothing on the WanGP page, and a
picture the current model cannot use is kept, badged *Not used by current model*, sent, and
reported as ignored rather than refused. While WanGP is not running the button says so and
is off, and a press is refused rather than stored.

**Press as often as you like.** Every press is a job in a queue the **server** owns, kept
beside the draft and the history, so a refresh, a closed tab or a second browser loses
nothing. Jobs are sent one at a time, in the order pressed across every browser, each by
the page that composed it — so a job uses the WanGP settings its user is looking at — and
the tab shows them under the button with *Cancel*, *Retry* and *Run from this page*. The
first job to find WanGP idle **starts the run**, through WanGP's own generate trigger; the
ones behind it join that run. The decision is made inside WanGP from WanGP's own
process-wide "is a generation running" flag, never from a guess in the browser, and when
that flag cannot be read the request is staged rather than started.

**What you get back.** *WanGP started generating it.* when the request's task is the one a
running loop is on; *Added to WanGP queue.* when a task carrying the request appeared in
WanGP's queue, with how many sit ahead of it; a refusal only when WanGP recorded one for
exactly that request; otherwise *WanGP did not confirm…* — never a claim WanGP did not
prove, and never a retry the machine decided on. Each confirmed recipe goes into **Queue
Send History** (the prompt only if you typed one here), where *Load* puts it back into the
composer without queueing anything. Once WanGP has a job, the page that queued it keeps
asking the bridge where its task is and the card says so: *In WanGP's queue, 2 ahead of
it*, *WanGP is generating it*, *Left WanGP's queue*.

**Enhanced prompts** (off by default). With the *SD-Neo-ModelSwitchRefiner* extension
installed and its LLM Studio set up, the enhancement switch sends the typed prompt - and the
pictures the model reads - to its MiniMax H3 writer first, for whichever H3 model
(FL2VA or Ref2VA) the WanGP page is on, and WanGP gets the written prompt. The job waits in
the Queue as *Enhancing* with the writer's own progress, and goes to WanGP in press order
once its prompt exists; a page on any other model is refused, not enhanced. One button under
the prompt opens a view that fills the window and holds the switch and the four system
prompts the writer runs under (each variant, with and without a picture), opened on the pair
the next press would use; it takes an override that is kept across sessions, and restores
the default.
**Cancel everything** empties the whole line - the writer's requests and the pending jobs -
in one press; jobs already being sent finish, and nothing in WanGP's own queue is touched.

**View Outputs** (under the queue). What WanGP made for this tab's requests, over the whole
window: a player with a filmstrip of everything else under it, tap to hide the controls,
paged at 60, scrubbable because the file is served with byte ranges. It is its own document,
not a view of the queue, so closing the WebUI does not empty it — last week's videos are
still there next week. A job the server ran is tied to its files exactly, by the paths WanGP
handed back; a job a browser page ran is matched to the files that appeared while it was
running, and the gallery marks those as a match rather than a fact. The videos are never
moved, copied or deleted.

**The browser.** Menu → *Choose storage folder* first: Clipboard keeps its pictures in that
folder on the machine running Forge and nowhere else, and never names it to the browser —
every thumbnail is fetched by an opaque id. Upload, paste (Ctrl+V, or the paste panel when
the browser will not let the page read its clipboard), drop a file on a card, sort, resize
the thumbnails, rename, delete, refresh (files dropped into the folder by hand appear),
and *Send selected to* Mini Paint, img2img, Inpaint, Extras or ImageStitch by the same
routes the Canvas takes. A Forge PNG keeps every byte, so its generation parameters
survive.

The grid **pages, sixty pictures at a time**, with Back / Next / go-to under it and the
whole library's count beside them; the selection is yours and survives paging, because a
send names a picture by its id. It is drawn by the browser from an index the server
answers over plain HTTP, and it keeps up by being told rather than by asking: a picture
imported from anywhere adds one tile to the page you are on without moving you, without
disturbing the rest of it, and without re-downloading a thumbnail whose file has not
changed. Thumbnails are cached in the server's memory, on its disk — so a restart does not
re-encode what you look at — and immutably in the browser.

**Send to a destination never needs it opened first.** The picture is placed in the
destination's own component while its tab is hidden, checked to have actually landed, and
only then is that tab shown — which is how Forge's own result buttons behave. A send that
fails leaves you here and says why; a picture that landed in a tab that would not open
says that, and is never sent twice.

**With Mini Paint.** The Canvas's *Send to* menu now offers **Clipboard**; *Send selected
to Mini Paint* lands on the Canvas as Layer 1 over a Background, one Undo away; and the
menu's **Intercept Options** choose what the gallery's 🖌️ button does: put the result on
the Canvas as always, put it *here* — its original file when Forge proves which one it is,
passed through to the Canvas with the reason when Clipboard cannot take it — or open the
**Send to WanGP** popup.

**Send to WanGP from the gallery.** With that third option ticked, the 🖌️ button freezes
the result under an opaque token (never in the library) and opens a compact popup over
the gallery: the composer's prompt and its *Enhanced prompts* switch, the image roles the
WanGP page's model reads (first frame, last frame, reference, several at once), *Inherit
Clipboard inputs* for everything the popup does not override, a dot saying whether WanGP
is idle, generating or not running, and Generate. The request goes down the composer's
own path into the same server-owned queue, marked *from the gallery*, and the popup is
gone as soon as the outbox has the job, so a run of generations can be queued without
leaving the tab. Its own history keeps the last hundred recipes, plus what you pin, and
loads one back reconciled against the model the page is on. Every request it makes is
bounded and it holds no stream open, so it costs nothing of the six-connection budget on
an HTTP/1.1 Forge and nothing at all over the auto-TLS extension's HTTP/2.
`docs/clipboard/README.md` has the whole of it.

**Themed.** Every part of the tab is an ordinary Gradio component and every colour is a
theme variable; there is no fixed white or black anywhere in it, so a night-mode theme
reaches the grid, the cards, the menu and the history alike.

**No live connection required.** Browsing, paging, sorting, selecting, sending, queueing
and reading the Queue all ride ordinary HTTP, so they keep working when the framework's
event channel does not — a backgrounded tab, a forgotten session, a Forge that restarted.
What still needs that channel is named in `docs/clipboard/NO_LIVE_CONNECTION_WHAT_WAS_BUILT_AND_V2.md`,
and the page says which parts are stale rather than claiming the whole tab is offline. The
only line that says the server is gone is the one raised when the server actually stops
answering.

**For other extensions.** `await window.minipaintInterop.wangp.enqueue({ prompt, images:
{ start, end, references }, start: "auto" })` with handles from `stageImage(blob)`
(`{ kind: "staged", id }`) or Clipboard assets (`{ kind: "clipboard_asset", id }`); omitted
fields inherit, `start` defaults to `"auto"` (`"never"` stages only), the request joins the
same server-owned queue as the tab's presses, ids are never paths, and the answer is
`started`, `queued`, `refused` (with a code) or `unconfirmed`. `docs/clipboard/README.md`
is the guide, and `docs/clipboard/CONTRACTS.md` the contract, for both the tab and the
API. `enqueue(request, { enhance: true })` asks for the MiniMax H3 rewrite (the page's model
travels with it), `cancelAll()` empties the line, and `jobs()` shows each job's enhancement
and its place in WanGP. Bridge plugin 1.5.0 carries the queue, start and track operations
(protocol 5) and the control plane server-owned execution runs on (protocol 6), and refuses
a request composed for a model the page has since left (`MODEL_CHANGED`), so WanGP's bridge
must be updated and WanGP restarted; a build lacking one of the six queue components keeps
the image send and refuses the queue with `BRIDGE_COMPONENT_INCOMPATIBLE`, and one lacking
the generate trigger queues but never starts.

**Press it and walk away.** With *WanGP queue: run queued jobs on the server* on — which is
the default — Forge runs the job itself. It starts WanGP if it is cold, waits if you are
generating in the WanGP tab (and says so, and never interrupts you), writes the enhanced
prompt if you asked for one, submits into WanGP's own queue and follows the generation to a
file. The browser may be hidden, frozen, closed or on a phone that has gone to sleep; come
back on any device and one sync shows you where it got to. Three things worth knowing: the
job **is** in the WanGP tab's queue and can be cancelled there; it may wait for the card
because of your own work; and on Windows a Forge crash loses a generation that was in flight,
which is reported as "could not be proved" rather than retried. `docs/wangp/SERVER_EXECUTION.md`
is the whole of it, including what is still an inference waiting for a real install.

## Legacy editor (Old UI)

Everything below is unchanged from the original extension and applies when *Use Old UI* is
on - with one part no longer only the legacy editor's. The transfer library it delivers
pictures with (`miniPaint/src/js/libs/webui-host.js`) is now served to the Canvas and
Clipboard tabs as well, so all three put a picture into a destination the same way, and
the paragraphs below about detecting the destination type, waiting for it to accept the
image, verifying the value the WebUI will submit and settling the img2img sub-tab describe
what every send in this extension does.

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

### What to look for when something in a tab does nothing

A control that does nothing looks the same whether the page failed to reach Forge, the
request was refused on the way, or the event was never on the page to begin with — and
until those can be told apart, fixing it is guesswork. The `browser` lines now carry the
facts that separate them. When a send is not acknowledged, or **Check again** on the
*"lost its live connection"* line comes back empty, look for:

```
browser   clipboard: send img2img: why it went unanswered - the request box holds this
          request; the receipt box is empty; the host's framework: no request left this
          browser; request box: 1 element(s), 1 component(s), wired for input
```

* **`no request left this browser`** — the event never fired. Nothing was asked of Forge,
  so there is nothing to find in Forge's console.
* **`NO EVENT IS WIRED TO IT ON THIS PAGE`**, or **`naming N component(s) NOT ON THIS
  PAGE`** — the control exists and the event behind it cannot run. Gradio will not say so:
  it just never answers. This is what was actually wrong with sending, found the first time
  the page was asked.
* **`2 element(s)`** — the page carries that control twice, so a script writing it by id
  may be writing the copy nothing is listening to.
* **`the receipt box holds an older stamp`** — the request arrived, carrying a value from
  an earlier send.
* **`the host tells this page to call it at http://… while the page is on https://…`** —
  see below. This one is reported as soon as the tab opens, not only after something fails.

### Forge behind TLS: why every event fails and nothing else does

Gradio's frontend does not use relative URLs. It reads an absolute root out of the config
Forge inlines into the page and builds every event from it, and Forge works that root out
from the request it saw. So a Forge reached over HTTPS — through a front end, a tunnel, or
a browser extension that upgrades the address bar — can serve a page over `https` whose
config says `http`. The browser blocks every one of those calls as mixed content, silently.
Nothing looks broken: the page loads, the pictures load, this extension's own routes work
perfectly, because all of those are relative URLs. Only Gradio's own events fail, every
time, on every build.

If the log says the host and the page disagree, fix it at the source: have whatever
terminates TLS send `x-forwarded-proto: https`, or give Forge the public address with
`--subpath`/`root_path` — or reach it over plain `http`. The extension reports this rather
than rewriting Forge's config behind your back.

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
    canvas/display.py            display copies as bytes behind an opaque id, and the lifetime rules that keep the live one
    canvas/routes.py             /minipaint-canvas/display/<id>: those bytes, signed in, immutable, no path anywhere in the URL
    assets.py                    the browser bundles, fetched per tab from /minipaint-assets/js/ rather than parsed into every page
    interop.py                   the public queue API's server half: staging, preparing handoffs, the outbox routes, /minipaint-interop/*
    clipboard/                   the Clipboard tab (see docs/clipboard/README.md)
        config.py                the folder, the intercept destination, the sort and the thumbnail size
        store.py                 the library: one folder, opaque ids, containment, import, refresh, rename, delete
        history.py               the composer's draft and Queue Send History
        outbox.py                the queue outbox: every press a job the server owns, in press order; what each job is waiting for, said out loud
        executor.py              the coordinator that advances those jobs with nobody watching: cold WanGP, the enhancer, the card, the generation
        job_inputs.py            the pictures a queued job owns, pinned until it is done and no sweeper's to take
        enhance.py               enhanced prompts: ModelSwitchRefiner's MiniMax H3 writer (mc_llm_api), the switch, the four system prompts and their overrides
        outputs.py               what WanGP made for this tab's requests, kept where a restart can find it: View Outputs reads this, not the queue
        routes.py                a picture by its id, and bytes in
        intercept.py             Send to WanGP from the gallery: the frozen picture behind a token, the roles a model reads,
                                 the popup's request path through the outbox, and its pinned history
        ui.py                    the tab: the browser, the composer, the enhancement panel, Add to Queue through the public API
    wangp/                       the WanGP tab, all of it (see docs/wangp/README.md)
        config.py                what survives a restart, and only that
        discovery.py             WanGP root, Conda/venv runtimes, GPUs, the bridge plugin
        runtime.py               the child process: one, ours, loopback, one GPU by UUID; the emergency restart
        lock.py                  one managed WanGP per machine: the lock a second Forge finds
        vram.py                  what the chosen GPU holds, as nvidia-smi reports it, for the restart's report
        proxy.py                 /wan2gp/* on the Forge origin, streamed to that one port
        handoff.py               PNGs on their way out, as opaque ids under a fixed root
        bridge.py                which browser page is talking to which live WanGP session
        protocol.py              the vocabulary all three sides share (protocol 5: the queue, starting, tracking, the model a request insists on;
                                 protocol 6: the control plane between Forge and the child, which no browser is part of)
        control.py               Forge's half of that control plane: compose, submit, status, cancel, over authenticated loopback
        errors.py                the failure codes and their sentences
        journal.py               the tab's console: the last 400 steps, in memory
        process_log.py           logs/wangp-log.txt: the same steps, on disk, bounded
        ui.py / settings.py / diagnostics.py    the tab, the one Settings entry, the report
javascript/main.js               legacy bridge, parent-frame side, and the loader for everything below
browser/minipaint_canvas.js      attaches the canvas; crop frame, touch gestures, tools, the rail's height, the layer list, the held mask, focus mode
browser/minipaint_wangp.js       the WanGP iframe: handshake, receiver query, verified send, queue, confirm and track
browser/minipaint_interop.js     window.minipaintInterop: the public queue API (v1, minipaint.wangp.queue/v1): enqueue, the event stream, sync, cancelAll
browser/minipaint_clipboard.js   the Clipboard tab's browser side: the grid and its pager, the queue list and the history,
                                 the menu, paste and drop, Add to Queue, the page's model, and the one standing notice
                                 (browser/ is not auto-loaded: each tab fetches its own bundle from /minipaint-assets/js/, cached by content)
browser/minipaint_intercept.js   the Send to WanGP popup: opened by the gallery button on a frozen picture, drawn from
                                 /minipaint-clipboard/intercept, closed the moment the outbox has the job; fetched only when first needed
wan2gp_bridge/                   the companion plugin, installed into your WanGP
style.css                        legacy rules, rules scoped to the Canvas root, then to the Clipboard root (theme variables only)
miniPaint/                       the legacy editor itself
docs/wangp/                      the WanGP operator's guide, the Phase 0 checklist, the module contracts,
                                 and SERVER_EXECUTION.md: press Add to Queue and walk away, what it rests on, what is still inferred
docs/clipboard/                  the Clipboard tab and the public queue API: the guide, the contracts,
                                 the no-live-connection design intent and what of it is built
docs/CANVAS_SEND_2026-09-18.txt  why every destination of the Canvas's Send to menu stopped working while its
                                 send log recorded them all as sent, and the shape that cannot do it again
tests/                           see tests/README.md
```

## Issues, Code ownership and contribution

This extension is mostly code slammed together from other extensions all being free to use. If you want to grab parts of it, go ahead.
If you find a bug, just report it over the issues section on github.

## Modifying the legacy editor

If you want to customize things inside the miniPaint iframe, go into the miniPaint directory and run `npm run dev` or `npm run build` and then reload your ui.
