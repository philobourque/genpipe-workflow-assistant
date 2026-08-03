"""Runs and jobs: the two things this tool keeps track of, and the difference.

  A RUN is one GenPipes invocation -- the thing you named, the command that was
  approved at the gate, and the cmd.sh GenPipes generated from it. It is a unit
  of intent. You approve a run, you cancel a run, you ask why a run failed.

  A JOB is one Slurm job inside that run. GenPipes turns a single run into
  dozens or hundreds of them, one per step per sample, wired together by
  dependencies. A job is a unit of execution. It has a Slurm id, a state, and
  its own log file on disk.

Everything confusing about monitoring a pipeline comes from conflating the two.
"Did it work?" is a question about a run; "what broke?" is only ever answerable
about a job. So this module keeps them as separate types with separate stores:

  registry (runs.jsonl)  -- durable, ours, one record per run. Survives
                            everything, including the cluster losing the run's
                            artifacts. Written by us, never by GenPipes.
  jobs (queried live)    -- never stored as truth. Read from the run's job_list
                            file and from Slurm, on demand. The scheduler is the
                            only authority on a job's state, so we do not cache
                            it except as a convenience snapshot for /list.

A run's lifecycle, and why "held" exists
----------------------------------------
    held  ->  submitted  ->  gone

A run enters the registry at the GATE, not at submission -- status "held". That
is deliberate and it is the fix for the tool's worst failure mode: the gate
pauses a run, you close the terminal, and the name you needed in order to
approve it existed nowhere but your memory. A held record makes a pending
decision survive a restart, which is the entire promise of a durable gate.

"gone" means the run's job_list file is no longer on disk (a scratch purge,
manual cleanup). Records are never deleted -- a gone run drops out of /list but
stays in /history forever, because "what did I run in June?" is a real question
and the cluster is not obliged to remember.

Stdlib only, no biomni
----------------------
Like gate.py, this module imports nothing heavy, so the registry and the
job parsing are testable in CI in a couple of seconds. genpipe/agent.py holds
the graph; this holds the bookkeeping; neither needs the other to be tested.
"""
import datetime
import glob
import json
import os
import re
import subprocess

# ---------------------------------------------------------------------------
# Job states. Slurm's vocabulary, not ours -- these strings are what sacct
# prints. BAD is the set that means a human is needed; the rest are either
# fine or not finished yet.
# ---------------------------------------------------------------------------
BAD_STATES = {"FAILED", "TIMEOUT", "CANCELLED", "NODE_FAIL",
              "OUT_OF_MEMORY", "PREEMPTED", "BOOT_FAIL", "DEADLINE"}

# Of those, the states that mean this job itself broke. CANCELLED is excluded on
# purpose: in a GenPipes DAG, one failing step cancels everything downstream of
# it, so a run with three real failures reports a dozen cancellations. Those jobs
# did not break -- they never ran. The distinction matters twice over: it stops
# the interface reporting "12 failed" when three things went wrong, and it stops
# triage handing a diagnosing model a cancelled job's log, which by definition
# contains no explanation of anything.
BROKE_STATES = BAD_STATES - {"CANCELLED"}
ACTIVE_STATES = {"RUNNING", "PENDING", "CONFIGURING", "COMPLETING",
                 "REQUEUED", "RESIZING", "SUSPENDED"}

# Run statuses.
HELD = "held"
SUBMITTED = "submitted"
GONE = "gone"
# A held run the user gave up on. Terminal, and the whole reason it exists: with
# only held/submitted, a run you have mentally dropped keeps appearing in the
# startup pending list and in /list forever, because there was no way to say no.
# Nothing was submitted; the record and the reason stay in /history.
ABANDONED = "abandoned"

# GenPipes version whose `tools log_report` we call. Same pin as genpipes.md.
GENPIPES_MODULE = "mugqic/genpipes/6.1.1"

# sacct is happy with a lot of job ids, but a command line is not infinite.
# Chunked so a 900-job run doesn't produce an unrunnable command.
_SACCT_CHUNK = 300


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


# ===========================================================================
#  Runs: the durable registry.
# ===========================================================================

class Registry:
    """One JSON record per run, in runs.jsonl, appended and updated in place.

    Records are matched by name, and the LAST record for a name wins. That is
    what makes reusing a name recoverable rather than destructive: an older
    record with the same name is shadowed, not overwritten, and still visible in
    /history. Callers that want to avoid the shadowing entirely use
    unique_name() to pick a fresh one up front.
    """

    def __init__(self, workdir):
        self.workdir = workdir
        self.path = os.path.join(workdir, "runs.jsonl")
        # When this user last had the app open. Its own file rather than a
        # record in runs.jsonl, because it is not a run and the store's whole
        # contract is "every line is a run, last one for a name wins".
        self.seen_path = os.path.join(workdir, "last_seen")

    # -- storage ---------------------------------------------------------- #

    def load(self):
        """Every record, oldest first, each normalised to the current shape.

        A line that isn't valid JSON is skipped rather than fatal: a truncated
        final line (a crash mid-write, a full disk) should cost you that one
        record, not the ability to list any run at all.
        """
        if not os.path.exists(self.path):
            return []
        records = []
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(_normalise(json.loads(line)))
                except (ValueError, TypeError):
                    continue
        return records

    def save(self, records):
        # Write to a temp file and rename over the original so a crash mid-write
        # never leaves runs.jsonl half-written.
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        os.replace(tmp, self.path)

    def seen_at(self):
        """When the app was last open, or '' the first time. Never raises.

        A missing or unreadable file means "no previous session", which is the
        right answer and is also what the very first launch genuinely is. This
        is decoration on a startup line -- it must never be the reason the app
        fails to start.
        """
        try:
            with open(self.seen_path) as handle:
                return handle.read().strip()
        except OSError:
            return ""

    def mark_seen(self):
        """Record that this session happened. Best effort, like seen_at()."""
        try:
            with open(self.seen_path, "w") as handle:
                handle.write(_now())
        except OSError:
            pass

    def unseen(self):
        """What you do not yet know the answer to, as {what: [records]}.

        NOT "what changed since a timestamp", which is what this was first
        written as and which turned out to report the wrong thing. Everything
        the registry records offline is something the person DID -- they held
        it, they submitted it -- so a list of runs submitted since the last
        launch is a list of things they already watched happen. It is a diff,
        and it is not news.

        What is genuinely unseen is an OUTCOME. A run submitted on Friday and
        left overnight has a result nobody has looked at, and that is knowable
        offline in exactly two forms:

            failed      a previous /check saw failing jobs and cached the
                        verdict. Stale by definition -- the caller says so --
                        but it is the single thing most likely to have been
                        closed and forgotten.
            unfinished  submitted, and either never checked or last checked
                        while still running. The answer needs the scheduler,
                        so what this earns is a prompt to go and ask, not a
                        claim about how it went.

        Asking Slurm here instead would cost a `module load` plus an sacct per
        run before the first prompt appears. A slow startup is worse than a
        line that says less.
        """
        out = {"failed": [], "unfinished": []}
        for record in self.live():
            if record["status"] != SUBMITTED:
                continue
            seen = (record.get("last_check") or {}).get("verdict", "")
            if NEEDS_ATTENTION in seen:
                out["failed"].append(record)
            elif seen != COMPLETE:
                out["unfinished"].append(record)
        return out

    # -- reads ------------------------------------------------------------ #

    def get(self, name):
        """The current record for a name, or None. Last match wins."""
        found = None
        for r in self.load():
            if r["name"] == str(name):
                found = r
        return found

    def all(self, prune=True):
        """Every record, newest first. This is /history."""
        records = self.load()
        if prune and self._prune(records):
            self.save(records)
        return sorted(records, key=lambda r: r.get("submitted_at") or "",
                      reverse=True)

    def live(self):
        """Runs still worth acting on: submitted and not purged, plus anything
        held. This is /list.

        Held runs belong here even though they have no job list and nothing on
        the scheduler -- a run waiting for your approval is the single most
        actionable thing the tool can be holding, so it cannot be the one thing
        /list leaves out.
        """
        records = self.load()
        if self._prune(records):
            self.save(records)
        current = {}
        for r in records:
            current[r["name"]] = r          # last record per name wins
        return [r for r in current.values()
                if r["status"] not in (GONE, ABANDONED)
                and not r.get("hidden")]

    def held(self):
        """Runs paused at the gate, waiting for a decision."""
        return [r for r in self.live() if r["status"] == HELD]

    def held_for_thread(self, thread_id):
        """The run this conversation already has waiting at the gate, if any.

        A conversation is one thread and can produce many runs, so the thread is
        not the run's identity -- but while a run is held, its thread is parked
        on that interrupt and cannot start another. This is what lets the gate
        recognise "the same run, rethought after a rejection" and update it in
        place instead of minting a second name for the same pending decision.
        """
        if thread_id is None:
            return None
        for r in self.held():
            if r["thread_id"] == str(thread_id):
                return r
        return None

    def unique_name(self, wanted):
        """`wanted` if it's free, else the first `wanted-2`, `wanted-3`, ... that is.

        A name has to identify exactly one run, because it is what /approve,
        /check and /diagnose are given. One conversation routinely produces several
        runs of the same pipeline, so the obvious derived name collides on the
        second one; this is what keeps them apart without asking anybody.
        """
        taken = {r["name"] for r in self.load()}
        if wanted not in taken:
            return wanted
        n = 2
        while f"{wanted}-{n}" in taken:
            n += 1
        return f"{wanted}-{n}"

    # -- writes ----------------------------------------------------------- #

    def hold(self, name, thread_id, proposal, workdir):
        """Record a run stopped at the gate. Idempotent per name: re-reaching
        the gate on the same run (after a rejection and a rethink) updates the
        proposal in place rather than stacking near-identical records.
        """
        existing = self.get(name)
        if existing and existing["status"] == HELD:
            return self.update(name, proposal=proposal, workdir=workdir,
                               held_at=_now())
        return self._append({
            "name": str(name),
            "thread_id": str(thread_id) if thread_id is not None else None,
            "status": HELD,
            "job_list": None,
            "workdir": workdir,
            "proposal": proposal,
            "submitted_at": None,
            "held_at": _now(),
            "source": "agent",
            "gone": False,
        })

    def mark_submitted(self, name, job_list, workdir=None, proposal=None,
                       thread_id=None, source="agent"):
        """Promote a held run to submitted, or record a submission outright.

        job_list may be None: a GenPipes submission where every step is already
        up to date creates no jobs and writes no list. That is a real, successful
        outcome, and the run should still stop showing as awaiting approval.
        """
        fields = {"status": SUBMITTED, "job_list": job_list,
                  "submitted_at": _now()}
        if workdir:
            fields["workdir"] = workdir
        if proposal:
            fields["proposal"] = proposal
        if self.get(name) is not None:
            return self.update(name, **fields)
        base = {
            "name": str(name),
            "thread_id": str(thread_id) if thread_id is not None else None,
            "held_at": None,
            "source": source,
            "gone": False,
            "workdir": workdir,
            "proposal": proposal,
        }
        base.update(fields)
        return self._append(base)

    def track(self, name, job_list):
        """Register a run launched outside the agent -- no thread, no gate, no
        conversation -- so it can be checked and analysed by name like any other."""
        return self.mark_submitted(name, job_list,
                                   workdir=os.path.dirname(os.path.dirname(job_list)),
                                   source="manual")

    def adopt(self, name, found):
        """Register a run that /scan discovered on disk.

        Same destination as track(), with two differences worth keeping. The
        pipeline and protocol read off the job-list filename are stored as a
        proposal, so /list can say what a discovered run is without a scheduler
        call. And every job list under the same run directory is kept as
        `attempts`: re-running a pipeline in one place is several submissions of
        one logical run, and the newest is the one worth checking.
        """
        record = self.mark_submitted(
            name, found["job_list"], workdir=found["workdir"],
            proposal={"command": "", "slots": {"pipeline": found.get("pipeline"),
                                               "protocol": found.get("protocol")}},
            source="scan")
        return self.update(name, attempts=found.get("attempts") or [found["job_list"]],
                           discovered_at=_now())

    def abandon(self, name, reason=None):
        """Retire a held run. Nothing submitted, nothing regenerated.

        The terminal end of /reject. It leaves held(), so it stops appearing in
        /list and in the startup pending line -- which is the entire point, and
        is achieved by the status change alone rather than by a deletion. The
        record and the reason stay in /history, because "why did I not run
        that?" is a question people ask months later.

        Refused on a submitted run: there the name is tied to a job list and
        real jobs, and abandoning it would hide a live thing.
        """
        record = self.get(name)
        if record is None:
            return None
        if record["status"] != HELD:
            return record
        fields = {"status": ABANDONED, "abandoned_at": _now()}
        if reason:
            fields["abandoned_because"] = reason
        record = self.update(name, **fields)
        if reason:
            self.add_note(name, f"abandoned: {reason}")
        return self.get(name)

    def rename(self, name, wanted):
        """Give a held run a different name. Returns the name it ended up with.

        A rename changes no flags and needs no regeneration -- it is a registry
        write and nothing else, which is what makes it the one row at the gate
        that costs no model call.

        Only a held run may be renamed. After submission the name is the handle
        for a job list and for jobs already on the scheduler, and moving it
        would strand them.

        Every record for the old name is rewritten, not just the current one.
        The store is append-only and get() takes the last match, so leaving the
        older records behind would leave a run findable under a name it no
        longer has -- a ghost that /approve could reach and /list could not.
        """
        record = self.get(name)
        if record is None or record["status"] != HELD:
            return None
        settled = self.unique_name(wanted)
        if settled == name:
            return name
        records = self.load()
        for r in records:
            if r["name"] == str(name):
                r["name"] = settled
                r["renamed_from"] = str(name)
        self.save(records)
        return settled

    def hide(self, name, hidden=True):
        """Drop a run out of /list without losing it.

        What /sort's discard does. Deliberately not a deletion: a registry that
        can forget is a registry you cannot trust to answer "what did I run in
        June?", and the reason to clear a row is nearly always that it is old
        rather than that it is wrong.
        """
        return self.update(name, hidden=bool(hidden))

    def remember_reasons(self, name, reasons):
        """Persist the pending reasons observed while the run was still queued.

        The one exception to "never cache scheduler state". Job states are
        permanent in sacct and must always be re-read; pending REASONS are
        perishable -- sacct never records them at all, and squeue drops a job
        the moment it leaves the queue. So once a run dies, "these 28 were
        waiting on a dependency that could never be satisfied" is unrecoverable
        unless it was written down while it was still true.
        """
        if not reasons:
            return self.get(name)
        return self.update(name, last_reasons={"at": _now(), "reasons": reasons})

    def update(self, name, **fields):
        """Merge fields into the last record for a name. Returns it, or None."""
        records = self.load()
        target = None
        for r in records:
            if r["name"] == str(name):
                target = r
        if target is None:
            return None
        target.update(fields)
        self.save(records)
        return target

    def remember_check(self, name, counts, total, verdict):
        """Cache the result of a status check on the run.

        So /list can show where each run stood without a `module load` plus a
        log_report per row -- which, at a couple of seconds each, is the
        difference between a listing and a wait. Explicitly a snapshot with a
        timestamp, never presented as live truth.
        """
        return self.update(name, last_check={
            "at": _now(), "counts": counts, "total": total, "verdict": verdict})

    def add_note(self, name, text):
        """Attach a finding to a run -- what /diagnose concluded, in one line.

        This is what makes the registry compound in value: six weeks later
        /history can say `patient-42 . failed . OOM in picard_mark_duplicates`
        instead of just naming a run you no longer remember.
        """
        record = self.get(name)
        if record is None:
            return None
        notes = list(record.get("notes") or [])
        notes.append({"at": _now(), "text": text})
        return self.update(name, notes=notes)

    # -- internals -------------------------------------------------------- #

    def _append(self, record):
        records = self.load()
        records.append(record)
        self.save(records)
        return record

    def _prune(self, records):
        """Mark submitted runs whose job_list has vanished as gone, in place.

        Three things are deliberately NOT pruned.

        A held run: it has no job list yet, and never having submitted is not the
        same as having been cleaned up.

        A record already gone: once the file is gone it stays gone, so a
        transient filesystem hiccup can't flicker it back to live.

        A run that never had a job list at all: that is a submission where every
        step was already up to date, which is a real and successful outcome.
        Calling it "gone" would claim its artifacts were purged, which is not
        what happened -- there were never any. It keeps its own status and says
        so in /list.

        Returns True if anything changed, so the caller knows to save.
        """
        changed = False
        for r in records:
            if r["status"] != SUBMITTED or not r["job_list"]:
                continue
            if not os.path.exists(r["job_list"]):
                r["status"] = GONE
                r["gone"] = True
                r["gone_at"] = _now()
                changed = True
        return changed


def _normalise(record):
    """Fill in fields a record predates, so an old runs.jsonl keeps working.

    The registry has been live on a cluster since before status/workdir/proposal
    existed. Those records are real history and must not be discarded, so shape
    is reconciled on read rather than by a migration: a record with no status is
    a submission (that was the only kind that got recorded), and its `gone` flag
    is authoritative.
    """
    record.setdefault("name", "?")
    record.setdefault("thread_id", None)
    record.setdefault("job_list", None)
    record.setdefault("source", "agent")
    record.setdefault("submitted_at", None)
    record.setdefault("held_at", None)
    record.setdefault("workdir", None)
    record.setdefault("proposal", None)
    record.setdefault("gone", False)
    record.setdefault("hidden", False)
    if "status" not in record:
        record["status"] = GONE if record["gone"] else SUBMITTED
    # Keep the two representations agreeing, whichever way the record arrived.
    if record["status"] == GONE:
        record["gone"] = True
    return record


# ===========================================================================
#  Jobs: read from the run's job_list, then from Slurm. Never stored as truth.
# ===========================================================================

class Job:
    """One Slurm job inside a run.

    `state` is None until Slurm has been asked. That distinction matters: an
    unknown state is not a healthy one, and nothing here should let the two
    render the same way.
    """

    __slots__ = ("job_id", "name", "step", "log", "state", "elapsed",
                 "maxrss", "exit_code", "start", "timelimit", "reason")

    def __init__(self, job_id=None, name="", log=None):
        self.job_id = job_id
        self.name = name
        # GenPipes names jobs `<step>.<sample>` -- the step is the part worth
        # grouping by, since a failure is nearly always a step failing across
        # samples rather than one unlucky sample.
        self.step = name.split(".")[0] if name else ""
        self.log = log
        self.state = None
        self.elapsed = None
        self.maxrss = None
        self.exit_code = None
        self.start = None
        self.timelimit = None
        self.reason = None

    @property
    def failed(self):
        return self.state in BAD_STATES

    @property
    def active(self):
        return self.state in ACTIVE_STATES

    def __repr__(self):
        return f"<Job {self.job_id} {self.name} {self.state}>"


def output_dir_of(script):
    """The OUTPUT_DIR a generated GenPipes script declares, or None.

    Every script GenPipes writes opens with the three lines that decide where
    everything lands:

        OUTPUT_DIR=/scratch/me/project
        JOB_OUTPUT_DIR=$OUTPUT_DIR/job_output
        JOB_LIST=$JOB_OUTPUT_DIR/DnaSeq.somatic_fastpass.job_list.$TIMESTAMP

    So the script is the authority on where its own job list goes -- more than
    the agent's cwd, which is only where the submission happened to be typed.
    """
    if not script or not os.path.exists(script):
        return None
    try:
        with open(script) as f:
            for line in f:
                m = re.match(r"\s*OUTPUT_DIR=(\S+)", line)
                if m:
                    return m.group(1).strip("\"'")
                # It is declared in the header. Give up at the first job rather
                # than read 60,000 lines looking for a line that is not coming.
                if line.startswith("#SBATCH"):
                    break
    except OSError:
        return None
    return None


def find_job_list(workdir, since, output_dir=None, script=None):
    """The job list a submission just wrote, or None if it wrote none.

    Where GenPipes puts it depends on the run's OUTPUT_DIR, which is frequently
    NOT the directory the submission was launched from -- `-o some_dir` is
    ordinary usage, and it puts the list in some_dir/job_output/. Looking only in
    the cwd is what made a 46-job run report as "created no jobs" on 2026-07-27:
    the list existed, one directory down, and nothing looked there.

    Four places, most authoritative first:

        1. OUTPUT_DIR declared by the script that was submitted
        2. the -o directory from the generation command
        3. the directory the submission ran in
        4. one level below it, for an -o nobody recorded

    `since` is what keeps this honest. A wider search is a wider chance of
    adopting a PREVIOUS run's list and reporting another run's jobs under this
    name, so only a file written after the submission started can match. A
    submission that genuinely created no jobs writes no file, and correctly
    stays None rather than picking up the newest thing lying around.

    Stdlib only, and here rather than in genpipe/agent.py, so the case that
    broke is covered by a test that runs on every push.
    """
    roots = []
    for d in (output_dir_of(script), output_dir):
        if d:
            roots.append(d if os.path.isabs(d) else os.path.join(workdir, d))
    roots.append(workdir)

    patterns = [os.path.join(r, "job_output", "*job_list*") for r in roots]
    # One level down, not `**`: workdir also holds raw_reads/, alignment/ and
    # everything else a pipeline drops, and walking all of that to find a file
    # we already have three better guesses for costs seconds on Lustre.
    patterns.append(os.path.join(workdir, "*", "job_output", "*job_list*"))

    found = []
    for pattern in patterns:
        for path in glob.glob(pattern):
            try:
                if os.path.getmtime(path) >= since:
                    found.append(path)
            except OSError:
                continue
    return max(found, key=os.path.getmtime) if found else None


def parse_job_list(path):
    """Read a GenPipes job_list file into Job objects.

    The manifest is a stable four-column, tab-separated, POSITIONAL format, and
    GenPipes' own `get_report` hardcodes those positions:

        job_id <TAB> job_name <TAB> dependencies <TAB> output_file_relpath

    Column 3 is a colon-joined list of the job ids this one waits on, empty for
    a job with no dependencies. That column is why the old "identify each field
    by what it looks like" heuristic had to go: for a fan-in job -- a multiqc
    that waits on thirteen upstream jobs -- the dependency string is 129
    characters and beats the real name at "longest field that is neither the id
    nor the log". Two of the 46 jobs on the first real manifest this was checked
    against parsed as a colon-joined id list instead of a name, which also
    corrupts Job.step, and therefore what triage() groups by and what /diagnose is
    told broke.

    So: positions when the line has exactly four fields, which is every line
    GenPipes writes. The old heuristic stays for any other shape, because a
    format change should cost a field rather than the feature -- it just no
    longer gets to overrule a manifest that is telling us plainly.
    """
    jobs = []
    if not path or not os.path.exists(path):
        return jobs
    with open(path) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if "\t" in line:
                fields = [x.strip() for x in line.split("\t")]
                if len(fields) == 4:
                    job_id, name, _deps, log = fields
                    jobs.append(Job(job_id=job_id or None, name=name,
                                    log=log or None))
                    continue
                fields = [x for x in fields if x]
            else:
                fields = [x for x in line.split() if x]
            if not fields:
                continue

            job_id = next((x for x in fields
                           if re.fullmatch(r"\d+(?:_\d+)?", x)), None)
            log = next((x for x in fields
                        if re.search(r"\.(o|out|log|err)\b", x) or "/" in x), None)
            rest = [x for x in fields if x != job_id and x != log]
            name = max(rest, key=len) if rest else (job_id or "?")
            jobs.append(Job(job_id=job_id, name=name, log=log))
    return jobs


# ===========================================================================
#  Discovery: finding runs that already exist on disk. What /scan is.
#
#  Read-only, deterministic, and metadata-only. It looks at job-list filenames,
#  generated command-file names, config traces and directory names -- never at a
#  FASTQ, a BAM, a VCF, a result table or the contents of a readset. The agent
#  does not need to read anybody's data in order to recognise a run, and this is
#  the module where that could most easily have stopped being true.
# ===========================================================================

# GenPipes names a job list `<Pipeline>.<protocol>.job_list.<TIMESTAMP>`, where
# the pipeline is CamelCase and the protocol is the -t value verbatim. That
# filename is the single most informative artifact a finished run leaves, and it
# is a name rather than a payload -- which is exactly why it is what we read.
_JOB_LIST = re.compile(
    r"^(?P<pipeline>[A-Za-z0-9]+)"
    r"(?:\.(?P<protocol>[A-Za-z0-9_]+))?"
    r"\.job_list\.(?P<stamp>[0-9T.\-]+)$")

# CamelCase pipeline name -> the flag value you would type. Derived rather than
# hardcoded where it can be (DnaSeq -> dnaseq), with the two-word ones that do
# not lower-case cleanly spelled out.
_PIPELINE_FROM_FILE = {
    "dnaseq": "dnaseq",
    "rnaseq": "rnaseq",
    "rnaseqlight": "rnaseq_light",
    "rnaseqdenovoassembly": "rnaseq_denovo_assembly",
    "chipseq": "chipseq",
    "methylseq": "methylseq",
    "covseq": "covseq",
    "nanoporecovseq": "nanopore_covseq",
    "ampliconseq": "ampliconseq",
    "longreaddnaseq": "longread_dnaseq",
}

# How deep to walk. A GenPipes run puts its job list at <run>/job_output/, so
# three levels below the directory you named is already generous; walking a
# whole project space on Lustre is minutes of IO for a listing nobody asked to
# wait for.
SCAN_DEPTH = 4


def discover(root, depth=SCAN_DEPTH, limit=200):
    """Every GenPipes run under `root`, as candidate registry records.

    One entry per RUN DIRECTORY, not per job list. Re-running the same pipeline
    in the same place is a second submission attempt of one logical run, and
    listing it twice would put two rows in /list that mean the same thing --
    so the attempts are collected together and the newest is the one that gets
    checked, with the rest kept as history.

    Returns [{name, pipeline, protocol, workdir, job_list, attempts, at}], newest
    first. `pipeline` and `protocol` are None rather than guessed when the
    filename does not say -- an unknown shown as unknown is worth more than a
    plausible invention, because the thing on the other end of the guess is a
    cluster.
    """
    root = os.path.abspath(os.path.expanduser(root or "."))
    if not os.path.isdir(root):
        return []

    by_dir = {}
    base_depth = root.rstrip(os.sep).count(os.sep)
    for here, dirnames, filenames in os.walk(root):
        if here.count(os.sep) - base_depth >= depth:
            dirnames[:] = []
        # Nothing in these can be a run, and raw_reads/ in particular is where
        # the data lives -- descending into it would be both slow and exactly
        # the thing this command promises not to do.
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".")
                       and d not in ("raw_reads", "trim", "alignment", "metrics",
                                     "report", "tracks", "variants", "peak_call",
                                     "methylation", "kallisto")]
        if os.path.basename(here) != "job_output":
            continue
        run_dir = os.path.dirname(here)
        for fn in filenames:
            m = _JOB_LIST.match(fn)
            if not m:
                continue
            path = os.path.join(here, fn)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            entry = by_dir.setdefault(run_dir, {"attempts": [], "workdir": run_dir})
            entry["attempts"].append({
                "job_list": path,
                "pipeline": _pipeline_of(m.group("pipeline")),
                "protocol": m.group("protocol"),
                "at": mtime,
            })
        if len(by_dir) >= limit:
            break

    found = []
    for run_dir, entry in by_dir.items():
        attempts = sorted(entry["attempts"], key=lambda a: a["at"], reverse=True)
        newest = attempts[0]
        found.append({
            "name": suggest_scan_name(run_dir, newest["pipeline"],
                                      newest["protocol"], newest["at"]),
            "pipeline": newest["pipeline"],
            "protocol": newest["protocol"],
            "workdir": run_dir,
            "job_list": newest["job_list"],
            "attempts": [a["job_list"] for a in attempts],
            "at": newest["at"],
        })
    return sorted(found, key=lambda f: f["at"], reverse=True)


def _pipeline_of(word):
    """`DnaSeq` -> `dnaseq`, or None when the name is not one we recognise."""
    if not word:
        return None
    return _PIPELINE_FROM_FILE.get(word.lower())


def suggest_scan_name(run_dir, pipeline, protocol, when=None):
    """A proposed run id for a discovered run.

    Built from the directory it lives in plus what the job list says it was,
    because the directory name is what the person who made it chose to call it
    and is therefore the part they will recognise. The date keeps repeat runs of
    the same thing in the same place apart.
    """
    stem = re.sub(r"[^a-z0-9]+", "-", os.path.basename(run_dir).lower()).strip("-")
    parts = [p for p in (stem, protocol or pipeline) if p]
    slug = "-".join(dict.fromkeys("-".join(parts).split("-")))[:40].strip("-")
    day = datetime.datetime.fromtimestamp(when or 0) if when else datetime.datetime.now()
    return f"{slug or 'run'}-{day:%m%d}"


def already_known(registry, found):
    """The existing record this discovered run duplicates, or None.

    Matched on the job list first and the run directory second. Both matter: the
    same run adopted twice under two names would put two rows in /list that
    cannot be told apart, and a /scan that silently re-added everything it found
    would make the command unusable the second time you ran it.
    """
    for record in registry.load():
        if record.get("job_list") and record["job_list"] == found["job_list"]:
            return record
        if record.get("workdir") and os.path.abspath(record["workdir"]) == found["workdir"]:
            return record
    return None


def _run(cmd, env=None):
    """Run a shell command, returning (stdout+stderr, returncode).

    Every cluster call in this module funnels through here so a missing
    scheduler behaves like an empty answer rather than an exception: on a laptop
    there is no sacct, and the right response to that is "I don't know the
    states", not a traceback in the middle of the interface.
    """
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                             executable="/bin/bash", env=env)
    except OSError as e:
        return f"{e}", 127
    return out.stdout + (("\n" + out.stderr) if out.stderr else ""), out.returncode


def query_states(job_ids):
    """Ask Slurm for the state of each job id. Returns {id: {...}}.

    --parsable2 gives pipe-separated fields with no padding and no trailing
    delimiter, which is the only sacct output shape worth parsing. Sub-steps
    (`12345.batch`, `12345.extern`) are dropped: they are accounting rows for
    the same job, and counting them would inflate every total.
    """
    states = {}
    ids = [j for j in job_ids if j]
    for i in range(0, len(ids), _SACCT_CHUNK):
        chunk = ids[i:i + _SACCT_CHUNK]
        raw, code = _run(
            "sacct --noheader --parsable2 "
            "--format=JobID,JobName,State,Elapsed,MaxRSS,ExitCode,Start,Timelimit "
            f"-j {','.join(chunk)}")
        if code != 0:
            continue
        for line in raw.splitlines():
            parts = line.split("|")
            if len(parts) < 3:
                continue
            job_id = parts[0].strip()
            if "." in job_id:          # .batch / .extern accounting rows
                continue
            # "CANCELLED by 12345" -> "CANCELLED"; the who is not our business.
            state = parts[2].strip().split()[0] if parts[2].strip() else None

            def field(i):
                return parts[i].strip() if len(parts) > i and parts[i].strip() else None

            states[job_id] = {
                "state": state,
                "elapsed": field(3),
                "maxrss": field(4),
                "exit_code": field(5),
                "start": field(6),
                "timelimit": field(7),
            }
    return states


def query_reasons(job_ids):
    """Ask the live queue why each still-queued job is still queued.

    Returns {job_id: (state, reason)} for the jobs squeue still knows about --
    which is only the pending and running ones. Everything else has left the
    queue and is simply absent, which is not an error and not a gap.

    This is the half sacct cannot answer. `sacct --format=Reason` returns None
    for every job on this cluster, verified across a whole 46-job run including
    all 43 that were cancelled. So a pending job's reason exists in exactly one
    place and stops existing the moment the job leaves the queue, and the one
    reason that matters -- DependencyNeverSatisfied, a job that will never run
    while sacct still calls it PENDING -- is unrecoverable after the fact.

    Empty dict when there is nothing to ask about, and the caller must not make
    the call at all in that case: on a finished run squeue has nothing to say
    and the interface should say so rather than imply it was consulted.
    """
    reasons = {}
    ids = [j for j in job_ids if j]
    if not ids:
        return reasons
    for i in range(0, len(ids), _SACCT_CHUNK):
        chunk = ids[i:i + _SACCT_CHUNK]
        raw, code = _run("squeue --noheader -o '%i|%T|%r' "
                         f"--jobs={','.join(chunk)}")
        if code != 0:
            continue
        for line in raw.splitlines():
            parts = line.split("|")
            if len(parts) < 3:
                continue
            reasons[parts[0].strip()] = (parts[1].strip(), parts[2].strip())
    return reasons


def scheduler_reachable():
    """Is there a scheduler to ask at all?

    Asked only when a state query came back empty, because empty has two
    meanings that must not be reported the same way:

        sacct is not there            -> we know nothing, and must say so
        sacct does not know these ids -> the ids are UNKNOWN, which is a fact

    Reporting the second as the first tells somebody their cluster is
    unreachable when it is fine; reporting the first as the second invents
    fifteen UNKNOWN jobs out of a missing binary. `--version` is the cheapest
    question sacct answers and it needs no ids.
    """
    _, code = _run("sacct --version")
    return code == 0


def jobs_for(record, with_states=True):
    """Every job in a run, with live Slurm state attached where available."""
    jobs = parse_job_list(record.get("job_list"))
    if jobs and with_states:
        states = query_states([j.job_id for j in jobs])
        for j in jobs:
            info = states.get(j.job_id or "")
            if info:
                _attach(j, info)
    return jobs


def _attach(job, info):
    """Copy one sacct row onto a Job. One place, so resolve() and jobs_for()
    cannot end up disagreeing about which fields were carried across."""
    job.state = info.get("state")
    job.elapsed = info.get("elapsed")
    job.maxrss = info.get("maxrss")
    job.exit_code = info.get("exit_code")
    job.start = info.get("start")
    job.timelimit = info.get("timelimit")


def counts(jobs):
    """{state: n} over a job list, with unknowns counted honestly as UNKNOWN."""
    tally = {}
    for j in jobs:
        key = j.state or "UNKNOWN"
        tally[key] = tally.get(key, 0) + 1
    return tally


def verdict(tally):
    """One short phrase for a whole run: what a person wants from a glance."""
    total = sum(tally.values())
    if not total:
        return "no jobs"
    bad = sum(n for s, n in tally.items() if s in BAD_STATES)
    if bad:
        return f"{bad} {NEEDS_ATTENTION}"
    active = sum(n for s, n in tally.items() if s in ACTIVE_STATES)
    if active:
        return f"{active} running"
    if tally.get("UNKNOWN"):
        return "state unknown"
    return COMPLETE


# ===========================================================================
#  resolve(): what a run is ACTUALLY doing, asked of the scheduler.
#
#  The thing this replaces, and why
#  --------------------------------
#  check() used to report a run's status from `genpipes tools log_report`. That
#  command never contacts Slurm. It infers state from files on disk, and on a
#  dead run it reports the run as alive. Measured on a real 46-job run that died
#  at 10:12 on 2026-07-27:
#
#      source        COMPLETED  RUNNING  PENDING  TIMEOUT  CANCELLED
#      log_report            1        2       43        -          -
#      sacct (truth)         1        -        -        2         43
#
#  Two mechanisms, both structural. A job with no .o file on disk is hardcoded
#  PENDING; a job whose .o has a PROLOGUE line and no EPILOGUE reads RUNNING
#  forever. The second is the dangerous one -- "2 RUNNING" is the affirmative
#  signal that says a pipeline is alive.
#
#  No amount of better file-reading fixes it. Every artifact GenPipes leaves is
#  written BY THE JOB ITSELF: the prologue needs the job to have started, the
#  epilogue needs the shell to exit normally (a SIGKILL bypasses a bash EXIT
#  trap), the .done file needs exit status 0. So "never started" and "died
#  violently" -- the two states that define a dead run -- are exactly the two
#  the filesystem is structurally incapable of recording. The record is
#  monotonic and success-only. On that run: 46 submitted, 3 ever started, 1
#  succeeded. The filesystem knew about 3.
#
#  log_report is not lying. Its vocabulary is about artifacts, not about Slurm:
#  PENDING means "no .o file yet", RUNNING means "prologue, no epilogue". The
#  defect was reading filesystem words as scheduler words.
# ===========================================================================

# What sacct says when a job is over, one way or another. Anything outside this
# and ACTIVE_STATES is treated as still-in-flight rather than assumed finished.
_TERMINAL = (BAD_STATES | {"COMPLETED", "SPECIAL_EXIT", "REVOKED"})

# The one squeue reason that means a job will never run, no matter how long you
# wait -- while sacct goes on calling it PENDING.
DOOMED_REASON = "DependencyNeverSatisfied"

# The phrase verdict() uses for a run with failed jobs, named rather than
# spelled twice: Registry.since() looks for it in a CACHED verdict to decide
# what to raise at startup, and a literal in two files is one edit away from
# a startup line that silently stops mentioning failures.
NEEDS_ATTENTION = "need attention"

# The verdict for a run with nothing left to watch. Named for the same reason:
# Registry.unseen() uses it to decide which runs still have an answer nobody
# has looked at.
COMPLETE = "complete"

# How close to its wall-clock limit a running job has to be before it is worth
# saying so. A job at 90% is the early warning for the exact thing that killed
# the run above: 00:10:01 elapsed against a 00:10:00 limit.
AT_RISK = 0.90


def _seconds(text):
    """Slurm's D-HH:MM:SS / HH:MM:SS / MM:SS as seconds, or None.

    None for UNLIMITED, Partition_Limit, INVALID and anything else non-numeric.
    A limit we cannot read is not a limit of zero, and a monitor that divides by
    it would report every job as over its budget.
    """
    text = (text or "").strip()
    if not text or not re.match(r"^[\d\-:.]+$", text):
        return None
    days = 0
    if "-" in text:
        head, _, text = text.partition("-")
        if not head.isdigit():
            return None
        days = int(head)
    parts = text.split(":")
    if not all(p.replace(".", "").isdigit() for p in parts if p):
        return None
    try:
        nums = [float(p or 0) for p in parts]
    except ValueError:
        return None
    while len(nums) < 3:
        nums.insert(0, 0)
    h, m, s = nums[-3:]
    return days * 86400 + h * 3600 + m * 60 + s


class RunStatus:
    """What resolve() found. Plain data: no rendering, no model, no opinions
    beyond the ones stated in the fields."""

    __slots__ = ("jobs", "counts", "total", "resolved", "unknown", "finished",
                 "verdict", "reasons", "at_risk", "root_cause", "source", "at",
                 "done_files", "doomed")

    def __init__(self, **kw):
        for slot in self.__slots__:
            setattr(self, slot, kw.get(slot))

    @property
    def done(self):
        return self.counts.get("COMPLETED", 0) if self.counts else 0

    @property
    def percent(self):
        return (100.0 * self.done / self.total) if self.total else 0.0

    def __repr__(self):
        return f"<RunStatus {self.verdict} {self.resolved}/{self.total}>"


def _root_cause(jobs):
    """The earliest job that broke on its own, or None.

    Cancelled jobs are never a root cause: in a GenPipes DAG one failure cancels
    everything downstream of it, so the cancellations are the casualties. The
    earliest independent failure is the one worth naming, and ordering by start
    time is what distinguishes the cause from the jobs that died after it.
    """
    broke = [j for j in jobs if j.state in BROKE_STATES]
    if not broke:
        return None
    def when(job):
        stamp = (job.start or "").strip()
        return stamp if re.match(r"^\d{4}-\d\d-\d\d", stamp) else "9999"
    first = min(broke, key=when)
    same = [j for j in broke if j.step == first.step and j.state == first.state]
    return {
        "step": first.step,
        "state": first.state,
        "count": len(same),
        "job": first.name,
        "elapsed": first.elapsed,
        "timelimit": first.timelimit,
        "maxrss": first.maxrss,
        # The failing jobs themselves, not just how many there were. `/check`
        # used to say "2 job(s) timeout" and stop, which is the count without
        # the evidence: the two jobs are a tumour and its matched normal, they
        # ran for different lengths, and which one is which is the first thing
        # anybody wants. It was reachable by then typing /jobs, and a fact that
        # needs a second command to see is a fact most people never see.
        "jobs": [{"name": j.name, "elapsed": j.elapsed, "maxrss": j.maxrss}
                 for j in sorted(same, key=lambda j: j.name or "")],
        "cancelled_after": sum(1 for j in jobs if j.state == "CANCELLED"),
    }


def resolve(record, states=None, reasons=None):
    """Everything known about a run's jobs, from the scheduler, right now.

    The order is the argument:

      1. the manifest is the DENOMINATOR. Every job ever submitted, with its id.
         Never drop one, never invent one.
      2. sacct is the SPINE. One batched query over all of them, authoritative
         and permanent -- PurgeJobAfter is NONE on Rorqual, so the accounting
         database never forgets and there is nothing here worth caching.
      3. squeue ANNOTATES, and only the jobs sacct still reports as non-terminal.
         Skipped entirely when there are none, because a finished run has nothing
         in the queue and pretending otherwise would fake a source.
      4. the verdict comes from job states. Never from artifacts on disk.

    `states` and `reasons` let /check all hand in the results of one batched
    query for many runs instead of paying a round-trip per run.

    Two rules the callers depend on:

      UNKNOWN never renders as healthy. An id sacct does not know is counted as
      UNKNOWN, and a run with any UNKNOWN is not finished, whatever else it says.

      A scheduler we could not reach is not a run with no jobs. source becomes
      "unavailable", every state stays None, and nothing is inferred from the
      filesystem to fill the hole.
    """
    jobs = parse_job_list(record.get("job_list"))
    total = len(jobs)
    ids = [j.job_id for j in jobs if j.job_id]

    if states is None:
        states = query_states(ids) if ids else {}
    # An empty answer is ambiguous, so it is the one case worth a second call:
    # no sacct at all means we know nothing, while an sacct that simply does not
    # recognise these ids means they are UNKNOWN -- a real finding about the run
    # rather than a failure to look. See scheduler_reachable().
    reachable = (not ids) or bool(states) or scheduler_reachable()

    for j in jobs:
        info = states.get(j.job_id or "")
        if info:
            _attach(j, info)

    unknown = sum(1 for j in jobs if not j.state)
    tally = counts(jobs)

    live_ids = [j.job_id for j in jobs
                if j.job_id and j.state and j.state not in _TERMINAL]
    if reasons is None:
        reasons = query_reasons(live_ids) if live_ids else {}
    for j in jobs:
        got = reasons.get(j.job_id or "")
        if got:
            j.reason = got[1]

    reason_tally = {}
    for j in jobs:
        if j.reason and j.reason not in ("None", ""):
            reason_tally[j.reason] = reason_tally.get(j.reason, 0) + 1
    doomed = reason_tally.get(DOOMED_REASON, 0)

    at_risk = []
    for j in jobs:
        if j.state != "RUNNING":
            continue
        spent, limit = _seconds(j.elapsed), _seconds(j.timelimit)
        if spent and limit and spent >= AT_RISK * limit:
            at_risk.append(j)

    if not reachable:
        source = "unavailable"
    elif live_ids and reasons:
        source = "sacct + squeue"
    else:
        source = "sacct"

    active = sum(n for s, n in tally.items() if s in ACTIVE_STATES)
    finished = reachable and unknown == 0 and active == 0 and total > 0

    # A run whose remaining work is queued behind something that already broke
    # is over, whatever sacct calls those jobs. This is the precise case the old
    # path got wrong while the run was still nominally alive.
    if doomed:
        finished = True

    if not reachable:
        phrase = "scheduler unreachable"
    elif not total:
        phrase = "no jobs"
    elif doomed:
        phrase = f"dead — {doomed} waiting on a dependency that will never come"
    else:
        phrase = verdict(tally)
        if finished and any(s in BROKE_STATES for s in tally):
            phrase = "failed, nothing still running"
        elif finished and tally.get("CANCELLED"):
            phrase = "cancelled"
        elif finished and unknown == 0:
            phrase = "complete"
        elif active:
            phrase = f"running, {active} active"

    return RunStatus(
        jobs=jobs,
        counts=tally,
        total=total,
        resolved=total - unknown,
        unknown=unknown,
        finished=finished,
        verdict=phrase,
        reasons=reason_tally,
        at_risk=at_risk,
        root_cause=_root_cause(jobs),
        source=source,
        at=datetime.datetime.now().strftime("%H:%M"),
        done_files=_done_count(record),
        doomed=doomed,
    )


def _done_count(record):
    """How many .done files the run has left behind.

    Counted separately and never folded into the state tally: it answers only
    "what would a re-run skip", which is a different question from "what did the
    scheduler do", and conflating the two is the whole mistake this module is
    correcting.
    """
    workdir = record.get("workdir")
    if not workdir:
        return None
    try:
        return len(glob.glob(os.path.join(workdir, "job_output", "**", "*.done"),
                             recursive=True))
    except OSError:
        return None


def resolve_all(records):
    """resolve() over many runs at the cost of one scheduler round-trip.

    Job ids are globally unique, so one flat {id: state} map over every live
    run's manifest can be attributed back by id -- which is what makes /check
    all cost the same whether you have two runs or twenty. Looping resolve()
    per run would be N sacct calls and N squeue calls, and on a login node that
    is the difference between a listing and a wait.

    Returns [(record, RunStatus or None)] in the order given; None for a run
    with no job list to resolve, which the caller renders as unavailable rather
    than as empty.
    """
    manifests = {}
    for r in records:
        manifests[r["name"]] = parse_job_list(r.get("job_list"))

    every_id = [j.job_id for jobs in manifests.values() for j in jobs if j.job_id]
    states = query_states(every_id) if every_id else {}

    live = [jid for jid, info in states.items()
            if info.get("state") and info["state"] not in _TERMINAL]
    reasons = query_reasons(live) if live else {}

    out = []
    for r in records:
        if r.get("status") == HELD or not r.get("job_list"):
            out.append((r, None))
            continue
        out.append((r, resolve(r, states=states, reasons=reasons)))
    return out


def resolve_log(job, record):
    """Find a job's log file on disk, returning a path or None.

    The path in the job_list is relative to the directory the run was launched
    from, which is why the run record carries `workdir` -- without it, a log
    lookup only works if you happen to still be sitting in the same place. If
    that path misses, fall back to searching job_output for a file named after
    the job, since GenPipes' own naming is more stable than its layout.
    """
    workdir = record.get("workdir") or os.getcwd()
    candidates = []
    if job.log:
        candidates += [job.log, os.path.join(workdir, job.log)]
    if job.name:
        candidates += glob.glob(os.path.join(workdir, "job_output", "**",
                                            f"{job.name}*.o"), recursive=True)
        if job.job_id:
            candidates += glob.glob(os.path.join(workdir, "job_output", "**",
                                                 f"*{job.job_id}*"), recursive=True)
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def tail(path, lines=40):
    """The last n lines of a file, or None. Errors are worth reading from the
    bottom: the traceback and the exit message are always at the end."""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, errors="replace") as f:
            return "".join(f.readlines()[-lines:])
    except OSError:
        return None


def triage(record, jobs=None, limit=5, log_lines=40):
    """Find what actually broke in a run: the failed jobs, and their logs.

    This is the deterministic pre-step that a diagnosing model should be given
    instead of a directory to browse. A GenPipes run has hundreds of .o files;
    letting a model go looking costs a great deal of context and returns a
    vague answer, while the set of jobs Slurm says failed is a fact obtainable
    in one query. So the model gets that fact and the relevant log text, and
    spends its reasoning on the cause rather than on the search.

    `limit` caps how many failures are read from disk. Failures in a pipeline
    are overwhelmingly correlated -- one bad ini, one missing input, the same
    step across forty samples -- so the first few are representative, and
    reading four hundred logs into a prompt buys nothing.
    """
    if jobs is None:
        jobs = jobs_for(record)
    failed = [j for j in jobs if j.failed]
    # Group by step so the sample chosen for each distinct failure is the first
    # one, and forty samples failing the same step contribute one entry, not forty.
    by_step, order = {}, []
    for j in failed:
        if j.step not in by_step:
            by_step[j.step] = []
            order.append(j.step)
        by_step[j.step].append(j)

    # Steps that actually broke come first, cancelled-downstream ones last. With
    # `limit` in play this is what decides which logs a model is handed, and a
    # cancelled job's log explains nothing -- reading five of those instead of the
    # one OOM would waste the entire investigation.
    order.sort(key=lambda step: not any(j.state in BROKE_STATES
                                        for j in by_step[step]))

    findings = []
    for step in order[:limit]:
        group = by_step[step]
        first = group[0]
        path = resolve_log(first, record)
        findings.append({
            "step": step,
            "count": len(group),
            "job": first.name,
            "job_id": first.job_id,
            "state": first.state,
            "maxrss": first.maxrss,
            "exit_code": first.exit_code,
            "log": path,
            "log_tail": tail(path, log_lines),
        })
    return {
        "failed_total": len(failed),
        "broke_total": sum(1 for j in failed if j.state in BROKE_STATES),
        "cancelled_total": sum(1 for j in failed if j.state == "CANCELLED"),
        "steps_affected": len(order),
        "truncated": max(0, len(order) - limit),
        "findings": findings,
    }


def log_report(job_list):
    """GenPipes' own progress report for a run. Returns raw text.

    The module load is part of the command because `genpipes` is not on PATH
    until Lmod puts it there, and this runs in a fresh non-login shell.
    """
    if not job_list:
        return ""
    raw, _ = _run(f"module load {GENPIPES_MODULE} && "
                  f"genpipes tools log_report --loglevel ERROR {job_list}")
    return raw


def parse_log_report(raw):
    """Pull the per-state counts and timings out of log_report's text.

    Lives here rather than in display.py for the same reason display.parse()
    exists: understanding what GenPipes said is not a rendering concern. A web
    UI would want these numbers too, and a test can assert on them without a
    terminal.

    total is 0 when nothing recognisable was found, which the caller must treat
    as "show the raw text instead" -- an unparsed report is still information,
    and silently rendering an empty bar would be a lie.
    """
    counts, total, meta = {}, 0, []
    for line in (raw or "").splitlines():
        m = re.match(r"\s*Number of jobs ([A-Z_]+):\s*(\d+)", line)
        if m:
            counts[m.group(1)] = int(m.group(2))
            continue
        m = re.match(r"\s*Number of jobs:\s*(\d+)", line)
        if m:
            total = int(m.group(1))
            continue
        # GenPipes' timing labels are long and unaligned; shorten them.
        m = re.match(r"\s*Cumulative time spent on compute nodes:\s*(.+)", line)
        if m:
            meta.append(("compute time", m.group(1).strip()))
            continue
        m = re.match(r"\s*Cumulative core time:\s*(.+)", line)
        if m:
            meta.append(("core time", m.group(1).strip()))
            continue
        m = re.match(r"\s*Human time.*?:\s*(.+)", line)
        if m:
            meta.append(("elapsed", m.group(1).strip()))
            continue
    return {"counts": counts, "total": total, "meta": meta}


def cancel(jobs):
    """scancel every job in a run that could still be stopped.

    Only pending and running jobs are passed to scancel: cancelling a job that
    already completed is harmless but produces an error per job, and a wall of
    scheduler errors after a destructive action is exactly when a user most
    needs to be able to tell whether it worked. Returns (n_targeted, output).
    """
    targets = [j.job_id for j in jobs
               if j.job_id and (j.active or j.state is None)]
    if not targets:
        return 0, ""
    raw, _ = _run(f"scancel {' '.join(targets)}")
    return len(targets), raw


# ===========================================================================
#  Naming. Small, but it is the first thing the user is asked for.
# ===========================================================================

_STOPWORDS = {"run", "the", "a", "an", "on", "for", "with", "my", "please",
              "all", "and", "to", "of", "using", "use", "then", "from", "in",
              "steps", "step", "pipeline", "genpipes", "do", "can", "you",
              # Words every task contains, so they distinguish nothing and only
              # crowd out the pipeline and protocol, which are what you'd
              # actually recognise the run by.
              "readset", "readsets", "samples", "sample", "data", "analysis"}


def suggest_name(task, when=None):
    """A plausible run name derived from the task text, e.g.

        "run dnaseq germline_snv on my readset, all steps"  ->  dnaseq-germline-snv-0725

    A run needs a name -- it is the handle for approving and checking it later,
    possibly from another session -- but nobody should be made to invent one
    before they have seen what is being proposed. So no one is asked: the name
    is derived here at the gate, from what the run turned out to be, and shown
    in the approval box next to the command it belongs to.

    The date suffix is what keeps names distinct in practice: the same pipeline
    gets run repeatedly, and the day is usually how a person remembers which
    time they mean.
    """
    when = when or datetime.date.today()
    words = re.findall(r"[a-z0-9_]+", (task or "").lower())
    keep = [w for w in words if w not in _STOPWORDS and not w.isdigit()]
    slug = "-".join(keep[:3]).replace("_", "-")[:32].strip("-")
    return f"{slug or 'run'}-{when:%m%d}"
