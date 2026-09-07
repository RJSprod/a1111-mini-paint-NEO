"""The plugin WanGP loads, and the one event everything happens on.

The shape is deliberately small. During ``setup_ui`` the bridge asks for the
components and globals it needs and builds three invisible controls of its
own. During ``post_ui_setup`` it records what it actually got, builds one
adapter per receiver it can serve, wires a single Gradio event whose outputs
are the acknowledgement plus every v1 receiver component, and injects the
document script. After that it does nothing at all until a browser asks it
something.

One event rather than one per receiver, because every request needs the same
thing first: the live values of this page, read as that event's inputs. That
is what makes the answer session-scoped without a session table, and it is
what lets an apply recompute the state fingerprint from the same read that
produced the value it is about to write. Outputs the request is not addressing
come back as ``gr.update()`` - "no change" - so owning every receiver in one
event cannot turn a start-frame send into a cleared reference list.

The bridge never generates, never switches model or mode, and never redirects
a send. Its whole job is to put one picture where the user said and then prove
that is where it went; every path that cannot prove it ends in a code and a
refusal. It also never speaks first: when WanGP was started by hand rather than
by the integration there is no instance id in the environment, and the plugin
loads, reports itself unmanaged and stays quiet.

Nothing here may raise into WanGP. A bridge that breaks is a bridge that
answers "no"; it is not an exception in somebody else's generate button.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import typing

try:
    from . import bridge_js, bridge_ui, compatibility, handoff, protocol, receiver_adapters, receiver_state
except ImportError:  # pragma: no cover - depends on how WanGP imports plugins
    import bridge_js  # type: ignore[no-redef]
    import bridge_ui  # type: ignore[no-redef]
    import compatibility  # type: ignore[no-redef]
    import handoff  # type: ignore[no-redef]
    import protocol  # type: ignore[no-redef]
    import receiver_adapters  # type: ignore[no-redef]
    import receiver_state  # type: ignore[no-redef]

try:
    import gradio as gr
except Exception:  # pragma: no cover
    gr = None  # type: ignore[assignment]


PLUGIN_NAME = "wan2gp-minipaint-bridge"
THEME_FILE = "theme.css"

#: The operations the hidden trigger understands. A request naming anything
#: else is answered with a refusal rather than ignored, so that a parent stuck
#: on an older protocol gets a code instead of a timeout.
OPERATIONS = ("hello", "receivers", "receive")


def _theme_css(folder: typing.Optional[pathlib.Path] = None) -> str:
    """The stylesheet, or "" if it will not load.

    Section 27.1 again: theme is presentation. A missing or unreadable file
    costs the dark colours and nothing else.
    """
    try:
        base = folder if folder is not None else pathlib.Path(__file__).resolve().parent
        return (base / THEME_FILE).read_text(encoding="utf-8")
    except Exception:
        return ""


def bridge_session_for(session_hash: typing.Any, instance: str) -> str:
    """One opaque, stable id per browser page - derived, never stored.

    A dictionary keyed by session hash would work and would also be a table of
    live browser sessions sitting in memory for as long as WanGP runs. Deriving
    the id instead gives the same stability with nothing kept: the same page
    hashes to the same session for the life of the process, a different page
    to a different one, and a restart changes the instance id and therefore
    every session with it, which is exactly the invalidation the Forge side
    expects after a restart.
    """
    if not isinstance(session_hash, str) or not session_hash:
        return ""
    digest = hashlib.sha256(f"{instance}:{session_hash}".encode("utf-8")).hexdigest()
    return digest[:32]


class MiniPaintBridge:
    """The bridge's own logic, with no WanGP base class in the way.

    Separated from the plugin class so that all of it can be exercised with a
    stub host and a dictionary of live values: no Gradio, no WanGP, no
    browser. The plugin below is the thin part that knows about hooks.
    """

    def __init__(
        self,
        host: typing.Optional["compatibility.Host"] = None,
        environ: typing.Optional[typing.Mapping[str, str]] = None,
    ) -> None:
        self.compat = compatibility.Compatibility(host=host, environ=environ)
        self.adapters: typing.Dict[str, receiver_adapters.ReceiverAdapter] = {}
        self.state_keys: typing.Tuple[str, ...] = ()
        self.receiver_keys: typing.Tuple[str, ...] = ()
        self.environ = environ

    # -- lifecycle -----------------------------------------------------------

    def declare(self) -> None:
        self.compat.declare()

    def resolve(self) -> "compatibility.Resolution":
        resolution = self.compat.resolve()
        self.adapters = receiver_adapters.build_adapters(self.compat)
        self.state_keys = tuple(key for key, _ in self.compat.state_components())
        self.receiver_keys = tuple(self.compat.output_receivers())
        return resolution

    def forget(self) -> None:
        self.compat.forget()

    # -- reading -------------------------------------------------------------

    def live_values(self, values: typing.Sequence[typing.Any]) -> typing.Dict[str, typing.Any]:
        """Positional event inputs back into component keys.

        The order is whatever ``compatibility.state_components`` returned when
        the event was wired, and the two are recorded together for exactly
        that reason: a mismatch here would read the reference list as the
        prompt type without anything looking wrong.
        """
        return {key: values[index] for index, key in enumerate(self.state_keys) if index < len(values)}

    def state(self, values: typing.Sequence[typing.Any]) -> "receiver_state.SessionState":
        return receiver_state.read(self.compat, self.live_values(values))

    # -- answering -----------------------------------------------------------

    def describe(self, state: "receiver_state.SessionState", bridge_session: str) -> dict:
        """Section 16.2's answer for this page, right now."""
        payload = self.compat.handshake(bridge_session=bridge_session, environ=self.environ)
        payload.update(
            {
                "state_revision": state.revision,
                "model": dict(state.model),
                "view": state.view,
                "receivers": receiver_adapters.describe(self.adapters, state, include_inactive=True),
            }
        )
        if payload["ready"] and not any(item.get("enabled") for item in payload["receivers"]):
            # Everything resolved and nothing is switched on: a real, ordinary
            # state - a model that takes no image right now - and it has its
            # own code so the menu can say so instead of looking broken.
            payload["reason_code"] = compatibility.NO_ACTIVE_RECEIVER
        return payload

    def receive(
        self,
        request: typing.Mapping[str, typing.Any],
        state: "receiver_state.SessionState",
        bridge_session: str,
    ) -> typing.Tuple[dict, typing.Optional["receiver_adapters.Applied"]]:
        """Validate, apply and verify one image. Returns (ack, applied).

        The order is the whole of sections 17, 22 and 23 in one place: the
        revision is checked against a state read in *this* call, the receiver
        must be one this bridge is offering and active, the file is validated
        before it is decoded, the adapter appends or replaces, and only then
        is the result compared with what was sent. A failure at any step
        leaves WanGP exactly as it was.
        """
        receiver_id = request.get("receiver_id")
        adapter = self.adapters.get(receiver_id) if isinstance(receiver_id, str) else None
        if adapter is None:
            raise compatibility.BridgeError(compatibility.UNKNOWN_RECEIVER, f"no adapter for {receiver_id!r}")

        wanted_session = request.get("bridge_session")
        if isinstance(wanted_session, str) and wanted_session and bridge_session and wanted_session != bridge_session:
            raise compatibility.BridgeError(compatibility.BRIDGE_SESSION_MISMATCH, "this send was prepared for another page")

        receiver_state.require_unchanged(state, request.get("state_revision"))
        receiver_state.require_active(state, receiver_id)

        loaded = handoff.load(request.get("handoff_id"), request.get("source"), self.environ)
        applied = adapter.apply(loaded, adapter.operation, state)
        verification = adapter.verify(loaded, applied)

        after = self._state_after(state, applied)
        ack = {
            "ok": bool(verification.ok),
            "receiver_id": applied.receiver_id,
            "role": applied.role,
            "operation": applied.operation,
            "width": loaded.width,
            "height": loaded.height,
            "source_digest": loaded.sha256,
            "source_pixel_digest": loaded.pixel_digest,
            "receiver_pixel_digest": verification.receiver_pixel_digest,
            "previous_count": applied.previous_count,
            "new_count": applied.new_count,
            "receiver_present": applied.new_count > 0,
            "verification": verification.level,
            "state_revision_after": after.revision,
            "chained": list(applied.chained),
        }
        if not verification.ok:
            ack["code"] = verification.code or compatibility.RECEIVER_VERIFY_FAILED
        return ack, applied

    def _state_after(
        self,
        state: "receiver_state.SessionState",
        applied: "receiver_adapters.Applied",
    ) -> "receiver_state.SessionState":
        """The fingerprint this page will have once Gradio applies the value.

        Computed rather than re-read, because the value has not been applied
        yet - the callback is still running. It is the honest answer to "what
        will the next menu open see", and the parent uses it to notice that
        the revision it was holding is spent.
        """
        values = dict(state.values)
        values[applied.component_key] = applied.value
        return receiver_state.build(values, state.model, state.capabilities, state.view)

    # -- the event -----------------------------------------------------------

    def handle(
        self,
        raw_request: typing.Any,
        values: typing.Sequence[typing.Any],
        session_hash: typing.Any,
    ) -> typing.Tuple[dict, typing.Optional["receiver_adapters.Applied"]]:
        """One request in, one acknowledgement out. Never raises.

        The acknowledgement always carries the request id and the channel id it
        came in with, because the browser correlates on them and an answer it
        cannot place is an answer it must throw away.
        """
        request = _parse(raw_request)
        operation = request.get("op")
        instance = compatibility.instance_id(self.environ)
        session = bridge_session_for(session_hash, instance)

        ack: dict = {
            "op": operation if operation in OPERATIONS else "",
            "request_id": _token(request.get("request_id")),
            "channel_id": _token(request.get("channel_id")),
            "protocol": protocol.PROTOCOL,
            "bridge_version": compatibility.BRIDGE_VERSION,
            "instance_id": instance,
            "bridge_session": session,
        }
        applied: typing.Optional[receiver_adapters.Applied] = None

        try:
            if operation not in OPERATIONS:
                raise compatibility.BridgeError(compatibility.INTERNAL_ERROR, f"unknown bridge operation {operation!r}")
            if not session:
                # Without a per-page identity the bridge cannot honour section
                # 13.2, and answering anyway would let one browser tab's state
                # decide another tab's destination.
                raise compatibility.BridgeError(
                    compatibility.BRIDGE_SESSION_MISMATCH,
                    "this Gradio build did not supply a session hash",
                )

            state = self.state(values)
            if operation in ("hello", "receivers"):
                ack.update(self.describe(state, session))
                ack["ok"] = bool(ack.get("ready"))
            else:
                result, applied = self.receive(request, state, session)
                ack.update(result)
        except compatibility.BridgeError as error:
            ack.update({"ok": False, "ready": False, "code": error.code})
            _note(f"{error.code}: {error.detail}")
        except Exception as error:  # pragma: no cover - the last line of defence
            ack.update({"ok": False, "ready": False, "code": compatibility.INTERNAL_ERROR})
            _note(f"unexpected: {error!r}")

        # The bridge session and instance id are restated after the fact: a
        # handshake payload carries its own copies, and the two must agree.
        ack["bridge_session"] = session
        ack["instance_id"] = instance
        return ack, applied

    def outputs(self, ack: dict, applied: typing.Optional["receiver_adapters.Applied"]) -> typing.List[typing.Any]:
        """The event's return: the acknowledgement, then every receiver.

        Every receiver output is "no change" except the one that was written,
        which is the mechanism section 21.1 describes and the reason a single
        event can safely own all of them.
        """
        updates = bridge_ui.no_change(len(self.receiver_keys))
        if applied is not None and applied.receiver_id in self.receiver_keys:
            updates[self.receiver_keys.index(applied.receiver_id)] = applied.value
        return [json.dumps(ack, default=str), *updates]


class MiniPaintBridgePlugin(compatibility.plugin_base()):  # type: ignore[misc]
    """The WanGP-facing half: hooks in, ``MiniPaintBridge`` does the thinking.

    VERIFY ON A REAL INSTALL: the hook names and signatures. Section 3.2 lists
    ``setup_ui``, ``post_ui_setup``, ``request_component``, ``request_global``,
    ``on_model_change`` and ``add_custom_js`` as confirmed, but not their exact
    arguments. Every override below takes ``*args, **kwargs`` and forwards to
    the base class where there is one, so a signature change is a mismatch to
    correct rather than a crash inside WanGP's startup.
    """

    name = PLUGIN_NAME

    def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        try:
            super().__init__(*args, **kwargs)
        except TypeError:
            super().__init__()
        self.bridge = MiniPaintBridge(host=compatibility.Host(self))
        self.controls: typing.Optional[bridge_ui.BridgeControls] = None
        self.wired = False

    # -- hooks ----------------------------------------------------------------

    def setup_ui(self, *args: typing.Any, **kwargs: typing.Any) -> typing.Any:
        result = _super_call(self, "setup_ui", *args, **kwargs)
        if not compatibility.managed(self.bridge.environ):
            # WanGP started by hand. The bridge stays out of the way entirely
            # rather than adding controls nobody will ever message.
            return result
        self.bridge.declare()
        self.controls = bridge_ui.build()
        return result

    def post_ui_setup(self, *args: typing.Any, **kwargs: typing.Any) -> typing.Any:
        result = _super_call(self, "post_ui_setup", *args, **kwargs)
        if self.controls is None:
            return result

        resolution = self.bridge.resolve()
        if not resolution.ok:
            # Fail closed and say why. The event is not wired at all, so there
            # is no path by which an unproven component could be written to,
            # and the handshake the parent gets says BRIDGE_COMPONENT_INCOMPATIBLE.
            _note(f"not ready: missing {', '.join(resolution.missing_mandatory)}")

        self.wired = bridge_ui.wire(
            self.controls,
            self._make_handler(),
            [component for _, component in self.bridge.compat.state_components()],
            self.bridge.compat.output_components(),
            bridge_js.DELIVER_JS,
        )
        self._inject_script()
        return result

    def on_model_change(self, *args: typing.Any, **kwargs: typing.Any) -> typing.Any:
        # Section 14.6: useful for dropping model-derived caches, never
        # sufficient to know what the user has selected. The authoritative read
        # happens in the event, on demand, per page.
        self.bridge.forget()
        return _super_call(self, "on_model_change", *args, **kwargs)

    # -- wiring ---------------------------------------------------------------

    def _make_handler(self) -> typing.Callable[..., typing.Any]:
        bridge = self.bridge

        def handler(*values: typing.Any, request: typing.Any = None) -> typing.List[typing.Any]:
            raw = values[0] if values else ""
            session_hash = getattr(request, "session_hash", None)
            ack, applied = bridge.handle(raw, values[1:], session_hash)
            return bridge.outputs(ack, applied)

        # Gradio decides to inject its request object by looking at the
        # annotation, and this module postpones annotations, so the real class
        # is attached here instead of written in the signature. Without it the
        # handler has no session hash and every request is refused - which is
        # the safe direction, but not the working one.
        if gr is not None:
            handler.__annotations__["request"] = gr.Request
        return handler

    def _inject_script(self) -> None:
        script = bridge_js.document_script(_theme_css())
        injector = getattr(self, "add_custom_js", None)
        if not callable(injector):
            _note("this WanGP has no add_custom_js; the bridge cannot reach the browser")
            return
        try:
            injector(script)
        except Exception as error:
            _note(f"add_custom_js refused the bridge script: {error!r}")


# ----------------------------------------------------------------- helpers --


def _super_call(instance: typing.Any, name: str, *args: typing.Any, **kwargs: typing.Any) -> typing.Any:
    """Let the base class have its hook, whatever it does with it."""
    method = getattr(super(MiniPaintBridgePlugin, instance), name, None)
    if not callable(method):
        return None
    try:
        return method(*args, **kwargs)
    except TypeError:
        return None


def _parse(raw: typing.Any) -> dict:
    """The request as an object, or an empty one. Never an exception."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    if len(raw.encode("utf-8", "ignore")) >= protocol.MAX_ENVELOPE_BYTES:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _token(value: typing.Any, limit: int = 64) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(character for character in value if character.isalnum() or character in "._:-")[:limit]


def _note(text: str) -> None:
    """One line on WanGP's console. Never a path, never a digest, never an id."""
    try:
        print(f"[{PLUGIN_NAME}] {text}")
    except Exception:
        pass


#: What a plugin loader looks for, under the several names loaders use.
PLUGIN_CLASS = MiniPaintBridgePlugin
Plugin = MiniPaintBridgePlugin


def create(*args: typing.Any, **kwargs: typing.Any) -> MiniPaintBridgePlugin:
    return MiniPaintBridgePlugin(*args, **kwargs)
