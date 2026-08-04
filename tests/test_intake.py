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

from genpipe import intake
from genpipe import slots
from harness import Report

HERE = os.path.dirname(os.path.abspath(__file__))
GRAMMAR = os.path.join(HERE, "..", "genpipe", "genpipes.md")

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
r.section("gap_for(): the one question the agent asked for")

gap = slots.gap_for("protocol", pipeline="dnaseq")
r.equal("asking for a protocol gets the protocol gap", gap.slot, "protocol")
r.equal("with every legal value and no others", len(gap.options), 7)
r.truthy("still closed to freehand answers", not gap.free_text)
r.truthy("the options come from the table, not the caller",
         all(o.value in {p.name for p in slots.protocols("dnaseq")}
             for o in gap.options))

# The most likely malformed ask, and the one worth recovering from rather than
# refusing: the pipeline is the real gap, and answering it first is the order
# gaps() exists to enforce.
gap = slots.gap_for("protocol")
r.equal("a protocol asked of no pipeline asks the pipeline instead",
        gap.slot, "pipeline")

gap = slots.gap_for("readset", readset_candidates=["a.txt", "b.txt"])
r.equal("candidates on disk become the options", len(gap.options), 2)
r.truthy("a file gap always allows free text", gap.free_text)
r.truthy("and explains what the file is", bool(gap.note))

gap = slots.gap_for("readset")
r.equal("nothing on disk means no options", len(gap.options), 0)
r.truthy("but the question is still askable", bool(gap.question))

gap = slots.gap_for("pairs", pipeline="dnaseq", protocol="somatic_ensemble")
r.contains("the question names what it is for", gap.question, "somatic_ensemble")

gap = slots.gap_for(None, question="Which genome build?")
r.equal("a question with no slot is still a gap", gap.slot, None)
r.equal("worded by the model", gap.question, "Which genome build?")
r.equal("with nothing to choose between", len(gap.options), 0)

r.equal("a slotless, questionless ask has nothing to render",
        slots.gap_for(None), None)
r.equal("and a protocol for a pipeline that takes none has nothing to ask",
        slots.gap_for("protocol", pipeline="covseq"), None)

# gaps() and gap_for() must word the same question the same way -- they share
# the builders precisely so that the sweep and a single ask cannot diverge.
r.equal("one wording, two callers",
        slots.gaps(pipeline="dnaseq")[0].question,
        slots.gap_for("protocol", pipeline="dnaseq").question)

# --------------------------------------------------------------------------
r.section("brief(): facts for the agent, not questions for the user")

text = intake.brief("run rnaseq stringtie with readset.tsv", ".")
r.contains("the original words survive verbatim", text, "run rnaseq stringtie")
r.contains("what was stated is marked settled", text, "do not ask again")
r.contains("including the pipeline", text, "pipeline: rnaseq")
r.contains("and the file named in the sentence", text, "readset: readset.tsv")

# The distinction the wording has to carry: a file that looks like a readset is
# a candidate, not a decision. Getting this backwards is how an agent silently
# runs on the wrong samples.
tmp = tempfile.mkdtemp(prefix="brief-")
try:
    for name in ("readset.rnaseq.txt", "design.rnaseq.txt", "notes.md"):
        open(os.path.join(tmp, name), "w").close()
    text = intake.brief("do the usual rnaseq thing", tmp)
    r.contains("files on disk are offered", text, "readset.rnaseq.txt")
    r.contains("as candidates", text, "candidates, not")
    r.truthy("and are never presented as settled",
             "do not ask again" not in text.split("candidates")[1])
    r.truthy("an irrelevant file is left out", "notes.md" not in text)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

empty = tempfile.mkdtemp(prefix="brief-empty-")
try:
    r.equal("nothing to add means the text is untouched",
            intake.brief("hello", empty), "hello")
finally:
    shutil.rmtree(empty, ignore_errors=True)

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
r.section("The intent that was never in a field survives")

# The reason brief() appends instead of rewriting. A request rebuilt from
# parsed fields loses "the thing Marie asked about", and that clause is often
# the part that decides whether the answer is any use.
original = "run the rnaseq thing Marie asked about"
text = intake.brief(original, ".")
r.contains("the whole sentence is still there", text, original)
r.truthy("and comes first", text.startswith(original))

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
from genpipe import ui


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

# --------------------------------------------------------------------- #
r.section("a directory the request names is read before anything is asked")
# The whole of the observed failure: a request that gave the folder the data
# was in was answered with "Which readset file?", while the agent held the path
# to the directory the readset was sitting in.
data = tempfile.mkdtemp(prefix="named-")
launch = tempfile.mkdtemp(prefix="launch-")
try:
    open(os.path.join(data, "myReadset.tsv"), "w").close()
    for i in range(9):
        open(os.path.join(data, f"S{i}_R1.fastq.gz"), "w").close()
    open(os.path.join(launch, "design.tsv"), "w").close()

    r.equal("the named directory is found", intake.find_directories(
        f"rna-seq on Rorqual (9 samples): {data}"), [data])
    r.equal("a directory that does not exist is not followed",
            intake.find_directories("look in /no/such/place"), [])
    r.check("and a bare word with no slash sends us nowhere",
            intake.find_directories("run rnaseq stringtie") == [])

    text = intake.brief(f"mouse rna-seq fastq on Rorqual (9 samples): {data}",
                        directory=launch)
    r.contains("the readset in it is offered", text, "myReadset.tsv")
    r.contains("labelled as the directory the request points at", text, "POINTS AT")

    # ----------------------------------------------------------------- #
    r.section("a file that is named but misspelled is corrected, not dropped")
    # One character -- myReadset.ts for myReadset.tsv -- and the run was built
    # with no readset at all, after echoing the answer back as accepted.
    r.equal("the nearest real file is found",
            intake.near_miss(os.path.join(data, "myReadset.ts")),
            os.path.join(data, "myReadset.tsv"))
    r.equal("a name with no near match stays unresolved",
            intake.near_miss(os.path.join(data, "nothing_like_it.ts")), None)
    open(os.path.join(data, "myReadset.csv"), "w").close()
    r.equal("and two matches are a question, not a correction",
            intake.near_miss(os.path.join(data, "myReadset.ts")), None)

    # ----------------------------------------------------------------- #
    r.section("files are on the command line because somebody said so")
    # A design.tsv in the directory the app was launched from reached the
    # command line as -d, naming a file with nothing to do with the data.
    stated = intake.find_files(intake.brief("run rnaseq stringtie",
                                            directory=launch))
    r.equal("a file merely lying around is not picked up",
            stated["design"], None)
    r.equal("but a file the person names still is",
            intake.find_files("use design.tsv for contrasts")["design"],
            "design.tsv")
finally:
    shutil.rmtree(data, ignore_errors=True)
    shutil.rmtree(launch, ignore_errors=True)

sys.exit(r.finish())
