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
    "what did. A narrowed range is how a run silently ends up half done: every "
    "step downstream of the failure was CANCELLED, never ran, and has no output "
    "to skip against."
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
- one observation per line, each naming the file it came from
FIX: what to change, naming the config section and key.
OVERRIDE:
[section_name]
key = value
RELAUNCH: the step range to resubmit, as a -s range.
CONFIDENCE: certain, likely, or unclear.

Rules for the content:
- OVERRIDE is a literal ini fragment and nothing else -- no prose, no fences. \
Include it ONLY when the fix really is a config change; leave it out otherwise.
- Never propose a resource value you cannot compute from something observed \
above. If the logs do not support a number, say so under FIX and omit OVERRIDE.
- "The logs do not show why" is a correct CAUSE. Do not invent a plausible one.
"""

_HEADINGS = ("MANNER", "CAUSE", "EVIDENCE", "FIX", "OVERRIDE", "RELAUNCH",
             "CONFIDENCE")

# A heading at the start of a line, with or without the model's habitual
# markdown bolding around it. `**FIX:**` and `FIX:` are the same heading, and
# refusing the first would fail the contract over punctuation.
_HEAD = re.compile(
    r"^\s*[*_#\s]*(" + "|".join(_HEADINGS) + r")\s*[*_]*\s*:?\s*[*_]*\s*",
    re.IGNORECASE)

_SECTION = re.compile(r"^\s*\[([A-Za-z_][A-Za-z0-9_.]*)\]\s*$")
_SETTING = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$")

_CONFIDENCE = ("certain", "likely", "unclear")


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
           "override": {}, "relaunch": "", "confidence": "",
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

    said = _join(buckets.get("confidence")).lower()
    out["confidence"] = next((c for c in _CONFIDENCE if c in said), "")
    return out


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
