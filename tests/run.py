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

SUITES = ["test_imaging", "test_frontends", "test_workflow"]


def main() -> int:
    ok = True
    for name in SUITES:
        try:
            module = __import__(name)
        except ImportError as error:
            print(f"{name}: skipped ({error})")
            continue
        ok = module.run().report() and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
