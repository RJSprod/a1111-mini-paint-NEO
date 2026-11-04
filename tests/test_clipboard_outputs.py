"""The outputs ledger, and the gallery's two routes.

What this asserts a reader should be able to rely on: a job the server ran
is tied to its files exactly, by the paths WanGP itself handed back, and a
job the browser ran is tied to them by the window it was running in and
says so; a file is never claimed by two requests; a claim left open by a
Forge that was shut down mid-generation is finished on the next sync, which
is what makes the gallery still full after a restart; a file the user has
deleted stops being listed; a page holds sixty, the size the picture
grid's pages were; and the byte route answers ranges, because a <video> that
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
    r.check("a page is 60 (routes.PAGE_SIZE), the size the picture grid's pages were, so the strip turns as it always did",
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


def folder_gallery_checks(r: Results, clock: _Clock, folder: pathlib.Path) -> None:
    """Every video in WanGP's output folder, not only this tab's.

    Asked for: "i want the 'view outputs' videos to actually be all the videos
    in wan2gp output folder, not just those that we requested through
    clipboard". The ledger keeps what it always kept - a request's own files,
    with their recipes - and the rest of the folder is listed beside it, each
    file once, newest first, by an id that names nothing.
    """
    outputs.reset_for_tests()
    config.update(outputs_folder=str(folder))
    nested = folder / "ltx2"
    nested.mkdir()
    clock.now += 10
    mine = _wrote(folder, "mine.mp4", clock.now + 1)
    # Claimed under a spelling of its own, as WanGP's answer may have it (a
    # relative save path rooted at its folder, a drive letter in another case
    # on Windows): the walk finds the same file as ".../mine.mp4", and it is
    # still one file. ``..`` is a spelling pathlib keeps as it is.
    outputs.remember("5" * 16, [str(folder / "ltx2" / ".." / "mine.mp4")], request_id="r5")
    _wrote(nested, "deeper.mkv", clock.now + 2)
    _wrote(folder, "theirs.mp4", clock.now + 3)
    _wrote(folder, "still.png", clock.now + 4)
    elsewhere = folder.parent / "elsewhere-video.mp4"
    elsewhere.write_bytes(b"not WanGP's" * 4)
    try:
        os.symlink(elsewhere, folder / "linked.mp4")
        linked = True
    except (OSError, NotImplementedError):
        linked = False

    listed = outputs.gallery(refresh=False)
    r.check("every video in the folder is listed, the ones made here and the ones made in WanGP, newest first",
            _names(listed) == ["theirs.mp4", "deeper.mkv", "mine.mp4"], str(_names(listed)))
    r.check("a video in one of WanGP's own subfolders is there too", "deeper.mkv" in _names(listed))
    r.check("a picture beside them is not: the view is the videos", "still.png" not in _names(listed))
    if linked:
        r.check("nor a link to a file elsewhere, which is not WanGP's", "linked.mp4" not in _names(listed))
    by_name = {item["name"]: item for item in listed}
    r.check("a file this tab asked for is the ledger's, with its request, and is listed once however its path was spelled",
            _names(listed).count("mine.mp4") == 1 and (by_name.get("mine.mp4") or {}).get("source") == "request"
            and (by_name.get("mine.mp4") or {}).get("request_id") == "r5"
            and not (by_name.get("mine.mp4") or {}).get("id", "wf").startswith(outputs.FOLDER_ID_PREFIX),
            str(by_name.get("mine.mp4")))
    folder_items = [by_name.get("theirs.mp4") or {}, by_name.get("deeper.mkv") or {}]
    r.check("a video only the folder knows says so, with no request, no recipe and no guess to mark",
            all(item.get("source") == "folder" and not item.get("request_id") and item.get("recipe") is None
                and str(item.get("id", "")).startswith(outputs.FOLDER_ID_PREFIX) for item in folder_items), str(folder_items))
    r.check("and its id names nothing - not the file, not the folders above it",
            all("theirs" not in item.get("id", "") and "ltx2" not in item.get("id", "") and len(item.get("id", "")) == 18
                for item in folder_items))
    r.check("the same video has the same id every time it is listed",
            [item["id"] for item in outputs.gallery(refresh=False)] == [item["id"] for item in listed])
    theirs_id = (by_name.get("theirs.mp4") or {}).get("id", "")
    r.check("and the id opens the file", outputs.path_of(theirs_id) == folder / "theirs.mp4", str(outputs.path_of(theirs_id)))

    # A restart: the listing is gone, a page still holds the id.
    outputs.forget_listing_for_tests()
    r.check("after a restart an id from the page before it still opens the file",
            outputs.path_of(theirs_id) == folder / "theirs.mp4", str(outputs.path_of(theirs_id)))
    r.check("an id that was never issued opens nothing",
            outputs.path_of(outputs.FOLDER_ID_PREFIX + "0" * 16) is None and outputs.path_of("wf../../etc") is None)
    (folder / "theirs.mp4").unlink()
    r.check("and a video deleted since is not opened", outputs.path_of(theirs_id) is None)

    page = routes.outputs_page()
    r.check("the page says where the list comes from, and each item which kind it is",
            "WanGP's output folder" in page["status"] and {item["source"] for item in page["items"]} == {"folder", "request"},
            str((page["status"], [item["source"] for item in page["items"]])))
    r.check("and names no folder anywhere in what the browser is given", str(folder) not in json.dumps(page))
    if linked:
        (folder / "linked.mp4").unlink()


def recipe_checks(r: Results, clock: _Clock, folder: pathlib.Path) -> None:
    """Load from View Outputs, for an output written before the ledger kept
    recipes: it offers Load when the history record of the request that made
    it is still there - the tab hands that record to the same Load the Queue
    Send History uses - and an output with no record has no recipe, and says
    so by having none. (``own_recipe_checks`` is the ledger's own recipe.)"""
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
        r.check("an output whose request is in the history offers Load, by its own id, which the tab resolves to that record",
                by_name["made.mp4"]["recipe"] == by_name["made.mp4"]["id"], str(by_name["made.mp4"]))
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


def settings_route_checks(r: Results, folder: pathlib.Path, clock: _Clock) -> None:
    """Send to WanGP's first half: a note naming one output, for the bridge.

    Asked for: "the ability to send a video from this view to wan2gp to load
    its settings ... a new send button in the playback controls bar". The
    browser holds an output's id and nothing else; the server answers with the
    id of a note in the handoff root naming the file, which the WanGP page's
    bridge reads through WanGP's own loader. A video WanGP keeps no settings
    in is refused here, and no answer names a folder or a file.
    """
    import inspect

    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from minipaint_neo.wangp import handoff, protocol as wangp_protocol

    outputs.reset_for_tests()
    config.update(outputs_folder=str(folder))
    clock.now += 10
    requested = _wrote(folder, "requested here.mp4", clock.now + 1)
    outputs.remember("6" * 16, [str(requested)], request_id="r6")
    from_wangp = _wrote(folder, "made in wangp.mkv", clock.now + 2)
    _wrote(folder, "a clip.webm", clock.now + 3)
    _wrote(folder, "another.m4v", clock.now + 4)
    listed = {item["name"]: item for item in outputs.gallery(refresh=False)}
    app = FastAPI()
    routes.install(app)
    client = TestClient(app)

    def route(output_id):
        return f"/minipaint-clipboard/output/{output_id}/settings"

    def named(answer):
        try:
            return pathlib.Path(json.loads((handoff.handoff_root() / (answer.json()["handoff_id"] + wangp_protocol.HANDOFF_REFERENCE_SUFFIX))
                                           .read_text(encoding="utf-8"))["path"])
        except Exception as error:  # the check below says what was wrong
            return error

    first = client.post(route(listed["requested here.mp4"]["id"]))
    if first.status_code == 401:
        r.check("the settings route is behind the sign-in gate (signed-out run)", True)
        return
    answers = [first]
    r.check("a video this tab asked for answers with a note's id",
            first.status_code == 200 and first.json().get("ok") is True and wangp_protocol.valid_handoff_id(first.json().get("handoff_id")),
            first.text[:200])
    r.check("and the note names that very file", named(first) == requested.resolve() or named(first) == requested, str(named(first)))
    second = client.post(route(listed["made in wangp.mkv"]["id"]))
    answers.append(second)
    r.check("so does a video only WanGP's folder knows, an MKV among them",
            second.status_code == 200 and (named(second) == from_wangp.resolve() or named(second) == from_wangp), second.text[:200])
    again = client.post(route(listed["requested here.mp4"]["id"]))
    answers.append(again)
    r.check("each press is a note of its own", again.status_code == 200 and again.json().get("handoff_id") != first.json().get("handoff_id"))

    before = {child.name for child in handoff.handoff_root().iterdir()}
    for name in ("a clip.webm", "another.m4v"):
        refused = client.post(route(listed[name]["id"]))
        answers.append(refused)
        r.check(f"{name.rsplit('.', 1)[1].upper()}, which WanGP keeps no settings in, is refused 409 OUTPUT_NO_SETTINGS",
                refused.status_code == 409 and refused.json().get("code") == "OUTPUT_NO_SETTINGS", refused.text[:200])
    unknown = client.post(route("deadbeefdeadbeef"))
    never = client.post(route(outputs.FOLDER_ID_PREFIX + "0" * 16))
    answers.extend([unknown, never])
    r.check("an id that is not an output's is a 404, wherever it claims to be from",
            unknown.status_code == 404 and never.status_code == 404 and unknown.json().get("code") == "CLIPBOARD_ASSET_UNKNOWN")
    looked = client.get(route(listed["requested here.mp4"]["id"]))
    r.check("a note is written by a POST alone", looked.status_code == 405, str(looked.status_code))
    r.check("and none of the refusals left a note behind",
            {child.name for child in handoff.handoff_root().iterdir()} == before)
    r.check("no answer names a folder or a file",
            not any(str(folder) in answer.text or "requested" in answer.text or "wangp.mkv" in answer.text for answer in answers))
    r.check("writing the note is not a coroutine, so it is off the event loop",
            not inspect.iscoroutinefunction(routes._output_settings))
    (folder / "made in wangp.mkv").unlink()
    gone = client.post(route(listed["made in wangp.mkv"]["id"]))
    r.check("a video deleted since is a 404", gone.status_code == 404, str(gone.status_code))


def whole_listing_checks(r: Results, clock: _Clock, folder: pathlib.Path) -> None:
    """The gallery's one listing: every output, as an archive is listed.

    Asked for: "The gallery should work much like the clipboard archives and
    load as I slide into video." So the gallery is not paged: one answer
    carries every output, newest first, and the page draws all of them and
    asks for a poster only in view. What a folder video does not have is left
    out of its item rather than sent as empty strings, because a folder of
    twenty thousand videos is otherwise mostly empty strings; and each item
    carries the version its poster's address is cached against.
    """
    outputs.reset_for_tests()
    config.update(outputs_folder=str(folder))
    clock.now += 10
    mine = _wrote(folder, "mine.mp4", clock.now + 1)
    outputs.remember("7" * 16, [str(mine)], request_id="r7", model="A video model",
                     recipe={"prompt": "a cat in the rain", "model_label": "A video model"})
    for index in range(75):
        _wrote(folder, f"folder{index:03d}.mp4", clock.now - 1000 - index)

    whole = routes.outputs_page(whole=True)
    r.check("the whole listing is one answer with every output in it, where a page holds sixty",
            whole["whole"] is True and whole["total"] == 76 and len(whole["items"]) == 76 and "page" not in whole,
            str({key: whole.get(key) for key in ("total", "whole", "page")}))
    r.check("newest first, as the pages were", whole["items"][0]["name"] == "mine.mp4"
            and whole["items"][1]["name"] == "folder000.mp4" and whole["items"][-1]["name"] == "folder074.mp4",
            str([item["name"] for item in whole["items"][:3]]))
    r.check("and nothing was left out", whole["truncated"] == 0)
    folder_item = whole["items"][1]
    r.check("a folder video's item carries only what it has: no prompt, model or recipe, not even empty",
            set(folder_item) == {"id", "name", "kind", "size", "at", "v", "source"}, str(sorted(folder_item)))
    request_item = whole["items"][0]
    r.check("a request's item keeps what it has: its model, its recipe, whether WanGP named the file",
            request_item.get("model") == "A video model" and request_item.get("recipe") == request_item["id"]
            and request_item.get("prompt") == "a cat in the rain" and "written" not in request_item
            and request_item.get("exact") is True and request_item["source"] == "request", str(request_item))
    info = mine.stat()
    r.check("every item carries its file's version - its time and size - for the poster's address",
            request_item["v"] == f"{int(round(info.st_mtime * 1000))}-{info.st_size}", request_item["v"])
    r.check("the address every item's file and poster are under is said once, by id and nothing else",
            whole["prefix"] == "/minipaint-clipboard/output/" and str(folder) not in json.dumps(whole))
    paged = routes.outputs_page()
    r.check("the paged answer is still there for a page loaded before, sixty at a time, with the version too",
            paged["pages"] == 2 and len(paged["items"]) == 60 and all(item.get("v") for item in paged["items"]),
            str({key: paged.get(key) for key in ("pages", "size")}))
    original = routes.OUTPUTS_WHOLE_LIMIT
    routes.OUTPUTS_WHOLE_LIMIT = 50
    try:
        capped = routes.outputs_page(whole=True)
    finally:
        routes.OUTPUTS_WHOLE_LIMIT = original
    r.check("past its limit the listing stops and says how many it left out",
            len(capped["items"]) == 50 and capped["truncated"] == 26, str((len(capped["items"]), capped["truncated"])))


def _clip(path: pathlib.Path, seconds: float = 2.0, size=(320, 180), rate: int = 12) -> bool:
    """A real video, written with PyAV (Forge Neo's own decoder): a coloured
    frame a step brighter every frame, so the poster's moment is knowable. False
    where PyAV is not installed."""
    try:
        import av
        import numpy
    except ImportError:
        return False
    with av.open(str(path), "w") as container:
        stream = container.add_stream("mpeg4", rate=rate)
        stream.width, stream.height = size
        stream.pix_fmt = "yuv420p"
        for index in range(int(seconds * rate)):
            frame = numpy.zeros((size[1], size[0], 3), dtype=numpy.uint8)
            frame[:, :, 0] = min(255, index * 8)
            frame[:, :, 2] = 120
            for packet in stream.encode(av.VideoFrame.from_ndarray(frame, format="rgb24")):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return True


def poster_checks(r: Results, folder: pathlib.Path, clock: _Clock) -> None:
    """A poster for every output: one frame, made once, kept three times.

    What has to be true: the route answers a small picture of the video - its
    first frame, scaled to the poster's side - by the output's opaque id; says how long the clip is; answers ``immutable`` only for the
    file's current version; makes it once (memory, then disk, then a decode);
    is off the event loop; and says plainly when this Forge has no decoder at
    all (501, so the page draws its posters itself) or this file gave no frame
    (422, so its tile is marked).
    """
    import inspect
    import io

    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from minipaint_neo.clipboard import posters

    outputs.reset_for_tests()
    posters.reset_for_tests()
    config.update(outputs_folder=str(folder))
    clock.now += 10
    real = folder / "real.mp4"
    # Twice the poster's side, so the poster has to be made smaller.
    have_av = _clip(real, seconds=2.0, size=(960, 540))
    if have_av:
        os.utime(real, (clock.now + 2, clock.now + 2))
    broken = _wrote(folder, "broken.mp4", clock.now + 1, b"not a video at all" * 20)
    still = folder / "still.png"
    from PIL import Image

    Image.new("RGB", (1200, 600), (10, 200, 30)).save(still)
    outputs.remember("8" * 16, [str(still)], request_id="r8")

    app = FastAPI()
    routes.install(app)
    client = TestClient(app)
    listed = {item["name"]: item for item in routes.outputs_page(whole=True)["items"]}

    def poster(name, version=None):
        item = listed[name]
        wanted = item["v"] if version is None else version
        return client.get(f"/minipaint-clipboard/output/{item['id']}/poster" + (f"?v={wanted}" if wanted else ""))

    r.check("making a poster is not a coroutine, so a decode never stops the server",
            not inspect.iscoroutinefunction(routes._output_poster))

    picture = poster("still.png")
    r.check("a picture's poster is the picture made small, a WebP at the poster's side",
            picture.status_code == 200 and picture.headers["content-type"] == "image/webp"
            and Image.open(io.BytesIO(picture.content)).size == (posters.POSTER_SIDE, posters.POSTER_SIDE // 2),
            f"{picture.status_code} {picture.headers.get('content-type')}")

    if not have_av:
        r.check("(PyAV is not installed here, so a real video's poster is not decoded)", True)
    else:
        made = posters.posters().made
        answer = poster("real.mp4")
        r.check("a video's poster is a frame of it, a small WebP, by the output's id",
                answer.status_code == 200 and answer.headers["content-type"] == "image/webp"
                and 0 < len(answer.content) < 40000, f"{answer.status_code} {len(answer.content)}")
        frame = Image.open(io.BytesIO(answer.content)).convert("RGB")
        r.check("scaled to fit the poster's side, its shape kept", frame.size == (480, 270), str(frame.size))
        red, _green, blue = frame.getpixel((frame.width // 2, frame.height // 2))
        # Asked for: "I would like each video to use its first frame as a
        # thumbnail". Frame 0 is red 0 and blue 120; frame 12, the one second
        # the poster used to be taken at, is red 96. A codec moves a flat
        # colour a little either way.
        r.check("it is the video's first frame", red <= 30 and 100 <= blue <= 140, str((red, blue)))
        r.check("and it says how long the clip is, for the tile",
                abs(float(answer.headers.get("x-minipaint-duration", "0")) - 2.0) < 0.2,
                str(answer.headers.get("x-minipaint-duration")))
        r.check("the current version earns the immutable header, so the browser keeps it for a year",
                answer.headers.get("cache-control") == routes.IMMUTABLE_CACHE, str(answer.headers.get("cache-control")))
        r.check("one decode", posters.posters().made == made + 1)
        again = poster("real.mp4")
        r.check("asked again, it is the same poster from memory, decoded no second time",
                again.content == answer.content and posters.posters().made == made + 1)
        posters.reset_for_tests()
        from_disk = poster("real.mp4")
        r.check("and after a restart from the disk cache, still decoded no second time",
                from_disk.content == answer.content and posters.posters().made == 0
                and abs(float(from_disk.headers.get("x-minipaint-duration", "0")) - 2.0) < 0.2,
                f"made {posters.posters().made}, {from_disk.headers.get('x-minipaint-duration')}")
        cached = list((config.config_dir() / posters.POSTER_DIR_NAME).iterdir())
        r.check("the disk cache is in this extension's data folder and names no file of WanGP's",
                cached and all("real" not in path.name for path in cached), str([path.name for path in cached]))
        r.check("a made-up version is answered, but never as immutable",
                poster("real.mp4", version="1-2").headers.get("cache-control") == routes.REVALIDATED_CACHE)
        r.check("and so is no version at all",
                poster("real.mp4", version="").headers.get("cache-control") == routes.REVALIDATED_CACHE)
        # The file changed: a new version, and a poster made from the new file.
        _clip(real, seconds=1.0)
        os.utime(real, (clock.now + 30, clock.now + 30))
        listed = {item["name"]: item for item in routes.outputs_page(whole=True)["items"]}
        changed = poster("real.mp4")
        r.check("a file that changed is a new version, and its poster is made again from the new file",
                changed.status_code == 200 and abs(float(changed.headers.get("x-minipaint-duration", "0")) - 1.0) < 0.2,
                str(changed.headers.get("x-minipaint-duration")))

    unreadable = poster("broken.mp4")
    if have_av:
        r.check("a file that gives no frame is 422 OUTPUT_POSTER_UNREADABLE, so its tile is marked and nothing else",
                unreadable.status_code == 422 and unreadable.json().get("code") == "OUTPUT_POSTER_UNREADABLE",
                f"{unreadable.status_code} {unreadable.text[:120]}")
        r.check("and the refusal names no file", "broken" not in unreadable.text and str(folder) not in unreadable.text)

    posters.reset_for_tests("")
    try:
        none = poster("broken.mp4")
        r.check("a Forge with no video decoder at all says so once, 501, so the page draws its posters itself",
                none.status_code == 501 and none.json().get("code") == "OUTPUT_POSTER_UNAVAILABLE",
                f"{none.status_code} {none.text[:120]}")
        r.check("while a picture's poster is still made, needing no video decoder",
                poster("still.png").status_code == 200)
    finally:
        posters.reset_for_tests()
    gone = client.get("/minipaint-clipboard/output/deadbeefdeadbeef/poster")
    r.check("an id that is no output is a 404", gone.status_code == 404, str(gone.status_code))
    poster_upload_checks(r, client, folder, listed, broken)


def poster_upload_checks(r: Results, client, folder: pathlib.Path, listed: dict, broken: pathlib.Path) -> None:
    """A first frame the browser took itself, given back to be kept.

    The server could not make this poster - no decoder at all here, or one
    that will not read the file - and the browser, which plays the video, can.
    What it gives back is kept as a poster made here is: checked, made small,
    encoded again, under the file's current version only, and answered from
    then on whatever the server can decode.
    """
    import io

    from PIL import Image

    from minipaint_neo.clipboard import posters

    item = listed["broken.mp4"]
    address = f"/minipaint-clipboard/output/{item['id']}/poster"
    frame = io.BytesIO()
    Image.new("RGB", (1280, 720), (12, 200, 40)).save(frame, format="JPEG", quality=90)
    posters.reset_for_tests("")
    try:
        r.check("(with no decoder here, the server cannot make this one itself)",
                client.get(address + f"?v={item['v']}").status_code == 501)
        stale = client.put(address + "?v=1-2", content=frame.getvalue(), headers={"Content-Type": "image/jpeg"})
        r.check("a frame given for a version of the file that is not the one on disk is refused, 409",
                stale.status_code == 409 and stale.json().get("code") == "OUTPUT_POSTER_STALE", stale.text[:120])
        junk = client.put(address + f"?v={item['v']}", content=b"not a picture" * 10, headers={"Content-Type": "image/jpeg"})
        r.check("and one that is not a picture, 415, keeping nothing",
                junk.status_code == 415 and junk.json().get("code") == "OUTPUT_POSTER_INVALID"
                and client.get(address + f"?v={item['v']}").status_code == 501, junk.text[:120])
        huge = client.put(address + f"?v={item['v']}", content=b"0" * (posters.UPLOAD_MAX_BYTES + 1),
                          headers={"Content-Type": "image/jpeg"})
        r.check("and one larger than a poster could be, 413, unread", huge.status_code == 413, str(huge.status_code))
        given = client.put(address + f"?v={item['v']}&duration=3.5", content=frame.getvalue(),
                           headers={"Content-Type": "image/jpeg"})
        r.check("a first frame the browser took is kept", given.status_code == 200 and given.json() == {"ok": True, "kept": True},
                given.text[:120])
        back = client.get(address + f"?v={item['v']}")
        kept = Image.open(io.BytesIO(back.content))
        r.check("and answered from then on, by a server that still cannot decode it, as immutable",
                back.status_code == 200 and back.headers.get("cache-control") == routes.IMMUTABLE_CACHE, str(back.status_code))
        r.check("made small and encoded again here, so nothing kept is the page's own bytes",
                back.headers["content-type"] == "image/webp" and kept.size == (posters.POSTER_SIDE, 270), str(kept.size))
        r.check("with the clip's length it was given", back.headers.get("x-minipaint-duration") == "3.500",
                str(back.headers.get("x-minipaint-duration")))
        again = client.put(address + f"?v={item['v']}", content=frame.getvalue(), headers={"Content-Type": "image/jpeg"})
        r.check("given again, the one kept is left as it is", again.json() == {"ok": True, "kept": False}, again.text[:80])
        posters.reset_for_tests("")
        r.check("and it is on disk: after a restart it is still the answer",
                client.get(address + f"?v={item['v']}").status_code == 200)
        r.check("no answer names the file or its folder",
                "broken" not in given.text + stale.text + junk.text and str(folder) not in given.text + stale.text + junk.text)
    finally:
        posters.reset_for_tests()


def poster_folder_checks(r: Results) -> None:
    """The cache an earlier build kept - frames at one second - is let go of
    the first time the cache of first frames is opened, and only what that
    build wrote there."""
    from minipaint_neo.clipboard import posters

    base = config.config_dir()
    former = base / posters.FORMER_DIR_NAMES[0]
    former.mkdir(parents=True, exist_ok=True)
    (former / ("wf" + "a" * 16 + "-1-2-480-1000.webp")).write_bytes(b"an old poster")
    stranger = base / "not-a-poster-folder"
    stranger.mkdir(exist_ok=True)
    (stranger / "keep.txt").write_text("mine")
    posters.reset_for_tests()
    folder = posters.posters().directory()
    r.check("the cache of first frames is a folder of its own", folder is not None and folder.name == posters.POSTER_DIR_NAME)
    r.check("and the one-second posters of the build before are gone, folder and all", not former.exists())
    r.check("while nothing else in the data folder is touched", (stranger / "keep.txt").read_text() == "mine")


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
            for name in ("exact", "window", "restart", "page", "route", "recipe", "folder", "settings", "whole", "posters"):
                folders[name] = base / name
                folders[name].mkdir()
            exact_checks(r, clock, folders["exact"])
            relative_checks(r, clock, base)
            window_checks(r, clock, folders["window"])
            restart_checks(r, clock, folders["restart"])
            page_checks(r, clock, folders["page"])
            folder_gallery_checks(r, clock, folders["folder"])
            recipe_checks(r, clock, folders["recipe"])
            range_checks(r)
            route_checks(r, folders["route"], clock)
            settings_route_checks(r, folders["settings"], clock)
            whole_listing_checks(r, clock, folders["whole"])
            poster_checks(r, folders["posters"], clock)
            poster_folder_checks(r)
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
