"""What another extension in this process may know about the managed WanGP.

Written for SD-Neo-ModelSwitchRefiner, which runs a local llama-server and
has to keep it out of WanGP's way: off the card WanGP is rendering on when
WanGP needs the room, and off the processor's cores while WanGP is feeding
that card. Everything it needs to decide that is known here - which card the
setup named, whether the child is up, and whether WanGP's own bridge has said
it is generating - and nothing it needs is a reason to reach into this
package's internals, so this is the one function it reads instead.

One call, one plain dict, and nothing in it that would let a caller reach the
process: no port, no pid, no secret, no path. The card is its UUID, which is
the identity both extensions already record and the one that survives every
renumbering; the caller resolves it against its own picture of the machine.

``generating`` is three-valued on purpose. True and False are what the bridge
last said, through the control plane's cached hello, and only while that
answer is recent; None is "nobody has said", which is what a WanGP without the
bridge, or one idle long enough that nothing has asked it, honestly is. A
caller that reads None as False will be wrong on the machine this was written
for, so the contract says which it is.

Cheap enough to ask every couple of seconds: the runtime snapshot is memory,
the hello is a cached dict, and the config is re-read only when its file has
changed.
"""

from __future__ import annotations

import os
import typing

#: Bumped when a key is added or its meaning changes. A caller that knows a
#: newer version than it finds reads the keys it has and ignores the rest.
PRESENCE_VERSION = 1

KEYS = ("version", "available", "configured", "gpu_uuid", "state", "running", "instance_id",
        "generating", "source")
"""Every key a report carries, and the only ones. The privacy check holds this closed."""

_saved: typing.Dict[str, typing.Any] = {"key": None, "configured": False, "uuid": ""}


def forget() -> None:
    """Drop the cached setup, so the next report reads the file. For tests."""
    _saved["key"], _saved["configured"], _saved["uuid"] = None, False, ""


def _setup() -> typing.Tuple[bool, str]:
    """``(configured, gpu_uuid)`` from the active config, re-read when its file changed.

    Keyed on the primary file's size and modification time rather than on a
    clock: a setup that just finished writes that file, and a report made a
    moment later has to see it, while a report every two seconds must not cost
    a JSON parse each time.
    """
    from . import config

    try:
        stat = os.stat(config.config_path())
        key: typing.Any = (stat.st_mtime_ns, stat.st_size)
    except FileNotFoundError:
        key = "absent"
    except Exception:
        key = None
    if key is not None and key == _saved["key"]:
        return bool(_saved["configured"]), str(_saved["uuid"])
    configured, uuid = False, ""
    try:
        saved = config.load()
        if saved is not None:
            configured = bool(getattr(saved, "initialized", False))
            gpu = getattr(saved, "gpu", None)
            uuid = str((gpu or {}).get("uuid") or "") if isinstance(gpu, dict) else ""
    except Exception:
        # Unreadable, too new, or a directory that cannot be listed: not set
        # up, as far as a caller deciding about VRAM is concerned.
        configured, uuid = False, ""
    _saved["key"], _saved["configured"], _saved["uuid"] = key, configured, uuid
    return configured, uuid


def report() -> dict:
    """WanGP as this extension knows it right now. Never raises.

    ``configured`` and ``gpu_uuid`` come from the saved setup; ``state``,
    ``running`` and ``instance_id`` from the runtime's own snapshot, which is
    what the tab draws; ``generating`` from the bridge's last hello, when it
    is recent. Each part is read on its own, so a part that cannot be read
    leaves the others truthful rather than the whole answer missing.
    """
    found: typing.Dict[str, typing.Any] = {
        "version": PRESENCE_VERSION, "available": True, "configured": False, "gpu_uuid": "",
        "state": "", "running": False, "instance_id": "", "generating": None,
        "source": "presence",
    }
    try:
        found["configured"], found["gpu_uuid"] = _setup()
    except Exception:
        pass
    try:
        from . import runtime

        snapshot = runtime.snapshot()
        found["state"] = str(snapshot.get("state") or "")
        found["running"] = bool(snapshot.get("running"))
        found["instance_id"] = str(snapshot.get("instance_id") or "")
    except Exception:
        pass
    try:
        from . import control

        hello = control.last_hello()
        flag = hello.get("generation_running") if isinstance(hello, dict) else None
        found["generating"] = flag if isinstance(flag, bool) else None
    except Exception:
        found["generating"] = None
    return found


__all__ = ["KEYS", "PRESENCE_VERSION", "forget", "report"]
