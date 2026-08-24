"""Where a run's config values came from, established without deciding anything.

WHAT THIS MODULE IS FOR
-----------------------
A GenPipes command layers ini files with `-c`, left to right, later winning.
When a step dies of a resource limit, the question that follows is always the
same: which file set that limit. Answering it needs three facts, and until this
module existed /diagnose had none of them reliably.

  the stack        which inis, in which order. Read from the run's own config
                   trace, because that is GenPipes' record of what it did --
                   not our parse of what we think we asked for. See stack().
  the merged value what the layering actually produced at generation time. Also
                   from the trace, which is a resolved snapshot. See effective().
  the sources      what each of those files says in that section, now, on disk.
                   See sections().

WHAT THIS MODULE DELIBERATELY DOES NOT DO
-----------------------------------------
It never says which file caused anything.

That restraint is the point, and it is worth being exact about where the line
falls, because two of the operations here look like they cross it and do not:

  reading `[step]` out of five inis and reporting what each says is the same
  kind of act as reading a log. It is transcription. Nothing is concluded.

  reporting that the trace's merged value and the current files disagree is a
  COMPARISON of two observations. It is not a claim about why they disagree.
  There are several ordinary reasons -- a file edited since the run, a relative
  path that resolved somewhere else at generation time, a `$VARIABLE` that
  pointed elsewhere, an interpolation this module does not expand -- and
  choosing between them is reasoning over evidence, which is the model's work.
  So the disagreement is stated as a fact and the sentence stops there.

The same rule governs the two stacks. When the trace and the stored record
disagree about which inis were used, both are printed and neither is declared
correct. Silently preferring one would be this module deciding what the
evidence is, having been written because exactly that went wrong: a run
submitted with five inis was diagnosed from a stored parse that had four, the
missing one was the file that caused the failure, and the model -- applying the
correct last-wins rule to a truncated list -- named the wrong file.

Standard library only, like gate.py, modify.py and slots.py.
"""
import os

from . import gate
from . import intake

# WHICH KEYS ARE QUOTED, and what that is and is not.
#
# It is a volume bound. A GenPipes ini section carries a dozen or more keys,
# most of them module versions, and quoting every key of every section of every
# file in a stack would put more text in the prompt than the logs do. These are
# the scheduler-facing ones.
#
# It is NOT a diagnostic rule. Nothing anywhere concludes anything from a key
# being in this tuple, from its value, or from its absence; no key implies a
# cause and no cause is looked for. Change the tuple and the same evidence is
# gathered about different keys -- nothing downstream reasons differently.
#
# Its one real cost is that a failure turning on a key outside it would not be
# quoted here, so lines() says outright that this is a selection and the files
# are there to be read. Everything omitted stays one <execute> away.
QUOTED_KEYS = ("cluster_walltime", "cluster_mem", "cluster_cpu", "ram",
               "cluster_queue", "cluster_other_arg", "other_options")

NO_SECTION = "no such section"
UNREADABLE = "unreadable"


def stack(record, trace=None):
    """The ordered `-c` stack for a run, and where the answer came from.

    Returns a dict:

        inis      the stack to work from, in order
        source    "trace" | "record" | ""
        recorded  what the run record's slots say, always, for comparison
        agrees    True/False when both exist, None when only one does

    THE TRACE IS PREFERRED, and the reason is not that it is newer but that it
    is a different KIND of evidence. `slots["inis"]` is our parse of the command
    we believe was generated, frozen at gate time and never re-derived; the
    trace header is GenPipes' own transcript of the command it actually ran.
    When a parser bug, an edit, or a rebuild puts those two out of step, the
    second is the one describing the run being diagnosed.

    `agrees` compares by basename and order, which is the only vocabulary the
    two share -- a trace lists absolute paths and a record may hold
    `$GENPIPES_INIS/...` or a bare name. That comparison is deliberately weak,
    and it is used for ONE thing: deciding whether to show the reader both
    lists. It never resolves which file anything is; modify.locate() exists for
    that and is not weakened here.
    """
    recorded = list(((record.get("proposal") or {}).get("slots") or {})
                    .get("inis") or ())
    found = []
    if trace:
        found = gate.flag_values(trace.get("command") or "", "-c")

    if found:
        inis, source = found, "trace"
    elif recorded:
        inis, source = list(recorded), "record"
    else:
        inis, source = [], ""

    agrees = None
    if found and recorded:
        agrees = ([os.path.basename(p) for p in found]
                  == [os.path.basename(p) for p in recorded])
    return {"inis": inis, "source": source, "recorded": recorded,
            "agrees": agrees}


def section(path, step, keys=QUOTED_KEYS):
    """The `[step]` section of one ini, as {key: value}, or a marker string.

    Returns NO_SECTION when the file is fine and has no such section -- which
    is a fact about the layering and not a gap in the evidence -- and UNREADABLE
    when the file could not be opened. Neither is filled in with a guess.

    Hand-scanned rather than handed to configparser, for two reasons that both
    bite here: GenPipes inis use `%(...)s` interpolation that configparser will
    try to expand against a DEFAULT section this function is not reading, and a
    file that fails to parse anywhere would cost the section that does parse.
    A line scanner reads what is written and stops.
    """
    if not path:
        return UNREADABLE
    try:
        handle = open(path, errors="replace")
    except OSError:
        return UNREADABLE

    wanted = f"[{step}]"
    found, out = False, {}
    with handle:
        for line in handle:
            text = line.strip()
            if text.startswith("[") and text.endswith("]"):
                if found:
                    break
                found = text == wanted
                continue
            if not found or not text or text.startswith(("#", ";")):
                continue
            key, sep, value = text.partition("=")
            if not sep:
                continue
            key = key.strip()
            if keys is None or key in keys:
                out[key] = value.strip()
    if not found:
        return NO_SECTION
    return out


def sections(inis, step, workdir=None, keys=QUOTED_KEYS):
    """What every ini in the stack says about one step, in stack order.

    A list of {"ini", "path", "found", "settings"} where `settings` is a dict,
    NO_SECTION or UNREADABLE. `found` is whether the path resolved at all.

    Relative entries are resolved against `workdir`: a `-c` entry written as a
    bare name refers to a file beside the RUN, not beside this process, and
    resolving it against the current directory would either miss or -- worse --
    find a different file with the same name. An entry holding an unexpanded
    `$VARIABLE` is reported as unresolved rather than guessed at.
    """
    out = []
    for ref in inis or ():
        path, found = _resolve(ref, workdir)
        out.append({
            "ini": ref,
            "path": path,
            "found": found,
            "settings": section(path, step, keys) if found else UNREADABLE,
        })
    return out


def _resolve(ref, workdir=None):
    """(path, exists) for one `-c` entry. Never expands a $VARIABLE."""
    if not ref:
        return None, False
    if "$" in ref:
        # The value as written points through the environment this process is
        # not the one that ran the command. Expanding it here would produce a
        # path that looks authoritative and is a guess.
        return ref, False
    path = ref if os.path.isabs(ref) else os.path.join(workdir or os.getcwd(), ref)
    return path, os.path.isfile(path)


def effective(trace_path, step, keys=QUOTED_KEYS):
    """What the trace records as the merged value of `[step]` at generation time.

    A config trace is itself an ini -- the resolved snapshot, after layering --
    so the same scanner reads it. This is the ONLY value here that describes the
    run as it was submitted. Everything sections() returns describes those files
    as they are now, and the two are different observations of different
    moments.
    """
    return section(trace_path, step, keys)


def report(record, step, trace_path=None, workdir=None):
    """Everything above for one step, as one structure. Nothing is concluded.

        stack       what stack() returned
        step        the step this is about
        effective   merged values from the trace, or a marker
        sources     what sections() returned, in stack order
        trace       the trace path used, or None
        differs     keys where the trace and the LAST source that sets them
                    disagree -- a comparison, not an explanation

    `differs` is computed because a person reading five sections of ini in a
    terminal will not spot it, and because it is the observation most likely to
    matter. It says WHICH keys disagree and stops. See the module docstring for
    why it does not say why.
    """
    layers = stack(record, intake.read_trace(trace_path) if trace_path else None)
    merged = effective(trace_path, step) if trace_path else UNREADABLE
    sources = sections(layers["inis"], step, workdir)

    differs = []
    if isinstance(merged, dict):
        for key, value in merged.items():
            # The last layer that sets the key at all is the one the ordinary
            # reading of `-c` points at. Comparing against it is arithmetic over
            # the stack order, not a claim that this file is responsible.
            last = None
            for row in sources:
                if isinstance(row["settings"], dict) and key in row["settings"]:
                    last = row
            if last is not None and last["settings"][key] != value:
                differs.append({"key": key, "trace": value,
                                "ini": last["ini"],
                                "now": last["settings"][key]})
    return {"stack": layers, "step": step, "effective": merged,
            "sources": sources, "trace": trace_path, "differs": differs}


def lines(found):
    """report() rendered for a prompt. Facts, in the order they were gathered.

    Kept here beside the thing it renders so that a field added above cannot
    quietly stop being shown, and so agent.py holds no opinion about what any of
    this means -- it appends these lines and moves on.
    """
    out = []
    layers = found["stack"]
    if not layers["inis"]:
        return out

    where = {"trace": "from this run's config trace, which is GenPipes' own "
                      "record of the command it ran",
             "record": "from the run record -- no config trace was found on "
                       "disk for this run"}.get(layers["source"], "")
    out.append(f"The -c stack, in order ({where}):")
    for i, ref in enumerate(layers["inis"], 1):
        out.append(f"  {i}. {ref}")

    if layers["agrees"] is False:
        out += [
            "",
            "  NOTE: the run record's stored stack is not the same as the "
            "trace's. Both are given; this layer does not choose between them.",
            "    stored in the run record: " + " , ".join(layers["recorded"]),
        ]
    out.append("")

    step = found["step"]
    merged = found["effective"]
    if isinstance(merged, dict) and merged:
        out.append(f"[{step}] AS THE TRACE RECORDS IT -- the merged values this "
                   f"run was actually generated with:")
        out += [f"    {k} = {v}" for k, v in merged.items()]
    elif merged == NO_SECTION:
        out.append(f"The trace has no [{step}] section, so this run's merged "
                   f"values for it are not recorded there.")
    else:
        out.append("No config trace was readable for this run, so the merged "
                   "values it was generated with are not available.")
    out.append("")

    out.append(f"[{step}] IN EACH SOURCE FILE AS IT STANDS NOW, in stack order:")
    for i, row in enumerate(found["sources"], 1):
        name = row["ini"]
        settings = row["settings"]
        if settings == NO_SECTION:
            out.append(f"  {i}. {name}: no [{step}] section")
        elif settings == UNREADABLE:
            out.append(f"  {i}. {name}: not readable from here"
                       + ("" if row["found"] else " (path did not resolve)"))
        elif not settings:
            out.append(f"  {i}. {name}: [{step}] present, none of the resource "
                       f"keys set")
        else:
            out.append(f"  {i}. {name}: "
                       + " ; ".join(f"{k} = {v}" for k, v in settings.items()))
    out.append(f"  (Only the scheduler-facing keys are quoted above. These "
               f"files hold others; read any of them if your reasoning needs "
               f"a key that is not shown.)")
    out.append("")

    out += [
        "THESE ARE TWO DIFFERENT OBSERVATIONS. The trace records what the "
        "layering produced when the command was generated; the files record "
        "what they contain now. They are not required to agree.",
    ]
    if found["differs"]:
        out.append("  They do not agree on:")
        for row in found["differs"]:
            out.append(f"    {row['key']}: trace says {row['trace']}, while "
                       f"{row['ini']} -- the last file in the stack that sets "
                       f"it -- now says {row['now']}")
        out += [
            "",
            "  WHAT THAT DISAGREEMENT DOES AND DOES NOT ESTABLISH. Taken alone "
            "it establishes exactly one thing: the current source files do not "
            "reproduce the configuration this run was generated with.",
            "  It does NOT establish which file supplied the historical value, "
            "that any particular file was changed, or when. Several ordinary "
            "situations produce it -- a file edited since, a relative path that "
            "resolved to a different file from the directory the command was "
            "run in, a $VARIABLE that pointed elsewhere, an interpolation not "
            "expanded here. Nothing above distinguishes between them.",
            "  If which one it is matters to your answer, that is evidence you "
            "do not yet have and can go and get: a backup or .bak beside the "
            "file, an archived copy, version control, an earlier trace from "
            "another run, or the generated .sh, which was written from the "
            "historical values. Read it, then say what it showed. Otherwise "
            "report the disagreement as the disagreement it is.",
        ]
    out.append("")
    return out
