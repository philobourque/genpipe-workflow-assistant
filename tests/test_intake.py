"""Reading a request, finding what is missing, and staying in sync with the doc.

Three things are checked here and the third is the one that will actually catch
a regression a year from now. The ini table in slots.py and the ini table in
genpipes.md are the same knowledge written twice -- once for the panel to offer
and once for the model to read -- and nothing but a test stops them drifting
apart. A protocol added to one and forgotten in the other produces a panel that
offers a value the model has never heard of, which is exactly the failure the
table was written to prevent.
"""

import os
import re
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import intake
import slots
from harness import Report

HERE = os.path.dirname(os.path.abspath(__file__))
GRAMMAR = os.path.join(HERE, "..", "genpipes.md")

r = Report("intake and slots")

# --------------------------------------------------------------------------
r.section("Reading what a request already states")

r.equal("finds the pipeline", intake.find_pipeline("run rnaseq on my samples"),
        "rnaseq")
r.equal("longest match wins over its own prefix",
        intake.find_pipeline("do a rnaseq_denovo_assembly run"),
        "rnaseq_denovo_assembly")
r.equal("hyphen and underscore are interchangeable",
        intake.find_pipeline("longread-dnaseq please"), "longread_dnaseq")
r.equal("no pipeline named is None", intake.find_pipeline("align some reads"),
        None)
# Word boundaries: a pipeline name inside a longer word is not a mention of it.
r.equal("not matched inside a longer word",
        intake.find_pipeline("the rnaseqc2 metrics step"), None)

r.equal("finds a protocol", intake.find_protocol("dnaseq somatic_ensemble run",
                                                 "dnaseq"), "somatic_ensemble")
r.equal("prefers the longer protocol name",
        intake.find_protocol("germline_high_cov please", "dnaseq"),
        "germline_high_cov")
r.equal("a protocol from another pipeline does not match",
        intake.find_protocol("stringtie", "dnaseq"), None)

files = intake.find_files("use readset.dnaseq.txt and pairs.somatic.csv")
r.equal("readset by name", files["readset"], "readset.dnaseq.txt")
r.equal("pairs by name", files["pairs"], "pairs.somatic.csv")
r.equal("design absent", files["design"], None)
# A file whose name says nothing about its role is deliberately not guessed.
r.equal("an unlabelled tsv is not assumed to be the readset",
        intake.find_files("use samples.tsv")["readset"], None)

# --------------------------------------------------------------------------
r.section("Gaps: what still has to be asked")

gaps = slots.gaps()
r.equal("nothing stated asks for the pipeline first", gaps[0].slot, "pipeline")
r.equal("and asks nothing else yet", len(gaps), 1)
r.truthy("the pipeline list is closed", not gaps[0].free_text)

gaps = slots.gaps(pipeline="dnaseq")
r.equal("dnaseq needs a protocol", gaps[0].slot, "protocol")
r.equal("protocol is asked alone", len(gaps), 1)
r.equal("all seven offered", len(gaps[0].options), 7)
r.truthy("protocols cannot be typed in freehand", not gaps[0].free_text)

# rnaseq has a documented default, so an unstated protocol is not a gap -- but
# the design file that default requires is.
gaps = slots.gaps(pipeline="rnaseq", readset="r.tsv")
r.equal("rnaseq defaults its protocol", [g.slot for g in gaps], ["design"])
r.truthy("a file gap allows free text", gaps[0].free_text)

gaps = slots.gaps(pipeline="dnaseq", protocol="somatic_ensemble")
r.equal("somatic_ensemble needs readset and pairs",
        sorted(g.slot for g in gaps), ["pairs", "readset"])

gaps = slots.gaps(pipeline="dnaseq", protocol="germline_snv", readset="r.tsv")
r.equal("germline_snv needs neither design nor pairs", gaps, [])

# chipseq skips differential binding rather than failing, so demanding a design
# would block a legitimate peak-calling-only run.
gaps = slots.gaps(pipeline="chipseq", protocol="chipseq", readset="r.tsv")
r.equal("chipseq does not demand a design", gaps, [])

gaps = slots.gaps(pipeline="notapipeline")
r.equal("an unknown pipeline is re-asked", gaps[0].slot, "pipeline")
gaps = slots.gaps(pipeline="dnaseq", protocol="not_a_protocol")
r.equal("an invalid protocol is re-asked", gaps[0].slot, "protocol")

r.equal("expected inis for somatic_ensemble",
        slots.expected_inis("dnaseq", "somatic_ensemble"), ("dnaseq.cancer.ini",))
r.equal("germline_snv genuinely takes none",
        slots.expected_inis("dnaseq", "germline_snv"), ())
r.equal("an unknown protocol is None, not empty",
        slots.expected_inis("dnaseq", "nope"), None)

# --------------------------------------------------------------------------
r.section("resolve(): one question at a time, and escapable")

asked = []


def answer_with(script):
    def asker(gap):
        asked.append(gap.slot)
        return script.get(gap.slot)
    return asker


asked.clear()
stated, cancelled = intake.resolve(
    "run a dnaseq somatic ensemble",
    asker=answer_with({"protocol": "somatic_ensemble",
                       "readset": "r.tsv", "pairs": "p.csv"}))
r.truthy("not cancelled", not cancelled)
r.equal("pipeline was read, not asked", stated["pipeline"], "dnaseq")
r.equal("pairs came from the panel", stated["pairs"], "p.csv")
r.truthy("protocol was never asked -- it was already in the sentence",
         "protocol" not in asked)

asked.clear()
stated, cancelled = intake.resolve("run dnaseq", asker=answer_with({}))
r.truthy("declining an answer is not a cancellation", not cancelled)
r.equal("and stops asking", len(asked), 1)


def refuser(gap):
    raise KeyboardInterrupt


stated, cancelled = intake.resolve("run dnaseq", asker=refuser)
r.truthy("Ctrl+C cancels the whole intake", cancelled)

stated, cancelled = intake.resolve("run rnaseq stringtie with readset.tsv")
r.truthy("no asker means no questions", not cancelled)
r.equal("but the request is still read", stated["pipeline"], "rnaseq")

# --------------------------------------------------------------------------
r.section("Candidates come from the working directory")

tmp = tempfile.mkdtemp(prefix="intake-")
try:
    for name in ("readset.rnaseq.txt", "design.rnaseq.txt", "notes.md",
                 "pairs.csv", "random.tsv"):
        open(os.path.join(tmp, name), "w").close()
    found = intake.candidates(tmp)
    r.equal("one readset found", len(found["readset"]), 1)
    r.equal("one design found", len(found["design"]), 1)
    r.equal("one pairs found", len(found["pairs"]), 1)
    r.truthy("an unlabelled tsv is offered for nothing",
             all("random.tsv" not in p for bucket in found.values() for p in bucket))
    r.equal("a missing directory is empty, not an error",
            intake.candidates(os.path.join(tmp, "nope"))["readset"], [])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# --------------------------------------------------------------------------
r.section("restate() adds answers without discarding the words")

original = "run the thing Marie asked about"
text = intake.restate(original, {"pipeline": "rnaseq", "readset": "r.tsv",
                                 "protocol": None, "design": None, "pairs": None})
r.contains("the original request survives verbatim", text, original)
r.contains("the answer is appended", text, "readset: r.tsv")
r.equal("nothing new means nothing appended",
        intake.restate("run rnaseq", intake.read("run rnaseq")), "run rnaseq")

# --------------------------------------------------------------------------
r.section("slots.py and genpipes.md still agree")

doc = open(GRAMMAR).read()

for pipeline, protocols in slots.PIPELINES.items():
    r.contains(f"{pipeline} appears in the grammar", doc, pipeline)
    for proto in protocols:
        r.contains(f"{pipeline}/{proto.name} appears in the grammar",
                   doc, proto.name)
        for ini in proto.inis:
            r.contains(f"{proto.name} names {ini} in the grammar", doc, ini)

# And the reverse: every feature ini the document mentions in its table has to
# be claimed by some protocol here, or the panel will never offer it.
table_inis = set(re.findall(r"`([a-z_]+\.[a-z_0-9]+\.ini)`", doc))
claimed = {ini for protos in slots.PIPELINES.values()
           for p in protos for ini in p.inis}
# base, batch and cit inis are layers, not protocol features; data overlays are
# chosen by what the reads are rather than by -t, and slots.py says which.
structural = {i for i in table_inis
              if i.endswith((".base.ini", ".batch.ini")) or i.startswith("cit")}
unclaimed = table_inis - claimed - structural - set(slots.DATA_OVERLAYS)
r.equal("no feature ini in the doc is missing from the table",
        sorted(unclaimed), [])
r.truthy("the exome overlay is declared orthogonal, not forgotten",
         "dnaseq.exome.ini" in slots.DATA_OVERLAYS)

# --------------------------------------------------------------------------
r.section("The grammar file stays inside its budget")

# Characters, not lines. The file is mostly tables and short lines now, so a
# line count would let it double in size while the number went down. This is
# the tripwire for the decision to keep everything in one file: when it trips,
# the answer is to split into skills/, not to raise the number.
size = len(doc)
r.truthy(f"genpipes.md is {size} chars, budget 18000", size < 18000)
r.truthy("and has not collapsed to a stub", size > 6000)

# The rule the whole structure rests on: step numbers live in --help, never
# here, because a copied step list goes stale on the next module bump.
step_lists = re.findall(r"^\s*\d+\s+[a-z_]+\s*$", doc, re.M)
r.equal("no step list has crept back into the document", step_lists, [])
r.contains("and --help is named as the authority", doc, "--help")

# --------------------------------------------------------------------------
r.section("The choice panel, headless")

import builtins
import ui


def answers(*script):
    it = iter(script)
    return lambda _prompt="": next(it)


options = [slots.Option("stringtie", "stringtie", "the usual choice"),
           slots.Option("variants", "variants", "variant calling")]

real_input = builtins.input
try:
    builtins.input = answers("1")
    r.equal("a number selects", ui.choose("Which?", options, free_text=False),
            "stringtie")

    builtins.input = answers("variants")
    r.equal("the value itself also selects",
            ui.choose("Which?", options, free_text=False), "variants")

    builtins.input = answers("9")
    r.equal("an out-of-range number is not a choice",
            ui.choose("Which?", options, free_text=False), None)

    builtins.input = answers("")
    r.equal("an empty answer declines",
            ui.choose("Which?", options, free_text=False), None)

    # The free-text row is the reason this is a panel and not a form: it must
    # always be reachable and must never be confused with a listed option.
    builtins.input = answers("3", "my_own_protocol")
    r.equal("the last row opens free text",
            ui.choose("Which?", options, free_text=True), "my_own_protocol")

    builtins.input = answers("2")
    r.equal("free text does not shift the real options",
            ui.choose("Which?", options, free_text=True), "variants")
finally:
    builtins.input = real_input

sys.exit(r.finish())
