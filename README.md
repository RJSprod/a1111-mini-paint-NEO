# miniPaint for Forge Neo — touch-first Canvas, with the original editor kept

A WebUI extension that adds one **Mini Paint** tab with a choice of two frontends:

* **Canvas** (default): a touch-first image preparation workspace built from ordinary
  Gradio components around one `gr.ImageEditor`. It does three things — **Crop**,
  **Mask**, **Expand** — and hands the result straight to img2img or Inpaint.
* **Old UI**: the original [miniPaint](https://github.com/viliusle/miniPaint) editor in an
  iframe, exactly as before, with its send buttons, ControlNet/Extras destinations,
  verification and transfer log.

Settings → **miniPaint / Canvas** → *Use Old UI (legacy miniPaint)* picks between them
(Reload UI after changing it). Only one frontend is ever built, under the same tab name and
id, so tab order, hidden-tab settings and themes see one stable tab.

Works with [Forge Neo](https://github.com/Haoming02/sd-webui-forge-classic/tree/neo) on
Gradio 4.40 and follows the host theme, including Lobe in dark mode, because every control
is a native Gradio component and the extension's own CSS only sets geometry.

## Installation

Extensions → Install from URL → this repository's URL → Install → Reload UI.

## The Canvas

```
 [Open] [Undo] [Redo] [Focus]                      [ Send to img2img ]
 +---------------------------------------------------------------+
 |                                                               |
 |                        image editor                           |
 |                                                               |
 +---------------------------------------------------------------+
 status: 1024 × 1024 · from txt2img — Auto sends to img2img
 [        Crop        ] [        Mask        ] [       Expand       ]
 options for the selected mode
 More ▾  (destination, reset, save a copy)
```

**Getting an image in.** Press 🖌️ in the button row under a txt2img, img2img or Extras
result, or use **Open** for a local file. The image appears large, in Crop mode.

**Crop.** Pick an aspect (Free, Original, 1:1, 4:3, 3:4, 16:9, 9:16, 3:2, 2:3 or a
custom ratio) and drag the handles on the image — with a mouse or a finger. The editor's
own ↶ undoes a drag. **Apply Crop** makes it permanent and shows the new size; sending
applies it as well, so Apply is only needed before masking or expanding a cropped image.
A crop never stretches.

**Mask.** Paint over what should change. The brush, eraser, size and colour are in the
editor's own toolbar under the image (the brush popup has the size slider); its ↶ undoes a
stroke. **Clear Mask** and **Invert Mask** are one tap. Edge smoothing (Off / Low / Medium /
High) is applied to the mask when it is sent, so painting stays exactly what the editor
does natively. The mask is coverage: the colour on screen is never part of what is sent.

**Expand.** Tap an amount (64 / 128 / 256) and a side (Left / Right / Top / Bottom), read
the resulting size, press **Apply Expand**. The new area is masked automatically, plus an
overlap band back into the original so the model has room to blend. *Advanced expansion*
has exact per-side numbers, the overlap width, what to fill the new area with (transparent,
edge-stretched, gray, white, black) and snapping to a pixel multiple. After Apply the
Canvas switches to Mask so the automatic mask can be refined.

**Send.** The big button always goes to img2img: a plain image goes to the img2img
sub-tab, an image with a mask (drawn, or created by an expansion) goes to **Inpaint** with
the mask in place, and the WebUI switches there — using its own tab-switch helpers, the
same ones its "Send to img2img" buttons use. The button's label says which it will be.
*More → Send to* overrides this (img2img, Inpaint, Extras). Transparent pixels from an
expansion are filled on the way out (setting: *Send: fill transparent pixels with*).

**Undo / Redo** in the top bar cover the structural steps — Open, Apply Crop, Apply
Expand, Clear, Invert — and *More → Reset to original* goes back to the image as it
arrived. Strokes and crop drags are undone inside the editor.

**Focus** makes the Canvas fill the window (Exit focus or Escape brings the WebUI back).
On tablets and phones the option panels dock to the bottom edge and every control is at
least 44 px tall.

### Settings (Settings → miniPaint / Canvas)

| option | default | |
| --- | --- | --- |
| Use Old UI (legacy miniPaint) | off | Reload UI to switch |
| Canvas height (% of the browser window) | 70 | |
| Mask brush color | `#ff2f2f` | display only; white and black are offered too |
| Mask brush radius in pixels | 0 = automatic | the editor's brush popup changes it per session |
| Expand: snap side amounts to a multiple of | 8 | |
| Send: fill transparent pixels with | Neutral gray | or edge colour, white, black |

`MINIPAINT_OLD_UI=1` in the environment forces the legacy editor for that run without
touching any setting — the lever for when the UI itself is the problem.

## Why the earlier redesign broke the tab bar, and what this one does about it

The first Canvas redesign left Forge Neo showing txt2img with **no top-level tab able to
switch**, and it could not be reproduced on the machine it was built on. The cause is now
known and covered by a test: Gradio 4.40's `ImageEditor` is built on PixiJS, which needs
WebGL. In a browser without WebGL (hardware acceleration off, a remote desktop, a policy,
some tablets) the editor throws while it is being mounted, inside Svelte's render pass,
and the whole page stops reacting — every tab, every button — while still looking
rendered. Nothing about the tab bar was ever touched; a single component that fails to
mount is enough.

So the extension now checks for WebGL in the browser *before* Gradio boots (the same test
PixiJS makes) and, only when it is missing, rewrites its own editor entry in the page
configuration into a plain notice and disables its own editor-dependent buttons. Every
other tab works; the notice says how to enable the legacy editor. The check runs only on
pages that carry the Canvas, touches only components this extension created, and is a
no-op everywhere else.

Beyond that, the tab-bar rules from the failed attempt are followed to the letter:

* no JavaScript runs at startup except that guard — every other function is called from a
  Gradio event on a user action, and every selector starts at the extension's own root;
* no document-wide observers, no polling, no synthetic clicks on host tabs or on the
  editor's toolbar while the page is coming up;
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
  context, and says so on the tab; a Gradio without `ImageEditor` does the same.

The smoke test in `tests/browser_smoke.py` switches every top-level tab — with WebGL and
without — before it touches a single Canvas feature.

## Working with the Gradio 4.40 editor

A few behaviours of the stock `ImageEditor` on this Gradio shaped the design; each has a
small, contained answer rather than a replacement component:

* **Crop handles answer to the mouse only.** A small adapter, bound to the editor element
  the first time a mode is chosen, replays a finger or pen drag on a handle as the mouse
  events the editor expects. Without it, mouse cropping still works.
* **The crop box survives a new image.** The editor never resets its crop rectangle when
  its contents are replaced, which would crop the next image and every export. So every
  step that replaces the editor's contents — receive, Open, Apply Crop, Apply Expand,
  Clear, Invert, Undo, Redo, Reset — first reads the editor, then presses the editor's own
  Undo until its history is empty (which puts the crop box back), then pushes the new
  image. For the same reason the editor's own upload, paste and "clear canvas" controls
  are not used; Open and 🖌️ take their place and reset properly.
* **Brush size is read once at mount.** The editor's brush popup changes it; the setting
  above sets the default.
* **There is no zoom.** The image is fitted to the block; Focus mode gives it the window.
* **Painting never goes to the server.** No change/input events are bound to the editor;
  its pixels are read only when Apply, Clear, Invert or Send is pressed.

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
line per receive and per send, and the WebGL guard writes a line if it ever has to act.
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
    canvas/host.py               what the Canvas needs from the WebUI, found without touching its tabs
    canvas/imaging.py            mask, crop and fill maths (Pillow only)
    canvas/outpaint.py           expansion with automatic mask
    canvas/document.py           the document, its history, the stage / commit handshake
javascript/main.js               legacy bridge, parent-frame side (unchanged)
javascript/minipaint_canvas.js   WebGL guard, editor flush, touch crop adapter, focus mode
style.css                        legacy rules, then rules scoped to the Canvas root
miniPaint/                       the legacy editor itself
tests/                           see tests/README.md
```

## Issues, Code ownership and contribution

This extension is mostly code slammed together from other extensions all being free to use. If you want to grab parts of it, go ahead.
If you find a bug, just report it over the issues section on github.

## Modifying the legacy editor

If you want to customize things inside the miniPaint iframe, go into the miniPaint directory and run `npm run dev` or `npm run build` and then reload your ui.
