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

Nothing is hidden. Every part of every message is shown -- the visual hierarchy
just makes clear what matters most:

  GATE      heavy red box. The one moment the run stops and needs a human.
  ASSISTANT green. Where the agent commits to a claim; read it closely.
  GENERATE  amber. Writing the pipeline script.        } one <execute> block,
  SUBMIT    amber. Putting it on the scheduler.        } labelled by what it
  SCHEDULER amber. Asking Slurm or GenPipes how it is. } is actually doing --
  CODE      amber. Anything else about to run.         } see _code_label
  TERMINAL  bright label, grey body. The machine talking back -- present, quiet.
  answered  one dim line. The receipt for a choice panel.
  note      thin rule, grey. The model's connective prose. Present, but quiet.

Two things are parsed and then not drawn -- a documentation lookup and its output,
and the model's checklist. See render() for why.

The person's own turns are labelled with their name (see who()), and the agent's
with ASSISTANT. Two named speakers, which is what a transcript of a conversation
is -- "YOU" and "SOLUTION" described the two halves of a form.

Labels are always bright; bodies are dimmed only where the content is long and
secondary (machine output, connective prose). The signal is in the labels.
"""

import getpass
import os
import random
import re
import shutil
import textwrap

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


def _left_column(user, source, model, path):
    lines = [""]
    lines.append(f"{BOLD}Welcome back, {user}{RESET}")
    lines.append("")
    lines += [f"   {row}" for row in _helix()]
    lines.append("")
    lines.append(f"{BOLD}{GREEN}GenPipes{RESET} {BOLD}assistant{RESET}  "
                 f"{GREY}{VERSION}{RESET}")
    lines.append(f"{GREY}{_identity(source, model)}{RESET}")
    lines.append(f"{DIM}{_tilde(path)}{RESET}")
    return lines


def _right_column(w):
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
    lines += _wrap("/help brings the full list back at any time.", w)
    lines.append(f"{GREY}{DIM}{'\u2500' * w}{RESET}")
    lines.append(f"{BOLD}Once it's running{RESET}")
    lines += _wrap("You can monitor it, cancel it, or diagnose a failure:", w)
    lines.append(f"  {GREEN}/check{RESET}  {GREEN}/jobs{RESET}  "
                 f"{GREEN}/cancel{RESET}  {GREEN}/why{RESET}")
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
    path = os.path.dirname(os.path.abspath(__file__))
    try:
        cols = shutil.get_terminal_size((80, 24)).columns
    except Exception:
        cols = 80

    total = min(cols - 2, 104)
    left_w = 32
    right_w = total - left_w - 7

    # One line in the right column can't be reflowed -- the example command --
    # and 49 columns is what it needs. Below that the two-column layout is being
    # forced, so stack instead: same content, no box.
    if right_w < 49:
        print()
        for line in _left_column(user, source, model, path):
            print(f"  {line}" if line else "")
        for line in _right_column(min(cols - 4, 60)):
            print(f"  {line}" if line else "")
        print()
        return

    left = _left_column(user, source, model, path)
    right = _right_column(right_w)
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
    there is one table, in launch_agent.py.

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
# codebase (the observation wrapper, the gate's rejection note, genpipe_agent's
# NUDGE) and one by biomni's generate node, which corrects a model that replied
# without a tag. Matched by content because that is all a message carries -- the
# API has no notion of "the graph said this", and a role is the only field there
# is. Kept here rather than imported so this module stays stdlib-only.
_NUDGE = "[continue]"                                   # genpipe_agent.NUDGE
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
    # block that is nothing but a call, and gate_rules.ask_request remains the
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


def _draw(event):
    """Draw one parsed event."""
    k = event["kind"]

    if k == "prompt":
        # Their own name, not "YOU". Upper-cased to sit in the same column of
        # labels as RUN and OUT -- it is a speaker label, and a transcript where
        # one name is styled differently from the others reads as a mistake.
        _rule(CYAN, "\u258c", who().upper(), event["text"])

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
        # "SOLUTION" is Biomni's word for the tag, and it made every reply sound
        # like the answer to an exercise. Most of what arrives in one now is the
        # agent talking -- an answer to a question, an explanation, a refusal --
        # so it is labelled as the speaker it is.
        _rule(GREEN, "\u258c", "ASSISTANT", event["text"])


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


def render(message):
    """Parse a message and draw what is worth drawing.

    Two things are deliberately not drawn, and both are the same judgement --
    that a transcript is for following the work, not for auditing the agent:

      the model's checklist   It re-emits the whole list every turn with one more
                              box ticked, which is six lines of repetition for one
                              line of news. What it is doing now is on screen
                              anyway, in the block underneath.
      documentation lookups   see _HIDDEN.

    Everything else -- prose, code, output, answers, conclusions -- is shown.
    """
    global _swallowing
    for event in parse(message):
        kind = event["kind"]
        if kind == "plan":
            continue
        if kind == "code":
            _swallowing = event.get("label") in _HIDDEN
            if _swallowing:
                continue
        elif kind == "observation" and _swallowing:
            _swallowing = False
            continue
        _draw(event)

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


def gate(proposal, thread_id, blockers=()):
    """Print the submission gate: what is about to run, and how to answer it.

    The approve/reject commands are printed here on purpose. The moment you are
    asked to make a decision is the worst moment to be recalling an API.

    `blockers` are environment findings that would make this submission fail no
    matter how good the command is. When there are any, the approve line is
    replaced rather than merely annotated: offering an action that cannot work,
    next to an explanation of why it cannot work, invites trying it anyway.
    """
    slots = proposal.get("slots") or {}

    rows = []
    if slots.get("protocol"):
        rows.append(("protocol", slots["protocol"]))
    if slots.get("steps"):
        rows.append(("steps", slots["steps"]))
    if slots.get("inis"):
        # dict.fromkeys de-duplicates while keeping order: the same ini can be
        # matched twice, once by full path and once by bare filename.
        rows.append(("config", " , ".join(dict.fromkeys(slots["inis"]))))
    if slots.get("design"):
        rows.append(("design", os.path.basename(str(slots["design"]))))
    if slots.get("pairs"):
        rows.append(("pairs", os.path.basename(str(slots["pairs"]))))
    if slots.get("output_dir"):
        rows.append(("output", slots["output_dir"]))
    else:
        rows.append(("output", f"{RED}cwd (no -o flag){RESET}"))

    print("\n")
    print(f"  {RED}{REVERSE}{BOLD} HOLD {RESET}  {RED}{UNDER}submission requires approval{RESET}")
    print()
    print(f"      {BOLD}{WHITE}{proposal.get('command', '?')}{RESET}")
    print()
    for label, value in rows:
        print(f"      {DIM}{label:<18}{RESET}{value}")
    if rows:
        print()
    if blockers:
        for finding in blockers:
            print(f"      {RED}{'cannot submit':<18}{RESET}"
                  f"{finding.variable} {finding.problem}")
            print(f"      {DIM}{'fix':<18}{RESET}{WHITE}{finding.fix}{RESET}")
        print()
        print(f"      {DIM}{'reject':<18}{RESET}/reject {WHITE}{thread_id}{RESET} \u2026")
        print()
        print(f"  {DIM}Nothing has reached the scheduler. Fix the above and "
              f"restart to approve.{RESET}")
        print("\n")
        return

    print(f"      {DIM}{'approve':<18}{RESET}/approve {WHITE}{thread_id}{RESET}")
    print(f"      {DIM}{'reject':<18}{RESET}/reject {WHITE}{thread_id}{RESET} \u2026")
    print()
    print(f"  {DIM}Nothing has reached the scheduler.{RESET}")
    print("\n")


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

_BAD = {"FAILED", "TIMEOUT", "CANCELLED", "NODE_FAIL", "OUT_OF_MEMORY",
        "PREEMPTED", "BOOT_FAIL", "DEADLINE"}
# States meaning this job itself broke. CANCELLED is not one: a GenPipes failure
# cancels everything downstream of it, so those jobs never ran. Kept in step with
# runs.BROKE_STATES, and separate from _BAD because both distinctions are needed
# -- red still marks anything wrong, but the counts must not conflate the two.
_BROKE = _BAD - {"CANCELLED"}
_ORDER = ["COMPLETED", "RUNNING", "PENDING", "FAILED", "TIMEOUT",
          "CANCELLED", "NODE_FAIL", "OUT_OF_MEMORY", "PREEMPTED",
          "BOOT_FAIL", "DEADLINE", "UNKNOWN"]


def status(name, parsed, raw=""):
    """Draw a run's progress from log_report's already-parsed counts.

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
            print(f"  {DIM}\u258c{RESET}   {GREY}awaiting your approval{RESET}")
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
    print(f"  {DIM}\u258c   /check <name> \u00b7 /jobs <name> \u00b7 /why <name>{RESET}")
    print()


def history(records):
    """/history -- every recorded run, live and gone, newest first.

    A gone entry is shown, marked as such, so a run can still be found after its
    job_list file has been cleaned up from Rorqual. Notes left by /why are shown
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
        # Offered only when something actually broke: /why on a run whose jobs
        # were merely cancelled downstream has nothing to diagnose.
        print(f"  {DIM}\u258c   /why {WHITE}{name}{RESET}{DIM} to diagnose{RESET}")
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


def pending(records):
    """Held runs, surfaced at startup -- in one line, however many there are.

    This exists because of the tool's worst failure mode: the gate pauses a run,
    the terminal closes, and the only record of a decision you still owe was the
    name in your head. Everything else in the interface can wait until asked.
    This cannot.

    But it is a reminder, not a report. Printing every held command turned a
    fortnight of experiments into the largest thing on a fresh screen, and pushed
    the one line that matters -- the prompt -- to the bottom of the scrollback.
    Names first three, count for the rest, /list for the commands.
    """
    if not records:
        return
    n = len(records)
    names = ", ".join(r["name"] for r in records[:3])
    if n > 3:
        names += f", +{n - 3} more"
    print()
    print(f"  {AMBER}▌{RESET} {AMBER}{n} run{'s' if n > 1 else ''} held{RESET}"
          f"{DIM}, waiting on you:{RESET} {WHITE}{names}{RESET}"
          f"   {DIM}/list{RESET}")
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
