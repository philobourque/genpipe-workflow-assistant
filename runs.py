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
Like gate_rules.py, this module imports nothing heavy, so the registry and the
job parsing are testable in CI in a couple of seconds. genpipe_agent.py holds
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
        return [r for r in current.values() if r["status"] != GONE]

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
        /check and /why are given. One conversation routinely produces several
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
        """Attach a finding to a run -- what /why concluded, in one line.

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
                 "maxrss", "exit_code")

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

    @property
    def failed(self):
        return self.state in BAD_STATES

    @property
    def active(self):
        return self.state in ACTIVE_STATES

    def __repr__(self):
        return f"<Job {self.job_id} {self.name} {self.state}>"


def parse_job_list(path):
    """Read a GenPipes job_list file into Job objects.

    Deliberately tolerant about the column layout. GenPipes writes this file for
    its own `log_report` to read back, the format has moved between versions,
    and it is not a documented interface -- so rather than hardcode a column
    order that a version bump can invalidate, each field is identified by what
    it looks like:

        the job id    the field that is all digits (or 12345_7 for an array task)
        the log       the field that looks like a path to a .o/.out/.log file
        the name      the longest field that is neither of those

    The consequence is that a format change costs us a field, not the feature.
    """
    jobs = []
    if not path or not os.path.exists(path):
        return jobs
    with open(path) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            fields = line.split("\t") if "\t" in line else line.split()
            fields = [x.strip() for x in fields if x.strip()]
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
            "--format=JobID,JobName,State,Elapsed,MaxRSS,ExitCode "
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
            states[job_id] = {
                "state": state,
                "elapsed": parts[3].strip() if len(parts) > 3 else None,
                "maxrss": parts[4].strip() if len(parts) > 4 else None,
                "exit_code": parts[5].strip() if len(parts) > 5 else None,
            }
    return states


def jobs_for(record, with_states=True):
    """Every job in a run, with live Slurm state attached where available."""
    jobs = parse_job_list(record.get("job_list"))
    if jobs and with_states:
        states = query_states([j.job_id for j in jobs])
        for j in jobs:
            info = states.get(j.job_id or "")
            if info:
                j.state = info["state"]
                j.elapsed = info["elapsed"]
                j.maxrss = info["maxrss"]
                j.exit_code = info["exit_code"]
    return jobs


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
        return f"{bad} need attention"
    active = sum(n for s, n in tally.items() if s in ACTIVE_STATES)
    if active:
        return f"{active} running"
    if tally.get("UNKNOWN"):
        return "state unknown"
    return "complete"


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
