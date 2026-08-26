"""Reading a request, finding what is missing, and staying in sync with the doc.

Three things are checked here and the third is the one that will actually catch
a regression a year from now. The ini table in slots.py and the ini table in
genpipes.md are the same knowledge written twice -- once for the panel to offer
and once for the model to read -- and nothing but a test stops them drifting
apart. A protocol added to one and forgotten in the other produces a panel that
offers a value the model has never heard of, which is exactly the failure the
table was written to prevent.
"""

import ast
import glob
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

# dnaseq no longer asks. Its `-t` carries `default="germline_snv"` in the
# install, so a person running the command by hand gets germline_snv without
# being asked, and adding a question the CLI does not ask is the agent being
# less predictable than the tool it drives. The choice is not silent: it
# reaches the approval gate as a `protocol -t` row, where /modify changes it.
gaps = slots.gaps(pipeline="dnaseq", readset="r.tsv")
r.equal("dnaseq takes GenPipes' own default rather than asking",
        [g.slot for g in gaps], [])

# A pipeline whose `-t` has NO default still asks, and offers the closed list.
gaps = slots.gaps(pipeline="covseq")
r.equal("a pipeline with no -t asks for nothing it does not have",
        [g.slot for g in gaps], ["readset"])

# The list is still complete where it is shown -- /modify offers the same one.
r.equal("all seven dnaseq protocols are still known",
        len(slots.protocols("dnaseq")), 7)

# Every DEFAULTS entry has to name a protocol that pipeline actually has, or an
# unstated -t resolves to something find_protocol() cannot look up and the run
# is built on a protocol GenPipes will reject.
for _pipeline, _default in slots.DEFAULTS.items():
    r.truthy(f"{_pipeline}'s default {_default} is one of its protocols",
             slots.find_protocol(_pipeline, _default) is not None)
# And the converse: a pipeline that takes a -t but has no default here would
# silently ask a question GenPipes answers by itself.
for _pipeline, _protos in slots.PIPELINES.items():
    if _protos:
        r.truthy(f"{_pipeline} declares a default -t",
                 _pipeline in slots.DEFAULTS)

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

# ---------------------------------------------------------------------- #
# THE WORDING IS THE AGENT'S; THE OPTIONS ARE THE TABLE'S.
#
# This used to be the other way round for any known slot -- the model's
# question= was discarded, on the grounds that the table's phrasing was the
# phrasing CI checks. That made CI the reason somebody got a worse question:
# "Which dnaseq protocol?" is fine alone and useless as the third turn of a
# conversation about tumour/normal pairs.
#
# What must NOT move with it is the option set. A model can phrase a question
# better than a constant; it cannot be trusted to enumerate seven protocols
# without eventually offering an eighth.
asked = ("You have matched normals, so this is a paired somatic run. "
         "Quick pass, or the ensemble?")
gap = slots.gap_for("protocol", pipeline="dnaseq", question=asked)
r.equal("the model's wording is used for a known slot", gap.question, asked)
r.equal("while the options still come from the table",
        [o.value for o in gap.options],
        [p.name for p in slots.protocols("dnaseq")])
r.equal("and free_text stays the table's call", gap.free_text, False)

plain = slots.gap_for("protocol", pipeline="dnaseq")
r.contains("no question= still gives the plain form", plain.question, "dnaseq")

# A file slot keeps its factual note whoever worded the question -- the note is
# a fact about GenPipes, not conversational framing.
worded = slots.gap_for("design", pipeline="rnaseq", protocol="stringtie",
                       question="Which contrasts file goes with these samples?")
r.equal("a reworded file question keeps its wording", worded.question,
        "Which contrasts file goes with these samples?")
r.contains("and keeps the table's note", worded.note, "contrasts")
r.equal("and stays free-text, because no table can list what is on disk",
        worded.free_text, True)

# Asking about a protocol without naming a pipeline is a different question --
# the pipeline is the real gap -- so the wording written for the other one is
# correctly dropped rather than pasted onto it.
redirected = slots.gap_for("protocol", question="Which dnaseq protocol?")
r.equal("a protocol ask with no pipeline redirects to the pipeline gap",
        redirected.slot, "pipeline")
r.check("and does not carry the wrong question with it",
        redirected.question != "Which dnaseq protocol?")

# gaps() and gap_for() must word the same question the same way -- they share
# the builders precisely so that the sweep and a single ask cannot diverge.
#
# Asked on the DESIGN gap rather than the protocol one, because every pipeline
# that takes a `-t` now has a documented default, so gaps() no longer produces
# a protocol question at all -- _protocol_gap survives only as the correction
# path for a protocol that was stated and is wrong. That path is checked just
# below, on its own.
r.equal("one wording, two callers",
        slots.gaps(pipeline="rnaseq", readset="r.tsv")[0].question,
        slots.gap_for("design", pipeline="rnaseq",
                      protocol="stringtie").question)
# The correction path still words itself through the same builder, and still
# refuses free text -- a protocol is a closed list however it is reached.
wrong = slots.gaps(pipeline="dnaseq", protocol="stringtie")
r.equal("a protocol from the wrong pipeline is still a gap",
        [g.slot for g in wrong], ["protocol"])
r.contains("and names the pipeline it does not belong to",
           wrong[0].question, "dnaseq")
r.truthy("protocols cannot be typed in freehand", not wrong[0].free_text)

# --------------------------------------------------------------------------
r.section("brief(): facts for the agent, not questions for the user")

text = intake.brief("run rnaseq stringtie with readset.tsv", ".")
r.contains("the original words survive verbatim", text, "run rnaseq stringtie")
# It used to say "do not ask again" here. It reports what was FOUND in the
# sentence now and leaves the reading to the agent -- a name in a question
# ("a.tsv or b.tsv?") parses identically to a name in an instruction.
r.contains("what was stated is reported as parsed, not as decided",
           text, "Names found in the request above")
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
    r.contains("as candidates", text, "Candidates only")
    r.contains("with the trap named", text, "not thereby the right readset")
    # Nothing in the brief forbids a question any more. It used to say "do not
    # ask again" of names merely PARSED out of a sentence, which is the same
    # overreach prep.goal() was deleted for: a filename in "should I use a.tsv
    # or b.tsv?" parses fine and is not an answer.
    r.truthy("and nothing in the brief forbids asking",
             "do not ask" not in text.lower())
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

# Section 1a's `default -t` column, against slots.DEFAULTS.
#
# These are the SAME FACT copied into two files, and the copy that drifts is the
# one that hurts: the panel reads slots.DEFAULTS, the model reads the table, and
# a disagreement is the assistant proposing one protocol while its own reasoning
# is grounded in another. Nothing in the run would look wrong.
#
# This is the second edge of a triangle. The section below checks slots.DEFAULTS
# against the install; this checks the table against slots.DEFAULTS; so the table
# is checked against the install too, without this half needing a mounted
# /cvmfs. Deliberate -- the edge that needs no cluster runs everywhere, and the
# one that does is the only part that can skip.
_table = doc.split("## 1a.")[1].split("\n## ")[0]
_listed = {}
for _line in _table.splitlines():
    if not _line.startswith("| `"):
        continue
    _cells = [c.strip() for c in _line.strip("|").split("|")]
    _listed[_cells[0].strip("`")] = (None if _cells[1] == "none"
                                     else _cells[1].strip("`"))

r.equal("every pipeline is in the choosing table",
        sorted(_listed), sorted(slots.PIPELINES))
for _pipeline, _shown in sorted(_listed.items()):
    r.equal(f"{_pipeline}: the table's default matches slots.py",
            _shown, slots.DEFAULTS.get(_pipeline))

# A question names the run without stuttering.
#
# The design question used to be reachable only through a protocol, so it could
# always say "<pipeline> <protocol>". Now that a pipeline can demand a design on
# its own (_PIPELINE_NEEDS), and that chipseq's default protocol is *called*
# chipseq, both halves can be the same word or one can be absent. "ampliconseq
# ampliconseq needs a design file" is not a cosmetic problem: a question that
# looks broken makes the reader distrust the answer it is asking for.
r.equal("a pipeline with no protocol names itself once",
        slots.gaps(pipeline="ampliconseq", readset="r.tsv")[0].question,
        "ampliconseq needs a design file. Which one?")
r.equal("a protocol named after its pipeline is not said twice",
        slots.gap_for("design", pipeline="chipseq", protocol="chipseq").question,
        "chipseq needs a design file. Which one?")
r.equal("and a protocol that adds information is kept",
        slots.gaps(pipeline="rnaseq", protocol="stringtie",
                   readset="r.tsv")[0].question,
        "rnaseq stringtie needs a design file. Which one?")

# --------------------------------------------------------------------------
r.section("The grammar file stays inside its budget")

# Characters, not lines. The file is mostly tables and short lines now, so a
# line count would let it double in size while the number went down. This is
# the tripwire for the decision to keep everything in one file: when it trips,
# the answer is to split into skills/, not to raise the number.
#
# RAISED ONCE, 18000 -> 21000, deliberately and against that advice. Recorded
# here rather than quietly edited, because a budget that moves without leaving
# a reason behind is not a budget.
#
# What it bought: section 1a, which tells the model which pipeline to choose.
# The file had no such guidance at all -- the word "pipeline" appeared only as
# a placeholder -- so the single most consequential choice in a run, the one
# every other flag is derived from, was being made from the model's general
# knowledge rather than from this document. That is a gap worth 1600
# characters.
#
# What was NOT done to fit: shrinking the prose until the number went green.
# About 1900 characters did come out of sections 5 and 12 first, with no fact
# lost, which is the honest kind of trim and is why the raise is only 3000 and
# not 5000. Past that point trimming would have meant deleting content to
# satisfy an arbitrary number, which is worse than moving the number.
#
# NEXT TIME IS THE SPLIT. The slack is spent. Sections 11 and 12 are ~4700
# characters of failure diagnosis that no run being BUILT needs, and that is
# the seam: load them when something has failed, not on every message. When
# this trips again, do that rather than raising it a second time.
size = len(doc)
r.truthy(f"genpipes.md is {size} chars, budget 21000", size < 21000)
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
    # No longer "the directory the request POINTS AT. This is where their data
    # is" -- one of several directories a conversation may have named, any of
    # which could be an output path or one that was ruled out.
    r.contains("labelled as a directory that was named", text,
               "directories that have been named")

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

# --------------------------------------------------------------------------
r.section("project_dir: a directory named in the request becomes first-class "
          "state, and nothing falls back to the process's own cwd")

# read() now recognises a project directory the same way find_directories()
# does -- the first one named, and only a real one.
data = tempfile.mkdtemp(prefix="project-")
try:
    stated = intake.read(f"run rnaseq on the samples in {data}")
    r.equal("project_dir is picked up by read()", stated["project_dir"], data)
    r.equal("no directory named means no project_dir",
            intake.read("run rnaseq on my samples")["project_dir"], None)
    r.equal("a directory that does not exist is not a project_dir",
            intake.read("run rnaseq on /no/such/place")["project_dir"], None)

    # candidates()/context_for() must never guess a directory: None in means
    # nothing discovered, not "whatever the caller happened to be standing in".
    # Asserted as "every bucket is empty" rather than against a literal dict:
    # the property is that nothing was DISCOVERED, and pinning the key set made
    # adding a bucket look like a regression in a test about guessing.
    r.equal("candidates(None) discovers nothing",
            sorted(p for paths in intake.candidates(None).values()
                   for p in paths), [])
    stated2, found2 = intake.context_for("hello")
    r.equal("context_for with no directory established finds nothing",
            sorted(p for paths in found2.values() for p in paths), [])

    # _resolves must not silently join a relative name against nothing.
    r.check("an absolute path can still resolve with no directory established",
            intake._resolves(__file__, None))
    r.check("a relative path resolves nothing without a directory",
            not intake._resolves("readset.tsv", None))
finally:
    shutil.rmtree(data, ignore_errors=True)

# The scenario this whole defect is named after: launched from a directory
# that happens to contain an unrelated design.tsv, asked about mouse rnaseq
# with no path in the sentence at all. brief() must never go looking on its
# own -- it only ever sees what its caller establishes as the project
# directory, and a caller that passes nothing (None) gets nothing back.
launch_cwd = tempfile.mkdtemp(prefix="unrelated-cwd-")
try:
    open(os.path.join(launch_cwd, "design.tsv"), "w").close()
    real_cwd = os.getcwd()
    os.chdir(launch_cwd)
    try:
        text = intake.brief("I want to run an rnaseq pipeline on mouse data")
        r.contains("the pipeline is still recognised", text, "pipeline: rnaseq")
        r.truthy("but nothing else is discovered, and no directory is scanned",
                 "possible" not in text and "candidates" not in text)
        r.truthy("the unrelated design.tsv is never mentioned",
                 "design.tsv" not in text)
    finally:
        os.chdir(real_cwd)
finally:
    shutil.rmtree(launch_cwd, ignore_errors=True)

# --------------------------------------------------------------------------
r.section("slots.py still agrees with the GenPipes install")

# The section above checks slots.py against genpipes.md -- two documents in
# this repo, both written by us. This one checks slots.py against the thing
# both are describing.
#
# It exists because slots.DEFAULTS and _PIPELINE_NEEDS are COPIES. Every value
# in them was read out of the install, which makes them right today and says
# nothing about tomorrow: a GenPipes upgrade that moves a `-t` default leaves
# this repo confidently choosing a protocol nobody chose, with no symptom until
# a run comes back wrong. modify.py refuses to hold a step table for exactly
# this reason. These two tables are the same hazard, and the answer is not to
# delete them -- the panel genuinely needs to know without shelling out -- but
# to make the drift LOUD.
#
# Reads the install's own source, never runs it: `genpipes --help` costs a
# module load, and this has to stay in the two-second stdlib-only suite.
#
# SKIPPED, NOT FAILED, WHERE CVMFS IS NOT MOUNTED. This suite's whole point is
# that it runs on any machine with no cluster; a check that turns CI red on a
# laptop would get deleted within the month, which would cost the check itself.


def _pipelines_dir():
    """The install's pipelines/ directory, or '' if there is no install here."""
    # $GENPIPES_INIS IS that directory -- the module sets it to the package's
    # pipelines/ folder, which is also where the inis live. Preferred over the
    # glob so a session pinned to an older module is checked against the
    # version it is actually going to run.
    here = os.environ.get("GENPIPES_INIS", "")
    if here and os.path.isdir(here):
        return here
    found = sorted(glob.glob("/cvmfs/soft.mugqic/*/software/genpipes/genpipes-*/"
                             "lib/python*/site-packages/genpipes/pipelines"))
    return found[-1] if found else ""


def _protocol_arg(tree):
    """This pipeline's -t as the install declares it: (default, choices)."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "add_argument"):
            continue
        if not ({"-t", "--type"} & {a.value for a in node.args
                                    if isinstance(a, ast.Constant)}):
            continue
        default, choices = None, ()
        for kw in node.keywords:
            if kw.arg == "default" and isinstance(kw.value, ast.Constant):
                default = kw.value.value
            if kw.arg == "choices" and isinstance(kw.value, (ast.List, ast.Tuple)):
                choices = tuple(e.value for e in kw.value.elts
                                if isinstance(e, ast.Constant))
        return default, choices
    return None, ()


_root = _pipelines_dir()
if not _root:
    print("  [SKIP] no GenPipes install mounted — install agreement unchecked")
else:
    print(f"  [ .. ] reading {_root}")
    for _name in sorted(os.listdir(_root)):
        _src = os.path.join(_root, _name, "__init__.py")
        if not os.path.exists(_src) or _name not in slots.PIPELINES:
            continue
        _tree = ast.parse(open(_src).read())
        _default, _choices = _protocol_arg(_tree)

        r.equal(f"{_name}: the -t default matches the install",
                slots.DEFAULTS.get(_name), _default)
        # Compared as SETS. The order in slots.PIPELINES is ours and is meant
        # to be -- it is the order the choice panel offers them in, common
        # first -- whereas argparse's `choices` order is whatever the install
        # happened to declare. Asserting the sequence would make a deliberate
        # UI decision look like a drift from the install.
        r.equal(f"{_name}: the protocol list matches the install",
                sorted(p.name for p in slots.PIPELINES[_name]), sorted(_choices))

        # Which pipelines consume a design, asked of the install rather than
        # asserted. A design is read by a STEP, so `main` is excluded by name:
        # it touches design_file only because that is the argparse dest, and
        # counting it would mark every pipeline as needing one.
        _reads = any(
            isinstance(n, ast.FunctionDef) and n.name != "main"
            and ("'contrasts'" in ast.dump(n) or "'design_file'" in ast.dump(n))
            for n in ast.walk(_tree))
        # slots.py says the same thing in three places, because a design can be
        # demanded by a protocol, by a pipeline that has no protocols, or be
        # known-but-not-demanded (chipseq -- see _DESIGN_OPTIONAL).
        _known = (any(p.needs == slots.DESIGN for p in slots.PIPELINES[_name])
                  or _name in slots._PIPELINE_NEEDS
                  or _name in slots._DESIGN_OPTIONAL)
        r.equal(f"{_name}: design use matches the install", _known, _reads)

# ---------------------------------------------------------------------------
# GenPipes' own config traces: records, not inputs.
#
# WHAT A TRACE IS. GenPipes writes the fully resolved configuration of every run
# it generates beside that run, as
# <Pipeline>.<protocol>.<TIMESTAMP>.config.trace.ini. It is an OUTPUT -- a
# record of a run that already happened -- and stacking one on -c asks for
# another run's entire resolved config.
#
# WHY A BUDGET AND NOT A FILTER. They are still offered, because the config
# row's panel takes no free text and a list that omitted them would make them
# unreachable rather than merely discouraged. What they may not do is CONSUME
# the list: in the directory this was reported from, six traces held six of the
# eight candidate slots, and the one hand-written ini shared the panel with a
# wall of timestamps.
# ---------------------------------------------------------------------------
r.section("config traces are records of past runs, not candidate inputs")
# THEY ARE NOT CANDIDATES AT ALL ANY MORE. They were candidates like any other
# ini and filled the panel; then they were capped at the two newest, which fixed
# the length by making the rest unreachable. Neither is the right shape for a
# list that gains an entry every time a command is generated, so they live
# behind one row instead -- see traces(), scanned on demand and never kept.
_trace_work = tempfile.mkdtemp(prefix="genpipe-traces-")
try:
    def _write(where, pipeline, protocol, stamp, script, body="[DEFAULT]\nx=1\n"):
        name = f"{pipeline}.{protocol}.{stamp}.config.trace.ini"
        path = os.path.join(where, name)
        open(path, "w").write(
            f"# {pipeline} Config Trace\n"
            f"# Command: /soft/genpipes/bin/genpipes {pipeline.lower()} "
            f"-t {protocol} -c /inis/base.ini -r r.tsv -g {script}\n"
            f"# Created on: {stamp}\n"
            f"# DO NOT EDIT\n\n{body}")
        return path

    _write(_trace_work, "DnaSeq", "somatic_fastpass", "2026-08-05T11.02.13",
           "cit_rerun.sh")
    _write(_trace_work, "DnaSeq", "germline_sv", "2026-08-04T07.15.58",
           "germline.sh")
    _write(_trace_work, "RnaSeq", "stringtie", "2026-07-11T09.00.00", "rna.sh")
    open(os.path.join(_trace_work, "override_walltime.ini"), "w").write("[x]\n")
    open(os.path.join(_trace_work, "mine.override.ini"), "w").write("[x]\n")

    _found = intake.candidates(_trace_work)["config"]
    _base = [os.path.basename(x) for x in _found]
    r.equal("no trace is offered as a candidate ini",
            [x for x in _base if x.endswith(".config.trace.ini")], [])
    r.check("while every real ini still is",
            {"override_walltime.ini", "mine.override.ini"} <= set(_base), _base)
    r.check("is_trace names the ones that were dropped",
            intake.is_trace("DnaSeq.x.2026-01-01T00.00.00.config.trace.ini")
            and not intake.is_trace("override_walltime.ini"))

    r.section("and are read on demand, from their own header")
    _traces = intake.traces(_trace_work)
    r.equal("all of them, none capped", len(_traces), 3)
    r.equal("newest first by TIMESTAMP, not by filename",
            [t["stamp"] for t in _traces],
            ["2026-08-05T11.02.13", "2026-08-04T07.15.58",
             "2026-07-11T09.00.00"])
    # The filename begins with the pipeline, so sorting on it would put RnaSeq
    # above both DnaSeq traces however old it was.
    r.equal("which is a different order from the filenames",
            _traces[0]["pipeline"], "dnaseq")

    _one = _traces[0]
    r.equal("the pipeline is read", _one["pipeline"], "dnaseq")
    r.equal("the protocol is read", _one["protocol"], "somatic_fastpass")
    r.equal("the timestamp is read", _one["stamp"], "2026-08-05T11.02.13")
    r.equal("and the script it generated", _one["script"], "cit_rerun.sh")
    r.contains("along with the command that produced it", _one["command"],
               "genpipes dnaseq -t somatic_fastpass")

    # THE HEADER WINS OVER THE NAME. A trace copied out of another directory
    # keeps the name it was given; the header is the invocation GenPipes ran.
    _odd = _write(_trace_work, "DnaSeq", "germline_snv", "2026-01-01T00.00.00",
                  "moved.sh")
    _renamed = os.path.join(_trace_work,
                            "RnaSeq.stringtie.2026-01-01T00.00.00.config.trace.ini")
    os.rename(_odd, _renamed)
    _read = intake.read_trace(_renamed)
    r.equal("the pipeline comes from the command, not the filename",
            _read["pipeline"], "dnaseq")
    r.equal("and so does the protocol", _read["protocol"], "germline_snv")
    os.remove(_renamed)

    # NEVER A GUESS. A trace with no readable header still yields what its name
    # says and nothing more.
    _bare = os.path.join(_trace_work,
                         "CovSeq.default.2026-02-02T02.02.02.config.trace.ini")
    open(_bare, "w").write("[DEFAULT]\nnothing = here\n")
    _read = intake.read_trace(_bare)
    r.equal("the name still answers what it can", _read["pipeline"], "covseq")
    r.equal("and what it cannot is left empty, not invented",
            (_read["script"], _read["command"]), ("", ""))
    os.remove(_bare)

    r.check("a file that is not a trace is not read as one",
            intake.read_trace(os.path.join(_trace_work,
                                           "override_walltime.ini")) is None)
    r.equal("and a directory with none has none", intake.traces(HERE), [])
    r.equal("as does no directory at all", intake.traces(None), [])
finally:
    shutil.rmtree(_trace_work, ignore_errors=True)

r.section("a directory full of traces leaves the candidates alone")
_work = tempfile.mkdtemp(prefix="genpipe-trace-")
try:
    _names = ["override_walltime.ini", "my_overlay.ini", "somebody.override.ini",
              "override_walltime.ini.bak"] + [
        f"DnaSeq.somatic_fastpass.2026-0{_n}-01T10.00.00.config.trace.ini"
        for _n in range(1, 7)]
    for _name in _names:
        open(os.path.join(_work, _name), "w").write("[x]\n")
    _found = intake.candidates(_work)["config"]
    _base = [os.path.basename(x) for x in _found]
    _traces = [x for x in _base if x.endswith(".config.trace.ini")]

    r.equal("six traces, and not one of them is a candidate", _traces, [])
    r.check("every hand-written ini survives them",
            {"override_walltime.ini", "my_overlay.ini"} <= set(_base), _base)
    r.check("a hand-written ini still ranks above a private override",
            _base.index("my_overlay.ini")
            < _base.index("somebody.override.ini"), _base)
    r.check("nothing is hidden that is not a trace",
            _base.count("my_overlay.ini") == 1, _base)
    r.check("and a .ini.bak was never an ini",
            not any(x.endswith(".bak") for x in _base), _base)

    # The candidate paths are anchored to the directory they were found in, so
    # the panel can tell two same-named inis apart and execution stays exact.
    r.check("candidates carry the directory they came from",
            all(os.path.dirname(x) == _work for x in _found), _found)
finally:
    shutil.rmtree(_work, ignore_errors=True)

# ========================================================================== #
r.section("where the session is, without looking at what is in it")
# THE BOUNDARY, as a test, because it is one line of code away from being the
# old bug again. AGENT-FIXES.md defect 1a was brief() taking os.getcwd() as the
# DISCOVERY ROOT: it listed that directory, bucketed anything named like a
# readset or a design, and offered this repo's own committed design.tsv as a
# candidate for whatever had been asked. 1594469 stopped it guessing a
# directory at all.
#
# The run output directory has to come back -- without it the model has no
# location facts and reaches for the `where` capability in the middle of
# preparing a run -- so what must hold is that it comes back as a STATED FACT
# and never as a place to search.
_yard = tempfile.mkdtemp(prefix="genpipe_phantom_")
try:
    for _name, _head in (("design.tsv", "Sample\tContrast\n"),
                         ("readset.tsv", "Sample\tReadset\n")):
        with open(os.path.join(_yard, _name), "w") as _f:
            _f.write(_head)

    said = intake.brief("run rnaseq stringtie steps 1-5", workdir=_yard)
    r.contains("the run output directory is stated", said, _yard)
    r.contains("and labelled as where output lands", said,
               "run output directory")
    for ghost in ("design.tsv", "readset.tsv"):
        r.check(f"{ghost} sitting in it is not offered", ghost not in said, said)
    r.check("no candidate block is built from it",
            "possible design" not in said and "possible readset" not in said,
            said)
    r.contains("and the brief says what these facts are not", said,
               "not files found in it")

    # The same directory NAMED in the request is a different thing and must
    # still be read. That is the behaviour brief() exists for, and the test
    # that stops this section being satisfied by breaking discovery entirely.
    named = intake.brief(f"run rnaseq on the data in {_yard}", workdir=_yard)
    r.contains("a directory the person named is still listed", named,
               "possible readset")

    r.section("...and the facts stop where /where's business begins")
    # Three facts, each a token in the command being written or deciding its
    # content. The rest of /where's rows change nothing the model writes.
    r.contains("the cluster is briefed", said, "cluster:")
    r.contains("and its ini, which lands on -c", said, "cluster ini:")
    for unwanted in ("agent workdir", "run registry", "checkpoints",
                     "settings", "this copy"):
        r.check(f"{unwanted!r} is not briefed", unwanted not in said, said)

    r.section("and talk is left alone")
    # Anchored on the workdir the caller passes. The cluster is readable from
    # the machine at any moment, so emitting it unanchored would put a context
    # block under "hello" and turn every line of talk into a briefing.
    r.equal("bare talk is returned exactly as typed",
            intake.brief("hello"), "hello")
    # A pipeline name in a question is still parsed and reported -- that is
    # brief()'s existing job and not what this section is about. What must be
    # absent without a workdir is the LOCATION block.
    asked = intake.brief("what does rnaseq_light do?")
    r.contains("a named pipeline is still reported", asked, "rnaseq_light")
    for absent in ("run output directory", "Where this session is", "cluster:"):
        r.check(f"but {absent!r} is not", absent not in asked, asked)
finally:
    shutil.rmtree(_yard, ignore_errors=True)

sys.exit(r.finish())
