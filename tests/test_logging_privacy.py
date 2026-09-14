"""Nothing this extension writes down can identify the person using it.

The promise is narrow enough to test directly: take the lines a real run
actually produces - WanGP's own console output most of all - and assert that
what comes out the other end carries no prompt, no output filename, no path,
no address and no credential, while still carrying the things a failure is
diagnosed from.

It is written as a pair of lists on purpose. ``LEAKS`` is what must never
survive, each entry a shape seen in a real WanGP log; ``KEPT`` is what must
survive, because a redaction pass with no second list is a pass that
eventually replaces the whole line with ``<path>`` and calls it a success. A
log nobody can read is not a safer log, it is a log that gets turned off.

The writers are checked as well as the rule, because the rule being right is
worth nothing if one writer goes around it. There are four that matter - the
child's drain, the journal, the process log and the transfer log - and every
one of them is exercised here against a line with a name in it.

Two structural properties get their own checks, since both are the kind of
thing that breaks quietly:

*Scrubbing twice is scrubbing once.* Lines pass through more than one writer,
and a pass that is not idempotent either grows brackets or eats more of the
line each time.

*It never raises.* The drain thread is the largest caller, and that thread
dying means the child's pipe fills and WanGP freezes mid-generation - so a
scrubber that throws is worse than no scrubber at all.
"""

from harness import Results, setup_path

setup_path()

import io  # noqa: E402
import os  # noqa: E402
import pathlib  # noqa: E402
import tempfile  # noqa: E402

from minipaint_neo import scrub, send_log  # noqa: E402
from minipaint_neo.wangp import journal, process_log, runtime  # noqa: E402

#: A name that is not a word, so finding it anywhere in an output is proof
#: rather than coincidence.
PERSON = "Rosalind"
HOME = f"/home/{PERSON.lower()}"
WANGP_ROOT = f"{HOME}/Wan2GP"

#: Lines a WanGP run really produces, each with something in it that belongs
#: to whoever is sitting at the machine. The second element is what must not
#: appear in the scrubbed line.
LEAKS = (
    ("Prompt: a photo of Rosalind at her mother's house", "Rosalind"),
    ("negative prompt: blurry, ugly, Rosalind", "Rosalind"),
    ('--prompt "a photo of Rosalind" --steps 30', "Rosalind"),
    ('{"prompt": "a photo of Rosalind", "seed": 42}', "Rosalind"),
    (f"Saving video to {WANGP_ROOT}/outputs/2026-09-11_a_photo_of_Rosalind.mp4", "Rosalind"),
    (f"Saving video to {WANGP_ROOT}/outputs/2026-09-11_a_photo_of_Rosalind.mp4", "2026-09-11_a"),
    ("a_photo_of_Rosalind.mp4 written in 42.1s", "Rosalind"),
    ("wrote a photo of Rosalind.mp4", "Rosalind"),
    ("outputs/a_photo_of_Rosalind.webp saved", "Rosalind"),
    (r"Loading lora C:\Users\Rosalind\loras\rosalind_face_v3.safetensors", "Rosalind"),
    (r"reading C:\Users\Rosalind Carter\Wan2GP\settings\preset.json", "Carter"),
    (f"could not read {HOME}/.cache/huggingface/token", PERSON.lower()),
    (f'File "{HOME}/forge/modules/processing.py", line 402, in run', PERSON.lower()),
    ("Running on public URL: https://8ac1f2.gradio.live", "gradio.live"),
    ("mail rosalind.carter@example.com about it", "rosalind.carter"),
    ("listening on 192.168.1.44:7860", "192.168.1.44"),
    ("MINIPAINT_WANGP_BRIDGE_SECRET=o4Zq1v-not-a-real-one", "o4Zq1v"),
    ("hf_AbCdEfGhIjKlMnOpQrStUvWxYz012345 loaded", "AbCdEfGhIj"),
    ("instance 9f3c2b1a4d5e6f708192a3b4c5d6e7f8 ready", "9f3c2b1a"),
    # The value is the word after the scheme, not the scheme: a rule that
    # stopped at the first space redacted "Bearer" and published the token.
    ("authorization: Bearer eyJhbGciOiJIUzI1NiJ9.QQQQQQQQQQQQ", "eyJhbGci"),
    ("Authorization = Basic cm9zYWxpbmQ6aHVudGVyMg==", "cm9zYWxpbmQ"),
    ("cookie: session=abc; hf_token=QQQQQQQQQQQQQQQQQQQQ", "hf_token=Q"),
)

#: What a failure is actually diagnosed from. None of it identifies anybody,
#: and a pass that removes any of it has gone too far.
KEPT = (
    ("ModuleNotFoundError: No module named 'torch'", "No module named 'torch'"),
    ("OSError: [Errno 98] Address already in use", "Address already in use"),
    (f'File "{WANGP_ROOT}/wgp.py", line 402, in generate_video', "wgp.py"),
    (f'File "{WANGP_ROOT}/wgp.py", line 402, in generate_video', "line 402"),
    ("wgp_config.json enabled_plugins = ['wan2gp-minipaint-bridge']", "wgp_config.json"),
    ("image_prompt_type: I, video_prompt_type: PV", "image_prompt_type: I"),
    ("bridge: protocol 2, plugin 1.0.2", "protocol 2, plugin 1.0.2"),
    ("1920x1080 at 24fps, 81 frames", "1920x1080"),
    ("  5%|##    | 5/100 [00:03<00:57,  1.72it/s]", "1.72it/s"),
    ("WanGP exited with code 1", "exited with code 1"),
    ("wrote a photo of Rosalind.mp4", "wrote"),
    (f"Saving video to {WANGP_ROOT}/outputs/a.mp4", ".mp4"),
    ("Loaded 3 models in 4.2 seconds", "3 models in 4.2 seconds"),
)


def scrubber_checks(r: Results) -> None:
    """The rule itself, against both lists and against being run twice."""
    scrub.forget_roots()
    scrub.register_root("wangp", WANGP_ROOT)

    for line, forbidden in LEAKS:
        cleaned = scrub.line(line)
        r.check(f"the line loses {forbidden!r}", forbidden not in cleaned, cleaned)

    for line, wanted in KEPT:
        cleaned = scrub.line(line)
        r.check(f"the line keeps {wanted!r}", wanted in cleaned, cleaned)

    # A root the integration knows about is structure, and structure is worth
    # reading: "it wrote a video into the outputs folder" is the useful half
    # of a line whose other half was the prompt.
    labelled = scrub.line(f"Saving video to {WANGP_ROOT}/outputs/whatever.mp4")
    r.check("a known install is labelled, not flattened",
            labelled == "Saving video to <wangp>/outputs/*.mp4", labelled)
    scrub.forget_roots()
    unlabelled = scrub.line(f"Saving video to {WANGP_ROOT}/outputs/whatever.mp4")
    r.check("an unknown one keeps no directory at all",
            unlabelled == "Saving video to <path>/*.mp4", unlabelled)
    scrub.register_root("wangp", WANGP_ROOT)

    # Registering the same label twice must not leave the old root matching:
    # a setup repointed at another install would otherwise keep labelling
    # paths from the one it no longer uses.
    scrub.register_root("wangp", "/elsewhere/Wan2GP")
    roots = [prefix for name, prefix in scrub.known_roots() if name == "<wangp>"]
    r.check("re-registering a label replaces it", roots == [os.path.normcase("/elsewhere/Wan2GP")], str(roots))
    scrub.forget_roots()
    scrub.register_root("wangp", WANGP_ROOT)

    for line, _ in LEAKS + KEPT:
        once = scrub.line(line)
        r.check(f"scrubbing twice is scrubbing once ({line[:28]!r})", scrub.line(once) == once, once)

    # The port is the tab's rule, not the console's: the browser must never
    # learn it, and the console is where somebody standing at the machine
    # needs to read it.
    r.check("the console keeps the backend port",
            "7862" in scrub.line("bound 127.0.0.1:7862"))
    r.check("anything the tab renders does not",
            "7862" not in scrub.private("bound 127.0.0.1:7862"))

    # Never raises, whatever it is handed.
    for odd in (None, 0, b"bytes", object(), ["a", "list"], {"a": 1}, "x" * 20000, "\x00\x07\x1b[31m"):
        try:
            scrub.line(odd)
            scrub.private(odd)
            scrub.block(odd)
            survived = True
        except Exception:
            survived = False
        r.check(f"a strange value is scrubbed, not raised ({type(odd).__name__})", survived)

    r.check("a line is bounded", len(scrub.private("x " * 5000)) <= scrub.MAX_LINE)
    r.check("control characters do not survive", "\x1b" not in scrub.line("\x1b[31mred\x1b[0m"))

    # A traceback keeps its shape, because a traceback on one line is a
    # traceback nobody reads.
    trace = scrub.block(f'Traceback (most recent call last):\n  File "{HOME}/x/wgp.py", line 1, in <module>\n')
    r.check("a traceback stays multi-line", len(trace.splitlines()) == 2, trace)
    r.check("and loses the home directory", PERSON.lower() not in trace, trace)
    r.check("but keeps the module", "wgp.py" in trace, trace)


def child_output_checks(r: Results) -> None:
    """The drain: what WanGP says, on its way to four different screens.

    This is the leak that started all of it. The child's words used to reach
    the WebUI console verbatim, which put prompts and output filenames on the
    surface most likely to be pasted, screenshotted or streamed.
    """
    from test_wangp_runtime import FakeProcess, unused_pid

    spoken = [
        b"Loading model...\n",
        f"Prompt: a photo of {PERSON} at the beach\n".encode("utf-8"),
        f"Saving video to {WANGP_ROOT}/outputs/a_photo_of_{PERSON}.mp4\n".encode("utf-8"),
        b"ModuleNotFoundError: No module named 'torch'\n",
    ]

    process = FakeProcess(unused_pid(), stderr_lines=list(spoken))
    child = runtime._Child(process, "instance", WANGP_ROOT, 7862)

    journal.clear()
    console = io.StringIO()
    import sys

    previous = sys.stderr
    sys.stderr = console
    try:
        runtime._drain(child)
    finally:
        sys.stderr = previous

    echoed = console.getvalue()
    recorded = journal.text()
    crash_tail = "\n".join(child.tail())

    for name, written in (("console", echoed), ("tab console", recorded), ("crash tail", crash_tail)):
        r.check(f"the {name} loses the prompt", PERSON not in written, written)
        r.check(f"the {name} loses the output filename", "a_photo_of" not in written, written)
        r.check(f"the {name} still says what went wrong", "No module named 'torch'" in written, written)
        r.check(f"the {name} still says what it was doing", "Loading model" in written, written)

    r.check("the console still gets a line per line of output",
            len([line for line in echoed.splitlines() if line.strip()]) == len(spoken), echoed)

    # The error screen quotes the tail, so the tail being clean is what keeps
    # a prompt off the one surface a user reads without asking for it.
    r.check("the tail is the scrubbed text, not a scrubbed copy of it",
            all(PERSON not in line for line in child.tail()), str(child.tail()))


def process_log_checks(r: Results) -> None:
    """The file in the extension folder: present, bounded, and safe to attach."""
    base = tempfile.TemporaryDirectory()
    try:
        directory = pathlib.Path(base.name) / "logs"
        process_log.use_log_dir(directory)

        r.check("the log lives beside the extension, in logs/",
                process_log.LOG_PATH.parent.name == "logs"
                and pathlib.Path(process_log.path()).name == "wangp-log.txt")

        process_log.begin("9f3c2b1a4d5e6f708192a3b4c5d6e7f8", f"root {WANGP_ROOT}")
        process_log.note("wangp", f"Prompt: a photo of {PERSON}")
        process_log.note("wangp", f"Saving video to {WANGP_ROOT}/outputs/{PERSON}.mp4")
        process_log.note("wangp", "ModuleNotFoundError: No module named 'torch'")
        process_log.end("9f3c2b1a4d5e6f708192a3b4c5d6e7f8", "exit code 1")

        written = process_log.LOG_PATH.read_text(encoding="utf-8")
        r.check("the file is written", process_log.LOG_PATH.is_file())
        r.check("it says a run started", "run 9f3c2b1a starting" in written, written)
        r.check("and never the whole instance id", "9f3c2b1a4d5e6f70" not in written, written)
        r.check("it carries no prompt", PERSON not in written, written)
        r.check("it carries the failure", "No module named 'torch'" in written, written)
        r.check("every line says who said it", all(
            "wangp" in line or "session" in line for line in written.splitlines() if line.strip()), written)

        tail = process_log.tail(2)
        r.check("the tail reads back", len(tail) == 2 and "run 9f3c2b1a ended" in tail[-1], str(tail))

        # Bounded, and bounded by rotation rather than by truncation, so the
        # run before the one that failed is still there.
        process_log.MAX_BYTES, kept_max = 2000, process_log.MAX_BYTES
        try:
            for index in range(400):
                process_log.note("wangp", f"line {index} of a very chatty model loading pass")
            r.check("the log rotates rather than growing",
                    process_log.LOG_PATH.stat().st_size <= 2000 + process_log.MAX_LINE + 200,
                    str(process_log.LOG_PATH.stat().st_size))
            r.check("and the previous run is still there", process_log.PREVIOUS_PATH.is_file())
        finally:
            process_log.MAX_BYTES = kept_max

        # A disk that refuses is not a reason to lose the child's pipe.
        process_log.use_log_dir(pathlib.Path(base.name) / "nope" / "\0bad")
        try:
            process_log.note("wangp", "a line nobody can write")
            survived = True
        except Exception:
            survived = False
        r.check("a log that cannot be written is not an exception", survived)
        r.check("and it is only complained about once", process_log._state["broken"])
    finally:
        process_log.use_log_dir(None)
        base.cleanup()


def journal_checks(r: Results) -> None:
    """The tab's console and the file are one record written to two places."""
    base = tempfile.TemporaryDirectory()
    try:
        process_log.use_log_dir(pathlib.Path(base.name) / "logs")
        journal.clear()
        journal.note("wangp", f"Prompt: a photo of {PERSON}")

        on_disk = process_log.LOG_PATH.read_text(encoding="utf-8")
        r.check("a journal line reaches the file", "Prompt:" in on_disk, on_disk)
        r.check("scrubbed there too", PERSON not in on_disk, on_disk)
        r.check("and scrubbed in the tab", PERSON not in journal.text(), journal.text())

        # Clearing the tab's console is not a request to destroy the only
        # durable record of the run that has just gone wrong.
        journal.clear()
        r.check("clearing the tab leaves the file alone",
                "Prompt:" in process_log.LOG_PATH.read_text(encoding="utf-8"))
    finally:
        process_log.use_log_dir(None)
        base.cleanup()


def transfer_log_checks(r: Results) -> None:
    """The send log, whose writer is a browser and whose file outlives the run."""
    entry = send_log.format_send_entry(
        {
            "destination": "WanGP - Reference",
            "outcome": f"sent {WANGP_ROOT}/outputs/{PERSON}.png",
            "steps": [f"prompt: a photo of {PERSON}", "runtime: READY", "bridge: protocol 2"],
        }
    )
    r.check("the transfer log loses the filename and the prompt", PERSON not in entry, entry)
    r.check("and keeps the step that matters", "runtime: READY" in entry, entry)
    r.check("and still says where it went", "WanGP - Reference" in entry, entry)
    r.check("and stays one entry", entry.endswith("\n\n"), repr(entry[-4:]))

    for odd in (None, [], "text", {"steps": "not a list"}, {"destination": object()}):
        try:
            send_log.format_send_entry(odd)
            survived = True
        except Exception:
            survived = False
        r.check(f"a malformed record is rendered, not raised ({odd!r:.24})", survived)


def relayed_prose_checks(r: Results) -> None:
    """The sentence, which no rule about shapes can see.

    WanGP's prompt enhancer streams the prompt it has just written straight to
    stdout: no key, no quotes, no filename, nothing for ``_TEXT_KEY`` to match
    on. Seven of them reached a real log file that way before this existed.
    The lines below are that sequence, in the order the pipe produced it.
    """
    # The enhancer's bar, the prompt it just wrote, the next thing the model
    # did. Only the middle one belongs to a person.
    bar = " Qwen3.5 prompt enhancement (vllm): 100%|##########| 1/1 [00:09<00:00,  9.36s/steps]"
    prose = (
        "she steps through the doorway and turns to look back at him while the light "
        "behind her fades to nothing and the room falls quiet again"
    )
    after = " H3 denoising Spectrum anchor capture:   0%|          | 0/6 [00:00<?, ?steps/s]"

    relay = scrub.Relay()
    said = [relay.line(one, limit=1000) for one in (bar, prose, after)]
    r.check("the enhancer's own progress bar survives", "Qwen3.5 prompt enhancement" in said[0], said[0])
    r.check("the prompt it streamed does not", said[1].strip() == scrub.PROMPT, said[1])
    r.check("none of the prompt's words survive",
            not any(word in said[1] for word in ("doorway", "fades", "quiet")), said[1])
    r.check("the next machine line survives", "H3 denoising" in said[2], said[2])

    # The latch: a prompt too short for the sentence test is still a prompt,
    # and the enhancer having just been named is the only warning there is.
    short = scrub.Relay()
    short.line(bar, limit=1000)
    withheld = short.line(f"{PERSON} steps outside.", limit=1000)
    r.check("a short line right after the enhancer is withheld too",
            withheld.strip() == scrub.PROMPT, withheld)
    r.check("and the name in it is gone with it", PERSON not in withheld, withheld)

    # ...and the latch lets go, or it would eat the rest of the run.
    short.line(after, limit=1000)
    resumed = short.line("Loaded 3 models in 4.2 seconds", limit=1000)
    r.check("the latch lets go once the machine is talking again",
            "3 models in 4.2 seconds" in resumed, resumed)

    # A sentence needs no latch: the enhancer is not the only thing that can
    # print one, and the test above would pass a prose leak from anywhere else.
    r.check("prose on its own is withheld with no latch armed",
            scrub.Relay().line(prose, limit=1000).strip() == scrub.PROMPT)

    # The other half of the promise. Every one of these is a line the OOM in
    # the log this was written from was actually diagnosed from, and each is
    # longer and wordier than most - which is exactly what makes them the ones
    # a sentence test gets wrong.
    machine = (
        "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 40.00 MiB. GPU 0 has a "
        "total capacity of 31.84 GiB of which 0 bytes is free.",
        "This is technically allowed, but may indicate you are losing the last user-visible "
        "tensor through which the allocation can be reclaimed.",
        "The whole model was pinned to reserved RAM: 57 large blocks spread across 12043.01 MB",
        "Async loading plan for model 'transformer' : base size of 58.22 MB will be preloaded "
        "with a 210.29 MB async circular shuttle",
        "[GGUF] linear backend=PyTorch dense weight materialization for Q5_K.",
        "    return self.token_refiner([self.condition_proj(text_states[0])]).unsqueeze(0)",
        "  File \"/opt/wangp/models/minimax_h3/pipeline.py\", line 893, in generate",
    )
    quiet = scrub.Relay()
    for one in machine:
        out = quiet.line(one, limit=1000)
        r.check(f"a diagnosis survives the sentence test: {one[:38]}...",
                out.strip() != scrub.PROMPT, out)

    # Structural, like the two the module already promises.
    twice = scrub.Relay()
    once = twice.line(prose, limit=1000)
    r.check("a withheld line stays withheld and stops shrinking",
            scrub.Relay().line(once, limit=1000).strip() == scrub.PROMPT, once)
    r.check("two relays do not share a latch", scrub.Relay()._armed is False)
    for bad in (None, 3, object(), b"\xff\xfe"):
        r.check(f"a relay never raises on {type(bad).__name__}",
                isinstance(scrub.Relay().line(bad), str))


def drain_prose_checks(r: Results) -> None:
    """...and the drain is what has to be using it.

    The rule being right is worth nothing if the one writer that matters goes
    around it, so this is the same sequence again through the real drain, on
    the three surfaces it feeds.
    """
    from test_wangp_runtime import FakeProcess, unused_pid

    prose = (
        "she steps through the doorway and turns to look back at him while the light "
        "behind her fades to nothing and the room falls quiet again"
    )
    spoken = [
        b" Qwen3.5 prompt enhancement (vllm): 100%|#####| 1/1 [00:09<00:00,  9.36s/steps]\n",
        prose.encode("utf-8") + b"\n",
        b"ModuleNotFoundError: No module named 'torch'\n",
    ]
    process = FakeProcess(unused_pid(), stderr_lines=list(spoken))
    child = runtime._Child(process, "instance", WANGP_ROOT, 7862)

    journal.clear()
    console = io.StringIO()
    import sys

    previous = sys.stderr
    sys.stderr = console
    try:
        runtime._drain(child)
    finally:
        sys.stderr = previous

    surfaces = (("console", console.getvalue()), ("tab console", journal.text()),
                ("crash tail", "\n".join(child.tail())))
    for name, written in surfaces:
        r.check(f"the {name} loses the streamed prompt", "doorway" not in written, written)
        r.check(f"the {name} says something was taken out", scrub.PROMPT in written, written)
        r.check(f"the {name} still says what went wrong",
                "No module named 'torch'" in written, written)


def no_exemptions_checks(r: Results) -> None:
    """Nothing is exempt from the scrubber, including us.

    WHY THIS IS A TEST AND NOT A COMMENT.

    A diagnostic once needed one of our own routes to survive redaction, and
    a narrow exception was written to let it through. It worked, the fact was
    captured, and it was taken out again the same day - but a rule with a
    hole in it is exactly the kind of thing that grows back, because the next
    person to want a path in a log will find the same reasons persuasive.

    So the property is asserted rather than remembered: a route this
    extension serves is a path, is redacted like any other path, and there is
    no allowlist anywhere for it to be added to. A log that is worth sharing
    is one whose rules have no exceptions in them.
    """
    for name in ("allow_route_prefix", "is_own_route", "_SAFE_ROUTE_PREFIXES", "SAFE_ROUTES", "exempt"):
        r.check(f"the scrubber has no {name}", not hasattr(scrub, name))

    ours = (
        "/wan2gp/",
        "/deepy/deepy_api/state",
        "/minipaint-interop/events",
        "/wan2gp/__minipaint_auth_probe",
    )
    for route in ours:
        cleaned = scrub.line(f"reverse proxy ready at {route} (one loopback upstream)")
        r.check(f"{route} is redacted in a console line like any other path",
                route not in cleaned and scrub.PATH in cleaned, cleaned)
        kept = scrub.private(f"a request for {route} answered 404")
        r.check(f"{route} is redacted in a file line too", route not in kept and scrub.PATH in kept, kept)

    # And the things a shared log is shared in spite of.
    private = scrub.private("upstream 127.0.0.1:60434; localhost:7860")
    r.check("the backend port never reaches a file anybody sends us",
            "60434" not in private and "7860" not in private, private)
    r.check("but the console keeps it, because that screen belongs to whoever is at the machine",
            "60434" in scrub.line("upstream 127.0.0.1:60434"))


def run() -> Results:
    r = Results("logging privacy")
    kept_roots = scrub.known_roots()
    try:
        scrubber_checks(r)
        child_output_checks(r)
        relayed_prose_checks(r)
        drain_prose_checks(r)
        process_log_checks(r)
        journal_checks(r)
        transfer_log_checks(r)
    finally:
        scrub.forget_roots()
        for label, prefix in kept_roots:
            scrub.register_root(label.strip("<>"), prefix)
        journal.clear()
    no_exemptions_checks(r)
    return r


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
