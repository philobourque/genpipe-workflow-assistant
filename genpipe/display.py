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
import shutil
import sys
import textwrap

from . import mirror
from . import modify

# ---------------------------------------------------------------------------
# ANSI escape codes. \033[<n>m sets an attribute; \033[0m clears everything.
# ---------------------------------------------------------------------------
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
AMBER = "\033[33m"
GREEN = "\033[32m"
CYAN = "\033[36m"
GREY = "\033[90m"
WHITE = "\033[37m"
REVERSE = "\033[7m"
UNDER = "\033[4m"

WIDTH = 74

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

# Two strands crossing. "\u259a" leans one way, "\u259e" the other, so a column of them
# reads as a diagonal; the dashes between are the base pairs. Six rows is one
# crossing -- enough to be unmistakably DNA, short enough to sit in a banner.
_HELIX = [
    "\u259a\u254c\u254c\u254c\u254c\u254c\u254c\u254c\u259e",
    "  \u259a\u254c\u254c\u254c\u259e  ",
    "   \u259a\u254c\u259e   ",
    "   \u259e\u254c\u259a   ",
    "  \u259e\u254c\u254c\u254c\u259a  ",
    "\u259e\u254c\u254c\u254c\u254c\u254c\u254c\u254c\u259a",
]

_STRAND = {"\u259a": f"{BOLD}{GREEN}", "\u259e": f"{GREEN}{DIM}", "\u254c": f"{GREY}{DIM}"}


def _helix():
    """The helix, coloured per glyph: one strand bright, the other shaded, the
    base pairs quiet. Two tones is what stops it reading as a flat texture."""
    return ["".join(f"{_STRAND[c]}{c}{RESET}" if c in _STRAND else c for c in row)
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

    So the nearest line is a question, and under it the three commands that
    answer "what can I do from here". Green, because every command everywhere in
    this interface is green.
    """
    print()
    print(f"  {BOLD}What can I help you with today?{RESET}")
    print()
    print(f"  {GREY}Ask a GenPipes question or describe the run you want.{RESET}")
    print()
    print(f"    {GREEN}/help{RESET} {GREY}commands{RESET}     "
          f"{GREEN}/list{RESET} {GREY}runs{RESET}     "
          f"{GREEN}/check all{RESET} {GREY}refresh statuses{RESET}")
    print()


def _centre(cell, w):
    """Leading spaces that centre a styled string in w columns.

    Measured on the visible text, so the escape sequences that colour the
    wordmark do not push it off centre -- which is exactly what a plain
    str.center() would do here.
    """
    return " " * max(0, (w - _vis_len(cell)) // 2) + cell


def _left_column(user, source, model, path):
    """The identity half: who you are, what this is.

    The model and the working directory used to close this column and now live
    on the right. They are reference, not identity, and while they sat here the
    left half was the taller of the two -- so the frame padded the right half
    with four blank rows to match, directly under "Once it's running", which is
    what made the box look badly balanced once the wordmark grew.
    """
    lines = [""]
    lines.append(f"{BOLD}Welcome back, {user}{RESET}")
    lines.append("")
    lines += [_centre(f"{BOLD}{GREEN}{row}{RESET}", _LEFT_W) for row in _WORDMARK]
    # The name in real letters under the glyphs that draw it. The wordmark is a
    # picture: it cannot be grepped out of a screenshot, pasted into an issue,
    # or read aloud by anything. Whatever the mark looks like, the product has
    # to say its own name in text somewhere.
    lines.append(_centre(f"{GREY}assistant{RESET}  {GREY}{DIM}{VERSION}{RESET}",
                         _LEFT_W))
    lines.append("")
    # Centred with the wordmark, not on its old fixed indent: the two are one
    # lockup, and a mark whose halves disagree about their centre line reads as
    # a mistake rather than as a choice.
    lines += [_centre(row, _LEFT_W) for row in _helix()]
    return lines


def _right_column(w, source=None, model=None, path=None):
    lines = [""]
    lines.append(f"{BOLD}Getting started{RESET}")
    lines += _wrap("Ask a question or describe the GenPipes run you want:", w)
    lines.append(f"  {WHITE}run dnaseq germline_snv on my readset, all steps{RESET}")
    lines.append(f"{GREY}{DIM}{'\u2500' * w}{RESET}")
    # Two rows, not one sentence: both are hand-styled, so neither can go
    # through _wrap() -- textwrap would count the escape sequences -- and a
    # single 60-column row clips at the widths where the box gives the right
    # column 49 to 59, taking the green / and Tab with it.
    lines.append(f"{GREY}Type {RESET}{GREEN}/{RESET}{GREY} to see available "
                 f"commands.{RESET}")
    lines.append(f"{GREY}Press {RESET}{GREEN}Tab{RESET}{GREY} to "
                 f"autocomplete.{RESET}")
    # Hand-styled for the same reason as the two rows above: _wrap counts escape
    # sequences as characters, so a command name coloured inside it either
    # clips or breaks the wrap. It stays green because every other command on
    # this screen is green, and the one that tells you where the rest of them
    # live is the last one that should look like prose.
    lines.append(f"{GREEN}/help{RESET}{GREY} brings the full list back at any "
                 f"time.{RESET}")
    lines.append(f"{GREY}{DIM}{'\u2500' * w}{RESET}")
    lines.append(f"{BOLD}Once it's running{RESET}")
    lines += _wrap("You can monitor it, cancel it, or diagnose a failure:", w)
    lines.append(f"  {GREEN}/check{RESET}  {GREEN}/jobs{RESET}  "
                 f"{GREEN}/cancel{RESET}  {GREEN}/diagnose{RESET}")
    # What this session is actually using. It closes the reference half rather
    # than the identity half because that is what it is -- something you look up
    # -- and because putting it here is what makes the two columns the same
    # height instead of padding one to match the other.
    lines.append(f"{GREY}{DIM}{'─' * w}{RESET}")
    lines.append(f"{GREY}{_identity(source, model)}{RESET}")
    lines.append(f"{DIM}{_tilde(path or '')}{RESET}")
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
    # The checkout, not the package directory: what someone reads off the
    # banner is the thing they would cd into or git pull.
    path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        cols = shutil.get_terminal_size((80, 24)).columns
    except Exception:
        cols = 80

    total = min(cols - 2, 104)
    left_w = _LEFT_W
    right_w = total - left_w - 7

    # One line in the right column can't be reflowed -- the example command --
    # and 49 columns is what it needs. Below that the two-column layout is being
    # forced, so stack instead: same content, no box.
    if right_w < 49:
        print()
        for line in _left_column(user, source, model, path):
            print(f"  {line}" if line else "")
        for line in _right_column(min(cols - 4, 60), source, model, path):
            print(f"  {line}" if line else "")
        print()
        return

    left = _left_column(user, source, model, path)
    right = _right_column(right_w, source, model, path)
    rows = max(len(left), len(right))
    left += [""] * (rows - len(left))
    right += [""] * (rows - len(right))

    edge = f"{GREEN}{DIM}"
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

# ---------------------------------------------------------------------------
# Layer 1 -- the parser. Text in, structured events out. No printing.
# ---------------------------------------------------------------------------

_ASK_ONLY = re.compile(r"^ask\s*\(.*\)$", re.DOTALL)

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

    Anything else is CODE. Deliberately: an unrecognised command is exactly the
    one you want to read closely, and dressing it up as something familiar would
    be the wrong kind of help.
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
    plan = re.findall(r"^\s*\d+\.\s*\[([ x\u2713v]?)\]\s*(.+)$", body, re.M | re.I)
    if plan:
        events.append({
            "kind": "plan",
            "items": [(text.strip(), mark.strip() != "") for mark, text in plan],
        })

    # Code the model wants to run -- except an <execute> block that only asks
    # the user a question. That block is not code and never runs; the panel it
    # opens is its rendering, and printing `RUN ask(slot="protocol")` just above
    # the panel would show the plumbing instead of the question.
    #
    # This is a rendering rule, not a second copy of ask()'s grammar: it drops a
    # block that is nothing but a call, and gate.ask_request remains the
    # only thing that decides what such a call means. A block that mixes an ask
    # with real code fails this test and is shown in full, which is the right
    # way round -- the router will not treat it as an ask either.
    for block in re.findall(r"<execute>(.*?)</execute>", body, re.DOTALL):
        # Comment lines are dropped before the test, because the model habitually
        # opens a block with "#!BASH" -- which the router ignores when it decides
        # what the block means, so the renderer has to ignore it too or the two
        # disagree about whether this is a question.
        bare = "\n".join(line for line in block.splitlines()
                         if line.strip() and not line.strip().startswith("#"))
        if _ASK_ONLY.match(bare.strip()):
            continue
        code = block.strip()
        events.append({"kind": "code", "text": code,
                       "label": _code_label(code)})

    # What the machine said back.
    events += _observations(body)

    # The model's final answer -- always shown in full.
    for block in re.findall(r"<solution>(.*?)</solution>", body, re.DOTALL):
        events.append({"kind": "solution", "text": block.strip()})

    # Whatever text remains once the structured parts are removed is the model's
    # connective prose. It is kept, not dropped, but rendered quietly. It goes
    # first, because the model writes "now let me..." before the thing it means.
    left = re.sub(r"<execute>.*?</execute>", "", body, flags=re.DOTALL)
    left = re.sub(r"<observation>.*?</observation>", "", left, flags=re.DOTALL)
    left = re.sub(r"<solution>.*?</solution>", "", left, flags=re.DOTALL)
    left = re.sub(r"</?think>", "", left)
    left = re.sub(r"^\s*\d+\.\s*\[[ x\u2713v]?\].*$", "", left, flags=re.M | re.I)
    left = "\n".join(line for line in left.splitlines() if line.strip())
    if left.strip():
        events.insert(0, {"kind": "note", "text": left.strip()})

    return events


# ---------------------------------------------------------------------------
# Layer 2 -- the terminal renderer.
# ---------------------------------------------------------------------------

def _rule(colour, mark, label, text, dim_body=False):
    """Print a block behind a left rule: a bright label, then the body.

    dim_body greys the content while leaving the label bright, which is how the
    long secondary blocks (machine output, connective prose) stay readable
    without competing with the things that matter.
    """
    shade = DIM if dim_body else ""
    if label:
        print(f"{colour}{mark} {BOLD}{label}{RESET}")
    for line in text.splitlines():
        print(f"{colour}{mark}{RESET} {shade}{line}{RESET}")
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
    task's -- two different jobs must not share one set of lines."""
    global _plan, _plan_lines
    _plan = None
    _plan_lines = 0


def _plan_body(items):
    """The block's lines, without the trailing blank.

    Three states, and the marker column is what distinguishes them, because
    colour alone does not survive being read at a glance or copied into a bug
    report:

      ✓  done      green
      ▶  current   the first unticked item, bright
         pending   no marker, dim -- it has not started, so it gets no ink
    """
    out = [f"  {CYAN}⏺{RESET} {BOLD}Plan{RESET}"]
    current = next((i for i, (_, done) in enumerate(items) if not done), None)
    for i, (text, done) in enumerate(items):
        if done:
            out.append(f"    {GREEN}✓{RESET} {DIM}{text}{RESET}")
        elif i == current:
            out.append(f"    {CYAN}▶{RESET} {text}")
        else:
            out.append(f"    {DIM}  {text}{RESET}")
    return out


def _draw_plan(items):
    """Draw or update the plan block."""
    global _plan, _plan_lines
    texts = [t for t, _ in items]
    body = _plan_body(items)

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
    _plan_lines = len(body) + 1


def _tty():
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


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
        _rule(GREY, "\u2502", "", event["text"], dim_body=True)

    elif k == "code":
        _rule(AMBER, "\u258c", event.get("label") or "CODE", event["text"])

    elif k == "observation":
        # Bright label so it is easy to find; grey body because machine output is
        # long and you are usually scanning it, not reading every line.
        _rule(CYAN, "\u258f", "TERMINAL", _clipped(event["text"]), dim_body=True)

    elif k == "answer":
        # One quiet line. The panel above it is the event; this is the receipt.
        print(f"{CYAN}{DIM}\u258f {_answer_line(event['text'])}{RESET}\n")

    elif k == "solution":
        # The reply itself. Plain text, no rule, no label -- this is the agent
        # talking, and in a two-party conversation the second party does not
        # need to be introduced. "SOLUTION" was Biomni's word for the tag and
        # made every answer sound like the end of an exercise.
        for line in (event["text"] or "").splitlines():
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
    """Draw every event folded so far. What /verbose shows for the work that
    has already happened -- without this, turning verbose on would only affect
    the next turn, which is never the turn you wanted it for."""
    if not _folded:
        nothing("Nothing has been folded away yet.")
        return
    print()
    print(f"  {DIM}│ {len(_folded)} step(s), replayed{RESET}")
    print()
    for event in _folded:
        _draw(event)


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
        if kind == "plan":
            # Never folded, at any verbosity. The fold's whole cost is that it
            # leaves you unable to tell what the agent is doing; the plan is
            # the answer to that, so folding it away with the rest would be
            # hiding the index along with the chapters.
            _draw_plan(event["items"])
            continue
        if kind == "code":
            _swallowing = event.get("label") in _HIDDEN
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
# volume. HOLD is the only coloured word in the block, because it is the only
# word that has to stop you. The command sits alone in whitespace because it is
# the thing actually being approved, and nothing should compete with it.
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
    print(f"  {GREEN}{DIM}{random.choice(_GOODBYES)}{RESET}\n")


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


def _mirror_state(row, active, pending, changed):
    """Which of the four states a mirror line is in, as (tint, strong).

    Three states plus the default, and each gets a colour AND a marker at the
    call site. Colour alone carries this badly -- it is the first thing a
    terminal theme, a screenshot or a colour-blind reader flattens -- and the
    whole point of the mirror is that somebody can SEE which line their cursor
    is about to change.

        pending   selected, not yet applied. Red. Reads as "about to move".
        changed   applied, and this is the run coming back. Green.
        active    the row under the cursor. Bright. Implies nothing -- it is
                  where you are looking, not what you have chosen.

    Checked in that order, so a row that is both active and pending reads as
    pending: what a line is about to become matters more than where the cursor
    happens to be resting.
    """
    if row is not None and row in pending:
        return RED, f"{RED}{BOLD}"
    if row is not None and row in changed:
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
        # A warning keeps its red whatever state the line is in; an observation
        # takes the line's own tint, so a row nobody has touched stays quiet.
        aside = RED if line.warn else (tint or DIM)
        body = (f"{body}  {DIM}{line.note}{RESET}" if body
                else f"{aside}{line.note}{RESET}")
    # A `-c` stack is the one row that is routinely several values, and stacking
    # them under each other is the only way its ORDER stays readable -- which is
    # the whole meaning of an ini stack.
    return f"{label}{flag}{body}", [f"{strong or DIM}{v}{RESET}" for v in values[1:]]


def mirror_lines(m, active=None, pending=(), changed=(), indent="      "):
    """The command mirror as a list of printable lines, for the gate.

    Returned rather than printed because it is drawn in two very different
    places: the gate prints it once and moves on, while modify_panel() repaints
    it on every cursor move with a checkbox beside each changeable line. A
    function that printed could not serve the second, and two renderers would
    drift on exactly the detail that has to match -- which line a row owns.
    """
    if not m:
        return []
    pending, changed = set(pending or ()), set(changed or ())
    out = []
    pad = " " * (_MIRROR_LABEL + _MIRROR_FLAG)

    for line in m.lines:
        tint, strong = _mirror_state(line.row, active, pending, changed)
        mark = (f"{RED}●{RESET}" if line.row in pending else
                f"{GREEN}●{RESET}" if line.row in changed else
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
    try:
        rows = shutil.get_terminal_size((80, 24)).lines
    except Exception:
        rows = 24
    return max(8, rows - 9)


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
                if entry.description and showing:
                    line += (f"{' ' * max(1, choicew - len(entry.label))}"
                             f"{DIM}{entry.description}{RESET}")
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

            # Three states and a default, each with a colour AND a marker, for
            # the reason _mirror_state gives: colour alone is the first thing a
            # theme, a screenshot or a colour-blind reader flattens.
            if open_here:
                tint, strong = RED, f"{RED}{BOLD}"
            elif moved:
                tint, strong = GREEN, f"{GREEN}{BOLD}"
            elif row in must:
                tint, strong = RED, f"{RED}{BOLD}"
            elif here:
                tint, strong = "", f"{BOLD}{WHITE}"
            else:
                tint, strong = DIM, ""

            dot = (f"{RED}●{RESET}" if open_here
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
            if open_here:
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
         resources=""):
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
    print(f"  {RED}{REVERSE}{BOLD} HOLD {RESET}  {RED}{UNDER}submission requires approval{RESET}")
    print()
    print(f"      {BOLD}{WHITE}{proposal.get('command', '?')}{RESET}")
    # `bash cmd.sh` is what runs and says nothing on its own -- two runs a week
    # apart submit the same three words -- so what that script was BUILT from is
    # the part worth reading, and with the agent's working folded away by
    # default this is the only place it is seen before it is approved. Laid out
    # by mirror.py rather than wrapped as prose: the people this is for know a
    # GenPipes command by its shape, and a paragraph does not have one.
    missing = proposal.get("missing") or ()
    m = (mirror.read(proposal.get("generated"), name=thread_id,
                     resources=resources, missing=missing)
         or mirror.from_slots(proposal, name=thread_id, resources=resources))
    drawn = mirror_lines(m, changed=changed)
    if drawn:
        print()
        for line in drawn:
            print(line)
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
    elif missing:
        # No separate line here -- the mirror above already carries a red
        # "not set \u2014 required" row per missing slot (see mirror._absent). A
        # second list saying the same names again would just be noise; what
        # changes is that /approve is withheld the same way it is for a
        # blocking environment finding, and for the same reason: offering an
        # action that cannot work invites trying it anyway.
        pass
    else:
        print(_action("/approve", "submits to Slurm \u2014 cannot be undone"))

    print(_action("/modify", "rewrites the command and asks you again"))
    print(_action("/reject", "abandons this run; nothing is submitted"))
    print(f"      {'':<{_MIRROR_LABEL + _MIRROR_FLAG}}{DIM}tab completes the name{RESET}")
    print()
    print(f"  {DIM}Nothing has reached the scheduler.{RESET}")
    print("\n")


# What each status lets you do, and what that costs. Held is the gate's own
# list; the others are what remains once a name is tied to a job list.
_VERBS = {
    "held": [("/approve", "submits to Slurm — cannot be undone"),
             ("/modify", "rewrites the command and asks you again"),
             ("/reject", "abandons this run; nothing is submitted")],
    "submitted": [("/check", "how it is doing on the scheduler"),
                  ("/modify", "copies it into a new run; this one is untouched"),
                  ("/diagnose", "read the logs and explain a failure")],
    "abandoned": [("/modify", "copies it into a new run to try again")],
    "gone": [("/modify", "copies it into a new run"),
             ("/history", "what is recorded about it")],
}


def run_view(proposal, name, status, resources="", blockers=()):
    """/view -- what a run IS, drawn for any status rather than only at the gate.

    The same mirror the gate draws, deliberately. A second layout for the same
    command would be a second thing to learn and a second place for the two to
    disagree, and the mirror's whole argument is that people know a GenPipes
    command by its shape.

    What differs is the frame and the verbs. There is no red HOLD banner --
    nothing is being asked here, and a box that shouts at somebody who typed a
    read-only command teaches them to ignore the shout. The verbs come from the
    status, because offering /approve on a run that went to Slurm an hour ago
    is offering something that cannot happen.
    """
    _flush_fold()
    print()
    print(f"  {DIM}▌{RESET} {BOLD}{name}{RESET}  {DIM}·{RESET}  {DIM}{status}"
          f"{RESET}")
    print()
    print(f"      {BOLD}{WHITE}{proposal.get('command', '?')}{RESET}")
    missing = proposal.get("missing") or ()
    m = (mirror.read(proposal.get("generated"), name=name, resources=resources,
                     missing=missing)
         or mirror.from_slots(proposal, name=name, resources=resources))
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
    for verb, consequence in _VERBS.get(status, ()):
        if verb == "/approve" and (blockers or missing):
            continue
        print(_action(verb, consequence))
    print(f"      {'':<{_MIRROR_LABEL + _MIRROR_FLAG}}{DIM}tab completes the "
          f"name{RESET}")
    print()


def ready(source=None, model=None, fake=None):
    """Printed right before the command loop takes over. Without this, the
    prompt that follows looks like a dead end rather than the normal, working
    state of the app -- especially once the banner has scrolled out of view
    behind a key prompt.

    It restates the model on purpose: on a first launch the banner printed
    before a key existed, so this is the first point at which the answer is
    actually known.

    `fake` names what is being simulated in dev mode. It is stated loudly and
    every single launch: a tool whose whole purpose is to be trusted with a
    cluster allocation must never leave you guessing whether what you are
    looking at was real.
    """
    if fake:
        print(f"  {AMBER}▌{RESET} {AMBER}{BOLD}dev mode{RESET}  {DIM}·{RESET}  "
              f"{GREY}{fake} — nothing here touches a real cluster{RESET}")
    print(f"  {GREEN}▌{RESET} {BOLD}ready{RESET}  {DIM}·{RESET}  "
          f"{GREY}{_identity(source, model)}{RESET}")


def post_approve(thread_id, approved):
    """Clean confirmation after the gate decision, replacing the raw dict dump."""
    if approved:
        print()
        print(f"  {DIM}\u258c {BOLD}{thread_id}{RESET}  {DIM}\u00b7{RESET}  {DIM}submitted{RESET}")
        print(f"  {DIM}\u258c{RESET}")
        print(f"  {DIM}\u258c{RESET}   /check {WHITE}{thread_id}{RESET}")
        print()
    else:
        print()
        print(f"  {DIM}\u258c {BOLD}{thread_id}{RESET}  {DIM}\u00b7{RESET}  {DIM}rejected, feedback sent{RESET}")
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
from .runs import BAD_STATES as _BAD, BROKE_STATES as _BROKE  # noqa: E402

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
            f"{RED}{DIM}{'▒' * n_cancel}{RESET}"
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
        print(gutter)
        print(f"{gutter}  {RED}{'root cause':<20}{RESET}{WHITE}{cause['step']}{RESET}"
              f"  {DIM}·{RESET}  {cause['count']} job(s) "
              f"{(cause['state'] or '').lower()}")
        if cause.get("timelimit"):
            print(f"{gutter}  {'':<20}{DIM}against a limit of "
                  f"{cause['timelimit']}{RESET}")
        # The jobs themselves. Named here rather than left to /jobs, because a
        # root cause without its jobs is a count: "2 job(s) timeout" does not
        # say that one is the tumour and one the matched normal, or that they
        # died 28 seconds apart. Capped, because one step failing across ninety
        # samples is a normal shape and printing ninety rows buries the tally
        # above it.
        listed = cause.get("jobs") or []
        for job in listed[:_CAUSE_JOBS]:
            detail = job.get("elapsed") or ""
            if job.get("maxrss"):
                detail = f"{detail}  {job['maxrss']}".strip()
            print(f"{gutter}  {'':<20}"
                  f"{DIM}{_job_tail(job.get('name'), cause['step']):<{_CAUSE_NAME_W}}"
                  f"{RESET}{DIM}{detail}{RESET}")
        if len(listed) > _CAUSE_JOBS:
            print(f"{gutter}  {'':<20}{GREY}+{len(listed) - _CAUSE_JOBS} more"
                  f"{RESET}")
        if cause.get("maxrss") and not listed:
            print(f"{gutter}  {'':<20}{DIM}peak memory {cause['maxrss']}{RESET}")
        if cause.get("cancelled_after"):
            print(f"{gutter}  {'':<20}{DIM}{cause['cancelled_after']} job(s) "
                  f"cancelled downstream — they never started{RESET}")

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

    if any(s in _BROKE for s in status.counts):
        print(gutter)
        print(f"{gutter}  {DIM}/diagnose {RESET}{WHITE}{name}{RESET}"
              f"{DIM}    read what the logs say{RESET}")
        print(f"{gutter}  {DIM}/jobs {RESET}{WHITE}{name}{RESET}"
              f"{DIM}   every job and its state{RESET}")

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


def nothing(text, hint=None):
    """A legitimately empty answer. Grey, not red -- an empty list is not an
    error, and colouring it like one trains people to ignore red."""
    print()
    print(f"  {DIM}\u258c{RESET} {DIM}{text}{RESET}")
    if hint:
        print(f"  {DIM}\u258c{RESET} {GREY}{hint}{RESET}")
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
    "submitted": lambda: f"{GREEN}live{RESET}",
    "gone": lambda: f"{DIM}gone{RESET}",
    # Terminal, and grey rather than red: it is not a problem, it is a decision
    # somebody made. It only ever appears in /history -- /list filters it out,
    # which is the entire reason the status exists.
    "abandoned": lambda: f"{DIM}abandoned{RESET}",
}


def _tag(record):
    return _STATUS_TAG.get(record.get("status"), lambda: f"{DIM}?{RESET}")()


def _snapshot(record):
    """The cached result of the last /check, if there was one.

    Explicitly labelled with when it was taken. A stale number presented as
    current is worse than no number, and this one is deliberately not refreshed
    on every /list -- see Registry.remember_check.
    """
    last = record.get("last_check")
    if not last:
        return None
    at = (last.get("at") or "").replace("T", " ")[5:16]
    return f"{last.get('verdict', '?')}  {GREY}(as of {at}){RESET}"


def run_list(records):
    """/list -- runs still worth acting on, held ones first.

    Held runs sort to the top because they are the only entries that are waiting
    on the person reading the list.
    """
    order = {"held": 0, "submitted": 1}
    records = sorted(records, key=lambda r: (order.get(r.get("status"), 2),
                                            r.get("submitted_at") or r.get("held_at") or ""))
    print()
    for r in records:
        print(f"  {DIM}\u258c{RESET} {BOLD}{r['name']}{RESET}  {DIM}\u00b7{RESET}  {_tag(r)}")
        if r["status"] == "held":
            cmd = ((r.get("proposal") or {}).get("command") or "").strip()
            if cmd:
                print(f"  {DIM}\u258c{RESET}   {WHITE}{cmd}{RESET}")
            print(f"  {DIM}\u258c{RESET}   {GREY}awaiting your approval{RESET}"
                  f"   {DIM}/approve \u00b7 /modify \u00b7 /reject{RESET}")
        else:
            snap = _snapshot(r)
            if snap:
                print(f"  {DIM}\u258c{RESET}   {snap}")
            if r.get("job_list"):
                print(f"  {DIM}\u258c   {os.path.basename(r['job_list'])}{RESET}")
            else:
                # A submission where every step was already up to date. Said
                # plainly, because "no jobs" reads as a failure otherwise.
                print(f"  {DIM}\u258c{RESET}   {GREY}no jobs \u2014 everything was already "
                      f"up to date{RESET}")
    print(f"  {DIM}\u258c{RESET}")
    print(f"  {DIM}\u258c   /check all \u00b7 /check <name> \u00b7 /jobs <name> \u00b7 "
          f"/diagnose <name>{RESET}")
    print(f"  {DIM}\u258c   /sort to hide rows \u00b7 /scan <path> to adopt runs "
          f"already on disk{RESET}")
    print()


def history(records):
    """/history -- every recorded run, live and gone, newest first.

    A gone entry is shown, marked as such, so a run can still be found after its
    job_list file has been cleaned up from Rorqual. Notes left by /diagnose are shown
    too: months later, "OOM in picard_mark_duplicates" is the only part of this
    record anyone still wants."""
    print()
    for r in records:
        when = (r.get("submitted_at") or r.get("held_at") or "").replace("T", " ")
        print(f"  {DIM}\u258c{RESET} {BOLD}{r['name']}{RESET}  {DIM}\u00b7{RESET}  {_tag(r)}"
              f"  {DIM}\u00b7{RESET}  {DIM}{r.get('source', 'agent')}{RESET}"
              f"  {DIM}\u00b7{RESET}  {DIM}{when}{RESET}")
        if r.get("job_list"):
            print(f"  {DIM}\u258c   {os.path.basename(r['job_list'])}{RESET}")
        for note in (r.get("notes") or [])[-2:]:
            print(f"  {DIM}\u258c{RESET}   {GREY}\u00b7 {note.get('text', '')}{RESET}")
    print(f"  {DIM}\u258c{RESET}")
    print()


JOB_NAME_W = 38


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
        if f.get("maxrss"):
            print(f"  {RED}\u258c{RESET}   {DIM}{'peak memory':<13}{f['maxrss']}{RESET}")
        if f.get("exit_code"):
            print(f"  {RED}\u258c{RESET}   {DIM}{'exit code':<13}{f['exit_code']}{RESET}")
        print(f"  {RED}\u258c{RESET}   {DIM}{'log':<13}"
              f"{os.path.basename(f['log']) if f.get('log') else 'not found'}{RESET}")
    if report.get("truncated"):
        print(f"  {RED}\u258c{RESET}   {GREY}+{report['truncated']} more step(s){RESET}")
    print(f"  {RED}\u258c{RESET}")
    print(f"  {DIM}  reading the logs, then explaining{RESET}")
    print()


def diagnosis(name, parsed, logs=()):
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
    """
    if not parsed.get("shaped"):
        print()
        for line in (parsed.get("prose") or "").splitlines():
            print(f"  {line}" if line.strip() else "")
        print()
        return

    confidence = parsed.get("confidence")
    tint = {"certain": WHITE, "likely": AMBER, "unclear": RED}.get(confidence, DIM)

    print()
    print(f"  {RED}▌{RESET} {BOLD}{name}{RESET}  {DIM}·{RESET}  "
          f"{DIM}diagnosis{RESET}"
          + (f"   {tint}{confidence}{RESET}" if confidence else ""))
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
                      _tilde(str(path)), style=DIM, wrap=False)
    if parsed.get("fix"):
        print(f"  {RED}▌{RESET}")
        _labelled(f"  {RED}▌{RESET}", "fix", parsed["fix"], style=WHITE)
    for section, keys in (parsed.get("override") or {}).items():
        print(f"  {RED}▌{RESET}")
        print(f"  {RED}▌{RESET}   {'':<18}{WHITE}[{section}]{RESET}")
        for key, value in keys.items():
            print(f"  {RED}▌{RESET}   {'':<18}{DIM}{key} = {RESET}{value}")
    if parsed.get("relaunch"):
        print(f"  {RED}▌{RESET}")
        _labelled(f"  {RED}▌{RESET}", "resubmit", parsed["relaunch"])
        print(f"  {RED}▌{RESET}   {'':<18}{DIM}the whole range — GenPipes skips "
              f"steps that already have output{RESET}")
    print(f"  {RED}▌{RESET}")
    if parsed.get("override"):
        print(f"  {RED}▌{RESET}   {DIM}/modify {RESET}{WHITE}{name}{RESET}"
              f"{DIM}   writes this into the run's override ini{RESET}")
    print(f"  {RED}▌{RESET}   {DIM}/jobs {RESET}{WHITE}{name}{RESET}"
          f"{DIM}    every job and its state{RESET}")
    print()


def _labelled(gutter, label, text, style="", wrap=True):
    """A label column and a wrapped body, the shape the gate uses. The label is
    printed once and the continuation lines align under the body, so a long
    sentence stays one visual block instead of becoming several rows.

    `wrap=False` is for paths. Wrapping one breaks it across lines mid-token,
    which costs the only thing a printed path is for: being copied. Letting the
    terminal overflow is uglier and works.
    """
    body = (textwrap.wrap(str(text), max(24, WIDTH - 26)) or [""] if wrap
            else [str(text)])
    for i, line in enumerate(body):
        shown = label if i == 0 else ""
        print(f"{gutter}   {DIM}{shown:<18}{RESET}{style}{line}{RESET}")


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


def where(paths):
    """/where -- the directories that decide where everything lands.

    Worth a command of its own because one of these silently determines whether
    a submission gets registered at all: the job list is looked for under the
    directory the app was launched from, and nothing else in the interface shows
    you what that is.
    """
    print()
    width = max(len(k) for k, _ in paths) + 2
    for label, value in paths:
        print(f"  {DIM}\u258c{RESET} {DIM}{label:<{width}}{RESET}{_tilde(str(value))}")
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

    print(f"  {DIM}/check <name>{RESET}{DIM}  ·  /diagnose <name>  ·  /jobs <name>"
          f"  ·  /scan [path]{RESET}")
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


def no_step_list(pipeline, protocol):
    """Why the step panel is offering nothing, and how to get the list.

    There is no step table in this repo and there must never be one --
    genpipes.md says so outright, because the numbered list for every protocol
    is version-exact and a copy here would be wrong on the next release. So
    when `--help` cannot be reached, the honest thing is an empty panel plus
    this, rather than a guess.

    The command is printed in full because it is the answer: run it, read the
    names, come back. Somebody who cannot see a list will otherwise invent one,
    and a section name GenPipes does not recognise is not an error -- it is
    ignored, and the run fails a second time in exactly the same way.
    """
    where = f"genpipes {pipeline or '<pipeline>'}"
    if protocol:
        where += f" -t {protocol}"
    print()
    print(f"  {AMBER}▌{RESET} {BOLD}no step list to offer{RESET}  {DIM}·{RESET}"
          f"  {DIM}--help could not be read{RESET}")
    print(f"  {AMBER}▌{RESET}")
    print(f"  {AMBER}▌{RESET}   {GREY}the names are version-exact, so they are "
          f"never kept in this tool{RESET}")
    print(f"  {AMBER}▌{RESET}   {WHITE}{where} --help{RESET}")
    print(f"  {AMBER}▌{RESET}   {GREY}a name it does not recognise is ignored "
          f"silently, not refused{RESET}")
    print()


def forking_from(name, status):
    """Said before a /modify on a run that is not held, so the fork is expected.

    Somebody typing `/modify pouletrun` on a finished run is asking to change
    that run, and what they are about to get is a different one. Saying so
    first turns a surprise into an answer -- and it is the honest framing
    anyway: the original is not being edited, it is being copied from.
    """
    print()
    print(f"  {DIM}▌{RESET} {BOLD}{name}{RESET}  {DIM}·{RESET}  "
          f"{DIM}{status}, so this makes a new run{RESET}")
    print(f"  {DIM}▌{RESET}   {GREY}its name is tied to a job list and cannot "
          f"be rewritten{RESET}")
    print()


def forked(original, new):
    """A /modify that made a second run instead of rewriting the first.

    Both names are printed, and the original first, because the whole reason to
    fork is that you want to keep it -- and a confirmation that named only the
    new run would leave somebody wondering what happened to the old one at the
    exact moment they were trying not to lose it.
    """
    print()
    print(f"  {DIM}▌{RESET} {BOLD}{new}{RESET}  {DIM}·{RESET}  "
          f"{DIM}held, a variant of {original}{RESET}")
    print(f"  {DIM}▌{RESET}")
    print(f"  {DIM}▌{RESET}   {DIM}{original} is unchanged and still held{RESET}")
    print(f"  {DIM}▌{RESET}   {DIM}both are waiting  ·  /list{RESET}")
    print()


def change_plan(deltas, notes=()):
    """The apply screen for a guided /modify: every change as old → new.

    Shown for every change set, including a single row. It used to be skipped
    for one change on the grounds that the re-rendered gate is already the
    review -- true when the only question was "apply or not", and false now that
    the screen underneath it asks which of four things should happen to the
    change. Holding it for later and forking it into a second run are not
    confirmations of a decision already made; they are the decision.
    """
    print()
    print(f"  {GREEN}▌{RESET} {BOLD}Ready to apply{RESET}")
    print()
    width = max((len(d[0]) for d in deltas), default=8) + 4
    for slot, old, new in deltas:
        old_text = old if old not in (None, "") else "—"
        print(f"    {WHITE}{slot:<{width}}{RESET}{DIM}{old_text}{RESET}"
              f"  {DIM}→{RESET}  {BOLD}{new}{RESET}")
    for note in notes or ():
        print()
        print(f"    {DIM}{'note':<{width}}{RESET}{AMBER}{note}{RESET}")
    print()


def reading_as(name, text):
    """Prose at the gate, and how it was understood. Printed before anything
    acts on it, so a misreading is visible while it is still cheap."""
    print()
    print(f"  {DIM}▌ Reading that as a change to {RESET}{WHITE}{name}{RESET}"
          f"{DIM}: {text}{RESET}")
    print()


def scan_results(root, found, added=(), skipped=()):
    """What /scan discovered and what was adopted from it.

    The directory is echoed because /scan takes a path and a person who typed
    the wrong one will otherwise read "no runs found" as a fact about GenPipes
    rather than about their typo.
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
    for name, why in skipped or ():
        print(f"  {DIM}▌{RESET}   {DIM}{name} — {why}{RESET}")
    print(f"  {DIM}▌{RESET}")
    print(f"  {DIM}▌{RESET}   {DIM}/list  ·  /check <name>  ·  /check all{RESET}")
    print()


def scan_found(root, found):
    """The discovered runs, before any of them are adopted. Read-only, and
    said out loud: /scan touches nothing it finds."""
    print()
    print(f"  {DIM}▌{RESET} {BOLD}{len(found)} run"
          f"{'s' if len(found) != 1 else ''}{RESET}"
          f"  {DIM}under {_tilde(root)}  ·  nothing has been changed{RESET}")
    print()
