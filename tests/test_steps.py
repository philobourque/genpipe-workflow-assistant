#!/usr/bin/env python
"""Steps, end to end: parsing, protocol scoping, prose, and the panel key.

Four bugs lived in this one lifecycle and they are fixed as one unit, because
fixing any of them alone makes another one worse:

  1. The parser expected `1- trimmomatic16S`, the GenPipes 4.x format. The
     versions this tool can actually invoke print `1 trimmomatic16S`, so
     steps_from_help() returned [] for every one of them -- silently, and for
     long enough that the tests grew a fixture in the dead format and passed.

  2. It was not protocol-scoped. `genpipes dnaseq --help` prints SEVEN
     protocols whose numbering all restarts at 1, and passing `-t` does not
     change that -- verified, see the dnaseq-with-t fixture. Merging them and
     deduplicating by number answered "1-27" for every dnaseq protocol, which
     is wrong in both directions: it would refuse a legal somatic_ensemble
     range (1-39) and wave through an impossible somatic_sv one (1-14).

  3. cli._step_risks decided "this change is about steps" with a bare digit
     regex, so "set walltime to 24 hours" meant step 24. Harmless only while
     the parser was broken; fixing 1 and 2 arms it.

  4. The /modify steps row could not accept digits at all -- ui.choose tested
     key.isdigit() before the text hook, so `3` fired Enter on row 3.

EVERY FIXTURE HERE IS REAL OUTPUT, captured from the live CVMFS install under
tests/fixtures/help/. That is the whole lesson of bug 1: a fixture written from
memory agreed with the parser and disagreed with GenPipes.

Run:  python tests/test_steps.py
"""
import os
import sys

from harness import Report

from genpipe import modify

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures", "help")


def helptext(name):
    with open(os.path.join(FIXTURES, f"{name}.txt")) as f:
        return f.read()


def main():
    r = Report("steps: one correctness unit")

    # ------------------------------------------------------------------ #
    r.section("real GenPipes output parses — this is what broke")

    for version in ("6.1.1", "5.1.0"):
        status, rows = modify.step_list(helptext(f"ampliconseq-{version}"))
        r.equal(f"ampliconseq {version} parses", status, modify.STEPS_OK)
        r.equal(f"  and finds all 8 steps", len(rows), 8)
        r.equal(f"  numbered from 1", rows[0], (1, "trimmomatic16S"))
        r.equal(f"  to 8", rows[-1], (8, "multiqc"))

    # The format the parser used to require. Unreachable in practice -- 4.x has
    # no `genpipes` entry point, so runs.pipeline_help() cannot invoke it -- but
    # tolerated, because refusing it would be a second silent failure the day
    # somebody points this at an older module.
    status, rows = modify.step_list(helptext("ampliconseq-4.6.1-legacy"))
    r.equal("the legacy `1- name` format is still tolerated",
            status, modify.STEPS_OK)
    r.check("and yields steps", bool(rows))

    # ------------------------------------------------------------------ #
    r.section("protocol scoping — the numbers restart at 1 seven times over")

    dnaseq = helptext("dnaseq-6.1.1")
    sections = modify.protocols_in_help(dnaseq)
    r.equal("dnaseq --help describes seven protocols", len(sections), 7)

    # The measured ranges. If any of these move, the install changed and this
    # test is the thing that should say so.
    for protocol, last in (("germline_snv", 27), ("germline_sv", 25),
                           ("germline_high_cov", 15), ("somatic_tumor_only", 22),
                           ("somatic_fastpass", 23), ("somatic_ensemble", 39),
                           ("somatic_sv", 14)):
        status, rows = modify.step_list(dnaseq, protocol)
        r.equal(f"{protocol} runs 1-{last}",
                (status, max(n for n, _ in rows)), (modify.STEPS_OK, last))

    # `-t` DOES NOT SCOPE THE OUTPUT. Verified against a real capture rather
    # than assumed -- an earlier reading of a truncated head suggested it did,
    # and the whole protocol-scoping design turns on it being false.
    r.equal("passing -t changes nothing about the step list",
            helptext("dnaseq-with-t-6.1.1"), dnaseq)

    status, rows = modify.step_list(dnaseq)
    r.equal("so an unscoped multi-protocol help is AMBIGUOUS, not a guess",
            status, modify.STEPS_AMBIGUOUS)
    r.equal("and offers nothing", rows, [])

    # ------------------------------------------------------------------ #
    r.section("the four kinds of nothing are distinguishable")

    r.equal("no help at all is unavailable",
            modify.step_list("")[0], modify.STEPS_UNAVAILABLE)
    r.equal("help with no recognisable list is unparseable — NOT unavailable",
            modify.step_list("usage: genpipes x\n\noptions:\n  -h  help\n")[0],
            modify.STEPS_UNPARSEABLE)
    r.equal("a single-protocol help needs no protocol named",
            modify.step_list(helptext("ampliconseq-6.1.1"))[0], modify.STEPS_OK)

    # ------------------------------------------------------------------ #
    r.section("validation is scoped to the protocol it was asked about")

    amplicon = helptext("ampliconseq-6.1.1")
    risks, stop = modify.step_risk("99", amplicon)
    r.truthy("step 99 is a hard stop on ampliconseq", stop)
    r.contains("naming the real range", stop, "1-8")

    # 30 is legal for somatic_ensemble and illegal for five of its siblings.
    # The merged parser answered the same thing for all of them.
    _, stop = modify.step_risk("30", dnaseq, "somatic_ensemble")
    r.equal("step 30 is fine for somatic_ensemble", stop, None)
    _, stop = modify.step_risk("30", dnaseq, "somatic_sv")
    r.truthy("but not for somatic_sv", stop)
    r.contains("which says 1-14", stop, "1-14")

    _, stop = modify.step_risk("30", dnaseq)
    r.equal("and with no protocol it refuses to have an opinion", stop, None)

    risks, stop = modify.step_risk("1-2,5-8", amplicon)
    r.equal("an internal gap is a risk, not a stop", stop, None)
    r.check("but it is raised", bool(risks))
    r.contains("naming what is skipped", risks[0], "3-4")

    r.equal("unreadable help is no opinion",
            modify.step_risk("1-5", ""), ([], None))
    r.equal("and so is a help this parser cannot read",
            modify.step_risk("1-5", "usage: genpipes x\n"), ([], None))

    # ------------------------------------------------------------------ #
    r.section("prose: a number is a step only when the sentence says so")

    for text, want in (
            ("steps 3-6", "3-6"),
            ("use steps 1-4 only", "1-4"),
            ("change -s to 2,5-7", "2,5-7"),
            ("run steps 3, 6-8", "3,6-8"),
            ("set the step range to 1-5", "1-5"),
    ):
        r.equal(f"{text!r} asks for steps", modify.steps_meant(text), want)

    # THE LANDMINE. Every one of these matched the old bare-digit regex and
    # would now hard-stop with "step 24 is not in this protocol".
    for text in ("set walltime to 24 hours",
                 "raise mem_per_cpu to 8",
                 "bump memory to 16",
                 "use the 2024 genome build",
                 "use readset_2.tsv",
                 "switch to the GRCh38 build",
                 "give it 12 cpus"):
        r.equal(f"{text!r} does not", modify.steps_meant(text), None)

    # A malformed range is not a step range either, so it reaches the model
    # rather than being refused by a check that half-recognised it.
    r.equal("an inverted range is not read as steps",
            modify.steps_meant("steps 9-2"), None)

    # ------------------------------------------------------------------ #
    r.section("the panel's step row accepts what a step range is made of")

    for good in ("1-5", "3,6-8", "1", "2-2", "1-5,7,9-12"):
        r.check(f"{good!r} is a legal range", modify.valid_steps(good))
    for bad in ("", "abc", "0-3", "5-2", "1-", "-4", "1..3"):
        r.check(f"{bad!r} is refused", not modify.valid_steps(bad))

    # The keystroke fix is in ui.choose, which needs a terminal; what is
    # checkable here is that the panel's own validator says yes to the values
    # somebody types digit by digit. See test_panel for the key routing.
    proposal = {"slots": {"pipeline": "ampliconseq", "steps": "1-5"}}
    r.check("the steps row validates 3-6",
            bool(modify.check("steps", "3-6", proposal)))
    r.check("and refuses 9-2",
            not modify.check("steps", "9-2", proposal))

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
