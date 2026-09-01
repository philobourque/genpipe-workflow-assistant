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
from genpipe.agent import GenpipeA1

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
    # Direct Slurm, reached through GenpipeA1's own method rather than the
    # module function -- the path the router actually takes.
    "sbatch cmd.sh",
    "sbatch --account=rrg-someone-ab job.sh",
    "srun --time=00:05:00 hostname",
    "salloc",
    "for f in *.sh; do sbatch $f; done",
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
    # Asking Slurm what is already there, versus putting something there.
    "sacct -j 41000001 --format=JobID,State",
    "scontrol show job 41000001",
    "scancel 41000001",
    # The word without the command: reading a generated script is how the
    # agent explains one, and a GenPipes cmd.sh is nothing but sbatch lines.
    "grep -n sbatch cmd.sh",
]:
    expect(c, gate._is_submission(c), False)

print("\n=== C. routing decision (extract + is_submission composed) ===")
expect("generation -> execute",
       route(state_with("genpipes rnaseq -t stringtie -s 1-5 -g cmd.sh")), "execute")
expect("bash cmd.sh -> gate", route(state_with("bash cmd.sh")), "gate")
expect("submit_genpipes -> gate", route(state_with("submit_genpipes")), "gate")
expect("sbatch cmd.sh -> gate", route(state_with("sbatch cmd.sh")), "gate")
expect("srun hostname -> gate", route(state_with("srun hostname")), "gate")
expect("salloc -> gate", route(state_with("salloc")), "gate")
expect("sacct -> execute", route(state_with("sacct -j 1 --format=State")), "execute")

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

print("\n=== G. run tracking store (track / job_list_for / prune) ===")
# The store itself now lives in runs.py and is tested directly and far more
# thoroughly by tests/test_runs.py, which runs in CI. What is left here is only
# what belongs at the AGENT level: that agent.track() refuses a path that isn't
# there, that a manually tracked run and an agent-recorded one coexist, and that
# job_list_for stops resolving a run whose artifacts have been purged.
import tempfile
import shutil

tracker = object.__new__(GenpipeA1)
tracker.path = tempfile.mkdtemp(prefix="genpipe_runs_test_")
try:
    # track() on a path that doesn't exist must record nothing.
    tracker.track("ghost", os.path.join(tracker.path, "does_not_exist.job_list"))
    expect("track() refuses a missing path", tracker.registry.load(), [])

    # track() on a real JOB LIST records a live, source="manual" entry.
    #
    # The fixture used to be an empty file, which adoption accepted -- that
    # was the defect: any file at all became a permanent run. It carries real
    # rows now, which is both what the guard requires and what a job list
    # actually is.
    job_list = os.path.join(tracker.path, "Pipeline.protocol.job_list.T1")
    with open(job_list, "w") as f:
        f.write("101\ttrimmomatic.sampleA\tjob_output/t.o\tCOMPLETED\n")
    tracker.track("manual-1", job_list)
    records = tracker.registry.load()
    expect("track() records one entry", len(records), 1)
    expect("tracked entry has no thread_id", records[0]["thread_id"], None)
    expect("tracked entry is live", records[0]["status"], "submitted")
    expect("tracked entry source is manual", records[0]["source"], "manual")

    # An agent-side submission coexists fine.
    job_list2 = os.path.join(tracker.path, "Pipeline.protocol.job_list.T2")
    open(job_list2, "w").close()
    tracker.registry.mark_submitted("patient-42", job_list2, thread_id="patient-42")
    expect("job_list_for finds the agent-recorded run",
           tracker.job_list_for("patient-42"), job_list2)
    expect("job_list_for finds the manually tracked run",
           tracker.job_list_for("manual-1"), job_list)

    # Delete one job_list file on disk, then prune via job_list_for. The gone run
    # must disappear from lookup but the record itself must survive, marked gone
    # -- not deleted.
    os.remove(job_list2)
    tracker.registry.live()          # triggers the prune
    expect("job_list_for no longer returns a gone run",
           tracker.job_list_for("patient-42"), None)
    by_name = {r["name"]: r for r in tracker.registry.load()}
    expect("gone run's record still exists", "patient-42" in by_name, True)
    expect("gone run's record is marked gone", by_name["patient-42"]["gone"], True)
    expect("untouched run is still live", by_name["manual-1"]["gone"], False)

    # Once marked gone, pruning again must not flip it back -- gone is a one-way
    # door, so a transient filesystem hiccup can't resurrect a run.
    open(job_list2, "w").close()   # simulate the path becoming valid again
    tracker.registry.live()
    expect("a gone run stays gone even if its path reappears",
           {r["name"]: r for r in tracker.registry.load()}["patient-42"]["gone"], True)
finally:
    shutil.rmtree(tracker.path, ignore_errors=True)

    shutil.rmtree(tracker.path, ignore_errors=True)

# ---------------------------------------------------------------------------
# An incomplete proposal never reaches the person.
#
# The gate means "you are one step from submitting". A box you can only reject
# is not that, and it puts the job on the wrong party: the model knows what a
# readset is and can go and find one, while the person is handed a form with a
# hole in it and no way to fill it from where they are standing.
#
# submission_gate is a closure over the compiled graph, so what is checked here
# is the decision it makes -- build_proposal's `missing` verdict -- rather than
# the node object. That verdict is the whole input to the branch.
# ---------------------------------------------------------------------------
complete = gate._build_proposal(
    {"messages": [Msg("<execute>\ngenpipes rnaseq -t stringtie "
                      "-r readset.tsv -d design.tsv -g cmd.sh\n</execute>")]},
    'propose_submission("cmd.sh")')
# -d as well as -r: stringtie is a protocol whose `needs` is DESIGN, so a
# readset alone is not a complete rnaseq stringtie run. slots.gaps() is the one
# table that knows which protocol wants which file.
expect("a command with everything required has nothing missing",
       complete["missing"], [])

no_readset = gate._build_proposal(
    {"messages": [Msg("<execute>\ngenpipes rnaseq -t stringtie "
                      "-g cmd.sh\n</execute>")]},
    'propose_submission("cmd.sh")')
expect("a command with no readset reports it missing",
       "readset" in (no_readset["missing"] or []), True)
# The slots still parse -- what is absent is absent, not unparsed. This is the
# distinction the gate node branches on, and the reason the box could ever have
# been drawn approvable with a hole in it.
expect("and its other slots are still parsed",
       no_readset["slots"]["protocol"], "stringtie")


print("\n" + "=" * 52)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 52)
sys.exit(1 if failed else 0)
