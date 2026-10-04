"""The composer's History, and Load from View Outputs.

Two asks of 2026-10-04, held here end to end on the server side:

* "the history for the last ten prompts submitted from clipboard to wangp.
  Not the enhanced, but the original raw input ... a new button in the right
  column 'History' where i can view and load from" - ``prompts``: what goes
  in (the Clipboard's own two doors, as typed, never the written prompt, never
  a refused press or another extension's request), how many stay (ten, a
  resend moving up), what Load does (the draft's prompt and nothing else) and
  the route the dialog reads and loads through.

* "I should also be able to load from our 'view outputs' video. Make sure i
  can load up supporting LTX 2.3 and LTX 2.5" - every output keeps the recipe
  of the request that made it (``history.recipe_of_job``, kept by
  ``outputs``), so Load works for a video whatever asked for it and however
  long ago, and the tab's ``history_action`` puts it back: the prompt as it
  was typed, the pictures still in the library, nothing queued.

The browser half of both - the button, the dialog, the caption - is
``browser_clipboard.py``'s.
"""

from harness import Results, setup_path

setup_path()

import json  # noqa: E402
import pathlib  # noqa: E402
import tempfile  # noqa: E402

from PIL import Image  # noqa: E402

from minipaint_neo.clipboard import config, enhance, executor, history, outbox, outputs, prompts, routes, store, targets  # noqa: E402
from minipaint_neo.wangp import config as wangp_config  # noqa: E402
from minipaint_neo.wangp import errors, process_log  # noqa: E402
from test_clipboard_enhance import FakeApi  # noqa: E402

PAGE = "d" * 16
LTX_23 = {"type": "ltx2_22B_distilled", "label": "LTX-2 2.3 Distilled 1.0 22B", "family": "ltx2", "architecture": "ltx2_22B"}
LTX_25 = {"type": "ltx2_25_22B_distilled", "label": "LTX-2 2.5 Distilled 22B", "family": "ltx2", "architecture": "ltx2_25_22B"}
FL2VA = {"type": "minimax_h3_fl2va", "label": "MiniMax H3 FL2VA 33B", "family": "minimax_h3", "architecture": "minimax_h3_fl2va"}


class _Clock:
    def __init__(self):
        self.now = 1_700_000_000.0

    def __call__(self):
        return self.now


def _asset(library, name, colour):
    return library.import_image(Image.new("RGB", (32, 24), colour), name, "upload").asset_id


def _journal_text(base: pathlib.Path) -> str:
    return "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in (base / "logs").rglob("*") if path.is_file())


# ------------------------------------------------------------------ the list --


def store_checks(r: Results) -> None:
    prompts.reset_for_tests()
    r.check("nothing is kept until something is sent", prompts.entries() == [] and prompts.view() == [])
    first = prompts.remember("  a fox in the snow \r\nat dusk  ", model=LTX_25, target="ltx25", enhanced=True)
    r.check("a prompt is kept as it was typed: its own lines, cleaned as a request is, and nothing added",
            first is not None and first["prompt"] == "a fox in the snow \nat dusk" and first["enhanced"] is True
            and first["target"] == "ltx25" and first["model"]["label"] == "LTX-2 2.5 Distilled 22B", json.dumps(first)[:300])
    r.check("no text is no entry", prompts.remember("   ") is None and prompts.remember(None) is None and len(prompts.entries()) == 1)
    for index in range(12):
        prompts.remember(f"prompt number {index}", model=FL2VA, target="fl2va")
    listed = prompts.entries()
    r.check("ten are kept, newest first: the oldest went first",
            len(listed) == prompts.MAX_PROMPTS == 10 and listed[0]["prompt"] == "prompt number 11"
            and listed[-1]["prompt"] == "prompt number 2" and all(item["prompt"] != "a fox in the snow \nat dusk" for item in listed),
            str([item["prompt"] for item in listed]))
    again = prompts.remember("prompt number 5", model=LTX_23, target="ltx23", origin="gallery")
    listed = prompts.entries()
    r.check("the same words sent again move to the top instead of taking a second place",
            len(listed) == 10 and listed[0]["id"] == again["id"] and [item["prompt"] for item in listed].count("prompt number 5") == 1
            and listed[0]["origin"] == "gallery" and listed[0]["target"] == "ltx23")
    view = prompts.view()
    r.check("the dialog is given the text whole, when, the model by its label, and which press it was",
            view[0]["prompt"] == "prompt number 5" and view[0]["model"] == "LTX-2 2.3 Distilled 1.0 22B"
            and view[0]["target_label"] == "LTX 2.3 Distilled" and view[0]["origin"] == "from the gallery"
            and view[0]["when"].endswith(" UTC") and view[0]["when"][10] == " " and view[1]["origin"] == "", json.dumps(view[0])[:300])

    # A document somebody edited by hand: what is not an entry is not shown.
    config.write_document(config.PROMPTS_NAME, {"schema": 1, "prompts": [
        {"id": "not-an-id", "prompt": "x"},
        {"id": "a" * 16, "prompt": "   "},
        {"id": "b" * 16, "prompt": "kept", "origin": "somewhere", "target": "wan", "enhanced": "yes", "model": "nope"},
        "a string",
    ]})
    listed = prompts.entries()
    r.check("a hand-edited document: only real entries, each read the safe way",
            len(listed) == 1 and listed[0]["prompt"] == "kept" and listed[0]["origin"] == "clipboard" and listed[0]["target"] == ""
            and listed[0]["enhanced"] is False and listed[0]["model"] == {"type": "", "label": "", "family": "", "architecture": ""},
            json.dumps(listed)[:300])
    prompts.reset_for_tests()


def load_checks(r: Results, library) -> None:
    prompts.reset_for_tests()
    first = _asset(library, "load-first.png", (200, 10, 10))
    history.save_draft({"prompt_override": "what was there", "first_asset_id": first, "last_asset_id": "",
                        "reference_asset_ids": []})
    entry = prompts.remember("she turns from the window and waves", model=LTX_25, target="ltx25")
    text = prompts.load(entry["id"])
    draft = history.load_draft()
    r.check("Load puts the words in the composer's draft and answers with them",
            text == "she turns from the window and waves" and draft["prompt_override"] == text)
    r.check("and nothing else: the first frame it found there is still there", draft["first_asset_id"] == first)
    r.check("a prompt that has left the list loads nothing", prompts.load("f" * 16) is None and prompts.load("") is None
            and history.load_draft()["prompt_override"] == text)
    history.save_draft(history.empty_draft())
    prompts.reset_for_tests()


# ------------------------------------------------------- what goes in, and how --


def press_checks(r: Results, base: pathlib.Path) -> None:
    prompts.reset_for_tests()
    outbox.use_executor(outbox.EXECUTOR_BROWSER)
    job = outbox.submit({"prompt": "the composer's words"}, PAGE, outbox.ORIGIN_CLIPBOARD, model=LTX_25)
    top = prompts.entries()[0]
    r.check("a stored press from the composer puts its prompt at the top, with the model and target the press read",
            top["prompt"] == "the composer's words" and top["origin"] == "clipboard" and top["target"] == "ltx25"
            and top["model"]["type"] == "ltx2_25_22B_distilled" and top["enhanced"] is False, json.dumps(top)[:300])
    outbox.cancel(job["job_id"])
    job = outbox.submit({"prompt": "the popup's words"}, PAGE, outbox.ORIGIN_GALLERY, model=FL2VA)
    r.check("so does the gallery popup's Generate, said as such", prompts.entries()[0]["prompt"] == "the popup's words"
            and prompts.entries()[0]["origin"] == "gallery")
    outbox.cancel(job["job_id"])
    count = len(prompts.entries())
    job = outbox.submit({"prompt": "another extension's words"}, PAGE, outbox.ORIGIN_API, model=FL2VA)
    r.check("another extension's request over the public API is its own, not the Clipboard's",
            len(prompts.entries()) == count and prompts.entries()[0]["prompt"] == "the popup's words")
    outbox.cancel(job["job_id"])
    job = outbox.submit({"images": {}}, PAGE, outbox.ORIGIN_CLIPBOARD, model=FL2VA)
    r.check("a request that inherits the WanGP page's prompt has no words of its own to keep", len(prompts.entries()) == count)
    outbox.cancel(job["job_id"])

    blocked = targets._blocked(errors.TARGET_UNSUPPORTED, enhance.model_block(FL2VA), message="no")
    targets.use_readiness(lambda model=None, fresh=False: blocked)
    try:
        outbox.submit({"prompt": "refused words"}, PAGE, outbox.ORIGIN_CLIPBOARD, model=FL2VA)
        refused = False
    except Exception:
        refused = True
    targets.use_readiness(targets.always_ready)
    r.check("a press that was refused stored nothing and sent nothing, so it is not here",
            refused and all(item["prompt"] != "refused words" for item in prompts.entries()))

    original = prompts.remember

    def broken(*args, **keywords):
        raise OSError("disk full")

    prompts.remember = broken
    try:
        job = outbox.submit({"prompt": "kept anyway"}, PAGE, outbox.ORIGIN_CLIPBOARD, model=FL2VA)
        stored = outbox.get(job["job_id"]) is not None
    finally:
        prompts.remember = original
    r.check("a history that would not write never costs the press: the job is stored all the same",
            stored and "the prompt history could not be written (OSError)" in _journal_text(base))
    outbox.cancel(job["job_id"])
    prompts.reset_for_tests()


def enhanced_checks(r: Results) -> None:
    """Not the enhanced, but the original raw input."""
    prompts.reset_for_tests()
    fake = FakeApi()
    enhance.use_api(fake)
    outbox.use_executor(outbox.EXECUTOR_BROWSER)
    job = outbox.submit({"prompt": "a car chase at dusk"}, PAGE, outbox.ORIGIN_CLIPBOARD, model=FL2VA, enhance=True)
    llm = outbox.get(job["job_id"])["enhance"]["llm_id"]
    fake.finish(llm, "WRITTEN: a car speeds through a rain-soaked street as the sun sets behind the towers.")
    written = outbox.get(job["job_id"])
    top = prompts.entries()[0]
    r.check("an enhanced press keeps the words that were typed, marked enhanced - not the prompt the writer made",
            written["request"]["prompt"].startswith("WRITTEN:") and top["prompt"] == "a car chase at dusk" and top["enhanced"] is True
            and len(prompts.entries()) == 1, json.dumps(top)[:200])

    # A retry that carries the written prompt over still files the typed one.
    outbox.cancel(job["job_id"])
    prompts.remember("something in between", model=FL2VA, target="fl2va")
    retried = outbox.retry(job["job_id"], PAGE)
    r.check("a retry that reuses the written prompt is the typed words again, moved back to the top",
            retried["request"]["prompt"].startswith("WRITTEN:") and prompts.entries()[0]["prompt"] == "a car chase at dusk"
            and prompts.entries()[0]["enhanced"] is True and len(prompts.entries()) == 2,
            str([item["prompt"] for item in prompts.entries()]))
    outbox.cancel(retried["job_id"])
    enhance.use_api(None)
    prompts.reset_for_tests()


# ------------------------------------------------------------------ the route --


def route_checks(r: Results) -> None:
    import inspect

    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    prompts.reset_for_tests()
    app = Starlette()
    routes.install(app)
    client = TestClient(app)
    older = prompts.remember("the older words", model=LTX_23, target="ltx23")
    newer = prompts.remember("the newer words", model=LTX_25, target="ltx25", enhanced=True)
    answer = client.get(routes.PROMPTS_ROUTE).json()
    r.check("GET lists them newest first, with how many it keeps",
            answer["ok"] is True and [entry["id"] for entry in answer["entries"]] == [newer["id"], older["id"]] and answer["max"] == 10
            and answer["entries"][0]["target_label"] == "LTX 2.5 Distilled", json.dumps(answer)[:300])
    history.save_draft({"prompt_override": "before", "first_asset_id": "", "last_asset_id": "", "reference_asset_ids": []})
    response = client.post(routes.PROMPTS_ROUTE, json={"action": "load", "id": older["id"]})
    answer = response.json()
    r.check("POST load answers with the words, says nothing was queued, and the draft holds them",
            response.status_code == 200 and answer["ok"] is True and answer["prompt"] == "the older words"
            and "Nothing was queued" in answer["status"] and history.load_draft()["prompt_override"] == "the older words")
    response = client.post(routes.PROMPTS_ROUTE, json={"action": "load", "id": "c" * 16})
    answer = response.json()
    r.check("a prompt that has left the list is a 404 carrying the list as it is now",
            response.status_code == 404 and answer["ok"] is False and len(answer["entries"]) == 2)
    response = client.post(routes.PROMPTS_ROUTE, json={"action": "drop"})
    r.check("anything else is refused, not guessed at", response.status_code == 400 and response.json()["code"] == errors.REQUEST_INVALID)
    source = inspect.getsource(routes._prompts)
    r.check("its file work runs off the event loop", "run_in_threadpool(prompts_action" in source)
    history.save_draft(history.empty_draft())
    prompts.reset_for_tests()


def privacy_checks(r: Results, base: pathlib.Path) -> None:
    prompts.reset_for_tests()
    secret = "a perfectly private sentence about my cat"
    entry = prompts.remember(secret, model=FL2VA, target="fl2va")
    prompts.load(entry["id"])
    outbox.cancel(outbox.submit({"prompt": secret}, PAGE, outbox.ORIGIN_CLIPBOARD, model=FL2VA)["job_id"])
    text = _journal_text(base)
    r.check("the journal says a prompt was kept and loaded, by id, and never what it said",
            f"prompt history: {entry['id'][:8]}" in text and secret not in text)
    history.save_draft(history.empty_draft())
    prompts.reset_for_tests()


# --------------------------------------------------------- the outputs' recipes --


def recipe_of_job_checks(r: Results) -> None:
    library_first, library_last, library_ref = "1" * 32, "2" * 32, "3" * 32
    job = {"job_id": "j" * 16, "origin": "clipboard", "model": dict(LTX_25),
           "request": {"request_id": "r" * 32, "prompt": "as typed", "images": {
               "start": {"kind": "clipboard_asset", "id": library_first},
               "end": {"kind": "clipboard_asset", "id": library_last}}},
           "enhance": None}
    recipe = history.recipe_of_job(job)
    r.check("a plain job's recipe: its prompt, its library pictures by id, its model",
            recipe["prompt"] == "as typed" and recipe["written"] == "" and recipe["first_asset_id"] == library_first
            and recipe["last_asset_id"] == library_last and recipe["reference_asset_ids"] == [] and recipe["transient"] == []
            and recipe["model_label"] == "LTX-2 2.5 Distilled 22B" and recipe["origin"] == "clipboard", json.dumps(recipe))
    job = dict(job, request=dict(job["request"], prompt="WRITTEN for WanGP"),
               enhance={"state": "done", "prompt_original": "as typed", "variant": "ltx25"})
    recipe = history.recipe_of_job(job)
    r.check("an enhanced job's recipe is the typed prompt, with the written one beside it",
            recipe["prompt"] == "as typed" and recipe["written"] == "WRITTEN for WanGP")
    job = {"job_id": "k" * 16, "origin": "gallery", "model": dict(FL2VA),
           "request": {"prompt": "from the popup", "images": {
               "start": {"kind": "staged", "id": "9" * 32},
               "references": [{"kind": "clipboard_asset", "id": library_ref}, {"kind": "staged", "id": "8" * 32}]}}}
    recipe = history.recipe_of_job(job)
    r.check("a picture that was not a library picture is named as gone, and a library one is kept",
            recipe["first_asset_id"] == "" and recipe["reference_asset_ids"] == [library_ref]
            and recipe["transient"] == ["start", "references"] and recipe["origin"] == "gallery", json.dumps(recipe))
    r.check("anything that is not a job has no recipe", history.recipe_of_job(None) is None and history.recipe_of_job({"job_id": "x"}) is None)


def ledger_checks(r: Results, clock: _Clock, folder: pathlib.Path) -> None:
    from test_clipboard_outputs import _wrote

    outputs.reset_for_tests()
    config.update(outputs_folder=str(folder))
    outbox.use_executor(outbox.EXECUTOR_BROWSER)
    fake = FakeApi()
    enhance.use_api(fake)
    # A browser-run job from the popup: queued in WanGP, its video written,
    # finished - and never in Queue Send History, which is the composer's.
    from minipaint_neo import interop

    frozen = interop.stage_image(Image.new("RGB", (40, 30), (9, 9, 9)))["image"]["id"]
    job = outbox.submit({"prompt": "the fox, as typed", "images": {"start": {"kind": "staged", "id": frozen}}},
                        PAGE, outbox.ORIGIN_GALLERY, model=FL2VA, enhance=True)
    fake.finish(outbox.get(job["job_id"])["enhance"]["llm_id"], "WRITTEN: a red fox crosses the snow at dusk.")
    outbox.get(job["job_id"])
    claimed = outbox.claim(PAGE)
    outbox.report(job["job_id"], claimed["lease"], "sent")
    outbox.report(job["job_id"], claimed["lease"], "done", {
        "ok": True, "status": "queued", "request_id": job["request"]["request_id"], "tasks_added": 1,
        "applied": {"prompt": True, "start": True}, "inherited": [], "ignored": [], "route": "queue",
        "model": {"type": FL2VA["type"], "label": FL2VA["label"]},
    })
    outputs.sync()
    clock.now += 60
    _wrote(folder, "fox.mp4", clock.now)
    clock.now += 10
    outbox.track(job["job_id"], PAGE, {"state": "finished", "position": None, "queue_depth": None})
    outputs.sync()
    page = routes.outputs_page()
    item = next((one for one in page["items"] if one["name"] == "fox.mp4"), None)
    r.check("a video the gallery popup asked for offers Load, from the recipe its claim kept",
            item is not None and item["recipe"] == item["id"] and not history.load_history(), json.dumps(item)[:300])
    r.check("its caption is the prompt as typed, the written one beside it, and the model",
            item is not None and item["prompt"] == "the fox, as typed" and item["written"].startswith("WRITTEN:")
            and item["model"] == "MiniMax H3 FL2VA 33B", json.dumps(item)[:300])
    facts = outputs.entry_for_file(item["id"] if item else "")
    r.check("the entry behind an output is facts about the request - no path",
            facts is not None and facts["recipe"]["prompt"] == "the fox, as typed" and "/" not in json.dumps(facts).replace("\\/", ""),
            json.dumps(facts)[:300])

    # The executor's half: a run the server finished hands over its paths and
    # the recipe while the job is still in the queue.
    clock.now += 5
    made = _wrote(folder, "ltx.mp4", clock.now)
    server_job = {"job_id": "s" * 16, "origin": "clipboard", "model": dict(LTX_25),
                  "request": {"request_id": "q" * 32, "prompt": "an LTX 2.5 prompt", "images": {}}, "enhance": None}
    outputs.remember(server_job["job_id"], [str(made)], request_id=server_job["request"]["request_id"],
                     model=LTX_25["label"], recipe=history.recipe_of_job(server_job))
    outputs.remember(server_job["job_id"], [str(made)], request_id=server_job["request"]["request_id"], model=LTX_25["label"])
    item = next((one for one in routes.outputs_page()["items"] if one["name"] == "ltx.mp4"), None)
    r.check("a run the server finished keeps the recipe the executor handed over, and a second word without one keeps it",
            item is not None and item["recipe"] == item["id"] and item["prompt"] == "an LTX 2.5 prompt"
            and item["model"] == "LTX-2 2.5 Distilled 22B", json.dumps(item)[:300])
    raw = config.read_document(config.OUTPUTS_NAME, {})
    kept = [entry for entry in raw.get("entries", []) if entry.get("job_id") == server_job["job_id"]]
    r.check("and it is on disk, so it outlives the job and a restart",
            kept and kept[0]["recipe"]["prompt"] == "an LTX 2.5 prompt")

    # The executor itself: what it hands the ledger when a run completes.
    clock.now += 5
    finished = _wrote(folder, "finished.mp4", clock.now)
    executor._remember_outputs({"job_id": "t" * 16, "origin": "clipboard", "model": dict(LTX_25),
                                "request": {"request_id": "p" * 32, "prompt": "typed for 2.5", "images": {}},
                                "enhance": {"state": "done", "prompt_original": "typed for 2.5", "variant": "ltx25"}},
                               {"generated_files": [str(finished)]})
    item = next((one for one in routes.outputs_page()["items"] if one["name"] == "finished.mp4"), None)
    r.check("a run the server completes is handed to the ledger with its recipe, so it loads with nobody having watched it",
            item is not None and item["recipe"] == item["id"] and item["prompt"] == "typed for 2.5", json.dumps(item)[:300])

    # An exact entry opened with no recipe takes one at the next sync, while
    # its job is still in the queue to take it from.
    clock.now += 5
    late = _wrote(folder, "late.mp4", clock.now)
    job = outbox.submit({"prompt": "late words"}, PAGE, outbox.ORIGIN_CLIPBOARD, model=LTX_23)
    outputs.remember(job["job_id"], [str(late)], request_id=job["request"]["request_id"])
    before = outputs.entry_for_file(next(one["id"] for one in outputs.files(refresh=False) if one["name"] == "late.mp4"))
    outputs.sync()
    after = outputs.entry_for_file(next(one["id"] for one in outputs.files(refresh=False) if one["name"] == "late.mp4"))
    r.check("an entry opened without a recipe takes its job's at the next sync",
            before["recipe"] is None and after["recipe"] is not None and after["recipe"]["prompt"] == "late words")
    outbox.cancel(job["job_id"])
    enhance.use_api(None)
    outputs.reset_for_tests()


def tab_load_checks(r: Results, library, folder: pathlib.Path, clock: _Clock) -> None:
    """The tab's half: ``output:<file id>`` through the box the Load button writes."""
    import forge_like  # noqa: F401  (Forge's patches, before any Gradio component)
    from minipaint_neo.clipboard import ui as clipboard_ui
    from test_clipboard_outputs import _wrote

    outputs.reset_for_tests()
    config.update(outputs_folder=str(folder))
    tab = clipboard_ui.ClipboardTab()
    first = _asset(library, "ltx-first.png", (210, 30, 30))
    last = _asset(library, "ltx-last.png", (30, 210, 30))
    gone = _asset(library, "gone.png", (30, 30, 210))
    queued_before = len(outbox.jobs())

    def ledger(name, recipe=None, request_id=""):
        clock.now += 3
        path = _wrote(folder, name, clock.now)
        outputs.remember("x" + name[:15].ljust(15, "0"), [str(path)], request_id=request_id, model="", recipe=recipe)
        return next(one["id"] for one in outputs.files(refresh=False) if one["name"] == name)

    ltx = ledger("ltx25.mp4", {"prompt": "she turns and waves", "written": "WRITTEN: she turns from the window and waves.",
                               "first_asset_id": first, "last_asset_id": last, "reference_asset_ids": [],
                               "model_type": LTX_25["type"], "model_label": LTX_25["label"], "origin": "clipboard"})
    history.save_draft({"prompt_override": "something else", "first_asset_id": "", "last_asset_id": "", "reference_asset_ids": [gone]})
    prompt, card_first, card_last, card_ref, status = tab.history_action(f"output:{ltx}:123", "")
    draft = history.load_draft()
    r.check("Load on an LTX 2.5 video: the prompt as typed - not the written one - and its first and last frames",
            prompt == "she turns and waves" and draft["first_asset_id"] == first and draft["last_asset_id"] == last
            and "minipaint-clip-card-override" in card_first and "minipaint-clip-card-override" in card_last, status)
    r.check("what it did not use is Use WanGP again, as it was when it was sent",
            draft["reference_asset_ids"] == [] and "minipaint-clip-card-inherit" in card_ref)
    r.check("and it says it queued nothing - and queued nothing",
            status.startswith("Loaded the request that made this output. Nothing was queued.") and len(outbox.jobs()) == queued_before, status)

    library.delete(last)
    popup = ledger("popup.mp4", {"prompt": "from the gallery", "first_asset_id": "", "last_asset_id": last,
                                 "transient": ["start"], "model_label": LTX_23["label"], "origin": "gallery"})
    prompt, card_first, card_last, _card_ref, status = tab.history_action(f"output:{popup}:124", "")
    r.check("a picture that was never in the folder, and one deleted since, leave their slots on Use WanGP, each said",
            prompt == "from the gallery" and "minipaint-clip-card-inherit" in card_first and "minipaint-clip-card-inherit" in card_last
            and "outside the Clipboard folder" in status and "no longer in the folder" in status, status)

    record = history.add_history({"history_id": "old0000000000000", "request_id": "e" * 32, "prompt_mode": "override",
                                  "prompt_override": "an older video's words", "first_mode": "override", "first_asset_id": first,
                                  "last_mode": "inherit", "reference_mode": "inherit"})
    old = ledger("old.mp4", None, request_id="e" * 32)
    prompt, card_first, _card_last, _card_ref, status = tab.history_action(f"output:{old}:125", "")
    r.check("an output from before recipes were kept loads from its Queue Send History record, as it did",
            prompt == "an older video's words" and "minipaint-clip-card-override" in card_first, status)
    history.delete_history(record["history_id"])
    orphan = ledger("orphan.mp4", None, request_id="f" * 32)
    prompt, *_cards, status = tab.history_action(f"output:{orphan}:126", "")
    r.check("one with neither says there is nothing to load, and changes nothing",
            prompt == clipboard_ui.gr.skip() or isinstance(prompt, dict), status)
    r.check("in so many words", "No recipe was kept" in status, status)
    prompt, *_cards, status = tab.history_action("output:0000000000000000:127", "")
    r.check("an output that has gone says so", "no longer in View Outputs" in status, status)
    history.save_draft(history.empty_draft())
    outputs.reset_for_tests()


def run() -> Results:
    r = Results("clipboard prompts")
    with tempfile.TemporaryDirectory(prefix="minipaint-prompts-") as scratch:
        base = pathlib.Path(scratch)
        wangp_config.use_config_dir(base / "data")
        config.use_config_dir(base / "data")
        process_log.use_log_dir(base / "logs")
        clock = _Clock()
        store.reset_for_tests()
        outbox.reset_for_tests()
        executor.reset_for_tests()
        executor.use_thread(False)
        enhance.reset_for_tests()
        outputs.reset_for_tests()
        outbox.use_running(lambda: True)
        outputs.use_clock(clock)
        outbox.use_clock(clock)
        folder = base / "library"
        folder.mkdir()
        made = base / "outputs"
        made.mkdir()
        try:
            library = store.store()
            library.set_root(str(folder), create=True)
            store_checks(r)
            load_checks(r, library)
            press_checks(r, base)
            enhanced_checks(r)
            route_checks(r)
            privacy_checks(r, base)
            recipe_of_job_checks(r)
            ledger_checks(r, clock, made)
            tab_load_checks(r, library, made, clock)
        finally:
            outputs.use_clock(None)
            outbox.use_clock(None)
            outbox.use_running(None)
            outputs.reset_for_tests()
            outbox.reset_for_tests()
            executor.reset_for_tests()
            enhance.reset_for_tests()
            prompts.reset_for_tests()
            wangp_config.use_config_dir(None)
            config.use_config_dir(None)
            process_log.use_log_dir(None)
            store.reset_for_tests()
    return r


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
