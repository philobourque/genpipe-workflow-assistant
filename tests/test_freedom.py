#!/usr/bin/env python
"""The freedoms the model and the user must keep, asserted as invariants.

This suite exists because the failure mode it guards against is invisible from
the outside: every one of these properties can be broken by a change that makes
some individual screen nicer, and nothing else in the repo would go red.

Two halves, and they are the two halves of the project's design.

THE MODEL'S FREEDOM. Prose is not a decision. A sentence that names a pipeline
has not selected one; a sentence containing the word "fail" has not called
diagnose_run; a sentence shaped like a slash command is not one. Deterministic
code may read the FILESYSTEM, the REGISTRY and the SCHEDULER, and it may
validate what the model chose -- it may not read the user's words and choose on
their behalf. tests/test_capability.py asserts the same boundary at the
capability parser; this asserts it across the modules the parser does not
cover.

THE USER'S FREEDOM, which is the half that is easier to lose by accident. If
the installed GenPipes says an option is optional, this tool must not make it
mandatory because an agent developer thought it safer. `-s` omitted means every
step; a pipeline with a `-t` default does not have to be asked about; the same
command may be run twice. Each of those is a legitimate GenPipes run that a
well-meaning "required slots" table would refuse.

Run:  python tests/test_freedom.py
"""
import sys

from harness import Report

from genpipe import capabilities, gate, modify, prep, runs, slots


# Sentences that name pipelines, protocols, commands and failures, and select
# none of them. Every one is a real shape from the issue reports.
PROSE = [
    "should I use rnaseq or chipseq?",
    "I do NOT want chipseq",
    "does dnaseq need a pairs file?",
    "why did this run fail?",
    "can you tell me what /cancel does?",
    "what is the difference between somatic_fastpass and somatic_ensemble?",
    "I have tumor data",
    "run dnaseq later, not now",
    "/approve is the command that submits, right?",
    "diagnose_run is something you can call, isn't it?",
    "my readset is not readset.somatic_fastpass.txt",
]


def main():
    r = Report("freedom: what prose may not decide, and what GenPipes may not be denied")

    # -- the model's freedom ------------------------------------------------

    r.section("prose selects no pipeline and no protocol")
    for line in PROSE:
        state, context = prep.track(prep.Preparation(), line)
        # The ONLY thing a line may leave behind is a directory that exists.
        r.check(f"no slots recorded: {line!r}",
                not hasattr(state, "pipeline") and not hasattr(state, "protocol"))
        r.check(f"no pipeline asserted to the model: {line!r}",
                not context or "Pipeline:" not in context)

    r.section("prose is never a capability call")
    for line in PROSE:
        r.equal(f"no capability from {line!r}",
                gate.capability_request(line, capabilities.NAMES), None)

    r.section("prose is never a submission")
    for line in PROSE:
        r.check(f"not a submission: {line!r}", not gate.is_submission(line))

    r.section("prose is never an approval")
    for line in PROSE:
        r.check(f"not approval-shaped: {line!r}", not modify.is_approval_shaped(line))
    # And the one that IS approval-shaped still may not submit -- it is used to
    # REFUSE. This is the absolute boundary: /approve is the only door.
    r.check("'yes, go ahead' is recognised", modify.is_approval_shaped("yes, go ahead"))
    r.check("...and is still not a submission",
            not gate.is_submission("yes, go ahead"))

    r.section("a structured call the MODEL wrote is executed")
    got = gate.capability_request("diagnose_run(name='foo')", capabilities.NAMES)
    r.equal("diagnose_run parses", got, {"capability": "diagnose_run",
                                         "args": {"name": "foo"}})
    spec, complaint = capabilities.validate("diagnose_run", {"name": "foo"})
    r.truthy("...and validates", spec is not None and complaint is None)

    r.section("capability summaries say what, never when")
    for name, spec in capabilities.TABLE.items():
        low = spec.summary.lower()
        for banned in ("when the user", "if the user", "call this when",
                       "use this when", "asks about", "asks for"):
            r.check(f"{name} does not prescribe ({banned!r})", banned not in low)

    # -- the user's GenPipes freedom ---------------------------------------

    r.section("-s omitted is a legal run, not a gap")
    for pipeline, protocol in (("dnaseq", "germline_snv"),
                               ("dnaseq", "somatic_fastpass"),
                               ("chipseq", "chipseq"),
                               ("ampliconseq", None),
                               ("rnaseq", "stringtie")):
        found = slots.gaps(pipeline=pipeline, protocol=protocol,
                           readset="r.txt", design="d.tsv", pairs="p.csv")
        r.check(f"{pipeline}/{protocol}: steps is never a gap",
                "steps" not in [g.slot for g in found])
        r.check(f"{pipeline}/{protocol}: output is never a gap",
                "output" not in [g.slot for g in found])
    r.check("steps/output/config are the assumed rows",
            set(prep.ASSUMED) == {"steps", "config", "output"})

    r.section("a pipeline with a GenPipes -t default is not interrogated")
    # Every default here is the literal `default=` on that pipeline's -t
    # argument in the install. Having one means an unstated protocol is not a
    # gap -- the model may still ask, and that is its call, not this table's.
    for pipeline, default in sorted(slots.DEFAULTS.items()):
        found = [g.slot for g in slots.gaps(pipeline=pipeline, readset="r.txt",
                                            design="d.tsv", pairs="p.csv")]
        r.check(f"{pipeline}: unstated protocol is not a gap (default {default})",
                "protocol" not in found)
        r.truthy(f"{pipeline}: the default is a real protocol",
                 slots.find_protocol(pipeline, default))

    r.section("a pipeline with no -t is never asked for one")
    for pipeline in ("ampliconseq", "covseq", "rnaseq_light"):
        r.equal(f"{pipeline} has no protocols", slots.protocols(pipeline), [])
        r.equal(f"{pipeline} raises no protocol gap",
                slots._protocol_gap(pipeline), None)

    r.section("conditional requirements attach to the SELECTED protocol")
    # Category B: legitimate deterministic fact, but only once the protocol is
    # chosen. Nothing here reads a sentence to decide it was chosen.
    r.equal("somatic_fastpass requires pairs",
            slots.needs_of("dnaseq", "somatic_fastpass"), slots.PAIRS)
    r.equal("germline_snv requires no pairs",
            slots.needs_of("dnaseq", "germline_snv"), None)
    r.check("...and 'I have tumor data' selects neither",
            slots.needs_of("dnaseq", None) is None)

    r.section("closed menus keep a free-text escape where a value can be arbitrary")
    for slot in ("readset", "design", "pairs"):
        gap = slots.gap_for(slot, pipeline="dnaseq", protocol="somatic_fastpass")
        r.truthy(f"{slot} accepts a path not on the list", gap.free_text)
    # pipeline/protocol are the two closed sets, and closed is correct: a typed
    # eighth protocol is an invented one, and GenPipes would refuse it anyway.
    for slot in ("pipeline", "protocol"):
        gap = slots.gap_for(slot, pipeline="dnaseq")
        r.check(f"{slot} is a closed set", not gap.free_text)
        r.truthy(f"{slot} offers every legal value", len(gap.options) > 1)

    r.section("the config row always admits an ini nobody listed")
    proposal = {"slots": {"pipeline": "dnaseq", "protocol": "somatic_fastpass"}}
    options = modify.options_for("config", proposal, candidates={"config": []})
    r.truthy("config offers something", options)
    verdict = modify.check("config", "/some/where/hand_written.ini", proposal)
    r.truthy("an unlisted ini is still accepted", bool(verdict))

    r.section("focus comes from an argument, never from a sentence")
    # runs.Focus is the "the run you are working on" concept behind
    # completion. It is the most dangerous kind of feature in this codebase --
    # "work out which run they mean" is one careless step from reading prose --
    # so the boundary is asserted rather than assumed.
    known = {"foo", "bar"}.__contains__
    focus = runs.Focus()
    focus.note("view", ["foo"], known=known)
    r.equal("/view foo focuses foo", focus.name, "foo")
    r.equal("...and puts it first when it is on offer",
            [n for n, _ in focus.rank([("bar", ""), ("foo", ""), ("baz", "")])],
            ["foo", "bar", "baz"])
    r.equal("...and adds nothing when it is not",
            [n for n, _ in focus.rank([("bar", ""), ("baz", "")])],
            ["bar", "baz"])

    for line in PROSE:
        before = focus.name
        # Prose is not a command and has no argument position, so it never
        # reaches note() at all. Asserted anyway, by handing note() the words
        # themselves: even fed a sentence, nothing here reads one.
        focus.note("check", line.split(), known=known)
        r.equal(f"prose does not move the focus: {line!r}", focus.name, before)

    r.equal("a name that is not a run is not a focus",
            runs.Focus().note("check", ["nosuchrun"], known=known), None)
    r.equal("/track names a run that does not exist yet, so it focuses nothing",
            runs.Focus().note("track", ["newname"], known=lambda n: True), None)
    r.check("every focusable command really takes a run name first",
            "track" not in runs.NAMES_A_RUN and "view" in runs.NAMES_A_RUN)

    r.section("an identical rerun is not refused")
    # Nothing may compare a new run's flags against an old one's and refuse it.
    # fork_sentence() with no changes is the shape /fork produces.
    proposal = {"slots": {"pipeline": "dnaseq", "protocol": "somatic_fastpass"},
                "generated": "genpipes dnaseq -t somatic_fastpass -c a.ini "
                             "-r r.txt -p p.csv -g cmd.sh"}
    sentence = modify.fork_sentence(proposal, {})
    r.truthy("an empty change set still produces a fork instruction", sentence)
    r.check("...and it asks for the same command again",
            "again" in sentence.lower() or "exactly" in sentence.lower())

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
