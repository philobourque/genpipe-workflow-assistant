# Case 4 — a real research dataset, mouse, with a design

**Part A costs nothing and can be run as often as you like. Part B costs a real
allocation (~1,000–2,000 core-hours) and takes one to two days. Run part A after
touching intake, the ask node, or the grammar. Run part B before claiming the app
works on anything other than human data.**

Hand-driven, unlike cases 1–3 — there is no `run.sh 4`. The point of this case is
the *questions*, and a script that asks them would be asserting on the model's
prose, which is exactly the thing cases 1–3 already avoid asserting on.

## Why this dataset and not more CIT

Case 2 is a real cluster run, and it is the easy path in three ways that matter:

| | case 2 (CIT) | case 4 (this) |
|---|---|---|
| genome | *Homo sapiens*, the default | **mouse**, where the default ini is wrong |
| design file | shipped in `testdata/` | **absent** — must be asked for |
| scale | one chromosome, 10-min walltimes | 74 GB, whole-node STAR, days |
| monitoring | over before the session is | outlives it — the only case where a stale status costs a day |

Each of those exercises machinery nothing else touches. The genome ini is the
sharp one: for human, `<Species>.<assembly>.ini` resolves to a genome whose
indexes are built, so the rule in `genpipes.md` looks correct. It is not correct.
It is correct *for human*, and no amount of CIT running will ever say so.

## The dataset

```
/lustre09/project/6007512/shared/C3G/projects/genpipes_agent_alain_rnaseq
├── myReadset.tsv          GenPipes format, 9 samples, already correct
├── 0_annotation.csv       the groups — NOT a design file
└── raw_reads/             18 fastq.gz, 74 GB
```

Nine mouse samples, paired-end, 101 bp, one NovaSeq run (`X0219`, lane 1),
quality offset 33, roughly 55 M read pairs per sample.

| group | samples |
|---|---|
| WT | S3382, S3385, S8613, S611 |
| Tks5hh | S8542, S8551, S9452, S9453, S5069 |

The directory is owned by another user and carries an ACL that denies writes even
to group members. **Treat it as read-only.** The setup below stages a writable
copy rather than fighting that.

## Prerequisites

- on a Rorqual login node (the dataset lives on `/lustre09`, which is Rorqual-local)
- read access to `/lustre09/project/6007512/shared/C3G/projects/` (group
  `rrg-bourqueg-ad`)
- `RAP_ID` set to a valid allocation, `JOB_MAIL` set
- a real API key configured
- `module load mugqic/genpipes/6.1.1` succeeds
- **part B only:** ~600 GB free in the output filesystem

## Setup

The readset's FASTQ paths are relative to the project directory, which is not
writable. Stage a working directory with absolute paths instead:

```bash
SRC=/lustre09/project/6007512/shared/C3G/projects/genpipes_agent_alain_rnaseq
WORK=$HOME/scratch/mouse_rnaseq_case4
mkdir -p "$WORK"

sed "s|\traw_reads/|\t$SRC/raw_reads/|g" "$SRC/myReadset.tsv" > "$WORK/myReadset.tsv"
cp "$SRC/0_annotation.csv" "$WORK/"
```

Do **not** stage a design file. Its absence is what part A tests.

For part B, the design file is this — tab-separated, and the contrast direction
is the one thing here that a human has to confirm rather than derive:

```
Sample	Tks5hh_vs_WT
S3382	1
S3385	1
S8613	1
S611	1
S8542	2
S8551	2
S9452	2
S9453	2
S5069	2
```

`1` is control, `2` is treatment, so fold-changes read as Tks5hh relative to
wild-type. If that is backwards for the biology, every result inverts silently —
which is why the app is expected to *ask* rather than write this itself.

---

## Part A — comprehension

Free. Nothing reaches Slurm, because nothing reaches Slurm before `/approve`.
Run it as many times as you want; that repeatability is the gate paying off as a
testing property rather than only a safety one.

```bash
cd $HOME/scratch/mouse_rnaseq_case4
~/genpipe-workflow-assistant/start_agent.sh
```

| # | action | expected |
|---|---|---|
| 1 | launch | banner shows the real model; environment check passes or names the bad variable |
| 2 | "what's in this directory?" | finds `myReadset.tsv` and `0_annotation.csv`; does not claim a design file exists |
| 3 | "describe this dataset" | 9 samples, paired-end, 101 bp; reads the readset rather than guessing from filenames |
| 4 | "what's the experiment?" | 4 WT vs 5 Tks5hh, from `0_annotation.csv` |
| 5 | "is this mouse or human?" | mouse — and says what it based that on, rather than asserting it |
| 6 | "compare gene expression between the two groups" | enters `● Preparing run…` — it is a request to perform an analysis, not to explain one, even with no launch keyword |
| 7 | — | infers `rnaseq` **and** `stringtie` from that description (`prep.goal`), and does not ask you to pick either |
| 8 | — | does **not** ask about the step range or the cluster ini; both are defaults, and asking turns a conversation into a form |
| 9 | — | **asks for a design file**; the panel offers `0_annotation.csv` as a candidate path |
| 10 | — | does **not** silently accept `0_annotation.csv` as the design, and does not write one |
| 11 | answer the panel with a path that does not exist | says so; does not generate against a missing file |
| 12 | answer with the staged `design.tsv` | generation proceeds |
| 13 | — | the `-c` stack is `rnaseq.base.ini`, the genome ini, the **cluster ini for the machine you are on**, in that order |
| 14 | — | the genome ini is **`Mus_musculus.GRCm38.Ensembl83.ini`** |
| 15 | — | `-o` points somewhere with room, not at the read-only project directory |
| 16 | the gate draws | HOLD box shows pipeline, protocol, 9 samples, the design, the full `-c` stack |
| 17 | — | the footer offers `/approve`, `/modify` and `/reject`, each with its consequence on the line beneath it |
| 18 | type `looks good, go ahead` | **refused.** It prints the `/approve <name>` line and nothing reaches the scheduler. Approval is typed, never inferred |
| 19 | type `use 32 cpus for star` | read as a change: it states its interpretation, then regenerates under the *same* name |
| 20 | `/modify <name>` | multi-select panel of the rows this proposal has, `name` first |
| 21 | select `protocol`, enter `germline_snv` | rejected inline with rnaseq's real protocols; **no model call is made** |
| 22 | select `name` only, give a new one | renamed with no regeneration — "still held · nothing regenerated" |
| 23 | "how long will this take?" | an estimate reasoned from data volume; does **not** repeat the 24 h walltime ceilings as a prediction |
| 24 | "what will this cost?" | order-of-magnitude core-hours; notes STAR asks for a whole node |
| 25 | `/list` | one run, `held`, not two — a modify cycle keeps one name |
| 26 | `/verbose` | the folded working appears, including what already scrolled past |

### Action 14 is known to fail

The rule at `genpipes.md:123` says the genome ini is
`<Species>.<assembly>/<Species>.<assembly>.ini`. For this dataset that resolves
to `Mus_musculus.GRCm38.ini`, which pins **Ensembl 102**. The only STAR index
built for GRCm38 on this install is **Ensembl 83**:

```
$MUGQIC_INSTALL_HOME/genomes/species/Mus_musculus.GRCm38/genome/star_index/
    Ensembl83.sjdbOverhang49
    Ensembl83.sjdbOverhang74
    Ensembl83.sjdbOverhang99     ← 101 bp reads
    Ensembl83.sjdbOverhang124
    Ensembl83.sjdbOverhang149
```

So the run generates cleanly, submits cleanly, and dies at `star_align` with no
index — the worst shape of failure, because everything before it looked right.

The fix is to make the ini a **lookup, not a naming rule**: pick the versioned
ini that has a `star_index/` at an overhang matching the read length.
`genpipes.md` §5 now says exactly that, and gives the two `ls` commands, so the
agent has been told how to look rather than how to derive. Whether it does is
what action 14 measures — the document changing is not the same as the behaviour
changing, which is the reason this row stays in the case rather than being
deleted along with the bug report.

Until it passes reliably, correct it by hand:

```
/modify <name> use Mus_musculus.GRCm38.Ensembl83.ini as the genome ini
```

Recording this as an expected failure is deliberate. A case that only passes
tells you nothing on the day it starts failing.

---

## Part B — execution

Costs a real allocation. Run once, and only with a reason.

| # | action | expected |
|---|---|---|
| 27 | `/approve <name>` | jobs submitted; real job IDs come back |
| 28 | — | `job_output/RnaSeq.stringtie.job_list.<TIMESTAMP>` exists under the `-o` directory |
| 29 | `/list` | reads `live`, with a job count |
| 30 | `/jobs <name>` | states come from `sacct` and change between polls |
| 31 | `/check <name>` | the state table sums to the manifest; the footer reads `N/N jobs resolved`, and names `sacct` or `sacct + squeue` |
| 32 | — | while jobs are queued, a `waiting on` block says what they are waiting for. This is unrecoverable once the run ends |
| 33 | quit the app entirely, relaunch, `/jobs <name>` | same run, same states — the registry survives the process |
| 34 | `/check all` | the run appears under `ACTIVE`, not under `NEEDS ATTENTION` |
| 35 | `/monitor <name> 300` | redraws only when the counts change; Ctrl+C stops watching and says the run keeps going |
| 36 | after ~1 h | trimming complete on all 9; STAR jobs `PENDING` or `RUNNING` |
| 37 | after ~1–2 days | every job `COMPLETED`; `/check` says `complete`, monochrome |
| 38 | — | `report/` and the MultiQC html exist under `-o` |
| 39 | inspect the DE output | a table of genes with fold-change and adjusted p-value for `Tks5hh_vs_WT` |
| 40 | — | the PCA/clustering plot separates WT from Tks5hh, or you have learned something |

Action 33 is the one that matters most and the one no other case tests. A run
this long outlives the session that started it. If the state does not survive a
restart, the app does not work for real data no matter what cases 1–3 say.

Actions 31 and 32 are the second half of that. `/check` used to report GenPipes'
`log_report`, which never contacts Slurm — on a 46-job run that died at 10:12 it
reported `2 RUNNING, 43 PENDING` for hours. A run of this length is exactly where
that lie is expensive, because nobody watches a two-day run closely. Confirm the
footer names `sacct`: if it does not, the old path is still live.

Action 40 is not an assertion about the code. If the groups do not separate, that
is a result about the biology, not a bug — record it and say so.

## What to record

Write `testcases/last-run-4.json` by hand if the run is worth keeping: the
generated command, the job IDs, the state transitions observed, wall-clock per
step, and the actual core-hours from `sacct`. The last of those is the only way
the runtime estimates in this file ever become measurements instead of guesses.

## Cleaning up

Nine whole-node STAR jobs are not cheap. If you stop partway:

```bash
squeue -u $USER -o "%i %j" | grep -i rnaseq
scancel <ids>
```

And the output is 400–600 GB. Delete it when you are done with it, or the next
person to run this case will find the filesystem full.
