"""The public API's server half: bytes behind a token, tokens into handoffs.

Section 11 of the Clipboard design intent draws the line: a caller hands
over bytes and gets a token, hands over tokens and gets codes, and never a
path either way. So the staging route is fed a real PNG, a JPEG, a WebP, a
text file, an oversized image and a path where an image should be; the
request normaliser is fed every shape of omitted, null and empty and every
wrong kind; and preparation is watched turning a staged token and a
Clipboard asset into ordinary handoffs - the same files the Send menu
writes - and refusing the whole request when one of them cannot be made.

The routes run on a bare Starlette app through its test client; the store
and the staging root live under a temporary directory. No WanGP, no Forge,
no browser.
"""

from harness import Results, setup_path

setup_path()

import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import pathlib  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402

from PIL import Image  # noqa: E402

from minipaint_neo import interop  # noqa: E402
from minipaint_neo.clipboard import config as clipboard_config  # noqa: E402
from minipaint_neo.clipboard import store as clipboard_store  # noqa: E402
from minipaint_neo.wangp import config as wangp_config  # noqa: E402
from minipaint_neo.wangp import errors, handoff, protocol  # noqa: E402
from minipaint_neo.wangp.errors import IntegrationError  # noqa: E402

GOOD = "0123456789abcdef0123456789abcdef"
OTHER = "fedcba9876543210fedcba9876543210"


def _png(size=(6, 4), colour=(10, 200, 30, 255)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def _encoded(fmt: str, size=(6, 4)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 10, 30)).save(buffer, format=fmt)
    return buffer.getvalue()


def _refused(call, *args, **keywords) -> str:
    try:
        call(*args, **keywords)
    except IntegrationError as error:
        return error.code
    return ""


def staging_checks(r: Results) -> None:
    root = interop.staging_root()
    r.check("the staging root is under the runtime directory", root.parent == wangp_config.runtime_dir())
    if os.name != "nt":
        r.check("and is private to the user", (root.stat().st_mode & 0o777) == 0o700, oct(root.stat().st_mode & 0o777))

    for label, data in (("a PNG", _png()), ("a JPEG", _encoded("JPEG")), ("a WebP", _encoded("WEBP"))):
        answer = interop.stage_bytes(data, "application/octet-stream")
        r.check(f"{label} is staged behind a token", answer["ok"] and answer["image"]["kind"] == "staged"
                and protocol.valid_handoff_id(answer["image"]["id"]) and answer["width"] == 6, str(answer))
        path = interop.resolve_staged(answer["image"]["id"])
        r.check(f"{label} lands in the staging root as a lossless PNG",
                path.parent == root.resolve() and path.read_bytes().startswith(b"\x89PNG"))
        r.check(f"{label}'s answer names no path", "/" not in json.dumps(answer) and "\\\\" not in json.dumps(answer))

    r.check("text is not an image", _refused(interop.stage_bytes, b"hello, not an image", "image/png") == errors.IMAGE_STAGE_INVALID)
    r.check("nothing at all is not an image", _refused(interop.stage_bytes, b"", "image/png") == errors.IMAGE_STAGE_INVALID)
    r.check("a content type that is not an image is refused unread",
            _refused(interop.stage_bytes, _png(), "text/html") == errors.IMAGE_STAGE_INVALID)
    r.check("a declared image type that lies is refused decoded",
            _refused(interop.stage_bytes, b"GIF89a" + b"\x00" * 40, "image/png") == errors.IMAGE_STAGE_INVALID)
    huge = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (70000).to_bytes(4, "big") + (70000).to_bytes(4, "big") + b"\x08\x06\x00\x00\x00" + b"\x00" * 4
    r.check("an image over the side ceiling is refused from its header",
            _refused(interop.stage_bytes, huge, "image/png") in (errors.HANDOFF_TOO_LARGE, errors.IMAGE_STAGE_INVALID))
    r.check("a body over the byte ceiling is refused unread",
            _refused(interop.stage_bytes, b"\x89PNG" + b"\x00" * (interop.STAGE_MAX_BYTES + 1), "image/png") == errors.HANDOFF_TOO_LARGE)

    staged = interop.stage_image(Image.new("RGB", (3, 3), (1, 2, 3)))
    r.check("the Python helper stages a PIL image the same way", staged["ok"] and protocol.valid_handoff_id(staged["image"]["id"]))
    for label, token in (("a path", "../" + GOOD[3:]), ("a short token", GOOD[:-1]), ("upper case", GOOD.upper()), ("nothing", "")):
        r.check(f"{label} is not a staging token", _refused(interop.resolve_staged, token) == errors.IMAGE_STAGE_INVALID)
    r.check("a token nobody staged is expired, not invalid", _refused(interop.resolve_staged, OTHER) == errors.IMAGE_STAGE_EXPIRED)
    if os.name != "nt":
        outside = pathlib.Path(tempfile.mkdtemp(prefix="minipaint-outside-")) / "victim.png"
        outside.write_bytes(_png())
        link = root / (GOOD + ".png")
        os.symlink(outside, link)
        try:
            r.check("a link planted under a token's name is refused unopened",
                    _refused(interop.resolve_staged, GOOD) == errors.IMAGE_STAGE_INVALID)
        finally:
            link.unlink()
    interop.discard_staged(staged["image"]["id"])
    r.check("a discarded token is gone", _refused(interop.resolve_staged, staged["image"]["id"]) == errors.IMAGE_STAGE_EXPIRED)
    interop.discard_staged("../nothing")
    old = root / (OTHER + ".png")
    old.write_bytes(_png())
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    stranger = root / "notes.txt"
    stranger.write_text("keep me", encoding="utf-8")
    removed = interop.sweep_staging(max_age_seconds=3600)
    r.check("the sweep removes a stale staged image and nothing that is not one", removed == 1 and not old.exists() and stranger.exists())
    stranger.unlink()


def request_checks(r: Results) -> None:
    empty = interop.normalize_public_request({})
    r.check("a request with nothing in it is valid and gets an id",
            protocol.valid_request_id(empty["request_id"]) and empty["images"] == {} and "prompt" not in empty, str(empty))
    kept = interop.normalize_public_request({"request_id": GOOD, "prompt": "  a cat \x00 ", "images": {
        "start": {"kind": "staged", "id": OTHER}, "end": None, "references": [{"kind": "clipboard_asset", "id": GOOD}, None, ""]}})
    r.check("a supplied id is kept", kept["request_id"] == GOOD)
    r.check("the prompt is cleaned", kept["prompt"] == "a cat")
    r.check("handles are kept by kind and id, and null ones dropped",
            kept["images"] == {"start": {"kind": "staged", "id": OTHER}, "references": [{"kind": "clipboard_asset", "id": GOOD}]}, str(kept["images"]))
    for label, raw in (("null prompt", {"prompt": None}), ("empty prompt", {"prompt": ""}), ("whitespace prompt", {"prompt": " \n "}),
                       ("empty references", {"images": {"references": []}}), ("null images", {"images": None})):
        out = interop.normalize_public_request(raw)
        r.check(f"{label} means inherit", "prompt" not in out and out["images"] == {}, str(out))
    for label, raw, code in (
        ("a bad request id", {"request_id": "nope"}, errors.REQUEST_INVALID),
        ("a prompt that is not text", {"prompt": 5}, errors.REQUEST_INVALID),
        ("a prompt over the ceiling", {"prompt": "x" * 4001}, errors.PROMPT_TOO_LONG),
        ("an unknown image kind", {"images": {"start": {"kind": "path", "id": GOOD}}}, errors.REQUEST_INVALID),
        ("a path as an id", {"images": {"start": {"kind": "staged", "id": "/tmp/x.png"}}}, errors.REQUEST_INVALID),
        ("a bare string as a handle", {"images": {"start": GOOD}}, errors.REQUEST_INVALID),
        ("references that are not a list", {"images": {"references": {"kind": "staged", "id": GOOD}}}, errors.REQUEST_INVALID),
        ("too many references", {"images": {"references": [{"kind": "staged", "id": GOOD}] * 17}}, errors.REQUEST_INVALID),
        ("not an object", [GOOD], errors.REQUEST_INVALID),
    ):
        r.check(f"{label} is refused", _refused(interop.normalize_public_request, raw) == code)
    r.check("the contract names itself", interop.contract()["contract"] == "minipaint.wangp.queue/v1" and interop.contract()["api_version"] == 1)


def prepare_checks(r: Results, folder: pathlib.Path) -> None:
    staged = interop.stage_bytes(_png(colour=(1, 2, 3, 255)), "image/png")["image"]
    library = clipboard_store.store()
    r.check("the library takes a folder", library.set_root(str(folder), create=True)["ok"])
    asset = library.import_bytes(_png(colour=(4, 5, 6, 255)), "mine.png", "upload")

    request = interop.normalize_public_request({"request_id": GOOD, "prompt": "p", "images": {
        "start": {"kind": "staged", "id": staged["id"]},
        "references": [{"kind": "clipboard_asset", "id": asset.asset_id}]}})
    wire = interop.prepare(request)
    r.check("preparation turns handles into handoff ids",
            protocol.valid_handoff_id(wire.get("start_handoff_id")) and len(wire.get("reference_handoff_ids", [])) == 1
            and protocol.valid_handoff_id(wire["reference_handoff_ids"][0]), str(wire))
    r.check("and carries the prompt and the request id, and nothing else",
            set(wire) == {"request_id", "prompt", "start_handoff_id", "reference_handoff_ids", "handoff_ids"}, str(sorted(wire)))
    r.check("the handoffs are real files under the handoff root",
            all(handoff.resolve(item).parent == handoff.handoff_root().resolve() for item in wire["handoff_ids"]))
    r.check("with the pictures that were behind the handles",
            Image.open(handoff.resolve(wire["start_handoff_id"])).getpixel((0, 0)) == (1, 2, 3, 255)
            and Image.open(handoff.resolve(wire["reference_handoff_ids"][0])).getpixel((0, 0)) == (4, 5, 6, 255))
    r.check("the staged image is still there for a retry", interop.resolve_staged(staged["id"]).exists())
    r.check("and the durable asset is untouched", library.get(asset.asset_id) is not None)
    released = interop.release(wire["handoff_ids"] + ["not-an-id"])
    r.check("release lets the handoffs go and counts only real ids", released == 2 and handoff.manifest_of(wire["start_handoff_id"]) is None)
    r.check("and never the durable asset", library.resolve(asset.asset_id)[1].exists())

    before = set(os.listdir(handoff.handoff_root()))
    broken = interop.normalize_public_request({"images": {"start": {"kind": "clipboard_asset", "id": asset.asset_id},
                                                          "end": {"kind": "staged", "id": OTHER}}})
    r.check("a handle that cannot be resolved refuses the whole request", _refused(interop.prepare, broken) == errors.IMAGE_STAGE_EXPIRED)
    r.check("and the handoff already written for it is let go", set(os.listdir(handoff.handoff_root())) == before)
    unknown = interop.normalize_public_request({"images": {"start": {"kind": "clipboard_asset", "id": OTHER}}})
    r.check("an unknown Clipboard asset is CLIPBOARD_ASSET_UNKNOWN", _refused(interop.prepare, unknown) == errors.CLIPBOARD_ASSET_UNKNOWN)
    plain = interop.prepare(interop.normalize_public_request({"request_id": GOOD}))
    r.check("a request with no images prepares nothing and says so", plain == {"request_id": GOOD, "handoff_ids": []}, str(plain))


def route_checks(r: Results, folder: pathlib.Path) -> None:
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    app = FastAPI()

    @app.get("/{path:path}")
    async def catch_all(path: str):  # a host catch-all that must not shadow us
        return {"caught": path}

    interop.install(app)
    interop.install(app)
    r.check("the routes are installed once", sum(1 for route in app.router.routes if getattr(route, "path", "") == interop.STAGE_ROUTE) == 1)
    client = TestClient(app)

    answer = client.post(interop.STAGE_ROUTE, content=_png(), headers={"Content-Type": "image/png"}).json()
    r.check("POST /stage answers a token", answer.get("ok") and answer["image"]["kind"] == "staged" and protocol.valid_handoff_id(answer["image"]["id"]), str(answer))
    bad = client.post(interop.STAGE_ROUTE, content=b"nope", headers={"Content-Type": "image/png"})
    r.check("and a refusal with a code and a sentence, not a traceback",
            bad.status_code == 400 and bad.json().get("code") == errors.IMAGE_STAGE_INVALID and bad.json().get("message"), bad.text[:120])
    library = clipboard_store.store()
    asset = library.import_bytes(_png(colour=(7, 8, 9, 255)), "route.png", "upload")
    prepared = client.post(interop.PREPARE_ROUTE, json={"request": {"request_id": GOOD, "images": {
        "start": {"kind": "staged", "id": answer["image"]["id"]}, "references": [{"kind": "clipboard_asset", "id": asset.asset_id}]}}}).json()
    r.check("POST /prepare answers the wire request", prepared.get("ok") and protocol.valid_handoff_id(prepared["request"]["start_handoff_id"])
            and len(prepared["request"]["handoff_ids"]) == 2, str(prepared))
    r.check("and it names no path", "/" not in json.dumps(prepared["request"]))
    refused = client.post(interop.PREPARE_ROUTE, json={"request": {"images": {"start": {"kind": "path", "id": "/etc/passwd"}}}})
    r.check("a request with a path is refused with REQUEST_INVALID", refused.status_code == 400 and refused.json().get("code") == errors.REQUEST_INVALID)
    garbage = client.post(interop.PREPARE_ROUTE, content=b"{not json", headers={"Content-Type": "application/json"})
    r.check("a body that is not JSON is refused, not a traceback", garbage.status_code == 400)
    released = client.post(interop.RELEASE_ROUTE, json={"handoff_ids": prepared["request"]["handoff_ids"]}).json()
    r.check("POST /release lets them go", released.get("ok") and released.get("released") == 2, str(released))
    contract = client.get(interop.CONTRACT_ROUTE).json()
    r.check("GET /contract says what this is", contract.get("contract") == "minipaint.wangp.queue/v1")
    r.check("the host catch-all still answers everything else", client.get("/elsewhere").json() == {"caught": "elsewhere"})


def run() -> Results:
    r = Results("interop")
    with tempfile.TemporaryDirectory(prefix="minipaint-interop-") as scratch:
        base = pathlib.Path(scratch)
        wangp_config.use_config_dir(base / "data")
        clipboard_config.use_config_dir(base / "data")
        clipboard_store.reset_for_tests()
        try:
            staging_checks(r)
            request_checks(r)
            prepare_checks(r, base / "library")
            route_checks(r, base / "library")
        finally:
            wangp_config.use_config_dir(None)
            clipboard_config.use_config_dir(None)
            clipboard_store.reset_for_tests()
    return r


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
