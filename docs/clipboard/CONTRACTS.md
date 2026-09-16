# Clipboard and the WanGP queue: module contracts

The one place the modules agree on each other's names, in the same spirit as
`docs/wangp/CONTRACTS.md`, which this extends. Every signature here is
load-bearing: another module, the tab, the browser, a third-party extension
or a test calls it. House style as there: `from __future__ import
annotations`, docstrings that say *why*, `typing.Optional[...]`, nothing the
WebUI does not already ship, nothing that polls forever, nothing that can
raise into WanGP.

---

## Sending a picture out, when the queue is not delivering

`POST /minipaint-clipboard/send` with `{"target": ..., "asset": ...}` answers
with everything the browser needs to finish a send, and nothing about Gradio.

It is how every send is placed. It exists because sending used to be a hidden
textbox written by script plus a Gradio event carrying it over the queue. When that queue stops delivering - a
connection that dropped and did not come back, a session the server has
forgotten - the box is written and nothing else happens, for as long as the
page stays open. Everything else the tab does for a running job rides plain
HTTP and keeps working through exactly that failure: the event stream, the
imports, the thumbnails. Only the actions were tied to the queue.

The answer:

    {"ok": true, "target": "img2img", "label": "img2img", "filename": "x.png",
     "backend": false, "instruction": "img2img", "box": "uuid_...",
     "payload": "data:image/png;base64,..."}

`payload` is present for every destination but `minipaint`, with one of two
ways to place it:

* `box` - `img2img` and `inpaint`. The browser writes the host canvas's hidden
  textbox, which is browser work however the send was carried. `box` is named
  by the server because only it knows which canvas the host registered for
  that tab, and because ForgeCanvas gives its background and scribble boxes
  the same id and tells them apart by class.

* `elem` - the Extras image and the two ImageStitch galleries, which hold
  their value in the component rather than in a box on the page. The server
  fills those by returning a new value, which is a Gradio event and so the
  queue; the browser fills the same component by handing its file input the
  picture, which is the ordinary upload route and is not. `adds: true` says
  the component keeps what is already in it (the galleries) where the
  server's write replaces the lot, so the page can say which happened.

        {"ok": true, "target": "stitch_txt2img", "elem": "script_txt2img_…_ref_latent",
         "adds": true, "backend": false, "payload": "data:image/png;base64,…"}

`backend: true` is left only when no component can be named - the destination
is not on this page at all. There is no payload for that, and the browser is
told so plainly rather than being handed something it cannot deliver and
reporting a send that did not happen.

Whichever way the plan arrives, the browser places the picture with the
legacy editor's own transfer library (`miniPaint/src/js/libs/webui-host.js`,
served as the `host` bundle): it classifies the destination, clears a Gradio
image before uploading into it, writes a ForgeCanvas through the native value
setter, settles the img2img sub-tab, and then checks what the WebUI will
submit against what was sent. A page that could not import it still writes
the canvas textbox directly, and says the send was not verified.

The queued event still runs, with `:done` appended to its request
(`<target>:<asset>:<stamp>:done`), which tells `ClipboardTab.send` to record
the send and acknowledge it without writing the destination again - a second
write is harmless for a canvas and one picture too many for a gallery that
appends. An unmarked request still performs the send, which is what happens
when the page could not place it.

**The send names no component from another tab.** `send` writes only this
tab's own boxes (`switch`, `payload`, `to_canvas`, `status`, `send_ack`), so
the event can always run. Extras and the stitch galleries are held in
components belonging to other tabs, and an event naming one that is not on
the page cannot run at all - silently, for ever - so they are wired to
`send_backend.click` on their own, pressed by the browser only when
`send_plan` came back `backend: true`. `host.destinations` also drops any
component whose `is_rendered` is false, so one built but never placed is not
offered in the first place.

**Two events carry that request, not one.** The browser writes
`send_request` and then presses the hidden `send_press` button;
`send_press.click` and `send_request.input` are bound to the same callback,
the same inputs and outputs, and the same follow-up steps. Neither way in is
reliable on every install - on one user's Forge no written box ever produced
an event, across four builds, while presses on the same page worked
throughout - so the tab offers both and the server sorts it out.
`ClipboardTab.send` keeps the last `_ANSWERED_KEPT` requests it has answered
and returns the stored receipt for any of them without delivering again. The
depth matters: a press carries whatever value the framework holds for the
box, so on a page where writes are not heard it can arrive carrying a request
from several sends ago, and delivering that would put the wrong picture in
the destination.

Both this route and the tab's own Gradio path decide what a send means in one
place, `ui.send_plan`, so they cannot drift apart. The route answers from the
built tab's own destinations (`ui.current()`), not from a fresh lookup: asking
the host again rebuilds its answer from whatever was registered last, and a
process that built a page more than once would otherwise name a box nothing on
the page is listening to.


## The public contract — `minipaint.wangp.queue/v1` (protocol 5)

```
window.minipaintInterop = {
    version: 1,
    contract: "minipaint.wangp.queue/v1",
    wangp: {
        capabilities(): Promise<{ ok, ready, queue, start, track, generation_running: bool|null, model: {type, label, family, architecture}, inputs: {start, end, references: {supported}}, api_version } | { ok: false, code, message }>,
        enhance(): Promise<{ ok, enabled, origin, variants, slots: {fl2va: {start, end}, ref2va: {references}}, overrides, capabilities: {found, available, vision, model, reason, ...} }>,
        stageImage(input: Blob | File | ArrayBuffer | TypedArray | HTMLCanvasElement | dataURL, options?): Promise<{ ok, image: {kind: "staged", id}, width, height } | { ok: false, code, message }>,
        enqueue(request, options?: { wait?: bool = true, timeoutMs?: number = 300000, enhance?: bool, model?: {type, label, family, architecture} }): Promise<Result>,
        jobs(): Promise<{ ok, running, counts, jobs: [Job] }>,
        cancel(jobId), retry(jobId), adopt(jobId): Promise<{ ok, job } | { ok: false, code, message }>,
        cancelAll(): Promise<{ ok, cancelled, enhancing, in_flight, jobs: [Job] }>,   // every waiting job, from every page; a job being sent finishes
        pump(): void,                       // run this page's waiting jobs now; idempotent, bounded in time
        track(job): bool,                   // follow a queued job of this page in WanGP's queue (started by the pump itself)
        resumeTracking(): Promise<int>,     // after a reload: this page's queued jobs whose tasks were last seen in WanGP
        refreshWaiters(): Promise<int>,     // answer callers still waiting on jobs the server has since settled
        pageId(): <32 hex>,                 // this page's identity, kept per browser tab
    },
    message(code): string,
}

request = {
    request_id?: <32 lowercase hex>,          // minted when absent; the same id + the same payload is one request
    prompt?: string,                          // omitted / null / "" -> inherit; 12000 chars after cleaning (protocol 5; was 4000)
    images?: {
        start?: Handle, end?: Handle,         // omitted / null -> inherit
        references?: Handle[],                // omitted / null / [] -> inherit; at most 16
    },
    start?: "auto" | "never",                 // omitted -> "auto"
}
Handle = { kind: "staged" | "clipboard_asset", id: <32 lowercase hex> }

Result (ok)  = { ok: true,  status: "started" | "queued", request_id, job_id, tasks_added, queue_depth: int|null, route: "generate"|"queue"|"",
                 model: {type, label, family, architecture}, applied: {prompt, start, end: bool, references: int}, inherited: [field], ignored: [{field, code}],
                 enhanced?: bool, wangp: {state: accepted|waiting|generating|finished|unknown, position, queue_depth} | null }
Result (not) = { ok: false, status: "refused" | "unconfirmed" | "pending", request_id, job_id, code, message, enhanced?: bool }

Job = { job_id: <16 hex>, request, page, origin: "clipboard"|"api", state: enhancing|pending|sending|queued|started|failed|unconfirmed|cancelled,
        created_at, updated_at, created, updated, attempts, sent, leased_until, result, error: {code, message}|null, retry_of, summary, history_recorded,
        model: {type, label, family, architecture}, enhance_requested: bool,
        enhance: { variant: fl2va|ref2va, llm_id, state: queued|running|done|failed|cancelled|lost, stage, position, elapsed, image_used, image_ignored: [slot],
                   dropped: [field], extra_references, system_override, prompt_original, error, submitted_at, finished_at, reused_from } | null,
        wangp: { state, position, queue_depth, seen_at } | null }
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
progress bar, no settings.

Protocol 5 adds: a job may be **enhanced** first (`enhance: true`, or the
tab's switch when the option is absent) - the prompt is rewritten by
ModelSwitchRefiner's MiniMax H3 writer for the H3 variant the page's model
is, the job waits as `enhancing` and holds the line behind it, and the
request is refused rather than queued as typed when that cannot be done;
the whole line can be cancelled at once; a job composed for one model
(`model_type` on the wire) is refused with `MODEL_CHANGED` when the page
has moved; and the page that queued a job reports where its task is in
WanGP (`wangp`) until it has left the queue.

Codes a caller can meet: `REQUEST_INVALID`, `PROMPT_TOO_LONG`,
`IMAGE_STAGE_INVALID`, `IMAGE_STAGE_EXPIRED`, `CLIPBOARD_NOT_CONFIGURED`,
`CLIPBOARD_ASSET_UNKNOWN`, `CLIPBOARD_ASSET_OUTSIDE_ROOT`, `IFRAME_NOT_READY`,
`BRIDGE_COMPONENT_INCOMPATIBLE`, `BRIDGE_SESSION_MISMATCH`, `WANGP_NOT_RUNNING`,
`WANGP_RESTARTED`, `QUEUE_BUSY`, `QUEUE_JOB_PENDING`, `QUEUE_JOB_UNKNOWN`,
`REQUEST_ID_CONFLICT`, `QUEUE_REQUEST_REFUSED`, `WANGP_VALIDATION_REFUSED`,
`ADMISSION_UNCONFIRMED`, `MODEL_CHANGED`, `ENHANCE_UNAVAILABLE`,
`ENHANCE_MODEL_UNSUPPORTED`, `ENHANCE_PROMPT_REQUIRED`, `ENHANCE_NO_VISION`,
`ENHANCE_IMAGE_UNREADABLE`, `ENHANCE_QUEUE_FULL`, `ENHANCE_SYSTEM_PROMPT_EMPTY`,
`ENHANCE_REFUSED`, `ENHANCE_FAILED`, `ENHANCE_CANCELLED`, `ENHANCE_LOST`,
`HANDOFF_*`, `AUTH_BOUNDARY_FAILED`, `INTERNAL_ERROR`. Every one has a
sentence in `minipaint_neo/wangp/errors.py`.

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
`done` with the public result; a `wait` answer (`busy`, `turn`, or protocol
5's `enhancing`) is honoured only while this page has jobs waiting and for
at most `PUMP_MAX_WAIT_MS` (four hours) of one held line, with a `waiting`
event every `WAITING_EVENT_MS`; `empty` or a refusal (`WANGP_NOT_RUNNING`
included) stops it. `enqueue` sends `enhance` when the caller said, and the
page's model (`minipaintWanGP.state().model`, or `options.model`) always;
`execute` puts a job's `model.type` on the wire as `model_type` for an
enhanced job and refuses `MODEL_CHANGED` itself when this side already
knows the page has moved. After a `queued` or `started` report the job is
**tracked**: every `TRACK_MS` this page asks `minipaintWanGP.trackQueue` for
its open jobs' request ids and posts each answer to `OUTBOX_TRACK_ROUTE`,
until the task is `finished` or `unknown` or `TRACK_MAX_MS` has passed;
`resumeTracking()` picks the open ones up after a reload. It dispatches
`minipaint:outbox` CustomEvents on `document` (`submitted`, `sending`,
`done`, `changed`, `stopped`, `waiting`, `tracked`) with the job. `pageId()`
lives in `sessionStorage` under `minipaint.interop.page`. Journal lines go
through `minipaintWanGP.note` under `browser` (`run <id8>: prompt, start;
start auto (N image(s) prepared); for model <type>`, `pump: stopped - CODE`,
`cancel all: N cancelled, M in flight`).

## The event spine — `/minipaint-interop/events` and `/sync`

```
GET /minipaint-interop/events?page=<id>[&cursor=<epoch>:<revision>]    text/event-stream
GET /minipaint-interop/sync?page=<id>                                  the authoritative snapshot
```

Events are **advisory**; the snapshot is authoritative. An event says something about job X
changed and carries enough to update a screen; it is never a second copy of the truth. A
page with any reason to doubt what it holds asks for a snapshot instead, and that is an
ordinary thing to do rather than a failure.

A cursor is `<epoch>:<revision>`. The epoch is minted once per Forge process, so a cursor
from another run is not stale — it is meaningless, and says so. A cursor this process never
issued, or one older than the bounded replay ring, produces one `reset` frame and one sync.
A `hello` and a `heartbeat` carry no id, because resuming from a liveness frame would hand a
page a cursor that names nothing.

The heartbeat is an application-visible frame (≈15 s) because an SSE comment cannot be read
from JavaScript, and a silent connection and a dead one look identical to a remote browser.
A stream is rotated after 30 minutes, because a proxy that quietly drops a long-lived
connection makes a page look connected while it is not.

**Losing the spine makes a screen stale and can never stop a job.** That is what lets the
browser's own timers be removed rather than merely lengthened.

Nothing private crosses: no prompt, no enhanced prompt, no caption, no path — `generated_files`
included, of which only the count goes.

## `minipaint_neo/interop.py` — the API's server half

```python
STAGE_ROUTE   = "/minipaint-interop/stage"      # POST bytes (Content-Type image/png|jpeg|webp|octet-stream) -> {ok, image:{kind:"staged", id}, width, height}
PREPARE_ROUTE = "/minipaint-interop/prepare"    # POST {request} -> {ok, request: wire} | {ok:false, code, message}
RELEASE_ROUTE = "/minipaint-interop/release"    # POST {handoff_ids} -> {ok, released}
CONTRACT_ROUTE = "/minipaint-interop/contract"  # GET -> contract()  (adds start_modes, outbox, enhance, track_states)
ENHANCE_ROUTE = "/minipaint-interop/enhance"    # GET -> {ok, **clipboard.enhance.describe()}
OUTBOX_ROUTE  = "/minipaint-interop/outbox"     # GET -> {ok, jobs, running, counts}
OUTBOX_SUBMIT_ROUTE = OUTBOX_ROUTE + "/submit"  # POST {request, page, origin, enhance?: bool, model?} -> {ok, job}   409 WANGP_NOT_RUNNING / QUEUE_BUSY / ENHANCE_UNAVAILABLE / ENHANCE_QUEUE_FULL / ENHANCE_MODEL_UNSUPPORTED, 400 the rest
OUTBOX_CLAIM_ROUTE  = OUTBOX_ROUTE + "/claim"   # POST {page} -> {ok, job, lease, pending} | {ok, wait, reason: busy|turn|enhancing, pending[, job_id]} | {ok, empty}   409 WANGP_NOT_RUNNING
OUTBOX_REPORT_ROUTE = OUTBOX_ROUTE + "/report"  # POST {job_id, lease, phase: sent|done, result?} -> {ok, job}
OUTBOX_CANCEL_ROUTE / OUTBOX_RETRY_ROUTE / OUTBOX_ADOPT_ROUTE   # POST {job_id[, page]} -> {ok, job}   404 QUEUE_JOB_UNKNOWN
OUTBOX_CANCEL_ALL_ROUTE = OUTBOX_ROUTE + "/cancel_all"  # POST {} -> {ok, cancelled, enhancing, in_flight, jobs}
OUTBOX_TRACK_ROUTE = OUTBOX_ROUTE + "/track"    # POST {job_id, page, state: waiting|generating|finished|unknown, position?, queue_depth?} -> {ok, job}   400 when not the owning page
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
def open_handle(handle) -> PIL.Image                         # a staged or clipboard_asset handle as a picture; the one resolver (prepare and the enhancer both use it)
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
OUTBOX_NAME = "clipboard-outbox.json"           # {"schema": 3, "jobs": [...]}, atomic, quarantined when broken
# The browser-executed vocabulary, kept so an already-loaded page keeps working and an old
# document is read as what it was. A schema-2 document needs no migration: every state it
# can carry is still a state.
ENHANCING, PENDING, SENDING, QUEUED, STARTED, FAILED, UNCONFIRMED, CANCELLED; LEGACY_TERMINAL; WAITING = (ENHANCING, PENDING)
# The server-executed vocabulary: what a job is waiting for, rather than where it is in a
# handshake. A job carries `executor` = "browser" | "server"; a document with neither is a
# legacy one, because that is all there used to be.
ADMITTED, WAITING_TURN, ENHANCED, ENSURING_WANGP, COMPOSING, WAITING_FOR_CARD,
SUBMITTING_WANGP, GENERATION_WAITING, GENERATION_RUNNING, COMPLETED, EXECUTION_UNKNOWN
SERVER_ACTIVE; SERVER_TERMINAL; SERVER_SUBMITTED   # SERVER_SUBMITTED is what a restart may never simply resume
TERMINAL = LEGACY_TERMINAL + (COMPLETED, EXECUTION_UNKNOWN); POSITIVE = (QUEUED, STARTED, COMPLETED)
STAGE_TEXT                                          # what each stage says to a screen when the job has nothing more specific
WANGP_ACCEPTED, WANGP_WAITING, WANGP_GENERATING, WANGP_FINISHED, WANGP_UNKNOWN; WANGP_STATES      # a queued job's place inside WanGP
ORIGIN_CLIPBOARD = "clipboard"; ORIGIN_API = "api"
LEASE_SECONDS = 90.0; PAGE_ACTIVE_SECONDS = 15.0; WAIT_BUSY_MS = 400; WAIT_TURN_MS = 250; WAIT_ENHANCE_MS = 1000; WATCH_SECONDS = 2.0
MAX_JOBS = 500; MAX_PENDING = 200; KEEP_TERMINAL_SECONDS = 7 days; PAGE_RE = 8..32 lowercase hex
def use_clock(fn); def use_running(fn); def use_watcher(bool); def reset_for_tests()      # seams (tests run without the watcher thread)
def wangp_running() -> bool                                        # runtime.current().snapshot()["running"], contained
def submit(request, page, origin="clipboard", require_running=True, enhance=None, model=None) -> Job
    # normalises through interop; WANGP_NOT_RUNNING; QUEUE_BUSY past MAX_PENDING; enhance None -> enhance.enabled(); True -> enhance.plan + enhance.submit
    # BEFORE the job is stored (a refusal stores nothing); the job is then ENHANCING with an enhance record, else PENDING; the model block is kept
def refresh() -> counts                                            # _sweep: expired leases, then _advance (every ENHANCING job against enhance.status), then pruning
def claim(page) -> {"job", "lease", "pending"} | {"wait", "reason": "busy"|"turn"|"enhancing", "pending"[, "job_id"]} | {"empty": True, ...}
def report(job_id, lease, phase, payload=None) -> Job              # "sent": the overlay was written; "done": sanitize_result(payload) -> queued|started|failed|unconfirmed; sets wangp
def track(job_id, page, payload) -> Job                            # the owning page's report of the task's place: sanitize_track(payload); finished and unknown stick
def cancel(job_id) -> Job                                          # enhancing (enhance.cancel too) or pending; QUEUE_BUSY while sending
def cancel_all() -> {"cancelled", "enhancing", "in_flight", "jobs"}   # enhance.cancel_all() once by origin, every ENHANCING and PENDING job cancelled; SENDING left to finish
def retry(job_id, page) -> Job                                     # failed | unconfirmed | cancelled -> a NEW job for this page: from prompt_original, enhanced again,
                                                                   # unless a written prompt exists and the failure was not MODEL_CHANGED (then carried over, reused_from)
def adopt(job_id, page) -> Job                                     # a pending job re-owned by this page
def on_wangp_restart() -> int                                      # sending -> unconfirmed (WANGP_RESTARTED)
def jobs() -> [Job]; def get(job_id); def counts(); def pending_count(page=None)   # pending_count counts WAITING
def unrecorded(origin="clipboard") -> [Job]; def mark_recorded(job_ids)   # the tab's history bookkeeping, once
def sanitize_result(raw) -> dict; def sanitize_track(raw) -> dict; def summary_of(request) -> dict; def public(job) -> Job

# -- the server executor's half -------------------------------------------------------
def use_executor(name | None); def chosen_executor() -> str; def unattended_enabled() -> bool
def transition(job_id, state, expect_revision=None, stage=None, **fields) -> Job | None
    # re-reads under the lock and writes only this job's fields; a write prepared against a
    # revision somebody has moved past does nothing and answers None. The only persistence
    # primitive is a whole-document overwrite, so a stage that held a job across four
    # minutes of model loading would otherwise undo a cancel that landed during it.
def next_executable(now=None) -> Job | None        # one at a time; anything in flight before anything new
def server_jobs(states=None) -> [Job]; def submitted_jobs() -> [Job]
def record_snapshot(job_id, snapshot, expect_revision=None) -> Job | None   # frozen once; never re-composed
def record_execution(job_id, record, state=None, expect_revision=None, child_instance="") -> Job | None
def fail(job_id, code, message="", expect_revision=None, state=FAILED) -> Job | None
def attempt(job_id) -> Job | None
def recover(child_instance="") -> counts           # before any sweeper; pre-submission resumes, post-submission reconciles
def input_ids(job) -> [str]                        # every durable input a job holds
```

A server-executed job's document also carries: `executor`, `revision` (bumped by every
durable transition), `stage`, `execution_id`, `execution` (the child's last word),
`snapshot` (the frozen settings — `public()` sends the source and the count, never the
settings themselves), `inputs` (slot → pinned id), `generated_files` (kept for a later
viewer; `public()` sends only `generated_count`), `child_instance`, `inputs_released` and
`settings_flush`.

`settings_flush` is what the pressing page managed to do about WanGP's live form before
submitting — one of `committed`, `unchanged`, `suppressed`, `unavailable`, or empty for a
press that never tried. It is provenance, not permission: it changes nothing about how the
job runs, and it crosses to a screen because it is one of five fixed words. Together with
the snapshot's `source` it answers the only question a reader of the queue can otherwise
not ask — whether the base this job ran at is the one its owner was looking at. A base
composed from the record after a successful flush is recorded as `flushed_form` rather than
`recorded_form`; both came out of `load_model_form`, and nothing downstream could tell them
apart otherwise.

**The queue is one Forge session's.** `start_session()` runs before anything sweeps and
empties it: every job, in every state, and every pinned input released. Nothing carries into
the next run, and a finished job leaves the list after a short grace
(`KEEP_TERMINAL_SECONDS`, long enough for a waiting page to be told what happened and no
longer). The enhancement switch is session-scoped the same way — `enhance.start_session()`
turns it off, which is what "off by default" has always claimed. What this gives up is
written down in `docs/wangp/SERVER_EXECUTION.md` §9.

The rules: one lease at a time across every page; the line keeps press
order - its head is the oldest job not yet handed out, and while that head
is `enhancing` nobody is served, whichever page asks (`wait: enhancing`); a
pending head goes to its own page, or, when that page has not claimed within
`PAGE_ACTIVE_SECONDS`, another page may run its own first job - unless that
job is itself enhancing; a lease that expires returns an unsent job to
pending and marks a sent one unconfirmed; an unconfirmed job is never
claimed again by itself. `_advance` moves an enhancing job on every read: a
written prompt (cleaned; `PROMPT_TOO_LONG` over the ceiling) replaces
`request.prompt` and the job is pending; `failed` → `ENHANCE_FAILED`;
`cancelled` (in LLM Studio's own panel) → cancelled with `ENHANCE_CANCELLED`;
a record the API no longer has → `ENHANCE_LOST`. A daemon watcher
(`WATCH_SECONDS`) does the same while any job is enhancing, so a prompt
finished while no page is asking is still collected before the API forgets
it. The document holds the request (prompt included - a snapshot of the
composer at press time; the written prompt once there is one, with the typed
one in `enhance.prompt_original`), never a lease token in what a page is
shown, never a path. `runtime.emergency_restart` calls `on_wangp_restart`
through a contained import.

## `minipaint_neo/clipboard/executor.py` — the coordinator

```python
IDLE_SECONDS = 30.0; POLL_SECONDS = 2.0; CARD_POLL_SECONDS = 3.0; WANGP_READY_TIMEOUT = 20 min
RETRYABLE = {ENHANCE_QUEUE_FULL, QUEUE_BUSY}; BACKOFF_START = 5.0; BACKOFF_MAX = 120.0; MAX_RETRYABLE_ATTEMPTS = 40
def use_clock(fn); def use_sleep(fn); def use_thread(bool); def reset_for_tests()     # seams
def wake(); def ensure_running(); def stop(); def running() -> bool
def step() -> bool                     # one stage. Everything the coordinator does; the thread only decides when
def recover() -> counts; def reconcile() -> {"adopted", "reattached", "unknown"}
def snapshot() -> dict                 # for a status line
```

One coordinator per Forge process, woken by a condition rather than a timer, sleeping for
nothing when the queue is empty. Four rules it does not bend: the outbox lock is never held
for anything slow; no route ever runs a stage; one job advances at a time (two would be two
things competing for one card); and nothing is ever resubmitted across an ambiguity.

## `minipaint_neo/clipboard/job_inputs.py` — the pictures a job owns

```python
PINS_NAME = "clipboard-job-inputs.json"; RETENTION_SECONDS = 24h; ORPHAN_SECONDS = 1h
def adopt(handle, job_id="", slot="") -> record     # resolved once, copied under the handoff root, pinned
def assign(input_ids, job_id) -> int                # the pins exist before the job document does
def release(input_ids, now=None) -> int             # starts the retention clock; does not delete
def resolve(input_id) -> Path                       # JOB_INPUT_MISSING rather than a generic refusal
def pinned_ids(include_released=False) -> {str}     # what handoff.sweep will not touch
def sweep(now=None) -> int; def counts() -> dict; def for_job(job_id) -> [record]; def describe(id)
```

A pin is written **before** the job document and released **after** the job is terminal. An
input pinned for a job that was never stored is an orphan the sweeper takes in an hour; an
input released for a job still waiting is the failure this file exists to prevent, and the
asymmetry is deliberate. Recovery registers pins before any sweeper runs.

## `minipaint_neo/clipboard/enhance.py` — the prompt enhancer

```python
ENHANCE_NAME = "clipboard-enhance.json"        # {"schema": 1, "enabled": bool, "overrides": {variant: {text|image: str}}}
API_MODULE = "mc_llm_api"; API_VERSION_SUPPORTED = 1; ORIGIN = "minipaint-clipboard"; CANCEL_REASON
FL2VA = "fl2va"; REF2VA = "ref2va"; VARIANTS; VARIANT_LABELS; MODEL_KEY = "minimax"
MODE_TEXT = "text"; MODE_IMAGE = "image"; MODES; MODE_LABELS
SLOT_FIRST = "first_frame"; SLOT_LAST = "last_frame"; SLOT_REFERENCE = "reference"; SLOTS
SLOTS_FOR = {fl2va: {start: first_frame, end: last_frame}, ref2va: {references: reference}}   # anything not listed is dropped from the enhancement
LLM_QUEUED, LLM_RUNNING, LLM_DONE, LLM_FAILED, LLM_CANCELLED; LLM_STATES; LLM_TERMINAL
REJECTIONS = {disabled: ENHANCE_UNAVAILABLE, empty_prompt: ENHANCE_PROMPT_REQUIRED, empty_system_prompt: ENHANCE_SYSTEM_PROMPT_EMPTY,
              bad_image: ENHANCE_IMAGE_UNREADABLE, no_vision: ENHANCE_NO_VISION, queue_full: ENHANCE_QUEUE_FULL}   # anything else: ENHANCE_REFUSED
def use_api(module); def reset_for_tests()                      # seams
def api() -> module | None            # sys.modules, then the other extension's folder (its imported modules' folders, the host's extension list,
                                      # the host's extension dirs, this extension's parent), imported with the folder APPENDED to sys.path; cached; never raises
def capabilities() -> {found, available, api_version, enabled, configured, vision, model, variants, max_queued, reason}   # available = enabled and configured
def variant_for_model(model) -> "fl2va" | "ref2va" | ""          # from type, architecture, family, label; "minimax" required; neither variant -> ""
def model_block(raw) -> {type, label, family, architecture}
def plan(request, model) -> {variant, slots: {slot: handle}, dropped: [field], extra_references, has_image, model}   # ENHANCE_MODEL_UNSUPPORTED, ENHANCE_PROMPT_REQUIRED
def preflight(planned) -> (code, sentence)       # "" is yes. A READ, not a start: the shipped API brings a cold runtime
                                                # to readiness as part of running the job, so there is nothing to pre-warm.
                                                # Four things one "unavailable" used to flatten: ENHANCE_EXTENSION_MISSING,
                                                # ENHANCE_SWITCHED_OFF (a user's own choice; nothing here turns it back on),
                                                # ENHANCE_NOT_CONFIGURED, ENHANCE_NO_VISION. A cold runtime is none of them
                                                # and is not a state at all - it is stage text on a job already running.
def follow(llm_id, timeout=FEED_WAIT_SECONDS) -> {state, stage, position, terminal} | {}
                                                # the API's own feed. Content-bearing events (FEED_CONTENT_EVENTS) are
                                                # discarded unread: a re-subscribe replays the whole written prompt. A feed
                                                # that expires or raises is a subscription that ended, not a job that failed;
                                                # the caller reconciles against status(). {} means this build has no feed.
def release_runtime() -> bool                   # the public VRAM seam, if the owning extension exports one. Called at the
                                                # ENHANCED -> ENSURING_WANGP boundary: enhancer-then-WanGP is the unprotected
                                                # direction, because WanGP sizes itself against the card with no ladder.
                                                # Never mc_llm_runtime internals; a build without a seam answers False.
def enabled() -> bool; def set_enabled(flag) -> bool               # read from disk on every call; off by default
def override(variant, mode) -> str; def set_override(variant, mode, text) -> str; def clear_override(variant, mode) -> bool; def overrides() -> {variant: {mode: bool}}
def default_prompt(variant, mode) -> str; def effective_prompt(variant, mode) -> (text, "override"|"default"|"unavailable"); def system_prompts() -> dict
def submit(prompt, planned) -> {llm_id, system_override, variant}   # interop.open_handle for each slot (pictures, never paths); the override for (variant, image|text) when saved;
                                                                      # mc_llm_api.submit_minimax(prompt, variant=, first_frame=, last_frame=, reference=, system_prompt=, origin=ORIGIN, remember=True)
def status(llm_id) -> {state, stage, position, elapsed, queued_for, image_used, image_ignored, system_override, cancelling, error, reason, prompt} | None
def cancel(llm_id, reason=CANCEL_REASON) -> dict; def cancel_all(reason=CANCEL_REASON) -> int   # ours only, by origin
def availability(model=None) -> {state: ready|blocked|model|unknown, text, variant, capabilities}   # the tab's line
def describe() -> {enabled, origin, variants, slots, overrides, capabilities}                         # GET /minipaint-interop/enhance
```

Nothing here logs a prompt, an override, a caption or a picture; the journal
(`enhance` column) sees ids, variants, states and seconds.

## `minipaint_neo/wangp/protocol.py` — protocol 6, the control plane

Added to the same SHARED block, and **versioned separately from `PROTOCOL`**: that number is
the postMessage envelope a browser and an iframe filter on, bumping it makes every
already-loaded page stop answering, and nothing about what those two say to each other
changed here.

```python
CONTROL_PREFIX = "/minipaint-bridge"; CONTROL_VERSION = 1; CONTROL_SECRET_HEADER = "x-minipaint-bridge-secret"
CONTROL_HELLO, CONTROL_COMPOSE, CONTROL_SUBMIT, CONTROL_STATUS, CONTROL_CANCEL, CONTROL_FORGET; CONTROL_OPERATIONS
EXECUTION_ID_RE = HANDOFF_ID_RE                 # the ledger key, and what the bridge writes into WanGP's own client_id
EXEC_ACCEPTED, EXEC_QUEUED, EXEC_RUNNING, EXEC_DONE, EXEC_FAILED, EXEC_CANCELLED, EXEC_UNKNOWN
EXEC_STATES; EXEC_TERMINAL; EXEC_OPEN
BASE_RECORDED, BASE_SESSION, BASE_FACTORY; BASE_SOURCES      # where a composed settings base came from
EXEC_SLOT_START, EXEC_SLOT_END, EXEC_SLOT_REFERENCES; EXEC_SLOTS
CONTROL_UNAUTHORISED, CONTROL_UNAVAILABLE, CONTROL_VERSION_MISMATCH, EXECUTION_ID_CONFLICT,
EXECUTION_REFUSED, EXECUTION_UNKNOWN, SERVICE_UNAVAILABLE, COMPOSE_UNAVAILABLE, MODEL_UNAVAILABLE
def valid_execution_id(value) -> bool
def normalize_compose_request(raw) -> (request, code); def normalize_compose_answer(raw) -> dict
def normalize_media_map(raw) -> (map, code)      # handoff ids only; a path never crosses in either direction
def normalize_execution_request(raw) -> (request, code)   # the settings dict passes through: it is WanGP's shape
def normalize_execution_record(raw) -> dict      # an unreadable record is `unknown`, never `done`
def normalize_control_hello(raw) -> dict
```

## `minipaint_neo/wangp/protocol.py` — protocol 5

Added to the SHARED block (mirrored byte-for-byte in the bridge's copy, and in
`javascript/minipaint_wangp.js`), on top of protocol 4:

```python
PROTOCOL = 5
QUEUE_TRACK = "WANGP_QUEUE_TRACK"; QUEUE_TRACKED = "WANGP_QUEUE_TRACKED"      # in TO_BRIDGE / TO_PARENT
PROMPT_MAX_CHARS = 12000                                                      # was 4000; the envelope ceiling holds it three times over
TRACK_WAITING, TRACK_GENERATING, TRACK_FINISHED, TRACK_UNKNOWN; TRACK_STATES; MAX_TRACKED_REQUESTS = 32
MODEL_TYPE_RE = [A-Za-z0-9][A-Za-z0-9_.:+-]{0,119}; QUEUE_TRACK_TIMEOUT_MS = 10000; QUEUE_CODE_MODEL_CHANGED = "MODEL_CHANGED"
normalize_queue_request: + model_type (optional; REQUEST_INVALID when not one); queue_payload_hash includes it
model_block(raw) -> {type, label, family, architecture}; normalize_queue_result's model uses it
normalize_queue_track(raw) -> {ok, tracked: {request_id: {state, position, queue_depth}}, code, generation_running}   # an unreadable entry is unknown, never finished
```

Protocol 4 had added:

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
minipaintWanGP.queue(request)                       one WANGP_QUEUE_REQUEST (start and model_type pass through), answered or timed out
minipaintWanGP.confirmQueue(requestId)              one WANGP_QUEUE_CONFIRM
minipaintWanGP.queueAndConfirm(request, {onAdmitted})  -> {ok, status: started|queued|refused|unconfirmed, request_id, tasks_added, queue_depth, route, start,
                                                          generation_running, applied, inherited, ignored, model, code, message, detail}
minipaintWanGP.trackQueue(requestIds)               one WANGP_QUEUE_TRACK (1..32 ids) -> {ok, tracked: {id: {state, position, queue_depth}}, generation_running} | failure with tracked: {}
minipaintWanGP.capabilities()                       -> {ok, ready, queue, start, track, generation_running, model: {type, label, family, architecture}, inputs}
minipaintWanGP.state().queue / .start / .track / .generation_running
```

`onAdmitted` is called once the bridge has admitted the request - the moment
the overlay is on the form - which is when the outbox is told `sent`.

## `wan2gp_bridge/wan2gp-minipaint-bridge/` — bridge 1.5.0

**The control surface (protocol 6).** `control.py` binds a loopback port Forge kept before
it launched the child and exported, compares the per-launch credential on every request
*before reading the body*, and answers the six operations above. No credential, no port, no
ledger root: no listener. `ledger.py` keeps the durable execution record — written and
flushed **before** the submission, never after, because that order is the only thing that
makes a repeated submission safe against an API with no idempotency key.
`compose.py` reads the settings a job runs at from Wan2GP's process-wide record of the last
committed form per model (no session, no mutation, works when no page has ever been open).
`execution.py` submits into WanGP's **own** queue and asks the one process-wide service to
look, which is what makes "at most one generation, whoever started it" true in both arrival
orders without a new lock or a new flag — see `docs/wangp/SERVER_EXECUTION.md` section 3 for
why the obvious adapter is the thing that causes the hazard.
`compatibility.WAN2GP_EXECUTION_REVISION` pins the Wan2GP revision this was proven against,
beside the element ids, under the same rule.

`OPERATIONS = ("hello", "receivers", "receive", "queue", "confirm", "track")`.
The one event's outputs are the acknowledgement, the receivers, the switch
components, then the queue-only components in
`compatibility.queue_components()` order: `prompt`, `wizard_prompt`,
`client_id`, `add_to_queue_trigger`, `generate_trigger`. `QUEUE_CRITICAL =
(client_id, add_to_queue_trigger, prompt, state)`; `START_CRITICAL =
(generate_trigger,)`. Globals asked for: `get_unique_id`, `get_gen_info`,
`is_generation_in_progress`, `get_base_model_type` (plus the model globals).
`Compatibility.generation_running()` calls the injected function and answers
None for anything that is not one; `model_descriptor` adds `architecture`
(the definition's, or `get_base_model_type(model_type)`); the handshake
carries `capabilities.queue`, `capabilities.start`, `capabilities.track`,
`queue_missing`, `start_missing` and `generation_running`.

Bridge 1.4.0 (protocol 5) adds, on top of the 1.3.0 flow below:

```python
MiniPaintBridge.queue(...):   after the duplicate check, a request carrying model_type is refused with MODEL_CHANGED - nothing written -
                              when receiver_state.read(...).model["type"] is known and differs
MiniPaintBridge.confirm(...): a matching task also calls ledger.remember_admitted(session, request_id)
MiniPaintBridge.track(raw, bridge_session, live) -> ({tracked: {id: {state, position, queue_depth}}, queue_length, generation_running}, {})
    1..MAX_TRACKED_REQUESTS valid ids, else REQUEST_INVALID; _require_queue (BRIDGE_SESSION_MISMATCH for another page's session)
    per id: admission.track_state(gen, id) -> generating (head of a running loop) | waiting (with position), and remember_admitted;
            else ledger.was_admitted -> finished; else unknown. Never writes.
admission.Ledger.remember_admitted / was_admitted     # per session, TRACKED_ADMISSION_SECONDS (a day), MAX_TRACKED_ADMISSIONS (1024); outlives the admission records
admission.track_state(gen, id); admission.queue_length(gen)
bridge_js: queueTrack -> {op: "track", queue: {request_ids, bridge_session}}, answered as queueTracked; queueProblem refuses a model_type that is not one
plugin_info.json: 1.4.0, protocol 5, capability "track"
```

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
is not a mode; `plugin_info.json` said 1.3.0, protocol 4, capability `start`.

## The tab's own routes — everything that used to need Gradio

Seven doors, all under `/minipaint-clipboard/`, all gated by the same
`_signed_in` the picture route uses, all answering `Cache-Control: no-store`
and their own `status` sentence for the status line (the two byte-serving
ones answer bytes and a revalidated cache header instead). They exist because every
one of this tab's events was a request with a response and none was ever a
push: what made them fragile was the transport, not the interaction.

    GET  /minipaint-clipboard/library?sort&page&size&selected&refresh
      -> {ok, revision: "<epoch>:<n>", configured, sort, total, page, pages, size,
          selected_page, reason: ""|"empty"|"unconfigured", status,
          items: [{id, name, w, h, bytes, v}]}

`routes.library_page(sort, page, size, selected, refresh)` is the whole of it;
the route is a signed-in wrapper. **Sorting is applied over the whole library
and then sliced** — `store.assets(sort)` already returns the complete ordered
list, so this is a slice of work the store does correctly rather than a second
ordering to keep in step. Bad input is answered, never refused: an unknown
sort falls back to the stored one, a page past the end returns the last page
that exists, a size outside `PAGE_SIZE_MIN..PAGE_SIZE_MAX` is clamped.
`PAGE_SIZE = 60`. `refresh=1` re-reads the folder first.

`revision` is the **library's own** generation (`Store.revision()`), not
`events.revision()`: job, enhancement, runtime and WanGP events move that one
constantly while the library sits still. `v` is `<mtime_ns>-<size_bytes>` —
the same identity both thumbnail caches are keyed by.

    POST /minipaint-clipboard/settings   {sort?, thumbnail?, intercept?}
      -> {ok, status, sort, thumbnail, intercept, menu: {...}}

    GET  /minipaint-clipboard/queue?page=<page id>
    POST /minipaint-clipboard/queue      {action: add|cancel|retry|adopt|dismiss|cancel_all, ...}
      -> {ok, jobs: [...], history: [...], status, queue_button: {label, enabled}, instruction?}

The action list here and the verbs `outbox_view` puts on a card are one thing
in two places, and `tests/test_clipboard_ui.py` holds them together as a
property rather than a second list: a button drawn on a card that this route
answers with "not a queue action" looks, from the tab, exactly like a press
that did nothing.

    GET  /minipaint-clipboard/enhance-settings?variant&mode
    POST /minipaint-clipboard/enhance-settings  {action: toggle|override|restore, ...}

    GET  /minipaint-clipboard/outputs?page&size
      -> {ok, total, page, pages, size, reason: ""|"empty"|"unconfigured", status,
          items: [{id, name, kind: "video"|"image", size, at, exact, prompt, url}]}
    GET  /minipaint-clipboard/output/{file_id}     (also HEAD)
      -> the bytes, or 206 + Content-Range for a Range request

`Accept-Ranges: bytes` is on every answer and **206 is the point of the
route**: a `<video>` served without ranges plays but cannot seek, so the
scrub bar does nothing. One range per request, up to `RANGE_CHUNK`; a header
asking for anything else is answered whole, which is always a correct answer
to a range request. `exact` says whether WanGP named the file or this tab
matched it to the request by when it was written, and the gallery says so on
screen - a match is never presented as a fact.

    POST /minipaint-clipboard/send       (as before)

`GET /image/<id>?thumb=1&v=<version>` answers
`private, max-age=31536000, immutable` when `v` is the asset's **current
canonical** version — read from the file, not from the index — and
`private, max-age=3600` for anything else. An unversioned or falsely versioned
URL must never be blessed immutable.

## `minipaint_neo/clipboard/` — the tab

```python
# __init__.py / config.py / history.py: as in 1.2.0 (the folder, the intercept, the draft and history)

# store.py
THUMBNAIL_CACHE_SIZE = 512; THUMBNAIL_DIR_NAME = "clipboard-thumbnails"; THUMBNAIL_DISK_BUDGET = 64 MiB; THUMBNAIL_NAME_RE
def version_of(asset) -> "<mtime_ns>-<size_bytes>"                                  # what a thumbnail URL carries
class Store:
    revision() -> "<epoch>:<n>"; moved(total=None) -> revision                       # publishes events.LIBRARY
    canonical_version(asset_id) -> str                                               # from the file, for the cache header
    thumbnail_dir(); sweep_thumbnails(budget=THUMBNAIL_DISK_BUDGET) -> {ok, kept, removed, bytes}
# import_bytes / import_image / rename / delete / set_root / a refresh that found a difference all call moved()

# routes.py
LIBRARY_ROUTE; SETTINGS_ROUTE; QUEUE_ROUTE; ENHANCE_SETTINGS_ROUTE; OUTPUTS_ROUTE; OUTPUT_FILE_ROUTE; PAGE_SIZE = 60; PAGE_SIZE_MIN = 10; PAGE_SIZE_MAX = 250
IMMUTABLE_CACHE; REVALIDATED_CACHE; RANGE_CHUNK = 4 MiB
def library_page(sort, page, size, selected="", refresh=False) -> dict; def apply_settings(changes) -> dict
def outputs_page(page=0, size=PAGE_SIZE) -> dict                                      # the gallery; syncs the ledger first, joins the prompt on from the history
def output_url(file_id) -> str; def _byte_range(header, size) -> (start, end) | None  # single-range only; anything else is answered whole
def clamp_size(value) -> int; def page_of(value, pages) -> int                        # zero-based; the pager shows page + 1
def image_url(asset_id, thumb=True, version="")

# outputs.py: the durable ledger behind View Outputs - one entry per request that reached WanGP
VIDEO_SUFFIXES (.mp4 .webm .mkv .mov .m4v); IMAGE_SUFFIXES (.png .jpg .jpeg .webp .gif)   # a GIF is an image: a <video> renders nothing from one
MAX_ENTRIES = 2000; CLOSE_GRACE_SECONDS = 180; CLAIM_MAX_SECONDS = 24h; MAX_SCANNED = 5000; SCHEMA = 1
def folder() -> Path | None                                                           # the outputs_folder setting, else <wangp root>/outputs; absent is not an error
def remember(job_id, paths, request_id="", model="") -> entry                         # the EXACT half: WanGP named these. Called from executor while the job still holds them
def sync()                                                                            # the PULL half: open a claim per admitted request, close the ones whose jobs are done or gone
def files(refresh=True) -> [{id, name, kind, size, at, job_id, request_id, exact}]     # newest first; a file that has left the disk is dropped from the document
def path_of(file_id) -> Path | None                                                   # the only place an id becomes a path
def forget_all() -> int; def use_clock(clock); def reset_for_tests()
# An entry: {entry_id, job_id, request_id, opened_at, closed_at, floor, exact, model, files: [{file_id, path, name, size, at}]}
# floor = the newest file already in the folder when the claim opened. A window claims
# what is strictly newer than that, never what it found - which beats "after the request"
# on a folder full of copied, restored or touched files whose times nobody can vouch for.

# history.py: a record also carries enhanced (bool) and enhanced_prompt (the written prompt; the typed one stays the recipe); make_record(draft, result, enhanced_prompt="")

# ui.py
PREFIX = "minipaint_clipboard"; SLOTS = (("first", "First Frame", "start"), ("last", "Last Frame", "end"), ("ref", "Reference", "references"))
QUEUE_BUTTON_LABEL = "Add to Queue"; QUEUE_BUTTON_BLOCKED = "WanGP is not running"; OUTBOX_LABELS (+ Enhancing); ENHANCE_LABELS; WANGP_LABELS; NO_PAGE = "00000000"; OUTBOX_SHOWN = 40
SP_VARIANT_CHOICES; SP_MODE_CHOICES; QUEUE_JS (prompt, switch -> addToQueue); CANCEL_ALL_JS; MENU_STATE_JS; OPEN_PROMPT_EDITOR_JS (passes its inputs through); CLOSE_PROMPT_EDITOR_JS
SORT_MENU_JS; SEND_MENU_JS (the toolbar's two flyouts); OPEN_OUTPUTS_JS; GRID_MOUNT = ""; LIST_MOUNT = ""
def card_html(slot, label, field, assets, missing=False)                              # the one server-rendered section left; see the V2 list
def outbox_view(jobs, page) -> [dict]; def history_view(records, asset_of) -> [dict]   # content, not nodes: the browser draws it
def job_sentence(job) -> str; def enhance_sentence(job) -> str; def wangp_sentence(job) -> str; def enhance_line_html(availability, enabled) -> str
def job_is_over(job) -> bool     # what the queue stops LISTING: COMPLETED, a browser job whose task left WanGP's queue, or a dismissed one. Never what it stops storing
class ClipboardTab:
    refresh; sort_request; thumbnail_changed; toggle_intercept                        # every refresh output ends with the button's state
                                                                                      # sort_changed is gone with the dropdown: one verb, one write path
    assign(slot, selected); slot_action("clear:<slot>" | "assign:<slot>:<id>"); slot_upload(slot, file, selected); upload(files, selected); pasted(path, selected)
    prompt_changed(prompt)
    toggle_enhance(flag, model) -> enhance line; model_changed(model) -> enhance line
    system_prompt_selected(variant, mode) -> (box, state); apply_override(variant, mode, text) -> (box, state, status); restore_default(variant, mode) -> (box, state, status)
    open_prompt_editor(model, variant, mode) -> (variant, mode, box, state)            # the full-window editor's one backend half: pick the pair a press would use, then read it
    _draft_has_picture_for(variant) -> bool                                            # a picture that variant's model reads (enhance.SLOTS_FOR), resolved through the library exactly as a press does
    cancel_all(page) -> {ok, cancelled, jobs, status}                                   # outbox.cancel_all()
    add_to_queue(prompt, page, model=None, enhance_wanted=None) -> {ok, instruction {nonce, job_id, executor, state} | None, status, jobs, queue_button}
    queue_answer(page) -> {ok, page, jobs, history, status, queue_button, running}      # records history for unrecorded positive Clipboard jobs, once
    outbox_action("cancel|retry|adopt|dismiss:<job>:<page>", page) -> {ok, jobs, status, queue_button?}
    show_history() -> gr.update(visible=True); history_action("load:<id>" | "delete:<id>", prompt)
    _queue_button_view(running=None) -> {label, enabled}                                # the same decision as _queue_button, as facts
    send("<target>:<asset id>:<nonce>", selected)          # minipaint | img2img | inpaint | extras | stitch_*
    choose_folder(text, create); open_folder; open_rename; rename; open_delete; delete
    _queue_button(running=None) -> gr.update(interactive, value); _running() -> outbox.wangp_running()
def create_ui() -> ClipboardTab; def on_ui_tabs() -> [(blocks, "Clipboard", "minipaint_clipboard")]   # a failure gives a note under the same id
```

`grid`, `outbox_list` and `history_list` are **mounts, not renders**: they are
built with an empty value and no event names them as an output. The browser
draws into them from the routes above. The hidden `queue_instruction`,
`outbox_action`, `outbox_refresh` and `page_id` that the queue used to need
are gone, and `tests/test_clipboard_ui.py` asserts both the hidden-component
ceiling (26) and their absence, so the tab cannot quietly keep both doors.

`receive_receipt` is new: the Canvas writes `landed` or `failed` into it for
the receive this send caused, and only `landed` shows the Mini Paint tab. It
is the one hidden box this change added, and it buys Mini Paint the same
Send-to contract every other destination keeps.

`menu_state` carries a `nonce`, so a callback that happened to return the same
settings still reaches the browser — the page watches it to know the
framework's channel is alive, which is the one thing an HTTP request cannot
tell it.

The events that remain around the queue: `queue.click(js: addToQueue, [prompt,
enhance_toggle])`, `cancel_all.click(js: cancelAll)`,
`menu_state.change(js: menuStateChanged)`,
`enhance_toggle.input(toggle_enhance, [enhance_toggle, model] ->
[enhance_line])`, `model.input(model_changed)`, the selectors' and Reload's
`system_prompt_selected`, `sp_apply.click(apply_override)`,
`sp_restore.click(restore_default)`, and `sp_open.click(open_prompt_editor,
[model, sp_variant, sp_mode] -> [sp_variant, sp_mode, system_prompt,
sp_state], js: openPromptEditor)`.

The system prompts have **no accordion and no shape on the tab**: the panel
carries `minipaint-clip-enhance-panel`, which the stylesheet gives
`display: none` until the browser puts `minipaint-clip-fullscreen` on it, and
`sp_open` is the only door. `sp_close.click` is browser-only
(`closePromptEditor`, no backend fn), because closing is a class the page
removes. `openPromptEditor`'s `js=` runs before its fn, so the view is already
open when the fresh variant, mode, box and state land in it; the fn's reload is
why an unapplied edit does not survive a close and re-open.

The tab's browser side, `browser/minipaint_clipboard.js`
(`window.minipaintClipboard`): `attach` (mounts the grid, fetches the library
and the queue, listens for `minipaint:outbox`, pumps once so a reloaded page
resumes its jobs), `library(options)` / `goToPage` / `setSort` (the grid, its
pager and the sort, over the index route; only the newest answer draws, and a
LIBRARY event makes the page re-ask rather than apply a delta),
`libraryState()`, `addToQueue(prompt, switch)`, `cancelAll`, `refreshQueue`,
`queue(instruction)` (→ `minipaintInterop.wangp.pump()`), `pump`, `pageId`,
`modelJson`, `afterRender`, `toggleMenu`, `select`, `setThumbnailSize` (sizes
now, remembers shortly, over `/settings`), `pasteFromClipboard`,
drop-on-card import, the menu, `sendTo` (delivers first, says
`Sent <name> to <destination>, but could not open that tab.` when the picture
landed and the tab would not open, and never resends in that case;
`whySilent` puts the three facts that separate "the event never fired", "it
failed" and "the answer never came back" in the log), `reportTiles` (says when
a thumbnail is *drawn* off-centre, measured through `object-fit` rather than
from the `<img>` box, which is centred whatever the picture does),
`refreshCapabilities` (throttled, never on a timer), `pressHidden`,
`menuStateChanged`, `showOffline`, `openToolbarMenu(section)` (the toolbar's
Sort and Send flyouts: the same list the menu's own sections draw, anchored
under the button that asked and standing alone, so no Back row and a second
press closes it), `openOutputs` / `closeOutputs` / `askOutputs(page)` (the
gallery: the stage, the filmstrip, tap-to-hide the controls, and a pager at
the grid's own 60 - `closeOutputs` clears the player's `src`, because a
`<video>` left with a source keeps downloading the largest file this
extension touches), `openPromptEditor` / `closePromptEditor`
(the full-window system-prompt view: the class, the Escape key, the focus, and
`markEditorChain`, which puts `minipaint-clip-grow` on every wrapper Gradio
built between the panel and the textarea so the box can take the height - the
chain is marked rather than selected because Gradio writes `flex-grow` and
`display` inline), `debug()` (its `editorOpen` says whether the view is up). Job buttons call the queue
route; retry and adopt kick the pump. It sets no colour of its own and
journals no prompt and no filename.

The one standing notice has two sentences and no third: **Forge is not
answering**, raised only when an HTTP request to this extension's own routes
fails; and **the composer's live channel to Forge is down on this page**,
raised when the framework's channel is not delivering and the server is fine,
naming the parts that are stale (`STALE_WITHOUT_THE_CHANNEL`). That list
shrinks as rows move, and when it is empty the second notice stops existing.

## The Canvas and the host

`canvas/ui.py`: `CLIPBOARD_TARGET = "clipboard"` in `DESTINATION_LABELS`;
`current()` (the TouchCanvas of the UI being built); `receive()` intercepts to
Clipboard when `clipboard.intercept_enabled()` and passes through on failure;
`after_receive(state)` → which tab the follow-up step switches to;
`receive_picture(image, state, mode, origin, label)`;
`receive_from(event, provider, inputs, origin="clipboard", label="Clipboard",
receipt=None)` wires an outside trigger into the Canvas's own structural
receive chain; with a `receipt` textbox it writes `RECEIVED` / `NOT_RECEIVED`
for **that** receive (keyed by the exact trigger value, so two pages sending
at once cannot read each other's outcome) and shows the Canvas tab only for
one that landed. `send()` routes `clipboard` to `_send_to_clipboard()`.
`canvas/host.py`: `gallery_file(payload)` — the file a gallery item stands
for, only through the host's own `check_tmp_file`.

`browser/minipaint_canvas.js`: **deliver, prove, then show.**
`switchTo(target)` returns `{ok, reason}` and reads the answer off the page —
a host helper that exists but left a different tab showing is a failed
switch, and `tabShowing(panelId)` is how that is known. `deliverToHost(...)`
answers `{ok, reason, switched, switchReason}`, so delivery and navigation
are separate facts: nothing navigates before `still_holds()` has passed, and
a picture proved to have landed in a tab that will not open is said, not
resent. `deliverTheOldWay` no longer navigates on its own — the caller
decides, through `landed(ok, reason, name)`, and only after a delivery that
actually happened.

## Tests

`tests/test_wangp_queue.py` (the queue inside the plugin: overlay, admit,
confirm, restore — on a stub host with the real component names),
`tests/test_wangp_start.py` (protocol 4's start decision, started versus
queued, the depth, the fail-safe direction), `tests/test_interop.py`
(staging, normalising, preparing, the routes, the start mode),
`tests/test_clipboard_store.py` (the library, containment, the documents),
`tests/test_clipboard_outbox.py` (the outbox's rules and its routes),
`tests/test_clipboard_enhance.py` (the enhancer against a fake `mc_llm_api`
shaped like its document: the loader off a folder, the variant and slot
rules, the overrides on disk, the enhancing stage in the line, how a run
ends, cancel everything, retry, tracking, the routes and the tab's panel),
`tests/test_clipboard_outputs.py` (the ledger and the gallery's two routes:
the exact half against WanGP's own paths, the window half against a folder
whose files appear while a job runs, a claim a shutdown left open finished at
the next sync, exclusive claiming, the 60-page, and the byte route's ranges),
`tests/test_clipboard_ui.py` (the tab on a Forge-shaped page, every event,
the outbox flow, the blocked button, the intercept, Send to Clipboard, the
fallback, the theming rules), `tests/test_queue_e2e.py` (a WanGP-shaped
Gradio app with Wan2GP's variable names and both triggers, the bridge placed
by `insert_after`, driven through Gradio's predict endpoint with the test
playing the browser, the outbox in the middle, an enhanced press on an H3
model through to `track`). `tests/test_wangp_queue.py` also covers `track`
and the `MODEL_CHANGED` guard on the stub host. The lock, the GPU report and
the emergency restart are in `tests/test_wangp_runtime.py`. All in
`tests/run.py`.
