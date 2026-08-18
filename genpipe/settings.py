"""The persisted settings file, read by the app that writes it.

Stdlib only, like everything below `agent`.

--------------------------------------------------------------------------
THE DEFECT THIS CLOSES
--------------------------------------------------------------------------
The app WRITES .env -- the API key, which provider it is for, which model, and
what to call you -- and until now it never READ it. The file was loaded by
`source "$HERE/.env"` inside start_agent.sh, one line before the interpreter
started, and by nothing else.

That works, on the one path it was written for, and it fails silently on every
other. `python -m genpipe` from an activated venv -- which is what the pty
suite does, and the obvious thing to type once the environment exists -- is
asked to paste an API key it pasted yesterday, every single time, because the
key is sitting in a file two directories away that nothing opened. And /model
answers "No Anthropic key configured yet -- run /key first" about a key that is
configured, which is the same fact reported as a different, wrong one.

It is the same shape as the RAP_ID defect: a dependency on an ambient
environment that somebody else's shell happened to set up, invisible until you
run the thing somewhere slightly different. The fix is the same shape too --
ask for it explicitly, locally, in the process that needs it.

--------------------------------------------------------------------------
WHAT IT DELIBERATELY DOES NOT DO
--------------------------------------------------------------------------
IT NEVER OVERWRITES. A variable already in the environment wins, always. So
start_agent.sh's `source` still decides on the path it owns, an exported
ANTHROPIC_API_KEY in somebody's shell still wins over a stale one in the file,
and running with an explicitly stripped environment stays a way to run with an
explicitly stripped environment.

IT IS NOT dotenv. No interpolation, no `${VAR}` expansion, no multi-line
values, no shell. It reads back what _write_env_var wrote -- `export NAME=value`
-- plus the unexported form and quotes, because .env.example uses the first and
people hand-edit the rest. Anything it does not understand it skips rather than
guesses at.

IT IS NOT A SEARCH. One file: GENPIPE_ENV_FILE if set, else the .env beside the
checkout, which is exactly where _write_env_var puts it. Walking up from the
working directory -- what python-dotenv does by default -- would make which
settings you get depend on which directory you launched from, and the working
directory is already load-bearing here for an unrelated reason (a run's job
list is resolved against it).
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / ".env"

# What the file is allowed to define. An allow-list rather than "whatever is in
# there", because this file is read at startup with no supervision and a .env
# is a plain text file people edit: a stray PATH= or LD_LIBRARY_PATH= line in it
# should not be able to reconfigure the interpreter that is reading it.
#
# The API key variables are named individually for the same reason -- these four
# are the ones KNOWN_PROVIDERS knows how to use, and a fifth appearing here
# would be a fifth thing this app cannot do anything with anyway.
ALLOWED = frozenset({
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY",
    "GENPIPE_LLM_SOURCE", "GENPIPE_LLM_MODEL",
    "GENPIPE_USER",
    "GENPIPE_THEME",
    "GENPIPE_AGENT_WORKDIR", "GENPIPE_CLUSTER",
    "GENPIPE_FAKE", "GENPIPE_FAKE_LLM", "GENPIPE_FAKE_STATE",
    "GENPIPE_TELEMETRY",
})


def path(env=None):
    """The settings file this process reads and writes."""
    env = os.environ if env is None else env
    override = (env.get("GENPIPE_ENV_FILE") or "").strip()
    return Path(override) if override else DEFAULT_PATH


def parse(text):
    """{name: value} from .env text. Never raises; skips what it cannot read.

    Accepts `export NAME=value`, `NAME=value`, and either with the value in
    single or double quotes. A `#` comment line is skipped; a `#` inside a
    value is NOT treated as a comment, because an API key is an opaque string
    and guessing at its interior is how a key gets silently truncated.
    """
    out = {}
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, value = line.partition("=")
        name = name.strip()
        if not sep or not name.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[name] = value
    return out


def load(env=None, only=ALLOWED):
    """Fill `env` from the settings file. Returns the names it set.

    Best-effort by design: an unreadable or absent file is not an error, it is
    the ordinary state of a fresh checkout, and the app's own first-run prompts
    are what create it.
    """
    env = os.environ if env is None else env
    try:
        text = path(env).read_text()
    except OSError:
        return []
    added = []
    for name, value in parse(text).items():
        if only is not None and name not in only:
            continue
        if env.get(name):          # already set, by a shell or by a caller
            continue
        env[name] = value
        added.append(name)
    return added
