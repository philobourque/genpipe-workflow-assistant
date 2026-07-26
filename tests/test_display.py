#!/usr/bin/env python
"""Every renderer, run once with realistic data.

Added after a rename left a stale variable in display.jobs()'s footer, which no
suite caught: the CI tests never imported the renderers, and the two that did
only reached that branch through a full agent run. A whole module of the product
had zero direct coverage, and its failure mode is a NameError in the middle of
the interface -- after a real submission, on a real cluster.

So this is deliberately shallow and broad rather than deep: call everything, with
data shaped the way the real callers shape it, and assert on the facts a user
must be able to read off the screen. Rendering is checked for content, never for
exact layout -- pinning byte-for-byte output would make every visual tweak a test
failure, which trains people to stop reading them.

Stdlib only, so it runs in CI.

Run:  python tests/test_display.py
"""
import io
import sys
from contextlib import redirect_stdout

from harness import Report

import display
import runs


def drawn(fn, *args, **kwargs):
    """Call a renderer and return what it printed, ANSI stripped."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*args, **kwargs)
    import re
    return re.sub(r"\033\[[0-9;]*[A-Za-z]", "", buf.getvalue())


class Msg:
    def __init__(self, content):
        self.content = content


class HumanMessage(Msg):
    pass


def job(job_id, name, state, elapsed="00:14:22", maxrss=None):
    j = runs.Job(job_id=job_id, name=name, log=f"{name.split('.')[0]}/x.o")
    j.state, j.elapsed, j.maxrss = state, elapsed, maxrss
    return j


def main():
    r = Report("every renderer")

    # ---------------------------------------------------------------- #
    r.section("the gate: what is being approved, and how to answer")
    proposal = {
        "command": "bash cmd.sh",
        "script": "cmd.sh",
        "slots": {"pipeline": "rnaseq", "protocol": "stringtie", "steps": "1-5",
                  "inis": ["rnaseq.base.ini", "rorqual.ini"], "design": None,
                  "pairs": None, "readset": "readset.tsv", "output_dir": None},
    }
    out = drawn(display.gate, proposal, "patient-42")
    r.contains("announces the hold", out, "HOLD")
    r.contains("shows the command", out, "bash cmd.sh")
    r.contains("the protocol", out, "stringtie")
    r.contains("the steps", out, "1-5")
    r.contains("both config files", out, "rorqual.ini")
    r.contains("how to approve", out, "/approve patient-42")
    r.contains("how to reject", out, "/reject patient-42")
    r.contains("and that nothing has happened yet", out,
               "Nothing has reached the scheduler")
    r.contains("with no -o, it says where output will land", out, "no -o flag")

    # ---------------------------------------------------------------- #
    r.section("held runs, surfaced at startup")
    out = drawn(display.pending, [
        {"name": "patient-42", "proposal": {"command": "bash cmd.sh"}}])
    r.contains("counts them", out, "1 run held")
    r.contains("names them", out, "patient-42")
    r.contains("and says where to look", out, "/list")
    r.equal("nothing at all when nothing is held",
            drawn(display.pending, []).strip(), "")

    # A startup notice is a reminder, not a report. Nine held runs from a
    # fortnight of experiments must not be nine lines above the prompt.
    many = drawn(display.pending,
                 [{"name": f"run-{i}", "proposal": {"command": "bash cmd.sh"}}
                  for i in range(9)])
    r.equal("nine held runs still take one line",
            len([l for l in many.splitlines() if l.strip()]), 1)
    r.contains("the count is all of them", many, "9 runs held")
    r.contains("naming the first few", many, "run-0")
    r.contains("and saying how many it left out", many, "+6 more")
    r.check("no commands are echoed", "bash cmd.sh" not in many)

    # ---------------------------------------------------------------- #
    r.section("/list distinguishes held from live")
    records = [
        {"name": "waiting", "status": "held", "held_at": "2026-07-25T09:00:00",
         "submitted_at": None, "job_list": None,
         "proposal": {"command": "bash cmd.sh"}},
        {"name": "running-one", "status": "submitted", "held_at": None,
         "submitted_at": "2026-07-25T10:00:00",
         "job_list": "/s/job_output/RnaSeq.stringtie.job_list.T1",
         "proposal": None,
         "last_check": {"at": "2026-07-25T10:30:00", "verdict": "6 running",
                        "counts": {}, "total": 15}},
        {"name": "nothing-to-do", "status": "submitted", "held_at": None,
         "submitted_at": "2026-07-25T11:00:00", "job_list": None,
         "proposal": None},
    ]
    out = drawn(display.run_list, records)
    r.contains("the held run is marked held", out, "held")
    r.contains("with what it is waiting for", out, "awaiting your approval")
    r.check("and it sorts first, because it needs a person",
            out.index("waiting") < out.index("running-one"))
    r.contains("a live run is marked live", out, "live")
    r.contains("with its cached verdict", out, "6 running")
    r.contains("labelled as a snapshot, not live truth", out, "as of")
    r.contains("a zero-job run says so plainly", out, "already")
    r.contains("and the next commands are offered", out, "/why")

    # ---------------------------------------------------------------- #
    r.section("/history keeps gone runs and their findings")
    out = drawn(display.history, [
        {"name": "old-one", "status": "gone", "source": "agent",
         "submitted_at": "2026-06-01T10:00:00", "held_at": None,
         "job_list": "/s/job_output/X.job_list.T0",
         "notes": [{"at": "2026-06-01T11:00:00",
                    "text": "OOM in picard_mark_duplicates: raise java heap"}]},
    ])
    r.contains("names the run", out, "old-one")
    r.contains("marked gone", out, "gone")
    r.contains("and the finding survives with it", out, "picard_mark_duplicates")

    # ---------------------------------------------------------------- #
    r.section("/check draws the run's progress")
    parsed = runs.parse_log_report(
        "Number of jobs: 15\n"
        "Number of jobs COMPLETED: 6\n"
        "Number of jobs OUT_OF_MEMORY: 3\n"
        "Number of jobs CANCELLED: 6\n"
        "Cumulative core time: 27:13:28\n")
    out = drawn(display.status, "patient-42", parsed, "")
    r.contains("names the run", out, "patient-42")
    r.contains("says something needs attention", out, "need attention")
    r.contains("shows a percentage", out, "40%")
    r.contains("breaks down by state", out, "out_of_memory")
    r.contains("and keeps the timing", out, "core time")

    r.section("...and shows raw text rather than faking a bar")
    out = drawn(display.status, "patient-42",
                runs.parse_log_report("module: command not found"),
                "module: command not found")
    r.contains("the unparsed output is shown", out, "command not found")
    r.check("and no progress bar was invented", "%" not in out)

    # ---------------------------------------------------------------- #
    r.section("/jobs groups by step and separates broke from cancelled")
    jobs = [
        job("1", "trimmomatic.sampleA", "COMPLETED"),
        job("2", "trimmomatic.sampleB", "COMPLETED"),
        job("3", "picard_mark_duplicates.sampleA", "OUT_OF_MEMORY", maxrss="8192000K"),
        job("4", "gatk_haplotype_caller.sampleA", "CANCELLED"),
        job("5", "gatk_haplotype_caller.sampleB", "CANCELLED"),
    ]
    out = drawn(display.jobs, "patient-42", jobs)
    r.contains("counts what broke", out, "1 failed")
    r.contains("and the cancelled ones separately", out, "2 cancelled downstream")
    r.check("without claiming three failures",
            "3 failed" not in out, out.splitlines()[1] if out else "")
    r.contains("groups by step", out, "picard_mark_duplicates")
    r.contains("shows per-job state", out, "out_of_memory")
    r.contains("and the memory that explains it", out, "8192000K")
    r.contains("offering the diagnosis", out, "/why patient-42")

    r.section("...and filters to failures on request")
    out = drawn(display.jobs, "patient-42", jobs, only_failed=True)
    r.contains("keeps the failure", out, "picard_mark_duplicates")
    r.check("drops the healthy step", "trimmomatic" not in out)
    r.contains("nothing to show is said, not left blank",
               drawn(display.jobs, "clean",
                     [job("1", "trimmomatic.sampleA", "COMPLETED")],
                     only_failed=True),
               "No failed jobs")

    r.section("an unknown state does not render as healthy")
    out = drawn(display.jobs, "mystery", [job("9", "trimmomatic.sampleA", None)])
    r.contains("it is shown as unknown", out, "unknown")

    # ---------------------------------------------------------------- #
    r.section("triage prints the evidence before any model speaks")
    out = drawn(display.triage, "patient-42", {
        "failed_total": 3, "broke_total": 1, "cancelled_total": 2,
        "steps_affected": 2, "truncated": 0,
        "findings": [{"step": "picard_mark_duplicates", "count": 1,
                      "job": "picard_mark_duplicates.sampleA", "job_id": "3",
                      "state": "OUT_OF_MEMORY", "maxrss": "8192000K",
                      "exit_code": "0:125",
                      "log": "/s/job_output/picard/x_3.o",
                      "log_tail": "java.lang.OutOfMemoryError"}]})
    r.contains("counts what broke", out, "1 failed")
    r.contains("names the step", out, "picard_mark_duplicates")
    r.contains("the peak memory", out, "8192000K")
    r.contains("the exit code", out, "0:125")
    r.contains("and the log it read", out, "x_3.o")

    # ---------------------------------------------------------------- #
    r.section("the small messages always offer a way forward")
    out = drawn(display.problem, "No run named 'patient-4'.", "/list shows what there is.")
    r.contains("states the problem", out, "No run named")
    r.contains("and the next step", out, "/list")
    r.contains("an empty answer is not an error",
               drawn(display.nothing, "No runs recorded yet."), "No runs recorded")
    r.contains("a completed action is confirmed",
               drawn(display.done, "Cancelled 3 jobs"), "Cancelled 3 jobs")
    r.contains("cancelling nothing says so",
               drawn(display.cancelled, "patient-42", 0), "Nothing left to cancel")
    r.contains("cancelling something reports the count",
               drawn(display.cancelled, "patient-42", 7, ""), "7")
    r.contains("tracking confirms the file",
               drawn(display.tracked, "adopted", "/s/job_output/X.job_list.T0"),
               "job_list")

    # ---------------------------------------------------------------- #
    r.section("/where names the directories that decide where things land")
    out = drawn(display.where, [("launched from", "/scratch/me/project"),
                                ("run registry", "/scratch/biomni_data/runs.jsonl")])
    r.contains("the launch directory", out, "/scratch/me/project")
    r.contains("and the registry", out, "runs.jsonl")

    # ---------------------------------------------------------------- #
    r.section("/help is grouped, and lists everything it is given")
    out = drawn(display.help_text, [
        ("approve", "<name>", "let it through", "deciding"),
        ("check", "<name>", "how it is doing", "watching"),
        ("why", "<name>", "diagnose it", "fixing"),
    ])
    for word in ("deciding", "watching", "fixing", "/approve", "/check", "/why"):
        r.contains(f"shows {word}", out, word)

    # ---------------------------------------------------------------- #
    r.section("the banner fits its frame at every sane width")
    import os as _os
    for cols in (60, 80, 92, 100, 120, 160):
        _os.environ["COLUMNS"] = str(cols)
        out = drawn(display.banner, "Anthropic", "claude-sonnet-5")
        framed = [l for l in out.splitlines() if l.startswith(" │")]
        widths = {len(l.rstrip()) for l in framed}
        r.check(f"borders line up at {cols} columns",
                len(widths) <= 1, f"row widths={sorted(widths)}")
        r.contains(f"still says what it is at {cols}", out, "GenPipes")
    _os.environ.pop("COLUMNS", None)

    r.section("dev mode is stated loudly, and only when it applies")
    r.contains("announced when faking",
               drawn(display.ready, "Anthropic", "claude-sonnet-5",
                     fake="fake cluster (happy)"), "dev mode")
    r.check("and silent when not",
            "dev mode" not in drawn(display.ready, "Anthropic", "claude-sonnet-5"))

    # ---------------------------------------------------------------- #
    r.section("the transcript parser separates what the model said from what ran")
    events = display.parse(Msg(
        "Let me generate it.\n"
        "1. [x] read the readset\n2. [ ] generate\n"
        "<execute>\ngenpipes rnaseq -g cmd.sh\n</execute>\n"
        "<observation>Generated cmd.sh</observation>\n"
        "<solution>Done.</solution>"))
    kinds = [e["kind"] for e in events]
    for kind in ("note", "plan", "code", "observation", "solution"):
        r.check(f"found the {kind}", kind in kinds, f"got={kinds}")
    plan = next(e for e in events if e["kind"] == "plan")
    r.equal("ticked items are marked done", plan["items"][0][1], True)
    r.equal("and unticked ones are not", plan["items"][1][1], False)
    r.check("drawing all of it raises nothing",
            drawn(display.render, Msg("<solution>ok</solution>")) is not None)

    # ---------------------------------------------------------------- #
    r.section("a question is rendered as a panel, never as code")

    # An <execute> block that only asks something never reaches an interpreter.
    # Printing `RUN ask(slot="protocol")` just above the panel would show the
    # plumbing instead of the question.
    events = display.parse(Msg(
        'I need to know which one.\n<execute>\nask(slot="protocol", '
        'pipeline="dnaseq")\n</execute>'))
    kinds = [e["kind"] for e in events]
    r.check("the ask block is not drawn as code", "code" not in kinds,
            f"got={kinds}")
    r.check("the prose around it survives", "note" in kinds, f"got={kinds}")
    r.check("and nothing of the call leaks into it",
            "ask(" not in drawn(display.render, Msg(
                'Which one?\n<execute>\nask(slot="protocol")\n</execute>')))

    # The answer does show. A transcript that hid both the question and the
    # answer would leave the model's next move unexplained.
    r.check("the answer comes back visibly",
            "stringtie" in drawn(display.render, Msg(
                "<observation>The user answered: stringtie</observation>")))

    # A block mixing an ask with real code is not an ask -- the router will not
    # treat it as one either -- so it must be shown in full.
    mixed = display.parse(Msg('<execute>\nask(slot="protocol")\nbash cmd.sh\n</execute>'))
    r.check("a block that also runs something is shown",
            "code" in [e["kind"] for e in mixed])

    # ---------------------------------------------------------------- #
    r.section("a block is titled by what it does, not by the fact that it runs")

    def label(code):
        return display.parse(Msg(f"<execute>\n{code}\n</execute>"))[0]["label"]

    r.equal("writing the script is GENERATE",
            label("module load mugqic/genpipes/6.1.1 && genpipes rnaseq "
                  "-t stringtie -s 1-5 -g cmd.sh"), "GENERATE")
    r.equal("running it is SUBMIT", label("bash cmd.sh"), "SUBMIT")
    r.equal("so is the DRAC pair",
            label("./chunk_genpipes.sh chunks && ./submit_genpipes chunks"),
            "SUBMIT")
    r.equal("reading the scheduler is SCHEDULER",
            label("sacct -j 1234 --format=State"), "SCHEDULER")
    r.equal("and GenPipes' own progress report too",
            label("genpipes tools log_report cmd.sh.job_list"), "SCHEDULER")
    # An unrecognised command is the one to read closely; dressing it up as
    # something familiar would be the wrong kind of help.
    r.equal("anything else stays CODE", label("python summarise.py"), "CODE")
    r.equal("a documentation lookup is HELP",
            label("module load mugqic/genpipes/6.1.1 && genpipes rnaseq --help"),
            "HELP")
    r.contains("and the label is what gets drawn", drawn(
        display.render, Msg("<execute>\nbash cmd.sh\n</execute>")), "SUBMIT")

    # ---------------------------------------------------------------- #
    r.section("long machine output is clipped at both ends, not one")
    flood = "<observation>" + "\n".join(f"line {i}" for i in range(60)) + "</observation>"
    out = drawn(display.render, Msg(flood))
    r.contains("the start survives", out, "line 0")
    r.contains("the end survives -- where an error is", out, "line 59")
    r.contains("and it says how much it left out", out, "more lines")
    r.check("the middle is gone", "line 30" not in out)
    short = drawn(display.render, Msg("<observation>one\ntwo</observation>"))
    r.contains("short output is untouched", short, "two")
    r.check("with nothing about lines left out", "more lines" not in short)

    # ---------------------------------------------------------------- #
    r.section("what the graph says to itself is never put in the user's mouth")

    # Every message the graph sends the model is user-role -- the API has no
    # other channel for it. Drawing any of it under the person's name would be
    # claiming they typed it.
    me = display.who().upper()

    scold = drawn(display.render, HumanMessage(
        "Each response must include thinking process followed by either "
        "<execute> or <solution> tag. But there are no tags in the current "
        "response. Please follow the instruction, fix and regenerate."))
    # Not attributed, and in fact not drawn at all: it is the harness telling the
    # model off about the harness's own tagging rules, and the reply that provoked
    # it is on screen immediately above.
    r.equal("biomni's correction is not drawn", scold.strip(), "")
    r.check("least of all as code about to run", "CODE" not in scold)

    rejected = drawn(display.render, HumanMessage(
        "The proposed submission was not approved. use steps 6-12 instead. "
        "Regenerate the command accordingly."))
    r.check("nor is the rejection sent back", me not in rejected)
    r.contains("though the feedback is visible", rejected, "steps 6-12")

    # Command output arrives as a user turn now (the Anthropic API rejects a
    # conversation that ends on the assistant's side), so it must render as OUT
    # from either role.
    from_machine = drawn(display.render,
                         HumanMessage("<observation>Generated cmd.sh</observation>"))
    r.contains("output on the user channel is still TERMINAL", from_machine,
               "TERMINAL")
    r.check("and is not attributed to them", me not in from_machine)

    r.equal("the continue nudge is not drawn at all",
            display.parse(HumanMessage("[continue]")), [])

    # intake.brief appends what it could establish about the request. It is for
    # the model; showing it back reads as if they had typed an inventory.
    import intake
    briefed = drawn(display.render, HumanMessage(
        intake.brief("run rnaseq stringtie with readset.tsv", ".")))
    r.contains("their own words are shown", briefed, "run rnaseq stringtie")
    r.check("the appended context is not", "do not ask again" not in briefed)
    r.contains("and it is still their turn", briefed, me)

    text = drawn(display.fresh, [])
    r.check("/new says the conversation is gone", "New conversation" in text)
    r.check("and that the runs are not", "keeps its name" in text)
    held = drawn(display.fresh, [{"name": "rnaseq-stringtie-0726"}])
    r.check("a held run is named on the way out",
            "rnaseq-stringtie-0726" in held)

    # ---------------------------------------------------------------- #
    r.section("environment findings, and the gate's refusal to offer approval")

    import preflight

    r.equal("a sound environment prints nothing",
            drawn(display.environment, []).strip(), "")

    warn = preflight.check_job_mail("x@gmail.coma")
    text = drawn(display.environment, [warn])
    r.check("a warning names the variable", "JOB_MAIL" in text)
    r.check("and offers the fix line", "export JOB_MAIL=" in text)
    r.check("and does not claim to block", "BLOCKS SUBMISSION" not in text)

    block = preflight.check_rap_id("")
    text = drawn(display.environment, [block])
    r.check("a blocker says so plainly", "BLOCKS SUBMISSION" in text)

    proposal = {"command": "bash cmd.sh", "slots": {"protocol": "germline_snv"}}
    clean = drawn(display.gate, proposal, "run-1")
    r.check("a normal gate offers approve", "/approve run-1" in clean)

    # The point of the blocked gate: the command that cannot work must not be
    # on screen next to the explanation of why it cannot work.
    stopped = drawn(display.gate, proposal, "run-1", blockers=[block])
    r.check("a blocked gate withholds approve", "/approve" not in stopped)
    r.check("but still allows reject", "/reject run-1" in stopped)
    r.check("and says what to fix", "RAP_ID" in stopped)
    r.check("and confirms nothing was spent",
            "Nothing has reached the scheduler" in stopped)

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
