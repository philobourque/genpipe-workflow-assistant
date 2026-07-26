#!/usr/bin/env bash
# Entry point for the three test cases. See README.md.
#
#   ./testcases/run.sh 1              interface, offline, free
#   ./testcases/run.sh 2              real cluster, CIT data, a few core-hours
#   ./testcases/run.sh 3 --confirm    production, real allocation, hours
#
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"

# The venv is where biomni and pyte live. Case 1 needs pyte for the pty screen;
# case 2 needs the agent stack. The stdlib-only suites in tests/ deliberately do
# not, which is why CI can run those and not these.
PY="${GENPIPE_PYTHON:-$HOME/scratch/biomni-venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"

# A shell that has `module load mugqic/genpipes` in its history exports a
# PYTHONPATH pointing at GenPipes' own Python 3.13 site-packages and stdlib.
# The venv runs 3.12, so importing anything then explodes with "SRE module
# mismatch" -- a failure that looks like a broken test and is really a leaked
# variable. GenPipes is invoked through `module load ... && ...` subshells
# anyway, so nothing here needs it.
unset PYTHONPATH PYTHONHOME

case "${1:-}" in
  1)
    echo "Case 1 -- interface, offline. Nothing will reach a cluster."
    exec "$PY" "$HERE/case1_interface.py" "${@:2}"
    ;;
  2)
    echo "Case 2 -- real cluster, CIT data. This spends a few core-hours."
    exec "$PY" "$HERE/case2_cluster.py" "${@:2}"
    ;;
  3)
    if [[ " ${*:2} " != *" --confirm "* ]]; then
      cat <<'EOF'
Case 3 -- production.

This runs a real pipeline on real data with production resources. It costs
real allocation and takes hours. It is not something to start by accident.

Read testcases/03-production.md, then re-run with --confirm:

    ./testcases/run.sh 3 --confirm --pipeline rnaseq --readset <path> ...

Nothing has been submitted.
EOF
      exit 1
    fi
    echo "Case 3 -- production. Confirmed."
    # Case 3 is case 2's machinery without the CIT overlay and without a step
    # cap. Keeping it as one code path means the production run exercises the
    # same script the cheap run does, rather than a second one that drifted.
    exec "$PY" "$HERE/case2_cluster.py" --production "${@:2}"
    ;;
  *)
    sed -n '2,8p' "$HERE/run.sh"
    exit 1
    ;;
esac
