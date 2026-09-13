# The Clipboard tab and the WanGP queue API — an operator's guide

This describes what the extension does when the **Clipboard** tab is used and when
another extension calls the public queue API: what it shows, what it writes, what it
refuses to write, and what you can check for yourself. It is written against the code in
`minipaint_neo/clipboard/`, `minipaint_neo/interop.py`, `javascript/minipaint_clipboard.js`,
`javascript/minipaint_interop.js`, the queue half of `wan2gp_bridge/` and the registration
in `scripts/mini_paint.py`. The WanGP tab it builds on has its own guide,
`docs/wangp/README.md`; the two design documents it implements are kept for the record
(the Clipboard design intent, and `docs/wangp/START_AND_OUTBOX.txt`, which is marked
deprecated and says where the implementation departs from it). Where a document and the
code differ, this file follows the code and says so.

## What it is

A third top-level tab, **Clipboard** (id `minipaint_clipboard`), beside Mini Paint and
WanGP. On the left, about two thirds of the width, a small file browser over one folder on
the machine running Forge; on the right, a small **WanGP request** composer: three cards —
*First Frame*, *Last Frame*, *Reference* — a prompt box, one button, **Add to Queue**, and
under it the **Queue**: every request sent from this Forge, newest first.

It is registered as a third, independent integration. If any part of it fails to import
or to build, one line says so in the WebUI console, the tab shows a note under the same
label, and Mini Paint and WanGP load exactly as before — including the gallery's *Send to
Mini Paint* button, which then keeps its old behaviour.

Everything on the page is an ordinary Gradio component, coloured by the host theme's own
variables and by nothing else. No fixed white, no fixed black: a night-mode theme (the Lobe
theme, for one) reaches every corner of the tab, including the thumbnail grid, the cards,
the menu, the toast, the queue and the history list, which are server-rendered HTML the
browser script only listens to.

## The one rule

A request is an **overlay on the live WanGP page**, never a new form:

* a field you leave empty is the WanGP page's own — the prompt the page has, the picture the
  page has, whatever they are at the moment the request is sent;
* a field you fill overrides the page **for that one task** and is put back afterwards;
* the four fields that can be overridden are the prompt, the start (first) frame, the end
  (last) frame and the reference images. Nothing else on the WanGP page can be touched from
  here: not the model, not a setting, not a slider.

So the composer never has to be complete. An entirely empty composer asks WanGP to run the
page *exactly as it is* — which is a perfectly good thing to ask for. A card put back to
*Use WanGP* with its × changes nothing on the WanGP page; there is no "clear WanGP's field"
here, on purpose.

**A request starts WanGP when WanGP is idle, and joins it when it is not.** That decision
is made inside WanGP, by the bridge, from Wan2GP's own process-wide "is a generation
running" flag — never by the page guessing from a button, and never by the caller. The
first request to find WanGP idle runs WanGP's own generate chain, exactly as its Generate
button would; the requests behind it run WanGP's add-to-queue chain and join that run.
When the flag cannot be read, or the installed bridge cannot start a run, the request is
staged rather than started: a task that waits costs a click, a second concurrent run costs
the loaded model. A caller that wants staging only says `start: "never"`.

Nothing is aborted and no progress is watched. WanGP's own validation decides whether a
task is taken, and WanGP's own queue and status show what it is doing.

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
in it overrides the page's prompt for that one task — the prompt WanGP's generation
actually reads, so with WanGP's prompt wizard switched on it goes into the wizard's box.
Control characters are dropped, it is trimmed, and 4000 characters is the ceiling
(`PROMPT_TOO_LONG`).

The line above the cards says what the live WanGP page can take right now — the model, the
inputs it takes, whether it is *generating now* (new requests join the run) or *idle* (the
next request starts one) — or why it cannot be asked (no WanGP page open, no model chosen,
the bridge not answering). It is asked when the tab opens, after every job ends and when a
card changes, never on a timer. A card holding a picture the current model cannot use (a
text-only model and a first frame, say) is **kept** and badged *Not used by current model*;
the request is still sent and WanGP is told to ignore that field. That is reported, not
refused.

**Add to Queue** is a button while the WanGP this extension manages is running. While it is
not, the button reads *WanGP is not running* and is off, and a press that reaches the
server anyway is refused with `WANGP_NOT_RUNNING` rather than stored — nothing is queued
for a process that may never come. Open the WanGP tab, let it start, and the button comes
back on the next refresh.

A press does this, in order:

1. The server turns the composer into a public request: the prompt if any, each filled
   card as a Clipboard asset **id** — never a path — and `start: "auto"`. A card whose file
   is gone stops here with a sentence; nothing has been stored and the rest of the draft
   is kept.
2. The request becomes a **job in the server's queue outbox**, owned by this browser page,
   and the press returns at once. Press again as often as you like; each press is another
   job in order.
3. This page's pump asks the server for its next job when it is its turn (one job is sent
   at a time across every browser page on this Forge, in the order pressed), and runs it
   through `window.minipaintInterop` — the same public API any other extension may call;
   the tab has no private shortcut. The API's server half turns each asset into an ordinary
   WanGP handoff (a lossless PNG under the runtime handoff root, named by a fresh id); the
   page asks the bridge inside WanGP to queue; the bridge writes the overrides *as
   replacements* into the page's own components, WanGP's `client_id` (set to the request
   id), and either WanGP's generate trigger (WanGP idle) or its add-to-queue trigger (WanGP
   busy, or the flag unreadable); WanGP's own chain runs.
4. The page asks the bridge to **confirm**, a few times over ten seconds: *started* when the
   request's task is the one at the head of a running loop, *queued* when a task in WanGP's
   queue carries the request id (with how many sit ahead of it), *refused* only when WanGP
   recorded an error for exactly that request, otherwise *unconfirmed*. The absence of a
   task is never treated as a refusal. Whichever way it ends, the overrides are put back —
   each one only where the page still holds what the bridge wrote, so an edit you made in
   WanGP meanwhile is yours and stays.
5. The page reports the outcome to the server, which re-renders the Queue and, for a
   started or queued job, writes the history record. The status line under the button
   says *WanGP started generating it.* or *Added to WanGP queue.* (with any ignored field
   named), or the sentence for the code; a toast says the same over the browser; the
   handoff files are released.

## The Queue

Under the button, every job the server holds, newest first: its state (*Waiting*,
*Sending*, *Generating*, *Queued*, *Refused*, *Unconfirmed*, *Cancelled*), when it was
pressed, the prompt typed for it (or *Prompt: Use WanGP*), which fields it supplies, and how
it ended. And what a person may still do:

* **Cancel** — a waiting job. A job being sent is not ours to stop.
* **Retry** — a refused or cancelled job, sent again *as a new request* by the page that
  pressed Retry. The machine never retries anything by itself.
* **Retry anyway** — an unconfirmed job. *Unconfirmed* means the bridge could not prove
  either way whether WanGP took the task, and a retry may queue it twice; look at WanGP's
  queue first. This is a rule, not a default.
* **Run from this page** — a waiting job composed on a browser page that is gone (closed,
  or not asking). A job is normally run only by the page that composed it, so that it
  inherits the WanGP settings its user is looking at; taking it over means it inherits
  *this* page's instead, which is why a person decides.

What that buys you with more than one browser: each page has its own identity (kept per
browser tab, so a reload resumes its jobs), all pages share one line at the server, one
job is sent at a time whichever page pressed it, a page that stops asking does not hold the
others up, and no page holds a queue of its own — a refresh, a closed tab or a second
browser cannot lose or duplicate work. A job whose page went away mid-send is marked
unconfirmed when the overlay had already been written and returned to waiting when it had
not; a job in flight when WanGP restarts is marked unconfirmed (`WANGP_RESTARTED`).

## Queue Send History

Every request **confirmed started or queued** from this tab is a record: when, on which
model, how many tasks, the prompt *if you typed one here* (an inherited prompt is recorded
as *Use WanGP*, never as WanGP's text), and each slot as Use WanGP, the picture, or
*ignored*. The last 200 are kept. **Load** puts a recipe back into the composer — a slot
whose picture is gone becomes Use WanGP and says so — and queues nothing. **Delete**
removes the record and touches no file. Nothing that was refused or unconfirmed is
recorded, and a request from another extension is in the Queue but never in this history.

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
| the queue outbox: every press as a job, with its request, its state and how it ended | `…/clipboard-outbox.json` (terminal jobs kept a week, the list capped) |
| Queue Send History | `…/clipboard-history.json` |
| the pictures | the storage folder you chose, and only there |
| thumbnails | in memory, rebuilt as needed; never on disk |
| images staged through the public API | `…/runtime/staging/<32 hex>.png`, swept after 30 minutes |
| images prepared for one queue request | `…/runtime/handoff/<32 hex>.png`, released when the request ends |
| every step, scrubbed | `extensions/a1111-mini-paint-NEO/logs/wangp-log.txt` (`clipboard`, `outbox`, `interop`, `browser` and `wangp` columns) |

A document that will not parse is moved aside as `<name>.broken-<stamp>.json` and started
afresh, and the console says so; nothing is ever half-written (every write is atomic).

## What is deliberately not stored or logged

* No **path** leaves the server: the browser, the public API and the wire protocol see ids.
* No **prompt text** is logged, anywhere, by any side — the journal says `overrides prompt`,
  never what it was. WanGP's own prompt is never copied into a Clipboard file.
* No **filename** is logged; the journal names an asset by the first eight characters of
  its id, a job by the first eight of its.
* The **request id**, the staged **tokens**, the handoff ids and the outbox **lease tokens**
  are in no config file and never shown to a page other than the one they were issued to.
* The **draft** and the **history** hold a prompt only when you typed one into the tab. The
  **outbox** holds the prompt of a job because the job is a snapshot of the composer at
  press time and must survive the composer changing afterwards; it is shown in the Queue,
  on your own screen, and nowhere else.

## Reading a failure code

The codes and their sentences are in `minipaint_neo/wangp/errors.py`, beside the WanGP
tab's own; the tab, the toast and the log say the same thing about the same failure.

| code | what it means |
| --- | --- |
| `WANGP_NOT_RUNNING` | the WanGP this extension manages is not serving. The button is off; a press is refused and nothing is stored. Open the WanGP tab and let it start. |
| `CLIPBOARD_NOT_CONFIGURED` | no storage folder yet. Menu → Choose storage folder. |
| `CLIPBOARD_ASSET_UNKNOWN` | that id is not in the index — the file left the folder. Refresh. |
| `CLIPBOARD_ASSET_OUTSIDE_ROOT` | the indexed entry no longer resolves inside the folder (a symlink, a move). Not used. |
| `REQUEST_INVALID` | the request is not one the API carries: a bad id, a wrong kind, a start mode that is not `auto` or `never`, an unreadable shape. |
| `PROMPT_TOO_LONG` | over 4000 characters after cleaning. |
| `IMAGE_STAGE_INVALID` / `IMAGE_STAGE_EXPIRED` | the bytes were not a PNG/JPEG/WebP within the ceilings, or the staged file was swept. |
| `IFRAME_NOT_READY` | no live WanGP page in this Forge tab. The job waits; open the WanGP tab and choose a model. |
| `BRIDGE_COMPONENT_INCOMPATIBLE` | the bridge inside this WanGP build lacks one of the six queue components; image send still works, the queue does not. |
| `QUEUE_BUSY` | the previous request from this page is still being confirmed, or the outbox holds as many waiting jobs as it will take. |
| `QUEUE_JOB_PENDING` | an `enqueue()` wait ran out while its job was still waiting its turn; the job id is in the answer and the job is still in the Queue. |
| `QUEUE_JOB_UNKNOWN` | that job is no longer in the outbox (pruned, or never there). |
| `REQUEST_ID_CONFLICT` | a request id was reused for a different payload. Mint a new id. |
| `QUEUE_REQUEST_REFUSED` | the bridge did not take the request; the code it named is in the log. |
| `WANGP_VALIDATION_REFUSED` | WanGP's own validation declined it; the WanGP page has the detail. |
| `ADMISSION_UNCONFIRMED` | no task carrying the request appeared within the wait and WanGP recorded no error. It may still be queued: look at WanGP's queue before *Retry anyway*. |
| `WANGP_RESTARTED` | WanGP restarted while the job was on its way; it is unconfirmed for the same reason. |
| `BRIDGE_SESSION_MISMATCH` | the request was prepared for another WanGP page. |

## Theming

`style.css` scopes every Clipboard rule to `#minipaint_clipboard_root` and decides layout
and sizes only. Colours come from Gradio's theme variables — `--body-text-color`,
`--background-fill-primary`, `--block-background-fill`, `--border-color-primary`,
`--color-accent`, `--error-text-color` and their kin — with neutral, translucent fallbacks
for a theme that lacks one; `tests/test_clipboard_ui.py` refuses a white, a `#fff`, a
black or a fixed background in that block, the queue list included. The thumbnail size is
one CSS variable the browser sets from the slider. On a narrow tab the two columns stack.

## The public API for other extensions

`window.minipaintInterop` is on every Forge page once the extension has loaded. It is
versioned, contract `minipaint.wangp.queue/v1`, and it is the only way into the queue —
the Clipboard tab uses it too, and its presses and a caller's requests share one line.

```js
const api = window.minipaintInterop;              // { version: 1, contract, wangp: {...}, message(code) }

const caps = await api.wangp.capabilities();      // advisory: { ok, ready, queue, start, generation_running, model, inputs: { start, end, references } }

const staged = await api.wangp.stageImage(blob);  // a Blob, File, ArrayBuffer, canvas or data: URL
// -> { ok: true, image: { kind: "staged", id: "<32 hex>" }, width, height }  (the bytes never cross postMessage)

const result = await api.wangp.enqueue({
    prompt: "a lighthouse at dusk",               // omit, null or "" -> the page's own prompt
    images: {
        start: staged.image,                      // { kind: "staged", id } or { kind: "clipboard_asset", id }
        references: [staged.image]                // up to 16; omit -> the page's own
        // end omitted -> the page's own
    },
    start: "auto"                                 // the default: start WanGP if idle, join it if not; "never" stages only
});
// -> { ok: true,  status: "started" | "queued", request_id, job_id, tasks_added, queue_depth, route, model, applied, inherited, ignored }
// -> { ok: false, status: "refused" | "unconfirmed" | "pending", request_id, job_id, code, message }

const ticket = await api.wangp.enqueue(request, { wait: false });   // -> { ok: false, status: "pending", job_id, code: "QUEUE_JOB_PENDING" } at once
const all = await api.wangp.jobs();                                // { ok, running, jobs: [...] }
await api.wangp.cancel(jobId); await api.wangp.retry(jobId); await api.wangp.adopt(jobId);
api.wangp.pump();                                                  // run this page's waiting jobs now (idempotent)
```

What the contract promises:

* **Sparse.** Omitted, `null` and empty mean *inherit*. Only `prompt`, `images.start`,
  `images.end`, `images.references` and `start` exist; anything else is `REQUEST_INVALID`.
* **Ids, never paths.** A handle is a kind and a 32-lowercase-hex id; `stageImage` is how
  bytes become one. `GET /minipaint-interop/contract` says the version, the ceilings, the
  kinds, the start modes and where the outbox is.
* **One line, server-owned.** `enqueue()` submits a job; the server sends one job at a time
  across every page in submission order; this page runs only its own jobs, against the WanGP
  page in this document. `enqueue()` resolves when the job has ended, or after `timeoutMs`
  (five minutes by default) with `pending` and the job id; `{wait: false}` returns the job
  id at once.
* **Idempotent by id.** Pass your own `request_id` (32 lowercase hex) to make a retry safe;
  omit it and one is minted. The same id with another payload — a different start mode
  included — is `REQUEST_ID_CONFLICT`.
* **Honest answers.** `started` and `queued` only on WanGP's own evidence, `refused` only
  on WanGP's correlated refusal or the bridge's own, `unconfirmed` otherwise; never a retry
  the machine decided on.
* **Refused when WanGP is not running.** `WANGP_NOT_RUNNING`, and nothing stored.
* **Not a generation API.** No abort, no progress, no model or setting; a request starts a
  run only when WanGP is idle and only through WanGP's own generate trigger.

The routes behind it (`/minipaint-interop/stage`, `/prepare`, `/release`, `/contract`,
`/minipaint-interop/outbox` and its `/submit`, `/claim`, `/report`, `/cancel`, `/retry`,
`/adopt`, `/minipaint-clipboard/image/…`, `/minipaint-clipboard/import`) sit behind the
same sign-in gate as the WanGP proxy: when Forge has a login, a request without it is
refused.

From Python, the same halves are `minipaint_neo.interop.stage_image(pil)`,
`stage_bytes(data, content_type)`, `normalize_public_request(raw)`, `prepare(request)` and
`release(ids)`; the outbox is `minipaint_neo.clipboard.outbox` (`submit`, `claim`, `report`,
`cancel`, `retry`, `adopt`, `jobs`); the Clipboard library is
`minipaint_neo.clipboard.store.store()`.

## Known limits

* **Not yet proven on real hardware.** Like the WanGP tab, the queue was written against
  Wan2GP's source (commit `362c346`: `add_to_queue_trigger.change` runs
  `validate_wizard_prompt → save_inputs → process_prompt_and_add_tasks`,
  `generate_trigger.change` runs the same and then `process_tasks`, `save_inputs` copies
  `client_id` into every task, and `is_generation_in_progress()` is the module function
  `process_tasks` raises). The shape of `queue_errors` is read both ways it has been seen
  and is marked `VERIFY ON A REAL INSTALL`; a build with neither shape answers *unconfirmed*
  for a failed validation rather than the wrong thing. `docs/wangp/PHASE0.md` has the checks.
* **One request at a time.** By design: across every page, one job is sent at a time;
  within one WanGP page, one request owns the form until it is confirmed (`QUEUE_BUSY`).
* **A person and the bridge in the same instant.** The bridge decides from WanGP's flag and
  writes the trigger a round trip later; a person pressing Generate inside that window
  could start a second run. The window is small, it is logged, and WanGP's own page-level
  guard covers the page the request ran in. It is not excluded.
* **A page that is gone keeps its jobs.** Jobs run on the page that composed them; a closed
  tab's waiting jobs wait for *Run from this page*, on purpose.
* **The browser's clipboard.** Reading it directly needs a browser that allows the page to;
  the paste panel is the fallback and always works.
* **Gradio's cache is lossy.** A picture written into a WanGP gallery comes back from Gradio
  as a WebP in its cache, so the bridge compares galleries by a coarse signature with a
  tolerance, not by exact pixels; a picture you swapped meanwhile is still recognised as
  yours.
* **Playwright.** The browser smoke test (`tests/browser_smoke.py`) does not yet cover the
  Clipboard tab or the pump.
