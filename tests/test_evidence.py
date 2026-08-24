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

from genpipe import diagnosis, runs
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
        # The categories themselves are deliberately untouched in this pass.
        for heading in ("MANNER", "CAUSE", "EVIDENCE", "FIX", "OVERRIDE",
                        "RELAUNCH", "CONFIDENCE"):
            r.contains(f"{heading} is still asked for", diagnosis.SHAPE, heading)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
