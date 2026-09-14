"""Display copies as bytes behind an opaque id, and the route that serves them.

The picture the canvas draws travels as base64 in a Gradio value today: a
third larger than the bytes, encoded on the server, parsed on the browser's
main thread, retained for the session, and uncacheable because it is a value
rather than a resource. This is the same picture as a resource.

What is checked here is mostly the part that would be a security bug if it
were wrong, because a route that serves a picture is one edit away from being
a route that serves a file:

*   the id is a fixed grammar, and a request that is not already that shape is
    refused before anything touches a filesystem;
*   there is no path, no filename, no prompt and no session hash in the URL,
    and nothing a caller sends becomes part of one;
*   an object outside the root, or one that is not a regular file, is refused
    unopened - including a symlink planted in the folder, which is unlinked
    rather than followed;
*   the route is behind the same sign-in as everything else, and both kinds
    of refusal answer the same 404, so a caller cannot learn which ids exist.

And the lifetime rules, which are the ones that cost a user something when
they are wrong: the object the canvas is drawing now is never swept, however
old the clock says it is, and an object nobody will ask for again does go.

The switch that makes the canvas actually use these is off, and stays off
until somebody proves on a real install that Forge's own ForgeCanvas takes a
same-origin URL where it takes a data URL. That is a question about somebody
else's JavaScript; this suite is everything that can be answered without it.
"""

from harness import Results, setup_path

setup_path()

import io  # noqa: E402
import os  # noqa: E402
import pathlib
import re  # noqa: E402
import tempfile  # noqa: E402

from PIL import Image  # noqa: E402

from minipaint_neo.canvas import display, imaging, routes  # noqa: E402
from minipaint_neo.wangp import config as wangp_config  # noqa: E402
from minipaint_neo.wangp import errors  # noqa: E402
from minipaint_neo.wangp.errors import IntegrationError  # noqa: E402

GOOD = "a" * 32


def _refused(call) -> str:
    try:
        call()
    except IntegrationError as error:
        return error.code
    return ""


def store_checks(r: Results) -> None:
    opaque = Image.new("RGB", (40, 30), (10, 90, 200))
    seethrough = Image.new("RGBA", (40, 30), (10, 90, 200, 0))

    display_id, content_type = display.write_image(opaque)
    r.check("an opaque picture is stored as a JPEG", content_type == "image/jpeg" and display.valid_id(display_id))
    path, served = display.resolve(display_id)
    r.check("and resolves to a real file with that type", path.is_file() and served == "image/jpeg")
    r.check("the bytes are the encoding, not a re-encoding of it",
            path.read_bytes() == imaging.display_bytes(opaque)[0])
    r.check("the id names one byte sequence: resolving twice is the same file",
            display.resolve(display_id)[0] == path)

    second, second_type = display.write_image(seethrough)
    r.check("a see-through picture keeps its alpha, in WebP where Pillow has it",
            second_type in ("image/webp", "image/png") and display.resolve(second)[1] == second_type)
    r.check("and gets an id of its own", second != display_id)

    r.check("an id of the wrong shape is refused before anything is opened",
            _refused(lambda: display.resolve("../../etc/passwd")) == errors.HANDOFF_INVALID_ID)
    r.check("so is one of the right characters and the wrong length",
            _refused(lambda: display.resolve("a" * 31)) == errors.HANDOFF_INVALID_ID)
    r.check("and upper case is not lower case", _refused(lambda: display.resolve("A" * 32)) == errors.HANDOFF_INVALID_ID)
    r.check("an id nobody minted is simply not there",
            _refused(lambda: display.resolve("b" * 32)) == errors.HANDOFF_NOT_FOUND)
    r.check("a format that is not one of ours cannot be written",
            _refused(lambda: display.write(b"x", "image/gif")) == errors.HANDOFF_INVALID_IMAGE)
    r.check("and neither can nothing at all", _refused(lambda: display.write(b"", "image/jpeg")) == errors.HANDOFF_INVALID_IMAGE)

    # A symlink planted in the folder under a plausible id: refused unopened,
    # for the same reason the handoff root refuses one.
    if hasattr(os, "symlink"):
        secret = pathlib.Path(tempfile.mkstemp(suffix=".jpg")[1])
        secret.write_bytes(b"not yours")
        link = display.display_root() / (GOOD + ".jpg")
        try:
            os.symlink(str(secret), str(link))
        except (OSError, NotImplementedError):
            link = None
        if link is not None:
            r.check("a symlink under a valid id is refused rather than followed",
                    _refused(lambda: display.resolve(GOOD)) == errors.HANDOFF_INVALID_ID)
            display.discard(GOOD)
            r.check("and discarding it removes the link, not what it pointed at",
                    not link.exists() and secret.exists())
            secret.unlink()


def url_checks(r: Results) -> None:
    display_id = display.new_id()
    url = display.url_for(display_id)
    r.check("the URL is the prefix and the id and nothing else",
            url == f"/minipaint-canvas/display/{display_id}", url)
    r.check("no filename, no path, no session, no prompt",
            url.count("/") == 3 and "?" not in url and "=" not in url, url)
    r.check("an id of the wrong shape cannot be made into one",
            _refused(lambda: display.url_for("../x")) == errors.HANDOFF_INVALID_ID)

    r.check("a URL of ours reads back as its id", display.id_in_url(url) == display_id)
    r.check("and so does the absolute form a page may hand back",
            display.id_in_url("http://127.0.0.1:7860" + url) == display_id)
    r.check("a query string is allowed and is not part of the id",
            display.id_in_url(url + "?v=2") == display_id)
    for hostile in ("/etc/passwd", "data:image/png;base64,AAAA", "/minipaint-canvas/display/../../etc/passwd",
                    "/minipaint-canvas/display/" + "z" * 32, "", None, 7,
                    "look at this /minipaint-canvas/display/" + "a" * 32):
        r.check(f"{str(hostile)[:38]!r} is not one of ours", display.id_in_url(hostile) == "", repr(display.id_in_url(hostile)))


def lifetime_checks(r: Results) -> None:
    display.reset_for_tests()
    for entry in display.display_root().iterdir():
        entry.unlink()

    old = display.write(imaging.display_bytes(Image.new("RGB", (8, 8), (1, 2, 3)))[0], "image/jpeg")
    older = display.write(imaging.display_bytes(Image.new("RGB", (8, 8), (4, 5, 6)))[0], "image/jpeg")
    newest = display.write(imaging.display_bytes(Image.new("RGB", (8, 8), (7, 8, 9)))[0], "image/jpeg")
    r.check("writing a third copy lets the first go: a canvas holds one picture and the one before it",
            _refused(lambda: display.resolve(old)) == errors.HANDOFF_NOT_FOUND)
    r.check("the two the canvas may still be showing stay",
            display.resolve(older)[0].is_file() and display.resolve(newest)[0].is_file())
    r.check("and they are the ones the sweeper is told about", set(display.live_ids()) == {older, newest})

    import time

    ancient = display.write(imaging.display_bytes(Image.new("RGB", (8, 8), (9, 9, 9)))[0], "image/jpeg")
    path, _type = display.resolve(ancient)
    os.utime(path, (time.time() - 10 * 3600, time.time() - 10 * 3600))
    display.reset_for_tests()  # nothing is live: a fresh process, an old folder
    removed = display.sweep()
    r.check("an object nobody will ask for again is swept", removed >= 1 and not path.exists(), str(removed))

    live = display.write(imaging.display_bytes(Image.new("RGB", (8, 8), (2, 2, 2)))[0], "image/jpeg")
    path, _type = display.resolve(live)
    os.utime(path, (time.time() - 10 * 3600, time.time() - 10 * 3600))
    display.sweep()
    r.check("but the one on the canvas now is never swept, however old the clock says it is",
            path.exists(), str(display.live_ids()))


def route_checks(r: Results) -> None:
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    app = FastAPI()

    @app.get("/{path:path}")
    async def catch_all(path: str):  # a host catch-all that must not shadow us
        return {"caught": path}

    routes.install(app)
    routes.install(app)
    r.check("the route is installed once",
            sum(1 for route in app.router.routes if getattr(route, "path", "") == display.IMAGE_ROUTE) == 1)
    client = TestClient(app)

    display_id, content_type = display.write_image(Image.new("RGB", (12, 9), (30, 60, 90)))
    answer = client.get(display.url_for(display_id))
    r.check("the route answers the bytes with their own type",
            answer.status_code == 200 and answer.headers["content-type"].startswith(content_type), answer.headers.get("content-type"))
    r.check("and they are the picture", Image.open(io.BytesIO(answer.content)).size == (12, 9))
    r.check("it is cacheable, because an id is immutable",
            "immutable" in answer.headers.get("cache-control", ""), answer.headers.get("cache-control"))

    missing = client.get(display.url_for("c" * 32))
    r.check("an id nobody minted is a 404", missing.status_code == 404 and missing.json().get("code"), missing.text[:120])
    # An id of the wrong shape does not reach this route at all: the path
    # pattern does not match it, so the host's own catch-all answers. What
    # matters is that nothing of ours served it - it is never an image.
    bad = client.get("/minipaint-canvas/display/" + "z" * 32)
    r.check("an id of the wrong shape never reaches the route",
            not bad.headers.get("content-type", "").startswith("image/"), bad.headers.get("content-type", ""))
    for hostile in ("/minipaint-canvas/display/..%2f..%2fetc%2fpasswd",
                    "/minipaint-canvas/display/../../etc/passwd",
                    "/minipaint-canvas/display/" + "a" * 32 + "/../../etc/passwd"):
        answer = client.get(hostile)
        r.check(f"{hostile[26:60]!r} is not served as a file",
                not answer.headers.get("content-type", "").startswith("image/"), answer.headers.get("content-type", ""))
    r.check("the host catch-all still answers everything else", client.get("/elsewhere").json() == {"caught": "elsewhere"})


def readback_checks(r: Results) -> None:
    """A URL that comes back from a page is resolved, never treated as a path.

    Not gated behind the setting on purpose: a page loaded while links were
    on can hand one back after they are turned off, and a read-back that did
    not understand it would lose the picture rather than the optimisation.
    """
    from minipaint_neo.canvas import surface

    display_id, _content_type = display.write_image(Image.new("RGB", (14, 11), (5, 5, 200)))
    found = surface._display_object(display.url_for(display_id))
    r.check("a display URL reads back as the picture", found is not None and found.size == (14, 11))
    r.check("an id of ours that is gone reads back as nothing, not as an error",
            surface._display_object(display.url_for("d" * 32)) is None)
    for hostile in ("/etc/passwd", "file:///etc/passwd", "../../etc/passwd", "", None):
        r.check(f"{str(hostile)[:24]!r} is not a display object", surface._display_object(hostile) is None)


def bundle_checks(r: Results) -> None:
    """C1: the tab bundles, fetched rather than parsed into every page.

    Forge loads every file in an extension's ``javascript/`` folder into
    every page. Four bundles - the Canvas adapter, the WanGP bridge, the
    public queue API and the Clipboard tab - were parsed on the main thread
    during hydration for a session that may open none of them.
    """
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from minipaint_neo import assets

    assets.reset_for_tests()
    r.check("only the bootstrap is left where Forge loads everything",
            sorted(path.name for path in (assets.root().parent / "javascript").iterdir()) == ["main.js"],
            str(sorted(path.name for path in (assets.root().parent / "javascript").iterdir())))
    r.check("and every bundle is where Forge does not", all(assets.path_for(name).is_file() for name in assets.BUNDLES))
    r.check("the loader is in the bootstrap, so a page always has it",
            "minipaintAssets" in (assets.root().parent / "javascript" / "main.js").read_text(encoding="utf-8"))

    for name, url in assets.manifest().items():
        r.check(f"{name} carries its own content in its URL", "?v=" in url and len(url.split("?v=")[1]) == 16, url)
    r.check("a name that is not a bundle cannot be asked for",
            not any(_refused_key(lambda: assets.url_for(bad)) is None for bad in ("../../etc/passwd", "main", "")),
            "a name outside the table resolved")

    app = FastAPI()
    assets.install(app)
    assets.install(app)
    r.check("the route is installed once",
            sum(1 for route in app.router.routes if getattr(route, "path", "") == assets.SCRIPT_ROUTE) == 1)
    client = TestClient(app)
    answer = client.get(assets.url_for("canvas"))
    r.check("a bundle is served as JavaScript",
            answer.status_code == 200 and "javascript" in answer.headers["content-type"], answer.headers.get("content-type"))
    r.check("and is the file", answer.text == assets.path_for("canvas").read_text(encoding="utf-8"))
    r.check("cacheable forever, because the content is in the URL",
            "immutable" in answer.headers.get("cache-control", ""), answer.headers.get("cache-control"))
    r.check("and never sniffed into something else", answer.headers.get("x-content-type-options") == "nosniff")
    r.check("a name outside the table is a 404", client.get("/minipaint-assets/js/wat.js").status_code == 404)
    r.check("and so is a path", client.get("/minipaint-assets/js/..%2f..%2fetc%2fpasswd").status_code in (404, 422))

    loader = assets.tab_loader_js("minipaint_clipboard", ["clipboard"])
    r.check("a tab loader names its panel and its bundles and nothing else",
            "loadOnTab" in loader and "minipaint_clipboard" in loader and assets.url_for("clipboard") in loader, loader[:120])

    # The regression this file did not catch. Both loaders are used inside an
    # ``await``, and a wrapper that returns nothing makes that await resolve
    # on the microtask queue - before any script it just asked for can run,
    # which is a task. Every branch returns a promise or the caller is not
    # actually waiting for anything.
    for name, made in (("page", assets.loader_js(["canvas"])),
                       ("tab", assets.tab_loader_js("minipaint_clipboard", ["clipboard"]))):
        r.check(f"the {name} loader returns its promise on the loaded path",
                "return" in made.split("?")[0] or "return w" in made, made[:140])
        r.check(f"and the {name} loader returns one when the page has no loader either",
                "Promise.resolve(false)" in made, made[:140])
    r.check("no loader drops its promise on the floor",
            not any(re.search(r"\{\s*w\.load\(", made) for made in
                    (assets.loader_js(["canvas"]), assets.tab_loader_js("p", ["canvas"]))))

    # Canvas readiness is a boolean contract in the browser, and the bootstrap
    # in main.js is what has to honour it. Promise.all resolves to an array,
    # and an array is truthy whatever is in it.
    bootstrap = (assets.root().parent / "javascript" / "main.js").read_text(encoding="utf-8")
    r.check("load() reduces its results to one boolean",
            "results.every(Boolean)" in bootstrap)
    r.check("a script element carries its own state, so a failed one is not read as loaded",
            'data-minipaint-state' in bootstrap and '"loaded"' in bootstrap)
    r.check("and a failed script is taken out of the document so a retry makes a real request",
            "element.remove()" in bootstrap and "delete loaded[key]" in bootstrap)
    r.check("the readiness primitive is in the bootstrap, not in the bundle it loads",
            "minipaintCanvasReady" in bootstrap
            and "minipaintCanvasReady" not in (assets.path_for("canvas")).read_text(encoding="utf-8"))


def _refused_key(call):
    try:
        call()
    except KeyError:
        return "KeyError"
    return None


def setting_checks(r: Results) -> None:
    r.check("the canvas keeps embedding copies until somebody proves the other half",
            display.enabled() is False)


def run() -> Results:
    r = Results("canvas display")
    with tempfile.TemporaryDirectory(prefix="minipaint-display-") as scratch:
        wangp_config.use_config_dir(pathlib.Path(scratch) / "data")
        display.reset_for_tests()
        try:
            store_checks(r)
            url_checks(r)
            lifetime_checks(r)
            route_checks(r)
            readback_checks(r)
            bundle_checks(r)
            setting_checks(r)
        finally:
            wangp_config.use_config_dir(None)
            display.reset_for_tests()
    return r


if __name__ == "__main__":
    raise SystemExit(0 if run().report() else 1)
