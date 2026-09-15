"""The Clipboard tab: a small file browser on the left, a small WanGP request
composer on the right.

Everything on the page is an ordinary Gradio component, coloured by the
host's theme variables and nothing else, so a theme's night mode reaches
every corner of it. The thumbnail grid, the three slot cards and the history
list are server-rendered HTML the browser script only listens to; each of
their buttons writes one hidden textbox or presses one hidden button, so
every action is still a Gradio event with the page's own values as inputs.

The composer is the whole product, and it is built around one rule. An
empty prompt means "use the WanGP page's prompt"; an empty slot means "use
the WanGP page's image for that input"; clearing a slot returns it to that,
and never clears anything on the WanGP page. Add to Queue is always a
button while WanGP is running: an entirely empty composition asks the live
WanGP page to queue itself exactly as it is, and a slot the current model
cannot use is kept, badged, and reported - not a reason to refuse. A press
appends a job to the server's queue outbox and returns at once; the page
pumps its own jobs through ``window.minipaintInterop`` - the public API any
extension may call - one at a time, in the order they were pressed across
every browser, and the server re-renders the job list as they go. While
WanGP is not running the button says so and is off.

Prompt enhancement sits under the prompt, off by default. Switched on, a
press first hands the typed prompt - and the pictures the model reads - to
ModelSwitchRefiner's MiniMax H3 writer, for the H3 variant the WanGP page
is on; the job waits in the queue as *enhancing* and goes to WanGP, in
press order, once the prompt is written. The same panel shows and edits the
four system prompts the writer uses (with and without a picture, for each
variant): the default is read from the other extension, an override is
kept on disk across sessions, and Restore default forgets it. Each job in
the list says where its enhancement is and, once WanGP has it, where its
task is in WanGP's queue; Cancel everything empties the line at once.

The tab is built once, inside the same guard the WanGP tab uses, and a tab
that cannot be built is a tab that says so under the same label and id.
"""

from __future__ import annotations

import collections
import contextlib
import html
import json
import os
import secrets
import typing

import gradio as gr

from .. import scrub
from ..canvas import host, imaging
from ..canvas import ui as canvas_ui
from ..wangp import errors, protocol
from ..wangp.errors import IntegrationError
from . import TAB_ID, TAB_LABEL, config, enhance, history, outbox, routes, store

#: How many answered send requests a tab remembers. Enough to cover a
#: page whose framework is several sends behind, small enough that the
#: record is never worth thinking about. See ``ClipboardTab.send``.
_ANSWERED_KEPT = 16

PREFIX = "minipaint_clipboard"


def _id(name: str) -> str:
    return f"{PREFIX}_{name}"


# Every browser-side helper lives in javascript/minipaint_clipboard.js. Each
# is called from a Gradio event on a user action, or from the page's load
# event, and none of them runs on a timer for longer than one bounded wait.
_JS = "window.minipaintClipboard"
ATTACH_JS = f"() => {{ if ({_JS}) {_JS}.attach(); }}"


def _attach_with_bundles_js() -> str:
    """Fetch this tab's browser half when it is first opened, then attach it.

    THE ONLY BUNDLE IN THIS EXTENSION THAT CAN BE TRULY LAZY, and it is worth
    saying why the others cannot. The Canvas adapter is reached from outside
    its tab - "Send to Mini Paint" on the txt2img output row runs in the
    browser before the Canvas has ever been opened - and the WanGP bridge is
    reached from the Canvas. Nothing outside this tab addresses this one, so
    a session that never opens Clipboard never parses it.

    It brings the queue API and the WanGP bridge with it, because it calls
    both; they are idempotent by URL, so a page that already has them from
    the Canvas pays a dictionary lookup.
    """
    from .. import assets
    from . import TAB_ID

    return (
        "async () => { "
        f"await ({assets.tab_loader_js(TAB_ID, ['wangp', 'interop', 'clipboard'])})(); "
        f"await ({assets.module_js('host')})(); "
        f"if ({_JS}) {_JS}.attach(); "
        "}"
    )


ATTACH_WITH_BUNDLES_JS = _attach_with_bundles_js()
MENU_JS = f"() => {{ if ({_JS}) {_JS}.toggleMenu(); }}"
THUMB_JS = f"(size) => {{ if ({_JS}) {_JS}.setThumbnailSize(size); }}"
# The queue instruction box changes when the server has built a request;
# the browser hands it to the public API and writes the result back.
QUEUE_JS = f"(instruction) => {{ if ({_JS}) {_JS}.queue(instruction); }}"
# The click hands the server this page's identity along with the prompt, so
# the job is the page's to run; the watcher is armed in the same breath.
ARM_QUEUE_JS = (f"(prompt, page, model) => {{ const c = {_JS}; if (c) {{ c.armQueue(); }} "
                f"return [prompt, c && c.pageId ? c.pageId() : page, c && c.modelJson ? c.modelJson() : model]; }}")
# After the server has cancelled the line: callers of the public API still
# waiting on those jobs are answered, and the list is re-read.
AFTER_CANCEL_JS = f"() => {{ if ({_JS} && {_JS}.afterCancelAll) {_JS}.afterCancelAll(); }}"
SELECTED_JS = f"(grid, selected) => {{ if ({_JS}) {_JS}.afterRender(); }}"
SWITCH_JS = "(target) => { if (window.minipaintCanvas && window.minipaintCanvas.switchTo) { window.minipaintCanvas.switchTo(target); } }"
CAPABILITIES_JS = f"() => {{ if ({_JS}) {_JS}.refreshCapabilities(); }}"

#: The three slots: the composer's name for each, its card title, and the
#: public request field it fills.
SLOTS: typing.Tuple[typing.Tuple[str, str, str], ...] = (
    ("first", "First Frame", protocol.QUEUE_FIELD_START),
    ("last", "Last Frame", protocol.QUEUE_FIELD_END),
    ("ref", "Reference", protocol.QUEUE_FIELD_REFERENCES),
)
SLOT_KEYS = {"first": "first_asset_id", "last": "last_asset_id", "ref": "reference_asset_ids"}
FIELD_LABELS = {
    protocol.QUEUE_FIELD_PROMPT: "The prompt",
    protocol.QUEUE_FIELD_START: "The first frame",
    protocol.QUEUE_FIELD_END: "The last frame",
    protocol.QUEUE_FIELD_REFERENCES: "The reference",
}

#: What the job list calls each state, and what the button says.
OUTBOX_LABELS = {
    outbox.ENHANCING: "Enhancing", outbox.PENDING: "Waiting", outbox.SENDING: "Sending", outbox.QUEUED: "Queued", outbox.STARTED: "Generating",
    outbox.FAILED: "Refused", outbox.UNCONFIRMED: "Unconfirmed", outbox.CANCELLED: "Cancelled",
    # The server-executed stages. Each names what the job is waiting for
    # rather than where it is in a handshake, because that is the only thing
    # somebody who walked away and came back can usefully be told.
    outbox.ADMITTED: "Queued", outbox.WAITING_TURN: "Waiting", outbox.ENHANCED: "Enhanced",
    outbox.ENSURING_WANGP: "Starting WanGP", outbox.COMPOSING: "Reading settings",
    outbox.WAITING_FOR_CARD: "Waiting for the card", outbox.SUBMITTING_WANGP: "Submitting",
    outbox.GENERATION_WAITING: "In WanGP's queue", outbox.GENERATION_RUNNING: "Generating",
    outbox.COMPLETED: "Done", outbox.EXECUTION_UNKNOWN: "Unknown",
}
#: What the list says of a job's enhancement, by the LLM side's state.
ENHANCE_LABELS = {
    enhance.LLM_QUEUED: "waiting for the LLM", enhance.LLM_RUNNING: "being written", enhance.LLM_DONE: "enhanced",
    enhance.LLM_FAILED: "failed", enhance.LLM_CANCELLED: "cancelled", "lost": "lost",
}
#: What the list says of a queued job's task inside WanGP.
WANGP_LABELS = {
    outbox.WANGP_ACCEPTED: "accepted by WanGP", outbox.WANGP_WAITING: "in WanGP's queue", outbox.WANGP_GENERATING: "WanGP is generating it",
    outbox.WANGP_FINISHED: "left WanGP's queue (finished, or removed there)", outbox.WANGP_UNKNOWN: "no longer tracked (the WanGP page changed)",
}
SP_VARIANT_CHOICES = [(enhance.VARIANT_LABELS[variant], variant) for variant in enhance.VARIANTS]
SP_MODE_CHOICES = [(enhance.MODE_LABELS[mode], mode) for mode in enhance.MODES]
QUEUE_BUTTON_LABEL = "Add to Queue"
QUEUE_BUTTON_BLOCKED = "WanGP is not running"
#: The page id a press carries when no browser script supplied one. Jobs
#: under it are shown as waiting for a page that never pumps, with "Run
#: from this page" offered.
NO_PAGE = "00000000"
#: How many jobs the list shows, newest first.
OUTBOX_SHOWN = 40

_LOG_PREFIX = "MiniPaint Clipboard:"


# --------------------------------------------------------------- rendering --


def _escape(text: typing.Any) -> str:
    return html.escape(str(text or ""), quote=True)


def _size_text(asset: store.Asset) -> str:
    size = asset.size_bytes
    if size >= 1024 * 1024:
        human = f"{size / (1024 * 1024):.1f} MB"
    elif size >= 1024:
        human = f"{size / 1024:.0f} KB"
    else:
        human = f"{size} B"
    return f"{asset.width} × {asset.height} · {human}"


def grid_html(assets: typing.Sequence[store.Asset], selected: str, configured: bool) -> str:
    """The browser body: one button per picture, the id in data, the name shown."""
    if not configured:
        return (
            '<div class="minipaint-clip-grid minipaint-clip-empty" data-count="0">'
            "<p><b>No storage folder yet.</b> Menu → <em>Choose storage folder</em> picks a folder on the machine "
            "running Forge; Clipboard keeps its pictures there.</p></div>"
        )
    if not assets:
        return (
            '<div class="minipaint-clip-grid minipaint-clip-empty" data-count="0">'
            "<p>No images yet. Upload or paste one from the menu, send one from Mini Paint, or turn on "
            "<em>Intercept “Send to Mini Paint”</em> and press 🖌️ under a result.</p></div>"
        )
    parts = [f'<div class="minipaint-clip-grid" role="listbox" aria-label="Clipboard images" data-count="{len(assets)}">']
    for asset in assets:
        chosen = " minipaint-clip-selected" if asset.asset_id == selected else ""
        title = f"{asset.filename} · {_size_text(asset)}"
        parts.append(
            f'<button type="button" class="minipaint-clip-item{chosen}" role="option" aria-selected="{"true" if chosen else "false"}" '
            f'data-asset="{_escape(asset.asset_id)}" data-name="{_escape(asset.filename)}" title="{_escape(title)}">'
            f'<span class="minipaint-clip-thumb"><img src="{_escape(routes.image_url(asset.asset_id, version=asset.mtime_ns))}" '
            f'alt="" loading="lazy" draggable="false"></span>'
            f'<span class="minipaint-clip-name">{_escape(asset.filename)}</span></button>'
        )
    parts.append("</div>")
    return "".join(parts)


def card_html(slot: str, label: str, field: str, assets: typing.Sequence[typing.Optional[store.Asset]], missing: bool = False) -> str:
    """One slot card in one of its states: inherit, override, or missing.

    "Not used by current model" is a badge the browser shows or hides from
    the live capabilities; it is in the markup so the card never has to be
    re-rendered for it.
    """
    present = [asset for asset in assets if asset is not None]
    head = f'<div class="minipaint-clip-card-title">{_escape(label)}'
    if present or missing:
        head += (f'<button type="button" class="minipaint-clip-card-clear" data-clear="{slot}" '
                 f'title="Use WanGP’s own {label.lower()} instead">×</button>')
    head += "</div>"
    badge = ('<span class="minipaint-clip-badge minipaint-clip-badge-unsupported" hidden>Not used by current model</span>')
    choose = (f'<button type="button" class="minipaint-clip-card-choose" data-choose="{slot}">Choose a file…</button>')
    if missing and not present:
        body = ('<div class="minipaint-clip-card-body minipaint-clip-card-missing">Missing image'
                '<small>the file is no longer in the folder — press Refresh</small></div>')
        state = "missing"
    elif not present:
        body = (f'<div class="minipaint-clip-card-body">Use WanGP<small>drop an image here, or select one and press '
                f'+{label.split()[0] if slot != "ref" else "Ref"}</small></div>')
        state = "inherit"
    else:
        pictures = "".join(
            f'<img src="{_escape(routes.image_url(asset.asset_id, version=asset.mtime_ns))}" alt="" draggable="false" title="{_escape(asset.filename)}">'
            for asset in present
        )
        names = ", ".join(asset.filename for asset in present)
        body = (f'<div class="minipaint-clip-card-body minipaint-clip-card-pictures">{pictures}</div>'
                f'<div class="minipaint-clip-card-name" title="{_escape(names)}">{_escape(names)}</div>')
        state = "override"
    return (
        f'<div class="minipaint-clip-card minipaint-clip-card-{state}" data-slot="{slot}" data-field="{field}" '
        f'data-asset="{_escape(present[0].asset_id) if present else ""}">{head}{body}{badge}{choose}</div>'
    )


def history_html(records: typing.Sequence[dict], asset_of: typing.Callable[[str], typing.Optional[store.Asset]]) -> str:
    """Queue Send History: each confirmed recipe, newest first."""
    if not records:
        return '<div class="minipaint-clip-history minipaint-clip-empty"><p>No request has been confirmed queued from here yet.</p></div>'
    parts = ['<div class="minipaint-clip-history">']
    for record in records:
        when = record.get("admitted_at", "").replace("T", " ").replace("+00:00", " UTC")
        model = record.get("model_label") or record.get("model_type") or "WanGP"
        if record.get("prompt_mode") == history.MODE_OVERRIDE:
            prompt = f'<div class="minipaint-clip-history-prompt" title="{_escape(record.get("prompt_override"))}">{_escape(record.get("prompt_override"))}</div>'
            if record.get("enhanced") and record.get("enhanced_prompt"):
                written = record["enhanced_prompt"]
                prompt += (f'<div class="minipaint-clip-history-enhanced" title="{_escape(written)}">enhanced: {_escape(written[:160])}'
                           f'{"…" if len(written) > 160 else ""}</div>')
        else:
            prompt = '<div class="minipaint-clip-history-prompt minipaint-clip-inherit-text">Prompt: Use WanGP</div>'
        thumbs = []
        for slot, label, _field in SLOTS:
            mode = record.get(f"{'reference' if slot == 'ref' else slot}_mode", history.MODE_INHERIT)
            ids = record.get("reference_asset_ids", []) if slot == "ref" else ([record.get(f"{slot}_asset_id")] if record.get(f"{slot}_asset_id") else [])
            if mode == history.MODE_INHERIT or not ids:
                thumbs.append(f'<span class="minipaint-clip-badge">{_escape(label)}: Use WanGP</span>')
                continue
            for asset_id in ids:
                asset = asset_of(asset_id)
                if asset is None:
                    thumbs.append(f'<span class="minipaint-clip-badge minipaint-clip-badge-missing">{_escape(label)}: Missing image</span>')
                else:
                    thumbs.append(
                        f'<span class="minipaint-clip-history-thumb" title="{_escape(label + ": " + asset.filename)}">'
                        f'<img src="{_escape(routes.image_url(asset.asset_id, version=asset.mtime_ns))}" alt="" draggable="false">'
                        f'<small>{_escape(label)}</small></span>'
                    )
            if mode == history.MODE_IGNORED:
                thumbs.append(f'<span class="minipaint-clip-badge minipaint-clip-badge-unsupported">{_escape(label)} was ignored</span>')
        tasks = record.get("tasks_added") or 0
        parts.append(
            f'<div class="minipaint-clip-history-entry" data-history="{_escape(record["history_id"])}">'
            f'<div class="minipaint-clip-history-head"><span class="minipaint-clip-history-when">{_escape(when)}</span>'
            f'<span class="minipaint-clip-history-model">{_escape(model)}</span>'
            f'<span class="minipaint-clip-history-tasks">{tasks} task{"s" if tasks != 1 else ""}</span></div>'
            f"{prompt}<div class=\"minipaint-clip-history-thumbs\">{''.join(thumbs)}</div>"
            f'<div class="minipaint-clip-history-actions">'
            f'<button type="button" data-history-action="load:{_escape(record["history_id"])}">Load</button>'
            f'<button type="button" data-history-action="delete:{_escape(record["history_id"])}">Delete</button></div></div>'
        )
    parts.append("</div>")
    return "".join(parts)


def job_sentence(job: typing.Mapping[str, typing.Any]) -> str:
    """How a job ended, in the words the rest of the extension uses."""
    state = job.get("state")
    result = job.get("result") or {}
    error = job.get("error") or {}
    if state == outbox.STARTED:
        return "WanGP started generating it."
    if state == outbox.QUEUED:
        depth = result.get("queue_depth")
        return "Added to WanGP queue." + (f" {depth} ahead of it." if isinstance(depth, int) and depth > 0 else "")
    if state in (outbox.FAILED, outbox.UNCONFIRMED):
        return str(error.get("message") or errors.message(error.get("code") or errors.QUEUE_REQUEST_REFUSED))
    if state == outbox.CANCELLED:
        return str(error.get("message") or "Cancelled before it was sent.")
    if state == outbox.SENDING:
        return "Being sent to WanGP…"
    if state == outbox.COMPLETED:
        count = int(job.get("generated_count") or 0)
        return f"Done: {count} file{'s' if count != 1 else ''} in WanGP's output folder." if count else "Done."
    if state == outbox.EXECUTION_UNKNOWN:
        return str(error.get("message") or errors.message(errors.EXECUTION_UNKNOWN))
    if job.get("executor") == outbox.EXECUTOR_SERVER and state in outbox.SERVER_ACTIVE:
        # The stage the executor wrote, which is the live one - "Waiting:
        # WanGP is busy with its own work", the enhancer's own progress text,
        # "Starting WanGP. This can take a few minutes from cold." A person
        # who comes back to a job that has been going for ten minutes is
        # owed the reason, and the reason is here rather than inferred.
        return str(job.get("stage") or outbox.STAGE_TEXT.get(state, "Waiting its turn."))
    if state == outbox.ENHANCING:
        return "Waiting for the enhanced prompt."
    return "Waiting its turn."


def enhance_sentence(job: typing.Mapping[str, typing.Any]) -> str:
    """Where a job's enhancement is, or was: the LLM side's own words, never the prompt."""
    record = job.get("enhance") or {}
    if not record:
        return ""
    variant = enhance.VARIANT_LABELS.get(record.get("variant"), str(record.get("variant") or ""))
    state = record.get("state")
    if state == enhance.LLM_QUEUED:
        position = record.get("position") or 0
        where = f" (position {position})" if position else ""
        return f"Enhancement as {variant}: waiting for the LLM{where}."
    if state == enhance.LLM_RUNNING:
        stage = record.get("stage") or "being written"
        return f"Enhancement as {variant}: {stage}"
    if state == enhance.LLM_DONE:
        seconds = record.get("elapsed") or 0.0
        parts = [f"Enhanced as {variant} in {seconds:.0f} s"]
        if record.get("image_used"):
            parts.append(f"described {enhance.SLOT_LABELS.get(record['image_used'], record['image_used'])}")
        ignored = [enhance.SLOT_LABELS.get(item, item) for item in record.get("image_ignored") or []]
        if ignored:
            parts.append(f"{', '.join(ignored)} not described")
        dropped = [FIELD_LABELS.get(item, item).lower() for item in record.get("dropped") or []]
        if dropped:
            parts.append(f"{', '.join(dropped)} left out of the enhancement")
        if record.get("system_override"):
            parts.append("with your system prompt")
        if record.get("reused_from"):
            parts.append("carried over from the earlier attempt")
        return "; ".join(parts) + "."
    if state == enhance.LLM_FAILED:
        return f"Enhancement failed: {record.get('error') or errors.message(errors.ENHANCE_FAILED)}"
    if state == enhance.LLM_CANCELLED:
        return f"Enhancement cancelled: {record.get('error') or errors.message(errors.ENHANCE_CANCELLED)}"
    if state == "lost":
        return errors.message(errors.ENHANCE_LOST)
    return ""


def wangp_sentence(job: typing.Mapping[str, typing.Any]) -> str:
    """Where a queued job's task is inside WanGP, as the page last saw it."""
    if job.get("state") not in outbox.POSITIVE:
        return ""
    seen = job.get("wangp") or {}
    state = seen.get("state") or outbox.WANGP_ACCEPTED
    if state == outbox.WANGP_WAITING:
        position = seen.get("position")
        return f"In WanGP's queue, {position} ahead of it." if isinstance(position, int) and position > 0 else "In WanGP's queue, next to run."
    if state == outbox.WANGP_GENERATING:
        return "WanGP is generating it."
    label = WANGP_LABELS.get(state, state)
    return label[:1].upper() + label[1:] + "."


def enhance_line_html(availability: typing.Mapping[str, typing.Any], enabled: bool) -> str:
    """The sentence above the switch, with its state for the stylesheet."""
    state = str(availability.get("state") or "unknown")
    head = "Enhanced prompts are on." if enabled else "Enhanced prompts are off."
    return (f'<div class="minipaint-clip-enhance-line" data-state="{_escape(state)}" data-enabled="{"1" if enabled else "0"}">'
            f'<b>{head}</b> {_escape(availability.get("text") or "")}</div>')


def outbox_html(jobs: typing.Sequence[dict], page: str) -> str:
    """The queue: every job the server holds, newest first, with what a
    person may still do about it. The prompt shown is the one typed here."""
    if not jobs:
        return '<div class="minipaint-clip-outbox minipaint-clip-empty"><p>No request has been sent from here yet.</p></div>'
    parts = [f'<div class="minipaint-clip-outbox" data-count="{len(jobs)}">']
    for job in list(reversed(list(jobs)))[:OUTBOX_SHOWN]:
        state = job.get("state", outbox.PENDING)
        mine = job.get("page") == page
        summary = job.get("summary") or {}
        supplied = [name for name, label in (("prompt", "prompt"), ("start", "first frame"), ("end", "last frame")) if summary.get(name)]
        if summary.get("references"):
            supplied.append(f"{summary['references']} reference{'s' if summary['references'] != 1 else ''}")
        prompt = (job.get("request") or {}).get("prompt")
        record = job.get("enhance") or {}
        typed = record.get("prompt_original") or ""
        if prompt and typed and record.get("state") == enhance.LLM_DONE and typed != prompt:
            excerpt = (f'<div class="minipaint-clip-job-prompt minipaint-clip-job-prompt-typed" title="{_escape(typed)}">{_escape(typed[:90])}{"…" if len(typed) > 90 else ""}</div>'
                       f'<div class="minipaint-clip-job-prompt minipaint-clip-job-prompt-enhanced" title="{_escape(prompt)}">{_escape(prompt[:120])}{"…" if len(prompt) > 120 else ""}</div>')
        elif prompt:
            excerpt = f'<div class="minipaint-clip-job-prompt" title="{_escape(prompt)}">{_escape(prompt[:90])}{"…" if len(prompt) > 90 else ""}</div>'
        else:
            excerpt = '<div class="minipaint-clip-job-prompt minipaint-clip-inherit-text">Prompt: Use WanGP</div>'
        when = str(job.get("created_at") or "").replace("T", " ").replace("+00:00", " UTC")
        actions = []
        server = job.get("executor") == outbox.EXECUTOR_SERVER
        if server and state in outbox.SERVER_ACTIVE:
            # Cancellable at every stage, from any page, because the server
            # owns it: there is no lease to be holding and no page whose turn
            # it is. "Run from this page" is not offered and would mean
            # nothing - no page runs it.
            actions.append(f'<button type="button" data-outbox-action="cancel:{_escape(job["job_id"])}">Cancel</button>')
        elif state in outbox.WAITING:
            actions.append(f'<button type="button" data-outbox-action="cancel:{_escape(job["job_id"])}">Cancel</button>')
            if not mine and state == outbox.PENDING:
                actions.append(f'<button type="button" data-outbox-action="adopt:{_escape(job["job_id"])}">Run from this page</button>')
        elif state == outbox.EXECUTION_UNKNOWN:
            actions.append(f'<button type="button" data-outbox-action="retry:{_escape(job["job_id"])}" '
                           'title="WanGP may already have generated this; check its output folder first">Retry anyway</button>')
        elif state in (outbox.FAILED, outbox.CANCELLED):
            actions.append(f'<button type="button" data-outbox-action="retry:{_escape(job["job_id"])}">Retry</button>')
        elif state == outbox.UNCONFIRMED:
            actions.append(f'<button type="button" data-outbox-action="retry:{_escape(job["job_id"])}" title="WanGP may already hold this task; check its queue first">Retry anyway</button>')
        badge = ""
        if state in outbox.WAITING and not mine:
            badge = '<span class="minipaint-clip-badge">composed on another page</span>'
        elif job.get("origin") == outbox.ORIGIN_API:
            badge = '<span class="minipaint-clip-badge">from another extension</span>'
        if job.get("enhance_requested"):
            badge += '<span class="minipaint-clip-badge">enhanced prompt</span>'
        if server:
            badge += '<span class="minipaint-clip-badge" title="This job runs on the server. You can close this page.">unattended</span>'
        snapshot = job.get("snapshot") or {}
        if snapshot.get("source") == protocol.BASE_FACTORY:
            # The one thing about a snapshot that must never be silent. A job
            # that ran at settings nobody chose for it, when its owner had
            # configured something else, is the failure compose exists to
            # prevent, and if it happens anyway it is said out loud rather
            # than looking like a job that ran at the settings they chose.
            #
            # "default settings" rather than "factory settings" because that
            # is what it actually is: Wan2GP's ``get_default_settings`` reads
            # the model's own saved settings file - what Save Settings writes
            # - and only synthesises true factory values when no such file
            # exists. Calling the common case "factory" overstated it.
            badge += ('<span class="minipaint-clip-badge minipaint-clip-badge-warn" '
                      'title="WanGP had no form recorded for this model, so this ran at that '
                      'model\'s saved defaults rather than at what was on screen">default settings</span>')
        lines = []
        llm = enhance_sentence(job)
        if llm:
            live = "failed" if record.get("state") in (enhance.LLM_FAILED, "lost") else "generating" if record.get("state") == enhance.LLM_RUNNING else ""
            lines.append(f'<div class="minipaint-clip-job-line" data-live="{live}"><b>LLM:</b> {_escape(llm)}</div>')
        inside = wangp_sentence(job)
        if inside:
            seen = (job.get("wangp") or {}).get("state") or outbox.WANGP_ACCEPTED
            live = "generating" if seen == outbox.WANGP_GENERATING else ""
            lines.append(f'<div class="minipaint-clip-job-line" data-live="{live}" data-wangp="{_escape(seen)}"><b>WanGP:</b> {_escape(inside)}</div>')
        parts.append(
            f'<div class="minipaint-clip-job minipaint-clip-job-{_escape(state)}" data-job="{_escape(job["job_id"])}" data-mine="{"1" if mine else "0"}">'
            f'<div class="minipaint-clip-job-head"><span class="minipaint-clip-job-state">{_escape(OUTBOX_LABELS.get(state, state))}</span>'
            f'<span class="minipaint-clip-job-when">{_escape(when)}</span>{badge}</div>'
            f"{excerpt}"
            f'<div class="minipaint-clip-job-fields">{_escape(", ".join(supplied) if supplied else "the WanGP page as it is")}'
            f'{" · start never" if summary.get("start_mode") == protocol.START_NEVER else ""}</div>'
            f'<div class="minipaint-clip-job-outcome">{_escape(job_sentence(job))}</div>'
            + "".join(lines)
            + (f'<div class="minipaint-clip-job-actions">{"".join(actions)}</div>' if actions else "")
            + "</div>"
        )
    parts.append("</div>")
    return "".join(parts)


def _page_of(value: typing.Any) -> str:
    text = str(value or "").strip()
    return text if outbox.PAGE_RE.match(text) else NO_PAGE


def _model_of(value: typing.Any) -> dict:
    """The WanGP model the page reported, from the hidden box's JSON; empty
    strings when it has not said, or said something that is not a model."""
    if isinstance(value, dict):
        return enhance.model_block(value)
    try:
        parsed = json.loads(str(value or "") or "{}")
    except ValueError:
        parsed = {}
    return enhance.model_block(parsed)


def _status(message: str, notes: typing.Sequence[str] = ()) -> str:
    parts = [message] if message else []
    parts.extend(f"<small>{note}</small>" for note in notes if note)
    return " ".join(parts)


def _hex(value: typing.Any) -> str:
    return value if protocol.valid_handoff_id(value) else ""


def _nonce() -> str:
    return secrets.token_hex(4)


#: The ClipboardTab most recently built, so the HTTP send route answers from
#: the same destinations the tab itself was wired to. Asking the host again
#: is not the same question: it rebuilds its answer from whatever was
#: registered last, and a page built more than once in a process (a test, a
#: Reload UI) hands back components the running tab never used - so the route
#: would name a box that nothing on the page is listening to.
_current: typing.Dict[str, typing.Any] = {"tab": None}


def current() -> typing.Optional["ClipboardTab"]:
    """The Clipboard tab of the UI being built, or None when there is none."""
    return _current.get("tab")


def send_plan(target: typing.Any, asset_id: typing.Any) -> dict:
    """Everything the browser needs to finish a send, without Gradio.

    The Gradio path and the HTTP route in ``routes.py`` both end up here, so
    "what does sending to X mean" is decided once. What differs is only who
    carries the answer: an event over the queue, or a JSON response over the
    transport that still works when the queue does not.

    Every destination is something the browser can finish. Two of them are
    written by putting the picture in a hidden textbox the host canvas
    reads; the rest hold their value in a Gradio component, and a component
    takes a picture from an upload as readily as from the server - so the
    plan names the component and hands over the picture, and the browser
    gives it the file the way a person dropping one would, over the upload
    route rather than the queue.

    ``backend`` is left true only when the component cannot be named, which
    is the one case no amount of browser work can finish. The browser is
    told so plainly instead of being handed a payload it cannot deliver.
    """
    name = str(target or "")
    label = canvas_ui.DESTINATION_LABELS.get(name, name)
    if name not in canvas_ui.DESTINATION_LABELS and name != "minipaint":
        return {"ok": False, "code": errors.REQUEST_INVALID,
                "message": f"{label or 'That destination'} is not a destination."}
    library = store.store()
    found = library.get(asset_id) if _hex(asset_id) else None
    if found is None:
        return {"ok": False, "code": errors.CLIPBOARD_ASSET_UNKNOWN,
                "message": "Select an image in the browser first."}
    try:
        image = library.open_image(found.asset_id)
    except IntegrationError as error:
        return {"ok": False, "code": error.code, "message": errors.message(error.code)}

    plan: dict = {"ok": True, "target": name, "filename": found.filename,
                  "label": label, "backend": name in canvas_ui.BACKEND_TARGETS}
    if name == "minipaint":
        plan["instruction"] = "minipaint"
        plan["asset"] = found.asset_id
        return plan
    if name in ("img2img", "inpaint"):
        # The browser finishes these by writing the host canvas's hidden
        # textbox, so it is told exactly which one rather than deducing it
        # from the page: ForgeCanvas gives its two boxes the same id and
        # tells them apart by class, and only this side knows which canvas
        # the host registered for this tab.
        tab = current()
        targets = tab.targets if tab is not None else host.destinations()
        box = getattr(targets.get(name), "elem_id", "") or ""
        if not box:
            return {"ok": False, "code": errors.REQUEST_INVALID,
                    "message": f"There is no {label} on this page to send to."}
        plan["box"] = box
        plan["instruction"] = f"inpaint:{image.width}x{image.height}" if name == "inpaint" else name
        plan["payload"] = imaging.to_data_url(image)
    else:
        # Extras and the stitch galleries. The server writes these by
        # returning a new value for the component, which is a Gradio event
        # and therefore the queue; the browser can put the same picture in
        # the same component by handing it the file, which is the ordinary
        # upload route and is not. Which component is this side's to say:
        # the host registers them by element id and a page built twice
        # would otherwise be told about one that is not on it.
        tab = current()
        targets = tab.targets if tab is not None else host.destinations()
        elem = getattr(targets.get(name), "elem_id", "") or ""
        plan["instruction"] = name
        if elem:
            plan["elem"] = elem
            plan["payload"] = imaging.to_data_url(image)
            #: A gallery keeps what is already in it when a file is added,
            #: where the server's write replaces the lot. The browser says
            #: which happened rather than claiming the server's wording.
            plan["adds"] = name in canvas_ui.STITCH_TARGETS
            plan["backend"] = False
    return plan


# ----------------------------------------------------------------- the tab --


class ClipboardTab:
    """Builds the tab and owns its callbacks. One instance per mounted UI."""

    def __init__(self) -> None:
        self.library = store.store()
        self.targets = host.destinations()
        self.image_targets = [key for key in canvas_ui.BACKEND_TARGETS if key in self.targets]
        self.stitch_targets = [key for key in canvas_ui.STITCH_TARGETS if key in self.targets]
        self.canvas = canvas_ui.current()
        #: The last markup sent for each repeatedly re-rendered section, so an
        #: update that would change nothing on screen is not sent at all. See
        #: ``_unchanged``.
        self._rendered: typing.Dict[str, typing.Any] = {}
        #: The send requests already answered, newest last, so no event that
        #: carries one can deliver the same picture twice. See ``send``.
        self._answered: "collections.OrderedDict[str, None]" = collections.OrderedDict()
        # Where a selected picture can go: Mini Paint when the Canvas is
        # mounted, and every host destination the Canvas itself knows.
        self.destinations: typing.List[typing.Tuple[str, str]] = []
        if self.canvas is not None:
            self.destinations.append(("minipaint", "Mini Paint"))
        self.destinations += [
            (key, canvas_ui.DESTINATION_LABELS[key])
            for key in canvas_ui.DESTINATION_LABELS
            if key in self.targets and key != canvas_ui.CLIPBOARD_TARGET
        ]

    # -- what the page shows ------------------------------------------------

    def _menu_state(self) -> str:
        current = config.load()
        return json.dumps({
            "intercept": bool(current.intercept),
            "configured": bool(current.configured),
            "sort": current.sort,
            "thumbnail": current.thumbnail,
            "sorts": [[mode, config.SORT_LABELS[mode]] for mode in config.SORT_MODES],
            "destinations": [[key, label] for key, label in self.destinations],
        })

    def _asset(self, asset_id: typing.Any) -> typing.Optional[store.Asset]:
        return self.library.get(asset_id) if _hex(asset_id) else None

    def _cards(self, draft: typing.Optional[dict] = None, missing: typing.Sequence[str] = ()) -> typing.Tuple[str, str, str]:
        draft = history.normalize_draft(draft if draft is not None else history.load_draft())
        rendered = []
        for slot, label, field in SLOTS:
            ids = draft["reference_asset_ids"] if slot == "ref" else ([draft[SLOT_KEYS[slot]]] if draft[SLOT_KEYS[slot]] else [])
            assets = [self._asset(asset_id) for asset_id in ids]
            rendered.append(card_html(slot, label, field, assets, missing=slot in missing))
        return tuple(rendered)  # type: ignore[return-value]

    def _grid(self, selected: str = "") -> str:
        current = config.load()
        return grid_html(self.library.assets(current.sort), _hex(selected), current.configured)

    def _history(self) -> str:
        return history_html(history.load_history(), self._asset)

    def _refresh_outputs(self, message: str, selected: str = "", notes: typing.Sequence[str] = ()) -> tuple:
        cards = self._cards()
        return (self._grid(selected), _status(message, notes), _hex(selected), self._menu_state(), *cards, self._queue_button())

    # -- WanGP's state, as the button shows it -----------------------------

    def _running(self) -> bool:
        return outbox.wangp_running()

    def _queue_button(self, running: typing.Optional[bool] = None):
        """Add to Queue, and when it is not one.

        The old rule - a button only while WanGP is already running - was
        correct for a browser-executed press: there was nothing for a page to
        drive, so storing the job would have been storing it for a process
        that might never come.

        It is exactly wrong for an unattended one. Starting a cold WanGP is
        a stage the server performs *after* admission, and refusing the press
        because the thing the server is about to start is not started yet
        would make the cold case - the one the whole feature exists for -
        the one case that does not work.
        """
        if outbox.chosen_executor() == outbox.EXECUTOR_SERVER:
            return gr.update(interactive=True, value=QUEUE_BUTTON_LABEL)
        running = self._running() if running is None else bool(running)
        return gr.update(interactive=running, value=QUEUE_BUTTON_LABEL if running else QUEUE_BUTTON_BLOCKED)

    def _outbox(self, page: typing.Any = "") -> str:
        try:
            return outbox_html(outbox.jobs(), _page_of(page))
        except Exception as error:
            return f'<div class="minipaint-clip-outbox minipaint-clip-empty"><p>The queue could not be read ({_escape(type(error).__name__)}).</p></div>'

    def _clear_missing_slots(self) -> typing.List[str]:
        """Slots whose asset is no longer in the library go back to inherit."""
        draft = history.load_draft()
        cleared: typing.List[str] = []
        for slot, label, _field in SLOTS:
            if slot == "ref":
                kept = [item for item in draft["reference_asset_ids"] if self._asset(item) is not None]
                if len(kept) != len(draft["reference_asset_ids"]):
                    draft["reference_asset_ids"] = kept
                    cleared.append(label)
            elif draft[SLOT_KEYS[slot]] and self._asset(draft[SLOT_KEYS[slot]]) is None:
                draft[SLOT_KEYS[slot]] = ""
                cleared.append(label)
        if cleared:
            history.save_draft(draft)
        return cleared

    # -- prompt enhancement -----------------------------------------------------

    def _enhance_line(self, model: typing.Any = None) -> str:
        try:
            return enhance_line_html(enhance.availability(_model_of(model)), enhance.enabled())
        except Exception as error:
            return enhance_line_html({"state": "blocked", "text": f"The enhancement settings could not be read ({type(error).__name__})."}, False)

    def _system_prompt_view(self, variant, mode) -> typing.Tuple[typing.Any, str]:
        """The box and the line under it for one of the four instruction sets."""
        try:
            text, source = enhance.effective_prompt(variant, mode)
        except IntegrationError:
            return gr.update(value=""), "Choose a variant and whether a picture is sent."
        label = f"{enhance.VARIANT_LABELS.get(variant, variant)}, {enhance.MODE_LABELS.get(mode, mode)}"
        if source == "override":
            line = f"**Override saved** for {label}. Restore default forgets it."
        elif source == "default":
            line = f"**Default** for {label}, as ModelSwitchRefiner ships it. Edit and Apply override to replace it."
        else:
            line = f"No default for {label} could be read: ModelSwitchRefiner is not available in this Forge. An override can still be saved."
        return gr.update(value=text), line

    def toggle_enhance(self, flag, model):
        wanted = enhance.set_enabled(bool(flag))
        self._journal(f"enhanced prompts {'on' if wanted else 'off'} from the tab")
        return self._enhance_line(model)

    def model_changed(self, model):
        """The browser says which WanGP model the page is on: the line follows."""
        return self._enhance_line(model)

    def system_prompt_selected(self, variant, mode):
        box, line = self._system_prompt_view(variant, mode)
        return box, line

    def apply_override(self, variant, mode, text):
        try:
            enhance.set_override(variant, mode, text)
        except IntegrationError as error:
            _box, line = self._system_prompt_view(variant, mode)
            return gr.skip(), line, _status(errors.message(error.code))
        box, line = self._system_prompt_view(variant, mode)
        self._journal(f"system prompt override applied for {variant} {mode}")
        return box, line, _status("Override saved. Every enhanced press from now on uses it, on every page, after a restart too.")

    def restore_default(self, variant, mode):
        try:
            had = enhance.clear_override(variant, mode)
        except IntegrationError as error:
            return gr.skip(), gr.skip(), _status(errors.message(error.code))
        box, line = self._system_prompt_view(variant, mode)
        return box, line, _status("Back to the default." if had else "There was no override; the default is shown.")

    def cancel_all(self, page):
        """Cancel everything: enhancing and pending jobs, from every page."""
        page_id = _page_of(page)
        try:
            answer = outbox.cancel_all()
        except Exception as error:
            return self._outbox(page_id), _status(f"The queue could not be cancelled ({type(error).__name__}).")
        count = answer.get("cancelled", 0)
        notes = []
        if answer.get("enhancing"):
            notes.append(f"{answer['enhancing']} enhancement{'s' if answer['enhancing'] != 1 else ''} stopped")
        if answer.get("in_flight"):
            notes.append(f"{answer['in_flight']} already being sent to WanGP and left to finish")
        notes.append("nothing already in WanGP's queue was touched")
        self._journal(f"cancel all from the tab: {count} cancelled")
        return self._outbox(page_id), _status(f"Cancelled {count} waiting request{'s' if count != 1 else ''}." if count else "Nothing was waiting.", notes)

    # -- the browser ----------------------------------------------------------

    def refresh(self, selected):
        """Read the folder again. The last import is selected when it is news."""
        if not self.library.configured():
            return self._refresh_outputs("Choose a storage folder from the menu to begin.")
        try:
            assets = self.library.refresh()
        except IntegrationError as error:
            return self._refresh_outputs(errors.message(error.code))
        chosen = _hex(selected)
        last = self.library.last_import
        if last is not None and self._asset(last[0]) is not None:
            chosen = last[0]
            self.library.last_import = None
        if chosen and self._asset(chosen) is None:
            chosen = ""
        cleared = self._clear_missing_slots()
        notes = [f"{', '.join(cleared)}: the image is no longer in the folder, so the slot is back to Use WanGP"] if cleared else []
        count = len(assets)
        return self._refresh_outputs(f"{count} image{'s' if count != 1 else ''} in the folder.", chosen, notes)

    def sort_changed(self, mode, selected):
        if mode in config.SORT_MODES:
            config.update(sort=mode)
        return self._grid(selected), self._menu_state()

    def sort_request(self, value, selected):
        """The menu's Sort submenu: the mode, then a nonce so a repeat still counts."""
        mode = str(value or "").split(":", 1)[0]
        if mode in config.SORT_MODES:
            config.update(sort=mode)
        return gr.update(value=config.load().sort), self._grid(selected), self._menu_state()

    def thumbnail_changed(self, size):
        config.update(thumbnail=config.clamp_thumbnail(size))
        return self._menu_state()

    def toggle_intercept(self):
        current = config.update(intercept=not config.load().intercept)
        message = ("The gallery’s 🖌️ button now sends into Clipboard." if current.intercept
                   else "The gallery’s 🖌️ button sends to Mini Paint again.")
        return self._menu_state(), _status(message)

    # -- the composer ---------------------------------------------------------

    def _assign(self, slot: str, asset_id: str) -> typing.Tuple[dict, str]:
        asset = self._asset(asset_id)
        if asset is None:
            return history.load_draft(), "Select an image in the browser first."
        draft = history.load_draft()
        if slot == "ref":
            draft["reference_asset_ids"] = [asset.asset_id]
        else:
            draft[SLOT_KEYS[slot]] = asset.asset_id
        history.save_draft(draft)
        label = next(title for name, title, _field in SLOTS if name == slot)
        return draft, f"{label}: {asset.filename}."

    def assign(self, slot, selected):
        draft, message = self._assign(slot, _hex(selected))
        return (*self._cards(draft), _status(message))

    def slot_action(self, value):
        """The card's own controls: ``clear:<slot>`` or ``assign:<slot>:<asset id>``."""
        parts = str(value or "").split(":")
        action, slot = (parts[0], parts[1]) if len(parts) >= 2 else ("", "")
        if slot not in SLOT_KEYS:
            return (*self._cards(), gr.skip())
        draft = history.load_draft()
        if action == "clear":
            if slot == "ref":
                draft["reference_asset_ids"] = []
            else:
                draft[SLOT_KEYS[slot]] = ""
            history.save_draft(draft)
            label = next(title for name, title, _field in SLOTS if name == slot)
            return (*self._cards(draft), _status(f"{label}: Use WanGP.", ["nothing on the WanGP page was changed"]))
        if action == "assign" and len(parts) >= 3:
            draft, message = self._assign(slot, _hex(parts[2]))
            return (*self._cards(draft), _status(message))
        return (*self._cards(draft), gr.skip())

    def slot_upload(self, slot, file, selected):
        """A file chosen for a card: imported into the library, then assigned."""
        path = getattr(file, "name", file)
        if not path:
            return self._refresh_outputs("No file was chosen.", selected)
        try:
            with open(str(path), "rb") as stream:
                data = stream.read(store.MAX_BYTES + 1)
            asset = self.library.import_bytes(data, os.path.basename(str(path)), "slot")
        except IntegrationError as error:
            return self._refresh_outputs(errors.message(error.code), selected)
        except Exception as error:
            return self._refresh_outputs(f"That file could not be imported ({type(error).__name__}).", selected)
        self.library.last_import = None
        draft, message = self._assign(slot, asset.asset_id)
        return (self._grid(asset.asset_id), _status(message, ["imported into the folder first"]), asset.asset_id, self._menu_state(), *self._cards(draft), self._queue_button())

    def upload(self, files, selected):
        """Upload image(s): each validated, copied as it is, indexed."""
        items = files if isinstance(files, (list, tuple)) else [files]
        taken, refused = 0, []
        last_id = ""
        for item in items:
            path = getattr(item, "name", item)
            if not path:
                continue
            try:
                with open(str(path), "rb") as stream:
                    data = stream.read(store.MAX_BYTES + 1)
                asset = self.library.import_bytes(data, os.path.basename(str(path)), "upload")
                taken += 1
                last_id = asset.asset_id
            except IntegrationError as error:
                refused.append(errors.message(error.code))
            except Exception as error:
                refused.append(f"one file could not be imported ({type(error).__name__})")
        self.library.last_import = None
        notes = sorted(set(refused))
        message = f"Imported {taken} image{'s' if taken != 1 else ''}." if taken else "Nothing was imported."
        return self._refresh_outputs(message, last_id or selected, notes)

    def pasted(self, path, selected):
        """Gradio's own paste surface handed a file over: pixels become a PNG."""
        if not path:
            return (*self._refresh_outputs("Nothing was pasted.", selected), gr.update(value=None), gr.update(visible=False))
        try:
            image = imaging.open_file(str(path))
            asset = self.library.import_image(image, "", "paste")
        except IntegrationError as error:
            return (*self._refresh_outputs(errors.message(error.code), selected), gr.update(value=None), gr.update(visible=True))
        except Exception as error:
            return (*self._refresh_outputs(f"The pasted image could not be read ({type(error).__name__}).", selected), gr.update(value=None), gr.update(visible=True))
        self.library.last_import = None
        return (*self._refresh_outputs(f"Pasted {asset.filename}.", asset.asset_id), gr.update(value=None), gr.update(visible=False))

    def prompt_changed(self, prompt):
        draft = history.load_draft()
        draft["prompt_override"] = str(prompt or "")
        history.save_draft(draft)
        return None

    # -- the queue ------------------------------------------------------------

    def prepare_queue(self, prompt, page, model=None, enhance_wanted=None):
        """Add to Queue: the draft as a public request, into the server's outbox.

        Everything inherited is omitted from the request. A slot whose file
        is gone fails here, before anything is stored, and keeps the draft;
        a WanGP that is not running refuses the press rather than storing it.
        With enhanced prompts on, the press is refused in the same way when
        the LLM side cannot take it or the page's model (``model``, as the
        browser reported it) is not an H3 one; otherwise the enhancement is
        asked for at once and the job waits as enhancing. The instruction
        box then tells the browser a job exists, and the page's pump does
        the rest.

        ``enhance_wanted`` IS THE SWITCH, AS IT STANDS AT THE PRESS, AND IT
        DECIDES.

        It used to be left out, and the press asked the stored setting
        instead. Those are the same answer right up until they are not: the
        switch's value lives in the browser, the setting lives on disk, and
        the only thing that reconciles them is a change event. Lose one -
        a click during a reload, a page that was open before the setting
        moved, a second browser - and the box says off while the press
        enhances, which is what it did.

        A checkbox that does not decide is not a checkbox, so the value that
        travels with the press is the one that was on screen when it was
        pressed. None means no switch was supplied (a caller that is not the
        tab) and the stored setting still decides.
        """
        page_id = _page_of(page)
        block = _model_of(model)
        draft = history.load_draft()
        draft["prompt_override"] = str(prompt or "")
        draft = history.save_draft(draft)
        missing = []
        for slot, label, _field in SLOTS:
            ids = draft["reference_asset_ids"] if slot == "ref" else ([draft[SLOT_KEYS[slot]]] if draft[SLOT_KEYS[slot]] else [])
            if any(self._asset(item) is None for item in ids):
                missing.append(slot)
        if missing:
            labels = ", ".join(title for name, title, _field in SLOTS if name in missing)
            self._journal(f"queue clicked; refused before storing - {labels.lower()} missing from the folder")
            return "", _status(f"{labels}: the image is no longer in the folder. Press Refresh, then try again.",
                               ["nothing was asked of WanGP; the rest of the draft is kept"]), gr.skip(), self._queue_button()
        request = history.public_request(draft)
        request["start"] = protocol.START_AUTO
        try:
            wanted = None if enhance_wanted is None else bool(enhance_wanted)
            # What decided it, in the log, every time. The switch's value
            # lives in the browser and the setting lives on disk, and when a
            # report says "the box was off but it enhanced" there is otherwise
            # nothing to tell whether the box sent True or was never asked.
            if wanted is None:
                decided = f"{'on' if enhance.enabled() else 'off'} (no switch was sent; the stored setting decided)"
            else:
                decided = f"{'on' if wanted else 'off'} (from the switch, as it stood at the press)"
            self._journal(f"queue clicked: enhancement {decided}")
            if wanted is not None and wanted != enhance.enabled():
                # The switch and the stored setting had drifted, so the click
                # that should have written it never arrived. Take the press as
                # the answer and make them agree: otherwise the next Forge
                # start reads the setting back and the box goes on lying, once
                # per restart, with nothing to show the user why.
                enhance.set_enabled(wanted)
                self._journal(f"enhanced prompts {'on' if wanted else 'off'} from the press; the stored setting disagreed")
            job = outbox.submit(request, page_id, outbox.ORIGIN_CLIPBOARD, model=block, enhance=wanted)
        except IntegrationError as error:
            self._journal(f"queue clicked; refused - {error.code}")
            notes = ["nothing was stored; press it again once WanGP is running"] if error.code == errors.WANGP_NOT_RUNNING else []
            if error.code.startswith("ENHANCE_"):
                notes.append("nothing was stored; switch enhanced prompts off to queue the prompt as typed")
            return "", _status(errors.message(error.code), notes), self._outbox(page_id), self._queue_button()
        pending = outbox.pending_count()
        record = job.get("enhance") or {}
        self._journal(f"queue clicked: job {job['job_id'][:8]} (overrides {', '.join(history.draft_overrides(draft)) or 'none'}"
                      f"{'; enhancing as ' + record['variant'] if record else ''}); {pending} waiting")
        notes = [f"{pending - 1} ahead of it" if pending > 1 else "it goes next"]
        if job.get("executor") == outbox.EXECUTOR_BROWSER and outbox.unattended_enabled():
            # The setting says unattended and the job is not. That is this
            # WanGP saying it has no queue worker to submit into, and the
            # difference matters to whoever is about to walk away: the job
            # runs, but only while this page is open.
            notes.append("this WanGP cannot run queued jobs on its own, so this one runs from this page - keep the tab open")
        if record:
            dropped = [FIELD_LABELS.get(item, item).lower() for item in record.get("dropped") or []]
            if dropped:
                notes.append(f"{', '.join(dropped)}: not read by {enhance.VARIANT_LABELS.get(record['variant'], record['variant'])}, so left out of the enhancement (still sent to WanGP)")
            if record.get("extra_references"):
                notes.append("only the first reference is described to the writer")
        if page_id == NO_PAGE:
            notes.append("this page did not identify itself, so the job waits for one that can run it")
        # The executor travels with the acknowledgement, because it decides
        # what the page does next: a server job is *watched*, and the page
        # may be closed the moment this lands; a browser job is pumped, and
        # closing the page stops it.
        instruction = json.dumps({
            "nonce": _nonce(), "job_id": job["job_id"],
            "executor": job.get("executor", outbox.EXECUTOR_BROWSER), "state": job.get("state", ""),
        })
        if job.get("executor") == outbox.EXECUTOR_SERVER:
            line = "Queued on the server."
            # The three things about walking away that are invisible unless
            # the UI says them. The first two are ordinary status; the third
            # is a note rather than a dialog, because it is true only of a
            # crash and only on one platform.
            notes.append("you can close this page - Forge runs it, starting WanGP if it is not already running")
            if record:
                line = "Queued on the server; the prompt is being enhanced first."
            notes.append("it may wait if you are generating in the WanGP tab; it will never interrupt that")
        else:
            line = (f"Enhancing the prompt as {enhance.VARIANT_LABELS.get(record['variant'], record['variant'])}; "
                    "WanGP gets it when it is written.") if record else "Queued for WanGP."
            notes.append("keep this page open until it is queued")
        return instruction, _status(line, notes), self._outbox(page_id), self._queue_button()

    def _draft_from_request(self, request: typing.Mapping[str, typing.Any], job: typing.Optional[typing.Mapping[str, typing.Any]] = None) -> dict:
        """A job's request as the draft it came from: Clipboard assets by id,
        anything staged by another extension left to inherit, and the prompt
        as it was typed when the job was enhanced."""
        images = request.get("images") if isinstance(request.get("images"), dict) else {}
        record = (job or {}).get("enhance") or {}
        typed = record.get("prompt_original") if record.get("state") == enhance.LLM_DONE and record.get("prompt_original") else None

        def asset_of(handle: typing.Any) -> str:
            return handle["id"] if isinstance(handle, dict) and handle.get("kind") == "clipboard_asset" and _hex(handle.get("id")) else ""

        return {
            "prompt_override": str(typed if typed is not None else request.get("prompt") or ""),
            "first_asset_id": asset_of(images.get(protocol.QUEUE_FIELD_START)),
            "last_asset_id": asset_of(images.get(protocol.QUEUE_FIELD_END)),
            "reference_asset_ids": [asset_of(item) for item in images.get(protocol.QUEUE_FIELD_REFERENCES) or [] if asset_of(item)],
        }

    def _record_history(self) -> int:
        """Queue Send History gains every Clipboard job confirmed since last asked. Idempotent."""
        recorded = []
        for job in outbox.unrecorded(outbox.ORIGIN_CLIPBOARD):
            result = job.get("result") or {}
            written = job.get("enhance") or {}
            enhanced = job["request"].get("prompt") if written.get("state") == enhance.LLM_DONE else ""
            record = history.make_record(self._draft_from_request(job["request"], job), {
                "request_id": job["request"].get("request_id"), "applied": result.get("applied"), "inherited": result.get("inherited"),
                "ignored": result.get("ignored"), "tasks_added": result.get("tasks_added"), "model": result.get("model"),
            }, enhanced_prompt=enhanced or "")
            history.add_history(record)
            recorded.append(job["job_id"])
            self._journal(f"job {job['job_id'][:8]} {job['state']}; history recorded")
        outbox.mark_recorded(recorded)
        return len(recorded)

    def refresh_outbox(self, page):
        """The browser says the queue moved: re-render it, record history,
        and say how the latest of this page's jobs ended."""
        page_id = _page_of(page)
        self._record_history()
        jobs = outbox.jobs()
        mine = [job for job in jobs if job.get("page") == page_id]
        settled = [job for job in mine if job.get("state") in outbox.TERMINAL]
        latest = max(settled, key=lambda job: (float(job.get("updated") or 0.0), float(job.get("created") or 0.0)), default=None)
        notes = []
        waiting = sum(1 for job in mine if job.get("state") == outbox.PENDING)
        enhancing = sum(1 for job in mine if job.get("state") == outbox.ENHANCING)
        sending = sum(1 for job in mine if job.get("state") == outbox.SENDING)
        if sending:
            notes.append("one is being sent")
        if enhancing:
            notes.append(f"{enhancing} being enhanced")
        if waiting:
            notes.append(f"{waiting} waiting")
        line = job_sentence(latest) if latest else ("" if not mine else "Waiting its turn.")
        ignored = [item.get("field") for item in ((latest or {}).get("result") or {}).get("ignored") or []]
        notes.extend(f"{FIELD_LABELS.get(field, field)} was not used by the current model." for field in ignored)
        return (
            self._unchanged("outbox", self._outbox(page_id)),
            (_status(line, notes) if line or notes else gr.skip()),
            self._unchanged("history", self._history()),
            self._queue_button(),
        )

    def _unchanged(self, key: str, rendered: typing.Any) -> typing.Any:
        """``gr.skip()`` when this render is the same as the last one.

        The queue moves for reasons that change nothing on screen - a job
        advancing through states the list does not show, another page's job,
        a heartbeat - and every one of those used to replace the list's HTML
        wholesale. Gradio tears the old nodes out and puts new ones in, so
        the section lost its scroll position and everything below it jumped,
        several times a minute, while the user was reading it.

        Comparing the rendered markup is the cheapest honest test of "would
        the user see any difference", and it is exact: identical markup
        cannot look different. What did change is still sent.
        """
        if self._rendered.get(key) == rendered:
            return gr.skip()
        self._rendered[key] = rendered
        return rendered

    def outbox_action(self, value, page):
        """A button on a job: ``cancel:<job>``, ``retry:<job>``, ``adopt:<job>``, with the page after."""
        parts = str(value or "").split(":")
        verb = parts[0] if parts else ""
        job_id = parts[1] if len(parts) > 1 else ""
        page_id = _page_of(parts[2]) if len(parts) > 2 and outbox.PAGE_RE.match(parts[2] or "") else _page_of(page)
        try:
            if verb == "cancel":
                job = outbox.cancel(job_id)
                message = "Cancelled." if job["state"] == outbox.CANCELLED else f"That job is {OUTBOX_LABELS.get(job['state'], job['state']).lower()}; nothing to cancel."
                if job["state"] == outbox.CANCELLED and job.get("enhance"):
                    message = "Cancelled, and its enhancement with it."
            elif verb == "retry":
                job = outbox.retry(job_id, page_id)
                message = "Sent again as a new request."
            elif verb == "adopt":
                job = outbox.adopt(job_id, page_id)
                message = "This page will run it, with this page's WanGP settings."
            else:
                return self._outbox(page_id), gr.skip()
        except IntegrationError as error:
            return self._outbox(page_id), _status(errors.message(error.code))
        self._journal(f"job {job_id[:8]}: {verb} from the tab")
        return self._outbox(page_id), _status(message)

    def _journal(self, message: str) -> None:
        try:
            from ..wangp import process_log

            process_log.note("clipboard", message)
        except Exception:
            pass

    # -- history ----------------------------------------------------------------

    def show_history(self):
        return gr.update(visible=True), self._history()

    def history_action(self, value, prompt):
        """``load:<id>`` replaces the draft with that recipe; ``delete:<id>`` removes the record."""
        action, _, history_id = str(value or "").partition(":")
        history_id = history_id.split(":", 1)[0]
        records = {record["history_id"]: record for record in history.load_history()}
        record = records.get(history_id)
        if record is None:
            return gr.skip(), *self._cards(), _status("That history entry is gone."), self._history()
        if action == "delete":
            history.delete_history(history_id)
            return gr.skip(), *self._cards(), _status("History entry deleted.", ["the image files were not touched"]), self._history()
        if action == "load":
            draft, missing = history.draft_from_record(record, lambda item: self._asset(item) is not None)
            history.save_draft(draft)
            notes = [f"{', '.join(missing)}: missing image, so that slot is Use WanGP"] if missing else []
            return draft["prompt_override"], *self._cards(draft), _status("Recipe loaded. Nothing was queued.", notes), gr.skip()
        return gr.skip(), *self._cards(), gr.skip(), gr.skip()

    # -- send out -----------------------------------------------------------------

    def send(self, request, selected):
        """Send the selected picture to another tab, the way the Canvas does it.

        Every path returns an acknowledgement carrying the browser's own
        stamp for this request. Writing a hidden box is only half of a send -
        the other half is a Gradio event crossing the queue, and that half
        can go missing on a connection that dropped or a session the server
        has forgotten. Without an answer to wait for, the browser could not
        tell "refused" from "never arrived", and a send that never arrived
        looked exactly like a button that did nothing.
        """
        text = str(request or "")
        # When the box has nothing new, take the request the browser posted.
        #
        # ``request`` is the hidden textbox's value as the host's framework
        # holds it, and on a page where a scripted write is not heard that
        # value is whatever the last heard write left there - a request from
        # an earlier send, or the empty string. A press then arrives asking
        # for a send that has already been answered, and the user gets the
        # receipt for a picture they moved on from.
        #
        # The browser posts every send to ``routes``' own HTTP route on its
        # way out, over the transport that keeps working, so the request is
        # already here. The box is still preferred - on a healthy page it is
        # the same string, and it is the one input that is unambiguously
        # about this event - and this is consulted only when the box offers
        # nothing this call has not already answered.
        if not text or text in self._answered:
            fresh = routes.recent_request()
            if fresh and fresh not in self._answered:
                text = fresh
        parts = text.split(":")
        target = parts[0] if parts else ""
        asset_id = _hex(parts[1]) if len(parts) > 1 else ""
        # The browser stamps each request so it can recognise the answer to
        # its own; echoed back untouched.
        ack = parts[2][:32] if len(parts) > 2 else ""
        skips = [gr.skip() for _ in self.image_targets]
        # One request, delivered once, however many times it arrives.
        #
        # Two events carry a send - the press and the written box - because
        # neither is reliable everywhere, and on a healthy install both
        # arrive. The second must not deliver the picture again: to a canvas
        # that is waste, to a gallery that appends it is one picture too many.
        #
        # More than the last request is remembered, and that is the part that
        # is load-bearing rather than tidy. A press carries whatever value the
        # framework holds for the box, and the install this was written for is
        # one where a written box may not reach the framework at all - so a
        # press can arrive carrying not the previous request but one from
        # several sends ago, and re-deliver a picture the user has moved on
        # from. Anything already answered is answered the same way again.
        #
        # The whole request is the key, not the stamp: a page that somehow
        # reused a stamp for a different picture should get the picture.
        if text and text in self._answered:
            return (*skips, gr.skip(), gr.skip(), gr.skip(), gr.skip(), ack)
        if text:
            self._answered[text] = None
            while len(self._answered) > _ANSWERED_KEPT:
                self._answered.popitem(last=False)
        # "done" means the page has already put the picture where it goes,
        # over the route and its own DOM, and this event is here to record
        # the send rather than to perform it. Writing the destination again
        # would be a second delivery of the same picture - harmless for a
        # canvas, and one picture too many for a gallery that appends.
        delivered = len(parts) > 3 and parts[3] == "done"
        label = dict(self.destinations).get(target, target)
        if target not in dict(self.destinations):
            return (*skips, "", "", "", _status(f"{label or 'That destination'} is not available in this WebUI."), ack)
        asset = self._asset(asset_id or selected)
        if asset is None:
            return (*skips, "", "", "", _status("Select an image in the browser first."), ack)
        try:
            image = self.library.open_image(asset.asset_id)
        except IntegrationError as error:
            return (*skips, "", "", "", _status(errors.message(error.code)), ack)
        if delivered:
            return (*skips, "", "", "", _status(f"Sent {asset.filename} to {label}."), ack)
        nonce = _nonce()
        if target == "minipaint":
            return (*skips, "", "", f"{asset.asset_id}:{nonce}", _status(f"Sent {asset.filename} to Mini Paint."), ack)
        payload = ""
        if target == "inpaint":
            instruction = f"inpaint:{image.width}x{image.height}"
            payload = imaging.to_data_url(image)
        else:
            instruction = target
            if target == "img2img":
                payload = imaging.to_data_url(image)
        outputs: typing.List[typing.Any] = []
        if target in self.image_targets:
            host.staged(image)
        for key in self.image_targets:
            outputs.append(([image] if key in canvas_ui.STITCH_TARGETS else image) if key == target else gr.skip())
        notes = ["it is now the only reference image there"] if target in canvas_ui.STITCH_TARGETS else []
        return (*outputs, instruction, payload, "", _status(f"Sent {asset.filename} to {label}.", notes), ack)

    # -- the folder and the file operations ---------------------------------------

    def choose_folder(self, text, create):
        answer = self.library.set_root(text, create=bool(create))
        if not answer["ok"]:
            hint = " Press **Create it** to make it." if answer["code"] == "ROOT_MISSING" else ""
            return (f"**Not used.** {answer['message']}.{hint}", gr.update(visible=True), *self._refresh_outputs("Choose a storage folder from the menu to begin."))
        self.library.forget()
        refreshed = self.refresh("")
        return (f"**Using** `{answer['path']}`", gr.update(visible=False), *refreshed)

    def open_folder(self):
        current = config.load()
        return gr.update(visible=True), gr.update(value=current.storage_root), ""

    def open_rename(self, selected):
        asset = self._asset(selected)
        if asset is None:
            return gr.update(visible=False), gr.update(), _status("Select an image in the browser first.")
        return gr.update(visible=True), gr.update(value=os.path.splitext(asset.filename)[0]), gr.skip()

    def rename(self, selected, name):
        try:
            asset = self.library.rename(selected, name)
        except IntegrationError as error:
            detail = errors.message(error.code) if error.code != errors.REQUEST_INVALID else f"Not renamed: {error.detail}."
            return gr.update(visible=True), self._grid(selected), _status(detail), *self._cards()
        return gr.update(visible=False), self._grid(asset.asset_id), _status(f"Renamed to {asset.filename}."), *self._cards()

    def open_delete(self, selected):
        asset = self._asset(selected)
        if asset is None:
            return gr.update(visible=False), _status("Select an image in the browser first.")
        return gr.update(visible=True), gr.skip()

    def delete(self, selected):
        try:
            asset = self.library.delete(selected)
        except IntegrationError as error:
            return (gr.update(visible=False), *self._refresh_outputs(errors.message(error.code), selected))
        cleared = self._clear_missing_slots()
        notes = [f"{', '.join(cleared)}: back to Use WanGP"] if cleared else []
        notes.append("history entries that used it now say Missing image")
        return (gr.update(visible=False), *self._refresh_outputs(f"Deleted {asset.filename}.", "", notes))

    # -- layout -------------------------------------------------------------------

    def build(self) -> None:
        current = config.load()
        draft = history.load_draft()
        cards = self._cards(draft)

        running = self._running()
        with gr.Column(elem_id=_id("root"), elem_classes=["minipaint-clipboard"]):
            with gr.Row(elem_id=_id("body"), elem_classes=["minipaint-clip-body"], equal_height=False):
                # ---- the browser --------------------------------------------------
                with gr.Column(scale=2, min_width=320, elem_id=_id("browser"), elem_classes=["minipaint-clip-browser"]):
                    with gr.Row(elem_id=_id("toolbar"), elem_classes=["minipaint-clip-toolbar"]):
                        menu_btn = gr.Button("☰ Menu", elem_id=_id("menu"), elem_classes=["minipaint-clip-action", "minipaint-clip-menu-button"], min_width=0)
                        to_first = gr.Button("+First", elem_id=_id("to_first"), elem_classes=["minipaint-clip-action", "minipaint-clip-role"], min_width=0)
                        to_last = gr.Button("+Last", elem_id=_id("to_last"), elem_classes=["minipaint-clip-action", "minipaint-clip-role"], min_width=0)
                        to_ref = gr.Button("+Ref", elem_id=_id("to_ref"), elem_classes=["minipaint-clip-action", "minipaint-clip-role"], min_width=0)
                        sort = gr.Dropdown(
                            [(config.SORT_LABELS[mode], mode) for mode in config.SORT_MODES], value=current.sort,
                            label="Sort", show_label=False, container=False, elem_id=_id("sort"), elem_classes=["minipaint-clip-sort"], min_width=120,
                        )
                        thumb = gr.Slider(
                            config.THUMBNAIL_MIN, config.THUMBNAIL_MAX, value=current.thumbnail, step=8,
                            label="Thumbnail size", show_label=False, container=False, elem_id=_id("thumb"), elem_classes=["minipaint-clip-thumb-size"], min_width=120,
                        )
                    grid = gr.HTML(self._grid(), elem_id=_id("grid"), elem_classes=["minipaint-clip-grid-host"])
                    status = gr.Markdown(
                        "Choose a storage folder from the menu to begin." if not current.configured else "Press Refresh to read the folder.",
                        elem_id=_id("status"), elem_classes=["minipaint-clip-status"],
                    )

                    # The dialogs: small panels, shown by the menu and put away by their own buttons.
                    with gr.Column(visible=not current.configured, elem_id=_id("folder_panel"), elem_classes=["minipaint-clip-panel"]) as folder_panel:
                        gr.Markdown(
                            "**Clipboard storage folder.** A folder on the machine running Forge - not on this device - "
                            "where Clipboard keeps its pictures. Changing it later moves nothing.",
                            elem_classes=["minipaint-clip-hint"],
                        )
                        folder_text = gr.Textbox(current.storage_root, label="Folder on the Forge host", placeholder="/path/on/forge/host", elem_id=_id("folder"))
                        with gr.Row(elem_classes=["minipaint-clip-pair"]):
                            folder_use = gr.Button("Use folder", variant="primary", elem_id=_id("folder_use"))
                            folder_create = gr.Button("Create it", elem_id=_id("folder_create"))
                            folder_close = gr.Button("Close", elem_id=_id("folder_close"))
                        folder_status = gr.Markdown("", elem_id=_id("folder_status"))
                    with gr.Column(visible=False, elem_id=_id("rename_panel"), elem_classes=["minipaint-clip-panel"]) as rename_panel:
                        rename_text = gr.Textbox("", label="New name (the extension stays)", elem_id=_id("rename_text"))
                        with gr.Row(elem_classes=["minipaint-clip-pair"]):
                            rename_ok = gr.Button("Rename", variant="primary", elem_id=_id("rename_ok"))
                            rename_cancel = gr.Button("Cancel", elem_id=_id("rename_cancel"))
                    with gr.Column(visible=False, elem_id=_id("delete_panel"), elem_classes=["minipaint-clip-panel"]) as delete_panel:
                        gr.Markdown("Delete the selected image from the folder? History keeps its record and will say Missing image.", elem_classes=["minipaint-clip-hint"])
                        with gr.Row(elem_classes=["minipaint-clip-pair"]):
                            delete_ok = gr.Button("Delete", variant="stop", elem_id=_id("delete_ok"))
                            delete_cancel = gr.Button("Cancel", elem_id=_id("delete_cancel"))
                    with gr.Column(visible=False, elem_id=_id("paste_panel"), elem_classes=["minipaint-clip-panel"]) as paste_panel:
                        gr.Markdown("This browser did not let the page read its clipboard directly. Paste into the box below instead (Ctrl+V), or drop a file on it.", elem_classes=["minipaint-clip-hint"])
                        paste_image = gr.Image(sources=["clipboard", "upload"], type="filepath", label="Paste here", elem_id=_id("paste"), height=200)
                        paste_close = gr.Button("Close", elem_id=_id("paste_close"))

                    # What the menu presses and writes. Hidden: the menu is their face.
                    refresh_btn = gr.Button("Refresh", visible=False, elem_id=_id("refresh"))
                    upload_btn = gr.UploadButton("Upload image(s)", file_types=["image"], file_count="multiple", type="filepath", visible=False, elem_id=_id("upload"))
                    intercept_btn = gr.Button("Intercept", visible=False, elem_id=_id("intercept"))
                    folder_open = gr.Button("Choose storage folder", visible=False, elem_id=_id("folder_open"))
                    rename_open = gr.Button("Rename selected", visible=False, elem_id=_id("rename_open"))
                    delete_open = gr.Button("Delete selected", visible=False, elem_id=_id("delete_open"))
                    paste_open = gr.Button("Paste", visible=False, elem_id=_id("paste_open"))
                    history_open = gr.Button("Queue Send History", visible=False, elem_id=_id("history_open"))
                    selected_box = gr.Textbox("", visible=False, elem_id=_id("selected"))
                    sort_request = gr.Textbox("", visible=False, elem_id=_id("sort_request"))
                    slot_action = gr.Textbox("", visible=False, elem_id=_id("slot_action"))
                    send_request = gr.Textbox("", visible=False, elem_id=_id("send_request"))
                    # The press that carries a send to the server.
                    #
                    # The request itself is the box above; this is only what
                    # tells the server to read it. Both are wired to the same
                    # callback, because the two ways a browser has of
                    # reaching a Gradio event are not equally reliable on
                    # every install: a written box relies on the framework
                    # noticing a scripted write, and on one user's Forge it
                    # never did - not one send acknowledged, across four
                    # builds, while a pressed button on the same page worked
                    # every time. A press is a DOM event and needs nothing
                    # noticed. See ClipboardTab.send for what makes the two
                    # of them arriving together harmless.
                    send_press = gr.Button("Send", visible=False, elem_id=_id("send_press"))
                    # The server's receipt for a send, carrying the stamp the
                    # browser put on the request. See ClipboardTab.send.
                    send_ack = gr.Textbox("", visible=False, elem_id=_id("send_ack"))
                    history_action = gr.Textbox("", visible=False, elem_id=_id("history_action"))
                    menu_state = gr.Textbox(self._menu_state(), visible=False, elem_id=_id("menu_state"))
                    switch_box = gr.Textbox("", visible=False, elem_id=_id("switch"))
                    payload_box = gr.Textbox("", visible=False, elem_id=_id("payload"))
                    to_canvas = gr.Textbox("", visible=False, elem_id=_id("to_canvas"))
                    mask_clear = gr.Textbox("", visible=False, elem_id=_id("mask_clear"))

                # ---- the composer --------------------------------------------------
                with gr.Column(scale=1, min_width=280, elem_id=_id("composer"), elem_classes=["minipaint-clip-composer"]):
                    gr.Markdown("**WanGP request**", elem_classes=["minipaint-clip-title"])
                    wangp_line = gr.HTML('<div class="minipaint-clip-wangp-line" data-state="unknown">WanGP: not checked yet</div>', elem_id=_id("wangp_line"))
                    with gr.Row(elem_id=_id("cards"), elem_classes=["minipaint-clip-cards"]):
                        card_first = gr.HTML(cards[0], elem_id=_id("card_first"), elem_classes=["minipaint-clip-card-host"])
                        card_last = gr.HTML(cards[1], elem_id=_id("card_last"), elem_classes=["minipaint-clip-card-host"])
                        card_ref = gr.HTML(cards[2], elem_id=_id("card_ref"), elem_classes=["minipaint-clip-card-host"])
                    slot_uploads = {
                        slot: gr.UploadButton(f"Choose {label}", file_types=["image"], type="filepath", visible=False, elem_id=_id(f"slot_upload_{slot}"))
                        for slot, label, _field in SLOTS
                    }
                    prompt = gr.Textbox(
                        draft["prompt_override"], lines=4, max_lines=12, label="Prompt", placeholder="Use current WanGP prompt",
                        elem_id=_id("prompt"), elem_classes=["minipaint-clip-prompt"],
                    )
                    enhanced_on = enhance.enabled()
                    with gr.Accordion("Prompt enhancement (ModelSwitchRefiner MiniMax H3)", open=enhanced_on, elem_id=_id("enhance_panel"),
                                      elem_classes=["minipaint-clip-enhance-panel"]):
                        enhance_line = gr.HTML(self._enhance_line(), elem_id=_id("enhance_line"))
                        enhance_toggle = gr.Checkbox(
                            value=enhanced_on, label="Enhance the prompt through MiniMax H3 before it reaches WanGP",
                            elem_id=_id("enhance_toggle"), elem_classes=["minipaint-clip-enhance-toggle"],
                        )
                        gr.Markdown(
                            "Off by default. On, a press sends the prompt typed here - and the pictures the model reads: first and last "
                            "frame for FL2VA, the reference for Ref2VA - to LLM Studio first, and WanGP gets the written prompt. The "
                            "variant is whichever MiniMax H3 model the WanGP page is on; a page on another model is refused, not enhanced.",
                            elem_classes=["minipaint-clip-hint"],
                        )
                        gr.Markdown("**System prompt** - the instructions the writer runs under. Four sets: each variant, with and without a picture.",
                                    elem_classes=["minipaint-clip-hint"])
                        with gr.Row(elem_classes=["minipaint-clip-pair"]):
                            sp_variant = gr.Dropdown(SP_VARIANT_CHOICES, value=enhance.FL2VA, label="Variant", elem_id=_id("sp_variant"), min_width=140)
                            sp_mode = gr.Dropdown(SP_MODE_CHOICES, value=enhance.MODE_TEXT, label="Instructions used", elem_id=_id("sp_mode"), min_width=140)
                        sp_box, sp_line = self._system_prompt_view(enhance.FL2VA, enhance.MODE_TEXT)
                        system_prompt = gr.Textbox(
                            sp_box.get("value", "") if isinstance(sp_box, dict) else "", lines=10, max_lines=40, label="System prompt",
                            elem_id=_id("system_prompt"), elem_classes=["minipaint-clip-system-prompt"],
                        )
                        sp_state = gr.Markdown(sp_line, elem_id=_id("sp_state"), elem_classes=["minipaint-clip-sp-state"])
                        with gr.Row(elem_classes=["minipaint-clip-pair"]):
                            sp_apply = gr.Button("Apply override", variant="primary", elem_id=_id("sp_apply"))
                            sp_restore = gr.Button("Restore default", elem_id=_id("sp_restore"))
                            sp_reload = gr.Button("Reload", elem_id=_id("sp_reload"))
                    queue_btn = gr.Button(
                        QUEUE_BUTTON_LABEL if running else QUEUE_BUTTON_BLOCKED, variant="primary", interactive=running,
                        elem_id=_id("queue"), elem_classes=["minipaint-clip-queue"],
                    )
                    queue_status = gr.Markdown("", elem_id=_id("queue_status"), elem_classes=["minipaint-clip-status"])
                    queue_instruction = gr.Textbox("", visible=False, elem_id=_id("queue_instruction"))
                    page_box = gr.Textbox("", visible=False, elem_id=_id("page_id"))
                    model_box = gr.Textbox("", visible=False, elem_id=_id("model"))
                    outbox_action = gr.Textbox("", visible=False, elem_id=_id("outbox_action"))
                    outbox_refresh = gr.Button("Refresh queue", visible=False, elem_id=_id("outbox_refresh"))
                    gr.Markdown("**Queue** - every request sent from this Forge, newest first. One is sent at a time, in the order pressed; "
                                "a prompt still being enhanced holds the line behind it.", elem_classes=["minipaint-clip-hint"])
                    cancel_all_btn = gr.Button("Cancel everything", variant="stop", elem_id=_id("cancel_all"), elem_classes=["minipaint-clip-cancel-all"])
                    outbox_list = gr.HTML(self._outbox(), elem_id=_id("outbox_list"), elem_classes=["minipaint-clip-outbox-host"])
                    with gr.Column(visible=False, elem_id=_id("history_panel"), elem_classes=["minipaint-clip-panel"]) as history_panel:
                        gr.Markdown("**Queue Send History** - recipes confirmed queued from here. Load puts one back into the composer; it queues nothing.", elem_classes=["minipaint-clip-hint"])
                        history_list = gr.HTML(self._history(), elem_id=_id("history_list"), elem_classes=["minipaint-clip-history-host"])
                        history_close = gr.Button("Close", elem_id=_id("history_close"))

        self._wire(
            grid=grid, status=status, selected_box=selected_box, menu_state=menu_state,
            cards=(card_first, card_last, card_ref), menu_btn=menu_btn, roles=(to_first, to_last, to_ref),
            sort=sort, sort_request=sort_request, thumb=thumb, refresh_btn=refresh_btn, upload_btn=upload_btn,
            intercept_btn=intercept_btn, folder_open=folder_open, folder_panel=folder_panel, folder_text=folder_text,
            folder_use=folder_use, folder_create=folder_create, folder_close=folder_close, folder_status=folder_status,
            rename_open=rename_open, rename_panel=rename_panel, rename_text=rename_text, rename_ok=rename_ok, rename_cancel=rename_cancel,
            delete_open=delete_open, delete_panel=delete_panel, delete_ok=delete_ok, delete_cancel=delete_cancel,
            paste_open=paste_open, paste_panel=paste_panel, paste_image=paste_image, paste_close=paste_close,
            slot_action=slot_action, slot_uploads=slot_uploads, prompt=prompt, queue_btn=queue_btn, queue_status=queue_status,
            queue_instruction=queue_instruction, page_box=page_box, model_box=model_box, outbox_action=outbox_action, outbox_refresh=outbox_refresh,
            outbox_list=outbox_list, cancel_all_btn=cancel_all_btn, history_open=history_open, history_panel=history_panel,
            enhance_line=enhance_line, enhance_toggle=enhance_toggle, sp_variant=sp_variant, sp_mode=sp_mode, system_prompt=system_prompt,
            sp_state=sp_state, sp_apply=sp_apply, sp_restore=sp_restore, sp_reload=sp_reload,
            history_list=history_list, history_close=history_close, history_action=history_action,
            send_request=send_request, send_press=send_press, send_ack=send_ack, switch_box=switch_box, payload_box=payload_box, to_canvas=to_canvas, mask_clear=mask_clear,
            wangp_line=wangp_line,
        )

    # -- wiring -------------------------------------------------------------------

    def _wire(self, **p) -> None:
        quiet = {"show_progress": "hidden"}
        cards = list(p["cards"])
        refresh_outputs = [p["grid"], p["status"], p["selected_box"], p["menu_state"], *cards, p["queue_btn"]]
        cards_outputs = [*cards, p["status"]]
        selected = p["selected_box"]

        # The page's load event attaches the browser script; the grid tells it
        # when it has been re-rendered so the selection and the sizes are re-applied.
        with contextlib.suppress(Exception):
            from gradio.context import Context

            # The only bundle in this extension nothing outside its own tab
            # addresses, and therefore the only one that can be left until
            # somebody opens that tab. It brings the public queue API and
            # the WanGP bridge with it, because it calls both.
            #
            # Registered on page load, but what it registers is a listener
            # on this tab's own nav button: the host builds the tab around
            # this block and an extension has no Gradio handle on it, so the
            # activation hook is in the browser. A panel that is already on
            # screen - a reload with this tab selected - loads at once.
            Context.root_block.load(None, js=ATTACH_WITH_BUNDLES_JS)
        p["menu_btn"].click(None, js=MENU_JS)
        p["grid"].change(None, js=SELECTED_JS, inputs=[p["grid"], selected])

        # -- the browser
        p["refresh_btn"].click(self.refresh, inputs=[selected], outputs=refresh_outputs, **quiet)
        p["sort"].input(self.sort_changed, inputs=[p["sort"], selected], outputs=[p["grid"], p["menu_state"]], **quiet)
        p["sort_request"].input(self.sort_request, inputs=[p["sort_request"], selected], outputs=[p["sort"], p["grid"], p["menu_state"]], **quiet)
        p["thumb"].change(None, js=THUMB_JS, inputs=[p["thumb"]])
        p["thumb"].release(self.thumbnail_changed, inputs=[p["thumb"]], outputs=[p["menu_state"]], **quiet)
        p["intercept_btn"].click(self.toggle_intercept, inputs=[], outputs=[p["menu_state"], p["status"]], **quiet)
        p["upload_btn"].upload(self.upload, inputs=[p["upload_btn"], selected], outputs=refresh_outputs, **quiet)
        p["paste_open"].click(lambda: gr.update(visible=True), inputs=[], outputs=[p["paste_panel"]], **quiet)
        p["paste_close"].click(lambda: gr.update(visible=False), inputs=[], outputs=[p["paste_panel"]], **quiet)
        p["paste_image"].upload(self.pasted, inputs=[p["paste_image"], selected], outputs=refresh_outputs + [p["paste_image"], p["paste_panel"]], **quiet)

        # -- the folder, rename, delete
        p["folder_open"].click(self.open_folder, inputs=[], outputs=[p["folder_panel"], p["folder_text"], p["folder_status"]], **quiet)
        p["folder_close"].click(lambda: gr.update(visible=False), inputs=[], outputs=[p["folder_panel"]], **quiet)
        p["folder_use"].click(lambda text: self.choose_folder(text, False), inputs=[p["folder_text"]], outputs=[p["folder_status"], p["folder_panel"], *refresh_outputs], **quiet)
        p["folder_create"].click(lambda text: self.choose_folder(text, True), inputs=[p["folder_text"]], outputs=[p["folder_status"], p["folder_panel"], *refresh_outputs], **quiet)
        p["rename_open"].click(self.open_rename, inputs=[selected], outputs=[p["rename_panel"], p["rename_text"], p["status"]], **quiet)
        p["rename_cancel"].click(lambda: gr.update(visible=False), inputs=[], outputs=[p["rename_panel"]], **quiet)
        p["rename_ok"].click(self.rename, inputs=[selected, p["rename_text"]], outputs=[p["rename_panel"], p["grid"], p["status"], *cards], **quiet)
        p["delete_open"].click(self.open_delete, inputs=[selected], outputs=[p["delete_panel"], p["status"]], **quiet)
        p["delete_cancel"].click(lambda: gr.update(visible=False), inputs=[], outputs=[p["delete_panel"]], **quiet)
        p["delete_ok"].click(self.delete, inputs=[selected], outputs=[p["delete_panel"], *refresh_outputs], **quiet)

        # -- the composer
        for (slot, _label, _field), button in zip(SLOTS, p["roles"]):
            button.click(lambda selected_id, slot=slot: self.assign(slot, selected_id), inputs=[selected], outputs=cards_outputs, **quiet)
        p["slot_action"].input(self.slot_action, inputs=[p["slot_action"]], outputs=cards_outputs, **quiet)
        for slot, upload in p["slot_uploads"].items():
            upload.upload(lambda file, selected_id, slot=slot: self.slot_upload(slot, file, selected_id), inputs=[upload, selected], outputs=refresh_outputs, **quiet)
        p["prompt"].blur(self.prompt_changed, inputs=[p["prompt"]], outputs=[], **quiet)

        # -- Add to Queue: the server builds the public request and appends
        # it to its outbox as this page's job; the instruction box's change
        # tells the browser to pump (the browser also watches the box briefly
        # after the click); the pump reports to the server's routes, and the
        # browser presses the hidden refresh so the list and the history are
        # re-rendered from what the server holds.
        p["queue_btn"].click(self.prepare_queue, inputs=[p["prompt"], p["page_box"], p["model_box"], p["enhance_toggle"]],
                             outputs=[p["queue_instruction"], p["queue_status"], p["outbox_list"], p["queue_btn"]], js=ARM_QUEUE_JS, **quiet)
        p["queue_instruction"].change(None, js=QUEUE_JS, inputs=[p["queue_instruction"]])
        p["outbox_refresh"].click(self.refresh_outbox, inputs=[p["page_box"]],
                                  outputs=[p["outbox_list"], p["queue_status"], p["history_list"], p["queue_btn"]], **quiet)
        p["outbox_action"].input(self.outbox_action, inputs=[p["outbox_action"], p["page_box"]], outputs=[p["outbox_list"], p["queue_status"]], **quiet)
        # Cancel everything: the server empties the line, then the browser
        # answers any public-API caller still waiting on one of those jobs.
        p["cancel_all_btn"].click(self.cancel_all, inputs=[p["page_box"]], outputs=[p["outbox_list"], p["queue_status"]], **quiet).then(
            None, js=AFTER_CANCEL_JS, inputs=[], outputs=[])

        # -- prompt enhancement: the switch and the four system prompts. The
        # model box is written by the browser from the public API's answer,
        # so the line above the switch follows the WanGP tab.
        p["enhance_toggle"].input(self.toggle_enhance, inputs=[p["enhance_toggle"], p["model_box"]], outputs=[p["enhance_line"]], **quiet)
        p["model_box"].input(self.model_changed, inputs=[p["model_box"]], outputs=[p["enhance_line"]], **quiet)
        for selector in (p["sp_variant"], p["sp_mode"]):
            selector.input(self.system_prompt_selected, inputs=[p["sp_variant"], p["sp_mode"]], outputs=[p["system_prompt"], p["sp_state"]], **quiet)
        p["sp_reload"].click(self.system_prompt_selected, inputs=[p["sp_variant"], p["sp_mode"]], outputs=[p["system_prompt"], p["sp_state"]], **quiet)
        p["sp_apply"].click(self.apply_override, inputs=[p["sp_variant"], p["sp_mode"], p["system_prompt"]],
                            outputs=[p["system_prompt"], p["sp_state"], p["queue_status"]], **quiet)
        p["sp_restore"].click(self.restore_default, inputs=[p["sp_variant"], p["sp_mode"]],
                              outputs=[p["system_prompt"], p["sp_state"], p["queue_status"]], **quiet)

        # -- history
        p["history_open"].click(self.show_history, inputs=[], outputs=[p["history_panel"], p["history_list"]], **quiet)
        p["history_close"].click(lambda: gr.update(visible=False), inputs=[], outputs=[p["history_panel"]], **quiet)
        p["history_action"].input(self.history_action, inputs=[p["history_action"], p["prompt"]], outputs=[p["prompt"], *cards, p["status"], p["history_list"]], **quiet)

        # -- send out: the same routes the Canvas takes, per destination.
        switch_box, payload_box = p["switch_box"], p["payload_box"]
        target_components = [self.targets[key] for key in self.image_targets]
        send_outputs = target_components + [switch_box, payload_box, p["to_canvas"], p["status"], p["send_ack"]]
        textbox_targets = [self.targets[key] for key in ("img2img", "inpaint") if key in self.targets]

        def after_send(sent):
            """The steps that finish a send, for one of the two triggers."""
            if len(textbox_targets) == 2:
                sent.then(None, js=canvas_ui.DELIVER_IMAGE_JS, inputs=[switch_box, payload_box], outputs=textbox_targets)
            elif textbox_targets:
                only = "img2img" if "img2img" in self.targets else "inpaint"
                sent.then(None, js=f"(target, payload) => [String(target || '').indexOf('{only}') === 0 ? payload : {canvas_ui._KEEP}]",
                          inputs=[switch_box, payload_box], outputs=textbox_targets)
            if self.stitch_targets:
                enables = [self.targets[f"{key}_enable"] for key in self.stitch_targets]
                sent.then(None, js=canvas_ui._stitch_enable_js(self.stitch_targets), inputs=[switch_box], outputs=enables)
            sent.then(None, js=SWITCH_JS, inputs=[switch_box], outputs=None)
            if "inpaint_mask" in self.targets:
                inpaint_uuid = getattr(self.targets["inpaint"], "elem_id", "") or ""
                sent.then(canvas_ui._noop, js=canvas_ui._host_wait_js(inpaint_uuid), inputs=[switch_box], outputs=None, **quiet).then(
                    None, js=canvas_ui.DELIVER_MASK_JS, inputs=[switch_box, p["mask_clear"]], outputs=[self.targets["inpaint_mask"]]
                )

        # Two triggers, one callback. The press is what the browser uses; the
        # written box is kept because it is what every build before this one
        # used, and an install where the press is the one that goes missing
        # is no more hypothetical than the install this was written for.
        # ``send`` answers the second arrival of a request with the receipt
        # it already gave, so nothing is delivered twice.
        after_send(p["send_press"].click(self.send, inputs=[p["send_request"], selected], outputs=send_outputs, **quiet))
        after_send(p["send_request"].input(self.send, inputs=[p["send_request"], selected], outputs=send_outputs, **quiet))
        # Mini Paint: the Canvas takes the picture through its own receive
        # chain, wired here because the Canvas was built first.
        if self.canvas is not None:
            self.canvas.receive_from(p["to_canvas"].change, self._picture_for_canvas, [p["to_canvas"]])

    def _picture_for_canvas(self, value):
        asset_id = _hex(str(value or "").split(":", 1)[0])
        if not asset_id:
            return None
        return self.library.open_image(asset_id)


# ----------------------------------------------------------------- the tab --


@contextlib.contextmanager
def _keep_build_context():
    """Build a tab without letting a failure take the rest of the WebUI with it.

    The same guard the Mini Paint and WanGP tabs use, for the same reason:
    Gradio's ``Blocks.__exit__`` does not put the render context back when
    the body raises.
    """
    try:
        from gradio.context import Context, get_render_context, set_render_context

        saved_block = get_render_context()
    except ImportError:  # pragma: no cover - Gradio 3.x
        from gradio.context import Context

        get_render_context = set_render_context = None
        saved_block = getattr(Context, "block", None)

    saved_root = getattr(Context, "root_block", None)
    try:
        yield
    finally:
        if set_render_context is not None:
            set_render_context(saved_block)
        else:  # pragma: no cover - Gradio 3.x
            Context.block = saved_block
        Context.root_block = saved_root


def create_ui() -> ClipboardTab:
    tab = ClipboardTab()
    tab.build()
    _current["tab"] = tab
    return tab


def _fallback_tab(reason: str):
    with gr.Blocks(analytics_enabled=False) as blocks:
        gr.Markdown(
            "### The Clipboard tab could not be built\n\n"
            f"`{reason}`\n\n"
            "The full traceback is in the WebUI console. Mini Paint, WanGP and the rest of the "
            "WebUI are unaffected, and the gallery's Send to Mini Paint works as it always did."
        )
    return blocks


def on_ui_tabs() -> list:
    """One tab, always. Built once, and never rebuilt."""
    try:
        with _keep_build_context():
            with gr.Blocks(analytics_enabled=False) as blocks:
                create_ui()
        return [(blocks, TAB_LABEL, TAB_ID)]
    except Exception as error:
        scrub.traceback_now(_LOG_PREFIX)
        scrub.console("the tab failed to build; Mini Paint and WanGP are unaffected.", _LOG_PREFIX)
        return [(_fallback_tab(f"{type(error).__name__}: {error}".strip()), TAB_LABEL, TAB_ID)]
