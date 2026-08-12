"""What a run needs before it can be generated, and what to ask when it is missing.

The point of this module is that the question comes from a table, not from the
model. `genpipes dnaseq --help` will tell you that -t accepts one of seven
values; it will not tell you that somatic_ensemble needs dnaseq.cancer.ini in
the -c stack and a pairs file in -p. That mapping exists nowhere on the install
-- not in --help, not in the shipped READMEs, not in the ini files themselves,
which are pure config with no protocol declaration. It is the one genuinely
irreplaceable thing this project knows about GenPipes.

Keeping it here as data rather than as prose in the grammar document buys three
things. The choice panel can offer exactly the legal protocols and no invented
ones. The gate can tell whether a proposal's -c stack matches its -t. And CI can
assert this table and the table in genpipes.md still say the same thing, so the
document and the behaviour cannot drift apart silently.

Standard library only. No filesystem access: callers that want to offer real
files on disk pass them in.
"""

DESIGN = "design"
PAIRS = "pairs"


class Protocol:
    """One legal -t value: its feature inis, what else it forces, and a line of
    plain English for the operator choosing between them."""

    __slots__ = ("name", "inis", "needs", "blurb")

    def __init__(self, name, inis=(), needs=None, blurb=""):
        self.name = name
        self.inis = tuple(inis)
        self.needs = needs
        self.blurb = blurb

    def __repr__(self):
        return f"<Protocol {self.name}>"


def _p(name, inis=(), needs=None, blurb=""):
    return Protocol(name, inis, needs, blurb)


# Feature inis are named bare; the caller prefixes $GENPIPES_INIS/<pipeline>/.
# They go between the base ini and the cluster ini -- see genpipes.md section 5.
PIPELINES = {
    "dnaseq": [
        _p("germline_snv", (), None,
           "SNVs and small indels in a normal genome"),
        _p("germline_sv", ("dnaseq.sv.ini",), None,
           "structural variants in a normal genome"),
        _p("germline_high_cov", ("dnaseq.high_cov.ini",), None,
           "deep-coverage germline calling"),
        _p("somatic_tumor_only", (), None,
           "tumour with no matched normal"),
        _p("somatic_fastpass", ("dnaseq.cancer.ini",), PAIRS,
           "quick tumour/normal pass"),
        _p("somatic_ensemble", ("dnaseq.cancer.ini",), PAIRS,
           "tumour/normal, several callers combined"),
        _p("somatic_sv", ("dnaseq.cancer.ini",), PAIRS,
           "structural variants, tumour versus normal"),
    ],
    "rnaseq": [
        _p("stringtie", (), DESIGN,
           "expression and differential expression (the usual choice)"),
        _p("variants", (), None,
           "variant calling from RNA"),
        _p("cancer", (), None,
           "variants plus gene-fusion detection"),
    ],
    "chipseq": [
        _p("chipseq", (), None,
           "peak calling; add a design for differential binding"),
        _p("atacseq", (), None,
           "ATAC-seq through the same pipeline; mark column must be 'atac'"),
    ],
    "methylseq": [
        _p("bismark", (), DESIGN, "the standard aligner"),
        _p("gembs", (), DESIGN, "gemBS aligner"),
        _p("hybrid", ("methylseq.hybrid.ini",), DESIGN, "hybrid capture data"),
        _p("dragen", ("methylseq.dragen.ini",), DESIGN, "DRAGEN aligner"),
    ],
    "longread_dnaseq": [
        _p("nanopore", (), None, "Oxford Nanopore reads"),
        _p("revio", (), None, "PacBio Revio reads"),
        _p("nanopore_paired_somatic", ("longread_dnaseq.cancer.ini",), PAIRS,
           "tumour/normal from Nanopore reads"),
    ],
    "rnaseq_denovo_assembly": [
        _p("trinity", (), DESIGN, "Trinity de novo assembly"),
        _p("seq2fun", (), DESIGN, "Seq2Fun functional profiling"),
    ],
    "nanopore_covseq": [
        _p("default", (), None, "from basecalled reads"),
        _p("basecalling", (), None, "basecall first, then analyse"),
    ],
    # No -t at all. Present so that "is this a real pipeline" and "does it take
    # a protocol" are one lookup rather than two.
    "ampliconseq": [],
    "covseq": [],
    "rnaseq_light": [],
}

# Where GenPipes itself marks a default, an unspecified protocol is not a gap.
#
# Every entry is the literal `default=` on that pipeline's `-t` argument, read
# out of the install rather than decided here:
#
#   .../site-packages/genpipes/pipelines/<pipeline>/__init__.py
#
# This table used to hold only rnaseq, which meant the other six pipelines
# asked "which protocol?" for a question GenPipes answers by itself -- and
# worse, made the agent's choice look like a judgement when it was reproducing
# a documented default. Deferring to the install is also the only version-safe
# story: when 6.2 changes a default, this is wrong in an obvious place rather
# than subtly wrong everywhere.
#
# The three pipelines absent from this table take no `-t` at all -- see the
# empty lists above -- so there is no default for them to have.
DEFAULTS = {
    "chipseq": "chipseq",
    "dnaseq": "germline_snv",
    "longread_dnaseq": "nanopore",
    "methylseq": "bismark",
    "nanopore_covseq": "default",
    "rnaseq": "stringtie",
    "rnaseq_denovo_assembly": "trinity",
}

# Pipelines that take no `-t` and still consume a design file.
#
# `needs` hangs off a PROTOCOL, so a pipeline with no protocols had nowhere to
# record that it needs anything -- and ampliconseq and rnaseq_light fell
# straight through gaps(), which reported no missing design for a run that
# cannot generate without one.
#
# Verified against the install rather than decided here, by asking which steps
# reach `self.contrasts`: ampliconseq's `asva` and rnaseq_light's
# `sleuth_differential_expression` both do. covseq and nanopore_covseq -- the
# other two pipelines that take no `-t` -- reference it nowhere, which is why
# they are not here and must not be added.
#
# WHAT "REQUIRED" REALLY MEANS, because it is narrower than it looks. The
# design is read by ONE step, and `-s` decides whether that step runs at all:
# `asva` is step 7 of 8, `sleuth_differential_expression` step 7 of 8. Below
# those, a run needs no design. That is a fact about step numbers, and step
# numbers are the one thing this repo refuses to hold (see modify.py's note on
# why there is no step table here and must never be one), so this table cannot
# express it. It asks whenever a design step EXISTS -- a question that can be
# declined -- and genpipes.md carries the precise rule for the model, which
# reads `--help` and does know the numbers.
_PIPELINE_NEEDS = {
    "ampliconseq": DESIGN,
    "rnaseq_light": DESIGN,
}

# Feature inis that are chosen by what the *data* is, not by which protocol was
# picked. dnaseq.exome.ini sets experiment_type=exome and touches both germline
# sections (gatk_haplotype_caller) and somatic ones (gatk_mutect2,
# strelka2_paired_somatic), so it stacks alongside a protocol ini rather than
# instead of one. Listed here so nothing treats an unclaimed overlay as a gap in
# the table.
DATA_OVERLAYS = {
    "dnaseq.exome.ini": "capture/exome reads rather than whole-genome",
}

# Pipelines this table does not ask a design for even though one of their steps
# reads it.
#
# The comment here used to say chipseq "skips the differential-binding step when
# there is no valid design rather than failing". That is not what the install
# does. `differential_binding` opens with `if self.contrasts:`, which looks like
# a guard and is not one: `contrasts` reaches `design_file`, and that property
# raises MissingInputError when no `-d` was given. Nothing anywhere catches it.
# So chipseq without a design does not skip a step -- it fails to generate, the
# moment step 19 is in range.
#
# chipseq stays here anyway, for the reason the old comment was reaching for
# even though it got the mechanism wrong: peak calling without differential
# binding is an ordinary, complete chipseq run, `differential_binding` is step
# 19 of 24, and `-s 1-18` is a normal thing to ask for. Demanding a design up
# front would block it. The trade is deliberate -- a run that DOES include step
# 19 fails at generation, which costs nothing but a message, whereas a question
# nobody can answer blocks work that was always legitimate.
_DESIGN_OPTIONAL = {"chipseq"}


class Option:
    """One line in a choice panel."""

    __slots__ = ("value", "label", "description")

    def __init__(self, value, label=None, description=""):
        self.value = value
        self.label = label or value
        self.description = description

    def __repr__(self):
        return f"<Option {self.value}>"


class Gap:
    """A piece of information a run needs and does not have.

    free_text says whether "something else" makes sense as an answer. It is true
    for anything naming a file, since no table can enumerate what is on disk,
    and false for a protocol, where the legal values are known exactly and a
    typed-in eighth option would just be a way to invent one.
    """

    __slots__ = ("slot", "question", "options", "free_text", "note")

    def __init__(self, slot, question, options=(), free_text=True, note=""):
        self.slot = slot
        self.question = question
        self.options = list(options)
        self.free_text = free_text
        self.note = note

    def __repr__(self):
        return f"<Gap {self.slot}>"


def as_data(gap):
    """A Gap as plain JSON-safe data, and from_data() is the way back.

    Needed because a question crosses a boundary that only carries JSON. The ask
    node pauses the graph with interrupt(), and LangGraph writes that payload
    into the SQLite checkpoint through its JSON serializer -- which refuses an
    object it does not recognise:

        TypeError: Object of type Gap is not serializable

    That refusal happened after the model had already decided to ask, so the turn
    died on the way to drawing the panel. Converting here rather than teaching the
    serializer about our classes keeps the pause durable in the way that matters:
    the payload is data, so a question can be re-read by a different process, or
    by a version of this code whose Gap has different fields.
    """
    if gap is None:
        return None
    return {
        "slot": gap.slot,
        "question": gap.question,
        "free_text": bool(gap.free_text),
        "note": gap.note,
        "options": [{"value": o.value, "label": o.label,
                     "description": o.description} for o in gap.options],
    }


def from_data(data):
    """Rebuild a Gap from as_data()'s output. Tolerant of missing keys, so a
    payload written by an older version still opens a usable panel."""
    if not data:
        return None
    if isinstance(data, Gap):
        return data
    return Gap(data.get("slot"),
               data.get("question") or "",
               [Option(o.get("value"), o.get("label"), o.get("description", ""))
                for o in data.get("options") or ()],
               free_text=bool(data.get("free_text", True)),
               note=data.get("note") or "")


def protocols(pipeline):
    """Legal -t values for a pipeline, or [] if it takes none."""
    return list(PIPELINES.get(pipeline, []))


def find_protocol(pipeline, name):
    for proto in PIPELINES.get(pipeline, []):
        if proto.name == name:
            return proto
    return None


def known(pipeline):
    return pipeline in PIPELINES


def expected_inis(pipeline, protocol):
    """The feature inis this protocol wants between base and cluster.

    Bare filenames. Empty tuple is a real answer -- germline_snv genuinely takes
    none -- so callers must distinguish it from an unknown protocol, which
    returns None.
    """
    proto = find_protocol(pipeline, protocol)
    return None if proto is None else proto.inis


# ---------------------------------------------------------------------------
# One builder per slot. Both callers below go through these, so a question is
# worded once no matter whether it was reached by sweeping a request for
# everything missing or by the agent asking for one specific thing.
# ---------------------------------------------------------------------------

def _pipeline_gap(stated=None):
    question = ("Which pipeline?" if not stated else
                f"{stated!r} is not a pipeline on this install. Which one?")
    return Gap("pipeline", question,
               [Option(name, name, _pipeline_blurb(name))
                for name in sorted(PIPELINES)],
               free_text=False)


def _protocol_gap(pipeline, stated=None):
    """None when the pipeline takes no -t at all -- there is nothing to ask."""
    choices = PIPELINES.get(pipeline) or []
    if not choices:
        return None
    question = (f"Which {pipeline} protocol?" if not stated else
                f"{stated!r} is not a {pipeline} protocol. Which one?")
    return Gap("protocol", question,
               [Option(p.name, p.name, p.blurb) for p in choices],
               free_text=False)


def _readset_gap(candidates=()):
    return Gap("readset", "Which readset file?",
               [Option(path, path) for path in candidates],
               free_text=True,
               note="Tab-separated, one row per readset. Required by every "
                    "pipeline.")


def _who(pipeline, protocol):
    """How to name the run in a question: "dnaseq somatic_fastpass", "ampliconseq".

    Both parts are optional and either can repeat the other. A pipeline with no
    `-t` has no protocol to name, and chipseq's default protocol is *called*
    chipseq -- printing the pair blindly gives "ampliconseq ampliconseq needs a
    design file", which reads like a bug in the question and makes the reader
    distrust the rest of it.
    """
    parts = [pipeline] if pipeline else []
    if protocol and protocol != pipeline:
        parts.append(protocol)
    return " ".join(parts)


def _design_gap(pipeline, protocol, candidates=()):
    who = _who(pipeline, protocol)
    return Gap("design", f"{who} needs a design file. Which one?".lstrip(),
               [Option(path, path) for path in candidates],
               free_text=True,
               # The second sentence is what makes this question answerable by
               # somebody who has no design file. Exactly one step reads it,
               # and `-s` decides whether that step runs -- so "I don't have
               # one" is a legitimate answer that means "keep -s below the
               # differential step", not a dead end. Without saying so, the
               # only visible options are inventing a design or giving up.
               note="Columns are contrasts; 1 is control, 2 is treatment, 0 or "
                    "blank excludes the sample. Only the differential step "
                    "reads it — a step range that stops before it needs none.")


def _pairs_gap(pipeline, protocol, candidates=()):
    who = _who(pipeline, protocol)
    return Gap("pairs", f"{who} needs a pairs file. Which one?".lstrip(),
               [Option(path, path) for path in candidates],
               free_text=True,
               note="Comma-separated, mapping each tumour sample to its matched "
                    "normal.")


def gap_for(slot, pipeline=None, protocol=None, question=None,
            readset_candidates=(), design_candidates=(), pairs_candidates=()):
    """The one question the agent asked for, as a Gap.

    This is the seam the ask node uses, and the whole point of it is the
    division of labour: the model names a slot, and the options come from the
    table above. A model asked to list dnaseq's protocols will eventually list
    an eighth one and sound entirely confident doing it; a model asked only
    *when* to raise the question cannot.

    `question` is the model's own wording, and IT WINS when it is given.

    That is a reversal. The wording used to be overridden for any known slot,
    on the grounds that the table's phrasing was the phrasing CI checks -- which
    made CI the reason a person got a worse question. "Which dnaseq protocol?"
    is fine in isolation and useless as the third turn of a conversation about
    tumour/normal pairs, where the model would have asked something like "you
    have matched normals, so somatic_fastpass or somatic_ensemble -- which?".
    The model can see the conversation; a constant cannot.

    WHAT THE TABLE STILL OWNS, unconditionally, is everything a model can be
    confidently wrong about: the option set, whether free text is admissible,
    and the factual note attached to a slot. A model asked to list dnaseq's
    protocols will eventually list an eighth and sound certain doing it; a
    model asked only how to PHRASE the question cannot invent a value. So the
    safety property survives the change intact -- a badly worded protocol
    question still cannot produce a protocol that does not exist.

    Returns None only when there is genuinely nothing to ask, which happens for
    exactly one case worth guarding: a protocol for a pipeline that takes none.
    """
    gap = None
    if slot == "pipeline":
        gap = _pipeline_gap()
    elif slot == "protocol":
        if not pipeline:
            # Asked about a protocol without saying whose. The pipeline is the
            # real gap, and answering it first is what the ordering in gaps()
            # exists to enforce. The model's wording is dropped with it: it was
            # written for a different question than the one being asked.
            return _pipeline_gap()
        gap = _protocol_gap(pipeline)
    elif slot == "readset":
        gap = _readset_gap(readset_candidates)
    elif slot == "design":
        gap = _design_gap(pipeline or "", protocol or "", design_candidates)
    elif slot == "pairs":
        gap = _pairs_gap(pipeline or "", protocol or "", pairs_candidates)
    elif question:
        return Gap(None, question, (), free_text=True)

    if gap is not None and question:
        gap.question = question
    return gap


def gaps(pipeline=None, protocol=None, readset=None, design=None, pairs=None,
         readset_candidates=(), design_candidates=(), pairs_candidates=()):
    """Everything a run still lacks. A COMPLETENESS CHECK, not a script.

    THIS DOES NOT DECIDE WHAT TO ASK, and the ordering below is a report order,
    not an asking order. It used to be described as "the order it should be
    asked", which is what a caller reaching for `gaps()[0]` would act on; that
    caller is gone and must not come back. The agent decides what it is stuck
    on, and resolves what it can by looking first.

    The order is still worth keeping for the one thing it is used for: naming
    what is absent in a refusal. A dependency-respecting list -- pipeline, then
    protocol, then the files that protocol turns out to require -- reads
    correctly, because mentioning a pairs file before the protocol is settled
    describes a requirement that may not exist.

    The one caller that matters is gate.build_proposal, on the way OUT: empty
    means the proposal is executable, non-empty means it is refused and handed
    back to the model with the slot names. Nothing watches this and advances on
    it -- a run becomes READY because the agent proposed it and this did not
    object, never because the count reached zero.

    The *_candidates arguments are real paths the caller found on disk. They
    become the numbered options; the free-text entry covers everything else.
    """
    found = []

    if not pipeline:
        return [_pipeline_gap()]          # nothing below is decidable yet
    if not known(pipeline):
        return [_pipeline_gap(stated=pipeline)]

    choices = PIPELINES[pipeline]
    if choices and not protocol:
        default = DEFAULTS.get(pipeline)
        if not default:
            return [_protocol_gap(pipeline)]   # design/pairs depend on this
        protocol = default

    proto = find_protocol(pipeline, protocol) if choices else None
    if choices and proto is None:
        return [_protocol_gap(pipeline, stated=protocol)]

    if not readset:
        found.append(_readset_gap(readset_candidates))

    # A protocol's demand where there is one, the pipeline's where there is no
    # protocol to carry it. Both, rather than either, because the two tables
    # describe different pipelines and never the same one: _PIPELINE_NEEDS
    # holds only pipelines whose PIPELINES entry is empty.
    needs = proto.needs if proto else _PIPELINE_NEEDS.get(pipeline)
    named = proto.name if proto else None      # nothing to name; see _who
    if needs == DESIGN and not design and pipeline not in _DESIGN_OPTIONAL:
        found.append(_design_gap(pipeline, named, design_candidates))
    if needs == PAIRS and not pairs:
        found.append(_pairs_gap(pipeline, named, pairs_candidates))

    return found


_BLURBS = {
    "ampliconseq": "16S/18S/ITS amplicon profiling",
    "chipseq": "ChIP-seq and ATAC-seq peak calling",
    "covseq": "SARS-CoV-2 consensus sequences",
    "dnaseq": "DNA variant calling, germline or somatic",
    "longread_dnaseq": "Nanopore or PacBio DNA",
    "methylseq": "bisulfite methylation",
    "nanopore_covseq": "SARS-CoV-2 from Nanopore reads",
    "rnaseq": "RNA-seq expression, variants or fusions",
    "rnaseq_denovo_assembly": "de novo transcriptome assembly",
    "rnaseq_light": "fast transcript quantification",
}


def _pipeline_blurb(name):
    return _BLURBS.get(name, "")
