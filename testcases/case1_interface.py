#!/usr/bin/env python3
"""Case 1: drive the whole interface offline and check every documented action.

The action list here is the table in 01-interface.md, in the same order and with
the same numbers, so a failure reported as "action 13" can be looked up rather
than reverse-engineered. Keeping the numbering aligned is the only reason this
file is a script rather than another suite in tests/.

Everything is fake: a scripted model, a stubbed GenPipes and Slurm on PATH, a
scratch working directory outside the repository, and no API keys in the
environment at all.
"""

import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, ROOT)

from harness import Report                      # noqa: E402
from test_app import App                        # noqa: E402

from genpipe import display                                  # noqa: E402 -- for _GOODBYES


def run():
    r = Report("case 1 -- the interface, end to end")
    work = tempfile.mkdtemp(prefix="case1-")
    # Seed the working directory so the panels have real files to offer. An
    # empty directory would exercise the degenerate one-option path instead of
    # the choice panel this case is here to test.
    for name in ("readset.rnaseq.txt", "design.rnaseq.txt", "pairs.somatic.csv"):
        with open(os.path.join(work, name), "w") as fh:
            fh.write("Sample\tReadset\n")
    app = None
    try:
        # -- 1, 2: launch ------------------------------------------------
        r.section("launch")
        app = App(work, state="failed-oom")
        r.check("1  banner draws", app.wait_for("GenPipes"))
        r.check("1  dev mode is announced", app.wait_for("dev mode"))
        # A regression here would mean --fake-llm silently reaching for a real
        # provider, which is the one failure that could cost money offline.
        r.check("2  no API key prompt", "API key" not in app.emitted())

        # -- 3, 4: the resting state -------------------------------------
        r.section("the interface at rest")
        app.line("/help")
        app.pump(1.0)
        for group in ("deciding", "watching", "fixing", "setup"):
            r.check(f"3  /help groups {group}", group in app.emitted())

        mark = len(app.emitted())
        app.line("/list")
        _quiet(app)
        listed = app.emitted()[mark:].lower()
        r.check("4  /list is empty and calm",
                any(s in listed for s in ("no runs", "nothing", "no run")),
                listed[-400:])

        # -- 5, 6, 7: the panel appears only when something is missing ----
        r.section("the choice panel")
        before = len(app.emitted())
        app.line("run rnaseq on the test samples")
        _await(app, lambda new: "readset" in new.lower(), timeout=60)
        new = app.emitted()[before:]
        # rnaseq defaults to stringtie, so asking would be noise.
        r.check("5  no protocol panel for rnaseq", "Which rnaseq protocol" not in new)
        # ...but the readset was never named, and stringtie needs a design, so
        # two real gaps follow in that order.
        r.check("6  a readset panel appears", "readset" in new.lower())
        app.send("1")                      # readset.rnaseq.txt -- send(), not
        app.pump(2.0)                      # line(): a digit selects at once
        _await(app, lambda new: "design" in new.lower(), timeout=60)
        r.check("6  a design panel follows it", "design" in app.emitted().lower())
        app.send("1")                      # design.rnaseq.txt
        app.pump(1.5)
        r.check("7  the chosen file is carried forward",
                "readset.rnaseq.txt" in app.emitted())

        # -- 8..11: naming and the gate ----------------------------------
        r.section("naming and the gate")
        r.check("9  the gate draws", app.wait_for("HOLD", timeout=120))
        drawn = app.emitted()
        # Named at the gate, from what the run turned out to BE, and never
        # asked for: a name invented before anyone has seen what they are
        # naming is a name nobody recognises later.
        r.check("8  the run is named without being asked",
                "name this run" not in drawn)
        r.check("8  after the command exists, not before", "rnaseq" in drawn)
        # The generation command, shown under `bash cmd.sh`. It has to be here:
        # the transcript folds the agent's working away, so this is the only
        # place it appears before it is approved.
        r.check("10 the gate shows the command it was built from",
                "genpipes" in drawn)
        r.check("10 and the output directory", "output" in drawn)
        r.check("11 approve is offered", "/approve" in drawn)
        r.check("11 modify is offered", "/modify" in drawn)
        r.check("11 reject is offered", "/reject" in drawn)
        # Each verb states its consequence. This is the one point in the product
        # where consequences matter, and "approve" and "reject" are not
        # self-explanatory when one spends an allocation irreversibly and the
        # other quietly abandons a run.
        r.check("11 and each says what it does",
                "cannot be undone" in drawn and "nothing is submitted" in drawn)

        name = _name_from(drawn)
        r.check("a run name was parsed from the gate", bool(name), f"got={name!r}")
        if not name:
            return r.finish()

        # -- 12, 13: rejection shows the NEW command ---------------------
        #
        # Everything below slices app.emitted() from a mark taken before the
        # action. Asserting against the whole buffer would let a string printed
        # by an *earlier* action satisfy the check -- which is how the first
        # draft of this file "passed" action 12 against the first gate's own
        # HOLD banner.
        r.section("modification")
        # Read the gate's own `steps` row, not the generation command in the
        # transcript. The gate's command line is `bash cmd.sh` on every
        # submission -- identical before and after a rejection -- so what the
        # operator actually reads to tell the proposals apart is the slot table.
        first = _slot_from(drawn, "steps")
        mark = len(app.emitted())
        # /modify, not /reject. Rework used to be what /reject did; /reject is
        # terminal now and would abandon the run instead of regenerating it.
        app.line(f"/modify {name} use steps 1-4 instead")
        _await(app, lambda new: "HOLD" in new and _slot_from(new, "steps"),
               timeout=180)
        after = app.emitted()[mark:]
        second = _slot_from(after, "steps")
        r.check("12 the run goes back to the model, not to Slurm", "HOLD" in after)
        # The bug this guards: the matcher once searched the message list
        # forwards and redrew the *first* proposal, so an operator could approve
        # a command they had already rejected.
        r.check("13 the redrawn gate shows the new proposal, not the stale one",
                second and second != first, f"first={first!r} second={second!r}")
        r.check("13 and the feedback is what changed it", second == "1-4",
                f"asked for 1-4, gate shows {second!r}")

        # -- 14..17: approval, runs, jobs --------------------------------
        r.section("approval and monitoring")
        mark = len(app.emitted())
        app.line(f"/approve {name}")
        _quiet(app)
        r.check("14 submission ran", "job" in app.emitted()[mark:].lower())

        mark = len(app.emitted())
        app.line("/list")
        _quiet(app)
        r.check("15 the run is listed", name in app.emitted()[mark:],
                app.emitted()[mark:][-600:])

        mark = len(app.emitted())
        app.line(f"/jobs {name}")
        _quiet(app)
        jobs = app.emitted()[mark:].lower()
        r.check("16 jobs are listed with states",
                any(s in jobs for s in ("failed", "completed", "timeout",
                                        "out_of_memory")),
                jobs[-600:])
        # A GenPipes DAG cancels everything downstream of a failure, so folding
        # cancellations into the failure count buries the actual cause.
        r.check("17 cancellations are counted apart from failures",
                "cancelled" in jobs.lower(), jobs[-500:])

        # -- 18, 19 ------------------------------------------------------
        r.section("diagnosis and orientation")
        app.line(f"/diagnose {name}")
        app.pump(10.0)
        r.check("18 a diagnosis is produced", "step" in app.emitted().lower())

        app.line("/where")
        app.pump(1.5)
        r.check("19 /where prints real paths", work in app.emitted())

        # -- 20, 21, 22: the closed-option panel -------------------------
        r.section("a pipeline that really does need asking")
        # A fresh conversation first. Everything above named a readset, a design
        # and a pipeline, and an agent reading its own history is right not to
        # ask again for what it was already told -- so asking dnaseq's protocol
        # question on that thread would be asking it to forget. /new is what a
        # person does here, and it is the thing being relied on.
        app.line("/new")
        _quiet(app)
        before = len(app.emitted())
        app.line("run dnaseq")
        _await(app, lambda new: "protocol" in new.lower(), timeout=60)
        panel = app.emitted()[before:]
        r.check("20 the dnaseq protocol panel appears",
                "protocol" in panel.lower())
        present = [p for p in ("germline_snv", "germline_sv", "germline_high_cov",
                               "somatic_tumor_only", "somatic_fastpass",
                               "somatic_ensemble", "somatic_sv") if p in panel]
        r.check("20 all seven protocols are offered", len(present) == 7,
                f"missing={set(('germline_snv','germline_sv','germline_high_cov','somatic_tumor_only','somatic_fastpass','somatic_ensemble','somatic_sv')) - set(present)}")
        mark = len(app.emitted())
        app.send("6")                       # somatic_ensemble
        _await(app, lambda new: "readset" in new.lower(), timeout=60)
        # The readset is asked for before the pairs file: gaps are answered in
        # the order the command needs them, and a pairs file is only known to be
        # required once the protocol is settled.
        r.check("21 the readset is asked first",
                "readset" in app.emitted()[mark:].lower())
        app.send("1")
        _await(app, lambda new: "pairs" in new.lower(), timeout=60)
        r.check("21 then a pairs panel, because somatic_ensemble needs one",
                "pairs" in app.emitted()[mark:].lower(),
                app.emitted()[mark:][-400:])
        app.send("\x1b")                    # escape
        app.pump(1.5)
        r.check("22 escape closes the panel rather than trapping the user",
                app.proc.poll() is None)
        app.send("\x03")
        app.pump(1.0)

        # -- 23: name collision ------------------------------------------
        r.section("a name collision cannot destroy a run")
        mark = len(app.emitted())
        # Name every slot in the request, so intake has nothing to ask and the
        # next prompt really is the name prompt.
        app.line("run rnaseq stringtie with readset.rnaseq.txt "
                 "and design.rnaseq.txt")
        _await(app, lambda new: "HOLD" in new, timeout=120)
        # The same request derives the same name, so the second run collides
        # with the first. It must be advanced rather than allowed to shadow it:
        # a name has to identify exactly one run, because it is what /approve,
        # /check and /diagnose are given.
        second = _name_from(app.emitted()[mark:])
        r.check("23 the reused name is redirected", bool(second) and second != name,
                f"first={name!r} second={second!r}")
        app.send("\x03")
        app.pump(1.0)

        # -- 24, 25 ------------------------------------------------------
        r.section("input handling and exit")
        app.paste("/runs\nthis second line must not run on its own")
        app.pump(1.5)
        r.check("24 a pasted newline does not self-submit",
                app.proc.poll() is None)
        app.send("\x03")                    # abandon the pasted line outright
        _quiet(app)

        app.line("/exit")
        app.pump(1.0)
        # One of several goodbyes, sampled at random -- so the assertion is that
        # the app said one of them, not that it said any particular one.
        emitted = app.emitted()
        r.check("25 the farewell is printed",
                any(line in emitted for line in display._GOODBYES))
        # wait(), not poll(). The app exits via os._exit while the pty master is
        # still open, and poll() kept returning None for a process whose
        # /proc/<pid>/cmdline was already empty -- i.e. reporting a dead process
        # as running. wait() reaps it properly.
        exited = True
        try:
            code = app.proc.wait(timeout=20)
        except Exception:
            exited, code = False, None
        r.check("25 exits cleanly", exited, "still running after 20s")
        r.equal("25 with status 0", code, 0)
        r.check("25 and no traceback", "Traceback" not in app.emitted())
    finally:
        if app:
            app.close()
        shutil.rmtree(work, ignore_errors=True)
    return r.finish()


def _await(app, extract, timeout=120, settle=1.0):
    """Pump until `extract` finds something new, then return it.

    Takes its own mark and only ever shows `extract` the output produced after
    it, so a value printed by an earlier action can never satisfy the wait.
    """
    mark = len(app.emitted())
    deadline = time.time() + timeout
    found = None
    while time.time() < deadline:
        app.pump(settle)
        found = extract(app.emitted()[mark:])
        if found:
            # One more pump: the gate prints its command before its footer, and
            # returning at first sight would race the rest of the box.
            app.pump(settle)
            return extract(app.emitted()[mark:]) or found
    return found


def _name_from(text):
    """The run name, read back off the gate's own approve line.

    Last match, and placeholders rejected. Several screens print the literal
    string "/approve <name>" as instruction -- the help text and the pending
    banner among them -- and taking the first match picks up "<name>" as though
    it were a real run, after which every later action addresses a run that does
    not exist and fails for the wrong reason.
    """
    found = None
    for line in text.splitlines():
        if "/approve" in line:
            parts = _plain(line.split("/approve", 1)[1]).split()
            if parts and not parts[0].startswith("<"):
                found = parts[0]
    return found


def _slot_from(text, label):
    """The value of one row in the gate's slot table, last occurrence."""
    found = None
    for line in text.splitlines():
        plain = _plain(line).strip()
        if plain.startswith(label + " "):
            value = plain[len(label):].strip()
            if value:
                found = value
    return found


def _quiet(app, settle=1.0, limit=30):
    """Wait until the app stops emitting, so the next command is not typed into
    a busy prompt. Fixed pumps race a model that took a second longer than
    usual, and the resulting failure looks like a missing feature."""
    last = -1
    for _ in range(limit):
        app.pump(settle)
        now = len(app.emitted())
        if now == last:
            return True
        last = now
    return False


def _command_from(text):
    """The last generation command the gate displayed.

    Keyed on `-g`, not on the word "genpipes": the gate's own footer and the
    activity log both mention genpipes in passing, and only the generation
    command carries the output-script flag.
    """
    found = None
    for line in text.splitlines():
        stripped = _plain(line).strip().lstrip("▌ ").strip()
        if "genpipes " in stripped and "-g " in stripped:
            found = stripped
    return found


def _plain(text):
    """Strip the escape sequences pyte leaves in reconstructed lines."""
    out, i = [], 0
    while i < len(text):
        if text[i] == "\033":
            while i < len(text) and text[i] not in "mK":
                i += 1
            i += 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


if __name__ == "__main__":
    sys.exit(run())
