# Clipboard and the WanGP queue: module contracts

The one place the modules agree on each other's names, in the same spirit as
`docs/wangp/CONTRACTS.md`, which this extends. Every signature here is
load-bearing: another module, the tab, the browser, a third-party extension
or a test calls it. House style as there: `from __future__ import
annotations`, docstrings that say *why*, `typing.Optional[...]`, nothing the
WebUI does not already ship, nothing that polls forever.

---

## The public contract — `minipaint.wangp.queue/v1`

```
window.minipaintInterop = {
    version: 1,
    contract: "minipaint.wangp.queue/v1",
    wangp: {
        capabilities(): Promise<{ ok, ready, model, inputs: {start, end, references: {supported}}, api_version, contract } | { ok: false, code, message }>,
        stageImage(input: Blob | File | ArrayBuffer | TypedArray | HTMLCanvasElement | dataURL, options?): Promise<{ ok, image: {kind: "staged", id}, width, height } | { ok: false, code, message }>,
        enqueue(request): Promise<Result>,
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
}
Handle = { kind: "staged" | "clipboard_asset", id: <32 lowercase hex> }

Result (ok)  = { ok: true,  status: "queued", request_id, tasks_added, model: {type, label},
                 applied: {prompt, start, end: bool, references: int}, inherited: [field], ignored: [{field, code}] }
Result (not) = { ok: false, status: "refused" | "unconfirmed", request_id, code, message }
```

Guarantees: sparse (omitted is inherit), ids never paths, one FIFO per page,
one owner of the WanGP form per bridge session (`QUEUE_BUSY` otherwise),
idempotent by request id (`REQUEST_ID_CONFLICT` for the same id with another
payload), a bounded answer (`QUEUE_REQUEST_TIMEOUT_MS` + `QUEUE_CONFIRM_TIMEOUT_MS`),
`queued` only on WanGP's own evidence, `refused` only on correlated evidence,
`unconfirmed` otherwise. No Generate, no abort, no progress, no settings.

Codes a caller can meet: `REQUEST_INVALID`, `PROMPT_TOO_LONG`,
`IMAGE_STAGE_INVALID`, `IMAGE_STAGE_EXPIRED`, `CLIPBOARD_NOT_CONFIGURED`,
`CLIPBOARD_ASSET_UNKNOWN`, `CLIPBOARD_ASSET_OUTSIDE_ROOT`, `IFRAME_NOT_READY`,
`BRIDGE_COMPONENT_INCOMPATIBLE`, `BRIDGE_SESSION_MISMATCH`, `QUEUE_BUSY`,
`REQUEST_ID_CONFLICT`, `QUEUE_REQUEST_REFUSED`, `WANGP_VALIDATION_REFUSED`,
`ADMISSION_UNCONFIRMED`, `HANDOFF_*`, `INTERNAL_ERROR`. Every one has a
sentence in `minipaint_neo/wangp/errors.py`.

## `javascript/minipaint_interop.js`

Defines `window.minipaintInterop` (above). `normaliseRequest(raw)` applies the
sparse rules and mints an id; `stageImage` posts bytes to `STAGE_ROUTE` (32 MiB
ceiling, `image/*` only); `enqueue` chains on one promise (`chain`) and, per
request: refuses at once without a live bridge (`IFRAME_NOT_READY`) or a bridge
without the queue capability (`BRIDGE_COMPONENT_INCOMPATIBLE`); posts
`{request}` to `PREPARE_ROUTE` when the request carries images and takes the
wire request back; calls `minipaintWanGP.queueAndConfirm(wire)`; posts the
handoff ids to `RELEASE_ROUTE` whatever happened; and returns `publicResult`,
which never echoes the prompt or a path. Journal lines go through
`minipaintWanGP.note` under `browser` (`enqueue <id8>: prompt, start (N image(s) prepared)`).

## `minipaint_neo/interop.py` — the API's server half

```python
STAGE_ROUTE   = "/minipaint-interop/stage"      # POST bytes (Content-Type image/png|jpeg|webp|octet-stream) -> {ok, image:{kind:"staged", id}, width, height}
PREPARE_ROUTE = "/minipaint-interop/prepare"    # POST {request} -> {ok, request: wire} | {ok:false, code, message}
RELEASE_ROUTE = "/minipaint-interop/release"    # POST {handoff_ids} -> {ok, released}
CONTRACT_ROUTE = "/minipaint-interop/contract"  # GET -> contract()
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
def normalize_public_request(raw) -> dict                    # sparse rules; mints request_id; PROMPT_TOO_LONG; at most 16 references
def prepare(request) -> dict                                 # -> {request_id, prompt?, start_handoff_id?, end_handoff_id?, reference_handoff_ids?, handoff_ids}
def release(handoff_ids) -> int
def install(app) -> None                                     # once; routes ahead of the host's catch-all; behind the sign-in gate
def register(script_callbacks) -> None                       # on_app_started -> install
```

`prepare` opens a `staged` handle from the staging root and a
`clipboard_asset` handle through `clipboard.store.open_image`, writes each as
an ordinary handoff (`wangp.handoff.write`) and, on any failure, releases what
it had already written: a half-prepared request is not queued.

## `minipaint_neo/wangp/protocol.py` — protocol 3

Added to the SHARED block (mirrored byte-for-byte in the bridge's copy, and in
`javascript/minipaint_wangp.js`):

```python
PROTOCOL = 3
QUEUE_REQUEST = "WANGP_QUEUE_REQUEST"; QUEUE_RESULT = "WANGP_QUEUE_RESULT"
QUEUE_CONFIRM = "WANGP_QUEUE_CONFIRM"; QUEUE_STATUS = "WANGP_QUEUE_STATUS"
QUEUE_CONTRACT = "minipaint.wangp.queue/v1"; PUBLIC_API_VERSION = 1
QUEUE_FIELDS = ("prompt", "start", "end", "references"); QUEUE_FIELD_RECEIVERS = {start: START_FRAME, end: END_FRAME, references: REFERENCE}
PROMPT_MAX_CHARS = 4000; MAX_QUEUE_REFERENCES = 16
ADMISSION_REQUESTED / ADMISSION_DUPLICATE / ADMISSION_REFUSED
QUEUE_PENDING / QUEUE_QUEUED / QUEUE_REFUSED / QUEUE_EXPIRED
QUEUE_REQUEST_TIMEOUT_MS = 30000; QUEUE_CONFIRM_TIMEOUT_MS = 10000; QUEUE_CONFIRM_DELAYS_MS = (0, 100, 300, 700, 1500, 3000)
PENDING_ADMISSION_SECONDS = 20.0; ADMISSION_RECORD_SECONDS = 600.0; MAX_ADMISSION_RECORDS = 512
def valid_request_id(value) -> bool                 # 32 lowercase hex, the handoff id's shape
def clean_prompt(value) -> Optional[str]            # Cc dropped (not \n \t), CRLF -> \n, stripped; empty -> None
def normalize_queue_request(raw) -> (request, code) # the wire shape: request_id, prompt?, start_handoff_id?, end_handoff_id?, reference_handoff_ids?, bridge_session?
def queue_overrides(request) -> list[str]
def queue_payload_hash(request) -> str              # what REQUEST_ID_CONFLICT compares
def queue_summary(applied, inherited, ignored) -> dict
def normalize_queue_result(raw) -> dict; def normalize_queue_status(raw) -> dict
```

`MAX_ENVELOPE_BYTES` stays 256 KiB: a 4000-character prompt is under 24 KiB as JSON.

Wire messages (parent → iframe): `WANGP_QUEUE_REQUEST {protocol, channel_id, request_id, queue: {...}}`,
`WANGP_QUEUE_CONFIRM {…, queue: {request_id}}`; answers: `WANGP_QUEUE_RESULT`,
`WANGP_QUEUE_STATUS`, each carrying the bridge's acknowledgement with `ok`,
`admission` / `status`, `queue_request_id`, `applied`, `inherited`, `ignored`,
`tasks_added`, `model`, `code`, `restored`, `restore_skipped`. The envelope's
`request_id` is the correlation id; the payload's is the public request id.

## `javascript/minipaint_wangp.js` — the parent's queue calls

```
minipaintWanGP.queue(request)            -> Promise<result>        one WANGP_QUEUE_REQUEST, answered or timed out (QUEUE_REQUEST_TIMEOUT_MS)
minipaintWanGP.confirmQueue(requestId)   -> Promise<status>        one WANGP_QUEUE_CONFIRM
minipaintWanGP.queueAndConfirm(request)  -> Promise<{ok, status: queued|refused|unconfirmed, request_id, tasks_added, applied, inherited, ignored, model, code, message, detail}>
minipaintWanGP.capabilities()            -> Promise<{ok, ready, model, inputs}>   one bounded receivers query
minipaintWanGP.state().queue             -> the handshake's capabilities.queue
```

`queueAndConfirm` confirms at `QUEUE_CONFIRM_DELAYS_MS` until the answer is
terminal or `QUEUE_CONFIRM_TIMEOUT_MS` is spent; a `pending` at the end is
`unconfirmed`. Same origin, source and channel checks as every other message.

## `wan2gp_bridge/wan2gp-minipaint-bridge/` — the queue half (bridge 1.2.0)

`OPERATIONS = ("hello", "receivers", "receive", "queue", "confirm")`. The one
event's outputs are the acknowledgement, the receivers, the switch components,
then the queue-only components in `compatibility.queue_components()` order:
`prompt`, `wizard_prompt`, `client_id`, `add_to_queue_trigger`.
`QUEUE_CRITICAL = (client_id, add_to_queue_trigger, prompt, state)`: a build
lacking one keeps the image send and refuses `queue` with
`BRIDGE_COMPONENT_INCOMPATIBLE`; the handshake says `capabilities.queue` and
`queue_missing`. Globals asked for: `get_unique_id`, `get_gen_info` (plus the
model globals).

```python
MiniPaintBridge.queue(raw, bridge_session, live) -> (ack, writes)    # section 14, in order:
    normalize -> session/components -> duplicate (answer from the record) / REQUEST_ID_CONFLICT
    -> owner still pending: QUEUE_BUSY; owner expired: its compare-before-restore first
    -> every handoff loaded (any failure refuses the whole request, nothing written)
    -> per image field: not offered by this page's model -> ignored {field, RECEIVER_DISABLED}; else a *replacement*
       list into the gallery, plus the same switch a menu send makes (selector, letters, row)
    -> prompt into wizard_prompt when wizard_prompt_activated_var == "on", else prompt
    -> client_id = request_id; add_to_queue_trigger = get_unique_id()
    -> a PendingAdmission is recorded (originals, what was written, loose signatures of the pictures)
MiniPaintBridge.confirm(raw, bridge_session, live) -> (answer, writes)
    queued  when admission.matching_tasks(get_gen_info(state), request_id) > 0   (sticky: seen_queued)
    refused when admission.refusal_evidence(gen, request_id)                     (queue_errors keyed by, or naming, the id)
    expired when PENDING_ADMISSION_SECONDS is up; else pending
    a terminal state runs the restore: admission.restore_plan(record, live)
```

`admission.py`: `PendingAdmission`, `Ledger` (per bridge session, bounded by
`MAX_ADMISSION_RECORDS`, terminal records kept `ADMISSION_RECORD_SECONDS`),
`RESTORE_GROUPS` (a selector and its letter string go back together or not at
all), `letters()` (letter strings compared as sets), `gallery_digests()` /
`galleries_match()` (galleries compared entry by entry with
`handoff.loose_signature` + `handoff.signatures_match`: size and an 8×8 box
thumbnail within `SIGNATURE_TOLERANCE`, because Gradio caches what a gallery
shows as lossy WebP and exact pixels never match again), `unchanged()`,
`restore_plan()`, `matching_tasks()`, `refusal_evidence()`.

`bridge_ui.Raw` wraps a value that must be written as it is (a restored
original that happens to be a mapping); `update_for` unwraps it.
`bridge_js.py` handles `queueRequest` / `queueConfirm` in the page and
rewrites `queue_request_id` to `request_id` in what it posts back.

## `minipaint_neo/clipboard/`

```python
# __init__.py
TAB_LABEL = "Clipboard"; TAB_ID = "minipaint_clipboard"
def intercept_enabled() -> bool          # read from clipboard.json on every call; never raises
def available() -> bool
def register(script_callbacks) -> None   # on_ui_tabs -> ui.on_ui_tabs, on_app_started -> routes.install; each contained

# config.py
CONFIG_NAME = "clipboard.json"; INDEX_NAME = "clipboard-index.json"; DRAFT_NAME = "clipboard-draft.json"; HISTORY_NAME = "clipboard-history.json"
SORT_MODES = ("name_asc", "name_desc", "newest", "oldest", "largest", "smallest"); DEFAULT_SORT = "newest"
THUMBNAIL_MIN, THUMBNAIL_DEFAULT, THUMBNAIL_MAX = 72, 144, 320; DEFAULT_INTERCEPT = False
class Config: schema_version; storage_root; root_id; intercept; sort; thumbnail; configured (property)
def use_config_dir(directory) -> None    # test seam, like wangp.config's
def config_dir() -> Path                 # wangp_config.config_dir()
def read_document(name, default); def write_document(name, payload)   # atomic; a broken file is quarantined
def quarantine(path, why) -> None
def load() -> Config; def save(config) -> None; def update(**changes) -> Config
def root_id_for(path) -> str; def clamp_thumbnail(value) -> int

# store.py
MAX_BYTES = MAX_HANDOFF_BYTES; MAX_PIXELS; MAX_SIDE; SOURCES = ("upload", "paste", "forge_gallery", "minipaint", "external", "slot")
class Asset: asset_id; root_id; relative_path; filename; size_bytes; width; height; mime; sha256; mtime_ns; imported_at; source; public()
def safe_basename(name) -> str           # REQUEST_INVALID for separators, a leading dot, reserved names, nothing before the extension
def unique_name(folder, name) -> str     # "name (2).ext"
def inspect_bytes(data) -> (width, height, mime)   # PNG/JPEG/WebP, still, within the ceilings
def validate_root(text, create=False) -> (Path | None, code, detail)   # ROOT_EMPTY, ROOT_MISSING, ROOT_NOT_A_DIRECTORY, ROOT_UNWRITABLE
class Store:
    configured(); root(); set_root(text, create) -> {ok, path | code, message}
    get(asset_id); assets(sort); resolve(asset_id) -> (Asset, Path)      # containment: plain filename, no symlink, regular file, inside the root
    open_image(asset_id); read_bytes(asset_id); refresh() -> [Asset]      # bytes-identical files keep their id
    import_bytes(data, filename, source) -> Asset; import_image(image, basename, source) -> Asset   # a PNG, every byte kept / pixels as PNG
    rename(asset_id, new_name, allow_suffix=False) -> Asset; delete(asset_id) -> Asset
    thumbnail(asset_id, side=320) -> (bytes, mime)                        # LRU, in memory
    last_import; forget()
def store() -> Store; def reset_for_tests() -> None; def open_image(asset_id)

# history.py
MAX_HISTORY = 200; MODE_OVERRIDE / MODE_INHERIT / MODE_IGNORED
def empty_draft(); def normalize_draft(raw); def load_draft(); def save_draft(draft); def draft_overrides(draft)
def public_request(draft, request_id="") -> dict          # the minipaint.wangp.queue/v1 request; inherited fields omitted
def normalize_record(raw); def load_history(); def add_history(record); def delete_history(history_id)
def make_record(draft, result) -> dict                     # prompt text only when it was an override
def draft_from_record(record, available) -> (draft, missing)

# routes.py
IMAGE_ROUTE = "/minipaint-clipboard/image/{asset_id}"     # GET, ?thumb=1; 404 unknown/unconfigured, 403 outside the root
IMPORT_ROUTE = "/minipaint-clipboard/import"              # POST bytes, X-MiniPaint-Filename, ?source=
def image_url(asset_id, thumb=True, version="") -> str    # the id, never the name
def install(app) -> None                                  # once; behind the sign-in gate

# ui.py
PREFIX = "minipaint_clipboard"; SLOTS = (("first", "First Frame", "start"), ("last", "Last Frame", "end"), ("ref", "Reference", "references"))
def grid_html(assets, selected, configured); def card_html(slot, label, field, assets, missing=False); def history_html(records, asset_of)
class ClipboardTab:
    refresh; sort_changed; sort_request; thumbnail_changed; toggle_intercept
    assign(slot, selected); slot_action("clear:<slot>" | "assign:<slot>:<id>"); slot_upload(slot, file, selected); upload(files, selected); pasted(path, selected)
    prompt_changed(prompt); prepare_queue(prompt, session) -> (instruction json {nonce, request}, status, session); queue_result(json, session)
    show_history(); history_action("load:<id>" | "delete:<id>", prompt)
    send("<target>:<asset id>:<nonce>", selected)          # minipaint | img2img | inpaint | extras | stitch_*
    choose_folder(text, create); open_folder; open_rename; rename; open_delete; delete
    build(); _wire(**components)
def create_ui() -> ClipboardTab; def on_ui_tabs() -> [(blocks, "Clipboard", "minipaint_clipboard")]   # a failure gives a note under the same id
```

The tab's browser side, `javascript/minipaint_clipboard.js` (`window.minipaintClipboard`):
`attach`, `afterRender`, `toggleMenu`, `select`, `setThumbnailSize` (the
`--minipaint-clip-thumb` variable), `pasteFromClipboard`, drop-on-card import,
the menu, `sendTo`, `armQueue` (a bounded watcher on the instruction box, 15 s),
`queue(instruction)` (→ `minipaintInterop.wangp.enqueue`, then the result into
the hidden result box), `refreshCapabilities` (throttled 2.5 s, never on a
timer), `pressHidden`. It sets no colour of its own and journals no prompt and
no filename.

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
`tests/test_interop.py` (staging, normalising, preparing, the routes),
`tests/test_clipboard_store.py` (the library, containment, the documents),
`tests/test_clipboard_ui.py` (the tab on a Forge-shaped page, every event,
the intercept, Send to Clipboard, the fallback, the theming rules),
`tests/test_queue_e2e.py` (a WanGP-shaped Gradio app with Wan2GP's variable
names, the bridge placed by `insert_after`, driven through Gradio's predict
endpoint with the test playing the browser). All in `tests/run.py`.
