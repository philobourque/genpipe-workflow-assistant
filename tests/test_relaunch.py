"""/relaunch: a diagnosis turned into a prepared retry, and never into a submission.

WHAT THIS SUITE IS ACTUALLY GUARDING.

/relaunch is the only flow in the product where deterministic code writes a
config change that nobody read first. Everywhere else -- the resources row, a
prose /modify -- a person is looking at the value as it goes in. Here a model
proposed it, code validated it, and the next screen anybody sees already has it
applied. Two properties therefore have to hold for the command to be defensible
at all, and both are asserted from several directions below:

  IT PREPARES, IT DOES NOT SUBMIT     the run it produces is held, and nothing
                                      in this file's call path can reach a
                                      scheduler. See the fake-cluster suites for
                                      the submission half of the lifecycle.
  THE ORIGINAL IS NOT TOUCHED         not its command, its -c stack, its job
                                      list, its submission record or its job
                                      ids. There is a whole section on this,
                                      comparing a deep copy of the record taken
                                      before the retry was prepared.

And one that is about honesty rather than safety: the fix travels WITH the
things the diagnosis said it did not establish. A walltime raised to a value
nothing proved sufficient is exactly as unproven on the review screen as it was
on the diagnosis, and the review screen is where somebody acts on it.

EVERYTHING HERE IS SYNTHETIC EXCEPT THE STEP RANGE. Run names, readsets and
paths are invented. The dnaseq/somatic_fastpass range is real, read from
genpipe/genpipes_facts.json, because "the range comes from the generated facts
and not from a model" is one of the claims under test and a fake range would
prove nothing about it.

Standard library only. No cluster, no model.
"""
import copy
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import Report
from genpipe import diagnosis
from genpipe import gate
from genpipe import modify
from genpipe import override
from genpipe import relaunch
from genpipe import runs
from genpipe import slots


COMMAND = (
    "genpipes dnaseq -t somatic_fastpass "
    "-c $GENPIPES_INIS/dnaseq/dnaseq.base.ini "
    "$GENPIPES_INIS/dnaseq/dnaseq.cancer.ini "
    "$GENPIPES_INIS/common_ini/rorqual.ini "
    "$GENPIPES_INIS/dnaseq/cit.ini "
    "-r /data/readset.txt -p /data/pairs.csv -j slurm -g cmd.sh"
)


def record(workdir, **over):
    """A launched dnaseq run with a diagnosed walltime fix on it."""
    base = {
        "name": "study-0805",
        "status": runs.SUBMITTED,
        "workdir": workdir,
        "job_list": os.path.join(workdir, "Dna.somatic_fastpass.job_list.T0"),
        "proposal": {
            "command": "bash cmd.sh",
            "generated": "#!BASH\n" + COMMAND,
            "slots": {"pipeline": "dnaseq", "protocol": "somatic_fastpass",
                      "steps": None, "readset": "/data/readset.txt",
                      "pairs": "/data/pairs.csv",
                      "inis": ["dnaseq.base.ini", "dnaseq.cancer.ini",
                               "rorqual.ini", "cit.ini"]},
        },
        "remediation": {
            "override": {"gatk_sam_to_fastq": {"cluster_walltime": "35:00:00"}},
            "before": {"gatk_sam_to_fastq": {"cluster_walltime": "0:10:00"}},
            "uncertain": ["whether 35:00:00 is sufficient for this input",
                          "why this input required more than ten minutes"],
            "source": "diagnose", "at": "2026-08-24T09:00:00",
        },
    }
    base.update(over)
    return base


class FakeStatus:
    """Just the two fields relaunch.plan reads. Not runs.RunStatus, on purpose:
    a suite that builds the real object would pass if plan() started reading a
    third field it has no business reading."""

    def __init__(self, counts):
        self.counts = counts


def main():
    r = Report("relaunch — preparing a retry, and refusing to")
    work = tempfile.mkdtemp(prefix="relaunch-")
    try:
        # ---------------------------------------------------------------- #
        r.section("the structured remediation contract already existed")
        # The claim being checked is that /relaunch reads DATA, never the
        # rendered prose. diagnosis.parse has produced this shape since the
        # OVERRIDE heading was added; this asserts the shape rather than
        # trusting it.
        parsed = diagnosis.parse(
            "MANNER: TIMEOUT.\n"
            "CAUSE: it ran out of walltime.\n"
            "FIX: raise cluster_walltime in [gatk_sam_to_fastq].\n"
            "OVERRIDE:\n[gatk_sam_to_fastq]\ncluster_walltime = 35:00:00\n"
            "RELAUNCH: -s 1-23\n"
            "UNCERTAIN:\n- whether 35:00:00 is enough\n")
        r.equal("section, key and value are separate fields",
                parsed["override"],
                {"gatk_sam_to_fastq": {"cluster_walltime": "35:00:00"}})
        r.equal("and the uncertainty is a list beside them",
                parsed["uncertain"], ["whether 35:00:00 is enough"])

        r.section("what the program will and will not apply on its own")
        good, refused = override.applicable(parsed["override"])
        r.equal("a cluster walltime it knows", good, parsed["override"])
        r.equal("nothing refused", refused, [])
        good, refused = override.applicable(
            {"gatk_sam_to_fastq": {"cluster_walltime": "30"}})
        r.equal("a bare number is not a walltime, so nothing is applied", good, {})
        r.check("and the reason is reported rather than swallowed",
                refused and "walltime" in refused[0][1], refused)
        good, refused = override.applicable({"step": {"invented_key": "7"}})
        r.equal("a knob it cannot write is not written", good, {})
        r.contains("named in the refusal", refused[0][0], "invented_key")
        good, refused = override.applicable(
            {"step": {"cluster_walltime": "35:00:00", "invented_key": "7"}})
        r.equal("a partly-understood fragment keeps only the understood half",
                good, {"step": {"cluster_walltime": "35:00:00"}})
        r.equal("and still reports the rest", len(refused), 1)
        good, refused = override.applicable({"not a section!": {"ram": "4G"}})
        r.equal("a section name that cannot be an ini header is refused",
                good, {})

        r.section("the old proposed_override field still reads")
        r.equal("legacy records keep working",
                runs.remediation_of(
                    {"proposed_override": {"s": {"ram": "8G"}}})["override"],
                {"s": {"ram": "8G"}})
        r.equal("with an empty uncertainty list, which means 'not recorded'",
                runs.remediation_of(
                    {"proposed_override": {"s": {"ram": "8G"}}})["uncertain"], [])
        r.equal("an empty override is no remediation at all",
                runs.remediation_of({"remediation": {"override": {}}}), {})
        r.equal("and a record with neither", runs.remediation_of({}), {})

        # ---------------------------------------------------------------- #
        r.section("the relaunch scope is a fact, not a model's answer")
        rec = record(work)
        got, where = relaunch.scope(rec)
        r.equal("the full protocol range comes out of the generated facts",
                got, slots.step_range("dnaseq", "somatic_fastpass"))
        r.contains("and says which protocol it is the range of",
                   where, "somatic_fastpass")
        r.check("which is a real range and not a placeholder",
                got and got.startswith("1-") and int(got.split("-")[1]) > 1, got)

        unknown = record(work)
        unknown["proposal"]["slots"] = dict(unknown["proposal"]["slots"],
                                            pipeline="notapipeline",
                                            protocol="notaprotocol")
        got, where = relaunch.scope(unknown)
        r.equal("a pipeline with no recorded steps and no -s leaves -s alone",
                got, "")
        r.contains("because omitting it already means every step",
                   where, "every step")

        narrowed = record(work)
        narrowed["proposal"]["slots"] = dict(narrowed["proposal"]["slots"],
                                             pipeline="notapipeline",
                                             protocol="notaprotocol",
                                             steps="4-9")
        refusal = relaunch.scope(narrowed)
        r.check("but a NARROWED run with no known full range is refused",
                isinstance(refusal, relaunch.Refusal), refusal)
        r.contains("naming the missing fact", refusal.message, "not recorded")
        r.contains("and offering the manual route", refusal.hint, "/modify")
        r.check("rather than silently keeping the narrow range",
                "4-9" in refusal.hint)

        # ---------------------------------------------------------------- #
        r.section("planning a retry for a failed run")
        plan = relaunch.plan(record(work))
        r.check("a plan comes back", isinstance(plan, relaunch.Plan), plan)
        r.equal("carrying the validated override", plan.override,
                {"gatk_sam_to_fastq": {"cluster_walltime": "35:00:00"}})
        r.equal("the deterministic scope", plan.scope, "1-23")
        r.equal("what the trace said before", plan.before,
                {"gatk_sam_to_fastq": {"cluster_walltime": "0:10:00"}})
        r.check("and everything the diagnosis did not establish",
                len(plan.uncertain) == 2 and "sufficient" in plan.uncertain[0],
                plan.uncertain)

        # ---------------------------------------------------------------- #
        r.section("when a retry must not be prepared")
        held = record(work, status=runs.HELD)
        ref = relaunch.plan(held)
        r.check("a run still at the gate", isinstance(ref, relaunch.Refusal))
        r.contains("is not something to retry", ref.message, "not been launched")
        r.contains("and /modify is the verb for it", ref.hint, "/modify")

        bare = record(work)
        bare["remediation"] = None
        ref = relaunch.plan(bare)
        r.check("no diagnosis on record", isinstance(ref, relaunch.Refusal))
        r.contains("says so plainly", ref.message, "No diagnosed fix")
        r.contains("and names /diagnose", ref.hint, "/diagnose")
        r.contains("as well as the manual alternative", ref.hint, "/modify")

        unusable = record(work)
        unusable["remediation"] = dict(
            unusable["remediation"],
            override={"gatk_sam_to_fastq": {"invented_key": "7"}})
        ref = relaunch.plan(unusable)
        r.check("a fix naming a knob this cannot write",
                isinstance(ref, relaunch.Refusal))
        r.contains("is refused as inapplicable", ref.message, "not one this can apply")
        r.contains("with the reason", ref.hint, "invented_key")

        scanned = record(work)
        scanned["proposal"] = {"slots": {}, "generated": ""}
        r.check("a run discovered on disk has nothing to copy from",
                isinstance(relaunch.plan(scanned), relaunch.Refusal))

        ref = relaunch.plan(record(work),
                            status=FakeStatus({"RUNNING": 3, "COMPLETED": 9}))
        r.check("a run with jobs still on the scheduler",
                isinstance(ref, relaunch.Refusal), ref)
        r.contains("is refused while they are running", ref.message, "3 job(s)")
        r.contains("because two runs would share one output directory",
                   ref.hint, "output directory")
        r.check("a finished tally does not refuse",
                isinstance(relaunch.plan(record(work),
                                         status=FakeStatus({"TIMEOUT": 1,
                                                            "CANCELLED": 32})),
                           relaunch.Plan))
        r.check("and neither does an unresolved one -- absence of evidence "
                "is not evidence",
                isinstance(relaunch.plan(record(work), status=None),
                           relaunch.Plan))

        # ---------------------------------------------------------------- #
        r.section("preparing: the file that is written, and whose it is")
        rec = record(work)
        plan = relaunch.plan(rec)
        path, changes, applied = relaunch.prepare(plan, rec, "study-0805-2", work)
        r.equal("the ini is named after the REVISION, never the original",
                os.path.basename(path), "study-0805-2.override.ini")
        r.check("the original's ini was not created or touched",
                not os.path.exists(os.path.join(work,
                                                "study-0805.override.ini")))
        written = override.read(path)
        r.equal("and it carries the diagnosed setting", written,
                {"gatk_sam_to_fastq": {"cluster_walltime": "35:00:00"}})
        r.contains("with a header saying it goes last",
                   open(path).read(), "Goes LAST on the -c line")
        r.equal("the change set is one the fork machinery already takes",
                sorted(changes), sorted([modify.RESOURCES, "steps"]))
        r.equal("the step range is the deterministic one",
                changes["steps"], "1-23")
        r.equal("the review rows say what moved", applied,
                [("gatk_sam_to_fastq", "cluster_walltime", "0:10:00",
                  "35:00:00")])

        r.section("tuning the original already had is carried forward, not lost")
        rec = record(work)
        rec["proposal"] = copy.deepcopy(rec["proposal"])
        rec["proposal"]["generated"] += " study-0805.override.ini"
        parent_ini = os.path.join(work, "study-0805.override.ini")
        override.write(parent_ini, {"picard_sam_to_fastq": {"ram": "40G"},
                                    "gatk_sam_to_fastq": {"cluster_cpu": "8"}},
                       run="study-0805")
        before = open(parent_ini).read()
        plan = relaunch.plan(rec)
        path, changes, applied = relaunch.prepare(plan, rec, "study-0805-3", work)
        written = override.read(path)
        r.equal("an unrelated step's tuning survives",
                written.get("picard_sam_to_fastq"), {"ram": "40G"})
        r.equal("a sibling key on the SAME step survives",
                written["gatk_sam_to_fastq"]["cluster_cpu"], "8")
        r.equal("beside the new setting",
                written["gatk_sam_to_fastq"]["cluster_walltime"], "35:00:00")
        r.equal("and the parent's own file is byte-identical afterwards",
                open(parent_ini).read(), before)

        r.section("the -s row is only touched when it moves")
        already = record(work)
        already["proposal"] = copy.deepcopy(already["proposal"])
        already["proposal"]["slots"]["steps"] = "1-23"
        plan = relaunch.plan(already)
        _, changes, _ = relaunch.prepare(plan, already, "study-0805-4", work)
        r.check("a run already carrying the full range asks for no -s change",
                "steps" not in changes, changes)

        # ---------------------------------------------------------------- #
        r.section("what the regenerated command is checked against")
        rec = record(work)
        plan = relaunch.plan(rec)
        path, changes, _ = relaunch.prepare(plan, rec, "study-0805-5", work)
        declared = relaunch.declaration(plan, rec, changes, work)
        fields = {(d["field"], d["operation"]) for d in declared}
        r.check("the step range is declared", ("steps", "set") in fields, fields)
        r.check("and so is the override ini's arrival on -c",
                (modify.CONFIG, "add") in fields, fields)
        # The verifier's own answer, over a command that did what was asked and
        # one that did not. This is the existing declared-change machinery --
        # nothing weaker was built for this path.
        after = {"slots": dict(rec["proposal"]["slots"], steps="1-23",
                               inis=["dnaseq.base.ini", "dnaseq.cancer.ini",
                                     "rorqual.ini", "cit.ini", path])}
        got = modify.realized(declared, after, work)
        r.equal("a command that honoured both passes on -s",
                got.get("steps"), modify.APPLIED)
        r.equal("and on -c", got.get(modify.CONFIG), modify.APPLIED)
        dropped = {"slots": dict(rec["proposal"]["slots"], steps="1-23")}
        got = modify.realized(declared, dropped, work)
        r.equal("a command that left the override off is caught",
                got.get(modify.CONFIG), modify.IGNORED)
        wrong = {"slots": dict(rec["proposal"]["slots"], steps="7-9",
                               inis=["dnaseq.base.ini", path])}
        got = modify.realized(declared, wrong, work)
        r.equal("so is one that narrowed the range",
                got.get("steps"), modify.IGNORED)

        # ---------------------------------------------------------------- #
        r.section("the original is not modified, in any field that matters")
        rec = record(work)
        frozen = copy.deepcopy(rec)
        plan = relaunch.plan(rec)
        relaunch.prepare(plan, rec, "study-0805-6", work)
        relaunch.declaration(plan, rec, {modify.RESOURCES: "x"}, work)
        r.equal("the whole record is unchanged, field for field",
                json.dumps(rec, sort_keys=True),
                json.dumps(frozen, sort_keys=True))
        for field in ("job_list", "status", "workdir"):
            r.equal(f"in particular {field}", rec[field], frozen[field])
        r.equal("and the generated command it was submitted with",
                rec["proposal"]["generated"], frozen["proposal"]["generated"])
        r.equal("including its -c stack",
                rec["proposal"]["slots"]["inis"],
                frozen["proposal"]["slots"]["inis"])

        # ---------------------------------------------------------------- #
        r.section("the plan is pure: no writes, no scheduler, no model")
        listing = sorted(os.listdir(work))
        relaunch.plan(record(work))
        relaunch.plan(record(work), status=FakeStatus({"TIMEOUT": 1}))
        r.equal("planning twice left the directory exactly as it was",
                sorted(os.listdir(work)), listing)

        # ---------------------------------------------------------------- #
        r.section("a revision's name is allocated, never asked for")
        store = runs.Registry(work)
        store.hold("study-0805", "t1", record(work)["proposal"], work)
        r.equal("the first revision takes the registry's own convention",
                store.unique_name("study-0805"), "study-0805-2")
        store.hold("study-0805-2", "t2", record(work)["proposal"], work)
        r.equal("and a collision moves to the next one",
                store.unique_name("study-0805"), "study-0805-3")
        r.check("which is never the original",
                store.unique_name("study-0805") != "study-0805")

        r.section("lineage is recorded on the revision, never on the original")
        store.derive("study-0805-2", "study-0805", relaunch.REASON)
        child = store.get("study-0805-2")
        parent = store.get("study-0805")
        r.equal("the child points at the parent",
                child.get("derived_from"), "study-0805")
        r.equal("and says why", child.get("derived_reason"),
                "relaunch_after_diagnosis")
        r.check("the parent gained nothing",
                "derived_from" not in parent and "derived_reason" not in parent,
                sorted(parent))
        r.equal("the revision is waiting for approval, like any other",
                child["status"], runs.HELD)
        r.equal("with no job list", child.get("job_list"), None)
        r.equal("and nothing submitted", child.get("submitted_at"), None)

        r.section("the revision's command is written, not asked for")
        # THE POINT OF THE WHOLE DETERMINISTIC PATH. Every assertion here is
        # about a command produced without an inference: there is no model in
        # this file, and there is none in relaunch.command either.
        rec = record(work)
        plan = relaunch.plan(rec)
        ini = os.path.join(work, "study-0805-2.override.ini")
        built = relaunch.command(rec, plan, ini)
        r.check("a command came back, not a refusal",
                not isinstance(built, relaunch.Refusal), built)
        block, script = built
        call = gate.invocation(block)

        r.equal("the pipeline is the source's", gate.flag_value(call, "-t"),
                "somatic_fastpass")
        for flag in ("-r", "-p", "-j", "-g"):
            r.equal(f"{flag} is copied through untouched",
                    gate.flag_value(call, flag),
                    gate.flag_value(COMMAND, flag))
        r.equal("the script is read back off the command it wrote",
                script, gate.flag_value(COMMAND, "-g"))

        stack = gate.flag_values(call, "-c")
        r.equal("the source's whole -c stack survives, in order",
                stack[:-1], gate.flag_values(COMMAND, "-c"))
        r.equal("with the override appended LAST, where it wins",
                stack[-1], ini)
        r.check("and $GENPIPES_INIS is left unexpanded",
                all("$GENPIPES_INIS" in v for v in stack[:-1]), stack)

        r.equal("the deterministic range is on the command",
                gate.flag_value(call, "-s"),
                slots.step_range("dnaseq", "somatic_fastpass"))
        r.check("the source carried no -s at all",
                gate.flag_value(COMMAND, "-s") is None)

        r.section("a rewritten command keeps everything nobody changed")
        # gate.rewrite is the writer that mirrors flag_value/flag_values. It
        # understands nothing about GenPipes, which is what stops it dropping a
        # flag it does not recognise -- the failure the model's version had.
        source = ('module load mugqic/genpipes/6.1.1 && genpipes dnaseq '
                  '-t somatic_fastpass -c a.ini b.ini -r /r.txt --force '
                  '-o "$OUTDIR" -g cmd.sh 2>&1')
        r.equal("no edits means the same call back",
                gate.rewrite(source, {}), gate.invocation(source))
        moved = gate.rewrite(source, {"-c": ["a.ini", "b.ini", "own.ini"],
                                      "-s": "1-9"})
        r.contains("a flag it was never given survives", moved, "--force")
        r.contains("and so does the quoting on a variable it must not expand",
                   moved, '-o "$OUTDIR"')
        r.check("a replaced flag keeps its position",
                moved.index("-c") < moved.index("-r"), moved)
        r.check("a new flag lands before the trailing redirection",
                moved.index("-s") < moved.index("2>&1"), moved)
        r.equal("and a flag can be taken off entirely",
                gate.flag_value(gate.rewrite(source, {"-o": None}), "-o"), None)
        r.equal("text with no genpipes call in it is no opinion",
                gate.rewrite("echo hello", {"-s": "1-2"}), "")

        r.section("the block carries what the command it wrote depends on")
        # The defect the model's regeneration had on the real run: it rebuilt
        # the genpipes call and dropped the assignment `-o "$OUTDIR"` needed,
        # so the retry would have written somewhere other than the directory
        # holding the .done files that make a retry cheap.
        wrapped = record(work)
        wrapped["proposal"]["generated"] = (
            '#!BASH OUTDIR=/scratch/out mkdir -p "$OUTDIR" cd /elsewhere && \\ '
            + COMMAND.replace("-g cmd.sh", '-o "$OUTDIR" -g cmd.sh')
            + ' 2>&1 | tail -8 echo "done" ls -l "$OUTDIR"')
        block2, _ = relaunch.command(wrapped, relaunch.plan(wrapped), ini)
        r.contains("the assignment comes across", block2, "OUTDIR=/scratch/out")
        r.contains("so the -o it feeds still means something", block2,
                   '-o "$OUTDIR"')
        r.contains("the directory is made, as the source block made it",
                   block2, 'mkdir -p "$OUTDIR"')
        r.contains("and the module the source loaded is the one reloaded",
                   block2, "module load mugqic/genpipes/6.1.1")
        for narration in ("tail -8", "echo", "ls -l", "cd /elsewhere"):
            r.check(f"{narration!r} is not carried over -- it reported on a "
                    f"moment that has passed", narration not in block2, block2)

        r.section("a command it cannot read is a refusal, not an invention")
        blind = record(work)
        blind["proposal"]["generated"] = "#!BASH\necho nothing to see"
        r.check("no genpipes call means no retry",
                isinstance(relaunch.command(blind, plan, ini),
                           relaunch.Refusal))
        gone = record(work)
        gone["proposal"]["generated"] = "#!BASH\n" + COMMAND.replace(
            " -g cmd.sh", "")
        r.check("and nor does a command that never says where its script goes",
                isinstance(relaunch.command(gone, relaunch.plan(gone), ini),
                           relaunch.Refusal))

        r.section("which runs /relaunch would actually accept")
        # ONE PREDICATE FOR THREE INTERFACES. The bare command's picker, the
        # completion behind /relaunch <TAB> and /relaunch <name> all decide
        # eligibility with plan(), so nothing can be offered that would then be
        # refused for a reason that was knowable when it was offered.
        eligible = record(work, name="ready-0805")
        r.equal("a launched run with an applicable diagnosed fix is a candidate",
                [rec["name"] for rec, _ in relaunch.candidates([eligible])],
                ["ready-0805"])

        undiagnosed = record(work, name="never-diagnosed")
        undiagnosed.pop("remediation")
        r.equal("a failed run nobody diagnosed is not",
                relaunch.candidates([undiagnosed]), [])

        unwritable = record(work, name="not-ours")
        unwritable["remediation"] = dict(
            unwritable["remediation"],
            override={"gatk_sam_to_fastq": {"module_version": "2.1"}})
        r.equal("nor is a fix naming a knob this cannot write",
                relaunch.candidates([unwritable]), [])

        waiting = record(work, name="still-held", status=runs.HELD)
        r.equal("nor a run that has not been launched",
                relaunch.candidates([waiting]), [])

        adopted = record(work, name="from-disk")
        adopted["proposal"] = {"slots": {}, "generated": ""}
        r.equal("nor one with no command on record",
                relaunch.candidates([adopted]), [])

        busy = record(work, name="still-going")
        busy["last_check"] = {"counts": {"RUNNING": 2, "COMPLETED": 4},
                              "total": 6}
        r.equal("nor one whose jobs were still on the scheduler last time we "
                "looked", relaunch.candidates([busy]), [])
        settled = record(work, name="all-stopped")
        settled["last_check"] = {"counts": {"TIMEOUT": 1, "CANCELLED": 3},
                                 "total": 4}
        r.equal("while a run whose jobs have all stopped is offered",
                [rec["name"] for rec, _ in relaunch.candidates([settled])],
                ["all-stopped"])

        r.section("the run just diagnosed comes first")
        # NO SESSION STATE. The order comes from when the fix was RECORDED --
        # registry.remember_remediation stamps it -- so the run somebody has
        # just diagnosed rises to the top on the strength of the registry
        # alone, and nothing has to be kept in step with a conversation.
        older = record(work, name="diagnosed-first")
        older["remediation"] = dict(older["remediation"], at="2026-08-01T09:00:00")
        newer = record(work, name="diagnosed-just-now")
        newer["remediation"] = dict(newer["remediation"], at="2026-08-25T11:00:00")
        legacy = record(work, name="legacy-field")
        legacy.pop("remediation")
        legacy["proposed_override"] = {"gatk_sam_to_fastq":
                                       {"cluster_walltime": "35:00:00"}}
        order = [rec["name"] for rec, _ in
                 relaunch.candidates([older, legacy, newer])]
        r.equal("newest diagnosis first, undated last", order,
                ["diagnosed-just-now", "diagnosed-first", "legacy-field"])

        r.section("what a candidate row says about itself")
        _, picked = relaunch.candidates([eligible])[0]
        line = relaunch.summary(picked)
        r.contains("the step and setting", line, "gatk_sam_to_fastq.cluster_walltime")
        r.contains("what it is now", line, "0:10:00")
        r.contains("and what it would become", line, "35:00:00")
        r.check("and not the whole diagnosis", len(line) < 120, line)
        bare = record(work, name="no-baseline")
        bare["remediation"] = dict(bare["remediation"], before={})
        r.check("with no arrow when there is nothing to point from",
                "→" not in relaunch.summary(relaunch.candidates([bare])[0][1]))

        r.section("a candidate that goes stale is refused, not retried")
        # THE ONE THING THE PICKER CANNOT PROMISE. It reads the tally the
        # record already carries; the scheduler can move between a list being
        # drawn and a row being chosen. /relaunch <name> resolves the run again
        # and plan() refuses on the fresh answer, which is what makes the
        # difference a refusal rather than two runs in one output directory.
        listed = relaunch.candidates([eligible])
        r.equal("listed while its jobs were all stopped",
                [rec["name"] for rec, _ in listed], ["ready-0805"])

        class Restarted:
            counts = {"RUNNING": 1}
        refused = relaunch.plan(eligible, status=Restarted())
        r.check("but planned against a fresh status it refuses",
                isinstance(refused, relaunch.Refusal), refused)
        r.contains("naming what changed", refused.message, "on the scheduler")

        r.section("discovery costs nothing")
        # Structural rather than measured: this module imports the registry,
        # the facts table and the override writer, and nothing that can reach a
        # model or a scheduler. A candidate list is a read of records already
        # in hand.
        source = open(relaunch.__file__).read()
        for forbidden in ("llm", "invoke(", "subprocess", "sacct", "squeue"):
            r.check(f"nothing in relaunch.py mentions {forbidden!r}",
                    forbidden not in source)
        r.equal("and an empty registry has no candidates",
                relaunch.candidates([]), [])

        r.section("the remediation round-trips through the registry")
        store.remember_remediation(
            "study-0805", {"gatk_sam_to_fastq": {"cluster_walltime": "35:00:00"}},
            ["whether 35:00:00 is sufficient"],
            before={"gatk_sam_to_fastq": {"cluster_walltime": "0:10:00"}})
        found = runs.remediation_of(store.get("study-0805"))
        r.equal("the fix", found["override"],
                {"gatk_sam_to_fastq": {"cluster_walltime": "35:00:00"}})
        r.equal("the caveat that travels with it", found["uncertain"],
                ["whether 35:00:00 is sufficient"])
        r.equal("and the baseline the review screen prints",
                found["before"],
                {"gatk_sam_to_fastq": {"cluster_walltime": "0:10:00"}})
        r.equal("an empty fix writes nothing",
                store.remember_remediation("study-0805", {}).get("remediation"),
                store.get("study-0805")["remediation"])
    finally:
        shutil.rmtree(work, ignore_errors=True)

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
