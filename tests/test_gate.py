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
by the case that caused them (see gate.executable_lines for the incidents).

Runs in CI on every push: gate imports nothing but the standard library,
so there is no biomni, no langgraph, no API key and no cluster involved.

Run:  python tests/test_gate.py
"""
import sys

from harness import Report

from genpipe import gate as g


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

    r.section("the box reads long-form flags too")
    # GenPipes accepts both, and a model writing the readable one is writing a
    # correct command. The box used to parse only the short form, so
    # `--output-dir /scratch/out` rendered as "output: cwd (no -o flag)" in red
    # on a command that really did set it -- wrong on the one screen whose whole
    # job is to say what is about to happen.
    long_form = [Msg("<execute>\ngenpipes chipseq --type chipseq "
                     "--output-dir /scratch/out --steps 1-4 "
                     "--readsets rs.tsv -g cmd.sh\n</execute>"),
                 Msg("<execute>\nbash cmd.sh\n</execute>")]
    p = g.build_proposal(long_form, "bash cmd.sh")
    r.equal("--type", p["slots"]["protocol"], "chipseq")
    r.equal("--steps", p["slots"]["steps"], "1-4")
    r.equal("--readsets", p["slots"]["readset"], "rs.tsv")
    r.equal("--output-dir", p["slots"]["output_dir"], "/scratch/out")
    # And the reason the long form has to be tried first: a short-form search
    # matches the `-o` inside `--output-dir` and returns the wrong token.
    r.equal("no phantom pairs file", p["slots"]["pairs"], None)

    equals = [Msg("<execute>\ngenpipes rnaseq --type=stringtie "
                  "--output-dir=/scratch/o -g cmd.sh\n</execute>"),
              Msg("<execute>\nbash cmd.sh\n</execute>")]
    p = g.build_proposal(equals, "bash cmd.sh")
    r.equal("--flag=value works too", p["slots"]["protocol"], "stringtie")
    r.equal("for the output directory as well",
            p["slots"]["output_dir"], "/scratch/o")

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

    # ---------------------------------------------------------------------
    r.section("ask(): the agent putting a question to the user")

    got = g.ask_request('ask(slot="protocol", pipeline="dnaseq")')
    r.equal("the slot is read", (got or {}).get("slot"), "protocol")
    r.equal("and the pipeline it is about", (got or {}).get("pipeline"), "dnaseq")

    r.equal("single quotes work too",
            (g.ask_request("ask(slot='readset')") or {}).get("slot"), "readset")
    r.equal("whitespace inside the call is fine",
            (g.ask_request('ask( slot = "pairs" )') or {}).get("slot"), "pairs")
    r.equal("a call split over lines still parses",
            (g.ask_request('ask(\n  slot="design",\n  pipeline="rnaseq",\n)')
             or {}).get("slot"), "design")

    free = g.ask_request('ask(question="Which steps should this run?")')
    r.equal("a free-form question has no slot", (free or {}).get("slot"), None)
    r.equal("but keeps its wording",
            (free or {}).get("question"), "Which steps should this run?")

    # An invented slot is not silently dropped. The panel has no table for
    # "genome", but the question is still worth putting to a human, so it
    # degrades to free text rather than to nothing happening.
    made_up = g.ask_request('ask(slot="genome")')
    r.equal("an unknown slot gets no menu", (made_up or {}).get("slot"), None)
    r.equal("and becomes a plain question",
            (made_up or {}).get("question"), "Which genome?")

    r.equal("no ask, no request", g.ask_request("genpipes rnaseq -g cmd.sh"), None)
    r.equal("nothing at all", g.ask_request(""), None)
    # Once the call is there, this ALWAYS returns a request. Returning None for an
    # argument list it could not read is what used to send `ask(...)` to the Python
    # interpreter, where it came back as "NameError: name 'ask' is not defined".
    bare = g.ask_request("ask()")
    r.truthy("a bare ask() is still recognised as a question", bare is not None)
    r.equal("with nothing to ask about", (bare or {}).get("question"), None)

    positional = g.ask_request('ask("Which steps should this run?")')
    r.equal("a positional question is read as the question",
            (positional or {}).get("question"), "Which steps should this run?")
    r.equal("a positional slot name is read as the slot",
            (g.ask_request('ask("protocol")') or {}).get("slot"), "protocol")
    r.equal("an f-string value is not a parse failure",
            (g.ask_request('ask(question=f"Which steps of {p}?")') or {}
             ).get("question"), "Which steps of {p}?")

    # The same inertness rules the gate lives by. The model narrates its own
    # work constantly, and a described ask must not open a real panel.
    r.equal("an ask inside print() is not an ask",
            g.ask_request('print("ask(slot=\'protocol\')")'), None)
    r.equal("an ask inside echo is not an ask",
            g.ask_request("echo ask(slot='protocol')"), None)
    r.equal("an ask in a comment is not an ask",
            g.ask_request('# ask(slot="protocol") would be the next move'), None)

    # The asymmetry that matters. A block doing both is a submission, and the
    # router tests is_submission first for exactly this reason -- an ask() must
    # never be a way to route work past the gate.
    both = 'ask(slot="protocol")\nbash cmd.sh'
    r.truthy("a block that also submits is still a submission",
             g.is_submission(both))
    r.truthy("even though an ask can be parsed out of it",
             g.ask_request(both) is not None)

    r.section("build_proposal(): missing is the required-slot check, not "
              "just a display of what's present")
    # The two shapes that reached a real gate with something essential absent
    # and nothing saying so -- see AGENT-FIXES.md / GATE-FIX.md.
    dnaseq_no_readset = g.build_proposal(
        [Msg("<execute>genpipes dnaseq -t germline_snv -s 1-5 -d design.tsv "
             "-g cmd.sh</execute>")],
        "bash cmd.sh")
    r.equal("a design with no readset is still missing the readset",
            dnaseq_no_readset["missing"], ["readset"])

    ampliconseq_bare = g.build_proposal(
        [Msg("<execute>genpipes ampliconseq -g cmd.sh</execute>")],
        "bash cmd.sh")
    # Both, now. ampliconseq takes no `-t`, so its need for a design had
    # nowhere to live -- `needs` hangs off a protocol -- and the gate reported
    # a complete command for one that cannot generate past step 7 (`asva`
    # reads self.contrasts, and design_file raises when there is no -d).
    r.equal("a bare command is missing the readset and the design",
            ampliconseq_bare["missing"], ["readset", "design"])

    rnaseq_no_design = g.build_proposal(
        [Msg("<execute>genpipes rnaseq -t stringtie -r readset.tsv "
             "-g cmd.sh</execute>")],
        "bash cmd.sh")
    r.equal("stringtie without a design is missing the design",
            rnaseq_no_design["missing"], ["design"])

    complete = g.build_proposal(
        [Msg("<execute>genpipes dnaseq -t germline_snv -s 1-5 "
             "-r readset.tsv -g cmd.sh</execute>")],
        "bash cmd.sh")
    r.equal("nothing missing once the required slots are all filled",
            complete["missing"], [])

    r.section("an omitted -t is not missing, but it is still stated")
    # The trade this pins. slots.DEFAULTS now carries GenPipes' own `-t`
    # defaults, so a command without `-t` is complete and `missing` says so --
    # which is true, and which quietly removed the only line on the approval box
    # that mentioned the protocol at all. What replaces it has to be visible,
    # because dnaseq defaults to GERMLINE: a tumour/normal cohort approved with
    # no `-t` generates, submits, finishes, and answers a different question.
    # Nobody reading the box could have caught it.
    assumed = g.build_proposal(
        [Msg("<execute>genpipes dnaseq -r readset.tsv -g cmd.sh</execute>")],
        "bash cmd.sh")
    r.equal("no -t is not a missing slot", assumed["missing"], [])
    r.contains("but the box names the protocol that will run",
               assumed["explanation"], "germline_snv")
    r.contains("and says it was assumed rather than asked for",
               assumed["explanation"], "assumed, no -t given")
    # The RECORD keeps describing the command as written. Filling the slot in
    # would put a `-t` in the run history that is not in the command, and no
    # later reader could tell a defaulted run from a chosen one.
    r.equal("the slot itself stays empty, because the flag is",
            assumed["slots"]["protocol"], None)

    stated = g.build_proposal(
        [Msg("<execute>genpipes dnaseq -t somatic_fastpass -r r.tsv "
             "-p pairs.csv -g cmd.sh</execute>")],
        "bash cmd.sh")
    r.contains("a stated protocol is shown as stated",
               stated["explanation"], "protocol: somatic_fastpass")
    r.truthy("and is not labelled an assumption",
             "assumed" not in stated["explanation"])

    # A pipeline GenPipes gives no default for must claim nothing. covseq takes
    # no `-t` at all, so there is no protocol to assume and inventing a line
    # here would be inventing a fact.
    bare = g.build_proposal(
        [Msg("<execute>genpipes covseq -r r.tsv -g cmd.sh</execute>")],
        "bash cmd.sh")
    r.truthy("a pipeline with no default assumes nothing",
             "protocol:" not in bare["explanation"])

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
