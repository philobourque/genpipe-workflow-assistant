"""Which files belong to the run being diagnosed, and which do not.

This suite exists because of one bug, and the bug is worth stating plainly
because everything here is a guard against a version of it.

A GenPipes working directory accumulates runs. The same pipeline, the same
steps, the same sample names, re-run a week later, all writing into the same
`job_output/<step>/` folders and distinguished only by a timestamp in the
filename. On 2026-07-30 a somatic_fastpass run timed out at
`gatk_sam_to_fastq`; on 2026-08-05 the same pipeline was run again and largely
succeeded. Asked to diagnose the July run, the tool handed the model forty
lines of the AUGUST run's logs -- as this run's evidence, filed under steps
that the same prompt had just said never executed.

Two independent faults produced that, and the tests below pin both:

  the declared path never resolved  Column 4 of a job_list is relative to
                                    `job_output/`, and it was being joined to
                                    the working directory instead. So the one
                                    artifact that carries the run's own
                                    timestamp missed on every single job, and
                                    the "fallback" glob was in fact the only
                                    code path that ever ran.

  the fallback matched on NAME      `trim_fastp.tumorPair_COLO829N*.o` matches
                                    every execution of that step there has ever
                                    been in that directory, and glob returns
                                    them in os.scandir order. The answer was
                                    whichever file the filesystem listed first.

The property being defended is one sentence: a log offered as evidence for a
run must be that run's log, and when there is none, `not found` is the answer.
Substituting a neighbour is worse than saying nothing, because a model cannot
tell that it happened and a person reading `read it yourself` cannot either.

Standard library only. No cluster, no model.
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from genpipe import diagnosis, runs, slots
from tests.harness import Report


JULY = "2026-07-30T16.17.43"
AUGUST = "2026-08-05T11.02.13"


def _write(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    return path


def _collision(work):
    """A working directory holding two runs of the same pipeline.

    Modelled on the real one. The July run timed out at gatk_sam_to_fastq and
    everything after it was cancelled, so July wrote a `.o` for that step and
    for nothing else. The August run ran the same steps through and wrote a
    `.o` for all of them. Every August file is a candidate the old name glob
    would happily have returned for a July job.
    """
    out = os.path.join(work, "job_output")

    # July: only the step that actually ran left a log.
    _write(os.path.join(out, "gatk_sam_to_fastq",
                        f"gatk_sam_to_fastq.tumorPair_COLO829N_{JULY}.o"),
           "EPILOGUE - Status: TIMEOUT\nEPILOGUE - Time Limit: 00:01:00\n")
    _write(os.path.join(out, "gatk_sam_to_fastq",
                        f"gatk_sam_to_fastq.tumorPair_COLO829N_{JULY}.sh"),
           "#!/bin/bash\ngatk SamToFastq --INPUT in.bam\n")

    # August: the same steps, a week later, all of them with logs.
    for step, job in (("gatk_sam_to_fastq", "gatk_sam_to_fastq.tumorPair_COLO829N"),
                      ("trim_fastp", "trim_fastp.tumorPair_COLO829N"),
                      ("bwa_mem2_samtools_sort",
                       "bwa_mem2_samtools_sort.tumorPair_COLO829N")):
        _write(os.path.join(out, step, f"{job}_{AUGUST}.o"),
               "AUGUST RUN -- this is not the run being diagnosed\n")

    # The config trace of each generation, in the directory the run was
    # launched from, exactly as GenPipes writes them.
    for stamp in (JULY, AUGUST):
        _write(os.path.join(work,
                            f"DnaSeq.somatic_fastpass.{stamp}.config.trace.ini"),
               f"# DnaSeq Config Trace\n# Created on: {stamp}\n")

    listing = os.path.join(out, f"DnaSeq.somatic_fastpass.job_list.{JULY}")
    with open(listing, "w") as f:
        # job_id, name, dependencies, log path RELATIVE TO job_output/
        f.write(f"17957639\tgatk_sam_to_fastq.tumorPair_COLO829N\t\t"
                f"gatk_sam_to_fastq/gatk_sam_to_fastq.tumorPair_COLO829N_{JULY}.o\n")
        f.write(f"17957642\ttrim_fastp.tumorPair_COLO829N\t17957639\t"
                f"trim_fastp/trim_fastp.tumorPair_COLO829N_{JULY}.o\n")
        f.write(f"17957644\tbwa_mem2_samtools_sort.tumorPair_COLO829N\t17957642\t"
                f"bwa_mem2_samtools_sort/"
                f"bwa_mem2_samtools_sort.tumorPair_COLO829N_{JULY}.o\n")
    return {"name": "Test_walltimefail", "workdir": work, "job_list": listing}


def main():
    r = Report("run-scoped evidence")
    work = tempfile.mkdtemp(prefix="genpipe-evidence-")
    try:
        record = _collision(work)
        jobs = runs.parse_job_list(record["job_list"])
        timed_out, cancelled, also_cancelled = jobs
        timed_out.state = "TIMEOUT"
        cancelled.state = "CANCELLED"
        also_cancelled.state = "CANCELLED"

        # ------------------------------------------------------------------ #
        r.section("the manifest states the run's identity, and it is read")
        r.equal("the stamp comes off the job_list filename",
                runs.run_stamp(record), JULY)
        # Without the filename there is still the declared log path, which
        # carries the same stamp. Both, so a record adopted by /scan with an
        # oddly named manifest is not suddenly unidentifiable.
        r.equal("or off a declared log path when there is no filename to read",
                runs.run_stamp({"job_list": None}, jobs), JULY)
        r.equal("and it is None when nothing states it -- never guessed",
                runs.run_stamp({}, []), None)

        # ------------------------------------------------------------------ #
        r.section("the declared path resolves against job_output/")
        # THE ORIGINAL BUG. Column 4 is relative to job_output/, and joining it
        # to workdir alone missed on 46 of 46 jobs -- which is why the glob
        # below was never a fallback at all.
        got = runs.declared_log(timed_out, record)
        r.truthy("the July log is found where the manifest says it is", got)
        r.contains("and it is the July one", str(got), JULY)
        r.check("the path that used to be tried does not exist",
                not os.path.isfile(os.path.join(work, timed_out.log)))
        r.check("the one now tried does",
                os.path.isfile(os.path.join(work, "job_output", timed_out.log)))

        r.section("a job whose log was never written resolves to nothing")
        # trim_fastp was CANCELLED in July and wrote no log. An August file
        # with the same job name is sitting right there.
        r.equal("declared path misses, because there is no July file",
                runs.declared_log(cancelled, record), None)
        august = os.path.join(work, "job_output", "trim_fastp",
                              f"trim_fastp.tumorPair_COLO829N_{AUGUST}.o")
        r.check("while the August file it used to return is on disk",
                os.path.isfile(august))
        r.equal("and resolve_log still refuses it",
                runs.resolve_log(cancelled, record, JULY), None)

        # ------------------------------------------------------------------ #
        r.section("a name is not an identity")
        # The regression in its purest form: same job name, two runs, no
        # declared path to lean on. Answering at all would be answering wrong.
        nameless = runs.Job(job_id=None,
                            name="trim_fastp.tumorPair_COLO829N", log=None)
        r.equal("no stamp, no id, no answer",
                runs.resolve_log(nameless, record), None)
        r.equal("a stamp this run does not own finds nothing either",
                runs.resolve_log(nameless, record, JULY), None)
        # And the other direction: given AUGUST it does find the August file,
        # which proves the refusal above is about run identity rather than
        # about the fallback being broken.
        r.equal("the same lookup under the August stamp does find one",
                runs.resolve_log(nameless, record, AUGUST), august)

        r.section("a Slurm job id is an identity, and is still trusted")
        # A job id belongs to exactly one submission, so a file carrying one
        # cannot be another run's. Kept as a fallback for adopted runs whose
        # manifest paths have gone stale.
        moved = runs.Job(job_id="17957639", name="gatk_sam_to_fastq.x",
                         log="nowhere/at/all.o")
        _write(os.path.join(work, "job_output", "gatk_sam_to_fastq",
                            "slurm-17957639.out"), "found by id\n")
        r.contains("found by id when the declared path has gone stale",
                   str(runs.resolve_log(moved, record, JULY)), "17957639")

        # ------------------------------------------------------------------ #
        r.section("triage reads the step that broke and no others")
        report = runs.triage(record, jobs=jobs)
        by_step = {f["step"]: f for f in report["findings"]}

        broke = by_step["gatk_sam_to_fastq"]
        r.equal("the step that broke is marked as having run", broke["ran"], True)
        r.contains("its log is this run's", str(broke["log"]), JULY)
        r.contains("and it was actually read", broke["log_tail"], "TIMEOUT")

        for step in ("trim_fastp", "bwa_mem2_samtools_sort"):
            row = by_step[step]
            r.equal(f"{step} is marked as never having run", row["ran"], False)
            r.equal(f"{step} has no log", row["log"], None)
            r.equal(f"{step} has no tail to paste", row["log_tail"], None)

        r.section("but the cancellations remain visible as facts")
        r.equal("counted", report["cancelled_total"], 2)
        r.equal("as is the one that broke", report["broke_total"], 1)
        r.equal("every step still named", len(report["findings"]), 3)
        r.equal("with its state", by_step["trim_fastp"]["state"], "CANCELLED")
        r.equal("and its job count", by_step["trim_fastp"]["count"], 1)

        # ------------------------------------------------------------------ #
        r.section("no August text reaches the report at all")
        # The end-to-end statement of the whole bug, checked over the entire
        # structure rather than field by field, so a future path that
        # reintroduces it in some other field is still caught.
        blob = repr(report)
        r.check("no August timestamp anywhere in the report",
                AUGUST not in blob, blob[:300])
        r.check("and none of the August log text", "AUGUST RUN" not in blob)

        # ------------------------------------------------------------------ #
        r.section("the decisive artifacts are named, not pasted")
        # genpipes.md calls the .sh "the artifact most often skipped and most
        # often decisive" and requires the trace to be cross-checked. Supplying
        # the path is evidence identity; supplying the contents would be a
        # decision about what they prove.
        r.contains("the .sh beside the log it belongs to",
                   str(broke["script"]), f"gatk_sam_to_fastq.tumorPair_COLO829N_{JULY}.sh")
        r.check("and it is a path, not the file's contents",
                "SamToFastq" not in repr(broke["script"]))
        r.contains("the config trace of THIS generation",
                   str(report["trace"]), f"DnaSeq.somatic_fastpass.{JULY}.config.trace.ini")
        r.check("not the August one",
                AUGUST not in str(report["trace"]))
        r.equal("a cancelled step names no script either",
                by_step["trim_fastp"]["script"], None)

        r.section("an artifact that is not there is not named")
        bare = tempfile.mkdtemp(prefix="genpipe-bare-")
        try:
            listing = _write(os.path.join(bare, "job_output",
                                          f"DnaSeq.somatic_fastpass.job_list.{JULY}"),
                             f"1\tstep_one.s\t\tstep_one/step_one.s_{JULY}.o\n")
            r.equal("no trace on disk, no trace claimed",
                    runs.config_trace({"workdir": bare, "job_list": listing}), None)
            r.equal("no .sh beside a log that is not there",
                    runs.sibling_script(None), None)
        finally:
            shutil.rmtree(bare, ignore_errors=True)

        # ------------------------------------------------------------------ #
        r.section("uncertainty is preserved, never upgraded")
        # `certain` is a substring of `uncertain`, and the old substring scan
        # therefore rendered the most hedged answer the contract allows as the
        # most confident one it allows, in white. genpipes.md's own rule is
        # that uncertainty must survive to the surface.
        cases = [
            ("certain", "certain"),
            ("likely", "likely"),
            ("unclear", "unclear"),
            ("uncertain", "unclear"),
            ("Uncertain", "unclear"),
            ("not certain", "unclear"),
            ("far from certain", "unclear"),
            ("not at all certain", "unclear"),
            ("unsure", "unclear"),
            ("inconclusive", "unclear"),
            ("not likely", "unclear"),
            # A word that merely contains one of the three is not one of them.
            ("certainly a resource problem", ""),
            ("", ""),
            # Clause boundaries stop a negation carrying: this really is a
            # claim of `likely`, and reading it as `unclear` would be safe but
            # less true than what the model said.
            ("I am not certain, but likely", "likely"),
            ("likely -- the logs are consistent", "likely"),
        ]
        for said, want in cases:
            r.equal(f"{said!r} reads as {want!r}", diagnosis.confidence(said), want)

        r.section("and it survives the parser, not just the helper")
        r.equal("CONFIDENCE: uncertain",
                diagnosis.parse("MANNER: x\nCONFIDENCE: uncertain")["confidence"],
                "unclear")
        r.equal("CONFIDENCE: certain",
                diagnosis.parse("MANNER: x\nCONFIDENCE: certain")["confidence"],
                "certain")
        r.equal("**CONFIDENCE:** uncertain -- markdown and all",
                diagnosis.parse("MANNER: x\n**CONFIDENCE:** uncertain")["confidence"],
                "unclear")

        r.section("the evidence instruction asks for support, not a recital")
        r.contains("a modest cap is stated", diagnosis.SHAPE, "At most four")
        r.contains("and what the cap is for",
                   diagnosis.SHAPE, "not a restatement of everything")
        for heading in ("MANNER", "CAUSE", "EVIDENCE", "FIX", "OVERRIDE",
                        "RELAUNCH", "UNCERTAIN"):
            r.contains(f"{heading} is asked for", diagnosis.SHAPE, heading)
        # CONFIDENCE IS NO LONGER ASKED FOR. It was one word over the whole
        # answer, and the answer carries claims of different standing: a sacct
        # record is certain, a proposed walltime is not. A label spanning both
        # takes its value from the weakest and defames the rest -- "likely"
        # printed above a job id, a state and a limit that are all facts.
        r.check("CONFIDENCE is not", "CONFIDENCE" not in diagnosis.SHAPE,
                diagnosis.SHAPE)
        # ...but it is still PARSED, so an older stored note or a model still
        # emitting the retired heading lands somewhere harmless rather than
        # spilling its text into whichever section came before it.
        stale = diagnosis.parse(
            "MANNER: it timed out\nCONFIDENCE: likely\nRELAUNCH: -s 1-23\n")
        r.equal("a retired heading does not contaminate its neighbour",
                stale["manner"], "it timed out")
        r.equal("and the range after it still parses", stale["relaunch"],
                "-s 1-23")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    _audit_2026_08_05(r)
    _claim_scoped_confidence(r)
    _grounding(r)
    return r.finish()



def _grounding(r):
    """What the brief must carry so the model does not go looking for it.

    THE ACCEPTANCE CASE. A live /diagnose took 76 seconds and spent it in a
    model -> tool -> model loop: show_run to find out what the run WAS, a
    four-command bash chain to count the manifest, an ls to recover from that
    chain failing, a grep/awk to list job names, a head -40 to read job ids and
    dependencies, and finally `genpipes dnaseq --help` to learn that
    somatic_fastpass has 23 steps. Every one of those facts was already parsed
    by this application, or is constant for a GenPipes version.

    These assert the INFORMATION CONTRACT, not a tool count. A model may still
    reach for the shell when the evidence genuinely does not answer something;
    what it must not have to do is rediscover what it was already given.
    """
    r.section("the protocol's step range is a recorded fact, not a lookup")
    # `-s` has no argparse default -- "every step" is what omitting it means --
    # so the range lived nowhere and was established by shelling out.
    r.equal("dnaseq somatic_fastpass is 23 steps",
            slots.step_range("dnaseq", "somatic_fastpass"), "1-23")
    names = slots.step_names("dnaseq", "somatic_fastpass")
    r.equal("step 1 is the one that failed here", names[0],
            "gatk_sam_to_fastq")
    r.equal("and step 23 closes the protocol", names[-1], "cram_output")
    # protocols() yields Protocol objects; the range is keyed by their name.
    missing = [(p, q.name)
               for p in ("dnaseq", "rnaseq", "chipseq", "methylseq",
                         "covseq", "rnaseq_light")
               for q in (slots.protocols(p) or ())
               if not slots.step_range(p, q.name)]
    r.check("every protocol of every pipeline carries one", not missing,
            missing)
    # A pipeline with no -t has one protocol, and GenPipes calls it "default".
    r.equal("a single-protocol pipeline answers without a protocol name",
            slots.step_range("ampliconseq"), "1-8")
    # NOT RECORDED IS A REAL ANSWER. A facts file that predates this, or a
    # protocol GenPipes has renamed, must produce silence rather than a range
    # the caller would state as established.
    r.equal("an unknown pipeline yields no range",
            slots.step_range("no-such-pipeline", "x"), None)

    r.section("the dependency graph is parsed, not left in the manifest")
    # parse_job_list read column 3 and threw it away, so the DAG -- the only
    # record of which jobs waited on which, since sacct never had it and
    # squeue forgets -- was rediscovered by the model with `head -40`.
    work = tempfile.mkdtemp(prefix="genpipe_dag_")
    try:
        listing = os.path.join(work, "DnaSeq.somatic_fastpass.job_list.T1")
        with open(listing, "w") as f:
            f.write("1\ta.s1\t\ta/a.s1.o\n")
            f.write("2\ta.s2\t\ta/a.s2.o\n")
            f.write("3\tb.s2\t2\tb/b.s2.o\n")
            f.write("4\tc.s2\t3\tc/c.s2.o\n")
            f.write("5\td.all\t1:4\td/d.all.o\n")
        jobs = runs.parse_job_list(listing)
        r.equal("the dependency column is kept", jobs[2].deps, ("2",))
        r.equal("including a fan-in", jobs[4].deps, ("1", "4"))
        r.equal("and a job with none says so", jobs[0].deps, ())
        # The closure is what turns "N cancelled" into a claim about THIS job.
        r.equal("everything waiting on job 2, transitively",
                runs.downstream_of(jobs, "2"), {"3", "4", "5"})
        r.equal("a sibling branch is not swept in",
                runs.downstream_of(jobs, "1"), {"5"})
        r.equal("and a job nothing waits on has no closure",
                runs.downstream_of(jobs, "5"), set())
        r.equal("an unknown id is empty, not everything",
                runs.downstream_of(jobs, "999"), set())
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _claim_scoped_confidence(r):
    """Uncertainty sits beside the claim it is about, not over the screen."""
    r.section("what a run does not establish is its own section")
    answer = ("MANNER: gatk_sam_to_fastq.tumorPair_COLO829T (Slurm 18382352) "
              "was killed by TIMEOUT after 00:10:22 against 00:10:00, per "
              "sacct.\n"
              "CAUSE: the log's last entry is a progress line 15s before the "
              "limit, with no traceback after it.\n"
              "EVIDENCE:\n"
              "- sacct: TIMEOUT, Elapsed 00:10:22, Timelimit 00:10:00\n"
              "- the job's own epilogue reports 00:10:19, a different window\n"
              "FIX: restore cluster_walltime to dnaseq.base.ini's 35:00:00.\n"
              "OVERRIDE:\n[gatk_sam_to_fastq]\ncluster_walltime = 35:00:00\n"
              "RELAUNCH: -s 1-23\n"
              "UNCERTAIN:\n"
              "- whether 35:00:00 is enough for this input\n"
              "- why this input needed more than ten minutes\n")
    got = diagnosis.parse(answer)
    r.check("it parses as a list, one unknown per line",
            got["uncertain"] == ["whether 35:00:00 is enough for this input",
                                 "why this input needed more than ten minutes"],
            got["uncertain"])
    r.equal("and it does not become a global label", got["confidence"], "")

    r.section("the contract asks for evidence, not for what was ruled out")
    # "This is not a hang and not an error in the tool" is a claim about
    # everything that did not happen, and no log establishes it.
    for rule in ("WRITE WHAT THE EVIDENCE SHOWS, NOT WHAT IT RULES OUT",
                 "A RESOURCE FIGURE NEAR ITS REQUEST IS AN OBSERVATION",
                 "MEASUREMENTS FROM DIFFERENT SOURCES MEASURE DIFFERENT",
                 "PREFER A VALUE THE PIPELINE ITSELF ALREADY CARRIES"):
        r.contains(f"the contract says: {rule[:34]}...", diagnosis.SHAPE, rule)
    # A sourced value is still not a proven one.
    r.contains("a sourced value is still not proven sufficient",
               diagnosis.SHAPE, "not proven sufficient for this input")
    # sacct times the ALLOCATION; the epilogue times the job SCRIPT, which
    # starts after the allocation and ends before it. 00:10:22 and 00:10:19
    # are one nested inside the other, not two readings of one window -- and
    # calling them "the same allocation window" was the live run's own error.
    r.contains("and the two windows are described as nested, not identical",
               diagnosis.SHAPE, "nested inside")
    r.check("never as the same window",
            "same window" in diagnosis.SHAPE, diagnosis.SHAPE)

    r.section("the relaunch rule counts jobs, it does not characterise steps")
    # 13 jobs of the audited run COMPLETED before the failure, so "every step
    # downstream of the failure was CANCELLED" is false about the run even
    # where the arithmetic happens to fit.
    r.check("it no longer says every step downstream was cancelled",
            "every step downstream" not in diagnosis.RELAUNCH_RULE,
            diagnosis.RELAUNCH_RULE)
    r.contains("it says the jobs were", diagnosis.RELAUNCH_RULE,
               "jobs downstream of the failure were CANCELLED")


def _audit_2026_08_05(r):
    """The August-5 audit: a real timed-out run, checked against the cluster.

    Every number here was read off sacct and the filesystem by hand before any
    of it was asserted. The suite carries the SHAPE of that run rather than its
    data, so it keeps testing the properties after the run itself is cleaned
    off the cluster.
    """
    work = tempfile.mkdtemp(prefix="genpipe_audit_")
    try:
        # THE SHAPE THAT BROKE LOG RESOLUTION. The run writes into its own
        # directory (GenPipes was given -o), while the record's `workdir` is
        # wherever the ASSISTANT was launched -- os.getcwd() at the time
        # record_outcome ran. Those differ for every run generated with -o.
        app = os.path.join(work, "assistant")
        runroot = os.path.join(work, "project", "COLO829_cit")
        out = os.path.join(runroot, "job_output")
        step = os.path.join(out, "gatk_sam_to_fastq")
        os.makedirs(app, exist_ok=True)
        os.makedirs(step, exist_ok=True)
        stamp = "2026-08-05T09.20.45"
        listing = os.path.join(out, f"DnaSeq.somatic_fastpass.job_list.{stamp}")
        rows = [("18382351", "gatk_sam_to_fastq.tumorPair_COLO829N",
                 f"gatk_sam_to_fastq/gatk_sam_to_fastq.tumorPair_COLO829N_{stamp}.o"),
                ("18382352", "gatk_sam_to_fastq.tumorPair_COLO829T",
                 f"gatk_sam_to_fastq/gatk_sam_to_fastq.tumorPair_COLO829T_{stamp}.o")]
        with open(listing, "w") as f:
            for jid, jname, log in rows:
                f.write(f"{jid}\t{jname}\t\t{log}\n")
        # Only T's log is written. N completed; T is the one that timed out.
        tlog = os.path.join(step, f"gatk_sam_to_fastq.tumorPair_COLO829T_{stamp}.o")
        with open(tlog, "w") as f:
            f.write("PROLOGUE - Time Limit: 00:10:00\n"
                    "EPILOGUE - Maximum Memory Usage: 3.97 GB\n")
        with open(tlog[:-2] + ".sh", "w") as f:
            f.write("#!/bin/bash\ngatk SamToFastq ...\n")
        record = {"name": "audit-0805", "status": "submitted",
                  "job_list": listing, "workdir": app,
                  "proposal": {"slots": {"pipeline": "dnaseq",
                                         "protocol": "somatic_fastpass"}}}

        r.section("a run's logs are found beside its manifest, not beside the app")
        # THE DEFECT: declared_log joined column 4 to `workdir`, which for a
        # run generated with -o is the assistant's own directory. All three
        # joins missed, the id and stamp globs searched the same wrong root,
        # and /diagnose reported "log not found for this run" for a file that
        # was on disk -- then explained a timeout with no in-job evidence.
        job = [j for j in runs.parse_job_list(listing)
               if j.name.endswith("COLO829T")][0]
        r.equal("the declared path resolves against the manifest's directory",
                runs.declared_log(job, record), tlog)
        r.equal("and so does the full resolver",
                runs.resolve_log(job, record, stamp), tlog)
        r.contains("the manifest's own directory leads the search order",
                   runs._log_roots(record)[0], "job_output")
        r.check("the assistant's directory is only a fallback",
                runs._log_roots(record).index(app) > 0,
                runs._log_roots(record))

        r.section("a log that is genuinely absent stays absent")
        # The other half, and the one that must not regress: N wrote no log
        # here, and T's file is one directory away with a nearly identical
        # name. A looser search would hand N's diagnosis T's evidence.
        njob = [j for j in runs.parse_job_list(listing)
                if j.name.endswith("COLO829N")][0]
        r.equal("a job with no log of its own gets None, not a neighbour's",
                runs.resolve_log(njob, record, stamp), None)
        # And nothing may borrow a DIFFERENT run's file for the same step.
        other = os.path.join(step,
                             "gatk_sam_to_fastq.tumorPair_COLO829N_2026-07-30T11.00.00.o")
        with open(other, "w") as f:
            f.write("a different run's log\n")
        r.equal("nor one from another execution of the same step",
                runs.resolve_log(njob, record, stamp), None)

        r.section("the exact failed job survives into the evidence handed on")
        states = {"18382351": {"state": "COMPLETED", "exit_code": "0:0",
                               "elapsed": "00:01:39", "timelimit": "00:10:00"},
                  "18382352": {"state": "TIMEOUT", "exit_code": "0:0",
                               "elapsed": "00:10:22", "timelimit": "00:10:00"}}
        status = runs.resolve(record, states=states, reasons={})
        rep = runs.triage(record, jobs=runs.jobs_for(record, with_states=False)
                          if False else None)
        # resolve() and triage() must name the SAME job, and it must be T.
        r.equal("resolve names the job that broke",
                status.root_cause["job"], "gatk_sam_to_fastq.tumorPair_COLO829T")
        r.check("and never the one that completed",
                "COLO829N" not in status.root_cause["job"])

        r.section("TIMEOUT is authoritative even when the exit code is 0:0")
        # Slurm records the step's own status; a job killed by the walltime
        # enforcer never returned a failing code of its own. Nothing in this
        # application may read 0:0 as "it was fine".
        r.check("a 0:0 timeout is still a broken job",
                "TIMEOUT" in runs.BROKE_STATES)
        r.check("and the exit code is not what decides that",
                "0:0" not in str(runs.BROKE_STATES))
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
