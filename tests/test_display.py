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

from genpipe import display
from genpipe import runs


def drawn(fn, *args, **kwargs):
    """Call a renderer and return what it printed, ANSI stripped."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*args, **kwargs)
    import re
    return re.sub(r"\033\[[0-9;]*[A-Za-z]", "", buf.getvalue())


def loud(fn, *args, **kwargs):
    """drawn(), with the transcript unfolded.

    The working -- commands, machine output, connective prose -- is folded away
    by default now, the way a chain of thought is. Everything that asserts on
    what a code block or an observation LOOKS like is asserting about the
    unfolded view, so it says so rather than relying on a default that has
    deliberately changed.
    """
    was = display.VERBOSE
    display.set_verbose(True)
    try:
        return drawn(fn, *args, **kwargs)
    finally:
        display.set_verbose(was)


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
    # The verbs, without the name. The name used to be repeated on each action
    # line -- `/approve patient-42` -- which put it on this screen four times
    # and grew the instructions to eight lines under a six-line command. It is
    # on the mirror's own `name` row once, and the prompt completes it, so the
    # action block is left saying only what each verb DOES.
    r.contains("how to approve", out, "/approve")
    r.contains("how to reject", out, "/reject")
    r.contains("how to modify", out, "/modify")
    r.check("without repeating the name on every line",
            out.count("patient-42") == 1, f"counted {out.count('patient-42')}")
    r.contains("and says the name is a keystroke away", out,
               "tab completes the name")
    r.contains("and that nothing has happened yet", out,
               "Nothing has reached the scheduler")
    # The wording moved into mirror._absent when the gate started drawing the
    # command as structure. The assertion is on where the output lands, not on
    # the phrase: the old "cwd (no -o flag)" named the flag and left the reader
    # to know what cwd meant.
    r.contains("with no -o, it says where output will land", out,
               "current directory")

    # ---------------------------------------------------------------- #
    r.section("the gate withholds /approve when something required is missing")
    incomplete = {
        "command": "bash cmd.sh",
        "script": "cmd.sh",
        "missing": ["readset"],
        "slots": {"pipeline": "dnaseq", "protocol": "germline_snv",
                  "steps": "1-5", "inis": [], "design": None, "pairs": None,
                  "readset": None, "output_dir": None},
    }
    out = drawn(display.gate, incomplete, "no-readset-run")
    r.check("no /approve offered", "/approve" not in out, out)
    r.contains("but /modify still is", out, "/modify")
    r.contains("but /reject still is", out, "/reject")
    r.contains("the missing row is drawn, not silently skipped", out, "readset")
    r.contains("and marked required", out, "required")

    # ---------------------------------------------------------------- #
    r.section("held runs, surfaced at startup")
    out = drawn(display.pending, [
        {"name": "patient-42", "proposal": {"command": "bash cmd.sh"}}])
    r.contains("counts them", out, "1 run held")
    r.contains("and says where to look", out, "/list")
    r.equal("nothing at all when nothing is held",
            drawn(display.pending, []).strip(), "")

    # A startup notice is a reminder, not a report. Nine held runs from a
    # fortnight of experiments must not be nine lines above the prompt -- and
    # naming even three of them was still a report: stale names, no news in any
    # of them, occupying the line nearest the cursor.
    many = drawn(display.pending,
                 [{"name": f"run-{i}", "proposal": {"command": "bash cmd.sh"}}
                  for i in range(9)])
    r.equal("nine held runs still take one line",
            len([l for l in many.splitlines() if l.strip()]), 1)
    r.contains("the count is all of them", many, "9 runs held")
    r.check("but no names -- that is what /list is for", "run-0" not in many)
    r.contains("so it points at /list instead", many, "/list")
    r.check("no commands are echoed", "bash cmd.sh" not in many)

    # What changed on its own IS news, and keeps its names: there are rarely
    # many, and each one is something you did not know a moment ago.
    news = drawn(display.pending, [], since={
        "failed": [{"name": "rnaseq-0803"}]})
    r.contains("a run that failed while away is named", news, "rnaseq-0803")

    # ---------------------------------------------------------------- #
    r.section("the invitation sits nearest the prompt")
    out = drawn(display.welcome)
    r.contains("it asks rather than reports", out, "What can I help you with today?")
    r.contains("and says what an answer looks like", out, "describe the run you want")
    for cmd in ("/help", "/list", "/check all"):
        r.contains(f"offering {cmd}", out, cmd)

    # ---------------------------------------------------------------- #
    r.section("/list tags each row with its lifecycle state, not its raw status")
    # rows mirrors runs_store.resolve_all()'s own shape -- [(record, RunStatus
    # or None), ...] -- since that is what agent.submissions() now hands to
    # display.run_list() after its one batched scheduler call.
    held = {"name": "waiting", "status": "held", "held_at": "2026-07-25T09:00:00",
            "submitted_at": None, "job_list": None,
            "proposal": {"command": "bash cmd.sh"}}
    running_record = {"name": "running-one", "status": "submitted", "held_at": None,
                      "submitted_at": "2026-07-25T10:00:00",
                      "job_list": "/s/job_output/RnaSeq.stringtie.job_list.T1"}
    running_status = runs.RunStatus(
        counts={"RUNNING": 6, "COMPLETED": 9}, total=15, resolved=15,
        unknown=0, finished=False, verdict="6 running", doomed=0,
        source="sacct", at="09:15")
    mixed_record = {"name": "half-broken", "status": "submitted", "held_at": None,
                    "submitted_at": "2026-07-25T09:30:00",
                    "job_list": "/s/job_output/DnaSeq.job_list.T2"}
    mixed_status = runs.RunStatus(
        counts={"FAILED": 3, "RUNNING": 2}, total=5, resolved=5, unknown=0,
        finished=False, verdict="3 need attention", doomed=0, source="sacct")
    dead_record = {"name": "fully-dead", "status": "submitted", "held_at": None,
                   "submitted_at": "2026-07-24T09:00:00",
                   "job_list": "/s/job_output/DnaSeq.job_list.T3"}
    dead_status = runs.RunStatus(
        counts={"FAILED": 2}, total=2, resolved=2, unknown=0, finished=True,
        verdict="failed, nothing still running", doomed=0, source="sacct")
    finished_record = {"name": "all-done", "status": "submitted", "held_at": None,
                       "submitted_at": "2026-07-23T09:00:00",
                       "job_list": "/s/job_output/RnaSeq.job_list.T4"}
    finished_status = runs.RunStatus(
        counts={"COMPLETED": 10}, total=10, resolved=10, unknown=0,
        finished=True, verdict="complete", doomed=0, source="sacct")
    up_to_date = {"name": "nothing-to-do", "status": "submitted", "held_at": None,
                  "submitted_at": "2026-07-25T11:00:00", "job_list": None}
    stopped_record = {"name": "i-stopped-it", "status": "submitted", "held_at": None,
                      "submitted_at": "2026-07-22T09:00:00",
                      "job_list": "/s/job_output/DnaSeq.job_list.T6"}
    stopped_status = runs.RunStatus(
        counts={"COMPLETED": 4, "CANCELLED": 6}, total=10, resolved=10,
        unknown=0, finished=True, verdict="cancelled", doomed=0, source="sacct")
    unreachable_record = {"name": "cant-tell", "status": "submitted", "held_at": None,
                          "submitted_at": "2026-07-25T08:00:00",
                          "job_list": "/s/job_output/RnaSeq.job_list.T5",
                          "last_check": {"at": "2026-07-25T07:00:00",
                                        "verdict": "6 running", "counts": {},
                                        "total": 15}}
    unreachable_status = runs.RunStatus(
        counts={}, total=15, resolved=0, unknown=15, finished=False,
        verdict="scheduler unreachable", doomed=0, source="unavailable")

    rows = [(held, None), (running_record, running_status),
            (mixed_record, mixed_status), (dead_record, dead_status),
            (finished_record, finished_status), (up_to_date, None),
            (stopped_record, stopped_status),
            (unreachable_record, unreachable_status)]
    out = drawn(display.run_list, rows)

    def row(name):
        """The name's own line, so a tag is asserted against ITS run rather
        than against anything else that happens to be on screen."""
        return next(l for l in out.splitlines() if name in l)

    r.check("one flat table, no section headings",
            "Awaiting approval" not in out and "Needs attention" not in out)
    r.check("one fact per column, each populated on every row",
            "PROGRESS" in out and "AGE" in out and "STATUS" in out)
    # OK/FAIL/RUN looked like three facts and carried about one: RUN was 0 or
    # a dash on every row that was not live, and OK had no denominator, so its
    # number meant nothing without one.
    r.check("and no bare counts without a denominator",
            "OK" not in out and "FAIL" not in out)

    def cells_of(name):
        """A row's fields, whitespace collapsed -- what the columns say,
        without asserting on the exact padding that keeps them aligned."""
        return row(name).split()

    r.contains("a held run says what it is waiting for",
               row("waiting"), "waiting for approval")
    r.check("marked as waiting on a person, not as broken",
            cells_of("waiting")[0] == "◇")
    r.check("and its progress is a dot, not 0/0 -- it has no jobs to have done",
            "·" in cells_of("waiting") and "0/0" not in row("waiting"))
    r.check("and shows only the name and its state, no command",
            "bash cmd.sh" not in out)
    r.check("with no repeated per-row actions",
            out.count("/approve") == 1 and out.count("/modify") == 1
            and out.count("/reject") == 1)

    # The column the old listing had no equivalent of. Ten runs held, the
    # oldest for a fortnight, is the actual state of a workspace, and it used
    # to be nowhere on screen.
    r.check("every row says how long it has been sitting there",
            all(any(c.endswith(("m", "h", "d")) and c[:-1].isdigit()
                    for c in cells_of(n))
                for n in ("waiting", "running-one", "half-broken", "all-done")))

    r.contains("a truly active run says it is running", row("running-one"), "running")
    r.check("and carries its own mark", cells_of("running-one")[0] == "▶")
    r.check("its progress is a fraction, so the number means something",
            "9/15" in cells_of("running-one"))

    r.check("a mixed active+failed run is marked broken, not live",
            cells_of("half-broken")[0] == "✗")
    r.check("its row leads with the failure, not with what is still going",
            row("half-broken").index("failed")
            < row("half-broken").index("still running"))
    r.contains("its still-running jobs are named, not just the failures",
               row("half-broken"), "still running")
    r.contains("a fully dead run says it failed", row("fully-dead"), "failed")
    # "nothing still running" is what usually follows a failure. Saying it on
    # every broken row spent the widest column on screen restating the expected
    # case; the surprise -- jobs still burning allocation after a failure -- is
    # the half worth the width, and it is still said above.
    r.check("and does not spend the column restating the ordinary case",
            "nothing still running" not in row("fully-dead"))

    # One vocabulary, in the same place on every row, so the column reads down.
    r.check("every row leads with its state in the same words",
            all(any(w in row(n) for w in (
                "waiting for approval", "running", "queued", "failed",
                "completed", "stopped", "nothing to run", "unknown"))
                for n in ("waiting", "running-one", "half-broken",
                          "fully-dead", "all-done", "i-stopped-it",
                          "nothing-to-do", "cant-tell")))

    # resolve() already worked out which step broke and how. The old listing
    # computed none of it and printed a count in a FAIL column instead, which
    # is the same width and answers a strictly smaller question.
    timeout_record = {"name": "timed-out", "status": "submitted", "held_at": None,
                      "submitted_at": "2026-07-24T09:00:00",
                      "job_list": "/s/job_output/DnaSeq.job_list.T7"}
    timeout_status = runs.RunStatus(
        counts={"COMPLETED": 1, "TIMEOUT": 2}, total=44, resolved=3, unknown=0,
        finished=True, verdict="failed", doomed=0, source="sacct",
        root_cause={"step": "gatk_haplotype_caller", "state": "TIMEOUT",
                    "count": 2, "job": "gatk_haplotype_caller.S1"})
    cause_out = drawn(display.run_list, [(timeout_record, timeout_status)])
    cause_row = next(l for l in cause_out.splitlines() if "timed-out" in l)
    r.contains("a broken run names the step that broke",
               cause_row, "gatk_haplotype_caller")
    r.contains("and says how it broke, in words rather than a Slurm constant",
               cause_row, "timeout")
    r.check("and how many broke the same way", "2×" in cause_row)
    r.contains("its progress shows it died on takeoff, not at the finish line",
               cause_row, "1/44")
    # "failed · failed in stringtie" says it twice. The generic FAILED
    # contributes no word of its own; every other state names something the
    # leading "failed" does not.
    plain = dict(timeout_record, name="plain-break")
    plain_status = runs.RunStatus(
        counts={"COMPLETED": 29, "FAILED": 1}, total=30, resolved=30,
        unknown=0, finished=True, verdict="failed", doomed=0, source="sacct",
        root_cause={"step": "stringtie", "state": "FAILED", "count": 1,
                    "job": "stringtie.S1"})
    plain_out = drawn(display.run_list, [(plain, plain_status)])
    plain_row = next(l for l in plain_out.splitlines() if "plain-break" in l)
    r.contains("a plain failure names its step", plain_row, "failed · stringtie")
    r.check("and does not say 'failed' twice",
            plain_row.count("failed") == 1)

    # "complete" beside a ✓ and a 10/10 is the third time one row has said the
    # same thing. The tick and the full fraction carry it; the NEEDS column is
    # for what a run wants from you, and this one wants nothing.
    r.check("a cleanly finished run reads as completed, with its full fraction",
            cells_of("all-done")[-1] == "completed"
            and "10/10" in cells_of("all-done"))
    r.check("marked with the done tick", cells_of("all-done")[0] == "✓")
    r.check("and no completion time is invented for it",
            "completed Aug" not in out and " at " not in row("all-done"))
    r.contains("a zero-job run says there was nothing to run",
               row("nothing-to-do"), "nothing to run")
    r.check("and is not given the tick that means jobs succeeded",
            cells_of("nothing-to-do")[0] != "✓")

    r.contains("a run somebody stopped is stopped, never completed",
               row("i-stopped-it"), "stopped")
    r.check("its mark is not the success tick",
            cells_of("i-stopped-it")[0] == "⊘")
    r.check("and it does not claim success",
            "complete" not in row("i-stopped-it"))

    r.check("an unresolvable run is marked unknown, not broken",
            cells_of("cant-tell")[0] == "?")
    r.check("it is not marked as having failed",
            cells_of("cant-tell")[0] != "✗")
    r.contains("and offers the last known verdict instead of silence",
               row("cant-tell"), "last known")

    # Every mark is distinct, which is the property that lets the column carry
    # the state on its own -- two states sharing a glyph would leave the
    # difference carried by colour, and there is no colour left to carry it.
    marks = [display._HELD_MARK, display._LIVE_MARK, display._BROKE_MARK,
             display._DONE_MARK, display._STOPPED_MARK, display._NOTHING_MARK,
             display._UNKNOWN_MARK]
    r.equal("every state has its own glyph", len(set(marks)), len(marks))

    # ---------------------------------------------------------------- #
    r.section("/list says each state in colour at both ends of its row")
    # These assert on the escapes themselves, so they cannot use drawn(),
    # which exists to strip exactly what is being checked here.
    painted = io.StringIO()
    with redirect_stdout(painted):
        display.run_list(rows)
    painted = painted.getvalue()

    def painted_row(name):
        return next(l for l in painted.splitlines() if name in l)

    # The glyph and the status phrase, and nothing in between. A row with its
    # name, its counts and its status all lit up has four highlights competing,
    # which is the same as having none.
    for name, colour in (("waiting", display.AMBER),
                         ("running-one", display.CYAN),
                         ("half-broken", display.RED),
                         ("all-done", display.GREEN)):
        r.equal(f"{name}'s state colour is spent exactly twice",
                painted_row(name).count(colour), 2)

    r.check("held is amber, not the red of something that went wrong",
            display.RED not in painted_row("waiting"))
    r.check("and a failure is red rather than the amber of a decision",
            display.AMBER not in painted_row("half-broken"))

    # The reason travels with the word it explains. Splitting them would put
    # "failed" in red and the one fact telling you what to do about it in
    # whatever colour was left over.
    broke = painted_row("half-broken")
    r.check("a failure's reason carries the same colour as the word 'failed'",
            broke.index(display.RED) < broke.index("failed")
            and display.RESET not in broke[broke.index("failed"):
                                           broke.index("still running")])

    # Everything between the two ends is weight, not hue: bold name, grey
    # pipeline, plain counts.
    r.check("the name is bold rather than coloured",
            display.BOLD in painted_row("waiting"))
    r.check("and a stopped run is dim, not tagged with a colour of its own",
            display.DIM in painted_row("i-stopped-it")
            and not any(c in painted_row("i-stopped-it")
                        for c in (display.RED, display.AMBER,
                                  display.GREEN, display.CYAN)))

    # The table has to fit the window, or the prompt box below it drifts -- see
    # display.fit and the three redraws that share it.
    r.check("no row is wider than a standard terminal",
            max(display.cells(line) for line in out.splitlines()) <= 100)

    r.check("held sorts above everything else",
            out.index("waiting") < out.index("running-one")
            and out.index("running-one") < out.index("half-broken"))

    # The job-list filename used to hang under every launched row. It was the
    # widest thing on screen -- wide enough to be the reason a 15-run listing
    # could not be a table -- and it is what /jobs and /view exist to show. The
    # listing answers "which runs, and how are they", not "where on disk".
    r.check("no job-list filename crowds the table",
            "job_list" not in out)
    r.check("and no absolute paths either",
            "/s/job_output" not in out)

    r.contains("the listing says when the states were read", out, "09:15")
    r.contains("actions are offered once, at the bottom", out, "Actions")
    r.contains("and cover diagnosis too", out, "/diagnose")

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
    r.contains("offering the diagnosis", out, "/diagnose patient-42")

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
        ("diagnose", "<name>", "explain it", "fixing"),
    ])
    for word in ("deciding", "watching", "fixing", "/approve", "/check", "/diagnose"):
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

    # The defect that switched agent.PLANS off: the checklist was lifted into a
    # plan event AND left in the text it was lifted from, so every turn drew the
    # list twice -- once as the block that repaints in place, once as raw
    # markdown directly underneath. The prompt requires exactly one <solution>
    # or one <execute> per reply, so a checklist reliably landed in the
    # solution path, which was the one that did not strip it.
    both = display.parse(Msg(
        "<solution>\n"
        "1. [x] read the step list\n"
        "2. [ ] generate the command\n\n"
        "Three protocols here. Generating next.\n"
        "</solution>"))
    told = next(e for e in both if e["kind"] == "solution")["text"]
    r.equal("the checklist is claimed by the plan block alone",
            told, "Three protocols here. Generating next.")
    r.check("so no raw checkbox line survives into the prose",
            "[x]" not in told and "[ ]" not in told)

    # A turn whose whole answer was the checklist has nothing left to say --
    # the block above is already saying it, and an empty solution would print
    # as a stray blank gap under it.
    r.equal("a checklist-only reply is the block and nothing else",
            [e["kind"] for e in display.parse(Msg(
                "<solution>\n1. [ ] first\n2. [ ] second\n</solution>"))],
            ["plan"])

    # The stripping keys on the CHECKBOX, not on the numbering: a pipeline's
    # step list, a set of options, any ordinary numbered list is content the
    # reader asked for and must survive intact.
    kept = next(e for e in display.parse(Msg(
        "<solution>This protocol runs:\n1. trimmomatic\n2. bwa_mem\n</solution>"))
        if e["kind"] == "solution")["text"]
    r.contains("an ordinary numbered list is untouched", kept, "1. trimmomatic")
    r.check("and is not mistaken for progress",
            not any(e["kind"] == "plan" for e in display.parse(Msg(
                "<solution>1. trimmomatic\n2. bwa_mem</solution>"))))

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
    # Drawn lowercase: the label is a quiet caption on a block, not a heading
    # shouted at the reader. _code_label's own vocabulary stays uppercase.
    r.contains("and the label is what gets drawn", loud(
        display.render, Msg("<execute>\nbash cmd.sh\n</execute>")), "submit")

    # ---------------------------------------------------------------- #
    r.section("long machine output is clipped at both ends, not one")
    flood = "<observation>" + "\n".join(f"line {i}" for i in range(60)) + "</observation>"
    out = loud(display.render, Msg(flood))
    r.contains("the start survives", out, "line 0")
    r.contains("the end survives -- where an error is", out, "line 59")
    r.contains("and it says how much it left out", out, "more lines")
    r.check("the middle is gone", "line 30" not in out)
    short = loud(display.render, Msg("<observation>one\ntwo</observation>"))
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

    rejected = loud(display.render, HumanMessage(
        "The proposed submission was not approved. use steps 6-12 instead. "
        "Regenerate the command accordingly."))
    r.check("nor is the rejection sent back", me not in rejected)
    r.contains("though the feedback is visible", rejected, "steps 6-12")

    # Command output arrives as a user turn now (the Anthropic API rejects a
    # conversation that ends on the assistant's side), so it must render as
    # machine output from either role.
    from_machine = loud(display.render,
                        HumanMessage("<observation>Generated cmd.sh</observation>"))
    r.contains("output on the user channel is still drawn", from_machine,
               "Generated cmd.sh")
    r.check("and is not attributed to them", me not in from_machine)
    # Uncaptioned on purpose: the command that produced it sits directly above,
    # so a "terminal" line named the channel rather than the event and cost a
    # line on every command the agent ran.
    r.check("with no caption naming the channel",
            "terminal" not in from_machine.lower())

    r.equal("the continue nudge is not drawn at all",
            display.parse(HumanMessage("[continue]")), [])

    # intake.brief appends what it could establish about the request. It is for
    # the model; showing it back reads as if they had typed an inventory.
    from genpipe import intake
    briefed = drawn(display.render, HumanMessage(
        intake.brief("run rnaseq stringtie with readset.tsv", ".")))
    r.contains("their own words are shown", briefed, "run rnaseq stringtie")
    r.check("the appended context is not", "do not ask again" not in briefed)
    r.contains("and it is still their turn, marked by the prompt chevron",
               briefed, "\u276f")
    r.check("with no speaker label above it", me not in briefed)

    text = drawn(display.fresh, [])
    r.check("/new says the conversation is gone", "New conversation" in text)
    r.check("and that the runs are not", "keeps its name" in text)
    held = drawn(display.fresh, [{"name": "rnaseq-stringtie-0726"}])
    r.check("a held run is named on the way out",
            "rnaseq-stringtie-0726" in held)

    # ---------------------------------------------------------------- #
    r.section("environment findings, and the gate's refusal to offer approval")

    from genpipe import preflight

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
    r.check("a normal gate offers approve", "/approve" in clean)

    # The point of the blocked gate: the command that cannot work must not be
    # on screen next to the explanation of why it cannot work.
    stopped = drawn(display.gate, proposal, "run-1", blockers=[block])
    r.check("a blocked gate withholds approve", "/approve" not in stopped)
    r.check("but still allows reject", "/reject" in stopped)
    r.check("and still allows modify", "/modify" in stopped)
    r.check("and says what to fix", "RAP_ID" in stopped)
    r.check("and confirms nothing was spent",
            "Nothing has reached the scheduler" in stopped)


    # ---------------------------------------------------------------- #
    # Every answer in /modify is an answer ABOUT a command, and the flow used
    # to take the command off the screen at exactly the moment the questions
    # about it started -- a stack of bare prompts scrolling down the terminal.
    # Seven prompts in, somebody was editing a thing they could not see, and
    # the six answers they had already given were three screens up.
    r.section("filling a row keeps the command, and the answers so far, in view")

    from genpipe import mirror
    m = mirror.read("genpipes dnaseq -t somatic_fastpass -s 1-5 -g cmd.sh",
                    name="pouletrun")

    first = "\n".join(display.fill_header(m, "protocol", {}, "somatic_fastpass",
                                          step="1 of 4"))
    r.contains("the invocation is always there", first, "genpipes dnaseq")
    r.contains("the row being asked shows what it says now", first,
               "somatic_fastpass")
    r.contains("and that it is the one in question", first, "→")
    r.contains("with the position in the run of questions", first, "1 of 4")

    later = "\n".join(display.fill_header(
        m, "steps",
        {"protocol": ("somatic_fastpass", "somatic_ensemble"),
         "design": ("design.tsv", "cohort.tsv")},
        "1-5", step="3 of 4"))
    r.contains("answers already given stay on screen", later,
               "somatic_ensemble")
    r.contains("all of them, not just the last", later, "cohort.tsv")
    r.contains("each as old → new", later, "design.tsv")
    r.contains("and the current question is still marked", later, "steps")

    # The collapse. The full mirror plus a protocol list plus the prompt runs
    # past twenty-four rows, and once the terminal scrolls the repaint
    # arithmetic that redraws this on every keystroke is wrong.
    r.check("rows nobody is asking about are dropped, not dimmed",
            "cmd.sh" not in later, later)
    r.check("so the header stays short enough to repaint",
            len(display.fill_header(m, "steps", {}, "1-5")) < 8)

    # A row with no value yet has to say so rather than showing a blank, which
    # reads as "already answered".
    absent = "\n".join(display.fill_header(m, "pairs", {}, ""))
    r.contains("an unset row says it is unset", absent, "not set")

    cascade = "\n".join(display.fill_header(
        m, "config", {"pipeline": ("dnaseq", "chipseq")}, "dnaseq.base.ini",
        step="required · 1 of 3",
        note="the -c stack is built on dnaseq.base.ini"))
    r.contains("a required round says so", cascade, "required")
    r.contains("and why the row came back", cascade, "-c stack is built on")

    # ---------------------------------------------------------------- #
    # A step name is not something anybody knows by heart. Faced with "its
    # --help name, e.g. gatk_sam_to_fastq" and no list, somebody typed `--help`
    # -- twice -- trying to get the thing the prompt was quoting at them.
    r.section("an empty step panel says why it is empty")

    why = drawn(display.no_step_list, "dnaseq", "somatic_fastpass")
    r.contains("it says the list could not be read", why, "--help")
    r.contains("and gives the exact command that prints it", why,
               "genpipes dnaseq -t somatic_fastpass --help")
    # The reason there is no table here, which is the same reason genpipes.md
    # gives: the numbers and names are version-exact.
    r.contains("and why the names are not kept in the tool", why,
               "version-exact")
    # The failure mode that makes a guess worse than an empty panel.
    r.contains("and what a wrong guess would do", why, "ignored silently")

    bare = drawn(display.no_step_list, None, None)
    r.contains("it still gives a usable shape with nothing to go on", bare,
               "--help")

    # ---------------------------------------------------------------- #
    # /list answers "what runs are there" and /check answers "how is it doing".
    # Neither answers "what IS it", which is the question somebody has before
    # they approve, modify or reject anything -- and the gate's box, the only
    # place the command was ever drawn, had scrolled away by then.
    r.section("/view draws any run, and offers only what its status allows")

    viewed = {"command": "bash cmd.sh",
              "generated": ("genpipes dnaseq -t somatic_fastpass -s 1-5 "
                            "-r readset_a.tsv -g cmd.sh"),
              "slots": {"pipeline": "dnaseq", "protocol": "somatic_fastpass",
                        "steps": "1-5", "readset": "readset_a.tsv"}}

    held = drawn(display.run_view, viewed, "pouletrun", "held")
    r.contains("the command is drawn as the same mirror the gate uses",
               held, "protocol     -t  somatic_fastpass")
    r.contains("under the run's name", held, "pouletrun")
    r.contains("a held run can be approved", held, "/approve")
    r.contains("modified", held, "/modify")
    r.contains("and rejected", held, "/reject")
    # No red banner: nothing is being ASKED here. A box that shouts at somebody
    # who typed a read-only command teaches them to ignore the shout.
    r.check("but nothing is being demanded", "HOLD" not in held)

    sent = drawn(display.run_view, viewed, "pouletrun", "submitted")
    r.check("a submitted run cannot be approved again",
            "/approve" not in sent)
    r.check("nor rejected", "/reject" not in sent)
    r.contains("it can be checked", sent, "/check")
    r.contains("and modified", sent, "/modify")
    # The one line that stops /modify reading as a rewrite of something live.
    r.contains("which says plainly that it copies", sent,
               "copies it into a new run")
    r.contains("leaving the original alone", sent, "this one is untouched")

    dropped = drawn(display.run_view, viewed, "pouletrun", "abandoned")
    r.contains("an abandoned run can still be copied", dropped, "/modify")
    r.check("but not checked — there is nothing on the scheduler",
            "/check" not in dropped)

    # A blocker withholds approve here for the same reason it does at the gate:
    # an action that cannot work must not sit beside the reason it cannot.
    block = preflight.check_rap_id("")
    stopped = drawn(display.run_view, viewed, "pouletrun", "held",
                    blockers=[block])
    r.check("a blocked run withholds approve", "/approve" not in stopped)
    r.contains("and says what to fix", stopped, "RAP_ID")
    r.contains("while still allowing modify", stopped, "/modify")

    tuned = drawn(display.run_view, viewed, "pouletrun", "held",
                  resources="gatk_sam_to_fastq  walltime 35:00:00")
    r.contains("tuning shows as its own row, not as an ini path",
               tuned, "walltime 35:00:00")

    # ---------------------------------------------------------------- #
    r.section("the transcript folds the working away, and keeps it")

    was = display.VERBOSE
    display.set_verbose(False)
    try:
        code = drawn(display.render, Msg("<execute>\nbash cmd.sh\n</execute>"))
        r.check("a command is not drawn", "cmd.sh" not in code)
        out = drawn(display.render, Msg("<observation>Submitted 46</observation>"))
        r.check("nor is its output", "Submitted 46" not in out)
        # But the reply is, and the marker above it says work happened. A fold
        # you cannot see is a deletion with better manners.
        answer = drawn(display.render, Msg("<solution>46 jobs went in.</solution>"))
        r.contains("the answer is", answer, "46 jobs went in")
        r.contains("under a count of what was folded", answer, "step")
        r.contains("and how to see it", answer, "/verbose")
        r.check("with no speaker label", "ASSISTANT" not in answer)

        replayed = drawn(display.replay)
        r.contains("and /verbose can replay what scrolled past", replayed, "cmd.sh")
        r.contains("including the output", replayed, "Submitted 46")
    finally:
        display.set_verbose(was)

    # ---------------------------------------------------------------- #
    r.section("the gate states the consequence of each verb")

    proposal = {"command": "bash cmd.sh",
                "slots": {"pipeline": "chipseq", "protocol": "chipseq",
                          "steps": "1-5", "design": "design.tsv"}}
    box = drawn(display.gate, dict(proposal, generated="genpipes chipseq -t "
                                   "chipseq -s 1-5 -d design.tsv -g cmd.sh"),
                "chipseq-0728")
    # `bash cmd.sh` is what runs and says nothing on its own. With the agent's
    # working folded away by default this is the only place the generation
    # command is seen before it is approved.
    r.contains("shows what the script was built from", box, "genpipes chipseq")
    box = drawn(display.gate, proposal, "chipseq-0728")
    for verb in ("/approve", "/modify", "/reject"):
        r.contains(f"offers {verb}", box, verb)
    # The change from a version that printed bare command names: this is the one
    # point in the product where consequences matter.
    r.contains("approve says it cannot be undone", box, "cannot be undone")
    r.contains("modify says it asks again", box, "asks you again")
    r.contains("reject says nothing is submitted", box, "nothing is submitted")
    r.contains("and the standing promise holds", box,
               "Nothing has reached the scheduler")

    risky = drawn(display.gate, proposal, "chipseq-0728",
                  warnings=["skipping 3-4 (trimming, alignment) but running 5"])
    r.contains("a risk is shown at the moment of decision", risky, "warning")
    r.contains("with its reasoning", risky, "skipping 3-4")

    # ---------------------------------------------------------------- #
    r.section("run status: the scheduler's words, with its provenance")

    def status_for(counts, **kw):
        st = runs.RunStatus(counts=counts, total=sum(counts.values()),
                            resolved=sum(counts.values()), unknown=0,
                            finished=True, verdict="failed, nothing still running",
                            reasons={}, at_risk=[], root_cause=None,
                            source="sacct", at="14:22", done_files=0, doomed=0,
                            jobs=[])
        for k, v in kw.items():
            setattr(st, k, v)
        return st

    dead = status_for({"COMPLETED": 1, "TIMEOUT": 2, "CANCELLED": 43},
                      root_cause={"step": "gatk_sam_to_fastq", "state": "TIMEOUT",
                                  "count": 2, "job": "gatk_sam_to_fastq.x",
                                  "elapsed": "00:10:01", "timelimit": "00:10:00",
                                  "maxrss": None, "cancelled_after": 43})
    text = drawn(display.run_status, "dnaseq-somatic-fastpass-0727", dead)
    r.contains("the run is named", text, "dnaseq-somatic-fastpass-0727")
    r.contains("every state present gets a row", text, "CANCELLED")
    r.check("and only those present do", "NODE_FAIL" not in text)
    r.contains("the total proves the denominator", text, "total")
    r.contains("percentages are shown", text, "93.5")
    r.contains("the root cause is the thing that broke", text, "gatk_sam_to_fastq")
    r.contains("not the jobs it took with it", text, "cancelled downstream")
    # The footer is provenance, not decoration: which tools were asked, and how
    # much of the manifest they accounted for.
    r.contains("the source is named", text, "sacct")
    r.contains("and the coverage", text, "46/46 jobs resolved")

    unknown = status_for({"COMPLETED": 44, "UNKNOWN": 2}, unknown=2, resolved=44,
                         finished=False, verdict="state unknown")
    text = drawn(display.run_status, "x", unknown)
    r.contains("an unaccounted job is said out loud", text, "2 UNKNOWN")
    r.check("and never rendered as healthy", "complete" not in text)

    live = status_for({"COMPLETED": 12, "RUNNING": 3, "PENDING": 31},
                      finished=False, verdict="running, 3 active",
                      reasons={"Dependency": 28, "Priority": 3})
    text = drawn(display.run_status, "x", live)
    r.contains("the reason block makes 31 PENDING legible", text, "waiting on")
    r.contains("naming the reason", text, "Dependency")

    doomed = status_for({"COMPLETED": 1, "FAILED": 1, "PENDING": 28},
                        verdict="dead — 28 waiting on a dependency that will never come",
                        reasons={"DependencyNeverSatisfied": 28}, doomed=28)
    text = drawn(display.run_status, "x", doomed)
    r.contains("a run queued behind a dead job reads as dead", text, "dead")
    r.contains("and says those jobs will never run", text, "never run")

    gone = status_for({}, total=0, resolved=0, source="unavailable",
                      verdict="scheduler unreachable", finished=False)
    text = drawn(display.run_status, "x", gone)
    r.contains("an unreachable scheduler says so", text, "could not reach")
    r.check("and guesses nothing", "PENDING" not in text)

    # ---------------------------------------------------------------- #
    r.section("the multi-run view")

    # One view, not two. /check all used to be a flat table and /status all the
    # grouped one, over the same query -- with /status <name> an exact alias for
    # /check <name>. The grouped one won: a listing answers "what should I be
    # doing", and the answer to that is never chronological.
    r.check("there is no second, flat renderer",
            not hasattr(display, "run_status_all"))

    groups = {display.ATTENTION: [{"name": "a", "what": "dnaseq somatic_fastpass",
                                   "when": "07-27 10:00",
                                   "line": "2 failed — gatk_sam_to_fastq  ·  "
                                           "1 of 46 done  (2%)",
                                   "suggest": "/diagnose a"}],
              display.ACTIVE: [{"name": "b", "what": "rnaseq stringtie",
                                "when": None, "line": "3 running, 31 queued",
                                "suggest": None}],
              display.FINISHED: [{"name": "c", "what": None, "when": None,
                                  "line": "12 of 12 done", "suggest": None}]}
    text = drawn(display.status_overview, groups)
    r.contains("the urgent group is named", text, "NEEDS ATTENTION")
    r.check("and comes first",
            text.index("NEEDS ATTENTION") < text.index("ACTIVE"))
    r.contains("with a count", text, "(1)")
    r.contains("and the next thing to type", text, "/diagnose a")
    r.contains("carrying the progress the flat table used to", text, "(2%)")
    r.contains("and naming the step that broke", text, "gatk_sam_to_fastq")
    r.check("no paths or job-list filenames leak in", "job_list" not in text)
    r.check("the footer offers /check, not a second listing command",
            "/check <name>" in text and "/status" not in text)

    # ---------------------------------------------------------------- #
    r.section("the plan is drawn, and drawn once")
    # The model's checklist used to be parsed and thrown away, which left the
    # fold with nothing to say what the agent was working through.
    checklist = ("1. [x] read the readset\n"
                 "2. [x] resolve the genome\n"
                 "3. [ ] generate the command\n"
                 "4. [ ] submit to the gate")

    display.reset_plan()
    text = drawn(display.render, Msg(checklist))
    r.contains("every stage is named", text, "submit to the gate")
    # Drawn in the model's own notation rather than translated into a marker
    # column: the list on screen and the list in the reply are one object, so
    # nobody holds a mapping between a ▶ and a `3. [ ]` in their head.
    r.contains("finished stages are ticked in place",
               text, "1. [✓] read the readset")
    r.contains("and the numbering is the model's own",
               text, "4. [ ] submit to the gate")
    r.check("a stage not started is an empty box, not a marker",
            "▶" not in text and "⏺" not in text)

    # The fold is what makes this necessary: with the working folded away the
    # plan is the only thing on screen saying what is happening, so it is the
    # one part of the working that must survive being folded.
    display.reset_plan()
    quiet = drawn(display.render, Msg(checklist))
    r.contains("and it survives the fold, unlike the rest of the working",
               quiet, "generate the command")

    # Re-emitting the same list is how the model reports progress. Off a
    # terminal there is nothing to repaint, so the duplicate is dropped rather
    # than printed again -- which is the bug that got it discarded originally.
    display.reset_plan()
    drawn(display.render, Msg(checklist))
    again = drawn(display.render, Msg(checklist))
    r.check("an unchanged plan is not printed twice", "Plan" not in again)

    # A different list is a different job and gets its own block.
    other = "1. [ ] read the logs\n2. [ ] explain the failure"
    fresh = drawn(display.render, Msg(other))
    r.contains("but a new plan starts a new block", fresh, "read the logs")

    # ------------------------------------------------------------------ #
    r.section("post_approve: the one message that claims something ran")
    # This line is the only place the product tells somebody that work reached
    # a shared cluster. It used to be printed whenever the graph came back
    # unpaused -- which is true of a thread that finished, one that died, and
    # one that was never resumed -- so it made a claim it could not support.
    # It now renders the reconciled record, and the word "submitted" belongs to
    # exactly one status.
    def record(status, **kw):
        base = {"status": status, "jobs_seen": None, "expected_jobs": None,
                "retry_safe": False, "outcome_detail": ""}
        base.update(kw)
        return base

    done = drawn(display.post_approve, "r1",
                 record(runs.SUBMITTED, jobs_seen=46, expected_jobs=46))
    r.contains("a real submission says so", done, "submitted")
    r.contains("with the job count", done, "46 job")
    r.contains("and offers monitoring", done, "/check r1")

    # Zero jobs is a success, not a failure, and must not read as one.
    nothing = drawn(display.post_approve, "r2",
                    record(runs.SUBMITTED, jobs_seen=0, expected_jobs=0))
    r.contains("zero jobs reads as an outcome, not a fault",
               nothing, "already up to date")
    r.check("and does not offer a check with nothing to check",
            "/check" not in nothing, nothing)
    for word in ("failed", "unknown", "error"):
        r.check(f"nor does it say {word!r}", word not in nothing.lower(), nothing)

    # A failure, with jobs already on the scheduler behind it.
    partial = drawn(display.post_approve, "r3",
                    record(runs.SUBMIT_FAILED, jobs_seen=20, expected_jobs=46,
                           outcome_detail="the submission command reported a failure"))
    r.contains("a failure says so", partial, "failed")
    r.check("and never uses the word submitted", "submitted" not in partial,
            partial)
    r.contains("it names the jobs already out there", partial, "20 job")
    r.contains("and warns against approving again", partial, "run twice")

    # THE INVARIANT THAT MATTERS MOST HERE: a failure with no rows counted is
    # still not offered a bare retry, because no count can prove that no sbatch
    # succeeded -- only the scheduler can.
    blind = drawn(display.post_approve, "r4",
                  record(runs.SUBMIT_FAILED, jobs_seen=0, expected_jobs=46))
    r.contains("a failure with zero rows still says check first",
               blind, "may already be queued")
    r.contains("and points at the scheduler", blind, "squeue")

    safe = drawn(display.post_approve, "r5",
                 record(runs.SUBMIT_FAILED, jobs_seen=0, expected_jobs=46,
                        retry_safe=True))
    r.contains("only a quiet scheduler unlocks a retry", safe, "safe to try again")

    unknown = drawn(display.post_approve, "r6",
                    record(runs.SUBMIT_UNKNOWN,
                           outcome_detail="the outcome of the submission was "
                                          "never established"))
    r.contains("an unestablished outcome says exactly that", unknown, "unknown")
    r.check("and is never promoted to submitted",
            "submitted" not in unknown, unknown)
    r.contains("it too points at the scheduler", unknown, "squeue")

    # ------------------------------------------------------------------ #
    r.section("reconciled(): what startup found still in flight")
    r.equal("a normal launch says nothing at all",
            drawn(display.reconciled, []).strip(), "")

    class Out:
        def __init__(self, status, jobs_seen=None, detail=""):
            self.status, self.jobs_seen, self.detail = status, jobs_seen, detail

    good = drawn(display.reconciled,
                 [("rnaseq-0812", Out(runs.SUBMITTED, 15))])
    r.contains("a settled run is named", good, "rnaseq-0812")
    r.contains("with what became of it", good, "submitted")
    r.contains("and its job count", good, "15 job")
    # A closed laptop after a complete submission is a successful run with an
    # interrupted terminal. Warning-colouring that teaches people to ignore the
    # colour on the day it means something.
    r.check("a complete one is not dressed as a problem",
            "squeue" not in good, good)

    murky = drawn(display.reconciled, [
        ("a", Out(runs.SUBMITTED, 15)),
        ("b", Out(runs.SUBMIT_UNKNOWN, None, "the outcome was never established")),
    ])
    r.contains("an unresolved one says so", murky, "outcome unknown")
    r.contains("with the reason", murky, "never established")
    r.contains("and points at the scheduler", murky, "squeue")
    r.contains("stating plainly that nothing was retried", murky,
               "Nothing was retried")

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
