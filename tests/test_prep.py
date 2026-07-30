#!/usr/bin/env python
"""Recognising a run request, and knowing what to ask next.

The distinction under test is not "did they use a launch keyword". It is whether
the person wants the assistant to PERFORM an analysis or to EXPLAIN one. Both
sentences below name a protocol and only one of them should start preparing
anything:

    what's the difference between somatic_fastpass and somatic_ensemble?
    run somatic_fastpass on these

The failure modes this is written against are asymmetric, so the tests are too.
Reading a question as a run wastes the person's allocation and their attention;
reading a run as a question wastes a sentence. And guessing a PROTOCOL from a
vague description is worse than either, because a wrong protocol produces a run
that completes successfully and answers a different question.

Run:  python tests/test_prep.py
"""
import sys

from harness import Report

from genpipe import prep


def main():
    r = Report("prep")

    # ------------------------------------------------------------------ #
    r.section("a question is a question, even when it names a pipeline")

    for line in ("what is a design file?",
                 "what does stringtie do",
                 "how do I run dnaseq?",
                 "which protocol should I use for tumour-normal?",
                 "explain the -c stack",
                 "compare somatic_fastpass and somatic_ensemble",
                 "why did that fail"):
        r.equal(f"question: {line!r}", prep.intent(line), prep.QUESTION)

    # ------------------------------------------------------------------ #
    r.section("an instruction is an instruction, however it is phrased")

    for line in ("run dnaseq germline_snv on my readset",
                 "can you run rnaseq on this?",
                 "launch the chipseq pipeline",
                 "prepare a chipseq run on these",
                 "please submit dnaseq for these samples",
                 # Case 3: the goal described as a RESULT rather than in
                 # GenPipes terminology. Nobody should have to name a pipeline
                 # they have already described.
                 "Find inherited SNVs and small indels",
                 "Compare gene expression between treatment and control",
                 "Analyze paired tumor-normal variants"):
        r.equal(f"run: {line!r}", prep.intent(line), prep.RUN)

    # ------------------------------------------------------------------ #
    r.section("ambiguity is answered with a question, not a guess")

    # Case 1. Mentions data or an analysis without saying which is wanted.
    # Guessing "question" wastes their time; guessing "run" starts spending it.
    for line in ("I have tumor-normal data",
                 "we have nine mouse rnaseq fastqs",
                 "rnaseq"):
        r.equal(f"ambiguous: {line!r}", prep.intent(line), prep.AMBIGUOUS)
    r.contains("and the clarification offers both", prep.CLARIFY, "or should I")

    # ------------------------------------------------------------------ #
    r.section("the pipeline is inferred readily; the protocol is not")

    strong = prep.goal("find inherited SNVs and small indels")
    r.equal("germline SNVs is one pipeline", strong.pipeline, "dnaseq")
    r.equal("and exactly one protocol", strong.protocol, "germline_snv")

    strong = prep.goal("compare gene expression between treatment and control")
    r.equal("differential expression is rnaseq", strong.pipeline, "rnaseq")
    r.equal("and stringtie", strong.protocol, "stringtie")

    # The asymmetry, and the point of the whole table. "Tumour versus normal" is
    # three somatic protocols that differ in cost and thoroughness. Inferring
    # one produces a run that finishes cleanly and answers a different question,
    # so the pipeline is taken and the protocol is left to be asked.
    weak = prep.goal("analyse paired tumour-normal variants")
    r.equal("the pipeline is clear", weak.pipeline, "dnaseq")
    r.equal("the protocol is not, and is not guessed", weak.protocol, None)

    weak = prep.goal("look for structural variants")
    r.equal("same again", weak.pipeline, "dnaseq")
    r.equal("still not guessed", weak.protocol, None)

    r.equal("a description this table does not know maps to nothing",
            prep.goal("do the usual thing with Marie's samples"), None)

    # ------------------------------------------------------------------ #
    r.section("what to ask next depends on what was already said")

    # Case 5: pipeline and protocol known, readset missing. The next question is
    # the readset -- not the pipeline again, and not a design file whose
    # necessity the protocol has already settled.
    state = prep.Preparation(pipeline="rnaseq", protocol="stringtie")
    gap = prep.missing(state)
    r.equal("the readset is next", gap.slot, "readset")

    # Case 6: readset supplied, a protocol-specific document missing. It must
    # ask only for the missing one, and must not restart the intake.
    state.learn(readset="readset.tsv")
    gap = prep.missing(state)
    r.equal("now the design", gap.slot, "design")
    r.contains("with the format explained", gap.note, "contrast")

    state.learn(design="design.tsv")
    r.equal("and then nothing is missing", prep.missing(state), None)
    r.check("so it is ready to generate", prep.ready(state))

    # Case 4: the pipeline is named and the protocol is not. The protocol must
    # come before any document question, because the protocol is what decides
    # which documents are required at all.
    state = prep.Preparation(pipeline="dnaseq")
    r.equal("the protocol comes first", prep.missing(state).slot, "protocol")

    state = prep.Preparation(pipeline="dnaseq", protocol="somatic_fastpass",
                             readset="readset.tsv")
    r.equal("a somatic run asks for pairs, not a design",
            prep.missing(state).slot, "pairs")

    # Nothing at all: the pipeline is the only decidable question.
    r.equal("with nothing said, ask the pipeline",
            prep.missing(prep.Preparation()).slot, "pipeline")

    # ------------------------------------------------------------------ #
    r.section("nothing already resolved is ever asked about again")

    state = prep.Preparation(pipeline="rnaseq", protocol="stringtie",
                             readset="readset.tsv")
    # learn() must never unset. A turn that mentions nothing new must not
    # dissolve what an earlier one settled -- being asked twice for a readset
    # you already named is what sends people back to writing the command.
    state.learn(pipeline=None, protocol=None, readset=None)
    r.equal("the pipeline survives an empty turn", state.pipeline, "rnaseq")
    r.equal("so does the readset", state.readset, "readset.tsv")
    state.learn(protocol="variants")
    r.equal("but a real correction lands", state.protocol, "variants")

    # ------------------------------------------------------------------ #
    r.section("defaults are never asked about")

    r.check("the step range is assumed", "steps" in prep.ASSUMED)
    r.check("so is the cluster config", "config" in prep.ASSUMED)
    r.check("and the output directory", "output" in prep.ASSUMED)
    # slots.gaps() is the authority on what gets asked, and it must not have
    # grown any of these.
    state = prep.Preparation(pipeline="rnaseq_light", readset="r.tsv")
    r.equal("a fully specified light run asks nothing",
            prep.missing(state), None)

    # ------------------------------------------------------------------ #
    r.section("the state is summarised so it can be corrected")

    state = prep.Preparation(pipeline="rnaseq", protocol="stringtie",
                             readset="readset.tsv")
    text = prep.summary(state)
    r.contains("the pipeline", text, "rnaseq")
    r.contains("the protocol", text, "stringtie")
    r.contains("and the readset", text, "readset.tsv")

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
