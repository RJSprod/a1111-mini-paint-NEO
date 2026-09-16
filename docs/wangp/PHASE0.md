# Phase 0 — the compatibility spikes, as a checklist

Section 38 of the design intent lists seven things that must be proved before the
integration can be trusted, and section 50 gives the order to prove them in. Section 49
lists ten more that "must receive deliberate developer attention". This file turns all of
that into a checklist for somebody sitting in front of a real Forge and a real WanGP.

## Status: none of this has been run

The whole integration was written in a container with **no Forge, no WanGP, no NVIDIA
driver, no Gradio front end and no browser**. Every claim in the code about a WanGP
element id, a Gradio event side effect, a Windows job object, a reverse-proxied WebSocket
or a Forge authentication boundary is therefore an *unverified* claim, written from
documentation and from the design intent's own research notes.

What *was* possible, and is not in question:

* every module imports and its pure functions behave as written;
* the two copies of `protocol.py` are byte-identical in their shared block;
* the argv builder refuses `--listen`, `--share`, `--open-browser` and a `--server-name`
  that is not `127.0.0.1`, and raises rather than launching;
* the handoff id grammar, the path construction and the file checks are exact;
* the config transaction writes, promotes and restores without ever leaving the active
  file absent or truncated.

What was **not** possible, and is what this file is for:

* starting WanGP at all;
* loading any page through `/wan2gp/`;
* resolving a single WanGP component id through the plugin API;
* proving a Gradio event applies a value the way a human upload does;
* proving what Forge's authentication does to a route added at `on_app_started`;
* proving Windows process-tree ownership;
* proving that two browser sessions stay separate.

Treat every unticked box below as a known unknown, not as an oversight. Where the code
could not prove something, it fails closed — a component that will not resolve produces
`BRIDGE_COMPONENT_INCOMPATIBLE` and an empty Send menu, not a guess — so an unverified
build is expected to be *unhelpful*, not dangerous. That is the property to confirm first.

The code was written whole rather than in section 50's spike-first order, so section 50 is
used below as a **verification** order instead: the same sequence, run against what is
already built, stopping at the first step that fails.

## What you need

* A Forge Neo install with this extension in `extensions/a1111-mini-paint-NEO`.
* A WanGP install you can restart freely, and its Python environment.
* At least one NVIDIA GPU, and ideally two, so the GPU pinning can be seen to be real.
* A second browser (or a private window) for the two-session spike.
* A terminal with `curl`, and `ss`/`netstat`/`lsof`.

Throughout, `$FORGE` is your Forge origin — `http://127.0.0.1:7860` unless you moved it.

## How to record what you find

For each box, write down the **WanGP version**, the **Gradio version inside WanGP**, the
**Gradio version inside Forge**, and the OS. Every finding below is only true of one
combination of those, which is precisely why section 51 asks for the compatibility tests
to be re-run when WanGP updates. The WanGP tab's *Copy diagnostic report* collects the
first three for you.

---

## Part 1 — the seven spikes of section 38

### A. Forge can proxy this WanGP under `/wan2gp/`

The wizard's step 5 covers most of this, but the transport completeness of section 49.3
is not something a checklist row can see. Do both.

* [ ] **The wizard's own rows.** Complete setup steps 1–4, press *Run the checks*, wait
  for WanGP to finish loading, press it again. **Proves:** the rows *WanGP serves itself
  under /wan2gp*, *the WanGP page loads through the proxy* and *a WanGP asset loads
  through the proxy* go green.
* [ ] **The base page by hand.** `curl -sS -o /dev/null -w '%{http_code}\n' $FORGE/wan2gp/`
  **Proves:** `200`. The proxy's own failures are distinguishable: `503` with
  `PROXY_NOT_READY` means the runtime has no port for it to talk to (WanGP is not
  running), `502` means the upstream refused the connection, `504` means it did not answer
  in time.
* [ ] **The redirect.** `curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' $FORGE/wan2gp`
  **Proves:** a 307 to `/wan2gp/`.
* [ ] **Root path in the page.** `curl -sS $FORGE/wan2gp/ | grep -o 'wan2gp' | head`
  **Proves:** Gradio's own config in the served page names `/wan2gp` as its root, which is
  what makes every asset URL it generates resolve back through the proxy. This is the
  check behind `PROXY_ROOT_PATH_FAILED` — the code raises that from `proxy.probe`, so it
  reaches you as a checklist row and a tab message rather than as an HTTP status.
* [ ] **The real UI, in the tab.** Open the WanGP tab and use WanGP normally: pick a
  model, wait for it to load, change modes, open the settings accordions. **Proves:** the
  application is genuinely usable, not merely served.
* [ ] **Queue and streaming (49.3).** Start a generation from inside the iframe and watch
  the browser's network panel. **Proves:** the queue/SSE connection stays open for the
  whole generation and progress updates arrive continuously. The proxy classifies
  `/queue/`, `/stream`, `/sse` and `/heartbeat` as stream-shaped and removes the read
  timeout for them; a generation that dies at exactly 120 seconds means that
  classification missed a path this Gradio uses, and the fix is
  `proxy.STREAM_PATH_MARKERS`.
* [ ] **WebSocket, if this Gradio uses one.** Filter the network panel by WS while WanGP
  loads. **Proves:** either there is no WebSocket (recent Gradio), or there is one and it
  connects and stays up through `/wan2gp/`.
* [ ] **Upload.** Drop a large image or video into a WanGP input through the iframe.
  **Proves:** it uploads. Nothing in `proxy.py` buffers a body, so a failure here is a
  header or a timeout, not a size limit of ours.
* [ ] **Media and range requests.** Play a finished video in WanGP's own output player,
  and seek in it. **Proves:** `Range`/`206` survives the proxy.
* [ ] **A full generation, end to end.** **Proves:** everything above at once. This is the
  real gate for A.

### B. A protected Forge does not expose `/wan2gp/` unauthenticated

**This is a release blocker (49.4).** Route registration is not protection, and the code
refuses to claim otherwise: `proxy.auth_boundary_report` only reports *covered* when
authentication runs as ASGI middleware, and reports *unknown* — which fails the checklist
— for per-route mechanisms it cannot see cover a dynamically added route.

* [ ] **Unauthenticated Forge, for a baseline.** With no auth configured, check the WebUI
  console at startup for the line
  `MiniPaint WanGP: reverse proxy ready at /wan2gp/ …`. **Proves:** the routes installed.
  With no auth configured, `auth_boundary_report` reports `no_auth_configured` and the
  checklist row is not mandatory — correctly, since `/wan2gp/` is then exactly as
  reachable as the rest of Forge.
* [ ] **Authenticated Forge.** Restart Forge with `--gradio-auth user:pass` (or whatever
  your deployment actually uses), then, with no credentials:
  `curl -i -o /dev/null -w '%{http_code}\n' $FORGE/wan2gp/`
  **Proves:** `401`, or a redirect to the login page. A `200` here is the failure this
  spike exists to catch, and it means the integration must not ship enabled for that
  configuration.
* [ ] **The same, with credentials.** `curl -i -u user:pass $FORGE/wan2gp/`
  **Proves:** `200`. Authentication that blocks the proxy for *everyone* is also a
  failure.
* [ ] **A deep path, not just the base.**
  `curl -i -o /dev/null -w '%{http_code}\n' $FORGE/wan2gp/queue/join` unauthenticated.
  **Proves:** the boundary covers the whole subtree, not only the page.
* [ ] **What the tab decided.** Look at the step-5 row *"/wan2gp/ is covered by this
  Forge's sign-in"* and at `auth_boundary_report`'s `coverage` in the diagnostic report.

* [ ] **The refusal is real.** While that row is red, `curl -i $FORGE/wan2gp/` *as a signed-in
  user*. **Proves:** `503` naming `AUTH_BOUNDARY_FAILED` — the proxy fails closed rather than
  merely logging the doubt, so an unproven boundary cannot serve WanGP to anyone. Then set
  `MINIPAINT_WANGP_ALLOW_UNPROVEN_AUTH=1`, restart Forge, and repeat both this request and the
  unauthenticated one above. **Proves:** the signed-in request now returns the WanGP page and
  the unauthenticated one still returns Forge's own `401` — which is the whole point of the
  override, and the reason it must never be set before that second result has been seen.
  The checkbox in the wizard's step 5 does the same thing and is saved with the setup; the
  environment variable exists for a deployment that would rather not store the answer.
  **Proves:** the code's verdict matches what curl just showed. If curl says protected but
  the row says `unknown`, the mechanism is per-route and the report is being honest about
  not being able to see it — record which mechanism, and treat the curl result as the
  answer.
* [ ] **In front of a public reverse proxy, if that is your deployment.** Repeat the
  unauthenticated request from outside. **Proves:** TLS terminates at Forge or its proxy,
  and the browser never needs a second certificate for WanGP.

### C. The bridge plugin can request and read the required receiver components

This is section 49.1, and it is the single largest unknown in the codebase. The element
ids in `compatibility.py` were written from WanGP's documented media-input naming, not
from a running build, and every one of them carries a `VERIFY ON A REAL INSTALL` comment.

* [ ] **Install and restart.** Setup step 4, *Install or update it*, then restart WanGP.
  Note that **Restart WanGP** only appears on the tab's error surface, so during setup the
  ways to restart a running WanGP are Reinitialize-and-set-up-again or reloading the
  WebUI; the simplest order is to install the bridge *before* pressing *Run the checks*
  for the first time. **Proves:** the plugin files are in
  `<WanGP root>/plugins/wan2gp-minipaint-bridge/` and were loaded, not merely copied —
  WanGP loads plugins once at startup.
* [ ] **The plugin loaded at all.** Look at WanGP's own console output. **Proves:** either
  silence (loaded and happy) or a line beginning `[wan2gp-minipaint-bridge]`. A line
  reading `not ready: missing …` names exactly which component keys did not resolve, and
  is the most useful output of this whole spike.
* [ ] **The hooks exist and have the signatures assumed.** `setup_ui`, `post_ui_setup`,
  `request_component`, `request_global`, `on_model_change`, `add_custom_js`. Every
  override takes `*args, **kwargs` and forwards, so a signature change shows up as a
  mismatch to correct rather than a crash inside WanGP's startup. **Proves:** no exception
  in WanGP's console during UI construction. Watch specifically for
  `this WanGP has no add_custom_js; the bridge cannot reach the browser` — without that
  hook there is no browser half at all.
* [ ] **The five mandatory components resolve.** These are the ids to confirm, with the
  candidate spellings the code tries in order:

  | key | candidates | why it is mandatory |
  | --- | --- | --- |
  | `start_image` | `image_start`, `image_prompt_start`, `start_image` | the start-frame receiver |
  | `end_image` | `image_end`, `image_prompt_end`, `end_image` | the end-frame receiver |
  | `reference_gallery` | `image_refs`, `image_references`, `reference_images` | the reference receiver |
  | `image_prompt_type` | `image_prompt_type` | decides whether start/end are in play |
  | `video_prompt_type` | `video_prompt_type` | carries the reference-image selection |

  **Proves:** the handshake reports `ready=true` and no `not ready: missing` line appears.
  If a name is wrong, correct the `candidates` tuple in
  `wan2gp_bridge/wan2gp-minipaint-bridge/compatibility.py` — that file is the only place
  in the whole integration allowed to know a WanGP identifier, and nothing else changes.
* [ ] **The optional ones, for the record.** `audio_prompt_type` and `model_mode` are not
  mandatory. **Proves:** note whether they resolved, since a build that gates image roles
  behind audio conditioning will need them.
* [ ] **The flag letters (49.1, second half).** With a MiniMax H3 model selected, switch
  start-frame, end-frame and reference conditioning on and off in WanGP's own UI and watch
  what the live values become. **Proves:** `image_prompt_type` contains `S` when the start
  frame is in play and `E` for the end frame, and `video_prompt_type` contains `I` when
  reference images are. These are `compatibility.SELECTION_RULES`, and a rule that finds
  no source value at all is treated as *not satisfied* — so a wrong letter shows up as a
  receiver that is never offered, never as one offered wrongly.
* [ ] **The receiver query, from the Forge side.** With the WanGP tab open on a model that
  takes a start frame, open the Canvas's *Send to* menu. **Proves:** exact lines appear —
  "Send Image to WanGP Start Frame" and so on — rather than
  `WanGP: this model and mode take no image right now`.

### D. A bridge Gradio event can populate the Start Frame

* [ ] Select a model and mode where the start frame is active. In the Canvas, prepare an
  image and choose *Send Image to WanGP Start Frame*. **Proves:** Forge switches to the
  WanGP tab and the start-frame input visibly holds the image.
* [ ] **It went through a real event, not the DOM (21.2).** After the send, change
  something unrelated in WanGP and let the page round-trip. **Proves:** the image is still
  there. A value set only in the browser would not survive.
* [ ] **The Send to menu was not open when it happened.** **Proves:** nothing polls: the
  receiver query is one bounded question per menu opening, and a late answer to a closed
  menu is dropped.

### E. A bridge Gradio event can append a Reference Image

This is section 22, and it is the one with a data-loss failure mode.

* [ ] Put two reference images into WanGP by hand. Send a third from the Canvas.
  **Proves:** the list holds **three** in their original order, with the new one last. A
  list holding one is the lost-update bug this whole design exists to prevent.
* [ ] Repeat until the receiver is full. **Proves:** the *Send to* menu stops offering the
  line and shows `WanGP <label>: limit reached` instead — `RECEIVER_LIMIT_REACHED`, not a
  silent overwrite.
* [ ] Send two images in quick succession without letting the first finish. **Proves:**
  the count ends at exactly two more than it started (35.2). Sends are serialised per
  session and receiver.

### F. Pressing Generate manually uses the bridge-inserted image

This is section 49.2, and it is the acceptance criterion the bridge cannot prove from the
inside: an adapter can prove the value it hands Gradio decodes to the right pixels, but
not that Gradio kept it and that generation reads it.

* [ ] For **each** of start frame, end frame and reference: send an image with the bridge,
  look at the visible UI, then press **Generate** *without touching the field*.
  **Proves:** the generation used the inserted image.
* [ ] If any of the three does not, find the dependent callback a human interaction would
  normally fire and add it to that adapter's `downstream_updates` in
  `receiver_adapters.py`. **Proves:** the same test then passes. Do not solve this from
  the browser.
* [ ] **Nothing generated on its own.** **Proves:** a send populates and stops. The bridge
  never presses Generate, and there is no code path in it that could.

### G. Two browser sessions do not share live receiver state

* [ ] Open Forge in two browsers (or one plus a private window). In each, open the WanGP
  tab and select a **different** model — ideally one that takes a start frame and one that
  takes none. **Proves:** each Canvas *Send to* menu offers that browser's own receivers.
  A menu showing the other window's model is the process-global bug section 13.2 is
  written against.
* [ ] Send an image from window A. **Proves:** window B's WanGP inputs do not change.
* [ ] Open *Send to* in window A, then, before clicking, change the model in window A's
  WanGP tab. Click the line. **Proves:** `STALE_RECEIVER_STATE` — "The WanGP input
  changed; reopen Send to" — and no image is applied anywhere. The state fingerprint is
  recomputed at apply time from freshly read values.
* [ ] Reload window B's Forge page and reopen its WanGP tab. **Proves:** window A still
  works, and B gets a fresh channel rather than reusing a dead one.
* [ ] **Restart during a send (35.4).** Begin a send, and restart WanGP from the tab
  before it lands. **Proves:** `WANGP_RESTARTED`, and the image is not applied to the new
  instance.

---

## Part 2 — section 50's order, as a verification sequence

Section 50 is written for somebody starting from nothing. The code exists, so run its
steps as a verification order instead and stop at the first failure — the sequence is
chosen so that each step's failure is diagnosable without the ones after it.

* [ ] **1–2. WanGP starts on loopback with `GRADIO_ROOT_PATH=/wan2gp`.** Complete setup
  steps 1–3 and press *Run the checks*. Confirm with `ss -ltnp` / `netstat -ano` that the
  listener is `127.0.0.1:<port>`, and with `ps -ef | grep wgp.py` that the argv is exactly
  `… wgp.py --server-name 127.0.0.1 --server-port <port>`.
* [ ] **3. The real UI works fully through `$FORGE/wan2gp/`.** All of spike A.
* [ ] **4. The plugin loads.** Spike C's first three boxes.
* [ ] **5. One known receiver resolves — Start Frame.** Spike C's component table, at
  least `start_image`, `image_prompt_type`.
* [ ] **6. The hidden bridge event exists.** The bridge asks WanGP, through
  `insert_after` during `post_ui_setup`, to place three invisible controls inside the
  generator form, and wires one event on them in that builder. Nothing built anywhere
  else is on the page: a component created before WanGP's Blocks exist has an id and is
  still absent from the page config. Its absence shows as a `Send to` menu that never
  gets an answer: `WanGP: unavailable — open WanGP tab`. WanGP's console says
  `controls placed after 'image_prompt_type' (set 1); event wired` when it worked.
* [ ] **7. A parent message populates one image.** Spike D.
* [ ] **8. Generate uses it.** Spike F, start frame only, at this point.
* [ ] **9. Reference append works.** Spike E.
* [ ] **10. The live receiver query answers.** Spike C's last box, on at least two
  different models, one of which offers nothing.
* [ ] **11. The state revision is deterministic.** Open *Send to* twice without touching
  WanGP. **Proves:** the same revision both times, and the menu does not reshuffle. Then
  change one control and reopen. **Proves:** a different revision.
* [ ] **12. Two browser sessions.** Spike G.
* [ ] **13. Only then trust the persistent setup UX.** Reinitialize, set up again, restart
  Forge, and confirm the tab comes back to a working WanGP without asking anything.

---

## Part 3 — the section 49 items, and where each one stands

| § | item | status | what settles it |
| --- | --- | --- | --- |
| 49.1 | exact receiver component ids and types | **unverified** — ids written from documentation, each marked `VERIFY ON A REAL INSTALL`; fails closed | spike C |
| 49.2 | Gradio event side effects after a programmatic update | **unverified** — adapters return values through a real event and can add `downstream_updates` if needed | spike F |
| 49.3 | reverse-proxy transport completeness | **unverified** — nothing is buffered, timeouts are three classes, a WebSocket route is registered, but no Gradio release was ever behind it | spike A's streaming, upload, media and WS boxes |
| 49.4 | Forge authentication coverage | **unverified, release blocker** — reported as `covered` only for ASGI middleware, `unknown` otherwise, and `unknown` fails the checklist | spike B |
| 49.5 | Windows process-tree ownership | **unverified** — a Job Object with kill-on-close is attached through `ctypes` where Windows allows it, with `CREATE_NEW_PROCESS_GROUP \| CREATE_BREAKAWAY_FROM_JOB`; POSIX uses `start_new_session` and `killpg` on the tracked pgid only | see below |
| 49.6 | a standalone WanGP running concurrently | **by design, not proven** — a single managed instance is documented, an unmanaged WanGP is ignored and never killed, and no attach mode exists | see below |
| 49.7 | Settings-only Reinitialize button | **implemented as the documented fallback** — one non-persisting entry (`OptionHTML` where the host has it) linking to the tab, where the real button lives; no dummy boolean anywhere | confirm the entry appears under *miniPaint / Canvas* and the link switches tabs |
| 49.8 | direct interpreter vs `conda run` | **implemented, unverified against a real Conda install** — the wizard probes and persists whichever worked | setup step 2 on a Conda environment that needs its activation scripts |
| 49.9 | theme parity | **not a functional requirement** — the bridge injects its own stylesheet inside its own try/catch, so a theme failure cannot take image handoff with it | look at the iframe in dark mode; a wrong colour is not a blocker |
| 49.10 | legacy MiniPaint UI | **out of scope for v1** — WanGP destinations are in the Canvas *Send to* menu only; the Old UI keeps its existing send path unchanged | confirm the Old UI still sends to img2img/Inpaint/Extras exactly as before |

### 49.5 — Windows process ownership, in detail

* [ ] Start WanGP from the tab on Windows. In the diagnostic report, check
  `job object  yes`. **Proves:** the child was assigned to a Job Object with
  kill-on-close. `no` means `ctypes` could not attach one, and the fallback is the process
  group alone.
* [ ] Note the child's pid in Task Manager, then close Forge **without** stopping WanGP
  from the tab. **Proves:** the WanGP process and its grandchildren are gone. If they
  survive, the Job Object did not take and section 49.5 is unresolved for that Windows
  build.
* [ ] Start a second, unrelated Python process. Stop WanGP from the tab. **Proves:** the
  unrelated process is untouched. There is no name-based kill in `runtime.py`, and there
  must never be one.
* [ ] On Linux, the same three: `ps -o pid,pgid,cmd -p <pid>` shows the child in its own
  process group, and stopping it takes the group and nothing else.

### 49.6 — a standalone WanGP at the same time

* [ ] Start WanGP by hand, on its own port, from the same root. **Proves:** the extension
  does not find it, does not attach to it and does not kill it, and its console shows no
  `[wan2gp-minipaint-bridge]` activity — with no `MINIPAINT_WANGP_INSTANCE_ID` in the
  environment the plugin adds nothing to the UI at all.
* [ ] Now start the managed one too. **Proves:** whether two WanGPs against one install
  interfere over configuration, caches or outputs. **This has not been proven safe.** The
  documented expectation for v1 is a single managed instance; if this is a use case you
  need, treat the result as a finding rather than as a supported configuration.

---

## Part 4 — regression, before any of the above counts

Section 39's list. If any of these fails, nothing else matters, because the WanGP tab was
supposed to be additive.

* [ ] The Mini Paint tab still builds, in both frontends.
* [ ] Canvas crop, mask, expand and layers still work.
* [ ] Old UI still loads and still sends.
* [ ] Sends to img2img, Inpaint, Extras and ImageStitch are unchanged.
* [ ] `logs/send-log.txt` is still created at startup and still rotates at 1 MB.
* [ ] `tests/browser_smoke.py` still passes, with WebGL and without.
* [ ] No new document-wide `MutationObserver`. `javascript/minipaint_wangp.js` observes
  one element — the WanGP tab's own iframe container — and nothing else.
* [ ] **With the WanGP package deliberately broken** (rename `minipaint_neo/wangp/ui.py`
  and restart): Forge starts, prints
  `MiniPaint: the WanGP integration did not load (…); Mini Paint is unaffected.`, and the
  Mini Paint tab works normally. This is the invariant the whole feature is wrapped in;
  put the file back afterwards.

---

## Part 5 — protocol 4, the process, and the queue outbox

Written, like the rest, without a WanGP to hand. Each row names what proves it and what
to correct if it comes back different. None blocks the build; every one changes a
constant or a candidate list, never the shape.

* [ ] **`generate_trigger` resolves.** With the bridge loaded, the handshake in the WanGP
  tab's console says `capabilities.start: true`. If it says `start_missing:
  ["generate_trigger"]`, find the hidden `gr.Text` declared beside `add_to_queue_trigger`
  in `wgp.py` and correct the candidate in `compatibility.COMPONENTS`. Until then every
  request is staged and the answer says `start: "unknown"`; nothing is wrong, only slower.
* [ ] **`is_generation_in_progress` is the flag `process_tasks` raises.** A hello answer
  carries `generation_running: false` while WanGP is idle and `true` while it generates
  (press Generate by hand in WanGP and ask again). `null` means the build injected
  something that is not a function, and every request is then staged; the row to correct
  is `compatibility.GLOBALS`.
* [ ] **Idle starts, busy joins.** With WanGP idle, press Add to Queue in the Clipboard tab
  once: WanGP starts generating by itself and the tab says *WanGP started generating it*.
  Press again while it runs: the tab says *Added to WanGP queue* (with how many are ahead)
  and the task joins the run - **a second run must never start**. If one does, the flag
  above is not process-wide on that build and `_start_route` in `plugin.py` must fall back
  to the queue route until it is.
* [ ] **The overlay is what the task got.** After the press, open WanGP's queue: the task's
  prompt is the Clipboard prompt and its start frame the Clipboard picture, and the WanGP
  form shows its own prompt and pictures again once the tab reports. If the form keeps the
  overlay, read the plugin's `restore skipped` line: it names the components whose value
  the page had changed meanwhile, and a gallery there means Gradio's cache moved the
  pixels more than `handoff.SIGNATURE_TOLERANCE` allows.
* [ ] **`queue_errors` on this build.** Queue a prompt WanGP's validation rejects (an empty
  one on a model that requires it). The tab must say *WanGP declined the queue request*
  within a few seconds; *WanGP did not confirm…* means the build records the refusal in a
  shape `admission.refusal_evidence` does not read - print `get_gen_info(state)["queue_errors"]`
  once and add that shape.
* [ ] **Two Forge servers, one WanGP.** Start a second Forge against the same data root
  and open its WanGP tab: it must show `WANGP_ALREADY_MANAGED` and start nothing, and
  `…/runtime/wangp.lock.json` must name the first Forge's pid. Stop the first Forge
  without stopping WanGP through it (kill it): the second Forge's next start must remove
  the stale lock and proceed. On Windows there is no start-time check, so a recycled pid
  looks live until it exits; note it if you meet it.
* [ ] **The emergency restart.** With a generation running, press *Restart WanGP now* in
  Integration management. The report must list the tree's processes and their memory
  before, say every process exited, and say how much came back; `nvidia-smi` afterwards
  must show no process of the old tree on the card. If per-process memory reads `N/A`
  (WSL, some Windows driver modes), the report says VRAM is unverified and the tree is
  still verified by pid - record which driver mode you were in.
* [ ] **Two browser pages, one line.** Open the Clipboard tab in two browsers, press in
  both quickly: the Queue in each shows both jobs, one is *Sending* at a time, the order is
  the order pressed, each browser's job runs against its own WanGP page. Close one browser
  with a job waiting: the other shows it as *composed on another page* with *Run from this
  page*, and never runs it by itself.
* [ ] **Nothing polls with nothing to do.** With the Queue empty and the tab open, the
  browser's network panel shows no request to `/minipaint-interop/outbox/claim`; after a
  press, claims stop once the page's jobs are done.

## Part 6 — enhanced prompts and the queue's two lives

Bridge 1.4.0 / protocol 5 with the *SD-Neo-ModelSwitchRefiner* extension installed in the
same Forge. Everything below is written to that extension's `docs/21-external-llm-api.md`
and exercised against a fake shaped like it; none of it has met the real `mc_llm_api` yet.

* [ ] **The API is found.** With ModelSwitchRefiner installed and LLM Studio set up, open
  the Clipboard tab: the panel under the prompt must say *LLM Studio ready (<model>)* and
  name WanGP's model. The journal has one `mc_llm_api imported from the ModelSwitchRefiner
  extension (API version 1)` line. If the panel says *not found*, note the two extensions'
  folder names and whether ModelSwitchRefiner's tab loaded at all.
* [ ] **The variant follows WanGP.** Load `minimax_h3_fl2va` in WanGP: the line says
  *FL2VA*; load `minimax_h3_ref2va`: *Ref2VA*; load anything else: *not a MiniMax H3
  model*, and a press with the switch on is refused with `ENHANCE_MODEL_UNSUPPORTED` and
  stores nothing. If a real H3 model type is spelled otherwise, record it -
  `enhance.variant_for_model` reads the type, the architecture, the family and the label.
* [ ] **An enhanced press.** Switch on, type a prompt, put a picture in First Frame on FL2VA,
  press. The Queue shows *Enhancing* with LLM Studio's own stage text; LLM Studio's MiniMax
  panel shows the banner naming `minimax-clipboard`; when it finishes, the card shows the
  typed prompt struck through and the written one, then *Sending*, then WanGP's task carries
  the written prompt and the First Frame. *Saved prompts* in LLM Studio has the result.
* [ ] **The pictures follow the model.** On FL2VA with only a Reference filled, the status
  line must say the reference was left out of the enhancement and the card's LLM line must
  not name a described picture; on Ref2VA the reverse. A model without vision must refuse a
  press with a picture (`ENHANCE_NO_VISION`) before anything is stored.
* [ ] **The system prompt.** Edit the FL2VA *with a picture* set, Apply override, press with
  a picture: LLM Studio's run must use your text (its console says `system_override`).
  Restart Forge: the override is still there. Restore default: the API's text is back.
* [ ] **The line is strict.** Press an enhanced job, then a plain one from a second browser:
  the plain one shows *Waiting* and does not reach WanGP until the first has. Its claim
  answers `reason: enhancing`.
* [ ] **Cancel everything.** With two enhancing and one pending job, press it: LLM Studio's
  banner must clear, its queue must be empty of ours, and the three cards must read
  *Cancelled*. A job that was *Sending* at that moment finishes.
* [ ] **Cancelled from the other side.** Press *Cancel all queued* in LLM Studio's own banner
  while a job is enhancing here: the card must read *Cancelled* with the reason naming LLM
  Studio, within a couple of seconds, with no page pumping (the watcher thread).
* [ ] **Tracking.** After a queued job, watch its card: *In WanGP's queue, N ahead of it*
  counting down, *WanGP is generating it*, and then the card must **leave the queue** - a
  queue lists what is still to happen, and the video is in *View Outputs*. Reload the Forge
  page while a task is still waiting: the card must say *no longer tracked*, not
  *finished*, and must stay.
* [ ] **Dismiss.** Make a job fail (stop WanGP and press *Add to Queue*): the card must
  read *Refused* and must still be there after several minutes - a failure waits for a
  person. Press *Dismiss*: it goes, and nothing else on the queue moves.
* [ ] **View Outputs.** After a generation, open it under the queue: the video must be
  there, must play, and the scrub bar must seek (not just play from the start). Restart
  Forge and open it again: the same video must still be listed. Generate one by hand in
  WanGP's own tab, with nothing queued from here, and confirm it does **not** appear.
* [ ] **The model moved.** Enhance on FL2VA, then switch WanGP to another model before the
  job is sent: the card must read `MODEL_CHANGED`, nothing must be written into the WanGP
  form, and Retry must write the prompt again for the new model (or refuse it, if that
  model is not an H3 one).

