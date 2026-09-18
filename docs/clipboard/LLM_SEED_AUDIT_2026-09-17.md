# LLM seeds, audited: what is fixed, what is drawn

Written 2026-09-17 against this repository at `claude/llm-seeds-audit-3y1dha`
and against `RJSprod/SD-Neo-ModelSwitchRefiner` at `30a3b27` (2026-09-15),
which is the extension that owns every language model this project touches.

Two facts frame everything below, and one of them contradicts the question as
it was asked:

* **There is no network LLM anywhere in either repository.** The "external
  MiniMax API" is `mc_llm_api`, a Python module imported in-process out of the
  sibling extension's folder — no URL, no port, no token
  (`minipaint_neo/clipboard/enhance.py` module docstring). The only HTTP an LLM
  request makes is to a `llama-server` this machine started on loopback, and
  that is enforced rather than assumed
  (`prompt_master/inference/local_only.py`, checked in `LlamaClient.__init__`).
  Nothing is sent to MiniMax the company. "MiniMax" here names two H3 *model
  definitions in WanGP* that a local model writes prompts for.
* **This repository runs no inference at all.** It has exactly one LLM use
  case — Clipboard prompt enhancement — and it consists of handing a prompt and
  some pictures to `mc_llm_api.submit_minimax()` and waiting.

**The headline:** at the head of both repositories, no user-facing writer pass
runs at a fixed seed. Every one of the five surfaces that writes text draws a
fresh seed per run, the external API included. What *is* fixed is a short list
of passes that are deliberately deterministic, plus one that is fixed by
default and is arguably a bug (§2.3), and three real defects (§3) — one of
which is this repository dropping the seed on the floor entirely.

If you are watching identical output come back, §4 ranks what actually causes
that, and the most likely cause is not a seed at all.

---

## 1. The short answer

| # | Use case | Where the seed comes from | Fixed? |
|---|---|---|---|
| 1 | **Clipboard enhancement** (this repo → external API) | not passed; the API draws one per request | **drawn** |
| 2 | MiniMax H3 panel (LLM Studio) | Seed box, `-1` by default → drawn | **drawn** |
| 3 | Prompt Studio | Seed box, `-1` by default → drawn | **drawn** |
| 4 | Conversation, and voice chat over it | character's seed, `-1` by default → drawn | **drawn** |
| 5 | Krea panel (LLM Studio) | Seed box, `-1` by default → drawn | **drawn** |
| 6 | Creative Mode on the image tab | `stable_hash(creative_seed, "llm")`; the Creative seed is drawn per roll unless pinned | **derived** |
| 7 | Spatial Composer, after a Creative roll | `stable_hash(creative_seed, "spatial")` | **derived** |
| 8 | Spatial Composer, with no Creative roll | `stable_hash(image_seed or 0, "spatial")` — and `0` whenever Forge has not settled a seed yet | **fixed** (§2.3) |
| 9 | Neutralize Prompt | `neutralizer.SEED = 0` | **fixed**, by design |
| 10 | Image caption passes (H3 and Krea) | the enclosing run's seed | irrelevant: temperature 0 |
| 11 | Smart-negative and speech expansion | fall back to the enclosing run's seed | **drawn**, with the run |
| 12 | Writer warm-up / prompt-cache prime | `PRIME_SEED = 1` | **fixed**, output discarded |
| 13 | Managed-model smoke test, installer probes | `seed=1` | **fixed**, output discarded |

A model profile cannot quietly pin any of these: `LlamaClient` filters a
profile's sampler fields against a whitelist that deliberately has no `seed` in
it (`prompt_master/models/managed_profiles.py:101`, applied at
`prompt_master/inference/llama_client.py:36` and `:46`).

---

## 2. Use case by use case

### 2.1 This repository: Clipboard enhancement

`minipaint_neo/clipboard/enhance.py:560` `submit()` assembles everything the
API is given, and the keyword dictionary it builds
(`enhance.py:580`) holds `variant`, `origin`, `remember`, the picture slots the
plan chose, and a `system_prompt` override when one is saved. **There is no
`seed` key**, and `enhance.py:599` calls `submit_minimax()` with exactly those
keywords. The documented call shape in `docs/clipboard/CONTRACTS.md:461` omits
the parameter too, so this is the contract working as written, not a slip.

What that means on the other side (`mc_llm_api.py:176-178`):

```python
resolved = RANDOM_SEED if seed is None else int(seed)
if resolved == RANDOM_SEED:
    resolved = draw_seed()
```

`seed=None` is the sentinel for "draw one", so **every enhancement this
extension asks for runs at a fresh random seed** — `random.randrange(0, 2**31 - 1)`
(`prompt_master/core/models.py:13`). The writer pass runs at temperature 0.6 /
top-p 0.9 (`prompt_master/minimax/enhancer.py:93-94`, which are WanGP's own
`prompt_enhancer_temperature` and `prompt_enhancer_top_p`), so the seed
genuinely changes the answer.

The gap is on our side, and it is about **reproducibility, not randomness**:

* `enhance.py:623` `status()` maps the API's job record into this extension's
  shape and **drops the `seed` field**, although the API publishes it — in
  `status()` (`docs/21-external-llm-api.md:192`) and on both the `started` and
  `done` feed events (`:332`, `:336`).
* The stored job record has no seed either (`outbox.py:1176-1193`, normalised
  at `outbox.py:464-489`).

So a good H3 prompt written for a Clipboard job cannot be asked for again, and
a bad one cannot be reported with the number that produced it. The seed exists,
is drawn, is used, is recorded in LLM Studio's Saved prompts — and is invisible
from the Clipboard tab. §5 says what carrying it would take.

The WanGP generation seed is a separate thing and is not ours: a queue request
may override four fields only, and "anything else on the live page — model,
resolution, length, steps, seed, guidance, LoRAs — is always the page's own"
(`minipaint_neo/wangp/protocol.py:142-144`).

### 2.2 The five writers in the sibling extension

All five resolve `-1` (`RANDOM_SEED`) to a drawn seed before the engine sees
it, and all five let a typed number win:

| Surface | Draws at | Box default |
|---|---|---|
| Prompt Studio | `mc_llm_prompt_panel.py:285` | `RANDOM_SEED` (`:153-155`) |
| Conversation | `mc_llm_chat_panel.py:2031-2033`, applied `:2048` | `RANDOM_SEED` (`:514`; `Character.seed` defaults to it too, `prompt_master/chat/characters.py:81-83`) |
| MiniMax H3 panel | `mc_llm_minimax_panel.py:351-353` | `RANDOM_SEED` (`:142`) |
| Krea panel | `mc_llm_krea_panel.py:475-477` | `RANDOM_SEED` (`:123`) |
| External API | `mc_llm_api.py:176-178` | `seed=None` |

Voice chat is not a sixth surface: `sessions.conversation` has exactly one call
site (`mc_llm_chat_panel.py:2100`), and a spoken reply is that reply read
aloud, so it runs at the character's seed like any other.

Two dataclass defaults look alarming in a grep and are not reached by any
current caller — `PromptRequest.seed = 7` (`prompt_master/core/models.py:63`,
upstream's node default) and `ChatRequest.seed = 0`
(`mc_llm_sessions.py:636`). Both panels always pass a resolved seed. They are
traps for a future caller, not live behaviour. The `"seed": 7` in this
repository's test double (`tests/test_clipboard_enhance.py:122`) is a fake's
constant and reaches nothing.

### 2.3 The one that *is* fixed by default: the Spatial Composer

`mc_spatial.composer_seed_for()` (`mc_spatial.py:167-183`) derives the
Composer's seed from the Creative seed when a Creative roll ran, from the image
seed when Forge has settled one, and otherwise from `NO_CREATIVE_SEED = 0`
(`mc_spatial.py:147`). On the peer path — Smart Spatial on, Creative Mode off,
image seed still `-1` because `before_process` runs before Forge resolves it —
that third branch is the live one, so **every such composition on every machine
runs at `stable_hash(0, "spatial")`, the same constant every time.**

The docstring argues the case: the Composer reconciles a scene with a layout,
"which is a correction rather than a creative draw, and the same scene over the
same boxes *should* reconcile the same way twice". That is a defensible
position, and it is the only pass that is constant while still sampling —
`composer.TEMPERATURE = 0.3`, `TOP_P = 0.9`
(`prompt_master/krea/composer.py:81-82`). At those settings the fixed seed is
doing real work, not just satisfying a required argument. Worth a second look
by that repository, and worth knowing about if you are comparing Smart against
Direct and wondering why the second pass never varies.

### 2.4 Fixed on purpose, and correctly

* **Neutralize Prompt** — `SEED = 0`, with the reason written down:
  "Passed because the client requires one, fixed because it changes nothing.
  Greedy decoding has no draw to seed" (`prompt_master/krea/neutralizer.py:93-97`;
  `TEMPERATURE = 0.0`, `TOP_P = 1.0` at `:81-82`; called from `mc_neutralize.py:125`).
  Copy-editing by deletion has one right answer, and two presses should agree.
* **Caption passes** — the H3 and Krea captioners run at the enclosing run's
  seed (`mc_llm_sessions.py:720-723`, `:842-845`) at temperature 0
  (`prompt_master/minimax/enhancer.py:105-106`,
  `prompt_master/krea/enhancer.py:159-161`), verbatim from WanGP's
  `do_sample=False` caption pass. Identical pictures give identical captions
  whatever the seed is; that is a description, not a draw.
* **Warm-up** — `PRIME_SEED = 1`, one token, result thrown away; it exists to
  put the writer's instruction in llama.cpp's prompt cache
  (`mc_llm_runtime.py:3606`, `:3621-3624`).
* **Smoke test and install probes** — `seed=1`
  (`mc_llm_managed_models.py:1749-1751`,
  `prompt_master/provisioning/installer.py:271-291`). They check that a server
  answers at all.

### 2.5 Derived, which is not the same as fixed

Creative Mode records one number and reproduces two passes from it: the
Director's choices and the writer's seed both derive from the Creative seed by
SHA-256 (`prompt_master/krea/director.py:116-149`), and the roll resolves `-1`
to a drawn number per roll (`:129-145`, `:553-555`). The stored default is
`krea_creative_seed: -1` (`mc_llm_state.py:145`), so nothing repeats unless you
pin it. On the image tab the writer really does run at the derived seed, and
there is a test that says so
(`tests/test_krea_creative.py:646-651`). If you have pinned a Creative seed to
compare settings, every LLM pass behind it is deterministic on purpose — that
is the feature.

---

## 3. Defects found

**3.1 The Krea panel shows a writer seed it does not use.**
`mc_llm_krea_panel.py:363` renders `Creative seed: … · writer seed: {recipe.llm_seed}`,
but the writer is started at the panel's own drawn seed:
`sessions.krea(written_source, found, resolved, …)` (`:498`), and both the
finished notice (`:584`) and the saved session (`:623`) record `resolved`. So
the recipe view names a number nothing ran at, and re-rolling at the recorded
Creative seed reproduces the *direction* but not the prompt — unlike the image
tab, where `mc_creative_krea.py:1153` passes `recipe.llm_seed` and is tested
for it. Either the panel should pass `recipe.llm_seed` when Creative Mode is on
(matching the image tab and making one number enough), or the line should stop
calling it the writer seed. No test covers which seed that panel's writer runs
at. Sibling repository.

**3.2 A seed of 0 cannot be pinned in two of the five surfaces.**
`resolved = int(seed or RANDOM_SEED)` (`mc_llm_minimax_panel.py:351`,
`mc_llm_krea_panel.py:475`) treats a typed `0` as falsy, so it becomes `-1` and
a random seed is drawn instead. Prompt Studio (`mc_llm_prompt_panel.py:261`),
Conversation (`_number`, `mc_llm_chat_panel.py:2238`) and the external API
(`mc_llm_api.py:176`) all accept `0` as a seed. `draw_seed()` can return `0`,
so a run whose seed was reported as `0` cannot be reproduced by typing it back
into those two panels. `is None`/`== RANDOM_SEED` is the test, not truthiness.
Sibling repository.

**3.3 This repository drops the seed.**
Described in §2.1. The consequence is narrow but real: an enhancement cannot be
reproduced, retried at the same seed, or reported with one, and the tab cannot
show what it ran at even though the API offers the number in `status()` and on
two feed events.

---

## 4. If a seed still looks fixed on your machine

In the order I would check them:

1. **Retry reuses the finished prompt — no LLM runs at all.**
   `outbox.py:1550`: a retry of a job whose enhancement reached `done` carries
   the written prompt over rather than asking for it twice, unless the failure
   was `MODEL_CHANGED`. The UI marks it (`ui.py:368`, `reused_from`). Pressing
   Retry and getting a byte-identical prompt is this, working as designed — and
   it is the single most likely reason an enhancement looks seeded to a
   constant from the Clipboard tab. A *new* Add to Queue press writes again.
2. **The pass you are looking at may be temperature 0.** Captions and Neutralize
   are deterministic by construction; their seed changes nothing.
3. **Smart Spatial with Creative Mode off** really is one constant seed — §2.3.
4. **A pinned Creative seed** makes everything behind it repeat, by design.
5. **An older install of the sibling extension.** Prompt Studio's seed box used
   to open on `7` — upstream's node default — and was changed to draw per run in
   `5b5ebda`, 2026-08-27 ("A drop-down that decided what to overwrite, a path
   already known, and a seed of 7"); `docs/07-llm-studio.md` §29.3 is that
   write-up, and `tests/test_llm_panels.py:3247` is the regression test. The
   external API has drawn a seed since the day it landed (`d78e160`,
   2026-09-13). If your Model Chain checkout predates late August, a fixed 7 in
   Prompt Studio is exactly what you would see. **I could not verify which
   commit is installed on your machine** — worth checking before anything else
   here is acted on.
6. **WanGP's own prompt enhancer**, if you have switched it on in the WanGP
   page, is a different enhancer with its own `prompt_enhancer_randomize_seed`
   setting. Clipboard's enhancement does not go through it; it hands WanGP a
   finished prompt.

Where the number is visible today: the panels' finished line (`Complete · Seed: N`),
LLM Studio's Saved prompts, and — for an external request — `status()["seed"]`
plus the `started` and `done` events. Not in the Clipboard tab, per §3.3.

---

## 5. What fixing §3.3 in this repository would take

Not applied here; this document is an audit. For the record, the change is
small and touches five places:

1. `enhance.py:580` — accept an optional seed in `planned` and pass
   `seed=` through to `submit_minimax` when one is set; `None` keeps today's
   draw-per-request behaviour.
2. `enhance.py:623` `status()` — carry `raw.get("seed")` into the returned
   dict as a whole number, beside `elapsed` and `position`.
3. `enhance.py:599` — the `_journal` line may name the seed; a seed is a
   number, not prompt text, so it does not touch the logging-privacy rule
   (`tests/test_logging_privacy.py`).
4. `outbox.py:1176-1193` and `:464-489` — one more field on the enhance
   record, defaulted for records written before the change.
5. `docs/clipboard/CONTRACTS.md:461` and the enhance suite
   (`tests/test_clipboard_enhance.py`) — the call shape is pinned in both, and
   `tests/test_wangp_protocol.py`'s `_RECOVERY_MUTATIONS` anchors must be
   re-checked in the same commit if any anchored line moves.

Whether the tab should also *offer* a seed box, or only record the one the API
drew, is a product decision. Recording it costs nothing and is what makes "this
prompt came out well, do it again" answerable at all.
