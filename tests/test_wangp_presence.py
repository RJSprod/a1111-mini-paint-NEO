"""What another extension may know about the managed WanGP: ``presence.report()``.

The contract is small and the whole of it is here: every key, the three
values ``generating`` can take and what each one means, the setup re-read only
when its file changed, and nothing in the report that would let a reader
reach the process. SD-Neo-ModelSwitchRefiner reads this dict to keep its
llama-server out of WanGP's VRAM and off WanGP's cores; a key that goes
missing or a pid that creeps in breaks that extension, not this one, which is
why the shape is held closed by a test rather than by a comment.
"""

from harness import Results, setup_path

setup_path()

import json  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402

from minipaint_neo.wangp import config as wangp_config  # noqa: E402
from minipaint_neo.wangp import control, presence, runtime  # noqa: E402

GPU_UUID = "GPU-11112222-3333-4444-5555-666677778888"
OTHER_UUID = "GPU-99998888-7777-6666-5555-444433332222"


def write_config(uuid=GPU_UUID, initialized=True, text=None):
    document = text if text is not None else json.dumps({
        "schema_version": wangp_config.SCHEMA_VERSION, "initialized": initialized,
        "wangp_root": "/somewhere/WanGP",
        "runtime": {"type": "venv", "prefix": "/somewhere/env", "display_name": "wan",
                    "launch_strategy": "direct_python"},
        "gpu": {"uuid": uuid},
        "integration": {"proxy_path": "/wan2gp", "auto_start": "lazy", "auth_checked": False},
    })
    wangp_config.atomic_write(wangp_config.config_path(), document)


def remember_hello(hello, age=0.0):
    with control._lock:
        control._last["at"] = time.time() - age
        control._last["hello"] = hello


def shape_checks(r: Results) -> None:
    found = presence.report()
    r.check("a report carries exactly the documented keys",
            tuple(found) == presence.KEYS, str(tuple(found)))
    r.check("the version is the module's", found["version"] == presence.PRESENCE_VERSION)
    r.check("a report says the integration is here", found["available"] is True)
    for forbidden in ("port", "pid", "secret", "root", "path", "prefix"):
        r.check(f"no key names a {forbidden}", not any(forbidden in key for key in found), str(tuple(found)))
    r.check("nothing set up reads as not configured and no card",
            found["configured"] is False and found["gpu_uuid"] == "")
    r.check("no child reads as not running, with no state and no instance",
            found["running"] is False and found["state"] == runtime.STOPPED and found["instance_id"] == "")
    r.check("nobody having said reads as generating None", found["generating"] is None)
    r.check("the source names itself", found["source"] == "presence")


def setup_checks(r: Results) -> None:
    write_config()
    presence.forget()
    found = presence.report()
    r.check("a saved setup gives the card's UUID and nothing else about it",
            found["configured"] is True and found["gpu_uuid"] == GPU_UUID, str(found))

    write_config(uuid=OTHER_UUID)
    # The file changed; the report has to see it without being told, which is
    # the case a setup that just finished is. Bumped rather than waited for,
    # because a modification time is what the re-read is keyed on.
    later = time.time() + 30
    os.utime(wangp_config.config_path(), (later, later))
    r.check("a changed file is re-read", presence.report()["gpu_uuid"] == OTHER_UUID)

    reads = []
    original = wangp_config.load

    def counted():
        reads.append(1)
        return original()

    wangp_config.load = counted
    try:
        presence.report()
        presence.report()
        presence.report()
        r.check("an unchanged file is not parsed again", reads == [], str(reads))
    finally:
        wangp_config.load = original

    write_config(initialized=False)
    presence.forget()
    r.check("a setup that never finished is not configured, and still names no card as its own",
            presence.report()["configured"] is False)

    write_config(text="{not json")
    presence.forget()
    found = presence.report()
    r.check("an unreadable setup is not configured rather than an error",
            found["configured"] is False and found["gpu_uuid"] == "" and found["available"] is True)

    wangp_config.config_path().unlink()
    presence.forget()
    r.check("a removed setup is not configured", presence.report()["configured"] is False)


def runtime_checks(r: Results) -> None:
    current = runtime.current()
    try:
        current.state = runtime.READY
        current.instance_id = "abc123"
        found = presence.report()
        r.check("a READY child reads as running, with its state and instance",
                found["running"] is True and found["state"] == runtime.READY and found["instance_id"] == "abc123",
                str(found))
        current.state = runtime.STARTING
        found = presence.report()
        r.check("a starting child is not running yet, and says which state it is in",
                found["running"] is False and found["state"] == runtime.STARTING)
        current.state = runtime.CRASHED
        r.check("a crashed child is not running", presence.report()["running"] is False)
    finally:
        runtime.reset_for_tests()


def generating_checks(r: Results) -> None:
    control.reset_for_tests()
    try:
        remember_hello({"generation_running": True})
        r.check("the bridge's recent word that it is generating is passed on",
                presence.report()["generating"] is True)
        remember_hello({"generation_running": False})
        r.check("and that it is idle", presence.report()["generating"] is False)
        remember_hello({"generation_running": None})
        r.check("a bridge that could not read the flag is None, not False",
                presence.report()["generating"] is None)
        remember_hello({"generation_running": "yes"})
        r.check("anything but a bool is None", presence.report()["generating"] is None)
        remember_hello({"generation_running": True}, age=control.HELLO_TTL + 5)
        r.check("a stale hello is None: an old True is not a current one",
                presence.report()["generating"] is None)
    finally:
        control.reset_for_tests()


def containment_checks(r: Results) -> None:
    """Each part is read on its own; a part that fails leaves the others truthful."""
    original = runtime.snapshot

    def broken():
        raise RuntimeError("the runtime is wedged")

    runtime.snapshot = broken
    try:
        write_config()
        presence.forget()
        found = presence.report()
        r.check("a runtime that cannot be read still leaves the setup in the report",
                found["configured"] is True and found["gpu_uuid"] == GPU_UUID and found["running"] is False)
    finally:
        runtime.snapshot = original
        with open(wangp_config.config_path(), "w", encoding="utf-8"):
            pass
        wangp_config.config_path().unlink()
        presence.forget()


def run() -> Results:
    r = Results("wangp presence")
    base = tempfile.TemporaryDirectory()
    wangp_config.use_config_dir(base.name)
    presence.forget()
    control.reset_for_tests()
    try:
        shape_checks(r)
        setup_checks(r)
        runtime_checks(r)
        generating_checks(r)
        containment_checks(r)
    finally:
        wangp_config.use_config_dir(None)
        presence.forget()
        base.cleanup()
    return r


if __name__ == "__main__":
    raise SystemExit(0 if run().report() else 1)
