#!/usr/bin/env python
"""The whole arc of a run, offline: generate, hold, reject, approve, watch, fix.

This is the suite that covers the half the others cannot. test_gate checks
the matcher as pure logic; test_fakecluster checks the stubs; this drives the
REAL agent -- the actual GenpipeA1 that cli.build_agent() builds, the
actual LangGraph gate, the actual SqliteSaver checkpoint -- against the fake
cluster, with a scripted model in place of Claude.

So the things being proved here are the ones that only exist when all the pieces
are assembled:

  * a submission is caught, held, and RECORDED, so it survives the session
  * a run held in one process can be approved from a DIFFERENT one, which is
    the gate's entire promise and was previously never tested
  * rejection feeds the feedback back and the model's second attempt is what
    gets submitted
  * approval writes a job list, and the registry links the name to it
  * check / jobs / why / cancel all work on the run that actually resulted

No API call, no cluster, no allocation, no cost. Takes a few seconds.

Run:  python tests/test_lifecycle.py

Needs biomni and the pinned langgraph, so it is NOT part of the GitHub Actions
run -- it belongs in the cluster venv, before a release. See .github/workflows.
"""
import os
import shutil
import sys
import tempfile

from harness import (Report, ScriptedLLM, execute_block, solution,
                     submission_environment)

from genpipe import display
from genpipe import fakecluster
from genpipe import runs as runs_store

# Never let this test's fixtures near the real .env: cli.py writes to
# ENV_PATH when a key or model is set, and a stray write would clobber a live
# key. Redirected before cli.py is used for anything.
from genpipe import cli
from genpipe.cli import build_agent

GEN = ("module load mugqic/genpipes/6.1.1 && genpipes rnaseq -t stringtie "
       "-s 1-5 -c rnaseq.base.ini common_ini/rorqual.ini -r readset.tsv "
       "-d design.tsv -g cmd.sh")
GEN_REVISED = GEN.replace("-s 1-5", "-s 6-12")


def script(gen=GEN):
    """The three turns a normal run takes: generate, submit, conclude."""
    return [
        "Generating the pipeline script.\n" + execute_block(gen),
        "The script looks right. Submitting.\n" + execute_block("bash cmd.sh"),
        solution("Submitted."),
    ]


def fixtures(work):
    """The files the fake genpipes validates against, so a correct command
    succeeds and an incorrect one still fails."""
    os.makedirs(os.path.join(work, "common_ini"), exist_ok=True)
    for path in ("rnaseq.base.ini", os.path.join("common_ini", "rorqual.ini")):
        with open(os.path.join(work, path), "w") as f:
            f.write("[DEFAULT]\ncluster_server=rorqual\n")
    with open(os.path.join(work, "readset.tsv"), "w") as f:
        f.write("Sample\tReadset\tLibrary\n")
    with open(os.path.join(work, "design.tsv"), "w") as f:
        f.write("Sample\tContrast\n")


def fresh_agent(work, llm_script, on_reject=None):
    """A fully built agent on `work`, with a scripted model in place of Claude."""
    agent = build_agent(path=work)
    # run() calls the model a second time up front for tool-retriever resource
    # selection when it is on (biomni's default). That call would eat the first
    # entry of the script and desync every step after it. Irrelevant here.
    agent.use_tool_retriever = False
    agent.llm = ScriptedLLM(llm_script, on_reject=on_reject)
    return agent


def main():
    # This suite approves and submits, so it needs an environment a
    # submission may legally happen in. See harness.submission_environment:
    # it used to inherit one from the login shell without asking.
    submission_environment()
    r = Report("a run's whole life, offline")
    work = tempfile.mkdtemp(prefix="genpipe_life_")
    # Biomni's A1.__init__ re-roots self.path at <workdir>/biomni_data, and the
    # registry lives beside the checkpoint database there rather than in the
    # directory handed to build_agent. Worth knowing, and worth /where printing.
    store = os.path.join(work, "biomni_data")
    envdir = tempfile.mkdtemp(prefix="genpipe_env_")
    cli.ENV_PATH = __import__("pathlib").Path(envdir) / ".env"
    prev = os.getcwd()
    try:
        with fakecluster.session("failed-oom"):
            os.chdir(work)          # cmd.sh and job_output/ land here
            fixtures(work)

            # ============================================================== #
            r.section("a submission is caught and held")
            agent = fresh_agent(work, script())
            status = agent.run("run rnaseq stringtie steps 1-5", thread_id="life-1")
            r.equal("paused at the gate", status["status"], "paused")
            r.equal("holding the right command",
                    status["proposal"]["command"], "bash cmd.sh")
            r.equal("with the protocol parsed from the generation",
                    status["proposal"]["slots"]["protocol"], "stringtie")
            r.equal("and the steps", status["proposal"]["slots"]["steps"], "1-5")

            r.truthy("the generation actually ran (cmd.sh exists)",
                     os.path.exists(os.path.join(work, "cmd.sh")))
            r.check("but nothing was submitted",
                    not os.path.isdir(os.path.join(work, "job_output")))

            r.section("...and recorded, so it survives the session")
            reg = runs_store.Registry(store)
            held = reg.held()
            r.equal("one run is held on disk", len(held), 1)
            # A run's name is minted at the gate from what the run turned out
            # to be, not from the conversation it happened on -- one conversation
            # produces many runs, so the thread cannot be the run's identity.
            # This test used to assert name == thread_id and then look the record
            # up under "life-1", which returned None and failed with a TypeError
            # three sections later.
            run_name = held[0]["name"]
            r.contains("named after what it is, not the conversation",
                       run_name, "rnaseq")
            r.equal("and it is this conversation's held run",
                    reg.held_for_thread("life-1")["name"], run_name)
            r.equal("with the command awaiting approval",
                    held[0]["proposal"]["command"], "bash cmd.sh")
            r.equal("and the directory it will submit from",
                    held[0]["workdir"], work)
            r.contains("the task that started it is kept too",
                       str(held[0].get("task")), "rnaseq")

            # ============================================================== #
            r.section("A HELD RUN CAN BE APPROVED FROM A DIFFERENT PROCESS")
            # The gate's central promise. A second agent object built on the same
            # workdir shares nothing but the SqliteSaver file on disk -- which is
            # exactly what a relaunched app has.
            reborn = fresh_agent(work, [solution("Submission complete.")])
            r.equal("the new process sees the pending decision",
                    len(reborn.pending()), 1)
            status = reborn.resume(run_name, approved=True)
            r.equal("and can act on it", status["status"], "done")

            r.section("approval submitted, and the run was registered")
            reg = runs_store.Registry(store)
            rec = reg.get(run_name)
            r.equal("promoted to submitted", rec["status"], runs_store.SUBMITTED)
            r.truthy("a job list was linked", rec["job_list"])
            r.truthy("which exists on disk", os.path.exists(rec["job_list"] or ""))
            r.equal("nothing is held any more", len(reg.held()), 0)

            r.section("the jobs inside it are readable")
            jobs = runs_store.jobs_for(rec)
            r.equal("fifteen jobs", len(jobs), 15)
            r.truthy("every one has a state from the scheduler",
                     all(j.state for j in jobs))
            r.equal("five distinct steps", len({j.step for j in jobs}), 5)

            # ============================================================== #
            r.section("check(): the aggregate view, and it caches")
            status = agent.check(run_name)
            r.truthy("the scheduler answered", status)
            r.equal("about every job in the manifest",
                    status.resolved, status.total)
            cached = runs_store.Registry(store).get(run_name)["last_check"]
            r.truthy("the result was cached for /list", cached)
            # The wording changed with the source. check() used to report
            # GenPipes' log_report, which reads files on disk and cannot see a
            # job that never started or one that was killed; it now reports
            # sacct, and says whether anything is still running.
            r.contains("with a verdict naming the trouble",
                       cached["verdict"], "failed")

            r.section("jobs(): the per-job view")
            listed = agent.jobs(run_name)
            r.equal("all of them", len(listed), 15)
            failed_only = agent.jobs(run_name, only_failed=True)
            r.truthy("filtering to failures returns fewer",
                     0 < len(failed_only) < 15)
            r.truthy("and only failures", all(j.failed for j in failed_only))

            # ============================================================== #
            r.section("diagnose(): facts first, then the model")
            agent.llm = ScriptedLLM([solution(
                "picard_mark_duplicates ran out of Java heap. Raise "
                "java_other_options -Xmx in the ini and rerun step 3.")])
            status = agent.diagnose(run_name)
            r.equal("it reached a conclusion", status["status"], "done")
            r.contains("naming the failing step", status["final"],
                       "picard_mark_duplicates")

            r.section("...on its own thread, never the run's")
            # The run's own conversation must be intact: Biomni's AgentState has
            # no message reducer, so reusing its thread would have replaced it.
            snap = agent.app.get_state(agent._config("life-1"))
            joined = "\n".join(str(getattr(m, "content", ""))
                              for m in snap.values["messages"])
            r.contains("the original generation is still there", joined, "genpipes rnaseq")
            r.contains("and the submission", joined, "bash cmd.sh")
            r.check("the diagnosis did not overwrite it",
                    "ran out of Java heap" not in joined)

            r.section("...and the finding is recorded against the run")
            notes = runs_store.Registry(store).get(run_name).get("notes") or []
            r.truthy("a note was kept", notes)
            r.contains("summarising the cause", str(notes), "heap")
            r.check("without the <solution> markup leaking into it",
                    "<solution>" not in str(notes))

            # ============================================================== #
            r.section("cancel(): nothing to do on a run that already finished")
            r.equal("no jobs targeted", agent.cancel(run_name), 0)

            # ============================================================== #
            r.section("rejection sends feedback back, and the retry is what submits")
            agent2 = fresh_agent(
                work,
                script(),
                on_reject=["Understood, using steps 6-12 instead.\n"
                           + execute_block(GEN_REVISED),
                           "Regenerated. Submitting.\n"
                           + execute_block("bash cmd.sh"),
                           solution("Submitted.")])
            status = agent2.run("run rnaseq stringtie", thread_id="life-2")
            r.equal("held first", status["status"], "paused")
            second = runs_store.Registry(store).held_for_thread("life-2")["name"]
            r.equal("at steps 1-5", status["proposal"]["slots"]["steps"], "1-5")

            status = agent2.resume(second, approved=False,
                                   feedback="use steps 6-12 instead")
            r.contains("the model was told why",
                       "\n".join(agent2.llm.seen), "use steps 6-12 instead")
            # The revised generation re-reaches the gate, since the second
            # attempt also ends in a submission.
            r.equal("and it comes back for approval", status["status"], "paused")
            r.equal("now at the revised steps",
                    status["proposal"]["slots"]["steps"], "6-12")
            r.equal("the held record was updated, not duplicated",
                    len([x for x in runs_store.Registry(store).all()
                         if x["name"] == second]), 1)

            # ============================================================== #
            r.section("a rejected run that is never approved submits nothing")
            before = set(os.listdir(os.path.join(work, "job_output")))
            agent2.resume(second, approved=False, feedback="stop, forget it")
            after = set(os.listdir(os.path.join(work, "job_output")))
            r.equal("no new job list appeared", before, after)
            r.equal("and it is still awaiting a decision, not submitted",
                    runs_store.Registry(store).get(second)["status"],
                    runs_store.HELD)

            # ============================================================== #
            r.section("commands on runs that don't qualify explain themselves")
            r.equal("check on an unknown run returns nothing",
                    agent.check("no-such-run"), None)
            r.equal("jobs on a held run returns nothing",
                    agent.jobs(second), [])
            r.equal("diagnose on a run with no failures returns nothing",
                    agent.diagnose("no-such-run"), None)

            r.section("a purged job list retires the run without deleting it")
            os.remove(rec["job_list"])
            reg = runs_store.Registry(store)
            r.check("gone from /list",
                    run_name not in [x["name"] for x in reg.live()])
            r.contains("still in /history",
                       str([x["name"] for x in reg.all()]), run_name)

            # ============================================================== #
            r.section("/sort hides a row, and /sort show brings it back")
            # `show` was in this command's own hint line with no handler behind
            # it, so it fell through to the name branch and came back as "No
            # run named 'show'" -- which made hiding a one-way door.
            cli._cmd_sort(agent, [second])
            reg = runs_store.Registry(store)
            r.check("the named run leaves /list",
                    second not in [x["name"] for x in reg.live()])
            r.check("but it is still on record",
                    second in [x["name"] for x in reg.all(prune=False)])

            cli._cmd_sort(agent, ["show"])
            reg = runs_store.Registry(store)
            r.check("and show puts it back", second in [x["name"] for x in reg.live()])

            # ============================================================== #
            r.section("/verbose's menu row says which way it will flip")
            # It was written as a fixed "[off]", which is a label for one of the
            # two states it can be in and so reads wrong in the other.
            was = display.VERBOSE
            try:
                display.set_verbose(False)
                row = [m for m in cli.menu() if m[0] == "verbose"][0]
                # Nothing to type: bare /verbose flips it. Offering an argument
                # for a toggle invites somebody to work out which of the two
                # they need before they can press a key.
                r.equal("folded away, no argument is asked for", row[1], "")
                r.contains("and the row says where it stands", row[2], "folded away")
                r.contains("and which way it will go", row[2], "show")

                display.set_verbose(True)
                row = [m for m in cli.menu() if m[0] == "verbose"][0]
                r.equal("showing, still no argument", row[1], "")
                r.contains("and the row says that too", row[2], "showing")
                r.contains("with the other direction offered", row[2], "fold")
                r.contains("/help reads from the same place",
                           str(cli.help_rows()), "showing")
            finally:
                display.set_verbose(was)

        return r.finish()
    finally:
        os.chdir(prev)
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(envdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
