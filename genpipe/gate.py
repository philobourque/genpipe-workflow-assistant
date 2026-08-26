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
import ast
import hashlib
import re
import shlex
import textwrap

from . import slots

# The phrase a rejection always renders into the transcript.
#
# Named, and living HERE rather than in agent.py, for two reasons. It is the
# gate's vocabulary -- what the gate says when a decision comes back negative
# -- and this module is stdlib-only, so the offline test harness can import it
# without pulling in biomni.
#
# Two things depend on recognising one phrase: the model, which has to
# understand that a decision was refused, and the scripted model in the test
# harness, which switches onto its retry script when it sees this. Both used to
# depend on a literal typed out in two places, so rewording the sentence broke
# every rejection test silently -- the scripts ran straight past the branch
# they existed to exercise and the suite reported a stale value instead of a
# failure.
REJECTION_MARK = "did not approve"

# Text that means "this really submits". Searched against executable_lines(),
# never the raw block -- see that function for why.
_SUBMIT_PATTERNS = [
    r"\bpropose_submission\s*\(",   # the tool the model is told to call
    r"\b(?:bash|sh)\s+\S+\.sh\b",   # bash <script>.sh -- any generated submission script
    r"\bsubmit_genpipes\b",         # DRAC submit
    r"\bchunk_genpipes\.sh\b",      # DRAC chunk (precedes submit)
]

# propose_submission("cmd.sh") is what the prompt tells the model to write when
# it decides the moment has come, and it exists so that intent is explicit in
# the transcript rather than inferred from a bare shell line.
#
# The other three patterns above are NOT thereby obsolete, and keeping them is a
# safety property rather than tidiness. The model is not the only thing that can
# put text in front of this function: it reads readsets, .ini files, job logs and
# .o files it did not write, and a model that ends up emitting `bash cmd.sh` --
# confused, or steered by something inside one of those files -- must still be
# gated rather than executed. The tool is how submission is normally requested;
# these are the floor under it. A submission recognised by any of them takes the
# same path to the same approval box.
_PROPOSE_CALL = re.compile(
    r"\bpropose_submission\s*\(\s*['\"]?([^'\")\s]+\.sh)['\"]?", re.I)


def proposed_script(code):
    """The script named in a propose_submission() call, or None."""
    m = _PROPOSE_CALL.search(code or "")
    return m.group(1) if m else None


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


# A named call with keyword arguments, e.g. `check_run(name="x")`. Anchored on
# a word boundary so `my_check_run(...)` is not one, and non-greedy to the first
# closing bracket, which is enough for the flat argument lists these calls take.
_CAPABILITY_CALL = re.compile(r"\b([a-z_][a-z0-9_]*)\s*\(\s*(.*?)\s*\)", re.DOTALL)

# Values a capability argument can take. Strings share ask()'s quoting rules --
# including the f"..." prefix a model reaches for out of habit -- plus the two
# bare words that mean something: booleans, for `failed=True`.
_CAP_ARG = re.compile(r"(\w+)\s*=\s*(?:" + _QUOTED + r"|(True|False|true|false))")


def capability_request(code, names=()):
    """A capability call the MODEL wrote, as {"capability", "args"}, or None.

    THE INPUT IS ALWAYS THE MODEL'S OWN CODE BLOCK. Never a user message,
    never the conversation, never prose. Callers hand this
    extract_pending_code(), which reads the <execute> block out of the LAST
    message -- and the last message at this point in the graph is the model's
    reply. That is what makes this a dispatcher rather than an intent
    classifier: there is no path by which a sentence somebody typed reaches
    this function, so there is nothing here that could map "why did it fail" to
    an action even if somebody wanted it to. The model decides; this reads what
    it decided.

    `names` is the closed set of recognised capabilities -- capabilities.NAMES.
    A call to anything else is not a capability and falls through to the
    interpreter exactly as it always has, so ordinary Python the model writes
    is unaffected. Passing an empty set makes this function inert, which is how
    it stays switched off until the table is wired up.

    Matched against executable_lines(), for the same reason ask_request() and
    is_submission() are: the model narrates its own work constantly, and
    `print("check_run(name='x')")` inside an explanation must not run anything.

    Returns None for anything that is not one of these calls. Deliberately
    strict -- a malformed capability call should reach the interpreter and come
    back as a plain Python error the model can read, rather than being guessed
    at here.
    """
    if not code or not names:
        return None
    runnable = executable_lines(code)
    if not runnable:
        return None
    for match in _CAPABILITY_CALL.finditer(runnable):
        name = match.group(1)
        if name not in names:
            continue
        args = {}
        for key, quoted, boolean in _CAP_ARG.findall(match.group(2)):
            if boolean:
                args[key] = boolean.lower() == "true"
            else:
                args[key] = quoted[1:-1]
        return {"capability": name, "args": args}
    return None


# ---------------------------------------------------------------------------
# THE CHANGE A PROPOSAL DECLARES.
#
# THE PROBLEM. A person says "rerun that, but without override_walltime.ini".
# The model reads it, regenerates, and the new command still carries the ini --
# and nothing anywhere noticed. modify.compare() can report a requested row as
# IGNORED, but only when something told it the row was requested, and the only
# thing that ever did was cli.py's /modify panel writing agent._gate_note. A
# change asked for in conversation reached the gate with an empty request set,
# so IGNORED was unreachable, the box carried no mark, and the person approved
# the command they had asked to have changed.
#
# THE BOUNDARY. The model decides what the request means -- that is its work and
# nothing here touches it. What it now also does is SAY what it decided, as
# structured data, in the call that creates the proposal:
#
#     propose_submission("$OUT/cmd.sh", changes=[
#         {"field": "config", "operation": "remove",
#          "value": "override_walltime.ini"}])
#
# and deterministic code checks the command it generated against that
# declaration (modify.realized). The model produces the intended delta; the
# application verifies its realisation. Neither half does the other's job, and
# nothing between them reads the user's sentence.
#
# WHY AN ARGUMENT AND NOT A CALL OF ITS OWN. A separate `changing(...)` line in
# the same block was the first shape of this and it is a second representation
# of one thing: it can be omitted while the proposal is emitted, duplicated,
# left behind from an abandoned attempt, or attached to the wrong
# propose_submission in a block with two. As an argument it is structurally
# bound -- one call, one proposal, one delta, emitted together or not at all --
# and it cannot survive into a later turn describing a command that has since
# been regenerated. _PROPOSE_CALL captures the script path and stops, so adding
# it changed nothing about how a submission is recognised.
#
# FIELD, OPERATION AND VALUE STAY SEPARATE. An earlier draft encoded the verb
# into the string -- "-override_walltime.ini" for a removal -- which makes the
# schema unvalidatable (every string is well-formed), gives a leading dash in a
# filename a second meaning, and leaves the renderer to un-parse a sigil before
# it can say "override_walltime.ini was to come off the stack". Three fields
# are checkable, and a malformed entry can be REFUSED rather than misread.
#
# Parsed, never evaluated, from the MODEL'S OWN CODE BLOCK -- same rule as
# capability_request(). ast.literal_eval accepts literals and nothing else, so
# there is no expression here that could run.
# ---------------------------------------------------------------------------

# What may be declared about each field. A closed table, because an operation
# this cannot check is one the gate would report a verdict on without having
# verified anything.
#
#   set       the flag should now have this value. Every scalar row.
#   add       this ini should be ON the resulting -c stack
#   remove    this ini should be OFF it
#   reorder   the -c stack should be exactly this sequence. `value` is a list.
#
# `name` and `resources` take no operation: one changes no flag, and the other
# changes a FILE whose arrival on -c the mirror already reports on its own line.
# Neither is checkable against a command, and a verdict nothing verified is
# worse than no verdict.
DECLARABLE = {
    "pipeline": ("set",),
    "protocol": ("set",),
    "steps": ("set",),
    "design": ("set",),
    "pairs": ("set",),
    "readset": ("set",),
    "output": ("set",),
    "config": ("add", "remove", "reorder"),
}

# Returned in place of the list when the declaration was present but unusable.
# Distinct from None (nothing was declared) and from [] (nothing was changed),
# because the three call for three different things to be said to a person.
MALFORMED = "malformed"


def _propose_keyword(code, keyword):
    """The value of one keyword argument to propose_submission(), as a Python
    literal, or None if the call does not carry it.

    Two readings, because the block is not reliably parseable Python. A
    submission block usually is -- it is one call -- but the model writes shell
    beside it often enough that ast.parse fails, and a declaration must not be
    lost to a stray `module load` on the line above. So: parse the block if it
    parses, and otherwise cut the argument out textually by matching brackets.
    Both end at literal_eval, which is the only thing that reads the value.
    """
    try:
        tree = ast.parse(textwrap.dedent(code))
    except SyntaxError:
        tree = None
    if tree is not None:
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "id", "") == "propose_submission"):
                for kw in node.keywords:
                    if kw.arg == keyword:
                        try:
                            return ast.literal_eval(kw.value)
                        except (ValueError, SyntaxError):
                            return MALFORMED
        return None

    at = code.find("propose_submission")
    if at < 0:
        return None
    m = re.search(rf"\b{re.escape(keyword)}\s*=\s*", code[at:])
    if not m:
        return None
    start = at + m.end()
    if start >= len(code) or code[start] not in "[({":
        return MALFORMED
    opens = {"[": "]", "(": ")", "{": "}"}
    depth, quote, end = 0, "", None
    for i in range(start, len(code)):
        ch = code[i]
        if quote:
            if ch == quote and code[i - 1] != "\\":
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
        elif ch in opens:
            depth += 1
        elif ch in opens.values():
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        return MALFORMED
    try:
        return ast.literal_eval(code[start:end])
    except (ValueError, SyntaxError):
        return MALFORMED


def declared_changes(code):
    """The delta the model declared on its propose_submission call.

    Three distinct answers, and the distinction is the whole point:

        None        no `changes` argument at all. The action is INCOMPLETE --
                    see agent's derived-run check, which refuses to let a
                    modification through on a proposal that did not say what it
                    was modifying. It is not the same as "nothing changed".
        []          declared, and deliberately empty: a rerun of exactly the
                    same command. There is nothing to verify and that is an
                    answer, not a silence.
        [entry, …]  each entry normalised to {"field", "operation", "value"}.
        MALFORMED   a `changes` argument that could not be read as that.

    Entries that are individually unusable make the whole declaration MALFORMED
    rather than being dropped. Dropping one would leave a shorter list that
    looks complete, and the row it described would be reported as unremarkable
    on the screen somebody is reading to find out whether their change landed.
    """
    if not code:
        return None
    runnable = executable_lines(code)
    if not runnable or "propose_submission" not in runnable:
        return None
    raw = _propose_keyword(runnable, "changes")
    if raw is None:
        return None
    if raw is MALFORMED or not isinstance(raw, (list, tuple)):
        return MALFORMED

    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            return MALFORMED
        field = str(entry.get("field") or "").strip()
        operation = str(entry.get("operation") or "").strip().lower()
        value = entry.get("value")
        if operation not in DECLARABLE.get(field, ()):
            return MALFORMED
        if operation == "reorder":
            if isinstance(value, str):
                value = value.split()
            if not isinstance(value, (list, tuple)) or not value:
                return MALFORMED
            value = [str(v) for v in value]
        else:
            if not isinstance(value, (str, int, float)) or not str(value).strip():
                return MALFORMED
            value = str(value).strip()
        out.append({"field": field, "operation": operation, "value": value})
    return out


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


# The long form of every flag the proposal box reads. GenPipes accepts both, and
# a model writing the readable one is writing a correct command -- but the box
# used to parse only the short form, so `--output-dir /scratch/out` rendered as
# "output: cwd (no -o flag)" in red on a command that really did set it. Being
# wrong in the approval box is worse than being silent: it is the one screen
# whose whole job is to say what is about to happen.
#
# Public because mirror.py needs the same equivalence when it tokenises the
# command for display. Two tables that disagree about whether `--steps` is `-s`
# would put a flag in the mirror that the gate does not think is set.
LONG_FORM = {
    "-t": "--type",
    "-s": "--steps",
    "-r": "--readsets",
    "-d": "--design",
    "-p": "--pairs",
    "-o": "--output-dir",
    "-c": "--config",
    "-j": "--job-scheduler",
    "-g": "--genpipes-file",
}


# ---------------------------------------------------------------------------
# Isolating the genpipes call from the block it arrived in.
#
# Lives here rather than in mirror.py, which is where it was written, because
# gate.build_proposal needs it just as badly and mirror already imports from
# this module -- the other direction would be a cycle. One implementation, so
# the approval box and the run record can never disagree about what the
# command was.
# ---------------------------------------------------------------------------

# `genpipes rnaseq`, the invocation itself. The pipeline is a bare word, so the
# match has to be anchored on the program name to avoid finding the word inside
# a path like /home/x/genpipes/inis.
_START = re.compile(r"\bgenpipes\s+([a-z][a-z0-9_]*)\b")

# Where a joined-up code block stops being the genpipes call. `generated` is a
# whole <execute> block collapsed onto one line, so it routinely carries a
# `module load` before the command and sometimes a `&& echo` after it.
_SEPARATOR = re.compile(r"\s(?:&&|\|\||;|\|)\s")

# A NEWLINE ends the command too, and this is separate from _SEPARATOR because
# it has to be applied before whitespace is flattened -- by the time the block
# is one line, the newline that ended the command is an ordinary space and
# nothing downstream can tell it from the space before a flag.
#
# What that cost, before this existed: a generation written as a short script
#
#     genpipes ampliconseq ... -g $OUT/cmd.sh
#     echo done
#
# put `echo` and `done` in -g's value list, because _groups appends every token
# up to the next flag and there was no next flag. The approval box then read
# `script -g  $OUT/cmd.sh / echo / done` -- three lines of shell spilling out of
# the field somebody reads to decide whether to submit.
#
# Line continuations are resolved first, so any newline still standing here is a
# real statement boundary rather than a wrapped line.
_NEWLINE = re.compile(r"\n+")

# A backslash-newline line continuation, the way a hand-formatted multi-line
# invocation breaks one long command across several lines for readability:
#
#     genpipes dnaseq -t somatic_fastpass \
#       -c $GENPIPES_INIS/dnaseq/dnaseq.base.ini \
#       -r ...
#
# Collapsed to a single space HERE, before shlex ever sees the text. Left in
# place, the backslash survives whitespace-flattening as `\ ` (backslash then
# space), and shlex's POSIX quoting rules read that as an ESCAPED space --
# not a token boundary -- so the token that follows comes out as `' -c'`,
# leading space and all. `_FLAG` matches on the first character, so a token
# starting with a space is never recognised as a flag: it is appended as a
# VALUE onto whatever flag came before, and every flag after the first line
# break drags the rest of the command down with it into one field. This is
# the root cause of a modify screen showing `-c`, `-r`, `-s`, `-j` and `-g`
# all crammed under `protocol`.
_CONTINUATION = re.compile(r"\\[ \t]*\n")

# The same continuation AFTER somebody else already flattened it -- the newline
# gone, the backslash left stranded between two spaces. _CONTINUATION cannot
# match it, because the newline it anchors on no longer exists.
#
# This is not hypothetical tidiness. Every run recorded before build_proposal()
# learned to resolve continuations first was stored in exactly this shape, and
# those records are on disk forever: `/modify` on any of them tokenises the
# stranded backslash as an escaped space and refuses the parse. Healing it here
# rather than in build_proposal() is what makes the repair retroactive -- this
# is the one funnel both the gate and the mirror read through, so an old record
# is repaired when it is READ instead of staying broken because it was WRITTEN
# a fortnight ago.
#
# Surrounded by whitespace is the whole discriminator, and it is a sound one. A
# backslash that means an escaped space -- `/my\ documents/readset.tsv`, which
# is legal and which shlex must go on honouring -- always has a non-space
# character to its left. One with whitespace on BOTH sides escapes nothing: no
# shell writes it deliberately, and the only thing that produces it is a
# continuation whose newline has been flattened away.
_STRANDED = re.compile(r"(?:^|(?<=\s))\\(?=\s|$)")

def tidy(generated):
    r"""A generation block, cleaned up but with its STATEMENT BOUNDARIES intact.

    What gets stored on the proposal as `generated`, and the one thing that has
    to survive the trip is the newline between one statement and the next.

    This used to be `" ".join(text.split())`, which flattens the block onto a
    single line -- and a generation is routinely a small script:

        genpipes ampliconseq ... -g ampliconseq_cit_cmd.sh
        echo "---exit $?---"
        ls

    Flattened, the newline that ended the genpipes call becomes an ordinary
    space, and nothing downstream can tell it from the space before a flag.
    invocation() below cuts on newlines for exactly this reason and had none
    left to cut on, so mirror._groups appended every token up to the next flag
    -- there being no next flag -- onto `-g`. /modify then drew

        script       -g  ampliconseq_cit_cmd.sh
                         echo
                         ---exit $?---
                         ls

    three lines of shell spilling out of a field on the screen somebody reads
    to decide what to change. The write side was destroying the boundary; the
    read side had been taught to respect one it was never given.

    Continuations are still resolved first, and each line is still
    whitespace-normalised -- those were always right. Only the join changed.
    """
    text = _CONTINUATION.sub(" ", generated or "")
    lines = (" ".join(line.split()) for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def invocation(generated):
    """The bare genpipes call, cut out of whatever block it arrived in.

    Returns '' when there is no genpipes call in the text, which is the honest
    answer for a proposal whose generation step was never captured -- the caller
    draws no mirror rather than an empty frame.
    """
    # Line continuations resolved before whitespace is flattened -- see
    # _CONTINUATION's comment for why the order matters -- and then again in
    # their already-flattened form, for commands that reached us pre-flattened.
    text = _STRANDED.sub(" ", _CONTINUATION.sub(" ", generated or ""))

    # Then cut to the LINE holding the call, while newlines still exist to cut
    # on. A generation is routinely a small script -- an OUT=... assignment, a
    # mkdir, the genpipes call, an echo -- and only one line of it is the
    # command. See _NEWLINE.
    for chunk in _NEWLINE.split(text):
        if _START.search(chunk):
            text = chunk
            break
    else:
        return ""

    text = " ".join(text.split())
    m = _START.search(text)
    if not m:
        return ""
    return _SEPARATOR.split(text[m.start():])[0].strip()


def flag_value(cmd, flag):
    r"""The argument given to a flag, e.g. flag_value(cmd, '-t') -> 'stringtie'.

    Accepts the long form too, and `--flag=value` as well as `--flag value`.
    The long form is tried FIRST: `-o` is a prefix of nothing, but a short-form
    search would happily match the `-o` inside `--output-dir` and return
    `/scratch/out`'s neighbour instead of the value.

    A flag's value is on the flag's own LINE. `\s` matches newlines, so the
    separator here is spaces and tabs only -- with `\s+` a flag sitting at the
    end of a line took the first word of the next line as its value, and since
    the generation command is a whole multi-line shell script, the approval box
    filled with fragments: `log level -l  $OUT`, an `output` row reading `-g`,
    `echo`, `exit=$?`, `ls`. Every one of those is a line that merely FOLLOWED a
    flag. The box is what somebody reads before approving, so a value invented
    by a regex crossing a newline is exactly the wrong thing to put in it.
    """
    long = LONG_FORM.get(flag)
    for name in ([long] if long else []) + [flag]:
        m = re.search(rf"(?<![\w-]){re.escape(name)}(?:[ \t]+|=)([^\s]+)", cmd)
        if m:
            return m.group(1)
    return None


# A token that opens a flag rather than being one flag's value. `-1` and a bare
# `-` are values; `-t` and `--config` are flags. The same rule mirror.py applies
# for the same reason, written twice because the dependency only runs one way
# (mirror imports this module).
_FLAG_TOKEN = re.compile(r"^--?[A-Za-z]")

# Shell redirection trailing the command -- `2>&1`, `>`, `>>`. It starts with a
# digit rather than a dash, so _FLAG_TOKEN does not catch it and without this it
# would be read as one more value on whatever flag preceded it.
_REDIRECT_TOKEN = re.compile(r"^\d*>>?&?\d*$|^\d*<$")

# Where the genpipes call stops and the surrounding shell begins. The values of
# a repeatable flag run to the end of the command, so without this `-c a.ini &&
# bash cmd.sh` reads `&&`, `bash` and `cmd.sh` as three more inis. Mostly masked
# in build_proposal, which cuts with _SEPARATOR before calling here -- but only
# when there IS a genpipes call to cut to, and the documented fallback for a
# proposal whose generation was never captured passes the whole block.
_SEPARATOR_TOKEN = frozenset(("&&", "||", ";", "|", "&"))


def flag_values(cmd, flag):
    r"""EVERY argument given to a repeatable flag, in order, as a list.

    `-c` is the only flag GenPipes takes plural, and it is the one flag whose
    ORDER is its semantics -- inis are applied left to right and later ones
    overrule earlier ones. So this returns a list and preserves both the order
    and the exact strings.

    WHY NOT A REGEX OVER THE WHOLE COMMAND, which is what this replaces. The
    old line was

        re.findall(r"\S+/([^/\s]+\.ini)", src) or re.findall(r"\S+\.ini", src)

    and it had two defects that compounded into a silent, expensive one. The
    first pattern captures only the BASENAME of any path-qualified ini, so
    `$GENPIPES_INIS/dnaseq/cit.ini` was recorded as `cit.ini` and the proposal
    no longer described the file that would be read. The second, worse: the
    `or` means the fallback runs only when the first pattern found NOTHING, so
    on the ordinary command -- four inis under $GENPIPES_INIS and one written
    plainly beside the run -- the bare one matched neither branch and was
    dropped from the proposal entirely.

    What that looked like: `-c ... cit.ini override_walltime.ini` reached the
    gate as a four-ini stack. mirror.read() tokenises the command properly and
    showed all five, so the screen was right and the record was not -- and
    modify.compare(), which diffs SLOTS, could not see that ini removed or
    retained. A request to drop it was neither applied nor reported as ignored,
    because the row it belonged to was invisible to the only check there is.

    Values stop at the next flag, at a shell separator, or at the end of the
    line, which is the same boundary flag_value() draws and for the same reason:
    `generated` is a whole shell block, and a flag at the end of one line must
    not swallow the first word of the next.
    """
    long = LONG_FORM.get(flag)
    names = ([long] if long else []) + [flag]
    out = []
    for line in (cmd or "").splitlines():
        try:
            tokens = shlex.split(line, comments=False, posix=True)
        except ValueError:
            # An unbalanced quote. A rough split beats refusing to read the
            # command at all -- the caller is building an approval box, and an
            # empty -c row on a command that has one is the failure above by
            # another route.
            tokens = line.split()
        at = 0
        while at < len(tokens):
            token = tokens[at]
            name, _, inline = token.partition("=")
            if name not in names:
                at += 1
                continue
            if inline:
                # `--config=a.ini`. argparse accepts it for the long form, and
                # it carries exactly one value.
                out.append(inline)
                at += 1
                continue
            at += 1
            while at < len(tokens):
                value = tokens[at]
                if (_FLAG_TOKEN.match(value) or _REDIRECT_TOKEN.match(value)
                        or value in _SEPARATOR_TOKEN):
                    break
                out.append(value)
                at += 1
    # Order preserved, duplicates dropped. `-c a.ini -c a.ini` is one layer
    # written twice, and dict.fromkeys keeps the FIRST position -- which is
    # where the layer actually takes effect.
    return list(dict.fromkeys(out))


def rewrite(text, edits):
    """The same genpipes call with exactly these flags changed. The WRITER.

    Everything above this line reads a command: invocation() cuts it out of a
    block, flag_value() and flag_values() ask what it says. This is the one
    thing in the file that produces one, and it exists because /relaunch knows
    every field of the revision it wants before anything runs -- the config
    stack, the step range, the script path -- and asking a model to retype a
    command the program can already write is thirty-six inference steps spent
    on syntax. See relaunch.command().

    `edits` maps a flag to what it should say:

        {"-s": "1-23"}          set it, replacing whatever was there
        {"-c": [a, b, c]}       a repeatable flag's whole value list, in order
        {"-s": None}            remove the flag and its values

    A flag already on the command keeps ITS POSITION; one that was not there is
    added before any trailing redirection. Every other token survives exactly as
    written -- quoting included, which is why the tokeniser runs in non-POSIX
    mode. `-o "$OUTDIR"` has to come out the other side still quoted and still
    unexpanded: the variable is assigned in the block around this call, and a
    rewrite that resolved or dropped the quotes would change where the run
    writes.

    NOTHING HERE UNDERSTANDS GENPIPES. It does not know that -c is a config
    stack or that -s is a step range; the caller decides what the command should
    say and this puts it there without disturbing the rest. What it does know is
    the same three token shapes flag_values() knows -- a flag, a redirection, a
    shell separator -- because those are what decide where one flag's values
    stop.

    Returns "" when there is no genpipes call in the text, which is the same
    "no opinion" invocation() returns and must be treated the same way: the
    caller refuses rather than writing a command from scratch.
    """
    call = invocation(str(text or "")) or ""
    if not call:
        return ""
    try:
        tokens = shlex.split(call, comments=False, posix=False)
    except ValueError:
        tokens = call.split()

    # Every spelling of every flag being edited, so `--steps 1-5` is recognised
    # as the same flag as `-s 1-5` and is replaced rather than left beside it.
    wanted = {}
    for flag, value in (edits or {}).items():
        for spelling in ([LONG_FORM[flag]] if flag in LONG_FORM else []) + [flag]:
            wanted[spelling] = flag

    out, seen, at = [], {}, 0
    while at < len(tokens):
        token = tokens[at]
        name, _, _inline = token.partition("=")
        flag = wanted.get(name)
        if flag is None:
            out.append(token)
            at += 1
            continue
        # The flag and everything belonging to it come out. A placeholder marks
        # where it was, so the replacement lands in the same place: a stack
        # rewritten at the end of the line would read differently from the one
        # somebody approved, and for -c a different position is a different run.
        if flag not in seen:
            seen[flag] = len(out)
            out.append(_HOLE)
        at += 1
        if _inline:
            continue
        while at < len(tokens):
            value = tokens[at]
            if (_FLAG_TOKEN.match(value) or _REDIRECT_TOKEN.match(value)
                    or value in _SEPARATOR_TOKEN):
                break
            at += 1

    for flag, value in (edits or {}).items():
        if value is None:
            continue
        words = [flag] + ([str(v) for v in value]
                          if isinstance(value, (list, tuple))
                          else [str(value)])
        if flag in seen:
            out[seen[flag]] = words
        else:
            # New to this command. Before the trailing redirection, which is
            # not part of what genpipes is being told to do and must stay last.
            stop = len(out)
            while stop and isinstance(out[stop - 1], str) and (
                    _REDIRECT_TOKEN.match(out[stop - 1])
                    or out[stop - 1] in _SEPARATOR_TOKEN):
                stop -= 1
            out.insert(stop, words)

    flat = []
    for item in out:
        if item is _HOLE:
            continue                      # a removed flag, and nothing to add
        flat.extend(item if isinstance(item, list) else [item])
    return " ".join(flat)


# Stands in for a flag that was cut out, so its replacement can be put back
# exactly where it was. A list is never a token, so nothing can collide with it.
_HOLE = object()


def submission_line(code):
    """Pull the real submission command out of a noisy code block, even if
    the model buried it in a Python script or a string literal.

    A propose_submission() call resolves to the command it stands for, because
    everything downstream -- the approval box, the run record, the thing that
    actually runs on /approve -- is about `bash cmd.sh`. The tool is how the
    model asks; it is not what gets executed, and the person approving should be
    reading the command rather than the request for it.
    """
    if not code:
        return ""
    script = proposed_script(code)
    if script:
        return f"bash {script}"
    for p in (r"(?:bash|sh)\s+\S+\.sh\b",
              r"chunk_genpipes\.sh\b[^\"'\n]*",
              r"submit_genpipes\b[^\"'\n]*"):
        m = re.search(p, code)
        if m:
            return m.group(0).strip()
    return code.strip()


def is_generation(code):
    """Is this block a GenPipes generation -- `genpipes ... -g <script>`?

    The counterpart to is_submission, and deliberately far less consequential:
    generating writes a script and spends nothing, which is why it runs
    ungated. It is worth recognising anyway, because a turn that generated and
    then ended without reaching the gate did real work and produced no run, and
    that is the one silent outcome worth saying out loud. See cli._talk.

    One definition, used by generation_command below and by the agent's
    per-turn flag, so "what counts as a generation" cannot be answered two
    ways.
    """
    text = code or ""
    return "genpipes" in text and "-g" in text


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
            if is_generation(block):
                return block
    return ""


def script_name(code):
    """The generated script a submission runs, e.g. 'cmd.sh' from 'bash cmd.sh'.

    Recorded on the run so a later analysis knows which script produced the
    jobs, without having to guess at the conventional name.
    """
    script = proposed_script(code)
    if script:
        return script
    m = re.search(r"\b(?:bash|sh)\s+(\S+\.sh)\b", executable_lines(code) or "")
    return m.group(1) if m else None


def build_proposal(messages, code, generated=None):
    """The payload shown to the human. The submission line is extracted from
    the caught block; the descriptive slots are parsed from the earlier
    generation command so the box stays populated even when the model splits
    generation and submission across separate blocks. Every fact is parsed
    from command text, never from the model's prose, so the explanation
    cannot disagree with the box the user approves.

    `generated` names the generation command outright, for a proposal the
    program WROTE rather than found in a transcript -- see relaunch.command().
    It changes nothing else: the same parse runs over it, producing the same
    slots, the same `missing` and the same revision hash, so a deterministic
    proposal and a model's are the same kind of object and the gate cannot
    tell them apart. Which is the point. There is no second, weaker proposal
    shape for the path with no model in it."""
    cmd = submission_line(code or "")
    gen = generated if generated is not None else generation_command(messages)
    block = gen or (code or "")

    # Flags are read from the genpipes CALL, not from the block it arrived in.
    # A generation is routinely a small script, and the surrounding shell has
    # flags of its own that mean nothing to GenPipes:
    #
    #     mkdir -p $HOME/ampliconseq_cit_run/output
    #     module load ... && genpipes ampliconseq -r ... -d ... -o ...
    #
    # Searching the whole block found `mkdir`'s `-p` and recorded the OUTPUT
    # DIRECTORY as the pairs file. The approval box then showed a `-d` and a
    # `-p` together -- which genpipes.md forbids outright -- so a correct
    # command looked like a broken one, and the bogus path was written into the
    # run record for /modify to read back later.
    #
    # Falls back to the whole block when there is no genpipes call in it, which
    # is the pre-existing behaviour for a proposal whose generation was never
    # captured: better a rough parse than an empty box.
    src = invocation(block) or block
    protocol = flag_value(src, "-t")
    steps = flag_value(src, "-s")
    # Read off the `-c` flag itself, exactly as written and in order. See
    # flag_values() for what the regex that used to be here got wrong, and why
    # a stack that is nearly right is worse than no stack at all.
    inis = flag_values(src, "-c")
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

    # What genpipes cannot run without, whether or not the model happened to
    # write it. slots.gaps() is the same table the offline fake-LLM's question
    # sequence and prep.Preparation both use -- one required-slot table, not a
    # second one invented here. Empty means the proposal is complete; it does
    # NOT mean the pipeline/protocol were understood, since an unknown pipeline
    # makes slots.gaps() ask about that instead (harmless here: build_proposal
    # is about what's missing from a command already believed to be genpipes).
    missing = [g.slot for g in slots.gaps(
        pipeline=pipeline, protocol=protocol,
        readset=readset, design=design, pairs=pairs)]

    lines = ["About to submit this GenPipes run:", f"  command: {cmd}"]
    if pipeline:
        lines.append(f"  pipeline: {pipeline}")
    if protocol:
        lines.append(f"  protocol: {protocol}")
    elif pipeline and slots.DEFAULTS.get(pipeline):
        # An omitted `-t` is not a missing slot -- GenPipes has its own default
        # and the command runs -- but it is the one assumption in the box that
        # nothing else on screen states. Before DEFAULTS covered every pipeline
        # this line read `MISSING (required): protocol`, which was untrue and
        # loud; the risk in making it true was making it invisible, and an
        # invisible assumption is the worse of the two. dnaseq is why: the
        # default is germline, so a tumour/normal cohort approved here runs to
        # completion and answers a different question, with nothing in the
        # approval box that a reader could have caught it by.
        #
        # Said as an assumption rather than recorded as a slot. The slots keep
        # describing the command as WRITTEN -- filling in `protocol` here would
        # put a `-t` in the record that is not in the command, and every later
        # reader of that record would have no way to tell the two apart.
        lines.append(f"  protocol: {slots.DEFAULTS[pipeline]} "
                     "— assumed, no -t given")
    if steps:
        lines.append(f"  steps: {steps}")
    if inis:
        lines.append(f"  config layering: {' , '.join(inis)}")
    if design:
        lines.append(f"  design file: {design}")
    if pairs:
        lines.append(f"  pairs file: {pairs}")
    if missing:
        lines.append(f"  MISSING (required): {', '.join(missing)}")
    lines.append("Approve to submit, or request an adjustment (protocol, steps, config).")
    proposal = {
        "command": cmd,
        "missing": missing,
        # The generation command itself, kept so the gate can show what the
        # script being approved was BUILT from. `bash cmd.sh` is what runs, and
        # on its own it says nothing: two runs a week apart submit the same
        # three words. It matters more now that the transcript folds the agent's
        # working away by default -- without this, the generation would scroll
        # past unseen and the approval box would be the first and only place the
        # command appeared, reading `bash cmd.sh`.
        # Continuations resolved BEFORE the whitespace is flattened, for the
        # reason spelled out above _CONTINUATION: a `\` survives flattening as
        # `\ `, and shlex reads that as an escaped space rather than a token
        # boundary, so every flag after the first continuation collapses into
        # the previous flag's value.
        #
        # Flattening first destroyed the newline _CONTINUATION needs to match,
        # so mirror.read() -- which tokenises this string -- silently failed on
        # every hand-formatted multi-line command. What that looked like: a
        # HOLD box listing the run's name and nothing else, with /approve still
        # offered, for a command that had a readset, a design file and three
        # inis in it. The slots were parsed correctly the whole time; only the
        # string kept for the mirror was mangled.
        "generated": tidy(gen) or None,
        # The exact string every flag above was read out of -- the genpipes call
        # alone, with the surrounding shell stripped off. Kept because it is
        # what with_usage() has to ask "was -c given" of, and reconstructing it
        # there from `generated` would get a different answer in the one case
        # that matters: a proposal whose generation was never captured falls
        # back to the submission block here, and `generated` is then None.
        "invocation": src or None,
        "explanation": "\n".join(lines),
        "script": script_name(code or ""),
        "slots": {"pipeline": pipeline, "protocol": protocol, "steps": steps,
                  "inis": inis, "design": design, "pairs": pairs,
                  "readset": readset, "output_dir": output_dir},
    }
    # WHAT THE MODEL SAYS IT CHANGED, kept beside what it actually wrote.
    #
    # Read off the submission block rather than the generation, because that is
    # the block the model writes when it is handing something over to be
    # approved -- a generation may be one of several attempts, and a claim
    # attached to an abandoned attempt would be checked against a command it
    # was never about.
    #
    # A CLAIM, NOT A FACT, and stored under a name that says so. Nothing
    # downstream may treat this as describing the command: modify.realized()
    # exists precisely to find out whether it does, and the gate reports the
    # answer rather than the claim. Absent for every turn that declares
    # nothing, which is most of them.
    #
    # Deliberately NOT part of revision(). What executes is the generation, the
    # command and the script; a declaration about them changes none of the
    # three, and letting it move the revision would invalidate an outstanding
    # approval over a comment.
    declared = declared_changes(code or "")
    if declared is not None:
        proposal["declared"] = declared
    # Stamped here, where the proposal is finished, and never recomputed
    # downstream. with_usage() adds flag metadata afterwards and deliberately
    # does not touch this: what argparse accepts is not part of what executes,
    # and a revision that moved because --help became readable would invalidate
    # a gate for no reason anybody could see.
    proposal["revision"] = revision(proposal)
    return proposal


def revision(proposal):
    """A short, stable identity for ONE EXACT EXECUTABLE PROPOSAL.

    What an approval authorises. The box on screen and the command that runs
    have to be the same thing, and until now nothing said so -- /approve
    checked that a decision was open, never that the open decision was about
    the command it was going to run.

    WHY NOT THE CHECKPOINT ID. LangGraph stamps every superstep with a
    monotonic id, and the interrupt lives at one. It fails all three tests an
    identity has to pass here: it changes on every superstep rather than when
    the proposal changes, so re-parking the same command would look like a
    different decision; it is not comparable to anything on the registry
    record, so the approval could not be checked against the box; and it is an
    internal detail of a pinned library that would not survive a checkpoint
    migration.

    WHAT GOES IN. The three strings that decide what executes, and nothing
    else:

        generated   the shell block re-run to rebuild the script
        command     the line that launches it
        script      the path both of them are about

    NOT `slots`. Slots are a PARSE of `generated` -- they cannot disagree with
    it, so including them adds no information and one more way for a re-parse
    to shift an identity that should have been stable.

    WHAT IS NOT CANONICALISED AWAY, and why that is the right direction. The
    text is used as `tidy()` left it: whitespace normalised per line, blank
    lines dropped, continuations resolved. Nothing further is stripped -- not
    comments, not echoes, not a `$(date …)` inside an output path. The two
    failure modes are not symmetrical. A revision that differs when it need
    not costs a redundant re-gate, which nobody notices. A revision that
    MATCHES when the commands differ approves one command having shown
    another, which is the single failure this function exists to prevent. So
    when in doubt, differ.

    Twelve hex characters. Long enough that a collision is not a thing that
    happens; short enough to sit on a record and in a log line and be compared
    by eye when something goes wrong.
    """
    if not proposal:
        return None
    parts = [str(proposal.get(key) or "")
             for key in ("generated", "command", "script")]
    if not any(parts):
        return None
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:12]


def with_usage(proposal, flags):
    """The proposal, plus what `genpipes <pipeline> --help` says about its flags.

    Three keys, all plain lists so the proposal stays JSON and survives the
    checkpoint and the run record:

        accepts   every flag this pipeline takes, canonical spelling
        required  the subset argparse will not run without
        lacking   the required flags this command did not write

    Separate from `missing`, which stays exactly what it was: slots.gaps()'s
    verdict, in SLOT names, about the files a run needs. These are FLAGS, from a
    different authority, and merging them would put `-c` in a list whose other
    entries are `readset` and `design` and leave every reader guessing which
    vocabulary it was looking at.

    `lacking` is the one that earns its keep. slots.gaps() has no entry for the
    config stack -- there is no table of which inis a run needs, because that
    depends on the machine -- so a generation with no `-c` at all passed every
    check this project had and was drawn as READY TO SUBMIT. It then failed at
    generation, after somebody had approved it, with an argparse error. --help
    knew the whole time.

    A falsy `flags` (no GenPipes on this machine, or an unreadable --help)
    leaves the proposal alone rather than writing empty lists into it. That
    distinction matters downstream: an absent `accepts` means "no opinion, show
    what the tables say", while an empty one would mean "this pipeline takes no
    flags" and would empty the gate.
    """
    if not proposal or not flags:
        return proposal
    proposal["accepts"] = list(flags.flags)
    proposal["required"] = list(flags.required)
    proposal["lacking"] = list(flags.lacking(proposal.get("invocation") or ""))
    return proposal
