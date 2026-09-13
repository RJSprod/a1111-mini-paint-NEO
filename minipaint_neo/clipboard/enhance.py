"""Prompt enhancement through ModelSwitchRefiner's MiniMax H3 writer.

Off by default. When it is on, an Add to Queue press does not go to WanGP
with the prompt that was typed: the prompt, and the pictures the H3 model
would look at, go first to the *SD-Neo-ModelSwitchRefiner* extension's
external LLM API (``mc_llm_api``, documented in that repository under
``docs/21-external-llm-api.md``), which writes an H3 prompt exactly as its
LLM Studio panel would; the job waits in the outbox as ``enhancing`` until
that prompt exists, and then goes to WanGP carrying it. The typed prompt is
kept beside it, so the tab can show both and a retry can start again from
what the user wrote.

Three facts shape everything below:

* **It is an import, not a URL.** The other extension runs in this same
  Forge process and says so: its API is a module to import, with no route,
  no port and no token. So the loader here finds that extension's folder
  and imports ``mc_llm_api`` from it, once, and every call afterwards is a
  function call. Nothing of this module is reachable from a browser except
  through the outbox routes, which is where the pressing page already is.

* **The variant is WanGP's model, not a choice.** ``fl2va`` and ``ref2va``
  are the two H3 model definitions WanGP loads (``minimax_h3_fl2va`` and
  ``minimax_h3_ref2va``), and the prompt has to be written for the one the
  page is on. The page reports its model with the press; a page on any
  other model is refused rather than enhanced for a model it is not
  running. The system prompt the writer uses is that variant's - one of the
  four the API publishes (with and without a picture, for each variant) -
  unless an override saved here replaces it.

* **Pictures follow the model.** FL2VA writes about a first frame (a last
  frame may be supplied and the API says which one it described); Ref2VA
  writes about reference material. A picture in a slot the variant does not
  read is left out of the enhancement - it still goes to WanGP with the job,
  it is simply not what the prompt is written about - and the job record
  says what was left out. The API would otherwise describe whichever
  picture it was given, which is a prompt about the wrong thing.

Nothing here logs a prompt, an override, a caption or a picture: the journal
sees ids, variants, states and elapsed seconds.
"""

from __future__ import annotations

import importlib
import pathlib
import sys
import threading
import typing

from ..wangp import errors, protocol
from ..wangp.errors import IntegrationError
from . import config

ENHANCE_NAME = "clipboard-enhance.json"
SCHEMA = 1

#: The module the other extension publishes, and the contract version this
#: caller was written against. A newer major version is refused rather than
#: guessed at; a newer minor one is fine, by that document's own rule.
API_MODULE = "mc_llm_api"
API_VERSION_SUPPORTED = 1
#: The label every request is submitted under. It is the one caller string
#: that reaches the other extension's console and banner, so it names us.
ORIGIN = "minipaint-clipboard"
CANCEL_REASON = "cancelled from the MiniPaint Clipboard tab"

#: The two H3 variants, spelled the way the API spells them.
FL2VA = "fl2va"
REF2VA = "ref2va"
VARIANTS = (FL2VA, REF2VA)
VARIANT_LABELS = {FL2VA: "FL2VA (first / last frame)", REF2VA: "Ref2VA (reference)"}
#: What names a MiniMax H3 model in WanGP's model types (``minimax_h3_fl2va``).
MODEL_KEY = "minimax"

#: The two instruction sets each variant has, by whether a picture is sent.
MODE_TEXT = "text"
MODE_IMAGE = "image"
MODES = (MODE_TEXT, MODE_IMAGE)
MODE_LABELS = {MODE_TEXT: "without a picture", MODE_IMAGE: "with a picture"}

#: The API's picture slots, and which of a queue request's image fields feeds
#: which slot for each variant. A field that is not listed for the variant
#: is dropped from the enhancement (see the module docstring).
SLOT_FIRST = "first_frame"
SLOT_LAST = "last_frame"
SLOT_REFERENCE = "reference"
SLOTS = (SLOT_FIRST, SLOT_LAST, SLOT_REFERENCE)
SLOTS_FOR: typing.Dict[str, typing.Dict[str, str]] = {
    FL2VA: {protocol.QUEUE_FIELD_START: SLOT_FIRST, protocol.QUEUE_FIELD_END: SLOT_LAST},
    REF2VA: {protocol.QUEUE_FIELD_REFERENCES: SLOT_REFERENCE},
}
SLOT_LABELS = {SLOT_FIRST: "the first frame", SLOT_LAST: "the last frame", SLOT_REFERENCE: "the reference"}

#: The API's job states, as its document lists them.
LLM_QUEUED = "queued"
LLM_RUNNING = "running"
LLM_DONE = "done"
LLM_FAILED = "failed"
LLM_CANCELLED = "cancelled"
LLM_STATES = (LLM_QUEUED, LLM_RUNNING, LLM_DONE, LLM_FAILED, LLM_CANCELLED)
LLM_TERMINAL = (LLM_DONE, LLM_FAILED, LLM_CANCELLED)

#: An override longer than this is a mistake, not instructions.
MAX_SYSTEM_PROMPT_CHARS = 20000

#: The API's refusal codes, as this extension reports them. A code it does
#: not know is a refusal it cannot retry its way past, by the API's own rule.
REJECTIONS = {
    "disabled": errors.ENHANCE_UNAVAILABLE,
    "empty_prompt": errors.ENHANCE_PROMPT_REQUIRED,
    "empty_system_prompt": errors.ENHANCE_SYSTEM_PROMPT_EMPTY,
    "bad_image": errors.ENHANCE_IMAGE_UNREADABLE,
    "no_vision": errors.ENHANCE_NO_VISION,
    "queue_full": errors.ENHANCE_QUEUE_FULL,
}

_LOG_PREFIX = "MiniPaint Clipboard:"

_lock = threading.RLock()
_state: typing.Dict[str, typing.Any] = {"api": None, "injected": None, "directory": None, "failure": ""}


# ---------------------------------------------------------------- seams --


def use_api(module: typing.Any) -> None:
    """Test seam: a module standing in for ``mc_llm_api``. None restores discovery."""
    with _lock:
        _state["injected"] = module
        _state["api"] = None
        _state["failure"] = ""


def reset_for_tests() -> None:
    with _lock:
        _state.update({"api": None, "injected": None, "directory": None, "failure": ""})


def _journal(message: str) -> None:
    try:
        from ..wangp import process_log

        process_log.note("enhance", message)
    except Exception:
        pass


# --------------------------------------------------------------- the api --


def _usable(module: typing.Any) -> bool:
    return module is not None and callable(getattr(module, "submit_minimax", None)) and hasattr(module, "Rejected")


def _module_dir(name: str) -> typing.Optional[pathlib.Path]:
    module = sys.modules.get(name)
    file = getattr(module, "__file__", "") if module is not None else ""
    return pathlib.Path(file).resolve().parent if file else None


def _candidate_dirs() -> typing.List[pathlib.Path]:
    """Where the other extension may be, most certain first, without a path
    from anyone's configuration: its own already-imported modules, the host's
    extension list, the host's extension folders, and the folder this
    extension is itself installed in."""
    found: typing.List[pathlib.Path] = []

    def offer(candidate: typing.Any) -> None:
        try:
            path = pathlib.Path(str(candidate)).resolve()
        except Exception:
            return
        if path not in found and (path / (API_MODULE + ".py")).is_file():
            found.append(path)

    for name in (API_MODULE, "mc_llm_jobs", "mc_llm_studio", "mc_llm_runtime", "mc_llm_sessions", "mc_llm_ui"):
        directory = _module_dir(name)
        if directory is not None:
            offer(directory)
    try:
        from modules import extensions as host_extensions  # type: ignore[import-not-found]

        for extension in getattr(host_extensions, "extensions", None) or []:
            offer(getattr(extension, "path", ""))
    except Exception:
        pass
    parents: typing.List[pathlib.Path] = []
    try:
        from modules import paths as host_paths  # type: ignore[import-not-found]

        for attribute in ("extensions_dir", "extensions_builtin_dir"):
            value = getattr(host_paths, attribute, None)
            if value:
                parents.append(pathlib.Path(str(value)))
    except Exception:
        pass
    try:
        parents.append(pathlib.Path(__file__).resolve().parents[2].parent)
    except Exception:
        pass
    for parent in parents:
        try:
            children = sorted(child for child in parent.iterdir() if child.is_dir())
        except Exception:
            continue
        for child in children:
            offer(child)
    return found


def _import_from(directory: pathlib.Path) -> typing.Any:
    """Import the API off its folder, the way the host imported that
    extension's own scripts: with the folder on ``sys.path``. Appended, never
    inserted, so nothing of theirs can shadow the host's own modules; and
    left there, because the API imports its neighbours lazily by their bare
    names and would otherwise fail on the first call."""
    text = str(directory)
    if text not in sys.path:
        sys.path.append(text)
    module = importlib.import_module(API_MODULE)
    if not _usable(module):
        raise ImportError(f"{API_MODULE} is not the ModelSwitchRefiner external API")
    return module


def api() -> typing.Any:
    """The ``mc_llm_api`` module, or None when it is not in this Forge.

    Found once and kept; not finding it is re-tried on every call, because
    the other extension may load after this one, and a press is the moment
    that matters. Never raises.
    """
    with _lock:
        if _state["injected"] is not None:
            return _state["injected"]
        if _usable(_state["api"]):
            return _state["api"]
        module = sys.modules.get(API_MODULE)
        if _usable(module):
            _state["api"] = module
            _state["failure"] = ""
            return module
        for directory in _candidate_dirs():
            try:
                module = _import_from(directory)
            except Exception as error:
                _state["failure"] = type(error).__name__
                continue
            _state["api"] = module
            _state["directory"] = directory
            _state["failure"] = ""
            _journal(f"mc_llm_api imported from the ModelSwitchRefiner extension (API version {getattr(module, 'API_VERSION', '?')})")
            return module
        return None


def capabilities() -> dict:
    """What the other extension can do right now, in this extension's words.

    ``available`` is the one flag a press is gated on: the module is here,
    LLM Studio is switched on and a language model is set up. ``vision`` is
    advisory - a request with a picture is refused by the API itself when
    the model cannot see, with a code this module knows. Never raises.
    """
    found = {
        "found": False, "available": False, "api_version": 0, "enabled": False, "configured": False,
        "vision": False, "model": "", "variants": list(VARIANTS), "max_queued": 0, "reason": "",
    }
    module = api()
    if module is None:
        why = f" ({_state['failure']})" if _state.get("failure") else ""
        found["reason"] = f"The ModelSwitchRefiner extension (mc_llm_api) was not found in this Forge{why}."
        return found
    found["found"] = True
    try:
        raw = module.capabilities()
    except Exception as error:
        found["reason"] = f"ModelSwitchRefiner did not answer ({type(error).__name__})."
        return found
    raw = raw if isinstance(raw, dict) else {}
    version = raw.get("api_version")
    found["api_version"] = int(version) if isinstance(version, int) and not isinstance(version, bool) else 0
    # A first-revision API had no switch and no "enabled" field; silence means on.
    found["enabled"] = raw.get("enabled", True) is not False
    found["configured"] = raw.get("configured") is True
    found["vision"] = raw.get("vision") is True
    found["model"] = pathlib.Path(str(raw.get("model") or "")).name[:80]
    variants = [item for item in (raw.get("variants") or []) if item in VARIANTS] if isinstance(raw.get("variants"), (list, tuple)) else []
    found["variants"] = variants or list(VARIANTS)
    capacity = raw.get("max_queued")
    found["max_queued"] = int(capacity) if isinstance(capacity, int) and not isinstance(capacity, bool) and capacity > 0 else 0
    found["available"] = found["enabled"] and found["configured"]
    if found["api_version"] > API_VERSION_SUPPORTED:
        found["available"] = False
        found["reason"] = (f"ModelSwitchRefiner speaks external LLM API version {found['api_version']}; "
                           f"this extension knows version {API_VERSION_SUPPORTED}. Update Mini Paint.")
    elif not found["available"]:
        found["reason"] = str(raw.get("reason") or "")[:300] or "LLM Studio is switched off or has no model set up."
    elif not found["vision"]:
        found["reason"] = str(raw.get("reason") or "")[:300]
    return found


# ------------------------------------------------------------- the model --


def variant_for_model(model: typing.Any) -> str:
    """Which H3 variant a WanGP model is, or "" for a model that is not one.

    Read from the model's type and architecture first, then its family and
    label, because a finetune of an H3 model names the base architecture
    rather than itself. A MiniMax model that is neither variant is "" too:
    the writer knows two instruction sets and guessing a third would write
    the wrong prompt.
    """
    model = model if isinstance(model, dict) else {}
    parts = [str(model.get(key) or "").lower() for key in ("type", "architecture", "family", "label")]
    if not any(MODEL_KEY in part for part in parts):
        return ""
    for part in parts:
        if REF2VA in part:
            return REF2VA
        if FL2VA in part:
            return FL2VA
    return ""


def model_block(raw: typing.Any) -> dict:
    """A model as a page reports it: four short strings, nothing else."""
    raw = raw if isinstance(raw, dict) else {}
    return {key: str(raw.get(key) or "")[:120] for key in ("type", "label", "family", "architecture")}


def plan(request: typing.Mapping[str, typing.Any], model: typing.Any) -> dict:
    """What an enhancement of this request would be, or a refusal.

    Decided before anything is stored or asked: the variant from the page's
    model, the prompt (the typed one is required - the page's own prompt is
    WanGP's and is not read from here), and which supplied picture goes to
    which slot. Fields the variant does not read are listed as dropped.
    """
    block = model_block(model)
    variant = variant_for_model(block)
    if not variant:
        raise IntegrationError(errors.ENHANCE_MODEL_UNSUPPORTED, f"the page is on {block.get('type') or 'no known model'}")
    if request.get("prompt") is None:
        raise IntegrationError(errors.ENHANCE_PROMPT_REQUIRED, "the request inherits the page's prompt")
    images = request.get("images") if isinstance(request.get("images"), dict) else {}
    mapping = SLOTS_FOR[variant]
    slots: typing.Dict[str, typing.Any] = {}
    dropped: typing.List[str] = []
    extra = 0
    for field in (protocol.QUEUE_FIELD_START, protocol.QUEUE_FIELD_END, protocol.QUEUE_FIELD_REFERENCES):
        value = images.get(field)
        if not value:
            continue
        slot = mapping.get(field)
        if slot is None:
            dropped.append(field)
            continue
        if field == protocol.QUEUE_FIELD_REFERENCES:
            slots[slot] = value[0]
            extra = max(0, len(value) - 1)
        else:
            slots[slot] = value
    return {"variant": variant, "slots": slots, "dropped": dropped, "extra_references": extra, "has_image": bool(slots), "model": block}


# --------------------------------------------------------- the settings --


def _normalize_document(raw: typing.Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    overrides_raw = raw.get("overrides") if isinstance(raw.get("overrides"), dict) else {}
    overrides: typing.Dict[str, typing.Dict[str, str]] = {}
    for variant in VARIANTS:
        block = overrides_raw.get(variant) if isinstance(overrides_raw.get(variant), dict) else {}
        kept = {}
        for mode in MODES:
            text = block.get(mode)
            if isinstance(text, str) and text.strip():
                kept[mode] = text[:MAX_SYSTEM_PROMPT_CHARS]
        if kept:
            overrides[variant] = kept
    return {"schema": SCHEMA, "enabled": raw.get("enabled") is True, "overrides": overrides}


def _document() -> dict:
    return _normalize_document(config.read_document(ENHANCE_NAME, {}))


def _write(document: dict) -> dict:
    cleaned = _normalize_document(document)
    config.write_document(ENHANCE_NAME, cleaned)
    return cleaned


def enabled() -> bool:
    """Whether a press is enhanced. Read from disk every time, so every
    browser and a Reload UI see one setting. Off until somebody turns it on."""
    try:
        return _document()["enabled"]
    except Exception:
        return False


def set_enabled(flag: typing.Any) -> bool:
    document = _document()
    document["enabled"] = bool(flag)
    _write(document)
    _journal(f"enhanced prompts switched {'on' if document['enabled'] else 'off'}")
    return document["enabled"]


def _check(variant: typing.Any, mode: typing.Any) -> typing.Tuple[str, str]:
    if variant not in VARIANTS:
        raise IntegrationError(errors.REQUEST_INVALID, f"unknown variant {str(variant)[:20]!r}")
    if mode not in MODES:
        raise IntegrationError(errors.REQUEST_INVALID, f"unknown mode {str(mode)[:20]!r}")
    return variant, mode


def override(variant: typing.Any, mode: typing.Any) -> str:
    """The saved override for one of the four instruction sets, or ""."""
    variant, mode = _check(variant, mode)
    return _document()["overrides"].get(variant, {}).get(mode, "")


def set_override(variant: typing.Any, mode: typing.Any, text: typing.Any) -> str:
    """Replace one instruction set for every request from now on. Kept on
    disk, so it survives a restart; blank is refused, not stored."""
    variant, mode = _check(variant, mode)
    cleaned = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not cleaned.strip():
        raise IntegrationError(errors.ENHANCE_SYSTEM_PROMPT_EMPTY, "a blank override")
    if len(cleaned) > MAX_SYSTEM_PROMPT_CHARS:
        raise IntegrationError(errors.REQUEST_INVALID, f"{len(cleaned)} characters of system prompt")
    document = _document()
    document["overrides"].setdefault(variant, {})[mode] = cleaned
    _write(document)
    _journal(f"system prompt override saved for {variant} {mode} ({len(cleaned)} characters)")
    return cleaned


def clear_override(variant: typing.Any, mode: typing.Any) -> bool:
    """Back to the API's default for that set. True when there was one."""
    variant, mode = _check(variant, mode)
    document = _document()
    had = mode in document["overrides"].get(variant, {})
    if had:
        del document["overrides"][variant][mode]
        if not document["overrides"][variant]:
            del document["overrides"][variant]
        _write(document)
        _journal(f"system prompt override cleared for {variant} {mode}")
    return had


def overrides() -> typing.Dict[str, typing.Dict[str, bool]]:
    """Which of the four sets are overridden, without their text."""
    saved = _document()["overrides"]
    return {variant: {mode: mode in saved.get(variant, {}) for mode in MODES} for variant in VARIANTS}


def default_prompt(variant: typing.Any, mode: typing.Any) -> str:
    """The API's own instructions for one set, or "" when it cannot say."""
    variant, mode = _check(variant, mode)
    module = api()
    if module is None:
        return ""
    try:
        return str(module.system_prompt(variant, has_image=(mode == MODE_IMAGE)) or "")
    except Exception:
        return ""


def effective_prompt(variant: typing.Any, mode: typing.Any) -> typing.Tuple[str, str]:
    """``(text, source)``: the instructions a request would run under, and
    whether they are an ``override``, the ``default``, or ``unavailable``."""
    saved = override(variant, mode)
    if saved:
        return saved, "override"
    text = default_prompt(variant, mode)
    return (text, "default") if text else ("", "unavailable")


def system_prompts() -> dict:
    """All four defaults with their structure guides, as the API publishes
    them; empty when it is not here."""
    module = api()
    if module is None:
        return {}
    try:
        raw = module.system_prompts()
    except Exception:
        return {}
    out = {}
    for variant in VARIANTS:
        block = raw.get(variant) if isinstance(raw, dict) and isinstance(raw.get(variant), dict) else None
        if block is None:
            continue
        out[variant] = {
            "label": str(block.get("label") or VARIANT_LABELS[variant])[:80],
            MODE_TEXT: str(block.get("text") or ""),
            MODE_IMAGE: str(block.get("image") or ""),
            "structure": str(block.get("structure") or ""),
            "max_tokens": int(block["max_tokens"]) if isinstance(block.get("max_tokens"), int) else 0,
        }
    return out


# ------------------------------------------------------------ the request --


def _picture(handle: typing.Any) -> typing.Any:
    """The picture behind a request's image handle, as a PIL image - never
    a path, so the API's saved history names the slot and not a file."""
    from .. import interop

    return interop.open_handle(handle)


def submit(prompt: str, planned: typing.Mapping[str, typing.Any]) -> dict:
    """Ask for the H3 prompt. Returns the API's job id; refuses with a code.

    Everything the API needs is assembled here and handed over once: the
    typed prompt, the variant, the pictures in the slots the plan chose, and
    this extension's override for that variant and picture-ness when one is
    saved. ``remember`` stays on, so the finished prompt is filed in LLM
    Studio's own Saved prompts exactly as a panel run's would be.
    """
    module = api()
    if module is None:
        raise IntegrationError(errors.ENHANCE_UNAVAILABLE, "mc_llm_api is not importable")
    ready = capabilities()
    if not ready["available"]:
        raise IntegrationError(errors.ENHANCE_UNAVAILABLE, ready["reason"])
    variant = planned["variant"] if planned.get("variant") in VARIANTS else FL2VA
    keywords: typing.Dict[str, typing.Any] = {"variant": variant, "origin": ORIGIN, "remember": True}
    pictures = []
    try:
        for slot in SLOTS:
            handle = (planned.get("slots") or {}).get(slot)
            if not handle:
                continue
            try:
                picture = _picture(handle)
            except IntegrationError:
                raise
            except Exception as error:
                raise IntegrationError(errors.ENHANCE_IMAGE_UNREADABLE, f"{slot}: {type(error).__name__}")
            pictures.append(picture)
            keywords[slot] = picture
        replacement = override(variant, MODE_IMAGE if pictures else MODE_TEXT)
        if replacement:
            keywords["system_prompt"] = replacement
        try:
            identifier = module.submit_minimax(str(prompt), **keywords)
        except Exception as error:
            code = getattr(error, "code", "")
            if isinstance(error, getattr(module, "Rejected", ())) or code:
                raise IntegrationError(REJECTIONS.get(str(code), errors.ENHANCE_REFUSED), f"{code}: {getattr(error, 'reason', error)}")
            raise IntegrationError(errors.ENHANCE_REFUSED, f"{type(error).__name__}")
    finally:
        for picture in pictures:
            try:
                picture.close()
            except Exception:
                pass
    llm_id = str(identifier or "")[:64]
    if not llm_id:
        raise IntegrationError(errors.ENHANCE_REFUSED, "the API returned no job id")
    _journal(f"llm {llm_id[:8]}: {variant} enhancement requested ({', '.join(slot for slot in SLOTS if slot in keywords) or 'no picture'}"
             f"{'; override' if replacement else ''})")
    return {"llm_id": llm_id, "system_override": bool(replacement), "variant": variant}


def _seconds(value: typing.Any) -> float:
    return round(float(value), 1) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else 0.0


def status(llm_id: typing.Any) -> typing.Optional[dict]:
    """One request as the API describes it, in this extension's shape, or
    None once the API has forgotten it. Never raises."""
    module = api()
    if module is None or not isinstance(llm_id, str) or not llm_id:
        return None
    try:
        raw = module.status(llm_id)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    state = raw.get("state") if raw.get("state") in LLM_STATES else LLM_RUNNING
    position = raw.get("position")
    used = raw.get("image_used") if raw.get("image_used") in SLOTS else ""
    ignored = [item for item in (raw.get("image_ignored") or []) if item in SLOTS] if isinstance(raw.get("image_ignored"), (list, tuple)) else []
    found = {
        "state": state,
        "stage": str(raw.get("stage") or "")[:160],
        "position": int(position) if isinstance(position, int) and not isinstance(position, bool) and position >= 0 else 0,
        "elapsed": _seconds(raw.get("elapsed")),
        "queued_for": _seconds(raw.get("queued_for")),
        "image_used": used,
        "image_ignored": ignored,
        "system_override": raw.get("system_override") is True,
        "cancelling": raw.get("cancelling") is True,
        "error": str(raw.get("error") or "")[:200] if state == LLM_FAILED else "",
        "reason": str(raw.get("reason") or "")[:200] if state == LLM_CANCELLED else "",
        "prompt": str(raw.get("prompt") or "") if state == LLM_DONE else "",
    }
    return found


def cancel(llm_id: typing.Any, reason: str = CANCEL_REASON) -> dict:
    """Stop one request, queued or running. Never raises."""
    module = api()
    if module is None or not isinstance(llm_id, str) or not llm_id:
        return {"ok": False, "code": "unavailable"}
    try:
        answer = module.cancel(llm_id, reason)
    except Exception as error:
        return {"ok": False, "code": type(error).__name__}
    return answer if isinstance(answer, dict) else {"ok": False, "code": "unreadable"}


def cancel_all(reason: str = CANCEL_REASON) -> int:
    """Cancel every request this extension submitted, running or waiting.
    Ours only, by origin - the API insists, and so would anyone else's panel."""
    module = api()
    if module is None:
        return 0
    try:
        answer = module.cancel_all(ORIGIN, reason)
    except Exception:
        return 0
    count = answer.get("cancelled") if isinstance(answer, dict) else 0
    return int(count) if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 0


# ----------------------------------------------------------- for the tab --


def availability(model: typing.Any = None) -> dict:
    """What the tab says above the switch: the LLM side, then WanGP's model.

    ``state`` is ``ready`` when a press would be enhanced, ``blocked`` when
    the LLM side cannot take one, ``model`` when only WanGP's model stands
    in the way, and ``unknown`` while the page has not said its model.
    """
    ready = capabilities()
    block = model_block(model)
    variant = variant_for_model(block)
    if not ready["available"]:
        return {"state": "blocked", "text": ready["reason"], "variant": variant, "capabilities": ready}
    llm = f"LLM Studio ready ({ready['model'] or 'a language model'}" + ("" if ready["vision"] else "; no vision, so pictures cannot be described") + ")."
    label = block.get("label") or block.get("type")
    if not label:
        return {"state": "unknown", "text": llm + " WanGP's model is not known yet; open the WanGP tab.", "variant": "", "capabilities": ready}
    if not variant:
        return {"state": "model", "text": llm + f" WanGP is on {label}, which is not a MiniMax H3 model; enhanced presses are refused until FL2VA or Ref2VA is loaded.",
                "variant": "", "capabilities": ready}
    return {"state": "ready", "text": llm + f" WanGP is on {label}: prompts are written as {VARIANT_LABELS[variant]}.", "variant": variant, "capabilities": ready}


def describe() -> dict:
    """For a caller of the public API: the switch, the LLM side, the rules."""
    ready = capabilities()
    return {
        "enabled": enabled(),
        "origin": ORIGIN,
        "variants": list(VARIANTS),
        "slots": {variant: dict(mapping) for variant, mapping in SLOTS_FOR.items()},
        "overrides": overrides(),
        "capabilities": ready,
    }


__all__ = [
    "API_MODULE", "API_VERSION_SUPPORTED", "CANCEL_REASON", "ENHANCE_NAME", "FL2VA", "LLM_CANCELLED", "LLM_DONE", "LLM_FAILED",
    "LLM_QUEUED", "LLM_RUNNING", "LLM_STATES", "LLM_TERMINAL", "MAX_SYSTEM_PROMPT_CHARS", "MODES", "MODE_IMAGE", "MODE_LABELS", "MODE_TEXT",
    "ORIGIN", "REF2VA", "REJECTIONS", "SLOTS", "SLOTS_FOR", "SLOT_FIRST", "SLOT_LABELS", "SLOT_LAST", "SLOT_REFERENCE", "VARIANTS",
    "VARIANT_LABELS", "api", "availability", "cancel", "cancel_all", "capabilities", "clear_override", "default_prompt", "describe",
    "effective_prompt", "enabled", "model_block", "override", "overrides", "plan", "reset_for_tests", "set_enabled", "set_override",
    "status", "submit", "system_prompts", "use_api", "variant_for_model",
]
