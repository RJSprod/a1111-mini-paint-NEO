# The WanGP integration — an operator's guide

This describes what the extension actually does when the **WanGP** tab is set up and
used: what it starts, what it writes, what it refuses to write, and what you can check
for yourself. It is written against the code in `minipaint_neo/wangp/`,
`wan2gp_bridge/`, `javascript/minipaint_wangp.js` and the registration in
`scripts/mini_paint.py`. The design document it implements is
`docs/WAN2GP_TAB_DESIGN_INTENT_REVISED_2026-09-06.txt`; where the two differ, this file
follows the code and says so.


## How the bridge plugin is installed

Two things have to be true before WanGP will run our plugin, and only the first
is what "installing a plugin" usually means:

1. **The folder is there.** `plugins/wan2gp-minipaint-bridge/` inside your WanGP
   installation. WanGP finds plugins by scanning that directory for folders
   containing a `plugin.py` or an `__init__.py`, so nothing has to be registered
   for it to be *found*.
2. **It is listed in `enabled_plugins`.** WanGP loads its built-in
   `SYSTEM_PLUGINS` plus exactly the folder names listed under `enabled_plugins`
   in `wgp_config.json`, in the WanGP root. The key defaults to an empty list.
   **A plugin that is only copied is discovered and then skipped** — which looks
   from the outside like a plugin that is installed and broken. Nothing inside
   our own `plugin_info.json` has any say in this.

**Install or update it** in step 4 does both, so there is nothing to edit by
hand. It appends our folder name to that list and writes the file back
atomically; every other plugin stays enabled and every other setting is
preserved. It is the same switch WanGP's own Plugins tab writes.

WanGP builds its plugin list once, at startup, so enabling it changes nothing
until the process restarts. Because this extension owns that process, the
installer stops it for you and the next **Run the checks** starts it again with
the plugin loaded.

## What it is

A second top-level tab, **WanGP**, that shows the real WanGP application, plus WanGP
destinations in the Canvas's *Send to* menu.

* **WanGP runs in its own process.** The extension starts it from your existing WanGP
  folder using your existing Python environment, with `cwd` set to the WanGP root, so
  WanGP keeps its own models, presets, LoRAs and outputs exactly where they are. Forge's
  interpreter never imports WanGP.
* **That process binds `127.0.0.1` and a port the kernel picks.** The browser never
  learns the port and never contacts it. The tab's iframe is `src="/wan2gp/"` — a path on
  the Forge origin — and a reverse proxy inside Forge streams that to the child.
* **One GPU, by UUID.** The child is launched with `CUDA_VISIBLE_DEVICES` set to the
  physical UUID you chose. If that card is not in the machine, nothing starts; no other
  GPU is used instead.
* **Forge's torch settings stay in Forge.** The child inherits Forge's environment so
  that PATH and drivers are what a terminal would give it, minus Forge's
  interpreter variables and the allocator variables `--cuda-malloc` and
  `--expandable-segments` export (`PYTORCH_CUDA_ALLOC_CONF`, `PYTORCH_ALLOC_CONF`). WanGP
  launched by hand never sees those, and under `cudaMallocAsync` its prompt enhancer's
  CUDA-graph capture aborts the whole process.
* **Startup is lazy.** Nothing is launched when Forge boots. Opening the tab (or opening
  *Send to*) asks for WanGP and returns immediately; the tab shows a "starting" card with
  a *Check again* button rather than blocking a Gradio event for a cold model load.
* **The Send destinations are WanGP's own answer.** *Send to* always lists the three
  places a picture can go — Start Frame, End Frame, Reference — and when it opens, one
  bounded question goes to the live WanGP page in your browser: *which of these does the
  model you have loaded take?* A scenario the model takes is a real menu action, whether
  or not WanGP's own selector (the Location radio, the End Image(s) checkbox, the
  reference dropdown) has been set to it: the send sets the selector as part of placing
  the image, the same way a click would, and never touches the model. A scenario the
  model does not take is greyed out and says so. When there is no live WanGP page in this
  Forge tab, all three are greyed and the line above them says to open the tab. There is
  no cached list, no model-name table, and no DOM scraping: what the model takes is read
  from WanGP's own model definition, inside WanGP, per page.
* **A send is verified before the tab switches.** The picture is flattened to a PNG on the
  server, handed to the browser as an opaque id, applied by a WanGP-side plugin through a
  real Gradio event, and acknowledged. Only a verified acknowledgement switches you to the
  WanGP tab. A send is never followed by an automatic Generate.

  Both halves check, and they check different things. Inside WanGP the plugin decodes the
  value it just applied and compares it, pixel for pixel, with the file it read - that is
  the half that can see what Generate will actually use. Back in Forge the answer is then
  held to the manifest of the file that was written and to the input that was asked for:
  an acknowledgement claiming another size, another input, or a file digest that never
  left here is refused even though it says `ok`. An answer carrying no evidence at all is
  refused too - `ok` on its own only proves that a message came back.
* **None of it can break Mini Paint.** `scripts/mini_paint.py` wraps the whole
  registration; if any part of the WanGP package fails to import, one line is printed and
  the Mini Paint tab loads exactly as before.

## Before you set it up

* A WanGP installation you already use — a folder containing `wgp.py` and at least one of
  its project packages (`shared/`, `wan/`, `ltx_video/`, `hyvideo/`, `preprocessing/`,
  `models/`).
* The Python environment that runs it: a Conda environment prefix, or any folder with
  `python.exe` / `bin/python` in it.
* An NVIDIA GPU. Cards are enumerated through NVML when `pynvml` happens to be importable
  and through `nvidia-smi` otherwise; if neither answers, the list is empty and setup
  cannot finish.
* Nothing to install. Gradio, FastAPI, Pillow and httpx already come with Forge.

## The five setup steps

Open the **WanGP** tab and press **Start Setup**. The wizard is five numbered steps and a
checklist; nothing is written until every mandatory row of that checklist is green.

**1. The WanGP installation.** Type the folder that contains `wgp.py` and press *Check
this folder*. It must exist, be readable, contain `wgp.py`, contain one of the project
directories above, and have a resolvable `plugins/` destination. Your install is not
moved, copied or modified by this step.

**2. The Python environment that runs it.** *Find environments* looks for Conda
installations and environments (`CONDA_PREFIX`, `CONDA_EXE`, the usual roots,
`environments.txt`, and `conda env list --json` when a `conda` binary is on the path) and
lists what it found; you can also type an environment prefix by hand. *Try it against this
WanGP* actually runs a probe in that environment — a short script that reads `wgp.py` and
*parses* it, never imports it, so nothing pulls torch or a CUDA context into anything —
and records which of two launch strategies worked:
`direct_python` (`<prefix>/python -u wgp.py …`) or `conda_run`
(`conda run --no-capture-output -p <prefix> python -u wgp.py …`). The strategy is
persisted, so a Conda environment that genuinely needs its activation scripts is not
launched the way that never worked.

**3. The GPU WanGP may use.** *List the GPUs* shows each card as
`NVIDIA GeForce RTX 5090 - 32 GB`. What is stored is the UUID, not the index, so a driver
reshuffle or a reboot cannot repoint it at a different card.

**4. The MiniPaint bridge plugin.** *Check the bridge plugin* reads
`plugin_info.json` from `<WanGP root>/plugins/wan2gp-minipaint-bridge/` and compares its
version to the one this copy of the extension ships. *Install or update it* copies the
extension's own `wan2gp_bridge/wan2gp-minipaint-bridge/` folder there and nothing else
— see *Updating the bridge plugin* below for the rules it follows and the restart it
needs.

**5. Security and launch checks.** *Run the checks* starts WanGP if it is not running,
loads the proxied page beside the wizard, and renders thirteen pass/fail rows:

| row | what proves it |
| --- | --- |
| WanGP is launched bound to 127.0.0.1 only | `--server-name 127.0.0.1` is in the argv that would actually be run |
| `--listen` is absent from the command line | read off that same argv |
| `--share` is absent from the command line | read off that same argv |
| nothing binds 0.0.0.0 | no argument anywhere in the argv contains it |
| the selected GPU is present and is the only one WanGP will see | the UUID matched a card on this machine |
| a loopback port was assigned to this run | the runtime reports a bound listener |
| WanGP serves itself under /wan2gp | the proxy's root-path probe |
| the MiniPaint bridge plugin is installed and switched on | step 4's answer, with no failure code |
| the WanGP page loads through the proxy | a base-page fetch through `/wan2gp/` |
| a WanGP asset loads through the proxy | one asset URL found in that page, fetched |
| the bridge answered through the proxied iframe | a round trip reported by the iframe below the wizard |
| the browser only ever talks to this Forge origin | the iframe `src` is a path, not a URL |
| `/wan2gp/` is covered by this Forge's sign-in | see below |

The last row is **only mandatory when this Forge has authentication of its own**. When it
does, coverage is only accepted as proven if the authentication runs as ASGI middleware,
which by construction wraps every route including a dynamically added one. Per-route
authentication — a FastAPI dependency, Gradio's own login check — is reported as
`unknown` rather than as covered, and the row stays red; that is deliberate, and the
manual test for it is in `docs/wangp/PHASE0.md`.

That verdict is not advice, it is the gate. `/wan2gp/` answers `AUTH_BOUNDARY_FAILED` —
HTTP and WebSocket alike — until coverage is proven, whatever else is running: WanGP has
no sign-in of its own, so a proxy that forwarded on an unproven boundary would hand the
whole of it to anyone who could reach Forge's port. Refusing at the point of service, and
not at startup, is what also covers the two awkward cases: a child the setup wizard's own
checks started, and an installation that was set up before authentication was switched on.

So on a Forge that has a sign-in, that row is the one thing you have to answer yourself,
and step 5 has a checkbox for it: **"I signed out and checked that /wan2gp/ asks me to
sign in"**. Check it first, honestly — sign out, open `/wan2gp/` in a private window, and
confirm it refuses you the way the rest of Forge does. The answer is saved with the rest
of the setup, so it survives a restart, and it is what both the checklist row and the
proxy read.

There is an environment variable that says the same thing, for a deployment that would
rather not carry the answer in its config file:

```
MINIPAINT_WANGP_ALLOW_UNPROVEN_AUTH=1
```

Neither is a claim this code can make for you. Ticking the box without looking gets you
exactly the exposure the row exists to prevent, and nothing later will catch it.

Anything not observed is a failure, not an omission: the checklist starts entirely red and
a row goes green only when something proved it. Rows can need two presses — the first
asks for WanGP, the second reads the result once it is serving. Nothing polls in between.

**Finish setup** is the only path in the whole extension that writes `initialized: true`,
and it re-judges the rows itself rather than trusting the button's enabled state. It writes
a pending file first and promotes it atomically, so a setup that half-succeeds leaves the
previous one intact. **Restore the previous working integration** puts the last known-good
setup back and then revalidates the root, the environment and the GPU before calling it
ready.

## Where things live

| what | where |
| --- | --- |
| the setup | `<Forge data_path>/a1111-mini-paint-NEO/wan2gp.json` |
| the previous setup | `…/wan2gp.backup.json` |
| a setup being validated | `…/wan2gp.pending.json` (short-lived) |
| prepared images on their way to WanGP | `…/runtime/handoff/<32 hex>.png`, mode `0700` where the platform has it |
| the bridge plugin, as installed | `<WanGP root>/plugins/wan2gp-minipaint-bridge/` |
| the bridge plugin, as shipped | `wan2gp_bridge/wan2gp-minipaint-bridge/` in this repository |
| transfer log lines | `extensions/a1111-mini-paint-NEO/logs/send-log.txt`, shared with Mini Paint |
| what the WanGP process said | `extensions/a1111-mini-paint-NEO/logs/wangp-log.txt`, rotating to `wangp-log.previous.txt` at 2 MB |

The data root is asked for in the order `modules.paths_internal.data_path`,
`modules.paths.data_path`, `modules.shared.cmd_opts.data_dir`. If none answers, the
settings fall back to `<extension folder>/data/` and a line is printed at startup saying
so and warning that a reinstall takes them away — the fallback is never silent.

Handoff files are swept at Forge startup: anything under the handoff root older than six
hours goes, except files this process still holds a manifest for (an in-flight send owns
its file, however slow the generation). Nothing outside that root is ever touched, and
symlinks are not followed out of it.

## What is deliberately not stored

None of the following is ever written to `wan2gp.json`, to the backup, to the send log, or
to any other file:

* the child's **pid** and its **backend port**
* the **runtime instance id** minted per launch
* the **server integration secret** passed to the child in its environment
* the **browser channel id** and the **bridge session id**
* the Gradio **session hash**
* **handoff ids** and handoff **paths**
* the last **receiver revision** and any **receiver cache**

`config.py` enforces this twice — a named `NEVER_PERSISTED` set is stripped from every
section, and then only the keys the schema declares survive — so widening a whitelist by
accident cannot quietly start writing one. `bridge.py` does the same for the send log:
`LOGGED_FIELDS` is a whitelist and `FORBIDDEN_FIELDS` is checked as well. The diagnostics
report reports the pid as `tracked`/`none` and the port as `bound to loopback`/`not
bound`, never as numbers, because that report is rendered inside the tab and the browser
must not learn the port.

## Nothing written down identifies the user

Every line this integration writes — to the WebUI console, to the tab's console, to
`send-log.txt` or to `wangp-log.txt` — goes through `minipaint_neo/scrub.py` first. There
is one pass and every writer calls it, because the alternative is each writer deciding for
itself, which is how a log ends up holding a prompt.

The hardest writer to reason about is not ours. WanGP is a third-party application whose
output `runtime._drain` merely relays, and it prints lines like
`Saving video to /home/sarah/Wan2GP/outputs/sarah_at_her_mothers_house.mp4`. Relaying that
verbatim is what used to put prompts and filenames on the console. The scrub happens once,
in the drain, at the only point the child's words enter the process — so the crash tail,
the tab's console, the log file, the WebUI console and the sentence the error screen quotes
all read the same scrubbed text, and there is no writer left to remember to fix.

The rules are about shapes: prompt-ish keys taken to the end of the line; paths reduced to
`<path>/*.mp4`; bare filenames with a content extension; URLs, e-mail addresses and
non-loopback IP addresses; `key=value` credentials, vendor-prefixed tokens and any opaque
run long enough to be an id or a digest. Exception types, error codes, module filenames in
a traceback, versions, resolutions and WanGP's own setting names all survive — the pass is
useless if it makes a log unreadable.

`scrub.register_root(label, path)` is what keeps paths worth reading. `runtime` registers
the WanGP root and the interpreter prefix at every launch and the extension registers its
own folder at import, so a path under one of them comes out as `<wangp>/outputs/*.mp4`
rather than a flat `<path>`: the tree a file sits in is structure, and the directories above
it are the account name.

Two properties are checked directly in `tests/test_logging_privacy.py`: scrubbing twice is
scrubbing once (lines pass through more than one writer), and the pass never raises (its
largest caller is the drain thread, and that thread dying means the child's pipe fills and
WanGP freezes mid-generation).

What it cannot remove is a bare personal name in free text with nothing structural around
it — in practice, a model, LoRA or preset the user named themselves. That is kept on
purpose: "which model failed" is the question a transfer log exists to answer.

## Security invariants you can check yourself

**The backend is loopback-only.** While WanGP is running, look at the listeners:

```
# Linux / macOS
ss -ltnp | grep python          # or: lsof -nP -iTCP -sTCP:LISTEN
# Windows
netstat -ano | findstr LISTENING
```

The child's port must appear as `127.0.0.1:<port>`, never `0.0.0.0:<port>` and never
`[::]:<port>`. From a second machine on the same network, nothing on that port answers.

**No `--listen`, no `--share`, no `0.0.0.0`.** Look at the child's command line
(`ps -ef | grep wgp.py`, or Task Manager's *Command line* column). It is either

```
<prefix>/python -u wgp.py --server-name 127.0.0.1 --server-port <n>
conda run --no-capture-output -p <prefix> python -u wgp.py --server-name 127.0.0.1 --server-port <n>
```

and nothing else. `runtime.command_line` refuses to return a command containing
`--listen`, `--share`, `--open-browser` or `--server-name` without `127.0.0.1`; it raises
`PROCESS_START_FAILED` rather than launching. The environment is pinned the same way:
`GRADIO_SERVER_NAME=127.0.0.1`, `GRADIO_SERVER_PORT=<n>`, and any inherited
`GRADIO_SHARE` or `GRADIO_ROOT_PATH` is dropped before the child sees it.

**The iframe is same-origin.** In the WanGP tab, inspect the iframe: its `src` is
`/wan2gp/` — a path, with no scheme, host or port in it — so it resolves against the Forge
origin the page is already on. In the browser's network panel, filter for `127.0.0.1`
while WanGP is loading and generating: every request goes to the Forge origin under
`/wan2gp/`, and the backend port appears nowhere. There is no mixed content and no second
certificate.

**The proxy has exactly one upstream.** `proxy.upstream()` composes
`http://127.0.0.1:<port>` from `runtime.current()` and from nothing else. There is no
query parameter, header, cookie or path segment anywhere in `proxy.py` that can influence
the destination. Try it: `GET /wan2gp/?url=http://example.com` is forwarded to WanGP with
that query string attached, exactly like any other, and reaches nowhere else.

**The GPU is the one you chose.** `CUDA_VISIBLE_DEVICES` is *set* to the configured UUID,
never inherited from Forge and never appended to. If the UUID is not present the tab shows
`GPU_UUID_MISSING` and offers Reinitialize — there is deliberately no "start anyway",
because the only thing "anyway" could mean is a different card.

**The handoff carries an id, not a path.** What crosses to the browser and back into
WanGP is 32 lowercase hex characters. Anything else — a name with a dot in it, a relative
path, an id of the wrong length — is rejected outright by `protocol.HANDOFF_ID_RE` on
both sides; it is never sanitised and retried. The path is built only as
`<handoff root>/<id>.png`, and the file is then checked for being a regular file, inside
the root after resolution, under the size and dimension ceilings, a real decodable PNG,
and matching the digest Forge recorded.

**Only our own child is ever killed.** The extension holds the `Popen` handle, the pid, the
process group (POSIX, via `start_new_session` and `killpg` on that pgid) or a Windows Job
Object with kill-on-close, and terminates that and only that. There is no name-based kill
anywhere in `runtime.py`, so a standalone WanGP or any other Python you are running is
never touched. The corollary is that a standalone WanGP started by hand is *not* managed:
it has no instance id in its environment, the bridge plugin loads and stays silent, and
this integration ignores it.

**No document-wide watching.** `javascript/minipaint_wangp.js` keeps exactly two things
alive between calls: the window's `message` listener, and one observer on the tab's own
iframe container. The document is never observed, nothing polls, no host tab is clicked at
startup, and the postMessage target origin is always this origin — `"*"` appears nowhere.
Every inbound message is checked for exact origin, exact source window, protocol version,
a type legal for that direction, the current channel id, a request id being waited on, and
a size under the shared ceiling; anything else is dropped without a reply.

## Updating the bridge plugin

The bridge is a small plugin this extension owns. It is the only piece of the integration
that knows a WanGP element id, and it is the piece to update when WanGP moves an input.

* **From the tab:** setup step 4, *Install or update it*. It copies
  `wan2gp_bridge/wan2gp-minipaint-bridge/` into `<WanGP root>/plugins/`, skipping
  `__pycache__`, VCS folders and build droppings, and reports how many files it wrote.
* **By hand:** `cp -r wan2gp_bridge/wan2gp-minipaint-bridge <your WanGP>/plugins/`.

The install refuses rather than works around: the destination is recomputed from the WanGP
root, must resolve to `<root>/plugins/wan2gp-minipaint-bridge`, and an existing folder is
replaced **only** when it already carries our `plugin_info.json`. A symlink in the way, a
name collision with somebody else's plugin, or a destination that escapes the root all stop
the install. Nothing outside that one folder is ever written.

**WanGP must be restarted afterwards.** Plugins are loaded once at startup, so copying
files over a running WanGP does not activate them, and the wizard says so after every
install. The tab's **Restart WanGP** button is on the error surface, so it is offered when
something has actually gone wrong; if WanGP is running happily and you have just replaced
its bridge, use **Reinitialize** and set up again, or reload the WebUI. (A restart button
that is always visible is worth having and is not in this version.)

Version comparison is exact equality, not a range: the two halves of the protocol are
released together, so an installed bridge that is *newer* than the extension is as wrong as
one that is older. Either way the code is `BRIDGE_VERSION_MISMATCH`, the tab stays usable,
and intelligent Send is switched off until it matches.

`plugin_info.json` also declares a WanGP version range, but that range is only an early
filter. Real compatibility is decided functionally at WanGP startup: the bridge resolves
each component it needs through WanGP's plugin API and reports what it actually got in its
handshake. A build that is missing a mandatory one answers `ready=false` with
`BRIDGE_COMPONENT_INCOMPATIBLE`, and the Send menu offers nothing rather than sending into
something that looked about right.

## Reading a failure code

Every failure this integration can report has a stable code and one sentence. The codes and
their sentences are in `minipaint_neo/wangp/errors.py`; nothing builds a message out of an
exception's text, so the tab, the Send menu and the log always say the same thing about the
same failure.

You will see a code in four places:

* **the tab's error surface** — the sentence, with the buttons that code allows;
* **a Send to line** — a short version, such as
  `WanGP: open WanGP tab to choose model/input`;
* **`logs/send-log.txt`** — `failed: CODE - the sentence`, followed by the receiver, the
  operation, the state revision, the per-step timings, and a detail line;
* **the diagnostic report** — as `failure code`, with `failure message` under it.

What the tab offers you depends only on the code:

* `SETUP_REQUIRED` → the wizard.
* Anything in `errors.REINIT_CODES` — `WANGP_ROOT_MISSING`, `RUNTIME_MISSING`,
  `RUNTIME_PROBE_FAILED`, `GPU_UUID_MISSING`, `CONFIG_UNREADABLE`,
  `CONFIG_SCHEMA_TOO_NEW`, `BRIDGE_MISSING`, `BRIDGE_DISABLED`,
  `BRIDGE_VERSION_MISMATCH`, `BRIDGE_COMPONENT_INCOMPATIBLE` → **Reinitialize**. These all
  mean "the setup on disk no longer describes reality", and restarting the process would
  only reproduce them.
* `AUTH_BOUNDARY_FAILED` → **nothing**. The proxy is refusing on purpose, and restarting
  changes nothing about who can reach the route. Prove the boundary and set
  `MINIPAINT_WANGP_ALLOW_UNPROVEN_AUTH=1`, or run this Forge without a sign-in of its own.
* Everything else → **Restart WanGP**.

Two of them do not take the tab away from you. `BRIDGE_VERSION_MISMATCH` and
`BRIDGE_COMPONENT_INCOMPATIBLE`, while WanGP is still serving pages, leave the iframe
visible and usable and only switch off intelligent Send — the tab calls this state
*degraded*.

The ones worth knowing by sight:

| code | what it actually means |
| --- | --- |
| `GPU_UUID_MISSING` | the card you chose is not in the machine. Nothing was started, and no other GPU was used. |
| `PORT_IN_USE` / `LOOPBACK_BIND_FAILED` | a port was chosen but the child never opened it. Usually WanGP died during startup — its output tail is in the WebUI console, in the tab's console, and in `logs/wangp-log.txt`. |
| `PROXY_ROOT_PATH_FAILED` | WanGP answers, but not under `/wan2gp/`. `GRADIO_ROOT_PATH` did not take effect in that build. |
| `AUTH_BOUNDARY_FAILED` | this Forge has a sign-in that could not be *proven* to cover `/wan2gp/`. Fail-closed, not a claim that it is exposed. |
| `IFRAME_NOT_READY` | there is no live WanGP page in this Forge tab yet. Open the WanGP tab and pick a model. |
| `NO_ACTIVE_RECEIVER` | the loaded model takes no image at all (no start frame, no end frame, no reference). Nothing is offered rather than something plausible. |
| anything else on the Send menu's WanGP line | the bridge inside WanGP refused the receiver query and named why; the code is in the line (`WanGP: unavailable (CODE)`) and the sentence in WanGP's console as `[wan2gp-minipaint-bridge] CODE: detail`. |
| `WanGP: did not answer in time` | the query reached the page and no acknowledgement came back within ten seconds. The line stays, a *WanGP: check again* line appears under it, and an answer that arrives inside the next minute replaces both. To see which step went quiet, read `logs/wangp-log.txt` after one attempt: the browser writes `WANGP_GET_RECEIVERS: asked (id)` and then `answered after N ms` or `refused … CODE (detail)`; the plugin inside WanGP writes `[wan2gp-minipaint-bridge] receivers: answered in N ms - start_frame allowed (location), …` when its event ran at all. A query that was asked, never answered by the plugin and never acknowledged is a click that Gradio did not turn into an event; the iframe's own console (`[minipaint bridge] …`) says whether the click was dispatched, on which set of controls, and how many of Gradio's animation frames had to be run by timer — see the next row. |
| the WanGP tab is not the one on screen | this is the ordinary case, not a fault: the Send menu is on the Mini Paint tab, so the WanGP iframe is a document the browser is not rendering, and some browsers give a document that is not rendered no animation frames at all. Gradio schedules every event trigger inside one and gates it on its component flush, which Gradio's core schedules through a reference captured when its module loaded - so a click on the bridge's hidden trigger would wait until the WanGP tab is opened again. The bridge therefore places a frame timer in the page head, before Gradio's modules, so that every frame Gradio asks for is also given a timer: a rendered page's own frame always wins, a hidden page's timer answers. The plugin's log says `frame timer script placed in the page head` at startup; if it says the script could not be placed, the page script installs the timer late, the iframe's console says `frame timer installed late - Gradio's own flush is not covered`, and a query from the Mini Paint tab may then wait until the WanGP tab is opened. WanGP's own focus patch reschedules the same frames for its hidden main tab, but needs a measurable panel, which a `display:none` iframe never has. |
| the menu line was clicked and nothing arrived in WanGP | a send leaves a line at every step, in order, in `logs/wangp-log.txt` (column `send` for this side, `browser` for the page, `wangp` for the plugin): `send: start_frame chosen from the menu` (the click reached the Canvas script), `WanGP Start Frame: prepared WxH; the browser is handed the file` (the Canvas's send event ran; `logs/send-log.txt` has the same `prepared` entry), `deliver: the server prepared the picture for start_frame` (the browser step ran), `WANGP_RECEIVE_IMAGE: asked (id) for start_frame` (the page was asked), `[wan2gp-minipaint-bridge] receive: start_frame replace pixel-equivalent in N ms` (the plugin placed it), `WANGP_RECEIVE_RESULT: answered after N ms` and `the browser reports: start_frame taken`. The WanGP tab then opens by itself. The first line missing names the half that stopped: no `chosen from the menu` line means the click never reached the script (the browser console is the place to look); no `prepared` line means the Canvas's send event never ran or raised, and the Forge console then has the traceback; `send: refused before asking - CODE` is the browser declining to ask, with the reason; `asked` without a plugin `receive:` line means the message never became a Gradio event inside the iframe, and the iframe's own console says why (`receive: refused before the click - CODE`, `dropped WANGP_RECEIVE_IMAGE: channel …`, or `receive: queued` with nothing after it); a `receive:` line without a result is an acknowledgement that never came back, and the browser writes `no acknowledgement within 30000 ms` when it gives up. |
| `STALE_RECEIVER_STATE` | the WanGP page moved between the menu opening and the click. Reopen *Send to*. |
| `BRIDGE_SESSION_MISMATCH` / `WANGP_RESTARTED` | the WanGP page or process is not the one this send was prepared for. The image was not applied. |
| `RECEIVER_VERIFY_FAILED` | WanGP took an image, but it could not be confirmed as the one that was sent. The tab does not switch, and the log keeps the detail. |

## Diagnostics

*Integration management → Diagnostics → Copy diagnostic report* in the WanGP tab builds one
block of text for a bug report. It is a whitelist, not a dump: Forge and Gradio versions,
the extension's commit, the protocol number, the shipped and installed bridge versions, the
setup fields with paths reduced to their last component, the GPU UUID and whether that card
is present, the runtime state and uptime, whether a child and a job object are tracked,
whether the port is bound, the failure code, the proxy probe steps if one has run, the live
bridge sessions with truncated channel ids, the last forty transfer-log lines, and the last
twenty-five lines of the WanGP process log.

No secret, cookie or authorization header has a line in it — not even a redacted one. The
pid and the port are reported as facts rather than numbers. The GPU UUID *is* included in
full, because it is the one field that makes `GPU_UUID_MISSING` diagnosable and it
identifies a piece of hardware rather than a person.

The report never raises: every field is collected inside its own guard, and one that could
not be collected says `unavailable`.

The two log tails it quotes are safe to carry for the same reason the files are safe to
attach: they were scrubbed as they were written, not as they were read.

## Reinitialize

There is exactly one entry on the Settings page — *Settings → miniPaint / Canvas → WanGP
integration* — and it is a paragraph with a link, not a switch. Forge's settings system
stores values, and a checkbox meaning "reinitialize" would be a checkbox that gets saved.
The real button lives in the WanGP tab, under *Integration management*, so there is one
place that knows what the running process was started from.

Reinitialize stops the child this extension started, invalidates every browser session and
receiver list, copies the active setup into the backup, marks the active setup incomplete,
and brings the tab back to the wizard. It does not uninstall WanGP, and it deletes no
models, no LoRAs, no presets, no outputs, no settings of yours and no plugin — the only
plugin folder it can ever write to is its own, and only when a new setup explicitly asks it
to install one.

## Known limits

* **One managed WanGP.** The extension runs a single child. A standalone WanGP you start
  yourself against the same install is not detected, not attached to and not killed;
  running both against one WanGP root is not something this version has proven safe.
* **The Canvas frontend only.** WanGP destinations appear in the Canvas's *Send to* menu.
  The legacy miniPaint (Old UI) frontend has its own send path and does not offer them.
* **No always-visible Restart.** *Restart WanGP* is offered on the error surface, which is
  where it is usually wanted, but not while WanGP is running normally.
* **Three receivers.** Start frame, end frame and reference are the v1 set. The protocol
  already names control image, positioned reference and style reference, and the bridge
  publishes none of them, so Mini Paint never offers them. Adding one is three declarations
  in `compatibility.py` and a small adapter — no change on the Forge side.
* **The component names in `compatibility.py` are Wan2GP's own variable names.** They
  were checked against Wan2GP at commit `362c346` (the generator form's locals are what
  the plugin API hands over by name; `"S"`/`"E"` in `image_prompt_type` and `"I"` in
  `video_prompt_type` are the letters generation reads). A fork that renames one fails
  closed: a missing mandatory component is `BRIDGE_COMPONENT_INCOMPATIBLE`, and a missing
  optional one (the selector controls a send may switch, `image_mode`, the page `state`)
  only takes away the "offer what the model allows" half — a receiver is then offered
  only while WanGP's own selector already has it switched on, as before. See
  `docs/wangp/PHASE0.md` for the checks that still want a real Forge and a real WanGP.
