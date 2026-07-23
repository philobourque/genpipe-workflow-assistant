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
  SOLUTION  green. Where the model commits to a claim; read it closely.
  RUN       amber. Code about to hit the machine.
  OUT       bright label, grey body. The machine talking back -- present, quiet.
  PLAN      cyan, bold. The model's checklist, ticking over as the run proceeds.
  note      thin rule, grey. The model's connective prose. Present, but quiet.

Labels are always bright; bodies are dimmed only where the content is long and
secondary (machine output, connective prose). The signal is in the labels.
"""

import re
import os

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
# the human sits in the pipeline. Model names, framework versions and file paths
# are deliberately absent -- that is builder trivia, and it belongs in a README.
# ---------------------------------------------------------------------------

def banner():
    """Print the startup banner. The little pipeline diagram is not decoration:
    it teaches the whole idea before the user types anything -- what runs freely,
    where it stops, and who decides. GATE is the same red it will be when it
    actually fires, so the colour already means something by the time they see it.
    """
    arrow = f"{GREY}\u2500\u2500\u25b6{RESET}"

    print()
    print(f"  {BOLD}{CYAN}\u259a\u259a  G E N P I P E S{RESET}  {GREY}assistant{RESET}")
    print(f"  {CYAN}\u259a\u259a{RESET}  {GREY}plain English in. gated pipelines out.{RESET}")
    print()
    print(f"    {GREY}ask{RESET} {arrow} {GREY}generate{RESET} {arrow} {RED}{BOLD}GATE{RESET} "
          f"{arrow} {GREY}submit{RESET} {arrow} {GREY}watch{RESET}")
    print(f"                          {RED}\u25b2{RESET}")
    print(f"                          {RED}you{RESET}")
    print()
    print(f"  {GREY}{'\u2500' * 64}{RESET}")
    print()
    print(f"  {BOLD}start{RESET}      agent.run({GREEN}\"your task\"{RESET}, "
          f"thread_id={GREEN}\"name-this-run\"{RESET})")
    print(f"  {BOLD}approve{RESET}    agent.resume({GREEN}\"name-this-run\"{RESET}, "
          f"approved={AMBER}True{RESET})")
    print(f"  {BOLD}reject{RESET}     agent.resume({GREEN}\"name-this-run\"{RESET}, "
          f"approved={AMBER}False{RESET}, feedback={GREEN}\"...\"{RESET})")
    print(f"  {BOLD}progress{RESET}   agent.check({GREEN}\"name-this-run\"{RESET})")
    print(f"  {BOLD}list{RESET}       agent.submissions()")
    print()
    print(f"  {GREY}Name each run. The name is how you approve it, and how you check{RESET}")
    print(f"  {GREY}on it days later.{RESET}")
    print()
    print(f"  {DIM}e.g.  agent.run(\"run dnaseq germline_snv on my readset, all steps\",{RESET}")
    print(f"  {DIM}                thread_id=\"patient-42\"){RESET}")
    print()

# ---------------------------------------------------------------------------
# Layer 1 -- the parser. Text in, structured events out. No printing.
# ---------------------------------------------------------------------------

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

    # The user's own prompt, or the feedback text sent back on a rejection.
    if kind == "HumanMessage":
        return [{"kind": "prompt", "text": content.strip()}]

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

    # Code the model wants to run.
    for block in re.findall(r"<execute>(.*?)</execute>", body, re.DOTALL):
        events.append({"kind": "code", "text": block.strip()})

    # What the machine said back.
    for block in re.findall(r"<observation>(.*?)</observation>", body, re.DOTALL):
        events.append({"kind": "observation", "text": block.strip()})

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


def _draw(event):
    """Draw one parsed event."""
    k = event["kind"]

    if k == "prompt":
        _rule(CYAN, "\u258c", "YOU", event["text"])

    elif k == "note":
        # Connective prose. No label, thin rule, grey. Present but subordinate.
        _rule(GREY, "\u2502", "", event["text"], dim_body=True)

    elif k == "plan":
        print(f"{CYAN}\u258f {BOLD}PLAN{RESET}")
        for text, done in event["items"]:
            box = f"{GREEN}\u2713{RESET}" if done else f"{GREY}\u00b7{RESET}"
            print(f"{CYAN}\u258f{RESET}   [{box}]{text}")
        print()

    elif k == "code":
        _rule(AMBER, "\u258c", "RUN", event["text"])

    elif k == "observation":
        # Bright label so it is easy to find; grey body because machine output is
        # long and you are usually scanning it, not reading every line.
        _rule(CYAN, "\u258f", "OUT", event["text"], dim_body=True)

    elif k == "solution":
        _rule(GREEN, "\u258c", "SOLUTION", event["text"])


def render(message):
    """Parse a message and draw everything it contains."""
    for event in parse(message):
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

def gate(proposal, thread_id):
    """Print the submission gate: what is about to run, and how to answer it.

    The approve/reject commands are printed here on purpose. The moment you are
    asked to make a decision is the worst moment to be recalling an API.
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
    print(f"      {DIM}{'approve':<18}{RESET}agent.resume({WHITE}{thread_id!r}{RESET}, "
          f"approved={BOLD}True{RESET})")
    print(f"      {DIM}{'reject':<18}{RESET}agent.resume({WHITE}{thread_id!r}{RESET}, "
          f"approved={BOLD}False{RESET}, feedback=\"\u2026\")")
    print()
    print(f"  {DIM}Nothing has reached the scheduler.{RESET}")
    print("\n")


def post_approve(thread_id, approved):
    """Clean confirmation after the gate decision, replacing the raw dict dump."""
    if approved:
        print()
        print(f"  {DIM}\u258c {BOLD}{thread_id}{RESET}  {DIM}\u00b7{RESET}  {DIM}submitted{RESET}")
        print(f"  {DIM}\u258c{RESET}")
        print(f"  {DIM}\u258c{RESET}   agent.check({WHITE}{thread_id!r}{RESET})")
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

_BAD = {"FAILED", "TIMEOUT", "CANCELLED", "NODE_FAIL", "OUT_OF_MEMORY", "PREEMPTED"}
_ORDER = ["COMPLETED", "RUNNING", "PENDING", "FAILED", "TIMEOUT",
          "CANCELLED", "NODE_FAIL", "OUT_OF_MEMORY", "PREEMPTED"]


def status(name, raw):
    """Draw a run's progress from the raw text log_report prints.

    Parses rather than reformats: pulls the per-state counts and the timing lines
    out of GenPipes' output. If no total is found the raw text is printed
    unchanged -- better to show something unexpected than to hide it.
    """
    counts, total, meta = {}, 0, []
    for line in raw.splitlines():
        m = re.match(r"\s*Number of jobs ([A-Z_]+):\s*(\d+)", line)
        if m:
            counts[m.group(1)] = int(m.group(2))
            continue
        m = re.match(r"\s*Number of jobs:\s*(\d+)", line)
        if m:
            total = int(m.group(1))
            continue
        # GenPipes' timing labels are long and unaligned; shorten them.
        m = re.match(r"\s*Cumulative time spent on compute nodes:\s*(.+)", line)
        if m:
            meta.append(("compute time", m.group(1).strip()))
            continue
        m = re.match(r"\s*Cumulative core time:\s*(.+)", line)
        if m:
            meta.append(("core time", m.group(1).strip()))
            continue
        m = re.match(r"\s*Human time.*?:\s*(.+)", line)
        if m:
            meta.append(("elapsed", m.group(1).strip()))
            continue

    # Nothing recognisable -- show the raw output rather than swallow it.
    if not total:
        print(f"\n{raw.strip()}\n")
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

def submissions(runs):
    """List recorded submissions: name, and the job list behind it."""
    print()
    for name, path in runs.items():
        print(f"  {DIM}\u258c{RESET} {BOLD}{name}{RESET}")
        print(f"  {DIM}\u258c   {os.path.basename(path)}{RESET}")
    print(f"  {DIM}\u258c{RESET}")
    print(f"  {DIM}\u258c   agent.check(\"<name>\") for progress{RESET}")
    print()
