"""The submission gate's decision logic, as pure functions.

This is the safety-critical part of the whole tool: the code that decides
whether something the model wrote is about to submit work to a scheduler. It
lives in its own module, importing nothing but the standard library, for two
reasons.

  1. It can be tested anywhere. genpipe_agent.py imports biomni, which drags in
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
