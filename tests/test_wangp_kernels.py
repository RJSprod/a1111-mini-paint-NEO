"""WanGP's GGUF kernels, older than WanGP asks for, said before a run does.

On 2026-10-02 a WanGP updated the ordinary way (a pull, then
requirements.txt) kept llamacpp_gguf_cuda 1.0.11 while its code asked for
1.0.13 or newer, and every GGUF step of LTX 2.3 took about 100 seconds on an
RTX 5090 that runs the same model in int8 at 3. The only sign was one line
from WanGP's prompt enhancer. ``kernels`` reads the installed version against
WanGP's install guide at every launch, and recognises WanGP's own two
sentences in its output; these checks hold both, the journal line, the
interop snapshot's ``wangp_warnings`` and the runtime's reader calling it.
"""

from harness import Results, setup_path

setup_path()

import io  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402

from minipaint_neo.wangp import journal, kernels, runtime  # noqa: E402

GUIDE = """# Installation

GGUF CUDA kernels (Windows, Python 3.11, torch 2.10, CUDA 13):

    pip install --no-deps https://github.com/deepbeepmeep/kernels/releases/download/gguf-v1.0.25/llamacpp_gguf_cuda-1.0.25%2Btorch210cu130py311-cp311-cp311-win_amd64.whl

Older builds: llamacpp_gguf_cuda-1.0.21+torch210cu130py311
"""

#: WanGP's own sentence, as it reached the log on 2026-10-01.
TOO_OLD_LINE = ("[Prompt Enhancer / Deepy] Fast KV Cache quantization disabled automatically because "
                "GGUF kernels 1.0.11+torch210cu130py311 do not meet the 1.0.13+ requirement.")
UNAVAILABLE_LINE = "[GGUF][llama.cpp CUDA] kernels unavailable, using fallback"


def make_install(root, installed=None, layout="windows", guide=GUIDE):
    wangp = os.path.join(root, "WanGP")
    prefix = os.path.join(root, "env")
    os.makedirs(os.path.join(wangp, "docs"), exist_ok=True)
    if guide is not None:
        with open(os.path.join(wangp, "docs", "INSTALLATION.md"), "w", encoding="utf-8") as handle:
            handle.write(guide)
    site = (os.path.join(prefix, "Lib", "site-packages") if layout == "windows"
            else os.path.join(prefix, "lib", "python3.11", "site-packages"))
    os.makedirs(site, exist_ok=True)
    if installed:
        os.makedirs(os.path.join(site, f"llamacpp_gguf_cuda-{installed}.dist-info"))
    return wangp, prefix


def reading_checks(r: Results) -> None:
    r.check("a version with a local part reads as its numbers",
            kernels.version_tuple("1.0.11+torch210cu130py311") == (1, 0, 11))
    r.check("an unreadable version is empty", kernels.version_tuple("abc") == ())
    r.check("versions compare as numbers, not text",
            kernels.version_tuple("1.0.9") < kernels.version_tuple("1.0.13"))
    with tempfile.TemporaryDirectory() as room:
        wangp, prefix = make_install(room, "1.0.11+torch210cu130py311")
        r.check("the installed version is read from the wheel's dist-info folder",
                kernels.installed_version(prefix) == "1.0.11", kernels.installed_version(prefix))
        r.check("the guide's newest wheel is the version wanted, URL-encoded or not",
                kernels.guide_version(wangp) == "1.0.25", kernels.guide_version(wangp))
    with tempfile.TemporaryDirectory() as room:
        _wangp, prefix = make_install(room, "1.0.25", layout="posix")
        r.check("a POSIX environment's site-packages is read too",
                kernels.installed_version(prefix) == "1.0.25", kernels.installed_version(prefix))
    with tempfile.TemporaryDirectory() as room:
        wangp, prefix = make_install(room, None, guide=None)
        r.check("no wheel installed reads as nothing", kernels.installed_version(prefix) == "")
        r.check("no guide reads as nothing", kernels.guide_version(wangp) == "")
    r.check("no prefix reads as nothing", kernels.installed_version("") == "")


def launch_checks(r: Results) -> None:
    journal.clear()
    with tempfile.TemporaryDirectory() as room:
        wangp, prefix = make_install(room, "1.0.11+torch210cu130py311")
        kernels.forget()
        kernels.begin_run(wangp, prefix)
        found = kernels.warnings()
        r.check("kernels older than the guide's are one warning at launch", len(found) == 1, str(found))
        r.check("it says both versions",
                found and found[0]["installed"] == "1.0.11" and found[0]["wanted"] == "1.0.25", str(found))
        r.check("with the code the page knows", found and found[0]["code"] == kernels.TOO_OLD)
        r.check("and says what to do, naming requirements.txt as not the fix",
                found and "requirements.txt" in found[0]["fix"])
        said = [line for line in journal.lines() if "] kernels " in line]
        r.check("and the journal says so once", len(said) == 1, str(said))
        kernels.begin_run(wangp, prefix)
        r.check("a new run starts from what is true now, not on top of the last",
                len(kernels.warnings()) == 1)
    with tempfile.TemporaryDirectory() as room:
        wangp, prefix = make_install(room, "1.0.25+torch210cu130py311")
        kernels.begin_run(wangp, prefix)
        r.check("kernels as new as the guide's are no warning", kernels.warnings() == [])
    with tempfile.TemporaryDirectory() as room:
        wangp, prefix = make_install(room, "1.0.9", guide="llamacpp_gguf_cuda-1.0.13+torch210cu130py311")
        kernels.begin_run(wangp, prefix)
        r.check("1.0.9 is older than 1.0.13, which text order would get wrong",
                len(kernels.warnings()) == 1, str(kernels.warnings()))
    with tempfile.TemporaryDirectory() as room:
        wangp, prefix = make_install(room, None)
        kernels.begin_run(wangp, prefix)
        r.check("kernels that cannot be found are no warning from the files alone",
                kernels.warnings() == [])
    kernels.forget()


def output_checks(r: Results) -> None:
    kernels.forget()
    kernels.observe(TOO_OLD_LINE)
    found = kernels.warnings()
    r.check("WanGP saying its kernels are too old is a warning",
            len(found) == 1 and found[0]["installed"] == "1.0.11" and found[0]["wanted"] == "1.0.13", str(found))
    with tempfile.TemporaryDirectory() as room:
        wangp, prefix = make_install(room, "1.0.11+torch210cu130py311")
        kernels.begin_run(wangp, prefix)
        kernels.observe(TOO_OLD_LINE)
        found = kernels.warnings()
        r.check("WanGP's floor does not lower the guide's version",
                len(found) == 1 and found[0]["wanted"] == "1.0.25", str(found))
    kernels.forget()
    journal.clear()
    kernels.observe(UNAVAILABLE_LINE)
    kernels.observe(UNAVAILABLE_LINE)
    said = [line for line in journal.lines() if "] kernels " in line]
    r.check("WanGP saying it again is not said again", len(said) == 1, str(said))
    found = kernels.warnings()
    r.check("kernels WanGP could not load are their own warning",
            len(found) == 1 and found[0]["code"] == kernels.UNAVAILABLE, str(found))
    kernels.forget()
    for line in ("[GGUF][llama.cpp CUDA] kernels available.",
                 "[GGUF][llama.cpp CUDA] linear fast path active for Q5_K.",
                 "100%|##########| 8/8 [00:23<00:00,  2.99s/it]"):
        kernels.observe(line)
    r.check("WanGP's ordinary lines are no warning", kernels.warnings() == [])


class _Process:
    def __init__(self, lines):
        self.stdout = io.BytesIO("".join(line + "\n" for line in lines).encode("utf-8"))
        self.pid = 0

    def poll(self):
        return None


def reader_checks(r: Results) -> None:
    """The runtime's reader is the only place WanGP's words arrive, so the
    check has to be called from there or it never hears them."""
    kernels.forget()
    child = runtime._Child.__new__(runtime._Child)
    child.process = _Process(["Loading Model ...", TOO_OLD_LINE])
    child.stderr_tail = __import__("collections").deque(maxlen=50)
    runtime._drain(child)
    r.check("a line read from WanGP reaches the check", len(kernels.warnings()) == 1, str(kernels.warnings()))
    kernels.forget()


def snapshot_checks(r: Results) -> None:
    from minipaint_neo import interop

    kernels.forget()
    kernels.observe(UNAVAILABLE_LINE)
    payload = interop.snapshot("")
    r.check("the interop snapshot carries the warnings for the page",
            [item["code"] for item in payload.get("wangp_warnings", [])] == [kernels.UNAVAILABLE],
            str(payload.get("wangp_warnings")))
    kernels.forget()
    payload = interop.snapshot("")
    r.check("and an empty list when there is nothing to say", payload.get("wangp_warnings") == [])


def run() -> Results:
    r = Results("wangp kernels")
    reading_checks(r)
    launch_checks(r)
    output_checks(r)
    reader_checks(r)
    snapshot_checks(r)
    return r


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
