import getpass
import os
import re
import readline  # noqa: F401 -- side-effect only: gives input() arrow-key history/editing
import sys
import termios
import textwrap
import tty
import io, contextlib
from pathlib import Path

import display
import intake
import preflight
import runs
import slots
import ui
from biomni.llm import get_llm
from genpipe_agent import GenpipeA1

HERE = Path(__file__).resolve().parent
GRAMMAR_PATH = HERE / "genpipes.md"
ENV_PATH = HERE / ".env"
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_SOURCE = "Anthropic"

# Recognized key prefix -> (source, env var Biomni's get_llm() reads for it, a
# current-as-of-2026-07-24 default model verified against each provider's own
# docs). Order matters: sk-ant- must be checked before the bare sk- fallback,
# since an Anthropic key would otherwise match the OpenAI rule.
#
# Providers not listed here (Azure, Bedrock, Ollama, Custom) either need more
# than a bare key -- an endpoint, a base_url -- or use ambient cloud
# credentials instead of a pasted key at all; out of scope for a one-line
# paste-a-key prompt, and left to manual .env setup (see .env.example).
KNOWN_PROVIDERS = [
    ("sk-ant-", "Anthropic", "ANTHROPIC_API_KEY", DEFAULT_MODEL),
    ("AIza", "Gemini", "GEMINI_API_KEY", "gemini-3.5-flash"),
    ("gsk_", "Groq", "GROQ_API_KEY", "openai/gpt-oss-120b"),
    ("sk-", "OpenAI", "OPENAI_API_KEY", "gpt-5.6-sol"),
]

# Working directory for the agent (checkpoint db, biomni_data/, runs.jsonl, ...).
# Override with GENPIPE_AGENT_WORKDIR if ~/scratch isn't the right place on your
# cluster; defaults to ~/scratch since that's writable and persistent on DRAC.
DEFAULT_WORKDIR = os.environ.get("GENPIPE_AGENT_WORKDIR", os.path.expanduser("~/scratch"))


def _write_env_var(name, value):
    """Set NAME=value in .env, preserving any other lines already there.

    Written as `export NAME=value` (not plain NAME=value) because
    start_agent.sh loads .env with `source`, in the same shell that then
    execs python -- export is what makes the var visible to that child
    process. chmod 0600 since this file now holds a live secret.

    Persistence here is best-effort: a full disk quota (seen in practice on
    this cluster's home filesystem) or a permissions issue shouldn't crash
    the app over a file write -- os.environ is set by the caller regardless,
    so the current session keeps working either way. It just means you'll be
    asked again next launch instead of it being remembered.
    """
    line = f"export {name}={value}\n"
    pattern = re.compile(rf"^\s*(export\s+)?{re.escape(name)}=")
    try:
        if ENV_PATH.exists():
            lines = ENV_PATH.read_text().splitlines(keepends=True)
            for i, existing in enumerate(lines):
                if pattern.match(existing):
                    lines[i] = line
                    break
            else:
                if lines and not lines[-1].endswith("\n"):
                    lines[-1] += "\n"
                lines.append(line)
            ENV_PATH.write_text("".join(lines))
        else:
            ENV_PATH.write_text(line)
        ENV_PATH.chmod(0o600)
        return True
    except OSError as e:
        print(f"  {display.RED}Couldn't save to .env ({e.strerror or e}) -- "
              f"using it for this session only.{display.RESET}")
        return False


def _looks_like_placeholder(key):
    """.env.example-style placeholders all contain a literal '...' -- a real
    key never does, so this is a cheap, provider-agnostic sanity check."""
    return not key or "..." in key


def _detect_provider(key):
    """Guess (source, env_var, model) from a key's prefix, or None if the
    prefix isn't one of the ones we recognize."""
    for prefix, source, env_var, model in KNOWN_PROVIDERS:
        if key.startswith(prefix):
            return source, env_var, model
    return None


def _choose_provider():
    """Ask which provider an unrecognized key belongs to."""
    print(f"  {display.GREY}Couldn't tell which provider that key is for "
          f"-- pick one:{display.RESET}")
    for i, (_, source, _, _) in enumerate(KNOWN_PROVIDERS, 1):
        print(f"    {i}) {source}")
    while True:
        try:
            choice = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n\n  {display.RED}Cancelled -- no key saved.{display.RESET}\n")
            raise SystemExit(1)
        if choice.isdigit() and 1 <= int(choice) <= len(KNOWN_PROVIDERS):
            _, source, env_var, model = KNOWN_PROVIDERS[int(choice) - 1]
            return source, env_var, model
        print(f"  {display.RED}Not a valid choice -- try again.{display.RESET}")


def _read_masked_key(prompt="  API key: ", reveal=4):
    """Read a line of hidden input, echoing the first `reveal` characters
    plain and `*` after that -- unlike getpass's fully-blank input, this
    gives visible proof that typing/pasting actually registered, without
    putting the whole secret on screen.

    Falls back to getpass (nothing echoed at all) when stdin isn't a real
    terminal -- there's no cursor to control on a pipe, and getpass already
    degrades gracefully there itself.
    """
    if not sys.stdin.isatty():
        return getpass.getpass(prompt)

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    chars = []
    try:
        # Raw mode has to be enabled *before* the prompt is written, not
        # after -- otherwise there's a window where input arriving right
        # after the prompt appears still hits normal cooked-mode echo (the
        # terminal shows it in full, unmasked) before this function gets a
        # chance to take over. Enabling it first closes that window
        # completely, since nothing can be typed in response to a prompt
        # that hasn't been printed yet.
        tty.setraw(fd)
        sys.stdout.write(prompt)
        sys.stdout.flush()
        while True:
            ch = sys.stdin.read(1)
            if ch in ("\r", "\n"):
                break
            if ch == "\x03":  # Ctrl+C
                raise KeyboardInterrupt
            if ch in ("", "\x04"):  # EOF / Ctrl+D
                raise EOFError
            if ch in ("\x7f", "\x08"):  # backspace/delete
                if chars:
                    chars.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            chars.append(ch)
            sys.stdout.write(ch if len(chars) <= reveal else "*")
            sys.stdout.flush()
    finally:
        # write() happens before this restores the terminal, so the raw
        # \r\n has to be sent here, inside raw mode, not after -- termios
        # restores canonical mode, but doesn't touch what's already sent.
        sys.stdout.write("\r\n")
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    return "".join(chars)


def _prompt_for_api_key():
    """Ask for a real key, save it (and the provider it's for) to .env, and
    set both up for this process.

    Runs once: the key is persisted, so start_agent.sh's `source .env` picks
    it up silently on every later launch and this prompt never fires again.
    """
    print(f"\n  {display.BOLD}{display.GREEN}No API key configured.{display.RESET}")
    # Wrapped rather than left to the terminal: a soft-wrapped second line
    # starts at column 0 and breaks the two-space indent everything else here
    # keeps.
    for line in textwrap.wrap("Paste a key for Anthropic, OpenAI, Gemini, or Groq "
                              "-- the first few characters stay visible, so you can "
                              "tell it registered.", ui.width() - 2):
        print(f"  {display.GREY}{line}{display.RESET}")
    print()
    while True:
        try:
            key = _read_masked_key().strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n\n  {display.RED}Cancelled -- no key saved.{display.RESET}\n")
            raise SystemExit(1)
        if not _looks_like_placeholder(key):
            break
        print(f"  {display.RED}That doesn't look like a real key -- try again.{display.RESET}\n")

    detected = _detect_provider(key)
    if detected:
        source, env_var, model = detected
        print(f"  {display.GREY}Detected: {source}{display.RESET}")
    else:
        source, env_var, model = _choose_provider()

    saved = _write_env_var(env_var, key)
    saved &= _write_env_var("GENPIPE_LLM_SOURCE", source)
    saved &= _write_env_var("GENPIPE_LLM_MODEL", model)
    os.environ[env_var] = key
    os.environ["GENPIPE_LLM_SOURCE"] = source
    os.environ["GENPIPE_LLM_MODEL"] = model
    tail = key[-4:] if len(key) > 4 else key
    if saved:
        print(f"\n  {display.GREEN}Saved{display.RESET} to .env  "
              f"{display.GREY}({source} · …{tail}){display.RESET}\n")
    else:
        print(f"\n  {display.AMBER}Using{display.RESET} {source} · …{tail} "
              f"{display.GREY}for this session (not saved -- see above){display.RESET}\n")


def _require_api_key():
    """Ensure some provider's API key is set, prompting for and saving one
    (plus which provider/model it's for) if not.

    Without this, a missing key doesn't surface until the first real LLM call
    -- deep inside Biomni's tool-retriever call in run() -- as a ~25-frame
    stack trace ending in a generic SDK error, with nothing in it written for
    a user of this tool.

    Deliberately not inside build_agent(): that function is shared with the
    mock pipeline test, which runs with no real key at all (FakeLLM replaces
    agent.llm before any real call happens), so a prompt/check there would
    break the test in exactly the environment it's meant to run in. This is
    called only from the two real entry points instead: the CLI below, and
    server.py.
    """
    for _, _, env_var, _ in KNOWN_PROVIDERS:
        if not _looks_like_placeholder(os.environ.get(env_var, "")):
            return
    _prompt_for_api_key()


def build_agent(path=None, llm=None, source=None):
    """Construct a fully configured GenpipeA1 agent: the gated graph, with the
    GenPipes grammar registered as software.

    Shared by launch_agent.py (real use, real LLM) and the mock pipeline test
    (fake LLM swapped in after construction) so both exercise the exact same
    construction path -- no separate, drifting "test version" of the setup.

    llm/source default to whatever _prompt_for_api_key() last saved
    (GENPIPE_LLM_MODEL/GENPIPE_LLM_SOURCE in .env), falling back to Anthropic
    if those aren't set -- e.g. a .env carried over from before multi-provider
    support, with only ANTHROPIC_API_KEY in it.
    """
    if path is None:
        path = DEFAULT_WORKDIR
    if llm is None:
        llm = os.environ.get("GENPIPE_LLM_MODEL", DEFAULT_MODEL)
    if source is None:
        source = os.environ.get("GENPIPE_LLM_SOURCE", DEFAULT_SOURCE)

    # 1. Build the agent on the venv interpreter, data lake skipped.
    #    Biomni prints a configuration block at construction (path, timeout, model,
    #    temperature). None of it is actionable, and it clashes with the banner, so
    #    swallow stdout for this call. To confirm the model later: agent.llm
    with contextlib.redirect_stdout(io.StringIO()):
        agent = GenpipeA1(path=path,
                          llm=llm,
                          source=source,
                          expected_data_lake_files=[])

    # 2. Read the GenPipes grammar document from disk, next to this script --
    #    not a path tied to any one person's home directory.
    content = GRAMMAR_PATH.read_text()

    # 3. Register the grammar as software; this also reconfigures the agent, which is
    #    what rebuilds the gated graph. Biomni's add_software prints the full
    #    description it registers (a1.py:799) -- here, the entire grammar -- so
    #    swallow stdout for this one call only.
    with contextlib.redirect_stdout(io.StringIO()):
        agent.add_software({"genpipes": content})

    return agent


# --------------------------------------------------------------------- #
#  The command loop -- the app's actual interface. Bare text is a task;  #
#  anything starting with / is a command. No Python syntax required.     #
# --------------------------------------------------------------------- #

def _apply_llm(agent, source, model):
    """Swap the model the agent uses, effective immediately -- no restart,
    no rebuilding the graph. Safe because the compiled graph's generate node
    reads agent.llm live on every step rather than capturing it by value
    (verified against Biomni's a1.py), so reassigning this attribute is
    enough on its own.
    """
    agent.llm = get_llm(
        model,
        stop_sequences=["</execute>", "</solution>"],
        source=source,
    )
    print(f"  {display.GREEN}Using{display.RESET} {source} · {model}\n")


def _cmd_model(agent, args):
    """/model -- show the current model. /model <provider> [model-name] --
    switch to it, using that provider's already-configured key. Providers
    are looked up by name (not guessed from a model string): Biomni's own
    model-name auto-detection misclassifies our Groq default
    (openai/gpt-oss-120b, since it contains "/") as Ollama, so an explicit
    provider name sidesteps that rather than fighting it.
    """
    if not args:
        source = os.environ.get("GENPIPE_LLM_SOURCE", DEFAULT_SOURCE)
        model = getattr(agent.llm, "model", "?")
        print(f"  {display.GREY}{source} · {model}{display.RESET}\n")
        return
    wanted = args[0].lower()
    match = next((p for p in KNOWN_PROVIDERS if p[1].lower() == wanted), None)
    if match is None:
        choices = ", ".join(p[1] for p in KNOWN_PROVIDERS)
        print(f"  {display.RED}Unknown provider '{args[0]}' -- choices: {choices}{display.RESET}\n")
        return
    _, source, env_var, default_model = match
    if _looks_like_placeholder(os.environ.get(env_var, "")):
        print(f"  {display.RED}No {source} key configured yet -- run /key first.{display.RESET}\n")
        return
    model = " ".join(args[1:]) if len(args) > 1 else default_model
    _apply_llm(agent, source, model)
    _write_env_var("GENPIPE_LLM_SOURCE", source)
    _write_env_var("GENPIPE_LLM_MODEL", model)
    os.environ["GENPIPE_LLM_SOURCE"] = source
    os.environ["GENPIPE_LLM_MODEL"] = model


def _cmd_key(agent, args):
    """/key -- rotate or add a key. Reuses the exact first-launch prompt,
    then applies it immediately instead of waiting for a relaunch.
    """
    _prompt_for_api_key()
    source = os.environ.get("GENPIPE_LLM_SOURCE", DEFAULT_SOURCE)
    model = os.environ.get("GENPIPE_LLM_MODEL", DEFAULT_MODEL)
    _apply_llm(agent, source, model)


# --------------------------------------------------------------------- #
#  The spinner's commentary.
#
#  A GenPipes run takes minutes, and a spinner that says "thinking" for all
#  of them conveys nothing except that the process has not crashed. The
#  agent is already streaming messages describing exactly what it is doing,
#  so the label is derived from those instead of guessed at.
#
#  Every phrase is what the agent is doing NOW, in the present tense, and
#  short enough not to reflow the line. The labels are deliberately about
#  the work rather than the machinery: "generating the script", not
#  "invoking the execute node".
# --------------------------------------------------------------------- #

def _label_for(message):
    """A short phrase for what this message means the agent is up to."""
    events = display.parse(message)
    kinds = {e["kind"] for e in events}

    for event in events:
        if event["kind"] != "code":
            continue
        code = event["text"]
        if "genpipes" in code and "-g" in code:
            return "generating the script"
        if "log_report" in code:
            return "asking GenPipes"
        if "sacct" in code or "squeue" in code:
            return "asking Slurm"
        if "genpipes" in code and "-h" in code:
            return "reading genpipes -h"
        if re.search(r"\b(?:bash|sh)\s+\S+\.sh\b", code):
            return "submitting"
        first = next((w for w in code.split() if w.isalpha()), None)
        return f"running {first}" if first else "running"

    if "solution" in kinds:
        return "writing up"
    if "observation" in kinds:
        return "reading the output"
    if "plan" in kinds:
        return "planning"
    return "thinking"


def _narrate(activity):
    """An on_step callback that keeps the spinner's label current."""
    def step(message):
        activity.say(_label_for(message))
    return step


def _cmd_approve(agent, args):
    if not args:
        display.problem("usage: /approve <name>")
        return
    # resume() renders each step as it streams, but reports its outcome only as
    # a returned status dict -- the old raw Python REPL printed returned values
    # for free, and this loop doesn't, so the confirmation is explicit here.
    with ui.Activity("submitting") as act:
        agent.resume(args[0], approved=True, on_step=_narrate(act))
    display.post_approve(args[0], True)


def _cmd_reject(agent, args):
    if not args:
        display.problem("usage: /reject <name> [feedback...]")
        return
    name, *rest = args
    with ui.Activity("reworking") as act:
        agent.resume(name, approved=False, feedback=" ".join(rest),
                     on_step=_narrate(act))
    display.post_approve(name, False)


def _cmd_check(agent, args):
    if not args:
        display.problem("usage: /check <name>")
        return
    with ui.Activity("asking GenPipes"):
        agent.check(args[0])


def _cmd_jobs(agent, args):
    """/jobs <name> [failed] -- the individual Slurm jobs inside a run."""
    if not args:
        display.problem("usage: /jobs <name> [failed]")
        return
    only_failed = len(args) > 1 and args[1].lower().startswith("fail")
    with ui.Activity("asking Slurm"):
        agent.jobs(args[0], only_failed=only_failed)


def _cmd_why(agent, args):
    """/why <name> [question...] -- diagnose a failed run."""
    if not args:
        display.problem("usage: /why <name> [question...]")
        return
    name, *rest = args
    with ui.Activity("finding what failed") as act:
        agent.why(name, question=" ".join(rest) or None, on_step=_narrate(act))


def _cmd_cancel(agent, args):
    """/cancel <name> -- scancel a run's pending and running jobs.

    Confirmed rather than immediate. It is the one command here that destroys
    work irreversibly, and the gate's whole premise is that this tool asks before
    it does something to the cluster -- being careful on the way in and casual on
    the way out would be incoherent.
    """
    if not args:
        display.problem("usage: /cancel <name>")
        return
    name = args[0]
    try:
        answer = ui.ask(f"cancel every running job in {name}?  (yes/no)", "no")
    except (EOFError, KeyboardInterrupt):
        answer = "no"
    if answer.strip().lower() not in ("y", "yes"):
        display.nothing("Left alone.")
        return
    with ui.Activity("cancelling"):
        agent.cancel(name)


def _cmd_list(agent, args):
    agent.submissions()


def _cmd_history(agent, args):
    agent.history()


def _cmd_track(agent, args):
    if len(args) < 2:
        display.problem("usage: /track <name> <path/to/job_list>")
        return
    agent.track(args[0], args[1])


def _cmd_where(agent, args):
    """/where -- the directories that decide where everything lands.

    The current working directory is the load-bearing one: a submission's job
    list is looked for under it, so launching the app from an unexpected place is
    the difference between a run being registered and vanishing. Nothing else in
    the interface shows it.
    """
    display.where([
        ("launched from", os.getcwd()),
        ("agent workdir", agent.path),
        ("run registry", agent.registry.path),
        ("checkpoints", os.path.join(agent.path, "genpipe_checkpoints.sqlite")),
        ("this copy", HERE),
    ])


def _cmd_help(agent, args):
    display.help_text(HELP)


# One table, three consumers: the dispatcher below, the completion menu the
# prompt draws as you type, and /help. Adding a command here is the whole job --
# there is nowhere else for the three to disagree.
#
# The group column exists only for /help, which is now long enough that a flat
# alphabetical list stops being a reference. The groups follow the order things
# happen in, so the workflow is legible from the help itself.
#
# /exit has no handler because the loop has to break rather than return; it is
# listed anyway so it shows up in the menu and in /help like everything else.
COMMAND_SPECS = [
    ("approve", "<name>",             "let a held submission through to Slurm",  "deciding", _cmd_approve),
    ("reject",  "<name> [why...]",    "send it back with feedback instead",      "deciding", _cmd_reject),
    ("list",    "",                   "runs awaiting approval, and live ones",   "watching", _cmd_list),
    ("check",   "<name>",             "how a whole run is doing",                "watching", _cmd_check),
    ("jobs",    "<name> [failed]",    "the individual jobs inside a run",        "watching", _cmd_jobs),
    ("history", "",                   "every run recorded, live or gone",        "watching", _cmd_history),
    ("why",     "<name> [question]",  "diagnose a failed run",                   "fixing",   _cmd_why),
    ("cancel",  "<name>",             "scancel a run's remaining jobs",          "fixing",   _cmd_cancel),
    ("track",   "<name> <job_list>",  "adopt a run launched outside the agent",  "fixing",   _cmd_track),
    ("where",   "",                   "which directories this is using",         "setup",    _cmd_where),
    ("model",   "[provider [model]]", "show or switch the model behind this",    "setup",    _cmd_model),
    ("key",     "",                   "add or rotate an API key",                "setup",    _cmd_key),
    ("help",    "",                   "this list",                               "setup",    _cmd_help),
    ("exit",    "",                   "leave",                                   "setup",   None),
]

COMMANDS = {name: fn for name, _, _, _, fn in COMMAND_SPECS if fn}
MENU = [(name, args, desc) for name, args, desc, _, _ in COMMAND_SPECS]
HELP = [(name, args, desc, group) for name, args, desc, group, _ in COMMAND_SPECS]


def _resolve(word):
    """Map what was typed to a command name, accepting any unambiguous
    abbreviation -- /appr is /approve, and typing it in full is optional. An
    ambiguous stub resolves to nothing rather than to a guess.
    """
    names = [spec[0] for spec in COMMAND_SPECS]
    if word in names or word == "quit":
        return word
    hits = [name for name in names if name.startswith(word)]
    return hits[0] if len(hits) == 1 else None


def _name_run(agent, task):
    """Ask what to call this run, offering a name derived from the task.

    Two things happen here that both exist to protect a run from its own name.

    The suggestion removes the friction of being made to invent an identifier
    before you have seen anything -- it is pre-typed and editable, so Enter
    accepts it and a keystroke changes it.

    The collision check is the important one. The name is the LangGraph thread
    key, and Biomni's AgentState declares `messages` with no reducer, so starting
    a new run under an existing name would REPLACE that thread's state -- erasing
    the earlier conversation and, if it were parked at the gate, the pending
    approval with it. A run can be destroyed by a typo. So a taken name is
    silently advanced to the next free one, and the user is told.

    Returns the name, or None if the user cancelled.
    """
    suggestion = runs.suggest_name(task)
    try:
        wanted = ui.ask("name this run", agent.registry.unique_name(suggestion)).strip()
    except (EOFError, KeyboardInterrupt):
        display.nothing("Cancelled.")
        return None
    if not wanted:
        display.problem("A run needs a name -- it's how you approve it later.")
        return None
    unique = agent.registry.unique_name(wanted)
    if unique != wanted:
        display.nothing(
            f"'{wanted}' is taken -- using {display.BOLD}{unique}{display.RESET} instead.",
            "the earlier run keeps its name, its jobs and its history.")
    return unique


def _panel(gap):
    """Render one gap as a question. The seam intake.resolve() asks through.

    A panel with nothing to offer is worse than a plain question: it costs a
    keystroke to reach the free-text row and implies there were alternatives
    worth reading. So when nothing on disk matches, this degrades to a one-line
    prompt with the note as its hint.
    """
    if not gap.options:
        if gap.note:
            print(f"  {display.DIM}{gap.note}{display.RESET}")
        try:
            return ui.ask(gap.question).strip() or None
        except (EOFError, KeyboardInterrupt):
            return None
    return ui.choose(gap.question, gap.options,
                     note=gap.note, free_text=gap.free_text)


def _repl(agent):
    """The application's actual interface. Bare text is a task -- named, then
    run, since every run needs a name to /approve, /reject, or /check it later,
    possibly in a separate session. Anything starting with / is a command.

    The prompt is created once and kept, so history survives across turns.
    """
    prompt = ui.Prompt(MENU)

    while True:
        try:
            line = prompt.read().strip()
        except EOFError:
            break
        except KeyboardInterrupt:
            continue

        if not line:
            continue

        if line.startswith("/"):
            parts = line[1:].split()
            if not parts:
                continue
            cmd, args = _resolve(parts[0].lower()), parts[1:]
            if cmd in ("exit", "quit"):
                break
            handler = COMMANDS.get(cmd)
            if handler is None:
                print(f"  {display.RED}No such command: {line.split()[0]}{display.RESET}"
                      f"  {display.GREY}(try /help){display.RESET}\n")
                continue
            try:
                handler(agent, args)
            except Exception as e:
                print(f"  {display.RED}{type(e).__name__}: {e}{display.RESET}\n")
            continue

        # Fill in what the request left out before the model sees it, so the
        # panel's options come from the ini table rather than from a model's
        # idea of what protocols exist.
        try:
            stated, cancelled = intake.resolve(line, asker=_panel)
        except Exception as e:
            display.problem(f"{type(e).__name__}: {e}")
            continue
        if cancelled:
            display.nothing("Cancelled.")
            continue
        task = intake.restate(line, stated)

        try:
            name = _name_run(agent, line)
        except Exception as e:
            display.problem(f"{type(e).__name__}: {e}")
            continue
        if name is None:
            continue
        print()
        try:
            with ui.Activity("thinking") as act:
                agent.run(task, thread_id=name, on_step=_narrate(act))
        except KeyboardInterrupt:
            display.problem("Stopped.", "Nothing has reached the scheduler.")
        except Exception as e:
            display.problem(f"{type(e).__name__}: {e}")


def main(argv=None):
    """Start the app. Split out of __main__ so the full-app test can drive the
    same startup sequence a user gets rather than an approximation of it."""
    argv = list(sys.argv[1:] if argv is None else argv)
    fake_cluster = "--fake" in argv or bool(os.environ.get("GENPIPE_FAKE"))
    fake_llm = "--fake-llm" in argv or bool(os.environ.get("GENPIPE_FAKE_LLM"))
    notes = []
    fakecluster = None
    if fake_cluster or fake_llm:
        # Dev mode: a stubbed GenPipes and Slurm on PATH, and optionally a stand-in
        # for the model, so the whole interface -- gate, approve, check, jobs, why
        # -- can be exercised on any machine with no allocation, no cluster and no
        # API spend. Imported here rather than at module scope so a production
        # launch never even loads it.
        import fakecluster
    if fake_cluster:
        notes.append(fakecluster.activate(
            os.environ.get("GENPIPE_FAKE_STATE", "failed-oom")))
    if fake_llm:
        notes.append("scripted model")

    # Greet the user first, so the app visibly launches before anything else
    # asks for their attention -- the key prompt (if any) appears below this,
    # not in front of a blank terminal. It shows whatever model is already
    # configured; on a first launch there isn't one yet, and ready() below
    # states it once the key prompt has settled the question.
    display.banner(source=os.environ.get("GENPIPE_LLM_SOURCE"),
                   model=os.environ.get("GENPIPE_LLM_MODEL"))
    if fake_llm:
        # build_agent() constructs a real provider client even though dev mode
        # replaces it immediately below, and some providers refuse to construct
        # without a key at all. A placeholder satisfies that with no possibility
        # of being used -- and setdefault means a real key, if there is one, is
        # left untouched and never written anywhere.
        os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-devmode-placeholder")
    else:
        # A scripted model needs no key, and asking for one would make dev mode
        # unusable on a machine that has never had a key configured.
        _require_api_key()
    agent = build_agent()
    if fake_llm:
        agent.llm = fakecluster.DevLLM()
        # Biomni's tool retriever would spend a real model call on resource
        # selection before the scripted conversation even starts.
        agent.use_tool_retriever = False
    # Confirm readiness right before the command loop takes over -- see
    # display.ready()'s docstring for why this matters.
    display.ready(os.environ.get("GENPIPE_LLM_SOURCE", DEFAULT_SOURCE),
                  os.environ.get("GENPIPE_LLM_MODEL", DEFAULT_MODEL),
                  fake=" + ".join(notes) if notes else None)
    # A run left parked at the gate in an earlier session is the one thing that
    # must not wait to be asked about: its name lived only in that session's
    # scrollback, and without this the decision is simply lost.
    display.pending(agent.pending())
    # Environment problems that only surface at submit time, surfaced now. The
    # blocking one is re-checked at the gate; this is the early warning.
    if not fake_cluster:
        display.environment(preflight.check())
    try:
        _repl(agent)
    except KeyboardInterrupt:
        print()
    display.farewell(agent.pending())

    # Leave deliberately rather than falling off the end of main().
    #
    # On the raw-terminal path the interpreter does not shut down: it spins at
    # 100% CPU and never exits, so /exit and Ctrl+D both appear to hang and the
    # only way out is to close the terminal. Headless it exits in two seconds,
    # which is why the pty test never caught it -- its close() shuts the master
    # and the app dies of SIGHUP looking healthy.
    #
    # The app keeps its own history in ui.Prompt and asks nothing of readline
    # (imported only so any stray input() call is editable), so there is no
    # atexit work here worth waiting for. Flush what we printed, then go.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
