"""The interactive shell: the prompt box, live command completion, and the
activity indicator shown while a run is working.

Split from display.py deliberately. display.py renders what the *agent* says --
pure output, parseable, reusable by a web front end unchanged. This module is
the other half: it owns the terminal *input* path, which is inherently
POSIX-and-tty specific (raw mode, escape sequences, cursor arithmetic) and has
no meaning at all outside a real terminal. Keeping them apart means neither one
inherits the other's constraints.

Everything here degrades rather than breaks. If stdin isn't a terminal (a pipe,
a test harness, a CI run) the editor falls back to input() and the spinner
becomes a no-op, so the app still works headless -- it just stops being pretty.

Why hand-roll a line editor instead of using readline? Two things readline
can't do: draw a box that stays around the line while it's being edited, and
show the command list *as you type* rather than only after a Tab. Both are the
point of this interface, so the ~200 lines are the price of admission. What
readline gave for free -- history, word kill, Ctrl+A/E -- is reimplemented
below rather than lost.
"""

import io
import os
import select
import shutil
import sys
import termios
import threading
import time
import tty

import display

# Braille dots: ten frames, all the same visual weight, so the spinner reads as
# motion rather than as a character that keeps changing shape.
FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# Escape sequences we care about. Both the CSI ([) and SS3 (O) forms are here:
# terminals differ on which they send for arrows depending on keypad mode, and
# supporting only one makes arrow keys mysteriously dead on some hosts.
_ESCAPES = {
    "[A": "up", "[B": "down", "[C": "right", "[D": "left",
    "OA": "up", "OB": "down", "OC": "right", "OD": "left",
    "[H": "home", "[F": "end", "OH": "home", "OF": "end",
    "[1~": "home", "[4~": "end", "[7~": "home", "[8~": "end",
    "[3~": "delete", "[Z": "shift-tab",
}

def width():
    """Width of the prompt's rules, in columns, drawn at a one-column indent --
    deliberately the same arithmetic display.banner() uses for its box, so the
    rules line up with the banner above them instead of nearly lining up.

    The floor keeps the box from collapsing into nonsense on a tiny window; the
    ceiling keeps a maximised terminal from stretching one input line across two
    feet of screen.
    """
    try:
        cols = shutil.get_terminal_size((80, 24)).columns
    except Exception:
        cols = 80
    return max(44, min(cols - 2, 104))


# --------------------------------------------------------------------------
# Key input. Reads bytes with os.read rather than sys.stdin.read so that
# select() tells the truth: a TextIOWrapper can buffer an entire escape
# sequence internally, leaving select() convinced nothing is waiting while
# "[A" sits in Python's buffer. Going straight to the fd removes that whole
# class of bug, at the cost of decoding UTF-8 here by hand.
# --------------------------------------------------------------------------

class _Reader:
    def __init__(self, fd):
        self.fd = fd
        self.buf = b""

    def _need(self, n):
        while len(self.buf) < n:
            chunk = os.read(self.fd, 1024)
            if not chunk:
                raise EOFError
            self.buf += chunk

    def _char(self):
        """One decoded character, however many bytes that took."""
        self._need(1)
        lead = self.buf[0]
        size = 4 if lead >= 0xF0 else 3 if lead >= 0xE0 else 2 if lead >= 0xC0 else 1
        self._need(size)
        raw, self.buf = self.buf[:size], self.buf[size:]
        return raw.decode("utf-8", "replace")

    def key(self):
        """One keypress: either the character itself, or a name like "up".

        A paste is returned as ("paste", text) instead of a character, because a
        paste is one event even though it arrives as hundreds of bytes.
        """
        ch = self._char()
        if ch != "\x1b":
            return ch

        # ESC starts a sequence -- unless it doesn't. A bare Escape keypress
        # arrives alone, so if nothing follows within a beat, treat it as
        # Escape rather than blocking for a continuation that never comes.
        if not self.buf:
            ready, _, _ = select.select([self.fd], [], [], 0.04)
            if not ready:
                return "escape"

        nxt = self._char()
        if nxt not in ("[", "O"):
            return "escape"
        seq = nxt
        while len(seq) < 8:
            c = self._char()
            seq += c
            if c.isalpha() or c == "~":
                break
        if seq == "[200~":
            return ("paste", self._paste())
        return _ESCAPES.get(seq, "unknown")

    def _paste(self):
        """Everything up to the bracketed-paste end marker, as literal text.

        Bracketed paste exists because a terminal cannot otherwise distinguish
        typing from pasting, and the difference matters enormously here. Without
        it, pasting a two-line readset path submits the first line as a command
        the moment its newline arrives, and then feeds the remaining lines in as
        further commands -- in a tool that runs things on a cluster. People paste
        paths constantly, so this is not a nicety.

        Inside a paste, newlines are data. They are folded to spaces rather than
        kept, because the prompt is a single line: a pasted multi-line block
        becomes one long line the user can still see and edit, instead of a
        sequence of half-commands.
        """
        end = "\x1b[201~"
        text = ""
        while not text.endswith(end):
            text += self._char()
        text = text[: -len(end)]
        return " ".join(text.split("\n")).replace("\r", "")


# --------------------------------------------------------------------------
# The line editor.
#
# One invariant holds the cursor arithmetic together: on entry to every draw,
# the cursor sits at column 0 of the input line, and it is left there on exit.
# The top rule above it is printed once and never touched again; everything
# from the input line down is cleared and rewritten each keystroke. All cursor
# moves are relative, never absolute, so a redraw that scrolls the terminal
# still lands in the right place afterwards.
# --------------------------------------------------------------------------

MAX_MENU = 10
_MARK = "❯"


class _Editor:
    def __init__(self, commands, history, initial=""):
        self.commands = commands
        self.history = history
        self.text = initial
        self.cur = len(initial)   # caret position within self.text
        self.sel = 0          # highlighted row in the completion menu
        self.scroll = 0       # first visible character, for long lines
        self.hist_at = len(history)
        self.stash = ""       # line being typed, parked while browsing history
        self.span = width()
        self.rule = " " + display.GREY + display.DIM + "─" * self.span + display.RESET
        # "  ❯ " occupies four columns; leave one at the far end so a full line
        # never touches the rule's last character.
        self.room = self.span - 5

    # -- what to complete ---------------------------------------------------

    def matches(self):
        """Commands the current line could still become, or None when the line
        isn't a command word at all (plain prose, or a command whose arguments
        have started)."""
        if not self.text.startswith("/"):
            return None
        word = self.text[1:]
        if " " in word:
            return None
        low = word.lower()
        return [c for c in self.commands if c[0].startswith(low)]

    def menu_open(self):
        m = self.matches()
        return bool(m)

    def _arghint(self):
        """Once a command is typed and the arguments begin, the menu is done --
        but the signature is still worth having on screen, since that's exactly
        the moment you've forgotten the argument order."""
        if not self.text.startswith("/") or " " not in self.text:
            return None
        word = self.text[1:].split(" ", 1)[0].lower()
        return next((c for c in self.commands if c[0] == word), None)

    # -- drawing ------------------------------------------------------------

    def _input_line(self):
        if self.cur - self.scroll > self.room:
            self.scroll = self.cur - self.room
        if self.cur < self.scroll:
            self.scroll = self.cur
        seen = self.text[self.scroll:self.scroll + self.room]
        return f"  {display.BOLD}{display.GREEN}{_MARK}{display.RESET} {seen}"

    def _menu_lines(self):
        m = self.matches()
        out = []

        if m is not None and not m:
            out.append(f"    {display.DIM}no command starts with "
                       f"{self.text}{display.RESET}")
            return out

        if m:
            # One description column for the whole menu, sized from the widest
            # row -- computed per draw rather than fixed, because the menu
            # narrows as you type and a fixed column would leave a gutter.
            namew = max(len(c[0]) for c in m) + 1
            argw = max(len(c[1]) for c in m)
            used = 4 + 1 + namew + argw + (2 if argw else 0)
            for i, (name, args, desc) in enumerate(m[:MAX_MENU]):
                on = i == self.sel
                weight = display.BOLD if on else ""
                cell = (f"{weight}{display.GREEN}/{name}{display.RESET}"
                        f"{' ' * (namew - len(name))}")
                if argw:
                    cell += (f"{display.DIM}{args}{display.RESET}"
                             f"{' ' * (argw - len(args) + 2)}")
                room = self.span - used
                text = desc if len(desc) <= room else desc[:max(0, room - 1)] + "…"
                mark = f"{display.GREEN}{_MARK}{display.RESET}" if on else " "
                out.append(f"  {mark} {cell}{display.GREY}{text}{display.RESET}")
            if len(m) > MAX_MENU:
                out.append(f"    {display.DIM}+{len(m) - MAX_MENU} more"
                           f"{display.RESET}")
            return out

        hint = self._arghint()
        if hint:
            name, args, desc = hint
            out.append(f"    {display.GREY}/{name} {args}{display.RESET}  "
                       f"{display.DIM}{desc}{display.RESET}")
        elif not self.text:
            out.append(f"    {display.DIM}type a task, or{display.RESET}"
                       f" {display.GREEN}/{display.RESET}"
                       f"{display.DIM} for commands · tab completes · "
                       f"↑ for history{display.RESET}")
        return out

    def draw(self):
        below = self._menu_lines()
        parts = ["\r\033[J", self._input_line(), "\r\n", self.rule]
        for line in below:
            parts.append("\r\n" + line)
        parts.append(f"\033[{1 + len(below)}A\r")
        col = 4 + (self.cur - self.scroll)
        if col:
            parts.append(f"\033[{col}C")
        sys.stdout.write("".join(parts))
        sys.stdout.flush()

    def finish(self):
        """Close the box for good: redraw without the menu and leave the cursor
        below the closing rule, so the line the user typed stays in the
        scrollback framed exactly as they saw it."""
        sys.stdout.write("\r\033[J" + self._input_line() + "\r\n" + self.rule + "\r\n")
        sys.stdout.flush()

    def open(self):
        sys.stdout.write("\r\n" + self.rule + "\r\n")
        sys.stdout.flush()
        self.draw()

    def repaint(self):
        sys.stdout.write("\033[H\033[2J")
        self.open()

    # -- editing ------------------------------------------------------------

    def _changed(self):
        self.sel = 0
        self.draw()

    def set(self, text):
        self.text = text
        self.cur = len(text)
        self.scroll = 0
        self._changed()

    def insert(self, ch):
        self.text = self.text[:self.cur] + ch + self.text[self.cur:]
        self.cur += len(ch)
        self._changed()

    def backspace(self):
        if self.cur:
            self.text = self.text[:self.cur - 1] + self.text[self.cur:]
            self.cur -= 1
        self._changed()

    def delete(self):
        self.text = self.text[:self.cur] + self.text[self.cur + 1:]
        self._changed()

    def move(self, by):
        self.cur = max(0, min(len(self.text), self.cur + by))
        self.draw()

    def home(self):
        self.cur = 0
        self.draw()

    def end(self):
        self.cur = len(self.text)
        self.draw()

    def kill_left(self):
        self.text = self.text[self.cur:]
        self.cur = 0
        self._changed()

    def kill_right(self):
        self.text = self.text[:self.cur]
        self._changed()

    def kill_word(self):
        left = self.text[:self.cur].rstrip()
        cut = left.rfind(" ") + 1
        self.text = self.text[:cut] + self.text[self.cur:]
        self.cur = cut
        self._changed()

    def select(self, by):
        m = self.matches() or []
        shown = min(len(m), MAX_MENU)
        if shown:
            self.sel = (self.sel + by) % shown
        self.draw()

    def recall(self, by):
        """Walk the history. The half-typed line is parked on the way out and
        handed back on the way past the end, so browsing costs nothing."""
        if not self.history:
            return
        if self.hist_at == len(self.history):
            self.stash = self.text
        target = self.hist_at + by
        if target < 0 or target > len(self.history):
            return
        self.hist_at = target
        self.set(self.history[target] if target < len(self.history) else self.stash)

    def complete(self):
        """Tab: advance as far as the matches agree, then start choosing
        between them. Completing to the common prefix first means Tab never
        guesses when the answer is still genuinely ambiguous."""
        m = self.matches()
        if not m:
            return
        word = self.text[1:]
        shared = os.path.commonprefix([c[0] for c in m])
        if len(shared) > len(word):
            self.set("/" + shared)
            return
        name, args, _ = m[min(self.sel, len(m) - 1)]
        self.set("/" + name + (" " if args else ""))


class Prompt:
    """The app's input line. One instance, reused for every turn, so history
    accumulates across the session."""

    def __init__(self, commands=()):
        self.commands = list(commands)
        self.history = []

    def read(self, initial="", allow_empty=False):
        """One line from the user. Raises EOFError on Ctrl+D and
        KeyboardInterrupt on Ctrl+C at an empty line -- the same contract
        input() has, so callers don't need to care which one ran.

        `initial` pre-fills the line with an editable suggestion; Enter accepts
        it. allow_empty lets Enter on a blank line return "" instead of being
        ignored, which is what a follow-up question wants and what the main
        prompt does not.
        """
        if not sys.stdin.isatty():
            return input("genpipe> ")

        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        ed = _Editor(self.commands, self.history, initial=initial)
        try:
            tty.setraw(fd)
            # Ask the terminal to bracket pastes. Without this a pasted newline
            # is indistinguishable from Enter, so pasting two lines runs the
            # first as a command. Disabled again in the finally below: leaving a
            # terminal in this mode after the app exits is other programs'
            # problem, not theirs to discover.
            sys.stdout.write("\033[?2004h")
            sys.stdout.flush()
            ed.open()
            reader = _Reader(fd)
            while True:
                key = reader.key()

                if isinstance(key, tuple) and key[0] == "paste":
                    ed.insert(key[1])
                    continue
                if key in ("\r", "\n"):
                    if not ed.text.strip() and not allow_empty:
                        continue
                    ed.finish()
                    break
                if key == "\t":
                    ed.complete()
                elif key == "\x03":                     # Ctrl+C
                    if ed.text:
                        ed.set("")
                        continue
                    ed.finish()
                    raise KeyboardInterrupt
                elif key == "\x04":                     # Ctrl+D
                    if ed.text:
                        ed.delete()
                        continue
                    ed.finish()
                    raise EOFError
                elif key in ("\x7f", "\x08"):
                    ed.backspace()
                elif key == "delete":
                    ed.delete()
                elif key == "left":
                    ed.move(-1)
                elif key == "right":
                    ed.move(1)
                elif key in ("home", "\x01"):
                    ed.home()
                elif key in ("end", "\x05"):
                    ed.end()
                elif key == "\x15":                     # Ctrl+U
                    ed.kill_left()
                elif key == "\x0b":                     # Ctrl+K
                    ed.kill_right()
                elif key == "\x17":                     # Ctrl+W
                    ed.kill_word()
                elif key == "\x0c":                     # Ctrl+L
                    ed.repaint()
                elif key == "up":
                    ed.select(-1) if ed.menu_open() else ed.recall(-1)
                elif key == "down":
                    ed.select(1) if ed.menu_open() else ed.recall(1)
                elif key == "shift-tab":
                    if ed.menu_open():
                        ed.select(-1)
                elif len(key) == 1 and key.isprintable():
                    ed.insert(key)
                # anything else (escape, unrecognised sequence) is ignored
        finally:
            sys.stdout.write("\033[?2004l")
            sys.stdout.flush()
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)

        line = ed.text
        if line.strip() and (not self.history or self.history[-1] != line):
            self.history.append(line)
        print()
        return line


def ask(label, default=""):
    """A short follow-up question, with an editable suggested answer.

    `default` is pre-typed rather than offered as "[press enter for X]". The
    difference matters for the one question this is actually used for -- naming a
    run -- because a name you can edit in place invites a small correction, while
    a name you must retype in full to change invites accepting whatever was
    offered. The suggestion should be a starting point, not a default.

    Falls back to plain input() with a bracketed hint when there is no terminal
    to edit in.
    """
    prompt = (f"  {display.GREEN}▌{display.RESET} {label}  "
              f"{display.BOLD}{display.GREEN}{_MARK}{display.RESET} ")
    if not sys.stdin.isatty():
        hint = f" [{default}]" if default else ""
        answer = input(f"{label}{hint}: ").strip()
        return answer or default

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    text, cur = default, len(default)

    def paint():
        sys.stdout.write(f"\r{prompt}{text}\033[K")
        back = len(text) - cur
        if back:
            sys.stdout.write(f"\033[{back}D")
        sys.stdout.flush()

    try:
        tty.setraw(fd)
        paint()
        reader = _Reader(fd)
        while True:
            key = reader.key()
            if isinstance(key, tuple) and key[0] == "paste":
                text = text[:cur] + key[1] + text[cur:]
                cur += len(key[1])
            elif key in ("\r", "\n"):
                break
            elif key == "\x03":
                raise KeyboardInterrupt
            elif key == "\x04" and not text:
                raise EOFError
            elif key in ("\x7f", "\x08"):
                if cur:
                    text = text[:cur - 1] + text[cur:]
                    cur -= 1
            elif key == "delete":
                text = text[:cur] + text[cur + 1:]
            elif key == "left":
                cur = max(0, cur - 1)
            elif key == "right":
                cur = min(len(text), cur + 1)
            elif key in ("home", "\x01"):
                cur = 0
            elif key in ("end", "\x05"):
                cur = len(text)
            elif key == "\x15":                       # Ctrl+U -- clear it
                text, cur = "", 0
            elif len(key) == 1 and key.isprintable():
                text = text[:cur] + key + text[cur:]
                cur += 1
            paint()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    print("\r\n", end="")
    return text


# --------------------------------------------------------------------------
# The activity indicator.
#
# A run isn't one long silence -- it alternates between the model thinking and
# the transcript printing. So the spinner can't simply own the screen: it has
# to yield the line whenever something is printed and then reclaim it, which
# is why sys.stdout is proxied for the duration rather than the spinner just
# being told when to pause. The effect is a spinner pinned below the
# transcript, scrolling up as output arrives, with no interleaving.
# --------------------------------------------------------------------------

class _Proxy(io.TextIOBase):
    """Stands in for sys.stdout while an Activity is running."""

    def __init__(self, activity, real):
        self._activity = activity
        self._real = real

    def write(self, s):
        return self._activity.emit(s)

    def writable(self):
        return True

    def flush(self):
        try:
            self._real.flush()
        except Exception:
            pass

    def isatty(self):
        return self._real.isatty()

    def fileno(self):
        return self._real.fileno()

    @property
    def encoding(self):
        return getattr(self._real, "encoding", "utf-8")


class Activity:
    """Context manager: a spinner that lives at the bottom of the output while
    the block runs, and leaves nothing behind when it ends.

    The label is not fixed for the life of the block -- say() changes it while
    the spinner is running. That is what the difference between a tool that feels
    alive and one that feels hung comes down to: a GenPipes run takes minutes,
    and "thinking" held for all of them tells you nothing, while "running cmd.sh"
    tells you where it is.
    """

    INTERVAL = 0.09

    def __init__(self, label="working"):
        self.label = label
        self.on = sys.stdout.isatty()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._drawn = False
        self._at_bol = True   # is the cursor at the start of a line?
        self._frame = 0
        self._t0 = 0.0
        self._real = None
        self._thread = None

    def __enter__(self):
        if self.on:
            self._real = sys.stdout
            self._t0 = time.monotonic()
            sys.stdout = _Proxy(self, self._real)
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc):
        if self.on:
            self._stop.set()
            if self._thread:
                self._thread.join(timeout=1.0)
            with self._lock:
                self._erase()
                sys.stdout = self._real
                self._real.flush()
        return False

    def say(self, label):
        """Change what the spinner claims to be doing, mid-flight.

        Locked because the spinner thread reads self.label on every frame; an
        unsynchronised swap would occasionally draw half of the old label.
        """
        with self._lock:
            self.label = label

    def _erase(self):
        if self._drawn:
            self._real.write("\r\033[K")
            self._real.flush()
            self._drawn = False

    def _spin(self):
        while not self._stop.wait(self.INTERVAL):
            with self._lock:
                # Mid-line output means the cursor is parked somewhere we
                # can't safely overwrite; sit the frame out rather than
                # scribble over half a line.
                if not self._at_bol:
                    continue
                frame = FRAMES[self._frame % len(FRAMES)]
                self._frame += 1
                secs = int(time.monotonic() - self._t0)
                self._real.write(
                    f"\r  {display.GREEN}{frame}{display.RESET} "
                    f"{display.DIM}{self.label}…  ·  {secs}s"
                    f"  ·  ctrl-c to stop{display.RESET}\033[K")
                self._real.flush()
                self._drawn = True

    def emit(self, s):
        if not s:
            return 0
        with self._lock:
            self._erase()
            self._real.write(s)
            self._real.flush()
            self._at_bol = s.endswith("\n")
        return len(s)
