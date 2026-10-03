"""Is one model set up in this WanGP? The facts behind the Clipboard's block.

Bridge 1.13.0. Forge's Clipboard tab sends to three WanGP models - MiniMax H3
FL2VA, MiniMax H3 Ref2VA and LTX 2.3 Distilled - and blocks its WanGP section
when the one the WanGP page is on is not set up: not defined in this WanGP, or
not downloaded. The second is the expensive surprise. WanGP fetches a model's
files when a generation first needs them, so a request for a model that was
never loaded is a request that starts a download of tens of gigabytes before
anything is generated; the user asked to be told before pressing instead.

What this answers is facts, never a verdict: whether WanGP defines the model,
the two keys of its definition that say what it is (``architecture``, and
``ltx2_pipeline``, which a distilled LTX-2 checkpoint declares), and which of
the files WanGP would fetch before generating are not on disk. Which models
are supported, and what a missing file means, is Forge's decision
(``minipaint_neo/clipboard/targets.py``).

HOW THE FILES ARE FOUND. Exactly the way WanGP finds them, by WanGP's own
functions, in the order its own ``load_models`` and ``download_models`` ask -
and with every download step replaced by "is it there":

* the transformer file(s), chosen by ``get_model_filename`` from the user's
  quantization and dtype settings, and the modules a definition adds;
* ``preload_URLs`` and ``VAE_URLs``, and the definition's own LoRAs;
* the handler's ``query_model_files`` - VAEs, encoders' tokenizers, upscalers -
  checked with WanGP's own ``download_def_missing_files``;
* the text encoder, from ``text_encoder_URLs`` and the text encoder
  quantization, in its own folder.

Nothing is downloaded, loaded or written, and no network is touched: a
definition whose file list WanGP would have to ask Hugging Face for (a whole
folder) is answered by whether that folder is there, as WanGP's own check
does. A WanGP whose internals moved, so that one of these steps raises,
answers ``checked: False`` with the name of what failed - "could not tell",
which Forge reports as such and never as "everything is there".
"""

from __future__ import annotations

import os
import sys
import typing

try:
    from . import protocol
except ImportError:  # pragma: no cover - depends on how WanGP imports plugins
    import protocol  # type: ignore[no-redef]

#: Where WanGP keeps its file locator and its download helpers.
FILES_LOCATOR_MODULE = "shared.utils.files_locator"
DOWNLOAD_MODULE = "shared.utils.download"


class _Missing:
    """The running tally: every file looked at, and the names not found."""

    def __init__(self) -> None:
        self.files = 0
        self.names: typing.List[str] = []
        self.count = 0

    def need(self, name: typing.Any, found: bool) -> None:
        self.files += 1
        if found:
            return
        self.count += 1
        base = os.path.basename(str(name or "").split("|", 1)[0].rstrip("/\\")) or str(name or "")[:80]
        if base not in self.names and len(self.names) < protocol.MODEL_MISSING_NAMES_MAX:
            self.names.append(base[:160])


def _module(name: str) -> typing.Any:
    module = sys.modules.get(name)
    if module is None:
        import importlib

        module = importlib.import_module(name)
    return module


def facts(wgp: typing.Any, model_type: str, *, locator: typing.Any = None, downloads: typing.Any = None) -> dict:
    """The facts about ``model_type`` in the WanGP whose module is ``wgp``.

    ``locator`` and ``downloads`` stand in for WanGP's own two helper modules
    in the tests; in WanGP they are imported from where WanGP keeps them.
    Never raises.
    """
    found: typing.Dict[str, typing.Any] = {
        "ok": True, "model_type": str(model_type or "")[:200], "defined": False, "label": "",
        "architecture": "", "pipeline": "", "checked": False, "files": 0, "missing_count": 0,
        "missing": [], "diagnosis": "", "code": "", "message": "",
    }
    getter = getattr(wgp, "get_model_def", None) if wgp is not None else None
    if not callable(getter):
        found.update(ok=False, code=protocol.CONTROL_UNAVAILABLE, message="WanGP's model definitions are not reachable")
        return found
    try:
        definition = getter(model_type)
    except Exception as error:
        definition = None
        found["diagnosis"] = f"get_model_def: {type(error).__name__}"
    if not isinstance(definition, dict):
        return found
    found["defined"] = True
    found["label"] = str(definition.get("name") or "")[:120]
    found["architecture"] = str(definition.get("architecture") or "")[:120]
    found["pipeline"] = str(definition.get("ltx2_pipeline") or "")[:40]
    tally = _Missing()
    try:
        locator = locator if locator is not None else _module(FILES_LOCATOR_MODULE)
        downloads = downloads if downloads is not None else _module(DOWNLOAD_MODULE)
        _check(wgp, model_type, definition, locator, downloads, tally)
    except Exception as error:
        found["diagnosis"] = f"{type(error).__name__}: {str(error)[:120]}"
        return found
    found.update(checked=True, files=tally.files, missing_count=tally.count, missing=list(tally.names))
    return found


def _check(wgp: typing.Any, model_type: str, definition: dict, locator: typing.Any, downloads: typing.Any,
           tally: _Missing) -> None:
    """The dry run of WanGP's own download path. Raises on anything unexpected."""
    base = wgp.get_base_model_type(model_type)
    handlers = getattr(wgp, "model_types_handlers", None) or {}
    handler = handlers.get(base) if isinstance(handlers, dict) else None
    quantization = str(getattr(wgp, "transformer_quantization", "") or "")
    dtype_policy = getattr(wgp, "transformer_dtype_policy", "")
    text_quantization = str(getattr(wgp, "text_encoder_quantization", "") or "")

    resolver = getattr(handler, "resolve_runtime_model_def", None)
    if callable(resolver):
        server_config = getattr(wgp, "server_config", None) or {}
        definition = resolver(definition, dict(
            server_config, transformer_quantization=quantization, text_encoder_quantization=text_quantization,
            mixed_precision=server_config.get("mixed_precision", "0"), vae_precision=server_config.get("vae_precision", "16")))

    # The transformer, a second one when the definition has two, and the
    # modules it adds - each with the source flag download_models reads.
    names: typing.List[typing.Tuple[str, int]] = []
    names.append((wgp.get_model_filename(model_type=model_type, quantization=quantization, dtype_policy=dtype_policy,
                                         model_def=definition), 0))
    if "URLs2" in definition:
        names.append((wgp.get_model_filename(model_type=model_type, quantization=quantization, dtype_policy=dtype_policy,
                                             submodel_no=2, model_def=definition), 0))
    modules = wgp.get_model_recursive_prop(model_type, "modules", return_list=True, model_def=definition) or []
    modules = [wgp.get_model_recursive_prop(module, "modules", sub_prop_name="_list", return_list=True)
               if isinstance(module, str) else module for module in modules]
    for module in modules:
        if isinstance(module, dict):
            for key in ("URLs", "URLs2"):
                urls = module.get(key)
                if urls:
                    names.append((wgp.get_model_filename(model_type, quantization, dtype_policy, URLs=urls), 1))
        else:
            names.append((wgp.get_model_filename(model_type, quantization, dtype_policy, module_type=module), 1))
    for name, source_type in names:
        if not name:
            continue
        if source_type == 0 and "source" in definition:
            continue
        if source_type == 1 and "module_source" in definition:
            continue
        tally.need(name, locator.get_local_model_filename(name) is not None)

    try:
        lora_dir = wgp.get_lora_dir(model_type)
    except Exception:
        lora_dir = None
    preload = list(wgp.get_model_recursive_prop(model_type, "preload_URLs", return_list=True, model_def=definition) or [])
    vae = definition.get("VAE_URLs", [])
    preload += [vae] if isinstance(vae, str) else list(vae or [])
    for url in preload:
        tally.need(url, locator.get_local_model_filename(url, lora_dir=lora_dir) is not None)
    loras = wgp.get_model_recursive_prop(model_type, "loras", return_list=True, model_def=definition) or []
    local_path = getattr(wgp, "get_lora_local_path", None)
    for url in loras if isinstance(loras, (list, tuple)) else [loras]:
        if callable(local_path) and lora_dir is not None:
            tally.need(url, os.path.isfile(local_path(lora_dir, url)))

    query = getattr(handler, "query_model_files", None)
    if callable(query):
        def compute_list(filename: typing.Any) -> typing.List[str]:
            if filename is None:
                return []
            return [str(filename)[str(filename).rfind("/") + 1:]]

        defs = query(compute_list, base, definition)
        for one in (defs if isinstance(defs, list) else [defs]):
            if not isinstance(one, dict):
                continue
            counted = sum(len(files) if files else 1 for files in (one.get("fileList") or []))
            gone = downloads.download_def_missing_files(one) or []
            tally.files += max(0, counted - len(gone))
            for name in gone:
                tally.need(name, False)

    urls = wgp.get_model_recursive_prop(model_type, "text_encoder_URLs", return_list=True, model_def=definition)
    if urls:
        name = wgp.get_model_filename(model_type=model_type, quantization=text_quantization, dtype_policy=dtype_policy, URLs=urls)
        if name:
            folder = definition.get("text_encoder_folder", None)
            tally.need(name, locator.get_local_model_filename(name, extra_paths=folder) is not None)
