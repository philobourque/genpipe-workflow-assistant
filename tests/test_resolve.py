#!/usr/bin/env python
"""runs.resolve(): what a run is actually doing, asked of the scheduler.

This suite exists because of one measured failure. On 2026-07-27 a 46-job run
died at 10:12. `genpipes tools log_report` reported it as 1 COMPLETED, 2
RUNNING, 43 PENDING -- healthy and in progress. sacct reported 1 COMPLETED,
2 TIMEOUT, 43 CANCELLED. The tool had been calling a dead run alive for hours,
and "2 RUNNING" is the affirmative signal that makes anyone stop looking.

The defect was not in the reading. log_report never contacts Slurm: it infers
state from files on disk, and every artifact GenPipes leaves is written BY THE
JOB ITSELF. A job that never started writes no prologue; a job killed rather
than exited runs no EXIT trap and writes no epilogue; only exit status 0 writes
a .done. So "never started" and "died violently" -- the two states that define
a dead run -- are exactly the two the filesystem cannot record.

What is asserted here is therefore mostly INVARIANTS rather than distributions.
Asserting "43 CANCELLED" would invite tuning the implementation until that
breakdown appeared; asserting "every job in the manifest is accounted for, and
nothing was invented" cannot be satisfied by a lucky guess.

Stdlib only, offline, against the fake cluster. Run:  python tests/test_resolve.py
"""
import os
import subprocess
import sys
import tempfile

from harness import Report

from genpipe import fakecluster
from genpipe import runs

GENPIPES = "module load mugqic/genpipes/6.1.1 && genpipes"


def submit(work, invocation):
    """Generate and submit through the fake cluster. Returns the job list.

    The fake is active in THIS process (fakecluster.session sets os.environ), so
    the subprocess inherits it and runs.query_states -- which shells out with no
    env argument of its own -- reaches the same stub. A fake that lived only in
    a dict the test held would be invisible to the functions under test.
    """
    for cmd in (f"{GENPIPES} {invocation} -g cmd.sh", "bash cmd.sh"):
        subprocess.run(cmd, shell=True, capture_output=True, text=True,
                       executable="/bin/bash", cwd=work)
    out = os.path.join(work, "job_output")
    listings = [f for f in os.listdir(out) if "job_list" in f] \
        if os.path.isdir(out) else []
    listings.sort(key=lambda f: os.path.getmtime(os.path.join(out, f)))
    return os.path.join(out, listings[-1]) if listings else None


# The real manifest shape, positionally exact:
#   id \t name \t dependencies(colon-joined) \t log
MANIFEST = "\n".join([
    "17508900\ttrimmomatic.sampleA\t\ttrimmomatic/a.o",
    "17508901\ttrimmomatic.sampleB\t\ttrimmomatic/b.o",
    "17508904\tgatk_sam_to_fastq.COLO829\t17508900\tgatk/1.o",
    "17508905\tgatk_sam_to_fastq.COLO829N\t17508901\tgatk/2.o",
    # The one that used to defeat the parser: thirteen colon-joined dependency
    # ids, longer than the job's own name, in the column the heuristic read.
    "17508908\tmultiqc.tumorPair_COLO829\t"
    + ":".join(str(17508900 + i) for i in range(13))
    + "\tmultiqc/x.o",
]) + "\n"


def write_manifest(directory, body=MANIFEST):
    out = os.path.join(directory, "job_output")
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, "DnaSeq.somatic_fastpass.job_list.2026-07-27T10.00.20")
    with open(path, "w") as f:
        f.write(body)
    return path


def main():
    r = Report("resolve")

    # ------------------------------------------------------------------ #
    r.section("the manifest is parsed positionally, not guessed at")

    with tempfile.TemporaryDirectory() as tmp:
        path = write_manifest(tmp)
        jobs = runs.parse_job_list(path)
        r.equal("every row became a job", len(jobs), 5)
        names = [j.name for j in jobs]
        # The regression. "name = the longest field that is neither the id nor
        # the log" picked the 129-character dependency string over the real
        # name, which also corrupts Job.step -- so triage() would group by, and
        # /diagnose would report, a colon-joined list of job ids.
        r.check("a fan-in job keeps its own name",
                "multiqc.tumorPair_COLO829" in names)
        r.check("and not its dependency list",
                not any(":" in n for n in names))
        fan = next(j for j in jobs if j.name == "multiqc.tumorPair_COLO829")
        r.equal("so the step is right too", fan.step, "multiqc")
        r.equal("and the log column is the log", fan.log, "multiqc/x.o")

        # A shape this format has never had must still cost a field, not the
        # feature -- the heuristic stays as the fallback.
        odd = write_manifest(tmp, "999 odd_job_name some/where.o\n")
        loose = runs.parse_job_list(odd)
        r.equal("an unfamiliar layout still parses", len(loose), 1)
        r.equal("id found", loose[0].job_id, "999")

    # ------------------------------------------------------------------ #
    r.section("a dead run reports as dead")

    work = tempfile.mkdtemp(prefix="genpipe_resolve_")
    with fakecluster.session("failed-oom"):
        # Drive a real submission through the fake cluster so the manifest and
        # the sacct store are the ones the code under test would really see.
        job_list = submit(work, "dnaseq -t somatic_fastpass -s 1-3")
        r.check("the fake cluster submitted something", bool(job_list))
        record = {"name": "x", "job_list": job_list, "workdir": work,
                  "status": "submitted"}
        status = runs.resolve(record)

        # The invariants. Not a state distribution: the composition is whatever
        # the scheduler says, and pinning it here would let an implementation be
        # tuned until that breakdown appeared rather than until it was right.
        r.equal("every manifest job is accounted for",
                status.resolved, status.total)
        r.equal("the tally sums to the denominator",
                sum(status.counts.values()), status.total)
        r.equal("and nothing is unknown", status.unknown, 0)
        r.check("nothing was invented", status.total == len(status.jobs))
        r.check("the run is finished, not active", status.finished)
        r.check("the verdict does not call it healthy",
                "complete" not in status.verdict)

        # The root cause must be the thing that broke, never one of the jobs it
        # took down with it -- a cancelled job's log explains nothing, so
        # handing one to a diagnosis is spending the investigation on a job that
        # never ran.
        cause = status.root_cause
        r.check("a root cause was found", cause is not None)
        r.check("and it is an independent failure",
                cause["state"] in runs.BROKE_STATES)
        r.check("never a cancelled job", cause["state"] != "CANCELLED")

        r.check("the source names what was actually queried",
                status.source.startswith("sacct"))

    # ------------------------------------------------------------------ #
    r.section("PENDING behind a dead job is not a healthy run")

    dying = tempfile.mkdtemp(prefix="genpipe_dying_")
    with fakecluster.session("dying"):
        job_list = submit(dying, "rnaseq -t stringtie -s 1-3")
        status = runs.resolve({"name": "y", "job_list": job_list,
                               "workdir": dying, "status": "submitted"})

        # This is the case the old path got wrong while the run was still
        # nominally alive: sacct honestly says PENDING, and those jobs will
        # never run. squeue is the only place that fact exists, and it stops
        # existing the moment the job leaves the queue.
        r.check("sacct still calls most of them pending",
                status.counts.get("PENDING", 0) > 0)
        r.check("squeue was consulted", "squeue" in status.source)
        r.check("the doomed ones were counted", status.doomed > 0)
        r.check("and the run is reported as over, not as queued",
                status.finished)
        r.check("the verdict says so", "dead" in status.verdict)

    # ------------------------------------------------------------------ #
    r.section("an unreachable scheduler is not a run with no jobs")

    with tempfile.TemporaryDirectory() as tmp:
        path = write_manifest(tmp)
        # No sacct on PATH at all -- a laptop. Nothing may be inferred from the
        # filesystem to fill the hole, and log_report must not be consulted:
        # it is a strictly weaker source that would answer confidently.
        saved = os.environ.get("PATH", "")
        os.environ["PATH"] = tmp
        try:
            status = runs.resolve({"name": "z", "job_list": path,
                                   "workdir": tmp, "status": "submitted"})
        finally:
            os.environ["PATH"] = saved
        r.equal("the source says so", status.source, "unavailable")
        r.equal("no state was invented", status.resolved, 0)
        r.check("and it is not claimed to be finished", not status.finished)
        r.check("nor reported as pending",
                "PENDING" not in status.counts)

    # ------------------------------------------------------------------ #
    r.section("a scheduler that does not recognise the ids is not an absent one")

    # Empty is ambiguous, and the two meanings must not render the same way:
    # no sacct means we know nothing; an sacct that does not know these ids
    # means they are UNKNOWN, which is a fact about the run. Reporting the
    # second as the first tells somebody their cluster is down when it is fine.
    with fakecluster.session("happy"):
        stale = tempfile.mkdtemp(prefix="genpipe_stale_")
        path = write_manifest(stale)      # ids no fake submission ever made
        status = runs.resolve({"name": "s", "job_list": path,
                               "workdir": stale, "status": "submitted"})
        r.check("the scheduler is reachable", runs.scheduler_reachable())
        r.equal("so the source is sacct, not unavailable", status.source, "sacct")
        r.equal("and every id it did not know is UNKNOWN",
                status.unknown, status.total)
        r.equal("none of them resolved", status.resolved, 0)
        r.check("which is never reported as finished", not status.finished)

    # ------------------------------------------------------------------ #
    r.section("the .done file count is kept apart from the state tally")

    with tempfile.TemporaryDirectory() as tmp:
        path = write_manifest(tmp)
        open(os.path.join(tmp, "job_output", "a.done"), "w").close()
        saved = os.environ.get("PATH", "")
        os.environ["PATH"] = tmp
        try:
            status = runs.resolve({"name": "d", "job_list": path,
                                   "workdir": tmp, "status": "submitted"})
        finally:
            os.environ["PATH"] = saved
        r.equal("counted", status.done_files, 1)
        r.check("but not as a job state", "DONE" not in status.counts)

    # ------------------------------------------------------------------ #
    r.section("durations parse, or refuse to")

    r.equal("HH:MM:SS", runs._seconds("00:10:00"), 600)
    r.equal("D-HH:MM:SS", runs._seconds("1-00:00:00"), 86400)
    r.equal("MM:SS", runs._seconds("10:01"), 601)
    r.equal("UNLIMITED is not zero", runs._seconds("UNLIMITED"), None)
    r.equal("nor is Partition_Limit", runs._seconds("Partition_Limit"), None)
    r.equal("nor is nothing", runs._seconds(""), None)

    # ------------------------------------------------------------------ #
    r.section("/check all costs one query, whatever the number of runs")

    calls = []
    real = runs.query_states

    def counting(ids):
        calls.append(list(ids))
        return real(ids)

    with fakecluster.session("happy"):
        records = []
        for i, pipeline in enumerate(("dnaseq -t germline_snv",
                                      "rnaseq -t stringtie",
                                      "chipseq -t chipseq")):
            each = tempfile.mkdtemp(prefix=f"genpipe_all{i}_")
            job_list = submit(each, f"{pipeline} -s 1-2")
            records.append({"name": f"run-{i}", "job_list": job_list,
                            "workdir": each, "status": "submitted"})

        runs.query_states = counting
        try:
            rows = runs.resolve_all(records)
        finally:
            runs.query_states = real

        r.equal("three runs resolved", len(rows), 3)
        r.equal("one sacct call, not three", len(calls), 1)
        r.check("and it asked about every job at once",
                len(calls[0]) == sum(s.total for _, s in rows))
        r.check("each run got its own states back",
                all(s.resolved == s.total for _, s in rows))

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
