#!/usr/bin/env python
"""The precedence rules that decide what a run's status may claim.

runs.classify() is the function that stops the registry lying. It is pure and
stdlib-only precisely so that these rules -- the part that is easy to get
subtly wrong and impossible to notice from the outside -- can be checked in
milliseconds against evidence dicts written by hand.

Every case here is one the real registry produced. In particular:

  * a run that submitted 46 jobs and kept saying `held` for three weeks,
    because the turn that would have recorded it died on an API error
  * a proposal that was rejected, regenerated elsewhere, and then re-held
    pointing at the directory the user had just said no to
  * an interrupt still parked on a thread whose proposal has since moved on

The one rule worth stating twice: SUBMISSION EVIDENCE BEATS EVERYTHING. A run
that has spent an approval cannot be awaiting one, whatever the checkpoint
looks like and whatever was last written down.

Run:  python tests/test_classify.py
"""
import sys

from harness import Report

from genpipe import runs


def record(**kw):
    """A held record with a proposal, plus whatever the case overrides."""
    base = {"name": "r", "status": runs.HELD, "source": "agent",
            "thread_id": "t", "proposal": {"generated": "genpipes x -c a -r b"}}
    base.update(kw)
    return runs._normalise(base)


def main():
    r = Report("classify: what the evidence supports")

    # ------------------------------------------------------------------ #
    r.section("the ordinary case")

    got = runs.classify(record(), {"gate": True})
    r.equal("a proposal with a live gate is held", got.status, runs.HELD)
    r.contains("and says why", got.why, "open")

    got = runs.classify(record(), {"gate": False})
    r.equal("without a live gate it lapses", got.status, runs.LAPSED)
    r.contains("naming the reason, not the symptom", got.why, "not left open")

    # ------------------------------------------------------------------ #
    r.section("evidence that was never gathered changes nothing")

    got = runs.classify(record(), {"gate": runs.UNKNOWN})
    r.equal("an unreadable checkpoint leaves the status alone",
            got.status, runs.HELD)
    got = runs.classify(record(), {})
    r.equal("and so does no evidence at all", got.status, runs.HELD)
    # The distinction the UNKNOWN sentinel exists for. If these two collapsed,
    # a locked database would retire every pending decision in the registry.
    r.check("UNKNOWN is not False", runs.UNKNOWN is not False
            and bool(runs.UNKNOWN))

    # ------------------------------------------------------------------ #
    r.section("submission evidence beats everything below it")

    # The 2026-07-29 case, exactly: a live-looking record, a gate that is gone,
    # and 46 jobs on the cluster. Anything but `submitted` here is the bug.
    out = runs.Outcome(runs.SUBMITTED, jobs_seen=46, expected=46,
                       detail="46 jobs were submitted")
    got = runs.classify(record(), {"gate": False, "outcome": out})
    r.equal("a graded submission wins over a missing gate",
            got.status, runs.SUBMITTED)

    got = runs.classify(record(), {"gate": True, "outcome": out})
    r.equal("and over a LIVE one", got.status, runs.SUBMITTED)
    r.check("carrying the outcome through", got.outcome is out)

    got = runs.classify(record(), {"gate": True, "submitted": True})
    r.equal("ungradeable submission evidence still beats a gate",
            got.status, runs.SUBMIT_UNKNOWN)
    r.contains("and is honest that it could not be graded",
               got.why, "could not be graded")

    # ------------------------------------------------------------------ #
    r.section("a settled record is advanced, never churned")

    # Reconciliation runs at every startup. Re-grading an already-settled run
    # from evidence that has aged -- a purged job list, an expired sacct entry
    # -- demoted a correctly submitted run to `submit_unknown` the first time
    # this was tried.
    stale = runs.Outcome(runs.SUBMIT_UNKNOWN, jobs_seen=11, expected=0,
                         detail="does not add up")
    got = runs.classify(record(status=runs.SUBMITTED),
                        {"gate": False, "outcome": stale})
    r.equal("a submitted record is not re-graded", got.status, runs.SUBMITTED)

    got = runs.classify(record(status=runs.SUBMIT_FAILED),
                        {"gate": False, "outcome": stale})
    r.equal("nor is a failed one", got.status, runs.SUBMIT_FAILED)

    # SUBMITTING is the exception, and the only one: it is the single status
    # whose claim is about a moment that has passed.
    got = runs.classify(record(status=runs.SUBMITTING),
                        {"gate": False, "outcome": stale})
    r.equal("but one still in flight is", got.status, runs.SUBMIT_UNKNOWN)

    # ------------------------------------------------------------------ #
    r.section("the gate must be for THIS proposal")

    got = runs.classify(record(revision="aaa"),
                        {"gate": True, "gate_revision": "aaa"})
    r.equal("a gate for the current revision holds it", got.status, runs.HELD)

    got = runs.classify(record(revision="bbb"),
                        {"gate": True, "gate_revision": "aaa"})
    r.equal("a gate for an EARLIER revision does not",
            got.status, runs.LAPSED)
    r.contains("and says so plainly", got.why, "earlier version")

    # Falsy means no opinion -- the rule usage.py already uses. Every record
    # and every interrupt written before identity existed lacks a revision,
    # and none of them may become unapprovable because of it.
    got = runs.classify(record(revision=None),
                        {"gate": True, "gate_revision": "aaa"})
    r.equal("no revision on the record is no opinion", got.status, runs.HELD)
    got = runs.classify(record(revision="aaa"),
                        {"gate": True, "gate_revision": None})
    r.equal("no revision on the gate is no opinion either",
            got.status, runs.HELD)
    got = runs.classify(record(revision="aaa"),
                        {"gate": True, "gate_revision": runs.UNKNOWN})
    r.equal("and an uninspected gate is no opinion", got.status, runs.HELD)

    # ------------------------------------------------------------------ #
    r.section("a rejected proposal can never come back")

    # Checked BEFORE the gate, deliberately: an interrupt for a rejected
    # proposal is exactly the shape of the bug. The graph does not know a
    # person said no; only the record does.
    got = runs.classify(record(revision="aaa", rejected=["aaa"]),
                        {"gate": True, "gate_revision": "aaa"})
    r.equal("even with a live gate authorising it",
            got.status, runs.ABANDONED)
    r.contains("naming the reason", got.why, "rejected")

    got = runs.classify(record(revision="aaa", superseded=["aaa"]),
                        {"gate": True, "gate_revision": "aaa"})
    r.equal("a superseded proposal lapses", got.status, runs.LAPSED)
    r.contains("and says a change replaced it", got.why, "superseded")

    # ------------------------------------------------------------------ #
    r.section("nothing to approve")

    got = runs.classify(record(proposal=None), {"gate": True})
    r.equal("no proposal means lapsed, whatever the graph is doing",
            got.status, runs.LAPSED)

    # ------------------------------------------------------------------ #
    r.section("records this does not own")

    for status in (runs.ABANDONED, runs.GONE):
        got = runs.classify(record(status=status), {"gate": False})
        r.equal(f"{status} is terminal and untouched", got.status, status)

    got = runs.classify(record(source="scan"), {"gate": False})
    r.equal("a scanned run never had a gate and keeps its status",
            got.status, runs.HELD)
    got = runs.classify(record(source="manual", status=runs.SUBMITTED),
                        {"gate": False})
    r.equal("nor did a tracked one", got.status, runs.SUBMITTED)

    # ------------------------------------------------------------------ #
    r.section("the invariant, stated as a test")

    # If a row says "awaiting approval", /approve must work. The only status
    # that renders that way is HELD, so this is the complete list of evidence
    # shapes that may produce it.
    holdable = [
        {"gate": True},
        {"gate": True, "gate_revision": None},
        {"gate": True, "gate_revision": "aaa"},
    ]
    for ev in holdable:
        rec = record(revision="aaa" if ev.get("gate_revision") == "aaa" else None)
        r.equal(f"held for {ev}", runs.classify(rec, ev).status, runs.HELD)

    never = [
        ({"gate": False}, "no gate"),
        ({"gate": True, "outcome": out}, "already submitted"),
        ({"gate": True, "submitted": True}, "ran, ungraded"),
    ]
    for ev, label in never:
        got = runs.classify(record(), ev)
        r.check(f"never held: {label}", got.status != runs.HELD,
                f"got {got.status}")

    # ------------------------------------------------------------------ #
    r.section("submitted ids are counted off the observation as stored")

    # The tag is glued to the first line, so an id-count anchored at line start
    # lost one every time. 46 jobs reported as 45 reads as a partial
    # submission -- which is the one thing a recovered outcome must not
    # invent.
    observation = ("<observation>Submitted job with ID: 111\n"
                   "Submitted job with ID: 222\n"
                   "Submitted job with ID: 333\n</observation>")
    r.equal("the first id is not lost to the observation tag",
            runs.submitted_ids(observation), ["111", "222", "333"])
    r.equal("an empty id is still not counted",
            runs.submitted_ids("<observation>Submitted job with ID:\n"), [])

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
