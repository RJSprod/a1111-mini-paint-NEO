"""One line of text with nothing in it that belongs to a person.

Everything this extension writes anywhere a human or a file can read it goes
through this module first: the WebUI console, the tab's console, the transfer
log, the WanGP process log. There is one function for that and every writer
calls it, because the alternative - each writer deciding for itself - is how a
log ends up holding a prompt.

The rules are about *shapes*, not about sources, because the hardest writer to
reason about is not ours. WanGP is a large third-party application whose
output we merely relay, and it says things like::

    Prompt: a photo of Sarah at her mother's house
    Saving video to /home/sarah/Wan2GP/outputs/sarah_at_her_mothers_house.mp4
    Loading lora C:\\Users\\Sarah\\loras\\sarah_face_v3.safetensors

Every one of those is a sentence nobody would knowingly paste into an issue,
and none of them is a secret in the sense the rest of this codebase already
handles. So the shapes are what get taken out:

* **free text behind a prompt-ish key** - ``prompt``, ``caption`` and their
  negative/positive/text variants, to the end of the line, because the end of
  a prompt is not something a regular expression gets to guess at;
* **paths**, rooted or relative, down to a placeholder plus a type. A path is
  the single richest leak in a generation log: the directories carry the
  account name and the last component carries the prompt that produced it;
* **bare filenames** with a content extension, for the same reason, since
  WanGP frequently prints one with no directory in front of it;
* **URLs, e-mail addresses and non-loopback IP addresses**;
* **credential shapes** - the ``key=value`` forms, the vendor-prefixed tokens,
  and any long opaque run that could be an id or a digest.

What deliberately survives is everything that helps and identifies nobody:
exception types, error codes, module filenames in a traceback, version
numbers, counts, dimensions, the names of WanGP's own settings, and file
*extensions* - ``<path>/*.mp4`` still says a video was written.

Known installations are registered rather than guessed at. ``register_root``
teaches the scrubber that a directory is the extension, the WanGP install or
its interpreter, and paths underneath one come out as ``<wangp>/outputs/*.mp4``
instead of a flat ``<path>``: the part that locates a file inside a known tree
is structure, and the part above it is the one carrying the account name.

What it cannot do, stated plainly because a redaction pass that is believed
to do more than it does is worse than none: it removes *shapes*, so a bare
personal name typed into free text with nothing structural around it survives.
The one place that reaches is a label the user chose themselves - a model
name, a LoRA, a receiver - which is kept on purpose, since "which model
failed" is the question a transfer log exists to answer. Prompts, filenames
and paths are shapes and are gone; a LoRA called after somebody is not, and
renaming it is the only fix there has ever been for that.

Two promises this module is written to keep, both checked by
``tests/test_logging_privacy.py``:

*Scrubbing twice is scrubbing once.* Every placeholder is shaped so that no
rule can match it again, so a line that passes through two writers does not
accumulate brackets or lose more of itself the second time.

*It never raises.* A redaction that throws would either take down the caller
or, far worse, be wrapped in a bare ``except`` somewhere and let the raw line
through. So the whole pass is guarded and a failure returns a placeholder
rather than the original.
"""

from __future__ import annotations

import os
import re
import threading
import typing

#: Long enough for a traceback frame or a WanGP status line, short enough that
#: a runaway line cannot push everything else out of a bounded log.
MAX_LINE = 400

# -- what a redaction looks like --------------------------------------------
#: Every placeholder is bracketed, and every rule below refuses to start a
#: match after ``<``, ``>`` or ``*``. That pair of facts is what makes a second
#: pass a no-op, which is the property that lets writers scrub defensively
#: without having to know whether someone upstream already did.
HIDDEN = "<hidden>"
PROMPT = "<prompt>"
FILE = "<file>"
PATH = "<path>"
URL = "<url>"
EMAIL = "<email>"
ADDRESS = "<ip>"
UNPRINTABLE = " "

#: Addresses that name a machine rather than a person, and are worth keeping:
#: "it bound loopback" and "it bound 0.0.0.0" are different failures.
_PUBLIC_ADDRESSES = frozenset({"127.0.0.1", "0.0.0.0", "255.255.255.255"})

#: Extensions whose last path component is a library, not a person's file.
#: Keeping them is what makes a traceback still worth reading.
KEEP_SUFFIXES = frozenset(
    {".py", ".pyc", ".pyo", ".pyd", ".so", ".dylib", ".dll", ".exe", ".bat", ".cmd", ".sh", ".ps1"}
)

#: Exact filenames this integration, WanGP or Python name structurally. A
#: whitelist rather than a suffix rule, because ``.json`` and ``.txt`` are also
#: what a user's saved settings and notes are called.
KEEP_NAMES = frozenset(
    {
        "config.json",
        "plugin_info.json",
        "pyvenv.cfg",
        "requirements.txt",
        "send-log.previous.txt",
        "send-log.txt",
        "settings.json",
        "wan2gp.backup.json",
        "wan2gp.json",
        "wan2gp.pending.json",
        "wangp-log.previous.txt",
        "wangp-log.txt",
        "wgp_config.json",
    }
)

#: Suffixes that mean "somebody's content": an output, a model, an archive. A
#: bare one of these with no directory in front is still redacted, because
#: WanGP prints output filenames that way and the filename *is* the prompt.
CONTENT_SUFFIXES = frozenset(
    {
        ".avi", ".bmp", ".flac", ".gif", ".jpeg", ".jpg", ".m4a", ".m4v", ".mkv", ".mov",
        ".mp3", ".mp4", ".mpo", ".ogg", ".opus", ".png", ".psd", ".svg", ".tif", ".tiff",
        ".wav", ".webm", ".webp", ".avif",
        ".bin", ".ckpt", ".gguf", ".npy", ".npz", ".onnx", ".pkl", ".pt", ".pth", ".safetensors",
        ".7z", ".gz", ".rar", ".tar", ".zip",
        ".csv", ".json", ".log", ".md", ".txt", ".yaml", ".yml",
    }
)

#: How much of a known install's inner structure is worth keeping. Two levels
#: says "outputs", "loras/flux" and stops before a folder someone named after
#: themselves gets three deep.
MAX_KEPT_SEGMENTS = 2

#: Filenames contain spaces, so a filename rule has to be able to read across
#: one - which means it can also swallow the sentence in front of the name.
#: These are the words it refuses to swallow: ordinary English joining words
#: and the verbs a log uses to introduce a file. "wrote model.safetensors"
#: keeps its verb; "Sarah Jones.mp4" keeps nothing.
SENTENCE_WORDS = frozenset(
    """a an and are as at be by for from in into is it its of on onto or over the this to
    was were will with without
    cannot could did directory does failed folder found is loaded loading located missing
    named new not open opened opening output outputs read reading resolved save saved saving
    to using used wrote write writing written
    already error exists file files image images model models video videos lora loras""".split()
)

# -- the rules, in the order they are applied -------------------------------

#: Nothing may start a match immediately after one of these, which is what
#: stops a placeholder written by an earlier rule from being eaten by a later
#: one - or by a second pass over the same line.
_GUARD = r"(?<![\w<>*])"

#: ``key=value`` credentials. The value goes; the key stays, because "there was
#: an authorization header" is worth knowing and its contents never are.
#: No word boundary in front on purpose: the name that matters most here is
#: ``MINIPAINT_WANGP_BRIDGE_SECRET``, and an underscore is a word character,
#: so a leading \\b would let every environment variable through.
_SECRET = re.compile(
    r"(?i)(secrets?|tokens?|passwords?|passwd|pwd|authorization|api[_-]?keys?|"
    r"access[_-]?keys?|refresh[_-]?tokens?|bearer|hf[_-]?tokens?|session[_-]?hash|"
    r"cookies?|credentials?)\b\s*[=:]\s*\S+"
)

#: Vendor-prefixed tokens, which travel on their own with no key in front.
_TOKEN = _GUARD + r"\b(?:hf_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{16,})\b"
_TOKEN_RE = re.compile(_TOKEN)

#: An opaque run long enough to be an id, a digest or a key. Bounded below so
#: that a commit hash, a resolution or a version number is not swept up.
_OPAQUE = re.compile(_GUARD + r"\b(?:[0-9a-fA-F]{32,}|[A-Za-z0-9_-]{43,})\b")

#: Free text behind a key that means "what the user typed". Taken to the end of
#: the line: a prompt has no closing token, and stopping early is how half a
#: prompt ends up in a log. ``\b`` around ``prompt`` is doing real work here -
#: it is why WanGP's ``image_prompt_type`` setting keeps its value.
_TEXT_KEY = re.compile(
    r"(?i)\b((?:negative[ _-]?|positive[ _-]?|text[ _-]?|input[ _-]?)?(?:prompts?|captions?))"
    r"""["']?\s*[=:]\s*(?!\s*["']?<prompt>).*"""
)

#: The same thing as a JSON member. Handled before the loose rule below so
#: that only the value goes and the rest of the object survives - a line of
#: JSON is usually carrying the seed and the resolution as well.
_TEXT_JSON = re.compile(
    r"""(?i)(["'])((?:negative[ _-]?|positive[ _-]?)?(?:prompts?|captions?))\1\s*:\s*"(?:[^"\\]|\\.)*"
    """,
    re.VERBOSE,
)

#: The same thing written as a quoted argument rather than a key.
_TEXT_QUOTED = re.compile(
    r"(?i)\b((?:negative[ _-]?|positive[ _-]?)?prompts?)\b\s+(['\"])(?:(?!\2).)*\2"
)

#: Keys whose value is a name rather than a sentence.
_NAME_KEY = re.compile(
    r"(?i)\b(file[ _-]?names?|output[ _-]?files?|sav(?:e|ing|ed)[ _-]?(?:file|path|to|as)|"
    r"dest(?:ination)?[ _-]?(?:file|path)?)\s*[=:]\s*.*"
)

_EMAIL = re.compile(_GUARD + r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")

#: Any scheme, not just http: a WanGP share link, an S3 URL and a file:// path
#: are all things a pasted log should not carry.
_URL = re.compile(_GUARD + r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>]+")

_IPV4 = re.compile(_GUARD + r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

#: A rooted path: a drive, a UNC share, a home shorthand, an explicit relative
#: root, or a POSIX absolute. The tail stops at whitespace, quotes and the
#: punctuation a sentence puts after a path.
#: What a path is allowed to be made of once it has started. A space is in
#: there too, but only where a separator still follows it before the next
#: whitespace: "C:\Users\Sarah Jones\Wan2GP" is one path and
#: "wrote /tmp/x and stopped" is a path followed by two words.
_INSIDE = r"""(?: [^\s'"<>|*?;,)\]}] | [ ](?=[^\s'"<>|*?;,)\]}]*[\\/]) )*"""

_ROOTED = re.compile(
    _GUARD
    + r"""(?:
            [A-Za-z]:[\\/]
          | \\\\[^\s\\/]+[\\/]?
          | ~[\\/]
          | \.{1,2}[\\/]
          | /(?=[^\s/])
        )
    """
    + _INSIDE,
    re.VERBOSE,
)

#: A relative path, recognised only when it ends in a filename with an
#: extension. Without that condition "1.2it/s" in a progress bar reads as a
#: path, and a log full of <path> where the speed used to be helps nobody.
_RELATIVE = r"[\w.~%+-]+[\\/](?:[\w.~%+-]+[\\/])*[\w.~%+-]+\.[A-Za-z0-9]{1,12}\b"

#: Both shapes in one expression, substituted in one pass. That is not tidying:
#: ``re.sub`` never looks at what it has just written, so a single pass is what
#: stops the relative rule from reading "<extension>/logs/..." as a fresh
#: relative path and flattening the label the rooted rule just worked out.
_PATH = re.compile(
    "(?:" + _ROOTED.pattern + "|" + _GUARD + r"(?<![\\/])" + _RELATIVE + ")", re.VERBOSE
)

#: A filename on its own, spaces and all. Only the last word is required to
#: carry the extension; ``_replace_bare`` hands back any leading words that
#: are sentence rather than name. Whether it is redacted at all is decided by
#: the extension, so ``wgp.py`` in a traceback survives and
#: ``a photo of Sarah.mp4`` does not.
_BARE_WORD = r"[\w.~%+&'()\[\]-]+"
_CONTENT_ALTERNATION = "|".join(
    re.escape(suffix[1:]) for suffix in sorted(CONTENT_SUFFIXES, key=len, reverse=True)
)
_BARE = re.compile(
    _GUARD + r"\b" + _BARE_WORD + r"(?:[ ]" + _BARE_WORD + r")*?"
    + r"(\.(?:" + _CONTENT_ALTERNATION + r"))\b",
    re.IGNORECASE,
)

#: The backend port, in the two forms it gets written. Not a secret the way a
#: token is, but the browser is not supposed to learn it and a pasted log is
#: one copy away from anywhere - so it is taken out of everything the tab can
#: show. See ``private``.
_LOOPBACK = re.compile(r"\b(127\.0\.0\.1|localhost|\[::1\])[:/](\d{2,5})\b")

# -- registered roots -------------------------------------------------------

_roots_lock = threading.Lock()
_roots: typing.List[typing.Tuple[str, str]] = []


def register_root(label: str, path: typing.Any) -> None:
    """Teach the scrubber that ``path`` is a known install called ``label``.

    Called with the extension folder at import time and with the WanGP root
    and its interpreter prefix when a child is launched, so that the paths a
    generation log is full of come out as structure - ``<wangp>/outputs/*.mp4``
    - rather than as an anonymous ``<path>`` that could be anywhere.

    Longest prefix wins, so a venv inside the WanGP root is still labelled as
    the venv. Re-registering a label replaces it: a user who repoints the
    setup at another install must not leave the old root matching.
    """
    try:
        text = os.path.normcase(os.path.normpath(str(path or "").strip()))
    except Exception:
        return
    if not text or text in (".", os.sep):
        return
    tag = f"<{str(label).strip() or 'path'}>"
    with _roots_lock:
        kept = [(name, prefix) for name, prefix in _roots if name != tag and prefix != text]
        kept.append((tag, text))
        kept.sort(key=lambda pair: len(pair[1]), reverse=True)
        _roots[:] = kept


def forget_roots() -> None:
    """Drop every registered root. For tests, and for Reinitialize."""
    with _roots_lock:
        _roots.clear()


def known_roots() -> typing.List[typing.Tuple[str, str]]:
    with _roots_lock:
        return list(_roots)


def _label_for(normalised: str) -> typing.Tuple[str, str]:
    """``(<label>, remainder)`` when a path is inside a known root."""
    for tag, prefix in known_roots():
        if normalised == prefix:
            return tag, ""
        if normalised.startswith(prefix + os.sep) or normalised.startswith(prefix + "/"):
            return tag, normalised[len(prefix) + 1 :]
    return "", ""


# -- the path replacement ---------------------------------------------------

_TRAILING = ".,;:!?)]}'\"`"


def _suffix_of(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""


def _keep_name(name: str) -> bool:
    return name.lower() in KEEP_NAMES or _suffix_of(name) in KEEP_SUFFIXES


def _shorten(raw: str) -> str:
    """One path, reduced to what is structure and stripped of what is not."""
    label, remainder = _label_for(os.path.normcase(os.path.normpath(raw)))
    segments = [part for part in re.split(r"[\\/]+", remainder if label else raw) if part]

    # A drive letter is not a segment worth keeping, and neither is anything
    # above a root we do not recognise.
    if not label:
        segments = segments[-1:]
        kept: typing.List[str] = []
    else:
        kept = segments[:-1][:MAX_KEPT_SEGMENTS] if len(segments) > 1 else []
        if len(segments) - 1 > len(kept):
            kept = kept + ["..."]
        segments = segments[-1:]

    name = segments[0] if segments else ""
    if name and _keep_name(name):
        tail = name
    elif name and not _suffix_of(name) and label:
        # Inside a known install an extension-less last component is structure
        # - "bin", "python", "ckpts" - and dropping it turns a useful line into
        # a shrug. Outside one it could be anything, so it still goes.
        tail = name
    elif name:
        suffix = _suffix_of(name)
        tail = f"*{suffix}" if suffix else ""
    else:
        tail = ""

    parts = [label or PATH] + kept + ([tail] if tail else [])
    return "/".join(parts)


def _replace_path(match: "re.Match[str]") -> str:
    raw = match.group(0)
    trailing = ""
    while raw and raw[-1] in _TRAILING:
        trailing = raw[-1] + trailing
        raw = raw[:-1]
    if not raw:
        return trailing
    return _shorten(raw) + trailing


def _replace_bare(match: "re.Match[str]") -> str:
    """A bare filename, with whatever sentence got absorbed handed back.

    The rule reads across spaces because filenames contain them, which means a
    match can begin several words before the name does. Leading words that are
    sentence rather than name are given back verbatim, so "wrote my video.mp4"
    keeps its verb and "Sarah Jones.mp4" keeps nothing.
    """
    suffix = match.group(1).lower()
    if suffix not in CONTENT_SUFFIXES:
        return match.group(0)

    words = match.group(0).split(" ")
    given_back = 0
    while given_back < len(words) - 1 and words[given_back].lower().strip(_TRAILING) in SENTENCE_WORDS:
        given_back += 1
    if _keep_name(words[-1]):
        return match.group(0)

    prefix = " ".join(words[:given_back])
    return f"{prefix} {FILE}{suffix}" if prefix else f"{FILE}{suffix}"


def _replace_address(match: "re.Match[str]") -> str:
    address = match.group(0)
    return address if address in _PUBLIC_ADDRESSES else ADDRESS


# -- the pass itself --------------------------------------------------------


def line(text: typing.Any, limit: int = 0) -> str:
    """One printable line with the shapes above taken out of it.

    Never raises. A rule that somehow fails is not allowed to hand the caller
    the unscrubbed original, so the failure path returns a placeholder: a lost
    log line is an inconvenience and a leaked one is not.
    """
    try:
        cleaned = str(text)
        cleaned = _SECRET.sub(lambda m: f"{m.group(1)}={HIDDEN}", cleaned)
        cleaned = _TOKEN_RE.sub(HIDDEN, cleaned)
        cleaned = _TEXT_JSON.sub(lambda m: f'{m.group(1)}{m.group(2)}{m.group(1)}: "{PROMPT}"', cleaned)
        cleaned = _TEXT_QUOTED.sub(lambda m: f"{m.group(1)} {PROMPT}", cleaned)
        cleaned = _TEXT_KEY.sub(lambda m: f"{m.group(1)}: {PROMPT}", cleaned)
        cleaned = _NAME_KEY.sub(lambda m: f"{m.group(1)}: {FILE}", cleaned)
        cleaned = _EMAIL.sub(EMAIL, cleaned)
        cleaned = _URL.sub(URL, cleaned)
        cleaned = _PATH.sub(_replace_path, cleaned)
        cleaned = _BARE.sub(_replace_bare, cleaned)
        cleaned = _IPV4.sub(_replace_address, cleaned)
        cleaned = _OPAQUE.sub(HIDDEN, cleaned)
        cleaned = "".join(character if character.isprintable() else UNPRINTABLE for character in cleaned)
    except Exception:  # pragma: no cover - a rule that throws must still not leak
        return "(a log line could not be scrubbed and was dropped)"
    return cleaned[:limit] if limit and limit > 0 else cleaned


def private(text: typing.Any, limit: int = MAX_LINE) -> str:
    """``line``, plus the backend port, for anything the browser can read.

    The tab renders the journal and a user is invited to copy it, so the one
    fact this integration keeps from the browser on purpose - which loopback
    port the child bound - does not get there by way of a log line.
    """
    cleaned = line(text)
    try:
        cleaned = _LOOPBACK.sub(lambda match: f"{match.group(1)}:<port>", cleaned)
    except Exception:  # pragma: no cover
        return "(a log line could not be scrubbed and was dropped)"
    return cleaned[:limit] if limit and limit > 0 else cleaned


def block(text: typing.Any, limit: int = MAX_LINE) -> str:
    """A multi-line string - a traceback, usually - scrubbed line by line.

    Tracebacks are the reason this exists. Every frame carries an absolute
    path, and on a single-user machine an absolute path carries the account
    name, so printing one raw is the most reliable way this extension could
    put somebody's name on a screen.
    """
    try:
        return "\n".join(line(one, limit) for one in str(text).splitlines())
    except Exception:  # pragma: no cover
        return "(a traceback could not be scrubbed and was dropped)"


def console(text: typing.Any, prefix: str = "MiniPaint:") -> None:
    """Say one thing on the WebUI console, scrubbed. Never raises.

    Every ``print`` this extension performs goes through here. The port is
    deliberately *not* hidden - see ``private`` - because the console belongs
    to whoever is standing at the machine, and it is the one place the number
    is meant to be readable.
    """
    try:
        print(f"{prefix} {line(text)}" if prefix else line(text))
    except Exception:  # pragma: no cover - a console write is never worth an exception
        pass


def traceback_now(prefix: str = "MiniPaint:") -> None:
    """``traceback.print_exc()`` with the paths taken out of it.

    Printed rather than routed through ``console``: that one flattens a line,
    which is the right thing for a log line and the wrong thing for a
    traceback - a traceback on one line is a traceback nobody reads.
    """
    import traceback

    try:
        print(f"{prefix} traceback follows, with paths removed:")
        print(block(traceback.format_exc(), limit=0))
    except Exception:  # pragma: no cover
        pass


def _register_extension_root() -> None:
    """The extension's own folder, known from the moment this is imported.

    Registered here rather than by a caller so there is no window in which a
    startup line prints the install path before anybody has said what it is.
    """
    try:
        from .paths import root_path

        register_root("extension", root_path)
    except Exception:  # pragma: no cover - only if the package layout changes
        pass


_register_extension_root()
