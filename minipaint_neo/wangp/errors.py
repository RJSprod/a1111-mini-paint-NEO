"""Failure codes, and the one exception that carries them.

Every failure the integration can report has a stable machine-readable code
and a sentence a user can act on. The code is what tests, the send log and a
bug report agree on; the sentence is what the tab shows. Neither ever
contains a secret, and a path only appears where the screen is explicitly a
local setup screen.
"""

from __future__ import annotations

import typing

# -- setup ------------------------------------------------------------------
SETUP_REQUIRED = "SETUP_REQUIRED"
WANGP_ROOT_MISSING = "WANGP_ROOT_MISSING"
RUNTIME_MISSING = "RUNTIME_MISSING"
RUNTIME_PROBE_FAILED = "RUNTIME_PROBE_FAILED"
GPU_UUID_MISSING = "GPU_UUID_MISSING"
CONFIG_UNREADABLE = "CONFIG_UNREADABLE"
CONFIG_SCHEMA_TOO_NEW = "CONFIG_SCHEMA_TOO_NEW"

# -- the companion plugin ---------------------------------------------------
BRIDGE_MISSING = "BRIDGE_MISSING"
BRIDGE_DISABLED = "BRIDGE_DISABLED"
BRIDGE_VERSION_MISMATCH = "BRIDGE_VERSION_MISMATCH"
BRIDGE_COMPONENT_INCOMPATIBLE = "BRIDGE_COMPONENT_INCOMPATIBLE"

# -- the child process ------------------------------------------------------
PROCESS_START_FAILED = "PROCESS_START_FAILED"
PROCESS_EXITED = "PROCESS_EXITED"
PORT_IN_USE = "PORT_IN_USE"
LOOPBACK_BIND_FAILED = "LOOPBACK_BIND_FAILED"

# -- the proxy --------------------------------------------------------------
PROXY_NOT_READY = "PROXY_NOT_READY"
PROXY_ROOT_PATH_FAILED = "PROXY_ROOT_PATH_FAILED"
PROXY_STREAM_FAILED = "PROXY_STREAM_FAILED"
AUTH_BOUNDARY_FAILED = "AUTH_BOUNDARY_FAILED"

# -- the browser session ----------------------------------------------------
IFRAME_NOT_READY = "IFRAME_NOT_READY"
BRIDGE_SESSION_MISMATCH = "BRIDGE_SESSION_MISMATCH"
RECEIVER_QUERY_TIMEOUT = "RECEIVER_QUERY_TIMEOUT"

# -- the receivers ----------------------------------------------------------
NO_ACTIVE_RECEIVER = "NO_ACTIVE_RECEIVER"
UNKNOWN_RECEIVER = "UNKNOWN_RECEIVER"
STALE_RECEIVER_STATE = "STALE_RECEIVER_STATE"
RECEIVER_DISABLED = "RECEIVER_DISABLED"
RECEIVER_LIMIT_REACHED = "RECEIVER_LIMIT_REACHED"
RECEIVER_APPLY_FAILED = "RECEIVER_APPLY_FAILED"
RECEIVER_VERIFY_FAILED = "RECEIVER_VERIFY_FAILED"

# -- the handoff ------------------------------------------------------------
HANDOFF_NOT_FOUND = "HANDOFF_NOT_FOUND"
HANDOFF_INVALID_ID = "HANDOFF_INVALID_ID"
HANDOFF_INVALID_IMAGE = "HANDOFF_INVALID_IMAGE"
HANDOFF_TOO_LARGE = "HANDOFF_TOO_LARGE"
HANDOFF_DIGEST_MISMATCH = "HANDOFF_DIGEST_MISMATCH"

# -- everything else --------------------------------------------------------
WANGP_RESTARTED = "WANGP_RESTARTED"
INTERNAL_ERROR = "INTERNAL_ERROR"

#: What each code says to a user. The tab and the Send menu read this; they
#: never build a sentence out of an exception's text.
MESSAGES: dict[str, str] = {
    SETUP_REQUIRED: "WanGP has not been set up yet.",
    WANGP_ROOT_MISSING: "The WanGP installation is no longer where it was; set the integration up again.",
    RUNTIME_MISSING: "The Python environment chosen for WanGP is no longer there; set the integration up again.",
    RUNTIME_PROBE_FAILED: "That environment could not start WanGP.",
    GPU_UUID_MISSING: "The GPU chosen for WanGP is not present. Nothing was started, and no other GPU was used.",
    CONFIG_UNREADABLE: "The WanGP integration settings could not be read.",
    CONFIG_SCHEMA_TOO_NEW: "These WanGP integration settings were written by a newer version of the extension.",
    BRIDGE_MISSING: "The MiniPaint bridge plugin is not installed in that WanGP.",
    BRIDGE_DISABLED: "The MiniPaint bridge plugin is installed but switched off in WanGP.",
    BRIDGE_VERSION_MISMATCH: "The MiniPaint bridge plugin in WanGP is a different version; intelligent send is off until it is updated.",
    BRIDGE_COMPONENT_INCOMPATIBLE: "This WanGP build does not expose the inputs the bridge needs.",
    PROCESS_START_FAILED: "WanGP could not be started.",
    PROCESS_EXITED: "WanGP stopped running.",
    PORT_IN_USE: "No free loopback port could be kept for WanGP.",
    LOOPBACK_BIND_FAILED: "WanGP did not open its loopback port.",
    PROXY_NOT_READY: "WanGP is not answering yet.",
    PROXY_ROOT_PATH_FAILED: "WanGP is answering, but not under /wan2gp/.",
    PROXY_STREAM_FAILED: "The connection to WanGP was interrupted.",
    AUTH_BOUNDARY_FAILED: "/wan2gp/ is not covered by this Forge's sign-in; the integration stayed off.",
    IFRAME_NOT_READY: "Open the WanGP tab and choose an input.",
    BRIDGE_SESSION_MISMATCH: "That WanGP page is no longer the one this send was prepared for.",
    RECEIVER_QUERY_TIMEOUT: "WanGP did not answer in time.",
    NO_ACTIVE_RECEIVER: "This WanGP model and mode take no image right now.",
    UNKNOWN_RECEIVER: "WanGP does not have that input.",
    STALE_RECEIVER_STATE: "The WanGP input changed; reopen Send to.",
    RECEIVER_DISABLED: "That WanGP input is not active right now.",
    RECEIVER_LIMIT_REACHED: "That WanGP input is full.",
    RECEIVER_APPLY_FAILED: "WanGP would not take the image.",
    RECEIVER_VERIFY_FAILED: "WanGP took an image, but it could not be confirmed as the one that was sent.",
    HANDOFF_NOT_FOUND: "The prepared image was gone before WanGP read it.",
    HANDOFF_INVALID_ID: "That is not a handoff this extension made.",
    HANDOFF_INVALID_IMAGE: "The prepared image was not a readable PNG.",
    HANDOFF_TOO_LARGE: "The image is too large for the WanGP handoff.",
    HANDOFF_DIGEST_MISMATCH: "The prepared image changed on the way to WanGP.",
    WANGP_RESTARTED: "WanGP restarted while the image was on its way.",
    INTERNAL_ERROR: "The WanGP integration hit an unexpected problem.",
}

#: Codes that mean "the setup on disk no longer describes reality". The tab
#: offers Reinitialize for these rather than Restart.
REINIT_CODES = frozenset(
    {
        WANGP_ROOT_MISSING,
        RUNTIME_MISSING,
        RUNTIME_PROBE_FAILED,
        GPU_UUID_MISSING,
        CONFIG_UNREADABLE,
        CONFIG_SCHEMA_TOO_NEW,
        BRIDGE_MISSING,
        BRIDGE_DISABLED,
        BRIDGE_VERSION_MISMATCH,
        BRIDGE_COMPONENT_INCOMPATIBLE,
    }
)


def message(code: str, detail: str = "") -> str:
    """The sentence for a code. An unknown code is still a sentence."""
    text = MESSAGES.get(code, MESSAGES[INTERNAL_ERROR])
    detail = str(detail or "").strip()
    return f"{text} {detail}".strip() if detail else text


class IntegrationError(Exception):
    """A failure with a code. ``detail`` is for the log, not the screen."""

    def __init__(self, code: str, detail: str = "", **extra: typing.Any) -> None:
        self.code = code
        self.detail = str(detail or "")
        self.extra = extra
        super().__init__(f"{code}: {self.detail}" if self.detail else code)

    @property
    def user_message(self) -> str:
        return message(self.code)

    def as_dict(self) -> dict:
        """What crosses a wire: the code and the sentence, never the detail."""
        payload = {"ok": False, "code": self.code, "message": self.user_message}
        payload.update({key: value for key, value in self.extra.items() if key not in payload})
        return payload
