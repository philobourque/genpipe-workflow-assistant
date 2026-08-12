#!/usr/bin/env python
"""Mention is not selection: what deterministic memory is allowed to keep.

THIS SUITE USED TO TEST THE OPPOSITE THING. It checked an intent classifier
(run / question / ambiguous, from opening words) and a table that mapped a
scientific description to a pipeline and a protocol. Both are gone, and this is
the guard that stops them coming back in another form.

The reason they went is measurable, and the first section below is exactly the
measurement. Matching a whole word proves the word was TYPED. It does not prove
it was CHOSEN:

    "should I use rnaseq or chipseq?"   a comparison, not a decision
    "I do NOT want chipseq"             a refusal, recorded as a selection
    "does dnaseq need a pairs file?"    a question about a pipeline, not a
                                        request to run one

Telling those apart is a reading task. It belongs to the agent, which is
replayed the whole conversation and can see the question mark, the "or" and the
"not". A regex here would only relocate the mistake -- so what is tested is
that deterministic code KEEPS NOTHING from any of them, not that it classifies
them correctly.

What deterministic code may still keep is the directories to search, because
intake needs a root and must never fall back to the process's own working
directory. Whether a mentioned path qualifies is decided from the filesystem
(does it hold anything that could be an input?), never from the wording.

Run:  python tests/test_prep.py
"""
import os
import shutil
import sys
import tempfile

from harness import Report

from genpipe import intake, prep


def main():
    r = Report("mention is not selection")

    # ------------------------------------------------------------------ #
    r.section("a mentioned name is never recorded as a chosen one")
    # Each of these contains a real pipeline or protocol name as a whole word,
    # which is precisely what the deleted matcher keyed on. None of them is a
    # decision, and the test is that nothing at all is kept -- no pipeline, no
    # protocol, and no note handed to the model claiming otherwise.
    for line in ("should I use rnaseq or chipseq?",
                 "why stringtie instead of kallisto?",
                 "what is the difference between somatic_fastpass and "
                 "somatic_ensemble?",
                 "does dnaseq need a pairs file?",
                 "I do NOT want chipseq",
                 "is somatic variant calling affected by tumour purity?",
                 "I want to compare gene expression between my two conditions",
                 "find inherited variants in these samples"):
        state, note = prep.track(prep.Preparation(), line)
        r.check(f"nothing settled: {line[:44]!r}",
                note is None and state.as_dict() == {"directories": []},
                f"note={note!r} state={state.as_dict()!r}")

    # ------------------------------------------------------------------ #
    r.section("nor is an explicit one — the model reads the transcript")
    # The unambiguous case settles nothing either, and that is deliberate
    # rather than a gap. agent.run() replays the entire thread, so a pipeline
    # named three turns ago is already in front of the model; re-asserting it
    # here bought nothing and cost the cases above.
    state, note = prep.track(prep.Preparation(),
                             "run dnaseq somatic_fastpass on these samples")
    r.equal("an explicit request stores no pipeline", state.as_dict(),
            {"directories": []})
    r.equal("and asserts nothing to the model", note, None)

    # ------------------------------------------------------------------ #
    r.section("Preparation holds directories, and only directories")
    p = prep.Preparation()
    r.equal("empty to begin with", p.directories, [])
    r.equal("no directory yet", p.directory, None)
    p.remember_dir("/data/one").remember_dir("/data/two").remember_dir("/data/one")
    r.equal("they accumulate, in order, without duplicates",
            p.directories, ["/data/one", "/data/two"])
    # First rather than last. The old single project_dir was last-one-wins, so
    # a path mentioned in passing displaced the one somebody led with.
    r.equal("the first mentioned is the primary", p.directory, "/data/one")
    r.check("the old run-state fields are gone",
            not any(hasattr(p, f) for f in
                    ("pipeline", "protocol", "readset", "design", "pairs",
                     "described", "active", "project_dir")))

    # ------------------------------------------------------------------ #
    r.section("the deleted machinery stays deleted")
    for gone in ("goal", "intent", "missing", "ready", "CLARIFY", "RUN",
                 "QUESTION", "AMBIGUOUS"):
        r.check(f"prep.{gone} no longer exists", not hasattr(prep, gone))

    # ------------------------------------------------------------------ #
    r.section("directories are remembered as provenance, not classified")
    work = tempfile.mkdtemp(prefix="genpipe_prep_")
    try:
        data = os.path.join(work, "data")
        output = os.path.join(work, "results")
        for d in (data, output):
            os.makedirs(d)
        with open(os.path.join(data, "readset.tsv"), "w") as f:
            f.write("Sample\tReadset\n")

        # THERE IS NO LONGER A TEST FOR WHETHER A DIRECTORY "QUALIFIES", and
        # that is the point. A filesystem rule (does it hold a readset?) was
        # tried here and removed: it is still a classifier deciding what the
        # user meant, and it refused ordinary layouts -- readsets under
        # raw_reads/, inputs spread over three directories, a readset not
        # written yet.
        r.check("the filesystem classifier is gone",
                not hasattr(intake, "holds_candidates"))

        # An output directory is remembered like any other. The agent reads
        # the word "output"; this does not try to.
        state, note = prep.track(prep.Preparation(), f"put the output in {output}")
        r.equal("an output path is still remembered", state.directories, [output])
        r.contains("as something that was mentioned", note, "mentioned so far")
        r.check("with no claim that the data is there",
                "data is" not in (note or ""), note)
        r.contains("and the reading is handed over", note, "Work out which is which")

        state, note = prep.track(prep.Preparation(), f"my data is in {data}")
        r.equal("a data directory is remembered too", state.directories, [data])
        r.check("with no 'do not look anywhere else' claim",
                "do not look anywhere else" not in (note or "").lower(), note)

        # Both halves of a comparison survive, and neither is picked.
        other = os.path.join(work, "data2")
        os.makedirs(other)
        state, _ = prep.track(prep.Preparation(), f"is it {data} or {other}?")
        r.equal("a comparison keeps both, and picks neither",
                sorted(state.directories), sorted([data, other]))

        # A directory that holds nothing recognisable is still remembered: an
        # empty project directory is where a run is about to be built.
        empty = os.path.join(work, "fresh")
        os.makedirs(empty)
        state, _ = prep.track(prep.Preparation(), f"set up a run in {empty}")
        r.equal("an empty directory is not refused", state.directories, [empty])

        # Accumulation across turns, which is what makes provenance useful.
        state = prep.Preparation()
        state, _ = prep.track(state, f"the fastqs are in {data}")
        state, note = prep.track(state, f"write results to {output}")
        r.equal("both turns are kept, in order", state.directories, [data, output])
        r.equal("and the first mentioned is still primary", state.directory, data)

        # Something that merely looks path-shaped is not a directory.
        state, note = prep.track(prep.Preparation(), "use the a/b ratio")
        r.equal("a non-directory token is not remembered", state.directories, [])
        r.equal("and nothing is asserted", note, None)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
