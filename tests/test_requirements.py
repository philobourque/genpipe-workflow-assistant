#!/usr/bin/env python
"""The requirements table, checked against GenPipes and against the gate.

Two different failures are guarded here, and only the second is the obvious one.

  1. THE TABLE ITSELF IS WRONG. slots.py records which protocol needs a design
     file and which needs a pairs file. Nothing on the install states that --
     not `--help`, not the shipped READMEs, not the ini files -- so the table
     was assembled by reading the source, and a table assembled that way goes
     stale the moment GenPipes reorganises. Sharing a stale table between the
     gate and the model would produce consistency without correctness, which
     is worse than disagreement because nothing would ever surface it.

  2. THE MODEL AND THE GATE DISAGREE. The facts handed to the model are
     generated from the same objects gaps() consults, so this checks that the
     rendering has not quietly lost or invented a row.

The first is checked against REAL captured `--help` output plus the installed
source; the second is pure logic. Both run without a cluster.

What the source says, verified 2026-08-17 against mugqic/genpipes/6.1.1:

    common.py           design_file raises MissingInputError when -d is absent
    dnaseq/__init__     tumor_pairs is parsed only if 'somatic' in protocol
                        and 'tumor_only' not in protocol
    longread_dnaseq     tumor_pairs is parsed only if 'somatic' in protocol
    chipseq/__init__    contrasts calls design_file, so the `if self.contrasts`
                        in differential_binding is NOT a guard -- it raises

Run:  python tests/test_requirements.py
"""
import os
import sys

from harness import Report

from genpipe import modify, slots, usage

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures", "help")


def helptext(pipeline):
    path = os.path.join(FIXTURES, f"{pipeline}-6.1.1.txt")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return f.read()


def main():
    r = Report("requirements: correct, and shared without drift")

    # ------------------------------------------------------------------ #
    r.section("the protocol lists match this GenPipes")

    for pipeline in ("ampliconseq", "dnaseq"):
        text = helptext(pipeline)
        if text is None:
            continue
        sections = [name for name in modify.protocols_in_help(text) if name]
        ours = [p.name for p in slots.protocols(pipeline)]
        if not ours:
            # A pipeline with no -t still prints a `Protocol default` header,
            # which is a labelling artifact rather than a choice. The check
            # that settles it is argparse's own: does -t exist at all.
            flags = usage.read(text, pipeline)
            r.check(f"{pipeline} takes no -t, and slots agrees",
                    not flags.takes("-t"))
        else:
            r.equal(f"{pipeline} protocols match --help",
                    sorted(ours), sorted(sections))

    # ------------------------------------------------------------------ #
    r.section("pairs are required exactly where GenPipes parses them")

    # From dnaseq/__init__.py's tumor_pairs property, which is the whole rule:
    #     if 'somatic' in self.protocol and 'tumor_only' not in self.protocol
    # Anything else returns None, so no pairs file is read and none is needed.
    for name in ("germline_snv", "germline_sv", "germline_high_cov",
                 "somatic_tumor_only"):
        r.equal(f"dnaseq -t {name} needs no pairs file",
                slots.needs_of("dnaseq", name) == slots.PAIRS, False)
    for name in ("somatic_fastpass", "somatic_ensemble", "somatic_sv"):
        r.equal(f"dnaseq -t {name} needs pairs",
                slots.needs_of("dnaseq", name), slots.PAIRS)

    # longread_dnaseq's property has no tumor_only clause -- 'somatic' alone.
    r.equal("longread_dnaseq -t nanopore_paired_somatic needs pairs",
            slots.needs_of("longread_dnaseq", "nanopore_paired_somatic"),
            slots.PAIRS)
    for name in ("nanopore", "revio"):
        r.check(f"longread_dnaseq -t {name} does not",
                slots.needs_of("longread_dnaseq", name) != slots.PAIRS)

    # And nothing claims pairs on a pipeline that cannot even parse -p.
    for pipeline in sorted(slots.PIPELINES):
        text = helptext(pipeline)
        if text is None:
            continue
        takes = usage.read(text, pipeline).takes("-p")
        wants = any(slots.needs_of(pipeline, p.name) == slots.PAIRS
                    for p in slots.protocols(pipeline)) or \
            slots.needs_of(pipeline) == slots.PAIRS
        if wants:
            r.check(f"{pipeline} accepts -p, as the table assumes", takes)

    # ------------------------------------------------------------------ #
    r.section("chipseq's design stays optional, deliberately")

    # differential_binding reads contrasts -> design_file -> raises. So a
    # chipseq run that INCLUDES that step and has no -d fails at generation.
    # The table still does not demand one, because peak calling without it is
    # an ordinary complete run and demanding it would block `-s 1-18`. The
    # trade is deliberate and this is what pins it.
    r.check("chipseq is on the optional list", "chipseq" in slots._DESIGN_OPTIONAL)
    r.equal("so gaps() asks for no design",
            [g.slot for g in slots.gaps(pipeline="chipseq", protocol="chipseq",
                                        readset="r.tsv")], [])
    # Contrast with one where it is not optional.
    r.equal("while rnaseq stringtie does ask",
            [g.slot for g in slots.gaps(pipeline="rnaseq", protocol="stringtie",
                                        readset="r.tsv")], ["design"])

    # ------------------------------------------------------------------ #
    r.section("what the model is told matches what the gate enforces")

    note = slots.requirements_note()
    r.truthy("a note is produced", note)

    # Every requirement gaps() would raise must appear in the note, and
    # nothing else may. This is the drift check: the two are generated from
    # one table, and this fails the moment they stop being.
    for pipeline in sorted(slots.PIPELINES):
        for proto in (slots.PIPELINES[pipeline] or [None]):
            name = proto.name if proto else None
            need = slots.needs_of(pipeline, name)
            label = f"{pipeline} -t {name}" if name else f"{pipeline} (no -t)"
            if need == slots.PAIRS:
                r.contains(f"{label} is stated to the model", note, label)
                r.contains(f"  as a pairs file", note, "-p pairs file")
            elif need == slots.DESIGN and pipeline not in slots._DESIGN_OPTIONAL:
                r.contains(f"{label} is stated to the model", note, label)

    r.contains("chipseq's exception is stated rather than omitted",
               note, "chipseq (any -t)")
    r.contains("and says what it costs to ignore", note, "generation FAILS")

    # Feature inis and defaults ride along, because --help states neither.
    r.contains("feature inis are stated", note, "dnaseq.cancer.ini")
    r.contains("and GenPipes' own defaults", note, "germline_snv")

    # ------------------------------------------------------------------ #
    r.section("it is a reference, not a procedure")

    # THE BOUNDARY. The table may state facts; it may not decide anything.
    # A note that told the model what to ask, in what order, or when it was
    # ready would be the deterministic questionnaire this project deleted --
    # the one that read "I do NOT want chipseq" as a request for chipseq.
    lowered = note.lower()
    for phrase in ("ask the user", "ask for it next", "you are ready",
                   "then ask", "in this order", "first ask",
                   "if missing, ask", "therefore"):
        r.check(f"the note does not say {phrase!r}", phrase not in lowered)
    r.contains("and says the decision is the model's",
               note, "yours to decide")

    # No step numbers, ever. They are version-exact and belong to --help.
    import re
    numbered = re.findall(r"\bstep \d+\b", lowered)
    r.equal("no step numbers leak into it", numbered, [])

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
