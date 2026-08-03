#!/usr/bin/env python
"""The editable line, driven by keys instead of by a terminal.

_Line is the text-and-caret half of every place this app lets somebody type:
the main prompt's framed box, the follow-up question after it, and the field
that sits inside a /modify panel row. Those three paint in three completely
different ways and share nothing about drawing -- but "what does Ctrl+W do to
this string" has to be one answer, or the least-used of the three is the one
that is quietly wrong.

Which is why this suite exists at all. Testing the old arrangement meant
driving a pty and reading escape sequences back, so in practice it was not
tested: the edit loop lived inside a `try: tty.setraw(fd)` and could not be
reached without one. Pulling the state out means the keys can just be sent.

Stdlib only, and no terminal -- it runs in CI like the rest of tests/.
"""
import sys

from harness import Report

from genpipe.slots import Option
from genpipe.ui import Field, _Line, option_lines


def plain(lines):
    """The panel with its colour stripped, which is what a layout assertion is
    actually about."""
    import re
    return [re.sub(r"\033\[[0-9;]*m", "", line) for line in lines]


def send(line, keys):
    """Type a sequence of keys into a line and hand it back."""
    for key in keys:
        line.key(key)
    return line


def main():
    r = Report("the editable line")

    # ------------------------------------------------------------------ #
    r.section("typing, and where the caret ends up")

    line = send(_Line(), list("gatk"))
    r.equal("printable keys land in order", line.text, "gatk")
    r.equal("and the caret follows them", line.cur, 4)

    line = _Line("pouletrun")
    r.equal("an initial value is pre-typed", line.text, "pouletrun")
    r.equal("with the caret at the end, ready to correct it", line.cur, 9)

    # Insertion in the middle, which is the whole reason a caret exists.
    line = _Line("pouletrun")
    send(line, ["home", "right", "right"])
    line.insert("X")
    r.equal("insert happens AT the caret", line.text, "poXuletrun")
    r.equal("and the caret sits after what was inserted", line.cur, 3)

    # ------------------------------------------------------------------ #
    r.section("deleting in both directions")

    line = send(_Line("abc"), ["\x7f"])
    r.equal("backspace takes the character to the left", line.text, "ab")
    line = send(_Line("abc"), ["home", "delete"])
    r.equal("delete takes the one to the right", line.text, "bc")

    # The no-ops. Both are reachable by holding a key down and both used to be
    # written slightly differently in each copy of this loop.
    line = send(_Line("abc"), ["home", "\x7f"])
    r.equal("backspace at column 0 changes nothing", line.text, "abc")
    r.check("and reports that nothing changed", not _Line("x").home()
            and not send(_Line("abc"), ["home"]).key("\x7f"))
    line = send(_Line("abc"), ["end", "delete"])
    r.equal("delete at the end changes nothing", line.text, "abc")

    # ------------------------------------------------------------------ #
    r.section("the control codes, which must mean one thing everywhere")

    line = _Line("some long answer")
    send(line, ["home", "right", "right", "right", "right", "\x15"])
    r.equal("Ctrl+U clears to the LEFT of the caret", line.text,
            " long answer")
    r.equal("leaving the caret at the start", line.cur, 0)

    line = _Line("some long answer")
    send(line, ["home", "right", "right", "right", "right", "\x0b"])
    r.equal("Ctrl+K clears to the right", line.text, "some")

    line = send(_Line("raise the walltime"), ["\x17"])
    r.equal("Ctrl+W takes the word to the left", line.text, "raise the ")
    line = send(_Line("trailing   "), ["\x17"])
    r.equal("and skips the space before it first", line.text, "")

    # ------------------------------------------------------------------ #
    r.section("what _Line refuses to handle")

    # These mean different things in a prompt, a question and a panel field --
    # Enter submits one and closes another; Escape cancels a panel and is
    # ignored at the prompt -- so _Line must not decide for them.
    for key in ("\r", "\n", "\t", "\x1b", "escape", "\x03", "\x04", "up",
                "down"):
        r.check(f"{key!r} is left to the caller", _Line("x").key(key) is None)

    line = send(_Line("abc"), ["\x1b", "up", "\r"])
    r.equal("so none of them corrupt the text", line.text, "abc")

    # ------------------------------------------------------------------ #
    r.section("text-changed versus caret-moved")

    # The return value is what lets a caller keep a completion menu honest: an
    # arrow key must leave the highlighted row alone, and a keystroke must
    # reset it, because the menu it pointed into has just been recomputed.
    r.check("a keystroke reports a text change", _Line("a").key("b") is True)
    r.check("an arrow key does not", _Line("ab").key("left") is False)
    r.check("nor does home", _Line("ab").key("home") is False)
    r.check("a backspace that removes something does",
            _Line("ab").key("\x7f") is True)

    # ------------------------------------------------------------------ #
    r.section("paste arrives as one chunk, not as keys")

    # The main prompt asks the terminal for bracketed paste so that a pasted
    # newline is not mistaken for Enter. What arrives is a tuple, and it has to
    # go in whole -- a paste inserted character by character would re-run the
    # completion menu once per character on a long path.
    line = _Line("-c ")
    line.key(("paste", "/scratch/inis/rorqual.ini"))
    r.equal("a paste lands whole", line.text, "-c /scratch/inis/rorqual.ini")
    r.equal("with the caret past it", line.cur, 28)

    line = _Line("ab")
    send(line, ["home"])
    line.key(("paste", "XY"))
    r.equal("and honours the caret like any insert", line.text, "XYab")

    # ------------------------------------------------------------------ #
    r.section("a panel's rows line up in one column")

    rows = [Option("apply", "apply to pouletrun", "back to the gate"),
            Option("again", "keep editing", "back to the command")]
    drawn = plain(option_lines(rows, cursor=0))
    starts = [line.index("back to") for line in drawn]
    r.equal("every description starts at the same column",
            len(set(starts)), 1)
    r.contains("the cursor row is marked", drawn[0], "❯")
    r.check("and the others are not", "❯" not in drawn[1])

    # A label long enough to push the column off the screen is capped rather
    # than obeyed, so one option cannot ruin the layout for the rest.
    wide = [Option("a", "x" * 60, "note"), Option("b", "short", "note")]
    r.check("one very long label does not push the column past the cap",
            plain(option_lines(wide, cursor=0))[1].index("note") < 40)

    # ------------------------------------------------------------------ #
    r.section("a field on a row: the value is visible before it is committed")

    def taken(text):
        if not text:
            return "a name is needed to keep both runs"
        return f"{text} already exists" if text == "pouletrun" else ""

    rows = [Option("apply", "apply to pouletrun", "back to the gate"),
            Option("fork", "save as a new run", "pouletrun stays as it is"),
            Option("again", "keep editing", "back to the command")]

    # Cursor away from the field: the value is readable, and its description is
    # NOT -- the value is what that row says now.
    away = plain(option_lines(rows, cursor=0, field=Field("fork", "pouletrun-2")))
    r.contains("the proposed name shows before the row is chosen",
               away[1], "pouletrun-2")
    r.check("and replaces that row's description",
            "pouletrun stays as it is" not in away[1], away[1])

    # Cursor on the field: a caret appears, because a value you can type into
    # and a label look identical without one.
    on = plain(option_lines(rows, cursor=1, field=Field("fork", "pouletrun-2")))
    r.contains("a caret appears when the cursor arrives", on[1], "█")
    r.check("and not when it is elsewhere", "█" not in away[1])

    # THE COLLISION, stated before Enter rather than resolved behind it.
    hit = plain(option_lines(rows, cursor=1,
                             field=Field("fork", "pouletrun", note=taken)))
    r.check("a taken name is called out on its own line",
            any("already exists" in line for line in hit), hit)
    r.check("under the field, not on the option's description",
            "already exists" not in hit[1], hit[1])

    empty = plain(option_lines(rows, cursor=1,
                               field=Field("fork", "", note=taken)))
    r.check("an emptied field says what is missing",
            any("a name is needed" in line for line in empty), empty)

    free = plain(option_lines(rows, cursor=1,
                              field=Field("fork", "pouletrun-2", note=taken)))
    r.equal("a free name says nothing at all", len(free), 3)

    # The caret tracks the caret, not the end of the string.
    field = Field("fork", "abcd")
    field.line.home()
    field.line.move(2)
    mid = plain(option_lines(rows, cursor=1, field=field))
    r.contains("the caret is drawn where the caret is", mid[1], "ab█cd")

    # ------------------------------------------------------------------ #
    r.section("a refusal is not the same thing as a note")

    # Most notes are things to KNOW and Enter should still work; only some are
    # refusals. Conflating them either blocks a legal name or accepts an
    # illegal one -- and accepting it used to throw the change set away in the
    # caller, which is the worst of the three outcomes.
    legal = Field("fork", "pouletrun-2", note=taken,
                  ok=lambda t: bool(t) and "/" not in t)
    r.check("a name with a collision note is still enterable",
            legal.ok(legal.text))

    illegal = Field("fork", "test_/modify", note=taken,
                    ok=lambda t: bool(t) and "/" not in t)
    r.check("an illegal one is not", not illegal.ok(illegal.text))

    blank = Field("fork", "", note=taken, ok=lambda t: bool(t))
    r.check("and neither is an empty one", not blank.ok(blank.text))

    r.check("a field with no ok() given accepts anything",
            Field("fork", "whatever").ok("whatever"))

    # ------------------------------------------------------------------ #
    r.section("descriptions can be got out of the way")

    # "germline_snv" means nothing the first time and the blurb beside it is
    # the difference between a menu and a guess. By the fiftieth time it is
    # nine lines of text between somebody and the row they already want.
    protocols = [Option("germline_snv", "germline_snv",
                        "SNVs and small indels in a normal genome"),
                 Option("somatic_fastpass", "somatic_fastpass",
                        "quick tumour/normal pass")]
    shown = plain(option_lines(protocols, cursor=0, details=True))
    r.contains("on by default, so a first reading has the blurbs",
               shown[0], "SNVs and small indels")

    hidden = plain(option_lines(protocols, cursor=0, details=False))
    r.check("hidden on request", "SNVs and small indels" not in hidden[0])
    r.contains("but the row itself still reads", hidden[0], "germline_snv")
    r.check("and the gutter closes up rather than being held open",
            hidden[0].rstrip() == hidden[0].rstrip(" "),
            repr(hidden[0]))
    r.check("the column is not reserved for text nobody is drawing",
            len(hidden[0]) < len(shown[0]))

    # A field is a value, not a description, so hiding descriptions must not
    # hide it -- that row would then say nothing at all.
    with_field = plain(option_lines(
        rows, cursor=0, field=Field("fork", "pouletrun-2"), details=False))
    r.contains("a field survives descriptions being hidden",
               with_field[1], "pouletrun-2")

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
