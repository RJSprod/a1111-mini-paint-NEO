"""Which WanGP models the Clipboard sends to, and whether the one in use is ready.

The Clipboard tab and the gallery's Send to WanGP popup send a request to the
model the WanGP page is on, and since 2026-10-03 only to three of them:

* **MiniMax H3 FL2VA** (``minimax_h3_fl2va`` and its finetunes - pruned, PDD,
  VDN): a first and a last frame;
* **MiniMax H3 Ref2VA** (``minimax_h3_ref2va`` and its finetunes): references;
* **LTX 2.3 Distilled** (WanGP's ``ltx2_22B`` architecture on its distilled
  pipeline - ``ltx2_22B_distilled``, ``_distilled_1_1`` and the GGUF builds of
  both, and any finetune that declares the same): a first and a last frame,
  and never a reference.

Not LTX 2.3 Dev, not LTX 2.5, not the EditAnything or MSR workflows built on
2.3, which are other architectures in WanGP's own definitions.

READY MEANS FOUR THINGS, and the section is blocked - prompt, enhancement,
image cards, Add to Queue, the popup's body - until all four are true:

1. WanGP is running (``WANGP_NOT_RUNNING``);
2. its bridge answers the model check (``TARGET_CHECK_UNAVAILABLE``);
3. the page is on one of the three (``TARGET_UNKNOWN`` when it has not said,
   ``TARGET_UNSUPPORTED`` when it is on another);
4. this WanGP defines it and has every file it would fetch before generating
   (``TARGET_NOT_DEFINED``, ``TARGET_NOT_DOWNLOADED``).

The fourth is asked of the bridge (``control.model``, bridge 1.13.0), which
walks WanGP's own download path without downloading anything. A bridge that
could not finish that walk - a WanGP whose internals moved - answers
"could not tell", and that is reported, not treated as a missing file: a
check that cannot run is not evidence the model is absent, and blocking on it
would take the Clipboard away from a working WanGP. Everything else blocks.

Who asks: the composer and the popup through their routes, to draw
themselves; and ``outbox.submit``, at the press, for the Clipboard's and the
gallery's own requests - fresh, never from the cache, because the press is
the moment that decides. A public-API caller (``origin`` ``api``) is not
gated here: another extension chooses its own model and has its own answer.

Nothing here logs a prompt or a path; the journal sees model types, codes and
counts.
"""

from __future__ import annotations

import re
import threading
import time
import typing

from ..wangp import errors, protocol
from ..wangp.errors import IntegrationError
from . import enhance

TARGET_FL2VA = enhance.FL2VA
TARGET_REF2VA = enhance.REF2VA
TARGET_LTX23 = enhance.LTX23
TARGETS = (TARGET_FL2VA, TARGET_REF2VA, TARGET_LTX23)
LABELS = {
    TARGET_FL2VA: "MiniMax H3 FL2VA",
    TARGET_REF2VA: "MiniMax H3 Ref2VA",
    TARGET_LTX23: "LTX 2.3 Distilled",
}
SUPPORTED_SENTENCE = "MiniMax H3 FL2VA, MiniMax H3 Ref2VA or LTX 2.3 Distilled"

#: The public request fields each target is sent. MiniMax keeps the overlay
#: rule it always had - a field the model ignores is sent, badged and ignored
#: by WanGP - so both its variants list all three. LTX 2.3 Distilled is sent a
#: first and a last frame and nothing else: a reference is not offered, not
#: staged and not sent (WanGP's LTX 2.3 can take an "ingredients" sheet
#: through its control video, and that is exactly what must not happen by
#: accident).
FIELDS: typing.Dict[str, typing.Tuple[str, ...]] = {
    TARGET_FL2VA: (protocol.QUEUE_FIELD_START, protocol.QUEUE_FIELD_END, protocol.QUEUE_FIELD_REFERENCES),
    TARGET_REF2VA: (protocol.QUEUE_FIELD_START, protocol.QUEUE_FIELD_END, protocol.QUEUE_FIELD_REFERENCES),
    TARGET_LTX23: (protocol.QUEUE_FIELD_START, protocol.QUEUE_FIELD_END),
}

#: WanGP's own names for LTX 2.3: the architecture of every 2.3 checkpoint
#: (its definitions' ``architecture``), and the pipeline a distilled one
#: declares (``ltx2_pipeline``).
LTX23_ARCHITECTURE = "ltx2_22B"
LTX23_PIPELINE = "distilled"

#: How long a model check stays fresh for a screen. The press never reads
#: the cache; a model downloaded meanwhile is seen at the next press, and on
#: the next screen read once this has run out.
CHECK_TTL = 10.0
#: How long a failed check is remembered, so a screen that asks twice in a row
#: while the bridge is not answering does not wait out two timeouts.
FAILURE_TTL = 3.0

#: A text-to-speech model that happens to carry "ref2va" in its name is not a
#: video model and is not a target.
_TTS = re.compile(r"(?:\A|[^a-z0-9])tts(?:[^a-z0-9]|\Z)", re.IGNORECASE)

_lock = threading.RLock()
_cache: typing.Dict[str, typing.Tuple[float, typing.Any]] = {}
_seams: typing.Dict[str, typing.Any] = {"readiness": None, "clock": time.monotonic}


def _journal(message: str) -> None:
    try:
        from ..wangp import process_log

        process_log.note("clipboard", message)
    except Exception:
        pass


# ------------------------------------------------------------------- seams --


def use_readiness(answer: typing.Optional[typing.Callable[[typing.Any, bool], dict]]) -> None:
    """Test seam: a callable ``(model, fresh) -> view`` standing in for the
    whole decision. None restores the real one."""
    with _lock:
        _seams["readiness"] = answer


def always_ready(model: typing.Any = None, fresh: bool = False) -> dict:
    """The seam every suite that is not about readiness runs under: the gate
    as it was before this module - ready for whatever the page says it is on,
    and refused only when a page would have to drive the job and WanGP is
    stopped - so a suite written before the gate keeps meaning what it
    meant. ``reset_for_tests`` installs it; ``use_readiness(None)`` is the
    real decision."""
    from . import outbox

    block = enhance.model_block(model)
    if outbox.chosen_executor() != outbox.EXECUTOR_SERVER and not outbox.wangp_running():
        return _blocked(errors.WANGP_NOT_RUNNING, block, message=errors.message(errors.WANGP_NOT_RUNNING))
    target = classify(block)
    return _view(True, "", "Ready.", block, target, checked=True)


def reset_for_tests() -> None:
    """Back to a clean cache, the real clock, and - like every other seam in
    this package's test resets - the permissive answer: a suite that is not
    about readiness is not made to fake a running WanGP to press a button.
    The readiness suite restores the real decision with ``use_readiness(None)``."""
    with _lock:
        _cache.clear()
        _seams["readiness"] = always_ready
        _seams["clock"] = time.monotonic


def forget() -> None:
    """Drop every cached check, so the next read asks the bridge again."""
    with _lock:
        _cache.clear()


# ---------------------------------------------------------------- the model --


def classify(model: typing.Any, facts: typing.Optional[typing.Mapping[str, typing.Any]] = None) -> str:
    """Which target a WanGP model is, or "" for one the Clipboard does not send to.

    MiniMax by the enhancer's own reading of the model's names (a finetune
    names its base architecture), less anything that is a text-to-speech
    model. LTX 2.3 Distilled by architecture and pipeline: the bridge's facts
    when there are any - they read the definition itself - and the model's
    own names otherwise, where a distilled checkpoint says so.
    """
    block = enhance.model_block(model)
    names = " ".join(str(block.get(key) or "") for key in ("type", "label"))
    if facts:
        names += " " + str(facts.get("label") or "")
    variant = enhance.variant_for_model(block)
    if variant in (TARGET_FL2VA, TARGET_REF2VA):
        return "" if _TTS.search(names.replace("_", " ")) else variant
    architecture = str((facts or {}).get("architecture") or "") or str(block.get("architecture") or "")
    if architecture != LTX23_ARCHITECTURE:
        return ""
    pipeline = str((facts or {}).get("pipeline") or "")
    if facts and facts.get("defined"):
        return TARGET_LTX23 if pipeline == LTX23_PIPELINE else ""
    return TARGET_LTX23 if LTX23_PIPELINE in names.lower() else ""


def fields_for(target: str) -> typing.Tuple[str, ...]:
    """The request fields a target is sent; all three for anything else."""
    return FIELDS.get(target, (protocol.QUEUE_FIELD_START, protocol.QUEUE_FIELD_END, protocol.QUEUE_FIELD_REFERENCES))


def narrow(request: typing.MutableMapping[str, typing.Any], target: str) -> typing.List[str]:
    """Take out of a public request every image field the target is not sent.

    Returns the fields removed. Today that is a reference for LTX 2.3
    Distilled, and nothing for MiniMax.
    """
    images = request.get("images") if isinstance(request.get("images"), dict) else None
    if not images:
        return []
    allowed = fields_for(target)
    removed = [field for field in list(images) if field not in allowed and images.get(field)]
    for field in removed:
        images.pop(field, None)
    if not images:
        request.pop("images", None)
    return removed


# ---------------------------------------------------------------- readiness --


def _view(ready: bool, code: str, message: str, block: typing.Mapping[str, typing.Any], target: str,
          checked: bool = False, missing: int = 0) -> dict:
    return {
        "ready": bool(ready),
        "code": "" if ready else str(code or errors.INTERNAL_ERROR),
        "message": str(message or "")[:400],
        "target": target if target in TARGETS else "",
        "target_label": LABELS.get(target, ""),
        "model": dict(block),
        "fields": list(fields_for(target)) if target in TARGETS else [],
        "checked": bool(checked),
        "missing_count": int(missing),
        "supported": SUPPORTED_SENTENCE,
    }


def _blocked(code: str, block: typing.Mapping[str, typing.Any], target: str = "", message: str = "", **extra: typing.Any) -> dict:
    return _view(False, code, message or errors.message(code), block, target, **extra)


def _facts(model_type: str, fresh: bool) -> typing.Tuple[typing.Optional[dict], str]:
    """``(facts, code)``: the bridge's answer about one model, or why there is none."""
    clock = _seams["clock"]
    now = clock()
    with _lock:
        cached = _cache.get(model_type)
    if cached is not None and not fresh:
        at, value = cached
        ttl = CHECK_TTL if isinstance(value, dict) else FAILURE_TTL
        if now - at <= ttl:
            return (value, "") if isinstance(value, dict) else (None, str(value))
    from ..wangp import control

    try:
        facts = control.model(model_type)
    except IntegrationError as error:
        code = errors.WANGP_NOT_RUNNING if error.code == errors.WANGP_NOT_RUNNING else errors.TARGET_CHECK_UNAVAILABLE
        with _lock:
            _cache[model_type] = (now, code)
        return None, code
    except Exception:
        with _lock:
            _cache[model_type] = (now, errors.TARGET_CHECK_UNAVAILABLE)
        return None, errors.TARGET_CHECK_UNAVAILABLE
    if not facts.get("ok"):
        with _lock:
            _cache[model_type] = (now, errors.TARGET_CHECK_UNAVAILABLE)
        return None, errors.TARGET_CHECK_UNAVAILABLE
    with _lock:
        _cache[model_type] = (now, facts)
    return facts, ""


def readiness(model: typing.Any = None, *, fresh: bool = False) -> dict:
    """Whether the Clipboard may send to the model the WanGP page is on, now.

    ``model`` is the page's model as its bridge described it (``type``,
    ``label``, ``family``, ``architecture``); without one, the model WanGP
    last said it was on. ``fresh`` skips the cache - the press asks that way.
    Never raises. The answer is the view both screens draw: ``ready``,
    ``code`` and a ``message`` that names the model, the ``target`` and its
    label, and the ``fields`` it is sent.
    """
    seam = _seams.get("readiness")
    if seam is not None:
        try:
            found = seam(model, fresh)
        except Exception as error:
            return _blocked(errors.INTERNAL_ERROR, enhance.model_block(model), message=f"Readiness could not be read ({type(error).__name__}).")
        return found if isinstance(found, dict) else _blocked(errors.INTERNAL_ERROR, enhance.model_block(model))
    try:
        return _readiness(model, fresh)
    except Exception as error:
        return _blocked(errors.INTERNAL_ERROR, enhance.model_block(model), message=f"Readiness could not be read ({type(error).__name__}).")


def _readiness(model: typing.Any, fresh: bool) -> dict:
    from . import outbox

    block = enhance.model_block(model)
    if not outbox.wangp_running():
        return _blocked(errors.WANGP_NOT_RUNNING, block, message=errors.message(errors.WANGP_NOT_RUNNING))
    model_type = block.get("type") or ""
    if not model_type:
        try:
            from ..wangp import control

            hello = control.last_hello() or {}
        except Exception:
            hello = {}
        model_type = str(hello.get("model_type") or "")
        if model_type:
            block = dict(block, type=model_type[:120])
    if not model_type or not protocol.MODEL_TYPE_RE.match(model_type):
        return _blocked(errors.TARGET_UNKNOWN, block)
    facts, code = _facts(model_type, fresh)
    if facts is None:
        if code == errors.WANGP_NOT_RUNNING:
            return _blocked(code, block, message=errors.message(errors.WANGP_NOT_RUNNING))
        return _blocked(code, block)
    if not block.get("label") and facts.get("label"):
        block = dict(block, label=str(facts["label"])[:120])
    if not block.get("architecture") and facts.get("architecture"):
        block = dict(block, architecture=str(facts["architecture"])[:120])
    label = block.get("label") or model_type
    target = classify(block, facts)
    if not target:
        return _blocked(errors.TARGET_UNSUPPORTED, block, message=(
            f"WanGP is on {label}. This sends only to {SUPPORTED_SENTENCE}; choose one in the WanGP tab."))
    if not facts.get("defined"):
        return _blocked(errors.TARGET_NOT_DEFINED, block, target, message=f"{label} is not defined in this WanGP.")
    missing = int(facts.get("missing_count") or 0) if facts.get("checked") else 0
    if missing:
        return _blocked(errors.TARGET_NOT_DOWNLOADED, block, target, checked=True, missing=missing, message=(
            f"{label} is not downloaded yet ({missing} file{'s' if missing != 1 else ''} missing). "
            "Generate with it once in the WanGP tab so WanGP fetches them."))
    note = "" if facts.get("checked") else " Its files could not be checked."
    return _view(True, "", f"Ready: WanGP is on {label} ({LABELS[target]}).{note}", block, target,
                 checked=bool(facts.get("checked")))


def require_ready(model: typing.Any = None) -> dict:
    """The press's own check, fresh. Returns the view, or raises with it.

    The IntegrationError carries the whole view as ``readiness``, so the
    screen that pressed can say the model's name and redraw itself blocked
    from the one answer, without asking again.
    """
    found = readiness(model, fresh=True)
    if not found["ready"]:
        _journal(f"press refused: {found['code']} ({(found.get('model') or {}).get('type') or 'no model'})")
        raise IntegrationError(found["code"], found["message"][:200], readiness=found)
    return found


__all__ = [
    "CHECK_TTL", "FAILURE_TTL", "FIELDS", "LABELS", "LTX23_ARCHITECTURE", "LTX23_PIPELINE", "SUPPORTED_SENTENCE",
    "TARGETS", "TARGET_FL2VA", "TARGET_LTX23", "TARGET_REF2VA", "always_ready", "classify", "fields_for", "forget",
    "narrow", "readiness", "require_ready", "reset_for_tests", "use_readiness",
]
