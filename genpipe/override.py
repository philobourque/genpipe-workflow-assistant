"""Private override inis -- layer 6 of the `-c` stack, and the only one we write.

genpipes.md, section 5, ends its description of the config stack with "**your
own overrides** -- last word", and then: "A private override ini is just a file
with the sections you want to change, appended last. That is how to tune a
resource. Never edit anything under `$GENPIPES_INIS`."

Until now nothing in this tool wrote one. The advice was correct, complete, and
somebody else's job: /diagnose would conclude "raise cluster_walltime for
[gatk_sam_to_fastq]", print a four-line ini, and leave a person to open an
editor, get the section name exactly right, remember which end of `-c` wins, and
re-type a command. Every one of those is a place to be silently wrong -- a
misspelled section name does not fail, it is ignored, and the run dies at the
same walltime a second time with no indication why.

WHO WRITES WHAT
---------------
This module writes the FILE. The model writes the COMMAND. The split is
modify.py's rule about not rebuilding commands, applied to the one case where
part of the change is not a command at all:

    the ini      deterministic, testable, and written here. `[section]`,
                 `key = value`. There is no judgement in it once the settings
                 are chosen, and putting judgement-free text through a model is
                 how a section name comes back subtly renamed.
    the -c line  goes through the model as feedback, like every other change,
                 because it IS a command edit and the model's view of the
                 conversation has to match what is queued.

WHAT IS NOT DECIDED HERE
------------------------
The VALUES. genpipes.md is explicit that a resource number must be computed from
something observed and traced to a config section -- "Never propose a resource
value that cannot be computed from what was observed", and its worked example is
a TIMEOUT whose obvious walltime fix was the wrong one. So nothing here defaults
a walltime, scales a memory figure, or suggests a number. It validates the shape
of what somebody typed and writes it down. The person, or a /diagnose that read
the logs, supplies the meaning.

Standard library only, like slots.py, gate.py, modify.py and mirror.py.
"""
import configparser
import datetime
import os
import re

from . import modify
from .modify import Verdict

# The keys worth offering, in the order a resource problem is usually met. Each
# is a real GenPipes cluster key -- the names come from the ini stack itself,
# not from anything invented here.
#
# `ram` is deliberately separate from `cluster_mem` rather than written
# alongside it. The idiom in the wild is `ram = %(cluster_mem)s`, and it is
# offered as this row's default, but it is offered rather than applied: handing
# a JVM a heap the exact size of its cgroup allocation is a way to turn a
# walltime problem into an OOM, and genpipes.md forbids proposing a resource
# value nobody computed.
SETTINGS = (
    ("cluster_walltime", "walltime", "How long? (HH:MM:SS)", "35:00:00"),
    ("cluster_mem", "memory", "How much memory? (e.g. 96G)", "96G"),
    ("ram", "java heap", "Heap for the tool inside the job",
     "%(cluster_mem)s"),
    ("cluster_cpu", "cpus", "How many cpus? (a number, or a %(MACRO)s)",
     "%(PINT_CPU)s"),
    ("other_options", "tool options", "Extra options passed to the tool",
     "--VALIDATION_STRINGENCY LENIENT"),
)

_LABEL = {key: label for key, label, _, _ in SETTINGS}

# HH:MM:SS, or D-HH:MM:SS, which is what Slurm and every GenPipes ini use.
_WALLTIME = re.compile(r"^\s*(?:\d+-)?\d{1,3}:[0-5]\d(?::[0-5]\d)?\s*$")

# `96G`, `4000M`, `96GB`, or a bare number of megabytes. Also a `%(MACRO)s`,
# because the cluster inis define their own and copying one is often the right
# answer -- `%(PINT_CPU)s` is not a value this module could have guessed.
_MEM = re.compile(r"^\s*(?:\d+(?:\.\d+)?\s*[KMGT]?B?|%\([A-Za-z_]\w*\)s)\s*$",
                  re.IGNORECASE)
_CPU = re.compile(r"^\s*(?:\d+|%\([A-Za-z_]\w*\)s)\s*$")

# A step name, which is also the section name. GenPipes step names are the
# lowercase underscored ones `--help` prints.
_SECTION = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


def path_for(name, directory, proposal=None):
    """Where this run's override ini lives.

    Named after the run and kept beside it rather than in a shared location, so
    that two runs tuned differently cannot quietly share one file -- and so that
    somebody who finds the file six weeks later can tell what it was for from
    its name alone.

    `proposal` wins when it has one, and that is READ off the `-c` stack rather
    than derived -- the same rule mirror.py follows. The name is how a file gets
    CREATED; the command is where it is afterwards, and those stop agreeing the
    moment the run is renamed. Deriving from the name alone meant that after a
    rename path_for() named a file that did not exist: the mirror's resources
    line went blank, the gate stopped showing tuning that was still very much in
    force, and the next /modify wrote a SECOND ini beside the live one.

    Renaming the FILE instead would have been worse. Its path is written into a
    command the model produced, so moving it means either editing that command
    locally -- the one thing modify.py exists to never do -- or paying a
    regeneration for a rename, which is the one change that costs nothing. So
    the file stays where it was written, and this reads where that was.
    """
    already = modify.stacked_override(proposal)
    if already:
        return (already if os.path.isabs(already)
                else os.path.join(directory or ".", os.path.basename(already)))
    return os.path.join(directory or ".", f"{name}.override.ini")


def validate(key, value):
    """Is this a plausible value for this key? Shape only, never meaning.

    Shape is genuinely worth checking: `cluster_walltime = 30` is accepted by
    the ini parser, means thirty MINUTES to nothing at all, and produces a run
    that dies exactly as it did before. Meaning is not checkable here and is not
    attempted -- whether thirty hours is the right number is a question about a
    BAM this module has never seen.
    """
    text = (value or "").strip()
    if not text:
        return Verdict(False, "Nothing entered.")
    if key == "cluster_walltime" and not _WALLTIME.match(text):
        return Verdict(False, f"{text!r} is not a walltime. "
                              f"GenPipes writes HH:MM:SS, e.g. 35:00:00.")
    if key == "cluster_mem" and not _MEM.match(text):
        return Verdict(False, f"{text!r} is not a memory size. "
                              f"Try 96G, or a macro like %(LARGE_MEM)s.")
    if key == "cluster_cpu" and not _CPU.match(text):
        return Verdict(False, f"{text!r} is not a cpu count. "
                              f"Try 16, or a macro like %(PINT_CPU)s.")
    return Verdict(True)


def valid_section(text):
    """Is this shaped like a GenPipes step name?

    Not whether the step EXISTS -- that comes from `--help`, at the moment of
    asking, and is the caller's job. This only refuses the shapes that cannot be
    a section header at all, because a section GenPipes does not recognise is
    not an error: it is silently ignored, and the run fails again identically.
    """
    return bool(_SECTION.match((text or "").strip()))


def _parser():
    """RawConfigParser, because `ram = %(cluster_mem)s` is a value GenPipes
    interpolates and we are not GenPipes. A plain ConfigParser would try to
    expand it on read and raise, or expand it on write and freeze in a number
    that was supposed to follow cluster_mem around."""
    parser = configparser.RawConfigParser()
    parser.optionxform = str          # keys are case-sensitive in GenPipes inis
    return parser


def read(path):
    """{section: {key: value}} from an existing override ini, or {} if there is
    none. A file we cannot parse reads as empty rather than raising: it is
    somebody's hand-edited ini, and /modify's job is to offer to add to it, not
    to fail on the way to the question."""
    if not path or not os.path.exists(path):
        return {}
    parser = _parser()
    try:
        parser.read(path)
    except configparser.Error:
        return {}
    return {s: dict(parser.items(s)) for s in parser.sections()}


def merge(sections, step, settings):
    """`sections` with `settings` applied to `step`. Neither argument is
    mutated -- the caller usually still needs the original to show what changed.

    A key set to an empty string is REMOVED rather than written blank, which is
    how somebody undoes an override they added a minute ago without editing a
    file by hand.
    """
    out = {s: dict(keys) for s, keys in (sections or {}).items()}
    current = dict(out.get(step) or {})
    for key, value in (settings or {}).items():
        text = (value or "").strip()
        if text:
            current[key] = text
        else:
            current.pop(key, None)
    if current:
        out[step] = current
    else:
        out.pop(step, None)
    return out


def write(path, sections, run=""):
    """Write the override ini and return its path, or '' if there is nothing
    to write.

    An empty section set DELETES the file rather than leaving an empty one
    behind. An empty ini in the `-c` stack is harmless but not honest: it says a
    run has overrides when it has none, and the next person to read the command
    has to open the file to find out.

    The header is not decoration. This file ends up on a `-c` line months after
    anybody remembers typing it, and the three facts it carries -- what wrote
    it, for which run, and when -- are exactly what somebody needs before they
    trust or delete it.

    See wrote() for callers that need to distinguish "removed it" from "there
    was never one". This returns '' for both, which is right for deciding what
    goes on `-c` and wrong for telling somebody what just happened to a file.
    """
    if not sections:
        removed(path)
        return ""

    parser = _parser()
    for step in sorted(sections):
        parser.add_section(step)
        for key, value in sections[step].items():
            parser.set(step, key, value)

    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(path, "w") as handle:
        handle.write(f"# Private overrides for {run or 'this run'}, "
                     f"written {stamp}.\n")
        handle.write("# Goes LAST on the -c line, after every GenPipes ini, so "
                     "these win.\n")
        handle.write("# Nothing under $GENPIPES_INIS was touched.\n\n")
        parser.write(handle)
    return path


def removed(path):
    """Delete the override ini if it is there. True only if one actually went.

    Split out of write() because the caller announces this on screen, and
    write() cannot tell the two empty cases apart: it returns '' both when it
    deleted a file and when there was never a file to delete. The screen printed
    "pouletrun.override.ini was removed" either way, which is a claim about the
    filesystem made without looking -- on the one flow whose entire purpose is
    that a file on disk says what the run will do.
    """
    if path and os.path.exists(path):
        os.remove(path)
        return True
    return False


def copy(old_path, new_path):
    """Copy a run's override ini to a second run's name. True if one was copied.

    For the FORK, and only the fork. A fork is a second run that starts life
    with the first one's tuning, and path_for() names the file after the run
    precisely so that "two runs tuned differently cannot quietly share one
    file". Without this the fork's `-c` pointed at its parent's ini, and
    re-tuning either one silently re-tuned both -- which is the exact thing that
    docstring promises cannot happen.

    A rename does NOT come through here. See path_for().
    """
    if not old_path or not new_path or old_path == new_path:
        return False
    if not os.path.exists(old_path):
        return False
    with open(old_path) as handle:
        body = handle.read()
    with open(new_path, "w") as handle:
        handle.write(body)
    return True


def summary(sections):
    """One line naming what is overridden, for the mirror.

    Steps first and settings counted, rather than the other way round, because
    the question somebody has at the gate is "which steps am I tuning?" -- the
    numbers are what they open the file for.
    """
    if not sections:
        return ""
    steps = sorted(sections)
    total = sum(len(keys) for keys in sections.values())
    if len(steps) == 1:
        keys = sections[steps[0]]
        if len(keys) == 1:
            key, value = next(iter(keys.items()))
            return f"{steps[0]}  {_LABEL.get(key, key)} {value}"
        return f"{steps[0]}  {total} settings"
    return f"{', '.join(steps[:2])}{'…' if len(steps) > 2 else ''}  " \
           f"{total} settings on {len(steps)} steps"


def describe(sections):
    """Every override as a (step, label, value) row, for the review screen."""
    out = []
    for step in sorted(sections or {}):
        for key, value in sections[step].items():
            out.append((step, _LABEL.get(key, key), value))
    return out
