"""Turning a diagnosis into a prepared retry, deterministically.

WHAT WAS MISSING
----------------
/diagnose ended by naming a fix and offering /modify. /modify forks a run and
opens a panel -- which is the right machinery and the wrong starting point:
somebody who has just been told exactly which section, which key and which
value has to open the resources row and type all three back in, correctly, from
a screen they are now scrolling away from. The step that was missing had a
shape but no verb.

So this is the verb. `/relaunch <name>` means: build a revision of this run
with the diagnosed change already in it, and stop at the ordinary gate.

WHAT IT IS NOT
--------------
It is not a resubmission. Nothing here reaches Slurm, and the run it produces
is `held` like any other -- see cli._cmd_relaunch, which ends at the same gate
/modify does. The approval boundary is unchanged and unbypassed, and that is
the reason "prepare" is in every sentence describing this file.

It is also not a second gate. The revision is held, verified and approved by
exactly the machinery every other run goes through -- registry.hold, the
LangGraph interrupt, modify.realized, /approve -- and the change set it carries
is in the shape modify.declaration() already takes.

WHY NO MODEL RUNS HERE
----------------------
It did, once, and it cost 36 inference steps and 108 seconds to produce a
command this module already knew every field of. A /modify may need a model:
"give alignment more memory" is a sentence that has to be understood. A
relaunch has nothing left to understand by the time it starts -- the config
stack, the step range and the override path are all decided by the code above
the model, and handing them over as prose so they can be typed back is the
whole of what those 36 steps were doing.

So command() writes the revision's generation itself, out of the source run's
own invocation, changing only the flags the plan changes and leaving every
other token exactly as the run that worked wrote it. What that buys is not only
speed: a model asked to retype a command drops the parts nobody mentioned, and
the one it produced for dnaseq-somatic-fastpass-0805 dropped the assignment its
own `-o "$OUTDIR"` depended on -- so the retry would have written somewhere
other than the directory whose `.done` files are the entire reason a retry
skips what is already finished.

The verification did NOT come off with the model. See cli._cmd_relaunch: the
change set is still declared, the regenerated command is still read back, and
modify.realized() still has to agree that what was asked for is what the
command says before /approve will go through.

WHY IT IS SO WILLING TO REFUSE
------------------------------
This is the one flow where deterministic code writes a config change nobody
read first. Everywhere else a person is looking at the value as they enter it.
So the preconditions below are deliberately narrow, and each returns a Refusal
naming the fact it does not have rather than a smaller version of the work:

    no remediation on record     /diagnose has not run, or found no config fix
    nothing applicable in it     the fragment names a knob this cannot write
    the run has not launched     there is nothing to retry; /modify edits it
    the run is still going       retrying now would run the same work twice
    no full step range           see scope() -- a narrowed retry is worse than
                                 no retry, and guessing the range is forbidden

Standard library only, like slots.py, gate.py, modify.py and override.py.
"""
import os
import re
import shlex

from . import gate
from . import modify
from . import override
from . import runs
from . import slots as slot_table

# Why a revision exists, recorded on it. One string, because /history reads it
# and a phrase somebody rewrites is a phrase that stops matching.
REASON = "relaunch_after_diagnosis"


class Refusal:
    """Why a retry could not be prepared, in the two parts display.problem takes.

    A class rather than a bare string because every refusal here has a second
    half worth saying -- which command to reach for instead -- and a caller that
    has to invent that half will invent a different one each time.
    """

    __slots__ = ("message", "hint")

    def __init__(self, message, hint=""):
        self.message = message
        self.hint = hint

    def __repr__(self):
        return f"<Refusal {self.message!r}>"


class Plan:
    """A retry that has been worked out but not built. Plain data.

        source      the run this is a retry OF
        override    {section: {key: value}}, already validated by
                    override.applicable -- never the raw parse
        before      what the config trace said for those same keys, or {}
        uncertain   what the diagnosis said this run does NOT establish
        skipped     settings the diagnosis proposed that were not applicable,
                    as (where, why)
        scope       the step range the retry should carry, or "" for "leave -s
                    alone because it is already every step". Never a guess --
                    see scope().
        scope_from  where `scope` came from, for the screen that shows it
    """

    __slots__ = ("source", "override", "before", "uncertain", "skipped",
                 "scope", "scope_from")

    def __init__(self, **kw):
        for slot in self.__slots__:
            setattr(self, slot, kw.get(slot))

    def __repr__(self):
        return f"<Plan of {self.source} scope={self.scope!r}>"


def scope(record):
    """The step range a retry of this run should carry, or a Refusal.

    Returns ("1-23", "somatic_fastpass's full step range, from GenPipes 6.1.1")
    or ("", "the run carries no -s, which is every step") or a Refusal.

    THE RULE IS diagnosis.RELAUNCH_RULE, applied without a model. A retry must
    cover the FULL range, not a range starting at the failure: GenPipes skips
    steps whose output is already up to date, so the full range costs nothing
    for the work that is genuinely done and is the only range that finishes the
    run. A narrowed one reruns the step that broke and stops, leaving the
    cancelled remainder undone under a run that now looks finished.

    THE POLICY IS TO WRITE THE RANGE DOWN. A retry carries `-s 1-23` even when
    the run it copies carried no -s at all. The two are the same run: omitting
    -s means every step, and `-s 1-23` for a 23-step protocol names every step,
    so the choice between them is not about what executes.

    Which is a claim, so it was measured rather than reasoned. Generating
    `dnaseq -t somatic_fastpass` against one config stack twice on GenPipes
    6.1.1 -- once with no -s, once with `-s 1-23` -- produced two scripts of
    the same size holding the same 23 STEP= lines and the same 46 JOB_NAME=
    lines, in the same order. The only differences in 165,639 bytes were the
    generation timestamp and the md5 checksums that embed it.

    Given that, the range is written because it can then be READ: the gate's
    mirror, /view and the review screen all have a steps row, and a retry whose
    scope is implied by an absent flag has nothing to put in it. The person
    approving a rerun of a 23-step pipeline should be able to see that it is 23
    steps without knowing that a missing flag means all of them.

    THREE ANSWERS, AND THE THIRD IS A REFUSAL ON PURPOSE.

      the facts have the range   slots.step_range reads it out of this GenPipes
                                 install's own step lists -- see
                                 tools/genpipes_facts.py. Version-exact, and the
                                 reason /diagnose no longer shells out to
                                 --help to learn it. This is the policy above.
      the run carries no -s      the FALLBACK, when the facts have no entry for
                                 this pipeline. The run already asks for every
                                 step, so the same selection is preserved by
                                 leaving the flag off -- the range is simply
                                 not written down, because nothing here knows
                                 it. Same run, less legible.
      neither                    the run was narrowed to `-s 4-9` and nothing
                                 here knows what the full range is. Refused.
                                 The alternatives were to keep the narrow range
                                 -- which is the half-finished run above -- or
                                 to let something guess, which is exactly what
                                 the generated facts exist to stop.
    """
    values = (record.get("proposal") or {}).get("slots") or {}
    full = slot_table.step_range(values.get("pipeline"), values.get("protocol"))
    if full:
        proto = values.get("protocol") or values.get("pipeline") or "this"
        return full, f"{proto}'s full step range, from GenPipes {_version()}"
    if not (values.get("steps") or "").strip():
        return "", "the run carries no -s, which is every step"
    return Refusal(
        f"The full step range for {values.get('pipeline')} "
        f"{values.get('protocol') or ''}".rstrip() + " is not recorded.",
        f"'{record.get('name')}' was submitted with -s {values['steps']}, and a "
        f"retry must not be narrower than the run it replaces. "
        f"/modify {record.get('name')} lets you set the range yourself.")


def _version():
    facts = slot_table._facts() or {}
    return facts.get("genpipes_version") or "the recorded facts"


def plan(record, status=None):
    """Work out the retry for `record`, or say why there is not one.

    Returns a Plan or a Refusal. Pure: reads the record, the generated facts
    and nothing else. Writes no file, calls no model, asks no scheduler -- the
    `status` it needs is passed in, because deciding whether to look is the
    caller's business and this must stay testable without one.

    `status` is a runs.RunStatus for the source run, or None when it was not
    resolved. None is treated as "no reason to think it is still going": a
    caller that did not look has not established that it is, and refusing on
    the absence of evidence would make /relaunch fail whenever the scheduler
    was unreachable.
    """
    proposal = record.get("proposal") or {}
    name = record.get("name") or "?"

    if record.get("status") in runs.BEFORE_APPROVAL:
        return Refusal(
            f"'{name}' has not been launched.",
            f"There is nothing to retry yet. /modify {name} changes it "
            f"directly, and it is still waiting at the gate.")
    if not proposal.get("generated"):
        return Refusal(
            f"'{name}' has no generation command on record.",
            "It was found on disk rather than built here, so there is nothing "
            "to copy. Describe the run you want instead.")

    found = runs.remediation_of(record)
    if not found:
        return Refusal(
            f"No diagnosed fix is on record for '{name}'.",
            f"/diagnose {name} reads the logs and works one out, or "
            f"/modify {name} lets you choose the changes yourself.")

    good, skipped = override.applicable(found["override"])
    if not good:
        why = "; ".join(f"{where} — {reason}" for where, reason in skipped)
        return Refusal(
            f"The fix recorded for '{name}' is not one this can apply.",
            (why + f". /modify {name} lets you write it yourself.") if why
            else f"/modify {name} lets you write it yourself.")

    # STILL RUNNING IS A REFUSAL, and it is the precondition most likely to
    # have changed since /diagnose printed its screen. A run with queued or
    # running jobs will go on producing output; preparing a retry of it now
    # means two runs writing into one output directory, and the second one
    # skipping steps on the strength of `.done` files the first is still
    # creating. Checked against the scheduler's own tally rather than the
    # record's status word, which says `submitted` for the whole of a run's
    # life.
    if status is not None and status.counts:
        active = sum(n for s, n in status.counts.items()
                     if s in runs.ACTIVE_STATES)
        if active:
            return Refusal(
                f"'{name}' still has {active} job(s) on the scheduler.",
                f"/check {name}. Preparing a retry now would put two runs in "
                f"one output directory.")

    picked = scope(record)
    if isinstance(picked, Refusal):
        return picked
    steps, scope_from = picked

    return Plan(source=name, override=good,
                before={s: dict(k) for s, k in (found["before"] or {}).items()},
                uncertain=list(found["uncertain"]), skipped=skipped,
                scope=steps, scope_from=scope_from)


class _Cached:
    """The scheduler tally a record already carries, in the shape plan() reads.

    Enough to keep a run whose jobs were still going at the last /check out of
    the picker, without a scheduler round-trip per candidate -- the completion
    menu redraws on every keystroke and cannot pay for one. It is the last
    KNOWN state and it says so; /relaunch <name> resolves the run properly
    before preparing anything, which is where a stale answer is caught.
    """

    __slots__ = ("counts",)

    def __init__(self, counts):
        self.counts = dict(counts or {})


def candidates(records):
    """[(record, Plan)] for the runs /relaunch would actually accept, best first.

    ONE PREDICATE, THREE INTERFACES. The bare command's picker, the tab
    completion behind `/relaunch <TAB>` and `/relaunch <name>` itself all end
    up in plan(), so a run offered by the first two cannot be refused by the
    third for a reason that was knowable when it was offered. The only thing
    that can separate them is the scheduler moving underneath, which is a fact
    about the world rather than a disagreement between two lists -- and the
    invocation path resolves the run again before it writes anything.

    ORDERED BY WHEN THE FIX WAS FOUND, newest first. `remediation.at` is
    written by registry.remember_remediation at the moment /diagnose stores the
    fix, so the run somebody has just diagnosed is the run at the top -- with
    no session state, no notion of a "current run", and nothing that has to be
    kept in step with what a conversation happens to have said. A record with
    no timestamp (the legacy proposed_override field carries none) sorts last,
    which is the honest place for a fix of unknown age.
    """
    out = []
    for record in records or ():
        cached = (record.get("last_check") or {}).get("counts")
        picked = plan(record, status=_Cached(cached) if cached else None)
        if isinstance(picked, Refusal):
            continue
        out.append((record, picked))
    out.sort(key=lambda pair: str(
        runs.remediation_of(pair[0]).get("at") or ""), reverse=True)
    return out


def summary(plan_):
    """The one line that says why a run is a candidate: `step.key  was → now`.

    Enough to recognise the run and the fix, and deliberately not the
    diagnosis: a picker showing four paragraphs per row is a screen nobody
    reads, and the whole finding is one /diagnose away.
    """
    parts = []
    for step in sorted(plan_.override):
        for key, value in sorted(plan_.override[step].items()):
            was = (plan_.before.get(step) or {}).get(key)
            parts.append(f"{step}.{key} " + (f"{was} → {value}" if was
                                             else str(value)))
    return "  ·  ".join(parts)


def prepare(plan_, record, new_name, directory):
    """Write the revision's override ini and return its change set.

    Returns `(path, changes, applied)`.

        path      the ini that was written, under the NEW run's name
        changes   the shape modify.fork_sentence() and cli._fork_run take
        applied   [(step, key, before, after)] for the review screen

    THE FILE IS THE REVISION'S OWN, always. override.path_for()'s docstring
    says why: a fork quotes its parent's command verbatim, so the parent's ini
    is already on that -c line, and tuning "the fork" through it would silently
    re-tune the run somebody went out of their way to leave alone. So this
    names the file after the new run and lets modify._deltas replace the
    inherited one on the -c stack.

    THE PARENT'S OWN OVERRIDES COME ACROSS FIRST. A run that was already tuned
    and then failed for a different reason must not lose that tuning to its
    retry -- the retry is meant to be the same run plus one change, and
    starting from an empty file would silently drop every earlier one. This is
    the same carry-over override.copy() performs for a plain fork, done through
    merge() because there is a new setting to fold in.

    Nothing here decides a value. `plan_.override` was validated by
    override.applicable and computed, upstream, from evidence.
    """
    proposal = record.get("proposal") or {}
    inherited = override.read(
        override.path_for(record.get("name"), directory, proposal))

    merged = inherited
    applied = []
    for step in sorted(plan_.override):
        settings = plan_.override[step]
        for key, value in settings.items():
            was = (plan_.before.get(step) or {}).get(key) or \
                  (inherited.get(step) or {}).get(key) or ""
            applied.append((step, key, was, value))
        merged = override.merge(merged, step, settings)

    path = os.path.join(directory or ".", f"{new_name}.override.ini")
    override.write(path, merged, run=new_name)

    changes = {modify.RESOURCES: path}
    # Only when it MOVES. Setting -s to what the command already says is a
    # change with nothing in it, and modify.sentence()/_deltas would put a line
    # in the model's instruction asking for a value that is already there.
    if plan_.scope and plan_.scope != (proposal.get("slots") or {}).get("steps"):
        changes["steps"] = plan_.scope
    return path, changes, applied


# A shell variable assignment, as one shlex token: OUTDIR=~/runs/thing.
# Anchored, so `--config=x.ini` (which starts with a dash) is not one.
_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)

# `module load mugqic/genpipes/6.1.1`, as the source block wrote it. Read from
# the block rather than assumed from runs.GENPIPES_MODULE, because a run
# generated against one version has to be retried against that version -- the
# step numbers a relaunch scope is expressed in are version-exact.
_MODULE_LOAD = re.compile(r"\bmodule\s+load\s+(\S+)")


def stack(record, ini):
    """The revision's whole -c line: the source's, with the override last.

    The parent's own override ini comes OFF, because prepare() has already
    merged its contents into the child's file -- leaving both on would stack a
    file against a copy of itself and make which one wins depend on their order.
    Everything else stays exactly as the source command wrote it, including
    `$GENPIPES_INIS` left unexpanded: the variable is set by `module load` in
    the shell that runs the command, and resolving it here would freeze one
    machine's paths into a record that has to survive being approved on
    another.

    The new file goes LAST and that is the entire point of it. See
    override.write's header, mirror's three-state resources note, and
    modify._deltas: an override that lands before the cluster ini is overruled
    by it, and the run behaves as though nobody touched anything.
    """
    proposal = (record or {}).get("proposal") or {}
    existing = gate.flag_values(gate.invocation(proposal.get("generated") or "")
                                or "", "-c")
    kept = [v for v in existing if not str(v).endswith(".override.ini")]
    return kept + ([str(ini)] if ini else [])


def command(record, plan_, ini):
    """The revision's generation block, written here rather than by a model.

    Returns `(block, script)` -- the generation and the path it writes its
    submission script to -- or a Refusal naming what it could not read.

    WHAT IT CHANGES, AND NOTHING ELSE:

        -c    the stack above -- the source's, minus the parent's override,
              plus the child's, last
        -s    plan_.scope, when it differs from what the source command says.
              See scope() for the policy and why it is not a guess.

    WHAT IT KEEPS, TOKEN FOR TOKEN. Every other flag the source wrote, in the
    position it wrote it, with its quoting intact -- gate.rewrite() does the
    editing and understands nothing about GenPipes, which is why it cannot
    helpfully drop a flag it does not recognise. `-o "$OUTDIR"` comes out still
    quoted and still unexpanded, and the assignment it depends on is carried
    across with it.

    WHAT IT REBUILDS, AND WHY IT IS NOT MORE. The source's block is a small
    shell script: an assignment or two, a mkdir, the genpipes call, and then
    echoes and listings that report on what just happened. Only the first three
    are part of generating a command; the rest is narration of a moment that
    has passed. So the block written here is

        the assignments the new invocation actually references
        mkdir -p <whatever -o says>, when the command has an -o
        module load <the source's own version> && <the new invocation>

    and the narration is not carried over. The mkdir is the one statement
    reconstructed rather than copied: it is idempotent, the source block had it,
    and without it a generation into a directory nobody has created yet fails
    for a reason that has nothing to do with the retry.

    A SOURCE WHOSE BLOCK CANNOT BE READ IS A REFUSAL. gate.invocation() returns
    "" when there is no genpipes call in the recorded text, and that is the
    honest end of the line: writing a command from scratch would mean inventing
    the flags the source used, which is the failure this whole module is
    arranged to avoid.
    """
    proposal = (record or {}).get("proposal") or {}
    block = str(proposal.get("generated") or "")
    invocation = gate.invocation(block)
    if not invocation:
        return Refusal(
            f"The recorded command for '{record.get('name')}' has no genpipes "
            f"call in it.",
            f"There is nothing to copy the retry from. /modify "
            f"{record.get('name')} builds one from a description instead.")

    edits = {"-c": stack(record, ini)}
    if plan_.scope != (gate.flag_value(invocation, "-s") or ""):
        edits["-s"] = plan_.scope or None
    rewritten = gate.rewrite(invocation, edits)
    if not rewritten:
        return Refusal(
            f"The recorded command for '{record.get('name')}' could not be "
            f"rewritten.",
            f"/modify {record.get('name')} builds one from a description "
            f"instead.")

    # WHERE THE SCRIPT GOES, read back off the command rather than chosen.
    # `-g` is kept exactly as the source wrote it, so a revision writes its
    # script the same way the run it copies did; /approve resolves it against
    # the revision's own workdir, which is what keeps the two files apart.
    script = gate.flag_value(rewritten, "-g")
    if not script:
        return Refusal(
            f"The recorded command for '{record.get('name')}' does not say "
            f"where it writes its script.",
            f"Without a -g there is nothing for /approve to run. /modify "
            f"{record.get('name')} builds one instead.")

    lines = []
    for name, value in _assignments(block):
        if re.search(r"\$\{?" + re.escape(name) + r"\b", rewritten):
            lines.append(f"{name}={value}")
    out = gate.flag_value(rewritten, "-o")
    if out:
        lines.append(f"mkdir -p {out}")
    found = _MODULE_LOAD.search(block)
    module = found.group(1) if found else runs.GENPIPES_MODULE
    lines.append(f"module load {module} && {rewritten}")
    return "#!BASH\n" + "\n".join(lines), script


def _assignments(block):
    """[(name, value)] for the shell assignments in a recorded block, in order.

    Deliberately narrow. This is not a shell parser and must not become one:
    it reads whole tokens of the form NAME=value and takes the FIRST value each
    name is given, which is what the genpipes call further down the block saw.
    Anything more -- conditionals, command substitution, a value that is itself
    computed -- is not carried across, and a command that depends on one is
    caught by the caller: the variable it references has no assignment here, so
    the block written names it unset and GenPipes refuses at generation, before
    anybody is asked to approve anything.
    """
    try:
        tokens = shlex.split(str(block or ""), comments=False, posix=False)
    except ValueError:
        tokens = str(block or "").split()
    seen, out = set(), []
    for token in tokens:
        found = _ASSIGNMENT.match(token)
        if found and found.group(1) not in seen:
            seen.add(found.group(1))
            out.append((found.group(1), found.group(2)))
    return out


def declaration(plan_, record, changes, directory=None):
    """What the regenerated command must be TRUE OF, in gate.DECLARABLE's shape.

    modify.declaration() covers the flag rows and deliberately skips
    `resources`, on the grounds that it changes a file rather than a command
    and "neither is checkable against a command". That is right where it is
    written -- the resources row can be a re-tune of an ini already on the -c
    line, which changes no command at all -- and it is not right here.

    A relaunch knows the ini's path before the model is asked, and knows it is
    not on the old command's -c stack, so "this file is on the -c line of what
    comes back" is a claim about one command, checkable by modify.realized()
    the same way any other -c addition is. Declaring it is what stops the one
    flow that writes a config change unattended from also being the one flow
    that never checks the change arrived.

    WHAT IT DOES NOT CLAIM: the ini's POSITION. gate.DECLARABLE's `add` is set
    membership, and being LAST on -c -- which is the whole reason an override
    works -- has no declarable form. The instruction to the model says it (see
    modify._deltas) and the gate's mirror prints the stack in order, so it is
    stated and it is visible; it is not machine-verified, and this docstring is
    where that gap is written down rather than implied.
    """
    declared = modify.declaration(record.get("proposal") or {}, changes,
                                  directory)
    ini = changes.get(modify.RESOURCES)
    if ini:
        declared.append({"field": modify.CONFIG, "operation": "add",
                         "value": str(ini)})
    return declared
