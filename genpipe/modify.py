"""Changing a proposal at the gate: what may change, and what a change costs.

The gate used to offer two verbs, approve and reject, and reject secretly meant
rework -- it regenerated and came back. So there was no way to abandon a run at
all, and a run you had mentally dropped went on appearing in /list and in the
startup pending line forever. Now there are three:

    /approve   submits. Irreversible, and the only one that spends anything.
    /modify    rewrites the command and asks you again. What reject used to do.
    /reject    abandons the run. Terminal, nothing submitted, reason recorded.

This module is the part of /modify that is not interface: which rows exist, what
their legal values are, which changes are checkable locally, and how a set of
changes becomes one sentence for the model.

Why a sentence and not a rebuilt command
----------------------------------------
Swapping `-t chipseq` for `-t atacseq` is a trivial string edit, and doing it
directly is the obvious implementation. It is also wrong. The model's view of
the conversation would then diverge from what is actually queued, and its next
turn would reason about a command it did not write -- which is the failure mode
the whole gate exists to prevent, arriving from the other direction. So every
substantive change goes back through the model as feedback, and the model
regenerates. One call, whatever the number of changes.

The one exception is the run's NAME, which changes no flag and needs no
regeneration. That is a registry write, and it is the only row here that costs
nothing.

Three tiers of validation, with genuinely different sources of truth
--------------------------------------------------------------------
    tier 1  enumerable, from slots.py. Protocols, and files that must exist.
            Checked inline as the field is entered, and never reaches the model.
    tier 2  form, not meaning. Is that a well-formed step range, is that a path.
    tier 3  steps and their dependencies. Reasoned about against `--help`, at
            the moment of applying, and reported as a RISK rather than a
            verdict -- see step_risk().

Standard library only. Like slots.py and gate.py, this runs in CI in seconds
without installing biomni, which is what lets the rules that matter be checked
on every push.
"""
import os
import re

from . import slots

# Rows the gate will offer to change, in the order they are offered. `name`
# comes first because it is the row where nothing happens to the run -- opening
# with the harmless one is what makes the panel safe to explore.
# Rows the gate will offer to change, in the order they are offered. `name`
# still comes first, and `pipeline` -- which is the most destructive row there
# is -- deliberately does not, even though the mirror DRAWS the pipeline first
# because it is the invocation. Those are two different orderings and conflating
# them briefly cost the property this list was arranged around: opening on the
# harmless row is what makes the panel safe to explore. See _ordered() in
# mirror.py, which floats the invocation to the top of the DISPLAY, and the
# cursor argument in cli._modify_guided, which keeps the CURSOR off it.
ROWS = ("name", "pipeline", "protocol", "steps", "design", "pairs", "readset",
        "config", "resources", "output")

# The order rows are FILLED in, which is not the order they are listed in.
# Listing order is about reading -- `name` first because it is the harmless row
# and opening with it makes the panel safe to explore. Filling order is about
# dependency: the answer to "which protocol?" depends on the pipeline, the legal
# step range comes from `--help` for that protocol, the -c stack follows from
# both, and a step to tune has to exist before it can be tuned. Asking in
# listing order means asking "which steps?" before knowing which pipeline's
# steps are meant.
FILL_ORDER = ("pipeline", "protocol", "design", "pairs", "readset", "config",
              "steps", "resources", "output", "name")

# `resources` is the odd row and is called out here because it does not behave
# like its neighbours. Every other row edits a flag that already exists; this
# one edits a FILE -- a private override ini, layer 6 of the -c stack -- and
# then adds that file to `-c` if it is not there yet. It exists because the
# alternative was what /diagnose used to do: print a correct four-line ini and
# leave somebody to create it, spell the step name exactly right, and remember
# which end of `-c` wins. See override.py.
RESOURCES = "resources"

# Which proposal slot each row reads its current value from. `name` is not a
# slot at all -- it is the registry's, not the command's -- which is exactly why
# it is the one row that needs no model call.
SLOT_OF = {
    "pipeline": "pipeline",
    "protocol": "protocol",
    "steps": "steps",
    "design": "design",
    "pairs": "pairs",
    "readset": "readset",
    "config": "inis",
    "output": "output_dir",
}

# How each row is described to the model when it becomes feedback. The flag,
# because the flag is what the model has to change and naming the row instead
# ("change the config") leaves it guessing which part of the -c stack is meant.
#
# Public because mirror.py reads it backwards, to decide which line of the
# displayed command a given row owns. That is the same question this table
# answers, asked from the other end, and answering it twice would let the
# highlight land on a flag the change does not touch.
FLAG_OF = {
    "protocol": "-t",
    "steps": "-s",
    "design": "-d",
    "pairs": "-p",
    "readset": "-r",
    "config": "-c",
    "output": "-o",
}

# Rows whose value is a file that has to be on disk before the run is generated.
# intake is the retrieval layer for these; this is only the check.
_FILE_ROWS = ("design", "pairs", "readset")

# Rows resolvable entirely from slots.py -- no filesystem, no model, no --help.
# A failure in one of these re-opens the field with the legal values rather than
# spending a generation to be told no.
_TIER1 = ("protocol",) + _FILE_ROWS

# Consequences a protocol carries that are not expressible as `needs` or `inis`
# -- they are demands on the READSET, which slots.py does not model because the
# readset is not a slot the gate can change. Kept here rather than left to be
# discovered from a failed run, because a chipseq readset run as atacseq
# generates, submits and produces peaks for a mark that is not what was called.
_CONSEQUENCE = {
    "atacseq": "atacseq needs the readset's mark column to be 'atac'",
    "chipseq": "chipseq reads the mark column; a design file adds "
               "differential binding",
}


def current(proposal, row):
    """What this row says now, as a display string, or '' if it says nothing."""
    if row == "name":
        return ""
    value = ((proposal or {}).get("slots") or {}).get(SLOT_OF.get(row, row))
    if value in (None, "", [], ()):
        return ""
    if isinstance(value, (list, tuple)):
        return " , ".join(dict.fromkeys(str(v) for v in value))
    return str(value)


def rows_for(proposal, name="", resources=""):
    """The changeable rows for this proposal, as (row, current value).

    A row with nothing in it is still offered -- adding a `-d` that was never
    there is a change like any other -- except for pairs and design, which are
    only offered when the protocol has any use for them. Offering to attach a
    pairs file to a germline run is offering a mistake.

    `resources` is a one-line summary of the run's private override ini, from
    override.summary(). It is passed in rather than read here because this
    module does not touch the filesystem for display -- the same reason `name`
    is passed in rather than looked up.
    """
    slot_values = (proposal or {}).get("slots") or {}
    pipeline = slot_values.get("pipeline")
    protocol = slot_values.get("protocol") or slots.DEFAULTS.get(pipeline or "")
    proto = slots.find_protocol(pipeline, protocol) if pipeline else None

    out = []
    for row in ROWS:
        if row == RESOURCES:
            # Always offered, and offered even when empty. "No step is tuned
            # yet" is the normal state of a fresh run and is exactly when
            # somebody who knows their data needs to raise a walltime.
            out.append((row, resources or ""))
            continue
        if row == "design" and not current(proposal, row):
            if not (proto and proto.needs == slots.DESIGN) and pipeline not in ("chipseq",):
                continue
        if row == "pairs" and not current(proposal, row):
            if not (proto and proto.needs == slots.PAIRS):
                continue
        if row == "protocol" and pipeline and not slots.protocols(pipeline):
            continue          # this pipeline takes no -t at all
        out.append((row, name if row == "name" else current(proposal, row)))
    return out


def options_for(row, proposal, candidates=None, pending=None):
    """The vocabulary for a row, as slots.Option rows, or [] if it has none.

    Empty is a real answer and the caller must honour it by degrading to a plain
    prompt. A panel with nothing to offer costs a keystroke to reach the
    free-text row and implies there were alternatives worth reading -- which is
    the rule _panel already follows for the agent's own questions.
    """
    values = (proposal or {}).get("slots") or {}
    pipeline = (pending or {}).get("pipeline") or values.get("pipeline")
    candidates = candidates or {}

    if row == "pipeline":
        return [slots.Option(p, p, slots._pipeline_blurb(p))
                for p in sorted(slots.PIPELINES)]
    if row == "protocol" and pipeline:
        # The protocol list follows the pipeline currently PICKED, not the one
        # the proposal was generated with. When both move in one pass, the
        # pipeline is filled first (see FILL_ORDER) and this is what makes the
        # second question offer chipseq's protocols rather than rnaseq's.
        return [slots.Option(p.name, p.name, p.blurb)
                for p in slots.protocols(pipeline)]
    if row in _FILE_ROWS:
        return [slots.Option(path, path) for path in candidates.get(row) or ()]
    if row == "config" and pipeline:
        protocol = values.get("protocol") or slots.DEFAULTS.get(pipeline)
        expected = slots.expected_inis(pipeline, protocol)
        return [slots.Option(ini, ini, "feature ini for this protocol")
                for ini in expected or ()]
    # name, steps and output are free text. A step range is a range, not a list,
    # and a path is a path -- inventing options for either would be inventing.
    return []


def question_for(row, proposal, pending=None):
    """The prompt shown when filling a row."""
    values = (proposal or {}).get("slots") or {}
    pipeline = ((pending or {}).get("pipeline") or values.get("pipeline") or "")
    if row == "pipeline":
        return "Which pipeline?"
    if row == "name":
        return "What should this run be called?"
    if row == "protocol":
        return f"Which {pipeline} protocol?".strip()
    if row == "steps":
        return "Which steps? (a GenPipes -s range, e.g. 1-5 or 3,6-8)"
    if row == "output":
        return "Which output directory?"
    if row == "config":
        return "Which config ini should be added?"
    return f"Which {row} file?"


# ---------------------------------------------------------------------------
# Tier 1 and tier 2. Local, deterministic, inline.
# ---------------------------------------------------------------------------

class Verdict:
    """The result of checking one field. `options` is what to offer instead
    when the answer was wrong in a way the table can correct."""

    __slots__ = ("ok", "message", "options", "note")

    def __init__(self, ok, message="", options=(), note=""):
        self.ok = ok
        self.message = message
        self.options = list(options)
        self.note = note

    def __bool__(self):
        return self.ok

    def __repr__(self):
        return f"<Verdict {'ok' if self.ok else 'no'} {self.message!r}>"


_STEP_RANGE = re.compile(r"^\s*\d+(-\d+)?(\s*,\s*\d+(-\d+)?)*\s*$")


def valid_steps(text):
    """Is this a well-formed GenPipes -s range? Form only, not meaning.

    Whether the steps make sense together is tier 3 and needs --help; whether
    `1-5,8` parses at all is a regex, and catching it here saves a round trip
    to a model to be told what a regex knew.
    """
    if not _STEP_RANGE.match(text or ""):
        return False
    for part in (text or "").split(","):
        bounds = [b for b in part.strip().split("-") if b]
        nums = [int(b) for b in bounds]
        if any(n < 1 for n in nums):
            return False
        if len(nums) == 2 and nums[0] > nums[1]:
            return False
    return True


def check(row, value, proposal, directory=".", registry=None, name=None,
          pending=None):
    """Validate one field's new value. Tiers 1 and 2 only.

    Returns a Verdict. A failing tier-1 verdict carries the legal options, so
    the caller can re-open the field with them instead of printing an error and
    leaving the person to guess -- "'germline_snv' is a dnaseq protocol; this
    run is chipseq. chipseq takes:" and then the two real ones.
    """
    value = (value or "").strip()
    if not value:
        return Verdict(False, "Nothing entered.")

    values = (proposal or {}).get("slots") or {}
    pipeline = (pending or {}).get("pipeline") or values.get("pipeline")

    if row == "pipeline":
        if value not in slots.PIPELINES:
            return Verdict(False, f"{value!r} is not a pipeline on this "
                                  f"install. There are:",
                           [slots.Option(p, p, slots._pipeline_blurb(p))
                            for p in sorted(slots.PIPELINES)])
        return Verdict(True)

    if row == "protocol":
        if not pipeline:
            return Verdict(True)          # nothing to check it against
        proto = slots.find_protocol(pipeline, value)
        if proto is None:
            legal = slots.protocols(pipeline)
            owner = next((p for p, ps in slots.PIPELINES.items()
                          if any(x.name == value for x in ps)), None)
            lead = (f"{value!r} is a {owner} protocol; this run is {pipeline}."
                    if owner else
                    f"{value!r} is not a {pipeline} protocol.")
            return Verdict(False, f"{lead} {pipeline} takes:",
                           [slots.Option(p.name, p.name, p.blurb) for p in legal])
        note = ""
        if proto.needs == slots.DESIGN and not values.get("design"):
            note = (f"{value} needs a design file — there is no -d on this "
                    f"command yet")
        elif proto.needs == slots.PAIRS and not values.get("pairs"):
            note = (f"{value} needs a pairs file — there is no -p on this "
                    f"command yet")
        elif proto.inis:
            note = f"{value} also wants {', '.join(proto.inis)} in the -c stack"
        elif _CONSEQUENCE.get(value):
            note = _CONSEQUENCE[value]
        return Verdict(True, note=note)

    if row in _FILE_ROWS:
        path = value if os.path.isabs(value) else os.path.join(directory, value)
        if not os.path.exists(path):
            return Verdict(False, f"{value!r} is not on disk here. "
                                  f"genpipes would fail on the argument.")
        return Verdict(True)

    if row == "steps":
        if not valid_steps(value):
            return Verdict(False, f"{value!r} is not a step range. "
                                  f"Try 1-5, or 3,6-8.")
        return Verdict(True)

    if row == "output":
        if not os.path.isdir(value if os.path.isabs(value)
                             else os.path.join(directory, value)):
            return Verdict(True, note=f"{value} does not exist yet — GenPipes "
                                      f"will create it")
        return Verdict(True)

    if row == "name":
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", value):
            return Verdict(False, "A run name is letters, digits, dot, dash "
                                  "and underscore.")
        if registry is not None:
            record = registry.get(name) if name else None
            if record and record.get("status") != "held":
                return Verdict(False, f"'{name}' has already been submitted — "
                                      f"its name is tied to a job list now.")
        return Verdict(True)

    return Verdict(True)


# ---------------------------------------------------------------------------
# Cross-field checks. These cannot run earlier, because they need every
# selection to exist -- which is also why the apply screen is the only place
# they can happen.
# ---------------------------------------------------------------------------

def required_after(proposal, changes):
    """Rows a change set has just made MANDATORY, as {row: why}.

    The difference between this and cross_check() is what the answer is for.
    cross_check returns notes -- things worth knowing that somebody may well
    have intended. This returns rows that cannot be left as they are, because
    the change already made has invalidated them.

    Changing the PIPELINE is the case that motivates it. `-t stringtie` is an
    rnaseq protocol and means nothing to chipseq; the `-c` stack is built out of
    `<pipeline>.base.ini` and is wrong the moment the pipeline moves; the step
    numbers came from a different `--help` entirely. None of that is a warning.
    A run generated from the old answers would fail, or -- worse, and this is
    the shape genpipes.md keeps returning to -- generate and run for hours with
    the wrong parameters.

    Returned as a mapping rather than a list so the caller can say WHY beside
    each red row. "protocol" on its own is a demand; "protocol — stringtie is
    an rnaseq protocol and this is chipseq now" is an explanation somebody can
    act on without going to look anything up.
    """
    values = (proposal or {}).get("slots") or {}
    out = {}

    was = values.get("pipeline")
    now = changes.get("pipeline", was)
    if now and was and now != was:
        protocol = changes.get("protocol") or values.get("protocol")
        if slots.protocols(now):
            if protocol and find_protocol(now, protocol) is None:
                out["protocol"] = (f"{protocol} is a {was} protocol; "
                                   f"{now} has its own")
            elif not protocol:
                out["protocol"] = f"{now} takes a -t and there is none"
        if current(proposal, "config"):
            out["config"] = (f"the -c stack is built on {was}.base.ini and "
                             f"has to be rebuilt on {now}")
        if current(proposal, "steps"):
            out["steps"] = (f"step numbers come from {now}'s --help, not "
                            f"{was}'s — the same number is a different step")

    # A protocol that demands a file it does not have. Checked whether or not
    # the pipeline moved, because swapping protocol alone does this too.
    pipeline = now or was
    protocol = changes.get("protocol") or values.get("protocol")
    proto = find_protocol(pipeline, protocol) if pipeline else None
    if proto and proto.needs == slots.PAIRS:
        if not (changes.get("pairs") or values.get("pairs")):
            out["pairs"] = f"{protocol} is a paired analysis and needs -p"
    if (proto and proto.needs == slots.DESIGN
            and pipeline not in slots._DESIGN_OPTIONAL):
        if not (changes.get("design") or values.get("design")):
            out["design"] = f"{protocol} needs a design file (-d)"
    return out


def fill_order(rows):
    """`rows` sorted into dependency order. See FILL_ORDER."""
    rank = {row: i for i, row in enumerate(FILL_ORDER)}
    return sorted(rows, key=lambda row: (rank.get(row, len(FILL_ORDER)), row))


def find_protocol(pipeline, protocol):
    """slots.find_protocol, tolerating a pipeline that has none."""
    try:
        return slots.find_protocol(pipeline, protocol)
    except Exception:
        return None


def cross_check(proposal, changes):
    """Consequences that only appear once the whole change set is known.

    Returned as notes, not errors. Every one of these describes something the
    user may well have intended, and a modify flow that refuses a legal change
    because it has a consequence is a flow that makes you fight it.
    """
    values = dict((proposal or {}).get("slots") or {})
    pipeline = values.get("pipeline")
    after = dict(values)
    for row, new in changes.items():
        if row in SLOT_OF:
            after[SLOT_OF[row]] = new

    notes = []
    protocol = after.get("protocol")
    proto = slots.find_protocol(pipeline, protocol) if pipeline else None
    if proto:
        if proto.needs == slots.DESIGN and not after.get("design"):
            if pipeline in slots._DESIGN_OPTIONAL:
                notes.append(f"{protocol} without a design file skips the "
                             f"differential-binding step rather than failing")
            else:
                notes.append(f"{protocol} needs a design file (-d) and there "
                             f"is none")
        if proto.needs == slots.PAIRS and not after.get("pairs"):
            notes.append(f"{protocol} needs a pairs file (-p) and there is none")
        have = " ".join(str(x) for x in (after.get("inis") or ()))
        for ini in proto.inis:
            if ini not in have:
                notes.append(f"{protocol} wants {ini} in the -c stack")
        if protocol != values.get("protocol") and _CONSEQUENCE.get(protocol):
            notes.append(_CONSEQUENCE[protocol])
    return notes


# ---------------------------------------------------------------------------
# Tier 3: steps and their dependencies.
#
# There is no step table in this repo and there must never be one. genpipes.md
# says it outright -- "Never take a step number from this document. There are
# none here by design" -- because the numbered step list for every protocol
# comes from `genpipes <pipeline> -t <protocol> --help` at the moment of need,
# and is version-exact. A table here would be wrong on the next GenPipes
# release and would contradict the document the agent is told to trust.
#
# So this function takes the --help text as an argument. It does not know the
# steps; it reads them.
# ---------------------------------------------------------------------------

# `3- trimmomatic` / `12- gatk_haplotype_caller`, which is how GenPipes numbers
# the step list in its own --help output.
_HELP_STEP = re.compile(r"^\s*(\d+)-\s*(\S+)")


def steps_from_help(text):
    """[(number, name)] as --help lists them. Empty if it did not list any."""
    seen, out = set(), []
    for line in (text or "").splitlines():
        m = _HELP_STEP.match(line)
        if m:
            n = int(m.group(1))
            if n not in seen:
                seen.add(n)
                out.append((n, m.group(2)))
    return out


def expand(text):
    """A -s range as a sorted list of step numbers, or [] if it is malformed."""
    if not valid_steps(text):
        return []
    out = set()
    for part in (text or "").split(","):
        bounds = [int(b) for b in part.strip().split("-") if b]
        if len(bounds) == 1:
            out.add(bounds[0])
        else:
            out.update(range(bounds[0], bounds[1] + 1))
    return sorted(out)


def step_risk(wanted, help_text):
    """Whether a step range skips something the requested steps depend on.

    Reported as a RISK with its reasoning, never as a certainty. What is being
    inferred here is a dependency between steps from their names, and that
    inference can be wrong -- GenPipes' own generation is the authoritative
    check, and when it refuses it names exactly which step, input or config
    option it rejected. That message is better than any guess made here, so the
    change is allowed through and the guess is offered as a warning.

    An out-of-range step number is the one hard stop: it means --help was not
    read, or was read for the wrong protocol, and generating against it wastes
    a round trip to be told a number does not exist.

    Returns (risks, hard_stop). Both may be empty, which is the normal case.
    """
    numbers = expand(wanted)
    known = steps_from_help(help_text)
    if not numbers or not known:
        return [], None

    valid = {n for n, _ in known}
    names = dict(known)
    outside = [n for n in numbers if n not in valid]
    if outside:
        return [], (f"step {', '.join(str(n) for n in outside)} is not in this "
                    f"protocol — it has {min(valid)}-{max(valid)}")

    skipped = [n for n in range(min(numbers), max(numbers) + 1)
               if n not in numbers]
    below = [n for n in valid if n < min(numbers)]

    risks = []
    if skipped:
        risks.append(
            f"skipping {_ranges(skipped)} "
            f"({', '.join(names[n] for n in skipped[:3])}) but running "
            f"{max(numbers)}\n— a later step usually reads what a skipped one "
            f"produces")
    elif below:
        risks.append(
            f"starting at {min(numbers)} ({names[min(numbers)]}), so "
            f"{_ranges(sorted(below))} will not run\n— fine on a re-run, wrong "
            f"on a first one: their outputs have to already exist")
    return risks, None


def _ranges(numbers):
    """[3,4,5,9] -> '3-5, 9'. Reads the way somebody would say it."""
    out, start, prev = [], None, None
    for n in list(numbers) + [None]:
        if start is None:
            start, prev = n, n
            continue
        if n == prev + 1:
            prev = n
            continue
        out.append(str(start) if start == prev else f"{start}-{prev}")
        start, prev = n, n
    return ", ".join(out)


# ---------------------------------------------------------------------------
# Turning a change set into one instruction for the model.
# ---------------------------------------------------------------------------

def sentence(proposal, changes):
    """One unambiguous feedback sentence covering every change.

    Names both sides of every delta and then names what must NOT move. The
    second half is the part that earns its length: a model told only "change -t
    to atacseq" regenerates the whole command and drifts on a step range or an
    output directory nobody asked it to touch, and the person at the gate then
    has to re-read every row to notice.
    """
    substantive = {r: v for r, v in changes.items() if r != "name"}
    # A resources change whose ini is already on the -c line changed a FILE, not
    # a command. Dropping it here is what keeps a re-tune from costing a
    # regeneration -- and a regeneration that has nothing to change is a chance
    # for the command to drift, which is the failure this whole module avoids.
    if (RESOURCES in substantive
            and already_stacked(proposal, substantive[RESOURCES])):
        substantive.pop(RESOURCES)
    if not substantive:
        return ""

    parts = _deltas(proposal, substantive)
    untouched = [FLAG_OF[r] for r in FLAG_OF
                 if r not in substantive and current(proposal, r)]
    tail = (f"; leave {', '.join(untouched)} exactly as they are"
            if untouched else "")
    return "; ".join(parts) + tail + ". Regenerate the command and stop at the gate again."


def fork_sentence(proposal, changes):
    """The same change set, addressed to a conversation that has not heard any
    of this before.

    sentence() is written for the thread that produced the command: it says
    "change -t from stringtie to atacseq" and trusts the model to still have the
    other seven flags in front of it. A fork has no such history -- it is a new
    thread opened so that the variant becomes a SECOND run rather than replacing
    the first -- so the base command has to travel with the request or the model
    will invent the parts nobody mentioned.

    Quoting the command verbatim is safe here in a way that rebuilding it would
    not be. The model is still the one that writes the new command; this only
    tells it what the old one was.
    """
    substantive = {r: v for r, v in changes.items() if r != "name"}
    base = " ".join((proposal or {}).get("generated", "").split())
    if not base:
        return ""
    if not substantive:
        return (f"Generate this GenPipes run again, exactly as written, and "
                f"stop at the gate:\n\n    {base}\n")
    deltas = "\n".join(f"  - {p}" for p in _deltas(proposal, substantive))
    return (f"Generate a variant of this GenPipes run:\n\n    {base}\n\n"
            f"with these changes, and nothing else changed:\n{deltas}\n\n"
            f"Do not submit it. Stop at the gate.")


def _deltas(proposal, substantive):
    """'change -t from stringtie to atacseq' for each row that moved."""
    out = []
    for row, new in substantive.items():
        if row == RESOURCES:
            # The one row whose change is not a value swap. The file is already
            # written by the time this runs; all the model has to do is put it
            # LAST on -c, and saying so explicitly matters more here than
            # anywhere else -- an override ini that lands before the cluster ini
            # is silently overruled by it and the run behaves as though nobody
            # touched anything.
            out.append(f"append {new} to the very END of the -c stack, after "
                       f"every other ini, and change nothing else about -c "
                       f"(it is a private override ini and has to win)")
            continue
        flag = FLAG_OF.get(row, row)
        old = current(proposal, row)
        out.append(f"change {flag} from {old} to {new}" if old
                   else f"add {flag} {new}")
    return out


def already_stacked(proposal, path):
    """Is this override ini already on the -c line?

    Asked because re-tuning a step that was tuned before changes the FILE and
    not the command, and putting a model call in front of a change the command
    does not need is a regeneration that can only introduce drift. The gate is
    redrawn instead -- the mirror reads the summary fresh off the file, so the
    new walltime shows up without anybody regenerating anything.
    """
    if not path:
        return False
    base = os.path.basename(str(path))
    haystack = " ".join([str((proposal or {}).get("generated") or "")]
                        + [str(v) for v in
                           (((proposal or {}).get("slots") or {}).get("inis") or ())])
    return base in haystack


# ---------------------------------------------------------------------------
# Prose at the gate.
# ---------------------------------------------------------------------------

# Lines that mean "yes, do it". Matched against the SINGLE LINE just typed and
# never against accumulated conversation text -- the system prompt contains the
# whole of genpipes.md, so a substring test over history is true of everything
# (see DevLLM's docstring in fakecluster.py, which records that bug).
#
# The list exists so that typing "looks good" at the gate is answered with the
# /approve line rather than treated as a change request. It must never CAUSE an
# approval: approval is typed, always, and this is a refusal path.
_APPROVAL = (
    "lgtm", "looks good", "looks right", "go ahead", "go for it", "yes",
    "yep", "yeah", "ok", "okay", "do it", "send it", "ship it", "approve",
    "approved", "submit", "submit it", "run it", "launch it", "sounds good",
    "perfect", "great", "fine", "sure", "proceed", "confirm", "confirmed",
)


def is_approval_shaped(line):
    """Does this line mean 'yes'? Used only to REFUSE, never to submit.

    Deliberately strict: EVERY comma-or-and-separated clause has to be one of
    the phrases. "looks good, go ahead" is two of them and is an approval;
    "yes, but use steps 1-8" is not, and must reach the modify path, which a
    prefix match would have prevented.
    """
    raw = (line or "").strip().lower()
    if not raw:
        return False
    clauses = re.split(r"[,;.!]+|\band\b|\bthen\b", raw)
    clauses = [c for c in (c.strip() for c in clauses) if c]
    if not clauses:
        return False
    for clause in clauses:
        text = re.sub(r"\s+", " ",
                      re.sub(r"[^a-z ]", "", clause)).strip()
        for prefix in ("please ", "lets ", "let us ", "just ", "ok "):
            if text.startswith(prefix):
                text = text[len(prefix):]
        if text not in _APPROVAL:
            return False
    return True
