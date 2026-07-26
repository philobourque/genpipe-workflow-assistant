"""Reading a request before the model sees it.

A person types "run somatic ensemble on my tumour samples" and means something
precise, but three of the things GenPipes will demand are not in that sentence.
Two of them are sitting in the directory they typed it from.

This module used to interrogate: it found every gap up front and asked about
each in turn, before the model had seen a word. That got the right values and
made the tool feel like a form -- a fixed questionnaire that ran whether or not
the answers were in reach, and that could only ever ask about slots hardcoded
here.

Now it briefs instead. It reads what the request already states, looks at what
is on disk, and hands both to the agent as facts. The agent asks -- when it is
actually stuck, about whatever it is actually stuck on -- through ask() and the
choice panel, whose options still come from slots.PIPELINES rather than from a
model. That division survives the change and is the part that matters: a model
asked to offer protocol choices will eventually offer one that does not exist,
and it will sound just as confident doing it.

Parsing is deliberately conservative. It only recognises things it can be sure
about -- a pipeline name, a protocol name, a filename with a recognisable role
-- and treats everything else as absent. A wrong guess here is worse than a
silence: silence gets asked about, a confident wrong guess does not.
"""

import os
import re

import slots

# Longest first, so rnaseq_denovo_assembly is not matched as rnaseq. Underscores
# and hyphens are both accepted because people type both.
_PIPELINE_TOKENS = sorted(slots.PIPELINES, key=len, reverse=True)

_FILE_SUFFIXES = (".tsv", ".txt", ".csv")

# The slots whose value is a path, and so can be checked against the disk.
_FILE_SLOTS = ("readset", "design", "pairs")

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


def _resolves(name, directory):
    """Is this filename actually there? Relative names are resolved against the
    directory the request was typed in, which is the one genpipes will run in."""
    try:
        return os.path.exists(name if os.path.isabs(name)
                              else os.path.join(directory, name))
    except (OSError, ValueError):
        return False


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


# Where the person's own words stop and the appended facts begin. One line, in
# the text itself, because everything downstream has only the string: the
# renderer cuts here so their turn is shown as they typed it rather than with an
# inventory of their directory attributed to them, and fakecluster's stand-in
# model cuts here so "hi" is not read as a work order just because a readset file
# happens to be lying around. Change this and change both.
CONTEXT_MARK = "--- context for you, not typed by the user ---"


def brief(text, directory="."):
    """The request, plus what can be established about it without asking.

    Appended rather than rewritten, so the person's own words stay in front of
    the model. A request rebuilt from parsed fields loses the intent that was
    never in a field -- "the samples Marie sent", "same as last time" -- and
    that intent is often the part that decides whether the answer is useful.

    Two kinds of fact go in, and the distinction between them is stated in the
    text rather than left for the model to infer:

      what the request states   parsed from the sentence, so it is settled and
                                must not be asked about again -- unless it names
                                a file that is not there, which is the third kind
                                below.
      what is in the directory  files that merely look like they could fill a
                                role. Candidates, not answers -- a file named
                                readset.txt is not thereby the right readset,
                                and confirming that is a question worth asking.
      what was named but is     a filename in the sentence that is not on disk.
      not there                 Said plainly and separately, because the failure
                                it prevents is specific and was observed: the
                                model took the name on trust, `genpipes` answered
                                "can't open 'readset.rnaseq.txt'", and a wasted
                                generation later it was asking where the file
                                was. It can ask that first.

    Sent only on the first message of a conversation. After that it is in the
    history, and repeating it every turn would be a standing invitation to
    re-read stale filenames.
    """
    stated = read(text)
    known, missing = [], []
    for slot, value in stated.items():
        if not value:
            continue
        if slot in _FILE_SLOTS and not _resolves(value, directory):
            missing.append((slot, value))
        else:
            known.append((slot, value))
    found = candidates(directory)
    seen = [(role, paths) for role, paths in found.items() if paths]
    if not known and not seen and not missing:
        return text

    lines = [text, "", CONTEXT_MARK]
    if known:
        lines.append("Already settled by the request above -- do not ask again:")
        lines += [f"- {slot}: {value}" for slot, value in known]
        lines.append("")
    if missing:
        lines.append("Named in the request but NOT on disk here. Do NOT pass these "
                     "to genpipes -- the run will fail on the argument. Ask where "
                     "the file is, or offer a candidate below:")
        lines += [f"- {slot}: {value} (not found)" for slot, value in missing]
        lines.append("")
    if seen:
        lines.append(f"Files in the working directory ({os.path.abspath(directory)}) "
                     f"that could fill a role. These are candidates, not "
                     f"choices -- confirm before using one:")
        for role, paths in seen:
            lines.append(f"- possible {role}: {', '.join(paths)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def context_for(text, directory="."):
    """Everything the ask node needs to turn a slot name into a real panel.

    Returns (stated, candidates). Kept separate from brief() because the two
    happen at different moments -- brief once, before the agent runs; this one
    every time the agent asks something.
    """
    return read(text), candidates(directory)
