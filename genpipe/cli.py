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
from . import metering
from . import modify
from . import override
from . import preflight
from . import prep
from . import readset
from . import relaunch
from . import runs as runs_store
from . import settings
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
# The settings file, via settings.py rather than recomputed here, so the module
# that READS it and the code that WRITES it cannot end up pointing at two
# different files -- and so GENPIPE_ENV_FILE moves both together.
ENV_PATH = settings.path()
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
    (plus which provider/model it's for) if not. True if it had to ask.

    The return value is what lets main() check a BRAND NEW key against the
    provider without checking an already-working one on every launch -- see
    _confirm_new_key.

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
            return False
    _prompt_for_api_key()
    return True


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

    #    And the requirements table, generated from slots.py rather than
    #    written into the document by hand.
    #
    #    The gate refuses a proposal that lacks a design or a pairs file, using
    #    that table; until now the model could not read it, so it was being
    #    judged against a constraint it had no way to have anticipated. Its
    #    only route to the knowledge was to propose something and be told.
    #
    #    Generated, in this process, from the same objects gaps() consults --
    #    so the facts the model is given and the check that enforces them are
    #    incapable of disagreeing. A copy pasted into genpipes.md would be a
    #    second source, and every second source in this project has eventually
    #    drifted from the first.
    #
    #    FACTS, NOT A PROCEDURE. See slots.requirements_note for what is
    #    deliberately left out of it: no step numbers, no ordering, no "ask for
    #    this next", nothing that would turn a reference table back into the
    #    deterministic questionnaire this project removed.
    content += "\n\n" + slots.requirements_note()

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

    #    Then put the meter in front of it. This is the seam the compiled graph
    #    reads on every step (see _switch_model below), which makes it the only
    #    place a cache breakpoint, the provider's token counts and the malformed
    #    -tag repair can all be reached -- biomni's generate node builds its own
    #    SystemMessage and parses its own response inside one call, so a wrapper
    #    around the node is too late for two of the three. See genpipe/metering.py.
    agent.llm = metering.Metered(agent.llm, agent.telemetry)

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
    # Re-wrapped, because the meter belongs to the session rather than to any
    # one model: switching provider mid-session must not silently switch off
    # caching and usage accounting for the rest of it.
    # getattr, because this function's job is switching models and it should
    # not acquire a hard dependency on an unrelated attribute to do it --
    # Metered's own telemetry argument is optional for the same reason.
    agent.llm = metering.Metered(candidate, getattr(agent, "telemetry", None))
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


def _confirm_new_key(agent):
    """Ask the provider whether the key just pasted actually works.

    THE ONE PLACE A KEY WAS NEVER CHECKED. /model probes before it switches and
    /key probes before it applies -- both because a provider only rejects a bad
    key when a request is actually made, and a client object constructed around
    one is indistinguishable from a client constructed around a good one. The
    first-launch path did neither: a typo, an expired key or a key pasted with
    half of it missing was masked, saved to .env, and reported as `Saved`, and
    the first sign of trouble was a provider error from inside the agent loop a
    turn later -- where it reads as a problem with the message.

    So the same probe runs here, once, and only after the prompt has actually
    fired. It costs one very short completion on a first launch and nothing on
    any launch after that.

    A rejection is not fatal and does not loop. The session is left standing
    with the two commands that can fix it named, because a person who has just
    mistyped a key may want to paste a different one OR pick a different
    provider, and guessing which is not this function's business.
    """
    source = os.environ.get("GENPIPE_LLM_SOURCE", DEFAULT_SOURCE)
    model = os.environ.get("GENPIPE_LLM_MODEL", DEFAULT_MODEL)
    state, detail = _probe_llm(agent.llm)
    if state == _MODEL_OK:
        return True
    if state == _MODEL_UNVERIFIED:
        # A rate limit or a DNS failure says nothing about the key. Saying so
        # and carrying on is the honest answer; refusing to start would strand
        # somebody behind a problem that is not theirs.
        print(f"  {display.GREY}Could not reach {source} to check the key "
              f"-- {detail}{display.RESET}\n")
        return True
    display.nothing(
        f"{source} rejected that key — {detail}.",
        "/key pastes another one · /model switches provider. "
        "Nothing else is affected.")
    return False


def _cmd_key(agent, args):
    """/key -- rotate or add a key. Reuses the exact first-launch prompt,
    then applies it immediately instead of waiting for a relaunch.

    THE CANCEL IS CAUGHT HERE, and that is the whole reason this is not a
    two-line function. _prompt_for_api_key() ends a cancelled prompt with
    SystemExit, from two places -- the key read and the provider chooser --
    and at startup that is exactly right: there is no key, nothing can be done
    without one, and carrying on would only reach the same failure further
    away.

    Reached from /key it is a different act. There is a working session
    behind this prompt, quite possibly with a proposal parked at the gate, and
    pressing Ctrl+C at a question is not a request to throw that away. It used
    to end the process and take the conversation with it.
    """
    try:
        _prompt_for_api_key()
    except SystemExit:
        display.nothing("No key changed.",
                        "The session, and anything held at the gate, are "
                        "untouched.")
        return
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


def _rework(agent, name, feedback, warnings=(), changed=(), declared=None):
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

    `declared` is the same change set restated as checkable claims about the
    command that comes back -- see modify.declaration. `changed` says which
    rows were asked about and is answered by diffing two proposals; this says
    what each of them was asked to BECOME and is answered by reading the one
    proposal that came back. The second survives a fork, where there is no
    earlier proposal under that name to diff against.
    """
    agent._gate_note = {"warnings": list(warnings or ()),
                        "changed": list(changed or ()),
                        # A LIST, like modify.declaration returns. This was
                        # dict(), which raised ValueError on every in-place
                        # /modify that changed a declarable row -- a declaration
                        # entry has three keys and dict() reads a sequence of
                        # pairs. The fork path never hit it because it stores
                        # the list unwrapped.
                        "declared": list(declared or ())}
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


def _step_risks(agent, name, change, wanted=None):
    """Tier 3, for a change that is actually about steps. Returns (risks, stop).

    `wanted` is the range when the caller KNOWS it -- the guided panel and the
    fork both do, because it came off a row. Prose does not, and reading a step
    range out of a sentence is modify.steps_meant's job: it requires the
    sentence to say "step"/"steps" or "-s", so a walltime of 24 hours is not
    read as step 24. See _STEPS_MEANT for why that scoping is load-bearing.

    Only fires when there is a range to check -- reading --help costs a
    subprocess and a module load, and paying that to validate a protocol swap
    would put a two-second pause in front of every modify.

    The protocol is passed through, and that is not a detail: `genpipes dnaseq
    --help` describes seven protocols whose step numbers all restart at 1 and
    whose ranges run from 1-14 to 1-39. Validating against the wrong one is
    worse than not validating at all.
    """
    wanted = wanted or modify.steps_meant(change)
    if not wanted:
        return [], None
    record = agent.registry.get(name) or {}
    values = (record.get("proposal") or {}).get("slots") or {}
    protocol = values.get("protocol") or slots.DEFAULTS.get(values.get("pipeline") or "")
    help_text = agent.step_help(values.get("pipeline"), protocol)
    if not help_text:
        return [], None
    return modify.step_risk(wanted, help_text, protocol)


def _cmd_modify(agent, args):
    """/modify <name> [what to change] -- change what this run will be.

    ONE MEANING, EVERYWHERE: replace this run's proposal. `/modify foo` is a
    person saying what foo should become, so foo becomes it. There is no
    trailing menu asking whether they meant it.

    That menu existed because a rework destroys the held interrupt, so applying
    changes and keeping the original apart were genuinely different acts. They
    still are -- but that is what a second VERB is for, not a confirmation
    screen on the first one. /fork keeps both. See _cmd_fork.

    A SUBMITTED RUN IS EDITED AND FORKED, not refused. Its name is still tied
    to a job list and to jobs that may still be on the scheduler, so it is not
    rewritten in place -- that stays forbidden for the reason abandon() and
    rename() give. What changes is who does the work: the panel opens on the
    submitted command, the edits are made there, and what comes out is a new run
    with the original untouched.

    This replaces a redirect to /fork, and the redirect was the wrong trade. It
    was defended as costing one keystroke and buying /modify a single meaning,
    but the meaning it bought was the wrong one -- /modify meant "rewrite this
    record" when what a person means by it is "change this run". Both readings
    produce a run with the change in it; only one of them makes the commonest
    thing anybody does with a finished run -- run it again with one thing
    different -- into an error message plus a re-typed command. The single
    meaning survives intact and is better stated: /modify gives you a run with
    your change in it. Whether that is this run or a copy is decided by whether
    this one can still be rewritten, which is a fact about the run and not a
    decision to hand back.

    The name is asked at the END here, not up front as /fork asks it. That is
    the difference between the two verbs now: /fork is "make me a copy", so the
    name is the first thing you know and the collision has to be visible before
    Enter; /modify is "change this", where the copy is a consequence of the
    run's state, and being asked to name a variant before deciding what makes it
    different is a question nobody can answer yet.
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
        # Said BEFORE the panel opens, not after the fork exists. Somebody
        # typing /modify on a launched run is asking to change that run, and
        # what they are about to get is a different one -- announcing it first
        # turns a surprise into an answer, and it is the honest framing anyway:
        # the original is not being edited, it is being copied from.
        status = runs_store.resolve(record) if record.get("job_list") else None
        display.forking_from(name, runs_store.list_tag(record, status))
        if rest:
            _fork_prose(agent, name, record, " ".join(rest))
        else:
            _modify_guided(agent, name, record, fork_as=True)
        return

    if rest:
        _modify_direct(agent, name, " ".join(rest))
        return
    _modify_guided(agent, name, record)


def _cmd_relaunch(agent, args):
    """/relaunch <name> -- prepare a retry of a failed run with its diagnosed fix.

    THE MISSING VERB. /diagnose ends by naming a section, a key and a value;
    the only way to act on that was /modify, which forks the run and then asks
    somebody to open the resources row and type all three back in from a screen
    they are now scrolling away from. The operation had a shape and no name, so
    the shape was performed by hand -- which is where a step name gets
    misspelled and an override silently does nothing.

    PREPARES. DOES NOT SUBMIT. This is the property the whole command is
    arranged around, and it is structural rather than promised: what this does
    is write a command, run the GENERATION -- which produces a script and puts
    nothing on a scheduler -- and stop at the same gate every other run stops
    at. There is no path from here to sbatch that does not go through a typed
    /approve. The review screen says so; so does this docstring, because the
    name is the one thing about the command that could be read as "run again".

    AND IT ASKS NO MODEL ANYTHING. Every field of the revision is known before
    this function starts: the config stack from the source command, the
    override path from relaunch.prepare, the step range from the generated
    facts. The first version handed all of that to the model as prose so it
    could type a command back, which cost 36 inference steps and 108 seconds
    and lost the `OUTDIR=` assignment its own `-o` depended on. relaunch.command
    writes it instead; agent.hold_prepared gates it. What did NOT come off with
    the model is the checking -- the declaration below is the same one the
    /modify panel makes, modify.realized still reads the regenerated command
    back, and a change that did not land still stops /approve.

    THE ORIGINAL IS NOT TOUCHED. Not its command, not its config stack, not its
    job list, not its submission record, not its job ids. It is read from and
    nothing else -- and relaunch.prepare() writes the override under the NEW
    run's name precisely so the parent's file cannot be edited by a retry.

    WHAT IT REFUSES, AND WHY IT REFUSES SO READILY. Every precondition lives in
    relaunch.plan() and comes back as a Refusal with a hint naming the command
    to reach for instead. The one that matters most is the first: no stored
    remediation means no retry, and no hidden /diagnose is run to obtain one.
    A command that quietly calls a model when its input is missing is a command
    nobody can predict the cost or the latency of, and the boundary between
    "explain this" and "act on that explanation" is worth more than the
    keystroke it saves.
    """
    if not args:
        name = _pick_relaunch(agent)
        if not name:
            return
    else:
        name = args[0]
    record = agent.registry.get(name)
    if record is None:
        display.problem(f"No run named '{name}'.", "/list shows what there is.")
        return

    # Resolved BEFORE planning, not cached from the diagnosis. The precondition
    # most likely to have changed since /diagnose drew its screen is whether
    # the run is still going -- see relaunch.plan, which refuses on active jobs
    # -- and answering it from a stored tally would be revalidating against the
    # state that raised the question.
    status = None
    if record.get("job_list"):
        with ui.Activity("checking the run"):
            status = runs_store.resolve(record)

    plan = relaunch.plan(record, status=status)
    if isinstance(plan, relaunch.Refusal):
        display.problem(plan.message, plan.hint)
        return

    proposal = record.get("proposal") or {}
    directory = record.get("workdir") or os.getcwd()
    new_name = agent.registry.unique_name(name)
    display.forking_from(name, runs_store.list_tag(record, status))

    path, changes, applied = relaunch.prepare(plan, record, new_name, directory)
    declared = relaunch.declaration(plan, record, changes, directory)

    # THE COMMAND IS WRITTEN HERE, not asked for. Every field of it was decided
    # before this line: the stack by relaunch.stack, the range by
    # relaunch.scope, the override path by relaunch.prepare. See
    # relaunch.command for what it keeps and what it refuses to invent, and the
    # module docstring for the 36 inference steps this replaces.
    built = relaunch.command(record, plan, path)
    if isinstance(built, relaunch.Refusal):
        display.problem(built.message, built.hint)
        return
    generated, script = built

    # What the conversation behind this revision knows. There is no
    # conversation -- nothing was asked of a model -- so the thread is seeded
    # with the request that would have been made and the command that answers
    # it, in the shape gate.generation_command() reads. Without it a later
    # /modify on the revision would reach the model with a change to make and
    # no command to make it to. Stated by the application, in the application's
    # own turn; nothing here is attributed to the model.
    request = modify.fork_sentence(proposal, dict(changes))
    seed = agent.prepared_transcript(request, generated) if request else None

    thread = f"{name}::retry-{datetime.datetime.now():%m%d%H%M%S}"
    with ui.Activity("building the retry") as act:
        _ = act
        held = agent.hold_prepared(
            new_name, generated, script, directory, thread,
            declared=declared, changed=list(changes), seed=seed)
    if not held:
        return
    agent.registry.derive(new_name, name, relaunch.REASON)
    display.prepared_retry(
        name, new_name, _standing(record), applied=applied,
        scope=(f"steps {plan.scope} — {plan.scope_from}" if plan.scope
               else ""),
        scope_from=plan.scope_from, uncertain=plan.uncertain,
        skipped=plan.skipped)


def _pick_relaunch(agent):
    """Bare /relaunch: which runs have a fix this can apply. Returns a name or None.

    DISCOVERY, because the alternative was a usage line. Somebody who has just
    read a diagnosis should not have to remember the exact name of the run it
    was about, and "usage: /relaunch <name>" answers a question nobody asked --
    they know the shape of the command, they wanted to know which run.

    THE LIST IS NOT A GUESS AT WHAT LOOKS RETRYABLE. Every row here went
    through relaunch.plan(), the same function /relaunch <name> goes through,
    so a run that is offered is a run that would be accepted. Failed runs with
    no diagnosis, diagnoses this program cannot write, runs still on the
    scheduler and runs that never launched are all absent -- not because
    anything here reasons about them, but because plan() refused them.

    NOTHING IS PREPARED BY LOOKING. Even with one candidate the name goes back
    to _cmd_relaunch to be planned again from scratch, against a freshly
    resolved status. That is the revalidation the picker cannot do for every
    row: a run whose jobs restarted between the list being drawn and a row
    being chosen is refused there, with the reason, rather than quietly
    retried.

    No model call anywhere in it.
    """
    found = relaunch.candidates(agent.registry.live())
    if not found:
        display.nothing(
            "No run has a diagnosed fix this can apply.",
            "/diagnose <name> on a failed run works one out, or /modify "
            "<name> to choose the changes yourself.")
        return None
    # _run_note is what the completion menu puts beside a name, and it is used
    # here for the same reason it is used there: it reads the CACHED verdict
    # rather than inventing one. list_tag() without a resolved status answers
    # "completed" for anything terminal, which on this screen would call two
    # failed runs finished.
    options = [slots.Option(record["name"], record["name"],
                            _relaunch_note(record, plan))
               for record, plan in found]
    return ui.choose("Which run should be retried?", options, free_text=False,
                     note="nothing is prepared until you pick one, and "
                          "nothing is submitted until you approve it")


def _cmd_fork(agent, args):
    """/fork <name> [new name] [what to change] -- build a second run from this one.

    The verb that lets /modify mean one thing. Keeping the original and
    replacing it are genuinely different acts -- a rework destroys the held
    interrupt, so there is no way back to the first proposal once the model has
    regenerated over it -- and that distinction deserves a verb rather than a
    confirmation screen appended to every edit.

    Works on a run in ANY state, which is the other half of the point: the
    commonest thing anybody wants from a finished run is to run it again
    against a different readset, and the original keeps its name, its job list
    and its jobs throughout.

    THE NAME IS ASKED FIRST here, unlike everything else in /modify. For a fork
    it is the one thing that must be decided, and the collision has to be
    visible before Enter rather than resolved silently by unique_name()
    afterwards.
    """
    if not args:
        display.problem("usage: /fork <name> [what to change]",
                        "/list shows what there is.")
        return
    name, *rest = args
    record = agent.registry.get(name)
    if record is None:
        display.problem(f"No run named '{name}'.", "/list shows what there is.")
        return
    proposal = record.get("proposal") or {}
    if not proposal.get("generated"):
        display.problem(
            f"'{name}' has no generation command on record.",
            "It was found on disk rather than built here, so there is nothing "
            "to copy. Describe the run you want instead.")
        return

    # The run's ACTUAL lifecycle state, not the raw registry status -- "live"
    # is reserved for a run that currently has queued or running jobs (see
    # runs.list_tag(), the same words /list tags its rows with).
    status = runs_store.resolve(record) if record.get("job_list") else None
    display.forking_from(name, runs_store.list_tag(record, status))

    try:
        wanted = ui.ask("What should the new run be called?",
                        default=agent.registry.unique_name(name))
    except (EOFError, KeyboardInterrupt):
        return
    verdict = modify.check("name", str(wanted or ""), proposal,
                           registry=agent.registry, forking=True)
    if not verdict:
        display.problem(verdict.message, f"'{name}' is unchanged.")
        return
    new_name = agent.registry.unique_name(str(wanted).strip())

    change = " ".join(rest)
    if change:
        _fork_prose(agent, name, record, change, wanted=new_name)
        return
    # No prose: pick the changes in the panel, then fork with them.
    _modify_guided(agent, name, record, fork_as=new_name)


def _standing(record):
    """How to describe the run a variant was made FROM, once the variant exists.

    "held" is true of a run still parked at the gate and of nothing else, and it
    was the only thing display.forked could say. Forking from a launched run is
    now the ordinary path (see _cmd_modify), so a confirmation reading "the
    original is unchanged and still held" would be reassuring somebody about a
    state their run left an hour ago -- and the whole reason to read that line
    is to check nothing happened to the original.

    The same words /list uses, via runs.list_tag: live, completed, needs
    attention, status unavailable.
    """
    if not record or record.get("status") == runs_store.HELD:
        return "held"
    status = runs_store.resolve(record) if record.get("job_list") else None
    return runs_store.list_tag(record, status)


def _fork_prose(agent, name, record, change, wanted=None):
    """Build a variant of `name` from a sentence, in its own conversation.

    The prose half of forking, shared by /fork and by a /modify on a run that
    has already been submitted. One function because they differ in exactly one
    thing -- whether the new name is already known -- and two copies of a model
    call that mints a run is two places for the thread id, the gate note or the
    name to drift.

    `wanted` is the name when the caller has already asked for it (/fork asks
    before opening anything). Absent, it is asked here, which is the /modify
    order: what the variant IS gets decided before what it is called.

    No `changed` rows on the gate note. A prose fork has no row list -- the
    person described the change in a sentence and the model decided which flags
    that touches -- so there is nothing to mark green, and guessing would put a
    tick beside a row that may not have moved.
    """
    proposal = record.get("proposal") or {}
    if not wanted:
        try:
            wanted = ui.ask("What should the new run be called?",
                            default=agent.registry.unique_name(name))
        except (EOFError, KeyboardInterrupt):
            return
    verdict = modify.check("name", str(wanted or ""), proposal,
                           registry=agent.registry)
    if not verdict:
        display.problem(verdict.message, f"'{name}' is unchanged.")
        return
    new_name = agent.registry.unique_name(str(wanted).strip())

    request = modify.fork_prose(proposal, change)
    if not request:
        display.problem("There is no generation command to copy from.",
                        "Describe the run you want instead.")
        return
    agent._gate_note = {"warnings": [], "changed": []}
    thread = f"{name}::variant-{datetime.datetime.now():%m%d%H%M%S}"
    with ui.Activity("building the variant") as act:
        agent.run(request, thread, on_step=_narrate(act), name=new_name)
    if agent.registry.get(new_name):
        agent.registry.derive(new_name, name, "fork")
    display.forked(name, new_name, _standing(record))


def _past_configs(agent, directory, widened=False):
    """The `Past run configs…` view: every trace GenPipes wrote, scanned now.

    Returns the chosen trace's path, or None.

    NOTHING IS REMEMBERED BETWEEN OPENINGS. Each call lists the directory and
    reads a few header lines from each trace it finds; nothing is cached, no
    index is written, and closing the view leaves no state behind. That is the
    whole reason this is a view rather than a slice of the candidate list: a
    project directory gains a trace every time a command is generated, so
    anything that held on to them would only grow.

    `widened` adds the other directories tracked runs live in. Bounded by the
    registry -- see runs.tracked_workdirs -- and never a walk: these are the
    directories this application already knows about because it recorded a run
    in each, and nothing here goes looking for more.
    """
    records = agent.registry.live(prune=False)
    places = [directory]
    if widened:
        places += [w for w in runs_store.tracked_workdirs(records)
                   if os.path.abspath(w) != os.path.abspath(directory or ".")]

    found, seen = [], set()
    for place in places:
        for trace in intake.traces(place):
            if trace["path"] in seen:
                continue
            seen.add(trace["path"])
            label, note = modify.trace_row(
                trace, runs_store.trace_owner(trace, records, place))
            found.append((trace, label, note))

    # WHERE EACH ONE CAME FROM, once the answer varies. Decided from the
    # directories that actually PRODUCED something rather than from the ones
    # that were searched -- widening to three tracked runs that turn out to hold
    # no traces still yields one directory's worth of results, and a column
    # repeating that one path beside every row says nothing.
    homes = {os.path.dirname(trace["path"]) for trace, _, _ in found}
    rows = []
    for trace, label, note in found:
        if len(homes) > 1:
            note += f" · {display._tilde(os.path.dirname(trace['path']))}"
        rows.append((trace, slots.Option(trace["path"], label, note)))
    # One list across every directory, ordered by when each config was written.
    # Sorting per-directory and concatenating would put an old trace from the
    # first directory above a new one from the second, which is not an order
    # anybody reading a dated list would expect.
    rows = [option for _, option in
            sorted(rows, key=lambda pair: intake.trace_order(pair[0]),
                   reverse=True)]

    if not rows:
        display.nothing(
            "No past run configs here.",
            f"GenPipes writes one beside every command it generates; "
            f"{display._tilde(directory or '.')} has none yet."
            + ("" if widened else "  ·  other tracked runs may."))
        if widened:
            return None

    if not widened:
        rows.append(slots.Option(
            modify.SEARCH_TRACKED, "› Search other tracked runs…",
            "the directories this app already knows runs in — no wider search"))

    picked = ui.choose(
        f"Past run configs   ·   {len(seen)} found"
        + ("" if widened else f" in {display._tilde(directory or '.')}"),
        rows, free_text=False,
        note="a resolved config from a run that already happened")
    if picked is None:
        return None
    if picked == modify.SEARCH_TRACKED:
        return _past_configs(agent, directory, widened=True)
    return picked


def _use_or_add(agent, proposal, changes, path, workdir=None):
    """How a chosen past-run config joins the `-c` stack. Returns True if it did.

    ASKED, NEVER ASSUMED, and that is the point of this screen. A resolved trace
    already contains everything the run it came from was given, so the two
    readings of "use this config" are genuinely different runs: laid on top of
    the current stack it overrules most of what is under it, and used as the
    stack it is the starting point for whatever gets added next.

    Neither is marked as the right one. There is no recommendation here and no
    default beyond the cursor having to start somewhere -- which of the two
    somebody means is not something this code can know, and guessing it would be
    exactly the kind of decision this application keeps out of deterministic
    code.
    """
    choice = ui.choose(
        "Use this resolved config",
        [slots.Option("use", "use as config stack",
                      "-c becomes this one config · anything you add layers "
                      "on top of it"),
         slots.Option("add", "add to current stack",
                      "one more ordered -c entry · the stack keeps everything "
                      "it has"),
         slots.Option("cancel", "cancel", "leave -c as it is")],
        free_text=False,
        note=display._tilde(path))
    if choice == "use":
        changes[modify.CONFIG] = modify.use_config(proposal, changes, path)
    elif choice == "add":
        changes[modify.CONFIG] = modify.toggle_config(proposal, changes, path,
                                                      workdir=workdir)
    else:
        return False
    if changes[modify.CONFIG] == modify.config_stack(proposal):
        changes.pop(modify.CONFIG, None)
    return True


def _run_panel(agent, name, proposal, m, offered, candidates, changes,
               required, fork_as=None, workdir=None):
    """The in-place panel. Returns (outcome, row) and mutates `changes`.

        ("done",   None)   review what has been changed
        ("else",   None)   describe it instead, in prose
        ("ask",    row)    resources -- see below
        ("past",   row)    past run configs -- a flow, for the same reason
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
             "problem": None,
             # Inis taken off the -c stack during this pass, in the order they
             # were taken off. DETERMINISTIC UI STATE, owned here because this
             # is the only thing that knows what was pressed.
             #
             # It exists because "removed" cannot be derived from the stack.
             # modify.options_for used to infer it as "in the original stack and
             # not in the pending one", which is right for cit.ini and wrong for
             # an ini added and removed in the same pass -- that one was never
             # in the original, so it fell back into the candidate list with a
             # blank marker while cit.ini in the same situation showed ✗. One
             # keystroke, two renderings, and the difference invisible.
             "removed": []}

    def choices():
        if not state["open"]:
            return ()
        return modify.options_for(state["open"], proposal, candidates,
                                  pending=changes, removed=state["removed"],
                                  workdir=workdir)

    def entries():
        return modify.panel_entries(m, offered, state["open"], choices(),
                                    state["typed"], changes,
                                    forking=bool(fork_as))

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
            if picked == modify.PAST_CONFIGS:
                # A DOOR, NOT A VALUE -- so config_stack, toggle_config and
                # everything downstream never see a sentinel and go on dealing
                # in inis alone.
                #
                # AND IT LEAVES THE PANEL, exactly as `resources` does and for
                # the same reason that row states: what is behind it is a flow
                # (a list, then a choice of two), not a field. Opening a second
                # ui.choose from inside this one cannot work -- paint() rewrites
                # its own block by walking up its own line count, and a nested
                # panel drawn below would be half-overwritten by the outer one's
                # next repaint. So this hands control back to _modify_guided,
                # which runs the flow and comes straight back to a freshly
                # drawn panel.
                state.update(out="past", row=row)
                return False
            before = modify.config_stack(proposal, changes)
            stack = modify.toggle_config(proposal, changes, picked,
                                         workdir=workdir)
            # Which way it went, recorded from the two stacks rather than
            # guessed at from the marker that was on screen. Shorter means it
            # came off, and an ini that comes back on stops being removed.
            state["removed"] = [
                x for x in state["removed"]
                if not modify.locate(picked, [x], workdir)]
            if len(stack) < len(before):
                state["removed"].append(picked)
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
                               name=name, pending=changes,
                               forking=bool(fork_as))
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

    # `[` and `]` move the ini under the cursor up and down the -c stack. Which
    # keys, and which way, is modify.REORDER_KEYS -- so the footer below and the
    # behaviour here cannot come to disagree.
    #
    # ONLY on the config row, and only on a row that is actually ON the stack.
    # Reordering something that is merely available is meaningless, and letting
    # the key fall through to on_text on every other row would put a `[` into
    # the narrowing filter and empty the list.
    def on_reorder(key, value):
        by = modify.reorder_key(key)
        if by is None or state["open"] != modify.CONFIG:
            return False
        # THE KEY IS CLAIMED FROM HERE ON, whatever it turns out to move.
        #
        # Returning False hands it to the next hook, and the next hook on a
        # printable character is on_text -- which types it into the narrowing
        # filter. So pressing `[` on the ini already at the top of the stack
        # (a no-op, and the most likely accidental press there is) put a `[`
        # in the filter, matched no ini, and emptied the row until somebody
        # worked out to press backspace. A key that means "move this" must
        # never also mean "type this", including on the presses where there is
        # nowhere to move to.
        if not isinstance(value, tuple) or value[0] != modify.CHOICE:
            return True
        ini = value[2]
        stack = modify.move_config(proposal, changes, ini, by, workdir=workdir)
        if stack == modify.config_stack(proposal, changes):
            return True           # already at the end it was moved towards
        if stack == modify.config_stack(proposal):
            changes.pop(modify.CONFIG, None)
        else:
            changes[modify.CONFIG] = stack
        # Follow the row that moved. A cursor that stayed put would leave the
        # highlight on whatever slid into the vacated position, so a second
        # press would move a different ini -- and the obvious way to move
        # something two places is to press the key twice.
        for entry in modify.selectable(entries()):
            if (entry.kind == modify.CHOICE and entry.pick is not None
                    and modify.locate(ini, [entry.value[2]], workdir)):
                return entry.pick
        return True

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
            if state["open"] == modify.CONFIG:
                # `-c` gets its own hint because it is the one row where Enter
                # does not close anything and the one row with a second verb.
                # The panel has claimed "applied in order — later wins" since it
                # was written; naming the keys that change that order is what
                # makes the claim actionable rather than a caption.
                return ("↑↓ · enter puts an ini on or off · [ ] moves it "
                        "earlier/later · esc closes the row")
            if choices():
                return "↑↓ · 1-9 to pick · type to narrow · esc closes the row"
            return "type the new value · enter confirms · esc closes the row"
        # What Enter on the last row will DO, named on the screen where it is
        # pressed. There is no menu after this one, so this line is the only
        # place the consequence appears -- and the two consequences are
        # genuinely different runs being written.
        if fork_as:
            # Named when the name is known, and described when it is not: a
            # /modify on a submitted run forks because the original cannot be
            # rewritten, and it asks what to call the variant after this panel
            # closes. "creates a new run" is the honest version of that, and it
            # still says the thing that matters -- this key does not touch the
            # run whose command is on screen.
            made = fork_as if isinstance(fork_as, str) else "a new run"
            return (f"↑↓ · enter opens a row · d creates {made} · "
                    f"esc creates nothing")
        return ("↑↓ · enter opens a row · d applies to this run · "
                "esc discards the changes")

    # Digits are TEXT while a free-text row is open, and row selectors
    # otherwise. Without this the steps row could not be filled in at all: its
    # values are only ever digits, and every one of them fired Enter on another
    # row. Scoped to rows with no choices, so `1-9 to pick` still works where
    # it is advertised -- see keys() above, which draws exactly this
    # distinction one line at a time.
    def hotkeys():
        """`d` for the row the footer says it is for, when that row exists.

        Computed per keystroke rather than fixed, because the row it stands for
        is not always on the panel: with nothing changed and nothing being
        forked there is no DONE entry, and a key that silently does nothing is
        the defect this replaces rather than a smaller version of it. It is
        also withheld while a row is open, where `d` is a character somebody is
        typing into a value.
        """
        if state["open"]:
            return {}
        if not any(e.kind == modify.EXTRA and e.row == modify.DONE
                   for e in entries()):
            return {}
        return {"d": (modify.EXTRA, modify.DONE)}

    picked = ui.choose(question, options, free_text=False, draw=draw,
                       cursor=modify.cursor_of(entries(), "name"),
                       on_enter=on_enter, on_escape=on_escape, on_text=on_text,
                       typing=lambda: bool(state["open"]) and not choices(),
                       hotkeys=hotkeys, on_key=on_reorder, note=keys)
    if state["out"]:
        return state["out"], state["row"]
    return ("cancel", None) if picked is None else ("done", None)


def _modify_guided(agent, name, record, fork_as=None):
    """The guided /modify: open the rows, fill them, and go back to the gate.

    One model call for the whole set, however many rows changed -- composed into
    a single unambiguous sentence by modify.sentence(). The command string is
    never rebuilt here, even for a change as trivial as a flag swap: that would
    diverge the model's view of the conversation from what is queued, and its
    next turn would reason about a command it did not write.

    TWO SCREENS, NOT FIVE. There used to be a `review N changes` row leading to
    a `Ready to apply` print leading to a `What should happen to these changes?`
    menu, and only then the regeneration. All three are gone.

    The delta was already legible without them: every changed row shows
    `old -> new` in green ON THE ROW IT BELONGS TO, which is closer to the thing
    being described than a separate list of the same edits could ever be. And
    the screen that follows -- the re-rendered gate, with the moved rows green
    -- is both the review and the one place execution is authorised. Confirming
    an edit twice before the authorisation screen is not caution; it is three
    chances to lose track of which screen is the one that matters.

    `fork_as` makes this build a SECOND run instead of replacing this one. Two
    shapes, because its two callers know different amounts:

        a name   /fork, which asked for it before opening anything
        True     /modify on a run that has already been submitted, where the
                 fork is a consequence of the run's state rather than something
                 anybody asked for. The name is asked once the changes are
                 settled -- being asked to name a variant before deciding what
                 makes it different is a question nobody can answer yet.

    Falsy means rewrite this run in place, which only a held run allows.

    WHOSE NAME THE SCREEN CARRIES. Everything below is drawn under `editing`,
    not under `name`. They differ for exactly one caller -- /fork, which has
    already asked what the variant is called -- and the difference was a
    reported bug: the panel said `name  test-now` while what was being built
    was `test-now-2`, so the screen somebody was editing was labelled as the
    run they were NOT editing. The source run's name survives in the header
    display.forking_from() printed above, which is where "this came from
    test-now" belongs.

    `name` is still the SOURCE, and stays the source everywhere a fact about
    the original is wanted: the registry lookup, the parent's override ini,
    the thread the variant is named after.
    """
    proposal = record.get("proposal") or {}
    directory = record.get("workdir") or os.getcwd()
    candidates = intake.candidates(directory)
    # The identity being edited. /fork settled it before opening anything;
    # /modify-on-a-submitted-run has not asked yet, so the original stands in
    # and the `name` row is where the answer arrives.
    editing = fork_as if isinstance(fork_as, str) else name

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
            override.read(override.path_for(editing, directory, proposal,
                                            fresh=bool(fork_as))))
        rows = modify.rows_for(proposal, editing, resources=tuned)
        m = (mirror.read(proposal.get("generated"), name=editing, resources=tuned,
                         missing=proposal.get("missing"))
             or mirror.from_slots(proposal, name=editing, resources=tuned)).ensure(
                 [row for row, _ in rows])
        # Cursor order follows the mirror, not ROWS, so ↓ moves down the screen.
        # They agree today; they would not the moment a command carried a flag
        # in an order the table does not know, and an arrow key that skips a
        # line is the kind of wrongness nobody reports and everybody distrusts.
        offered = [line.row for line in m.lines
                   if line.row in {row for row, _ in rows}]

        # `directory` is the RUN's workdir, not this process's cwd, and that
        # is what decides whether `override_walltime.ini` on the -c line and
        # the absolute path the scan found are the same file. Resolving a
        # relative ini against wherever the app happens to be running would
        # make two unrelated files one, or one file two.
        outcome, row = _run_panel(agent, editing, proposal, m, offered,
                                  candidates, changes, required,
                                  fork_as=fork_as, workdir=directory)
        if outcome == "cancel":
            if fork_as:
                display.nothing("Nothing created.",
                                f"'{name}' is unchanged.")
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
        if outcome == "past":
            # Past run configs -- a flow rather than a field, like resources.
            # The view scans on demand and remembers nothing; what comes back is
            # one path, and how it joins the stack is asked separately.
            chosen = _past_configs(agent, directory)
            if chosen:
                _use_or_add(agent, proposal, changes, chosen, workdir=directory)
                required.pop(row, None)
            continue
        if outcome == "ask":
            # Resources -- the one row that is a flow rather than a field. Its
            # screen writes an override ini and hands back the path, which goes
            # into `changes` like any other answer, and the loop comes straight
            # back to the panel with that row now green.
            path = _fill_resources(agent, name, proposal, directory,
                                   editing=editing, fresh=bool(fork_as))
            if path is None:
                return
            if path:
                changes[row] = path
            continue

        # An empty change set means two different things, and only one of them
        # is a no-op.
        #
        # REWRITING a run with nothing changed is genuinely nothing: the
        # command is already what it would be regenerated as, and paying a
        # model call to reproduce it can only introduce drift.
        #
        # FORKING with nothing changed is a real request, and refusing it was
        # a reported bug. "Run this again under a second name" is an ordinary
        # thing to want -- a rerun after a cluster problem, a second identity
        # for a command somebody is about to tune elsewhere -- and the new
        # name IS the change. Nothing else has to justify the copy.
        # modify.fork_sentence() has always handled the empty set ("Generate
        # this GenPipes run again, exactly as written"); the refusal was in
        # this caller, not in the instruction it would have sent.
        if not changes and not fork_as:
            display.nothing("Nothing changed.", f"'{name}' is still held.")
            return

        # A row an earlier answer made mandatory and nobody filled in. This is
        # the ONE thing that still sends somebody back to the panel, and it is
        # not a confirmation -- changing the pipeline invalidates the protocol,
        # and regenerating with the old one would produce a command nobody
        # asked for. The rows come back red with the reason beside them.
        outstanding = {row: why for row, why
                       in modify.required_after(proposal, changes).items()
                       if not changes.get(row)}
        if outstanding:
            required = outstanding
            display.problem(
                "Some rows still have to be answered: "
                + ", ".join(sorted(outstanding)),
                "They are marked on the command.")
            continue
        break

    if fork_as:
        # `True` carries no name, and _fork_run asks for one when `wanted` is
        # falsy -- which is exactly the /modify order, so nothing else is needed
        # to make the two callers differ.
        #
        # Except for the `name` row, which the panel offers on every run. On a
        # fork _fork_run pops it, because renaming belongs to the run being
        # copied FROM and applying it there would rename the original as a side
        # effect. That is right for /fork, where the new name was settled before
        # the panel opened -- and silently wrong here, where the name has not
        # been asked yet: somebody typing one into the row is naming the thing
        # they are making, and dropping it would then ask them for it again.
        # The panel's own `name` row wins when it was touched. /fork settled a
        # name before opening, and somebody who then opens that row and types a
        # different one has changed their mind about what they are making --
        # taking the earlier answer would silently discard the later one.
        # Either way _fork_run pops it from `changes`, so it names the copy and
        # never renames the original.
        wanted = (changes.get("name")
                  or (fork_as if isinstance(fork_as, str) else None))
        _fork_run(agent, name, record, proposal, changes, wanted=wanted)
        return
    _apply_changes(agent, name, proposal, changes, workdir=directory)


# _ask_ending() lived here: a three-row menu asking whether a finished change
# set should be applied to the run, saved as a new one, or edited further.
#
# It existed because a rework destroys the held interrupt, so "replace" and
# "keep both" really are different acts. But that is a reason for a second
# VERB, not for a question appended to every edit -- /modify foo already says
# which run is being changed, and /fork keeps both. The third row, "keep
# editing", was undoing a screen that should not have been there.
#
# Its one genuinely load-bearing detail moved rather than died: the fork's name
# is asked BEFORE anything else, so a collision is visible while the choice is
# still open instead of being resolved silently by unique_name() afterwards.
# See _cmd_fork.


def _fill_resources(agent, name, proposal, directory, editing=None, fresh=False):
    """Tune one or more steps' cluster resources into this run's override ini.

    TWO NAMES, because a fork reads from one run and writes to another.
    `name` is the SOURCE -- the registry record whose /diagnose may already
    have computed an override worth offering -- and `editing` is the run the
    file is written for, which is the fork when there is one. `fresh` goes to
    override.path_for and stops it reusing the ini it finds on the parent's
    inherited -c line.

    Getting this wrong was a real defect and a silent one: tuning a step in a
    fork wrote into the PARENT's override ini, so a run somebody had gone out
    of their way not to touch quietly acquired a new walltime, and the two runs
    the fork exists to keep separate shared one file again.

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
    editing = editing or name
    path = override.path_for(editing, directory, proposal, fresh=fresh)
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
    proposed = (runs_store.remediation_of(agent.registry.get(name))
                .get("override") or {})
    proposed = {s: k for s, k in proposed.items() if s not in sections}
    if proposed:
        display.overrides(editing, override.describe(proposed), path)
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
            display.overrides(editing, override.describe(sections), path)
            return override.write(path, sections, run=editing)

    protocol = (values.get("protocol")
                or slots.DEFAULTS.get(values.get("pipeline") or ""))
    help_text = agent.step_help(values.get("pipeline"), protocol)
    status, known = modify.step_list(help_text, protocol)
    # When there is no list the panel degrades to free text. It used to do that
    # SILENTLY, which is how somebody faced with "its --help name, e.g.
    # gatk_sam_to_fastq" and no list typed `--help` -- twice -- trying to get
    # the thing the prompt was quoting.
    #
    # And then it did the opposite: it said "--help could not be read" about
    # help that had been read in full and simply printed a shape the parser did
    # not know. Both are unhelpful, and they call for different sentences, so
    # the reason is passed through rather than inferred from an empty list.
    if not known:
        display.no_step_list(values.get("pipeline"), protocol, status)

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

    display.overrides(editing, override.describe(sections), path)
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


def _fork_run(agent, name, record, proposal, changes, wanted=None,
              reason="fork"):
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

    `wanted` normally arrives already answered, because /fork asks for the name
    before opening the panel. The prompt below is the fallback for callers that
    have no panel to have asked in, and the `name` row is popped either way: a
    rename belongs to the run being forked FROM, and applying it here would
    rename the original as a side effect of copying it.

    THE SHARED REVISION PRIMITIVE. /fork, /modify on a launched run and
    /relaunch all end here, and that is deliberate: forking is the operation
    with the safety property everything else depends on -- the original keeps
    its name, its command, its job list and its jobs -- and a second
    implementation of it is a second place for that property to stop holding.
    The three callers differ in what they put in `changes` and in nothing else.

    `reason` is why this revision exists, recorded on it.

    THE INTERPRETIVE PATH, and it stays that way. What arrives here is a change
    somebody stated -- "give alignment more memory", a protocol swapped in a
    panel -- and turning that into a command is what the model is for. It is
    not what /relaunch needs: a diagnosed remediation is already a section, a
    key and a value, so that path writes its own command and never comes here.
    See relaunch.command and agent.hold_prepared.
    """
    changes.pop("name", None)
    if not wanted:
        try:
            wanted = ui.ask("What should the new run be called?")
        except (EOFError, KeyboardInterrupt):
            return
    verdict = modify.check("name", str(wanted or ""), proposal,
                           registry=agent.registry, forking=True)
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
        risks, stop = _step_risks(agent, name, "", wanted=changes["steps"])
        if stop:
            display.problem(stop, f"Nothing was changed; '{name}' is unchanged.")
            return

    # The declaration matters MORE on this path than on the in-place one. A
    # fork lands under a new name, so there is no earlier proposal to diff
    # against and modify.compare() correctly declines to say anything; without
    # this the panel's whole change set would reach the gate unverified.
    agent._gate_note = {"warnings": risks, "changed": list(changes),
                        "declared": modify.declaration(proposal, changes,
                                                       directory)}
    thread = f"{name}::variant-{datetime.datetime.now():%m%d%H%M%S}"
    with ui.Activity("building the variant") as act:
        agent.run(request, thread, on_step=_narrate(act), name=new_name)
    # LINEAGE, WRITTEN AFTER THE RUN EXISTS and only if it does. agent.run()
    # mints the record at the gate; a revision that never reached one has
    # nothing to attach a parent to, and writing the link first would leave a
    # dangling one behind every abandoned regeneration. Recorded for every
    # caller, not just /relaunch: a fork's parent has always been findable only
    # by reading a confirmation line that scrolled away.
    if not agent.registry.get(new_name):
        # The regeneration never reached the gate, so there is no revision --
        # and saying "prepared" over an empty registry is the one thing this
        # screen must not do. Nothing was written under the new name and the
        # original was never touched, so there is nothing to undo either.
        display.problem(
            f"'{new_name}' was not prepared — the regeneration stopped "
            f"before the approval gate.",
            f"'{name}' is unchanged and nothing was submitted.")
        return None
    agent.registry.derive(new_name, name, reason)
    display.forked(name, new_name, _standing(record))
    return new_name


def _apply_changes(agent, name, proposal, changes, workdir=None):
    """Apply a validated change set: at most one model call, then the rename.

    The rename goes LAST, after resume() returns, so the gate re-renders once
    and under the new name. Doing it first would draw the box twice -- once
    under each name -- which reads as two runs.

    There is no longer a version of this that applies the changes and does NOT
    redraw the gate. That was "hold for later", and it left the run in exactly
    the state this does: the redraw is the review AND the authorisation point,
    so suppressing it would remove the only screen that matters.
    """
    new_name = changes.pop("name", None)
    sentence = modify.sentence(proposal, changes)

    if sentence:
        risks, stop = [], None
        if "steps" in changes:
            risks, stop = _step_risks(agent, name, "", wanted=changes["steps"])
            if stop:
                display.problem(stop, "Nothing was changed.")
                return
        # `workdir` is the RUN's directory, and it is passed for the same
        # reason the panel resolves inis against it: modify.declaration diffs
        # the -c stack through locate(), so a relative ini on the command line
        # would otherwise be resolved against wherever the app happens to be
        # running. The fork path has always passed it; this one silently did
        # not, and a config add or remove could be mis-diffed into the wrong
        # applied/ignored verdict whenever the app was launched from somewhere
        # other than the run's own directory.
        _rework(agent, name, sentence, warnings=risks, changed=list(changes),
                declared=modify.declaration(proposal, changes, workdir))

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


def _cmd_redraw(agent, args):
    """/redraw -- draw the last panel again, at the width the window is now.

    WHY THIS IS A COMMAND AND NOT A RESIZE HANDLER. Everything here is ordinary
    scrollback. Resizing a terminal makes the EMULATOR reflow what is already
    printed, and it reflows it as text: a line wrapped by this application to
    fit 100 columns is soft-wrapped again at 80, and the overflow restarts at
    column zero -- underneath the gutter it was drawn beside. The diagnosis
    panel loses its structure that way and nothing in this process can prevent
    it, because those bytes were written to a stream it cannot edit.

    Catching SIGWINCH would not fix it either. It cannot rewrite the panel; it
    can only print a second copy, and it would fire repeatedly while a window
    is being dragged, in the middle of whatever the person was typing. A copy
    on request is the honest version of the same thing.

    DISPLAY ONLY, and structurally rather than by promise: what is stored is a
    display function and the arguments it was called with (see
    display.canonical), so there is no model to call, no log to re-read and no
    record to touch. Redrawing twice draws the same panel twice.
    """
    if args:
        display.problem("usage: /redraw",
                        "It draws the last panel again, at the current width.")
        return
    name, again = display.last_surface()
    if again is None:
        display.nothing("Nothing has been drawn to redraw yet.",
                        "/list, /check, /jobs, /view or /diagnose draws one.")
        return
    again()


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
    # Reconciled before it is drawn, for the same reason /list is: the verbs
    # printed under this box are the ones somebody is about to type, and
    # offering /approve on a run that would refuse it is the contradiction
    # this whole change exists to remove. One record, one checkpoint read.
    agent.reconcile_registry([record])
    record = agent.registry.get(name) or record
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
        else (),
        # The whole record, so the verbs can be chosen from what this run can
        # actually support rather than from its status word alone. A submitted
        # run with no manifest is still `submitted`, and /check on it can only
        # report that it cannot look.
        record=record)


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
    # The same words /check, /list and /diagnose use for this verb. done()
    # carries a one-line hint rather than an Actions block -- it is a two-line
    # confirmation, and a heading over a single suggestion is furniture.
    display.done(f"{name} · {last[1] if last else 'finished'}",
                 f"/jobs {name}  ·  {display.action_text('/jobs')}")


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

    # The same rows /list just drew, in the same order, with checkboxes on
    # them. This used to iterate `records` in the registry's raw append order,
    # so the row somebody had read as fourth in /list turned up seventeenth
    # here and the only way to find it was to read every line. resolve_all()
    # is the same batched scheduler call /list makes, and it is what lets
    # listing_order() put a live run where /list puts it.
    with ui.Activity("reading the scheduler"):
        rows = runs_store.resolve_all(records)
    options = [slots.Option(record["name"], record["name"],
                            _run_note(record, status))
               for _, record, status in runs_store.listing_order(rows)]
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
    """/verbose -- show the agent's activity, including what already scrolled by.

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
        # WHAT IT ACTUALLY UNFOLDS, said accurately. "the agent's working"
        # promises a chain of thought and this shows no reasoning at all: the
        # folded events are `code` (commands the agent ran), `observation`
        # (what they printed) and `note` (its connective prose) -- see
        # display._draw and the _folded list. That is ACTIVITY, observable from
        # outside, and nothing here depends on a model exposing anything
        # private. Promising working and delivering a command log is the kind
        # of small overclaim that makes somebody distrust the rest of a screen.
        "Showing agent activity — commands, their output, and what it read."
        + (f"  {replayed} step{'s' if replayed != 1 else ''} replayed." if replayed else ""),
        "/verbose to fold it away")


def _cmd_jobs(agent, args):
    """/jobs <name> [failed] -- the individual Slurm jobs inside a run.

    ITS OWN VERB, NOT A DETAIL LEVEL OF /check, and the distinction that
    settles it is worth writing down because it was nearly got wrong.

    /check answers "how is this run doing". /jobs answers "which jobs are in
    it". Those are different QUESTIONS about the same subject, and a different
    question wants its own word. Compare `/check all`, which is genuinely an
    argument: the same question, asked of a different subject.

        same question, different subject   ->  an argument      /check all
        different question, same subject   ->  its own verb     /jobs

    Folding this in as `/check <name> jobs` was proposed and rejected on the
    evidence. It costs a completion vocabulary (`/jobs <TAB>` completes run
    names; `/check <name> <TAB>` would have to complete mode words after one),
    it makes the /help row read as `/check <name>|all [jobs]` where the bracket
    is skimmed past, and the modifier this command already takes turns into a
    four-token sentence -- `/check x jobs failed`. That is a small command
    language, invented to save one line of /help.
    """
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
    """/history [name] -- the archive, or one record in full.

    The bare form is a table with one row per run and no scheduler call in it:
    /history is an archive, not a dashboard. Naming a run opens what /diagnose
    once found out about it, which is the part worth keeping and the part that
    made the summary unreadable when it was printed under every row.
    """
    agent.history(" ".join(args) if args else None)


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
                    else "not a recognised Alliance login node",
         "picks the cluster ini a generated command stacks on -c"),
        # The one row with teeth, and now the one row that says so. A run's
        # job list is looked for under here, so launching the app from an
        # unexpected directory is the difference between a submission being
        # recorded and vanishing -- which is a failure that looks exactly like
        # success until somebody types /check.
        ("launched from", os.getcwd(),
         "where a submission's job_output is looked for — a run launched "
         "from elsewhere has to be adopted with /track"),
        ("agent workdir", agent.path, ""),
        ("run registry", agent.registry.path,
         "every run this tool knows about; /history reads it"),
        ("checkpoints", os.path.join(agent.path, "genpipe_checkpoints.sqlite"),
         "the conversations, and the gates parked in them"),
        # The file the app writes the key, the model and the name into, and
        # now also reads back. It belongs on this screen for the same reason
        # `launched from` does: when it is not where somebody assumes, nothing
        # else on the screen explains why the session came up unconfigured.
        ("settings", str(ENV_PATH),
         f"key, model and name · colours are {display.THEME} "
         f"(GENPIPE_THEME=light|dark)"),
        # The fourth of the four locations the README names. It is not a path
        # this process chooses -- start_agent.sh built it and activated it
        # before Python existed -- so it is read back off the interpreter
        # rather than recomputed from GENPIPE_VENV, which would report what
        # somebody MEANT rather than what is actually running.
        ("venv", os.environ.get("VIRTUAL_ENV") or os.path.dirname(
            os.path.dirname(os.path.abspath(sys.executable))),
         "built once per cluster by start_agent.sh (GENPIPE_VENV)"),
        ("this copy", ROOT, ""),
    ])


def _cmd_telemetry(agent, args):
    """/telemetry -- counts and timings from the generate/execute loop, plus
    what the provider says each model call cost.

    Two sources, and they answer different questions. The node timings come
    from genpipe/telemetry.py and are off unless GENPIPE_TELEMETRY=1, because
    recording them costs a dict append per graph step. The model rows come from
    genpipe/metering.py, which is in the call path either way -- so the tokens
    are reported whether or not the flag was set, and the flag is only about
    the graph's own bookkeeping.
    """
    meter = getattr(agent, "llm", None)
    usage = meter.summary() if hasattr(meter, "summary") else None

    rows = []
    if usage and usage["calls"]:
        # Rendered as prose per row rather than a table: a run with no cache
        # and a run with one have different fields worth showing, and a table
        # would print two empty columns to keep its shape.
        parts = [f"{usage['calls']} call(s), {usage['seconds']:.2f}s total"]
        if usage["input"] is not None:
            parts.append(f"{usage['input']:,} in / {usage['output']:,} out")
        if usage["cache_read"] is not None:
            parts.append(f"cache {usage['cache_read']:,} read, "
                         f"{usage['cache_write'] or 0:,} written")
        elif not usage["caching"]:
            parts.append("caching not available for this provider")
        if usage["repairs"]:
            parts.append(f"{usage['repairs']} malformed reply repaired")
        rows.append(("model", " · ".join(parts)))

    summary = agent.telemetry.summary() if agent.telemetry.enabled else {}
    rows += [(kind, f"{row['count']} call(s), {row['total']:.2f}s total, "
                    f"{row['mean']:.2f}s mean")
             for kind, row in sorted(summary.items()) if kind != "model"]

    if not rows:
        display.nothing(
            "Nothing recorded yet this session.",
            "Node timings also need GENPIPE_TELEMETRY=1 before startup."
            if not agent.telemetry.enabled else None)
        return
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
#
# THE LAST COLUMN IS WHETHER THE COMMAND IS PART OF THE PRODUCT. A False row
# still parses, still dispatches and still has its tests; it is simply not
# taught. That distinction is worth having as a flag rather than as a deletion
# for two different reasons, and both are on this table:
#
#   /telemetry  developer instrumentation. It is genuinely useful when a
#               generation feels slow, and it is not a thing a person running
#               a pipeline should be reading about in /help.
#   /readset    not mature enough to teach. Building a readset from filenames
#               is a real need and a real subsystem, and half of one offered in
#               /help reads as a promise. Hidden rather than removed, because
#               removing it is a bigger change than the situation calls for and
#               `/readset schema` -- which prints the format and touches
#               nothing -- is the part that already works.
#
# Hiding rather than deleting also keeps the honest property: nothing anybody
# has typed before stops working. What changes is what the product claims.
COMMAND_SPECS = [
    ("new",      "",                   "start a fresh conversation",              "talking",  None,          True),
    ("verbose",  "[off]",              "show or fold away agent activity",     "talking",  _cmd_verbose,  True),
    ("approve",  "<name>",             "let a held submission through to Slurm",  "deciding", _cmd_approve,  True),
    ("modify",   "<name>",             "change a run before it is launched",      "deciding", _cmd_modify,   True),
    # PREPARES A RETRY, and the description says "prepare" for the same reason
    # the docstring does: this is the one verb in the table whose name could be
    # read as reaching the scheduler, and it stops at the gate like everything
    # else. Filed under "deciding" beside /modify and /fork, which is what it
    # is -- a third way to arrive at a run waiting for approval.
    ("relaunch", "<name>",             "prepare a retry using a diagnosed fix",   "deciding", _cmd_relaunch, True),
    ("fork",     "<name>",             "build a second run from an existing one", "deciding", _cmd_fork,     True),
    ("reject",   "<name>",             "abandon a held run; nothing submitted",   "deciding", _cmd_reject,   True),
    ("view",     "<name>",             "the command a run is, and what it takes", "watching", _cmd_view,     True),
    ("list",     "",                   "runs awaiting approval, and live ones",   "watching", _cmd_list,     True),
    # These four answer four different questions about one run, and they are
    # worded to read as a set: what is its overall state, what are its jobs,
    # why did it break, and tell me when it changes. Each keeps its own verb
    # and its own implementation -- see the note on _cmd_jobs for why "jobs"
    # is not a detail level of "check".
    ("check",    "<name>|all",         "show the run's overall status",           "watching", _cmd_check,    True),
    ("monitor",  "<name>",             "watch a run until its state changes",     "watching", _cmd_monitor,  True),
    ("jobs",     "<name> [failed]",    "show every job and its state",            "watching", _cmd_jobs,     True),
    ("history",  "[name]",             "the archive of every run recorded",       "watching", _cmd_history,  True),
    # One verb, not two. /why and /diagnose were the same function under two
    # names, and the only thing the pair achieved was making people wonder which
    # to reach for -- the two answers differed by model variance, never by
    # design. /check already gives the quick why, in its root cause block, for
    # free and with no model call. This is the one that reads the logs.
    ("diagnose", "<name>",             "investigate why a run failed",            "fixing",   _cmd_diagnose, True),
    ("hold",     "<name> [release]",   "stop a run's queued jobs being scheduled", "fixing",  _cmd_hold,     True),
    ("cancel",   "<name>",             "scancel a run's remaining jobs",          "fixing",   _cmd_cancel,   True),
    ("sort",     "[show]",             "tick rows to hide from /list; show undoes", "fixing", _cmd_sort,     True),
    ("scan",     "[path]",             "find GenPipes runs already on disk",      "fixing",   _cmd_scan,     True),
    ("track",    "<name> <job_list>",  "adopt a run launched outside the agent",  "fixing",   _cmd_track,    True),
    ("readset",  "[dir|schema]",       "build a readset file from filenames",     "setup",    _cmd_readset,  False),
    ("where",    "",                   "the cluster, and where runs are written", "setup",    _cmd_where,    True),
    # AFTER A RESIZE. The terminal reflows printed scrollback as plain text, so
    # a panel drawn at one width loses its structure at another; this draws the
    # last one again at the width the window is now. See _cmd_redraw for why it
    # is not a SIGWINCH handler.
    ("redraw",   "",                   "draw the last panel again, at this width", "setup",   _cmd_redraw,   True),
    ("telemetry", "",                  "generate/execute/checkpoint timings",     "setup",    _cmd_telemetry, False),
    ("user",     "[name]",             "show or change what it calls you",        "setup",    _cmd_user,     True),
    ("model",    "[provider [model]]", "show or switch the model behind this",    "setup",    _cmd_model,    True),
    ("key",      "",                   "add or rotate an API key",                "setup",    _cmd_key,      True),
    ("help",     "",                   "this list",                               "setup",    _cmd_help,     True),
    ("exit",     "",                   "leave",                                   "setup",    None,          True),
]

# Every command that dispatches, public or not. A hidden command still works
# when it is typed -- it is only untaught.
COMMANDS = {name: fn for name, _, _, _, fn, _ in COMMAND_SPECS if fn}


def _specs_now():
    """The PUBLIC commands, with the state-dependent rows filled in for now.

    Two jobs, and the first one is the command surface. A row flagged
    non-public is dropped here, which is the single point that decides what
    /help teaches and what the completion menu offers -- COMMANDS above is
    built from the raw table, so a hidden command still dispatches when it is
    typed. See the note on COMMAND_SPECS for why /telemetry and /readset are
    hidden rather than deleted.

    The second job is /verbose. Its row was written as a fixed "[off]", which is a
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
    for name, args, desc, group, fn, public in COMMAND_SPECS:
        if not public:
            continue
        if name == "verbose":
            args = ""
            desc = ("fold agent activity away  ·  now: showing"
                    if display.VERBOSE else
                    "show agent activity  ·  now: folded away")
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


def _dispatch_line(agent, line, focus=None):
    """Run a slash command. One implementation, shared by every caller that
    needs to dispatch a typed command.

    `focus` is the session's runs.Focus, updated here because this is the one
    place a typed command and its arguments are both in hand. It is recorded
    BEFORE the handler runs and from the argument alone -- see runs.Focus for
    why an argument is a fact and a sentence is not.
    """
    parts = line[1:].split()
    if not parts:
        return
    cmd, args = _resolve(parts[0].lower()), parts[1:]
    if focus is not None:
        focus.note(cmd, args, known=agent.registry.get)
    handler = COMMANDS.get(cmd)
    if handler is None:
        print(f"  {display.RED}No such command: {line.split()[0]}{display.RESET}"
              f"  {display.GREY}(try /help){display.RESET}\n")
        return
    # THE RESOLVED NAME, not the word that was typed. _resolve accepts any
    # unambiguous abbreviation, so `/diag` runs /diagnose -- and the
    # interrupt claim below, keyed on the raw word, did not recognise it and
    # fell through to _scheduler_claim. Ctrl-c during `/diag <an old run>`
    # printed the "already submitted" false alarm the read-only list exists to
    # prevent, for the only reason that the command had been abbreviated.
    verb = cmd
    try:
        handler(agent, args)
    except KeyboardInterrupt as stop:
        # /approve is why this is not a bare "Stopped.". Every other command
        # here reads state, but an interrupt during an approval can land after
        # the submission has begun -- and the registry knows, because
        # begin_submission() writes before the command runs. The runs this
        # command NAMED are the ones asked about; a command with no run
        # argument gets the general answer.
        #
        # AND A READ-ONLY COMMAND SAYS SO PLAINLY. _scheduler_claim reads the
        # STATUS of the runs it is handed, so ctrl-c during
        # `/diagnose <a run submitted three weeks ago>` reported "'<name>' had
        # already been approved -- already submitted", which is true of the run
        # and a false alarm about the interruption: it reads as though the
        # diagnosis might have put something on Slurm. Nothing /diagnose can
        # call does that -- every capability the model may use is
        # capabilities.READS -- so the honest claim is the reassuring one.
        left = getattr(stop, "genpipe_unreaped", 0)
        claim = (_INTERRUPT_CLAIM.get(verb) or _scheduler_claim(agent, args))
        display.interrupted(
            claim,
            note=(f"a tool was still finishing in the background "
                  f"({left} worker{'' if left == 1 else 's'})" if left else None))
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


def _names_on(agent, thread):
    """Every run this conversation has produced, whatever state it is in.

    registry.held_for_thread() answers a narrower question -- which run is
    WAITING on this thread -- and is the wrong one to ask after an interrupt: a
    run that has already been approved is exactly the one worth checking and is
    exactly the one that lookup skips.
    """
    try:
        return [r["name"] for r in agent.registry.live(prune=False)
                if r.get("thread_id") == str(thread)]
    except Exception:                      # noqa: BLE001
        # An unreadable registry must cost the reassurance, not the prompt.
        return []


def _scheduler_claim(agent, names=()):
    """What may honestly be said about the scheduler after an interruption.

    "Nothing reached the scheduler" used to be printed unconditionally by the
    ctrl-c handler, which is a claim about the cluster made by a function that
    had looked at nothing. It is usually true -- nothing reaches Slurm without a
    typed /approve -- and "usually true" is the wrong standard for the one
    sentence on the screen whose job is to stop somebody going and looking.

    So it is checked. `names` are the runs this interruption could plausibly
    have been in the middle of; for a conversational turn that is whatever the
    thread holds, and for /approve it is the run being approved. A run sitting
    in one of the AFTER_APPROVAL states has been through submission and may have
    reached the scheduler, and this says so rather than reassuring.

    The evidence is the registry's own, read through the same standing_of() that
    /list and /check use -- not a second opinion invented here. The wording is
    runs.interrupt_claim's, so the decision about what may be claimed is one
    stdlib-only function that CI checks rather than a sentence in a handler.
    """
    entries = []
    for name in dict.fromkeys(n for n in names if n):
        record = agent.registry.get(name)
        if not record:
            continue
        entries.append((name, record.get("status"),
                        agent.standing_of(record).why))
    return runs_store.interrupt_claim(entries)


# Commands that cannot reach the scheduler, whatever the runs in this thread
# say about themselves.
#
# WHY THIS LIST EXISTS. _scheduler_claim answers "could this interruption have
# left something on Slurm" by reading the STATUS of the runs the thread holds --
# which is right for a conversational turn, where the model may have been in
# the middle of anything, and for /approve, where it may have been in the
# middle of the one thing that matters. It is wrong for a read-only command:
# ctrl-c during /diagnose on a run submitted three weeks ago printed "'<name>'
# had already been approved -- already submitted. /check before assuming either
# way", which is a true sentence about the RUN and a false alarm about the
# INTERRUPTION. Nothing /diagnose does can submit anything.
#
# Keyed on the command, not on the run, because that is the fact in question:
# what was interrupted. Every capability the model can call during one of these
# is capabilities.READS (see capabilities.ENABLED), so the claim is safe.
_READ_ONLY_COMMANDS = frozenset((
    "check", "diagnose", "jobs", "list", "view", "history", "where",
    "monitor", "sort", "help", "verbose", "redraw",
))


# What may be said after ctrl-c, for the commands that can say something
# stronger than _scheduler_claim's reading of a run's status.
#
# /relaunch is here and is NOT read-only: it writes an override ini and runs a
# GenPipes generation. What it cannot do is submit -- it ends at the gate by
# construction -- so _scheduler_claim, which answers by reading the SOURCE
# run's status, would report "already submitted" about a run that was launched
# weeks ago and read as a warning about the interruption. That is the same
# false alarm _READ_ONLY_COMMANDS exists to prevent, with a different true
# sentence to replace it: the command did things, and none of them was a
# submission.
_INTERRUPT_CLAIM = dict(
    {verb: "Nothing reached the scheduler — that command only reads."
     for verb in _READ_ONLY_COMMANDS},
    relaunch="Nothing reached the scheduler — /relaunch only prepares a run.",
)


def _say_nothing_was_prepared(agent, outcome):
    """A turn that generated and produced no run says so.

    THE SILENCE THIS CLOSES. `agent.run()` returning normally printed nothing.
    That is right for a turn that was only ever talk -- the answer is the
    answer -- and wrong for one that actually ran `genpipes ... -g` and then
    stopped: real work happened, no run exists, and the screen looked exactly
    like a conversation that had finished. The first report of it was a turn
    that ended on a rendered panel with a half-ticked plan above it and nothing
    saying the run had been abandoned.

    DETERMINISTIC, AND NOT AN INTENT CLASSIFIER. Two facts, both observed
    rather than interpreted: whether the graph ran a generation block
    (gate.is_generation, recorded by the router as it passed) and what the
    graph returned. No sentence is read, nothing decides whether a request
    "looked like" run preparation, and a turn that only talked is left alone --
    which is the common case and the one that must stay quiet.

    `paused` means the gate has it and the HOLD box is already on screen.
    `asking` means a question is on screen. Both are outcomes somebody can see;
    only a plain `done` after a generation is the silent one.
    """
    if (outcome or {}).get("status") != "done":
        return
    if not getattr(agent, "_generated_this_turn", False):
        return
    display.nothing(
        "No run was prepared.",
        "A command was generated but nothing reached the approval gate, so "
        "there is nothing held and nothing submitted. Say what to change, or "
        "ask again.")


def _turn(agent, thread, text, raw=None, label="thinking", read_only=False):
    """One exchange: the user's line in, the agent's work out.

    A conversation parked at the gate does not take a normal turn -- see
    _at_the_gate for what happens instead and why it must not reach agent.run().

    Ctrl+C interrupts the AGENT, not the session. It abandons this answer and
    returns to the prompt with the conversation intact, which is what people
    expect from every other assistant with a spinner in it: stopping a reply is
    not the same as leaving. The conversation, the run records and everything
    already on screen survive; only the reply in flight does not.

    WHAT IT DOES NOT DO is claim anything about the scheduler it has not
    checked. See _scheduler_claim.
    """
    if _at_the_gate(agent, thread, raw if raw is not None else text):
        return
    try:
        with ui.Activity(label) as act:
            agent.on_ask = _asker(act)
            outcome = agent.run(text, thread_id=thread, on_step=_narrate(act))
        _say_nothing_was_prepared(agent, outcome)
    except KeyboardInterrupt as stop:
        # A tool that was still finishing when the deadline expired. Named
        # rather than swallowed: its output is muted from here on, so this line
        # is the only thing that says it exists.
        left = getattr(stop, "genpipe_unreaped", 0)
        note = (f"a tool was still finishing in the background "
                f"({left} worker{'' if left == 1 else 's'}) — its output is "
                f"suppressed from here on" if left else None)
        # A read-only command has no run to be uncertain about -- see
        # _READ_ONLY_COMMANDS.
        claim = ("Nothing reached the scheduler — that command only reads."
                 if read_only
                 else _scheduler_claim(agent, _names_on(agent, thread)))
        display.interrupted(claim, note=note)
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
_EITHER = ("modify", "view", "fork")
# The one command whose completion is not a state filter but a PREDICATE: a run
# is offered only if relaunch.plan() would accept it. Same source as the bare
# command's picker, so the two cannot offer different sets -- see
# relaunch.candidates for why that matters.
_ELIGIBLE = ("relaunch",)


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


def _run_names(agent, command, focus=None):
    """Values to complete the first argument of `command` with, or None.

    None means "this command's argument is neither a run name nor anything
    else we can enumerate" -- /track's first argument is a name being
    invented. /model's is a provider, which is a closed set, so it completes
    from _provider_names(). An empty list means it IS a run name and there are
    none, which the prompt says out loud rather than silently offering
    nothing.

    `focus` is the run the person last named on a command line, and it is
    applied LAST -- after this function has decided which runs are legal for
    this command -- so it can only reorder that set, never widen it. That is
    what makes `/view foo` then `/approve ` offer foo first while `/approve `
    after foo has been approved does not offer it at all: foo stops being held,
    so it is not in the list for focus to move.
    """
    # Before the try: this reads the environment, not the registry, and a
    # broken registry has no bearing on whether the provider list is right.
    if command == "model":
        return _provider_names()
    try:
        if command in _ELIGIBLE:
            # The same row the bare command's picker draws, from the same
            # candidate list and through the same note -- so what is offered
            # here and what is listed there cannot read differently.
            return _ranked([(record["name"], _relaunch_note(record, plan))
                            for record, plan in
                            relaunch.candidates(agent.registry.live())], focus)
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
            return _ranked([(r["name"], _run_note(r)) for r in records], focus)
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
    return _ranked([(r["name"], _run_note(r)) for r in reversed(records)], focus)


def _relaunch_note(record, plan):
    """One row's description, wherever a relaunch candidate is offered.

    What it is, how it stands, and the fix that makes it a candidate. Written
    once because the picker and the completion menu must not describe the same
    run differently.
    """
    return f"{_run_note(record)}  ·  {relaunch.summary(plan)}".strip()


def _ranked(rows, focus):
    """`rows` with the focused run first, when it is one of them."""
    return focus.rank(rows) if focus is not None else rows


def _run_note(record, status=None):
    """One phrase saying what this run is, for a completion menu or /sort.

    `status` is a resolved RunStatus when the caller has already paid for the
    scheduler round-trip. Given one, the state comes from runs.list_tag -- the
    same words /list puts in its STATUS column -- so a row reads identically on
    both screens. Without one it falls back to the last cached verdict, which
    is what the completion menu has to live with: it redraws on every keystroke
    and cannot query Slurm to do it.
    """
    values = (record.get("proposal") or {}).get("slots") or {}
    what = " ".join(str(values[k]) for k in ("pipeline", "protocol")
                    if values.get(k)) or (record.get("proposal") or {}).get("command", "")
    if status is not None:
        return f"{what}  {runs_store.list_tag(record, status)}".strip()
    # NO RESOLVED STATUS MEANS THE CACHED VERDICT, not list_tag's answer for a
    # missing one. list_bucket() files an unresolved submitted run under
    # FINISHED and list_tag words that "completed" -- correct on /list, where a
    # status is always resolved, and a lie in a menu, where it is not: two runs
    # that had timed out were offered as "completed" beside the fix for the
    # timeout. The cache says what the last look actually found.
    check = record.get("last_check") or {}
    verdict = str(check.get("verdict") or "")
    if verdict:
        return f"{what}  {verdict}".strip()
    return f"{what}  {runs_store.list_tag(record, status)}".strip()


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
    # os.getcwd() as the RUN OUTPUT DIRECTORY, which is a fact about the
    # session, and never as a directory to search. intake.brief() states it and
    # passes it to nothing that lists files -- see its docstring on defect 1a.
    briefed = intake.brief(line, directory, workdir=os.getcwd())
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
    # The run somebody last named on a command line. See runs.Focus: it is set
    # from an argument, never from prose, and it only reorders a menu the
    # completion code had already decided to show.
    focus = runs_store.Focus()
    prompt = ui.Prompt(menu,
                       arguments=lambda cmd: _run_names(agent, cmd, focus))
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
            _dispatch_line(agent, line, focus)
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
    asked = False
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
        asked = _require_api_key()
    agent = build_agent()
    if fake_llm:
        agent.llm = metering.Metered(fakecluster.DevLLM(), agent.telemetry)
    elif asked:
        # Only when a key was pasted a moment ago. See _confirm_new_key.
        _confirm_new_key(agent)
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
