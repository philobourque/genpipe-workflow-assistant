#!/usr/bin/env python
"""The fake cluster, tested on its own.

This is the fixture the lifecycle and full-app suites stand on, so a silent
regression here would let those two pass while proving nothing -- the worst kind
of failure, because it looks like success.

Two things are checked. First, that the stubs produce the real artifacts the
product reads: a cmd.sh, a job_output tree, a *.job_list.* in the shape
runs.parse_job_list expects, per-job .o logs, and a sacct that answers about
those ids. Second -- the part worth having -- that `genpipes` REJECTS bad
commands. An accept-everything stub would let a model write nonsense and call it
a pass, which is precisely the gap the README named as untested at any price.

Everything runs through fakecluster.session(), which activates the fake in this
process. That matters: the code under test does its own shelling out, so a fake
that lived only in an env dict the test held would be invisible to the very
functions being exercised.

Stdlib only, so it runs in CI. It shells out to bash, which the runners have.

Run:  python tests/test_fakecluster.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

from harness import Report

import fakecluster
import runs

GENPIPES = "module load mugqic/genpipes/6.1.1 && genpipes"


def sh(cmd, cwd):
    """Run a command the way genpipe_agent does: through bash, both streams
    captured, inheriting the environment the fake cluster has activated."""
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                       executable="/bin/bash", cwd=cwd)
    return p.returncode, p.stdout + p.stderr


def submit(work, extra=""):
    """Generate and submit a run in `work`. Returns the job list path."""
    sh(f"{GENPIPES} rnaseq -t stringtie -s 1-5 {extra} -g cmd.sh", work)
    sh("bash cmd.sh", work)
    out = os.path.join(work, "job_output")
    listings = [f for f in os.listdir(out) if "job_list" in f]
    return os.path.join(out, listings[0]) if listings else None


def main():
    r = Report("the fake cluster")
    work = tempfile.mkdtemp(prefix="genpipe_fakework_")
    try:
        with fakecluster.session("failed-oom") as (root, label):
            r.section("the fake is reached, not the real toolchain")
            r.equal("activation is labelled for the banner", label,
                    "fake cluster (failed-oom)")
            code, out = sh("module load mugqic/genpipes/6.1.1 && "
                           "echo reached && command -v genpipes", work)
            r.equal("module load succeeds", code, 0)
            r.contains("the command after it runs", out, "reached")
            r.contains("and genpipes resolves to the stub, not /cvmfs",
                       out, root)

            # A readset and an ini for the stub to validate against.
            readset = os.path.join(work, "readset.tsv")
            with open(readset, "w") as f:
                f.write("Sample\tReadset\n")
            ini = os.path.join(work, "custom.ini")
            with open(ini, "w") as f:
                f.write("[DEFAULT]\n")

            # -------------------------------------------------------------- #
            r.section("genpipes REJECTS commands a model could plausibly invent")
            bad = [
                ("unknown pipeline", "genpipes rnaseqq -t stringtie -g cmd.sh"),
                ("unknown protocol", "genpipes rnaseq -t stringtei -g cmd.sh"),
                ("missing required protocol", "genpipes rnaseq -g cmd.sh"),
                ("step out of range", "genpipes rnaseq -t stringtie -s 1-99 -g cmd.sh"),
                ("malformed step range",
                 "genpipes rnaseq -t stringtie -s one-five -g cmd.sh"),
                ("missing readset file",
                 "genpipes rnaseq -t stringtie -r /nowhere/readset.tsv -g cmd.sh"),
                ("missing ini file",
                 "genpipes rnaseq -t stringtie -c /nowhere/x.ini -g cmd.sh"),
                ("nothing to write to", "genpipes rnaseq -t stringtie"),
                ("unknown tools subcommand", "genpipes tools frobnicate"),
            ]
            for lbl, cmd in bad:
                code, out = sh(cmd, work)
                r.check(f"rejected: {lbl}", code != 0 and "error" in out.lower(),
                        f"rc={code} out={out[:120]!r}")

            r.section("...and accepts a correct one")
            code, out = sh(f"{GENPIPES} rnaseq -t stringtie -s 1-5 "
                           f"-c {ini} -r {readset} -g cmd.sh", work)
            r.equal("exits clean", code, 0)
            r.truthy("wrote cmd.sh", os.path.exists(os.path.join(work, "cmd.sh")))

            r.section("genpipes -h lists steps, as genpipes.md relies on")
            code, out = sh("genpipes dnaseq -h", work)
            r.equal("exits clean", code, 0)
            r.contains("names a protocol", out, "germline_snv")
            r.contains("and numbers its steps", out, "1- step_1")

            # -------------------------------------------------------------- #
            r.section("running cmd.sh submits: real artifacts appear")
            code, out = sh("bash cmd.sh", work)
            r.equal("submission exits clean", code, 0)
            r.contains("reports submitted job ids", out, "Submitted batch job")

            job_output = os.path.join(work, "job_output")
            r.truthy("job_output/ created", os.path.isdir(job_output))
            listings = [f for f in os.listdir(job_output) if "job_list" in f]
            r.equal("exactly one job list written", len(listings), 1)
            listing = os.path.join(job_output, listings[0])
            r.contains("named the GenPipes way", listings[0], ".job_list.")

            r.section("the product's own parser reads what the stub wrote")
            jobs = runs.parse_job_list(listing)
            r.equal("fifteen jobs", len(jobs), 15)
            r.truthy("ids parsed", all(j.job_id for j in jobs))
            r.truthy("names parsed", all("." in j.name for j in jobs))
            r.equal("steps grouped", len({j.step for j in jobs}), 5)

            r.section("sacct answers about those ids")
            states = runs.query_states([j.job_id for j in jobs])
            r.equal("every job has a state", len(states), 15)
            distinct = {v["state"] for v in states.values()}
            r.contains("the requested failure is present",
                       str(distinct), "OUT_OF_MEMORY")
            r.check("accounting sub-rows were dropped",
                    not any("." in k for k in states))
            r.check("'CANCELLED by 3001234' was reduced to 'CANCELLED'",
                    "CANCELLED" in distinct and
                    not any(" by " in (v["state"] or "") for v in states.values()),
                    f"got={distinct}")
            oom = [v for v in states.values() if v["state"] == "OUT_OF_MEMORY"][0]
            r.truthy("with the peak memory that explains it", oom["maxrss"])

            r.section("triage finds the failure and reads its log")
            record = {"job_list": listing, "workdir": work, "proposal": None}
            report = runs.triage(record)
            r.truthy("something failed", report["failed_total"] > 0)
            first = report["findings"][0]
            r.equal("the OOM step is named first", first["step"],
                    "picard_mark_duplicates")
            r.truthy("its log was located on disk", first["log"])
            r.contains("and read", first["log_tail"], "OutOfMemoryError")

            r.section("log_report agrees with the job list")
            raw = runs.log_report(listing)
            parsed = runs.parse_log_report(raw)
            r.equal("total matches the job list", parsed["total"], 15)
            r.truthy("and the failures are reported",
                     any(s in parsed["counts"] for s in runs.BAD_STATES))

            r.section("cancel is observable afterwards")
            live = [j for j in runs.jobs_for(record) if j.active or j.state is None]
            n, _ = runs.cancel(runs.jobs_for(record))
            r.equal("only cancellable jobs were targeted", n, len(live))
            if n:
                after = {j.job_id: j.state for j in runs.jobs_for(record)}
                r.check("they now read CANCELLED",
                        all(after[j.job_id] == "CANCELLED" for j in live),
                        f"got={after}")

        # ------------------------------------------------------------------ #
        r.section("a different state gives a different failure signature")
        work2 = tempfile.mkdtemp(prefix="genpipe_fakework2_")
        try:
            with fakecluster.session("failed-missing-input"):
                listing2 = submit(work2)
                report2 = runs.triage({"job_list": listing2, "workdir": work2})
                r.equal("a different step is at fault",
                        report2["findings"][0]["step"], "trimmomatic")
                r.equal("and only that one sample",
                        report2["failed_total"], 1)
                r.contains("with a different cause in the log",
                           report2["findings"][0]["log_tail"],
                           "No such file or directory")
        finally:
            shutil.rmtree(work2, ignore_errors=True)

        r.section("the happy state has nothing to diagnose")
        work3 = tempfile.mkdtemp(prefix="genpipe_fakework3_")
        try:
            with fakecluster.session("happy"):
                listing3 = submit(work3)
                record3 = {"job_list": listing3, "workdir": work3}
                r.equal("nothing failed", runs.triage(record3)["failed_total"], 0)
                r.equal("and the verdict says so",
                        runs.verdict(runs.counts(runs.jobs_for(record3))),
                        "complete")
        finally:
            shutil.rmtree(work3, ignore_errors=True)

        r.section("the running state is neither done nor broken")
        work4 = tempfile.mkdtemp(prefix="genpipe_fakework4_")
        try:
            with fakecluster.session("running"):
                listing4 = submit(work4)
                tally = runs.counts(runs.jobs_for({"job_list": listing4,
                                                   "workdir": work4}))
                r.truthy("some jobs are still moving",
                         tally.get("RUNNING", 0) + tally.get("PENDING", 0) > 0)
                r.contains("and the verdict reflects that",
                           runs.verdict(tally), "running")
        finally:
            shutil.rmtree(work4, ignore_errors=True)

        r.section("the environment is restored after a session")
        r.check("no fake store left behind",
                "GENPIPE_FAKE_STORE" not in os.environ)

        return r.finish()
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
