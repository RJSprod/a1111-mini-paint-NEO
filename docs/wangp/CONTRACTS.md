# WanGP integration: module contracts

The one place the modules agree on each other's names. Every signature here
is load-bearing: another module, the tab, the browser or a test calls it.
`minipaint_neo/wangp/errors.py` and `minipaint_neo/wangp/protocol.py` are the
shared vocabulary and are already written.

House style, which this integration follows: `from __future__ import
annotations`; module docstrings that say *why*; `typing.Optional[...]` rather
than `X | None`; no third-party dependency the WebUI does not already have
(Gradio, FastAPI/Starlette, Pillow, httpx come with Forge — `pynvml` does
not, so GPU discovery must degrade to `nvidia-smi` and then to nothing);
nothing that watches the whole document or polls forever.

---

## `config.py` — what survives a restart

```python
SCHEMA_VERSION = 2
class Config:                       # a plain dataclass-like holder
    schema_version: int
    initialized: bool
    wangp_root: str
    runtime: dict                   # {"type","prefix","display_name","launch_strategy"}
    gpu: dict                       # {"uuid": "GPU-..."}
    integration: dict               # {"proxy_path": "/wan2gp", "auto_start": "lazy"}
    def as_dict(self) -> dict
    @classmethod
    def from_dict(cls, data: dict) -> "Config"     # raises IntegrationError(CONFIG_SCHEMA_TOO_NEW)
    def validate(self) -> list                     # [] or a list of failure codes

def config_dir() -> pathlib.Path        # <data_path>/a1111-mini-paint-NEO, made if absent
def config_path() -> pathlib.Path       # .../wan2gp.json
def backup_path() -> pathlib.Path       # .../wan2gp.backup.json
def pending_path() -> pathlib.Path      # .../wan2gp.pending.json
def runtime_dir() -> pathlib.Path       # .../runtime  (never in git, never persisted state)

def load() -> Optional[Config]          # None when absent; IntegrationError when unreadable/too new
def write_pending(config: Config) -> None
def promote_pending() -> Config         # active -> backup, pending -> active, atomically
def restore_backup() -> Optional[Config]
def clear() -> None                     # mark uninitialised, keep the backup
def atomic_write(path, text: str) -> None   # temp in the same directory, flush, fsync, os.replace
```

`config_dir()` asks the host for its data root
(`modules.paths_internal.data_path`, then `modules.paths.data_path`, then
`modules.shared.cmd_opts.data_dir`) and falls back to the extension folder
with a printed line saying so. Never persist a PID, a port, a nonce, a
secret, a session hash, a handoff id or a receiver cache.

## `discovery.py` — is this real

```python
class Gpu:  index:int; name:str; uuid:str; total_mb:int; bus_id:str
    @property label -> "NVIDIA GeForce RTX 5090 - 32 GB"

def validate_root(path) -> list           # [] or failure codes; wants wgp.py + shared/
def bridge_dir(root) -> pathlib.Path      # <root>/plugins/wan2gp-minipaint-bridge
def find_conda_environments() -> list     # [{"type","prefix","display_name"}], best effort, never raises
def interpreter_for(runtime: dict) -> Optional[pathlib.Path]
def probe_runtime(runtime: dict, root) -> tuple   # (ok: bool, strategy: str, detail: str)
def list_gpus() -> list                   # [Gpu]; NVML, then nvidia-smi, then []
def find_gpu(uuid: str) -> Optional[Gpu]  # exact UUID match only, never a fallback
```

`probe_runtime` decides `"direct_python"` vs `"conda_run"` by actually
running a harmless command, and returns the one that worked.

## `handoff.py` — PNGs on the way out

```python
class Handoff:  id:str; path:pathlib.Path; manifest:dict
def handoff_root() -> pathlib.Path        # config.runtime_dir()/handoff, created 0o700 where supported
def new_id() -> str                       # secrets.token_hex(16), matches protocol.HANDOFF_ID_RE
def write(image) -> Handoff               # PIL image -> PNG; manifest holds sha256, size, dims
def path_for(handoff_id: str) -> pathlib.Path   # IntegrationError(HANDOFF_INVALID_ID) unless valid
def resolve(handoff_id: str) -> pathlib.Path    # + exists, regular file, inside root after resolve()
def discard(handoff_id: str) -> None      # only under the root, never raises
def sweep(max_age_seconds: int = 6*3600) -> int # stale files under the root only
def manifest_of(handoff_id) -> Optional[dict]   # from memory; forgotten after discard
```

## `runtime.py` — the child process

```python
STOPPED STARTING READY STOPPING CRASHED INCOMPATIBLE REINIT_REQUIRED   # str constants
class Runtime:                              # one module-level instance, `current()`
    state: str
    instance_id: str        # fresh per launch, never persisted
    backend_port: int
    error_code: str
    error_detail: str
    def snapshot(self) -> dict              # safe to show; no secret, no full path
def current() -> Runtime
def build_environment(config, port, instance_id, secret) -> dict
def command_line(config, port) -> list      # --server-name 127.0.0.1 --server-port N; never --listen/--share
def free_port() -> int                      # bind 127.0.0.1:0, read it, close
def start(config) -> Runtime                # under a lock; one child, bounded port retries
def stop(timeout: float = 20.0) -> None     # only the tracked child's group/job
def restart(config) -> Runtime
def health() -> dict
```

`build_environment` starts from a copy of `os.environ` with
`CUDA_VISIBLE_DEVICES` **set to the configured UUID** (never inherited),
`GRADIO_ROOT_PATH=/wan2gp`, `MINIPAINT_WANGP_INSTANCE_ID`,
`MINIPAINT_WANGP_HANDOFF_ROOT`, `MINIPAINT_WANGP_BRIDGE_SECRET`; Forge's
interpreter variables (`PYTHON*`) and allocator variables
(`FORGE_ALLOCATOR_VARIABLES`: `PYTORCH_CUDA_ALLOC_CONF`, `PYTORCH_ALLOC_CONF`)
are removed. Windows:
`CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB` and a Job Object with
kill-on-close when `pywin32`/`ctypes` allow it. POSIX: `start_new_session`
and `killpg` on the tracked pgid only. Never a name-based kill.

## `lock.py` / `vram.py` — one WanGP per machine, and what the card holds

```python
# lock.py  (<runtime dir>/wangp.lock.json: {"schema", "forge_pid", "forge_start", "created_at", "host"} and nothing else)
def claim() -> dict            # raises IntegrationError(WANGP_ALREADY_MANAGED) when another *live* Forge holds it; a stale lock is removed
def release() -> bool          # only when it is this Forge's
def holder() -> Optional[dict] # the other live Forge, or None
def sweep() -> bool            # at app start: drop a lock nobody live holds
def pid_alive(pid) -> bool     # signal 0 / a query handle; never a signal that does anything
def process_start(pid) -> str  # /proc start time where the platform has it: a recycled pid is not the Forge that took the lock
def status() -> dict           # {"state": none|ours|other|stale, "summary"} for diagnostics

# vram.py  (nvidia-smi, best effort, never raising; a process name is its last component only)
def snapshot(uuid, runner=subprocess.run) -> {"available", "memory": {"used_mb", "total_mb"} | None, "processes": [{"pid", "used_mb", "name"}], "detail"}
def parse_memory(text); def parse_compute_apps(text); def mib(value) -> str
```

`runtime.start` claims the lock after validation and before the spawn; `stop`, a
crash and a failed launch release it. `Runtime.tree_pids()` names every pid in the
child's process group (Linux, from `/proc`; the leader elsewhere), and
`Runtime.emergency_restart(config, gpu_uuid, runner, start_async, ...) -> report`
reads the card, stops, waits for the tree to be gone (escalating to exactly the pids
proved ours), reads the card again, invalidates the sessions and the outbox's jobs in
flight, and starts again - synchronously with the keywords, or through
`start_async` (the tab passes `request_start`). The report is `{ok, steps, before,
after, pids, remaining, freed_mb, verified, started}`; `ui.restart_report_markdown`
renders it. A launch in progress is ended first (`_launching`), so the emergency
restart works while a start is stuck.

## `proxy.py` — `/wan2gp/*` on the Forge origin

```python
PROXY_PATH = "/wan2gp"
def install(app) -> None            # from on_app_started; adds the routes + a Starlette WS route
def upstream() -> Optional[str]     # "http://127.0.0.1:<port>" from runtime.current(), or None
async def forward(request) -> Response          # streamed both ways
HOP_BY_HOP = frozenset({...})       # stripped in both directions
def forwarded_headers(request) -> dict          # Host/X-Forwarded-Host/-Proto/-Port/-For
async def probe(client=None) -> dict            # base page + one asset + root-path check
```

The upstream host and port come **only** from `runtime.current()`. No query
parameter, header or path segment may influence them. `/wan2gp` redirects to
`/wan2gp/`. Timeouts are three classes: connect (short), ordinary response
(medium), stream idle (long or none) — a generation must never be cut.

## `bridge.py` — which page is talking to which session

```python
class Session:  channel_id; bridge_session; instance_id; state_revision; receivers; model; updated
class Registry:
    def hello(self, channel_id, instance_id) -> dict
    def ready(self, channel_id, payload) -> Session
    def receivers(self, channel_id, payload) -> Session
    def get(self, channel_id) -> Optional[Session]
    def drop(self, channel_id) -> None
    def invalidate_instance(self, instance_id="") -> None    # on restart/crash
    def snapshot(self) -> dict                               # diagnostics, redacted
def registry() -> Registry
def new_channel_id() -> str
def check_send(channel_id, receiver_id, state_revision) -> dict   # raises IntegrationError
```

Session state is per browser page, keyed by channel id — never one global.
An instance id change invalidates every session bound to the old one.

## `scrub.py` / `journal.py` / `process_log.py` — what may be written down

```python
# minipaint_neo/scrub.py  (not WanGP-specific: every writer in the extension uses it)
def line(text, limit=0) -> str          # PII out; the backend port kept (console)
def private(text, limit=MAX_LINE) -> str  # line(), plus the port (anything the tab renders)
def block(text, limit=MAX_LINE) -> str  # a traceback, scrubbed frame by frame
def console(text, prefix="MiniPaint:") -> None
def traceback_now(prefix="MiniPaint:") -> None
def register_root(label, path) -> None  # "<wangp>/outputs/*.mp4" instead of "<path>"
def forget_roots() -> None
def known_roots() -> list[tuple[str, str]]
# journal.py                            the tab's console: a ring buffer, and a mirror to disk
MAX_LINES = 400
def note(source, message) -> None       # scrubbed, then in-memory, then process_log
def lines() -> list[str]; def text() -> str; def clear() -> None   # clear() leaves the file
# process_log.py                        logs/wangp-log.txt, beside logs/send-log.txt
MAX_BYTES = 2_000_000
def note(source, message) -> None       # never raises; latches off after one refusal
def begin(instance_id="", detail="") -> None; def end(instance_id="", detail="") -> None
def tail(limit=40) -> list[str]; def path() -> str
def use_log_dir(directory) -> None      # test seam; nothing in the extension calls it
```

Every line the extension prints or writes goes through `scrub` first, once, at the point
it enters the process — for the child's output that is `runtime._drain`, which is why the
crash tail, the journal, the log file and the console all hold the same scrubbed text.
`scrub` never raises: its largest caller is the drain thread, and that thread dying fills
the child's pipe and freezes WanGP mid-generation. Scrubbing twice is scrubbing once.

## `ui.py` / `settings.py` / `diagnostics.py`

```python
# ui.py
TAB_LABEL = "WanGP"; TAB_ID = "wangp"
def on_ui_tabs() -> list        # [(blocks, TAB_LABEL, TAB_ID)] — one tab, always built
def create_ui() -> None         # the four roots: setup / starting / error / iframe
# settings.py
def on_ui_settings() -> None    # ONE entry, pointing at the tab's Reinitialize; no fake boolean
# minipaint_neo/settings.py (the extension's own page): the WanGP look
WANGP_LOOK = "minipaint_wangp_look"   # "Match this page's theme" (default) | "Mini Paint's own dark and light" | "WanGP's own theme"
def wangp_look() -> str                # the protocol's skin word: "host" | "bridge" | "off"; anything else stored is "host"
# ui.py: every view dict carries "look": current_look(), read by the browser half (see below)
# diagnostics.py
def report() -> str             # redacted, paste-into-a-bug text
def redact_path(path) -> str
LOG_TAIL_LINES = 40; PROCESS_TAIL_LINES = 25   # the two log tails the report carries
```

The tab is built once and never destroyed; only visibility changes.

## `javascript/minipaint_wangp.js`

`window.minipaintWanGP` with: `attach()`, `receivers({late})` (one bounded
query, returns a promise of the normalised list — or the bridge's own failure
code when it refused, never a stale-revision stand-in; `late` is called with an
answer that arrives inside the minute after the deadline), `send(receiverId,
revision, handoffId)`, `focus(receiverId)`, `state()`, `switchToWanGP()`. Every
query is journalled: asked, answered after N ms, refused with a code, or no
answer within the deadline. It validates
`event.origin === window.location.origin` **and** `event.source ===
iframe.contentWindow` on every message, checks the protocol version, the
channel id and the request id, and drops everything else silently. No `"*"`
target origin. No polling once a menu closes.

Protocol 3 adds `queue(request)`, `confirmQueue(requestId)`, `queueAndConfirm(request)`
and `capabilities()`, and `state().queue` from the handshake's `capabilities.queue`;
the shapes are in `docs/clipboard/CONTRACTS.md`.

`setupWarnings(list)` (2026-10-02) shows what the server found wrong with
WanGP's setup as a bar over the bottom of the frame, with a Dismiss that
holds for the page until the versions in the warning change. The list comes
from the interop snapshot's `wangp_warnings`: entries of
`{code, installed, wanted, source, message, fix}`, `code` one of
`GGUF_KERNELS_OUTDATED` and `GGUF_KERNELS_UNAVAILABLE` (any other is ignored),
written by `minipaint_neo/wangp/kernels.py` from the environment's
`llamacpp_gguf_cuda` `.dist-info` against WanGP's `docs/INSTALLATION.md`, and
from WanGP's own lines saying the kernels are too old or missing. Cleared at
every launch. Every word on the bar is the server's, set as text.

`generate()` (2026-10-02) is WanGP's own Generate pressed from outside its
page, for the sibling extension's assistant panel, whose ▶ button calls it on
the WanGP tab: `queueAndConfirm({start: "auto"})` and nothing else, so the
bridge writes WanGP's own generate trigger when WanGP is idle (route
`generate`, status `started`) and its add-to-queue trigger when it is busy
(route `queue`, status `queued`), and WanGP's own chain reads the live form.
It deliberately does not go through `minipaintInterop.wangp.enqueue`: a job
there is composed from WanGP's *recorded* form, or the model's defaults with
inheritance off, which is not what is on screen. Its name and its answer's
`ok`, `status`, `route` and `message` are read by that extension; renaming it
there or here breaks the button silently, and `tests/test_wangp_protocol.py`
holds that it overrides nothing.

Protocol 6 adds `flushForm({timeoutMs, settleMs, callTimeoutMs})`: ask WanGP to
commit this page's live settings form so a job composed later — on the server,
with this page shut — runs at what was on screen. It writes one hidden trigger
(`save_form_trigger`, which WanGP already wires to
`save_inputs(target="state")`) rather than reading the form back, then polls a
fingerprint of the recorded form until it moves or the budget (900 ms unless
given) runs out; with `settleMs` it looks once, that long after the press, and
stops. Resolves — never rejects — with one of `committed`, `unchanged`,
`requested` (settle mode, not moved yet), `suppressed`, `unavailable`; every
one of them is a fine outcome, because a job composes from the recorded form
when a flush cannot happen. A probe never writes, and a page carrying
`ignore_save_form` is refused rather than flushed.

`saveForSend(reason)` is the save every send path waits on — the gallery's
Generate, Clipboard's Add to Queue and `minipaintInterop.enqueue` (pass
`{flush: false}` to opt out). It resolves within 2 s with `{flush}`: `""` when
nothing reads the record (inheritance off, or a queue this page runs itself),
`unchanged` at once when the bridge reports its changes (`form_watch`) and
every one it reported is already committed, otherwise the outcome of a flush,
and `unavailable` when there is no bridge session or it did not answer in
time. The page also commits about a second after the last `FORM_CHANGED`, one
save at a time; `state().settings` says whether the record is current.

**What the WanGP page wears** (bridge 1.9.0): `theme(mode?)` sends `THEME_STATE`
`{mode, accent, skin, palette}` after READY and again whenever the tab comes back on
screen; `samplePalette()` is the page's own colours, one solid colour per slot of
`PALETTE_SLOTS` (`ink, ink-dim, page, panel, raised, line, line-soft, accent,
accent-ink`). Five are read off the page - the text, the page colour and the accent -
by painting a probe from each slot's Gradio variables in turn (`PALETTE_SOURCES`, first
choice first - a gradient or an unset variable computes to transparent and the next is
tried; a slot nothing paints is left out) and reading back what the browser computed.
The four surfaces are DERIVED (`SURFACE_STEPS`, `settlePalette`): steps of luminance
from the page colour towards the ink - a group's box 0.08 above the page, an input
0.05 above the box, a hover 0.04 and a border 0.10 above the input - because Gradio's
variables for them say what the theme's base declares and not what the page shows
(Lobe paints its blocks and inputs with rules of its own). `state().theme` is
`{mode, skin}`, and the journal line lists the colours sent. **The typeface** (bridge
1.10.0, fixed in 1.10.1): `sampleFont()` is the page's `--font` and `--font-mono` as its
stylesheet states them (Lobe writes its own stacks into both - 380 and 313 characters; a
list may be 1000; the container's `font-family` when `--font` is unset) and, for the
families they name, the `@font-face` rules that declare them, their `url()` made absolute
against the sheet: from this origin's own stylesheets through the CSSOM (into `@media`,
`@supports` and `@layer` blocks and a readable `@import`), and from a stylesheet another
origin serves - a CDN's webfont CSS, where Lobe's HarmonyOS Sans and Hack come from - by
reading it once more over CORS (`readFontSheet`: `fetch` with `mode: "cors"`, no
credentials, `force-cache`, aborted after `FONT_SHEET_TIMEOUT_MS` = 4 s; at most
`MAX_FONT_SHEETS` = 12; faces made absolute against the address the response came from,
after redirects). What was read is remembered for the page's life (`FONT_SHEETS`); a sheet
that could not be read is tried again after `FONT_SHEET_RETRY_MS` = 2 min at the next send.
The first send carries what is known at once; when a sheet brings faces the theme is sent
again (`resendTheme`), which finds it read and starts nothing. `font_faces` holds at most
`MAX_FONT_FACES` = 48 faces and `MAX_FONT_BYTES` = 48 KiB, so THEME_STATE never nears
`MAX_ENVELOPE_BYTES` (a message that size is dropped whole). It rides with any skin but
off - it is not a colour - and is validated on both sides (`valid_font_list`,
`valid_font_face`: one plain `@font-face` block, its `url()` http(s) or root-relative -
an `@` allowed, since CDN paths carry the version after one - never `data:`).
`state().theme` adds `font` (the list's first family), `faces`, `sheets` (`read`,
`pending`, `unreadable` as `host (why)`) and `refused` (a list the bundle could not carry). `skin` is the look from
Settings as the tab's view JSON carries it (`look`): `host` sends the palette and the mode
read off the page colour sampled (dark below half luminance), `bridge` sends no palette,
`off` sends none and says `mode: "light"` - the one word that leaves WanGP alone on a
bridge older than the skins, which reads `mode` and nothing else; `font`, `font_mono` and
`font_faces` ride with host and bridge (a bridge older than 1.10.0 ignores them). A look word the bundle
does not know is `host`. The journal says `theme: host, dark; ink rgb(...), ... accent-ink
rgb(...)` once per change, never once per send.

**Focus mode** (SD-Neo-ModelSwitchRefiner's assistant): the class it puts on the tab's
panel, `forge-assistant-focus-root`, is the contract. Under it the stylesheet makes the
frame the whole window (the panel's padding and border off, `#wangp_manage_root` not
displayed, the browser's recovery notice laid over the frame) and `fitFrame()` gives the
frame the panel's whole height - nothing under it and the accordion not measured, the
panel's bottom rather than the window's. Nothing is moved in the DOM and nothing is
repainted; leaving focus is the class coming off. Its side of the contract is that
extension's `docs/22-forge-assistant.md`.

**The compact page** (bridge 1.11.0): `layout()` sends `LAYOUT_STATE` `{compact}` -
`compact` true while `focusedPanel()` finds that class on the tab's panel - at READY
(`layoutSent` reset, so a page reloaded under focus is told again) and whenever the panel's
attribute observer hears the class change (`attributeFilter: ["style", "class"]`); the
same answer is never sent twice. Only to a bridge whose READY said
`capabilities.layout_compact`; otherwise the journal says once, when focus would have
asked, that the page keeps its header. Presentation like `THEME_STATE`: never answered,
never a reason a send fails. `state().layout` is `{offered, compact}`.

The heartbeat: while the WanGP tab is on screen and the page is visible, a
bridge whose READY said `capabilities.ping` is sent `PING` every 5 s and
answers `PONG` `{busy_ms, op, waiting}` from its page script, never through
Gradio. Twenty seconds of on-screen silence (sixty while the document is still
loading) shows *WanGP stopped responding* with Reload view and Dismiss over the
frame and reloads the frame's `src` ten seconds later unless dismissed — at
most once per two minutes and three times per page. `state().heartbeat` is the
whole of it. `HELLO` carries `watch_form: true`; a bridge that honours it says
`capabilities.form_watch` and sends `FORM_CHANGED` `{touches}` for trusted
events only.

## `wan2gp_bridge/wan2gp-minipaint-bridge/` — the WanGP-side plugin

Standalone; imports nothing of ours. Ships its own `protocol.py` copy whose
SHARED block is byte-identical to `minipaint_neo/wangp/protocol.py`'s.
`compatibility.py` is the only file allowed to know a WanGP component id, and
every id it wants is version-gated and reported in the handshake so a build
that lacks one fails closed with `BRIDGE_COMPONENT_INCOMPATIBLE`.

**The theme hijack** (`theme.css`, injected by the document script as
`<style id="minipaint-bridge-theme">`, bridge 1.9.0). WanGP's plugin API adds a
script, never a theme or a stylesheet, so the look is not a sixth entry in WanGP's
theme dropdown: it is a stylesheet that outranks whichever entry that dropdown
picked. Keyed on one attribute the script sets on `<html>`, `data-minipaint-theme`,
whose value is the mode (`dark` | `light`); no attribute is WanGP untouched, and an
unmanaged WanGP never has one. Nine `--mp-*` slots: the bridge's own defaults per
mode, or - for the `host` skin - the parent page's colours written as inline
properties on `<html>` (`normalize_theme` on both sides keeps only known slots
holding one solid colour, `COLOUR_RE`: a hex triplet or one colour function, never
`url()`, a gradient or `var()`). Everything else in the file reads a slot: Gradio's
theme variables on `.gradio-container` and `body`, and WanGP's eleven `--studio-*`
variables - declared `!important` on `[id^="component-"]`, because `ui_studio.py`
rewrites its `.wangp-studio-*` classes to `#component-N` ids when it builds the page's
CSS and only importance gets past an id - plus the fixed colours of `ui_styles.css`
that no variable reaches (the model selector's slab, the title rules, the editor's
buttons, the queue table, the progress bar). Groups: every `.form` and `.gr-group` is a
box of the panel colour (`!important`, over the transparency the studio look gives
them), a selected tab is the accent on the raised surface, and in dark mode a ticked box
is the accent pulled towards the page so a white check shows on a white accent. The
typeface (1.10.0): the script marks `<html>` `data-minipaint-font` and puts `--mp-font`
and `--mp-font-mono` on it inline, with the faces in `<style id="minipaint-bridge-fonts">`;
under the mark the container reads them as Gradio's `--font` and `--font-mono` and sets
itself in the first, and WanGP's one hand-written stack (the queue table) reads it too.
Without the mark WanGP keeps its own - which, for its studio themes, is `--font: 'Verdana'`
with no generic family behind it, a serif on any browser that will not use Verdana. The
handshake says `capabilities.theme_font`. The compact page (1.11.0): on `LAYOUT_STATE`
the script's `layout()` takes `compact` only for a real `true` (`protocol.normalize_layout`)
and finds the parts from WanGP's `#wangp-gallery-tabs` (`CHROME_ANCHOR_ID`, an id WanGP
gives only the Media Generator form's gallery tabs, for bridges): the tab panel around it,
the main `.tabs` holding that panel and its `.tab-wrapper`, every element before the main
tabs (the title), and every block of the panel's column before the one holding the anchor
(the model row, the hidden search panel, the header group). Each is hidden with an inline
`display: none !important` - nothing in a stylesheet beats it - and marked
`data-minipaint-chrome`; `<html>` gets `data-minipaint-layout="compact"`. Whole puts back
the inline display each had and removes both marks. A part holding the bridge's own column
is never taken. No anchor yet: up to `TRIGGER_ATTEMPTS` looks `TRIGGER_RETRY_MS` apart,
then the page stays whole. `window.__minipaintBridge.layout(payload)` returns
`{compact, hidden}`. Measured on Gradio 5.29's own markup of WanGP's structure
(`tests/wangp_page_gradio_5_29.html`) by `tests/browser_intercept.py`. The handshake says
`capabilities.theme_palette`; a parent that never hears it sends `mode` alone, and
the script's first paint is the bridge's own dark until the parent speaks.

A receiver descriptor carries `enabled` (offered: switched on now, *or* allowed
by this page's model definition and switchable), `selected` (switched on now)
and `switch` (`location`, `end_images`, `reference_images`, or empty — what the
send will set). Allowances come from `get_model_def(get_state_model_type(state))`
— the same two globals Wan2GP's own handlers use, asked for in `__init__`
because Wan2GP injects globals once, right after constructing the plugin. A
send to an allowed-but-unselected receiver updates the selector, the letter
string generation reads and the row's visibility in the same Gradio event that
places the image; the acknowledgement names the switch under `switched` and
the updated components under `chained`. Unknown is never allowed: a build that
hands over no definition, or none of the selector controls, offers exactly what
it offered before.

The frame timer (`bridge_js.head_script`, placed by `page_head`) wraps
`requestAnimationFrame` in the page head, before Gradio's modules load. Gradio
schedules every event trigger inside an animation frame and gates it on its
component-update flush, which Gradio's core schedules through a reference it
captured when its module was evaluated; the iframe used to be a document the
browser was not rendering while the Forge tab holding it was not on screen
(since 2026-09-25 the tab's panel is parked rather than hidden - see
`PARKED_PANEL_2026-09-25.txt` - so this is now the fallback for a page whose
stylesheet did not apply), and a browser that gives such a document no
frames leaves that flush - and every bridge request behind it - waiting
until the WanGP tab is opened again. So every frame requested in the
page is also given a timer, short while a bridge request is in flight
(`FRAME_FALLBACK_MS`) and longer otherwise (`IDLE_FRAME_FALLBACK_MS`), and the
first to fire runs the callback; a rendered page's own frame always wins.
`page_head.install` wraps Gradio's template loader (`gradio.routes.templates`)
for `frontend/index.html` and `frontend/share.html` only, inserting the script
before the `<script type="module">` tag - the way WanGP's own focus patch goes
in - once, and leaves every other template alone. The page script
(`bridge_js.document_script`) finds the head copy through
`window.__minipaintFrames` and installs the wrapper itself only when the copy
is missing; its failure details then say `frame timer installed late`, and the
plugin's log says why the head copy could not be placed. It also reads the
acknowledgement box while a request is in flight (`ACK_POLL_MS`) as a second
route for the chained `.then(js=…)` delivery, and its failure answers carry a
`detail`. A `WANGP_RECEIVE_IMAGE` on the bound channel that the page cannot act
on - a handoff id that is not 32 lowercase hex characters, a receiver id the
protocol does not declare, no state revision - is answered at once with a
`WANGP_RECEIVE_RESULT` carrying `ok: false` and the code
(`HANDOFF_INVALID_ID`, `UNKNOWN_RECEIVER`, `STALE_RECEIVER_STATE`) rather than
dropped; a message on another channel is still dropped without a reply, and
only the console says so.

Every step of a send is one line in the journal (`logs/wangp-log.txt`), so a
send that fails leaves a trace whichever half stopped: the Forge side writes
under `send` (`prepared WxH`, `nothing to send`, `the browser reports: …`),
the page under `browser` (`send: … chosen from the menu`, `deliver: …`,
`send: refused before asking - CODE`, `WANGP_RECEIVE_IMAGE: asked (id)`,
`no acknowledgement within N ms`) through `minipaintWanGP.note`, and the plugin
under `wangp`. `Canvas -> WanGP …` in `logs/send-log.txt` is written twice per
send: `prepared …` when the file is written, then how it ended.

The delivery has two routes. Gradio's chained browser step on the send event
(`.then(js=…)` with the instruction and payload boxes as inputs) is the first;
from the click on, the Canvas also reads those two boxes itself a few times a
second (`WANGP_WATCH_MS`) and delivers the moment they hold this send's
instruction and a fresh 32-hex file id, for at most `WANGP_WATCH_LIMIT_MS`.
Whichever route runs first delivers and the other finds nothing armed; a
chained step that arrives with a stale instruction, or with the file id of the
previous send, leaves the armed send to the watcher. When the boxes are still
empty `WANGP_FETCH_AFTER_MS` after the click, the watcher presses the hidden
fetch button (`minipaint_canvas_wangp_fetch`), whose event (`wangp_fetch`,
inputs `[state]`, outputs `[switch_box, payload_box]`) hands the prepared send
over again from the record kept when the file was written - and nothing when
none is pending or the file has been released; it presses again every
`WANGP_FETCH_EVERY_MS`. A send whose answer never reaches the boxes ends with
`deliver: gave up after N s` in the journal and a notice on the status line.

Bridge 1.2.0 (protocol 3) added the `queue` and `confirm` operations and `admission.py`:
a queue request overlays the prompt and the image inputs on the live form *as
replacements*, writes WanGP's `client_id` and a trigger so WanGP's own chain runs,
keeps a per-session `PendingAdmission`, confirms through `get_gen_info(state)` (a
task whose `params.client_id` is the request id), refuses only on a correlated
`queue_errors` entry, expires to `ADMISSION_UNCONFIRMED`, and puts every override
back where the page still holds what the bridge wrote. Bridge 1.3.0 (protocol 4)
adds the start decision: the request carries `start: auto | never`; the bridge asks
Wan2GP's `is_generation_in_progress()` (requested as a global, read live) and writes
`generate_trigger` only for `auto` on a definite "idle" with the trigger resolved,
`add_to_queue_trigger` otherwise (`start: "unknown"` when the flag or the trigger is
missing); `confirm` answers `started` when the request's task is at the head of a
running loop (`gen["in_progress"]`), `queued` otherwise, with `queue_depth`; the
handshake carries `capabilities.start` and a live `generation_running`. Bridge 1.4.0
(protocol 5) adds the `track` operation - where the tasks this page admitted are in
WanGP's queue now (`waiting` with a position, `generating`, `finished` for a request the
page once saw queued whose task is gone, `unknown` for one it never admitted; the ledger
remembers admitted ids for a day, past its admission records), read from
`get_gen_info(state)` and never written - the `model_type` a request may insist on
(refused with `MODEL_CHANGED`, untouched, when the page's model differs), the model
block's `architecture` (`get_base_model_type`), `capabilities.track`, and a prompt
ceiling of 12000 characters. The full contract is in `docs/clipboard/CONTRACTS.md`.

## `control.py` — Forge's half of the control plane (protocol 6)

```python
LOOPBACK = "127.0.0.1"; CONNECT_TIMEOUT = 5.0; CALL_TIMEOUT = 30.0; COMPOSE_TIMEOUT = 60.0
def use_transport(fn | None); def reset_for_tests()          # seam: answer control calls without a socket
def hello(timeout=CALL_TIMEOUT) -> dict                      # what the child is, and whether it can take a job
def compose(model_type="", session_hash="", timeout=COMPOSE_TIMEOUT) -> dict
def submit(execution_id, settings, prompt=None, media=None, model_type="", priority=False) -> record
def status(execution_ids) -> {id: record}; def cancel(execution_id) -> record; def forget(execution_ids) -> int
def available() -> (bool, code)                              # never raises; for a status line and the executor's gate
# bridge 1.12.0, the card lease (only ``turns.drive`` calls these; see below)
HOLD_TIMEOUT = 10.0; FLUSH_TIMEOUT = 90.0
def hold(lease, ttl_s, label="") -> answer                   # ask, or renew; normalize_hold_answer
def resume(lease) -> answer                                  # idempotent: a lease not held is resumed False
def flush(lease, level) -> answer                            # "soft" | "hard"; a refusal raises with the bridge's code
def can_hold(answer=None) -> bool | None                     # the hello's capabilities.hold; None when nothing recent said
```

The destination comes off the runtime object and nowhere else — the host and scheme are
literals here, the port is the one this process kept before it launched that child, and
nothing a caller passes reaches the URL. Both the port and the secret come off that object
*together*: a port from one run and a secret from another would produce a 401 that looked
like a configuration problem rather than the restart it is.

`urllib`, not the proxy's httpx client: this is not an async path and must never borrow a
client that belongs to another event loop. Every caller is the executor thread; no route
calls any of this.

READY is **not** an admission fact. It says a process is alive and answered an HTTP request.
Whether the bridge is on the page, the generation service resolved and a settings base can be
read are the child's to answer, and `hello` is where it does.

## `presence.py` — what another extension in the process may ask

```python
PRESENCE_VERSION = 1
KEYS = ("version", "available", "configured", "gpu_uuid", "state", "running", "instance_id", "generating", "source")
def report() -> dict        # never raises; exactly KEYS, nothing else
def forget() -> None        # drop the cached setup; tests only
```

Written for `RJSprod/SD-Neo-ModelSwitchRefiner`, which reads it (by name, and only
once this package is already imported by the host) to keep its llama-server out of
WanGP's VRAM and off WanGP's cores: on the card `gpu_uuid` names it sizes the server to
what WanGP has not needed, stops it when WanGP grows into the reserve it keeps, and caps
the server's processor threads while `running` is true. Its README's *WanGP on the other
card* section is the other half of this contract.

The rules, each of which its suite holds:

* **The keys are closed.** A report carries `KEYS` and nothing else, and no key ever
  names a port, a pid, a secret or a path. The card is its UUID — the identity both
  extensions record and the one that survives every renumbering; the reader resolves it
  against its own picture of the machine.
* **`generating` is three-valued.** `True` and `False` are what the bridge last said,
  through `control.last_hello()` and only while that answer is within `HELLO_TTL`; `None`
  is "nobody has said" — a WanGP without the bridge, or one idle long enough that nothing
  has asked it. A reader that takes `None` for `False` is wrong on the machine this was
  written for.
* **`configured` and `gpu_uuid` are the saved setup**, re-read only when the primary
  config file's size or modification time changed, so a report every two seconds costs no
  JSON parse and a setup that just finished is seen at once. An unreadable or too-new file
  reads as not configured, never as an error.
* **`state`, `running` and `instance_id` are `runtime.snapshot()`**, which is what the tab
  draws: `running` is READY and nothing else.
* **Each part is read on its own.** A runtime that cannot be read leaves the setup in the
  report and the rest truthful; `available` is the one key that is always `True`, because
  a call that reached this function reached the integration.

Suite: `tests/test_wangp_presence.py`.

## `turns.py` — the card lease: another extension borrowing WanGP's card

```python
TURNS_VERSION = 1
LEASE_KEYS = ("version", "lease", "owner", "phase", "reason", "card_uuid", "wanted", "bridge_hold", "expires_in_s", "wangp")
WANGP_KEYS = ("running", "task_running", "queue_length", "jobs_waiting", "vram_free_bytes", "vram_total_bytes")
REPORT_KEYS = ("version", "available", "configured", "card_uuid", "running", "lease", "owner", "phase", "wanted",
               "can_hold", "bridge_hold", "wangp")
PENDING, HOLDING, HELD, RELEASED, EXPIRED, REFUSED   # the phases
LEASE_TTL_SECONDS = 20.0      # unrenewed this long, a lease expires
BRIDGE_TTL_SECONDS = 45.0     # the timer every hold hands the bridge

# for the owner - never raise, return at once, never touch the control plane, never from an ASGI handler
def request(owner, *, purpose="", need_bytes=0) -> record
def state(lease) -> record                  # and renews it
def flush(lease, level) -> record           # "soft" | "hard", only while held
def release(lease, *, reason="") -> record
def report() -> dict                        # exactly REPORT_KEYS, with or without a lease
# for the executor
def gate_closed() -> bool; def gate_owner() -> str | None; def needs_driver() -> bool
def driver_interval() -> float; def drive() -> None           # the one place the lease calls the control plane
```

Written for `RJSprod/SD-Neo-ModelSwitchRefiner`, whose Voice Box can put a text-to-speech
model of eighteen to twenty gigabytes on the card WanGP runs on. Its user's rules are this
module's: the speech request goes NEXT on that card; the WanGP job that is running is never
cut off; the Clipboard's next job waits until the card is given back; the guest may stay warm
while WanGP is idle and gives the card back when WanGP work wants it. It is read like
`presence` - by name, once the host has imported this package - and its side of the contract
is section 6 of that repository's `docs/23-voice-box.md`.

**The phases.** *pending*: a WanGP task of Mini Paint's is still running on the card (the hold
has already been asked for, so WanGP stops after it rather than taking its next task).
*holding*: the bridge has been asked to hold WanGP and has not said the card is clear -
somebody else's task is finishing, WanGP is starting or stopping, another of WanGP's own GPU
processes has the card, the bridge did not answer, or a flush is being done; `reason` says
which. *held*: nothing is running on WanGP's card for WanGP and nothing will start - the
bridge says so, or WanGP is not running at all (and no Clipboard job will start it). The owner
may use the card. *released* / *expired*: the gate is open, and the executor tells the bridge
to resume on its next pass (`bridge_hold` then reads `none`). *refused*: WanGP is not set up,
another Forge on this machine runs it, or it is running and cannot be held - a bridge older
than 1.12.0, or a WanGP build without what a hold needs; `reason` says which.

The rules, each held by a check:

* **The gate closes the moment `request` returns a live lease.** No Clipboard job advances
  through `ENSURING_WANGP`, `COMPOSING`, `WAITING_FOR_CARD` or `SUBMITTING_WANGP`, so none
  starts WanGP and none is handed to it (the executor's `LENT_GATED`), and `outbox.claim`
  hands no page a job (`WAIT_LENT_MS`). The job WanGP is running is followed to its end and
  never aborted. `WAITING_FOR_CARD`'s 60-second "submit anyway" does not reach a held job,
  and a job given back starts that courtesy clock again.
* **One lease at a time.** A second request by the same owner returns (and renews) the live
  lease; anybody else is refused and told whose the card is.
* **Renew or lose it.** `state` renews; a lease not renewed for twenty seconds expires. The
  owner renews through a render too. The bridge's own timer (`BRIDGE_TTL_SECONDS`, renewed by
  every executor pass) covers a Forge that died.
* **The functions only record and read.** The executor - the one thread allowed to use the
  control plane - drives the lease at the top of every step, and once a second while a lease
  is live and there is no job: hello (can this bridge hold at all), hold (asked, renewed, where
  it stands), flush when one is owed and WanGP is held, resume once the lease has ended. A
  lease change nudges it, cutting a stage's pause short.
* **`flush` is asynchronous.** It returns at once with the lease `holding` ("WanGP's weights
  are being moved off its card"); `held` again means the flush is done - `reason` then says
  what the card has free (`Soft flush done in 1.2 s; 29.8 GB of 31.8 GB is free on WanGP's
  card.`) or why it was refused. An owner measures after `held`, not after `flush` returns.
  With WanGP not running there is nothing to flush and the lease says so at once.
* **Held is never taken back by refusal.** A lease the owner was told was `held` (say with
  WanGP stopped) is not refused if WanGP then comes up unholdable: it says `holding` with
  `bridge_hold: unsupported`, and the gate stays closed until it is released. Phases are not
  monotonic in general - a flush, WanGP started from its tab, a restart (a WanGP run that was
  never asked) and a task that slips past the hold all take `held` back to `holding`.
* **`wanted`** is a Clipboard job waiting for the card (`outbox.card_demand`: a server job in
  `CARD_WAITING`, a page-driven one pending or being sent - not one whose prompt is still being
  written) or WanGP work waiting on the hold (the bridge's `waiter`).
* **`bridge_hold`** is the bridge's own word: `none` (nothing asked, or let go), `holding`,
  `held`, or `unsupported` - WanGP running with a bridge or a build that cannot hold, or,
  with WanGP stopped, an installed bridge older than 1.12.0 (so an owner knows not to stay
  warm on a card WanGP could take back unasked).
* **`wangp`** is the freshest bridge answer: WanGP running, a task on the card, WanGP's queue
  length, the Clipboard jobs waiting, and the card's free and total bytes as the driver
  reports them; any of them `None` when unknown. With WanGP stopped `task_running` is `False`.

The bridge's half is the next section. Suites: `tests/test_wangp_turns.py` (the lease, both
gates, the control client) and `tests/test_wangp_hold.py` (the bridge).

## Bridge 1.12.0: `hold`, `resume`, `flush`

Three operations on the authenticated control plane, additive (`CONTROL_VERSION` stays 1) and
advertised in hello as `capabilities.hold`, so an older bridge is recognised rather than
refused - it answers everything else as before. `hello` gains `worker`, `task_running`,
`active_client_id` (only when it is one of Forge's execution ids), `queue_length`, `hold`,
`waiter`, `vram_free` and `vram_total`; every hold answer carries the same seven activity
fields plus `hold`, `claim`, `busy_with`, `expires_in_s` and, for a flush, `flushed` and
`parts` (`protocol.normalize_hold_answer`). The state machine is `hold.py`; every WanGP
internal it touches is a method of `compatibility.py`, read live out of `sys.modules`, never
imported and checked for before use.

* `hold {lease, ttl_s, label}` sets WanGP's between-task pause flag (`queue_paused_for_edit`,
  which WanGP's worker checks before taking each task) and re-asserts it every 0.1 s (WanGP's
  own edit feature clears it). While no generation worker exists it takes the idle claim on
  WanGP's GPU lock (`try_acquire_GPU_ressources`, named `label` in WanGP's own "waiting for
  <name> to release GPU resources"), so a run started from the WanGP tab waits; it never asks
  for the claim while a worker exists, so it can never suspend a run. A claim refused by the
  run flag a crash leaves set falls back to the flag alone; one refused by another of WanGP's
  GPU processes keeps the hold `holding`, naming it. Answers `holding` while a task is running
  (the task marker `api_active_queue_task` where WanGP has it; the worker's own "paused"
  status and its loop's state where it does not) and `held` otherwise. A hold for a new lease
  takes the old one over without letting go. Not renewed within `ttl_s` (5 to 300 s), it lets
  go by itself.
* `resume {lease}` undoes it in reverse order: the claim, then the flag - and the flag only if
  the hold set it (an edit that had it set before keeps it). A lease not held is `resumed:
  false`, changing nothing.
* `flush {lease, level}`: `soft` moves the video model and the prompt enhancer off the card
  (`unload_all`), releases every GPU resident through its own callback, unloads the extensions'
  offload objects and empties the allocator's cache - weights stay in RAM. `hard` then runs
  `release_model()` inside `model_unload_guard()`, so the next task reloads from disk.
  Refused while a task runs (`HOLD_TASK_RUNNING`); hard also while a run is paused between
  tasks (as WanGP's own Unload is), where WanGP preloads its model at start and would wait for
  ever for a released one (`FLUSH_HARD_REFUSED`), and on a build without the guard
  (`HOLD_UNSUPPORTED`). A flush renews the hold, and the timer cannot run out under it.

The one window: WanGP's edit clearing the flag between two re-assertions (at most 0.1 s) can
let its worker - which looks every half second - take the next task. The hold then says
`holding` again until that task ends, and the lease reports it; WanGP's plugin API has no
hook before a task that could close it.

## Tests

`tests/test_wangp_*.py`, in the existing style: a `run()` returning
`harness.Results`, no pytest, nothing that needs WanGP, Forge or a network.
Add the new suites to `tests/run.py`. The queue's own suites are `tests/test_wangp_queue.py`,
`tests/test_wangp_start.py`, `tests/test_interop.py`, `tests/test_clipboard_store.py`,
`tests/test_clipboard_outbox.py`, `tests/test_clipboard_enhance.py`, `tests/test_clipboard_ui.py` and `tests/test_queue_e2e.py`;
the lock, the GPU report and the emergency restart are in `tests/test_wangp_runtime.py`; the card
lease is `tests/test_wangp_turns.py` (Forge's half) and `tests/test_wangp_hold.py` (the bridge's).
