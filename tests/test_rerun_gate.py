#!/usr/bin/env python
"""A conversational rerun goes through the same verification /modify does.

THE RUN THIS SUITE IS ABOUT. Somebody typed

    I want you to rerun Test_walltimefail, while removing override_walltime.ini

and got back a command that still carried override_walltime.ini -- and then a
`submitted` run. Nothing in the path from the sentence to the scheduler was in a
position to notice, because the one mechanism that reports a change as IGNORED
(modify.compare) only fires for rows something told it were REQUESTED, and the
only thing that ever told it was cli.py's /modify panel. Prose reached the gate
with an empty request set.

Drives the REAL graph -- the actual GenpipeA1, the actual LangGraph interrupt,
the actual registry -- with a scripted model, because what is being proved is
the mechanism and not a description of it.

WHAT THE SCRIPT STANDS IN FOR. The model reads the sentence and decides it means
"take that ini off the -c stack". That decision is stubbed here, deliberately:
this suite must not be able to pass because some deterministic code recognised
the word "removing". The stub emits the same structured declaration a real model
would, and everything downstream of it is the real thing.

Run:  GENPIPE_FAKE=1 python tests/test_rerun_gate.py
"""
import copy
import os
import pathlib
import shutil
import tempfile

from harness import (Report, ScriptedLLM, execute_block, solution,
                     submission_environment)

from genpipe import cli
from genpipe import display
from genpipe import fakecluster
from genpipe import gate
from genpipe import modify
from genpipe import override
from genpipe import relaunch
from genpipe import runs as runs_store
from genpipe import slots
from genpipe.cli import build_agent


STACK = "rnaseq.base.ini common_ini/rorqual.ini override_walltime.ini"
GEN = ("module load mugqic/genpipes/6.1.1 && genpipes rnaseq -t stringtie "
       f"-s 1-5 -c {STACK} -r readset.tsv -d design.tsv -g cmd.sh")
# The rerun the model produces. Same everything, minus the override ini.
GEN_WITHOUT = GEN.replace(" override_walltime.ini", "")

# What the model says it did. THE MODEL'S OWN WORDS, structured -- field,
# operation and value kept apart so the application can check the claim rather
# than pattern-match a string.
DECLARES_REMOVAL = execute_block(
    'propose_submission("cmd.sh", changes=[',
    '    {"field": "config", "operation": "remove",',
    '     "value": "override_walltime.ini"}])')


def fixtures(work):
    os.makedirs(os.path.join(work, "common_ini"), exist_ok=True)
    for path in ("rnaseq.base.ini", "override_walltime.ini",
                 os.path.join("common_ini", "rorqual.ini")):
        open(os.path.join(work, path), "w").write(
            "[DEFAULT]\ncluster_server=rorqual\n")
    open(os.path.join(work, "readset.tsv"), "w").write("Sample\tReadset\tLibrary\n")
    open(os.path.join(work, "design.tsv"), "w").write("Sample\tContrast\n")


def fixture(work, script):
    agent = build_agent(path=work)
    agent.use_tool_retriever = False
    agent.llm = ScriptedLLM(script)
    return agent


def main():
    submission_environment()
    r = Report("a rerun is verified like a /modify")
    work = tempfile.mkdtemp(prefix="rerun-")
    envdir = tempfile.mkdtemp(prefix="rerun-env-")
    cli.ENV_PATH = pathlib.Path(envdir) / ".env"
    here = os.getcwd()
    display.VERBOSE = False
    try:
      with fakecluster.session("happy"):
        os.chdir(work)
        fixtures(work)

        # ============================================================== #
        r.section("the reported failure: a declared removal that did not happen")
        # The model declares the removal and then regenerates the command
        # UNCHANGED -- which is exactly what happened, and what nothing caught.
        agent = fixture(work, [
            execute_block(f"#!BASH\n{GEN}"),
            DECLARES_REMOVAL,
            solution("Rerun without the walltime override."),
        ])
        status = agent.run("rerun it without override_walltime.ini", "chat-a")
        name = status.get("name")
        r.truthy("a run reached the gate", name)

        record = agent.registry.get(name)
        stack = record["proposal"]["slots"]["inis"]
        r.check("the -c stack in the record is the whole stack",
                "override_walltime.ini" in stack, stack)
        r.equal("and it is recorded exactly as written, in order", stack,
                STACK.split())

        r.check("the model's declaration is on the proposal",
                record["proposal"].get("declared"), record["proposal"])
        r.equal("the gate reports the change as NOT applied",
                (record.get("changed") or {}).get("config"), modify.IGNORED)

        # THE SAFETY PROPERTY. This is the step that used to go straight to
        # `submitted`.
        status = agent.resume(name, approved=True)
        r.equal("/approve is refused the first time", status.get("submitted"),
                False)
        r.equal("naming the row that did not move", status.get("ignored"),
                ["config"])
        r.equal("and nothing was submitted",
                agent.registry.get(name)["status"], runs_store.HELD)

        # Refused ONCE, not forever: submitting it as it stands is a legitimate
        # thing to want, and approval is still typed -- now by somebody who has
        # been told what they would otherwise have missed.
        status = agent.resume(name, approved=True)
        r.check("a second /approve goes through",
                status.get("submitted") is not False)
        r.check("and the run leaves the held state",
                agent.registry.get(name)["status"] != runs_store.HELD)

        # ============================================================== #
        r.section("the same declaration, honoured")
        agent = fixture(work, [
            execute_block(f"#!BASH\n{GEN_WITHOUT}"),
            DECLARES_REMOVAL,
            solution("Rerun without the walltime override."),
        ])
        status = agent.run("rerun it without override_walltime.ini", "chat-b")
        name_b = status.get("name")
        record = agent.registry.get(name_b)
        r.check("the ini really is off the stack",
                "override_walltime.ini" not in record["proposal"]["slots"]["inis"],
                record["proposal"]["slots"]["inis"])
        r.equal("so the gate reports it applied",
                (record.get("changed") or {}).get("config"), modify.APPLIED)
        status = agent.resume(name_b, approved=True)
        r.check("and /approve is not refused",
                status.get("submitted") is not False)

        # ============================================================== #
        r.section("a rebuild that declares nothing is treated as incomplete")
        # NOT decided by asking whether the sentence sounded like a change.
        # Decided from state: this run name already had a proposal, so the one
        # that replaces it is a rebuild, and a rebuild owes a declaration of
        # what it rebuilt. `changes=[]` would satisfy it; nothing does not.
        agent = fixture(work, [
            execute_block(f"#!BASH\n{GEN}"),
            execute_block('propose_submission("cmd.sh")'),
            # The rework: regenerate and re-propose, saying nothing about what
            # moved. This is the shape prose at the gate produces.
            execute_block(f"#!BASH\n{GEN_WITHOUT}"),
            execute_block('propose_submission("cmd.sh")'),
            solution("Done."),
        ])
        status = agent.run("run rnaseq stringtie on readset.tsv", "chat-c")
        name_c = status.get("name")
        r.check("the first proposal is not flagged -- nothing preceded it",
                not agent.registry.get(name_c).get("undeclared"))

        agent.resume(name_c, approved=False,
                     feedback="drop the walltime override")
        record = agent.registry.get(name_c)
        r.equal("the rebuild is still held -- this warns, it does not block",
                record["status"], runs_store.HELD)
        r.truthy("and carries a warning that nothing checked it",
                 record.get("undeclared"))
        r.contains("which says so in as many words", record["undeclared"],
                   "did not declare what it was changing")

        status = agent.resume(name_c, approved=True)
        r.equal("/approve is refused once", status.get("submitted"), False)
        r.equal("nothing was submitted",
                agent.registry.get(name_c)["status"], runs_store.HELD)
        status = agent.resume(name_c, approved=True)
        r.check("and a second /approve goes through",
                status.get("submitted") is not False)

        # ============================================================== #
        r.section("changes=[] is a real answer and satisfies it")
        agent = fixture(work, [
            execute_block(f"#!BASH\n{GEN}"),
            execute_block('propose_submission("cmd.sh", changes=[])'),
            solution("The same run again, unchanged."),
        ])
        agent._runs_examined = {"some-earlier-run"}
        status = agent.run("run exactly that again", "chat-d")
        name_d = status.get("name")
        r.check("nothing is flagged",
                not agent.registry.get(name_d).get("undeclared"),
                agent.registry.get(name_d).get("undeclared"))
        status = agent.resume(name_d, approved=True)
        r.equal("and the first /approve submits",
                status.get("submitted") is not False, True)

        # ============================================================== #
        r.section("a fresh run owes no declaration")
        agent = fixture(work, [
            execute_block(f"#!BASH\n{GEN}"),
            execute_block('propose_submission("cmd.sh")'),
            solution("Built."),
        ])
        status = agent.run("run rnaseq stringtie on readset.tsv", "chat-e")
        name_e = status.get("name")
        r.check("nothing is flagged on a first proposal",
                not agent.registry.get(name_e).get("undeclared"))
        r.equal("and no row is marked either way",
                agent.registry.get(name_e).get("changed"), {})

        # ============================================================== #
        r.section("the guided /modify apply path survives its own seam")
        # BOTH CHECKS HERE ARE FOR CRASHES, and both were reachable by an
        # ordinary keystroke while every suite passed -- nothing drove the
        # guided-apply path end to end.
        agent = fixture(work, [
            execute_block(f"#!BASH\n{GEN}"),
            execute_block('propose_submission("cmd.sh")'),
            execute_block(f"#!BASH\n{GEN.replace('-s 1-5', '-s 1-4')}"),
            execute_block('propose_submission("cmd.sh", changes=['
                          '{"field": "steps", "operation": "set", '
                          '"value": "1-4"}])'),
            solution("Done."),
        ])
        status = agent.run("run rnaseq stringtie on readset.tsv", "chat-f")
        name_f = status.get("name")
        record = agent.registry.get(name_f)

        # _rework wrapped the declaration in dict(); a declaration is a list of
        # three-key dicts, so this raised ValueError and lost the change set.
        cli._apply_changes(agent, name_f, record["proposal"], {"steps": "1-4"},
                           workdir=work)
        record = agent.registry.get(name_f)
        r.equal("the change reached the command",
                record["proposal"]["slots"]["steps"], "1-4")
        r.equal("and the gate marked it applied",
                (record.get("changed") or {}).get("steps"), modify.APPLIED)

        r.section("a change that cost no model call leaves an approvable run")
        # cli._redraw writes `changed` as a LIST of row names -- there are no
        # verdicts to write, because nothing regenerated. The approval guard
        # called .items() on it, which raised AttributeError BEFORE the
        # script-exists check and before submission.
        cli._redraw(agent, name_f, ["resources"])
        marks = agent.registry.get(name_f).get("changed")
        r.check("the record really does carry a list here",
                isinstance(marks, list), marks)
        status = agent.resume(name_f, approved=True)
        r.check("and /approve does not raise on it",
                status.get("submitted") is not False, status)

        # ============================================================== #
        r.section("an acknowledgement does not outlive the box it was for")
        # It used to be keyed on the proposal's revision -- a hash of the
        # command. The failure being caught is "the model regenerated and the
        # command did not move", which produces the SAME hash, so a second
        # identically-ignored regeneration reused the acknowledgement and
        # submitted with no warning.
        agent = fixture(work, [
            execute_block(f"#!BASH\n{GEN}"),
            DECLARES_REMOVAL,
            # Asked again; ignored again; byte-identical command, so identical
            # revision.
            execute_block(f"#!BASH\n{GEN}"),
            DECLARES_REMOVAL,
            solution("Done."),
        ])
        status = agent.run("rerun without override_walltime.ini", "chat-g")
        name_g = status.get("name")
        r.equal("first pass: reported not applied",
                (agent.registry.get(name_g).get("changed") or {}).get("config"),
                modify.IGNORED)
        r.equal("and refused once", agent.resume(name_g, approved=True)
                .get("submitted"), False)

        # Instead of approving again, ask for it again. The model ignores it
        # again and the command is identical.
        agent.resume(name_g, approved=False,
                     feedback="no, really, take that ini off")
        again = agent.registry.get(name_g)
        r.equal("second pass: still reported not applied",
                (again.get("changed") or {}).get("config"), modify.IGNORED)
        r.check("the acknowledgement was cleared by the new gate pass",
                not again.get("acknowledged_ignored"),
                again.get("acknowledged_ignored"))
        r.equal("so it is refused again rather than submitted silently",
                agent.resume(name_g, approved=True).get("submitted"), False)
        r.equal("and nothing was submitted",
                agent.registry.get(name_g)["status"], runs_store.HELD)

        # ============================================================== #
        r.section("/relaunch: a diagnosis becomes a prepared retry")
        # END TO END, through the real graph, WITH NOTHING STUBBED -- because
        # there is no longer a model in this path to stub. The script below is
        # EMPTY on purpose: ScriptedLLM raises IndexError the moment anything
        # asks it for a reply, so a single inference step anywhere between
        # /relaunch and the gate fails this suite loudly rather than quietly
        # costing 108 seconds. agent.llm.calls is asserted as well, so a
        # future llm that answers without being scripted cannot hide either.
        #
        # The suite must not be able to pass because something recognised a
        # walltime. The remediation is put on the record as DATA, the way
        # /diagnose stores it, and the assertions are about what deterministic
        # code did with it.
        launched = agent.registry.get(name)
        agent.registry.remember_remediation(
            name, {"picard_sam_to_fastq": {"cluster_walltime": "35:00:00"}},
            ["whether 35:00:00 is sufficient for this input"],
            before={"picard_sam_to_fastq": {"cluster_walltime": "0:10:00"}})
        frozen = copy.deepcopy(agent.registry.get(name))

        # The name /relaunch will allocate is not assumed: earlier sections of
        # this suite have already consumed several suffixes in this workdir, so
        # the test asks the same allocator the command will ask.
        revised = agent.registry.unique_name(name)
        retry_ini = os.path.join(work, f"{revised}.override.ini")
        # The range is NOT written down here. It is whatever relaunch.scope
        # computed from the generated 6.1.1 facts. A literal would let the
        # suite pass while the facts said something else.
        full = relaunch.scope(launched)[0]
        agent.llm = ScriptedLLM([])
        cli._cmd_relaunch(agent, [name])
        r.equal("the retry was prepared without asking a model anything",
                agent.llm.calls, 0)

        revision = agent.registry.get(revised)
        r.truthy("a revision was created under an allocated name", revision)
        r.check("under a name the original did not have", revised != name)
        r.equal("waiting for approval, not submitted",
                revision["status"], runs_store.HELD)
        r.equal("with no job list of its own", revision.get("job_list"), None)
        r.equal("its lineage points at the original",
                revision.get("derived_from"), name)
        r.equal("and says why it exists",
                revision.get("derived_reason"), relaunch.REASON)

        r.check("an override ini was written under the REVISION's name",
                os.path.exists(retry_ini), os.listdir(work))
        r.equal("carrying exactly the diagnosed setting",
                override.read(retry_ini),
                {"picard_sam_to_fastq": {"cluster_walltime": "35:00:00"}})
        r.check("and no ini was written under the original's name",
                not os.path.exists(os.path.join(work,
                                                f"{name}.override.ini")))

        stack = revision["proposal"]["slots"]["inis"]
        r.check("the revision's -c stack carries the override",
                any(os.path.basename(str(i)) == os.path.basename(retry_ini)
                    for i in stack), stack)
        r.equal("as its LAST entry, where an override has to be",
                os.path.basename(str(stack[-1])), os.path.basename(retry_ini))
        r.equal("and the deterministic full step range is on the command",
                revision["proposal"]["slots"]["steps"], full)
        r.equal("which came from the generated facts, not the model",
                full, slots.step_range("rnaseq", "stringtie"))
        r.check("and it is not the range the original ran",
                full != frozen["proposal"]["slots"].get("steps"))

        r.equal("the command is the SOURCE's, not a new one",
                gate.flag_value(revision["proposal"]["generated"], "-r"),
                gate.flag_value(frozen["proposal"]["generated"], "-r"))
        r.equal("with every other flag the source wrote left alone",
                gate.flag_value(revision["proposal"]["generated"], "-d"),
                gate.flag_value(frozen["proposal"]["generated"], "-d"))
        r.check("and a script for /approve to run, generated for real",
                os.path.exists(os.path.join(work, "cmd.sh")), os.listdir(work))

        r.equal("the declared step change was verified as applied",
                (revision.get("changed") or {}).get("steps"), modify.APPLIED)
        r.equal("and so was the override's arrival on -c",
                (revision.get("changed") or {}).get("config"), modify.APPLIED)

        r.section("/relaunch changed nothing about the run it copied")
        after = agent.registry.get(name)
        for field in ("status", "job_list", "submitted_at", "workdir",
                      "thread_id"):
            r.equal(f"the original's {field} is untouched",
                    after.get(field), frozen.get(field))
        r.equal("its generated command is byte-identical",
                after["proposal"]["generated"], frozen["proposal"]["generated"])
        r.equal("its -c stack is byte-identical",
                after["proposal"]["slots"]["inis"],
                frozen["proposal"]["slots"]["inis"])
        r.equal("its step range is byte-identical",
                after["proposal"]["slots"].get("steps"),
                frozen["proposal"]["slots"].get("steps"))
        r.equal("and its diagnosis survives for a second retry",
                runs_store.remediation_of(after)["override"],
                runs_store.remediation_of(frozen)["override"])

        r.section("the revision still has to be approved like anything else")
        r.equal("nothing was submitted by preparing it",
                agent.registry.get(revised)["status"], runs_store.HELD)
        status = agent.resume(revised, approved=True)
        r.check("and a typed approval goes through the ordinary gate",
                status.get("submitted") is not False, status)
        r.check("only then does it leave the held state",
                agent.registry.get(revised)["status"] != runs_store.HELD)

        r.section("a second /relaunch does not overwrite the first")
        # ALSO THE REGRESSION FOR THE FLAG THAT SILENCED THE GATE. This runs
        # after the approval above, and submission_gate() ends the turn without
        # interrupting while `_submitted_this_turn` stands. That flag is
        # cleared by _drive(), which neither regate() nor hold_prepared() goes
        # through -- so before agent._raise_gate cleared it, every gate raised
        # after any approval in the same session parked nothing, said nothing,
        # and left a record behind claiming to be held.
        second = agent.registry.unique_name(name)
        second_ini = os.path.join(work, f"{second}.override.ini")
        agent.llm = ScriptedLLM([])
        cli._cmd_relaunch(agent, [name])
        r.check("the next name is allocated instead", second != revised)
        r.truthy("and that name now holds a run", agent.registry.get(second))
        r.truthy("with a real decision behind it, not just the word held",
                 agent.gate_interrupt(agent._config(
                     agent.registry.get(second)["thread_id"])))
        r.equal("recorded in the run's own directory, where /approve will "
                "re-run its generation",
                agent.registry.get(second)["workdir"], work)
        r.check("with its own override ini, not the first one's",
                os.path.exists(second_ini) and second_ini != retry_ini)
        r.equal("and the first revision is still there and still itself",
                agent.registry.get(revised)["proposal"]["slots"]["steps"], full)

        r.section("/modify on a prepared revision still reaches the model")
        # THE ONE THING THE DETERMINISTIC PATH COULD HAVE BROKEN. A revision
        # built without a model has no conversation behind it, and /modify's
        # in-place rework says "change -s from X to Y" and trusts the thread to
        # still have the command in front of it. agent.prepared_transcript is
        # what puts it there; this is the check that it really is readable by
        # the same parser every other reader uses.
        # Done on the SECOND revision, which is still held: the first has been
        # approved by now, and /modify on a launched run forks and asks for a
        # name rather than reworking in place.
        narrowed = GEN_WITHOUT.replace(" -s 1-5", " -s 1-4").replace(
            " -r readset.tsv", f" {second_ini} -r readset.tsv")
        agent.llm = ScriptedLLM([
            execute_block(f"#!BASH\n{narrowed}"),
            execute_block('propose_submission("cmd.sh", changes=[',
                          '    {"field": "steps", "operation": "set",',
                          '     "value": "1-4"}])'),
            solution("Narrowed it."),
        ])
        cli._cmd_modify(agent, [second, "run only steps 1-4"])
        r.check("the model was asked", agent.llm.calls > 0)
        asked = "\n".join(agent.llm.seen)
        r.contains("and it was given the command it is changing", asked,
                   "genpipes rnaseq -t stringtie")
        r.contains("including the override the retry put on -c", asked,
                   os.path.basename(second_ini))
        r.contains("and the change that was asked for", asked, "1-4")
        r.equal("so the rework landed on the same run, not a new one",
                agent.registry.get(second)["proposal"]["slots"]["steps"], "1-4")
        r.equal("and it is still the retry it was",
                agent.registry.get(second).get("derived_reason"),
                relaunch.REASON)

        r.section("a generation that fails is not called prepared")
        # The deterministic path's version of the truth-telling rule: the
        # command is written here, so the way it can go wrong is GenPipes
        # refusing it. Nothing may be held on the strength of a generation that
        # did not produce a script -- an approval spent on one can only fail.
        third = agent.registry.unique_name(name)
        agent.llm = ScriptedLLM([])
        held = agent.hold_prepared(third, "#!BASH\nexit 3", "cmd.sh", work,
                                   f"{third}::thread")
        r.equal("a refused generation holds nothing", held, None)
        r.equal("no run was created under the name it would have used",
                agent.registry.get(third), None)
        r.equal("and no model was asked to rescue it", agent.llm.calls, 0)
        r.equal("the original is still exactly what it was",
                agent.registry.get(name)["status"], frozen["status"])

        # ============================================================== #
        r.section("modifying a launched run still leaves the original alone")
        # The rule that predates all of this: a submitted run is history.
        submitted = agent.registry.get(name)
        r.check("the run submitted above is still recorded as submitted",
                submitted["status"] in runs_store.AFTER_APPROVAL,
                submitted["status"])
        r.equal("holding the command it actually ran",
                submitted["proposal"]["slots"]["inis"], STACK.split())

    finally:
        os.chdir(here)
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(envdir, ignore_errors=True)

    return r.finish()


if __name__ == "__main__":
    raise SystemExit(main())
