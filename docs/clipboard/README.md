# The Clipboard tab and the WanGP queue API — an operator's guide

This describes what the extension does when the **Clipboard** tab is used and when
another extension calls the public queue API: what it shows, what it writes, what it
refuses to write, and what you can check for yourself. It is written against the code in
`minipaint_neo/clipboard/`, `minipaint_neo/interop.py`, `browser/minipaint_clipboard.js`,
`browser/minipaint_interop.js`, the queue half of `wan2gp_bridge/` and the registration
in `scripts/mini_paint.py`. The WanGP tab it builds on has its own guide,
`docs/wangp/README.md`; the two design documents it implements are kept for the record
(the Clipboard design intent, and `docs/wangp/START_AND_OUTBOX.txt`, which is marked
deprecated and says where the implementation departs from it). Where a document and the
code differ, this file follows the code and says so.

## What it is

A third top-level tab, **Clipboard** (id `minipaint_clipboard`), beside Mini Paint and
WanGP. On the left, about two thirds of the width, a small file browser over one folder on
the machine running Forge; on the right, a small **WanGP request** composer: three cards —
*First Frame*, *Last Frame*, *Reference* — a prompt box, one button, **Add to Queue**, and
under it the **Queue**: every request sent from this Forge, newest first.

It is registered as a third, independent integration. If any part of it fails to import
or to build, one line says so in the WebUI console, the tab shows a note under the same
label, and Mini Paint and WanGP load exactly as before — including the gallery's *Send to
Mini Paint* button, which then keeps its old behaviour.

Everything on the page is an ordinary Gradio component, coloured by the host theme's own
variables and by nothing else. No fixed white, no fixed black: a night-mode theme (the Lobe
theme, for one) reaches every corner of the tab, including the thumbnail grid, the cards,
the menu, the toast, the queue and the history list.

The grid, its pager, the queue and the history are drawn **by the browser**, from routes
this extension serves over plain HTTP; the composer's three slot cards are still
server-rendered HTML the browser script listens to. What that division buys, what it cost
and what is left of it is
`docs/clipboard/NO_LIVE_CONNECTION_WHAT_WAS_BUILT_AND_V2.md`, beside the design intent it
implements.

## The one rule

A request is an **overlay on the live WanGP page**, never a new form:

* a field you leave empty is the WanGP page's own — the prompt the page has, the picture the
  page has, whatever they are at the moment the request is sent;
* a field you fill overrides the page **for that one task** and is put back afterwards;
* the four fields that can be overridden are the prompt, the start (first) frame, the end
  (last) frame and the reference images. Nothing else on the WanGP page can be touched from
  here: not the model, not a setting, not a slider.

So the composer never has to be complete. An entirely empty composer asks WanGP to run the
page *exactly as it is* — which is a perfectly good thing to ask for. A card put back to
*Use WanGP* with its × changes nothing on the WanGP page; there is no "clear WanGP's field"
here, on purpose.

**A request starts WanGP when WanGP is idle, and joins it when it is not.** That decision
is made inside WanGP, by the bridge, from Wan2GP's own process-wide "is a generation
running" flag — never by the page guessing from a button, and never by the caller. The
first request to find WanGP idle runs WanGP's own generate chain, exactly as its Generate
button would; the requests behind it run WanGP's add-to-queue chain and join that run.
When the flag cannot be read, or the installed bridge cannot start a run, the request is
staged rather than started: a task that waits costs a click, a second concurrent run costs
the loaded model. A caller that wants staging only says `start: "never"`.

Nothing is aborted and no progress is watched. WanGP's own validation decides whether a
task is taken, and WanGP's own queue and status show what it is doing.

## Setting it up

Open the tab. Until a folder is chosen the grid says so and the folder panel is open:
**Menu → Choose storage folder**, type a folder on the machine running Forge (not on the
device you are browsing from), and press *Use folder* — or *Create it* if it does not exist
yet. A folder that does not exist is never created behind your back; a path that is a file,
or nothing at all, is refused with its reason. Changing the folder later moves nothing: the
old folder keeps its files, the new one is read as it is.

Clipboard keeps its pictures **in that folder and nowhere else**. Files dropped into it by
hand appear on the next *Refresh* with a fresh id; a file renamed by hand keeps its id when
its bytes are the same bytes; a file removed by hand is gone from the grid on the next
Refresh, and every card and history entry that used it says so.

## The browser

**☰ Menu** holds everything the grid does not show:

* **Intercept "Send to Mini Paint"** — a switch. While it is on, the 🖌️ button under a
  txt2img / img2img / Extras result puts the picture *here* instead of on the Canvas, and
  the page switches to this tab. Off, the button does what it always did. The switch is
  saved on the Forge host, so a second browser and a Reload UI see the same setting.
* **Refresh** — read the folder again.
* **Sort ›** — by name, newest, oldest, largest, smallest. Also the dropdown on the toolbar.
* **Paste image** — reads your clipboard when the browser lets the page do that; otherwise
  a small panel opens with an ordinary paste box (Ctrl+V into it, or drop a file on it).
  Ctrl+V anywhere on the tab does the same.
* **Upload image(s)** — the file chooser, several at once.
* **Choose storage folder**, **Rename selected**, **Delete selected** — the file operations.
  A rename keeps the id and the extension and refuses a name with a separator, a leading
  dot, a reserved name or nothing before the extension; two files that want one name become
  `name` and `name (2)`. A delete asks first, removes the file, and leaves history entries
  that used it saying *Missing image*.
* **Send selected to ›** — Mini Paint, img2img, Inpaint, Extras, ImageStitch: the same
  routes the Canvas's own *Send to* takes, one picture at a time.
* **Queue Send History** — see below.

The toolbar has **+First**, **+Last**, **+Ref** (the selected picture into that card) and a
thumbnail-size slider (72–320 px, remembered). Tap a thumbnail to select it; drop one on a
card to assign it; drop a file from your desktop on a card to import it into the folder and
assign it in one go. Only PNG, JPEG and WebP are library files — an animated file, a text
file, or anything over the handoff ceilings (64 MiB, 16384 px on a side, 64 megapixels) is
refused with a sentence and never copied. A Forge PNG keeps every byte, so its generation
parameters survive; a paste and a *Send to Clipboard* from the Canvas become PNGs.

Nothing in the grid names the folder: every thumbnail is fetched by its opaque 32-character
id (`/minipaint-clipboard/image/<id>?thumb=1`), the file's name is shown as text, and the
route refuses an id that is not in the index, a name that is not a plain filename, a
symlink, and a file that has moved outside the folder (`CLIPBOARD_ASSET_OUTSIDE_ROOT`).

## The composer

Each card is in one of three states: **Use WanGP** (empty — inherit), a picture (override),
or **Missing image** (its file left the folder; press Refresh and it goes back to Use
WanGP). Each card has *Choose a file…* for a file that is not in the library yet, and × to
put it back to Use WanGP.

The prompt box's placeholder says what an empty box means: *Use current WanGP prompt*. Text
in it overrides the page's prompt for that one task — the prompt WanGP's generation
actually reads, so with WanGP's prompt wizard switched on it goes into the wizard's box.
Control characters are dropped, it is trimmed, and 12000 characters is the ceiling
(`PROMPT_TOO_LONG`; protocol 5 raised it from 4000 so that a written H3 prompt fits).

The line above the cards says what the live WanGP page can take right now — the model, the
inputs it takes, whether it is *generating now* (new requests join the run) or *idle* (the
next request starts one) — or why it cannot be asked (no WanGP page open, no model chosen,
the bridge not answering). It is asked when the tab opens, after every job ends and when a
card changes, never on a timer. A card holding a picture the current model cannot use (a
text-only model and a first frame, say) is **kept** and badged *Not used by current model*;
the request is still sent and WanGP is told to ignore that field. That is reported, not
refused.

**Add to Queue** is a button while the WanGP this extension manages is running. While it is
not, the button reads *WanGP is not running* and is off, and a press that reaches the
server anyway is refused with `WANGP_NOT_RUNNING` rather than stored — nothing is queued
for a process that may never come. Open the WanGP tab, let it start, and the button comes
back on the next refresh.

A press does this, in order:

1. The server turns the composer into a public request: the prompt if any, each filled
   card as a Clipboard asset **id** — never a path — and `start: "auto"`. A card whose file
   is gone stops here with a sentence; nothing has been stored and the rest of the draft
   is kept.
2. The request becomes a **job in the server's queue outbox**, owned by this browser page,
   and the press returns at once. Press again as often as you like; each press is another
   job in order.
3. This page's pump asks the server for its next job when it is its turn (one job is sent
   at a time across every browser page on this Forge, in the order pressed), and runs it
   through `window.minipaintInterop` — the same public API any other extension may call;
   the tab has no private shortcut. The API's server half turns each asset into an ordinary
   WanGP handoff (a lossless PNG under the runtime handoff root, named by a fresh id); the
   page asks the bridge inside WanGP to queue; the bridge writes the overrides *as
   replacements* into the page's own components, WanGP's `client_id` (set to the request
   id), and either WanGP's generate trigger (WanGP idle) or its add-to-queue trigger (WanGP
   busy, or the flag unreadable); WanGP's own chain runs.
4. The page asks the bridge to **confirm**, a few times over ten seconds: *started* when the
   request's task is the one at the head of a running loop, *queued* when a task in WanGP's
   queue carries the request id (with how many sit ahead of it), *refused* only when WanGP
   recorded an error for exactly that request, otherwise *unconfirmed*. The absence of a
   task is never treated as a refusal. Whichever way it ends, the overrides are put back —
   each one only where the page still holds what the bridge wrote, so an edit you made in
   WanGP meanwhile is yours and stays.
5. The page reports the outcome to the server, which re-renders the Queue and, for a
   started or queued job, writes the history record. The status line under the button
   says *WanGP started generating it.* or *Added to WanGP queue.* (with any ignored field
   named), or the sentence for the code; a toast says the same over the browser; the
   handoff files are released.

## Enhanced prompts (ModelSwitchRefiner)

Under the prompt box, a panel: **Prompt enhancement (ModelSwitchRefiner MiniMax H3)**. It
is off by default and does nothing until three things are true, and its first line says
which of them is not: the *SD-Neo-ModelSwitchRefiner* extension is installed in this Forge
(its external LLM API, `mc_llm_api`, is imported straight from that extension's folder -
there is no URL, port or token to configure), its **LLM Studio** is switched on with a
language model set up, and the WanGP page is on a **MiniMax H3** model - `minimax_h3_fl2va`
or `minimax_h3_ref2va`, or a finetune of one. The line follows the WanGP tab: switch the
model there and it changes here.

**What a press does with the switch on.** The prompt you typed - it is required; the WanGP
page's own prompt is WanGP's and is not read from here - goes to LLM Studio's MiniMax H3
writer at once, as the same request its own panel would make, for the variant the page is
on. The job waits in the Queue as **Enhancing**, showing the writer's own progress (*waiting
for the LLM (position 2)*, *Describing the image…*, *Writing the prompt…*), and the moment
the prompt is written it becomes *Waiting* carrying it and goes to WanGP in press order.
The card then shows both: the typed prompt struck through, the written one under it.

**The pictures follow the model.** The writer describes one picture and writes the prompt
about it; which one is the model's business, and a picture the model does not read is left
out of the enhancement rather than described in the wrong role. It still goes to WanGP with
the job; the card and the status line say what was left out.

| WanGP model | described | left out of the enhancement |
| --- | --- | --- |
| FL2VA (`minimax_h3_fl2va`) | the **First Frame** (the Last Frame is sent too; the writer says which one it described) | the Reference |
| Ref2VA (`minimax_h3_ref2va`) | the first **Reference** | the First and Last Frame (and any further reference) |

A picture at all needs a language model that can see; one that cannot refuses the press
(`ENHANCE_NO_VISION`), and the line says so beforehand.

**The system prompt.** The writer runs under one of four instruction sets - each variant,
with and without a picture - and the panel shows them: pick the variant and *Instructions
used*, and the box holds the text with its provenance under it (*Default, as
ModelSwitchRefiner ships it* or *Override saved*). Edit it and **Apply override** to replace
that set for every enhanced press from then on, on every page, after a restart too
(`clipboard-enhance.json`); **Restore default** forgets the override and shows the default
again; **Reload** re-reads whichever is current. A blank override is refused, not saved.
WanGP's own `@` and `@@` prompt dialect still applies on top, as the API documents.

**The line is strict.** Requests reach WanGP in the order pressed. A job whose prompt is
still being written holds every job behind it - a plain job pressed later, on any page,
waits - because "in the order requested" is the promise, and the language model works one
request at a time anyway. **Cancel everything** (below the queue heading) cancels every
waiting job at once - the writer's requests by this extension's own origin, then the pending
jobs; a job already being sent finishes, and nothing in WanGP's own queue is touched.

**How it can end.** The writer's own failure ends the job as *Refused* with its sentence
(`ENHANCE_FAILED`); a request cancelled from LLM Studio's own panel - it may be, at any time
- ends it *Cancelled* (`ENHANCE_CANCELLED`); a record the API has forgotten (its retention
ran out, or Forge restarted and its memory with it) is `ENHANCE_LOST`; a written prompt over
the 12000-character ceiling is `PROMPT_TOO_LONG`; and a page that moved to another model
between the press and the send is refused by the bridge with `MODEL_CHANGED`, because the
prompt was written for the model it left. **Retry** on any of these starts again from the
typed prompt and writes it again - except a prompt already written, which is carried over
rather than asked for twice, unless the failure was `MODEL_CHANGED`. Nothing is retried by
the machine.

**For other extensions**, `enqueue(request, { enhance: true })` asks for the same rewrite
(the page's model travels with the request; pass `{ model }` to name it yourself), and
`GET /minipaint-interop/enhance` says whether the switch is on, whether the LLM side can
take a request, and the slot rules.

## The Queue

Under the button, every job the server holds, newest first: its state (*Enhancing*,
*Waiting*, *Sending*, *Generating*, *Queued*, *Refused*, *Unconfirmed*, *Cancelled*), when
it was pressed, the prompt typed for it (or *Prompt: Use WanGP*; both prompts once it has
been enhanced), which fields it supplies, how it ended, and - on two lines of their own -
where its enhancement is (**LLM:**) and, once WanGP has it, where its task is inside WanGP
(**WanGP:** *accepted*, *in WanGP's queue, 2 ahead of it*, *WanGP is generating it*, *left
WanGP's queue (finished, or removed there)*, or *no longer tracked* when the page that
queued it was reloaded - a new WanGP session cannot vouch for the old one's tasks). The
page that queued a job asks the bridge every few seconds while its task is still in
WanGP's queue, and stops when it has left. And what a person may still do:

* **Cancel** — a waiting job, its enhancement with it. A job being sent is not ours to stop.
* **Cancel everything** — every waiting job at once, from every page (above).
* **Retry** — a refused or cancelled job, sent again *as a new request* by the page that
  pressed Retry. The machine never retries anything by itself.
* **Retry anyway** — an unconfirmed job. *Unconfirmed* means the bridge could not prove
  either way whether WanGP took the task, and a retry may queue it twice; look at WanGP's
  queue first. This is a rule, not a default.
* **Run from this page** — a waiting job composed on a browser page that is gone (closed,
  or not asking). A job is normally run only by the page that composed it, so that it
  inherits the WanGP settings its user is looking at; taking it over means it inherits
  *this* page's instead, which is why a person decides.

What that buys you with more than one browser: each page has its own identity (kept per
browser tab, so a reload resumes its jobs), all pages share one line at the server, one
job is sent at a time whichever page pressed it, a page that stops asking does not hold the
others up, and no page holds a queue of its own — a refresh, a closed tab or a second
browser cannot lose or duplicate work. A job whose page went away mid-send is marked
unconfirmed when the overlay had already been written and returned to waiting when it had
not; a job in flight when WanGP restarts is marked unconfirmed (`WANGP_RESTARTED`).

## Queue Send History

Every request **confirmed started or queued** from this tab is a record: when, on which
model, how many tasks, the prompt *if you typed one here* (an inherited prompt is recorded
as *Use WanGP*, never as WanGP's text) with the written prompt beside it when the press was
enhanced (*Load* puts the typed one back, so a new press writes it again), and each slot as
Use WanGP, the picture, or *ignored*. The last 200 are kept. **Load** puts a recipe back into the composer — a slot
whose picture is gone becomes Use WanGP and says so — and queues nothing. **Delete**
removes the record and touches no file. Nothing that was refused or unconfirmed is
recorded, and a request from another extension is in the Queue but never in this history.

## Working with Mini Paint

* The Canvas's **Menu → Send to** now lists **Clipboard**: the flattened picture goes into
  the folder as a PNG named after the document (`minipaint.png` when it has no name), the
  status line says as what, and the Canvas keeps its document. It does not switch tabs.
* **Send selected to › Mini Paint** hands the picture to the Canvas through the Canvas's
  own receive chain (Layer 1 over a Background, one Undo away) and switches to it.
* With the **intercept** on, the gallery's 🖌️ button imports the original output file when
  Forge proves which file it is (its generation parameters survive) and the decoded pixels
  as a PNG otherwise, and switches to the Clipboard tab. If Clipboard cannot take the
  picture — no folder chosen, a refused file — it goes to the Canvas as before, with the
  reason on the Canvas's status line.

## When the page loses its connection

Sending a picture out used to be two halves: a hidden textbox written by
script, and a Gradio event carrying it over the queue. The first half always
worked; the second could stop delivering - a connection that dropped and did
not come back, a session the server has forgotten - and then the menu item did
nothing at all. No picture, no error, no status. Everything else the tab does
for a running job kept working through exactly that failure, because the
queue, the event stream, the imports and the thumbnails all ride ordinary
HTTP. Only the actions were tied to Gradio.

They are not any more:

* **The page places the picture itself, first.** The send used to be handed
  to Gradio, with the direct route kept back as a fallback the page tried
  after twelve seconds of silence. On a phone that is the wrong way round:
  the logs from one are full of *"the page went to the background"* and
  *"back on screen after 2386s"*, and Gradio's event stream does not survive
  being backgrounded - so the queue there is not an occasional casualty, it
  is down more often than not, and every send was paying twelve seconds to
  rediscover that. The picture now goes over the route and the page's own
  DOM straight away, which takes about as long as one request.

* The queued event still goes, marked as already delivered, so the server
  records the send - the status line, the history, the log - without writing
  the destination a second time. Nothing waits on it.

* Every send is acknowledged. The server echoes back the stamp the browser put
  on its own request, so the page can tell "refused" from "never heard" -
  which it could not before, and which is why the failure was so hard to
  place. That receipt is now diagnostic rather than something the user waits
  on: when it does not arrive, the page says the composer's live channel is
  down, and names which parts of the tab that leaves stale.

* The delivery goes over `POST /minipaint-clipboard/send`, and **every
  destination finishes**.
  **img2img** and **Inpaint** are delivered by writing the host canvas's
  hidden textbox. **Extras** and the two **ImageStitch** galleries hold their
  value in the component, which the server fills by returning a new value -
  a Gradio event, and so the queue - so the browser hands the component its
  file instead, over the upload route. The toast says the page had lost its
  connection, so a picture arriving the long way round is not a mystery.

* The picture being sent is held for the whole round trip. The fallback runs
  twelve seconds after the menu was pressed, and it used to look the
  selection up again when it got there - so a render landing in between left
  it with nothing to send, and it returned without a word.

* A render cannot take the selection away. A selection is made in the browser
  and reaches the server as a Gradio event, so while the queue is not
  delivering, every render carries back a server that still believes nothing
  is selected. Adopting that answer is how a Send menu came to have every
  destination greyed out with a picture plainly selected on the grid. The
  page keeps its own selection now, and gives it up only when the grid is
  listing pictures and the selected one is not among them.

* The line offers to **check again**, never to reload. It offered a reload
  once, and on a Forge behind its own TLS front end reloading took the whole
  WebUI page with it - the session gone, for a line that is only ever
  advisory. Checking asks the server for the library over HTTP and presses
  Refresh, and clears the line if either answers.

* The first send to go unanswered says so and leaves a line standing. It no
  longer says *"This page has lost its live connection to Forge"*, which was
  raised whenever a framework's event stream sulked - constantly, on a
  machine where Forge is running, and meaning almost nothing. There are two
  sentences now and no third: **Forge is not answering**, raised only when an
  HTTP request to this extension's own routes actually fails; and **the
  composer's live channel to Forge is down on this page**, which says that
  browsing, paging, sorting, selecting, sending and the queue list all still
  work - they do not use it - and names the parts that will not update until
  it comes back. Sends after an unanswered one stop waiting twelve seconds
  for an answer that is not coming: they try the direct route after two and a
  half. The first acknowledgement to arrive clears both.

* **A destination never has to be opened first, and a verified send opens it.**
  The picture goes into the destination's own component while its tab is
  hidden - hidden is not absent - the transfer is checked to have actually
  landed, and only then is that tab shown. That is how Forge's own result
  buttons behave. A send that fails leaves you in Clipboard with the reason;
  a picture proved to have landed in a tab that would not open says
  *"Sent x.png to img2img, but could not open that tab."* and is **never sent
  again**, because sending it twice is one picture too many in a gallery that
  appends. Mini Paint keeps the same contract although its delivery is still
  a Gradio event: it does not have to be visited first, a receive that landed
  shows it, and one that did not leaves you here.

* A picture handed **in** from another tab arrives too. The button under a
  txt2img, img2img or Extras result picks its picture in the browser and
  hands it to the server as a Gradio event, so it stopped for the same
  reason sending out did. The page now watches for it to arrive - the tab
  switch at the end of the receive is the acknowledgement - and puts it in
  the library itself when it does not, by fetching the file the host is
  already serving and posting it to the import route. That keeps the
  metadata Forge wrote into a generated PNG, which a re-encode of what is on
  screen would lose. Only when the intercept is on: with it off the picture
  was going to the Canvas, whose document lives on the server, and the page
  says so rather than putting it somewhere else.

Reloading the page still makes sending quick again, and the Reconnect button
is there to do it. The difference is that sending works either way.

### Writing one of the host's inputs

Most of what this tab asks the server to do crosses the same way: the browser
writes a hidden Gradio textbox and the event bound to it fires. That write
goes through the input's own prototype setter, never `element.value = ...`.

The difference is not cosmetic. Gradio's inputs are owned by its framework,
and a framework keeps its own record of what an input holds; an assignment
writes the DOM and leaves that record untouched, so the framework can compare
the two, see no change, and send nothing. The write succeeds and the event
never happens - and from the page's side there is nothing to report, because
the write worked.

**None of that was the fault, and here is what was.** Five builds went on
sending, and the page's own report named the cause in one line the first time
it was asked:

```
request box: 1 element(s), 1 component(s), wired for input
             but naming 1 component(s) NOT ON THIS PAGE,
             which is an event the host cannot run
```

The send event named, among its outputs, a component that no page contained.
Gradio does not complain about that - it simply never runs the event. No
error, no failed request, no console line: the control is there, the write
lands, and nothing answers, on every build, for ever, while every other event
on the same tab works.

Where the component came from: destinations belong to other tabs and are
remembered as the host builds its UI, and the hook that remembers one fires
when a component is CREATED. Creating is not placing. Gradio has
``render=False``, a scratch context is a normal thing to build in, and a
script can build a component for one tab and place only the copy it made for
another. ``host.destinations`` now asks Gradio's own ``is_rendered`` and
drops anything that was never put anywhere.

And the severity was not the missing destination. Every send named every
backend destination in its outputs, so one component nobody rendered stopped
sending to *all* of them - including img2img, a canvas plainly on the page,
which the browser was writing perfectly well on its own. So ``send`` names
nothing but this tab's own boxes now and can always run, and the outputs that
carry that risk moved to ``send_backend``, pressed only when the page has
just found out it cannot place the picture itself. A destination that is not
on the page costs that destination.

A send also writes the box *and* presses a hidden button
(`minipaint_clipboard_send_press`), and posts the request to the tab's own
HTTP route. That was built for a different theory of the fault and is kept,
because none of the three costs anything and no two of them fail together:
the box is the request, the press only says "read it", and the route is where
the request waits when the framework is holding a stale value for that box.
`ClipboardTab.send` remembers the requests it has answered, so whichever
arrives first delivers the picture and any later arrival gets the same
receipt. Remembering more than the last one is deliberate: a press carries
whatever value the framework holds for the box, which on an unlucky page may
be a request from several sends ago - and re-delivering *that* would put a
picture the user has moved on from into their canvas.

The suite checks each against the failure it is for: a destination built but
never rendered must not be offered and must not break the other destinations,
and the send must still arrive with the page made deaf to the write.

### When something crosses to the server and nothing happens

Three different faults look identical from inside the page: the event never
fired, it fired and the request failed, or it fired and the answer never came
back. Nothing in any log separated them, which is how four builds went on
guessing. The page now collects the three facts that do, and writes them into
`logs/wangp-log.txt` when a send goes unanswered and when **Check again** on
the connection line comes back empty:

* **what the page holds** - whether the request box took the write, and
  whether the receipt box is empty or holds some *other* send's stamp. An
  older stamp means the press arrived carrying a value the framework never
  updated, which is a different fault from the press not arriving at all.
* **what left the browser** - every call the host's own framework put on the
  wire in that window, from `PerformanceObserver`, which is read-only and
  cannot become the fault it is diagnosing. "No request left this browser"
  means the event never fired and there is nothing to look for server-side.
* **where the host told the page to call it** - Gradio's frontend does not use
  relative URLs; it reads an absolute root out of the config the server
  inlined. A Forge behind anything that terminates TLS - a front end, a
  tunnel, a browser extension that upgrades the address bar - can serve a
  page over `https` whose config says `http`, and the browser then blocks
  every framework call as mixed content while everything this extension does
  over a relative URL keeps working perfectly. That is reported, not
  repaired: the fix is to tell Forge it is behind TLS (an `x-forwarded-proto`
  header from whatever terminates it, or `--subpath`/`root_path`), and an
  extension that quietly rewrote the host's own config would be one upgrade
  away from breaking an install that was fine.

### How a picture is actually put into a destination

Both directions go through the transfer library the legacy editor has always
used (`miniPaint/src/js/libs/webui-host.js`), served to the page rather than
copied, so there is one implementation of "put this picture there" and not
two that drift.

It does what writing a value cannot: it classifies a destination by what is
inside it rather than assuming; primes a ForgeCanvas so its first re-encode
has a frame to draw; clears a scribble that would otherwise be sent along
with a new picture of the same size; writes through the native value setter,
which is what a framework listens to; clears a Gradio image before handing
its upload input a file, because a component that already holds a picture
has no upload input to hand it to; and then reads back what the WebUI will
actually submit, compares it with what was sent, and tries again when they
differ.

A send to img2img or Inpaint also settles the img2img sub-tab, because
img2img generates from the slot its own hidden mode value names and that
value only moves on a server round trip - the "my image is right there and
it was ignored" case.

## Where things live

| what | where |
| --- | --- |
| the folder, the intercept switch, the sort and the thumbnail size | `<Forge data_path>/a1111-mini-paint-NEO/clipboard.json` |
| the index: id, filename, size, dimensions, digest, source per file | `…/clipboard-index.json` (rebuilt from the folder on Refresh) |
| the composer's draft | `…/clipboard-draft.json` |
| the queue outbox: every press as a job, with its request, its state, its enhancement and its place in WanGP | `…/clipboard-outbox.json` (schema 2; terminal jobs kept a week, the list capped) |
| the enhanced-prompts switch and the system prompt overrides | `…/clipboard-enhance.json` |
| Queue Send History | `…/clipboard-history.json` |
| the pictures | the storage folder you chose, and only there |
| thumbnails | in memory, rebuilt as needed; never on disk |
| images staged through the public API | `…/runtime/staging/<32 hex>.png`, swept after 30 minutes |
| images prepared for one queue request | `…/runtime/handoff/<32 hex>.png`, released when the request ends |
| every step, scrubbed | `extensions/a1111-mini-paint-NEO/logs/wangp-log.txt` (`clipboard`, `outbox`, `enhance`, `interop`, `browser` and `wangp` columns) |

A document that will not parse is moved aside as `<name>.broken-<stamp>.json` and started
afresh, and the console says so; nothing is ever half-written (every write is atomic).

## What is deliberately not stored or logged

* No **path** leaves the server: the browser, the public API and the wire protocol see ids.
* No **prompt text** is logged, anywhere, by any side — the journal says `overrides prompt`,
  never what it was. WanGP's own prompt is never copied into a Clipboard file.
* No **filename** is logged; the journal names an asset by the first eight characters of
  its id, a job by the first eight of its.
* The **request id**, the staged **tokens**, the handoff ids and the outbox **lease tokens**
  are in no config file and never shown to a page other than the one they were issued to.
* The **draft** and the **history** hold a prompt only when you typed one into the tab. The
  **outbox** holds the prompt of a job because the job is a snapshot of the composer at
  press time and must survive the composer changing afterwards; it is shown in the Queue,
  on your own screen, and nowhere else. A **written prompt** joins it there once the
  writer is done, and the history keeps it beside the typed one.
* A **system prompt override** is in `clipboard-enhance.json` and nowhere else; the journal
  says an override was applied or went with a request, never its text. What the writer
  itself keeps (its *Saved prompts*) is that extension's own, filed exactly as a run from
  its panel would be, under the origin `minipaint-clipboard`.

## Reading a failure code

The codes and their sentences are in `minipaint_neo/wangp/errors.py`, beside the WanGP
tab's own; the tab, the toast and the log say the same thing about the same failure.

| code | what it means |
| --- | --- |
| `WANGP_NOT_RUNNING` | the WanGP this extension manages is not serving. The button is off; a press is refused and nothing is stored. Open the WanGP tab and let it start. |
| `CLIPBOARD_NOT_CONFIGURED` | no storage folder yet. Menu → Choose storage folder. |
| `CLIPBOARD_ASSET_UNKNOWN` | that id is not in the index — the file left the folder. Refresh. |
| `CLIPBOARD_ASSET_OUTSIDE_ROOT` | the indexed entry no longer resolves inside the folder (a symlink, a move). Not used. |
| `REQUEST_INVALID` | the request is not one the API carries: a bad id, a wrong kind, a start mode that is not `auto` or `never`, an unreadable shape. |
| `PROMPT_TOO_LONG` | over 12000 characters after cleaning - a typed prompt, or a written one. |
| `IMAGE_STAGE_INVALID` / `IMAGE_STAGE_EXPIRED` | the bytes were not a PNG/JPEG/WebP within the ceilings, or the staged file was swept. |
| `IFRAME_NOT_READY` | no live WanGP page in this Forge tab. The job waits; open the WanGP tab and choose a model. |
| `BRIDGE_COMPONENT_INCOMPATIBLE` | the bridge inside this WanGP build lacks one of the six queue components; image send still works, the queue does not. |
| `QUEUE_BUSY` | the previous request from this page is still being confirmed, or the outbox holds as many waiting jobs as it will take. |
| `QUEUE_JOB_PENDING` | an `enqueue()` wait ran out while its job was still waiting its turn; the job id is in the answer and the job is still in the Queue. |
| `QUEUE_JOB_UNKNOWN` | that job is no longer in the outbox (pruned, or never there). |
| `REQUEST_ID_CONFLICT` | a request id was reused for a different payload. Mint a new id. |
| `QUEUE_REQUEST_REFUSED` | the bridge did not take the request; the code it named is in the log. |
| `WANGP_VALIDATION_REFUSED` | WanGP's own validation declined it; the WanGP page has the detail. |
| `ADMISSION_UNCONFIRMED` | no task carrying the request appeared within the wait and WanGP recorded no error. It may still be queued: look at WanGP's queue before *Retry anyway*. |
| `WANGP_RESTARTED` | WanGP restarted while the job was on its way; it is unconfirmed for the same reason. |
| `BRIDGE_SESSION_MISMATCH` | the request was prepared for another WanGP page. |
| `MODEL_CHANGED` | the request was composed - its prompt written - for a model the WanGP page has since left. Retry writes it again for the model the page is on. |
| `ENHANCE_UNAVAILABLE` | ModelSwitchRefiner is not installed here, or its LLM Studio is switched off or has no model set up. The press is refused and nothing stored. |
| `ENHANCE_MODEL_UNSUPPORTED` | the WanGP page is not on a MiniMax H3 model. Load FL2VA or Ref2VA there, or switch enhancement off. |
| `ENHANCE_PROMPT_REQUIRED` | enhancement needs a prompt typed here; the page's own prompt is not read. |
| `ENHANCE_NO_VISION` | a picture was to be described and the language model running cannot see. |
| `ENHANCE_IMAGE_UNREADABLE` | a picture could not be read for the writer. |
| `ENHANCE_QUEUE_FULL` | LLM Studio's own line is full (32). Try again in a moment. |
| `ENHANCE_SYSTEM_PROMPT_EMPTY` | a blank override; Restore default instead. |
| `ENHANCE_REFUSED` / `ENHANCE_FAILED` / `ENHANCE_CANCELLED` / `ENHANCE_LOST` | the writer refused the request, its run failed, it was cancelled (here, or in LLM Studio's panel), or its record was forgotten before the prompt was collected. Retry starts from the typed prompt. |

## Theming

`style.css` scopes every Clipboard rule to `#minipaint_clipboard_root` and decides layout
and sizes only. Colours come from Gradio's theme variables — `--body-text-color`,
`--background-fill-primary`, `--block-background-fill`, `--border-color-primary`,
`--color-accent`, `--error-text-color` and their kin — with neutral, translucent fallbacks
for a theme that lacks one; `tests/test_clipboard_ui.py` refuses a white, a `#fff`, a
black or a fixed background in that block, the queue list included. The thumbnail size is
one CSS variable the browser sets from the slider. On a narrow tab the two columns stack.

## The public API for other extensions

`window.minipaintInterop` is on every Forge page once the extension has loaded. It is
versioned, contract `minipaint.wangp.queue/v1`, and it is the only way into the queue —
the Clipboard tab uses it too, and its presses and a caller's requests share one line.

```js
const api = window.minipaintInterop;              // { version: 1, contract, wangp: {...}, message(code) }

const caps = await api.wangp.capabilities();      // advisory: { ok, ready, queue, start, track, generation_running, model, inputs: { start, end, references } }
const llm = await api.wangp.enhance();            // { ok, enabled, capabilities: { available, vision, model, reason }, variants, slots, overrides }

const staged = await api.wangp.stageImage(blob);  // a Blob, File, ArrayBuffer, canvas or data: URL
// -> { ok: true, image: { kind: "staged", id: "<32 hex>" }, width, height }  (the bytes never cross postMessage)

const result = await api.wangp.enqueue({
    prompt: "a lighthouse at dusk",               // omit, null or "" -> the page's own prompt
    images: {
        start: staged.image,                      // { kind: "staged", id } or { kind: "clipboard_asset", id }
        references: [staged.image]                // up to 16; omit -> the page's own
        // end omitted -> the page's own
    },
    start: "auto"                                 // the default: start WanGP if idle, join it if not; "never" stages only
}, { enhance: true });                            // write the prompt with MiniMax H3 first (omit: the tab's switch decides); the page's model travels with it
// -> { ok: true,  status: "started" | "queued", request_id, job_id, tasks_added, queue_depth, route, model, applied, inherited, ignored, enhanced, wangp }
// -> { ok: false, status: "refused" | "unconfirmed" | "pending", request_id, job_id, code, message }

const ticket = await api.wangp.enqueue(request, { wait: false });   // -> { ok: false, status: "pending", job_id, code: "QUEUE_JOB_PENDING" } at once
const all = await api.wangp.jobs();                                // { ok, running, jobs: [...] }  each job: state, enhance {state, stage, ...}, wangp {state, position}
await api.wangp.cancel(jobId); await api.wangp.retry(jobId); await api.wangp.adopt(jobId);
await api.wangp.cancelAll();                                       // every waiting job, from every page -> { ok, cancelled, enhancing, in_flight, jobs }
api.wangp.pump();                                                  // run this page's waiting jobs now (idempotent)
document.addEventListener("minipaint:outbox", e => e.detail);      // { kind: submitted|sending|done|changed|stopped|waiting|tracked, job, page }
```

What the contract promises:

* **Sparse.** Omitted, `null` and empty mean *inherit*. Only `prompt`, `images.start`,
  `images.end`, `images.references` and `start` exist; anything else is `REQUEST_INVALID`.
* **Ids, never paths.** A handle is a kind and a 32-lowercase-hex id; `stageImage` is how
  bytes become one. `GET /minipaint-interop/contract` says the version, the ceilings, the
  kinds, the start modes and where the outbox is.
* **One line, server-owned.** `enqueue()` submits a job; the server sends one job at a time
  across every page in submission order; this page runs only its own jobs, against the WanGP
  page in this document. `enqueue()` resolves when the job has ended, or after `timeoutMs`
  (five minutes by default) with `pending` and the job id; `{wait: false}` returns the job
  id at once.
* **Idempotent by id.** Pass your own `request_id` (32 lowercase hex) to make a retry safe;
  omit it and one is minted. The same id with another payload — a different start mode
  included — is `REQUEST_ID_CONFLICT`.
* **Honest answers.** `started` and `queued` only on WanGP's own evidence, `refused` only
  on WanGP's correlated refusal or the bridge's own, `unconfirmed` otherwise; never a retry
  the machine decided on.
* **Refused when WanGP is not running.** `WANGP_NOT_RUNNING`, and nothing stored.
* **Enhanced on request, refused when it cannot be.** `{ enhance: true }` is refused - and
  nothing stored - with `ENHANCE_UNAVAILABLE`, `ENHANCE_MODEL_UNSUPPORTED`,
  `ENHANCE_PROMPT_REQUIRED` or `ENHANCE_NO_VISION` rather than queued as typed. A job
  composed for one model insists on it (`MODEL_CHANGED` otherwise).
* **Tracked by the page that queued it.** After `queued` or `started`, this page asks the
  bridge where the task is every few seconds until it has left WanGP's queue; `jobs()`
  shows it under `wangp`, and a `tracked` event fires on each move.
* **Not a generation API.** No abort, no progress bar, no model or setting; a request
  starts a run only when WanGP is idle and only through WanGP's own generate trigger.

The routes behind it (`/minipaint-interop/stage`, `/prepare`, `/release`, `/contract`,
`/enhance`, `/minipaint-interop/outbox` and its `/submit`, `/claim`, `/report`, `/cancel`,
`/cancel_all`, `/retry`, `/adopt`, `/track`, `/minipaint-clipboard/image/…`,
`/minipaint-clipboard/import`) sit behind the same sign-in gate as the WanGP proxy: when
Forge has a login, a request without it is refused.

From Python, the same halves are `minipaint_neo.interop.stage_image(pil)`,
`stage_bytes(data, content_type)`, `normalize_public_request(raw)`, `prepare(request)` and
`release(ids)`; the outbox is `minipaint_neo.clipboard.outbox` (`submit`, `claim`, `report`,
`track`, `cancel`, `cancel_all`, `retry`, `adopt`, `jobs`); the enhancer is
`minipaint_neo.clipboard.enhance` (`enabled`, `set_enabled`, `capabilities`,
`variant_for_model`, `plan`, `override`, `set_override`, `clear_override`); the Clipboard
library is `minipaint_neo.clipboard.store.store()`.

## Known limits

* **Not yet proven on real hardware.** Like the WanGP tab, the queue was written against
  Wan2GP's source (commit `362c346`: `add_to_queue_trigger.change` runs
  `validate_wizard_prompt → save_inputs → process_prompt_and_add_tasks`,
  `generate_trigger.change` runs the same and then `process_tasks`, `save_inputs` copies
  `client_id` into every task, and `is_generation_in_progress()` is the module function
  `process_tasks` raises). The shape of `queue_errors` is read both ways it has been seen
  and is marked `VERIFY ON A REAL INSTALL`; a build with neither shape answers *unconfirmed*
  for a failed validation rather than the wrong thing. `docs/wangp/PHASE0.md` has the checks.
* **One request at a time.** By design: across every page, one job is sent at a time;
  within one WanGP page, one request owns the form until it is confirmed (`QUEUE_BUSY`).
* **A person and the bridge in the same instant.** The bridge decides from WanGP's flag and
  writes the trigger a round trip later; a person pressing Generate inside that window
  could start a second run. The window is small, it is logged, and WanGP's own page-level
  guard covers the page the request ran in. It is not excluded.
* **A page that is gone keeps its jobs.** Jobs run on the page that composed them; a closed
  tab's waiting jobs wait for *Run from this page*, on purpose.
* **The browser's clipboard.** Reading it directly needs a browser that allows the page to;
  the paste panel is the fallback and always works.
* **Gradio's cache is lossy.** A picture written into a WanGP gallery comes back from Gradio
  as a WebP in its cache, so the bridge compares galleries by a coarse signature with a
  tolerance, not by exact pixels; a picture you swapped meanwhile is still recognised as
  yours.
* **Playwright.** The browser smoke test (`tests/browser_smoke.py`) does not yet cover the
  Clipboard tab, the pump or the tracking loop.
* **ModelSwitchRefiner's API is written to its document, not yet run against it.** The
  enhancer follows `docs/21-external-llm-api.md` of that repository (`submit_minimax`,
  `status`, `cancel`, `cancel_all` by origin, `capabilities`, the four system prompts) and
  is exercised here against a fake shaped like it; the H3 model types it recognises
  (`minimax_h3_fl2va`, `minimax_h3_ref2va`, or a finetune whose architecture names one) are
  the names that extension itself documents. `docs/wangp/PHASE0.md` Part 6 has the checks.
* **Tracking is the page's.** Only the page that queued a job can ask WanGP where its task
  is, because the task lives in that page's WanGP session; reload the page and the job is
  *no longer tracked*, honestly, rather than reported from a session that never saw it.
  WanGP does not say whether a task that left its queue finished or was removed, so
  neither does the card.
* **An enhancement holds the line.** By design: a plain job pressed after an enhanced one
  waits for it, on every page, so that requests reach WanGP in the order pressed.
