"""`python -m genpipe` -- the way the app is started.

A module rather than a console script so it works from a git checkout with
nothing installed, which is how it runs on the cluster: start_agent.sh loads
the Python module, activates the venv, and runs this.
"""
from .cli import main

if __name__ == "__main__":
    main()
