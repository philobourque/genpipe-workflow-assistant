#!/usr/bin/env python
"""The /modify panel: one list, and the row you are answering is IN it.

The panel this replaced asked its questions underneath itself -- pick a row, get
a fresh screen below the command, answer it, get another screen below that. Four
changes meant four screens stacked down the terminal, each one redrawing the
command it was asking about. Here the row opens where it sits and collapses back
green, so the list never grows a second list under it.

Two properties are the whole design and both are checked below:

  * WHILE A ROW IS OPEN, ONLY ITS ANSWERS ARE SELECTABLE. That is what makes a
    digit key mean what it shows -- choice 3 is the third line under the row,
    not the third selectable thing on screen -- and it stops the cursor
    wandering off, leaving a row open somewhere nobody is looking.
  * A ROW WITH NO VOCABULARY OPENS ANYWAY. `name`, `steps` and `output` have
    nothing to offer, and used to drop out to a prompt below the panel -- which
    put the stacked layout back one row at a time. They get a caret on the row
    instead, and Enter takes the text through the same check() every option
    goes through, so `protocol` does not become free text by that door.

Everything here is pure: entries in, entries out. The keyboard lives in
ui.choose and the drawing in display.modify_panel, which is what lets the layout
be tested without a pty.

Run:  python tests/test_panel.py
"""
import sys

from harness import Report

from genpipe import mirror
from genpipe import modify
from genpipe import slots


PROPOSAL = {
    "command": "bash cmd.sh",
    "generated": ("genpipes dnaseq -t germline_snv -s 1-5 "
                  "-r readset.tsv -g dnaseq.sh"),
    "slots": {"pipeline": "dnaseq", "protocol": "germline_snv", "steps": "1-5",
              "readset": "readset.tsv", "inis": [], "pairs": None,
              "design": None, "output_dir": None},
}

OFFERED = ["name", "protocol", "steps", "readset", "config", "output"]


def build(open_row=None, choices=(), typed="", changes=None):
    m = mirror.read(PROPOSAL["generated"], name="run1").ensure(OFFERED)
    return modify.panel_entries(m, OFFERED, open_row, choices, typed, changes)


def kinds(entries, kind):
    return [e for e in entries if e.kind == kind]


def main():
    r = Report("modify panel")

    # ------------------------------------------------------------------ #
    r.section("closed: the command is the menu")

    entries = build()
    picks = modify.selectable(entries)
    r.check("every offered row can be opened",
            {e.row for e in picks if e.kind == modify.ROW} >= set(OFFERED))
    r.equal("picks are numbered without gaps",
            [e.pick for e in picks], list(range(len(picks))))
    r.check("a row nobody can change is still drawn",
            any(e.kind == modify.ROW and e.pick is None for e in entries))
    r.equal("no choices while nothing is open", len(kinds(entries, modify.CHOICE)), 0)
    r.equal("no caret while nothing is open", len(kinds(entries, modify.TYPED)), 0)

    # 'describe it instead' is always there; 'review N changes' only once
    # something has moved -- a row offering to review nothing is a dead end
    # dressed as an action.
    extras = [e.row for e in kinds(entries, modify.EXTRA)]
    r.equal("nothing changed yet, so nothing to review", extras, [modify.ELSE])
    r.equal("one change, one review row",
            [e.row for e in kinds(build(changes={"steps": "1-3"}),
                                  modify.EXTRA)],
            [modify.DONE, modify.ELSE])

    # ------------------------------------------------------------------ #
    r.section("open: the answers belong to the row above them")

    protos = modify.options_for("protocol", PROPOSAL)
    entries = build("protocol", protos)
    picks = modify.selectable(entries)
    r.check("the choices are the only thing selectable",
            all(e.kind == modify.CHOICE for e in picks))
    r.equal("so digit N is the Nth line under the row",
            [e.pick for e in picks], list(range(len(protos))))

    # The choices are inserted after their own row, not appended to the list.
    order = [(e.kind, e.row) for e in entries]
    at = order.index((modify.ROW, "protocol"))
    r.equal("and they sit directly under it",
            order[at + 1][0], modify.CHOICE)

    r.check("the open row is not selectable while it is open",
            all(not (e.kind == modify.ROW and e.row == "protocol"
                     and e.pick is not None) for e in entries))
    r.equal("cursor_of lands on the first of them",
            modify.cursor_of(entries, "protocol"), 0)

    # ------------------------------------------------------------------ #
    r.section("typing narrows the open row")

    narrowed = build("protocol", protos, typed="germline")
    left = [e.label for e in kinds(narrowed, modify.CHOICE)]
    r.check("only the matching protocols survive",
            left and all("germline" in x for x in left))
    r.check("and there are fewer than before", len(left) < len(protos))
    r.equal("still numbered from one", [e.pick for e in kinds(narrowed, modify.CHOICE)],
            list(range(len(left))))

    # Substring, not prefix: somebody typing a word from the blurb is
    # describing what they want, and a prefix match would answer with nothing.
    r.truthy("a word from the description finds it too",
             kinds(build("protocol", protos, typed="tumour"), modify.CHOICE)
             or kinds(build("protocol", protos, typed="variant"), modify.CHOICE))

    # ------------------------------------------------------------------ #
    r.section("a row with no vocabulary opens in place")

    for row in ("name", "steps", "output"):
        r.equal(f"{row} has nothing to offer",
                modify.options_for(row, PROPOSAL), [])
        entries = build(row, modify.options_for(row, PROPOSAL), typed="x")
        typing = kinds(entries, modify.TYPED)
        r.equal(f"{row} gets a caret instead of a menu", len(typing), 1)
        r.equal("which is what the keyboard lands on",
                [e.pick for e in modify.selectable(entries)], [0])
        r.equal("and it belongs to that row", typing[0].row, row)

    # The caret draws nothing of its own -- the row is already showing what has
    # been typed -- so it carries no label to draw.
    r.equal("the caret entry has no label to render",
            kinds(build("name", (), typed="abc"), modify.TYPED)[0].label, "")

    # ------------------------------------------------------------------ #
    r.section("narrowing to nothing still lets you answer")

    # A file row's options are the paths the scan found. Naming one it did not
    # find has to stay possible, or the panel can only ever pick from a list
    # that was never meant to be exhaustive.
    found = [slots.Option("scanned.tsv", "scanned.tsv")]
    entries = build("readset", found, typed="elsewhere/other.tsv")
    r.equal("no option matches what was typed",
            len(kinds(entries, modify.CHOICE)), 0)
    r.equal("so the text itself becomes the answer",
            len(kinds(entries, modify.TYPED)), 1)
    r.equal("and it is the only thing Enter can reach",
            len(modify.selectable(entries)), 1)

    # ...but only as far as check() allows. This is the door protocol must not
    # walk through: typing a protocol that does not exist offers a caret, and
    # check() is what refuses it.
    entries = build("protocol", protos, typed="not_a_protocol")
    r.equal("an unknown protocol offers the same caret",
            len(kinds(entries, modify.TYPED)), 1)
    verdict = modify.check("protocol", "not_a_protocol", PROPOSAL)
    r.check("and check() is what refuses it", not verdict)
    r.truthy("naming the real ones", verdict.options)

    r.check("a real protocol typed in full is accepted",
            bool(modify.check("protocol", "germline_sv", PROPOSAL))
            or bool(modify.check("protocol", "germline_snv", PROPOSAL)))

    # ------------------------------------------------------------------ #
    r.section("the caret answers go through the same gate")

    r.check("a legal step range settles",
            bool(modify.check("steps", "3,6-8", PROPOSAL)))
    r.check("a malformed one does not",
            not modify.check("steps", "3 to 8", PROPOSAL))
    r.check("an illegal run name does not",
            not modify.check("name", "test_/modify_steps", PROPOSAL))
    r.equal("and it is offered back, legal",
            modify.check("name", "test_/modify_steps", PROPOSAL).options[0].value,
            "test_modify_steps")
    r.check("an empty answer is refused rather than committed",
            not modify.check("name", "   ", PROPOSAL))

    # ------------------------------------------------------------------ #
    r.section("answered rows come back green, in the list")

    before = [e.row for e in build() if e.kind == modify.ROW]
    entries = build(changes={"protocol": "germline_sv", "steps": "1-3"})
    r.equal("the rows stay exactly where they were",
            [e.row for e in entries if e.kind == modify.ROW], before)
    r.check("still selectable, so an answer can be changed again",
            all(any(e.row == row and e.pick is not None for e in entries)
                for row in ("protocol", "steps")))
    r.equal("and no answer screen was added anywhere",
            len(kinds(entries, modify.CHOICE)) + len(kinds(entries, modify.TYPED)),
            0)

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
