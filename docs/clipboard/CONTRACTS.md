# Clipboard and the WanGP queue: module contracts

The one place the modules agree on each other's names, in the same spirit as
`docs/wangp/CONTRACTS.md`, which this extends. Every signature here is
load-bearing: another module, the tab, the browser, a third-party extension
or a test calls it. House style as there: `from __future__ import
annotations`, docstrings that say *why*, `typing.Optional[...]`, nothing the
WebUI does not already ship, nothing that polls forever, nothing that can
raise into WanGP.

---

## The public contract — `minipaint.wangp.queue/v1` (protocol 4)

```
window.minipaintInterop = {
    version: 1,
    contract: "minipaint.wangp.queue/v1",
    wangp: {
        capabilities(): Promise<{ ok, ready, queue, start, generation_running: bool|null, model, inputs: {start, end, references: {supported}}, api_version } | { ok: false, code, message }>,
        stageImage(input: Blob | File | ArrayBuffer | TypedArray | HTMLCanvasElement | dataURL, options?): Promise<{ ok, image: {kind: "staged", id}, width, height } | { ok: false, code, message }>,
        enqueue(request, options?: { wait?: bool = true, timeoutMs?: number = 300000 }): Promise<Result>,
        jobs(): Promise<{ ok, running, counts, jobs: [Job] }>,
        cancel(jobId), retry(jobId), adopt(jobId): Promise<{ ok, job } | { ok: false, code, message }>,
        pump(): void,                       // run this page's waiting jobs now; idempotent, bounded
        pageId(): <32 hex>,                 // this page's identity, kept per browser tab
    },
    message(code): string,
}

request = {
    request_id?: <32 lowercase hex>,          // minted when absent; the same id + the same payload is one request
    prompt?: string,                          // omitted / null / "" -> inherit; 4000 chars after cleaning
    images?: {
        start?: Handle, end?: Handle,         // omitted / null -> inherit
        references?: Handle[],                // omitted / null / [] -> inherit; at most 16
    },
    start?: "auto" | "never",                 // omitted -> "auto"
}
Handle = { kind: "staged" | "clipboard_asset", id: <32 lowercase hex> }

Result (ok)  = { ok: true,  status: "started" | "queued", request_id, job_id, tasks_added, queue_depth: int|null, route: "generate"|"queue"|"",
                 model: {type, label, family}, applied: {prompt, start, end: bool, references: int}, inherited: [field], ignored: [{field, code}] }
Result (not) = { ok: false, status: "refused" | "unconfirmed" | "pending", request_id, job_id, code, message }

Job = { job_id: <16 hex>, request, page, origin: "clipboard"|"api", state: pending|sending|queued|started|failed|unconfirmed|cancelled,
        created_at, updated_at, created, updated, attempts, sent, leased_until, result, error: {code, message}|null, retry_of, summary, history_recorded }
```

Guarantees: sparse (omitted is inherit), ids never paths, one line owned by
the server (one lease at a time across every page, submission order, a page
runs only its own jobs), idempotent by request id (`REQUEST_ID_CONFLICT` for
the same id with another payload, the start mode included), a bounded
answer (`QUEUE_REQUEST_TIMEOUT_MS` + `QUEUE_CONFIRM_TIMEOUT_MS` per job;
`timeoutMs` for the wait in line), `started`/`queued` only on WanGP's own
evidence, `refused` only on correlated evidence, `unconfirmed` otherwise,
never a machine retry, `WANGP_NOT_RUNNING` rather than storage while the
managed WanGP is not serving. A run is started only through WanGP's own
generate trigger, only when WanGP's own process-wide flag says nothing is
generating, and never by the caller's or a page's decision. No abort, no
progress, no settings.

Codes a caller can meet: `REQUEST_INVALID`, `PROMPT_TOO_LONG`,
`IMAGE_STAGE_INVALID`, `IMAGE_STAGE_EXPIRED`, `CLIPBOARD_NOT_CONFIGURED`,
`CLIPBOARD_ASSET_UNKNOWN`, `CLIPBOARD_ASSET_OUTSIDE_ROOT`, `IFRAME_NOT_READY`,
`BRIDGE_COMPONENT_INCOMPATIBLE`, `BRIDGE_SESSION_MISMATCH`, `WANGP_NOT_RUNNING`,
`WANGP_RESTARTED`, `QUEUE_BUSY`, `QUEUE_JOB_PENDING`, `QUEUE_JOB_UNKNOWN`,
`REQUEST_ID_CONFLICT`, `QUEUE_REQUEST_REFUSED`, `WANGP_VALIDATION_REFUSED`,
`ADMISSION_UNCONFIRMED`, `HANDOFF_*`, `AUTH_BOUNDARY_FAILED`, `INTERNAL_ERROR`.
Every one has a sentence in `minipaint_neo/wangp/errors.py`.

## `javascript/minipaint_interop.js`

Defines `window.minipaintInterop` (above). `normaliseRequest(raw)` applies the
sparse rules, mints an id and defaults `start` to `auto`; `stageImage` posts
bytes to `STAGE_ROUTE` (32 MiB ceiling, `image/*` only). `enqueue` posts
`{request, page, origin: "api"}` to `OUTBOX_SUBMIT_ROUTE`, kicks `pump()`, and
resolves from the job's terminal state (waiters keyed by job id; a timed-out
wait reads `jobs()` once and answers `pending` with the job id). `pump()`
posts `{page}` to `OUTBOX_CLAIM_ROUTE` in a loop: a job is run with
`execute(request, {onAdmitted})` (prepare the images, `queueAndConfirm`,
release the handoffs), reported `sent` the moment the bridge admits it and
`done` with the public result; a `wait` answer is honoured only while this
page has jobs pending and at most `PUMP_MAX_WAITS` times; `empty` or a
refusal (`WANGP_NOT_RUNNING` included) stops it. It dispatches
`minipaint:outbox` CustomEvents on `document` (`submitted`, `sending`,
`done`, `changed`, `stopped`) with the job. `pageId()` lives in
`sessionStorage` under `minipaint.interop.page`. Journal lines go through
`minipaintWanGP.note` under `browser` (`run <id8>: prompt, start; start auto
(N image(s) prepared)`, `pump: stopped - CODE`).

## `minipaint_neo/interop.py` — the API's server half

```python
STAGE_ROUTE   = "/minipaint-interop/stage"      # POST bytes (Content-Type image/png|jpeg|webp|octet-stream) -> {ok, image:{kind:"staged", id}, width, height}
PREPARE_ROUTE = "/minipaint-interop/prepare"    # POST {request} -> {ok, request: wire} | {ok:false, code, message}
RELEASE_ROUTE = "/minipaint-interop/release"    # POST {handoff_ids} -> {ok, released}
CONTRACT_ROUTE = "/minipaint-interop/contract"  # GET -> contract()  (adds start_modes and outbox)
OUTBOX_ROUTE  = "/minipaint-interop/outbox"     # GET -> {ok, jobs, running, counts}
OUTBOX_SUBMIT_ROUTE = OUTBOX_ROUTE + "/submit"  # POST {request, page, origin} -> {ok, job}         409 WANGP_NOT_RUNNING / QUEUE_BUSY, 400 REQUEST_INVALID
OUTBOX_CLAIM_ROUTE  = OUTBOX_ROUTE + "/claim"   # POST {page} -> {ok, job, lease, pending} | {ok, wait, reason, pending} | {ok, empty}   409 WANGP_NOT_RUNNING
OUTBOX_REPORT_ROUTE = OUTBOX_ROUTE + "/report"  # POST {job_id, lease, phase: sent|done, result?} -> {ok, job}
OUTBOX_CANCEL_ROUTE / OUTBOX_RETRY_ROUTE / OUTBOX_ADOPT_ROUTE   # POST {job_id[, page]} -> {ok, job}   404 QUEUE_JOB_UNKNOWN
STAGE_MAX_BYTES = MAX_HANDOFF_BYTES // 2; STAGE_MAX_AGE_SECONDS = 30 * 60
KIND_STAGED = "staged"; KIND_CLIPBOARD = "clipboard_asset"

def contract() -> dict
def staging_root() -> Path                                   # <runtime dir>/staging, 0700
def stage_bytes(data: bytes, content_type: str = "") -> dict # decodes, refuses non-images, writes a lossless PNG behind a token
def stage_image(image) -> dict                               # the Python caller's stageImage
def resolve_staged(token) -> Path                            # IMAGE_STAGE_EXPIRED when gone; containment checked
def discard_staged(token) -> None
def sweep_staging(max_age_seconds=STAGE_MAX_AGE_SECONDS, now=None) -> int
def normalize_handle(raw) -> dict                            # {kind, id} or REQUEST_INVALID
def normalize_public_request(raw) -> dict                    # sparse rules; mints request_id; PROMPT_TOO_LONG; at most 16 references; start auto|never
def prepare(request) -> dict                                 # -> {request_id, start, prompt?, start_handoff_id?, end_handoff_id?, reference_handoff_ids?, handoff_ids}
def release(handoff_ids) -> int
def install(app) -> None                                     # once; routes ahead of the host's catch-all; behind the sign-in gate
def register(script_callbacks) -> None                       # on_app_started -> install
```

`prepare` opens a `staged` handle from the staging root and a
`clipboard_asset` handle through `clipboard.store.open_image`, writes each as
an ordinary handoff (`wangp.handoff.write`) and, on any failure, releases what
it had already written: a half-prepared request is not queued. The outbox
routes call `clipboard.outbox` (below); `claim` refuses while the managed
WanGP is not running, which is what stops a page's pump.

## `minipaint_neo/clipboard/outbox.py` — the queue outbox

```python
OUTBOX_NAME = "clipboard-outbox.json"           # {"schema": 1, "jobs": [...]}, atomic, quarantined when broken
PENDING, SENDING, QUEUED, STARTED, FAILED, UNCONFIRMED, CANCELLED; TERMINAL; POSITIVE = (QUEUED, STARTED)
ORIGIN_CLIPBOARD = "clipboard"; ORIGIN_API = "api"
LEASE_SECONDS = 90.0; PAGE_ACTIVE_SECONDS = 15.0; WAIT_BUSY_MS = 400; WAIT_TURN_MS = 250
MAX_JOBS = 500; MAX_PENDING = 200; KEEP_TERMINAL_SECONDS = 7 days; PAGE_RE = 8..32 lowercase hex
def use_clock(fn); def use_running(fn); def reset_for_tests()      # seams
def wangp_running() -> bool                                        # runtime.current().snapshot()["running"], contained
def submit(request, page, origin="clipboard", require_running=True) -> Job   # normalises through interop; WANGP_NOT_RUNNING; QUEUE_BUSY past MAX_PENDING
def claim(page) -> {"job", "lease", "pending"} | {"wait", "reason": "busy"|"turn", "pending"} | {"empty": True, ...}
def report(job_id, lease, phase, payload=None) -> Job              # "sent": the overlay was written; "done": sanitize_result(payload) -> queued|started|failed|unconfirmed
def cancel(job_id) -> Job                                          # pending only; QUEUE_BUSY while sending
def retry(job_id, page) -> Job                                     # failed | unconfirmed | cancelled -> a NEW job (fresh request_id, retry_of) for this page
def adopt(job_id, page) -> Job                                     # a pending job re-owned by this page
def on_wangp_restart() -> int                                      # sending -> unconfirmed (WANGP_RESTARTED)
def jobs() -> [Job]; def get(job_id); def counts(); def pending_count(page=None)
def unrecorded(origin="clipboard") -> [Job]; def mark_recorded(job_ids)   # the tab's history bookkeeping, once
def sanitize_result(raw) -> dict; def summary_of(request) -> dict; def public(job) -> Job
```

The rules: one lease at a time across every page; the head of the line is
the oldest pending job of a page that has claimed within
`PAGE_ACTIVE_SECONDS` (or this page's own), so a page that stopped asking
does not hold the others up; a lease that expires returns an unsent job to
pending and marks a sent one unconfirmed; an unconfirmed job is never
claimed again by itself. The document holds the request (prompt included -
a snapshot of the composer at press time), never a lease token in what a
page is shown, never a path. `runtime.emergency_restart` calls
`on_wangp_restart` through a contained import.

## `minipaint_neo/wangp/protocol.py` — protocol 4

Added to the SHARED block (mirrored byte-for-byte in the bridge's copy, and in
`javascript/minipaint_wangp.js`), on top of protocol 3's queue vocabulary:

```python
PROTOCOL = 4
QUEUE_STARTED = "started"; QUEUE_STATUSES = (pending, queued, started, refused, expired); QUEUE_POSITIVE = (queued, started)
START_AUTO = "auto"; START_NEVER = "never"; START_MODES; START_UNKNOWN = "unknown"; START_ANSWERS
ROUTE_GENERATE = "generate"; ROUTE_QUEUE = "queue"; ROUTES
normalize_queue_request: start defaults to auto, anything else REQUEST_INVALID; queue_payload_hash includes it
normalize_queue_result:  + route, start, generation_running (True | False | None)
normalize_queue_status:  + queue_depth (int >= 0 | None), route
```

Wire messages are unchanged in shape: `WANGP_QUEUE_REQUEST` carries `start`
in its `queue` payload; `WANGP_QUEUE_RESULT` answers with `route`, `start`
and `generation_running`; `WANGP_QUEUE_STATUS` with `status` (now possibly
`started`), `queue_depth` and `route`. A hello / receivers answer carries
`capabilities.start`, `start_missing` and a live `generation_running`.

## `javascript/minipaint_wangp.js` — the parent's queue calls

```
minipaintWanGP.queue(request)                       one WANGP_QUEUE_REQUEST (start passes through), answered or timed out
minipaintWanGP.confirmQueue(requestId)              one WANGP_QUEUE_CONFIRM
minipaintWanGP.queueAndConfirm(request, {onAdmitted})  -> {ok, status: started|queued|refused|unconfirmed, request_id, tasks_added, queue_depth, route, start,
                                                          generation_running, applied, inherited, ignored, model, code, message, detail}
minipaintWanGP.capabilities()                       -> {ok, ready, queue, start, generation_running, model, inputs}
minipaintWanGP.state().queue / .start / .generation_running
```

`onAdmitted` is called once the bridge has admitted the request - the moment
the overlay is on the form - which is when the outbox is told `sent`.

## `wan2gp_bridge/wan2gp-minipaint-bridge/` — bridge 1.3.0

`OPERATIONS = ("hello", "receivers", "receive", "queue", "confirm")`. The one
event's outputs are the acknowledgement, the receivers, the switch components,
then the queue-only components in `compatibility.queue_components()` order:
`prompt`, `wizard_prompt`, `client_id`, `add_to_queue_trigger`,
`generate_trigger`. `QUEUE_CRITICAL = (client_id, add_to_queue_trigger, prompt,
state)`; `START_CRITICAL = (generate_trigger,)`. Globals asked for:
`get_unique_id`, `get_gen_info`, `is_generation_in_progress` (plus the model
globals). `Compatibility.generation_running()` calls the injected function
and answers None for anything that is not one; the handshake carries
`capabilities.queue`, `capabilities.start`, `queue_missing`, `start_missing`
and `generation_running`.

```python
MiniPaintBridge.queue(raw, bridge_session, live) -> (ack, writes)    # section 14, then the start decision:
    normalize -> session/components -> duplicate / REQUEST_ID_CONFLICT -> owner busy (QUEUE_BUSY) or expired (restored first)
    -> every handoff loaded -> per image field: not offered -> ignored; else a replacement list + the switch a send makes
    -> prompt into wizard_prompt when the wizard is on, else prompt -> client_id = request_id
    -> _start_route(start): never -> queue; start_missing or flag None -> queue, "unknown"; flag True -> queue; flag False -> generate
    -> writes generate_trigger or add_to_queue_trigger = get_unique_id(); the record keeps route, start_answer, generation_running
MiniPaintBridge.confirm(raw, bridge_session, live) -> (answer, writes)
    a task carrying the request: started when route == generate and admission.task_position(gen, id) == 0 and admission.generating(gen); else queued
    queue_depth = task_position; the positive status is sticky (seen_status)
    refused on admission.refusal_evidence; expired to ADMISSION_UNCONFIRMED; a terminal state runs compare-before-restore
```

`admission.py` adds `route`, `start_mode`, `start_answer`, `generation_running`,
`queue_depth`, `seen_status` to `PendingAdmission`, `task_position(gen, id)`
and `generating(gen)` (`gen["in_progress"]`), beside the loose gallery
signatures of 1.2.0. `bridge_js.py`'s `queueProblem` refuses a `start` that
is not a mode; `plugin_info.json` says 1.3.0, protocol 4, capability `start`.

## `minipaint_neo/clipboard/` — the tab

```python
# __init__.py / config.py / store.py / history.py / routes.py: as in 1.2.0 (the folder, the intercept, the library, the draft and history, the image and import routes)

# ui.py
PREFIX = "minipaint_clipboard"; SLOTS = (("first", "First Frame", "start"), ("last", "Last Frame", "end"), ("ref", "Reference", "references"))
QUEUE_BUTTON_LABEL = "Add to Queue"; QUEUE_BUTTON_BLOCKED = "WanGP is not running"; OUTBOX_LABELS; NO_PAGE = "00000000"; OUTBOX_SHOWN = 40
def grid_html(assets, selected, configured); def card_html(slot, label, field, assets, missing=False); def history_html(records, asset_of)
def outbox_html(jobs, page) -> str; def job_sentence(job) -> str
class ClipboardTab:
    refresh; sort_changed; sort_request; thumbnail_changed; toggle_intercept          # every refresh output ends with the button's state
    assign(slot, selected); slot_action("clear:<slot>" | "assign:<slot>:<id>"); slot_upload(slot, file, selected); upload(files, selected); pasted(path, selected)
    prompt_changed(prompt)
    prepare_queue(prompt, page) -> (instruction json {nonce, job_id} | "", status, outbox html, button)   # outbox.submit(..., "clipboard"); WANGP_NOT_RUNNING refuses
    refresh_outbox(page) -> (outbox html, status, history html, button)                # records history for unrecorded positive Clipboard jobs, once
    outbox_action("cancel|retry|adopt:<job>:<page>:<nonce>", page) -> (outbox html, status)
    show_history(); history_action("load:<id>" | "delete:<id>", prompt)
    send("<target>:<asset id>:<nonce>", selected)          # minipaint | img2img | inpaint | extras | stitch_*
    choose_folder(text, create); open_folder; open_rename; rename; open_delete; delete
    _queue_button(running=None) -> gr.update(interactive, value); _running() -> outbox.wangp_running()
def create_ui() -> ClipboardTab; def on_ui_tabs() -> [(blocks, "Clipboard", "minipaint_clipboard")]   # a failure gives a note under the same id
```

Components added for the queue: the hidden `page_id` (the page writes its
identity into it on attach; the Add to Queue click's JS returns it as the
second input), the hidden `outbox_action` textbox, the hidden
`outbox_refresh` button, and the `outbox_list` HTML under the button. The
events: `queue.click(prepare_queue, [prompt, page_id] -> [queue_instruction,
queue_status, outbox_list, queue])`, `queue_instruction.change(js: pump)`,
`outbox_refresh.click(refresh_outbox, [page_id] -> [outbox_list,
queue_status, history_list, queue])`, `outbox_action.input(outbox_action,
[outbox_action, page_id] -> [outbox_list, queue_status])`.

The tab's browser side, `javascript/minipaint_clipboard.js`
(`window.minipaintClipboard`): `attach` (writes the page id, listens for
`minipaint:outbox`, pumps once so a reloaded page resumes its jobs),
`afterRender`, `toggleMenu`, `select`, `setThumbnailSize`,
`pasteFromClipboard`, drop-on-card import, the menu, `sendTo`, `armQueue` (a
bounded watcher on the instruction box), `queue(instruction)` (→
`minipaintInterop.wangp.pump()`), `pump`, `pageId`, `refreshCapabilities`
(throttled, never on a timer; the line says generating / idle),
`pressHidden`. Job buttons write `outbox_action`; retry and adopt kick the
pump. It sets no colour of its own and journals no prompt and no filename.

## The Canvas and the host

`canvas/ui.py`: `CLIPBOARD_TARGET = "clipboard"` in `DESTINATION_LABELS`;
`current()` (the TouchCanvas of the UI being built); `receive()` intercepts to
Clipboard when `clipboard.intercept_enabled()` and passes through on failure;
`after_receive(state)` → which tab the follow-up step switches to;
`receive_picture(image, state, mode, origin, label)`;
`receive_from(event, provider, inputs)` wires an outside trigger into the
Canvas's own structural receive chain; `send()` routes `clipboard` to
`_send_to_clipboard()`. `canvas/host.py`: `gallery_file(payload)` — the file a
gallery item stands for, only through the host's own `check_tmp_file`.
`javascript/minipaint_canvas.js`: `switchTo("clipboard")`.

## Tests

`tests/test_wangp_queue.py` (the queue inside the plugin: overlay, admit,
confirm, restore — on a stub host with the real component names),
`tests/test_wangp_start.py` (protocol 4's start decision, started versus
queued, the depth, the fail-safe direction), `tests/test_interop.py`
(staging, normalising, preparing, the routes, the start mode),
`tests/test_clipboard_store.py` (the library, containment, the documents),
`tests/test_clipboard_outbox.py` (the outbox's rules and its routes),
`tests/test_clipboard_ui.py` (the tab on a Forge-shaped page, every event,
the outbox flow, the blocked button, the intercept, Send to Clipboard, the
fallback, the theming rules), `tests/test_queue_e2e.py` (a WanGP-shaped
Gradio app with Wan2GP's variable names and both triggers, the bridge placed
by `insert_after`, driven through Gradio's predict endpoint with the test
playing the browser, the outbox in the middle). The lock, the GPU report and
the emergency restart are in `tests/test_wangp_runtime.py`. All in
`tests/run.py`.
