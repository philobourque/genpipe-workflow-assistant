#!/bin/bash
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# The supported environment, checked rather than assumed.
#
# This app runs on a cluster with Lmod and GenPipes available as modules. That
# is not incidental: the model's own commands are `module load
# mugqic/genpipes/6.1.1 && genpipes ...` (see genpipe/genpipes.md), so a machine
# with no module system cannot run this even if the Python half installs
# perfectly. Both checks below therefore STOP rather than continue.
#
# What they replace reported the wrong cause. `module` missing wrote
# "module: command not found" to the terminal and the script carried on; the
# venv step then failed on the next line with
#
#   ./start_agent.sh: line 41: python: command not found
#   Could not create a virtual environment at $VENV.
#   Set GENPIPE_VENV to somewhere writable and try again.
#
# -- a true sentence about a directory that was never the problem, pointing at
# a variable that could not have fixed it. An accurate error costs six lines.
# ---------------------------------------------------------------------------
if ! command -v module >/dev/null 2>&1; then
  echo "  No 'module' command here, so the GenPipes environment cannot be loaded." >&2
  echo "  This assistant runs on a cluster with Lmod and GenPipes installed as" >&2
  echo "  modules -- it was built against GenPipes 6.1.1 on the Alliance's" >&2
  echo "  Rorqual. Log in to such a cluster and run it from there." >&2
  exit 1
fi

# module purge complains about sticky modules it deliberately leaves loaded
# (StdEnv and friends) -- expected every time, not something a user of this
# tool can act on, so it's silenced here rather than left to look like an error.
module purge >/dev/null 2>&1
# NOT silenced, unlike the purge: Lmod's own message names the module it could
# not find, which is the half of the diagnosis this script cannot write itself.
if ! module load python/3.12.4; then
  echo >&2
  echo "  Could not load python/3.12.4, which this app is built and tested on." >&2
  echo "  See what this cluster offers with:  module spider python" >&2
  echo "  If 3.12.4 is genuinely unavailable here, edit the 'module load' line" >&2
  echo "  in start_agent.sh -- but 3.12 is the only version the suites run on." >&2
  exit 1
fi
unset PYTHONPATH

# Belt and braces: `module load` can return 0 and still leave no interpreter on
# PATH (a modulefile that resolved but whose install is broken or unreadable).
# The venv is built by name below, so an absent `python` has to be caught here
# or it resurfaces as the writability error this block exists to stop telling.
if ! command -v python >/dev/null 2>&1; then
  echo "  python/3.12.4 loaded but put no 'python' on PATH." >&2
  echo "  The module is present and its interpreter is not; this is a problem" >&2
  echo "  with the cluster's module rather than with this app. Report it to" >&2
  echo "  your support desk, quoting:  module load python/3.12.4 && which python" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# The virtual environment, created on first launch rather than assumed.
#
# This used to be one `source ~/scratch/biomni-venv/bin/activate`, which was
# true on the one cluster it was built on and false everywhere else. On Narval
# it failed like this, and the second half is what made it confusing:
#
#   ./start_agent.sh: line 10: .../biomni-venv/bin/activate: No such file or directory
#   ModuleNotFoundError: No module named 'biomni'
#
# A failing `source` is not fatal to bash, so the app launched anyway on the
# system interpreter and produced a missing-module traceback -- which reads as a
# broken install rather than as an absent venv.
#
# ~/scratch is CLUSTER-LOCAL on the Digital Alliance: it is not shared between
# Rorqual, Narval, Béluga and Cedar, so a venv built on one genuinely does not
# exist on the next. That is not something anyone should have to discover from a
# traceback. So: create it, install into it, say so while it happens. Once.
# Every later launch takes the fast path and prints nothing.
#
# GENPIPE_VENV overrides the location, for a cluster where ~/scratch is not the
# right place or is not writable.
# ---------------------------------------------------------------------------
VENV="${GENPIPE_VENV:-$HOME/scratch/biomni-venv}"

if [ ! -f "$VENV/bin/activate" ]; then
  echo
  echo "  Setting up the environment. This happens once per cluster."
  echo "  $VENV"
  echo
  if ! python -m venv "$VENV"; then
    echo "  Could not create a virtual environment at $VENV." >&2
    echo "  Set GENPIPE_VENV to somewhere writable and try again." >&2
    exit 1
  fi
  # shellcheck disable=SC1091
  source "$VENV/bin/activate"
  python -m pip install --quiet --upgrade pip
  if ! python -m pip install --quiet -r "$HERE/requirements.txt"; then
    echo "  Installing the dependencies failed -- see above." >&2
    echo "  By hand:  source $VENV/bin/activate && pip install -r $HERE/requirements.txt" >&2
    exit 1
  fi
  echo "  Done."
  echo
else
  # shellcheck disable=SC1091
  source "$VENV/bin/activate"
fi

# A venv that exists but predates a requirements.txt change is the other half of
# the same problem, and it fails at import time rather than at launch. One cheap
# import catches it, and reinstalling into a venv that already has everything is
# fast enough to be worth doing unconditionally when it does not.
if ! python -c "import biomni, langgraph" >/dev/null 2>&1; then
  echo "  Updating the environment to match requirements.txt…"
  python -m pip install --quiet -r "$HERE/requirements.txt" || {
    echo "  Could not install the dependencies -- see above." >&2
    exit 1
  }
fi

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
# running, failed-oom (the default), failed-missing-input, or dying.
# -m rather than a path: the app is a package now, and running it by file would
# put genpipe/ on sys.path instead of the checkout, so `from . import display`
# would have no package to be relative to. PYTHONPATH rather than a cd, because
# the working directory is load-bearing -- the app registers a run's job list
# relative to where it was launched, so this must stay wherever you started it.
PYTHONPATH="$HERE" python -m genpipe "$@"
