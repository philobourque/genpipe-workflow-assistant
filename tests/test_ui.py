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

from genpipe import ui
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

    # ------------------------------------------------------------------ #
    r.section("the composer wraps a paragraph instead of scrolling it away")
    # The reported defect: typing a long request scrolled the box sideways and
    # the beginning of the sentence left the screen, so there was no way to
    # read back what was about to be asked. Widening the box would only have
    # moved the column at which that happens, so the text wraps instead.
    #
    # Everything here is asserted against wrap_segments/caret_at rather than
    # against a terminal, because they are where the arithmetic lives -- the
    # pty can only show that the result looks right, not that the caret lands
    # on the correct character.
    text = ("I want to change something about this run but I am not sure "
            "what exactly it is that I would like to do")
    segs = ui.wrap_segments(text, 30)
    rows = [text[a:b] for a, b, _ in segs]

    r.check("every row fits the width", all(len(x) <= 30 for x in rows), rows)
    r.check("more than one row for a paragraph", len(rows) > 1)
    r.check("the beginning is still on screen", rows[0].startswith("I want"))
    # Nothing may be lost or duplicated by wrapping. Rejoining on the breaks
    # has to give back exactly what was typed.
    r.equal("no character is dropped or repeated", " ".join(rows), text)
    r.check("no row starts or ends on a space",
            not any(x.startswith(" ") or x.endswith(" ") for x in rows), rows)

    r.section("the caret lands on the character it is actually on")
    # Every index, not a sample: an off-by-one on a single wrap boundary is
    # exactly the kind of bug that survives spot checks and is then reported
    # as "the cursor is sometimes wrong".
    bad = []
    for i in range(len(text) + 1):
        row, col = ui.caret_at(segs, i)
        start, end, _ = segs[row]
        if not (0 <= col <= end - start):
            bad.append((i, row, col))
    r.check("every caret position is inside its row", not bad, bad[:4])
    r.equal("the caret starts at the top left", ui.caret_at(segs, 0), (0, 0))
    last_start, last_end, _ = segs[-1]
    r.equal("and ends after the last character typed",
            ui.caret_at(segs, len(text)),
            (len(segs) - 1, last_end - last_start))

    r.section("the degenerate cases have a row for the caret to sit on")
    r.equal("an empty line still has one row", ui.wrap_segments("", 30),
            [(0, 0, 0)])
    r.equal("and the caret is on it", ui.caret_at(ui.wrap_segments("", 30), 0),
            (0, 0))
    runon = "x" * 70
    r.equal("a word longer than the box is hard-broken rather than dropped",
            "".join(runon[a:b] for a, b, _ in ui.wrap_segments(runon, 30)),
            runon)
    r.check("a one-column box does not loop forever",
            len(ui.wrap_segments("abc", 1)) == 3)
    r.check("nor does a zero-width one",
            len(ui.wrap_segments("abc", 0)) == 3)

    r.section("re-wrapping on resize is a recomputation, not a stored layout")
    narrow = ui.wrap_segments(text, 20)
    wide = ui.wrap_segments(text, 60)
    r.check("a narrower window takes more rows", len(narrow) > len(wide))
    r.equal("and neither loses anything",
            " ".join(text[a:b] for a, b, _ in narrow), text)
    r.equal("nor does the wide one",
            " ".join(text[a:b] for a, b, _ in wide), text)

    # ------------------------------------------------------------------ #
    r.section("the keys that reorder a -c stack")
    # `[` and `]` are the advertised pair because they are plain ASCII and no
    # terminal fails to send them. Shift+arrows are accepted where a terminal
    # does send them and are deliberately NOT advertised -- see
    # modify.REORDER_KEYS.
    from genpipe import modify as _modify
    r.equal("[ moves an ini earlier", _modify.reorder_key("["), -1)
    r.equal("] moves it later", _modify.reorder_key("]"), 1)
    r.equal("shift+up is accepted too", _modify.reorder_key("shift-up"), -1)
    r.equal("and shift+down", _modify.reorder_key("shift-down"), 1)
    r.equal("an ordinary character moves nothing",
            _modify.reorder_key("a"), None)
    r.equal("and neither does a bare arrow, which still navigates",
            _modify.reorder_key("up"), None)

    # The reader has to actually produce those names, or the alias is a key
    # binding to nothing. Driven through a pipe, since _Reader wants only an fd.
    import os as _os
    for seq, name in ((b"\x1b[1;2A", "shift-up"), (b"\x1b[1;2B", "shift-down"),
                      (b"\x1b[A", "up"), (b"[", "["), (b"]", "]")):
        read_fd, write_fd = _os.pipe()
        _os.write(write_fd, seq)
        _os.close(write_fd)
        try:
            r.equal(f"{seq!r} decodes to {name!r}",
                    ui._Reader(read_fd).key(), name)
        finally:
            _os.close(read_fd)

    r.section("a reorder key never becomes typed text")
    # ui.choose hands an unclaimed key on to on_text, which appends it to the
    # narrowing filter. So a hook that recognises `[` as a reorder key and then
    # returns False -- which /modify's did whenever the move was a no-op, i.e.
    # on the ini already at the top of the stack -- puts a `[` in the filter,
    # matches no ini, and empties the row.
    #
    # Asserted against the source because the hook is a closure over panel
    # state with no terminal-free way in. What it has to be true of is simple:
    # once the key IS a reorder key, no path may return False.
    import os as _os
    _cli = open(_os.path.join(_os.path.dirname(_os.path.dirname(
        _os.path.abspath(__file__))), "genpipe", "cli.py")).read()
    body = _cli[_cli.index("    def on_reorder(key, value):"):]
    body = body[:body.index("\n    def ")]
    guard, rest = body.split("return False", 1)
    r.check("the key is recognised, and the row checked, before anything else",
            "modify.reorder_key(key)" in guard and "modify.CONFIG" in guard,
            guard)
    r.check("and past that one guard, no path hands the key back unclaimed",
            "return False" not in rest, rest)

    r.section("choose() takes the hook the panel reorders through")
    import inspect as _inspect
    params = _inspect.signature(ui.choose).parameters
    r.check("on_key is a parameter", "on_key" in params)
    r.check("and it defaults to off, so every other panel is unaffected",
            params["on_key"].default is None)

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
