"""Thirty-two hex characters, and everything they are not allowed to name.

The handoff is the one place where something the browser hands back turns
into a file this process opens, so the FILES half of section 42 lives here:
a traversal id, a slash, a backslash, a symlink out of the directory, a file
that is not a PNG, a PNG that stops halfway, an image over the ceilings, and
a file that changed between being prepared and being read. Every one of them
has to be refused rather than cleaned up into something that works, because
a sanitiser is an argument with the input and a fixed grammar is not.

Everything runs against a temporary directory through ``config.use_config_dir``.
No WanGP, no Forge, no network, and nothing outside the temp directory is
written to - several of the checks exist precisely to prove that last part.
"""

from harness import Results, setup_path

setup_path()

import hashlib  # noqa: E402
import os  # noqa: E402
import pathlib  # noqa: E402
import struct  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402

from PIL import Image  # noqa: E402

from minipaint_neo.canvas import imaging  # noqa: E402
from minipaint_neo.wangp import config, errors, handoff, protocol  # noqa: E402

#: Well-formed ids used where the shape is not what is being tested.
ID_A = "0123456789abcdef0123456789abcdef"
ID_B = "fedcba9876543210fedcba9876543210"
ID_C = "aaaaaaaabbbbbbbbccccccccdddddddd"
ID_D = "11112222333344445555666677778888"
ID_E = "99990000aaaabbbbccccddddeeeeffff"
ID_F = "0f0f0f0f1e1e1e1e2d2d2d2d3c3c3c3c"

#: Every shape the module must refuse outright. The last three are the ones
#: a sanitiser would "fix" into something openable.
BAD_IDS = (
    ("empty", ""),
    ("a dot", "."),
    ("two dots", ".."),
    ("a traversal", "../" + "a" * 29),
    ("a traversal with a suffix", "../../etc/passwd"),
    ("a forward slash", "0123456789abcdef/123456789abcdef"),
    ("a backslash", "0123456789abcdef\\123456789abcdef"),
    ("an absolute path", "/etc/passwd"),
    ("a windows path", "C:\\Windows\\System32"),
    ("31 characters", "0123456789abcdef0123456789abcde"),
    ("33 characters", "0123456789abcdef0123456789abcdef0"),
    ("upper case", ID_A.upper()),
    ("mixed case", "0123456789ABCDEF0123456789abcdef"),
    ("non-hex letters", "g" * 32),
    ("a png suffix inside the id", "0" * 28 + ".png"),
    ("a null byte", "0123456789abcdef0123456789abcde\x00"),
    ("a trailing newline", ID_A + "\n"),
    ("leading whitespace", " " + ID_A[1:]),
    ("a url", "http://127.0.0.1:7860/x"),
    ("bytes rather than text", ID_A.encode("ascii")),
    ("a number", 12345),
    ("nothing at all", None),
)


class FakeImage:
    """Something with a ``.size`` - the ceilings are checked before encoding."""

    def __init__(self, size):
        self.size = size


def failure(call, *args, **kwargs):
    """The IntegrationError a call raised, or None when it returned."""
    try:
        call(*args, **kwargs)
    except errors.IntegrationError as error:
        return error
    return None


def code_of(error) -> str:
    return getattr(error, "code", "")


def detail_of(error) -> str:
    return getattr(error, "detail", "")


def forget_everything() -> None:
    """Drop this process's in-memory manifests between groups of checks."""
    handoff._manifests.clear()


def entries(directory) -> set:
    return {child.name for child in os.scandir(directory)} if os.path.isdir(directory) else set()


def picture(width=9, height=7, color=(1, 2, 3)) -> Image.Image:
    return Image.new("RGB", (width, height), color)


def real_png(width=9, height=7) -> bytes:
    return imaging.to_png_bytes(imaging.to_rgba(picture(width, height)))


def lying_png(width, height) -> bytes:
    """A PNG header that announces an enormous image and stops there.

    The point of the IHDR check is that the dimensions are read before the
    decoder is asked for that much memory, so this file must be refused for
    being too large rather than for being truncated.
    """
    return handoff.PNG_SIGNATURE + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height)


def backdate(path, seconds: float) -> None:
    moment = time.time() - seconds
    os.utime(path, (moment, moment))


def can_symlink(base) -> bool:
    probe = pathlib.Path(base) / "symlink-probe"
    try:
        os.symlink(str(base), str(probe))
    except (OSError, NotImplementedError, AttributeError):
        return False
    os.unlink(str(probe))
    return True


# --------------------------------------------------------------------------


def check_ids(r: Results) -> None:
    """The id is the whole transport, so its grammar is the whole check."""
    minted = handoff.new_id()
    r.check("a minted id is 32 lowercase hex", bool(protocol.HANDOFF_ID_RE.match(minted)))
    r.check("a minted id is nothing but hex", len(minted) == 32 and minted == minted.lower())
    r.check("two ids differ", handoff.new_id() != handoff.new_id())
    r.check("a minted id survives its own validator", protocol.valid_handoff_id(minted))

    root = handoff.handoff_root()
    r.check("the root is the handoff folder under the runtime dir",
            root == config.runtime_dir() / handoff.DIRECTORY_NAME and root.is_dir())
    r.check("a valid id names a file directly under the root",
            handoff.path_for(ID_A) == root / (ID_A + protocol.HANDOFF_SUFFIX))
    r.check("the filename is derived from the id, never given",
            handoff.path_for(ID_A).name == ID_A + ".png" and handoff.path_for(ID_A).parent == root)

    for label, value in BAD_IDS:
        error = failure(handoff.path_for, value)
        r.check(f"path_for refuses {label}", code_of(error) == errors.HANDOFF_INVALID_ID, repr(value))
        r.check(f"resolve refuses {label} too", code_of(failure(handoff.resolve, value)) == errors.HANDOFF_INVALID_ID)
        r.check(f"{label} is not remembered", handoff.manifest_of(value) is None)

    # Refused, not repaired: no cleaned-up form of a bad id may be openable.
    traversal = failure(handoff.path_for, "../" + "a" * 29)
    r.check("a traversal is rejected rather than sanitised",
            code_of(traversal) == errors.HANDOFF_INVALID_ID and "32 lowercase hex" in detail_of(traversal))
    r.check("the sentence a user sees never quotes the id",
            traversal.user_message == errors.MESSAGES[errors.HANDOFF_INVALID_ID] and ".." not in traversal.user_message)
    r.check("an id shaped like a file this module writes is still not one",
            code_of(failure(handoff.path_for, ID_A + ".png")) == errors.HANDOFF_INVALID_ID)


def check_resolve(r: Results, base: pathlib.Path) -> None:
    """Existence, plain-file-ness, and staying inside the root."""
    forget_everything()
    root = handoff.handoff_root()

    real = root / (ID_A + protocol.HANDOFF_SUFFIX)
    real.write_bytes(real_png())
    r.check("a real file under the root resolves", handoff.resolve(ID_A) == real.resolve())

    r.check("a valid id with no file is not found",
            code_of(failure(handoff.resolve, ID_B)) == errors.HANDOFF_NOT_FOUND)
    r.check("not-found is a timing accident, not an invalid id",
            errors.MESSAGES[errors.HANDOFF_NOT_FOUND] != errors.MESSAGES[errors.HANDOFF_INVALID_ID])

    (root / (ID_C + protocol.HANDOFF_SUFFIX)).mkdir()
    r.check("a directory under our name is refused",
            code_of(failure(handoff.resolve, ID_C)) == errors.HANDOFF_INVALID_ID)

    outside = base / "outside" / "secret.png"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(real_png(4, 4))

    if can_symlink(base):
        os.symlink(str(outside), str(root / (ID_D + protocol.HANDOFF_SUFFIX)))
        error = failure(handoff.resolve, ID_D)
        r.check("a symlink pointing out of the root is refused", code_of(error) == errors.HANDOFF_INVALID_ID)
        r.check("the file it pointed at was not touched", outside.is_file() and outside.read_bytes() == real_png(4, 4))
        r.check("the refusal says it is not a regular file", "regular file" in detail_of(error))

        os.symlink(str(real), str(root / (ID_E + protocol.HANDOFF_SUFFIX)))
        r.check("even a symlink to a file inside the root is refused",
                code_of(failure(handoff.resolve, ID_E)) == errors.HANDOFF_INVALID_ID)

        dangling = root / (ID_F + protocol.HANDOFF_SUFFIX)
        os.symlink(str(base / "nothing-here.png"), str(dangling))
        r.check("a dangling symlink is refused rather than reported missing",
                code_of(failure(handoff.resolve, ID_F)) == errors.HANDOFF_INVALID_ID)
    else:  # pragma: no cover - Windows without developer mode
        r.check("symlink checks were skipped, not passed", True, "this platform cannot create symlinks")

    for label, value in (("a traversal", ".."), ("a slash", "a/b"), ("a backslash", "a\\b")):
        r.check(f"resolve never opens {label}",
                code_of(failure(handoff.resolve, value)) == errors.HANDOFF_INVALID_ID)


def check_write(r: Results, base: pathlib.Path) -> None:
    """A real PNG, a manifest that describes it, and nothing else on disk."""
    forget_everything()
    root = handoff.handoff_root()
    for child in os.scandir(root):
        (os.unlink if child.is_file(follow_symlinks=False) or child.is_symlink() else os.rmdir)(child.path)

    prepared = handoff.write(picture(9, 7, (10, 20, 30)))
    r.check("write mints a valid id", protocol.valid_handoff_id(prepared.id))
    r.check("write puts the file under the root", prepared.path.parent == root)
    r.check("the file is named after the id", prepared.path.name == prepared.id + protocol.HANDOFF_SUFFIX)
    r.check("the file is there", prepared.path.is_file())

    data = prepared.path.read_bytes()
    r.check("what was written is a PNG", data.startswith(handoff.PNG_SIGNATURE))
    r.check("the manifest digest matches the bytes", prepared.manifest["sha256"] == hashlib.sha256(data).hexdigest())
    r.check("the manifest size matches the bytes", prepared.manifest["bytes"] == len(data) == prepared.path.stat().st_size)
    r.check("the manifest dimensions match the image", (prepared.manifest["width"], prepared.manifest["height"]) == (9, 7))
    r.check("the manifest says PNG", prepared.manifest["content_type"] == handoff.CONTENT_TYPE == "image/png")
    r.check("the manifest carries its own id", prepared.manifest["id"] == prepared.id)
    r.check("the manifest is timestamped", str(prepared.manifest.get("created_at", "")).startswith("20"))
    r.check("the manifest never carries a path", "path" not in prepared.manifest and "root" not in prepared.manifest)

    decoded = imaging.from_png_bytes(data)
    r.check("the PNG decodes to the image that went in", decoded.size == (9, 7))
    r.check("the handoff is lossless", imaging.to_rgba(decoded).getpixel((4, 3)) == (10, 20, 30, 255))

    r.check("the id resolves to the file that was written", handoff.resolve(prepared.id) == prepared.path.resolve())
    r.check("the file validates against its own manifest",
            handoff.validate_file(prepared.path, prepared.manifest)["sha256"] == prepared.manifest["sha256"])
    r.check("manifest_of returns what was recorded", handoff.manifest_of(prepared.id) == prepared.manifest)
    r.check("manifest_of hands back a copy", handoff.manifest_of(prepared.id) is not handoff.manifest_of(prepared.id))
    r.check("manifest_of an unknown id is None", handoff.manifest_of(ID_A) is None)
    r.check("manifest_of a bad id is None, not an error", handoff.manifest_of("../x") is None)

    # Nothing beside the PNG: a file whose purpose is to name a handoff is
    # exactly what section 9 says must not survive a restart.
    r.check("write leaves only the PNG", entries(root) == {prepared.path.name}, str(sorted(entries(root))))

    second = handoff.write(picture(4, 4))
    r.check("two handoffs do not collide", second.id != prepared.id and second.path != prepared.path)
    r.check("both are remembered", handoff.manifest_of(prepared.id) and handoff.manifest_of(second.id))

    # An RGBA source keeps its transparency rather than being flattened.
    transparent = Image.new("RGBA", (5, 5), (200, 100, 50, 0))
    kept = handoff.write(transparent)
    r.check("transparency survives the handoff",
            imaging.from_png_bytes(kept.path.read_bytes()).convert("RGBA").getpixel((2, 2))[3] == 0)

    before = entries(root)
    for label, image, code, needle in (
        ("an over-wide image", FakeImage((protocol.MAX_HANDOFF_SIDE + 1, 8)), errors.HANDOFF_TOO_LARGE, "side limit"),
        ("an over-tall image", FakeImage((8, protocol.MAX_HANDOFF_SIDE + 1)), errors.HANDOFF_TOO_LARGE, "side limit"),
        ("an oversized image", FakeImage((8193, 8193)), errors.HANDOFF_TOO_LARGE, "pixels"),
        ("a zero-width image", FakeImage((0, 10)), errors.HANDOFF_INVALID_IMAGE, "degenerate"),
        ("a negative size", FakeImage((-4, -4)), errors.HANDOFF_INVALID_IMAGE, "degenerate"),
        ("something that is not an image", object(), errors.HANDOFF_INVALID_IMAGE, "not a usable image"),
        ("a size that is not numbers", FakeImage(("a", "b")), errors.HANDOFF_INVALID_IMAGE, "not a usable image"),
    ):
        error = failure(handoff.write, image)
        r.check(f"write refuses {label}", code_of(error) == code, detail_of(error))
        r.check(f"and says which limit {label} hit", needle in detail_of(error), detail_of(error))
    r.check("a refused image writes nothing at all", entries(root) == before, str(sorted(entries(root) - before)))
    r.check("the too-large sentence is the one the tab shows",
            errors.message(errors.HANDOFF_TOO_LARGE) == "The image is too large for the WanGP handoff.")

    r.check("the byte ceiling is its own refusal",
            code_of(failure(handoff._refuse_oversized, 8, 8, protocol.MAX_HANDOFF_BYTES + 1)) == errors.HANDOFF_TOO_LARGE)
    r.check("and says it was the bytes",
            "bytes of PNG" in detail_of(failure(handoff._refuse_oversized, 8, 8, protocol.MAX_HANDOFF_BYTES + 1)))
    r.check("the ceilings are finite but generous",
            protocol.MAX_HANDOFF_SIDE >= 16384 and protocol.MAX_HANDOFF_BYTES >= 64 * 1024 * 1024)

    # A write that dies at the rename must leave neither debris nor a manifest.
    before = entries(root)
    original_replace = handoff.os.replace

    def refuse(source, destination):
        raise OSError("simulated crash between the fsync and the rename")

    handoff.os.replace = refuse
    try:
        crashed = None
        try:
            handoff.write(picture(6, 6))
        except OSError as error:
            crashed = error
    finally:
        handoff.os.replace = original_replace
    r.check("a write that cannot rename raises", isinstance(crashed, OSError))
    r.check("a failed write leaves no partial file", entries(root) == before, str(sorted(entries(root) - before)))
    r.check("a failed write leaves no .part debris", not any(name.endswith(handoff.PART_SUFFIX) for name in entries(root)))
    r.check("a failed write is not remembered", len(handoff._manifests) == 3)


def check_validate(r: Results, base: pathlib.Path) -> None:
    """The checks the receiving side runs, which assume nothing about the writer."""
    forget_everything()
    root = handoff.handoff_root()
    untouched = entries(root)
    holding = base / "validate"
    holding.mkdir(parents=True, exist_ok=True)

    good = holding / (ID_A + ".png")
    good.write_bytes(real_png(9, 7))
    facts = handoff.validate_file(good)
    r.check("a real PNG validates", facts["width"] == 9 and facts["height"] == 7)
    r.check("the facts describe the bytes on disk",
            facts["bytes"] == good.stat().st_size and facts["sha256"] == hashlib.sha256(good.read_bytes()).hexdigest())
    r.check("the facts never carry a path", "path" not in facts and "name" not in facts)
    r.check("the facts are shaped like a manifest", set(facts) == {"content_type", "bytes", "width", "height", "sha256"})

    not_png = holding / (ID_B + ".png")
    not_png.write_bytes(b"GIF89a this is not a png at all, whatever the name says")
    error = failure(handoff.validate_file, not_png)
    r.check("a non-PNG is refused", code_of(error) == errors.HANDOFF_INVALID_IMAGE)
    r.check("and is refused on the signature, before any decoder sees it", "signature" in detail_of(error))

    truncated = holding / (ID_C + ".png")
    truncated.write_bytes(real_png(9, 7)[:40])
    error = failure(handoff.validate_file, truncated)
    r.check("a truncated PNG is refused", code_of(error) == errors.HANDOFF_INVALID_IMAGE)
    r.check("and is refused for not decoding", "decode" in detail_of(error))

    headerless = holding / (ID_D + ".png")
    headerless.write_bytes(handoff.PNG_SIGNATURE + b"\x00" * 20)
    r.check("a PNG with no IHDR is refused",
            "header" in detail_of(failure(handoff.validate_file, headerless)))

    # A header that claims far more than the ceiling, on a tiny file: the
    # dimensions are read before the decoder is asked to allocate them.
    bomb = holding / (ID_E + ".png")
    bomb.write_bytes(lying_png(99999, 99999))
    error = failure(handoff.validate_file, bomb)
    r.check("a decompression bomb is refused on its header", code_of(error) == errors.HANDOFF_TOO_LARGE)
    r.check("and never got as far as decoding", "side limit" in detail_of(error))
    r.check("the bomb file was tiny", bomb.stat().st_size < 64)

    wrong_suffix = holding / (ID_F + ".txt")
    wrong_suffix.write_bytes(real_png())
    r.check("a file that is not named .png is refused",
            code_of(failure(handoff.validate_file, wrong_suffix)) == errors.HANDOFF_INVALID_IMAGE)

    r.check("a file that is not there is not found",
            code_of(failure(handoff.validate_file, holding / "absent.png")) == errors.HANDOFF_NOT_FOUND)
    a_directory = holding / "folder.png"
    a_directory.mkdir()
    r.check("a directory is not an image",
            code_of(failure(handoff.validate_file, a_directory)) == errors.HANDOFF_INVALID_IMAGE)

    manifest = dict(handoff.validate_file(good))
    manifest["id"] = ID_A
    r.check("a file validates against a manifest it matches", handoff.validate_file(good, manifest)["bytes"] == manifest["bytes"])
    r.check("a manifest is optional", handoff.validate_file(good, None)["bytes"] == manifest["bytes"])
    r.check("a manifest that is not a dict is ignored", handoff.validate_file(good, "nope")["bytes"] == manifest["bytes"])

    for label, key, value in (
        ("digest", "sha256", "0" * 64),
        ("size", "bytes", manifest["bytes"] + 1),
        ("width", "width", 10),
        ("height", "height", 8),
    ):
        tampered = dict(manifest)
        tampered[key] = value
        error = failure(handoff.validate_file, good, tampered)
        r.check(f"a mismatched {label} is a digest mismatch", code_of(error) == errors.HANDOFF_DIGEST_MISMATCH)
        r.check(f"and the detail names the {label}", key in detail_of(error), detail_of(error))

    # The worst case: a decodable, correctly named file that is not the one
    # that was prepared. Everything else about the send still looks right.
    replaced = holding / (ID_B + ".png")
    replaced.write_bytes(imaging.to_png_bytes(imaging.to_rgba(picture(9, 7, (99, 99, 99)))))
    r.check("a swapped image of the same size is caught by the digest",
            code_of(failure(handoff.validate_file, replaced, manifest)) == errors.HANDOFF_DIGEST_MISMATCH)
    r.check("the mismatch sentence is its own",
            errors.message(errors.HANDOFF_DIGEST_MISMATCH) == "The prepared image changed on the way to WanGP.")

    r.check("validate_file takes a plain string path", handoff.validate_file(str(good))["width"] == 9)
    r.check("validating a file elsewhere writes nothing into the root", entries(root) == untouched,
            str(sorted(entries(root) ^ untouched)))


def check_discard(r: Results, base: pathlib.Path) -> None:
    """Cleanup runs on the failure path too, so it may never raise."""
    forget_everything()
    root = handoff.handoff_root()

    prepared = handoff.write(picture())
    r.check("discard removes the file", handoff.discard(prepared.id) is None and not prepared.path.exists())
    r.check("discard forgets the manifest", handoff.manifest_of(prepared.id) is None)
    r.check("discarding twice is fine", handoff.discard(prepared.id) is None)
    r.check("discarding an id that was never written is fine", handoff.discard(ID_A) is None)

    precious = base / "precious.txt"
    precious.write_text("not yours to remove", encoding="utf-8")
    for label, value in (("a traversal", "../precious.txt"), ("a path", str(precious)), ("two dots", ".."),
                         ("a slash", "a/b"), ("a backslash", "a\\b"), ("nothing", None), ("a number", 7),
                         ("bytes", b"0" * 32), ("31 characters", ID_A[:-1])):
        r.check(f"discard of {label} does not raise", handoff.discard(value) is None)
    r.check("discard never reaches outside the root", precious.is_file())

    if can_symlink(base):
        target = base / "linked-target.png"
        target.write_bytes(real_png())
        link = root / (ID_C + protocol.HANDOFF_SUFFIX)
        os.symlink(str(target), str(link))
        handoff.discard(ID_C)
        r.check("discarding a symlink removes the link", not link.is_symlink() and not link.exists())
        r.check("and leaves what it pointed at", target.is_file())

    unreadable = root / (ID_D + protocol.HANDOFF_SUFFIX)
    unreadable.mkdir()
    r.check("discard of a directory in our name does not raise", handoff.discard(ID_D) is None)
    unreadable.rmdir()


def check_sweep(r: Results, base: pathlib.Path) -> None:
    """Stale debris goes; a send in flight, a stranger's file and a link stay."""
    forget_everything()
    root = handoff.handoff_root()
    for child in os.scandir(root):
        if child.is_dir(follow_symlinks=False):
            os.rmdir(child.path)
        else:
            os.unlink(child.path)

    stale = root / (ID_A + protocol.HANDOFF_SUFFIX)
    stale.write_bytes(real_png())
    backdate(stale, 100000)

    fresh = root / (ID_B + protocol.HANDOFF_SUFFIX)
    fresh.write_bytes(real_png())

    debris = root / (ID_C + ".abc123" + handoff.PART_SUFFIX)
    debris.write_bytes(b"half a png")
    backdate(debris, 100000)

    foreign = root / "somebody-elses-notes.txt"
    foreign.write_text("not ours", encoding="utf-8")
    backdate(foreign, 100000)

    odd_suffix = root / (ID_D + ".jpg")
    odd_suffix.write_bytes(b"jpeg-ish")
    backdate(odd_suffix, 100000)

    folder = root / (ID_E + protocol.HANDOFF_SUFFIX)
    folder.mkdir()
    backdate(folder, 100000)

    in_flight = handoff.write(picture())
    backdate(in_flight.path, 100000)

    linked_target = base / "sweep-target.png"
    linked_target.write_bytes(real_png())
    link = None
    if can_symlink(base):
        link = root / (ID_F + protocol.HANDOFF_SUFFIX)
        os.symlink(str(linked_target), str(link))

    removed = handoff.sweep(max_age_seconds=3600)
    r.check("sweep removes the stale handoff", not stale.exists())
    r.check("sweep removes half-written debris", not debris.exists())
    r.check("sweep counts what it removed", removed == 2, str(removed))
    r.check("sweep keeps a fresh handoff", fresh.is_file())
    r.check("sweep keeps a send that is still in flight", in_flight.path.is_file())
    r.check("an in-flight handoff is still remembered", handoff.manifest_of(in_flight.id) is not None)
    r.check("sweep leaves a file it did not write alone", foreign.is_file())
    r.check("sweep leaves an unexpected suffix alone", odd_suffix.is_file())
    r.check("sweep does not remove directories", folder.is_dir())
    r.check("sweep cannot reach outside the root", linked_target.is_file())
    if link is not None:
        r.check("sweep does not follow a symlink in the root", link.is_symlink())

    r.check("sweeping again removes nothing", handoff.sweep(max_age_seconds=3600) == 0)
    r.check("a generous age keeps everything", handoff.sweep(max_age_seconds=10**9) == 0)

    # The clock is injectable, so "stale" can be tested without waiting. Only
    # the fresh handoff is left to collect; everything else is live, is not
    # ours, or is not a plain file.
    r.check("a future clock ages the one remaining handoff",
            handoff.sweep(max_age_seconds=1, now=time.time() + 10**6) == 1, str(sorted(entries(root))))
    r.check("the fresh handoff is the one that went", not fresh.exists())
    r.check("the in-flight send survived the future", in_flight.path.is_file())
    r.check("and so did the stranger's file", foreign.is_file())

    handoff.discard(in_flight.id)
    r.check("once discarded, the same file is sweepable", handoff.manifest_of(in_flight.id) is None)

    r.check("the sweeper only recognises its own two shapes",
            handoff._sweepable_id(ID_A + ".png") == ID_A
            and handoff._sweepable_id(ID_A + ".x" + handoff.PART_SUFFIX) == ID_A
            and handoff._sweepable_id(ID_A + ".jpg") is None
            and handoff._sweepable_id("notes.txt") is None
            and handoff._sweepable_id("../" + ID_A + ".png") is None)


def run() -> Results:
    r = Results("wangp handoff")

    original_dir = config._state.get("dir")
    with tempfile.TemporaryDirectory(prefix="minipaint-wangp-handoff-") as temporary:
        base = pathlib.Path(temporary)
        config.use_config_dir(base / "data")
        try:
            check_ids(r)
            check_resolve(r, base)
            check_write(r, base)
            check_validate(r, base)
            check_discard(r, base)
            check_sweep(r, base)
        finally:
            # Module state: the manifests and the path helpers both outlive
            # this function, and the directory they point at is about to go.
            forget_everything()
            config.use_config_dir(original_dir)

    return r


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
