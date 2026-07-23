#!/bin/bash
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

module purge && module load python/3.12.4
unset PYTHONPATH
source ~/scratch/biomni-venv/bin/activate
source "$HERE/.env"
python -i "$HERE/launch_agent.py"
