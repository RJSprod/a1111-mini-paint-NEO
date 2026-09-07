"""What a restart is allowed to remember, and whether any of it is real.

Section 40 of the design intent asks two questions that split neatly across
two modules. ``config.py`` decides what survives a restart - a whitelist, a
migration, a three-file transaction - and ``discovery.py`` decides whether
what survived still describes the machine. Both are exercised here against a
temporary directory and injected seams: no Forge, no WanGP, no conda, no
NVIDIA driver and no network are involved at any point.

The adversarial half is the point. A config that quietly persisted a port, a
migration that guessed at a schema it has never seen, a write that left half
a file behind, or a GPU lookup that fell back to "the other card" would all
pass a happy-path test and fail a user.
"""

from harness import Results, setup_path

setup_path()

import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import pathlib  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402

from minipaint_neo.wangp import config, discovery, errors  # noqa: E402

#: Planted in every field the config is not allowed to keep. If this string
#: appears anywhere in a written file, something reached the disk that the
#: "do not persist" list in section 9 forbids.
SENTINEL = "MUST-NOT-BE-PERSISTED"

GOOD_UUID = "GPU-1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d"
OTHER_UUID = "GPU-aaaabbbb-cccc-dddd-eeee-ffff00001111"


def failure(call, *args, **kwargs):
    """The IntegrationError a call raised, or None when it returned."""
    try:
        call(*args, **kwargs)
    except errors.IntegrationError as error:
        return error
    return None


def code_of(error) -> str:
    return getattr(error, "code", "")


def sample() -> config.Config:
    """A complete, valid setup - the thing a finished wizard would write."""
    return config.Config(
        initialized=True,
        wangp_root="/opt/Wan2GP",
        runtime={
            "type": discovery.CONDA,
            "prefix": "/opt/conda/envs/wangp",
            "display_name": "wangp",
            "launch_strategy": discovery.DIRECT_PYTHON,
        },
        gpu={"uuid": GOOD_UUID},
        integration={"proxy_path": config.DEFAULT_PROXY_PATH, "auto_start": config.AUTO_START_LAZY},
    )


def keys_in(document) -> set:
    """Every key anywhere in a parsed document, at any depth."""
    found = set()
    if isinstance(document, dict):
        for key, value in document.items():
            found.add(key)
            found |= keys_in(value)
    elif isinstance(document, list):
        for item in document:
            found |= keys_in(item)
    return found


def entries(directory) -> set:
    return {child.name for child in pathlib.Path(directory).iterdir()}


class Completed:
    """What subprocess.run hands back, without a subprocess."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class Runner:
    """A scripted stand-in for subprocess.run that remembers what it was asked."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), kwargs))
        reply = self.replies.pop(0) if self.replies else Completed(1, "", "no reply scripted")
        if isinstance(reply, BaseException):
            raise reply
        return reply


def probe_reply(python="3.10.6", imports=("gradio", "torch")) -> Completed:
    """What the in-environment probe prints when it likes what it parsed."""
    info = {"python": python, "imports": list(imports), "modules": 42}
    return Completed(0, f"some unrelated chatter\n{discovery._PROBE_MARKER} {json.dumps(info)}\n", "")


@contextlib.contextmanager
def which(answer):
    """shutil.which, answered by the test instead of by this machine."""
    original = discovery.shutil.which
    discovery.shutil.which = lambda name: answer
    try:
        yield
    finally:
        discovery.shutil.which = original


def make_prefix(path, layout="posix", conda=False) -> pathlib.Path:
    """A directory that looks like a Python environment, without being one."""
    path = pathlib.Path(path)
    relative = {"posix": ("bin", "python"), "windows": ("Scripts", "python.exe"), "bare": ("python.exe",)}[layout]
    interpreter = path.joinpath(*relative)
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_text("", encoding="utf-8")
    if conda:
        (path / "conda-meta").mkdir(parents=True, exist_ok=True)
    return path


def make_root(path, marker=True, project=True) -> pathlib.Path:
    """A directory that looks like a WanGP checkout, without being one."""
    path = pathlib.Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if marker:
        (path / discovery.ROOT_MARKER).write_text("import gradio\n", encoding="utf-8")
    if project:
        (path / "shared").mkdir(exist_ok=True)
    return path


# --------------------------------------------------------------------------


def check_location(r: Results, base: pathlib.Path) -> None:
    """Where the settings land, and whether the fallback says so out loud."""
    answered = base / "host-data"
    r.check(
        "a host data root is used as given",
        config.data_root(sources=(("test", lambda: answered),)) == (answered, ""),
    )
    root, reason = config.data_root(sources=(("test", lambda: ""),))
    r.check("an empty host answer is not a data root", root == config.paths.root_path and "empty" in reason)
    root, reason = config.data_root(sources=(("test", _explode),))
    r.check("a host that raises is not a data root", root == config.paths.root_path and "boom" in reason)
    root, reason = config.data_root(sources=())
    r.check("no host at all still answers", root == config.paths.root_path and reason)
    r.check("the second element is empty only on success", config.data_root(sources=(("t", lambda: base),))[1] == "")

    original_hosts = config.HOST_DATA_ROOTS
    original_root = config.paths.root_path
    try:
        # The host answers: the folder is named after the extension and
        # nothing is printed, because nothing surprising happened.
        config.HOST_DATA_ROOTS = (("test", lambda: answered),)
        config.use_config_dir(None)
        spoken = io.StringIO()
        with contextlib.redirect_stdout(spoken):
            directory = config.config_dir()
        r.check("a host data root gives the named folder", directory == answered / config.DIRECTORY_NAME)
        r.check("the ordinary case says nothing", spoken.getvalue() == "")
        r.check("config_dir creates what it returns", directory.is_dir())
        r.check("the config file sits in it", config.config_path() == directory / config.CONFIG_NAME)
        r.check("so do the backup and the pending file",
                config.backup_path().parent == directory and config.pending_path().parent == directory)
        r.check("runtime scratch is a subfolder, not a sibling", config.runtime_dir() == directory / config.RUNTIME_NAME)

        # No host answers: the extension folder is used, and said so.
        config.HOST_DATA_ROOTS = (("test", _explode),)
        config.paths.root_path = base / "extension"
        config.use_config_dir(None)
        spoken = io.StringIO()
        with contextlib.redirect_stdout(spoken):
            directory = config.config_dir()
        said = spoken.getvalue()
        r.check("the fallback lands inside the extension", directory == base / "extension" / config.FALLBACK_DIRECTORY_NAME)
        r.check("the fallback is audible", said.strip() != "" and str(directory) in said)
        r.check("the fallback says why it is a bad place", "reinstalled" in said)
        r.check("the fallback is announced once", config.config_dir() == directory)
        spoken = io.StringIO()
        with contextlib.redirect_stdout(spoken):
            config.config_dir()
        r.check("the second call is silent", spoken.getvalue() == "")
    finally:
        config.HOST_DATA_ROOTS = original_hosts
        config.paths.root_path = original_root
        config.use_config_dir(None)


def _explode():
    raise RuntimeError("boom")


def check_schema(r: Results) -> None:
    """as_dict / from_dict, the migration, and the two ways of failing closed."""
    document = sample().as_dict()
    r.check("a written config declares this schema", document["schema_version"] == config.SCHEMA_VERSION == 2)
    r.check("the document has exactly the declared sections", set(document) == set(config.TOP_LEVEL_KEYS))
    r.check("round trip is stable", config.Config.from_dict(document).as_dict() == document)
    r.check("round trip keeps the answers",
            config.Config.from_dict(document).gpu.get("uuid") == GOOD_UUID
            and config.Config.from_dict(document).runtime.get("prefix") == "/opt/conda/envs/wangp")
    r.check("a round trip is idempotent", config.Config.from_dict(config.Config.from_dict(document).as_dict()).as_dict() == document)

    # The proxy path is a constant of the proxy: a config that could move the
    # route could aim it past whatever Forge puts in front of it.
    moved = sample()
    moved.integration = {"proxy_path": "http://elsewhere.invalid/", "auto_start": "eager"}
    written = moved.as_dict()
    r.check("the proxy path is not configurable", written["integration"]["proxy_path"] == config.DEFAULT_PROXY_PATH)
    r.check("an unknown start policy becomes lazy", written["integration"]["auto_start"] == config.AUTO_START_LAZY)

    empty = config.Config().as_dict()
    r.check("an empty config still has every section", set(empty) == set(config.TOP_LEVEL_KEYS))
    r.check("an empty config has no launch strategy", empty["runtime"]["launch_strategy"] == "")

    v1 = {
        "schema_version": 1,
        "initialized": True,
        "wangp_root": "/opt/Wan2GP",
        "gpu_uuid": GOOD_UUID,
        "runtime": {"type": "conda", "prefix": "/opt/conda/envs/wangp", "display_name": "wangp",
                    "launch_strategy": discovery.DIRECT_PYTHON},
    }
    migrated = config.Config.from_dict(v1)
    r.check("a v1 file migrates", migrated.schema_version == config.SCHEMA_VERSION)
    r.check("v1 keeps what it knew", migrated.initialized and migrated.wangp_root == "/opt/Wan2GP")
    r.check("v1 gpu_uuid becomes gpu.uuid", migrated.gpu.get("uuid") == GOOD_UUID)
    r.check("the migrated document has no gpu_uuid", "gpu_uuid" not in keys_in(migrated.as_dict()))
    r.check("v1 does not inherit a launch strategy", migrated.as_dict()["runtime"]["launch_strategy"] == "")
    r.check("v1 gains the integration defaults",
            migrated.as_dict()["integration"] == {"proxy_path": config.DEFAULT_PROXY_PATH,
                                                  "auto_start": config.AUTO_START_LAZY})
    r.check("a v1 file with no runtime at all still parses", config.Config.from_dict({"schema_version": 1}).runtime == {})

    too_new = failure(config.Config.from_dict, {"schema_version": config.SCHEMA_VERSION + 1, "initialized": True})
    r.check("a newer schema fails closed", code_of(too_new) == errors.CONFIG_SCHEMA_TOO_NEW)
    r.check("a newer schema is not guessed at", too_new is not None and str(config.SCHEMA_VERSION + 1) in too_new.detail)
    r.check("a newer schema offers reinitialize", errors.CONFIG_SCHEMA_TOO_NEW in errors.REINIT_CODES)
    r.check("a far newer schema is the same answer",
            code_of(failure(config.Config.from_dict, {"schema_version": 99})) == errors.CONFIG_SCHEMA_TOO_NEW)

    for name, data in (
        ("a schema we never wrote", {"schema_version": 0}),
        ("a missing schema version", {"initialized": True}),
        ("a schema version that is a string", {"schema_version": "2"}),
        ("a schema version that is a bool", {"schema_version": True}),
        ("a schema version that is a float", {"schema_version": 2.0}),
        ("a document that is a list", [1, 2, 3]),
        ("a document that is a string", "{}"),
        ("a document that is nothing", None),
    ):
        r.check(f"{name} is unreadable", code_of(failure(config.Config.from_dict, data)) == errors.CONFIG_UNREADABLE)

    # Shape only. Whether the root still exists is discovery's question.
    r.check("a complete config validates", sample().validate() == [])
    r.check("an empty config lists every gap",
            config.Config().validate() == [errors.SETUP_REQUIRED, errors.WANGP_ROOT_MISSING,
                                           errors.RUNTIME_MISSING, errors.GPU_UUID_MISSING])
    unfinished = sample()
    unfinished.initialized = False
    r.check("setup not finished is its own code", unfinished.validate() == [errors.SETUP_REQUIRED])
    no_prefix = sample()
    no_prefix.runtime = {"type": "conda", "display_name": "wangp"}
    r.check("a runtime without a prefix describes nothing", no_prefix.validate() == [errors.RUNTIME_MISSING])
    for label, uuid in (("blank", ""), ("an index", "0"), ("a name", "RTX 5090"), ("truncated", "GPU-"),
                        ("a path", "/dev/nvidia0")):
        bad = sample()
        bad.gpu = {"uuid": uuid}
        r.check(f"a gpu uuid that is {label} is refused", bad.validate() == [errors.GPU_UUID_MISSING])
    mig = sample()
    mig.gpu = {"uuid": "MIG-" + GOOD_UUID}
    r.check("a MIG device uuid is a uuid", mig.validate() == [])


def check_forbidden_keys(r: Results, base: pathlib.Path) -> None:
    """Section 9's per-run facts, handed in on purpose, refused on the way out."""
    config.use_config_dir(base / "forbidden")

    stuffed = {
        "schema_version": config.SCHEMA_VERSION,
        "initialized": True,
        "wangp_root": "/opt/Wan2GP",
        "pid": SENTINEL,
        "port": SENTINEL,
        "session_hash": SENTINEL,
        "runtime": {
            "type": "venv", "prefix": "/opt/wangp/venv", "display_name": "venv",
            "launch_strategy": discovery.DIRECT_PYTHON,
            "pid": SENTINEL, "backend_port": SENTINEL, "secret": SENTINEL, "instance_id": SENTINEL,
        },
        "gpu": {"uuid": GOOD_UUID, "nonce": SENTINEL, "channel_nonce": SENTINEL},
        "integration": {
            "proxy_path": config.DEFAULT_PROXY_PATH, "auto_start": config.AUTO_START_LAZY,
            "handoff_id": SENTINEL, "handoff_root": SENTINEL, "receivers": [SENTINEL],
            "receiver_cache": {"x": SENTINEL}, "state_revision": SENTINEL, "revision": SENTINEL,
            "bridge_secret": SENTINEL,
        },
    }
    parsed = config.Config.from_dict(stuffed)
    document = parsed.as_dict()
    r.check("nothing from the do-not-persist list reaches the document",
            not (keys_in(document) & config.NEVER_PERSISTED), str(sorted(keys_in(document) & config.NEVER_PERSISTED)))
    r.check("the config still keeps what it is for",
            document["wangp_root"] == "/opt/Wan2GP" and document["gpu"]["uuid"] == GOOD_UUID)

    # The same values handed straight to a Config object, bypassing from_dict.
    direct = config.Config(initialized=True, wangp_root="/opt/Wan2GP",
                           runtime=dict(stuffed["runtime"]), gpu=dict(stuffed["gpu"]),
                           integration=dict(stuffed["integration"]))
    r.check("a Config built by hand drops them too", not (keys_in(direct.as_dict()) & config.NEVER_PERSISTED))

    config.write_pending(direct)
    promoted = config.promote_pending()
    text = config.config_path().read_text(encoding="utf-8")
    r.check("the sentinel never reaches the active file", SENTINEL not in text)
    r.check("the sentinel never reaches the parsed config", not (keys_in(promoted.as_dict()) & config.NEVER_PERSISTED))
    r.check("the file on disk has only the declared keys", set(json.loads(text)) == set(config.TOP_LEVEL_KEYS))
    r.check("every forbidden name is still listed by name",
            {"pid", "port", "secret", "nonce", "session_hash", "handoff_id", "receiver_cache", "state_revision"}
            <= config.NEVER_PERSISTED)


def check_atomic_write(r: Results, base: pathlib.Path) -> None:
    """Either the old file or the new one, and never a temp file left behind."""
    directory = base / "atomic"
    config.use_config_dir(directory)
    target = config.config_path()

    config.atomic_write(target, '{"schema_version": 2}\n')
    r.check("a write lands whole", target.read_text(encoding="utf-8") == '{"schema_version": 2}\n')
    r.check("a successful write leaves nothing else", entries(directory) == {config.CONFIG_NAME})

    def refuse(source, destination):
        raise OSError("simulated crash between the flush and the rename")

    original_replace = config.os.replace
    config.os.replace = refuse
    try:
        crashed = None
        try:
            config.atomic_write(target, "x" * 200000)
        except OSError as error:
            crashed = error
    finally:
        config.os.replace = original_replace

    r.check("a write that dies before the rename raises", isinstance(crashed, OSError))
    r.check("the old file is untouched by a failed write",
            target.read_text(encoding="utf-8") == '{"schema_version": 2}\n')
    r.check("no partial file survives a failed write", entries(directory) == {config.CONFIG_NAME},
            str(sorted(entries(directory))))
    r.check("the config that survived is still readable",
            config.load() is not None and config.load().schema_version == config.SCHEMA_VERSION)

    # The same crash, through the transaction rather than through the helper.
    config.write_pending(sample())
    config.os.replace = refuse
    try:
        promote_failed = None
        try:
            config.promote_pending()
        except OSError as error:
            promote_failed = error
    finally:
        config.os.replace = original_replace
    r.check("a promote that cannot rename raises rather than half-succeeding", isinstance(promote_failed, OSError))
    r.check("the pending file is still there after a failed promote", config.pending_path().is_file())


def check_transaction(r: Results, base: pathlib.Path) -> None:
    """Fresh install, promote, backup, restore, clear - in that order."""
    directory = base / "transaction"
    config.use_config_dir(directory)

    r.check("a fresh install has no config", config.load() is None)
    r.check("a fresh install has no backup", config.restore_backup() is None)
    r.check("a fresh install has no pending file", not config.pending_path().exists())
    r.check("nothing to promote is an error, not an empty config",
            code_of(failure(config.promote_pending)) == errors.CONFIG_UNREADABLE)

    first = sample()
    config.write_pending(first)
    r.check("writing a pending config changes nothing yet", config.load() is None)
    r.check("the pending file exists", config.pending_path().is_file())

    promoted = config.promote_pending()
    r.check("promote makes the pending config active", promoted.as_dict() == first.as_dict())
    r.check("the active config is what a restart reads", config.load().as_dict() == first.as_dict())
    r.check("promote consumes the pending file", not config.pending_path().exists())
    r.check("a first promote has nothing to back up", not config.backup_path().exists())

    second = sample()
    second.gpu = {"uuid": OTHER_UUID}
    config.write_pending(second)
    config.promote_pending()
    r.check("a second promote replaces the active config", config.load().gpu["uuid"] == OTHER_UUID)
    r.check("a second promote keeps what it replaced", config.backup_path().is_file())
    r.check("the backup is the previous working setup", config._read(config.backup_path()).gpu["uuid"] == GOOD_UUID)

    restored = config.restore_backup()
    r.check("restore returns the backed-up config", restored.gpu["uuid"] == GOOD_UUID)
    r.check("restore puts it back as the active one", config.load().gpu["uuid"] == GOOD_UUID)
    r.check("restore keeps the backup file", config.backup_path().is_file())
    r.check("restore twice is allowed", config.restore_backup().gpu["uuid"] == GOOD_UUID)

    config.write_pending(second)
    config.clear()
    r.check("clear marks the integration uninitialized", config.load().initialized is False)
    r.check("clear keeps the answers so the wizard can prefill", config.load().wangp_root == "/opt/Wan2GP")
    r.check("clear leaves the backup in place", config.backup_path().is_file())
    r.check("clear drops an abandoned pending config", not config.pending_path().exists())

    # The second clear is the one that matters: an already-cleared config is
    # not "the last setup that worked" and must not overwrite the backup.
    before = config.backup_path().read_text(encoding="utf-8")
    config.clear()
    r.check("clearing twice does not overwrite the backup",
            config.backup_path().read_text(encoding="utf-8") == before)
    r.check("the backup is still an initialized config", config._read(config.backup_path()).initialized is True)
    r.check("restore after clear brings the setup back", config.restore_backup().initialized is True)


def check_unreadable(r: Results, base: pathlib.Path) -> None:
    """A file we cannot parse is a state a user can act on, not a crash."""
    directory = base / "unreadable"
    config.use_config_dir(directory)

    config.config_path().write_text("{ not json at all", encoding="utf-8")
    r.check("corrupt json is unreadable", code_of(failure(config.load)) == errors.CONFIG_UNREADABLE)
    r.check("an empty file is unreadable", _write_and_load(r, "") == errors.CONFIG_UNREADABLE)
    r.check("a truncated document is unreadable", _write_and_load(r, '{"schema_version": 2, "initial') == errors.CONFIG_UNREADABLE)
    r.check("a json list is unreadable", _write_and_load(r, "[]") == errors.CONFIG_UNREADABLE)
    config.config_path().write_text(json.dumps({"schema_version": 7}), encoding="utf-8")
    r.check("a file from a newer extension fails closed", code_of(failure(config.load)) == errors.CONFIG_SCHEMA_TOO_NEW)
    kept = config.config_path().read_text(encoding="utf-8")
    failure(config.load)
    r.check("reading a newer file does not rewrite it", config.config_path().read_text(encoding="utf-8") == kept)

    # A directory where the file should be: an OSError that is not "absent".
    directory_config = base / "unreadable-dir"
    config.use_config_dir(directory_config)
    config.config_path().mkdir(parents=True, exist_ok=True)
    r.check("a directory in the config's place is unreadable",
            code_of(failure(config.load)) == errors.CONFIG_UNREADABLE)

    # clear() and promote() both have to survive that, because they are the
    # two things the recovery UX offers.
    config.use_config_dir(directory)
    config.config_path().write_text("{ not json at all", encoding="utf-8")
    config.clear()
    r.check("clear replaces an unparseable config with an honest empty one",
            config.load() is not None and config.load().initialized is False)
    config.config_path().write_text("{ not json at all", encoding="utf-8")
    config.write_pending(sample())
    promoted = config.promote_pending()
    r.check("promote over an unparseable config still works", promoted.initialized is True)
    r.check("an unparseable config is never copied into the backup",
            not config.backup_path().exists() or "not json" not in config.backup_path().read_text(encoding="utf-8"))


def _write_and_load(r: Results, text: str) -> str:
    config.config_path().write_text(text, encoding="utf-8")
    return code_of(failure(config.load))


# ------------------------------------------------------------- discovery --


def check_root(r: Results, base: pathlib.Path) -> None:
    """Is that directory a WanGP checkout, and if not, which part is missing."""
    good = make_root(base / "roots" / "wangp")
    r.check("a real checkout is accepted", discovery.validate_root(good) == [])
    r.check("a real checkout has no problems", discovery.root_problems(good) == [])

    no_marker = make_root(base / "roots" / "no-marker", marker=False)
    r.check("a directory without wgp.py is refused", discovery.validate_root(no_marker) == [errors.WANGP_ROOT_MISSING])
    r.check("and says which file is missing",
            any(discovery.ROOT_MARKER in reason for _code, reason in discovery.root_problems(no_marker)))

    no_project = make_root(base / "roots" / "no-project", project=False)
    r.check("wgp.py alone is not a checkout", discovery.validate_root(no_project) == [errors.WANGP_ROOT_MISSING])
    r.check("and says the project directory is missing",
            any("project directory" in reason for _code, reason in discovery.root_problems(no_project)))

    empty = base / "roots" / "empty"
    empty.mkdir(parents=True, exist_ok=True)
    problems = discovery.root_problems(empty)
    r.check("an empty directory fails more than one check", len(problems) >= 2)
    r.check("but collapses to one code", discovery.validate_root(empty) == [errors.WANGP_ROOT_MISSING])

    r.check("a path that is not there is refused", discovery.validate_root(base / "roots" / "absent") == [errors.WANGP_ROOT_MISSING])
    r.check("and says so", discovery.root_problems(base / "roots" / "absent")[0][1] == "not a directory")
    r.check("a file is not a root", discovery.validate_root(good / discovery.ROOT_MARKER) == [errors.WANGP_ROOT_MISSING])
    r.check("no path at all is refused", discovery.root_problems("")[0][1] == "no path given")
    r.check("None is refused", discovery.validate_root(None) == [errors.WANGP_ROOT_MISSING])

    # A fresh WanGP has no plugins folder; one that is a file is a problem.
    blocked = make_root(base / "roots" / "blocked")
    (blocked / discovery.PLUGINS_DIR_NAME).write_text("not a directory", encoding="utf-8")
    r.check("a plugins file where a folder belongs is a problem",
            any("not a directory" in reason for _code, reason in discovery.root_problems(blocked)))
    r.check("a checkout with no plugins folder yet is still fine", discovery.validate_root(good) == [])

    r.check("the bridge folder is derived from the root, never given",
            discovery.bridge_dir(good) == good / discovery.PLUGINS_DIR_NAME / discovery.BRIDGE_FOLDER_NAME)
    r.check("no bridge installed reads as no info", discovery.read_bridge_info(good) is None)
    r.check("no bridge installed is BRIDGE_MISSING", discovery.bridge_status(good)[0] == errors.BRIDGE_MISSING)


def check_gpus(r: Results) -> None:
    """nvidia-smi parsing, and the refusal to substitute one card for another."""
    sample_output = (
        "0, NVIDIA GeForce RTX 5090, GPU-1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d, 32607, 00000000:01:00.0\n"
        "\n"
        "1,  NVIDIA GeForce RTX 3090 ,  GPU-aaaabbbb-cccc-dddd-eeee-ffff00001111 ,  24576 ,  00000000:02:00.0\n"
        "this line is not a gpu\n"
        "2, Disabled Card, N/A, 8192, 00000000:03:00.0\n"
        "x, Bad Index, GPU-deadbeef-0000-1111-2222-333344445555, 1024\n"
        "4, Only Two Fields\n"
        "5, No Memory Reported, GPU-55555555-6666-7777-8888-999999999999, N/A\n"
    )
    devices = discovery.parse_nvidia_smi(sample_output)
    r.check("only complete rows become devices", [device.index for device in devices] == [0, 1, 5])
    r.check("odd spacing is trimmed", devices[1].name == "NVIDIA GeForce RTX 3090" and devices[1].uuid == OTHER_UUID)
    r.check("memory is parsed", devices[0].total_mb == 32607 and devices[1].total_mb == 24576)
    r.check("bus ids survive", devices[0].bus_id == "00000000:01:00.0")
    r.check("an empty line is skipped", all(device.uuid for device in devices))
    r.check("a garbage line is skipped", not any("not a gpu" in device.name for device in devices))
    r.check("an N/A uuid is not a device", GOOD_UUID in {d.uuid for d in devices} and 2 not in {d.index for d in devices})
    r.check("a non-numeric index is not a device", "GPU-deadbeef-0000-1111-2222-333344445555" not in {d.uuid for d in devices})
    r.check("a short row is not a device", not any(d.name == "Only Two Fields" for d in devices))
    r.check("N/A memory reads as unknown, not as zero bytes", devices[2].total_mb == 0)

    r.check("the label is the name and whole gigabytes", devices[0].label == "NVIDIA GeForce RTX 5090 - 32 GB")
    r.check("a device with no memory is labelled by name", devices[2].label == "No Memory Reported")
    r.check("a nameless device falls back to its uuid", discovery.Gpu(0, "", GOOD_UUID).label == GOOD_UUID)
    r.check("as_dict carries the uuid the config will store", devices[0].as_dict()["uuid"] == GOOD_UUID)

    r.check("no output is no devices", discovery.parse_nvidia_smi("") == [])
    r.check("whitespace is no devices", discovery.parse_nvidia_smi("   \n\n") == [])
    r.check("None is no devices", discovery.parse_nvidia_smi(None) == [])
    r.check("a header-only file is no devices", discovery.parse_nvidia_smi("index, name, uuid\n") == [])

    # The whole reason the config stores a UUID rather than an index.
    r.check("an exact uuid matches", discovery.find_gpu(GOOD_UUID, devices) is devices[0])
    r.check("a surrounding-space uuid still matches", discovery.find_gpu(f"  {GOOD_UUID}  ", devices) is devices[0])
    r.check("no match is None, never the first card", discovery.find_gpu("GPU-00000000-0000-0000-0000-000000000000", devices) is None)
    r.check("one character off is not a match", discovery.find_gpu(GOOD_UUID[:-1] + "e", devices) is None)
    r.check("a prefix is not a match", discovery.find_gpu(GOOD_UUID[:20], devices) is None)
    r.check("a different case is not a match", discovery.find_gpu(GOOD_UUID.upper(), devices) is None)
    r.check("an index is not a uuid", discovery.find_gpu("0", devices) is None)
    r.check("an empty uuid matches nothing", discovery.find_gpu("", devices) is None and discovery.find_gpu(None, devices) is None)
    r.check("no cards at all is None, not an error", discovery.find_gpu(GOOD_UUID, []) is None)
    r.check("a single remaining card is still not a substitute", discovery.find_gpu(GOOD_UUID, [devices[1]]) is None)

    # nvidia-smi separates with ", " and quotes any name that contains a comma
    # of its own. Reading that quoting correctly is what keeps the UUID in the
    # UUID column: parsed naively the name splits in two, every later field
    # shifts along, and the card becomes unselectable - safe, but for no reason.
    quoted = discovery.parse_nvidia_smi('3, "NVIDIA RTX A6000, 48GB", GPU-99998888-7777-6666-5555-444433332222, 49140\n')
    r.check("a quoted name containing a comma stays one name", [card.name for card in quoted] == ["NVIDIA RTX A6000, 48GB"])
    r.check("and its uuid is still the uuid",
            discovery.find_gpu("GPU-99998888-7777-6666-5555-444433332222", quoted) is not None)
    r.check("with the memory that followed it", [card.total_mb for card in quoted] == [49140])

    r.check("nvml is skippable and the smi path still answers",
            discovery.list_gpus(runner=Runner(Completed(0, sample_output)), use_nvml=False) is not None)
    with which(None):
        r.check("no nvidia-smi is an empty list, not a failure",
                discovery.list_gpus(runner=Runner(Completed(0, sample_output)), use_nvml=False) == [])
    with which("/usr/bin/nvidia-smi"):
        found = discovery.list_gpus(runner=Runner(Completed(0, sample_output)), use_nvml=False)
        r.check("nvidia-smi output becomes the device list", [g.index for g in found] == [0, 1, 5])
        r.check("a failing nvidia-smi is an empty list",
                discovery.list_gpus(runner=Runner(Completed(1, "", "no driver")), use_nvml=False) == [])
        r.check("an nvidia-smi that cannot be run is an empty list",
                discovery.list_gpus(runner=Runner(OSError("not executable")), use_nvml=False) == [])


def check_interpreter(r: Results, base: pathlib.Path) -> None:
    """Finding a Python inside a prefix, on either platform's layout."""
    posix = make_prefix(base / "envs" / "posix")
    windows = make_prefix(base / "envs" / "windows", layout="windows")
    bare = make_prefix(base / "envs" / "bare", layout="bare")
    hollow = base / "envs" / "hollow"
    hollow.mkdir(parents=True, exist_ok=True)

    r.check("a posix prefix resolves to bin/python", discovery.interpreter_for({"prefix": str(posix)}) == posix / "bin" / "python")
    r.check("a windows prefix resolves to Scripts/python.exe",
            discovery.interpreter_for({"prefix": str(windows)}) == windows / "Scripts" / "python.exe")
    r.check("a windows base prefix resolves to python.exe", discovery.interpreter_for({"prefix": str(bare)}) == bare / "python.exe")
    r.check("a plain string prefix works too", discovery.interpreter_for(str(posix)) == posix / "bin" / "python")
    r.check("a prefix with no python is None", discovery.interpreter_for({"prefix": str(hollow)}) is None)
    r.check("a prefix that is not there is None", discovery.interpreter_for({"prefix": str(base / "envs" / "gone")}) is None)
    r.check("no prefix is None", discovery.interpreter_for({}) is None and discovery.interpreter_for("") is None)
    r.check("None is None", discovery.interpreter_for(None) is None)

    both = make_prefix(base / "envs" / "both")
    make_prefix(both, layout="windows")
    expected = both / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
    r.check("this platform's layout is preferred when both exist", discovery.interpreter_for(str(both)) == expected)

    r.check("a venv prefix classifies as a venv", discovery.runtime_for_prefix(posix)["type"] == discovery.VENV)
    conda = make_prefix(base / "envs" / "conda-env", conda=True)
    r.check("a conda prefix classifies as conda", discovery.runtime_for_prefix(conda)["type"] == discovery.CONDA)
    r.check("a prefix with no interpreter is not a runtime", discovery.runtime_for_prefix(hollow) is None)
    r.check("no prefix is not a runtime", discovery.runtime_for_prefix("") is None)


def check_conda_discovery(r: Results, base: pathlib.Path) -> None:
    """Enumeration is a convenience; it must never raise and never invent."""
    home = base / "home"
    installation = make_prefix(home / "miniconda3", conda=True)
    named = make_prefix(installation / "envs" / "wangp", conda=True)
    (installation / "envs" / "broken" / "conda-meta").mkdir(parents=True, exist_ok=True)

    with which(None):
        found = discovery.find_conda_environments(runner=Runner(Completed(1)), environ={"HOME": str(home)})
    prefixes = {entry["prefix"] for entry in found}
    r.check("an installation's base environment is found", str(installation) in prefixes)
    r.check("a named environment is found", str(named) in prefixes)
    r.check("an environment with no interpreter is dropped", str(installation / "envs" / "broken") not in prefixes)
    names = {entry["prefix"]: entry["display_name"] for entry in found}
    r.check("a base environment is qualified by its folder", names.get(str(installation)) == "base (miniconda3)")
    r.check("a named environment keeps its name", names.get(str(named)) == "wangp")
    r.check("every entry is a conda runtime", all(entry["type"] == discovery.CONDA for entry in found))

    with which(None):
        r.check("a hostile environ does not raise",
                isinstance(discovery.find_conda_environments(runner=Runner(OSError("no")),
                                                             environ={"HOME": "\0", "CONDA_PREFIX": "\0/nope"}), list))
        r.check("an empty environ does not raise",
                isinstance(discovery.find_conda_environments(runner=Runner(Completed(1)), environ={}), list))
        r.check("a runner that explodes is survivable",
                isinstance(discovery.find_conda_environments(runner=Runner(RuntimeError("boom")), environ={}), list))

        extra = make_prefix(base / "wangp-checkout" / "venv")
        runtimes = discovery.find_runtimes(extra_prefixes=[str(extra), str(base / "not-there"), ""],
                                           runner=Runner(Completed(1)), environ={"HOME": str(home)})
        found_prefixes = [entry["prefix"] for entry in runtimes]
        r.check("a named venv is offered alongside conda", str(extra) in found_prefixes)
        r.check("a prefix that is not there is not offered", str(base / "not-there") not in found_prefixes)
        r.check("nothing is offered twice", len(found_prefixes) == len(set(found_prefixes)))

        r.check("no conda binary is None", discovery.conda_binary({}) is None)
    r.check("CONDA_EXE is only believed when the file is there",
            discovery.conda_binary({"CONDA_EXE": str(base / "no-such-conda")}) is None
            or discovery.conda_binary({"CONDA_EXE": str(base / "no-such-conda")}) != str(base / "no-such-conda"))


def check_probe(r: Results, base: pathlib.Path) -> None:
    """The probe decides a strategy by running one, and reports the one that worked."""
    root = make_root(base / "probe" / "wangp")
    venv = make_prefix(base / "probe" / "venv")
    venv_runtime = {"type": discovery.VENV, "prefix": str(venv), "display_name": "venv"}
    conda_prefix = make_prefix(base / "probe" / "conda", conda=True)
    conda_runtime = {"type": discovery.CONDA, "prefix": str(conda_prefix), "display_name": "wangp"}

    with which(None):
        runner = Runner(probe_reply())
        ok, strategy, detail = discovery.probe_runtime(venv_runtime, root, runner=runner, environ={"PATH": "/usr/bin"})
        r.check("a probe that works reports direct_python", (ok, strategy) == (True, discovery.DIRECT_PYTHON))
        r.check("the detail names the interpreter version", "3.10.6" in detail and "gradio" in detail)
        r.check("only one strategy was tried", len(runner.calls) == 1)

        command, kwargs = runner.calls[0]
        r.check("the probe runs the environment's own interpreter", command[0] == str(venv / "bin" / "python"))
        r.check("the probe is a -c program, not a script on disk", command[1] == "-c")
        r.check("the probe parses wgp.py instead of importing it",
                "ast.parse" in command[2] and "import_module" not in command[2] and "exec(" not in command[2])
        r.check("the root is passed as an argument, not interpolated", command[3] == str(root))
        r.check("the probe runs in the WanGP root", kwargs.get("cwd") == str(root))
        r.check("the probe is bounded by a timeout", kwargs.get("timeout") == discovery.PROBE_TIMEOUT_DIRECT)

        child = kwargs.get("env") or {}
        r.check("PATH is handed to the child", child.get("PATH") == "/usr/bin")
        r.check("PYTHONHOME is not inherited into the probe", "PYTHONHOME" not in child)
        r.check("PYTHONPATH is not inherited into the probe", "PYTHONPATH" not in child)
        r.check("the probe ignores user site-packages", child.get("PYTHONNOUSERSITE") == "1")

        # Forge's own interpreter would otherwise leak into the answer.
        poisoned = {"PYTHONHOME": "/forge", "PYTHONPATH": "/forge/lib", "PYTHONSTARTUP": "/forge/rc.py"}
        runner = Runner(probe_reply())
        discovery.probe_runtime(venv_runtime, root, runner=runner, environ=poisoned)
        leaked = {key for key in poisoned if key in (runner.calls[0][1].get("env") or {})}
        r.check("nothing of Forge's Python reaches the candidate", leaked == set(), str(sorted(leaked)))

        failed = Runner(Completed(1, "", "ModuleNotFoundError: No module named 'gradio'"))
        ok, strategy, detail = discovery.probe_runtime(venv_runtime, root, runner=failed, environ={})
        r.check("a probe that fails reports no strategy", (ok, strategy) == (False, ""))
        r.check("the failure detail names the strategy it tried", discovery.DIRECT_PYTHON in detail)
        r.check("the failure detail carries the child's complaint", "gradio" in detail)

        silent = Runner(Completed(0, "hello from somebody's .bashrc\n"))
        ok, _strategy, detail = discovery.probe_runtime(venv_runtime, root, runner=silent, environ={})
        r.check("a probe that prints nothing recognisable is a failure", ok is False and "recognisable" in detail)

        wrong = Runner(Completed(0, f'{discovery._PROBE_MARKER} {json.dumps({"python": "3.9.0", "imports": ["numpy"]})}'))
        ok, _strategy, detail = discovery.probe_runtime(venv_runtime, root, runner=wrong, environ={})
        r.check("a wgp.py that does not import gradio is not WanGP", ok is False and "gradio" in detail)

        broken = Runner(Completed(0, f"{discovery._PROBE_MARKER} not json"))
        r.check("a probe whose json is broken is a failure",
                discovery.probe_runtime(venv_runtime, root, runner=broken, environ={})[0] is False)

        timed_out = Runner(subprocess.TimeoutExpired(cmd="python", timeout=60))
        ok, _strategy, detail = discovery.probe_runtime(venv_runtime, root, runner=timed_out, environ={})
        r.check("a probe that times out is a failure, not an exception", ok is False and detail)

        hollow = {"type": discovery.VENV, "prefix": str(base / "probe" / "nothing-here")}
        ok, _strategy, detail = discovery.probe_runtime(hollow, root, runner=Runner(), environ={})
        r.check("a runtime with no interpreter has no strategy to try", ok is False and "unavailable" in detail)
        r.check("direct_python without an interpreter has no command",
                discovery.probe_command(hollow, root, discovery.DIRECT_PYTHON) is None)
        r.check("an unknown strategy has no command",
                discovery.probe_command(venv_runtime, root, "ssh_into_it") is None)

    with which("/opt/conda/bin/conda"):
        runner = Runner(Completed(1, "", "activation required"), probe_reply(python="3.11.2"))
        ok, strategy, detail = discovery.probe_runtime(conda_runtime, root, runner=runner, environ={})
        r.check("conda_run is the fallback, not the first try", (ok, strategy) == (True, discovery.CONDA_RUN))
        r.check("the direct attempt happened first", runner.calls[0][0][0] == str(conda_prefix / "bin" / "python"))
        conda_command = runner.calls[1][0]
        r.check("conda run targets the prefix, never a name",
                conda_command[:5] == ["/opt/conda/bin/conda", "run", "--no-capture-output", "-p", str(conda_prefix)])
        r.check("conda run gets the longer timeout", runner.calls[1][1].get("timeout") == discovery.PROBE_TIMEOUT_CONDA)
        r.check("the reported strategy is the one that worked", strategy in (discovery.DIRECT_PYTHON, discovery.CONDA_RUN))

        both_fail = Runner(Completed(1, "", "no"), Completed(1, "", "also no"))
        ok, strategy, detail = discovery.probe_runtime(conda_runtime, root, runner=both_fail, environ={})
        r.check("both strategies failing reports no strategy", (ok, strategy) == (False, ""))
        r.check("the detail mentions both attempts",
                discovery.DIRECT_PYTHON in detail and discovery.CONDA_RUN in detail)


def run() -> Results:
    r = Results("wangp setup")

    original_dir = config._state.get("dir")
    with tempfile.TemporaryDirectory(prefix="minipaint-wangp-config-") as temporary:
        base = pathlib.Path(temporary)
        try:
            check_location(r, base)
            check_schema(r)
            check_forbidden_keys(r, base)
            check_atomic_write(r, base)
            check_transaction(r, base)
            check_unreadable(r, base)
            check_root(r, base)
            check_gpus(r)
            check_interpreter(r, base)
            check_conda_discovery(r, base)
            check_probe(r, base)
        finally:
            # The path helpers are module state; a later suite in the same
            # process must not inherit a directory that is about to be deleted.
            config.use_config_dir(original_dir)

    return r


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
