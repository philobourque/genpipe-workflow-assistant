#!/bin/bash
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# module purge complains about sticky modules it deliberately leaves loaded
# (StdEnv and friends) -- expected every time, not something a user of this
# tool can act on, so it's silenced here rather than left to look like an error.
module purge >/dev/null 2>&1
module load python/3.12.4
unset PYTHONPATH
source ~/scratch/biomni-venv/bin/activate
# .env doesn't exist yet on a first run -- genpipe/cli.py prompts for and
# creates it itself, so a missing file here is expected, not an error.
[ -f "$HERE/.env" ] && source "$HERE/.env"
# No -i: genpipe/cli.py runs its own command loop and owns the interactive
# session itself, instead of handing off to the Python REPL.
#
# Arguments are passed straight through, which is what makes dev mode reachable:
#
#   ./start_agent.sh --fake              stubbed GenPipes + Slurm, real model
#   ./start_agent.sh --fake --fake-llm   nothing real at all -- no allocation,
#                                        no API key, no cost
#
# GENPIPE_FAKE_STATE picks which canned cluster the stubs present: happy,
# running, failed-oom (the default), or failed-missing-input.
# -m rather than a path: the app is a package now, and running it by file would
# put genpipe/ on sys.path instead of the checkout, so `from . import display`
# would have no package to be relative to. PYTHONPATH rather than a cd, because
# the working directory is load-bearing -- the app registers a run's job list
# relative to where it was launched, so this must stay wherever you started it.
PYTHONPATH="$HERE" python -m genpipe "$@"
