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
    agent = build_agent()
    # Greet the user. Last, so the banner is the final thing on screen.
    display.banner()
