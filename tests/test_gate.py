#!/usr/bin/env python
"""
test_gate.py  --  model-free logic test for the GenpipeA1 submission gate.

Runs on Rorqual in the biomni venv, no model call, no API, no cluster.
It builds a bare GenpipeA1 with object.__new__ (skipping the heavy __init__)
and hammers the pure helpers the gate's correctness reduces to:

    _extract_pending_code   pulls code out of the model's last message
    _is_submission          decides whether that code is a real submission
    _build_proposal         parses the command into the approval box

Run:  python tests/test_gate.py
Exit code is 0 if every invariant holds, 1 otherwise.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from genpipe_agent import GenpipeA1

# Bare instance: no __init__, so no LLM, no data lake, no API key needed.
gate = object.__new__(GenpipeA1)


class Msg:
    """Minimal stand-in for a langchain message: only .content is read."""
    def __init__(self, content):
        self.content = content


def state_with(code_line):
    """A state whose last message carries an <execute> block."""
    return {"messages": [Msg("some reasoning\n<execute>" + code_line + "</execute>")]}


passed = 0
failed = 0


def expect(label, got, want):
    global passed, failed
    ok = got == want
    passed += ok
    failed += (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} want={want!r}")


def route(state):
    """Mirror routing_function's decision for a next_step=='execute' turn:
    a real submission is diverted to the gate, everything else runs."""
    code = gate._extract_pending_code(state)
    if code and gate._is_submission(code):
        return "gate"
    return "execute"


print("=== A. _is_submission: commands that MUST be gated ===")
for c in [
    "bash cmd.sh",
    "sh cmd.sh",
    "bash ./cmd.sh",
    "bash /home/pbourque/scratch/rnaseq_run/cmd.sh",
    "chunk_genpipes.sh cmd.sh job_output -n 15",
    "submit_genpipes",
    "chunk_genpipes.sh script.sh && submit_genpipes",
]:
    expect(c, gate._is_submission(c), True)

print("\n=== B. _is_submission: commands that MUST pass through ===")
for c in [
    "module load mugqic/genpipes/6.1.1 && genpipes rnaseq -t stringtie "
    "-c $GENPIPES_INIS/rnaseq/rnaseq.base.ini -r readset.rnaseq.txt -s 1-5 -g cmd.sh",
    "squeue -u pbourque",
    "ls /home/pbourque/scratch/rnaseq_tutorial",
    "cat cmd.sh",
    "module load mugqic/genpipes/6.1.1 && genpipes rnaseq -h",
]:
    expect(c, gate._is_submission(c), False)

print("\n=== C. routing decision (extract + is_submission composed) ===")
expect("generation -> execute",
       route(state_with("genpipes rnaseq -t stringtie -s 1-5 -g cmd.sh")), "execute")
expect("bash cmd.sh -> gate", route(state_with("bash cmd.sh")), "gate")
expect("submit_genpipes -> gate", route(state_with("submit_genpipes")), "gate")

print("\n=== D. _build_proposal slot parsing ===")
cmd = ("module load mugqic/genpipes/6.1.1 && genpipes rnaseq -t stringtie "
       "-c $GENPIPES_INIS/rnaseq/rnaseq.base.ini $GENPIPES_INIS/common_ini/rorqual.ini "
       "-r readset.rnaseq.txt -d design.rnaseq.txt -s 1-5 -g cmd.sh")
prop = gate._build_proposal({"messages": []}, cmd)
slots = prop["slots"]
expect("protocol", slots["protocol"], "stringtie")
expect("steps", slots["steps"], "1-5")
expect("design", slots["design"], "design.rnaseq.txt")
expect("pairs (none)", slots["pairs"], None)
expect("inis count", len(slots["inis"]), 2)
expect("command echoed verbatim", prop["command"], cmd.strip())

print("\n=== E. _extract_pending_code edge cases ===")
expect("normal block", gate._extract_pending_code(state_with("bash cmd.sh")), "bash cmd.sh")
expect("unclosed tag",
       gate._extract_pending_code({"messages": [Msg("<execute>bash cmd.sh")]}), "bash cmd.sh")
expect("no execute block",
       gate._extract_pending_code({"messages": [Msg("<solution>done</solution>")]}), None)
expect("empty messages", gate._extract_pending_code({"messages": []}), None)

# --- Advisory: NOT a pass/fail invariant, a design decision to make ---
print("\n=== F. ADVISORY: submissions whose script is not named cmd.sh ===")
print("  The matcher only recognizes 'cmd.sh'. If the model names the generated")
print("  script anything else, the bash-form submission slips through the gate.")
print("  Decide: enforce -g cmd.sh upstream, or broaden the matcher.")
for c in ["bash rnaseq_steps_1-5.sh", "bash ./rnaseq_stringtie.sh"]:
    got = gate._is_submission(c)
    print(f"    {'gates' if got else 'MISSES'}: {c!r}")

print("\n" + "=" * 52)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 52)
sys.exit(1 if failed else 0)
