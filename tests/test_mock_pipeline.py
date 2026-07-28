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
import sys
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.messages import AIMessage
from genpipe.cli import build_agent


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
        return AIMessage(content=self.script[i])


# A harmless bash heredoc: writes a stub "GenPipes-shaped" script and a
# comment line that looks like a real generation command, so the gate's
# proposal box has something realistic to parse and display -- without
# actually invoking GenPipes or touching the module system.
GENERATE_STEP = (
    "Generating the run.\n"
    "<execute>\n"
    "#!BASH\n"
    "# module load mugqic/genpipes/6.1.1 && genpipes rnaseq -t stringtie -s 1-4 -g cmd.sh\n"
    "cat > cmd.sh << 'EOF'\n"
    "#!/bin/bash\n"
    "echo mock-submission-ran\n"
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

        print("\n=== B. reject path ===")
        agent.llm = FakeLLM([GENERATE_STEP, SUBMIT_STEP,
                             "<solution>Stopped, will not resubmit without new instructions.</solution>"])
        status = agent.run("mock task", thread_id="mock-reject")
        expect("pauses at the gate", status["status"], "paused")

        status = agent.resume("mock-reject", approved=False, feedback="not now")
        expect("finishes after rejection", status["status"], "done")

        print(f"\n{PASSED} passed, {FAILED} failed")
        return 0 if FAILED == 0 else 1
    finally:
        os.chdir(prev_cwd)
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
