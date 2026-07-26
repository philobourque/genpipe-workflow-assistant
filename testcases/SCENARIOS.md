# Five conversations to hold with the assistant

These are hand-driven scenarios, not automated tests. Each one is a conversation
you actually type, chosen because it is the shortest path to a distinct piece of
machinery, and each says what should happen at every step so that "it seemed
fine" is never the result.

They complement the numbered cases: [case 1](01-interface.md) proves the
interface is coherent against a scripted model, [case 2](02-cluster.md) proves
one real run works end to end. These five are for **iterating** — for the days
when you are changing how the agent behaves and need to know quickly whether it
still behaves.

All five use GenPipes' own CIT data: chr19 reads, ten-to-forty-minute walltimes,
absolute CVMFS paths already written into the readsets, nothing to stage. A CIT
run is a real run — real generation, real ini layering, real `sbatch`, real job
IDs. Only the data and the walltimes are small.

---

## Before any of them

**Prerequisites**

- a Rorqual login node
- `RAP_ID` set to a valid allocation (the app refuses to approve without it)
- `JOB_MAIL` set to an address that exists — the check no longer warns at
  startup, so a typo here is silent and every job's mail bounces
- a real API key configured (`/key` if not)
- `module load mugqic/genpipes/6.1.1` succeeds

**Launch from the directory the run should live in.** This is the single most
common way to waste twenty minutes. `cmd.sh`, `job_output/` and the job list all
land in the directory the app was started from, and that is where the registry
looks for them:

```bash
mkdir -p ~/scratch/<scenario> && cd ~/scratch/<scenario>
~/genpipe-workflow-assistant/start_agent.sh
```

Type `/where` once inside and check the **launched from** line before you spend
anything.

**Three invariants to check in every HOLD box, every time**

1. `cit.ini` is **last** in the `-c` stack. It is an override layer; if
   `rorqual.ini` comes after it, you have just approved production walltimes.
2. The readset path in the command is the one you meant. An absolute path is a
   good sign; a bare filename means it is trusting the working directory.
3. The step range is what you asked for. The gate box is the last honest
   statement of what will run.

**Cleaning up.** Jobs are billed. If you abandon a scenario part-way:

```bash
squeue -u $USER -o "%i %j" | grep cit
scancel <ids>
```

**Recording a result.** For each scenario, note the run name, whether each
expectation held, and — when something is wrong — the whole transcript block, not
a summary of it. The failures worth fixing are almost always visible in the exact
words the model used.

---

## 1. The happy path — `rnaseq_light`

**What it proves.** That the two modes are distinct and the whole arc works:
talking stays talk, a request becomes a generation, a submission stops at the
gate, approval reaches Slurm, and monitoring reports what the scheduler says.

**Machinery under test.** `genpipe_agent.TALK_PROTOCOL` (mode choice),
`intake.brief` (directory context), `gate_rules.is_submission` → the gate,
`runs.Registry` (hold → submitted), `runs.jobs_for` / `log_report`.

**Why this pipeline.** The cheapest real one on the install: no `-t` protocol, no
alignment, kallisto pseudo-alignment against an index already built in CVMFS. 17
jobs, none longer than forty minutes.

**Setup**

```bash
mkdir -p ~/scratch/sc1-light && cd ~/scratch/sc1-light
cp /cvmfs/soft.mugqic/CentOS6/testdata/rnaseq_light/readset.rnaseq.txt .
~/genpipe-workflow-assistant/start_agent.sh
```

**The conversation**

| # | you type | expected |
|---|---|---|
| 1 | `what does rnaseq_light do?` | one `ASSISTANT` block. **No** `CODE`, **no** `GENERATE`, no shell of any kind |
| 2 | `run rnaseq_light steps 1-5 on my readset, no design file, and put cit.ini last in the -c stack so it stays cheap` | `GENERATE` → `TERMINAL` ending `TOTAL: 17 jobs created` → **HOLD** box |
| 3 | — | the box names the run (`rnaseq-light-MMDD`), shows `bash cmd.sh`, and the `-c` stack ends with `cit.ini` |
| 4 | `/approve <name>` | Tab or ↑ completes the name. Real job IDs come back; `job_output/RnaSeqLight.job_list.<TIMESTAMP>` exists |
| 5 | `/list` | the run reads `submitted` with a job count |
| 6 | `/jobs <name>` | 17 jobs, states from `sacct`, changing between polls |
| 7 | `how is my run doing?` | a `SCHEDULER` block (`sacct` or `log_report`), then a plain-English answer derived from what came back — not a guess |
| 8 | wait, then `/check <name>` | `log_report` agrees with what `/jobs` said; every job reaches `COMPLETED` |

**Pass criteria.** Step 1 produced no code. Step 2 reached the gate in a single
turn without being asked to submit separately. Step 7's answer is traceable to
the command it ran.

**If it fails.** A generation that stops before proposing the submission is the
failure this scenario exists to catch — see the `HOW A RUN IS APPROVED` section
of `TALK_PROTOCOL`. A `NameError: name 'bash' is not defined` means
`gate_rules.mark_shell` stopped recognising the block as shell.

**Cost.** ~15 core-minutes, ~20 minutes wall.

---

## 2. The empty request — `dnaseq germline_snv` through the panels

**What it proves.** That the agent asks rather than guesses, and that what it
offers comes from this project's table rather than from the model. A model asked
to list dnaseq protocols will eventually invent an eighth one and sound certain;
the panel cannot.

**Machinery under test.** `ask()` inside `<execute>` →
`gate_rules.ask_request` → the `ask_user` node → `slots.gap_for` → `ui.choose`.
Also `slots.as_data` / `from_data`: the pause is written into the SQLite
checkpoint as JSON, and a `Gap` object there used to kill the turn with
`TypeError: Object of type Gap is not serializable`.

**Setup** — deliberately an empty directory, so nothing can be inferred:

```bash
mkdir -p ~/scratch/sc2-dnaseq && cd ~/scratch/sc2-dnaseq
~/genpipe-workflow-assistant/start_agent.sh
```

**The conversation**

| # | you type | expected |
|---|---|---|
| 1 | `I want to launch a job` | a **pipeline panel**, ten rows, one per pipeline in `slots.PIPELINES`. Not a prose list of pipelines |
| 2 | pick `dnaseq` | a **protocol panel**, exactly seven rows: `germline_snv`, `germline_sv`, `germline_high_cov`, `somatic_tumor_only`, `somatic_fastpass`, `somatic_ensemble`, `somatic_sv` — each with a line of plain English |
| 3 | pick `germline_snv` | a **readset question**. The directory is empty, so it should be a free-text prompt, not an empty menu |
| 4 | Tab-complete to `/cvmfs/soft.mugqic/CentOS6/testdata/dnaseq/readset.dnaseq.txt` | path completion works inside the answer (`~` and directory slashes included) |
| 5 | `steps 1-5, and use cit.ini last so it stays on chr19` | `GENERATE` → **HOLD** |
| 6 | — | the box shows `pipeline dnaseq`, `protocol germline_snv`, and a `-c` stack of `dnaseq.base.ini`, `rorqual.ini`, `cit.ini` |
| 7 | `/approve <name>` | submits; `dnaseq` CIT walltimes are ten minutes each |

**Pass criteria.** Every panel's options came from the table (count them). The
agent asked at most one thing at a time. Nothing it asked about was already in
the conversation.

**Watch for.** Asking the same question twice — that is a prompt failure worth
recording verbatim. And an `ask()` that reaches the interpreter
(`NameError: name 'ask' is not defined`) means `ask_request` stopped tolerating
the shape the model wrote; it is designed to always return a request once the
call is present.

**Cost.** ~10 core-minutes.

---

## 3. Rejection and rework

**What it proves.** The other half of the gate. Approval is the boring half;
rejection is the one that has to feed a correction back into a live conversation
and produce a *different* command with the *same* name.

**Machinery under test.** the `submission_gate` node's rejection branch,
`GenpipeA1.resume(approved=False)`, `runs.Registry.held_for_thread` (one pending
decision keeps one name), `gate_rules.generation_command` searching **backwards**
so the box shows the revision and not the original.

**Setup**

```bash
mkdir -p ~/scratch/sc3-reject && cd ~/scratch/sc3-reject
cp /cvmfs/soft.mugqic/CentOS6/testdata/rnaseq_light/readset.rnaseq.txt .
~/genpipe-workflow-assistant/start_agent.sh
```

**The conversation**

| # | you type | expected |
|---|---|---|
| 1 | `run rnaseq_light steps 1-5 on my readset with cit.ini last` | **HOLD**, steps `1-5`, 17 jobs |
| 2 | `/reject <name> only run steps 1-3, I don't need kallisto yet` | it regenerates and comes **back to the gate** |
| 3 | — | the new box says steps `1-3` and ~7 jobs. The **same run name** as step 1 — a second name for one pending decision would leave a phantom in `/list` that can never be approved |
| 4 | `/list` | exactly one held run, not two |
| 5 | `/reject <name> actually stop, I'll come back to this` | still held, still awaiting a decision, nothing submitted |
| 6 | `/exit`, relaunch from the same directory | the startup line reports the run still held **by name** — the decision survived the process |
| 7 | `/approve <name>` | approving from a **different process** works: this is the gate's central promise |

**Pass criteria.** One name throughout. The command in the box changed at step 3.
Step 7 submitted the revised command, not the original.

**Cost.** ~5 core-minutes.

---

## 4. A failure, and an honest diagnosis

**What it proves.** That a run that goes wrong is legible. This is the half of
the product that matters most and the half a happy path never touches.

**Machinery under test.** `runs.triage` (which jobs failed, from `sacct`, plus
their logs from disk), `GenpipeA1.why` on its own thread — Biomni's `AgentState`
has no message reducer, so diagnosing on the run's own thread would erase the
conversation that built it — and `display.triage`.

**Setup.** Force one step to die by giving it thirty seconds:

```bash
mkdir -p ~/scratch/sc4-fail && cd ~/scratch/sc4-fail
cp /cvmfs/soft.mugqic/CentOS6/testdata/rnaseq_light/readset.rnaseq.txt .
cat > tiny.ini <<'EOF'
[kallisto]
cluster_walltime = 0:00:30
EOF
~/genpipe-workflow-assistant/start_agent.sh
```

**The conversation**

| # | you type | expected |
|---|---|---|
| 1 | `run rnaseq_light steps 1-5 on my readset, with cit.ini and then tiny.ini last in the -c stack` | `GENERATE` → **HOLD** |
| 2 | — | check the box: `tiny.ini` after `cit.ini`, or the override does nothing |
| 3 | `/approve <name>` | submits |
| 4 | wait ~5 min, `/jobs <name> failed` | the kallisto jobs are `TIMEOUT`; the count-matrix jobs downstream are **cancelled, not failed** |
| 5 | `/check <name>` | the verdict names trouble rather than reporting progress |
| 6 | `/why <name>` | the facts first (which step, how many jobs, the log path, the tail of the log), **then** the model's explanation |
| 7 | — | it names `kallisto` and the walltime. It must **not** invent a memory problem, and must not propose a resource value it cannot trace to a config line |
| 8 | `why did that happen?` (plain English) | the same conclusion, reached in conversation |

**Pass criteria.** Step 7 is the real assertion. "The logs do not show why" is an
acceptable answer. A confident wrong cause is a failure of the product, not of
the run — record the exact wording.

**Cost.** ~10 core-minutes. Deliberately wasted, and worth it.

---

## 5. Working with the data, and two runs in one conversation

**What it proves.** Two things a pipeline launcher is usually bad at. First, that
the agent can do ordinary work on your files — read them, count them, write a new
one — without that turning into a submission. Second, that a conversation is not
a run: one thread can produce several, each named and monitored on its own.

**Machinery under test.** the ungated `execute` path (Python by default, shell
with `#!BASH`, variables persisting between blocks), `gate_rules.mark_shell`,
`intake.brief`'s "named but NOT on disk" section, and the identity split — a
`thread_id` names the conversation, a `name` names the run
(`GenpipeA1._run_name`).

**Setup**

```bash
mkdir -p ~/scratch/sc5-data && cd ~/scratch/sc5-data
cp /cvmfs/soft.mugqic/CentOS6/testdata/rnaseq_light/readset.rnaseq.txt .
~/genpipe-workflow-assistant/start_agent.sh
```

**The conversation**

| # | you type | expected |
|---|---|---|
| 1 | `how many samples are in my readset, and do all the FASTQ paths in it actually exist?` | a `CODE` block that reads the file and checks the paths, then an answer with real numbers. **No** `GENERATE`, no gate |
| 2 | `write me a readset with just the first two samples, call it small.tsv` | it writes the file. Verify with `/where` and a shell outside, or ask it to show the file |
| 3 | `run rnaseq_light steps 1-3 on small.tsv with cit.ini last` | **HOLD**, and the command's `-r` is `small.tsv` |
| 4 | `/approve <name>` | submits (~7 jobs) |
| 5 | `now run the same thing on the full readset, steps 1-3` | a **second** HOLD, with a **different** run name, in the same conversation |
| 6 | `/approve <name2>` | submits |
| 7 | `/list` | two runs, both submitted, distinct names and job counts |
| 8 | `/check <name>` then `/check <name2>` | each reports its own progress |
| 9 | `run rnaseq_light on missing.tsv` | it should say the file is not there and **ask**, before generating anything — not run `genpipes` and fail on the argument, and never `find /` |

**Pass criteria.** Steps 1–2 did work without approaching the gate. Steps 5–7
produced two independent runs from one conversation. Step 9 asked first.

**A caveat worth knowing.** The gate covers cluster spend, not file safety. Only
submissions are intercepted; a request to reorganise a directory is carried out
immediately, with your permissions and no approval box.

**Cost.** ~10 core-minutes.

---

## Rough edges seen while writing these

Known, not yet fixed. Worth recognising so they are not re-diagnosed from
scratch:

- **The proposal box can mis-report a flag.** One `rnaseq_light` run with `-o
  <dir>` showed `pairs  rnaseq_light_out` in the HOLD box. Cosmetic, in
  `gate_rules.build_proposal`, but the box is meant to be the trustworthy summary.
- **The model sometimes stops after generating** and asks in prose whether to
  submit. Prose cannot be approved — there is no box and no run name. The prompt
  now forbids this explicitly; if it recurs, the fix is there, not in the graph.
- **`JOB_MAIL` is no longer checked out loud.** A typo bounces every notification
  silently.
- **`tests/test_lifecycle.py` is out of date** — it asserts `run name ==
  thread_id`, which the per-conversation change deliberately broke. The six
  stdlib suites in `tests/` are green and run in CI; this one needs updating to
  read the name off the returned status.
