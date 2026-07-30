#!/usr/bin/env python
"""/scan: finding GenPipes runs that already exist, without reading any data.

Two properties, and the second matters more than the first.

It has to FIND runs: a person who has been using GenPipes for a year has runs on
disk that the assistant knows nothing about, and telling them to re-run
everything through the agent to get a listing is not an answer.

And it has to stay READ-ONLY and metadata-only. The scanner reads job-list
filenames, directory names and structure. It never opens a FASTQ, a BAM, a VCF,
a result table or a readset. It never renames, regenerates, resubmits or cancels
anything it finds. Both halves are asserted here, because "it does not read the
data" is the kind of claim that is true when written and quietly false later.

Run:  python tests/test_scan.py
"""
import os
import sys
import tempfile

from harness import Report

from genpipe import runs


def make_run(root, name, listing, when=None, extra_listings=()):
    """A directory shaped like a finished GenPipes run."""
    workdir = os.path.join(root, name)
    out = os.path.join(workdir, "job_output")
    os.makedirs(out, exist_ok=True)
    paths = []
    for fn in (listing,) + tuple(extra_listings):
        path = os.path.join(out, fn)
        with open(path, "w") as f:
            f.write("41000000\tstep.sample\t\tstep/x.o\n")
        paths.append(path)
    if when:
        for i, path in enumerate(paths):
            os.utime(path, (when + i, when + i))
    return workdir, paths


def main():
    r = Report("scan")

    with tempfile.TemporaryDirectory() as root:
        make_run(root, "colo829",
                 "DnaSeq.somatic_fastpass.job_list.2026-07-27T10.00.20")
        make_run(root, "marie_rnaseq",
                 "RnaSeq.stringtie.job_list.2026-07-20T08.11.02")
        # Two job lists in one directory: a re-run, which is a second
        # submission ATTEMPT of one logical run and must not become two rows.
        make_run(root, "chip_h3k27ac",
                 "ChipSeq.chipseq.job_list.2026-07-01T09.00.00",
                 extra_listings=("ChipSeq.chipseq.job_list.2026-07-02T09.00.00",))
        # A pipeline whose CamelCase name is not in the table.
        make_run(root, "odd", "Frobnicate.default.job_list.2026-07-05T09.00.00")

        # The data. Nothing here may be opened, and nothing under it walked.
        reads = os.path.join(root, "colo829", "raw_reads")
        os.makedirs(reads, exist_ok=True)
        secret = os.path.join(reads, "patient_R1.fastq.gz")
        with open(secret, "w") as f:
            f.write("@read1\nACGT\n+\nIIII\n")
        before = os.stat(secret).st_atime_ns

        # A decoy job_output nested under raw_reads. If it turns up in the
        # results, the walk descended into the data directory.
        decoy_out = os.path.join(reads, "job_output")
        os.makedirs(decoy_out, exist_ok=True)
        with open(os.path.join(decoy_out,
                               "DnaSeq.x.job_list.2026-07-27T11.00.00"), "w") as f:
            f.write("1\ta\t\tb.o\n")

        found = runs.discover(root)

        # -------------------------------------------------------------- #
        r.section("what it finds")

        r.equal("one row per run directory, not per job list", len(found), 4)
        names = {f["workdir"].rsplit("/", 1)[-1]: f for f in found}
        r.check("the dnaseq run", "colo829" in names)
        r.check("the rnaseq run", "marie_rnaseq" in names)
        r.check("the chipseq run", "chip_h3k27ac" in names)

        r.equal("the pipeline comes off the filename",
                names["colo829"]["pipeline"], "dnaseq")
        r.equal("and so does the protocol",
                names["colo829"]["protocol"], "somatic_fastpass")
        r.equal("CamelCase is mapped, not lower-cased blindly",
                names["marie_rnaseq"]["pipeline"], "rnaseq")

        # An unknown pipeline shown as unknown is worth more than a plausible
        # invention, because the thing on the other end of the guess is a
        # cluster.
        r.equal("an unrecognised pipeline is not guessed",
                names["odd"]["pipeline"], None)

        r.equal("re-runs are attempts of one run",
                len(names["chip_h3k27ac"]["attempts"]), 2)
        r.check("and the newest is the one that gets checked",
                names["chip_h3k27ac"]["job_list"].endswith("07-02T09.00.00"))

        r.check("a proposed id is offered",
                all(f["name"] for f in found))
        r.check("built from the directory the person named",
                "colo829" in names["colo829"]["name"])

        # -------------------------------------------------------------- #
        r.section("what it does not touch")

        r.check("nothing under raw_reads is reported",
                not any("raw_reads" in f["workdir"] for f in found))
        r.check("not even a job_output hidden inside it",
                not any("raw_reads" in f["job_list"] for f in found))
        r.check("the FASTQ was never opened",
                os.stat(secret).st_atime_ns == before)
        r.check("and it is still there, unchanged",
                open(secret).read().startswith("@read1"))

        # -------------------------------------------------------------- #
        r.section("adopting is explicit, and never silent")

        with tempfile.TemporaryDirectory() as home:
            registry = runs.Registry(home)
            entry = names["colo829"]
            r.equal("nothing is known yet",
                    runs.already_known(registry, entry), None)

            registry.adopt(entry["name"], entry)
            record = registry.get(entry["name"])
            r.equal("registered as submitted", record["status"], runs.SUBMITTED)
            r.equal("marked as discovered", record["source"], "scan")
            r.equal("with the job list", record["job_list"], entry["job_list"])
            r.equal("and what it is, without a scheduler call",
                    record["proposal"]["slots"]["protocol"], "somatic_fastpass")

            # Running /scan twice must not produce two rows for one run.
            r.check("a second scan recognises it",
                    runs.already_known(registry, entry) is not None)
            # Matched on the run directory too, so a re-run that wrote a new job
            # list is still the same run.
            moved = dict(entry, job_list=entry["job_list"] + ".other")
            r.check("even under a different job list",
                    runs.already_known(registry, moved) is not None)

            r.equal("a name collision is suffixed rather than overwritten",
                    registry.unique_name(entry["name"]), entry["name"] + "-2")

        # -------------------------------------------------------------- #
        r.section("an empty directory is an answer, not an error")

        with tempfile.TemporaryDirectory() as empty:
            r.equal("nothing found", runs.discover(empty), [])
        r.equal("a path that is not a directory is not a crash",
                runs.discover(os.path.join(root, "nope")), [])

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
