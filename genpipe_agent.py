# /home/pbourque/genpipe_agent.py

"""GenpipeA1 — subclass of Biomni's A1 agent that inserts a human-approval gate before any
GenPipes submission, without reimplementing Biomni's agent loop.

Design:
  * super().configure() builds the system prompt (with genpipes.md) and Biomni's
    ungated graph. We keep the prompt work untouched and reuse Biomni's own
    `generate` and `execute` nodes by reference, so this file never copies them.
  * We rebuild the graph with one extra node, `submission_gate`, that only fires
    for code the matcher classifies as a submission to the cluster (bash cmd.sh,
    or the DRAC chunk_genpipes.sh / submit_genpipes pair). Generations
    (`genpipes ... -g`) and reads (squeue, sacct) pass straight through, ungated.
  * The gate calls interrupt(), which freezes the run until a human resumes it.
    Freezing means saving the graph's state, which requires a checkpointer —
    LangGraph's component for persisting state. SqliteSaver is the disk-backed
    one, and it must be passed into compile(); attaching a checkpointer after
    compile (as stock A1 does with MemorySaver) does not enable interrupt.
    Because it writes to disk, the same saved state that holds the pause is what
    lets a run be resumed from a fresh session.

Driving the gated graph (in this file):
  run(prompt, thread_id) streams until the graph finishes or pauses, and returns
  a status dict. resume(thread_id, approved, feedback) continues a paused run by
  streaming Command(resume={"approved": bool, "feedback": str}). Both end by
  calling _gate_status(), which reads the checkpoint and reports "paused" (with
  the proposal from _build_proposal) or "done". These replace Biomni's stock go(),
  which streams once in values mode and cannot detect or resume an interrupt.

Where the rest of the logic lives:
  Two stdlib-only modules hold everything that does not need a graph, so both
  are testable without installing biomni (which is what lets CI check them on
  every push):

    gate_rules.py  the gate's decision logic -- is this code a submission, and
                   what exactly is being proposed. The methods on this class are
                   thin delegates, so the graph's routing and the tests exercise
                   one implementation rather than two.
    runs.py        runs and jobs. A RUN is one GenPipes invocation you named and
                   approved; a JOB is one of the hundreds of Slurm jobs it
                   creates. The registry (runs.jsonl) is durable and ours; job
                   state is always read live from the scheduler.

Monitoring, in three sizes:
  check(name)      GenPipes' own log_report for the run: one aggregate progress
                   view. Deterministic, no model, and the result is cached on the
                   record so /list can show where things stood without re-asking.
  jobs(name)       every individual job and its Slurm state -- the per-job view
                   check() aggregates away.
  why(name, ...)   the only one that costs a model call. runs.triage() first
                   establishes deterministically WHICH jobs failed and reads
                   their logs, then the model is asked to explain the cause. It
                   runs on its own thread (see why()'s docstring) so it can
                   never disturb the run it is diagnosing.

A run enters runs.jsonl at the GATE, not at submission -- see runs.py's
docstring on why "held" exists. The short version: a paused run whose name lives
only in your memory is not a durable gate.
"""
import os
import re
import sqlite3
import uuid
import json
import datetime
import display
import gate_rules
import runs as runs_store
import time
import warnings

# Noise from LangGraph's own checkpoint serializer, triggered just by importing
# it -- an unset-default warning about a kwarg this codebase never passes and
# has no reason to. Not something a user of this tool can act on.
#
# A plain filterwarnings("ignore", ...) doesn't hold: partway through this
# same import chain, langchain_core re-registers a "default" (show) filter for
# this exact warning class, which -- being added later -- outranks an ignore
# filter set beforehand, no matter where it's placed in the import order.
# Patching the actual display step sidesteps that registration race instead of
# trying to win it.
_orig_showwarning = warnings.showwarning


def _showwarning(message, category, filename, lineno, file=None, line=None):
    if "allowed_objects" in str(message):
        return
    _orig_showwarning(message, category, filename, lineno, file, line)


warnings.showwarning = _showwarning

from biomni.utils import pretty_print
from biomni.agent import A1
from biomni.agent.a1 import AgentState          # the state schema Biomni's graph uses
from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import interrupt, Command


class GenpipeA1(A1):
    # --------------------------------------------------------------------- #
    #  Graph construction: reuse A1's nodes, splice in the submission gate.  #
    # --------------------------------------------------------------------- #
    def configure(self, self_critic=False, test_time_scale_round=0):
        if self_critic:
            raise NotImplementedError(
                "GenpipeA1's gated graph supports the standard (non-self-critic) "
                "loop only. Run with self_critic=False."
            )

        # 1. Let A1 do its normal work: build the system prompt (injecting
        #    genpipes.md) and compile its ungated graph into self.app.
        super().configure(self_critic=False,
                          test_time_scale_round=test_time_scale_round)

        # A1 defaults to <execute> even for simple questions. Nudge it to awnser
        # conceptual questions directly via <solution>. Biomni already supports
        # answering directly via the <solution> tag, but never tells the model 
        # WHEN to prefer it. We append that missing criterion. 
        self.system_prompt += (
            "\n\nAnswering conceptual or factual questions: if a question can be "
            "answered from your own knowledge and does not require running code, "
            "reading files, or using the environment, respond directly in a "
            "<solution> block with a clear, complete explanation, and do not use "
            "<execute>. Only use <execute> when the task genuinely requires "
            "running code, inspecting data, or driving GenPipes."
        )
        # 2. Borrow A1's real nodes from the compiled graph. No reimplementation.
        generate = self.app.nodes["generate"].bound
        execute = self.app.nodes["execute"].bound

        # 3. This is a checkpointer from the LangGraph library : the component
        #    Langraph uses to persist a graph's state. The gate pauses with
        #    interrupt(), and a pause can only survive if the graph's state is
        #    written to disk rather than held in memory. SqliteSaver is LangGraph's 
        #    disk-backed checkpointer: it writes a snapshot after every graph step
        #    into a SQLite file, tagged by thread_id, so a paused run can be resumed
        #    later, even in a fresh process. One store covers both: the same on-disk
        #    snapshot that holds the pause is what lets a closed and reopened session
        #    pick the run back up. No second store is needed.
        if not hasattr(self, "_gate_checkpointer"):
            db_path = os.path.join(self.path, "genpipe_checkpoints.sqlite")
            conn = sqlite3.connect(db_path, check_same_thread=False)
            self._gate_checkpointer = SqliteSaver(conn)
        self.checkpointer = self._gate_checkpointer

        # 4. Routing out of generate: identical to A1 except that code the
        #    matcher flags as a submission is diverted to the gate.
        def routing_function(state):
            next_step = state.get("next_step")
            if next_step == "execute":
                code = self._extract_pending_code(state)
                if code and self._is_submission(code):
                    return "gate"
                return "execute"
            if next_step in ("generate", "end"):
                return next_step
            raise ValueError(f"Unexpected next_step: {next_step}")

        # 5. Routing out of the gate: approve -> run it, adjust -> rethink.
        def gate_routing(state):
            return "execute" if state.get("next_step") == "execute" else "generate"

        # 6. The gate node. Pure before interrupt() (safe to re-run on resume).
        def submission_gate(state):
            code = self._extract_pending_code(state)
            reply = interrupt(self._build_proposal(state, code))   # <-- PAUSES here
            if reply.get("approved"):
                state["next_step"] = "execute"                     # let it run
            else:
                note = reply.get("feedback") or "Adjust the command before resubmitting."
                state["messages"].append(HumanMessage(
                    content=(f"The proposed submission was not approved. {note} "
                             f"Regenerate the command accordingly.")))
                state["next_step"] = "generate"                    # loop back to rethink
            return state

        # 7. Rebuild the graph with the gate on the path to submission only.
        workflow = StateGraph(AgentState)
        workflow.add_node("generate", generate)
        workflow.add_node("execute", execute)
        workflow.add_node("submission_gate", submission_gate)
        workflow.add_conditional_edges(
            "generate", routing_function,
            path_map={"execute": "execute", "generate": "generate",
                      "gate": "submission_gate", "end": END})
        workflow.add_conditional_edges(
            "submission_gate", gate_routing,
            path_map={"execute": "execute", "generate": "generate"})
        workflow.add_edge("execute", "generate")
        workflow.add_edge(START, "generate")

        self.app = workflow.compile(checkpointer=self.checkpointer)
    
    # --------------------------------------------------------------------- #
    #  Runs and jobs. The store itself is runs.Registry (stdlib-only, in     #
    #  runs.py); what lives here is only the part that needs the agent --    #
    #  the working directory a submission ran in, and the model call in      #
    #  why(). Everything else delegates.                                    #
    # --------------------------------------------------------------------- #
    @property
    def registry(self):
        """The run registry, created on first use.

        Lazy rather than set in configure() because configure() is called during
        A1.__init__ and again by add_software(), and because test_gate.py builds
        a bare instance with object.__new__ -- no __init__ at all. A property
        means the registry exists whenever it is actually needed and never
        requires the constructor to have run.
        """
        if not hasattr(self, "_registry"):
            self._registry = runs_store.Registry(self.path)
        return self._registry

    def track(self, name, job_list_path):
        """Register a run launched outside the agent -- no thread_id, no prior
        conversation -- so check()/list/history can find it by name just like an
        agent-launched run."""
        job_list_path = os.path.abspath(job_list_path)
        if not os.path.exists(job_list_path):
            display.problem(f"'{job_list_path}' does not exist -- nothing tracked.")
            return
        self.registry.track(name, job_list_path)
        display.tracked(name, job_list_path)

    def submissions(self):
        """List every run still worth acting on: held runs awaiting a decision,
        and submitted runs whose artifacts are still on disk.

        A run whose job_list has vanished is pruned first, silently -- see
        history() to find it anyway. Held runs are listed even though nothing of
        theirs is on the scheduler yet, because a pending approval is the most
        actionable thing this tool can be holding."""
        records = self.registry.live()
        if not records:
            display.nothing("No runs recorded yet.",
                            "Describe a pipeline in plain English to start one.")
            return
        display.run_list(records)

    def history(self):
        """List every recorded run, live and gone, newest first. Unlike
        submissions(), nothing is hidden -- this is how you find a run again
        after its job_list file has been cleaned up from Rorqual."""
        records = self.registry.all()
        if not records:
            display.nothing("No runs recorded yet.")
            return
        display.history(records)

    def pending(self):
        """Runs paused at the gate. Surfaced at startup, so a decision left
        behind in a previous session is not silently lost."""
        return self.registry.held()

    def job_list_for(self, name):
        """The job list recorded for a run, or None if there is no live record."""
        record = self.registry.get(name)
        if record is None or record["status"] == runs_store.GONE:
            return None
        return record["job_list"]

    # --------------------------------------------------------------------- #
    #  Driving layer: gated replacements for A1.go that survive the pause.   #
    # --------------------------------------------------------------------- #

    def _config(self, thread_id):
        return {"recursion_limit": 500,
                "configurable": {"thread_id": str(thread_id)}}

    def _stream(self, payload, config, on_step=None):
        """Drive the graph, rendering every message as it arrives.

        Shared by run(), resume() and why() so all three render identically and
        the transcript looks the same regardless of which one produced it.

        Each message is drawn by display.render (the structured, coloured view)
        and also stored as plain text in self.log. pretty_print with
        printout=False still returns the formatted string, it just doesn't print
        it, so the log keeps a clean uncoloured copy of everything.

        on_step, if given, is called with each message -- used to drive the
        spinner's label from what the agent is actually doing, so a long run
        reports "running cmd.sh" rather than "thinking" for two minutes.
        """
        self.log = []
        for s in self.app.stream(payload, stream_mode="values", config=config):
            msg = s["messages"][-1]
            display.render(msg)
            self.log.append(pretty_print(msg, printout=False))
            if on_step:
                on_step(msg)

    def run(self, prompt, thread_id, on_step=None):
        """Gated replacement for go(). Streams until the graph either finishes
        or pauses at the submission gate, and returns a status dict.

        thread_id is required and names the run. It does double duty: it is the
        checkpoint key LangGraph uses to save and later resume this run's state,
        and it is the human-facing handle for the run afterward, used to look up
        its job list and check progress. Because a single run can pause for
        human approval and be resumed in a separate session, and because its
        submitted jobs outlive the conversation, every run needs a stable name
        the caller chooses up front rather than one assigned behind the scenes.
        """
        # Reset per-run state, mirroring what go() does at the top of a task.
        self.critic_count = 0
        self.user_task = prompt
        if getattr(self, "use_tool_retriever", False):
            selected = self._prepare_resources_for_retrieval(prompt)
            self.update_system_prompt_with_selected_resources(selected)

        # The thread_id is the label this run is saved under in the checkpoint.
        # Every step gets stored under that label, so passing the same thread_id
        # later finds this exact run and resumes it.
        config = self._config(thread_id)
        inputs = {"messages": [HumanMessage(content=prompt)], "next_step": None}

        # values mode gives no signal on a pause, so we don't inspect here;
        # _gate_status reads the checkpoint afterward to tell which happened.
        self._stream(inputs, config, on_step)
        return self._settle(thread_id, config, task=prompt)

    def resume(self, thread_id, approved, feedback=None, on_step=None):
        """Continue a run paused at the submission gate. approved=True lets the
        command execute; approved=False sends feedback back to generate. Safe to
        call when nothing is paused: it just reports current status.

        On approval, the submission actually runs and GenPipes writes a job list
        file. That is the only moment the run's job list exists, so this is where
        the run's name is linked to that file for later progress lookups.
        """
        started = time.time()
        config = self._config(thread_id)
        snap = self.app.get_state(config)
        if not (snap.next and snap.tasks and snap.tasks[0].interrupts):
            return self._gate_status(config)
        command = Command(resume={"approved": bool(approved), "feedback": feedback})
        self._stream(command, config, on_step)
        if approved:
            self._record_submission(thread_id, started)
        return self._settle(thread_id, config)

    def _settle(self, thread_id, config, task=None):
        """Classify where the run ended up, record it, and show the consequence.

        Both run() and resume() end here so that reaching the gate always has the
        same three effects, in the same order, regardless of which call got
        there: the pause is persisted to the registry, the approval box is drawn,
        and the status dict is returned.

        Persisting BEFORE drawing is the point. Everything on screen is lost the
        moment the terminal closes; the record is what makes the pending decision
        findable again tomorrow.
        """
        status = self._gate_status(config)
        if status["status"] == "paused":
            proposal = status["proposal"]
            self.registry.hold(thread_id, thread_id, proposal, os.getcwd())
            if task:
                self.registry.update(thread_id, task=task)
            display.gate(proposal, status["thread_id"])
        return status

    def _gate_status(self, config):
        """Read the checkpoint after a stream ends and classify the run: either
        paused at the submission gate (returning the proposal to approve) or
        finished (returning the final message). Both run and resume call this so
        the two outcomes are reported through one consistent status dict.

        Uses LangGraph 0.3.18's snapshot API. The important detail is where a
        paused run's proposal lives: not on the snapshot directly, but on the
        pending task's interrupt. snap.next names the node the graph is parked
        before, and snap.tasks[0].interrupts[0].value holds the payload passed to
        interrupt() in the gate. That exact path was confirmed empirically for
        this version; snap.interrupts does not exist.
        """
        snap = self.app.get_state(config)
        thread_id = config["configurable"]["thread_id"]

        # Paused iff the graph is parked before a node (snap.next non-empty) AND
        # that pending task is actually sitting on an interrupt. Checking all
        # three guards avoids an IndexError when tasks is empty on a finished run.
        if snap.next and snap.tasks and snap.tasks[0].interrupts:
            return {"status": "paused",
                    "proposal": snap.tasks[0].interrupts[0].value,
                    "thread_id": thread_id}

        # Otherwise the run finished. snap.values can be empty for a thread that
        # never ran (unknown thread_id), so guard before indexing messages and
        # return final=None rather than raising.
        msgs = snap.values.get("messages") if snap.values else None
        return {"status": "done",
                "final": msgs[-1].content if msgs else None,
                "thread_id": thread_id}
    # --------------------------------------------------------------------- #
    #  Gate helpers. Thin delegates to gate_rules, which holds the actual     #
    #  logic as pure functions over plain data.                               #
    #                                                                        #
    #  The split is not tidiness: gate_rules imports nothing but the standard #
    #  library, so the one property that must never regress -- "does this     #
    #  code submit to a scheduler?" -- is checked on every push in seconds,   #
    #  without installing biomni. These methods stay because the graph's      #
    #  routing_function and test_gate.py both call them, and delegating means #
    #  there is one implementation rather than two that can drift.            #
    # --------------------------------------------------------------------- #
    def _extract_pending_code(self, state):
        return gate_rules.extract_pending_code(state.get("messages"))

    def _is_submission(self, code):
        return gate_rules.is_submission(code)

    def _executable_lines(self, code):
        return gate_rules.executable_lines(code)

    def _flag_value(self, cmd, flag):
        return gate_rules.flag_value(cmd, flag)

    def _submission_line(self, code):
        return gate_rules.submission_line(code)

    def _generation_command(self, state):
        return gate_rules.generation_command(state.get("messages"))

    def _build_proposal(self, state, code):
        return gate_rules.build_proposal(state.get("messages"), code)

    # --------------------------------------------------------------------- #
    #  Monitoring, in three sizes. check() is one aggregate number, jobs() is #
    #  every job, why() is the only one that costs a model call.             #
    # --------------------------------------------------------------------- #
    def _record_submission(self, name, since):
        """Called from resume() right after an approved submission runs. Records
        the job list GenPipes just wrote, promoting the run from held to
        submitted and pinning the directory it ran in.

        `since` is the time the submission started. Only a job list written after
        that moment belongs to this run. Without this guard, a submission that
        creates zero jobs (everything already up to date) writes no list at all,
        and the glob would silently grab the newest list from a PREVIOUS run --
        linking this name to another run's jobs.

        The working directory is recorded rather than inferred. A job's log path
        in the job list is relative to wherever the submission ran, so without
        this a later /why can only find logs if you happen to still be sitting in
        the same directory. Pinning it here is what makes analysis work from
        anywhere, in any later session.
        """
        import glob as _glob

        workdir = os.getcwd()
        lists = _glob.glob(os.path.join(workdir, "job_output", "*job_list*"))

        # Keep only job lists written by THIS submission, not a stale one.
        lists = [f for f in lists if os.path.getmtime(f) >= since]
        newest = max(lists, key=os.path.getmtime) if lists else None

        # Promote even when there is no list: "every step already up to date"
        # produces no jobs, and that is a successful outcome, not a run still
        # awaiting approval.
        held = self.registry.get(name)
        proposal = (held or {}).get("proposal")
        self.registry.mark_submitted(name, newest, workdir=workdir,
                                     proposal=proposal, thread_id=name)

    def _need_run(self, name, needs_jobs=True):
        """Resolve a name to a record, explaining the failure if it can't.

        Every monitoring command starts with the same three questions -- does
        this run exist, is it still on disk, does it have jobs to look at -- and
        the answers are far more useful than "None". A held run in particular is
        not a missing run; it is a run waiting for you, and saying so turns a
        dead end into the next thing to type.
        """
        record = self.registry.get(name)
        if record is None:
            display.problem(f"No run named '{name}'.", "/list shows what there is.")
            return None
        if record["status"] == runs_store.HELD:
            display.problem(f"'{name}' hasn't been submitted yet -- it's waiting "
                            f"for approval.", f"/approve {name}")
            return None
        if record["status"] == runs_store.GONE:
            display.problem(f"'{name}' ran, but its job list is no longer on disk.",
                            "/history still has the record.")
            return None
        if needs_jobs and not record["job_list"]:
            display.problem(f"'{name}' created no jobs -- every step was already "
                            f"up to date.")
            return None
        return record

    def check(self, name):
        """Show a run's progress: GenPipes' own log_report, drawn as a bar.

        This is the cheap, deterministic answer to "how is it going" -- no model,
        no cost, and the number GenPipes itself would give you. The result is
        cached on the record so /list can show where each run stood without
        re-running a module load per row.
        """
        record = self._need_run(name)
        if record is None:
            return None
        raw = runs_store.log_report(record["job_list"])
        parsed = runs_store.parse_log_report(raw)
        if parsed["total"]:
            self.registry.remember_check(name, parsed["counts"], parsed["total"],
                                         runs_store.verdict(parsed["counts"]))
        display.status(name, parsed, raw)
        return raw

    def jobs(self, name, only_failed=False):
        """List the individual Slurm jobs inside a run, with their live states.

        check() answers "how is the run doing"; this answers "which jobs".
        States come from sacct rather than from any cached record, because the
        scheduler is the only authority on whether a job is still alive.
        """
        record = self._need_run(name)
        if record is None:
            return []
        jobs = runs_store.jobs_for(record)
        if not jobs:
            display.problem(f"'{name}' has a job list, but no jobs could be read "
                            f"from it.", os.path.basename(record["job_list"] or ""))
            return []
        # The tally is always over ALL the jobs, even when only failures are
        # shown: the cached verdict describes the run, and computing it from a
        # filtered view would record "everything failed" for a healthy run.
        tally = runs_store.counts(jobs)
        self.registry.remember_check(name, tally, len(jobs),
                                     runs_store.verdict(tally))
        display.jobs(name, jobs, only_failed=only_failed)
        return [j for j in jobs if j.failed] if only_failed else jobs

    def cancel(self, name):
        """scancel every job in a run that is still pending or running.

        The counterpart to the gate. A tool careful enough to stop before
        submitting should also be able to stop after -- otherwise the moment you
        realise a run is wrong is the moment the tool stops being useful.
        Returns the number of jobs targeted.
        """
        record = self._need_run(name)
        if record is None:
            return 0
        jobs = runs_store.jobs_for(record)
        n, raw = runs_store.cancel(jobs)
        display.cancelled(name, n, raw)
        if n:
            self.registry.add_note(name, f"cancelled {n} job(s)")
        return n

    def why(self, name, question=None, on_step=None):
        """Ask the model why a run failed, having first established what failed.

        Two deliberate decisions here.

        1. THE FACTS ARE GATHERED BEFORE THE MODEL IS INVOLVED. runs.triage()
           asks Slurm which jobs failed and reads their logs from disk. A
           GenPipes run has hundreds of .o files; a model told to "go look" burns
           an enormous amount of context rediscovering what one sacct call
           already knows, and then answers vaguely. So the model is handed the
           failed steps and the relevant log text, and spends its reasoning on
           the cause instead of the search.

        2. IT RUNS ON ITS OWN THREAD, never the run's. Biomni's AgentState
           declares `messages` with no reducer, which makes it a last-value-wins
           channel: passing new inputs to an existing thread REPLACES its message
           list instead of appending. Re-using the run's thread would therefore
           erase the conversation that built the pipeline -- and, if the run were
           parked at the gate, the pending interrupt with it. A sibling thread
           gets the analysis without ever touching the state that /approve
           depends on.

        The investigation is read-only in intent, but it is not trusted to be:
        it goes through the same gated graph, so if the model decides to
        resubmit something, it stops at the gate like anything else.
        """
        record = self._need_run(name)
        if record is None:
            return None

        report = runs_store.triage(record)
        if not report["failed_total"]:
            display.problem(f"Nothing in '{name}' has failed.",
                            f"/check {name} for where it's up to.")
            return None

        display.triage(name, report)

        thread = f"{name}::why-{datetime.datetime.now():%m%d%H%M%S}"
        prompt = self._why_prompt(name, record, report, question)
        self.critic_count = 0
        self.user_task = prompt
        self._stream({"messages": [HumanMessage(content=prompt)], "next_step": None},
                     self._config(thread), on_step)
        status = self._gate_status(self._config(thread))

        # Record the conclusion on the RUN, not the investigation thread, so it
        # shows up next to the run in /history months later.
        if status.get("final"):
            self.registry.add_note(name, _one_line(status["final"]))
        return status

    def _why_prompt(self, name, record, report, question):
        """Build the diagnosis prompt out of facts, not prose.

        Every value here was parsed from a command or read from the scheduler, so
        the model is arguing from the same evidence the user can see on screen
        above it. The instruction to answer in a <solution> block matters: without
        it the agent's default is to start running code, and the first thing worth
        having is a hypothesis, not more shell.
        """
        slots = (record.get("proposal") or {}).get("slots") or {}
        lines = [
            f"A GenPipes run named '{name}' has failed jobs. Diagnose the cause.",
            "",
            "What is known, gathered from the scheduler and the run's own files:",
            f"  working directory: {record.get('workdir') or 'unknown'}",
        ]
        for label, key in (("pipeline", "pipeline"), ("protocol", "protocol"),
                           ("steps", "steps"), ("readset", "readset")):
            if slots.get(key):
                lines.append(f"  {label}: {slots[key]}")
        if slots.get("inis"):
            lines.append(f"  config layering: {' , '.join(slots['inis'])}")
        if record.get("job_list"):
            lines.append(f"  job list: {record['job_list']}")
        lines += [
            f"  failed jobs: {report['failed_total']} "
            f"across {report['steps_affected']} step(s)",
            "",
        ]
        for f in report["findings"]:
            lines.append(f"--- step {f['step']}: {f['count']} job(s) {f['state']} ---")
            if f["maxrss"]:
                lines.append(f"    peak memory: {f['maxrss']}")
            if f["exit_code"]:
                lines.append(f"    exit code: {f['exit_code']}")
            lines.append(f"    log: {f['log'] or 'not found on disk'}")
            if f["log_tail"]:
                lines.append("    tail of that log:")
                lines += [f"      {l}" for l in f["log_tail"].splitlines()]
            lines.append("")
        if question:
            lines += [f"The user specifically asks: {question}", ""]
        lines += [
            "Explain, in a <solution> block: the single most likely cause, the "
            "evidence in the logs above that supports it, and the concrete fix "
            "(which ini key, which resource, which file). If a resubmission is "
            "needed, say exactly which steps to rerun -- do not resubmit now.",
            "Only use <execute> if you genuinely need to read a file that is not "
            "quoted above.",
        ]
        return "\n".join(lines)


def _one_line(text, limit=140):
    """Squash a model's answer to one line for the run's note field.

    The <solution> wrapper is stripped: the tag is an artifact of how the agent
    talks to itself, and leaving it in means /history six weeks later reads as
    markup rather than as a finding.
    """
    flat = re.sub(r"</?solution>", " ", text or "")
    flat = " ".join(flat.split())
    return flat[:limit - 1] + "…" if len(flat) > limit else flat
