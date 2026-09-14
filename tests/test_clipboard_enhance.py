"""Enhanced prompts: ModelSwitchRefiner's MiniMax H3 writer in front of WanGP.

The other extension's external LLM API (``mc_llm_api``) is a module in the
same process, so it is stood in for here by a small fake that keeps the
document's shapes - ``submit_minimax`` and its refusal codes, ``status``
with the states and fields the contract lists, ``cancel``, ``cancel_all``
by origin, ``capabilities``, the four system prompts - and lets a test move
a request from queued to running to done, fail it, cancel it from the LLM
side, or forget it, exactly as the real one would over minutes.

What is checked is the rule set a reader should be able to rely on: the
loader finds the API off the extension's folder without a configured path;
the switch is off until turned on and is read from disk; the H3 variant is
the WanGP model's and a page on any other model is refused before anything
is stored; the typed prompt is required; pictures follow the variant and
the ones it does not read are left out and said so; an override replaces
one of the four instruction sets, survives a restart, and Restore forgets
it; a press with enhancement on asks the LLM at once and waits in press
order, holding the line behind it whichever page is asking; a finished
prompt makes the job pending carrying it, with the typed one kept; a run
that failed, was cancelled in LLM Studio, or was forgotten ends the job
honestly and never retries by itself; Cancel everything cancels our
requests by origin and every pending job; a retry re-enhances from the
typed prompt, or carries an already written prompt over; a queued job's
place in WanGP is tracked only by the page that queued it; and nothing
written down carries a prompt.
"""

from harness import Results, setup_path

setup_path()

import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import pathlib  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import types  # noqa: E402

from PIL import Image  # noqa: E402

from minipaint_neo import interop  # noqa: E402
from minipaint_neo.clipboard import config, enhance, history, outbox  # noqa: E402
from minipaint_neo.clipboard import store as clipboard_store  # noqa: E402
from minipaint_neo.wangp import config as wangp_config  # noqa: E402
from minipaint_neo.wangp import errors, process_log, protocol  # noqa: E402
from minipaint_neo.wangp.errors import IntegrationError  # noqa: E402

PAGE_A = "a" * 16
PAGE_B = "b" * 16
FL2VA_MODEL = {"type": "minimax_h3_fl2va", "label": "MiniMax H3 FL2VA", "family": "minimax_h3", "architecture": "minimax_h3_fl2va"}
REF2VA_MODEL = {"type": "minimax_h3_ref2va", "label": "MiniMax H3 Ref2VA", "family": "minimax_h3", "architecture": "minimax_h3_ref2va"}
FINETUNE_MODEL = {"type": "my_h3_finetune", "label": "My tune", "family": "", "architecture": "minimax_h3_fl2va"}
VIDEO_MODEL = {"type": "t2v", "label": "A video model", "family": "wan"}


class _Clock:
    def __init__(self):
        self.now = 1_700_000_000.0

    def __call__(self):
        return self.now


class FakeRejected(Exception):
    def __init__(self, reason, code="rejected"):
        super().__init__(reason)
        self.reason, self.code = reason, code


class FakeApi:
    """``mc_llm_api`` as its document describes it, driven by the test."""

    API_VERSION = 1
    Rejected = FakeRejected
    PREFERENCE = {"fl2va": ("first_frame", "last_frame", "reference"), "ref2va": ("reference", "first_frame", "last_frame")}

    def __init__(self):
        self.jobs = {}
        self.order = []
        self.enabled = True
        self.configured = True
        self.vision = True
        self.model = "/models/qwen3-vl-8b.gguf"
        self.submissions = []
        self.cancelled = []
        self.cancel_all_calls = []
        self.counter = 0
        self.max_queued = 32

    # -- the contract
    def capabilities(self):
        reason = ""
        if not self.enabled:
            reason = "LLM Studio is switched off in this WebUI's settings. Requests will be refused until it is turned on."
        elif not self.configured:
            reason = "No language model is set up. Choose one in LLM Studio → Setup."
        elif not self.vision:
            reason = "The model running has no vision projector, so requests carrying a picture will fail. Text-only requests are fine."
        return {"api_version": self.API_VERSION, "kinds": ["minimax"], "variants": ["fl2va", "ref2va"], "slots": ["first_frame", "last_frame", "reference"],
                "events": [], "max_queued": self.max_queued, "feed_ttl": 300.0, "job_retention": 900.0, "enabled": self.enabled,
                "configured": self.configured, "vision": self.vision, "model": pathlib.Path(self.model).name, "reason": reason}

    def submit_minimax(self, prompt, *, variant="", first_frame=None, last_frame=None, reference=None, system_prompt=None, seed=None, origin="", remember=True):
        if not self.enabled:
            raise FakeRejected("LLM Studio is switched off", "disabled")
        if not str(prompt or "").strip():
            raise FakeRejected("A prompt is required", "empty_prompt")
        chosen = variant if variant in ("fl2va", "ref2va") else "fl2va"
        supplied = {"first_frame": first_frame, "last_frame": last_frame, "reference": reference}
        filled = [slot for slot in ("first_frame", "last_frame", "reference") if supplied[slot] is not None]
        used = next((slot for slot in self.PREFERENCE[chosen] if slot in filled), "")
        if used and not self.vision:
            raise FakeRejected("The model running has no vision projector", "no_vision")
        if system_prompt is not None and not str(system_prompt).strip():
            raise FakeRejected("A system prompt override cannot be blank", "empty_system_prompt")
        if sum(1 for job in self.jobs.values() if job["state"] in ("queued", "running")) >= self.max_queued:
            raise FakeRejected("32 requests are already waiting", "queue_full")
        self.counter += 1
        identifier = f"{self.counter:016x}"
        job = {"id": identifier, "kind": "minimax", "state": "queued", "origin": origin, "variant": chosen, "seed": 7, "created": 1.0, "started": None,
               "finished": None, "elapsed": 0.0, "queued_for": 0.0, "cancelling": False, "stage": "", "system_override": system_prompt is not None,
               "images": filled, "image_used": used, "image_ignored": [slot for slot in filled if slot != used], "events": 1, "dropped_events": 0,
               "prompt": "", "caption": "", "request": str(prompt)}
        self.jobs[identifier] = job
        self.order.append(identifier)
        self.submissions.append({"id": identifier, "prompt": prompt, "variant": variant, "images": filled, "pictures": supplied, "system_prompt": system_prompt,
                                 "origin": origin, "remember": remember})
        return identifier

    def status(self, identifier):
        job = self.jobs.get(identifier)
        if job is None:
            return None
        waiting = [key for key in self.order if key in self.jobs and self.jobs[key]["state"] == "queued"]
        found = dict(job)
        found["position"] = waiting.index(identifier) + 1 if identifier in waiting else 0
        if job["state"] == "failed":
            found["error"] = job.get("error", "")
        if job["state"] == "cancelled":
            found["reason"] = job.get("reason", "")
        return found

    def result(self, identifier):
        job = self.jobs.get(identifier)
        return job["prompt"] if job else ""

    def cancel(self, identifier, reason=""):
        job = self.jobs.get(identifier)
        if job is None:
            return {"ok": False, "code": "unknown_job"}
        if job["state"] in ("done", "failed", "cancelled"):
            return {"ok": False, "code": "already_finished"}
        was = job["state"]
        job["state"] = "cancelled"
        job["reason"] = reason
        self.cancelled.append(identifier)
        return {"ok": True, "state": "cancelled", "was": was}

    def cancel_all(self, origin, reason=""):
        if not str(origin or "").strip():
            raise FakeRejected("cancel_all needs the origin", "empty_origin")
        self.cancel_all_calls.append((origin, reason))
        ids = [key for key, job in self.jobs.items() if job["origin"] == origin and job["state"] in ("queued", "running")]
        for key in ids:
            self.cancel(key, reason)
        return {"ok": True, "cancelled": len(ids), "ids": ids}

    def subscribe(self, *args, **kwargs):
        raise NotImplementedError

    def forget(self, identifier):
        return self.jobs.pop(identifier, None) is not None

    def variants(self):
        return (("fl2va", "FL2VA — from text or a frame"), ("ref2va", "Ref2VA — from reference material"))

    def system_prompt(self, variant="", *, has_image=False):
        return f"DEFAULT {variant or 'fl2va'} {'image' if has_image else 'text'} instructions"

    def system_prompts(self):
        return {variant: {"label": label, "text": self.system_prompt(variant), "image": self.system_prompt(variant, has_image=True),
                          "structure": f"{variant} structure", "max_tokens": 1024 if variant == "fl2va" else 2048}
                for variant, label in self.variants()}

    # -- the test's hands on the run
    def run_next(self):
        for key in self.order:
            job = self.jobs.get(key)
            if job and job["state"] == "queued":
                job["state"] = "running"
                job["started"] = 2.0
                job["stage"] = "Describing the image…" if job["image_used"] else "Writing the prompt…"
                return key
        return ""

    def finish(self, identifier, prompt=None, elapsed=43.4):
        job = self.jobs[identifier]
        job["state"] = "done"
        job["prompt"] = prompt if prompt is not None else f"ENHANCED: {job['request']}"
        job["caption"] = "a caption" if job["image_used"] else ""
        job["elapsed"] = elapsed
        job["finished"] = 3.0

    def fail(self, identifier, error="the server went away"):
        job = self.jobs[identifier]
        job["state"] = "failed"
        job["error"] = error

    def cancel_from_panel(self, identifier):
        self.cancel(identifier, "cancelled from LLM Studio")


def _refused(call, *args, **keywords) -> str:
    try:
        call(*args, **keywords)
    except IntegrationError as error:
        return error.code
    return ""


def _png(colour=(10, 200, 30, 255), size=(6, 4)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def _request(prompt="a rough prompt", **images):
    request = {"prompt": prompt, "images": {}}
    for field, handle in images.items():
        request["images"][field] = handle
    return request


def _positive(status="queued", depth=None):
    result = {"ok": True, "status": status, "tasks_added": 1, "applied": {"prompt": True}, "inherited": ["start", "end", "references"],
              "ignored": [], "model": {"type": "minimax_h3_fl2va", "label": "MiniMax H3 FL2VA"}, "route": "generate" if status == "started" else "queue"}
    if depth is not None:
        result["queue_depth"] = depth
    return result


# ----------------------------------------------------------------- the loader --


def loader_checks(r: Results, base: pathlib.Path) -> None:
    """The real import path: a folder shaped like the other extension, found
    through the host's extension list, imported with its folder appended to
    sys.path, and usable afterwards by its bare name."""
    folder = base / "SD-Neo-ModelSwitchRefiner"
    folder.mkdir()
    (folder / "mc_llm_jobs.py").write_text(
        "class Rejected(Exception):\n"
        "    def __init__(self, reason, code='rejected'):\n"
        "        super().__init__(reason)\n"
        "        self.reason, self.code = reason, code\n"
        "MAX_QUEUED = 32\n", encoding="utf-8")
    (folder / "mc_llm_api.py").write_text(
        "import mc_llm_jobs as jobs\n"
        "API_VERSION = 1\n"
        "Rejected = jobs.Rejected\n"
        "def submit_minimax(prompt, **keywords):\n"
        "    return 'abc123'\n"
        "def capabilities():\n"
        "    return {'api_version': 1, 'enabled': True, 'configured': True, 'vision': False, 'model': 'x.gguf', 'reason': 'no vision', 'variants': ['fl2va', 'ref2va']}\n"
        "def status(job_id):\n"
        "    return None\n"
        "def system_prompt(variant='', *, has_image=False):\n"
        "    return 'from the folder ' + (variant or 'fl2va')\n", encoding="utf-8")
    enhance.reset_for_tests()
    saved_modules = {name: sys.modules.get(name) for name in ("mc_llm_api", "mc_llm_jobs", "modules.extensions")}
    saved_path = list(sys.path)
    for name in ("mc_llm_api", "mc_llm_jobs"):
        sys.modules.pop(name, None)
    r.check("with no such extension anywhere the API is None and the reason says so",
            enhance.api() is None and "not found" in enhance.capabilities()["reason"] and enhance.capabilities()["found"] is False)
    fake_extensions = types.ModuleType("modules.extensions")
    fake_extensions.extensions = [types.SimpleNamespace(path=str(base / "some-other-extension")), types.SimpleNamespace(path=str(folder))]
    import modules as host_modules  # the stub package

    sys.modules["modules.extensions"] = fake_extensions
    setattr(host_modules, "extensions", fake_extensions)
    try:
        module = enhance.api()
        r.check("the API is found through the host's extension list and imported off its folder",
                module is not None and getattr(module, "API_VERSION", None) == 1 and module.__file__.startswith(str(folder)), str(module))
        r.check("its folder was appended to sys.path, never inserted in front", sys.path and sys.path[-1] == str(folder) and sys.path[0] != str(folder))
        r.check("and its neighbours import by their bare names afterwards", "mc_llm_jobs" in sys.modules)
        found = enhance.capabilities()
        r.check("capabilities are read from it", found["found"] and found["available"] and found["vision"] is False and found["model"] == "x.gguf" and "no vision" in found["reason"], json.dumps(found))
        r.check("and so is a default system prompt", enhance.default_prompt("ref2va", "text") == "from the folder ref2va")
        r.check("the answer is cached: a second call is the same module", enhance.api() is module)
    finally:
        for name, saved in saved_modules.items():
            if saved is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved
        if hasattr(host_modules, "extensions") and getattr(host_modules, "extensions") is fake_extensions:
            delattr(host_modules, "extensions")
        sys.path[:] = saved_path
        enhance.reset_for_tests()


# ------------------------------------------------------------ the decisions --


def decision_checks(r: Results, fake: FakeApi) -> None:
    r.check("a MiniMax H3 model type names its variant",
            enhance.variant_for_model(FL2VA_MODEL) == "fl2va" and enhance.variant_for_model(REF2VA_MODEL) == "ref2va")
    r.check("a finetune is read through its architecture", enhance.variant_for_model(FINETUNE_MODEL) == "fl2va")
    r.check("any other model is no variant at all", enhance.variant_for_model(VIDEO_MODEL) == "" and enhance.variant_for_model({}) == "" and enhance.variant_for_model(None) == "")
    r.check("a MiniMax model that is neither variant is refused rather than guessed", enhance.variant_for_model({"type": "minimax_h3_other"}) == "")

    start = {"kind": "clipboard_asset", "id": "1" * 32}
    end = {"kind": "clipboard_asset", "id": "2" * 32}
    refs = [{"kind": "staged", "id": "3" * 32}, {"kind": "staged", "id": "4" * 32}]
    planned = enhance.plan({"prompt": "p", "images": {"start": start, "end": end, "references": refs}}, FL2VA_MODEL)
    r.check("FL2VA takes the first and last frame and leaves the references out, saying so",
            planned["variant"] == "fl2va" and planned["slots"] == {"first_frame": start, "last_frame": end} and planned["dropped"] == ["references"] and planned["has_image"])
    planned = enhance.plan({"prompt": "p", "images": {"start": start, "references": refs}}, REF2VA_MODEL)
    r.check("Ref2VA takes the first reference and leaves the frames out; the second reference is counted",
            planned["variant"] == "ref2va" and planned["slots"] == {"reference": refs[0]} and planned["dropped"] == ["start"] and planned["extra_references"] == 1)
    planned = enhance.plan({"prompt": "p", "images": {"references": refs[:1]}}, FL2VA_MODEL)
    r.check("a reference alone for FL2VA is dropped, never captioned in its place - the API's fallback is not taken",
            planned["slots"] == {} and planned["dropped"] == ["references"] and planned["has_image"] is False)
    r.check("a page on another model is refused before anything is stored", _refused(enhance.plan, {"prompt": "p", "images": {}}, VIDEO_MODEL) == errors.ENHANCE_MODEL_UNSUPPORTED)
    r.check("a request that inherits the page's prompt is refused: the typed prompt is required",
            _refused(enhance.plan, {"images": {}}, FL2VA_MODEL) == errors.ENHANCE_PROMPT_REQUIRED)

    found = enhance.capabilities()
    r.check("capabilities through the fake: found, available, vision, the model's basename",
            found["found"] and found["available"] and found["vision"] and found["model"] == "qwen3-vl-8b.gguf" and found["api_version"] == 1 and found["reason"] == "", json.dumps(found))
    fake.enabled = False
    r.check("LLM Studio switched off is not available, with its reason", not enhance.capabilities()["available"] and "switched off" in enhance.capabilities()["reason"])
    fake.enabled = True
    fake.configured = False
    r.check("no model set up is not available either", not enhance.capabilities()["available"] and "No language model" in enhance.capabilities()["reason"])
    fake.configured = True
    fake.API_VERSION = 2
    r.check("a newer major API version is refused rather than guessed at", not enhance.capabilities()["available"] and "version 2" in enhance.capabilities()["reason"])
    fake.API_VERSION = 1
    line = enhance.availability(FL2VA_MODEL)
    r.check("the tab's line says ready for an H3 page, and names the variant", line["state"] == "ready" and "FL2VA" in line["text"] and line["variant"] == "fl2va", json.dumps(line))
    r.check("says the model stands in the way for another page", enhance.availability(VIDEO_MODEL)["state"] == "model")
    r.check("and unknown while the page has not said its model", enhance.availability({})["state"] == "unknown")
    fake.enabled = False
    r.check("and blocked when the LLM side cannot take a request", enhance.availability(FL2VA_MODEL)["state"] == "blocked")
    fake.enabled = True


def override_checks(r: Results) -> None:
    r.check("enhanced prompts are off until somebody turns them on", enhance.enabled() is False)
    r.check("turning them on is read back from disk", enhance.set_enabled(True) is True and enhance.enabled() is True and (config.config_dir() / enhance.ENHANCE_NAME).is_file())
    enhance.set_enabled(False)
    r.check("and off again", enhance.enabled() is False)
    text, source = enhance.effective_prompt("fl2va", "image")
    r.check("with no override the effective instructions are the API's default", source == "default" and text == "DEFAULT fl2va image instructions")
    r.check("a blank override is refused, not saved", _refused(enhance.set_override, "fl2va", "image", "   \n") == errors.ENHANCE_SYSTEM_PROMPT_EMPTY and enhance.override("fl2va", "image") == "")
    r.check("an unknown variant or mode is refused", _refused(enhance.set_override, "fl3va", "image", "x") == errors.REQUEST_INVALID and _refused(enhance.override, "fl2va", "video") == errors.REQUEST_INVALID)
    saved = enhance.set_override("fl2va", "image", "Write it MY way\r\nwith two lines")
    r.check("an override is saved with its newlines normalised", saved == "Write it MY way\nwith two lines" and enhance.override("fl2va", "image") == saved)
    text, source = enhance.effective_prompt("fl2va", "image")
    r.check("and is what an enhanced request runs under", source == "override" and text == saved)
    r.check("the other three sets are untouched", enhance.effective_prompt("fl2va", "text")[1] == "default" and enhance.overrides() == {"fl2va": {"text": False, "image": True}, "ref2va": {"text": False, "image": False}})
    document = json.loads((config.config_dir() / enhance.ENHANCE_NAME).read_text(encoding="utf-8"))
    r.check("it lives in its own document, beside the outbox, and would survive a restart",
            document["overrides"]["fl2va"]["image"] == saved and document["enabled"] is False and document["schema"] == 1)
    r.check("Restore default forgets it", enhance.clear_override("fl2va", "image") is True and enhance.effective_prompt("fl2va", "image")[1] == "default" and enhance.clear_override("fl2va", "image") is False)
    prompts = enhance.system_prompts()
    r.check("all four defaults are readable with their structure guides", set(prompts) == {"fl2va", "ref2va"} and prompts["ref2va"]["max_tokens"] == 2048 and prompts["fl2va"]["image"] == "DEFAULT fl2va image instructions")


# ------------------------------------------------------------- the pipeline --


def pipeline_checks(r: Results, fake: FakeApi, clock: _Clock, base: pathlib.Path) -> None:
    library = clipboard_store.store()
    library.set_root(str(base / "library"), create=True)
    first = library.import_bytes(_png((200, 30, 30, 255)), "first.png", "upload")
    reference = library.import_bytes(_png((30, 30, 200, 255)), "ref.png", "upload")
    start = {"kind": "clipboard_asset", "id": first.asset_id}
    ref = {"kind": "clipboard_asset", "id": reference.asset_id}
    outbox.use_running(lambda: True)

    # -- off: a press is a plain job, whatever the model
    plain = outbox.submit(_request("as typed"), PAGE_A, model=FL2VA_MODEL)
    r.check("with the switch off a press is a pending job, not enhanced, and the model is remembered",
            plain["state"] == "pending" and plain["enhance"] is None and plain["enhance_requested"] is False and plain["model"]["type"] == "minimax_h3_fl2va" and not fake.submissions)
    claimed = outbox.claim(PAGE_A)
    outbox.report(plain["job_id"], claimed["lease"], "done", _positive("started", depth=0))

    # -- on: refusals store nothing
    enhance.set_enabled(True)
    r.check("a page on another model is refused with ENHANCE_MODEL_UNSUPPORTED, and nothing is stored or asked",
            _refused(outbox.submit, _request("x"), PAGE_A, model=VIDEO_MODEL) == errors.ENHANCE_MODEL_UNSUPPORTED and len(outbox.jobs()) == 1 and not fake.submissions)
    r.check("a press without a typed prompt is refused: the page's prompt cannot be enhanced from here",
            _refused(outbox.submit, {"images": {"start": start}}, PAGE_A, model=FL2VA_MODEL) == errors.ENHANCE_PROMPT_REQUIRED and not fake.submissions)
    fake.enabled = False
    # A policy state with its own sentence, not a flattened "unavailable":
    # a user who switched LLM Studio off is told that, and nothing here
    # turns it back on for them.
    r.check("LLM Studio off is ENHANCE_SWITCHED_OFF, before anything is stored",
            _refused(outbox.submit, _request("x"), PAGE_A, model=FL2VA_MODEL) == errors.ENHANCE_SWITCHED_OFF and len(outbox.jobs()) == 1)
    fake.enabled = True
    fake.configured = False
    r.check("no configured model is ENHANCE_NOT_CONFIGURED, and nothing is downloaded silently",
            _refused(outbox.submit, _request("x"), PAGE_A, model=FL2VA_MODEL) == errors.ENHANCE_NOT_CONFIGURED and len(outbox.jobs()) == 1)
    fake.configured = True
    fake.enabled = False
    fake.enabled = True
    fake.vision = False
    r.check("a picture for a model that cannot see is ENHANCE_NO_VISION, from the API's own refusal",
            _refused(outbox.submit, _request("x", start=start), PAGE_A, model=FL2VA_MODEL) == errors.ENHANCE_NO_VISION and len(outbox.jobs()) == 1 and not fake.jobs)
    fake.vision = True
    r.check("a caller may decline enhancement for one request while the switch is on",
            outbox.submit(_request("plain by choice"), PAGE_B, enhance=False, model=FL2VA_MODEL)["state"] == "pending" and not fake.submissions)
    outbox.cancel([job for job in outbox.jobs() if job["state"] == "pending"][0]["job_id"])

    # -- an enhanced press asks the LLM at once and waits in the line
    enhance.set_override("fl2va", "image", "MY fl2va image instructions")
    clock.now += 1
    job = outbox.submit(_request("a rough prompt", start=start, references=[ref]), PAGE_A, model=FL2VA_MODEL)
    record = job["enhance"]
    r.check("the press is an enhancing job with the LLM's id, the variant, and the typed prompt kept",
            job["state"] == "enhancing" and job["enhance_requested"] and record["llm_id"] == fake.order[-1] and record["variant"] == "fl2va"
            and record["prompt_original"] == "a rough prompt" and record["state"] == "queued", json.dumps(record))
    sent = fake.submissions[-1]
    picture = sent["pictures"]["first_frame"]
    r.check("the LLM got the prompt, the variant, the first frame as a picture and not a path, and our origin",
            sent["prompt"] == "a rough prompt" and sent["variant"] == "fl2va" and sent["images"] == ["first_frame"] and hasattr(picture, "size") and sent["origin"] == enhance.ORIGIN and sent["remember"] is True)
    r.check("the reference was left out of the enhancement, and the job says so", record["dropped"] == ["references"] and "reference" not in sent["images"])
    r.check("the saved override for FL2VA with a picture went with it", sent["system_prompt"] == "MY fl2va image instructions" and record["system_override"] is True)
    r.check("the job still carries every image for WanGP", job["request"]["images"]["start"] == start and job["request"]["images"]["references"] == [ref])
    enhance.clear_override("fl2va", "image")

    answer = outbox.claim(PAGE_A)
    r.check("the pressing page is told to wait for the enhancement, naming the job",
            answer.get("wait") == outbox.WAIT_ENHANCE_MS and answer.get("reason") == "enhancing" and answer.get("pending") == 1 and answer.get("job_id") == job["job_id"], str(answer))
    clock.now += 1
    later = outbox.submit(_request("pressed later, plain"), PAGE_B, enhance=False, model=FL2VA_MODEL)
    answer = outbox.claim(PAGE_B)
    r.check("a plain job pressed later on another page waits behind it: the line keeps press order",
            later["state"] == "pending" and answer.get("reason") == "enhancing" and answer.get("pending") == 1, str(answer))
    r.check("pending_count counts both", outbox.pending_count() == 2 and outbox.pending_count(PAGE_A) == 1)

    # -- progress is read from the LLM side on every read
    fake.run_next()
    listed = outbox.get(job["job_id"])
    r.check("once the LLM runs it the job says so, with the stage", listed["enhance"]["state"] == "running" and listed["enhance"]["stage"] == "Describing the image…" and listed["state"] == "enhancing")
    fake.finish(record["llm_id"], "ENHANCED: a rough prompt, at length", elapsed=43.4)
    listed = outbox.get(job["job_id"])
    r.check("a written prompt makes the job pending, carrying it, with the typed prompt kept beside it",
            listed["state"] == "pending" and listed["request"]["prompt"] == "ENHANCED: a rough prompt, at length" and listed["enhance"]["state"] == "done"
            and listed["enhance"]["prompt_original"] == "a rough prompt" and listed["enhance"]["elapsed"] == 43.4 and listed["enhance"]["image_used"] == "first_frame", json.dumps(listed["enhance"]))
    answer = outbox.claim(PAGE_B)
    r.check("page B still waits: the head is now A's pending job and A is asking", answer.get("reason") == "turn", str(answer))
    claimed = outbox.claim(PAGE_A)
    r.check("page A claims it with the enhanced prompt", claimed.get("job", {}).get("job_id") == job["job_id"] and claimed["job"]["request"]["prompt"].startswith("ENHANCED"))
    outbox.report(job["job_id"], claimed["lease"], "sent")
    done = outbox.report(job["job_id"], claimed["lease"], "done", _positive("queued", depth=2))
    r.check("and reports it queued; WanGP's place is recorded from the confirmation", done["state"] == "queued" and done["wangp"] == {"state": "waiting", "position": 2, "queue_depth": 2, "seen_at": done["wangp"]["seen_at"]})
    claimed_b = outbox.claim(PAGE_B)
    r.check("only then does page B's later job go", claimed_b.get("job", {}).get("job_id") == later["job_id"])
    outbox.report(later["job_id"], claimed_b["lease"], "done", _positive("queued", depth=3))

    # -- tracking: the page that queued it says where the task is
    tracked = outbox.track(job["job_id"], PAGE_A, {"state": "waiting", "position": 1, "queue_depth": 1})
    r.check("a track from the owning page moves the job's place in WanGP", tracked["wangp"]["state"] == "waiting" and tracked["wangp"]["position"] == 1)
    r.check("a track from another page is refused", _refused(outbox.track, job["job_id"], PAGE_B, {"state": "generating"}) == errors.REQUEST_INVALID)
    tracked = outbox.track(job["job_id"], PAGE_A, {"state": "generating", "position": 0})
    r.check("generating is recorded", tracked["wangp"]["state"] == "generating" and tracked["wangp"]["position"] == 0)
    tracked = outbox.track(job["job_id"], PAGE_A, {"state": "finished"})
    r.check("finished is recorded and sticks", tracked["wangp"]["state"] == "finished" and outbox.track(job["job_id"], PAGE_A, {"state": "waiting", "position": 4})["wangp"]["state"] == "finished")
    r.check("a report the server cannot read is unknown, never finished", outbox.sanitize_track({"state": "done", "position": -1}) == {"state": "unknown", "position": None, "queue_depth": None})
    r.check("a started job tracked as waiting again follows what the page saw", outbox.track(plain["job_id"], PAGE_A, {"state": "waiting", "position": 1})["wangp"]["state"] == "waiting")

    # -- how an enhancement can end: failed, cancelled in LLM Studio, forgotten, too long
    clock.now += 1
    failing = outbox.submit(_request("will fail"), PAGE_A, model=FL2VA_MODEL)
    fake.run_next()
    fake.fail(failing["enhance"]["llm_id"], "the server went away")
    listed = outbox.get(failing["job_id"])
    r.check("a failed run fails the job with ENHANCE_FAILED and the LLM's own sentence",
            listed["state"] == "failed" and listed["error"]["code"] == errors.ENHANCE_FAILED and listed["error"]["message"] == "the server went away" and listed["enhance"]["state"] == "failed")
    r.check("a job WanGP does not hold is not tracked", outbox.track(failing["job_id"], PAGE_A, {"state": "waiting"})["wangp"] is None)
    clock.now += 1
    panel = outbox.submit(_request("cancelled over there"), PAGE_A, model=FL2VA_MODEL)
    fake.cancel_from_panel(panel["enhance"]["llm_id"])
    listed = outbox.get(panel["job_id"])
    r.check("a request cancelled from LLM Studio's own panel is a cancelled job, with the reason",
            listed["state"] == "cancelled" and listed["error"]["code"] == errors.ENHANCE_CANCELLED and "LLM Studio" in listed["error"]["message"])
    clock.now += 1
    forgotten = outbox.submit(_request("forgotten"), PAGE_A, model=FL2VA_MODEL)
    fake.forget(forgotten["enhance"]["llm_id"])
    listed = outbox.get(forgotten["job_id"])
    r.check("a record the API has forgotten is ENHANCE_LOST, never a silent retry", listed["state"] == "failed" and listed["error"]["code"] == errors.ENHANCE_LOST and listed["enhance"]["state"] == "lost" and len(fake.submissions) == 4)
    clock.now += 1
    long = outbox.submit(_request("too long"), PAGE_A, model=FL2VA_MODEL)
    fake.finish(long["enhance"]["llm_id"], "x" * (protocol.PROMPT_MAX_CHARS + 1))
    listed = outbox.get(long["job_id"])
    r.check("a written prompt over the ceiling fails the job rather than being cut", listed["state"] == "failed" and listed["error"]["code"] == errors.PROMPT_TOO_LONG and listed["request"]["prompt"] == "too long")

    # -- cancel: one, and everything
    clock.now += 1
    one = outbox.submit(_request("cancel me"), PAGE_A, model=FL2VA_MODEL)
    cancelled = outbox.cancel(one["job_id"])
    r.check("cancelling an enhancing job cancels its request on the LLM side too",
            cancelled["state"] == "cancelled" and one["enhance"]["llm_id"] in fake.cancelled and cancelled["error"]["code"] == errors.ENHANCE_CANCELLED)
    clock.now += 1
    first_wait = outbox.submit(_request("first of three"), PAGE_A, model=FL2VA_MODEL)
    clock.now += 1
    second_wait = outbox.submit(_request("second of three, plain"), PAGE_B, enhance=False, model=FL2VA_MODEL)
    clock.now += 1
    third_wait = outbox.submit(_request("third of three"), PAGE_B, model=REF2VA_MODEL)
    r.check("(three waiting: enhancing, pending, enhancing)", [outbox.get(j["job_id"])["state"] for j in (first_wait, second_wait, third_wait)] == ["enhancing", "pending", "enhancing"])
    answer = outbox.cancel_all()
    r.check("Cancel everything cancels every waiting job, and our requests on the LLM side by origin - once",
            answer["cancelled"] == 3 and answer["enhancing"] == 2 and answer["in_flight"] == 0 and fake.cancel_all_calls[-1][0] == enhance.ORIGIN
            and all(outbox.get(j["job_id"])["state"] == "cancelled" for j in (first_wait, second_wait, third_wait)), json.dumps(answer)[:200])
    r.check("and the LLM side has nothing of ours left", not any(job["state"] in ("queued", "running") for job in fake.jobs.values()))

    # -- retry: from the typed prompt, or carrying a written one over
    retried = outbox.retry(failing["job_id"], PAGE_A)
    r.check("a failed enhanced job is retried from the typed prompt and enhanced again",
            retried["state"] == "enhancing" and retried["request"]["prompt"] == "will fail" and retried["retry_of"] == failing["job_id"] and fake.submissions[-1]["prompt"] == "will fail")
    outbox.cancel(retried["job_id"])
    clock.now += 1
    written = outbox.submit(_request("write once"), PAGE_A, model=FL2VA_MODEL)
    fake.finish(written["enhance"]["llm_id"], "ENHANCED: write once")
    claimed = outbox.claim(PAGE_A)
    outbox.report(written["job_id"], claimed["lease"], "sent")
    outbox.report(written["job_id"], claimed["lease"], "done", {"ok": False, "status": "unconfirmed"})
    count_before = len(fake.submissions)
    carried = outbox.retry(written["job_id"], PAGE_A)
    r.check("a retry of a job whose prompt was already written carries it over rather than asking twice",
            carried["state"] == "pending" and carried["request"]["prompt"] == "ENHANCED: write once" and carried["enhance"]["reused_from"] == written["job_id"]
            and carried["enhance"]["prompt_original"] == "write once" and len(fake.submissions) == count_before, json.dumps(carried["enhance"]))
    claimed = outbox.claim(PAGE_A)
    outbox.report(carried["job_id"], claimed["lease"], "done", {"ok": False, "status": "refused", "code": errors.MODEL_CHANGED, "message": "moved"})
    rewritten = outbox.retry(carried["job_id"], PAGE_A)
    r.check("but a MODEL_CHANGED failure is retried by writing the prompt again, from the typed one",
            rewritten["state"] == "enhancing" and rewritten["request"]["prompt"] == "write once" and len(fake.submissions) == count_before + 1)
    outbox.cancel(rewritten["job_id"])

    # -- the document and the log
    stored = json.loads((config.config_dir() / outbox.OUTBOX_NAME).read_text(encoding="utf-8"))
    r.check("the outbox document is at the current schema with the enhancement records inside the jobs",
            stored["schema"] == outbox.SCHEMA and any(item.get("enhance") for item in stored["jobs"]))
    log = process_log.path()
    text = pathlib.Path(log).read_text(encoding="utf-8") if os.path.isfile(log) else ""
    r.check("nothing written to the log is a prompt, typed or written, or an override",
            "rough prompt" not in text and "ENHANCED" not in text and "MY fl2va" not in text and "write once" not in text and "first.png" not in text, text[-400:])
    enhance.set_enabled(False)


# ---------------------------------------------------------------- the routes --


def route_checks(r: Results, fake: FakeApi) -> None:
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    outbox.use_running(lambda: True)
    app = FastAPI()
    interop.install(app)
    client = TestClient(app)

    described = client.get(interop.ENHANCE_ROUTE).json()
    r.check("GET /enhance says the switch is off, the LLM side is available, and the slot rules",
            described.get("ok") and described["enabled"] is False and described["capabilities"]["available"] and described["slots"]["fl2va"] == {"start": "first_frame", "end": "last_frame"}
            and described["origin"] == enhance.ORIGIN, json.dumps(described)[:200])
    contract = client.get(interop.CONTRACT_ROUTE).json()
    r.check("the contract names the enhance route, the track states and the raised prompt ceiling",
            contract["enhance"] == interop.ENHANCE_ROUTE and contract["track_states"] == ["waiting", "generating", "finished", "unknown"] and contract["prompt_max_chars"] == 12000)
    answer = client.post(interop.OUTBOX_SUBMIT_ROUTE, json={"request": _request("through the route"), "page": PAGE_A, "enhance": True, "model": FL2VA_MODEL}).json()
    r.check("POST /outbox/submit with enhance and a model is an enhancing job", answer.get("ok") and answer["job"]["state"] == "enhancing" and answer["job"]["enhance"]["variant"] == "fl2va", str(answer)[:160])
    blocked = client.post(interop.OUTBOX_SUBMIT_ROUTE, json={"request": _request("x"), "page": PAGE_A, "enhance": True, "model": VIDEO_MODEL})
    r.check("and 409 ENHANCE_MODEL_UNSUPPORTED for a page on another model", blocked.status_code == 409 and blocked.json().get("code") == errors.ENHANCE_MODEL_UNSUPPORTED)
    waiting = client.post(interop.OUTBOX_CLAIM_ROUTE, json={"page": PAGE_A}).json()
    r.check("a claim waits for the enhancement", waiting.get("reason") == "enhancing")
    llm_id = answer["job"]["enhance"]["llm_id"]
    fake.finish(llm_id)
    claimed = client.post(interop.OUTBOX_CLAIM_ROUTE, json={"page": PAGE_A}).json()
    r.check("once written, the claim hands out the job with the enhanced prompt", claimed.get("job", {}).get("request", {}).get("prompt", "").startswith("ENHANCED"))
    job_id = claimed["job"]["job_id"]
    client.post(interop.OUTBOX_REPORT_ROUTE, json={"job_id": job_id, "lease": claimed["lease"], "phase": "done", "result": _positive("queued", depth=1)})
    tracked = client.post(interop.OUTBOX_TRACK_ROUTE, json={"job_id": job_id, "page": PAGE_A, "state": "generating", "position": 0}).json()
    r.check("POST /outbox/track records where the task is", tracked.get("ok") and tracked["job"]["wangp"]["state"] == "generating")
    wrong = client.post(interop.OUTBOX_TRACK_ROUTE, json={"job_id": job_id, "page": PAGE_B, "state": "finished"})
    r.check("and refuses another page", wrong.status_code == 400 and wrong.json().get("code") == errors.REQUEST_INVALID)
    client.post(interop.OUTBOX_SUBMIT_ROUTE, json={"request": _request("one"), "page": PAGE_A, "enhance": True, "model": FL2VA_MODEL})
    client.post(interop.OUTBOX_SUBMIT_ROUTE, json={"request": _request("two"), "page": PAGE_B, "enhance": False, "model": FL2VA_MODEL})
    cancelled = client.post(interop.OUTBOX_CANCEL_ALL_ROUTE, json={"page": PAGE_A}).json()
    r.check("POST /outbox/cancel_all empties the line and lists the jobs", cancelled.get("ok") and cancelled["cancelled"] == 2 and len(cancelled["jobs"]) == 2 and cancelled["enhancing"] == 1)


# ------------------------------------------------------------------- the tab --


def tab_checks(r: Results, fake: FakeApi, clock: _Clock, base: pathlib.Path) -> None:
    import forge_like  # noqa: F401  (Forge's patches, before any Gradio component)
    from minipaint_neo.clipboard import ui as clipboard_ui

    outbox.use_running(lambda: True)
    tab = clipboard_ui.ClipboardTab()
    page_id = "c" * 16
    line = tab.toggle_enhance(True, json.dumps(FL2VA_MODEL))
    r.check("the switch turns enhancement on and the line says ready, naming the variant", enhance.enabled() and 'data-state="ready"' in line and "FL2VA" in line and "are on" in line, line)
    r.check("the line follows the page's model", 'data-state="model"' in tab.model_changed(json.dumps(VIDEO_MODEL)) and 'data-state="unknown"' in tab.model_changed(""))
    box, state = tab.system_prompt_selected("ref2va", "image")
    r.check("the editor shows a default with its provenance", box["value"] == "DEFAULT ref2va image instructions" and state.startswith("**Default**"))
    box, state, status = tab.apply_override("ref2va", "image", "MINE for ref2va")
    r.check("Apply override saves it and says so", box["value"] == "MINE for ref2va" and state.startswith("**Override saved**") and "Override saved" in status and enhance.override("ref2va", "image") == "MINE for ref2va")
    _box, state, status = tab.apply_override("ref2va", "image", "  ")
    r.check("a blank override is refused with the sentence", "cannot be blank" in status and state.startswith("**Override saved**"))
    box, state, status = tab.restore_default("ref2va", "image")
    r.check("Restore default forgets it and shows the default again", box["value"] == "DEFAULT ref2va image instructions" and state.startswith("**Default**") and enhance.override("ref2va", "image") == "")

    tab.prompt_changed("typed in the tab")
    instruction, status, listing, button = tab.prepare_queue("typed in the tab", page_id, json.dumps(FL2VA_MODEL))
    job = outbox.jobs()[-1]
    r.check("an enhanced press is an enhancing job, and the status line says so",
            json.loads(instruction)["job_id"] == job["job_id"] and job["state"] == "enhancing" and status.startswith("Enhancing the prompt as FL2VA") and "Enhancing" in listing and "LLM:" in listing, status)
    instruction, status, listing, button = tab.prepare_queue("typed in the tab", page_id, json.dumps(VIDEO_MODEL))
    r.check("a press on another model is refused with the sentence and a way out", instruction == "" and "MiniMax H3" in status and "switch enhanced prompts off" in status)
    fake.run_next()
    listing, status, history_listing, button = tab.refresh_outbox(page_id)
    r.check("the list shows the stage while the LLM runs", "Describing" not in listing and "Writing the prompt" in listing and "1 being enhanced" in status, status)
    fake.finish(job["enhance"]["llm_id"], "ENHANCED: typed in the tab, at length")
    claimed = outbox.claim(page_id)
    outbox.report(job["job_id"], claimed["lease"], "done", _positive("started", depth=0))
    listing, status, history_listing, button = tab.refresh_outbox(page_id)
    record = history.load_history()[0]
    r.check("the history records the typed prompt as the recipe and the written one beside it",
            record["prompt_override"] == "typed in the tab" and record["enhanced"] is True and record["enhanced_prompt"] == "ENHANCED: typed in the tab, at length", json.dumps(record))
    r.check("the card shows both prompts, the enhancement line and WanGP's place",
            "minipaint-clip-job-prompt-typed" in listing and "ENHANCED: typed in the tab" in listing and "Enhanced as FL2VA" in listing and "WanGP is generating it" in listing and "enhanced prompt" in listing)
    r.check("and the history list shows the enhanced text", "enhanced: ENHANCED" in history_listing)
    outbox.track(job["job_id"], page_id, {"state": "finished"})
    listing, status, history_listing, button = tab.refresh_outbox(page_id)
    r.check("a finished task is said to have left WanGP's queue", "Left WanGP&#x27;s queue" in listing or "Left WanGP's queue" in listing, listing[-600:])

    clock.now += 1
    tab.prepare_queue("one more", page_id, json.dumps(FL2VA_MODEL))
    listing, status = tab.cancel_all(page_id)
    r.check("Cancel everything from the tab cancels the line and says how many", status.startswith("Cancelled 1 waiting request") and "1 enhancement" in status and "Cancelled" in listing, status)
    listing, status = tab.cancel_all(page_id)
    r.check("and says when nothing was waiting", status.startswith("Nothing was waiting"))
    tab.toggle_enhance(False, "")
    r.check("the switch turns it off again", enhance.enabled() is False)


def run() -> Results:
    r = Results("clipboard enhance")
    with tempfile.TemporaryDirectory(prefix="minipaint-enhance-") as scratch:
        base = pathlib.Path(scratch)
        wangp_config.use_config_dir(base / "data")
        config.use_config_dir(base / "data")
        process_log.use_log_dir(base / "logs")
        clipboard_store.reset_for_tests()
        outbox.reset_for_tests()
        enhance.reset_for_tests()
        clock = _Clock()
        outbox.use_clock(clock)
        try:
            loader_checks(r, base)
            fake = FakeApi()
            enhance.use_api(fake)
            decision_checks(r, fake)
            override_checks(r)
            pipeline_checks(r, fake, clock, base)
            outbox.reset_for_tests()
            outbox.use_clock(clock)
            (config.config_dir() / outbox.OUTBOX_NAME).unlink(missing_ok=True)
            route_checks(r, fake)
            outbox.reset_for_tests()
            outbox.use_clock(clock)
            (config.config_dir() / outbox.OUTBOX_NAME).unlink(missing_ok=True)
            (config.config_dir() / config.HISTORY_NAME).unlink(missing_ok=True)
            tab_checks(r, fake, clock, base)
        finally:
            enhance.reset_for_tests()
            outbox.reset_for_tests()
            wangp_config.use_config_dir(None)
            config.use_config_dir(None)
            process_log.use_log_dir(None)
            clipboard_store.reset_for_tests()
    return r


if __name__ == "__main__":
    sys.exit(0 if run().report() else 1)
