# The Clipboard tab and the WanGP queue API — an operator's guide

This describes what the extension does when the **Clipboard** tab is used and when
another extension calls the public queue API: what it shows, what it writes, what it
refuses to write, and what you can check for yourself. It is written against the code in
`minipaint_neo/clipboard/`, `minipaint_neo/interop.py`, `javascript/minipaint_clipboard.js`,
`javascript/minipaint_interop.js`, the queue half of `wan2gp_bridge/` and the registration
in `scripts/mini_paint.py`. The design document it implements is the Clipboard design
intent (`CLIPBOARD_WANGP_QUEUE_SPEC_UPDATED`); where the two differ, this file follows the
code and says so. The WanGP tab it builds on has its own guide, `docs/wangp/README.md`.

## What it is

A third top-level tab, **Clipboard** (id `minipaint_clipboard`), beside Mini Paint and
WanGP. On the left, about two thirds of the width, a small file browser over one folder on
the machine running Forge; on the right, a small **WanGP request** composer: three cards —
*First Frame*, *Last Frame*, *Reference* — a prompt box, and one button, **Add to Queue**.

It is registered as a third, independent integration. If any part of it fails to import
or to build, one line says so in the WebUI console, the tab shows a note under the same
label, and Mini Paint and WanGP load exactly as before — including the gallery's *Send to
Mini Paint* button, which then keeps its old behaviour.

Everything on the page is an ordinary Gradio component, coloured by the host theme's own
variables and by nothing else. No fixed white, no fixed black: a night-mode theme (the Lobe
theme, for one) reaches every corner of the tab, including the thumbnail grid, the cards,
the menu, the toast and the history list, which are server-rendered HTML the browser script
only listens to.

## The one rule

A request is an **overlay on the live WanGP page**, never a new form:

* a field you leave empty is the WanGP page's own — the prompt the page has, the picture the
  page has, whatever they are at the moment the request is queued;
* a field you fill overrides the page **for that one queued request** and is put back
  afterwards;
* the four fields that can be overridden are the prompt, the start (first) frame, the end
  (last) frame and the reference images. Nothing else on the WanGP page can be touched from
  here: not the model, not a setting, not a slider.

So the composer never has to be complete. **Add to Queue is always a button.** An entirely
empty composer asks WanGP to add the page *exactly as it is* to its queue — which is a
perfectly good thing to ask for. A card put back to *Use WanGP* with its × changes nothing
on the WanGP page; there is no "clear WanGP's field" here, on purpose.

Nothing is generated for you, nothing is aborted, no progress is watched. The request is
added to **WanGP's own queue**, through the same chain WanGP's own *Add to queue* button
runs (its trigger's `.change`, which validates the form and writes the task), and WanGP's
own validation decides whether it is taken. Press Generate in WanGP, as you always did.

## Setting it up

Open the tab. Until a folder is chosen the grid says so and the folder panel is open:
**Menu → Choose storage folder**, type a folder on the machine running Forge (not on the
device you are browsing from), and press *Use folder* — or *Create it* if it does not exist
yet. A folder that does not exist is never created behind your back; a path that is a file,
or nothing at all, is refused with its reason. Changing the folder later moves nothing: the
old folder keeps its files, the new one is read as it is.

Clipboard keeps its pictures **in that folder and nowhere else**. Files dropped into it by
hand appear on the next *Refresh* with a fresh id; a file renamed by hand keeps its id when
its bytes are the same bytes; a file removed by hand is gone from the grid on the next
Refresh, and every card and history entry that used it says so.

## The browser

**☰ Menu** holds everything the grid does not show:

* **Intercept "Send to Mini Paint"** — a switch. While it is on, the 🖌️ button under a
  txt2img / img2img / Extras result puts the picture *here* instead of on the Canvas, and
  the page switches to this tab. Off, the button does what it always did. The switch is
  saved on the Forge host, so a second browser and a Reload UI see the same setting.
* **Refresh** — read the folder again.
* **Sort ›** — by name, newest, oldest, largest, smallest. Also the dropdown on the toolbar.
* **Paste image** — reads your clipboard when the browser lets the page do that; otherwise
  a small panel opens with an ordinary paste box (Ctrl+V into it, or drop a file on it).
  Ctrl+V anywhere on the tab does the same.
* **Upload image(s)** — the file chooser, several at once.
* **Choose storage folder**, **Rename selected**, **Delete selected** — the file operations.
  A rename keeps the id and the extension and refuses a name with a separator, a leading
  dot, a reserved name or nothing before the extension; two files that want one name become
  `name` and `name (2)`. A delete asks first, removes the file, and leaves history entries
  that used it saying *Missing image*.
* **Send selected to ›** — Mini Paint, img2img, Inpaint, Extras, ImageStitch: the same
  routes the Canvas's own *Send to* takes, one picture at a time.
* **Queue Send History** — see below.

The toolbar has **+First**, **+Last**, **+Ref** (the selected picture into that card) and a
thumbnail-size slider (72–320 px, remembered). Tap a thumbnail to select it; drop one on a
card to assign it; drop a file from your desktop on a card to import it into the folder and
assign it in one go. Only PNG, JPEG and WebP are library files — an animated file, a text
file, or anything over the handoff ceilings (64 MiB, 16384 px on a side, 64 megapixels) is
refused with a sentence and never copied. A Forge PNG keeps every byte, so its generation
parameters survive; a paste and a *Send to Clipboard* from the Canvas become PNGs.

Nothing in the grid names the folder: every thumbnail is fetched by its opaque 32-character
id (`/minipaint-clipboard/image/<id>?thumb=1`), the file's name is shown as text, and the
route refuses an id that is not in the index, a name that is not a plain filename, a
symlink, and a file that has moved outside the folder (`CLIPBOARD_ASSET_OUTSIDE_ROOT`).

## The composer

Each card is in one of three states: **Use WanGP** (empty — inherit), a picture (override),
or **Missing image** (its file left the folder; press Refresh and it goes back to Use
WanGP). Each card has *Choose a file…* for a file that is not in the library yet, and × to
put it back to Use WanGP.

The prompt box's placeholder says what an empty box means: *Use current WanGP prompt*. Text
in it overrides the page's prompt for that one request — the prompt WanGP's generation
actually reads, so with WanGP's prompt wizard switched on it goes into the wizard's box.
Control characters are dropped, it is trimmed, and 4000 characters is the ceiling
(`PROMPT_TOO_LONG`).

The line above the cards says what the live WanGP page can take right now — *WanGP: ready*,
or why not (no WanGP page open, no model chosen, the bridge not answering). It is asked
when the tab opens, after every queue attempt and when a card changes, never on a timer.
A card holding a picture the current model cannot use (a text-only model and a first
frame, say) is **kept** and badged *Not used by current model*; the request is still sent
and WanGP is told to ignore that field. That is reported, not refused.

**Add to Queue** does this, in order:

1. The server turns the composer into a public request: the prompt if any, and each filled
   card as a Clipboard asset **id** — never a path. A card whose file is gone stops here
   with a sentence; nothing has been asked of WanGP and the rest of the draft is kept.
2. The browser hands the request to `window.minipaintInterop.wangp.enqueue()` — the same
   public API any other extension may call; the tab has no private shortcut.
3. The API's server half turns each asset into an ordinary WanGP handoff (a lossless PNG
   under the runtime handoff root, named by a fresh id), and the page asks the bridge inside
   WanGP to queue: the bridge writes the overrides *as replacements* into the page's own
   components, WanGP's `client_id` (set to the request id) and its `add_to_queue_trigger`,
   and WanGP's own chain runs.
4. The page then asks the bridge to **confirm**, a few times over ten seconds: *queued* when
   a task in WanGP's queue carries the request id as its client id, *refused* only when
   WanGP recorded an error for exactly that request, otherwise *unconfirmed*. The absence of
   a task is never treated as a refusal. Whichever way it ends, the overrides are put back —
   each one only where the page still holds what the bridge wrote, so an edit you made in
   WanGP meanwhile is yours and stays.
5. The status line under the button says *Added to WanGP queue.* (with any ignored field
   named), or the sentence for the code; a toast says the same over the browser; the
   handoff files are released.

One request at a time per page: a second *Add to Queue* while the first is still being
confirmed is refused with `QUEUE_BUSY` and touches nothing. Pressing the button twice with
the same composition is safe — a request id is used once, and a retry of an admitted request
is answered from the record rather than queued again.

## Queue Send History

Every request **confirmed queued** from this tab is a record: when, on which model, how
many tasks, the prompt *if you typed one here* (an inherited prompt is recorded as *Use
WanGP*, never as WanGP's text), and each slot as Use WanGP, the picture, or *ignored*. The
last 200 are kept. **Load** puts a recipe back into the composer — a slot whose picture is
gone becomes Use WanGP and says so — and queues nothing. **Delete** removes the record and
touches no file. Nothing that was refused or unconfirmed is recorded.

## Working with Mini Paint

* The Canvas's **Menu → Send to** now lists **Clipboard**: the flattened picture goes into
  the folder as a PNG named after the document (`minipaint.png` when it has no name), the
  status line says as what, and the Canvas keeps its document. It does not switch tabs.
* **Send selected to › Mini Paint** hands the picture to the Canvas through the Canvas's
  own receive chain (Layer 1 over a Background, one Undo away) and switches to it.
* With the **intercept** on, the gallery's 🖌️ button imports the original output file when
  Forge proves which file it is (its generation parameters survive) and the decoded pixels
  as a PNG otherwise, and switches to the Clipboard tab. If Clipboard cannot take the
  picture — no folder chosen, a refused file — it goes to the Canvas as before, with the
  reason on the Canvas's status line.

## Where things live

| what | where |
| --- | --- |
| the folder, the intercept switch, the sort and the thumbnail size | `<Forge data_path>/a1111-mini-paint-NEO/clipboard.json` |
| the index: id, filename, size, dimensions, digest, source per file | `…/clipboard-index.json` (rebuilt from the folder on Refresh) |
| the composer's draft | `…/clipboard-draft.json` |
| Queue Send History | `…/clipboard-history.json` |
| the pictures | the storage folder you chose, and only there |
| thumbnails | in memory, rebuilt as needed; never on disk |
| images staged through the public API | `…/runtime/staging/<32 hex>.png`, swept after 30 minutes |
| images prepared for one queue request | `…/runtime/handoff/<32 hex>.png`, released when the request ends |
| every step, scrubbed | `extensions/a1111-mini-paint-NEO/logs/wangp-log.txt` (`clipboard`, `browser` and `wangp` columns) |

A document that will not parse is moved aside as `<name>.broken-<stamp>.json` and started
afresh, and the console says so; nothing is ever half-written (every write is atomic).

## What is deliberately not stored or logged

* No **path** leaves the server: the browser, the public API and the wire protocol see ids.
* No **prompt text** is logged, anywhere, by any side — the journal says `overrides prompt`,
  never what it was. WanGP's own prompt is never copied into a Clipboard file.
* No **filename** is logged; the journal names an asset by the first eight characters of
  its id.
* The **request id**, the staged **tokens** and the handoff ids are in no config file.
* The **draft** and the **history** hold a prompt only when you typed one into the tab.

## Reading a failure code

The codes and their sentences are in `minipaint_neo/wangp/errors.py`, beside the WanGP
tab's own; the tab, the toast and the log say the same thing about the same failure.

| code | what it means |
| --- | --- |
| `CLIPBOARD_NOT_CONFIGURED` | no storage folder yet. Menu → Choose storage folder. |
| `CLIPBOARD_ASSET_UNKNOWN` | that id is not in the index — the file left the folder. Refresh. |
| `CLIPBOARD_ASSET_OUTSIDE_ROOT` | the indexed entry no longer resolves inside the folder (a symlink, a move). Not used. |
| `REQUEST_INVALID` | the request is not one the API carries: a bad id, a wrong kind, an unreadable shape. |
| `PROMPT_TOO_LONG` | over 4000 characters after cleaning. |
| `IMAGE_STAGE_INVALID` / `IMAGE_STAGE_EXPIRED` | the bytes were not a PNG/JPEG/WebP within the ceilings, or the staged file was swept. |
| `IFRAME_NOT_READY` | no live WanGP page in this Forge tab. Open the WanGP tab and choose a model. |
| `BRIDGE_COMPONENT_INCOMPATIBLE` | the bridge inside this WanGP build lacks one of the six queue components; image send still works, the queue does not. |
| `QUEUE_BUSY` | the previous request from this page is still being confirmed. |
| `REQUEST_ID_CONFLICT` | a request id was reused for a different payload. Mint a new id. |
| `QUEUE_REQUEST_REFUSED` | the bridge did not take the request; the code it named is in the log. |
| `WANGP_VALIDATION_REFUSED` | WanGP's own validation declined it; the WanGP page has the detail. |
| `ADMISSION_UNCONFIRMED` | no task carrying the request appeared within the wait and WanGP recorded no error. It may still be queued: look at WanGP's queue before trying again. |
| `BRIDGE_SESSION_MISMATCH` | the request was prepared for another WanGP page. |

## Theming

`style.css` scopes every Clipboard rule to `#minipaint_clipboard_root` and decides layout
and sizes only. Colours come from Gradio's theme variables — `--body-text-color`,
`--background-fill-primary`, `--block-background-fill`, `--border-color-primary`,
`--color-accent`, `--error-text-color` and their kin — with neutral, translucent fallbacks
for a theme that lacks one; `tests/test_clipboard_ui.py` refuses a white, a `#fff`, a
black or a fixed background in that block. The thumbnail size is one CSS variable the
browser sets from the slider. On a narrow tab the two columns stack.

## The public API for other extensions

`window.minipaintInterop` is on every Forge page once the extension has loaded. It is
versioned, contract `minipaint.wangp.queue/v1`, and it is the only way into the queue —
the Clipboard tab uses it too.

```js
const api = window.minipaintInterop;              // { version: 1, contract, wangp: {...}, message(code) }

const caps = await api.wangp.capabilities();      // advisory: { ok, ready, model, inputs: { start: {supported}, end, references } }

const staged = await api.wangp.stageImage(blob);  // a Blob, File, ArrayBuffer, canvas or data: URL
// -> { ok: true, image: { kind: "staged", id: "<32 hex>" }, width, height }  (the bytes never cross postMessage)

const result = await api.wangp.enqueue({
    prompt: "a lighthouse at dusk",               // omit, null or "" -> the page's own prompt
    images: {
        start: staged.image,                      // { kind: "staged", id } or { kind: "clipboard_asset", id }
        references: [staged.image]                // up to 16; omit -> the page's own
        // end omitted -> the page's own
    }
});
// -> { ok: true, status: "queued", request_id, tasks_added, model, applied, inherited, ignored }
// -> { ok: false, status: "refused" | "unconfirmed", request_id, code, message }
```

What the contract promises:

* **Sparse.** Omitted, `null` and empty mean *inherit*. Only `prompt`, `images.start`,
  `images.end` and `images.references` exist; anything else is `REQUEST_INVALID`.
* **Ids, never paths.** A handle is a kind and a 32-lowercase-hex id; `stageImage` is how
  bytes become one. `GET /minipaint-interop/contract` says the version, the ceilings and the
  kinds.
* **FIFO.** Calls to `enqueue()` are serialised on one queue in the page; a request owns the
  WanGP form until it is confirmed or its time is up, then the next one runs.
* **Idempotent by id.** Pass your own `request_id` (32 lowercase hex) to make a retry safe;
  omit it and one is minted.
* **A bounded answer.** `enqueue()` resolves within about forty seconds at most — thirty for
  the bridge to answer, ten to confirm — with `queued`, `refused` (with the code) or
  `unconfirmed`. It never claims more than WanGP proved.
* **Not a generation API.** No Generate, no abort, no progress, no model or setting; the
  request adds to WanGP's queue and nothing else.

The routes behind it (`/minipaint-interop/stage`, `/prepare`, `/release`, `/contract`,
`/minipaint-clipboard/image/…`, `/minipaint-clipboard/import`) sit behind the same sign-in
gate as the WanGP proxy: when Forge has a login, a request without it is refused.

From Python, the same halves are `minipaint_neo.interop.stage_image(pil)`,
`stage_bytes(data, content_type)`, `normalize_public_request(raw)`, `prepare(request)` and
`release(ids)`; the Clipboard library is `minipaint_neo.clipboard.store.store()`.

## Known limits

* **Not yet proven on real hardware.** Like the WanGP tab, the queue was written against
  Wan2GP's source (commit `362c346`: `add_to_queue_trigger.change` runs
  `validate_wizard_prompt → save_inputs → process_prompt_and_add_tasks`, and `save_inputs`
  copies `client_id` into every task). The shape of `queue_errors` is read both ways it has
  been seen and is marked `VERIFY ON A REAL INSTALL`; a build with neither shape answers
  *unconfirmed* for a failed validation rather than the wrong thing.
* **One request per page at a time.** By design, and reported as `QUEUE_BUSY`.
* **The browser's clipboard.** Reading it directly needs a browser that allows the page to;
  the paste panel is the fallback and always works.
* **Gradio's cache is lossy.** A picture written into a WanGP gallery comes back from Gradio
  as a WebP in its cache, so the bridge compares galleries by a coarse signature with a
  tolerance, not by exact pixels; a picture you swapped meanwhile is still recognised as
  yours.
* **Playwright.** The browser smoke test (`tests/browser_smoke.py`) does not yet cover the
  Clipboard tab.
