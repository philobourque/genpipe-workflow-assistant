"""`python -m genpipe` -- the way the app is started.

A module rather than a console script so it works from a git checkout with
nothing installed, which is how it runs on the cluster: start_agent.sh loads
the Python module, activates the venv, and runs this.
"""
# BEFORE cli, and that ordering is the point. .env carries which model this
# session uses and which palette (GENPIPE_THEME), and display.py resolves its
# colours at import time -- so the file has to be read before the first import
# that reads the environment, not after. settings.load() never overwrites a
# variable that is already set, so start_agent.sh's own `source .env` and an
# exported key in somebody's shell both still win.
from . import settings

settings.load()

from .cli import main  # noqa: E402 -- must follow settings.load(); see above

if __name__ == "__main__":
    main()
