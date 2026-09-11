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
# diagnostics.py
def report() -> str             # redacted, paste-into-a-bug text
def redact_path(path) -> str
LOG_TAIL_LINES = 40; PROCESS_TAIL_LINES = 25   # the two log tails the report carries
```

The tab is built once and never destroyed; only visibility changes.

## `javascript/minipaint_wangp.js`

`window.minipaintWanGP` with: `attach()`, `receivers()` (one bounded query,
returns a promise of the normalised list), `send(receiverId, revision,
handoffId)`, `focus(receiverId)`, `state()`, `switchToWanGP()`. It validates
`event.origin === window.location.origin` **and** `event.source ===
iframe.contentWindow` on every message, checks the protocol version, the
channel id and the request id, and drops everything else silently. No `"*"`
target origin. No polling once a menu closes.

## `wan2gp_bridge/wan2gp-minipaint-bridge/` — the WanGP-side plugin

Standalone; imports nothing of ours. Ships its own `protocol.py` copy whose
SHARED block is byte-identical to `minipaint_neo/wangp/protocol.py`'s.
`compatibility.py` is the only file allowed to know a WanGP component id, and
every id it wants is version-gated and reported in the handshake so a build
that lacks one fails closed with `BRIDGE_COMPONENT_INCOMPATIBLE`.

## Tests

`tests/test_wangp_*.py`, in the existing style: a `run()` returning
`harness.Results`, no pytest, nothing that needs WanGP, Forge or a network.
Add the new suites to `tests/run.py`.
