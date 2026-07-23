# GenPipes command grammar (v6.1.1, Rorqual / DRAC)

This document is the agent's model of GenPipes. It encodes the invariant grammar: the shape of a valid invocation, the config layering rule, the mapping from pipeline and protocol to required inputs, the two-stage generate-then-submit model, and the file formats. It deliberately does not carry step counts or step lists, because those change across versions and protocols and are authoritative only from `genpipes <pipeline> -h` on the live install. The grammar tells you which slots exist and how they combine; `-h` tells you the numbers that fill the `-s` slot. Knowing where to look is part of the grammar.

Every fact here is either a rule the agent applies or a signal the agent reads. Nothing here submits a job. Submission is a separate, explicit act described in the generation-versus-submission section, and it is the only consequential step.

## Environment contract

GenPipes v6 is a Python package, not a loadable set of Python modules, and it requires Python 3.12 or newer. On Rorqual it is delivered through CVMFS and made available with a module. Two facts follow that the agent must treat as hard rules.

First, every `genpipes` invocation runs inside a subprocess that first loads the module, in the same shell, joined with `&&`:

```
module load mugqic/genpipes/6.1.1 && genpipes <pipeline> ...
```

The agent's own Python environment is 3.12.4 and is separate from the one GenPipes runs under. The `module load` prefix is the boundary between them. A bare `genpipes` call without the prefix is invalid and will fail.

Second, `$GENPIPES_INIS` does not exist until the module is loaded. It is the variable that resolves the config directory, so every ini path is written relative to it and is only meaningful after the load in the same subprocess. Do not expand `$GENPIPES_INIS` yourself and do not hardcode an absolute config path; write the paths with the variable and let the loaded module resolve them.

## Invocation skeleton

Every generation command has this shape. Square brackets mark slots that depend on the pipeline and protocol.

```
module load mugqic/genpipes/6.1.1 && \
genpipes <pipeline> [-t <protocol>] \
  -c $GENPIPES_INIS/<pipeline>/<pipeline>.base.ini [<feature>.ini ...] $GENPIPES_INIS/common_ini/rorqual.ini \
  -r <readset.tsv> [-d <design.tsv> | -p <pairs.csv>] \
  -s <steps> [-j slurm] \
  -g <cmd.sh>
```

The slots, in order of how often they are decided wrongly:

`<pipeline>` is the pipeline token, for example `rnaseq`, `chipseq`, `dnaseq`. It is the first positional argument.

`-t <protocol>` selects a protocol within the pipeline. Whether it is required, and its legal values, are pipeline-specific and listed in the per-pipeline grammar below. Pipelines with a single protocol do not take `-t`.

`-c` takes the ordered list of ini files. Order is semantic. See the config layering rule.

`-r <readset.tsv>` is required for every pipeline.

`-d <design.tsv>` and `-p <pairs.csv>` are mutually exclusive and protocol-dependent. Most pipelines take neither, differential pipelines take `-d`, paired-somatic protocols take `-p`. Never supply both.

`-s <steps>` is the step range, for example `1-N` for a whole protocol or `6-N` to resume from step 6, where the last step number N comes from `-h`, not from memory. See discovering steps.

`-j` is the scheduler. On Rorqual it defaults to `slurm`, so it may be omitted; including `-j slurm` is harmless and explicit. Use `-j pbs` only on Abacus, `-j batch` only for local container runs.

`-g <cmd.sh>` writes the generated commands to a script file. This is the generation artifact and the gate seam. Always generate with `-g`. Do not use the `> cmd.sh` redirect; it still works but is deprecated and it breaks the assumption that a named artifact file exists.

## Config layering rule

The `-c` list is applied left to right and later files override earlier ones, so order is meaning, not formatting. The rule is fixed:

1. The pipeline base ini first: `$GENPIPES_INIS/<pipeline>/<pipeline>.base.ini`.
2. Any protocol or feature inis in the middle, for example `$GENPIPES_INIS/dnaseq/dnaseq.cancer.ini` for somatic dnaseq, or `$GENPIPES_INIS/dnaseq/dnaseq.sv.ini` for the germline structural-variant protocol.
3. The cluster ini last, always `$GENPIPES_INIS/common_ini/rorqual.ini` on Rorqual.

The cluster ini goes last because it carries the site's scheduler and resource settings and must win over anything a pipeline default set earlier. Omitting the cluster ini is a common and silent error: the command may still generate but will not be correctly parameterized for Rorqual.

## Discovering steps and protocol details

`-s` is the one slot this document does not fill, because step numbers are version- and protocol-specific and go stale the moment either changes. The authoritative source is the pipeline's own help:

```
module load mugqic/genpipes/6.1.1 && genpipes <pipeline> -h
```

The help output lists the legal `-t` protocol values with the default marked, the full ordered list of numbered steps for the pipeline, and the complete flag set. Read it to learn the last step number for the `-s` range, to confirm a protocol name before using it, and to confirm whether a flag like `-d` or `-p` applies. Prefer the numbered step list from `-h` over any count, because resuming needs the actual step boundary, not a total.

This lookup is free. Introspection runs nothing on the cluster and consumes no allocation, and so does generation itself: a `-g` generation is a safe dry probe that either succeeds or reports exactly which step or input it rejected. Read `-h` once per pipeline and protocol at the start of a task and reuse the result across generations within that task rather than shelling out on every run. If a generation is later rejected for an out-of-range step, re-read `-h` before changing anything else; the step list has almost certainly shifted under a version bump.

## Generation versus submission

GenPipes separates writing the pipeline from running it, and the agent must keep these two acts distinct because the human-approval gate sits between them.

Generation is `genpipes <pipeline> ... -g cmd.sh`. It reads the readset, design or pairs, and the layered inis, resolves the requested steps, and writes a bash script. It consumes no allocation and submits nothing. It is safe to run and re-run.

Submission is running that script, and there are two forms. Both are the consequential act and both must pass the approval gate.

The simple form is `bash cmd.sh`, which sends every job to SLURM directly.

The DRAC-aware form uses two GenPipes tools. `chunk_genpipes.sh` is run once against the generated script to split it into scheduler-sized job chunks in a `job_chunks` folder. Then `submit_genpipes` submits those chunks, managing queue limits, resubmitting jobs that fail to reach the scheduler up to ten times, and holding a lock so two submitters cannot run against the same chunk folder at once. This is the correct path for large runs on Rorqual and it is a distinct submission mechanism from `bash cmd.sh`. The gate and the watcher must recognize both `bash cmd.sh` and the `chunk_genpipes.sh` then `submit_genpipes` sequence as submission; treating only `bash cmd.sh` as the submit act misses every chunked run.

## Per-pipeline grammar

For every pipeline below, the step range comes from `genpipes <pipeline> -h`, not from this document. What is stated here is the stable grammar: protocol names, ini layering, and whether design or pairs applies.

### rnaseq

STAR-based RNA sequencing. Protocols under `-t` are `stringtie` (the default, differential expression), `variants` (variant calling from RNA), and `cancer` (variants plus gene-fusion detection). The `stringtie` protocol takes a design file with `-d` defining comparison groups and optionally a batch file with `-b` for batch-effect correction in the Ballgown differential analysis. Readset required. Inis: `rnaseq.base.ini` then `rorqual.ini`.

### chipseq

BWA alignment, MACS2 peak calling, HOMER annotation, DiffBind differential binding. Protocols under `-t` are `chipseq` and `atacseq`. A design file drives the differential-binding steps and is not required for a peak-calling-only run: if no valid design is supplied the differential-binding step is skipped rather than erroring, so treat `-d` as conditional, required only when the run should produce differential binding. The design format is chipseq-specific (see the design file section). For `atacseq` the mark column value must be `atac`. Readset required. Inis: `chipseq.base.ini` then `rorqual.ini`.

### dnaseq

DNA sequencing, variant calling. Protocol under `-t` is required and selects one of the germline or somatic workflows. The distinguishing grammar is the feature ini and whether pairs apply.

Germline protocols take neither design nor pairs:
`germline_snv` uses `dnaseq.base.ini` then `rorqual.ini`.
`germline_sv` adds `dnaseq.sv.ini` between base and cluster.
`germline_high_cov` adds `dnaseq.high_cov.ini` between base and cluster.

Somatic protocols compare tumor against normal:
`somatic_sv`, `somatic_fastpass`, and `somatic_ensemble` add `dnaseq.cancer.ini` between base and cluster and take a pairs file with `-p pairs.csv`.
`somatic_tumor_only` runs without a normal, so it does not take `-p`; confirm its exact ini set with `genpipes dnaseq -h`, as it does not follow the paired-somatic ini pattern.

### Other pipelines on the install

`rnaseq_denovo_assembly` (Trinity de novo assembly, takes `-d` design), `rnaseq_light`, `longread_dnaseq` (protocols `nanopore` and `revio` under `-t`, no design, uses `-o` for output dir), `methylseq`, `covseq`, `nanopore_covseq`, `ampliconseq`, and `tools`. Do not assume their grammar. Query it with `genpipes <pipeline> --help` before generating.

## Readset file

Tab-separated, one readset per row. It describes samples and the location of their input sequence data and is required for every pipeline, passed with `-r`. Columns cover the sample name, the readset name, library, run, lane, adapter sequences, the quality-score offset, and the paths to the FASTQ or BAM input files. The exact column set is pipeline-specific; the long-read pipeline for instance expects paths to FASTQ, FAST5, or BAM. A malformed readset, a missing column or a path that does not exist, is a common generation failure and is diagnosable by comparing the file's header against what the pipeline expects.

## Design file

Tab-separated, describing the contrasts for a differential analysis. Required for the rnaseq `stringtie` differential run and used by chipseq differential binding; pipelines that do no differential analysis do not take one. The body is a membership matrix: the first column is the sample name, and for chipseq the second column is the mark or binding-protein name (`atac` for the atacseq protocol). Each remaining column is one named contrast, and each cell encodes that sample's role in that contrast: `1` for control, `2` for treatment, `0` or blank to exclude the sample from that contrast. A three-group comparison is expressed as several pairwise contrast columns rather than a single column. Each group in a contrast needs at least two samples or the differential step for that contrast is skipped.

## Pairs file

For paired somatic dnaseq protocols, a comma-separated file passed with `-p` that maps each tumor sample to its matched normal. Required by `somatic_sv`, `somatic_fastpass`, and `somatic_ensemble`. Not used by germline protocols or by `somatic_tumor_only`.

## Resuming

Resuming after a failure is a fresh generation with a later start step followed by a fresh submission, so it passes through both stages again and the submission is again the consequential one. To resume from step 6, regenerate with `-s 6-N`, where N is the protocol's last step from `-h`, then submit the new script. GenPipes tracks completed work: with smart restart it skips steps already finished, so re-running a range does not redo completed jobs. The step to resume from is located from the job status (see monitoring), not guessed.





## Monitoring submitted jobs

Reading job and pipeline state commits no resources and is always safe. None of these submit or modify anything. Only generation and submission consume allocation; reading logs and status is free.
The correct, run-scoped way to check a pipeline's progress is GenPipes' own log_report tool, run against that run's job list file. In v6 this is a subcommand, not a standalone script, so it takes the module load prefix like any other genpipes call:
module load mugqic/genpipes/6.1.1 && genpipes tools log_report --loglevel ERROR --tsv log.out <job_output_dir>/<Pipeline>.<protocol>.job_list.<TIMESTAMP>
The job list file is written at submission time into the run's job_output directory, named <Pipeline>.<protocol>.job_list.<TIMESTAMP>, for example DnaSeq.germline_snv.job_list.2026-07-08T14.46.07. It lives wherever the submission ran from, so resolve its actual path rather than assuming; if it is not in the current directory's job_output, search for it with: find . -name "job_list". This file is the anchor that ties one pipeline run to its own job IDs.
log_report reads that job list and prints a summary scoped to that single run: the number of jobs COMPLETED, PENDING, and the total, plus a per-step breakdown. This is how progress like "4 of 56 complete" is obtained, and how a failed step is located for a resume. Use --loglevel ERROR to suppress the routine warnings about pending jobs that have not yet written logs (those warnings are expected for jobs still queued, not errors), and --tsv <file> to write the detailed per-step table to a file.
Prefer log_report over the raw scheduler commands for tracking a specific run, because the scheduler commands are account-wide and mix in unrelated jobs:
squeue -u $USER lists the user's currently queued and running jobs across the whole account; it does not show completed jobs, since a finished job leaves the queue. Use it for a quick "what is active right now" view, not for per-run progress.
sacct -j <jobid>[,<jobid>...] shows the state and exit status of specific jobs, including completed and failed ones. Scope it to a run's own job IDs, not to -u $USER, which returns every job on the account across unrelated runs and gives a misleading picture.
scontrol show job <jobid> shows detail for a single pending or running job.
Job states to expect: PENDING, RUNNING, COMPLETED, FAILED, CANCELLED, TIMEOUT, OUT_OF_MEMORY, NODE_FAIL, PREEMPTED. A job must pass through RUNNING before it can be COMPLETED, so a completed job that was never observed running simply finished between checks.


## Success and failure signals

A clean job reports `MUGQICexitStatus:0` or `Exit_status=0`. On failure the pipeline aborts and the cause is written into the `job_output` folder; the specific step's output file there holds the error. A successful run also leaves a report, for example `report/<Pipeline>.<protocol>.multiqc.html`, and pipeline-specific result folders such as `DGE` for rnaseq differential expression. When classifying an outcome, read the exit status first, then the `job_output` file for the failing step, then `log_report.py` for which step failed. These are the signals the retry loop keys on; do not infer success or failure from raw stdout alone.

## Diagnosing a failed or timed-out job

A job's SLURM state is the manner of death, not the cause. TIMEOUT means the scheduler killed the job at its walltime limit; it does not say why the job needed more time than it was given. FAILED means the job exited nonzero; it does not say what went wrong. Treating the state as the cause is the most common diagnostic error, and it produces fixes that do not work: raising a walltime when the real problem is that the tool was pointed at the wrong input only buys more time to do the wrong thing.

First establish whether the run reached the scheduler at all. If there is no job_output directory for the run's timestamp, the failure happened at generation, before any job was submitted, and there are no per-job artifacts to read. GenPipes' own sanity check aborts generation for a malformed readset, a config path that does not exist, or a required parameter that is not set, and it names the offending section and option in its stderr. Diagnose this class from that message and the readset or ini it points to, not from job_output.

For a run that reached the scheduler, five artifacts exist, each answering a different question. They are all tied together by one timestamp, the run's timestamp, which appears in every filename. Resolve artifacts by globbing on that timestamp. Never derive a path by splitting a job name on dots: the job name is not the step directory.

log_report, run against the run's job list, says which pipeline step a job ID belongs to and what state it ended in. It is the only artifact that maps job IDs to steps. It is the starting point and nothing more. A diagnosis that reads only this is not a diagnosis.

Two of the states log_report reports are not what they look like, because it derives status by reading the .o file's prologue and epilogue, not by asking the scheduler. RUNNING means a prologue was written but no epilogue: a live job, or a job killed so hard (out of memory, node failure) that it died before its epilogue ran. Never read RUNNING as "still alive" for a stalled run; confirm the terminal state with sacct. CANCELLED is often not a real cancel but a dependency cascade: log_report stamps a job CANCELLED when any job it depends on did not complete. Trace the dependency list before diagnosing a CANCELLED job, and never write a cause for a job that never ran; the fix is upstream.

sacct -j <jobid> --format=JobID,JobName%40,State,ExitCode,Elapsed,Timelimit,MaxRSS,ReqMem says how the job died and by how much it missed its resources. Compare Elapsed to Timelimit and MaxRSS to ReqMem. A job killed at 3:00:06 against a 3:00:00 limit was working right up to the moment it was killed. A job that died at 0:00:33 hit an error.

This split decides whether a resource change is the fix. A job whose MaxRSS sits at or near its ReqMem, or whose state is OUT_OF_MEMORY, died for lack of memory: raising cluster_mem for that step, sized from the observed MaxRSS, is the correct fix. A job killed at its walltime while working may genuinely need more time, or may be doing futile work it should not do at all, and the .sh and .o decide which. Raising a resource is right for a true shortfall and wrong for futile work; the state alone never tells you which, so do not carry one reflex across both.

The .o log, at job_output/<step_dir>/<jobname>_<TIMESTAMP>.o, says what the tool itself was doing. It holds the tool's own output and any error message.

The .sh script, at job_output/<step_dir>/<jobname>_<TIMESTAMP>.sh, says what the tool was told to do. This is the exact command GenPipes generated, with every flag and every input path. It is the artifact that distinguishes "the job needed more resources" from "the job was given the wrong input", and it is the one most often skipped. Read it before proposing any fix.

The config trace, <Pipeline>.<protocol>.<TIMESTAMP>.config.trace.ini in the directory the pipeline was generated from, says which ini section and which value produced that command. A fix proposal must name the section and the current value, for example [verify_bam_id] cluster_walltime = 3:00:00. A value that cannot be traced to a config line is a guess and must not be presented as a fix. Note that the config trace can contain settings a step does not actually use, so cross-check any option against the .sh that really ran.

Rules for reporting a diagnosis.

State the manner and the cause as separate claims. The manner comes from the SLURM state and sacct. The cause requires the .o log and the .sh script.

If the artifacts do not determine the cause, say so. "The logs do not show why" is a correct and acceptable answer. Do not construct a plausible explanation to fill the gap, and do not infer the internal behaviour of a tool whose documentation you do not have.

Preserve uncertainty all the way to the surface. If an intermediate step concluded "likely", the final report says "likely". Never present a hedged inference as a settled fact.

Do not propose a new resource value unless it can be computed from what was observed and traced to a config section. Quote the section and the current value.

Worked example, from a real run.

Job 15985499 ended in TIMEOUT. sacct shows it killed at 3:00:06 against a 3:00:00 limit at 99.2% CPU efficiency, so it was working when killed. log_report identifies it as step 20, metrics_verify_bam_id. The config trace shows [verify_bam_id] cluster_walltime = 3:00:00, which is the source of the limit.

At that point the obvious fix is to raise the walltime, and it is wrong.

The .sh script shows what the tool was actually told to do:
  VerifyBamID --SVDPrefix $VERIFYBAMID_HOME/resource/1000g.phase3.100k.b38.vcf.gz.dat
              --BamFile alignment/NA24385_chr19/NA24385_chr19.sorted.dup.cram
A genome-wide 100,000-marker panel, against a file containing a single chromosome. The .o log confirms it: the tool was walking chr1 through chr6 when it was killed, searching for markers in chromosomes the file does not contain.

The cause is therefore not an insufficient time limit. Raising the walltime would only pay for more of a futile search. The correct responses are to skip the step for chromosome-subsetted data, or to supply a marker panel that matches the data.

This cause is visible only in the .sh script. It cannot be reached from log_report, from sacct, or from the config trace.

That is the futile-work branch of timeout. Two other classes resolve elsewhere. Out of memory: sacct shows MaxRSS at 30G against a 30G ReqMem and the .o stops abruptly with no tool error, so the cause is the memory ceiling and the fix is a higher cluster_mem in that step's section. Generation failure: the command exits before submission with "Error: REQUIRED parameter [section] option is not defined" and no job_output exists for the run, so the cause and the fix are both in the config or readset the message names, and there is no per-job artifact to read.

## Constraints and gotchas

Always prefix with `module load mugqic/genpipes/6.1.1 &&` in the same subprocess; `$GENPIPES_INIS` is undefined otherwise.

Always put the cluster ini (`rorqual.ini`) last in the `-c` list and the pipeline base ini first.

Always generate with `-g cmd.sh`; do not use `> cmd.sh` (deprecated) and do not combine `-g` with `--clean` (use `>` for clean output instead).

Never supply both `-d` and `-p`.

Never take a step number from this document; there are none here by design. Read `-s` bounds from `genpipes <pipeline> -h`.

## Self-tests

These check comprehension, not transcription. The full answers are deliberately not written here; the document is sufficient only if the correct command can be assembled from the rules above.

Test one. Build an rnaseq stringtie generation for a project with readset `readset.tsv` and design `design.tsv`, all steps, output script `rnaseq_cmd.sh`, layered for Rorqual. A correct answer must load the module, read the step range from `genpipes rnaseq -h` rather than assuming one, use `-t stringtie` or rely on it as the default, layer `rnaseq.base.ini` before `rorqual.ini`, include `-d design.tsv`, and end in `-g rnaseq_cmd.sh`. Then state that submission is a separate `bash rnaseq_cmd.sh`.

Test two. Build a dnaseq somatic_ensemble generation with readset `readset.tsv` and pairs `pairs.csv`, all steps, output script `dnaseq_cmd.sh`, for Rorqual. A correct answer must use `-t somatic_ensemble`, layer three inis in the order `dnaseq.base.ini`, `dnaseq.cancer.ini`, `rorqual.ini`, include `-p pairs.csv` and no `-d`, take the step range from `-h`, and end in `-g dnaseq_cmd.sh`. If the somatic feature ini or the pairs flag is missing, the document was not internalized.
