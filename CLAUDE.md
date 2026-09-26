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
python tests/run.py                    # 30 suites; every one must run
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
now lives in the auto-TLS extension (HTTP/2, see below). The transport breaker
that shed the iframe when the event spine went silent was removed with the
spine in PR #102; what notices a WanGP page that stopped answering now is the
heartbeat (below), which uses no connection at all. The whole incident is
`docs/wangp/BROWSER_CONNECTION_STARVATION_2026-09-17.txt`.

**Under HTTP/2 a stalled request stalls the page, and a press adds another.**
With the auto-TLS extension's HTTP/2, a page has ONE connection to Forge
(Hypercorn), and every request, stream and reload rides it. On 2026-09-23,
after the tab had been in the background for an hour, that connection
stalled: eleven presses of the gallery's Send to WanGP made eleven
unbounded requests that never reached the server, no popup opened, the page
would not reload, and the only message was eleven NetworkErrors when the
reload cancelled them - after which Forge answered at once. So every request
on the direct route (`stageAndOpen`) has a deadline and is aborted when it
misses it, a second press waits for the first instead of adding a request,
one retry follows a stall, and a request cancelled by a reload is not a
failure. The server writes `stage: received N bytes` on arrival, so a send
that never reached Forge shows as that line's absence, and the page logs the
protocol it arrived over (`page over h2`). A request without a deadline is a
bug here even when "it always answers".

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

**A theme's rule about `button` reaches the Clipboard tiles.** Every tile in
the thumbnail grid is a `<button>`, and the Lobe theme says
`button { min-width: fit-content !important }` about every button on the
page. That beat the stylesheet's `min-width: 0`, made each tile as wide as
its own one-line caption, and ran it out of its cell under the next tile:
pictures centred in boxes two columns wide, a selection border the width of
two tiles. Three earlier fixes centred the picture inside the tile and none
could reach the tile's own box, because no stylesheet rule beats a theme's
`!important` on the element. The bundle now writes every geometric
declaration of the grid, the tile, its box, its picture and its caption
inline and marked `!important` (`pin` in `browser/minipaint_clipboard.js`),
which nothing in the cascade can override; `style.css` keeps the colours and
the readable copy of the same geometry, and `tests/browser_clipboard.py`
puts the theme's rule on its page to prove the tiles hold. Do not fix this
grid with a stylesheet rule again; if it is ever reported once more,
`reportTiles` puts the winning declaration in the journal.

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

**A frame in a hidden tab is a document the browser does not render.** Gradio
switches an unselected tab's panel off with `display: none`; a document inside
a box that does not exist gets no animation frames in Firefox, and Gradio 5
inside the WanGP page dispatches its events and applies its updates inside
animation frames. On 2026-09-25 the WanGP view "froze" - clicks did nothing,
progress and results stopped arriving - while the server, the proxy and the
bridge's own requests through that very page all kept working: the page had
spent hours in a hidden Forge tab between uses, and every return was a
`display:none -> block` relayout of the whole WanGP page. The bridge's frame
timer keeps the *bridge's* requests moving through that; it never kept all of
WanGP moving. So the WanGP panel is never `display: none`: `style.css` parks it
instead - keyed on the very inline style Gradio writes, a laid-out box, fixed in
the viewport, `visibility: hidden`, untouchable - and `browser/minipaint_wangp.js`
keeps the panel's width across the switch, fits the frame for where the panel
will be, and takes "on screen" to mean the tab is selected. What that bought was
less than claimed: Chromium keeps a parked frame's animation frames flowing
(`tests/browser_intercept.py` measures it), but the host's Firefox throttles a
document whose embedder is `visibility: hidden` - the journal's `tab:` lines
counted 4 frames by the browser in 33 minutes parked, 16119 by the bridge's
timer - and the freeze came back after 17 seconds away. What the parking gives
everywhere is geometry (no relayout on return, the width kept) and those frame
counts, which are the measurement that settled it. Do not move the iframe in
the DOM (a moved iframe reloads its document), and if `opacity: 0` is ever
tried in place of the hidden visibility, the sibling assistant's `visible()`
must be taught about opacity first, or its focus mode takes the parked panel
for the workspace on screen. `docs/wangp/PARKED_PANEL_2026-09-25.txt` has the
log evidence and what the freeze turned out to need instead.

**Gradio deletes a page's state after its heartbeat drops, and never reopens it.**
Gradio 5 (WanGP pins 5.29.0) keeps one `/heartbeat/<session>` stream per page, marks the
session closed the moment that stream ends for any reason, never marks it open again when
the browser's EventSource reconnects a second later, and a task running every second
deletes a closed session's `gr.State` once it is more than an hour old
(`STATE_TTL_WHEN_CLOSED`). WanGP writes its `state` at page load (`main.load → fill_inputs`
returns it), so a page older than an hour loses it at the first blip: every WanGP handler
then runs on a fresh copy of the build-time state whose `gen` is not the shared record -
queue and progress blind, presses doing nothing - while the bridge's own events, which
never touch that state, keep answering, and only a reload (a new session) brings it back.
That is what "the WanGP frame froze" was on 2026-09-25: both logs showed the bridge's saves
answered in 60 ms and no heartbeat miss, and both pages were hours old. Standalone WanGP's
heartbeat is a localhost connection that never blips; embedded, it rides Forge's HTTP/2
connection through Hypercorn and this proxy - the connection that stalled on 09-23. Bridge
1.8.0's `session_guard.py` wraps Gradio's heartbeat route (a reconnect reopens the session,
every beat is remembered) and `state_holder.delete_state` (a session heard within
`GRACE_SECONDS` is never expired), marks the page's state on every hello, and reports
`session: {closed, reset, silent_s}` on every answer; the parent shows *WanGP's page lost its
session* and reloads within the heartbeat's bounds, and asks once with a probe on every
return to the tab. The reopen alone would not be enough: the expiry runs every second and
the reconnect takes three, so the guard on the expiry is the part that matters. Do not read
`is_closed` as "the tab is gone", and never write a session hash to the console.

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

**`loading="lazy"` does nothing for a `<video>`, and `#t=1` is not metadata.**
The outputs strip built one `<video preload="metadata" src="...#t=1">` per item
for a page of sixty: to paint that frame the browser fetches the header,
range-requests the keyframe and starts a decoder, sixty times, against the six
connections this origin has. That is what made the stage player choppy - the
video was not slow to decode, it was slow to arrive. Tiles keep their address
in `data-src` and an IntersectionObserver attaches it near the strip's window
and removes it well past, with a `load()` after the removal because a `<video>`
holds its decoder until told to look again.
`docs/OUTPUTS_PLAYBACK_2026-09-22.txt` has the measurements.

**A blocking read in an `async def` route blocks every other request.** Both
file routes of the Clipboard tab were coroutines reading files - 4 MB video
chunks, Pillow thumbnail encodes - on the event loop, which on this server is
the rest of the gallery, Forge's streams and the interop spine. Starlette runs
a plain `def` endpoint in a threadpool, so the signature is the fix; the test
asserts the signature because here it is the behaviour.

**A steady-state assertion cannot see a transient cost.** The check for the
lazy strip counted tiles holding a `src` shortly after the strip was drawn, and
passed with the defect put back, because the far observer strips the address
off either way. What differs is the burst of requests made getting there, so
the check counts requests on the wire: forty of forty with the defect, twelve
with the fix.

**Loading a source puts a video's rate back.** The media load algorithm sets
`playbackRate` to `defaultPlaybackRate`, so a rate set before `src` lasts until the
file arrives - and the `ratechange` that reset fires is not a preference. The
View Outputs player sets both and saves its three preferences only from a press.
`tests/browser_clipboard.py` caught it on the second video, which is the only
place it shows.

**Chromium has no `fastSeek`.** Firefox (LibreWolf, the host's browser) and Safari
have it, and the player uses it while the timeline is held and an exact seek
where it is let go. In Chromium the two paths are the same seek, so the check
gives the page a stand-in `fastSeek` that lands on whole seconds; without it the
check passed with the exact seek removed.

**`document.fullscreenElement` is not necessarily yours.** The Forge Assistant's
focus mode makes the whole document full screen, and anything else on the page
can stack an element on top of that. A player that asked "is anything full
screen?" took the page's full screen for its own: its button called
`exitFullscreen()` instead of entering, and closing the view ended the page's -
and the assistant's focus mode with it. Ask whether *your* element is the full
screen one (`inFullscreen()` in the Clipboard's player); only Escape's "leave
full screen first" wants any full screen, because the browser spends that key
on whichever it is. Headless Chromium and its headless shell both grant full
screen to a real press, stacking included, so this is checked for real.

**Playwright's ffmpeg is a build of its own.** It reads piped JPEG frames and
writes VP8 WebM and nothing else, and it wants `-i pipe:0` - `-` is "Protocol
not found". Playwright's Chromium has no H.264, so a WanGP-shaped MP4 does not
play in the browser suites at all; `_real_clip` makes the WebM the player's
checks watch.

**A percentage height inside a Gradio container resolves to auto.** The WanGP
frame was given `height: 100%` of a container the stylesheet had correctly
sized to the window; Gradio wraps raw markup in containers of its own, those
have auto height, and the frame sat at its inline `min-height` floor with the
difference showing as a void. Heights that have to be exact are measured and
written as pixels on the element itself, with nothing styled in between - see
`docs/OVERLAY_AND_TAB_HEIGHT_2026-09-22.txt` §5. Layout like this is settled in
a real browser; a Node stub has no layout and every source check passed while
the page was wrong.

**Never observe the box you resize.** The same sizer watched the column and
wrote a custom property on the column, which is a ResizeObserver loop waiting
for a layout that oscillates. It watches only boxes it never sizes, and writes
only when the value changed.

**A source-text check cannot tell a function that is written from one that is
called.** The WanGP tab's height checks were written against the bundle's text
and passed with the observer replaced by `if (false)` (the word was still in the
comment above it) and again with the call removed from the boot sequence (the
line was still in the file). `_fit()` in `tests/test_wangp_receiver_contract.py`
runs the bundle against a page whose column can be moved and asserts the numbers
it computes; five mutations that survived the text checks fail against it. While
writing it: the bundle's auth probe writes a journal line after an async turn, so
the scenario's JSON was not the last line of stdout and the entire check block
was skipped in silence - the harness silences every console method and ends the
process on the write.

**Every check has a mutation that must break it.** `_RECOVERY_MUTATIONS` in
`tests/test_wangp_protocol.py` reverts one decision per entry and names the
check that must then fail; an anchor that no longer matches the live source is
itself a failure. Moving a line means updating its anchor in the same commit.

**Nothing of ours is held open.** Every incident that locked the page up came
back to connections that were open as far as the page could tell and silent.
The page holds no live connection to Forge at all: the event spine is not
opened (the `/events` route stays, unused, for pages loaded before), the
Clipboard grid and history are read when opened, after the page changes them
and on Refresh, and `enqueue()` answers a server job the moment the server has
it. A job the server cannot run unattended is failed with a sentence, not
handed back to a page nothing would tell. `sync` is bounded
(`SYNC_TIMEOUT_MS`): an unbounded one stayed "in flight" for the life of the
page and every later snapshot queued behind it. A live connection is for a
known boundary - something is coming and will finish - and nothing in this
extension has one; the WanGP iframe is the one exception, and it is WanGP's.

**A snapshot nobody takes tells nobody.** PR #102 made every snapshot one a
page asks for, and the only thing that asked was a return from the background
- through a listener that only a snapshot installed. So no page ever took one:
the WanGP tab was never told that jobs are built from its settings, never kept
WanGP's record current, and for two days a LoRA weight changed in the WanGP tab
never reached a job sent from anywhere else. The interop bundle now takes one
bounded snapshot when it loads, and the Canvas surface loads it at startup
right after the WanGP bundle; a WanGP bundle that loads second reads the flag
from `snapshotState()`. A behaviour that only happens "on return" needs
something that makes the first one happen.

**WanGP saves only what is committed, and a send cannot wait on a poll.** A job
composes from the form WanGP last *recorded*, and WanGP records it only when
the form is committed. So the WanGP page reports touches (`FORM_CHANGED`, only
`isTrusted` events - the bridge writes its own request box with synthetic
input events, and WanGP's model loads change the form by script, and either
would otherwise be a save loop or a save of a half-loaded form) and the parent
saves about a second after the last one. The count of notices a save covered
is what decides whether the record is current: a save that began before the
last touch is not the save of it. `saveForSend` answers at once when the record
is current and otherwise waits at most two seconds - a poll that has nothing to
find spends its whole budget, so it must not be the common case. A bridge
without `form_watch` is never taken at its word.

**The heartbeat cannot see WanGP's own connection.** `PING` is answered by the
script in the WanGP document itself, on the spot and never through Gradio, and
that document shares this page's thread: silence means no document there able
to answer (reloaded and not back, an error page, a lost script), and a late
answer means this whole page was busy, which the miss line says. A stall in
WanGP's own Gradio channel leaves the page answering; the `PONG` carries how
long a bridge request has waited and the journal says so once, but nothing acts
on it, because a generation can hold a request for minutes. Time away (tab off
screen, page hidden) is never silence, a loading document gets a minute, and
Dismiss ends the bar, not the episode - the journal claimed a bar was showing
when the first version let the next beat start a new episode behind it.

**A freeze usually arrives at a page that is already hidden.** The lifecycle
journal records hidden and frozen separately; the first version dropped the
freeze because the page was already "away", so no log could say whether the
browser had frozen it. A thaw is not a return to the screen either.

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

`RJSprod/SD-Neo-ModelSwitchRefiner` also draws a floating assistant panel over
every workspace and has a focus mode that gives one workspace the whole window.
Two things are shared with it, and both are written down in
`docs/OVERLAY_AND_TAB_HEIGHT_2026-09-22.txt`:

**A dialog layer, `--minipaint-dialog-layer: 2000`.** Everything that extension
draws is below it (1100 for a focused workspace, 1200 for the panel) and it has
a check saying so. The Send to WanGP popup used to draw at 60, which put it
under both of them *and* under this extension's own canvas focus mode - the
report was "I cannot open this menu in focus mode", and it had been opening all
along. Moving either number means moving the check in the other repository.

**An event, `minipaint:overlay`.** The popup dispatches it on `document` with
`{name, open, modal}` when it takes the page and when it gives it back, and the
assistant puts its panel away for it. Fire and forget: nothing waits for a
listener, and a page with none behaves as it always did. A replacement picture
is the same overlay and is announced once.

A third thing is shared since 2026-09-25, and it is a reading rather than a
contract: the assistant's `visible()` (its `forge_assistant_host.js`) takes a
panel whose computed visibility is hidden as not showing, and that is what
keeps a parked WanGP panel - a rendered box, `visibility: hidden` - from being
mistaken for the workspace on screen. The parked rule in `style.css` says so;
a change to either side has to keep the other true.

`RJSprod/SD-Neo-ModelSwitchRefiner` runs a local LLM. Its logs showed
`llama-server` taking every CPU core for nine minutes with its model on the
CPU, and its GPU placement pointing at the card WanGP owns. That starves WanGP
while it generates. Since 2026-09-26 that repository reads
`minipaint_neo.wangp.presence.report()` — one dict: the card's UUID, whether
the child is READY, and the bridge's last word on whether it is generating —
and on WanGP's card sizes its server to what WanGP has not needed, stops it
when WanGP grows into a reserve it keeps, and caps its processor threads while
WanGP is up. The contract is in `docs/wangp/CONTRACTS.md` and its keys are held
closed by `tests/test_wangp_presence.py`: a key that goes missing, or a pid
that creeps in, breaks that extension and not this one. `generating` is
three-valued on purpose; `None` is "nobody has said", never `False`.

## The host

Windows, Python 3.13, Forge Neo 2.29, one NVIDIA card, ~96 GB of RAM, launched
with `--listen` on a non-default port behind the auto-TLS certificate. The
browsers that matter are LibreWolf on the host and Via on Android; Via cannot
be configured, which is why per-browser connection limits were never an answer
and HTTP/2 was.

## What is still open

Nothing from the 2026-09-17 incident is unbuilt, but some things have never
been exercised on the user's own machine rather than in tests: the HTTP/2 path
on Windows, and the heartbeat's automatic reload of a WanGP view in anger. If
HTTP/2 misbehaves, `--autotls-http1` puts the old server back with one flag.
The heartbeat's whole state is in `minipaintWanGP.state().heartbeat`, and the
settings save's in `minipaintWanGP.state().settings`; every miss, bar, dismissal
and reload is a `heartbeat:` line in the page journal. Saving as the form
changes and the heartbeat need bridge 1.7.0 installed in WanGP, and the session guard
bridge 1.8.0.
