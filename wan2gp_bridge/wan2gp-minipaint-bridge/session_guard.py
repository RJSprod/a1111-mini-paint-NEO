"""Keeping a page's Gradio session alive across a dropped heartbeat.

WHAT GRADIO DOES, AND WHY IT FREEZES AN EMBEDDED PAGE.

Every Gradio page keeps one ``/heartbeat/<session>`` stream open. Gradio 5
marks the session closed the moment that stream ends, for any reason, and
nothing marks it open again when the browser's EventSource reconnects a
second later. A background task then deletes the closed session's
``gr.State`` values as soon as they are more than an hour old
(``SessionState.STATE_TTL_WHEN_CLOSED``). WanGP writes its ``state`` at page
load, so a page older than an hour loses it at once. Every WanGP handler is
then handed a fresh copy of the build-time state: its ``gen`` is not the
shared generation record, the queue and the progress go blind, handlers fail
on keys that are not there, presses do nothing - while this bridge's own
events, which never touch that state, keep answering. A reload is a new
session, and shows every job that ran meanwhile.

Standalone WanGP's heartbeat is a localhost connection that never drops.
Embedded in Forge it rides Forge's HTTP/2 connection through Hypercorn and
the extension's proxy, and that connection has stalled before.

WHAT THIS DOES.

Two small things, on Gradio's own objects, installed once from the first
request that carries a ``gr.Request`` (the only place a plugin can reach
the app from):

* The heartbeat route is wrapped. A new connection for a session that is
  marked closed reopens it, and every beat that goes out is remembered as
  the moment the page was last heard.
* ``state_holder.delete_state`` is wrapped. An expiry-only deletion for a
  session heard within ``GRACE_SECONDS`` is skipped: a page heard a minute
  ago is not a page that closed its tab. A page that really went away stops
  beating, and after the grace Gradio's own rule applies unchanged.

And one thing on this bridge's own state: every hello writes a marker into
the page's live ``state``. A later request from a page whose heartbeat has
dropped and whose state lacks the marker is a page whose state was reset.
That is reported to the parent under ``session`` in every acknowledgement
and written to the console once, so the tab can say so and reload the view
instead of leaving a dead page on screen. Nothing here reads, writes or
keeps anything but session hashes, moments and one marker string.
"""

from __future__ import annotations

import time
import typing

#: The key this bridge writes into the page's live ``state`` on every hello.
#: WanGP keeps arbitrary keys in that dict and serialises none of them, so a
#: string under an unmistakable name costs nothing and collides with nothing.
MARKER_KEY = "_minipaint_bridge_session"

#: How long after its last beat a session is still taken to be alive when
#: Gradio would expire it. Long enough for a stalled connection to be noticed
#: and rebuilt, short enough that a tab really closed is not kept for hours.
GRACE_SECONDS = 15 * 60

#: The heartbeat route, by its shape: Gradio 5 mounts it under its API
#: prefix and older builds at the root, and the path parameter is the same.
HEARTBEAT_SUFFIX = "/heartbeat/{session_hash}"

#: Sessions remembered at most; the oldest are forgotten past this. A page
#: is one entry, so this is thousands of page loads.
REMEMBERED_MAX = 2000


class SessionGuard:
    """One per bridge. Everything it knows fits in four dicts."""

    def __init__(self, note: typing.Callable[[str], None], clock: typing.Callable[[], float] = time.monotonic) -> None:
        self._note = note
        self._clock = clock
        #: session hash -> the bridge session the marker was written with.
        self._marked: typing.Dict[str, str] = {}
        #: session hash -> when its heartbeat last sent a beat.
        self._heard: typing.Dict[str, float] = {}
        #: session hash -> when its heartbeat stream last ended.
        self._dropped: typing.Dict[str, float] = {}
        #: sessions found reset; reported once, and reported on every answer
        #: until the page is a new session.
        self._reset: typing.Set[str] = set()
        self._reported: typing.Set[str] = set()
        self.installed = False
        self.install_note = ""
        self.reopened = 0
        self.skipped_expiries = 0

    # -- installing on Gradio's app -------------------------------------------

    def install(self, app: typing.Any) -> bool:
        """Wrap the heartbeat route and the expiry on ``app``, once.

        Duck-typed on purpose: what is needed is ``app.state_holder`` with a
        ``session_data`` mapping and a ``delete_state`` method, and
        ``app.router.routes`` with a route whose path ends in
        ``HEARTBEAT_SUFFIX`` and whose ``app`` is the ASGI callable Starlette
        hands a matched request to. A Gradio that shapes these differently
        gets no guard and one line saying so; nothing else changes.
        """
        if self.installed or app is None:
            return self.installed
        holder = getattr(app, "state_holder", None)
        routes = getattr(getattr(app, "router", None), "routes", None)
        if holder is None or not hasattr(holder, "session_data") or not callable(getattr(holder, "delete_state", None)):
            self.install_note = "no state holder on the app"
            return False
        route = None
        for candidate in routes or ():
            path = getattr(candidate, "path", "")
            if isinstance(path, str) and path.endswith(HEARTBEAT_SUFFIX) and callable(getattr(candidate, "app", None)):
                route = candidate
                break
        if route is None:
            self.install_note = "no heartbeat route on the app"
            return False

        guard = self
        inner = route.app

        async def heartbeat(scope: typing.Any, receive: typing.Any, send: typing.Any) -> None:
            params = scope.get("path_params") if isinstance(scope, dict) else None
            session_hash = str((params or {}).get("session_hash") or "")
            guard.connected(holder, session_hash)

            async def send_heard(message: typing.Any) -> None:
                if isinstance(message, dict) and message.get("type") == "http.response.body":
                    guard.beat(session_hash)
                await send(message)

            try:
                await inner(scope, receive, send_heard)
            finally:
                guard.dropped(session_hash)

        original = holder.delete_state

        def delete_state(session_id: typing.Any, expired_only: bool = False) -> typing.Any:
            if expired_only and guard.recently_heard(session_id):
                guard.skipped_expiries += 1
                return None
            return original(session_id, expired_only)

        route.app = heartbeat
        holder.delete_state = delete_state
        self.installed = True
        self.install_note = "installed"
        self._note(
            "session guard: Gradio's heartbeat route is wrapped - a session heard within the last "
            f"{GRACE_SECONDS // 60} minutes is never expired, and one whose stream reconnects is reopened"
        )
        return True

    # -- what the heartbeat tells it ---------------------------------------------

    def connected(self, holder: typing.Any, session_hash: str) -> bool:
        """A heartbeat stream opened for this session. Returns whether it reopened one."""
        if not session_hash:
            return False
        now = self._clock()
        self._heard[session_hash] = now
        self._prune()
        session = self._session(holder, session_hash)
        if session is None or not getattr(session, "is_closed", False):
            return False
        try:
            session.is_closed = False
        except Exception:
            return False
        self.reopened += 1
        gap = now - self._dropped.get(session_hash, now)
        self._note(
            f"session: a page's heartbeat reconnected after {gap:.0f}s and its session is reopened "
            "(Gradio had marked it closed, which deletes its state an hour on)"
        )
        return True

    def beat(self, session_hash: str) -> None:
        if session_hash:
            self._heard[session_hash] = self._clock()

    def dropped(self, session_hash: str) -> None:
        if session_hash:
            self._dropped[session_hash] = self._clock()

    def recently_heard(self, session_hash: typing.Any) -> bool:
        heard = self._heard.get(session_hash) if isinstance(session_hash, str) else None
        return heard is not None and self._clock() - heard < GRACE_SECONDS

    # -- what a request tells it ---------------------------------------------------

    def observe(
        self,
        session_hash: typing.Any,
        state: typing.Any,
        operation: str,
        bridge_session: str,
        holder: typing.Any = None,
    ) -> dict:
        """The session as this request finds it: ``{closed, reset, silent_s}``.

        ``closed`` is Gradio's own flag, read and never written here.
        ``reset`` is this bridge's finding: the page's live state lacks the
        marker a hello wrote into it, and its heartbeat is known to have
        dropped (or Gradio still holds it closed) - the one combination that
        means the state was deleted rather than replaced. A marker missing
        with a heartbeat that never dropped is WanGP handing the page a new
        state object, which is its business: re-marked, and not a reset.
        Once found reset a session stays reported so, on every answer,
        until the page is a new session.
        """
        sh = session_hash if isinstance(session_hash, str) else ""
        session = self._session(holder, sh) if sh else None
        closed = bool(getattr(session, "is_closed", False)) if session is not None else False
        heard = self._heard.get(sh)
        silent = int(self._clock() - heard) if heard is not None else None
        report: typing.Dict[str, typing.Any] = {"closed": closed, "reset": sh in self._reset, "silent_s": silent}
        if not sh or not isinstance(state, dict):
            return report

        expected = self._marked.get(sh)
        found = state.get(MARKER_KEY)
        if expected is not None and found != expected and sh not in self._reset:
            if sh in self._dropped or closed:
                self._reset.add(sh)
                report["reset"] = True
                if sh not in self._reported:
                    self._reported.add(sh)
                    since = f"last heard {silent}s ago" if silent is not None else "its heartbeat had dropped"
                    self._note(
                        f"session: a page's session state was reset by Gradio ({since}; the state was deleted "
                        "after the heartbeat drop) - the page needs a reload, and the parent is told"
                    )
            else:
                self._note("session: a page's state is a new object with no marker and its heartbeat never dropped; marked again")
                try:
                    state[MARKER_KEY] = bridge_session
                except Exception:
                    return report
        if operation == "hello" or expected is None:
            try:
                state[MARKER_KEY] = bridge_session
            except Exception:
                return report
            self._marked[sh] = bridge_session
        return report

    # -- housekeeping ------------------------------------------------------------

    @staticmethod
    def _session(holder: typing.Any, session_hash: str) -> typing.Any:
        data = getattr(holder, "session_data", None)
        try:
            return data.get(session_hash) if data is not None and session_hash else None
        except Exception:
            return None

    def _prune(self) -> None:
        if len(self._heard) <= REMEMBERED_MAX:
            return
        for stale in sorted(self._heard, key=self._heard.get)[: len(self._heard) - REMEMBERED_MAX]:
            for table in (self._heard, self._dropped, self._marked):
                table.pop(stale, None)
            self._reset.discard(stale)
            self._reported.discard(stale)

    def snapshot(self) -> dict:
        """For a diagnostic report: counts, never hashes."""
        return {
            "installed": self.installed,
            "install_note": self.install_note,
            "known": len(self._heard),
            "reopened": self.reopened,
            "reset": len(self._reset),
            "skipped_expiries": self.skipped_expiries,
        }
