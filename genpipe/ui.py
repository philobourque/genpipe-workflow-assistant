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
import glob
import os
import select
import shutil
import sys
import termios
import threading
import time
import tty

from . import display

# Braille dots: ten frames, all the same visual weight, so the spinner reads as
# motion rather than as a character that keeps changing shape.
#
# Two attempts at making it bigger were both worse and are recorded so they are
# not tried a third time. The dense braille set (⣾⣽⣻) fills a 2-wide-by-4-tall dot
# matrix, so it reads as a vertical bar rather than a bigger spinner. Quadrant
# blocks (▛▜▟▙) are square but far too heavy -- a solid block pulsing next to a
# line of dim text is the loudest thing on the screen, which is exactly backwards
# for a progress indicator. Ten small dots at speed is the right amount of motion.
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


class _Line:
    """An editable line of text and a caret. No terminal, no drawing, no keys.

    Everything that knows how a line of text responds to backspace, Ctrl+W, or
    an arrow key -- and nothing else. It was written three times before this
    existed: once in _Editor for the main prompt, once inside ask() for a
    follow-up question, and a third was about to be written for the field that
    sits inside a /modify panel row. Three copies of "delete the word to the
    left of the caret" is three chances for one of them to be subtly wrong, and
    the one people would notice is always the one they use least.

    The split is drawing, not keys. Each of the three paints somewhere
    different -- a framed box with a completion menu, a single line after a
    prompt, an indented field between two other rows -- and there is nothing
    shared in that. What IS shared is what the text becomes, which is here, and
    which can be tested by calling methods instead of by driving a pty.

    Methods return True when the text changed and False when only the caret
    moved. Callers that maintain a completion menu need to tell those apart --
    a highlighted row survives an arrow key and must reset on a keystroke.
    """

    __slots__ = ("text", "cur")

    def __init__(self, text=""):
        self.text = text
        self.cur = len(text)

    def set(self, text):
        self.text = text
        self.cur = len(text)
        return True

    def insert(self, chunk):
        self.text = self.text[:self.cur] + chunk + self.text[self.cur:]
        self.cur += len(chunk)
        return True

    def backspace(self):
        if not self.cur:
            return False
        self.text = self.text[:self.cur - 1] + self.text[self.cur:]
        self.cur -= 1
        return True

    def delete(self):
        if self.cur >= len(self.text):
            return False
        self.text = self.text[:self.cur] + self.text[self.cur + 1:]
        return True

    def move(self, by):
        self.cur = max(0, min(len(self.text), self.cur + by))
        return False

    def home(self):
        self.cur = 0
        return False

    def end(self):
        self.cur = len(self.text)
        return False

    def kill_left(self):
        self.text = self.text[self.cur:]
        self.cur = 0
        return True

    def kill_right(self):
        self.text = self.text[:self.cur]
        return True

    def kill_word(self):
        left = self.text[:self.cur].rstrip()
        cut = left.rfind(" ") + 1
        self.text = self.text[:cut] + self.text[self.cur:]
        self.cur = cut
        return True

    def key(self, key):
        """Apply one key. Returns None if this class does not handle it.

        The dispatch lives here so the three callers cannot disagree about
        which control code means what -- Ctrl+U clearing to the left in one
        editor and the whole line in another is the kind of difference nobody
        reports and everybody works around. Keys that are not editing --
        Enter, Tab, Escape, Ctrl+C -- are deliberately NOT handled: they mean
        different things in a prompt, a question and a panel field, and that is
        each caller's business.
        """
        if isinstance(key, tuple) and key and key[0] == "paste":
            return self.insert(key[1])
        if key in ("\x7f", "\x08"):
            return self.backspace()
        if key == "delete":
            return self.delete()
        if key == "left":
            return self.move(-1)
        if key == "right":
            return self.move(1)
        if key in ("home", "\x01"):
            return self.home()
        if key in ("end", "\x05"):
            return self.end()
        if key == "\x15":                     # Ctrl+U
            return self.kill_left()
        if key == "\x0b":                     # Ctrl+K
            return self.kill_right()
        if key == "\x17":                     # Ctrl+W
            return self.kill_word()
        if isinstance(key, str) and len(key) == 1 and key.isprintable():
            return self.insert(key)
        return None


class _Editor:
    def __init__(self, commands, history, initial="", arguments=None):
        self.commands = commands
        # Called with a command name, returns [(value, hint), ...] for its first
        # argument. Supplied by the app rather than known here: the values are run
        # names out of the registry, and this module has no business reading it.
        self.arguments = arguments or (lambda name: [])
        self.history = history
        # The text and the caret live in a _Line, so this class is left with the
        # part that is actually its own: the completion menu, the history, and
        # the framed box. `text` and `cur` stay readable as attributes because
        # everything from matches() to _input_line() reads them.
        self.line = _Line(initial)
        self.sel = 0          # highlighted row in the completion menu
        self.scroll = 0       # first visible character, for long lines
        self.hist_at = len(history)
        self.stash = ""       # line being typed, parked while browsing history
        self.span = width()
        self.rule = " " + display.GREY + display.DIM + "─" * self.span + display.RESET
        # "  ❯ " occupies four columns; leave one at the far end so a full line
        # never touches the rule's last character.
        self.room = self.span - 5

    @property
    def text(self):
        return self.line.text

    @property
    def cur(self):
        return self.line.cur

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

    def arg_matches(self):
        """Values the first argument could still become, or None when we are not
        typing one.

        Only the FIRST argument, and only while it is the last word on the line.
        `/reject patient-42 use steps 6-12` is prose after that point, and a menu
        of run names trying to complete "steps" would be noise.

        The empty list is meaningful and distinct from None: it means this command
        does take a name, and there is not one to offer -- which is worth saying,
        because "no run is waiting for approval" is the actual answer to what the
        person is trying to do.
        """
        if not self.text.startswith("/") or " " not in self.text:
            return None
        name, _, rest = self.text[1:].partition(" ")
        if " " in rest:
            return None
        values = self.arguments(name.lower())
        if values is None:
            return None
        low = rest.strip().lower()
        return [v for v in values if v[0].lower().startswith(low)]

    def menu_open(self):
        return bool(self.matches()) or bool(self.arg_matches())

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

        values = self.arg_matches()
        if values:
            namew = max(len(v[0]) for v in values) + 2
            for i, (value, note) in enumerate(values[:MAX_MENU]):
                on = i == self.sel
                mark = f"{display.GREEN}{_MARK}{display.RESET}" if on else " "
                weight = display.BOLD if on else ""
                room = self.span - 6 - namew
                note = note if len(note) <= room else note[:max(0, room - 1)] + "…"
                out.append(f"  {mark} {weight}{display.WHITE}{value}{display.RESET}"
                           f"{' ' * (namew - len(value))}"
                           f"{display.GREY}{note}{display.RESET}")
            if len(values) > MAX_MENU:
                out.append(f"    {display.DIM}+{len(values) - MAX_MENU} more"
                           f"{display.RESET}")
            return out

        hint = self._arghint()
        if values is not None and not values:
            # The command takes a run name and there is no run to name.
            out.append(f"    {display.DIM}no run matches{display.RESET}")
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
        """Take the box down and leave nothing behind.

        It used to close by redrawing itself without the menu, so the line stayed
        in the scrollback framed exactly as it had been typed. That was the wrong
        call once the transcript started drawing the same line: the message
        appeared TWICE, once in the box and once below it, and a reader cannot
        tell a transcript that repeats itself from a person who said something
        twice.

        The box is the editor, and an editor is furniture -- it exists while you
        are typing and has no business in the record afterwards. The line itself
        is drawn once, by display.echo, as `❯ what you said`.
        """
        sys.stdout.write("\r\033[J")
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

    def _after(self, changed):
        """Repaint, and reset the menu highlight only if the TEXT moved.

        The distinction is why _Line's methods return a bool. An arrow key that
        merely moves the caret must leave the highlighted completion row alone;
        a keystroke that changes what has been typed invalidates it, because the
        menu it was pointing into has just been recomputed underneath it.
        """
        if changed:
            self._changed()
        else:
            self.draw()

    def set(self, text):
        self.line.set(text)
        self.scroll = 0
        self._changed()

    def insert(self, ch):
        self._after(self.line.insert(ch))

    def backspace(self):
        # Unconditionally treated as a text change, even at column 0 where it
        # does nothing. Backspace is how somebody dismisses a completion menu
        # they did not want, and it has to work on the keystroke after the one
        # that emptied the line.
        self.line.backspace()
        self._changed()

    def delete(self):
        self.line.delete()
        self._changed()

    def move(self, by):
        self._after(self.line.move(by))

    def home(self):
        self._after(self.line.home())

    def end(self):
        self._after(self.line.end())

    def kill_left(self):
        self._after(self.line.kill_left())

    def kill_right(self):
        self._after(self.line.kill_right())

    def kill_word(self):
        self._after(self.line.kill_word())

    def select(self, by):
        m = self.matches() or self.arg_matches() or []
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
        if m:
            word = self.text[1:]
            shared = os.path.commonprefix([c[0] for c in m])
            if len(shared) > len(word):
                self.set("/" + shared)
                return
            name, args, _ = m[min(self.sel, len(m) - 1)]
            self.set("/" + name + (" " if args else ""))
            return

        values = self.arg_matches()
        if not values:
            return
        self.set(f"/{self.text[1:].partition(' ')[0]} {self.pick()}")

    def completes_on_enter(self):
        """Should Enter finish the argument first?

        Only when there is something unambiguous to finish: a run-name menu is
        open, and what has been typed is not already one of the names in it. A
        name typed in full, or pasted, submits as itself.
        """
        values = self.arg_matches()
        if not values:
            return False
        typed = self.text[1:].partition(" ")[2].strip()
        return typed.lower() not in [v[0].lower() for v in values]

    def pick(self):
        """The argument value Tab or Enter should settle on.

        Run names share long prefixes -- rnaseq-light-0726 and
        rnaseq-light-0726-2 differ in the last two characters -- so completing to
        the common prefix, which is what Tab does for commands, stopped at
        "rnaseq-" and looked like nothing had happened. Worse, the completion
        counted as a text change, which reset the highlighted row, so the next Tab
        chose the first candidate rather than the one under the cursor.

        So this resolves to a whole name, always: the highlighted row when there
        is a choice, and the only row when there isn't. Tab and Enter both go
        through it, which is what makes the arrow keys mean what they look like
        they mean.
        """
        values = self.arg_matches() or []
        if not values:
            return ""
        return values[min(self.sel, len(values) - 1)][0]


class Prompt:
    """The app's input line. One instance, reused for every turn, so history
    accumulates across the session."""

    def __init__(self, commands=(), arguments=None):
        # A callable is allowed and is resolved on every read, so a command
        # whose hint depends on state -- /verbose, whose argument is whichever
        # way it would flip -- is right each time the menu opens rather than
        # frozen as it was when the prompt was built.
        self.commands = commands if callable(commands) else list(commands)
        # See _Editor.arguments: a callable that turns a command name into the
        # values its first argument could take.
        self.arguments = arguments
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

        commands = self.commands() if callable(self.commands) else self.commands

        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        ed = _Editor(commands, self.history, initial=initial,
                     arguments=self.arguments)
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
                    # A highlighted run name is accepted by Enter, not just by
                    # Tab. With the menu open and a row picked out with the arrow
                    # keys, Enter meaning "submit the two characters I typed" is
                    # indefensible -- it produced "No run named 'rn'" with the
                    # right name sitting highlighted on the screen. Only ever
                    # replaces a partial name with a real one: an argument that
                    # already matches a candidate exactly is left alone.
                    if ed.completes_on_enter():
                        ed.complete()
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


_ELSEWHERE = object()          # sentinel for the free-text row


# Whether choice panels show their descriptions. Session state, flipped by `?`
# inside any panel and remembered for every panel after it.
#
# On by default, and that is the whole design. "germline_snv" means nothing the
# first time and the blurb beside it is the difference between a menu and a
# guess; by the fiftieth time it is nine lines of text between you and the row
# you already know you want. Neither audience is wrong, and neither is served by
# a setting somebody has to find and remember they set -- so it is one key,
# inside the panel, advertised on the hint line, and it sticks for the session.
_DETAILS = [True]


def details_on():
    return _DETAILS[0]


def option_lines(rows, cursor, picked=(), multi=False, field=None,
                 details=None, numbered=True):
    """The rows of a choice panel, as printable lines.

    Returned rather than printed, and module-level rather than closed over
    choose()'s locals, for the reason mirror_lines() is: the layout is the part
    worth checking, and it used to be unreachable without a pty. Everything
    around it -- raw mode, the reader, the repaint arithmetic -- is what makes
    a terminal work and not what makes a panel readable.

    ONE DESCRIPTION COLUMN, measured from the widest label. They used to start
    three spaces after each label, which put them at a different column on
    every row and made a list of options read as prose with the labels buried
    in it. The mirror earns its legibility by being a table; so does this. The
    width is capped so one long option cannot push every description off the
    right-hand edge.
    """
    picked = set(picked or ())
    details = details_on() if details is None else details
    # With descriptions hidden the column serves only the field, so it collapses
    # to whatever the labels need. A gutter held open for text that is not being
    # drawn is the raggedness this column was introduced to remove.
    labelw = (min(max((len(r.label) for r in rows), default=0) + 3, 26)
              if details or field is not None else 1)
    out = []
    for i, row in enumerate(rows):
        here = i == cursor
        mark = f"{display.GREEN}❯{display.RESET}" if here else " "
        if multi:
            num = (f"{display.GREEN}◉{display.RESET}" if i in picked
                   else f"{display.DIM}◯{display.RESET}")
        elif not numbered:
            # A panel with one row has nothing to number. "1" beside the only
            # option implies a second one somewhere and invites looking for it.
            num = ""
        else:
            num = f"{display.DIM}{i + 1:>2}{display.RESET}"
        if i in picked:
            label = f"{display.GREEN}{row.label}{display.RESET}"
        elif here:
            label = f"{display.BOLD}{display.WHITE}{row.label}{display.RESET}"
        else:
            label = row.label
        pad = " " * max(1, labelw - len(row.label))
        line = f"  {mark} {num}  {label}" if num else f"  {mark} {label}"
        # A field replaces its row's description rather than sitting beside it:
        # the value IS what that row now says, and a description repeating what
        # the row already announces is the duplication this panel exists to
        # stop printing.
        mine = field is not None and row.value == field.row
        if mine:
            line += f"{pad}{field.render(here)}"
            aside = field.note(field.text)
            if aside and not here:
                line += f"   {display.DIM}{aside}{display.RESET}"
        elif row.description and details:
            line += f"{pad}{display.DIM}{row.description}{display.RESET}"
        out.append(line)
        # While the field is live its note gets a line of its own, indented
        # under the value it is about. On the row it would be read as a
        # description of the OPTION -- "already exists" beside "save as a new
        # run" says the wrong thing entirely.
        if mine and here:
            aside = field.note(field.text)
            if aside:
                out.append(f"       {display.AMBER}▌{display.RESET} {aside}")
    return out


class Field:
    """An editable value living ON one row of a choice panel.

    For the case where picking an option and saying what it applies to are the
    same decision, and splitting them into two screens makes the second one
    arrive too late to matter. The fork ending is the case that motivated it:
    "hold as a new launch" used to be picked from a menu, and only THEN did a
    bare prompt ask what the new run should be called -- so the answer to "is
    this name already taken?" landed after the fork had been committed to, and
    unique_name() silently appended a `-2` nobody was shown.

    On the row, the name is visible while the choice is still being made, and
    `note` is recomputed on every keystroke, so a collision is stated before
    Enter rather than resolved behind somebody's back.

    `note` is a callable rather than a string because it is a function of what
    has been typed so far, and the whole value of putting it here is that it
    keeps up with the typing.

    `ok` decides whether Enter is allowed to leave with what is typed, and is
    separate from `note` because most notes are not refusals. "pouletrun
    already exists -- enter saves this as pouletrun-2" is something to KNOW,
    and Enter should work; "a run name is letters, digits, dot, dash and
    underscore" is a refusal, and Enter must not. Before this the panel took
    the illegal name, handed it on, and the caller printed an error and
    returned -- throwing away a change set somebody had spent four prompts
    assembling because they mistyped its name at the last step.
    """

    __slots__ = ("row", "line", "note", "hint", "ok")

    def __init__(self, row, initial="", note=None, hint="", ok=None):
        self.row = row
        self.line = _Line(initial)
        self.note = note or (lambda text: "")
        self.ok = ok or (lambda text: True)
        self.hint = hint or "type to change · enter confirms · esc cancels"

    @property
    def text(self):
        return self.line.text.strip()

    def render(self, active):
        """The field as it sits on its row: caret when active, quiet when not.

        A block character stands in for the terminal's own cursor. The panel
        repaints whole lines from the top on every keystroke and never places a
        real cursor inside one, so a caret has to be part of the text -- and a
        field with no visible caret reads as a label rather than as something
        you can type into, which is the entire point of putting it here.
        """
        text = self.line.text
        if not active:
            return (f"{display.DIM}{_MARK} {text or '—'}{display.RESET}")
        at = self.line.cur
        body = (f"{display.BOLD}{display.WHITE}{text[:at]}{display.RESET}"
                f"{display.GREEN}█{display.RESET}"
                f"{display.BOLD}{display.WHITE}{text[at:]}{display.RESET}")
        return f"{display.GREEN}{_MARK}{display.RESET} {body}"


def _text(value):
    """A string that may have arrived as a callable. See choose's `question`."""
    return value() if callable(value) else value


def choose(question, options, note="", free_text=True, free_label="Something else",
           multi=False, draw=None, cursor=0, field=None,
           on_enter=None, on_escape=None, on_text=None):
    """A numbered choice panel. Returns the chosen value, or None if cancelled.

    `options` are slots.Option-shaped: anything with .value, .label and
    .description. When free_text is on, a final row opens an editable prompt, so
    the panel narrows the answer without ever being a dead end -- the thing that
    makes a menu feel helpful rather than like a form.

    Digits select immediately when there are nine or fewer rows, because that is
    the whole appeal of a numbered list. Past nine the digits become ambiguous
    (does "1" mean 1 or the start of 12?), so they move the highlight and enter
    confirms. The hint line says which mode is in force rather than leaving it
    to be discovered.

    `multi` turns the same panel into a multi-select and returns a LIST of
    values instead of one. Space toggles a row, enter confirms the set, and
    selected rows are drawn with a filled marker in green. It is a mode on this
    function rather than a second panel elsewhere on purpose: the keyboard
    handling, the headless fallback, the paste guard and the escape behaviour
    are the fiddly parts, and a parallel implementation would drift from them
    one fix at a time.

    `draw` hands the ROWS -- and only the rows -- to somebody else. It is called
    as draw(cursor, picked) on every repaint and returns the lines to print in
    their place; the question, the note and the key hints stay here. /modify
    uses it to draw the command mirror instead of a plain list, so moving the
    cursor lights up the flag it is about to change. The split is the same
    argument as `multi` above, made once more: the fiddly part of a panel is the
    raw-mode keyboard, not the text, so what varies is the text.

    A drawn panel may show lines that are not options at all -- `-g cmd.sh` is
    worth seeing and cannot be changed -- and that costs nothing here, because
    the cursor only ever indexes `options`. Context is drawn; only choices are
    counted.

    `cursor` is where the highlight starts, and defaults to the top. /modify
    moves it, because the first line of its panel is the invocation -- the
    pipeline -- and that is the single most destructive thing on the screen.
    Opening with the cursor already resting on it undoes the reason the rows are
    ordered the way they are: the panel has to be safe to explore, which means
    the row you land on by accident should be one where nothing happens.

    `field` is a Field the cursor can type into when it rests on that row -- see
    Field, and note that while it is active the digit keys are TEXT rather than
    row selectors, because a run called `2` is a legal run name and a panel that
    jumped rows halfway through typing one would be unusable. The chosen value
    still comes back as the return; the caller reads field.text for the rest.

    A PANEL WHOSE ROWS CHANGE WHILE IT IS OPEN. `options` may be a callable
    instead of a list, re-read on every repaint, and `question` may be too. With
    `on_enter` and `on_escape`, that is enough for a row to open in place --
    /modify's panel unfolds a row's choices underneath it, as more rows in this
    same flat list, and the heading becomes the question being asked.

    It is one list and one cursor, deliberately. The alternative was a second
    choose() for the opened row, and that cannot work: paint() rewrites its own
    block by moving up its own line count, and `painted` starts at zero on every
    call -- so a second panel paints BELOW the first rather than inside it,
    which is the stacked-screens layout the in-place design exists to replace.
    Nesting a cursor inside a cursor was the other option, and a flat list with
    indented rows gets the same result without a second keyboard model.

        on_enter(value)   what Enter does instead of returning. Falsy means the
                          normal thing -- pick it and leave. True keeps the
                          panel open; an int keeps it open and moves the cursor
                          there, which is how opening a row lands you on its
                          first choice.
        on_escape()       True closes an open row and keeps the panel; falsy
                          cancels the panel, which is what Escape means when
                          nothing is open.
        on_text(key)      a printable character or backspace that nothing else
                          claimed. /modify narrows an open row's choices with
                          it. Digits are NOT offered here when they are picking
                          rows -- a key cannot both choose option 3 and type a
                          3, and picking is the older meaning.

    Both are called between repaints, so a hook may change whatever `options`
    reads and the next paint will show it.

    Falls back to a printed list and input() with no terminal, so the panel
    works over a pipe and in tests. The fallback ignores `draw` and prints the
    plain list: a repaint hook is meaningless without a cursor to repaint for.
    It also ignores the hooks and the callable forms, taking one reading of the
    rows -- a panel nobody can press a key in cannot open anything.
    """
    def current_rows():
        got = list(_text(options) if callable(options) else options)
        if free_text:
            got = got + [_FreeRow(free_label)]
        return got

    rows = current_rows()
    if not rows:
        return [] if multi else None

    quick = len(rows) <= 9

    if not sys.stdin.isatty():
        return _choose_headless(_text(question), rows, _text(note), quick,
                                multi=multi, field=field)

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    cursor = cursor if 0 <= cursor < len(rows) else 0
    painted = 0
    picked = set()

    def refresh(at=None):
        """Re-read the rows after a hook has moved something underneath us.

        The cursor is clamped rather than preserved by identity: the row it was
        on may not exist any more, and landing at the end of a shorter list is
        the one behaviour that is never surprising.
        """
        nonlocal rows, cursor, quick
        rows = current_rows()
        quick = len(rows) <= 9
        if at is not None:
            cursor = at
        cursor = max(0, min(cursor, len(rows) - 1)) if rows else 0

    def block():
        out = []
        head = (f"  {display.GREEN}▌{display.RESET} "
                f"{display.BOLD}{_text(question)}{display.RESET}")
        if multi:
            head += (f"          {display.DIM}space to select · enter when "
                     f"done{display.RESET}")
        out.append(head)
        out.append("")
        if draw:
            out.extend(draw(cursor, set(picked)))
        else:
            out.extend(option_lines(rows, cursor, picked, multi, field))
        out.append("")
        # The note under a live field is about that field, not about the panel.
        # Saying "nothing is submitted by any of these" beneath a name somebody
        # is typing answers a question they stopped asking two keystrokes ago.
        on_field = (field is not None and 0 <= cursor < len(rows)
                    and rows[cursor].value == field.row)
        shown = _text(note)
        if shown and not on_field:
            out.append(f"     {display.DIM}{shown}{display.RESET}")
        if on_enter is not None and not on_field:
            # A panel that redefines Enter has to say so itself. Printing
            # "enter picks" underneath a note that says "enter opens a row" is
            # worse than printing nothing: two hints that disagree teach people
            # to read neither.
            return out
        if on_field:
            keys = field.hint
        elif multi:
            keys = "space toggles, enter confirms · esc cancels"
        else:
            keys = ("1-9 to pick" if quick else "digits move, enter picks")
            keys += " · esc cancels"
        # Advertised only where there is something to reveal or hide. A panel
        # whose rows carry no descriptions would be offering a key that does
        # nothing visible, which is how a hint line stops being read.
        if not on_field and any(r.description for r in rows):
            keys += " · ? details" if not details_on() else " · ? hides details"
        out.append(f"     {display.DIM}↑↓ · {keys}{display.RESET}")
        return out

    def paint():
        nonlocal painted
        if painted:
            sys.stdout.write(f"\033[{painted}A")
        lines = block()
        for line in lines:
            sys.stdout.write(f"\r{line}\033[K\r\n")
        painted = len(lines)
        sys.stdout.flush()

    chosen = None
    confirmed = False
    try:
        tty.setraw(fd)
        print()
        paint()
        reader = _Reader(fd)
        while True:
            key = reader.key()
            # A field under the cursor claims the keyboard for everything that
            # is editing, and nothing that is navigation. ↑↓ still move off the
            # row -- being unable to leave a field without answering it is the
            # trap this panel exists to avoid -- and Enter still confirms.
            on_field = (field is not None and 0 <= cursor < len(rows)
                        and rows[cursor].value == field.row)
            if on_field and key not in ("\r", "\n", "up", "down", "escape",
                                        "\x1b", "\x03", "\t"):
                if field.line.key(key) is not None:
                    paint()
                    continue
            if isinstance(key, tuple):            # a paste; ignore in a menu
                continue
            if key in ("\r", "\n"):
                # A field that refuses what is typed refuses Enter with it. The
                # note is already on screen saying why, so this simply does
                # nothing -- which is the correct amount of ceremony for a
                # keypress that was a mistake, and infinitely better than the
                # alternative it replaced: taking the bad value, failing in the
                # caller, and discarding the whole change set on the way out.
                if on_field and not field.ok(field.text):
                    continue
                if on_enter is not None and not multi and 0 <= cursor < len(rows):
                    verdict = on_enter(rows[cursor].value)
                    if verdict is not False and verdict is not None:
                        refresh(verdict if isinstance(verdict, int)
                                and not isinstance(verdict, bool) else None)
                        paint()
                        continue
                if multi:
                    # Enter on an empty set takes the row under the cursor.
                    # Confirming nothing is almost always a missed space bar,
                    # and answering it with "cancelled" teaches the wrong
                    # lesson about a key that did work.
                    if not picked:
                        picked.add(cursor)
                    confirmed = True
                else:
                    chosen = rows[cursor]
                break
            if key == "\x03":
                raise KeyboardInterrupt
            # _Reader.key() names a bare Escape "escape" rather than returning
            # the raw byte, so matching on "\x1b" here would never fire and the
            # panel would be inescapable.
            if key in ("escape", "\x1b", "\x04"):
                if on_escape is not None and on_escape():
                    refresh()
                    paint()
                    continue
                break
            if key == "?" and not on_field:
                # Not while a field is live: `?` is a character somebody might
                # be typing, and a panel that reflowed underneath a half-typed
                # name would be answering a question nobody asked.
                _DETAILS[0] = not _DETAILS[0]
            elif key == " " and multi:
                picked.symmetric_difference_update({cursor})
            elif key == "up":
                cursor = (cursor - 1) % len(rows)
            elif key == "down":
                cursor = (cursor + 1) % len(rows)
            elif key in ("home", "\x01"):
                cursor = 0
            elif key in ("end", "\x05"):
                cursor = len(rows) - 1
            elif key.isdigit() and key != "0":
                index = int(key) - 1
                if index < len(rows):
                    cursor = index
                    if multi and quick:
                        picked.symmetric_difference_update({cursor})
                    elif quick:
                        # A digit is Enter on that row, so it goes through the
                        # same hook. Without this, 3 would pick a row that
                        # Enter would merely have opened -- the two keys are
                        # advertised as the same action and have to be one.
                        if on_enter is not None:
                            verdict = on_enter(rows[cursor].value)
                            if verdict is not False and verdict is not None:
                                refresh(verdict if isinstance(verdict, int)
                                        and not isinstance(verdict, bool)
                                        else None)
                                paint()
                                continue
                        chosen = rows[cursor]
                        break
            elif on_text is not None and (
                    key in ("\x7f", "\x08") or
                    (len(key) == 1 and key.isprintable())):
                if on_text(key):
                    refresh()
            paint()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)

    if multi:
        if not confirmed:
            print()
            return None
        out = []
        for i in sorted(picked):
            row = rows[i]
            if row.value is _ELSEWHERE:
                extra = ask(_text(question))
                if extra:
                    out.append(extra)
            else:
                out.append(row.value)
        return out

    if chosen is None:
        print()
        return None
    if chosen.value is _ELSEWHERE:
        return ask(_text(question)) or None
    return chosen.value


class _FreeRow:
    """The 'something else' row. Shaped like slots.Option so the panel does not
    need a special case for it until the moment it is chosen."""

    __slots__ = ("value", "label", "description")

    def __init__(self, label):
        self.value = _ELSEWHERE
        self.label = label
        self.description = "type your own"


def _choose_headless(question, rows, note, quick, multi=False, field=None):
    """No terminal: print the list, read a number or free text.

    Deliberately accepts the option *text* as well as its number, so a scripted
    test reads as what it means -- "somatic_ensemble" rather than "5".

    In multi mode a comma-separated answer selects several rows, by number or by
    text or a mix of both. This is what keeps the multi-select testable without
    a tty, which is the only way the panel gets exercised in CI at all.

    A `field` cannot be typed into here -- there is no cursor to put on its row
    -- but its value is still PRINTED, because it is part of what that row
    means. A scripted run that picks "save as a new run" gets the proposed name
    and needs to be able to see what it got.
    """
    print(f"\n{question}")
    for i, row in enumerate(rows):
        if field is not None and row.value == field.row:
            tail = f"   {field.text}"
            aside = field.note(field.text)
            if aside:
                tail += f"   ({aside})"
        else:
            tail = f"   {row.description}" if row.description else ""
        print(f"  {i + 1:>2}  {row.label}{tail}")
    if note:
        print(f"      {note}")
    try:
        answer = input("choices: " if multi else "choice: ").strip()
    except EOFError:
        return [] if multi else None
    if not answer:
        return [] if multi else None

    if multi:
        out = []
        for part in (p.strip() for p in answer.split(",")):
            if not part:
                continue
            if part.isdigit() and 1 <= int(part) <= len(rows):
                row = rows[int(part) - 1]
                if row.value is _ELSEWHERE:
                    continue
                out.append(row.value)
                continue
            match = next((r for r in rows if r.value is not _ELSEWHERE
                          and (str(r.value) == part or r.label == part)), None)
            out.append(match.value if match else part)
        return out

    if answer.isdigit():
        index = int(answer) - 1
        if 0 <= index < len(rows):
            row = rows[index]
            if row.value is _ELSEWHERE:
                try:
                    return input(f"{question} ").strip() or None
                except EOFError:
                    return None
            return row.value
        return None
    for row in rows:
        if row.value is not _ELSEWHERE and str(row.value) == answer:
            return row.value
    return answer                        # treat anything else as free text


def _complete_path(text, cur):
    """Tab inside a free-text answer: finish the filename being typed.

    The question this exists for is "which readset file?" with nothing matching in
    the working directory, which leaves the person typing an absolute path by hand
    -- and typing /home/pbourque/scratch/lighttest/readset.rnaseq.txt correctly,
    from memory, is not a reasonable thing to ask of anybody.

    Completes to the longest common prefix, exactly like a shell, and appends a
    slash on a directory so the next Tab carries on into it. No listing of the
    alternatives: this prompt owns a single line that it repaints in place, and
    printing a column of candidates underneath would tear it apart. A prefix that
    stops advancing is the signal that there is more than one answer.
    """
    head, sep, token = text[:cur].rpartition(" ")
    if not token:
        return text, cur
    expanded = os.path.expanduser(token)
    try:
        hits = glob.glob(expanded + "*")
    except Exception:
        return text, cur
    if not hits:
        return text, cur
    shared = hits[0] if len(hits) == 1 else os.path.commonprefix(hits)
    if len(hits) == 1 and os.path.isdir(shared) and not shared.endswith(os.sep):
        shared += os.sep
    if token.startswith("~") and shared.startswith(os.path.expanduser("~")):
        shared = "~" + shared[len(os.path.expanduser("~")):]
    if len(shared) <= len(token):
        return text, cur
    completed = head + sep + shared
    return completed + text[cur:], len(completed)


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
    line = _Line(default)

    def paint():
        sys.stdout.write(f"\r{prompt}{line.text}\033[K")
        back = len(line.text) - line.cur
        if back:
            sys.stdout.write(f"\033[{back}D")
        sys.stdout.flush()

    try:
        tty.setraw(fd)
        paint()
        reader = _Reader(fd)
        while True:
            key = reader.key()
            # Everything this question does differently from the main prompt,
            # and nothing it does the same. Enter submits, Tab completes a PATH
            # rather than a command, and Ctrl+D on an empty line is EOF. The
            # editing keys go to _Line, which is the whole point of _Line.
            if key in ("\r", "\n"):
                break
            if key == "\x03":
                raise KeyboardInterrupt
            if key == "\x04" and not line.text:
                raise EOFError
            if key == "\t":
                line.text, line.cur = _complete_path(line.text, line.cur)
            else:
                line.key(key)
            paint()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    print("\r\n", end="")
    return line.text


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
        self._paused = False

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

    def pause(self):
        """Give the terminal back to something that needs to own it.

        The agent asks questions in the middle of a turn, and a choice panel
        repaints itself by moving the cursor up over its own lines. A spinner
        ticking away underneath would be erasing rows the panel is counting on,
        so the spinner stops and stdout goes back to the real one -- the panel
        must not be writing through the proxy either.

        Idempotent, and a no-op when there is no terminal at all.
        """
        if not self.on or self._paused:
            return
        with self._lock:
            self._paused = True
            self._erase()
            sys.stdout = self._real
            self._real.flush()

    def resume(self):
        """Take it back and start ticking again."""
        if not self.on or not self._paused:
            return
        with self._lock:
            sys.stdout = _Proxy(self, self._real)
            self._at_bol = True
            self._paused = False

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
                # scribble over half a line. Paused means something else owns
                # the screen entirely.
                if self._paused or not self._at_bol:
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
