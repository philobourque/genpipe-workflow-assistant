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

from . import slots

# Longest first, so rnaseq_denovo_assembly is not matched as rnaseq. Underscores
# and hyphens are both accepted because people type both.
_PIPELINE_TOKENS = sorted(slots.PIPELINES, key=len, reverse=True)

_FILE_SUFFIXES = (".tsv", ".txt", ".csv")

# The slots whose value is a path, and so can be checked against the disk.
_FILE_SLOTS = ("readset", "design", "pairs")

# Buckets candidates() fills for the /modify panel and brief() must NOT pass to
# the model. The panel offers an ini to somebody who can see the whole -c stack
# and is deciding where it goes; brief() offers candidates under the standing
# instruction "exactly one candidate for a role means use it and say so", which
# is right for a readset and dangerous for an ini. A project directory
# accumulates `*.override.ini` files belonging to other runs and GenPipes'
# own `<Pipeline>.<protocol>.<TIMESTAMP>.config.trace.ini` artefacts, and a run
# that quietly inherits one of those generates cleanly, submits cleanly, and
# runs with somebody else's walltimes.
_NOT_A_CANDIDATE = ("config",)

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
    """Filenames THE PERSON NAMED, bucketed by the role their name implies.

    A path whose name says nothing about its role is left out entirely rather
    than assigned to the most likely bucket. Guessing that `samples.tsv` is a
    readset is right often enough to be dangerous and wrong often enough to
    matter.

    Only their own words are read. brief() appends an inventory of the working
    directory to the message, and this used to scan that too -- so a run
    launched from a directory that happened to contain a design.tsv came out
    with `-d design.tsv` on the command line, naming a file from the launch
    directory that had nothing to do with the data. A file is on the command
    line because somebody said so, never because it was lying around.
    """
    found = {"readset": None, "design": None, "pairs": None}
    for token in re.findall(r"\S+", spoken(text)):
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
    """Everything the request states outright, including a project directory."""
    pipeline = find_pipeline(text)
    files = find_files(text)
    dirs = find_directories(text)
    return {
        "pipeline": pipeline,
        "protocol": find_protocol(text, pipeline),
        "readset": files["readset"],
        "design": files["design"],
        "pairs": files["pairs"],
        "project_dir": dirs[0] if dirs else None,
    }


def _resolves(name, directory):
    """Is this filename actually there? Relative names are resolved against
    `directory` -- the established project directory, if there is one. An
    absolute name is checked on its own; a relative one with no directory to
    resolve against is not assumed to be anywhere in particular."""
    try:
        if os.path.isabs(name):
            return os.path.exists(name)
        return directory is not None and os.path.exists(os.path.join(directory, name))
    except (OSError, ValueError):
        return False


# holds_candidates() lived here. It answered "does this directory contain a
# readset, design or pairs file?" and was used to decide whether a path
# somebody MENTIONED became a directory this tool would search.
#
# It was the same mistake as prep.goal(), one layer down. Swapping a regex over
# the sentence for a rule over the filesystem does not stop it being a
# classifier deciding what the user meant -- it just makes the misclassification
# harder to see. And it was wrong about real layouts: a project directory whose
# readsets sit in raw_reads/, a run assembled from files in three places, a
# readset that has not been written yet. Each of those is a valid setup this
# would have silently refused to look at.
#
# What replaced it is not a better rule. Directories somebody named are
# remembered as PROVENANCE -- "these were mentioned" -- and which of them is a
# data root is left to the agent, which can see the sentence, list the tree,
# and ask. See prep.track().


def candidates(directory=None, limit=8):
    """Files in `directory` that could fill each role, or nothing if no
    directory has been established.

    Cheap, non-recursive and best-effort: these become numbered options in the
    panel, and the free-text row covers everything this misses. A slow or
    clever search would be the wrong trade for a list nobody is obliged to use.

    `directory=None` -- no project directory known yet -- returns empty
    buckets rather than falling back to the process's current directory. The
    caller decides what counts as "established"; this function never guesses.
    """
    buckets = {"readset": [], "design": [], "pairs": [], "config": []}
    if directory is None:
        return buckets
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return buckets
    for name in names:
        path = os.path.join(directory, name) if directory != "." else name
        # An ini is recognised by its SUFFIX, not by a name hint, because
        # unlike the other three roles there is nothing to hint at: an ini is
        # whatever somebody wrote sections into, and the ones worth offering
        # here -- a private override, a hand-written overlay -- are named after
        # whatever they tune. Deliberately kept out of _ROLE_HINTS, which
        # find_files() also reads: that function parses prose, and teaching it
        # that every `.ini` mentioned in a sentence is a slot value would have
        # it filling `-c` from a message that merely explained the layering.
        if name.lower().endswith(".ini"):
            if len(buckets["config"]) < limit:
                buckets["config"].append(path)
            continue
        if not name.lower().endswith(_FILE_SUFFIXES):
            continue
        stem = name.lower()
        for role, hints in _ROLE_HINTS:
            if any(h in stem for h in hints) and len(buckets[role]) < limit:
                buckets[role].append(path)
                break
    return buckets


def spoken(text):
    """One message, with brief()'s appended context cut off the end.

    Apply this to each message SEPARATELY, never to several joined together.
    The context block has a mark where it starts and nothing where it ends, so
    in a concatenation there is no way to tell where the appended facts stop and
    the next real message begins -- cutting at the first mark would take every
    later message with it. That is not hypothetical: joining the request to the
    answers that followed it, then cutting once, silently dropped the readset
    chosen in a panel, and the gate withheld /approve for a slot that had in
    fact been filled.
    """
    return _untagged(str(text or "").split(CONTEXT_MARK)[0])


def _untagged(text):
    """Tag markers turned into spaces, so a path at the end of one is still a path.

    Answers to ask() come back to the model wrapped as
    `<observation>The user answered: /data/readset.tsv</observation>`, and
    splitting that on whitespace yields `readset.tsv</observation>` -- which
    ends in neither .tsv nor anything else recognisable, so the readset was
    dropped and the same question asked again until the ask budget ran out.
    """
    return re.sub(r"</?[A-Za-z_][A-Za-z0-9_]*>", " ", text)


def find_directories(text):
    """Directories the person named, in the order they said them.

    A request that names where the data is -- "the fastqs are in
    /lustre09/.../alain_rnaseq" -- has told us where to look for the readset
    too, and asking "which readset file?" while holding that path is asking a
    question whose answer is one listdir away.

    Only their own words, for the same reason find_files reads only theirs, and
    only paths that are really directories: a token has to survive isdir() to
    get here, so a sentence that mentions a word with a slash in it does not
    send us reading somebody's home.
    """
    out = []
    for token in re.findall(r"\S+", spoken(text)):
        token = token.strip("'\"`,;()[]").rstrip(":")
        if "/" not in token and not token.startswith("~"):
            continue
        # Sentence punctuation is stripped as a SECOND attempt rather than up
        # front, so a directory whose name really does end in one of these is
        # still found first. "is it ~/proj/a or ~/proj/b?" used to yield only
        # the first path, because the second arrived as `b?` and failed
        # isdir() -- which mattered the moment a question naming two candidate
        # directories stopped being read as a choice of one of them.
        for candidate in (token, token.rstrip("?!.,;")):
            path = os.path.expanduser(candidate)
            try:
                if os.path.isdir(path):
                    if path not in out:
                        out.append(path)
                    break
            except (OSError, ValueError):
                break
    return out


def near_miss(name, directory=None):
    """The file they probably meant, when the one they named is not there.

    `myReadset.ts` for `myReadset.tsv` -- one character, and the whole run was
    built without a readset because of it. The answer was echoed back as
    accepted and then silently dropped, which is the worst way to fail: there
    was no reason to doubt it had landed.

    Deliberately narrow. Only a name that differs by its extension or its case
    counts, and only when exactly one file in the directory matches -- a fuzzy
    match with several answers is a question, not a correction.
    """
    if not name:
        return None
    where = os.path.dirname(name) or directory
    if not where:
        return None
    stem = os.path.splitext(os.path.basename(name))[0].lower()
    try:
        names = os.listdir(where)
    except OSError:
        return None
    hits = [n for n in names
            if os.path.splitext(n)[0].lower() == stem
            and n.lower().endswith(_FILE_SUFFIXES)]
    return os.path.join(where, hits[0]) if len(hits) == 1 else None


# Where the person's own words stop and the appended facts begin. One line, in
# the text itself, because everything downstream has only the string: the
# renderer cuts here so their turn is shown as they typed it rather than with an
# inventory of their directory attributed to them, and fakecluster's stand-in
# model cuts here so "hi" is not read as a work order just because a readset file
# happens to be lying around. Change this and change both.
CONTEXT_MARK = "--- context for you, not typed by the user ---"


def brief(text, directory=None):
    """The request, plus what can be established about it without asking.

    Appended rather than rewritten, so the person's own words stay in front of
    the model. A request rebuilt from parsed fields loses the intent that was
    never in a field -- "the samples Marie sent", "same as last time" -- and
    that intent is often the part that decides whether the answer is useful.

    `directory` is the PROJECT directory already established for this
    conversation -- from an earlier turn, or from an explicit /project -- not
    the process's current working directory. Pass None when nothing has been
    established yet; this function never falls back to os.getcwd(), and
    discovers nothing when it has nowhere to look. A directory named in `text`
    itself always wins over `directory`, because the person is telling you
    right now where today's data is.

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
    # `directory` may be one path or several: prep.Preparation now remembers
    # every directory a conversation has named, because deciding which of them
    # is "the" project directory is a reading, not a lookup. The first is used
    # where a single root is needed to resolve a relative filename -- it is the
    # one somebody led with.
    carried = ([directory] if isinstance(directory, str)
               else list(directory or ()))
    here = stated.get("project_dir")
    established = here or (carried[0] if carried else None)

    known, missing, corrected = [], [], []
    for slot, value in stated.items():
        if not value:
            continue
        if slot not in _FILE_SLOTS or _resolves(value, established):
            known.append((slot, value))
            continue
        # Named but not there. Before reporting it as missing, look for the
        # file they meant -- see near_miss.
        better = near_miss(value, established)
        if better:
            corrected.append((slot, value, better))
            known.append((slot, better))
        else:
            missing.append((slot, value))

    # Directories they pointed at. Read before anything is asked, because the
    # question this answers -- "which readset file?" -- is one nobody should be
    # made to answer while the agent is holding the folder it is sitting in.
    #
    # This is NOT the filesystem search the system prompt forbids, and the
    # difference is worth stating: one non-recursive listing of a directory
    # named in the request, versus walking a tree nobody pointed at. The ban is
    # on wandering, and following a path somebody handed over is the opposite.
    named = {}
    for where in find_directories(text):
        hits = {role: paths for role, paths in candidates(where).items()
                if paths and role not in _NOT_A_CANDIDATE}
        if hits:
            named[where] = hits

    # An established project directory not already named in THIS message --
    # carried over from an earlier turn, or set by /project -- gets the same
    # one-listing treatment, never the process's cwd.
    seen = []
    for where in ([here] if here else []) + carried:
        if not where or where in named:
            continue
        hits = {role: paths for role, paths in candidates(where).items()
                if paths and role not in _NOT_A_CANDIDATE}
        if hits:
            named[where] = hits

    if not known and not seen and not missing and not named:
        return text

    lines = [text, "", CONTEXT_MARK]
    if known:
        # PARSED, NOT CHOSEN. This used to read "Already settled by the request
        # above -- do not ask again", which is the same overreach that was
        # removed from prep: a filename in "should I use readset_a.tsv or
        # readset_b.tsv?" parses perfectly and is not an answer. What the
        # sentence MEANT is the model's to read; this only says what was found
        # in it.
        lines.append("Names found in the request above, already resolved "
                     "against the filesystem:")
        lines += [f"- {slot}: {value}" for slot, value in known]
        lines.append("")
    if corrected:
        lines.append("Read as the nearest real file, because what was named is "
                     "not on disk and exactly one file differs only by extension. "
                     "Use the corrected path, and say that you did:")
        lines += [f"- {slot}: {was} -> {now}" for slot, was, now in corrected]
        lines.append("")
    if named:
        lines.append("In the directories that have been named. Candidates "
                     "only -- a file called readset.txt is not thereby the "
                     "right readset, and one of these directories may be an "
                     "output path or one that was ruled out. Exactly one "
                     "plausible candidate for a role means use it and say so; "
                     "several means ask which:")
        for where, hits in named.items():
            for role, paths in hits.items():
                lines.append(f"- possible {role} in {where}: {', '.join(paths)}")
        lines.append("")
    if missing:
        lines.append("Named in the request but NOT on disk here. Do NOT pass these "
                     "to genpipes -- the run will fail on the argument. Ask where "
                     "the file is, or offer a candidate below:")
        lines += [f"- {slot}: {value} (not found)" for slot, value in missing]
        lines.append("")
    if seen:
        lines.append(f"Files in the project directory ({os.path.abspath(established)}) "
                     f"that could fill a role. These are candidates, not "
                     f"choices -- confirm before using one, and never prefer one "
                     f"over a file in a directory the request named:")
        for role, paths in seen:
            lines.append(f"- possible {role}: {', '.join(paths)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def context_for(text, directory=None):
    """Everything the ask node needs to turn a slot name into a real panel.

    Returns (stated, candidates). Kept separate from brief() because the two
    happen at different moments -- brief once, before the agent runs; this one
    every time the agent asks something. `directory` is the established
    project directory, or None -- never the process's cwd.
    """
    return read(text), candidates(directory)
