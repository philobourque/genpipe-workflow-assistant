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
(`genpipe/agent.py`), reusing Biomni's own generate/execute nodes and splicing in one extra node —
the gate — rather than reimplementing the agent loop. `genpipe/genpipes.md` is the grammar document that
teaches the model GenPipes' invocation shape, config layering, and file formats.

## Prerequisites

This runs on a cluster, not on a laptop. The commands the agent generates are GenPipes commands,
loaded from a module tree; `start_agent.sh` checks for that environment and stops with a specific
error rather than starting without it.

- **A cluster with Lmod and GenPipes installed as modules.** Built and tested against GenPipes
  v6.1.1 on the Digital Research Alliance of Canada's Rorqual — see the note on
  cluster-specificity below.
- **Python 3.12.** `start_agent.sh` loads `python/3.12.4`, and 3.12 is the only version the test
  suites run on (CI included). Newer versions are untested here rather than known-broken.
- **An API key for an LLM provider.** Built and tested against Claude (Anthropic); the first-launch
  prompt also recognizes OpenAI, Gemini and Groq keys. Nothing extra to install per provider —
  `requirements.txt` already carries what all four need.

### The GenPipes environment

The agent does not load GenPipes at launch. Every command it generates carries its own
`module load mugqic/genpipes/6.1.1 && …` prefix, in its own subshell — that boundary is part of the
grammar it is given (`genpipe/genpipes.md`). What your login shell has to supply is the module tree
those commands load from, plus the two variables GenPipes interpolates straight into every `sbatch`
line without ever validating them.

In your `~/.bash_profile`, with **your own** allocation and address substituted in:

```bash
export MUGQIC_INSTALL_HOME=/cvmfs/soft.mugqic/CentOS6
module use $MUGQIC_INSTALL_HOME/modulefiles

export RAP_ID=rrg-yourgroup-ab        # your own Alliance allocation
export JOB_MAIL=you@example.com       # your own address for job notifications
```

What each of those is for, and what happens without it — note that the last one is graded
differently on purpose (`genpipe/preflight.py`):

| What | Status | Why |
|---|---|---|
| `MUGQIC_INSTALL_HOME` + `module use` | **required** | without them `module load mugqic/genpipes/6.1.1` resolves to nothing and every generated command dies on its first token |
| `RAP_ID` | **required** | the cluster ini puts `-A $RAP_ID` on every job, so an unset one has Slurm reject the entire run *after* you approved it. The agent checks at startup and at the gate, and **refuses to offer approval** without it |
| `JOB_MAIL` | optional | addresses notification mail and nothing else. Missing or misspelled, it is a warning that never blocks a submission |

Loading `mugqic/genpipes` from your profile is normal and fine, but be aware it exports a
`PYTHONPATH` pointing at GenPipes' own Python 3.13 standard library. `start_agent.sh` unsets that
before activating the 3.12 venv; if you ever run `python -m genpipe` by hand, do the same.

## Setup

```bash
git clone https://github.com/philobourque/genpipe-workflow-assistant
cd genpipe-workflow-assistant
./start_agent.sh
```

That is the whole install — **there is no separate `pip install` step, and nothing inside
`start_agent.sh` needs editing.** On its first launch it loads `python/3.12.4`, creates a virtual
environment, installs `requirements.txt` into it, and says so while it happens:

```
  Setting up the environment. This happens once per cluster.
  /home/you/scratch/biomni-venv
```

Every launch after that takes the fast path and prints nothing. It also re-installs by itself if a
`requirements.txt` change ever leaves the venv behind.

The venv defaults to `~/scratch/biomni-venv`. On the Alliance `~/scratch` is **cluster-local** — it
is not shared between Rorqual, Narval, Béluga and Cedar — so this is genuinely once per cluster
rather than once per account. Set `GENPIPE_VENV` to put it somewhere else, on a cluster where
`~/scratch` is not the right place or is not writable:

```bash
GENPIPE_VENV=$HOME/projects/def-yourgroup/$USER/biomni-venv ./start_agent.sh
```

### Where you launch it from matters

`start_agent.sh` does not change directory, and that is deliberate: **the directory you launch from
is the run's output directory.** GenPipes writes `job_output/`, `trim/`, `alignment/` and the rest
there, and it is where the agent looks for a run's job list afterwards. So launch it from the
directory the run belongs in, not from the checkout — a run started somewhere else has to be
adopted with `/track` before `/check` can find it. `/where` prints every path the current session
is actually using.

## Launch

```bash
./start_agent.sh
```

The banner prints first — who you are, which model is behind it, where this copy lives — and if no
key is configured yet you're then prompted below it to paste one in (masked, with the first four
characters left visible so you can tell the paste landed). The provider is guessed from the key's
shape (Anthropic/OpenAI/Gemini/Groq), or asked for if it isn't recognized. It's saved to a
gitignored `.env` in the repo root (mode `0600`) along with which provider/model it's for, so every
launch after that starts straight away with no prompt. A key pasted here is checked against the
provider once, immediately, so a typo or an expired key is reported at the prompt that caused it
rather than from inside the agent a turn later.

The key never has to go anywhere near the source. If you'd rather set one up non-interactively —
or want to see every variable the app reads — copy [`.env.example`](.env.example) to `.env` and fill
it in; it documents the API-key variables, `GENPIPE_AGENT_WORKDIR`, `GENPIPE_THEME` and the dev-mode
switches. `/key` adds or rotates a key later without a restart.

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
`/approve`. `↑` at an empty prompt walks back through the session's history, `Ctrl+D` leaves.

```
talking     /new
            /verbose [off]
deciding    /approve  patient-42
            /modify   patient-42 [use steps 6-12 instead]
            /reject   patient-42 [why]
watching    /list
            /check    patient-42 | all
            /monitor  patient-42 [seconds]
            /jobs     patient-42 [failed]
            /history
fixing      /diagnose patient-42 [is the memory limit too low?]
            /hold     patient-42 [release]
            /cancel   patient-42
            /sort     [names...]
            /scan     [path]
            /track    some-other-run /path/to/Pipeline.protocol.job_list.TIMESTAMP
setup       /readset  [directory|schema]
            /where
            /model    [provider [model-name]]
            /key
            /help
            /exit
```

While a run is working, a spinner sits below the transcript with the elapsed time, and it says what
the agent is currently doing — `generating the script`, `asking Slurm`, `submitting` — rather than a
static "thinking". Output scrolls up past it as it arrives.

**Ctrl+C stops the agent, not the session.** It abandons the answer in flight and hands the prompt
back with the conversation intact — the same thing it means in every other assistant with a spinner
in it. At an idle prompt it clears the line. It never leaves; `Ctrl+D` and `/exit` do that, and a
single key that sometimes clears a line and sometimes ends the session is the thing that made people
afraid to press it.

The transcript shows your line once, beside `❯`, and the reply as plain prose. The agent's working —
the commands it runs, the machine output, its connective prose — is folded away and kept, the way a
chain of thought is: one dim line says how many steps were taken, and `/verbose` unfolds them,
including what has already scrolled past.

Pasting is safe: multi-line pastes are folded onto the input line instead of the first newline
submitting a half-finished command.

## Runs and jobs

The distinction runs through the whole tool:

- A **run** is one GenPipes invocation — the thing you named, the command you approved, the `cmd.sh`
  GenPipes generated. It is a unit of intent. You approve, cancel and diagnose runs.
- A **job** is one Slurm job inside that run. GenPipes turns a single run into dozens or hundreds of
  them, one per step per sample. A job is a unit of execution, with its own id, state and log file.

"Did it work?" is a question about a run. "What broke?" is only ever answerable about a job. So
`/check` reports the run and `/jobs` reports each job, grouped by step, since a failure is nearly
always one step failing across many samples rather than one unlucky job. Both read `sacct`.

**Both read `sacct`, and that is a correction.** `/check` used to report GenPipes' own
`tools log_report`, which never contacts Slurm — it infers state from files on disk. On a real
46-job run that died at 10:12 on 2026-07-27 it reported `1 COMPLETED, 2 RUNNING, 43 PENDING`;
`sacct` said `1 COMPLETED, 2 TIMEOUT, 43 CANCELLED`. The run had been dead for hours and the tool
called it healthy and in progress.

No amount of better file-reading fixes that. Every artifact GenPipes leaves is written *by the job
itself*: the prologue needs the job to have started, the epilogue needs the shell to exit normally
(a SIGKILL bypasses a bash `EXIT` trap), and only exit status 0 writes a `.done`. So *never started*
and *died violently* — the two states that define a dead run — are exactly the two the filesystem is
structurally incapable of recording. `log_report` was not lying; its vocabulary is about artifacts,
not about Slurm. The defect was reading filesystem words as scheduler words.

`runs.resolve()` is the replacement. The manifest is the denominator (every job ever submitted,
never one dropped, never one invented), `sacct` is the authoritative spine, and `squeue` annotates
only the jobs `sacct` still reports as non-terminal — because a pending job's *reason* exists in
exactly one place and stops existing the moment it leaves the queue. `DependencyNeverSatisfied`
means those jobs will never run while `sacct` goes on calling them `PENDING`, so the run is reported
as dead. A job `sacct` does not know is `UNKNOWN`, which never renders as healthy; a scheduler that
cannot be reached says so and guesses nothing.

`/check all` does the same over every run at the cost of **one** scheduler round-trip — job ids are
globally unique, so one batched query is attributed back by id rather than looping per run. It
groups rather than lists: NEEDS ATTENTION first and always, then ACTIVE, then FINISHED, because the
question a listing answers is "what should I be doing" and the answer to that is never
chronological.

A run's life is `held → submitted → gone`, with `abandoned` as the terminal branch off `held`:

- **held** — stopped at the gate, nothing submitted. A run is recorded here, *before* anything
  reaches Slurm, so a decision you left behind survives closing the terminal. Relaunching announces
  it; `/list` shows it first.
- **submitted** — on the scheduler, artifacts on disk.
- **gone** — the job list file is no longer on disk (a scratch purge, manual cleanup). The run drops
  out of `/list` but nothing is deleted: `/history` still shows it, marked `gone`, along with
  anything `/diagnose` concluded about it.
- **abandoned** — you said no. `/reject` is terminal now: nothing is submitted, nothing regenerated,
  the reason is recorded, and the run leaves `/list` and the startup pending line. Rework moved to
  `/modify`, which is what `/reject` secretly did before — so there was no way to abandon a run at
  all, and one you had mentally dropped kept asking to be decided forever.

Name every run. The name is how you approve it and how you check on it later. You are offered a name
derived from the task, pre-filled and editable, so Enter accepts it. If the name is already taken it
is quietly advanced (`patient-42` → `patient-42-2`) — reusing one would replace that run's stored
conversation, including a pending approval.

`/diagnose <name>` is the only command that costs a model call. It works in two stages, deliberately
visible as two: first it asks Slurm which jobs failed and reads those specific logs off disk, prints
that as evidence, and only then asks the model to explain the cause. A GenPipes run has hundreds of
`.o` files, and a model told to "go look" burns context rediscovering what one `sacct` call already
knows. The investigation runs on its own thread, so it can never disturb the run it is diagnosing.

The answer comes back in a fixed shape — manner, cause, evidence, fix, override, relaunch,
confidence — rather than as free prose, so it is drawn rather than dumped and so the parts that are
actionable can be acted on. The `override` section is a real ini fragment: `/modify <name>` offers to
write it into the run's private override ini rather than leaving you to create the file, spell the
step name right and remember which end of `-c` wins. The `relaunch` range is always the **full**
original one — GenPipes skips steps whose output is already up to date, so resubmitting everything
costs nothing and a narrowed range silently leaves the cancelled cascade undone.

There is no `/why`. It existed, as an alias for this same function under a second name, and the only
thing the pair achieved was making people wonder which to reach for. `/check` already gives the quick
version, in its root-cause block, with no model call at all.

`/track <name> <job_list_path>` registers a run you launched outside the agent entirely, so
`/check`/`/jobs`/`/diagnose` can find it by name with no prior conversation required. `/scan [path]` is
the same thing without the path-hunting: it walks a directory you name, recognises runs by their
job-list filenames and directory structure, and offers them for you to pick from. It is read-only
and metadata-only — it never opens a FASTQ, a BAM, a VCF or a readset, and it changes nothing it
finds. Nothing is adopted that you did not select.

## Changing a proposal at the gate

The gate offers three verbs, each with its consequence printed under it, because this is the one
point in the product where consequences matter:

```
approve           /approve chipseq-0728
                  submits to Slurm — cannot be undone
modify            /modify chipseq-0728 <what to change>
                  rewrites the command, asks you again
                  omit the change to pick from what's there
reject            /reject chipseq-0728 [why]
                  abandons this run; nothing is submitted
```

`/modify <name> <change>` is one model call and back to the gate. `/modify <name>` opens a
multi-select of the rows this proposal actually has — protocol, steps, design, pairs, readset,
config, output, and the run's own name — fills each in turn with the right widget, reviews the whole
set as `old → new`, and applies it as **one** model call however many rows changed.

Validation runs in three tiers with genuinely different sources of truth. Tier 1 is enumerable from
`slots.py`: a protocol from the wrong pipeline is answered inline with that pipeline's real ones and
never reaches the model, and a design file that is not on disk is caught before a generation is
spent on it. Tier 2 is form — is that a well-formed `-s` range, is that a path. Tier 3 is steps and
their dependencies, and it is reasoned about against `genpipes <pipeline> -t <protocol> --help` at
the moment of applying. **There is no step table in this repo and there must not be one**:
`genpipes.md` says so outright, because the numbered list is version-exact and a copy here would be
wrong on the next GenPipes release while looking authoritative. A tier-3 finding is reported as a
`warning` row in the re-rendered gate — a risk with its reasoning, not a verdict — and you may
proceed anyway, because GenPipes' own generation is the authoritative check and its error names
exactly which step it rejected.

The run's **name** is the one row that costs no model call: it changes no flag and needs no
regeneration, so it is a registry write. Only a held run can be renamed; after submission the name
is tied to a job list and to jobs already on the scheduler.

Prose typed at the gate routes to `/modify`, stating its interpretation first so a misreading is
visible while it is still free. With one exception: **a line that means "yes" is refused**, with the
`/approve` command that would work. Approval is typed, never inferred. No prose may ever cause a
submission, and a helpful assistant that reads "looks good" as consent is the exact failure the gate
exists to prevent.

## Readset files

`/readset [directory]` builds one from what is on disk — pairing `_R1`/`_R2` by **filename**, never
by opening a file — and shows it before offering to write it, with its guesses flagged (lanes of one
sample must share a `Sample` name, and a filename cannot say whether they do). It refuses to
overwrite: a readset file is hand-corrected after it is generated.

`/readset schema [pipeline]` prints the format instead. That is the version you can send to a
colleague, and it is the whole privacy argument in one artifact: the columns, their types and which
are mandatory are enough to write, test and review every piece of code that touches a readset, and
contain no sample name, no path and nothing real. Development and testing run on synthetic rows that
satisfy the same schema (`fake_A`, `/fake/r1.fq.gz`), and anything written against those works
unchanged on real data — because the logic depends on where a column is and what is allowed in it,
not on what is inside.

`/model` alone shows the current provider/model; `/model <provider>` (`anthropic`/`openai`/`gemini`/
`groq`) switches to it using that provider's already-configured key, and `/model <provider>
<model-name>` picks a specific model. `/key` adds or rotates a key for a provider — same prompt as
first launch, applied immediately, no restart needed.

By default the agent's working directory (checkpoint database, `runs.jsonl`, Biomni's own data
folder) is `~/scratch`. Override with `GENPIPE_AGENT_WORKDIR` if that's not the right place on your
cluster.

### Optional: web UI

`web/server.py` + `web/index.html` are a minimal browser front end, useful for demos or one-off
exploratory questions:

```bash
uvicorn web.server:app --reload    # from the repo root
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

`genpipe/fakecluster.py` writes a directory of small stubs — `module`, `genpipes`, `sbatch`, `sacct`,
`scancel`, `squeue` — and puts it at the front of `PATH`. Everything downstream then runs for real:
GenPipes "generates" a `cmd.sh`, running it writes a `job_output/` tree with per-job `.o` logs and a
`*.job_list.*`, and `sacct` answers about those job ids. The registry, the job parser, the triage and
every renderer run unmodified. `--fake-llm` adds a scripted stand-in for the model, so the entire
interface — gate, approve, check, jobs, diagnose, cancel — can be clicked through on a laptop.

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

Two layers. `tests/` covers the parts; `testcases/` walks the whole product. The split within
`tests/` is by what each suite needs, not by what it covers:

```bash
# Run anywhere, no dependencies beyond the standard library. These are the CI set.
python tests/test_gate.py          # the gate's invariants — the one test that must never go red
python tests/test_runs.py          # the run registry's lifecycle, and the job layer's parsing
python tests/test_fakecluster.py   # the fake cluster itself, including that it rejects bad commands
python tests/test_display.py       # every renderer, including the gate's refusal to offer approval
python tests/test_preflight.py     # RAP_ID blocks a submission; JOB_MAIL only warns
python tests/test_intake.py        # the choice panel, and slots.py vs genpipes.md staying in sync

# Need the venv (biomni, langgraph, pyte). Run before a release.
python tests/test_lifecycle.py     # the real agent + real gate + fake cluster, whole arc of a run
python tests/test_app.py           # the real app in a pty, asserting on the rendered screen
python tests/test_agent_gate.py    # the same gate helpers, reached through GenpipeA1's methods
python tests/test_mock_pipeline.py # the approve/reject round trip against a scripted model
```

All six run on every push via GitHub Actions (`.github/workflows/tests.yml`). That is possible
because every module in `genpipe/` except `agent.py` and `cli.py` deliberately imports nothing but
the standard library — so the safety-critical decision ("does this code submit to a scheduler?") is
checked in about two seconds, with no pinned agent stack, no API key and no cluster.

**`test_gate.py`** is two lists — code that must be gated, and code that must not — plus the
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

**`test_intake.py`** carries the tripwire for the one-file decision: `genpipes.md` must stay under
18,000 characters, and no step list may reappear in it. It also asserts that the feature-ini table
in `slots.py` and the one in `genpipes.md` still agree — the same knowledge written twice, once for
the panel to offer and once for the model to read, with nothing else stopping them drifting apart.

### The three test cases

`tests/` proves the parts work. `testcases/` proves the product does, by walking it the way a person
does. See [`testcases/README.md`](testcases/README.md).

```bash
./testcases/run.sh 1              # interface, offline, ~2 min, free
./testcases/run.sh 2              # a real run on the real cluster, CIT data, ~30 min
./testcases/run.sh 3 --confirm    # production: real data, full steps, hours
```

Case 2 is worth knowing about. It is a **real** GenPipes run — real generation, real ini layering,
real `sbatch`, real `sacct` — made short by appending GenPipes' own `$GENPIPES_INIS/<pipeline>/cit.ini`
to `-c`, which repoints the genome to chr19, points annotations at
`$MUGQIC_INSTALL_HOME/testdata/`, and caps most walltimes at ten minutes. Nothing is stubbed; only
the data is small. Matching readsets, designs and pairs already exist under
`$MUGQIC_INSTALL_HOME/testdata/<pipeline>/` with absolute paths, so there is nothing to stage.

## A note on `genpipes.md`

One file, deliberately, and a CI assert keeps it that way: under 18,000 characters, with no step
lists. The rule it is built on is that **anything `genpipes <pipeline> --help` can answer is never
written down here**. `--help` is 454 lines for rnaseq alone — every flag, every protocol, the full
numbered step list per protocol, and a description of each step — and it is version-exact because
it is the install talking about itself. Copying any of that would create something that goes stale
on the next module bump with nobody noticing.

What is left is only what no machine-readable source has: the environment contract, the `-c`
layering rule, the protocol-to-feature-ini mapping (which exists nowhere on the install — not in
`--help`, not in the shipped READMEs, not in the inis themselves), generate-versus-submit, the file
formats, and how to read a failure.

It is pinned to GenPipes v6.1.1 and to Rorqual's cluster ini (`common_ini/rorqual.ini`). On a
different DRAC cluster or GenPipes version it needs updating; it is not cluster- or
version-agnostic by design.

When the tripwire trips, the answer is to split into `skills/` — one always-loaded core plus
per-pipeline files loaded on demand — not to raise the number.

## Repo layout

```
genpipe/          the application, one importable package
tests/            suites over the parts        (see Testing)
testcases/        walkthroughs of the product  (see Testing)
web/              optional, ungated browser front end
start_agent.sh    module load, venv, python -m genpipe
```

Inside the package, in dependency order — each module depends only on the ones above it:

| Module | Purpose | Needs |
|---|---|---|
| `slots.py` | What each pipeline/protocol requires, and the feature-ini table | stdlib |
| `preflight.py` | Environment checks: RAP_ID blocks, JOB_MAIL warns | stdlib |
| `runs.py` | Runs and jobs: the registry, the job parser, triage, naming | stdlib |
| `gate.py` | The gate's decision logic, as pure functions over command text | stdlib |
| `intake.py` | Reads a request, finds what is missing, drives the choice panel | stdlib |
| `display.py` | Rendering (`parse()` is UI-agnostic; a future web UI can reuse it) | stdlib |
| `ui.py` | The terminal input side: prompt box, live completion, spinner, paste | stdlib |
| `fakecluster.py` | Stubbed GenPipes + Slurm + model, for dev mode and the tests | stdlib |
| `agent.py` | `GenpipeA1`: the gated graph, and run/resume/check/jobs/diagnose/cancel | biomni |
| `cli.py` | Builds the agent, owns the command table and the loop (`_repl()`) | biomni |
| `genpipes.md` | The GenPipes grammar fed to the model as "software" | — |

The "Needs" column is load-bearing, not incidental. Only the last two touch `biomni`; keeping
everything above them free of it is what lets the gate's invariants, the registry's lifecycle and
the fake cluster all be verified on every push in seconds. If one of them grows a heavy import, CI
stops covering the thing that matters most — so `tests.yml` has a step that imports all of them,
and a second that asserts `import genpipe` alone pulls in no `biomni`.

`genpipes.md` lives inside the package rather than at the root because `cli.GRAMMAR_PATH` resolves
it relative to the code that reads it. `.env` stays at the root: `start_agent.sh` sources it before
this process exists.
