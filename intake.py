"""Reading a request before the model sees it, and asking about what is missing.

A person types "run somatic ensemble on my tumour samples" and means something
precise, but three of the things GenPipes will demand are not in that sentence.
Left alone the model does one of two unhelpful things: it invents plausible
values, or it writes a paragraph asking for all of them at once.

So the missing pieces are found here, deterministically, and asked one at a
time as a numbered panel whose options come from slots.PIPELINES rather than
from a model. That is the difference that matters: a model asked to offer
protocol choices will eventually offer one that does not exist, and it will
sound just as confident doing it.

Parsing is deliberately conservative. It only recognises things it can be sure
about -- a pipeline name, a protocol name, a filename with a recognisable role
-- and treats everything else as absent, since a wrong guess here becomes a
question that was never asked. Anything not recognised simply falls through to
the model, which is where it would have gone anyway.
"""

import os
import re

import slots

# Longest first, so rnaseq_denovo_assembly is not matched as rnaseq. Underscores
# and hyphens are both accepted because people type both.
_PIPELINE_TOKENS = sorted(slots.PIPELINES, key=len, reverse=True)

_FILE_SUFFIXES = (".tsv", ".txt", ".csv")

_ROLE_HINTS = (
    ("readset", ("readset", "readsets", "samplesheet", "sample_sheet")),
    ("design", ("design", "contrast", "contrasts")),
    ("pairs", ("pairs", "pair", "tumor_pair", "tumour_pair")),
)


def _word(token):
    """Match a token on word boundaries, treating - and _ as interchangeable."""
    pattern = re.escape(token).replace("_", "[-_ ]").replace(r"\-", "[-_ ]")
    return re.compile(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])", re.I)


def find_pipeline(text):
    for name in _PIPELINE_TOKENS:
        if _word(name).search(text or ""):
            return name
    return None


def find_protocol(text, pipeline):
    """A protocol name stated anywhere in the request.

    Longest first again: somatic_sv must not be swallowed by a shorter sibling,
    and germline_high_cov must not match as germline_snv's neighbour.
    """
    if not pipeline:
        return None
    names = sorted((p.name for p in slots.protocols(pipeline)),
                   key=len, reverse=True)
    for name in names:
        if _word(name).search(text or ""):
            return name
    return None


def find_files(text):
    """Filenames in the request, bucketed by the role their name implies.

    A path whose name says nothing about its role is left out entirely rather
    than assigned to the most likely bucket. Guessing that `samples.tsv` is a
    readset is right often enough to be dangerous and wrong often enough to
    matter.
    """
    found = {"readset": None, "design": None, "pairs": None}
    for token in re.findall(r"\S+", text or ""):
        token = token.strip("'\"`,;()[]")
        if not token.lower().endswith(_FILE_SUFFIXES):
            continue
        stem = os.path.basename(token).lower()
        for role, hints in _ROLE_HINTS:
            if found[role] is None and any(h in stem for h in hints):
                found[role] = token
                break
    return found


def read(text):
    """Everything the request states outright."""
    pipeline = find_pipeline(text)
    files = find_files(text)
    return {
        "pipeline": pipeline,
        "protocol": find_protocol(text, pipeline),
        "readset": files["readset"],
        "design": files["design"],
        "pairs": files["pairs"],
    }


def candidates(directory=".", limit=8):
    """Files in the working directory that could fill each role.

    Cheap, non-recursive and best-effort: these become numbered options in the
    panel, and the free-text row covers everything this misses. A slow or
    clever search would be the wrong trade for a list nobody is obliged to use.
    """
    buckets = {"readset": [], "design": [], "pairs": []}
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return buckets
    for name in names:
        if not name.lower().endswith(_FILE_SUFFIXES):
            continue
        stem = name.lower()
        for role, hints in _ROLE_HINTS:
            if any(h in stem for h in hints) and len(buckets[role]) < limit:
                path = os.path.join(directory, name) if directory != "." else name
                buckets[role].append(path)
                break
    return buckets


def resolve(text, directory=".", asker=None):
    """Fill in what the request left out, asking one panel per gap.

    `asker` is called as asker(gap) and returns a value or None; injecting it
    keeps this function testable without a terminal and lets the caller decide
    what a panel looks like. Returning None means the person declined, and
    declining is a legitimate answer -- the value stays unset and the model
    deals with it, which is strictly better than a menu that cannot be escaped.

    Returns (stated, cancelled). `cancelled` is True only for a hard abort.
    """
    stated = read(text)
    if asker is None:
        return stated, False

    found = candidates(directory)
    for _ in range(6):                    # each answer can reveal the next gap
        remaining = slots.gaps(
            pipeline=stated["pipeline"],
            protocol=stated["protocol"],
            readset=stated["readset"],
            design=stated["design"],
            pairs=stated["pairs"],
            readset_candidates=found["readset"],
            design_candidates=found["design"],
            pairs_candidates=found["pairs"],
        )
        if not remaining:
            break
        gap = remaining[0]
        try:
            answer = asker(gap)
        except (EOFError, KeyboardInterrupt):
            return stated, True
        if not answer:
            break                         # declined; let the model handle it
        stated[gap.slot] = answer
    return stated, False


def restate(text, stated):
    """Append what was answered to the original request.

    Appended rather than rewritten so the person's own words stay in front of
    the model. A request rebuilt from parsed fields loses the intent that was
    never in a field -- "the samples Marie sent", "same as last time" -- and
    that intent is often the part that decides whether the answer is useful.
    """
    original = read(text)
    added = [(slot, value) for slot, value in stated.items()
             if value and value != original.get(slot)]
    if not added:
        return text
    lines = "\n".join(f"- {slot}: {value}" for slot, value in added)
    return f"{text}\n\nConfirmed with the user:\n{lines}"
