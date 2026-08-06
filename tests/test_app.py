#!/usr/bin/env python
"""The whole app, driven through a real terminal.

test_lifecycle drives the agent's API. This drives the PRODUCT: it launches
cli.py.main() inside a pty, types keystrokes at it, and asserts on the
screen a person would actually be looking at -- reconstructed with pyte, so what
is checked is the rendered result of every escape sequence, not the bytes.

That distinction is the point. Everything here is invisible to an API-level test:

  * the banner, and dev mode announcing itself
  * the completion menu appearing as you type, and Tab finishing a command
  * the suggested run name being pre-filled and editable
  * the gate's HOLD box, and /approve from the prompt
  * /list, /jobs, /diagnose and /where actually being wired to their handlers
  * a pasted multi-line string NOT executing its first line as a command

Runs against the fake cluster and the scripted model, so no allocation, no API
key, no cost. Needs biomni, pyte and a pty, so it is not part of CI.

Run:  python tests/test_app.py
"""
import fcntl
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time

from harness import Report

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROWS, COLS = 44, 100


class App:
    """A launched app on the other end of a pty, plus the screen it has drawn."""

    def __init__(self, workdir, state="failed-oom"):
        import pyte

        self.master, slave = pty.openpty()
        # Without a window size the app reads 80x24 defaults and every
        # width-dependent layout decision under test is the wrong one.
        fcntl.ioctl(slave, termios.TIOCSWINSZ,
                    struct.pack("HHHH", ROWS, COLS, 0, 0))

        env = dict(os.environ)
        env["COLUMNS"], env["LINES"] = str(COLS), str(ROWS)
        env["GENPIPE_AGENT_WORKDIR"] = workdir
        env["GENPIPE_FAKE_STATE"] = state
        # The checkout, and only the checkout. A module-loaded shell exports a
        # PYTHONPATH pointing at GenPipes' own 3.13 site-packages, which makes
        # any import here die of "SRE module mismatch"; replacing it rather than
        # dropping it is what lets `-m genpipe` resolve from cwd=workdir below.
        env["PYTHONPATH"] = ROOT
        # A scripted model needs no key, and leaving the real ones visible would
        # let a bug spend money. Removing them also proves --fake-llm does not
        # quietly fall back to asking for one.
        for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                    "GEMINI_API_KEY", "GROQ_API_KEY"):
            env.pop(var, None)
        env["GENPIPE_LLM_SOURCE"] = "Anthropic"
        env["GENPIPE_LLM_MODEL"] = "claude-sonnet-5"
        # Already introduced, so the app goes straight to the prompt. Without
        # this it would stop at "what should I call you?" and every send() below
        # would be answering that question instead.
        env["GENPIPE_USER"] = "tester"

        self.proc = subprocess.Popen(
            [sys.executable, "-m", "genpipe", "--fake", "--fake-llm"],
            stdin=slave, stdout=slave, stderr=slave,
            # Launched from the work directory, so job_output/ and cmd.sh land
            # there -- and, incidentally, so biomni's import-time load_dotenv()
            # cannot pick up the repo's real .env.
            cwd=workdir, env=env, close_fds=True)
        os.close(slave)

        self.screen = pyte.Screen(COLS, ROWS)
        self.stream = pyte.Stream(self.screen)
        # Two views, and the difference matters. `screen` is the 44-row viewport
        # -- what a person is looking at right now -- and is what layout
        # assertions must use. `scrollback` is everything ever emitted, which is
        # what "did this appear at all?" needs: a long /diagnose scrolls its own
        # evidence off the top before the next assertion runs, and checking the
        # viewport for it would fail for a reason that has nothing to do with the
        # product.
        self.scrollback = ""

    def pump(self, seconds=0.4):
        """Read whatever the app has emitted and feed it to the screen."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            r, _, _ = select.select([self.master], [], [], 0.05)
            if not r:
                continue
            try:
                data = os.read(self.master, 65536)
            except OSError:
                break
            if not data:
                break
            text = data.decode("utf-8", "replace")
            self.stream.feed(text)
            self.scrollback += text

    def wait_for(self, needle, timeout=90):
        """Pump until `needle` has been emitted. Returns whether it was.

        Everything here is timing-dependent by nature, so waiting on content is
        the only reliable synchronisation -- a fixed sleep either flakes or makes
        the suite slow, and usually both. Matched against the scrollback rather
        than the viewport, so a marker that has already scrolled past still
        counts as having appeared.
        """
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.pump(0.3)
            if needle in self.emitted():
                return True
        return False

    def emitted(self):
        """Everything the app has ever printed, ANSI stripped and whitespace
        collapsed. Use for "did this appear?"; use visible() for "how does it
        look?"."""
        plain = re.sub(r"\033\[[0-9;?]*[A-Za-z]", "", self.scrollback)
        plain = plain.replace("\r", "\n")
        return re.sub(r"[ \t]+", " ", plain)

    def send(self, text):
        os.write(self.master, text.encode())
        self.pump(0.35)

    def line(self, text):
        """Type a line and press Enter."""
        self.send(text)
        self.send("\r")

    def paste(self, text):
        """Send text the way a terminal delivers a paste: bracketed."""
        self.send("\x1b[200~" + text + "\x1b[201~")

    def text(self):
        return "\n".join(self.screen.display)

    def visible(self):
        """The screen with runs of whitespace collapsed, for robust matching."""
        return re.sub(r"[ \t]+", " ", self.text())

    def close(self):
        try:
            os.write(self.master, b"\x04")
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
        finally:
            try:
                os.close(self.master)
            except OSError:
                pass


def main():
    r = Report("the whole app, through a terminal")
    workdir = tempfile.mkdtemp(prefix="genpipe_app_")
    # The intake step asks about anything a request leaves out, so these exist
    # to be named in the request below. This suite is about the app as a whole;
    # the panel's own behaviour is covered by tests/test_intake.py and walked
    # end to end by testcases/case1_interface.py.
    for _seed in ("readset.rnaseq.txt", "design.rnaseq.txt"):
        with open(os.path.join(workdir, _seed), "w") as _fh:
            _fh.write("Sample\tReadset\n")
    app = None
    try:
        app = App(workdir, state="failed-oom")

        # ================================================================== #
        r.section("it launches, and says what it is")
        ok = app.wait_for("ready", timeout=120)
        r.check("reaches 'ready'", ok, app.text()[-600:] if not ok else "")
        screen = app.visible()
        r.contains("names the tool", screen, "GenPipes")
        r.contains("shows the version", screen, "v0")
        r.contains("greets the user", screen, "Welcome back")
        r.contains("shows the model", screen, "claude-sonnet-5")
        r.contains("shows where it lives", screen, "assistant")
        # The banner used to close with the approval promise. It says more where
        # it is demonstrated -- at the gate, holding a real submission -- than as
        # a claim made to someone who has not typed anything yet, so what is
        # asserted here is that the screen orients rather than promises.
        r.contains("says what to type first", screen, "Getting started")
        r.contains("and what is available afterwards", screen, "/check")
        r.contains("dev mode is announced, not hidden", screen, "dev mode")
        r.contains("...naming what is simulated", screen, "fake cluster")
        r.check("and it never asked for an API key",
                "API key" not in screen, screen[-300:])

        # ================================================================== #
        r.section("the completion menu appears as you type")
        app.send("/")
        screen = app.visible()
        # The menu is capped to what fits above the prompt, and the command
        # list has grown past a screenful, so assert that it opened and is
        # showing real rows with their descriptions -- not that every command
        # in the table is visible at once, which stopped being true and is not
        # the property this section is about.
        r.check("the menu opened", "/" in screen)
        rows = [c for c in ("/approve", "/modify", "/reject", "/list",
                            "/check", "/jobs", "/diagnose", "/cancel")
                if c in screen]
        r.check("showing several commands", len(rows) >= 3, f"saw={rows}")
        r.check("with descriptions beside them",
                any(d in screen for d in
                    ("let a held submission through", "diagnose a failed run",
                     "rewrites the command", "abandons a held run",
                     "how a whole run is doing")),
                screen[-400:])

        r.section("typing narrows it, Tab completes it")
        app.send("j")
        screen = app.visible()
        r.contains("still offers /jobs", screen, "/jobs")
        r.check("and no longer /approve",
                "/approve" not in screen.split("/jobs")[0][-400:], screen[-400:])
        app.send("\t")
        r.contains("Tab completed the command", app.visible(), "/jobs")

        # Clear the line rather than run it.
        app.send("\x15")

        # ================================================================== #
        r.section("a pasted multi-line string does not run its first line")
        app.paste("run rnaseq stringtie\non a second line")
        screen = app.visible()
        r.contains("the whole paste is on the input line", screen, "second line")
        r.check("and nothing was submitted by the newline in it",
                "name this run" not in screen, screen[-300:])
        app.send("\x15")

        # ================================================================== #
        r.section("a task is named at the gate, without being asked")
        # The project directory is named explicitly. Phase 1 (AGENT-FIXES.md
        # defect 1) stopped intake from treating the process's own launch
        # directory as an implicit project directory -- discovery only ever
        # looks where the request points, never at cwd on its own -- so the
        # seeded readset/design here have to be pointed at the same way a real
        # request would point at them.
        app.line(f"run rnaseq stringtie steps 1-5, everything is in {workdir}")
        # Two panels stand between the request and the name prompt now: the
        # readset and the design, neither of which this request names by
        # FILENAME. Answering them with the seeded files keeps the run name --
        # and therefore every assertion below -- the same as before intake
        # existed.
        # send(), not line(): with nine or fewer rows a digit selects
        # immediately, so a trailing Enter would leak into whatever prompt comes
        # next -- which is the name prompt, whose suggestion is pre-filled.
        app.wait_for("Which readset file", timeout=30)
        app.send("1")
        app.wait_for("needs a design file", timeout=30)
        app.send("1")
        r.check("reaches the gate", app.wait_for("HOLD", timeout=120),
                app.text()[-800:])
        # Named here and only here, from what the run turned out to BE. Nobody
        # is asked: a name is invented before anyone has seen what they are
        # naming, and the command is the better source anyway -- it says
        # `rnaseq -t stringtie` where the request said "the usual thing".
        r.contains("with a name derived from the command",
                   app.visible(), "rnaseq-stringtie")
        r.check("and it was never asked for",
                "name this run" not in app.emitted())
        screen = app.visible()
        r.contains("says approval is required", screen, "requires approval")
        r.contains("shows the command to be approved", screen, "bash cmd.sh")
        r.contains("shows the protocol it parsed", screen, "stringtie")
        r.contains("offers the way to approve", screen, "/approve")
        r.contains("and reassures nothing was submitted", screen,
                   "Nothing has reached the scheduler")

        r.section("/list shows the held run before it is approved")
        mark = len(app.emitted())
        app.line("/list")
        app.pump(0.8)
        screen = app.emitted()[mark:]
        r.contains("the run is listed", screen, "rnaseq-stringtie")
        r.contains("marked as held", screen, "held")
        r.contains("with what it is waiting for", screen, "awaiting your approval")

        # ================================================================== #
        r.section("/approve submits it")
        # The name was suggested, so read it back off the screen rather than
        # assuming the date suffix.
        m = re.search(r"(rnaseq-stringtie-\d{4})", app.text())
        name = m.group(1) if m else "rnaseq-stringtie"
        app.line(f"/approve {name}")
        r.check("confirms submission", app.wait_for("submitted", timeout=120),
                app.text()[-800:])
        r.contains("and points at what to do next", app.visible(), "/check")

        r.section("/check draws the run's progress")
        app.line(f"/check {name}")
        app.pump(1.5)
        screen = app.visible()
        r.contains("names the run", screen, name)
        # The wording changed with the source. /check used to report GenPipes'
        # log_report, which reads files on disk and therefore cannot see a job
        # that never started or one that was killed; it now reports sacct.
        r.contains("reports the failures loudly", screen.lower(), "failed")
        r.contains("counts the completed jobs", screen, "COMPLETED")
        r.contains("and the out-of-memory ones", screen, "OUT_OF_MEMORY")
        r.contains("names the step that actually broke", screen, "root cause")
        r.contains("and says where the answer came from", screen, "sacct")
        r.contains("with how much of the manifest it covered",
                   screen, "jobs resolved")

        r.section("/jobs shows the individual jobs, grouped by step")
        app.line(f"/jobs {name}")
        app.pump(1.5)
        screen = app.visible()
        r.contains("groups by step", screen, "picard_mark_duplicates")
        r.contains("shows a per-job state", screen, "out_of_memory")
        r.contains("and offers the diagnosis", screen, "/diagnose")

        r.section("/jobs <name> failed narrows to the failures")
        app.line(f"/jobs {name} failed")
        app.pump(1.5)
        screen = app.visible()
        r.contains("still shows the failing step", screen, "picard_mark_duplicates")
        r.check("and drops the healthy one",
                "trimmomatic" not in screen.split("picard_mark_duplicates")[-1],
                screen[-500:])

        # ================================================================== #
        r.section("/diagnose establishes the facts before explaining")
        before = len(app.scrollback)
        app.line(f"/diagnose {name}")
        r.check("reports what failed first",
                app.wait_for("step(s) affected", timeout=60), app.text()[-800:])
        # The evidence block's last line, printed by display.triage before the
        # model is asked anything. It is the seam: everything after it is the
        # explanation. Anchoring on it rather than on a speaker label is
        # deliberate -- the transcript no longer labels the agent's turns, and a
        # test that depended on a label would have been testing the furniture.
        SEAM = "reading the logs, then explaining"
        r.check("the evidence block closes", app.wait_for(SEAM, timeout=60),
                app.text()[-800:])
        # Then wait for the model to finish. There is no marker to wait on any
        # more -- the reply is plain prose with no label, which is the point --
        # so wait for the output to go quiet instead.
        settled = ""
        for _ in range(40):
            app.pump(3.0)
            if app.scrollback == settled:
                break
            settled = app.scrollback
        r.check("then explains it", len(app.scrollback) > before,
                app.text()[-800:])
        # Only what this command produced, so an earlier /jobs can't satisfy it.
        out = re.sub(r"\033\[[0-9;?]*[A-Za-z]", "", app.scrollback[before:])
        out = re.sub(r"[ \t]+", " ", out.replace("\r", "\n"))
        r.contains("names the failing step", out, "picard_mark_duplicates")
        r.contains("and the evidence it read", out, "peak memory")
        r.contains("naming the log it read", out, ".o")
        r.check("the evidence came before the explanation",
                out.index("peak memory") < out.index(SEAM),
                "the model's answer preceded the facts it was given")
        r.contains("with an actionable cause", out, "memory")

        r.section("/history keeps the finding")
        app.line("/history")
        app.pump(0.8)
        screen = app.visible()
        r.contains("the run is there", screen, name)
        r.contains("with the note from /diagnose", screen.lower(), "memory")

        # ================================================================== #
        r.section("/where shows the directories that decide where things land")
        app.line("/where")
        app.pump(0.6)
        screen = app.visible()
        r.contains("the launch directory", screen, "launched from")
        r.contains("the registry", screen, "runs.jsonl")
        r.contains("and the checkpoint db", screen, "genpipe_checkpoints")

        r.section("/help is grouped by what you are doing")
        app.line("/help")
        app.pump(0.6)
        screen = app.visible()
        for group in ("deciding", "watching", "fixing", "setup"):
            r.contains(f"group: {group}", screen, group)

        r.section("an unknown command explains itself")
        app.line("/nonsense")
        app.pump(0.5)
        r.contains("says so", app.visible(), "No such command")

        r.section("an unknown run does not look like a crash")
        app.line("/check nope")
        app.pump(0.8)
        screen = app.visible()
        r.contains("names the problem", screen, "No run named")
        r.contains("and offers a way forward", screen, "/list")

        # ================================================================== #
        r.section("a reused name is redirected, not allowed to clobber")
        # A fresh Preparation started for this run (the previous one reached
        # the gate, which resets it -- see cli._repl), so the project
        # directory has to be named again; it is never picked up from cwd.
        app.line(f"run rnaseq stringtie steps 1-5, everything is in {workdir}")
        # Two panels stand between the request and the name prompt now: the
        # readset and the design, neither of which this request names by
        # FILENAME. Answering them with the seeded files keeps the run name --
        # and therefore every assertion below -- the same as before intake
        # existed.
        # send(), not line(): with nine or fewer rows a digit selects
        # immediately, so a trailing Enter would leak into the next prompt.
        app.wait_for("Which readset file", timeout=30)
        app.send("1")
        app.wait_for("needs a design file", timeout=30)
        app.send("1")
        mark = len(app.emitted())
        reached = False
        for _ in range(60):
            app.pump(2.0)
            if "HOLD" in app.emitted()[mark:]:
                reached = True
                break
        r.check("the second run also reaches the gate", reached,
                app.text()[-800:])
        # A name has to identify exactly one run, because it is what /approve,
        # /check and /diagnose are given. The name is derived from the command, so a
        # repeat of the same request collides -- and the collision must be
        # advanced rather than allowed to shadow the first run's record.
        # Read the name off the second box rather than predicting the suffix:
        # what is being asserted is that the two differ, not what the second is
        # called, and the model's exact command is not this suite's business.
        #
        # Read from the mirror's `name` row, not from the /approve line. The
        # gate stopped spelling the run name beside its verbs once the prompt
        # learned to complete it -- see display.gate -- so the name now appears
        # exactly once on that screen, which is the row this matches.
        found = re.findall(r"name\s+(\S+)\s+not a flag", app.emitted()[mark:])
        second = found[-1] if found else None
        r.truthy("the second gate names a run", second)
        r.check("and it is not the first run's name", second != name,
                f"both runs are called {second!r}")

        r.section("Ctrl+D leaves cleanly")
        app.close()
        r.equal("exited without error", app.proc.returncode, 0)
        app = None

        # ================================================================== #
        r.section("the held run survives into a NEW process")
        # The startup screen no longer reports what is held. It used to, on the
        # grounds that a decision left at the gate existed only in the previous
        # session's scrollback -- but in practice the counts were of accumulated
        # testing rather than of anything anybody was about to act on, and they
        # occupied the space between the banner and the prompt.
        #
        # What is asserted now is the part that actually matters: the run itself
        # survived the process, and /list still finds it. The reminder was
        # convenience; the persistence is the guarantee.
        again = App(workdir, state="failed-oom")
        try:
            r.check("relaunches", again.wait_for("ready", timeout=120))
            r.check("and the opening screen stays quiet about what is held",
                    "waiting on you" not in again.emitted())
            again.line("/list")
            again.pump(1.0)
            listed = again.emitted()
            r.contains("but /list still has the held run", listed,
                       second or f"{name}-2")
            r.contains("and still says it is waiting", listed, "held")
        finally:
            again.close()

        return r.finish()
    finally:
        if app is not None:
            app.close()
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
