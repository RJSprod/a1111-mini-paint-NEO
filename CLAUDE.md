# Working on this extension

Mini Paint NEO is a Forge Neo extension: a touch Canvas, a Clipboard tab, and a
WanGP tab that runs WanGP as a child process behind a reverse proxy on Forge's
own origin. `docs/` holds the design intent for each part and `tests/README.md`
holds the whole testing contract - read those two before changing anything;
this file is only what a session has to know that is written nowhere else,
because it was learned the hard way.

## Run the checks

```
pip install -r tests/requirements.txt
python -m playwright install chromium
python tests/run.py                    # 29 suites; every one must run
```

Gradio is pinned to 4.40.0 because that is what the target Forge ships, and the
event graph differs across majors. `tests/README.md` says why the whole Gradio
ecosystem is pinned rather than Gradio alone.

## The traps

Each of these cost a day or broke something on a user's machine. None of them
is visible from the code that depends on it.

**Six connections to one origin.** A browser allows six persistent HTTP/1.1
connections per origin, and this extension's page spends them on streams that
never close: Forge's heartbeat and queue, the interop event spine, and the
WanGP iframe's own heartbeat and queue through our proxy. On 2026-09-17 a
wedged WanGP held two of them for ever and the whole WebUI froze in the
browser while the server was fine. Never add another always-open stream on
Forge's origin without counting what is already there. The structural cure
now lives in the auto-TLS extension (HTTP/2, see below); the transport breaker
in `browser/minipaint_wangp.js` is what keeps a page alive without it. The
whole incident is `docs/wangp/BROWSER_CONNECTION_STARVATION_2026-09-17.txt`.

**Gradio applies an equal string as no change.** Its frontend updates a
component through Svelte's `safe_not_equal`, so handing an HTML component the
string it already holds leaves the DOM exactly as it was. A repaint whose
purpose is a *new* iframe element must therefore differ from the last one:
`minipaint_neo/wangp/ui.py` stamps those with `paint_token()`. Every other
paint deliberately does not stamp, because a stamped paint reloads the WanGP
page out from under whoever is using it.

**A component of another tab in an outputs list is a silent, total
failure.** Gradio 4.40 postprocesses `gr.skip()` as a property update, and a
property update is resolved against the session's copy of the component -
`SessionState.__getitem__`, which is `blocks[id]`. A component the running
page does not contain raises `KeyError` there, *after the function has run*.
So the work happens, the send log says "sent 1024x832", and the answer never
reaches the page - and every browser step chained behind that event is
chained behind an event that failed, so the tab switch, the host canvas, the
ImageStitch box and the WanGP handover do not happen either. One stale
component from another tab stopped the Canvas sending anywhere at all, for
weeks, with a send log full of successful sends. The Clipboard tab was cured
of it first (`ClipboardTab.send_backend`) and the Canvas on 2026-09-18
(`canvas/ui.py`'s `DELIVER_SEND_JS`): a send now names only its own tab's
boxes and hands the browser a plan, and the one event that may name another
tab's components is a hidden button pressed only when the page has just
found out it cannot place the picture itself. `docs/CANVAS_SEND_2026-09-18.txt`
is the whole story. Do not put another tab's component in an outputs list.

**A chained browser step runs after a failed step, with the box's stale
value.** Gradio 4.40 runs a `.then` step whether or not the step before it
succeeded, and a js-only step reads its inputs off the page, so when the
Canvas's receive event fails (the queue cut, a refused picture) the step that
opens the Send to WanGP popup is handed the hidden box's *previous* handoff -
the last picture's token - and would open the popup on a picture nobody just
pressed. `browser/minipaint_intercept.js` therefore refuses a handoff it has
already opened once, and the direct route (`stageAndOpen`) always freezes a
new one. A test that opens the popup twice must use two handoffs; the Node
harness's `keys` scenario is where that was learned.

**Forge Neo's `extract_image_from_gallery` returns the inputs array.** It
answers a Gradio js function, so it returns `[[item]]` - a one-item gallery
inside a one-element array - where older builds returned `[item]`. The Canvas
wrapped that once more, and Gradio 4.40 refused the nested gallery payload
*before* `receive()` ran: nothing on the server saw the press, nothing was
logged anywhere, and the only visible thing was the chained tab switch (the
trap above). `pickGalleryImage` now unwraps either shape, and the browser
suites carry the Neo-shaped helper in their page head
(`browser_clipboard.FORGE_GALLERY_HELPERS`); a page without it exercises a
helper no real Forge has. The WanGP destination no longer rides that event
at all: `browser/minipaint_clipboard.js` takes the button over in the page
(`onGalleryButton`) and finishes the press over plain HTTP, the way the
Clipboard tab's own sends work, and the server's WanGP branch in `receive()`
is only the fallback for a page without that bundle.

**A Gradio gallery that is holding a picture has no upload input.** Gradio 4
swaps a gallery's drop zone for its thumbnails as soon as it has one, so a
component the browser wrote perfectly well a moment ago classifies as
`unsupported` on the next send. Its Clear button puts the drop zone back,
which is what `set_image_file`'s `replace` option does in
`miniPaint/src/js/libs/webui-host.js`. A gallery also keeps its pictures
*inside buttons* where an ordinary `gr.Image` keeps its one outside them, so
"has it taken the picture yet" is a different question for each - that is
what `accept_more` is for, and why the plan says which kind a destination is.

**A cyclic `gr.State` in an outputs list is a RecursionError.** Gradio 4.40
walks state components among a dependency's outputs and hashes them *before*
calling the function, and that walk has no cycle detection and no depth limit.
WanGP's own per-page state refers back to itself. The WanGP tab's `painted`
list has no stateful component in it for exactly this reason, and the browser's
iframe repair leans on that immunity. The comment above `painted` says so; do
not treat it as decoration.

**The WanGP tab is painted once, when Forge builds the UI.** Nothing repaints
it until something presses one of its buttons, so a page loaded an hour later
showed "Start WanGP" over a WanGP that had been serving all along. The browser
now presses the hidden Refresh once at boot when it finds the root with no
iframe, and again whenever a runtime frame on the event spine disagrees with
what the tab shows. `Runtime._announce()` is what publishes those frames.

**A silent stream is the only free signal a page gets.** Forge heartbeats every
fifteen seconds on both of its streams, so silence never means "nothing to
say". `minipaint_neo/wangp/proxy.py` therefore bounds a proxied stream's idle
time (`STREAM_IDLE_TIMEOUT`), and `browser/minipaint_interop.js` says `silent`,
then `answered` if one plain request comes back. A request that never comes
back says nothing at all, and that absence is what the WanGP tab acts on. An
unanswered request is not a failed one; do not collapse the two.

**Windows needs its own kill.** The emergency restart's escalation was a
POSIX-only branch that silently did nothing on Windows, so a child that
survived the job object was reported and left running. `runtime._force_exit()`
now ends a pid the platform's way on both. A process that survives that is
stuck inside a driver call and nothing in this extension can end it - the
report says so rather than blaming itself.

**Playwright route handlers only run while the test thread is inside a
Playwright call.** A `time.sleep()` while waiting for a held request waits for
ever, because nothing pumps the connection; use `page.wait_for_timeout()`.
Answer every held route before `page.unroute()`, or the handler dies as a
cancelled future.

**The Node DOM harness has two sharp edges.** A bare `new MutationObserver`
resolves to the *global*, not to the stub window's, so `load()` sets both. And
journal lines are captured through a `console.debug` hook rather than the log
route, because that route only ever carries the last two dozen lines and a long
scenario pushes out the line being asserted.

**Every check has a mutation that must break it.** `_RECOVERY_MUTATIONS` in
`tests/test_wangp_protocol.py` reverts one decision per entry and names the
check that must then fail; an anchor that no longer matches the live source is
itself a failure. Moving a line means updating its anchor in the same commit.

**`admission` is three-valued.** An unconfirmed queue request omits the field
rather than adding a fourth value, because the protocol test holds that
vocabulary closed.

## The sibling repositories

`RJSprod/NEO-webui-auto-tls-https` gives Forge its certificate and, since
2026-09-17, serves it over **HTTP/2 through Hypercorn**, which is what removes
the six-connection limit for everything on Forge's origin. Two things to know
about it: only Hypercorn's public `serve()`/`Config` API is safe across
releases (0.13, which the old `certipie` dependency pinned into venvs, has
none of the internals), and its installer upgrades anything below 0.17.

`RJSprod/SD-Neo-ModelSwitchRefiner` runs a local LLM. Its logs showed
`llama-server` taking every CPU core for nine minutes with its model on the
CPU, and its GPU placement pointing at the card WanGP owns. That starves WanGP
while it generates. Capping its threads below the core count, keeping it off
WanGP's card, and not running a CPU model during a video generation are that
repository's to fix; nothing here can.

## The host

Windows, Python 3.13, Forge Neo 2.29, one NVIDIA card, ~96 GB of RAM, launched
with `--listen` on a non-default port behind the auto-TLS certificate. The
browsers that matter are LibreWolf on the host and Via on Android; Via cannot
be configured, which is why per-browser connection limits were never an answer
and HTTP/2 was.

## What is still open

Nothing from the 2026-09-17 incident is unbuilt, but two things have never
been exercised on the user's own machine rather than in tests: the HTTP/2 path
on Windows, and the transport breaker actually shedding an iframe in anger. If
either misbehaves, `--autotls-http1` puts the old server back with one flag,
and the breaker's whole state is in `minipaintWanGP.state().transport`.
