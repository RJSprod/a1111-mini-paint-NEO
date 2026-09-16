"""Run every check in this folder.

    python tests/run.py

Nothing here needs the WebUI running. The image maths needs only Pillow; the
frontend suite needs Gradio, and says so rather than failing if it is not
installed in the interpreter being used. The browser smoke test is separate:
see tests/browser_smoke.py.
"""

from __future__ import annotations

import os
import pathlib
import sys

os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

from harness import setup_path

setup_path()

SUITES = [
    "test_imaging",
    # The picture the canvas draws, as bytes behind an opaque id: the store,
    # the fixed grammar, the route in front of it and the lifetime rules.
    "test_canvas_display",
    "test_frontends",
    "test_workflow",
    # What every writer in the extension is allowed to put on a screen or in a
    # file. Ahead of the WanGP suites because it is the rule they all obey.
    "test_logging_privacy",
    # The event spine the integrations below are moving onto: the cursor, the
    # bounded replay, the targeted advisory event, and the presence grace that
    # keeps a remote browser's turn across a dropped connection.
    "test_events",
    # The WanGP integration, in the order it is built: what survives a restart,
    # the files on their way out, the vocabulary all three sides share, the
    # child process, the route in front of it, and finally the gate that
    # decides whether an image may be sent at all.
    "test_wangp_config",
    "test_wangp_handoff",
    "test_wangp_protocol",
    "test_wangp_runtime",
    "test_wangp_proxy",
    "test_wangp_receiver_contract",
    # Protocol 3: the queue operation inside the plugin - overlay, admit,
    # confirm, restore - then the public API that carries it and the
    # Clipboard tab that is its first client.
    "test_wangp_queue",
    "test_wangp_start",
    # Protocol 6: the control plane between Forge and the WanGP child, and
    # the one worker both submission paths funnel through - which is where
    # "at most one generation, whoever started it" is actually established.
    "test_wangp_control",
    "test_interop",
    # The browser half of walking away: a page that is told rather than
    # asking, and that can be closed without the job noticing.
    "test_interop_browser",
    "test_clipboard_store",
    "test_clipboard_outbox",
    "test_clipboard_enhance",
    # The server executor: press, walk away, come back to a generated file.
    # Ahead of the tab, because the tab is one of its screens.
    "test_clipboard_executor",
    # What WanGP made, kept where a restart can find it. After the executor,
    # because the exact half of it is the executor's own record.
    "test_clipboard_outputs",
    "test_clipboard_ui",
    "test_queue_e2e",
    # The Canvas startup contract, in a real browser, over the real asset
    # route. Last because it is the slowest, and listed HERE rather than left
    # to be run by hand because the regression it exists to catch lived
    # entirely in the gap between "the unit tests pass" and "the page works".
    "browser_loading",
    # The Clipboard tab in a browser: sending a picture out, and the
    # thumbnail grid. Both failed in the page while its 181 unit checks
    # passed, because both failures live where the graph cannot see.
    "browser_clipboard",
]

#: Suites that drive a browser. They need Playwright and a Chromium, and they
#: are the only ones that do; a checkout without them still runs everything
#: else rather than reporting a failure it cannot act on.
BROWSER = {"browser_loading", "browser_clipboard"}


#: Third-party names a suite is allowed to be missing. Gradio is the reason
#: this list exists - the image maths runs on a bare interpreter and should
#: not need it - and everything else here is the same kind of optional host
#: dependency.
OPTIONAL = ("gradio", "PIL", "httpx", "fastapi", "starlette", "numpy", "playwright")


def _is_optional(error: ImportError) -> bool:
    """Whether an import failure is a missing dependency or a broken suite.

    The difference decides the exit code, so it cannot be a guess. A suite that
    cannot find Gradio is skipped, as it always was; a suite that cannot import
    one of *our* modules is a suite that would otherwise vanish from the run
    while the run still reported success - which is exactly how a broken module
    stays broken.
    """
    missing = (getattr(error, "name", "") or "").split(".")[0]
    return bool(missing) and missing in OPTIONAL


def _run_isolated(name: str) -> str:
    """Run a browser suite in its own interpreter.

    They build a whole Forge-shaped page, and building one is not a pure
    function of this process: Gradio keeps a render context, the host keeps
    the components it captured, the WebUI stubs keep their callback lists,
    and twenty-two suites have already had their way with all three. Run in
    the shared interpreter after them, the page comes out unbuildable - and
    the suite fails for a reason that has nothing to do with what it tests.

    A subprocess is also the honest boundary in the other direction: a suite
    that drives a browser cannot leave anything behind for the next one.

    Returns "ok", "failed" or "skipped".
    """
    import subprocess

    started = subprocess.run([sys.executable, str(pathlib.Path(__file__).parent / f"{name}.py")],
                             capture_output=True, text=True)
    output = (started.stdout or "") + (started.stderr or "")
    if "No module named 'playwright'" in output or "no chromium" in output.lower():
        print(f"{name}: skipped (needs Playwright with a Chromium)")
        return "skipped"
    for line in output.splitlines():
        if line.startswith("  FAIL") or ": " in line and ("passed" in line or "failed" in line):
            print(line)
    if started.returncode != 0:
        tail = [x for x in output.splitlines() if x.strip()][-4:]
        print(f"{name}: FAILED (exit {started.returncode})")
        for line in tail:
            print("   " + line[:200])
        return "failed"
    return "ok"


def main() -> int:
    ok = True
    skipped = []
    for name in SUITES:
        if name in BROWSER:
            outcome = _run_isolated(name)
            if outcome == "skipped":
                skipped.append(name)
            elif outcome == "failed":
                ok = False
            continue
        try:
            module = __import__(name)
        except ImportError as error:
            if _is_optional(error):
                print(f"{name}: skipped ({error})")
                skipped.append(name)
                continue
            print(f"{name}: FAILED TO IMPORT ({error})")
            ok = False
            continue
        try:
            report = module.run()
        except ImportError as error:
            # A suite that imports an optional dependency *inside* a check
            # rather than at the top of the file. The skip above cannot see
            # one of those, and before this branch existed the exception
            # escaped the whole loop: one suite reaching for Gradio at line
            # 1606 aborted the run and silently took eleven later suites with
            # it, which is worse than either skipping or failing, because the
            # output still looked like a finished run.
            if _is_optional(error):
                print(f"{name}: skipped ({error})")
                skipped.append(name)
                continue
            print(f"{name}: FAILED ({error})")
            ok = False
            continue
        ok = report.report() and ok

    # A run that skipped most of itself and still said nothing is how a green
    # suite comes to mean very little. Say how much of it actually ran, and
    # let a caller that cares - CI - insist that all of it did.
    if skipped:
        print(f"\n{len(skipped)} of {len(SUITES)} suites skipped: {', '.join(skipped)}")
        print("   install tests/requirements.txt to run them.")
        if os.environ.get("MINIPAINT_REQUIRE_ALL_SUITES"):
            missing = [name for name in skipped if name not in BROWSER or _browser_expected()]
            if missing:
                print(f"FAILED: MINIPAINT_REQUIRE_ALL_SUITES is set and these did not run: {', '.join(missing)}")
                ok = False
    else:
        print(f"\nall {len(SUITES)} suites ran.")
    return 0 if ok else 1


def _browser_expected() -> bool:
    """Whether a skipped browser suite should count as a failure.

    It should when something has gone to the trouble of installing a browser,
    which is the case in CI and generally not the case on a laptop.
    """
    import importlib.util

    return importlib.util.find_spec("playwright") is not None


if __name__ == "__main__":
    sys.exit(main())
