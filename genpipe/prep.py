"""Recognising that an ordinary sentence is the beginning of a run.

The distinction this module draws is NOT "did they use a launch keyword". It is
whether the person is asking the assistant to *perform or prepare* an analysis,
or to *explain* something. "What's the difference between somatic_fastpass and
somatic_ensemble?" and "Run somatic_fastpass on these" both name a protocol; only
one of them should start preparing anything.

Once it is reasonably clear that a run is wanted, the conversation enters one
continuous state -- `● Preparing run...` -- and stays in it while it resolves, in
this order and no other:

    scientific goal → pipeline → protocol → required documents → explicit
    options → defaults → validation → generated run

The order matters because each step decides what the next question even is: a
protocol determines whether a design file or a pairs file is required, so asking
for a readset before the protocol is known is asking a question whose follow-up
you cannot predict. But it must not feel like a form. The next question depends
entirely on what the person already said, and anything they already settled is
never asked about again.

The failure modes this is written against, all of them observed or obvious:

    treating every technical question as a request to prepare a run
    asking a fixed sequence regardless of what was already supplied
    guessing a consequential protocol from a vague scientific request
    asking about defaults (all steps, the standard Rorqual config)
    forgetting resolved information between turns
    generating before everything required has been validated

One rule about matching, and it is load-bearing: every keyword test here runs
against THE SINGLE LINE JUST TYPED, never against accumulated conversation text.
The agent's system prompt contains the whole of genpipes.md, so a substring test
over history is true of every pipeline name at once -- the bug recorded in
DevLLM's docstring in fakecluster.py.

Standard library only, and no filesystem: the caller passes in what it found.
"""
import re

from . import slots

# ---------------------------------------------------------------------------
# Intent.
# ---------------------------------------------------------------------------

RUN = "run"
QUESTION = "question"
AMBIGUOUS = "ambiguous"

# Asking the assistant to DO the analysis. Verbs of performance, not of naming.
_DO = (
    "run", "launch", "submit", "start", "kick off", "fire off", "execute",
    "process", "prepare", "set up", "set it up", "generate", "analyse",
    "analyze", "align", "quantify", "call variants", "call peaks", "assemble",
    "let's do", "lets do", "go ahead and", "i want to run", "can you run",
    "please run", "i need to run", "do the",
)

# Asking to be told something. These win over a performance verb when they open
# the sentence, because "how do I run dnaseq?" contains "run" and is a question.
_ASK = (
    "what", "what's", "whats", "which", "why", "how", "when", "who", "where",
    "explain", "describe", "tell me", "difference between", "compare",
    "should i", "do i need", "does it", "is it", "are there", "can i",
    "help me understand", "remind me", "meaning of", "what does",
)

# Sentences about having data. On their own these are ambiguous: "I have
# tumour-normal data" may be a preamble to a run or to a question about one.
_HAVE = ("i have", "i've got", "ive got", "we have", "we've got", "there is",
         "there are", "my data", "our data", "here is", "here's", "got some")

# Imperative verbs that describe a RESULT rather than a command. These are how
# a biologist states a run: the object is the finding, not the pipeline.
_GOAL_VERBS = ("find", "compare", "identify", "detect", "measure", "quantify",
               "call", "look for", "check for", "profile", "assemble",
               "get me", "give me", "work out", "figure out")


def _named_count(text):
    """How many distinct pipeline or protocol names this line mentions.

    Two or more is the signal that the sentence is ABOUT them -- a comparison,
    a question -- rather than an instruction using one.
    """
    seen = set()
    for pipeline, protocols in slots.PIPELINES.items():
        if re.search(rf"\b{re.escape(pipeline)}\b", text):
            seen.add(pipeline)
        for proto in protocols:
            if re.search(rf"\b{re.escape(proto.name)}\b", text):
                seen.add(proto.name)
    return len(seen)


def _clean(line):
    return re.sub(r"\s+", " ", (line or "").strip().lower())


def intent(line):
    """RUN, QUESTION or AMBIGUOUS for the single line just typed.

    AMBIGUOUS is a real answer and the most important one. It means the message
    is about an analysis but does not say whether an explanation or a run is
    wanted, and the right response is one short clarification rather than a
    guess in either direction -- guessing "question" wastes their time, guessing
    "run" starts spending it.
    """
    text = _clean(line)
    if not text:
        return QUESTION

    if text.endswith("?"):
        # A question mark is the clearest signal a person can give, and it costs
        # nothing to believe it. The exception is a request phrased politely as
        # a question -- "can you run dnaseq on this?" -- which is a request.
        if not any(text.startswith(v) or f" {v} " in f" {text} "
                   for v in ("can you run", "could you run", "will you run",
                             "would you run", "can you prepare",
                             "can you launch", "can you submit")):
            return QUESTION

    # A scientific goal in the imperative -- "find inherited SNVs", "compare
    # gene expression between treatment and control" -- is a request to do the
    # analysis, described in the language of the result rather than in GenPipes
    # terminology. Nobody should have to name a pipeline they have already
    # described.
    #
    # The exception is a sentence whose objects are the pipeline names
    # themselves: "compare somatic_fastpass and somatic_ensemble" is the same
    # verb asking to be told the difference, and two named protocols in one line
    # is what tells the two apart.
    if any(text.startswith(v) for v in _GOAL_VERBS):
        if goal(line) and _named_count(text) < 2:
            return RUN

    opens_as_question = any(text.startswith(a) for a in _ASK)
    asks_to_do = any(v in text for v in _DO)

    if opens_as_question and not text.startswith(("can you run", "could you run")):
        return QUESTION
    if asks_to_do:
        return RUN
    if any(text.startswith(h) or f" {h} " in f" {text} " for h in _HAVE):
        return AMBIGUOUS
    # Names a pipeline or a scientific goal and nothing else: "rnaseq on these
    # nine samples". Nobody types that to be told what rnaseq is, but nobody
    # said to run it either.
    if goal(line) or _pipeline_named(text):
        return AMBIGUOUS
    return QUESTION


CLARIFY = "Would you like help choosing the analysis, or should I prepare the run?"


def _pipeline_named(text):
    for name in slots.PIPELINES:
        if re.search(rf"\b{re.escape(name)}\b", text):
            return name
        if re.search(rf"\b{re.escape(name.replace('_', '[- ]?'))}\b", text):
            return name
    return None


# ---------------------------------------------------------------------------
# Scientific goal -> pipeline, and sometimes protocol.
#
# The mapping is deliberately asymmetric. A pipeline is inferred readily: it is
# recoverable, visible in the gate box, and getting it wrong costs one
# correction. A PROTOCOL is inferred only where the mapping is unambiguous,
# because a protocol decides which callers run, which files are required, and
# what the numbers at the end mean -- and a wrong one produces a run that
# completes successfully and answers a different question.
#
# So: "find inherited SNVs and small indels" is germline_snv and nothing else,
# and is inferred. "paired tumour/normal variants" is one of three somatic
# protocols that differ in cost and thoroughness, and is not.
# ---------------------------------------------------------------------------

_GOALS = [
    # (pattern, pipeline, protocol or None, what was understood)
    (r"\b(inherited|germline)\b.*\b(snv|snp|variant|indel)",
     "dnaseq", "germline_snv", "germline SNVs and small indels"),
    (r"\bsnv?s?\b.*\bsmall indels?\b", "dnaseq", "germline_snv",
     "germline SNVs and small indels"),
    (r"\b(structural variants?|\bsvs?\b)\b", "dnaseq", None,
     "structural variants"),
    (r"\b(tumou?r).{0,20}\b(normal|matched)\b", "dnaseq", None,
     "paired tumour/normal variant calling"),
    (r"\bsomatic\b", "dnaseq", None, "somatic variant calling"),
    (r"\bexome\b|\bwes\b", "dnaseq", None, "exome variant calling"),
    (r"\b(differential(ly)? express|gene expression|expression between|"
     r"\bdeg\b|up.?regulat|down.?regulat)", "rnaseq", "stringtie",
     "differential gene expression"),
    (r"\b(fusion|gene[- ]fusion)\b", "rnaseq", "cancer", "gene fusions"),
    (r"\b(transcript|isoform)\b.*\b(quantif|abundance)", "rnaseq_light", None,
     "fast transcript quantification"),
    (r"\bde novo\b.*\b(transcriptome|assembl)", "rnaseq_denovo_assembly", None,
     "de novo transcriptome assembly"),
    (r"\b(peak calling|call peaks|peaks?)\b", "chipseq", None, "peak calling"),
    (r"\b(chip[- ]?seq|histone mark|h3k\w+)\b", "chipseq", "chipseq",
     "ChIP-seq peak calling"),
    (r"\batac[- ]?seq\b", "chipseq", "atacseq", "ATAC-seq"),
    (r"\b(methylat|bisulfite|cpg)\b", "methylseq", None, "methylation"),
    (r"\b(nanopore|oxford)\b", "longread_dnaseq", "nanopore", "Nanopore reads"),
    (r"\b(pacbio|revio)\b", "longread_dnaseq", "revio", "PacBio reads"),
    (r"\b(16s|18s|its|amplicon)\b", "ampliconseq", None, "amplicon profiling"),
    (r"\b(sars|covid|cov[- ]?seq)\b", "covseq", None, "SARS-CoV-2 consensus"),
]


class Goal:
    """What a scientific description was understood to mean."""

    __slots__ = ("pipeline", "protocol", "described")

    def __init__(self, pipeline, protocol, described):
        self.pipeline = pipeline
        self.protocol = protocol
        self.described = described

    def __repr__(self):
        return f"<Goal {self.pipeline} {self.protocol} {self.described!r}>"


def goal(line):
    """The pipeline (and protocol, where unambiguous) a description implies.

    None when nothing here maps strongly. That is the common case and it is
    correct: a description this table does not recognise is a description the
    person should be asked about, not one to be pattern-matched into a run.
    """
    text = _clean(line)
    if not text:
        return None
    for pattern, pipeline, protocol, described in _GOALS:
        if re.search(pattern, text):
            return Goal(pipeline, protocol, described)
    return None


# ---------------------------------------------------------------------------
# What is still unresolved, and therefore what to ask next.
# ---------------------------------------------------------------------------

# Rows nobody should ever be asked about. A run with no -s runs every step, and
# the cluster ini for the machine you are logged into is not a decision -- it is
# the answer. Asking about either is how a conversation turns into a form.
ASSUMED = ("steps", "config", "output")


class Preparation:
    """The resolved state of a run being prepared, and what is missing from it.

    Carried across turns by the caller. The single most damaging thing this
    could do is forget: being asked twice for a readset you already named is the
    exact experience that makes people stop using an assistant and go back to
    writing the command by hand.
    """

    __slots__ = ("pipeline", "protocol", "readset", "design", "pairs",
                 "described", "active")

    def __init__(self, **kw):
        for slot in self.__slots__:
            setattr(self, slot, kw.get(slot))
        self.active = bool(kw.get("active"))

    def learn(self, **facts):
        """Record what a turn settled. Never unsets: a value already resolved
        stays resolved unless a later turn gives a different one."""
        for key, value in facts.items():
            if value and key in self.__slots__:
                setattr(self, key, value)
        return self

    def as_dict(self):
        return {s: getattr(self, s) for s in self.__slots__}

    def __repr__(self):
        return f"<Preparation {self.pipeline} {self.protocol} active={self.active}>"


def missing(prep, candidates=None):
    """The next thing to resolve, as a slots.Gap, or None when nothing is.

    Exactly one gap, not a list, because the answer to one changes whether the
    next is a gap at all. It delegates every question's wording and options to
    slots.gaps(), so a question asked here and a question asked by the agent's
    own ask node are worded identically -- there is one table, and CI checks it.
    """
    candidates = candidates or {}
    found = slots.gaps(
        pipeline=prep.pipeline,
        protocol=prep.protocol,
        readset=prep.readset,
        design=prep.design,
        pairs=prep.pairs,
        readset_candidates=candidates.get("readset") or (),
        design_candidates=candidates.get("design") or (),
        pairs_candidates=candidates.get("pairs") or (),
    )
    return found[0] if found else None


def ready(prep, candidates=None):
    """Is everything required settled? Then it is time to generate, not ask."""
    return missing(prep, candidates) is None and bool(prep.pipeline)


def summary(prep):
    """One line saying what has been settled so far.

    Shown under the `Preparing run...` label so the state is visible rather than
    remembered. It is also the cheapest possible correction mechanism: seeing
    "rnaseq · stringtie" is what makes somebody say "no, variants" before a
    generation is spent on the wrong one.
    """
    parts = [p for p in (prep.pipeline, prep.protocol) if p]
    for label, value in (("readset", prep.readset), ("design", prep.design),
                         ("pairs", prep.pairs)):
        if value:
            parts.append(f"{label} {value}")
    return "  ·  ".join(parts)
