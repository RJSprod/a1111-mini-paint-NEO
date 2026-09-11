"""The plugin WanGP loads, and the one event everything happens on.

The shape is deliberately small. During ``setup_ui`` the bridge asks for the
components and globals it needs and hands WanGP the document script. During
``post_ui_setup`` it records what it actually got, builds one adapter per
receiver it can serve, and asks WanGP - through ``insert_after`` - to place
its three invisible controls inside the form it was handed, wiring a single
Gradio event whose outputs are the acknowledgement plus every v1 receiver
component as it does so. After that it does nothing at all until a browser
asks it something.

The controls are placed by WanGP rather than built by the plugin because a
component built anywhere else is on no page. ``setup_ui`` runs before WanGP's
Blocks exist, and a Gradio component created outside a Blocks context has an
id, can be named in an event, and is still absent from the page config: the
browser never finds it, and the bridge never answers. That was the silence.

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

import dataclasses
import hashlib
import json
import pathlib
import time
import typing

try:
    from . import bridge_js, bridge_ui, compatibility, handoff, protocol, receiver_adapters, receiver_state
    from . import scrub
except ImportError:  # pragma: no cover - depends on how WanGP imports plugins
    import bridge_js  # type: ignore[no-redef]
    import bridge_ui  # type: ignore[no-redef]
    import compatibility  # type: ignore[no-redef]
    import handoff  # type: ignore[no-redef]
    import protocol  # type: ignore[no-redef]
    import receiver_adapters  # type: ignore[no-redef]
    import receiver_state  # type: ignore[no-redef]
    import scrub  # type: ignore[no-redef]

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
        #: The selector components after the receivers in the event's outputs,
        #: in ``compatibility.switch_components`` order.
        self.switch_keys: typing.Tuple[str, ...] = ()
        self.environ = environ

    # -- lifecycle -----------------------------------------------------------

    def declare(self) -> None:
        self.compat.declare()

    def resolve(self) -> "compatibility.Resolution":
        resolution = self.compat.resolve()
        self.adapters = receiver_adapters.build_adapters(self.compat)
        self.state_keys = tuple(key for key, _ in self.compat.state_components())
        self.receiver_keys = tuple(self.compat.output_receivers())
        self.switch_keys = tuple(key for key, _ in self.compat.switch_components())
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
        must be one this bridge is offering - switched on, or allowed by the
        model and switchable, in which case this call switches it - the file
        is validated
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
        receiver_state.require_offered(state, receiver_id)

        loaded = handoff.load(request.get("handoff_id"), request.get("source"), self.environ)
        applied = adapter.apply(loaded, adapter.operation, state)
        # A receiver the model allows but the page has not selected is
        # switched on in this same event - the Location radio, the End
        # Image(s) checkbox or the reference dropdown, plus the letter string
        # generation reads. Empty when it already was on.
        switch = self.compat.switch_for(receiver_id, state.values)
        if switch:
            applied = dataclasses.replace(
                applied,
                switch_updates=dict(switch.updates),
                chained=tuple(applied.chained) + tuple(switch.updates),
            )
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
            "switched": switch.token,
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
        # The letter strings a switch rewrote are part of the fingerprint too.
        for key in (compatibility.IMAGE_PROMPT_TYPE, compatibility.VIDEO_PROMPT_TYPE):
            if key in applied.switch_updates:
                values[key] = applied.switch_updates[key]
        return receiver_state.build(values, state.model, state.capabilities, state.view, allowances=state.allowances)

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
        started = time.perf_counter()
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
            _note(f"{operation}: {error.code}: {error.detail}")
        except Exception as error:  # pragma: no cover - the last line of defence
            ack.update({"ok": False, "ready": False, "code": compatibility.INTERNAL_ERROR})
            _note(f"{operation}: unexpected: {error!r}")
        else:
            # One line per request that went through, so the log outside
            # WanGP can tell "the event ran and answered" from "the click
            # never reached us" - the two look identical from the Send menu.
            _note(_summary(operation, ack, time.perf_counter() - started))

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
        # Then the selector components, in the same order they were wired;
        # "no change" for each one a switch did not name.
        switched = bridge_ui.no_change(len(self.switch_keys))
        if applied is not None:
            for index, key in enumerate(self.switch_keys):
                if key in applied.switch_updates:
                    switched[index] = bridge_ui.update_for(applied.switch_updates[key])
        return [json.dumps(ack, default=str), *updates, *switched]


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
        # The globals are asked for here and not only in setup_ui: Wan2GP
        # injects them right after constructing the plugin and never again,
        # so a request made in setup_ui is a request nothing answers. Asking
        # is a list append; it commits the bridge to nothing.
        try:
            self.bridge.compat.declare_globals()
        except Exception:
            pass
        #: Whether setup_ui declared anything - False when WanGP was started
        #: by hand, and the bridge is staying out of the way.
        self.declared = False
        #: The most recently placed set of controls, and whether its event is
        #: wired. WanGP asks for one set per form it builds; ``instances``
        #: counts them so that each gets element ids of its own.
        self.controls: typing.Optional[bridge_ui.BridgeControls] = None
        self.wired = False
        self.instances = 0
        #: Whether the browser half has been handed to WanGP. Once only: the
        #: UI is built more than once on some pages.
        self.injected = False

    # -- hooks ----------------------------------------------------------------

    def setup_ui(self, *args: typing.Any, **kwargs: typing.Any) -> typing.Any:
        result = _super_call(self, "setup_ui", *args, **kwargs)
        if not compatibility.managed(self.bridge.environ):
            # WanGP started by hand. The bridge stays out of the way entirely
            # rather than adding controls nobody will ever message.
            return result
        self.bridge.declare()
        self.declared = True
        # Here, not in post_ui_setup. Everything a plugin declares - the
        # components it wants, the JavaScript it adds - is declared before the
        # main UI is built; by the time post_ui_setup runs, the Blocks exist
        # and the page's scripts are settled, so a script added then is
        # accepted without complaint and never reaches the document. That is
        # what "the bridge is loaded and silent" was.
        #
        # The controls are the other way round: they cannot be built here,
        # because nothing built before the Blocks exist is on the page. They
        # are asked for in post_ui_setup, from the page itself.
        self._inject_script()
        return result

    def post_ui_setup(self, *args: typing.Any, **kwargs: typing.Any) -> typing.Any:
        result = _super_call(self, "post_ui_setup", *args, **kwargs)
        if not getattr(self, "declared", False):
            return result

        # The components arrive here and nowhere else: WanGP resolves what was
        # asked for in setup_ui and hands the mapping to this call. Looking for
        # them on the plugin object instead found nothing, and every receiver
        # then reported itself missing however right its elem_id was.
        handed = kwargs.get("components")
        if not isinstance(handed, dict):
            handed = next((item for item in args if isinstance(item, dict)), None)
        taken = self.bridge.compat.host.accept_components(handed)
        _note(f"post_ui_setup handed {taken} component(s)")

        resolution = self.bridge.resolve()
        if not resolution.ok:
            # Fail closed and say why. The controls are still placed, so that
            # the handshake the parent gets is an answer naming
            # BRIDGE_COMPONENT_INCOMPATIBLE rather than a timeout; only the
            # receivers that resolved are ever outputs of the event, so there
            # is no path by which an unproven component could be written to.
            _note(
                f"not ready: missing {', '.join(resolution.missing_mandatory)}. "
                f"Resolved: {', '.join(sorted(resolution.elem_ids.values())) or 'nothing'}."
            )

        # Nothing this plugin does may take WanGP down with it. It already
        # has, once: a value that was not a component reached a Gradio event,
        # create_ui() raised, and WanGP restarted into safe mode with every
        # user plugin disabled - other people's plugins included. A bridge
        # that fails to place itself is a Send menu without WanGP in it, and
        # that is all it is ever allowed to be.
        try:
            self._place(handed if isinstance(handed, dict) else {})
        except Exception as error:
            self.wired = None
            _note(
                f"could not place the bridge controls ({type(error).__name__}: {error}). "
                "WanGP is unaffected; intelligent send stays off for this run."
            )
            try:
                import traceback

                # Scrubbed rather than printed: every frame of a traceback is
                # an absolute path, and on a single-user machine that is the
                # account name, printed onto a console somebody screenshots.
                print(scrub.block(traceback.format_exc()))
            except Exception:
                pass
        return result

    def _place(self, handed: typing.Mapping[str, typing.Any]) -> None:
        """Ask WanGP to put the controls on the page, wired. See ``post_ui_setup``.

        ``insert_after(target, builder)`` is WanGP's contract: after every
        plugin's ``post_ui_setup`` has run, WanGP calls the builder inside the
        container that holds ``target`` and moves the one component it built
        to sit right behind it. That is the only moment a plugin's own
        component is created inside the page, so the controls are built and
        their event attached there, in the builder, and nowhere else.

        The event's inputs and outputs are read *now*, not in the builder:
        WanGP hands each form's components to a fresh ``post_ui_setup`` and
        runs every builder afterwards, so a builder that asked the bridge
        for its components at run time would wire the first form's controls
        to the second form's values.
        """
        # WanGP answers components and globals out of one mapping. A global's
        # name handed to insert_after is a target WanGP cannot find, so only
        # real components are candidates for "the thing to sit behind".
        placeable = {name: value for name, value in handed.items() if compatibility.Host.is_component(value)}
        target = bridge_ui.target_for(placeable)
        inserter = getattr(self, "insert_after", None)
        if not target:
            _note(
                "WanGP handed over no component to place the bridge controls after; "
                "intelligent send stays off for this run"
            )
            return
        if not callable(inserter):
            _note(
                "this WanGP has no insert_after, so the bridge cannot put its controls "
                "on the page; intelligent send stays off for this run"
            )
            return

        self.instances += 1
        instance = self.instances
        handler = self._make_handler()
        inputs = [component for _, component in self.bridge.compat.state_components()]
        # The receivers first, then the selector components a send may switch
        # (``switch_components`` order) - ``MiniPaintBridge.outputs`` returns
        # one value per entry in exactly this order.
        outputs = list(self.bridge.compat.output_components())
        outputs += [component for _, component in self.bridge.compat.switch_components()]

        def build_controls() -> typing.Any:
            # Runs inside WanGP's Blocks, inside the target's own container.
            # It must add exactly one component to that container and return
            # it - WanGP moves the container's last child behind the target -
            # so the column is created first and returned whatever else
            # happens, and a wiring failure is a note, never an exception
            # that would leave WanGP's own last child where the column went.
            controls = bridge_ui.build(instance)
            if controls is None:
                raise RuntimeError("gradio is not importable inside the builder")
            self.controls = controls
            try:
                self.wired = bridge_ui.wire(controls, handler, inputs, outputs, bridge_js.DELIVER_JS)
            except Exception as error:
                self.wired = None
                _note(f"controls placed after '{target}' but their event could not be wired: {type(error).__name__}: {error}")
            else:
                _note(
                    f"controls placed after '{target}' (set {instance}); event wired with "
                    f"{len(inputs)} input(s) and {len(outputs)} receiver output(s)"
                )
            return controls.column

        inserter(target, build_controls)
        _note(f"asked WanGP to place the bridge controls after '{target}' (set {instance})")

    def on_model_change(self, *args: typing.Any, **kwargs: typing.Any) -> typing.Any:
        # Section 14.6: useful for dropping model-derived caches, never
        # sufficient to know what the user has selected. The authoritative read
        # happens in the event, on demand, per page.
        self.bridge.forget()
        return _super_call(self, "on_model_change", *args, **kwargs)

    # -- wiring ---------------------------------------------------------------

    def _make_handler(self) -> typing.Callable[..., typing.Any]:
        bridge = self.bridge

        # ``request`` first, and positional. Gradio decides which arguments
        # to fill in itself by walking the signature's *positional* parameters
        # and stopping at the first that is not one - so a request parameter
        # placed after ``*values``, keyword-only, is never even looked at,
        # the handler runs with no request, and without a session hash every
        # hello is refused with BRIDGE_SESSION_MISMATCH. That was the answer
        # a real install gave, over and over, once its controls were finally
        # on the page.
        def handler(request: typing.Any, *values: typing.Any) -> typing.List[typing.Any]:
            raw = values[0] if values else ""
            session_hash = getattr(request, "session_hash", None)
            ack, applied = bridge.handle(raw, values[1:], session_hash)
            return bridge.outputs(ack, applied)

        # Gradio recognises the parameter by its annotation, and this module
        # postpones annotations, so the real class is attached here instead of
        # written in the signature.
        if gr is not None:
            handler.__annotations__["request"] = gr.Request
        return handler

    def _inject_script(self) -> None:
        if self.injected:
            return
        script = bridge_js.document_script(_theme_css())
        for name in ("add_custom_js", "add_js", "custom_js"):
            injector = getattr(self, name, None)
            if not callable(injector):
                continue
            try:
                injector(script)
            except Exception as error:
                _note(f"{name} refused the bridge script: {type(error).__name__}: {error}")
                continue
            self.injected = True
            _note(f"browser script handed to {name} ({len(script)} characters)")
            return
        _note(
            "this WanGP exposes no way to add JavaScript (tried add_custom_js, add_js, "
            "custom_js); the bridge cannot reach the browser and intelligent send stays off"
        )


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


def _summary(operation: typing.Any, ack: typing.Mapping[str, typing.Any], seconds: float) -> str:
    """What one answered request amounted to, in receiver ids and tokens only."""
    took = f"{seconds * 1000:.0f} ms"
    if operation == "receive":
        line = f"receive: {ack.get('receiver_id')} {ack.get('operation')} {ack.get('verification')} in {took}"
        return line + (f", switched {ack['switched']}" if ack.get("switched") else "")
    parts = []
    for item in ack.get("receivers") or []:
        if not isinstance(item, dict):
            continue
        if item.get("selected"):
            state = "selected"
        elif item.get("enabled"):
            state = f"allowed ({item.get('switch') or 'switch'})"
        else:
            state = "off"
        parts.append(f"{item.get('id')} {state}")
    return f"{operation}: answered in {took} - " + (", ".join(parts) if parts else "no receiver resolved")


def _note(text: str) -> None:
    """One line on WanGP's console. Never a path, never a digest, never an id.

    Scrubbed rather than merely written carefully. Most of what reaches here is
    this module's own sentences, which carry nothing - but two of the callers
    pass the repr of an exception they did not expect, and that is exactly
    where somebody's home directory arrives on a console.
    """
    try:
        print(f"[{PLUGIN_NAME}] {scrub.line(text)}")
    except Exception:
        pass


#: What a plugin loader looks for, under the several names loaders use.
PLUGIN_CLASS = MiniPaintBridgePlugin
Plugin = MiniPaintBridgePlugin


def create(*args: typing.Any, **kwargs: typing.Any) -> MiniPaintBridgePlugin:
    return MiniPaintBridgePlugin(*args, **kwargs)
