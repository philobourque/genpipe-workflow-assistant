#!/usr/bin/env python
"""The gate's invariants. This is the one test that must never go red.

Everything else in this repo is about convenience. This is about whether an
unapproved pipeline can reach a scheduler, so it is written as two lists and one
rule about which direction a mistake is allowed to fall in:

    MUST_GATE      code that submits. A miss here puts unapproved work on a real
                   cluster and spends someone's allocation. Unacceptable.
    MUST_NOT_GATE  code that only generates, reads, or TALKS ABOUT submitting.
                   A false positive here costs a rejection and an annoyed user.

Both failure modes have happened in production, and both are represented below
by the case that caused them (see gate_rules.executable_lines for the incidents).

Runs in CI on every push: gate_rules imports nothing but the standard library,
so there is no biomni, no langgraph, no API key and no cluster involved.

Run:  python tests/test_gate_rules.py
"""
import sys

from harness import Report

import gate_rules as g


class Msg:
    """Minimal stand-in for a langchain message: only .content is read."""

    def __init__(self, content):
        self.content = content


# Code that really does submit. Every one of these must stop at the gate.
MUST_GATE = [
    "bash cmd.sh",
    "bash dnaseq_cmd.sh",
    "sh rnaseq_cmd.sh",
    "bash /scratch/me/project/cmd.sh",
    "cd /scratch/me && bash cmd.sh",
    "bash chunk_genpipes.sh chunks/ 20",
    "submit_genpipes chunks/",
    # Buried in Python -- a real submission, and the reason executable_lines
    # strips only print() and not every quoted string.
    'subprocess.run("bash cmd.sh", shell=True)',
    'os.system("bash cmd.sh")',
    # Generated and submitted in one block: the generation is fine, the last
    # line is not, and the block as a whole has to be held.
    "genpipes rnaseq -t stringtie -s 1-5 -g cmd.sh\nbash cmd.sh",
]

# Code that must flow through untouched. Anything that stops here makes the tool
# unusable for the 90% of work that is generation and inspection.
MUST_NOT_GATE = [
    # Generation only -- the whole point of being able to work without approval.
    "genpipes rnaseq -t stringtie -s 1-5 -g cmd.sh",
    "module load mugqic/genpipes/6.1.1 && genpipes dnaseq -t germline_snv -g cmd.sh",
    # Reads.
    "squeue -u $USER",
    "sacct -j 41000001 --format=JobID,State",
    "genpipes dnaseq -h",
    "genpipes tools log_report --loglevel ERROR job_output/x.job_list.2026",
    "cat readset.tsv",
    "ls job_output/",
    # 2026-07-13, shell: the model summarised finished work by echoing the
    # command. The gate caught the summary, rejecting it produced another
    # summary, and the run could not leave the gate.
    'echo "  bash dnaseq_cmd.sh"',
    'echo "Next step: bash cmd.sh"',
    # 2026-07-14, python: a generation-only task ended with a "here is how you
    # would submit this" note, while explicitly not submitting. The gate fired
    # before the code ran, so the script was never even generated.
    'print("  bash launch_cmd.sh")',
    'print("To submit: bash cmd.sh")',
    # A comment is not a command.
    "# bash cmd.sh",
    "genpipes rnaseq -g cmd.sh   # then: bash cmd.sh",
    # A heredoc body is text being written to a file, not code being run. This
    # is how cmd.sh gets created in the first place.
    "cat > cmd.sh << 'EOF'\n#!/bin/bash\nbash inner.sh\nEOF\nchmod +x cmd.sh",
    # Nothing at all.
    "",
]


def main():
    r = Report("gate invariants")

    r.section("code that MUST be gated (a miss here submits unapproved work)")
    for code in MUST_GATE:
        r.check(f"gated: {code[:64]}", g.is_submission(code) is True)

    r.section("code that MUST NOT be gated (a hit here blocks ordinary work)")
    for code in MUST_NOT_GATE:
        r.check(f"free: {code[:64]!r}", g.is_submission(code) is False)

    r.section("extracting the proposal from a message")
    msgs = [Msg("Generating.\n" + "<execute>\n"
                "genpipes rnaseq -t stringtie -s 1-5 "
                "-c $MUGQIC/rnaseq.base.ini common_ini/rorqual.ini "
                "-r readset.tsv -g cmd.sh\n</execute>"),
            Msg("Now submitting.\n<execute>\nbash cmd.sh\n</execute>")]
    code = g.extract_pending_code(msgs)
    r.equal("pulls the last block's code", code.strip(), "bash cmd.sh")

    p = g.build_proposal(msgs, code)
    r.equal("command shown for approval", p["command"], "bash cmd.sh")
    r.equal("pipeline parsed from the earlier generation", p["slots"]["pipeline"], "rnaseq")
    r.equal("protocol parsed", p["slots"]["protocol"], "stringtie")
    r.equal("steps parsed", p["slots"]["steps"], "1-5")
    r.equal("readset parsed", p["slots"]["readset"], "readset.tsv")
    r.equal("script recorded", p["script"], "cmd.sh")
    r.check("both inis found", set(p["slots"]["inis"]) ==
            {"rnaseq.base.ini", "rorqual.ini"},
            f"got={p['slots']['inis']}")

    r.section("an unclosed tag is tolerated, not dropped")
    truncated = [Msg("<execute>\nbash cmd.sh")]
    r.truthy("still finds the code", g.extract_pending_code(truncated))
    r.check("and still gates it",
            g.is_submission(g.extract_pending_code(truncated)) is True)

    r.section("no generation command in history: the box still populates")
    only = [Msg("<execute>\nbash cmd.sh\n</execute>")]
    p = g.build_proposal(only, "bash cmd.sh")
    r.equal("command survives", p["command"], "bash cmd.sh")
    r.equal("unknown protocol is None, not a guess", p["slots"]["protocol"], None)

    r.section("after a rejection, the box shows the REVISED command")
    # Found by tests/test_lifecycle.py, 2026-07-25. The history now holds two
    # generations, and taking the first showed the stale one -- so rejecting
    # "steps 1-5", asking for 6-12, and being shown 1-5 again would look like
    # nothing had changed. Approving what you were not shown is the exact
    # failure the gate exists to prevent, so this is a permanent regression test.
    after_reject = [
        Msg("<execute>\ngenpipes rnaseq -t stringtie -s 1-5 -g cmd.sh\n</execute>"),
        Msg("<execute>\nbash cmd.sh\n</execute>"),
        Msg("The proposed submission was not approved. use steps 6-12 instead."),
        Msg("<execute>\ngenpipes rnaseq -t stringtie -s 6-12 -g cmd.sh\n</execute>"),
        Msg("<execute>\nbash cmd.sh\n</execute>"),
    ]
    p = g.build_proposal(after_reject, "bash cmd.sh")
    r.equal("the revised steps are shown", p["slots"]["steps"], "6-12")
    r.check("not the stale ones", p["slots"]["steps"] != "1-5")

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
