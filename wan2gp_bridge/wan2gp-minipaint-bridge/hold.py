"""Holding WanGP between tasks, at Forge's request: the bridge's half of the card lease.

UNTIL 1.12.0 THIS PLUGIN NEVER STOPPED WANGP DOING ANYTHING. It still never
starts a second run, never aborts one and never presses Generate. What it can
now do, and only when Forge asks over the authenticated control plane, is
HOLD WanGP between two tasks, so that another extension in Forge's process can
use the card for a while - a text-to-speech model of eighteen to twenty
gigabytes, on the card WanGP renders on - and then let it go.

A hold is two of WanGP's own mechanisms, and nothing of this plugin's:

*   **WanGP's between-task pause flag.** Its worker loop checks the flag at the
    top of every iteration - after a task has finished and been removed, before
    the next is taken - and sleeps while it is set. So the task that is
    running finishes exactly as it would have; the next one does not start.
    WanGP's own edit feature sets the same flag while a queued task is edited
    and clears it when the edit is saved or cancelled, so a hold RE-ASSERTS it
    every tenth of a second rather than setting it once.

*   **The idle claim on WanGP's GPU lock**, taken only while no generation
    worker exists. A run started afterwards - Generate pressed in the WanGP tab,
    or another of WanGP's own GPU processes - waits for the claim, and WanGP
    tells its user so ("Media generation is waiting for <name> to release GPU
    resources"). The claim is never asked for while a worker exists, so it can
    never suspend a run: a run already going is held by the flag alone.

It says ``holding`` while a task is still running on the card (or another of
WanGP's GPU processes has it) and ``held`` once nothing is running and nothing
will start. It lets go by itself when Forge stops renewing it - ``ttl_s``
after the last hold call - so a Forge that died cannot leave WanGP held; and
``resume`` undoes what it did in reverse order: the claim first, then the
flag, and the flag only if the hold set it (an edit that had it set before
keeps it).

THE ONE WINDOW. Between WanGP's edit clearing the flag and the hold setting it
again - at most a tenth of a second - WanGP's worker, which looks at the flag
every half second, can find it clear and take its next task. That task then
runs, and the hold says ``holding`` again until it has finished; Forge's lease
reports that, and the lease's owner reads it. Nothing in WanGP's plugin API
closes this: there is no hook before a task, only this flag.

A FLUSH moves WanGP's weights off the card while it is held: ``soft`` keeps
them in RAM, ``hard`` releases the model so its next task reloads it. Refused
while a task runs, and - hard only - while a run is paused between tasks or
when WanGP would never get a released model back. What each of them touches
is in ``compatibility.py``, which remains the one file here that knows a
WanGP internal.

Nothing here may raise into WanGP. The thread is a daemon that exists only
while a hold does.
"""

from __future__ import annotations

import threading
import time
import typing

try:
    from . import compatibility, protocol
except ImportError:  # pragma: no cover - depends on how WanGP imports plugins
    import compatibility  # type: ignore[no-redef]
    import protocol  # type: ignore[no-redef]


#: How often a hold looks at WanGP: re-asserts the flag, takes the claim once
#: the worker has gone, lets go when not renewed. A tenth of a second against
#: the worker's own half-second look at the flag is what keeps the window above
#: small; the work per look is a few dictionary reads.
REASSERT_SECONDS = 0.1
#: How often a refused claim is asked for again.
CLAIM_RETRY_SECONDS = 0.5
#: The timer a hold runs on when Forge sends none it can use. Forge sends one.
DEFAULT_TTL_SECONDS = 45.0


class HoldError(Exception):
    """A refusal with a control-plane code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = str(detail or "")
        super().__init__(f"{code}: {self.detail}" if self.detail else code)


class Holder:
    """One per child: whether WanGP is held, for which lease, and what it changed."""

    def __init__(
        self,
        compat: "compatibility.Compatibility",
        note: typing.Optional[typing.Callable[[str], None]] = None,
        clock: typing.Callable[[], float] = time.monotonic,
        threaded: bool = True,
    ) -> None:
        self.compat = compat
        self._note = note or (lambda _text: None)
        self._clock = clock
        self._threaded = threaded
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._thread: typing.Optional[threading.Thread] = None
        self._said = ""
        self._forget()

    def _forget(self) -> None:
        self._lease = ""
        self._ttl = DEFAULT_TTL_SECONDS
        self._deadline = 0.0
        self._label = ""
        #: Whether this hold set the pause flag, and whether WanGP's own edit
        #: had it set already when the hold began (and still has).
        self._flag_set = False
        self._flag_borrowed = False
        self._claim = protocol.CLAIM_NONE
        self._busy_with = ""
        self._claim_next = 0.0
        self._flushing = False
        self._unsupported = ""
        self._cleared = 0

    @property
    def lease(self) -> str:
        return self._lease

    # -- the operations ------------------------------------------------------

    def hold(self, lease: str, ttl_s: float, label: str = "") -> dict:
        """Hold WanGP for ``lease``, or renew the hold. Answers where it stands.

        A different lease takes the hold over rather than being refused: only
        Forge can reach this, Forge has one lease at a time, and a new one
        while another is held means the old one ended without its resume
        arriving - letting go in between would be the gap a task slips into.
        """
        with self._lock:
            now = self._clock()
            if self._lease and self._lease != lease:
                self._note("hold: a new lease from Forge takes the hold over")
            fresh = not self._lease
            self._lease = lease
            self._ttl = max(protocol.HOLD_TTL_MIN_SECONDS, min(protocol.HOLD_TTL_MAX_SECONDS, float(ttl_s)))
            self._deadline = now + self._ttl
            if label:
                self._label = label
            if fresh:
                self._begin()
            view = self._tick(now)
            answer = self._status(now, view)
        self._ensure_thread()
        return answer

    def resume(self, lease: str) -> dict:
        """Let WanGP go. A lease this bridge is not holding for changes nothing."""
        with self._lock:
            now = self._clock()
            if not self._lease or lease != self._lease:
                answer = self._status(now)
                answer["resumed"] = False
                return answer
            self._let_go("resumed at Forge's request")
            answer = self._status(now)
            answer["resumed"] = True
            return answer

    def flush(self, lease: str, level: str) -> dict:
        """Move WanGP's weights off the card while it is held. Also renews the hold.

        The work runs outside the lock, so the hold's own look keeps
        re-asserting the flag while a large model is being moved, and the
        timer is not allowed to run out underneath it.
        """
        with self._lock:
            now = self._clock()
            if not self._lease or lease != self._lease:
                raise HoldError(compatibility.HOLD_NOT_HELD, "no hold for that lease")
            self._deadline = now + self._ttl
            _service, gen, worker = self.compat.hold_view()
            if gen is None or self._unsupported:
                raise HoldError(compatibility.HOLD_UNSUPPORTED, self._unsupported or "WanGP's shared record is out of reach")
            running, _client = self.compat.task_on_card(gen, worker)
            if running is not False:
                raise HoldError(compatibility.HOLD_TASK_RUNNING, "a WanGP task is running, or cannot be proved not to be")
            if level == protocol.FLUSH_HARD:
                if worker is not False:
                    # WanGP's own Unload refuses while any GPU process runs,
                    # a run paused between tasks included; so does this.
                    raise HoldError(compatibility.HOLD_TASK_RUNNING, "a WanGP run is paused between tasks")
                code, why = self.compat.hard_flush_blocker()
                if code:
                    raise HoldError(code, why)
            self._flushing = True
        started = time.monotonic()
        parts: typing.List[str] = []
        try:
            parts = list(self.compat.flush_soft(gen))
            if level == protocol.FLUSH_HARD:
                parts.extend(self.compat.flush_hard())
        finally:
            with self._lock:
                self._flushing = False
                if self._lease == lease:
                    self._deadline = self._clock() + self._ttl
        took = int((time.monotonic() - started) * 1000)
        self._note(f"flush {level}: done in {took} ms ({', '.join(parts) or 'nothing to move'})")
        with self._lock:
            answer = self._status(self._clock())
        answer["flushed"] = level
        answer["parts"] = parts
        return answer

    def status(self) -> dict:
        """Where the hold stands and what WanGP is doing, for hello."""
        with self._lock:
            return self._status(self._clock())

    def tick(self) -> None:
        """One look: what the thread does every ``REASSERT_SECONDS``. Public for tests."""
        with self._lock:
            self._tick(self._clock())

    def shutdown(self) -> None:
        """The control surface is closing: let WanGP go, if it was held."""
        with self._lock:
            if self._lease:
                self._let_go("the control surface is closing")
        self._wake.set()

    # -- the hold itself -----------------------------------------------------

    def _begin(self) -> None:
        """Set the flag, remembering whether WanGP's own edit had it set already."""
        _service, gen, _worker = self.compat.hold_view()
        if gen is None:
            self._unsupported = "WanGP's shared generation record could not be reached"
            self._note(f"hold: cannot hold - {self._unsupported}")
            return
        self._unsupported = ""
        self._flag_borrowed = self.compat.pause_flag(gen)
        self.compat.set_pause_flag(gen, True)
        self._flag_set = True
        self._note(f"hold: asked by Forge; WanGP stops between tasks until it is let go "
                   f"(or not renewed within {self._ttl:.0f}s)")

    def _tick(self, now: float) -> typing.Optional[tuple]:
        """Re-assert the flag, take the claim once no worker exists, expire. Lock held."""
        if not self._lease:
            return None
        if now > self._deadline and not self._flushing:
            self._let_go(f"not renewed within {self._ttl:.0f}s; WanGP resumes by itself")
            return None
        view = self.compat.hold_view()
        _service, gen, worker = view
        if gen is None:
            if not self._unsupported:
                self._unsupported = "WanGP's shared generation record could not be reached"
                self._note(f"hold: {self._unsupported}")
            return view
        if self._unsupported or not self._flag_set:
            self._begin()
        elif not self.compat.pause_flag(gen):
            # WanGP's edit feature cleared it - an edit ended, saved or
            # cancelled. Whatever edit had it before is over now, so the flag
            # is this hold's to clear when it lets go.
            self._flag_borrowed = False
            self._cleared += 1
            if self._cleared == 1 or self._cleared % 50 == 0:
                self._note(f"hold: WanGP cleared its pause flag (an edit ended); set again ({self._cleared} time(s))")
            self.compat.set_pause_flag(gen, True)
        if self._claim == protocol.CLAIM_TAKEN and not self.compat.gpu_claimed(gen):
            self._note("hold: WanGP's GPU lock no longer names the hold; claiming it again when it is idle")
            self._claim = protocol.CLAIM_NONE
        if (self._claim != protocol.CLAIM_TAKEN and self._claim != protocol.CLAIM_ABSENT
                and worker is False and now >= self._claim_next):
            outcome, who = self.compat.claim_gpu(gen, self._label or compatibility.HOLD_PROCESS_NAME)
            if outcome != self._claim:
                self._note({
                    protocol.CLAIM_TAKEN: "hold: WanGP's GPU lock claimed; a run started now waits for it",
                    protocol.CLAIM_FLAG_ONLY: "hold: WanGP's GPU lock says a run holds it but no worker exists "
                                              "(a flag a crash left set); holding with the pause flag alone",
                    protocol.CLAIM_BUSY: f"hold: WanGP's GPU is in use by {who or 'another of its processes'}; "
                                         "claiming it once it is free",
                    protocol.CLAIM_ABSENT: "hold: this WanGP has no GPU lock to claim; holding with the pause flag alone",
                }.get(outcome, f"hold: claim {outcome}"))
            self._claim = outcome
            self._busy_with = who if outcome == protocol.CLAIM_BUSY else ""
            self._claim_next = now + CLAIM_RETRY_SECONDS
        return view

    def _let_go(self, why: str) -> None:
        """Undo the hold in reverse order: the claim, then the flag. Lock held."""
        _service, gen, _worker = self.compat.hold_view()
        if gen is not None:
            if self._claim == protocol.CLAIM_TAKEN:
                self.compat.release_gpu(gen)
            if self._flag_set and not self._flag_borrowed:
                self.compat.set_pause_flag(gen, False)
        self._note(f"hold: {why}")
        self._forget()
        self._said = ""

    def _status(self, now: float, view: typing.Optional[tuple] = None) -> dict:
        """The answer every operation and hello carry. Lock held."""
        _service, gen, worker = view if view is not None else self.compat.hold_view()
        running, client = self.compat.task_on_card(gen, worker)
        length = self.compat.queue_length(gen)
        free, total = self.compat.vram()
        if not self._lease:
            word = protocol.HOLD_NONE
        elif self._unsupported or gen is None:
            word = protocol.HOLD_UNSUPPORTED
        elif running is not False or self._claim == protocol.CLAIM_BUSY:
            word = protocol.HOLD_HOLDING
        else:
            word = protocol.HOLD_HELD
        if not self._lease:
            waiter: typing.Optional[bool] = False
        elif worker is None or running is None:
            waiter = None
        else:
            # A run that exists and is not running a task, with tasks in its
            # queue, is waiting for the hold: paused at the flag, or stopped
            # at the claim. One ending its run has an empty queue.
            waiter = bool(worker) and running is False and bool(length)
        if self._lease and word != self._said:
            self._said = word
            self._note({
                protocol.HOLD_HELD: "hold: WanGP is held - nothing is running on its card and nothing will start",
                protocol.HOLD_HOLDING: "hold: a task is still running on WanGP's card; WanGP stops after it",
                protocol.HOLD_UNSUPPORTED: "hold: this WanGP cannot be held",
            }.get(word, f"hold: {word}"))
        return {
            "ok": True,
            "lease": self._lease,
            "hold": word,
            "worker": worker,
            "task_running": running,
            "active_client_id": client if protocol.valid_execution_id(client) else "",
            "queue_length": length,
            "waiter": waiter,
            "vram_free": free,
            "vram_total": total,
            "claim": self._claim if self._lease else protocol.CLAIM_NONE,
            "busy_with": self._busy_with if self._claim == protocol.CLAIM_BUSY else "",
            "expires_in_s": round(max(0.0, self._deadline - now), 1) if self._lease else None,
            "detail": self._unsupported,
        }

    # -- the thread ----------------------------------------------------------

    def _ensure_thread(self) -> None:
        if not self._threaded:
            return
        with self._lock:
            thread = self._thread
            if thread is not None and thread.is_alive():
                return
            thread = threading.Thread(target=self._run, name="minipaint-bridge-hold", daemon=True)
            self._thread = thread
        thread.start()

    def _run(self) -> None:
        stumbled = False
        try:
            while True:
                self._wake.wait(REASSERT_SECONDS)
                self._wake.clear()
                with self._lock:
                    if not self._lease:
                        return
                    try:
                        self._tick(self._clock())
                    except Exception as error:  # pragma: no cover - a look that throws is a note
                        if not stumbled:
                            stumbled = True
                            self._note(f"hold: a look at WanGP failed ({type(error).__name__}); it keeps looking")
        finally:
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None


__all__ = ["CLAIM_RETRY_SECONDS", "DEFAULT_TTL_SECONDS", "REASSERT_SECONDS", "HoldError", "Holder"]
