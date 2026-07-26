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
- An API key for an LLM provider. This tool is built and tested against Claude (Anthropic), but
  the first-launch prompt also recognizes OpenAI, Gemini, and Groq keys. OpenAI/Gemini/Groq need
  `langchain-openai` installed too (`pip install langchain-openai`) — only `langchain-anthropic` is
  in `requirements.txt` by default.

## Setup

```bash
git clone https://github.com/philobourque/genpipe-workflow-assistant
cd genpipe-workflow-assistant

module load python/3.12.4      # or whatever gives you Python 3.12+ on your cluster
python -m venv ~/scratch/biomni-venv
source ~/scratch/biomni-venv/bin/activate
pip install -r requirements.txt
```

`start_agent.sh` assumes the venv lives at `~/scratch/biomni-venv`. If you put it somewhere else,
edit the `source` line in `start_agent.sh` to match.

No API key setup needed here — the first launch asks for it.

## Launch

```bash
./start_agent.sh
```

The banner prints first — who you are, which model is behind it, where this copy lives — and if no
key is configured yet you're then prompted below it to paste one in (masked, with the first four
characters left visible so you can tell the paste landed). The provider is guessed from the key's
shape (Anthropic/OpenAI/Gemini/Groq), or asked for if it isn't recognized. It's saved to a
gitignored `.env` in the repo root along with which provider/model it's for, so every launch after
that starts straight away with no prompt.

This drops you into the assistant's own interface, with the GenPipes grammar already loaded — not a
Python interpreter. You get a prompt box:

```
 ──────────────────────────────────────────────────────────────
  ❯ run dnaseq germline_snv on my readset, all steps
 ──────────────────────────────────────────────────────────────
```

Type a task in plain English and it runs — you're asked to name it first. Anything starting with
`/` is a command instead, and the command list appears live as you type: press `/` to see all of
them, `Tab` to complete, `↑`/`↓` to pick one. Any unambiguous abbreviation works, so `/appr` is
`/approve`. `↑` at an empty prompt walks back through the session's history, `Ctrl+C` clears the
line, `Ctrl+D` leaves.

```
deciding    /approve patient-42
            /reject  patient-42 use steps 6-12 instead
watching    /list
            /check   patient-42
            /jobs    patient-42 [failed]
            /history
fixing      /why     patient-42 [is the memory limit too low?]
            /cancel  patient-42
            /track   some-other-run /path/to/Pipeline.protocol.job_list.TIMESTAMP
setup       /where
            /model   [provider [model-name]]
            /key
            /help
            /exit
```

While a run is working, a spinner sits below the transcript with the elapsed time, and it says what
the agent is currently doing — `generating the script`, `asking Slurm`, `submitting` — rather than a
static "thinking". Output scrolls up past it as it arrives. `Ctrl+C` stops a run without submitting
anything.

Pasting is safe: multi-line pastes are folded onto the input line instead of the first newline
submitting a half-finished command.

## Runs and jobs

The distinction runs through the whole tool:

- A **run** is one GenPipes invocation — the thing you named, the command you approved, the `cmd.sh`
  GenPipes generated. It is a unit of intent. You approve, cancel and diagnose runs.
- A **job** is one Slurm job inside that run. GenPipes turns a single run into dozens or hundreds of
  them, one per step per sample. A job is a unit of execution, with its own id, state and log file.

"Did it work?" is a question about a run. "What broke?" is only ever answerable about a job. So
`/check` reports the run (GenPipes' own `log_report`, drawn as a bar) and `/jobs` reports each job
(from `sacct`, grouped by step, since a failure is nearly always one step failing across many
samples rather than one unlucky job).

A run's life is `held → submitted → gone`:

- **held** — stopped at the gate, nothing submitted. A run is recorded here, *before* anything
  reaches Slurm, so a decision you left behind survives closing the terminal. Relaunching announces
  it; `/list` shows it first.
- **submitted** — on the scheduler, artifacts on disk.
- **gone** — the job list file is no longer on disk (a scratch purge, manual cleanup). The run drops
  out of `/list` but nothing is deleted: `/history` still shows it, marked `gone`, along with
  anything `/why` concluded about it.

Name every run. The name is how you approve it and how you check on it later. You are offered a name
derived from the task, pre-filled and editable, so Enter accepts it. If the name is already taken it
is quietly advanced (`patient-42` → `patient-42-2`) — reusing one would replace that run's stored
conversation, including a pending approval.

`/why <name>` is the only command that costs a model call. It works in two stages, deliberately
visible as two: first it asks Slurm which jobs failed and reads those specific logs off disk, prints
that as evidence, and only then asks the model to explain the cause. A GenPipes run has hundreds of
`.o` files, and a model told to "go look" burns context rediscovering what one `sacct` call already
knows. The investigation runs on its own thread, so it can never disturb the run it is diagnosing.

`/track <name> <job_list_path>` registers a run you launched outside the agent entirely, so
`/check`/`/jobs`/`/why` can find it by name with no prior conversation required.

`/model` alone shows the current provider/model; `/model <provider>` (`anthropic`/`openai`/`gemini`/
`groq`) switches to it using that provider's already-configured key, and `/model <provider>
<model-name>` picks a specific model. `/key` adds or rotates a key for a provider — same prompt as
first launch, applied immediately, no restart needed.

By default the agent's working directory (checkpoint database, `runs.jsonl`, Biomni's own data
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

## Dev mode: the whole app, with nothing real behind it

```bash
./start_agent.sh --fake              # stubbed GenPipes + Slurm, real model
./start_agent.sh --fake --fake-llm   # nothing real at all: no allocation, no API key, no cost
```

`fakecluster.py` writes a directory of small stubs — `module`, `genpipes`, `sbatch`, `sacct`,
`scancel`, `squeue` — and puts it at the front of `PATH`. Everything downstream then runs for real:
GenPipes "generates" a `cmd.sh`, running it writes a `job_output/` tree with per-job `.o` logs and a
`*.job_list.*`, and `sacct` answers about those job ids. The registry, the job parser, the triage and
every renderer run unmodified. `--fake-llm` adds a scripted stand-in for the model, so the entire
interface — gate, approve, check, jobs, why, cancel — can be clicked through on a laptop.

Dev mode says so on every launch, in amber, under the banner. A simulation you can mistake for the
real thing is worse than no simulation.

`GENPIPE_FAKE_STATE` picks which cluster the stubs present:

| state | what it looks like |
|---|---|
| `happy` | every job COMPLETED |
| `running` | a mix of RUNNING and PENDING |
| `failed-oom` | one step OUT_OF_MEMORY, with a Java heap log (the default) |
| `failed-missing-input` | a missing FASTQ, so diagnosis can't cheat by memorising one signature |

The stubs **validate**. `genpipes` rejects an unknown pipeline, an unknown protocol, a malformed `-s`
range, a missing readset or a missing ini, and fails with a GenPipes-shaped error. So running
`--fake` with a *real* model tests whether Claude writes correct GenPipes commands from `genpipes.md`
— which used to be possible only by hand, on a cluster, with an allocation.

## Testing

Five suites in `tests/`, all offline. The split is by what they need, not by what they cover:

```bash
# Run anywhere, no dependencies beyond the standard library. These are the CI set.
python tests/test_gate_rules.py    # the gate's invariants — the one test that must never go red
python tests/test_runs.py          # the run registry's lifecycle, and the job layer's parsing
python tests/test_fakecluster.py   # the fake cluster itself, including that it rejects bad commands

# Need the venv (biomni, langgraph, pyte). Run before a release.
python tests/test_lifecycle.py     # the real agent + real gate + fake cluster, whole arc of a run
python tests/test_app.py           # the real app in a pty, asserting on the rendered screen
python tests/test_gate.py          # the original gate helpers, via GenpipeA1's own methods
python tests/test_mock_pipeline.py # the original approve/reject round trip
```

The first three run on every push via GitHub Actions (`.github/workflows/tests.yml`). That is
possible because `gate_rules.py`, `runs.py` and `fakecluster.py` deliberately import nothing but the
standard library — the safety-critical decision ("does this code submit to a scheduler?") is checked
in about two seconds, with no pinned agent stack, no API key and no cluster.

**`test_gate_rules.py`** is two lists — code that must be gated, and code that must not — plus the
rule about which direction a mistake may fall in. A false negative puts unapproved work on a real
cluster; a false positive costs a rejection. Both have happened in production and both are in the
list as the case that caused them.

**`test_lifecycle.py`** drives the actual `GenpipeA1`, the actual LangGraph gate and the actual
SQLite checkpoint against the fake cluster, with a scripted model. It covers what only exists once
the pieces are assembled — including approving a run **from a different process** than the one that
held it, which is the gate's central promise and was previously untested.

**`test_app.py`** launches the app in a pty, types at it, and asserts on the screen reconstructed
with `pyte` — the banner, the completion menu, Tab, the pre-filled run name, the HOLD box, and that
a pasted multi-line string does not execute its first line.

## A note on `genpipes.md`

The grammar document is pinned to GenPipes v6.1.1 and hardcodes Rorqual's cluster ini
(`common_ini/rorqual.ini`). If you're on a different DRAC cluster (Narval, Béluga, Cedar) or a
different GenPipes version, that file needs updating — it is not cluster- or version-agnostic by
design (see its own header for why step counts are deliberately left out and read from `-h`
instead).

## Repo layout

| File | Purpose | Needs |
|---|---|---|
| `genpipe_agent.py` | `GenpipeA1`: the gated graph, and run/resume/check/jobs/why/cancel | biomni |
| `gate_rules.py` | The gate's decision logic, as pure functions | stdlib |
| `runs.py` | Runs and jobs: the registry, the job parser, triage, naming | stdlib |
| `display.py` | Rendering (`parse()` is UI-agnostic; a future web UI can reuse it) | stdlib |
| `ui.py` | The terminal input side: prompt box, live completion, spinner, paste | stdlib |
| `fakecluster.py` | Stubbed GenPipes + Slurm + model, for dev mode and the tests | stdlib |
| `genpipes.md` | The GenPipes grammar fed to the model as "software" | — |
| `launch_agent.py` | Builds the agent, owns the command table and the loop (`_repl()`) | biomni |
| `start_agent.sh` | Loads the cluster's Python module, activates the venv, launches the app | — |
| `server.py` / `index.html` | Optional, ungated web UI | fastapi |
| `tests/` | Five offline suites — see above | mixed |

The "Needs" column is load-bearing, not incidental. Keeping `gate_rules.py`, `runs.py` and
`fakecluster.py` free of `biomni` is what lets the gate's invariants, the registry's lifecycle and
the fake cluster all be verified on every push in seconds. If one of them grows a heavy import, CI
stops covering the thing that matters most — so `tests.yml` has a step that imports all three and
fails if it can't.
