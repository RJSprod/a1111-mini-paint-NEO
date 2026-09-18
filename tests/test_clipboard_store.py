"""The Clipboard library: one folder, opaque ids, and everything they refuse.

Section 28.3 of the design intent, made concrete. A root is chosen and
proved usable; a name with a separator, a symlink, a file that has moved out
of the folder and an id nobody minted are each refused with their own code
and never repaired into something openable. A Forge PNG imported from bytes
keeps every byte, so its generation metadata survives; pixels that exist only
in memory become a PNG. Two files with one name become "name" and "name
(2)". A rename keeps the id and the extension; a delete removes the file and
leaves the history record to say Missing; a file dropped into the folder by
hand appears on Refresh with a fresh id, and one renamed by hand keeps its
id when its bytes are the same bytes. Changing the root moves nothing.

The draft and the history are checked as documents: written atomically, read
forgivingly, a broken one moved aside rather than fatal, and a history record
holding a prompt only when the user typed one into Clipboard.
"""

from harness import Results, setup_path

setup_path()

import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import pathlib  # noqa: E402
import tempfile  # noqa: E402

from PIL import Image, PngImagePlugin  # noqa: E402

from minipaint_neo.clipboard import config, history, routes, store  # noqa: E402
from minipaint_neo.wangp import config as wangp_config  # noqa: E402
from minipaint_neo.wangp import errors, protocol  # noqa: E402
from minipaint_neo.wangp.errors import IntegrationError  # noqa: E402

GOOD = "0123456789abcdef0123456789abcdef"


def _png(colour=(10, 200, 30, 255), size=(8, 6), text=None) -> bytes:
    buffer = io.BytesIO()
    image = Image.new("RGBA", size, colour)
    info = PngImagePlugin.PngInfo()
    if text:
        info.add_text("parameters", text)
    image.save(buffer, format="PNG", pnginfo=info)
    return buffer.getvalue()


def _jpeg(size=(8, 6)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 10, 30)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _refused(call, *args, **keywords) -> str:
    try:
        call(*args, **keywords)
    except IntegrationError as error:
        return error.code
    return ""


def root_checks(r: Results, base: pathlib.Path) -> None:
    library = store.store()
    r.check("before a root is chosen the library is not configured", not library.configured() and library.assets() == [])
    r.check("and an asset lookup is CLIPBOARD_NOT_CONFIGURED", _refused(library.resolve, GOOD) == errors.CLIPBOARD_NOT_CONFIGURED)
    r.check("and an import is too", _refused(library.import_bytes, _png(), "a.png") == errors.CLIPBOARD_NOT_CONFIGURED)

    path, code, _ = store.validate_root("")
    r.check("an empty root is named as empty", path is None and code == "ROOT_EMPTY")
    missing = base / "not-yet"
    path, code, _ = store.validate_root(str(missing))
    r.check("a missing folder is not created behind the user's back", path is None and code == "ROOT_MISSING" and not missing.exists())
    path, code, _ = store.validate_root(str(missing), create=True)
    r.check("and is created when asked", path == missing.resolve() and code == "" and missing.is_dir())
    file_here = base / "a-file.txt"
    file_here.write_text("x", encoding="utf-8")
    path, code, _ = store.validate_root(str(file_here))
    r.check("an existing non-directory is refused", path is None and code == "ROOT_NOT_A_DIRECTORY")
    r.check("a relative or ~ path is made absolute", store.validate_root("~")[0] is not None and store.validate_root("~")[0].is_absolute())

    library_root = base / "library"
    answer = library.set_root(str(library_root), create=True)
    r.check("choosing a root saves it, resolved", answer["ok"] and config.load().storage_root == str(library_root.resolve()), str(answer))
    r.check("with a stable opaque id", config.load().root_id == config.root_id_for(library_root.resolve()) and len(config.load().root_id) == 16)
    r.check("the chosen path is shown back to the user", answer.get("path") == str(library_root.resolve()))
    r.check("and the library is now configured and empty", library.configured() and library.refresh() == [])
    r.check("nothing but the config was written into the data dir",
            sorted(p.name for p in config.config_dir().iterdir() if p.name.startswith("clipboard")) == ["clipboard-index.json", "clipboard.json"],
            str(sorted(p.name for p in config.config_dir().iterdir())))
    r.check("and nothing was written into the image folder", list(library_root.iterdir()) == [])


def import_checks(r: Results, base: pathlib.Path) -> None:
    library = store.store()
    root = library.root()

    forge_png = _png(text="a photo of a cat, Steps: 20")
    asset = library.import_bytes(forge_png, "00001-12345.png", "forge_gallery")
    r.check("an imported PNG gets a 32-hex id", protocol.valid_handoff_id(asset.asset_id))
    r.check("and keeps its name", asset.filename == "00001-12345.png" and asset.relative_path == "00001-12345.png")
    r.check("and its bytes, metadata included", (root / "00001-12345.png").read_bytes() == forge_png)
    r.check("and its facts", asset.width == 8 and asset.height == 6 and asset.mime == "image/png" and asset.size_bytes == len(forge_png) and asset.source == "forge_gallery")
    r.check("the id is in the index on disk", asset.asset_id in json.loads(config.path_of(config.INDEX_NAME).read_text())["roots"][config.load().root_id])
    r.check("the browser is told the name and never a path",
            set(asset.public()) == {"asset_id", "filename", "size_bytes", "width", "height", "mime", "mtime_ns", "source"})
    r.check("the last import is remembered for the tab to select", library.last_import and library.last_import[0] == asset.asset_id)

    second = library.import_bytes(_png(colour=(1, 1, 1, 255)), "00001-12345.png", "upload")
    r.check("a second file with the same name is not overwritten", second.filename == "00001-12345 (2).png" and (root / "00001-12345.png").read_bytes() == forge_png)
    third = library.import_bytes(_png(colour=(2, 2, 2, 255)), "00001-12345 (2).png", "upload")
    r.check("and the suffix counts on", third.filename == "00001-12345 (3).png", third.filename)

    jpeg = library.import_bytes(_jpeg(), "photo.png", "upload")
    r.check("a JPEG called .png is imported as what it is", jpeg.filename == "photo.jpg" and jpeg.mime == "image/jpeg", jpeg.filename)
    r.check("a JPEG's bytes are preserved too", (root / "photo.jpg").read_bytes() == _jpeg())
    pasted = library.import_image(Image.new("RGB", (5, 5), (9, 9, 9)), "", "paste")
    r.check("pixels from memory become a PNG named after their source", pasted.filename.startswith("paste-") and pasted.filename.endswith(".png") and pasted.source == "paste")
    r.check("losslessly", Image.open(root / pasted.filename).convert("RGB").getpixel((0, 0)) == (9, 9, 9))
    named = library.import_image(Image.new("RGB", (5, 5)), "canvas render.webp", "minipaint")
    r.check("a picture from Mini Paint is named for its document, as a PNG", named.filename == "canvas render.png", named.filename)

    for label, name in (("a separator", "sub/dir.png"), ("a backslash", "sub\\dir.png"), ("a traversal", "../up.png"),
                        ("a hidden name", ".hidden.png"), ("our temp prefix", ".minipaint-x.png"), ("a device name", "CON.png"), ("nothing", "")):
        r.check(f"{label} is not a filename", _refused(store.safe_basename, name) == errors.REQUEST_INVALID, name)
    r.check("an unsupported extension gets the default", store.safe_basename("notes.txt") == "notes.txt.png")
    r.check("a long name is bounded", len(store.safe_basename("x" * 500 + ".png")) <= store.MAX_NAME_LENGTH)
    r.check("text is not an image", _refused(library.import_bytes, b"hello", "a.png") == errors.HANDOFF_INVALID_IMAGE)
    gif = io.BytesIO()
    Image.new("P", (2, 2)).save(gif, format="GIF")
    r.check("a GIF is not a library format", _refused(library.import_bytes, gif.getvalue(), "a.gif") == errors.HANDOFF_INVALID_IMAGE)
    frames = io.BytesIO()
    Image.new("RGB", (2, 2)).save(frames, format="WEBP", save_all=True, append_images=[Image.new("RGB", (2, 2), (1, 1, 1))], duration=10)
    with Image.open(io.BytesIO(frames.getvalue())) as probe:
        animated = getattr(probe, "n_frames", 1) > 1
    if animated:
        r.check("an animated WebP is refused", _refused(library.import_bytes, frames.getvalue(), "a.webp") == errors.HANDOFF_INVALID_IMAGE)
    else:
        r.check("an animated WebP is refused (this Pillow cannot make one; skipped)", True)
    r.check("the folder holds exactly the files that were imported",
            sorted(p.name for p in root.iterdir()) == sorted(a.filename for a in library.assets()), str(sorted(p.name for p in root.iterdir())))


def containment_checks(r: Results, base: pathlib.Path) -> None:
    library = store.store()
    root = library.root()
    asset = library.assets("name_asc")[0]
    found, path = library.resolve(asset.asset_id)
    r.check("resolve proves the file is directly inside the resolved root", path.parent == root.resolve() and found.asset_id == asset.asset_id)
    for label, bad in (("a path", "../" + GOOD[3:]), ("a short id", GOOD[:-1]), ("upper case", GOOD.upper()), ("nothing", ""), ("None", None)):
        r.check(f"{label} is not an asset id", _refused(library.resolve, bad) == errors.CLIPBOARD_ASSET_UNKNOWN)
    r.check("an id nobody minted is unknown", _refused(library.resolve, GOOD) == errors.CLIPBOARD_ASSET_UNKNOWN)

    # An index entry that names a path: never joined into one.
    records = library._records()
    planted = store.Asset(asset_id=GOOD, root_id=asset.root_id, relative_path="../outside.png", filename="outside.png")
    records[GOOD] = planted
    r.check("an indexed name with a separator is refused before it becomes a path",
            _refused(library.resolve, GOOD) == errors.CLIPBOARD_ASSET_OUTSIDE_ROOT)
    records.pop(GOOD, None)

    if os.name != "nt":
        outside = base / "elsewhere"
        outside.mkdir(exist_ok=True)
        victim = outside / "victim.png"
        victim.write_bytes(_png())
        link = root / "link.png"
        os.symlink(victim, link)
        try:
            names = [a.filename for a in library.refresh()]
            r.check("refresh does not index a symlink", "link.png" not in names, str(names))
            records = library._records()
            records[GOOD] = store.Asset(asset_id=GOOD, root_id=asset.root_id, relative_path="link.png", filename="link.png")
            r.check("and an indexed link is refused unopened", _refused(library.resolve, GOOD) == errors.CLIPBOARD_ASSET_OUTSIDE_ROOT)
            r.check("and not served", _refused(library.read_bytes, GOOD) == errors.CLIPBOARD_ASSET_OUTSIDE_ROOT)
            r.check("and not deleted through the library", _refused(library.delete, GOOD) == errors.CLIPBOARD_ASSET_OUTSIDE_ROOT and victim.exists())
            records.pop(GOOD, None)
        finally:
            link.unlink()
        sub = root / "nested"
        sub.mkdir(exist_ok=True)
        (sub / "deep.png").write_bytes(_png())
        r.check("refresh reads the top level only", "deep.png" not in [a.filename for a in library.refresh()])

    data, mime = library.read_bytes(asset.asset_id)
    r.check("read_bytes serves the file's own bytes with its mime", data == path.read_bytes() and mime == asset.mime)
    thumb, thumb_mime = library.thumbnail(asset.asset_id)
    r.check("a thumbnail is a small encoded copy", len(thumb) > 0 and thumb_mime in ("image/webp", "image/png"))
    r.check("and is cached by identity", library.thumbnail(asset.asset_id) == (thumb, thumb_mime) and len(library._thumbnails) >= 1)
    r.check("the URL the tab uses names the id and never the file", routes.image_url(asset.asset_id, version=asset.mtime_ns).startswith("/minipaint-clipboard/image/" + asset.asset_id)
            and asset.filename not in routes.image_url(asset.asset_id))
    image = library.open_image(asset.asset_id)
    r.check("open_image decodes to RGBA", image.mode == "RGBA" and image.size == (asset.width, asset.height))


def file_operation_checks(r: Results, base: pathlib.Path) -> None:
    library = store.store()
    root = library.root()
    asset = next(a for a in library.assets("name_asc") if a.filename == "photo.jpg")

    renamed = library.rename(asset.asset_id, "holiday")
    r.check("rename keeps the id and the extension", renamed.asset_id == asset.asset_id and renamed.filename == "holiday.jpg" and (root / "holiday.jpg").exists() and not (root / "photo.jpg").exists())
    r.check("and the index follows", library.get(asset.asset_id).filename == "holiday.jpg")
    other = library.import_bytes(_jpeg(), "other.jpg", "upload")
    r.check("a rename to a name in use is refused", _refused(library.rename, asset.asset_id, "other.jpg") == errors.REQUEST_INVALID
            and library.get(asset.asset_id).filename == "holiday.jpg")
    r.check("unless a suffix is asked for", library.rename(asset.asset_id, "other.jpg", allow_suffix=True).filename == "other (2).jpg"
            and library.get(other.asset_id).filename == "other.jpg")
    r.check("a rename with a separator is refused", _refused(library.rename, asset.asset_id, "../x") == errors.REQUEST_INVALID)
    r.check("a rename that lies about the content keeps the real extension", library.rename(asset.asset_id, "still-a-photo.png").filename == "still-a-photo.jpg")

    # The draft holds it; history holds it; delete keeps both records honest.
    history.save_draft({"prompt_override": "", "first_asset_id": asset.asset_id, "last_asset_id": "", "reference_asset_ids": []})
    record = history.add_history(history.make_record(history.load_draft(), {"request_id": GOOD, "applied": {"start": True}, "tasks_added": 1, "model": {"label": "M"}}))
    count_before = len(list(root.iterdir()))
    gone = library.delete(asset.asset_id)
    r.check("delete removes exactly that file", gone.asset_id == asset.asset_id and not (root / "still-a-photo.jpg").exists() and len(list(root.iterdir())) == count_before - 1)
    r.check("and forgets the id", library.get(asset.asset_id) is None and _refused(library.resolve, asset.asset_id) == errors.CLIPBOARD_ASSET_UNKNOWN)
    r.check("history keeps its record and can say Missing",
            history.load_history()[0]["history_id"] == record["history_id"] and history.load_history()[0]["first_asset_id"] == asset.asset_id)
    draft, missing = history.draft_from_record(history.load_history()[0], lambda item: library.get(item) is not None)
    r.check("loading that record leaves the missing slot inherited and names it", draft["first_asset_id"] == "" and missing == ["first"], str((draft, missing)))
    r.check("deleting the history record deletes only the record", history.delete_history(record["history_id"]) and history.load_history() == [] and len(list(root.iterdir())) == count_before - 1)
    r.check("and a second delete of it is a no", history.delete_history(record["history_id"]) is False)


def refresh_checks(r: Results, base: pathlib.Path) -> None:
    library = store.store()
    root = library.root()
    before = {a.asset_id for a in library.assets()}

    (root / "dropped-in.png").write_bytes(_png(colour=(3, 4, 5, 255)))
    (root / "notes.txt").write_text("not a picture", encoding="utf-8")
    (root / "fake.png").write_bytes(b"not a png at all")
    (root / ".minipaint-tmp.png").write_bytes(_png())
    after = library.refresh()
    names = {a.filename for a in after}
    r.check("a file copied in by hand appears on Refresh", "dropped-in.png" in names, str(names))
    r.check("with a fresh id", len({a.asset_id for a in after} - before) == 1)
    r.check("a text file, a fake image and a temp file do not", not ({"notes.txt", "fake.png", ".minipaint-tmp.png"} & names))
    r.check("the ids of the files already there are kept", before <= {a.asset_id for a in after})

    dropped = next(a for a in after if a.filename == "dropped-in.png")
    os.rename(root / "dropped-in.png", root / "renamed-by-hand.png")
    again = library.refresh()
    recovered = next((a for a in again if a.filename == "renamed-by-hand.png"), None)
    r.check("a file renamed outside Clipboard keeps its id when its bytes match", recovered is not None and recovered.asset_id == dropped.asset_id)
    (root / "renamed-by-hand.png").write_bytes(_png(colour=(6, 7, 8, 255)))
    changed = next(a for a in library.refresh() if a.filename == "renamed-by-hand.png")
    r.check("a file edited in place keeps its id and updates its facts", changed.asset_id == dropped.asset_id and changed.sha256 != dropped.sha256)

    # B21: a refresh does not re-read a file whose metadata proves it
    # unchanged. A folder of large stills would otherwise be hashed in full
    # on every press of Refresh, which is the whole cost of the operation.
    reads = {"count": 0}
    original = store.inspect_bytes

    def counting(data):
        reads["count"] += 1
        return original(data)

    store.inspect_bytes = counting
    try:
        listed = library.refresh()
    finally:
        store.inspect_bytes = original
    r.check("a refresh that changes nothing reads no file at all",
            reads["count"] == 0 and len(listed) == len(again), f"{reads['count']} file(s) read")

    os.utime(root / "renamed-by-hand.png", None)
    reads["count"] = 0
    store.inspect_bytes = counting
    try:
        library.refresh()
    finally:
        store.inspect_bytes = original
    r.check("and exactly one when one file's metadata moved", reads["count"] == 1, str(reads["count"]))
    os.unlink(root / "renamed-by-hand.png")
    r.check("a file removed by hand leaves the index", dropped.asset_id not in {a.asset_id for a in library.refresh()})
    (root / "notes.txt").unlink()
    (root / "fake.png").unlink()
    (root / ".minipaint-tmp.png").unlink()

    modes = {mode: [a.filename for a in library.assets(mode)] for mode in config.SORT_MODES}
    r.check("name sorts are each other reversed", modes["name_asc"] == list(reversed(modes["name_desc"])))
    r.check("size sorts are each other reversed", [a.size_bytes for a in library.assets("largest")] == sorted((a.size_bytes for a in library.assets()), reverse=True))
    r.check("newest first is by modification time", [a.mtime_ns for a in library.assets("newest")] == sorted((a.mtime_ns for a in library.assets()), reverse=True))


def root_change_checks(r: Results, base: pathlib.Path) -> None:
    library = store.store()
    old_root = library.root()
    old_files = sorted(p.name for p in old_root.iterdir())
    old_ids = {a.asset_id for a in library.assets()}
    keep = next(iter(library.assets()))
    history.save_draft({"prompt_override": "kept prompt", "first_asset_id": keep.asset_id, "last_asset_id": "", "reference_asset_ids": []})

    new_root = base / "second-library"
    r.check("a second root is taken", library.set_root(str(new_root), create=True)["ok"])
    r.check("nothing moved and nothing was deleted", sorted(p.name for p in old_root.iterdir()) == old_files and list(new_root.iterdir()) == [])
    r.check("the new library is empty until something is put in it", library.refresh() == [])
    r.check("an old asset id is unknown in the new root", library.get(keep.asset_id) is None)
    r.check("the draft's prompt survives the change", history.load_draft()["prompt_override"] == "kept prompt")
    r.check("the config remembers the new root", config.load().storage_root == str(new_root.resolve()))
    r.check("going back finds the old library with its ids", library.set_root(str(old_root))["ok"] and library.get(keep.asset_id) is not None
            and {a.asset_id for a in library.assets()} == old_ids)


def document_checks(r: Results, base: pathlib.Path) -> None:
    draft = history.save_draft({"prompt_override": "p", "first_asset_id": GOOD, "last_asset_id": "nope", "reference_asset_ids": [GOOD, "x", GOOD], "path": "/tmp"})
    r.check("a draft keeps text and ids and nothing else", draft == {"prompt_override": "p", "first_asset_id": GOOD, "last_asset_id": "", "reference_asset_ids": [GOOD, GOOD]}, str(draft))
    r.check("and comes back the same", history.load_draft() == draft)
    r.check("a draft with only a prompt supplies only the prompt", history.draft_overrides({"prompt_override": " x "}) == ["prompt"])
    r.check("an empty draft supplies nothing", history.draft_overrides(history.empty_draft()) == [])
    request = history.public_request(draft, GOOD)
    r.check("the public request omits inherited fields and names Clipboard assets by kind",
            request == {"request_id": GOOD, "images": {"start": {"kind": "clipboard_asset", "id": GOOD},
                                                       "references": [{"kind": "clipboard_asset", "id": GOOD}, {"kind": "clipboard_asset", "id": GOOD}]}, "prompt": "p"}, str(request))
    r.check("an empty draft is an empty request", history.public_request(history.empty_draft(), GOOD) == {"request_id": GOOD, "images": {}})

    inherit = history.make_record(history.empty_draft(), {"request_id": GOOD, "applied": {}, "inherited": ["prompt", "start", "end", "references"], "tasks_added": 1, "model": {"type": "t", "label": "L"}})
    r.check("a record for an empty draft inherits everything and holds no prompt",
            inherit["prompt_mode"] == "inherit" and inherit["prompt_override"] == "" and inherit["first_mode"] == "inherit" and inherit["model_label"] == "L")
    typed = history.make_record({"prompt_override": "typed here", "first_asset_id": GOOD, "reference_asset_ids": [GOOD]},
                                {"request_id": GOOD, "applied": {"prompt": True, "start": True}, "ignored": [{"field": "references", "code": "RECEIVER_DISABLED"}], "tasks_added": 2})
    r.check("a typed prompt is stored as an override", typed["prompt_mode"] == "override" and typed["prompt_override"] == "typed here")
    r.check("an ignored slot is recorded as ignored with its asset kept", typed["reference_mode"] == "ignored" and typed["reference_asset_ids"] == [GOOD] and typed["ignored"] == [{"field": "references", "code": "RECEIVER_DISABLED"}])
    r.check("the task count is kept", typed["tasks_added"] == 2)
    history.add_history(inherit)
    history.add_history(typed)
    r.check("history is newest first", [x["history_id"] for x in history.load_history()] == [typed["history_id"], inherit["history_id"]])
    loaded, missing = history.draft_from_record(typed, lambda item: True)
    r.check("Load restores the recipe, ignored slots included, without queueing", loaded == {"prompt_override": "typed here", "first_asset_id": GOOD, "last_asset_id": "", "reference_asset_ids": [GOOD]} and missing == [])
    for _ in range(history.MAX_HISTORY + 5):
        history.add_history(history.make_record(history.empty_draft(), {"request_id": GOOD, "tasks_added": 1}))
    r.check("history is bounded", len(history.load_history()) == history.MAX_HISTORY)

    config.path_of(config.HISTORY_NAME).write_text("{not json", encoding="utf-8")
    r.check("a broken history is moved aside and read as empty", history.load_history() == [] and any(p.name.startswith("clipboard-history.json.broken-") for p in config.config_dir().iterdir()))
    config.path_of(config.CONFIG_NAME).write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    r.check("a config from the future is not trusted", not config.load().configured)
    config.save(config.Config(storage_root="/x", intercept_target=config.INTERCEPT_CLIPBOARD, sort="largest", thumbnail=999))
    loaded = config.load()
    r.check("the config keeps the intercept destination, the sort and a clamped thumbnail",
            loaded.intercept_target == config.INTERCEPT_CLIPBOARD and loaded.intercept is True and loaded.sort == "largest" and loaded.thumbnail == config.THUMBNAIL_MAX)
    r.check("and an unknown sort falls back", config.Config.from_dict({"sort": "sideways"}).sort == config.DEFAULT_SORT)
    r.check("update changes one field and keeps the rest", config.update(intercept=False).sort == "largest" and config.load().intercept is False)
    written = config.path_of(config.CONFIG_NAME).read_text(encoding="utf-8")
    r.check("the file holds exactly the declared keys",
            set(json.loads(written)) == {"schema_version", "storage_root", "root_id", "intercept", "intercept_target", "intercept_inherit",
                                         "sort", "thumbnail", "outputs_folder"},
            str(sorted(json.loads(written))))
    # The intercept is a destination now, and the older switch is a view of
    # it in both directions: an old file reads as the destination it meant,
    # and a new file still carries the switch an old build reads.
    r.check("a file that only knows the switch reads as Mini Paint when it is off",
            config.Config.from_dict({"intercept": False}).intercept_target == config.INTERCEPT_MINIPAINT)
    r.check("and as Clipboard when it is on",
            config.Config.from_dict({"intercept": True}).intercept_target == config.INTERCEPT_CLIPBOARD)
    r.check("a destination the file names wins over the switch beside it",
            config.Config.from_dict({"intercept": True, "intercept_target": config.INTERCEPT_WANGP}).intercept_target == config.INTERCEPT_WANGP)
    r.check("and one it does not know falls back to the switch",
            config.Config.from_dict({"intercept": True, "intercept_target": "sideways"}).intercept_target == config.INTERCEPT_CLIPBOARD)
    r.check("the switch is written on exactly when the destination is Clipboard",
            config.update(intercept_target=config.INTERCEPT_WANGP).as_dict()["intercept"] is False
            and config.update(intercept_target=config.INTERCEPT_CLIPBOARD).as_dict()["intercept"] is True)
    r.check("and setting the switch moves the destination", config.update(intercept=False).intercept_target == config.INTERCEPT_MINIPAINT)


def send_route_checks(r: Results, base) -> None:
    """The send that does not need Gradio.

    Sending used to be a hidden box plus a Gradio event over the queue, and
    when that queue stopped delivering the send simply vanished. This route
    is the same decision reached over the transport that still works, so
    what it answers has to be exactly what the browser needs to finish the
    job - and it has to refuse honestly for the destinations it cannot.
    """
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from minipaint_neo.clipboard import ui as clip_ui

    # A destination only exists on a page that was built: the route answers
    # from the tab's own targets, and without a tab there is nothing to name.
    import forge_like
    from modules import script_callbacks, shared
    from minipaint_neo import router, settings
    from minipaint_neo.canvas import host as canvas_host

    shared.opts.data[settings.USE_OLD_UI] = False
    script_callbacks.callbacks["after_component"][:] = [canvas_host.on_after_component]
    canvas_host.reset_capture()
    forge_like.build_host(lambda: (router.on_ui_tabs() or []) + (clip_ui.on_ui_tabs() or []))

    library = base / "sendable"
    library.mkdir(exist_ok=True)
    answer = store.store().set_root(str(library), create=True)
    r.check("the send route's library is in place", answer["ok"], str(answer))
    Image.new("RGB", (48, 32), (10, 20, 30)).save(library / "one.png")
    store.store().refresh()
    asset = next(iter(store.store().assets()))

    app = FastAPI()
    routes.install(app)
    client = TestClient(app)

    answered = client.post(routes.SEND_ROUTE, json={"target": "img2img", "asset": asset.asset_id})
    body = answered.json()
    r.check("a send to img2img is answered without Gradio", answered.status_code == 200 and body.get("ok"), str(body)[:160])
    r.check("and carries the picture the browser has to write",
            str(body.get("payload", "")).startswith("data:image/"), str(body.get("payload", ""))[:40])
    r.check("and names the box to write it into, rather than leaving the browser to guess",
            "box" in body, str(sorted(body)))
    r.check("and is not marked as one the server has to finish", body.get("backend") is False, str(body.get("backend")))

    # The stitch galleries hold their value in the component. When this
    # process has no component to name, there is nothing the browser could
    # fill and the plan says so rather than handing over a payload it
    # cannot place.
    from minipaint_neo.canvas import host as canvas_host

    class _Component:
        def __init__(self, elem_id):
            self.elem_id = elem_id

    held = clip_ui._current.get("tab")
    clip_ui._current["tab"] = None
    canvas_host.reset_capture()
    try:
        stitched = client.post(routes.SEND_ROUTE, json={"target": "stitch_txt2img", "asset": asset.asset_id}).json()
        r.check("a destination with no component on this page says so and hands over no payload",
                stitched.get("ok") and stitched.get("backend") is True and not stitched.get("payload"),
                str(stitched)[:160])

        # And when there is one: its id and the picture, which is all the
        # browser needs to hand it the file over the upload route - the half
        # of the connection that still works when the queue does not.
        canvas_host._captured["stitch_txt2img"] = _Component("script_txt2img_imagestitch_integrated_ref_latent")
        canvas_host._captured["stitch_txt2img_enable"] = _Component("script_txt2img_imagestitch_integrated-checkbox")
        named = client.post(routes.SEND_ROUTE, json={"target": "stitch_txt2img", "asset": asset.asset_id}).json()
        r.check("a destination with a component names it and carries the picture",
                named.get("ok") and named.get("backend") is False
                and named.get("elem") == "script_txt2img_imagestitch_integrated_ref_latent"
                and str(named.get("payload", "")).startswith("data:image/"),
                str({k: (v[:24] if k == "payload" else v) for k, v in named.items()})[:200])
        r.check("and says a gallery keeps what is already in it, where the server's write replaces it",
                named.get("adds") is True, str(named.get("adds")))
    finally:
        canvas_host.reset_capture()
        clip_ui._current["tab"] = held

    # The request left where the Gradio event can find it. A press makes the
    # server read a hidden box and gets whatever the framework holds for it,
    # which on the install this is for is not what the page wrote; this is
    # the same request arriving over the transport that keeps working.
    routes.forget_request()
    r.check("with nothing posted, there is no request to fall back on", routes.recent_request() == "")
    recorded = client.post(routes.SEND_ROUTE, json={"request": f"img2img:{asset.asset_id}:1700000100:done"})
    r.check("a request with no destination is kept, and nothing is prepared",
            recorded.status_code == 200 and recorded.json().get("recorded") is True
            and "payload" not in recorded.json(), str(recorded.json())[:160])
    r.check("and is what the event falls back on",
            routes.recent_request() == f"img2img:{asset.asset_id}:1700000100:done", routes.recent_request())
    stale_at = routes._last_request[1] - routes.REQUEST_MEMORY_SECONDS - 1
    routes._last_request = (routes._last_request[0], stale_at)
    r.check("a request nobody followed up on stops being offered, rather than being picked up much later",
            routes.recent_request() == "", routes.recent_request())
    routes.forget_request()

    unknown = client.post(routes.SEND_ROUTE, json={"target": "nowhere", "asset": asset.asset_id})
    r.check("a destination that is not one is refused", unknown.status_code == 400 and not unknown.json().get("ok"))
    missing = client.post(routes.SEND_ROUTE, json={"target": "img2img", "asset": "00" * 16})
    r.check("a picture that is not there is refused", missing.status_code == 400 and not missing.json().get("ok"))
    r.check("and a body that is not a send at all is refused rather than guessed at",
            client.post(routes.SEND_ROUTE, content=b"not json").status_code == 400)

    # The same question asked directly, so the route and the tab cannot drift.
    plan = clip_ui.send_plan("img2img", asset.asset_id)
    r.check("the route and the tab answer the same question the same way",
            plan.get("instruction") == body.get("instruction") and plan.get("backend") == body.get("backend"),
            f"{plan.get('instruction')!r} vs {body.get('instruction')!r}")


def http_door_checks(r: Results, base: pathlib.Path) -> None:
    """Every row that moved has a door on plain HTTP, and it is gated.

    "Anything that can ride plain HTTP, does" is not elegance: HTTP is the
    transport that has kept working through every failure this tab has had,
    while the one that kept being chosen is the one that broke. So the doors
    are checked as doors - status codes, sign-in, and an answer that carries
    its own sentence for the status line.
    """
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    app = FastAPI()
    routes.install(app)
    client = TestClient(app)

    listed = client.get(routes.LIBRARY_ROUTE, params={"sort": "name_asc", "page": 0, "size": 10})
    body = listed.json()
    r.check("the index route answers over plain HTTP", listed.status_code == 200 and body.get("ok") is True, str(body)[:120])
    r.check("and is cached by nobody: it is the truth, asked for again on every change",
            listed.headers.get("cache-control") == "no-store", str(listed.headers.get("cache-control")))
    r.check("and carries a sentence for the status line, not just numbers",
            isinstance(body.get("status"), str) and body["status"], str(body.get("status")))

    refreshed = client.get(routes.LIBRARY_ROUTE, params={"refresh": "1"}).json()
    r.check("it can be asked to read the folder again first, which is what opening the tab does",
            refreshed.get("ok") is True)

    saved = client.post(routes.SETTINGS_ROUTE, json={"sort": "largest", "thumbnail": 5000, "intercept": True})
    kept = saved.json()
    r.check("the settings route saves the sort, clamps the size and takes the switch",
            saved.status_code == 200 and kept["sort"] == "largest"
            and kept["thumbnail"] == config.THUMBNAIL_MAX and kept["intercept"] is True, str(kept)[:160])
    r.check("and answers with the menu the browser draws itself from, so no render is needed",
            kept["menu"]["sort"] == "largest" and kept["menu"]["intercept"] is True
            and kept["menu"]["intercept_target"] == config.INTERCEPT_CLIPBOARD
            and [target for target, _label in kept["menu"]["intercepts"]] == list(config.INTERCEPT_TARGETS)
            and [mode for mode, _label in kept["menu"]["sorts"]] == list(config.SORT_MODES), str(kept["menu"])[:160])
    pointed = client.post(routes.SETTINGS_ROUTE, json={"intercept_target": "wangp"}).json()
    r.check("the route takes the destination by name too, and says what the button does now",
            pointed["intercept_target"] == "wangp" and pointed["intercept"] is False and "WanGP" in pointed["status"], str(pointed)[:160])
    r.check("and a destination it does not know changes nothing",
            client.post(routes.SETTINGS_ROUTE, json={"intercept_target": "elsewhere"}).json()["intercept_target"] == "wangp")
    r.check("and says what it did, in the words the tab has always used",
            "Clipboard" in kept["status"], str(kept["status"]))
    nonsense = client.post(routes.SETTINGS_ROUTE, json={"sort": "sideways"}).json()
    r.check("a sort it does not know changes nothing rather than refusing",
            nonsense["ok"] is True and nonsense["sort"] == "largest", str(nonsense)[:120])
    client.post(routes.SETTINGS_ROUTE, json={"sort": config.DEFAULT_SORT, "intercept": False})

    enhancement = client.get(routes.ENHANCE_SETTINGS_ROUTE).json()
    r.check("the enhancement settings describe themselves over HTTP",
            enhancement.get("ok") is True and "enabled" in enhancement, str(enhancement)[:120])
    toggled = client.post(routes.ENHANCE_SETTINGS_ROUTE, json={"action": "toggle", "enabled": True}).json()
    r.check("and the switch is a write route now, not only a Gradio event",
            toggled.get("ok") is True and toggled.get("enabled") is True and "on" in toggled.get("status", ""), str(toggled)[:120])
    client.post(routes.ENHANCE_SETTINGS_ROUTE, json={"action": "toggle", "enabled": False})
    r.check("an action it does not know is refused with its own sentence",
            client.post(routes.ENHANCE_SETTINGS_ROUTE, json={"action": "sideways"}).status_code == 400)

    queue = client.get(routes.QUEUE_ROUTE, params={"page": "a" * 16})
    answer = queue.json()
    r.check("the queue is readable over HTTP - which is what 'I closed the browser and came back' needs",
            queue.status_code in (200, 503) and isinstance(answer, dict), str(answer)[:120])
    if answer.get("ok"):
        r.check("and carries the jobs, the history and the button's two facts",
                isinstance(answer.get("jobs"), list) and isinstance(answer.get("history"), list)
                and set(answer.get("queue_button") or {}) == {"label", "enabled"}, str(sorted(answer))[:160])
    r.check("a queue action it does not know is refused rather than guessed at",
            client.post(routes.QUEUE_ROUTE, json={"action": "sideways"}).status_code in (400, 503))

    # -- the sign-in gate, which every one of these shares
    from minipaint_neo.wangp import proxy

    original = proxy.signed_in
    proxy.signed_in = lambda request: False
    try:
        for route, call in ((routes.LIBRARY_ROUTE, client.get), (routes.QUEUE_ROUTE, client.get)):
            r.check(f"{route} is gated by the same sign-in as the picture route", call(route).status_code == 401)
        for route in (routes.SETTINGS_ROUTE, routes.QUEUE_ROUTE, routes.ENHANCE_SETTINGS_ROUTE):
            r.check(f"and so is writing to {route}", client.post(route, json={}).status_code == 401)
    finally:
        proxy.signed_in = original


# ------------------------------------------------------- the picture index --


def index_checks(r: Results, base: pathlib.Path) -> None:
    """The route contract: sorted over the whole library, then sliced.

    The grid has been reported broken three times with every check passing,
    so what the grid is built from is checked directly rather than through
    the page that draws it.
    """
    library = store.store()
    root = library.root()
    for stale in root.glob("*"):
        stale.unlink()
    library.refresh()
    # Enough to page. Named so that name_asc is a known order.
    for number in range(25):
        (root / f"p{number:02d}.png").write_bytes(_png(size=(4 + number % 3, 4)))
    library.refresh()

    whole = [asset.filename for asset in library.assets("name_asc")]
    r.check("the library has the pictures the pages are cut from", len(whole) == 25, str(len(whole)))

    first = routes.library_page("name_asc", 0, 10)
    second = routes.library_page("name_asc", 1, 10)
    last = routes.library_page("name_asc", 2, 10)
    r.check("a page is exactly `size` pictures, and the last one is what is left",
            len(first["items"]) == 10 and len(second["items"]) == 10 and len(last["items"]) == 5, str(len(last["items"])))
    r.check("and the totals describe the whole library, not the page",
            first["total"] == 25 and first["pages"] == 3 and first["page"] == 0 and last["page"] == 2, json.dumps({k: first[k] for k in ("total", "pages", "page")}))
    r.check("SORTING IS OVER THE WHOLE LIBRARY AND THEN SLICED, not within a page",
            [item["name"] for item in first["items"]] == whole[:10]
            and [item["name"] for item in second["items"]] == whole[10:20], str([i["name"] for i in second["items"]])[:90])
    r.check("the other direction is the same list reversed, page for page",
            [item["name"] for item in routes.library_page("name_desc", 0, 10)["items"]] == list(reversed(whole))[:10])

    r.check("an unknown sort falls back to the stored one rather than refusing",
            routes.library_page("sideways", 0, 10)["sort"] == config.load().sort)
    r.check("a page past the end returns the last page that exists",
            routes.library_page("name_asc", 99, 10)["page"] == 2 and routes.library_page("name_asc", -5, 10)["page"] == 0)
    r.check("a size outside its bounds is clamped, not refused",
            routes.library_page("name_asc", 0, 0)["size"] == routes.PAGE_SIZE_MIN
            and routes.library_page("name_asc", 0, 9999)["size"] == routes.PAGE_SIZE_MAX
            and routes.library_page("name_asc", 0, "nonsense")["size"] == routes.PAGE_SIZE)
    r.check("and the default page holds sixty pictures",
            routes.PAGE_SIZE == 60 and routes.library_page()["size"] == 60)

    item = first["items"][0]
    asset = next(a for a in library.assets() if a.asset_id == item["id"])
    r.check("an item is the id, the name, the shape, the bytes and the version - and no path",
            set(item) == {"id", "name", "w", "h", "bytes", "v"} and item["name"] == asset.filename
            and item["v"] == f"{asset.mtime_ns}-{asset.size_bytes}" and str(base) not in json.dumps(first))
    r.check("and the version is the one the thumbnail cache is keyed by",
            store.version_of(asset) == item["v"] and library.canonical_version(asset.asset_id) == item["v"])

    where = routes.library_page("name_asc", 0, 10, selected=whole and first["items"][0]["id"])
    r.check("the answer says which page holds the selection", where["selected_page"] == 0)
    twelfth = [a for a in library.assets("name_asc")][11]
    r.check("including when it is not this page", routes.library_page("name_asc", 0, 10, selected=twelfth.asset_id)["selected_page"] == 1)
    r.check("and says so plainly when the selection is not in the library at all",
            routes.library_page("name_asc", 0, 10, selected=GOOD)["selected_page"] == -1)

    # -- the revision: it moves when the library does, and at no other time
    was = library.revision()
    r.check("the revision is <epoch>:<n>, namespaced by the process", was.count(":") == 1 and was.split(":")[1].isdigit())
    r.check("reading the library again does not move it",
            routes.library_page()["revision"] == was and library.assets() and library.revision() == was)
    library.refresh()
    r.check("nor does a refresh that found no difference", library.revision() == was)
    (root / "p25.png").write_bytes(_png())
    library.refresh()
    after_refresh = library.revision()
    r.check("a refresh that found a new file does move it", after_refresh != was)
    added = library.import_bytes(_png(), "imported.png", "upload")
    r.check("an import moves it", library.revision() != after_refresh)
    at_import = library.revision()
    library.rename(added.asset_id, "renamed-here")
    r.check("a rename moves it", library.revision() != at_import)
    at_rename = library.revision()
    library.delete(added.asset_id)
    r.check("and a delete moves it", library.revision() != at_rename)

    # -- the states of the index, which are not states of the transport
    for stale in root.glob("*"):
        stale.unlink()
    library.refresh()
    empty = routes.library_page()
    r.check("an empty library is answered with a total of zero and a reason",
            empty["ok"] is True and empty["total"] == 0 and empty["reason"] == "empty" and empty["configured"] is True, json.dumps(empty)[:120])
    r.check("and a page number is still valid on it", empty["page"] == 0 and empty["pages"] == 1)


def thumbnail_cache_checks(r: Results, base: pathlib.Path) -> None:
    """The three caches, and the one that must never be load-bearing."""
    library = store.store()
    root = library.root()
    asset = library.import_bytes(_png(size=(64, 48)), "cached.png", "upload")

    made, mime = library.thumbnail(asset.asset_id)
    directory = library.thumbnail_dir()
    r.check("the disk cache is in the extension's data directory, never in the picture folder",
            directory is not None and directory.parent == config.config_dir() and directory.parent != root, str(directory))
    name = f"{asset.asset_id}-{asset.mtime_ns}-{asset.size_bytes}-{store.THUMBNAIL_SIDE}"
    on_disk = [path.name for path in directory.glob("*")]
    r.check("and a cached thumbnail names the whole file identity it was made from",
            any(one.startswith(name) for one in on_disk), str(on_disk[:3]))
    r.check("so there is no invalidation logic: a file that changed simply does not hit",
            all(store.THUMBNAIL_NAME_RE.match(one) for one in on_disk), str(on_disk[:3]))

    # A new process: memory gone, disk kept.
    library._thumbnails.clear()
    again, again_mime = library.thumbnail(asset.asset_id)
    r.check("a thumbnail survives the memory cache being emptied - which is what a restart is",
            again == made and again_mime == mime and len(library._thumbnails) == 1)

    path = root / asset.filename
    path.write_bytes(_png(colour=(9, 9, 9, 255), size=(70, 50)))
    library.refresh()
    changed = next(a for a in library.assets() if a.asset_id == asset.asset_id)
    r.check("a changed file is a different identity, so a different cache entry",
            store.version_of(changed) != store.version_of(asset))
    library.thumbnail(asset.asset_id)
    r.check("and the new one is written beside the old rather than over it",
            len([one for one in directory.glob("*") if one.name.startswith(asset.asset_id)]) == 2,
            str([one.name for one in directory.glob("*")][:4]))

    # -- the bound, and what it drops
    swept = library.sweep_thumbnails(budget=0)
    r.check("a sweep to nothing empties the cache, and says what it removed",
            swept["ok"] is True and swept["removed"] >= 2 and swept["bytes"] == 0
            and not [one for one in directory.glob("*") if store.THUMBNAIL_NAME_RE.match(one.name)], str(swept))
    stranger = directory / "not-ours.txt"
    stranger.write_text("left by something else", encoding="utf-8")
    library.sweep_thumbnails(budget=0)
    r.check("and a file in that directory that is not ours is counted by nobody and removed by nobody",
            stranger.is_file())
    stranger.unlink()
    r.check("the budget is a real number of bytes, not a count", store.THUMBNAIL_DISK_BUDGET > 1024 * 1024)

    # -- never load-bearing
    # A cache directory that cannot exist: something else put a file where
    # it would have to go. A slower tab, never a broken one.
    library._thumbnails.clear()
    for one in directory.glob("*"):
        one.unlink()
    directory.rmdir()
    directory.write_bytes(b"a file where the cache directory would have to be")
    library._disk_complaint = ""
    fallback = library.thumbnail(asset.asset_id)
    r.check("an unwritable cache directory costs a thumbnail nothing but the time to make it",
            fallback and len(fallback[0]) > 0 and fallback[1] in ("image/webp", "image/png"))
    r.check("and the sweep over one says so rather than raising",
            library.sweep_thumbnails()["ok"] is False and library.thumbnail_dir() is None)
    r.check("said once, not once per tile", library._disk_complaint != "")
    directory.unlink()

    library.thumbnail(asset.asset_id)
    library.delete(asset.asset_id)
    r.check("deleting a picture takes its cached copies with it",
            library.thumbnail_dir() is not None
            and not [one for one in library.thumbnail_dir().glob("*") if one.name.startswith(asset.asset_id)])


def cache_header_checks(r: Results, base: pathlib.Path) -> None:
    """Only the asset's CURRENT version earns a year of immutability."""
    library = store.store()
    asset = library.import_bytes(_png(size=(20, 20)), "header.png", "upload")
    current = library.canonical_version(asset.asset_id)
    r.check("the URL the tab writes carries that version",
            routes.image_url(asset.asset_id, version=current).endswith("?thumb=1&v=" + current))
    r.check("and the two headers say different things",
            "immutable" in routes.IMMUTABLE_CACHE and "immutable" not in routes.REVALIDATED_CACHE
            and "31536000" in routes.IMMUTABLE_CACHE)
    r.check("a version that is not the asset's own is not its current one",
            library.canonical_version(asset.asset_id) != "1-1"
            and library.canonical_version(GOOD) == "")


def run() -> Results:
    r = Results("clipboard store")
    with tempfile.TemporaryDirectory(prefix="minipaint-clipboard-") as scratch:
        base = pathlib.Path(scratch)
        wangp_config.use_config_dir(base / "data")
        config.use_config_dir(base / "data")
        store.reset_for_tests()
        try:
            root_checks(r, base)
            import_checks(r, base)
            containment_checks(r, base)
            file_operation_checks(r, base)
            refresh_checks(r, base)
            root_change_checks(r, base)
            document_checks(r, base)
            send_route_checks(r, base)
            index_checks(r, base)
            http_door_checks(r, base)
            thumbnail_cache_checks(r, base)
            cache_header_checks(r, base)
        finally:
            wangp_config.use_config_dir(None)
            config.use_config_dir(None)
            store.reset_for_tests()
    return r


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
