"""A GenPipes assistant: talk to it in English, and nothing reaches Slurm
without your approval.

Import nothing here. `import genpipe` must stay free of biomni, langgraph and
langchain, because CI imports this package with none of them installed -- see
the boundary below.

The modules, in dependency order. Each one depends only on the ones above it:

    slots         what each pipeline and protocol requires; the feature-ini table
    preflight     environment checks -- RAP_ID blocks a submission, JOB_MAIL warns
    runs          the run registry, the job list parser, Slurm state, triage
    gate          the submission gate's rules, as pure functions over command text
    intake        reads a request against slots, and finds what is missing
    display       everything the app prints; parse() is UI-agnostic on purpose
    ui            everything the app reads: prompt box, completion, spinner, paste
    fakecluster   stand-ins for GenPipes, Slurm and the model, for dev mode
    agent         GenpipeA1: A1's graph with the gate spliced into it
    cli           the command loop, the command table, and startup

THE BOUNDARY THAT MATTERS: only `agent` and `cli` import biomni. Everything
above them is standard library and nothing else, which is what lets seven test
suites run on any machine in about two seconds with no agent stack, no API key
and no cluster. Adding a heavy import to a module in the first group is not a
detail -- it takes CI with it. The workflow has a step that checks exactly this.

The grammar the model is given as "software" is genpipes.md, which lives here
beside the code that reads it rather than at the repo root, so cli.GRAMMAR_PATH
resolves wherever the package is installed from.

Entry point:  python -m genpipe   (what start_agent.sh runs)
"""
