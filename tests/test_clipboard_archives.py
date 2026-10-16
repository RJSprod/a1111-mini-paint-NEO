"""Archives: folders on the Forge host the Clipboard shows and never writes to.

What is held here, server side:

* the list - a folder added once, kept on the host, refused when it is not a
  folder, is missing, is the storage folder itself; removing one leaves the
  folder exactly as it was; the view moves between the storage folder and an
  archive and falls back to the storage folder when its archive is removed;
* the listing - every picture in one answer, read from ``scandir`` alone (a
  file whose name lies about it is listed, and nothing is opened), hidden
  files, links, other suffixes, empty and oversized files left out, in the
  storage folder's six orders;
* the references - a picture named by its archive's id and its file name,
  the same after a restart, and every way of naming something else refused:
  a separator, a dot file, a link, a file that has gone, an archive that is
  not in the list;
* the thumbnails - made once (memory, then the extension's own disk cache,
  then Pillow), made once even when asked twice at the same moment, upright,
  never written into the archive, swept within a budget of their own;
* read-only - after listing, thumbnailing, sending and copying, every file in
  the archive has the bytes and the time it had, and nothing was added;
* the copy into the storage folder that a slot card needs - byte for byte,
  and only once for the same bytes;
* sending - img2img gets the picture's pixels, PNG Info gets the file's own
  bytes so its generation parameters survive, Mini Paint gets the picture
  through the Canvas's own receive;
* the routes on a bare app: the list, the listing, a thumbnail blessed
  immutable only for its current version, the full picture, the send route
  naming an archive picture.
"""

from harness import Results, setup_path

setup_path()

import base64  # noqa: E402
import io  # noqa: E402
import os  # noqa: E402
import pathlib  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

from PIL import Image, PngImagePlugin  # noqa: E402

from minipaint_neo.clipboard import archives, config, routes, store  # noqa: E402
from minipaint_neo.wangp import config as wangp_config  # noqa: E402
from minipaint_neo.wangp import errors  # noqa: E402
from minipaint_neo.wangp.errors import IntegrationError  # noqa: E402

PARAMETERS = "a lighthouse at dusk\nSteps: 20, Sampler: Euler a, CFG scale: 7, Seed: 1234"


def _png(colour=(10, 200, 30), size=(64, 48), text=None) -> bytes:
    buffer = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    if text:
        info.add_text("parameters", text)
    Image.new("RGB", size, colour).save(buffer, format="PNG", pnginfo=info)
    return buffer.getvalue()


def _jpeg(size=(64, 48), orientation=None) -> bytes:
    buffer = io.BytesIO()
    image = Image.new("RGB", size, (200, 10, 30))
    if orientation:
        exif = Image.Exif()
        exif[0x0112] = orientation
        image.save(buffer, format="JPEG", exif=exif.tobytes())
    else:
        image.save(buffer, format="JPEG")
    return buffer.getvalue()


def _refused(call, *args, **keywords) -> str:
    try:
        call(*args, **keywords)
    except IntegrationError as error:
        return error.code
    return ""


def _snapshot(folder: pathlib.Path) -> dict:
    """Every entry in a folder: its bytes' digest, size and modification time."""
    import hashlib

    found = {}
    for entry in sorted(folder.iterdir()):
        info = os.lstat(entry)
        digest = hashlib.sha256(entry.read_bytes()).hexdigest() if entry.is_file() and not entry.is_symlink() else ""
        found[entry.name] = (digest, info.st_size, info.st_mtime_ns)
    return found


def _seed(folder: pathlib.Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "b-meta.png").write_bytes(_png(text=PARAMETERS))
    (folder / "a-photo.jpg").write_bytes(_jpeg(size=(400, 200), orientation=6))
    (folder / "C-wide.webp").write_bytes(_png(size=(10, 10)))  # a PNG named .webp: read for what it is
    (folder / "broken.jpg").write_bytes(b"not a picture at all, whatever the name says")
    (folder / "ünïcode name: odd.png").write_bytes(_png(colour=(1, 2, 3)))
    (folder / ".hidden.png").write_bytes(_png())
    (folder / "notes.txt").write_text("not a picture", encoding="utf-8")
    (folder / "empty.png").write_bytes(b"")
    (folder / "sub").mkdir(exist_ok=True)
    (folder / "sub" / "nested.png").write_bytes(_png())
    # Times far enough apart that the orders are unambiguous.
    for offset, name in enumerate(("b-meta.png", "a-photo.jpg", "C-wide.webp", "broken.jpg", "ünïcode name: odd.png")):
        stamp = 1_700_000_000 + offset * 1000
        os.utime(folder / name, (stamp, stamp))
    try:
        os.symlink(folder / "b-meta.png", folder / "link.png")
    except (OSError, NotImplementedError):
        pass


# ---------------------------------------------------------------- the list --


def list_checks(r: Results, base: pathlib.Path, archive: pathlib.Path) -> None:
    r.check("no archive is listed before one is added", archives.all_archives() == [])
    r.check("an empty path is refused", archives.add("")["code"] == "ARCHIVE_EMPTY")
    r.check("a folder that does not exist is refused, and not created",
            archives.add(str(base / "nowhere"))["code"] == "ARCHIVE_MISSING" and not (base / "nowhere").exists())
    a_file = base / "a-file.png"
    a_file.write_bytes(_png())
    r.check("a file is not a folder", archives.add(str(a_file))["code"] == "ARCHIVE_NOT_A_DIRECTORY")
    storage = base / "storage"
    store.store().set_root(str(storage), create=True)
    r.check("the storage folder is not an archive of itself", archives.add(str(storage))["code"] == "ARCHIVE_IS_STORAGE")

    before = _snapshot(archive)
    added = archives.add(str(archive))
    r.check("a folder is added", added["ok"] and added["added"] and added["archive"]["name"] == archive.name, str(added))
    archive_id = added["archive"]["id"]
    r.check("its id is a digest of where it is, not its path", len(archive_id) == 16 and str(archive) not in archive_id)
    r.check("and it is kept on the host, with its path for the Archives screen",
            config.load().archives == [{"id": archive_id, "path": str(archive.resolve()), "name": archive.name}],
            str(config.load().archives))
    again = archives.add(str(archive) + os.sep)
    r.check("adding the same folder again adds nothing", again["ok"] and again["added"] is False and len(config.load().archives) == 1)
    twin = base / "twin" / archive.name
    twin.mkdir(parents=True)
    second = archives.add(str(twin))
    r.check("two folders with one name are told apart on the screen",
            second["ok"] and second["archive"]["name"] == archive.name + " (2)", str(second))
    r.check("the list answers with every archive and whether it can be read",
            [one["available"] for one in archives.all_archives()] == [True, True])

    shown = archives.set_view(archive_id)
    r.check("an archive can be shown in place of the storage folder",
            shown["ok"] and config.load().view == archive_id and archives.current_view() == archive_id, str(shown))
    r.check("and an id that is not an archive cannot", archives.set_view("0" * 16)["code"] == errors.ARCHIVE_UNKNOWN)
    archives.set_view(second["archive"]["id"])
    removed = archives.remove(second["archive"]["id"])
    r.check("removing the archive on screen puts the storage folder back on screen",
            removed["ok"] and config.load().view == "" and len(config.load().archives) == 1, str(removed))
    r.check("and the folder itself is still there", twin.is_dir())
    archives.set_view(archive_id)
    r.check("nothing was written into the archive by adding it", _snapshot(archive) == before)

    facts = routes.menu_facts()
    r.check("the menu draws the archives and the view from the same facts as the sort",
            facts.get("archives") == [[archive_id, archive.name]] and facts.get("view") == archive_id, str(facts))
    broken = config.Config.from_dict({"archives": [{"id": "nothex", "path": "/x", "name": "x"}, "junk",
                                                    {"id": archive_id, "path": "", "name": "y"}], "view": "zz"})
    r.check("a hand-edited list keeps only real records, and a view that is not an id is the storage folder",
            broken.archives == [] and broken.view == "", str(broken.archives))


# ------------------------------------------------------------- the listing --


def listing_checks(r: Results, archive: pathlib.Path) -> str:
    archive_id = archives.current_view()
    made_before = archives.thumbnails().made
    opened = []
    real_open = Image.open

    def spying_open(*args, **keywords):
        opened.append(args[0] if args else "")
        return real_open(*args, **keywords)

    import builtins

    real_builtin_open = builtins.open
    real_read_bytes = pathlib.Path.read_bytes

    def spying_builtin_open(file, *args, **keywords):
        if str(archive) in str(file):
            opened.append(str(file))
        return real_builtin_open(file, *args, **keywords)

    def spying_read_bytes(self):
        if str(archive) in str(self):
            opened.append(str(self))
        return real_read_bytes(self)

    Image.open = spying_open
    builtins.open = spying_builtin_open
    pathlib.Path.read_bytes = spying_read_bytes
    try:
        answer = archives.listing(archive_id, "name_asc")
    finally:
        Image.open = real_open
        builtins.open = real_builtin_open
        pathlib.Path.read_bytes = real_read_bytes
    names = [item[1] for item in answer["items"]]
    r.check("every picture is in one answer, in the order asked for, names compared without case",
            names == ["a-photo.jpg", "b-meta.png", "broken.jpg", "C-wide.webp", "ünïcode name: odd.png"], str(names))
    r.check("hidden files, links, other suffixes, folders and empty files are left out",
            not {".hidden.png", "link.png", "notes.txt", "sub", "empty.png", "nested.png"} & set(names))
    r.check("an empty file is counted as skipped", answer["skipped"] == 1, str(answer["skipped"]))
    r.check("nothing was opened to list it - not even the file that is no picture at all",
            opened == [] and archives.thumbnails().made == made_before, str(opened[:3]))
    first = answer["items"][0]
    info = os.stat(archive / "a-photo.jpg")
    r.check("each item is its key, its name, its size and the version its thumbnail URL carries",
            first[1] == "a-photo.jpg" and first[2] == info.st_size and first[3] == f"{info.st_mtime_ns}-{info.st_size}", str(first))
    r.check("and says where it is from and how many", answer["archive"]["id"] == archive_id and answer["total"] == 5
            and "read-only" in answer["status"], str(answer["status"]))
    # The status line is where the grid says which folder it shows - there is
    # no row over the grid for it - so it says where new pictures go as well.
    r.check("and that new pictures go to the storage folder", "storage folder" in answer["status"], str(answer["status"]))
    orders = {mode: [item[1] for item in archives.listing(archive_id, mode)["items"]] for mode in config.SORT_MODES}
    r.check("newest first is by modification time", orders["newest"][0] == "ünïcode name: odd.png" and orders["oldest"][0] == "b-meta.png", str(orders))
    r.check("name Z-A is the reverse of A-Z", orders["name_desc"] == list(reversed(orders["name_asc"])))
    r.check("largest and smallest are by size",
            [os.stat(archive / n).st_size for n in orders["largest"]] == sorted((os.stat(archive / n).st_size for n in orders["largest"]), reverse=True))
    r.check("a sort that is not one falls back to the stored one", archives.listing(archive_id, "bogus")["sort"] == config.load().sort)
    r.check("an archive not in the list is ARCHIVE_UNKNOWN", _refused(archives.listing, "f" * 16) == errors.ARCHIVE_UNKNOWN)
    return archive_id


# ----------------------------------------------------------- references ----


def reference_checks(r: Results, archive: pathlib.Path, archive_id: str) -> None:
    for name in ("b-meta.png", "ünïcode name: odd.png"):
        ref = archives.ref_for(archive_id, name)
        r.check(f"a reference names its picture and carries no colon or path ({name})",
                ref.startswith("ar-" + archive_id + "-") and ":" not in ref and "/" not in ref and archives.parse_ref(ref) == (archive_id, name))
        record, found, path, _info = archives.resolve(ref)
        r.check(f"and resolves to that file, inside its archive ({name})",
                record["id"] == archive_id and found == name and path == (archive / name).resolve())
    r.check("the same picture is the same reference every time - a restart changes nothing",
            archives.ref_for(archive_id, "b-meta.png") == archives.ref_for(archive_id, "b-meta.png"))
    r.check("a library id is not a reference, and a reference is not a library id",
            not archives.is_ref("0123456789abcdef0123456789abcdef") and not store.protocol.valid_handoff_id(archives.ref_for(archive_id, "b-meta.png")))

    def crafted(name: str) -> str:
        return "ar-" + archive_id + "-" + base64.urlsafe_b64encode(name.encode("utf-8")).rstrip(b"=").decode("ascii")

    r.check("a name with a separator is refused before it is a path",
            _refused(archives.resolve, crafted("../storage/x.png")) == errors.ARCHIVE_FILE_OUTSIDE
            and _refused(archives.resolve, crafted("sub/nested.png")) == errors.ARCHIVE_FILE_OUTSIDE
            and _refused(archives.resolve, crafted("..\\x.png")) == errors.ARCHIVE_FILE_OUTSIDE)
    r.check("a dot file is not an archive picture", _refused(archives.resolve, crafted(".hidden.png")) == errors.ARCHIVE_FILE_OUTSIDE)
    r.check("nor is a file of another kind", _refused(archives.resolve, crafted("notes.txt")) == errors.ARCHIVE_FILE_OUTSIDE)
    if (archive / "link.png").is_symlink():
        r.check("a link is refused whatever it points at", _refused(archives.resolve, crafted("link.png")) == errors.ARCHIVE_FILE_OUTSIDE)
    r.check("a file that has gone is ARCHIVE_FILE_UNKNOWN", _refused(archives.resolve, crafted("gone.png")) == errors.ARCHIVE_FILE_UNKNOWN)
    r.check("an archive that is not in the list is ARCHIVE_UNKNOWN",
            _refused(archives.resolve, "ar-" + "e" * 16 + "-YS5wbmc") == errors.ARCHIVE_UNKNOWN)
    r.check("and a value that is not a reference at all is refused", _refused(archives.resolve, "ar-zz-..") == errors.ARCHIVE_FILE_UNKNOWN)


# ------------------------------------------------------------ thumbnails ----


def thumbnail_checks(r: Results, archive: pathlib.Path, archive_id: str) -> None:
    archives.reset_for_tests()
    cache = archives.thumbnails()
    ref = archives.ref_for(archive_id, "a-photo.jpg")
    data, mime = cache.thumbnail(ref)
    r.check("a thumbnail is made for an archive picture", mime == "image/webp" and len(data) > 0 and cache.made == 1)
    with Image.open(io.BytesIO(data)) as small:
        size = small.size
    r.check("upright, as the camera meant it: a 400x200 photograph turned by its EXIF is taller than wide",
            size[1] > size[0] and max(size) <= archives.THUMBNAIL_SIDE, str(size))
    cache.thumbnail(ref)
    r.check("asked again, it comes from memory", cache.made == 1)
    cache.forget()
    cache.thumbnail(ref)
    r.check("and after a restart, from the disk cache - never made twice", cache.made == 1)
    directory = cache.directory()
    r.check("the disk cache is the extension's own, beside the library's and not inside the archive",
            directory is not None and directory.name == archives.THUMBNAIL_DIR_NAME
            and directory.parent == config.config_dir() and archive not in directory.parents)
    r.check("its file names carry the identity and no name or path",
            all(archives.THUMBNAIL_NAME_RE.match(one.name) and "photo" not in one.name for one in directory.glob("*")))

    # Two asks for one thumbnail at the same moment make it once.
    archives.reset_for_tests()
    cache = archives.thumbnails()
    for one in directory.glob("*"):
        one.unlink()
    gate = threading.Event()
    real_make = cache._make

    def slow_make(*args, **keywords):
        gate.wait(5)
        return real_make(*args, **keywords)

    cache._make = slow_make
    answers = []
    threads = [threading.Thread(target=lambda: answers.append(cache.thumbnail(ref))) for _ in range(3)]
    for thread in threads:
        thread.start()
    time.sleep(0.2)
    gate.set()
    for thread in threads:
        thread.join(10)
    r.check("three asks at once for one thumbnail make it once, and all three get it",
            cache.made == 1 and len(answers) == 3 and all(answer == answers[0] for answer in answers), str(cache.made))
    cache._make = real_make

    r.check("a PNG under a .webp name is read for what it is", cache.thumbnail(archives.ref_for(archive_id, "C-wide.webp"))[1] == "image/webp")
    r.check("a file that is no picture has no thumbnail, and says so with a code",
            _refused(cache.thumbnail, archives.ref_for(archive_id, "broken.jpg")) == errors.HANDOFF_INVALID_IMAGE)
    swept = cache.sweep(budget=0)
    r.check("the archive cache is swept within its own budget", swept["ok"] and swept["bytes"] == 0, str(swept))
    r.check("a budget of its own, far larger than the library's",
            archives.THUMBNAIL_DISK_BUDGET >= 8 * store.THUMBNAIL_DISK_BUDGET)


# -------------------------------------------------------- copy and send ----


def copy_and_send_checks(r: Results, base: pathlib.Path, archive: pathlib.Path, archive_id: str) -> None:
    from minipaint_neo.clipboard import ui as clip_ui

    library = store.store()
    ref = archives.ref_for(archive_id, "b-meta.png")
    asset = archives.adopt(ref)
    copied = library.root() / asset.filename
    r.check("copied into the storage folder, byte for byte, its generation parameters with it",
            copied.read_bytes() == (archive / "b-meta.png").read_bytes() and asset.source == "archive", str(asset))
    again = archives.adopt(ref)
    r.check("and the same picture again is the same asset, not a second copy",
            again.asset_id == asset.asset_id and len([a for a in library.assets() if a.sha256 == asset.sha256]) == 1)

    plan = clip_ui.send_plan("img2img", ref)
    r.check("an archive picture is sent to img2img as its pixels", plan.get("ok") is True and str(plan.get("payload", "")).startswith("data:image/png"), str(plan)[:200])
    r.check("named by its file name in the plan", plan.get("filename") == "b-meta.png", str(plan.get("filename")))
    plan = clip_ui.send_plan("pnginfo", ref)
    payload = str(plan.get("payload", ""))
    raw = base64.b64decode(payload.split(",", 1)[1]) if payload.startswith("data:") else b""
    r.check("PNG Info is sent the file's own bytes, so its parameters survive",
            plan.get("ok") is True and raw == (archive / "b-meta.png").read_bytes() and plan.get("elem") == "pnginfo_image"
            and plan.get("backend") is False, str(plan)[:200])
    jpeg_plan = clip_ui.send_plan("pnginfo", archives.ref_for(archive_id, "a-photo.jpg"))
    r.check("a JPEG goes to PNG Info as a JPEG, with its own type", str(jpeg_plan.get("payload", "")).startswith("data:image/jpeg;base64,"))
    library_plan = clip_ui.send_plan("pnginfo", asset.asset_id)
    raw = base64.b64decode(str(library_plan.get("payload", "")).split(",", 1)[1])
    r.check("and a storage-folder picture goes to PNG Info as its own bytes too", raw == copied.read_bytes())
    r.check("a reference to nothing is refused like an unknown id",
            clip_ui.send_plan("img2img", "ar-" + "e" * 16 + "-YS5wbmc").get("ok") is False)

    tab = clip_ui.ClipboardTab()
    picture = tab._picture_for_canvas(f"{ref}:abc123")
    r.check("Mini Paint's receive opens an archive picture by its reference", picture is not None and picture.size == (64, 48))
    draft, message = tab._assign("first", ref)
    r.check("a slot card given an archive picture holds the storage folder's copy, and says so",
            draft.get("first_asset_id") == asset.asset_id and "copied" in message, message)


def read_only_checks(r: Results, archive: pathlib.Path, before: dict) -> None:
    r.check("after listing, thumbnailing, copying and sending, the archive is exactly as it was",
            _snapshot(archive) == before, str(set(_snapshot(archive)) ^ set(before)))


# ---------------------------------------------------------------- routes ---


def route_checks(r: Results, archive: pathlib.Path, archive_id: str) -> None:
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    app = FastAPI()
    routes.install(app)
    client = TestClient(app)

    answer = client.get(routes.ARCHIVES_ROUTE).json()
    r.check("the list route answers with the archives, the view and the menu's facts",
            answer.get("ok") and [one["id"] for one in answer["archives"]] == [archive_id] and answer.get("view") == archive_id
            and answer.get("menu", {}).get("archives"), str(answer)[:200])
    listed = client.get(routes.ARCHIVE_LIST_ROUTE, params={"id": archive_id, "sort": "name_asc"})
    body = listed.json()
    r.check("the listing route answers every picture at once, never cached",
            listed.status_code == 200 and body.get("total") == 5 and listed.headers.get("cache-control") == "no-store", str(body)[:120])
    item = body["items"][1]
    ref = archives.REF_PREFIX + archive_id + "-" + item[0]
    url = routes.archive_image_url(ref, thumb=True, version=item[3])
    thumb = client.get(url)
    r.check("a thumbnail with its current version is immutable for a year",
            thumb.status_code == 200 and thumb.headers.get("content-type") == "image/webp"
            and thumb.headers.get("cache-control") == routes.IMMUTABLE_CACHE, str(thumb.headers))
    stale = client.get(routes.archive_image_url(ref, thumb=True, version="1-1"))
    r.check("and with any other version, only for the careful hour", stale.headers.get("cache-control") == routes.REVALIDATED_CACHE)
    full = client.get(routes.archive_image_url(ref, thumb=False, version=item[3]))
    r.check("the full picture is the file's own bytes, for the full-screen view",
            full.status_code == 200 and full.content == (archive / item[1]).read_bytes(), str(full.status_code))
    missing = client.get(routes.archive_image_url("ar-" + archive_id + "-Z29uZS5wbmc", thumb=True))
    r.check("a picture that is not there is a 404", missing.status_code == 404)
    broken = client.get(routes.archive_image_url(archives.ref_for(archive_id, "broken.jpg"), thumb=True))
    r.check("and one that is no picture a 415, so the tile is marked rather than asked for again", broken.status_code == 415, str(broken.status_code))
    outside = client.get(routes.ARCHIVE_IMAGE_ROUTE.replace("{ref}", "ar-" + archive_id + "-" + base64.urlsafe_b64encode(b"../x.png").rstrip(b"=").decode()) + "?thumb=1")
    r.check("and one outside the archive a 403", outside.status_code == 403, str(outside.status_code))
    added = client.post(routes.ARCHIVES_ROUTE, json={"action": "add", "path": str(archive / "nope")}).json()
    r.check("adding a folder that is not there is refused with its reason", added.get("ok") is False and added.get("code") == "ARCHIVE_MISSING")
    viewed = client.post(routes.ARCHIVES_ROUTE, json={"action": "view", "id": ""}).json()
    r.check("the view goes back to the storage folder over the route", viewed.get("ok") and config.load().view == "" and viewed.get("menu", {}).get("view") == "")
    client.post(routes.ARCHIVES_ROUTE, json={"action": "view", "id": archive_id})
    sent = client.post(routes.SEND_ROUTE, json={"target": "img2img", "asset": ref}).json()
    r.check("the send route takes an archive picture", sent.get("ok") is True and str(sent.get("payload", "")).startswith("data:image/"), str(sent)[:160])
    unknown = client.post(routes.ARCHIVES_ROUTE, json={"action": "explode"})
    r.check("an action that is not one is refused", unknown.status_code == 400)


def run() -> Results:
    r = Results("clipboard archives")
    with tempfile.TemporaryDirectory(prefix="minipaint-archives-") as scratch:
        base = pathlib.Path(scratch)
        wangp_config.use_config_dir(base / "data")
        config.use_config_dir(base / "data")
        store.reset_for_tests()
        archives.reset_for_tests()
        archive = base / "Photos 2019"
        _seed(archive)
        try:
            # A page built with PNG Info on it, so the send plan has a
            # destination to name - the way the host builds it.
            import forge_like
            from modules import script_callbacks, shared
            from minipaint_neo import router, settings
            from minipaint_neo.canvas import host as canvas_host
            from minipaint_neo.clipboard import ui as clip_ui

            shared.opts.data[settings.USE_OLD_UI] = False
            script_callbacks.callbacks["after_component"][:] = [canvas_host.on_after_component]
            canvas_host.reset_capture()
            list_checks(r, base, archive)
            before = _snapshot(archive)
            forge_like.build_host(lambda: (router.on_ui_tabs() or []) + (clip_ui.on_ui_tabs() or []))
            tab = clip_ui.current()
            r.check("PNG Info is a destination the Clipboard offers, where the host has one",
                    tab is not None and ("pnginfo", "PNG Info") in tab.destinations, str(tab.destinations if tab else None))
            archive_id = listing_checks(r, archive)
            reference_checks(r, archive, archive_id)
            thumbnail_checks(r, archive, archive_id)
            copy_and_send_checks(r, base, archive, archive_id)
            route_checks(r, archive, archive_id)
            read_only_checks(r, archive, before)
        finally:
            wangp_config.use_config_dir(None)
            config.use_config_dir(None)
            store.reset_for_tests()
            archives.reset_for_tests()
    return r


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
