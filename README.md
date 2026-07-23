# GenPipes agent

A Claude-powered assistant for running [GenPipes](https://genpipes.readthedocs.io/) on a Slurm
cluster (built and tested on the Digital Research Alliance of Canada's Rorqual), with one rule
baked into the graph: **nothing reaches the scheduler without a human approving it first.**

```
  ask ──▶ generate ──▶ GATE ──▶ submit ──▶ watch
                        ▲
                        you
```

Generation (`genpipes ... -g cmd.sh`) and read-only commands (`squeue`, `sacct`, `-h`) run freely.
The moment the agent proposes something that actually submits to the cluster — `bash cmd.sh`, or
the `chunk_genpipes.sh` / `submit_genpipes` pair — the graph pauses and shows you exactly what's
about to run. You approve, reject, or send it back with feedback.

It's a thin subclass of [Biomni](https://github.com/snap-stanford/Biomni)'s `A1` agent
(`genpipe_agent.py`), reusing Biomni's own generate/execute nodes and splicing in one extra node —
the gate — rather than reimplementing the agent loop. `genpipes.md` is the grammar document that
teaches the model GenPipes' invocation shape, config layering, and file formats.

## Prerequisites

- An account on a cluster with GenPipes installed as a module (this was built against GenPipes
  v6.1.1 on Rorqual — see the note on cluster-specificity below).
- Python 3.12+.
- An [Anthropic API key](https://console.anthropic.com/).

## Setup

```bash
git clone https://github.com/philobourque/genpipe-prototype
cd genpipe-prototype

module load python/3.12.4      # or whatever gives you Python 3.12+ on your cluster
python -m venv ~/scratch/biomni-venv
source ~/scratch/biomni-venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and paste in your real ANTHROPIC_API_KEY
```

`start_agent.sh` assumes the venv lives at `~/scratch/biomni-venv`. If you put it somewhere else,
edit the `source` line in `start_agent.sh` to match.

## Launch

```bash
./start_agent.sh
```

This drops you into an interactive Python REPL with `agent` already built and the GenPipes grammar
loaded. The banner printed on startup is the full command reference; the short version:

```python
agent.run("run dnaseq germline_snv on my readset, all steps", thread_id="patient-42")
agent.resume("patient-42", approved=True)
agent.resume("patient-42", approved=False, feedback="use steps 6-12 instead")
agent.check("patient-42")
agent.submissions()
```

Name every run (`thread_id`). The name is how you approve it and how you check on it later — runs
can pause for approval and be resumed in a completely separate session, and submitted jobs
obviously outlive the conversation.

By default the agent's working directory (checkpoint database, `runs.tsv`, Biomni's own data
folder) is `~/scratch`. Override with `GENPIPE_AGENT_WORKDIR` if that's not the right place on your
cluster.

### Optional: web UI

`server.py` + `index.html` are a minimal browser front end, useful for demos or one-off exploratory
questions:

```bash
uvicorn server:app --reload
```

**This bypasses the approval gate.** It drives Biomni's own `agent.go()`, not `GenpipeA1`'s gated
`run()`/`resume()`, so nothing pauses before a submission. Use it for generation and exploration
only — anything that might actually submit to the cluster belongs in the CLI (`start_agent.sh`),
not this UI, until it grows its own gate.

## Development: testing without a model or a cluster

Two test suites live in `tests/`, both offline (no Anthropic API call, no GenPipes module, no
Slurm job):

```bash
python tests/test_gate.py            # pure logic: the gate's helper functions in isolation
python tests/test_mock_pipeline.py   # the real graph, end to end, with a scripted fake LLM
```

**`test_gate.py`** builds a bare `GenpipeA1` (skipping `__init__`) and hammers
`_extract_pending_code`, `_is_submission`, and `_build_proposal` directly — the pure functions the
gate's correctness reduces to.

**`test_mock_pipeline.py`** is the one to reach for when changing `genpipe_agent.py` or
`display.py`: it builds the *real* agent via the same `launch_agent.build_agent()` production uses,
swaps in a `FakeLLM` that returns a scripted conversation instead of calling Claude, and drives the
actual LangGraph plumbing — generate → route → **gate** → interrupt → resume → execute → generate →
end — for both an approve and a reject scenario. The approved path really executes a (harmless,
throwaway) stub script, so it's proof the whole round trip works, not just that the state machine
looks right on paper. A full run takes a couple of seconds and costs nothing.

What it does *not* test: whether Claude actually writes correct GenPipes commands from
`genpipes.md`. That needs a real model and a real (small) task, and is worth doing occasionally by
hand — it's not part of the fast loop.

## A note on `genpipes.md`

The grammar document is pinned to GenPipes v6.1.1 and hardcodes Rorqual's cluster ini
(`common_ini/rorqual.ini`). If you're on a different DRAC cluster (Narval, Béluga, Cedar) or a
different GenPipes version, that file needs updating — it is not cluster- or version-agnostic by
design (see its own header for why step counts are deliberately left out and read from `-h`
instead).

## Repo layout

| File | Purpose |
|---|---|
| `genpipe_agent.py` | `GenpipeA1`: the gated graph, run/resume/check/submissions |
| `display.py` | Terminal rendering (`parse()` is UI-agnostic; a future web UI can reuse it) |
| `genpipes.md` | The GenPipes grammar fed to the model as "software" |
| `launch_agent.py` | Builds the agent (`build_agent()`); entry point for `start_agent.sh` |
| `start_agent.sh` | Loads the cluster's Python module, activates the venv, launches the REPL |
| `server.py` / `index.html` | Optional, ungated web UI |
| `tests/` | Offline test suites — see above |
