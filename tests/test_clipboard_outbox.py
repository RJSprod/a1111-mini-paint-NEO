"""The queue outbox: the server owns the line, one lease at a time.

Protocol 4's answer to a button that blocked. A press is a job appended to a
document beside the draft and the history; a page is a pump that claims the
next job it may run and reports how it went. The checks here are the rules
a reader should be able to rely on without reading the code: submits under
a burst keep their order and are refused while WanGP is not running; a
claim hands out one lease at a time across every page and gives a page its
own jobs in order; a page that stopped asking does not hold the others up;
a lease that expires returns a job to pending only when nothing was written
into WanGP and marks it unconfirmed otherwise; an unconfirmed job is never
claimed again by itself; cancel works while pending and not while sending;
retry is a new request; a broken document is moved aside and the rest
survives; nothing in what a page is shown names a path. The routes are
driven on a bare app behind the sign-in gate.
"""

from harness import Results, setup_path

setup_path()

import json  # noqa: E402
import pathlib  # noqa: E402
import tempfile  # noqa: E402

from minipaint_neo import interop  # noqa: E402
from minipaint_neo.clipboard import config, outbox  # noqa: E402
from minipaint_neo.wangp import config as wangp_config  # noqa: E402
from minipaint_neo.wangp import errors  # noqa: E402
from minipaint_neo.wangp.errors import IntegrationError  # noqa: E402

PAGE_A = "a" * 16
PAGE_B = "b" * 16
ASSET = "0123456789abcdef0123456789abcdef"


class _Clock:
    def __init__(self):
        self.now = 1_700_000_000.0

    def __call__(self):
        return self.now


def _refused(call, *args, **keywords) -> str:
    try:
        call(*args, **keywords)
    except IntegrationError as error:
        return error.code
    return ""


def _request(prompt="a prompt", **extra):
    request = {"prompt": prompt, "images": {"start": {"kind": "clipboard_asset", "id": ASSET}}}
    request.update(extra)
    return request


def _positive(status="queued", depth=None, **extra):
    result = {"ok": True, "status": status, "tasks_added": 1, "applied": {"prompt": True, "start": True}, "inherited": ["end", "references"],
              "ignored": [], "model": {"type": "video", "label": "A video model"}, "route": "generate" if status == "started" else "queue"}
    if depth is not None:
        result["queue_depth"] = depth
    result.update(extra)
    return result


def outbox_checks(r: Results, clock: _Clock) -> None:
    running = {"on": True}
    outbox.use_running(lambda: running["on"])

    # -- submit: refused while WanGP is not running, kept in order otherwise
    running["on"] = False
    r.check("a press while WanGP is not running is WANGP_NOT_RUNNING, and stores nothing",
            _refused(outbox.submit, _request(), PAGE_A) == errors.WANGP_NOT_RUNNING and outbox.jobs() == [])
    running["on"] = True
    r.check("a request that does not normalise is refused before it is stored", _refused(outbox.submit, {"prompt": 3}, PAGE_A) == errors.REQUEST_INVALID and outbox.jobs() == [])
    r.check("a page id that is not one is refused", _refused(outbox.submit, _request(), "not a page") == errors.REQUEST_INVALID)
    submitted = []
    for index in range(12):
        clock.now += 0.001
        submitted.append(outbox.submit(_request(f"prompt {index}"), PAGE_A if index % 3 else PAGE_B))
    jobs = outbox.jobs()
    r.check("a burst of presses is kept, in the order pressed", [job["job_id"] for job in jobs] == [job["job_id"] for job in submitted], str(len(jobs)))
    r.check("each job is pending, unsent, owned by its page, with its request and a fresh request id",
            all(job["state"] == "pending" and not job["sent"] and job["attempts"] == 0 for job in jobs)
            and jobs[0]["page"] == PAGE_B and jobs[1]["page"] == PAGE_A and jobs[0]["request"]["prompt"] == "prompt 0"
            and len({job["request"]["request_id"] for job in jobs}) == 12 and jobs[0]["request"]["start"] == "auto")
    r.check("what a page is shown names no path and no lease token",
            "/" not in json.dumps([job["request"] for job in jobs]) and all("lease" not in job and "token" not in json.dumps(job) for job in jobs))
    r.check("the summary says which fields a job supplies", jobs[0]["summary"] == {"prompt": True, "start": True, "end": False, "references": 0, "start_mode": "auto"})
    r.check("the document lives beside the draft and the history", (config.config_dir() / outbox.OUTBOX_NAME).is_file())
    stored = json.loads((config.config_dir() / outbox.OUTBOX_NAME).read_text(encoding="utf-8"))
    r.check("and holds only the jobs", set(stored) == {"schema", "jobs"} and len(stored["jobs"]) == 12)

    # -- claim: one lease at a time, the oldest job of a page that is asking
    first = outbox.claim(PAGE_B)
    r.check("page B claims the head of the line, which is its own", "job" in first and first["job"]["job_id"] == submitted[0]["job_id"] and first["job"]["state"] == "sending" and first["job"]["attempts"] == 1, str(first)[:120])
    r.check("with a lease and a count of its own jobs behind it", isinstance(first.get("lease"), str) and len(first["lease"]) == 32 and first["pending"] == 3)
    waiting = outbox.claim(PAGE_A)
    r.check("page A is told to wait while B's job is being sent", waiting.get("wait") == outbox.WAIT_BUSY_MS and waiting.get("reason") == "busy" and waiting.get("pending") == 8, str(waiting))
    r.check("so is B itself", outbox.claim(PAGE_B).get("reason") == "busy")
    r.check("cancelling a job being sent is refused", _refused(outbox.cancel, submitted[0]["job_id"]) == errors.QUEUE_BUSY)
    r.check("a report without the lease is nobody's", _refused(outbox.report, submitted[0]["job_id"], "wrong", "done", _positive()) == errors.REQUEST_INVALID)
    r.check("a report with an unknown phase is refused", _refused(outbox.report, submitted[0]["job_id"], first["lease"], "maybe") == errors.REQUEST_INVALID)

    sent = outbox.report(submitted[0]["job_id"], first["lease"], "sent")
    r.check("the page says the bridge admitted it: sent, still leased", sent["sent"] is True and sent["state"] == "sending")
    done = outbox.report(submitted[0]["job_id"], first["lease"], "done", _positive("started", depth=0))
    r.check("and how it ended: started, with the result kept without a prompt",
            done["state"] == "started" and done["result"]["status"] == "started" and done["result"]["queue_depth"] == 0 and done["result"]["route"] == "generate"
            and done["error"] is None and "prompt 0" not in json.dumps(done["result"]) and done["leased_until"] == "", json.dumps(done)[:200])
    r.check("a second done report is refused: the lease is spent", _refused(outbox.report, submitted[0]["job_id"], first["lease"], "done", _positive()) == errors.REQUEST_INVALID)

    # -- turns: the head is A's now; B waits its turn while A is asking
    second = outbox.claim(PAGE_A)
    r.check("page A gets the next job, its own oldest", "job" in second and second["job"]["job_id"] == submitted[1]["job_id"])
    outbox.report(submitted[1]["job_id"], second["lease"], "done", _positive("queued", depth=1))
    r.check("a queued outcome is queued", outbox.get(submitted[1]["job_id"])["state"] == "queued")
    third = outbox.claim(PAGE_B)
    r.check("page B, asking while the head is A's and A asked a moment ago, is told it is A's turn", third.get("reason") == "turn" and third.get("wait") == outbox.WAIT_TURN_MS, str(third))
    clock.now += outbox.PAGE_ACTIVE_SECONDS + 1
    fourth = outbox.claim(PAGE_B)
    r.check("once A has stopped asking, B is given its own next job rather than held up", "job" in fourth and fourth["job"]["page"] == PAGE_B and fourth["job"]["job_id"] == submitted[3]["job_id"], str(fourth)[:120])

    # -- a lease that expires: not sent -> pending again; sent -> unconfirmed
    clock.now += outbox.LEASE_SECONDS + 1
    listed = {job["job_id"]: job for job in outbox.jobs()}
    r.check("an expired lease on a job nothing was written for puts it back to pending",
            listed[submitted[3]["job_id"]]["state"] == "pending" and listed[submitted[3]["job_id"]]["attempts"] == 1)
    again = outbox.claim(PAGE_B)
    r.check("and it is claimed again, as a second attempt", again["job"]["job_id"] == submitted[3]["job_id"] and again["job"]["attempts"] == 2)
    outbox.report(submitted[3]["job_id"], again["lease"], "sent")
    clock.now += outbox.LEASE_SECONDS + 1
    listed = {job["job_id"]: job for job in outbox.jobs()}
    r.check("an expired lease after the overlay was written is unconfirmed, never retried by the machine",
            listed[submitted[3]["job_id"]]["state"] == "unconfirmed" and listed[submitted[3]["job_id"]]["error"]["code"] == errors.ADMISSION_UNCONFIRMED)
    for _ in range(3):
        answer = outbox.claim(PAGE_B)
        if "job" in answer:
            outbox.report(answer["job"]["job_id"], answer["lease"], "done", _positive())
    r.check("no later claim hands the unconfirmed job out", outbox.get(submitted[3]["job_id"])["state"] == "unconfirmed")

    # -- how a job ends: refused, unconfirmed, and what the page may say
    claimed = outbox.claim(PAGE_A)
    refused = outbox.report(claimed["job"]["job_id"], claimed["lease"], "done", {"ok": False, "status": "refused", "code": "WANGP_VALIDATION_REFUSED", "message": "x" * 500})
    r.check("a refusal is failed with its code, the message bounded", refused["state"] == "failed" and refused["error"]["code"] == "WANGP_VALIDATION_REFUSED" and len(refused["error"]["message"]) <= 200)
    claimed = outbox.claim(PAGE_A)
    unsure = outbox.report(claimed["job"]["job_id"], claimed["lease"], "done", {"ok": False, "status": "unconfirmed"})
    r.check("an unconfirmed answer is unconfirmed with the sentence", unsure["state"] == "unconfirmed" and unsure["error"]["code"] == errors.ADMISSION_UNCONFIRMED)
    claimed = outbox.claim(PAGE_A)
    odd = outbox.report(claimed["job"]["job_id"], claimed["lease"], "done", {"ok": True, "status": "done", "tasks_added": "many", "queue_depth": -3, "route": "sideways"})
    r.check("an answer the server cannot read is a refusal, never a success", odd["state"] == "failed" and odd["result"]["queue_depth"] is None and odd["result"]["route"] == "")

    # -- cancel, retry, adopt
    pending = [job for job in outbox.jobs() if job["state"] == "pending"]
    cancelled = outbox.cancel(pending[0]["job_id"])
    r.check("a pending job is cancelled", cancelled["state"] == "cancelled" and outbox.cancel(pending[0]["job_id"])["state"] == "cancelled")
    r.check("an unknown job is QUEUE_JOB_UNKNOWN", _refused(outbox.cancel, "0" * 16) == errors.QUEUE_JOB_UNKNOWN)
    retried = outbox.retry(refused["job_id"], PAGE_B)
    r.check("a retry is a new job for the page that pressed it, with a new request id and the same request",
            retried["job_id"] != refused["job_id"] and retried["page"] == PAGE_B and retried["state"] == "pending" and retried["retry_of"] == refused["job_id"]
            and retried["request"]["request_id"] != refused["request"]["request_id"] and retried["request"]["prompt"] == refused["request"]["prompt"], json.dumps(retried)[:160])
    r.check("retrying a job that is not over is refused", _refused(outbox.retry, [j for j in outbox.jobs() if j["state"] == "pending"][0]["job_id"], PAGE_A) == errors.REQUEST_INVALID)
    r.check("an unconfirmed job can be retried, but only when asked", outbox.retry(unsure["job_id"], PAGE_A)["retry_of"] == unsure["job_id"])
    orphan = [job for job in outbox.jobs() if job["state"] == "pending" and job["page"] == PAGE_A][0]
    adopted = outbox.adopt(orphan["job_id"], PAGE_B)
    r.check("a pending job can be adopted by another page", adopted["page"] == PAGE_B and adopted["state"] == "pending")
    running["on"] = False
    r.check("a retry while WanGP is not running is refused like a press", _refused(outbox.retry, refused["job_id"], PAGE_B) == errors.WANGP_NOT_RUNNING)
    running["on"] = True

    # -- WanGP restarting: whatever was being sent is unconfirmed
    claimed = outbox.claim(PAGE_B)
    r.check("(a job is being sent)", "job" in claimed)
    r.check("a restart marks it unconfirmed and touches nothing pending",
            outbox.on_wangp_restart() == 1 and outbox.get(claimed["job"]["job_id"])["state"] == "unconfirmed"
            and outbox.get(claimed["job"]["job_id"])["error"]["code"] == errors.WANGP_RESTARTED and any(job["state"] == "pending" for job in outbox.jobs()))

    # -- bounds, history bookkeeping, counts
    r.check("counts add up", sum(outbox.counts().values()) == len(outbox.jobs()))
    positive = [job for job in outbox.jobs() if job["state"] in ("queued", "started")]
    r.check("positive jobs wait to be recorded in the history", {job["job_id"] for job in outbox.unrecorded("clipboard")} == {job["job_id"] for job in positive})
    outbox.mark_recorded([job["job_id"] for job in positive])
    r.check("and only once", outbox.unrecorded("clipboard") == [])
    api_job = outbox.submit(_request("from an extension"), PAGE_A, "api")
    r.check("a job from another extension is marked so, and never enters the tab's history", api_job["origin"] == "api" and outbox.unrecorded("clipboard") == [])
    before = len(outbox.jobs())
    saved = outbox.MAX_PENDING
    outbox.MAX_PENDING = len([job for job in outbox.jobs() if job["state"] in ("pending", "sending")])
    r.check("a flood of pending requests is refused as busy rather than stored", _refused(outbox.submit, _request(), PAGE_A) == errors.QUEUE_BUSY and len(outbox.jobs()) == before)
    outbox.MAX_PENDING = saved
    clock.now += outbox.KEEP_TERMINAL_SECONDS + 1
    r.check("jobs that went well are pruned once the keep window is past; pending ones stay",
            not any(job["state"] in ("queued", "started", "completed") for job in outbox.jobs()) and outbox.jobs(), str(outbox.counts()))
    # A failure is the one thing here that wants a person, so it does not
    # delete itself out from under them while they are away.
    failures = [job for job in outbox.jobs() if outbox.failed_state(job["state"])]
    r.check("but a failure is still there, however long the keep window has been past", bool(failures), str(outbox.counts()))
    r.check("a job that has not failed cannot be dismissed",
            _refused(outbox.dismiss, next(job["job_id"] for job in outbox.jobs() if job["state"] == "pending")) == errors.REQUEST_INVALID)
    outbox.dismiss(failures[0]["job_id"])
    r.check("dismissing one marks it, and keeps the record for the grace every terminal job gets",
            outbox.get(failures[0]["job_id"])["dismissed"] is True and outbox.get(failures[0]["job_id"]) is not None)
    clock.now += outbox.KEEP_TERMINAL_SECONDS + 1
    r.check("after which it goes, and the failures nobody dismissed are still there",
            outbox.get(failures[0]["job_id"]) is None
            and len([job for job in outbox.jobs() if outbox.failed_state(job["state"])]) == len(failures) - 1,
            str(outbox.counts()))
    clock.now += outbox.KEEP_FAILED_SECONDS + 1
    r.check("and a week later even an undismissed failure has gone, so a broken setup cannot pile up forever",
            not any(outbox.failed_state(job["state"]) for job in outbox.jobs()), str(outbox.counts()))

    # -- a broken document is moved aside and the outbox starts again
    path = config.config_dir() / outbox.OUTBOX_NAME
    path.write_text("{not json", encoding="utf-8")
    r.check("a document that will not parse is quarantined, not fatal", outbox.jobs() == [] and any(name.startswith(outbox.OUTBOX_NAME + ".broken-") for name in (p.name for p in config.config_dir().iterdir())))
    fresh = outbox.submit(_request("after the break"), PAGE_A)
    r.check("and a new press starts a new document", outbox.jobs()[0]["job_id"] == fresh["job_id"])


def route_checks(r: Results) -> None:
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    outbox.use_running(lambda: True)
    app = FastAPI()
    interop.install(app)
    client = TestClient(app)

    answer = client.post(interop.OUTBOX_SUBMIT_ROUTE, json={"request": _request("through the route"), "page": PAGE_A, "origin": "clipboard"}).json()
    r.check("POST /outbox/submit appends a job", answer.get("ok") and answer["job"]["state"] == "pending" and answer["job"]["origin"] == "clipboard", str(answer)[:120])
    job_id = answer["job"]["job_id"]
    listing = client.get(interop.OUTBOX_ROUTE).json()
    r.check("GET /outbox lists it and says WanGP is running", listing.get("ok") and listing["running"] is True and any(job["job_id"] == job_id for job in listing["jobs"]))
    claimed = client.post(interop.OUTBOX_CLAIM_ROUTE, json={"page": PAGE_A}).json()
    r.check("POST /outbox/claim leases it to the page", claimed.get("ok") and claimed["job"]["job_id"] == job_id and claimed["lease"], str(claimed)[:120])
    sent = client.post(interop.OUTBOX_REPORT_ROUTE, json={"job_id": job_id, "lease": claimed["lease"], "phase": "sent"}).json()
    done = client.post(interop.OUTBOX_REPORT_ROUTE, json={"job_id": job_id, "lease": claimed["lease"], "phase": "done", "result": _positive("started", depth=0)}).json()
    r.check("POST /outbox/report takes sent then done", sent.get("ok") and sent["job"]["sent"] and done.get("ok") and done["job"]["state"] == "started")
    bad = client.post(interop.OUTBOX_REPORT_ROUTE, json={"job_id": job_id, "lease": "nope", "phase": "done", "result": {}})
    r.check("a report without the lease is refused with a code, not a traceback", bad.status_code == 400 and bad.json().get("code") == errors.REQUEST_INVALID)
    r.check("an empty claim says so", client.post(interop.OUTBOX_CLAIM_ROUTE, json={"page": PAGE_A}).json().get("empty") is True)
    second = client.post(interop.OUTBOX_SUBMIT_ROUTE, json={"request": _request("second"), "page": PAGE_A}).json()["job"]
    cancelled = client.post(interop.OUTBOX_CANCEL_ROUTE, json={"job_id": second["job_id"]}).json()
    r.check("POST /outbox/cancel cancels a pending job", cancelled.get("ok") and cancelled["job"]["state"] == "cancelled")
    retried = client.post(interop.OUTBOX_RETRY_ROUTE, json={"job_id": second["job_id"], "page": PAGE_B}).json()
    r.check("POST /outbox/retry makes a new job for the page", retried.get("ok") and retried["job"]["retry_of"] == second["job_id"] and retried["job"]["page"] == PAGE_B)
    adopted = client.post(interop.OUTBOX_ADOPT_ROUTE, json={"job_id": retried["job"]["job_id"], "page": PAGE_A}).json()
    r.check("POST /outbox/adopt hands a pending job to another page", adopted.get("ok") and adopted["job"]["page"] == PAGE_A)
    missing = client.post(interop.OUTBOX_CANCEL_ROUTE, json={"job_id": "0" * 16})
    r.check("an unknown job is 404 with its code", missing.status_code == 404 and missing.json().get("code") == errors.QUEUE_JOB_UNKNOWN)

    outbox.use_running(lambda: False)
    blocked = client.post(interop.OUTBOX_SUBMIT_ROUTE, json={"request": _request(), "page": PAGE_A})
    r.check("a submit while WanGP is not running is 409 WANGP_NOT_RUNNING", blocked.status_code == 409 and blocked.json().get("code") == errors.WANGP_NOT_RUNNING)
    stopped = client.post(interop.OUTBOX_CLAIM_ROUTE, json={"page": PAGE_A})
    r.check("and so is a claim, which is what stops a page's pump", stopped.status_code == 409 and stopped.json().get("code") == errors.WANGP_NOT_RUNNING)
    r.check("the list still answers, saying WanGP is not running", client.get(interop.OUTBOX_ROUTE).json().get("running") is False)
    r.check("the contract names the outbox route", client.get(interop.CONTRACT_ROUTE).json().get("outbox") == interop.OUTBOX_ROUTE)


def settings_source_checks(r: Results) -> None:
    """Where a job's WanGP settings came from, as the queue, both send
    histories and the log say it - worked out from the job alone.

    Written as a table because it is one: every combination a job can be in,
    and the one answer each gets. The executor's checks drive the server
    half through a real compose; this is every row, the page-run ones and
    the not-yet-decided ones included.
    """
    from minipaint_neo.wangp import protocol as wire

    def server(source, flush="", inherit=True):
        job = {"executor": outbox.EXECUTOR_SERVER, "state": outbox.COMPLETED, "settings_flush": flush, "inherit_settings": inherit}
        if source is not None:
            job["snapshot"] = {"source": source}
        return job

    table = (
        ("a flushed base", server(wire.BASE_FLUSHED, wire.FLUSH_COMMITTED), outbox.SETTINGS_SAVED_AT_SEND,
         "saved from the WanGP page at send"),
        ("a recorded base whose page did not answer", server(wire.BASE_RECORDED, wire.FLUSH_UNAVAILABLE), outbox.SETTINGS_NO_ANSWER,
         "WanGP's last saved settings (the page didn't answer)"),
        ("a recorded base whose WanGP was loading settings", server(wire.BASE_RECORDED, wire.FLUSH_SUPPRESSED), outbox.SETTINGS_LOADING,
         "WanGP's last saved settings (WanGP was loading a model's settings at send)"),
        ("a recorded base nobody tried to save", server(wire.BASE_RECORDED, ""), outbox.SETTINGS_LAST_SAVED,
         "WanGP's last saved settings"),
        ("factory, inheriting", server(wire.BASE_FACTORY, wire.FLUSH_COMMITTED), outbox.SETTINGS_DEFAULTS,
         "the model's defaults (nothing saved yet)"),
        ("factory, by choice", server(wire.BASE_FACTORY, "", inherit=False), outbox.SETTINGS_OFF,
         "the model's defaults (taking the WanGP page's settings is switched off)"),
        ("a job not composed yet", server(None, wire.FLUSH_COMMITTED), "", ""),
        ("a page-run job WanGP took", {"executor": outbox.EXECUTOR_BROWSER, "state": outbox.QUEUED}, outbox.SETTINGS_LIVE,
         "the WanGP page itself, as it was when this browser queued the job"),
        ("a page-run job still waiting", {"executor": outbox.EXECUTOR_BROWSER, "state": outbox.PENDING}, "", ""),
        ("a legacy document that names no executor", {"state": outbox.STARTED}, outbox.SETTINGS_LIVE,
         "the WanGP page itself, as it was when this browser queued the job"),
    )
    for name, job, key, sentence in table:
        r.check(f"settings source: {name}", outbox.settings_source(job) == key and outbox.settings_sentence(job) == sentence,
                repr((outbox.settings_source(job), outbox.settings_sentence(job))))
    r.check("the vocabulary is closed: every key has its sentence and nothing else is stored",
            set(outbox.SETTINGS_KEYS) == set(outbox.SETTINGS_TEXT) and "" not in outbox.SETTINGS_KEYS)

    notes = {
        "committed": outbox.flush_note(server(None, wire.FLUSH_COMMITTED)),
        "unchanged": outbox.flush_note(server(None, wire.FLUSH_UNCHANGED)),
        "unavailable": outbox.flush_note(server(None, wire.FLUSH_UNAVAILABLE)),
        "suppressed": outbox.flush_note(server(None, wire.FLUSH_SUPPRESSED)),
        "none": outbox.flush_note(server(None, "")),
        "off": outbox.flush_note(server(None, "", inherit=False)),
        "page": outbox.flush_note({"executor": outbox.EXECUTOR_BROWSER, "inherit_settings": True}),
        "page off": outbox.flush_note({"executor": outbox.EXECUTOR_BROWSER, "inherit_settings": False}),
    }
    r.check("the press's own line says what was done about WanGP's form, before anything is composed",
            notes["committed"] == "WanGP's form saved from its page at send"
            and notes["unchanged"] == "WanGP's form already saved from its page"
            and notes["unavailable"] == "the WanGP page did not answer, so its form was not saved at send"
            and notes["suppressed"] == "WanGP was loading a model's settings, so its form was not saved at send"
            and notes["none"] == "WanGP's form was not asked to save at send", repr(notes))
    r.check("and says plainly when the page's settings are not taken at all, or are the page's own",
            notes["off"] == "WanGP's settings are not taken from its page (switched off)"
            and notes["page"] == "WanGP's settings are its page's own when this browser queues it", repr(notes))
    r.check("a job a page runs itself drives the live form whatever the setting says, and its line says so",
            notes["page off"] == notes["page"], repr(notes))


def run() -> Results:
    r = Results("clipboard outbox")
    settings_source_checks(r)
    with tempfile.TemporaryDirectory(prefix="minipaint-outbox-") as scratch:
        base = pathlib.Path(scratch)
        wangp_config.use_config_dir(base / "data")
        config.use_config_dir(base / "data")
        outbox.reset_for_tests()
        clock = _Clock()
        outbox.use_clock(clock)
        try:
            outbox_checks(r, clock)
            outbox.reset_for_tests()
            (config.config_dir() / outbox.OUTBOX_NAME).unlink(missing_ok=True)
            route_checks(r)
        finally:
            outbox.reset_for_tests()
            wangp_config.use_config_dir(None)
            config.use_config_dir(None)
    return r


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
