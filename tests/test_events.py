"""The event spine: the cursor, the ring, the wake, and the presence grace.

Section 6 of the performance design intent turns "ask again in 250 ms" into
"be told". What has to be right for that to be safe is not the telling - it is
everything around a connection that broke while nobody was looking:

  - a cursor from a previous Forge process must be refused rather than
    honoured, because its numbers mean nothing here;
  - a page that blinked must be replayed exactly what it missed, once, in
    order, and a page that has been gone too long must be told to resync
    rather than handed an unbounded replay;
  - a targeted advisory event must reach its own page and no other, and must
    never be replayed to anybody - it offers an opportunity, and a stale
    opportunity is a wasted request;
  - a publisher on a worker thread, holding another module's lock, must never
    block on a subscriber and never raise into the caller;
  - a page whose transport dropped for a moment must keep its turn.

No Forge, no browser, no network.
"""

from harness import Results, setup_path

setup_path()

import asyncio  # noqa: E402

from minipaint_neo import events  # noqa: E402


def cursor_checks(r: Results) -> None:
    events.reset_for_tests(epoch="aaaa0000")
    r.check("a fresh process starts at revision zero", events.revision() == 0)
    r.check("a cursor names the epoch and the revision", events.cursor() == "aaaa0000:0", events.cursor())

    r.check("a cursor parses", events.parse_cursor("aaaa0000:7") == ("aaaa0000", 7))
    for bad in ("", "nonsense", "aaaa0000:", ":7", "aaaa0000:x", None, 7, "aaaa0000:7:9"):
        parsed = events.parse_cursor(bad)
        ok = parsed is None or (bad == "aaaa0000:7:9" and parsed is None)
        r.check(f"an unreadable cursor is None, not an error ({bad!r})", parsed is None, str(parsed))

    rev = events.publish(events.JOB, {"job_id": "abcd1234", "state": "pending"})
    r.check("a durable publish takes the next revision", rev == 1 and events.revision() == 1, str(rev))
    r.check("and the cursor moves with it", events.cursor() == "aaaa0000:1", events.cursor())


def replay_checks(r: Results) -> None:
    events.reset_for_tests(epoch="bbbb0000")
    for index in range(5):
        events.publish(events.JOB, {"job_id": f"job{index}", "state": "pending"})

    records, reset = events.replay("bbbb0000:2")
    r.check("a cursor mid-ring is replayed exactly what it missed",
            not reset and [record["revision"] for record in records] == [3, 4, 5],
            str([record["revision"] for record in records]))

    records, reset = events.replay("bbbb0000:5")
    r.check("a cursor at the head replays nothing and does not reset", not reset and records == [])

    records, reset = events.replay("bbbb0000:9")
    r.check("a cursor ahead of this process resets", reset and records == [])

    records, reset = events.replay("cccc9999:2")
    r.check("a cursor from another epoch resets", reset and records == [])

    records, reset = events.replay("not-a-cursor")
    r.check("an unreadable cursor resets", reset and records == [])

    # Older than the ring still holds.
    events.reset_for_tests(epoch="dddd0000")
    for index in range(events.RING_LIMIT + 20):
        events.publish(events.JOB, {"job_id": str(index)})
    records, reset = events.replay("dddd0000:1")
    r.check("a cursor older than the ring resets rather than replaying unbounded",
            reset and records == [], str(len(records)))
    r.check("and the ring itself stays bounded", len(events._ring) == events.RING_LIMIT, str(len(events._ring)))

    records, reset = events.replay(events.cursor(events.revision() - 3))
    r.check("while a recent cursor still replays", not reset and len(records) == 3, str(len(records)))


def advisory_checks(r: Results) -> None:
    events.reset_for_tests(epoch="eeee0000")
    before = events.revision()
    events.notify("page1", events.CLAIM_READY, {"pending": 2})
    r.check("a targeted advisory event does not advance the revision",
            events.revision() == before, str(events.revision()))
    records, reset = events.replay(f"eeee0000:{before}")
    r.check("and never enters the replay ring", not reset and records == [], str(records))


def delivery_checks(r: Results) -> None:
    async def main() -> None:
        events.reset_for_tests(epoch="ffff0000")
        with events.subscribe("page1") as one, events.subscribe("page2") as two:
            r.check("a subscription registers", events.subscriber_count() == 2, str(events.subscriber_count()))

            events.publish(events.JOB, {"job_id": "shared"})
            first = await one.next(timeout=1.0)
            second = await two.next(timeout=1.0)
            r.check("a durable publish reaches every page",
                    first is not None and second is not None
                    and first["payload"]["job_id"] == "shared"
                    and second["payload"]["job_id"] == "shared", str(first))
            r.check("and carries the revision it was given", first["revision"] == 1, str(first))

            events.notify("page1", events.CLAIM_READY, {"pending": 1})
            mine = await one.next(timeout=1.0)
            r.check("a targeted event reaches its page", mine is not None and mine["kind"] == events.CLAIM_READY, str(mine))
            r.check("and carries no revision", mine["revision"] is None, str(mine))
            theirs = await two.next(timeout=0.05)
            r.check("and reaches no other page", theirs is None, str(theirs))

            r.check("a quiet feed answers None so the caller can send a heartbeat",
                    await one.next(timeout=0.05) is None)

        r.check("leaving the context unregisters both", events.subscriber_count() == 0, str(events.subscriber_count()))
        events.publish(events.JOB, {"job_id": "nobody"})
        r.check("and publishing with nobody listening is fine", events.revision() == 2, str(events.revision()))

    asyncio.run(main())


def thread_publish_checks(r: Results) -> None:
    """The publisher is a worker thread holding somebody else's lock."""

    async def main() -> None:
        events.reset_for_tests(epoch="1111aaaa")
        with events.subscribe("page1") as feed:
            done = asyncio.Event()
            loop = asyncio.get_running_loop()

            def worker() -> None:
                # Exactly what outbox._save() will do: publish from off the loop.
                events.publish(events.JOB, {"job_id": "fromthread", "state": "queued"})
                loop.call_soon_threadsafe(done.set)

            import threading

            thread = threading.Thread(target=worker)
            thread.start()
            await asyncio.wait_for(done.wait(), 2.0)
            thread.join(2.0)

            record = await feed.next(timeout=1.0)
            r.check("a publish from another thread wakes the loop's subscriber",
                    record is not None and record["payload"]["job_id"] == "fromthread", str(record))

    asyncio.run(main())


def overflow_checks(r: Results) -> None:
    async def main() -> None:
        events.reset_for_tests(epoch="2222aaaa")
        with events.subscribe("slow") as feed:
            for index in range(events.QUEUE_LIMIT + 25):
                events.publish(events.JOB, {"job_id": str(index)})
            await asyncio.sleep(0)  # let the queued wake-ups run
            r.check("a subscriber that never reads is capped, not grown without bound",
                    feed.queue.qsize() <= events.QUEUE_LIMIT, str(feed.queue.qsize()))
            r.check("and is marked for a resync rather than waited for", feed.overflowed)

    asyncio.run(main())


def presence_checks(r: Results) -> None:
    events.reset_for_tests(epoch="3333aaaa")
    clock = {"now": 1000.0}
    events.use_clock(lambda: clock["now"])
    try:
        r.check("a page nobody has seen is not active", not events.active("page1"))
        events.connected("page1")
        r.check("a connected page is active", events.active("page1"))

        events.disconnected("page1")
        clock["now"] += events.PAGE_ACTIVE_SECONDS - 1
        r.check("a page that dropped is still active inside the grace period", events.active("page1"))
        r.check("and is listed as active", events.active_pages() == ["page1"], str(events.active_pages()))

        clock["now"] += 2
        r.check("and is inactive once the grace period passes", not events.active("page1"))
        r.check("and is no longer listed", events.active_pages() == [], str(events.active_pages()))

        events.connected("page1")
        r.check("reconnecting makes it active again", events.active("page1"))

        events.forget("page1")
        r.check("and a forgotten page is gone", not events.active("page1"))
    finally:
        events.use_clock(None)


def privacy_checks(r: Results) -> None:
    """An event is a description, not a record. The payload carries ids and
    states; test_logging_privacy holds the call sites to it, and this holds
    the shape the hub itself produces."""
    events.reset_for_tests(epoch="4444aaaa")
    events.publish(events.JOB, {"job_id": "abcd1234", "state": "queued"})
    records, _ = events.replay("4444aaaa:0")
    record = records[0]
    r.check("a record is kind, revision, payload and a time",
            set(record) == {"kind", "revision", "payload", "at"}, str(sorted(record)))
    r.check("and the payload is copied, so a later mutation cannot rewrite history",
            record["payload"] is not None and record["payload"]["job_id"] == "abcd1234")

    original = {"job_id": "mutable", "state": "pending"}
    events.publish(events.JOB, original)
    original["state"] = "changed"
    records, _ = events.replay("4444aaaa:1")
    r.check("an event already in the ring is not altered by its caller's dict",
            records[-1]["payload"]["state"] == "pending", str(records[-1]))


def run() -> Results:
    r = Results("events")
    try:
        cursor_checks(r)
        replay_checks(r)
        advisory_checks(r)
        delivery_checks(r)
        thread_publish_checks(r)
        overflow_checks(r)
        presence_checks(r)
        privacy_checks(r)
    finally:
        events.reset_for_tests()
        events.use_clock(None)
    return r


if __name__ == "__main__":
    import sys

    sys.exit(0 if run().report() else 1)
