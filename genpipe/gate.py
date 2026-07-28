"""The router's decision logic, as pure functions.

Two questions are answered here, both asked of the same thing -- the code the
model just wrote inside an <execute> block -- and both answered before that code
runs:

    is_submission(code)   is this about to put work on the scheduler?
    ask_request(code)     is this the agent asking the user a question?

The first is the safety-critical one and the reason this module exists. The
second rides along because it is decided at the same moment, by the same router,
from the same text; keeping them apart would mean two parsers disagreeing about
what a code block is.

It lives in its own module, importing nothing but the standard library, for two
reasons.

  1. It can be tested anywhere. genpipe/agent.py imports biomni, which drags in
     langchain, langgraph and a pinned checkpoint stack -- fine on the cluster,
     too heavy to install in CI just to ask "is `bash cmd.sh` a submission?".
     Because this module is stdlib-only, the gate's invariants run on every push
     in a couple of seconds. The one property that must never regress is the one
     that is cheapest to check.

  2. It cannot drift. GenpipeA1 keeps thin methods that delegate here, so the
     graph's routing decision and the tests exercise the same code, not two
     copies of the same idea.

The functions take plain data -- strings, and lists of objects with a .content
attribute -- rather than LangGraph state, so nothing here needs a graph to run.
"""
import re

# Text that means "this really submits". Searched against executable_lines(),
# never the raw block -- see that function for why.
_SUBMIT_PATTERNS = [
    r"\b(?:bash|sh)\s+\S+\.sh\b",   # bash <script>.sh -- any generated submission script
    r"\bsubmit_genpipes\b",         # DRAC submit
    r"\bchunk_genpipes\.sh\b",      # DRAC chunk (precedes submit)
]


def extract_pending_code(messages):
    """The code the model just proposed, pulled from the last message.

    Takes the message list rather than the graph state so this is callable with
    anything that has a .content attribute.
    """
    if not messages:
        return None
    last = getattr(messages[-1], "content", "") or ""
    # Tolerate an unclosed tag, which happens when a message is cut short.
    if "<execute>" in last and "</execute>" not in last:
        last += "</execute>"
    m = re.search(r"<execute>(.*?)</execute>", last, re.DOTALL)
    return m.group(1) if m else None


def _drop_trailing_comment(line):
    """Cut a line at the # that starts a comment, leaving the code before it.

    Two conditions, both needed to avoid cutting something that does execute:

      outside quotes      `echo "size # 3"` has no comment in it, and neither
                          does a URL fragment or an awk program.
      preceded by space    `${VAR#prefix}` and `git show HEAD#1` are parameter
                          expansion and an argument, not comments. Requiring
                          whitespace (or start of line) before the # is what
                          distinguishes a comment from a # used as punctuation.

    Cutting can only ever REMOVE text, and the text removed provably does not
    run, so this cannot introduce a false negative -- the direction that
    matters.
    """
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line


def executable_lines(code):
    """Return only the parts of a code block that actually run.

    A block can *mention* a submission without performing one. The model does
    this constantly when it summarizes or explains its own work, and it does
    it in two languages:

        bash dnaseq_cmd.sh                 <- runs: submits the pipeline
        echo "  bash dnaseq_cmd.sh"        <- shell: prints the words only
        print("  bash dnaseq_cmd.sh")      <- python: prints the words only

    All three contain the same text, so searching the raw block cannot tell
    them apart. Both false positives happened for real:

      2026-07-13, shell: after an approved submission, the model echoed a
      summary of what it had done. The gate caught the summary as a second
      submission, and rejecting it only produced another summary. Stuck.

      2026-07-14, python: on a generation-only task, the model ended with
      print("  bash launch_cmd.sh") inside a "here is how you would submit
      this" note -- while explicitly saying it was NOT submitting. The gate
      fired before the code ran, so the script was never even generated.

    So before asking "is this a submission?", strip the places text is written
    but never executed:

        - comments              (# anything)
        - echo arguments        (shell prints its words, it does not run them)
        - heredoc bodies        (cat << 'EOF' ... EOF is a block of plain text)
        - print() arguments     (python prints its words, it does not run them)

    What remains is only code that would truly execute, and is_submission
    searches that instead of the raw block.

    WHY ONLY print(), AND NOT ALL PYTHON STRINGS. In python a quoted string is
    sometimes inert and sometimes the command itself:

        print("bash cmd.sh")                       <- inert
        subprocess.run("bash cmd.sh", shell=True)  <- a real submission

    Stripping every string would blind the matcher to the second, which is a
    false negative: a real submission slipping through ungated. That is the
    dangerous direction. A false positive only costs a rejection; a false
    negative costs an unapproved pipeline on the cluster. So this strips only
    what is handed to print(), the one construct we are confident is inert,
    and leaves every other string intact so subprocess and os.system calls are
    still caught.

    A rule of thumb, not a parser. A submission hidden in a variable that is
    never spelled out, an eval, or a $(...) would still slip through. That is
    accepted: the job is to stop misreading a cooperative model's own summary,
    not to defeat someone deliberately hiding a command.
    """
    # Nothing to look at.
    if not code:
        return ""

    kept = []            # the lines we decide are real code
    in_heredoc = False   # are we currently inside a block of heredoc text?
    end_word = None      # the word that will close that heredoc (often "EOF")

    # .splitlines() turns the one big block of code into a list of single lines,
    # so we can judge each line on its own.
    for line in code.splitlines():

        # .strip() removes spaces from both ends, so an indented "   EOF"
        # still matches the plain word "EOF".
        bare = line.strip()

        # --- Case 1: we are inside a heredoc -------------------------------
        # Everything here is text handed to a command, not code. Skip it. The
        # only thing we watch for is the closing word, which ends the block.
        if in_heredoc:
            if bare == end_word:
                in_heredoc = False
            continue     # "continue" = skip to the next line, keep nothing

        # --- Case 2: does this line START a heredoc? -----------------------
        # A heredoc opens like:   cat << EOF    or    cat << 'EOF'
        # We grab the closing word so we know when the text block ends.
        opener = re.search(r"<<-?\s*['\"]?(\w+)['\"]?", line)
        if opener:
            in_heredoc = True
            end_word = opener.group(1)   # group(1) = the captured word, e.g. EOF
            continue                     # the opener line is not a command

        # --- Case 3: a comment ---------------------------------------------
        # Anything starting with # never runs. Covers both shell and python.
        if bare.startswith("#"):
            continue

        # --- Case 3b: a comment at the END of a real line ------------------
        # `genpipes ... -g cmd.sh   # then: bash cmd.sh` is a generation, and
        # the gate fired on it because only whole comment LINES were stripped.
        # Models annotate what comes next constantly, so this was a false
        # positive waiting to happen on ordinary work.
        line = _drop_trailing_comment(line)

        # --- Case 4: strip shell echo's arguments --------------------------
        # echo only prints its words. Delete "echo" and everything after it,
        # but stop at ; | or & so a real command chained on the same line
        # survives:   echo hi && bash cmd.sh   ->   " && bash cmd.sh"
        line = re.sub(r"\becho\b[^;|&\n]*", "", line)

        # --- Case 5: strip python print's arguments ------------------------
        # print(...) only writes to the screen. Replace the whole call with an
        # empty print(), so anything quoted inside it -- including a submission
        # command the model is merely describing -- is gone. Strings passed to
        # subprocess, os.system, eval and friends are deliberately NOT touched:
        # those really do execute, and must still trip the gate.
        line = re.sub(r"\bprint\s*\([^)]*\)", "print()", line)

        # Whatever is left on this line is treated as real code.
        kept.append(line)

    # Join the surviving lines back into one block for the matcher to search.
    return "\n".join(kept)


def is_submission(code):
    """True only for code that actually submits: bash cmd.sh, or the DRAC
    chunk_genpipes.sh / submit_genpipes path. Generation and reads return False.

    The whole gate reduces to this function returning True at the right moments.
    A false positive costs a rejection; a false negative puts an unapproved
    pipeline on the cluster. The asymmetry is why executable_lines() strips only
    what it is certain is inert.
    """
    if not code:
        return False
    stripped = executable_lines(code)
    return any(re.search(p, stripped) for p in _SUBMIT_PATTERNS)


# ---------------------------------------------------------------------------
# ask(): the agent's way of putting a question to the person at the keyboard.
#
# It travels inside <execute> rather than as a tag of its own, and that is not a
# shortcut -- it is the only shape that does not require forking Biomni. A1's
# generate node recognises exactly three tags (<think>, <execute>, <solution>);
# anything else falls into its parsing-error branch, where the model is scolded
# and told to regenerate. Riding inside <execute> means generate needs no change
# at all: it routes to "execute" as usual, and routing_function -- already the
# place where "what kind of action is this?" is decided -- diverts asks to the
# ask node the same way it diverts submissions to the gate.
#
# The call is parsed, never evaluated. Nothing here executes model-written code.
# ---------------------------------------------------------------------------

_ASK_CALL = re.compile(r"\bask\s*\(\s*(.*?)\s*\)", re.DOTALL)

# A quoted string, allowing the prefixes a model reaches for out of habit --
# f"...", r'...'. The prefix matters more than it looks: ask(question=f"Which
# steps of {pipeline}?") parsed as zero arguments, which used to mean "not an ask"
# and sent the call to the Python interpreter, where it died as
# NameError: name 'ask' is not defined.
_QUOTED = r"(?:[rbfuRBFU]{0,2})(\"[^\"]*\"|'[^']*')"

# key="value", the shape the prompt asks for.
_ASK_ARG = re.compile(r"(\w+)\s*=\s*" + _QUOTED)

# A bare string, for the model that writes ask("Which steps?") positionally.
# Applied only after the keyword arguments have been cut out of the text.
_ASK_POSITIONAL = re.compile(_QUOTED)

# What the panel can build a real menu for. Anything else is still askable --
# it just arrives as a plain question with no options, which is the honest
# rendering of a question this tool has no table for.
ASK_SLOTS = ("pipeline", "protocol", "readset", "design", "pairs")


def ask_request(code):
    """Parse an ask() call out of a code block, or None if there isn't one.

    Returns a dict of its keyword arguments, e.g.

        ask(slot="protocol", pipeline="dnaseq")
            -> {"slot": "protocol", "pipeline": "dnaseq"}
        ask(question="Which steps?")
            -> {"slot": None, "question": "Which steps?"}
        ask("Which steps?")
            -> {"slot": None, "question": "Which steps?"}

    ONCE THE CALL IS THERE, THIS ALWAYS RETURNS A REQUEST. That is the rule the
    whole function is arranged around, and it was learned the hard way: an
    argument list this could not parse used to return None, which the router reads
    as "not a question", which sends `ask(...)` to the Python interpreter, which
    answers `NameError: name 'ask' is not defined` -- twice, because the model then
    tries again. A question that arrives in an unexpected shape must degrade to a
    plainer question, never to an error.

    So keyword arguments are read where they are given, a positional string is
    accepted as the question (or as the slot, if it happens to name one), and an
    unrecognised slot becomes prose rather than nothing.

    Two rules survive from before, both about not asking when something else is
    going on. It is matched against executable_lines(), for exactly the reason
    is_submission() is: the model narrates its own work constantly, and
    `print("ask(slot='protocol')")` or an echoed summary must not open a panel.
    And a block that also submits is not an ask -- if both appear the caller must
    treat it as a submission. That check lives in the router, but the asymmetry is
    stated here too, because a question is a fine thing to get wrong and an
    ungated submission is not.
    """
    if not code:
        return None
    runnable = executable_lines(code)
    if not runnable:
        return None
    m = _ASK_CALL.search(runnable)
    if not m:
        return None

    inside = m.group(1)
    args = {k: v[1:-1] for k, v in _ASK_ARG.findall(inside)}
    # Whatever is left once the keyword arguments are removed. A model that calls
    # ask("Which steps should this run?") is asking a perfectly good question.
    leftover = _ASK_ARG.sub("", inside)
    positional = [v[1:-1] for v in _ASK_POSITIONAL.findall(leftover)]

    slot = (args.get("slot") or "").strip().lower()
    if not slot:
        slot = next((p.strip().lower() for p in positional
                     if p.strip().lower() in ASK_SLOTS), "")

    question = args.get("question") or next(
        (p for p in positional if p.strip().lower() not in ASK_SLOTS), None)
    if not question and slot and slot not in ASK_SLOTS:
        # An invented slot is not discarded silently -- `slot="genome"` becomes
        # "Which genome?", so the model still gets to ask.
        question = f"Which {slot}?"

    return {
        "slot": slot if slot in ASK_SLOTS else None,
        "pipeline": args.get("pipeline") or None,
        "protocol": args.get("protocol") or None,
        "question": question or None,
    }


# Commands that mean the block is shell, not Python. Biomni decides which
# interpreter to use from a marker on the first line of an <execute> block --
# "#!BASH" for a shell script, nothing for Python -- and a model that forgets the
# marker gets its shell command handed to exec(). For a submission that is a
# silent, expensive failure: the person approves `bash cmd.sh`, and what comes back
# is "Error: name 'bash' is not defined".
#
# Only the first executable line is consulted, which is the convention anyway: a
# block opens with what it is. `print("bash x")` is not in this set, and neither is
# `import` or any name followed by a bracket, so Python stays Python.
_SHELL_WORDS = frozenset("""
    bash sh zsh module genpipes sbatch squeue sacct scancel sinfo scontrol srun
    salloc chunk_genpipes submit_genpipes validate_genpipes
    ls cat head tail wc cp mv mkdir rmdir touch chmod chown ln readlink realpath
    grep egrep zgrep awk sed sort uniq cut tr diff find xargs tee
    echo printf pwd cd export source which env date df du hostname whoami
    tar gzip gunzip zcat unzip rsync scp curl wget make python python3 Rscript
""".split())


def needs_bash_marker(code):
    """Should this block be run as shell even though it is not marked as such?

    True for a submission unconditionally -- that is the one case where guessing
    wrong costs a person their approval -- and otherwise when the first thing the
    block does is run a program.
    """
    if not code:
        return False
    stripped = code.strip()
    if stripped.startswith(("#!BASH", "#!CLI", "#!R", "# Bash script", "# R code",
                            "# R script")):
        return False                      # already declared, leave it alone
    if is_submission(code):
        return True
    lines = executable_lines(code).splitlines()
    if not lines:
        return False
    first = lines[0].strip().split()
    return bool(first) and first[0] in _SHELL_WORDS


def mark_shell(content):
    """Add the #!BASH marker to an <execute> block that needs one.

    Rewrites the message rather than the code so the change lands where biomni
    looks -- its execute node re-extracts the block from the last message's text
    and inspects the first line itself. Returns the content unchanged when there
    is nothing to do, so the caller can tell whether anything happened.
    """
    if not content:
        return content
    m = re.search(r"<execute>(.*?)</execute>", content, re.DOTALL)
    if not m:
        return content
    code = m.group(1)
    if not needs_bash_marker(code):
        return content
    return content[:m.start(1)] + "\n#!BASH\n" + code.strip() + "\n" + content[m.end(1):]


def flag_value(cmd, flag):
    """The argument given to a flag, e.g. flag_value(cmd, '-t') -> 'stringtie'."""
    m = re.search(rf"{re.escape(flag)}\s+(\S+)", cmd)
    return m.group(1) if m else None


def submission_line(code):
    """Pull the real submission command out of a noisy code block, even if
    the model buried it in a Python script or a string literal."""
    if not code:
        return ""
    for p in (r"(?:bash|sh)\s+\S+\.sh\b",
              r"chunk_genpipes\.sh\b[^\"'\n]*",
              r"submit_genpipes\b[^\"'\n]*"):
        m = re.search(p, code)
        if m:
            return m.group(0).strip()
    return code.strip()


def generation_command(messages):
    """Find the genpipes generation command in the history, most recent first.

    Searched backwards, and that direction is load-bearing. The model often
    generates in one block and submits in a later one, so the generation has to
    be found somewhere earlier in the conversation -- but after a REJECTION the
    history holds two of them: the original and the revision. Taking the first
    match showed the stale command in the approval box, so rejecting "steps 1-5",
    asking for 6-12, and being handed a box that still read 1-5 would look like
    nothing had changed. Approving what you were not shown is the one failure
    this gate exists to prevent, so the newest generation wins.
    """
    for m in reversed(list(messages or [])):
        content = getattr(m, "content", "") or ""
        blocks = re.findall(r"<execute>(.*?)</execute>", content, re.DOTALL)
        for block in reversed(blocks):
            if "genpipes" in block and "-g" in block:
                return block
    return ""


def script_name(code):
    """The generated script a submission runs, e.g. 'cmd.sh' from 'bash cmd.sh'.

    Recorded on the run so a later analysis knows which script produced the
    jobs, without having to guess at the conventional name.
    """
    m = re.search(r"\b(?:bash|sh)\s+(\S+\.sh)\b", executable_lines(code) or "")
    return m.group(1) if m else None


def build_proposal(messages, code):
    """The payload shown to the human. The submission line is extracted from
    the caught block; the descriptive slots are parsed from the earlier
    generation command so the box stays populated even when the model splits
    generation and submission across separate blocks. Every fact is parsed
    from command text, never from the model's prose, so the explanation
    cannot disagree with the box the user approves."""
    cmd = submission_line(code or "")
    gen = generation_command(messages)
    src = gen or (code or "")
    protocol = flag_value(src, "-t")
    steps = flag_value(src, "-s")
    inis = re.findall(r"\S+/([^/\s]+\.ini)", src) or re.findall(r"\S+\.ini", src)
    design = flag_value(src, "-d")
    pairs = flag_value(src, "-p")
    readset = flag_value(src, "-r")
    output_dir = flag_value(src, "-o")

    # The pipeline is the bare word right after `genpipes`, e.g. `genpipes
    # rnaseq -t stringtie`. Parsed here rather than left to the model's prose
    # for the same reason as everything else in this function: it ends up in a
    # run record that a later analysis will trust.
    pipeline = None
    m = re.search(r"\bgenpipes\s+([a-z_]+)\b", src)
    if m and m.group(1) != "tools":
        pipeline = m.group(1)

    lines = ["About to submit this GenPipes run:", f"  command: {cmd}"]
    if pipeline:
        lines.append(f"  pipeline: {pipeline}")
    if protocol:
        lines.append(f"  protocol: {protocol}")
    if steps:
        lines.append(f"  steps: {steps}")
    if inis:
        lines.append(f"  config layering: {' , '.join(inis)}")
    if design:
        lines.append(f"  design file: {design}")
    if pairs:
        lines.append(f"  pairs file: {pairs}")
    lines.append("Approve to submit, or request an adjustment (protocol, steps, config).")
    return {
        "command": cmd,
        "explanation": "\n".join(lines),
        "script": script_name(code or ""),
        "slots": {"pipeline": pipeline, "protocol": protocol, "steps": steps,
                  "inis": inis, "design": design, "pairs": pairs,
                  "readset": readset, "output_dir": output_dir},
    }
