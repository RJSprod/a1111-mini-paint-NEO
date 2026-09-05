# Checks

```
python tests/run.py                  # image maths + both frontends + the workflow
python tests/browser_smoke.py        # the real extension in a real browser
```

Nothing in `run.py` starts the WebUI. `tests/stubs/modules` stands in for the
handful of `modules.*` names the extension touches, and `tests/forge_like.py`
builds a page shaped like Forge Neo's `modules/ui.py` — the same component
hooks, the same constructor repair, ForgeCanvas-style hidden textboxes, and
one `gr.Tabs(elem_id="tabs")` around everything.

| file | what it holds the line on |
| --- | --- |
| `test_imaging.py` | the mask is coverage carried as a layer's alpha and comes back unchanged; a padded crop is trimmed, an opaque one is not; smoothing rounds an edge without eating a thin stroke; expansion places the original correctly, masks only what is new, carries an existing mask, refuses a canvas no browser can hold; the inpaint layer is what Forge's threshold reads; the document's stage / commit handshake and its undo history |
| `test_frontends.py` | the Old UI setting exists, is saved, defaults to the touch Canvas and asks for a Reload UI; each frontend mounts alone under the same tab; the receive button lands in each output row right after "send to extras"; every event on the assembled page resolves; send writes the host's img2img, Inpaint (image and mask) and Extras inputs and is followed by the host's tab switch; every step that replaces the editor is stage → flush → commit; a Canvas that throws leaves a working legacy tab **and a working host**; a Gradio without `ImageEditor` picks legacy and says why |
| `test_workflow.py` | receive, crop (including a padded one), undo, redo, mask, send to Inpaint with the mask as alpha, explicit img2img dropping the mask, expand with auto-mask and fill-on-send, clear, invert, reset, the side buttons and snapping, the aspect picker, and a non-editor value (the no-WebGL notice) never crashing a callback |
| `browser_smoke.py` | with WebGL: every top-level tab switches *before* any Canvas feature is touched; receive from a gallery lands in the tab; crop with the mouse and with a finger, Apply, structural Undo; a stroke sent to Inpaint arrives as image + same-size mask and the host switches to the Inpaint sub-tab; Expand adds 128px, masks it, sends opaque pixels; focus mode and Escape. Without WebGL: the editor is not mounted, a notice shows, every tab still switches. Legacy mode: the iframe mounts alone. |

`browser_smoke.py` needs Playwright with a Chromium (`CHROMIUM=/path/to/chrome`
if it is not where Playwright keeps it). Set `FORGE_ROOT` to a Forge Neo checkout
to run with Forge's own `script.js` and `ui.js`; otherwise a few stand-ins
provide `gradioApp`, `switch_to_*` and `extract_image_from_gallery`.

`test_frontends.py` prints one traceback on purpose — it breaks the Canvas to
prove the fallback catches it.
