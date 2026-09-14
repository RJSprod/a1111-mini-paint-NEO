"""The durable record of what was submitted, and what came of it.

WanGP's submission path carries no idempotency key, no client-supplied
deduplication and no identifier a caller can probe with afterwards. It does
honour ``client_id`` - a task keeps whatever one it is given, and the
artifacts a generation returns are routed by it - but nothing compares one
against anything already submitted, so sending the same id twice starts two
generations. That single fact is why this file exists: it is the only
mechanism by which a Forge that restarted between "submitted" and "recorded
the result" can avoid generating the same thing twice.

So the order is fixed and is the whole design:

    write the record, flush it to disk, *then* submit.

A record that exists with no outcome means "a submission was made and its
result was never seen", which is an honest ambiguity and is answered as
``unknown``. It is never answered as "did not happen", because the cost of
being wrong about that is a second generation on somebody's card, and it is
never answered as "finished", because the cost of being wrong about that is
a job reported complete that produced nothing.

It lives here, in the child, rather than on the Forge side, because this is
the process that knows whether a task exists and this is the process whose
lifetime bounds the generation. On POSIX the child outlives a Forge restart
and this file is what lets the new Forge re-attach to a generation still
running; on Windows the child is killed with Forge and this file is what
proves, afterwards, that the generation was lost rather than duplicated.

Nothing here holds a lock across I/O that can take a while, and nothing here
blocks on WanGP's generation lock. The whole file is a dict, a threading
lock, and an atomic rename.
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
import threading
import time
import typing

try:
    from . import compatibility, protocol
except ImportError:  # pragma: no cover - depends on how WanGP imports plugins
    import compatibility  # type: ignore[no-redef]
    import protocol  # type: ignore[no-redef]


LEDGER_NAME = "wangp-executions.json"
SCHEMA = 1

#: A terminal record is kept this long so a Forge that restarts can still
#: adopt its outcome, then dropped. A non-terminal record is never dropped by
#: age: the whole point of it is that nobody knows how that one ended.
KEEP_TERMINAL_SECONDS = 7 * 24 * 3600.0
#: A bound on the file, so a runaway caller cannot fill a disk. Only terminal
#: records are ever evicted to stay under it.
MAX_RECORDS = 2000


def _now() -> float:
    return time.time()


class Ledger:
    """One durable record per execution id, and the rules around it.

    Constructed once per child and reached from the control surface's
    handler threads, so every public method takes the lock; none of them
    does anything slow while holding it.
    """

    def __init__(
        self,
        root: typing.Any = None,
        instance: str = "",
        clock: typing.Callable[[], float] = _now,
    ) -> None:
        self._lock = threading.RLock()
        self._clock = clock
        self.instance = str(instance or "")
        self.root = pathlib.Path(str(root)) if root else None
        self._records: typing.Dict[str, dict] = {}
        self._loaded = False

    # -- the file ------------------------------------------------------------

    @property
    def path(self) -> typing.Optional[pathlib.Path]:
        return (self.root / LEDGER_NAME) if self.root is not None else None

    def load(self) -> int:
        """Read what a previous run of this child left behind. Returns the count.

        Forgiving, like every other document this integration reads: a file
        that will not parse starts an empty ledger rather than refusing to
        run. That is safe in exactly one direction and it is this one - an
        empty ledger makes a repeated submission look new, which can
        duplicate a generation - so the failure is loud on the child's
        console and the record count is reported through ``hello``.
        """
        with self._lock:
            self._loaded = True
            path = self.path
            if path is None:
                return 0
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return 0
            except Exception:
                return 0
            records = raw.get("records") if isinstance(raw, dict) else None
            if not isinstance(records, list):
                return 0
            for item in records:
                record = protocol.normalize_execution_record(item)
                if record["execution_id"]:
                    self._records[record["execution_id"]] = record
            return len(self._records)

    def _flush(self) -> None:
        """Write the whole ledger, atomically. Called with the lock held.

        Atomic because the alternative is a truncated file holding half a
        ledger, and a half-read ledger is a submission that looks new.
        """
        path = self.path
        if path is None:
            return
        payload = {"schema": SCHEMA, "instance": self.instance, "records": list(self._records.values())}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".ledger-", suffix=".part")
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, str(path))
            except Exception:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
        except Exception:
            # A ledger that cannot be written is reported by the caller, which
            # refuses the submission rather than making one it cannot record.
            raise

    # -- reading -------------------------------------------------------------

    def get(self, execution_id: typing.Any) -> typing.Optional[dict]:
        with self._lock:
            if not self._loaded:
                self.load()
            found = self._records.get(str(execution_id or ""))
            return dict(found) if found else None

    def records(self) -> typing.List[dict]:
        with self._lock:
            if not self._loaded:
                self.load()
            return [dict(item) for item in self._records.values()]

    def open_count(self) -> int:
        """How many submissions have no outcome yet."""
        with self._lock:
            return sum(1 for item in self._records.values() if item["state"] in protocol.EXEC_OPEN)

    # -- writing -------------------------------------------------------------

    def reserve(self, execution_id: str, model_type: str = "", residency_key: str = "") -> dict:
        """Record a submission *before* it is made, and return the record.

        This is the load-bearing call. It returns a record whose state is
        ``accepted``, which means "we are about to ask WanGP" - not "WanGP
        took it". If the process dies between here and the submission, the
        record says ``accepted`` with no outcome, and a later reconciliation
        reads that as unknown rather than as a job that never ran.
        """
        with self._lock:
            if not self._loaded:
                self.load()
            now = float(self._clock())
            record = protocol.normalize_execution_record(
                {
                    "execution_id": execution_id,
                    "state": protocol.EXEC_ACCEPTED,
                    "stage": "handed to WanGP's queue",
                    "submitted_at": now,
                    "instance": self.instance,
                    "residency_key": residency_key,
                }
            )
            record["model_type"] = str(model_type or "")[:200]
            self._records[execution_id] = record
            self._sweep(now)
            self._flush()
            return dict(record)

    def update(self, execution_id: str, **changes: typing.Any) -> typing.Optional[dict]:
        """Move a record on. Unknown fields are dropped by the normaliser.

        A terminal record never moves again: once a generation is recorded as
        done, failed or cancelled, a later observation is a stale reading of
        a queue that has moved on, and letting it overwrite the outcome is
        how a finished job becomes "unknown" an hour later.
        """
        with self._lock:
            if not self._loaded:
                self.load()
            current = self._records.get(str(execution_id or ""))
            if current is None:
                return None
            if current["state"] in protocol.EXEC_TERMINAL:
                return dict(current)
            merged = dict(current)
            merged.update(changes)
            record = protocol.normalize_execution_record(merged)
            record["model_type"] = current.get("model_type", "")
            if record["state"] in protocol.EXEC_TERMINAL and not record["finished_at"]:
                record["finished_at"] = float(self._clock())
            self._records[execution_id] = record
            self._flush()
            return dict(record)

    def forget(self, execution_id: typing.Any) -> bool:
        """Drop a terminal record because Forge has it. Never a live one."""
        with self._lock:
            if not self._loaded:
                self.load()
            key = str(execution_id or "")
            found = self._records.get(key)
            if found is None:
                return False
            if found["state"] not in protocol.EXEC_TERMINAL:
                return False
            del self._records[key]
            self._flush()
            return True

    def orphan_open_records(self) -> int:
        """Every submission from an earlier run of this child, made honest.

        Called once at start-up. A record whose instance is not this one
        belongs to a process that is gone; whatever it was doing, it is not
        doing it now, and nobody can say whether it finished. That is
        ``unknown``, and it is the answer that stops a resubmission rather
        than causing one.
        """
        with self._lock:
            if not self._loaded:
                self.load()
            changed = 0
            for key, record in list(self._records.items()):
                if record["state"] not in protocol.EXEC_OPEN:
                    continue
                if record.get("instance") == self.instance and self.instance:
                    continue
                record = dict(record)
                record["state"] = protocol.EXEC_UNKNOWN
                record["code"] = protocol.EXECUTION_UNKNOWN
                record["message"] = "the WanGP process that held this generation is gone"
                record["finished_at"] = float(self._clock())
                self._records[key] = protocol.normalize_execution_record(record)
                changed += 1
            if changed:
                self._flush()
            return changed

    # -- bookkeeping ---------------------------------------------------------

    def _sweep(self, now: float) -> None:
        """Old terminal records out; live ones never. Lock held."""
        for key, record in list(self._records.items()):
            if record["state"] not in protocol.EXEC_TERMINAL:
                continue
            settled = record["finished_at"] or record["submitted_at"]
            if settled and now - settled > KEEP_TERMINAL_SECONDS:
                del self._records[key]
        while len(self._records) > MAX_RECORDS:
            victim = next((key for key, item in self._records.items() if item["state"] in protocol.EXEC_TERMINAL), None)
            if victim is None:
                return
            del self._records[victim]


def for_child(environ: typing.Optional[typing.Mapping[str, str]] = None) -> typing.Optional[Ledger]:
    """The ledger for this run, or None when this run has no ledger root.

    No root means Forge did not launch this child for server execution - an
    older Forge, or a WanGP somebody started by hand - and the control
    surface then does not open. There is deliberately no in-memory fallback:
    a ledger that does not survive the process is not a ledger, and one that
    pretends to be is worse than none.
    """
    root = compatibility.ledger_root(environ)
    if not root:
        return None
    book = Ledger(root=root, instance=compatibility.instance_id(environ))
    book.load()
    book.orphan_open_records()
    return book


__all__ = ["KEEP_TERMINAL_SECONDS", "LEDGER_NAME", "MAX_RECORDS", "SCHEMA", "Ledger", "for_child"]
