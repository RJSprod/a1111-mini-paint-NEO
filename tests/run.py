"""Run every check in this folder.

    python tests/run.py

Nothing here needs the WebUI running. The image maths needs only Pillow; the
frontend suite needs Gradio, and says so rather than failing if it is not
installed in the interpreter being used. The browser smoke test is separate:
see tests/browser_smoke.py.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

from harness import setup_path

setup_path()

SUITES = [
    "test_imaging",
    "test_frontends",
    "test_workflow",
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
]


#: Third-party names a suite is allowed to be missing. Gradio is the reason
#: this list exists - the image maths runs on a bare interpreter and should
#: not need it - and everything else here is the same kind of optional host
#: dependency.
OPTIONAL = ("gradio", "PIL", "httpx", "fastapi", "starlette", "numpy")


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


def main() -> int:
    ok = True
    for name in SUITES:
        try:
            module = __import__(name)
        except ImportError as error:
            if _is_optional(error):
                print(f"{name}: skipped ({error})")
                continue
            print(f"{name}: FAILED TO IMPORT ({error})")
            ok = False
            continue
        ok = module.run().report() and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
