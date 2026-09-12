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
    config.save(config.Config(storage_root="/x", intercept=True, sort="largest", thumbnail=999))
    loaded = config.load()
    r.check("the config keeps the intercept switch, the sort and a clamped thumbnail", loaded.intercept is True and loaded.sort == "largest" and loaded.thumbnail == config.THUMBNAIL_MAX)
    r.check("and an unknown sort falls back", config.Config.from_dict({"sort": "sideways"}).sort == config.DEFAULT_SORT)
    r.check("update changes one field and keeps the rest", config.update(intercept=False).sort == "largest" and config.load().intercept is False)
    written = config.path_of(config.CONFIG_NAME).read_text(encoding="utf-8")
    r.check("the file holds exactly the declared keys", set(json.loads(written)) == {"schema_version", "storage_root", "root_id", "intercept", "sort", "thumbnail"})


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
        finally:
            wangp_config.use_config_dir(None)
            config.use_config_dir(None)
            store.reset_for_tests()
    return r


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
