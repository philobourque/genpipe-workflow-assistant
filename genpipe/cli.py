import datetime
import getpass
import os
import re
import readline  # noqa: F401 -- side-effect only: gives input() arrow-key history/editing
import sys
import termios
import textwrap
import time
import tty
import io, contextlib
from pathlib import Path

from biomni.llm import get_llm

from . import display
from . import intake
from . import mirror
from . import modify
from . import override
from . import preflight
from . import prep
from . import readset
from . import runs as runs_store
from . import slots
from . import ui
from .agent import GenpipeA1

# HERE is the package; ROOT is the checkout it sits in. The distinction is
# load-bearing: genpipes.md ships beside the code that reads it, while .env
# belongs to the checkout -- start_agent.sh sources it from there before this
# process exists, so writing it anywhere else would silently stop persisting
# the API key while still looking like it worked.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
GRAMMAR_PATH = HERE / "genpipes.md"
ENV_PATH = ROOT / ".env"
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


def _prompt_for_name():
    """Ask what to call them, and remember it.

    Asked once, at the first launch that has no name saved, in the same spirit as
    the API key prompt: one question, answered, never asked again. The Unix
    username is pre-typed rather than offered as a default to press enter on, so
    the common case is one keystroke and a correction is an edit rather than a
    retype.

    Skipped silently when there is no terminal to ask on -- a piped or scripted
    launch keeps the username and asks nothing, which is what makes this safe to
    put in main().
    """
    if not sys.stdin.isatty():
        return
    suggestion = (os.environ.get("USER") or "").strip()
    print(f"\n  {display.BOLD}{display.GREEN}One thing first.{display.RESET}"
          f"  {display.GREY}Change it any time with /user.{display.RESET}")
    try:
        name = (ui.ask("What should I call you?", default=suggestion) or "").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    _set_name(name or suggestion)


def _set_name(name):
    """Apply a name for this session and save it for the next one."""
    name = " ".join((name or "").split())[:24]
    if not name:
        return
    os.environ["GENPIPE_USER"] = name
    _write_env_var("GENPIPE_USER", name)


def _require_name():
    """Ensure we know what to call the user, asking once if we don't."""
    if not (os.environ.get("GENPIPE_USER") or "").strip():
        _prompt_for_name()


def _cmd_user(agent, args):
    """/user [name] -- show or change what the agent calls you.

    Same shape as /model: no argument reports, an argument sets. The name is
    cosmetic -- it labels their turns in the transcript and greets them at
    launch -- so this needs no confirmation and takes effect on the next line.
    """
    if not args:
        print(f"\n  {display.DIM}Calling you{display.RESET} "
              f"{display.WHITE}{display.who()}{display.RESET}"
              f"   {display.GREY}/user <name> to change it{display.RESET}\n")
        return
    _set_name(" ".join(args))
    # The transcript label reads the environment every time, so it is already
    # current; the model's system prompt is not, and has to be told.
    agent.address_user(display.who())
    display.done(f"Noted -- {display.who()} it is.")


def _drop_sampling_params(llm, source):
    """Clear temperature/top_p/top_k on an Anthropic model, and return it.

    Biomni's get_llm defaults temperature to 0.7 (biomni/llm.py) and passes it
    to ChatAnthropic unconditionally. Every Claude model from Opus 4.7 onward --
    which includes the Sonnet 5 this app defaults to -- rejects the parameter
    outright:

        BadRequestError: 400 ... `temperature` is deprecated for this model.

    It fails on the first real call, so a fresh install looks like it works
    right up until the moment it is asked to do anything.

    Clearing the attribute is enough to remove it: langchain_anthropic builds
    its request payload and then filters out every None value (chat_models.py,
    _get_request_payload), so a None temperature is not sent at all rather than
    being sent as null.

    Dropping it rather than picking a supported value is deliberate. Nothing
    here wants a particular sampling temperature -- 0.7 is biomni's default, not
    a choice this project made -- and the set of models that accept it shrinks
    with every release. Letting the API apply its own default is the version of
    this that does not need a model table to keep up to date.

    Scoped to Anthropic because it is the provider whose current models reject
    it; the OpenAI, Gemini and Groq paths are left exactly as biomni built them.
    """
    if (source or "").lower() != "anthropic":
        return llm
    for name in ("temperature", "top_p", "top_k"):
        if getattr(llm, name, None) is not None:
            try:
                setattr(llm, name, None)
            except (AttributeError, ValueError, TypeError):
                # A pydantic model with validation that refuses None. Not worth
                # failing the launch over -- the request may still go through on
                # an older model, and the 400 above says exactly what happened.
                pass
    return llm


def build_agent(path=None, llm=None, source=None):
    """Construct a fully configured GenpipeA1 agent: the gated graph, with the
    GenPipes grammar registered as software.

    Shared by this module (real use, real LLM) and the mock pipeline test
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

    #    Pin the one fact in that document that is different per machine. The
    #    grammar is written for the Alliance in general and names the cluster ini
    #    as `common_ini/<cluster>.ini`; which cluster that is depends on where
    #    this process is running, and getting it wrong generates and submits
    #    cleanly before every job is rejected for a partition that does not exist
    #    there. Stated as a fact rather than left to be inferred from a hostname.
    here = preflight.cluster()
    if here:
        content += (f"\n\n## This machine\n\nYou are on **{here}**. The cluster "
                    f"ini for every `-c` stack is "
                    f"`$GENPIPES_INIS/common_ini/{preflight.cluster_ini()}`. "
                    f"Do not use another cluster's.\n")
    else:
        content += ("\n\n## This machine\n\nThis is not a recognised Alliance "
                    "login node, so the cluster ini cannot be assumed. Ask "
                    "which one to use before generating a command that "
                    "submits.\n")

    # 3. Register the grammar as software; this also reconfigures the agent, which is
    #    what rebuilds the gated graph. Biomni's add_software prints the full
    #    description it registers (a1.py:799) -- here, the entire grammar -- so
    #    swallow stdout for this one call only.
    with contextlib.redirect_stdout(io.StringIO()):
        agent.add_software({"genpipes": content})

    # 4. Strip the sampling parameters biomni set for us. Must happen after
    #    construction: A1.__init__ is what calls get_llm, so there is no
    #    argument we could have passed to prevent it.
    _drop_sampling_params(agent.llm, source)

    # 5. Switch off biomni's tool retriever, which is on by default.
    #
    #    It spends an extra model call at the top of every turn asking which of
    #    biomni's ~200 wet-lab tools and data-lake files are relevant, prints
    #    "Using prompt-based retrieval with the agent's LLM" while doing it, and
    #    then injects the winners into the system prompt. Nothing in this
    #    application uses any of them: the tools here are GenPipes, Slurm, and
    #    the person at the keyboard. What the retrieval actually achieved was to
    #    put a data lake in front of a model that then went looking through it
    #    for something to do.
    agent.use_tool_retriever = False

    return agent


# --------------------------------------------------------------------- #
#  The command loop -- the app's actual interface. Bare text is a task;  #
#  anything starting with / is a command. No Python syntax required.     #
# --------------------------------------------------------------------- #

# What _probe_llm found. Three outcomes rather than a boolean, because "the
# provider says no such model" and "the provider did not answer" call for
# opposite decisions: the first must block the switch, and the second must not,
# since the name is probably fine and refusing would strand somebody behind a
# rate limit.
_MODEL_OK = "ok"
_MODEL_REJECTED = "rejected"
_MODEL_UNVERIFIED = "unverified"


def _probe_llm(llm):
    """Ask the provider to answer one trivial prompt. Returns (state, detail).

    A provider only rejects an unknown model when a request is actually made.
    get_llm() builds a client object and returns -- ChatAnthropic's constructor
    never touches the network -- so nothing in the construction path can tell a
    real model name from a typo. That is the whole bug this exists to close:
    `/model Anthropic haiku-4-5` used to be accepted, confirmed on screen,
    written to .env, and then 404 a turn later from inside the agent loop,
    where it read as a problem with the message rather than with the command.

    Deliberately provider-agnostic. It goes through the langchain object the
    session is about to use, so one code path checks the whole chain at once --
    key, base url, model name, and whether this account may use that model --
    for all four providers, with no per-provider table to keep current. That is
    the same argument _drop_sampling_params makes about sampling parameters:
    the version of this that does not need a model table is the version that
    stays right.

    Costs one very short completion, paid only when somebody types /model or
    /key. Nothing overrides max_tokens to trim it further: the current Anthropic
    models think by default, a thinking budget shares that ceiling, and a
    one-token cap is a second way to fail that has nothing to do with whether
    the model exists.
    """
    try:
        llm.invoke("hi")
        return _MODEL_OK, ""
    except Exception as e:                       # noqa: BLE001 -- see below
        # Provider SDKs raise their own exception classes, and this module must
        # not import four of them to name the ones it cares about. Both the
        # Anthropic and OpenAI clients carry the HTTP status on the exception,
        # which is the part that actually distinguishes the cases.
        status = getattr(e, "status_code", None)
        text = str(e)
        if status == 404 or "not_found_error" in text:
            return _MODEL_REJECTED, "no such model"
        if status == 401 or "authentication_error" in text:
            return _MODEL_REJECTED, "the key was rejected"
        if status == 403 or "permission_error" in text:
            return _MODEL_REJECTED, "this key may not use that model"
        # Rate limits, overloads, timeouts, DNS. The model name is very likely
        # fine and the caller is told the check did not happen, rather than
        # being refused a switch on evidence nobody has.
        return _MODEL_UNVERIFIED, f"{type(e).__name__}: {text.splitlines()[0][:120]}"


def _apply_llm(agent, source, model):
    """Swap the model the agent uses. True if it is now in use.

    Effective immediately -- no restart, no rebuilding the graph. Safe because
    the compiled graph's generate node reads agent.llm live on every step
    rather than capturing it by value (verified against Biomni's a1.py), so
    reassigning this attribute is enough on its own.

    The new model is proven before it replaces the working one. A rejected
    model leaves the session exactly as it was: `agent.llm` is untouched, so a
    typo costs one error message rather than every turn until the next restart.
    """
    candidate = _drop_sampling_params(
        get_llm(model,
                stop_sequences=["</execute>", "</solution>"],
                source=source),
        source)
    state, detail = _probe_llm(candidate)
    if state == _MODEL_REJECTED:
        print(f"  {display.RED}{source} · {model} -- {detail}.{display.RESET}")
        # The one thing the provider will not tell us is what the name should
        # have been, and every wrong name here so far has been a real model
        # missing its family prefix.
        print(f"  {display.GREY}Model names are given in full, e.g. "
              f"{DEFAULT_MODEL}. Still using "
              f"{display._identity(os.environ.get('GENPIPE_LLM_SOURCE', DEFAULT_SOURCE), getattr(agent.llm, 'model', None))}."
              f"{display.RESET}\n")
        return False
    agent.llm = candidate
    # display._identity, not a second f-string saying the same thing: the
    # banner and the readiness line already render this pair, and a private
    # copy here is one edit away from the two disagreeing about how a model is
    # named.
    print(f"  {display.GREEN}Using{display.RESET} {display._identity(source, model)}")
    if state == _MODEL_UNVERIFIED:
        print(f"  {display.GREY}Could not reach {source} to confirm the model "
              f"exists -- {detail}{display.RESET}")
    print()
    return True


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
    # Persisted only once the provider has answered to it. Writing first was
    # what turned a typo into a lasting problem: the bad name went into .env,
    # survived the restart, and came back on the next launch's banner, so the
    # session after the mistake looked broken for no visible reason.
    if not _apply_llm(agent, source, model):
        return
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

    # The classification itself lives in display._code_label, which the transcript
    # already uses to title the block. One classifier, two consumers: the label on
    # screen and the spinner's commentary cannot disagree about what is happening.
    for event in events:
        if event["kind"] != "code":
            continue
        code = event["text"]
        phrase = {"GENERATE": "generating the script",
                  "SUBMIT": "submitting",
                  "SCHEDULER": "asking the scheduler"}.get(event.get("label"))
        if phrase:
            return "asking GenPipes" if "log_report" in code else phrase
        if "genpipes" in code and "-h" in code:
            return "reading genpipes -h"
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
    # THE CONFIRMATION COMES FROM THE RECORD, NEVER FROM THE GRAPH.
    #
    # This used to read `if status["status"] == "done"`, and "done" means only
    # that the graph is not paused. It is true of a thread that finished, of one
    # that died, and of one that was never resumed at all -- so /approve printed
    # "<name> · submitted" for runs it had not touched. Meanwhile the run that
    # really did submit 46 jobs on 2026-07-29 was never marked at all.
    #
    # agent.resume() now reconciles in a `finally` and writes what actually
    # happened, so the honest thing to report is what it wrote.
    name = args[0]
    with ui.Activity("submitting") as act:
        status = agent.resume(name, approved=True, on_step=_narrate(act))
    if (status or {}).get("submitted") is False:
        return                       # resume() has already said why
    record = agent.registry.get(name) or {}
    if record.get("status") == runs_store.HELD:
        return                       # never resumed; resume() explained it
    display.post_approve(name, record)


def _cmd_reject(agent, args):
    """/reject <name> [why] -- abandon a held run. Terminal.

    This used to be rework: it regenerated and came back to the gate. That
    behaviour now lives in /modify, and this is the verb that was missing --
    there was no way to abandon a run at all, so a run you had mentally dropped
    kept appearing in /list and in the startup pending line forever.
    """
    if not args:
        display.problem("usage: /reject <name> [why...]")
        return
    name, *rest = args
    agent.abandon(name, " ".join(rest) or None)


def _rework(agent, name, feedback, warnings=(), changed=()):
    """Send a run back to the model with feedback and return to the gate.

    The single seam every /modify path funnels through -- direct, guided, and
    prose at the gate -- so there is one place where a change becomes a model
    call and one place where the run keeps its name.

    `changed` names the rows this rework is moving, and rides the same seam as
    `warnings` for the same reason: the gate that comes back is where somebody
    reads, and it is otherwise identical to the gate they just rejected.
    Confirming a change landed by diffing two screens from memory is how a
    person approves a command they did not mean to, so the rows that moved come
    back green. The guided flow knows them exactly; a prose change does not, and
    passes nothing rather than guessing -- a green line that did not move is
    worse than no green at all.
    """
    agent._gate_note = {"warnings": list(warnings or ()),
                        "changed": list(changed or ())}
    with ui.Activity("modifying") as act:
        status = agent.resume(name, approved=False, feedback=feedback,
                              on_step=_narrate(act))
    return status


def _modify_direct(agent, name, change):
    """/modify <name> <change> -- one change, stated in prose, one model call.

    No apply screen: the re-rendered gate is the review, and confirming a change
    that has a confirmation coming is how a two-keystroke edit becomes four.
    """
    risks, stop = _step_risks(agent, name, change)
    if stop:
        display.problem(stop, "Re-read the step list before changing anything else.")
        return
    _rework(agent, name, change, warnings=risks)


def _step_risks(agent, name, change):
    """Tier 3, for a change that mentions steps. Returns (risks, hard_stop).

    Only fires when the change actually names a step range -- reading --help
    costs a subprocess and a module load, and paying that to validate a protocol
    swap would put a two-second pause in front of every modify.
    """
    m = re.search(r"\b(\d+(?:-\d+)?(?:\s*,\s*\d+(?:-\d+)?)*)\b", change or "")
    if not m or not modify.valid_steps(m.group(1)):
        return [], None
    record = agent.registry.get(name) or {}
    values = (record.get("proposal") or {}).get("slots") or {}
    help_text = agent.step_help(values.get("pipeline"), values.get("protocol"))
    if not help_text:
        return [], None
    return modify.step_risk(m.group(1), help_text)


def _cmd_modify(agent, args):
    """/modify <name> [what to change] -- rewrite a run's command.

    With a change, it is one model call and back to the gate. Without one, it
    opens the guided flow: pick the rows, fill them in, review, apply.

    A RUN THAT IS NOT HELD IS FORKED, NEVER REWRITTEN. This used to refuse
    outright -- "only a run waiting at the gate can be modified" -- which made
    the commonest thing anybody wants to do with a finished run impossible:
    run it again against a different readset, a second database, a re-sequenced
    sample. The command is right there on the record and the fork machinery
    already existed; all that stood in the way was this check.

    Rewriting one in place stays forbidden, and for the reason abandon() and
    rename() give: after submission the name is tied to a job list and to jobs
    that may still be on the scheduler, and moving it strands them. So the
    original is never touched. What comes back is a second run, held at its own
    gate, which is what somebody asking for this actually wanted anyway.
    """
    if not args:
        held = agent.registry.held()
        if len(held) == 1:
            args = [held[0]["name"]]
        else:
            display.problem("usage: /modify <name> [what to change]",
                            "/list shows what is held.")
            return
    name, *rest = args
    record = agent.registry.get(name)
    if record is None:
        display.problem(f"No run named '{name}'.", "/list shows what there is.")
        return

    if record["status"] != runs_store.HELD:
        # A run discovered by /scan or registered by /track has a pipeline and a
        # protocol read off a job-list filename and no command at all. There is
        # nothing to fork FROM, and saying so is better than producing a variant
        # of a command this tool never saw.
        if not (record.get("proposal") or {}).get("generated"):
            display.problem(
                f"'{name}' has no generation command on record.",
                "It was found on disk rather than built here, so there is "
                "nothing to copy. Describe the run you want instead.")
            return
        _modify_past(agent, name, record, " ".join(rest))
        return

    if rest:
        _modify_direct(agent, name, " ".join(rest))
        return
    _modify_guided(agent, name, record)


def _modify_past(agent, name, record, change=""):
    """/modify on a run that has already been submitted. Always a fork.

    Prose goes straight through -- there is one question left, what to call the
    result, and the panel that would ask it has nothing else to offer. Without
    prose it opens the same guided flow a held run gets, with the ending
    restricted: applying to the original is not on the table.
    """
    proposal = record.get("proposal") or {}
    # The run's ACTUAL lifecycle state, not the raw registry status -- "live"
    # is reserved for a run that currently has queued or running jobs (see
    # runs.list_tag(), the same words /list tags its rows with), and a launched
    # run sitting here is exactly as likely to be finished, failing, or
    # unreachable as it is to still be running.
    status = runs_store.resolve(record) if record.get("job_list") else None
    display.forking_from(name, runs_store.list_tag(record, status))
    if not change:
        _modify_guided(agent, name, record, fork_only=True)
        return

    wanted = agent.registry.unique_name(name)
    try:
        wanted = ui.ask("What should the new run be called?", default=wanted)
    except (EOFError, KeyboardInterrupt):
        return
    verdict = modify.check("name", str(wanted or ""), proposal,
                           registry=agent.registry)
    if not verdict:
        display.problem(verdict.message, f"'{name}' is unchanged.")
        return

    request = modify.fork_prose(proposal, change)
    new_name = agent.registry.unique_name(str(wanted).strip())
    agent._gate_note = {"warnings": [], "changed": []}
    thread = f"{name}::variant-{datetime.datetime.now():%m%d%H%M%S}"
    with ui.Activity("building the variant") as act:
        agent.run(request, thread, on_step=_narrate(act), name=new_name)
    display.forked(name, new_name)


def _run_panel(agent, name, proposal, m, offered, candidates, changes,
               required, fork_only=False):
    """The in-place panel. Returns (outcome, row) and mutates `changes`.

        ("done",   None)   review what has been changed
        ("else",   None)   describe it instead, in prose
        ("ask",    row)    resources -- see below
        ("cancel", None)   escape with nothing open

    Enter OPENS a row rather than ticking it, and picking a choice collapses it
    again, green, in place. That is the whole difference from the panel this
    replaces, and everything else here follows from it: there is no confirm
    keystroke left over, hence the DONE row; the cursor must not wander while a
    row is open, hence panel_entries' selectability rule; and typing narrows the
    open row rather than jumping between rows, hence on_text.

    A row with no vocabulary opens the same way -- the caret lands on the row
    and Enter takes what was typed, via the TYPED entry panel_entries emits.
    This used to drop out to a prompt underneath the panel, which put the whole
    stacked-questions layout back on screen one row at a time.

    RESOURCES IS THE ONE THAT STILL LEAVES, and it is not an exception to the
    rule so much as a different question. It is not a value somebody types: it
    is an override ini this tool writes, and writing it means asking which step,
    which keys and what values. That is a flow, not a field, so it gets a screen
    rather than a caret.
    """
    state = {"open": None, "typed": "", "out": None, "row": None,
             # The last refusal, as (row, message). It rides in the panel's
             # notes rather than being printed, because anything printed above
             # a panel that repaints over itself is gone by the next keystroke
             # -- which is how the same wrong answer gets typed twice.
             "problem": None}

    def choices():
        if not state["open"]:
            return ()
        return modify.options_for(state["open"], proposal, candidates,
                                  pending=changes)

    def entries():
        return modify.panel_entries(m, offered, state["open"], choices(),
                                    state["typed"], changes)

    def options():
        return [slots.Option(e.value, e.label, e.description)
                for e in modify.selectable(entries())]

    def question():
        if state["open"]:
            return modify.question_for(state["open"], proposal, pending=changes)
        return "What should change?"

    def settle(row, picked):
        """Take `picked` for `row`, or leave the row open saying why not."""
        if row == modify.CONFIG:
            # `-c` is a stack, so Enter moves ONE ini on or off it and the row
            # stays open for the next one. Everywhere else Enter is an answer
            # and closing the row is the acknowledgement; here it would mean a
            # person adding a genome ini and dropping cit.ini has to find the
            # row, open it, and re-read a four-line list twice.
            #
            # The stack is recomputed from the proposal every time rather than
            # mutated in place, so toggling something on and back off leaves
            # `changes` holding a list equal to what the run already has --
            # which sentence() then correctly reports as no change at all,
            # rather than as a -c edit that happens to be a no-op.
            state["typed"] = ""
            stack = modify.toggle_config(proposal, changes, picked)
            if stack == modify.config_stack(proposal):
                # Toggled back to where it started. Dropping the entry rather
                # than storing a list that equals the original is what keeps
                # the row from going green, the count from saying "1 change",
                # and sentence() from spending a regeneration on a diff with
                # nothing in it.
                changes.pop(row, None)
            else:
                changes[row] = stack
            required.pop(row, None)
            return True
        verdict = modify.check(row, picked, proposal, registry=agent.registry,
                               name=name, pending=changes)
        if not verdict:
            # A tier-1 refusal leaves the row open with what was typed still
            # there. Closing it would throw away the narrowing and make the
            # person start the row again to read the reason.
            state["problem"] = (row, verdict.message)
            return True
        changes[row] = picked
        required.pop(row, None)
        # What this answer has just made mandatory -- changing the pipeline
        # invalidates the protocol, the -c stack and the step numbers. Chased
        # here rather than at the review, so the rows turn red while the person
        # is still looking at the panel that made them red.
        for other, why in modify.required_after(proposal, changes).items():
            if not changes.get(other):
                required[other] = why
        state.update(open=None, typed="", problem=None)
        return modify.cursor_of(entries(), row)

    def on_enter(value):
        kind = value[0]
        if kind == modify.CHOICE:
            return settle(value[1], value[2])
        if kind == modify.TYPED:
            return settle(value[1], state["typed"].strip())
        if kind == modify.ROW:
            row = value[1]
            if row == modify.RESOURCES:
                state.update(out="ask", row=row)
                return False
            state.update(open=row, typed="", problem=None)
            return 0
        state.update(out="done" if value[1] == modify.DONE else "else")
        return False

    def on_escape():
        if state["open"]:
            state.update(open=None, typed="", problem=None)
            return True
        return False

    def on_text(key):
        if not state["open"]:
            return False
        if key in ("\x7f", "\x08"):
            state["typed"] = state["typed"][:-1]
        else:
            state["typed"] += key
        # The refusal was about what was typed a keystroke ago. Keeping it on
        # screen while the text under it changes makes it look like a verdict
        # on the new text, which it is not.
        state["problem"] = None
        return True

    def notes():
        if not state["problem"]:
            return {}
        row, message = state["problem"]
        return {row: (display.RED, message)}

    draw = display.modify_panel(entries, changes=lambda: changes,
                                required=lambda: required, notes=notes,
                                typed=lambda: state["typed"],
                                open_of=lambda: state["open"],
                                details=ui.details_on)
    def keys():
        if state["open"]:
            if choices():
                return "↑↓ · 1-9 to pick · type to narrow · esc closes the row"
            return "type the new value · enter confirms · esc closes the row"
        if fork_only:
            return "↑↓ · enter opens a row · esc leaves without creating a new run"
        return ("↑↓ · enter opens a row · esc leaves this run alone, "
                "changes and all")

    picked = ui.choose(question, options, free_text=False, draw=draw,
                       cursor=modify.cursor_of(entries(), "name"),
                       on_enter=on_enter, on_escape=on_escape, on_text=on_text,
                       note=keys)
    if state["out"]:
        return state["out"], state["row"]
    return ("cancel", None) if picked is None else ("done", None)


def _modify_guided(agent, name, record, fork_only=False):
    """The guided /modify: multi-select the rows, fill each, review, apply.

    One model call for the whole set, however many rows changed -- composed into
    a single unambiguous sentence by modify.sentence(). The command string is
    never rebuilt here, even for a change as trivial as a flag swap: that would
    diverge the model's view of the conversation from what is queued, and its
    next turn would reason about a command it did not write.

    `fork_only` is for a run that has already been submitted -- see
    _modify_past. Everything about picking and filling the rows is identical;
    what changes is that the ending cannot offer to apply the changes to the
    original, because its name belongs to a job list now.
    """
    proposal = record.get("proposal") or {}
    directory = record.get("workdir") or os.getcwd()
    candidates = intake.candidates(directory)

    # Checked once, outside the loop below: mirror.read()'s emptiness depends
    # only on the recorded command, which does not change from one pass of
    # this loop to the next. A command that exists but could not be reliably
    # tokenised (see mirror.read()'s corruption check) falls back to the
    # proposal's parsed slots -- degraded, but not silently: this is the one
    # place that fallback is told apart from "no generation was ever
    # captured", so the person sees a warning instead of trusting fields that
    # were reconstructed rather than read.
    generated = proposal.get("generated")
    if generated and not mirror.read(generated):
        display.problem(
            "The original command could not be read back reliably.",
            "Showing the fields recorded when it was built instead — "
            "double-check anything unusual before applying changes.")

    picked = None
    changes = {}
    # Rows a previous pass left mandatory and unanswered. They come back red in
    # the panel rather than only in the round that raised them: somebody who
    # chose "change something else" is now looking at the command again, and the
    # obligation did not go away because the screen did.
    required = {}
    while True:
        # The mirror is built from the run's own generated command, so what the
        # panel offers to change and what is actually queued cannot disagree.
        # Rows that modify.rows_for() withholds -- a `-p` on a germline run --
        # still appear if the command has them, without a box: the panel is
        # allowed to show more than it will change, never less.
        #
        # The override summary is re-read every pass rather than captured once
        # before the loop. It is a file on disk, and tuning a step that was
        # already tuned rewrites it without changing the command -- which is
        # exactly the case a summary read once would show stale.
        tuned = override.summary(
            override.read(override.path_for(name, directory, proposal)))
        rows = modify.rows_for(proposal, name, resources=tuned)
        m = (mirror.read(proposal.get("generated"), name=name, resources=tuned,
                         missing=proposal.get("missing"))
             or mirror.from_slots(proposal, name=name, resources=tuned)).ensure(
                 [row for row, _ in rows])
        # Cursor order follows the mirror, not ROWS, so ↓ moves down the screen.
        # They agree today; they would not the moment a command carried a flag
        # in an order the table does not know, and an arrow key that skips a
        # line is the kind of wrongness nobody reports and everybody distrusts.
        offered = [line.row for line in m.lines
                   if line.row in {row for row, _ in rows}]

        outcome, row = _run_panel(agent, name, proposal, m, offered,
                                  candidates, changes, required,
                                  fork_only=fork_only)
        if outcome == "cancel":
            if fork_only:
                display.nothing("Nothing created.",
                                f"'{name}' and its job list are unchanged.")
            else:
                display.nothing("Left alone.", f"'{name}' is still held.")
            return
        if outcome == "else":
            try:
                text = ui.ask("What should change?")
            except (EOFError, KeyboardInterrupt):
                return
            if text:
                _modify_direct(agent, name, text)
            return
        if outcome == "ask":
            # Resources -- the one row that is a flow rather than a field. Its
            # screen writes an override ini and hands back the path, which goes
            # into `changes` like any other answer, and the loop comes straight
            # back to the panel with that row now green.
            path = _fill_resources(agent, name, proposal, directory)
            if path is None:
                return
            if path:
                changes[row] = path
            continue

        if not changes:
            if fork_only:
                display.nothing("Nothing changed.",
                                f"'{name}' and its job list are unchanged.")
            else:
                display.nothing("Nothing changed.", f"'{name}' is still held.")
            return

        # `config` hands change_plan the OLD STACK AS A LIST rather than
        # modify.current()'s joined string, because that is what makes the two
        # sides comparable: change_plan diffs them by basename to mark what was
        # added and what left. Every other row is a scalar and stays one.
        deltas = [(row,
                   modify.config_stack(proposal) if row == modify.CONFIG
                   else name if row == "name"
                   else modify.current(proposal, row),
                   new) for row, new in changes.items()]
        notes = modify.cross_check(proposal, changes)
        display.change_plan(deltas, notes)
        ending, fork_name = _ask_ending(agent, name, changes,
                                        fork_only=fork_only)
        if ending == "again":
            required = {row: why for row, why
                        in modify.required_after(proposal, changes).items()
                        if not changes.get(row)}
            continue
        if ending == "fork":
            _fork_run(agent, name, record, proposal, changes, wanted=fork_name)
            return
        if ending == "apply":
            break
        # esc, or a headless caller that answered nothing. Silence is not
        # consent for a change set somebody spent four prompts assembling, but
        # it is not a change either.
        if fork_only:
            display.nothing("Nothing created.",
                            f"'{name}' and its job list are unchanged.")
        else:
            display.nothing("Left alone.", f"'{name}' is still held.")
        return

    _apply_changes(agent, name, proposal, changes)


def _ask_ending(agent, name, changes, fork_only=False):
    """What to do with a finished change set. Returns (ending, fork name).

    THREE ROWS, NOT FOUR. There used to be a "hold for later" between "back to
    launch" and the fork, and the two of them applied the same changes, left the
    run in the same `held` status, and submitted nothing. The only difference
    was whether the gate was redrawn afterwards -- a rendering preference,
    offered as a decision, on the screen whose entire job is to make decisions
    legible. (The batch case it was reaching for is real: somebody editing six
    runs does not want six boxes. That wants a flow of its own, not a menu row
    whose sole effect is suppressing a repaint.)

    The names changed with the count. "back to launch" launched nothing -- it
    applied and redrew the gate -- and "change something else" was four words
    for "keep editing". Every label here now begins with a verb that is what
    happens.

    ESC IS THE FOURTH OUTCOME AND IT DISCARDS. That is why it is in the note
    rather than left to the generic "esc cancels": a person who has answered
    four prompts reads "cancels" as "close this menu", and the changes are gone.
    It is deliberately not a row -- an outcome that throws work away should take
    a key you have to mean, not one you can arrow onto.

    THE FORK'S NAME IS ON ITS ROW. Picking the fork used to be followed by a
    bare "what should the new run be called?", so the one fact that decides the
    answer -- whether the name is free -- arrived after the fork was already
    chosen, and unique_name() appended a `-2` nobody was shown. Now it is a
    Field: the proposed name is visible while the choice is still open, and the
    collision is stated before Enter.
    """
    # The name the fork should propose. A `name` row already answered in this
    # pass is what somebody has said they want this run called, so the fork
    # takes it; otherwise the first free variant of the current name, which is
    # what unique_name would have picked silently anyway.
    proposed = changes.get("name") or agent.registry.unique_name(name)

    def collision(text):
        """What to say about the typed name, recomputed on every keystroke."""
        if not text:
            return "a name is needed to keep both runs"
        verdict = modify.check("name", text, None, registry=agent.registry)
        if not verdict:
            return verdict.message
        settled = agent.registry.unique_name(text)
        if settled != text:
            return f"{text} already exists — enter saves this as {settled}"
        return ""

    def usable(text):
        """Whether Enter may leave with this name. A collision is not a
        refusal -- it is answered by suffixing, and the note says so."""
        return bool(text) and bool(modify.check("name", text, None,
                                                registry=agent.registry))

    field = ui.Field("fork", proposed, note=collision, ok=usable,
                     hint="type to rename · enter confirms · esc discards")

    # A submitted run cannot be the target of its own edit, so the row that
    # would offer it is not drawn at all. Greying it out and explaining would
    # be worse: this panel's job is to make the remaining choice obvious, and
    # the remaining choice here is genuinely just the name.
    rows = []
    if not fork_only:
        rows.append(slots.Option("apply", f"apply to {name}",
                                 "back to the gate, still held"))
    rows.append(slots.Option("fork", "save as a new run",
                             f"{name} stays exactly as it is"))
    rows.append(slots.Option("again", "keep editing", "back to the command"))

    # With the apply row gone the fork is row 0, so the cursor opens on the
    # field -- which is right when the name is the only thing left to decide,
    # and would be wrong on the held panel where applying is the common answer.
    ending = ui.choose(
        "What should happen to these changes?", rows,
        free_text=False, field=field,
        note="nothing is submitted by any of these  ·  esc discards them")
    return ending, field.text


def _fill_resources(agent, name, proposal, directory):
    """Tune one or more steps' cluster resources into this run's override ini.

    Returns the ini's path, "" if nothing was written, or None if the person
    backed out. The path is what the caller turns into a change, because from
    the COMMAND's point of view that is the whole of it: one more entry at the
    end of `-c`. Everything else happened in a file.

    Step names come from `genpipes <pipeline> -t <protocol> --help`, read at the
    moment of asking. There is no step table in this repo and there must not be
    one -- genpipes.md says so, because the list is version-exact -- so when
    --help cannot be reached the panel degrades to free text rather than to a
    guess. A typed step name is still checked for SHAPE, because a section
    header GenPipes does not recognise is not an error: it is ignored, and the
    run fails a second time in exactly the same way.

    No value is ever suggested. genpipes.md forbids proposing a resource number
    that was not computed from something observed, and its own worked example is
    a TIMEOUT whose obvious walltime fix was the wrong one. The examples in the
    prompts are shapes, not recommendations.
    """
    values = (proposal or {}).get("slots") or {}
    path = override.path_for(name, directory, proposal)
    sections = override.read(path)

    # An override /diagnose already worked out from the logs. Offered, never
    # applied: it was computed from evidence and is very likely right, and it is
    # still a resource number somebody is about to spend an allocation on. The
    # offer is the whole point of parsing it -- the alternative was reading a
    # fenced ini block off the screen and retyping it, which is where a step
    # name gets misspelled and the override silently does nothing.
    # Two genuinely different situations reach this flow, and they deserve
    # different first questions.
    #
    #   AFTER A FAILURE  /diagnose has read the logs and computed a value from
    #                    what it saw. That is the strongest answer anyone here
    #                    has, and it should be the thing on offer -- not a
    #                    yes/no gate in front of a form that then asks the same
    #                    questions from scratch.
    #   BEFORE A LAUNCH  there is no evidence, so no value may be proposed at
    #                    all (genpipes.md, and its worked example is a timeout
    #                    whose obvious walltime fix was the wrong one). The
    #                    only honest offer is "which step, which knob".
    proposed = (agent.registry.get(name) or {}).get("proposed_override") or {}
    proposed = {s: k for s, k in proposed.items() if s not in sections}
    if proposed:
        display.overrides(name, override.describe(proposed), path)
        take = ui.choose(
            "What should this run use?",
            [slots.Option("take", "what /diagnose worked out",
                          "computed from the logs of the failed run"),
             slots.Option("edit", "those, then change them",
                          "start from them and tune further"),
             slots.Option("mine", "set them myself",
                          "ignore the diagnosis and start empty")],
            free_text=False,
            note="nothing is written to disk until you are done")
        if take is None:
            return None
        if take in ("take", "edit"):
            for step, keys in proposed.items():
                sections = override.merge(sections, step, keys)
        if take == "take":
            # Accepted as-is. Asking "which step should be tuned?" after
            # somebody has just said "use exactly those" is asking them to
            # re-answer a question they came here having already answered.
            display.overrides(name, override.describe(sections), path)
            return override.write(path, sections, run=name)

    help_text = agent.step_help(values.get("pipeline"), values.get("protocol"))
    known = modify.steps_from_help(help_text)
    # When --help cannot be reached the panel has no step list to offer and
    # degrades to free text. It used to do that SILENTLY, which is how somebody
    # faced with "its --help name, e.g. gatk_sam_to_fastq" and no list typed
    # `--help` -- twice -- trying to get the thing the prompt was quoting. A
    # step name is not something anybody knows by heart, and a prompt that
    # cannot offer them owes an explanation for why.
    if not known:
        display.no_step_list(values.get("pipeline"), values.get("protocol"))

    while True:
        step = _ask_step(known, sections)
        if step is None:
            return None
        if not step:
            break
        settings = _ask_settings(step, sections.get(step) or {})
        if settings is None:
            return None
        sections = override.merge(sections, step, settings)

        again = ui.choose("Tune another step?",
                          [slots.Option("no", "no", "write what is there"),
                           slots.Option("yes", "yes", "pick another step")],
                          free_text=False)
        if again != "yes":
            break

    display.overrides(name, override.describe(sections), path)
    # Whether a file actually went is asked BEFORE writing, because write()
    # returns '' both for "deleted it" and for "there was never one" -- and this
    # used to announce a removal on the strength of that empty string alone.
    # Backing out of the step prompt on a run with no overrides printed
    # "<run>.override.ini was removed" about a file that had never existed.
    had_one = os.path.exists(path)
    written = override.write(path, sections, run=name)
    if not written and had_one:
        display.nothing("No overrides left.",
                        f"{os.path.basename(path)} was removed.")
    return written


def _ask_step(known, sections):
    """Which step to tune. '' to stop, None if the person backed out."""
    options = [slots.Option(step, step, f"{len(keys)} override(s) already")
               for step, keys in sorted(sections.items())]
    options += [slots.Option(n, n, f"step {i}") for i, n in known
                if n not in sections]
    while True:
        try:
            if options:
                answer = ui.choose("Which step should be tuned?", options,
                                   free_text=True, free_label="another step…",
                                   note="section names are the step names "
                                        "--help prints")
            else:
                answer = ui.ask("Which step should be tuned? (its --help name, "
                                "e.g. gatk_sam_to_fastq)")
        except (EOFError, KeyboardInterrupt):
            return None
        answer = (answer or "").strip()
        if not answer or override.valid_section(answer):
            return answer
        print(f"  {display.RED}▌{display.RESET} {answer!r} is not shaped like a "
              f"step name.")
        print(f"  {display.RED}▌{display.RESET} {display.GREY}GenPipes would "
              f"ignore the section silently, and the run would fail the same "
              f"way again.{display.RESET}")


def _ask_settings(step, existing):
    """Which keys to set on one step, and to what. None if backed out.

    An empty answer to a prompt CLEARS that key rather than skipping it, which
    is how an override added a minute ago is undone without opening the file.
    """
    options = []
    for key, label, _, example in override.SETTINGS:
        now = existing.get(key)
        options.append(slots.Option(key, label,
                                    f"now {now}" if now else f"e.g. {example}"))
    try:
        picked = ui.choose(f"What should change for {step}?", options,
                           free_text=False, multi=True,
                           note="empty answer clears a setting")
    except (EOFError, KeyboardInterrupt):
        return None
    if not picked:
        return {}

    out = {}
    for key, label, prompt, example in override.SETTINGS:
        if key not in picked:
            continue
        # `ram` is the one prefilled field, and only because its idiomatic value
        # is a REFERENCE rather than a number: `%(cluster_mem)s` means "whatever
        # the memory ends up being", which cannot be the wrong size. Prefilling
        # a walltime or a memory figure would be proposing a value nobody
        # computed, which genpipes.md forbids for good reasons.
        default = example if key == "ram" else ""
        while True:
            try:
                answer = ui.ask(f"{step} · {prompt}", default=default)
            except (EOFError, KeyboardInterrupt):
                return None
            if answer is None or not str(answer).strip():
                out[key] = ""             # clear it
                break
            verdict = override.validate(key, str(answer))
            if verdict.ok:
                out[key] = str(answer).strip()
                break
            print(f"  {display.RED}▌{display.RESET} {verdict.message}")
    return out


def _fork_run(agent, name, record, proposal, changes, wanted=None):
    """Apply a change set as a SECOND run, leaving the first one untouched.

    The reason this is not a rename: a thread parked at the gate holds exactly
    one interrupt, so a variant produced by reworking the original replaces it.
    Anybody comparing a ten-minute walltime against a thirty-minute one wants
    both, and there is no way back to the first once the model has regenerated
    over it.

    So the fork opens its own conversation. It costs the same single model call
    a rework does, the original stays parked and approvable exactly as it was,
    and the new run gets its own thread, its own name and its own gate. What it
    does NOT get is the original's history, which is why the request carries the
    base command with it -- see modify.fork_sentence.

    `wanted` normally arrives already answered, off the fork row's own field --
    see _ask_ending. The prompt below is the fallback for callers that have no
    panel to have asked in, and the `name` row is popped either way: a rename
    belongs to the run being forked FROM, and applying it here would rename the
    original as a side effect of copying it.
    """
    changes.pop("name", None)
    if not wanted:
        try:
            wanted = ui.ask("What should the new run be called?")
        except (EOFError, KeyboardInterrupt):
            return
    verdict = modify.check("name", str(wanted or ""), proposal,
                           registry=agent.registry)
    if not verdict:
        display.problem(verdict.message, f"'{name}' is unchanged.")
        return

    new_name = agent.registry.unique_name(str(wanted).strip())

    # The fork gets its OWN override ini, copied before the sentence is built so
    # the sentence names the new path. Without this the variant's -c pointed at
    # its parent's file and re-tuning either one silently re-tuned both -- which
    # is precisely what naming the file after the run is supposed to prevent.
    # Only when this fork is not already writing its own: a resources change in
    # this same pass has already produced a file, and copying over it would
    # replace what was just tuned with what it was forked from.
    directory = record.get("workdir") or os.getcwd()
    if modify.RESOURCES not in changes:
        parent_ini = override.path_for(name, directory, proposal)
        own_ini = os.path.join(directory, f"{new_name}.override.ini")
        if override.copy(parent_ini, own_ini):
            changes[modify.RESOURCES] = own_ini

    request = modify.fork_sentence(proposal, changes)
    if not request:
        display.problem("There is no generation command to copy from.",
                        f"/modify {name} on its own changes this run instead.")
        return

    risks = []
    if "steps" in changes:
        risks, stop = _step_risks(agent, name, changes["steps"])
        if stop:
            display.problem(stop, f"Nothing was changed; '{name}' is unchanged.")
            return

    agent._gate_note = {"warnings": risks, "changed": list(changes)}
    thread = f"{name}::variant-{datetime.datetime.now():%m%d%H%M%S}"
    with ui.Activity("building the variant") as act:
        agent.run(request, thread, on_step=_narrate(act), name=new_name)
    display.forked(name, new_name)


def _apply_changes(agent, name, proposal, changes):
    """Apply a validated change set: at most one model call, then the rename.

    The rename goes LAST, after resume() returns, so the gate re-renders once
    and under the new name. Doing it first would draw the box twice -- once
    under each name -- which reads as two runs.

    There is no longer a version of this that applies the changes and does NOT
    redraw the gate. That was "hold for later", and it left the run in exactly
    the state this does -- see _ask_ending, which explains why it stopped being
    offered as a choice.
    """
    new_name = changes.pop("name", None)
    sentence = modify.sentence(proposal, changes)

    if sentence:
        risks, stop = [], None
        if "steps" in changes:
            risks, stop = _step_risks(agent, name, changes["steps"])
            if stop:
                display.problem(stop, "Nothing was changed.")
                return
        _rework(agent, name, sentence, warnings=risks, changed=list(changes))

    if new_name:
        settled = agent.rename(name, new_name)
        if settled and not sentence:
            _redraw(agent, settled, ["name"] + list(changes))
    elif not sentence and changes:
        # Changes that cost no model call: a re-tune of a step already in the
        # -c stack rewrote the ini and left the command alone. Nothing has
        # redrawn the box, so it is redrawn here -- the mirror reads the
        # override summary off the file, so the new walltime is visible without
        # a regeneration that had nothing to regenerate.
        _redraw(agent, name, list(changes))


def _redraw(agent, name, changed):
    """Re-render the gate for a change that never reached the model."""
    record = agent.registry.get(name)
    if record and record.get("proposal"):
        agent.registry.update(name, changed=list(changed))
        display.gate(record["proposal"], name, blockers=agent._blockers(),
                     changed=changed,
                     resources=override.summary(override.read(
                         override.path_for(name, record.get("workdir") or ".",
                                           record["proposal"]))))


def _cmd_view(agent, args):
    """/view <name> -- the command a run is, and what can still be done to it.

    The gate already draws this. What it did not do was draw it on request: the
    box appeared when a run reached the gate and then scrolled away, so the
    command -- the thing every decision is actually about -- was only ever
    visible in the moment it arrived. /list answers "what runs are there" and
    /check answers "how is it doing"; neither answers "what IS it", and that is
    the question somebody has before they approve, modify or reject anything.

    Read-only, and works on any run in any status. The verbs underneath change
    with the status, because they have to: a submitted run cannot be approved
    again and its /modify is a fork -- see _cmd_modify.
    """
    if not args:
        held = agent.registry.held()
        if len(held) == 1:
            args = [held[0]["name"]]
        else:
            display.problem("usage: /view <name>",
                            "/list shows what there is.")
            return
    name = args[0]
    record = agent.registry.get(name)
    if record is None:
        display.problem(f"No run named '{name}'.", "/list shows what there is.")
        return
    proposal = record.get("proposal") or {}
    if not proposal:
        display.problem(f"'{name}' has no command on record.",
                        "It was adopted from a job list rather than built "
                        "here. /check tells you how it is doing.")
        return
    workdir = record.get("workdir") or "."
    display.run_view(
        proposal, name, record["status"],
        resources=override.summary(override.read(
            override.path_for(name, workdir, proposal))),
        blockers=agent._blockers() if record["status"] == runs_store.HELD
        else ())


def _cmd_check(agent, args):
    """/check <name> -- what the scheduler says about one run.
    /check all -- every run, grouped by what it needs from you.

    One command, two scopes. There was briefly a /status as well, whose
    single-run form was an exact alias for this one and whose all-runs form was
    the same query in a second layout. /check is the name everything else
    already uses -- the post-approve confirmation, /list's footer, the README
    and every test case print it -- so the alias went rather than the original.
    """
    if not args:
        display.problem("usage: /check <name>",
                        "/check all groups every run by what it needs.")
        return
    if args[0].lower() == "all" and agent.registry.get("all") is None:
        with ui.Activity("asking Slurm"):
            agent.check_all()
        return
    if args[0].lower() == "all":
        display.nothing("There is a run actually named 'all'.",
                        "Rename it with /modify all, or /check it by name.")
    with ui.Activity("asking Slurm"):
        agent.check(args[0])


def _cmd_monitor(agent, args):
    """/monitor <name> [seconds] -- watch one run until it stops changing.

    A poll loop rather than anything clever. The interval is generous by
    default: sacct is cheap but a login node is shared, and a monitor that
    hammers the scheduler every second is the kind of thing that gets a tool
    banned from a cluster.

    Ctrl+C stops watching. It does not stop the run, and it says so.
    """
    if not args:
        display.problem("usage: /monitor <name> [seconds between checks]")
        return
    name = args[0]
    try:
        every = max(10, int(args[1])) if len(args) > 1 else 30
    except ValueError:
        every = 30
    record = agent._need_run(name)
    if record is None:
        return
    print(f"\n  {display.DIM}watching {display.RESET}{display.WHITE}{name}"
          f"{display.RESET}{display.DIM} every {every}s  ·  Ctrl+C to stop "
          f"watching (the run keeps going){display.RESET}")
    last = None
    try:
        while True:
            status = runs_store.resolve(record)
            signature = (tuple(sorted(status.counts.items())), status.verdict)
            if signature != last:
                display.run_status(name, status)
                agent.registry.remember_check(name, status.counts, status.total,
                                              status.verdict)
                agent.registry.remember_reasons(name, status.reasons)
                last = signature
            if status.finished or status.source == "unavailable":
                break
            time.sleep(every)
    except KeyboardInterrupt:
        print(f"\n  {display.DIM}Stopped watching. {name} is still on the "
              f"scheduler.{display.RESET}\n")
        return
    display.done(f"{name} · {last[1] if last else 'finished'}",
                 f"/jobs {name} for the detail")


def _cmd_hold(agent, args):
    """/hold <name> -- put a submitted run's queued jobs on hold in Slurm.

    The counterpart to /cancel that does not destroy anything. `scontrol hold`
    stops queued jobs being scheduled while leaving them in the queue, so a run
    you have doubts about can be stopped from consuming more allocation without
    losing its place or its dependencies. `/hold <name> release` puts it back.

    Only pending jobs are touched: a running job cannot be held, and asking
    Slurm to hold one produces an error per job, which is a wall of noise at
    exactly the moment somebody needs to know whether it worked.
    """
    if not args:
        display.problem("usage: /hold <name> [release]")
        return
    name = args[0]
    release = len(args) > 1 and args[1].lower().startswith("rel")
    record = agent._need_run(name)
    if record is None:
        return
    jobs = runs_store.jobs_for(record)
    targets = [j.job_id for j in jobs if j.job_id and j.state == "PENDING"]
    if not targets:
        display.nothing(f"Nothing queued in '{name}' to "
                        f"{'release' if release else 'hold'}.",
                        f"/check {name} shows where it stands.")
        return
    verb = "release" if release else "hold"
    with ui.Activity(f"{verb}ing"):
        raw, _ = runs_store._run(f"scontrol {verb} {','.join(targets)}")
    display.done(f"{len(targets)} queued job(s) {verb}d in {name}.",
                 f"/hold {name} release" if not release else f"/check {name}")
    if raw.strip():
        print(f"  {display.DIM}{raw.strip()}{display.RESET}\n")


def _cmd_sort(agent, args):
    """/sort -- prune what /list shows.

    A registry that has been running for a fortnight has rows in it nobody is
    acting on any more, and /list stops being read the moment it stops being
    short. This hides rows; it never deletes them -- the reason to clear a row
    is almost always that it is old rather than that it is wrong, and /history
    still has to be able to answer "what did I run in June?".
    """
    # `show` was advertised in this command's own hint line and had nothing
    # behind it -- it fell through to the name branch below and came back as
    # "No run named 'show'". A hidden row you cannot bring back is a deletion,
    # which is the one thing this command promises not to be.
    if args and args[0].lower() in ("show", "all", "unhide", "restore"):
        rest = args[1:]
        buried = [r for r in agent.registry.all(prune=False) if r.get("hidden")]
        if not buried:
            display.nothing("Nothing is hidden from /list.")
            return
        names = rest or [r["name"] for r in buried]
        back = [n for n in names if agent.registry.get(n)]
        for name in back:
            agent.registry.hide(name, hidden=False)
        if back:
            display.done(f"Back in /list: {', '.join(back)}", "/list to see them")
        for name in (n for n in names if n not in back):
            display.problem(f"No run named '{name}'.")
        return

    records = agent.registry.live()
    if not records:
        display.nothing("Nothing in /list to sort.",
                        "/sort show brings back anything already hidden")
        return
    if args:
        # Named directly: /sort chipseq-0728 rnaseq-0726
        hidden = [n for n in args if agent.registry.get(n)]
        missing = [n for n in args if not agent.registry.get(n)]
        for name in hidden:
            agent.registry.hide(name)
        if hidden:
            display.done(f"Hidden from /list: {', '.join(hidden)}",
                         "/history still has them  ·  /sort show to bring them back")
        for name in missing:
            display.problem(f"No run named '{name}'.")
        return

    options = [slots.Option(r["name"], r["name"], _run_note(r)) for r in records]
    options.append(slots.Option("__track__", "add a run instead",
                                "/track or /scan adopts one that already exists"))
    # "as many as you like" is the part the key hint at the foot of the panel
    # cannot carry on its own: "space toggles" tells you which key, not that
    # this list is one you tick rather than one you pick a single row from.
    picked = ui.choose("Which rows should /list stop showing?", options,
                       free_text=False, multi=True,
                       note="tick as many as you like — nothing is deleted; "
                            "/sort show brings rows back")
    if not picked:
        display.nothing("Left alone.")
        return
    if "__track__" in picked:
        display.nothing("Use /scan <path> to find runs on disk, or "
                        "/track <name> <job_list> for one you already know.")
        picked = [p for p in picked if p != "__track__"]
    for name in picked:
        agent.registry.hide(name)
    if picked:
        display.done(f"Hidden from /list: {', '.join(picked)}",
                     "/history still has them")


def _cmd_scan(agent, args):
    """/scan [path] -- find GenPipes runs already on disk and adopt them.

    Read-only, and it asks before it takes anything. The scan itself is
    deterministic local code that reads filenames and directory structure --
    never a FASTQ, a BAM, a VCF, a result table or a readset's contents.
    """
    if args:
        root = " ".join(args)
    else:
        try:
            root = ui.ask("Which directory should I scan?", default=os.getcwd())
        except (EOFError, KeyboardInterrupt):
            return
    if not root:
        return
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(root):
        display.problem(f"'{root}' is not a directory.")
        return
    with ui.Activity("scanning"):
        found = agent.scan(root)
    if not found:
        return

    options = []
    for entry in found:
        what = " ".join(x for x in (entry.get("pipeline") or "unknown",
                                    entry.get("protocol") or "") if x)
        options.append(slots.Option(entry["name"], entry["name"],
                                    f"{what}   {display._tilde(entry['workdir'])}"))
    picked = ui.choose("Which of these should I add?", options, free_text=False,
                       multi=True, note="nothing on disk is changed either way")
    if not picked:
        display.nothing("Nothing added.")
        return
    agent.scan(root, chosen=picked)


def _cmd_readset(agent, args):
    """/readset [directory] -- build a readset file from what is on disk.

    Filenames only. The pairing is done on `_R1`/`_R2` and the sample names are
    derived from the stems -- nothing here opens a FASTQ, which is what makes it
    safe to point at a directory of somebody else's data.

    `/readset schema [pipeline]` prints the format instead, which is the version
    of this you can send to a colleague: it is enough to write and review every
    piece of code that touches a readset, and it contains no sample name, no
    path and nothing real.
    """
    if args and args[0].lower() in ("schema", "spec", "format"):
        pipeline = args[1] if len(args) > 1 else None
        print()
        for line in readset.schema_text(pipeline).splitlines():
            print(f"  {display.DIM}{line}{display.RESET}" if line.startswith("  ")
                  else f"  {line}")
        print()
        return

    directory = " ".join(args) if args else os.getcwd()
    directory = os.path.abspath(os.path.expanduser(directory))
    if not os.path.isdir(directory):
        display.problem(f"'{directory}' is not a directory.")
        return

    pipeline = None
    held = agent.registry.held()
    if held:
        pipeline = ((held[-1].get("proposal") or {}).get("slots") or {}).get("pipeline")
    with ui.Activity("reading filenames"):
        rows, warnings = readset.from_directory(directory, pipeline=pipeline)
    if not rows:
        display.problem(f"No FASTQ files in {display._tilde(directory)}.",
                        "/readset <directory> to look somewhere else.")
        return

    print()
    print(f"  {display.DIM}▌{display.RESET} {display.BOLD}{len(rows)} readset(s)"
          f"{display.RESET}  {display.DIM}·  "
          f"{len({r['Sample'] for r in rows})} sample(s)  ·  from filenames "
          f"only{display.RESET}")
    print()
    for line in readset.render(rows, pipeline).splitlines()[:12]:
        print(f"  {display.DIM}{line}{display.RESET}")
    if len(rows) > 11:
        print(f"  {display.GREY}… {len(rows) - 11} more{display.RESET}")
    print()
    for warning in warnings:
        print(f"  {display.AMBER}▌{display.RESET} {display.DIM}{warning}"
              f"{display.RESET}")
    if warnings:
        print()

    try:
        path = ui.ask("Write it where? (blank to not write)",
                      default=os.path.join(directory, "readset.tsv"))
    except (EOFError, KeyboardInterrupt):
        return
    if not path:
        display.nothing("Not written.")
        return
    try:
        readset.write(path, rows, pipeline)
    except FileExistsError:
        display.problem(f"'{path}' already exists.",
                        "A readset file is hand-corrected after it is generated "
                        "— overwriting one destroys those edits.")
        return
    except OSError as e:
        display.problem(f"Could not write it: {e.strerror or e}")
        return
    display.done(f"Wrote {display._tilde(path)}",
                 "Check the Sample column before using it — lanes of one sample "
                 "must share a name.")


def _cmd_verbose(agent, args):
    """/verbose -- show the agent's working, including what already scrolled by.

    The transcript folds away the commands, the machine output and the
    connective prose by default, the way a chain of thought is folded away. This
    unfolds it, and replays what has already happened -- a fold you can only
    open going forward is not much of a fold.

    Bare /verbose FLIPS it. A command whose whole job is one setting with two
    states, typed with no argument, can only sensibly mean "the other one" --
    and it used to mean "on" unconditionally, so typing it while the working
    was already showing did nothing and said so in three lines. `on` and `off`
    still work for anyone who would rather say it than count states.

    One confirmation, whichever way it went. The replay used to announce itself
    with a header, and then a second block announced the setting, and a third
    case announced that there had been nothing to replay -- three messages for
    one keystroke.
    """
    word = args[0].lower() if args else ""
    if word in ("off", "no", "hide", "quiet"):
        wanted = False
    elif word in ("on", "yes", "show"):
        wanted = True
    else:
        wanted = not display.VERBOSE
    display.set_verbose(wanted)
    if not wanted:
        display.done("Working folded away.", "/verbose to show it")
        return
    # Replayed first, so the confirmation lands nearest the prompt -- under the
    # work it is describing rather than above a screen of it.
    replayed = display.replay()
    display.done(
        "Showing the agent's working."
        + (f"  {replayed} step{'s' if replayed != 1 else ''} replayed." if replayed else ""),
        "/verbose to fold it away")


def _cmd_jobs(agent, args):
    """/jobs <name> [failed] -- the individual Slurm jobs inside a run."""
    if not args:
        display.problem("usage: /jobs <name> [failed]")
        return
    only_failed = len(args) > 1 and args[1].lower().startswith("fail")
    with ui.Activity("asking Slurm"):
        agent.jobs(args[0], only_failed=only_failed)


def _cmd_diagnose(agent, args):
    """/diagnose <name> [question...] -- diagnose a failed run."""
    if not args:
        display.problem("usage: /diagnose <name> [question...]")
        return
    name, *rest = args
    with ui.Activity("finding what failed") as act:
        agent.diagnose(name, question=" ".join(rest) or None, on_step=_narrate(act))


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
    here = preflight.cluster()
    display.where([
        ("cluster", f"{here}  ({preflight.cluster_ini()})" if here
                    else "not a recognised Alliance login node"),
        ("launched from", os.getcwd()),
        ("agent workdir", agent.path),
        ("run registry", agent.registry.path),
        ("checkpoints", os.path.join(agent.path, "genpipe_checkpoints.sqlite")),
        ("this copy", ROOT),
    ])


def _cmd_telemetry(agent, args):
    """/telemetry -- counts and timings from the generate/execute loop, when
    GENPIPE_TELEMETRY=1 turned them on. Off by default; see genpipe/telemetry.py."""
    if not agent.telemetry.enabled:
        display.nothing("Telemetry is off.",
                        "Set GENPIPE_TELEMETRY=1 before starting the app to "
                        "record generate/execute/checkpoint timings.")
        return
    summary = agent.telemetry.summary()
    if not summary:
        display.nothing("Nothing recorded yet this session.")
        return
    rows = [(kind, f"{row['count']} call(s), {row['total']:.2f}s total, "
                   f"{row['mean']:.2f}s mean")
            for kind, row in sorted(summary.items())]
    display.where(rows)


def _cmd_help(agent, args):
    display.help_text(help_rows())


# One table, three consumers: the dispatcher below, the completion menu the
# prompt draws as you type, and /help. Adding a command here is the whole job --
# there is nowhere else for the three to disagree.
#
# The group column exists only for /help, which is now long enough that a flat
# alphabetical list stops being a reference. The groups follow the order things
# happen in, so the workflow is legible from the help itself.
#
# /exit and /new have no handler because both act on the loop's own state --
# breaking out of it, and swapping the conversation thread -- rather than on the
# agent. They are listed anyway so they show up in the menu and in /help like
# everything else.
COMMAND_SPECS = [
    ("new",      "",                   "start a fresh conversation",              "talking",  None),
    ("verbose",  "[off]",              "show or fold away the agent's working",   "talking",  _cmd_verbose),
    ("approve",  "<name>",             "let a held submission through to Slurm",  "deciding", _cmd_approve),
    ("modify",   "<name> [change]",    "rewrite a held run and ask again",        "deciding", _cmd_modify),
    ("reject",   "<name> [why...]",    "abandon a held run; nothing submitted",   "deciding", _cmd_reject),
    ("view",     "<name>",             "the command a run is, and what it takes", "watching", _cmd_view),
    ("list",     "",                   "runs awaiting approval, and live ones",   "watching", _cmd_list),
    ("check",    "<name>|all",         "how a run is doing; all groups them",     "watching", _cmd_check),
    ("monitor",  "<name> [seconds]",   "watch one run until it stops changing",   "watching", _cmd_monitor),
    ("jobs",     "<name> [failed]",    "the individual jobs inside a run",        "watching", _cmd_jobs),
    ("history",  "",                   "every run recorded, live or gone",        "watching", _cmd_history),
    # One verb, not two. /why and /diagnose were the same function under two
    # names, and the only thing the pair achieved was making people wonder which
    # to reach for -- the two answers differed by model variance, never by
    # design. /check already gives the quick why, in its root cause block, for
    # free and with no model call. This is the one that reads the logs.
    ("diagnose", "<name> [question]",  "read the logs and explain a failure",     "fixing",   _cmd_diagnose),
    ("hold",     "<name> [release]",   "stop a run's queued jobs being scheduled", "fixing",  _cmd_hold),
    ("cancel",   "<name>",             "scancel a run's remaining jobs",          "fixing",   _cmd_cancel),
    ("sort",     "[names...|show]",    "tick rows to hide from /list; show undoes", "fixing", _cmd_sort),
    ("scan",     "[path]",             "find GenPipes runs already on disk",      "fixing",   _cmd_scan),
    ("track",    "<name> <job_list>",  "adopt a run launched outside the agent",  "fixing",   _cmd_track),
    ("readset",  "[dir|schema]",       "build a readset file from filenames",     "setup",    _cmd_readset),
    ("where",    "",                   "which directories this is using",         "setup",    _cmd_where),
    ("telemetry", "",                  "generate/execute/checkpoint timings",     "setup",    _cmd_telemetry),
    ("user",     "[name]",             "show or change what it calls you",        "setup",    _cmd_user),
    ("model",    "[provider [model]]", "show or switch the model behind this",    "setup",    _cmd_model),
    ("key",      "",                   "add or rotate an API key",                "setup",    _cmd_key),
    ("help",     "",                   "this list",                               "setup",    _cmd_help),
    ("exit",     "",                   "leave",                                   "setup",   None),
]

COMMANDS = {name: fn for name, _, _, _, fn in COMMAND_SPECS if fn}


def _specs_now():
    """COMMAND_SPECS with the state-dependent rows filled in for right now.

    Just /verbose so far. Its row was written as a fixed "[off]", which is a
    label for one of the two states it can be in and is therefore wrong half
    the time -- it read as "off" while the working was already being shown. A
    toggle has to say which way it will flip and where it currently stands, so
    both halves of the row are rewritten from the live setting.

    The argument column is empty now, because bare /verbose flips it and there
    is nothing to type. `on` and `off` still parse; they are just no longer the
    thing the menu teaches, since offering an argument for a toggle invites
    somebody to work out which one they need before pressing a key.
    """
    out = []
    for name, args, desc, group, fn in COMMAND_SPECS:
        if name == "verbose":
            args = ""
            desc = ("fold the agent's working away  ·  now: showing"
                    if display.VERBOSE else
                    "show the agent's working  ·  now: folded away")
        out.append((name, args, desc, group, fn))
    return out


def menu():
    """Rows for the completion menu the prompt draws as you type."""
    return [(name, args, desc) for name, args, desc, _, _ in _specs_now()]


def help_rows():
    """Rows for /help."""
    return [(name, args, desc, group) for name, args, desc, group, _ in _specs_now()]


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


def _panel(gap):
    """Render one gap as a question. The seam the agent's ask() arrives through.

    A panel with nothing to offer is worse than a plain question: it costs a
    keystroke to reach the free-text row and implies there were alternatives
    worth reading. So when there is nothing to choose between -- a free-form
    question, or a file role with nothing matching on disk -- this degrades to a
    one-line prompt with the note as its hint.
    """
    if not gap.options:
        if gap.note:
            print(f"  {display.DIM}{gap.note}{display.RESET}")
        try:
            answer = ui.ask(gap.question)
        except (EOFError, KeyboardInterrupt):
            return None
        return answer.strip() or None
    return ui.choose(gap.question, gap.options,
                     note=gap.note, free_text=gap.free_text)


def _dispatch_line(agent, line):
    """Run a slash command. One implementation, shared by every caller that
    needs to dispatch a typed command."""
    parts = line[1:].split()
    if not parts:
        return
    cmd, args = _resolve(parts[0].lower()), parts[1:]
    handler = COMMANDS.get(cmd)
    if handler is None:
        print(f"  {display.RED}No such command: {line.split()[0]}{display.RESET}"
              f"  {display.GREY}(try /help){display.RESET}\n")
        return
    try:
        handler(agent, args)
    except KeyboardInterrupt:
        print()
        display.nothing("Stopped.")
    except Exception as e:
        print(f"  {display.RED}{type(e).__name__}: {e}{display.RESET}\n")


def _asker(activity):
    """Wrap the panel so the spinner gets out of its way and comes back after.

    Activity paints on the same line the panel is about to draw over, so
    without this the question appears with a spinner ticking through it.
    """
    def ask(gap):
        activity.pause()
        try:
            return _panel(gap)
        finally:
            activity.resume()
    return ask


def _conversation_id():
    """A fresh conversation key. Not shown to anyone: runs get real names at
    the gate, and this only has to be unique in the checkpoint database."""
    return f"chat-{datetime.datetime.now():%m%d-%H%M%S}"


def _at_the_gate(agent, thread, line):
    """Handle a line typed while this conversation is parked at the gate.

    Returns True when the line was dealt with here. Exactly one thing is decided
    here rather than by the model, and it is the safety property: an
    approval-shaped line is REFUSED, with the command that would work. Zero
    model calls. Approval is typed, never inferred -- no prose may ever cause a
    submission, and a helpful assistant that reads "looks good" as consent is
    the exact failure this gate exists to prevent.

    Everything else goes to the agent as feedback and is READ BY THE MODEL.
    This used to classify first and send every non-approval down /modify, which
    is why "why did you propose the submission gate without asking me for
    additional info" came back as a refusal to guess which run was meant: a
    question was treated as an edit, and then abandoned for ambiguity that did
    not exist. The thread names the run -- held_for_thread returned it -- so
    there is nothing to disambiguate, and whether a line is a question, a
    change, or neither is a reading task, not a keyword test. The prompt tells
    the model to answer a question and then re-propose the same submission, and
    agent.resume() re-holds the run if it does not.

    What this must never do is call agent.run(). A held conversation cannot take
    a normal turn: LangGraph starts a fresh superstep and DISCARDS the pending
    interrupt, destroying an approval that is still outstanding. That failure is
    silent -- it looks exactly like it worked. Resuming is not that: it delivers
    the line into the graph exactly where it stopped.
    """
    waiting = agent.registry.held_for_thread(thread)
    if not waiting:
        return False
    name = waiting["name"]

    if modify.is_approval_shaped(line):
        display.problem(
            "Approval has to be typed as a command.",
            f"/approve {name}   ·   nothing has reached the scheduler")
        return True

    _rework(agent, name, line)
    return True


def _turn(agent, thread, text, raw=None, label="thinking"):
    """One exchange: the user's line in, the agent's work out.

    A conversation parked at the gate does not take a normal turn -- see
    _at_the_gate for what happens instead and why it must not reach agent.run().

    Ctrl+C interrupts the AGENT, not the session. It abandons this answer and
    returns to the prompt with the conversation intact, which is what people
    expect from every other assistant with a spinner in it: stopping a reply is
    not the same as leaving. Nothing that was on its way to the scheduler can
    survive it, because nothing reaches the scheduler without /approve.
    """
    if _at_the_gate(agent, thread, raw if raw is not None else text):
        return
    try:
        with ui.Activity(label) as act:
            agent.on_ask = _asker(act)
            agent.run(text, thread_id=thread, on_step=_narrate(act))
    except KeyboardInterrupt:
        print()
        display.nothing("Stopped.",
                        "Nothing reached the scheduler. The conversation is "
                        "still here — say something else, or /new to start over.")
    except Exception as e:
        display.problem(f"{type(e).__name__}: {e}")
    finally:
        agent.on_ask = None


# Commands whose first argument is the name of an existing run. The two groups
# differ in what is worth offering: a decision applies only to a run that is
# waiting for one, and everything else applies only to a run that has actually
# been submitted. Offering the wrong set is how you end up typing /check on a run
# that has not run.
_DECIDE = ("approve", "reject")
_WATCH = ("check", "jobs", "diagnose", "cancel", "monitor", "hold")
# Commands that work on a run in ANY state. /modify used to be in _DECIDE and
# so completed only from held runs, which was right when a run that was not
# held could not be modified at all. It forks one now -- see _cmd_modify -- and
# a completion list that still hid every finished run would hide exactly the
# runs somebody reaches for when they want to run something again.
_EITHER = ("modify", "view")


def _provider_names():
    """The providers /model accepts, each noted with the model it defaults to.

    Providers can be completed and models cannot, and that asymmetry is
    Biomni's rather than ours. get_llm() checks `source` against a fixed set
    (biomni/llm.py's ALLOWED_SOURCES) and dispatches on it; `model` it never
    inspects at all, just hands to the provider's client as an opaque string.
    So a menu of providers is exhaustive and stays correct, while a menu of
    model names could only ever be a hand-kept guess -- stale the week a
    provider ships something, and wrong in the direction that matters, since
    it would imply the models NOT listed are unavailable when any string the
    provider recognizes works.

    Hence: complete the closed set, and leave the open one to be typed. The
    default model rides along in the note so that `/model Anthropic` with no
    second word is a visible choice rather than a silent one.

    KNOWN_PROVIDERS is four of Biomni's eight on purpose -- see its own
    comment: the other four need an endpoint or ambient cloud credentials
    rather than a pasted key, so /model cannot reach them and must not offer
    them.
    """
    here = os.environ.get("GENPIPE_LLM_SOURCE", DEFAULT_SOURCE)
    running = os.environ.get("GENPIPE_LLM_MODEL", DEFAULT_MODEL)
    out = []
    for _, source, env_var, default_model in KNOWN_PROVIDERS:
        if _looks_like_placeholder(os.environ.get(env_var, "")):
            # Offered, not hidden, and marked. Hiding a provider you have no
            # key for answers "why is OpenAI missing?" with silence; saying so
            # here names the command that fixes it.
            note = f"{default_model}  ·  no key yet, /key first"
        elif source == here:
            # The model actually loaded, not this provider's default. They
            # differ the moment anyone types a second word, and "current" beside
            # a model you are not running is the one note here that could lie.
            note = f"{running}  ·  current"
        else:
            note = default_model
        out.append((source, note))
    return out


def _run_names(agent, command):
    """Values to complete the first argument of `command` with, or None.

    None means "this command's argument is neither a run name nor anything
    else we can enumerate" -- /track's first argument is a name being
    invented. /model's is a provider, which is a closed set, so it completes
    from _provider_names(). An empty list means it IS a run name and there are
    none, which the prompt says out loud rather than silently offering
    nothing.
    """
    # Before the try: this reads the environment, not the registry, and a
    # broken registry has no bearing on whether the provider list is right.
    if command == "model":
        return _provider_names()
    try:
        if command in _DECIDE:
            records = agent.registry.held()
        elif command in _EITHER:
            # Held first, whatever the registry's order, because a run waiting
            # on a decision is the one being reached for far more often than a
            # finished one being copied. The rest follow newest-first below.
            live = agent.registry.live()
            records = ([r for r in live if r["status"] == runs_store.HELD]
                       + list(reversed([r for r in live
                                        if r["status"] != runs_store.HELD])))
            return [(r["name"], _run_note(r)) for r in records]
        elif command in _WATCH:
            records = [r for r in agent.registry.live()
                       if r["status"] != runs_store.HELD]
        else:
            return None
    except Exception:
        return []          # a broken registry must not break the input line
    # Newest first, because the registry is append-only and therefore oldest
    # first, and the run you mean is almost always the one you just made. It is
    # also the row Enter takes when you have not moved the selection.
    return [(r["name"], _run_note(r)) for r in reversed(records)]


def _run_note(record):
    """One phrase saying what this run is, for the completion menu."""
    slots = (record.get("proposal") or {}).get("slots") or {}
    what = " ".join(str(slots[k]) for k in ("pipeline", "protocol")
                    if slots.get(k)) or (record.get("proposal") or {}).get("command", "")
    check = record.get("last_check") or {}
    return f"{what}  {check.get('verdict', '')}".strip()


# _learned_files() lived here. It pulled a readset, design or pairs file out of
# a sentence and remembered it, keeping only names that resolved on disk -- the
# disk check being sound, and the remembering being the problem. A filename in
# "should I use readset_a.tsv or readset_b.tsv?" resolves perfectly well and was
# recorded as the answer to a question the person was still asking. It went with
# the rest of _settled()'s fact list; the model reads the sentence.


def _briefed(line, already_sent, directory=None):
    """The line to send the agent, plus the directory brief that went with it.

    Every turn is briefed now, not just the opening one, and the change came from
    a real failure. The request "run rnaseq_light on readset.rnaseq.txt" arrived
    third in a conversation, so it carried no brief; the model had never been told
    what was in the project directory, took the filename on trust, and got
    `can't open 'readset.rnaseq.txt'` back. Its next move was `find / -iname
    readset.rnaseq.txt`, which is the wrong answer to the right question.

    The original reason for briefing only once was that repeating a list of
    filenames every turn keeps putting stale paths in front of the model. So the
    brief is deduplicated instead of rationed: an unchanged context block is
    dropped and only the line is sent, and a readset that appears in the directory
    an hour into the conversation is mentioned once, when it appears.

    `directory` is the project directory established so far in this
    conversation (`prep.Preparation.directory`), or None. Never the process's
    cwd -- intake.brief() discovers nothing when it has nowhere established to
    look, rather than describing whatever happens to be on the floor where the
    app was launched (AGENT-FIXES.md defect 1).
    """
    briefed = intake.brief(line, directory)
    if intake.CONTEXT_MARK not in briefed:
        return line, already_sent
    context = briefed.split(intake.CONTEXT_MARK, 1)[1].strip()
    if context == (already_sent or ""):
        return line, already_sent
    return briefed, context


def _repl(agent):
    """The application's actual interface: a conversation, with commands in it.

    Bare text is talk. It goes to the agent on one continuous thread, so the
    third thing you say lands after the first two rather than in front of a
    blank agent -- ask a question, get an answer, refine it, and only then build
    a run. The agent asks its own questions along the way, through _panel.

    Anything starting with / is a command. Those exist because a decision about
    the cluster should never be inferred from prose: /approve is typed, never
    guessed at.

    One conversation can produce any number of runs. Each is named at the gate
    and lives on under that name -- /list, /check and /diagnose are about runs, and
    outlive the conversation that started them.

    The prompt is created once and kept, so history survives across turns.
    """
    prompt = ui.Prompt(menu, arguments=lambda cmd: _run_names(agent, cmd))
    thread = _conversation_id()
    context = None              # the last directory brief sent on this thread
    preparation = prep.Preparation()

    while True:
        try:
            line = prompt.read().strip()
        except EOFError:
            break
        except KeyboardInterrupt:
            # Ctrl+C at an idle prompt clears the line and stays. Leaving is
            # Ctrl+D or /exit, and it has to be a different key from the one
            # that means "stop what you are doing" -- a single keystroke that
            # sometimes clears a line and sometimes ends the session is the
            # thing that made people afraid to press it.
            continue

        if not line:
            continue

        # Drawn here, the moment it is read, rather than when the message
        # streams back through the transcript. Two reasons: the input box has
        # just erased itself, so this is the only copy; and anything printed
        # about the line -- the "Preparing run…" heading in particular -- has to
        # come after it, which it cannot if the echo waits for the graph.
        display.echo(line)
        # A new turn is a new job. Without this the next checklist would be
        # repainted onto the last one's lines, over the top of the answer that
        # sits between them.
        display.reset_plan()

        if line.startswith("/"):
            parts = line[1:].split()
            if not parts:
                continue
            cmd = _resolve(parts[0].lower())
            if cmd in ("exit", "quit"):
                break
            if cmd == "new":
                thread = _conversation_id()
                context = None
                preparation = prep.Preparation()
                display.fresh(agent.pending())
                continue
            # Ctrl+C inside a command -- a panel escaped, a monitor stopped --
            # returns to the prompt rather than ending the session; that is
            # _dispatch_line's doing, and without it Ctrl+C would mean two
            # different things depending on when it was pressed.
            _dispatch_line(agent, line)
            continue

        try:
            # A conversation parked at the gate is not preparing anything: the
            # run is already built and this line is a decision about it. The
            # brief would be a wasted directory listing, and _at_the_gate --
            # reached through _turn -- resumes the graph rather than starting a
            # fresh one.
            if agent.registry.held_for_thread(thread):
                extra = None
                _turn(agent, thread, line, raw=line)
                continue
            preparation, extra = prep.track(preparation, line)
        except (EOFError, KeyboardInterrupt):
            print()
            continue
        except Exception as e:
            display.problem(f"{type(e).__name__}: {e}")
            extra = None

        try:
            text, context = _briefed(line, context, preparation.directory)
        except Exception as e:
            display.problem(f"{type(e).__name__}: {e}")
            text = line
        if extra:
            text = f"{text}\n\n{intake.CONTEXT_MARK}\n{extra}\n"

        _turn(agent, thread, text, raw=line)
        # A run reaching the gate is the end of preparing it: what was settled
        # belongs to that run, and carrying it into the next question would have
        # the following turn briefed with a finished run's readset.
        if agent.registry.held_for_thread(thread):
            preparation = prep.Preparation()


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
        from . import fakecluster
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
    # The CLI draws the person's own turns itself -- see _repl -- so the
    # transcript must not draw them a second time.
    display.ECHOED = True
    display.banner(source=os.environ.get("GENPIPE_LLM_SOURCE"),
                   model=os.environ.get("GENPIPE_LLM_MODEL"))
    # Who we are talking to. Asked before the key prompt because it is the
    # cheaper question of the two and the answer is used immediately -- it
    # labels their side of the conversation from the first line on.
    _require_name()
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
    # The readiness line printed here, and does not any more: it existed
    # because the prompt used to follow a banner and nothing else, and looked
    # like a dead end. welcome() is the last thing before the prompt now, and
    # the banner already states which model is configured.
    #
    # What it also carried is the part with teeth, and that stays -- see below
    # welcome(), which is where it goes now.
    #
    # The startup status block used to print here -- held runs, and runs whose
    # outcome nobody had looked at. It is gone from the opening screen: the
    # counts were of accumulated testing rather than of anything anybody was
    # about to act on, and they sat between the banner and the prompt, which is
    # the most expensive space on the screen. /list and /check all answer both
    # questions on demand, and the welcome block below names them.
    #
    # display.pending still exists and is still tested; nothing calls it at
    # startup. The visit is still recorded, so "unseen since you were last
    # here" stays meaningful for whatever asks later.
    agent.registry.mark_seen()
    # Runs left mid-submission by a session that never came back. Almost always
    # nothing, and silent when it is -- but this is the one startup check that
    # can be holding news about work already on the cluster, so it runs before
    # the prompt rather than waiting for somebody to type /list. It reads three
    # things off disk and writes a status; it never retries anything.
    agent.reconcile_stale()
    # Environment problems that only surface at submit time, surfaced now --
    # blockers only.
    #
    # A warning is shown nowhere any more, and that is deliberate. RAP_ID unset
    # means every job in the run is rejected by Slurm, so it earns a place on a
    # fresh screen and is re-checked at the gate besides. JOB_MAIL merely bounces
    # notifications, and reprinting the same amber line at every launch until the
    # day it is fixed is how a startup screen teaches you not to read it. The
    # check still exists (preflight.check), it is just not shouted.
    if not fake_cluster:
        display.environment([f for f in preflight.check() if f.blocking])
    # Last thing before the prompt, so the question it asks is the nearest line
    # above the cursor rather than three screens up behind the status lines.
    display.welcome()
    # Under the welcome block rather than above it. `notes` says the cluster,
    # or the model, or both, are simulated, and above the block it is the first
    # thing to scroll off a short terminal -- which is the one warning here
    # that must survive being ignored. A fake cluster is otherwise invisible:
    # a submission that touched nothing looks exactly like one that did.
    #
    # Outside welcome() because it is a fact about this session, not part of
    # the introduction, and it has to read the same on the hundredth launch as
    # on the first -- welcome() shortens itself once you have been introduced.
    display.simulated(" + ".join(notes) if notes else None)
    try:
        _repl(agent)
    except KeyboardInterrupt:
        print()
    display.farewell()

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
