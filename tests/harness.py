"""A check() and a path, so the tests stay readable and have no dependencies."""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
STUBS = ROOT / "tests" / "stubs"


def setup_path() -> None:
    """Put the extension, and the WebUI stubs, where an import can find them."""
    for entry in (str(STUBS), str(ROOT)):
        if entry not in sys.path:
            sys.path.insert(0, entry)


class Results:
    def __init__(self, title: str) -> None:
        self.title = title
        self.failures: list = []
        self.passed = 0

    def check(self, name: str, condition, detail: str = "") -> bool:
        ok = bool(condition)
        if ok:
            self.passed += 1
        else:
            self.failures.append(name)
            print(f"  FAIL {name}" + (f"  [{detail}]" if detail else ""))
        return ok

    def report(self) -> bool:
        total = self.passed + len(self.failures)
        if self.failures:
            print(f"{self.title}: {len(self.failures)} of {total} failed")
            return False
        print(f"{self.title}: {total} passed")
        return True


def pixels(image):
    """Every pixel of an image, on whichever Pillow the host ships."""
    flattened = getattr(image, "get_flattened_data", None)
    return list(flattened() if flattened else image.getdata())


def value_of(update):
    """Gradio hands back either a value or an update dict; tests want the value."""
    if isinstance(update, dict):
        return update.get("value", "")
    return update
