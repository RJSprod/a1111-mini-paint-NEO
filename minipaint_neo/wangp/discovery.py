"""Is any of this real: the install, the interpreter, the card.

Setup asks four questions, and this module answers all four without changing
anything it looks at. Is that directory a WanGP checkout? Can that Python
environment start it? Which physical GPUs exist? Is our companion plugin in
there, and is it the version we ship?

Two rules shape every answer here. The first is that Forge's own interpreter
must never import WanGP: a probe that imports the ML stack would pull torch
and a CUDA context into the process that draws the UI, so the compatibility
check runs *in the candidate environment* and even there only parses wgp.py
rather than executing it. The second is that a thing we cannot prove is a
thing we say no to - a GPU UUID that is not present is not "close enough" to
another card, a directory that only half looks like WanGP is not WanGP, and a
plugin folder that is not ours is not something we overwrite.

Everything that touches the OS takes a seam - a runner, an environ, a source
directory - so the whole module can be exercised with no WanGP, no conda and
no NVIDIA driver anywhere near the machine.
"""

from __future__ import annotations

import csv
import io
import json
import os
import pathlib
import shutil
import subprocess
import typing

from . import errors

#: Where the companion plugin lives inside someone else's WanGP.
PLUGINS_DIR_NAME = "plugins"
BRIDGE_FOLDER_NAME = "wan2gp-minipaint-bridge"
BRIDGE_INFO_NAME = "plugin_info.json"

#: The file that makes a directory recognisably WanGP rather than a folder
#: that happens to share its name.
ROOT_MARKER = "wgp.py"

#: WanGP keeps its own code in packages next to wgp.py. ``shared`` is the one
#: the design names; the others are the model families that have travelled
#: with it, and one of them being present is enough structure to believe the
#: checkout is a real one rather than a stray script.
PROJECT_DIR_CANDIDATES = ("shared", "wan", "ltx_video", "hyvideo", "preprocessing", "models")

#: Not copied into the user's install: build droppings and VCS metadata.
BRIDGE_COPY_SKIP = ("__pycache__", ".git", ".hg", ".svn", ".mypy_cache", ".pytest_cache")
BRIDGE_COPY_SKIP_SUFFIXES = (".pyc", ".pyo", ".orig", ".rej")

#: How long a probe may take. Direct interpreter startup is quick; ``conda
#: run`` resolves an environment first and is routinely slower on Windows.
PROBE_TIMEOUT_DIRECT = 60.0
PROBE_TIMEOUT_CONDA = 180.0

#: Launch strategies, persisted into config["runtime"]["launch_strategy"].
DIRECT_PYTHON = "direct_python"
CONDA_RUN = "conda_run"

#: Runtime kinds.
CONDA = "conda"
VENV = "venv"

_PROBE_MARKER = "MINIPAINT_WANGP_PROBE"

# Deliberately block-free so it survives being passed as one ``-c`` argument
# on Windows as well as POSIX. It reads wgp.py and *parses* it - never
# imports it - which proves three things at once: the file is there, it is
# syntactically valid for this interpreter's Python version, and it imports
# gradio, which is what makes it the WanGP application rather than a helper
# script of the same name.
_PROBE_SOURCE = "\n".join(
    (
        "import ast,json,os,sys",
        "root=sys.argv[1]",
        "src=open(os.path.join(root,%r),'r',encoding='utf-8',errors='replace').read()" % ROOT_MARKER,
        "tree=ast.parse(src,filename=%r)" % ROOT_MARKER,
        "mods={(n.module or '').split('.')[0] for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)}",
        "mods|={a.name.split('.')[0] for n in ast.walk(tree) if isinstance(n,ast.Import) for a in n.names}",
        "info={'python':sys.version.split()[0],'imports':sorted(m for m in ('gradio','torch','numpy') if m in mods),'modules':len(mods)}",
        "print(%r+' '+json.dumps(info))" % _PROBE_MARKER,
    )
)

_MAX_DETAIL = 400


def _detail(value: typing.Any) -> str:
    """One short line for a log. Never shown to a user as-is."""
    text = " ".join(str(value or "").split())
    return text[:_MAX_DETAIL]


# --------------------------------------------------------------------- GPU --


class Gpu:
    """One physical NVIDIA device, as both discovery paths describe it."""

    __slots__ = ("index", "name", "uuid", "total_mb", "bus_id")

    def __init__(self, index: int, name: str, uuid: str, total_mb: int = 0, bus_id: str = "") -> None:
        self.index = int(index)
        self.name = str(name or "").strip()
        self.uuid = str(uuid or "").strip()
        self.total_mb = int(total_mb or 0)
        self.bus_id = str(bus_id or "").strip()

    @property
    def label(self) -> str:
        """"NVIDIA GeForce RTX 5090 - 32 GB" - what the wizard shows.

        VRAM is rounded to whole gigabytes because that is how the card is
        sold; the exact MiB is in the diagnostics, not in a radio button.
        """
        if self.total_mb > 0:
            return f"{self.name} - {round(self.total_mb / 1024)} GB"
        return self.name or self.uuid

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "name": self.name,
            "uuid": self.uuid,
            "total_mb": self.total_mb,
            "bus_id": self.bus_id,
            "label": self.label,
        }

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<Gpu {self.index} {self.name!r} {self.uuid}>"

    def __eq__(self, other: typing.Any) -> bool:
        return isinstance(other, Gpu) and other.uuid == self.uuid and other.index == self.index

    def __hash__(self) -> int:
        return hash((self.index, self.uuid))


def parse_nvidia_smi(text: str) -> typing.List[Gpu]:
    """Parse ``--format=csv,noheader,nounits`` output into devices.

    Pure, so the whole nvidia-smi path is testable on a machine with no
    driver. Anything malformed is dropped rather than repaired: a row we
    cannot read completely would become a GPU we cannot prove exists, and the
    UUID is the thing the launch is going to trust.
    """
    devices: typing.List[Gpu] = []
    if not isinstance(text, str) or not text.strip():
        return devices

    # csv, because a product name may legitimately contain a comma and
    # nvidia-smi quotes it when it does. ``skipinitialspace`` is what makes
    # that quoting work: nvidia-smi separates with ", ", and a quote that does
    # not start the field is just a character, so without this a name like
    # "RTX A6000, Ada" splits in two and every field after it shifts along -
    # putting half a name where the UUID should be.
    for row in csv.reader(io.StringIO(text), skipinitialspace=True):
        fields = [str(cell).strip() for cell in row]
        if len(fields) < 3:
            continue
        try:
            index = int(fields[0])
        except (TypeError, ValueError):
            continue
        name, uuid = fields[1], fields[2]
        if not uuid or uuid.upper() == "N/A":
            continue
        try:
            total_mb = int(float(fields[3])) if len(fields) > 3 and fields[3] not in ("", "N/A") else 0
        except (TypeError, ValueError):
            total_mb = 0
        bus_id = fields[4] if len(fields) > 4 else ""
        devices.append(Gpu(index=index, name=name, uuid=uuid, total_mb=total_mb, bus_id=bus_id))
    return devices


def _gpus_from_nvml() -> typing.List[Gpu]:
    """NVML if the environment happens to have it. It is not a dependency."""
    try:
        import pynvml  # type: ignore
    except Exception:
        return []

    devices: typing.List[Gpu] = []
    try:
        pynvml.nvmlInit()
    except Exception:
        return []
    try:
        count = int(pynvml.nvmlDeviceGetCount())
        for index in range(count):
            try:
                handle = pynvml.nvmlDeviceGetHandleByIndex(index)
                name = pynvml.nvmlDeviceGetName(handle)
                uuid = pynvml.nvmlDeviceGetUUID(handle)
                name = name.decode("utf-8", "replace") if isinstance(name, bytes) else str(name)
                uuid = uuid.decode("utf-8", "replace") if isinstance(uuid, bytes) else str(uuid)
                try:
                    total_mb = int(pynvml.nvmlDeviceGetMemoryInfo(handle).total // (1024 * 1024))
                except Exception:
                    total_mb = 0
                try:
                    bus_id = pynvml.nvmlDeviceGetPciInfo(handle).busId
                    bus_id = bus_id.decode("utf-8", "replace") if isinstance(bus_id, bytes) else str(bus_id)
                except Exception:
                    bus_id = ""
                if uuid:
                    devices.append(Gpu(index=index, name=name, uuid=uuid, total_mb=total_mb, bus_id=bus_id))
            except Exception:
                continue
    except Exception:
        devices = []
    finally:
        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass
    return devices


def _gpus_from_smi(runner: typing.Callable[..., typing.Any]) -> typing.List[Gpu]:
    binary = shutil.which("nvidia-smi")
    if not binary:
        return []
    command = [
        binary,
        "--query-gpu=index,name,uuid,memory.total,pci.bus_id",
        "--format=csv,noheader,nounits",
    ]
    try:
        completed = runner(command, capture_output=True, text=True, timeout=30)
    except Exception:
        return []
    if getattr(completed, "returncode", 1) != 0:
        return []
    return parse_nvidia_smi(getattr(completed, "stdout", "") or "")


def list_gpus(runner: typing.Callable[..., typing.Any] = subprocess.run, use_nvml: bool = True) -> typing.List[Gpu]:
    """Physical NVIDIA devices: NVML, then nvidia-smi, then nothing.

    An empty list is a legitimate answer and the wizard must treat it as one -
    no GPU means no setup, not a default selection.
    """
    devices = _gpus_from_nvml() if use_nvml else []
    if not devices:
        devices = _gpus_from_smi(runner)
    return devices


def find_gpu(uuid: str, devices: typing.Optional[typing.List[Gpu]] = None) -> typing.Optional[Gpu]:
    """The device with exactly this UUID, or nothing at all.

    There is no nearest match and no ordinal fallback: the whole reason the
    config stores a UUID instead of an index is that "the other card" is
    never an acceptable substitute. The caller fails closed on None.
    """
    wanted = str(uuid or "").strip()
    if not wanted:
        return None
    for device in devices if devices is not None else list_gpus():
        if device.uuid == wanted:
            return device
    return None


# -------------------------------------------------------------------- root --


def root_problems(path: typing.Any) -> typing.List[typing.Tuple[str, str]]:
    """Every reason this directory is not a usable WanGP root, with detail.

    Split out from :func:`validate_root` because the codes all collapse to
    one - the wizard still wants to say *which* check failed, and a test
    wants to assert on that without parsing a sentence.
    """
    problems: typing.List[typing.Tuple[str, str]] = []
    text = str(path or "").strip()
    if not text:
        return [(errors.WANGP_ROOT_MISSING, "no path given")]

    try:
        root = pathlib.Path(text).expanduser()
    except (OSError, ValueError) as error:
        return [(errors.WANGP_ROOT_MISSING, _detail(error))]

    if not root.is_dir():
        return [(errors.WANGP_ROOT_MISSING, "not a directory")]
    if not os.access(str(root), os.R_OK | os.X_OK):
        problems.append((errors.WANGP_ROOT_MISSING, "not readable"))

    if not (root / ROOT_MARKER).is_file():
        problems.append((errors.WANGP_ROOT_MISSING, f"no {ROOT_MARKER}"))

    if not any((root / name).is_dir() for name in PROJECT_DIR_CANDIDATES):
        problems.append((errors.WANGP_ROOT_MISSING, "no shared/ or equivalent project directory"))

    # The plugins folder is allowed not to exist yet - a fresh WanGP has
    # none - but then the root has to be writable, because installing the
    # bridge will have to create it. Checking is not creating: nothing here
    # writes to someone else's install.
    plugins = root / PLUGINS_DIR_NAME
    if plugins.exists():
        if not plugins.is_dir():
            problems.append((errors.WANGP_ROOT_MISSING, f"{PLUGINS_DIR_NAME} is not a directory"))
        elif not os.access(str(plugins), os.W_OK):
            problems.append((errors.WANGP_ROOT_MISSING, f"{PLUGINS_DIR_NAME} is not writable"))
    elif not os.access(str(root), os.W_OK):
        problems.append((errors.WANGP_ROOT_MISSING, f"{PLUGINS_DIR_NAME} cannot be created"))

    return problems


def validate_root(path: typing.Any) -> typing.List[str]:
    """Failure codes for a candidate WanGP root; empty means it is one."""
    codes: typing.List[str] = []
    for code, _reason in root_problems(path):
        if code not in codes:
            codes.append(code)
    return codes


# ------------------------------------------------------------------ bridge --


def bridge_dir(root: typing.Any) -> pathlib.Path:
    """``<root>/plugins/wan2gp-minipaint-bridge``. The only folder we own."""
    return pathlib.Path(str(root or "")).expanduser() / PLUGINS_DIR_NAME / BRIDGE_FOLDER_NAME


def read_bridge_info(root: typing.Any) -> typing.Optional[dict]:
    """The installed plugin's ``plugin_info.json``, or None.

    None covers every way of not having one - absent folder, absent file,
    unreadable, not JSON, JSON that is not an object - because the caller
    does the same thing in all of those cases.
    """
    try:
        info_path = bridge_dir(root) / BRIDGE_INFO_NAME
        if not info_path.is_file():
            return None
        data = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def bridge_version(info: typing.Optional[dict]) -> str:
    """The version string a plugin manifest declares, under either name."""
    if not isinstance(info, dict):
        return ""
    for key in ("bridge_version", "version", "plugin_version"):
        value = info.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _bridge_is_disabled(root: typing.Any, info: dict) -> bool:
    """Whether WanGP would skip this plugin.

    Two conventions are honoured because plugin loaders differ: a falsy
    ``enabled`` in the manifest, and a ``.disabled`` marker file next to it.
    Neither is authoritative for every WanGP build, so the handshake remains
    the real proof - this only lets setup explain a silent plugin.
    """
    for key in ("enabled", "active"):
        if key in info and not bool(info.get(key)):
            return True
    if bool(info.get("disabled")):
        return True
    try:
        return (bridge_dir(root) / ".disabled").exists()
    except OSError:
        return False


def bridge_status(root: typing.Any, expected_version: str = "") -> typing.Tuple[str, str]:
    """``("", detail)`` when the shipped bridge is installed and switched on.

    Otherwise one of BRIDGE_MISSING / BRIDGE_DISABLED /
    BRIDGE_VERSION_MISMATCH. Version comparison is exact equality, not a
    range: the two halves of the protocol are released together, so "newer"
    is as wrong as "older" and there is nothing to be clever about.
    """
    info = read_bridge_info(root)
    if info is None:
        return errors.BRIDGE_MISSING, _detail(f"no {BRIDGE_INFO_NAME} under {BRIDGE_FOLDER_NAME}")

    if _bridge_is_disabled(root, info):
        return errors.BRIDGE_DISABLED, "the plugin manifest says it is switched off"

    installed = bridge_version(info)
    wanted = str(expected_version or "").strip()
    if wanted and installed != wanted:
        return errors.BRIDGE_VERSION_MISMATCH, _detail(f"installed {installed or 'unknown'}, expected {wanted}")

    return "", _detail(f"version {installed or 'unknown'}")


def _bridge_source(source_dir: typing.Any) -> pathlib.Path:
    """The repository's bridge folder, given it or its parent."""
    source = pathlib.Path(str(source_dir or "")).expanduser()
    if source.name == BRIDGE_FOLDER_NAME and source.is_dir():
        return source
    candidate = source / BRIDGE_FOLDER_NAME
    if candidate.is_dir():
        return candidate
    raise errors.IntegrationError(errors.BRIDGE_MISSING, _detail(f"no {BRIDGE_FOLDER_NAME} under {source}"))


def _copy_bridge_tree(source: pathlib.Path, destination: pathlib.Path) -> typing.Tuple[int, int]:
    """Copy the plugin folder, skipping build droppings. Returns (files, bytes)."""
    files = 0
    total = 0
    for current, directories, names in os.walk(str(source)):
        directories[:] = [name for name in directories if name not in BRIDGE_COPY_SKIP and not name.startswith(".")]
        relative = pathlib.Path(current).relative_to(source)
        target_dir = destination / relative
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in names:
            if name in BRIDGE_COPY_SKIP or name.endswith(BRIDGE_COPY_SKIP_SUFFIXES):
                continue
            source_file = pathlib.Path(current) / name
            if source_file.is_symlink() or not source_file.is_file():
                continue
            shutil.copy2(str(source_file), str(target_dir / name))
            files += 1
            total += source_file.stat().st_size
    return files, total


def install_bridge(root: typing.Any, source_dir: typing.Any) -> dict:
    """Put our plugin folder - and nothing else - into that WanGP install.

    The refusals matter more than the copy. The destination is recomputed
    from the root rather than taken from anywhere, it must resolve to a path
    inside ``<root>/plugins`` with our exact folder name, and an existing
    folder is only replaced when it carries our manifest. A symlink there, a
    name collision with somebody else's plugin, or a destination that escapes
    the root all stop the install instead of being worked around.
    """
    problems = validate_root(root)
    if problems:
        raise errors.IntegrationError(problems[0], _detail(root_problems(root)[0][1]))

    source = _bridge_source(source_dir)
    root_path = pathlib.Path(str(root)).expanduser()
    plugins = root_path / PLUGINS_DIR_NAME
    destination = plugins / BRIDGE_FOLDER_NAME

    if destination.is_symlink() or plugins.is_symlink():
        raise errors.IntegrationError(errors.INTERNAL_ERROR, "the plugin destination is a symlink; nothing was written")

    replaced = destination.exists()
    if replaced:
        if not destination.is_dir():
            raise errors.IntegrationError(errors.INTERNAL_ERROR, "the plugin destination exists and is not a directory")
        if read_bridge_info(root_path) is None:
            raise errors.IntegrationError(
                errors.INTERNAL_ERROR,
                f"{BRIDGE_FOLDER_NAME} exists without our {BRIDGE_INFO_NAME}; it was left untouched",
            )

    try:
        plugins.mkdir(parents=True, exist_ok=True)
        # Resolve only after the parent exists, then prove the folder we are
        # about to delete and rewrite is still the one under this root.
        resolved_root = root_path.resolve()
        resolved_destination = (plugins.resolve() / BRIDGE_FOLDER_NAME)
        if resolved_destination.parent != (resolved_root / PLUGINS_DIR_NAME):
            raise errors.IntegrationError(errors.INTERNAL_ERROR, "the plugin destination is outside the WanGP root")
        if replaced:
            shutil.rmtree(str(resolved_destination))
        files, total = _copy_bridge_tree(source, resolved_destination)
    except errors.IntegrationError:
        raise
    except OSError as error:
        raise errors.IntegrationError(errors.BRIDGE_MISSING, _detail(error))

    return {
        "ok": True,
        "path": str(destination),
        "files": files,
        "bytes": total,
        "replaced": replaced,
        "version": bridge_version(read_bridge_info(root_path)),
        # WanGP loads plugins once, at startup. Copying files over a running
        # instance does not activate them, so the caller coordinates a
        # managed restart rather than assuming the new code is live.
        "restart_required": True,
    }


# ----------------------------------------------------------------- runtime --


def _environ(environ: typing.Optional[typing.Mapping[str, str]]) -> typing.Mapping[str, str]:
    return os.environ if environ is None else environ


def interpreter_for(runtime: typing.Any) -> typing.Optional[pathlib.Path]:
    """The Python inside a runtime prefix, or None.

    Existence is the whole test. Returning a plausible-looking path that is
    not there would only move the failure to launch time, where it is much
    harder to explain.
    """
    if isinstance(runtime, dict):
        prefix_text = str(runtime.get("prefix") or "").strip()
    else:
        prefix_text = str(runtime or "").strip()
    if not prefix_text:
        return None
    try:
        prefix = pathlib.Path(prefix_text).expanduser()
    except (OSError, ValueError):
        return None

    windows = ("python.exe", os.path.join("Scripts", "python.exe"))
    posix = (os.path.join("bin", "python"), os.path.join("bin", "python3"))
    candidates = windows + posix if os.name == "nt" else posix + windows

    for relative in candidates:
        candidate = prefix / relative
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def conda_binary(environ: typing.Optional[typing.Mapping[str, str]] = None) -> typing.Optional[str]:
    """A conda executable, preferring the one that owns CONDA_EXE."""
    env = _environ(environ)
    exe = str(env.get("CONDA_EXE") or "").strip()
    if exe and pathlib.Path(exe).is_file():
        return exe
    for name in ("conda", "conda.bat", "conda.exe", "mamba", "micromamba"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _conda_roots(environ: typing.Mapping[str, str]) -> typing.List[pathlib.Path]:
    """Where a conda installation is likely to be, on either platform."""
    roots: typing.List[pathlib.Path] = []

    exe = str(environ.get("CONDA_EXE") or "").strip()
    if exe:
        # <root>/bin/conda on POSIX, <root>/Scripts/conda.exe on Windows.
        roots.append(pathlib.Path(exe).expanduser().parent.parent)

    for key in ("CONDA_ROOT", "CONDA_PREFIX_1", "MAMBA_ROOT_PREFIX"):
        value = str(environ.get(key) or "").strip()
        if value:
            roots.append(pathlib.Path(value).expanduser())

    home_text = str(environ.get("USERPROFILE") or environ.get("HOME") or "").strip()
    homes = [pathlib.Path(home_text).expanduser()] if home_text else []
    try:
        homes.append(pathlib.Path.home())
    except (OSError, RuntimeError):
        pass

    names = ("anaconda3", "miniconda3", "miniforge3", "mambaforge", "micromamba", "conda")
    for home in homes:
        roots.extend(home / name for name in names)
    roots.extend(pathlib.Path(base) / name for base in ("/opt", "/usr/local") for name in names)

    program_data = str(environ.get("ProgramData") or "").strip()
    if program_data:
        roots.extend(pathlib.Path(program_data) / name for name in ("Anaconda3", "Miniconda3", "Miniforge3"))

    return roots


def _is_conda_prefix(prefix: pathlib.Path) -> bool:
    try:
        return (prefix / "conda-meta").is_dir()
    except OSError:
        return False


def _conda_display_name(prefix: pathlib.Path) -> str:
    """What to call this environment in a dropdown.

    Environment names are only unique inside one installation, so a base
    environment is qualified with the folder it lives in - two "base" rows
    that cannot be told apart are worse than a long label.
    """
    if prefix.parent.name == "envs":
        return prefix.name
    return f"base ({prefix.name})"


def _conda_from_command(
    runner: typing.Callable[..., typing.Any],
    environ: typing.Mapping[str, str],
) -> typing.List[pathlib.Path]:
    binary = conda_binary(environ)
    if not binary:
        return []
    try:
        completed = runner([binary, "env", "list", "--json"], capture_output=True, text=True, timeout=60)
    except Exception:
        return []
    if getattr(completed, "returncode", 1) != 0:
        return []
    try:
        payload = json.loads(getattr(completed, "stdout", "") or "")
        entries = payload.get("envs") if isinstance(payload, dict) else None
    except (ValueError, AttributeError):
        return []
    if not isinstance(entries, list):
        return []
    found: typing.List[pathlib.Path] = []
    for entry in entries:
        if isinstance(entry, str) and entry.strip():
            found.append(pathlib.Path(entry.strip()).expanduser())
    return found


def _environments_txt(environ: typing.Mapping[str, str]) -> typing.List[pathlib.Path]:
    """``~/.conda/environments.txt`` - conda's own list of what it has seen."""
    home_text = str(environ.get("USERPROFILE") or environ.get("HOME") or "").strip()
    homes = [pathlib.Path(home_text).expanduser()] if home_text else []
    try:
        homes.append(pathlib.Path.home())
    except (OSError, RuntimeError):
        pass

    found: typing.List[pathlib.Path] = []
    for home in homes:
        listing = home / ".conda" / "environments.txt"
        try:
            if not listing.is_file():
                continue
            lines = listing.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if line and not line.startswith("#"):
                found.append(pathlib.Path(line).expanduser())
    return found


def find_conda_environments(
    runner: typing.Callable[..., typing.Any] = subprocess.run,
    environ: typing.Optional[typing.Mapping[str, str]] = None,
) -> typing.List[dict]:
    """Every conda environment we can find, best effort, never raising.

    Discovery is a convenience: the user can always type a prefix, so an
    empty list here is a smaller failure than an exception out of the setup
    screen. Candidates that have no interpreter are dropped, because the very
    next thing setup does is try to run one.
    """
    env = _environ(environ)
    candidates: typing.List[pathlib.Path] = []

    try:
        active = str(env.get("CONDA_PREFIX") or "").strip()
        if active:
            candidates.append(pathlib.Path(active).expanduser())

        for root in _conda_roots(env):
            candidates.append(root)
            try:
                envs = root / "envs"
                if envs.is_dir():
                    candidates.extend(child for child in sorted(envs.iterdir()) if child.is_dir())
            except OSError:
                continue

        candidates.extend(_environments_txt(env))
        candidates.extend(_conda_from_command(runner, env))
    except Exception:  # pragma: no cover - discovery must never be fatal
        return []

    found: typing.List[dict] = []
    seen: typing.Set[str] = set()
    for prefix in candidates:
        try:
            if not prefix.is_dir():
                continue
            key = str(prefix.resolve()).lower() if os.name == "nt" else str(prefix.resolve())
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        if not _is_conda_prefix(prefix) or interpreter_for({"prefix": str(prefix)}) is None:
            continue
        found.append({"type": CONDA, "prefix": str(prefix), "display_name": _conda_display_name(prefix)})

    found.sort(key=lambda entry: (entry["display_name"].lower(), entry["prefix"]))
    return found


def runtime_for_prefix(prefix: typing.Any) -> typing.Optional[dict]:
    """Classify a directory the user named, or None if no Python is in it.

    This is the plain-virtualenv door: WanGP is often installed into a venv
    rather than a conda environment, and a prefix with ``bin/python`` in it
    is exactly as launchable as one with ``conda-meta`` beside it.
    """
    text = str(prefix or "").strip()
    if not text:
        return None
    try:
        path = pathlib.Path(text).expanduser()
    except (OSError, ValueError):
        return None
    if interpreter_for({"prefix": str(path)}) is None:
        return None
    if _is_conda_prefix(path):
        return {"type": CONDA, "prefix": str(path), "display_name": _conda_display_name(path)}
    return {"type": VENV, "prefix": str(path), "display_name": path.name or str(path)}


def find_runtimes(
    extra_prefixes: typing.Iterable[typing.Any] = (),
    runner: typing.Callable[..., typing.Any] = subprocess.run,
    environ: typing.Optional[typing.Mapping[str, str]] = None,
) -> typing.List[dict]:
    """The runtime choices to offer: discovered conda envs plus named prefixes.

    ``extra_prefixes`` is whatever the user typed or the wizard remembers -
    a WanGP checkout's own ``venv``, for instance. Nothing is invented; a
    prefix with no interpreter simply does not appear.
    """
    runtimes: typing.List[dict] = []
    seen: typing.Set[str] = set()

    def _add(entry: typing.Optional[dict]) -> None:
        if not entry:
            return
        key = entry["prefix"].lower() if os.name == "nt" else entry["prefix"]
        if key not in seen:
            seen.add(key)
            runtimes.append(entry)

    for entry in find_conda_environments(runner=runner, environ=environ):
        _add(entry)
    for prefix in extra_prefixes or ():
        try:
            _add(runtime_for_prefix(prefix))
        except Exception:  # pragma: no cover - a hostile path is just skipped
            continue
    return runtimes


def probe_command(
    runtime: typing.Any,
    root: typing.Any,
    strategy: str,
    environ: typing.Optional[typing.Mapping[str, str]] = None,
) -> typing.Optional[typing.List[str]]:
    """The exact argv for one probe strategy, or None if it is unavailable."""
    root_text = str(pathlib.Path(str(root or "")).expanduser())
    if strategy == DIRECT_PYTHON:
        interpreter = interpreter_for(runtime)
        if interpreter is None:
            return None
        return [str(interpreter), "-c", _PROBE_SOURCE, root_text]
    if strategy == CONDA_RUN:
        prefix = str(runtime.get("prefix") or "").strip() if isinstance(runtime, dict) else str(runtime or "").strip()
        binary = conda_binary(environ)
        if not prefix or not binary:
            return None
        return [binary, "run", "--no-capture-output", "-p", prefix, "python", "-c", _PROBE_SOURCE, root_text]
    return None


def _probe_environment(environ: typing.Optional[typing.Mapping[str, str]]) -> typing.Dict[str, str]:
    """A child environment that cannot be poisoned by Forge's own Python.

    PYTHONHOME and PYTHONPATH from this process would point the candidate
    interpreter at Forge's libraries and turn a real incompatibility into a
    passing probe, or the reverse.
    """
    child = dict(_environ(environ))
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONEXECUTABLE"):
        child.pop(key, None)
    child["PYTHONNOUSERSITE"] = "1"
    child["PYTHONIOENCODING"] = "utf-8"
    return child


def _read_probe(completed: typing.Any) -> typing.Tuple[bool, str]:
    """Turn one finished probe into (ok, detail)."""
    if getattr(completed, "returncode", 1) != 0:
        stderr = _detail(getattr(completed, "stderr", "") or getattr(completed, "stdout", ""))
        return False, stderr or "the probe exited with an error"

    stdout = getattr(completed, "stdout", "") or ""
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith(_PROBE_MARKER):
            continue
        try:
            info = json.loads(line[len(_PROBE_MARKER) :].strip())
        except ValueError:
            break
        if not isinstance(info, dict):
            break
        imports = info.get("imports") or []
        version = str(info.get("python") or "?")
        if "gradio" not in imports:
            return False, _detail(f"Python {version}: {ROOT_MARKER} parsed but does not import gradio")
        return True, _detail(f"Python {version}, {ROOT_MARKER} parses and imports {', '.join(imports)}")
    return False, "the probe printed nothing recognisable"


def probe_runtime(
    runtime: typing.Any,
    root: typing.Any,
    runner: typing.Callable[..., typing.Any] = subprocess.run,
    environ: typing.Optional[typing.Mapping[str, str]] = None,
) -> typing.Tuple[bool, str, str]:
    """Decide how this environment can start WanGP, by trying it.

    Direct interpreter first, because it needs no shell activation state and
    is what the launcher would rather use; ``conda run -p <prefix>`` only if
    the direct attempt failed and this is a conda environment, since some
    installs really do depend on activation scripts. The strategy returned is
    the one that worked, and it is the strategy that gets persisted.

    The probe never imports WanGP. It parses wgp.py inside the candidate
    interpreter, which is enough to prove the file is there, is valid for
    that Python and is the gradio application - and stops short of dragging
    torch and a CUDA context into a process we are about to throw away.
    """
    strategies = [DIRECT_PYTHON]
    kind = runtime.get("type") if isinstance(runtime, dict) else ""
    if kind == CONDA or conda_binary(environ):
        strategies.append(CONDA_RUN)

    problems: typing.List[str] = []
    child_env = _probe_environment(environ)
    root_text = str(pathlib.Path(str(root or "")).expanduser())

    for strategy in strategies:
        command = probe_command(runtime, root, strategy, environ)
        if command is None:
            problems.append(f"{strategy}: unavailable")
            continue
        timeout = PROBE_TIMEOUT_CONDA if strategy == CONDA_RUN else PROBE_TIMEOUT_DIRECT
        try:
            completed = runner(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=root_text if os.path.isdir(root_text) else None,
                env=child_env,
            )
        except Exception as error:
            problems.append(f"{strategy}: {_detail(error)}")
            continue
        ok, detail = _read_probe(completed)
        if ok:
            return True, strategy, detail
        problems.append(f"{strategy}: {detail}")

    return False, "", _detail("; ".join(problems) or "no launch strategy could be tried")
