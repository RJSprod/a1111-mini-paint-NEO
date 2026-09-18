# Feasibility: a fresh seed on every LLM request, by default, everywhere

Companion to `LLM_SEED_AUDIT_2026-09-17.md`. The audit said what is fixed and
what is drawn; this says what it would take to make *drawn* the default
everywhere, without asking anyone to undo a value Forge wrote.

**Verdict: feasible, small, and low risk.** Five line-level edits and four
tests, almost all of them in `RJSprod/SD-Neo-ModelSwitchRefiner`. Nothing is
required in this repository. Critically, **the stale `ui-config.json` entry
does not have to be found, edited or deleted** — the host's own opt-out is
checked before the saved value is applied, so an existing entry goes inert the
moment the flag is set. That is the requirement "blow it away", met without
touching the file.

---

## 1. The mechanism, verified rather than assumed

Three earlier answers in this investigation were wrong because they reasoned
from the extension's source about what a running panel shows. The source is not
where that value comes from. So this section is verified end to end.

**Forge restores every labelled component from `ui-config.json` over the value
the script asked for.** The sibling repository already knows this and already
works around it for a different control — `mc_pipeline_panel.switch()` sets
`do_not_save_to_config = True` on the image-pipeline stage switches, with
`docs/10-image-pipeline.md` §16.3 explaining why.

**The flag suppresses the restore, not just the save.** From A1111's
`modules/ui_loadsave.py`, which Forge inherits:

```python
saved_value = self.ui_settings.get(key, None)

if getattr(obj, 'do_not_save_to_config', False):
    return
```

The early return happens before the saved value is applied to the component, so
a key that is already in the file stops having any effect. **No migration, no
file surgery, no user action.**

**The key is `<accumulated path>/<label>/value`, and the path only grows at
Tabs** — not at Columns or Rows. LLM Studio builds its four panels as sibling
`gr.Column`s inside one tab (`mc_llm_studio.py:221-248`, "Keyed by mode rather
than zipped against MODES"), so every component labelled `Seed` in that tab
**collapses onto a single key**:

| Panel | Control |
|---|---|
| Prompt Studio | `mc_llm_prompt_panel.py:153-155` |
| Conversation | `mc_llm_chat_panel.py:514` |
| MiniMax H3 | `mc_llm_minimax_panel.py:142` |
| Krea | `mc_llm_krea_panel.py:123` |

One stored `7` pins all four. That is the whole explanation of a MiniMax panel
opening on a number its own source has never contained, and it also means a fix
applied to one panel would appear to fix nothing.

A fifth control, `Creative seed` (`mc_creative_panel.py:859-860`), has its own
label and so its own key — same treatment, separate entry.

---

## 2. The work, in the sibling repository

### A. One seed-box factory, with the host's opt-out on it

Mirror `mc_pipeline_panel.switch()` exactly: a single `ui.seed_box()` that every
seed control is built by, which sets `do_not_save_to_config = True` and carries
the docstring explaining why. Five call sites change to use it.

A factory rather than five loose assignments for the reason the switch factory
exists: the next seed control somebody adds inherits the fix, and there is one
place for the test to point at.

*Effort: ~40 lines including the docstring. Risk: none — the attribute is the
host's own, and a host without it simply does not read it.*

### B. Prompt Studio's second, independent pin

Prompt Studio is pinned twice: once by the host, and once by the extension's own
preferences file. `stored.get("seed", RANDOM_SEED)` (`:153-155`, `stored` from
`:53`) reads `prompt_defaults["seed"]`, which `_remember()` rewrites after every
generation (`:376-383`). An install that used the panel before 2026-08-27 has
upstream's `7` in there, written by the old build, and `5b5ebda` changed the
fallback without migrating it.

Two edits: read `RANDOM_SEED` unconditionally, and drop `seed` from the mapping
`_remember` persists. An orphaned key that nothing reads needs no migration.

*Effort: two lines. Risk: somebody who deliberately set a default seed there
loses it — which is the stated intent.*

### C. The falsy-zero bug, in the same pass

`resolved = int(seed or RANDOM_SEED)` (`mc_llm_minimax_panel.py:351`,
`mc_llm_krea_panel.py:475`) treats a typed `0` as "draw one". Same two lines of
code, same review: `RANDOM_SEED if seed is None else int(seed)`. Worth doing
here because after this change a drawn seed is the only way most runs get one,
and `draw_seed()` can return `0` — a run somebody wants to reproduce should not
be the one seed the box refuses to accept.

### D. Conversation's character seeds: leave them

A character's stored seed is a number somebody typed into the flyout, and new
characters already default to `-1` (`prompt_master/chat/characters.py:81-83`).
Blowing those away would discard a real setting rather than a leftover default.
Flagged as a decision, with a recommendation not to.

**Total: half a day including tests and a release note.**

---

## 3. Where "every LLM request" should stop, and why

The request reads naturally as *every request whose output somebody looks at and
which samples*. Four passes are outside that, and changing them costs something
while buying nothing:

| Pass | Recommendation | Reason |
|---|---|---|
| Neutralize | Leave at `SEED = 0` | Greedy (`T=0`): the output cannot change. The fixed number is what keeps two requests for the same source byte-identical, "which is what lets llama.cpp resume the second one from its cache" (`neutralizer.py:93-99`). Randomizing loses the cache hit and changes no text. |
| Caption passes (H3, Krea) | Leave | `T=0`, and they already inherit the run's seed. Deterministic by construction. |
| Warm-up prime, smoke test, install probes | Leave | One token, output discarded. |
| Creative Mode's writer | Leave derived | Already fresh per roll unless a Creative seed is pinned; deriving is what makes one recorded number reproduce a whole roll. |

**One genuine judgment call: the Spatial Composer.** With no Creative roll behind
it and no settled image seed, it runs at `stable_hash(0, "spatial")` — a true
constant, on a pass that samples (`T=0.3`). Under a literal reading of "random
everywhere" it should draw. The cost is that "the same scene over the same boxes
should reconcile the same way twice" stops being true, and a recorded Composer
pass stops being replayable. Recommendation: leave it derived, or have it draw
only when no image seed has settled — but this one is yours to call, and it is
the only pass where the two goals genuinely conflict.

---

## 4. This repository: nothing required

The Clipboard enhancement passes no seed, so `mc_llm_api` draws one per request
(`mc_llm_api.py:176-178`), and there is no Gradio control anywhere in that path —
neither pin can reach it. It is already what this proposal is trying to make
everything else.

One optional add, worth its cost only if the LLM Studio history is going to be
used as the record: carry the API's `seed` through `status()` into the job record
and onto the card (§5 of the audit lists the five touch points), so a Clipboard
enhancement can be matched by number to its entry in Saved prompts.

---

## 5. How it gets verified

* A test per control that `do_not_save_to_config is True`. The sibling repository
  already has this shape twice (`tests/test_pipeline.py:396`,
  `tests/test_krea_neutralizer.py:1255`), so it is a known pattern there.
* A test that a stored `prompt_defaults["seed"]` no longer reaches the box.
* A test that `0` survives as a seed in all four panels.
* `tests/test_llm_panels.py:3247` stays, but a comment belongs on it saying what
  it does **not** cover: it asserts the value the code asks for, which is exactly
  the half the host overrides. Trusting it alone is what let this ship.
* **Manual, and the only check that exercises the real mechanism:** leave the
  stale `7` in `ui-config.json`, restart Forge, open LLM Studio. All four boxes
  must read `-1`. No unit test can cover this, because the behaviour belongs to
  the host.

---

## 6. Risks

* **Low overall.** The flag is the host's own opt-out, already used in this
  codebase, and a host that does not have it ignores an unknown attribute.
* **A future Forge could rename or drop it**, and the boxes would silently go
  back to being restored. The manual check above is the only detector; it belongs
  in the release checklist rather than in the test suite.
* **Anyone who deliberately pinned a seed in a box loses that pin across
  restarts.** That is the intent, and it belongs in the release note. A seed
  typed during a session still wins for that session.
* **Do not sweep `ui-config.json` programmatically.** txt2img and img2img have
  their own controls labelled `Seed`, the key format is the host's private
  detail, and the host rewrites the file itself. The opt-out reaches the same
  end state with none of that exposure.
