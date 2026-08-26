"""Terminal display for GenpipeA1.

Two layers, deliberately separated:

  parse(message)  -> a list of plain dicts describing what is in the message.
                     Knows nothing about terminals, colours, or printing.
  render(message) -> parses, then prints to the terminal with ANSI colours.

The separation is the point. If a web interface is built later it imports
parse(), gets the same dicts, and writes its own renderer; the logic that
understands what an agent message contains is not written twice. Nothing here is
load-bearing: if this module breaks, run() can fall back to Biomni's
pretty_print and lose only appearance, never behaviour.

What is shown, and what is folded away
--------------------------------------
The transcript is a conversation, so it is drawn as one: the line you typed,
once, beside a `>`, and the reply as plain prose underneath. No speaker labels.
Nothing is labelled PBOURQUE or ASSISTANT, for the same reason nothing in a
chat window is -- there are two participants, they alternate, and naming them
every time is furniture.

The agent's working -- the commands it runs, the output it reads, its
connective prose -- is folded away by default and kept, not discarded. It is
the equivalent of a chain of thought: worth being able to see, not worth
reading every time. `/verbose` unfolds it, permanently, and unfolds what has
already scrolled past as well. When it is folded, one dim line says how many
steps were taken, so the fold is visible rather than silent.

What is NEVER folded is the plan -- the model's own checklist of the stages it
is about to work through. That is the one part of the working that answers
"what is it doing, and how far along is it?", which is exactly the question the
fold leaves you with. It is drawn as a single block that repaints in place as
stages complete, so the progression happens on one set of lines instead of
reprinting the whole list on every turn. See _draw_plan.

Unfolded, the hierarchy is:

  GATE      heavy red box. The one moment the run stops and needs a human.
  GENERATE  amber. Writing the pipeline script.        } one <execute> block,
  SUBMIT    amber. Putting it on the scheduler.        } labelled by what it
  SCHEDULER amber. Asking Slurm or GenPipes how it is. } is actually doing --
  CODE      amber. Anything else about to run.         } see _code_label
  TERMINAL  bright label, grey body. The machine talking back -- present, quiet.
  answered  one dim line. The receipt for a choice panel.
  note      thin rule, grey. The model's connective prose. Present, but quiet.

Three things are never drawn even unfolded -- a documentation lookup and its
output, and the model's checklist. See render() for why.

The gate is the one thing that is never folded. It is not the agent thinking;
it is the agent stopping.
"""

import datetime
import getpass
import os
import random
import re
import sys
import textwrap
import unicodedata

# ALIASED, both of them, because this module already defines a `gate` -- the
# renderer that draws the approval box -- and a plain `from . import gate`
# is silently overwritten by that def further down the file. The import wins
# at import time and loses the moment the def is executed, so every later
# reference resolves to the renderer and any attribute lookup on it fails
# quietly inside a try. Same shape as agent.py's `interrupt as interrupting`.
from . import capabilities as capability_table
from . import gate as gate_rules
from . import mirror
from . import modify
# Stdlib-only, like this module: the status vocabulary lives with the registry
# that writes it, so a renderer cannot invent a fifth outcome the store has
# never heard of.
from . import runs
# The palette, and the rule for choosing one. Stdlib-only like this module.
from . import theme

# ---------------------------------------------------------------------------
# ANSI escape codes. \033[<n>m sets an attribute; \033[0m clears everything.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# HOW THIS PALETTE SURVIVES A TERMINAL IT KNOWS NOTHING ABOUT.
#
# The values live in theme.py, which owns the choosing and states the whole
# argument -- read that first. What is here is the BINDING: one name per
# semantic role, resolved once, so that sixty call sites saying GREY do not
# have to learn that there are now two greys and a rule for picking one.
#
# The rules that survive from the single-palette version, unchanged, because
# they are what make the rest a readability question rather than a
# correctness one:
#
#   1. COLOUR IS NEVER THE ONLY CARRIER. Every state has a glyph (see _MARKS)
#      and a word ("waiting for approval", "failed"). Strip every escape
#      sequence and the screen still says the same things.
#
#   2. EMPHASIS IS A WEIGHT, NOT A HUE. WHITE is BOLD in every palette,
#      including the two tuned ones. It is the one role a wrongly-guessed
#      theme could render invisible rather than merely faint, so it is never
#      guessed at -- bold contrasts with whatever is behind it because that
#      is what the terminal's own foreground colour means. The blast radius
#      of a wrong theme is confined to furniture.
#
#   3. NO_COLOR AND TERM=dumb ARE HONOURED. See theme.colour_wanted().
#
# WHAT CHANGED. There are two greys now and they are real colours rather than
# attributes. GREY is `muted` -- the readable quiet, what a pipeline name or
# a hint is set in. DIM is `faint` -- furniture: rules, box edges, the
# receipt line under a choice. On the tuned palettes both are RGB values
# measured against their background; on SAFE they are exactly what they were
# before (90 and SGR 2).
#
# AND SGR 2 IS STILL AVAILABLE, as FAINT. A handful of places want a hue
# ATTENUATED rather than replaced -- a quiet green frame, the red of a
# cancelled bar segment -- and `{GREEN}{DIM}` cannot mean that once DIM is
# itself a colour, because the last sequence wins. Those sites say
# `{GREEN}{FAINT}` and keep their hue.
# ---------------------------------------------------------------------------

_PALETTE = theme.palette()

# Which of theme.py's four answers this session got: "dark", "light", "safe"
# or "none". Printed by /where, asserted by the suites, and the one thing
# somebody debugging "why is my terminal beige" needs to see.
THEME = _PALETTE["theme"]

_COLOUR = THEME != theme.NONE


def _sgr(code):
    return code if _COLOUR else ""


RESET = _sgr("\033[0m")
BOLD = _sgr("\033[1m")
REVERSE = _sgr("\033[7m")
UNDER = _sgr("\033[4m")
# The SGR 2 attribute itself. Not a role -- see the note above.
FAINT = _sgr("\033[2m")

RED = _PALETTE["error"]
AMBER = _PALETTE["warning"]
GREEN = _PALETTE["success"]
GREY = _PALETTE["muted"]
DIM = _PALETTE["faint"]
SECONDARY = _PALETTE["secondary"]
FOCUS = _PALETTE["focus"]
# The old name for what is now the secondary role. It named a hue (cyan) that
# was retired for being illegible on white; the role it was reaching for --
# "quiet, but a different quiet from grey" -- is what SECONDARY is. Kept as an
# alias rather than deleted so nothing that still says CYAN silently loses its
# colour.
CYAN = SECONDARY

# The helix's four depth roles. Named rather than expressed as shades of GREEN
# because they are a ramp -- front strand, turn, back strand, rung -- and the
# ramp has to run the OTHER WAY on a light background, where further away is
# lighter rather than darker. See theme.py's tables.
DNA_FG = _PALETTE["dna_fg"]
DNA_MID = _PALETTE["dna_mid"]
DNA_BG = _PALETTE["dna_bg"]
DNA_RUNG = _PALETTE["dna_rung"]

# Emphasis, and deliberately not a colour. See rule 2 above; the name is left
# alone because sixty call sites say WHITE where they mean "the emphasised
# one", and renaming them would be a large diff over working code to relabel a
# decision that is stated here.
WHITE = BOLD

# The names this module rebinds when the palette changes. Listed once so
# retheme() cannot fall out of step with the block above by forgetting one.
_THEMED = ("THEME", "_COLOUR", "RESET", "BOLD", "REVERSE", "UNDER", "FAINT",
           "RED", "AMBER", "GREEN", "GREY", "DIM", "SECONDARY", "FOCUS",
           "CYAN", "DNA_FG", "DNA_MID", "DNA_BG", "DNA_RUNG")


def retheme(env=None):
    """Recompute every colour from `env` (default: os.environ). Returns THEME.

    The constants above are read at call time by everything in this module and
    reached as `display.GREY` from everywhere else -- nothing from-imports them
    -- so rebinding the module globals is enough to restyle the whole app.

    Exists for two callers and no others: the suites, which have to render the
    same screen under dark, light and NO_COLOR without three subprocesses; and
    anything that changes the environment after import. It is not a feature of
    the product and there is no command for it -- the palette is chosen once,
    from the environment, at startup.
    """
    pal = theme.palette(env)
    g = globals()
    g["_PALETTE"] = pal
    g["THEME"] = pal["theme"]
    g["_COLOUR"] = pal["theme"] != theme.NONE
    on = g["_COLOUR"]
    g["RESET"] = "\033[0m" if on else ""
    g["BOLD"] = "\033[1m" if on else ""
    g["REVERSE"] = "\033[7m" if on else ""
    g["UNDER"] = "\033[4m" if on else ""
    g["FAINT"] = "\033[2m" if on else ""
    g["RED"] = pal["error"]
    g["AMBER"] = pal["warning"]
    g["GREEN"] = pal["success"]
    g["GREY"] = pal["muted"]
    g["DIM"] = pal["faint"]
    g["SECONDARY"] = g["CYAN"] = pal["secondary"]
    g["FOCUS"] = pal["focus"]
    g["DNA_FG"] = pal["dna_fg"]
    g["DNA_MID"] = pal["dna_mid"]
    g["DNA_BG"] = pal["dna_bg"]
    g["DNA_RUNG"] = pal["dna_rung"]
    g["WHITE"] = g["BOLD"]
    return g["THEME"]

WIDTH = 74

# ---------------------------------------------------------------------------
# How wide a line is, and how many rows it will really take.
#
# Three places in this app repaint a block in place: the prompt box, the choice
# panel, and the plan checklist. All three worked the same way and were wrong
# the same way -- they counted the LINES they had printed and walked the cursor
# back up that many ROWS. Those are only the same number while every line fits
# the window. One line too long wraps onto a second row, the walk-up comes up
# short, and the block drifts a row further down the screen on every repaint:
# the reported symptom was a prompt that printed `> /m`, `> /mo`, `> /mod` down
# the screen instead of editing one line in place.
#
# So the arithmetic is done here, once, and the three callers share it:
# cells() is what a string costs on screen, fit() guarantees a line cannot
# wrap, and rows() is the honest row count if one does anyway.
# ---------------------------------------------------------------------------

_ANSI = re.compile(r"\033\[[0-9;?]*[a-zA-Z]")


def cells(text):
    """Columns `text` occupies when printed.

    Not len(): colour codes are bytes that take no space, a combining mark
    hangs off the character before it, and the box-drawing and CJK ranges are
    two columns wide. len() over-counts the first two and under-counts the
    last, and each error is a wrapped line somebody has to debug from a
    screenshot.
    """
    n = 0
    for ch in _ANSI.sub("", text):
        if unicodedata.combining(ch) or unicodedata.category(ch)[0] == "C":
            continue
        n += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return n


def fit(text, cols):
    """`text` cut to at most `cols` columns, colour preserved, ellipsis added.

    Escape sequences are copied through rather than counted, so a truncated
    line keeps the colour it was written in -- and a RESET is appended, because
    cutting a string mid-colour leaves the rest of the screen tinted.
    """
    if cols <= 0:
        return ""
    if cells(text) <= cols:
        return text
    out, n, i = [], 0, 0
    while i < len(text):
        m = _ANSI.match(text, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        ch = text[i]
        i += 1
        if unicodedata.combining(ch) or unicodedata.category(ch)[0] == "C":
            out.append(ch)
            continue
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if n + w > cols - 1:          # one column held back for the ellipsis
            break
        out.append(ch)
        n += w
    return "".join(out) + "…" + RESET


def pad(text, width):
    """`text` cut to `width` columns and padded out to exactly that many.

    Not f"{text:<width}", which pads by len() -- and len() counts the escape
    sequences fit() may have just appended, so a truncated cell comes out
    several columns short and every column to its right steps left.
    """
    text = fit(text, width)
    return text + " " * max(0, width - cells(text))


def row_count(text, cols):
    """Terminal rows a printed line occupies, wrapping included.

    Not named rows(): several functions in this module already use `rows` as a
    local for a list of runs, and a helper that silently disappears behind a
    local in half the file is worse than a longer name.
    """
    if cols <= 0:
        return 1
    return max(1, -(-cells(text) // cols))


def terminal_rows():
    """The real height of the window, in rows.

    Same reasoning as terminal_cols() below, and needed for the same class of
    bug from the other axis: a block taller than the window cannot be repainted
    by walking the cursor up its own height, because the rows it wants to walk
    back to have already scrolled off. Anything that repaints in place has to
    know when to stop growing.
    """
    for stream in (sys.__stdout__, sys.__stderr__, sys.__stdin__):
        try:
            return os.get_terminal_size(stream.fileno()).lines
        except (AttributeError, ValueError, OSError):
            continue
    try:
        return max(1, int(os.environ.get("LINES", "24")))
    except ValueError:
        return 24


# ---------------------------------------------------------------------------
# THE LAST CANONICAL SURFACE, kept so it can be drawn again at a new width.
#
# WHY THIS EXISTS, AND WHAT IT HONESTLY CANNOT DO. Everything this application
# prints is ordinary scrollback. When the window is resized the terminal
# emulator reflows what is already on it, and it reflows it as text: a line
# this module hard-wrapped to fit 100 columns is soft-wrapped again at 80, and
# the overflow restarts at COLUMN ZERO -- left of and underneath the gutter it
# was drawn beside. That is what breaks a diagnosis panel on resize, and no
# amount of care here prevents it, because the bytes have already been written
# to a stream this process cannot edit. Rewriting printed history would mean
# owning the screen -- an alternate-screen TUI with a live region per surface --
# which is a different application.
#
# What CAN be done is draw it again, now, at the width the window is now. This
# memo is what makes that possible: the renderer and the arguments it was
# called with, so /redraw is the SAME function over the SAME data. It cannot
# call a model, re-read a log or ask the scheduler anything, because it does
# not have the code to -- it holds a display function and a tuple.
#
# One slot, not a history: the question somebody has after resizing is about
# the thing they were just looking at.
_LAST = None


def canonical(fn):
    """Mark a renderer as a complete user-facing surface, and remember the last.

    The same idea capabilities.Capability.renders records for the model, on the
    display side: these are the screens somebody reads and acts from, as
    opposed to a notice, a prompt or a line of progress.
    """
    def drawn(*args, **kwargs):
        global _LAST
        _LAST = (fn, args, kwargs)
        return fn(*args, **kwargs)

    drawn.__name__ = fn.__name__
    drawn.__doc__ = fn.__doc__
    drawn.__wrapped__ = fn
    return drawn


def last_surface():
    """(name, callable) for the last canonical surface drawn, or (None, None)."""
    if not _LAST:
        return None, None
    fn, args, kwargs = _LAST
    return fn.__name__, lambda: fn(*args, **kwargs)


def forget_surface():
    """Drop the memo. For tests, and for /new."""
    global _LAST
    _LAST = None


def terminal_cols():
    """The real width of the window, in columns.

    Deliberately not shutil.get_terminal_size, which consults $COLUMNS first
    and only falls back to asking the terminal. A stale COLUMNS -- exported
    once by a shell that never updated it, which is ordinary on a login node --
    then makes every box in this app draw wider than the window it is drawn in,
    and every one of them wrap. The terminal is asked directly; $COLUMNS is a
    last resort for when there is no terminal to ask.
    """
    for stream in (sys.__stdout__, sys.__stderr__, sys.__stdin__):
        try:
            return os.get_terminal_size(stream.fileno()).columns
        except (AttributeError, ValueError, OSError):
            continue
    try:
        return max(1, int(os.environ.get("COLUMNS", "80")))
    except ValueError:
        return 80


# ---------------------------------------------------------------------------
# The startup banner. Written for a GenPipes user, not a builder: it answers
# "can I trust this with my allocation?" before anything else, and shows where
# the human sits in the pipeline.
#
# Two columns, because the two things it has to say are different in kind. The
# left is identity -- who you are, what this is, which model is behind it, where
# it lives on disk. The right is orientation -- what to type, and the one rule
# that makes this tool different from talking to a chatbot with a shell.
#
# Green throughout, and a helix rather than a logo: the whole point of the tool
# is that the thing on the other end knows biology, and the first screen may as
# well say so.
# ---------------------------------------------------------------------------

VERSION = "v0"

# ---------------------------------------------------------------------------
# THE HELIX. One continuous right-handed B-DNA double helix, side-on.
#
# The art below is a literal rather than something computed at startup -- it is
# greppable, it diffs, and it cannot drift -- but it was GENERATED, and these
# are the parameters, so that a later change is a re-render rather than a
# freehand edit:
#
#     rows 11    period 10 rows/turn    amplitude 6 columns    phase +pi/2
#     x_A(r) = cx - A sin(th)      depth_A =  cos(th)
#     x_B(r) = cx + A sin(th)      depth_B = -cos(th)      th = pi/2 + 2 pi r/P
#
# WHY THE MINUS SIGN ON STRAND A, which is the entire biological content of
# this block. With `+`, the strand that is in FRONT sweeps left-to-right as you
# read DOWN the page -- a `\` front diagonal, which is a LEFT-handed helix.
# B-DNA is right-handed, and in every side view of it the front-facing segments
# run lower-left to upper-right. Reading downwards, the front strand moves
# right to left. That is what the sign buys.
#
# The phase offset starts the figure at maximum separation, which puts the two
# crossings at rows 2.5 and 7.5 -- between sampled rows, so the strands never
# land in the same column and the twist is legible at every row. One full turn,
# two crossings, and front-ness passes from one strand to the other at each of
# them and never passes back: strand B is in front from row 1 to row 4 while
# sweeping column 11 to column 1, then strand A takes over on the same
# trajectory. Neither strand ever reverses.
#
# DEPTH IS DRAWN TWICE, in density and in colour, and that is deliberate: the
# same rule the status glyphs follow (see _MARKS). Stripped of every escape
# sequence the ramp is still there, because a light-shaded block IS further
# away than a dark-shaded one to anybody who has ever seen a terminal.
#
#     ▓  front strand      ▒  the turn      ░  back strand      ─  base pairs
#
# A base pair is a BOND, not a bead, so the rung glyph is a box-drawing
# horizontal rule: adjacent cells join, and the rung renders as one unbroken
# line from backbone to backbone. `·` sampled the same span as a row of
# points and read as dotted rather than as a pair held together; ╌ was tried
# against it and is the same figure with half the ink, but the solid stroke
# is the one that reads as a bond at this size.
#
# The rungs stop where the strands come within three columns of each other,
# which is not a rendering compromise: at the crossing a base pair is edge-on
# to the viewer and there is nothing to draw.
# ---------------------------------------------------------------------------
_HELIX = [
    "▒───────────▒",
    " ░─────────▓ ",
    "    ░───▓    ",
    "    ▓───░    ",
    " ▓─────────░ ",
    "▒───────────▒",
    " ░─────────▓ ",
    "    ░───▓    ",
    "    ▓───░    ",
    " ▓─────────░ ",
    "▒───────────▒",
]

# 13 columns. Asserted rather than assumed: _left_column centres this against
# _LEFT_W, and a row that is one column wider than its neighbours centres half
# a column off and makes the whole figure look bent.
HELIX_W = 13


def _strand():
    """glyph -> colour, read at call time so retheme() reaches the logo too.

    Four roles, not four shades of GREEN spelled out here, because the ramp
    runs the OTHER WAY on a light background -- further away is lighter there
    and darker on black. theme.py holds both directions; this only says which
    role each glyph is.
    """
    return {"\u2593": DNA_FG, "\u2592": DNA_MID, "\u2591": DNA_BG, "\u2500": DNA_RUNG}


def _helix():
    """The helix, coloured per glyph."""
    strand = _strand()
    return ["".join(f"{strand[c]}{c}{RESET}" if c in strand else c for c in row)
            for row in _HELIX]


def who():
    """What to call the person at the keyboard.

    GENPIPE_USER is what they told us to call them (asked for once at first
    launch, changed with /user, persisted in .env like the API key). The Unix
    username is the fallback: it is nearly always right and always available, so
    a fresh install is not addressed as "you" while it waits to be introduced.

    Read from the environment at every call rather than cached, so /user takes
    effect on the next line of the conversation instead of the next launch.
    """
    name = (os.environ.get("GENPIPE_USER") or "").strip()
    if not name:
        try:
            name = os.environ.get("USER") or getpass.getuser()
        except Exception:              # no passwd entry for this uid
            name = ""
    return (name or "you")[:24]


def _tilde(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home) else path


def _pad(cell, w):
    """Fit a styled string to exactly w visible columns.

    The truncating branch is a backstop, not a feature: every line the banner
    builds is meant to fit, but one that didn't would push the box's right-hand
    border out and make the whole frame look broken. Dropping the colour when
    truncating avoids slicing through the middle of an escape sequence.
    """
    n = _vis_len(cell)
    if n <= w:
        return cell + " " * (w - n)
    return re.sub(r"\033\[[0-9;]*[A-Za-z]", "", cell)[:max(0, w - 1)] + "…"


def _vis_len(s):
    return len(re.sub(r"\033\[[0-9;]*[A-Za-z]", "", s))


def _wrap(text, w, style=GREY, indent=""):
    body = textwrap.wrap(text, max(20, w - len(indent))) or [""]
    return [f"{indent}{style}{line}{RESET}" for line in body]


def _identity(source, model):
    who = source and model and f"{source} \u00b7 {model}"
    return who or "no model configured yet"


# The wordmark, drawn rather than set in type. "GenPipes assistant" as one bold
# line was the same size as every other line in the box, so the product's own
# name carried no more weight than the path underneath it.
#
# Box-drawing glyphs rather than a figlet font: they sit on the same grid as the
# frame around them, and they stay legible in a terminal that would render an
# ASCII-art font as noise. 22 columns wide, which fits the 32-column left half
# with room to spare.
_WORDMARK = [
    "╔═╗╔═╗╔╗╔╔═╗╦╔═╗╔═╗╔═╗",
    "║ ╦║╣ ║║║╠═╝║╠═╝║╣ ╚═╗",
    "╚═╝╚═╝╝╚╝╩  ╩╩  ╚═╝╚═╝",
]

# The identity half's width. A constant rather than a local in banner() because
# _left_column centres the wordmark against it, and a centre computed from a
# number that lives somewhere else is one edit away from being off by two.
_LEFT_W = 32


def welcome():
    """The invitation, printed last -- immediately above the prompt.

    The banner is a reference card and it is at the top of the scrollback by the
    time anybody types. What sat here instead was a roll-call of held runs, which
    is the least useful thing that could occupy the line nearest the cursor: nine
    names from a fortnight of testing, none of them news, none of them anything
    you were about to act on.

    So the nearest line is a question, and nothing else.

    It used to carry two more things: a sentence restating what an answer looks
    like, and a row of three commands. Both were already on the banner a few
    lines above -- "Ask naturally" with a worked example, and /list and
    /check all under "Keep track" -- so this was the same guidance a second
    time, in the one place where brevity is worth most. A question with an
    answer printed underneath it is not an invitation.
    """
    print()
    print(f"  {BOLD}What can I help you with today?{RESET}")
    print()


def _centre(cell, w):
    """Leading spaces that centre a styled string in w columns.

    Measured on the visible text, so the escape sequences that colour the
    wordmark do not push it off centre -- which is exactly what a plain
    str.center() would do here.
    """
    return " " * max(0, (w - _vis_len(cell)) // 2) + cell


def _left_column(user, source, model, path, returning=True):
    """The identity half: who you are, what this is.

    The model and the working directory used to close this column and now live
    on the right. They are reference, not identity, and while they sat here the
    left half was the taller of the two -- so the frame padded the right half
    with four blank rows to match, directly under "Once it's running", which is
    what made the box look badly balanced once the wordmark grew.
    """
    lines = [""]
    # "Welcome back" on a screen somebody is seeing for the first time is a
    # small lie, and it is the first sentence the product says. GENPIPE_USER is
    # only set once they have been introduced -- _require_name asks below this
    # banner -- so its absence is exactly "we have not met".
    lines.append(f"{BOLD}{'Welcome back' if returning else 'Welcome'}, "
                 f"{user}{RESET}")
    lines.append("")
    lines += [_centre(f"{BOLD}{GREEN}{row}{RESET}", _LEFT_W) for row in _WORDMARK]
    # The name in real letters under the glyphs that draw it. The wordmark is a
    # picture: it cannot be grepped out of a screenshot, pasted into an issue,
    # or read aloud by anything. Whatever the mark looks like, the product has
    # to say its own name in text somewhere.
    # "GenPipes assistant", not "assistant". The full name used to reach the
    # screen through the right column's opening sentence ("...describe the
    # GenPipes run you want"); that sentence has gone, and with it the only
    # plain-text occurrence of the product's own name -- leaving it legible
    # solely as box-drawing glyphs, which is exactly what the paragraph above
    # says must never be the case.
    lines.append(_centre(
        f"{GREY}GenPipes assistant{RESET}  {DIM}{VERSION}{RESET}",
        _LEFT_W))
    lines.append("")
    # Centred with the wordmark, not on its old fixed indent: the two are one
    # lockup, and a mark whose halves disagree about their centre line reads as
    # a mistake rather than as a choice.
    lines += [_centre(row, _LEFT_W) for row in _helix()]
    return lines


# How far a command runs before its description, and a metadata label before
# its value. Constants because both columns must start in ONE place: a
# description that begins at a different offset per row reads as two lists
# rather than one.
_CMD_W = 13
_META_W = 9


def _right_column(w, source=None, model=None, path=None):
    """The onboarding half: what to type, and what this session is.

    THREE GROUPS, ONE RULE, NO RULES BETWEEN THEM. This used to carry two
    horizontal separators and two blocks of instruction -- how to open the
    command menu, how to autocomplete, how to get the list back, and a second
    heading listing four monitoring verbs. That is a manual, and a manual is the
    wrong thing to hand somebody before they have typed anything: the command
    list is one keystroke away by design, and the monitoring verbs are offered
    by name at the moment each one applies.

    So the rule is spent once, on the only boundary here that is not
    onboarding -- what this session is running. Everything above it is
    separated by whitespace, which is what whitespace is for.
    """
    lines = [""]
    lines.append(f"{BOLD}{WHITE}Getting started{RESET}")
    lines.append("")

    # The example is the most important line on this screen and is deliberately
    # NOT green: it is a thing to say, not a command to type, and colouring it
    # like the commands below would file it under the same idea.
    #
    # AND IT IS NOT EMPHASISED EITHER. WHITE is BOLD (see the palette note
    # above), so setting the example in it put a second bold line directly
    # under a bold heading and a few rows below the wordmark -- three claims on
    # the loudest weight this terminal has, competing on the one screen where
    # the branding is supposed to be the thing you see first. What the example
    # is, is helper text: an illustration of the heading above it, exactly like
    # "type / to browse commands" is an illustration of the heading above that.
    # So it gets the same treatment as that line -- GREY, the readable quiet --
    # which leaves the headings and the wordmark as the only emphasised things
    # on the left half of the screen and costs the example nothing in
    # legibility (`muted` clears 4.5:1 on every background theme.py tunes for).
    lines.append(f"{WHITE}Ask naturally{RESET}")
    lines.append(f"  {GREY}run dnaseq germline_snv on my readset, "
                 f"all steps{RESET}")
    lines.append("")

    lines.append(f"{WHITE}Keep track{RESET}")
    # Hand-padded rather than sent through _wrap(): textwrap counts escape
    # sequences as characters, so a coloured command inside it either clips or
    # breaks the wrap. The padding is measured on the plain text for the same
    # reason.
    # Read from _ACTION_TEXT where the command is one of the shared verbs, so
    # the first screen anybody sees teaches the same words every later screen
    # uses. "/check all" is its own form and gets its own line: "refresh
    # statuses" described a scheduler round-trip to somebody who wanted to know
    # how their runs were doing, which is the implementation talking.
    for cmd, what in (("/list", _ACTION_TEXT["/list"]),
                      ("/check all", "see how every run is doing")):
        lines.append(f"  {GREEN}{cmd}{RESET}"
                     f"{' ' * max(1, _CMD_W - len(cmd))}{GREY}{what}{RESET}")
    lines.append("")

    lines.append(f"{WHITE}Need something else?{RESET}")
    lines.append(f"  {GREY}type {RESET}{GREEN}/{RESET}"
                 f"{GREY} to browse commands{RESET}")

    # What this session is actually using. It closes the reference half rather
    # than the identity half because that is what it is -- something you look up
    # -- and because putting it here is what makes the two columns the same
    # height instead of padding one to match the other.
    lines.append("")
    lines.append(f"{DIM}{chr(0x2500) * w}{RESET}")
    lines.append("")
    # Both read from what this session settled on, never hardcoded. On a first
    # launch there is no key yet and no model chosen, and saying so is the
    # honest answer rather than naming a default nobody picked.
    for label, value in (("Model", model or "not configured yet"),
                         ("Project", _tilde(path or ""))):
        lines.append(f"{DIM}{label}{RESET}"
                     f"{' ' * max(1, _META_W - len(label))}{GREY}{value}{RESET}")
    # The approval promise used to close this screen. It says more where it is
    # demonstrated -- at the gate, holding a real submission -- than as a claim
    # made to someone who has not yet typed anything.
    return lines


def banner(source=None, model=None):
    """Print the startup banner.

    source/model are what the session will actually use -- passed in rather than
    read here, because on a first launch there is no key yet and the honest
    answer is "not decided": the key prompt appears below this banner and picks
    them. ready() states them again once they're settled.
    """
    user = who()
    returning = bool((os.environ.get("GENPIPE_USER") or "").strip())
    # The checkout, not the package directory: what someone reads off the
    # banner is the thing they would cd into or git pull.
    path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # terminal_cols(), not shutil: shutil consults $COLUMNS first, and a
    # $COLUMNS exported once by a login shell and never updated is exactly the
    # stale width this whole module avoids. The banner was the last reader of
    # it, which is why it was the one surface that could be drawn at the width
    # the window used to be.
    cols = terminal_cols()

    total = min(cols - 2, 104)
    left_w = _LEFT_W
    right_w = total - left_w - 7

    # One line in the right column can't be reflowed -- the example command --
    # and 49 columns is what it needs. Below that the two-column layout is being
    # forced, so stack instead: same content, no box.
    if right_w < 49:
        print()
        for line in _left_column(user, source, model, path, returning):
            print(f"  {line}" if line else "")
        for line in _right_column(min(cols - 4, 60), source, model, path):
            print(f"  {line}" if line else "")
        print()
        return

    left = _left_column(user, source, model, path, returning)
    right = _right_column(right_w, source, model, path)
    rows = max(len(left), len(right))
    left += [""] * (rows - len(left))
    right += [""] * (rows - len(right))

    edge = f"{GREEN}{FAINT}"
    print()
    print(f" {edge}\u256d{'\u2500' * (left_w + 2)}\u252c{'\u2500' * (right_w + 2)}\u256e{RESET}")
    for l, r in zip(left, right):
        print(f" {edge}\u2502{RESET} {_pad(l, left_w)} {edge}\u2502{RESET} "
              f"{_pad(r, right_w)} {edge}\u2502{RESET}")
    print(f" {edge}\u2570{'\u2500' * (left_w + 2)}\u2534{'\u2500' * (right_w + 2)}\u256f{RESET}")
    print()


def help_text(commands):
    """Print the command reference, grouped by where you are in a run's life.

    Takes the command table rather than owning a copy of it, so the menu the
    prompt completes against, the dispatcher, and this list cannot drift apart --
    there is one table, in genpipe/cli.py.

    Grouped rather than alphabetical because the list is now long enough that a
    flat version stops being a reference and becomes a wall. The groups follow
    the order things actually happen in, so the shape of the workflow is legible
    from the help itself: you decide, then you watch, then you fix.
    """
    width = max(len(f"/{n} {a}".rstrip()) for n, a, _, _ in commands) + 3
    order, groups = [], {}
    for name, args, desc, group in commands:
        if group not in groups:
            groups[group] = []
            order.append(group)
        groups[group].append((name, args, desc))

    print()
    for group in order:
        print(f"  {DIM}{group}{RESET}")
        for name, args, desc in groups[group]:
            left = f"/{name}" + (f" {args}" if args else "")
            print(f"    {GREEN}/{name}{RESET}{DIM}{f' {args}' if args else ''}{RESET}"
                  f"{' ' * (width - len(left))}{GREY}{desc}{RESET}")
        print()
    print(f"  {DIM}Anything that isn't a /command is talk. It goes to the agent, "
          f"which keeps the thread{RESET}")
    print(f"  {DIM}and asks you when it needs something. A run gets its name when "
          f"it reaches the gate;{RESET}")
    print(f"  {DIM}that name is how you approve it, and how you check on it days "
          f"later.{RESET}")
    print()
    # WHAT THE HELP TEACHES AND WHAT THE PARSER ACCEPTS ARE NOT THE SAME SET,
    # and this line is where the difference is admitted rather than advertised
    # in every row. The signatures above used to read `/modify <name> [change]`,
    # `/reject <name> [why...]`, `/diagnose <name> [question]` -- three
    # different notations for one idea, spending the widest column on the
    # option nobody needs to start with. The flexibility is real and is not
    # being removed; it is just not the first thing somebody has to parse.
    print(f"  {DIM}Most of these take a sentence after the name too — "
          f"{RESET}{GREY}/modify run-1 use steps 1-5{RESET}"
          f"{DIM}, {RESET}{GREY}/diagnose run-1 why did it stall{RESET}{DIM}.{RESET}")
    print()

# ---------------------------------------------------------------------------
# Layer 1 -- the parser. Text in, structured events out. No printing.
# ---------------------------------------------------------------------------

_ASK_ONLY = re.compile(r"^ask\s*\(.*\)$", re.DOTALL)
_PROPOSE_ONLY = re.compile(r"^propose_submission\s*\(.*\)$", re.DOTALL)

# One checklist line: "1. [ ] do a thing", "2. [x] did a thing".
#
# Defined once because two things must agree about it exactly: what the plan
# block CLAIMS, and what the prose paths REMOVE. When they disagreed -- the
# solution path kept lines the plan block had already taken -- the checklist
# printed twice on every turn, which is what parked agent.PLANS. A single
# pattern is what stops that from being reintroduced by editing one of a pair.
#
# Only [ ], [x], [✓] and [v] are marks. A failed step has no mark of its own,
# so agent.PLAN_PROTOCOL tells the model to describe failures in prose and
# amend the list -- see the note at agent.py's _MARKS discussion. Adding [✗]
# here means adding it there in the same change.
_PLAN_LINE = re.compile(r"^[ \t]*\d+\.\s*\[([ x✓v]?)\]\s*(.+)$", re.M | re.I)


def _strip_plan(text):
    """`text` without its checklist lines.

    The plan block owns those lines: parse() lifts them into a "plan" event
    that _draw_plan repaints in place, so any path that also prints the text
    they came from has to take them out or the reader sees the same list twice.
    """
    return _PLAN_LINE.sub("", text)

# Fingerprints of the user-role messages nobody typed. Three are written by this
# codebase (the observation wrapper, the gate's rejection note, agent.py's
# NUDGE) and one by biomni's generate node, which corrects a model that replied
# without a tag. Matched by content because that is all a message carries -- the
# API has no notion of "the graph said this", and a role is the only field there
# is. Kept here rather than imported so this module stays stdlib-only.
_NUDGE = "[continue]"                                   # agent.NUDGE
_CONTEXT_MARK = "--- context for you, not typed by the user ---"  # intake.CONTEXT_MARK

_CORRECTION = re.compile(r"Each response must include thinking process")
_ANSWER = re.compile(r"The user (?:answered|declined)")

_MACHINERY = re.compile(
    r"<observation>"
    r"|" + re.escape(_NUDGE) +
    r"|The proposed submission was not approved"
    r"|Each response must include thinking process"
    r"|Execution terminated due to repeated parsing errors")


def _is_help_only(code):
    """Is this block nothing but a request for documentation?

    Split on the shell's own separators, because the real shape of these is
    `module load mugqic/genpipes/6.1.1 && genpipes rnaseq_light --help`: a setup
    command and a question. Every part has to be one or the other for the block to
    count, so a `--help` bolted onto something that also does work stays visible.
    """
    parts = [p.strip() for p in re.split(r"&&|\|\||;|\n", code) if p.strip()]
    parts = [p for p in parts if not p.startswith("#")]
    if not parts:
        return False
    for part in parts:
        if part.startswith(("module load", "module purge", "export ", "set ",
                            "cd ", "source ")):
            continue
        if re.search(r"(?:^|\s)(?:--help|-h)(?:\s|$)", part):
            continue
        return False
    return True


# Commands that only look. Deliberately a small, closed list of the ones whose
# whole purpose is to show you something: nothing here creates, moves, deletes
# or submits, so a block made only of these cannot have changed anything.
_READ_ONLY = ("ls", "cat", "head", "tail", "find", "wc", "file", "stat",
              "readlink", "realpath", "du", "df", "pwd", "hostname", "echo")


def _is_read_only(code):
    """Is this block nothing but looking at the filesystem?

    Same shape as _is_help_only, and for the same reason: the real form is
    `module load ... && ls /some/path`, a setup command and a look. Every part
    has to qualify, so an `ls` bolted onto something that writes stays labelled
    for the thing that writes.

    `grep` is deliberately absent. It reads, but it is also how a block filters
    the output of something that did not, and the conservative reading of an
    unrecognised block is the one that leaves it labelled CODE -- see the note
    in _code_label about not dressing up commands as something familiar.
    """
    parts = [p.strip() for p in re.split(r"&&|\|\||;|\n", code) if p.strip()]
    parts = [p for p in parts if not p.startswith("#")]
    if not parts:
        return False
    for part in parts:
        if part.startswith(("module load", "module purge", "export ", "set ",
                            "cd ", "source ")):
            continue
        head = part.split()[0] if part.split() else ""
        # No redirection: `ls > listing.txt` reads and then writes, and the
        # write is the part worth knowing about.
        if head in _READ_ONLY and ">" not in part:
            continue
        return False
    return True


def _code_label(code):
    """What a block of code is, in the terms this tool is about.

    Every <execute> block used to be labelled RUN, which is true and useless: the
    things that actually happen here -- writing the pipeline script, putting it on
    the scheduler, asking the scheduler how it is going -- are different enough
    that seeing which one you are looking at is most of the value of the
    transcript. The words are chosen to match what the person asked for, not what
    the shell is doing: GENERATE, SUBMIT, SCHEDULER.

    HELP is the one label that means "do not draw this at all" (see render). A
    competent agent reads `genpipes <pipeline> --help` constantly -- it is how it
    avoids asserting a step number it half-remembers -- and fifty lines of usage
    text per turn buries the conversation it was in service of.

    READ is the agent looking at your files -- an `ls` of a data directory, a
    `cat` of a readset. It was the largest thing falling through to CODE, and
    it is worth its own word for the same reason the others are: "it is reading
    your data" and "it is running something I do not recognise" are different
    facts, and CODE covering both is what made the label uninformative.

    Anything else is CODE. Deliberately: an unrecognised command is exactly the
    one you want to read closely, and dressing it up as something familiar would
    be the wrong kind of help.

    Order is load-bearing. GENERATE and SUBMIT are tested before READ, so a
    block that looks at a file AND writes a script is labelled for the write --
    the conservative direction, since mislabelling a submission as a look is
    the only error here with consequences.
    """
    low = code.lower()
    if _is_help_only(low):
        return "HELP"
    if "genpipes" in low and re.search(r"(?:^|\s)(?:-g\b|--genpipes_file\b)", low):
        return "GENERATE"
    if (re.search(r"\b(?:bash|sh)\s+\S+\.sh\b", low)
            or "chunk_genpipes" in low or "submit_genpipes" in low
            or re.search(r"\bsbatch\b", low)):
        return "SUBMIT"
    if (re.search(r"\b(?:squeue|sacct|sinfo|scontrol|scancel)\b", low)
            or "log_report" in low):
        return "SCHEDULER"
    if _is_read_only(low):
        return "READ"
    return "CODE"


def _observations(body):
    """The <observation> blocks in a message, split into two kinds.

    An observation is whatever came back from the last <execute>, and it is
    normally terminal output. But the ask node answers a question through the same
    channel -- it is the shape the model is already prompted to read -- and
    "TERMINAL: The user answered: rnaseq_light" says the wrong thing twice: it was
    not a terminal, and the panel that asked is two lines above. So an answer gets
    its own kind and one quiet line.
    """
    events = []
    for block in re.findall(r"<observation>(.*?)</observation>", body, re.DOTALL):
        text = block.strip()
        events.append({"kind": "answer" if _ANSWER.match(text) else "observation",
                       "text": text})
    return events


def parse(message):
    """Turn one agent message into a list of events.

    An event is a plain dict with a "kind" and whatever that kind needs:

        {"kind": "prompt",      "text": ...}
        {"kind": "note",        "text": ...}
        {"kind": "plan",        "items": [(text, done_bool), ...]}
        {"kind": "code",        "text": ...}
        {"kind": "observation", "text": ...}
        {"kind": "solution",    "text": ...}

    One message routinely yields several events -- the model emits prose, a plan,
    and an <execute> block in a single turn.
    """
    events = []
    content = getattr(message, "content", "") or ""
    kind = type(message).__name__

    # A user-role message that the user did not write. Everything the graph says
    # back to the model travels on this channel -- command output, the answer to a
    # panel, a rejection sent back to be reworked, biomni's own scolding when a
    # reply arrives with no tag in it, the nudge that keeps a conversation from
    # ending on the assistant's turn -- and labelling any of it with the person's
    # name puts words in their mouth. They fall through to be parsed like any
    # other message, so an <observation> in one renders as OUT and the rest
    # renders as quiet prose. Nothing is hidden; it is just not attributed.
    if kind == "HumanMessage":
        if not _MACHINERY.search(content):
            # Their line as they typed it. intake.brief appends the facts it could
            # establish -- what the sentence states, what is in the working
            # directory -- and marks where that starts; showing it back under
            # their name reads as if they had typed an inventory of their files.
            return [{"kind": "prompt",
                     "text": content.split(_CONTEXT_MARK)[0].strip()}]
        if content.strip() == _NUDGE or _CORRECTION.search(content):
            # Pure plumbing. The nudge carries nothing; the correction is the
            # harness telling the model off for replying without a tag, which is a
            # conversation between the graph and the model about the graph's own
            # rules. Neither is something a person can act on, and the reply that
            # provoked it is shown immediately above either way.
            return []
        # Machine-authored. Command output is structured and drawn as TERMINAL;
        # everything else is plain prose and is deliberately NOT run through the
        # tag parser below -- biomni's messages quote the words "<execute>" and
        # "<solution>", and parsing one would draw half of its own instructions as
        # code the agent is about to run.
        blocks = _observations(content)
        if blocks:
            return blocks
        return [{"kind": "note", "text": content.strip()}]

    # Tolerate an unclosed tag, which happens when a message is cut short.
    body = content
    if "<execute>" in body and "</execute>" not in body:
        body += "</execute>"

    # The model's checklist. When it re-emits the list each turn with another box
    # ticked, each turn prints a fresh copy and the progression shows up in the
    # scrollback. Matches "1. [ ] do a thing" and "1. [x] did a thing".
    plan = _PLAN_LINE.findall(body)
    if plan:
        events.append({
            "kind": "plan",
            "items": [(text.strip(), mark.strip() != "") for mark, text in plan],
        })

    # Code the model wants to run -- except the two blocks that never reach an
    # interpreter. agent.routing_function tests both BEFORE the execute node:
    # a submission goes to the gate, an ask() goes to the question node, and
    # neither string is ever handed to Python or bash. propose_submission() in
    # particular cannot run -- it is not a defined name, which is why the gate
    # rewrites the approved block into the command it stood for.
    #
    # So drawing either as a CODE block, in the same style as blocks that did
    # run, tells the reader something untrue: nothing on screen separates "this
    # executed, here is its output" from "this was intercepted and discarded".
    # Each one's real rendering is the thing it opens -- the panel for an ask,
    # the HOLD box for a submission -- and both appear immediately below.
    #
    # This is a rendering rule, not a second copy of either grammar: it drops a
    # block that is nothing but the call, and gate.ask_request / gate.is_submission
    # remain the only things that decide what such a call means. A block that
    # mixes one with real code fails this test and is shown in full, which is
    # the right way round -- the router will not treat it as inert either.
    for block in re.findall(r"<execute>(.*?)</execute>", body, re.DOTALL):
        # Comment lines are dropped before the test, because the model habitually
        # opens a block with "#!BASH" -- which the router ignores when it decides
        # what the block means, so the renderer has to ignore it too or the two
        # disagree about whether this is a question.
        bare = "\n".join(line for line in block.splitlines()
                         if line.strip() and not line.strip().startswith("#"))
        if _ASK_ONLY.match(bare.strip()) or _PROPOSE_ONLY.match(bare.strip()):
            continue
        code = block.strip()
        events.append({"kind": "code", "text": code,
                       "label": _code_label(code)})

    # What the machine said back.
    events += _observations(body)

    # The model's final answer -- always shown in full, minus the checklist.
    #
    # _strip_plan is what makes the plan block usable at all. The lines are
    # lifted into a "plan" event above and drawn as a block that repaints in
    # place; leaving them in the text they were lifted from printed the same
    # checklist twice on every turn -- once rendered, once as raw markdown
    # directly underneath. That double-draw is why the prompt section producing
    # these lines was switched off (agent.PLANS), so the parser has to claim the
    # lines it consumes before turning it back on.
    #
    # The connective-prose path below has always done this. The solution path
    # did not, and every reply lands in one or the other -- so with the prompt
    # requiring exactly one <solution> or one <execute>, a checklist reliably
    # took the un-stripped route.
    for block in re.findall(r"<solution>(.*?)</solution>", body, re.DOTALL):
        text = _strip_plan(block).strip()
        # A turn whose whole answer was the checklist has nothing left to say;
        # the block above is already saying it.
        if text:
            events.append({"kind": "solution", "text": text})

    # Whatever text remains once the structured parts are removed is the model's
    # connective prose. It is kept, not dropped, but rendered quietly. It goes
    # first, because the model writes "now let me..." before the thing it means.
    left = re.sub(r"<execute>.*?</execute>", "", body, flags=re.DOTALL)
    left = re.sub(r"<observation>.*?</observation>", "", left, flags=re.DOTALL)
    left = re.sub(r"<solution>.*?</solution>", "", left, flags=re.DOTALL)
    left = re.sub(r"</?think>", "", left)
    left = _strip_plan(left)
    left = "\n".join(line for line in left.splitlines() if line.strip())
    if left.strip():
        events.insert(0, {"kind": "note", "text": left.strip()})

    return events


# ---------------------------------------------------------------------------
# Layer 2 -- the terminal renderer.
# ---------------------------------------------------------------------------

# The two labels worth colouring. Everything else the agent does is looking:
# reading a file, reading --help, asking the scheduler how a run is going. These
# two are the ones that changed something -- a script written to disk, a job on
# the cluster -- and they are what you scan a long transcript for.
#
# Colour spent anywhere else is colour spent everywhere, which is the state the
# marker column was in before: amber code, cyan output, grey prose, three rule
# glyphs, on every block of every turn. Nothing was emphasised because
# everything was.
_CONSEQUENTIAL = ("GENERATE", "SUBMIT")

# The rule colour of a block whose closing blank line has not been printed yet,
# or None. A command and the output it produced are one act, but they arrive as
# two messages -- the block is drawn, the command runs, and the observation
# turns up on the next call -- so the join cannot be made at parse time. The
# code block holds its blank open instead, and the observation that follows
# lands directly underneath it as a continuation rather than as a second
# labelled block. Anything else closes it first.
_open_rule = None


def _close_open_rule():
    """Emit the blank line a held-open block still owes, if any."""
    global _open_rule
    if _open_rule is not None:
        _open_rule = None
        print()


def _wrapped(line, room):
    """`line` broken to fit `room` columns, as a list. [line] when it fits or
    when there is no width to fit it to.

    Breaks on whitespace where there is any and mid-token where there is not --
    a 200-character path has nowhere to break and still must not run off the
    window. Measured with cells(), because a line may carry escape sequences
    that occupy no columns.
    """
    if not room or cells(line) <= room:
        return [line]
    out, current = [], ""
    for token in re.split(r"(\s+)", line):
        while cells(token) > room:
            take = room - cells(current)
            if take <= 0:
                out.append(current)
                current = ""
                take = room
            current += token[:take]
            token = token[take:]
            out.append(current)
            current = ""
        if cells(current) + cells(token) > room:
            out.append(current)
            current = token.lstrip()
        else:
            current += token
    if current.strip() or not out:
        out.append(current)
    return [p for p in out if p != ""] or [line]


def _tool_of(code):
    """The name of what ACTUALLY ran, for the caption over a verbose block.

    WHAT WAS WRONG. The caption came from _code_label, which classifies a
    block by the PURPOSE it infers from the command text -- HELP, GENERATE,
    SUBMIT, READ, SCHEDULER. Those are the right words for deciding what to
    colour and what to hide, and the wrong words for a caption that claims to
    say what ran:

        show_run(name="...")            captioned `bash`. It is not bash. It
                                        is a capability call this application
                                        answers itself, and nothing reached a
                                        shell.
        module load … --help            captioned `help`. It IS bash, and
                                        `help` is why the model ran it, not
                                        what it is.

    So the caption is derived from execution metadata instead, and there are
    exactly two kinds of thing an <execute> block can be:

      a capability call   gate.capability_request() parses it, and the name it
                          returns is the name of the tool. Same parser the
                          router uses to decide the block is a capability, so
                          the caption cannot disagree with what happened.
      anything else       bash. Biomni runs every other block through a shell,
                          which is the whole of what is known about it.

    NO `read` AND NO `help` HERE, deliberately, and this is a departure worth
    stating. Both were purposes inferred from the text -- `head -40 <path>` is
    a read only in the sense that somebody reading the command can see that it
    is, which is a judgement, not metadata. There is nothing in the execution
    path that distinguishes a read from any other shell command, so captioning
    one differently would be the same class of guess this function exists to
    remove. _code_label still produces those words, because HIDING and
    COLOURING are judgements and are allowed to be.
    """
    try:
        wanted = gate_rules.capability_request(
            code, tuple(capability_table.TABLE))
    except Exception:                                    # noqa: BLE001
        wanted = None
    if wanted and wanted.get("capability"):
        return str(wanted["capability"])
    return "bash"


def _rule(colour, mark, label, text, dim_body=False, hold=False, space=False):
    """Print a block behind a left rule: a quiet label, then the body.

    One glyph for every kind of block, and the label carries the difference.
    Three different glyphs said what three words underneath them were already
    saying, and left the reader learning a private alphabet to get no extra
    fact out of it.

    The rule stays -- machine output runs to dozens of lines and the left edge
    is what shows where a block starts and ends when you are scrolling -- but it
    is one thin mark in one weight, not a bar per line in full colour.

    dim_body greys the content while leaving the label at the rule's own
    colour, which is how the long secondary blocks stay readable without
    competing with the reply they are in service of.

    hold leaves the closing blank unprinted so the next block can continue this
    one -- see _open_rule. An empty label draws no caption line at all, which
    is what makes an observation read as the output of the command above it
    rather than as an event with a name of its own.
    """
    global _open_rule
    shade = DIM if dim_body else ""
    # The routing directive, not something anybody reads. The model opens
    # nearly every bash block with it, so left in it is a line of noise per
    # block; the router strips it too when deciding what the block means.
    body = "\n".join(line for line in text.splitlines()
                     if line.strip().lower() != "#!bash")
    # An <execute> whose output was empty still arrives as an observation, and
    # a labelled block with nothing under it says only that something happened
    # that had nothing to report.
    if not body.strip():
        return
    if label:
        # A bare gutter row before the caption, where one block continues
        # another. The rule is unbroken -- the two halves are one command and
        # its output -- and the gap is what stops "output" reading as a third
        # line of the command above it.
        if space:
            print(f"{colour}{mark}{RESET}")
        print(f"{colour}{mark} {label}{RESET}")
    # WRAPPED, AND WRAPPED INTO THE BLOCK. This printed each line raw, so a
    # generated command line or a job_list row -- routinely 150-200 columns --
    # ran off the window and the TERMINAL wrapped it, at column zero, under the
    # gutter. The overflow then sat left of the rule it was supposed to be
    # beside, and on a /diagnose screen it collided with the ▌ panel. Every
    # other long block in this module goes through fit() or _body_width; this
    # one, which carries the longest text on screen, went through neither.
    #
    # Continuation lines are indented past the gutter rather than aligned with
    # column zero, so a wrapped command still reads as ONE command: the mark
    # says "this block", and the indent says "still the same line".
    #
    # Only against a real terminal. Piped to a file or a CI log there is no
    # width to overflow, and folding the agent's own bytes there would lose
    # information to solve a problem that does not exist off-screen.
    cols = terminal_cols() if _tty() else 0
    room = max(24, cols - 4) if cols else 0
    for line in body.splitlines():
        for i, part in enumerate(_wrapped(line, room)):
            lead = "" if i == 0 else "  "
            print(f"{colour}{mark}{RESET} {shade}{lead}{part}{RESET}")
    # _open_rule is left accurate either way, so a caller never has to clear it
    # by hand -- doing that at one of two call sites was what printed the gap
    # twice, once by the block that closed and once by the one that followed.
    if hold:
        _open_rule = colour
    else:
        _open_rule = None
        print()


def _clipped(text, head=10, tail=4):
    """Machine output, shortened in the middle when it runs long.

    The one place this module does not show everything, and it earns the
    exception: `genpipes --help` is fifty lines, a GenPipes log tail can be
    hundreds, and a screen of it buries the reply that follows. Head and tail are
    both kept because the two ends are where the information is -- what the
    command was doing, and how it ended.

    Only the display is clipped. The model reads the full observation, and the
    full text is in self.log, so nothing is lost to anything but the eye.
    """
    lines = (text or "").splitlines()
    if len(lines) <= head + tail + 1:
        return text
    hidden = len(lines) - head - tail
    return "\n".join(lines[:head] + [f"… {hidden} more lines …"] + lines[-tail:])


# Whether the caller draws the person's own turns itself. The CLI sets this,
# because it needs the echo to land before anything else it prints about that
# line. Off by default so a different front end (web/server.py) still gets the
# turn drawn for it by render().
ECHOED = False


def echo(text):
    """The person's line, drawn once, beside the chevron the prompt uses.

    This is the whole of "show each user message once". The input box is the
    editor and takes itself down on submit (see ui._Editor.finish); this is the
    record.
    """
    for line in (text or "").splitlines() or [""]:
        print(f"  {GREEN}❯{RESET} {line}")
    print()


# ---------------------------------------------------------------------------
# The plan block.
#
# The model re-emits its whole checklist every turn with one more box ticked.
# Printed naively that is the same six lines over and over, which is why this
# used to be dropped on the floor -- but dropping it took away the only thing
# that said what the agent was working through. So it is kept and drawn ONCE,
# on a block that repaints itself in place while the list keeps its shape.
#
# Repainting is only safe while the block is still the last thing on screen.
# _plan_lines is that permission: it holds the block's height, and anything
# else that reaches the screen clears it back to zero, after which the next
# plan draws fresh below whatever interrupted it.
#
# "Anything else" is enforced by shadowing print for this module (see below)
# rather than by a call at each of the several dozen sites that print. The
# shim is the only way to be sure: a display function added later invalidates
# the block for free, whereas a convention to call an invalidator by hand is
# one forgotten call away from a block that repaints over somebody's output.
# ---------------------------------------------------------------------------

_real_print = print


def print(*args, **kwargs):  # noqa: A001 -- deliberate, see above
    """print, plus "the plan block is no longer at the bottom of the screen".

    Module-level shadowing, so every bare print() in this file is covered
    whenever it was written. _draw_plan writes its own repaint through
    sys.stdout directly, which is what keeps it exempt.
    """
    global _plan_lines
    _plan_lines = 0
    return _real_print(*args, **kwargs)

# The item texts last drawn, so a re-emitted list is recognised as the same
# plan rather than a new one. Ticks are deliberately not part of the identity --
# a changed tick is the update we are trying to draw.
_plan = None

# Height of the block on screen, or 0 when it may no longer be repainted.
_plan_lines = 0


def reset_plan():
    """Forget the current plan. Called when a new turn starts, so the next
    task's checklist is a new block rather than an in-place edit of the last
    task's -- two different jobs must not share one set of lines.

    Also closes any block still holding its blank open. A turn that ended on a
    command whose output never arrived would otherwise leave that blank owed,
    and the next turn's first line would print hard against it."""
    global _plan, _plan_lines
    _close_open_rule()
    _plan = None
    _plan_lines = 0


def _plan_body(items):
    """The block's lines, without the trailing blank.

    Three states, and the marker column is what distinguishes them, because
    colour alone does not survive being read at a glance or copied into a bug
    report:

      1. [✓]  done      dim, the tick green
      2. [ ]  current   the first unticked item, bright
      3. [ ]  pending   dim -- it has not started, so it gets no ink

    Drawn as the numbered checkbox the model actually wrote, rather than
    translated into a marker column of its own. The list on screen and the list
    in the reply are then the same object: what you read back to the model, what
    it re-emits next turn, and what you see are one notation, so nobody has to
    hold a mapping between a ▶ and a `2. [ ]` in their head. No header either --
    the brackets say what the block is.
    """
    out = []
    current = next((i for i, (_, done) in enumerate(items) if not done), None)
    for i, (text, done) in enumerate(items):
        n = f"{i + 1}."
        if done:
            out.append(f"  {DIM}{n} [{RESET}{GREEN}✓{RESET}{DIM}] {text}{RESET}")
        elif i == current:
            out.append(f"  {n} [ ] {BOLD}{text}{RESET}")
        else:
            out.append(f"  {DIM}{n} [ ] {text}{RESET}")
    return out


def _draw_plan(items):
    """Draw or update the plan block."""
    global _plan, _plan_lines
    texts = [t for t, _ in items]
    # Clamped to the window before anything is counted. A plan step is the
    # model's own wording, so its length is not ours to predict -- and a step
    # that wrapped used two rows while _plan_lines recorded one, so the walk-up
    # below landed a row short and the block marched down the screen, printing
    # a fresh "Plan" header on every tick.
    # Only against a real terminal. Redirected to a file or a CI log there is
    # no width to overflow and nothing repaints, so clipping the model's own
    # wording there would lose information to solve a problem that does not
    # exist off-screen.
    cols = terminal_cols() if _tty() else 0
    body = ([fit(line, cols - 1) for line in _plan_body(items)] if cols
            else _plan_body(items))

    same = _plan == texts
    if same and _plan_lines and _tty():
        # Back up over the block and lay the new one down on the same rows.
        # \033[J clears from the cursor to the end of the screen, so a plan
        # that has lost a line does not leave the old last line stranded.
        sys.stdout.write(f"\033[{_plan_lines}A\033[J")
    elif same and not _tty():
        # Nothing can be repainted and the list has not changed shape, so
        # reprinting it would just be the duplication this block exists to
        # avoid. The final state still gets drawn when the plan completes.
        return
    else:
        print()
        _plan_lines = 0

    for line in body:
        print(line)
    print()
    _plan = texts
    # Rows, not lines. fit() above means these are the same number today; going
    # through row_count anyway keeps them the same number if a future line
    # escapes the clamp, which is exactly how this drifted in the first place.
    # The +1 is the trailing blank print().
    _plan_lines = (sum(row_count(line, cols) for line in body) if cols
                   else len(body)) + 1


def _tty():
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Model prose, and the three markers a real model habitually uses.
#
# WHY THIS EXISTS. <solution> is the one output path in this module whose
# presentation was never decided. It was drawn with _rule until 4b9393f, which
# replaced that with a raw print loop -- for label-and-rule reasons, not
# content ones -- and _rule then learned to wrap without the prose path
# following. So the agent's own reply was the only block printed exactly as the
# model typed it, asterisks and backticks included.
#
# NARROW ON PURPOSE, and this is not a markdown renderer. diagnosis.py settled
# the question of whether a real model emits markup when asked for a shape --
# "the model's habitual markdown bolding", diagnosis.py:133 -- and answered it
# the same way: ask for a small subset in the prompt, then tolerate what turns
# up. Three markers are understood here. Everything else is printed as the
# characters the model wrote, because unsupported markup must stay readable and
# must never be silently deleted.
#
# NO WRAPPING, deliberately. Every other block in this module wraps; this one
# does not, because re-flowing a line would put a newline into a command
# somebody is about to paste. That is a separate decision from this one.
#
# Colours are read at call time and never bound at import -- retheme() rebinds
# these globals, and under NO_COLOR every token is "" so the markers are simply
# stripped.

# A fence opens or closes a block that is passed through untouched, fence lines
# and all. Rewriting one would mean reflowing or restyling a command, and a
# fenced command is the single most likely thing on the screen to be copied.
_FENCE = re.compile(r"^\s*```")

# A bullet is a line-leading -, * or bullet glyph followed by space and content.
# The space is what keeps "**bold**" from reading as a bullet.
_BULLET = re.compile(r"^(\s*)[-*\u2022][ \t]+(?=\S)")

# One pass, alternation ordered so a code span wins at the same position: the
# `**` inside `a ** b` written as code is text, not emphasis. Both alternatives
# are single-line -- markup never spans a newline, so an unclosed marker stops
# at the end of its own line instead of swallowing the paragraph under it.
_INLINE = re.compile(r"`([^`\n]+)`|\*\*(\S(?:[^*\n]*\S)?)\*\*")

# Code inside a bold span. The nesting is one level and one direction: bold may
# contain code, code contains nothing.
_CODE_ONLY = re.compile(r"`([^`\n]+)`")


def _inline(text):
    """`code` and **bold** as styles. Unpaired markers stay literal.

    THE NESTED CASE IS WHY THIS IS NOT TWO SUBSTITUTIONS. A real model writes
    **`germline_snv`** and **the `germline_snv` protocol** -- code inside
    emphasis -- and the naive rendering closes the inner span with RESET, which
    clears every attribute rather than just the colour. The word after it then
    loses the bold it was meant to keep. So an inner span ends by restoring
    BOLD, and bold is never switched off before the colour is applied, which is
    what makes nested code read as bold AND coloured rather than as a hole in
    the emphasis.
    """
    def one(m):
        if m.group(1) is not None:
            return f"{SECONDARY}{m.group(1)}{RESET}"
        inner = _CODE_ONLY.sub(
            lambda c: f"{SECONDARY}{c.group(1)}{RESET}{BOLD}", m.group(2))
        # A restore left at the very end would enable an attribute nothing goes
        # on to use, so it is trimmed rather than emitted and immediately reset.
        if BOLD and inner.endswith(BOLD):
            inner = inner[:-len(BOLD)]
            return f"{BOLD}{inner}"
        return f"{BOLD}{inner}{RESET}"
    return _INLINE.sub(one, text)


def _prose(text):
    """Model prose as terminal lines. Returns a list; prints nothing.

    Line count is preserved exactly -- one line in, one line out -- which is
    the property that says no wrapping was introduced here.
    """
    out, fenced = [], False
    for raw in (text or "").splitlines():
        if _FENCE.match(raw):
            fenced = not fenced
            out.append(raw)
            continue
        if fenced or not raw.strip():
            out.append(raw)
            continue
        bullet = _BULLET.match(raw)
        if bullet:
            out.append(f"{bullet.group(1)}{GREY}\u2022{RESET} "
                       f"{_inline(raw[bullet.end():])}")
            continue
        out.append(_inline(raw))
    return out


def _draw(event):
    """Draw one parsed event."""
    k = event["kind"]

    if k == "prompt":
        # The line as typed, beside the same chevron the prompt uses, once.
        # It appeared twice before -- in the input box and again under a
        # speaker label -- which is the one thing a transcript must not do,
        # because the reader cannot tell whether they said it once or twice.
        #
        # Skipped when the caller has already drawn it. The CLI does, the
        # moment the line is read, because it needs the echo to land BEFORE
        # anything it prints about the line -- "Preparing run\u2026" above the
        # sentence that caused it reads backwards.
        if not ECHOED:
            echo(event["text"])

    elif k == "note":
        # Connective prose. No label, thin rule, grey. Present but subordinate.
        _rule(GREY, "\u258f", "", event["text"], dim_body=True)

    elif k == "code":
        label = event.get("label") or "CODE"
        # Held open: the output of this command, if there is any, belongs to
        # the same block and arrives on the next message.
        _rule(AMBER if label in _CONSEQUENTIAL else GREY,
              "\u258f", _tool_of(event["text"]),
              event["text"], dim_body=True, hold=True)

    elif k == "observation":
        # No caption. "terminal" named the channel rather than the event, and
        # it cost a line on every command the agent ran -- while the thing it
        # was distinguishing, whose output this is, is already answered by the
        # command sitting directly above it. When there is no command above
        # (its block was hidden, or folded) the rule and the indent still mark
        # it as machine output.
        # CAPTIONED NOW, because the block above it is captioned too. The
        # caption was dropped when it read "terminal", which named the channel
        # rather than the event and cost a line on every command -- but with a
        # tool name over the command, an uncaptioned wall of text underneath
        # reads as more command. "output" is one word and it is the boundary
        # between what was asked and what came back, which is the whole
        # hierarchy this block was missing.
        #
        # Only when a command is actually open above it. An observation with
        # no visible command -- its block hidden, or folded -- is the only
        # thing on screen and needs no boundary drawn against nothing.
        _rule(_open_rule or GREY, "\u258f", "output" if _open_rule else "",
              _clipped(event["text"]), dim_body=True, space=bool(_open_rule))

    elif k == "answer":
        # One quiet line. The panel above it is the event; this is the receipt.
        # The bar is furniture and is muted; the text is the agent's answer and
        # is left in the terminal's own foreground. This used to be cyan AND
        # dim together, which is the least legible combination available and
        # was applied to a line somebody is meant to read.
        print(f"{GREY}\u258f{RESET} {_answer_line(event['text'])}\n")

    elif k == "solution":
        # The reply itself. No rule, no label -- this is the agent talking, and
        # in a two-party conversation the second party does not need to be
        # introduced. "SOLUTION" was Biomni's word for the tag and made every
        # answer sound like the end of an exercise.
        #
        # Through _prose, which renders the three markers the prompt asks for
        # and leaves everything else as typed. It does not wrap: the indent
        # below is the only thing added to a line.
        for line in _prose(event["text"]):
            print(f"  {line}" if line.strip() else "")
        print()


def _answer_line(text):
    """"The user answered: stringtie" as the receipt for a panel."""
    first = (text or "").splitlines()[0].strip()
    if _ANSWER.match(first) and "declined" in first:
        return "no answer given -- carrying on with a default"
    return re.sub(r"^The user answered:\s*", "answered: ", first)


# Labels whose block AND its output are not drawn at all. Just HELP: an agent
# that reads `genpipes <pipeline> --help` before writing a command is doing the
# right thing, and doing it on most turns, but the person asked the question in
# the line above and does not need fifty lines of usage text to see the answer.
_HIDDEN = ("HELP",)

# Whether the observation about to arrive belongs to a block that was hidden.
# Module state because the two arrive as separate messages: the code block is
# rendered, the command runs, and the output turns up on the next call.
_swallowing = False

# ---------------------------------------------------------------------------
# Folding. The agent's working is kept and not shown.
#
# What the reader wants from a transcript is the answer. What the agent emits
# on the way to it -- a --help lookup, a generation, its output, a paragraph of
# reasoning about what the output meant -- is the equivalent of a chain of
# thought: genuinely useful when something has gone wrong, noise on every turn
# where nothing has. Claude Code makes the same call and it is the right one.
#
# Kept, not discarded, and that distinction is the whole design. Every folded
# event goes into _folded, so /verbose can print what already scrolled past
# rather than only changing what happens next. A fold you cannot open is a
# deletion with better manners.
#
# Two things are never folded: the agent's actual reply, and the gate.
# ---------------------------------------------------------------------------

VERBOSE = bool(os.environ.get("GENPIPE_VERBOSE"))

# Every folded event this session, in order, so /verbose can replay them.
_folded = []

# How many steps have been folded since the last thing that WAS drawn. Reset by
# anything visible, so the marker counts this answer's working rather than the
# session's.
_folded_here = 0


def set_verbose(on):
    """Turn folding off (or back on). Returns the new setting."""
    global VERBOSE
    VERBOSE = bool(on)
    return VERBOSE


def replay():
    """Draw every event folded so far, and return how many there were.

    What /verbose shows for the work that has already happened -- without this,
    turning verbose on would only affect the next turn, which is never the turn
    you wanted it for.

    It reports rather than announces. Nothing folded is not an event: it means
    the session has not done any working yet, which the person can see, and
    saying so took a whole block to answer a question nobody asked. The count
    goes back to the caller so the one confirmation it prints can carry it,
    instead of a header here and a confirmation there for the same keystroke.
    """
    # The same close-before-a-new-block rule render() applies. Replay draws
    # through _draw directly rather than through render(), so without this the
    # held-open blank between a command and its output is never paid and every
    # replayed block runs into the next one.
    for event in _folded:
        if event["kind"] != "observation":
            _close_open_rule()
        _draw(event)
    _close_open_rule()
    return len(_folded)


def _fold(event):
    """Set an event aside instead of drawing it."""
    global _folded_here
    _folded.append(event)
    _folded_here += 1


def _flush_fold():
    """Say that working happened, in one line, then stop counting.

    Printed before the thing that follows it, so the marker sits above the
    answer it produced rather than orphaned at the end of the turn.
    """
    global _folded_here
    if not _folded_here:
        return
    n = _folded_here
    _folded_here = 0
    print(f"  {DIM}│ {n} step{'s' if n > 1 else ''}"
          f"  ·  /verbose to see the working{RESET}")


def render(message):
    """Parse a message and draw what is worth drawing.

    One thing is never drawn, at any verbosity, and it is a judgement that a
    transcript is for following the work rather than for auditing the agent:

      documentation lookups   see _HIDDEN.

    One thing is drawn at EVERY verbosity, for the same reason from the other
    side: the model's checklist. It re-emits the whole list each turn with one
    more box ticked, so it is drawn once and repainted in place -- see
    _draw_plan, which turns that repetition into progress instead of noise.

    Everything else is drawn when VERBOSE is on. When it is off -- the default --
    only the person's own line and the agent's reply are drawn; the working is
    folded away, counted, and kept for /verbose.
    """
    global _swallowing
    for event in parse(message):
        kind = event["kind"]
        # An observation continues the block above it; everything else starts a
        # new one, so the blank that block was holding open is owed now. Done
        # here rather than in each branch of _draw so that a path added later
        # cannot forget it and print into somebody else's block.
        if kind != "observation":
            _close_open_rule()
        if kind == "plan":
            # Never folded, at any verbosity. The fold's whole cost is that it
            # leaves you unable to tell what the agent is doing; the plan is
            # the answer to that, so folding it away with the rest would be
            # hiding the index along with the chapters.
            _draw_plan(event["items"])
            continue
        if kind == "code":
            # Hidden when folded, shown when unfolded. The original reason for
            # dropping a --help outright still holds at the default verbosity:
            # a competent agent reads one most turns, and fifty lines of usage
            # text buries the conversation it was in service of. But /verbose is
            # the person saying they want the working, and the --help lookups
            # are how the agent avoids asserting a step number it half-
            # remembers -- which makes them among the most worth reading. The
            # clip at _clipped keeps even the unfolded copy to a dozen lines.
            _swallowing = event.get("label") in _HIDDEN and not VERBOSE
            if _swallowing:
                continue
        elif kind == "observation" and _swallowing:
            _swallowing = False
            continue
        if not VERBOSE and kind in ("code", "observation", "note"):
            _fold(event)
            continue
        if kind in ("solution", "prompt"):
            _flush_fold()
        if kind == "solution" and _DEFER_SOLUTION:
            # /diagnose draws its own answer, once, after the whole thing has
            # arrived and been parsed into sections. Letting the transcript draw
            # it too would print the same conclusion twice in two different
            # shapes -- the raw markdown first, the structured version under it.
            continue
        _draw(event)


# Set only while /diagnose is streaming. A global, like VERBOSE and
# _swallowing, because render() is called from inside the graph's stream and
# there is nowhere to thread an argument through.
_DEFER_SOLUTION = False


def defer_solution(on):
    """Stop render() drawing <solution> blocks, so a caller can draw its own."""
    global _DEFER_SOLUTION
    _DEFER_SOLUTION = bool(on)

# ---------------------------------------------------------------------------
# The gate. The one moment the run stops and hands a decision to a human, so it
# gets the loudest treatment on screen -- but "loudest" here means restraint, not
# volume. The banner is reversed rather than coloured -- READY TO SUBMIT is a
# decision point, not a failure, and spending red on it left nothing to say
# with when a run genuinely cannot proceed. The one amber thing on the screen
# is the verb that cannot be undone. The command sits alone in whitespace
# because it is the thing actually being approved, and nothing should compete
# with it.
#
# No box, no borders, no rules. Whitespace does the framing. That keeps the
# alignment from breaking on long paths, and it means the resume commands can be
# selected and pasted without dragging a border character along with them.
# ---------------------------------------------------------------------------

# The last line of the session. One sentence, and it is about the work rather
# than about the tool: nobody opens a terminal to run a pipeline, they open it to
# find something out. Sampled so that a tool used ten times a day does not say the
# same thing ten times.
_GOODBYES = (
    "Goodbye -- and thank you for advancing biology.",
    "See you next time. Thank you for advancing biology.",
    "Until next time. The science is better for it.",
    "Goodbye. Somewhere a genome is better understood for this.",
    "That's all for now -- thank you for moving biology forward.",
    "Goodbye, and thank you for the work you do.",
    "See you soon. Biology moves one run at a time.",
    "Take care -- and thank you for advancing biology.",
)


def farewell():
    """Printed on the way out.

    Held runs used to be listed here as a last reminder. They are not any more:
    the list is the first thing shown at startup, which is where a decision you
    still owe is actually actionable, and the goodbye is a better goodbye for
    being only that.
    """
    print()
    print(f"  {GREEN}{FAINT}{random.choice(_GOODBYES)}{RESET}\n")


def fresh(pending=()):
    """Printed by /new: the conversation is gone, the runs are not.

    Worth stating explicitly, because the two are easy to conflate and the
    consequence of getting it wrong runs in both directions -- believing a run
    was discarded with the conversation, or believing a conversation still
    remembers a run it can no longer see.
    """
    print()
    print(f"  {GREEN}▌{RESET} {BOLD}New conversation.{RESET} "
          f"{DIM}The agent starts from nothing here.{RESET}")
    if pending:
        names = ", ".join(r["name"] for r in pending)
        print(f"  {DIM}  {len(pending)} run(s) still held and still yours: "
              f"{RESET}{WHITE}{names}{RESET}")
    print(f"  {DIM}  Every run you have started keeps its name. /list to see "
          f"them.{RESET}")
    print()


def environment(findings):
    """Environment problems, at startup. Silent when there are none.

    A blocker gets two lines and says so in words -- "jobs will not run" is worth
    stopping on, and a person who has to decode a colour to learn that has been
    told nothing. A warning gets one, because it is the same warning every launch
    until it is fixed and it is competing with the prompt for attention.

    Both carry the fix. A check that reports a problem and not its remedy has
    only moved the work.
    """
    if not findings:
        return
    print()
    for finding in findings:
        if finding.blocking:
            print(f"  {RED}{BOLD}{finding.variable}{RESET} "
                  f"{RED}BLOCKS SUBMISSION{RESET}  {DIM}{finding.problem}{RESET}")
            print(f"      {DIM}fix:{RESET} {WHITE}{finding.fix}{RESET}")
        else:
            print(f"  {AMBER}▌{RESET} {DIM}{finding.variable}: "
                  f"{finding.problem}{RESET}  {DIM}fix:{RESET} "
                  f"{WHITE}{finding.fix}{RESET}")
    print()


# Columns for the command mirror. The label column is narrower than the gate's
# old 18 because the flag now sits beside it and the two together have to leave
# room for a path -- 13 + 4 puts the value at column 23 of 74, which fits an
# absolute ini path without wrapping in the common case.
_MIRROR_LABEL = 13
_MIRROR_FLAG = 4


def _verdicts(changed):
    """`changed` as {row: verdict}, whatever shape it arrived in.

    Callers hand this either a plain collection of rows -- every one of them
    applied, which is what the panel means -- or the dict modify.compare()
    returns. Normalising here rather than at each call site keeps the three
    renderers below reading one shape.
    """
    if isinstance(changed, dict):
        return dict(changed)
    return {row: modify.APPLIED for row in (changed or ())}


def _mirror_state(row, active, pending, changed):
    """Which of the four states a mirror line is in, as (tint, strong).

    Three states plus the default, and each gets a colour AND a marker at the
    call site. Colour alone carries this badly -- it is the first thing a
    terminal theme, a screenshot or a colour-blind reader flattens -- and the
    whole point of the mirror is that somebody can SEE which line their cursor
    is about to change.

        pending   selected, not yet applied. Bold + underline.
        changed   applied, and this is the run coming back. Green.
        active    the row under the cursor. Bright. Implies nothing -- it is
                  where you are looking, not what you have chosen.

    PENDING USED TO BE RED, and the docstring defended it as "reads as about to
    move". It does not. Red was simultaneously carrying three meanings on these
    screens -- the row you are editing, a row you are obliged to answer, and an
    environment blocker that stops submission entirely -- so the one state that
    is completely ordinary looked like the two that need attention. Editing a
    field is not an error, and a screen where every edit glows red teaches
    people to ignore the colour that means something is actually wrong.

    Underline survives what colour does not: a screenshot, a light theme, a
    colour-blind reader. So the change is more robust than what it replaces,
    not merely calmer.

    Checked in that order, so a row that is both active and pending reads as
    pending: what a line is about to become matters more than where the cursor
    happens to be resting.
    """
    if row is not None and row in pending:
        return WHITE, f"{BOLD}{UNDER}{WHITE}"
    if row is not None and row in changed:
        verdict = changed[row] if isinstance(changed, dict) else modify.APPLIED
        # RED for a change that did not land, amber for one nobody asked for.
        # A row is only green when the command actually moved the way it was
        # asked to -- see modify.compare for why green used to mean "requested"
        # and why that is the one thing this screen must not say.
        if verdict == modify.IGNORED:
            return RED, f"{RED}{BOLD}"
        if verdict == modify.DRIFTED:
            return AMBER, f"{AMBER}{BOLD}"
        return GREEN, f"{GREEN}{BOLD}"
    if row is not None and row == active:
        return WHITE, f"{BOLD}{WHITE}"
    return DIM, ""


def _mirror_body(line, tint, strong):
    """One mirror line's text from the label column rightwards, and the
    continuation lines its extra values need."""
    label = f"{tint}{line.label:<{_MIRROR_LABEL}}{RESET}"
    flag = f"{tint}{line.flag:<{_MIRROR_FLAG}}{RESET}"
    values = [_tilde(v) for v in line.values]
    body = f"{strong}{values[0]}{RESET}" if values else ""
    if line.note:
        # A caution keeps its colour whatever state the line is in; an
        # observation takes the line's own tint, so a row nobody has touched
        # stays quiet.
        #
        # AMBER, not RED. Red on this screen means "cannot submit" -- it is
        # what a preflight blocker uses, and a blocker is the one thing here
        # that stops the run. An unset `-o` does not stop anything; it means
        # GenPipes writes into whatever directory you were standing in, which
        # is worth noticing and is not an error. Spending the same red on both
        # left the loudest colour on the screen meaning two different things,
        # and the one that actually blocks lost by being indistinguishable.
        aside = AMBER if line.warn else (tint or DIM)
        body = (f"{body}  {DIM}{line.note}{RESET}" if body
                else f"{aside}{line.note}{RESET}")
    # A `-c` stack is the one row that is routinely several values, and stacking
    # them under each other is the only way its ORDER stays readable -- which is
    # the whole meaning of an ini stack.
    return f"{label}{flag}{body}", [f"{strong or DIM}{v}{RESET}" for v in values[1:]]


def _count_rows(path):
    """Data rows in a readset or design file, or None if it cannot be read.

    None rather than 0 on any failure, and every caller treats it as "say
    nothing". A count is a claim about what is about to run, and a wrong one on
    the approval screen is worse than an absent one -- the whole point of the
    line is that somebody can catch `8 samples` when they expected 40.
    """
    try:
        with open(path, errors="replace") as f:
            rows = [l for l in f.read().splitlines() if l.strip()]
    except OSError:
        return None
    return max(0, len(rows) - 1) or None          # minus the header


def _flag_line_value(m, row):
    """The value the command actually wrote for one mirror row, or ''.

    Reads the parse rather than the proposal's slots, which is the point: the
    two are independent readings of two different commands, and the only way to
    notice they disagree is to hold both.
    """
    index = m.index_of(row) if m else None
    return m.lines[index].value if index is not None else ""


def _job_total(proposal):
    """How many jobs the generated script says it will submit, or None.

    None where the script cannot be found or its header cannot be read, and
    every caller must say nothing rather than guess -- the same rule
    _count_rows follows, for the same reason: a wrong number on the approval
    screen is worse than an absent one.

    Resolved against the same bases agent.submission_gate checks the script's
    existence with, so the two cannot disagree about which file is meant.
    """
    slots = (proposal or {}).get("slots") or {}
    script = runs.resolve_path((proposal or {}).get("script"),
                               os.getcwd(), slots.get("output_dir"))
    return runs.expected_jobs(script) if script else None


def _consequences(proposal, name, total=None):
    """The two or three facts somebody actually approves on.

    The mirror below is exact and complete, and neither of these is legible
    from it: how much data this is about to run on (a readset path does not
    say 8 samples) and where the output lands (an ABSENT `-o` is the loudest
    fact on the screen and is written as nothing at all).

    Derived, so everything here is hedged: a count that could not be read is
    left out rather than guessed, and the flags underneath remain the record.
    """
    slots = (proposal or {}).get("slots") or {}
    pipeline = slots.get("pipeline") or "genpipes"
    out = []

    what = pipeline
    if slots.get("protocol"):
        what += f" {slots['protocol']}"
    steps = slots.get("steps")
    # "all steps" is an INFERENCE from an absent `-s`, not a setting anybody
    # chose. It says so on the `steps` ROW now -- the box draws that row even
    # when the flag is absent -- rather than here, so the fact appears once.
    # Contrast `output` below, which stays here and is hidden as a row: an
    # unset `-o` is a hazard and earns the loudest line on the screen, while an
    # unset `-s` is a default and earns a row.
    detail = [f"steps {steps}" if steps else "all steps"]
    samples = _count_rows(slots.get("readset") or "")
    if samples:
        detail.append(f"{samples} sample{'s' if samples != 1 else ''}")
    # How much work this actually is, from the generated script's own header.
    # The most decision-relevant number on the screen and the last one to get
    # here: everything else describes what was ASKED for, while this is what
    # GenPipes decided that amounts to after looking at what is already on disk.
    # Passed in rather than read here, because gate() needs the same number to
    # word the /approve line and the two must not disagree.
    if total:
        detail.append(f"{total} job{'s' if total != 1 else ''}")
    out.append(f"      {BOLD}{WHITE}{what}{RESET}{DIM}, {', '.join(detail)}{RESET}")

    # And the case where it amounts to nothing.
    #
    # GenPipes skips any step whose outputs are already present, so re-running
    # a pipeline into a directory that still holds the last run's results
    # generates a script with no jobs in it -- `# TOTAL: 0 job... skipping`.
    # That is an ordinary thing to hit and it is invisible everywhere else: the
    # box lists a readset, a design, a full config stack and a step range, and
    # every one of those is correct. Approving it spends nothing, submits
    # nothing, and used to come back `the outcome is unknown`, which reads like
    # a fault in the tool rather than the pipeline saying there was no work.
    #
    # Amber, not red: nothing is broken and the command is fine.
    #
    # ONE LINE, AND NO REMEDY. This carried a second line naming `-f` and a
    # fresh `-o` as the ways out, which is the tool explaining GenPipes to
    # somebody who runs GenPipes. The fact is not guessable from the box and is
    # worth the line; what to do about it is theirs, they know the flags, and a
    # gate that starts suggesting flags is back to lecturing at the one moment
    # somebody is trying to read quickly.
    if total == 0:
        out.append(f"      {AMBER}nothing to submit{RESET}{DIM} — every step's "
                   f"output is already on disk{RESET}")

    where = slots.get("output_dir")
    out.append(f"      {DIM}writes into {RESET}{WHITE}{where}{RESET}" if where else
               f"      {DIM}writes into {RESET}{AMBER}the current directory{RESET}"
               f"{DIM} — no -o was set{RESET}")
    # The name used to be a third consequence line ("named test-now"). It is
    # the headline's subject now -- READY TO SUBMIT <name> -- which is where
    # the thing being decided about belongs, and printing it twice on the one
    # screen whose job is to identify what you are launching was noise.
    return out


def mirror_lines(m, active=None, pending=(), changed=(), indent="      ",
                 hide=(), wanted=None):
    """The command mirror as a list of printable lines, for the gate.

    Returned rather than printed because it is drawn in two very different
    places: the gate prints it once and moves on, while modify_panel() repaints
    it on every cursor move with a checkbox beside each changeable line. A
    function that printed could not serve the second, and two renderers would
    drift on exactly the detail that has to match -- which line a row owns.
    """
    if not m:
        return []
    pending = set(pending or ())
    changed = _verdicts(changed)
    hide = set(hide or ())
    out = []
    pad = " " * (_MIRROR_LABEL + _MIRROR_FLAG)

    for line in m.lines:
        # Dropped from the drawing only. The line stays on the Mirror, so
        # /modify's panel -- which passes no `hide` -- still owns and indexes
        # it, and a row hidden here can never shift a checkbox there.
        #
        # Matched on the label as well as the row, because a flag with no
        # modify row of its own -- `-g` is one -- carries row=None and can only
        # be named by its label.
        if not line.head and (line.row in hide or line.label in hide):
            continue
        tint, strong = _mirror_state(line.row, active, pending, changed)
        # A different GLYPH per state, not only a different colour -- ◆ for a
        # row about to move, ● for one that has. See _mirror_state for why red
        # stopped being the pending colour.
        # A different GLYPH per verdict as well as a different colour, for
        # the reason the whole mirror is marked twice over: colour is the first
        # thing a screenshot, a light theme or a colour-blind reader loses, and
        # "your change did not land" is the one thing on this screen that must
        # survive all three.
        verdict = changed.get(line.row)
        mark = (f"{BOLD}◆{RESET}" if line.row in pending else
                f"{RED}✗{RESET}" if verdict == modify.IGNORED else
                f"{AMBER}●{RESET}" if verdict == modify.DRIFTED else
                f"{GREEN}●{RESET}" if verdict else
                f"{GREEN}❯{RESET}" if line.row is not None and line.row == active
                else " ")
        if line.head:
            out.append(f"{indent[:-2]}{mark} {strong or f'{BOLD}{WHITE}'}"
                       f"{line.value}{RESET}")
            out.append("")
            continue
        body, extras = _mirror_body(line, tint, strong)
        out.append(f"{indent[:-2]}{mark} {body}")
        out.extend(f"{indent}{pad}{extra}" for extra in extras)
        # SAY WHAT WAS ASKED FOR. A red row on its own tells somebody the
        # value is not what they wanted without telling them what they wanted,
        # and they are reading this screen precisely because they cannot hold
        # both in their head. Only when the value is known -- a change made
        # through the panel carries it, one typed in prose does not, and
        # inventing it would be the same sort of claim this whole change
        # removes.
        if verdict == modify.IGNORED:
            asked = (wanted or {}).get(line.row)
            out.append(f"{indent}{pad}{RED}not applied{RESET}{DIM}"
                       + (f" — you asked for {asked}" if asked else
                          " — the regenerated command still has the old value")
                       + f"{RESET}")
        elif verdict == modify.DRIFTED:
            out.append(f"{indent}{pad}{AMBER}changed on its own{RESET}{DIM}"
                       f" — this was not part of what you asked for{RESET}")
    return out


# The panel's gutter: two spaces, the cursor arrow, a space, the state dot, a
# space. Six, where the old checkbox panel needed eight -- selecting and editing
# used to be separate acts and needed separate markers, and now that a row is
# opened rather than ticked there is one marker for where you are and one for
# what the row has become.
_PANEL_GUTTER = 6

# Where an open row's choices hang: under the VALUE of the row they belong to,
# not under its label. A choice is a candidate value, so it is drawn in the
# column values live in, and the eye reads straight down from `gembs` to the
# thing that would replace it.
_CHOICE_INDENT = _PANEL_GUTTER + _MIRROR_LABEL + _MIRROR_FLAG


def _panel_height():
    """How many lines the panel's rows may use before the terminal scrolls.

    Scrolling is not a cosmetic failure here. ui.choose repaints by moving the
    cursor up its own line count; if the block was taller than the window, the
    terminal has scrolled underneath it and every subsequent repaint lands in
    the wrong place, painting over the transcript. So the rows are budgeted
    against the real window, and what does not fit is elided deliberately
    rather than lost accidentally.

    The reserve covers what choose() draws around the rows -- the question, two
    blank lines, the note and the hint -- plus a couple of lines of headroom so
    the panel is not flush against the top of the screen.
    """
    return max(8, terminal_rows() - 9)


def _elide(lines, focus, room):
    """Drop the lines furthest from `focus` until the rest fit in `room`.

    The invocation stays -- it is what the whole panel is about -- and so does a
    window around wherever the cursor is, so an open row and its choices are
    always whole. What went is stated rather than silently missing: a panel that
    quietly shows eight of twelve rows is a panel that lies about the command.
    """
    if len(lines) <= room or room < 4:
        return lines
    keep_top = 2                      # the invocation and the blank under it
    body = lines[keep_top:]
    focus = max(0, focus - keep_top)
    budget = room - keep_top - 1      # -1 for the line that says what is hidden
    start = max(0, min(focus - budget // 2, len(body) - budget))
    shown = body[start:start + budget]
    hidden = len(body) - len(shown)
    note = f"{' ' * _PANEL_GUTTER}{DIM}···   {hidden} more{RESET}"
    return lines[:keep_top] + shown + [note]


def modify_panel(entries_of, changes=None, notes=None, required=None,
                 typed=lambda: "", open_of=lambda: None, details=lambda: True):
    """The /modify chooser: the command, with the row being changed open in it.

    Returns a draw function for ui.choose, not lines, because the panel is
    repainted on every keystroke and only ui.choose knows where the cursor is.
    `entries_of` is a callable for the same reason the panel's row list is one --
    the list changes while the panel is open, as rows unfold and collapse.

    THE MIRROR IS THE LIST. The obvious build -- a mirror printed above a
    separate menu of row names -- was tried on paper and is worse than either
    half alone: every changeable row appears twice, three lines apart, and the
    person has to hold the mapping between the two columns in their head while
    the thing they are trying to read is a command. Here there is one list. A
    line you cannot change -- `-g cmd.sh`, `-j slurm` -- is simply shown,
    because "what else is in this command" is a question the panel should
    answer without being asked.

    AND THE CHOICES ARE IN IT TOO. They used to be a second screen printed
    underneath, which meant the command was redrawn inside it, the answer landed
    twelve lines away from where the question was asked, and both blocks sat on
    a 24-line terminal at once. Opening the row in place costs (choices - 1)
    lines and gives them back on collapse.

        changes   row -> its new value. Drawn `old  →  new` in green, which is
                  the same green the gate uses for a row that moved, so the two
                  screens agree about what colour means.
        notes     row -> (colour, text), for a change worth a word of warning.
                  Amber does not block; red does. See modify.step_risk for why
                  a skipped dependency is amber and not a refusal.
        required  rows an earlier answer has made mandatory, as {row: why}.
                  Red with the reason beside them, because "you have to answer
                  this" is a fact about the row, not a state somebody put it in.
    """
    changes = changes if callable(changes) else (lambda c=changes: dict(c or {}))
    notes = notes if callable(notes) else (lambda n=notes: dict(n or {}))
    required = required if callable(required) else (lambda r=required: dict(r or {}))
    pad = " " * _CHOICE_INDENT

    def draw(cursor, picked):
        entries = list(entries_of())
        now, warn, must = changes(), notes(), required()
        opened, narrowing, showing = open_of(), typed(), details()
        out, focus = [], 0
        # One description column for the open row's choices, measured from the
        # widest of them and capped, exactly as option_lines does it: labels of
        # different lengths put every description at a different column, and a
        # list of options then reads as prose with the labels buried in it.
        widest = max((len(e.label) for e in entries
                      if e.kind == modify.CHOICE), default=0)
        choicew = min(widest + 3, 26)

        for entry in entries:
            here = entry.pick is not None and entry.pick == cursor
            if here:
                focus = len(out)

            if entry.kind == modify.TYPED:
                # Draws nothing. The row above it is already showing the caret
                # and what has been typed into it; a second line saying so
                # would be the same answer twice, and putting it under the row
                # is exactly the stacked layout this panel exists to remove.
                continue

            if entry.kind == modify.CHOICE:
                mark = f"{GREEN}❯{RESET}" if here else " "
                label = (f"{BOLD}{WHITE}{entry.label}{RESET}" if here
                         else entry.label)
                num = f"{DIM}{entry.pick + 1:>2}{RESET}"
                line = f"{pad[:-4]}{mark} {num}  {label}"
                # WHAT THE KEYS DO TO THIS ROW GOES ON THIS ROW ONLY.
                #
                # `[` and `]` move the ini the cursor is on, so the hint belongs
                # where the cursor is. Carried on every option instead -- which
                # is how it started, as `· [ ] reorders` in each description --
                # it is the same sentence repeated down the list, and it sits
                # beside removed and merely-available inis where those keys do
                # nothing at all.
                #
                # Read off the MARKER, which is the row's state and is already
                # in the label because a description is the first thing a narrow
                # terminal drops. Only a `-c` row that is on the stack has
                # anywhere to be moved to.
                note = entry.description
                if (here and entry.row == modify.CONFIG
                        and entry.label.startswith(modify.ON_MARK)):
                    note = f"{note} {modify.REORDER_HINT}".strip()
                if note and showing:
                    line += (f"{' ' * max(1, choicew - len(entry.label))}"
                             f"{DIM}{note}{RESET}")
                out.append(line)
                continue

            if entry.kind == modify.EXTRA:
                mark = f"{GREEN}❯{RESET}" if here else " "
                label = f"{BOLD}{WHITE}{entry.label}{RESET}" if here else entry.label
                out.append("")
                out.append(f"  {mark}   {label}   {DIM}{entry.description}{RESET}")
                continue

            line = entry.line
            row = entry.row
            moved = row in now
            open_here = row is not None and row == opened

            # Four states and a default, each with a colour AND a marker, for
            # the reason _mirror_state gives: colour alone is the first thing a
            # theme, a screenshot or a colour-blind reader flattens.
            #
            # RED IS RESERVED FOR THE ONE STATE THAT BLOCKS. The row being
            # edited used to be red and the row you are OBLIGED to answer was
            # red too, so the commonest thing on the screen looked identical to
            # the only thing on it that stops you proceeding. Opening a row is
            # not an error; being unable to regenerate without answering it is.
            #
            # So: bold+underline for the row you are in, red only for a row
            # that must be answered before this change set can be applied.
            if open_here:
                tint, strong = WHITE, f"{BOLD}{UNDER}{WHITE}"
            elif moved:
                tint, strong = GREEN, f"{GREEN}{BOLD}"
            elif row in must:
                tint, strong = RED, f"{RED}{BOLD}"
            elif here:
                tint, strong = "", f"{BOLD}{WHITE}"
            else:
                tint, strong = DIM, ""

            dot = (f"{BOLD}◆{RESET}" if open_here
                   else f"{GREEN}●{RESET}" if moved
                   else f"{RED}●{RESET}" if row in must else " ")
            arrow = f"{GREEN}❯{RESET}" if here else " "
            # The open row keeps its marker even though the cursor has moved
            # down into its choices and it is no longer selectable. It is the
            # row being answered; losing its dot at the moment it matters most
            # is how the eye loses track of what the choices belong to.
            gutter = (f"  {arrow} {dot} "
                      if entry.pick is not None or dot != " "
                      else " " * _PANEL_GUTTER)

            if line.head:
                out.append(f"{gutter}{strong or f'{BOLD}{WHITE}'}"
                           f"{line.value}{RESET}")
                out.append("")
                continue

            body, more = _mirror_body(line, tint, strong)
            if open_here and row == modify.CONFIG:
                # `-c` open. The `old  →  new` header every other row uses is a
                # lie here twice over: there is no single old value (the row is
                # a stack, and that header can only show its first line), and
                # Enter is not about to replace anything -- it moves one ini on
                # or off. So the header states the stack's SIZE and what the
                # keys do, and keeps only the caret, because typing still
                # narrows the list underneath.
                stack = now[row] if row in now else line.values
                body = (f"{tint}{line.label:<{_MIRROR_LABEL}}{RESET}"
                        f"{tint}{line.flag:<{_MIRROR_FLAG}}{RESET}"
                        f"{DIM}{len(stack)} on the stack, applied in order — "
                        f"later wins{RESET}"
                        f"  {BOLD}{WHITE}{narrowing}{RESET}{GREEN}█{RESET}")
                more = []
            elif open_here:
                # The row becomes the question while its choices are showing:
                # the old value, an arrow, and a live caret. Without the caret
                # the row reads as a label rather than as something typing
                # narrows, which is the whole reason filtering is on this line
                # and not on a prompt somewhere below the list.
                body = (f"{tint}{line.label:<{_MIRROR_LABEL}}{RESET}"
                        f"{tint}{line.flag:<{_MIRROR_FLAG}}{RESET}"
                        f"{DIM}{line.value or 'not set'}{RESET}"
                        f"{DIM}  →  {RESET}"
                        f"{BOLD}{WHITE}{narrowing}{RESET}{GREEN}█{RESET}")
                more = []
            elif moved and isinstance(now[row], (list, tuple)):
                # The `-c` stack, the one row whose new value is plural. Drawn
                # as the stack it will become, one ini per line and in order,
                # because that IS the value: `-c` is applied left to right and
                # later inis overrule earlier ones. Flattened onto the single
                # `old  →  new` line the other rows use, four inis run past the
                # width of the panel and the ordering that decides the run's
                # parameters is the first thing off the edge.
                new = list(now[row])
                body = (f"{GREEN}{line.label:<{_MIRROR_LABEL}}{RESET}"
                        f"{GREEN}{line.flag:<{_MIRROR_FLAG}}{RESET}"
                        f"{GREEN}{BOLD}{new[0] if new else '—'}{RESET}")
                more = [f"{GREEN}{BOLD}{ini}{RESET}" for ini in new[1:]]
            elif moved:
                body = (f"{tint}{line.label:<{_MIRROR_LABEL}}{RESET}"
                        f"{tint}{line.flag:<{_MIRROR_FLAG}}{RESET}"
                        f"{DIM}{line.value or '—'}{RESET}"
                        f"{DIM}  →  {RESET}{GREEN}{BOLD}{now[row]}{RESET}")
                more = []
            elif row in must:
                body = (f"{RED}{line.label:<{_MIRROR_LABEL}}{RESET}"
                        f"{RED}{line.flag:<{_MIRROR_FLAG}}{RESET}"
                        f"{DIM}{line.value or 'not set'}{RESET}"
                        f"{DIM}  →  {RESET}{RED}?{RESET}"
                        f"   {RED}{must[row]}{RESET}")
                more = []
            out.append(f"{gutter}{body}")
            out.extend(f"{pad}{extra}" for extra in more)

            if warn.get(row):
                colour, text = warn[row]
                for bit in str(text).splitlines():
                    out.append(f"{pad}{colour}{bit}{RESET}")

        return _elide(out, focus, _panel_height())

    return draw


def _action(verb, consequence):
    """One line of the gate's action block, on the mirror's own column grid.

    The verb sits where a mirror line's label and flag sit, and the consequence
    starts where its VALUE starts -- so the three things you can do read as the
    last three rows of the same table rather than as a paragraph stacked beneath
    one. The two columns already happened to be 17 wide in both places; this
    makes that deliberate, by measuring from the mirror's own constants.
    """
    return (f"      {WHITE}{verb:<{_MIRROR_LABEL + _MIRROR_FLAG}}{RESET}"
            f"{DIM}{consequence}{RESET}")


# ---------------------------------------------------------------------------
#  PROPOSED NEXT ACTIONS, SAID THE SAME WAY EVERYWHERE
# ---------------------------------------------------------------------------
# ONE DESCRIPTION PER COMMAND, IN ONE PLACE. These were written independently
# in six renderers, and they had drifted into six vocabularies for the same
# seven verbs: /jobs was "inspect its jobs" on one screen, "every job and its
# state" on two others and "for its jobs" on a fourth; /diagnose was "explain
# what went wrong", "read what the logs say", "investigate a problem" and "for
# what went wrong". A person reading those four screens in one session has no
# way to know they are being offered the same command, which is the entire
# thing a command vocabulary is for.
#
# THE DESCRIPTION TEACHES THE COMMAND, NOT THE SITUATION. "/diagnose  explain
# what went wrong" is true of /diagnose everywhere it is offered; "/diagnose
# read what the logs say" describes a mechanism, and "/diagnose  inspect the
# timeout in gatk_sam_to_fastq" would describe one run. The screen around the
# block is where a situation gets explained -- /check prints the failing step
# three lines above these, and that is the right place for it.
#
# WHAT IS DELIBERATELY NOT IN HERE. The gate's verb block (_VERBS["held"], via
# gate_box) keeps its own wording -- "submits to Slurm — cannot be undone" is a
# consequence, printed at the one screen where a wrong keystroke spends an
# allocation, and a safety line is not a description. See the note there.
_ACTION_TEXT = {
    # The investigation ladder, in the order it is climbed. Each rung answers
    # a question the one above it raised.
    "/check":     "see a run's current status",
    "/diagnose":  "explain what went wrong",
    "/jobs":      "inspect individual jobs",
    # Before anything is launched.
    # TWO ENTRIES, BECAUSE /modify GENUINELY DOES TWO THINGS. On a run still
    # at the gate it edits the proposal in place and asks again. On a run that
    # has already been submitted it FORKS -- a new run is built and gated, and
    # the launched one is not touched (see cli._cmd_modify). "change a run
    # before launch" is right for the first and misleading for the second: it
    # was printed under a /diagnose of a run that had been on the scheduler for
    # nineteen days, where there is no "before launch" left and nothing about
    # that run is going to change.
    #
    # This is the ONE sanctioned exception to "the same command is described
    # the same way everywhere", and it earns it on the rule's own terms: the
    # rule exists so a reader learns one meaning per command, and here the
    # behaviour really is two. Everything else still comes from one entry.
    "/modify":    "change a run before launch",
    "/modify@launched": "build a revised copy; this run is untouched",
    # A THIRD WORDING, AND THE LAST ONE. It is used in exactly one place: an
    # Actions block where /relaunch is also offered. There, "build a revised
    # copy; this run is untouched" describes what BOTH verbs do, so as the
    # label distinguishing them it says nothing -- and the thing a person is
    # actually choosing between is whose change goes in, the diagnosis's or
    # theirs. The copy is still made and the original is still untouched;
    # /relaunch's own line says so one row above, which is why this one can
    # spend its width on the distinction instead of repeating it.
    "/modify@else": "make different changes",
    # PREPARES, NEVER SUBMITS, and the description has to carry that or the
    # verb reads as "run it again". What it produces is another run waiting at
    # the gate; see cli._cmd_relaunch.
    #
    # PROPOSED, not "this". The diagnosis may say in the same breath that the
    # value it names is not established -- 35:00:00 was the pipeline's own
    # pre-CIT walltime, not a duration anything measured -- and a row reading
    # "with this fix" endorses it on the one screen where the caveat is still
    # two lines away. The word costs nothing and is the honest one.
    "/relaunch":  "prepare a retry with the proposed fix",
    "/approve":   "launch a run",
    "/reject":    "discard a run",
    # Bringing something in, and looking things up.
    "/scan":      "bring an existing run into the assistant",
    "/list":      "see your runs",
    "/view":      "see the command a run was built from",
    "/track":     "attach a job list by hand",
    "/history":   "see what is recorded about a run",
    "/monitor":   "watch a run until it stops changing",
}


def action_text(verb):
    """The canonical one-line description of a command, or "".

    Public because two callers outside this module offer a command in a place
    an Actions block does not fit -- a one-line confirmation, a hint under a
    small message -- and the words still have to be the same words.
    """
    return _ACTION_TEXT.get(verb, "")


# Statuses for which /modify forks rather than edits. Read off the record's
# own status, never guessed from the screen it is being drawn on.
_LAUNCHED = frozenset(("submitted", "submitting", "submit_failed",
                       "submit_unknown", "gone", "abandoned"))


def modify_text(status):
    """The right description of /modify for a run in `status`."""
    return _ACTION_TEXT["/modify@launched" if status in _LAUNCHED
                        else "/modify"]


def _action_rows(group):
    """[(command, argument, description)] with the canonical text filled in.

    A row may be given as (command,), (command, argument) or with an explicit
    description as the third field. The explicit form exists for the two places
    that genuinely need their own words -- see _VERBS -- and is not the way any
    ordinary screen should spell a verb it shares with five others.
    """
    rows = []
    for row in group:
        verb = row[0]
        arg = row[1] if len(row) > 1 else ""
        note = row[2] if len(row) > 2 else _ACTION_TEXT.get(verb, "")
        # `/modify@launched` is a lookup key, not something anybody types.
        rows.append((verb.split("@")[0], str(arg or ""), note))
    return rows


def actions(groups, gutter=" "):
    """Print an `Actions` block: a heading, then one line per command.

        <gutter> Actions
        <gutter>
        <gutter>   /diagnose  rnaseq-0810    explain what went wrong
        <gutter>   /jobs      rnaseq-0810    inspect individual jobs

    `groups` is a list of rows, or a tuple of such lists -- one blank line
    between groups, which is how /list separates "understanding a run" from
    "preparing one". `gutter` is whatever prefix the surrounding screen puts on
    every line: " " for a plain screen, the panel's own "  ▌" for /check and
    /diagnose, so a block dropped into a framed screen stays inside its frame.

    EXISTS SO SIX SCREENS CANNOT DISAGREE ABOUT WHAT A PROPOSED COMMAND LOOKS
    LIKE. They each formatted their own -- two loose lines here, a middle-dot
    run-on there, a heading and two padded columns somewhere else -- so the one
    thing every one of them was trying to say ("you could type this next") had
    no consistent shape to be recognised by. The heading is the shape.

    Deliberately small. It owns the heading, the spacing, the two columns and
    the canonical descriptions, and nothing else: which commands to offer is
    the caller's, because that is the part that has to change with the run's
    state. A screen with no useful next command calls nothing and prints no
    heading -- an empty Actions block is worse than none, because it promises
    a way forward and then does not name one.
    """
    if groups and isinstance(groups[0], (list, tuple)) and groups[0] \
            and isinstance(groups[0][0], (list, tuple)):
        blocks = [_action_rows(g) for g in groups]
    else:
        blocks = [_action_rows(groups)]
    blocks = [b for b in blocks if b]
    if not blocks:
        return
    every = [row for b in blocks for row in b]
    # Both columns measured across EVERY group, not per group, so the three
    # blocks of /list read as one list rather than as three that happen to be
    # stacked. cells() rather than len() because a run name may hold anything.
    w_cmd = max(len(verb) for verb, _, _ in every)
    w_arg = max(cells(arg) for _, arg, _ in every)
    gap = "  " if w_arg else ""
    # NARROW WINDOWS GET TWO LINES, NEVER A TRUNCATED ONE. `/diagnose
    # dnaseq-somatic-fastpass-0805    explain what went wrong` is 66 columns
    # before the gutter, and on a 60-column login-node window the terminal
    # soft-wraps the overflow to column ZERO -- left of and underneath the
    # panel edge it was supposed to sit beside. This is the fault _hint()
    # existed to fix for /diagnose's two lines, generalised: when a row will
    # not fit, the description drops to its own indented line rather than being
    # cut, because a description cut in half is the half that mattered.
    room = (terminal_cols() if _tty() else WIDTH) - cells(gutter) - 3
    fits = all(w_cmd + len(gap) + w_arg + 4 + len(note) <= room
               for _, _, note in every)
    print(f"{gutter} {BOLD}Actions{RESET}")
    for block in blocks:
        # rstrip, because a gutter that is only indentation (" ") would
        # otherwise leave trailing whitespace on a blank row -- which
        # `git diff --check` flags and which shows as a stray cell in a
        # terminal recording. A gutter that DRAWS something ends in an escape
        # sequence, so rstrip leaves the ▌ and the panel edge unbroken.
        print(gutter.rstrip())
        for verb, arg, note in block:
            # A PLACEHOLDER IS QUIET, A REAL NAME IS NOT. "<name>" is grammar --
            # it tells you the shape of the command and there is nothing to
            # copy. An actual run name is the thing somebody is about to type or
            # paste, so it gets the emphasis, which is also what makes
            # `/check all`'s footer and `/check rnaseq-0810`'s block visibly
            # different kinds of offer.
            tint = DIM if arg.startswith("<") or not arg else WHITE
            head = (f"{gutter}   {DIM}{verb:<{w_cmd}}{RESET}"
                    f"{gap}{tint}{arg}{RESET}")
            if fits:
                pad_arg = " " * max(0, w_arg - cells(arg))
                print(f"{head}{pad_arg}    {GREY}{note}{RESET}")
            else:
                print(head)
                print(f"{gutter}   {' ' * w_cmd}{gap}{GREY}{note}{RESET}")


def fill_header(m, row, changes, current, step="", note=""):
    """The command, collapsed to what matters while ONE row is being answered.

    The panel that picks the rows draws the whole mirror, and then the filling
    used to happen somewhere else entirely -- a stack of prompts scrolling down
    the terminal, each one asking about a command that was no longer on screen.
    By the seventh question somebody was editing a thing they could not see, and
    the answers they had already given were three screens up.

    So the mirror comes along. Not all of it: the full mirror plus a protocol
    list plus the prompt is past twenty-four rows, and once the terminal scrolls
    the repaint arithmetic that redraws this on every keystroke is wrong. What
    survives the collapse is exactly what is still being decided:

        the invocation      always. It is what the command IS, and every
                            remaining answer is read against it.
        rows already given  as `old → new`, in green. This is the running
                            record of the pass, and it is the thing the old
                            flow made people scroll to find.
        the row being asked  in red, showing what it says now, so the question
                            and the value it replaces are on screen together.

    Everything else is dropped rather than dimmed. A row nobody has touched and
    is not being asked about is not context here, it is nine lines between the
    question and the answer.
    """
    out = []
    if m and m.head:
        out.append(f"      {BOLD}{WHITE}{m.head}{RESET}")
        out.append("")

    for done, (was, now) in changes.items():
        was = was if was not in (None, "") else "—"
        out.append(f"    {GREEN}●{RESET} {GREEN}{done:<{_MIRROR_LABEL}}{RESET}"
                   f"{DIM}{_tilde(str(was))}{RESET}  {DIM}→{RESET}  "
                   f"{GREEN}{BOLD}{_tilde(str(now))}{RESET}")

    shown = current if current not in (None, "") else "not set"
    out.append(f"    {RED}●{RESET} {RED}{row:<{_MIRROR_LABEL}}{RESET}"
               f"{DIM}{_tilde(str(shown))}{RESET}  {DIM}→{RESET}  "
               f"{RED}?{RESET}")
    if note:
        out.append(f"      {' ' * _MIRROR_LABEL}{GREY}{note}{RESET}")
    out.append("")
    if step:
        out.append(f"    {DIM}{step}{RESET}")
        out.append("")
    return out


def gate(proposal, thread_id, blockers=(), warnings=(), changed=(),
         resources="", wanted=None):
    """Print the submission gate: what is about to run, and how to answer it.

    The three commands are printed here on purpose. The moment you are asked to
    make a decision is the worst moment to be recalling an API.

    Each one carries its consequence beside it. This is the single point in the
    product where consequences matter, and "approve" and "reject" are not
    self-explanatory when one of them spends an allocation and cannot be undone
    and the other quietly abandons a run.

    WHAT THE ACTION LINES DO NOT SAY. Not the run name, and not the argument
    list. Both used to be here -- `/approve mouse-rna-0803`, `/modify <name>
    <what to change>` -- which put the name on this screen four times and grew
    the instructions to eight lines under a six-line command. Neither is missing
    now, they have moved to where they are actually needed:

        the name        the prompt completes it. Type /approve and the name of
                        the held run appears after the caret in grey; tab takes
                        it (ui._Editor.ghost). A name you never have to type is
                        better than a name printed three times, and the mirror
                        still shows it once, on its own row, because /modify
                        points at rows.

        the arguments   ui._Editor._arghint already prints `/modify <name>
                        [change]` the moment you type the verb, which is the
                        moment you want it and not before. The box says what a
                        verb DOES; the prompt says what it TAKES.

    What is left is one line per verb, in the mirror's columns -- see _action().
    The consequence is the only text, so it is the only thing to read.

    `blockers` are environment findings that would make this submission fail no
    matter how good the command is. When there are any, the approve line is
    removed rather than merely annotated: offering an action that cannot work,
    next to an explanation of why it cannot work, invites trying it anyway.
    /modify and /reject stay, because both are still things you can usefully do.

    `warnings` are risks that do not block -- a step range that skips a
    dependency, a protocol switch that now wants a design file. They are shown
    here, at the moment of decision, and not earlier, because that is when
    somebody is actually reading.

    `changed` are the rows a /modify just moved, drawn green in the mirror. It
    is what makes the returning gate legible: the box is otherwise identical to
    the one rejected a moment ago, and having to diff two screens by eye to
    confirm a change landed is how somebody approves a command they did not
    mean to.
    """
    _flush_fold()

    print("\n")
    # READY TO SUBMIT, not HOLD, and not in red.
    #
    # Red-reverse reads as an error, and this is not one -- it is the moment a
    # complete, correct run is waiting for a decision. Saying HOLD in the
    # colour used for failed jobs and blocked environments framed the tool's
    # ordinary successful path as something going wrong, and spent the one
    # colour that should mean "you cannot proceed" on the screen where you
    # usually can.
    #
    # The irreversibility has not been softened away with it: it has moved to
    # where the irreversible thing actually is, in amber, on the /approve line.
    print(f"  {REVERSE}{BOLD} READY TO SUBMIT {RESET}  {BOLD}{thread_id}{RESET}")

    # What this run DOES, before what it is spelled as. The flags are exact and
    # the box would be complete without this, but "8 samples" and "writes into
    # the current directory" are the two facts somebody actually decides on,
    # and neither is legible from a path and an absent `-o`. See _consequence.
    print()
    # Read once and used twice -- in the summary above and in the /approve line
    # below, which has to stop promising an irreversible submission when the
    # script it would run contains no jobs.
    total = _job_total(proposal)
    for line in _consequences(proposal, thread_id, total):
        print(line)

    # `bash cmd.sh` is what runs and says nothing on its own -- two runs a week
    # apart submit the same three words -- so what that script was BUILT from is
    # the part worth reading, and with the agent's working folded away by
    # default this is the only place it is seen before it is approved. Laid out
    # by mirror.py rather than wrapped as prose: the people this is for know a
    # GenPipes command by its shape, and a paragraph does not have one.
    #
    # `missing` is no longer passed: an incomplete proposal does not reach the
    # gate at all now (see agent.submission_gate), so there is no absent-and-
    # required row left to draw.
    m = (mirror.read(proposal.get("generated"), name=thread_id,
                     resources=resources)
         or mirror.from_slots(proposal, name=thread_id, resources=resources))

    # EVERY FLAG THIS PIPELINE CAN TAKE, AND NO OTHERS.
    #
    # The box used to draw only the flags the command happened to WRITE, which
    # answers "what will run" and silently declines to answer "what could I
    # change". Those are both questions somebody has at the gate, and the second
    # one was being sent to /modify to discover -- so an unset `-s` was invisible
    # here and "all steps" appeared only as a phrase in the summary line.
    #
    # ensure() draws the absences, and modify.rows_for() decides which absences
    # are real questions FOR THIS PIPELINE. That distinction is the whole reason
    # this is safe to do: `-p` on a germline run is not a decision left unmade,
    # it is a flag that does not apply, and listing it would be how a gate turns
    # into a form. rows_for already filters by protocol -- pairs only where the
    # protocol pairs, design only where the pipeline needs one, no `-t` row at
    # all on a pipeline that takes none -- so what lands here is exactly the set
    # this run could legally differ in.
    #
    # One source, shared with the panel that does the editing: the rows offered
    # on this screen and the rows /modify will open are the same list, so
    # nothing is shown as changeable that cannot be changed.
    m = m.ensure([row for row, _ in modify.rows_for(proposal)])

    # ...and which of them GenPipes will not run without. `required` is the
    # unbracketed half of this pipeline's own `usage:` line -- argparse stating
    # its preconditions -- so it is the one claim on this screen that cannot go
    # stale against the install.
    #
    # After ensure(), not before, and the order is the point. ensure() draws the
    # absences; this marks them. A required flag that was never written has no
    # line until ensure() makes one, so marking first would annotate everything
    # except the case that matters.
    m = mirror.mark_required(m, proposal.get("required"))

    # `output` joins them only when it is UNSET, because the consequence line
    # above has just said where the run writes in words. Left in, the box says
    # "writes into the current directory" twice, three lines apart, which reads
    # as two separate findings. When `-o` IS set the row is the record of an
    # actual choice and stays.
    hide = mirror._GATE_HIDDEN + (
        () if (proposal.get("slots") or {}).get("output_dir") else ("output",))
    # ...and `script` stops being hidden the moment it stops being redundant.
    # _GATE_HIDDEN drops the `-g` row on the reasoning that it is the same word
    # as the `bash <script>` line below. When that is true it is noise. When it
    # is NOT true it is the single most important thing on the screen, and the
    # box was hiding it on the strength of an assumption it never checked:
    #
    #     script  -g   ampliconseq_cit_cmd.sh          <- what was built
    #     runs    bash ampliconseq_cit_test/ampliconseq_cit_cmd.sh   <- what ran
    #
    # Two paths, one of them written by nobody, and the row that would have
    # shown it was suppressed. The comparison has to be between the two
    # INDEPENDENT readings -- the `-g` value parsed out of the generation, and
    # the script named by the submission line -- since both are on the proposal
    # but only one of them comes from each command. Comparing `proposal["script"]`
    # against `proposal["command"]`, as this first did, compares a string with
    # the string it was extracted from and can never disagree.
    written = _flag_line_value(m, "script")
    submitted = str(proposal.get("script") or "")
    if written and submitted and (os.path.normpath(written)
                                  != os.path.normpath(submitted)):
        hide = tuple(row for row in hide if row != "script")
    drawn = mirror_lines(m, changed=changed, hide=hide, wanted=wanted)
    if drawn:
        print()
        for line in drawn:
            print(line)
        print()
    # Labelled and last. Unlabelled and first, it read as a second command to
    # run alongside the GenPipes call above it, rather than as the one line
    # that submits what the call built.
    print(f"      {DIM}{'runs':<{_MIRROR_LABEL}}{RESET}"
          f"{BOLD}{WHITE}{proposal.get('command', '?')}{RESET}")
    # Said because it is now true, and because it changes what the rows above
    # mean. Approving does not run a file that is already sitting there -- it
    # rebuilds it from the command in this box and then launches it, which is
    # what makes the box and the submission the same thing rather than two
    # things that are usually the same. A hand edit to the script does not
    # survive it, and somebody has to be told that before they approve.
    print(f"      {DIM}{'':<{_MIRROR_LABEL}}rebuilt from the command above, "
          f"then run{RESET}")
    print()

    for text in warnings or ():
        first, _, rest = str(text).partition("\n")
        print(f"      {AMBER}{'warning':<17}{RESET}{first}")
        for line in rest.splitlines():
            print(f"      {'':<17}{DIM}{line}{RESET}")
    if warnings:
        print()

    if blockers:
        for finding in blockers:
            print(f"      {RED}{'cannot submit':<17}{RESET}"
                  f"{finding.variable} {finding.problem}")
            print(f"      {DIM}{'fix':<17}{RESET}{WHITE}{finding.fix}{RESET}")
        print()
    elif proposal.get("missing"):
        # Unreachable through the graph: agent.submission_gate now sends an
        # incomplete proposal back to be finished rather than drawing it, so
        # the gate has one mode and this branch should never run.
        #
        # Kept anyway, and deliberately. This is the screen where a wrong
        # answer puts unapproved work on a cluster, and "the caller guarantees
        # it" is the assumption that eventually stops being true -- a second
        # entry point, a replayed record, a future refactor of the node. The
        # guard costs one comparison and removes the need to be right about
        # that. What is gone is the red required-row rendering, which was the
        # part that only made sense when this state was routine.
        pass
    else:
        # The one irreversible verb on the screen, and the only amber on it.
        # Amber where the risk actually is beats red across the whole header:
        # a warning that covers everything marks nothing.
        #
        # Unless it is not irreversible, which is the whole point of reading the
        # job total. A script declaring no jobs cannot reach the scheduler, and
        # "submits to Slurm — cannot be undone" printed three lines under
        # "nothing to submit" is the screen contradicting itself at the exact
        # moment somebody is deciding whether to trust it.
        if total == 0:
            print(_action("/approve", "runs the script — it has no jobs in it, "
                                      "so nothing reaches Slurm"))
        else:
            print(_action("/approve",
                          f"{AMBER}submits to Slurm — cannot be undone{RESET}"))

    print(_action("/modify", "rewrites the command and asks you again"))
    print(_action("/reject", "abandons this run; nothing is submitted"))
    print(f"      {'':<{_MIRROR_LABEL + _MIRROR_FLAG}}{DIM}tab completes the name{RESET}")
    print()
    print(f"  {DIM}Nothing has reached the scheduler.{RESET}")
    print("\n")


# The verb key for a submitted run whose jobs cannot be reached. Not a status
# -- the registry is right that the run is `submitted` -- but the verbs that
# make sense for it are a different set, and _VERBS is keyed by what you can
# DO rather than by what the record says.
_UNREACHABLE = "submitted:no-jobs"

# WHICH COMMANDS EACH STATUS SUPPORTS. Selection only -- the descriptions come
# from _ACTION_TEXT, because these are the same seven verbs the rest of the
# product offers and a run's status changes WHICH of them are worth offering,
# never what any one of them means. This table used to carry its own wording
# and had drifted: /diagnose was "read the logs and explain a failure" here,
# "read what the logs say" in /check and "explain what went wrong" in /list.
#
# ORDER IS THE INVESTIGATION LADDER where a run has already been launched
# (/check, then /diagnose, then the low-level view) and the pre-launch order
# everywhere else (/modify, then /approve, then /reject) -- the same order
# /list uses, so a person meets the verbs in one sequence wherever they are.
_VERBS = {
    # NOTE these three are ALSO what the gate offers, and the gate deliberately
    # does not read them from here -- see gate_box, where /approve carries a
    # consequence rather than a description because that screen is where the
    # allocation is actually spent.
    "held": [("/modify",), ("/approve",), ("/reject",)],
    # NO /approve. This is the whole point of the status existing: the verbs
    # under a run have to be the ones that will actually work, and offering an
    # approval that /approve would refuse is the contradiction that started
    # this. What is left is real -- the command is intact and /modify rebuilds
    # it into a proposal that can be approved.
    "lapsed": [("/modify",), ("/reject",)],
    # NO /diagnose, AND THAT IS A SEMANTIC POINT RATHER THAN A TRIM. /view asks
    # the scheduler nothing -- it is the "what IS this run" screen -- so it
    # cannot know whether anything went wrong, and /diagnose means "explain
    # what went wrong". Offering it under every submitted run proposed an
    # explanation for a run that may be queued, running, or finished cleanly.
    #
    # The ladder still reaches it, one rung later and on evidence: /view offers
    # /check, /check asks sacct, and /check offers /diagnose exactly when
    # something actually broke. Gating this on the record's cached last_check
    # was the alternative and was rejected -- that verdict can be three weeks
    # old, and this table is about what a run can SUPPORT, not about what
    # probably happened to it.
    "submitted": [("/check",), ("/modify@launched",)],
    # A run that submitted real jobs and left no manifest. NOT the same list as
    # `submitted`, because two of those three verbs cannot do anything here:
    # /check and /diagnose both need job ids to ask the scheduler about, and
    # there are none. /track is what recovers one; /modify builds a fresh run
    # from the command still on the record. See runs.jobs_are_unreachable.
    _UNREACHABLE: [("/track",), ("/modify@launched",), ("/history",)],
    "abandoned": [("/modify@launched",)],
    "gone": [("/modify@launched",), ("/history",)],
}


def _verbs_for(status, record=None):
    """The actions worth offering for this run, as (verb, consequence).

    Keyed off what the run can actually support rather than off its status
    word alone. The two came apart the moment reconciliation started recovering
    outcomes for runs with no manifest: their status is honestly `submitted`,
    and /check on them can only report that it cannot look.
    """
    if record is not None and runs.jobs_are_unreachable(record):
        return _VERBS[_UNREACHABLE]
    return _VERBS.get(status, ())


@canonical
def run_view(proposal, name, status, resources="", blockers=(), record=None):
    """/view -- what a run IS, drawn for any status rather than only at the gate.

    The same mirror the gate draws, deliberately. A second layout for the same
    command would be a second thing to learn and a second place for the two to
    disagree, and the mirror's whole argument is that people know a GenPipes
    command by its shape.

    What differs is the frame and the verbs. There is no READY TO SUBMIT
    banner -- nothing is being asked here, and a box that announces a decision
    to somebody who typed a read-only command teaches them to ignore it. The verbs come from the
    status, because offering /approve on a run that went to Slurm an hour ago
    is offering something that cannot happen.
    """
    _flush_fold()
    print()
    print(f"  {DIM}▌{RESET} {BOLD}{name}{RESET}  {DIM}·{RESET}  {DIM}{status}"
          f"{RESET}")
    # WHERE IT CAME FROM, on the screen somebody reads before approving it. A
    # revision's parent is otherwise only visible in the line /relaunch or
    # /modify printed once and in /history, and "which run is this a retry of"
    # is exactly the question /view exists to answer. Same table as
    # history_detail, so the two cannot drift apart.
    parent = str((record or {}).get("derived_from") or "")
    if parent:
        why = _DERIVED.get((record or {}).get("derived_reason"), "")
        print(f"  {DIM}▌{RESET}   {GREY}{why or 'derived from'} "
              f"{DIM}·{RESET}  {GREY}{parent}{RESET}")
    print()
    print(f"      {BOLD}{WHITE}{proposal.get('command', '?')}{RESET}")
    missing = proposal.get("missing") or ()
    m = (mirror.read(proposal.get("generated"), name=name, resources=resources,
                     missing=missing)
         or mirror.from_slots(proposal, name=name, resources=resources))
    # Same annotation the gate makes, for the same reason: /view is where
    # somebody goes to ask what a run IS, and "this flag is required" is part of
    # the answer whether or not a decision is being asked for.
    m = mirror.mark_required(m, proposal.get("required"))
    drawn = mirror_lines(m)
    if drawn:
        print()
        for line in drawn:
            print(line)
    print()
    for finding in blockers or ():
        print(f"      {RED}{'cannot submit':<17}{RESET}"
              f"{finding.variable} {finding.problem}")
        print(f"      {DIM}{'fix':<17}{RESET}{WHITE}{finding.fix}{RESET}")
    if blockers:
        print()
    offered = [row for row in _verbs_for(status, record)
               if not (row[0] == "/approve" and (blockers or missing))]
    if offered:
        actions([(verb, name) for (verb, *_) in offered])
        print()
        print(f"    {DIM}tab completes the name{RESET}")
    print()


def simulated(what):
    """The dev-mode warning, on its own so it can outlive the readiness line
    that used to carry it. `what` names what is being faked -- the cluster,
    the model, or both.

    Said loudly and on every single launch, never once and never folded away.
    A tool whose whole purpose is to be trusted with a cluster allocation must
    not leave you working out for yourself whether what you just watched was
    real: a submission that touched nothing looks exactly like one that did,
    right up until somebody goes looking for the jobs.

    Silent when nothing is simulated, so the caller can hand it whatever it
    has without asking first.
    """
    if not what:
        return
    print(f"  {AMBER}▌{RESET} {AMBER}{BOLD}dev mode{RESET}  {DIM}·{RESET}  "
          f"{GREY}{what} — nothing here touches a real cluster{RESET}")


def ready(source=None, model=None, fake=None):
    """The readiness line. No longer printed at startup -- welcome() is the
    last thing before the prompt now, and it says which model is configured --
    but kept whole for whatever wants to restate both facts in one line.

    It restates the model on purpose: on a first launch the banner printed
    before a key existed, so this is the first point at which the answer is
    actually known.
    """
    simulated(fake)
    print(f"  {GREEN}▌{RESET} {BOLD}ready{RESET}  {DIM}·{RESET}  "
          f"{GREY}{_identity(source, model)}{RESET}")


def post_approve(name, record):
    """What the approved command actually did, read off the reconciled record.

    THE WORD "submitted" APPEARS FOR EXACTLY ONE STATUS. It used to be printed
    whenever the graph came back unpaused, which is true of a thread that
    finished, one that died, and one that was never resumed -- so the single
    message in this product that claims something reached a shared cluster was
    the one making a claim it could not support.

    Four outcomes, four different next actions, because they are four genuinely
    different situations and the wrong remedy is expensive in each:

      submitted        monitor it.
      zero jobs        nothing to monitor. A real and successful result, so it
                       is not dressed up as a failure.
      failed           amber. Whether a retry is safe is NOT inferred from the
                       job count -- a job list with no new rows does not prove
                       no sbatch succeeded, since GenPipes creates the job and
                       appends its row as two separate statements. Retry is
                       offered only where Slurm itself was asked and was quiet.
      unknown          amber, and the honest one. Never quietly promoted to
                       either neighbour.
    """
    status = (record or {}).get("status")
    seen = (record or {}).get("jobs_seen")
    expected = (record or {}).get("expected_jobs")
    detail = (record or {}).get("outcome_detail") or ""
    print()

    if status == runs.SUBMITTED:
        if seen:
            head = f"submitted  \u00b7  {seen} job{'s' if seen != 1 else ''}"
        elif expected == 0 or seen == 0:
            head = "nothing to do \u2014 every step was already up to date"
        else:
            head = "submitted"
        print(f"  {DIM}\u258c {BOLD}{name}{RESET}  {DIM}\u00b7{RESET}  {DIM}{head}{RESET}")
        # ONLY WHERE THERE IS SOMETHING TO CHECK. A submission that created no
        # jobs is finished -- there is nothing on the scheduler for /check to
        # look at -- so it gets no Actions block rather than an empty gesture
        # towards one. Same rule /check and /list follow for `up to date`.
        if seen:
            gut = f"  {DIM}\u258c{RESET}"
            print(gut)
            actions([("/check", name)], gutter=gut)
        print()
        return

    if status in (runs.SUBMIT_FAILED, runs.SUBMIT_UNKNOWN):
        word = ("the submission failed" if status == runs.SUBMIT_FAILED
                else "the outcome is unknown")
        print(f"  {AMBER}\u258c {BOLD}{name}{RESET}  {DIM}\u00b7{RESET}  {AMBER}{word}{RESET}")
        if detail:
            print(f"  {AMBER}\u258c{RESET}   {DIM}{detail}{RESET}")
        if seen:
            print(f"  {AMBER}\u258c{RESET}   {WHITE}{seen} job{'s' if seen != 1 else ''} "
                  f"were recorded before it stopped{RESET}"
                  f"{DIM} \u2014 they are on the scheduler{RESET}")
        gut = f"  {AMBER}\u258c{RESET}"
        print(gut)
        if (record or {}).get("retry_safe"):
            # The scheduler was ASKED and came back empty -- the one condition
            # under which nothing is out there, so a rebuild is the next move.
            print(f"{gut}   {DIM}Slurm has no jobs from this attempt \u2014 it "
                  f"is safe to try again.{RESET}")
            print(gut)
            actions([("/modify@launched", name), ("/reject", name)],
                    gutter=gut)
        else:
            # THE DEFAULT, AND /check COMES FIRST. A failed submission is not a
            # failed run: what broke is the launch, so there are no pipeline
            # logs for /diagnose to read, and the question that actually
            # decides what to do next is whether anything reached the scheduler
            # before it died. Only /check can answer that, and answering it
            # wrong costs a pipeline run twice.
            print(f"{gut}   {DIM}Some jobs may already be queued. Check before "
                  f"resubmitting \u2014 approving again is how a pipeline gets "
                  f"run twice.{RESET}")
            print(gut)
            actions([("/check", name), ("/modify@launched", name)],
                    gutter=gut)
            print(gut)
            print(f"{gut}   {DIM}squeue -u $USER says the same thing from "
                  f"outside this tool{RESET}")
        print()
        return

    if status == runs.SUBMITTING:
        gut = f"  {AMBER}\u258c{RESET}"
        print(f"  {AMBER}\u258c {BOLD}{name}{RESET}  {DIM}\u00b7{RESET}  "
              f"{AMBER}still submitting{RESET}")
        print(f"{gut}   {DIM}The command was started and has not reported "
              f"back.{RESET}")
        # Same reasoning as the unsafe-retry branch above: what is unknown is
        # whether anything reached the scheduler, and /check is what asks.
        print(gut)
        actions([("/check", name)], gutter=gut)
        print()
        return

    print(f"  {DIM}\u258c {BOLD}{name}{RESET}  {DIM}\u00b7{RESET}  "
          f"{DIM}{status or 'no outcome recorded'}{RESET}")
    print()


def reconciled(settled):
    """What startup found and resolved about submissions left in flight.

    Printed only when there IS something -- a normal launch says nothing --
    because this is news by definition: a run was mid-submission when a session
    ended, and what became of it is the first thing worth knowing.

    Deliberately not amber unless it needs to be. A session killed after a
    complete submission is a successful run with an interrupted terminal, and
    dressing that in a warning colour would make the commonest cause of this
    (closing a laptop) look like a fault.
    """
    if not settled:
        return
    print()
    n = len(settled)
    print(f"  {DIM}▌ {BOLD}{n} run{'s' if n != 1 else ''}{RESET}"
          f"{DIM} {'were' if n != 1 else 'was'} still submitting when a "
          f"session ended. Reconciled:{RESET}")
    for name, outcome in settled:
        status = getattr(outcome, "status", None)
        seen = getattr(outcome, "jobs_seen", None)
        if status == runs.SUBMITTED:
            said = (f"submitted · {seen} job{'s' if seen != 1 else ''}"
                    if seen else "submitted")
            tint = ""
        elif status == runs.SUBMIT_FAILED:
            said, tint = "the submission failed", AMBER
        else:
            said, tint = "outcome unknown", AMBER
        print(f"  {DIM}▌{RESET}   {WHITE}{name}{RESET}  {DIM}·{RESET}  "
              f"{tint}{said}{RESET}")
        detail = getattr(outcome, "detail", "")
        if detail and status != runs.SUBMITTED:
            print(f"  {DIM}▌{RESET}     {DIM}{detail}{RESET}")
    # Said once, here, rather than per row: the reason any of this is worth
    # reading is that a run whose outcome is not established may already have
    # work queued, and approving it again is how a pipeline gets run twice.
    if any(getattr(o, "status", None) != runs.SUBMITTED for _, o in settled):
        print(f"  {DIM}▌{RESET}")
        print(f"  {DIM}▌{RESET}   {DIM}Nothing was retried. Check before "
              f"resubmitting:{RESET} squeue -u $USER")
    print()


def post_reject(name):
    """The counterpart for a rejection. Split out from post_approve, which no
    longer takes a boolean: the two messages had nothing in common but a bar."""
    print()
    print(f"  {DIM}\u258c {BOLD}{name}{RESET}  {DIM}\u00b7{RESET}  "
          f"{DIM}rejected, feedback sent{RESET}")
    print()
# ---------------------------------------------------------------------------
# Run status. GenPipes' log_report prints a flat list where a failure reads with
# exactly the same weight as a success, which is the wrong way round.
#
# The palette here is deliberately restrained: red is the only colour, and it
# appears only when something is wrong. Completed, running and pending are all
# just "normal", so they are all grey. The consequence is that a healthy run is
# entirely monochrome, and a broken one has exactly one red thing in it. Red
# means the same thing it means at the gate -- you are needed.
# ---------------------------------------------------------------------------

BAR_WIDTH = 46

# How many of a root cause's jobs /check names before it stops counting them
# out, and how wide their names get. Six is enough for the shapes that actually
# happen -- a tumour/normal pair, a handful of samples -- and short enough that
# the tally above it stays on screen. Past that the answer is /jobs, which
# exists to list all of them.
_CAUSE_JOBS = 6
_CAUSE_NAME_W = 34

# Imported, not restated. These lived here as a second copy of runs.BAD_STATES
# and runs.BROKE_STATES, which is two literals that have to be edited together
# forever and will not be. runs.py is stdlib-only, so this costs nothing.
from .runs import (BAD_STATES as _BAD, BROKE_STATES as _BROKE,             # noqa: E402
                   ACTIVE_STATES)
# The step-list statuses, imported rather than restated. no_step_list() picks
# its first line from one of them, and a second copy of the vocabulary here
# would be a second thing to keep in step with the parser that produces it.
from .modify import (STEPS_AMBIGUOUS, STEPS_UNPARSEABLE)                 # noqa: E402
from .runs import (HELD_BUCKET, LAPSED_BUCKET, ACTIVE_BUCKET,             # noqa: E402
                   ATTENTION_BUCKET,
                   FINISHED_BUCKET, UNAVAILABLE_BUCKET, CANCELLED_TAG,
                   list_bucket, list_line, list_tag)

_ORDER = ["COMPLETED", "RUNNING", "PENDING", "FAILED", "TIMEOUT",
          "CANCELLED", "NODE_FAIL", "OUT_OF_MEMORY", "PREEMPTED",
          "BOOT_FAIL", "DEADLINE", "UNKNOWN"]


def _bar(status):
    """The progress bar, one glyph per job state.

    Broke and cancelled are drawn differently on purpose: two red blocks among
    forty dim ones sends the eye to the cause rather than to the casualties,
    which is the whole shape of a GenPipes failure. A failure that would round
    away to zero width still gets one character -- one failed job out of six
    hundred is exactly the case you must not lose.
    """
    total = status.total or 1
    counts = status.counts or {}
    broke = sum(n for s, n in counts.items() if s in _BROKE)
    cancelled = counts.get("CANCELLED", 0)
    unknown = counts.get("UNKNOWN", 0)
    done = counts.get("COMPLETED", 0)
    live = counts.get("RUNNING", 0)

    def width(n, floor=0):
        w = int(round(BAR_WIDTH * n / total))
        return max(w, floor) if n else 0

    n_broke = width(broke, 1)
    n_unknown = width(unknown, 1)
    n_done = width(done)
    n_live = width(live)
    n_cancel = width(cancelled, 1)
    used = n_done + n_live + n_broke + n_cancel + n_unknown
    n_rest = max(BAR_WIDTH - used, 0)

    return (f"{WHITE}{'▓' * n_done}{RESET}"
            f"{WHITE}{'▒' * n_live}{RESET}"
            f"{RED}{'█' * n_broke}{RESET}"
            f"{RED}{FAINT}{'▒' * n_cancel}{RESET}"
            f"{RED}{'?' * n_unknown}{RESET}"
            f"{DIM}{'░' * n_rest}{RESET}")


def _tally_table(status, gutter):
    """The state table. Only states actually present get a row -- a column of
    zeroes is noise -- and the total row is there to prove the denominator."""
    print(f"{gutter}  {DIM}{'state':<20}{'number of jobs':>14}{'%':>8}{RESET}")
    print(f"{gutter}  {DIM}{'─' * 42}{RESET}")
    present = [s for s in _ORDER if s in status.counts]
    present += [s for s in status.counts if s not in _ORDER]
    for state in present:
        n = status.counts[state]
        colour = RED if state in _BAD or state == "UNKNOWN" else DIM
        emphasis = BOLD if state in _BROKE or state == "UNKNOWN" else ""
        pct = 100.0 * n / status.total if status.total else 0.0
        print(f"{gutter}  {colour}{emphasis}{state:<20}{RESET}"
              f"{colour}{n:>14}{pct:>8.1f}{RESET}")
    print(f"{gutter}  {DIM}{'─' * 42}{RESET}")
    print(f"{gutter}  {DIM}{'total':<20}{status.total:>14}{100.0 if status.total else 0.0:>8.1f}{RESET}")


def _job_tail(job, step):
    """A job name with its step prefix removed, trimmed from the LEFT if it is
    still too long.

    GenPipes names jobs `<step>.<sample>`, and under a heading that already says
    the step the prefix is thirty characters of the word you just read. Worse,
    it is thirty characters at the FRONT: truncating on the right turned
    `gatk_sam_to_fastq.tumorPair_COLO829N` and `...COLO829T` into two identical
    rows, deleting the one letter that said which was the tumour and which the
    normal. What distinguishes these names is always at the end, so that is the
    end that survives.
    """
    text = str(job or "?")
    if step and text.startswith(f"{step}."):
        text = text[len(step) + 1:]
    return text if len(text) <= _CAUSE_NAME_W else "…" + text[-(_CAUSE_NAME_W - 1):]


# The scheduler's word for a failure, as a phrase a row can be labelled with.
# Derived from sacct's State and nothing else -- these are translations, not
# interpretations, which is the whole reason /check may print them.
_BROKE_LABEL = {
    "TIMEOUT": "timed out",
    "OUT_OF_MEMORY": "out of memory",
    "NODE_FAIL": "node failed",
    "PREEMPTED": "preempted",
    "BOOT_FAIL": "boot failed",
    "DEADLINE": "past deadline",
    "FAILED": "failed",
}

_CAUSE_LABEL_W = 20


def _cause_row(label, body, gutter, colour=""):
    return print(f"{gutter}  {colour}{label:<{_CAUSE_LABEL_W}}{RESET}{body}")


def _cause_block(cause, gutter):
    """The failure, in four labelled rows, in causal order.

    WHY IT IS NOT ONE BLOCK UNDER "root cause" ANY MORE. It used to be, and
    everything in it hung off that one word: the step, the scheduler state, the
    limit, the individual jobs, and the downstream cancellations. Four of those
    are evidence about the failure and the fifth is its CONSEQUENCE, and a
    reader with no way to tell them apart is left asking whether the forty-three
    cancelled jobs are part of what went wrong. They are not. They are what went
    wrong next, and they are the reason a run that lost two jobs shows 93%
    cancelled.

    So one label per question, in the order somebody asks them:

        first failure    what broke, and how many of it       (step, count)
        walltime limit   what the scheduler measured it       (Timelimit)
                         against -- only when there IS one
        timed out        WHICH jobs, and what each of them     (Elapsed,
                         actually did                           MaxRSS)
        impact           what followed from it                 (CANCELLED)

    "FIRST FAILURE", NOT "ROOT CAUSE", and the change is not cosmetic. What
    runs._root_cause computes is the earliest job that broke on its own --
    orderable, checkable, and entirely within what sacct reports. "Root cause"
    claims to have found the reason, which is a claim about logs and about
    science, and this screen has read neither. /diagnose is where that claim
    may be made; the line offering it is three rows below.

    The `impact` wording keeps its one inference and keeps it visible:
    "downstream" is read off the shape of a GenPipes DAG, not off sacct, which
    reports a cancellation without saying what cancelled it.
    """
    print(gutter)
    state = cause.get("state") or ""
    kind = _BROKE_LABEL.get(state, str(state or "failed").lower().replace("_", " "))
    count = cause.get("count") or 1
    _cause_row("first failure",
               f"{WHITE}{cause['step']}{RESET}  {DIM}·{RESET}  "
               f"{count} job{'s' if count != 1 else ''} {kind}",
               gutter, RED)

    # Only for a state that WAS measured against a limit. sacct reports
    # Timelimit on every job; printing it under an out-of-memory failure would
    # offer the wrong number as the explanation.
    if cause.get("timelimit"):
        _cause_row("walltime limit", f"{DIM}{cause['timelimit']}{RESET}", gutter)

    # The jobs themselves, under a heading that says what the list IS. Named
    # here rather than left to /jobs, because a failure without its jobs is a
    # count: "2 jobs timed out" does not say that one is the tumour and one the
    # matched normal, or that they died 28 seconds apart. Capped, because one
    # step failing across ninety samples is a normal shape and printing ninety
    # rows buries the tally above it.
    listed = cause.get("jobs") or []
    for i, job in enumerate(listed[:_CAUSE_JOBS]):
        # "ran 00:01:01", not a bare "00:01:01" in an unlabelled column. The
        # number sat two rows under a limit in the same format and there was
        # nothing on screen saying which was which.
        detail = f"ran {job['elapsed']}" if job.get("elapsed") else ""
        if job.get("maxrss"):
            detail = f"{detail}  peak {job['maxrss']}".strip()
        _cause_row(kind if i == 0 else "",
                   f"{DIM}{_job_tail(job.get('name'), cause['step']):<{_CAUSE_NAME_W}}"
                   f"{RESET}{DIM}{detail}{RESET}",
                   gutter)
    if len(listed) > _CAUSE_JOBS:
        _cause_row("", f"{GREY}+{len(listed) - _CAUSE_JOBS} more{RESET}", gutter)
    if cause.get("maxrss") and not listed:
        _cause_row(kind, f"{DIM}peak memory {cause['maxrss']}{RESET}", gutter)

    if cause.get("cancelled_after"):
        n = cause["cancelled_after"]
        _cause_row("impact",
                   f"{DIM}{n} job{'s' if n != 1 else ''} cancelled downstream "
                   f"— they never started{RESET}",
                   gutter)


@canonical
def run_status(name, status):
    """/check <name> -- what the scheduler says this run is doing.

    Everything drawn here comes from runs.resolve(), which asks sacct and, only
    when something is still in the queue, squeue. Nothing is read off the
    filesystem, which is the entire point: the artifacts GenPipes leaves are
    written by the jobs themselves, so a job that never started and a job that
    was killed leave the same trace as a job that has not got to it yet.

    The footer is provenance, not decoration. It names which tools were actually
    queried and how many of the manifest's jobs they accounted for, so
    "44/46 resolved · 2 UNKNOWN" is visible rather than rounded away into a
    percentage that looks fine.
    """
    gutter = f"  {DIM}▌{RESET}"

    if status.source == "unavailable":
        print()
        print(f"{gutter} {BOLD}{name}{RESET}  {DIM}·{RESET}  "
              f"{RED}could not reach the scheduler{RESET}")
        print(gutter)
        print(f"{gutter}   {DIM}{status.total} job(s) in the manifest, no state "
              f"for any of them{RESET}")
        print(f"{gutter}   {DIM}nothing is guessed from files on disk — see "
              f"runs.resolve{RESET}")
        print()
        return

    if not status.total:
        print()
        print(f"{gutter} {BOLD}{name}{RESET}  {DIM}·{RESET}  {DIM}no jobs{RESET}")
        print(gutter)
        print(f"{gutter}   {DIM}the job list is empty — nothing was submitted "
              f"under this name{RESET}")
        print()
        return

    verdict = (f"{RED}{status.verdict}{RESET}"
               if (status.doomed or any(s in _BAD for s in status.counts)
                   or status.unknown)
               else f"{DIM}{status.verdict}{RESET}")

    print()
    print(f"{gutter} {BOLD}{name}{RESET}  {DIM}·{RESET}  {verdict}")
    print(gutter)
    print(f"{gutter}  {_bar(status)}  {BOLD}{status.percent:.0f}% done{RESET}")
    print(gutter)
    _tally_table(status, gutter)

    cause = status.root_cause
    if cause:
        _cause_block(cause, gutter)

    if status.reasons:
        print(gutter)
        for i, (reason, n) in enumerate(sorted(status.reasons.items(),
                                               key=lambda kv: -kv[1])):
            label = "waiting on" if i == 0 else ""
            doomed = reason == "DependencyNeverSatisfied"
            colour = RED if doomed else DIM
            note = ("these will never run" if doomed else
                    "queued, waiting for a slot" if reason == "Priority" else
                    "upstream steps not finished yet" if reason.startswith("Depend")
                    else "")
            print(f"{gutter}  {DIM}{label:<20}{RESET}{colour}{n:>3}  "
                  f"{reason:<26}{RESET}{DIM}{note}{RESET}")

    for job in status.at_risk or ():
        print(gutter)
        print(f"{gutter}  {AMBER}⚠{RESET}  {WHITE}{job.name}{RESET}   "
              f"{DIM}{job.elapsed} of {job.timelimit}   near its limit{RESET}")

    # WHAT TO DO NEXT, CHOSEN FROM WHAT THE EVIDENCE SUPPORTS. Three cases,
    # and the third is the one that used to be wrong by omission.
    #
    #   something broke        /diagnose, then /jobs. There are logs, because a
    #                          job that broke ran far enough to write one.
    #   jobs unaccounted for   /jobs ONLY. status.unknown is jobs in THIS
    #                          MANIFEST that sacct would not account for -- the
    #                          shape an accounting database aging ids out
    #                          leaves. There is no log for /diagnose to read,
    #                          so offering it would send somebody to a screen
    #                          that can only report finding nothing.
    #
    #                          NOT to be confused with /list's `? unknown`,
    #                          which is a wider display-level state with six
    #                          sources; five of them never reach this function
    #                          with a manifest at all, and the next action for
    #                          `? unknown` on a listing row is /check -- which
    #                          is what /list offers. This branch is what /check
    #                          then says for the one source that does arrive
    #                          here.
    #   anything else          nothing. A run that is queued, running, finished
    #                          cleanly or had no work to do has no next command
    #                          worth naming, and an Actions block that exists
    #                          for symmetry teaches people to stop reading it.
    #
    # /check ITSELF IS NEVER IN THIS LIST. The reader is already inside it.
    broke = any(s in _BROKE for s in status.counts) or status.doomed
    if broke:
        print(gutter)
        actions([("/diagnose", name), ("/jobs", name)], gutter=gutter)
    elif status.unknown:
        print(gutter)
        actions([("/jobs", name)], gutter=gutter)

    print(gutter)
    resolved = (f"{RED}{status.resolved}/{status.total} jobs resolved · "
                f"{status.unknown} UNKNOWN{RESET}" if status.unknown
                else f"{status.resolved}/{status.total} jobs resolved")
    print(f"{gutter}  {DIM}{status.source}  ·  {resolved}{DIM}  ·  {status.at}{RESET}")
    print()


def status(name, parsed, raw=""):
    """Draw a run's progress from log_report's already-parsed counts.

    Kept for /diagnose and for the fake-cluster tests, which still exercise the
    log_report path. Nothing on the /check path reaches this any more -- see
    run_status() and the comment above runs.resolve() for why.

    The parsing lives in runs.parse_log_report -- this only draws. If no total
    was found the raw text is printed unchanged: better to show something
    unexpected than to hide it behind an empty bar.
    """
    counts, total, meta = parsed["counts"], parsed["total"], parsed["meta"]

    # Nothing recognisable -- show the raw output rather than swallow it.
    if not total:
        body = (raw or "").strip() or "log_report returned nothing."
        print(f"\n{DIM}{body}{RESET}\n")
        return

    done = counts.get("COMPLETED", 0)
    live = counts.get("RUNNING", 0)
    bad = sum(v for k, v in counts.items() if k in _BAD)

    # The bar: done, then failed, then the remainder. Failures are drawn even
    # when they would round away to nothing -- a single failed job out of
    # hundreds still has to be visible.
    n_done = int(round(BAR_WIDTH * done / total))
    n_bad = int(round(BAR_WIDTH * bad / total))
    if bad and n_bad == 0:
        n_bad = 1
    n_rest = max(BAR_WIDTH - n_done - n_bad, 0)

    bar = (f"{WHITE}{'\u2593' * n_done}{RESET}"
           f"{RED}{'\u2588' * n_bad}{RESET}"
           f"{DIM}{'\u2591' * n_rest}{RESET}")

    # The one-glance verdict, so a broken run announces itself.
    if bad:
        verdict = f"{RED}{bad} need attention{RESET}"
    elif live:
        verdict = f"{DIM}{live} running{RESET}"
    else:
        verdict = f"{DIM}complete{RESET}"

    print()
    print(f"  {DIM}\u258c {BOLD}{name}{RESET}  {DIM}\u00b7{RESET}  {verdict}")
    print(f"  {DIM}\u258c{RESET} {bar} {BOLD}{100 * done / total:.0f}%{RESET}")
    print(f"  {DIM}\u258c{RESET}")
    for state in _ORDER:
        if state in counts:
            label = RED if state in _BAD else DIM
            value = BOLD if state in _BAD else ""
            print(f"  {DIM}\u258c{RESET}   {label}{state.lower():<11}{RESET}"
                  f"{value}{counts[state]:>4}{RESET}{DIM} / {total}{RESET}")
    if meta:
        print(f"  {DIM}\u258c{RESET}")
        for label, value in meta:
            print(f"  {DIM}\u258c{RESET}   {DIM}{label:<14}{RESET}{DIM}{value}{RESET}")
    print()

# ---------------------------------------------------------------------------
# Small messages. Every command that can fail goes through problem() or
# nothing() rather than printing its own line, so a failure always looks the
# same and always offers the next thing to type.
#
# The hint is the point. "No run named 'patient-4'" is a dead end; the same
# message plus "/list shows what there is" is a next step. A tool that answers
# questions should not answer one with silence.
# ---------------------------------------------------------------------------

def problem(text, hint=None):
    """Something the user asked for could not be done."""
    print()
    print(f"  {RED}\u258c{RESET} {text}")
    if hint:
        print(f"  {RED}\u258c{RESET} {GREY}{hint}{RESET}")
    print()


def output(text):
    """What a command printed, when a message about it is not enough.

    Used where this tool ran something itself rather than through the graph --
    a failed regeneration at /approve -- because the transcript's own OUT block
    only exists for blocks the model executed. Clipped like every other piece
    of machine output here, and silent when there is nothing to show: an empty
    frame under an error message reads as output that was lost.
    """
    body = (text or "").strip()
    if not body:
        return
    for line in _clipped(body).splitlines():
        print(f"  {GREY}{line}{RESET}")
    print()


def nothing(text, hint=None):
    """A legitimately empty answer. Grey, not red -- an empty list is not an
    error, and colouring it like one trains people to ignore red."""
    print()
    print(f"  {DIM}\u258c{RESET} {DIM}{text}{RESET}")
    if hint:
        print(f"  {DIM}\u258c{RESET} {GREY}{hint}{RESET}")
    print()


def interrupted(hint=None, note=None):
    """Ctrl-C, said the way an interruption should be said.

    NOT AN ERROR AND NOT A FAILURE. Stopping a reply is an ordinary thing to do
    and the screen has to read that way: no red, no traceback, no apology. It
    used to print "Stopped." over a bare claim that nothing had been submitted,
    which was two problems at once -- it looked like a crash report, and the
    reassurance underneath it had been checked by nobody.

    `hint` is what is actually known about the scheduler, and it is passed in
    rather than written here because only the caller has the evidence. `note`
    is for the rarer thing worth saying: a tool that was still finishing when
    the prompt came back.
    """
    print()
    print(f"  {DIM}⎿{RESET} {DIM}Interrupted{RESET}"
          f"{DIM} · what should I do instead?{RESET}")
    if note:
        print(f"  {DIM}⎿{RESET} {AMBER}{note}{RESET}")
    if hint:
        print(f"  {DIM}⎿{RESET} {GREY}{hint}{RESET}")
    print()


def done(text, hint=None):
    """A completed action, confirmed."""
    print()
    print(f"  {GREEN}\u258c{RESET} {text}")
    if hint:
        print(f"  {GREEN}\u258c{RESET} {GREY}{hint}{RESET}")
    print()


def tracked(name, path):
    done(f"Tracking {BOLD}{name}{RESET}", os.path.basename(path))


def cancelled(name, n, raw=""):
    """The result of a /cancel. Says nothing was running when nothing was, rather
    than reporting a success that didn't happen."""
    if not n:
        nothing(f"Nothing left to cancel in '{name}'.",
                "every job has already finished or been cancelled.")
        return
    done(f"Cancelled {BOLD}{n}{RESET} job(s) in {BOLD}{name}{RESET}")
    body = (raw or "").strip()
    if body:
        for line in body.splitlines()[:4]:
            print(f"  {GREY}{line}{RESET}")
        print()


# ---------------------------------------------------------------------------
# Run and job listings.
#
# The distinction between the two is carried visually, not just in wording: a
# run is a titled block, a job is a row in a table. That is the difference the
# tool most needs its user to internalise -- you approve and cancel runs, but
# only ever diagnose jobs -- so the two never look interchangeable.
# ---------------------------------------------------------------------------

_STATUS_TAG = {
    "held": lambda: f"{RED}{BOLD}held{RESET}",
    # Grey and unbolded, because the one thing this state must not do is look
    # like a decision waiting to be made. It is a proposal whose decision is
    # gone; the verb underneath is /modify, and the tag has to agree with it.
    "lapsed": lambda: f"{GREY}no longer at the gate{RESET}",
    "submitted": lambda: f"{GREEN}live{RESET}",
    "gone": lambda: f"{DIM}gone{RESET}",
    # Terminal, and grey rather than red: it is not a problem, it is a decision
    # somebody made. It only ever appears in /history -- /list filters it out,
    # which is the entire reason the status exists.
    "abandoned": lambda: f"{DIM}abandoned{RESET}",
}


def _tag(record):
    return _STATUS_TAG.get(record.get("status"), lambda: f"{DIM}?{RESET}")()


# ---------------------------------------------------------------------------
# TABLES THAT HOLD COLOURS ARE FUNCTIONS, NOT DICTS, and the reason is one
# rule with teeth: a module-level dict built from RED and GREY captures the
# STRINGS those names had at import, and retheme() rebinds the names. A table
# that captured them keeps painting in the palette the session started with
# while everything around it changes -- which is how a NO_COLOR screen ends up
# with exactly one escape sequence left on it, in the marker column.
#
# So every table below that mentions a colour is a call. _STATUS_TAG already
# worked this way (its values are lambdas); these are the same idea written
# the same way. The cost is a dict literal per row rendered, which is nothing
# next to the print it feeds.
# ---------------------------------------------------------------------------


def _tag_colours():
    return {
        HELD_BUCKET: RED,
        LAPSED_BUCKET: AMBER,
        ACTIVE_BUCKET: GREEN,
        ATTENTION_BUCKET: RED,
        FINISHED_BUCKET: DIM,
        UNAVAILABLE_BUCKET: GREY,
    }

# The marker column for a /list row: one glyph and one colour per state.
#
# Both, not either. Colour is what makes the states separable at a glance, and
# it is also the half that does not survive -- a screenshot pasted into an
# email, a colourblind reader, `/list | tee log.txt`. The glyph carries the
# meaning on its own and the colour makes it fast, which is the same reasoning
# _plan_body already states about its own marker column.
#
# The pair is spent twice per row and nowhere else: on the glyph, and on the
# STATUS phrase that ends the row. Nothing between them is coloured -- the name
# is bold, the pipeline grey, the counts plain -- so a row reads as one state
# stated at both ends rather than as four competing highlights.
#
# SEVEN MARKS FOR SIX BUCKETS, because two buckets hold outcomes that must
# never be confused. FINISHED holds a run somebody stopped as well as two that
# succeeded -- tagging a cancellation with a green tick would report it as a
# success. ATTENTION holds a confirmed failure and a state nobody could
# establish, which ask for opposite next actions. _marks() below is the BUCKET
# default; _row_state is where a sub-state overrides it, and it is the only
# place that decides.
#
# A GLYPH MAY COVER TWO PHRASES, and three of them do. That is not a lapse: the
# pair always belongs to one idea and differs in something the reader acts on
# -- ● is wait-and-wait-while-watching-the-age, ✓ is work-happened-or-was-not
# -needed, ✗ is there-are-logs-or-there-are-not. What no glyph may do is cover
# two ideas, which is what ✗ was doing across a confirmed and an unestablished
# submission, and what ? was doing across LAPSED and genuine uncertainty.
_HELD_MARK = "◇"          # ◇ waiting on you
_REBUILD_MARK = "↻"       # ↻ a proposal that has to be rebuilt before it can go
# ● AND NOT ▶, WHICH IS A BUTTON. Every media control on earth uses ▶ for
# "press to start", so on a row describing a run that has ALREADY been launched
# it says the opposite of what is true -- and it reads as an affordance on a
# screen where the glyph column is not clickable and never was. ● is the status
# -LED convention instead: a filled dot means live, and it needs no learning.
# It is also the only symmetric mark that fits here; ▶ pointed off the edge of
# its cell and was the one glyph breaking the column's optical rhythm.
#
# The dot ▪ that "terminal with nothing to do" used to carry has LEFT this
# column -- see _row_state, where a zero-job run joins the ✓ family -- so there
# is no longer a lighter circle for ● to be confused with.
_LIVE_MARK = "●"          # ● live: the cluster has it
_BROKE_MARK = "✗"         # ✗ something failed
_DONE_MARK = "✓"          # ✓ finished, and the outcome is good
_STOPPED_MARK = "⊘"       # ⊘ stopped on purpose
# ? MEANS ONE THING AND IT IS NOT "SOMETHING ODD HERE": there is not enough
# authoritative evidence to make a stronger claim. It is a statement about what
# is KNOWN, never about what happened.
#
# It used to be spent on two unrelated ideas. The other one was LAPSED -- a
# proposal whose gate interrupt is gone -- and nothing about that state is
# unknown: the command is on record, complete, and the only missing thing is
# the authorisation slot, which is precisely why it cannot be approved and has
# to be rebuilt. Marking a fully-established state with the uncertainty glyph
# taught the glyph to mean "unusual", which is how a reader stops being able to
# tell it from the two rows where the tool genuinely cannot see.
#
# So LAPSED has ↻, which says the same thing its STATUS column says -- rebuild
# it -- and ? is left to the states that earn it, every one of which is a
# sentence about what could not be established rather than about what happened:
#
#   scheduler unreachable      nothing about these jobs is known right now
#   no job manifest on disk    it submitted N jobs; there is no list to ask about
#   jobs sacct will not own    the accounting database aged the ids out
#   submit_unknown             the submission's outcome was never established
#   submitting                 the launch was written down and never came back
#   submitted, never counted   a legacy record; absent is not zero
#
# ALL SIX SAY THE SAME WORD, and that is a deliberate display-level collapse.
# They reached the screen as three different phrases -- "submission
# unconfirmed", "submission interrupted" and "unknown" -- which is three
# vocabulary items for one fact ("this dashboard cannot establish the state")
# and one next action (/check). Two of the three differed from each other only
# in WHERE OUR OWN PROCESS DIED, which is not a fact about anybody's run.
#
# WHAT THE COLLAPSE IS NOT. Nothing underneath merges. SUBMITTING and
# SUBMIT_UNKNOWN remain distinct registry statuses, jobs_are_unreachable and
# `source == "unavailable"` remain distinct findings, and reconcile(),
# /check, /history and the startup reconciliation all keep reading them
# exactly as before. This is one column on one screen choosing one word;
# the evidence model is untouched, and /check is where the six diverge again.
#
# The three submission rows are also the ones that moved here from ✗. They used
# to be drawn in red, identically to a confirmed failure, because
# runs.list_bucket files them under ATTENTION and ATTENTION owned the glyph --
# see the long note above _row_state, which is where that is now decided.
_UNKNOWN_MARK = "?"       # ? not enough evidence to say more

def _marks():
    return {
    HELD_BUCKET: (_HELD_MARK, AMBER),
    # Grey, not amber. A lapsed proposal is not urgent and not broken -- it is
    # simply not a decision any more, and colouring it like one that is would
    # put it back in the queue this whole change exists to clear.
    LAPSED_BUCKET: (_REBUILD_MARK, GREY),
    # Bold green, not cyan. Cyan is unreadable on a light terminal, and a run
    # that is running is not a fourth kind of thing -- it is healthy, like a
    # finished one, and doing something, unlike a finished one. Green says the
    # first; the weight and the glyph (\u25cf against \u2713) say the second.
    ACTIVE_BUCKET: (_LIVE_MARK, BOLD + GREEN),
    ATTENTION_BUCKET: (_BROKE_MARK, RED),
    FINISHED_BUCKET: (_DONE_MARK, GREEN),
    UNAVAILABLE_BUCKET: (_UNKNOWN_MARK, GREY),
    }


# ---------------------------------------------------------------------------
#  THE PRIMARY STATE OF A RUN, WHICH IS THE ONLY THING /list ANSWERS
# ---------------------------------------------------------------------------
# /list is a dashboard. The question it exists to answer is "what state is
# each run in", once, per row, in the same words every time. It is NOT a
# diagnosis, and every attempt to make it one has cost it the thing it was for:
# the STATUS column had grown a `· <reason>` tail carrying the step that broke,
# the count that broke, how many jobs went out before a submission died, and
# what the scheduler last said -- all of it then truncated at the column edge
# to "failed · 2× timeout in gatk_sam_to_…", which is simultaneously too much
# detail for a listing and too little to act on.
#
# The layers underneath it already answer those questions properly:
#
#     /list             what state is everything in
#     /check <name>     what is happening with this run, with the tally
#     /diagnose <name>  why it broke and what to do
#     /jobs <name>      the individual scheduler jobs
#
# So the tail is gone, the reserved width came DOWN rather than up (see
# run_list), and every phrase below is short enough to be printed whole. If a
# phrase here ever needs an ellipsis, it is the wrong phrase.
#
# WHAT THE GLYPH MEANS, AND WHY EACH ROW HAS THE ONE IT HAS. The glyph is the
# PRIMARY STATE, never the reason for it, and the word beside it is the same
# state said in words -- which is what makes the STATUS column its own legend
# and why there is no legend block on this screen.
#
#   ◇  waiting on a person       the only row that stops until somebody acts
#   ↻  waiting on a rebuild      established, but not approvable as it stands
#   ▶  the cluster has it        queued or running, confirmed by the scheduler
#   ✓  finished, cleanly
#   ⊘  finished, because somebody stopped it
#   ·  finished, with nothing to do
#   ✗  something went wrong      a CONFIRMED failure, and nothing else
#   ?  not enough evidence       a statement about what is KNOWN, never about
#                                what happened
#
# THE ONE THAT WAS WRONG, and the reason this function exists rather than a
# bucket lookup. `submit_unknown` and `submitting` were rendered ✗ in red,
# identically to a confirmed failure -- because list_bucket files all three
# submission-trouble statuses under ATTENTION, and ATTENTION owned the glyph.
# runs.py is explicit that SUBMIT_UNKNOWN "is the honest state, and the one
# that must never be quietly upgraded to either neighbour", and a red cross IS
# that upgrade: it tells a reader the submission definitely did not happen,
# when what we actually have is a submission whose outcome was never
# established and which may have put a full pipeline on the cluster. Those two
# ask for opposite next actions. They get ? now, and only SUBMIT_FAILED -- the
# submission command itself running and reporting failure -- keeps the cross.
#
# The same correction applies inside a resolved run: jobs the scheduler will
# not account for (status.unknown) used to read "failed · 12 unaccounted for".
# Nothing failed. sacct did not recognise the ids, which is what happens when
# an accounting database ages them out, and the honest word for it is unknown.
#
# NOTHING HERE RE-DERIVES A RUN'S STATE FROM ANYTHING NEW. The bucket is still
# runs.list_bucket()'s, the submission statuses are still the registry's, and
# the two claims that are stronger than a bucket -- "these jobs cannot be
# reached" and "this submission created no jobs" -- are runs.py predicates over
# recorded evidence (jobs_are_unreachable, submitted_nothing). This function
# chooses words and glyphs; it establishes nothing.


# What each "the submission did not finish cleanly" registry status says about
# itself, and which glyph that earns. Worded as the EVENT, because that is what
# is known: none of the three has a job tally to report.
#
# Glyphs rather than colours, because the colour has to be read at call time --
# retheme() rebinds RED and GREY, and a module-level table freezing them would
# survive a palette change with the old escape sequences in it. _row_state
# pairs each glyph with its colour there. Same reason _marks() is a function.
_SUBMISSION_STATE = {
    # The command ran and reported failure. Confirmed, so it keeps the cross,
    # and it keeps its own phrase -- it is the one member of this group whose
    # next action differs. `failed` means jobs broke and /diagnose has logs to
    # read; this means the LAUNCH broke and /diagnose has nothing, so the
    # useful question is "did anything get out first", which is /check's.
    # What reached the scheduler is /check's to explain and reconcile()'s
    # `retry_safe` to answer; the uncertainty here is about what escaped, never
    # about whether it failed.
    "submit_failed": ("submission failed", _BROKE_MARK),
    # The outcome could not be established. NOT a failure, and the display must
    # not round it into one.
    "submit_unknown": ("unknown", _UNKNOWN_MARK),
    # /approve was accepted, the record was written before the irreversible
    # act, and nothing ever came back to reconcile it. Also unestablished, and
    # it says the same word as its neighbour above on purpose: the two differ
    # only in where this tool's own process died, which is not a fact about the
    # run. The registry keeps them apart; this column does not need to.
    "submitting": ("unknown", _UNKNOWN_MARK),
}

# EVERY PHRASE THE STATUS COLUMN CAN PRINT, with the glyph it is printed
# beside. Written down rather than left implicit in the branches below for
# three reasons, and all three are things that used to go wrong quietly:
#
#   the column sizes itself from it   w_status is max(len(word)) -- so a phrase
#                                     can never be added that does not fit, and
#                                     the table can never reserve width for a
#                                     phrase that no longer exists.
#   the mapping is assertable         a suite can check every state is reachable
#                                     and that no two states share a glyph
#                                     without agreeing on what the glyph means.
#   it is the vocabulary, in one place. This IS the legend. There is no legend
#                                     block on the screen because each word sits
#                                     beside its own glyph on every row, which
#                                     is a legend that cannot go out of date.
_STATE_WORDS = (
    ("waiting for approval", _HELD_MARK),     # a person has to decide
    ("needs rebuilding",     _REBUILD_MARK),  # established, not approvable
    ("queued",               _LIVE_MARK),     # the scheduler has it
    ("running",              _LIVE_MARK),     # ...and has started it
    ("completed",            _DONE_MARK),     # finished; the work ran
    ("up to date",           _DONE_MARK),     # finished; there was no work
    ("stopped",              _STOPPED_MARK),  # finished, somebody cancelled
    ("failed",               _BROKE_MARK),    # confirmed: jobs broke
    ("submission failed",    _BROKE_MARK),    # confirmed: the launch broke
    ("unknown",              _UNKNOWN_MARK),  # not enough evidence to say
)


def _row_state(bucket, record, status):
    """(glyph, colour, word) -- the primary state of one /list row.

    THE single place a listing row's state is decided, so the glyph and the
    STATUS word cannot disagree with each other. They used to be computed by
    two functions that both re-derived the answer from the bucket, which is how
    a row ended up marked ? while its status column said "submitted".

    Held is amber rather than red: red is for something that went wrong, and a
    run waiting for approval has not gone wrong -- it is doing exactly what
    this tool exists to make it do. A stopped run and a run that had nothing to
    do are both DIM: terminal, and neither an outcome to celebrate nor one to
    worry about.
    """
    # The bucket's own glyph and colour, from the one table, and the sub-states
    # below override it where the bucket is not the whole answer. Read from
    # _marks() rather than retyped per branch: a second copy of "held is amber"
    # is a second thing to forget when the palette moves.
    glyph, colour = _marks()[bucket]
    uncertain = (_UNKNOWN_MARK, GREY)

    if bucket == HELD_BUCKET:
        return glyph, colour, "waiting for approval"
    if bucket == LAPSED_BUCKET:
        # Worded as the next action rather than as the internal state.
        # "lapsed" is what the registry calls it; what somebody reading a list
        # needs to know is that this row will not approve and what to type
        # instead. The reason it lapsed -- which gate went, and why -- is
        # /check's, and used to be truncated to death here.
        return glyph, colour, "needs rebuilding"
    if bucket == UNAVAILABLE_BUCKET:
        # The scheduler could not be reached, so nothing about this run's jobs
        # is known RIGHT NOW. The last cached verdict is still on the record
        # and is still worth reading -- in /check, dated, and said as the
        # explicitly stale thing it is (_unavailable_line).
        return glyph, colour, "unknown"
    counts = (status.counts if status is not None and status.counts else {})
    if bucket == ACTIVE_BUCKET:
        # Two words, one state. Not a detail tail: a run that has been queued
        # for three days is a starving allocation and a run that is executing
        # is spending one, and the AGE column beside it only means something
        # once you know which of the two you are looking at.
        return glyph, colour, "running" if counts.get("RUNNING") else "queued"
    if bucket == ATTENTION_BUCKET:
        # THE SUBMISSION ITSELF, before there is anything to count. These three
        # statuses reach ATTENTION with no RunStatus at all -- see the note
        # above for why only one of them is a failure.
        trouble = _SUBMISSION_STATE.get(record.get("status"))
        if trouble and status is None:
            word, mark = trouble
            return (glyph, colour, word) if mark == glyph else (*uncertain, word)
        broke = any(s in _BROKE for s in counts)
        doomed = bool(status is not None and status.doomed)
        if not broke and not doomed and status is not None and status.unknown:
            # Jobs in the manifest that the scheduler would not account for --
            # what an accounting database aging ids out looks like. An absence
            # of evidence, and the one ATTENTION row that is not a failure.
            # list_bucket is right to raise it (it wants a person) but the word
            # for it is not "failed".
            return (*uncertain, "unknown")
        return glyph, colour, "failed"
    # FINISHED, in its four flavours.
    if counts.get("CANCELLED"):
        # Nothing broke and it is over, so it is not ATTENTION -- but a green
        # tick on a cancellation would report somebody stopping a run as a
        # success, which is the one thing a status column must never do.
        return _STOPPED_MARK, DIM, "stopped"
    if status is None or not counts:
        if runs.jobs_are_unreachable(record):
            # It ran, it put N jobs on the scheduler, and there is no manifest
            # to ask about them. "submitted · 46 jobs · no job list on disk" was
            # the old row, and the noun in it is a past EVENT rather than a
            # current state -- which is the question this column answers. What
            # is true now is that the live state cannot be established.
            return (*uncertain, "unknown")
        if runs.submitted_nothing(record) or (status is not None
                                              and not status.total):
            # A real, successful, TERMINAL outcome: GenPipes generated no work
            # because every output the run asked for was already on disk. Never
            # "no jobs" and never "nothing to run" -- both read as a failure to
            # do something, and this is the opposite. Reached only on positive
            # evidence (see runs.submitted_nothing); a record where nobody ever
            # counted falls through to unknown below rather than borrowing a
            # success it cannot show.
            #
            # ✓ AND THE SAME GREEN AS `completed`, which reverses an earlier
            # call that gave this row a dim dot on the grounds that no job
            # succeeded. The tick is not "jobs succeeded", it is "this is done
            # and the outcome is good", and by that reading these two belong
            # together: one ran the work, one found the work already done, and
            # both leave the user with the outputs they asked for.
            #
            # It keeps its own WORD, though, and that is the half worth
            # preserving. A run that spent no allocation when you expected it
            # to spend one is a surprise, and a green tick saying "completed"
            # over zero jobs invites the reading "I computed your results" when
            # the truth is "your results were already there". The glyph says
            # the outcome; the word says whether anything was computed.
            #
            # "up to date", not "already up to date". The dropped adverb was
            # reporting on an EVENT that had just happened, and this column
            # describes a state: what is true of this run is that its outputs
            # are current.
            return _DONE_MARK, GREEN, "up to date"
        return (*uncertain, "unknown")
    return glyph, colour, "completed"


def _progress(status):
    """"29/30" -- jobs done out of jobs there are, for the PROGRESS column.

    A fraction rather than the three raw counts it replaced. "29" on its own is
    unreadable: whether it is nearly finished or barely started depends
    entirely on a denominator that was never on screen, and "29 1 0" and
    "1 2 0" hid the difference between a run that died at the finish line and
    one that died on takeoff.

    A middle dot, not a zero and not a fraction, where there is nothing to
    count. A held run has not been submitted, so it has no jobs, and "0/0"
    would state that it has finished all of them.
    """
    if status is None or not status.counts or not status.total:
        return "·"
    done = status.counts.get("COMPLETED", 0)
    if status.unknown and not done:
        # Jobs exist and the scheduler would not account for any of them.
        # "0/15" says fifteen are outstanding, which is not something we found
        # out -- the NEEDS column says they went unaccounted for, and this
        # column has to stop short of contradicting it.
        return "·"
    return f"{done}/{status.total}"


def _age(record, now):
    """How long this run has been in the listing: "13d", "3h", "45m", "".

    Measured from the moment it became something you could be ignoring:
    submitted_at for a launched run, held_at for one still awaiting approval.
    It is deliberately NOT time since a run finished: nothing records
    when a run ended (Job has start and elapsed, no end), and deriving a finish
    time from the last check would report when we happened to look rather than
    when it happened.

    It is the column the old listing had no equivalent of, and on a real screen
    it is the loudest thing on it: ten runs held, the oldest for a fortnight,
    is the actual state of a workspace and used to be invisible.
    """
    # submitted_at first and held_at only as the fallback, rather than a test
    # on the record's status: a run that was held and then approved carries
    # both, and the age that matters from then on is how long it has been
    # running, not how long it once sat waiting. A still-held run has no
    # submitted_at, so it falls through to held_at on its own.
    stamp = record.get("submitted_at") or record.get("held_at")
    if not stamp:
        return ""
    try:
        then = datetime.datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return ""
    seconds = (now - then).total_seconds()
    if seconds < 0:
        return ""
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _what(record):
    """The pipeline and protocol this run is, or "" if it never said.

    Two places know it and neither knows it always: a held run carries the
    slots the gate resolved, and a run adopted by /scan carries what was read
    off its job-list filename. Asking both is what makes the column populated
    for rows that arrived by different routes.
    """
    slots = (record.get("proposal") or {}).get("slots") or {}
    parts = []
    for key in ("pipeline", "protocol"):
        value = slots.get(key) or record.get(key)
        if value:
            parts.append(str(value).strip())
    return " ".join(p for p in parts if p)


# Held first -- it is the one state waiting on a person to make a decision.
# Live and needs attention next, because they describe something actually
# happening right now; completed and status unavailable are, in different
# ways, both "nothing to do here". The rows are one flat list, so this is a
# sort order rather than a set of headings: a run's state is on its own row,
# where the name is, and you never have to look up the screen to find out
# which section you are reading.
#
# The order itself moved to runs.SECTION_ORDER, because /sort has to present
# the same collection the same way and importing the renderer to find out how
# would have been the wrong dependency. This alias is kept so the reasoning
# above stays next to the screen it was written about.
_SECTION_ORDER = list(runs.SECTION_ORDER)


def _finished_line(status):
    """What a terminal run's row says after its tag.

    Never a completion timestamp. sacct is asked for Start, not End, so the
    moment a run actually finished is not a thing this tool knows -- and a
    listing that prints today's date next to a run that ended on Sunday has
    invented the one fact somebody would quote back. The job tally is what we
    genuinely resolved, so the job tally is what is shown.
    """
    if status is None:
        return "no jobs — everything was already up to date"
    if not status.total and not status.counts:
        return "no jobs in the list"
    return list_line(status)


def _unavailable_line(record):
    """The last thing that WAS known about a run we could not resolve, or
    nothing at all.

    The row's tag already says the status is unavailable, so this line owes
    the one thing the tag cannot carry: what the scheduler said the last time
    it answered. See Registry.remember_check -- a failed query never
    overwrites a valid cached verdict, so there is usually something here.
    """
    last = record.get("last_check")
    if not last:
        return "nothing known about it yet"
    at = (last.get("at") or "").replace("T", " ")[5:16]
    return f"last known: {last.get('verdict', '?')} (as of {at})"


# WHAT SOMEBODY DOES NEXT, IN THE ORDER THEY ARE LIKELY TO DO IT. Three
# groups, and the grouping is the argument.
#
# The first group is why this screen was opened. Nobody types /list to launch
# something -- they type it because they want to know what is going on with
# runs that already exist, and the path from "I see a row" to an answer is:
#
#     /check      what is happening with this run
#       -> /diagnose   why did it go wrong
#            -> /jobs       show me the actual scheduler jobs
#
# /jobs IS DELIBERATELY LAST OF THE THREE, and it is the one ordering choice
# here worth defending. Half the reason this tool exists is so nobody has to
# read a job list by hand; putting /jobs before /diagnose advertises the
# manual path first and quietly says the interpretation is the fallback. It is
# the other way round: the agent's reading comes first, and the raw jobs are
# there for when you want to check its work.
#
# The pre-launch verbs come second. They are still reachable from here -- a
# held row is the first thing on the table -- but somebody preparing a run
# usually meets them at the gate, in the moment, rather than by opening a
# listing. /modify leads them because a proposal is much more often adjusted
# than discarded, and /approve is the irreversible one, which is not the
# member of a group that should be nearest the top.
#
# /scan is on its own because it is a different job entirely: not acting on a
# run in this list, but putting one INTO it.
#
# The descriptions say why you would use the command, not what it does
# internally. "refresh a launched run" described the implementation -- a
# scheduler call -- to somebody who wanted to know how their run was doing;
# "adopt runs already on disk" used this project's own word for it. Neither
# reads as an answer to "which of these do I want".
# Descriptions come from _ACTION_TEXT, not from here. This table is the one
# thing that IS local to /list: which commands, in which groups, in which
# order. (No "awaiting approval" note on /approve -- the STATUS column already
# says which rows are, on the rows themselves, a few lines up the same screen.)
_LIST_ACTIONS = (
    (("/check", "<name>"), ("/diagnose", "<name>"), ("/jobs", "<name>")),
    (("/modify", "<name>"), ("/approve", "<name>"), ("/reject", "<name>")),
    (("/scan", "<path>"),),
)


@canonical
def run_list(rows):
    """/list -- every run still worth acting on, one row each, tagged.

    `rows` is runs_store.resolve_all()'s own shape -- [(record, RunStatus or
    None), ...] -- so the caller (agent.submissions()) makes the one batched
    scheduler call and this function only renders what it found.
    list_bucket()/list_tag() are the single source of truth for what a run's
    state is; this function never re-derives that from a record's raw fields.
    In particular the tag is never the registry's own `status`, which says
    "submitted" about a run that failed three days ago.

    An aligned table, sorted by state rather than broken into headed sections,
    one row per run:

         NAME               PIPELINE                 PROGRESS  AGE  STATUS
      \u25c7  rnaseq-0804        rnaseq stringtie                \u00b7  13d  waiting for approval
      \u25b6  rnaseq-0810        rnaseq stringtie            18/44   3h  running
      \u2717  Test_walltimefail  dnaseq somatic_fastpass      1/44   9d  failed
      \u2713  rnaseq-light-0726  rnaseq_light                17/17  16d  completed

    ONE STATE PER ROW AND NOTHING ELSE. Every phrase in the STATUS column
    comes from _row_state and every one of them is printed whole -- there is
    no `\u00b7 <reason>` tail any more, and the width reserved for the column went
    DOWN when the tails went, from 30 to 22. See the long note above
    _row_state for why the reasons left and where they went.

    It replaced three stacked lines per run -- name and tag, then a sentence
    restating the tag, then a bare job-list filename -- which cost forty lines
    for fifteen runs and still could not be compared down a column.

    Three columns before the status, not the four this started with.
    OK/FAIL/RUN looked like three facts and carried about one: RUN was 0 or a
    dash on every row that was not live, OK had no denominator so its number
    meant nothing on its own, and two thirds of the grid was em dashes -- a
    column empty for most rows is a footnote rather than a column.

    What replaced them is one fact each, populated on every row: how far along
    (_progress, a fraction, because "29" without a denominator cannot be read),
    how long it has been sitting there (_age, which the listing had no
    equivalent of at all), and what state it is in (_row_state, which names
    the state and leaves the reason to /check).

    The job-list filename is gone from the listing and lives in /jobs and
    /view, which is what those commands are for; it was the widest thing on
    screen and the least often read.
    """
    # runs.listing_order, not a second sort written here. /sort renders the
    # same collection with checkboxes on it and has to present it in the same
    # order -- see the note on runs.SECTION_ORDER for what disagreeing cost.
    ordered = runs.listing_order(rows)

    # Columns sized from the data, then from the window. A name column wide
    # enough for the longest name is worth having and is not worth wrapping the
    # table for, so the window is allowed to overrule it -- and fit() below is
    # the guarantee, not the intention.
    cols = terminal_cols() if _tty() else 100
    names = [str(r["name"]) for _, r, _ in ordered] or [""]
    whats = [_what(r) for _, r, _ in ordered] or [""]
    # One clock for the whole table, read once. Ages computed per row from a
    # per-row now() are measured from instants a few microseconds apart, which
    # is harmless right up until two rows straddle a minute boundary and the
    # listing gives two runs submitted together different ages.
    now = datetime.datetime.now()
    ages = {id(r): _age(r, now) for _, r, _ in ordered}
    # Everything on a row that is not the name, the pipeline or the needs cell:
    #   "  " + glyph + "  " …name…  "  " …pipeline…  "  " + 8 + "  " + 4 + "  "
    w_prog, w_age = 8, 4
    fixed = 2 + 1 + 2 + 2 + 2 + w_prog + 2 + w_age + 2
    # STATUS is reserved BEFORE the name and pipeline columns get to bid, not
    # left whatever they happen not to use: it carries the one thing no other
    # column can say, and a run name is allowed to be abbreviated long before
    # it is.
    #
    # 20, DOWN FROM 30, and the direction is the point. The column used to
    # carry a reason after the state ("failed · 2× timeout in gatk_sam_to_f…")
    # and 30 was not enough for it either -- the fix for a truncated
    # explanation is not a wider table, it is not putting the explanation
    # there. What is left is the state alone, and the reserve is COMPUTED from
    # _STATE_WORDS rather than typed, so it is exactly the longest phrase
    # _row_state can produce ("waiting for approval") and nothing in this
    # column is ever elided. Ten columns went back to the name and the
    # pipeline, and a phrase can never be added that does not fit.
    w_status = max(len(word) for word, *_ in _STATE_WORDS)
    budget = max(24, cols - 1 - fixed - w_status)
    w_name = min(max(len(n) for n in names), max(12, budget * 3 // 5))
    w_what = min(max(len(w) for w in whats), max(10, budget - w_name))

    print()
    if ordered:
        head = (f"     {'NAME':<{w_name}}  {'PIPELINE':<{w_what}}"
                f"  {'PROGRESS':>{w_prog}}  {'AGE':>{w_age}}  STATUS")
        print(f"  {DIM}{fit(head, cols - 3)}{RESET}")
    for bucket, record, status in ordered:
        name = str(record["name"])
        # Truncated with an ellipsis rather than allowed to push the columns
        # apart. A name too long for its column is a formatting problem; a table
        # whose columns move from row to row is an unreadable one.
        #
        # The state's colour appears exactly twice, at the two ends of the row:
        # on the glyph, and on the whole status phrase including its reason.
        # Everything between them is bold, grey or plain, so the row has one
        # highlight rather than four competing ones.
        glyph, colour, word = _row_state(bucket, record, status)
        line = (f"  {colour}{glyph}{RESET}  "
                f"{BOLD}{pad(name, w_name)}{RESET}"
                f"  {GREY}{pad(_what(record), w_what)}{RESET}"
                f"  {_progress(status):>{w_prog}}"
                f"  {ages[id(record)]:>{w_age}}"
                f"  {colour}{word}{RESET}")
        print(fit(line, cols - 1))

    # Provenance for the whole listing rather than a timestamp per row: every
    # row above was resolved by the same batched scheduler call, a moment ago.
    at = next((s.at for _, s in rows if s is not None and s.at), None)
    if at:
        # A plain footer rather than the old \u258c gutter marker: the gutter used to
        # align with the \u258c that started every row, and rows start with a state
        # glyph now, so it aligned with nothing.
        print()
        print(f"     {DIM}states read from the scheduler at {at}{RESET}")

    print()
    actions(_LIST_ACTIONS)
    print()


# What a registry status means as a HISTORICAL record, which is not what it
# means as a current state.
#
# `submitted` used to render as "live", and that was the reported defect: a run
# submitted three weeks ago whose jobs finished, failed or were cleaned up long
# since was described as running right now, on a screen that asks the scheduler
# nothing. The status field records that an approval was spent. It cannot know
# what happened next, and the words here are chosen so they cannot be read as
# though it did.
#
# THREE THINGS, KEPT APART, because collapsing them is what produced the lie:
#
#   registry lifecycle       this table. What the record says about itself.
#   live scheduler evidence  NOT ON THIS SCREEN. /list and /check ask Slurm;
#                            /history deliberately does not, so it stays fast
#                            and stays honest about being an archive.
#   last known outcome       _last_seen() below, printed as a separate, dated,
#                            explicitly stale clause.
def _archive_tag():
    return {
    "held": ("awaiting approval", AMBER),
    "lapsed": ("proposal expired", GREY),
    "submitted": ("submitted", ""),
    "gone": ("artifacts gone", GREY),
    "abandoned": ("abandoned", GREY),
    }

# How a run got into the registry, said as a verb rather than as a noun.
# "agent" and "manual" were the stored values and neither reads as anything on
# its own -- a column of the word "agent" tells you nothing about what agent
# did. These say what happened to produce the record.
_ORIGIN = {
    "agent": "built here",
    "manual": "tracked",
    "scan": "found on disk",
}


def _last_seen(record):
    """The last scheduler outcome recorded for this run, or "".

    Explicitly dated and explicitly past tense. This is a cached snapshot from
    whenever somebody last ran /check or /list -- it is the strongest thing
    /history can say about what became of a run, and it is still not current
    truth, so it never appears without the day it was taken.
    """
    check = record.get("last_check") or {}
    verdict = str(check.get("verdict") or "").strip()
    if not verdict:
        return ""
    at = str(check.get("at") or "")[:10]
    return f"{verdict} when last checked{f' {at}' if at else ''}"


@canonical
def history(records, limit=None):
    """/history -- the archive of run records, newest first.

    WHAT THIS SCREEN IS FOR, because it was answering a different question than
    the one it was being asked. /history is an ARCHIVE: it exists so a run can
    still be found months later, after its job_list has been cleaned off the
    cluster and after everyone has forgotten what it was called. It is not a
    dashboard and it is not a diagnosis report.

    So three things left, and each was doing real harm rather than merely
    taking up room:

      "live"        a registry status rendered as a claim about right now. See
                    _ARCHIVE_TAG. A run that failed on the 5th of August was
                    described as live for a fortnight.
      the notes     two lines of /diagnose prose under every entry, usually
                    "The log does not name a cause on its own." Sixty runs of
                    that is a screen nobody reads, and the notes are still on
                    the record and still shown by /diagnose, which is the
                    command that is about them.
      the gutter    a \u258c down the left of entries of wildly differing height,
                    which is what a gutter is worst at.

    What replaced them is one row per run, aligned, so the archive can be read
    down a column -- which is the only way anybody has ever used it.
    """
    rows = list(records)
    shown = rows[:limit] if limit else rows
    cols = terminal_cols() if _tty() else 100

    names = [str(r.get("name") or "?") for r in shown] or [""]
    whats = [_what(r) for r in shown] or [""]
    w_when = 10
    w_origin = max(len(v) for v in _ORIGIN.values())
    fixed = 2 + w_when + 2 + 2 + 2 + w_origin + 2
    # 18 reserves the longest lifecycle word ("awaiting approval") and nothing
    # for the "when last checked" clause after it. That clause is the least
    # important thing on the row and the only one that may be cut: the name is
    # what this screen exists to help somebody find again.
    budget = max(24, cols - 1 - fixed - 18)
    w_name = min(max(len(n) for n in names), max(12, budget * 2 // 3))
    w_what = min(max(len(w) for w in whats), max(10, budget - w_name))

    print()
    head = (f"{'RECORDED':<{w_when}}  {'NAME':<{w_name}}  {'PIPELINE':<{w_what}}"
            f"  {'ORIGIN':<{w_origin}}  RECORD")
    print(f"  {DIM}{fit(head, cols - 3)}{RESET}")
    for r in shown:
        # The day, not the second. A timestamp to the second implies this
        # screen is about sequencing events; it is about finding a run again,
        # and the day is how anybody remembers which one they mean.
        when = str(r.get("submitted_at") or r.get("held_at") or "")[:10]
        word, colour = _archive_tag().get(r.get("status"), ("?", GREY))
        origin = _ORIGIN.get(r.get("source"), str(r.get("source") or "?"))
        tail = _last_seen(r)
        line = (f"  {DIM}{when:<{w_when}}{RESET}"
                f"  {BOLD}{pad(str(r.get('name') or '?'), w_name)}{RESET}"
                f"  {GREY}{pad(_what(r), w_what)}{RESET}"
                f"  {DIM}{origin:<{w_origin}}{RESET}"
                f"  {colour}{word}{RESET}"
                + (f"{DIM}  \u00b7  {tail}{RESET}" if tail else ""))
        print(fit(line, cols - 1))

    print()
    if limit and len(rows) > limit:
        print(f"  {DIM}{len(rows) - limit} older record(s) not shown{RESET}")
    # Where the detail went, named on the screen it was taken off. /view leads
    # because /history is an archive: what somebody comes here for is a run
    # they have forgotten, and the first question about a forgotten run is what
    # it was. The scheduler-facing pair follow in their usual order.
    print()
    actions([("/view", "<name>"), ("/diagnose", "<name>"), ("/jobs", "<name>")])
    print()


# Why a run was derived from another, said in English. The stored value is a
# constant (see runs.Registry.derive) so that this table is the only place the
# wording lives and an old record cannot carry a phrase that has since changed.
_DERIVED = {
    "relaunch_after_diagnosis": "a retry prepared from its diagnosis",
    "fork": "a copy made with changes",
}


@canonical
def history_detail(record):
    """One archived run, in full: what it was, and what was found out about it.

    THIS IS WHERE THE NOTES WENT. /history used to print every run's last two
    /diagnose findings underneath it, which on a real registry is sixty lines
    of "The log does not name a cause on its own." for the two that say
    something. Deleting them outright would have been the wrong trade -- months
    later, "OOM in picard_mark_duplicates" really is the only part of a record
    anybody still wants, and /diagnose cannot recover it once the logs are off
    the cluster.

    So the finding is archived rather than broadcast: it stays on the record,
    and this is the screen that shows it, reached by naming the run.
    """
    name = str(record.get("name") or "?")
    word, colour = _archive_tag().get(record.get("status"), ("?", GREY))
    origin = _ORIGIN.get(record.get("source"), str(record.get("source") or "?"))
    when = str(record.get("submitted_at") or record.get("held_at") or "").replace("T", " ")

    print()
    print(f"  {DIM}\u258c{RESET} {BOLD}{name}{RESET}  {DIM}\u00b7{RESET}  {colour}{word}{RESET}")
    print(f"  {DIM}\u258c{RESET}")
    # WHERE IT CAME FROM, when it came from somewhere. A revision is a run in
    # its own right -- own name, own thread, own gate -- and the cost of that
    # separation is that nothing on its record says what it is a revision OF.
    # /history is the screen that question is asked on, months later, so the
    # link is printed here rather than left to be reconstructed from two
    # timestamps and a similar command. `derived_reason` is a stored constant
    # (relaunch.REASON, or "fork"), never prose, so it reads the same every
    # time.
    derived = str(record.get("derived_from") or "")
    lineage = (f"{derived}  {DIM}·  {_DERIVED.get(record.get('derived_reason'), '')}"
               if derived else "")
    for label, value in (("pipeline", _what(record)),
                         ("recorded", when),
                         ("origin", origin),
                         ("derived from", lineage),
                         ("last seen", _last_seen(record)),
                         ("workdir", _tilde(record.get("workdir") or "")),
                         ("job list", os.path.basename(record.get("job_list") or ""))):
        if value:
            print(f"  {DIM}\u258c{RESET}   {GREY}{label:<14}{RESET}{value}{RESET}")

    notes = record.get("notes") or []
    if notes:
        print(f"  {DIM}\u258c{RESET}")
        print(f"  {DIM}\u258c{RESET}   {GREY}what was found{RESET}")
        for note in notes:
            at = str(note.get("at") or "")[:10]
            for i, line in enumerate(textwrap.wrap(str(note.get("text") or ""), 66)):
                lead = f"{at:<10}" if i == 0 else " " * 10
                print(f"  {DIM}\u258c{RESET}     {DIM}{lead}{RESET}{line}")
    print(f"  {DIM}\u258c{RESET}")
    print()


JOB_NAME_W = 38


@canonical
def jobs(name, job_list, only_failed=False):
    """Every job in a run, as a table grouped by step.

    Grouped because a GenPipes failure is almost never one unlucky job -- it is
    one step failing across every sample -- and a flat list of two hundred rows
    hides exactly that shape. The step is printed once and its jobs indented
    under it, so "trimmomatic is fine, mark_duplicates is not" is visible without
    reading a single job name.
    """
    shown = [j for j in job_list if j.failed] if only_failed else list(job_list)
    if not shown:
        nothing(f"No failed jobs in '{name}'.", f"/jobs {name} shows all of them.")
        return

    tally = {}
    for j in job_list:
        key = j.state or "UNKNOWN"
        tally[key] = tally.get(key, 0) + 1
    broke = sum(n for s, n in tally.items() if s in _BROKE)
    cancelled = tally.get("CANCELLED", 0)

    # Broken and cancelled are counted separately, because one failing step
    # cancels everything downstream of it. Rolling them together reports "9
    # failed" for a run where three things went wrong and six never started --
    # which sends you looking for six problems that do not exist.
    if broke and cancelled:
        head = (f"{RED}{broke} failed{RESET}{DIM} \u00b7 {cancelled} cancelled "
                f"downstream{RESET}")
    elif broke:
        head = f"{RED}{broke} failed{RESET}"
    elif cancelled:
        head = f"{DIM}{cancelled} cancelled{RESET}"
    else:
        head = f"{DIM}{len(job_list)} jobs{RESET}"

    print()
    print(f"  {DIM}\u258c {BOLD}{name}{RESET}  {DIM}\u00b7{RESET}  {head}")
    print(f"  {DIM}\u258c{RESET}")

    step = None
    for j in shown:
        if j.step != step:
            step = j.step
            print(f"  {DIM}\u258c{RESET} {WHITE}{step}{RESET}")
        state = j.state or "UNKNOWN"
        colour = RED if state in _BAD else DIM
        emphasis = BOLD if state in _BAD else ""
        detail = j.elapsed or ""
        if j.maxrss and state in _BAD:
            detail = f"{detail}  {j.maxrss}".strip()
        print(f"  {DIM}\u258c{RESET}   {DIM}{(j.name or '?')[:JOB_NAME_W]:<{JOB_NAME_W}}{RESET}"
              f"{colour}{emphasis}{state.lower():<14}{RESET}"
              f"{DIM}{detail}{RESET}")
    print(f"  {DIM}\u258c{RESET}")
    if broke:
        # Offered only when something actually broke: /diagnose on a run whose jobs
        # were merely cancelled downstream has nothing to diagnose.
        print(f"  {DIM}\u258c   /diagnose {WHITE}{name}{RESET}{DIM} to diagnose{RESET}")
    print()


def triage(name, report):
    """What broke, established from the scheduler before any model is asked.

    Printed on its own, ahead of the model's answer, so the evidence and the
    interpretation are visibly separate things. If the explanation that follows
    disagrees with this block, the block is the one to trust -- it came from
    sacct and from the log files, not from a model.
    """
    broke = report.get("broke_total", report["failed_total"])
    cancelled = report.get("cancelled_total", 0)
    tail = f", {cancelled} cancelled downstream" if cancelled else ""
    print()
    print(f"  {RED}\u258c {BOLD}{broke} failed{RESET}"
          f"  {DIM}\u00b7{RESET}  {DIM}{report['steps_affected']} step(s) affected"
          f"{tail} in {name}{RESET}")
    print(f"  {RED}\u258c{RESET}")
    for f in report["findings"]:
        print(f"  {RED}\u258c{RESET} {WHITE}{f['step']}{RESET}"
              f"  {DIM}\u00d7{f['count']}{RESET}  {RED}{(f['state'] or '?').lower()}{RESET}")
        # THE JOB, NOT JUST THE STEP. A paired tumour/normal run has two jobs
        # per step whose names differ by one character, and naming only the
        # step leaves the reader -- and anything reading this screen -- to
        # guess which one. resolve() has always known; this panel did not say.
        if f.get("job"):
            print(f"  {RED}\u258c{RESET}   {DIM}{'job':<13}{RESET}{WHITE}{f['job']}{RESET}"
                  + (f"{DIM}  \u00b7  {f['job_id']}{RESET}" if f.get("job_id") else ""))
        if f.get("maxrss"):
            print(f"  {RED}\u258c{RESET}   {DIM}{'peak memory':<13}{f['maxrss']}{RESET}")
        # ONLY FOR A JOB THAT ACTUALLY RAN. A CANCELLED job never started, so
        # its recorded 0:0 is not its exit status -- it is the absence of one,
        # printed identically on every cancelled row, which is thirty-two
        # copies of a number that means nothing about any of them.
        #
        # EXIT CODE 0:0 BESIDE `timeout` READS AS A CONTRADICTION, and it is
        # not one. Slurm records the step's own exit status; a job killed by
        # the walltime enforcer never returned a failing code of its own, so
        # TIMEOUT with ExitCode 0:0 is exactly what sacct reports and is not
        # evidence that nothing went wrong. The STATE is the authoritative
        # signal here -- runs.BROKE_STATES is keyed on it and nothing in this
        # application infers failure from an exit code.
        #
        # So a 0:0 is annotated rather than printed bare next to a state that
        # says the job died. A non-zero code is a real second fact and is
        # printed plainly.
        # `is not False`, matching the log branch below: a caller that does
        # not say whether the job ran has not said it DIDN'T, and dropping a
        # real exit code on that silence would lose evidence.
        code = f.get("exit_code")
        ran = f.get("ran") is not False
        if code and ran and str(code).strip() not in ("0:0", "0"):
            print(f"  {RED}\u258c{RESET}   {DIM}{'exit code':<13}{code}{RESET}")
        elif code and ran:
            print(f"  {RED}\u258c{RESET}   {DIM}{'exit code':<13}{code}"
                  f"  \u2014 nothing the job returned; the state above is "
                  f"what stopped it{RESET}")
        # WHY THERE IS NO LOG, WHEN THERE IS NO LOG. A cancelled job never
        # started and so never wrote one; that is not the same as a file that
        # should be there and is missing, and printing "not found" for both sent
        # people hunting for the first. See runs.triage.
        if f.get("log"):
            note = os.path.basename(f["log"])
        elif f.get("ran") is False:
            note = "never ran, no log"
        else:
            note = "not found for this run"
        print(f"  {RED}\u258c{RESET}   {DIM}{'log':<13}{note}{RESET}")
    if report.get("truncated"):
        print(f"  {RED}\u258c{RESET}   {GREY}+{report['truncated']} more step(s){RESET}")
    print(f"  {RED}\u258c{RESET}")
    print(f"  {DIM}  reading the logs, then explaining{RESET}")
    print()


@canonical
def diagnosis(name, parsed, logs=(), applicable=False):
    """What /diagnose concluded, drawn rather than dumped.

    The old ending printed the model's markdown straight to the terminal:
    asterisks, backticks, fenced ini blocks and all. It read as a chat window
    that had escaped into a tool, and it buried the two things somebody actually
    needs -- what to change, and what to type next -- in the middle of a wall of
    justified prose.

    So it is drawn in the same house style as the gate: manner and cause as
    separate claims, because they are separate claims; the evidence indented
    under them; and the fix at the bottom next to the command that applies it.
    An answer the model did not shape falls back to its own prose, printed
    plainly. See diagnosis.parse -- degrading to the old behaviour is a
    requirement, not an accident.

    `logs` are the log files the evidence came from, printed in full. The point
    is not that anybody reads them here; it is that "go and look yourself" stops
    being an invitation without an address.

    `applicable` says whether the OVERRIDE this answer proposes is one the
    program can apply on its own -- override.applicable()'s verdict, decided by
    the caller and never re-derived here. It selects the verbs at the bottom.
    Passed in rather than computed from `parsed` because "there is an override
    section" and "there is a change this can make" are different claims, and
    the screen must offer the command only for the second.
    """
    if not parsed.get("shaped"):
        print()
        for line in (parsed.get("prose") or "").splitlines():
            print(f"  {line}" if line.strip() else "")
        print()
        return

    # NO GLOBAL CONFIDENCE BADGE, and its removal is the point rather than a
    # simplification.
    #
    # It printed ONE word over the whole answer -- "likely" -- above a screen
    # whose first two rows are sacct facts. On 2026-08-05 that read as
    # "probably gatk_sam_to_fastq.tumorPair_COLO829T probably timed out",
    # when the job, the id, the state, the limit and the overrun are all
    # certain and only the remedy is not. A label that spans claims of
    # different standing takes its value from the weakest one and defames the
    # rest; there is no single number that is true of a screen carrying both
    # a scheduler record and a suggested walltime.
    #
    # CONFIDENCE BELONGS TO CLAIMS. So it is expressed where the doubt
    # actually is -- the `uncertain` rows beneath the fix, one line per thing
    # this run does not establish, in plain language. Three named unknowns
    # read as three named unknowns; one adjective over everything reads as
    # hedging, and teaches a reader to discount the facts too.
    #
    # `confidence` is still parsed (see diagnosis._HEADINGS) so an older
    # stored note, or a model that emits the retired heading out of habit,
    # lands somewhere harmless instead of spilling into another section.
    print()
    print(f"  {RED}▌{RESET} {BOLD}{name}{RESET}  {DIM}·{RESET}  "
          f"{DIM}diagnosis{RESET}")
    print(f"  {RED}▌{RESET}")
    for label, key in (("died", "manner"), ("because", "cause")):
        if parsed.get(key):
            _labelled(f"  {RED}▌{RESET}", label, parsed[key])
    if parsed.get("evidence"):
        print(f"  {RED}▌{RESET}")
        for i, item in enumerate(parsed["evidence"]):
            _labelled(f"  {RED}▌{RESET}", "evidence" if i == 0 else "", item,
                      style=DIM)
    if logs:
        print(f"  {RED}▌{RESET}")
        for i, path in enumerate(logs):
            _labelled(f"  {RED}▌{RESET}", "read it yourself" if i == 0 else "",
                      _tilde(str(path)), style=DIM, wrap="path")
    if parsed.get("fix"):
        print(f"  {RED}▌{RESET}")
        _labelled(f"  {RED}▌{RESET}", "fix", parsed["fix"], style=WHITE)
    for section, keys in (parsed.get("override") or {}).items():
        print(f"  {RED}▌{RESET}")
        print(f"  {RED}▌{RESET}   {'':<{LABEL_W}}{WHITE}[{section}]{RESET}")
        for key, value in keys.items():
            print(f"  {RED}▌{RESET}   {'':<{LABEL_W}}{DIM}{key} = {RESET}{value}")
    if parsed.get("uncertain"):
        print(f"  {RED}▌{RESET}")
        for i, item in enumerate(parsed["uncertain"]):
            _labelled(f"  {RED}▌{RESET}", "not established" if i == 0 else "",
                      item, style=AMBER)
    # NO `resubmit` ROW. It printed a raw `-s 1-23` on the screen whose job is
    # to explain a failure, which put GenPipes command syntax in front of
    # somebody two rows below a sentence about a walltime -- and then left them
    # to assemble the command around it. The range has not gone anywhere: it is
    # established deterministically from the generated step lists (see
    # relaunch.scope, and slots.step_range under it), carried as internal state,
    # and rendered where command syntax belongs -- in the `-s` row of the mirror
    # /view and the gate draw for the revision /relaunch prepares.
    #
    # `relaunch` is still PARSED, and still what the model is asked for. It is
    # a cross-check on the range this computes rather than the source of it,
    # and a source that can be wrong has no business being the one on screen.
    gut = f"  {RED}▌{RESET}"
    print(gut)
    # WHICH VERBS, AND WHY THE FIRST ONE IS CONDITIONAL.
    #
    # /relaunch appears only where the diagnosis produced a fix this program
    # can actually apply -- `applicable` is the caller's answer from
    # override.applicable(), never merely "the run failed". Offering it on a
    # failure whose fix is "the readset points at a file that is not there"
    # would propose an operation that has nothing to perform.
    #
    # /modify is always here, and reads differently depending on whether it has
    # company. Beside /relaunch it is the manual alternative -- the same fork,
    # with changes somebody chooses -- and says so. Alone, it is the only next
    # step on the screen, and then it has to carry the fact that a launched run
    # is copied rather than edited. See _ACTION_TEXT.
    #
    # /jobs stays last either way: it is evidence, not a remedy.
    rows = ([("/relaunch", name)] if applicable else []) + \
           [("/modify@else" if applicable else "/modify@launched", name),
            ("/jobs", name)]
    actions(rows, gutter=gut)
    print()


# The label column in a /diagnose row, and the gap between the gutter and it.
# Named because the wrap budget is computed from them: a line whose body is
# measured against a different prefix than the one it is printed with is
# exactly the bug this replaced.
LABEL_W = 18
LABEL_GAP = 3


def _body_width(gutter, cols=None):
    """How many columns a labelled row's body may occupy.

    THE BUG THIS FIXES. The budget used to be `WIDTH - 26`, where WIDTH was the
    constant 74 -- the only reference to it in this module, while every other
    block that has to fit the window (the listing, the history table, the plan
    checklist) asks terminal_cols(). On a window narrower than 74 the body
    overflowed; the terminal then soft-wrapped the overflow to COLUMN ZERO,
    which is left of and underneath the gutter, so a long line ran under the
    very rule it was supposed to sit beside. On a window wider than 74 it threw
    away the extra space.

    The prefix is measured with cells(), not len(), because the gutter carries
    colour codes that occupy no columns and a block-drawing glyph that does.
    """
    if cols is None:
        # A real window when there is one. Off-screen -- redirected to a file,
        # captured in a test -- there is nothing to overflow, so the old fixed
        # width stands and output stays stable.
        cols = terminal_cols() if _tty() else WIDTH
    return max(24, cols - cells(gutter) - LABEL_GAP - LABEL_W - 1)


def _wrap_path(text, width):
    """A path broken onto several lines at its separators.

    Paths used to be printed unwrapped, on the reasoning that breaking one costs
    the only thing a printed path is for -- being copied -- and that letting the
    terminal overflow was merely ugly. It is not merely ugly: the terminal
    restarts the overflow at column zero, so a 138-column log path prints its
    tail underneath the gutter.

    Breaking at "/" keeps every line copyable as a piece and keeps the whole
    thing readable as a path, which mid-token wrapping does neither of. A single
    segment longer than the budget is cut, because a name that cannot fit has
    to go somewhere and off the edge of the screen is the one place it must
    not.
    """
    text = str(text)
    if len(text) <= width:
        return [text]
    # Each separator stays with the segment it follows, so a line ends on "/"
    # and the eye can see the path continues.
    parts = text.split("/")
    pieces = [c + ("/" if i < len(parts) - 1 else "")
              for i, c in enumerate(parts)]
    out, line = [], ""
    for piece in pieces:
        if len(line) + len(piece) <= width:
            line += piece
            continue
        if len(piece) <= width:
            if line:
                out.append(line)
            line = piece
            continue
        # A single segment too long for any line -- GenPipes job names run to
        # sixty characters. Fill the room left on this line before cutting, so
        # the break is at the edge of the screen rather than wherever the
        # segment happened to start.
        room = width - len(line)
        if room > 0:
            line += piece[:room]
            piece = piece[room:]
        out.append(line)
        while len(piece) > width:
            out.append(piece[:width])
            piece = piece[width:]
        line = piece
    if line:
        out.append(line)
    return out or [text]


def _labelled(gutter, label, text, style="", wrap=True, cols=None):
    """A label column and a wrapped body. The label is printed once and the
    continuation lines align under the body, so a long sentence stays one
    visual block instead of becoming several rows.

    `wrap="path"` breaks on separators instead of on spaces -- see _wrap_path.
    """
    width = _body_width(gutter, cols)
    text = str(text)
    if wrap == "path":
        body = _wrap_path(text, width)
    elif wrap:
        body = textwrap.wrap(text, width) or [""]
    else:
        body = [text]
    for i, line in enumerate(body):
        shown = label if i == 0 else ""
        print(f"{gutter}   {DIM}{shown:<{LABEL_W}}{RESET}{style}{line}{RESET}")


def _names(records, limit=3):
    """First few names, then a count. The rest is what /list is for."""
    shown = ", ".join(r["name"] for r in records[:limit])
    if len(records) > limit:
        shown += f", +{len(records) - limit} more"
    return shown


def _ago(stamp):
    """'3 days ago' from a stored timestamp, or '' if it cannot be read."""
    try:
        then = datetime.datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return ""
    seconds = (datetime.datetime.now() - then).total_seconds()
    if seconds < 0:
        return ""
    if seconds < 3600:
        return "earlier"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours} hour{'s' if hours > 1 else ''} ago"
    days = int(seconds // 86400)
    return f"{days} day{'s' if days > 1 else ''} ago"


def pending(records, since=None, seen=""):
    """What is waiting on you, surfaced at startup.

    This exists because of the tool's worst failure mode: the gate pauses a run,
    the terminal closes, and the only record of a decision you still owe was the
    name in your head. Everything else in the interface can wait until asked.
    This cannot.

    But it is a reminder, not a report. Printing every held command turned a
    fortnight of experiments into the largest thing on a fresh screen, and pushed
    the one line that matters -- the prompt -- to the bottom of the scrollback.
    Names first three, count for the rest, /list for the commands.

    `since` is Registry.unseen() -- runs whose OUTCOME nobody has looked at.
    Not a diff against the last launch: everything the registry knows offline is
    something the person did themselves, so "2 submitted since you were last
    here" is a list of things they already watched happen. What is actually
    unseen is how those runs turned out.

    OFFLINE, ON PURPOSE. Finding out that a run finished overnight costs a
    `module load` and an sacct per run, which is seconds of dead time before the
    first prompt. So the live answer is one keystroke away rather than in the
    startup path, and what is printed here is careful never to claim otherwise:
    a cached failure says "when last checked", and a run still going says only
    that nobody has asked.
    """
    since = since or {}
    failed = list(since.get("failed", ()))
    waiting = list(since.get("unfinished", ()))
    if not records and not failed and not waiting:
        return

    print()
    if records:
        # A count, not a roll-call. Nine held runs from a fortnight of testing
        # listed three stale names and a "+6 more", which reads as debris rather
        # than as news -- none of those names is something you did not already
        # know, and /list is one keystroke away for anyone who wants them. What
        # DID change on its own gets named below, because that is the half of
        # this notice worth printing.
        n = len(records)
        print(f"  {AMBER}▌{RESET} {AMBER}{n} run{'s' if n > 1 else ''} held{RESET}"
              f"{DIM}, waiting on you{RESET}   {GREEN}/list{RESET}")
    if failed:
        # Never presented as live truth -- it is what a check saw, and the run
        # may well have been fixed or cancelled since.
        n = len(failed)
        print(f"  {RED}▌{RESET} {RED}{n} had failing jobs{RESET}"
              f"{DIM} when last checked:{RESET} "
              f"{WHITE}{_names(failed)}{RESET}   {DIM}/check{RESET}")
    if waiting:
        when = _ago(seen)
        n = len(waiting)
        tail = f"  {DIM}·{RESET}  {DIM}last here {when}{RESET}" if when else ""
        print(f"  {DIM}▌{RESET} {DIM}{n} run{'s' if n > 1 else ''} nobody has "
              f"checked:{RESET} {GREY}{_names(waiting)}{RESET}   "
              f"{DIM}/check all{RESET}{tail}")
    print()


@canonical
def where(paths):
    """/where -- the directories that decide where everything lands.

    Worth a command of its own because one of these silently determines whether
    a submission gets registered at all: the job list is looked for under the
    directory the app was launched from, and nothing else in the interface shows
    you what that is.

    WHAT THIS SCREEN IS FOR was the thing it never said. It printed six paths
    and left the reader to work out which of them mattered and why -- so the
    one row with teeth read exactly like the five that are merely informative.
    A heading now says what the list is, and a row may carry its own note.

    `paths` rows are (label, value) or (label, value, note).
    """
    rows = [(r[0], r[1], r[2] if len(r) > 2 else "") for r in paths]
    print()
    print(f"  {DIM}\u258c{RESET} {BOLD}Where this session reads and writes{RESET}")
    print(f"  {DIM}\u258c{RESET}")
    width = max(len(k) for k, _, _ in rows) + 2
    for label, value, note in rows:
        print(f"  {DIM}\u258c{RESET} {DIM}{label:<{width}}{RESET}{_tilde(str(value))}")
        if note:
            print(f"  {DIM}\u258c{RESET} {' ' * width}{GREY}{note}{RESET}")
    print(f"  {DIM}\u258c{RESET}")
    print()


# ---------------------------------------------------------------------------
# The multi-run view. /check all.
#
# There were briefly two of these -- a flat table under /check all and a grouped
# one under /status all -- rendering the same query in two layouts, with
# /status <name> an exact alias for /check <name>. Two layouts of one query is
# one layout too many. The grouped one won, because the question a listing
# answers is "what should I be doing" and the answer to that is never
# chronological; the flat one's progress figure moved into these rows.
# ---------------------------------------------------------------------------

# What /check all sorts runs into. Three categories, because there are three
# things a person does with a listing: leave it alone, act on it, or forget it.
ACTIVE = "ACTIVE"
ATTENTION = "NEEDS ATTENTION"
FINISHED = "FINISHED"


@canonical
def status_overview(groups):
    """/check all -- every registered run, grouped by what it needs from you.

    NEEDS ATTENTION first and always, whatever it contains, because a listing
    whose most urgent group is below the fold is a listing that trained you to
    scroll. Blank lines between the title and its first row, and more between
    groups, so the groups stay separable at a glance rather than reading as one
    long table with headings in it.

    No paths, no job-list filenames, no log excerpts. This is an overview; the
    evidence lives behind /check <name> and /diagnose.
    """
    order = [ATTENTION, ACTIVE, FINISHED]
    if not any(groups.get(k) for k in order):
        nothing("No runs registered yet.",
                "/scan <path> adopts runs that already exist on disk.")
        return

    for title in order:
        rows = groups.get(title) or []
        if not rows:
            continue
        colour = RED if title == ATTENTION else (WHITE if title == ACTIVE else DIM)
        print()
        print(f"  {colour}{BOLD}{title}{RESET}  {DIM}({len(rows)}){RESET}")
        print()
        for row in rows:
            what = "  ·  ".join(x for x in (row.get("what"), row.get("when")) if x)
            print(f"    {BOLD}{row['name']}{RESET}"
                  + (f"   {DIM}{what}{RESET}" if what else ""))
            print(f"      {DIM}{row.get('line', '')}{RESET}")
            if row.get("suggest"):
                print(f"      {DIM}{row['suggest']}{RESET}")
        print()

    # /check <name> IS offered here, unlike inside /check <name> itself: this
    # is the all-runs view, and narrowing to one run is the natural next step
    # rather than a re-run of what the reader just typed.
    actions([[("/check", "<name>"), ("/diagnose", "<name>"), ("/jobs", "<name>")],
             [("/scan", "<path>")]])
    print()


# ---------------------------------------------------------------------------
# Confirmations for the new gate verbs and for /scan. All three mirror
# post_approve()'s shape so the outcomes of a decision read as one family.
# ---------------------------------------------------------------------------

def abandoned(name, reason=None):
    """/reject, which is now terminal. Says plainly that nothing was submitted,
    because "rejected" used to mean "sent back for rework" and somebody who
    learned it that way has to be told it no longer does."""
    print()
    print(f"  {DIM}▌ {BOLD}{name}{RESET}  {DIM}·{RESET}  {DIM}abandoned{RESET}")
    if reason:
        print(f"  {DIM}▌{RESET}   {GREY}{reason}{RESET}")
    print(f"  {DIM}▌{RESET}")
    print(f"  {DIM}▌{RESET}   {DIM}nothing was submitted  ·  /history to see it{RESET}")
    print()


def renamed(old, new):
    """A rename is a registry write and nothing else -- no model call, no
    regeneration, no new command. Saying so is the point of the second line:
    every other row at the gate changes what would run."""
    print()
    print(f"  {DIM}▌{RESET} {DIM}{old}{RESET}  {DIM}→{RESET}  {BOLD}{new}{RESET}")
    print(f"  {DIM}▌{RESET} {DIM}still held  ·  nothing regenerated{RESET}")
    print()


def now_required(rows):
    """Rows a change just made mandatory, announced before they are asked.

    Announced as a set, ahead of the prompts, rather than arriving one at a
    time. Being asked three unexpected questions in a row feels like a form that
    will not end; being told "changing the pipeline means three things have to
    move, here they are" is the same three questions with a reason attached, and
    the reason is what makes them answerable.
    """
    if not rows:
        return
    print()
    n = len(rows)
    print(f"  {RED}▌{RESET} {BOLD}{n} more {'answer' if n == 1 else 'answers'} "
          f"needed{RESET}  {DIM}·{RESET}  {DIM}that change invalidated "
          f"{'it' if n == 1 else 'them'}{RESET}")
    print(f"  {RED}▌{RESET}")
    for row, why in rows.items():
        print(f"  {RED}▌{RESET}   {RED}{row:<12}{RESET}{GREY}{why}{RESET}")
    print()


def still_required(rows):
    """Rows that were asked, left unanswered, and still matter.

    Not a refusal. The change goes through, because a flow that will not let you
    out is worse than a run you were warned about -- and GenPipes' own
    generation is the authoritative check, which will name exactly what it
    rejected. This is the warning that makes that rejection legible when it
    comes.
    """
    if not rows:
        return
    print()
    print(f"  {AMBER}▌{RESET} {BOLD}left unanswered{RESET}  {DIM}·{RESET}  "
          f"{DIM}generation may refuse this{RESET}")
    for row, why in rows.items():
        print(f"  {AMBER}▌{RESET}   {AMBER}{row:<12}{RESET}{GREY}{why}{RESET}")
    print()


def overrides(name, rows, path):
    """What the private override ini is about to say, before it is written.

    Shown as the file's own contents rather than as "3 settings changed",
    because this is the one artifact of a /modify that outlives the session:
    it sits on the `-c` line, it wins over every GenPipes ini, and somebody will
    open it in six weeks. Seeing it in the shape it will have on disk is what
    makes it recognisable then.

    The path is printed in full for the same reason, and the last line says
    where it lands in the stack -- an override ini that is not last is silently
    overruled, which looks exactly like a change that did not take.
    """
    print()
    if not rows:
        print(f"  {DIM}▌ {BOLD}{name}{RESET}  {DIM}·{RESET}  "
              f"{DIM}no overrides{RESET}")
        print()
        return
    print(f"  {DIM}▌ {BOLD}{name}{RESET}  {DIM}·{RESET}  "
          f"{DIM}private overrides{RESET}")
    print(f"  {DIM}▌{RESET}")
    step = None
    for this, label, value in rows:
        if this != step:
            step = this
            print(f"  {DIM}▌{RESET}   {WHITE}[{step}]{RESET}")
        print(f"  {DIM}▌{RESET}     {DIM}{label:<14}{RESET}{value}")
    print(f"  {DIM}▌{RESET}")
    print(f"  {DIM}▌{RESET}   {DIM}{_tilde(path)}{RESET}")
    print(f"  {DIM}▌{RESET}   {DIM}goes last on -c, so it wins over every "
          f"GenPipes ini{RESET}")
    print()


def no_step_list(pipeline, protocol, why=None):
    """Why the step panel is offering nothing, and how to get the list.

    There is no step table in this repo and there must never be one --
    genpipes.md says so outright, because the numbered list for every protocol
    is version-exact and a copy here would be wrong on the next release. So
    when the list cannot be established, the honest thing is an empty panel
    plus this, rather than a guess.

    `why` decides the first line, and getting it wrong is not cosmetic. This
    said "--help could not be read" for every empty list, including the case
    that was actually happening on this cluster: --help ran, printed eight
    steps, and the parser did not recognise the format. Telling somebody their
    GenPipes install is unreachable when it answered perfectly sends them to
    debug the wrong thing -- and it hides a bug in here behind a complaint
    about their environment.

    The command is printed in full because it is the answer: run it, read the
    names, come back. Somebody who cannot see a list will otherwise invent one,
    and a section name GenPipes does not recognise is not an error -- it is
    ignored, and the run fails a second time in exactly the same way.
    """
    where = f"genpipes {pipeline or '<pipeline>'}"
    if protocol:
        where += f" -t {protocol}"
    reason = {
        STEPS_UNPARSEABLE:
            "--help was read, but this GenPipes prints its steps in a shape "
            "this tool does not recognise",
        STEPS_AMBIGUOUS:
            f"--help covers several protocols and none was chosen — say which "
            f"{pipeline or 'pipeline'} protocol this is",
    }.get(why, "--help could not be read")
    print()
    print(f"  {AMBER}▌{RESET} {BOLD}no step list to offer{RESET}  {DIM}·{RESET}"
          f"  {DIM}{reason}{RESET}")
    print(f"  {AMBER}▌{RESET}")
    print(f"  {AMBER}▌{RESET}   {GREY}the names are version-exact, so they are "
          f"never kept in this tool{RESET}")
    print(f"  {AMBER}▌{RESET}   {WHITE}{where} --help{RESET}")
    print(f"  {AMBER}▌{RESET}   {GREY}a name it does not recognise is ignored "
          f"silently, not refused{RESET}")
    print()


def forking_from(name, phrase):
    """Said before a /modify on a run that is not held, so the fork is expected.

    Somebody typing `/modify pouletrun` on a finished run is asking to change
    that run, and what they are about to get is a different one. Saying so
    first turns a surprise into an answer -- and it is the honest framing
    anyway: the original is not being edited, it is being copied from.

    `phrase` is the run's actual lifecycle state -- "live", "needs attention",
    "completed", "status unavailable" -- the same words /list uses (see
    runs.list_bucket()), never the raw registry status. "submitted" is true of
    every launched run whatever it is doing right now, so printing it here
    says nothing a person could act on.
    """
    print()
    print(f"  {DIM}▌{RESET} {BOLD}{name}{RESET}  {DIM}·{RESET}  "
          f"{DIM}{phrase}{RESET}")
    print(f"  {DIM}▌{RESET}   {GREY}already launched — changes will create a "
          f"new run{RESET}")
    print(f"  {DIM}▌{RESET}   {GREY}the original run and job list will remain "
          f"unchanged{RESET}")
    print()


def forked(original, new, standing="held"):
    """A /modify that made a second run instead of rewriting the first.

    Both names are printed, and the original first, because the whole reason to
    fork is that you want to keep it -- and a confirmation that named only the
    new run would leave somebody wondering what happened to the old one at the
    exact moment they were trying not to lose it.

    `standing` is where the ORIGINAL stands, in /list's words (cli._standing).
    It used to be the constant "still held", which was true when a fork could
    only come from a run parked at the gate. Forking from a launched run is the
    ordinary path now, and telling somebody their live run is "still held" gets
    wrong the one fact this line exists to confirm.

    The closing line follows from it: two held runs are "both waiting", and a
    variant of something already on the scheduler is not.
    """
    waiting = standing == "held"
    print()
    print(f"  {DIM}▌{RESET} {BOLD}{new}{RESET}  {DIM}·{RESET}  "
          f"{DIM}held, a variant of {original}{RESET}")
    print(f"  {DIM}▌{RESET}")
    print(f"  {DIM}▌{RESET}   {DIM}{original} is unchanged — {standing}{RESET}")
    print(f"  {DIM}▌{RESET}   {DIM}"
          f"{'both are waiting' if waiting else f'{new} is waiting for you'}"
          f"  ·  /list{RESET}")
    print()


@canonical
def prepared_retry(original, new, standing, applied=(), scope="",
                   scope_from="", uncertain=(), skipped=()):
    """What /relaunch built, and what it is still not sure about.

    Drawn INSTEAD OF forked() on the relaunch path, and the difference is not
    decoration. forked() answers one question -- which run is which -- and that
    is the whole of what a plain fork has to say, because a person who filled
    the panel in already knows what they changed. Here nobody filled anything
    in: the change came out of a diagnosis, deterministic code applied it, and
    this is the first and last screen where what was applied can be read before
    an allocation is spent on it.

    So the rows are in the order the questions arrive:

      original / revision   which run is which, original first, for forked()'s
                            reason -- the point of a retry is that the failed
                            run survives to be compared against.
      applied               the change itself, `was -> now` where a `was` is
                            known. This is the one thing nobody typed and
                            therefore the one thing that must be legible.
      relaunch              the step range, in words rather than as a flag. The
                            syntax belongs on the command, which /view and the
                            gate both print; what this row is for is that
                            somebody sees the retry covers the WHOLE protocol
                            and not just the step that broke.
      not established       genpipes.md's rule, carried to the last screen
                            before /approve. A walltime raised to a value
                            nothing proved sufficient is exactly as unproven
                            here as it was on the diagnosis, and this is where
                            somebody is about to act on it. AMBER, like the
                            same rows in diagnosis().
      skipped               settings the diagnosis proposed that were NOT
                            applied. Printed because a smaller file than the
                            fix described is the kind of difference nobody
                            notices, and silence about it would make this
                            screen a claim that the whole fix landed.
      unchanged             the original, in /list's own words for where it
                            now stands. The reassurance this whole flow is
                            arranged around, so it is stated rather than
                            implied by the absence of bad news.

    NO ACTIONS BLOCK. The gate has already drawn one for this revision by the
    time this prints -- /relaunch ends at the ordinary gate, which is where
    /approve, /modify and /reject belong and where the command itself is on
    screen to be read. A second set of the same three verbs under a summary
    would put the authorisation question on two screens, and the one that is
    not the gate is the one somebody would answer.
    """
    gut = f"  {DIM}▌{RESET}"
    print()
    print(f"{gut} {BOLD}{new}{RESET}  {DIM}·{RESET}  "
          f"{DIM}prepared retry of {original}{RESET}")
    print(gut)
    for i, (step, key, was, now) in enumerate(applied or ()):
        _labelled(gut, "applied" if i == 0 else "", f"{step}.{key}",
                  style=WHITE)
        # `was -> now` only where a `was` was actually observed. An arrow with
        # nothing on its left would invent a baseline, and the baseline here is
        # a reading of the run's own config trace that may not exist -- see
        # agent._trace_values.
        arrow = f"{DIM}{was} → {RESET}{now}" if was else str(now)
        print(f"{gut}   {'':<{LABEL_W}}{arrow}{RESET}")
    if scope:
        print(gut)
        _labelled(gut, "relaunch", scope, style=DIM)
    elif scope_from:
        print(gut)
        _labelled(gut, "relaunch", scope_from, style=DIM)
    if uncertain:
        print(gut)
        for i, item in enumerate(uncertain):
            _labelled(gut, "not established" if i == 0 else "", item,
                      style=AMBER)
    if skipped:
        print(gut)
        for i, (where, why) in enumerate(skipped):
            _labelled(gut, "not applied" if i == 0 else "",
                      f"{where} — {why}", style=AMBER)
    print(gut)
    print(f"{gut}   {DIM}{original} is unchanged — {standing}{RESET}")
    print(f"{gut}   {DIM}nothing has been submitted{RESET}")
    print()


# change_plan() lived here: a "Ready to apply" block listing every edit before
# a menu asked what to do with them. Removed with that menu (see cli, where
# _ask_ending was) for the reason the deltas never needed restating -- each one
# is already drawn on the row it belongs to, `old -> new` in green, which is
# nearer the thing it describes than any separate list can be. What follows the
# panel now is the gate, which is the review and the authorisation at once.

def reading_as(name, text):
    """Prose at the gate, and how it was understood. Printed before anything
    acts on it, so a misreading is visible while it is still cheap."""
    print()
    print(f"  {DIM}▌ Reading that as a change to {RESET}{WHITE}{name}{RESET}"
          f"{DIM}: {text}{RESET}")
    print()


def scan_results(root, found, added=(), skipped=(), restored=()):
    """What /scan discovered, and what happened to each of them.

    The directory is echoed because /scan takes a path and a person who typed
    the wrong one will otherwise read "no runs found" as a fact about GenPipes
    rather than about their typo.

    `restored` is reported separately from `added`, and the separation is the
    point rather than a nicety. Adding creates an identity; restoring returns
    one that already existed to /list, with its name, its history and its
    notes intact. Reporting a restore as an addition would suggest a second
    row had appeared for a run that already had one -- which is the exact
    confusion the rediscovery rules exist to prevent.
    """
    if not found:
        print()
        print(f"  {DIM}▌{RESET} {DIM}No GenPipes runs under{RESET} "
              f"{WHITE}{_tilde(root)}{RESET}")
        print(f"  {DIM}▌{RESET}   {GREY}a run is a directory with a "
              f"job_output/*.job_list.* in it{RESET}")
        print(f"  {DIM}▌{RESET}   {DIM}/scan <path> to look somewhere else{RESET}")
        print()
        return
    print()
    if added:
        print(f"  {GREEN}▌{RESET} {BOLD}{len(added)} run"
              f"{'s' if len(added) != 1 else ''} added{RESET}"
              f"  {DIM}from {_tilde(root)}{RESET}")
        for name in added:
            print(f"  {DIM}▌{RESET}   {WHITE}{name}{RESET}")
    else:
        print(f"  {DIM}▌{RESET} {DIM}Nothing added{RESET}"
              f"  {DIM}·  {len(found)} run(s) found under {_tilde(root)}{RESET}")
    if restored:
        print(f"  {GREEN}▌{RESET} {BOLD}{len(restored)} run"
              f"{'s' if len(restored) != 1 else ''} back in /list{RESET}"
              f"  {DIM}·  the same record, not a new one{RESET}")
        for name, why in restored:
            print(f"  {DIM}▌{RESET}   {BOLD}{name}{RESET}  {DIM}— {why}{RESET}")
    for name, why in skipped or ():
        print(f"  {DIM}▌{RESET}   {DIM}{name} — {why}{RESET}")
    gut = f"  {DIM}▌{RESET}"
    print(gut)
    # /list first: what /scan just did is put runs INTO the listing, so seeing
    # them there is the step that confirms it worked.
    actions([("/list",), ("/check", "<name>")], gutter=gut)
    print()


def scan_found(root, found):
    """The discovered runs, before any of them are adopted. Read-only, and
    said out loud: /scan touches nothing it finds."""
    print()
    print(f"  {DIM}▌{RESET} {BOLD}{len(found)} run"
          f"{'s' if len(found) != 1 else ''}{RESET}"
          f"  {DIM}under {_tilde(root)}  ·  nothing has been changed{RESET}")
    print()
