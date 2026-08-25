"""The shape of an answer to /diagnose, and the parser that reads it back.

/diagnose used to end by printing whatever prose the model produced. The prose
was usually right and always shaped differently: sometimes "Most likely cause"
came first and sometimes "Manner (from sacct)" did, evidence was a bulleted list
one time and a paragraph the next, and the fix arrived as a fenced ini block
that somebody then had to create by hand, in the right directory, with the
section name spelled exactly right. Two runs of the same command on the same
failure produced two documents.

That is not a prompt problem to be solved with firmer wording. It is a missing
contract. So there is one here: SHAPE is the instruction the model is given, and
parse() reads what comes back. They live in the same file so that changing what
is asked for and changing what is understood is one edit rather than two, which
is the failure this module exists to prevent a second version of.

WHY THE SECTIONS ARE THESE SECTIONS
-----------------------------------
Each one heads off a specific way the old free-form answer went wrong.

  manner       from sacct, and separable from cause. genpipes.md: "State manner
               and cause as separate claims." TIMEOUT is a manner; "needed more
               time" is a cause, and they are not the same claim -- a job killed
               at its walltime while hunting for markers it could never find was
               also a TIMEOUT.
  cause        one claim, from the logs.
  evidence     the lines that support it, each traceable to a file. Rendered
               with its paths so somebody can go and read them, which was the
               thing the prose version made hardest.
  fix          the ini section and key, named.
  override     the fix as a literal ini fragment. This is the section that
               changed the command's shape: it is parsed, not just printed, and
               handed to /modify's resources row, which writes the file. The old
               answer's fenced code block was correct advice and four manual
               steps, every one of them a place to be silently wrong.
  relaunch     which steps to resubmit. See RELAUNCH_RULE.
  confidence   because genpipes.md requires uncertainty to survive to the
               surface: "If a step concluded 'likely', say 'likely'."

Nothing here is required. An answer that fills no section at all parses as prose
and is rendered as prose -- see parse(). A contract the model fails to meet must
degrade to the old behaviour, never to an empty screen.

Standard library only, like slots.py, gate.py, modify.py and mirror.py.
"""
import re

# The rule that used to be missing, and the reason it is stated as an
# instruction rather than left to judgement.
#
# A person reading "gatk_sam_to_fastq timed out" reasonably concludes they
# should rerun gatk_sam_to_fastq. That is almost always wrong, and wrong in the
# expensive direction: everything downstream of it was CANCELLED, never ran, and
# produced no output, so a narrowed range reruns the one step that failed and
# then stops -- leaving 43 jobs' worth of work undone and a run that looks
# finished. GenPipes' own up-to-date check already skips steps whose outputs
# exist, so resubmitting the FULL original range costs nothing for the work that
# is genuinely done and is the only range that finishes the run.
RELAUNCH_RULE = (
    "For RELAUNCH, name the FULL step range the run was originally submitted "
    "with, not a narrowed one starting at the failure. GenPipes checks whether "
    "each step's output is already up to date and skips it, so resubmitting the "
    "whole range re-runs only what never produced output and costs nothing for "
    "what did. A narrowed range is how a run silently ends up half done: the "
    "jobs downstream of the failure were CANCELLED, never ran, and have no "
    "output to skip against."
)

# What the model is told to produce. Given verbatim, headings included, because
# a heading it invents is a heading parse() will not find.
SHAPE = """Answer in a <solution> block, using exactly these headings, each on \
its own line, in this order. Omit a heading entirely if you have nothing true to \
put under it -- never pad one.

MANNER: how it died, in one line, from sacct alone. The state and the numbers.
CAUSE: why it died, in one short paragraph, from the logs. A different claim \
from MANNER; do not restate it.
EVIDENCE:
- one observation per line, each naming the file it came from. At most four, \
and only the ones the CAUSE actually rests on -- this is the support for a \
claim, not a restatement of everything you were shown.
FIX: what to change, naming the config section and key.
OVERRIDE:
[section_name]
key = value
RELAUNCH: the step range to resubmit, as a -s range.
UNCERTAIN:
- one line per thing this run does NOT establish, and nothing else. Whether \
the value you propose is enough belongs here. So does any competing \
explanation the evidence does not rule out.

Rules for the content:
- OVERRIDE is a literal ini fragment and nothing else -- no prose, no fences. \
Include it ONLY when the fix really is a config change; leave it out otherwise.
- Never propose a resource value you cannot compute from something observed \
above. If the logs do not support a number, say so under FIX and omit OVERRIDE.
- "The logs do not show why" is a correct CAUSE. Do not invent a plausible one.
- A value that is merely LARGER than one that failed is not thereby a value \
that will succeed. If the evidence shows a limit was too small but does not \
show what would be enough, say both: what is now known to be insufficient, and \
what this run does not establish. Do not describe any value as sufficient, \
safe or adequate unless something you observed shows it is.
- PREFER A VALUE THE PIPELINE ITSELF ALREADY CARRIES over one you choose. If \
an earlier ini in the -c stack sets this key and a later one lowered it, \
proposing the earlier value is sourced rather than invented -- say where it \
came from. It is still not proven sufficient for this input, and that belongs \
under UNCERTAIN.
- WRITE WHAT THE EVIDENCE SHOWS, NOT WHAT IT RULES OUT. "This is not a hang \
and not an error in the tool" is a claim about everything that did not happen, \
and no log establishes it. "The log's last entry is a progress line 15s before \
the wall-clock limit, and no traceback or error appears after it" is the same \
observation stated as one, and it is checkable.
- A RESOURCE FIGURE NEAR ITS REQUEST IS AN OBSERVATION, NOT A CAUSE. Peak \
memory at 99% of the request is worth reporting and does not by itself \
establish that memory pressure made the job slow. If you raise it, put it \
under EVIDENCE and put the causal question under UNCERTAIN.
- MEASUREMENTS FROM DIFFERENT SOURCES MEASURE DIFFERENT THINGS. sacct times \
the ALLOCATION; a job's epilogue times the job SCRIPT, which starts after the \
allocation and ends before it, so the epilogue figure is nested inside sacct's \
and is shorter by seconds. Two windows, not two readings of one. Quote the \
scheduler's figure for what the scheduler enforced; if you quote the job's \
own, name what each one measured. Never merge them into one number, and never \
describe them as the same window.
"""

# CONFIDENCE is still PARSED and no longer asked for -- see the note above
# display.diagnosis. Keeping it in the table means a model that emits the
# old heading out of habit has its text captured under a key nothing
# renders, rather than spilled into whichever section came before it.
_HEADINGS = ("MANNER", "CAUSE", "EVIDENCE", "FIX", "OVERRIDE", "RELAUNCH",
             "UNCERTAIN", "CONFIDENCE")

# A heading at the start of a line, with or without the model's habitual
# markdown bolding around it. `**FIX:**` and `FIX:` are the same heading, and
# refusing the first would fail the contract over punctuation.
_HEAD = re.compile(
    r"^\s*[*_#\s]*(" + "|".join(_HEADINGS) + r")\s*[*_]*\s*:?\s*[*_]*\s*",
    re.IGNORECASE)

_SECTION = re.compile(r"^\s*\[([A-Za-z_][A-Za-z0-9_.]*)\]\s*$")
_SETTING = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$")

_CONFIDENCE = ("certain", "likely", "unclear")

# Words that mean the model is hedging. Read as `unclear`, never as `certain`.
# `uncertain` is the one that mattered: it CONTAINS "certain", so a substring
# scan -- which is what this used to be -- turned the most hedged answer the
# contract allows into the most confident one it allows, rendered in white. That
# inverts genpipes.md's own rule that uncertainty must survive to the surface.
_HEDGES = frozenset(("uncertain", "unsure", "unknown", "inconclusive"))

# A negation shortly before one of the three words. "not certain" is a hedge,
# and reading the word without the "not" in front of it is the same failure as
# reading "certain" out of "uncertain". The window is three words because the
# negation is not always adjacent: "far from certain", "not at all certain".
_NEGATIONS = frozenset(("not", "never", "no", "hardly", "isn", "aren", "wasn",
                        "cannot", "can", "less", "far", "without"))
_NEGATION_REACH = 3

_WORD = re.compile(r"[a-z]+")


# How many distinct headings make a response a DIAGNOSIS rather than prose that
# happens to open with one of these words. Three, because two is reachable by
# accident -- a paragraph beginning "CAUSE: ..." and later "FIX: ..." is a
# person writing in shorthand -- and requiring all seven would refuse the
# contract's own permission to omit a heading with nothing true under it.
MIN_HEADINGS = 3

# A fenced block. Biomni treats one as an execution payload when no <execute>
# tag is present (a1.py:1341), so a response containing one is a response that
# may be asking to run something. complete() refuses those outright rather than
# deciding which of the two it is.
_FENCE = re.compile(r"^\s*```", re.MULTILINE)

_ANY_TAG = re.compile(r"</?\s*(execute|solution|think)\b", re.IGNORECASE)


def complete(text):
    """Is `text` a whole answer in SHAPE, with nothing else in it?

    A STRUCTURAL question, and only a structural one. Nothing here reads what
    the sections SAY -- a diagnosis that is wrong, hedged, self-contradictory or
    about the wrong step passes exactly as readily as a correct one, because
    judging that is not this function's business and never becomes it.

    What it exists for: a measured /diagnose turn spent 66 seconds producing a
    complete, correctly-shaped answer with no <solution> around it, which biomni
    discarded and asked for again. The second attempt returned the same text
    with the tag on. The whole cost was a missing wrapper.

    Every condition below is there to make "already a complete answer"
    unmistakable, so that the caller is restoring a wrapper rather than
    choosing one:

      no tag anywhere            a response carrying <execute> has asked for an
                                 action, and one carrying <solution> needs
                                 nothing. Either way the model already said
                                 which it meant.
      no fenced block            biomni reads a bare fence as an execution
                                 payload. A response containing one is
                                 ambiguous between answer and action, and
                                 ambiguity is the case this refuses.
      it OPENS with a heading    not "contains". Prose with headings somewhere
                                 inside it would need somebody to decide where
                                 the answer starts, and that decision is
                                 exactly what must not be made here.
      MIN_HEADINGS distinct      enough structure that the shape was intended.
      in the order SHAPE gives   the contract asks for that order; text that
                                 wanders through the headings, or repeats one,
                                 is not a document following it.

    True means: wrap the WHOLE text, unchanged. It never means: extract part of
    it.
    """
    if not text or not isinstance(text, str):
        return False
    if _ANY_TAG.search(text) or _FENCE.search(text):
        return False

    body = text.strip()
    if not body:
        return False

    order = []
    first = True
    for line in body.splitlines():
        if first:
            if not line.strip():
                continue
            if not _HEAD.match(line):
                return False
            first = False
        found = _HEAD.match(line)
        if found:
            order.append(_HEADINGS.index(found.group(1).upper()))

    if len(order) < MIN_HEADINGS:
        return False
    # Strictly increasing: each heading appears once, and in the order SHAPE
    # states. A repeat is two documents or one confused one; either way it is
    # not a thing to wrap silently.
    return all(b > a for a, b in zip(order, order[1:]))


def strip_tags(text):
    """The body of a <solution> block, or the whole text if there is none."""
    m = re.search(r"<solution>(.*?)</solution>", text or "", re.DOTALL)
    return (m.group(1) if m else (text or "")).strip()


def parse(text):
    """Read a model's answer into its sections.

    Always returns a dict with every key present, so callers never guard. The
    two that decide how it is rendered:

        `prose`  the whole answer, unchanged. Always set.
        `shaped` whether any heading was found at all.

    A `shaped` of False is not an error and must not be treated as one. It means
    the model answered in its own words -- which happens, and whose content is
    usually fine -- and the caller is expected to print `prose`. Failing to an
    empty screen because a heading was missing would make this module a
    liability on exactly the command people reach for when something is already
    wrong.
    """
    body = strip_tags(text)
    out = {"manner": "", "cause": "", "evidence": [], "fix": "",
           "override": {}, "relaunch": "", "uncertain": [],
           "confidence": "",
           "prose": body, "shaped": False}

    buckets, current = {}, None
    for line in body.splitlines():
        m = _HEAD.match(line)
        if m:
            current = m.group(1).lower()
            buckets.setdefault(current, [])
            rest = line[m.end():].strip()
            if rest:
                buckets[current].append(rest)
            out["shaped"] = True
            continue
        if current:
            buckets[current].append(line)

    if not out["shaped"]:
        return out

    out["manner"] = _join(buckets.get("manner"))
    out["cause"] = _join(buckets.get("cause"))
    out["fix"] = _join(buckets.get("fix"))
    out["evidence"] = _bullets(buckets.get("evidence"))
    out["override"] = _ini(buckets.get("override"))
    out["relaunch"] = _join(buckets.get("relaunch"))[:80]
    # WHAT THIS RUN DOES NOT ESTABLISH, as a list, beside the claims it is
    # about. Bulleted like EVIDENCE because it is the same kind of thing --
    # several separate statements, one per line -- and because a paragraph of
    # caveats is read as hedging, while three named unknowns are read as three
    # named unknowns.
    out["uncertain"] = _bullets(buckets.get("uncertain"))

    # PARSED, NOT RENDERED, AND NO LONGER ASKED FOR. See display.diagnosis.
    out["confidence"] = confidence(_join(buckets.get("confidence")))
    return out


def confidence(said):
    """Which of certain / likely / unclear the model claimed, or "".

    Whole words, in the order they were written, never substrings. Two rules,
    and both only ever move the answer toward less confidence:

        a hedge word          reads as `unclear`
        a negated word        is not a claim of that word -- keep looking.
                              Negation counts within the three words before it
                              and does not cross a clause boundary, so "far
                              from certain" is caught and "not certain, but
                              likely" still reads as `likely`.

    So `uncertain`, `not certain` and `far from certain` all come back
    `unclear`, and nothing here can turn a qualified answer into a confident
    one. An unrecognised word returns "", which renders as no label at all --
    the contract asks for one of three and inventing a fourth is not parsing.
    """
    hedged = False
    # Clause by clause, so a negation cannot reach across a comma: "not certain,
    # but likely" is a claim of `likely`, and letting the "not" carry into the
    # second clause would report it as `unclear` -- still safe, but less true
    # than what the model actually said.
    for clause in re.split(r"[,;.:]|--", (said or "").lower()):
        words = _WORD.findall(clause)
        for i, word in enumerate(words):
            if word in _HEDGES:
                hedged = True
                continue
            if word not in _CONFIDENCE:
                continue
            if _NEGATIONS & set(words[max(0, i - _NEGATION_REACH):i]):
                hedged = True
                continue
            return word
    return "unclear" if hedged else ""


def _join(lines):
    return " ".join(" ".join(lines or ()).split()).strip()


def _bullets(lines):
    """Evidence as a list. Leading bullets and numbering are stripped, and a
    wrapped line is folded back onto the item above it -- a model that wraps at
    eighty columns is not starting a new observation."""
    out = []
    for raw in lines or ():
        line = raw.rstrip()
        if not line.strip():
            continue
        bullet = re.match(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)$", line)
        if bullet:
            out.append(bullet.group(1).strip())
        elif out and line.startswith((" ", "\t")):
            out[-1] = f"{out[-1]} {line.strip()}"
        else:
            out.append(line.strip())
    return [item for item in out if item]


def _ini(lines):
    """The OVERRIDE block as {section: {key: value}} -- the shape override.py
    merges and writes.

    Fences and stray prose are dropped rather than fought over: what is wanted
    is the settings, and a line that is neither a section header nor a
    `key = value` is not one of them.
    """
    out, section = {}, None
    for raw in lines or ():
        line = raw.strip()
        if not line or line.startswith(("```", "#", ";")):
            continue
        head = _SECTION.match(line)
        if head:
            section = head.group(1)
            out.setdefault(section, {})
            continue
        setting = _SETTING.match(line)
        if setting and section:
            out[section][setting.group(1)] = setting.group(2)
    return {s: keys for s, keys in out.items() if keys}
