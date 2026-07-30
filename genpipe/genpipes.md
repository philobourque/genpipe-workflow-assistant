# GenPipes on the Digital Alliance (v6.1.1)

Written against Rorqual and Narval. They differ in exactly one place that
matters — the cluster ini, which carries the partitions, the walltimes and the
resource macros — so everywhere this document says "the cluster ini" it means
`common_ini/<cluster>.ini` for the machine you are actually logged into. Get it
from `hostname` rather than from memory: `rorqual*` takes `rorqual.ini`,
`narval*` takes `narval.ini`. Using the wrong one generates and submits, and
then every job is rejected for a partition that does not exist there.

This is the basis: what is true for every GenPipes run, and what `--help` cannot
tell you. Everything pipeline-specific — flags, protocol values, step numbers,
what each step does — comes from `--help` at the moment you need it.

Nothing in this document submits a job. Submission is one explicit act, named in
"Generate versus submit", and it is the only consequential step.

## 1. How to find things out

`genpipes <pipeline> --help` is authoritative and free. It prints the complete
flag set, the legal `-t` protocol values, the **full numbered step list for every
protocol**, and a description of what each step does. It is version-exact,
because it is the install talking about itself.

Read it before generating a command for a pipeline. Never state a step number
from memory; take it from `--help`. If a generation is rejected for an
out-of-range step, re-read `--help` before changing anything else.

Reading is free. `--help`, `ls`, log inspection and `sacct` consume no
allocation. So does generation itself — see section 3.

## 2. Environment

GenPipes v6 is a Python package delivered through CVMFS and reached with a
module. It runs under its own Python 3.13, separate from this agent's
interpreter. The module load is the boundary between them.

Every invocation loads the module in the same subprocess, joined with `&&`:

```
module load mugqic/genpipes/6.1.1 && genpipes <pipeline> ...
```

A bare `genpipes` call is invalid. `$GENPIPES_INIS` does not exist until the
module is loaded, so write ini paths with the variable and let the loaded module
resolve them — never expand it yourself, never hardcode an absolute config path.

Four environment variables matter, normally set in `~/.bash_profile`:

| variable | what it is |
|---|---|
| `MUGQIC_INSTALL_HOME` | `/cvmfs/soft.mugqic/CentOS6`; roots the modulefiles, genomes and testdata |
| `GENPIPES_INIS` | set by the module; roots every config path |
| `RAP_ID` | the Alliance allocation jobs are billed to, e.g. `rrg-bourqueg-ad` |
| `JOB_MAIL` | address for job notifications |

`RAP_ID` and `JOB_MAIL` are not GenPipes settings. The cluster ini
(`common_ini/rorqual.ini`, `common_ini/narval.ini`) interpolates them straight
into every generated `sbatch` line:

```ini
cluster_other_arg = --mail-type=END,FAIL --mail-user=$JOB_MAIL -A $RAP_ID
```

GenPipes never validates them. An unset `RAP_ID` produces `-A` with nothing after
it and every job is rejected at submit time — after generation, after approval.
A wrong `JOB_MAIL` costs only notifications, silently.

## 3. Generate versus submit

These are two acts and the approval gate sits between them.

**Generation** is `genpipes <pipeline> ... -g cmd.sh`. It reads the readset,
the design or pairs, and the layered inis, resolves the requested steps, and
writes a bash script. It consumes no allocation and submits nothing. It is safe
to run and re-run, and it is the best probe available: it either succeeds or
names exactly which step, input or config option it rejected.

**Submission** is running that script. Two forms, both consequential, both
gated:

- `bash cmd.sh` sends every job to Slurm directly.
- For large runs, `genpipes tools chunk_genpipes <script> <folder>` splits it
  into scheduler-sized chunks, then `genpipes tools submit_genpipes <folder> -n N`
  submits them under a queue cap, retrying submit-time failures and holding a
  lock so two submitters cannot race. This is a distinct submission mechanism;
  recognising only `bash cmd.sh` as the submit act misses every chunked run.

**Submit exactly once.** A second `bash cmd.sh` silently queues a duplicate of
every job. There is no warning and no deduplication.

## 4. Command skeleton

```
module load mugqic/genpipes/6.1.1 && \
genpipes <pipeline> [-t <protocol>] \
  -c <ini> [<ini> ...] \
  -r <readset.tsv> [-d <design.tsv> | -p <pairs.csv>] \
  -s <steps> [-j slurm] [-o <outdir>] \
  -g <cmd.sh>
```

`-t` selects a protocol; pipelines with only one do not take it. `-r` is
required everywhere. `-d` and `-p` are mutually exclusive — never both. `-s`
takes `1-5`, `3,6,7` or `2,4-8`, with the last number from `--help`. `-j`
defaults to `slurm` on every Alliance cluster. `-g` names the generation artifact; always use
it, never the deprecated `> cmd.sh` redirect.

## 5. The `-c` stack

An ini is a plain settings file of `[section]` headers and `key = value` lines,
where **a section is usually a step name** — the same names `--help` prints:

```ini
[gatk_haplotype_caller]
nb_jobs=1
cluster_walltime = 12:00:00
```

GenPipes itself holds no numbers. Every module version, CPU count, memory
request, walltime and reference path comes from the inis. `-c` takes several,
applied left to right, later winning. Each layer answers a different question:

1. **`<pipeline>.base.ini`** — how the pipeline works at all. Always first.
2. **protocol feature ini** — what changes for this `-t`. See the table.
3. **data-type overlay** — `dnaseq.exome.ini` and friends. Orthogonal to
   protocol: it sets `experiment_type=exome` and touches both germline and
   somatic sections, so it stacks *with* the feature ini when the reads are
   capture rather than whole-genome.
4. **cluster ini** — `common_ini/<cluster>.ini`, matching the machine you are
   on: `rorqual.ini` on Rorqual, `narval.ini` on Narval. Carries partitions,
   walltimes, the resource macros and the `RAP_ID`/`JOB_MAIL` line. Check
   `hostname`; do not assume.
5. **genome ini** — only for a non-default genome. **Find it by looking, not by
   deriving it from the species name.** The directory is
   `$MUGQIC_INSTALL_HOME/genomes/species/<Species>.<assembly>/`, and it holds
   SEVERAL inis: an unversioned `<Species>.<assembly>.ini` and one per Ensembl
   release, e.g. `Mus_musculus.GRCm38.Ensembl83.ini`. The unversioned one is not
   reliably the one whose indexes exist.

   ```bash
   ls $MUGQIC_INSTALL_HOME/genomes/species/<Species>.<assembly>/*.ini
   ls $MUGQIC_INSTALL_HOME/genomes/species/<Species>.<assembly>/genome/star_index/
   ```

   Pick the versioned ini whose release has a `star_index/` built, at an
   `sjdbOverhang` matching read length minus one (101 bp reads want
   `sjdbOverhang99`). This matters because the naming rule is right for human
   and wrong elsewhere: `Mus_musculus.GRCm38.ini` pins Ensembl 102, while the
   only STAR index built for GRCm38 on this install is Ensembl 83. That run
   generates cleanly, submits cleanly, and dies at `star_align` with no index —
   the worst shape of failure, because everything before it looked right.
6. **your own overrides** — last word.

A private override ini is just a file with the sections you want to change,
appended last. That is how to tune a resource. Never edit anything under
`$GENPIPES_INIS`.

Getting layer 2 wrong does not crash. It generates and runs, with the wrong
parameters, for hours.

### Feature inis by pipeline

Confirm names against `ls $GENPIPES_INIS/<pipeline>/` if one does not resolve.

| pipeline | protocols (`-t`) | feature ini between base and cluster | design / pairs |
|---|---|---|---|
| `dnaseq` | `germline_snv` | — | neither |
| | `germline_sv` | `dnaseq.sv.ini` | neither |
| | `germline_high_cov` | `dnaseq.high_cov.ini` | neither |
| | `somatic_tumor_only` | — | neither |
| | `somatic_fastpass` | `dnaseq.cancer.ini` | `-p` |
| | `somatic_ensemble` | `dnaseq.cancer.ini` | `-p` |
| | `somatic_sv` | `dnaseq.cancer.ini` | `-p` |
| `rnaseq` | `stringtie` (default) | — | `-d` |
| | `variants`, `cancer` | — | neither |
| `chipseq` | `chipseq`, `atacseq` | — | `-d` conditional |
| `methylseq` | `bismark`, `gembs` | — | `-d` |
| | `hybrid` | `methylseq.hybrid.ini` | `-d` |
| | `dragen` | `methylseq.dragen.ini` | `-d` |
| `longread_dnaseq` | `nanopore`, `revio` | — | `-d` |
| | `nanopore_paired_somatic` | `longread_dnaseq.cancer.ini` | `-p` |
| `rnaseq_denovo_assembly` | `trinity`, `seq2fun` | — | `-d` |
| `nanopore_covseq` | `default`, `basecalling` | `ARTIC_v4.ini` / `ARTIC_v4.1.ini` by primer scheme | `-d` |
| `covseq` | none | `ARTIC_v4.ini` / `ARTIC_v4.1.ini` by primer scheme | `-d` |
| `ampliconseq`, `rnaseq_light` | none | — | `-d` |

`<pipeline>.batch.ini` is a separate overlay for `-b` batch-effect correction,
not a protocol ini. `cit.ini` is the test overlay — see section 10.

## 6. Readset file

Tab-separated, one readset per row, passed with `-r`. Required by every
pipeline.

Mandatory: `Sample`, `Readset`, `RunType` (`PAIRED_END` or `SINGLE_END`), `Run`,
`Lane`, and either `FASTQ1`/`FASTQ2` or `BAM`. Optional: `Library`, `Adapter1`,
`Adapter2`, `QualityOffset`, `BED`.

**Sample names accept only `A-Z`, `a-z`, `0-9`, `-` and `_`.** A dot or space is
a silent source of trouble downstream.

ChIP-seq adds `MarkName` (the histone mark or binding protein) and `MarkType`
(control versus treatment). Long-read pipelines expect FAST5 or BAM paths.

A malformed readset is the most common generation failure. Validate rather than
eyeball it — section 9.

## 7. Design file

Tab-separated, describing contrasts for a differential analysis. Required for
rnaseq `stringtie`; used by chipseq differential binding, where an absent or
invalid design skips that step rather than erroring.

First column is `Sample`, and the names must match the readset exactly. For
chipseq the second column is `MarkName` (`atac` under the `atacseq` protocol).
Every remaining column is one named contrast, its header the contrast name, and
each cell that sample's role in it:

- `1` — control group
- `2` — treatment group
- `0` or blank — excluded from this contrast

Three groups are expressed as several pairwise contrast columns, not one column
with three values. A group with fewer than two samples is skipped.

## 8. Pairs file

Comma-separated, passed with `-p`, mapping each tumor sample to its matched
normal. Used only by the paired somatic protocols in the table above.

## 9. Pre-flight

Two checks that cost nothing and catch most of what fails after approval:

```
genpipes tools validate_genpipes -p <pipeline> -r <readset.tsv> [-d <design.tsv>]
genpipes <pipeline> ... --sanity-check
```

`validate_genpipes` checks readset and design structure. `--sanity-check`
verifies that every input file the run needs actually exists. Prefer either over
reasoning about a file's contents.

## 10. Running something cheaply

Every pipeline ships `$GENPIPES_INIS/<pipeline>/cit.ini`, the continuous-
integration overlay. Appended **last** to `-c`, it repoints the genome to the
chr19 subset, repoints annotations to `$MUGQIC_INSTALL_HOME/testdata/`, scopes
callers to chr19, and cuts nearly every walltime to ten minutes.

Matching inputs live in `$MUGQIC_INSTALL_HOME/testdata/<pipeline>/` — readsets,
designs, pairs and raw reads, with absolute paths already in them.

A CIT run is a real run: real generation, real layering, real submission, real
job list, real Slurm states. Only the data and the walltimes are small. It is
the right answer to "will this command work" when the honest answer needs a
cluster.

## 11. After submission

Submission writes `job_output/<Pipeline>.<protocol>.job_list.<TIMESTAMP>`, for
example `DnaSeq.germline_snv.job_list.2026-07-08T14.46.07`. That file ties one
run to its own job IDs and is the anchor for everything below. It lands wherever
the submission ran from; resolve its real path rather than assuming.

Scoped to one run — prefer these:

```
genpipes tools log_report --loglevel ERROR --tsv log.out <job_list_path>
```

Per-step state and counts for that run alone. Use `--loglevel ERROR` to suppress
routine warnings about queued jobs that have not written logs yet.

Account-wide, and therefore mixed with unrelated work: `squeue -u $USER` for
what is active now, `sacct -j <ids>` for terminal states of specific jobs.
Never scope `sacct` with `-u $USER` when asking about one run.

Success is `MUGQICexitStatus:0` or `Exit_status=0`. Output lands in `report/`
(including `report/<Pipeline>.<protocol>.multiqc.html`) and pipeline-specific
folders such as `DGE/` or `peak_call/`. Errors land in `job_output/`.

## 12. Diagnosing a failure

A Slurm state is the manner of death, not the cause. `TIMEOUT` says the job was
killed at its walltime, not why it needed more. `FAILED` says it exited nonzero,
not what broke. Treating state as cause produces fixes that do not work.

First: did the run reach the scheduler at all? No `job_output` for the run's
timestamp means it failed at generation, and the error is in the generation
command's own stderr, which names the offending section or file. There are no
per-job artifacts to read.

For a run that did reach the scheduler, five artifacts, all tied by the run's
timestamp. Glob on the timestamp; never derive a path by splitting a job name on
dots.

**`log_report`** maps job IDs to steps. Starting point only — a diagnosis that
reads nothing else is not a diagnosis. Two of its states mislead, because it
reads `.o` prologues and epilogues rather than asking Slurm: `RUNNING` can mean
a job killed so hard it never wrote an epilogue, so confirm with `sacct`; and
`CANCELLED` is usually a dependency cascade, not a real cancel — the fix is
upstream, and a job that never ran has no cause of its own.

**`sacct -j <id> --format=JobID,JobName%40,State,ExitCode,Elapsed,Timelimit,MaxRSS,ReqMem`**
says how it died and by how much it missed. Killed at 3:00:06 against 3:00:00
means it was working when killed; dead at 0:00:33 means it hit an error.

**The `.o` log**, `job_output/<step_dir>/<jobname>_<TIMESTAMP>.o` — what the
tool was doing, and its error message.

**The `.sh` script**, same directory — what the tool was *told* to do, with
every flag and input path. This distinguishes "needed more resources" from
"given the wrong input". It is the artifact most often skipped and most often
decisive. Read it before proposing any fix.

**The config trace**, `<Pipeline>.<protocol>.<TIMESTAMP>.config.trace.ini` in the
generation directory — which ini section produced that value. It can list
options a step never used, so cross-check against the `.sh`.

Reporting rules:

- State manner and cause as separate claims. Manner from `sacct`; cause needs
  the `.o` and the `.sh`.
- "The logs do not show why" is a correct answer. Do not invent a plausible
  cause to fill the gap.
- Preserve uncertainty to the surface. If a step concluded "likely", say
  "likely".
- Never propose a resource value that cannot be computed from what was observed
  and traced to a config section. Quote the section and its current value.
- Resubmit the **full original `-s` range**, never one narrowed to the failure.
  GenPipes skips steps whose output is up to date, so the full range costs
  nothing extra; everything downstream of a failure was `CANCELLED` and has no
  output to skip against. Narrowing is how a run ends up silently half finished.
- Put a resource fix in a private override ini **appended last to `-c`**. One
  that is not last is overruled, which looks exactly like a fix that did nothing.

Worked example. Job 15985499 ended `TIMEOUT`, killed at 3:00:06 against a
3:00:00 limit at 99.2% CPU efficiency — working when killed. `log_report` puts
it at step 20, `metrics_verify_bam_id`. The config trace shows
`[verify_bam_id] cluster_walltime = 3:00:00`. The obvious fix is to raise the
walltime, and it is wrong. The `.sh` shows `VerifyBamID` pointed at a
genome-wide 100,000-marker panel against a single-chromosome CRAM, and the `.o`
shows it walking chr1–chr6 hunting markers the file cannot contain. More time
would only buy more futile search. The fix is to skip the step for
chromosome-subsetted data or supply a matching panel — visible only in the
`.sh`.

The other two classes resolve elsewhere. Out of memory: `MaxRSS` at the `ReqMem`
ceiling and a `.o` that stops mid-sentence, so raise `cluster_mem` for that
section, sized from the observed `MaxRSS`. Generation failure: no `job_output`
at all and an explicit "REQUIRED parameter ... is not defined" on stderr.

## 13. Data you must not open

Everything here is done from **structure** — filenames, directory layout, column
headers, the readset/design formats, `--help`, the scheduler. None of it needs a
single read. So never `cat`, `head`, `zcat` or `grep` the contents of `*.fastq*`,
`*.bam`, `*.cram`, `*.sam`, `*.vcf*`, `*.bed`, `*.bw` or a count matrix.

Names, sizes and presence are fair game and are usually the whole answer: `ls`
says how many files, the readset says how they group, `sacct` says what happened
to them. Readset, design and pairs files are the exception — they are the
specification, not the data — but read their shape, not their contents.

If a question truly cannot be answered without opening a data file, say so and
let the person decide. Opening it quietly is not an option.

## Hard rules

- Prefix every call with `module load mugqic/genpipes/6.1.1 &&` in the same
  subprocess.
- Base ini first, cluster ini after the feature ini, `cit.ini` or private
  overrides last.
- Always generate with `-g`; never `> cmd.sh`; never combine `-g` with
  `--clean`.
- Never supply both `-d` and `-p`.
- Never take a step number from this document. There are none here by design.
- Never submit twice.
- Never read the contents of a FASTQ, BAM, CRAM, VCF or result file. Names,
  sizes and structure are enough — see section 13.
