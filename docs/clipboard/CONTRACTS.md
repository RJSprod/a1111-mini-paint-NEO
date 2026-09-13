# Clipboard and the WanGP queue: module contracts

The one place the modules agree on each other's names, in the same spirit as
`docs/wangp/CONTRACTS.md`, which this extends. Every signature here is
load-bearing: another module, the tab, the browser, a third-party extension
or a test calls it. House style as there: `from __future__ import
annotations`, docstrings that say *why*, `typing.Optional[...]`, nothing the
WebUI does not already ship, nothing that polls forever, nothing that can
raise into WanGP.

---

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
OUTBOX_NAME = "clipboard-outbox.json"           # {"schema": 2, "jobs": [...]}, atomic, quarantined when broken
ENHANCING, PENDING, SENDING, QUEUED, STARTED, FAILED, UNCONFIRMED, CANCELLED; TERMINAL; POSITIVE = (QUEUED, STARTED); WAITING = (ENHANCING, PENDING)
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
```

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

## `wan2gp_bridge/wan2gp-minipaint-bridge/` — bridge 1.4.0

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

## `minipaint_neo/clipboard/` — the tab

```python
# __init__.py / config.py / store.py / history.py / routes.py: as in 1.2.0 (the folder, the intercept, the library, the draft and history, the image and import routes)

# history.py: a record also carries enhanced (bool) and enhanced_prompt (the written prompt; the typed one stays the recipe); make_record(draft, result, enhanced_prompt="")

# ui.py
PREFIX = "minipaint_clipboard"; SLOTS = (("first", "First Frame", "start"), ("last", "Last Frame", "end"), ("ref", "Reference", "references"))
QUEUE_BUTTON_LABEL = "Add to Queue"; QUEUE_BUTTON_BLOCKED = "WanGP is not running"; OUTBOX_LABELS (+ Enhancing); ENHANCE_LABELS; WANGP_LABELS; NO_PAGE = "00000000"; OUTBOX_SHOWN = 40
SP_VARIANT_CHOICES; SP_MODE_CHOICES; ARM_QUEUE_JS returns [prompt, page id, model json]; AFTER_CANCEL_JS
def grid_html(assets, selected, configured); def card_html(slot, label, field, assets, missing=False); def history_html(records, asset_of)   # + the enhanced line
def outbox_html(jobs, page) -> str; def job_sentence(job) -> str; def enhance_sentence(job) -> str; def wangp_sentence(job) -> str; def enhance_line_html(availability, enabled) -> str
class ClipboardTab:
    refresh; sort_changed; sort_request; thumbnail_changed; toggle_intercept          # every refresh output ends with the button's state
    assign(slot, selected); slot_action("clear:<slot>" | "assign:<slot>:<id>"); slot_upload(slot, file, selected); upload(files, selected); pasted(path, selected)
    prompt_changed(prompt)
    toggle_enhance(flag, model) -> enhance line; model_changed(model) -> enhance line
    system_prompt_selected(variant, mode) -> (box, state); apply_override(variant, mode, text) -> (box, state, status); restore_default(variant, mode) -> (box, state, status)
    cancel_all(page) -> (outbox html, status)                                           # outbox.cancel_all()
    prepare_queue(prompt, page, model) -> (instruction json {nonce, job_id} | "", status, outbox html, button)   # outbox.submit(..., "clipboard", model=); WANGP_NOT_RUNNING and ENHANCE_* refuse
    refresh_outbox(page) -> (outbox html, status, history html, button)                # records history for unrecorded positive Clipboard jobs, once (typed prompt + written prompt)
    outbox_action("cancel|retry|adopt:<job>:<page>:<nonce>", page) -> (outbox html, status)
    show_history(); history_action("load:<id>" | "delete:<id>", prompt)
    send("<target>:<asset id>:<nonce>", selected)          # minipaint | img2img | inpaint | extras | stitch_*
    choose_folder(text, create); open_folder; open_rename; rename; open_delete; delete
    _queue_button(running=None) -> gr.update(interactive, value); _running() -> outbox.wangp_running()
def create_ui() -> ClipboardTab; def on_ui_tabs() -> [(blocks, "Clipboard", "minipaint_clipboard")]   # a failure gives a note under the same id
```

Components added for the queue: the hidden `page_id` (the page writes its
identity into it on attach; the Add to Queue click's JS returns it as the
second input), the hidden `model` (the page writes the WanGP model the public
API's capabilities answer named, once per change; the click's JS returns it
as the third input), the hidden `outbox_action` textbox, the hidden
`outbox_refresh` button, the `cancel_all` button and the `outbox_list` HTML
under the button; for enhancement, the `enhance_panel` accordion with
`enhance_line`, `enhance_toggle`, `sp_variant`, `sp_mode`, `system_prompt`,
`sp_state`, `sp_apply`, `sp_restore`, `sp_reload`. The events:
`queue.click(prepare_queue, [prompt, page_id, model] -> [queue_instruction,
queue_status, outbox_list, queue])`, `queue_instruction.change(js: pump)`,
`outbox_refresh.click(refresh_outbox, [page_id] -> [outbox_list,
queue_status, history_list, queue])`, `outbox_action.input(outbox_action,
[outbox_action, page_id] -> [outbox_list, queue_status])`,
`cancel_all.click(cancel_all, [page_id] -> [outbox_list, queue_status]).then(js:
afterCancelAll)`, `enhance_toggle.input(toggle_enhance, [enhance_toggle,
model] -> [enhance_line])`, `model.input(model_changed)`, the selectors' and
Reload's `system_prompt_selected`, `sp_apply.click(apply_override)`,
`sp_restore.click(restore_default)`.

The tab's browser side, `javascript/minipaint_clipboard.js`
(`window.minipaintClipboard`): `attach` (writes the page id, listens for
`minipaint:outbox`, pumps once so a reloaded page resumes its jobs),
`afterRender`, `toggleMenu`, `select`, `setThumbnailSize`,
`pasteFromClipboard`, drop-on-card import, the menu, `sendTo`, `armQueue` (a
bounded watcher on the instruction box), `queue(instruction)` (→
`minipaintInterop.wangp.pump()`), `pump`, `pageId`, `modelJson` (the model
the press carries), `afterCancelAll` (→ `refreshWaiters`), `refreshCapabilities`
(throttled, never on a timer; the line says generating / idle; writes the
model box through `sendModel` once per change), `pressHidden`. `attach` also
resumes the public API's tracking of this page's queued jobs. Job buttons
write `outbox_action`; retry and adopt kick the pump; `waiting` and
`tracked` events re-render the list. It sets no colour of its own and
journals no prompt and no filename.

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
`tests/test_clipboard_enhance.py` (the enhancer against a fake `mc_llm_api`
shaped like its document: the loader off a folder, the variant and slot
rules, the overrides on disk, the enhancing stage in the line, how a run
ends, cancel everything, retry, tracking, the routes and the tab's panel),
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
