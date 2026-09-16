# What was built, and what is left: the no-live-connection programme

Companion to `NO_LIVE_CONNECTION_DESIGN_INTENT_2026-09-15_REV2.md`, written
against the build that implements it. That document says what the tab should
become; this one says what it now is, and names — with reasons — everything
it is not yet.

Read section 17 of the intent for the order of work. The steps below use its
numbers.

---

## 1. What moved

### Before step 1: the Send-to contract, locked

Section 12's rule is now the code's rule and the browser suite's.

* `minipaintCanvas.switchTo()` **returns a result** (`{ok, reason}`) instead of
  being fire-and-forget, and it reads the answer off the page rather than
  assuming the host's helper worked: a helper that exists but left a different
  tab showing is a failed switch.
* `deliverToHost()` answers with **delivery and navigation as separate facts**
  (`{ok, reason, switched, switchReason}`). Nothing navigates before
  `still_holds()` has passed, and the plain unverified write for a page
  without the transfer library no longer navigates on its own either — the
  caller decides, and only after a delivery that happened.
* Clipboard says `Sent <name> to <destination>, but could not open that tab.`
  when the picture is proved to have landed and the switch could not be made,
  and **never resends** in that case.
* A failed send leaves the user in Clipboard with the reason. Navigation is
  never used as evidence that delivery worked.
* **Mini Paint follows the same UX although its transport is different.** Its
  receive is still a Gradio event (section 13), but the chain now writes a
  receipt box and the Canvas tab is shown only for a receive that landed. A
  receive that had nothing to take, or could not read the picture, leaves the
  user in Clipboard.

### Step 1 — the thumbnail caches

* Memory: `THUMBNAIL_CACHE_SIZE` 256 → **512** (~6 MiB), which covers several
  pages so paging back and forth stays warm.
* Disk: `<data dir>/clipboard-thumbnails/`, **never** the user's picture
  folder. Named `<asset id>-<mtime_ns>-<size_bytes>-<side>.webp`, which is the
  whole file identity — so a changed file simply does not hit and there is no
  invalidation logic anywhere. Bounded by **total size** (64 MiB) with
  least-recently-used eviction, swept at startup and after every 8 MiB
  written. Never load-bearing: unreadable or unwritable is a slower tab, and
  it complains once rather than once per tile.
* Browser: `routes.image_url` carries `?v=<mtime_ns>-<size_bytes>`, and a
  request whose `v` is the asset's **current canonical** version — read from
  the file, not from the index — is answered
  `Cache-Control: private, max-age=31536000, immutable`. Anything else keeps
  `max-age=3600`. A rename changes the caption and not the file, so the URL
  does not move and nothing is re-fetched.

### Steps 2 and 3 — the index route and the LIBRARY event

* `GET /minipaint-clipboard/library?sort&page&size&selected&refresh` answers
  the shape in section 5, plus `selected_page` (which page holds the
  selection, so the pager can mark it) and `status` (the sentence for the
  status line). Sorting is applied over the whole library and then sliced.
  Bad input is answered, not refused: an unknown sort falls back to the stored
  one, a page past the end returns the last page that exists, a size outside
  its bounds is clamped. `refresh=1` re-reads the folder first, which is what
  opening the tab has always done and now no longer needs Gradio to do.
* `events.LIBRARY` carries `{"revision": "<epoch>:<n>", "total": int}` and
  nothing else. The revision is the **library's own** generation — deliberately
  not `events.revision()`, which job, enhancement, runtime and WanGP events
  move constantly. Published by the store on import, rename, delete, folder
  change, and a refresh that found a difference; on nothing else.

### Steps 4 to 7 — the grid in the browser, and the deletion

* The browser fetches the index, builds tiles, and reuses them: a tile is
  rebuilt only when its `v` changes, and re-ordering *moves* nodes rather than
  recreating them. Patching and building are one code path.
* The pager is section 6's: Back / Next disabled at the ends rather than
  hidden, a go-to box committed on Enter or blur and clamped, the whole
  library's count, hidden entirely at one page, PageUp / PageDown on the grid,
  and a mark saying which page holds the selection when you have paged away.
  Two presses in a row are two pages, and only the newest answer draws.
* Sorting marks the grid busy and disturbs nothing until the new order lands;
  a sort that cannot be fetched leaves the grid as it was **and says so**.
* `grid_html` is **gone**, and so is the grid from `refresh_outputs`. 571
  bytes a tile, 279 KiB at five hundred pictures, no longer crosses anything.

### Steps 8 to 10 — the status line, the queue list, the history, the press

* Every route answers with its own `status` sentence, composed where the rest
  of the tab's wording is composed, and the browser draws the line. The
  browser writes the **same node** Gradio renders into, so during the
  transition each write is the answer to its own action and the last one wins.
* `GET/POST /minipaint-clipboard/queue` carries the queue list, the history,
  the status sentence and the button's two facts, and takes `add`, `cancel`,
  `retry`, `adopt` and `cancel_all`. `outbox_html` and `history_html` are gone;
  `outbox_view` and `history_view` answer with content, and the browser builds
  the nodes.
* Add to Queue is a browser press to that route, carrying the prompt as typed
  and the switch as it stands — the two things that live in the browser.
* `POST /minipaint-clipboard/settings` saves the sort, the tile size and the
  intercept, and answers with the menu the browser draws itself from.
* `GET/POST /minipaint-clipboard/enhance-settings` describes the enhancement
  settings and takes `toggle`, `override` and `restore`.

### Step 14 — what the notice became

`This page has lost its live connection to Forge` is retired. Two sentences
replace it:

* **Forge is not answering** — raised only when an HTTP request to this
  extension's own routes fails. On localhost that means Forge has stopped.
* **The composer's live channel to Forge is down on this page** — raised when
  the framework's channel is not delivering and the server is fine, and it
  **names which parts** are stale (`STALE_WITHOUT_THE_CHANNEL` in the browser
  bundle). That list shrinks as rows move; when it is empty this notice stops
  being raised at all.

The backing-off self-healing retry stays and now probes over HTTP when the
server is the thing that is silent.

---

## 2. The V2 list

Everything below is deliberate. None of it is a discovered surprise.

### V2-1 — Step 11: the composer's slot cards, and what re-renders them

**What is left.** The three slot cards, the draft, rename, delete, the folder
chooser and Queue Send History's *Load* still cross the framework's channel.
The cards are `card_html` server-renders and every action that changes a slot
returns new markup for them.

**Why it is not in this change.** Section 12 calls this "the long tail", and
it is the one row where moving the *route* is the small half: the cards are
also where the WanGP capability badges are toggled, where a drop on a card
imports and assigns in one gesture, and where "Missing image" is decided. The
routes would be thin; a second renderer for the cards is not, and putting one
in beside `card_html` is exactly the "two doors" this programme names as its
main risk. Doing it properly means the cards become browser-drawn from a view
model in the same shape as `outbox_view`, which is a change worth its own
review rather than a tail on this one.

**What it costs today.** With the framework's channel dead, browsing, paging,
sorting, selecting, sending, queueing and reading the queue all work; the
composer's slots do not update, and the notice says so by name.

### V2-2 — The toolbar's sort dropdown

**What is left.** The sort itself is the browser's: the menu's Sort submenu
posts to `/minipaint-clipboard/settings` and re-draws the grid over HTTP. The
Gradio `Dropdown` in the toolbar still persists through its own event, and its
displayed label follows the server.

**Why.** A Gradio Dropdown's displayed value is the framework's own state, and
writing it from outside is not something an extension can do honestly. The
fix is to draw the control in the browser, which is a visible UI change and
belongs with V2-1's rendering work.

**What it costs.** With the channel dead, changing the sort from the menu
works and the grid re-draws; the dropdown's label shows the previous sort
until the page is reloaded.

### V2-3 — The prompt-enhancement panel

**What is left.** The write routes exist and are checked
(`/minipaint-clipboard/enhance-settings`), but the panel — the switch, the two
selectors, the system-prompt box, Apply and Restore — is still Gradio
components wired to Gradio callbacks.

**Why.** Section 12 calls for "thin write routes for toggle / override /
restore", which is what was built. Consuming them means replacing a Gradio
`Textbox`, two `Dropdown`s and a `Checkbox` with browser controls, which is
the same rendering work as V2-1 and has the same reason to wait.

### V2-4 — Step 12: retiring the notice completely

**What is left.** The notice about the framework's channel still exists,
because V2-1 to V2-3 still ride it.

**Why.** Section 17 is explicit: step 12 is "only honest once 1–11 are done".
The interim behaviour section 14 asks for — keeping the notice for what has
not moved *and saying which* — is what this build does.

### V2-5 — Mini Paint as a destination

**What is left.** Handing a picture to the Canvas is still a Gradio event that
re-renders the Canvas.

**Why.** Section 13 names it: the Canvas's document is server state and its UI
is server-rendered, so this stays until the Canvas gets the same treatment.
That is separate work, and section 18 names scope creep into the Canvas as the
signal to stop and write a separate document.

**What it does NOT mean.** Mini Paint has no different Send-to experience: it
does not have to be opened first, a receive that landed shows its tab, and one
that did not leaves the user in Clipboard. That is built and checked.

### V2-6 — A durable thumbnail store in the browser

**What is left.** The browser's thumbnail cache is the HTTP cache, made
effective by immutable versioned URLs.

**Why.** Section 10: Cache Storage and IndexedDB are worth considering only if
measurement later shows the HTTP cache being evicted between sessions. Nothing
has measured that yet, so nothing was built for it.

### V2-7 — Virtualisation

Explicitly not this programme (section 4). Paging bounds the DOM, which is
what virtualisation would have been for.

---

## 3. Where to look

| what | where |
| --- | --- |
| the index route, the settings/queue/enhancement doors, the cache headers | `minipaint_neo/clipboard/routes.py` |
| the library's revision, the disk thumbnail cache, `version_of` | `minipaint_neo/clipboard/store.py` |
| `LIBRARY` | `minipaint_neo/events.py` |
| the view models and what is left of the Gradio graph | `minipaint_neo/clipboard/ui.py` |
| the grid, the pager, the patching, the queue list, the notice | `browser/minipaint_clipboard.js` |
| deliver, prove, then show | `browser/minipaint_canvas.js` (`deliverToHost`, `landed`, `switchTo`) |
| what the checks prove | `tests/browser_clipboard.py`, `tests/test_clipboard_store.py`, `tests/test_events.py`, `tests/test_clipboard_ui.py` |
