"""The outputs ledger, and the gallery's two routes.

What this asserts a reader should be able to rely on: a job the server ran
is tied to its files exactly, by the paths WanGP itself handed back, and a
job the browser ran is tied to them by the window it was running in and
says so; a file is never claimed by two requests; a claim left open by a
Forge that was shut down mid-generation is finished on the next sync, which
is what makes the gallery still full after a restart; a file the user has
deleted stops being listed; the page is the same size and shape as the
picture grid's; and the byte route answers ranges, because a <video> that
cannot ask for the middle of a file cannot be scrubbed.
"""

from harness import Results, setup_path

setup_path()

import json  # noqa: E402
import os  # noqa: E402
import pathlib  # noqa: E402
import tempfile  # noqa: E402

from minipaint_neo.clipboard import config, outbox, outputs, routes  # noqa: E402
from minipaint_neo.wangp import config as wangp_config  # noqa: E402

PAGE_A = "a" * 16
ASSET = "0123456789abcdef0123456789abcdef"


class _Clock:
    def __init__(self):
        self.now = 1_700_000_000.0

    def __call__(self):
        return self.now


def _request(prompt="a prompt"):
    return {"prompt": prompt, "images": {"start": {"kind": "clipboard_asset", "id": ASSET}}}


def _wrote(folder: pathlib.Path, name: str, at: float, body: bytes = b"") -> pathlib.Path:
    """A file in WanGP's output folder, written at a given moment."""
    path = folder / name
    path.write_bytes(body or (name.encode("utf-8") * 8))
    import os

    os.utime(path, (at, at))
    return path


def _names(listed) -> list:
    return [item["name"] for item in listed]


# ------------------------------------------------------------------- folder --


def folder_checks(r: Results, base: pathlib.Path) -> None:
    config.update(outputs_folder="")
    r.check("with no WanGP root and no setting there is no folder, and that is not an error",
            outputs.folder() is None)
    made = base / "elsewhere"
    made.mkdir()
    config.update(outputs_folder=str(made))
    r.check("the setting is the way out for an install that keeps its outputs somewhere else",
            outputs.folder() == made)
    config.update(outputs_folder=str(base / "not there"))
    r.check("and a folder that is not there reads as none rather than throwing", outputs.folder() is None)
    config.update(outputs_folder=str(made))


# -------------------------------------------------------------- the exact way --


def exact_checks(r: Results, clock: _Clock, folder: pathlib.Path) -> None:
    """A server-run job: WanGP said which files it wrote."""
    outputs.reset_for_tests()
    config.update(outputs_folder=str(folder))
    first = _wrote(folder, "a.mp4", clock.now)
    second = _wrote(folder, "b.mp4", clock.now)
    outputs.remember("1" * 16, [str(first), str(second)], request_id="r1", model="A video model")
    listed = outputs.files(refresh=False)
    r.check("the files WanGP named are the job's, both of them, newest first",
            sorted(_names(listed)) == ["a.mp4", "b.mp4"], str(_names(listed)))
    r.check("and they are marked exact, because nothing about them was guessed",
            all(item["exact"] for item in listed))
    r.check("each is addressed by an opaque id, and the path is not in what the browser gets",
            all(len(item["id"]) == 16 for item in listed) and "/" not in json.dumps(_names(listed)))
    r.check("the id resolves to the file on the server, which is the only place it does",
            outputs.path_of(listed[0]["id"]) is not None and outputs.path_of("nope") is None)

    outputs.remember("2" * 16, [str(first)], request_id="r2")
    r.check("a file another request already claimed is not claimed twice",
            len(outputs.files(refresh=False)) == 2, str(len(outputs.files(refresh=False))))

    first.unlink()
    listed = outputs.files(refresh=False)
    r.check("a file the user deleted stops being listed rather than becoming a broken tile",
            _names(listed) == ["b.mp4"], str(_names(listed)))


# ----------------------------------------------------------- relative paths --


def _set_wangp_root(root) -> None:
    """A WanGP config naming a root, the way a finished setup leaves one."""
    wangp_config.atomic_write(
        wangp_config.config_path(),
        json.dumps(wangp_config.Config(initialized=True, wangp_root=str(root)).as_dict()),
    )


def relative_checks(r: Results, clock: _Clock, base: pathlib.Path) -> None:
    """A path WanGP spelled relative to its own directory still finds its file.

    THE FAILURE: A SUCCESSFUL JOB, A VIDEO ON THE DISK, AN EMPTY GALLERY.

    WanGP's ``save_path`` is a setting, and an install that has not
    repointed it holds the relative string ``outputs``. The paths it hands
    back are then relative to the directory WanGP runs in, which is not
    this process's - WanGP is a child started with ``cwd=<wangp root>`` and
    Forge is elsewhere. Stored verbatim, such a path stats as missing the
    first moment ``files`` looks at it, and the file is dropped as one the
    user deleted: the tab says "Nothing yet" about a video that exists.

    Only the exact half could ever carry one, because the window half
    matches files it walked ``folder()`` for and those are absolute by
    construction. Newer bridges resolve their own paths before sending
    them; this is what makes the ones already written come back.
    """
    outputs.reset_for_tests()
    root = base / "wangp-root"
    made = root / "outputs"
    made.mkdir(parents=True)
    config.update(outputs_folder=str(made))
    _set_wangp_root(root)
    relative = os.path.join("outputs", "rel.mp4")
    written = _wrote(made, "rel.mp4", clock.now)

    outputs.remember("3" * 16, [relative], request_id="r3", model="A video model")
    listed = outputs.files(refresh=False)
    r.check("a relative path is resolved against WanGP's own root, not dropped as a missing file",
            _names(listed) == ["rel.mp4"], str(_names(listed)))
    # Read defensively: when the check above fails there is nothing in the
    # list, and a test that raises there reports one failure as a crash and
    # takes every check after it down with it.
    served = outputs.path_of(listed[0]["id"]) if listed else None
    r.check("and the id behind it serves the real file", served == written, str(served))
    entries = config.read_document(config.OUTPUTS_NAME, {}).get("entries") or [{}]
    stored = ((entries[0].get("files") or [{}])[0]).get("path") or ""
    r.check("what is written down is the absolute form, so nothing downstream has to resolve it again",
            os.path.isabs(stored), stored)

    outputs.remember("4" * 16, [str(written)], request_id="r4")
    r.check("the same file under its other spelling is not claimed a second time",
            len(outputs.files(refresh=False)) == 1, str(len(outputs.files(refresh=False))))

    # What an older bridge already wrote into the document, repaired on the
    # way in rather than left to fail at each reader.
    outputs.reset_for_tests()
    config.write_document(config.OUTPUTS_NAME, {"schema": outputs.SCHEMA, "entries": [{
        "entry_id": "e" * 16, "job_id": "5" * 16, "request_id": "r5",
        "opened_at": clock.now, "closed_at": clock.now, "floor": 0.0, "exact": True,
        "model": "A video model",
        "files": [{"file_id": "f" * 16, "path": relative, "name": "rel.mp4", "size": 0, "at": clock.now}],
    }]})
    r.check("a record written before this still lists its file rather than being swept as deleted",
            _names(outputs.files(refresh=False)) == ["rel.mp4"], str(_names(outputs.files(refresh=False))))

    # And with no root to resolve against, it stays exactly as it was: a
    # wrong guess would list somebody else's video under this job.
    wangp_config.config_path().unlink()
    r.check("with no WanGP root known the path is left alone and simply does not list",
            outputs.files(refresh=False) == [], str(outputs.files(refresh=False)))
    _set_wangp_root(root)
    r.check("and comes back the moment the root is known again",
            _names(outputs.files(refresh=False)) == ["rel.mp4"], str(_names(outputs.files(refresh=False))))
    wangp_config.config_path().unlink()
    outputs.reset_for_tests()


# ------------------------------------------------------------- the window way --


def window_checks(r: Results, clock: _Clock, folder: pathlib.Path) -> None:
    """A browser-run job: the page saw WanGP take it and later let it go."""
    outputs.reset_for_tests()
    outbox.reset_for_tests()
    outbox.use_clock(clock)
    outbox.use_running(lambda: True)
    config.update(outputs_folder=str(folder))

    _wrote(folder, "before.mp4", clock.now - 500)
    job = outbox.submit(_request(), PAGE_A, "clipboard")
    claimed = outbox.claim(PAGE_A)
    outbox.report(job["job_id"], claimed["lease"], "sent")
    outbox.report(job["job_id"], claimed["lease"], "done", {
        "ok": True, "status": "queued", "request_id": job["request"]["request_id"], "tasks_added": 1,
        "applied": {"prompt": True}, "inherited": [], "ignored": [], "route": "queue",
        "model": {"type": "video", "label": "A video model"},
    })
    outputs.sync()
    r.check("a request WanGP has taken opens a claim, and claims nothing while it is open",
            outputs.files(refresh=False) == [])

    clock.now += 60
    made = _wrote(folder, "during.mp4", clock.now)
    outbox.track(job["job_id"], PAGE_A, {"state": "generating", "position": 0, "queue_depth": 0})
    outputs.sync()
    r.check("a file written while it is still running is still not claimed - the job is not over",
            outputs.files(refresh=False) == [])

    clock.now += 10
    outbox.track(job["job_id"], PAGE_A, {"state": "finished", "position": None, "queue_depth": None})
    outputs.sync()
    listed = outputs.files(refresh=False)
    r.check("and the moment the task leaves WanGP's queue the window closes over what appeared in it",
            _names(listed) == ["during.mp4"], str(_names(listed)))
    r.check("a file that was there before the request is never claimed by it",
            "before.mp4" not in _names(listed))
    r.check("and the match says it is a match: exact is false, so the gallery can say so",
            listed and not listed[0]["exact"])
    r.check("the file is still tied to the job that made it", listed[0]["job_id"] == job["job_id"])


def restart_checks(r: Results, clock: _Clock, folder: pathlib.Path) -> None:
    """A claim left open by a Forge that went away mid-generation."""
    outputs.reset_for_tests()
    outbox.reset_for_tests()
    outbox.use_clock(clock)
    outbox.use_running(lambda: True)
    config.update(outputs_folder=str(folder))
    job = outbox.submit(_request("shut down under it"), PAGE_A, "clipboard")
    claimed = outbox.claim(PAGE_A)
    outbox.report(job["job_id"], claimed["lease"], "sent")
    outbox.report(job["job_id"], claimed["lease"], "done", {
        "ok": True, "status": "queued", "request_id": job["request"]["request_id"], "tasks_added": 1,
        "applied": {"prompt": True}, "inherited": [], "ignored": [], "route": "queue",
        "model": {"type": "video", "label": "A video model"},
    })
    outputs.sync()
    r.check("the claim is open and empty", outputs.files(refresh=False) == [])

    # Forge goes away here. WanGP finishes; the file lands; nothing is told.
    clock.now += 120
    _wrote(folder, "while-forge-was-off.mp4", clock.now)
    # The queue's own sweep takes the finished job away, as it does after a
    # restart - so the claim now has no job at all to ask about.
    clock.now += outbox.KEEP_TERMINAL_SECONDS + 1
    outbox.jobs()
    r.check("and the job it belonged to is gone from the queue", outbox.get(job["job_id"]) is None)

    outputs.sync()
    listed = outputs.files(refresh=False)
    r.check("the next sync closes the orphaned claim over the file that appeared, so a restart does not lose the video",
            _names(listed) == ["while-forge-was-off.mp4"], str(_names(listed)))
    r.check("and it is still tied to the request that asked for it, long after the job stopped existing",
            listed and listed[0]["job_id"] == job["job_id"])

    # The document is the thing that survives, so read it back from disk with
    # nothing in memory: this is the whole promise of "closing the WebUI
    # should not empty this gallery".
    outbox.reset_for_tests()
    reread = outputs.files(refresh=False)
    r.check("and it is read back from the document alone, with no job and no queue left to consult",
            _names(reread) == ["while-forge-was-off.mp4"], str(_names(reread)))


# ------------------------------------------------------------------- the page --


def page_checks(r: Results, clock: _Clock, folder: pathlib.Path) -> None:
    outputs.reset_for_tests()
    config.update(outputs_folder=str(folder))
    made = []
    for index in range(65):
        clock.now += 1
        made.append(_wrote(folder, f"v{index:03d}.mp4", clock.now))
    outputs.remember("3" * 16, [str(path) for path in made], request_id="r3")

    page = routes.outputs_page()
    r.check("a page is 60, the same as the picture grid's, so there is one pager to learn",
            page["size"] == routes.PAGE_SIZE == 60 and len(page["items"]) == 60, str(len(page["items"])))
    r.check("and it counts the whole ledger, not the page", page["total"] == 65 and page["pages"] == 2)
    r.check("newest first", page["items"][0]["name"] == "v064.mp4", page["items"][0]["name"])
    second = routes.outputs_page(page=1)
    r.check("the second page has the rest", len(second["items"]) == 5 and second["page"] == 1)
    r.check("a page past the end is answered with the last one, not refused",
            routes.outputs_page(page=99)["page"] == 1)
    r.check("every item carries its own address, and no path", all(
        item["url"].startswith("/minipaint-clipboard/output/") and "/" not in item["name"] for item in page["items"]))
    r.check("what the browser is given names no folder anywhere in it",
            str(folder) not in json.dumps(page))


def recipe_checks(r: Results, clock: _Clock, folder: pathlib.Path) -> None:
    """Load from View Outputs: an output names the history record of the
    request that made it, so the gallery can hand that record to the same
    Load the Queue Send History uses. An output with no record has no recipe,
    and says so by having none."""
    from minipaint_neo.clipboard import history

    kept = "a" * 32
    gone = "b" * 32
    outputs.reset_for_tests()
    config.update(outputs_folder=str(folder))
    clock.now += 1
    made = _wrote(folder, "made.mp4", clock.now)
    clock.now += 1
    stray = _wrote(folder, "stray.mp4", clock.now)
    outputs.remember("4" * 16, [str(made)], request_id=kept)
    outputs.remember("5" * 16, [str(stray)], request_id=gone)
    record = history.add_history({
        "history_id": "recipe0000000000", "request_id": kept, "prompt_mode": "override",
        "prompt_override": "a fox in the snow", "first_mode": "inherit", "last_mode": "inherit",
        "reference_mode": "inherit"})
    try:
        by_name = {item["name"]: item for item in routes.outputs_page()["items"]}
        r.check("an output whose request is in the history names that record, for Load",
                by_name["made.mp4"]["recipe"] == record["history_id"], str(by_name["made.mp4"]))
        r.check("and carries its prompt beside it, as it did",
                by_name["made.mp4"]["prompt"] == "a fox in the snow", str(by_name["made.mp4"]))
        r.check("an output whose request left no record names none, so the gallery can say so",
                by_name["stray.mp4"]["recipe"] == "", str(by_name["stray.mp4"]))
        r.check("the recipe is an id and nothing else - no prompt, path or picture rides on it",
                all(isinstance(item["recipe"], str) and "/" not in item["recipe"] for item in by_name.values()))
    finally:
        history.delete_history(record["history_id"])


# ------------------------------------------------------------------ the bytes --


def range_checks(r: Results) -> None:
    r.check("a plain range is understood", routes._byte_range("bytes=0-99", 1000) == (0, 99))
    r.check("an open-ended one runs to the end", routes._byte_range("bytes=500-", 1000) == (500, 999))
    r.check("a suffix range is the last bytes", routes._byte_range("bytes=-50", 1000) == (950, 999))
    r.check("an end past the file is clamped rather than refused", routes._byte_range("bytes=900-5000", 1000) == (900, 999))
    for bad in ("", "junk", "bytes=abc-def", "bytes=2000-3000", "bytes=50-10", "bytes=0-9,20-29"):
        r.check(f"{bad or 'nothing'} is answered whole rather than half-understood",
                routes._byte_range(bad, 1000) is None)


def route_checks(r: Results, folder: pathlib.Path, clock: _Clock) -> None:
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    outputs.reset_for_tests()
    config.update(outputs_folder=str(folder))
    clock.now += 10
    made = _wrote(folder, "served.mp4", clock.now, b"0123456789" * 40)
    outputs.remember("4" * 16, [str(made)], request_id="r4")
    file_id = outputs.files(refresh=False)[0]["id"]

    app = FastAPI()
    routes.install(app)
    client = TestClient(app)

    answer = client.get(routes.OUTPUTS_ROUTE)
    r.check("the outputs index is behind the sign-in gate like every other route of this tab",
            answer.status_code in (200, 401), str(answer.status_code))
    if answer.status_code == 401:
        return
    r.check("it answers the page", answer.json()["total"] == 1)

    whole = client.get(f"/minipaint-clipboard/output/{file_id}")
    r.check("a whole file comes back with the length and the type",
            whole.status_code == 200 and len(whole.content) == 400 and whole.headers["content-type"].startswith("video/"),
            str(whole.status_code))
    r.check("and says it takes ranges, which is how a <video> knows it may seek",
            whole.headers.get("accept-ranges") == "bytes")

    part = client.get(f"/minipaint-clipboard/output/{file_id}", headers={"Range": "bytes=10-19"})
    r.check("a range comes back 206, with only those bytes",
            part.status_code == 206 and part.content == b"0123456789", str(part.status_code))
    r.check("and says which bytes they were, out of how many - without this a scrub bar does nothing",
            part.headers.get("content-range") == "bytes 10-19/400", str(part.headers.get("content-range")))

    head = client.head(f"/minipaint-clipboard/output/{file_id}")
    r.check("a HEAD gives the size and no body", head.status_code == 200 and head.headers.get("content-length") == "400")

    gone = client.get("/minipaint-clipboard/output/deadbeefdeadbeef")
    r.check("an id that is not one is a 404, not a path anybody can steer", gone.status_code == 404)
    r.check("and the refusal names no folder", str(folder) not in gone.text)

    # -- and none of it on the event loop ---------------------------------
    #
    # Reported as choppy video and a slow thumbnail strip. These routes read
    # files - a four-megabyte video chunk, a Pillow decode for a thumbnail
    # that is not cached yet - and a blocking read inside a coroutine blocks
    # the event loop, which on this server is every other request there is:
    # the rest of the gallery, Forge's own streams, the interop spine. A
    # video is a lot of chunks, so it is a lot of everybody else waiting.
    #
    # Starlette runs a plain `def` endpoint in a threadpool and awaits a
    # coroutine one on the loop, so the signature IS the fix and asserting it
    # is asserting the behaviour.
    import inspect

    from minipaint_neo.clipboard import routes as routes_module

    r.check("serving an output is not a coroutine, so Starlette gives it a worker thread",
            not inspect.iscoroutinefunction(routes_module._output_file))
    r.check("and neither is serving a picture or its thumbnail",
            not inspect.iscoroutinefunction(routes_module._image))
    # A whole file is streamed rather than read into memory first: a video
    # answered without a range used to be its own size in RAM per request,
    # with nothing on the wire until the last byte had been read.
    source = inspect.getsource(routes_module._output_file)
    r.check("a whole file is streamed, not gathered in memory and then sent",
            "FileResponse(path" in source and "data = handle.read()" not in source, source[:200])


def run() -> Results:
    r = Results("clipboard outputs")
    with tempfile.TemporaryDirectory(prefix="minipaint-outputs-") as scratch:
        base = pathlib.Path(scratch)
        wangp_config.use_config_dir(base / "data")
        config.use_config_dir(base / "data")
        folder = base / "outputs"
        folder.mkdir()
        clock = _Clock()
        outputs.reset_for_tests()
        outbox.reset_for_tests()
        outputs.use_clock(clock)
        outbox.use_clock(clock)
        outbox.use_running(lambda: True)
        try:
            folder_checks(r, base)
            folders = {}
            for name in ("exact", "window", "restart", "page", "route", "recipe"):
                folders[name] = base / name
                folders[name].mkdir()
            exact_checks(r, clock, folders["exact"])
            relative_checks(r, clock, base)
            window_checks(r, clock, folders["window"])
            restart_checks(r, clock, folders["restart"])
            page_checks(r, clock, folders["page"])
            recipe_checks(r, clock, folders["recipe"])
            range_checks(r)
            route_checks(r, folders["route"], clock)
        finally:
            outputs.use_clock(None)
            outbox.use_clock(None)
            outbox.use_running(None)
            outputs.reset_for_tests()
            outbox.reset_for_tests()
            wangp_config.use_config_dir(None)
            config.use_config_dir(None)
    return r


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
