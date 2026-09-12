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
button: an entirely empty composition asks the live WanGP page to queue
itself exactly as it is, and a slot the current model cannot use is kept,
badged, and reported - not a reason to refuse. The button asks through
``window.minipaintInterop`` - the public API any extension may call - and
never through a private shortcut.

The tab is built once, inside the same guard the WanGP tab uses, and a tab
that cannot be built is a tab that says so under the same label and id.
"""

from __future__ import annotations

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
from . import TAB_ID, TAB_LABEL, config, history, routes, store

PREFIX = "minipaint_clipboard"


def _id(name: str) -> str:
    return f"{PREFIX}_{name}"


# Every browser-side helper lives in javascript/minipaint_clipboard.js. Each
# is called from a Gradio event on a user action, or from the page's load
# event, and none of them runs on a timer for longer than one bounded wait.
_JS = "window.minipaintClipboard"
ATTACH_JS = f"() => {{ if ({_JS}) {_JS}.attach(); }}"
MENU_JS = f"() => {{ if ({_JS}) {_JS}.toggleMenu(); }}"
THUMB_JS = f"(size) => {{ if ({_JS}) {_JS}.setThumbnailSize(size); }}"
# The queue instruction box changes when the server has built a request;
# the browser hands it to the public API and writes the result back.
QUEUE_JS = f"(instruction) => {{ if ({_JS}) {_JS}.queue(instruction); }}"
ARM_QUEUE_JS = f"(prompt, session) => {{ if ({_JS}) {_JS}.armQueue(); return [prompt, session]; }}"
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

#: How many prepared requests one browser page may have in flight at once
#: before the oldest recipe is forgotten. The FIFO makes one the norm.
MAX_PENDING = 8

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


def _status(message: str, notes: typing.Sequence[str] = ()) -> str:
    parts = [message] if message else []
    parts.extend(f"<small>{note}</small>" for note in notes if note)
    return " ".join(parts)


def _hex(value: typing.Any) -> str:
    return value if protocol.valid_handoff_id(value) else ""


def _nonce() -> str:
    return secrets.token_hex(4)


# ----------------------------------------------------------------- the tab --


class ClipboardTab:
    """Builds the tab and owns its callbacks. One instance per mounted UI."""

    def __init__(self) -> None:
        self.library = store.store()
        self.targets = host.destinations()
        self.image_targets = [key for key in canvas_ui.BACKEND_TARGETS if key in self.targets]
        self.stitch_targets = [key for key in canvas_ui.STITCH_TARGETS if key in self.targets]
        self.canvas = canvas_ui.current()
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
        return (self._grid(selected), _status(message, notes), _hex(selected), self._menu_state(), *cards)

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
        return (self._grid(asset.asset_id), _status(message, ["imported into the folder first"]), asset.asset_id, self._menu_state(), *self._cards(draft))

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

    def prepare_queue(self, prompt, session):
        """Add to Queue: the draft as a public request, for the browser to send.

        Everything inherited is omitted from the request. A slot whose file
        is gone fails here, before WanGP is asked, and keeps the draft.
        """
        session = session if isinstance(session, dict) else {"pending": {}}
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
            self._journal(f"queue clicked; refused before asking - {labels.lower()} missing from the folder")
            return "", _status(f"{labels}: the image is no longer in the folder. Press Refresh, then try again.",
                               ["nothing was asked of WanGP; the rest of the draft is kept"]), session
        request = history.public_request(draft)
        pending = session.setdefault("pending", {})
        pending[request["request_id"]] = {"draft": draft}
        while len(pending) > MAX_PENDING:
            pending.pop(next(iter(pending)))
        self._journal(f"queue clicked (request {request['request_id'][:8]}; overrides {', '.join(history.draft_overrides(draft)) or 'none'})")
        instruction = json.dumps({"nonce": _nonce(), "request": request})
        return instruction, _status("Asking WanGP to add the request to its queue…"), session

    def queue_result(self, value, session):
        """How the browser says it went: a history record on a confirmed
        admission, a sentence otherwise. Nothing in the answer is trusted
        beyond its shape; the prompt is never in it."""
        session = session if isinstance(session, dict) else {"pending": {}}
        try:
            raw = json.loads(value) if isinstance(value, str) and value.strip() else {}
        except ValueError:
            raw = {}
        raw = raw if isinstance(raw, dict) else {}
        request_id = _hex(raw.get("request_id"))
        status = raw.get("status") if raw.get("status") in ("queued", "refused", "unconfirmed") else "refused"
        code = raw.get("code") if isinstance(raw.get("code"), str) and protocol.CODE_RE.match(raw.get("code")) else ""
        pending = session.get("pending", {}).pop(request_id, None) if request_id else None
        summary = protocol.queue_summary(raw.get("applied"), raw.get("inherited"), raw.get("ignored"))
        model = raw.get("model") if isinstance(raw.get("model"), dict) else {}
        tasks = raw.get("tasks_added") if isinstance(raw.get("tasks_added"), (int, float)) and not isinstance(raw.get("tasks_added"), bool) else 0

        if raw.get("ok") is True and status == "queued" and pending is not None:
            record = history.make_record(pending["draft"], {
                "request_id": request_id, "applied": summary["applied"], "inherited": summary["inherited"],
                "ignored": summary["ignored"], "tasks_added": int(tasks), "model": model,
            })
            history.add_history(record)
            notes = [f"{FIELD_LABELS.get(item['field'], item['field'])} was not used by the current model." for item in summary["ignored"]]
            self._journal(f"queue {request_id[:8]} queued; {int(tasks)} task(s); history recorded")
            return _status("Added to WanGP queue.", notes), self._history(), session

        code = code or (errors.ADMISSION_UNCONFIRMED if status == "unconfirmed" else errors.QUEUE_REQUEST_REFUSED)
        self._journal(f"queue {request_id[:8] or '?'} {status} ({code})")
        return _status(errors.message(code), ["no history was recorded"]), gr.skip(), session

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
        """Send the selected picture to another tab, the way the Canvas does it."""
        parts = str(request or "").split(":")
        target = parts[0] if parts else ""
        asset_id = _hex(parts[1]) if len(parts) > 1 else ""
        skips = [gr.skip() for _ in self.image_targets]
        label = dict(self.destinations).get(target, target)
        if target not in dict(self.destinations):
            return (*skips, "", "", "", _status(f"{label or 'That destination'} is not available in this WebUI."))
        asset = self._asset(asset_id or selected)
        if asset is None:
            return (*skips, "", "", "", _status("Select an image in the browser first."))
        try:
            image = self.library.open_image(asset.asset_id)
        except IntegrationError as error:
            return (*skips, "", "", "", _status(errors.message(error.code)))
        nonce = _nonce()
        if target == "minipaint":
            return (*skips, "", "", f"{asset.asset_id}:{nonce}", _status(f"Sent {asset.filename} to Mini Paint."))
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
        return (*outputs, instruction, payload, "", _status(f"Sent {asset.filename} to {label}.", notes))

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

        with gr.Column(elem_id=_id("root"), elem_classes=["minipaint-clipboard"]):
            session = gr.State({"pending": {}})
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
                    queue_btn = gr.Button("Add to Queue", variant="primary", elem_id=_id("queue"), elem_classes=["minipaint-clip-queue"])
                    queue_status = gr.Markdown("", elem_id=_id("queue_status"), elem_classes=["minipaint-clip-status"])
                    queue_instruction = gr.Textbox("", visible=False, elem_id=_id("queue_instruction"))
                    queue_result = gr.Textbox("", visible=False, elem_id=_id("queue_result"))
                    with gr.Column(visible=False, elem_id=_id("history_panel"), elem_classes=["minipaint-clip-panel"]) as history_panel:
                        gr.Markdown("**Queue Send History** - recipes confirmed queued from here. Load puts one back into the composer; it queues nothing.", elem_classes=["minipaint-clip-hint"])
                        history_list = gr.HTML(self._history(), elem_id=_id("history_list"), elem_classes=["minipaint-clip-history-host"])
                        history_close = gr.Button("Close", elem_id=_id("history_close"))

        self._wire(
            session=session, grid=grid, status=status, selected_box=selected_box, menu_state=menu_state,
            cards=(card_first, card_last, card_ref), menu_btn=menu_btn, roles=(to_first, to_last, to_ref),
            sort=sort, sort_request=sort_request, thumb=thumb, refresh_btn=refresh_btn, upload_btn=upload_btn,
            intercept_btn=intercept_btn, folder_open=folder_open, folder_panel=folder_panel, folder_text=folder_text,
            folder_use=folder_use, folder_create=folder_create, folder_close=folder_close, folder_status=folder_status,
            rename_open=rename_open, rename_panel=rename_panel, rename_text=rename_text, rename_ok=rename_ok, rename_cancel=rename_cancel,
            delete_open=delete_open, delete_panel=delete_panel, delete_ok=delete_ok, delete_cancel=delete_cancel,
            paste_open=paste_open, paste_panel=paste_panel, paste_image=paste_image, paste_close=paste_close,
            slot_action=slot_action, slot_uploads=slot_uploads, prompt=prompt, queue_btn=queue_btn, queue_status=queue_status,
            queue_instruction=queue_instruction, queue_result=queue_result, history_open=history_open, history_panel=history_panel,
            history_list=history_list, history_close=history_close, history_action=history_action,
            send_request=send_request, switch_box=switch_box, payload_box=payload_box, to_canvas=to_canvas, mask_clear=mask_clear,
            wangp_line=wangp_line,
        )

    # -- wiring -------------------------------------------------------------------

    def _wire(self, **p) -> None:
        quiet = {"show_progress": "hidden"}
        cards = list(p["cards"])
        refresh_outputs = [p["grid"], p["status"], p["selected_box"], p["menu_state"], *cards]
        cards_outputs = [*cards, p["status"]]
        selected = p["selected_box"]

        # The page's load event attaches the browser script; the grid tells it
        # when it has been re-rendered so the selection and the sizes are re-applied.
        with contextlib.suppress(Exception):
            from gradio.context import Context

            Context.root_block.load(None, js=ATTACH_JS)
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

        # -- Add to Queue: the server builds the public request, the browser
        # sends it through window.minipaintInterop, the result comes back
        # through the hidden box. The instruction box's change is the first
        # route; the browser also watches the box briefly after the click.
        p["queue_btn"].click(self.prepare_queue, inputs=[p["prompt"], p["session"]], outputs=[p["queue_instruction"], p["queue_status"], p["session"]], js=ARM_QUEUE_JS, **quiet)
        p["queue_instruction"].change(None, js=QUEUE_JS, inputs=[p["queue_instruction"]])
        p["queue_result"].input(self.queue_result, inputs=[p["queue_result"], p["session"]], outputs=[p["queue_status"], p["history_list"], p["session"]], **quiet)

        # -- history
        p["history_open"].click(self.show_history, inputs=[], outputs=[p["history_panel"], p["history_list"]], **quiet)
        p["history_close"].click(lambda: gr.update(visible=False), inputs=[], outputs=[p["history_panel"]], **quiet)
        p["history_action"].input(self.history_action, inputs=[p["history_action"], p["prompt"]], outputs=[p["prompt"], *cards, p["status"], p["history_list"]], **quiet)

        # -- send out: the same routes the Canvas takes, per destination.
        switch_box, payload_box = p["switch_box"], p["payload_box"]
        target_components = [self.targets[key] for key in self.image_targets]
        sent = p["send_request"].input(
            self.send, inputs=[p["send_request"], selected],
            outputs=target_components + [switch_box, payload_box, p["to_canvas"], p["status"]], **quiet,
        )
        textbox_targets = [self.targets[key] for key in ("img2img", "inpaint") if key in self.targets]
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
