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
import shutil
import subprocess

from . import usage

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

# The three states that exist so this tool can stop guessing about the cluster.
#
# Before them a run had two homes after /approve -- `held`, which invites a
# second approval for work already on the scheduler, and `submitted`, which
# invites monitoring for jobs that may not exist. On 2026-07-29 a real run took
# the first: 46 jobs were submitted and completed, the turn that would have
# recorded them died on an API error, and the record still said `held` a
# fortnight later. Both homes were lies, and the code picked whichever one the
# exception happened to leave behind.
#
#   SUBMITTING      /approve was accepted and the command is running. Written
#                   BEFORE the graph is resumed, so a process killed mid-flight
#                   leaves this rather than nothing.
#   SUBMIT_FAILED   execution ran and reported failure. Says nothing about what
#                   reached the scheduler -- see reconcile()'s `retry_safe`.
#   SUBMIT_UNKNOWN  the outcome could not be established. The honest state, and
#                   the one that must never be quietly upgraded to either
#                   neighbour.
SUBMITTING = "submitting"
SUBMIT_FAILED = "submit_failed"
SUBMIT_UNKNOWN = "submit_unknown"

# A proposal whose authorisation slot is gone.
#
# NOT a draft, and not an error: the command is complete and was good enough to
# have been offered. What is missing is the live gate interrupt, without which
# /approve has nothing to act on -- the graph consumed it (a question answered
# at the gate), or the turn holding it died.
#
# This exists because `held` was carrying two meanings and only advertising one.
# A record left saying `held` after its gate was gone put "waiting for approval"
# in /list beside a run /approve would refuse, and there was no third word to
# write instead. There is now, and it comes with its own next action: /modify
# rebuilds the proposal and re-gates it.
#
# Reachable but rare by design -- see agent.regate(), which restores the real
# decision point rather than the label whenever there is one to restore.
LAPSED = "lapsed"

# Statuses meaning "the command ran, or may have run". None of them may be
# approved again, and every one of them wants reconciling against Slurm before
# anything is retried.
AFTER_APPROVAL = (SUBMITTING, SUBMITTED, SUBMIT_FAILED, SUBMIT_UNKNOWN)

# Statuses nothing will reclassify. A person said no, or the artifacts are gone;
# neither is a state evidence can talk anybody out of.
TERMINAL = (ABANDONED, GONE)

# Statuses that carry a durable proposal and no submission. The two states a
# reconciler chooses BETWEEN, which is why they are named together.
BEFORE_APPROVAL = (HELD, LAPSED)

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
        """Every record, newest first. This is /history.

        Keyed on _listing_key -- submitted_at OR held_at -- and that `or` is a
        bug fix rather than a tidy-up. Sorting on submitted_at alone gave the
        same empty string to every record that was never submitted, so held,
        lapsed and abandoned runs all compared equal and fell out in the
        registry's raw append order, in one undifferentiated block below every
        submitted run. /history claimed to be newest first and was newest first
        only for the runs that had launched.
        """
        records = self.load()
        if prune and self._prune(records):
            self.save(records)
        return sorted(records, key=_listing_key, reverse=True)

    def live(self, prune=True):
        """Runs still worth acting on: submitted and not purged, plus anything
        held or lapsed. This is /list.

        Held runs belong here even though they have no job list and nothing on
        the scheduler -- a run waiting for your approval is the single most
        actionable thing the tool can be holding, so it cannot be the one thing
        /list leaves out. AGE IS NOT A CRITERION: a proposal from three weeks
        ago whose gate is still open has a real next action and belongs here
        exactly as much as one from this morning.

        Lapsed runs belong here for the same reason and a different action --
        they cannot be approved, but /modify rebuilds them, and a proposal
        somebody is still going to want is not history yet.

        `prune=False` skips the gone-file sweep, for callers that are about to
        reconcile the whole registry and do not want a save in the middle of it.
        """
        records = self.load()
        if prune and self._prune(records):
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
        revision = (proposal or {}).get("revision")
        existing = self.get(name)
        if existing and existing["status"] in BEFORE_APPROVAL:
            fields = {"proposal": proposal, "workdir": workdir,
                      "status": HELD, "held_at": _now(), "revision": revision}
            # THE PROPOSAL THIS ONE REPLACES CAN NEVER COME BACK.
            #
            # Reaching the gate again on the same run means the command was
            # rethought -- a /modify, or a rejection the model acted on. The
            # old revision is not merely uninteresting: an interrupt raised
            # for it may still be sitting in some checkpoint, and without this
            # list nothing would stop that stale authorisation from being
            # honoured. Recorded on the way past, which is the only moment
            # both revisions are in the same place.
            was = existing.get("revision")
            if was and revision and was != revision:
                fields["superseded"] = list(
                    dict.fromkeys(list(existing.get("superseded") or ()) + [was]))
            return self.update(name, **fields)
        return self._append({
            "name": str(name),
            "thread_id": str(thread_id) if thread_id is not None else None,
            "status": HELD,
            "job_list": None,
            "workdir": workdir,
            "proposal": proposal,
            "revision": revision,
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

    def begin_submission(self, name, workdir=None, baseline=None, script=None,
                         since=None):
        """Mark a run as being submitted, BEFORE the command is allowed to run.

        The record has to change before the irreversible act, not after it. A
        process killed between the two leaves `submitting`, which is visibly
        unfinished and asks to be reconciled -- where leaving `held` would
        invite a second approval of work that may already be on the scheduler.

        THE BASELINE IS PERSISTED HERE, and that is what makes the reconciling
        possible at all after a crash. It was previously a local variable in
        agent.resume(): if the process died mid-submission, the one measurement
        that says which job rows belong to THIS approval died with it, and a
        later reconciliation could only ever answer "unknown" -- for a run that
        may have put a full pipeline on the cluster. A snapshot of a file is
        cheap to store and impossible to reconstruct afterwards, so it goes on
        the record next to the status it justifies.
        """
        fields = {"status": SUBMITTING, "submitted_at": _now()}
        if workdir:
            fields["workdir"] = workdir
        if baseline is not None:
            fields["job_list_baseline"] = baseline
        if script:
            fields["submitted_script"] = script
        if since is not None:
            fields["submitted_since"] = float(since)
        return self.update(name, **fields)

    def submitting(self):
        """Runs recorded as mid-submission. Normally empty.

        A record only stays here if the process died between `/approve` and the
        reconciliation in resume()'s `finally` -- a kill, a closed terminal, a
        node reboot. See agent.reconcile_stale(), which is what empties it.
        """
        return [r for r in self.live() if r["status"] == SUBMITTING]

    def record_outcome(self, name, outcome, workdir=None, proposal=None):
        """Write what reconcile() established. The only way a run becomes
        `submitted`, and the only way it stops being `submitting`.

        The evidence is stored alongside the verdict -- how many rows this
        approval added, how many the script promised, whether the scheduler was
        asked and found quiet -- because "submitted" on its own is the claim
        that was being made without proof, and the next reader deserves to see
        what it rests on.
        """
        fields = {
            "status": outcome.status,
            "jobs_seen": outcome.jobs_seen,
            "expected_jobs": outcome.expected,
            "retry_safe": bool(outcome.retry_safe),
            "outcome_detail": outcome.detail or "",
            "reconciled_at": _now(),
        }
        if outcome.job_list and os.path.exists(outcome.job_list):
            fields["job_list"] = outcome.job_list
        if workdir:
            fields["workdir"] = workdir
        if proposal:
            fields["proposal"] = proposal
        return self.update(name, **fields)

    def adoption_blocked(self, name):
        """Why `name` cannot be adopted onto, as a sentence, or None if it can.

        ADOPTION MINTS A RECORD; IT MUST NEVER SILENTLY REPLACE ONE. Without
        this, `/track <the name of a run waiting at the gate> <any path>`
        overwrote that record's status to `submitted` while the graph was
        still parked on its interrupt -- so the run vanished from the pending
        section of /list and /view stopped offering /approve, while the
        decision itself sat there, live and unreachable. An orphaned approval
        is the exact class of state-versus-display contradiction the lifecycle
        work exists to close, and it does not self-heal: reconciliation treats
        a settled `submitted` record as authoritative and leaves it alone.

        Three rules, in order of how much is at stake:

          a live decision   held or lapsed. This tool owns a proposal under
                            that name and possibly an open gate. Refused.
          our own run       source == "agent". Its name is tied to a
                            conversation and a generation command, and
                            overwriting it would throw both away for a job
                            list somebody typed. Refused.
          an adopted run    source manual or scan. Re-pointing one at a
                            corrected path is the whole reason somebody types
                            this twice. Allowed.

        Returns a sentence rather than a boolean so the caller can say which
        run is in the way -- "that name is taken" sends somebody to /list to
        work out what by.
        """
        record = self.get(name)
        if record is None:
            return None
        if record["status"] in BEFORE_APPROVAL:
            return (f"'{name}' is a run this tool is holding a decision for "
                    f"({record['status']}). Adopting onto it would discard "
                    f"that proposal.")
        if (record.get("source") or "agent") == "agent":
            return (f"'{name}' is already a run built here — it has a command "
                    f"on record and a conversation behind it.")
        return None

    def track(self, name, job_list):
        """Register a run launched outside the agent -- no thread, no gate, no
        conversation -- so it can be checked and analysed by name like any other.

        Returns (record, reason). A record of None means nothing was written
        and `reason` says why, which is the shape every caller needs: this is
        reached from a command line where both the name and the path are typed
        by hand, so both can be wrong and each is wrong in its own way.
        """
        blocked = self.adoption_blocked(name)
        if blocked:
            return None, blocked

        # THE FILE HAS TO BE A JOB LIST. Not merely present -- present was the
        # only check, so `/track notes-1 ./notes.txt` on a text file produced a
        # permanent record with one UNKNOWN job in it, from a typo. job rows
        # are counted with the same parser /check will use, so a file that
        # passes here is a file the rest of the tool can read.
        state = job_list_state(job_list)
        if not state.get("rows"):
            return None, (f"{os.path.basename(job_list)} has no GenPipes job "
                          f"rows in it. A job list is tab-separated with a job "
                          f"id in the first column.")

        record = self.mark_submitted(
            name, job_list,
            workdir=os.path.dirname(os.path.dirname(job_list)),
            source="manual")
        return record, None

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

    def rediscover(self, name, found):
        """Re-point an existing record at artifacts /scan found again.

        The restore half of rediscovery(). It writes exactly four things --
        where the run is, which job list is newest, the attempts under it, and
        the fact that it is neither gone nor hidden any more -- and touches
        nothing else.

        WHAT IT DELIBERATELY DOES NOT WRITE, because a restore that quietly
        rewrote identity would be worse than the refusal it replaces:

          source        a run built here stays built here. Finding its files
                        on disk is not evidence it was launched elsewhere, and
                        relabelling it would lose the conversation behind it.
          proposal      the command it is remains the command it is.
          thread_id     ditto, and it is what /approve would resume through.
          decisions     the record of what a person authorised is not ours to
                        edit from a directory listing.

        Nothing on disk is read beyond the paths /scan already collected, and
        nothing on disk is written at all.
        """
        record = self.get(name)
        if record is None:
            return None
        attempts = list(dict.fromkeys(
            (found.get("attempts") or []) + (record.get("attempts") or [])))
        return self.update(name,
                           status=SUBMITTED,
                           gone=False,
                           hidden=False,
                           job_list=found["job_list"],
                           workdir=found.get("workdir") or record.get("workdir"),
                           attempts=attempts or [found["job_list"]],
                           rediscovered_at=_now())

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
        # Lapsed as well as held. A proposal whose gate is gone is exactly the
        # kind somebody wants to be rid of -- it cannot be approved, so the
        # only two things to do with it are rebuild it or drop it, and
        # refusing the second would leave /list holding it forever.
        if record["status"] not in BEFORE_APPROVAL:
            return record
        fields = {"status": ABANDONED, "abandoned_at": _now()}
        # The proposal this rejection was about, so it can never come back
        # through a re-gate. See classify()'s rule 4.
        if record.get("revision"):
            fields["rejected"] = list(
                dict.fromkeys(list(record.get("rejected") or ())
                              + [record["revision"]]))
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

    def remember_remediation(self, name, override, uncertain=(), before=None):
        """Persist the machine-applicable fix /diagnose worked out, as data.

        THE POINT IS THAT NOTHING DOWNSTREAM RE-READS PROSE. /diagnose renders
        a screen; the OVERRIDE section of that answer is already structured by
        diagnosis.parse(), and this is where it stops being a thing on a
        terminal and becomes a thing a later command can act on. /relaunch
        reads it, applies it, and never sees a sentence -- which is the whole
        reason the contract in diagnosis.SHAPE asks for an ini fragment rather
        than advice.

        `uncertain` TRAVELS WITH IT, and that is not bookkeeping. The fix and
        what the run failed to establish about the fix are one finding: a
        walltime raised to a value nothing proved sufficient is exactly as
        uncertain a week later as it was on the screen it was printed on. Storing
        the remedy without its caveats is how a proposal becomes a
        recommendation somewhere between two commands.

        `before` is what the run's own config trace recorded for the same keys
        at generation time -- the left-hand side of `0:10:00 -> 35:00:00`. Read
        from the trace rather than derived, and stored rather than recomputed,
        because the trace is a snapshot of a moment that has passed.

        Supersedes proposed_override, which held the same fragment and nothing
        else. See remediation_of() for the read side, which still understands
        the old field.
        """
        if not override:
            return self.get(name)
        return self.update(name, remediation={
            "override": {s: dict(k) for s, k in override.items()},
            "uncertain": [str(u) for u in (uncertain or ())],
            "before": {s: dict(k) for s, k in (before or {}).items()},
            "source": "diagnose", "at": _now()})

    def derive(self, name, source, reason):
        """Record that `name` was built from `source`, and why.

        LINEAGE, NOT A SECOND PROVENANCE SUBSYSTEM. A revision is a new run
        with its own name, its own thread and its own gate -- deliberately, so
        that the original keeps its job list and its jobs -- and the cost of
        that separation is that nothing on the new record says where it came
        from. Two fields is the whole of the fix: /history reads them, and the
        relationship survives in the same append-only file everything else
        about a run lives in.

        Never written to the SOURCE. The original is historical truth and this
        would be a mutation of it; a parent that listed its children would also
        have to be updated every time one appeared, which is a write to a
        launched record for the sake of a link that can be found by reading
        the other end.
        """
        if not source or not reason:
            return self.get(name)
        return self.update(name, derived_from=str(source),
                           derived_reason=str(reason))

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

    def add_decision(self, name, decision, revision=None, outcome=None,
                     feedback=None):
        """Record what a human decided at the gate, as data. Returns the entry.

        THE ONLY ACCOUNT OF A GATE DECISION THAT ANYTHING SHOULD TRUST, and
        the reason it exists is a specific failure. What the model used to get
        was a rewritten copy of its own message: the application replaced its
        `propose_submission("cmd.sh")` with `bash cmd.sh` in place, so that
        biomni's execute node would find something runnable. The transcript
        that survived showed the assistant launching a script with no gate
        anywhere in sight, and on the next turn it read that back and
        apologised to the user for bypassing an approval that had in fact been
        given:

            "I have to flag something before anything else: I submitted that
             run directly instead of putting it through the approval box
             first. That was my mistake."

        It had not. The evidence had been erased from the record it was
        reading. So the decision is stored here, structurally, and the sentence
        the model sees is RENDERED from it -- never hand-written, never edited
        afterwards, and never attributed to the assistant. If the outcome is
        later reconciled to something else, this changes and the rendering
        changes with it.

        Append-only, because a decision is a historical fact and a list of them
        is the run's history. `by` is recorded and is always the user: nothing
        in this application decides to submit.
        """
        record = self.get(name)
        if record is None:
            return None
        entry = {"decision": decision, "revision": revision,
                 "at": _now(), "by": "user"}
        if feedback:
            entry["feedback"] = feedback
        if outcome is not None:
            entry["outcome"] = {"status": outcome.status,
                                "jobs": outcome.jobs_seen,
                                "expected": outcome.expected,
                                "job_list": outcome.job_list,
                                "detail": outcome.detail}
        self.update(name, decisions=list(record.get("decisions") or ()) + [entry])
        return entry

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
            # Any run that got as far as running its command and left a job
            # list behind, not only a cleanly submitted one: a partial or
            # unreconciled submission whose artifacts were purged is just as
            # gone, and leaving it in /list forever was the same bug.
            if r["status"] not in AFTER_APPROVAL or not r["job_list"]:
                continue
            if not os.path.exists(r["job_list"]):
                r["status"] = GONE
                r["gone"] = True
                r["gone_at"] = _now()
                changed = True
        return changed


def remediation_of(record):
    """The structured fix /diagnose proposed for this run, or {}.

    Keys: `override` ({section: {key: value}}), `uncertain` (a list of the
    things the run did NOT establish), `before` (what the config trace said for
    the same keys), `at`, `source`.

    READS THE OLD FIELD TOO. `proposed_override` held the same ini fragment
    before remember_remediation() existed, and a cluster's runs.jsonl is full of
    them. Those records come back with an empty `uncertain`, which is honest --
    the caveats were never stored -- and callers must not read an empty list as
    "nothing was uncertain". They read it as "nothing was recorded", which is
    why /relaunch prints what it has and says where it came from rather than
    asserting a clean bill of health.

    An override that is present but empty is the same as no remediation. A
    diagnosis that found no config change to make is the ordinary case (the
    contract tells the model to omit OVERRIDE unless the fix really is one),
    and it must not read as a remediation with nothing in it.
    """
    found = (record or {}).get("remediation")
    if isinstance(found, dict) and found.get("override"):
        return {"override": {s: dict(k) for s, k in found["override"].items()},
                "uncertain": [str(u) for u in (found.get("uncertain") or ())],
                "before": {s: dict(k) for s, k in
                           (found.get("before") or {}).items()},
                "at": found.get("at") or "",
                "source": found.get("source") or "diagnose"}
    legacy = (record or {}).get("proposed_override")
    if isinstance(legacy, dict) and legacy:
        return {"override": {s: dict(k) for s, k in legacy.items()},
                "uncertain": [], "before": {}, "at": "", "source": "diagnose"}
    return {}


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
    # Submission evidence. Absent on every record written before reconcile()
    # existed, and absent is not zero: `jobs_seen` None means nobody counted,
    # which is exactly right for a run recorded when nothing did.
    record.setdefault("jobs_seen", None)
    record.setdefault("expected_jobs", None)
    record.setdefault("retry_safe", False)
    record.setdefault("outcome_detail", "")
    # The measurement taken before an approved command ran, kept so a crash
    # between /approve and the reconciliation can still be resolved. Absent on
    # every record written before it existed, and absent means "no baseline",
    # which rows_added() reads as "cannot be established" rather than zero.
    record.setdefault("job_list_baseline", None)
    record.setdefault("submitted_script", None)
    record.setdefault("submitted_since", None)
    # Proposal identity. `revision` names the proposal this record currently
    # holds; the two lists are the revisions that can never become actionable
    # again. All absent on every record written before identity existed, and
    # absent means NO OPINION -- classify() and the approval check both skip a
    # comparison they cannot make, so nothing built earlier becomes
    # unapprovable. See gate.revision().
    record.setdefault("revision", None)
    record.setdefault("superseded", [])
    record.setdefault("rejected", [])
    # What a human decided at the gate, as data. The model's account of an
    # approval is RENDERED from this rather than written by hand, so the
    # transcript cannot drift from what happened. See agent._record_decision.
    record.setdefault("decisions", [])
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

    __slots__ = ("job_id", "name", "step", "log", "deps", "state", "elapsed",
                 "maxrss", "exit_code", "start", "timelimit", "reason")

    def __init__(self, job_id=None, name="", log=None, deps=()):
        self.job_id = job_id
        # THE IDS THIS JOB WAITED ON. Column 3 of the manifest, which
        # parse_job_list read and threw away -- so the dependency structure
        # was parsed by this application and then rediscovered by the model,
        # which read the raw job_list with `head -40` to find out which jobs
        # were downstream of the one that broke. It is the only place the DAG
        # is written down: sacct does not carry it and squeue forgets it once
        # the jobs are terminal.
        self.deps = tuple(deps or ())
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


# ===========================================================================
#  Did the submission happen? The evidence, and what it is worth.
#
#  Everything in this section exists to answer one question mechanically, so
#  that no model is ever asked "did you submit?" and no exception can turn a
#  real submission into a lost one.
#
#  WHAT THE EXECUTION LAYER ACTUALLY GIVES US, verified against biomni 0.0.8
#  and against five real generated scripts (dnaseq somatic_fastpass, dnaseq
#  germline_sv, ampliconseq):
#
#    biomni's run_bash_script returns `result.stdout` on success, and on
#    failure returns "Error running Bash script (exit code N):\n{stderr}"
#    -- DISCARDING STDOUT. So on the failure path every "Submitted job with
#    ID:" line is destroyed by the runner. That is why the job list on disk,
#    not the observation, is the authority on what was submitted: GenPipes
#    appends to it with >> immediately after each sbatch, so a run that died
#    half way leaves the rows for the jobs it did create.
#
#    The scripts open with `set -eu -o pipefail` and submit with
#        JOB_ID=$(sbatch $SUBMISSION_FILE | awk '{print $4}')
#    A command substitution inside a pipeline. `pipefail` is the single token
#    that makes a failed sbatch abort the script: without it awk exits 0, the
#    pipeline exits 0, the failure is invisible and the script runs to
#    completion emitting an EMPTY job id. Verified both ways. It is present in
#    every script seen so far -- and it is written by GenPipes, not by us, so
#    it is checked rather than assumed, and exit 0 is never trusted on its own.
# ===========================================================================

# `# TOTAL: 46 jobs`, from the header GenPipes writes above every script. The
# script's own count of what it intends to submit, which is what makes "exit 0"
# checkable instead of merely hopeful.
#
# GenPipes writes that line in TWO shapes, and missing the second one cost a
# whole approval:
#
#     #   TOTAL: 46 jobs
#     #   TOTAL: 0 job... skipping
#
# The second is what a pipeline emits when every output is already on disk --
# an entirely ordinary thing to hit when re-running a test into a directory
# that still holds yesterday's results. This pattern used to anchor `jobs?` to
# end-of-line, so `0 job... skipping` did not match and expected_jobs() came
# back None. None means "the script does not say", which sent a perfectly
# well-understood outcome down reconcile()'s unestablished branch: the person
# saw `the outcome is unknown  ·  the script declares no job total` for a
# submission whose script said, in the clearest terms available, that it
# contained nothing to submit.
#
# Both shapes exactly, rather than a loose tail. A third shape should come back
# None and be called unknown, which is the honest answer for a header this code
# has never seen -- see expected_jobs on why None must never soften into zero.
_TOTAL_JOBS = re.compile(
    r"^#\s*TOTAL:\s*(\d+)\s+jobs?(?:\.\.\.\s*skipping)?\s*$", re.M)

# The four header assignments that decide where the job list lands. All literal
# in the header except for the $-references between them, which resolve against
# each other and nothing else.
_HEADER_VAR = re.compile(r"^\s*(OUTPUT_DIR|JOB_OUTPUT_DIR|TIMESTAMP|JOB_LIST)="
                         r"(\S+)\s*$", re.M)

# What biomni prints instead of the output when the block exits non-zero. A
# string in a pinned dependency, so it is matched loosely and, more to the
# point, is never the only thing consulted -- reconcile() also requires the job
# count to agree with the script's declared total before it will say SUBMITTED.
_RUNNER_ERROR = re.compile(r"Error running Bash script \(exit code\s*(\d+)")

# `Submitted job with ID: 17784414`. The id is captured separately because an
# EMPTY one is itself a finding: it is what a failed sbatch leaves behind in a
# script that lacks pipefail, and counting it as a submission would turn the
# one case exit-status cannot see into a false success.
# The `(?:^|<observation>)` alternation is not defensive dressing -- it is an
# off-by-one that cost a job on every single count. An observation is stored as
# one string with the tag glued to the first line:
#
#     <observation>Submitted job with ID: 17784414
#     Submitted job with ID: 17784415
#
# so `^\s*` matched every line except the first, and a 46-job submission
# reported 45 ids. Harmless while ids were only corroboration; not harmless now
# that they are the evidence recovering a lost outcome, where 45-of-46 reads as
# a partial submission and 46-of-46 reads as a clean one.
_SUBMITTED_LINE = re.compile(
    r"(?:^|<observation>)\s*Submitted job with ID:\s*(\S*)\s*$", re.M)


def expand(path):
    r"""A path as the SHELL would read it: `~` and `$VARS` resolved.

    Nothing in os.path does this, and the omission is silent rather than loud.
    `os.path.isabs("~/run/cmd.sh")` is False -- as far as os.path is concerned
    that is a relative path beginning with a directory literally named `~` --
    and `os.path.exists()` on it goes looking for one. A model writes these
    constantly, because they are what a person writes:

        -g ~/ampliconseq_cit_test/ampliconseq_cit_cmd.sh
        -r $MUGQIC_INSTALL_HOME/testdata/ampliconseq/readset.txt

    What that cost: a run whose script was generated perfectly, at a path bash
    would have expanded without comment, was refused at /approve as "not on
    disk" -- and every run with a `~` or `$VAR` in its `-g` has been unable to
    find its own job list, so a complete submission reconciled as
    `submit_unknown` rather than as the success it was.

    A `$` that survives is left standing rather than blanked. `expandvars`
    leaves an unset variable as written, and that distinction is the whole
    point: a path still holding a `$` is one this process CANNOT check -- the
    variable is set by `module load` in the shell that runs the command, not
    here -- and a caller must read that as "unverifiable", never as "absent".
    See resolvable().
    """
    return os.path.expanduser(os.path.expandvars(str(path or "")))


def resolvable(path):
    """Can this process check whether `path` exists at all?

    False for a path still carrying an unexpanded `$VAR` after expand(). The
    honest answer about such a path is "I don't know", and the one thing a
    caller must not do is turn "I don't know" into "it isn't there" -- that is
    a refusal aimed at a run that is probably fine.
    """
    return bool(path) and "$" not in expand(path)


def resolve_path(path, *bases):
    """`path` as an absolute path that EXISTS, or None.

    Tries it as written (after expand()), then against each base directory in
    turn for a relative path. Bases are given most-authoritative first by the
    caller; None and "" are skipped so callers can pass optional fields
    straight through.
    """
    if not path:
        return None
    expanded = expand(path)
    candidates = [expanded]
    if not os.path.isabs(expanded):
        candidates = [os.path.join(expand(base), expanded)
                      for base in bases if base] + [expanded]
    for candidate in candidates:
        if os.path.exists(candidate):
            return os.path.abspath(candidate)
    return None


def observation(output, code):
    """An <observation> for a command this tool ran itself, in the one shape
    the reconciliation already reads.

    Deliberately imitates what biomni's execute node prints, rather than
    inventing a second format. execution_failed() and reconcile() decide
    whether a submission failed by looking for _RUNNER_ERROR, and a submission
    run outside the graph -- see agent._perform_submission -- has to be
    gradeable by exactly the same standard as one run inside it. Two formats
    would be two parsers that have to agree forever about what a failure looks
    like.
    """
    body = str(output or "")
    if code:
        return (f"<observation>Error running Bash script (exit code {code}):\n"
                f"{body}</observation>")
    return f"<observation>{body}</observation>"


def _header(script):
    """The literal header assignments of a generated script, as a dict."""
    if not script or not os.path.exists(script):
        return {}
    try:
        with open(script) as f:
            head = f.read(8192)
    except OSError:
        return {}
    return {m.group(1): m.group(2).strip("\"'")
            for m in _HEADER_VAR.finditer(head)}


def declared_job_list(script):
    """The exact job list path a generated script will append to, or None.

    Resolved from the script's own header rather than found by globbing, and
    that is what makes a BASELINE possible: the file has to be identified
    before the submission runs, and find_job_list() cannot do that because it
    selects on a modification time that has not happened yet.

    It matters for a second reason. The TIMESTAMP is baked in at generation
    time, so re-running one script appends to ONE file -- a retry after a
    partial failure lands in the same list as the attempt that failed. Without
    a baseline those earlier rows would be counted as this approval's work.
    """
    head = _header(script)
    listed = head.get("JOB_LIST")
    if not listed:
        return None
    resolved = listed
    for _ in range(4):                     # OUTPUT_DIR -> JOB_OUTPUT_DIR -> JOB_LIST
        before = resolved
        for key in ("JOB_OUTPUT_DIR", "OUTPUT_DIR", "TIMESTAMP"):
            if head.get(key):
                resolved = resolved.replace(f"${key}", head[key])
                resolved = resolved.replace(f"${{{key}}}", head[key])
        if resolved == before:
            break
    if "$" in resolved:                    # an unresolved reference is not a path
        return None
    if not os.path.isabs(resolved):
        base = head.get("OUTPUT_DIR") or os.path.dirname(os.path.abspath(script))
        resolved = os.path.join(base, resolved)
    return os.path.normpath(resolved)


def expected_jobs(script):
    """How many jobs the script says it will submit, or None if it does not say.

    None is not zero and must never be treated as it: a script whose header
    could not be read gives no basis for saying a submission was complete.
    """
    if not script or not os.path.exists(script):
        return None
    try:
        with open(script) as f:
            head = f.read(8192)
    except OSError:
        return None
    m = _TOTAL_JOBS.search(head)
    return int(m.group(1)) if m else None


def has_pipefail(script):
    """Whether the script's `set` line makes a failed sbatch abort it.

    Consulted so that "exit 0" is believed only where the script's own
    semantics support it. Where it is absent, a clean exit proves nothing and
    reconcile() falls through to the job count instead.
    """
    head = _header(script) if script else {}
    if not head and not (script and os.path.exists(script)):
        return False
    try:
        with open(script) as f:
            text = f.read(8192)
    except OSError:
        return False
    return bool(re.search(r"^\s*set\s+-[a-z]*e[a-z]*\s+-o\s+pipefail", text, re.M)
                or re.search(r"^\s*set\s+-o\s+pipefail", text, re.M))


def job_list_state(path):
    """A snapshot of one job list: (rows, identity), for comparing later.

    `identity` is (device, inode, size). It is carried so that a file REPLACED
    between the two readings -- a different run writing the same path, a
    cleanup, a restore -- is detected rather than silently differenced, and so
    that a file that got SMALLER cannot yield a negative count.

    A path that does not exist yet is a legitimate baseline: rows 0, identity
    None. That is the ordinary case for a first submission.
    """
    if not path:
        return {"path": None, "rows": 0, "identity": None}
    try:
        st = os.stat(path)
        identity = (st.st_dev, st.st_ino, st.st_size)
    except OSError:
        return {"path": path, "rows": 0, "identity": None}
    rows = sum(1 for job in parse_job_list(path) if job.job_id)
    return {"path": path, "rows": rows, "identity": identity}


def rows_added(baseline, after):
    """Job rows this approval is responsible for, or None if that is not knowable.

    The delta, never the absolute count. GenPipes appends, an output directory
    is routinely reused, and a retry of the same script writes into the same
    file -- so the rows already present when /approve was typed belong to
    somebody else's submission and must not be credited to this one.

    Returns None rather than a number whenever the comparison is unsound:

      the two readings are of different files   (inode or device changed)
      the file shrank                           (replaced, truncated, rotated)
      no path could be identified at all

    None means "cannot be established", which reconcile() reads as unknown --
    never as zero.
    """
    if not baseline or not after:
        return None
    if not baseline.get("path") or baseline.get("path") != after.get("path"):
        return None
    before_id, after_id = baseline.get("identity"), after.get("identity")
    if before_id and after_id:
        if before_id[:2] != after_id[:2]:      # different file at the same path
            return None
        if after_id[2] < before_id[2]:         # shrank
            return None
    delta = after.get("rows", 0) - baseline.get("rows", 0)
    return delta if delta >= 0 else None


def execution_failed(observation):
    """Did the runner report a non-zero exit? True/False, or None if unreadable.

    None for an observation we never captured, which is a different thing from
    a clean run and is classified as such.
    """
    if observation is None:
        return None
    return bool(_RUNNER_ERROR.search(str(observation)))


def submitted_ids(observation):
    """Non-empty job ids the submission announced.

    Success-path corroboration only, and deliberately not the primary count:
    biomni discards stdout when the block exits non-zero, so on exactly the
    partial-submission path these lines do not survive to be counted.

    An empty id after the label is not counted, and that omission is the point:
    `Submitted job with ID:` with nothing after it is what a failed sbatch
    leaves in a script without pipefail.
    """
    if not observation:
        return []
    return [m.group(1) for m in _SUBMITTED_LINE.finditer(str(observation))
            if m.group(1)]


def scheduler_quiet_since(since, user=None):
    """True if Slurm shows no jobs for this user since `since`. None if unknown.

    The ONLY admissible evidence that a retry cannot double-submit. It exists
    because a job list with no new rows does not prove no job was created:
    GenPipes runs `sbatch` and appends the row as two separate statements, and
    a kill, a full disk or a quota rejection in between leaves a real job on
    the scheduler with nothing written down for it.

    Deliberately over-broad -- it asks about the user, not about this run,
    because a run whose rows were never written has no ids to ask about. An
    unrelated job submitted in the same window therefore reports "not quiet",
    which suppresses an offer to retry. That is the safe direction: the cost is
    one manual check, and the cost of the other direction is submitting a
    pipeline twice.

    None (sacct missing, unreachable, or erroring) is never read as quiet.
    """
    try:
        start = datetime.datetime.fromtimestamp(float(since))
    except (TypeError, ValueError, OSError):
        return None
    who = user or os.environ.get("USER") or ""
    if not who:
        return None
    raw, code = _run(
        f"sacct -u {who} -S {start:%Y-%m-%dT%H:%M:%S} "
        f"--parsable2 --noheader --format=JobID 2>/dev/null")
    if code != 0:
        return None
    for line in raw.splitlines():
        line = line.strip()
        # Top-level ids only; `12345.batch` is an accounting row for one of them.
        if line and "." not in line:
            return False
    return True


class Outcome:
    """What reconcile() decided, and the evidence it decided from."""

    __slots__ = ("status", "jobs_seen", "expected", "job_list", "retry_safe",
                 "detail")

    def __init__(self, status, jobs_seen=None, expected=None, job_list=None,
                 retry_safe=False, detail=""):
        self.status = status
        self.jobs_seen = jobs_seen
        self.expected = expected
        self.job_list = job_list
        self.retry_safe = bool(retry_safe)
        self.detail = detail

    def __repr__(self):
        return (f"<Outcome {self.status} jobs={self.jobs_seen}/{self.expected} "
                f"retry_safe={self.retry_safe}>")


def reconcile(script=None, observation=None, baseline=None, after=None,
              quiet=None):
    """Classify what an approved submission actually did. Pure, no IO.

    Every argument is evidence gathered elsewhere, so this is testable without
    a cluster and cannot be wrong about something it did not look at.

        script       the approved script, for its declared total and its `set`
                     line. None where it could not be read.
        observation  what the execute node returned, or None if never captured.
        baseline     job_list_state() taken BEFORE the submission ran.
        after        job_list_state() taken after.
        quiet        scheduler_quiet_since(), or None. Positive evidence only.

    THE RULE THIS IS ARRANGED AROUND: exit 0 is never sufficient on its own.
    It is accepted only when the number of job rows this approval added equals
    the number the script said it would submit. That is what catches the case
    exit status structurally cannot see -- a script without pipefail, whose
    failed sbatch leaves it running to a clean exit having submitted fewer jobs
    than it meant to.

    And the mirror of it: a failure with no new rows is NOT evidence that
    nothing reached Slurm, so `retry_safe` is never inferred from a count. It
    is true only when the scheduler itself was asked and came back empty.
    """
    failed = execution_failed(observation)
    expected = expected_jobs(script)
    added = rows_added(baseline, after)
    path = (after or baseline or {}).get("path")
    ids = submitted_ids(observation)
    safe = quiet is True

    # Nothing was captured and nothing was written. This is the shape a turn
    # takes when it died before the command ran -- but it is also the shape it
    # takes when the command ran and the process was killed before either the
    # observation or the append landed, so it is unknown rather than harmless.
    if failed is None and not added and not ids:
        return Outcome(SUBMIT_UNKNOWN, jobs_seen=added, expected=expected,
                       job_list=path, retry_safe=safe,
                       detail="the outcome of the submission was never "
                              "established")

    # NO OBSERVATION, BUT THE JOB LIST ADDS UP. The shape a killed process
    # leaves behind: the exit status is gone with the terminal, and the job
    # list is still on disk.
    #
    # This is a promotion without an exit status, so it is worth being explicit
    # about why it is not a guess. GenPipes appends a row only AFTER the sbatch
    # that created that job returned, so N rows is direct evidence of N
    # successful submissions; `# TOTAL: N` is the script's own statement of how
    # many it intended. The two together say every intended submission
    # occurred, which is the same standard the exit-status path is held to --
    # the exit status simply is not the thing that establishes it.
    #
    # Requires a real declared total and an exact match. Fewer rows than
    # promised, more than promised, or no total at all all fall through to
    # unknown below.
    if failed is None and expected is not None and added == expected and added:
        return Outcome(SUBMITTED, jobs_seen=added, expected=expected,
                       job_list=path,
                       detail="established from the job list alone — the "
                              "session that submitted it did not survive to "
                              "report back")

    if failed:
        return Outcome(SUBMIT_FAILED, jobs_seen=added, expected=expected,
                       job_list=path, retry_safe=safe,
                       detail="the submission command reported a failure")

    if failed is False:
        if expected == 0 and not added:
            return Outcome(SUBMITTED, jobs_seen=0, expected=0, job_list=path,
                           detail="no jobs — everything was already up to date")
        if expected is not None and added == expected:
            # The ONLY route to SUBMITTED on a non-empty run: the script said
            # how many jobs it would submit, and exactly that many rows
            # appeared. Two independent facts agreeing.
            return Outcome(SUBMITTED, jobs_seen=added, expected=expected,
                           job_list=path, detail="")
        # Clean exit that cannot be checked, or that does not add up.
        #
        # PIPEFAIL IS NOT ENOUGH ON ITS OWN, and this used to promote on it.
        # `set -e -o pipefail` establishes that no sbatch the script actually
        # RAN returned non-zero -- it establishes nothing about whether the
        # script contained every submission it was supposed to. A truncated
        # generation, a step loop that emitted no sbatch at all, or a script
        # this tool has never seen the shape of all exit 0 having submitted
        # less than intended. Without a declared total there is no second fact
        # to check the exit status against, so the honest answer is that the
        # outcome is unestablished.
        #
        # pipefail is still worth SAYING, because it changes what the person
        # should suspect: with it, whatever did run ran cleanly.
        if expected is None:
            why = ("the script declares no job total, so a clean exit cannot "
                   "be checked for completeness")
            if has_pipefail(script):
                why += " (it does use pipefail, so no submission it ran failed)"
        else:
            why = (f"the run reported success but "
                   f"{added if added is not None else 'an unknown number'} of "
                   f"{expected} jobs were recorded")
        return Outcome(SUBMIT_UNKNOWN, jobs_seen=added, expected=expected,
                       job_list=path, retry_safe=safe, detail=why)

    # An observation we could not classify, with rows or ids to show for it.
    return Outcome(SUBMIT_UNKNOWN, jobs_seen=added, expected=expected,
                   job_list=path, retry_safe=safe,
                   detail="the outcome of the submission could not be read")


# ===========================================================================
#  Classification: which status a record's EVIDENCE supports.
#
#  The registry caches a status because reading one should not cost a
#  deserialised message history per row. A cache is only safe if something can
#  correct it, and this is that something: one pure function, over evidence
#  gathered from every durable source, whose answer beats whatever happens to
#  be written down.
#
#  Pure and stdlib-only on purpose. Deciding is a table; GATHERING needs the
#  graph, the filesystem and sometimes Slurm, and that half lives in
#  agent.evidence_for(). Split this way the precedence rules -- the part that
#  is easy to get subtly wrong and impossible to notice -- are testable in CI
#  in milliseconds, against evidence dicts written by hand.
# ===========================================================================

# Evidence that was not gathered. Distinct from False, and the distinction is
# the whole reason the sentinel exists: "no live gate interrupt" is grounds to
# lapse a run, and "the checkpoint could not be opened" is grounds to change
# nothing. Collapsing them would let a locked database retire every pending
# decision in the registry.
UNKNOWN = "?"


def trace_owner(trace, records, directory=None):
    """The tracked run a past-run config came from, or None when it cannot be
    established.

    NONE IS THE COMMON ANSWER AND THE HONEST ONE. A trace names the script it
    generated (`-g cmd.sh` in its header) and a run record names the same thing
    in `proposal["script"]`, which is a real link -- but not a unique one: a
    regeneration overwrites the script under the same name, so two traces in
    this very directory name `dnaseq_somatic_fastpass_cit.sh`, sixteen minutes
    apart. Matching them to one record would mean choosing between two runs on
    no evidence.

    So this answers only when EXACTLY ONE record matches, on both the script and
    the directory. Zero matches and several matches are the same answer -- None
    -- and the caller shows the script name from the file instead, which is a
    fact rather than an association.

    Nothing here reads a pipeline, a protocol or a status to decide. Those are
    metadata the view displays; they are not evidence about which run this was
    and must never be used as if they were.
    """
    script = str((trace or {}).get("script") or "").strip()
    if not script:
        return None
    where = os.path.abspath(directory or (trace or {}).get("path") and
                            os.path.dirname((trace or {}).get("path") or "") or ".")
    hits = []
    for record in records or ():
        named = ((record or {}).get("proposal") or {}).get("script")
        if not named or os.path.basename(str(named)) != os.path.basename(script):
            continue
        home = (record or {}).get("workdir")
        if home and os.path.abspath(str(home)) != where:
            continue
        hits.append(record)
    return hits[0] if len(hits) == 1 else None


def tracked_workdirs(records):
    """The distinct directories tracked runs live in, newest first.

    What "search other tracked runs" is allowed to look at, and the boundary is
    the point: these are directories this application already knows about
    because it recorded a run in each. It never walks a project space, a scratch
    filesystem or a home directory looking for more.
    """
    seen = []
    for record in sorted(records or (),
                         key=lambda r: str((r or {}).get("held_at") or ""),
                         reverse=True):
        home = (record or {}).get("workdir")
        if not home:
            continue
        home = os.path.abspath(str(home))
        if home not in seen and os.path.isdir(home):
            seen.append(home)
    return seen


def interrupt_claim(entries):
    """What may honestly be said about the scheduler after an interruption.

    `entries` is [(name, status, why)] for the runs the interruption could
    plausibly have been in the middle of -- read off the registry by the caller,
    which is the only part of this that touches state.

    WHY THIS IS NOT A CONSTANT STRING. The ctrl-c handler used to print
    "Nothing reached the scheduler." unconditionally, from a function that had
    looked at nothing. It is usually true -- nothing reaches Slurm without a
    typed /approve -- and "usually true" is the wrong standard for the one
    sentence on the screen whose entire job is to stop somebody going to check.
    The case it is wrong in is the expensive one: ctrl-c during /approve, after
    begin_submission() has written and while the submission command is running.

    So a run already past approval makes this say so instead. AFTER_APPROVAL is
    the same set every other reader of a record uses, and `why` comes from
    classify() -- this invents no opinion of its own, it only decides which of
    two true things is the one worth printing.
    """
    risky = [(name, why) for name, status, why in entries or ()
             if status in AFTER_APPROVAL]
    if not risky:
        return "Nothing reached the scheduler — the conversation is still here."
    if len(risky) == 1:
        name, why = risky[0]
        return (f"'{name}' had already been approved"
                + (f" — {why}" if why else "")
                + f". /check {name} before assuming either way.")
    return (f"{len(risky)} runs here had already been approved — "
            f"/list before assuming nothing ran.")


class Standing:
    """What a record's evidence supports: a status, and why it says so.

    `why` is not decoration. It goes into the record as `reconciled_because`,
    into /list's row, and into the migration report -- a status that changed
    under somebody without saying what changed it is the failure this whole
    mechanism exists to stop repeating.
    """

    __slots__ = ("status", "why", "outcome")

    def __init__(self, status, why="", outcome=None):
        self.status = status
        self.why = why
        self.outcome = outcome

    def __eq__(self, other):
        return (isinstance(other, Standing) and self.status == other.status
                and self.why == other.why)

    def __repr__(self):
        return f"<Standing {self.status}: {self.why}>"


def classify(record, evidence=None):
    """The status this record's evidence supports, as a Standing.

    PRECEDENCE, and the order is the design:

      0. A terminal record is never reclassified. Somebody said no, or the
         artifacts are gone. Neither is a state evidence can argue with.

      1. A record nobody here proposed -- /scan, /track -- is left alone. It
         has no thread and no gate, and "no live interrupt" is not news about
         a run that never had one.

      2. SUBMISSION EVIDENCE WINS OVER EVERYTHING BELOW IT. This is the rule
         that `test-now` cost us: 46 jobs went to the scheduler, the turn that
         would have recorded them died, and the record still said `held` three
         weeks later because nothing ever asked whether it had run. A run that
         has spent an approval cannot be awaiting one, whatever the checkpoint
         looks like and whatever the registry last wrote down.

      3. No proposal means nothing to approve.

      4. A REJECTED revision can never come back. Checked before the gate,
         because a live interrupt for a rejected proposal is exactly the shape
         of the bug: the graph does not know a person said no, only the record
         does.

      5. A SUPERSEDED revision is not the current one. /modify moved on.

      6. Then, and only then, the gate. It must exist AND authorise THIS
         proposal -- an interrupt raised for an older revision is not a
         decision about the one on the record, which is the difference between
         "a gate exists" and "this proposal is awaiting approval".

      7. Evidence that could not be gathered changes nothing.

    `evidence` keys, all optional, all defaulting to UNKNOWN:

        gate           True | False | UNKNOWN -- a live GATE-kind interrupt is
                       parked on this run's thread
        gate_revision  the revision that interrupt authorises, or None when it
                       carries none (every interrupt raised before identity
                       existed), or UNKNOWN when the gate was not inspected
        outcome        an Outcome from reconcile(), when submission evidence
                       was found and graded
        submitted      True when something proves the command ran, even if the
                       outcome could not be graded
    """
    evidence = evidence or {}
    status = record.get("status")

    if status in TERMINAL:
        return Standing(status, "terminal — nothing reclassifies it")

    if (record.get("source") or "agent") != "agent":
        return Standing(status, f"adopted from {record.get('source')} "
                                f"— never had a gate")

    # ---- 2. submission evidence, ahead of everything else ---------------- #
    #
    # RECONCILIATION ADVANCES A RECORD; IT DOES NOT CHURN ONE. A record that
    # has already been graded keeps its verdict, and the single exception is
    # SUBMITTING -- the one status that means "this was in flight when we last
    # looked", which is the only claim about the past that goes stale.
    #
    # Without that exception this pass re-grades every settled run on every
    # startup, from evidence that has aged: a job list purged from scratch, a
    # `# TOTAL: 0 jobs` header, an accounting record beyond sacct's retention.
    # It demoted a correctly-submitted run to `submit_unknown` the first time
    # it was tried, on the strength of a job list that was no longer there.
    settled = status in AFTER_APPROVAL and status != SUBMITTING
    outcome = evidence.get("outcome")
    if outcome is not None and not settled:
        return Standing(outcome.status,
                        outcome.detail or "reconciled from submission evidence",
                        outcome)
    if evidence.get("submitted") is True and not settled:
        return Standing(SUBMIT_UNKNOWN,
                        "the command ran, but the outcome could not be graded")
    if status in AFTER_APPROVAL:
        # Already past the gate and nothing new to say about it. Left exactly
        # as it is rather than being re-graded from silence.
        return Standing(status, "already submitted")

    # ---- 3-5. the proposal itself ---------------------------------------- #
    if not record.get("proposal"):
        return Standing(LAPSED, "no proposal on record")

    revision = record.get("revision")
    if revision:
        if revision in (record.get("rejected") or ()):
            return Standing(ABANDONED, "this proposal was rejected")
        if revision in (record.get("superseded") or ()):
            return Standing(LAPSED, "this proposal was superseded by a change")

    # ---- 6-7. the gate ---------------------------------------------------- #
    gate = evidence.get("gate", UNKNOWN)
    if gate is UNKNOWN:
        return Standing(status, "the checkpoint could not be consulted")
    if not gate:
        return Standing(LAPSED, "no live gate — the decision was not left open")

    gate_revision = evidence.get("gate_revision", UNKNOWN)
    if (gate_revision is not UNKNOWN and gate_revision and revision
            and gate_revision != revision):
        return Standing(LAPSED, "the open gate is for an earlier version of "
                                "this run")

    return Standing(HELD, "a decision is open for this exact proposal")


def jobs_are_unreachable(record):
    """This run submitted jobs, and none of them can be looked at. True/False.

    The state `test-now` and `ampliconseq_demo` are in, and it needed a name
    because three different screens were each inventing their own wrong answer
    for it.

    What is KNOWN: the command ran and put N jobs on the scheduler. Reconciling
    them recovered the count from the conversation's own record of the launch.
    What is NOT known: anything about the individual jobs -- there is no
    manifest on disk, so there is no list of ids to ask Slurm about, and after
    this long sacct would not remember them anyway.

    The three wrong answers this replaces, all from the same missing
    distinction -- "no job list" being read as "no jobs":

      /list    "nothing to run", which says GenPipes generated no work.
               46 jobs is not no work.
      /check   "either every step was already up to date, or GenPipes wrote its
               list outside the directories searched" -- a guess between two
               possibilities, offered after the answer had been established.
      /view    offered /check and /diagnose, neither of which can do anything
               without a manifest.

    Distinct from a GONE run, and the difference is real rather than
    bookkeeping: GONE means a list existed at a path we recorded and has since
    been purged. This means one was never recorded in the first place, which is
    what happened to every run submitted before begin_submission() existed.
    """
    if record.get("status") not in AFTER_APPROVAL:
        return False
    if record.get("job_list"):
        return False
    seen = record.get("jobs_seen")
    return bool(seen)


def submitted_nothing(record):
    """This submission created no jobs BECAUSE there was nothing left to do,
    and somebody counted. True/False.

    The evidenced half of an absence that used to be read as a claim. A run
    with `submitted` and no job list is one of two completely different things:

      nothing to do   reconcile() saw a script declaring `# TOTAL: 0`, a clean
                      exit, and no new job rows -- three facts agreeing that
                      GenPipes generated no work because every output the run
                      asked for was already on disk. record_outcome() then
                      stored jobs_seen 0 and expected_jobs 0. This is a real,
                      successful, terminal outcome and any run can reach it by
                      being re-run after it has already finished.

      nobody counted  a record from before begin_submission() existed, or one
                      whose reconciliation never ran. jobs_seen is None, which
                      means unmeasured -- NOT zero. Absence of a manifest is
                      not evidence of absence of a submission (see
                      ran_already), so nothing here may be read as success.

    /list used to render both as "already up to date", which turned "we never
    looked" into "it finished cleanly with nothing to do" -- a confirmed
    success invented out of a missing field. This is the predicate that
    separates them, so the second one can say `unknown` instead.

    Deliberately narrower than jobs_are_unreachable's AFTER_APPROVAL: only
    `submitted` means the submission itself is established. `submitting`,
    `submit_failed` and `submit_unknown` are all unfinished business, and none
    of them may borrow this answer.
    """
    if record.get("status") != SUBMITTED:
        return False
    if record.get("job_list"):
        return False
    for field in ("jobs_seen", "expected_jobs"):
        value = record.get(field)
        # `is not None` and an int, because the whole distinction is
        # "counted zero" against "not counted". A bool is excluded on
        # principle: True == 1 and False == 0 in Python, and neither is a
        # job count.
        if isinstance(value, int) and not isinstance(value, bool) and value == 0:
            return True
    return False


def ran_already(record):
    """Durable, filesystem-only proof that this run's command has run.

    True, or UNKNOWN. Never False -- and that asymmetry is the point. A run
    with no job list anywhere may have submitted nothing, or may have been a
    submission where every step was already up to date, which writes no list at
    all and is a real successful outcome. Absence of a manifest is not evidence
    of absence of a submission, so this reports only what it can prove.

    Deliberately does not touch the checkpoint or Slurm. This is the half of
    the evidence that survives a lost conversation and an aged-out accounting
    database, which is what makes it worth asking first.
    """
    path = record.get("job_list")
    if path and os.path.exists(path):
        return True
    script = record.get("submitted_script")
    if script:
        declared = declared_job_list(script)
        if declared and os.path.exists(declared):
            after = job_list_state(declared)
            added = rows_added(record.get("job_list_baseline"), after)
            if added:
                return True
            # The file exists and this approval added nothing to it. That is
            # evidence the command RAN -- GenPipes wrote the header -- and no
            # evidence about what it submitted. Enough to stop calling the run
            # pending; not enough to grade.
            if after and after.get("rows"):
                return True
    return UNKNOWN


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
                    job_id, name, deps, log = fields
                    jobs.append(Job(job_id=job_id or None, name=name,
                                    log=log or None,
                                    deps=[d for d in deps.split(":") if d]))
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

    A RECORD WITH NO JOB LIST IS NOT A MATCH, whatever directory it names, and
    that exception is a bug fix rather than a refinement. A held proposal has
    never launched -- that is what held means -- so a job_output directory
    beside it was written by some other run, quite possibly one this tool has
    never seen. Matching on workdir alone made the held proposal `hi-0724`
    "own" every run that had ever executed in its directory, and /scan refused
    to adopt a real, finished, launched run with the words "already known as
    hi-0724". They were never the same run. A workdir identifies a run only
    once that run has actually produced something.
    """
    for record in registry.load():
        if not record.get("job_list"):
            continue
        if record["job_list"] == found["job_list"]:
            return record
        if record.get("workdir") and os.path.abspath(record["workdir"]) == found["workdir"]:
            return record
    return None


# What /scan should do about a run it found on disk. Three outcomes, and the
# distinction between them is the whole of the fix.
ADOPT = "adopt"        # nothing on record matches; mint an identity
RESTORE = "restore"    # an identity exists and had lost its artifacts or its
                       # place in /list. Re-point it; do not mint a second one.
KNOWN = "known"        # already on record, already current. Nothing to do.


class Rediscovery:
    """What a discovered run means for the registry: an action and why.

    `record` is the existing identity when there is one, so the caller can name
    it rather than saying "already known" and leaving somebody to guess which
    of forty rows that was.
    """

    __slots__ = ("action", "record", "reason")

    def __init__(self, action, record=None, reason=""):
        self.action = action
        self.record = record
        self.reason = reason

    @property
    def name(self):
        return (self.record or {}).get("name")

    def __repr__(self):
        return f"<Rediscovery {self.action} {self.name!r}>"


def rediscovery(registry, found):
    """What to do about `found`: adopt it, restore an identity, or nothing.

    WHY RESTORING RATHER THAN REFUSING. "already known as hi-0724" was the
    whole of what /scan could say, and it was the wrong answer to three
    different questions. A run hidden by /sort is on record and not in /list,
    so finding it again should put it back. A run marked `gone` because its
    job list had been cleaned off the cluster is on record and pointing at a
    file that had disappeared -- and here is that file, so the record can be
    made true again. Only the third case, a run already on record and already
    current, is genuinely nothing to do.

    None of the three may mint a second identity for one job list, and the
    restore path never touches disk, never resubmits, and never rewrites what
    the run IS -- not its source, not its proposal, not the conversation it
    came from. It re-points a record at artifacts that are demonstrably there.

    An agent-built run whose directory has since produced a DIFFERENT job list
    is deliberately left alone. Re-pointing it would break the link between a
    run and the submission it actually made, which is the one thing its record
    is for; the newer job list is somebody else's run and can be adopted on its
    own.
    """
    record = already_known(registry, found)
    if record is None:
        return Rediscovery(ADOPT)

    same_file = record.get("job_list") == found["job_list"]
    hidden = bool(record.get("hidden"))
    lost = record.get("status") == GONE or bool(record.get("gone"))

    if same_file:
        if hidden and lost:
            return Rediscovery(RESTORE, record,
                               "its job list is back, and it was hidden from "
                               "/list")
        if lost:
            return Rediscovery(RESTORE, record, "its job list is back")
        if hidden:
            return Rediscovery(RESTORE, record, "it was hidden from /list")
        return Rediscovery(KNOWN, record, "already tracked, and up to date")

    # Same directory, a different job list: another attempt at the same run.
    if (record.get("source") or "agent") == "agent":
        return Rediscovery(KNOWN, record,
                           "a run built here owns this directory — the newer "
                           "job list here is not the submission it made")
    return Rediscovery(RESTORE, record, "a newer job list in the same directory")


def _run(cmd, env=None, cwd=None):
    """Run a shell command, returning (stdout+stderr, returncode).

    Every cluster call in this module funnels through here so a missing
    scheduler behaves like an empty answer rather than an exception: on a laptop
    there is no sacct, and the right response to that is "I don't know the
    states", not a traceback in the middle of the interface.

    `cwd` matters for exactly one caller and matters absolutely there: a
    generation and the submission that follows it have to run in the directory
    the run was built in, or every relative `-o`, `-g` and `-c` on the command
    line means a different file. See run_block.
    """
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                             executable="/bin/bash", env=env, cwd=cwd or None)
    except OSError as e:
        return f"{e}", 127
    return out.stdout + (("\n" + out.stderr) if out.stderr else ""), out.returncode


def run_block(block, cwd=None):
    """Run a recorded command block, as written, where it was written.

    (output, exit code). Used to re-run a proposal's own generation and then to
    launch the script it produces -- the two halves of what /approve does --
    and it is deliberately dumb: the block already ran once, from this process,
    in this directory, so re-running it byte-for-byte is the version least
    likely to do something different the second time.

    The one addition is a `module load` fallback, for the case where the model
    loaded GenPipes in an EARLIER block than the one recorded here. That load
    lived in a shell that is long gone, so the recorded block alone would come
    back `genpipes: command not found`. Prepended only when the block does not
    load anything itself and genpipes is not already on PATH, and its failure
    is swallowed (`;`, not `&&`) so that a machine with no module system runs
    the block exactly as it would have anyway.
    """
    text = str(block or "").strip()
    if not text:
        return "", 0
    if "module load" not in text and shutil.which("genpipes") is None:
        text = f"module load {GENPIPES_MODULE} >/dev/null 2>&1; {text}"
    return _run(text, cwd=cwd)


# `genpipes <pipeline> --help` output, and the flag surface parsed out of it,
# both keyed and both kept for the life of the process.
#
# Caching is safe for exactly one reason and it is worth naming: the module
# version is PINNED (GENPIPES_MODULE above), so within one process --help is a
# constant. It is worth doing because the callers are interactive. The step
# panel asks for it on every /modify, the gate asks for the flag surface on
# every proposal, and a quarter-second of subprocess under a keystroke is the
# difference between a panel that opens and a panel that hesitates.
_HELP_CACHE = {}
_USAGE_CACHE = {}


def pipeline_help(pipeline, protocol=None):
    """`genpipes <pipeline> [-t <protocol>] --help`, as text.

    The only authority on what a protocol's steps are, what flags it takes, and
    which of those flags argparse will not run without. There is no step table
    in this repo and there must not be one: genpipes.md says so outright,
    because the numbered list is version-exact and a copy here would be wrong on
    the next GenPipes release while looking authoritative.

    Returns "" when it cannot be reached -- on a laptop, or with no module
    system -- and every caller must treat that as "no opinion", never as "no
    problem". A "" is cached like any other answer: a machine with no GenPipes
    on it will not acquire one mid-session, and retrying the failure under every
    keystroke is how a missing module system turns into a slow interface.
    """
    if not pipeline:
        return ""
    key = (str(pipeline), str(protocol or ""))
    if key in _HELP_CACHE:
        return _HELP_CACHE[key]
    proto = f" -t {protocol}" if protocol else ""
    raw, code = _run(f"module load {GENPIPES_MODULE} >/dev/null 2>&1; "
                     f"genpipes {pipeline}{proto} --help")
    # argparse exits non-zero on some installs after printing perfectly good
    # help, so the text is trusted whenever it looks like help -- a usage line
    # or a step list -- rather than only when the exit code was clean.
    text = raw if (code == 0 or "Steps:" in raw or "usage:" in raw) else ""
    _HELP_CACHE[key] = text
    return text


def pipeline_usage(pipeline):
    """Which flags this pipeline takes and which it requires, as a usage.Usage.

    Falsy when --help could not be read, which callers must let through
    untouched: an unreadable install is not evidence that a command is missing
    a flag, and manufacturing a refusal from a missing module system would make
    the tool unusable in exactly the place it is hardest to debug.

    No protocol is passed. `-t` changes the STEP list and not the flag surface
    -- checked across all ten pipelines on 6.1.1 -- so one lookup per pipeline
    serves every protocol, and the step panel's own per-protocol fetch stays
    separate.
    """
    if not pipeline:
        return usage.Usage()
    key = str(pipeline)
    if key not in _USAGE_CACHE:
        _USAGE_CACHE[key] = usage.read(pipeline_help(pipeline), pipeline)
    return _USAGE_CACHE[key]


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
        # The id as well as the name. The name is what a person reads; the id
        # is what sacct, squeue, the log filename and the manifest's dependency
        # column all agree on, and it is the key downstream_of() walks.
        "job_id": first.job_id,
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


# ===========================================================================
#  /list's grouping: one classification, shared so /check all and /list
#  cannot drift on what "needs attention" means. Stdlib-only, no colour, no
#  printing -- display.py owns rendering.
# ===========================================================================

HELD_BUCKET = "held"
# A proposal with no live gate. Its own bucket rather than a flavour of HELD,
# because the two differ in the only way a /list row has to get right: what you
# can do about it. HELD offers /approve; this one cannot, and says so.
LAPSED_BUCKET = "lapsed"
ACTIVE_BUCKET = "active"
ATTENTION_BUCKET = "attention"
FINISHED_BUCKET = "finished"
UNAVAILABLE_BUCKET = "unavailable"

# THE ORDER RUNS ARE PRESENTED IN. One list, because a collection that
# rearranges itself between two screens is a collection nobody can learn.
#
# /list and /sort used to disagree completely: /list grouped by state and
# sorted each group by age, /sort showed the registry's raw append order. So
# the row somebody had just read as fourth from the top in /list was
# seventeenth in the panel where they went to hide it, and the only way to find
# it was to read every line. /sort is /list with selectors on it; it has to
# look like /list.
#
# Needing attention is deliberately not first. What is FIRST is what is waiting
# on a decision from the person reading -- a held proposal is the only row here
# that stops until somebody acts -- and a broken run, however urgent, has
# already happened.
SECTION_ORDER = (HELD_BUCKET, ACTIVE_BUCKET, ATTENTION_BUCKET,
                 LAPSED_BUCKET, FINISHED_BUCKET, UNAVAILABLE_BUCKET)


def _listing_key(record):
    """When a run entered its current state, for ordering inside a bucket.

    submitted_at where there is one, held_at otherwise: the moment the row
    started being what it now is. Empty sorts first, which puts records too old
    to carry either timestamp at the top of their group rather than scattering
    them.
    """
    return str(record.get("submitted_at") or record.get("held_at") or "")


def listing_order(rows):
    """[(bucket, record, status)] in the one order both screens present.

    `rows` is resolve_all()'s shape -- [(record, RunStatus or None), ...].

    Deliberately here rather than in display.py, even though /list is the
    screen it was written for. It is a policy about the registry, not about
    rendering, and putting it beside the bucket rule it depends on is what lets
    a caller with no terminal -- /sort's option builder, a test -- ask for the
    same order without importing the renderer.
    """
    grouped = {bucket: [] for bucket in SECTION_ORDER}
    for record, status in rows:
        grouped[list_bucket(record, status)].append((record, status))
    for entries in grouped.values():
        entries.sort(key=lambda rs: _listing_key(rs[0]))
    return [(bucket, record, status)
            for bucket in SECTION_ORDER
            for record, status in grouped[bucket]]


def list_bucket(record, status):
    """held / active / attention / finished / unavailable for one /list row.

    `status` is what resolve_all() returned for this record: a RunStatus, or
    None for a held run or one whose submission created no jobs at all (every
    step was already up to date).

    Order is the whole design:

      1. held is checked first and unconditionally -- an awaiting-approval
         run has no jobs to have an opinion about.
      2. unavailable is checked before anything job-shaped. A scheduler that
         could not be reached tells us nothing about this run's jobs, and
         must never be read as though it did -- not as a failure, not as
         "needs attention", not as anything but "we don't know right now".
      3. a broken, doomed or UNKNOWN job puts the run in ATTENTION even when
         something else in it is still queued or running. A run that is half
         failed and half active already needs a person; it is not LIVE just
         because part of it has not caught up to the bad news yet.
      4. only after all of that does "still has active jobs" mean LIVE.
    """
    if record.get("status") == HELD:
        return HELD_BUCKET
    if record.get("status") == LAPSED:
        return LAPSED_BUCKET
    # Checked before `status is None`, which would otherwise file an unresolved
    # submission under FINISHED and report a run that may have half a pipeline
    # on the cluster as though it had completed cleanly.
    if record.get("status") in (SUBMITTING, SUBMIT_FAILED, SUBMIT_UNKNOWN):
        return ATTENTION_BUCKET
    if status is None:
        return FINISHED_BUCKET
    if status.source == "unavailable":
        return UNAVAILABLE_BUCKET
    # A manifest with nothing in it is not a run that is still going. resolve()
    # already words this "no jobs"; without this line the tallies below are all
    # zero, nothing matches, and the run falls through to LIVE -- a listing
    # claiming something is queued when there is not a single job to queue.
    if not status.total and not status.counts:
        return FINISHED_BUCKET
    broke = sum(n for s, n in status.counts.items() if s in BROKE_STATES)
    if broke or status.doomed or status.unknown:
        return ATTENTION_BUCKET
    if status.finished:
        return FINISHED_BUCKET
    return ACTIVE_BUCKET


def list_line(status):
    """The plain-text (no colour) one-line job tally for a /list row. None
    when there is no status to tally -- a held run, or a submission that
    created no jobs at all.

    ATTENTION is worded from the SAME cause check_all() already uses
    (failed, then doomed, then unaccounted-for) rather than a second
    vocabulary for the same finding -- see resolve()'s BROKE_STATES comment
    for why a downstream cancellation is not itself the cause.
    """
    if status is None:
        return None
    tally = status.counts
    broke = sum(n for s, n in tally.items() if s in BROKE_STATES)
    active = sum(n for s, n in tally.items() if s in ACTIVE_STATES)
    if broke or status.doomed or status.unknown:
        cause = (f"{broke} failed" if broke else
                 f"{status.doomed} will never run" if status.doomed else
                 f"{status.unknown} unaccounted for")
        # Still-active jobs are named rather than left implicit: a run that is
        # half broken and half still burning allocation is a different
        # decision from one that is simply over, and the difference is the
        # whole reason to look at this row now rather than later. The cause is
        # kept in both cases -- the row's tag already says something is wrong,
        # so what this line owes is the number, not the adjective again.
        return (f"{cause}  ·  {active} still running" if active
                else f"{cause}  ·  nothing still running")
    running = tally.get("RUNNING", 0)
    queued = tally.get("PENDING", 0)
    done = tally.get("COMPLETED", 0)
    # Counted here, not folded into "failed" above: a cancellation with
    # nothing broken behind it is somebody's decision, not a fault, and it is
    # the only thing separating a run that finished from one that was stopped.
    cancelled = tally.get("CANCELLED", 0)
    parts = [p for p in (
        f"{running} running" if running else None,
        f"{queued} queued" if queued else None,
        f"{done} completed" if done else None,
        f"{cancelled} cancelled" if cancelled else None,
    ) if p]
    return "  ·  ".join(parts) if parts else "queued"


# The one word each row is tagged with. Same vocabulary everywhere a run's
# lifecycle state is named -- /list's rows and /modify's fork notice both read
# from here, so the two can never end up calling the same run different things.
LIST_TAG = {
    HELD_BUCKET: "held",
    LAPSED_BUCKET: "needs rebuilding",
    ACTIVE_BUCKET: "live",
    ATTENTION_BUCKET: "needs attention",
    FINISHED_BUCKET: "completed",
    UNAVAILABLE_BUCKET: "status unavailable",
}

CANCELLED_TAG = "cancelled"


def list_tag(record, status):
    """The tag word for one /list row: held, live, needs attention, completed,
    cancelled, or status unavailable.

    Everything comes from list_bucket() except the one distinction a bucket
    cannot carry: a run that ended because somebody stopped it. Nothing in it
    broke, so it is not ATTENTION, and it is terminal, so it lands in
    FINISHED -- but tagging it "completed" would report a cancellation as a
    success, which is the one thing a status line must never do.
    """
    bucket = list_bucket(record, status)
    if (bucket == FINISHED_BUCKET and status is not None
            and status.counts.get("CANCELLED")):
        return CANCELLED_TAG
    return LIST_TAG[bucket]


# A GenPipes artifact filename ends in the run's own timestamp:
#   gatk_sam_to_fastq.tumorPair_COLO829N_2026-07-30T16.17.43.o
# That stamp is the only thing in the name that distinguishes one execution of a
# step from another, which makes it this module's notion of run identity for
# files on disk. Captured from the manifest, never invented.
_LOG_STAMP = re.compile(r"_(\d{4}-\d\d-\d\dT[\d.]+)\.(?:o|sh|submit)$")


def run_stamp(record, jobs=None):
    """The timestamp that identifies this run's artifacts on disk, or None.

    Read from the manifest, in the order the manifest states it most plainly:
    the job_list filename first (`<Pipeline>.<protocol>.job_list.<STAMP>`), then
    a declared log path, which carries the same stamp in its own name.

    None means the run's identity could not be established from what is
    recorded -- and callers must treat that as "cannot verify" rather than as
    permission to match on something weaker. See resolve_log().
    """
    listing = record.get("job_list")
    if listing:
        found = _JOB_LIST.match(os.path.basename(listing))
        if found and found.group("stamp"):
            return found.group("stamp")
    for job in jobs or ():
        if job.log:
            found = _LOG_STAMP.search(os.path.basename(job.log))
            if found:
                return found.group(1)
    return None


def declared_log(job, record):
    """The log path the job_list declares, resolved against disk, or None.

    Column 4 of a GenPipes job_list is relative to `job_output/`, not to the
    directory the run was launched from:

        17957639\tgatk_sam_to_fastq.tumorPair_COLO829N\t\tgatk_sam_to_fastq/gat…o

    Joining it to `workdir` alone -- which is what this did -- produces
    `<workdir>/gatk_sam_to_fastq/…` and misses every time, so the declared path,
    the one artifact that carries the run's own timestamp, never resolved and
    every lookup fell through to the name glob below. That was not a rare
    fallback: on a 46-job run it was 46 of 46.

    All three joins are tried because the second and third are cheap and a
    manifest that ever writes an absolute path, or one already including
    `job_output/`, should still resolve.
    """
    if not job.log:
        return None
    for base in _log_roots(record):
        for path in (os.path.join(base, job.log),
                     os.path.join(base, "job_output", job.log)):
            if os.path.isfile(path):
                return path
    return job.log if os.path.isfile(job.log) else None


def _log_roots(record):
    """The directories this run's `job_output/` paths may be relative to,
    most authoritative first.

    THE MANIFEST'S OWN DIRECTORY COMES FIRST, and that is the fix for a whole
    class of runs rather than a special case. `workdir` is written by
    record_outcome() as os.getcwd() -- the directory the ASSISTANT was launched
    from -- while the run itself writes wherever `-o` sent it. Those are the
    same directory only when somebody happened to start the app inside the run
    directory, and on 2026-08-05 they were not:

        workdir    /home/pbourque/genpipe-workflow-assistant
        job_list   /home/pbourque/genpipes_somatic_fastpass/COLO829_.../job_output/

    Every declared path was therefore joined to the wrong root, every lookup
    missed, and /diagnose reported "log not found for this run" for 46 jobs
    whose logs were all sitting on disk. The model was then asked to explain a
    timeout with no in-job evidence, and did what anybody would: it reasoned
    from the absence.

    The job_list path is the RUN'S OWN identity -- it is the manifest this run
    wrote, recorded at submission -- so anchoring on its directory is stricter
    than anchoring on workdir, not looser. config_trace() has always resolved
    this way, which is why the trace was found for the same run whose logs
    were not.

    workdir is kept as a fallback for records that have no job_list path, and
    the ORDER matters: a run whose manifest is known is never resolved against
    a directory that merely happens to be the current one.
    """
    roots = []
    listing = record.get("job_list")
    if listing:
        # dirname(job_list) IS job_output/, which is what column 4 is relative
        # to; its parent covers a manifest that writes `job_output/...` itself.
        here = os.path.dirname(listing)
        if here:
            roots.append(here)
            parent = os.path.dirname(here)
            if parent:
                roots.append(parent)
    workdir = record.get("workdir")
    if workdir:
        roots.append(workdir)
    roots.append(os.getcwd())
    seen, out = set(), []
    for r in roots:
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def resolve_log(job, record, stamp=None):
    """Find THIS run's log file for a job on disk, returning a path or None.

    The declared path first. When it misses -- a run adopted from a directory
    that has since been reorganised, a manifest written by an older GenPipes --
    the fallbacks are allowed only where they can still name one execution:

      the job id      a Slurm id belongs to exactly one submission, so a file
                      carrying it cannot be another run's.
      the run stamp   the job name plus this run's timestamp. The name ALONE is
                      not identity: `trim_fastp.tumorPair_COLO829N*.o` matches
                      every time that step has ever run in this directory, and
                      glob returns them in os.scandir order, so the answer was
                      whichever file the filesystem happened to list first.

    That is the July-30/August-5 collision: a job CANCELLED on July 30 wrote no
    log at all, the glob found the August 5 run's, and forty lines of an
    unrelated -- successful -- execution went into the diagnosis prompt as this
    run's evidence.

    So a log that cannot be tied to this run is NOT RETURNED. `not found` is a
    true statement about a run and a useful one; another run's file is neither.
    """
    found = declared_log(job, record)
    if found:
        return found

    # Searched under the SAME roots the declared path is resolved against --
    # see _log_roots. Globbing `workdir/job_output` while the manifest says the
    # run lives somewhere else searches a directory belonging to no run at all.
    for base in _log_roots(record):
        output = base if os.path.basename(base) == "job_output" else \
            os.path.join(base, "job_output")
        if job.job_id:
            for path in sorted(glob.glob(os.path.join(output, "**",
                                                      f"*{job.job_id}*"),
                                         recursive=True)):
                if os.path.isfile(path):
                    return path
        if job.name and stamp:
            for path in sorted(glob.glob(os.path.join(output, "**",
                                                      f"{job.name}*{stamp}*.o"),
                                         recursive=True)):
                if os.path.isfile(path):
                    return path
    return None


def sibling_script(log):
    """The .sh GenPipes wrote next to a .o, or None.

    genpipes.md calls this "the artifact most often skipped and most often
    decisive": it is what the tool was TOLD to do, with every flag and input
    path, and it separates "needed more resources" from "given the wrong input".
    Named here and not read -- see triage()'s note on why the path is the
    deliverable.
    """
    if not log:
        return None
    root = log[:-2] if log.endswith(".o") else os.path.splitext(log)[0]
    path = f"{root}.sh"
    return path if os.path.isfile(path) else None


def config_trace(record, stamp=None):
    """This run's `<Pipeline>.<protocol>.<STAMP>.config.trace.ini`, or None.

    Which ini section produced which value, for the exact generation this run
    came from. Located by the manifest's own stamp and returned only if that
    file is really there: a trace from a neighbouring generation would answer
    config questions about a different command, which is the same class of
    mistake resolve_log() exists to prevent.
    """
    listing = record.get("job_list")
    if not listing:
        return None
    found = _JOB_LIST.match(os.path.basename(listing))
    if not found:
        return None
    stamp = stamp or found.group("stamp")
    if not stamp:
        return None
    name = found.group("pipeline")
    if found.group("protocol"):
        name = f"{name}.{found.group('protocol')}"
    workdir = record.get("workdir") or os.getcwd()
    for base in (workdir, os.path.dirname(os.path.dirname(listing) or "."),
                 os.path.dirname(listing) or "."):
        path = os.path.join(base, f"{name}.{stamp}.config.trace.ini")
        if os.path.isfile(path):
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


def downstream_of(jobs, job_id):
    """Every job that waited on `job_id`, directly or through a chain.

    The transitive closure over the manifest's dependency column. This is the
    only place the DAG exists: sacct never had it, and squeue forgets it the
    moment the jobs go terminal.

    WHAT IT IS FOR. "32 jobs were CANCELLED" is a count over the whole run and
    says nothing about whether this failure caused them -- a person running
    scancel produces the same number. The closure is what turns the count into
    a statement about THIS job, and it is what the model went and read the raw
    job_list to reconstruct.

    Returns a set of ids, never including `job_id` itself. Cycles cannot occur
    in a submitted Slurm DAG, and a malformed manifest that contained one would
    still terminate here because `seen` gates the walk.
    """
    if not job_id:
        return set()
    waiting = {}
    for j in jobs or ():
        for dep in j.deps or ():
            waiting.setdefault(dep, []).append(j.job_id)
    seen, queue = set(), [job_id]
    while queue:
        current = queue.pop()
        for child in waiting.get(current, ()):
            if child and child not in seen:
                seen.add(child)
                queue.append(child)
    seen.discard(job_id)
    return seen


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

    stamp = run_stamp(record, jobs)
    findings = []
    for step in order[:limit]:
        group = by_step[step]
        first = group[0]
        # A JOB THAT NEVER RAN HAS NOTHING TO READ.
        #
        # CANCELLED means the scheduler killed it in the queue because something
        # upstream failed: it never started, so it never wrote a log. Asking the
        # disk for one anyway is how a diagnosis ends up quoting a DIFFERENT
        # run's file -- the only `.o` with that job's name in it belongs to some
        # other execution of the same step. The state stays visible, because the
        # cancellation is a real fact about this run; only the search stops.
        ran = first.state in BROKE_STATES
        path = resolve_log(first, record, stamp) if ran else None
        findings.append({
            "step": step,
            "count": len(group),
            "job": first.name,
            "job_id": first.job_id,
            "state": first.state,
            "maxrss": first.maxrss,
            "exit_code": first.exit_code,
            "ran": ran,
            "log": path,
            "log_tail": tail(path, log_lines) if path else None,
            # NAMED, NOT READ. genpipes.md calls the .sh decisive and the
            # trace necessary for cross-checking, but pasting both for every
            # finding would put tens of kilobytes into every prompt to answer a
            # question that has not been asked yet. So the path is established
            # here -- which is evidence identity, and deterministic -- and
            # whether it is worth opening stays with the reader.
            "script": sibling_script(path),
        })
    return {
        "failed_total": len(failed),
        "broke_total": sum(1 for j in failed if j.state in BROKE_STATES),
        "cancelled_total": sum(1 for j in failed if j.state == "CANCELLED"),
        "steps_affected": len(order),
        "truncated": max(0, len(order) - limit),
        "stamp": stamp,
        "trace": config_trace(record, stamp),
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
    exists: understanding what GenPipes said is not a rendering concern. Keeping
    it out of the renderer is what lets a test assert on these numbers without a
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


# ===========================================================================
#  Focus: the run the person is visibly working on.
#
#  WHAT MAKES THIS FACTUAL RATHER THAN A GUESS, because a "what did they mean"
#  feature is exactly the kind this project deletes on sight.
#
#  Focus is only ever set from a run name somebody TYPED as the argument of a
#  slash command. `/view foo` is not evidence that foo was mentioned; it is
#  the user having named foo, unambiguously, in a position where nothing else
#  can go. That is the same class of fact as "this file is on disk" -- it is
#  read off the command line, not off prose.
#
#  Nothing here reads a sentence. "should I use foo or bar?" sets no focus,
#  because it is not a command and never reaches this code. That is not a
#  filter applied to prose; it is that prose has no argument position.
#
#  And focus only ever REORDERS a list somebody is already being offered. It
#  never adds a candidate, never selects one, and never survives the run
#  leaving the set -- /approve after approving foo does not offer foo again,
#  because foo is no longer held and the ranking below only moves rows that
#  the caller had already decided to show.
# ===========================================================================

class Focus:
    """The last run named explicitly on a command line, if it still exists.

    One mutable field, deliberately. A stack of recent runs sounds better and
    is worse: the second-most-recent thing you looked at is not a thing anyone
    reasons about, and a ranking with two answers in it stops being
    predictable, which is the only property this feature has to have.
    """

    __slots__ = ("name",)

    def __init__(self, name=None):
        self.name = name or None

    def note(self, command, args, known=None):
        """Record `args[0]` as the focus, if this command takes a run name.

        `known` is a callable that returns the record for a name, or None. It
        is consulted so that a typo does not become the focus -- a name that
        is not in the registry is not a run somebody is working on, it is a
        mistake, and ranking by it would push a real row down the list for
        nothing.

        Returns the focus after the update, so callers can chain.
        """
        if command in NAMES_A_RUN and args:
            candidate = str(args[0]).strip()
            if candidate and (known is None or known(candidate)):
                self.name = candidate
        return self.name

    def clear(self):
        self.name = None

    def rank(self, rows):
        """`rows` -- [(name, note), ...] -- with the focused run moved first.

        Order-preserving otherwise, and a no-op when the focused run is not in
        `rows`. Both matter: the caller decided which runs are legal for this
        command, and this must not second-guess it in either direction.
        """
        if not self.name:
            return list(rows)
        first = [r for r in rows if r and r[0] == self.name]
        return first + [r for r in rows if not (r and r[0] == self.name)] \
            if first else list(rows)

    def __repr__(self):
        return f"<Focus {self.name!r}>"


# Commands whose FIRST argument is the name of an existing run. Kept beside
# Focus rather than in cli.py so that "which commands name a run" is one fact
# the completion menu and the focus rule share, and so it can be asserted in
# CI without importing the agent stack.
#
# /track is absent on purpose: its first argument names a run that does NOT
# exist yet, which is the opposite of what focus means.
NAMES_A_RUN = frozenset({
    "approve", "reject", "modify", "fork", "view",
    "check", "jobs", "diagnose", "cancel", "monitor", "hold",
})
