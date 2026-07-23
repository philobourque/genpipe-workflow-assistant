import os
import io, contextlib
from pathlib import Path

import display
from genpipe_agent import GenpipeA1

HERE = Path(__file__).resolve().parent
GRAMMAR_PATH = HERE / "genpipes.md"

# Working directory for the agent (checkpoint db, biomni_data/, runs.jsonl, ...).
# Override with GENPIPE_AGENT_WORKDIR if ~/scratch isn't the right place on your
# cluster; defaults to ~/scratch since that's writable and persistent on DRAC.
DEFAULT_WORKDIR = os.environ.get("GENPIPE_AGENT_WORKDIR", os.path.expanduser("~/scratch"))


def _require_api_key():
    """Fail fast, with a clear message, if ANTHROPIC_API_KEY isn't really set.

    Without this, a missing key doesn't surface until the first real LLM call
    -- deep inside Biomni's tool-retriever call in run() -- as a ~25-frame
    stack trace ending in a generic Anthropic SDK TypeError, with nothing in
    it written for a user of this tool.

    Deliberately not inside build_agent(): that function is shared with the
    mock pipeline test, which runs with no real key at all (FakeLLM replaces
    agent.llm before any real call happens), so a hard check there would
    break the test in exactly the environment it's meant to run in. This is
    called only from the two real entry points instead: the CLI below, and
    server.py.
    """
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key or key.startswith("sk-ant-..."):
        print(
            "\nError: ANTHROPIC_API_KEY is not set (or still the placeholder "
            "from .env.example).\n\n"
            "    cp .env.example .env\n"
            "    # edit .env and paste in your real key from "
            "https://console.anthropic.com/\n\n"
            "Then relaunch with ./start_agent.sh so .env actually gets loaded.\n"
        )
        raise SystemExit(1)


def build_agent(path=None, llm="claude-sonnet-4-5-20250929", source="Anthropic"):
    """Construct a fully configured GenpipeA1 agent: the gated graph, with the
    GenPipes grammar registered as software.

    Shared by launch_agent.py (real use, real LLM) and the mock pipeline test
    (fake LLM swapped in after construction) so both exercise the exact same
    construction path -- no separate, drifting "test version" of the setup.
    """
    if path is None:
        path = DEFAULT_WORKDIR

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


if __name__ == "__main__":
    _require_api_key()
    agent = build_agent()
    # Greet the user. Last, so the banner is the final thing on screen.
    display.banner()
