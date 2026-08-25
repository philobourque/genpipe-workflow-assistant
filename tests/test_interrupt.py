"""Ctrl-C: what an interruption does, and what it is allowed to claim.

THE BUG THIS SUITE IS ABOUT. Interrupting a turn used to look like a crash:

    ▌ Stopped.
    ▌ Nothing reached the scheduler...
    File ".../threading.py"...
    File ".../biomni/utils.py"...
    traceback.print_stack()
    CompletedProcess(... returncode=-2, <the whole captured stdout> ...)

Three separate defects in one screen. The stack and the CompletedProcess are
biomni's worker thread printing into a terminal the main thread had already
taken back (genpipe/interrupt.py explains the race). "Stopped." reads as a
failure when nothing failed. And "Nothing reached the scheduler" was printed
unconditionally by a handler that had checked nothing -- which is fine on the
turn path and wrong during /approve, where an interrupt can land after the
submission command has started.

Everything here is stdlib-only on purpose: interrupt.py and runs.interrupt_claim
were kept free of biomni precisely so the lifecycle can be checked in CI in
milliseconds rather than only by pressing ctrl-c on a login node.
"""
import io
import os
import sys
import threading
import time

from harness import Report

from genpipe import interrupt
from genpipe import runs


# What biomni prints from inside its worker when a command exits non-zero. Not
# a paraphrase: run_bash_script does traceback.print_stack() and then
# print(result) on the CompletedProcess, and on ctrl-c the exit code is -2
# because the tty signals the whole process group.
BIOMNI_NOISE = (
    'File "/usr/lib/python3.12/threading.py", line 1075, in _bootstrap_inner\n'
    "    self.run()\n"
    "CompletedProcess(args=['/tmp/x.sh'], returncode=-2, "
    "stdout='" + "y" * 4000 + "', stderr='')\n"
)


def _worker_that_prints(started, hold=None, delay=0.0):
    """A stand-in for biomni's worker: it prints the way run_bash_script does
    once its subprocess has been killed."""
    def body():
        started.set()
        if hold is not None:
            hold.wait(5.0)
        if delay:
            time.sleep(delay)
        sys.stdout.write(BIOMNI_NOISE)
        sys.stderr.write(BIOMNI_NOISE)
    return body


def main():
    r = Report("Ctrl-C")

    # ------------------------------------------------------------------
    r.section("the ordinary case: a node that prints and returns")

    def chatty(state):
        print("biomni debug chatter")
        sys.stderr.write("parsing error...\n")
        return {"ok": state}

    screen = io.StringIO()
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = screen
    try:
        got = interrupt.shielded(chatty)("payload")
    finally:
        sys.stdout, sys.stderr = real_out, real_err
    r.equal("the return value is untouched", got, {"ok": "payload"})
    r.equal("and none of its printing reached the screen", screen.getvalue(), "")

    # ------------------------------------------------------------------
    r.section("ctrl-c while a worker is running")
    # The shape of the real thing: the node starts a thread and is then
    # interrupted while waiting for it, exactly as run_with_timeout's join()
    # is. The worker goes on to print AFTER the interrupt has been raised.
    started = threading.Event()

    def interrupted_node(_state):
        t = threading.Thread(target=_worker_that_prints(started, delay=0.05),
                             daemon=True)
        t.start()
        started.wait(2.0)
        raise KeyboardInterrupt

    screen = io.StringIO()
    sys.stdout = sys.stderr = screen
    try:
        try:
            interrupt.shielded(interrupted_node)(None)
            raised = None
        except BaseException as e:            # noqa: BLE001
            raised = e
        # Give the worker every chance to leak after the call has returned.
        time.sleep(0.3)
        after = screen.getvalue()
    finally:
        sys.stdout, sys.stderr = real_out, real_err

    r.check("the interrupt still reaches the caller",
            isinstance(raised, KeyboardInterrupt), type(raised).__name__)
    r.check("no traceback frame leaked to the terminal",
            "threading.py" not in after, after[:200])
    r.check("no CompletedProcess leaked to the terminal",
            "CompletedProcess" not in after, after[:200])
    r.check("and none of the captured stdout came with it",
            "yyyy" not in after, after[:200])
    r.equal("nothing at all was printed, in fact", after, "")
    r.equal("the worker was reaped, so nothing is reported as left running",
            getattr(raised, "genpipe_unreaped", 0), 0)

    # ------------------------------------------------------------------
    r.section("a worker that will not stop is reported, not hidden")
    # The one case the join cannot cover. It must not be silently detached:
    # a tool still executing after the prompt returns is a fact the person
    # needs, so the count rides out on the exception for the caller to print.
    hold = threading.Event()
    started = threading.Event()
    lingering = []

    def stuck_node(_state):
        t = threading.Thread(target=_worker_that_prints(started, hold=hold),
                             daemon=True)
        t.start()
        lingering.append(t)
        started.wait(2.0)
        raise KeyboardInterrupt

    interrupt.REAP_SECONDS, budget = 0.2, interrupt.REAP_SECONDS
    screen = io.StringIO()
    sys.stdout = sys.stderr = interrupt.Muted(screen)
    guard_out = sys.stdout
    try:
        began = time.monotonic()
        try:
            interrupt.shielded(stuck_node)(None)
            raised = None
        except BaseException as e:            # noqa: BLE001
            raised = e
        waited = time.monotonic() - began
        # Now let it finish. Its output must be dropped, because the terminal
        # has already gone back to the prompt.
        hold.set()
        lingering[0].join(2.0)
        time.sleep(0.1)
        after = screen.getvalue()
    finally:
        sys.stdout, sys.stderr = real_out, real_err
        interrupt.REAP_SECONDS = budget

    r.equal("the straggler is counted for the caller to mention",
            getattr(raised, "genpipe_unreaped", 0), 1)
    r.check("the prompt is not held for longer than the budget", waited < 1.5,
            f"waited={waited:.2f}s")
    r.check("and its late output is dropped rather than printed",
            "CompletedProcess" not in after, after[:200])
    r.check("the muting is scoped to that thread, not to the stream",
            guard_out.write("a real line from the main thread\n") and
            "a real line from the main thread" in screen.getvalue())

    # ------------------------------------------------------------------
    r.section("repeated ctrl-c")
    # Pressing it again while the first one is being handled must not escape as
    # a second traceback, and must not leave the streams redirected.
    def double_node(_state):
        raise KeyboardInterrupt

    screen = io.StringIO()
    sys.stdout = sys.stderr = screen
    try:
        for _ in range(5):
            try:
                interrupt.shielded(double_node)(None)
            except KeyboardInterrupt:
                pass
        after = screen.getvalue()
    finally:
        sys.stdout, sys.stderr = real_out, real_err
    r.equal("five interrupts in a row print nothing", after, "")
    r.check("and the streams are back where they belong",
            sys.stdout is real_out and sys.stderr is real_err)

    # ------------------------------------------------------------------
    r.section("a real exception is still diagnosable")
    # The one thing this must never become is a blanket suppressor. A genuine
    # failure has to come out whole.
    def broken(_state):
        raise ValueError("something genuinely went wrong")

    try:
        interrupt.shielded(broken)(None)
        blew_up = None
    except Exception as e:                    # noqa: BLE001
        blew_up = e
    r.check("the exception propagates unchanged",
            isinstance(blew_up, ValueError), type(blew_up).__name__)
    r.equal("with its message intact", str(blew_up),
            "something genuinely went wrong")

    # ------------------------------------------------------------------
    r.section("what may be claimed about the scheduler")
    r.equal("nothing pending: the reassurance is earned",
            runs.interrupt_claim([]),
            "Nothing reached the scheduler — the conversation is still here.")
    r.equal("a held run has not been submitted either",
            runs.interrupt_claim([("poulet", runs.HELD, "waiting")]),
            "Nothing reached the scheduler — the conversation is still here.")

    # THE ONE THAT MATTERS. begin_submission() writes `submitting` BEFORE the
    # command runs, so a ctrl-c during /approve finds exactly this -- and the
    # comforting sentence would be a lie told at the only moment it costs
    # something.
    mid = runs.interrupt_claim(
        [("poulet", runs.SUBMITTING, "the job list was being written")])
    r.check("a submission in flight is not called nothing",
            "Nothing reached the scheduler" not in mid, mid)
    r.contains("it names the run", mid, "poulet")
    r.contains("and says how to find out", mid, "/check poulet")
    for status in (runs.SUBMITTED, runs.SUBMIT_UNKNOWN, runs.SUBMIT_FAILED):
        claim = runs.interrupt_claim([("x", status, "")])
        r.check(f"{status} makes no claim that nothing ran",
                "Nothing reached the scheduler" not in claim, claim)
    many = runs.interrupt_claim([("a", runs.SUBMITTED, ""),
                                 ("b", runs.SUBMITTING, "")])
    r.contains("several at once are counted rather than listed", many, "2 runs")

    _read_only_claim(r)
    return r.finish()



def _read_only_claim(r):
    """Ctrl-c during a command that cannot submit says so.

    THE FALSE ALARM. _scheduler_claim answers "could this interruption have
    left something on Slurm" from the STATUS of the runs it is handed. That is
    right for a conversational turn and for /approve. It is wrong for
    /diagnose: interrupting a diagnosis of a run submitted three weeks ago
    printed "'<name>' had already been approved -- already submitted. /check
    before assuming either way", which is true of the RUN and a false alarm
    about the INTERRUPTION -- it reads as though the diagnosis might have put
    work on the cluster.
    """
    from genpipe import capabilities

    # cli.py is not importable here -- this suite runs in the offline CI job,
    # which installs no biomni -- so the command list is read as SOURCE. What
    # is being asserted is a table, and a table can be checked without
    # importing the module that acts on it. test_surface, which does have the
    # stack, exercises the dispatcher itself.
    r.section("interrupting a read-only command claims nothing about Slurm")
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(here, "genpipe", "cli.py")).read()
    block = src[src.index("_READ_ONLY_COMMANDS = frozenset(("):]
    block = block[:block.index("))")]
    for verb in ("diagnose", "check", "jobs", "list", "view", "history"):
        r.check(f"/{verb} is known to be read-only", f'"{verb}"' in block)
    for verb in ("approve", "modify", "reject", "cancel", "hold", "track"):
        r.check(f"/{verb} is NOT, so it still asks the registry",
                f'"{verb}"' not in block)
    r.contains("and the claim it makes says why", src,
               "that command only reads")

    r.section("and nothing a diagnosis may call can reach the scheduler")
    # The claim above is only honest because of this: every capability the
    # model is allowed to invoke is READS. LOCAL and SCHEDULER are declared so
    # the shape is settled, and deliberately not enabled.
    r.equal("only read-only capabilities are enabled",
            capabilities.ENABLED, (capabilities.READS,))
    for name, cap in sorted(capabilities.TABLE.items()):
        r.equal(f"{name} only reads", cap.kind, capabilities.READS)
    r.check("a submitting capability does not exist to be called",
            not any(c.kind == capabilities.SCHEDULER
                    for c in capabilities.TABLE.values()))


if __name__ == "__main__":
    raise SystemExit(main())
