"""Interrupting a turn cleanly.

Ctrl-C has to mean "stop what you are doing" and nothing else: the conversation
survives, the run records survive, the prompt comes back, and no traceback lands
on a screen where nothing has actually gone wrong.

Getting that right is not about catching KeyboardInterrupt -- cli.py has always
done that -- it is about the OTHER thread. Biomni runs each <execute> block in a
daemon worker (utils.run_with_timeout) while the main thread waits on join(),
and an interrupt hits all three participants at once:

    the subprocess   ctrl-c reaches it through the process group, so it exits
                     with returncode -2
    the main thread  join() raises KeyboardInterrupt and starts unwinding
    the worker       is still inside subprocess.run and knows nothing about it

The worker then returns, sees a non-zero exit, and runs biomni's
traceback.print_stack() and print(result) -- by which time the main thread has
already restored stdout and headed for the prompt. That is the forty-line stack
and the CompletedProcess with an entire captured stdout in it that appears after
an interrupt: not a crash, an orphan writing into a terminal somebody else has
taken back.

The fix is a lifecycle one and it is small: keep the sink up while the worker
finishes handling its own interruption, and join it before giving the terminal
back. Nothing is killed here that the interrupt has not already reached, and
nothing keeps running quietly -- a worker that outlives the deadline is COUNTED
and reported, because a tool still executing after the prompt returns is a fact
the person needs, not one to hide behind muted output.

Stdlib only, and separate from agent.py, so all of this is testable in CI
without installing biomni -- the same boundary gate.py and modify.py keep.
"""
import contextlib
import io
import sys
import threading
import time


# How long an interrupted turn waits for biomni's worker to finish handling the
# interruption before giving the terminal back. The subprocess is already dead
# by then -- ctrl-c reaches it through the process group and it exits -2 -- so
# what is being waited for is the few milliseconds the worker spends returning
# from subprocess.run and printing about it. Generous, and bounded, because a
# prompt that does not come back is worse than a stray line of output.
REAP_SECONDS = 2.0


class Muted:
    """A stdout/stderr stand-in that drops writes from named threads.

    THE FALLBACK, not the fix. shielded()'s join is what normally keeps biomni's
    interruption noise off the screen; this covers the one case the join cannot
    -- a worker still running when the deadline expires, which is announced to
    the person rather than hidden (see shielded). Without it that worker's output
    would land in a terminal the prompt has already taken back, which is the
    original defect by a slower route.

    Only idents handed to mute() are dropped, and only shielded() hands them
    over.
    The main thread is never muted by anything here, so every genuine error,
    panel and transcript line is untouched.
    """

    def __init__(self, stream):
        self._stream = stream
        self._muted = set()

    def mute(self, ident):
        self._muted.add(ident)

    def write(self, text):
        if threading.get_ident() in self._muted:
            # Drop the write and forget any muted thread that has since died,
            # so the set cannot grow across a long session.
            self._muted &= {t.ident for t in threading.enumerate()}
            return len(text)
        return self._stream.write(text)

    def flush(self):
        if threading.get_ident() not in self._muted:
            self._stream.flush()

    def __getattr__(self, name):
        # isatty, fileno, encoding, and whatever else asks. Delegated rather
        # than reimplemented: this is a filter on write, not a stream.
        return getattr(self._stream, name)


def muted_streams():
    """Install the filter on stdout and stderr, once. Returns the stdout one.

    Idempotent, and it must be: configure() runs more than once (A1 calls it at
    construction and again from add_software), and wrapping a wrapper would
    leave two filters disagreeing about who is muted.
    """
    if not isinstance(sys.stdout, Muted):
        sys.stdout = Muted(sys.stdout)
    if not isinstance(sys.stderr, Muted):
        sys.stderr = Muted(sys.stderr)
    return sys.stdout


def shielded(call):
    """Wrap a callable so its stray printing never reaches the terminal, and so
    an interruption is reaped rather than abandoned.

    Biomni's run_bash_script prints a full traceback.print_stack() and the raw
    CompletedProcess object whenever a command exits non-zero (biomni/utils.py).
    A non-zero exit is completely ordinary here -- `genpipes rnaseq` without -c
    is how the agent discovers it needs one -- and everything worth seeing is
    already in the string the call returns, which comes back as an <observation>
    and gets drawn properly by display.render. The print is pure duplication.

    It is also destructive: it arrives from inside biomni's worker thread, on
    stderr, which the spinner does not proxy, so a forty-line stack lands in the
    middle of the status line and tears the display apart. That is what this
    fixes -- suppressed here rather than patched upstream because biomni is a
    dependency, and because a node's return value is the only channel this
    application reads.

    AND ON CTRL-C THE REDIRECT ALONE IS NOT ENOUGH, which is the whole reason
    this function has an except clause. Biomni runs each block in a daemon
    worker thread (utils.run_with_timeout) while the main thread waits on
    join(). An interrupt hits all three participants at once: the tty signals the process
    group, so the bash subprocess exits -2; join() raises KeyboardInterrupt in
    the MAIN thread; and the `with` below unwinds with it. The worker is still
    alive. It returns from subprocess.run, sees the non-zero exit, and runs
    biomni's traceback.print_stack() and print(result) -- into a stdout that no
    longer belongs to it. That is the stack dump and the CompletedProcess with
    a whole captured stdout in it that a person sees after interrupting: not a
    crash, an orphan writing into a terminal somebody else has taken back.

    So the sink STAYS UP while the worker finishes handling the interruption,
    and the noise lands in it exactly as it does on any other non-zero exit.
    The subprocess needs no killing of our own: it is in this process group and
    has already had the same ctrl-c the main thread got. Nothing is detached and
    nothing keeps running quietly -- a worker that outlives the deadline is
    reported to the caller, which prints it (see cli._turn), because a tool
    still executing after the prompt has come back is a fact the person needs
    rather than one to hide.
    """
    def quietly(*args, **kwargs):
        before = {t.ident for t in threading.enumerate()}
        # The installed filters, read BEFORE the redirect goes up. Inside it
        # sys.stdout is the sink, and muting there would wrap the sink and
        # leave that wrapper installed once the redirect came down -- the
        # filter has to be the one that owns the real terminal.
        guards = [s for s in (sys.stdout, sys.stderr) if isinstance(s, Muted)]
        sink = io.StringIO()
        try:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                return call(*args, **kwargs)
        except BaseException as stop:
            # BaseException, not Exception: KeyboardInterrupt is the whole
            # reason this clause exists, and it does not inherit from the other.
            #
            # The redirect goes back up for the join, so anything a worker
            # prints on its way out lands in the sink rather than on screen --
            # which is what makes this a lifecycle fix rather than a filter.
            alive = []
            deadline = time.monotonic() + REAP_SECONDS
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                for thread in threading.enumerate():
                    if (thread.ident in before
                            or thread is threading.main_thread()
                            or not thread.is_alive()):
                        continue
                    # ONE deadline for all of them, not one each. Two workers
                    # would otherwise hold the prompt for twice as long, and
                    # the budget is about how long a person waits after
                    # pressing ctrl-c, not about how many threads there are.
                    thread.join(max(0.0, deadline - time.monotonic()))
                    if thread.is_alive():
                        alive.append(thread)
                        for guard in guards:
                            guard.mute(thread.ident)
            if alive:
                # Told, not hidden. The caller decides how to say it; what
                # matters here is that a tool which is still running does not
                # get to be invisible just because its output is now muted.
                stop.genpipe_unreaped = len(alive)
            raise
    return quietly
