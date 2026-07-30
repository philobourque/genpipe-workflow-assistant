# Case 3 — production

**Costs real allocation and hours of wall time. Run it deliberately, with a
reason, and not often.**

Everything real: real data, the whole genome, the full step range, production
walltimes. This is the only case that can catch a problem which appears only at
scale, and the only one expensive enough to need justifying.

```bash
./testcases/run.sh 3 --confirm
```

The `--confirm` is required. Without it the runner prints what it would do and
stops.

## When to run it

- before a release that touched generation, submission, or the ini stack
- after a GenPipes module version bump on the cluster
- after a change to how the agent chooses inis or protocols
- when case 2 passed but something in real use did not

Not on a schedule. A test this expensive that runs on a schedule stops being
read.

## What is different from case 2

| | case 2 | case 3 |
|---|---|---|
| genome | `Homo_sapiens.GRCh38_chr19` | full assembly |
| data | `$MUGQIC_INSTALL_HOME/testdata/` | a real project's readsets |
| `-c` stack | ends with `cit.ini` | **no** `cit.ini` |
| steps | `1-4` | the full range from `--help` |
| walltimes | ten minutes | whatever the pipeline ini says |
| jobs | tens | hundreds |
| duration | ~30 min | hours to days |

Two of those rows are the actual point. Removing `cit.ini` means the production
walltimes and resources apply for the first time. And "hundreds of jobs" is
where the queue-limit path — `chunk_genpipes` then `submit_genpipes` — engages,
which case 2 never touches.

## Actions

| # | action | expected |
|---|---|---|
| 1 | choose a real project and record which one in the run log | — |
| 2 | validate the readset before anything else: `genpipes tools validate_genpipes -p <pipeline> -r <readset> [-d <design>]` | passes |
| 3 | ask for the run in plain language, naming pipeline, protocol and files | panels fill only genuinely missing slots |
| 4 | — | the agent reads `--help` for the step range rather than assuming |
| 5 | — | the gate draws with the full production `-c` stack and **no** `cit.ini` |
| 6 | read the whole command aloud before approving | every ini is one you meant; `-o` is set; `-d`/`-p` matches the protocol |
| 7 | `--sanity-check` the same command by hand | every input file resolves |
| 8 | `/approve <name>` | submission returns job IDs |
| 9 | if the run exceeds the queue cap | `chunk_genpipes` / `submit_genpipes` is used, not a bare `bash cmd.sh` |
| 10 | `/runs`, `/jobs`, `/check` over the following hours | states track reality; the app survives being left open |
| 11 | close the terminal, reopen, `/runs` | the run is still there with its jobs — the registry is on disk, not in scrollback |
| 12 | on any failure | `/diagnose` produces a diagnosis that traces to a `.sh` and a config section |
| 13 | at completion | outputs exist; `log_report` and `sacct` agree |

Action 11 is worth the whole case on its own. A production run outlives the
session that started it, and everything the interface knows has to survive that.

## The one thing that must never happen

Submitting twice. A second `bash cmd.sh` silently queues a duplicate of every
job — no warning, no deduplication, double the allocation spent. The registry
exists to make an already-submitted run visible, and this case is where that
protection is worth the most. If a run reads `submitted`, do not approve it
again; resume from the failing step instead.

## Recording the result

Append to `testcases/production-log.md`: the date, the module version, the
pipeline and protocol, the job count, the wall-clock duration, and anything
surprising. Three entries of that are worth more than any assertion this
document could make, because they are the only record of what "normal" looks
like at production scale.
