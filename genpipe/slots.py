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

# Where --help marks a default, an unspecified protocol is not a gap.
DEFAULTS = {"rnaseq": "stringtie"}

# Feature inis that are chosen by what the *data* is, not by which protocol was
# picked. dnaseq.exome.ini sets experiment_type=exome and touches both germline
# sections (gatk_haplotype_caller) and somatic ones (gatk_mutect2,
# strelka2_paired_somatic), so it stacks alongside a protocol ini rather than
# instead of one. Listed here so nothing treats an unclaimed overlay as a gap in
# the table.
DATA_OVERLAYS = {
    "dnaseq.exome.ini": "capture/exome reads rather than whole-genome",
}

# Pipelines whose design file is genuinely optional: chipseq skips the
# differential-binding step when there is no valid design rather than failing,
# so demanding one would block a legitimate peak-calling-only run.
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


def _design_gap(pipeline, protocol, candidates=()):
    who = f"{pipeline} {protocol}".strip()
    return Gap("design", f"{who} needs a design file. Which one?".lstrip(),
               [Option(path, path) for path in candidates],
               free_text=True,
               note="Columns are contrasts; 1 is control, 2 is treatment, 0 or "
                    "blank excludes the sample.")


def _pairs_gap(pipeline, protocol, candidates=()):
    who = f"{pipeline} {protocol}".strip()
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

    `question` is the model's own wording. It is used as-is when there is no
    slot -- an agent must be able to ask about things this table has never
    heard of -- and ignored when there is one, because the table's wording is
    the wording CI checks.

    Returns None only when there is genuinely nothing to ask, which happens for
    exactly one case worth guarding: a protocol for a pipeline that takes none.
    """
    if slot == "pipeline":
        return _pipeline_gap()
    if slot == "protocol":
        if not pipeline:
            # Asked about a protocol without saying whose. The pipeline is the
            # real gap, and answering it first is what the ordering in gaps()
            # exists to enforce.
            return _pipeline_gap()
        return _protocol_gap(pipeline)
    if slot == "readset":
        return _readset_gap(readset_candidates)
    if slot == "design":
        return _design_gap(pipeline or "", protocol or "", design_candidates)
    if slot == "pairs":
        return _pairs_gap(pipeline or "", protocol or "", pairs_candidates)
    if question:
        return Gap(None, question, (), free_text=True)
    return None


def gaps(pipeline=None, protocol=None, readset=None, design=None, pairs=None,
         readset_candidates=(), design_candidates=(), pairs_candidates=()):
    """Everything still missing, in the order it should be asked.

    Order is deliberate: pipeline, then protocol, then the files the protocol
    turns out to require. Asking for a pairs file before knowing the protocol
    would sometimes be asking for a file the run does not use.

    The *_candidates arguments are real paths the caller found on disk. They
    become the numbered options; the free-text entry covers everything else.

    Still used on the way in to a conversation (as a brief for the model) and
    on the way out of one (as the gate's completeness check), even though the
    questions themselves are now asked by the agent through gap_for().
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

    needs = proto.needs if proto else None
    if needs == DESIGN and not design and pipeline not in _DESIGN_OPTIONAL:
        found.append(_design_gap(pipeline, proto.name, design_candidates))
    if needs == PAIRS and not pairs:
        found.append(_pairs_gap(pipeline, proto.name, pairs_candidates))

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
