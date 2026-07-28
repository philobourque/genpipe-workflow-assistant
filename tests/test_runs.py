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

import runs


def make_job_list(path, rows):
    """Write a job_list in GenPipes' shape: id, name, log, state."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for jid, name, log, state in rows:
            f.write(f"{jid}\t{name}\t{log}\t{state}\n")


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

        return r.finish()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
