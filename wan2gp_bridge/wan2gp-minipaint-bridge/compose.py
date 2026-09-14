"""The settings a queued job runs at, read without a browser.

WHY THIS IS SERVER-SIDE, WHEN THE OBVIOUS SEAM IS NOT.

"Add to Queue" means *this, with this prompt and these pictures* - the
resolution, steps, guidance, LoRAs, preset and profile the user set up in
their own WanGP tab, not factory defaults. So the base of a job is the
settings that exist for the model, and the tempting way to get them is to
ask the live Gradio session for the page the user is looking at.

That cannot be the mechanism, for a reason that is not about likelihood:

*   A queued job may be admitted with the WanGP child STOPPED. Cold start is
    a server-side stage that happens *after* admission, so at the moment a
    job is composed there may be no process, no Gradio app and no session at
    all. On a fresh Forge, a fresh page, or a phone that has only ever
    opened Clipboard, there is nothing to ask.

*   A server-side caller that asks anyway gets a different session, and a
    fresh session's dict has no stored settings for the model. The getter
    then returns None, and the variant that produces the canonical shape
    assigns a key on that None and raises.

*   Reading mutates. That path takes the session's stored settings *by
    reference*, sets a key on them, and hands them to a normaliser that pops
    keys and rewrites the LoRA list. Composing would change what the user's
    own tab generates next, which is a side effect nobody asked for and
    nobody would attribute to pressing Add to Queue.

Wan2GP already keeps exactly what is wanted, process-wide: ``save_inputs``
records a snapshot of the committed form per model beside the session copy,
shared across sessions for the same reason the generation record is. It is
not factory defaults - it is what the user last committed for that model -
and reading it needs no session, mutates nothing, and works when no page has
ever been open.

So the order is: the process-wide recorded form, then the live session when
one happens to exist and was asked for, then factory defaults. The third is
a real fallback and is never silent: the job records that its base came from
there and the queue says so, because a job that quietly ran at settings
nobody chose is the failure this whole file exists to prevent.
"""

from __future__ import annotations

import copy
import typing

try:
    from . import compatibility, protocol
except ImportError:  # pragma: no cover - depends on how WanGP imports plugins
    import compatibility  # type: ignore[no-redef]
    import protocol  # type: ignore[no-redef]


class ComposeError(Exception):
    """A refusal with a control-plane code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = str(detail or "")
        super().__init__(f"{code}: {self.detail}" if self.detail else code)


class Composer:
    """One per child. Reads; never writes anything a user can see."""

    def __init__(
        self,
        compat: "compatibility.Compatibility",
        note: typing.Optional[typing.Callable[[str], None]] = None,
    ) -> None:
        self.compat = compat
        self._note = note or (lambda _text: None)

    def available(self) -> bool:
        """Whether a base can be composed at all on this build."""
        if self.compat.service() is not None:
            return True
        return callable(self.compat.host.read_global("get_default_settings")) or callable(
            self.compat.host.read_global("get_factory_settings")
        )

    def compose(self, model_type: str = "", session_hash: str = "") -> dict:
        """The base for one model, and where it came from.

        ``model_type`` empty means the model this child is on, which is the
        honest answer for a job that named no model. A model this WanGP does
        not have is a refusal rather than a substitution: running a job
        against a different model than the one it was composed for is a
        different generation, and nobody pressed a button for that one.
        """
        service = self.compat.service()
        chosen = str(model_type or "").strip() or self.compat.current_model_type(service)
        if not chosen:
            raise ComposeError(protocol.COMPOSE_UNAVAILABLE, "this WanGP is on no model and the job named none")
        self._require_model(chosen)

        settings, source = self._base(service, chosen, session_hash)
        if not settings:
            raise ComposeError(protocol.COMPOSE_UNAVAILABLE, f"no settings could be read for {chosen[:60]}")

        # The base is the user's configuration and is carried through
        # untouched except for two things that are never theirs to carry: the
        # composing page's correlation id, which the executor replaces with
        # the job's own, and the model, which the job named.
        settings = copy.deepcopy(settings)
        settings["model_type"] = chosen
        settings.pop(self.compat.settings_key(compatibility.CLIENT_ID) or "client_id", None)
        # A Gradio state object would be a session handle crossing a process
        # boundary. Nothing composed here may carry one.
        settings.pop("state", None)

        if source == protocol.BASE_FACTORY:
            self._note(
                f"compose {chosen[:40]}: no committed form for this model; composed from factory defaults. "
                "The job records it and the queue says so."
            )
        return {
            "ok": True,
            "settings": settings,
            "source": source,
            "model_type": chosen,
            "wan2gp_version": self.compat.wan2gp_version,
            "residency_key": self.compat.residency_key(settings, chosen),
            "model": self._model_block(chosen),
        }

    # -- the three sources ---------------------------------------------------

    def _base(self, service: typing.Any, model_type: str, session_hash: str) -> typing.Tuple[typing.Optional[dict], str]:
        recorded = self.compat.recorded_form(service, model_type)
        if recorded:
            return recorded, protocol.BASE_RECORDED
        live = self._session_form(model_type, session_hash) if session_hash else None
        if live:
            return live, protocol.BASE_SESSION
        factory = self.compat.factory_settings(model_type)
        if factory:
            return factory, protocol.BASE_FACTORY
        return None, ""

    def _session_form(self, model_type: str, session_hash: str) -> typing.Optional[dict]:
        """One named page's own settings, as an optimisation and never a rule.

        Only reached when a caller supplied a session and the process-wide
        record had nothing, and deep-copied the moment it is read because the
        seam behind it hands back the session's own dict by reference. This
        path is the one that raises on a session with no stored settings, so
        every failure here is an absence rather than an error.
        """
        getter = self.compat.host.read_global("get_model_settings")
        state = self._state_for(session_hash)
        if not callable(getter) or state is None:
            return None
        try:
            found = getter(state, model_type)
        except Exception:
            return None
        return copy.deepcopy(found) if isinstance(found, dict) and found else None

    def _state_for(self, session_hash: str) -> typing.Any:
        """The Gradio state of a named page, when this build publishes one.

        Absent on every build seen so far, which is why the session path is
        an optimisation. It is written as a lookup rather than a live event
        because a compose runs on a control-plane thread with no request.
        """
        registry = self.compat.host.read_global("session_states")
        if isinstance(registry, dict):
            found = registry.get(session_hash)
            return found if isinstance(found, dict) else None
        return None

    # -- validation ----------------------------------------------------------

    def _require_model(self, model_type: str) -> None:
        getter = self.compat.host.read_global("get_model_def")
        if not callable(getter):
            return
        try:
            definition = getter(model_type)
        except Exception:
            return
        if definition is None or definition is False:
            raise ComposeError(protocol.MODEL_UNAVAILABLE, f"{model_type[:60]} is not a model this WanGP has")

    def _model_block(self, model_type: str) -> dict:
        """The four short strings a job records its target by."""
        block = {"type": model_type, "label": "", "family": "", "architecture": ""}
        getter = self.compat.host.read_global("get_model_def")
        if callable(getter):
            try:
                definition = getter(model_type)
            except Exception:
                definition = None
            if isinstance(definition, dict):
                block["label"] = str(definition.get("name") or definition.get("label") or "")[:120]
                block["family"] = str(definition.get("family") or definition.get("group") or "")[:120]
        base = self.compat.host.read_global("get_base_model_type")
        if callable(base):
            try:
                block["architecture"] = str(base(model_type) or "")[:120]
            except Exception:
                pass
        return protocol.model_block(block)


__all__ = ["ComposeError", "Composer"]
