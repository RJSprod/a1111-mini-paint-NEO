# Server-owned execution: press Add to Queue and walk away

What this is, how it is built, which dependency facts it rests on, and — the
part that matters most when somebody comes back to this in a year — which of
those facts were *read* and which are still inferences waiting for a real
install.

The specification is
`PERFORMANCE_DESIGN_INTENT_2026-09-13_WALKAWAY_VERIFIED_1.txt` and its review
response `PERFORMANCE_DESIGN_INTENT_WALKAWAY_VERIFIED_REVIEW_RESPONSE_2026-09-14.txt`.
Where the two disagree, the review response wins and section 3 below says why.

---

## 1. The requirement, and why the browser cannot meet it

> After Forge durably acknowledges a submitted job, the browser that composed
> it may be hidden, frozen, discarded, closed, disconnected, or powered off.
> The job must continue.

A browser cannot be made into a reliable background worker. Timer throttling,
freezing, discard, app switching and network loss are browser policy, outside
this extension's control, and no amount of care changes any of them. So the
only architecture that satisfies the requirement is to move execution onto the
machine that stays running.

The browser keeps exactly one critical job: describe what the user asked for
and obtain a durable admission. After that it is an observer.

---

## 2. What runs where

```
  browser            Forge (minipaint_neo)              WanGP child (the plugin)
  ───────            ─────────────────────              ────────────────────────
  compose a          outbox.submit  ── durable ack ──▶  (nothing yet)
  request,             │
  get an ack           │  executor.step(), one job at a time
  ──────────────▶      ├─ ENHANCING      mc_llm_api feed
                       ├─ ENHANCED       B22 handover
                       ├─ ENSURING_WANGP runtime.start() ──────▶ child starts
                       ├─ COMPOSING      control.compose() ────▶ compose.py
                       ├─ WAITING_FOR_CARD control.hello() ───▶ control.py
                       ├─ SUBMITTING     control.submit() ────▶ execution.py
                       │                                        ledger.reserve()
                       │                                        gen["inline_queue"]
                       │                                        service.command(...)
                       └─ WANGP_*        control.status() ────▶ ledger + queue
  ◀── events ─────   events.publish()
```

Nothing on the left is required for anything on the right to finish.

---

## 3. The one decision that is not obvious

**Submission goes into WanGP's own queue, not beside it.**

Wan2GP publishes a Python API whose session can be constructed with no Gradio
object. It is the obvious adapter for unattended work; the specification
chose it; and it is wrong.

A session constructed that way allocates a **private `gen` dict**. Every
per-state guard in Wan2GP keys on the *shared* one:

* the generation arbiter, so a headless run and a WebUI run proceed
  concurrently against one card, one set of module globals and one model
  cache that either may free mid-inference;
* the GPU-resource acquire;
* and — reachably, from a button a user can press — the guard on **Force
  Unload Models from RAM**, which reads the browser's dict, sees nothing
  happening, and frees the model out from under an unattended generation.

That hazard cannot be closed from a plugin. Checking a flag before submitting
closes exactly one of the two arrival orders, and the flag it would check has
four unconditional writers, no owner and no nesting count — its headless
clear runs *outside* the lock that guards the run, so it under-reports even
for two runs of the same kind. Nothing between a user's Generate press and
`generate_media` consults anything a plugin can own.

So the bridge does not build an exclusion. It does what Wan2GP's own Deepy
integration does: leaves the task in the shared queue and asks the one
process-wide service to look. The service holds a mutation lock, starts at
most one worker, and returns the existing worker rather than a second one.

| arrival order | what happens |
| --- | --- |
| user first, MiniPaint second | no second worker starts; the running one drains our task from the same list |
| MiniPaint first, user second | the user's own chain reaches the same `start_generation` and observes the existing worker |

No new lock, no new flag, nothing to release on cancel, nothing upstream to
change. And because the task runs on the shared dict, an unattended job is
**visible in the WanGP tab's queue**, shows in its progress display, is
cancellable there, and every guard keyed on that dict starts protecting it.

Three defects in that path are handled explicitly, because they are real:

* **The stranded-task window.** The worker clears its handle only after its
  `finally` has finalised, synced and unloaded — seconds — and during that
  window `start_generation` hands back a dying worker whose drain loop has
  exited. A task landing there is merged and never runs. `execution.py`
  re-triggers boundedly and idempotently, and reports a task it cannot get
  moving rather than leaving it pending forever.
* **Append, never splice.** The wrapper Wan2GP offers plugins forces
  `priority=True` and splices at index 1, ahead of everything the user has
  queued — which satisfies the letter of "never pre-empt the user" while
  inverting it. Unattended work goes on the end.
* **Only signals that cannot go stale.** Queue membership, the service's
  worker handle, `gen["in_progress"]`. Never `main_process_running`: it is
  set one line before a call that can raise and cleared two branches later,
  so one exception leaks it `True` for the life of the process.

---

## 4. Where the settings come from

A job runs at *the settings the user configured*, not factory defaults. The
obvious seam — the live Gradio session — cannot be the mechanism:

* a job may be admitted with the WanGP child **stopped**, because cold start
  is a server-side stage that happens after admission, so there may be no
  process, no session and nothing to ask;
* a server-side caller that asks anyway gets a *different* session, whose
  dict has no stored settings for the model, so the canonical getter returns
  `None` and the preferred variant assigns a key on that `None` and **raises**;
* and reading **mutates**: it takes the session's stored dict by reference,
  sets a key on it and hands it to a normaliser that pops keys and rewrites
  the LoRA list, so composing would change what the user's own tab generates
  next.

Wan2GP already keeps what is wanted, process-wide: `save_inputs` records a
snapshot of the committed form per model beside the session copy, shared
across sessions. It is not factory defaults — it is what the user last
committed — and reading it needs no session, mutates nothing, and works when
no page has ever been open.

Order: **recorded form → a named live session (optimisation only) → the
model's defaults**. The third is recorded on the job and shown in the queue
with a "default settings" badge, because a job that silently ran at settings
nobody chose is the failure this whole path exists to prevent.

That third source is *not* factory values in the usual case, and the badge
used to overstate it. `get_default_settings` reads the model's own
`<model_type>_settings.json` — the file WanGP's **Save Settings** button
writes — and only synthesises factory values when no such file exists. So a
job that falls all the way through still runs at the user's saved defaults
for that model, which is also why a genuinely cold child (Forge just started,
the WanGP tab never opened, nothing recorded) is far less bad than it sounds.

### Flushing: the settings that were on screen

The recorded form is only written when the user *commits* one. Wan2GP fires
`save_inputs(target="state")` — which ends in
`service.record_model_form(...)` — from pressing Generate, from Add to Queue
inside WanGP, from applying or saving a LoRA set, from switching model, from
switching image-mode tab, from loading a settings file, and from a handful of
other actions. It does **not** fire on an ordinary widget change; the one
blanket auto-save on tab-switch is commented out in `wgp.py`.

So a LoRA weight dragged and then left alone lives in the browser and nowhere
else, and a press from the Clipboard tab would quietly compose at the previous
value. The only place those values exist is the page, so that is where the gap
is closed — but by **writing, not reading**:

```
browser              bridge                     Wan2GP
  │  flush ─────────▶ writes save_form_trigger
  │                                        └─▶ validate_wizard_prompt
  │                                            save_inputs(target="state")
  │                                            service.record_model_form(...)
  │  probe ─────────▶ fingerprint of load_model_form(model_type)
  │  …until it moves, or the budget runs out
  │  submit ────────▶ outbox
                       └─▶ compose reads the record, now current
```

The alternative — put the whole form on the bridge event's inputs and read the
values back — works and is the worse trade. It is about ninety components in
`save_inputs`' signature order, this bridge would then own a copy of that
contract, and one insertion upstream would have it reading the reference list
as the prompt type with nothing looking wrong. That is the hazard
`state_components` already warns about at eighteen. Writing one trigger is one
component of coupling, and WanGP commits its own form with its own full input
list.

Two things make it safe rather than merely clever:

* **A probe never writes.** The commit happens after the call that asked for
  it returns, so a flush cannot observe its own result; the caller polls a
  fingerprint of the recorded form instead. A poll that wrote would restart
  the wait it exists to end.
* **`ignore_save_form` is left alone.** It is a one-shot suppression a
  settings load sets so the model switch behind it cannot clobber what was
  just loaded, and `save_inputs` pops it. A flush that popped it first would
  let that clobber happen and make it look like the user's own press did it,
  so a page carrying the flag is refused (`suppressed`) rather than flushed.

It is an **optimisation and never a rule**. No WanGP tab, a bridge without the
trigger, a page mid settings-load, or a wait that runs out all mean the job
composes from the recorded form — which is exactly what it did before any of
this existed. The job records what the page managed (`settings_flush`) and
compose turns that into the base's `source`, so `flushed_form` and
`recorded_form` can be told apart by a reader of the queue even though both
came out of `load_model_form`.

The one thing it cannot do is work with no page at all: a truly cold start has
no Gradio session, so there is no live form to commit and the saved defaults
above are what the job gets.

Two things the job owns and the base does not: the prompt, and `client_id`.
The base carries the *composing page's* client id, WanGP routes a
generation's returned artifacts by that key, and an adapter that did not
overwrite it would hand this job's files to somebody else's request.

The media flags are **recomputed**, not inherited. Wan2GP's own back-fill
only adds flags implied by media that is present and never removes one whose
media is gone, so a base that said "start and end frame" and a job supplying
only a start frame would reach generation claiming a last frame it does not
have.

---

## 5. The transport

Forge keeps a second loopback port before it launches the child and exports
it, exactly as it does the Gradio port. The child's plugin binds it and
answers six operations: `hello`, `compose`, `submit`, `status`, `cancel`,
`forget`.

Four rules:

* **Its own port, not WanGP's app.** Wan2GP's plugin API offers no hook at
  which the FastAPI application exists; depending on Gradio internals and on
  when `launch` happens to run would be a worse dependency than a socket.
  The child never picks a number and never announces one, so there is no
  discovery step to get wrong.
* **The credential is compared on every request, before the body is read.**
  Forge has minted a per-launch secret for the child since this integration
  was built and had never used it. Loopback alone was a defensible boundary
  while everything behind it needed a Gradio session hash; the moment a route
  on that socket can start a generation with no session, it stops being one.
* **Nothing blocks on a generation.** `submit` records the ledger entry,
  leaves the task in the queue, asks the service to look, and returns.
* **No credential, no port, no ledger root: no listener.** A control plane
  that listened without a check because the credential was missing would be
  worse than the absence it stands in for.

---

## 6. Never twice

WanGP's submission path has no idempotency key, no deduplication and no
identifier a caller can probe with afterwards. Sending the same `client_id`
twice starts two generations.

So the child keeps a durable ledger keyed by the MiniPaint execution id, and
the order is fixed and is the whole design:

> **write the record, flush it to disk, *then* submit.**

A record that exists with no outcome means "a submission was made and its
result was never seen". That is answered as `unknown` — never as "did not
happen", because the cost of being wrong about that is a second generation on
somebody's card, and never as "finished", because the cost of being wrong
about *that* is a job reported complete that produced nothing.

After a Forge restart:

| the ledger says | what happens |
| --- | --- |
| terminal, written by this child | adopt that outcome |
| still running, same child | re-attach and keep tracking |
| this child never saw it | `EXECUTION_UNKNOWN`, and a person decides |

The last row is the **expected** answer on Windows, where the managed child
is attached to a job object carrying `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` and
is killed with Forge by deliberate design. Proving a generation was lost is
the correct result there; resubmitting it is not. On POSIX the child is
started with `start_new_session=True` and outlives Forge, so the first two
rows are the usual ones.

---

## 7. The pictures

A queued job may wait for a cold model, for a language model to write a
prompt, and — because the card is shared with whatever the user is doing in
the WanGP tab — for somebody else's generation, which takes as long as it
takes. It waits across a Forge restart.

So at admission every image handle is resolved once and copied into a
durable, job-owned object under the handoff root, and that object is
**pinned**: a registry on disk that the sweepers read.

The hazard this is written against is narrower than it first looks, and worth
stating accurately. The six-hour handoff sweep already exempts anything live.
What it does not cover is an *admitted* job holding a staging token, and the
staging area is swept by age at start-up — so the reachable failure is a
**restart** with a queued job whose staged input is older than half an hour,
not a long wait. Nothing deletes an image while Forge keeps running.

The start-up order is the protection: **recovery registers pins before any
sweeper runs.** A sweep that ran first has already deleted the thing the pin
was going to protect.

---

## 8. What was read, and what is still inferred

The specification's own section 14.3 asks a reader to verify its claims
before building on them, and warns that an earlier revision got one wrong "by
reasoning from call graphs instead of reading the session". That warning is
the reason this section exists.

**Dependency revisions the design was written and reviewed against:**

| repository | revision | date |
| --- | --- | --- |
| `deepbeepmeep/Wan2GP` | `cd832e9d676907f0c055f3fafba472f2f61a4c91` | 2026-09-13 |
| `RJSprod/SD-Neo-ModelSwitchRefiner` | `c2ed41747d9ee3ba73ec93dfc20d6a52e351295e` | 2026-09-13 |

The Wan2GP revision is pinned in
`wan2gp_bridge/wan2gp-minipaint-bridge/compatibility.py` as
`WAN2GP_EXECUTION_REVISION`, beside the element ids, under the same rule: when
a Wan2GP release moves the service, the queue dict or the compose seams, that
is the one file to correct.

**Read at source and confirmed** (review response section 5): the WebUI path
does not take the headless generation lock and the headless path does not
check the flag before starting; `submit_minimax` brings a cold runtime to
readiness as part of running a submitted job, so no readiness seam is needed
in ModelSwitchRefiner; no idempotency key exists; the WebUI-backed session is
browser-bound by explicit refusal; model residency turns on
`(model_type, profile, config, reload_needed)` and not on holding a session;
`capabilities()` distinguishes `enabled` / `configured` / `vision`; the
managed child's platform lifetime is as `runtime.py` intends.

**Built here and provable without a real install** — asserted in
`tests/test_wangp_control.py` and `tests/test_clipboard_executor.py`: worker
identity in both arrival orders, the stranded-task window, ledger ordering and
idempotency, the credential check, compose's three sources, media-flag
recomputation, `client_id` substitution, restart reconciliation in all three
outcomes, input pinning across every sweep, and the write discipline that
stops a long stage clobbering a cancel.

**Read at source afterwards, against `deepbeepmeep/Wan2GP` at `3bd5e0b7`**,
when the compose path was questioned directly. Each of these had been an
inference and is now a citation:

| claim | where |
| --- | --- |
| `save_inputs(target="state")` ends in `service.record_model_form(model_type, cleaned_inputs)` | `wgp.py:10625-10629` |
| the reader is `load_model_form`, deep-copied under a lock, needing no session | `shared/deepy/hybrid.py:68-76` |
| `prepare_inputs_dict(target="state")` keeps `activated_loras` and `loras_multipliers` | `wgp.py:9673-9693` |
| the session path is dead on this build — `session_states` does not exist | zero occurrences in `wgp.py` |
| plugins are handed components **by Python variable name**, from the generator tab's entire local scope | `wgp.py:13563,13568` → `shared/utils/plugins.py:1620-1631` |
| `save_form_trigger` is in that scope and its `.change` runs the save chain | `wgp.py:12824`, `wgp.py:13166-13176` |
| writing a trigger component as an event *output* fires its `.change` | WanGP does it itself at `wgp.py:13230-13232` |
| `ignore_save_form` is a one-shot flag set only by settings loads that switch model | `wgp.py:9521,10177,10393`, popped at `10601` |
| the third compose source is the model's **saved** settings file, not factory values | `wgp.py:3243-3252` |

**Still inferences, and what would settle each:**

| claim | how to settle it |
| --- | --- |
| the process-wide service is reachable from a server-side entry point with no browser session | one hour inside the child: obtain the service without a request, enqueue one task, watch one worker run it. **This is the load-bearing unknown** — everything in section 3 depends on it, and `hello` reports `service: false` and refuses server execution if it is not. |
| a flush lands within its budget on a loaded page | press with a WanGP tab open and a slider moved; the job should record `settings_flush: committed` and a `flushed_form` base. The budget is 2.5 s and a miss is not a failure — it composes from the record — so this is a tuning question, not a correctness one. |
| alternating between the shared-queue path and ordinary WebUI use leaves `wgp`'s module state good | alternate several times on a real install, not once |
| `preload_model_policy = "U"` does not fire on this path | check it on the target install; if it does, residency is defeated and the setting is the fix |
| the enhancer/WanGP VRAM handover is needed at all | measure a cold enhanced chain with GPU memory sampled at each stage boundary. WanGP-then-enhancer is the protected direction; enhancer-then-WanGP is not, and that is the boundary `enhance.release_runtime()` targets |
| ForgeCanvas accepts a same-origin URL where it accepts a data URL | prototype it; until then `minipaint_display_objects` stays off and the canvas keeps embedding copies |

---

## 9. What is deliberately not promised

* **Survival of a Forge restart.** Durability means browser-independent
  execution *while Forge is running*: close the tab, lock the phone, walk
  away, and the job still runs. It does not mean the queue outlives Forge.

  This was the other way round, and the reversal came from use. The queue was
  durable across restarts and reconciled against the child's ledger, and what
  that produced was a restart bringing back prompts from hours earlier,
  running them again, and holding up the ones just pressed. A queue that
  resurrects work nobody asked for again is worse than one that forgets work
  they did, so a run now starts empty and a finished job leaves the list.

  What that gives up, stated rather than discovered: a generation in flight
  when Forge stops is no longer tracked. It is not cancelled — on POSIX the
  child outlives Forge and finishes what it was given, and the output lands
  in WanGP's own gallery — but this extension no longer has a record of it,
  and `recover()` no longer reconciles anything. Press it again if you want
  it queued again, which is what the old `EXECUTION_UNKNOWN` outcome left you
  doing anyway.
* **A viewer.** Success is a `GenerationResult` and persisted generated-file
  paths. The paths are kept on the job for a later viewer; they never cross
  to a browser on the shared event stream, where only the count goes.
* **Parallelism with the user's own WanGP work.** One card, one generation.
  A job waits, visibly, and the wait can be as long as the user's own run.

---

## 10. The test suite

```
pip install -r tests/requirements.txt
python tests/run.py
```

Four packages, no torch, under two minutes. The runner used to hide eleven of
nineteen suites behind one package imported lazily *inside* a check function —
the exception escaped `module.run()` and aborted the whole run, which is worse
than either skipping or failing because the output still looked like a
finished run. Fixed, and the dependency set is written down.
