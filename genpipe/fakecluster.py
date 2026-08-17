"""A fake GenPipes and a fake Slurm, on PATH.

Why this exists
---------------
Half of this tool talks to a cluster: `genpipes` generates and reports, `sbatch`
submits, `sacct` says what happened, `scancel` stops it. None of that can run on
a laptop, and running it on Rorqual costs an allocation and several minutes per
attempt. So the monitoring half -- the newest and least exercised half -- was
only ever testable by hand, on the cluster, slowly.

This module writes a directory of small shell stubs and puts it at the front of
PATH. Everything downstream then works for real: GenPipes "generates" a cmd.sh,
the script "submits" and writes a job_output/*.job_list.*, sacct answers about
those job ids, log_report prints a GenPipes-shaped report. The registry, the job
parser, the triage and every renderer run unmodified against it.

Two consequences worth having beyond the tests:

  * `./start_agent.sh --fake` is a dev mode. The whole interface can be clicked
    through on any machine -- gate, approve, check, jobs, why -- with no
    allocation and no API spend (with --fake-llm, no model either).

  * The stubs VALIDATE. `genpipes` rejects an unknown pipeline, an unknown
    protocol, a malformed -s range or a missing readset, and fails with a
    GenPipes-shaped error. That turns "does the model write correct GenPipes
    commands from genpipes.md?" -- explicitly the one thing the README said was
    untested at any price -- into a test that runs in seconds against a real
    model and a fake cluster.

States
------
A state is a canned cluster reality, chosen by name, so a scenario is
reproducible rather than whatever the cluster happened to be doing:

    happy                 every job COMPLETED
    running               a mix of RUNNING and PENDING
    failed-oom            one step OUT_OF_MEMORY, with a real-looking log
    failed-missing-input  a different failure signature, so diagnosis can't cheat
    dying                 one step FAILED, everything after it still PENDING --
                          the run sacct calls healthy and squeue calls doomed

The two failure states matter most: a /diagnose that only ever sees one kind of
breakage is not being tested, it is being demonstrated.

The one non-obvious gotcha
--------------------------
Getting a fake `module` to be reached at all is harder than it looks, and it
fails in exactly the place it matters most -- on the cluster.

On DRAC, `module` is not a binary. Lmod exports it as a bash FUNCTION, and a
function always beats PATH, so a stub named `module` in a PATH directory is
simply never called. Worse, the function is restored even after its
BASH_FUNC_module%% environment entry is removed, because DRAC also sets

    BASH_ENV=/cvmfs/soft.computecanada.ca/custom/software/lmod/lmod/init/bash

and non-interactive bash sources $BASH_ENV on startup -- which is precisely the
kind of shell agent.py runs its commands in. The real `module load
mugqic/genpipes/6.1.1` then succeeds and PREPENDS the real GenPipes to PATH,
ahead of the fake, so the tests quietly exercise the real toolchain and the fake
cluster looks broken for reasons that have nothing to do with it.

So env_for() points BASH_ENV at a small script of ours instead. It unsets the
function and re-asserts the fake bin at the front of PATH, in every shell spawned
during a test. Observed and fixed on Rorqual, 2026-07-25; without it these suites
pass on a laptop and mislead on the cluster.
"""
import contextlib
import os
import re
import shutil
import stat
import tempfile

STATES = ("happy", "running", "failed-oom", "failed-missing-input",
          "dying")

# The pipelines and protocols the stub will accept. Kept deliberately short: the
# point is that SOMETHING is rejected, so a model inventing a protocol name gets
# a GenPipes-shaped error instead of a silent success.
PIPELINES = {
    "dnaseq": ["germline_snv", "germline_sv", "germline_high_cov", "somatic_tumor_only",
               "somatic_fastpass", "somatic_ensemble", "somatic_sv"],
    "rnaseq": ["stringtie", "variants", "cancer"],
    "rnaseq_denovo_assembly": ["trinity", "seq2fun"],
    "rnaseq_light": [],
    "chipseq": ["chipseq", "atacseq"],
    "methylseq": ["bismark", "gembs", "dragen", "hybrid"],
    "nanopore_covseq": ["default", "basecalling"],
    "covseq": [],
    "ampliconseq": ["dada2"],
}

# Steps per pipeline, only so `-s 1-N` can be range-checked and `-h` can print
# something. Not the real step lists -- the real ones are read from `genpipes -h`
# at runtime by design (see genpipes.md), and duplicating them here would create
# exactly the drift that document avoids.
STEP_COUNT = 20


def _write(path, body, executable=True):
    with open(path, "w") as f:
        f.write(body)
    if executable:
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# ---------------------------------------------------------------------------
# The stubs. Written as shell/python scripts rather than monkeypatched into the
# process because the code under test really does shell out: agent.py runs
# `module load ... && genpipes ...` through bash, and a patched subprocess.run
# would prove nothing about whether that command line is correct.
# ---------------------------------------------------------------------------

_MODULE = """#!/bin/bash
# Lmod stand-in. `module load anything` succeeds and does nothing, which is all
# the code under test needs -- it loads a module purely to put genpipes on PATH,
# and the fake genpipes is already there.
exit 0
"""

_GENPIPES = r'''#!/usr/bin/env python3
"""Fake `genpipes`. Validates its arguments, then does the smallest real thing.

Validation is the point: an accepted-everything stub would let a model write
nonsense and call it a pass. Errors are shaped like GenPipes' own -- a message
on stderr and a non-zero exit -- so the agent sees what it would really see.
"""
import os
import sys
import glob
import time

STATE = os.environ.get("GENPIPE_FAKE_STATE", "happy")
STORE = os.environ["GENPIPE_FAKE_STORE"]
PIPELINES = __import__("json").load(open(os.path.join(STORE, "pipelines.json")))
STEP_COUNT = 20

# How many jobs fake_submit will create (its STEPS x SAMPLES). Stated here as
# well because the generator has to declare the total in the script's header
# BEFORE the submission runs -- which is what real GenPipes does, and what lets
# a clean exit be checked against a promise rather than merely believed.
# test_fakecluster asserts the two agree.
FAKE_STEPS, FAKE_SAMPLES = 5, 3


def die(msg):
    sys.stderr.write(f"genpipes: error: {msg}\n")
    sys.exit(2)


def flag(args, name):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
        die(f"argument {name}: expected one argument")
    return None


args = sys.argv[1:]
if not args:
    die("a pipeline is required")

# ---- genpipes tools log_report <job_list> --------------------------------
if args[0] == "tools":
    if len(args) < 2 or args[1] != "log_report":
        die(f"unknown tools subcommand: {' '.join(args[1:]) or '(none)'}")
    paths = [a for a in args[2:] if not a.startswith("-")]
    if not paths:
        die("log_report requires a job list file")
    job_list = paths[-1]
    if not os.path.exists(job_list):
        die(f"no such job list: {job_list}")

    # The manifest is the real GenPipes four-column shape and carries no state,
    # so the states come from the fake sacct store -- the same place the real
    # log_report would have had to infer them from .o files on disk.
    known = {}
    store = os.path.join(os.environ["GENPIPE_FAKE_STORE"], "sacct.db")
    if os.path.exists(store):
        with open(store) as f:
            for line in f:
                parts = line.strip().split("|")
                if len(parts) == 3:
                    known[parts[0]] = parts[2]
    states = {}
    with open(job_list) as f:
        for line in f:
            parts = line.strip().split("\t")
            if parts and parts[0]:
                states[parts[0]] = known.get(parts[0], "PENDING")
    total = len(states)
    tally = {}
    for s in states.values():
        tally[s] = tally.get(s, 0) + 1

    print("-" * 40)
    print(f"Number of jobs: {total}")
    for name in ("COMPLETED", "RUNNING", "PENDING", "FAILED", "OUT_OF_MEMORY",
                 "TIMEOUT", "CANCELLED"):
        if name in tally:
            print(f"Number of jobs {name}: {tally[name]}")
    print("Cumulative time spent on compute nodes: 3:24:11")
    print("Cumulative core time: 27:13:28")
    print("Human time spent on this pipeline: 0:41:02")
    print("-" * 40)
    sys.exit(0)

# ---- genpipes <pipeline> ... ---------------------------------------------
pipeline = args[0]
if pipeline not in PIPELINES:
    die(f"invalid choice: '{pipeline}' (choose from {', '.join(sorted(PIPELINES))})")

if "-h" in args or "--help" in args:
    print(f"usage: genpipes {pipeline} [-h] [-c CONFIG [CONFIG ...]] [-s STEPS]")
    print(f"                [-r READSETS] [-g GENPIPES_FILE] [-t PROTOCOL]")
    print("\nSteps:\n")
    protocols = PIPELINES[pipeline] or ["default"]
    for proto in protocols:
        print(f"{proto}:")
        for i in range(1, STEP_COUNT + 1):
            print(f"{i}- step_{i}")
        print()
    sys.exit(0)

protocol = flag(args, "-t") or flag(args, "--type")
allowed = PIPELINES[pipeline]
if allowed and protocol is None:
    die(f"argument -t is required for {pipeline} "
        f"(choose from {', '.join(allowed)})")
if allowed and protocol not in allowed:
    die(f"argument -t: invalid choice: '{protocol}' "
        f"(choose from {', '.join(allowed)})")

steps = flag(args, "-s") or flag(args, "--steps")
if steps:
    for part in steps.split(","):
        part = part.strip()
        bounds = part.split("-")
        if not all(b.strip().isdigit() for b in bounds if b.strip() != ""):
            die(f"argument -s: malformed step range: '{steps}'")
        nums = [int(b) for b in bounds if b.strip() != ""]
        if any(n < 1 or n > STEP_COUNT for n in nums):
            die(f"argument -s: step out of range 1-{STEP_COUNT}: '{steps}'")

readset = flag(args, "-r") or flag(args, "--readsets")
if readset and not os.path.exists(readset):
    die(f"readset file not found: {readset}")

for cfg in args:
    if cfg.endswith(".ini") and not os.path.exists(cfg):
        die(f"config file not found: {cfg}")

out = flag(args, "-g") or flag(args, "--genpipes_file")
if not out:
    die("argument -g is required: nothing to write the commands to")

# Write a cmd.sh that, when run, behaves like a real submission: it creates
# job_output/ and a job_list naming the jobs it "submitted".
#
# THE HEADER IS PART OF THE FIDELITY, not decoration. runs.reconcile() reads
# four things off a generated script -- its declared OUTPUT_DIR/JOB_LIST, its
# `# TOTAL: N jobs`, and whether its `set` line carries pipefail -- to decide
# whether a submission actually happened. A fake that omitted them made every
# offline run reconcile as "unknown", which is the correct verdict for a script
# that declares nothing and exactly the wrong thing for a fake whose whole
# purpose is that everything downstream works for real.
#
# The TIMESTAMP is fixed HERE rather than by fake_submit, because that is what
# real GenPipes does and it is what makes the job list identifiable BEFORE the
# submission runs -- which is what a baseline needs.
label = f"{pipeline.capitalize()}.{protocol or 'default'}"
stamp = time.strftime("%Y-%m-%dT%H.%M.%S")
total = FAKE_STEPS * FAKE_SAMPLES
outdir = flag(args, "-o") or flag(args, "--output-dir") or os.getcwd()
outdir = outdir if os.path.isabs(outdir) else os.path.join(os.getcwd(), outdir)
with open(out, "w") as f:
    f.write("#!/bin/bash\n")
    f.write("# Exit immediately on error\n\n")
    f.write("set -eu -o pipefail\n\n")
    f.write(f"# fake GenPipes submission script for {label}\n")
    # The step range the script was built FOR, recorded in the script.
    #
    # Real GenPipes bakes the step selection into what it emits -- a script for
    # -s 3-6 contains different jobs from one for -s 1-5 -- so a stub that
    # wrote an identical script whatever it was asked for could not be used to
    # check the one property the gate exists to guarantee: that the value shown
    # at the gate is the value the launched script was generated from. Without
    # this line that chain is untestable and the test can only assert that a
    # file exists.
    f.write(f"#   STEPS: {steps or 'all'}\n")
    f.write(f"#   TOTAL: {total} jobs\n\n")
    f.write(f"OUTPUT_DIR={outdir}\n")
    f.write("JOB_OUTPUT_DIR=$OUTPUT_DIR/job_output\n")
    f.write(f"TIMESTAMP={stamp}\n")
    f.write(f"JOB_LIST=$JOB_OUTPUT_DIR/{label}.job_list.$TIMESTAMP\n")
    f.write(f'exec "$GENPIPE_FAKE_STORE/bin/fake_submit" "{label}" "{stamp}"\n')
os.chmod(out, 0o755)
# stdout, not stderr: the agent shows the command's output in the transcript, and
# a generation that appears to have printed nothing reads as a generation that
# did nothing.
print(f"Generated {out} for {label}")
print(f"Steps: {steps or 'all'}   Protocol: {protocol or 'default'}")
sys.exit(0)
'''

_FAKE_SUBMIT = r'''#!/usr/bin/env python3
"""What a generated cmd.sh does when run: create the jobs, write the job list.

This is the moment the tool cares most about -- a submission actually happening
-- so it produces the real artifacts: a job_output tree, per-job .o logs, and a
*.job_list.* file in exactly the shape runs.parse_job_list reads back.
"""
import os
import sys
import time

STATE = os.environ.get("GENPIPE_FAKE_STATE", "happy")
label = sys.argv[1] if len(sys.argv) > 1 else "Pipeline.default"

STEPS = ["trimmomatic", "bwa_mem_sambamba_sort_sam", "picard_mark_duplicates",
         "gatk_haplotype_caller", "metrics_dna_picard"]
SAMPLES = ["sampleA", "sampleB", "sampleC"]

OOM_LOG = """Loading modules...
INFO: picard MarkDuplicates starting for {job}
Picard version 3.1.1
[Wed Jul 22 04:11:07 EDT 2026] MarkDuplicates INPUT=[alignment/{sample}.sorted.bam]
OUTPUT=alignment/{sample}.sorted.dup.bam METRICS_FILE=metrics/{sample}.dup.metrics
Exception in thread "main" java.lang.OutOfMemoryError: Java heap space
    at htsjdk.samtools.util.SortingLongCollection.<init>(SortingLongCollection.java:100)
    at picard.sam.markduplicates.MarkDuplicates.doWork(MarkDuplicates.java:283)
slurmstepd: error: Detected 1 oom_kill event in StepId={jid}.batch.
Some of the step tasks have been OOM Killed.
srun: error: node042: task 0: Out Of Memory
"""

MISSING_LOG = """Loading modules...
INFO: trimmomatic starting for {job}
Exception in thread "main" java.io.FileNotFoundException:
  raw_reads/{sample}/{sample}_R1.fastq.gz (No such file or directory)
    at java.base/java.io.FileInputStream.open0(Native Method)
    at org.usadellab.trimmomatic.Trimmomatic.main(Trimmomatic.java:33)
ERROR: readset {sample} points at a FASTQ that is not on disk
srun: error: node017: task 0: Exited with exit code 1
"""


def plan():
    """(job_name, state) for every job, according to the requested state."""
    jobs = []
    for step in STEPS:
        for sample in SAMPLES:
            jobs.append([f"{step}.{sample}", "COMPLETED"])
    if STATE == "running":
        for j in jobs[6:]:
            j[1] = "PENDING"
        for j in jobs[3:6]:
            j[1] = "RUNNING"
    elif STATE == "failed-oom":
        for j in jobs:
            if j[0].startswith("picard_mark_duplicates"):
                j[1] = "OUT_OF_MEMORY"
            elif j[0].startswith(("gatk_haplotype_caller", "metrics_dna_picard")):
                j[1] = "CANCELLED"
    elif STATE == "failed-missing-input":
        for j in jobs:
            if j[0].startswith("trimmomatic") and j[0].endswith("sampleB"):
                j[1] = "FAILED"
    elif STATE == "dying":
        # The shape the filesystem cannot record: one job died, and everything
        # behind it is queued on a dependency that will never be satisfied.
        # sacct reports 1 FAILED and the rest PENDING for as long as you ask.
        for j in jobs[1:]:
            j[1] = "PENDING"
        jobs[0][1] = "FAILED"
    return jobs


cwd = os.getcwd()
out = os.path.join(cwd, "job_output")
os.makedirs(out, exist_ok=True)
# Fixed by the generator and passed in, as real GenPipes bakes it into the
# script. It is what makes the job list nameable before the run starts.
stamp = sys.argv[2] if len(sys.argv) > 2 else time.strftime("%Y-%m-%dT%H.%M.%S")
listing = os.path.join(out, f"{label}.job_list.{stamp}")

base = 41000000
rows = []
for i, (name, state) in enumerate(plan()):
    jid = str(base + i)
    step = name.split(".")[0]
    sample = name.split(".")[-1]
    logdir = os.path.join(out, step)
    os.makedirs(logdir, exist_ok=True)
    log = os.path.join(step, f"{name}_{jid}.o")
    body = f"Loading modules...\nINFO: {name} completed successfully\n"
    if state == "OUT_OF_MEMORY":
        body = OOM_LOG.format(job=name, sample=sample, jid=jid)
    elif state == "FAILED":
        body = MISSING_LOG.format(job=name, sample=sample)
    with open(os.path.join(out, log), "w") as f:
        f.write(body)
    rows.append((jid, name, log, state))

    # APPENDED PER JOB, and flushed, because that is what the real script does
    # and the difference is the whole reason a partial submission is
    # recoverable: `sbatch` then `>> $JOB_LIST` are two statements, so a run
    # that dies half way leaves the rows for the jobs it did create. Writing
    # the manifest in one go at the end would make every interrupted run look
    # like it submitted nothing.
    #
    # GenPipes' real manifest shape, positionally exact:
    #   id \t name \t dependencies(colon-joined) \t log
    # The dependency column is populated for everything after the first job
    # precisely so that runs.parse_job_list is tested against the field that
    # used to defeat it -- a fan-in job whose dependency string is longer than
    # its own name.
    deps = ":".join(r[0] for r in rows[:i]) if i else ""
    with open(listing, "a") as f:
        f.write(f"{jid}\t{name}\t{deps}\t{log}\n")
        f.flush()
    # The line runs.submitted_ids() counts, in GenPipes' own wording. The
    # `Submitted batch job` line above it was sbatch's, which the real script
    # consumes into a variable rather than printing.
    print(f"Submitted job with ID: {jid}")

# A registry of every job this fake cluster knows about, so sacct can answer
# about a run's jobs long after the submitting process is gone.
with open(os.path.join(os.environ["GENPIPE_FAKE_STORE"], "sacct.db"), "a") as f:
    for jid, name, log, state in rows:
        f.write(f"{jid}|{name}|{state}\n")

print(f"job list: {listing}")
'''

_SACCT = r'''#!/usr/bin/env python3
"""Fake `sacct`, answering only the shape runs.query_states asks for:
--noheader --parsable2 --format=JobID,JobName,State,Elapsed,MaxRSS,ExitCode -j ids
"""
import os
import sys

STORE = os.environ["GENPIPE_FAKE_STORE"]
db = os.path.join(STORE, "sacct.db")

args = sys.argv[1:]
wanted = []
if "-j" in args:
    i = args.index("-j")
    if i + 1 < len(args):
        wanted = [x.strip() for x in args[i + 1].split(",") if x.strip()]

known = {}
if os.path.exists(db):
    with open(db) as f:
        for line in f:
            parts = line.strip().split("|")
            if len(parts) == 3:
                known[parts[0]] = (parts[1], parts[2])

MEM = {"OUT_OF_MEMORY": "8192000K", "COMPLETED": "2140528K", "FAILED": "312044K"}
CODE = {"OUT_OF_MEMORY": "0:125", "FAILED": "1:0", "CANCELLED": "0:15",
        "COMPLETED": "0:0", "TIMEOUT": "0:1"}

for jid in wanted:
    if jid not in known:
        continue
    name, state = known[jid]
    elapsed = "00:00:00" if state == "PENDING" else "00:14:22"
    # A real sacct writes cancellation as "CANCELLED by 3001234"; keeping that
    # here is what proves runs.query_states strips it rather than treating the
    # whole phrase as a state.
    shown = "CANCELLED by 3001234" if state == "CANCELLED" else state
    print(f"{jid}|{name}|{shown}|{elapsed}|{MEM.get(state, '')}|{CODE.get(state, '0:0')}")
    # Real sacct also emits .batch/.extern accounting rows for each job. They
    # are here on purpose: dropping them is runs.query_states' job, and a stub
    # that never emits them would never test that.
    print(f"{jid}.batch|batch|{shown}|{elapsed}|{MEM.get(state, '')}|{CODE.get(state, '0:0')}")
'''

_SCANCEL = r'''#!/usr/bin/env python3
"""Fake `scancel`. Flips the named jobs to CANCELLED in the fake sacct store,
so a /cancel is observable afterwards rather than just not erroring."""
import os
import sys

STORE = os.environ["GENPIPE_FAKE_STORE"]
db = os.path.join(STORE, "sacct.db")
targets = set()
for a in sys.argv[1:]:
    if not a.startswith("-"):
        targets.update(x for x in a.split(",") if x)

if not os.path.exists(db) or not targets:
    sys.exit(0)

rows = []
with open(db) as f:
    for line in f:
        parts = line.strip().split("|")
        if len(parts) == 3 and parts[0] in targets:
            parts[2] = "CANCELLED"
        rows.append("|".join(parts))
with open(db, "w") as f:
    f.write("\n".join(rows) + "\n")
sys.stderr.write(f"scancel: cancelled {len(targets)} job(s)\n")
'''

_SQUEUE = r'''#!/usr/bin/env python3
"""Fake `squeue`: the pending and running jobs from the fake sacct store.

Understands the two shapes this app asks for -- a bare listing for a human, and
`-o '%i|%T|%r' --jobs=...` for runs.query_reasons. The reason column is the
whole point of the second one: sacct records `None` for every reason on this
cluster, so a pending job's reason exists in exactly one place and stops
existing the moment the job leaves the queue.

A pending job downstream of something that already broke reports
DependencyNeverSatisfied, which is the case that matters: sacct still calls
that job PENDING, and reading PENDING as "queued and healthy" is how a dead run
reports itself as alive.
"""
import os
import sys

STORE = os.environ["GENPIPE_FAKE_STORE"]
db = os.path.join(STORE, "sacct.db")

args = sys.argv[1:]
fmt = None
wanted = None
for i, a in enumerate(args):
    if a in ("-o", "--format") and i + 1 < len(args):
        fmt = args[i + 1]
    elif a.startswith("--jobs="):
        wanted = {x for x in a[len("--jobs="):].split(",") if x}
    elif a in ("-j", "--jobs") and i + 1 < len(args):
        wanted = {x for x in args[i + 1].split(",") if x}

rows = []
if os.path.exists(db):
    with open(db) as f:
        for line in f:
            parts = line.strip().split("|")
            if len(parts) == 3:
                rows.append(parts)

doomed = any(p[2] in ("FAILED", "OUT_OF_MEMORY", "TIMEOUT", "NODE_FAIL")
             for p in rows)

def reason(state):
    if state == "RUNNING":
        return "None"
    return "DependencyNeverSatisfied" if doomed else "Dependency"

live = [p for p in rows if p[2] in ("PENDING", "RUNNING")
        and (wanted is None or p[0] in wanted)]

if fmt:
    for jid, name, state in live:
        out = (fmt.replace("%i", jid).replace("%T", state)
                  .replace("%j", name).replace("%r", reason(state)))
        print(out)
else:
    print("JOBID    NAME                 STATE")
    for jid, name, state in live:
        print(f"{jid:<9}{name:<21}{state}")
'''

_SBATCH = """#!/bin/bash
# Present so a model reaching for sbatch directly gets a plausible answer
# rather than "command not found" -- which would send it down a debugging path
# that has nothing to do with what is being tested.
echo "Submitted batch job 49999999"
"""


def build(root, state="happy"):
    """Materialise the stubs under `root`. Returns the bin directory."""
    import json

    bindir = os.path.join(root, "bin")
    os.makedirs(bindir, exist_ok=True)
    with open(os.path.join(root, "pipelines.json"), "w") as f:
        json.dump(PIPELINES, f)
    # Truncate the job database so each activation starts from a clean cluster.
    open(os.path.join(root, "sacct.db"), "w").close()

    # A config stack that EXISTS. Every real pipeline requires -c -- argparse
    # says so on its own usage line, and the gate refuses a generation without
    # one before it will draw a box. A fake LLM that emitted commands with no
    # -c was modelling something GenPipes would reject, so dev mode and the
    # mock suite were exercising a path a real run can never take.
    #
    # Written here rather than left to each caller because the stub validates
    # that every .ini on the command line is on disk, which is the check that
    # makes the fixture honest in the first place.
    inis = os.path.join(root, "inis")
    os.makedirs(os.path.join(inis, "common_ini"), exist_ok=True)
    for pipeline in PIPELINES:
        _write(os.path.join(inis, f"{pipeline}.base.ini"),
               "[DEFAULT]\ncluster_server=rorqual\n", executable=False)
    _write(os.path.join(inis, "common_ini", "rorqual.ini"),
           "[DEFAULT]\ncluster_server=rorqual\n", executable=False)

    # Sourced by every non-interactive bash the fake cluster's commands run in.
    # See the module docstring: this is what actually keeps Lmod out of the way.
    _write(os.path.join(root, "bashenv.sh"), f"""\
# Loaded via BASH_ENV in every shell the fake cluster runs a command in.
# Lmod defines `module` as a shell function, which beats any stub on PATH, and
# a real `module load` would put the real GenPipes ahead of ours.
unset -f module ml 2>/dev/null || true
PATH="{bindir}:$PATH"
export PATH
""", executable=False)

    _write(os.path.join(bindir, "module"), _MODULE)
    _write(os.path.join(bindir, "genpipes"), _GENPIPES)
    _write(os.path.join(bindir, "fake_submit"), _FAKE_SUBMIT)
    _write(os.path.join(bindir, "sacct"), _SACCT)
    _write(os.path.join(bindir, "scancel"), _SCANCEL)
    _write(os.path.join(bindir, "squeue"), _SQUEUE)
    _write(os.path.join(bindir, "sbatch"), _SBATCH)
    return bindir


def env_for(root, state="happy", env=None):
    """A copy of `env` with the fake cluster in front of PATH.

    Use this for a subprocess. activate() is the in-process equivalent.
    """
    env = dict(os.environ if env is None else env)
    env["PATH"] = os.path.join(root, "bin") + os.pathsep + env.get("PATH", "")
    env["GENPIPE_FAKE_STORE"] = root
    env["GENPIPE_FAKE_STATE"] = state
    # Where the fake config stack lives, under the same name the real module
    # exports -- so a generated command reads the way a real one does and the
    # shell resolves it to files that are actually there.
    env["GENPIPES_INIS"] = os.path.join(root, "inis")
    # See the module docstring. All three are needed: the inherited function
    # entries, and the BASH_ENV that would put the function back regardless.
    for key in list(env):
        if key.startswith("BASH_FUNC_"):
            del env[key]
    env["BASH_ENV"] = os.path.join(root, "bashenv.sh")
    return env


def activate(state="happy", root=None):
    """Put the fake cluster in front of this process's PATH. Returns a label.

    The label is what display.ready() prints, so dev mode announces which canned
    reality is loaded. A simulation you can mistake for the real thing is worse
    than no simulation.
    """
    if state not in STATES:
        state = "happy"
    root = root or tempfile.mkdtemp(prefix="genpipe_fake_")
    build(root, state)
    prepared = env_for(root, state)
    for key in ("PATH", "GENPIPE_FAKE_STORE", "GENPIPE_FAKE_STATE", "BASH_ENV",
                "GENPIPES_INIS"):
        os.environ[key] = prepared[key]
    for key in [k for k in os.environ if k.startswith("BASH_FUNC_")]:
        del os.environ[key]
    return f"fake cluster ({state})"


# ---------------------------------------------------------------------------
# A model stand-in for dev mode.
#
# The fake cluster removes the need for an allocation; this removes the need for
# an API key, so the entire interface can be driven for free. It is not trying to
# be clever -- it reads the pipeline and protocol out of whatever was typed and
# produces the conversation a cooperative model would, which is all that is
# needed to exercise the gate, the transcript, the spinner and every command.
# ---------------------------------------------------------------------------

_KNOWN_PROTOCOLS = {p: v for p, v in PIPELINES.items()}


class DevLLM:
    """Enough of a model to drive the interface, with no API call.

    Reasons over the STRUCTURE of the conversation -- how many generations the
    assistant has emitted, how many submissions, how many rejections it has been
    handed -- rather than searching the text for keywords.

    That distinction was a real bug, worth recording. The first version asked
    questions like `"bash cmd.sh" not in conversation` to decide whether to
    submit yet, and `"dnaseq" in conversation` to pick a pipeline. Both were
    always true from the very first turn, because the conversation includes the
    system prompt, and the system prompt contains the whole of genpipes.md --
    which naturally mentions every pipeline name and shows `bash cmd.sh` as an
    example. So it generated dnaseq for an rnaseq request and skipped straight to
    a conclusion without ever submitting. Only the user's own messages describe
    what the user asked for.
    """

    def __init__(self):
        self.calls = 0

    def invoke(self, messages):
        from langchain_core.messages import AIMessage

        self.calls += 1
        # Scoped to the CURRENT run, not the whole thread. One conversation can
        # produce any number of runs one after another (see cli._repl), and
        # without this a second "run rnaseq stringtie steps 1-5" typed right
        # after the first one had already been generated and submitted was
        # read against the FIRST run's tally: generations == 1, submissions ==
        # 1 already, so the branches below fell straight through to "The
        # pipeline was submitted" instead of asking about or building the
        # second one. _run_start finds where the current run began; everything
        # before it belongs to an earlier, concluded exchange.
        start = _run_start(messages)
        scoped = messages[start:]
        # The system prompt is deliberately excluded: it is a reference document,
        # not something anyone said.
        mine = [str(getattr(m, "content", "") or "") for m in scoped
                if type(m).__name__ == "AIMessage"]
        theirs = [str(getattr(m, "content", "") or "") for m in scoped
                  if type(m).__name__ == "HumanMessage"]
        task = theirs[0] if theirs else ""
        feedback = [t for t in theirs[1:] if "was not approved" in t]
        answers = [t for t in theirs[1:] if "The user answered" in t]

        # A /diagnose investigation. Its prompt carries the facts already, so the
        # answer is written from those rather than invented.
        # Matched on the prompt's real opening line. It used to look for
        # "Diagnose the cause", which _why_prompt never wrote, so this branch
        # was dead and dev mode answered a diagnosis with a generic reply.
        if "needs diagnosing" in task:
            return AIMessage(content=f"<solution>\n{_diagnosis(task)}\n</solution>")

        # What the person has actually typed, as opposed to what the graph fed
        # back to itself. One conversation now spans many turns, so the request
        # is the accumulation of their own lines -- and a turn that is just talk
        # is answered with talk, never with a pipeline nobody asked for.
        spoken = [_typed(t) for t in theirs
                  if not t.lstrip().startswith("<observation>")
                  and "was not approved" not in t]
        if spoken and not _wants_a_run(" ".join(spoken), spoken[-1]):
            return AIMessage(content=f"<solution>{_chat(spoken[-1])}</solution>")

        generations = sum(1 for m in mine if "genpipes" in m and "-g" in m)
        submissions = sum(1 for m in mine if "bash cmd.sh" in m)
        asked = sum(1 for m in mine if "ask(" in m)

        # Rejected: regenerate, honouring whatever change was asked for. The
        # `feedback and` guard matters: without it a first turn (no feedback, no
        # generations) satisfies 0 >= 0 and the stand-in opens the conversation
        # by announcing it is "regenerating with that change".
        if feedback and len(feedback) >= generations:
            return AIMessage(content=(
                "Understood -- regenerating with that change.\n"
                + _gen_block(task, feedback, answers)))

        if generations == 0:
            question = _next_question(theirs) if asked < 2 else None
            if question:
                return AIMessage(content=(
                    "One thing I need before I can build this.\n" + question))
            return AIMessage(content=(
                "I'll generate the pipeline script first, so you can see the "
                "commands before anything is submitted.\n"
                + _gen_block(task, feedback, answers)))

        if submissions < generations:
            return AIMessage(content=(
                "The script generated cleanly. Submitting it now.\n"
                "<execute>\n#!BASH\nbash cmd.sh\n</execute>"))

        return AIMessage(content=(
            "<solution>The pipeline was submitted. Use /check to follow its "
            "progress, /jobs to see the individual Slurm jobs, and /diagnose if "
            "anything fails.</solution>"))


def _typed(message):
    """Only the part of a message the person actually typed.

    intake.brief appends what the request already states and what is lying around
    in the working directory. Useful to a model, ruinous here: a brief that
    mentions "possible readset: readset.tsv" would make "hi" read as a request to
    run something. It marks where its own text starts (intake.CONTEXT_MARK).
    """
    from . import intake as _intake
    return _intake.spoken(message)


# A request to put work on the cluster, as opposed to a question about it. Verbs
# only, plus the pipeline names -- naming a pipeline is itself an instruction
# ("rnaseq stringtie on my readset"), whereas nothing else anyone types is.
_RUN_VERBS = re.compile(
    r"\b(run|runs|launch|submit|resubmit|rerun|generate|process|analyse|analyze)\b",
    re.I)


# A question about GenPipes, as opposed to an instruction to use it.
_QUESTION = re.compile(
    r"^\s*(?:what|how|which|why|when|where|who|is|are|does|do|did|can|could|"
    r"should|would|tell me|explain|remind me|difference)\b", re.I)


def _run_start(messages):
    """Index into `messages` where the CURRENT run began, or 0.

    The most recent HumanMessage that wants a run all by itself -- judged the
    same way _wants_a_run judges a single line, said=latest=that line's own
    text. Panel answers are never candidates: the graph always wraps them as
    `<observation>...</observation>` (see agent.ask_user), never as a bare
    HumanMessage, so a typed "dnaseq" answered INTO a pipeline choice panel
    cannot be mistaken for a fresh "run dnaseq" request and reset the anchor
    mid-run. Rejection feedback is excluded for the same reason feedback is
    excluded elsewhere in this class: "was not approved" is a continuation of
    the run being revised, not a new one starting.

    Returns 0 -- the start of the whole thread -- when nothing anchors,
    which is the previous behaviour: a normal single-run conversation is
    unaffected by this function existing at all.
    """
    start = 0
    for i, m in enumerate(messages):
        if type(m).__name__ != "HumanMessage":
            continue
        content = str(getattr(m, "content", "") or "")
        if content.lstrip().startswith("<observation>") or "was not approved" in content:
            continue
        line = _typed(content)
        if _wants_a_run(line, line):
            start = i
    return start


def _wants_a_run(said, latest):
    """Has the person asked for work to be done, or are they talking?

    Dev mode needs this because the real model now has it in its system prompt
    (see agent.TALK_PROTOCOL): talk is the default, and building a
    pipeline is what you do when you were asked to. A stand-in that generated a
    submission in reply to "hi" would be evidence about nothing except itself.

    `said` is everything they have said, `latest` only the last line, and the two
    are read differently on purpose. A run verb anywhere means a run was asked for
    at some point and the conversation is still about it. But a pipeline NAME is
    only an instruction when the sentence around it is one -- "what does
    rnaseq_light do?" names a pipeline and orders nothing, and answering it with a
    submission is precisely the behaviour this stand-in exists to catch.
    """
    if _RUN_VERBS.search(said):
        return True
    stripped = (latest or "").strip()
    if _QUESTION.match(stripped) or stripped.endswith("?"):
        return False
    from . import intake as _intake
    return bool(_intake.find_pipeline(said))


def _chat(latest):
    """A plain conversational reply -- dev mode's stand-in for just talking.

    Deliberately says what it is. The scripted model has no knowledge to answer
    a real GenPipes question with, and inventing a confident-sounding paragraph
    here would make dev mode a worse test than no test: someone would read it and
    believe the pipeline documentation had been consulted.
    """
    text = latest.strip()
    low = text.lower()
    if re.match(r"^(hi|hey|hello|yo|salut|bonjour|good (morning|afternoon))\b", low):
        return ("Hi. I'm your GenPipes assistant. Ask me anything about the "
                "pipelines, or tell me what you want to run -- something like "
                "\"run rnaseq stringtie steps 1-5 on my readset\" -- and I'll "
                "build the command and show it to you before anything is "
                "submitted.")
    if low.startswith(("thanks", "thank", "merci", "ok", "cool", "nice")):
        return "Any time. Tell me when you want to run something."
    return ("(dev mode: the scripted stand-in can't answer GenPipes questions -- "
            "the real model does that.) Ask me to run a pipeline and I'll build "
            "the command, hold it at the gate, and let you approve it.")


def _diagnosis(prompt):
    """An explanation written from the facts triage put in the prompt.

    Answered in diagnosis.SHAPE, the same contract a real model is given, so
    dev mode exercises the parser and the structured renderer rather than only
    their prose fallback. A stand-in that answered in a shape the real one is
    forbidden from using would be evidence about nothing.
    """
    step = "the failing step"
    for line in prompt.splitlines():
        if line.startswith("--- step "):
            step = line.split()[2].rstrip(":")
            break
    # The original -s range, quoted back. The relaunch rule is that the FULL
    # range is resubmitted, never a narrowed one -- see diagnosis.RELAUNCH_RULE.
    steps = "the full original range"
    for line in prompt.splitlines():
        if line.strip().startswith("steps:"):
            steps = line.split(":", 1)[1].strip()
            break

    if "OutOfMemory" in prompt or "oom_kill" in prompt:
        return _shaped(
            manner=f"OUT_OF_MEMORY — {step} was killed by the cgroup limit.",
            cause=(f"{step} ran out of memory. The log ends in a Java heap "
                   f"OutOfMemoryError and Slurm recorded an oom_kill, with peak "
                   f"RSS at the cgroup limit."),
            evidence=["the .o log ends in java.lang.OutOfMemoryError",
                      "sacct reports MaxRSS at the ReqMem ceiling",
                      "no other step reports a resource problem"],
            fix=f"raise cluster_mem, and ram with it, for the [{step}] section",
            override=f"[{step}]\ncluster_mem = 96G\nram = %(cluster_mem)s",
            steps=steps, confidence="likely")
    if "No such file or directory" in prompt:
        return _shaped(
            manner=f"FAILED — {step} exited without producing its output.",
            cause=(f"{step} could not read an input it expected. The log names "
                   f"a FASTQ that is not on disk, so a path in the readset file "
                   f"does not resolve from the run directory."),
            evidence=["the .o log ends in No such file or directory",
                      "the path it names is not under the run directory"],
            fix="correct the path in the readset file; this is not a resource "
                "problem and no ini change will help",
            override="", steps=steps, confidence="certain")
    return _shaped(
        manner=f"{step} did not complete.",
        cause="The log does not name a cause on its own.",
        evidence=["the .o log ends without an error message"],
        fix="check the step's resources and its inputs; the logs do not "
            "support a specific value",
        override="", steps=steps, confidence="unclear")


def _shaped(manner, cause, evidence, fix, override, steps, confidence):
    """The seven headings diagnosis.parse() reads back."""
    lines = [f"MANNER: {manner}", f"CAUSE: {cause}", "EVIDENCE:"]
    lines += [f"- {e}" for e in evidence]
    lines.append(f"FIX: {fix}")
    if override:
        lines.append("OVERRIDE:")
        lines += override.splitlines()
    lines += [f"RELAUNCH: {steps}", f"CONFIDENCE: {confidence}"]
    return "\n".join(lines)


def _next_question(said, limit=2):
    """The ask() block a competent model would emit next, or None.

    Driven by slots.gaps() rather than by a script of its own, which is the
    only way dev mode can be evidence about the real thing: the stand-in asks
    when a real model with the same tables would have to ask, and stays quiet
    when it would not. An rnaseq request gets no protocol question because
    rnaseq has a documented default; a dnaseq one gets all seven options.

    Everything the user has said is read, answers included, so a question
    already answered is not asked twice -- the same accumulation a real model
    does by reading its own history.

    Each message is stripped of brief()'s appended context ON ITS OWN before
    they are joined. Joining first and stripping once cuts at the request's mark
    and takes every answer with it, so the slot never fills, and the same
    question is asked until the ask budget runs out -- which is precisely what
    it did: two identical readset panels, then a gate with no readset in it.
    """
    from . import intake as _intake
    from . import slots as _slots

    stated = _intake.read(_spoken_join(said))
    # project_dir is intake's, not slots.gaps()' -- gaps() has never asked
    # about a directory, only about the five file/pipeline slots.
    gaps = _slots.gaps(**{k: v for k, v in stated.items() if k != "project_dir"})
    if not gaps:
        return None
    gap = gaps[0]
    args = [f'slot="{gap.slot}"']
    if stated.get("pipeline"):
        args.append(f'pipeline="{stated["pipeline"]}"')
    if stated.get("protocol"):
        args.append(f'protocol="{stated["protocol"]}"')
    return f"<execute>\nask({', '.join(args)})\n</execute>"


def _gen_block(task, feedback=(), answers=()):
    """A genpipes generation command for whatever was asked for.

    Built to satisfy the stub's validation, so dev mode exercises the success
    path. A `-s` range mentioned in the request is honoured, and the most recent
    mention wins -- which is what makes a rejection like "use steps 6-12
    instead" visibly change the command in the approval box.

    Answers to the agent's own questions are read alongside the request, so a
    protocol chosen in a panel reaches the command line and shows up in the
    approval box. A panel whose answer changed nothing would be theatre.
    """
    import re as _re

    text = _spoken_join([task, *feedback, *answers]).lower()

    # Longest name first, so rnaseq_denovo_assembly is not read as rnaseq, and
    # on a word boundary so it is a request rather than an incidental mention.
    pipeline = "rnaseq"
    for candidate in sorted(PIPELINES, key=len, reverse=True):
        if _re.search(rf"\b{candidate}\b", text):
            pipeline = candidate
            break

    protocols = PIPELINES.get(pipeline) or []
    protocol = next((p for p in protocols if p in text),
                    protocols[0] if protocols else None)

    steps = "1-5"
    ranges = _re.findall(r"steps?\s+(\d+\s*-\s*\d+)", text)
    if ranges:
        steps = ranges[-1].replace(" ", "")

    # A file named anywhere in the request or in an answer reaches the command
    # line, so that choosing one in a panel visibly changes what gets approved.
    # The stub validates that it exists, which is the check that would catch a
    # panel handing back something the run cannot use.
    # Each message stripped of brief()'s appended context on its own, then
    # joined -- see intake.spoken. Joining first and stripping once cuts at the
    # request's mark and throws the answers away with the context.
    files = _intake_files(_spoken_join([task, *feedback, *answers]))

    # The -c stack, first on the line and never omitted. See build() for why
    # a generation without one is not a command GenPipes would accept.
    inis = ("$GENPIPES_INIS/" + pipeline + ".base.ini "
            "$GENPIPES_INIS/common_ini/rorqual.ini")
    flag = f"-t {protocol} " if protocol else ""
    readset = f"-r {files['readset']} " if files.get("readset") else ""
    design = f"-d {files['design']} " if files.get("design") else ""
    pairs = f"-p {files['pairs']} " if files.get("pairs") else ""
    return ("<execute>\n#!BASH\n"
            f"module load {GENPIPES_MODULE} && genpipes {pipeline} "
            f"-c {inis} "
            f"{flag}{readset}{design}{pairs}-s {steps} -g cmd.sh\n"
            "</execute>")


def _intake_files(text):
    """intake.find_files, imported lazily to keep this module's import cheap."""
    from . import intake as _intake
    return _intake.find_files(text)


def _spoken_join(parts):
    """Several messages, each stripped of its appended context, then joined."""
    from . import intake as _intake
    return " ".join(_intake.spoken(p) for p in parts)


# Imported lazily by the module that needs it, to keep this file stdlib-only at
# import time -- fakecluster is used by a CI test that must not need langchain.
GENPIPES_MODULE = "mugqic/genpipes/6.1.1"


def destroy(root):
    shutil.rmtree(root, ignore_errors=True)


@contextlib.contextmanager
def session(state="happy", root=None):
    """Run a block with the fake cluster active in THIS process, then undo it.

    In-process rather than "pass env= to the subprocess" because the code under
    test does its own shelling out -- runs.query_states calls sacct with no env
    argument, inheriting os.environ. A fake that only exists in an env dict the
    test holds is therefore invisible to exactly the functions being tested.

    The environment is snapshotted and restored wholesale, so a suite can move
    between states without one leaking into the next.
    """
    saved = dict(os.environ)
    root = root or tempfile.mkdtemp(prefix="genpipe_fake_")
    try:
        yield root, activate(state, root)
    finally:
        os.environ.clear()
        os.environ.update(saved)
        destroy(root)
