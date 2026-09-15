# The Clipboard grid: paged, drawn by the browser, fed by events

Design intent for replacing the server-rendered thumbnail grid with one the
browser draws from an index it fetches over plain HTTP, pages through, and
patches when the server says something changed.

Written 2026-09-15, against the state of the tab after the send fault was
found. Every number in it was measured on this repository, not estimated;
where a figure is load-bearing the measurement is described so it can be
repeated.

---

## 1. What is wrong with the grid we have

The grid is `gr.HTML`. The server renders every tile as markup and pushes the
whole thing through Gradio's event channel on every change.

**Measured, on this repository:**

| library | grid markup | per tile |
| --- | --- | --- |
| 50 | 28 KiB | 571 B |
| 200 | 112 KiB | 571 B |
| 500 | **279 KiB** | 571 B |
| 1000 | 558 KiB | 571 B |

The same index as JSON is **73 KiB at 500 assets** — a quarter of the size
before compression, and it does not carry a single byte of presentation.

Three consequences, in the order they hurt:

**It dies with the Gradio connection.** This is the complaint that started the
whole workstream. The pictures themselves were never the problem: thumbnails
have always come from this extension's own HTTP route and keep loading when
Gradio is gone. It is the *markup* that cannot arrive. After the send fault
was fixed, the grid is the last thing in this tab still built this way.

**There is no such thing as a small change.** Import one picture and all 500
tiles are re-sent, and Gradio replaces the component's DOM wholesale. Every
`<img>` node is destroyed and rebuilt, so the scroll position is lost and
every visible picture is decoded again. The bytes mostly come from the browser
cache; the jank does not.

**It has no ceiling.** At a thousand pictures the tab ships half a megabyte of
markup per action and puts a thousand nodes in the document. Nothing in the
current shape improves with a bigger library.

A fourth problem is not about transport at all, and gets worse under paging
rather than better: **the thumbnail cache is smaller than the library.**
`store.THUMBNAIL_CACHE_SIZE` is 256, in memory, and nothing is written to
disk. Past 256 pictures, scrolling re-encodes thumbnails that were made
minutes ago, and every Forge restart re-encodes everything.

---

## 2. What this is not

*   **Not a rewrite of the tab.** The composer, the queue, the history, the
    slot cards and every action stay exactly where they are. Only the browser
    half of the grid changes, plus the one route and one event that feed it.

*   **Not, by itself, the end of the live connection.** This change frees the
    grid. The status line, the queue list, the composer cards and every
    action's result still come back through Gradio afterwards, so the notice
    can still appear. That is a property of *this step*, not a limit of the
    design: section 10 sets out why nothing in this tab genuinely needs a live
    connection, what each remaining piece would cost to move, and what the
    notice should become once they have.

*   **Not virtualisation.** Paging bounds the DOM to one page, which is what
    virtualisation would have been for. If a page of 60 is ever too many nodes
    the answer is a smaller page, not a windowing library.

*   **Not a change to what a picture *is*.** Ids, the store, the folder, the
    index file and the send path are untouched.

---

## 3. The shape

> The server owns the **order**. The browser owns the **drawing**.

That split is the whole design, and it is the one the tab's own user asked
for: sorting decides the sequence, drawing consumes a window of it and renders
when it is ready. The browser never sorts, never decides what is in the
library, and never holds an opinion the server cannot overrule. It decides
only what a tile looks like and which tiles are currently on screen.

Three pieces:

1. an **index route** that answers "the library in this order, this page of
   it" as JSON;
2. a **library event** on the spine this extension already has, saying "the
   library moved" without saying what it now is;
3. a **browser renderer** that builds tiles from the index and patches them by
   id.

---

## 4. The index route

```
GET /minipaint-clipboard/library?sort=<mode>&page=<n>&size=<k>

{ "ok": true,
  "revision": "9f3c1a:41",          // the library's identity right now
  "sort": "newest",                  // echoed: what was actually applied
  "total": 437, "page": 3, "pages": 8, "size": 60,
  "items": [
    { "id": "<32 hex>", "name": "2026-02-09-091233.png",
      "w": 1024, "h": 1536, "bytes": 1802416, "v": 17012345678901234 }
  ] }
```

**`sort` is applied over the whole library, then sliced.** `store.assets(sort)`
already returns the complete ordered list from the in-memory index, so this
route is a slice of work the store does correctly today. That is most of why
this change is cheap: the ordering logic is not being moved, rewritten or
duplicated.

**`revision` is the library's identity, not a timestamp.** It uses the event
spine's existing `<epoch>:<revision>` form, so a cursor from a previous Forge
process is not stale — it is meaningless, and says so. The browser holds the
revision its page was built from; anything that does not match means "ask
again".

**`v` is the version of the picture's bytes** (`asset.mtime_ns`), and it is
what makes the browser's thumbnail cache work. See §8.

**Unknown or absent parameters are answered, not refused**: an unknown sort
falls back to the stored one, a page past the end returns the last page, a
size outside its bounds is clamped. A grid that shows nothing because a query
string was wrong is a worse answer than a grid.

**It is gated by the same sign-in as the other Clipboard routes**, through
`_signed_in`, like `/image` and `/import`.

### Why not extend `/send`

`/minipaint-clipboard/send` has grown two jobs already (prepare a send,
remember a request). The index is a third unrelated thing with a different
cache policy and a different failure meaning. It gets its own route.

---

## 5. Paging, and the controls

**Page size: 60.** Reasoning, measured:

| page | thumbnails over the wire | cold server work | warm |
| --- | --- | --- | --- |
| 40 | 489 KiB | 1.8 s | instant |
| **60** | **733 KiB** | **2.8 s** | **instant** |
| 100 | 1.2 MiB | 4.6 s | instant |
| 250 | 3.1 MiB | 11.5 s | instant |

A 320px WEBP thumbnail measures **12 KiB median** (3–24 KiB over realistic
photographic content) and costs **~46 ms to make cold**, of which ~42 ms is
decoding the original; a 12 MP phone photo reaches ~390 ms.

The binding constraint is **not bytes, it is connections**. Every thumbnail is
its own request and a browser allows six per origin on HTTP/1.1 — and this
page is already spending some of those on Gradio's event stream, this
extension's event stream, and the WanGP iframe through the reverse proxy. A
page of 60 fills in six to ten round-trip waves. A page of 250 would be forty,
and would feel like the grid never finishes.

60 also lands well in the layout: at 144px tiles it is eight to ten rows, two
or three scrolls inside the grid's own box — a page, rather than an arbitrary
cut. And **four pages fit inside the 256-entry thumbnail cache**, so paging
back and forth stays warm.

100 is defensible if fewer pages are preferred. Below 40 the round trips stop
buying anything. The number is one constant, and the route takes `size`, so
changing it later is a one-line decision rather than a migration.

### The controls

A single row under the grid, drawn by the browser like the rest of it:

```
   [‹ Back]   Page [ 3 ] of 8   [Next ›]        437 pictures
```

*   **Back / Next** — disabled at the ends rather than hidden, so the row does
    not change width as you move through it.
*   **Go to page** — a number box. Committed on Enter or blur; out-of-range is
    clamped rather than refused.
*   **The count** — the whole library, not the page, because "437 pictures" is
    the number a person actually wants and it is free to say.

The row is hidden entirely when there is one page, which is the common case
for a small library and should not cost a line of chrome.

Keyboard: PageUp/PageDown move a page while the grid has focus. The grid is
already a `role="listbox"`, so this is the behaviour a listbox is expected to
have rather than an invention.

---

## 6. The library event

The extension already has the transport. `minipaint_neo/events.py` is a
complete event spine — SSE, snapshot on `/minipaint/sync`, cursors namespaced
by process epoch, a bounded replay ring, and resync-on-gap. The WanGP queue
has ridden it since protocol 3. The Clipboard **library** simply never got put
on it.

Add one kind beside `JOB`, `ENHANCE`, `HANDOFF`, `RUNTIME`, `WANGP`:

```python
LIBRARY = "library"     # {"revision": "<epoch>:<n>", "total": int}
```

Published by `store` when, and only when, the set of pictures or their order
could have changed: import, rename, delete, a folder change, and a refresh
that actually found a difference.

**The event is advisory. The route is authoritative.** That is the spine's own
first rule and this follows it exactly: the event says "the library moved", it
does not carry the new contents. A browser that sees one re-asks for the page
it is showing — 9 KiB for 60 items — and patches from the answer. No deltas to
get wrong, no way for a missed event to leave a page quietly incorrect, and a
page that was asleep resyncs through the machinery that already exists.

**No polling is introduced anywhere.** A page with the tab closed is not
subscribed; a page whose tab is open holds the one SSE connection it already
holds for the queue.

---

## 7. Drawing and patching

The browser keeps `Map<id, HTMLElement>` for the tiles currently in the
document, and one ordered array of ids for the page it is showing.

**Building a page** is: fetch the index, then for each item reuse the existing
tile if the map has that id and its `v` is unchanged, else build a new one.
Reused tiles keep their decoded picture and their position in the browser's
own cache; only genuinely new ones cause a request.

**Patching** on a library event is the same operation against the same page,
so there is exactly one code path that puts tiles on screen and no second one
to drift. An import that lands on your page adds one node. A delete removes
one. A rename changes one caption. Fifty-nine tiles are untouched, the scroll
position is untouched, and nothing is decoded twice.

**Ordering** is applied by moving existing nodes, not by rebuilding them — the
browser has the ids in order and the map has the nodes, so re-ordering is
`appendChild` in sequence, which moves rather than recreates.

### Sorting is separate from drawing

Changing the sort does not touch the tiles until the new order arrives. The
grid is marked busy (`aria-busy`, a subdued state), the index is fetched for
page 0 of the new sort, and only then are the nodes re-ordered. A sort that
cannot be fetched leaves the grid exactly as it was and says so — where today
a failed round trip leaves the grid stale with no indication that the sort did
not take.

### Empty and unconfigured

The two states the server renders as prose today (`no storage folder`,
`no images yet`) move into the browser with the same words. They are states of
the index, not of the transport, and the route answers them with `total: 0`
and a `reason`.

---

## 8. Thumbnails: three caches, each with a job

**In the server's memory** — `store._thumbnails`, today 256 entries keyed by
`(id, mtime, size, side)`. Raise to cover several pages comfortably; the entry
is ~12 KiB, so 512 entries is ~6 MiB, which is not a number worth worrying
about next to a model.

**On the server's disk** — new, and the one that matters most for a library of
hundreds. Without it every Forge restart re-encodes at ~46 ms per picture
whatever you look at.

*   Location: the extension's own data directory, beside `clipboard.json` —
    **not** the user's picture folder, which is theirs and should not acquire
    files it did not ask for.
*   Name: `<asset id>-<mtime_ns>-<side>.webp`, so a changed file simply does
    not hit and no invalidation logic is needed.
*   Bounded: a total-size cap with least-recently-used eviction, swept on
    startup and occasionally after writes. A cache that grows forever is a bug
    with a long fuse.
*   Never load-bearing: a disk cache that cannot be read or written is a
    slower tab, never a broken one. Every path through it falls back to
    encoding.

**In the browser** — this is the one that answers "if the thumb is unchanged,
no need to re-download", and it is nearly free because the URL is already
versioned.

`routes.image_url` already appends `?v=<mtime_ns>`, so a thumbnail's URL
changes exactly when its bytes change. The header today is
`Cache-Control: private, max-age=3600`, which makes the browser re-validate
after an hour for content that by construction cannot have changed. For a
request that carries a version, it becomes:

```
Cache-Control: private, max-age=31536000, immutable
```

`immutable` is the part that does the work: it stops the browser
re-validating on reload, so a thumbnail fetched once is never fetched again
for as long as it is in the cache. A request *without* `?v=` keeps the
conservative header, because an unversioned URL genuinely can change meaning.

A rename changes the caption and not the file, so `mtime_ns` is unchanged, the
URL is unchanged, and the picture is not re-fetched — which is the behaviour
asked for, falling out of the design rather than being special-cased.

**Not** Cache Storage or IndexedDB. The HTTP cache already does this, is
shared with everything else the page fetches, is evicted sensibly under
pressure, and needs no code. A durable store would be worth considering only
if measurement later shows the HTTP cache being evicted between sessions,
and that is a decision with evidence, not now.

---

## 9. Selection, and the library moving under you

**Selection survives paging.** The selected id is browser state and does not
belong to a page. Selecting on page 1 and moving to page 4 keeps the composer
buttons live, because the picture still exists and the send path names it by
id. The pager marks the page holding the selection so it can be found again.

**Selection survives a patch**, for free, because the tile is not rebuilt.

**Selection gives way to the truth.** If the selected picture is deleted, the
selection clears — the same rule the tab already applies, which is that a
selection is given up only when the library is listing pictures and the
selected one is not among them.

**A new picture does not move you.** An import while you are on page 3 updates
the count and the page total; it does not jump you to page 1. Where the new
picture belongs on your page it simply appears. Where it does not, a quiet
affordance says so. Being relocated mid-task because a background job finished
is the kind of helpfulness nobody wants.

---

## 10. Why anything is "live" at all, and what should be

This section exists because the question was asked directly, and the honest
answer turns out to be better than the one section 2 first implied.

### Three different things get called "the connection"

**Push** — the server telling a page something it did not ask about: a job
finished, another page added a picture. This genuinely needs a channel held
open. **In this extension it already is one, and it is not Gradio:**
`/minipaint/events` is server-sent events over plain HTTP, with
`/minipaint/sync` for a snapshot and a cursor to resume from after a gap. The
WanGP queue has used it since protocol 3.

**Request and response** — press a thing, get an answer. This needs nothing
held open at all. An HTTP request does it, and has since the web did.

**Server-rendered UI** — the server deciding what the page *displays*. This is
the only one of the three that forces Gradio, because the value of a Gradio
component can only be set by a Gradio event.

### Counted on this tab: all 38 of its Gradio events are the second kind

Not one of them is a push. Every one is a click, an input, an upload or a blur
that returns a value to the page that asked for it.

So the honest answer to *"why does this page need anything live?"* is:
**it does not.** It needs Gradio — and Gradio's request and response happen to
travel over a queue-and-SSE transport that dies when the tab is backgrounded,
when a session is forgotten, and when Forge restarts. The fragility belongs to
the transport, not to the interaction.

And it needs Gradio only where we let the server render. **Every
server-rendered component is a purchased dependency on that transport.** The
grid is the largest such purchase. It is not the only one.

### The test: submit a request, close the browser, have it complete

That already works, and it is the stated intent of the code rather than a
happy accident. From `clipboard/executor.py`:

> the outbox is work Forge owns, and the browser's only remaining job is to
> describe what the user wants and get a durable acknowledgement before it
> disappears.

`outbox.chosen_executor()` returns server execution unless unattended
execution is switched off in settings, or this WanGP build has *explicitly*
said it cannot run unattended jobs. Silence is not a refusal — a cold start
still admits as unattended, because that is the case the unattended path
exists for. On a normal install, a queued job survives the browser closing.

What does not survive is **the view of it**. The queue list is Gradio-rendered,
so a page that comes back needs Gradio to show you the job that ran perfectly
well without it. The work is already durable; only the window onto it is not.

### What each remaining piece would take

The important discovery is how much already exists. `interop.py` publishes
`/minipaint/outbox/{submit,claim,report,cancel,retry,adopt,cancel_all,track}`,
`/minipaint/enhance`, `/minipaint/stage`, `/minipaint/events` and
`/minipaint/sync` — the documented public `minipaint.wangp.queue/v1` contract
that any extension may call. And the Clipboard's Gradio callbacks and those
routes **call the same functions**: `outbox.submit`, `outbox.jobs`,
`outbox.cancel`, `outbox.retry`, `outbox.adopt`, `outbox.cancel_all`. Two front
doors, one core.

So most of what follows is wiring a browser to an API that is already built,
already public and already tested — not designing one.

| piece | needs push? | needs Gradio? | how it moves | effort |
| --- | --- | --- | --- | --- |
| grid, pictures, paging, sorting | on change only | no | this document | medium |
| status line | no — it is the *response* to your own action | no | each action's JSON answer carries its own sentence; the browser draws the line | small |
| queue list | already pushed on the spine | no | `outbox.jobs` is already an HTTP route; only the rendering moves | small |
| Queue Send History | no | no | one read route over `history` | small |
| composer cards, the draft | no | no | read and write the draft over two routes | medium |
| rename, delete, choose folder | no | no | thin routes over store methods that already exist | small each |
| upload, paste, drop | no | **already not** | `/minipaint-clipboard/import` has always been HTTP | done |
| slot assignment | no | no | thin route | small |
| Add to Queue | no | no | `/minipaint/outbox/submit` exists; the browser calls the public API | small |
| prompt enhancement settings | no | no | `/minipaint/enhance` exists | small |
| send to img2img, Inpaint, Extras, ImageStitch | no | **already not** | the browser writes those components itself | done |
| send to Mini Paint | no | **yes** | see below | out of scope |

### What is genuinely irreducible

Two things, and both are narrower than they sound.

**The tab has to be a Gradio tab.** Forge builds its tab bar from Gradio
blocks, so the shell is Gradio and always will be. That is build time. Once the
page has loaded, the shell needs nothing from the transport.

**Mini Paint as a destination.** The Canvas's document is server state and its
UI is Gradio-rendered, so handing it a picture means a Gradio event that
re-renders the Canvas. That stays until the Canvas gets this same treatment —
separate work, named here so it is not discovered as a surprise later. It is
also the rarest of the destinations.

Note what is **not** on that list: putting a picture into img2img, Inpaint,
Extras or ImageStitch. Those look like they must need Gradio, because the
picture ends up in another tab's component — but the browser already writes
those components directly, through the transfer library, and has since the
send route was built. That is precisely why sending kept working through every
connection failure in this tab's history.

### What the notice becomes

Retire *"This page has lost its live connection to Forge"* and replace it with
one that means what it says: **Forge is not answering.**

Raised only when an HTTP request to this extension's own routes fails — not
when a framework's event stream sulks. On localhost that means Forge has
actually stopped, which is worth being told and which no amount of design can
hide. Over a network it means the network is gone. Either way it is rare, it is
true, and it is actionable.

**We cannot make "the server is gone" never appear. We can make it the only
thing that appears** — and on a machine where Forge is running, that is never.

Until every row above has moved, the notice keeps its current meaning for what
has not moved, and says which: a page whose grid is live and whose queue list
is stale should say that, rather than claiming the whole tab is offline.

### The order to move them in

Roughly by value over cost, and each is independently shippable:

1. **The grid** — sections 3 to 9 of this document. The largest piece, and the
   one most looked at.
2. **The status line** — small, and the thing that most often looks wrong when
   a round trip is lost, because it silently keeps its old text.
3. **The queue list** — small, because the routes exist; and it is the answer
   to "I closed the browser and came back".
4. **Add to Queue, history, enhancement settings** — small each, all over
   routes that already exist.
5. **The draft, the slot cards, rename, delete, folder** — the long tail.
6. **Retire the notice**, and replace it with the HTTP one.

After step 3 the tab is usable end to end with Gradio's channel dead: browse,
page, sort, select, send, queue, and watch a job run. Steps 4 and 5 are the
last things that would still quietly not work.

## 11. Failure modes

Every one of these has a defined answer, because the tab's history is of
failures with no answer at all.

| what fails | what the page does |
| --- | --- |
| index route unreachable | keeps the tiles it has, says the library could not be re-read, retries on the same backing-off timer the connection notice uses |
| index answers, a thumbnail 404s | that tile shows its name and a missing mark; the rest of the page is unaffected |
| event stream drops | the grid is stale but correct; it resyncs on reconnect through the spine's existing cursor |
| event missed entirely | the next fetch corrects it, because the event carries no content |
| `revision` moved between fetching page 3 and drawing it | draw it, then re-fetch once; the answer is authoritative and the cost is 9 KiB |
| disk thumbnail cache unreadable | encode as before; log once, never fail |
| library empties while on page 5 | clamp to the last page that exists |
| Gradio channel dead | grid fully functional; the notice explains what is not |

---

## 12. What the checks must prove

The grid has been reported broken three times with every check passing, so the
test plan is part of the design rather than an afterthought.

**In `tests/browser_clipboard.py`** (a real page, real Chromium):

*   a page shows exactly `size` tiles, and the pager says the right totals;
*   Next/Back/goto move to the right pictures, and the ends are disabled;
*   importing a picture onto the shown page adds **one** node — asserted by
    identity, holding references to the other tiles and checking they are the
    same elements afterwards;
*   the scroll position survives that patch;
*   changing the sort does not disturb the tiles until the new order lands;
*   a thumbnail whose `v` is unchanged is not re-requested — asserted from
    `PerformanceObserver`, which the page already carries for the send
    diagnostics;
*   selection survives paging and survives a patch;
*   the grid still draws, pages and updates **with Gradio's channel cut**,
    which is the entire point and is already a fixture in this suite.

**In `tests/test_clipboard_store.py`**: the route's contract — sort applied
over the whole library then sliced, clamping, `revision` changing when and
only when the library does, the disk cache's naming, bound and eviction, and
that an unwritable cache directory costs nothing.

**In `tests/test_events.py`**: that `LIBRARY` publishes on the operations that
change the library and on no others.

---

## 13. Order of work

Each step leaves the tab working and is independently revertable.

1. **Thumbnail caches** — raise the memory bound, add the disk cache, make
   versioned thumbnail URLs immutable. No UI change; the current grid gets
   faster immediately. *(Smallest, and it stands alone if the rest is
   abandoned.)*
2. **The index route** — with sorting, paging, clamping and `revision`.
   Nothing consumes it yet; it is checked directly.
3. **The `LIBRARY` event** — published by the store, carried by the spine.
   Still nothing consuming it.
4. **The browser renderer** — draw from the index, behind the existing grid
   element, replacing the server-rendered markup. The server keeps rendering
   the empty and unconfigured states until this lands.
5. **The pager.**
6. **Patching on events**, and the busy-state sort.
7. **Remove the server-side `grid_html`** and the grid from `refresh_outputs`,
   once nothing reads them.

Step 7 is what actually deletes the 279 KiB. Steps 1–6 add; only the last one
takes away, which is the right order for a change that must not break a tab
people are using.

---

## 14. Risks, and what would make me stop

**The grid becomes browser code.** Today the server decides what is on screen
and a bug is a Python bug with a test. Afterwards, more of it is JavaScript,
which this repository has learned twice is where bugs hide from the suite.
Mitigated by the browser suite being real (Chromium, a real Gradio page) and
by the checks above being about identity and geometry rather than markup.

**Two renderers during the transition.** Steps 4–7 have the server still able
to render a grid nothing displays. Kept short on purpose, and step 7 is
explicitly part of this change rather than a follow-up that never happens.

**A stale page between an event and a fetch.** Bounded by the fetch being
9 KiB and the event carrying no content: the worst case is a grid that is
correct a few hundred milliseconds late.

**It would not be worth continuing** if the index route cannot be served
without Gradio's session (it can — three Clipboard routes already are), or if
paging turns out to be unwanted at the sizes actually in use. The first is
answered; the second is why step 1 comes first and stands alone.

---

## 15. Summary

Replace 279 KiB of server-rendered markup per action with a 9 KiB JSON page
the browser draws and patches, fed by an event spine that already exists,
paged 60 at a time with Back / Next / go-to, over thumbnails cached in the
server's memory, on the server's disk, and immutably in the browser.

The grid stops depending on the connection whose failure has dominated this
tab's history, stops re-rendering everything to change one thing, and gains a
ceiling it does not currently have.

It is also the first step of a larger one. Nothing in this tab genuinely needs
a live connection: every one of its 38 Gradio events is a request with a
response, not a push, and push already rides this extension's own HTTP event
spine. Gradio is needed only where the server renders — and most of the HTTP
API that would replace those renders is already built, public and tested.
Section 10 sets out the order to move the rest in, what is genuinely
irreducible (the tab shell, and Mini Paint as a destination), and what the
notice should become: not *"the live connection is gone"* but *"Forge is not
answering"* — which, on a machine where Forge is running, is never.
