#!/usr/bin/env python
"""Runs and jobs: the registry's lifecycle, and the job layer's parsing.

Covers the bookkeeping the whole monitoring half rests on, without a cluster and
without biomni -- runs.py is stdlib-only, so this runs in CI in about a second.

The cases chosen are the ones where being wrong is expensive and silent:

  * a held run surviving into a later session (the gate's actual promise)
  * a reused name shadowing rather than destroying the earlier run
  * an OLD runs.jsonl, written before status/workdir existed, still reading
    correctly -- there is a live one of these on a cluster right now
  * a purged job list turning a run gone without deleting its history
  * triage grouping forty identical failures into one finding instead of forty

Run:  python tests/test_runs.py
"""
import os
import shutil
import sys
import tempfile

from harness import Report

from genpipe import runs


def make_job_list(path, rows):
    """Write a job_list in GenPipes' shape: id, name, log, state."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for jid, name, log, state in rows:
            f.write(f"{jid}\t{name}\t{log}\t{state}\n")


def _since_checks(r):
    """Runs whose OUTCOME nobody has looked at, without asking Slurm.

    Deliberately not a diff against the last launch. Everything the registry
    knows offline is something the person did themselves -- they held it, they
    submitted it -- so "2 submitted since you were last here" is a list of
    things they already watched happen. What is genuinely unseen is how those
    runs turned out, and asking the scheduler for that would put seconds of
    `module load` in front of the first prompt.
    """
    r.section("what nobody has seen the answer to yet")

    with tempfile.TemporaryDirectory() as tmp:
        registry = runs.Registry(tmp)
        r.equal("the first ever launch has no previous session",
                registry.seen_at(), "")
        registry.mark_seen()
        r.truthy("and one is recorded on the way in", registry.seen_at())

        proposal = {"command": "bash cmd.sh", "slots": {"pipeline": "dnaseq"}}
        registry.hold("waiting", "chat-1", proposal, tmp)
        # A real job-list file: a submitted run whose list has vanished is
        # pruned to `gone`, and gone runs are not somebody's problem any more.
        joblist = os.path.join(tmp, "jl")
        open(joblist, "w").close()
        registry.mark_submitted("sent", joblist)

        unseen = registry.unseen()
        r.equal("a submitted run nobody has checked is worth a prompt",
                [x["name"] for x in unseen["unfinished"]], ["sent"])
        # A held run is already on the line above it, and it is not an outcome
        # -- it is a decision, which is a different thing to be nagged about.
        r.check("a held run is not one of these",
                "waiting" not in [x["name"] for x in unseen["unfinished"]])

        # A failure a previous /check cached. Stale by definition, and the
        # caller says so -- but it is the thing most likely to have been closed
        # and forgotten, so it is worth raising again.
        registry.remember_check("sent", {"FAILED": 2}, 5,
                                runs.verdict({"FAILED": 2}))
        unseen = registry.unseen()
        r.equal("a cached failure is raised again",
                [x["name"] for x in unseen["failed"]], ["sent"])
        r.equal("and is not double-counted as unchecked",
                unseen["unfinished"], [])

        # Once it is finished and known finished, there is nothing left to say.
        registry.remember_check("sent", {"COMPLETED": 5}, 5,
                                runs.verdict({"COMPLETED": 5}))
        done = registry.unseen()
        r.equal("a completed run drops off entirely", done["failed"], [])
        r.equal("from both lists", done["unfinished"], [])

        # Still running is still unanswered: the verdict is a snapshot, and the
        # run has moved on since it was taken.
        registry.remember_check("sent", {"RUNNING": 3}, 5,
                                runs.verdict({"RUNNING": 3}))
        r.equal("a run that was still going stays worth checking",
                [x["name"] for x in registry.unseen()["unfinished"]], ["sent"])

    # A broken or missing mark must never stop the app starting -- this is
    # decoration on one line, and it is read before anything else happens.
    with tempfile.TemporaryDirectory() as tmp:
        registry = runs.Registry(tmp)
        os.mkdir(registry.seen_path)      # a directory where a file should be
        r.equal("an unreadable mark reads as no session", registry.seen_at(), "")
        registry.mark_seen()              # must not raise
        r.check("and writing one cannot crash the launch", True)


def _status(**kw):
    """A RunStatus with sensible defaults, so each test only states the
    fields it actually cares about."""
    defaults = dict(counts={}, total=0, resolved=0, unknown=0,
                    finished=False, verdict="", doomed=0, source="sacct")
    defaults.update(kw)
    return runs.RunStatus(**defaults)


def _list_bucket_checks(r):
    """/list's classification: one function, five buckets, shared with
    /check all so the two can never disagree about what "needs attention"
    means. See runs.list_bucket()'s docstring for the ordering this asserts.
    """
    r.section("list_bucket(): which of /list's five sections a run lands in")

    held = {"status": "held"}
    r.equal("a held run is HELD, whatever status happens to be passed",
            runs.list_bucket(held, _status(finished=True)), runs.HELD_BUCKET)
    r.equal("HELD is checked before status is even looked at",
            runs.list_bucket(held, None), runs.HELD_BUCKET)

    submitted = {"status": "submitted"}
    r.equal("no status and a submitted record: every step was up to date",
            runs.list_bucket(submitted, None), runs.FINISHED_BUCKET)

    r.equal("a scheduler that could not be reached is UNAVAILABLE, not a "
            "verdict about the run",
            runs.list_bucket(submitted, _status(source="unavailable")),
            runs.UNAVAILABLE_BUCKET)

    r.equal("purely active jobs are LIVE",
            runs.list_bucket(submitted,
                             _status(counts={"RUNNING": 3, "PENDING": 2})),
            runs.ACTIVE_BUCKET)

    r.equal("a broken job is ATTENTION even with nothing else wrong",
            runs.list_bucket(submitted, _status(counts={"FAILED": 1})),
            runs.ATTENTION_BUCKET)

    r.equal("a doomed (DependencyNeverSatisfied) job is ATTENTION",
            runs.list_bucket(submitted, _status(counts={"PENDING": 1}, doomed=1)),
            runs.ATTENTION_BUCKET)

    r.equal("an UNKNOWN job is ATTENTION, never read as healthy",
            runs.list_bucket(submitted, _status(counts={"RUNNING": 1}, unknown=1)),
            runs.ATTENTION_BUCKET)

    # The exact case the redesign was asked to fix: some jobs failed, others
    # are still running. This must not be LIVE -- it already needs a person.
    r.equal("mixed active + failed is ATTENTION, never LIVE",
            runs.list_bucket(submitted,
                             _status(counts={"FAILED": 3, "RUNNING": 2})),
            runs.ATTENTION_BUCKET)

    r.equal("cleanly finished, nothing broken, is FINISHED",
            runs.list_bucket(submitted,
                             _status(counts={"COMPLETED": 4}, finished=True)),
            runs.FINISHED_BUCKET)

    # A job list that parsed to nothing. Every tally below is zero, so without
    # its own check this falls through to LIVE and the listing claims work is
    # queued that does not exist.
    r.equal("an empty manifest is FINISHED, never LIVE",
            runs.list_bucket(submitted, _status(counts={}, total=0)),
            runs.FINISHED_BUCKET)

    r.section("list_tag(): the word each /list row is tagged with")

    r.equal("held", runs.list_tag(held, None), "held")
    r.equal("live", runs.list_tag(submitted,
                                  _status(counts={"RUNNING": 2})), "live")
    r.equal("needs attention",
            runs.list_tag(submitted, _status(counts={"FAILED": 1})),
            "needs attention")
    r.equal("completed",
            runs.list_tag(submitted,
                          _status(counts={"COMPLETED": 4}, finished=True)),
            "completed")
    # Nothing broke, so it is not ATTENTION, and it is over, so it lands in
    # FINISHED -- but a run somebody stopped must never be reported as a
    # success.
    r.equal("a stopped run is tagged cancelled, not completed",
            runs.list_tag(submitted,
                          _status(counts={"COMPLETED": 4, "CANCELLED": 6},
                                  finished=True)),
            "cancelled")
    r.equal("status unavailable",
            runs.list_tag(submitted, _status(source="unavailable")),
            "status unavailable")

    r.section("list_line(): the one-line summary for LIVE and ATTENTION rows")

    r.equal("None for anything with no status",
            runs.list_line(None), None)
    r.contains("a live run reports running/queued/completed",
               runs.list_line(_status(counts={"RUNNING": 6, "COMPLETED": 9})),
               "6 running")
    r.contains("and completed counts, together",
               runs.list_line(_status(counts={"RUNNING": 6, "COMPLETED": 9})),
               "9 completed")
    r.contains("a fully dead run keeps the count and says nothing is left",
               runs.list_line(_status(counts={"FAILED": 2})),
               "2 failed  ·  nothing still running")
    r.contains("a mixed run names the cause instead of just 'failed'",
               runs.list_line(_status(counts={"FAILED": 3, "RUNNING": 2})),
               "3 failed")
    r.contains("and says what of it is still burning allocation",
               runs.list_line(_status(counts={"FAILED": 3, "RUNNING": 2})),
               "2 still running")
    r.contains("a stopped run counts its cancellations",
               runs.list_line(_status(counts={"COMPLETED": 4, "CANCELLED": 6},
                                      finished=True)),
               "6 cancelled")
    r.contains("a doomed run says so, not just 'failed'",
               runs.list_line(_status(counts={"PENDING": 1}, doomed=2)),
               "will never run")


def _trace_owner_checks(r):
    """Which tracked run a past-run config came from -- and when that cannot be
    said at all.

    A trace names the script it generated (`-g cmd.sh` in its header) and a run
    record names the same thing in proposal["script"], which is a real link and
    NOT a unique one: a regeneration overwrites the script under the same name.
    In the directory this was designed against, two traces sixteen minutes apart
    both name dnaseq_somatic_fastpass_cit.sh. Choosing one of those two runs
    would be an association invented rather than established.
    """
    r.section("associating a past config with the run that wrote it")
    here, there = "/proj/run-a", "/proj/run-b"
    records = [
        {"name": "germline-0804", "workdir": here, "status": "submitted",
         "proposal": {"script": "germline.sh"}, "held_at": "2026-08-04"},
        {"name": "cit-a", "workdir": here, "status": "submitted",
         "proposal": {"script": "cit.sh"}, "held_at": "2026-07-30"},
        {"name": "cit-b", "workdir": here, "status": "failed",
         "proposal": {"script": "cit.sh"}, "held_at": "2026-07-30"},
        {"name": "elsewhere", "workdir": there, "status": "submitted",
         "proposal": {"script": "germline.sh"}, "held_at": "2026-06-01"},
    ]

    def owner(script, where=here):
        got = runs.trace_owner({"script": script, "path": f"{where}/t.ini"},
                               records, where)
        return got["name"] if got else None

    r.equal("exactly one match is the run", owner("germline.sh"),
            "germline-0804")
    r.equal("a full path in the trace still matches by basename",
            owner("/some/where/germline.sh"), "germline-0804")
    r.equal("TWO matches is not a match", owner("cit.sh"), None)
    r.equal("and no match is not a match", owner("nothing.sh"), None)
    r.equal("a trace with no script names no run", owner(""), None)
    r.equal("the directory is part of the evidence",
            owner("germline.sh", there), "elsewhere")
    r.equal("no records at all is None, not an error",
            runs.trace_owner({"script": "germline.sh"}, [], here), None)

    # NOTHING ELSE COUNTS AS EVIDENCE. A record whose pipeline or protocol
    # happens to match is not thereby the run that wrote a trace, and this must
    # never start reading them.
    r.equal("a matching pipeline is not evidence",
            runs.trace_owner({"script": "unknown.sh", "pipeline": "dnaseq"},
                             records, here), None)

    r.section("which directories 'search other tracked runs' may look in")
    # The registry's own, and nothing else: these are directories this app knows
    # about because it recorded a run in each. It never walks a project space.
    seen = runs.tracked_workdirs(records + [{"name": "x", "workdir": None}])
    r.check("only directories that exist are offered",
            all(os.path.isdir(d) for d in seen), seen)
    real = tempfile.mkdtemp(prefix="trace-where-")
    try:
        got = runs.tracked_workdirs([
            {"name": "a", "workdir": real, "held_at": "2026-01-01"},
            {"name": "b", "workdir": real, "held_at": "2026-02-01"},
            {"name": "c", "workdir": None, "held_at": "2026-03-01"}])
        r.equal("each directory once, however many runs are in it",
                got, [os.path.abspath(real)])
        r.equal("and a record with no directory contributes none",
                runs.tracked_workdirs([{"name": "c"}]), [])
    finally:
        shutil.rmtree(real, ignore_errors=True)


def main():
    r = Report("runs and jobs")
    workdir = tempfile.mkdtemp(prefix="genpipe_runs_test_")
    try:
        reg = runs.Registry(workdir)

        # ---------------------------------------------------------------- #
        r.section("a held run survives the session that created it")
        proposal = {"command": "bash cmd.sh",
                    "slots": {"pipeline": "rnaseq", "protocol": "stringtie"}}
        reg.hold("patient-42", "patient-42", proposal, workdir)

        # A fresh Registry object stands in for a completely new process: the
        # only thing carried over is the file on disk, which is the point.
        fresh = runs.Registry(workdir)
        held = fresh.held()
        r.equal("one run is waiting for approval", len(held), 1)
        r.equal("under the name it was given", held[0]["name"], "patient-42")
        r.equal("with the command that needs approving",
                held[0]["proposal"]["command"], "bash cmd.sh")
        r.contains("and it appears in /list", str(fresh.live()), "patient-42")

        r.section("re-reaching the gate updates, it does not duplicate")
        reg.hold("patient-42", "patient-42",
                 {"command": "bash cmd2.sh", "slots": {}}, workdir)
        r.equal("still one held record", len(reg.held()), 1)
        r.equal("carrying the newer proposal",
                reg.get("patient-42")["proposal"]["command"], "bash cmd2.sh")

        # ---------------------------------------------------------------- #
        r.section("a conversation is not a run, and can produce several")

        # One thread, two runs. This is what per-conversation threading buys and
        # what the registry has to keep straight: the name identifies the run
        # forever, the thread only says which conversation produced it.
        reg.hold("second-run", "patient-42",
                 {"command": "bash other.sh", "slots": {}}, workdir)
        both = [x["name"] for x in reg.held()]
        r.equal("both runs are held", sorted(both), ["patient-42", "second-run"])

        # held_for_thread is what stops a rejected-and-rethought run acquiring a
        # second name, so it must answer with a run, not with the thread.
        found = reg.held_for_thread("patient-42")
        r.truthy("the thread maps back to a held run", found is not None)
        r.contains("and it is one of them", str(sorted(both)), found["name"])
        r.equal("a thread with nothing waiting maps to nothing",
                reg.held_for_thread("chat-nothing-here"), None)
        r.equal("and neither does no thread at all",
                reg.held_for_thread(None), None)
        reg.mark_submitted("second-run", None, workdir=workdir)

        # ---------------------------------------------------------------- #
        r.section("approval promotes held -> submitted")
        listing = os.path.join(workdir, "job_output", "RnaSeq.stringtie.job_list.2026")
        make_job_list(listing, [
            ("41000001", "trimmomatic.sampleA", "trimmomatic/t.sampleA_41000001.o", "COMPLETED"),
            ("41000002", "trimmomatic.sampleB", "trimmomatic/t.sampleB_41000002.o", "COMPLETED"),
            ("41000003", "picard_mark_duplicates.sampleA", "picard/p.sampleA_41000003.o", "OUT_OF_MEMORY"),
            ("41000004", "picard_mark_duplicates.sampleB", "picard/p.sampleB_41000004.o", "OUT_OF_MEMORY"),
        ])
        reg.mark_submitted("patient-42", listing, workdir=workdir)
        rec = reg.get("patient-42")
        r.equal("status advanced", rec["status"], runs.SUBMITTED)
        r.equal("job list linked", rec["job_list"], listing)
        r.truthy("submission timestamped", rec["submitted_at"])
        r.equal("nothing left held", len(reg.held()), 0)
        r.equal("workdir pinned, so logs are findable later",
                rec["workdir"], workdir)

        r.section("a submission that created no jobs is done, not held, not gone")
        reg.hold("already-done", "already-done", proposal, workdir)
        reg.mark_submitted("already-done", None, workdir=workdir)
        r.equal("promoted despite having no job list",
                reg.get("already-done")["status"], runs.SUBMITTED)
        r.check("no longer awaiting approval",
                "already-done" not in [x["name"] for x in reg.held()])
        r.check("and not mislabelled as purged",
                "already-done" in [x["name"] for x in reg.live()])

        # ---------------------------------------------------------------- #
        # 2026-07-27: a 46-job dnaseq run reported "created no jobs". It had
        # been generated with -o, so GenPipes wrote its list one directory
        # below the cwd, and the search only looked in the cwd. Every case
        # below is that incident or the guard that must survive fixing it.
        r.section("finding the job list a submission just wrote")
        root = os.path.join(workdir, "submission")
        os.makedirs(root, exist_ok=True)
        # Fixed timestamps rather than time.time(): a filesystem that stores
        # mtimes to the second would otherwise make this race its own setup.
        since, older = 2_000_000_000, 1_999_999_000

        buried = os.path.join(root, "cit_run", "job_output",
                              "DnaSeq.somatic_fastpass.job_list.2026")
        make_job_list(buried, [("41000010", "picard.s1", "picard/s1.o", "PENDING")])
        os.utime(buried, (since + 10, since + 10))

        r.equal("a list written under -o is found, not missed",
                runs.find_job_list(root, since, output_dir="cit_run"), buried)
        r.equal("...and found even when nobody recorded the -o",
                runs.find_job_list(root, since), buried)

        script = os.path.join(root, "cmd.sh")
        elsewhere = os.path.join(workdir, "declared", "job_output",
                                 "RnaSeq.default.job_list.2026")
        make_job_list(elsewhere, [("41000020", "trim.s1", "trim/s1.o", "PENDING")])
        os.utime(elsewhere, (since + 5, since + 5))
        with open(script, "w") as f:
            f.write("#!/bin/bash\n"
                    f"OUTPUT_DIR={os.path.join(workdir, 'declared')}\n"
                    "JOB_OUTPUT_DIR=$OUTPUT_DIR/job_output\n")
        r.equal("the script's own OUTPUT_DIR is read",
                runs.output_dir_of(script), os.path.join(workdir, "declared"))
        r.truthy("and a list outside the cwd entirely is still found",
                 runs.find_job_list(root, since, script=script) in
                 (elsewhere, buried))

        # The guard that makes widening the search safe. Without it a run that
        # created nothing adopts the previous run's jobs and reports them as
        # its own -- which is worse than saying nothing at all.
        stale = os.path.join(root, "old_run", "job_output",
                             "DnaSeq.germline_snv.job_list.2025")
        make_job_list(stale, [("40000001", "trim.old", "trim/old.o", "COMPLETED")])
        os.utime(stale, (older, older))
        r.equal("a list from a previous run is never adopted",
                runs.find_job_list(os.path.join(root, "old_run"), since), None)

        empty = os.path.join(workdir, "quiet")
        os.makedirs(empty, exist_ok=True)
        r.equal("a submission that really wrote nothing stays None",
                runs.find_job_list(empty, since), None)
        r.equal("a missing script is not an error", runs.output_dir_of(
                os.path.join(empty, "nope.sh")), None)

        # ---------------------------------------------------------------- #
        r.section("reusing a name shadows, it does not destroy")
        r.equal("a free name is returned unchanged",
                reg.unique_name("brand-new"), "brand-new")
        r.equal("a taken name is advanced",
                reg.unique_name("patient-42"), "patient-42-2")
        reg.mark_submitted("patient-42-2", listing, workdir=workdir)
        r.equal("and again", reg.unique_name("patient-42"), "patient-42-3")
        r.contains("the original is still in history",
                   str([x["name"] for x in reg.all()]), "patient-42")

        # ---------------------------------------------------------------- #
        r.section("the job layer reads the run's jobs")
        jobs = runs.parse_job_list(listing)
        r.equal("all four jobs found", len(jobs), 4)
        r.equal("id parsed", jobs[0].job_id, "41000001")
        r.equal("name parsed", jobs[0].name, "trimmomatic.sampleA")
        r.equal("step derived from the name", jobs[2].step, "picard_mark_duplicates")
        r.equal("state is unknown until Slurm is asked", jobs[0].state, None)
        r.check("...and unknown does not read as healthy", not jobs[0].failed)

        r.section("a whitespace-separated job list still parses")
        loose = os.path.join(workdir, "job_output", "loose.job_list.2026")
        with open(loose, "w") as f:
            f.write("41000009 trimmomatic.sampleZ trimmomatic/x_41000009.o COMPLETED\n")
            f.write("\n")                       # blank lines are skipped
            f.write("# a comment\n")            # so are comments
        loose_jobs = runs.parse_job_list(loose)
        r.equal("one job, two ignored lines", len(loose_jobs), 1)
        r.equal("id still found", loose_jobs[0].job_id, "41000009")

        # ---------------------------------------------------------------- #
        r.section("triage groups correlated failures")
        for j in jobs:
            # Stand in for sacct: the states are in the fixture's 4th column.
            j.state = {"41000001": "COMPLETED", "41000002": "COMPLETED",
                       "41000003": "OUT_OF_MEMORY",
                       "41000004": "OUT_OF_MEMORY"}[j.job_id]
        os.makedirs(os.path.join(workdir, "job_output", "picard"), exist_ok=True)
        with open(os.path.join(workdir, "job_output", "picard",
                               "p.sampleA_41000003.o"), "w") as f:
            f.write("java.lang.OutOfMemoryError: Java heap space\n")

        report = runs.triage(rec, jobs=jobs)
        r.equal("both failures counted", report["failed_total"], 2)
        r.equal("but reported as ONE step, not two jobs",
                report["steps_affected"], 1)
        r.equal("with the count kept", report["findings"][0]["count"], 2)
        r.equal("named by step", report["findings"][0]["step"],
                "picard_mark_duplicates")
        r.contains("and the log actually read",
                   report["findings"][0]["log_tail"], "OutOfMemoryError")

        r.section("a cancelled-downstream job is not counted as a failure")
        # One failing step cancels everything after it in a GenPipes DAG, so
        # rolling the two together reports many problems where there is one --
        # and hands a diagnosing model logs that explain nothing.
        dag = os.path.join(workdir, "job_output", "dag.job_list.2026")
        make_job_list(dag, [
            ("42000001", "trimmomatic.sampleA", "trimmomatic/a.o", "COMPLETED"),
            ("42000002", "picard_mark_duplicates.sampleA", "picard/b.o", "OUT_OF_MEMORY"),
            ("42000003", "gatk_haplotype_caller.sampleA", "gatk/c.o", "CANCELLED"),
            ("42000004", "metrics_dna_picard.sampleA", "metrics/d.o", "CANCELLED"),
        ])
        dag_jobs = runs.parse_job_list(dag)
        for j in dag_jobs:
            j.state = {"42000001": "COMPLETED", "42000002": "OUT_OF_MEMORY",
                       "42000003": "CANCELLED", "42000004": "CANCELLED"}[j.job_id]
        dag_report = runs.triage({"job_list": dag, "workdir": workdir},
                                 jobs=dag_jobs)
        r.equal("three jobs need attention", dag_report["failed_total"], 3)
        r.equal("but only one actually broke", dag_report["broke_total"], 1)
        r.equal("and two were cancelled downstream",
                dag_report["cancelled_total"], 2)
        r.equal("the step that broke is diagnosed FIRST",
                dag_report["findings"][0]["step"], "picard_mark_duplicates")
        r.check("not a cancelled one, whose log explains nothing",
                dag_report["findings"][0]["state"] != "CANCELLED")

        r.section("counts and verdict")
        tally = runs.counts(jobs)
        r.equal("tallied by state", tally,
                {"COMPLETED": 2, "OUT_OF_MEMORY": 2})
        r.equal("verdict leads with the problem",
                runs.verdict(tally), "2 need attention")
        r.equal("a clean run says so",
                runs.verdict({"COMPLETED": 9}), "complete")
        r.equal("a live run counts what's moving",
                runs.verdict({"COMPLETED": 4, "RUNNING": 2, "PENDING": 3}),
                "5 running")
        r.equal("unknown is not silently 'complete'",
                runs.verdict({"UNKNOWN": 3}), "state unknown")

        # ---------------------------------------------------------------- #
        r.section("a purged job list makes a run gone, not deleted")
        os.remove(listing)
        live_names = [x["name"] for x in reg.live()]
        r.check("drops out of /list", "patient-42" not in live_names)
        r.contains("but survives in /history",
                   str([x["name"] for x in reg.all()]), "patient-42")
        r.equal("marked gone", reg.get("patient-42")["status"], runs.GONE)
        r.truthy("with a timestamp", reg.get("patient-42").get("gone_at"))

        # ---------------------------------------------------------------- #
        r.section("notes and cached checks accumulate on the record")
        reg.remember_check("patient-42", {"COMPLETED": 2, "OUT_OF_MEMORY": 2}, 4,
                           "2 need attention")
        reg.add_note("patient-42", "OOM in picard_mark_duplicates: raise java heap")
        rec = reg.get("patient-42")
        r.equal("snapshot verdict cached",
                rec["last_check"]["verdict"], "2 need attention")
        r.truthy("snapshot timestamped", rec["last_check"]["at"])
        r.contains("finding kept for later", str(rec["notes"]), "OOM in picard")

        # ---------------------------------------------------------------- #
        r.section("an OLD runs.jsonl still reads (one exists on a cluster now)")
        legacy_dir = tempfile.mkdtemp(prefix="genpipe_legacy_")
        with open(os.path.join(legacy_dir, "runs.jsonl"), "w") as f:
            f.write('{"name": "old-live", "thread_id": "old-live", '
                    f'"job_list": "{listing}", "submitted_at": "2026-06-01T10:00:00", '
                    '"source": "agent", "gone": false}\n')
            f.write('{"name": "old-gone", "thread_id": null, '
                    '"job_list": "/nowhere/x.job_list.1", '
                    '"submitted_at": "2026-06-02T10:00:00", '
                    '"source": "manual", "gone": true}\n')
        legacy = runs.Registry(legacy_dir)
        old = {x["name"]: x for x in legacy.all()}
        r.equal("a record with no status is a submission (then pruned to gone, "
                "since its file is gone too)",
                old["old-live"]["status"], runs.GONE)
        r.equal("an old gone record stays gone", old["old-gone"]["status"], runs.GONE)
        r.equal("both records kept", len(old), 2)
        r.check("no crash on missing new fields",
                old["old-gone"]["workdir"] is None
                and old["old-gone"]["proposal"] is None)
        shutil.rmtree(legacy_dir, ignore_errors=True)

        r.section("a truncated final line costs one record, not the file")
        broken_dir = tempfile.mkdtemp(prefix="genpipe_broken_")
        with open(os.path.join(broken_dir, "runs.jsonl"), "w") as f:
            f.write('{"name": "intact", "job_list": null, "gone": false}\n')
            f.write('{"name": "trunca')       # crash mid-write
        r.equal("the good record still loads",
                len(runs.Registry(broken_dir).load()), 1)
        shutil.rmtree(broken_dir, ignore_errors=True)

        # ---------------------------------------------------------------- #
        r.section("log_report parsing")
        raw = """----------------------------------------
Number of jobs: 15
Number of jobs COMPLETED: 12
Number of jobs OUT_OF_MEMORY: 3
Cumulative time spent on compute nodes: 3:24:11
Cumulative core time: 27:13:28
Human time spent on this pipeline: 0:41:02
----------------------------------------"""
        parsed = runs.parse_log_report(raw)
        r.equal("total read", parsed["total"], 15)
        r.equal("per-state counts read", parsed["counts"],
                {"COMPLETED": 12, "OUT_OF_MEMORY": 3})
        r.equal("timings kept", len(parsed["meta"]), 3)
        r.equal("unparseable output yields total 0, never a fake bar",
                runs.parse_log_report("module: command not found")["total"], 0)

        # ---------------------------------------------------------------- #
        r.section("suggested run names")
        r.equal("prose becomes a slug",
                runs.suggest_name("run dnaseq germline_snv on my readset, all steps",
                                  when=__import__("datetime").date(2026, 7, 25)),
                "dnaseq-germline-snv-0725")
        r.equal("an empty task still yields a usable name",
                runs.suggest_name("", when=__import__("datetime").date(2026, 7, 25)),
                "run-0725")

        _since_checks(r)
        _list_bucket_checks(r)
        _reconcile_checks(r)
        _stale_submission_checks(r)
        _trace_owner_checks(r)

        return r.finish()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _script(path, total=None, pipefail=True, outdir=None, stamp="2026-08-12T10.00.00"):
    """A generated script's header, in the shape GenPipes really writes it.

    Only the header matters here: reconcile() reads the declared total, the
    `set` line and the JOB_LIST path, and never executes anything.
    """
    outdir = outdir or os.path.dirname(path)
    lines = ["#!/bin/bash", "# Exit immediately on error", ""]
    lines.append("set -eu -o pipefail" if pipefail else "set -eu")
    lines.append("")
    if total is not None:
        lines.append(f"#   TOTAL: {total} jobs")
    lines += [f"OUTPUT_DIR={outdir}",
              "JOB_OUTPUT_DIR=$OUTPUT_DIR/job_output",
              f"TIMESTAMP={stamp}",
              "JOB_LIST=$JOB_OUTPUT_DIR/DnaSeq.somatic_fastpass.job_list.$TIMESTAMP",
              ""]
    with open(path, "w") as f:
        f.write("\n".join(lines))
    return path


def _reconcile_checks(r):
    """Did the submission happen? Every cell of the evidence table.

    This is the suite for the 2026-07-29 defect and its whole family. A real
    run put 46 jobs on Rorqual, the turn that would have recorded them died on
    an API error, and the registry said `held` a fortnight later -- so /approve
    offered it again and /check refused to look at it. Nothing here needs a
    cluster: reconcile() is pure over evidence gathered elsewhere.

    The two rules being pinned, both of which cost real work when they were
    absent:

      exit 0 is not on its own proof of success. It is accepted only when the
      rows added match the total the script declared, or when the script's own
      `pipefail` semantics make a clean exit mean every sbatch returned 0.

      a failure with no new rows is not proof that nothing reached Slurm.
      GenPipes runs `sbatch` and appends the row as two separate statements, so
      `retry_safe` is never inferred from a count -- only from asking Slurm.
    """
    r.section("reconcile(): what the evidence actually establishes")
    work = tempfile.mkdtemp(prefix="genpipe_rec_")
    try:
        listing = os.path.join(work, "job_output",
                               "DnaSeq.somatic_fastpass.job_list.2026-08-12T10.00.00")
        script = _script(os.path.join(work, "cmd.sh"), total=3, outdir=work)

        r.equal("the declared total is read off the header",
                runs.expected_jobs(script), 3)
        r.truthy("and so is pipefail", runs.has_pipefail(script))
        r.equal("the job list path is resolved from the header, not globbed",
                runs.declared_job_list(script), listing)

        # -- a clean, complete submission -------------------------------- #
        before = runs.job_list_state(listing)
        r.equal("a baseline over a file that does not exist yet is zero rows",
                (before["rows"], before["identity"]), (0, None))
        make_job_list(listing, [("1", "a.s", "step/a.o", ""),
                                ("2", "b.s", "step/b.o", ""),
                                ("3", "c.s", "step/c.o", "")])
        after = runs.job_list_state(listing)
        out = runs.reconcile(script=script,
                             observation="Submitted job with ID: 1\n"
                                         "Submitted job with ID: 2\n"
                                         "Submitted job with ID: 3\n",
                             baseline=before, after=after)
        r.equal("3 of 3 rows and a clean exit is submitted", out.status,
                runs.SUBMITTED)
        r.equal("with the count recorded", out.jobs_seen, 3)

        # -- ROWS ARE A DELTA, NOT A TOTAL ------------------------------- #
        # The case that makes a baseline necessary: GenPipes appends, the
        # TIMESTAMP is baked into the script, so retrying one script writes
        # into the very same list as the attempt that failed.
        rerun_before = runs.job_list_state(listing)
        r.equal("a second approval starts from 3 rows already present",
                rerun_before["rows"], 3)
        with open(listing, "a") as f:
            f.write("4\td.s\t\tstep/d.o\n")
            f.write("5\te.s\t\tstep/e.o\n")
            f.write("6\tf.s\t\tstep/f.o\n")
        rerun_after = runs.job_list_state(listing)
        out = runs.reconcile(script=script, observation="ok",
                             baseline=rerun_before, after=rerun_after)
        r.equal("only the rows this approval added are counted", out.jobs_seen, 3)
        r.equal("so a rerun into the same list still reconciles", out.status,
                runs.SUBMITTED)

        # A file replaced underneath us cannot be differenced.
        swapped = dict(rerun_before)
        swapped["identity"] = (rerun_before["identity"][0],
                               (rerun_before["identity"][1] or 0) + 1,
                               rerun_before["identity"][2])
        r.equal("a different file at the same path yields no delta",
                runs.rows_added(swapped, rerun_after), None)
        shrunk = dict(rerun_after)
        shrunk["identity"] = (rerun_after["identity"][0], rerun_after["identity"][1], 1)
        r.equal("nor does a file that shrank",
                runs.rows_added(rerun_before, shrunk), None)

        # -- zero jobs is a real success --------------------------------- #
        empty_script = _script(os.path.join(work, "uptodate.sh"), total=0,
                               outdir=os.path.join(work, "u"))
        none_state = runs.job_list_state(runs.declared_job_list(empty_script))
        out = runs.reconcile(script=empty_script, observation="",
                             baseline=none_state, after=none_state)
        r.equal("a script promising 0 jobs that creates none is submitted",
                out.status, runs.SUBMITTED)
        r.equal("with zero jobs, not an error", out.jobs_seen, 0)

        # -- the silent-failure case exit status cannot see --------------- #
        # No pipefail: a failed sbatch leaves awk exiting 0, the script runs on
        # and exits clean having submitted fewer jobs than it promised. This is
        # the reason a clean exit is never trusted on its own.
        quiet_script = _script(os.path.join(work, "nopipefail.sh"), total=3,
                               pipefail=False, outdir=os.path.join(work, "np"))
        qlist = runs.declared_job_list(quiet_script)
        qbefore = runs.job_list_state(qlist)
        make_job_list(qlist, [("7", "a.s", "step/a.o", "")])
        out = runs.reconcile(script=quiet_script,
                             observation="Submitted job with ID: 7\n"
                                         "Submitted job with ID: \n",
                             baseline=qbefore, after=runs.job_list_state(qlist))
        r.equal("a clean exit with 1 of 3 rows is NOT submitted", out.status,
                runs.SUBMIT_UNKNOWN)
        r.contains("and says so", out.detail, "1 of 3 jobs were recorded")
        r.equal("an empty id after the label is not counted as a submission",
                runs.submitted_ids("Submitted job with ID: 7\n"
                                   "Submitted job with ID: \n"), ["7"])

        # A script that declares no total is trusted only on its own semantics.
        bare = _script(os.path.join(work, "bare.sh"), total=None,
                       outdir=os.path.join(work, "b"))
        blist = runs.declared_job_list(bare)
        bbefore = runs.job_list_state(blist)
        make_job_list(blist, [("8", "a.s", "step/a.o", "")])
        out = runs.reconcile(script=bare, observation="ok", baseline=bbefore,
                             after=runs.job_list_state(blist))
        # PIPEFAIL ALONE DOES NOT PROMOTE. It proves no sbatch the script ran
        # returned non-zero; it proves nothing about whether the script
        # contained every submission it was meant to. With no declared total
        # there is no second fact to check the exit status against.
        r.equal("no declared total is unknown even with pipefail",
                out.status, runs.SUBMIT_UNKNOWN)
        r.contains("and it says why", out.detail, "declares no job total")
        r.contains("while noting what pipefail does establish",
                   out.detail, "no submission it ran failed")
        nobody = _script(os.path.join(work, "nobody.sh"), total=None,
                         pipefail=False, outdir=os.path.join(work, "n2"))
        nlist = runs.declared_job_list(nobody)
        nbefore = runs.job_list_state(nlist)
        make_job_list(nlist, [("9", "a.s", "step/a.o", "")])
        out = runs.reconcile(script=nobody, observation="ok", baseline=nbefore,
                             after=runs.job_list_state(nlist))
        r.equal("no total and no pipefail: nothing vouches for it",
                out.status, runs.SUBMIT_UNKNOWN)

        # -- a reported failure ------------------------------------------ #
        # biomni's own wording, and the shape that matters: it returns stderr
        # ONLY, so the "Submitted job with ID" lines are gone by this point and
        # the job list is the sole surviving witness.
        failed_obs = "Error running Bash script (exit code 1):\nsbatch: error\n"
        r.truthy("a runner error is recognised", runs.execution_failed(failed_obs))
        r.equal("a clean run is not", runs.execution_failed("all fine"), False)
        r.equal("an observation nobody captured is neither",
                runs.execution_failed(None), None)

        pbefore = runs.job_list_state(listing)
        with open(listing, "a") as f:
            f.write("10\tg.s\t\tstep/g.o\n")
        out = runs.reconcile(script=script, observation=failed_obs,
                             baseline=pbefore, after=runs.job_list_state(listing))
        r.equal("a failure with rows behind it is a partial submission",
                out.status, runs.SUBMIT_FAILED)
        r.equal("and the live jobs are counted", out.jobs_seen, 1)
        r.check("a partial is never retry-safe", not out.retry_safe)

        # -- THE ONE THAT MUST NOT BE INFERRED --------------------------- #
        # A failure that added no rows still does not prove nothing was
        # submitted: the sbatch and its `>>` are two statements, and a kill in
        # between leaves a real job with nothing written down for it.
        clean_before = runs.job_list_state(listing)
        out = runs.reconcile(script=script, observation=failed_obs,
                             baseline=clean_before, after=clean_before)
        r.equal("a failure with no new rows is still a failure", out.status,
                runs.SUBMIT_FAILED)
        r.equal("with nothing counted", out.jobs_seen, 0)
        r.check("and it is NOT declared safe to retry on that basis",
                not out.retry_safe)
        asked = runs.reconcile(script=script, observation=failed_obs,
                               baseline=clean_before, after=clean_before,
                               quiet=True)
        r.truthy("only asking Slurm makes a retry safe", asked.retry_safe)
        unknown_sched = runs.reconcile(script=script, observation=failed_obs,
                                       baseline=clean_before, after=clean_before,
                                       quiet=None)
        r.check("a scheduler that could not be reached is not 'quiet'",
                not unknown_sched.retry_safe)

        # -- nothing established at all ----------------------------------- #
        out = runs.reconcile(script=script, observation=None,
                             baseline=clean_before, after=clean_before)
        r.equal("no observation and no rows is unknown, never held or submitted",
                out.status, runs.SUBMIT_UNKNOWN)
        r.check("the word 'submitted' is never claimed for it",
                out.status != runs.SUBMITTED)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _stale_submission_checks(r):
    """A submission whose session never came back.

    `submitting` is written before the command runs, so a process killed
    mid-flight leaves one behind. resume()'s `finally` catches an exception; it
    cannot catch a kill, a closed terminal or a rebooted login node -- and that
    is exactly the case where a full pipeline may be on the cluster with
    nothing on the record to say so.

    Two properties are pinned here. The BASELINE MUST BE DURABLE, because it is
    the one measurement that cannot be reconstructed afterwards; and a killed
    session must still be resolvable from what is left on disk, including all
    the way to `submitted` when the job list adds up.
    """
    r.section("a submission whose session never came back")
    work = tempfile.mkdtemp(prefix="genpipe_stale_")
    try:
        reg = runs.Registry(work)
        listing = os.path.join(work, "job_output",
                               "DnaSeq.somatic_fastpass.job_list.2026-08-12T10.00.00")
        script = _script(os.path.join(work, "cmd.sh"), total=3, outdir=work)
        proposal = {"command": "bash cmd.sh", "script": "cmd.sh",
                    "generated": "genpipes dnaseq -t somatic_fastpass -g cmd.sh",
                    "slots": {"pipeline": "dnaseq"}}
        reg.hold("killed", "chat-1", proposal, work)

        before = runs.job_list_state(runs.declared_job_list(script))
        reg.begin_submission("killed", workdir=work, baseline=before,
                             script=script, since=1.0)

        rec = reg.get("killed")
        r.equal("the record moves before the command runs", rec["status"],
                runs.SUBMITTING)
        r.equal("and it is no longer offered for approval", reg.held(), [])
        r.truthy("the baseline is on the record, not in a dead process's memory",
                 rec.get("job_list_baseline"))
        r.equal("recording the file it will watch",
                rec["job_list_baseline"]["path"], listing)
        r.equal("and the script it is checking against",
                rec.get("submitted_script"), script)
        r.equal("submitting() finds it", [x["name"] for x in reg.submitting()],
                ["killed"])

        # --- the process dies here. All three rows landed. ---------------- #
        make_job_list(listing, [("1", "a.s", "step/a.o", ""),
                                ("2", "b.s", "step/b.o", ""),
                                ("3", "c.s", "step/c.o", "")])

        # A later session, with no observation to read: the terminal that held
        # it is gone. The job list is not.
        reloaded = runs.Registry(work).get("killed")
        outcome = runs.reconcile(
            script=reloaded["submitted_script"], observation=None,
            baseline=reloaded["job_list_baseline"],
            after=runs.job_list_state(reloaded["job_list_baseline"]["path"]))
        r.equal("3 of 3 rows settles it as submitted, with no exit status",
                outcome.status, runs.SUBMITTED)
        r.equal("and the count survives", outcome.jobs_seen, 3)
        r.contains("saying what established it", outcome.detail,
                   "from the job list alone")

        # Fewer rows than promised is NOT promoted. The script may have been
        # killed part way, which is precisely the dangerous case.
        partial = runs.reconcile(
            script=script, observation=None, baseline=before,
            after={"path": listing, "rows": 2,
                   "identity": (0, 1, 10)})
        r.equal("2 of 3 rows stays unknown", partial.status, runs.SUBMIT_UNKNOWN)
        r.check("and is never called retry-safe on a count alone",
                not partial.retry_safe)

        # Nor is a run with no declared total, however many rows appeared.
        bare = _script(os.path.join(work, "bare2.sh"), total=None,
                       outdir=os.path.join(work, "b2"))
        blist = runs.declared_job_list(bare)
        bbefore = runs.job_list_state(blist)
        make_job_list(blist, [("9", "a.s", "step/a.o", "")])
        out = runs.reconcile(script=bare, observation=None, baseline=bbefore,
                             after=runs.job_list_state(blist))
        r.equal("no declared total is still unknown after a crash",
                out.status, runs.SUBMIT_UNKNOWN)

        # Recording the outcome takes it out of submitting() for good.
        reg.record_outcome("killed", outcome)
        settled = runs.Registry(work).get("killed")
        r.equal("the reconciled status is durable", settled["status"],
                runs.SUBMITTED)
        r.equal("nothing is left mid-submission",
                runs.Registry(work).submitting(), [])
        r.equal("and it is still not approvable", runs.Registry(work).held(), [])

        # A stale record with no baseline at all -- written before the field
        # existed -- must be answerable rather than crash. It cannot produce a
        # delta, so it cannot be promoted.
        reg.hold("ancient", "chat-2", proposal, work)
        reg.update("ancient", status=runs.SUBMITTING)
        old = runs.Registry(work).get("ancient")
        r.equal("an old record normalises to no baseline",
                old["job_list_baseline"], None)
        out = runs.reconcile(script=script, observation=None, baseline=None,
                             after=runs.job_list_state(listing))
        r.equal("and cannot be promoted without one", out.status,
                runs.SUBMIT_UNKNOWN)
        r.equal("because no delta is computable", out.jobs_seen, None)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # ------------------------------------------------------------------ #
    r.section("adoption mints a record; it never replaces one")

    # Both of these were reachable, and the first is the serious one: a
    # /track onto the name of a run waiting at the gate overwrote its status
    # to `submitted` while the graph was STILL PARKED on that run's
    # interrupt. The decision vanished from /list and /view stopped offering
    # /approve, while the interrupt sat there live and unreachable -- an
    # orphaned approval, which reconciliation does not undo because it treats
    # a settled `submitted` record as authoritative.
    work = tempfile.mkdtemp(prefix="genpipe_adopt_")
    try:
        reg = runs.Registry(work)
        listing = os.path.join(work, "job_output", "P.p.job_list.T1")
        make_job_list(listing, [("1", "trim.a", "job_output/a.o", "COMPLETED"),
                                ("2", "trim.b", "job_output/b.o", "COMPLETED")])

        # A clean adoption still works, and reports no reason.
        record, why = reg.track("adopted", listing)
        r.truthy("a real job list is adopted", record)
        r.equal("with nothing to complain about", why, None)
        r.equal("as a manual entry", reg.get("adopted")["source"], "manual")

        # Re-pointing an ADOPTED run at another path is the legitimate repeat
        # -- somebody fixing a path they typed wrong -- and stays allowed.
        second = os.path.join(work, "job_output", "P.p.job_list.T2")
        make_job_list(second, [("9", "trim.c", "job_output/c.o", "COMPLETED")])
        record, why = reg.track("adopted", second)
        r.equal("an adopted run can be re-pointed", why, None)
        r.equal("and now names the new list", reg.get("adopted")["job_list"], second)

        # THE SERIOUS ONE. A run waiting for a decision is not adoptable.
        reg.hold("pending", "chat-9", {"command": "bash cmd.sh",
                                       "generated": "genpipes rnaseq -c a -r b",
                                       "slots": {"pipeline": "rnaseq"}}, work)
        r.equal("the held run is held", reg.get("pending")["status"], runs.HELD)
        record, why = reg.track("pending", listing)
        r.equal("adopting onto it is refused", record, None)
        r.contains("saying a decision is being held", why, "holding a decision")
        r.equal("and the run is untouched",
                reg.get("pending")["status"], runs.HELD)
        r.truthy("with its proposal intact", reg.get("pending")["proposal"])

        # A lapsed run still owns its proposal, so it is protected too.
        reg.update("pending", status=runs.LAPSED)
        record, why = reg.track("pending", listing)
        r.equal("a lapsed run is protected as well", record, None)

        # A run this tool built and submitted keeps its name: it has a command
        # and a conversation behind it, and a typed job list is not worth
        # discarding those for.
        reg.mark_submitted("ours", listing, thread_id="chat-1")
        record, why = reg.track("ours", second)
        r.equal("an agent-built run is not adoptable onto", record, None)
        r.contains("saying why", why, "already a run built here")

        # And the file has to be a job list. `/track notes-1 ./notes.txt` used
        # to create a permanent record with one UNKNOWN job in it.
        notes = os.path.join(work, "notes.txt")
        with open(notes, "w") as f:
            f.write("hello, this is not a manifest\n")
        record, why = reg.track("notes-1", notes)
        r.equal("a text file is not a job list", record, None)
        r.contains("and says what one looks like", why, "job rows")
        r.equal("nothing was written", reg.get("notes-1"), None)

        empty = os.path.join(work, "empty.job_list")
        open(empty, "w").close()
        record, why = reg.track("empty-1", empty)
        r.equal("nor is an empty file", record, None)

        # ---------------------------------------------------------------- #
        r.section("what an absent job list is, and is not, evidence of")
        # TWO COMPLETELY DIFFERENT RUNS LOOK IDENTICAL IN THE ONE FIELD:
        # `submitted`, no job_list. One finished with nothing to do; the other
        # was never measured. ran_already() is explicit that "absence of a
        # manifest is not evidence of absence of a submission", and the
        # listing was reading exactly that absence as a confirmed success.
        # These two predicates are what separate the three cases.
        counted_zero = {"status": runs.SUBMITTED, "job_list": None,
                        "jobs_seen": 0, "expected_jobs": 0}
        never_counted = {"status": runs.SUBMITTED, "job_list": None,
                         "jobs_seen": None, "expected_jobs": None}
        jobs_out = {"status": runs.SUBMITTED, "job_list": None, "jobs_seen": 46}

        r.check("a counted zero is 'there was nothing to do'",
                runs.submitted_nothing(counted_zero))
        r.check("and it is not 'we cannot see its jobs'",
                not runs.jobs_are_unreachable(counted_zero))
        r.check("46 jobs and no manifest is 'we cannot see its jobs'",
                runs.jobs_are_unreachable(jobs_out))
        r.check("and it is certainly not 'there was nothing to do'",
                not runs.submitted_nothing(jobs_out))
        r.check("an unmeasured record is neither",
                not runs.submitted_nothing(never_counted)
                and not runs.jobs_are_unreachable(never_counted))

        # A count of zero on an UNFINISHED submission proves nothing about the
        # run: the submission itself never settled, so there is no outcome to
        # read off it. Only `submitted` means the submission is established.
        for unsettled in (runs.SUBMITTING, runs.SUBMIT_FAILED,
                          runs.SUBMIT_UNKNOWN):
            r.check(f"{unsettled} may not borrow that answer",
                    not runs.submitted_nothing(dict(counted_zero,
                                                    status=unsettled)))
        r.check("nor may a run that still has a manifest",
                not runs.submitted_nothing(dict(counted_zero,
                                                job_list="/s/x.job_list.T1")))
        # True and False are 1 and 0 in Python, and neither is a job count.
        r.check("a boolean is not a count of zero",
                not runs.submitted_nothing(dict(counted_zero, jobs_seen=False,
                                                expected_jobs=False)))
        # The route a real run takes to get there: reconcile() seeing a script
        # that declares no jobs, a clean exit, and no new rows. Three facts
        # agreeing -- which is what makes the outcome a finding rather than an
        # absence, and what record_outcome() then stores as jobs_seen 0.
        empty_script = _script(os.path.join(work, "nothing.sh"), total=0,
                               outdir=work)
        nothing_to_do = runs.reconcile(script=empty_script, observation="ok",
                                       baseline=None, after=None)
        r.equal("and that is what a nothing-to-do submission reconciles to",
                nothing_to_do.status, runs.SUBMITTED)
        r.equal("with the count recorded as zero", nothing_to_do.jobs_seen, 0)
        r.equal("and the declared total as zero too", nothing_to_do.expected, 0)
        r.contains("said in the words the listing borrows",
                   nothing_to_do.detail, "already up to date")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
