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
import os
import re
import sys
from contextlib import redirect_stdout

from harness import Report

from genpipe import display
from genpipe import runs
from genpipe import theme

# The width /check's cause block reserves for its label column.
_CAUSE_W = display._CAUSE_LABEL_W + 2


def strip(text):
    """The screen with every escape sequence removed -- what a NO_COLOR
    terminal, a grayscale screenshot and `| tee log.txt` all reduce to."""
    return re.sub(r"\033\[[0-9;]*[A-Za-z]", "", text)


def painted(fn, *args, **kwargs):
    """Call a renderer and return what it printed, escape sequences and all."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*args, **kwargs)
    return buf.getvalue()


def drawn(fn, *args, **kwargs):
    """Call a renderer and return what it printed, ANSI stripped."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*args, **kwargs)
    return re.sub(r"\033\[[0-9;]*[A-Za-z]", "", buf.getvalue())


def _at_width(cols, fn, *args, **kwargs):
    """Call a renderer as if the terminal were `cols` columns wide.

    Both halves are needed: display asks _tty() whether there is a window at
    all before it asks terminal_cols() how wide it is, and under redirected
    stdout the first answer is False.
    """
    was_tty, was_cols = display._tty, display.terminal_cols
    display._tty = lambda: True
    display.terminal_cols = lambda: cols
    try:
        fn(*args, **kwargs)
    finally:
        display._tty, display.terminal_cols = was_tty, was_cols


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


def _offers(screen, verb, arg="", note=None):
    """Is `verb` proposed on this screen, with `arg` and (optionally) `note`?

    Column-padding-insensitive on purpose. display.actions() aligns the command
    and argument columns across the whole block, so "/check r1" is rendered
    "/check  r1" when a longer verb shares the block -- and an assertion on the
    exact spacing would fail every time a sibling command was added or removed,
    which is not what any of these tests are about.
    """
    want = [re.escape(verb)] + ([re.escape(arg)] if arg else [])
    if note:
        want.append(re.escape(note))
    return re.search(r"\s+".join(want), strip(screen)) is not None


def main():
    # EVERY ASSERTION ABOUT COLOUR IN THIS SUITE IS GUARDED BY THIS, and the
    # reason is the thing the guard is checking. With NO_COLOR set the palette
    # is a set of empty strings, so `display.RED not in output` is trivially
    # false and `output.count(display.AMBER)` is the length of the whole
    # string -- these checks do not fail honestly when colour is off, they fail
    # meaninglessly. What should be asserted in that mode is that the screen
    # still SAYS everything it said before, which is the section at the end.
    colour = bool(display._COLOUR)
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
    # READY TO SUBMIT, not HOLD, and not red. This screen is a decision point
    # for a complete run, not a failure -- red-reverse framed the tool's
    # ordinary successful path as something going wrong, and spent the colour
    # that should mean "you cannot proceed" on the screen where you usually
    # can. The irreversibility moved to the /approve line, in amber.
    r.contains("announces that the run is ready", out, "READY TO SUBMIT")
    r.contains("and names the run it is about", out, "patient-42")
    r.contains("with the irreversible verb still marked", out,
               "cannot be undone")
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
    # ONE LINE, AND ONLY ONE. It used to add a sentence restating what an
    # answer looks like and a row of three commands -- both of which the banner
    # a few lines above already carries, with a worked example. A question with
    # its own answer printed underneath is not an invitation.
    r.check("and nothing else", len([l for l in out.splitlines() if l.strip()]) == 1,
            out)
    for gone in ("describe the run you want", "/help", "/list", "/check all"):
        r.check(f"no {gone!r} restated here", gone not in out, out)

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
    # jobs_seen 0 / expected_jobs 0 is what record_outcome() writes when
    # reconcile() saw a script declaring `# TOTAL: 0`, a clean exit and no new
    # job rows. It is the EVIDENCE that lets the listing say "already up to
    # date" -- a record with no job list and no count at all is a different
    # thing entirely, and gets its own fixture below.
    up_to_date = {"name": "nothing-to-do", "status": "submitted", "held_at": None,
                  "submitted_at": "2026-07-25T11:00:00", "job_list": None,
                  "jobs_seen": 0, "expected_jobs": 0}
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

    # Kept under a second name so the no-colour section at the end of this
    # suite can re-render exactly this listing. `rows` is rebound several
    # times below.
    rows = listing_rows = [(held, None), (running_record, running_status),
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
    # ● AND NOT ▶. A play triangle is a control -- "press to start" everywhere
    # else a person has seen one -- and this row describes a run that has
    # already been launched, on a column that is not clickable. ● is the
    # status-LED reading instead: a filled dot means live.
    r.check("and carries the live indicator, not a play button",
            cells_of("running-one")[0] == "●")
    r.check("the play triangle is gone from the state column",
            "▶" not in out, out)
    r.check("its progress is a fraction, so the number means something",
            "9/15" in cells_of("running-one"))

    r.check("a mixed active+failed run is marked broken, not live",
            cells_of("half-broken")[0] == "✗")
    r.contains("a fully dead run says it failed", row("fully-dead"), "failed")
    # THE STATE, AND NOT A WORD MORE. /list answers "what state is this run
    # in"; how many jobs are still burning allocation behind the failure is an
    # operational fact, it is what /check exists to lay out, and putting it
    # here only ever fitted as "failed · 3 jobs · 2 still runn…".
    for row_name in ("half-broken", "fully-dead"):
        r.equal(f"{row_name} says the state and stops there",
                cells_of(row_name)[-1], "failed")
        r.check("with no job tally trailing it",
                "still running" not in row(row_name), row(row_name))

    # ONE CLOSED VOCABULARY. Every row's STATUS is one of display._STATE_WORDS
    # exactly -- not "starts with", not "contains". That is the property that
    # makes the column readable down rather than parsed row by row, and it is
    # the property a reason-tail breaks the moment somebody adds one back.
    said = {word for word, _ in display._STATE_WORDS}
    for n in ("waiting", "running-one", "half-broken", "fully-dead",
              "all-done", "i-stopped-it", "nothing-to-do", "cant-tell"):
        tail = row(n).split("  ")[-1].strip()
        r.check(f"{n}'s status is one of the states, whole", tail in said,
                f"{tail!r} not in {sorted(said)}")

    # ------------------------------------------------------------------ #
    r.section("the diagnosis lives in /check, and /list does not paraphrase it")
    # THE DEFECT THIS CLOSES. resolve() works out which step broke, how, and
    # how many broke the same way, and /list used to print it: "failed · 2×
    # timeout in gatk_sam_to_…". Too much for a listing and, once the column
    # cut it, too little to act on -- the one word that would have told you
    # which step is the word that got elided. /check <name> lays the same
    # finding out in full, with the tally, the walltime limit and the
    # downstream impact, which is what it is for.
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
    r.check("a broken run says failed, and only failed",
            cause_row.split("  ")[-1].strip() == "failed", cause_row)
    for detail in ("gatk_haplotype_caller", "timeout", "2×"):
        r.check(f"and does not paraphrase {detail!r} from the diagnosis",
                detail not in cause_row, cause_row)
    r.contains("its progress still shows it died on takeoff",
               cause_row, "1/44")
    # The evidence is not lost -- it moved to the screen that can hold it.
    r.contains("while /check names the step that broke",
               strip(drawn(display.run_status, "timed-out", timeout_status)),
               "gatk_haplotype_caller")

    # NOTHING IN THIS COLUMN IS EVER ELIDED. The old fix for a truncated
    # explanation would have been a wider table; the actual fix was to stop
    # putting explanations here, and the reserved width went DOWN as a result.
    long_rows = [(dict(timeout_record, name=f"r-{i}"), timeout_status)
                 for i in range(3)]
    long_rows.append(({"name": "held-wide", "status": "held",
                       "held_at": "2026-07-24T09:00:00", "submitted_at": None,
                       "job_list": None}, None))
    narrow = drawn(_at_width, 80, display.run_list, long_rows)
    r.contains("the longest state prints whole, even at 80 columns",
               narrow, "waiting for approval")
    # The reserve is COMPUTED from _STATE_WORDS, not typed, so a phrase can
    # never be added that the column has not grown to hold.
    r.equal("and the column reserves exactly that much and no more",
            max(len(w) for w, _ in display._STATE_WORDS), 20)

    # "complete" beside a ✓ and a 10/10 is the third time one row has said the
    # same thing. The tick and the full fraction carry it; the NEEDS column is
    # for what a run wants from you, and this one wants nothing.
    r.check("a cleanly finished run reads as completed, with its full fraction",
            cells_of("all-done")[-1] == "completed"
            and "10/10" in cells_of("all-done"))
    r.check("marked with the done tick", cells_of("all-done")[0] == "✓")
    r.check("and no completion time is invented for it",
            "completed Aug" not in out and " at " not in row("all-done"))
    # "already up to date", not "nothing to run" and not "no jobs". A run that
    # generated no work is a SUCCESS -- every output it was asked for was
    # already on disk -- and both older wordings read as a failure to do
    # something. _finished_line has always said it this way; the listing now
    # agrees with it.
    def status_of(name):
        """The STATUS column's whole phrase. cells_of() splits on whitespace,
        which cuts a multi-word state into pieces; the columns are separated by
        two spaces, so the last such field is the phrase, intact."""
        return row(name).split("  ")[-1].strip()

    r.equal("a zero-job run says its outputs are current",
            status_of("nothing-to-do"), "up to date")
    for wrong in ("nothing to run", "no jobs", "failed"):
        r.check(f"and never {wrong!r}, which reads as a fault",
                wrong not in row("nothing-to-do"), row("nothing-to-do"))
    # "already up to date" was a report on an event that had just happened.
    # This column describes a STATE, and what is true of this run is that its
    # outputs are current.
    r.check("and not as a report on something that just happened",
            "already" not in row("nothing-to-do"), row("nothing-to-do"))
    # THE TICK IS "THIS IS DONE AND THE OUTCOME IS GOOD", not "jobs succeeded".
    # By that reading a run that found its work already done belongs with a run
    # that did the work: both leave the user holding the outputs they asked
    # for. The WORD is what keeps them apart -- a green tick reading
    # "completed" over zero jobs would invite "I computed your results" when
    # the truth is "your results were already there".
    r.equal("it joins the successful-terminal glyph family",
            cells_of("nothing-to-do")[0], "✓")
    r.check("while still not claiming the work was computed",
            "completed" not in row("nothing-to-do"), row("nothing-to-do"))

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
    r.equal("and says exactly that, with no stale verdict dragged along",
            cells_of("cant-tell")[-1], "unknown")
    # The cached verdict is not gone -- it is dated, explicitly stale, and
    # belongs where there is room to say so.
    r.check("the last known verdict is not paraphrased into the listing",
            "last known" not in out, out)
    r.contains("it is what _unavailable_line is for",
               display._unavailable_line(unreachable_record), "last known")

    # Every mark is distinct, which is the property that lets the column carry
    # the state on its own -- two states sharing a glyph would leave the
    # difference carried by colour, and there is no colour left to carry it.
    marks = [display._HELD_MARK, display._REBUILD_MARK, display._LIVE_MARK,
             display._BROKE_MARK, display._DONE_MARK, display._STOPPED_MARK,
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
    # Read from _marks() rather than retyped. What this section is about is that
    # a state's colour appears exactly TWICE on its row -- on the glyph and on
    # the status phrase -- and hardcoding which colour that is turned a palette
    # change into a failure of an assertion that was not about the palette.
    for name, shade in (
            ("waiting", display._marks()[runs.HELD_BUCKET][1]),
            ("running-one", display._marks()[runs.ACTIVE_BUCKET][1]),
            ("half-broken", display._marks()[runs.ATTENTION_BUCKET][1]),
            ("all-done", display._marks()[runs.FINISHED_BUCKET][1])):
        if not colour:
            continue
        r.equal(f"{name}'s state colour is spent exactly twice",
                painted_row(name).count(shade), 2)

    if colour:
        r.check("held is amber, not the red of something that went wrong",
                display.RED not in painted_row("waiting"))
        r.check("and a failure is red rather than the amber of a decision",
                display.AMBER not in painted_row("half-broken"))

    # The state word is the ONLY thing on the row painted in the state's
    # colour besides the glyph. There is no reason clause after it to leak a
    # second highlight into the widest column on screen.
    broke = painted_row("half-broken")
    if colour:
        r.check("the word 'failed' is painted, and it is the end of the row",
                broke.index(display.RED) < broke.index("failed"))

    # Everything between the two ends is weight, not hue: bold name, grey
    # pipeline, plain counts.
    r.check("the name is bold rather than coloured",
            display.BOLD in painted_row("waiting"))
    if colour:
        r.check("and a stopped run is dim, not tagged with a colour of its own",
                display.DIM in painted_row("i-stopped-it")
                and not any(c in painted_row("i-stopped-it")
                            for c in (display.RED, display.AMBER,
                                      display.GREEN)))

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
    # THIS SECTION CHANGED WITH THE BEHAVIOUR IT DESCRIBES, deliberately.
    #
    # It used to assert that a run's /diagnose finding is printed underneath
    # its /history row, and that assertion was holding a defect in place. On a
    # real registry it produced two lines of prose under sixty runs, almost all
    # of them "The log does not name a cause on its own." -- and the summary
    # became unreadable, which is the one thing an archive may not be.
    #
    # The finding itself was never the problem and is not lost: what changed is
    # which screen it is on. So the pair of checks below is stricter than the
    # single one it replaces -- the summary must NOT carry the prose, and
    # /history <name> MUST still be able to produce it.
    archived = {"name": "old-one", "status": "gone", "source": "agent",
                "submitted_at": "2026-06-01T10:00:00", "held_at": None,
                "job_list": "/s/job_output/X.job_list.T0",
                "notes": [{"at": "2026-06-01T11:00:00",
                           "text": "OOM in picard_mark_duplicates: raise java heap"}]}

    r.section("/history summarises; it does not dump diagnoses")
    out = drawn(display.history, [dict(archived)])
    r.contains("names the run", out, "old-one")
    r.contains("marked as an archived record", out, "artifacts gone")
    r.contains("says how it got here", out, "built here")
    r.check("the diagnosis prose stays off the summary",
            "picard_mark_duplicates" not in out)
    r.check("and a stale status is never called live", "live" not in out)
    r.check("it points at where the detail is, under an Actions heading",
            "Actions" in out and _offers(out, "/diagnose", "<name>"), out)

    r.section("/history <name> is where the finding survives")
    out = drawn(display.history_detail, dict(archived))
    r.contains("names the run", out, "old-one")
    r.contains("and the finding survives with it", out, "picard_mark_duplicates")
    r.contains("with the job list it belongs to", out, "X.job_list.T0")

    r.section("a submitted record is not reported as running")
    out = drawn(display.history, [
        {"name": "sub-one", "status": "submitted", "source": "agent",
         "submitted_at": "2026-06-01T10:00:00",
         "last_check": {"at": "2026-06-02T10:00:00", "verdict": "failed"}},
    ])
    r.contains("the lifecycle fact is stated", out, "submitted")
    r.check("the word 'live' appears nowhere", "live" not in out)
    r.contains("the cached outcome is dated and past tense", out, "when last checked")

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

    r.section("a job that never ran says so, rather than 'not found'")
    # A CANCELLED job never started, so it never wrote a log. That is not the
    # same as a file that should be there and is missing, and printing "not
    # found" for both sent people hunting for the first. See runs.triage.
    out = drawn(display.triage, "patient-42", {
        "failed_total": 2, "broke_total": 0, "cancelled_total": 2,
        "steps_affected": 1, "truncated": 0,
        "findings": [{"step": "trim_fastp", "count": 2, "job": "trim_fastp.A",
                      "job_id": "9", "state": "CANCELLED", "maxrss": None,
                      "exit_code": None, "ran": False,
                      "log": None, "log_tail": None, "script": None}]})
    r.contains("the cancellation is still named", out, "trim_fastp")
    r.contains("with its state", out, "cancelled")
    r.contains("and the reason there is no log", out, "never ran")
    r.check("without claiming a file is missing", "not found" not in out)

    # ---------------------------------------------------------------- #
    r.section("the diagnosis panel keeps every line right of its gutter")
    # THE BUG. The body used to be wrapped against the constant WIDTH = 74 --
    # the only reference to it in display.py, while every other block that has
    # to fit the window asks terminal_cols(). Below 74 the line overflowed, and
    # a terminal soft-wraps an overflow to COLUMN ZERO: left of and underneath
    # the gutter it was supposed to sit beside. Log paths were worse, printed
    # unwrapped on purpose at 130-odd columns.
    long_path = ("/home/pbourque/genpipe-workflow-assistant/job_output/"
                 "gatk_sam_to_fastq/"
                 "gatk_sam_to_fastq.tumorPair_COLO829N_2026-07-30T16.17.43.o")
    answer = {"shaped": True, "confidence": "unclear",
              "manner": "gatk_sam_to_fastq hit its walltime.",
              "cause": "The job was killed at 00:01:00 after running 00:01:01, "
                       "while it was still streaming reads from the input BAM.",
              "evidence": [f"the .o log ends mid-record with no error, at {long_path}"],
              "fix": "raise cluster_walltime for the [gatk_sam_to_fastq] section",
              "override": {"gatk_sam_to_fastq": {"cluster_walltime": "3:00:00"}},
              "relaunch": "1-23"}

    for cols in (60, 74, 100, 120):
        text = drawn(_at_width, cols, display.diagnosis,
                     "Test_walltimefail", answer, [long_path])
        lines = [l for l in text.splitlines() if l.strip()]
        widest = max(len(l) for l in lines)
        r.check(f"nothing overflows a {cols}-column window",
                widest <= cols, f"widest={widest}")
        # Every line of the block carries the gutter, and the gutter is always
        # at the same two columns -- which is the visible form of "no line
        # restarted at column zero".
        r.check(f"every line still starts at the gutter at {cols}",
                all(l.startswith("  ▌") for l in lines),
                [l for l in lines if not l.startswith("  ▌")][:2])
        r.contains(f"and the path is still whole at {cols}",
                   "".join(l[24:] for l in lines
                           if l.startswith("  ▌") and "genpipe-workflow" in l
                           or l.startswith("  ▌") and "16.17.43.o" in l),
                   "16.17.43.o")

    r.section("the width is read at every render, so a resize is picked up")
    # WHAT RESIZING ACTUALLY DOES, and the honest boundary around it. Printed
    # scrollback belongs to the terminal emulator: on resize it reflows what is
    # already on screen as plain text, so a line this module wrapped for 100
    # columns is soft-wrapped again at 80 and its tail restarts at column zero,
    # under the gutter. Nothing in this process can edit bytes it has already
    # written. What it CAN do is read the width afresh every time it draws --
    # which is what these transitions assert, one render per width, in the
    # order somebody would actually resize.
    # The whole panel, including the two sections the shorter fixture above
    # leaves out -- what the run did not establish, and the Actions block that
    # depends on the fix being one this program can apply.
    whole = dict(answer, uncertain=[
        "whether 35:00:00 is actually sufficient for this input",
        "whether memory pressure at 99.3% of the request contributed"])
    for before, after in ((120, 80), (80, 120), (100, 60), (60, 100)):
        for cols in (before, after):
            text = drawn(_at_width, cols, display.diagnosis,
                         "Test_walltimefail", whole, [long_path],
                         applicable=True)
            lines = [l for l in text.splitlines() if l.strip()]
            r.check(f"{before}->{after}: nothing overflows at {cols}",
                    max(len(l) for l in lines) <= cols,
                    max(len(l) for l in lines))
            r.check(f"{before}->{after}: every line still sits on its gutter "
                    f"at {cols}",
                    all(l.startswith("  ▌") for l in lines),
                    [l for l in lines if not l.startswith("  ▌")][:2])
            for label in ("died", "because", "evidence", "fix",
                          "not established", "Actions"):
                r.check(f"{before}->{after}: '{label}' survives at {cols}",
                        label in text, label)
            for verb in ("/relaunch", "/modify", "/jobs"):
                r.check(f"{before}->{after}: {verb} is offered at {cols}",
                        verb in text, verb)

    r.section("the last canonical surface can be drawn again, from its own data")
    # /redraw's mechanism. What is stored is the FUNCTION and the ARGUMENTS --
    # there is no model in it, no log path it re-reads and no record it can
    # touch, because it does not hold any of those things.
    display.forget_surface()
    r.equal("nothing to redraw before anything is drawn",
            display.last_surface(), (None, None))
    at100 = drawn(_at_width, 100, display.diagnosis, "Test_walltimefail",
                  whole, [long_path], applicable=True)
    name, again = display.last_surface()
    r.equal("the diagnosis is what is remembered", name, "diagnosis")
    r.truthy("and it can be drawn again", again)

    at60 = drawn(_at_width, 60, again)
    r.check("redrawn at 60 it fits 60",
            max(len(l) for l in at60.splitlines() if l.strip()) <= 60)
    r.check("and is not the same text as the 100-column render", at60 != at100)
    for label in ("died", "because", "evidence", "fix", "not established"):
        r.contains(f"'{label}' is still there after the redraw", at60, label)
    r.contains("and so is the fix's value", at60, "3:00:00")

    twice = drawn(_at_width, 60, again)
    r.equal("redrawing twice draws the same thing", twice, at60)
    r.equal("and the memo is unchanged by being read",
            display.last_surface()[0], "diagnosis")

    back = drawn(_at_width, 120, again)
    r.check("and widening redraws wider again",
            len(back.splitlines()) < len(at60.splitlines()))

    r.section("a wide window is actually used, not squandered")
    narrow = drawn(_at_width, 74, display.diagnosis, "n", answer, [long_path])
    wide = drawn(_at_width, 140, display.diagnosis, "n", answer, [long_path])
    r.check("140 columns produces fewer lines than 74",
            len(wide.splitlines()) < len(narrow.splitlines()),
            f"wide={len(wide.splitlines())} narrow={len(narrow.splitlines())}")

    r.section("paths break at separators, never mid-token by accident")
    pieces = display._wrap_path("/aaa/bbb/ccc/ddd", 10)
    r.equal("packed to the budget", pieces, ["/aaa/bbb/", "ccc/ddd"])
    r.check("nothing longer than the budget",
            all(len(p) <= 10 for p in pieces))
    huge = display._wrap_path("/x/" + "z" * 40, 12)
    r.check("a segment too long for any line is still cut to fit",
            all(len(p) <= 12 for p in huge), huge)
    r.equal("and rejoins to exactly the original", "".join(huge), "/x/" + "z" * 40)
    r.equal("a path that fits is left alone",
            display._wrap_path("/short/path", 40), ["/short/path"])

    r.section("the resubmit range is no longer on the diagnosis screen")
    # It printed raw `-s` syntax two rows under a sentence about a walltime,
    # and then left somebody to assemble a command around it. The range is now
    # established deterministically (relaunch.scope) and rendered where command
    # syntax belongs -- the revision's own command. The renderer had already
    # stopped CAPTIONING the model's range, for the reason that RELAUNCH_RULE
    # asks for a full range and cannot make a model comply; this goes further
    # and stops printing a number the screen cannot vouch for at all.
    narrowed = dict(answer, relaunch="7-9")
    out = drawn(_at_width, 100, display.diagnosis, "n", narrowed, [])
    r.check("a narrowed range the model returned is not shown",
            "7-9" not in out, out)
    r.check("nor is the row it sat on", "resubmit" not in out)
    r.check("and no claim about what a range covers survives",
            "the whole range" not in out and "already have output" not in out)

    # ---------------------------------------------------------------- #
    r.section("/diagnose offers /relaunch only when the fix can be applied")
    # THE DISTINCTION UNDER TEST. "There is an OVERRIDE heading" is a claim
    # about the model's output; "there is a change this program can make" is a
    # claim about this program. The screen must offer the command only for the
    # second, which is why `applicable` is an argument rather than something
    # the renderer re-derives from `parsed`.
    out = drawn(_at_width, 100, display.diagnosis, "n", answer, [],
                applicable=True)
    r.contains("/relaunch is offered", out, "/relaunch")
    r.check("first, above the manual alternative",
            out.index("/relaunch") < out.index("/modify"))
    r.contains("with wording that says it prepares rather than submits",
               out, "prepare a retry")
    r.contains("/modify stays as the manual route", out, "/modify")
    r.contains("and reads as the alternative beside it", out,
               "make different changes")
    r.check("the fork wording is not ALSO on the screen — one meaning per row",
            "build a revised copy" not in out, out)
    r.contains("/jobs is still the evidence rung", out, "/jobs")
    r.check("no submission is proposed anywhere on the screen",
            "/approve" not in out, out)

    out = drawn(_at_width, 100, display.diagnosis, "n", answer, [],
                applicable=False)
    r.check("a fix this cannot apply offers no /relaunch",
            "/relaunch" not in out, out)
    r.contains("/modify is then the only next step", out, "/modify")
    r.contains("so it carries the fact that the run is copied, not edited",
               out, "build a revised copy")
    r.contains("and that the original is untouched", out, "untouched")

    nofix = dict(answer, override={})
    out = drawn(_at_width, 100, display.diagnosis, "n", nofix, [],
                applicable=False)
    r.check("a diagnosis with no config fix at all offers no /relaunch",
            "/relaunch" not in out)
    r.contains("but still offers the evidence rung", out, "/jobs")

    r.section("the prepared-retry review screen")
    out = drawn(_at_width, 100, display.prepared_retry,
                "study-0805", "study-0805-2", "needs attention",
                [("gatk_sam_to_fastq", "cluster_walltime", "0:10:00", "35:00:00")],
                "steps 1-23 — the whole protocol", "the whole protocol",
                ["whether 35:00:00 is sufficient for this input"],
                [("step.odd_key", "not a cluster setting this can write")])
    r.contains("both runs are named", out, "study-0805-2")
    r.contains("the original first, since keeping it is the point",
               out, "prepared retry of study-0805")
    r.contains("the change is spelled out", out, "gatk_sam_to_fastq.cluster_walltime")
    r.contains("as a transition, not a bare value", out, "0:10:00")
    r.contains("with what it became", out, "35:00:00")
    r.contains("the retry's scope is stated in words", out, "1-23")
    r.contains("what is still unproven survives to this screen",
               out, "whether 35:00:00 is sufficient")
    r.contains("what was NOT applied is named too", out, "odd_key")
    r.contains("the original is confirmed unchanged", out, "is unchanged")
    r.contains("in /list's words for where it stands", out, "needs attention")
    r.contains("and nothing was submitted", out, "nothing has been submitted")
    r.check("the gate owns the verbs, so this screen proposes none",
            "/approve" not in out and "Actions" not in out, out)

    out = drawn(_at_width, 100, display.prepared_retry,
                "a", "a-2", "held",
                [("s", "cluster_mem", "", "96G")], "", "", [], [])
    r.contains("a setting with no observed baseline prints the value alone",
               out, "96G")
    r.check("and invents no arrow to put in front of it", "→" not in out, out)

    for cols in (60, 74, 100):
        text = drawn(_at_width, cols, display.prepared_retry,
                     "study-0805", "study-0805-2", "needs attention",
                     [("gatk_sam_to_fastq", "cluster_walltime", "0:10:00",
                       "35:00:00")],
                     "steps 1-23 — the whole somatic_fastpass protocol", "",
                     ["whether 35:00:00 is sufficient for this input and this "
                      "reference, which nothing observed here establishes"], [])
        lines = [l for l in text.splitlines() if l.strip()]
        r.check(f"the review screen fits a {cols}-column window",
                max(len(l) for l in lines) <= cols,
                f"widest={max(len(l) for l in lines)}")
        r.check(f"and stays right of its gutter at {cols}",
                all(l.startswith("  ▌") for l in lines),
                [l for l in lines if not l.startswith("  ▌")][:2])

    r.section("/history shows where a revision came from")
    out = drawn(display.history_detail,
                {"name": "study-0805-2", "status": "held", "source": "agent",
                 "held_at": "2026-08-24T09:00:00", "workdir": "/w",
                 "derived_from": "study-0805",
                 "derived_reason": "relaunch_after_diagnosis",
                 "proposal": {"slots": {"pipeline": "dnaseq"}}})
    r.contains("the parent is named", out, "study-0805")
    r.contains("under a label somebody can read", out, "derived from")
    r.contains("and the reason is words, not a stored constant",
               out, "a retry prepared from its diagnosis")
    r.check("the constant itself never reaches the screen",
            "relaunch_after_diagnosis" not in out, out)
    out = drawn(display.history_detail,
                {"name": "plain", "status": "held", "source": "agent",
                 "workdir": "/w", "proposal": {"slots": {"pipeline": "dnaseq"}}})
    r.check("a run that came from nowhere shows no lineage row",
            "derived from" not in out, out)

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

    # ---------------------------------------------------------------- #
    r.section("the banner onboards, and does not lecture")
    _os.environ["COLUMNS"] = "100"
    out = drawn(display.banner, "Anthropic", "claude-sonnet-5")
    for heading in ("Getting started", "Ask naturally", "Keep track",
                    "Need something else?"):
        r.contains(f"has {heading!r}", out, heading)
    r.contains("with a worked example to say out loud", out,
               "run dnaseq germline_snv on my readset, all steps")

    # THE EXAMPLE IS HELPER TEXT, NOT A THIRD PIECE OF BRANDING. It was set in
    # WHITE, which is BOLD (see the palette note in display.py) -- so a bold
    # line sat directly under a bold heading, a few rows below a bold wordmark,
    # on the one screen whose job is to say what this product is called. Three
    # things competing on the loudest weight the terminal has is the same as
    # none of them being emphasised. It reads exactly like "type / to browse
    # commands" does: an illustration of the heading above it. So it is set the
    # same way -- GREY, the readable quiet -- and the emphasis is left to the
    # headings and the mark.
    right = display._right_column(60, "Anthropic", "claude-sonnet-5", "/tmp/p")
    example = next(l for l in right if "germline_snv on my readset" in l)
    browse = next(l for l in right if "to browse commands" in l)
    if colour:
        r.check("the example is not emphasised", display.BOLD not in example,
                repr(example))
        r.check("it is set in the same quiet as the other helper line",
                display.GREY in example and display.GREY in browse,
                repr(example))
        # ...while what it is an example OF still is.
        heading = next(l for l in right if l.endswith("Ask naturally" + display.RESET))
        r.check("its heading keeps the emphasis", display.BOLD in heading,
                repr(heading))
        r.check("and the example is still not painted like a command",
                display.GREEN not in example, repr(example))
    r.contains("and it is still fully legible on the screen", out,
               "run dnaseq germline_snv")
    r.contains("the two commands worth knowing", out, "/list")
    r.contains("and what they do", out, "see your runs")
    r.contains("plus how to find the rest", out, "to browse commands")

    # What went, and why: the banner was a manual. The command list is one
    # keystroke away by design, and the monitoring verbs are offered by name at
    # the moment each applies -- so neither needs teaching before anybody has
    # typed anything.
    for gone in ("Press Tab", "autocomplete", "brings the full list",
                 "Once it's running", "/jobs", "/cancel", "/diagnose"):
        r.check(f"no longer says {gone!r}", gone not in out, out)

    # Model and project are read from the session, never hardcoded.
    r.contains("names the model in use", out, "claude-sonnet-5")
    r.contains("under a label", out, "Model")
    # The load-bearing directory, and the checkout, under names that say which
    # is which. It used to show the checkout alone, labelled "Project" -- the
    # one directory on the screen that decides nothing about where a run lands.
    r.contains("and the directory the run will be written into", out, "Working in")
    r.contains("plus where this copy of the tool lives", out, "This copy")
    r.check("and nothing calls the checkout the project any more",
            "Project" not in out, out)
    other = drawn(display.banner, "Anthropic", "claude-opus-4-5")
    r.contains("a different model renders differently", other, "claude-opus-4-5")
    r.check("with no trace of the previous one",
            "claude-sonnet-5" not in other, other)
    unset = drawn(display.banner, None, None)
    r.contains("and an unconfigured session says so", unset, "not configured yet")

    # ONE horizontal rule on the right, immediately before the metadata.
    # Whitespace separates the three onboarding groups; a rule between them
    # made three short lists look like three unrelated screens.
    right = display._right_column(60, "Anthropic", "claude-sonnet-5", "/tmp/p")
    rules = [i for i, l in enumerate(right) if "─" in l]
    r.equal("exactly one rule on the right side", len(rules), 1)
    tail = "\n".join(right[rules[0]:])
    r.contains("and it sits immediately before the metadata", tail, "Model")
    r.check("with nothing but metadata after it",
            "Keep track" not in tail and "Ask naturally" not in tail, tail)

    # A healthy startup screen carries no alarm colours.
    painted_banner = io.StringIO()
    with redirect_stdout(painted_banner):
        display.banner("Anthropic", "claude-sonnet-5")
    raw = painted_banner.getvalue()
    if colour:
        r.check("no red anywhere on a healthy banner", display.RED not in raw)
        r.check("and no amber either", display.AMBER not in raw)
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
    # THE CAPTION IS NO LONGER THE LABEL. _code_label's vocabulary decides what
    # is HIDDEN (HELP) and what is COLOURED as consequential (SUBMIT, GENERATE)
    # -- both judgements, and both allowed to be. What the caption says is what
    # ACTUALLY RAN, which is a different question with a definite answer.
    r.equal("a shell block is captioned as the shell",
            display._tool_of("bash cmd.sh"), "bash")
    r.equal("...whatever purpose the label assigns it",
            label("bash cmd.sh"), "SUBMIT")
    # ...and a capability call is not shell at all. This is the bug: the live
    # run captioned `show_run(name="…")` as `bash`, and nothing about it
    # reached a shell -- the application answered it.
    r.equal("a capability call is captioned with the capability",
            display._tool_of('show_run(name="patient-42")'), "show_run")
    r.equal("and so is another one",
            display._tool_of('check_run(name="patient-42")'), "check_run")
    # `help` and `read` are gone from the caption: both were purposes inferred
    # from the command text, and neither is distinguishable in the execution
    # path -- everything that is not a capability is run by a shell.
    for shell in ("module load mugqic/genpipes/6.1.1 && genpipes rnaseq --help",
                  "head -40 /s/job_output/DnaSeq.job_list.T1",
                  "ls -la /s/job_output/"):
        r.equal(f"{shell[:24]!r} is captioned bash",
                display._tool_of(shell), "bash")
    r.check("but the colouring judgement is untouched",
            label("bash cmd.sh") in display._CONSEQUENTIAL)
    r.check("and so is the hiding one", label(
        "module load mugqic/genpipes/6.1.1 && genpipes rnaseq --help")
        in display._HIDDEN)

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
    r.check("but nothing is being demanded",
            "READY TO SUBMIT" not in held)

    sent = drawn(display.run_view, viewed, "pouletrun", "submitted")
    r.check("a submitted run cannot be approved again",
            "/approve" not in sent)
    r.check("nor rejected", "/reject" not in sent)
    r.contains("it can be checked", sent, "/check")
    r.contains("and modified", sent, "/modify")
    # NOR IS IT OFFERED AN EXPLANATION FOR SOMETHING NOTHING SAYS WENT WRONG.
    # /view asks the scheduler nothing, so it cannot know whether this run is
    # queued, running, finished cleanly or broken -- and /diagnose means
    # "explain what went wrong". It used to be offered under every submitted
    # run regardless. The ladder still reaches it one rung later and on
    # evidence: /view offers /check, and /check offers /diagnose exactly when
    # something actually broke.
    r.check("but not diagnosed on a screen that has asked nobody anything",
            "/diagnose" not in sent, sent)
    r.check("the command that would find out is what it offers instead",
            _offers(sent, "/check", "pouletrun",
                    display._ACTION_TEXT["/check"]), sent)
    # ONE DESCRIPTION PER COMMAND. This block used to carry its own wording
    # ("copies it into a new run; this one is untouched") which was a fourth
    # phrasing of a verb three other screens already described differently.
    # What /modify does to a submitted run -- fork it -- is a property of the
    # run's state, and the screen says the state on its own header line.
    # A SANCTIONED EXCEPTION, and it earns it on the consistency rule's own
    # terms. /modify on a HELD run edits the proposal and asks again; on a
    # SUBMITTED one it forks -- a new run is built and gated, and the launched
    # one is not touched. "change a run before launch" is right for the first
    # and misleading for the second: it was printed under a /diagnose of a run
    # that had been on the scheduler for nineteen days. The rule exists so a
    # reader learns one meaning per command; here the behaviour really is two.
    r.check("a submitted run says /modify builds a copy",
            _offers(sent, "/modify", "pouletrun",
                    display.modify_text("submitted")), sent)
    r.check("and does not imply the launched run is edited",
            display._ACTION_TEXT["/modify"] not in sent, sent)
    r.contains("saying plainly that this one is untouched", sent,
               "this run is untouched")
    for stale in ("copies it into a new run", "this one is untouched",
                  "how it is doing on the scheduler",
                  "read the logs and explain a failure"):
        r.check(f"and no longer says {stale!r}", stale not in sent, sent)

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

    # WHERE A REVISION CAME FROM, on the screen read before approving it.
    # /view is where somebody asks what a run IS, and for a retry that
    # includes which run it is a retry of.
    retry = drawn(display.run_view, viewed, "pouletrun-3", "held",
                  record={"status": "held", "derived_from": "pouletrun",
                          "derived_reason": "relaunch_after_diagnosis"})
    r.contains("a revision names the run it came from", retry, "pouletrun")
    r.contains("and says why it exists, in the same words /history uses",
               retry, display._DERIVED["relaunch_after_diagnosis"])
    r.check("a run with no parent gets no such line",
            display._DERIVED["relaunch_after_diagnosis"] not in held, held)

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
            _offers(text, "/check", "<name>") and "/status" not in text, text)

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
    r.check("and offers monitoring, under an Actions heading",
            "Actions" in done and _offers(done, "/check", "r1",
                                          display._ACTION_TEXT["/check"]), done)

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

    # ------------------------------------------------------------------ #
    r.section("red means blocked, and nothing else")
    # Red was carrying three meanings at once: the row being edited, a row that
    # must be answered, and an environment blocker. The first is the commonest
    # thing on the screen and is not a problem at all, so a person learned to
    # read red as decoration -- on the two screens where it is the only signal
    # that something is actually wrong.
    #
    # Asserted on RAW output, not the ANSI-stripped view, because the escape
    # code IS the claim being made.
    def painted(fn, *args, **kwargs):
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn(*args, **kwargs)
        return buf.getvalue()

    healthy = painted(display.gate, proposal, "patient-42")
    if colour:
        r.check("a complete run with no blockers draws no red at all",
                display.RED not in healthy,
                healthy[:400])
    r.contains("the header is not an error colour", healthy, display.REVERSE)
    r.contains("but the irreversible verb is amber", healthy, display.AMBER)

    # A blocker is what red is for, and it must still be unmistakable.
    class Finding:
        variable, problem, fix = "RAP_ID", "is not set", "export RAP_ID=def-xyz"
    blocked = painted(display.gate, proposal, "patient-42",
                      blockers=[Finding()])
    r.contains("an environment blocker is red", blocked, display.RED)
    r.check("and withholds /approve", "/approve" not in
            re.sub(r"\033\[[0-9;]*[A-Za-z]", "", blocked))

    # The mirror's pending state: bold+underline, never red.
    from genpipe import mirror as _mirror
    from genpipe import modify
    m = _mirror.from_slots(proposal, name="patient-42")
    rows = "\n".join(display.mirror_lines(m, pending=["protocol"]))
    if colour:
        r.check("a row about to change is not red", display.RED not in rows, rows)
    r.contains("it is underlined instead", rows, display.UNDER)
    r.contains("and carries its own glyph", rows, "◆")

    moved = "\n".join(display.mirror_lines(m, changed=["protocol"]))
    r.contains("a row that has changed stays green", moved, display.GREEN)
    if colour:
        r.check("and is still not red", display.RED not in moved, moved)

    # ------------------------------------------------------------------ #
    r.section("every state is legible with the colour taken away")
    # The palette's first rule (see display.py's note above RESET): colour
    # REINFORCES information, it never carries it. So the test is not that the
    # colours are well chosen -- that is a judgement no assertion can make --
    # but that removing them costs nothing. What is checked here is the
    # ANSI-STRIPPED screen, which is what a light terminal that renders one of
    # these shades badly, a grayscale screenshot, `| tee log.txt` and NO_COLOR
    # all reduce to.
    stripped = drawn(display.run_list, listing_rows)
    for name, glyph, word in (("waiting", "\u25c7", "waiting for approval"),
                              ("running-one", "\u25cf", "running"),
                              ("half-broken", "\u2717", "failed"),
                              ("all-done", "\u2713", "completed"),
                              ("i-stopped-it", "\u2298", "stopped")):
        row = next((l for l in stripped.splitlines() if name in l), "")
        r.check(f"{name} keeps its glyph", glyph in row, row)
        r.check(f"{name} keeps its word", word in row, row)

    r.section("the palette avoids the shades that only work on one background")
    # ANSI 37 is a light grey that disappears on a white terminal, and 36
    # (cyan) is unreadable on it too. Both were in use and both were reported.
    # Asserted as a property of the palette rather than as a search of every
    # screen, so a new screen cannot reintroduce them by copying an old one.
    if colour:
        r.check("nothing is painted ANSI 37 'white'", display.WHITE != "\033[37m")
        r.check("...it is the terminal's own foreground, emphasised",
                display.WHITE == display.BOLD)
        for bucket in runs.SECTION_ORDER:
            shade = display._marks()[bucket][1]
            r.check(f"{bucket}'s mark is not cyan", "36m" not in shade)
            r.check(f"{bucket}'s mark is not blue", "34m" not in shade)


    # ------------------------------------------------------------------ #
    r.section("? means one thing: not enough evidence to say more")
    # The glyph was spent on two unrelated ideas. LAPSED is not one of them:
    # a proposal whose gate interrupt is gone is FULLY established -- the
    # command is on record and the only missing thing is the authorisation
    # slot, which is exactly why it has to be rebuilt rather than approved.
    # Marking a known state with the uncertainty mark taught the mark to mean
    # "unusual", which is how a reader stops being able to tell it from the
    # two rows where the tool genuinely cannot see.
    lapsed = {"name": "gate-gone", "status": "lapsed",
              "held_at": "2026-07-20T09:00:00", "submitted_at": None,
              "job_list": None,
              "reconciled_because": "no live gate — the decision was not left open",
              "proposal": {"command": "genpipes ampliconseq"}}
    unreachable_jobs = {"name": "manifest-lost", "status": "submitted",
                        "held_at": None, "submitted_at": "2026-07-20T09:00:00",
                        "job_list": None, "jobs_seen": 46,
                        "proposal": {"command": "genpipes dnaseq"}}
    glyphs = drawn(display.run_list,
                   [(lapsed, None), (unreachable_jobs, None),
                    (unreachable_record, unreachable_status),
                    (up_to_date, None)])

    def mark_of(name):
        return next(l for l in glyphs.splitlines() if name in l).strip()[0]

    r.equal("a lapsed proposal is marked rebuild, not unknown",
            mark_of("gate-gone"), "\u21bb")
    r.contains("and the row says what to do about it",
               glyphs, "needs rebuilding")
    r.equal("a submission whose manifest is gone IS unknown",
            mark_of("manifest-lost"), "?")
    r.equal("and so is a scheduler that could not be reached",
            mark_of("cant-tell"), "?")
    # "submitted · 46 jobs · no job list on disk" was the old row, under a ?.
    # The glyph was right and the word was not: `submitted` is a past EVENT,
    # and this column answers what state the run is in NOW -- which, with no
    # manifest to ask about, is precisely that we cannot establish it.
    lost_row = next(l for l in glyphs.splitlines() if "manifest-lost" in l)
    r.equal("and it says so, rather than naming the event that got it there",
            lost_row.split("  ")[-1].strip(), "unknown")
    r.check("with the job count left to /check", "46 jobs" not in lost_row,
            lost_row)
    r.check("those two are the only rows ? is spent on",
            [l for l in glyphs.splitlines() if l.strip()[:1] == "?"].__len__() == 2,
            glyphs)
    # Not "no jobs" and not "nothing to run". GenPipes generating no work means
    # every output asked for was already on disk, which is a run that succeeded
    # without spending an allocation.
    r.equal("a run with nothing to do is marked as the success it is",
            mark_of("nothing-to-do"), "\u2713")
    r.contains("and worded so the tick cannot be read as 'I computed this'",
               glyphs, "up to date")

    # ------------------------------------------------------------------ #
    r.section("'already up to date' is a claim, and it needs the evidence for it")
    # WHAT THAT ROW ACTUALLY MEANT. record_outcome() writes jobs_seen 0 and
    # expected_jobs 0 when reconcile() saw three facts agree: a script
    # declaring `# TOTAL: 0`, a clean exit, and no new rows in the job list.
    # That is a real, successful, terminal outcome -- GenPipes generated no
    # work because every output was already on disk -- and any run reaches it
    # by being re-run after it has finished. So the state stays.
    #
    # What does NOT stay is inferring it from an absence. `submitted` with no
    # job list ALSO describes a record from before begin_submission() existed,
    # where jobs_seen is None -- unmeasured, not zero. runs.ran_already is
    # explicit that "absence of a manifest is not evidence of absence of a
    # submission", and the listing was reading exactly that absence as a
    # confirmed success on every legacy row.
    never_counted = {"name": "legacy-blank", "status": "submitted",
                     "held_at": None, "submitted_at": "2026-07-20T09:00:00",
                     "job_list": None,
                     "proposal": {"command": "genpipes rnaseq"}}
    r.check("a counted zero is positive evidence",
            runs.submitted_nothing(up_to_date))
    r.check("an uncounted absence is not",
            not runs.submitted_nothing(never_counted))
    r.check("and neither is an unfinished submission",
            not runs.submitted_nothing(dict(never_counted,
                                            status="submit_unknown",
                                            jobs_seen=0)))
    legacy = drawn(display.run_list, [(never_counted, None), (up_to_date, None)])
    legacy_row = next(l for l in legacy.splitlines() if "legacy-blank" in l)
    r.equal("so a record nobody ever counted reads as unknown",
            legacy_row.split("  ")[-1].strip(), "unknown")
    r.equal("and is marked as uncertain rather than as a success",
            legacy_row.strip()[0], "?")
    r.check("it is never told it finished with nothing to do",
            "up to date" not in legacy_row, legacy_row)
    # And the evidenced one is unaffected -- historical records that DO carry
    # the count keep their outcome.
    r.equal("while a counted zero still says what it is",
            next(l for l in legacy.splitlines()
                 if "nothing-to-do" in l).split("  ")[-1].strip(),
            "up to date")

    # ------------------------------------------------------------------ #
    r.section("jobs the scheduler will not account for are unknown, not failed")
    # status.unknown is the count of jobs in the manifest that sacct did not
    # recognise -- which is what an accounting database aging ids out looks
    # like. runs.list_bucket is right to raise it to ATTENTION (it wants a
    # person) but the word for it is not "failed": nothing broke, and nothing
    # is known to have broken. The old row said "failed · 12 unaccounted for".
    aged_out = {"name": "aged-out", "status": "submitted", "held_at": None,
                "submitted_at": "2026-07-20T09:00:00",
                "job_list": "/s/job_output/RnaSeq.job_list.T9"}
    aged_status = runs.RunStatus(
        counts={}, total=12, resolved=0, unknown=12, finished=False,
        verdict="12 unaccounted for", doomed=0, source="sacct")
    aged = drawn(display.run_list, [(aged_out, aged_status)])
    aged_row = next(l for l in aged.splitlines() if "aged-out" in l)
    r.equal("an unaccounted-for run is uncertain", aged_row.strip()[0], "?")
    r.equal("and says so", aged_row.split("  ")[-1].strip(), "unknown")
    r.check("it is not reported as a failure", "failed" not in aged_row, aged_row)
    # A run with something genuinely broken in it is still a failure, and a
    # doomed one -- queued behind something that already broke -- still is too.
    for label, st in (
            ("something broke", runs.RunStatus(
                counts={"FAILED": 1, "COMPLETED": 4}, total=12, resolved=5,
                unknown=7, finished=False, verdict="1 failed", doomed=0,
                source="sacct")),
            ("dependencies that will never come", runs.RunStatus(
                counts={"PENDING": 7, "COMPLETED": 5}, total=12, resolved=12,
                unknown=0, finished=True, verdict="dead", doomed=7,
                source="sacct + squeue"))):
        broke_row = next(l for l in drawn(
            display.run_list, [(dict(aged_out, name="real-break"), st)]
        ).splitlines() if "real-break" in l)
        r.equal(f"but {label} is still a failure",
                broke_row.split("  ")[-1].strip(), "failed")
        r.equal("marked with the cross", broke_row.strip()[0], "\u2717")

    # ------------------------------------------------------------------ #
    r.section("the glyph is the state, and the state column is its legend")
    # No legend block is printed, and that is a design commitment rather than
    # an omission: every glyph appears beside its own word on every row it is
    # used on, which is a legend that cannot go stale. The property that makes
    # it work is that a glyph never means two different things.
    seen = {}
    for word, glyph in display._STATE_WORDS:
        seen.setdefault(glyph, []).append(word)
    # Each WORD has exactly one glyph -- that is what stops the same state
    # being drawn two ways on two rows.
    r.equal("no state is drawn two different ways",
            len({w for w, _ in display._STATE_WORDS}),
            len(display._STATE_WORDS))
    # A glyph may cover several words, but only words that are the same IDEA.
    # This is the mapping, written out, so a state cannot be filed under a
    # glyph that means something else.
    r.equal("the live glyph covers the two halves of being live",
            sorted(seen[display._LIVE_MARK]), ["queued", "running"])
    r.equal("the cross is spent only on confirmed failure",
            sorted(seen[display._BROKE_MARK]), ["failed", "submission failed"])
    r.equal("the tick covers the two ways a run ends well",
            sorted(seen[display._DONE_MARK]), ["completed", "up to date"])
    # ONE WORD FOR ALL OF IT. Six internal conditions reach this glyph and they
    # say one thing, because they mean one thing to a reader -- the dashboard
    # cannot establish the state -- and lead to one next action, /check.
    r.equal("and ? says one word, however many ways it was reached",
            sorted(seen[display._UNKNOWN_MARK]), ["unknown"])
    for glyph in (display._HELD_MARK, display._REBUILD_MARK,
                  display._STOPPED_MARK):
        r.equal(f"{glyph} means exactly one state", len(seen[glyph]), 1)
    # Every state fits, whole, in the width the table reserves for it -- which
    # is how the reserve is computed, so this is really the assertion that no
    # phrase can be added without the column growing to hold it.
    r.check("no state is longer than the column that prints it",
            max(len(w) for w, _ in display._STATE_WORDS) <= 22,
            [w for w, _ in display._STATE_WORDS])
    r.check("and none of them carries a reason clause",
            not any("\u00b7" in w for w, _ in display._STATE_WORDS))
    r.check("no legend block is printed",
            "legend" not in glyphs.lower(), glyphs)

    # ------------------------------------------------------------------ #
    r.section("/list's actions are ordered by what you do after reading it")
    acted = drawn(display.run_list, listing_rows)
    tail = acted.split("Actions")[1]
    order = [w for w in ("/check", "/diagnose", "/jobs", "/modify", "/approve",
                         "/reject", "/scan")]
    positions = [tail.index(cmd) for cmd in order]
    r.check("understanding first, then preparing, then adopting",
            positions == sorted(positions), tail)
    # /jobs AFTER /diagnose, deliberately. Half the point of the tool is that
    # nobody should have to read a job list by hand; offering the manual path
    # before the interpretation says the opposite.
    r.check("the agent's reading comes before the raw jobs",
            tail.index("/diagnose") < tail.index("/jobs"), tail)
    for cmd, note in (("/check", "see a run's current status"),
                      ("/diagnose", "explain what went wrong"),
                      ("/jobs", "inspect individual jobs"),
                      ("/modify", "change a run before launch"),
                      ("/approve", "launch a run"),
                      ("/reject", "discard a run"),
                      ("/scan", "bring an existing run into the assistant")):
        r.contains(f"{cmd} says why you would use it", tail, note)
    # Implementation words, replaced by the user's goal. "refresh" described a
    # scheduler call to somebody who wanted to know how their run was doing,
    # and "adopt" is this project's own noun for a thing a newcomer would call
    # "bring it in".
    for jargon in ("refresh a launched run", "adopt runs already on disk",
                   "awaiting approval"):
        r.check(f"and never {jargon!r}", jargon not in tail, tail)
    r.equal("three groups, separated by blank lines",
            len([b for b in tail.split("\n\n") if b.strip()]), 3)

    # ------------------------------------------------------------------ #
    r.section("one vocabulary for proposed commands, across every screen")
    # THE DEFECT. Six renderers each wrote their own description for the same
    # seven verbs, and they had drifted: /jobs was "inspect its jobs" on /list,
    # "every job and its state" in /check and /diagnose, and "for its jobs" in
    # /history. Somebody reading four screens in one session had no way to know
    # they were being offered the same command.
    r.check("every canonical verb has exactly one description",
            len(set(display._ACTION_TEXT.values()))
            == len(display._ACTION_TEXT), display._ACTION_TEXT)
    for verb, note in (("/check", "see a run's current status"),
                       ("/diagnose", "explain what went wrong"),
                       ("/jobs", "inspect individual jobs"),
                       ("/modify", "change a run before launch"),
                       ("/approve", "launch a run"),
                       ("/reject", "discard a run"),
                       ("/scan", "bring an existing run into the assistant")):
        r.equal(f"{verb} is described once, canonically",
                display._ACTION_TEXT[verb], note)

    # The wordings that were retired, hunted across every screen that could
    # still be printing one rather than only the screens that were fixed.
    surfaces = "\n".join((
        acted,
        strip(drawn(display.run_status, "walltimefail", runs.RunStatus(
            counts={"COMPLETED": 1, "TIMEOUT": 2}, total=3, resolved=3,
            unknown=0, finished=True, doomed=0, source="sacct", at="09:38",
            reasons={}, verdict="failed",
            root_cause={"step": "gatk_sam_to_fastq", "state": "TIMEOUT",
                        "count": 2, "jobs": []}))),
        drawn(display.diagnosis, "walltimefail",
              {"shaped": True, "manner": "timed out", "cause": "walltime",
               "override": {"gatk_sam_to_fastq":
                            {"cluster_walltime": "24:00:00"}},
               "relaunch": "1-46"}),
        drawn(display.history, [{"name": "old", "status": "submitted",
                                 "source": "agent",
                                 "submitted_at": "2026-07-01T09:00:00"}]),
        drawn(display.status_overview,
              {"NEEDS ATTENTION": [{"name": "a", "line": "3 failed"}]}),
        drawn(display.scan_results, "/s", ["x"], added=["run-a"]),
        drawn(display.run_view, {"command": "genpipes rnaseq"}, "r", "submitted"),
    ))
    for retired in ("read what the logs say", "every job and its state",
                    "how it is doing on the scheduler",
                    "read the logs and explain a failure",
                    "copies it into a new run",
                    "writes this into the run's override ini",
                    "for its jobs", "for what went wrong",
                    "adopts a job list by hand"):
        r.check(f"no screen still says {retired!r}",
                retired not in surfaces, retired)

    # ------------------------------------------------------------------ #
    r.section("a proposed command looks like a proposed command")
    # The heading is the shape. Before it, a suggestion was two loose lines
    # here, a middle-dot run-on there, and a padded block somewhere else --
    # so the one thing all of them were saying had nothing to be recognised by.
    broke_status = runs.RunStatus(
        counts={"COMPLETED": 1, "TIMEOUT": 2}, total=3, resolved=3, unknown=0,
        finished=True, doomed=0, source="sacct", at="09:38", reasons={},
        verdict="failed", root_cause={"step": "gatk_sam_to_fastq",
                                      "state": "TIMEOUT", "count": 2,
                                      "jobs": []})
    chk_broke = strip(drawn(display.run_status, "walltimefail", broke_status))
    r.contains("/check heads its suggestions", chk_broke, "Actions")
    r.check("inside its own panel, not beside it",
            all(l.lstrip().startswith("\u258c")
                for l in chk_broke.splitlines()
                if "Actions" in l or "/diagnose" in l or "/jobs" in l),
            chk_broke)
    # CONCRETE NAMES SURVIVE. Consistency is about presentation, not about
    # replacing something useful with a placeholder: inside /check <name> the
    # reader wants a line they can copy.
    r.check("with the run's real name, not a placeholder",
            _offers(chk_broke, "/diagnose", "walltimefail")
            and "<name>" not in chk_broke, chk_broke)
    # ...while a screen about no particular run keeps the placeholder.
    r.check("while an all-runs screen keeps the placeholder",
            _offers(drawn(display.status_overview,
                          {"NEEDS ATTENTION": [{"name": "a", "line": "x"}]}),
                    "/check", "<name>"))

    r.section("/check proposes what the evidence supports, and never itself")
    # FAILED: there are logs, because a job that broke ran far enough to write
    # one. So the interpretation first, then the raw jobs.
    r.check("a failure offers /diagnose then /jobs",
            chk_broke.index("/diagnose") < chk_broke.index("/jobs"), chk_broke)
    r.check("and never offers /check to somebody already inside /check",
            "/check" not in chk_broke, chk_broke)

    # JOBS UNACCOUNTED FOR -- status.unknown, which is jobs in THIS MANIFEST
    # that sacct would not account for. There is no log for /diagnose to read,
    # so offering it sends somebody to a screen that can only report finding
    # nothing. Not-knowing is not a failure.
    #
    # THIS IS NOT /list's `? unknown`, which is a wider display state with six
    # sources. Five of them never reach run_status with a manifest, and the
    # next action for a `? unknown` LISTING ROW is /check -- which is what
    # /list offers. The two are asserted together below so the distinction
    # cannot quietly collapse.
    unsure = strip(drawn(display.run_status, "aged-out", runs.RunStatus(
        counts={}, total=12, resolved=0, unknown=12, finished=False,
        doomed=0, source="sacct", at="09:38", reasons={},
        verdict="12 unaccounted for")))
    r.check("a run with unaccounted-for jobs is not sent to /diagnose",
            "/diagnose" not in unsure, unsure)
    r.check("it is offered the manifest instead",
            _offers(unsure, "/jobs", "aged-out"), unsure)
    # The listing's own answer for the same run, and for every other row that
    # reads `? unknown`: ask the scheduler.
    r.check("while a `? unknown` listing row is pointed at /check",
            _offers(acted, "/check", "<name>")
            and acted.index("/check") < acted.index("/diagnose"), acted)

    # NOTHING WORTH SAYING: an Actions block printed for symmetry teaches
    # people to stop reading the heading.
    for label, st in (
            ("a healthy running run", runs.RunStatus(
                counts={"RUNNING": 6, "COMPLETED": 9}, total=15, resolved=15,
                unknown=0, finished=False, doomed=0, source="sacct", at="09:38",
                reasons={}, verdict="running")),
            ("a cleanly finished run", runs.RunStatus(
                counts={"COMPLETED": 10}, total=10, resolved=10, unknown=0,
                finished=True, doomed=0, source="sacct", at="09:38",
                reasons={}, verdict="complete")),
            ("a scheduler that could not be reached", runs.RunStatus(
                counts={}, total=15, resolved=0, unknown=15, finished=False,
                doomed=0, source="unavailable", at="09:38", reasons={},
                verdict="scheduler unreachable"))):
        screen = strip(drawn(display.run_status, "r", st))
        r.check(f"{label} gets no empty Actions block",
                "Actions" not in screen, screen)

    r.section("what follows a submission depends on what the submission did")
    def rec(status, **kw):
        base = {"status": status, "jobs_seen": None, "expected_jobs": None,
                "retry_safe": False, "outcome_detail": ""}
        base.update(kw)
        return base

    # A FAILED SUBMISSION IS NOT A FAILED RUN. What broke is the launch, so
    # there are no pipeline logs and /diagnose has nothing to read. The
    # question that decides what to do next is whether anything reached the
    # scheduler -- and answering it wrong runs a pipeline twice.
    unsafe = strip(drawn(display.post_approve, "amp-aug",
                         rec(runs.SUBMIT_FAILED)))
    r.contains("a failed submission heads its actions", unsafe, "Actions")
    r.check("and asks the scheduler first", _offers(unsafe, "/check", "amp-aug"),
            unsafe)
    r.check("before offering to rebuild",
            unsafe.index("/check") < unsafe.index("/modify"), unsafe)
    r.check("it is never sent to /diagnose — there are no job logs",
            "/diagnose" not in unsafe, unsafe)

    # ...unless the scheduler itself was asked and came back empty, which is
    # the one condition under which nothing is out there.
    safe = strip(drawn(display.post_approve, "amp-0813",
                       rec(runs.SUBMIT_UNKNOWN, retry_safe=True)))
    r.check("a provably quiet retry goes straight to /modify",
            _offers(safe, "/modify", "amp-0813")
            and "/check" not in safe, safe)
    r.check("with /reject beside it", _offers(safe, "/reject", "amp-0813"), safe)
    r.check("and /modify before /reject, as everywhere else",
            safe.index("/modify") < safe.index("/reject"), safe)

    # An interrupted launch is the same question as an unconfirmed one.
    mid = strip(drawn(display.post_approve, "amp-x", rec(runs.SUBMITTING)))
    r.check("a launch that never came back asks the scheduler too",
            _offers(mid, "/check", "amp-x"), mid)

    r.section("pre-launch screens keep their three verbs, in their order")
    held_view = strip(drawn(display.run_view,
                            {"command": "genpipes rnaseq"}, "rnaseq-0901",
                            "held"))
    for verb in ("/modify", "/approve", "/reject"):
        r.check(f"a held run is offered {verb}",
                _offers(held_view, verb, "rnaseq-0901",
                        display._ACTION_TEXT[verb]), held_view)
    r.check("preparing before launching before discarding",
            held_view.index("/modify") < held_view.index("/approve")
            < held_view.index("/reject"), held_view)
    # A lapsed proposal cannot be approved -- that is the whole reason the
    # status exists -- so the verb it would refuse is not offered.
    lapsed_view = strip(drawn(display.run_view,
                              {"command": "genpipes rnaseq"}, "gate-gone",
                              "lapsed"))
    r.check("a lapsed proposal is offered the rebuild",
            _offers(lapsed_view, "/modify", "gate-gone"), lapsed_view)
    r.check("and never an approval that would be refused",
            "/approve" not in lapsed_view, lapsed_view)

    r.section("/modify is described by what it does to THIS run")
    r.equal("a held run: it edits the proposal",
            display.modify_text("held"), "change a run before launch")
    r.equal("a lapsed one too -- nothing has launched",
            display.modify_text("lapsed"), "change a run before launch")
    for launched in ("submitted", "submitting", "submit_failed",
                     "submit_unknown", "gone", "abandoned"):
        r.equal(f"a {launched} run: it builds a copy",
                display.modify_text(launched),
                "build a revised copy; this run is untouched")
    # The internal key never reaches a screen as something to type.
    for screen in (strip(drawn(display.run_view, {"command": "genpipes dnaseq"},
                               "r", "submitted")),
                   strip(drawn(display.diagnosis, "r",
                               {"shaped": True, "manner": "m", "fix": "f",
                                "override": {"s": {"k": "v"}},
                                "evidence": [], "uncertain": [],
                                "relaunch": "", "confidence": ""}, []))):
        r.check("the lookup key is never printed", "@launched" not in screen,
                screen)
        r.contains("the fork wording is", screen, "this run is untouched")
    # ...and /list, which is about runs in every state, keeps the generic one.
    r.contains("while /list keeps the pre-launch wording", acted,
               "change a run before launch")

    r.section("the gate keeps its own words, and that is deliberate")
    # THE ONE PLACE A DESCRIPTION IS NOT USED. gate_box is where an allocation
    # is actually spent, and "submits to Slurm — cannot be undone" is a
    # consequence rather than a description of what the verb means. Replacing
    # it with "launch a run" for the sake of a table would take the warning off
    # the only screen whose keystroke is irreversible.
    gate = strip(drawn(display.gate,
                       {"command": "genpipes rnaseq", "generated": None},
                       "rnaseq-0901"))
    r.contains("the gate still states the consequence", gate,
               "cannot be undone")
    r.check("and does not borrow the listing's gentler wording",
            display._ACTION_TEXT["/approve"] not in gate, gate)

    r.section("a diagnosis carries no single confidence label over everything")
    shaped = {"shaped": True, "manner": "killed by TIMEOUT after 00:10:22",
              "cause": "the log's last entry is a progress line",
              "evidence": ["sacct: TIMEOUT, Elapsed 00:10:22"],
              "fix": "restore cluster_walltime to 35:00:00",
              "override": {"gatk_sam_to_fastq": {"cluster_walltime": "35:00:00"}},
              "relaunch": "-s 1-23",
              "uncertain": ["whether 35:00:00 is enough for this input",
                            "why this input needed more than ten minutes"],
              "confidence": "likely"}
    diag = strip(drawn(display.diagnosis, "audit-0805", shaped, []))
    # THE BADGE IS GONE. It printed one word over a screen whose first rows are
    # sacct facts -- "likely" above a job id, a state and a limit that are all
    # certain. A label spanning claims of different standing takes its value
    # from the weakest one and defames the rest.
    r.check("no global badge, even when the model still supplies one",
            "likely" not in diag, diag)
    r.contains("the heading is just the diagnosis", diag, "diagnosis")
    # ...and the doubt is where the doubt is.
    r.contains("what is not established has its own rows", diag,
               "not established")
    r.contains("naming the untested value", diag,
               "whether 35:00:00 is enough")
    r.check("beside the fix it is about, not above the facts",
            diag.index("fix") < diag.index("not established"), diag)
    # The facts above it are stated plainly, with nothing hedging them.
    head = diag[:diag.index("not established")]
    r.contains("the scheduler's finding is stated flatly", head, "TIMEOUT")
    r.check("with no adjective attached to it",
            "likely" not in head and "unclear" not in head, head)
    # A diagnosis with nothing unestablished prints no such block rather than
    # an empty one.
    plain = strip(drawn(display.diagnosis, "x",
                        dict(shaped, uncertain=[], confidence=""), []))
    r.check("and a diagnosis with no unknowns prints no empty block",
            "not established" not in plain, plain)

    r.section("the panel names the job, and does not read 0:0 as 'fine'")
    # A paired tumour/normal run has two jobs per step whose names differ by
    # one character. Naming only the step left the reader -- and the model --
    # to work out which one, and on 2026-08-05 the answer came back as
    # "COLO829N (or T -- one of the two)", leading with the sample that had
    # COMPLETED in 00:01:39.
    audit = drawn(display.triage, "audit-0805", {
        "failed_total": 33, "broke_total": 1, "cancelled_total": 32,
        "steps_affected": 23, "truncated": 18,
        "findings": [
            {"step": "gatk_sam_to_fastq", "count": 1, "state": "TIMEOUT",
             "job": "gatk_sam_to_fastq.tumorPair_COLO829T",
             "job_id": "18382352", "maxrss": None, "exit_code": "0:0",
             "ran": True, "log": "/p/job_output/gatk/x_T.o", "log_tail": "..."},
            {"step": "trim_fastp", "count": 1, "state": "CANCELLED",
             "job": "trim_fastp.tumorPair_COLO829T", "job_id": "18382354",
             "maxrss": None, "exit_code": "0:0", "ran": False,
             "log": None, "log_tail": None}]})
    r.contains("the exact failing job is named", audit,
               "gatk_sam_to_fastq.tumorPair_COLO829T")
    r.contains("with the id sacct and the log filename both agree on",
               audit, "18382352")
    # TIMEOUT with ExitCode 0:0 is exactly what Slurm reports for a job the
    # walltime enforcer killed -- the job never returned a failing code of its
    # own. Printed bare beside "timeout" it reads as a contradiction.
    timeout_row = audit.split("trim_fastp")[0]
    r.contains("a 0:0 beside a timeout is explained, not left bare",
               timeout_row, "the state above is what stopped it")
    r.check("and the state is still the thing that says it failed",
            "timeout" in timeout_row, timeout_row)
    # A cancelled job never started, so its recorded 0:0 is the absence of an
    # exit status rather than one. Thirty-two identical copies of a number
    # that means nothing about any of them is not evidence.
    # The cancelled block runs from its step heading to the end of the panel.
    cancelled_row = audit[audit.index("trim_fastp"):]
    r.check("a job that never ran shows no exit code at all",
            "exit code" not in cancelled_row, cancelled_row)
    r.contains("only that it never ran", cancelled_row, "never ran, no log")

    r.section("agent activity is drawn inside its own gutter, at any width")
    # THE DEFECT. _rule printed every line raw. A generated command line or a
    # job_list row is routinely 150-200 columns, so the TERMINAL wrapped it --
    # at column zero, under the gutter and left of the rule it was supposed to
    # sit beside. On a /diagnose screen the overflow collided with the ▌ panel.
    display.set_verbose(True)
    long_cmd = ("module load mugqic/genpipes/6.1.1 && genpipes dnaseq --help "
                "2>&1 | sed -n '/somatic_fastpass/,/^$/p' | head -80\n"
                "head -50 /home/p/genpipes_somatic_fastpass/COLO829_somatic_"
                "fastpass_cit/job_output/DnaSeq.somatic_fastpass.job_list."
                "2026-08-05T09.20.45")
    long_out = ("18382365    preprocess_vcf.panel.somatic.tumorPair_COLO829    "
                "18382364    preprocess_vcf/preprocess_vcf.panel.somatic."
                "tumorPair_COLO829_2026-08-05T09.20.45.o")
    for cols in (60, 80, 100, 120):
        shown = strip(drawn(_at_width, cols, display._draw,
                            {"kind": "code", "text": long_cmd, "label": "BASH"}))
        shown += strip(drawn(_at_width, cols, display._draw,
                             {"kind": "observation", "text": long_out}))
        rows = [l for l in shown.splitlines() if l.strip()]
        widest = max(display.cells(l) for l in rows)
        r.check(f"nothing overflows at {cols} columns", widest <= cols,
                f"widest={widest}\n{shown}")
        # EVERY row stays inside the block. A continuation that starts at
        # column zero is the bug, not a cosmetic issue: it lands under the
        # gutter and reads as a different kind of line.
        r.check(f"every row keeps the gutter at {cols}",
                all(l.lstrip().startswith("\u258f") for l in rows), shown)
        r.check(f"and continuations are indented past it at {cols}",
                any(l.startswith("\u258f   ") for l in rows), shown)
    # The content survives the wrap -- this folds, it does not truncate.
    wide = strip(drawn(_at_width, 80, display._draw,
                       {"kind": "code", "text": long_cmd, "label": "BASH"}))
    joined = "".join(l.lstrip("\u258f ").rstrip() for l in wide.splitlines())
    for piece in ("mugqic/genpipes/6.1.1", "somatic_fastpass", "job_list"):
        r.contains(f"{piece!r} survives the wrap", joined, piece)
    display.set_verbose(False)

    r.section("a verbose block separates what ran from what came back")
    display.set_verbose(True)
    pair = strip(drawn(_at_width, 88, lambda: (
        display._draw({"kind": "code", "text": "head -50 /a/very/long/path",
                       "label": "BASH"}),
        display._draw({"kind": "observation",
                       "text": "Protocol somatic_fastpass\n1 gatk_sam_to_fastq"}))))
    # THE TOOL, NAMED. "code" is the routing tag the model writes for our own
    # router; "bash" is the thing a reader recognises.
    r.contains("the command is captioned with the tool", pair, "bash")
    r.check("not with the internal routing word", "code" not in pair, pair)
    r.contains("and its output is captioned as output", pair, "output")
    r.check("command before output", pair.index("bash") < pair.index("output"),
            pair)
    r.check("with a break between them, and the rule unbroken",
            all(l.lstrip().startswith("\u258f")
                for l in pair.splitlines() if l.strip()), pair)
    # An observation with no command above it needs no boundary drawn against
    # nothing -- its block was hidden or folded and it is the only thing here.
    display._open_rule = None
    lone = strip(drawn(_at_width, 88, display._draw,
                       {"kind": "observation", "text": "a line with no command above it"}))
    r.check("a lone observation is not captioned",
            "output" not in lone and "bash" not in lone, lone)
    display.set_verbose(False)

    r.section("machine output is clipped in the middle, keeping both ends")
    long_text = "\n".join(f"line {i}" for i in range(200))
    clipped = display._clipped(long_text)
    r.contains("the head is kept", clipped, "line 0")
    r.contains("the tail is kept", clipped, "line 199")
    r.contains("and the gap is counted", clipped, "more lines")
    r.check("so a screen of output cannot bury the reply",
            len(clipped.splitlines()) < 20, clipped)

    r.section("an Actions block fits the window it is drawn in")
    # _hint() used to do this for /diagnose's two lines and nothing else did it
    # for anybody: at sixty columns a long run name plus a description
    # overflowed, and the terminal restarted the overflow at column ZERO --
    # left of and underneath the panel edge it was supposed to sit beside.
    long_name = "dnaseq-somatic-fastpass-0805-rerun"
    for cols in (60, 80, 100, 120):
        narrow = strip(drawn(_at_width, cols, display.actions,
                             [("/diagnose", long_name), ("/jobs", long_name)],
                             f"  {display.DIM}\u258c{display.RESET}"))
        widest = max(display.cells(l) for l in narrow.splitlines())
        r.check(f"nothing overflows at {cols} columns", widest <= cols, narrow)
        # And the description is never the half that gets dropped.
        r.contains(f"the description survives at {cols}", narrow,
                   display._ACTION_TEXT["/diagnose"])
        r.contains(f"so does the run name at {cols}", narrow, long_name)

    r.section("a submission that did not finish says so, rather than counting to zero")
    # "failed · 0 jobs" was the old rendering, and the number in it is about the
    # wrong noun -- nothing failed 0 jobs. These three statuses reach the
    # listing with no RunStatus at all, and what is known about them is what
    # happened to the SUBMISSION.
    trouble = []
    for status_word in ("submit_failed", "submit_unknown", "submitting"):
        trouble.append(({"name": f"run-{status_word}", "status": status_word,
                         "held_at": None, "submitted_at": "2026-07-24T09:00:00",
                         "job_list": None,
                         "proposal": {"command": "genpipes ampliconseq"}}, None))
    out_t = drawn(display.run_list, trouble)

    def trouble_row(status_word):
        return next(l for l in out_t.splitlines() if f"run-{status_word}" in l)

    for status_word, expect in (("submit_failed", "submission failed"),
                                ("submit_unknown", "unknown"),
                                ("submitting", "unknown")):
        row_t = trouble_row(status_word)
        r.equal(f"{status_word} is said in one whole phrase",
                row_t.split("  ")[-1].strip(), expect)
        r.check("and never as a job count", "0 jobs" not in row_t, row_t)
        r.check("nor as a scheduler observation the listing cannot fit",
                "nothing on the scheduler" not in row_t, row_t)

    # ------------------------------------------------------------------ #
    r.section("the dashboard says 'unknown' once, however it got there")
    # THREE PHRASES FOR ONE FACT. "submission unconfirmed", "submission
    # interrupted" and "unknown" were three vocabulary items meaning the same
    # thing to a reader -- this screen cannot establish the state -- with the
    # same next action, /check. Two of the three differed from each other only
    # in WHERE THIS TOOL'S OWN PROCESS DIED, which is not a fact about anybody's
    # run. So the column says one word.
    r.equal("an unestablished outcome and an interrupted launch read alike",
            trouble_row("submit_unknown").split("  ")[-1].strip(),
            trouble_row("submitting").split("  ")[-1].strip())
    for gone in ("submission unconfirmed", "submission interrupted"):
        r.check(f"{gone!r} is no longer a state of its own",
                gone not in out_t and gone not in
                {w for w, _ in display._STATE_WORDS}, out_t)

    # AND NOTHING UNDERNEATH MERGED. This is one column choosing one word; the
    # registry still keeps the two statuses apart, and everything that reasons
    # about them still sees the difference.
    r.check("the registry still distinguishes them",
            runs.SUBMITTING != runs.SUBMIT_UNKNOWN)
    for status_word in ("submitting", "submit_unknown", "submit_failed"):
        rec = {"name": f"run-{status_word}", "status": status_word,
               "job_list": None}
        r.equal(f"{status_word} is still its own registry status",
                rec["status"], status_word)
        r.check("and is still filed where it always was",
                runs.list_bucket(rec, None) == runs.ATTENTION_BUCKET)
    # The evidence each one carries is what the screens with room for it read
    # to tell them apart -- the outcome detail, the count that went out first,
    # and whether a retry is safe. None of that moved.
    unsure_detail = drawn(display.post_approve, "run-submit_unknown",
                          {"name": "run-submit_unknown",
                           "status": "submit_unknown", "jobs_seen": 12,
                           "outcome_detail": "the outcome of the submission "
                                             "was never established"})
    r.contains("an unconfirmed submission is still explained in full",
               unsure_detail, "never established")
    r.contains("with the jobs that did go out still counted",
               unsure_detail, "12 jobs")
    r.check("and is still not called a failure there either",
            "the submission failed" not in unsure_detail, unsure_detail)
    broke_detail = drawn(display.post_approve, "run-submit_failed",
                         {"name": "run-submit_failed", "status": "submit_failed",
                          "outcome_detail": "the submission command reported "
                                            "a failure"})
    r.contains("while a failed one still says so", broke_detail,
               "the submission failed")

    # ------------------------------------------------------------------ #
    r.section("an unconfirmed submission is not a confirmed failure")
    # THE CONCEPTUAL DEFECT THIS CLOSES. All three statuses above are filed
    # under ATTENTION by runs.list_bucket, and the listing took its glyph from
    # the bucket -- so `submit_unknown` was drawn with the same red ✗ as a
    # submission that demonstrably failed. runs.py is explicit that
    # SUBMIT_UNKNOWN "is the honest state, and the one that must never be
    # quietly upgraded to either neighbour", and a red cross IS that upgrade:
    # it tells the reader nothing reached the cluster, when what is actually
    # known is that nobody could establish whether a full pipeline did. Those
    # two ask for opposite next actions.
    r.equal("a submission the command itself reported failing keeps the cross",
            trouble_row("submit_failed").strip()[0], "✗")
    r.equal("an unestablished outcome is uncertainty, not failure",
            trouble_row("submit_unknown").strip()[0], "?")
    r.equal("and so is a submission that never came back",
            trouble_row("submitting").strip()[0], "?")
    if colour:
        painted_t = io.StringIO()
        with redirect_stdout(painted_t):
            display.run_list(trouble)
        painted_t = painted_t.getvalue()
        unsure = next(l for l in painted_t.splitlines()
                      if "run-submit_unknown" in l)
        r.check("and it is not painted like a failure either",
                display.RED not in unsure, unsure)

    # SUBMIT_FAILED "says nothing about what it managed to submit first" (see
    # runs.py's status table). That matters enormously -- it decides whether a
    # retry is safe -- and it is exactly the kind of thing a dashboard cannot
    # carry and /check can. What /list owes is the state.
    partial = drawn(display.run_list,
                    [({"name": "half-out", "status": "submit_failed",
                       "held_at": None, "submitted_at": "2026-07-24T09:00:00",
                       "job_list": None, "jobs_seen": 12,
                       "proposal": {"command": "genpipes dnaseq"}}, None)])
    half_row = next(l for l in partial.splitlines() if "half-out" in l)
    r.equal("a partly-out submission still reads as one state",
            half_row.split("  ")[-1].strip(), "submission failed")
    r.check("with the job count left to the screen that can hold it",
            "12 jobs" not in half_row, half_row)

    # ------------------------------------------------------------------ #
    r.section("/check separates the failure from what followed from it")
    # The old block hung all of this off one label, "root cause": the step, the
    # limit, the individual jobs, and the downstream cancellations. Four of
    # those are evidence about the failure and the fifth is its CONSEQUENCE,
    # and nothing on screen said which was which.
    cause_status = runs.RunStatus(
        counts={"COMPLETED": 1, "TIMEOUT": 2, "CANCELLED": 43}, total=46,
        resolved=46, unknown=0, finished=True, doomed=0, source="sacct",
        at="09:38", reasons={}, verdict="failed, nothing still running",
        root_cause={"step": "gatk_sam_to_fastq", "state": "TIMEOUT", "count": 2,
                    "timelimit": "00:01:00", "cancelled_after": 43,
                    "job": "gatk_sam_to_fastq.tumorPair_COLO829N",
                    "elapsed": "00:01:01", "maxrss": None,
                    "jobs": [{"name": "gatk_sam_to_fastq.tumorPair_COLO829N",
                              "elapsed": "00:01:01"},
                             {"name": "gatk_sam_to_fastq.tumorPair_COLO829T",
                              "elapsed": "00:01:01"}]})
    chk = strip(drawn(display.run_status, "walltimefail", cause_status))
    # The LABEL COLUMN, not a substring search: "timed out" is also the last
    # two words of the first-failure line, and a naive index() finds that one
    # and reports the block as out of order.
    labels = [l.split("▌")[-1][:_CAUSE_W].strip()
              for l in chk.splitlines() if "▌" in l]
    labels = [w for w in labels if w in
              ("first failure", "walltime limit", "timed out", "impact")]
    r.equal("four labels, in causal order", labels,
            ["first failure", "walltime limit", "timed out", "impact"])
    r.contains("what broke is the step, with how many of it",
               chk, "gatk_sam_to_fastq")
    r.contains("and how it broke, in the scheduler's own word",
               chk, "2 jobs timed out")
    r.contains("the limit is labelled as a limit", chk, "walltime limit      00:01:00")
    r.contains("the jobs are labelled as the jobs that timed out",
               chk, "tumorPair_COLO829N")
    r.contains("and their elapsed time says it is elapsed time",
               chk, "ran 00:01:01")
    r.contains("the cancellations are impact, not cause", chk, "impact")
    # /check reads sacct and nothing else. "root cause" claims to have found
    # the reason, which is a claim about logs -- and /diagnose is the command
    # that reads those.
    r.check("nothing on this screen claims to be a root cause",
            "root cause" not in chk, chk)
    r.check("and the log-level question is handed to /diagnose",
            _offers(chk, "/diagnose", "walltimefail"), chk)

    no_limit = runs.RunStatus(
        counts={"OUT_OF_MEMORY": 1}, total=1, resolved=1, unknown=0,
        finished=True, doomed=0, source="sacct", at="09:38", reasons={},
        verdict="failed",
        root_cause={"step": "picard_sort_sam", "state": "OUT_OF_MEMORY",
                    "count": 1, "timelimit": None, "cancelled_after": 0,
                    "maxrss": "31.4G",
                    "jobs": [{"name": "picard_sort_sam.NA12878",
                              "elapsed": "02:10:00", "maxrss": "31.4G"}]})
    oom = strip(drawn(display.run_status, "oom", no_limit))
    r.check("a walltime is not offered as the explanation for an OOM",
            "walltime limit" not in oom, oom)
    r.contains("the state's own word labels the jobs", oom, "out of memory")
    r.contains("and the memory it actually reached is labelled", oom, "peak 31.4G")
    r.check("an impact row appears only when something was cancelled",
            "impact" not in oom, oom)

    # ------------------------------------------------------------------ #
    r.section("the helix is one continuous right-handed double helix")
    art = display._HELIX
    r.check("every row is the same width",
            {len(row) for row in art} == {display.HELIX_W}, [len(x) for x in art])
    r.check("and that width is what the module says it is",
            display.HELIX_W == 13)
    r.check("it is shorter than the panel beside it", len(art) <= 12)
    r.check("no glyph outside the depth ramp and the rungs",
            set("".join(art)) <= set(" \u2593\u2592\u2591\u2500"), set("".join(art)))
    r.check("two strands on every row, never one and never three",
            all(sum(row.count(g) for g in "\u2593\u2592\u2591") == 2 for row in art),
            [sum(row.count(g) for g in "\u2593\u2592\u2591") for row in art])

    def strands(row):
        return sorted(i for i, c in enumerate(row) if c in "\u2593\u2592\u2591")

    # RIGHT-HANDED, which is the one claim here that is about biology rather
    # than about drawing. In every side view of B-DNA the front-facing segments
    # run lower-left to upper-right -- so reading DOWN the page, the strand in
    # front moves right to left. Checked over each run of rows where a front
    # strand (▓) is visible, and it must never once move the other way.
    fronts = [row.index("\u2593") for row in art if "\u2593" in row]
    runs_down, current = [], [fronts[0]]
    for prev, nxt in zip(fronts, fronts[1:]):
        (current.append(nxt) if nxt < prev else runs_down.append(current) or
         current.__setitem__(slice(None), [nxt]))
    runs_down.append(current)
    r.check("the front strand only ever sweeps right to left, going down",
            all(all(b < a for a, b in zip(seq, seq[1:])) for seq in runs_down),
            fronts)
    r.check("and it does so more than once, so the twist is a twist",
            len(runs_down) >= 2, runs_down)
    # The two backbones wind around ONE axis: every row's pair is symmetric
    # about the same centre column.
    centres = {sum(strands(row)) / 2 for row in art}
    r.check("both backbones wind around one common axis", centres == {6.0}, centres)
    # A rung is drawn only where a base pair is not edge-on to the viewer.
    for row in art:
        lo, hi = strands(row)
        gap = hi - lo - 1
        r.check(f"rungs span exactly the gap on {row!r}",
                row.count("\u2500") == (gap if gap >= 2 else 0), row)

    r.section("and it survives the colour being taken away")
    plain_logo = strip("\n".join(display._helix()))
    r.check("depth is drawn in density as well as hue",
            all(g in plain_logo for g in "\u2593\u2592\u2591"), plain_logo)
    r.check("stripped, it is exactly the art", plain_logo.splitlines() == art)

    # ------------------------------------------------------------------ #
    r.section("both palettes reach every screen, and NO_COLOR reaches it too")
    was = dict(os.environ)
    try:
        for name, env in (("dark", {"GENPIPE_THEME": "dark", "COLORTERM": "truecolor"}),
                          ("light", {"GENPIPE_THEME": "light", "COLORTERM": "truecolor"}),
                          ("none", {"NO_COLOR": "1"})):
            got = display.retheme(env)
            r.equal(f"retheme picks {name}", got, name)
            screen = painted(display.run_list, listing_rows)
            screen += painted(display.run_status, "walltimefail", cause_status)
            screen += painted(display.banner, "Anthropic", "claude-sonnet-5")
            if name == "none":
                r.check("no escape sequence is emitted at all",
                        "\033[" not in screen, screen[:200])
            else:
                r.contains(f"{name} paints in 24-bit colour", screen, "\033[38;2;")
                r.check("and never in a shade chosen for the other background",
                        theme.palette(
                            {"GENPIPE_THEME": "light" if name == "dark" else "dark",
                             "COLORTERM": "truecolor"})["muted"] not in screen)
            # WHATEVER THE PALETTE, THE WORDS AND GLYPHS ARE THE SAME. This is
            # the property that makes a wrongly-guessed theme a readability
            # problem rather than a correctness one.
            bare = strip(screen)
            for needle in ("waiting for approval", "\u25c7", "first failure",
                           "impact", "GenPipes assistant"):
                r.contains(f"{name}: {needle!r} is there with or without colour",
                           bare, needle)
    finally:
        os.environ.clear()
        os.environ.update(was)
        display.retheme()
    r.check("and the palette is put back for whatever runs next",
            display.THEME == theme.resolve())

    # ------------------------------------------------------------------ #
    r.section("the reorder hint sits on the row the keys would act on")
    # `[` and `]` move the ini UNDER THE CURSOR, so the hint belongs where the
    # cursor is. It began as `· [ ] reorders` in every included option's
    # description, which is the same sentence repeated down the list -- and it
    # said which keys without saying which way, which is the half somebody
    # needs at the moment of pressing one.
    from genpipe import mirror as _mirror
    _prop = {"slots": {"pipeline": "dnaseq", "protocol": "somatic_fastpass",
                       "inis": ["dnaseq.base.ini", "rorqual.ini",
                                "dnaseq.cancer.ini", "override_walltime.ini"]}}
    _m = _mirror.from_slots(_prop, name="poulet")
    _changes = {"config": ["dnaseq.base.ini", "rorqual.ini",
                           "dnaseq.cancer.ini"]}
    _opts = modify.options_for("config", _prop, {"config": ["spare.ini"]},
                               pending=_changes,
                               removed=["override_walltime.ini"])
    _entries = modify.panel_entries(_m, [l.row for l in _m.lines if l.row],
                                    open_row="config", choices=_opts,
                                    changes=_changes)
    _draw = display.modify_panel(
        lambda: _entries, changes=lambda: _changes, required=lambda: {},
        notes=lambda: {}, typed=lambda: "", open_of=lambda: "config",
        details=lambda: True)

    def _rows(cursor):
        return [strip(line).rstrip() for line in _draw(cursor, set())]

    def _row_with(cursor, name):
        return next((x for x in _rows(cursor) if name in x), "")

    # Cursor on the second included ini.
    r.contains("the highlighted included row says what enter does",
               _row_with(1, "rorqual.ini"), "enter removes")
    r.contains("and which way each bracket moves it",
               _row_with(1, "rorqual.ini"), "[ up · ] down")
    r.check("the hint names both directions rather than just the keys",
            "reorders" not in "\n".join(_rows(1)), _rows(1))

    for other in ("dnaseq.base.ini", "dnaseq.cancer.ini"):
        row = _row_with(1, other)
        r.contains(f"{other} still says what it is", row, "on the stack")
        r.check(f"and {other} carries no reorder hint", "up ·" not in row, row)
    r.equal("exactly one row carries it",
            sum("up ·" in x for x in _rows(1)), 1)

    # It follows the cursor rather than belonging to a position.
    r.contains("moving the cursor moves the hint",
               _row_with(2, "dnaseq.cancer.ini"), "[ up · ] down")
    r.check("and it leaves the row it came from",
            "up ·" not in _row_with(2, "rorqual.ini"),
            _row_with(2, "rorqual.ini"))

    # The two states the keys do nothing to.
    r.contains("a removed ini says how to bring it back",
               _row_with(3, "override_walltime.ini"), "removed · enter restores")
    r.equal("and highlighted, it still offers no reordering",
            sum("up ·" in x for x in _rows(3)), 0)
    r.equal("nor does a merely available ini",
            sum("up ·" in x for x in _rows(4)), 0)
    r.contains("which still says what enter would do to it",
               _row_with(4, "spare.ini"), "enter adds it")

    # ------------------------------------------------------------------ #
    r.section("an interruption does not look like a crash")
    # Ctrl-C used to print "Stopped." over an unconditional claim that nothing
    # had been submitted, with biomni's stack trace arriving underneath it a
    # moment later. Stopping a reply is an ordinary thing to do and has to read
    # that way: no red, no traceback, and no reassurance nobody checked.
    text = drawn(display.interrupted, "Nothing reached the scheduler.")
    r.contains("it says it was interrupted", text, "Interrupted")
    r.contains("and asks what to do instead", text, "instead")
    r.contains("carrying whatever the caller established", text,
               "Nothing reached the scheduler.")
    r.check("it is not worded as a failure",
            "Stopped" not in text and "Error" not in text, text)
    r.check("and it is not painted like one",
            display.RED not in painted(display.interrupted, "x"),
            painted(display.interrupted, "x"))

    # The claim is the CALLER'S, so a caller that cannot make it does not.
    unsure = drawn(display.interrupted,
                   "'poulet' had already been approved — /check it.")
    r.check("a caller with no reassurance to give prints none",
            "Nothing reached the scheduler" not in unsure, unsure)
    r.contains("and says what it does know instead", unsure, "already been approved")

    quiet = drawn(display.interrupted)
    r.contains("with no claim at all it still says it was interrupted",
               quiet, "Interrupted")

    left = drawn(display.interrupted, "x", note="a tool is still finishing")
    r.contains("a straggler is named rather than hidden", left,
               "a tool is still finishing")

    # ====================================================================== #
    r.section("model prose: the three markers, and everything else left alone")
    # THE FIXTURES ARE REAL MODEL OUTPUT. The scripted stand-in in
    # fakecluster._chat() cannot emit markdown -- deliberately, it refuses to
    # invent GenPipes prose -- so no suite here could observe what a real model
    # actually writes until these went in. Two of them are copied verbatim from
    # a session transcript.

    def prose(text):
        return "\n".join(display._prose(text))

    def painted_prose(text):
        return "\n".join(display._prose(text))

    plain = lambda t: strip(prose(t))

    r.equal("**bold** loses its asterisks", plain("**What they're for**"),
            "What they're for")
    r.check("and is emphasised", display.BOLD in painted_prose("**a**"),
            painted_prose("**a**"))
    r.equal("`code` loses its backticks", plain("run `PAIRED_END` here"),
            "run PAIRED_END here")
    r.check("and is set in the secondary role, never the command green",
            display.SECONDARY in painted_prose("`x`")
            and display.GREEN not in painted_prose("`x`"),
            painted_prose("`x`"))
    r.equal("a leading bullet becomes one glyph",
            plain("- **Sample** \u2014 the name"),
            "\u2022 Sample \u2014 the name")
    r.equal("an indented bullet keeps its indent",
            plain("    - two"), "    \u2022 two")

    # ---- the nested case, from the transcript --------------------------- #
    # Code inside emphasis. The naive rendering closes the inner span with
    # RESET, which clears every attribute rather than the colour alone, and the
    # word after it silently loses the bold it was meant to keep.
    r.equal("**`code`** keeps its content exactly once",
            plain("**`germline_snv`**"), "germline_snv")

    nested = painted_prose("**the `germline_snv` protocol**")
    r.equal("and in a sentence the content is whole",
            strip(nested), "the germline_snv protocol")
    r.check("the nested span is coloured", display.SECONDARY in nested, nested)
    # The property that matters, stated as a property: whatever follows the
    # inner span is still bold. Read off the bytes rather than asserted about
    # one fixture -- the text after the code span must be preceded by a BOLD
    # with no RESET between it and the text.
    tail = nested.split("germline_snv")[1]
    r.check("and the emphasis after it is restored, not cancelled",
            tail.index(display.BOLD) < tail.index(" protocol"), repr(tail))

    # ---- degrade to literal, never delete -------------------------------- #
    for text, why in (
            ("a ** b ** c", "spaced asterisks are not emphasis"),
            ("2 * 3 * 4", "arithmetic is not emphasis"),
            ("`unclosed backtick", "a lone backtick stays a backtick"),
            ("50% of **", "a trailing marker is text"),
    ):
        r.equal(why, plain(text), text)

    r.equal("a marker never spans a newline", plain("**a\nb**"), "**a\nb**")

    # A valid **pair** around an unpaired backtick: the emphasis is real and is
    # consumed, the backtick is not and stays on screen. Content is what must
    # survive, not the markers that happened to be around it.
    r.equal("an unpaired backtick inside real emphasis stays literal",
            plain("**a `b**"), "a `b")
    r.check("and the emphasis around it still applies",
            display.BOLD in painted_prose("**a `b**"),
            painted_prose("**a `b**"))

    # ---- fenced blocks: byte-for-byte ------------------------------------ #
    fence = ("Here is one:\n\n```bash\n"
             "module load mugqic/genpipes/6.1.1 && genpipes rnaseq_light \\\n"
             "  -c $GENPIPES_INIS/rnaseq_light/rnaseq_light.base.ini \\\n"
             "  -r readset.rnaseq.txt -s 1-5 -g cmd.sh\n"
             "```\n\nThen `bash cmd.sh` submits it.")
    out = prose(fence)
    body = fence.split("```bash\n")[1].split("\n```")[0]
    r.check("a fenced command is passed through untouched", body in strip(out),
            out)
    r.check("including the fence lines themselves", "```bash" in strip(out), out)
    r.check("nothing inside a fence is styled",
            display.SECONDARY not in painted_prose(fence).split("```")[1],
            painted_prose(fence))
    r.contains("while prose outside it is still rendered", strip(out),
               "Then bash cmd.sh submits it")

    # ---- the two hard constraints, as properties ------------------------- #
    cases = ["**a**", "`b`", "- c", "**the `d` e**", "a ** b", fence,
             "x" * 300]
    r.equal("no line is ever split -- this patch adds no wrapping",
            [len(display._prose(t)) for t in cases],
            [len(t.splitlines()) for t in cases])
    r.equal("and the visible width is the text without its markers",
            display._vis_len(painted_prose("**the `d` e**")),
            len("the d e"))

    # ---- theme ----------------------------------------------------------- #
    was = display.THEME
    try:
        display.retheme({"NO_COLOR": "1"})
        bare = "\n".join(display._prose("**the `germline_snv` protocol**"))
        r.equal("under NO_COLOR the markers are stripped", bare,
                "the germline_snv protocol")
        r.check("and nothing escaped is emitted", "\033" not in bare, repr(bare))
    finally:
        display.retheme({"GENPIPE_THEME": was})

    # ---- and it is actually wired to <solution> -------------------------- #
    shown = drawn(display.render, Msg("<solution>**Bold** and `code`.</solution>"))
    r.contains("a real reply arrives rendered", shown, "Bold and code.")
    r.check("with no markers left on screen",
            "**" not in shown and "`" not in shown, shown)

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
