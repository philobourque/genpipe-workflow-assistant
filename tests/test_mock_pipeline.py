#!/usr/bin/env python
"""
test_mock_pipeline.py -- fast, offline, end-to-end test of the gated graph.

test_gate.py checks the gate's pure helper functions in isolation. This test
drives the real thing: the actual GenpipeA1 built by cli.build_agent()
(same construction path production uses), through the real LangGraph plumbing
-- generate -> route -> gate -> interrupt -> resume -> execute -> generate ->
end -- with a scripted FakeLLM standing in for Claude.

No Anthropic API call, no ANTHROPIC_API_KEY required, no real GenPipes command,
no SLURM job, no cost, no wait. A full round trip (both an approve and a
reject) takes a couple of seconds. This is the loop to run after touching
genpipe/agent.py or display.py, before burning API credits or a cluster
allocation on a real conversation.

What this does NOT test: whether Claude actually writes correct GenPipes
commands from genpipes.md. That needs a real model and a real task, and
belongs in an occasional live smoke test, not this one. This test is about the
gate's plumbing -- does a real submission get caught, does approval let it
run, does rejection loop back -- not about the model's judgement.

The one real side effect: the approved scenario writes and runs a harmless
local stub `cmd.sh` (not a real GenPipes script) inside a throwaway temp
directory, to prove the full "approve -> execute -> done" path actually
executes code, not just that the graph transitions look right on paper. Never
touches the real cluster, scheduler, or GenPipes module.

Run:  python tests/test_mock_pipeline.py
Exit code is 0 if every scenario behaves as expected, 1 otherwise.
"""
import os
import subprocess
import sys
import shutil
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import submission_environment
from langchain_core.messages import AIMessage
from genpipe.cli import build_agent
from genpipe import runs as runs_store


class FakeLLM:
    """Stands in for agent.llm. .invoke() ignores the conversation and returns
    the next canned response in `script` -- the graph and the gate are what's
    under test here, not a model's reasoning."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def invoke(self, messages):
        i = min(self.calls, len(self.script) - 1)
        self.calls += 1
        if self.script[i] is BOOM:
            # What an exhausted API key looked like on 2026-07-29, and the
            # shape of anything else that kills a turn after the command has
            # already run: a rate limit, a dropped connection, a Ctrl-C.
            raise RuntimeError("Error code: 400 - credit balance is too low")
        return AIMessage(content=self.script[i])


# A scripted turn that raises instead of replying. Used to fail the turn AFTER
# the submission has executed, which is the only way to test that the record
# survives the reporting.
BOOM = object()


# A harmless bash heredoc: writes a stub "GenPipes-shaped" script and a
# comment line that looks like a real generation command, so the gate's
# proposal box has something realistic to parse and display -- without
# actually invoking GenPipes or touching the module system.
GENERATE_STEP = (
    "Generating the run.\n"
    "<execute>\n"
    "#!BASH\n"
    # -c is on this line because argparse requires it of every pipeline, and
    # the gate refuses a generation without one before it will draw a box. A
    # fixture missing it was modelling a command GenPipes would reject.
    "# module load mugqic/genpipes/6.1.1 && genpipes rnaseq -t stringtie -s 1-4 "
    "-c rnaseq.base.ini common_ini/rorqual.ini "
    "-r readset.tsv -d design.tsv -g cmd.sh\n"
    "cat > cmd.sh << 'EOF'\n"
    "#!/bin/bash\n"
    "echo mock-submission-ran\n"
    # A durable side effect, so a test can assert the command really ran even
    # when the turn that would have reported it never finished.
    "echo ran >> mock-submission-ran\n"
    "EOF\n"
    "chmod +x cmd.sh\n"
    "</execute>"
)

SUBMIT_STEP = (
    "Generation looks good, submitting.\n"
    "<execute>\n"
    "#!BASH\n"
    "bash cmd.sh\n"
    "</execute>"
)

PASSED = 0
FAILED = 0


def expect(label, got, want):
    global PASSED, FAILED
    ok = got == want
    PASSED += ok
    FAILED += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} want={want!r}")


def main():
    # Drives a full generate-approve-submit cycle against the fake cluster.
    submission_environment()
    workdir = tempfile.mkdtemp(prefix="genpipe_mock_")
    prev_cwd = os.getcwd()
    try:
        os.chdir(workdir)  # cmd.sh and job_output/ land here, not in the repo
        agent = build_agent(path=workdir)

        # run() calls the LLM a second time up front for tool-retriever resource
        # selection when use_tool_retriever is on (biomni's default). That call
        # would eat the first entry of our scripted FakeLLM and desync every
        # step after it. Irrelevant to what this test checks -- turn it off.
        agent.use_tool_retriever = False

        print("=== A. approve path ===")
        agent.llm = FakeLLM([GENERATE_STEP, SUBMIT_STEP,
                             "<solution>Mock run complete.</solution>"])
        status = agent.run("mock task", thread_id="mock-approve")
        expect("pauses at the gate", status["status"], "paused")
        expect("gate caught the right command", status["proposal"]["command"], "bash cmd.sh")
        expect("proposal parsed the protocol", status["proposal"]["slots"]["protocol"], "stringtie")

        status = agent.resume("mock-approve", approved=True)
        expect("finishes after approval", status["status"], "done")
        # The graph's shape is not the claim worth checking. What /approve says
        # to a person now comes from the RECORD, because "done" is equally true
        # of a thread that finished, one that died, and one that was never
        # resumed -- see section C.
        name = agent.registry.all()[0]["name"]
        expect("and the run is no longer awaiting approval",
               agent.registry.get(name)["status"] in
               (runs_store.SUBMITTED, runs_store.SUBMIT_UNKNOWN), True)

        print("\n=== B. reject path ===")
        agent.llm = FakeLLM([GENERATE_STEP, SUBMIT_STEP,
                             "<solution>Stopped, will not resubmit without new instructions.</solution>"])
        status = agent.run("mock task", thread_id="mock-reject")
        expect("pauses at the gate", status["status"], "paused")

        status = agent.resume("mock-reject", approved=False, feedback="not now")
        # THIS ASSERTION CHANGED, and the change is the fix rather than a
        # concession to it. It used to read `"done"`: feedback consumed the
        # interrupt, the model answered in prose without re-proposing, and the
        # graph ended -- while the registry was written back to `held` anyway,
        # so /list offered an approval /approve would refuse.
        #
        # The decision is restored for real now, with no model call, so a run
        # that says it is waiting IS waiting. What the invariant demands is
        # that these three agree, and agreeing is the whole of what is checked.
        expect("the decision is restored, not faked", status["status"], "paused")
        reject_name = agent.registry.held_for_thread("mock-reject")["name"]
        expect("the record says held", agent.registry.get(reject_name)["status"],
               runs_store.HELD)
        expect("and the graph really is parked at a gate",
               agent.gate_interrupt(agent._config("mock-reject")) is not None,
               True)

        # ============================================================== #
        print("\n=== C. an exception AFTER the submission ran ===")
        # The 2026-07-29 defect, reproduced. The submission executes, then the
        # turn that would have reported it dies -- on that day, an API credit
        # error two graph nodes later. _record_submission sat after _drive()
        # and was simply skipped, so 46 real jobs finished on Rorqual while the
        # registry said `held` and kept offering /approve.
        agent.llm = FakeLLM([GENERATE_STEP, SUBMIT_STEP, BOOM])
        status = agent.run("mock task", thread_id="mock-boom")
        expect("pauses at the gate", status["status"], "paused")
        boom = agent.registry.held_for_thread("mock-boom")["name"]
        before = agent.llm.calls
        raised = None
        try:
            agent.resume(boom, approved=True)
        except Exception as e:                        # noqa: BLE001
            raised = e
        # THIS ASSERTION CHANGED, and it changed because the failure it used to
        # provoke has been designed out rather than papered over.
        #
        # It read `raised is not None`: the third scripted reply was BOOM, the
        # graph called the model after the submission to narrate it, and that
        # call raised -- reproducing 2026-07-29 exactly. There is no such call
        # any more. /approve regenerates the script and runs it itself, then
        # releases the interrupt with a settled fact, so no inference happens
        # anywhere between the person saying yes and the jobs existing.
        #
        # So the stronger property is asserted instead, and it is the one that
        # makes the original defect unreachable: an approval spends ZERO model
        # calls. A turn that never happens cannot die halfway through.
        expect("an approval spends no model calls at all",
               agent.llm.calls, before)
        expect("so the reporting turn cannot fail", raised, None)
        expect("the submission really did run",
               os.path.exists(os.path.join(workdir, "mock-submission-ran")), True)
        record = agent.registry.get(boom)
        expect("but the run is NOT left awaiting approval",
               record["status"] != runs_store.HELD, True)
        expect("and it is not offered for approval again",
               boom in [h["name"] for h in agent.registry.held()], False)
        expect("the outcome was recorded despite the exception",
               record["status"] in (runs_store.SUBMITTED,
                                    runs_store.SUBMIT_UNKNOWN,
                                    runs_store.SUBMIT_FAILED), True)

        # ============================================================== #
        print("\n=== D. approving a run with nothing parked at the gate ===")
        # Defect B. The thread above is now parked on an errored task rather
        # than an interrupt -- exactly the state chat-0729-132543 was left in.
        # resume() used to return the checkpoint's bare "done" here, and
        # cli._cmd_approve printed "<name> · submitted" on the strength of it,
        # having resumed nothing.
        again = agent.resume(boom, approved=True)
        expect("a second /approve reports that nothing was submitted",
               again.get("submitted"), False)
        expect("it is not dressed up as a completed turn",
               again.get("status") == "done" and again.get("submitted") is not False,
               False)

        # ============================================================== #
        print("\n=== E. a session killed mid-submission, resolved at startup ===")
        # The gap the `finally` cannot close. An exception is caught; a SIGKILL,
        # a closed terminal or a rebooted login node is not, and the record is
        # left saying `submitting` about a session that ended days ago -- while
        # a full pipeline may be sitting on the cluster.
        #
        # Simulated by doing exactly what resume() does up to the point of no
        # return, then walking away: mark submitting with the baseline, run the
        # command, and never reconcile.
        agent.llm = FakeLLM([GENERATE_STEP, SUBMIT_STEP,
                             "<solution>done</solution>"])
        status = agent.run("mock task", thread_id="mock-killed")
        expect("pauses at the gate", status["status"], "paused")
        killed = agent.registry.held_for_thread("mock-killed")["name"]

        script = agent._approved_script(agent.registry.get(killed))
        baseline = runs_store.job_list_state(
            runs_store.declared_job_list(script))
        agent.registry.begin_submission(killed, workdir=workdir,
                                        baseline=baseline, script=script,
                                        since=time.time())
        expect("the run is mid-submission and not approvable",
               killed in [h["name"] for h in agent.registry.held()], False)
        expect("and is visible as such",
               killed in [x["name"] for x in agent.registry.submitting()], True)

        # The command runs; the session does not come back to report it.
        subprocess.run(["bash", os.path.join(workdir, "cmd.sh")],
                       cwd=workdir, capture_output=True)

        # Counted across the reconciliation, not absolutely: sections A and C
        # ran the same stub, so the marker already has lines in it. What must
        # not change is the count.
        marker = os.path.join(workdir, "mock-submission-ran")
        before_runs = open(marker).read().count("ran")

        settled = agent.reconcile_stale()
        expect("startup settles it", killed in settled, True)
        after = agent.registry.get(killed)
        expect("it is no longer mid-submission",
               after["status"] != runs_store.SUBMITTING, True)
        expect("and never silently returns to awaiting approval",
               after["status"] != runs_store.HELD, True)
        expect("nothing is left for a second pass",
               agent.registry.submitting(), [])
        # THE PROPERTY THAT MATTERS MOST HERE. Reconciling reads three things
        # off disk and writes a status; it must never resume a graph or re-run
        # a command. The stub appends one line per execution, so a retry would
        # show up as an extra one.
        expect("and nothing was resubmitted",
               open(marker).read().count("ran"), before_runs)
        expect("the outcome is recorded conservatively — this stub declares no "
               "job total, so a clean exit cannot be checked",
               after["status"], runs_store.SUBMIT_UNKNOWN)
        # retry_safe is deliberately NOT asserted here: it is answered by
        # asking the real scheduler, so it depends on what this machine's sacct
        # says -- True on a quiet login node, None-and-therefore-False where
        # there is no sacct at all. Both are correct; neither is a property of
        # this code path. test_runs pins it exactly, with quiet= supplied.
        expect("the evidence behind the verdict is on the record",
               "retry_safe" in after and "outcome_detail" in after, True)

        print(f"\n{PASSED} passed, {FAILED} failed")
        return 0 if FAILED == 0 else 1
    finally:
        os.chdir(prev_cwd)
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
