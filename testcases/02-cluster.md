# Case 2 — a real run on the real cluster

**Costs a few core-hours. Takes about half an hour. Run it before a release, and
after touching anything to do with submission or monitoring.**

Real model, real GenPipes, real Slurm. Nothing is stubbed. The run is short
because the *data* is one chromosome and the walltimes come from GenPipes' own
CIT configuration — not because anything took a shortcut.

```bash
./testcases/run.sh 2
```

## Why CIT rather than an invented small job

GenPipes ships its own continuous-integration setup, and it is exactly the thing
this case needs.

`$GENPIPES_INIS/<pipeline>/cit.ini` is a normal ini appended last to `-c`. It
repoints the genome to `Homo_sapiens.GRCh38_chr19`, repoints annotations to
`$MUGQIC_INSTALL_HOME/testdata/`, scopes the callers to chr19, and cuts nearly
every step's walltime to ten minutes:

```ini
project_name=cit
cluster_other_arg=-A $RAP_ID
cluster_walltime = 0:10:00
cit_assembly_dir = .../Homo_sapiens.GRCh38_chr19
```

`$MUGQIC_INSTALL_HOME/testdata/<pipeline>/` holds matching readsets, designs,
pairs and raw reads, with absolute CVMFS paths already written into them — so
there is nothing to stage.

The alternative would have been to write our own short job. That would have
proved that *our* short job works. A CIT run goes through the same generation,
the same ini layering, the same `sbatch`, and writes the same job list our
monitoring parses. The only differences are the size of the data and the number
in `cluster_walltime`.

**`cit.ini` goes last in `-c`.** It is an override layer and has to win over
`rorqual.ini`'s production walltimes. Getting that order right is itself part of
what the case tests.

## Prerequisites

- on a Rorqual login node
- `RAP_ID` set to a valid allocation, `JOB_MAIL` set (the app checks both at
  startup and refuses to approve without `RAP_ID`)
- a real API key configured
- `module load mugqic/genpipes/6.1.1` succeeds

The runner checks all four before spending anything and stops with a named cause
if any is missing.

## Actions

| # | action | expected |
|---|---|---|
| 1 | launch, real model, real cluster | banner shows the real model; **no** dev-mode line |
| 2 | — | environment check passes, or names the variable that is wrong |
| 3 | ask for an rnaseq stringtie run on the CIT readset, steps 1–4 | design panel offers the CIT design file |
| 4 | — | the agent reads `genpipes rnaseq --help` rather than asserting step numbers |
| 5 | — | generation succeeds and writes `cmd.sh` |
| 6 | — | the gate draws, and the `-c` stack is `rnaseq.base.ini`, `rorqual.ini`, `cit.ini` **in that order** |
| 7 | inspect `cmd.sh` by hand | `sbatch` lines carry `-A $RAP_ID`; walltimes are the CIT ten-minute ones, not production |
| 8 | `/approve <name>` | jobs are submitted; real job IDs come back |
| 9 | — | `job_output/RnaSeq.stringtie.job_list.<TIMESTAMP>` exists |
| 10 | `/runs` | the run reads `submitted` with a job count |
| 11 | `/jobs <name>` | states come from `sacct` and change between polls |
| 12 | wait for completion | every job reaches `COMPLETED` |
| 13 | `/check <name>` | the state table sums to the manifest, and the footer reads `N/N jobs resolved` naming `sacct` — never `log_report` |
| 14 | — | `report/` and the MultiQC html exist |
| 15 | re-run the same request under a new name | the second run generates, and GenPipes skips already-complete work |

## The failure path, deliberately

A run where everything succeeds does not exercise the half of the product that
matters most. After 15, do this:

| # | action | expected |
|---|---|---|
| 16 | generate again with an override ini setting one step's `cluster_walltime` to `0:00:30` | generation succeeds |
| 17 | approve | that step is submitted and killed at its limit |
| 18 | `/jobs <name>` | one job `TIMEOUT`; the jobs downstream counted as **cancelled, not failed** |
| 19 | `/diagnose <name>` | names the failing step, quotes the `.sh` and the config section, and does **not** claim a cause the logs do not show |

Action 19 is the real assertion. The diagnosis is allowed to say "the logs do
not show why". It is not allowed to invent a plausible reason, and it is not
allowed to propose a resource value it cannot trace to a config line.

## What to record

The runner writes `testcases/last-run-2.json`: the generated command, the job
IDs, the state transitions it observed, and wall-clock timings. Keep it when
something looks wrong — the job IDs are what make a past run diagnosable after
the logs have rotated out of anyone's memory.

## Cleaning up

Jobs are billed to `RAP_ID` and are small but not free. If the runner is
interrupted, cancel what it left behind:

```bash
squeue -u $USER -o "%i %j" | grep cit
scancel <ids>
```
