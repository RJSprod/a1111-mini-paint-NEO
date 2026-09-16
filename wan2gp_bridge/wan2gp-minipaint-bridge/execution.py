"""Server-executed generation: into WanGP's own queue, not beside it.

THE ONE DECISION THIS FILE EXISTS TO MAKE, AND WHY IT IS NOT THE OBVIOUS ONE.

Wan2GP publishes a Python API whose session can be constructed with no Gradio
object at all. It is the obvious adapter for unattended work, it is the one a
first reading of the problem arrives at, and it is wrong - not slightly, and
not for a reason visible from its own documentation.

A session constructed that way allocates a private ``gen`` dict. Every
per-state guard in Wan2GP keys on the *shared* one: the generation arbiter,
the GPU-resource acquire, and - reachably, from a button a user can press -
the guard on Force Unload Models from RAM. A run in a private dict is
invisible to all of them. So a headless submission arriving during a WebUI
generation proceeds concurrently, against one card, one set of module
globals and one model cache that either side may free mid-inference; and a
user pressing Force Unload during an unattended run is not refused, because
the guard looks at a dict that says nothing is happening.

That hazard cannot be closed from the plugin. Checking a flag before
submitting closes exactly one of the two arrival orders, and the flag it
would check has four unconditional writers, no owner and no nesting count -
its headless clear runs outside the lock that guards the run, so it
under-reports even for two runs of the same kind. Nothing between a user's
Generate press and ``generate_media`` consults anything a plugin can own.

So this file does not build an exclusion. It submits the way Wan2GP's own
Deepy integration submits: leave the task in the shared queue and ask the one
process-wide service to look. The service holds a mutation lock, starts at
most one worker, and returns the existing worker rather than a second one -
and the worker drains the same list. Both arrival orders are then covered by
a gate that was already there and is already correct:

    user first, MiniPaint second  - no second worker starts; the running one
                                    drains our task from the same list
    MiniPaint first, user second  - the user's own chain reaches the same
                                    ``start_generation`` and observes the
                                    existing worker

There is no new lock, no new flag, nothing to release on cancel, and nothing
upstream to change. The dividend is larger than the exclusion: because the
task runs on the shared ``gen`` dict, an unattended job is visible in the
WanGP tab's queue, shows in its progress display, is cancellable there, and
every guard keyed on that dict - Force Unload included - starts protecting it
for free.

Three things this file is careful about, each of which is a real defect in
the path it uses rather than a precaution:

*   **The stranded-task window.** The worker clears itself only after its
    finally block has finalised, synced and unloaded - which can take
    seconds - and during that window ``start_generation`` returns a dying
    worker whose drain loop has already exited. A task landing there is
    merged and never run. So a submission that sits in the queue with no
    worker and no generation in progress is re-triggered, boundedly and
    idempotently, and a task that cannot be got moving is reported rather
    than left pending forever.

*   **Append, never splice.** The wrapper Wan2GP offers plugins forces
    priority and splices at index 1, ahead of everything the user has
    queued. That satisfies the letter of "never pre-empt the user" while
    inverting it. Unattended work goes on the end.

*   **Only signals that cannot go stale.** Queue membership, the service's
    worker handle, and ``gen["in_progress"]``. Never
    ``main_process_running`` - it is set one line before a call that can
    raise and cleared two branches later, so one exception leaks it True for
    the life of the process and anything gated on it waits forever.

Nothing here blocks a caller: ``service.command`` starts a detached worker
and returns, the ledger write is a rename, and the tracking is one daemon
thread shared by every open execution.
"""

from __future__ import annotations

import os
import threading
import time
import typing

try:
    from . import admission, compatibility, handoff, protocol
except ImportError:  # pragma: no cover - depends on how WanGP imports plugins
    import admission  # type: ignore[no-redef]
    import compatibility  # type: ignore[no-redef]
    import handoff  # type: ignore[no-redef]
    import protocol  # type: ignore[no-redef]


#: How often the tracker looks at the shared queue. Seconds, never
#: sub-second: this is somebody else's data structure and reading it in a
#: tight loop buys nothing but contention.
TICK_SECONDS = 0.5

#: How long a submission may sit in the queue with no worker and no
#: generation before it counts as stranded rather than as merely queued.
#: Longer than the finally block's unload takes on any install seen so far,
#: and short enough that a user is not left looking at a job that will never
#: move.
STRANDED_GRACE_SECONDS = 4.0
#: How many times the service is asked again before the task is reported
#: stranded. Each re-trigger is idempotent - it sets no inline queue, so a
#: non-empty queue is left exactly as it is and only the worker is started.
STRANDED_RETRIES = 5

#: How long a submission may fail to appear in the queue at all before it is
#: treated as refused. The merge happens on the service's own thread, so a
#: moment's absence is normal and a minute's absence is not.
MERGE_GRACE_SECONDS = 60.0

#: What a completed task's outputs are looked for under, in the shared
#: record. The first that is a list of strings wins.
OUTPUT_KEYS: typing.Tuple[str, ...] = ("file_list", "files", "output_files", "generated_files")


class ExecutionError(Exception):
    """A refusal with a control-plane code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = str(detail or "")
        super().__init__(f"{code}: {self.detail}" if self.detail else code)


class Executor:
    """One per child. Owns the tracker thread and nothing else.

    Deliberately holds no session, no model and no queue of its own: the
    queue is WanGP's, the model is WanGP's, and the only state here is what
    has been seen about each open execution since it was submitted.
    """

    def __init__(
        self,
        compat: "compatibility.Compatibility",
        book: typing.Any,
        environ: typing.Optional[typing.Mapping[str, str]] = None,
        clock: typing.Callable[[], float] = time.time,
        note: typing.Optional[typing.Callable[[str], None]] = None,
    ) -> None:
        self.compat = compat
        self.ledger = book
        self.environ = environ
        self._clock = clock
        self._note = note or (lambda _text: None)
        self._lock = threading.RLock()
        #: What has been observed about each open execution, keyed by id:
        #: when it was submitted, whether it has ever been seen in the queue,
        #: and how many times it has been re-triggered.
        self._watched: typing.Dict[str, dict] = {}
        self._thread: typing.Optional[threading.Thread] = None
        self._wake = threading.Event()
        self._stopping = False

    # -- readiness -----------------------------------------------------------

    def service(self) -> typing.Any:
        return self.compat.service()

    def available(self) -> typing.Tuple[bool, str]:
        """Whether a job can be submitted right now, and why not.

        READY on the Forge side is a liveness and transport fact. It says the
        process answered an HTTP request; it does not say the bridge is on
        the page, that the arbiter resolved, or that a settings base can be
        composed. Those are this process's to answer, and the executor
        answers them here so that nothing upstream treats a running process
        as an admission.
        """
        if self.ledger is None:
            return False, protocol.CONTROL_UNAVAILABLE
        if self.service() is None:
            return False, protocol.SERVICE_UNAVAILABLE
        if self.compat.shared_gen() is None:
            return False, protocol.SERVICE_UNAVAILABLE
        return True, ""

    # -- submission ----------------------------------------------------------

    def submit(self, request: typing.Mapping[str, typing.Any]) -> dict:
        """One server-executed generation. Returns the ledger record.

        Idempotent by execution id, which is the whole of B8: a repeated
        submission is answered from the record, whatever state that record is
        in, and never by asking WanGP again. WanGP itself offers no
        deduplication - the same ``client_id`` twice starts two generations -
        so this is the only place the guarantee can live.
        """
        execution_id = request["execution_id"]
        existing = self.ledger.get(execution_id) if self.ledger is not None else None
        if existing is not None:
            self._note(f"execute {execution_id[:8]}: already submitted; answering from the ledger ({existing['state']})")
            return existing

        ok, code = self.available()
        if not ok:
            raise ExecutionError(code, "server execution is not available in this WanGP")

        service = self.service()
        gen = self.compat.shared_gen(service)
        settings = self._prepare(request)
        residency = self.compat.residency_key(settings, request.get("model_type", ""))

        # BEFORE the submission, and flushed. A record with no outcome is an
        # honest "we asked and cannot prove what happened"; no record at all
        # is a submission that looks new and generates a second time.
        record = self.ledger.reserve(execution_id, model_type=request.get("model_type", ""), residency_key=residency)

        try:
            self._enqueue(service, gen, settings, execution_id)
        except ExecutionError:
            self.ledger.update(execution_id, state=protocol.EXEC_FAILED, code=protocol.EXECUTION_REFUSED,
                               message="WanGP would not take the task", stage="")
            raise
        except Exception as error:
            self.ledger.update(execution_id, state=protocol.EXEC_FAILED, code=protocol.EXECUTION_REFUSED,
                               message=f"{type(error).__name__}", stage="")
            raise ExecutionError(protocol.EXECUTION_REFUSED, f"{type(error).__name__}: {error}")

        with self._lock:
            self._watched[execution_id] = {
                "submitted_at": float(self._clock()),
                "seen_queued": False,
                "retries": 0,
                "idle_since": 0.0,
                "priority": bool(request.get("priority")),
            }
        self._ensure_tracker()
        self._note(
            f"execute {execution_id[:8]}: {len(settings)} setting(s) into WanGP's own queue "
            f"(appended); residency {residency or 'unknown'}"
        )
        return self.ledger.get(execution_id) or record

    def _prepare(self, request: typing.Mapping[str, typing.Any]) -> typing.Dict[str, typing.Any]:
        """The composed base, with this job's own prompt, media and id on it.

        Everything the request does not own is the user's configuration and
        is carried through untouched. What the request owns is exactly three
        things, and the third is the one nobody thinks of: the settings a
        compose returns carry the *composing page's* ``client_id``, and an
        adapter that did not overwrite it would have WanGP route this job's
        artifacts to somebody else's request.
        """
        settings = dict(request["settings"])
        prompt = request.get("prompt")
        if prompt is not None:
            key = self.compat.settings_key(compatibility.PROMPT) or "prompt"
            settings[key] = prompt
        media = self._resolve_media(request.get("media") or {})
        switched = self.compat.apply_media_to_settings(settings, media)
        if switched:
            self._note(f"execute {request['execution_id'][:8]}: switched on {', '.join(switched)}")
        client_key = self.compat.settings_key(compatibility.CLIENT_ID) or "client_id"
        settings[client_key] = request["execution_id"]
        model_type = request.get("model_type") or ""
        if model_type:
            self._require_model(model_type)
            settings["model_type"] = model_type
        if not settings.get("model_type"):
            # Refused here rather than handed over. Wan2GP's queue unpacker
            # requires it and SKIPS a task without it - printing a line and
            # carrying on - so a task that reached the queue this way was
            # never a task at all, and the job that owned it waited forever
            # for a generation nobody was ever going to run. A refusal with a
            # code is the difference between a bug you can see and a hang.
            raise ExecutionError(
                protocol.MODEL_UNAVAILABLE,
                "the composed settings name no model, and WanGP's queue would skip the task",
            )
        return settings

    def _resolve_media(self, media: typing.Mapping[str, typing.Any]) -> typing.Dict[str, typing.List[str]]:
        """Handoff ids into paths this child may read. Ids in, paths out.

        The resolution happens here and nowhere else, which is what keeps the
        promise that no browser ever supplies a filesystem path: Forge writes
        the pixels into the handoff root both processes share and names them,
        and this proves each name is a plain file under that root before it
        becomes a string WanGP will open.
        """
        out: typing.Dict[str, typing.List[str]] = {}
        for slot in protocol.EXEC_SLOTS:
            supplied = media.get(slot)
            if not supplied:
                continue
            ids = list(supplied) if isinstance(supplied, (list, tuple)) else [supplied]
            paths: typing.List[str] = []
            for handoff_id in ids:
                try:
                    paths.append(str(handoff.resolve(handoff_id, self.environ)))
                except Exception as error:
                    raise ExecutionError(compatibility.HANDOFF_NOT_FOUND, f"{str(handoff_id)[:8]}: {error}")
            out[slot] = paths
        return out

    def _require_model(self, model_type: str) -> None:
        """Refuse a snapshot whose model this WanGP no longer has.

        The job names the model it was composed for and runs against that one
        or none. It is never quietly run against whatever model a user
        selected in the WanGP tab afterwards - that is a different
        generation, and the person who pressed the button would have no way
        of knowing it happened.
        """
        getter = self.compat.host.read_global("get_model_def")
        if not callable(getter):
            return
        try:
            definition = getter(model_type)
        except Exception:
            return
        if definition is None or definition is False:
            raise ExecutionError(protocol.MODEL_UNAVAILABLE, f"{model_type[:60]} is not a model this WanGP has")

    def _enqueue(
        self,
        service: typing.Any,
        gen: typing.Any,
        settings: typing.Mapping[str, typing.Any],
        execution_id: str,
    ) -> None:
        """Leave the task where the one worker will find it, and ask it to look.

        Two statements, and they are Wan2GP's own: the inline slot the queue
        loader reads, and the command that starts or joins the worker. The
        command returns at once - it spawns a detached worker of the
        service's own - so no handler thread waits on a generation here.
        """
        if not isinstance(gen, dict):
            raise ExecutionError(protocol.SERVICE_UNAVAILABLE, "the shared generation record could not be read")
        # A LIST, and that is the whole of it.
        #
        # Wan2GP's loader wraps the inline slot itself when it finds a dict:
        # ``newly_loaded_queue = [{"id": 0, "params": newly_loaded_queue}]``.
        # Handing it a dict that was already ``{"id": …, "params": …}`` got
        # that wrapped a second time, so the manifest's ``params`` was
        # ``{"id": 0, "params": {…}}`` - a mapping with no ``model_type`` in
        # it, one level above the settings. Wan2GP then said exactly that
        # ("Settings must contain 'model_type'"), skipped the task, and the
        # job sat in WanGP's queue forever having never been a task at all.
        #
        # A list is passed through as the manifest verbatim, so this is the
        # shape the parser actually reads: one entry, its params the settings.
        #
        # ``plugin_data`` is a SIBLING OF params, never a member of it, and
        # the difference is a crash rather than a stray value. Wan2GP's
        # worker filters params down to the arguments ``generate_media``
        # names and splats them - and ``plugin_data`` is one of those names,
        # so a copy left inside params survives the filter and is passed a
        # second time beside the one the worker pops off the task:
        #
        #     plugin_data = task.pop('plugin_data', {})
        #     generate_media(task, send_cmd, plugin_data=plugin_data, **filtered_params)
        #     TypeError: generate_media() got multiple values for keyword argument 'plugin_data'
        #
        # Nothing here puts it there. The composed base is whatever the user
        # last committed for the model, and Wan2GP's own recorder pops the
        # key before it stores that - but another plugin capturing the form
        # for its own purposes may record it with the key still on, and then
        # every bridge job on that install dies before its first frame. So
        # the entry is built the way Wan2GP builds its own (``add_video_task``
        # pops it out of the inputs and writes it beside them), which both
        # removes the collision and carries the plugin's data through to the
        # generation instead of dropping it on the floor.
        params = dict(settings)
        plugin_data = params.pop(compatibility.PLUGIN_DATA_KEY, None)
        entry: typing.Dict[str, typing.Any] = {
            "id": 0,
            "params": params,
            compatibility.PLUGIN_DATA_KEY: plugin_data if isinstance(plugin_data, dict) else {},
        }
        gen[compatibility.INLINE_QUEUE_KEY] = [entry]
        try:
            service.command(compatibility.LOAD_QUEUE_COMMAND, {"client_id": execution_id})
        except Exception as error:
            gen.pop(compatibility.INLINE_QUEUE_KEY, None)
            raise ExecutionError(protocol.EXECUTION_REFUSED, f"{type(error).__name__}: {error}")

    def _retrigger(self, service: typing.Any, execution_id: str) -> bool:
        """Ask the service to look again, without offering it anything new.

        WITHOUT the inline slot, which is what makes it idempotent: the queue
        loader with no inline entry leaves a non-empty queue exactly as it
        is, and only the worker is started. Setting the slot again would
        merge a second copy of the task.
        """
        try:
            service.command(compatibility.LOAD_QUEUE_COMMAND, {"client_id": execution_id})
            return True
        except Exception as error:
            self._note(f"execute {execution_id[:8]}: re-trigger refused ({type(error).__name__})")
            return False

    # -- cancellation --------------------------------------------------------

    def cancel(self, execution_id: str) -> dict:
        """Stop one of ours, and never disturb anything that is not.

        A task still waiting is removed from the shared queue, which is the
        same thing removing it in the WanGP tab would do. A task that is
        already generating is aborted through WanGP's own abort, which is
        cooperative and is the control a user would press - and only when the
        task at the head is *ours*, because aborting somebody else's run to
        cancel our own queued job is the pre-emption this whole design
        refuses.
        """
        record = self.ledger.get(execution_id) if self.ledger is not None else None
        if record is None:
            raise ExecutionError(protocol.QUEUE_CODE_REQUEST_INVALID, f"no record for {str(execution_id)[:8]}")
        if record["state"] in protocol.EXEC_TERMINAL:
            return record
        service = self.service()
        gen = self.compat.shared_gen(service)
        position = admission.task_position(gen, execution_id)
        running = position == 0 and admission.generating(gen)
        if position is not None and not running and isinstance(gen, dict):
            queue = gen.get(compatibility.GEN_QUEUE_KEY)
            if isinstance(queue, list):
                for index in range(len(queue) - 1, -1, -1):
                    task = queue[index]
                    params = task.get("params") if isinstance(task, dict) else None
                    client = params.get("client_id") if isinstance(params, dict) else (task.get("client_id") if isinstance(task, dict) else None)
                    if client == execution_id and index > 0:
                        del queue[index]
        elif running:
            self._abort(service)
        with self._lock:
            self._watched.pop(execution_id, None)
        updated = self.ledger.update(
            execution_id,
            state=protocol.EXEC_CANCELLED,
            stage="cancelled from MiniPaint",
            code="",
            message="",
        )
        self._note(f"execute {execution_id[:8]}: cancelled ({'while generating' if running else 'while waiting'})")
        return updated or record

    def _abort(self, service: typing.Any) -> None:
        """WanGP's own abort, through whichever name this build has for it."""
        for name in ("abort", "abort_generation", "request_abort"):
            method = getattr(service, name, None)
            if callable(method):
                try:
                    method()
                    return
                except Exception:
                    continue
        aborter = self.compat.host.read_global("abort_generation")
        if callable(aborter):
            try:
                aborter()
            except Exception:
                pass

    # -- tracking ------------------------------------------------------------

    def status(self, execution_ids: typing.Sequence[str]) -> typing.Dict[str, dict]:
        """Where each of these is, from the ledger, refreshed against the queue."""
        self._tick()
        out: typing.Dict[str, dict] = {}
        for execution_id in execution_ids:
            found = self.ledger.get(execution_id) if self.ledger is not None else None
            out[execution_id] = found or protocol.normalize_execution_record(
                {"execution_id": execution_id, "state": protocol.EXEC_UNKNOWN, "code": protocol.EXECUTION_UNKNOWN,
                 "message": "no record of that submission"}
            )
        return out

    def _ensure_tracker(self) -> None:
        with self._lock:
            thread = self._thread
            if thread is not None and thread.is_alive():
                self._wake.set()
                return
            self._stopping = False
            thread = threading.Thread(target=self._track_loop, name="minipaint-bridge-executions", daemon=True)
            self._thread = thread
        thread.start()

    def stop(self) -> None:
        """Let the tracker end. The child is going away; nothing is aborted."""
        with self._lock:
            self._stopping = True
        self._wake.set()

    def _track_loop(self) -> None:
        try:
            while True:
                self._wake.wait(TICK_SECONDS)
                self._wake.clear()
                with self._lock:
                    if self._stopping:
                        return
                    if not self._watched:
                        self._thread = None
                        return
                try:
                    self._tick()
                except Exception as error:  # pragma: no cover - a tracker that dies is a note
                    self._note(f"the execution tracker stumbled ({type(error).__name__}); it keeps going")
        finally:
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None

    def _tick(self) -> None:
        """One pass over every open execution. The whole of the tracking."""
        with self._lock:
            watching = dict(self._watched)
        if not watching:
            return
        service = self.service()
        gen = self.compat.shared_gen(service)
        worker = self.compat.service_generation_running(service)
        in_progress = admission.generating(gen)
        depth = admission.queue_length(gen)
        now = float(self._clock())

        for execution_id, seen in watching.items():
            record = self.ledger.get(execution_id) if self.ledger is not None else None
            if record is None or record["state"] in protocol.EXEC_TERMINAL:
                with self._lock:
                    self._watched.pop(execution_id, None)
                continue
            position = admission.task_position(gen, execution_id)

            if position is not None:
                seen["seen_queued"] = True
                running = position == 0 and bool(in_progress)
                self.ledger.update(
                    execution_id,
                    state=protocol.EXEC_RUNNING if running else protocol.EXEC_QUEUED,
                    stage="generating" if running else "waiting in WanGP's queue",
                    position=position,
                    queue_depth=depth,
                )
                if running or worker:
                    seen["idle_since"] = 0.0
                else:
                    # The stranded window: our task is in the queue, no
                    # worker exists and nothing is generating. It is normal
                    # for a fraction of a second and is a lost job after
                    # that, so it is given a grace and then re-triggered.
                    if not seen["idle_since"]:
                        seen["idle_since"] = now
                    elif now - seen["idle_since"] > STRANDED_GRACE_SECONDS:
                        seen["idle_since"] = now
                        seen["retries"] += 1
                        if seen["retries"] > STRANDED_RETRIES:
                            self.ledger.update(
                                execution_id,
                                state=protocol.EXEC_FAILED,
                                code=protocol.EXECUTION_REFUSED,
                                message="the task reached WanGP's queue but nothing started it",
                                stage="",
                            )
                            self._note(f"execute {execution_id[:8]}: stranded in the queue after {STRANDED_RETRIES} attempts")
                            with self._lock:
                                self._watched.pop(execution_id, None)
                            continue
                        self._note(f"execute {execution_id[:8]}: queued with no worker; asking the service again ({seen['retries']})")
                        self._retrigger(service, execution_id)
            elif seen["seen_queued"]:
                # It was in the queue and is not now: the worker drained it.
                # WanGP removes a finished task, so absence after presence is
                # completion - unless it recorded a refusal against the id,
                # which is the one piece of correlated evidence there is.
                if admission.refusal_evidence(gen, execution_id):
                    self.ledger.update(execution_id, state=protocol.EXEC_FAILED,
                                       code=compatibility.WANGP_VALIDATION_REFUSED,
                                       message="WanGP declined the task", stage="")
                    self._note(f"execute {execution_id[:8]}: WanGP declined it")
                else:
                    files = self._outputs(gen, float(seen["submitted_at"]))
                    self.ledger.update(execution_id, state=protocol.EXEC_DONE, stage="",
                                       generated_files=files, position=None, queue_depth=depth)
                    self._note(f"execute {execution_id[:8]}: finished with {len(files)} file(s)")
                with self._lock:
                    self._watched.pop(execution_id, None)
            elif now - float(seen["submitted_at"]) > MERGE_GRACE_SECONDS:
                # Never seen in the queue at all. The merge runs on the
                # service's own thread, so a moment's absence is ordinary and
                # a minute's absence means it was refused before it got there.
                self.ledger.update(execution_id, state=protocol.EXEC_FAILED,
                                   code=protocol.EXECUTION_REFUSED,
                                   message="the task never reached WanGP's queue", stage="")
                self._note(f"execute {execution_id[:8]}: never appeared in the queue; refused")
                with self._lock:
                    self._watched.pop(execution_id, None)

        with self._lock:
            for execution_id, seen in watching.items():
                if execution_id in self._watched:
                    self._watched[execution_id] = seen

    def _outputs(self, gen: typing.Any, since: float) -> typing.List[str]:
        """The files this generation produced, as far as the record will say.

        Filtered by modification time rather than trusted wholesale: the
        shared list is WanGP's gallery and holds everything the session has
        ever made, so a job's own output is the part of it that appeared
        after the job was submitted. A build whose record cannot be read
        returns nothing, and the job is still a success - an empty list with
        the reason beside it is honest, and inventing paths is not.

        ABSOLUTE, BECAUSE THESE PATHS LEAVE THE PROCESS THAT CAN READ THEM.

        Wan2GP's ``save_path`` is a setting, and an install that has not
        repointed it holds the relative string ``outputs`` - so the paths it
        records are relative to the directory Wan2GP runs in. That works
        perfectly here, inside that process, and not at all in Forge, which
        is a different process with a different working directory: the path
        arrives, stats as missing, and the gallery drops the file as one the
        user deleted. Forge can guess at the directory when it started the
        child itself, and does, for the records written before this - but a
        Wan2GP somebody launched independently leaves it nothing to guess
        with, and a guess is not what a path should be.

        This process is the one that knows for certain, so this is where it
        is settled. The same working directory resolved the ``getmtime``
        above, so the absolute form names the file that was just checked.
        """
        if not isinstance(gen, dict):
            return []
        found: typing.List[str] = []
        for key in OUTPUT_KEYS:
            listed = gen.get(key)
            if not isinstance(listed, (list, tuple)):
                continue
            for item in listed:
                path = item if isinstance(item, str) else (item.get("path") if isinstance(item, dict) else None)
                if not isinstance(path, str) or not path:
                    continue
                try:
                    if os.path.getmtime(path) + 1.0 < since:
                        continue
                except OSError:
                    continue
                # Deduplicated on the absolute form, not the recorded one,
                # so two spellings of one file collapse instead of both
                # being reported.
                resolved = os.path.abspath(path)
                if resolved not in found:
                    found.append(resolved)
            if found:
                break
        return found[:64]


__all__ = [
    "MERGE_GRACE_SECONDS", "OUTPUT_KEYS", "STRANDED_GRACE_SECONDS", "STRANDED_RETRIES", "TICK_SECONDS",
    "ExecutionError", "Executor",
]
