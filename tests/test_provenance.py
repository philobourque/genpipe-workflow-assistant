"""Config provenance as evidence, and the line it must not cross.

The failure this suite guards against is a class, not an incident. A working
directory accumulates runs; a `-c` stack layers several files; the record of
what was used is a parse made at submission time and never re-derived. When any
of those drifts, /diagnose hands the model a picture of the run that is not the
run -- and a model applying the correct layering rule to a wrong stack produces
a confident, wrong, actionable answer.

So two properties are asserted throughout, and they pull in opposite directions
on purpose:

  ESTABLISH MORE   the stack comes from the run's own config trace, the merged
                   values come from the trace, and what each source file says is
                   read and reported. All three are transcription.

  CONCLUDE NOTHING no output of this module names a file as a cause, says a file
                   changed, or says when. Where it compares two observations it
                   reports that they differ and enumerates the situations it
                   cannot distinguish between.

EVERYTHING HERE IS SYNTHETIC. The names below -- `base.ini`, `site.ini`,
`custom.ini`, `step_alpha`, `some_resource` -- are deliberately not the ones
from any real pipeline, and the values are deliberately not walltimes. If a
check only passes because a name looked like a GenPipes name or a value looked
like a duration, the implementation has a special case in it and this suite is
where that should hurt.

Standard library only. No cluster, no model.
"""
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from genpipe import provenance
from tests.harness import Report


STAMP = "2031-02-09T04.05.06"
STEP = "step_alpha"
KEY = "cluster_mem"


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    return path


def _scene(work):
    """Four layers, of which the record remembers three, over a synthetic step.

    The shape that matters is structural: several files set the same key with
    different values, the last one in the stack is reachable only relative to
    the run's directory, and what that file says NOW is not what the trace
    recorded THEN.
    """
    lib = os.path.join(work, "lib")
    _write(os.path.join(lib, "base.ini"),
           f"[DEFAULT]\n{KEY} = 1G\n\n"
           f"[{STEP}]\n{KEY} = 64G\nsome_resource = wide\n"
           f"module_thing = vendor/thing/1.0\n")
    _write(os.path.join(lib, "site.ini"),
           f"[step_beta]\n{KEY} = 8G\n")
    _write(os.path.join(lib, "custom.ini"),
           f"[{STEP}]\n{KEY} = 4G\n")
    # Beside the run, named relatively in the command -- which is the only
    # reason workdir has to be threaded through at all.
    _write(os.path.join(work, "local.ini"), f"[{STEP}]\n{KEY} = 4G\n")

    stack = " ".join([os.path.join(lib, n) for n in
                      ("base.ini", "site.ini", "custom.ini")] + ["local.ini"])
    trace = _write(
        os.path.join(work, f"Widget.plain.{STAMP}.config.trace.ini"),
        f"# Widget Config Trace\n"
        f"# Command: /usr/bin/genpipes widget -t plain -c {stack} "
        f"-r input.txt -s 1-9 -g run.sh\n"
        f"# Created on: {STAMP}\n"
        f"# DO NOT EDIT\n\n"
        f"[{STEP}]\n{KEY} = 2G\nmodule_thing = vendor/thing/1.0\n")

    record = {
        "name": "some_run",
        "workdir": work,
        # What a parse made at submission time happened to keep: three, and not
        # the relative one.
        "proposal": {"slots": {"inis": ["base.ini", "site.ini", "custom.ini"]}},
    }
    return record, trace


# Ways of asserting an explanation, as opposed to reporting an observation.
# None of these may appear in anything this module renders.
ASSERTIONS = (
    "was edited", "has been edited", "was changed", "has changed",
    "was modified", "therefore", "caused", "responsible", "culprit",
    "the problem is", "you should", "must have", "clearly", "obviously",
)


def _sentences(text):
    """Roughly one sentence per item. Newlines end a sentence too: these are
    rendered lines, and a bullet does not always carry a full stop."""
    parts = re.split(r"[.\n]", text)
    return [p.strip() for p in parts if p.strip()]


def _denies(sentence):
    """Is this sentence saying the thing does NOT hold?"""
    return any(word in sentence for word in
               ("not ", "never", "cannot", "n't", "do not", "does not"))


def main():
    r = Report("config provenance")
    work = tempfile.mkdtemp(prefix="genpipe-prov-")
    try:
        record, trace = _scene(work)
        found = provenance.report(record, STEP, trace_path=trace, workdir=work)
        text = "\n".join(provenance.lines(found))

        # ------------------------------------------------------------------ #
        r.section("the stack comes from the run's own trace")
        layers = found["stack"]
        r.equal("all four, not the record's three", len(layers["inis"]), 4)
        r.equal("and the trace is named as the source", layers["source"], "trace")
        r.equal("in the order the command gave them",
                [os.path.basename(p) for p in layers["inis"]],
                ["base.ini", "site.ini", "custom.ini", "local.ini"])
        r.contains("including the one the record lost", text, "local.ini")

        r.section("the record's version is kept, not discarded")
        r.equal("still available for comparison", len(layers["recorded"]), 3)
        r.equal("and the two are reported as disagreeing", layers["agrees"], False)
        r.contains("said plainly", text, "not the same as the trace")
        r.contains("with the stored list shown too", text,
                   "base.ini , site.ini , custom.ini")
        r.check("and neither is declared correct",
                "correct" not in text and "wrong" not in text)

        r.section("agreement is reported honestly when there is any")
        same = dict(record)
        same["proposal"] = {"slots": {"inis":
                            [os.path.basename(p) for p in layers["inis"]]}}
        r.equal("matching stacks agree",
                provenance.stack(same, {"command": "-c " + " ".join(
                    layers["inis"])})["agrees"], True)
        r.check("and no disagreement is announced",
                "not the same as the trace" not in
                "\n".join(provenance.lines(provenance.report(
                    same, STEP, trace, work))))

        r.section("without a trace it falls back, and says so")
        bare = provenance.report(record, STEP, trace_path=None, workdir=work)
        r.equal("the record's stack is used", bare["stack"]["source"], "record")
        r.equal("all three of them", len(bare["stack"]["inis"]), 3)
        r.equal("with nothing to disagree with", bare["stack"]["agrees"], None)
        r.contains("and the fallback is stated",
                   "\n".join(provenance.lines(bare)), "no config trace was found")

        # ------------------------------------------------------------------ #
        r.section("the merged value is read from the trace")
        r.equal("what this run was actually generated with",
                found["effective"].get(KEY), "2G")
        r.contains("and labelled as the trace's", text, "AS THE TRACE RECORDS IT")

        r.section("each source file is read as it stands now")
        by_name = {os.path.basename(row["ini"]): row for row in found["sources"]}
        r.equal("the first layer", by_name["base.ini"]["settings"][KEY], "64G")
        r.equal("the third narrows it", by_name["custom.ini"]["settings"][KEY], "4G")
        r.equal("the relative one resolves against the run's workdir",
                by_name["local.ini"]["settings"][KEY], "4G")
        r.equal("a file with no such section says so, rather than nothing",
                by_name["site.ini"]["settings"], provenance.NO_SECTION)
        r.check("a DEFAULT section is not read as the step's", "1G" not in text)
        r.check("and keys outside the resource set stay out of the prompt",
                "module_thing" not in text)

        # ------------------------------------------------------------------ #
        r.section("the disagreement is surfaced as a comparison")
        keys = [row["key"] for row in found["differs"]]
        r.equal("exactly the key that differs", keys, [KEY])
        r.equal("named against the last file in the stack that sets it",
                os.path.basename(found["differs"][0]["ini"]), "local.ini")
        r.equal("with both values",
                (found["differs"][0]["trace"], found["differs"][0]["now"]),
                ("2G", "4G"))
        r.contains("the two observations are distinguished in words",
                   text, "TWO DIFFERENT OBSERVATIONS")
        r.contains("and not required to agree", text, "not required to agree")

        r.section("and what it proves is bounded explicitly")
        r.contains("the one thing it does establish", text,
                   "do not reproduce the configuration")
        r.contains("and what it does not", text, "does NOT establish")
        r.contains("no claim about which file supplied the historical value",
                   text, "which file supplied the historical value")
        r.contains("nor that anything changed, nor when",
                   text, "that any particular file was changed, or when")
        r.contains("the alternatives are enumerated rather than chosen between",
                   text, "Nothing above distinguishes between them")
        r.contains("and the way to settle it is named as more evidence",
                   text, "evidence you do not yet have")

        r.section("nothing is ever asserted as an explanation")
        # THE PROPERTY THIS MODULE EXISTS FOR, and it has to be stated
        # carefully. Phrases like "was changed" and "edited since" DO appear
        # above -- inside the sentences that say this evidence does not
        # establish them, and inside the list of situations it cannot tell
        # apart. Banning the words outright would forbid the disclaimer along
        # with the claim, so what is checked is the sentence: an explanatory
        # phrase may only occur somewhere that denies it.
        low = text.lower()
        for phrase in ASSERTIONS:
            guilty = [sentence for sentence in _sentences(low)
                      if phrase in sentence and not _denies(sentence)]
            r.check(f"never asserts {phrase!r}", not guilty, guilty[:1])
        r.check("no recommendation of any kind",
                "recommend" not in low and "should be" not in low)

        r.section("nothing that was not observed is invented")
        r.check("no ini is credited with a key it does not set",
                "local.ini: some_resource" not in text)
        r.check("no verdict, no total, no cause", "cause" not in low)

        # ------------------------------------------------------------------ #
        r.section("a path that cannot be resolved is not guessed at")
        rows = provenance.sections(["$SOME_ROOT/lib/custom.ini"], STEP, work)
        r.equal("an unexpanded variable does not resolve", rows[0]["found"], False)
        r.equal("and reports unreadable rather than a value",
                rows[0]["settings"], provenance.UNREADABLE)
        r.equal("the variable is left exactly as written",
                rows[0]["ini"], "$SOME_ROOT/lib/custom.ini")
        r.equal("a file that is not there is unreadable",
                provenance.sections(["absent.ini"], STEP, work)[0]["settings"],
                provenance.UNREADABLE)
        r.equal("and so is no path at all",
                provenance.section(None, STEP), provenance.UNREADABLE)

        r.section("it works for any step name, not a known one")
        for step in ("step_beta", "a.b.c", "UPPER_CASE", "x1"):
            got = provenance.report(record, step, trace, work)
            r.check(f"{step!r} produces a report at all",
                    isinstance(got["sources"], list) and len(got["sources"]) == 4)
        beta = provenance.report(record, "step_beta", trace, work)
        r.equal("and finds that step where it is set",
                {os.path.basename(row["ini"]): row["settings"]
                 for row in beta["sources"]}["site.ini"][KEY], "8G")
        r.equal("while the trace has nothing to say about it",
                beta["effective"], provenance.NO_SECTION)
        r.equal("so no disagreement is manufactured", beta["differs"], [])

        r.section("the quoted-key set is a projection, not a rule")
        # Swapping it changes WHICH keys are transcribed and nothing else: no
        # branch anywhere reads the tuple to decide what a value means, so the
        # same evidence is gathered about a different key with no other effect.
        other = provenance.sections(layers["inis"], STEP, work,
                                    keys=("some_resource",))
        got = {os.path.basename(row["ini"]): row["settings"]
               for row in other}
        r.equal("a key outside the default set is read when asked for",
                got["base.ini"], {"some_resource": "wide"})
        r.equal("and the default keys are then absent",
                KEY in got["custom.ini"], False)
        r.equal("keys=None takes the whole section",
                sorted(provenance.section(
                    os.path.join(work, "lib", "base.ini"), STEP, keys=None)),
                [KEY, "module_thing", "some_resource"])
        r.contains("and the selection is disclosed to the reader",
                   text, "Only the scheduler-facing keys are quoted")

        r.section("an empty stack renders nothing at all")
        r.equal("no lines",
                provenance.lines(provenance.report({"proposal": {}}, STEP,
                                                   None, work)), [])

        r.section("the block stays small enough to send every time")
        r.check("under four kilobytes for a four-ini stack",
                len(text) < 4000, f"{len(text)} chars")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
