#!/bin/bash
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# module purge complains about sticky modules it deliberately leaves loaded
# (StdEnv and friends) -- expected every time, not something a user of this
# tool can act on, so it's silenced here rather than left to look like an error.
module purge >/dev/null 2>&1
module load python/3.12.4
unset PYTHONPATH
source ~/scratch/biomni-venv/bin/activate
# .env doesn't exist yet on a first run -- launch_agent.py prompts for and
# creates it itself, so a missing file here is expected, not an error.
[ -f "$HERE/.env" ] && source "$HERE/.env"
# No -i: launch_agent.py now runs its own command loop and owns the
# interactive session itself, instead of handing off to the Python REPL.
#
# Arguments are passed straight through, which is what makes dev mode reachable:
#
#   ./start_agent.sh --fake              stubbed GenPipes + Slurm, real model
#   ./start_agent.sh --fake --fake-llm   nothing real at all -- no allocation,
#                                        no API key, no cost
#
# GENPIPE_FAKE_STATE picks which canned cluster the stubs present: happy,
# running, failed-oom (the default), or failed-missing-input.
python "$HERE/launch_agent.py" "$@"
