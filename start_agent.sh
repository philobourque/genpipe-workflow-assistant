#!/bin/bash
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# THE WORKING DIRECTORY IS NEVER CHANGED, and that is the whole launch contract.
# The `cd` above runs inside a command substitution, so it moves a subshell and
# nothing else. Everything below addresses the checkout as "$HERE" for exactly
# that reason -- the directory you launch from is the directory the GenPipes run
# is written into, and the app resolves a submission's job list against it. So:
#
#     cd /path/to/where/the/run/belongs
#     ~/genpipe-workflow-assistant/start_agent.sh
#
# Adding a `cd` here would silently move every future run into the checkout.
# tests/test_launcher.py asserts there isn't one.

# Skip the environment checks below when the caller has asked for the stubbed
# cluster: dev mode replaces module/genpipes/sbatch with its own stubs after
# this script has handed off, so checking for the real ones would refuse to
# launch the one mode that is designed to run without them.
FAKE=0
for arg in "$@"; do
  case "$arg" in
    --fake|--fake-llm) FAKE=1 ;;
  esac
done
[ -n "${GENPIPE_FAKE:-}${GENPIPE_FAKE_LLM:-}" ] && FAKE=1

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
HAVE_MODULE=0
command -v module >/dev/null 2>&1 && HAVE_MODULE=1

if [ "$HAVE_MODULE" = 0 ] && [ "$FAKE" = 0 ]; then
  echo "  No 'module' command here, so the GenPipes environment cannot be loaded." >&2
  echo "  GenPipes Assistant is developed and validated on the Digital Research" >&2
  echo "  Alliance of Canada's Rorqual cluster with GenPipes 6.1.1. No other" >&2
  echo "  environment is validated. Log in to Rorqual and run it from there." >&2
  echo >&2
  echo "  To drive the interface with nothing real behind it, on any machine:" >&2
  echo "      python -m genpipe --fake --fake-llm" >&2
  exit 1
fi

# module purge complains about sticky modules it deliberately leaves loaded
# (StdEnv and friends) -- expected every time, not something a user of this
# tool can act on, so it's silenced here rather than left to look like an error.
#
# It does not undo `module use`: MODULEPATH survives a purge, so the MUGQIC tree
# a GenPipes-configured profile added is still visible to the check below.
if [ "$HAVE_MODULE" = 1 ]; then
  module purge >/dev/null 2>&1
fi

# ---------------------------------------------------------------------------
# GenPipes itself, checked before a single model call is paid for.
#
# The `module` check above proves there is a module system. It proves nothing
# about whether GenPipes is IN it, and those are different failures with the
# same symptom at very different distances from the cause. Without this, a
# profile missing `module use $MUGQIC_INSTALL_HOME/modulefiles` gets: launch,
# name prompt, key prompt, a paid model turn, a generated command, the approval
# gate, an approval -- and only then
#
#   Lmod has detected the following error: The following module(s) are unknown:
#   "mugqic/genpipes/6.1.1"
#
# after the one irreversible decision in the product had already been made. The
# check costs ~0.9s on Rorqual and answers the question the failure asks.
#
# `module -t avail <exact>` rather than `module load`: it exits 0 either way and
# prints the module's name only when it resolves, so the test is on output and
# nothing is left loaded in this shell for the venv to inherit.
# ---------------------------------------------------------------------------
GENPIPES_MODULE="mugqic/genpipes/6.1.1"
if [ "$HAVE_MODULE" = 1 ] && [ "$FAKE" = 0 ] && [ -z "${GENPIPE_SKIP_GENPIPES_CHECK:-}" ]; then
  if ! module -t avail "$GENPIPES_MODULE" 2>&1 | grep -qF "$GENPIPES_MODULE"; then
    echo >&2
    echo "  GenPipes 6.1.1 is not visible to 'module' here." >&2
    echo >&2
    echo "  Every command this assistant generates begins" >&2
    echo "  'module load $GENPIPES_MODULE', so nothing it produces can run" >&2
    echo "  until that resolves. This assistant does not configure GenPipes;" >&2
    echo "  it uses the GenPipes environment your shell already provides." >&2
    echo >&2
    echo "  Configure GenPipes for your Alliance account first -- see the" >&2
    echo "  official setup instructions at https://genpipes.readthedocs.io/" >&2
    echo "  Then confirm with:" >&2
    echo "      module load $GENPIPES_MODULE" >&2
    echo >&2
    echo "  To launch anyway:  GENPIPE_SKIP_GENPIPES_CHECK=1 $0" >&2
    exit 1
  fi
fi

# The scheduler commands the app itself runs -- not the generated ones. sbatch
# is reached through the approved script; squeue and sacct are what /check,
# /jobs and /diagnose read a run's real state from (genpipe/runs.py).
#
# A WARNING rather than a stop, unlike the two above: with none of these the
# assistant still generates, still gates, still reports on runs it has records
# of. Only the scheduler half goes quiet. Refusing to launch over that would
# block honest read-only use on a machine where sbatch simply is not installed.
if [ "$FAKE" = 0 ]; then
  MISSING=""
  for cmd in sbatch squeue sacct; do
    command -v "$cmd" >/dev/null 2>&1 || MISSING="$MISSING $cmd"
  done
  if [ -n "$MISSING" ]; then
    echo >&2
    echo "  Slurm command(s) not on PATH:$MISSING" >&2
    echo "  Submission and run monitoring (/check, /jobs, /diagnose) need them." >&2
    echo "  Everything else works. Continuing." >&2
    echo >&2
  fi
fi

# ---------------------------------------------------------------------------
# Python 3.12, which is the version contract and not merely a default.
#
# 3.12 is the only version tests/ and CI run on, and python/3.12.4 is what
# Rorqual's software stack calls its 3.12 -- the validated combination, and the
# reason this can have a default at all.
#
# GENPIPE_PYTHON_MODULE exists as an escape hatch, not as a portability claim:
# no environment other than Rorqual with GenPipes 6.1.1 has been validated. It
# is a variable BECAUSE the alternative was an error message telling people to
# edit this file, which contradicted the README's promise that they never have
# to and turned a one-line difference into a local source change that every
# `git pull` would fight.
# ---------------------------------------------------------------------------
PYTHON_MODULE="${GENPIPE_PYTHON_MODULE:-python/3.12.4}"
# NOT silenced, unlike the purge: Lmod's own message names the module it could
# not find, which is the half of the diagnosis this script cannot write itself.
if [ "$HAVE_MODULE" = 1 ] && ! module load "$PYTHON_MODULE"; then
  echo >&2
  echo "  Could not load $PYTHON_MODULE, which this app is built and tested on." >&2
  echo "  On Rorqual it should be there; see what is offered with:" >&2
  echo "      module spider python" >&2
  echo "  GENPIPE_PYTHON_MODULE overrides the name if you need it to, but only" >&2
  echo "  Python 3.12 is tested and only Rorqual is validated." >&2
  exit 1
fi
unset PYTHONPATH

# The interpreter the venv is BUILT with. Once it is activated, `python` is the
# venv's own and this variable is not used again.
#
# Two names because there are two ways in. On a cluster the module above put a
# bare `python` on PATH and that is the one to use. Off-cluster -- which is only
# reachable in dev mode, since every other path stopped above -- there usually
# is no `python`, only `python3`, and falling back to it is the difference
# between `--fake --fake-llm` working on a laptop and a "command not found" for
# a name nobody asked for.
PY=""
for candidate in python python3; do
  command -v "$candidate" >/dev/null 2>&1 && { PY="$candidate"; break; }
done

# Belt and braces: `module load` can return 0 and still leave no interpreter on
# PATH (a modulefile that resolved but whose install is broken or unreadable).
# The venv is built by name below, so an absent interpreter has to be caught
# here or it resurfaces as the writability error this block exists to stop
# telling.
if [ -z "$PY" ]; then
  if [ "$HAVE_MODULE" = 1 ]; then
    echo "  $PYTHON_MODULE loaded but put no 'python' on PATH." >&2
    echo "  The module is present and its interpreter is not; this is a problem" >&2
    echo "  with the cluster's module rather than with this app. Report it to" >&2
    echo "  your support desk, quoting:  module load $PYTHON_MODULE && which python" >&2
  else
    echo "  No python or python3 on PATH. This app needs Python 3.12." >&2
  fi
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
# What requirements.txt looked like when this venv was last installed into. See
# the freshness block below for why a hash and not a timestamp.
STAMP="$VENV/.genpipe-requirements-sha256"

requirements_hash() {
  python - "$HERE/requirements.txt" <<'PY' 2>/dev/null
import hashlib, sys
with open(sys.argv[1], "rb") as fh:
    print(hashlib.sha256(fh.read()).hexdigest())
PY
}

install_requirements() {
  python -m pip install --quiet -r "$HERE/requirements.txt" || return 1
  # Written only after a successful install, so an interrupted or failed one
  # leaves no stamp and the next launch tries again rather than believing it.
  requirements_hash > "$STAMP" 2>/dev/null
  return 0
}

if [ ! -f "$VENV/bin/activate" ]; then
  echo
  echo "  Setting up the environment. This happens once per cluster."
  echo "  $VENV"
  echo
  if ! "$PY" -m venv "$VENV"; then
    echo "  Could not create a virtual environment at $VENV." >&2
    echo "  Set GENPIPE_VENV to somewhere writable and try again." >&2
    exit 1
  fi
  # shellcheck disable=SC1091
  source "$VENV/bin/activate"
  python -m pip install --quiet --upgrade pip
  if ! install_requirements; then
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

# ---------------------------------------------------------------------------
# A venv that exists but predates a requirements.txt change: the other half of
# the same problem, and it fails at import time rather than at launch.
#
# THE HASH IS THE TEST, and the import is only a second trigger. This used to be
# `python -c "import biomni, langgraph"` alone, which answers a narrower question
# than the one being asked: it catches a dependency that is absent, and misses
# every dependency whose PIN moved and every new package that is neither of
# those two. Every pin in requirements.txt except those two is invisible to that
# import -- langchain-anthropic among them, whose own comment in that file
# records the release where its pin was wrong and nothing noticed.
#
# So: the sha256 of requirements.txt, stored in the venv it was installed into.
# It changes when and only when the file does, on any machine, with no clock and
# no network involved. The import check stays as well, because a venv can also
# be broken without requirements.txt having moved at all (an interrupted
# install, a purged scratch directory).
# ---------------------------------------------------------------------------
WANT="$(requirements_hash)"
HAVE="$(cat "$STAMP" 2>/dev/null)"
if [ "$WANT" != "$HAVE" ] || ! python -c "import biomni, langgraph" >/dev/null 2>&1; then
  echo "  Updating the environment to match requirements.txt…"
  if ! install_requirements; then
    echo "  Could not install the dependencies -- see above." >&2
    exit 1
  fi
fi

# .env doesn't exist yet on a first run -- genpipe/cli.py prompts for and
# creates it itself, so a missing file here is expected, not an error. It lives
# beside the checkout, never in the directory you launched from: genpipe/
# settings.py resolves it from the package's own location, so the same key is
# found from whichever project directory you start in.
[ -f "$HERE/.env" ] && source "$HERE/.env"
# No -i: genpipe/cli.py runs its own command loop and owns the interactive
# session itself, instead of handing off to the Python REPL.
#
# Arguments are passed straight through, which is what makes dev mode reachable:
#
#   start_agent.sh --fake              stubbed GenPipes + Slurm, real model
#   start_agent.sh --fake --fake-llm   nothing real at all -- no allocation,
#                                      no API key, no cost
#
# GENPIPE_FAKE_STATE picks which canned cluster the stubs present: happy,
# running, failed-oom (the default), failed-missing-input, or dying.
# -m rather than a path: the app is a package now, and running it by file would
# put genpipe/ on sys.path instead of the checkout, so `from . import display`
# would have no package to be relative to. PYTHONPATH rather than a cd, because
# the working directory is load-bearing -- the app registers a run's job list
# relative to where it was launched, so this must stay wherever you started it.
PYTHONPATH="$HERE" python -m genpipe "$@"
