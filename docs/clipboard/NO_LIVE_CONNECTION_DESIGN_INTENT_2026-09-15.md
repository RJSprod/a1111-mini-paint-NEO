# The Clipboard tab without a live connection

One design intent for the whole programme: move this tab off Gradio's event
transport, so the *"This page has lost its live connection to Forge"* line
stops being something a user ever sees on a machine where Forge is running.

Written 2026-09-15, after the send fault was found. Supersedes and absorbs
`GRID_DESIGN_INTENT_2026-09-15.md`, which covered only the first step.

Every number here was measured on this repository. Where a figure decides
something, the measurement is described so it can be repeated.

---

## 0. How we got here, in four lines

A user could not send a picture to img2img. Five builds guessed at causes and
fixed plausible things that were not wrong. The sixth added diagnostics
instead, and the page named the fault the first time it was asked: the send
event listed, among its outputs, a component no page contained, and Gradio
will not run such an event — silently, for ever, with no error anywhere.

That is fixed. This document is about the thing it exposed: **how much of this
tab depends on a transport whose failures are invisible.**

---

## 1. What "live" actually means here

Three different things get called "the connection", and conflating them is
what made this hard to reason about.

**Push** — the server telling a page something it did not ask about: a job
finished, another page added a picture. This genuinely needs a channel held
open. **In this extension it already is one, and it is not Gradio:**
`/minipaint/events` is server-sent events over plain HTTP, with
`/minipaint/sync` for a snapshot and a resumable cursor namespaced by process
epoch. The WanGP queue has used it since protocol 3.

**Request and response** — press a thing, get an answer. Needs nothing held
open. An HTTP request does it.

**Server-rendered UI** — the server deciding what the page *displays*. The only
one of the three that forces Gradio, because the value of a Gradio component
can only be set by a Gradio event.

### Counted on this tab: all 38 of its Gradio events are the second kind

Not one is a push. Every one is a click, an input, an upload or a blur that
returns a value to the page that asked for it.

So **this page does not need anything live.** It needs Gradio — and Gradio's
request and response travel over a queue-and-SSE transport that dies when the
tab is backgrounded, when a session is forgotten, and when Forge restarts. The
fragility belongs to the transport, not to the interaction.

And it needs Gradio **only where we let the server render**. Every
server-rendered component is a purchased dependency on that transport.

> **The thesis of this document:** stop buying that dependency, except where
> something genuinely cannot be had any other way.

### The work is already durable; only the window onto it is not

`outbox.chosen_executor()` returns server execution unless unattended
execution is switched off in settings, or this WanGP build has *explicitly*
said it cannot run unattended jobs — and silence is not a refusal. From
`clipboard/executor.py`:

> the outbox is work Forge owns, and the browser's only remaining job is to
> describe what the user wants and get a durable acknowledgement before it
> disappears.

So *submit a request, close the browser, have it complete* already works. What
does not survive is the **view** of it: the queue list is Gradio-rendered, so a
page that comes back needs Gradio to show the job that ran fine without it.

---

## 2. Principles

Five rules that decide where any piece of this tab belongs. Everything in
sections 5–13 follows from them.

1. **The server owns truth. The browser owns drawing.** The server decides
   what exists and in what order; the browser decides what a thing looks like
   and which things are on screen. The browser never holds an opinion the
   server cannot overrule.

2. **Push is advisory; a fetch is authoritative.** An event says "something
   moved", never what it moved to. A page that sees one re-asks. This is the
   event spine's own first rule and it means a missed event can never leave a
   page quietly wrong.

3. **Anything that can ride plain HTTP, does.** Not for elegance: HTTP is the
   transport that has kept working through every failure in this tab's
   history, while the one we kept choosing is the one that broke.

4. **Nothing polls.** A page with the tab closed is subscribed to nothing. A
   page with it open holds the one SSE connection it already holds. Retry
   loops exist only while something is known to be broken, and back off.

5. **A failure is named, never silent.** The whole cost of the last six builds
   was a fault with no symptom. Every failure mode in section 15 has a defined
   thing the page says.

---

## 3. HTTPS, TLS front ends, and the browser extension: what is supported

Asked directly, so answered with evidence rather than assurance.

### Supported, and here is why

**Everything this extension serves uses relative URLs.** Audited: there is not
one absolute `http://` or `ws://` in `browser/*.js`, `javascript/main.js` or
the transfer library. A relative URL inherits the page's scheme, so every
route this extension owns is scheme-agnostic by construction. That is why
thumbnails, imports, the send route and the log route kept working through
every connection failure reported so far.

**The WanGP reverse proxy handles TLS explicitly**, and did so before this
workstream. `proxy.public_scheme()` reports the transport *the browser* is on
rather than the scheme of the current hop, reading `X-Forwarded-Proto` where a
terminator sets it and mapping a socket's own `ws`/`wss` to `http`/`https`
otherwise. The child's `Origin` is restated for it rather than forwarded,
because WanGP compares `Origin` against the scheme it thinks it is on. The
proxy suite carries **31 assertions naming https**.

**Both cookie shapes are read** — `access-token-<id>` and
`access-token-unsecure-<id>` — so a sign-in works whether Gradio issued a
secure cookie or not.

### The one configuration that genuinely breaks, and how it is detected

Gradio's frontend does **not** use relative URLs. It reads an absolute root out
of the config Forge inlines into the page and builds every event from it, and
Forge derives that root from the request it saw. So a Forge reached over HTTPS
whose uvicorn was never told it is behind TLS serves a page over `https` whose
config says `http`. The browser blocks every Gradio call as mixed content,
**silently**, while everything this extension does keeps working perfectly.

`window.minipaintHostRoot()` compares the two on every page and says so, at
tab-open rather than after the first failure, with the fix in the sentence:
have whatever terminates TLS send `X-Forwarded-Proto: https`, or give Forge
the public address with `--subpath`/`root_path`.

### On your install specifically

Your log carries **zero** host-root mismatches, and the send diagnosis it
produced carried no root clause — which it would have if the page and Gradio
disagreed. Your HTTPS extension is not causing anything, and was not the cause
of the send fault.

### And after this programme, the question mostly stops mattering

Every row this document moves off Gradio moves onto relative URLs. The
mixed-content trap only exists for Gradio's own absolute root; the less of
this tab that rides it, the less that misconfiguration can take with it.

---

## 4. What this programme is not

* **Not a rewrite of the Clipboard's server half.** Every `ClipboardTab`
  method keeps doing what it does. What changes is the door in front of it:
  a route instead of a Gradio callback. Most already have both.

* **Not a new API.** `interop.py` already publishes
  `/minipaint/outbox/{submit,claim,report,cancel,retry,adopt,cancel_all,track}`,
  `/minipaint/enhance`, `/minipaint/stage`, `/minipaint/events` and
  `/minipaint/sync` — the documented public `minipaint.wangp.queue/v1`
  contract. The Clipboard's Gradio callbacks and those routes **call the same
  functions**. Two front doors, one core. Most of this is wiring.

* **Not virtualisation.** Paging bounds the DOM; that is what virtualisation
  would have been for.

* **Not a change to what a picture is.** Ids, the store, the folder layout,
  the index file and the send path are untouched.

* **Not the Canvas.** Mini Paint's own tab keeps its Gradio rendering. It is
  named in section 13 as the one destination that stays, and is separate work.

---

## 5. The picture index

```
GET /minipaint-clipboard/library?sort=<mode>&page=<n>&size=<k>

{ "ok": true,
  "revision": "9f3c1a:41",
  "sort": "newest",
  "total": 437, "page": 3, "pages": 8, "size": 60,
  "items": [
    { "id": "<32 hex>", "name": "2026-02-09-091233.png",
      "w": 1024, "h": 1536, "bytes": 1802416, "v": 17012345678901234 } ] }
```

**Sorting is applied over the whole library, then sliced.**
`store.assets(sort)` already returns the complete ordered list from the
in-memory index, so this route is a slice of work the store does correctly
today. The ordering logic is not moved, rewritten or duplicated — which is
most of why this step is cheap.

**`revision` is the library's identity**, in the event spine's existing
`<epoch>:<revision>` form, so a value from a previous Forge process is not
stale but meaningless, and says so. The browser holds the revision its page
was built from; a mismatch means "ask again".

**`v` is the version of the picture's bytes** (`asset.mtime_ns`). It is what
makes the browser's thumbnail cache work — see section 10.

**Bad input is answered, not refused.** An unknown sort falls back to the
stored one; a page past the end returns the last page; a size outside its
bounds is clamped. A grid that shows nothing because a query string was wrong
is a worse answer than a grid.

**Gated by the same sign-in** as `/image` and `/import`, through `_signed_in`.

**Its own route, not an extension of `/send`.** `/send` already has two jobs
(prepare a send, remember a request); the index is a third with a different
cache policy and a different failure meaning.

---

## 6. Paging, and its controls

### Page size: 60

| page | thumbnails over the wire | cold server work | warm |
| --- | --- | --- | --- |
| 40 | 489 KiB | 1.8 s | instant |
| **60** | **733 KiB** | **2.8 s** | **instant** |
| 100 | 1.2 MiB | 4.6 s | instant |
| 250 | 3.1 MiB | 11.5 s | instant |

A 320px WEBP thumbnail measures **12 KiB median** (3–24 KiB over realistic
photographic content) and costs **~46 ms to make cold**, of which ~42 ms is
decoding the original; a 12 MP phone photo reaches ~390 ms.

**The binding constraint is connections, not bytes.** Every thumbnail is its
own request and a browser allows six per origin on HTTP/1.1 — already shared
with Gradio's event stream, this extension's event stream, and the WanGP
iframe through the proxy. A page of 60 fills in six to ten round-trip waves.
250 would be forty, and would feel like the grid never finishes.

60 also lands well in the layout: at 144px tiles it is eight to ten rows, two
or three scrolls inside the grid's box. And **four pages fit inside the
256-entry thumbnail cache**, so paging back and forth stays warm.

100 is defensible if fewer pages are wanted; below 40 the round trips buy
nothing. It is one constant and the route takes `size`, so it stays a
decision rather than becoming a migration.

### The controls

One row under the grid, drawn by the browser:

```
   [‹ Back]   Page [ 3 ] of 8   [Next ›]        437 pictures
```

* **Back / Next** — disabled at the ends rather than hidden, so the row does
  not change width as you move through it.
* **Go to page** — a number box, committed on Enter or blur, clamped rather
  than refused.
* **The count** — the whole library, because that is the number a person
  wants and it is free to say.
* Hidden entirely when there is one page.
* **PageUp / PageDown** move a page while the grid has focus. The grid is
  already `role="listbox"`; this is what a listbox is expected to do.

---

## 7. Sorting is separate from drawing

Changing the sort does not touch a tile until the new order has arrived. The
grid is marked busy (`aria-busy`, a subdued state), page 0 of the new sort is
fetched, and only then are nodes re-ordered.

A sort that cannot be fetched leaves the grid exactly as it was **and says
so** — where today a failed round trip leaves the grid stale with nothing to
indicate the sort did not take.

---

## 8. The library event

One new kind beside `JOB`, `ENHANCE`, `HANDOFF`, `RUNTIME`, `WANGP`:

```python
LIBRARY = "library"     # {"revision": "<epoch>:<n>", "total": int}
```

Published by `store` when the set of pictures or their order could have
changed: import, rename, delete, a folder change, and a refresh that found a
difference.

**Advisory, per principle 2.** It carries no contents. A browser that sees one
re-asks for the page it is showing — 9 KiB for 60 items — and patches from the
answer. No deltas to get wrong, no way for a missed event to leave a page
quietly incorrect, and a page that was asleep resyncs through machinery that
already exists.

---

## 9. Drawing and patching

The browser keeps `Map<id, HTMLElement>` for the tiles in the document and an
ordered array of ids for the page being shown.

**Building a page:** fetch the index; for each item reuse the existing tile if
the map has that id and its `v` is unchanged, else build one. Reused tiles keep
their decoded picture and their place in the browser's cache; only genuinely
new ones cause a request.

**Patching** is the same operation against the same page, so there is one code
path that puts tiles on screen and no second one to drift. An import that lands
on your page adds one node; a delete removes one; a rename changes one caption.
The other fifty-nine are untouched, the scroll position is untouched, nothing
is decoded twice.

**Re-ordering** moves existing nodes rather than rebuilding them:
`appendChild` in sequence moves, it does not recreate.

**Empty and unconfigured** states move into the browser with the same words
they have now. They are states of the index, not of the transport, and the
route answers them with `total: 0` and a `reason`.

---

## 10. Thumbnails: three caches, each with a job

**In the server's memory** — `store._thumbnails`, today 256 entries keyed by
`(id, mtime, size, side)`. Raised to cover several pages; an entry is ~12 KiB,
so 512 is ~6 MiB, which is not a number worth worrying about beside a model.

**On the server's disk** — new, and the one that matters most for hundreds of
pictures. Without it, every Forge restart re-encodes at ~46 ms each whatever
you look at.

* In the extension's own data directory, beside `clipboard.json` — **not** the
  user's picture folder, which is theirs and should not acquire files it did
  not ask for.
* Named `<asset id>-<mtime_ns>-<side>.webp`, so a changed file simply does not
  hit and no invalidation logic exists to get wrong.
* Bounded by total size with least-recently-used eviction, swept at startup
  and occasionally after writes. A cache that grows for ever is a bug with a
  long fuse.
* Never load-bearing: unreadable or unwritable means a slower tab, never a
  broken one. Every path falls back to encoding.

**In the browser** — this is "if the thumbnail has not changed, do not
download it again", and it is nearly free because the URL is already
versioned. `routes.image_url` appends `?v=<mtime_ns>`, so the URL changes
exactly when the bytes do. Today's header re-validates hourly for content that
by construction cannot have changed. For a request carrying a version:

```
Cache-Control: private, max-age=31536000, immutable
```

`immutable` is the part that works: the browser stops re-validating on reload,
so a thumbnail fetched once is never fetched again while it is in the cache. A
request *without* `?v=` keeps the conservative header, because an unversioned
URL can genuinely change meaning.

A rename changes the caption and not the file, so `mtime_ns` is unchanged, the
URL is unchanged, and nothing is re-fetched — the asked-for behaviour falling
out of the design rather than being special-cased.

**Not** Cache Storage or IndexedDB. The HTTP cache already does this, is shared
with everything else the page fetches, is evicted sensibly under pressure, and
needs no code. A durable store is worth considering only if measurement later
shows the HTTP cache being evicted between sessions.

---

## 11. Selection, and the library moving under you

**Selection survives paging.** The selected id is browser state and does not
belong to a page. Selecting on page 1 and moving to page 4 keeps the composer
live, because the send path names a picture by id. The pager marks the page
holding the selection.

**Selection survives a patch**, for free, because the tile is not rebuilt.

**Selection gives way to truth.** If the selected picture is deleted the
selection clears — the rule the tab already applies: a selection is given up
only when the library is listing pictures and the selected one is not among
them.

**A new picture does not move you.** An import while you are on page 3 updates
the count and the page total; it does not jump you to page 1. Where the new
picture belongs on your page it appears; where it does not, a quiet affordance
says so. Being relocated mid-task because a background job finished is the
kind of helpfulness nobody wants.

---

## 12. Moving the rest off Gradio

| piece | needs push? | needs Gradio? | how it moves | effort |
| --- | --- | --- | --- | --- |
| grid, pictures, paging, sorting | on change only | no | sections 5–11 | medium |
| status line | no — it is the *response* to your own action | no | each action's JSON answer carries its own sentence | small |
| queue list | already pushed on the spine | no | `outbox.jobs` is already a route; only rendering moves | small |
| Queue Send History | no | no | one read route over `history` | small |
| composer cards, the draft | no | no | read and write the draft over two routes | medium |
| rename, delete, choose folder | no | no | thin routes over existing store methods | small each |
| upload, paste, drop | no | **already not** | `/minipaint-clipboard/import` has always been HTTP | done |
| slot assignment | no | no | thin route | small |
| Add to Queue | no | no | `/minipaint/outbox/submit` exists; call the public API | small |
| prompt enhancement settings | no | no | `/minipaint/enhance` exists | small |
| send to img2img, Inpaint, Extras, ImageStitch | no | **already not** | the browser writes those components itself | done |
| send to Mini Paint | no | **yes** | section 13 | out of scope |

Three of these deserve detail.

### The status line

It looks like a thing the server tells you. It is not: it is the **response to
an action you just took**. Nothing else ever writes it.

So every action route answers with `{"ok": ..., "status": "Sent x.png to
img2img.", "notes": [...]}` and the browser draws the line. The wording stays
where it is — `_status()` already composes it and keeps doing so.

This is worth doing early and out of proportion to its size, because a status
line is the thing that most often looks *wrong* when a round trip is lost: it
silently keeps its old text, so the page appears to have done something it did
not do.

### The queue list

Updates already arrive on the event spine, which is why the list moves without
polling today. Only the **rendering** is Gradio: the browser presses a hidden
refresh button and the server returns HTML.

`outbox.jobs()` is already exposed at `/minipaint/outbox` and already returns
the job records the HTML is built from. Moving this is: fetch instead of press,
draw instead of receive markup. The job buttons (cancel, retry, adopt) already
have routes.

This is the piece that answers *"I closed the browser and came back"*, because
after it the list of what ran without you is readable without Gradio.

### Add to Queue

Today: a Gradio click builds the request from the stored draft and appends it
to the outbox, then a chained JS callback tells the browser to pump.

After: the browser calls `minipaintInterop.wangp.enqueue(...)` — the public API
it already loads for the WanGP bridge — and the server builds the request from
the same draft through the same `outbox.submit`. The durable acknowledgement
the browser waits for is the one the contract already defines.

---

## 13. What is genuinely irreducible

Two things, and both narrower than they sound.

**The tab must be a Gradio tab.** Forge builds its tab bar from Gradio blocks,
so the shell is Gradio and always will be. That is **build time**. Once the
page has loaded, the shell needs nothing from the transport.

**Mini Paint as a destination.** The Canvas's document is server state and its
UI is Gradio-rendered, so handing it a picture means a Gradio event that
re-renders the Canvas. This stays until the Canvas gets the same treatment —
separate work, named here so it is not discovered as a surprise. It is also
the rarest of the destinations.

Note what is **not** on that list: putting a picture into img2img, Inpaint,
Extras or ImageStitch. Those look like they must need Gradio, because the
picture ends up in another tab's component — but the browser already writes
those components directly, through the transfer library, and has since the
send route was built. That is precisely why sending kept working through every
connection failure in this tab's history.

---

## 14. What the notice becomes

Retire *"This page has lost its live connection to Forge"*.

Replace it with one that means what it says: **Forge is not answering** —
raised only when an HTTP request to this extension's own routes fails, not
when a framework's event stream sulks.

On localhost that means Forge has actually stopped, which is worth being told
and which no design can hide. Over a network it means the network is gone.
Either way it is rare, true, and actionable.

> **We cannot make "the server is gone" never appear. We can make it the only
> thing that appears — and on a machine where Forge is running, that is
> never.**

Until every row in section 12 has moved, the notice keeps its current meaning
for what has not moved **and says which**: a page whose grid is live and whose
queue list is stale should say that, rather than claiming the whole tab is
offline.

The self-healing retry already added stays, and applies to the new notice: it
backs off from five seconds to a minute, does not fire while the tab is in the
background, tries at once on return, and takes itself down when an answer
arrives.

---

## 15. Failure modes

Every one has a defined answer, because this tab's history is of failures with
none.

| what fails | what the page does |
| --- | --- |
| index route unreachable | keeps the tiles it has; says the library could not be re-read; retries with backoff |
| index answers, a thumbnail 404s | that tile shows its name and a missing mark; the page is otherwise unaffected |
| event stream drops | grid stale but correct; resyncs on reconnect through the existing cursor |
| event missed entirely | the next fetch corrects it, because the event carries no contents |
| `revision` moved between fetch and draw | draw, then re-fetch once; 9 KiB, and the answer is authoritative |
| disk thumbnail cache unreadable | encode as before; log once; never fail |
| library empties while on page 5 | clamp to the last page that exists |
| an action route fails | the status line says which action and why; nothing else is disturbed |
| Gradio channel dead, programme complete | nothing visible except Mini Paint as a destination |
| Forge actually stopped | the new notice, retrying quietly |

---

## 16. What the checks must prove

The grid has been reported broken three times with every check passing, so the
test plan is part of the design.

**`tests/browser_clipboard.py`** (real Chromium, real Gradio page):

* a page shows exactly `size` tiles and the pager states the right totals;
* Back / Next / go-to reach the right pictures; the ends are disabled;
* importing a picture onto the shown page adds **one** node — asserted by
  element identity, holding references to the others and checking they are the
  same elements afterwards;
* the scroll position survives that patch;
* changing the sort disturbs nothing until the new order lands;
* a thumbnail whose `v` is unchanged is not re-requested — asserted from
  `PerformanceObserver`, which the page already carries;
* selection survives paging and survives a patch;
* **the whole tab works with Gradio's channel cut**: browse, page, sort,
  select, send, queue, read the queue list. This suite already cuts the queue
  with page routing, so the fixture exists.

**`tests/test_clipboard_store.py`**: the route contracts — sort over the whole
library then sliced, clamping, `revision` changing when and only when the
library does, the disk cache's naming, bound and eviction, and an unwritable
cache directory costing nothing.

**`tests/test_events.py`**: `LIBRARY` publishes on the operations that change
the library and on no others.

**`tests/test_clipboard_ui.py`**: as pieces move, the Gradio graph shrinks. The
hidden-component ceiling comes **down** with each step, asserted, so the tab
cannot quietly keep both doors for ever.

---

## 17. Order of work

Each step leaves the tab working and is independently revertable. Value over
cost, with the deletions last.

| # | step | why here |
| --- | --- | --- |
| 1 | Thumbnail caches: memory bound, disk cache, immutable versioned URLs | No UI change; speeds up today's grid; stands alone if all else is abandoned |
| 2 | The index route | Nothing consumes it yet; checked directly |
| 3 | The `LIBRARY` event | Still nothing consuming it |
| 4 | Browser renderer for the grid | Replaces the markup behind the same element |
| 5 | The pager | |
| 6 | Patching on events; busy-state sort | |
| 7 | **Remove `grid_html`** and the grid from `refresh_outputs` | The step that actually deletes the 279 KiB |
| 8 | The status line | Small, and the thing that most often looks wrong |
| 9 | The queue list | Answers "I closed the browser and came back" |
| 10 | Add to Queue, history, enhancement settings | Small each, over routes that exist |
| 11 | Draft, slot cards, rename, delete, folder | The long tail |
| 12 | **Retire the notice**; replace with the HTTP one | Only honest once 1–11 are done |

**After step 9 the tab is usable end to end with Gradio's channel dead**:
browse, page, sort, select, send, queue, and watch a job run. Steps 10 and 11
are the last things that would still quietly not work.

Steps 1–6 add. Only 7 and 12 take away. That is the right order for something
people are using.

---

## 18. Risks, and what would make me stop

**More of the tab becomes browser code.** Today a bug is a Python bug with a
test. Afterwards more of it is JavaScript, which this repository has learned
twice is where bugs hide from the suite. Mitigated by the browser suite being
real, and by the checks above asserting identity and geometry rather than
markup.

**Two doors during the transition.** Steps 4–7, and again 8–11, have the server
able to render something nothing displays. Kept short deliberately, and the
removals are numbered steps rather than follow-ups that never happen. The
hidden-component ceiling coming down is the check that enforces it.

**A stale page between an event and a fetch.** Bounded: the fetch is 9 KiB and
the event carries no contents, so the worst case is a grid correct a few
hundred milliseconds late.

**Scope creep into the Canvas.** Section 13 names it as out of scope. If it
starts being touched, that is the signal to stop and write a separate document.

**It would not be worth continuing** if the index route could not be served
without Gradio's session — it can; three Clipboard routes already are — or if
paging turned out to be unwanted at the sizes actually in use. That is why
step 1 comes first and stands alone.

---

## 19. Measurements

All taken on this repository, 2026-09-15.

| what | value | how |
| --- | --- | --- |
| grid markup | 571 B/tile; 279 KiB at 500; 558 KiB at 1000 | `grid_html()` over synthetic assets |
| same index as JSON | 73 KiB at 500 | the fields in section 5 |
| 320px WEBP thumbnail | 12 KiB median, 3–24 KiB | photographic content, `quality=82, method=0` |
| making one, cold | ~46 ms median; ~42 ms of it decode; ~390 ms for 12 MP | open → convert → thumbnail → encode |
| thumbnail cache today | 256 entries, memory only, nothing on disk | `store.THUMBNAIL_CACHE_SIZE` |
| Gradio events on this tab | 38, all request-and-response, 0 pushes | counted in `_wire()` |
| absolute `http://`/`ws://` in browser code | 0 | audited across `browser/`, `javascript/`, the transfer library |
| https assertions in the proxy suite | 31 | `tests/test_wangp_proxy.py` |

---

## 20. Summary

This tab does not need a live connection. Every one of its 38 Gradio events is
a request with a response; push already rides this extension's own HTTP event
spine; and Gradio is required only where we let the server render. Most of the
HTTP that would replace those renders is already built, public and tested, and
calls the same functions the Gradio callbacks call.

So: the server keeps the order and the truth, the browser draws, changes arrive
as advisory events over the spine, pictures are cached in the server's memory,
on its disk, and immutably in the browser, and the grid pages 60 at a time with
Back / Next / go-to.

What remains on Gradio at the end is the tab shell, which is build time, and
Mini Paint as a destination, which is separate work.

And the line stops saying *"the live connection is gone"* and starts saying
*"Forge is not answering"* — which, on a machine where Forge is running, it
never has to say at all.
