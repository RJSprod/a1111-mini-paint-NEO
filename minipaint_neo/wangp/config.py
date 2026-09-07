"""What survives a restart, and only that.

The setup wizard asks four questions whose answers are expensive to find
again - where WanGP is installed, which interpreter can run it, which
physical GPU it may have, how it is reached - and cheap to store. Everything
else the integration knows is about *this* run: a pid, a loopback port, a
runtime instance id, a browser channel, a handoff id, whatever the bridge
last said a receiver looked like. None of that is written here, ever. A
config file that remembered a port would send the next Forge start at a
stranger's process; one that remembered a handoff id would hand a stale image
to a session that never asked for it. So this module keeps an explicit
whitelist of the keys that may be persisted and drops everything else on the
way out, whether it arrived from an older schema, a hand-edited file or a
caller that stuffed a spare field into ``runtime``.

The other decision encoded here is the setup transaction. A reinitialize that
half-succeeded used to be the worst outcome available: the old working setup
gone, the new one not yet true. So there are three files - active, backup and
pending - and the only moment anything becomes true is an ``os.replace`` of a
fully written temp file. The active config is never absent, never truncated
and never overwritten by a config that is not worth restoring.

Nothing in here touches the disk to find out whether the recorded root, the
recorded interpreter or the recorded GPU still exist. ``validate()`` judges
the *shape* of the config only; ``discovery`` judges reality.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
import tempfile
import typing

from .. import paths
from .errors import (
    CONFIG_SCHEMA_TOO_NEW,
    CONFIG_UNREADABLE,
    GPU_UUID_MISSING,
    RUNTIME_MISSING,
    SETUP_REQUIRED,
    WANGP_ROOT_MISSING,
    IntegrationError,
)

SCHEMA_VERSION = 2

#: The folder under the host's data root. Named after the extension because a
#: data root is shared with every other extension that has one.
DIRECTORY_NAME = "a1111-mini-paint-NEO"

#: Where the settings go when no host data root could be found. The extension
#: folder is already named after the extension, so the fallback gets a plain
#: subfolder rather than a duplicated one.
FALLBACK_DIRECTORY_NAME = "data"

CONFIG_NAME = "wan2gp.json"
BACKUP_NAME = "wan2gp.backup.json"
PENDING_NAME = "wan2gp.pending.json"
RUNTIME_NAME = "runtime"

#: The mount point is a constant of the proxy, not a configurable value: a
#: config file that could move the route could also aim it somewhere that is
#: outside whatever authentication Forge puts in front of it. It is written
#: because the schema says so, and normalised back on the way out.
DEFAULT_PROXY_PATH = "/wan2gp"

#: Lazy means "start WanGP when someone opens the tab", which is the only
#: policy implemented. An unrecognised value becomes lazy rather than an
#: error, because lazy is the one that starts no process on its own.
AUTO_START_LAZY = "lazy"
AUTO_START_CHOICES = (AUTO_START_LAZY,)

#: The keys each section may contain. Anything else is dropped, silently and
#: on purpose - see NEVER_PERSISTED.
TOP_LEVEL_KEYS = ("schema_version", "initialized", "wangp_root", "runtime", "gpu", "integration")
RUNTIME_KEYS = ("type", "prefix", "display_name", "launch_strategy")
GPU_KEYS = ("uuid",)
INTEGRATION_KEYS = ("proxy_path", "auto_start")

#: Named so a reader of this file, and of a review of it, can see the rule
#: rather than infer it from a whitelist. These are the per-run facts from
#: section 9's "do not persist" list; the whitelists above already exclude
#: them, and ``_section`` removes them again by name so that widening a
#: whitelist by accident cannot quietly start writing one.
NEVER_PERSISTED = frozenset(
    {
        "pid",
        "port",
        "backend_port",
        "channel_id",
        "channel_nonce",
        "nonce",
        "secret",
        "bridge_secret",
        "session_hash",
        "instance_id",
        "handoff_id",
        "handoff_ids",
        "handoff_root",
        "handoff_path",
        "state_revision",
        "revision",
        "receivers",
        "receiver_cache",
    }
)

#: An NVIDIA UUID, or the MIG device form of one. Shape only: whether this
#: particular GPU is in the machine is discovery's question, and a value that
#: is not a UUID at all is not something to go looking for.
GPU_UUID_RE = re.compile(r"\A(?:MIG-)?GPU-[0-9a-fA-F][0-9a-fA-F-]{7,}\Z")

_LOG_PREFIX = "MiniPaint WanGP:"

#: Where config_dir() settled, and whether the fallback line has been printed.
#: Resolution is cached because config_dir() is called on every path lookup and
#: the answer cannot change inside one process.
_state: typing.Dict[str, typing.Any] = {"dir": None, "announced": False}


# --------------------------------------------------------------- location --


def _host_data_path() -> typing.Any:
    from modules import paths_internal

    return paths_internal.data_path


def _host_paths_data_path() -> typing.Any:
    from modules import paths as host_paths

    return host_paths.data_path


def _host_cmd_opts_data_dir() -> typing.Any:
    from modules import shared

    return shared.cmd_opts.data_dir


#: In the order the design intent asks for them. Each is a separate callable
#: so that a host with two of the three still answers, and so a test can pass
#: its own list without a Forge on the path.
HOST_DATA_ROOTS: typing.Tuple[typing.Tuple[str, typing.Callable[[], typing.Any]], ...] = (
    ("modules.paths_internal.data_path", _host_data_path),
    ("modules.paths.data_path", _host_paths_data_path),
    ("modules.shared.cmd_opts.data_dir", _host_cmd_opts_data_dir),
)


def data_root(sources: typing.Optional[typing.Sequence] = None) -> typing.Tuple[pathlib.Path, str]:
    """The host's data root and, when it had to be guessed, why.

    The second element is empty when a host API answered and a sentence when
    it did not. The spec insists the fallback be explicit and logged rather
    than silently correct-looking, because settings landing inside the
    extension folder are settings a ``git pull`` can take away.
    """
    if sources is None:
        sources = HOST_DATA_ROOTS

    tried = []
    for name, source in sources:
        try:
            value = source()
        except Exception as error:  # the host may lack the module, the
            tried.append(f"{name} ({error})")  # attribute, or parsed cmd_opts yet
            continue
        if value:
            return pathlib.Path(str(value)), ""
        tried.append(f"{name} (empty)")

    detail = ", ".join(tried) or "no host data root was offered"
    return paths.root_path, f"no host data root: {detail}"


def use_config_dir(directory: typing.Optional[typing.Any]) -> None:
    """Point every path helper at ``directory``; None restores the host's.

    The seam exists so the transaction can be exercised without a Forge and
    without writing into anybody's real data root. Nothing in the extension
    calls it.
    """
    _state["dir"] = pathlib.Path(str(directory)) if directory is not None else None
    _state["announced"] = False


def config_dir() -> pathlib.Path:
    """``<data root>/a1111-mini-paint-NEO``, created if it is not there."""
    directory = _state.get("dir")
    if directory is None:
        root, fallback_reason = data_root()
        name = FALLBACK_DIRECTORY_NAME if fallback_reason else DIRECTORY_NAME
        directory = pathlib.Path(root) / name
        _state["dir"] = directory
        if fallback_reason and not _state["announced"]:
            _state["announced"] = True
            print(
                f"{_LOG_PREFIX} {fallback_reason}; keeping WanGP settings in {directory}, "
                "which is inside the extension folder and goes away if the extension is reinstalled"
            )
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def config_path() -> pathlib.Path:
    return config_dir() / CONFIG_NAME


def backup_path() -> pathlib.Path:
    return config_dir() / BACKUP_NAME


def pending_path() -> pathlib.Path:
    return config_dir() / PENDING_NAME


def runtime_dir() -> pathlib.Path:
    """Per-run scratch - the handoff root lives under here.

    It sits beside the config because it wants the same data root, and it is
    emphatically not configuration: nothing under it is read back after a
    restart, and no filename under it is ever written into a config file.
    """
    directory = config_dir() / RUNTIME_NAME
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass  # Windows and most network filesystems; the path is still ours
    return directory


# ------------------------------------------------------------------ write --


def atomic_write(path: typing.Any, text: str) -> None:
    """Write ``text`` so that ``path`` is either the old file or the new one.

    The temp file is made in the destination's own directory, so the final
    ``os.replace`` is a rename within one filesystem - the only kind that is
    atomic. Everything the caller wrote is on the platter before the rename,
    which is what stops a crash from leaving a config that parses as far as
    the truncation and no further.
    """
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            try:
                os.fsync(stream.fileno())
            except (OSError, AttributeError, ValueError):
                pass  # not every filesystem or platform supports it
        # mkstemp makes a 0600 file; keep whatever mode the config already had
        # so replacing it does not quietly change who can read it.
        try:
            os.chmod(temp_name, path.stat().st_mode & 0o7777)
        except OSError:
            pass
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise

    # Durability of the rename itself, where the platform has the concept.
    try:
        directory = os.open(str(path.parent), os.O_RDONLY)
    except (OSError, AttributeError):
        return
    try:
        os.fsync(directory)
    except OSError:
        pass
    finally:
        os.close(directory)


def _section(raw: typing.Any, allowed: typing.Sequence[str]) -> dict:
    """One nested block, reduced to the keys that may be stored.

    Two passes on purpose. The first drops the per-run facts by name, so the
    rule from section 9 is visible in the code and a future key added to
    ``allowed`` cannot smuggle one back. The second keeps only what the
    schema declares, which is what actually makes the file forward-safe.
    """
    if not isinstance(raw, dict):
        return {}
    kept = {key: value for key, value in raw.items() if key not in NEVER_PERSISTED}
    return {key: kept[key] for key in allowed if key in kept}


def _text(value: typing.Any) -> str:
    return str(value).strip() if isinstance(value, (str, os.PathLike)) else ""


# ----------------------------------------------------------------- schema --


@dataclasses.dataclass
class Config:
    """The whole of what a restart may know about the WanGP integration."""

    schema_version: int = SCHEMA_VERSION
    initialized: bool = False
    wangp_root: str = ""
    runtime: dict = dataclasses.field(default_factory=dict)
    gpu: dict = dataclasses.field(default_factory=dict)
    integration: dict = dataclasses.field(default_factory=dict)

    def as_dict(self) -> dict:
        """The document that gets written. Unknown keys do not survive this."""
        runtime = _section(self.runtime, RUNTIME_KEYS)
        integration = _section(self.integration, INTEGRATION_KEYS)

        auto_start = _text(integration.get("auto_start"))
        return {
            "schema_version": SCHEMA_VERSION,
            "initialized": bool(self.initialized),
            "wangp_root": _text(self.wangp_root),
            "runtime": {
                "type": _text(runtime.get("type")),
                "prefix": _text(runtime.get("prefix")),
                "display_name": _text(runtime.get("display_name")),
                # Empty means "nobody has proved a strategy yet"; runtime.py
                # asks discovery rather than assuming one.
                "launch_strategy": _text(runtime.get("launch_strategy")),
            },
            "gpu": {"uuid": _text(_section(self.gpu, GPU_KEYS).get("uuid"))},
            "integration": {
                "proxy_path": DEFAULT_PROXY_PATH,
                "auto_start": auto_start if auto_start in AUTO_START_CHOICES else AUTO_START_LAZY,
            },
        }

    @classmethod
    def from_dict(cls, data: typing.Any) -> "Config":
        """Parse a config document, migrating an older schema explicitly."""
        if not isinstance(data, dict):
            raise IntegrationError(CONFIG_UNREADABLE, f"expected an object, found {type(data).__name__}")

        version = data.get("schema_version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise IntegrationError(CONFIG_UNREADABLE, f"schema_version is {version!r}")
        if version > SCHEMA_VERSION:
            # No guessing. A newer file may mean something different by a key
            # this version already has, and writing over it would take the
            # newer extension's setup with it.
            raise IntegrationError(
                CONFIG_SCHEMA_TOO_NEW, f"file is schema {version}, this extension writes {SCHEMA_VERSION}"
            )
        if version == 1:
            data = _migrate_v1(data)
        elif version != SCHEMA_VERSION:
            raise IntegrationError(CONFIG_UNREADABLE, f"no migration from schema {version}")

        return cls(
            schema_version=SCHEMA_VERSION,
            initialized=bool(data.get("initialized")),
            wangp_root=_text(data.get("wangp_root")),
            runtime=_section(data.get("runtime"), RUNTIME_KEYS),
            gpu=_section(data.get("gpu"), GPU_KEYS),
            integration=_section(data.get("integration"), INTEGRATION_KEYS),
        )

    def validate(self) -> list:
        """Failure codes for the shape of this config, in reading order.

        Shape only: an empty root and a root that was deleted yesterday are
        different problems, and only the first one is answerable without
        touching the disk. ``discovery`` answers the second.
        """
        codes = []
        if not self.initialized:
            codes.append(SETUP_REQUIRED)
        if not _text(self.wangp_root):
            codes.append(WANGP_ROOT_MISSING)
        runtime = self.runtime if isinstance(self.runtime, dict) else {}
        # A display name is a label; the prefix is the thing an interpreter is
        # resolved from, so a config without one describes no environment.
        if not _text(runtime.get("prefix")) or not _text(runtime.get("type")):
            codes.append(RUNTIME_MISSING)
        gpu = self.gpu if isinstance(self.gpu, dict) else {}
        if not GPU_UUID_RE.match(_text(gpu.get("uuid"))):
            codes.append(GPU_UUID_MISSING)
        return codes


def _migrate_v1(data: dict) -> dict:
    """Schema 1 (pre-release) to schema 2, one named change at a time.

    Three things moved:

    * the GPU was a top-level ``gpu_uuid`` string and is now ``gpu.uuid``,
      because the block gained room for the diagnostics discovery reports;
    * ``runtime`` had no ``launch_strategy``: schema 1 always ran the
      interpreter directly. The strategy is dropped rather than assumed to be
      ``direct_python``, so the wizard re-probes it and a Conda environment
      that needs activation is not launched the way that never worked;
    * there was no ``integration`` block at all - the proxy path and the
      start policy were constants - so it is created at its defaults.
    """
    migrated = dict(data)
    migrated["schema_version"] = SCHEMA_VERSION

    gpu = migrated.get("gpu")
    if not isinstance(gpu, dict):
        gpu = {}
    if not gpu.get("uuid") and migrated.get("gpu_uuid"):
        gpu["uuid"] = migrated["gpu_uuid"]
    migrated["gpu"] = gpu
    migrated.pop("gpu_uuid", None)

    runtime = dict(migrated.get("runtime") or {}) if isinstance(migrated.get("runtime"), dict) else {}
    runtime.pop("launch_strategy", None)
    migrated["runtime"] = runtime

    integration = migrated.get("integration")
    migrated["integration"] = integration if isinstance(integration, dict) else {}
    migrated["integration"].setdefault("proxy_path", DEFAULT_PROXY_PATH)
    migrated["integration"].setdefault("auto_start", AUTO_START_LAZY)
    return migrated


def _dumps(config: Config) -> str:
    return json.dumps(config.as_dict(), indent=2) + "\n"


def _read(path: pathlib.Path) -> typing.Optional[Config]:
    """Parse one of the three files, or None when it is not there."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise IntegrationError(CONFIG_UNREADABLE, f"{path.name}: {error}")

    try:
        data = json.loads(text)
    except ValueError as error:
        raise IntegrationError(CONFIG_UNREADABLE, f"{path.name}: {error}")
    return Config.from_dict(data)


# ------------------------------------------------------------ transaction --


def load() -> typing.Optional[Config]:
    """The active config. None means "never set up", which is not an error."""
    return _read(config_path())


def write_pending(config: Config) -> None:
    """Put a candidate setup where it can be validated before it counts.

    Writing this file changes nothing about the running integration: the
    active config is still whatever it was, and stays that way until
    ``promote_pending``.
    """
    atomic_write(pending_path(), _dumps(config))


def promote_pending() -> Config:
    """Make the pending config the active one, keeping what it replaces.

    The backup is written from the active file's own parsed contents rather
    than moved, so there is no instant where the active config does not
    exist. An active config that is not initialized is *not* copied over the
    backup: the backup means "the last setup that worked", and a config that
    the reinitialize flow already marked incomplete is not that.
    """
    pending = _read(pending_path())
    if pending is None:
        raise IntegrationError(CONFIG_UNREADABLE, f"{PENDING_NAME} is not there to promote")

    try:
        active = _read(config_path())
    except IntegrationError:
        # Unreadable or from a newer extension: it is being replaced anyway,
        # and it is not something restore_backup() could ever put back.
        active = None
    if active is not None and active.initialized:
        atomic_write(backup_path(), _dumps(active))

    os.replace(str(pending_path()), str(config_path()))
    return pending


def restore_backup() -> typing.Optional[Config]:
    """Put the previous working setup back. None when there is no backup.

    The backup file stays where it is - restoring twice is allowed, and a
    restore that turns out to be wrong should not have eaten the evidence.
    The caller revalidates the restored root, environment, GPU and bridge
    before calling the integration ready; this only puts the document back.
    """
    backup = _read(backup_path())
    if backup is None:
        return None
    atomic_write(config_path(), _dumps(backup))
    return backup


def clear() -> None:
    """Mark the integration uninitialized, keeping everything else.

    This is the front half of reinitialize: the active config becomes
    incomplete so the tab shows the wizard instead of an iframe, and the
    setup it described is preserved in the backup so "restore the previous
    working integration" still has something to restore. It deletes no
    backup, no WanGP install, no environment and no model.
    """
    try:
        active = load()
    except IntegrationError:
        # A file we cannot parse cannot be backed up and cannot be edited;
        # replacing it with an honest empty config is the only move that
        # leaves the tab in a state a user can act on.
        active = None

    if active is not None and active.initialized:
        atomic_write(backup_path(), _dumps(active))

    cleared = active if active is not None else Config()
    cleared.initialized = False
    atomic_write(config_path(), _dumps(cleared))

    # A pending config from an abandoned attempt describes a setup nobody
    # validated; leaving it around invites a later promote of a stale one.
    try:
        pending_path().unlink()
    except (OSError, FileNotFoundError):
        pass
