"""The Clipboard's WanGP targets: which models it sends to, and when it may.

Since 2026-10-03 the Clipboard tab and the gallery's Send to WanGP popup send
to three WanGP models - MiniMax H3 FL2VA, MiniMax H3 Ref2VA and LTX 2.3
Distilled - and block their WanGP section unless the one the page is on is
ready: WanGP running, its bridge answering, the model one of the three, defined
and downloaded. This suite holds the whole of that, from the bridge's facts
(bridge 1.13.0's model check, against a working model of WanGP's own download
path) through ``targets.readiness`` and the press's own fresh gate in
``outbox.submit``, to the LTX 2.3 writer the enhancement reaches through
ModelSwitchRefiner's ``submit_ltx`` - shown the first frame and nothing else -
and the popup's half: no Reference for LTX, and its body blocked by the same
answer.

Every other suite runs under ``targets.always_ready`` - the gate as it was
before - which ``outbox.reset_for_tests`` installs; this one asks for the real
decision (``targets.use_readiness(None)``) and fakes what it reads: the
runtime (``outbox.use_running``) and the control plane
(``control.use_transport``).
"""

from harness import Results, setup_path

setup_path()

import importlib  # noqa: E402
import json  # noqa: E402
import pathlib  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import types  # noqa: E402

from PIL import Image  # noqa: E402

from harness import ROOT  # noqa: E402
from minipaint_neo import interop  # noqa: E402
from minipaint_neo.clipboard import config, enhance, executor, intercept, outbox, store, targets  # noqa: E402
from minipaint_neo.wangp import config as wangp_config  # noqa: E402
from minipaint_neo.wangp import control, errors, process_log, protocol  # noqa: E402
from minipaint_neo.wangp.errors import IntegrationError  # noqa: E402
from test_clipboard_enhance import FakeApi, FakeRejected  # noqa: E402

BRIDGE_DIR = ROOT / "wan2gp_bridge" / "wan2gp-minipaint-bridge"
PAGE = "e" * 16

# WanGP's own model types and names, as its defaults/ ship them (Wan2GP
# b8b18f8, 2026-10-01).
LTX_DISTILLED = {"type": "ltx2_22B_distilled", "label": "LTX-2 2.3 Distilled 1.0 22B", "family": "ltx2", "architecture": "ltx2_22B"}
LTX_DISTILLED_11 = {"type": "ltx2_22B_distilled_1_1", "label": "LTX-2 2.3 Distilled 1.1 22B", "family": "ltx2", "architecture": "ltx2_22B"}
LTX_GGUF = {"type": "ltx2_22B_distilled_gguf_q8_0", "label": "LTX-2 2.3 Distilled 1.0 GGUF Q8_0 Light 22B", "family": "ltx2", "architecture": "ltx2_22B"}
LTX_DEV = {"type": "ltx2_22B", "label": "LTX-2 2.3 Dev 1.0 22B", "family": "ltx2", "architecture": "ltx2_22B"}
LTX_25 = {"type": "ltx2_25_22B_distilled", "label": "LTX-2 2.5 Distilled 22B", "family": "ltx2", "architecture": "ltx2_25_22B"}
LTX_20 = {"type": "ltx2_distilled", "label": "LTX-2 2.0 Distilled 19B", "family": "ltx2", "architecture": "ltx2_19B"}
LTX_EDIT = {"type": "ltx2_22B_distilled_edit_anything", "label": "LTX-2 2.3 EditAnything Ref V2V Distilled 1.0 22B",
            "family": "ltx2", "architecture": "ltx2_22B_edit_anything"}
LTX_MSR = {"type": "ltx2_22B_msr", "label": "LTX-2 2.3 MSR Ref V1 Distilled 1.1 22B", "family": "ltx2", "architecture": "ltx2_22B_msr"}
FL2VA = {"type": "minimax_h3_fl2va", "label": "MiniMax H3 FL2VA 33B", "family": "minimax_h3", "architecture": "minimax_h3_fl2va"}
FL2VA_VDN = {"type": "minimax_h3_vdn", "label": "MiniMax H3 VDN 8-Step 33B", "family": "minimax_h3", "architecture": "minimax_h3_fl2va"}
REF2VA = {"type": "minimax_h3_ref2va_pruned", "label": "MiniMax H3 Ref2VA Pruned 20B", "family": "minimax_h3", "architecture": "minimax_h3_ref2va_pruned"}
TTS = {"type": "minimax_h3_tts_ref2va_pruned", "label": "TTS MiniMax H3 Voice Clone 20B", "family": "minimax_h3",
       "architecture": "minimax_h3_tts_ref2va_pruned"}
WAN = {"type": "t2v_2_2", "label": "Wan2.2 Text2Video 14B", "family": "wan", "architecture": "t2v_2_2"}


def _facts(model, *, defined=True, pipeline="", checked=True, missing=0, names=None):
    return {"ok": True, "model_type": model["type"], "defined": defined, "label": model["label"],
            "architecture": model["architecture"], "pipeline": pipeline, "checked": checked, "files": 9,
            "missing_count": missing, "missing": names or [], "diagnosis": "" if checked else "KeyError: 'x'",
            "code": "", "message": ""}


class FakeChild:
    """The control plane of a WanGP child, as far as readiness reads it."""

    def __init__(self):
        self.facts = {}
        self.calls = []
        self.model_status = 200
        self.hello_model = ""

    def __call__(self, operation, payload):
        self.calls.append((operation, dict(payload)))
        if operation == protocol.CONTROL_HELLO:
            return 200, {"ok": True, "control_version": protocol.CONTROL_VERSION, "model_type": self.hello_model,
                         "capabilities": {"hold": True, "model": True}}
        if operation == protocol.CONTROL_MODEL:
            if self.model_status != 200:
                return self.model_status, {"ok": False, "code": errors.REQUEST_INVALID, "message": "no such operation"}
            found = self.facts.get(payload.get("model_type"))
            if found is None:
                return 200, {"ok": True, "model_type": payload.get("model_type"), "defined": False, "checked": False}
            return 200, found
        return 404, {"ok": False, "code": errors.REQUEST_INVALID}

    def model_calls(self):
        return [payload for operation, payload in self.calls if operation == protocol.CONTROL_MODEL]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _real(child, running=True, clock=None):
    """The real decision, over a fake child and a fake runtime."""
    targets.use_readiness(None)
    targets.forget()
    outbox.use_running(lambda: running)
    control.use_transport(child)
    if clock is not None:
        targets._seams["clock"] = clock
    return child


def _refused(call) -> tuple:
    try:
        call()
    except IntegrationError as error:
        return error.code, error.extra
    return "", {}


# --------------------------------------------------------------- the models --


def classify_checks(r: Results) -> None:
    r.check("LTX 2.3 Distilled 1.0, 1.1 and a GGUF build of it are the LTX target, by their own names",
            all(targets.classify(model) == targets.TARGET_LTX23 for model in (LTX_DISTILLED, LTX_DISTILLED_11, LTX_GGUF)))
    r.check("LTX 2.3 Dev, 2.5, 2.0, EditAnything and MSR are not",
            all(targets.classify(model) == "" for model in (LTX_DEV, LTX_25, LTX_20, LTX_EDIT, LTX_MSR)),
            str([targets.classify(model) for model in (LTX_DEV, LTX_25, LTX_20, LTX_EDIT, LTX_MSR)]))
    r.check("the definition's own pipeline wins over the names: a 2.3 finetune that declares distilled is the target",
            targets.classify({"type": "my_ltx_tune", "label": "My tune", "architecture": "ltx2_22B"},
                             _facts({"type": "my_ltx_tune", "label": "My tune", "architecture": "ltx2_22B"}, pipeline="distilled")) == targets.TARGET_LTX23)
    r.check("and a checkpoint named distilled whose definition runs the two-stage pipeline is not",
            targets.classify(LTX_DISTILLED, _facts(LTX_DISTILLED, pipeline="two_stage")) == "")
    r.check("MiniMax H3 FL2VA and Ref2VA are read the enhancer's way, finetunes and pruned builds included",
            targets.classify(FL2VA) == "fl2va" and targets.classify(FL2VA_VDN) == "fl2va" and targets.classify(REF2VA) == "ref2va")
    r.check("a text-to-speech model that carries ref2va in its name is not a target",
            targets.classify(TTS) == "" and enhance.variant_for_model(TTS) == "ref2va")
    r.check("any other model, or none, is no target", targets.classify(WAN) == "" and targets.classify({}) == "" and targets.classify(None) == "")
    r.check("the enhancer reads the model through the same function",
            enhance.target_for_model(LTX_DISTILLED) == "ltx23" and enhance.target_for_model(FL2VA) == "fl2va" and enhance.target_for_model(WAN) == "")


def narrow_checks(r: Results) -> None:
    refs = [{"kind": "staged", "id": "3" * 32}]
    start = {"kind": "staged", "id": "1" * 32}
    request = {"prompt": "p", "images": {"start": start, "references": refs}}
    removed = targets.narrow(request, targets.TARGET_LTX23)
    r.check("LTX 2.3 Distilled is sent no reference: it is taken out of the request and named",
            removed == ["references"] and request["images"] == {"start": start}, str(request))
    request = {"prompt": "p", "images": {"references": refs}}
    targets.narrow(request, targets.TARGET_LTX23)
    r.check("and a request left with no image at all inherits them all", "images" not in request, str(request))
    request = {"prompt": "p", "images": {"start": start, "references": refs}}
    r.check("MiniMax keeps every field, as it always has (WanGP ignores what the model does not read)",
            targets.narrow(request, targets.TARGET_FL2VA) == [] and "references" in request["images"])
    r.check("the fields each target is sent",
            targets.fields_for("ltx23") == ("start", "end") and targets.fields_for("fl2va") == ("start", "end", "references")
            and targets.fields_for("") == ("start", "end", "references"))


# ---------------------------------------------------------------- readiness --


def readiness_checks(r: Results) -> None:
    child = _real(FakeChild(), running=False)
    view = targets.readiness(LTX_DISTILLED)
    r.check("WanGP stopped: blocked, and the bridge is not even asked",
            view["ready"] is False and view["code"] == errors.WANGP_NOT_RUNNING and child.calls == [], json.dumps(view)[:300])
    r.check("with the integration's own sentence", view["message"] == errors.message(errors.WANGP_NOT_RUNNING))

    child = _real(FakeChild())
    view = targets.readiness(None)
    r.check("running, with no model from the page and none from WanGP: TARGET_UNKNOWN",
            view["ready"] is False and view["code"] == errors.TARGET_UNKNOWN, view["code"])

    child = _real(FakeChild())
    child.facts[WAN["type"]] = _facts(WAN)
    view = targets.readiness(WAN)
    r.check("on another model: TARGET_UNSUPPORTED, naming the model and the three it could be",
            view["code"] == errors.TARGET_UNSUPPORTED and "Wan2.2 Text2Video 14B" in view["message"]
            and "LTX 2.3 Distilled" in view["message"] and view["target"] == "", view["message"])

    child = _real(FakeChild())
    child.facts[LTX_DEV["type"]] = _facts(LTX_DEV, pipeline="two_stage")
    r.check("LTX 2.3 Dev is another model, from its definition", targets.readiness(LTX_DEV)["code"] == errors.TARGET_UNSUPPORTED)

    child = _real(FakeChild())
    view = targets.readiness(LTX_DISTILLED)
    r.check("a model WanGP does not define: TARGET_NOT_DEFINED",
            view["code"] == errors.TARGET_NOT_DEFINED and view["target"] == "ltx23", json.dumps(view)[:300])

    child = _real(FakeChild())
    child.facts[LTX_DISTILLED["type"]] = _facts(LTX_DISTILLED, pipeline="distilled", missing=3, names=["ltx-2.3-22b-distilled_diffusion_model_quanto_int8.safetensors"])
    view = targets.readiness(LTX_DISTILLED)
    r.check("files WanGP would download first are missing: TARGET_NOT_DOWNLOADED, with how many",
            view["code"] == errors.TARGET_NOT_DOWNLOADED and "3 files missing" in view["message"]
            and "LTX-2 2.3 Distilled 1.0 22B" in view["message"] and view["missing_count"] == 3, view["message"])

    child = _real(FakeChild())
    child.facts[LTX_DISTILLED["type"]] = _facts(LTX_DISTILLED, pipeline="distilled")
    view = targets.readiness(LTX_DISTILLED)
    r.check("ready: the target, its label, and the fields it is sent - a first and a last frame",
            view["ready"] is True and view["code"] == "" and view["target"] == "ltx23" and view["target_label"] == "LTX 2.3 Distilled"
            and view["fields"] == ["start", "end"] and view["checked"] is True, json.dumps(view)[:300])
    r.check("and the bridge was asked about exactly the page's model", child.model_calls() == [{"model_type": "ltx2_22B_distilled"}])

    child = _real(FakeChild())
    child.facts[FL2VA["type"]] = _facts(FL2VA, checked=False)
    view = targets.readiness(FL2VA)
    r.check("a check the bridge could not finish is reported, never read as a missing file: still ready, and it says so",
            view["ready"] is True and view["checked"] is False and "could not be checked" in view["message"], view["message"])

    child = _real(FakeChild())
    child.model_status = 404
    view = targets.readiness(FL2VA)
    r.check("a bridge without the model check (older than 1.13.0): TARGET_CHECK_UNAVAILABLE, blocked",
            view["ready"] is False and view["code"] == errors.TARGET_CHECK_UNAVAILABLE, view["code"])

    def silent(operation, payload):
        raise IntegrationError(errors.CONTROL_UNAVAILABLE, "the control surface did not answer: timeout")

    _real(silent)
    r.check("a bridge that does not answer: the same, blocked", targets.readiness(FL2VA)["code"] == errors.TARGET_CHECK_UNAVAILABLE)

    def stopped(operation, payload):
        raise IntegrationError(errors.WANGP_NOT_RUNNING, "the managed WanGP is stopping")

    _real(stopped)
    r.check("WanGP going away between the two questions reads as not running",
            targets.readiness(FL2VA)["code"] == errors.WANGP_NOT_RUNNING)

    child = _real(FakeChild())
    child.hello_model = "minimax_h3_ref2va_pruned"
    child.facts[REF2VA["type"]] = _facts(REF2VA)
    control.hello()
    view = targets.readiness(None)
    r.check("with no model from the page, the one WanGP last said it was on, its label from the definition",
            view["ready"] is True and view["target"] == "ref2va" and view["model"]["type"] == "minimax_h3_ref2va_pruned"
            and view["model"]["label"] == "MiniMax H3 Ref2VA Pruned 20B", json.dumps(view)[:300])

    clock = Clock()
    child = _real(FakeChild(), clock=clock)
    child.facts[FL2VA["type"]] = _facts(FL2VA)
    targets.readiness(FL2VA)
    targets.readiness(FL2VA)
    r.check("a screen's second read inside the check's lifetime is answered from the cache", len(child.model_calls()) == 1)
    targets.readiness(FL2VA, fresh=True)
    r.check("fresh - Check again, and every press - asks again", len(child.model_calls()) == 2)
    clock.now += targets.CHECK_TTL + 1
    targets.readiness(FL2VA)
    r.check("and so does a read after the check has gone stale", len(child.model_calls()) == 3)
    child.model_status = 404
    targets.readiness(FL2VA, fresh=True)
    targets.readiness(FL2VA)
    r.check("a failure is kept for a few seconds, so a screen asking twice does not wait out two timeouts",
            len(child.model_calls()) == 4)
    clock.now += targets.FAILURE_TTL + 1
    targets.readiness(FL2VA)
    r.check("and no longer", len(child.model_calls()) == 5)
    targets._seams["clock"] = __import__("time").monotonic

    child = _real(FakeChild())
    child.facts[WAN["type"]] = _facts(WAN)
    code, extra = _refused(lambda: targets.require_ready(WAN))
    r.check("require_ready raises with the code and carries the whole view, for the screen that pressed",
            code == errors.TARGET_UNSUPPORTED and extra.get("readiness", {}).get("code") == code
            and "Wan2.2" in extra["readiness"]["message"], str(extra)[:200])
    r.check("every code has a sentence of its own",
            all(errors.MESSAGES.get(code) for code in (errors.TARGET_UNKNOWN, errors.TARGET_UNSUPPORTED, errors.TARGET_NOT_DEFINED,
                                                       errors.TARGET_NOT_DOWNLOADED, errors.TARGET_CHECK_UNAVAILABLE,
                                                       errors.ENHANCE_LTX_UNSUPPORTED)))


# ------------------------------------------------------------ the press gate --


def gate_checks(r: Results) -> None:
    outbox.use_executor(outbox.EXECUTOR_BROWSER)
    child = _real(FakeChild())
    child.facts[WAN["type"]] = _facts(WAN)
    before = len(outbox.jobs())
    code, extra = _refused(lambda: outbox.submit({"prompt": "a dog"}, PAGE, outbox.ORIGIN_CLIPBOARD, model=WAN))
    r.check("a Clipboard press for a model it does not send to is refused, and nothing is stored",
            code == errors.TARGET_UNSUPPORTED and len(outbox.jobs()) == before and "readiness" in extra)
    code, _extra = _refused(lambda: outbox.submit({"prompt": "a dog"}, PAGE, outbox.ORIGIN_GALLERY, model=WAN))
    r.check("and so is the gallery popup's", code == errors.TARGET_UNSUPPORTED)
    job = outbox.submit({"prompt": "a dog"}, PAGE, outbox.ORIGIN_API, model=WAN)
    r.check("a public-API caller is not gated: it chooses its own model", job["state"] == outbox.PENDING)
    asked = len(child.model_calls())
    child.facts[LTX_DISTILLED["type"]] = _facts(LTX_DISTILLED, pipeline="distilled")
    targets.readiness(LTX_DISTILLED)  # a screen's read, cached
    job = outbox.submit({"prompt": "she waves", "images": {"start": {"kind": "staged", "id": "1" * 32},
                                                            "references": [{"kind": "staged", "id": "3" * 32}]}},
                        PAGE, outbox.ORIGIN_CLIPBOARD, model=LTX_DISTILLED)
    r.check("the press checks afresh, whatever a screen read a moment ago", len(child.model_calls()) == asked + 2)
    stored = outbox.get(job["job_id"])
    r.check("an LTX 2.3 press is stored without its reference, and the answer says it was not sent",
            job.get("narrowed") == ["references"] and "references" not in (stored["request"].get("images") or {})
            and "start" in stored["request"]["images"], json.dumps(stored["request"])[:300])
    child.facts[LTX_DISTILLED["type"]] = _facts(LTX_DISTILLED, pipeline="distilled", missing=1)
    code, _extra = _refused(lambda: outbox.submit({"prompt": "p"}, PAGE, outbox.ORIGIN_CLIPBOARD, model=LTX_DISTILLED))
    r.check("a model downloaded a moment ago and deleted since is refused at the press", code == errors.TARGET_NOT_DOWNLOADED)
    outbox.use_executor(outbox.EXECUTOR_SERVER)
    child = _real(FakeChild(), running=False)
    code, _extra = _refused(lambda: outbox.submit({"prompt": "p"}, PAGE, outbox.ORIGIN_CLIPBOARD, model=FL2VA))
    r.check("under the unattended queue too, a Clipboard press while WanGP is stopped is refused, not admitted cold",
            code == errors.WANGP_NOT_RUNNING)
    outbox.use_executor(outbox.EXECUTOR_BROWSER)
    targets.use_readiness(targets.always_ready)


# --------------------------------------------------------- the LTX 2.3 writer --


class LtxApi(FakeApi):
    """``mc_llm_api`` with the LTX 2.3 writer: the kind, ``submit_ltx``, its prompts."""

    def __init__(self):
        super().__init__()
        self.ltx_submissions = []

    def capabilities(self):
        found = super().capabilities()
        found["kinds"] = ["minimax", "ltx"]
        return found

    def submit_ltx(self, prompt, *, first_frame=None, system_prompt=None, seed=None, origin="", remember=True):
        if not str(prompt or "").strip():
            raise FakeRejected("A prompt is required", "empty_prompt")
        if first_frame is not None and not self.vision:
            raise FakeRejected("The model running has no vision projector", "no_vision")
        self.counter += 1
        identifier = f"{self.counter:016x}"
        self.jobs[identifier] = {"id": identifier, "kind": "ltx", "state": "queued", "origin": origin, "variant": "ltx23",
                                 "seed": 7, "images": ["first_frame"] if first_frame is not None else [],
                                 "image_used": "first_frame" if first_frame is not None else "", "image_ignored": [],
                                 "prompt": "", "caption": "", "request": str(prompt), "stage": "", "elapsed": 0.0,
                                 "queued_for": 0.0, "cancelling": False, "system_override": system_prompt is not None}
        self.order.append(identifier)
        self.ltx_submissions.append({"id": identifier, "prompt": prompt, "first_frame": first_frame,
                                     "system_prompt": system_prompt, "origin": origin, "remember": remember})
        return identifier

    def system_prompt(self, variant="", *, has_image=False):
        if variant == "ltx23":
            return "LTX " + ("image" if has_image else "text") + " instructions"
        return super().system_prompt(variant, has_image=has_image)


def _asset(library, name, colour):
    return library.import_image(Image.new("RGB", (32, 24), colour), name, "upload").asset_id


def writer_checks(r: Results, library) -> None:
    api = LtxApi()
    enhance.use_api(api)
    first, last, ref = (_asset(library, f"{name}.png", colour) for name, colour in
                        (("first", (200, 20, 20)), ("last", (20, 200, 20)), ("ref", (20, 20, 200))))
    handles = {name: {"kind": "clipboard_asset", "id": asset} for name, asset in (("start", first), ("end", last), ("ref", ref))}
    request = {"prompt": "she turns and waves", "images": {"start": handles["start"], "end": handles["end"], "references": [handles["ref"]]}}
    planned = enhance.plan(request, LTX_DISTILLED)
    r.check("LTX 2.3 reads the first frame and nothing else; the last frame and the reference are left out, said",
            planned["variant"] == "ltx23" and planned["slots"] == {"first_frame": handles["start"]}
            and planned["dropped"] == ["end", "references"], json.dumps(planned)[:300])
    only_last = enhance.plan({"prompt": "p", "images": {"end": handles["end"]}}, LTX_DISTILLED)
    r.check("a last frame alone is a text request to the writer: it was asked to look at the first frame only",
            only_last["slots"] == {} and only_last["has_image"] is False and only_last["dropped"] == ["end"])
    r.check("the press's own reading of the target wins over the names",
            enhance.plan({"prompt": "p"}, {"type": "x", "architecture": "ltx2_22B"}, target="ltx23")["variant"] == "ltx23")

    enhance.set_override("ltx23", "image", "Write it plainly.")
    asked = enhance.submit("she turns and waves", planned)
    sent = api.ltx_submissions[-1]
    r.check("it goes to submit_ltx with the first frame as a picture and no variant",
            asked["variant"] == "ltx23" and sent["first_frame"] is not None and hasattr(sent["first_frame"], "size")
            and sent["origin"] == enhance.ORIGIN and sent["remember"] is True and not api.submissions, str(sent)[:200])
    r.check("under the override saved for LTX 2.3 with a picture", sent["system_prompt"] == "Write it plainly." and asked["system_override"] is True)
    enhance.clear_override("ltx23", "image")
    enhance.submit("a dog runs", enhance.plan({"prompt": "a dog runs"}, LTX_DISTILLED))
    r.check("a text request goes with no picture and no override", api.ltx_submissions[-1]["first_frame"] is None
            and api.ltx_submissions[-1]["system_prompt"] is None)
    r.check("its defaults are read from the API by the LTX name",
            enhance.default_prompt("ltx23", "image") == "LTX image instructions" and enhance.effective_prompt("ltx23", "text") == ("LTX text instructions", "default"))
    r.check("capabilities say the writer is there and offer it as a variant",
            enhance.capabilities()["ltx"] is True and enhance.capabilities()["variants"] == ["fl2va", "ref2va", "ltx23"])
    line = enhance.availability(LTX_DISTILLED)
    r.check("the enhancement line names LTX 2.3 for an LTX page", line["state"] == "ready" and line["variant"] == "ltx23"
            and "LTX 2.3" in line["text"], line["text"])
    r.check("and a page on another model is told which three the writers write for",
            "LTX 2.3 Distilled" in enhance.availability(WAN)["text"] and enhance.availability(WAN)["state"] == "model")

    older = FakeApi()
    enhance.use_api(older)
    r.check("a ModelSwitchRefiner without the writer: no LTX variant, and its preflight says to update it",
            enhance.capabilities()["ltx"] is False and "ltx23" not in enhance.capabilities()["variants"]
            and enhance.preflight(planned)[0] == errors.ENHANCE_LTX_UNSUPPORTED)
    code, _extra = _refused(lambda: enhance.submit("p", planned))
    r.check("and a request is refused rather than sent to MiniMax under LTX's name",
            code == errors.ENHANCE_LTX_UNSUPPORTED and not older.submissions)
    r.check("nor are MiniMax's instructions handed back as LTX's default", enhance.default_prompt("ltx23", "text") == "")
    r.check("its line says to update it", enhance.availability(LTX_DISTILLED)["state"] == "blocked")
    enhance.use_api(None)


# ------------------------------------------------------------ the popup half --


def popup_checks(r: Results) -> None:
    caps = intercept.capabilities(LTX_DISTILLED, None)
    r.check("LTX 2.3 offers a First and a Last frame and no Reference, even before the page has said what it takes",
            [role["id"] for role in caps["roles"]] == ["first_frame", "last_frame"] and caps["default_roles"] == ["first_frame"],
            str(caps["roles"]))
    every = {"start": {"supported": True}, "end": {"supported": True}, "references": {"supported": True}}
    r.check("and a live page claiming a reference input changes nothing",
            [role["id"] for role in intercept.capabilities(LTX_DISTILLED, every)["roles"]] == ["first_frame", "last_frame"])
    r.check("MiniMax keeps all three", len(intercept.capabilities(FL2VA, None)["roles"]) == 3)

    token = interop.stage_image(Image.new("RGB", (40, 30), (9, 9, 9)))["image"]["id"]
    handoff = intercept.handoff_text(token, 40, 30, "txt2img")
    blocked = targets._blocked(errors.TARGET_UNSUPPORTED, enhance.model_block(WAN), message="WanGP is on Wan2.2 Text2Video 14B.")
    targets.use_readiness(lambda model=None, fresh=False: blocked)
    told = intercept.describe(handoff, WAN, None)
    r.check("describe carries the readiness answer, and a blocked one leaves Generate no button, with its reason",
            told["readiness"]["ready"] is False and told["generate"]["enabled"] is False
            and told["generate"]["reason"] == "WanGP is on Wan2.2 Text2Video 14B.", json.dumps(told["generate"]))
    answer = intercept.submit(handoff, "a dog", ["first_frame"], False, False, PAGE, WAN, None)
    r.check("Generate pressed anyway is refused with the readiness answer for the popup to draw",
            answer["ok"] is False and answer["code"] == errors.TARGET_UNSUPPORTED and answer["readiness"]["ready"] is False
            and answer["message"] == "WanGP is on Wan2.2 Text2Video 14B.", json.dumps(answer)[:300])
    seen = {}
    targets.use_readiness(lambda model=None, fresh=False: seen.setdefault("fresh", fresh) and targets.always_ready(model))
    intercept.describe(handoff, FL2VA, None, fresh=True)
    r.check("Check again's describe asks for a fresh check", seen.get("fresh") is True)
    targets.use_readiness(targets.always_ready)


# ------------------------------------------------ the bridge's model check --


def _bridge():
    folder = str(BRIDGE_DIR)
    if folder not in sys.path:
        sys.path.insert(0, folder)
    for name in ("protocol", "model_check"):
        sys.modules.pop(name, None)
    return importlib.import_module("model_check")


class FakeLocator:
    """WanGP's files_locator, over a set of names that are on disk."""

    def __init__(self, present):
        self.present = set(present)
        self.asked = []

    def get_local_model_filename(self, name, use_locator=True, extra_paths=None, lora_dir=None):
        base = str(name).split("|", 1)[0].rsplit("/", 1)[-1]
        self.asked.append((base, extra_paths, lora_dir))
        return "/ckpts/" + base if base in self.present else None


class FakeDownloads:
    def __init__(self, present):
        self.present = set(present)

    def download_def_missing_files(self, one):
        missing = []
        for folder, files in zip(one.get("sourceFolderList", []), one.get("fileList", [])):
            for name in files:
                if name not in self.present:
                    missing.append(f"{folder}/{name}" if folder else name)
        return missing


def _wgp(lora_present=True, raise_in=None):
    """Enough of wgp.py for the dry run: LTX 2.3 Distilled's definition and handler."""
    module = types.SimpleNamespace()
    definition = {"name": "LTX-2 2.3 Distilled 1.0 22B", "architecture": "ltx2_22B", "ltx2_pipeline": "distilled",
                  "URLs": ["https://hf/ltx-2.3-22b-distilled_diffusion_model.safetensors",
                           "https://hf/ltx-2.3-22b-distilled_diffusion_model_quanto_int8.safetensors"],
                  "preload_URLs": ["https://hf/loras/ltx2_ic_lora.safetensors|%lora_dir"],
                  "loras": ["https://hf/loras/accel.safetensors"],
                  "text_encoder_URLs": ["https://hf/gemma3/gemma_bf16.safetensors", "https://hf/gemma3/gemma_quanto_int8.safetensors"],
                  "text_encoder_folder": "gemma3"}
    module.transformer_quantization = "int8"
    module.transformer_dtype_policy = ""
    module.text_encoder_quantization = "int8"
    module.server_config = {}
    module.get_model_def = lambda model_type: dict(definition) if model_type == "ltx2_22B_distilled" else None
    module.get_base_model_type = lambda model_type: "ltx2_22B"

    def get_model_filename(model_type=None, quantization="", dtype_policy="", module_type=None, submodel_no=1, URLs=None, model_def=None, *args):
        if raise_in == "filename":
            raise KeyError("URLs")
        urls = URLs if URLs is not None else (model_def or definition)["URLs"]
        quant = [url for url in urls if quantization and quantization in url]
        return (quant or urls)[0]

    module.get_model_filename = get_model_filename

    def recursive(model_type, prop="URLs", sub_prop_name=None, return_list=True, model_def=None, stack=None):
        value = (model_def or definition).get(prop)
        return [] if value is None else value

    module.get_model_recursive_prop = recursive
    module.get_lora_dir = lambda model_type: "/loras/ltx2"
    module.get_lora_local_path = lambda lora_dir, url: lora_dir + "/" + url.rsplit("/", 1)[-1]

    class Handler:
        @staticmethod
        def query_model_files(compute_list, base, model_def):
            return [{"repoId": "DeepBeepMeep/LTX-2", "sourceFolderList": [""], "fileList": [["spatial_upscaler.safetensors", "audio_vae.safetensors"]]},
                    {"repoId": "DeepBeepMeep/LTX-2", "sourceFolderList": ["gemma3"], "fileList": [["tokenizer.json"]]}]

    module.model_types_handlers = {"ltx2_22B": Handler}
    return module


def bridge_checks(r: Results) -> None:
    check = _bridge()
    everything = ["ltx-2.3-22b-distilled_diffusion_model_quanto_int8.safetensors", "ltx2_ic_lora.safetensors",
                  "gemma_quanto_int8.safetensors", "spatial_upscaler.safetensors", "audio_vae.safetensors", "tokenizer.json"]
    import os.path as os_path

    original_isfile = os_path.isfile
    os_path.isfile = lambda path: str(path) == "/loras/ltx2/accel.safetensors" or original_isfile(path)
    try:
        locator = FakeLocator(everything)
        found = check.facts(_wgp(), "ltx2_22B_distilled", locator=locator, downloads=FakeDownloads(everything))
        r.check("a model with every file on disk: defined, checked, nothing missing, its architecture and pipeline read",
                found["ok"] and found["defined"] and found["checked"] and found["missing_count"] == 0
                and found["architecture"] == "ltx2_22B" and found["pipeline"] == "distilled" and found["files"] == 7, json.dumps(found))
        r.check("the transformer asked for is the one WanGP's quantization setting chooses",
                any(name == "ltx-2.3-22b-distilled_diffusion_model_quanto_int8.safetensors" for name, _p, _l in locator.asked))
        r.check("and the text encoder is looked for in its own folder",
                ("gemma_quanto_int8.safetensors", "gemma3", None) in locator.asked, str(locator.asked))
        r.check("preload files are looked for with the LoRA folder WanGP would use",
                ("ltx2_ic_lora.safetensors", None, "/loras/ltx2") in locator.asked)

        some = [name for name in everything if name not in ("ltx-2.3-22b-distilled_diffusion_model_quanto_int8.safetensors", "tokenizer.json")]
        found = check.facts(_wgp(), "ltx2_22B_distilled", locator=FakeLocator(some), downloads=FakeDownloads(some))
        r.check("missing files are counted and named by base name, never by path",
                found["checked"] and found["missing_count"] == 2
                and found["missing"] == ["ltx-2.3-22b-distilled_diffusion_model_quanto_int8.safetensors", "tokenizer.json"], json.dumps(found))
        os_path.isfile = original_isfile
        found = check.facts(_wgp(), "ltx2_22B_distilled", locator=FakeLocator(everything), downloads=FakeDownloads(everything))
        r.check("a LoRA the definition carries that is not in WanGP's LoRA folder is missing too",
                found["missing"] == ["accel.safetensors"], json.dumps(found))
    finally:
        os_path.isfile = original_isfile

    found = check.facts(_wgp(), "not_a_model", locator=FakeLocator([]), downloads=FakeDownloads([]))
    r.check("a model WanGP does not define: defined False, nothing checked", found["ok"] and not found["defined"] and not found["checked"])
    found = check.facts(_wgp(raise_in="filename"), "ltx2_22B_distilled", locator=FakeLocator([]), downloads=FakeDownloads([]))
    r.check("a step of WanGP's own that raises: checked False, with what failed - could not tell, never missing",
            found["ok"] and found["defined"] and not found["checked"] and found["missing_count"] == 0 and "KeyError" in found["diagnosis"],
            json.dumps(found))
    found = check.facts(None, "ltx2_22B_distilled")
    r.check("no WanGP module at all: not an answer", found["ok"] is False and found["code"] == protocol.CONTROL_UNAVAILABLE)

    facts = protocol.normalize_model_facts({"ok": True, "defined": True, "checked": False, "missing_count": 4, "missing": ["a"]})
    r.check("Forge reads an unchecked answer as nothing missing and nothing named", facts["missing_count"] == 0 and facts["missing"] == [])
    facts = protocol.normalize_model_facts({"ok": True, "defined": True, "checked": True, "missing_count": 1,
                                            "missing": ["/home/me/ckpts/x.safetensors", "y.safetensors", "..\\z"]})
    r.check("and drops a name that is a path", facts["missing"] == ["y.safetensors"] and facts["missing_count"] == 1, str(facts))
    request, code = protocol.normalize_model_request({"model_type": "../etc"})
    r.check("the request takes a model type and nothing else", code == protocol.QUEUE_CODE_REQUEST_INVALID
            and protocol.normalize_model_request({"model_type": "ltx2_22B_distilled"})[0] == {"model_type": "ltx2_22B_distilled"})
    hello = protocol.normalize_control_hello({"ok": True, "capabilities": {"hold": True, "model": True}})
    r.check("a 1.13.0 hello says it answers the model check; an older one does not",
            hello["capabilities"]["model"] is True and protocol.normalize_control_hello({"ok": True})["capabilities"]["model"] is False
            and control.can_check_models(hello) is True)

    sys.path.insert(0, str(BRIDGE_DIR))
    for name in ("compatibility", "control", "model_check", "protocol", "compose", "execution", "hold", "ledger"):
        sys.modules.pop(name, None)
    bridge_control = importlib.import_module("control")
    compat = types.SimpleNamespace(_wgp=lambda: None)
    surface = bridge_control.ControlSurface.__new__(bridge_control.ControlSurface)
    surface.compat = compat
    surface._note = lambda text: None
    status, body = surface.dispatch(protocol.CONTROL_MODEL, {"model_type": "ltx2_22B_distilled"})
    r.check("the control surface answers the model operation, through the same check",
            status == 200 and body["ok"] is False and body["code"] == protocol.CONTROL_UNAVAILABLE, str(body))
    status, body = surface.dispatch(protocol.CONTROL_MODEL, {"model_type": "no spaces allowed"})
    r.check("and refuses a model type that does not normalise", status == 400)
    info = json.loads((BRIDGE_DIR / "plugin_info.json").read_text(encoding="utf-8"))
    r.check("the bridge is 1.13.0 and lists the model check", info["version"] == "1.13.0" and "model_check" in info["capabilities"])


# ------------------------------------------------------------- the screens --


def screen_checks(r: Results) -> None:
    from minipaint_neo.clipboard import ui as clipboard_ui

    html = clipboard_ui.BLOCKED_HTML
    r.check("the composer's quiet line is a status with Check again, and draws no x the theme could swap for an icon",
            'role="status"' in html and "Check again" in html and "×" not in html and "minipaint-clip-blocked-text" in html)
    css = (ROOT / "style.css").read_text(encoding="utf-8")
    r.check("the stylesheet hides what sends until the root says ready",
            '#minipaint_clipboard_root:not([data-wangp-ready="1"]) :is(' in css
            and all(name in css for name in ("#minipaint_clipboard_cards", "#minipaint_clipboard_prompt", "#minipaint_clipboard_sp_open",
                                              "#minipaint_clipboard_queue,", "#minipaint_clipboard_to_first")))
    r.check("hides no reference card for LTX 2.3", '#minipaint_clipboard_root[data-wangp-target="ltx23"] :is(#minipaint_clipboard_card_ref' in css)
    r.check("and the popup's body while it is blocked",
            ".minipaint-intercept.minipaint-intercept-is-blocked :is(" in css and ".minipaint-intercept-generate)" in css)
    script = (ROOT / "browser" / "minipaint_clipboard.js").read_text(encoding="utf-8")
    r.check("the composer asks the readiness route and writes both attributes",
            '"/minipaint-clipboard/readiness"' in script and "dataset.wangpReady" in script and "dataset.wangpTarget" in script)
    popup = (ROOT / "browser" / "minipaint_intercept.js").read_text(encoding="utf-8")
    r.check("the popup still names no model: it renders the readiness answer it is given",
            "ltx" not in popup.lower() and "minimax" not in popup.lower().replace("minimax h3", "") and "-is-blocked" in popup)


def route_checks(r: Results) -> None:
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from minipaint_neo.clipboard import routes

    app = Starlette()
    routes.install(app)
    client = TestClient(app)
    child = _real(FakeChild())
    child.facts[LTX_DISTILLED["type"]] = _facts(LTX_DISTILLED, pipeline="distilled")
    answer = client.post(routes.READINESS_ROUTE, json={"model": LTX_DISTILLED}).json()
    r.check("the readiness route answers with the view", answer["ok"] is True and answer["readiness"]["target"] == "ltx23", json.dumps(answer)[:200])
    asked = len(child.model_calls())
    client.post(routes.READINESS_ROUTE, json={"model": LTX_DISTILLED})
    client.post(routes.READINESS_ROUTE, json={"model": LTX_DISTILLED, "fresh": True})
    r.check("from the cache, unless fresh", len(child.model_calls()) == asked + 1)
    child.facts[WAN["type"]] = _facts(WAN)
    answer = client.post(routes.READINESS_ROUTE, json={"model": WAN}).json()
    r.check("a blocked answer is an answer, not an error", answer["ok"] is True and answer["readiness"]["ready"] is False
            and answer["readiness"]["code"] == errors.TARGET_UNSUPPORTED)
    targets.use_readiness(targets.always_ready)


def run() -> Results:
    r = Results("clipboard targets")
    with tempfile.TemporaryDirectory(prefix="minipaint-targets-") as scratch:
        base = pathlib.Path(scratch)
        wangp_config.use_config_dir(base / "data")
        config.use_config_dir(base / "data")
        process_log.use_log_dir(base / "logs")
        store.reset_for_tests()
        outbox.reset_for_tests()
        executor.reset_for_tests()
        executor.use_thread(False)
        enhance.reset_for_tests()
        control.reset_for_tests()
        folder = base / "library"
        folder.mkdir()
        try:
            library = store.store()
            library.set_root(str(folder), create=True)
            classify_checks(r)
            narrow_checks(r)
            readiness_checks(r)
            gate_checks(r)
            writer_checks(r, library)
            popup_checks(r)
            bridge_checks(r)
            screen_checks(r)
            route_checks(r)
        finally:
            outbox.reset_for_tests()
            executor.reset_for_tests()
            enhance.reset_for_tests()
            control.reset_for_tests()
            targets.reset_for_tests()
            wangp_config.use_config_dir(None)
            config.use_config_dir(None)
            process_log.use_log_dir(None)
            store.reset_for_tests()
    return r


if __name__ == "__main__":
    sys.exit(0 if run().report() else 1)
