# GenPipes Assistant

GenPipes Assistant is a terminal-based assistant for [GenPipes](https://genpipes.readthedocs.io/)
on Rorqual. You describe the analysis you want in plain English; it writes the GenPipes command,
shows you exactly what would be submitted, and waits for you to approve it. Afterwards it tracks the
run — what is queued, what failed, and why.

```
  ask ──▶ generate ──▶ GATE ──▶ submit ──▶ watch
                        ▲
                        you
```

**No job submission or interactive Slurm allocation reaches Slurm without your explicit
`/approve`.** That is enforced deterministically in the agent's graph, not by asking the model to be
careful.

It does not install or configure GenPipes. It uses the GenPipes environment you already have,
including your own allocation.

## Where this has been validated

**GenPipes Assistant has been developed and validated on the Digital Research Alliance of Canada's
Rorqual cluster using GenPipes 6.1.1.**

No other cluster, scheduler or GenPipes version has been validated, and none is currently claimed as
supported. The code contains cluster-configuration mappings for several other Alliance systems, and
the Python module name can be overridden; neither is a statement that the assistant works there. If
you run it elsewhere, you are the first person to do so.

## Prerequisites

1. **A Rorqual account**, with an allocation (`RAP_ID`).
2. **GenPipes 6.1.1 already configured and working in your shell**, per the official GenPipes setup
   instructions: <https://genpipes.readthedocs.io/>. This assistant does not set that up for you and
   does not read your shell configuration files.
3. **An API key** for a large-language-model provider. Developed and validated against Anthropic
   (Claude). The first-launch prompt also accepts OpenAI, Gemini and Groq keys; those paths are
   wired up but have not been validated.

You have what the assistant needs when both of these work in a fresh login shell on Rorqual:

```bash
module load mugqic/genpipes/6.1.1     # must succeed
echo "$RAP_ID"                        # must be non-empty
```

### What the assistant does with your GenPipes configuration

| | |
|---|---|
| **GenPipes module tree** | Used as-is. Every command the assistant generates begins `module load mugqic/genpipes/6.1.1`, in its own subshell. Checked at launch. |
| **`RAP_ID`** | **Read from your environment. Never created, guessed, defaulted or stored.** GenPipes puts `-A $RAP_ID` on every job, so without it Slurm rejects the entire run. The assistant checks at startup and again at the approval gate, and **refuses to offer approval** without one. |
| **`JOB_MAIL`** | Read from your environment if set. Addresses job notification mail and nothing else; a missing or misspelled one never blocks anything. |

Without a `RAP_ID` the assistant still opens: you can talk to it, build a run and take it all the
way to the gate. You just cannot submit. Set it in your shell, start a new session, and the block
clears.

## Install

```bash
git clone https://github.com/philobourque/genpipe-workflow-assistant ~/genpipe-workflow-assistant
```

That is the whole install. There is no `pip install` step, no virtual environment to create, and
nothing inside `start_agent.sh` to edit.

## Launch it from your project directory

```bash
cd /path/to/where/you/want/the/run
~/genpipe-workflow-assistant/start_agent.sh
```

**The directory you launch from is where the run is written.** GenPipes puts `job_output/`, the
generated `cmd.sh`, the job list and every pipeline output directory there, and it is where the
assistant looks for a run afterwards. `start_agent.sh` never changes directory — it locates its own
checkout independently — so launch it by path from wherever the analysis belongs.

Do not launch it from inside the clone. A run started there writes pipeline output into the
repository; a run started somewhere unexpected has to be adopted with `/track` before `/check` can
find it. `/where` shows every path the current session is using.

## What happens on first launch

`start_agent.sh` does the setup for you, in this order:

1. **Checks the environment.** That `module` exists, that `mugqic/genpipes/6.1.1` resolves in your
   module tree, and that `sbatch`, `squeue` and `sacct` are on `PATH`. The first two stop with an
   explanation; the third only warns.
2. **Loads Python.** `python/3.12.4`. It then unsets the `PYTHONPATH` that a loaded
   `mugqic/genpipes` exports, which otherwise points a 3.12 environment at GenPipes' own 3.13
   standard library.
3. **Creates its own virtual environment** at `~/scratch/biomni-venv` and installs
   `requirements.txt` into it, printing progress while it does. This takes a few minutes and
   happens once.
4. **Keeps your working directory.** The assistant starts in the directory you launched it from.
5. **Asks what to call you**, pre-filled with your username. One keystroke to accept.
6. **Asks for your API key** if none is configured, and saves it (see below).
7. **Checks `RAP_ID`** and says so on the opening screen if it is missing.

```
  Setting up the environment. This happens once per cluster.
  /home/you/scratch/biomni-venv
```

Every later launch takes a fast path and prints nothing — unless `requirements.txt` has changed
since the environment was built, which is detected by hashing the file, in which case it reinstalls
first.

### Your API key

If no key is set, you are prompted once. What you type is masked, with the first four characters
left visible so you can tell the paste registered. The provider is recognised from the key's shape
(Anthropic, OpenAI, Gemini, Groq) or asked for if it is not, and the key is checked against that
provider immediately — so a typo or an expired key is reported at the prompt that caused it, not
from inside the assistant a turn later.

The key is written to a `.env` file **inside the clone**, at mode `0600`, together with which
provider and model it is for. It is never written into the directory you launched from, and `.env`
is gitignored. If the file cannot be written — a full home quota is the usual reason — the
assistant says so and keeps working for that session, asking again next time.

`/key` replaces the key later without restarting. `/model` switches provider or model. A key already
exported in your shell takes precedence over the saved one.

## Checking your environment

From inside the assistant, `/where` prints everything the session is actually using: the cluster it
detected, the directory you launched from, its own state directory, the settings file and the
virtual environment.

To try the whole interface without a cluster, an allocation, an API key or any cost:

```bash
~/genpipe-workflow-assistant/start_agent.sh --fake --fake-llm
```

## Using it

Type what you want in plain English. You are asked to name the run first — the name is how you
approve it and how you check on it later.

```
 ──────────────────────────────────────────────────────────────
  ❯ run dnaseq germline_snv on my readset, all steps
 ──────────────────────────────────────────────────────────────
```

Anything starting with `/` is a command. Press `/` to browse them, `Tab` to complete, `↑`/`↓` to
pick. Any unambiguous abbreviation works, so `/appr` is `/approve`. `↑` at an empty prompt walks
back through the session's history. `Ctrl+D` or `/exit` leaves.

| | |
|---|---|
| **talking** | `/new` start a fresh conversation · `/verbose` show or fold away what the agent is doing |
| **deciding** | `/approve <name>` let a held run through to Slurm · `/modify <name>` change it before it launches · `/relaunch <name>` prepare a retry from a diagnosis · `/fork <name>` build a second run from an existing one · `/reject <name>` abandon it |
| **watching** | `/list` runs awaiting approval and live ones · `/view <name>` the command a run is · `/check <name>\|all` its overall status · `/jobs <name> [failed]` every job · `/monitor <name> [seconds]` watch until it changes · `/history [name]` the archive |
| **fixing** | `/diagnose <name>` investigate a failure · `/hold <name> [release]` stop queued jobs being scheduled · `/cancel <name>` scancel the rest · `/scan [path]` find runs already on disk · `/track <name> <job_list>` adopt a run started outside the assistant · `/sort [show]` hide rows from `/list` |
| **setup** | `/where` · `/user [name]` · `/model [provider [model]]` · `/key` · `/redraw` after resizing the terminal · `/help` · `/exit` |

`/readset [dir|schema]` builds a readset file from the FASTQ filenames in a directory, or prints the
format. It works but is not listed in `/help`.

**Ctrl+C stops the agent, not the session.** It abandons the answer in flight and returns the prompt
with the conversation intact. At an idle prompt it clears the line. It never exits.

While the agent works, a spinner below the transcript says what it is currently doing. Its working —
the commands it runs and their output — is folded away by default; `/verbose` unfolds it, including
what has already scrolled past. Multi-line pastes are folded onto the input line rather than the
first newline submitting a half-written command.

### Runs and jobs

A **run** is one GenPipes invocation: the thing you named, the command you approved. A **job** is one
Slurm job inside it, and GenPipes turns a single run into dozens or hundreds. So `/check` answers
"did it work?" about the run, and `/jobs` answers "what broke?" about each job, grouped by step.
Both read `sacct` rather than inferring state from files on disk, because a job that never started
and a job that was killed leave no artifacts to read.

A run's life is `held → submitted → gone`, with `abandoned` as the branch off `held`:

- **held** — stopped at the gate, nothing submitted. Recorded before anything reaches Slurm, so a
  decision you leave behind survives closing the terminal.
- **submitted** — on the scheduler.
- **gone** — the job list is no longer on disk (a scratch purge, say). It leaves `/list`; `/history`
  still has it.
- **abandoned** — you rejected it. Terminal: nothing is submitted or regenerated.

`/diagnose <name>` is the only command that costs a model call. It first asks Slurm which jobs
failed and reads those specific logs off disk, shows you that as evidence, and only then asks the
model to explain the cause and propose a fix.

## The approval gate

The moment a proposal contains something that submits, the graph **pauses** and draws what is about
to run. Nothing has reached Slurm at that point.

```
approve           /approve chipseq-0728
                  submits to Slurm — cannot be undone
modify            /modify chipseq-0728 <what to change>
                  rewrites the command, asks you again
                  omit the change to pick from what's there
reject            /reject chipseq-0728 [why]
                  abandons this run; nothing is submitted
```

`/approve` is the only command that releases a held run to the scheduler. `/modify` sends it back to
be rewritten; `/reject` ends it.

**Approval is typed, never inferred.** Prose typed at the gate is routed to `/modify` — and a line
that means "yes" is refused, with the `/approve` command that would work printed underneath. No
sentence you type can cause a submission.

`RAP_ID` is re-checked here. Without one, the gate reports `cannot submit` and withholds approval
rather than spending it on jobs Slurm would reject.

The run is recorded as held before anything is submitted, so a proposal parked at the gate can be
approved from a later session.

## What it can and cannot touch

Read this before pointing the assistant at a directory containing real data.

**The gate protects the scheduler, not the filesystem.** It holds anything that would submit a job
or take an allocation, whether it comes through GenPipes or goes to Slurm directly:

| Held for `/approve` | Runs freely |
|---|---|
| `bash <script>.sh`, `submit_genpipes`, `chunk_genpipes.sh` | `squeue`, `sacct`, `sinfo`, `scontrol show` |
| `sbatch`, `srun`, `salloc` | `scancel`, `scontrol hold`/`release` |

Reading a script that *contains* those words is not submitting one, so `grep sbatch cmd.sh` and the
rest of the agent's ordinary inspection stay free.

Everything else the agent decides to do — reading files, listing directories, running GenPipes'
generation step, writing config files — runs without asking you. It has whatever access your account
has. Treat it as a capable colleague at your own shell prompt, not as a sandbox.

**What leaves the cluster.** The assistant sends to your chosen model provider: the GenPipes grammar
document, your side of the conversation, the commands the agent runs and their output, and — when
you run `/diagnose` — the text of the failed jobs' log files. Sample names, file paths and readset
contents appear in generated commands and therefore go too. Nothing else is transmitted anywhere.

**What it deliberately does not read.** `/readset` pairs `_R1`/`_R2` by **filename** and never opens
a FASTQ. `/scan` recognises runs from job-list filenames, generated command names and directory
structure, and opens no data file. Neither reads a BAM, a VCF or a result table.

**No usage data is collected.** There is no analytics or reporting call anywhere in the code.
`/telemetry` and `GENPIPE_TELEMETRY` refer to in-process timings printed on your own terminal.

**Nothing is downloaded in the background.** The underlying agent framework's data lake is
explicitly disabled.

## Configuration

Everything below has a working default. `/where` shows what the current session resolved.

### The four locations

| | What is there | Default |
|---|---|---|
| **Your analysis directory** | GenPipes output: `job_output/`, the generated `cmd.sh`, the job list, pipeline results | the directory you launched from — there is no variable, deliberately |
| **The clone** | The source, and `.env` with your API key, provider, model and name | wherever you cloned it |
| **Assistant state** | The checkpoint database (which is what lets a held run be approved in a later session) and the run registry | `~/scratch/biomni_data` |
| **Virtual environment** | `requirements.txt`, installed | `~/scratch/biomni-venv` |

Your data and the clone stay separate: pipeline output never lands in the repository, and your API
key never lands in your project directory.

### Environment variables

Read by `start_agent.sh`, so set these in your shell, not in `.env`:

| | |
|---|---|
| `GENPIPE_VENV` | where the virtual environment is built |
| `GENPIPE_PYTHON_MODULE` | which Python module to load. Default `python/3.12.4`. Python 3.12 is the only version this is tested on |
| `GENPIPE_SKIP_GENPIPES_CHECK` | launch even though `mugqic/genpipes/6.1.1` does not resolve |

Read by the assistant, and settable in `.env` — see [`.env.example`](.env.example):

| | |
|---|---|
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY` | written by the first-launch prompt and by `/key` |
| `GENPIPE_LLM_SOURCE`, `GENPIPE_LLM_MODEL` | which provider and model; `/model` writes these. Defaults to Anthropic `claude-sonnet-5` |
| `GENPIPE_AGENT_WORKDIR` | the parent of the assistant's state directory. Default `~/scratch` |
| `GENPIPE_ENV_FILE` | where the settings file lives |
| `GENPIPE_THEME` | `light` or `dark`. `NO_COLOR=1` disables colour entirely |

Anything already exported in your shell wins over what `.env` says. Paths in `.env` must be
absolute: the assistant parses that file itself and expands nothing, so a `~` stays a literal
character.

`RAP_ID` and `JOB_MAIL` are deliberately absent from that list. They belong to GenPipes and come
from your shell.

## Troubleshooting

| What you see | What it means | What to do |
|---|---|---|
| `No 'module' command here` | not on Rorqual, or not on a login node | log in to Rorqual. To try the interface anywhere, add `--fake --fake-llm` |
| `GenPipes 6.1.1 is not visible to 'module' here` | GenPipes is not configured in your shell | follow the official GenPipes setup, then confirm with `module load mugqic/genpipes/6.1.1` |
| `Could not load python/3.12.4` | the Python module is unavailable | on Rorqual it should be there; check with `module spider python` |
| `Slurm command(s) not on PATH` | `sbatch`/`squeue`/`sacct` missing | a warning only. Generation and the gate still work; submission and monitoring do not |
| `Could not create a virtual environment at …` | `~/scratch` is unwritable or full | set `GENPIPE_VENV` to somewhere writable |
| `RAP_ID BLOCKS SUBMISSION` at startup, or `cannot submit` at the gate | no allocation in your environment | export `RAP_ID` in your shell profile and start a new session. Find yours in [CCDB](https://ccdb.alliancecan.ca/) or with `sacctmgr show associations user=$USER` |
| The key prompt appears every launch | `.env` could not be saved | usually a full home quota; the message says which error |
| `/check` cannot find a run you submitted | it was launched from a different directory | `/track <name> <job_list_path>`, or `/scan <path>`. `/where` shows the current directory |
| `SRE module mismatch` running Python by hand | a loaded `mugqic/genpipes` set `PYTHONPATH` to GenPipes' 3.13 library | `unset PYTHONPATH`. `start_agent.sh` already does this |

## Known limitations

- **Validated on Rorqual with GenPipes 6.1.1 only.** Other clusters and other GenPipes versions are
  untested. The GenPipes version is written into the grammar the model is given.
- **The gate covers scheduler submission and allocation, not the filesystem.** The agent can read
  and write files in your account without asking. See [What it can and cannot
  touch](#what-it-can-and-cannot-touch).
- **Anthropic is the validated provider.** OpenAI, Gemini and Groq are selectable but unvalidated.
- **`/diagnose` costs a model call**; no other command does.
- **The model can be wrong.** The gate exists so that being wrong is visible and cheap — read the
  proposal before approving it.

## Testing

Offline and agent integration test suites run in CI on every push
(`.github/workflows/tests.yml`), in two jobs split by what each has to install:

- **offline** — the gate's invariants, the run registry and job parsing, the environment preflight,
  the settings file, the launch contract, and every renderer. No dependencies beyond the standard
  library, no API key, no cluster.
- **agent** — the real gated graph, the real checkpoint and the full command surface, driven against
  a stubbed cluster and a scripted model. No API key; every provider call is a stub.

A terminal-level suite drives the assistant in a pty and asserts on the rendered screen; it runs
before a release rather than in CI.

`tests/test_gate.py` is the one that must never fail: two lists, of code that must be gated and code
that must not, drawing one boundary — scheduler submission and allocation are gated, scheduler
observation and inspection are not.

`testcases/` holds manual walkthroughs of the whole product on Rorqual, including runs against real
Slurm with GenPipes' own CIT (chr19) data. See [`testcases/README.md`](testcases/README.md); the
cluster cases spend real allocation.

## Development

### Dev mode

```bash
~/genpipe-workflow-assistant/start_agent.sh --fake            # stubbed GenPipes and Slurm, real model
~/genpipe-workflow-assistant/start_agent.sh --fake --fake-llm # nothing real at all
```

`genpipe/fakecluster.py` puts small stubs for `module`, `genpipes`, `sbatch`, `sacct`, `scancel` and
`squeue` at the front of `PATH`. Everything downstream runs unmodified: a `cmd.sh` is "generated",
running it writes a `job_output/` tree with per-job logs and a job list, and `sacct` answers about
those job ids. The stubs validate their input, so `--fake` with a real model tests whether the model
writes correct GenPipes commands. `--fake-llm` adds a scripted stand-in for the model.

Either flag makes the launcher skip the cluster checks, since dev mode replaces the very commands
they look for — so this is the one form that runs off Rorqual. Dev mode announces itself in amber on
every launch.

`GENPIPE_FAKE_STATE` picks what the stubbed cluster presents: `happy`, `running`, `failed-oom`
(default), `failed-missing-input`, or `dying`.

### Layout

```
genpipe/          the application, one importable package
tests/            automated suites
testcases/        manual walkthroughs on Rorqual
start_agent.sh    environment checks, venv, launch — never changes directory
```

The terminal interface is the only interface. There is no alternative front end, and one is not
planned: an interface that reached the scheduler without passing the gate would be a second door
into the one thing this product exists to prevent.

Every module in `genpipe/` except `agent.py` and `cli.py` imports nothing but the standard library.
That is what lets the safety-critical question — does this code submit to a scheduler? — be checked
in seconds on every push, with no agent stack, no API key and no cluster. CI asserts that
`import genpipe` pulls in no heavy dependency.

`genpipe/genpipes.md` is the grammar document given to the model: the environment contract, the `-c`
config-layering rule, the protocol-to-feature-ini mapping, and how to read a failure. It records
nothing that `genpipes <pipeline> --help` can answer — step lists especially — because that is
version-exact and a copy here would go stale silently. It is pinned to GenPipes 6.1.1.
