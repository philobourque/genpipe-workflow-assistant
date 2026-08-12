"""The generated command, laid out so it can be pointed at.

The gate has always printed the command it was built from -- wrapped, grey, as
one long line of prose under `bash cmd.sh`. That is enough to prove the command
exists and not nearly enough to read. People who launch GenPipes by hand know
their pipelines as a shape: `-t` here, `-s` there, a `-c` stack at the end. A
paragraph destroys that shape, so the one screen whose whole job is "say what is
about to happen" said it in the least recognisable form available.

This module turns the command back into its shape, and tags each line with the
/modify row that owns it:

    genpipes rnaseq
      protocol     -t   stringtie
      steps        -s   1-5
      config       -c   dnaseq.base.ini
                        rorqual.ini
      script       -g   cmd.sh

The tags are what make the layout worth having. Once a line knows which row
changes it, the /modify panel can highlight the line under the cursor, mark the
lines about to change, and show in green the ones that just did -- so a change
is watched happening to the command rather than described and then verified by
re-reading a box.

READ, NEVER REBUILT
-------------------
Every line here is tokenised out of the command string the model actually wrote.
Nothing is reconstructed from the proposal's slots, and that is not a shortcut
saved -- it is the invariant. A mirror assembled from slots would show a command
that is merely CONSISTENT with what is queued, and would go on looking correct
in exactly the case that matters: a flag the slot parser missed. What is drawn
is the command, in a different order.

The reordering is the one liberty taken. Lines come out in /modify's row order
rather than the order they were typed, so the mirror and the panel beside it
stay in lockstep. GenPipes does not care what order its flags arrive in, and a
mirror whose fourth line is the panel's fourth row is worth more than one that
preserves an ordering nobody chose deliberately.

Standard library only, like slots.py, gate.py and modify.py, so the layout is
checkable in CI without installing biomni.
"""
import re
import shlex

from .gate import LONG_FORM, invocation
from .modify import FLAG_OF, ROWS, SLOT_OF

# Flags that carry no /modify row but are still part of what is about to run.
# They are shown -- a mirror that hid `-g` would be describing a command that
# writes a script without saying which -- and they are simply not selectable.
_LABEL = {
    "-g": "script",
    "-j": "scheduler",
    "-l": "log level",
    "-b": "batch",
    "--genome": "genome",
    "--container": "container",
    "--wrap": "wrap",
    "--force": "force",
    "--json": "json",
    "--no-json": "json",
    "--sanity-check": "sanity check",
    "--report": "report",
    "--clean": "clean",
}

# Which row each flag belongs to, both spellings. Built from modify's table
# rather than restated, so a row that gains a flag gains a mirror line for free.
_ROW_OF = {}
for _row, _flag in FLAG_OF.items():
    _ROW_OF[_flag] = _row
    if LONG_FORM.get(_flag):
        _ROW_OF[LONG_FORM[_flag]] = _row

# A token that opens a flag rather than being one's value. `-1` and a bare `-`
# are values; `-t` and `--steps` are flags.
_FLAG = re.compile(r"^--?[A-Za-z]")

# Shell redirection, not a GenPipes argument. A model that writes its
# generation and its `2>&1` capture in the same block leaves this trailing
# the last real flag -- `-g cmd.sh 2>&1` -- and because `2>&1` starts with a
# digit rather than a dash, _FLAG never matches it either, so without this
# filter it becomes an extra, wrong VALUE on whatever flag preceded it
# (`script` reading "cmd.sh 2>&1", say). Covers every shape genpipes.md's own
# examples use: `>`, `>>`, `2>`, `2>&1`, `&>`, `<`.
_REDIRECTION = re.compile(r"^\d*>>?&?\d*$|^\d*<$")


class Line:
    """One line of the mirror.

    row     the /modify row that changes it, or None if nothing in /modify does
    label   what to call it on screen -- the row name where there is one
    flag    the flag as the command spells it, or '' for a line that is not one
    values  its arguments, one per rendered line. Empty for a bare switch.
    note    a dim aside, used only where a line needs to disclaim itself
    head    whether this line IS the invocation -- `genpipes rnaseq` -- rather
            than a flag under it. Exactly one line ever sets it, and it exists
            so the pipeline can be both the heading everybody reads first and a
            row /modify can put a checkbox against. Splitting those into a
            heading plus a `pipeline` row printed the same word twice, three
            lines apart, on the one screen where duplication reads as two
            different settings.
    warn    whether that aside is a WARNING rather than an observation. Only
            one line sets it -- see _absent() -- and the distinction is worth a
            field because 'design is not set' and 'output is not set' look
            identical and mean opposite things. The first is a flag this run
            does not use; the second is GenPipes about to write into whatever
            directory you were standing in. Colouring both red teaches people
            that red on this screen means nothing.
    """

    __slots__ = ("row", "label", "flag", "values", "note", "warn", "head")

    def __init__(self, row, label, flag, values=(), note="", warn=False,
                 head=False):
        self.row = row
        self.label = label
        self.flag = flag
        self.values = list(values)
        self.note = note
        self.warn = warn
        self.head = head

    @property
    def value(self):
        return self.values[0] if self.values else ""

    def __repr__(self):
        return f"<Line {self.label} {self.flag} {' '.join(self.values)}>"

    def __eq__(self, other):
        return (isinstance(other, Line) and self.row == other.row
                and self.label == other.label and self.flag == other.flag
                and self.values == other.values and self.note == other.note)


class Mirror:
    """A command, as an ordered list of tagged lines.

    The invocation -- 'genpipes rnaseq' -- is the first line, flagged `head`,
    rather than a string beside the list. It used to be a string, on the
    reasoning that the pipeline is context and not a target; making it a line is
    what let /modify offer to change it, which is the change every other change
    hangs off. Everything that reads `m.head` still works: it is a property now.
    """

    __slots__ = ("lines",)

    def __init__(self, head="", lines=()):
        self.lines = list(lines)
        # Only if there is not one already. ensure() rebuilds a Mirror from its
        # own lines and passes its own head back in, and without this guard the
        # invocation appeared twice -- which is cosmetic on the gate and not at
        # all cosmetic in the panel, where a second line with the same row shifts
        # every checkbox index below it by one.
        if head and not any(line.head for line in self.lines):
            self.lines.insert(0, Line("pipeline", "pipeline", "", [head],
                                      head=True))

    @property
    def head(self):
        for line in self.lines:
            if line.head:
                return line.value
        return ""

    def __bool__(self):
        """True only when there is a FLAG line, not merely a head.

        Callers write `mirror.read(...) or mirror.from_slots(...)`: read() is
        preferred because it quotes the command, and from_slots() is the
        fallback that reconstructs from the parsed slots. That fallback only
        fires when read()'s result is falsy, so "read produced a head and
        nothing else" has to count as failure -- otherwise a command that
        tokenised badly draws an approval box naming the pipeline and the run
        and hiding every flag, while the slots that would have filled it sit
        unused on the same proposal.

        That is not hypothetical: it is what a `\\`-continued command did. The
        two failure shapes differed only by luck -- one tripped _groups' own
        dash-inside-a-value guard and returned nothing (so the fallback fired
        and the box was right), the other did not (so a near-empty box was
        drawn over a perfectly good set of slots). Both now take the fallback.
        """
        return any(not line.head for line in self.lines)

    def __repr__(self):
        return f"<Mirror {self.head!r} {len(self.lines)} lines>"

    def index_of(self, row):
        """Which line a /modify row owns, or None if it owns none.

        None is a real answer with two distinct causes, and the caller must
        handle both by highlighting nothing rather than by guessing: `name`
        changes no flag at all, and a row whose flag is absent from the command
        -- adding a `-d` that was never there -- has no line to point at yet.
        """
        for i, line in enumerate(self.lines):
            if line.row == row:
                return i
        return None

    def ensure(self, rows):
        """A copy carrying a placeholder line for each of `rows` that has none.

        /modify offers rows the command does not use yet -- adding a `-d` that
        was never there is a change like any other -- and a panel row with no
        line to point at is a checkbox floating beside nothing. So the rows the
        panel will offer get drawn as absences: the flag, and 'not set' where a
        value would be.

        Only what the caller asks for. read() deliberately does not do this on
        its own, because listing every unused flag on every command is how a
        gate turns into a form: `-p` on a germline run is not a decision left
        unmade, it is a flag that does not apply. modify.rows_for() has already
        decided which absences are real questions; this draws those.
        """
        have = {line.row for line in self.lines}
        extra = [_absent(row) for row in rows
                 if row not in have and row != "name"]
        if not extra:
            return self
        return Mirror(self.head, _ordered(self.lines + extra))


def _tokens(text):
    """Split a command into words, tolerating a quote the model left open.

    shlex is used for the quoted paths that turn up in `-o` and `-c`, and its
    failure mode on unbalanced quotes is an exception. A mirror is a display;
    it degrades to a rougher split rather than taking the gate down with it.

    Shell redirection (`2>&1`, `>`, ...) is dropped here, unconditionally --
    see _REDIRECTION's comment. It is plumbing the model added around the
    genpipes call, never a GenPipes argument, and the one thing worse than
    not showing it is showing it as though it were a flag's value.
    """
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()

    # Everything from the first redirection operator onward is dropped, not just
    # the operator itself. Filtering only the operator left its TARGET behind as
    # a loose word, which _groups then appended to the last flag: `> out.log`
    # became `script -g  cmd.sh / out.log`, which reads as though GenPipes had
    # been told to write two files. Redirection is the model capturing output
    # around the call, and it comes last, so the command is over at that point.
    for i, token in enumerate(tokens):
        if _REDIRECTION.match(token):
            return tokens[:i]
    return tokens


def _groups(tokens):
    """[(flag, [values])] in written order, ignoring anything before the first
    flag. `--flag=value` and `--flag value` come out the same."""
    groups = []
    for token in tokens:
        if _FLAG.match(token):
            flag, eq, value = token.partition("=")
            groups.append((flag, [value] if eq and value else []))
        elif groups:
            groups[-1][1].append(token)
    return groups


def _absent(row, required=False):
    """The line for a row the command does not set.

    `output` gets a longer note than the rest because its absence is not
    neutral: every other missing flag means GenPipes does that thing, while a
    missing `-o` means GenPipes writes into whatever directory you happened to
    be standing in. That is a decision nobody made, and it is the only one the
    gate has always called out in red.

    `required=True` is the second such case: a row named in the proposal's
    `missing` list (see gate.build_proposal) is not "this run doesn't use
    this flag" the way an absent `-p` on a germline run is -- it is a flag
    every run needs and this one does not have. Drawn the same way `output`
    already is, in red, because the reasoning is the same: a silent absence
    here is what let an approvable box go out with no readset row at all.
    """
    if row == "output":
        return Line("output", "output", "-o", [], warn=True,
                    note="not set — GenPipes writes into the current directory")
    if row == "resources":
        return Line("resources", "resources", "", [],
                    note="no step tuned — walltime, cpu and memory as the "
                         "inis set them")
    if required:
        return Line(row, row, FLAG_OF.get(row, ""), [], warn=True,
                    note="not set — required")
    return Line(row, row, FLAG_OF.get(row, ""), [], note="not set")


# Rows the gate does not draw, because something directly above already said
# it. `script` is the `-g` filename, which is the same word as the `bash
# <script>` line the box opens with; `name` is not a flag at all and used to sit
# in the flag table wearing a disclaimer saying so. Both are still parsed, still
# on the proposal, and still available to /modify -- this is about what the
# approval screen shows.
_GATE_HIDDEN = ("script", "name")


# Where each row sits when lines are put back in order: the invocation always
# first, then modify's row order, then every flag that has no row at all. Ties
# keep the order they arrived in, so a `-c` stack read out of a command is never
# silently resorted.
#
# The head is floated by its own flag rather than by being ROWS[0]. Making the
# pipeline the first ROW to get it drawn first also made it the row the /modify
# cursor opened on -- which is the most destructive change available, offered
# before anything else, in a panel whose listing order was arranged so that the
# harmless row came first. Display order and offer order are separate questions
# and this is the one place they are allowed to differ.
_RANK = {row: i for i, row in enumerate(ROWS)}


def _ordered(lines):
    return [line for _, line in sorted(
        (((-1 if line.head else _RANK.get(line.row, len(ROWS)), i), line)
         for i, line in enumerate(lines)), key=lambda pair: pair[0])]


def read(generated, name="", resources="", missing=()):
    """Tokenise a generated command into a Mirror.

    `name` adds the run's name as the first line. It is not a flag and says so
    -- the mirror is the place that disclaimer belongs, because it is the only
    screen where the name sits among things that ARE flags and could be mistaken
    for one.

    `resources` is override.summary() for this run, and gets a line of its own
    for the opposite reason. It is not a flag either, but unlike the name it
    genuinely changes what runs: the private override ini it summarises is the
    last entry on `-c` and wins over every GenPipes ini. It already appears in
    the `-c` line as a path; the point of a second line is that a path ending in
    `.override.ini` does not say a walltime was doubled.

    `missing` is gate.build_proposal's own verdict on what this command needs
    and does not have -- pass proposal["missing"] straight through. Rows named
    here are drawn as required-but-absent (see _absent) rather than silently
    skipped the way an unused `-p` is.
    """
    text = invocation(generated)
    if not text:
        return Mirror()

    tokens = _tokens(text)
    head = " ".join(tokens[:2]) if len(tokens) > 1 else (tokens[0] if tokens else "")
    groups = _groups(tokens[2:])

    # A value that itself starts with a dash and a letter is, by _FLAG's own
    # definition, a flag -- so finding one INSIDE another flag's values means
    # a flag boundary was missed and everything after it collapsed into one
    # field, the exact shape a stray backslash or an unhandled quoting style
    # produces. Read this as "this command could not be reliably tokenised"
    # rather than draw a mirror with `-c`, `-r` and `-s` sitting inside
    # `protocol`'s value. The caller's `mirror.read(...) or
    # mirror.from_slots(...)` falls back to the proposal's parsed slots, and
    # cli.py tells the person that happened rather than showing it silently.
    # Each value is checked WORD BY WORD, not just at its first character. A
    # flag can hide inside a quoted value -- `-t 'stringtie -s 1-5'` arrives
    # from shlex as one token, so matching only its start sees "stringtie" and
    # judges it sound, and the mirror then shows a protocol of `stringtie -s
    # 1-5` and no steps row at all. Splitting first means the buried `-s` is
    # found and the whole parse is refused, which is the point: a mirror that
    # looks like a faithful reconstruction and is not is worse than no mirror.
    if any(_FLAG.match(word)
           for _, values in groups
           for value in values
           for word in str(value).split()):
        return Mirror()

    seen = {}
    for flag, values in groups:
        row = _ROW_OF.get(flag)
        if row and row in seen:
            # The same row written twice -- `-c a.ini -c b.ini` is legal and
            # means one stack, so the values join rather than the second line
            # replacing the first and quietly losing an ini.
            seen[row].values.extend(values)
            continue
        line = Line(row, row or _LABEL.get(flag) or flag.lstrip("-").replace("-", " "),
                    flag, values)
        if row:
            seen[row] = line
        else:
            seen[f"\x00{len(seen)}"] = line

    # The one absence worth a line of its own. Every other missing flag means
    # the run does not use it -- a germline run has no `-p` and listing one
    # would be inventing a decision. A missing `-o` means something different:
    # GenPipes silently writes into whatever directory you happened to be in,
    # which is a real and frequently unwanted choice that no flag records. The
    # gate has always said this in red; the mirror keeps saying it, and gives
    # /modify a line to point at when someone selects `output` to fix it.
    #
    # Added before the lines are ordered, so it sits where `output` belongs
    # rather than trailing the flags that have no row at all -- an absence is
    # still that row, and it reads as an afterthought anywhere else.
    seen.setdefault("output", _absent("output"))
    for row in missing or ():
        seen.setdefault(row, _absent(row, required=True))
    if resources:
        seen["resources"] = Line("resources", "resources", "", [resources],
                                 note="last in the -c stack, so it wins")

    lines = [seen[row] for row in ROWS if row in seen]
    lines += [line for key, line in seen.items() if key.startswith("\x00")]

    if name:
        lines.insert(0, Line("name", "name", "", [name],
                             note="not a flag — only what you call it"))
    return Mirror(head, lines)


def from_slots(proposal, name="", resources=""):
    """A mirror built from the proposal's parsed slots. The fallback, only.

    This is the reconstruction read() exists to avoid, and it is here because
    the alternative is worse. `generated` is empty whenever the model wrote its
    generation and its submission in the same block -- gate.build_proposal then
    parses the slots straight out of the submission code and has nothing left
    over to quote. Drawing no mirror at all in that case would leave the gate
    showing `bash cmd.sh` and nothing else, which is precisely the blindness the
    mirror was built to end.

    So: prefer read(). Fall back to this. The difference is not cosmetic -- a
    line drawn here is only as complete as the slot parser, so a flag the parser
    does not know is simply absent, whereas read() would have shown it. Callers
    should try read() first and treat an empty Mirror as the signal to come
    here, which is what `mirror.read(...) or mirror.from_slots(...)` does.

    `missing` (proposal["missing"], set by gate.build_proposal) is read
    straight off the proposal rather than taken as a separate argument, unlike
    read() -- this function already has the whole proposal in hand.
    """
    values = (proposal or {}).get("slots") or {}
    missing = set((proposal or {}).get("missing") or ())
    pipeline = values.get("pipeline") or ""
    lines = []
    for row in ROWS:
        # Both are lines this function does not build: `name` is inserted at the
        # end, and `pipeline` is the head, inserted by Mirror itself. Letting the
        # loop emit either produces a second line with the same row, which the
        # panel reads as a second checkbox and which shifts every index under it.
        if row in ("name", "pipeline"):
            continue
        if row == "resources":
            lines.append(Line("resources", "resources", "", [resources],
                              note="last in the -c stack, so it wins")
                         if resources else _absent("resources"))
            continue
        raw = values.get(SLOT_OF.get(row, row))
        if raw in (None, "", [], ()):
            if row == "output":
                lines.append(_absent("output"))
            elif row in missing:
                lines.append(_absent(row, required=True))
            continue
        got = ([str(v) for v in raw] if isinstance(raw, (list, tuple))
               else [str(raw)])
        lines.append(Line(row, row, FLAG_OF.get(row, ""),
                          list(dict.fromkeys(got))))

    if name:
        lines.insert(0, Line("name", "name", "", [name],
                             note="not a flag — only what you call it"))
    return Mirror(f"genpipes {pipeline}".strip() if pipeline else "", lines)
